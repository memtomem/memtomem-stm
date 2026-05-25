"""Tests for ``mms hook`` — the built-in-tool → STM surfacing bridge.

Coverage:

- Pure helpers ``_tool_response_to_text`` / ``_extract_surfaced_block`` — the
  novel logic that flattens a tool result and recovers the injected memory
  block from the engine's combined output.
- ``run_surfacing_hook`` via the ``engine=`` test seam (a real
  :class:`SurfacingEngine` over a mock LTM adapter — no LTM subprocess) so the
  end-to-end shape is locked without depending on a live memtomem server.
- CLI degradation through ``CliRunner``: malformed / non-PostToolUse / disabled
  inputs must all print ``{}`` and exit 0 so a hook can never disrupt the host.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.hook_cmd import (
    _extract_surfaced_block,
    _tool_response_to_text,
    run_surfacing_hook,
)
from memtomem_stm.cli.proxy import cli
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine


# ── Fakes (mirror tests/test_surfacing_engine.py shapes) ─────────────────────


@dataclass
class _FakeMeta:
    source_file: Path = Path("/notes/test.md")
    namespace: str = "default"


@dataclass
class _FakeChunk:
    id: str = ""
    content: str = "some memory content"
    metadata: _FakeMeta | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid4())
        if self.metadata is None:
            self.metadata = _FakeMeta()


@dataclass
class _FakeResult:
    chunk: _FakeChunk
    score: float


def _engine_with_results(results: list[_FakeResult] | None) -> SurfacingEngine:
    """A real SurfacingEngine over a mock adapter returning ``results``."""
    res = results or []
    adapter = AsyncMock()
    adapter.search = AsyncMock(return_value=(res, [], "ok" if res else "empty_results"))
    config = SurfacingConfig(
        enabled=True,
        min_response_chars=10,
        timeout_seconds=5.0,
        min_score=0.02,
        cooldown_seconds=0.0,
        max_surfacings_per_minute=1000,
        auto_tune_enabled=False,
        include_session_context=False,
        fire_webhook=False,
    )
    return SurfacingEngine(config, mcp_adapter=adapter)


_LONG = "JWT authentication handler. " * 50  # well above min_response_chars
_READ_PAYLOAD = {
    "hook_event_name": "PostToolUse",
    "tool_name": "Read",
    "tool_input": {"file_path": "/src/auth/jwt_handler.py"},
    "tool_response": {"content": _LONG},
}


# ── _tool_response_to_text ───────────────────────────────────────────────────


def test_tool_response_to_text_passthrough_and_shapes():
    assert _tool_response_to_text("raw output") == "raw output"
    assert _tool_response_to_text(None) == ""
    assert _tool_response_to_text({"stdout": "out", "stderr": "err"}) == "out\nerr"
    assert _tool_response_to_text(["a", "b"]) == "a\nb"


def test_tool_response_to_text_dict_without_text_fields_falls_back_to_json():
    out = _tool_response_to_text({"exitCode": 0, "interrupted": False})
    assert json.loads(out) == {"exitCode": 0, "interrupted": False}


# ── _extract_surfaced_block ──────────────────────────────────────────────────

_BLOCK = "<surfaced-memories>\nMEM\n</surfaced-memories>"


def test_extract_block_append_mode():
    injected = f"ORIG\n\n{_BLOCK}"
    assert _extract_surfaced_block("ORIG", injected, "append") == _BLOCK


def test_extract_block_prepend_mode():
    injected = f"{_BLOCK}\n\nORIG"
    assert _extract_surfaced_block("ORIG", injected, "prepend") == _BLOCK


def test_extract_block_no_change_returns_none():
    assert _extract_surfaced_block("ORIG", "ORIG", "append") is None


def test_extract_block_defensive_fallback_on_wrong_mode():
    # Mode says prepend but the block was actually appended → the slice won't
    # match the tag delimiters, so the direct tag scan must still recover it.
    injected = f"ORIG\n\n{_BLOCK}"
    assert _extract_surfaced_block("ORIG", injected, "prepend") == _BLOCK


# ── run_surfacing_hook (engine seam) ─────────────────────────────────────────


async def test_run_hook_surfaces_into_additional_context():
    engine = _engine_with_results([_FakeResult(_FakeChunk(content="Use RS256 for JWT"), 0.5)])
    out = await run_surfacing_hook(_READ_PAYLOAD, engine=engine)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    ctx = hso["additionalContext"]
    assert "Use RS256 for JWT" in ctx
    assert ctx.startswith("<surfaced-memories>")
    # additionalContext carries only the memories, not a copy of tool output.
    assert "JWT authentication handler." not in ctx
    # No feedback store on this path → no unresolvable feedback prompt.
    assert "stm_surfacing_feedback" not in ctx
    assert "surfacing_id" not in ctx


async def test_run_hook_empty_results_is_noop():
    engine = _engine_with_results([])
    assert await run_surfacing_hook(_READ_PAYLOAD, engine=engine) == {}


async def test_run_hook_ignores_non_posttooluse():
    engine = _engine_with_results([_FakeResult(_FakeChunk(), 0.9)])
    payload = {**_READ_PAYLOAD, "hook_event_name": "PreToolUse"}
    assert await run_surfacing_hook(payload, engine=engine) == {}


async def test_run_hook_requires_tool_name():
    engine = _engine_with_results([_FakeResult(_FakeChunk(), 0.9)])
    payload = {"hook_event_name": "PostToolUse", "tool_input": {"file_path": "x"}}
    assert await run_surfacing_hook(payload, engine=engine) == {}


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit", "Task"])
async def test_run_hook_rejects_non_readlike_tools(tool: str):
    # Write/Edit/etc. must never surface — even with a matching engine and a
    # broad host matcher — so their inputs don't become queries (the gate's
    # write-tool block is case-sensitive and misses PascalCase names).
    engine = _engine_with_results([_FakeResult(_FakeChunk(content="secret"), 0.9)])
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "tool_input": {"file_path": "/a/b.py", "old_string": "x", "new_string": "y"},
        "tool_response": {"content": _LONG},
    }
    assert await run_surfacing_hook(payload, engine=engine) == {}


async def test_run_hook_allowlist_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", "CustomRead")
    engine = _engine_with_results([_FakeResult(_FakeChunk(content="hit"), 0.9)])
    # A default-allowlisted tool is now rejected...
    assert await run_surfacing_hook(_READ_PAYLOAD, engine=engine) == {}
    # ...and the override tool is accepted.
    out = await run_surfacing_hook({**_READ_PAYLOAD, "tool_name": "CustomRead"}, engine=engine)
    assert "hit" in out["hookSpecificOutput"]["additionalContext"]


async def test_run_hook_never_raises_on_engine_error():
    # The "never raises" contract must hold for direct callers (a future
    # daemon), not only the CLI's outer guard.
    boom = AsyncMock()
    boom.surface = AsyncMock(side_effect=RuntimeError("LTM exploded"))
    boom.injection_mode = "append"
    assert await run_surfacing_hook(_READ_PAYLOAD, engine=boom) == {}


# ── CLI degradation (must always print {} and exit 0) ────────────────────────


@pytest.mark.parametrize(
    "stdin",
    ["", "   ", "not json", '{"hook_event_name": "PreToolUse"}', "[1,2,3]"],
)
def test_cli_degrades_to_empty_object(stdin: str):
    result = CliRunner().invoke(cli, ["hook"], input=stdin)
    assert result.exit_code == 0
    assert json.loads(result.output) == {}


def test_cli_surfacing_disabled_is_noop(monkeypatch: pytest.MonkeyPatch):
    # A valid PostToolUse payload, but surfacing off → returns {} without ever
    # constructing the LTM adapter (so no subprocess spawn in CI).
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "false")
    result = CliRunner().invoke(cli, ["hook"], input=json.dumps(_READ_PAYLOAD))
    assert result.exit_code == 0
    assert json.loads(result.output) == {}
