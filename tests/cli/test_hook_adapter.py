"""Tests for ``cli/hook_adapter.py`` — the host-shaped parse/render seam.

B1 ships only :class:`ClaudeHookAdapter`. These pin that ``parse`` normalizes a
Claude PostToolUse payload into a :class:`CanonicalHookCall` (and no-ops on a
bad payload), and that ``render`` is byte-identical to the existing
``_build_hook_output`` envelope (it delegates to it). The capability flag
(``can_replace_output``) and the registry dispatch are pinned too. A
source-inspection test that the wiring routes through the adapter lives
alongside the wire-in commit.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from memtomem_stm.cli import hook_cmd
from memtomem_stm.cli.hook_adapter import (
    CanonicalHookCall,
    ClaudeHookAdapter,
    CodexHookAdapter,
    get_adapter,
)
from memtomem_stm.cli.hook_cmd import _build_hook_output, _tool_response_to_text

_CLAUDE = ClaudeHookAdapter()
_CODEX = CodexHookAdapter()

# Golden host-contract fixtures (B0): tests/fixtures/hooks/<host>/.
_HOOK_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hooks"


# ── parse ────────────────────────────────────────────────────────────────────


def test_adapter_parse_claude_valid_payload():
    response = {"content": "JWT handler. " * 20}
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/src/auth/jwt.py"},
        "tool_response": response,
    }
    call = _CLAUDE.parse(payload)
    assert isinstance(call, CanonicalHookCall)
    assert call.event_type == "PostToolUse"
    assert call.tool_name == "Read"
    # Read normalizes to the canonical 'read' (the allowlist/compression gates
    # key on this, not the host-native PascalCase name).
    assert call.canonical_tool == "read"
    assert call.tool_input == {"file_path": "/src/auth/jwt.py"}
    # The ORIGINAL response object is preserved (compression needs the real dict).
    assert call.tool_response is response
    # tool_response_text is the flattened text, identical to the shared helper.
    assert call.tool_response_text == _tool_response_to_text(response)
    assert call.host_tag == "claude"


@pytest.mark.parametrize(
    ("native", "canonical"),
    [
        ("Read", "read"),
        ("Grep", "grep"),
        ("Glob", "glob"),
        ("Bash", "shell"),
        ("WebFetch", "web_fetch"),
        ("Write", "write"),
        ("Edit", "edit"),
        ("MultiEdit", "edit"),
        ("Task", ""),  # outside the canonical vocabulary
        ("TodoWrite", ""),
    ],
)
def test_adapter_parse_canonicalizes_tool_name(native: str, canonical: str):
    call = _CLAUDE.parse({"tool_name": native, "tool_response": "x"})
    assert call is not None
    assert call.tool_name == native  # host-native preserved (engine query input)
    assert call.canonical_tool == canonical


@pytest.mark.parametrize("mcp_tool", ["mcp__memtomem__mem_search", "mcp__github__create_issue"])
def test_adapter_parse_rejects_mcp_tools(mcp_tool: str):
    # mcp__-prefixed tools already flow through the MCP proxy pipeline — the
    # native-tool hook must not double-handle them, so parse no-ops to None.
    assert _CLAUDE.parse({"tool_name": mcp_tool, "tool_response": "x"}) is None


def test_adapter_parse_unmapped_tool_canonicalizes_to_empty():
    call = _CLAUDE.parse({"tool_name": "SomeFutureTool", "tool_response": "x"})
    assert call is not None
    assert call.canonical_tool == ""


def test_adapter_parse_defaults_event_to_posttooluse():
    call = _CLAUDE.parse({"tool_name": "Bash", "tool_response": {"stdout": "ok"}})
    assert call is not None
    assert call.event_type == "PostToolUse"


@pytest.mark.parametrize("bad", [None, "not a dict", [1, 2, 3], 42])
def test_adapter_parse_non_dict_is_none(bad):
    # Mirrors _read_payload's None no-op; never raises.
    assert _CLAUDE.parse(bad) is None


def test_adapter_parse_coerces_missing_and_wrong_typed_fields():
    # Permissive: missing tool_name -> "", non-dict tool_input -> {}, missing
    # tool_response -> None/"" — eligibility gating stays in the core, not parse.
    call = _CLAUDE.parse({"hook_event_name": "PostToolUse", "tool_input": "oops"})
    assert call is not None
    assert call.tool_name == ""
    assert call.tool_input == {}
    assert call.tool_response is None
    assert call.tool_response_text == ""


def test_canonical_hook_call_is_frozen():
    call = _CLAUDE.parse({"tool_name": "Read", "tool_response": "x"})
    assert call is not None
    with pytest.raises((AttributeError, TypeError)):
        call.tool_name = "Write"  # type: ignore[misc]


# ── wire serialization (hook → daemon) ─────────────────────────────────────────


def test_to_wire_drops_tool_response_keeps_text():
    response = {"stdout": "x" * 500, "stderr": "warn", "exitCode": 0}
    call = _CLAUDE.parse(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": response}
    )
    assert call is not None
    wire = call.to_wire()
    # The original tool_response object is NOT transmitted (compression already
    # ran in the hook process; it may not even be JSON-serializable).
    assert "tool_response" not in wire
    assert wire["tool_name"] == "Bash"
    assert wire["canonical_tool"] == "shell"
    assert wire["tool_input"] == {"command": "ls"}
    assert wire["tool_response_text"] == call.tool_response_text
    assert wire["event_type"] == "PostToolUse"
    assert wire["host_tag"] == "claude"


def test_from_wire_round_trips_surfacing_fields():
    call = _CLAUDE.parse(
        {"tool_name": "Grep", "tool_input": {"pattern": "jwt"}, "tool_response": {"stdout": "hit"}}
    )
    assert call is not None
    rebuilt = CanonicalHookCall.from_wire(call.to_wire())
    assert rebuilt is not None
    # Everything the daemon-side core consumes survives; tool_response is None
    # (never transmitted) but the daemon path reads tool_response_text.
    assert rebuilt.event_type == call.event_type
    assert rebuilt.tool_name == call.tool_name
    assert rebuilt.canonical_tool == call.canonical_tool
    assert rebuilt.tool_input == call.tool_input
    assert rebuilt.tool_response_text == call.tool_response_text
    assert rebuilt.host_tag == call.host_tag
    assert rebuilt.tool_response is None


@pytest.mark.parametrize("bad", [None, "not a dict", [1, 2], 42])
def test_from_wire_non_dict_is_none(bad):
    assert CanonicalHookCall.from_wire(bad) is None


def test_from_wire_coerces_missing_and_wrong_typed_fields():
    rebuilt = CanonicalHookCall.from_wire({"tool_input": "oops"})
    assert rebuilt is not None
    assert rebuilt.event_type == "PostToolUse"  # missing → default
    assert rebuilt.tool_name == ""
    assert rebuilt.canonical_tool == ""
    assert rebuilt.tool_input == {}  # non-dict → {}
    assert rebuilt.tool_response_text == ""
    assert rebuilt.host_tag == "claude"


# ── render (delegates to _build_hook_output — must stay byte-identical) ────────

_BLOCK = "<surfaced-memories>\nMEM\n</surfaced-memories>"


def test_adapter_render_compression_only():
    updated = {"stdout": "compressed"}
    out = _CLAUDE.render(updated_tool_output=updated, additional_context=None)
    assert out == _build_hook_output(updated, None)
    assert out["hookSpecificOutput"].keys() == {"hookEventName", "updatedToolOutput"}


def test_adapter_render_surfacing_only():
    out = _CLAUDE.render(updated_tool_output=None, additional_context=_BLOCK)
    assert out == _build_hook_output(None, _BLOCK)
    assert out["hookSpecificOutput"].keys() == {"hookEventName", "additionalContext"}


def test_adapter_render_both_halves():
    updated = {"stdout": "c"}
    out = _CLAUDE.render(updated_tool_output=updated, additional_context=_BLOCK)
    assert out == _build_hook_output(updated, _BLOCK)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert hso["updatedToolOutput"] == updated
    assert hso["additionalContext"] == _BLOCK


def test_adapter_render_neither_is_empty():
    assert _CLAUDE.render(updated_tool_output=None, additional_context=None) == {}


# ── capability + registry dispatch ────────────────────────────────────────────


def test_claude_capability_and_tag():
    # Claude is the one host that can replace native tool output (B0). Surfacing
    # is universal; compression is gated on this flag in the orchestrator.
    assert _CLAUDE.host_tag == "claude"
    assert _CLAUDE.can_replace_output is True


def test_get_adapter_returns_registered_and_falls_back():
    assert get_adapter().host_tag == "claude"  # default
    assert get_adapter("claude").host_tag == "claude"
    assert get_adapter("codex").host_tag == "codex"  # B2 step3
    # An unregistered host tag falls back to Claude.
    assert get_adapter("nonexistent-host").host_tag == "claude"


# ── source pins (shape-identical routing a behavior spy can't see) ────────────


def test_render_delegates_to_build_hook_output():
    # The Claude output envelope (hookSpecificOutput / hookEventName /
    # updatedToolOutput / additionalContext) must live in exactly ONE place
    # (_build_hook_output), so render delegating to it — not inlining the keys —
    # keeps a future B2 host from silently inheriting Claude's shape.
    src = inspect.getsource(ClaudeHookAdapter.render)
    assert "_build_hook_output(" in src
    assert "hookSpecificOutput" not in src  # envelope built only in the helper


def test_parse_delegates_flattening_to_shared_helper():
    src = inspect.getsource(ClaudeHookAdapter.parse)
    assert "_tool_response_to_text(" in src


def test_orchestrate_routes_through_adapter():
    # Pin the shape-identical routing: _orchestrate must dispatch via
    # get_adapter() and use the adapter's parse/render, and gate compression on
    # the capability flag rather than running it unconditionally.
    src = inspect.getsource(hook_cmd._orchestrate)
    assert "get_adapter(" in src
    assert ".parse(" in src
    assert ".render(" in src
    assert "can_replace_output" in src


# ── Codex adapter (B2 step3 — surfacing-only) ──────────────────────────────────

_CODEX_DIR = _HOOK_FIXTURES / "codex"


def test_codex_parse_golden_inbound():
    # Golden: the host's documented inbound PostToolUse payload → CanonicalHookCall.
    payload = json.loads((_CODEX_DIR / "inbound_bash_posttooluse.json").read_text())
    call = _CODEX.parse(payload)
    assert isinstance(call, CanonicalHookCall)
    assert call.event_type == "PostToolUse"
    assert call.tool_name == "Bash"  # host-native preserved (engine query input)
    assert call.canonical_tool == "shell"  # Bash → canonical shell (allowlisted)
    assert call.tool_input == {"command": "pytest -q"}
    # Codex puts the tool output under ``tool_response`` (same key as Claude).
    assert call.tool_response == "12 passed in 3.4s"
    assert call.tool_response_text == "12 passed in 3.4s"
    assert call.host_tag == "codex"


def test_codex_render_golden_surfacing():
    # Golden: feed the canonical surfaced block from the fixture and assert render
    # reproduces the committed Codex surfacing emission exactly — the same
    # ``hookSpecificOutput.additionalContext`` envelope as Claude.
    expected = json.loads((_CODEX_DIR / "expected_surfacing_output.json").read_text())
    block = expected["hookSpecificOutput"]["additionalContext"]
    out = _CODEX.render(updated_tool_output=None, additional_context=block)
    assert out == expected


@pytest.mark.parametrize(
    ("native", "canonical"),
    [
        ("Bash", "shell"),
        ("apply_patch", "edit"),
        ("Read", ""),  # Codex shells out — no native Read/Grep/Glob/WebFetch
        ("WebSearch", ""),  # not intercepted by Codex's PostToolUse hook
        ("SomeFutureTool", ""),
    ],
)
def test_codex_parse_canonicalizes_tool_name(native: str, canonical: str):
    call = _CODEX.parse({"tool_name": native, "tool_response": "x"})
    assert call is not None
    assert call.tool_name == native  # host-native preserved
    assert call.canonical_tool == canonical


@pytest.mark.parametrize("mcp_tool", ["mcp__memtomem__mem_search", "mcp__github__create_issue"])
def test_codex_parse_rejects_mcp_tools(mcp_tool: str):
    # mcp__-prefixed tools already flow through the MCP proxy — the native-tool
    # hook must not double-handle them.
    assert _CODEX.parse({"tool_name": mcp_tool, "tool_response": "x"}) is None


@pytest.mark.parametrize("bad", [None, "not a dict", [1, 2, 3], 42])
def test_codex_parse_non_dict_is_none(bad):
    assert _CODEX.parse(bad) is None


def test_codex_parse_defaults_event_to_posttooluse():
    call = _CODEX.parse({"tool_name": "Bash", "tool_response": {"stdout": "ok"}})
    assert call is not None
    assert call.event_type == "PostToolUse"


def test_codex_parse_flattens_dict_response_via_shared_helper():
    # The common Bash-result shape is a dict, not a string. The golden fixture
    # uses a string ``tool_response`` (for which the flatten is identity), so pin
    # the dict path explicitly: parse must flatten through the shared helper —
    # identical to Claude — so surfacing's ``min_response_chars`` gate sees real
    # text and the ORIGINAL object is preserved (mirrors the Claude coverage).
    response = {"stdout": "ok", "stderr": "", "exitCode": 0}
    call = _CODEX.parse({"tool_name": "Bash", "tool_response": response})
    assert call is not None
    assert call.tool_response_text == _tool_response_to_text(response)
    assert call.tool_response_text == "ok"
    assert call.tool_response is response


@pytest.mark.parametrize("bad_input", [["x"], "weird", 42, 0, False])
def test_codex_parse_coerces_non_dict_tool_input(bad_input):
    # A non-dict ``tool_input`` must coerce to ``{}`` (not leak through): it flows
    # to the surfacing query extractor, which calls ``.items()`` on it, so a
    # leaked non-dict would raise at runtime. Permissive parse, gating in the core.
    call = _CODEX.parse({"tool_name": "Bash", "tool_input": bad_input, "tool_response": "x"})
    assert call is not None
    assert call.tool_input == {}


def test_codex_to_wire_carries_host_tag():
    # Provenance survives the hook→daemon wire so the daemon attributes the call
    # to Codex without any host knowledge.
    call = _CODEX.parse(
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": "ok"}
    )
    assert call is not None
    assert call.to_wire()["host_tag"] == "codex"


def test_codex_capability_and_tag():
    # B0: compression (output replacement) does NOT port to Codex
    # (``updatedMCPToolOutput`` is parsed-but-unsupported + MCP-only), so Codex is
    # surfacing-only — the orchestrator skips compression on this flag.
    assert _CODEX.host_tag == "codex"
    assert _CODEX.can_replace_output is False


def test_codex_render_delegates_to_build_hook_output():
    # The Codex surfacing envelope is identical to Claude's — it must delegate to
    # the single shared helper, not re-spell ``hookSpecificOutput`` (which would
    # let the two shapes drift apart silently).
    src = inspect.getsource(CodexHookAdapter.render)
    assert "_build_hook_output(" in src
    assert "hookSpecificOutput" not in src


def test_codex_parse_delegates_flattening_to_shared_helper():
    src = inspect.getsource(CodexHookAdapter.parse)
    assert "_tool_response_to_text(" in src
