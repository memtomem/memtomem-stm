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
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
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
    HOST_NOT_EXPLICIT_NOTICE,
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
from memtomem_stm.daemon.protocol import OP_SURFACE, PROTOCOL_VERSION
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
    # Bash (legacy native → canonical shell) is accepted. A description gives
    # the native-tool extractor a query: ``seq 1 4000`` alone is one token.
    payload = _bash_payload({"stdout": _LONG})
    payload["tool_input"] = {**payload["tool_input"], "description": "Print the load test numbers"}
    out = await run_surfacing_hook(_canonical(payload), engine=engine)
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


_HOST_IDS = {
    "session_id": "sess-1",
    "cwd": "/work",
    "tool_use_id": "toolu-1",
    "agent_id": "agent-1",
}


async def test_run_hook_passes_host_ids_to_engine():
    # Engine path (the daemon hands its warm engine in): the host's call ids
    # parsed off the payload reach ``surface`` as keyword arguments.
    spy = AsyncMock()
    spy.surface = AsyncMock(return_value="orig")
    spy.injection_mode = "append"
    await run_surfacing_hook(_canonical({**_READ_PAYLOAD, **_HOST_IDS}), engine=spy)
    kwargs = spy.surface.await_args.kwargs
    assert {k: kwargs[k] for k in _HOST_IDS} == _HOST_IDS


async def test_cold_path_passes_host_ids_to_engine(monkeypatch: pytest.MonkeyPatch):
    # Cold in-process path: the engine is built here, not handed in, so it is a
    # second call site that could drop the ids.
    built: list[AsyncMock] = []

    def fake_engine(*args, **kwargs):
        engine = AsyncMock()
        engine.surface = AsyncMock(return_value="orig")
        built.append(engine)
        return engine

    monkeypatch.setattr("memtomem_stm.surfacing.engine.SurfacingEngine", fake_engine)
    monkeypatch.setattr(
        "memtomem_stm.surfacing.mcp_client.McpClientSearchAdapter",
        lambda *args, **kwargs: AsyncMock(),
    )
    await run_surfacing_hook(_canonical({**_READ_PAYLOAD, **_HOST_IDS}))
    assert len(built) == 1
    kwargs = built[0].surface.await_args.kwargs
    assert {k: kwargs[k] for k in _HOST_IDS} == _HOST_IDS


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


# ── Always-exit-0 barrier around the whole bridge body (#865) ────────────────


def test_cli_barrier_catches_pre_orchestrate_failure(monkeypatch: pytest.MonkeyPatch):
    # Host resolution runs BEFORE the inner asyncio barrier; a crash there must
    # not become a non-zero exit the host reads as a hook failure — it degrades
    # to the standard pass-through "{}".
    monkeypatch.setattr(
        "memtomem_stm.cli.hook_cmd._resolve_host_tag",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("resolver exploded")),
    )
    result = CliRunner().invoke(cli, ["hook"], input=json.dumps(_READ_PAYLOAD))
    assert result.exit_code == 0
    assert json.loads(result.output) == {}


def test_cli_barrier_catches_flag_validation_failure(monkeypatch: pytest.MonkeyPatch):
    # ``runtime_env_overrides`` runs before an adapter is even resolved; the
    # barrier's fallback then has no adapter and must still emit "{}" / exit 0.
    monkeypatch.setattr(
        "memtomem_stm.cli.hook_cmd.runtime_env_overrides",
        lambda **k: (_ for _ in ()).throw(ValueError("flag validation exploded")),
    )
    result = CliRunner().invoke(cli, ["hook"], input=json.dumps(_READ_PAYLOAD))
    assert result.exit_code == 0
    assert json.loads(result.output) == {}


def test_cli_barrier_catches_systemexit_from_orchestrate(monkeypatch: pytest.MonkeyPatch):
    # The inner barrier catches ``Exception`` only; a ``SystemExit`` escaping
    # ``_orchestrate`` previously became the process exit code. The outer
    # barrier must swallow it (always-exit-0 contract) and pass through.
    monkeypatch.setattr(
        "memtomem_stm.cli.hook_cmd._orchestrate", AsyncMock(side_effect=SystemExit(3))
    )
    result = CliRunner().invoke(cli, ["hook", "--host", "claude"], input=json.dumps(_READ_PAYLOAD))
    assert result.exit_code == 0
    assert json.loads(result.output) == {}


def test_cli_barrier_serialize_failure_degrades_to_nothing(monkeypatch: pytest.MonkeyPatch):
    # ``adapter.serialize`` is outside the inner barrier. If it raises — and the
    # fallback re-serialize of {} raises too — the barrier still exits 0 with
    # empty stdout (safe for every host) rather than crashing the hook.
    class _BrokenAdapter(ClaudeHookAdapter):
        def serialize(self, output):  # type: ignore[override]
            raise RuntimeError("serialize exploded")

    monkeypatch.setattr(
        "memtomem_stm.cli.hook_cmd.get_adapter", lambda *a, **k: _BrokenAdapter()
    )
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._orchestrate", AsyncMock(return_value={}))
    result = CliRunner().invoke(cli, ["hook"], input=json.dumps(_READ_PAYLOAD))
    assert result.exit_code == 0
    assert result.output == ""


def test_cli_barrier_pre_adapter_failure_honors_explicit_kimi_host(
    monkeypatch: pytest.MonkeyPatch,
):
    # A failure BEFORE adapter resolution must still respect an explicitly
    # selected host's channel shape: with --host kimi the pass-through is raw
    # stdout, so the fallback must be truly empty — a literal "{}" would be
    # injected into the model context.
    monkeypatch.setattr(
        "memtomem_stm.cli.hook_cmd.runtime_env_overrides",
        lambda **k: (_ for _ in ()).throw(ValueError("flag validation exploded")),
    )
    result = CliRunner().invoke(cli, ["hook", "--host", "kimi"], input=json.dumps(_READ_PAYLOAD))
    assert result.exit_code == 0
    assert result.output == ""


def test_cli_barrier_resolver_failure_auto_detects_kimi_payload(
    monkeypatch: pytest.MonkeyPatch,
):
    # Under the default --host auto, a resolver crash must not discard what the
    # already-read payload says about the host: a Kimi-shaped payload still
    # routes the fallback through payload-shape detection, so the raw-stdout
    # channel stays truly empty instead of getting a Claude-shaped "{}".
    monkeypatch.setattr(
        "memtomem_stm.cli.hook_cmd._resolve_host_tag",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("resolver exploded")),
    )
    result = CliRunner().invoke(cli, ["hook"], input=json.dumps(_KIMI_SHELL_PAYLOAD))
    assert result.exit_code == 0
    assert result.output == ""


def test_cli_barrier_fallback_baseexception_still_exits_zero(monkeypatch: pytest.MonkeyPatch):
    # The recovery path must not itself violate the contract: when the primary
    # ``serialize`` raises a ``BaseException`` (not just ``Exception``) and the
    # fallback re-serialize of {} raises the same way, the barrier still exits 0
    # with empty stdout instead of letting the escape become the process exit.
    class _FatalAdapter(ClaudeHookAdapter):
        def serialize(self, output):  # type: ignore[override]
            raise SystemExit(3)

    monkeypatch.setattr("memtomem_stm.cli.hook_cmd.get_adapter", lambda *a, **k: _FatalAdapter())
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._orchestrate", AsyncMock(return_value={}))
    result = CliRunner().invoke(cli, ["hook"], input=json.dumps(_READ_PAYLOAD))
    assert result.exit_code == 0
    assert result.output == ""


def test_cli_barrier_kimi_fallback_is_empty_stdout(monkeypatch: pytest.MonkeyPatch):
    # When the failing call had already resolved the Kimi adapter, the barrier's
    # fallback goes through ``adapter.serialize({})`` — which for Kimi's raw
    # stdout channel is "" — so nothing is injected into the model context.
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd.get_adapter", lambda *a, **k: KimiHookAdapter())
    monkeypatch.setattr(
        "memtomem_stm.cli.hook_cmd._orchestrate", AsyncMock(side_effect=SystemExit(3))
    )
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


@pytest.mark.parametrize(
    ("status", "turned_away"),
    [("busy", True), ("expired", True), ("unavailable", False)],
)
def test_daemon_turning_a_call_away_is_a_skip_not_an_outage(
    monkeypatch: pytest.MonkeyPatch, status: str, turned_away: bool
):
    # A daemon that answers busy or expired is alive. Treating it as
    # unreachable asked for a spawn and, with fallback=cold, redid the search
    # in-process -- more load at the moment the daemon reported too much
    # (#1198). ``unavailable`` keeps the outage path as the positive control.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__FALLBACK", "cold")
    monkeypatch.delenv("MEMTOMEM_STM_HOOK__AUTO_SPAWN", raising=False)
    sent: list[str] = []

    async def _request(hs, op, payload, **kwargs):
        sent.append(op)
        return {"v": PROTOCOL_VERSION, "ok": False, "status": status}

    monkeypatch.setattr(
        "memtomem_stm.daemon.client._live_handshake_candidate", lambda config: {"token": "t"}
    )
    monkeypatch.setattr("memtomem_stm.daemon.client._request", _request)
    spawns: list[int] = []
    monkeypatch.setattr("memtomem_stm.daemon.spawn.request_spawn", lambda cfg: spawns.append(1))
    sentinel = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "cold"}}
    cold = AsyncMock(return_value=sentinel)
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd.run_surfacing_hook", cold)

    out = asyncio.run(_run_hook(_canonical(_READ_PAYLOAD)))

    assert sent == [OP_SURFACE]
    if turned_away:
        assert out == {}
        assert spawns == []
        cold.assert_not_called()
    else:
        assert out == sentinel
        assert spawns == [1]
        cold.assert_awaited_once()


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
    _record_hook_metrics(
        _canonical(_bash_payload({"stdout": _BIG_STDOUT})), updated, _BLOCK, cfg, "claude"
    )

    summary = read_compression_summary(tmp_path / "metrics.db", source="hook")
    assert summary["available"] is True
    assert summary["total_calls"] == 1
    row = summary["by_tool"][0]
    assert (row["server"], row["tool"]) == ("builtin:claude", "Bash")
    assert row["original_chars"] == len(_BIG_STDOUT)
    assert row["compressed_chars"] == len(f"{_COMPRESS_SENTINEL}\nshort")


def test_record_hook_metrics_does_not_reconcile_foreign_sources(tmp_path: Path):
    """The hook's cap comes from an env-only STMConfig, not the proxy JSON —
    a hook invocation must never apply it to OTHER sources' retention
    (codex review round 4: with a file-configured cap larger than the env
    default, every hook call would have shrunk the mcp history)."""
    import sqlite3
    import time as _time

    from memtomem_stm.proxy.metrics_store import MetricsStore

    # Seed 15 mcp rows via a store whose (authoritative) cap allows them.
    seed = MetricsStore(tmp_path / "metrics.db", max_history=100)
    seed.initialize()
    try:
        now = _time.time()
        seed._db.executemany(
            "INSERT INTO proxy_metrics (server, tool, original_chars, "
            "compressed_chars, cleaned_chars, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'mcp', ?)",
            [("srv", "t", 1, 1, 1, now) for _ in range(15)],
        )
        seed._db.commit()
    finally:
        seed.close()

    # Hook invocation with a smaller (env-default-like) cap of 10.
    cfg = _metrics_config(tmp_path)
    cfg.proxy.metrics.max_history = 10
    _record_hook_metrics(
        _canonical(_bash_payload({"stdout": _BIG_STDOUT})), None, None, cfg, "claude"
    )

    db = sqlite3.connect(str(tmp_path / "metrics.db"))
    try:
        counts = dict(
            db.execute("SELECT source, COUNT(*) FROM proxy_metrics GROUP BY source").fetchall()
        )
    finally:
        db.close()
    assert counts == {"mcp": 15, "hook": 1}


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
    _record_hook_metrics(_canonical(payload), None, None, cfg, "claude")

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
    assert (row["server"], row["tool"]) == ("builtin:claude", "Bash")
    assert row["original_chars"] == len(_BIG_STDOUT)
    assert row["compressed_chars"] == len(_BIG_STDOUT)
    assert not (home / ".memtomem" / "proxy_metrics.db").exists()


_HOOK_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "hooks"


@pytest.mark.parametrize(
    ("adapter", "fixture"),
    [
        (ClaudeHookAdapter(), "claude/inbound_read_posttooluse.json"),
        (CodexHookAdapter(), "codex/inbound_bash_posttooluse.json"),
        (CursorHookAdapter(), "cursor/inbound_shell_posttooluse.json"),
        (KimiHookAdapter(), "kimi/inbound_shell_posttooluse.json"),
    ],
    ids=lambda value: value.host_tag if hasattr(value, "host_tag") else None,
)
def test_orchestrate_records_the_host_in_the_metrics_server(
    adapter,
    fixture: str,
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_hook_state: tuple[Path, Path],
):
    """Each host's rows are told apart by ``server`` (#1438).

    A host that cannot replace output can never shrink a row, so without the
    host on the row its volume is indistinguishable from compressible output
    that was left alone. Driven through ``_orchestrate`` with each host's golden
    payload: the tag has to come from the adapter that parsed the call.
    """
    import sqlite3

    metrics_db, _home = _hermetic_hook_state
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__METRICS_ENABLED", "1")
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._run_hook", AsyncMock(return_value={}))
    payload = json.loads((_HOOK_FIXTURES / fixture).read_text(encoding="utf-8"))

    asyncio.run(_orchestrate(payload, adapter))

    db = sqlite3.connect(str(metrics_db))
    try:
        rows = db.execute("SELECT server, source FROM proxy_metrics").fetchall()
    finally:
        db.close()
    assert rows == [(f"builtin:{adapter.host_tag}", "hook")]


@pytest.mark.parametrize(
    ("argv", "fixture", "recorded"),
    [
        (["hook"], "claude/inbound_read_posttooluse.json", "builtin:claude"),
        (["hook", "--host", "claude"], "claude/inbound_read_posttooluse.json", "builtin:claude"),
        (["hook", "--host", "codex"], "codex/inbound_bash_posttooluse.json", "builtin:codex"),
        # A Claude-shaped payload (no ``turn_id``) auto-detects as Claude, so
        # only an honored ``--host`` can record it as Codex.
        (["hook", "--host", "codex"], "claude/inbound_read_posttooluse.json", "builtin:codex"),
        (["hook"], "kimi/inbound_shell_posttooluse.json", "builtin:kimi"),
        # An unrecognized --host falls open to auto-detect; the row follows
        # the host that detection picked, not the typo.
        (["hook", "--host", "nope"], "kimi/inbound_shell_posttooluse.json", "builtin:kimi"),
    ],
    ids=[
        "bare",
        "explicit-claude",
        "explicit-codex",
        "explicit-codex-overrides-detection",
        "auto-detected-kimi",
        "unknown-host",
    ],
)
def test_cli_records_the_resolved_host(
    argv: list[str],
    fixture: str,
    recorded: str,
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_hook_state: tuple[Path, Path],
):
    """End to end through the CLI: the row names the host ``--host`` resolved to.

    The ``_orchestrate`` tests above hand the adapter in, so they cannot catch a
    wrong host resolution on the way there.
    """
    import sqlite3

    metrics_db, _home = _hermetic_hook_state
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__METRICS_ENABLED", "1")
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._run_hook", AsyncMock(return_value={}))
    payload = (_HOOK_FIXTURES / fixture).read_text(encoding="utf-8")

    result = CliRunner().invoke(cli, argv, input=payload)

    assert result.exit_code == 0, result.output
    db = sqlite3.connect(str(metrics_db))
    try:
        rows = db.execute("SELECT server FROM proxy_metrics").fetchall()
    finally:
        db.close()
    assert rows == [(recorded,)]


def test_bare_and_explicit_claude_record_the_same_host(
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_hook_state: tuple[Path, Path],
):
    """The host on the row is who ran the tool, not whether replacement was allowed.

    A bare ``mms hook`` resolves to the Claude adapter but refuses to replace
    output (#1437), so its rows carry the same ``builtin:claude`` as an explicit
    ``--host claude`` and the label alone cannot say which registration wrote
    them.
    """
    import sqlite3

    metrics_db, _home = _hermetic_hook_state
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__METRICS_ENABLED", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__MAX_CHARS", "2000")
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._run_hook", AsyncMock(return_value={}))
    payload = _bash_payload({"stdout": _BIG_STDOUT})

    refused = asyncio.run(
        _orchestrate(payload, ClaudeHookAdapter(), allow_output_replacement=False)
    )
    allowed = asyncio.run(_orchestrate(payload, ClaudeHookAdapter(), allow_output_replacement=True))

    assert refused == {"systemMessage": HOST_NOT_EXPLICIT_NOTICE}
    assert "updatedToolOutput" in json.dumps(allowed)
    db = sqlite3.connect(str(metrics_db))
    try:
        rows = db.execute(
            "SELECT server, compressed_chars < original_chars FROM proxy_metrics ORDER BY id"
        ).fetchall()
    finally:
        db.close()
    assert rows == [("builtin:claude", 0), ("builtin:claude", 1)]


def test_orchestrate_metrics_count_preserved_stderr_in_the_compressed_size(
    monkeypatch: pytest.MonkeyPatch,
    _hermetic_hook_state: tuple[Path, Path],
):
    """Only stdout is compressed; stderr survives in the replacement. Its size
    has to count on both sides, or the row records savings the call never had."""
    from memtomem_stm.config import STMConfig
    from memtomem_stm.proxy.metrics_store import read_compression_summary

    metrics_db, _home = _hermetic_hook_state
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__METRICS_ENABLED", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__MAX_CHARS", "2000")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__MIN_RETENTION", "0")
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._run_hook", AsyncMock(return_value={}))
    resp = {"stdout": _BIG_STDOUT, "stderr": "w" * 2000}
    replaced = maybe_compress_builtin(_bash_call(resp), STMConfig().hook.compression)
    assert isinstance(replaced, dict) and replaced["stderr"] == resp["stderr"]

    asyncio.run(_orchestrate(_bash_payload(resp), ClaudeHookAdapter()))

    row = read_compression_summary(metrics_db, source="hook")["by_tool"][0]
    assert row["original_chars"] == len(_tool_response_to_text(resp))
    assert row["compressed_chars"] == len(_tool_response_to_text(replaced))


def test_record_hook_metrics_disabled_writes_nothing(tmp_path: Path):
    cfg = _metrics_config(tmp_path, enabled=False)
    _record_hook_metrics(
        _canonical(_bash_payload({"stdout": _BIG_STDOUT})), None, None, cfg, "claude"
    )
    assert not (tmp_path / "metrics.db").exists()  # store never opened


def test_record_hook_metrics_skips_empty_output(tmp_path: Path):
    # A result that flattens to nothing carries no spend to record.
    cfg = _metrics_config(tmp_path)
    _record_hook_metrics(
        _canonical({"hook_event_name": "PostToolUse", "tool_name": "Bash"}),
        None,
        None,
        cfg,
        "claude",
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
    _record_hook_metrics(
        _canonical(_bash_payload({"stdout": _BIG_STDOUT})), None, None, bad, "claude"
    )


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
        _record_hook_metrics(
            _canonical(_bash_payload({"stdout": _BIG_STDOUT})), None, None, cfg, "claude"
        )
        elapsed = time.monotonic() - start
    finally:
        blocker.rollback()
        blocker.close()

    # Fast-fail: comfortably under the shared 3000 ms busy timeout (generous
    # bound to stay non-flaky in CI) and nothing persisted.
    assert elapsed < 2.0
    assert read_compression_summary(db, source="hook")["total_calls"] == 0


def test_record_hook_metrics_degrades_when_the_rollup_install_meets_a_lock(tmp_path: Path):
    """First hook run against a pre-#1316 DB takes the rollup install path, whose
    ``BEGIN IMMEDIATE`` must honour the hook's short busy timeout too: no row,
    no raise, no stall — and the next run installs."""
    import sqlite3
    import time

    from memtomem_stm.proxy.metrics_store import MetricsStore, read_compression_all_time

    db = tmp_path / "metrics.db"
    seed = MetricsStore(db)
    seed.initialize()
    seed.close()
    raw = sqlite3.connect(str(db))
    raw.execute("DROP TRIGGER proxy_metrics_rollup_on_delete_v1")
    raw.execute("DROP TABLE proxy_metrics_daily")
    raw.execute("DELETE FROM metrics_meta")
    raw.commit()
    raw.close()

    cfg = _metrics_config(tmp_path)
    payload = _canonical(_bash_payload({"stdout": _BIG_STDOUT}))
    blocker = sqlite3.connect(str(db), timeout=0.1)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        start = time.monotonic()
        _record_hook_metrics(payload, None, None, cfg, "claude")
        elapsed = time.monotonic() - start
    finally:
        blocker.rollback()
        blocker.close()

    assert elapsed < 2.0
    assert read_compression_all_time(db)["rollup"]["installed"] is False

    _record_hook_metrics(payload, None, None, cfg, "claude")
    after = read_compression_all_time(db, source="hook")
    assert after["rollup"]["installed"] is True
    assert after["total_calls"] == 1


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


class TestEmitHookChangeDisplayEscaping:
    """``_emit_hook_change`` renders the host-config path it is about to
    rewrite. That path is filesystem-derived (it hangs off ``HOME``), and a
    directory name can hold anything the filesystem allows — including the
    characters that rewrite the rendered line (#760).

    ``change.notes``, ``label``, ``action`` and ``host_tag`` are literals this
    package writes itself and deliberately stay raw.
    """

    HOSTILE = "ev\x1b[31m\ril"

    def _change(self, tmp_path: Path):
        @dataclass
        class _Change:
            path: Path
            label: str = "Claude Code"
            status: str = "create"
            action: str = "install"
            changed: bool = True
            fmt: str = "toml"
            rendered_block: str = "[[hooks]]"
            notes: tuple = ()
            host_tag: str = "claude"

        return _Change(path=tmp_path / f"cfg{self.HOSTILE}" / "settings.toml")

    def test_plan_header_and_toml_note_escape_the_path(self, tmp_path, capsys):
        from memtomem_stm.cli.hook_cmd import _emit_hook_change

        _emit_hook_change(self._change(tmp_path), apply_=False, backup=None)
        out = capsys.readouterr().out

        # The header renders the full path; the TOML note renders `path.name`.
        assert "\\u001B" in out and "\\u000D" in out
        assert "applying rewrites" in out
        assert "\x1b" not in out
        assert "\r" not in out

    def test_applied_backup_path_is_escaped(self, tmp_path, capsys):
        from memtomem_stm.cli.hook_cmd import _emit_hook_change

        backup = tmp_path / f"cfg{self.HOSTILE}" / "settings.toml.bak"
        _emit_hook_change(self._change(tmp_path), apply_=True, backup=backup)
        out = capsys.readouterr().out

        assert "Backed up" in out
        assert "\x1b" not in out
        assert "\r" not in out


class TestHookPreviewAndErrorsEscape:
    """``rendered_block`` and every ``HookInstallError`` reach the terminal (#780).

    The block embeds the source-checkout command, so a path with a hostile
    character renders in all four previews. The errors interpolate a config
    path and a parser's own text; they are escaped at the two ``ClickException``
    sites rather than at each raise, so future messages are covered too.
    """

    HOSTILE = "ho\x1b[31m\rst"

    def test_preview_keeps_its_line_structure(self, tmp_path, monkeypatch, capsys):
        """Escaping must not flatten the preview (#780).

        ``_disp`` escapes the newline itself, so escaping the whole block
        before ``splitlines()`` collapsed a real multi-line preview into one
        line of ``\\u000A``. Driven off an actual ``plan_install`` rather than a
        synthetic one-line block, which is what let the first version of this
        test miss the regression.
        """
        from memtomem_stm.cli import hook_cmd, hook_hosts

        # ``CLAUDE_CONFIG_DIR``, not ``HOME``: ``_config_path`` prefers that
        # override, and its fallback is ``expanduser()``, which reads
        # ``USERPROFILE`` on Windows. Patching ``HOME`` alone leaves this
        # reading the developer's real settings on Windows, and on any
        # platform where ``CLAUDE_CONFIG_DIR`` is exported — where an already
        # installed hook makes the plan unchanged and skips the preview.
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        change = hook_hosts.plan_install("claude", "uv run memtomem-stm hook")
        expected = len(change.rendered_block.splitlines())
        assert expected > 1, "fixture must be multi-line to pin anything"

        hook_cmd._emit_hook_change(change, apply_=False, backup=None)
        out = capsys.readouterr().out

        rendered = [line for line in out.splitlines() if line.startswith("    ")]
        assert len(rendered) == expected
        assert "\\u000A" not in out

    def test_preview_escapes_the_rendered_block(self, capsys):
        from memtomem_stm.cli import hook_cmd
        from memtomem_stm.cli.hook_hosts import HookChange

        change = HookChange(
            host_tag="claude",
            label="Claude Code",
            path=Path("/tmp/settings.json"),
            fmt="json",
            action="install",
            status="create",
            changed=True,
            current_text=None,
            new_text="{}",
            rendered_block=f'{{"command": "{self.HOSTILE}"}}',
            notes=(),
        )

        hook_cmd._emit_hook_change(change, apply_=False, backup=None)

        out = capsys.readouterr().out
        assert "\x1b" not in out and "\r" not in out
        assert "\\u001B" in out and "\\u000D" in out

    @pytest.mark.parametrize(
        ("subcommand", "planner"), [("install", "plan_install"), ("uninstall", "plan_uninstall")]
    )
    def test_error_is_escaped_at_both_click_boundaries(
        self, tmp_path, monkeypatch, subcommand: str, planner: str
    ):
        """Every ``HookInstallError`` becomes terminal text at a choke point.

        Both boundaries are exercised: reverting either one alone survived a
        test that covered only ``install``.
        """
        from click.testing import CliRunner

        from memtomem_stm.cli import hook_cmd, hook_hosts
        from memtomem_stm.cli.hook_hosts import HookInstallError

        def boom(*a, **k):
            raise HookInstallError(f"cannot read /tmp/{self.HOSTILE}/settings.json")

        # ``hook_cmd`` imports ``hook_hosts`` inside the command body, so the
        # patch has to land on the source module, not on a module attribute.
        monkeypatch.setattr(hook_hosts, planner, boom)
        res = CliRunner().invoke(hook_cmd.hook_command, [subcommand, "--host", "claude"])

        assert res.exit_code != 0
        assert "\x1b" not in res.output and "\r" not in res.output
        assert "\\u001B" in res.output and "\\u000D" in res.output


def test_cli_broken_proxy_env_passes_through_and_logs_the_hint(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """#847 observability: a proxy-subtree env break still degrades to `{}`
    exit 0 (the always-exit-0 contract), but no longer silently — the log
    names the implicated MEMTOMEM_STM_* var so the degradation is
    attributable."""
    import logging

    monkeypatch.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND", "x")
    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(cli, ["hook"], input=json.dumps(_READ_PAYLOAD))

    assert result.exit_code == 0
    assert json.loads(result.output) == {}
    assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND" in caplog.text
    # Logged exactly once each (#847 review round 1): one hint line (no
    # traceback of its own) and one barrier warning that owns the traceback —
    # not two traceback blocks per tool call.
    hint_records = [
        r for r in caplog.records if "invalid MEMTOMEM_STM_* configuration" in r.getMessage()
    ]
    assert len(hint_records) == 1
    assert not hint_records[0].exc_info
    traceback_records = [r for r in caplog.records if r.exc_info]
    assert len(traceback_records) == 1


# ── #1437: a refused replacement is reported, not silent ─────────────────────


def _refused_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__METRICS_ENABLED", "0")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__MAX_CHARS", "2000")
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._run_hook", AsyncMock(return_value={}))


def _compressor_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every ``TruncateCompressor.compress`` call (by input length)."""
    from memtomem_stm.proxy.compression import TruncateCompressor

    calls: list[int] = []
    real = TruncateCompressor.compress

    def spy(self, text, *args, **kwargs):
        calls.append(len(text))
        return real(self, text, *args, **kwargs)

    monkeypatch.setattr(TruncateCompressor, "compress", spy)
    return calls


def test_refused_replacement_reports_notice_without_running_the_compressor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    _refused_env(monkeypatch)
    calls = _compressor_calls(monkeypatch)
    payload = _bash_payload({"stdout": _BIG_STDOUT})

    with caplog.at_level(logging.WARNING, logger="memtomem_stm.cli.hook_cmd"):
        out = asyncio.run(
            _orchestrate(payload, ClaudeHookAdapter(), allow_output_replacement=False)
        )

    assert out == {"systemMessage": HOST_NOT_EXPLICIT_NOTICE}
    assert "--host claude" in HOST_NOT_EXPLICIT_NOTICE
    assert "add --host claude to the hook command" in HOST_NOT_EXPLICIT_NOTICE
    assert "mms hook install" not in HOST_NOT_EXPLICIT_NOTICE
    assert calls == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [HOST_NOT_EXPLICIT_NOTICE]


def test_refused_notice_keeps_the_surfaced_context(monkeypatch: pytest.MonkeyPatch):
    _refused_env(monkeypatch)
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._run_hook", AsyncMock(return_value=_SURF_DICT))
    payload = _bash_payload({"stdout": _BIG_STDOUT})

    out = asyncio.run(_orchestrate(payload, ClaudeHookAdapter(), allow_output_replacement=False))

    assert out == {
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": _SURF_BLOCK},
        "systemMessage": HOST_NOT_EXPLICIT_NOTICE,
    }
    serialized = json.loads(ClaudeHookAdapter().serialize(out))
    assert serialized == out


def test_refused_notice_also_covers_output_the_retention_guard_would_keep(
    monkeypatch: pytest.MonkeyPatch,
):
    """The refused path skips the compressor, so it cannot know the retention
    guard's verdict; the notice says the attempt was not made, which holds
    either way. Pinned so a later change that starts claiming savings has to
    run the compressor first."""
    _refused_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__MIN_RETENTION", "0.99")
    payload = _bash_payload({"stdout": _BIG_STDOUT})

    allowed = asyncio.run(_orchestrate(payload, ClaudeHookAdapter(), allow_output_replacement=True))
    refused = asyncio.run(
        _orchestrate(payload, ClaudeHookAdapter(), allow_output_replacement=False)
    )

    assert allowed == {}  # retention_guard on the authorized path
    assert refused == {"systemMessage": HOST_NOT_EXPLICIT_NOTICE}
    assert "not attempted" in HOST_NOT_EXPLICIT_NOTICE


@pytest.mark.parametrize(
    ("env", "payload"),
    [
        pytest.param({}, _bash_payload({"stdout": "short"}), id="below_threshold"),
        pytest.param(
            {"MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED": "0"},
            _bash_payload({"stdout": _BIG_STDOUT}),
            id="compression_disabled",
        ),
        pytest.param(
            {"MEMTOMEM_STM_HOOK__COMPRESSION__MAX_CHARS": "5"},
            _bash_payload({"stdout": _BIG_STDOUT}),
            id="budget_too_small",
        ),
        pytest.param(
            {}, _bash_payload({"stdout": _BIG_STDOUT, "exitCode": 1}), id="unsafe_result"
        ),
        pytest.param(
            {}, _bash_payload({"stdout": _BIG_STDOUT}, tool="Read"), id="ineligible_tool"
        ),
        pytest.param({}, _bash_payload(_BIG_STDOUT), id="unsupported_shape"),
        pytest.param(
            {},
            _bash_payload({"stdout": f"{_COMPRESS_SENTINEL}\n{_BIG_STDOUT}"}),
            id="already_compressed",
        ),
    ],
)
def test_refused_path_stays_silent_when_compression_would_not_run(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    env: dict[str, str],
    payload: dict[str, Any],
):
    _refused_env(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with caplog.at_level(logging.WARNING, logger="memtomem_stm.cli.hook_cmd"):
        out = asyncio.run(
            _orchestrate(payload, ClaudeHookAdapter(), allow_output_replacement=False)
        )

    assert "systemMessage" not in out
    assert "updatedToolOutput" not in json.dumps(out)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_cli_bare_hook_reports_refusal_and_explicit_claude_compresses(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    # Under pytest the root logger already has handlers, so the hook's own
    # stderr handler is not installed; the record is asserted through caplog.
    _refused_env(monkeypatch)
    stdin = json.dumps(_bash_payload({"stdout": _BIG_STDOUT}))

    with caplog.at_level(logging.WARNING, logger="memtomem_stm.cli.hook_cmd"):
        bare = CliRunner().invoke(cli, ["hook"], input=stdin)
    bare_warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="memtomem_stm.cli.hook_cmd"):
        explicit = CliRunner().invoke(cli, ["hook", "--host", "claude"], input=stdin)

    assert bare.exit_code == 0
    assert json.loads(bare.stdout) == {"systemMessage": HOST_NOT_EXPLICIT_NOTICE}
    assert bare_warnings == [HOST_NOT_EXPLICIT_NOTICE]
    assert explicit.exit_code == 0
    explicit_out = json.loads(explicit.stdout)
    assert "systemMessage" not in explicit_out
    assert "updatedToolOutput" in explicit_out["hookSpecificOutput"]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("host", ["codex", "cursor", "kimi"])
def test_cli_explicit_non_replacing_hosts_get_no_refusal_notice(
    monkeypatch: pytest.MonkeyPatch, host: str
):
    """A host that cannot replace output is a capability, not a registration
    mistake: no notice, whatever the compression setting."""
    _refused_env(monkeypatch)
    stdin = json.dumps(_bash_payload({"stdout": _BIG_STDOUT}))

    result = CliRunner().invoke(cli, ["hook", "--host", host], input=stdin)

    assert result.exit_code == 0
    assert "systemMessage" not in result.stdout
    assert HOST_NOT_EXPLICIT_NOTICE not in result.stderr


def test_cli_auto_detected_kimi_gets_no_refusal_notice(monkeypatch: pytest.MonkeyPatch):
    _refused_env(monkeypatch)
    payload = {**_KIMI_SHELL_PAYLOAD, "tool_output": _BIG_STDOUT}

    result = CliRunner().invoke(cli, ["hook"], input=json.dumps(payload))

    assert result.exit_code == 0
    assert result.stdout == ""
    assert HOST_NOT_EXPLICIT_NOTICE not in result.stderr


_CURSOR_BIG_SHELL_PAYLOAD = {
    "hook_event_name": "postToolUse",
    "tool_name": "Shell",
    "tool_input": {"command": "seq 1 4000"},
    "tool_output": _BIG_STDOUT,
}


@pytest.mark.parametrize("argv", [["hook"], ["hook", "--host", "cursor"]], ids=["auto", "explicit"])
def test_cli_cursor_shell_payload_gets_no_refusal_notice(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, argv: list[str]
):
    """Outcome pin, not a pin of the capability gate: with the gate removed
    this still passes, because the compression gates stop Cursor's call first.
    The adapter normalizes the event to ``PostToolUse``, but Cursor's
    ``tool_output`` is a string, not a structured result, so the call ends at
    ``unsupported_shape``. The Kimi control is silenced the same way; the
    Codex controls are the ones that fail without the gate."""
    _refused_env(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="memtomem_stm.cli.hook_cmd"):
        result = CliRunner().invoke(cli, argv, input=json.dumps(_CURSOR_BIG_SHELL_PAYLOAD))

    assert result.exit_code == 0
    assert "systemMessage" not in result.stdout
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_cli_auto_detected_codex_gets_no_refusal_notice(monkeypatch: pytest.MonkeyPatch):
    _refused_env(monkeypatch)
    payload = {**_bash_payload({"stdout": _BIG_STDOUT}), "turn_id": "turn-1"}

    result = CliRunner().invoke(cli, ["hook"], input=json.dumps(payload))

    assert result.exit_code == 0
    assert "systemMessage" not in result.stdout


_REGISTRATIONS = [
    # (command, host, runs_bridge, inline_compression_env)
    ("mms hook", "auto", True, False),
    ("mms hook --host claude", "claude", True, False),
    ("mms hook --host=claude", "claude", True, False),
    ("mms hook --host ' claude '", " claude ", True, False),
    ("mms hook --host claude --host auto", "auto", True, False),
    ("mms hook --host", "auto", True, False),
    ("mms hook --host codex", "codex", True, False),
    ("mms hook --host claude --help", "claude", False, False),
    ("mms hook --host claude --typo", "claude", False, False),
    ("mms hook --host claude extra", "claude", False, False),
    ("mms hook --host claude --surfacing-timeout-seconds", "claude", False, False),
    (
        "uv run --directory /src/memtomem-stm memtomem-stm hook --host claude "
        "--use-daemon --surfacing-timeout-seconds 12",
        "claude",
        True,
        False,
    ),
    ("env MEMTOMEM_STM_HOOK__USE_DAEMON=0 memtomem-stm-proxy hook", "auto", True, False),
    ("env MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 mms hook", "auto", True, True),
]


@pytest.mark.parametrize(("command", "host", "runs_bridge", "inline_compression"), _REGISTRATIONS)
def test_inspect_hook_registration_reads_the_command_the_runtime_runs(
    command: str, host: str, runs_bridge: bool, inline_compression: bool
):
    from memtomem_stm.cli.hook_cmd import HookRegistration, inspect_hook_registration

    assert inspect_hook_registration(command) == HookRegistration(
        host=host, runs_bridge=runs_bridge, inline_compression_env=inline_compression
    )


@pytest.mark.parametrize(("command", "host", "runs_bridge", "inline_compression"), _REGISTRATIONS)
def test_runs_bridge_matches_what_the_runtime_does(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    host: str,
    runs_bridge: bool,
    inline_compression: bool,
):
    """Checks the table, not the function: run each registered argv and see
    whether the bridge answered (exit 0, a JSON object on stdout). The test
    above holds :func:`inspect_hook_registration` to the same table, so the
    two together pin the function to what the runtime does."""
    import shlex

    from memtomem_stm.cli import hook_hosts

    monkeypatch.setenv("MEMTOMEM_STM_HOOK__METRICS_ENABLED", "0")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "0")
    split = hook_hosts.stm_hook_args(command)
    assert split is not None
    result = CliRunner().invoke(cli, ["hook", *split[1]], input="{}")
    answered = result.exit_code == 0 and result.stdout.strip().startswith("{")
    assert answered is runs_bridge, shlex.join(split[1])


@pytest.mark.parametrize("command", ["echo hook && mms status", "mms status"])
def test_inspect_hook_registration_ignores_non_stm_commands(command: str):
    from memtomem_stm.cli.hook_cmd import inspect_hook_registration

    assert inspect_hook_registration(command) is None


def test_surfacing_timeout_keeps_the_refusal_notice(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """The refusal is known before surfacing starts, so a surfacing call that
    outlives the outer hook deadline must not erase it."""
    _refused_env(monkeypatch)

    async def stalled(*_args, **_kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._run_hook", stalled)
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._hook_budget_seconds", lambda: 0.2)
    stdin = json.dumps(_bash_payload({"stdout": _BIG_STDOUT}))

    with caplog.at_level(logging.WARNING, logger="memtomem_stm.cli.hook_cmd"):
        result = CliRunner().invoke(cli, ["hook"], input=stdin)

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"systemMessage": HOST_NOT_EXPLICIT_NOTICE}
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert messages[0] == HOST_NOT_EXPLICIT_NOTICE
    assert any("hook processing failed" in m for m in messages[1:])


def test_surfacing_timeout_without_a_refusal_still_passes_through(
    monkeypatch: pytest.MonkeyPatch,
):
    _refused_env(monkeypatch)

    async def stalled(*_args, **_kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._run_hook", stalled)
    monkeypatch.setattr("memtomem_stm.cli.hook_cmd._hook_budget_seconds", lambda: 0.2)
    stdin = json.dumps(_bash_payload({"stdout": "short"}))

    result = CliRunner().invoke(cli, ["hook"], input=stdin)

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {}


@pytest.mark.parametrize(
    ("host", "allowed"),
    [("claude", True), (" claude ", True), ("auto", False), ("codex", False), ("Claude", False)],
)
def test_host_authorizes_replacement_only_for_explicit_claude(host: str, allowed: bool):
    from memtomem_stm.cli.hook_cmd import host_authorizes_replacement

    assert host_authorizes_replacement(host) is allowed


def test_bare_hook_process_writes_the_notice_to_stderr(tmp_path: Path):
    """The stderr half, in a real process: pytest's own log handlers keep the
    hook from installing its stderr handler in-process."""
    import subprocess
    import sys

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MEMTOMEM_STM_")
    }
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "USERPROFILE": str(tmp_path / "home"),
            "MEMTOMEM_STM_DATA_DIR": str(tmp_path / "daemon"),
            "MEMTOMEM_STM_HOOK__METRICS_ENABLED": "0",
            "MEMTOMEM_STM_SURFACING__ENABLED": "0",
            "MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED": "1",
            "MEMTOMEM_STM_HOOK__COMPRESSION__MAX_CHARS": "2000",
            "MEMTOMEM_STM_HOOK__COMPRESSION__MIN_RETENTION": "0",
        }
    )
    stdin = json.dumps(_bash_payload({"stdout": _BIG_STDOUT}))
    argv = [sys.executable, "-c", "from memtomem_stm.cli.proxy import cli; cli()", "hook"]

    bare = subprocess.run(
        argv, input=stdin, capture_output=True, text=True, env=env, timeout=60, check=False
    )
    explicit = subprocess.run(
        [*argv, "--host", "claude"],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )

    assert bare.returncode == 0
    assert json.loads(bare.stdout) == {"systemMessage": HOST_NOT_EXPLICIT_NOTICE}
    assert bare.stderr.splitlines() == [HOST_NOT_EXPLICIT_NOTICE]
    assert explicit.returncode == 0
    assert "updatedToolOutput" in json.loads(explicit.stdout)["hookSpecificOutput"]
    assert HOST_NOT_EXPLICIT_NOTICE not in explicit.stderr
