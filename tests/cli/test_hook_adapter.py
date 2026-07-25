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
    CursorHookAdapter,
    KimiHookAdapter,
    detect_host,
    get_adapter,
    known_hosts,
)
from memtomem_stm.cli.hook_cmd import _build_hook_output, _tool_response_to_text

_CLAUDE = ClaudeHookAdapter()
_CODEX = CodexHookAdapter()
_CURSOR = CursorHookAdapter()
_KIMI = KimiHookAdapter()

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
    assert get_adapter("cursor").host_tag == "cursor"  # B2 step3
    assert get_adapter("kimi").host_tag == "kimi"  # B2 step3
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
    # Pin the shape-identical routing: _orchestrate consumes the supplied adapter's
    # parse/render and gates compression on the capability flag rather than running
    # it unconditionally. (Adapter resolution + serialization live in hook_command.)
    src = inspect.getsource(hook_cmd._orchestrate)
    assert "adapter.parse(" in src
    assert "adapter.render(" in src
    assert "can_replace_output" in src
    # And it must NOT re-resolve its own adapter (that resolution is hook_command's
    # job — the host-selection plug-in point). Re-internalizing get_adapter() here
    # would silently ignore the supplied adapter; pin against it.
    assert "get_adapter(" not in src


def test_hook_command_resolves_adapter_and_serializes():
    # hook_command owns adapter selection (get_adapter() — the host-selection
    # plug-in point) and per-host stdout serialization (adapter.serialize), so the
    # raw-stdout (Kimi) vs JSON (Claude/Codex/Cursor) decision is the adapter's,
    # not a hardcoded json.dumps in the CLI. (hook_command is a click Command —
    # inspect its underlying callback.)
    src = inspect.getsource(hook_cmd.hook_command.callback)
    assert "get_adapter(" in src
    assert "adapter.serialize(" in src
    assert "json.dumps(" not in src  # serialization delegated to the adapter


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


# ── Cursor adapter (B2 step3 — surfacing-only; camelCase event + flat render) ──

_CURSOR_DIR = _HOOK_FIXTURES / "cursor"


def test_cursor_parse_golden_inbound():
    # Golden: documented inbound payload → CanonicalHookCall. Exercises the two
    # Cursor-specific edges: the camelCase event normalizes, and the output is
    # read from ``tool_output`` (a JSON-stringified string), not ``tool_response``.
    payload = json.loads((_CURSOR_DIR / "inbound_shell_posttooluse.json").read_text())
    call = _CURSOR.parse(payload)
    assert isinstance(call, CanonicalHookCall)
    assert call.event_type == "PostToolUse"  # normalized from "postToolUse"
    assert call.tool_name == "Shell"  # host-native preserved
    assert call.canonical_tool == "shell"  # Shell → canonical shell (allowlisted)
    assert call.tool_input == {"command": "npm test"}
    # tool_output is a JSON-stringified string; flattened verbatim for the gate.
    assert call.tool_response == '{"exitCode":0,"stdout":"All tests passed"}'
    assert call.tool_response_text == '{"exitCode":0,"stdout":"All tests passed"}'
    assert call.host_tag == "cursor"


def test_cursor_render_golden_surfacing():
    # Golden: feed the canonical surfaced block and assert render reproduces the
    # committed Cursor emission exactly — a FLAT top-level ``additional_context``
    # string (not the ``hookSpecificOutput`` envelope).
    expected = json.loads((_CURSOR_DIR / "expected_surfacing_output.json").read_text())
    block = expected["additional_context"]
    out = _CURSOR.render(updated_tool_output=None, additional_context=block)
    assert out == expected


@pytest.mark.parametrize(
    ("raw_event", "expected"),
    [
        ("postToolUse", "PostToolUse"),  # camelCase normalized to canonical
        (None, "PostToolUse"),  # missing → default (as for Claude/Codex)
        ("preToolUse", "preToolUse"),  # other event passes through (core rejects it)
    ],
)
def test_cursor_parse_normalizes_event(raw_event, expected: str):
    payload = {"tool_name": "Shell", "tool_output": "x"}
    if raw_event is not None:
        payload["hook_event_name"] = raw_event
    call = _CURSOR.parse(payload)
    assert call is not None
    assert call.event_type == expected


@pytest.mark.parametrize(
    ("native", "canonical"),
    [
        ("Shell", "shell"),
        ("Read", "read"),
        ("Write", "write"),
        ("Grep", ""),  # not in Cursor's verified native names
        ("SomeFutureTool", ""),
    ],
)
def test_cursor_parse_canonicalizes_tool_name(native: str, canonical: str):
    call = _CURSOR.parse({"tool_name": native, "tool_output": "x"})
    assert call is not None
    assert call.tool_name == native  # host-native preserved
    assert call.canonical_tool == canonical


@pytest.mark.parametrize("mcp_tool", ["mcp__memtomem__mem_search", "mcp__github__create_issue"])
def test_cursor_parse_rejects_mcp_tools(mcp_tool: str):
    assert _CURSOR.parse({"tool_name": mcp_tool, "tool_output": "x"}) is None


@pytest.mark.parametrize("bad", [None, "not a dict", [1, 2, 3], 42])
def test_cursor_parse_non_dict_is_none(bad):
    assert _CURSOR.parse(bad) is None


@pytest.mark.parametrize(
    "payload", [{}, {"tool_name": 123}, {"tool_name": None}, {"tool_name": []}]
)
def test_cursor_parse_coerces_missing_or_non_str_tool_name(payload):
    # Permissive + never-raises contract: a missing/non-str tool_name must coerce
    # to "" before the `tool_name.startswith("mcp__")` guard (which would raise on
    # a non-str). Pins the load-bearing coercion the sibling Claude suite already
    # pins — without this, a dropped guard passes every test yet crashes on real
    # host stdin.
    call = _CURSOR.parse({**payload, "tool_output": "x"})
    assert call is not None
    assert call.tool_name == ""
    assert call.canonical_tool == ""


def test_cursor_parse_flattens_dict_output_via_shared_helper():
    # tool_output is usually Cursor's JSON-stringified string, but stay permissive:
    # a dict shape must flatten through the shared helper (the size-gate input).
    response = {"stdout": "ok", "stderr": ""}
    call = _CURSOR.parse({"tool_name": "Shell", "tool_output": response})
    assert call is not None
    assert call.tool_response_text == _tool_response_to_text(response)
    assert call.tool_response_text == "ok"
    assert call.tool_response is response


@pytest.mark.parametrize("bad_input", [["x"], "weird", 42, 0, False])
def test_cursor_parse_coerces_non_dict_tool_input(bad_input):
    call = _CURSOR.parse({"tool_name": "Shell", "tool_input": bad_input, "tool_output": "x"})
    assert call is not None
    assert call.tool_input == {}


def test_cursor_to_wire_carries_host_tag():
    call = _CURSOR.parse(
        {"tool_name": "Shell", "tool_input": {"command": "ls"}, "tool_output": "ok"}
    )
    assert call is not None
    assert call.to_wire()["host_tag"] == "cursor"


def test_cursor_render_surfacing_only_flat_shape():
    block = "<surfaced-memories>\nMEM\n</surfaced-memories>"
    out = _CURSOR.render(updated_tool_output=None, additional_context=block)
    # Flat top-level, snake_case — NOT nested under hookSpecificOutput.
    assert out == {"additional_context": block}


def test_cursor_render_neither_is_empty():
    assert _CURSOR.render(updated_tool_output=None, additional_context=None) == {}


def test_cursor_render_ignores_updated_tool_output():
    # Cursor has no native output-replace field (can_replace_output=False); render
    # must NEVER emit one (a guessed field could disrupt the host), even if handed
    # an updated output — only the surfacing half renders.
    block = "<surfaced-memories>\nMEM\n</surfaced-memories>"
    out = _CURSOR.render(updated_tool_output={"stdout": "compressed"}, additional_context=block)
    assert out == {"additional_context": block}
    assert "updated_mcp_tool_output" not in out
    # And with no surfaced block, an updated output alone yields a pass-through {}.
    assert _CURSOR.render(updated_tool_output={"stdout": "x"}, additional_context=None) == {}


def test_cursor_capability_and_tag():
    # B0: Cursor's only output-replace field (updated_mcp_tool_output) is MCP-only,
    # so native compression does not port — surfacing-only.
    assert _CURSOR.host_tag == "cursor"
    assert _CURSOR.can_replace_output is False


def test_cursor_render_builds_flat_shape_not_envelope():
    # Cursor's render owns a DIFFERENT shape than Claude/Codex: it must NOT delegate
    # to _build_hook_output nor emit hookSpecificOutput, and must use the flat
    # snake_case additional_context key.
    src = inspect.getsource(CursorHookAdapter.render)
    assert "hookSpecificOutput" not in src
    assert "_build_hook_output" not in src
    assert "additional_context" in src


def test_cursor_parse_delegates_flattening_to_shared_helper():
    src = inspect.getsource(CursorHookAdapter.parse)
    assert "_tool_response_to_text(" in src


# ── serialize seam (per-host stdout: JSON for Claude/Codex/Cursor, raw for Kimi) ─


@pytest.mark.parametrize("adapter", [_CLAUDE, _CODEX, _CURSOR])
def test_json_hosts_serialize_is_json_dumps_byte_identical(adapter):
    # The default serialize() must stay byte-identical to the pre-seam emit
    # (`json.dumps(output, ensure_ascii=False)`) for every JSON host — both the
    # nested Claude/Codex envelope and Cursor's flat keys. The non-ASCII payload
    # pins the ``ensure_ascii=False`` half: with only-ASCII inputs a regression to
    # ``ensure_ascii=True`` is invisible, yet it would `\uXXXX`-escape the Korean
    # LTM blocks this project routinely surfaces.
    for rendered in (
        {},
        {"additional_context": "<surfaced-memories>\nMEM\n</surfaced-memories>"},
        {"additional_context": "<surfaced-memories>\n한국어 메모\n</surfaced-memories>"},
        {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "x"}},
    ):
        assert adapter.serialize(rendered) == json.dumps(rendered, ensure_ascii=False)
        # Guard the escape directly: a non-ASCII block must NOT be \uXXXX-escaped.
        if "한국어" in str(rendered):
            assert "한국어" in adapter.serialize(rendered)
            assert "\\ud55c" not in adapter.serialize(rendered)


@pytest.mark.parametrize("adapter", [_CLAUDE, _CODEX, _CURSOR])
def test_json_hosts_serialize_survives_a_lone_surrogate(adapter):
    """The echoed-back tool output is host-supplied and read with
    ``json.loads``, where ``"\\udcff"`` is a legal escape. Left raw it made the
    final ``click.echo`` raise, breaking the hook's always-exit-0 pass-through
    at the last step — so serialize escapes it instead (#757)."""
    rendered = {"hookSpecificOutput": {"updatedToolOutput": "out\udcffput"}}

    serialized = adapter.serialize(rendered)

    serialized.encode("utf-8")  # the encode click.echo does, which used to raise
    assert json.loads(serialized) == rendered


# ── Kimi adapter (B2 step3 — raw-stdout surfacing channel) ─────────────────────

_KIMI_DIR = _HOOK_FIXTURES / "kimi"


def test_kimi_parse_golden_inbound():
    payload = json.loads((_KIMI_DIR / "inbound_shell_posttooluse.json").read_text())
    call = _KIMI.parse(payload)
    assert isinstance(call, CanonicalHookCall)
    # Kimi's event is PascalCase PostToolUse already — no normalization needed.
    assert call.event_type == "PostToolUse"
    assert call.tool_name == "Shell"
    assert call.canonical_tool == "shell"
    assert call.tool_input == {"command": "pytest -q"}
    # Output is under ``tool_output`` (like Cursor), not ``tool_response``.
    assert call.tool_response == "12 passed in 3.4s"
    assert call.tool_response_text == "12 passed in 3.4s"
    assert call.host_tag == "kimi"


def test_kimi_render_then_serialize_golden_raw_stdout():
    # The golden: render → serialize must reproduce the committed RAW stdout
    # fixture exactly (the surfaced block, NOT JSON). Byte-identical to the .txt
    # (which stores the block with no trailing newline — the live newline is the
    # caller's click.echo).
    raw_path = _KIMI_DIR / "expected_surfacing_stdout.txt"
    block = raw_path.read_text()
    rendered = _KIMI.render(updated_tool_output=None, additional_context=block)
    out = _KIMI.serialize(rendered)
    assert out == block
    assert out.encode("utf-8") == raw_path.read_bytes()  # byte-identical, no framing
    # It is RAW text, not a JSON envelope — none of the JSON-host markers.
    assert not out.startswith("{")
    assert "additionalContext" not in out
    assert "hookSpecificOutput" not in out


def test_kimi_serialize_is_raw_not_json_vs_claude():
    block = "<surfaced-memories>\nMEM\n</surfaced-memories>"
    rendered = {"additional_context": block}
    # Kimi emits the block verbatim; the JSON hosts emit json.dumps of the dict.
    assert _KIMI.serialize(rendered) == block
    assert _CLAUDE.serialize(rendered) == json.dumps(rendered, ensure_ascii=False)
    assert _CLAUDE.serialize(rendered).startswith("{")


def test_kimi_serialize_empty_is_empty_stdout():
    # Nothing surfaced → empty stdout (Kimi adds only non-empty stdout to context).
    assert _KIMI.serialize({}) == ""
    assert _KIMI.serialize(_KIMI.render(updated_tool_output=None, additional_context=None)) == ""


def test_kimi_serialize_ignores_updated_output_no_replace_field():
    # Kimi has no output-replace channel: an updated output never appears in stdout;
    # only the surfaced block (raw) is emitted.
    block = "<surfaced-memories>\nMEM\n</surfaced-memories>"
    rendered = _KIMI.render(updated_tool_output={"stdout": "compressed"}, additional_context=block)
    assert _KIMI.serialize(rendered) == block


@pytest.mark.parametrize(
    ("native", "canonical"),
    [
        ("Shell", "shell"),
        ("ReadFile", "read"),
        ("WriteFile", "write"),
        ("StrReplaceFile", "edit"),
        ("Bash", "shell"),
        ("SomeFutureTool", ""),
    ],
)
def test_kimi_parse_canonicalizes_tool_name(native: str, canonical: str):
    call = _KIMI.parse({"tool_name": native, "tool_output": "x"})
    assert call is not None
    assert call.tool_name == native
    assert call.canonical_tool == canonical


@pytest.mark.parametrize("mcp_tool", ["mcp__memtomem__mem_search", "mcp__github__create_issue"])
def test_kimi_parse_rejects_mcp_tools(mcp_tool: str):
    assert _KIMI.parse({"tool_name": mcp_tool, "tool_output": "x"}) is None


@pytest.mark.parametrize("bad", [None, "not a dict", [1, 2, 3], 42])
def test_kimi_parse_non_dict_is_none(bad):
    assert _KIMI.parse(bad) is None


@pytest.mark.parametrize(
    "payload", [{}, {"tool_name": 123}, {"tool_name": None}, {"tool_name": []}]
)
def test_kimi_parse_coerces_missing_or_non_str_tool_name(payload):
    # Never-raises contract: a missing/non-str tool_name coerces to "" before the
    # mcp__ startswith guard.
    call = _KIMI.parse({**payload, "tool_output": "x"})
    assert call is not None
    assert call.tool_name == ""
    assert call.canonical_tool == ""


@pytest.mark.parametrize("bad_input", [["x"], "weird", 42, 0, False])
def test_kimi_parse_coerces_non_dict_tool_input(bad_input):
    call = _KIMI.parse({"tool_name": "Shell", "tool_input": bad_input, "tool_output": "x"})
    assert call is not None
    assert call.tool_input == {}


def test_kimi_parse_flattens_dict_output_via_shared_helper():
    response = {"stdout": "ok", "stderr": ""}
    call = _KIMI.parse({"tool_name": "Shell", "tool_output": response})
    assert call is not None
    assert call.tool_response_text == _tool_response_to_text(response)
    assert call.tool_response_text == "ok"


def test_kimi_to_wire_carries_host_tag():
    call = _KIMI.parse({"tool_name": "Shell", "tool_input": {"command": "ls"}, "tool_output": "ok"})
    assert call is not None
    assert call.to_wire()["host_tag"] == "kimi"


def test_kimi_capability_and_tag():
    # B0: Kimi has no output-replace field at all → surfacing-only.
    assert _KIMI.host_tag == "kimi"
    assert _KIMI.can_replace_output is False


def test_kimi_serialize_emits_raw_not_json():
    # The seam pin: Kimi's serialize must NOT json.dumps — it emits the block text.
    src = inspect.getsource(KimiHookAdapter.serialize)
    assert "json.dumps" not in src
    assert "additional_context" in src


def test_kimi_parse_delegates_flattening_to_shared_helper():
    src = inspect.getsource(KimiHookAdapter.parse)
    assert "_tool_response_to_text(" in src


# ── host selection (--host / auto-detect, B4) ──────────────────────────────────


def test_known_hosts_matches_registry():
    # The values ``--host`` accepts are derived from the registry, so the CLI
    # choice and the registered adapters never drift apart.
    assert known_hosts() == ("claude", "codex", "cursor", "kimi")
    for tag in known_hosts():
        assert get_adapter(tag).host_tag == tag


def test_detect_host_cursor_by_camelcase_event():
    # Cursor's only unique signature is the camelCase ``postToolUse`` event.
    payload = json.loads((_HOOK_FIXTURES / "cursor" / "inbound_shell_posttooluse.json").read_text())
    assert detect_host(payload) == "cursor"


def test_detect_host_kimi_by_tool_output_pascalcase():
    # Kimi: ``tool_output`` (no ``tool_response``) + PascalCase ``PostToolUse``.
    payload = json.loads((_HOOK_FIXTURES / "kimi" / "inbound_shell_posttooluse.json").read_text())
    assert detect_host(payload) == "kimi"


def test_detect_host_claude_by_tool_response():
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_response": "x"}
    assert detect_host(payload) == "claude"


def test_detect_host_codex_payload_uses_turn_id():
    # Codex's payload is shape-identical to Claude's (snake_case, tool_response,
    # PascalCase PostToolUse) — auto-detect CANNOT distinguish them and must
    # resolve to claude (safe: identical parse + surfacing envelope). Codex users
    # pass --host codex explicitly. This pins the documented ambiguity.
    payload = json.loads((_HOOK_FIXTURES / "codex" / "inbound_bash_posttooluse.json").read_text())
    assert detect_host(payload) == "codex"


@pytest.mark.parametrize("bad", [None, "not a dict", [1, 2, 3], 42, {}])
def test_detect_host_falls_back_to_claude(bad):
    # Non-dict / empty / unrecognized payloads fall back to claude (the adapter
    # then no-ops on the unusable payload, as today).
    assert detect_host(bad) == "claude"


def test_detect_host_cursor_event_wins_over_tool_output():
    # Both Cursor and Kimi carry ``tool_output``; the camelCase event disambiguates
    # to Cursor (checked first), so a Cursor payload never mis-detects as Kimi.
    payload = {"hook_event_name": "postToolUse", "tool_name": "Shell", "tool_output": "x"}
    assert detect_host(payload) == "cursor"


def test_detect_host_both_output_keys_resolves_to_claude():
    # A payload carrying BOTH ``tool_output`` AND ``tool_response`` is ambiguous; the
    # Kimi branch requires ``tool_response`` ABSENT, so it resolves to claude (the
    # safe default — its adapter reads ``tool_response``). Intended behavior, pinned
    # here (#526 secondary) so a future adapter/detect change can't silently flip
    # ambiguous Kimi/Cursor/Claude routing.
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Shell",
        "tool_output": "x",
        "tool_response": "y",
    }
    assert detect_host(payload) == "claude"
