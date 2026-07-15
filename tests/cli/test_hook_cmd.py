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

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.hook_adapter import (
    ClaudeHookAdapter,
    CodexHookAdapter,
    CursorHookAdapter,
    KimiHookAdapter,
)
from memtomem_stm.cli.hook_cmd import (
    _COMPRESS_SENTINEL,
    _SAFE_DAEMON_BUDGET,
    _bounded_call,
    _build_hook_output,
    _daemon_enabled,
    _extract_surfaced_block,
    _orchestrate,
    _record_hook_metrics,
    _resolve_host_tag,
    _run_hook,
    _tool_response_to_text,
    compress_builtin,
    maybe_compress_builtin,
    run_surfacing_hook,
)
from memtomem_stm.cli.proxy import cli
from memtomem_stm.config import HookCompressionConfig
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine
from helpers import set_home


@pytest.fixture(autouse=True)
def _hermetic_hook_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Daemon mode is on by default now (``HookConfig.use_daemon=True``), so any
    ``mms hook`` / ``_run_hook`` call here would otherwise read the real
    ``~/.memtomem`` handshake and fire-and-forget ``Popen`` a detached daemon.
    Isolate daemon state, HOME, and the independently configured proxy metrics
    DB, then neutralize the spawn so these CLI tests never touch developer state
    or leak a subprocess. (Spawn behavior itself is covered in
    tests/daemon/test_server.py.)"""
    home = tmp_path / "home"
    metrics_db = tmp_path / "metrics" / "proxy_metrics.db"
    set_home(monkeypatch, home)
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path / "daemon"))
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__METRICS__DB_PATH", str(metrics_db))
    for key in (
        "MEMTOMEM_STM_HOOK__USE_DAEMON",
        "MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS",
        "MEMTOMEM_STM_SURFACING__USE_DAEMON",
        "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS",
        "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("memtomem_stm.daemon.spawn.request_spawn", lambda cfg: None)
    return metrics_db, home


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


def _canonical(payload: dict):
    """Parse a raw host payload into the CanonicalHookCall the hook core +
    compression + metrics consume (mirrors what ``_orchestrate`` does once)."""
    return ClaudeHookAdapter().parse(payload)


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
    out = await run_surfacing_hook(_canonical(_READ_PAYLOAD), engine=engine)
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
    assert await run_surfacing_hook(_canonical(_READ_PAYLOAD), engine=engine) == {}


async def test_run_hook_ignores_non_posttooluse():
    engine = _engine_with_results([_FakeResult(_FakeChunk(), 0.9)])
    payload = {**_READ_PAYLOAD, "hook_event_name": "PreToolUse"}
    assert await run_surfacing_hook(_canonical(payload), engine=engine) == {}


async def test_run_hook_requires_tool_name():
    # No tool_name → canonical_tool "" → not in the allowlist → no-op.
    engine = _engine_with_results([_FakeResult(_FakeChunk(), 0.9)])
    payload = {"hook_event_name": "PostToolUse", "tool_input": {"file_path": "x"}}
    assert await run_surfacing_hook(_canonical(payload), engine=engine) == {}


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit", "Task"])
async def test_run_hook_rejects_non_readlike_tools(tool: str):
    # Write/Edit/etc. must never surface — even with a matching engine and a
    # broad host matcher — so their inputs don't become queries. Their canonical
    # names (write/edit, or "" for tools outside the vocabulary) aren't in the
    # surface allowlist {read,grep,glob,shell}.
    engine = _engine_with_results([_FakeResult(_FakeChunk(content="secret"), 0.9)])
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "tool_input": {"file_path": "/a/b.py", "old_string": "x", "new_string": "y"},
        "tool_response": {"content": _LONG},
    }
    assert await run_surfacing_hook(_canonical(payload), engine=engine) == {}


async def test_run_hook_allowlist_env_override(monkeypatch: pytest.MonkeyPatch):
    # The override lists *canonical* names. Set it to grep-only: the default
    # read (Read) is now rejected, and grep (Grep) is accepted.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", "grep")
    engine = _engine_with_results([_FakeResult(_FakeChunk(content="hit"), 0.9)])
    assert await run_surfacing_hook(_canonical(_READ_PAYLOAD), engine=engine) == {}
    grep_payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Grep",
        "tool_input": {"pattern": "jwt_auth_handler_token"},
        "tool_response": {"content": _LONG},
    }
    out = await run_surfacing_hook(_canonical(grep_payload), engine=engine)
    assert "hit" in out["hookSpecificOutput"]["additionalContext"]


async def test_run_hook_allowlist_env_accepts_legacy_native_names(monkeypatch: pytest.MonkeyPatch):
    # Back-compat: the env historically listed Claude's native names. A legacy
    # value still resolves (Bash → shell) so surfacing keeps firing — the
    # canonical rename is not a silent breaking change.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", "Bash")
    engine = _engine_with_results([_FakeResult(_FakeChunk(content="hit"), 0.9)])
    # Read (canonical read) is not in the Bash-only allowlist → rejected.
    assert await run_surfacing_hook(_canonical(_READ_PAYLOAD), engine=engine) == {}
    # Bash (legacy native → canonical shell) is accepted.
    out = await run_surfacing_hook(_canonical(_bash_payload({"stdout": _LONG})), engine=engine)
    assert "hit" in out["hookSpecificOutput"]["additionalContext"]


async def test_run_hook_passes_host_native_tool_name_to_engine():
    # Behavior-preservation lever: the surfacing engine receives the HOST-NATIVE
    # tool name (Bash), not the canonical (shell), so query extraction — which
    # can fall back to the tool name as a query token — is unchanged for Claude.
    spy = AsyncMock()
    spy.surface = AsyncMock(return_value="orig")
    spy.injection_mode = "append"
    await run_surfacing_hook(_canonical(_bash_payload({"stdout": _LONG})), engine=spy)
    args = spy.surface.await_args.args
    assert args[0] == "builtin"
    assert args[1] == "Bash"  # NOT "shell"


def test_surface_tools_resolves_canonical_native_and_unknown(monkeypatch: pytest.MonkeyPatch):
    from memtomem_stm.cli.hook_cmd import _surface_tools

    # Mixed: a canonical name kept, a legacy native name translated, an unknown
    # token dropped (logged). The two valid tokens resolve; the bogus one does not.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", "read, Bash, Bogus")
    assert _surface_tools() == frozenset({"read", "shell"})

    # An explicit list that resolves to nothing → empty allowlist (NOT the
    # default — the operator restricted, even if every token was a typo).
    monkeypatch.setenv("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", "Bogus,Nope")
    assert _surface_tools() == frozenset()

    # All-blank is unset-equivalent → default.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", " , ")
    assert _surface_tools() == frozenset({"read", "grep", "glob", "shell"})


async def test_run_hook_never_raises_on_engine_error():
    # The "never raises" contract must hold for direct callers (a future
    # daemon), not only the CLI's outer guard.
    boom = AsyncMock()
    boom.surface = AsyncMock(side_effect=RuntimeError("LTM exploded"))
    boom.injection_mode = "append"
    assert await run_surfacing_hook(_canonical(_READ_PAYLOAD), engine=boom) == {}


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
    # Default daemon-on path + surfacing globally off → {} immediately, and the
    # hook must NOT spawn a daemon (nothing to surface) or build an LTM adapter.
    monkeypatch.delenv("MEMTOMEM_STM_HOOK__USE_DAEMON", raising=False)  # prove default-on
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "false")
    spawns: list[int] = []
    monkeypatch.setattr("memtomem_stm.daemon.spawn.request_spawn", lambda cfg: spawns.append(1))
    result = CliRunner().invoke(cli, ["hook"], input=json.dumps(_READ_PAYLOAD))
    assert result.exit_code == 0
    assert json.loads(result.output) == {}
    assert spawns == []  # surfacing disabled → no daemon spawn


def test_cli_kimi_empty_surfacing_emits_truly_empty_stdout(monkeypatch: pytest.MonkeyPatch):
    # Kimi's surfacing channel is RAW stdout: a non-empty stdout is injected into
    # the model context, so "nothing surfaced" must emit a *truly* empty stdout —
    # not the "\n" an unconditional ``click.echo`` appends to an empty payload.
    # ``KimiHookAdapter.serialize({}) == ""``, so this pins the CLI emit (not just
    # serialize's return) once host selection can route to Kimi. A regression to
    # ``click.echo(serialize(output))`` makes ``result.output == "\n"`` and fails.
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd.get_adapter", lambda *a, **k: KimiHookAdapter())
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._orchestrate", AsyncMock(return_value={}))
    result = CliRunner().invoke(cli, ["hook"], input=json.dumps(_READ_PAYLOAD))
    assert result.exit_code == 0
    assert result.output == ""


# ── Host selection (--host / auto-detect, B4) ────────────────────────────────

_KIMI_SHELL_PAYLOAD = {
    "hook_event_name": "PostToolUse",
    "tool_name": "Shell",  # Kimi's native shell name → canonical "shell"
    "tool_input": {"command": "pytest -q"},
    "tool_output": "12 passed in 3.4s",  # Kimi/Cursor carry output under tool_output
}
_SURF_BLOCK = "<surfaced-memories>\nMEM\n</surfaced-memories>"
_SURF_DICT = {
    "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": _SURF_BLOCK}
}


def test_cli_host_kimi_is_metrics_only(monkeypatch: pytest.MonkeyPatch):
    # --host kimi resolves the Kimi adapter end-to-end (NO get_adapter monkeypatch),
    # so a surfaced block is emitted as RAW stdout, not a JSON envelope. Only the
    # surfacing core is mocked; the real parse → render → serialize chain runs.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__METRICS_ENABLED", "0")  # no sqlite write
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._run_hook", AsyncMock(return_value=_SURF_DICT))
    result = CliRunner().invoke(
        cli, ["hook", "--host", "kimi"], input=json.dumps(_KIMI_SHELL_PAYLOAD)
    )
    assert result.exit_code == 0
    assert result.output == ""
    assert not result.output.startswith("{")  # not the JSON hookSpecificOutput envelope


def test_cli_host_auto_detects_kimi_from_payload(monkeypatch: pytest.MonkeyPatch):
    # Default (--host auto) infers the host from the payload shape: a Kimi-shaped
    # payload (tool_output + PascalCase event) routes through the Kimi raw-stdout
    # adapter without an explicit flag.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__METRICS_ENABLED", "0")
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._run_hook", AsyncMock(return_value=_SURF_DICT))
    result = CliRunner().invoke(cli, ["hook"], input=json.dumps(_KIMI_SHELL_PAYLOAD))
    assert result.exit_code == 0
    assert result.output == ""


def test_cli_host_claude_emits_json_envelope(monkeypatch: pytest.MonkeyPatch):
    # The Claude/Codex/Cursor JSON hosts emit the nested hookSpecificOutput
    # envelope — the same surfaced block, but JSON, not raw stdout.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__METRICS_ENABLED", "0")
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._run_hook", AsyncMock(return_value=_SURF_DICT))
    result = CliRunner().invoke(cli, ["hook", "--host", "claude"], input=json.dumps(_READ_PAYLOAD))
    assert result.exit_code == 0
    assert json.loads(result.output) == _SURF_DICT


@pytest.mark.parametrize("host", ["claude", "codex", "cursor", "kimi", "auto"])
def test_cli_host_accepts_every_registered_host(host: str):
    # Every registered host (+ auto) is an accepted --host value; a malformed
    # payload still degrades cleanly (exit 0). Pins the runtime --host ↔ registry tie
    # (the runtime option is now a plain string, validated fail-open in the body —
    # see test_cli_runtime_invalid_host_fails_open).
    result = CliRunner().invoke(cli, ["hook", "--host", host], input="not json")
    assert result.exit_code == 0


def test_cli_runtime_invalid_host_fails_open():
    # The runtime bridge is fired NON-interactively by the host, which treats a
    # non-zero exit as a block/deny — so an unrecognized --host must NOT exit 2 the
    # way #524 shipped. #526 reverses that: it warns and falls back to auto-detect,
    # passing the tool output through (exit 0, {} for the JSON Claude fallback).
    result = CliRunner().invoke(cli, ["hook", "--host", "bogus"], input="not json")
    assert result.exit_code == 0
    assert result.output.strip() == "{}"


def test_cli_runtime_bare_host_flag_fails_open():
    # A bare `--host` with the value OMITTED (a plausible hand-edit) must NOT trip
    # Click's "requires an argument" exit 2 before the fail-open body runs. The
    # option's flag_value="auto" resolves it to auto-detect, so it exits 0 and
    # passes through — closing the missing-value half of the contract (#526; the
    # invalid-value half is test_cli_runtime_invalid_host_fails_open).
    result = CliRunner().invoke(cli, ["hook", "--host"], input="not json")
    assert result.exit_code == 0
    assert result.output.strip() == "{}"


def test_cli_managed_runtime_flags_are_scoped_and_order_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "false")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS", "77")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS", "88")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT", "true")
    seen: dict[str, str | None] = {}

    async def capture(*_args, **_kwargs):
        for key in (
            "MEMTOMEM_STM_HOOK__USE_DAEMON",
            "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS",
            "MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS",
            "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT",
        ):
            seen[key] = os.environ.get(key)
        return {}

    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._orchestrate", capture)
    result = CliRunner().invoke(
        cli,
        [
            "hook",
            "--host",
            "claude",
            "--use-daemon",
            "--surfacing-timeout-seconds",
            "12",
            "--daemon-timeout-seconds",
            "2",
            "--no-persist-query-text",
        ],
        input=json.dumps(_READ_PAYLOAD),
    )

    assert result.exit_code == 0, result.output
    assert seen == {
        "MEMTOMEM_STM_HOOK__USE_DAEMON": "true",
        "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS": "12",
        "MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS": "13",
        "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT": "false",
    }
    # CliRunner/embedders reuse this process: every ambient value is restored.
    assert os.environ["MEMTOMEM_STM_HOOK__USE_DAEMON"] == "false"
    assert os.environ["MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS"] == "77"
    assert os.environ["MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS"] == "88"
    assert os.environ["MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT"] == "true"


def test_cli_invalid_managed_timeout_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._orchestrate", AsyncMock(return_value={}))
    result = CliRunner().invoke(
        cli,
        ["hook", "--surfacing-timeout-seconds", "not-a-number"],
        input=json.dumps(_READ_PAYLOAD),
    )
    assert result.exit_code == 0
    assert result.output.strip() == "{}"


def test_cli_install_missing_host_value_is_usage_error():
    # Symmetric guard: the operator `install` command's --host is NOT optional —
    # a bare `--host` there is still a usage error (exit 2). Pins that the runtime
    # flag_value leniency did not leak into install/uninstall.
    result = CliRunner().invoke(cli, ["hook", "install", "--host"])
    assert result.exit_code == 2


def test_cli_runtime_invalid_host_falls_back_to_autodetect(monkeypatch: pytest.MonkeyPatch):
    # The fallback is auto-detect (detect_host on the payload), NOT a hard-coded
    # Claude: a Kimi-shaped payload under a bogus --host still routes through Kimi's
    # raw-stdout adapter. Pins that _resolve_host_tag falls back via detect_host,
    # not merely get_adapter's claude default.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__METRICS_ENABLED", "0")
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._run_hook", AsyncMock(return_value=_SURF_DICT))
    result = CliRunner().invoke(
        cli, ["hook", "--host", "bogus"], input=json.dumps(_KIMI_SHELL_PAYLOAD)
    )
    assert result.exit_code == 0
    assert result.output == ""


def test_resolve_host_tag_known_host_is_authoritative():
    # A known --host wins over the payload shape: a Claude-shaped payload still
    # routes through Kimi when --host kimi is explicit.
    claude_shaped = {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_response": "x"}
    assert _resolve_host_tag("kimi", claude_shaped) == "kimi"


def test_resolve_host_tag_auto_infers_from_payload():
    kimi_shaped = {"hook_event_name": "PostToolUse", "tool_name": "Shell", "tool_output": "x"}
    assert _resolve_host_tag("auto", kimi_shaped) == "kimi"
    assert _resolve_host_tag("auto", None) == "claude"  # detect_host's default


@pytest.mark.parametrize("bogus", ["bogus", "", "  ", "CLAUDE"])
def test_resolve_host_tag_unknown_falls_back_to_autodetect(bogus: str):
    # Unknown values (typo / blank / wrong case — click.Choice is case-sensitive,
    # mirrored here) never raise: they fall back to detect_host on the payload, so a
    # Kimi-shaped payload still resolves to kimi rather than a hard-coded claude.
    kimi_shaped = {"hook_event_name": "PostToolUse", "tool_name": "Shell", "tool_output": "x"}
    assert _resolve_host_tag(bogus, kimi_shaped) == "kimi"
    assert _resolve_host_tag(bogus, None) == "claude"  # detect_host's default


def test_resolve_host_tag_strips_whitespace():
    # A hand-edited config with a padded value (" kimi ") still resolves to the host
    # rather than tripping the unknown-host fallback.
    assert _resolve_host_tag(" kimi ", None) == "kimi"


# ── Daemon routing + degradation ladder ──────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("f", False),
        ("no", False),
        ("n", False),
        ("off", False),
    ],
)
def test_daemon_enabled_env_parsing(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool):
    # Falsy-token set matches Pydantic's bool parsing, so the hot path and
    # `mms daemon status` (the parsed field) never disagree on a value Pydantic
    # accepts.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", value)
    assert _daemon_enabled() is expected


def test_daemon_enabled_default_when_unset(monkeypatch: pytest.MonkeyPatch):
    # Daemon mode is the default now — unset → enabled.
    monkeypatch.delenv("MEMTOMEM_STM_HOOK__USE_DAEMON", raising=False)
    assert _daemon_enabled() is True


def test_daemon_explicitly_disabled_uses_cold_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "0")
    sentinel = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "cold"}}
    monkeypatch.setattr(
        "memtomem_stm.cli.hook_cmd.run_surfacing_hook", AsyncMock(return_value=sentinel)
    )
    # client.surface must never be consulted when the daemon is opted out.
    boom = AsyncMock(side_effect=AssertionError("daemon must not be used when disabled"))
    monkeypatch.setattr("memtomem_stm.daemon.client.surface", boom)
    out = asyncio.run(_run_hook(_canonical(_READ_PAYLOAD)))
    assert out == sentinel
    boom.assert_not_called()


def test_daemon_used_when_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")
    daemon_out = {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "warm"}
    }
    monkeypatch.setattr("memtomem_stm.daemon.client.surface", AsyncMock(return_value=daemon_out))
    # Cold path must not run when the daemon answers.
    monkeypatch.setattr(
        "memtomem_stm.cli.hook_cmd.run_surfacing_hook",
        AsyncMock(side_effect=AssertionError("cold path must not run on a daemon hit")),
    )
    out = asyncio.run(_run_hook(_canonical(_READ_PAYLOAD)))
    assert out == daemon_out


def test_daemon_not_used_for_ineligible_tool(monkeypatch: pytest.MonkeyPatch):
    # Default daemon-on, but a non-allowlisted tool (Write) → {} without
    # consulting or spawning the daemon: an off-target call must not warm one.
    monkeypatch.delenv("MEMTOMEM_STM_HOOK__USE_DAEMON", raising=False)  # prove default-on
    spawns: list[int] = []
    monkeypatch.setattr("memtomem_stm.daemon.spawn.request_spawn", lambda cfg: spawns.append(1))
    surface = AsyncMock(
        side_effect=AssertionError("daemon must not be consulted for an ineligible tool")
    )
    monkeypatch.setattr("memtomem_stm.daemon.client.surface", surface)
    out = asyncio.run(_run_hook(_canonical({**_READ_PAYLOAD, "tool_name": "Write"})))
    assert out == {}
    assert spawns == []
    surface.assert_not_called()


def test_daemon_unavailable_skip_returns_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")
    monkeypatch.delenv("MEMTOMEM_STM_HOOK__FALLBACK", raising=False)  # default = skip
    monkeypatch.setattr("memtomem_stm.daemon.client.surface", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "memtomem_stm.cli.hook_cmd.run_surfacing_hook",
        AsyncMock(side_effect=AssertionError("skip must not fall back to the cold path")),
    )
    out = asyncio.run(_run_hook(_canonical(_READ_PAYLOAD)))
    assert out == {}


def test_daemon_unavailable_cold_fallback(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__FALLBACK", "cold")
    monkeypatch.setattr("memtomem_stm.daemon.client.surface", AsyncMock(return_value=None))
    sentinel = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "cold"}}
    monkeypatch.setattr(
        "memtomem_stm.cli.hook_cmd.run_surfacing_hook", AsyncMock(return_value=sentinel)
    )
    out = asyncio.run(_run_hook(_canonical(_READ_PAYLOAD)))
    assert out == sentinel


# ── Bash output compression (P1a — updatedToolOutput) ─────────────────────────

_BIG_STDOUT = "log line %d\n" % 0 + "".join(f"log line {i}\n" for i in range(1, 4000))  # ~50KB
_CFG = HookCompressionConfig(enabled=True, max_chars=2000, min_retention=0.0)


@pytest.fixture(autouse=True)
def permissive_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__MIN_RETENTION", "0")


def _bash_payload(tool_response, *, tool="Bash", event="PostToolUse"):
    return {
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": {"command": "seq 1 4000"},
        "tool_response": tool_response,
    }


def _bash_call(tool_response, *, tool="Bash", event="PostToolUse"):
    """The CanonicalHookCall ``maybe_compress_builtin`` now consumes."""
    return _canonical(_bash_payload(tool_response, tool=tool, event=event))


def test_compress_dict_preserves_metadata_and_shrinks_stdout():
    resp = {"stdout": _BIG_STDOUT, "stderr": "a warning", "interrupted": False, "isImage": False}
    out = maybe_compress_builtin(_bash_call(resp), _CFG)
    assert isinstance(out, dict)
    # Only stdout is replaced; it is shrunk and carries the sentinel.
    assert out["stdout"].startswith(_COMPRESS_SENTINEL)
    assert len(out["stdout"]) < len(_BIG_STDOUT)
    # Budget is a target: sentinel reserved out of it, small compressor-suffix
    # overage tolerated — but the result must stay close to max_chars, not blow it.
    assert len(out["stdout"]) <= _CFG.max_chars + 200
    # Every other channel survives verbatim (Codex: must not lose stderr/metadata).
    assert out["stderr"] == "a warning"
    assert out["interrupted"] is False
    assert out["isImage"] is False
    # Original payload is not mutated in place.
    assert resp["stdout"] == _BIG_STDOUT


def test_compress_reserves_sentinel_from_budget(monkeypatch: pytest.MonkeyPatch):
    # Directly prove the sentinel prefix is reserved out of the compressor's
    # budget (Codex nit): capture the max_chars handed to TruncateCompressor and
    # assert it is cfg.max_chars minus the prefix length — a regression back to
    # passing the full budget would fail here even though the repetitive fixture
    # compresses well under max_chars.
    seen: dict[str, int] = {}

    class _Spy:
        def compress(self, text: str, *, max_chars: int) -> str:
            seen["budget"] = max_chars
            return "BODY"

    monkeypatch.setattr("memtomem_stm.proxy.compression.TruncateCompressor", _Spy)
    out = maybe_compress_builtin(_bash_call({"stdout": _BIG_STDOUT}), _CFG)
    prefix_len = len(_COMPRESS_SENTINEL) + 1  # sentinel + "\n"
    assert seen["budget"] == _CFG.max_chars - prefix_len
    assert out["stdout"] == f"{_COMPRESS_SENTINEL}\nBODY"


def test_compress_skipped_when_budget_below_sentinel():
    # max_chars only validates gt=0, so it can be configured below the
    # sentinel prefix length. Prepending the sentinel anyway would EXPAND
    # the configured cap many-fold (max_chars=1 → ~18 chars) — the stage
    # must skip instead of "compressing" past its own budget.
    cfg = HookCompressionConfig(enabled=True, max_chars=len(_COMPRESS_SENTINEL))
    assert maybe_compress_builtin(_bash_call({"stdout": _BIG_STDOUT}), cfg) is None


def test_compress_noop_for_plain_string_response():
    # For the built-in Bash tool, updatedToolOutput must be a structured object;
    # a bare string would be ignored by the host, so an unstructured response is
    # left untouched rather than replaced (Codex Major).
    assert maybe_compress_builtin(_bash_call(_BIG_STDOUT), _CFG) is None


def test_compress_noop_when_small():
    assert maybe_compress_builtin(_bash_call({"stdout": "tiny"}), _CFG) is None


def test_compress_noop_when_disabled():
    cfg = HookCompressionConfig(enabled=False, max_chars=2000)
    assert maybe_compress_builtin(_bash_call({"stdout": _BIG_STDOUT}), cfg) is None


def test_compress_retention_guard_passes_through_large_output():
    cfg = HookCompressionConfig(enabled=True, max_chars=2000, min_retention=0.65)
    outcome = compress_builtin(_bash_call({"stdout": _BIG_STDOUT}), cfg)
    assert outcome.status == "retention_guard"
    assert outcome.replacement is None
    assert outcome.compressed_chars < outcome.original_chars * cfg.min_retention


@pytest.mark.parametrize(
    "unsafe_field",
    [{"exitCode": 1}, {"isError": True}, {"interrupted": True}, {"isImage": True}],
)
def test_compress_unsafe_results_pass_through(unsafe_field: dict[str, object]):
    outcome = compress_builtin(_bash_call({"stdout": _BIG_STDOUT, **unsafe_field}), _CFG)
    assert outcome.status == "unsafe_result"
    assert outcome.replacement is None


def test_compress_noop_for_non_bash_tool():
    # Read output must never be replaced (a later Edit needs it verbatim).
    assert maybe_compress_builtin(_bash_call({"stdout": _BIG_STDOUT}, tool="Read"), _CFG) is None


def test_compress_noop_for_non_posttooluse():
    assert (
        maybe_compress_builtin(_bash_call({"stdout": _BIG_STDOUT}, event="PreToolUse"), _CFG)
        is None
    )


def test_compress_is_idempotent_on_sentinel():
    out = maybe_compress_builtin(_bash_call({"stdout": _BIG_STDOUT}), _CFG)
    # Feeding the already-compressed stdout back must not re-compress.
    assert maybe_compress_builtin(_bash_call({"stdout": out["stdout"]}), _CFG) is None


def test_compress_no_false_positive_on_truncate_marker():
    # A real log that happens to contain TruncateCompressor's generic marker text
    # must STILL be compressed — only the unique sentinel suppresses (Codex Major).
    poisoned = (
        _BIG_STDOUT + "\n... (truncated, original: 5 chars)\n... (12 similar lines omitted)\n"
    )
    out = maybe_compress_builtin(_bash_call({"stdout": poisoned}), _CFG)
    assert out is not None and out["stdout"].startswith(_COMPRESS_SENTINEL)


def test_compress_no_false_positive_on_embedded_sentinel():
    # Self-referential output (``git log`` / ``grep`` / ``cat`` over STM's own
    # repo, history, or docs) can legitimately contain the sentinel string
    # mid-text. Idempotency keys on OUR prefix only, so such output must still be
    # compressed. Regression: a bare ``in`` match left the whole result
    # uncompressed (observed on ``git log --stat`` whose commit body names the
    # sentinel).
    embedded = f"a commit body mentioning the {_COMPRESS_SENTINEL} sentinel\n" + _BIG_STDOUT
    out = maybe_compress_builtin(_bash_call({"stdout": embedded}), _CFG)
    assert out is not None and out["stdout"].startswith(_COMPRESS_SENTINEL)


def test_compress_never_raises(monkeypatch: pytest.MonkeyPatch):
    class _Boom:
        def compress(self, *a, **k):
            raise RuntimeError("compressor exploded")

    monkeypatch.setattr("memtomem_stm.proxy.compression.TruncateCompressor", _Boom)
    assert maybe_compress_builtin(_bash_call({"stdout": _BIG_STDOUT}), _CFG) is None


# ── native-tool metrics (A1 — proxy_metrics.db row with source='hook') ───────


def _metrics_config(tmp_path: Path, *, enabled: bool = True):
    """Minimal duck-typed STMConfig for ``_record_hook_metrics`` — only the two
    attributes it reads, so no env/file/frozen-model coupling."""
    from types import SimpleNamespace

    return SimpleNamespace(
        hook=SimpleNamespace(metrics_enabled=enabled),
        proxy=SimpleNamespace(
            metrics=SimpleNamespace(db_path=tmp_path / "metrics.db", max_history=10000)
        ),
    )


def test_record_hook_metrics_writes_source_hook_row(tmp_path: Path):
    from memtomem_stm.proxy.metrics_store import read_compression_summary

    cfg = _metrics_config(tmp_path)
    updated = {"stdout": f"{_COMPRESS_SENTINEL}\nshort", "stderr": ""}
    _record_hook_metrics(_canonical(_bash_payload({"stdout": _BIG_STDOUT})), updated, _BLOCK, cfg)

    summary = read_compression_summary(tmp_path / "metrics.db", source="hook")
    assert summary["available"] is True
    assert summary["total_calls"] == 1
    row = summary["by_tool"][0]
    assert (row["server"], row["tool"]) == ("builtin", "Bash")
    assert row["original_chars"] == len(_BIG_STDOUT)
    assert row["compressed_chars"] == len(f"{_COMPRESS_SENTINEL}\nshort")


def test_record_hook_metrics_no_compression_original_equals_compressed(tmp_path: Path):
    from memtomem_stm.proxy.metrics_store import read_compression_summary

    cfg = _metrics_config(tmp_path)
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/x"},
        "tool_response": {"content": "y" * 4000},
    }
    # Both stages no-op (Read is never compressed; no surfaced context).
    _record_hook_metrics(_canonical(payload), None, None, cfg)

    row = read_compression_summary(tmp_path / "metrics.db", source="hook")["by_tool"][0]
    assert row["tool"] == "Read"
    assert row["original_chars"] == 4000
    assert row["compressed_chars"] == 4000
    assert row["saved_ratio"] == 0.0


def test_orchestrate_writes_metrics_only_to_hermetic_db(
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_hook_state: tuple[Path, Path],
):
    """The real config path must stay inside the fixture, not ``~/.memtomem``.

    Unlike the unit tests above, this exercises ``STMConfig()`` construction in
    ``_orchestrate`` so a missing metrics-path override cannot silently write a
    fixture row into the developer's live metrics history.
    """
    from memtomem_stm.proxy.metrics_store import read_compression_summary

    metrics_db, home = _hermetic_hook_state
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__METRICS_ENABLED", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "0")
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._run_hook", AsyncMock(return_value={}))

    out = asyncio.run(_orchestrate(_bash_payload({"stdout": _BIG_STDOUT}), ClaudeHookAdapter()))

    assert out == {}
    summary = read_compression_summary(metrics_db, source="hook")
    assert summary["total_calls"] == 1
    row = summary["by_tool"][0]
    assert (row["server"], row["tool"]) == ("builtin", "Bash")
    assert row["original_chars"] == len(_BIG_STDOUT)
    assert row["compressed_chars"] == len(_BIG_STDOUT)
    assert not (home / ".memtomem" / "proxy_metrics.db").exists()


def test_record_hook_metrics_disabled_writes_nothing(tmp_path: Path):
    cfg = _metrics_config(tmp_path, enabled=False)
    _record_hook_metrics(_canonical(_bash_payload({"stdout": _BIG_STDOUT})), None, None, cfg)
    assert not (tmp_path / "metrics.db").exists()  # store never opened


def test_record_hook_metrics_skips_empty_output(tmp_path: Path):
    # A result that flattens to nothing carries no spend to record.
    cfg = _metrics_config(tmp_path)
    _record_hook_metrics(
        _canonical({"hook_event_name": "PostToolUse", "tool_name": "Bash"}), None, None, cfg
    )
    assert not (tmp_path / "metrics.db").exists()


def test_record_hook_metrics_never_raises_on_bad_store(tmp_path: Path):
    from types import SimpleNamespace

    # db_path is a directory → MetricsStore.initialize() raises; must be swallowed
    # so metrics can never disrupt the host.
    bad = SimpleNamespace(
        hook=SimpleNamespace(metrics_enabled=True),
        proxy=SimpleNamespace(metrics=SimpleNamespace(db_path=tmp_path, max_history=10000)),
    )
    _record_hook_metrics(_canonical(_bash_payload({"stdout": _BIG_STDOUT})), None, None, bad)


def test_record_hook_metrics_degrades_quickly_when_db_locked(tmp_path: Path):
    import sqlite3
    import time

    from memtomem_stm.proxy.metrics_store import MetricsStore, read_compression_summary

    db = tmp_path / "metrics.db"
    # Create the schema first so the lock contention is purely on the write.
    seed = MetricsStore(db)
    seed.initialize()
    seed.close()

    # Hold an EXCLUSIVE write lock from another connection so the hook's writer
    # contends. Its 250 ms busy timeout must make it fail fast (no row, no raise,
    # no multi-second stall up to the shared 3000 ms budget).
    blocker = sqlite3.connect(str(db), timeout=0.1)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        cfg = _metrics_config(tmp_path)  # db_path == db
        start = time.monotonic()
        _record_hook_metrics(_canonical(_bash_payload({"stdout": _BIG_STDOUT})), None, None, cfg)
        elapsed = time.monotonic() - start
    finally:
        blocker.rollback()
        blocker.close()

    # Fast-fail: comfortably under the shared 3000 ms busy timeout (generous
    # bound to stay non-flaky in CI) and nothing persisted.
    assert elapsed < 2.0
    assert read_compression_summary(db, source="hook")["total_calls"] == 0


# ── bounded canonical call + merge builder ───────────────────────────────────


def test_bounded_call_passthrough_when_small():
    call = _bash_call({"stdout": "small"})
    assert _bounded_call(call) is call  # same object, no copy


def test_bounded_call_caps_huge_text():
    huge = "x" * (_SAFE_DAEMON_BUDGET + 5000)
    call = _bash_call({"stdout": huge, "stderr": "y" * 100})
    bounded = _bounded_call(call)
    assert bounded is not call
    assert len(bounded.tool_response_text) == _SAFE_DAEMON_BUDGET
    # The capped copy is sent over the wire; nothing else changes.
    assert bounded.tool_name == call.tool_name
    assert bounded.canonical_tool == "shell"


def test_build_hook_output_merges_both_halves():
    out = _build_hook_output({"stdout": "c"}, "<surfaced-memories>m</surfaced-memories>")
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert hso["updatedToolOutput"] == {"stdout": "c"}
    assert hso["additionalContext"] == "<surfaced-memories>m</surfaced-memories>"


def test_build_hook_output_each_half_alone():
    assert _build_hook_output({"stdout": "c"}, None)["hookSpecificOutput"].keys() == {
        "hookEventName",
        "updatedToolOutput",
    }
    assert _build_hook_output(None, "ctx")["hookSpecificOutput"].keys() == {
        "hookEventName",
        "additionalContext",
    }
    assert _build_hook_output(None, None) == {}


# ── _orchestrate: compression + surfacing merge, independent gates ───────────


def test_orchestrate_merges_compression_and_surfacing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__MAX_CHARS", "2000")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "0")  # cold path → run_surfacing_hook
    surfaced = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "<surfaced-memories>m</surfaced-memories>",
        }
    }
    monkeypatch.setattr(
        "memtomem_stm.cli.hook_cmd.run_surfacing_hook", AsyncMock(return_value=surfaced)
    )
    out = asyncio.run(_orchestrate(_bash_payload({"stdout": _BIG_STDOUT}), ClaudeHookAdapter()))
    hso = out["hookSpecificOutput"]
    assert hso["updatedToolOutput"]["stdout"].startswith(_COMPRESS_SENTINEL)
    assert hso["additionalContext"] == "<surfaced-memories>m</surfaced-memories>"


def test_orchestrate_compression_runs_when_surfacing_yields_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    # Independent gate: surfacing produced {} (disabled / no hits) yet the Bash
    # output is still compressed.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__MAX_CHARS", "2000")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "0")
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd.run_surfacing_hook", AsyncMock(return_value={}))
    out = asyncio.run(_orchestrate(_bash_payload({"stdout": _BIG_STDOUT}), ClaudeHookAdapter()))
    hso = out["hookSpecificOutput"]
    assert hso["updatedToolOutput"]["stdout"].startswith(_COMPRESS_SENTINEL)
    assert "additionalContext" not in hso


def test_orchestrate_honors_supplied_adapter_capability(monkeypatch: pytest.MonkeyPatch):
    # _orchestrate must use the SUPPLIED adapter, not re-resolve Claude internally:
    # a can_replace_output=False adapter (Codex) skips compression even with the
    # same compressible Bash payload + compression env that Claude DOES compress
    # (test_orchestrate_merges_*). Pins this PR's seam against re-internalizing
    # get_adapter() — a mutation re-adding it would compress here and fail.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__MAX_CHARS", "2000")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "0")
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd.run_surfacing_hook", AsyncMock(return_value={}))
    # Codex maps Bash→shell (so it reaches the compression gate) but
    # can_replace_output is False → compression is skipped, output passes through.
    out = asyncio.run(_orchestrate(_bash_payload({"stdout": _BIG_STDOUT}), CodexHookAdapter()))
    assert out == {}  # no updatedToolOutput — the supplied adapter's capability won


@pytest.mark.parametrize("adapter", [CursorHookAdapter(), KimiHookAdapter()])
def test_metrics_only_hosts_skip_surfacing(adapter, monkeypatch: pytest.MonkeyPatch):
    run = AsyncMock(return_value={})
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._run_hook", run)
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_response": {"content": _BIG_STDOUT},
        "tool_output": {"content": _BIG_STDOUT},
    }
    asyncio.run(_orchestrate(payload, adapter))
    run.assert_not_awaited()

    # Positive control: the identical, surface-eligible payload reaches
    # surfacing on a host that can inject context. This proves the assertion
    # above is pinned to the capability gate rather than an ineligible payload.
    asyncio.run(_orchestrate(payload, ClaudeHookAdapter()))
    run.assert_awaited_once()


def test_orchestrate_keeps_compression_when_surfacing_raises(monkeypatch: pytest.MonkeyPatch):
    # A surfacing failure must NOT discard the already-computed compression half
    # (Codex Major — the never-raises/independence contract).
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__MAX_CHARS", "2000")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "0")
    monkeypatch.setattr(
        "memtomem_stm.cli.hook_cmd.run_surfacing_hook",
        AsyncMock(side_effect=RuntimeError("surfacing exploded")),
    )
    out = asyncio.run(_orchestrate(_bash_payload({"stdout": _BIG_STDOUT}), ClaudeHookAdapter()))
    hso = out["hookSpecificOutput"]
    assert hso["updatedToolOutput"]["stdout"].startswith(_COMPRESS_SENTINEL)
    assert "additionalContext" not in hso


def test_cli_compresses_bash_output_end_to_end(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__MAX_CHARS", "2000")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "0")
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd.run_surfacing_hook", AsyncMock(return_value={}))
    result = CliRunner().invoke(
        cli, ["hook", "--host", "claude"], input=json.dumps(_bash_payload({"stdout": _BIG_STDOUT}))
    )
    assert result.exit_code == 0
    hso = json.loads(result.output)["hookSpecificOutput"]
    assert hso["updatedToolOutput"]["stdout"].startswith(_COMPRESS_SENTINEL)


def test_cli_compression_default_off_is_passthrough(monkeypatch: pytest.MonkeyPatch):
    # Opt-in: with the flag unset, a large Bash output is NOT replaced.
    monkeypatch.delenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", raising=False)
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "0")
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd.run_surfacing_hook", AsyncMock(return_value={}))
    result = CliRunner().invoke(
        cli, ["hook"], input=json.dumps(_bash_payload({"stdout": _BIG_STDOUT}))
    )
    assert result.exit_code == 0
    assert json.loads(result.output) == {}
