"""``mms hook`` — bridge a host's built-in tool calls into STM's surfacing.

Claude Code (and compatible hosts) invoke a ``PostToolUse`` hook as a plain
command: the hook payload arrives as JSON on **stdin** and the hook replies
with JSON on **stdout**. This subcommand reads that payload, runs STM's
proactive memory surfacing over the built-in tool's output, and — when
relevant LTM memories are found — returns them through ``additionalContext``
so the host appends them next to the tool result the model reads.

Why surfacing-only (no ``updatedToolOutput``): appending context is the
side-effect-free hook play. Compression-style *replacement* of a built-in
tool's output is a separate, riskier stage (it must match each tool's output
shape, and compressing ``Read`` breaks a later ``Edit``'s verbatim
``old_string`` match) and is intentionally out of scope here. See the Stage B
section of the project plan / ``project_stm_builtin_tool_hooks`` memory.

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

Performance caveat — near-no-op with stock defaults: a fresh ``mms hook``
process spawns a fresh LTM MCP child per call, and that cold start (embedding
+ reranker model load) measured ~6s here, which exceeds the default
``surfacing.timeout_seconds`` of 3.0 — so surfacing usually times out and
returns ``{}``. The default ``surfacing.min_response_chars`` of 5000 also
gates out small tool outputs. To make surfacing actually fire before the
daemon lands, raise ``MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS`` (e.g. 8) and
lower ``MEMTOMEM_STM_SURFACING__MIN_RESPONSE_CHARS``, accepting the per-call
latency. A persistent local daemon (warm LTM connection) is the real fix.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, TextIO

import click

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


@click.command(name="hook")
def hook_command() -> None:
    """Surface STM memories for a host's built-in tool call (PostToolUse hook).

    Reads the hook JSON payload on stdin and, for read-like built-in tools,
    prints a hook response whose ``additionalContext`` carries relevant LTM
    memories. Always exits 0; on any problem the tool output passes through
    unchanged (prints ``{}``).
    """
    payload = _read_payload(sys.stdin)
    output: dict[str, Any] = {}
    if payload is not None:
        try:
            output = asyncio.run(
                asyncio.wait_for(run_surfacing_hook(payload), timeout=_hook_budget_seconds())
            )
        except Exception:
            logger.warning("hook surfacing failed — passing tool output through", exc_info=True)
            output = {}
    click.echo(json.dumps(output, ensure_ascii=False))
