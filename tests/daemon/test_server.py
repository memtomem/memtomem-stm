"""End-to-end transport + lifecycle tests for the surfacing daemon.

These never contact a *production* LTM server: the surface-hit path injects a
``SurfacingEngine`` over a mock adapter, and the real-wiring path exercises a
non-allowlisted tool so ``run_surfacing_hook`` returns ``{}`` before any LTM
RPC. The one exception is the teardown leak-sweep e2e
(``test_real_teardown_reaps_warm_ltm_child``), which deliberately warms a real
stdio MCP round trip — against the in-repo ``_fake_memtomem_server.py``, never
an installed memtomem. That keeps the suite deterministic even on a dev box
with a live LTM.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import memtomem_stm.daemon.server as daemon_server
from memtomem_stm.config import STMConfig
from memtomem_stm.daemon import client
from memtomem_stm.daemon.discovery import (
    config_fingerprint,
    handshake_path,
    is_pid_alive,
    read_handshake,
)
from memtomem_stm.daemon.protocol import (
    MAX_MESSAGE_BYTES,
    OP_PING,
    OP_SURFACE,
    PROTOCOL_VERSION,
    build_request,
    encode_line,
    read_message,
)
from memtomem_stm.daemon.server import DaemonServer
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine


@dataclass
class _Meta:
    source_file: Path = Path("/notes/test.md")
    namespace: str = "default"


@dataclass
class _Chunk:
    id: str = ""
    content: str = "remembered detail about jwt"
    metadata: _Meta | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid4())
        if self.metadata is None:
            self.metadata = _Meta()


@dataclass
class _Result:
    chunk: _Chunk
    score: float


_LONG = "JWT authentication handler. " * 50
_READ_PAYLOAD = {
    "hook_event_name": "PostToolUse",
    "tool_name": "Read",
    "tool_input": {"file_path": "/src/auth/jwt_handler.py"},
    "tool_response": {"content": _LONG},
}


def _canonical(payload: dict):
    """Parse a raw host payload into the CanonicalHookCall the hook/daemon now
    pass around (``client.surface`` serializes it onto the wire)."""
    from memtomem_stm.cli.hook_adapter import ClaudeHookAdapter

    return ClaudeHookAdapter().parse(payload)


def _config(tmp_path: Path) -> STMConfig:
    cfg = STMConfig()
    cfg.data_dir = tmp_path
    cfg.daemon.idle_timeout_seconds = 0.0
    s = cfg.surfacing
    s.feedback_db_path = tmp_path / "feedback.db"
    s.enabled = True
    s.min_response_chars = 10
    s.timeout_seconds = 5.0
    s.min_score = 0.02
    s.cooldown_seconds = 0.0
    s.max_surfacings_per_minute = 1000
    s.auto_tune_enabled = False
    s.include_session_context = False
    s.fire_webhook = False
    return cfg


def _hs_path(cfg: STMConfig) -> Path:
    """This config's keyed handshake path — what the running daemon publishes."""
    return handshake_path(cfg.data_dir, config_fingerprint(cfg))


def _lock_path(cfg: STMConfig) -> Path:
    """This config's keyed lifetime-lock path."""
    from memtomem_stm.daemon.locking import lock_path

    return lock_path(cfg.data_dir, config_fingerprint(cfg))


def _engine_with_result() -> SurfacingEngine:
    adapter = AsyncMock()
    adapter.search = AsyncMock(return_value=([_Result(_Chunk(), 0.5)], [], "ok"))
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


async def _await_handshake(cfg: STMConfig, timeout: float = 3.0) -> None:
    hp = _hs_path(cfg)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if hp.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("daemon did not publish a handshake in time")


async def _start(cfg: STMConfig, engine: SurfacingEngine | None = None):
    server = DaemonServer(cfg)
    if engine is not None:
        # Bypass the real LTM/SQLite wiring; inject a ready engine instead.
        server._build_engine = lambda: setattr(server, "_engine", engine)  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    await _await_handshake(cfg)
    return server, task


async def _stop(cfg: STMConfig, task: asyncio.Task) -> None:
    await client.shutdown(cfg)
    await asyncio.wait_for(task, timeout=5.0)


async def test_ping_reports_ready_and_cold_ltm(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        hs = await client.ping(cfg, timeout=2.0)
        assert hs is not None
        assert hs["ltm"] == "cold"  # injected engine: adapter never started
    finally:
        await _stop(cfg, task)


async def test_surface_round_trip_injects_memories(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        out = await client.surface(cfg, _canonical(_READ_PAYLOAD), timeout=3.0)
        assert out is not None
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "<surfaced-memories>" in ctx
        # Stage-1 invariant survives the daemon path: no unresolvable prompt.
        assert "surfacing_id" not in ctx
        assert "stm_surfacing_feedback" not in ctx
    finally:
        await _stop(cfg, task)


async def test_noop_surface_for_non_allowlisted_tool_real_wiring(tmp_path: Path) -> None:
    # Real _build_engine (FeedbackTracker + lazy LTM adapter). A Write tool is
    # not allowlisted, so run_surfacing_hook returns {} without touching LTM.
    cfg = _config(tmp_path)
    _, task = await _start(cfg)
    try:
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "/x"},
            "tool_response": {"content": _LONG},
        }
        out = await client.surface(cfg, _canonical(payload), timeout=3.0)
        assert out == {}
    finally:
        await _stop(cfg, task)


async def test_bad_token_is_rejected(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        hs = read_handshake(_hs_path(cfg))
        assert hs is not None
        reader, writer = await asyncio.open_connection(
            hs["host"], hs["port"], limit=MAX_MESSAGE_BYTES
        )
        writer.write(
            encode_line(
                build_request("wrong-token", OP_SURFACE, _canonical(_READ_PAYLOAD).to_wire())
            )
        )
        await writer.drain()
        # Server closes the connection without responding to an unauthenticated peer.
        data = await asyncio.wait_for(reader.read(), timeout=3.0)
        assert data == b""
        writer.close()
    finally:
        await _stop(cfg, task)


async def test_server_rejects_mismatched_protocol_version(tmp_path: Path) -> None:
    # An authenticated request carrying a wrong protocol `v` gets an explicit
    # error frame, not action on a payload shape this version may not understand.
    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        hs = read_handshake(_hs_path(cfg))
        assert hs is not None
        reader, writer = await asyncio.open_connection(
            hs["host"], hs["port"], limit=MAX_MESSAGE_BYTES
        )
        # Correct token, wrong version — craft the frame directly (build_request
        # always stamps the current PROTOCOL_VERSION).
        frame = {"v": 999, "token": hs["token"], "op": OP_PING}
        writer.write(encode_line(frame))
        await writer.drain()
        resp = await asyncio.wait_for(read_message(reader), timeout=3.0)
        assert resp["ok"] is False
        assert "version" in resp["error"]
        assert resp["v"] == PROTOCOL_VERSION
        writer.close()
    finally:
        await _stop(cfg, task)


async def test_client_rejects_mismatched_protocol_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A live v2 daemon answers, but a client built at a different protocol
    # version must discard the reply (defense beyond the fingerprint split).
    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        # Sanity: same-version ping works.
        assert await client.ping(cfg, timeout=3.0) is not None
        # Now pretend the client speaks a different protocol version. The daemon
        # still replies with its own v (== real PROTOCOL_VERSION), which the
        # client's response guard rejects → None.
        monkeypatch.setattr("memtomem_stm.daemon.client.PROTOCOL_VERSION", 999)
        assert await client.ping(cfg, timeout=3.0) is None
        assert await client.surface(cfg, _canonical(_READ_PAYLOAD), timeout=3.0) is None
    finally:
        await _stop(cfg, task)


async def test_surface_returns_none_when_daemon_absent(tmp_path: Path) -> None:
    # No daemon → client.surface returns None so the hook degrades to {}.
    cfg = _config(tmp_path)
    out = await client.surface(cfg, _canonical(_READ_PAYLOAD), timeout=0.5)
    assert out is None


async def test_oversized_wire_frame_degrades_to_none(tmp_path: Path) -> None:
    # _bounded_call caps tool_response_text, but to_wire ships tool_input
    # uncapped. If an operator adds write/edit to the surface allowlist, a
    # multi-MB tool_input (file contents / new_string) can push the frame past
    # MAX_MESSAGE_BYTES. The contract (documented in hook_cmd._SAFE_DAEMON_BUDGET)
    # is that this degrades to None — the server's readline limit drops the
    # oversized frame and the client gets no parseable reply — and never raises
    # to the host. A live daemon makes this exercise the server-side drop, not
    # just the daemon-absent path above.
    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        huge = "z" * (MAX_MESSAGE_BYTES + 4096)
        call = _canonical({**_READ_PAYLOAD, "tool_input": {"file_path": "/x", "content": huge}})
        out = await client.surface(cfg, call, timeout=3.0)
        assert out is None  # degraded, no exception raised
    finally:
        await _stop(cfg, task)


async def test_hook_run_hook_routes_to_live_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end: ``_run_hook`` (daemon enabled, env-driven config) reaches a
    # live daemon and returns its surfaced output — the full hook→client→
    # daemon→engine→back path, no LTM (engine injected).
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH", str(tmp_path / "fb.db"))
    monkeypatch.setenv("MEMTOMEM_STM_DAEMON__IDLE_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")

    from memtomem_stm.cli.hook_cmd import _run_hook

    cfg = STMConfig()  # reads the env above → data_dir == tmp_path
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        out = await _run_hook(_canonical(_READ_PAYLOAD))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "<surfaced-memories>" in ctx
    finally:
        await _stop(cfg, task)


async def test_build_engine_dedup_only_wiring(tmp_path: Path) -> None:
    # Default (record_feedback_events=False) → dedup-only: result cache off so
    # _surfaced_ids/seen_memories dedup is authoritative (F1), AutoTuner off so
    # shared feedback rows can't nudge ranking (F3), query text never persisted.
    cfg = _config(tmp_path)
    assert cfg.hook.record_feedback_events is False
    server = DaemonServer(cfg)
    server._build_engine()
    try:
        ec = server._engine._config
        assert ec.cache_ttl_seconds == 0.0
        assert ec.auto_tune_enabled is False
        assert ec.persist_query_text is False
        assert server._engine._auto_tuner is None
        assert server._engine._record_feedback_events is False
        assert server._tracker is not None  # tracker still wired for dedup
    finally:
        if server._tracker is not None:
            server._tracker.close()
        await server._adapter.stop()


async def test_handler_write_timeout_drops_stuck_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A peer that sends its request then never reads must not pin the handler
    # task for the process lifetime — the bounded drain drops the connection
    # and the finally block closes the writer.
    server = DaemonServer(_config(tmp_path))
    monkeypatch.setattr(daemon_server, "_WRITE_TIMEOUT_SECONDS", 0.1)

    reader = asyncio.StreamReader()
    reader.feed_data(encode_line(build_request(server._token, OP_PING)))
    reader.feed_eof()

    class _StuckWriter:
        closed = False

        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            await asyncio.Event().wait()  # peer never reads; buffer never drains

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    writer = _StuckWriter()
    await asyncio.wait_for(server._handle_conn(reader, writer), timeout=5.0)
    assert writer.closed


async def test_sub_second_idle_timeout_shuts_down_promptly(tmp_path: Path) -> None:
    # The idle poll used to floor at 1.0s, so idle_timeout=0.2 shut down ~1s+
    # after start (the test below only proves *eventual* shutdown). With the
    # lowered floor the daemon exits near the configured threshold.
    cfg = _config(tmp_path)
    cfg.daemon.idle_timeout_seconds = 0.2
    server = DaemonServer(cfg)
    server._build_engine = lambda: setattr(server, "_engine", _engine_with_result())  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    await _await_handshake(cfg)
    t0 = asyncio.get_running_loop().time()
    await asyncio.wait_for(task, timeout=5.0)
    elapsed = asyncio.get_running_loop().time() - t0
    # Pre-fix lower bound was the 1.0s poll floor; expected now ≈ 0.2-0.4s.
    assert elapsed < 0.9, f"idle shutdown took {elapsed:.2f}s — poll floor regressed?"


async def test_idle_shutdown_stops_daemon(tmp_path: Path) -> None:
    # With a tiny idle timeout and no requests, the daemon shuts itself down
    # and removes its handshake — no leaked warm process after a quiet session.
    cfg = _config(tmp_path)
    cfg.daemon.idle_timeout_seconds = 0.2
    server = DaemonServer(cfg)
    server._build_engine = lambda: setattr(server, "_engine", _engine_with_result())  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    await _await_handshake(cfg)
    await asyncio.wait_for(task, timeout=5.0)  # self-terminates on idle
    assert not _hs_path(cfg).exists()


async def test_hook_run_hook_skips_when_daemon_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Daemon enabled but none running, default fallback=skip → {} (no cold path),
    # AND auto-spawn (default on) kicks off a background spawn for the next call.
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")
    monkeypatch.delenv("MEMTOMEM_STM_HOOK__FALLBACK", raising=False)
    monkeypatch.delenv("MEMTOMEM_STM_HOOK__AUTO_SPAWN", raising=False)

    from memtomem_stm.daemon import spawn

    calls: list[int] = []
    monkeypatch.setattr(spawn, "request_spawn", lambda cfg: bool(calls.append(1)) or True)

    from memtomem_stm.cli.hook_cmd import _run_hook

    out = await _run_hook(_canonical(_READ_PAYLOAD))
    assert out == {}
    assert calls == [1]  # fire-and-forget spawn requested


async def test_hook_run_hook_autospawn_runs_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # request_spawn does blocking work (flock probe + fork/exec); _run_hook
    # must offload it via asyncio.to_thread so the event loop stays free
    # while the outer wait_for budget clock runs.
    import threading

    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")

    from memtomem_stm.daemon import spawn

    threads: list[bool] = []
    monkeypatch.setattr(
        spawn,
        "request_spawn",
        lambda cfg: threads.append(threading.current_thread() is not threading.main_thread()),
    )

    from memtomem_stm.cli.hook_cmd import _run_hook

    out = await _run_hook(_canonical(_READ_PAYLOAD))
    assert out == {}
    assert threads == [True]  # ran in a worker thread, not on the loop thread


async def test_hook_run_hook_no_autospawn_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AUTO_SPAWN=0 → degrade to {} without requesting a spawn.
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__AUTO_SPAWN", "0")

    from memtomem_stm.daemon import spawn

    calls: list[int] = []
    monkeypatch.setattr(spawn, "request_spawn", lambda cfg: calls.append(1))

    from memtomem_stm.cli.hook_cmd import _run_hook

    out = await _run_hook(_canonical(_READ_PAYLOAD))
    assert out == {}
    assert calls == []


async def test_hook_run_hook_autospawn_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A spawn failure must never break the hook — it still degrades to {}.
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")

    from memtomem_stm.daemon import spawn

    def _boom(cfg):
        raise RuntimeError("spawn blew up")

    monkeypatch.setattr(spawn, "request_spawn", _boom)

    from memtomem_stm.cli.hook_cmd import _run_hook

    out = await _run_hook(_canonical(_READ_PAYLOAD))
    assert out == {}


async def test_hook_run_hook_autospawn_with_fallback_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fallback=cold → request a spawn (for next call) AND still run the cold
    # in-process path for THIS call. The two are independent.
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__FALLBACK", "cold")

    import memtomem_stm.cli.hook_cmd as hook_cmd
    from memtomem_stm.daemon import spawn

    spawned: list[int] = []
    monkeypatch.setattr(spawn, "request_spawn", lambda cfg: spawned.append(1))

    cold: list = []

    async def _fake_cold(call):
        cold.append(call)
        return {"cold": True}

    monkeypatch.setattr(hook_cmd, "run_surfacing_hook", _fake_cold)

    out = await hook_cmd._run_hook(_canonical(_READ_PAYLOAD))
    assert spawned == [1]  # spawn kicked off for next call
    # Cold path ran this call with the normalized CanonicalHookCall (Read→read).
    assert len(cold) == 1 and cold[0].tool_name == "Read" and cold[0].canonical_tool == "read"
    assert out == {"cold": True}


async def test_hook_run_hook_no_autospawn_when_daemon_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A live daemon answers surface → no spawn requested (we only spawn on None).
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH", str(tmp_path / "fb.db"))
    monkeypatch.setenv("MEMTOMEM_STM_DAEMON__IDLE_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")

    from memtomem_stm.daemon import spawn

    calls: list[int] = []
    monkeypatch.setattr(spawn, "request_spawn", lambda cfg: calls.append(1))

    from memtomem_stm.cli.hook_cmd import _run_hook

    cfg = STMConfig()
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        out = await _run_hook(_canonical(_READ_PAYLOAD))
        assert "<surfaced-memories>" in out["hookSpecificOutput"]["additionalContext"]
        assert calls == []  # live daemon → no duplicate spawn
    finally:
        await _stop(cfg, task)


# ── teardown leak sweep (E-3) ─────────────────────────────────────────────────


_CANCEL_SCOPE_MSG = "Attempted to exit cancel scope in a different task than it was entered in"

_FAKE_LTM_SERVER = Path(__file__).parent.parent / "_fake_memtomem_server.py"


class _StopRaisingAdapter:
    """Adapter stub whose ``stop()`` fails like a cross-task scope exit."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def stop(self) -> None:
        raise self._exc


def _sleeping_child() -> subprocess.Popen:
    """A real direct child mimicking a leaked warm LTM process: its own
    session (like mcp's stdio child) and a sleep only a signal can end."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals/process groups")
@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError(_CANCEL_SCOPE_MSG),
        ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError(_CANCEL_SCOPE_MSG)]),
    ],
    ids=["bare", "group"],
)
async def test_teardown_kills_leaked_child_on_cancel_scope_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exc: BaseException,
) -> None:
    # The known E-3 shape: the adapter's stdio scopes were entered in a
    # connection-handler task, stop() from the serve task raises the
    # cross-task cancel-scope error (bare, or group-wrapped by anyio >= 4)
    # — teardown must warn and terminate the surviving LTM child.
    server = DaemonServer(_config(tmp_path))
    server._adapter = _StopRaisingAdapter(exc)
    child = _sleeping_child()
    try:
        monkeypatch.setattr(daemon_server, "_direct_child_pids", lambda: {child.pid})
        monkeypatch.setattr(daemon_server, "_LEAK_KILL_ESCALATE_SECONDS", 0.2)
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.daemon.server"):
            await server._teardown()
        assert child.wait(timeout=5.0) == -signal.SIGTERM
        messages = [r.getMessage() for r in caplog.records]
        assert any("cross-task cancel-scope" in m for m in messages)
        assert any("leaked LTM child" in m for m in messages)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5.0)


async def test_teardown_sweeps_on_generic_stop_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Any stop() failure leaves the unwind incomplete, so the sweep runs —
    # and kills exactly the children that survived it (before ∩ after).
    server = DaemonServer(_config(tmp_path))
    server._adapter = _StopRaisingAdapter(ValueError("boom"))
    snapshots = iter([{111, 222}, {222}])
    monkeypatch.setattr(daemon_server, "_direct_child_pids", lambda: next(snapshots))
    killed: list[set[int]] = []

    async def _record(pids: set[int]) -> None:
        killed.append(pids)

    monkeypatch.setattr(daemon_server, "_terminate_leaked_children", _record)
    await server._teardown()
    assert killed == [{222}]


async def test_teardown_clean_adapter_stop_skips_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A successful stop() already awaited the child's exit (mcp's stdio
    # shutdown sequence) — no second probe, no kill.
    server = DaemonServer(_config(tmp_path))
    server._adapter = AsyncMock()
    probes: list[int] = []
    monkeypatch.setattr(daemon_server, "_direct_child_pids", lambda: probes.append(1) or set())
    killed: list[set[int]] = []

    async def _record(pids: set[int]) -> None:
        killed.append(pids)

    monkeypatch.setattr(daemon_server, "_terminate_leaked_children", _record)
    await server._teardown()
    assert probes == [1]  # only the pre-stop snapshot
    assert killed == []


@pytest.mark.skipif(sys.platform == "win32", reason="pgrep-based probe is POSIX-only")
def test_direct_child_pids_sees_spawned_child() -> None:
    child = _sleeping_child()
    try:
        assert child.pid in daemon_server._direct_child_pids()
    finally:
        child.terminate()
        child.wait(timeout=5.0)
    # Reaped → no longer listed.
    assert child.pid not in daemon_server._direct_child_pids()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process probes")
async def test_real_teardown_reaps_warm_ltm_child(tmp_path: Path) -> None:
    # E-3 end-to-end: real engine wiring with a stdio LTM (the fake memtomem
    # server). The lazy LTM start happens inside a connection-handler task,
    # so daemon shutdown exits the transport's cancel scopes from the serve
    # task — whatever path that unwind takes, no live LTM child may survive.
    cfg = _config(tmp_path)
    cfg.surfacing.timeout_seconds = 15.0
    cfg.surfacing.ltm_mcp_command = sys.executable
    cfg.surfacing.ltm_mcp_args = [str(_FAKE_LTM_SERVER)]
    before = daemon_server._direct_child_pids()
    _, task = await _start(cfg)  # real _build_engine
    try:
        out = await client.surface(cfg, _canonical(_READ_PAYLOAD), timeout=15.0)
        assert out is not None
        ltm_children = daemon_server._direct_child_pids() - before
        assert ltm_children  # the surface call warmed a real stdio LTM child
    finally:
        await client.shutdown(cfg)
        await asyncio.wait_for(task, timeout=20.0)
    deadline = asyncio.get_running_loop().time() + 5.0
    while any(is_pid_alive(pid) for pid in ltm_children):
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"leaked LTM child process(es) after teardown: {ltm_children}")
        await asyncio.sleep(0.1)


# ── lifetime ownership lock ───────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="same-process flock contention")
async def test_second_daemon_returns_without_building_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # While daemon 1 owns the lifetime lock, a second daemon must exit 0 WITHOUT
    # building an engine (the load-bearing ordering: no orphaned warm LTM).
    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        monkeypatch.setattr(DaemonServer, "_LOCK_ACQUIRE_RETRY_SECONDS", 0.2)
        d2 = DaemonServer(cfg)
        built: list[int] = []
        d2._build_engine = lambda: built.append(1)  # type: ignore[method-assign]
        rc = await d2.serve()
        assert rc == 0
        assert built == []  # loser never warms an engine
        assert d2._handshake_written is False
    finally:
        await _stop(cfg, task)


@pytest.mark.skipif(sys.platform == "win32", reason="same-process flock contention")
async def test_daemon_holds_lock_for_lifetime(tmp_path: Path) -> None:
    from memtomem_stm.daemon.locking import single_owner_lock

    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        with single_owner_lock(_lock_path(cfg)) as got:
            assert got is False  # held by the serving daemon
    finally:
        await _stop(cfg, task)
    # Released on teardown → acquirable again.
    with single_owner_lock(_lock_path(cfg)) as got:
        assert got is True


@pytest.mark.skipif(sys.platform == "win32", reason="same-process flock contention")
async def test_lifetime_lock_retry_acquires_after_incumbent_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A late/losing daemon retries the acquire and becomes the owner the instant
    # the incumbent releases — no dead window, and it builds exactly once after.
    from memtomem_stm.daemon import locking

    cfg = _config(tmp_path)
    monkeypatch.setattr(DaemonServer, "_LOCK_ACQUIRE_RETRY_SECONDS", 3.0)

    fd = locking.open_lock_fd(_lock_path(cfg))
    assert locking.try_lock(fd) is True  # hold it like a (mock) incumbent

    server = DaemonServer(cfg)
    server._build_engine = lambda: setattr(server, "_engine", _engine_with_result())  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.2)  # daemon is now retrying the acquire
    assert not _hs_path(cfg).exists()  # hasn't built/published yet
    locking.release_lock(fd)  # incumbent leaves
    try:
        await _await_handshake(cfg)  # proves it acquired → built → published
    finally:
        await _stop(cfg, task)


# ── run() top-level exception barrier (#581) ─────────────────────────────


def test_run_logs_traceback_on_startup_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A crash inside serve() (e.g. _build_engine / start_server) must reach the
    logger — for a detached daemon its stderr is DEVNULL, so without the barrier
    the traceback would be lost and stm-daemon.log (the log the start hint points
    at) would stay empty."""
    cfg = _config(tmp_path)

    async def _boom(self):
        raise ValueError("engine build blew up")

    monkeypatch.setattr(DaemonServer, "serve", _boom)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValueError, match="engine build blew up"):
            daemon_server.run(cfg)

    assert "daemon terminated with an unhandled exception" in caplog.text
    # The original traceback is attached (exc_info), not just the message.
    assert any(r.exc_info for r in caplog.records if r.levelno >= logging.ERROR)


def test_run_ignores_clean_cancel_scope_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean anyio cancel-scope teardown is not a crash: the barrier swallows
    it, returns 0, and does not re-raise."""
    cfg = _config(tmp_path)

    async def _clean_teardown(self):
        raise RuntimeError("Attempted to exit cancel scope in a different task")

    monkeypatch.setattr(DaemonServer, "serve", _clean_teardown)
    monkeypatch.setattr(daemon_server, "is_clean_cancel_scope_shutdown", lambda _e: True)

    rc = daemon_server.run(cfg)
    assert rc == 0
