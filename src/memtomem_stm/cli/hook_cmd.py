"""``mms hook`` — bridge a host's built-in tool calls into STM's surfacing.

Claude Code (and compatible hosts) invoke a ``PostToolUse`` hook as a plain
command: the hook payload arrives as JSON on **stdin** and the hook replies
with JSON on **stdout**. This subcommand reads that payload, runs STM's
proactive memory surfacing over the built-in tool's output, and — when
relevant LTM memories are found — returns them through ``additionalContext``
so the host appends them next to the tool result the model reads.

Two stages, two hook fields. *Surfacing* appends ``additionalContext`` (the
side-effect-free play). *Compression* (P1a) *replaces* a built-in tool's output
via ``updatedToolOutput`` and is **Bash-only and opt-in**
(``MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1``): for the built-in Bash tool the
value must be a structured object mirroring the tool result, so we clone the
original ``tool_response`` and shrink only its ``stdout`` channel (preserving
``stderr`` / exit / ``interrupted`` / ``isImage`` verbatim). Compression stays
Bash-only because replacing ``Read`` would break a later ``Edit`` whose
``old_string`` must match the file verbatim. The two halves merge into one
``hookSpecificOutput`` (:func:`_orchestrate` / :func:`_build_hook_output`).

Tool scope: we only surface for **read-like** built-in tools
(:data:`_DEFAULT_SURFACE_TOOLS` — Read/Grep/Glob/Bash, overridable via
``MEMTOMEM_STM_HOOK_SURFACE_TOOLS``). This allowlist is enforced here rather
than relying on the host's hook ``matcher`` (a too-broad matcher would
otherwise feed Write/Edit through) — surfacing on a write is semantically
wrong, and a write's inputs (file contents, ``old_string``/``new_string``)
should never become a search query. To keep extracted queries (which for
``Bash`` may include secret-bearing commands) off disk, this MVP path runs
**without** the feedback store, so no query text is persisted; the feedback
loop, cross-session dedup, and a privacy gate for ingestion arrive with the
daemon stage (P2/Stage D).

Hard rule — a hook must never disrupt the host. This command **always exits
0** and, on any malformed input, disabled surfacing, timeout, or internal
error, emits an empty object ``{}`` so the original tool result passes through
untouched.

Register in Claude Code ``settings.json``::

    {"hooks": {"PostToolUse": [
      {"matcher": "Read|Grep|Glob|Bash",
       "hooks": [{"type": "command", "command": "mms hook"}]}]}}

Performance — daemon by default: surfacing routes through the local daemon
(``HookConfig.use_daemon`` defaults ``True``), a warm LTM connection that turns
each call into a sub-second round trip. The daemon is auto-spawned on first use
(``auto_spawn``), so the first call typically returns ``{}`` while it warms up
and later calls hit the warm process. The legacy in-process path spawns a fresh
LTM MCP child per call, whose cold start (embedding + reranker model load)
measured ~6s here — exceeding the default ``surfacing.timeout_seconds`` of 3.0,
so it usually times out to ``{}``. Opt out with
``MEMTOMEM_STM_HOOK__USE_DAEMON=0``; when the daemon is unreachable the hook
degrades per ``HookConfig.fallback`` — ``skip`` (default) returns ``{}``,
``cold`` runs the in-process path. To exercise the cold path directly, set
``MEMTOMEM_STM_HOOK__USE_DAEMON=0``, raise
``MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS`` (e.g. 8) and lower
``MEMTOMEM_STM_SURFACING__MIN_RESPONSE_CHARS``, accepting the per-call latency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import TYPE_CHECKING, Any, TextIO

import click

if TYPE_CHECKING:
    from memtomem_stm.config import HookCompressionConfig, STMConfig

logger = logging.getLogger(__name__)

# Delimiters emitted by ``SurfacingFormatter`` around the injected block. We
# slice this back out so ``additionalContext`` carries only the memories, not
# a duplicate of the tool output the engine was handed as ``response_text``.
_SURFACED_OPEN = "<surfaced-memories>"
_SURFACED_CLOSE = "</surfaced-memories>"

# Read-like built-in tools we surface against. Write/Edit/MultiEdit are
# deliberately excluded (see module docstring). Matched case-sensitively
# against Claude Code's PascalCase tool names — note the surfacing gate's own
# ``write_tool_patterns`` can't be relied on here because ``fnmatch`` is
# case-normalizing (identity on POSIX), so ``*write*`` never matches ``Write``.
_DEFAULT_SURFACE_TOOLS = frozenset({"Read", "Grep", "Glob", "Bash"})

# Unique marker prepended to compressed ``stdout`` so a re-fired hook recognizes
# its own output and no-ops (idempotency). Matched *exactly* — never via
# ``TruncateCompressor``'s generic markers (``(truncated`` / ``(original:`` /
# ``… omitted``), which appear in real logs and would falsely skip compression.
_COMPRESS_SENTINEL = "⟦stm-compressed⟧"

# Hard cap on the ``tool_response`` text forwarded to surfacing, independent of
# the compression budget. Keeps a multi-MB built-in result from overflowing the
# daemon's ``MAX_MESSAGE_BYTES`` (4 MiB) frame even when compression is disabled
# or no-ops. Comfortably below 4 MiB after JSON escaping; surfacing only needs
# enough text to clear its ``min_response_chars`` gate (the query comes from
# ``tool_input``), so capping costs nothing for relevance.
_SAFE_DAEMON_BUDGET = 256 * 1024


def _surface_tools() -> frozenset[str]:
    """Allowlist of tool names to surface for, overridable via env.

    ``MEMTOMEM_STM_HOOK_SURFACE_TOOLS`` is a comma-separated list; empty or
    unset falls back to :data:`_DEFAULT_SURFACE_TOOLS`.
    """
    raw = os.environ.get("MEMTOMEM_STM_HOOK_SURFACE_TOOLS")
    if not raw:
        return _DEFAULT_SURFACE_TOOLS
    tools = {t.strip() for t in raw.split(",") if t.strip()}
    return frozenset(tools) or _DEFAULT_SURFACE_TOOLS


def _hook_budget_seconds() -> float:
    """Outer wall-clock backstop for the whole hook call.

    ``SurfacingEngine`` bounds its own LTM search via
    ``surfacing.timeout_seconds``; this only guards against a hang *outside*
    that window (e.g. tearing down a wedged LTM subprocess). Sized to sit above
    the configured surfacing timeout so raising that env to beat LTM cold start
    isn't silently clipped.
    """
    raw = os.environ.get("MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS")
    try:
        configured = float(raw) if raw else 3.0
    except ValueError:
        configured = 3.0
    return max(configured + 8.0, 12.0)


def _tool_response_to_text(tool_response: Any) -> str:
    """Best-effort flatten of a built-in tool's output to a single string.

    The surfaced *query* is derived from ``tool_input`` (the file path /
    pattern / command), so this text matters mainly for surfacing's size gate
    (``min_response_chars``). Shapes differ per tool and per host version, so
    stay permissive: strings pass through; dicts surrender common text-ish
    fields (``stdout``/``stderr``/``content``/``text``/``output``/``result``)
    or fall back to a JSON dump; lists join their flattened parts.
    """
    if tool_response is None:
        return ""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, list):
        return "\n".join(_tool_response_to_text(part) for part in tool_response)
    if isinstance(tool_response, dict):
        parts = [
            value
            for key in ("stdout", "stderr", "content", "text", "output", "result")
            if isinstance(value := tool_response.get(key), str) and value
        ]
        if parts:
            return "\n".join(parts)
        try:
            return json.dumps(tool_response, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(tool_response)
    return str(tool_response)


def _bash_stdout(tool_response: Any) -> tuple[str | None, dict[str, Any] | None]:
    """Extract the compressible ``stdout`` channel of a Bash tool result.

    Returns ``(stdout_text, original_dict)`` when ``tool_response`` is a dict
    carrying a string ``stdout`` (so the caller can clone the dict and replace
    only that field, preserving ``stderr`` / exit / ``interrupted`` / ``isImage``
    verbatim), or ``(text, None)`` when ``tool_response`` is itself a plain
    string (replace wholesale). ``(None, None)`` when there is nothing
    string-like to compress. Unlike :func:`_tool_response_to_text` this never
    folds ``stderr`` into the compressible channel — replacement must not lose
    the error stream or merge it into stdout.
    """
    if isinstance(tool_response, str):
        return tool_response, None
    if isinstance(tool_response, dict):
        stdout = tool_response.get("stdout")
        if isinstance(stdout, str):
            return stdout, tool_response
    return None, None


def _already_compressed(text: str) -> bool:
    """Whether ``text`` already carries our compression sentinel (idempotency)."""
    return _COMPRESS_SENTINEL in text


def maybe_compress_builtin(payload: dict[str, Any], cfg: "HookCompressionConfig") -> Any | None:
    """Compress a Bash tool's ``stdout`` for the PostToolUse ``updatedToolOutput``.

    Returns the value to place in ``updatedToolOutput`` — a dict mirroring the
    original Bash ``tool_response`` with only ``stdout`` shrunk (every other
    field preserved verbatim), or a plain string when the original response was a
    string — or ``None`` to leave the tool output untouched. **Never raises.**

    Gate: ``cfg.enabled`` + PostToolUse + ``tool_name == "Bash"`` + ``stdout``
    longer than ``cfg.max_chars`` + not already compressed. The strategy is fixed
    to :class:`TruncateCompressor` (self-contained — no chunk-store callback,
    unlike SELECTIVE/HYBRID/PROGRESSIVE, whose retrieval tools live in the
    separate ``mms`` server process).
    """
    try:
        if not cfg.enabled:
            return None
        if (payload.get("hook_event_name") or "PostToolUse") != "PostToolUse":
            return None
        if payload.get("tool_name") != "Bash":
            return None
        stdout, original = _bash_stdout(payload.get("tool_response"))
        if stdout is None or len(stdout) <= cfg.max_chars or _already_compressed(stdout):
            return None

        # Lazy import: keep ``mms hook --help`` and the no-op path off the
        # compression module's import cost.
        from memtomem_stm.proxy.compression import TruncateCompressor

        compressed = TruncateCompressor().compress(stdout, max_chars=cfg.max_chars)
        compressed = f"{_COMPRESS_SENTINEL}\n{compressed}"
        if original is None:
            return compressed
        clone = dict(original)
        clone["stdout"] = compressed
        return clone
    except Exception:
        logger.debug(
            "builtin output compression failed — leaving tool output unchanged", exc_info=True
        )
        return None


def _bounded_surfacing_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``payload`` with its ``tool_response`` text capped to
    :data:`_SAFE_DAEMON_BUDGET`.

    Independent of compression: a multi-MB built-in result must never overflow
    the daemon's 4 MiB frame, even when compression is disabled or no-ops.
    Pass-through (same object) when the response is already small, so the common
    case is untouched. The cap keeps the head of the flattened text (stdout, then
    stderr), which is all surfacing needs to clear its size gate.
    """
    text = _tool_response_to_text(payload.get("tool_response"))
    if len(text) <= _SAFE_DAEMON_BUDGET:
        return payload
    return {**payload, "tool_response": text[:_SAFE_DAEMON_BUDGET]}


def _extract_surfaced_block(original: str, injected: str, injection_mode: str) -> str | None:
    """Return just the ``<surfaced-memories>…</surfaced-memories>`` block.

    ``SurfacingEngine.surface`` returns ``response_text`` with the block
    prepended or appended (or returns it unchanged when nothing surfaced).
    ``injection_mode`` tells us which side the formatter added, so we recover
    the exact delta; we then sanity-check the tag delimiters and fall back to a
    direct tag scan if the slice looks wrong (defensive against formatter
    changes). Returns ``None`` when nothing was surfaced.
    """
    if injected == original or len(injected) <= len(original):
        return None
    if injection_mode == "prepend":
        block = injected[: len(injected) - len(original)]
    else:  # "append" / "section"
        block = injected[len(original) :]
    block = block.strip()
    if block.startswith(_SURFACED_OPEN) and block.endswith(_SURFACED_CLOSE):
        return block
    start = injected.find(_SURFACED_OPEN)
    end = injected.rfind(_SURFACED_CLOSE)
    if start == -1 or end == -1:
        return None
    return injected[start : end + len(_SURFACED_CLOSE)]


async def run_surfacing_hook(
    payload: dict[str, Any], *, engine: Any | None = None
) -> dict[str, Any]:
    """Core hook logic. Returns the hook-output dict (``{}`` means no-op).

    **Never raises** — every failure path (bad payload, disabled surfacing,
    LTM error/timeout, internal bug) degrades to ``{}`` so any caller (the CLI
    today, a daemon/HTTP adapter later) can emit it and let the tool output
    pass through. ``engine`` is a test seam: when provided it is used as-is
    (caller owns its lifecycle); when ``None``, an engine + LTM adapter are
    built from :class:`STMConfig` and torn down before returning.
    """
    try:
        return await _run_surfacing_hook_inner(payload, engine=engine)
    except Exception:
        logger.warning("hook surfacing failed — passing tool output through", exc_info=True)
        return {}


async def _run_surfacing_hook_inner(
    payload: dict[str, Any], *, engine: Any | None
) -> dict[str, Any]:
    if (payload.get("hook_event_name") or "PostToolUse") != "PostToolUse":
        return {}
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or tool_name not in _surface_tools():
        return {}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    response_text = _tool_response_to_text(payload.get("tool_response"))

    if engine is not None:
        injected = await engine.surface("builtin", tool_name, tool_input, response_text)
        return _build_output(response_text, injected, engine.injection_mode)

    # Lazy imports: keep ``mms hook --help`` and unrelated CLI paths off the
    # surfacing/MCP import cost.
    from memtomem_stm.config import STMConfig
    from memtomem_stm.surfacing.engine import SurfacingEngine
    from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter
    from memtomem_stm.surfacing.observability import SurfacingObservability

    surfacing_cfg = STMConfig().surfacing
    if not surfacing_cfg.enabled:
        return {}

    # No FeedbackTracker on this path: it would persist extracted query text
    # (potentially secret-bearing Bash commands) to disk and contend on the
    # server's SQLite store. The feedback loop + dedup return with the daemon
    # stage (and an explicit privacy gate). ``feedback_tracker=None`` also means
    # the injected block carries no ``surfacing_id`` / rating prompt.
    adapter = McpClientSearchAdapter(surfacing_cfg)
    built = SurfacingEngine(
        surfacing_cfg,
        mcp_adapter=adapter,
        feedback_tracker=None,
        observability=SurfacingObservability(),
    )
    try:
        injected = await built.surface("builtin", tool_name, tool_input, response_text)
    finally:
        await _quiet_async(built.stop(), "surfacing engine stop")
        await _quiet_async(adapter.stop(), "LTM adapter stop")

    return _build_output(response_text, injected, surfacing_cfg.injection_mode)


def _build_output(response_text: str, injected: str, injection_mode: str) -> dict[str, Any]:
    block = _extract_surfaced_block(response_text, injected, injection_mode)
    if not block:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": block,
        }
    }


def _extract_surfaced_context(surf_out: dict[str, Any]) -> str | None:
    """Pull the surfaced ``additionalContext`` string out of a surfacing
    hook-output dict (as produced by :func:`_build_output`), or ``None`` when
    surfacing produced nothing."""
    hso = surf_out.get("hookSpecificOutput")
    if isinstance(hso, dict):
        ctx = hso.get("additionalContext")
        if isinstance(ctx, str) and ctx:
            return ctx
    return None


def _build_hook_output(
    updated_tool_output: Any | None, additional_context: str | None
) -> dict[str, Any]:
    """Merge optional compression + surfacing results into a single PostToolUse
    ``hookSpecificOutput`` so neither stage overwrites the other. Returns ``{}``
    when both are absent (the tool output passes through untouched)."""
    inner: dict[str, Any] = {"hookEventName": "PostToolUse"}
    if updated_tool_output is not None:
        inner["updatedToolOutput"] = updated_tool_output
    if additional_context:
        inner["additionalContext"] = additional_context
    if len(inner) == 1:  # only hookEventName — nothing to say
        return {}
    return {"hookSpecificOutput": inner}


async def _quiet_async(coro: Any, what: str) -> None:
    """Await a cleanup coroutine, swallowing (and debug-logging) any error."""
    try:
        await coro
    except Exception:
        logger.debug("%s failed", what, exc_info=True)


def _read_payload(stream: TextIO) -> dict[str, Any] | None:
    """Read and JSON-parse the hook payload from ``stream``; ``None`` if absent
    or malformed (caller treats that as a clean no-op)."""
    try:
        raw = stream.read()
    except Exception:
        return None
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _daemon_enabled() -> bool:
    """Whether the hook should route through the local daemon.

    Daemon mode is the default (``HookConfig.use_daemon=True``); unset → on. We
    read ``MEMTOMEM_STM_HOOK__USE_DAEMON`` directly here (mirroring the field's
    env binding) so the explicit opt-out path pays no extra config load —
    ``_surface_tools()`` reads its env knob the same way. The falsy-token set
    matches Pydantic's bool parsing so this and ``mms daemon status`` (which
    reads the parsed field) agree on every value Pydantic accepts. We strip and
    lower-case first, so a padded token like ``" off "`` still disables (Pydantic
    doesn't strip and would reject it); any other value Pydantic rejects (empty,
    garbage) returns enabled here, while a config load like ``mms daemon status``
    would raise.
    """
    val = os.environ.get("MEMTOMEM_STM_HOOK__USE_DAEMON")
    if val is None:
        return True
    return val.strip().lower() not in ("0", "false", "f", "no", "n", "off")


def _hook_eligible(payload: dict[str, Any]) -> bool:
    """Cheap pre-gate mirroring the head of :func:`_run_surfacing_hook_inner` —
    a PostToolUse call for a surface-allowlisted tool. ``_run_hook`` uses it to
    reject ineligible payloads (non-PostToolUse, or a missing/non-allowlisted
    tool) before routing to — or auto-spawning — the daemon, so an off-target
    hook call never warms a pointless daemon. The inner re-checks, so this is an
    optimization, never the authority."""
    if (payload.get("hook_event_name") or "PostToolUse") != "PostToolUse":
        return False
    tool_name = payload.get("tool_name")
    return isinstance(tool_name, str) and tool_name in _surface_tools()


async def _run_hook(
    payload: dict[str, Any], *, config: "STMConfig | None" = None
) -> dict[str, Any]:
    """Resolve the *surfacing* hook output, preferring the warm daemon when enabled.

    Degradation ladder (every rung still yields a hook-output dict, never
    raises): daemon disabled → cold in-process path (unchanged). Daemon enabled
    → one bounded round trip; on unavailable/stale/slow we (optionally)
    fire-and-forget spawn the daemon so the *next* call is warm, then for *this*
    call either return ``{}`` (default ``fallback=skip`` — the daemon exists
    precisely to avoid the ~6s cold start) or take the cold path
    (``fallback=cold``). The whole call runs inside ``_hook_budget_seconds()``
    in :func:`hook_command`. ``config`` is reused from :func:`_orchestrate` when
    given (saving a redundant ``STMConfig()`` load); ``None`` loads on demand.
    """
    if _daemon_enabled():
        from memtomem_stm.config import STMConfig
        from memtomem_stm.daemon import client

        if config is None:
            config = STMConfig()
        # Reject ineligible payloads (surfacing globally off, non-PostToolUse, or
        # a non-allowlisted tool) before routing to / spawning the daemon — an
        # off-target hook call must not warm a pointless daemon. The cold path
        # (run_surfacing_hook) re-checks, so this gate is an optimization.
        if not config.surfacing.enabled or not _hook_eligible(payload):
            return {}
        try:
            out = await client.surface(config, payload, timeout=config.hook.daemon_timeout_seconds)
        except Exception:
            logger.debug("daemon surface request failed", exc_info=True)
            out = None
        if out is not None:
            return out
        # Daemon unreachable. Kick off a lock-guarded background spawn so the
        # next call is warm (this call still degrades below). request_spawn is a
        # quick flock probe + detached Popen — fire-and-forget, never blocking
        # the hook budget, never raising into the hot path.
        if config.hook.auto_spawn:
            try:
                from memtomem_stm.daemon.spawn import request_spawn

                request_spawn(config)
            except Exception:
                logger.debug("daemon auto-spawn failed", exc_info=True)
        if config.hook.fallback != "cold":
            return {}
        # fallback=cold → fall through to the in-process surfacing path.
    return await run_surfacing_hook(payload)


async def _orchestrate(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve the full hook output: in-process Bash *compression* merged with
    LTM *surfacing*, as one ``hookSpecificOutput``.

    Ordering matters. Compression runs **first and in-process** so a multi-MB
    Bash result is shrunk before it could be sent whole to the daemon's 4 MiB
    frame, and it is **independent of the surfacing gate** — it still runs when
    surfacing is disabled or Bash is not in the *surface* allowlist. Surfacing
    then runs on a frame-bounded copy of the payload. Never raises (the CLI
    wrapper also backstops); each half degrades to "absent" on any failure.
    """
    from memtomem_stm.config import STMConfig

    config = STMConfig()
    updated = maybe_compress_builtin(payload, config.hook.compression)
    surf_out = await _run_hook(_bounded_surfacing_payload(payload), config=config)
    return _build_hook_output(updated, _extract_surfaced_context(surf_out))


@click.command(name="hook")
def hook_command() -> None:
    """Compress and/or surface for a host's built-in tool call (PostToolUse hook).

    Reads the hook JSON payload on stdin and prints a hook response that may
    carry ``updatedToolOutput`` (compressed Bash output — opt-in via
    ``MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1``) and/or ``additionalContext``
    (relevant LTM memories, for read-like tools). Surfacing runs in the warm
    ``mms daemon`` by default (no per-call cold start), auto-spawned on first
    use; set ``MEMTOMEM_STM_HOOK__USE_DAEMON=0`` for the legacy cold in-process
    path. Always exits 0; on any problem the tool output passes through unchanged
    (prints ``{}``).
    """
    payload = _read_payload(sys.stdin)
    output: dict[str, Any] = {}
    if payload is not None:
        try:
            output = asyncio.run(
                asyncio.wait_for(_orchestrate(payload), timeout=_hook_budget_seconds())
            )
        except Exception:
            logger.warning("hook processing failed — passing tool output through", exc_info=True)
            output = {}
    click.echo(json.dumps(output, ensure_ascii=False))
