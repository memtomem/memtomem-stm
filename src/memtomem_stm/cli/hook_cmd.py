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

Tool scope: we only surface for **read-like** built-in tools, gated on STM's
host-agnostic *canonical* tool name (:data:`_DEFAULT_SURFACE_TOOLS` — ``read /
grep / glob / shell``, overridable via ``MEMTOMEM_STM_HOOK_SURFACE_TOOLS``).
Each host adapter maps its native names (Claude ``Read``/``Bash``) to the
canonical vocabulary, so this one allowlist works across hosts. It is enforced
here rather than relying on the host's hook ``matcher`` (a too-broad matcher
would otherwise feed Write/Edit through) — surfacing on a write is semantically
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
import shlex
import sys
from contextlib import contextmanager
from dataclasses import replace
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, TextIO

import click

from memtomem_stm.cli.hook_adapter import (
    CANONICAL_TOOLS,
    READLIKE_SURFACE_TOOLS,
    canonicalize_tool_token,
    detect_host,
    get_adapter,
    known_hosts,
)
from memtomem_stm.cli.host_runtime import (
    HostRuntimePolicy,
    format_seconds,
    resolve_host_runtime_policy,
    runtime_env_overrides,
)

if TYPE_CHECKING:
    from memtomem_stm.cli.hook_adapter import CanonicalHookCall, HostHookAdapter
    from memtomem_stm.config import HookCompressionConfig, STMConfig

logger = logging.getLogger(__name__)

# Delimiters emitted by ``SurfacingFormatter`` around the injected block. We
# slice this back out so ``additionalContext`` carries only the memories, not
# a duplicate of the tool output the engine was handed as ``response_text``.
_SURFACED_OPEN = "<surfaced-memories>"
_SURFACED_CLOSE = "</surfaced-memories>"

# Read-like built-in tools we surface against, named in STM's canonical
# vocabulary (``HostHookAdapter.native_tool_map``), so one allowlist covers
# every host: Claude ``Read`` and a future host's ``ReadFile`` both canonicalize
# to ``read``. ``web_fetch`` / ``write`` / ``edit`` are deliberately excluded —
# surfacing on a write is semantically wrong and a write's inputs must never
# become a search query (see module docstring). Defined in ``hook_adapter`` (beside
# the canonical vocabulary) so ``hook_hosts``'s per-host install matcher and this
# allowlist share one source — see :data:`READLIKE_SURFACE_TOOLS`.
_DEFAULT_SURFACE_TOOLS = READLIKE_SURFACE_TOOLS

# Unique marker prepended to compressed ``stdout`` so a re-fired hook recognizes
# its own output and no-ops (idempotency). Detected as a *prefix* only (see
# :func:`_already_compressed`) — never via ``TruncateCompressor``'s generic
# markers (``(truncated`` / ``(original:`` / ``… omitted``), and never by a bare
# substring match: self-referential output (``git log`` / ``grep`` / ``cat`` over
# STM's own source, history, or docs that mention this sentinel) would otherwise
# falsely suppress compression of the whole result.
_COMPRESS_SENTINEL = "⟦stm-compressed⟧"

# Hard cap on the ``tool_response_text`` forwarded to surfacing, independent of
# the compression budget. Keeps a multi-MB built-in *result* from overflowing the
# daemon's ``MAX_MESSAGE_BYTES`` (4 MiB) wire frame even when compression is
# disabled or no-ops. Comfortably below 4 MiB after JSON escaping; surfacing only
# needs enough text to clear its ``min_response_chars`` gate (the query comes from
# ``tool_input``), so capping costs nothing for relevance.
#
# Scope: this caps the *response* channel only — the read-like default allowlist
# (read/grep/glob/shell) has a tiny ``tool_input`` (a path/pattern/command), so
# the frame stays bounded. If an operator adds ``write``/``edit`` to
# ``MEMTOMEM_STM_HOOK_SURFACE_TOOLS``, a multi-MB ``tool_input`` (file contents /
# ``new_string``) is sent uncapped and may exceed the frame — that degrades
# safely (the daemon's ``readline`` ``limit`` drops the oversized frame, the hook
# gets ``None`` and returns ``{}``, tool output passes through), it does not
# disrupt the host.
_SAFE_DAEMON_BUDGET = 256 * 1024

# Connect + busy timeout for the best-effort native-tool metrics write. The hook
# runs synchronously inside the host's PostToolUse, so a locked shared
# ``proxy_metrics.db`` (e.g. the live ``mms`` server writing) must fast-fail and
# degrade to "no row" rather than stall the tool call up to the store's shared
# 3000 ms busy timeout. Generous vs. a sub-ms WAL write, tiny vs. host latency.
_METRICS_BUSY_TIMEOUT_MS = 250

_UNSAFE_SURFACE_TOOLS = frozenset({"write", "edit"})


def _surface_tools() -> frozenset[str]:
    """Allowlist of *canonical* tool names to surface for, overridable via env.

    ``MEMTOMEM_STM_HOOK_SURFACE_TOOLS`` is a comma-separated list. Tokens are
    resolved through :func:`~memtomem_stm.cli.hook_adapter.canonicalize_tool_token`,
    which accepts a canonical name (``read,grep,glob,shell,web_fetch,write,edit``)
    or — for back-compat with the pre-canonical spelling — a host's native name
    (Claude ``Read`` / ``Bash`` → ``read`` / ``shell``). An unrecognized token is
    logged and dropped rather than silently producing an allowlist that matches
    nothing. Unset / all-blank falls back to :data:`_DEFAULT_SURFACE_TOOLS`; an
    explicit list whose every token is unrecognized resolves to an empty
    allowlist (honors the operator's restriction — we warned), never the default.
    """
    raw = os.environ.get("MEMTOMEM_STM_HOOK_SURFACE_TOOLS")
    if not raw:
        return _DEFAULT_SURFACE_TOOLS
    tools: set[str] = set()
    saw_token = False
    for token in (t.strip() for t in raw.split(",")):
        if not token:
            continue
        saw_token = True
        canonical = canonicalize_tool_token(token)
        if canonical is None:
            logger.warning(
                "MEMTOMEM_STM_HOOK_SURFACE_TOOLS: ignoring unrecognized tool %r "
                "(use canonical names: %s)",
                token,
                ", ".join(sorted(CANONICAL_TOOLS)),
            )
            continue
        if canonical in _UNSAFE_SURFACE_TOOLS:
            logger.warning(
                "MEMTOMEM_STM_HOOK_SURFACE_TOOLS: refusing mutating tool %r; "
                "write/edit inputs are never valid surfacing queries",
                token,
            )
            continue
        tools.add(canonical)
    if tools:
        return frozenset(tools)
    # All-blank env is unset-equivalent → default. A non-blank list that resolved
    # to nothing was an explicit (if mistaken) restriction → empty allowlist.
    return frozenset() if saw_token else _DEFAULT_SURFACE_TOOLS


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


def _bash_stdout(tool_response: Any) -> tuple[str, dict[str, Any]] | None:
    """Extract the compressible ``stdout`` channel of a Bash tool result.

    Returns ``(stdout_text, original_dict)`` only when ``tool_response`` is a
    dict carrying a string ``stdout`` — so the caller can clone the dict and
    replace just that field, preserving ``stderr`` / exit / ``interrupted`` /
    ``isImage`` verbatim. ``None`` for any other shape.

    We deliberately do **not** handle a plain-string ``tool_response``: for the
    built-in Bash tool ``updatedToolOutput`` must be a structured object
    mirroring the tool result, so a bare string would be ignored by the host
    (silently leaving the original output visible). An unknown shape is left
    untouched rather than replaced with something the host rejects. Unlike
    :func:`_tool_response_to_text` this never folds ``stderr`` into the
    compressible channel — replacement must not lose or merge the error stream.
    """
    if isinstance(tool_response, dict):
        stdout = tool_response.get("stdout")
        if isinstance(stdout, str):
            return stdout, tool_response
    return None


def _already_compressed(text: str) -> bool:
    """Whether ``text`` is *our own* prior compression output (idempotency).

    The sentinel is always written as a prefix (``prefix + compressed`` in
    :func:`maybe_compress_builtin`), so the check is anchored to the start of the
    text. A bare ``in`` substring match false-positives on any tool output that
    merely *mentions* the sentinel — e.g. ``git log`` / ``grep`` / ``cat`` over
    STM's own source, history, or docs — silently skipping compression of the
    whole result.
    """
    return text.startswith(_COMPRESS_SENTINEL)


@dataclass(frozen=True, slots=True)
class BuiltinCompressionOutcome:
    status: str
    replacement: dict[str, Any] | None = None
    original_chars: int = 0
    compressed_chars: int = 0


def compress_builtin(
    call: "CanonicalHookCall", cfg: "HookCompressionConfig"
) -> BuiltinCompressionOutcome:
    """Compress a shell tool's ``stdout`` for the PostToolUse ``updatedToolOutput``.

    Returns the value to place in ``updatedToolOutput`` — a dict mirroring the
    original shell ``tool_response`` with only ``stdout`` shrunk (every other
    field preserved verbatim) — or ``None`` to leave the tool output untouched.
    Only structured shell results are handled (see :func:`_bash_stdout`).
    **Never raises.**

    Gate: ``cfg.enabled`` + PostToolUse + ``canonical_tool == "shell"`` + ``stdout``
    longer than ``cfg.max_chars`` + not already compressed. The strategy is fixed
    to :class:`TruncateCompressor` (self-contained — no chunk-store callback,
    unlike SELECTIVE/HYBRID/PROGRESSIVE, whose retrieval tools live in the
    separate ``mms`` server process). ``cfg.max_chars`` budgets the *whole*
    replacement stdout: the sentinel prefix is reserved out of the compressor's
    budget so the result stays at/near the configured size. The caller gates
    this on the adapter's ``can_replace_output`` (compression ports only to
    Claude), so this is reached only for a host that honors ``updatedToolOutput``.
    """
    try:
        if not cfg.enabled:
            return BuiltinCompressionOutcome("disabled")
        if call.event_type != "PostToolUse":
            return BuiltinCompressionOutcome("ineligible_event")
        if call.canonical_tool != "shell":
            return BuiltinCompressionOutcome("ineligible_tool")
        extracted = _bash_stdout(call.tool_response)
        if extracted is None:
            return BuiltinCompressionOutcome("unsupported_shape")
        stdout, original = extracted
        explicit_error = original.get("isError") is True or original.get("is_error") is True
        interrupted = original.get("interrupted") is True
        image = original.get("isImage") is True or original.get("is_image") is True
        exit_code = original.get("exitCode", original.get("exit_code"))
        nonzero_exit = (
            isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0
        )
        if explicit_error or interrupted or image or nonzero_exit:
            return BuiltinCompressionOutcome("unsafe_result", original_chars=len(stdout))
        if len(stdout) <= cfg.max_chars or _already_compressed(stdout):
            status = "already_compressed" if _already_compressed(stdout) else "below_threshold"
            return BuiltinCompressionOutcome(status, original_chars=len(stdout))

        # Lazy import: keep ``mms hook --help`` and the no-op path off the
        # compression module's import cost.
        from memtomem_stm.proxy.compression import TruncateCompressor

        # Reserve the sentinel prefix out of the budget so sentinel + body stays
        # at/near ``max_chars`` (TruncateCompressor's own suffix may still add a
        # small, bounded overage — the budget is a target, not a hard cap).
        prefix = f"{_COMPRESS_SENTINEL}\n"
        if cfg.max_chars <= len(prefix):
            # A budget that cannot even hold the sentinel cannot produce a
            # meaningful replacement — prepending anyway would EXPAND the
            # configured cap many-fold (max_chars=1 → ~18 chars of sentinel).
            # Leave the output unchanged instead.
            return BuiltinCompressionOutcome("budget_too_small", original_chars=len(stdout))
        body_budget = cfg.max_chars - len(prefix)
        compressed = TruncateCompressor().compress(stdout, max_chars=body_budget)
        replacement_text = prefix + compressed
        if len(stdout) and len(replacement_text) / len(stdout) < cfg.min_retention:
            return BuiltinCompressionOutcome(
                "retention_guard",
                original_chars=len(stdout),
                compressed_chars=len(replacement_text),
            )
        clone = dict(original)
        clone["stdout"] = replacement_text
        return BuiltinCompressionOutcome(
            "compressed",
            replacement=clone,
            original_chars=len(stdout),
            compressed_chars=len(replacement_text),
        )
    except Exception:
        logger.debug(
            "builtin output compression failed — leaving tool output unchanged", exc_info=True
        )
        return BuiltinCompressionOutcome("error")


def maybe_compress_builtin(
    call: "CanonicalHookCall", cfg: "HookCompressionConfig"
) -> dict[str, Any] | None:
    """Backward-compatible replacement-only wrapper for direct callers."""
    return compress_builtin(call, cfg).replacement


def _bounded_call(call: "CanonicalHookCall") -> "CanonicalHookCall":
    """Return ``call`` with ``tool_response_text`` capped to :data:`_SAFE_DAEMON_BUDGET`.

    Independent of compression: a multi-MB built-in result must never overflow
    the daemon's 4 MiB wire frame, even when compression is disabled or no-ops.
    Pass-through (same object) when the text is already small, so the common case
    is untouched. The cap keeps the head of the flattened text, which is all
    surfacing needs to clear its size gate. Applied before both the daemon round
    trip and the cold in-process path so the two see the same bounded text.
    """
    text = call.tool_response_text
    if len(text) <= _SAFE_DAEMON_BUDGET:
        return call
    return replace(call, tool_response_text=text[:_SAFE_DAEMON_BUDGET])


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
    call: "CanonicalHookCall", *, engine: Any | None = None
) -> dict[str, Any]:
    """Core hook logic. Returns the hook-output dict (``{}`` means no-op).

    Consumes a normalized :class:`CanonicalHookCall` (host-agnostic): the
    allowlist gates on ``canonical_tool`` while the surfacing engine receives the
    host-native ``tool_name`` for query extraction. **Never raises** — every
    failure path (disabled surfacing, LTM error/timeout, internal bug) degrades
    to ``{}`` so any caller (the CLI, the daemon) can emit it and let the tool
    output pass through. ``engine`` is a test seam: when provided it is used
    as-is (caller owns its lifecycle); when ``None``, an engine + LTM adapter are
    built from :class:`STMConfig` and torn down before returning.
    """
    try:
        return await _run_surfacing_hook_inner(call, engine=engine)
    except Exception:
        logger.warning("hook surfacing failed — passing tool output through", exc_info=True)
        return {}


async def _run_surfacing_hook_inner(
    call: "CanonicalHookCall", *, engine: Any | None
) -> dict[str, Any]:
    if call.event_type != "PostToolUse":
        return {}
    if call.canonical_tool not in _surface_tools():
        return {}
    tool_name = call.tool_name
    tool_input = call.tool_input
    response_text = call.tool_response_text

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


def _hook_eligible(call: "CanonicalHookCall") -> bool:
    """Cheap pre-gate mirroring the head of :func:`_run_surfacing_hook_inner` —
    a PostToolUse call for a surface-allowlisted (canonical) tool. ``_run_hook``
    uses it to reject ineligible calls (non-PostToolUse, or a missing/
    non-allowlisted tool) before routing to — or auto-spawning — the daemon, so
    an off-target hook call never warms a pointless daemon. The inner re-checks,
    so this is an optimization, never the authority."""
    return call.event_type == "PostToolUse" and call.canonical_tool in _surface_tools()


async def _run_hook(
    call: "CanonicalHookCall", *, config: "STMConfig | None" = None
) -> dict[str, Any]:
    """Resolve the *surfacing* hook output, preferring the warm daemon when enabled.

    Takes the normalized :class:`CanonicalHookCall` and ships its host-agnostic
    wire form to the daemon (so the daemon needs no host knowledge). The text is
    capped once via :func:`_bounded_call` so both the daemon round trip and the
    cold path see the same bounded ``tool_response_text`` (and the wire frame
    stays under the daemon's 4 MiB cap).

    Degradation ladder (every rung still yields a hook-output dict, never
    raises): daemon disabled → cold in-process path. Daemon enabled → one
    bounded round trip; on unavailable/stale/slow we (optionally) fire-and-forget
    spawn the daemon so the *next* call is warm, then for *this* call either
    return ``{}`` (default ``fallback=skip`` — the daemon exists precisely to
    avoid the ~6s cold start) or take the cold path (``fallback=cold``). The
    whole call runs inside ``_hook_budget_seconds()`` in :func:`hook_command`.
    ``config`` is reused from :func:`_orchestrate` when given (saving a redundant
    ``STMConfig()`` load); ``None`` loads on demand.
    """
    bounded = _bounded_call(call)
    if _daemon_enabled():
        from memtomem_stm.config import STMConfig
        from memtomem_stm.daemon import client

        if config is None:
            config = STMConfig()
        # Reject ineligible calls (surfacing globally off, non-PostToolUse, or
        # a non-allowlisted tool) before routing to / spawning the daemon — an
        # off-target hook call must not warm a pointless daemon. The cold path
        # (run_surfacing_hook) re-checks, so this gate is an optimization.
        if not config.surfacing.enabled or not _hook_eligible(bounded):
            return {}
        try:
            out = await client.surface(config, bounded, timeout=config.hook.daemon_timeout_seconds)
        except Exception:
            logger.debug("daemon surface request failed", exc_info=True)
            out = None
        if out is not None:
            return out
        # Daemon unreachable. Request a lock-guarded spawn so the NEXT call is
        # warm (this call still degrades below). request_spawn is a quick flock
        # probe + detached Popen that returns right after fork — the daemon
        # itself comes up detached — and never raises into the hot path. The
        # probe and fork/exec are blocking syscalls (flock; close_fds scan), so
        # they run in a worker thread, awaited only for that brief offloaded
        # call, keeping the event loop free while the outer wait_for budget
        # clock runs.
        if config.hook.auto_spawn:
            try:
                from memtomem_stm.daemon.spawn import request_spawn

                await asyncio.to_thread(request_spawn, config)
            except Exception:
                logger.debug("daemon auto-spawn failed", exc_info=True)
        if config.hook.fallback != "cold":
            return {}
        # fallback=cold → fall through to the in-process surfacing path.
    return await run_surfacing_hook(bounded)


def _record_hook_metrics(
    call: "CanonicalHookCall",
    updated: dict[str, Any] | None,
    additional_context: str | None,
    config: "STMConfig",
) -> None:
    """Record one native built-in tool call into ``proxy_metrics.db`` (``source='hook'``).

    Makes native-tool spend — which never reaches the MCP proxy — visible next
    to proxied calls. Reads the normalized :class:`CanonicalHookCall` (``tool_name``
    + the once-flattened ``tool_response_text``), so the raw payload is parsed
    exactly once. Persists **sizes only** (original / compressed / surfaced chars
    + the tool name): never ``tool_input`` and never the output text, mirroring
    the hook's no-query-text privacy posture (a ``Bash`` command may carry
    secrets). Gated on ``config.hook.metrics_enabled`` (independent of
    ``proxy.metrics.enabled`` so a hook-only deployment still measures), reusing
    the proxy's ``metrics.db_path`` / ``max_history``. A short-lived store is
    opened per call — the hook is a one-shot subprocess, so there is no
    long-lived tracker to write through. **Never raises**: any failure degrades
    to no row, so metrics can't disrupt the host.
    """
    try:
        if not config.hook.metrics_enabled:
            return
        tool_name = call.tool_name
        if not tool_name:
            return
        original_chars = len(call.tool_response_text)
        if original_chars == 0:
            return  # nothing observed (empty / malformed result) — skip
        compressed_chars = (
            len(_tool_response_to_text(updated)) if updated is not None else original_chars
        )
        surfaced_chars = len(additional_context) if additional_context else 0

        # Lazy import: keep ``mms hook --help`` and the metrics-disabled path off
        # the metrics / sqlite import cost.
        from memtomem_stm.proxy.metrics import CallMetrics
        from memtomem_stm.proxy.metrics_store import MetricsStore

        store = MetricsStore(
            config.proxy.metrics.db_path.expanduser(),
            max_history=config.proxy.metrics.max_history,
            busy_timeout_ms=_METRICS_BUSY_TIMEOUT_MS,
        )
        store.initialize()
        try:
            store.record(
                CallMetrics(
                    server="builtin",
                    tool=tool_name,
                    original_chars=original_chars,
                    compressed_chars=compressed_chars,
                    surfaced_chars=surfaced_chars,
                    compression_strategy="truncate" if updated is not None else None,
                    source="hook",
                )
            )
        finally:
            store.close()
    except Exception:
        logger.debug("hook metrics recording failed — no row written", exc_info=True)


async def _orchestrate(
    payload: dict[str, Any],
    adapter: "HostHookAdapter",
    *,
    allow_output_replacement: bool | None = None,
) -> dict[str, Any]:
    """Resolve the full hook output: in-process Bash *compression* merged with
    LTM *surfacing*, as one host-shaped output.

    The host-shaped edges go through the caller-supplied :class:`HostHookAdapter`
    (resolved in :func:`hook_command`; Claude is the only one reachable today):
    :meth:`parse` normalizes the payload into a :class:`CanonicalHookCall` and
    :meth:`render` builds the host output. The normalized ``call`` drives the
    whole pipeline — the compression gate, the surfacing core, and metrics all
    consume it, so the raw payload is parsed exactly once. *Compression* is gated
    on the adapter's ``can_replace_output`` capability — only hosts that honor
    output replacement (Claude today; see B0) run it; for others it is skipped and
    only the surfacing half renders. It is otherwise an opt-in, shell-only
    ``updatedToolOutput`` stage that clones the response — it never alters what
    surfacing (or the daemon) receives, and it still runs when surfacing is
    disabled or shell is not in the *surface* allowlist.

    The two stages are independent. Surfacing receives the same ``call``;
    ``_run_hook`` caps its ``tool_response_text`` via :func:`_bounded_call`
    (:data:`_SAFE_DAEMON_BUDGET`) so the daemon wire frame stays bounded whether
    or not compression ran. Surfacing is wrapped so a failure degrades to "no
    memories" while still returning the compression half
    (``maybe_compress_builtin`` is itself non-raising). The CLI wrapper backstops
    the whole call. A non-raising metrics row (``source='hook'``) is recorded
    afterwards via :func:`_record_hook_metrics`, regardless of whether either
    stage produced output.

    The daemon receives the *canonical* call over the wire (``_run_hook`` →
    ``client.surface`` → ``CanonicalHookCall.to_wire``), so it needs no host
    knowledge; compression still runs here in the hook process (it needs the
    original ``tool_response`` object, which is not transmitted).
    """
    from memtomem_stm.config import STMConfig

    call = adapter.parse(payload)
    if call is None:  # unusable payload → pass the tool output through untouched
        return {}
    config = STMConfig()
    replacement_allowed = (
        adapter.can_replace_output
        if allow_output_replacement is None
        else adapter.can_replace_output and allow_output_replacement
    )
    compression = (
        compress_builtin(call, config.hook.compression)
        if replacement_allowed
        else BuiltinCompressionOutcome("unsupported_host")
    )
    logger.debug(
        "native compression status=%s original_chars=%d compressed_chars=%d",
        compression.status,
        compression.original_chars,
        compression.compressed_chars,
    )
    updated = compression.replacement
    surf_out: dict[str, Any] = {}
    if adapter.can_inject_context:
        try:
            surf_out = await _run_hook(call, config=config)
        except Exception:
            logger.debug("surfacing failed — keeping the compression half", exc_info=True)
    additional_context = _extract_surfaced_context(surf_out)
    _record_hook_metrics(call, updated, additional_context, config)
    return adapter.render(updated_tool_output=updated, additional_context=additional_context)


def _resolve_host_tag(host: str, payload: dict[str, Any] | None) -> str:
    """Resolve the runtime ``--host`` value to a concrete host tag — *fail-open*.

    ``auto`` (the default) infers the host from the payload shape via
    :func:`~memtomem_stm.cli.hook_adapter.detect_host`; a known host tag is
    authoritative. An **unknown** value never errors: the runtime bridge is fired
    *non-interactively by the host*, every supported host treats a non-zero hook
    exit as a block/deny/correction (see the module docstring's "Hard rule"), so a
    typo'd or stale ``--host`` must fall open rather than surface as a usage error
    to a person who isn't there. We log a warning and fall back to
    :func:`detect_host` (which itself defaults to Claude), so the worst case is the
    same safe pass-through any unusable payload already yields. This is the runtime
    counterpart of ``install`` / ``uninstall``'s ``click.Choice``, where an exit-2
    usage error on a typo *is* correct — those run at an operator's terminal (#526,
    reversing #524's deliberate runtime exit-2 contract)."""
    host = host.strip()
    if host == "auto":
        return detect_host(payload)
    if host in known_hosts():
        return host
    logger.warning(
        "mms hook: unrecognized --host %r — falling back to auto-detect "
        "(fail-open; known hosts: %s)",
        host,
        ", ".join(known_hosts()),
    )
    return detect_host(payload)


@contextmanager
def _runtime_registration_env(overrides: dict[str, str]) -> Iterator[None]:
    """Temporarily apply registration flags through the existing env bindings.

    ``STMConfig`` already has the authoritative parsing and the detached daemon
    deliberately inherits the hook process environment.  A short-lived overlay
    therefore keeps both consumers aligned without adding a second config path.
    Restoring every value matters for ``CliRunner`` and embedders that invoke
    multiple commands in one Python process.
    """
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@click.group(name="hook", invoke_without_command=True)
@click.option(
    "--host",
    "host",
    default="auto",
    show_default=True,
    # Optional value (is_flag=False + flag_value): a bare ``--host`` with the value
    # omitted resolves to ``auto`` (→ detect_host) instead of Click's
    # "requires an argument" exit 2. Together with the plain-string type (no
    # click.Choice), this closes *every* ``--host`` argv shape — missing value AND
    # unrecognized value — into the fail-open body rather than a non-zero parse
    # exit, honoring the runtime bridge's always-exit-0 contract for any
    # hand-edited registration (#526). install/uninstall keep the strict
    # click.Choice (operator commands; an exit-2 usage error on a typo is correct).
    is_flag=False,
    flag_value="auto",
    help=(
        "Host whose PostToolUse payload/output shape to use. 'auto' infers it from "
        "the payload shape (falls back to Claude). 'auto' cannot tell Codex from "
        "Claude — their payloads are identical — so pass --host codex explicitly "
        "for Codex. A plain string with an optional value: an unrecognized value is "
        "NOT a usage error here, and a bare --host (no value) resolves to 'auto' — "
        "the host fires this non-interactively and treats a non-zero exit as a "
        "block, so a bad/missing value logs a warning and falls back to auto-detect "
        "rather than exiting 2. (install/uninstall keep the strict click.Choice.)"
    ),
)
@click.option(
    "--use-daemon",
    "runtime_use_daemon",
    flag_value="true",
    default=None,
    help="Use the shared warm daemon (serialized by `mms hook install`).",
)
@click.option(
    "--no-daemon",
    "runtime_use_daemon",
    flag_value="false",
    help="Run without the shared daemon.",
)
@click.option(
    "--surfacing-timeout-seconds",
    default=None,
    metavar="SECONDS",
    help="Managed LTM search deadline. Invalid hand-edited values fail open.",
)
@click.option(
    "--daemon-timeout-seconds",
    default=None,
    metavar="SECONDS",
    help="Managed hook-to-daemon deadline. Kept above the surfacing deadline.",
)
@click.option(
    "--persist-query-text",
    "runtime_persist_query_text",
    flag_value="true",
    default=None,
    help="Allow surfacing query text persistence.",
)
@click.option(
    "--no-persist-query-text",
    "runtime_persist_query_text",
    flag_value="false",
    help="Hash surfacing query text before persistence.",
)
@click.pass_context
def hook_command(
    ctx: click.Context,
    host: str,
    runtime_use_daemon: str | None,
    surfacing_timeout_seconds: str | None,
    daemon_timeout_seconds: str | None,
    runtime_persist_query_text: str | None,
) -> None:
    """Bridge a host's built-in tool calls into STM (PostToolUse hook).

    Bare ``mms hook`` (no subcommand) is the *runtime* bridge: it reads the hook
    JSON payload on stdin and prints a hook response that may carry
    ``updatedToolOutput`` (compressed Bash output — opt-in via
    ``MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1``) and/or ``additionalContext``
    (relevant LTM memories, for read-like tools). Surfacing runs in the warm
    ``mms daemon`` by default (no per-call cold start), auto-spawned on first
    use; set ``MEMTOMEM_STM_HOOK__USE_DAEMON=0`` for the legacy cold in-process
    path. Always exits 0; on any problem the tool output passes through unchanged
    (prints ``{}``).

    ``--host`` selects the adapter, which owns both the output *shape*
    (``render``) and its *stdout serialization* (``serialize`` — JSON for
    Claude/Codex/Cursor, raw stdout for Kimi). It defaults to ``auto``, which
    infers the host from the payload shape (``detect_host``), falling back to
    Claude — backward-compatible with the original ``mms hook`` Claude
    registration. An explicit ``--host`` is authoritative; per-host registration
    writes it so raw-stdout (Kimi) and Codex routing are unambiguous (``auto``
    cannot tell Codex from Claude, nor identify Kimi from a malformed payload). An
    *unrecognized* ``--host`` is not a usage error on this path — a host fires it
    non-interactively and reads a non-zero exit as a block — so it warns and falls
    back to auto-detect (:func:`_resolve_host_tag`, #526); only ``install`` /
    ``uninstall`` reject a bad ``--host`` (operator commands, run at a terminal).

    The ``install`` / ``uninstall`` subcommands are the *registration UX*: they
    write (or remove) STM's PostToolUse hook block in a host's own config file
    (``mms hook install --host <name>``), so the host actually fires this hook.
    """
    # A subcommand (install/uninstall) was invoked — don't run the runtime bridge
    # (and never read stdin). The bare ``mms hook`` invocation, where
    # ``invoked_subcommand`` is None, falls through to the payload path below,
    # byte-identical to the pre-group command.
    if ctx.invoked_subcommand is not None:
        return
    overrides = runtime_env_overrides(
        use_daemon=runtime_use_daemon,
        surfacing_timeout_seconds=surfacing_timeout_seconds,
        daemon_timeout_seconds=daemon_timeout_seconds,
        persist_query_text=runtime_persist_query_text,
    )
    with _runtime_registration_env(overrides):
        payload = _read_payload(sys.stdin)
        host_tag = _resolve_host_tag(host, payload)
        adapter = get_adapter(host_tag)
        output: dict[str, Any] = {}
        if payload is not None:
            try:
                output = asyncio.run(
                    asyncio.wait_for(
                        _orchestrate(
                            payload,
                            adapter,
                            # Automatic/ambiguous routing must never enable the
                            # sole destructive capability (native output replace).
                            allow_output_replacement=host.strip() == "claude",
                        ),
                        timeout=_hook_budget_seconds(),
                    )
                )
            except Exception:
                logger.warning(
                    "hook processing failed — passing tool output through", exc_info=True
                )
                output = {}
        serialized = adapter.serialize(output)
    # ``serialize`` returns "" only when nothing is emitted (Kimi's raw-stdout
    # channel with nothing surfaced). An unconditional ``click.echo`` would still
    # append a newline, making stdout "\n" — non-empty, which Kimi injects into
    # the model context — so the "empty stdout" pass-through contract requires
    # suppressing the newline on an empty payload. JSON hosts always serialize to
    # a non-empty string ("{}" or more), so their trailing newline (and the
    # pre-seam byte-identity) is unchanged.
    click.echo(serialized, nl=bool(serialized))


def _hook_install_command_argv(host: str, *, policy: HostRuntimePolicy | None = None) -> list[str]:
    """The argv a host should fire on PostToolUse: ``<entrypoint> hook --host <host>``.

    Reuses the MCP-registration entry-point detection (``_detect_install_type``):
    a source/editable checkout registers ``uv run --directory <root>
    memtomem-stm hook --host <host>`` and a global install the bare
    ``memtomem-stm hook --host <host>`` console script. ``--host`` is always
    written explicitly so raw-stdout (Kimi) and Codex routing are unambiguous (a
    bare ``mms hook`` would auto-detect, which cannot tell Codex from Claude)."""
    # Lazy import: ``cli.proxy`` imports this module at top level, so importing it
    # here (only on the install path) avoids the cycle and keeps it off the
    # latency-sensitive bare-hook path.
    from memtomem_stm.cli.proxy import _detect_install_type

    server_cmd, server_args = _detect_install_type()
    argv = [server_cmd, *server_args, "hook", "--host", host]
    if policy is not None:
        argv.extend(policy.hook_args())
    return argv


def _emit_hook_change(
    change: Any, *, apply_: bool, backup: Any, apply_hint: str | None = None
) -> None:
    """Render a planned (or applied) install/uninstall to the terminal.

    Pure output: the caller has already applied the change when ``apply_`` and
    passes the resulting ``backup`` path (or ``None``). Dry-run (the default)
    shows what *would* happen and how to apply it."""
    from memtomem_stm.cli.proxy import _hdr, _ok, _warn

    click.echo(f"{_hdr(change.label + ' hook')} — {change.path}")

    if not change.changed:
        no_change = {
            "already": "already installed (no change).",
            "not_installed": "not installed (no memtomem-stm hook found).",
            "absent": "config not found (nothing to remove).",
        }.get(change.status, "no change.")
        click.echo(f"  {_ok(no_change) if change.status == 'already' else no_change}")
        return

    if change.action == "install":
        if apply_:
            if backup is not None:
                click.echo(f"  {_ok('Backed up')} {backup}")
            click.echo(f"  {_ok('Installed')} memtomem-stm's PostToolUse hook.")
        else:
            verb = "create the config and add" if change.status == "create" else "add"
            click.echo(f"  Would {verb} memtomem-stm's PostToolUse hook:")
            click.echo("")
            for line in change.rendered_block.splitlines():
                # Indent non-blank lines only — TOML array-of-tables separators are
                # blank, and prefixing them would emit trailing-whitespace lines.
                click.echo(f"    {line}" if line else "")
            click.echo("")
    else:  # uninstall (only reached when changed → status == "remove")
        if apply_:
            if backup is not None:
                click.echo(f"  {_ok('Backed up')} {backup}")
            click.echo(f"  {_ok('Removed')} memtomem-stm's PostToolUse hook.")
        else:
            click.echo("  Would remove memtomem-stm's PostToolUse hook.")

    if not apply_ and change.fmt == "toml":
        click.echo(
            f"  {_warn('Note:')} applying rewrites {change.path.name} — TOML "
            "comments/formatting are not preserved (a non-clobbering .bak backup "
            "is kept)."
        )

    for note in change.notes:
        click.echo(f"  {_warn('•')} {note}")

    if not apply_:
        click.echo("")
        click.echo(
            f"  To apply: "
            f"{_hdr(apply_hint or f'mms hook {change.action} --host {change.host_tag} --apply')}"
        )


@hook_command.command(name="install")
@click.option(
    "--host",
    "host",
    required=True,
    type=click.Choice(known_hosts()),
    help="Host whose hook config to register STM's PostToolUse hook in.",
)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    help="Write the change (default: dry-run preview). Backs up any prior file "
    "to <path>.bak (.bak.1, … if one already exists).",
)
@click.option(
    "--surfacing-timeout",
    type=click.FloatRange(min=0.0, min_open=True),
    default=None,
    metavar="SECONDS",
    help="Pin the LTM search deadline in the installed hook command.",
)
@click.option(
    "--daemon",
    "daemon_mode",
    flag_value=True,
    default=None,
    help="Install an explicit shared-daemon route.",
)
@click.option(
    "--no-daemon",
    "daemon_mode",
    flag_value=False,
    help="Install an explicit cold/in-process route instead of the shared daemon.",
)
@click.option(
    "--inherit-runtime-env",
    is_flag=True,
    help="Do not serialize daemon, timeout, or query-persistence settings.",
)
def hook_install_command(
    host: str,
    apply_: bool,
    surfacing_timeout: float | None,
    daemon_mode: bool | None,
    inherit_runtime_env: bool,
) -> None:
    """Register STM's PostToolUse hook in a host's config (idempotent).

    Read-modify-writes the host's hook-config file (``~/.claude/settings.json``,
    ``~/.cursor/hooks.json``, ``$KIMI_CODE_HOME/config.toml`` or
    ``~/.kimi-code/config.toml``, or ``~/.codex/config.toml``)
    so the host fires ``mms hook --host <name>`` after each built-in tool call.
    Re-running updates STM's existing block in place rather than duplicating it.
    Default is a dry-run preview; ``--apply`` writes (backing up any prior file)
    under the hook-host write lock, with plan and apply in one locked span so
    concurrent mms runs cannot interleave (the host app's own concurrent writes
    are caught by ``apply_change``'s staleness guard instead)."""
    # Lazy import: the lock machinery pulls in ``mms.state`` (pydantic), and
    # this module is imported on the per-tool-call hook hot path.
    from memtomem_stm.cli import hook_hosts
    from memtomem_stm.cli._write_lock import hook_hosts_write_lock

    if inherit_runtime_env and (surfacing_timeout is not None or daemon_mode is not None):
        raise click.UsageError(
            "--inherit-runtime-env cannot be combined with --surfacing-timeout, "
            "--daemon, or --no-daemon"
        )
    with hook_hosts_write_lock(enabled=apply_):
        try:
            commands = hook_hosts.installed_stm_hook_commands(host)
            policy = None
            if not inherit_runtime_env:
                policy = resolve_host_runtime_policy(
                    existing_command=commands[0] if commands else None,
                    use_daemon=daemon_mode,
                    surfacing_timeout_seconds=surfacing_timeout,
                )
            command = shlex.join(_hook_install_command_argv(host, policy=policy))
            change = hook_hosts.plan_install(host, command)
            backup = hook_hosts.apply_change(change) if apply_ else None
        except hook_hosts.HookInstallError as exc:
            raise click.ClickException(str(exc)) from exc
        hint = ["mms", "hook", "install", "--host", host]
        if surfacing_timeout is not None:
            hint.extend(["--surfacing-timeout", format_seconds(surfacing_timeout)])
        if daemon_mode is True:
            hint.append("--daemon")
        elif daemon_mode is False:
            hint.append("--no-daemon")
        if inherit_runtime_env:
            hint.append("--inherit-runtime-env")
        hint.append("--apply")
        _emit_hook_change(
            change,
            apply_=apply_,
            backup=backup,
            apply_hint=shlex.join(hint),
        )


@hook_command.command(name="uninstall")
@click.option(
    "--host",
    "host",
    required=True,
    type=click.Choice(known_hosts()),
    help="Host whose hook config to remove STM's PostToolUse hook from.",
)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    help="Write the change (default: dry-run preview). Backs up any prior file "
    "to <path>.bak (.bak.1, … if one already exists).",
)
def hook_uninstall_command(host: str, apply_: bool) -> None:
    """Remove STM's PostToolUse hook from a host's config (symmetric to install).

    Removes exactly the block ``install`` would add (recognized by command
    shape), leaving any hand-written hooks untouched. A no-op when the config is
    absent or holds no STM block. Default is a dry-run preview; ``--apply`` writes
    (backing up the prior file) under the same locked plan+apply span as
    ``install``."""
    # Lazy import: see hook_install_command.
    from memtomem_stm.cli import hook_hosts
    from memtomem_stm.cli._write_lock import hook_hosts_write_lock

    with hook_hosts_write_lock(enabled=apply_):
        try:
            change = hook_hosts.plan_uninstall(host)
            backup = hook_hosts.apply_change(change) if apply_ else None
        except hook_hosts.HookInstallError as exc:
            raise click.ClickException(str(exc)) from exc
        _emit_hook_change(change, apply_=apply_, backup=backup)
