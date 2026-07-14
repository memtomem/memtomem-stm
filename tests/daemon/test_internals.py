"""Unit tests for the daemon's protocol / discovery / locking primitives."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from memtomem_stm.config import STMConfig
from memtomem_stm.daemon import discovery, protocol
from memtomem_stm.daemon.locking import single_owner_lock


# ── protocol ──────────────────────────────────────────────────────────────


def _reader_with(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


def test_token_matches():
    assert protocol.token_matches("abc", "abc") is True
    assert protocol.token_matches("abc", "xyz") is False
    assert protocol.token_matches("abc", None) is False
    assert protocol.token_matches("abc", 123) is False
    assert protocol.token_matches("", "") is False  # empty expected never matches


async def test_encode_read_round_trip():
    frame = protocol.build_request("tok", protocol.OP_SURFACE, {"a": 1, "msg": "line\nbreak"})
    reader = _reader_with(protocol.encode_line(frame))
    got = await protocol.read_message(reader)
    assert got == frame  # embedded newline survived as an escaped char


async def test_read_message_closed_stream():
    with pytest.raises(protocol.ProtocolError):
        await protocol.read_message(_reader_with(b""))


async def test_read_message_malformed_and_non_object():
    with pytest.raises(protocol.ProtocolError):
        await protocol.read_message(_reader_with(b"not json\n"))
    with pytest.raises(protocol.ProtocolError):
        await protocol.read_message(_reader_with(b"[1,2,3]\n"))


# ── discovery ───────────────────────────────────────────────────────────────


def test_handshake_round_trip_and_mode(tmp_path: Path):
    path = discovery.handshake_path(tmp_path, "fp")
    discovery.write_handshake(
        path,
        pid=4321,
        host="127.0.0.1",
        port=5555,
        token="secret",
        config_fingerprint="fp",
        created_at=1700000000.0,
    )
    data = discovery.read_handshake(path)
    assert data is not None
    assert data["pid"] == 4321 and data["port"] == 5555 and data["token"] == "secret"
    if sys.platform != "win32":
        assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_read_handshake_missing_and_malformed(tmp_path: Path):
    assert discovery.read_handshake(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert discovery.read_handshake(bad) is None


def test_handshake_and_lock_paths_keyed_by_fingerprint(tmp_path: Path):
    # Distinct fingerprints → distinct files (so different-config daemons can
    # coexist); same fingerprint → stable path; the two files share the
    # ``stm-daemon`` prefix and differ only by extension.
    from memtomem_stm.daemon.locking import lock_path

    assert discovery.handshake_path(tmp_path, "aaaa").name == "stm-daemon-aaaa.json"
    assert lock_path(tmp_path, "aaaa").name == "stm-daemon-aaaa.lock"
    assert discovery.handshake_path(tmp_path, "aaaa") != discovery.handshake_path(tmp_path, "bbbb")
    assert lock_path(tmp_path, "aaaa") != lock_path(tmp_path, "bbbb")
    assert discovery.handshake_path(tmp_path, "aaaa") == discovery.handshake_path(tmp_path, "aaaa")


def _put_handshake(data_dir: Path, fingerprint: str, pid: int) -> None:
    discovery.write_handshake(
        discovery.handshake_path(data_dir, fingerprint),
        pid=pid,
        host="127.0.0.1",
        port=1,
        token="t",
        config_fingerprint=fingerprint,
        created_at=0.0,
    )


def test_iter_foreign_handshakes_excludes_current_and_bad(tmp_path: Path):
    # Three published daemons + one malformed file; the current fingerprint is
    # excluded, the malformed file is skipped, the rest come back keyed by the
    # filename fingerprint and sorted.
    _put_handshake(tmp_path, "cur00000", pid=10)
    _put_handshake(tmp_path, "bbbb1111", pid=20)
    _put_handshake(tmp_path, "aaaa0000", pid=30)
    (tmp_path / "stm-daemon-broken.json").write_text("{not json", encoding="utf-8")
    # Sibling files that share neither shape must be ignored.
    (tmp_path / "stm-daemon.log").write_text("noise", encoding="utf-8")

    out = discovery.iter_foreign_handshakes(tmp_path, "cur00000")

    assert [fp for fp, _ in out] == ["aaaa0000", "bbbb1111"]  # sorted, current dropped
    assert {fp: hs.get("pid") for fp, hs in out} == {"aaaa0000": 30, "bbbb1111": 20}


def test_iter_foreign_handshakes_missing_dir(tmp_path: Path):
    assert discovery.iter_foreign_handshakes(tmp_path / "nope", "cur") == []


# ── daemon logging (#612 convergence) ─────────────────────────────────────


_skip_on_windows = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX 0o700/0o600 modes are unenforceable on Windows",
)


@pytest.fixture
def _restore_root_logging():
    import logging

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    for h in root.handlers[:]:
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


@_skip_on_windows
def test_configure_logging_detached_hardens_file(tmp_path, _restore_root_logging):
    """Detached daemon logs to stm-daemon.log, now via the shared #612 handler:
    0o600 file / 0o700 parent + rotation, closing the pre-existing gap where
    the plain FileHandler left the crash-trace log world-readable."""
    import logging

    from memtomem_stm.cli.daemon_cmd import _configure_logging
    from memtomem_stm.logging_setup import PrivateRotatingFileHandler

    config = STMConfig(data_dir=tmp_path)
    _configure_logging(config, detached=True)
    logging.getLogger("daemon.test").warning("crash trace")

    logpath = tmp_path / "stm-daemon.log"
    assert logpath.exists()
    assert logpath.stat().st_mode & 0o777 == 0o600
    assert logpath.parent.stat().st_mode & 0o777 == 0o700
    handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, PrivateRotatingFileHandler)
    ]
    assert len(handlers) == 1


def test_configure_logging_foreground_uses_stderr(tmp_path, _restore_root_logging):
    import logging

    from memtomem_stm.cli.daemon_cmd import _configure_logging
    from memtomem_stm.logging_setup import PrivateRotatingFileHandler

    _configure_logging(STMConfig(data_dir=tmp_path), detached=False)
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
    assert not isinstance(handlers[0], PrivateRotatingFileHandler)
    assert not (tmp_path / "stm-daemon.log").exists()


def test_config_fingerprint_stable_and_broad(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", raising=False)
    fp = discovery.config_fingerprint(STMConfig())
    assert fp == discovery.config_fingerprint(STMConfig())  # stable

    # LTM command (subset of surfacing) moves it.
    c1 = STMConfig()
    c1.surfacing.ltm_mcp_command = "some-other-server"
    assert discovery.config_fingerprint(c1) != fp

    # A broad surfacing knob (min_score) must move it too — the old hand-picked
    # subset missed this and would keep using a stale daemon.
    c2 = STMConfig()
    c2.surfacing.min_score = 0.5
    assert discovery.config_fingerprint(c2) != fp

    # The flat MEMTOMEM_STM_HOOK_SURFACE_TOOLS env (read directly by the hook,
    # not bound to any model) must also move it.
    monkeypatch.setenv("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", "Read,Custom")
    assert discovery.config_fingerprint(STMConfig()) != fp


def test_config_fingerprint_includes_protocol_version(monkeypatch: pytest.MonkeyPatch):
    # A wire-protocol bump must move the fingerprint so a hook and a daemon built
    # at different PROTOCOL_VERSIONs key to distinct handshake/lock paths and
    # coexist (the stale one idle-times-out) instead of exchanging frames one
    # side can't parse.
    monkeypatch.delenv("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", raising=False)
    fp = discovery.config_fingerprint(STMConfig())
    monkeypatch.setattr("memtomem_stm.daemon.discovery.PROTOCOL_VERSION", 999)
    assert discovery.config_fingerprint(STMConfig()) != fp


def test_config_fingerprint_excludes_client_only_hook_fields(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", raising=False)
    fp = discovery.config_fingerprint(STMConfig())
    # Client-only hook fields must NOT move the fingerprint — the daemon's
    # behavior is independent of them, so a live daemon must still match a hook
    # that has opted out (else the hook rejects it and returns {} forever under
    # fallback=skip). use_daemon defaults True now, so toggle it OFF to prove
    # it's actually excluded.
    for field, value in [
        ("use_daemon", False),
        ("fallback", "cold"),
        ("daemon_timeout_seconds", 9.9),
        ("auto_spawn", False),
    ]:
        c = STMConfig()
        setattr(c.hook, field, value)
        assert discovery.config_fingerprint(c) == fp, field
    # The whole compression sub-block runs in the hook process, never in the
    # daemon — toggling it must not move the fingerprint either.
    c = STMConfig()
    c.hook.compression.enabled = True
    c.hook.compression.max_chars = 123
    assert discovery.config_fingerprint(c) == fp
    # record_feedback_events DOES drive daemon engine wiring → must move it.
    c = STMConfig()
    c.hook.record_feedback_events = True
    assert discovery.config_fingerprint(c) != fp

    # Standalone route selection is likewise client-only: the daemon's direct
    # adapter and engine behave identically regardless of who discovers it.
    c = STMConfig()
    c.surfacing.use_daemon = True
    assert discovery.config_fingerprint(c) == fp


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock re-entrancy semantics")
def test_start_retries_spawn_until_lock_frees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # A daemon shutting down still holds the lifetime lock, so request_spawn
    # declines — but it WILL release the lock and never become ready. `start`
    # must keep retrying the spawn (not poll ping once and give up), and must
    # never hold the lock itself (so the spawned child can take ownership).
    from click.testing import CliRunner

    from memtomem_stm.cli.proxy import cli
    from memtomem_stm.daemon.locking import lock_path, single_owner_lock

    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))

    state = {"spawns": 0, "lock_free_seen": []}

    def fake_request_spawn(config):
        # The real request_spawn probes the (per-config) lock; if start_cmd held
        # it this would see it taken. Assert it's free → start holds nothing.
        lp = lock_path(config.data_dir, discovery.config_fingerprint(config))
        with single_owner_lock(lp) as got:
            state["lock_free_seen"].append(got)
        state["spawns"] += 1
        return state["spawns"] >= 3  # declined twice (lock "held"), then spawns

    async def fake_ping(config, *, timeout=2.0):
        # Answers only once the (faked) shutdown finished and we spawned.
        return {"pid": 123, "port": 4567} if state["spawns"] >= 3 else None

    monkeypatch.setattr("memtomem_stm.daemon.spawn.request_spawn", fake_request_spawn)
    monkeypatch.setattr("memtomem_stm.daemon.client.ping", fake_ping)

    result = CliRunner().invoke(cli, ["daemon", "start"])
    assert result.exit_code == 0
    assert state["spawns"] >= 3  # retried past the initial declines
    assert all(state["lock_free_seen"])  # start never held the lock
    assert "daemon started" in result.output


def test_start_caps_spawn_attempts_for_crash_looping_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A child that crashes on startup (bad config) releases the lifetime lock
    # immediately, so every retry iteration finds it free. Without a cap,
    # `start` fired a detached child every 0.3s for the whole 10s window
    # (~33 crash-looping processes).
    from types import SimpleNamespace

    from click.testing import CliRunner

    from memtomem_stm.cli.proxy import cli

    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))

    spawns = {"n": 0}

    def crash_looping_spawn(config):
        spawns["n"] += 1
        return True  # lock free every time — the spawned child died instantly

    async def never_ready(config, *, timeout=2.0):
        return None

    monkeypatch.setattr("memtomem_stm.daemon.spawn.request_spawn", crash_looping_spawn)
    monkeypatch.setattr("memtomem_stm.daemon.client.ping", never_ready)
    # Compress the 10s wall-clock window: sleep advances a fake clock.
    clock = {"t": 0.0}
    monkeypatch.setattr(
        "memtomem_stm.cli.daemon_cmd.time",
        SimpleNamespace(
            time=lambda: clock["t"],
            sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        ),
    )

    result = CliRunner().invoke(cli, ["daemon", "start"])
    assert result.exit_code == 0
    assert "did not become ready" in result.output
    from memtomem_stm.cli.daemon_cmd import _START_MAX_SPAWNS

    assert spawns["n"] == _START_MAX_SPAWNS  # capped, not ~33


def test_start_coexists_with_different_config_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # config-drift coexistence: a daemon under a *different* config is alive (it
    # owns its own keyed handshake + lock). `start` under our config must just
    # spawn our daemon and report success — never treat the other as a conflict.
    from click.testing import CliRunner

    from memtomem_stm.cli.proxy import cli

    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    # A different-config daemon's live handshake, at ITS keyed path. Our config
    # never reads this file, so it must not influence `start` at all.
    discovery.write_handshake(
        discovery.handshake_path(tmp_path, "a-different-fp"),
        pid=os.getpid(),  # alive
        host="127.0.0.1",
        port=1,
        token="t",
        config_fingerprint="a-different-fp",
        created_at=0.0,
    )

    state = {"spawns": 0}

    def fake_request_spawn(config):
        state["spawns"] += 1
        return True  # our config's lock is free → spawn proceeds

    async def fake_ping(config, *, timeout=2.0):
        # Our config has no daemon until we've spawned one.
        return {"pid": 99, "port": 4567} if state["spawns"] >= 1 else None

    monkeypatch.setattr("memtomem_stm.daemon.spawn.request_spawn", fake_request_spawn)
    monkeypatch.setattr("memtomem_stm.daemon.client.ping", fake_ping)

    result = CliRunner().invoke(cli, ["daemon", "start"])
    assert result.exit_code == 0
    assert state["spawns"] >= 1  # spawned our own daemon despite the other one
    assert "different config" not in result.output  # no obsolete conflict report
    assert "daemon started" in result.output


# ── spawn (request_spawn) ─────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock re-entrancy semantics")
def test_request_spawn_skips_when_lock_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from memtomem_stm.daemon import spawn
    from memtomem_stm.daemon.locking import lock_path, single_owner_lock

    cfg = STMConfig()
    cfg.data_dir = tmp_path
    calls: list[int] = []
    monkeypatch.setattr(spawn, "_spawn_detached", lambda: calls.append(1))
    with single_owner_lock(lock_path(cfg.data_dir, discovery.config_fingerprint(cfg))) as held:
        assert held is True
        assert spawn.request_spawn(cfg) is False  # a live same-config owner → defer
    assert calls == []  # no duplicate spawn launched


def test_request_spawn_spawns_when_lock_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from memtomem_stm.daemon import spawn
    from memtomem_stm.daemon.locking import lock_path, single_owner_lock

    cfg = STMConfig()
    cfg.data_dir = tmp_path
    calls: list[int] = []
    monkeypatch.setattr(spawn, "_spawn_detached", lambda: calls.append(1))
    assert spawn.request_spawn(cfg) is True
    assert calls == [1]  # spawned exactly once
    # The probe released the lock → the child can still acquire it.
    with single_owner_lock(lock_path(cfg.data_dir, discovery.config_fingerprint(cfg))) as got:
        assert got is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock re-entrancy semantics")
def test_request_spawn_unblocked_by_different_config_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # config-drift coexistence: a daemon under a *different* config holds ITS
    # (differently-keyed) lock. Our spawn keys to our own fingerprint, finds it
    # free, and spawns anyway — the other daemon never blocks us.
    from memtomem_stm.daemon import spawn
    from memtomem_stm.daemon.locking import lock_path, single_owner_lock

    cfg = STMConfig()
    cfg.data_dir = tmp_path
    other_fp = "different00000fp"
    assert other_fp != discovery.config_fingerprint(cfg)
    calls: list[int] = []
    monkeypatch.setattr(spawn, "_spawn_detached", lambda: calls.append(1))
    with single_owner_lock(lock_path(cfg.data_dir, other_fp)) as held:
        assert held is True  # the other config's lock is taken
        assert spawn.request_spawn(cfg) is True  # ours is free → spawn proceeds
    assert calls == [1]


def test_request_spawn_swallows_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from memtomem_stm.daemon import locking, spawn

    cfg = STMConfig()
    cfg.data_dir = tmp_path
    calls: list[int] = []
    monkeypatch.setattr(spawn, "_spawn_detached", lambda: calls.append(1))

    def _boom(_path):
        raise OSError("cannot open lock file")

    monkeypatch.setattr(locking, "single_owner_lock", _boom)
    assert spawn.request_spawn(cfg) is False  # never raises into the hot path
    assert calls == []


# ── config (hook use_daemon / auto_spawn) ─────────────────────────────────────


def test_hook_use_daemon_default_true(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MEMTOMEM_STM_HOOK__USE_DAEMON", raising=False)
    assert STMConfig().hook.use_daemon is True


def test_hook_use_daemon_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "0")
    assert STMConfig().hook.use_daemon is False


def test_hook_auto_spawn_default_true(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MEMTOMEM_STM_HOOK__AUTO_SPAWN", raising=False)
    assert STMConfig().hook.auto_spawn is True


def test_hook_auto_spawn_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__AUTO_SPAWN", "0")
    assert STMConfig().hook.auto_spawn is False


async def test_request_respects_single_wall_clock_budget(monkeypatch: pytest.MonkeyPatch):
    # connect that hangs past the budget → the whole _request is bounded by the
    # single asyncio.timeout, returning None well within ~budget (not ~2x).
    import asyncio as _asyncio
    import time

    from memtomem_stm.daemon import client

    async def _slow_open(*args, **kwargs):
        await _asyncio.sleep(5)

    monkeypatch.setattr(_asyncio, "open_connection", _slow_open)
    hs = {"token": "t", "host": "127.0.0.1", "port": 1}
    start = time.monotonic()
    out = await client._request(hs, protocol.OP_PING, None, timeout=0.2)
    elapsed = time.monotonic() - start
    assert out is None
    assert elapsed < 1.5  # bounded by the 0.2 budget, not multiplied per step


def test_remove_handshake_if_owner(tmp_path: Path):
    path = discovery.handshake_path(tmp_path, "fp")
    discovery.write_handshake(
        path, pid=10, host="127.0.0.1", port=1, token="t", config_fingerprint="fp", created_at=0.0
    )
    # Wrong owner → kept.
    discovery.remove_handshake_if_owner(path, pid=999, token="t")
    assert path.exists()
    discovery.remove_handshake_if_owner(path, pid=10, token="wrong")
    assert path.exists()
    # Correct owner → removed.
    discovery.remove_handshake_if_owner(path, pid=10, token="t")
    assert not path.exists()


def test_is_pid_alive():
    assert discovery.is_pid_alive(os.getpid()) is True
    assert discovery.is_pid_alive(-1) is False
    if sys.platform != "win32":
        # A PID that almost certainly doesn't exist.
        assert discovery.is_pid_alive(2**31 - 1) is False
        # Beyond pid_t range: os.kill raises OverflowError (not OSError)
        # before ESRCH — must read as "not alive", not crash.
        assert discovery.is_pid_alive(10**1000) is False


def test_probe_listening():
    """The cross-platform connect-probe (#519): True iff something accepts on
    host:port, False for a closed port or a malformed endpoint."""
    import socket

    from memtomem_stm.daemon import client

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    host, port = srv.getsockname()
    try:
        # A listening socket — connect succeeds even though we never speak the
        # protocol (mirrors a foreign daemon at an incompatible PROTOCOL_VERSION).
        assert client.probe_listening(host, port) is True
    finally:
        srv.close()
    # Same port, now closed (the daemon's process is gone) — connect refused.
    assert client.probe_listening(host, port, timeout=0.5) is False
    # Malformed endpoints from a corrupted/partial handshake → quiet False.
    assert client.probe_listening(None, port) is False
    assert client.probe_listening(host, None) is False
    assert client.probe_listening(host, 0) is False
    assert client.probe_listening(host, -1) is False


# ── locking ─────────────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock re-entrancy semantics")
def test_single_owner_lock_excludes_second(tmp_path: Path):
    p = tmp_path / "x.lock"
    with single_owner_lock(p) as a:
        assert a is True
        with single_owner_lock(p) as b:
            assert b is False  # second concurrent acquirer is locked out
    # Released → acquirable again.
    with single_owner_lock(p) as c:
        assert c is True


# ── daemon stop / status / restart CLI ──────────────────────────────────────
#
# Zero direct CliRunner coverage existed for stop_cmd / status_cmd /
# restart_cmd — the commands carrying the unguarded int()/float() handshake
# casts and the SIGTERM-fallback + handshake-cleanup branches. These pin the
# contract that ops commands degrade (exit 0) instead of raising on a
# corrupted / hand-edited handshake, which read_handshake's docstring calls
# an anticipated input (it validates dict-ness only, not field types).


def _cli():
    from memtomem_stm.cli.proxy import cli

    return cli


def _write_handshake(payload: dict) -> Path:
    """Write a handshake file for the current env-derived config."""
    import json

    config = STMConfig()
    p = discovery.handshake_path(config.data_dir.expanduser(), discovery.config_fingerprint(config))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _no_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the graceful client paths report 'no responsive daemon'."""

    async def fake_shutdown(config):
        return False

    async def fake_ping(config, *, timeout=2.0):
        return None

    monkeypatch.setattr("memtomem_stm.daemon.client.shutdown", fake_shutdown)
    monkeypatch.setattr("memtomem_stm.daemon.client.ping", fake_ping)


class TestDaemonStopCli:
    def test_graceful_ack(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))

        async def fake_shutdown(config):
            return True

        monkeypatch.setattr("memtomem_stm.daemon.client.shutdown", fake_shutdown)
        result = CliRunner().invoke(_cli(), ["daemon", "stop"])
        assert result.exit_code == 0, result.output
        assert "daemon stopped" in result.output

    def test_no_daemon_no_handshake(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        result = CliRunner().invoke(_cli(), ["daemon", "stop"])
        assert result.exit_code == 0, result.output
        assert "no running daemon" in result.output

    @pytest.mark.parametrize(
        "bad_pid",
        ["garbage", None, float("inf"), float("-inf"), True, 10**1000],
        ids=["str", "null", "json-Infinity", "json-neg-Infinity", "json-true", "huge-int"],
    )
    def test_corrupted_pid_degrades_and_cleans(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_pid
    ):
        """The crash repro family: a handshake whose pid is not a sane int.
        The tail branch exists precisely to clean a stale/bad handshake, but
        the unguarded ``int(raw.get("pid", -1))`` crashed before reaching
        the cleanup — ValueError for strings, TypeError for null, and
        OverflowError for JSON ``Infinity`` (which Python's ``json.loads``
        accepts by default). JSON ``true`` coerced to pid 1 (launchd/init)
        and could steer the SIGTERM fallback at the wrong process; it must
        degrade instead, with no kill attempted."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        killed: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            # Faithful mimic of the real os.kill, which is also what
            # ``is_pid_alive``'s signal-0 probe reaches: pids beyond pid_t
            # raise OverflowError (the real syscall behavior is pinned in
            # ``test_is_pid_alive``); in-range pids are recorded instead of
            # signalled.
            if pid > 2**31 - 1:
                raise OverflowError("signed integer is greater than maximum")
            killed.append((pid, sig))

        monkeypatch.setattr(os, "kill", fake_kill)
        hs = _write_handshake({"pid": bad_pid, "port": 1, "token": "t"})

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 0, result.output
        assert "cleaned stale handshake" in result.output
        assert not hs.exists()
        assert killed == []

    def test_stale_handshake_alive_pid_gets_sigterm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Graceful shutdown declined but the endpoint still accepts a connect →
        the daemon is there-but-unresponsive, so SIGTERM its recorded pid."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        _write_handshake({"pid": 12345, "host": "127.0.0.1", "port": 1, "token": "t"})
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        monkeypatch.setattr(
            "memtomem_stm.daemon.client.probe_listening", lambda host, port, **kw: True
        )
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 0, result.output
        assert "sent SIGTERM" in result.output
        import signal as _signal

        assert killed == [(12345, _signal.SIGTERM)]

    def test_stale_handshake_recycled_pid_not_sigtermed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#519 same-config hardening: graceful declined, the handshake's pid is
        alive (recycled to an unrelated process) but its endpoint no longer
        accepts → do NOT SIGTERM; clean the stale handshake instead."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        hs = _write_handshake({"pid": 12345, "host": "127.0.0.1", "port": 1, "token": "t"})
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        monkeypatch.setattr(
            "memtomem_stm.daemon.client.probe_listening", lambda host, port, **kw: False
        )
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 0, result.output
        assert "cleaned stale handshake" in result.output
        assert killed == []  # recycled pid never signalled
        assert not hs.exists()

    def test_windows_declines_posix_signal_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        _write_handshake({"pid": 12345, "host": "127.0.0.1", "port": 1, "token": "t"})
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        monkeypatch.setattr(
            "memtomem_stm.daemon.client.probe_listening", lambda host, port, **kw: True
        )
        monkeypatch.setattr("memtomem_stm.cli.daemon_cmd._can_force_terminate", lambda: False)
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])
        assert result.exit_code == 0, result.output
        assert "Windows does not provide the POSIX SIGTERM fallback" in result.output
        assert killed == []


class TestDaemonStatusCli:
    def test_running_json_shape(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import json
        import time as _time

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))

        async def fake_ping(config, *, timeout=2.0):
            return {
                "pid": 11,
                "host": "127.0.0.1",
                "port": 4567,
                "ltm": "warm",
                "created_at": _time.time() - 3.0,
            }

        monkeypatch.setattr("memtomem_stm.daemon.client.ping", fake_ping)
        result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)
        assert info["state"] == "running"
        assert info["pid"] == 11
        assert info["uptime_seconds"] >= 0.0
        assert "hook_will_use_daemon" in info
        assert "standalone_will_use_daemon" in info

    def test_running_corrupted_created_at_degrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A garbage created_at must degrade to uptime 0.0, not a
        ValueError traceback."""
        import json

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))

        async def fake_ping(config, *, timeout=2.0):
            return {"pid": 11, "host": "h", "port": 1, "ltm": "warm", "created_at": "garbage"}

        monkeypatch.setattr("memtomem_stm.daemon.client.ping", fake_ping)
        result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)
        assert info["state"] == "running"
        assert info["uptime_seconds"] == 0.0

    def test_stale_corrupted_pid_degrades(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Stale-handshake branch with a null pid must report state=stale
        (pid_alive False), not crash on ``int(None)``."""
        import json

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        _write_handshake({"pid": None, "port": 1, "token": "t"})

        result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])

        assert result.exit_code == 0, result.output
        info = json.loads(result.output)
        assert info["state"] == "stale"
        assert info["pid_alive"] is False

    def test_stopped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        result = CliRunner().invoke(_cli(), ["daemon", "status"])
        assert result.exit_code == 0, result.output
        assert "stopped" in result.output


def _probe_all(monkeypatch: pytest.MonkeyPatch, listening: bool = True) -> None:
    """Pin the connect-probe so the two-factor liveness check (#519) is decided
    by ``is_pid_alive`` alone — ``listening=True`` simulates a daemon still
    accepting on its endpoint, ``False`` a stale handshake whose port is gone."""
    monkeypatch.setattr(
        "memtomem_stm.daemon.client.probe_listening", lambda host, port, **kw: listening
    )


class TestDaemonForeignOrphans:
    """`#517` — daemons orphaned under a stale fingerprint (config/protocol drift)
    must be visible to ``status`` and stoppable via ``stop --all``, even when
    pinned with ``idle_timeout_seconds=0`` (so they never self-clear).

    Liveness is two-factor (#519): the recorded pid must be alive *and* its
    endpoint must accept a connect. These tests mock both factors
    (``is_pid_alive`` + ``client.probe_listening``), so they are platform-agnostic
    — the connect-probe is what re-enabled Windows, where ``is_pid_alive`` alone
    is uninformative. ``test_foreign_detection_cross_platform`` pins that, and
    ``test_recycled_pid_skipped_by_connect_probe`` pins the recycled-pid gate."""

    def _write_foreign(
        self, tmp_path: Path, fingerprint: str, pid: int, *, port: int = 4242
    ) -> None:
        discovery.write_handshake(
            discovery.handshake_path(tmp_path, fingerprint),
            pid=pid,
            host="127.0.0.1",
            port=port,
            token="secret-token",
            config_fingerprint=fingerprint,
            created_at=0.0,
        )

    def test_status_reports_foreign(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import json

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        cur = discovery.config_fingerprint(STMConfig())
        assert cur != "foreignfp000000"
        self._write_foreign(tmp_path, "foreignfp000000", pid=98765)
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        _probe_all(monkeypatch)

        # JSON surface: a `foreign` array with non-sensitive fields (never token).
        result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)
        assert info["foreign"] == [
            {"fingerprint": "foreignfp000000", "pid": 98765, "host": "127.0.0.1", "port": 4242}
        ]
        assert "secret-token" not in result.output

        # Text surface: a warning line + the actionable hint.
        result = CliRunner().invoke(_cli(), ["daemon", "status"])
        assert result.exit_code == 0, result.output
        assert "pid=98765 fp=foreignfp000000" in result.output
        assert "stop --all" in result.output

    def test_bare_stop_leaves_foreign_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        self._write_foreign(tmp_path, "foreignfp000000", pid=98765)
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 0, result.output
        assert "no running daemon" in result.output
        assert killed == []  # default scope never reaches a different-config daemon

    def test_stop_all_sigterms_foreign(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import signal as _signal

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        self._write_foreign(tmp_path, "foreignfp000000", pid=98765)
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        _probe_all(monkeypatch)
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 0, result.output
        assert killed == [(98765, _signal.SIGTERM)]
        assert "sent SIGTERM to daemon pid=98765 (fp=foreignfp000000)" in result.output

    def test_pinned_fingerprint_change_is_reachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The acceptance scenario: a real fingerprint change strands a pinned
        daemon, which ``stop --all`` then reaches by pid."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        # The "old" daemon ran under a different fingerprinted config (min_score),
        # then the operator changed it — leaving the old handshake stranded.
        old = STMConfig()
        old.surfacing.min_score = 0.5
        old_fp = discovery.config_fingerprint(old)
        assert old_fp != discovery.config_fingerprint(STMConfig())
        self._write_foreign(tmp_path, old_fp, pid=4321)
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        _probe_all(monkeypatch)
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 0, result.output
        assert [pid for pid, _ in killed] == [4321]
        assert f"fp={old_fp}" in result.output

    def test_dead_pid_foreign_filtered_and_sorted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A left-behind handshake naming a dead pid must be dropped (not
        reported), and live ones rendered in fingerprint-sorted order — pins the
        liveness filter that a non-filtering impl would silently skip."""
        import json

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        self._write_foreign(tmp_path, "aaaa000000000000", pid=1001)  # alive
        self._write_foreign(tmp_path, "bbbb000000000000", pid=2002)  # dead
        self._write_foreign(tmp_path, "cccc000000000000", pid=3003)  # alive
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: pid != 2002)
        _probe_all(monkeypatch)

        result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)
        assert [d["fingerprint"] for d in info["foreign"]] == [
            "aaaa000000000000",
            "cccc000000000000",
        ]
        assert [d["pid"] for d in info["foreign"]] == [1001, 3003]  # dead 2002 dropped
        assert "2002" not in result.output

        result = CliRunner().invoke(_cli(), ["daemon", "status"])
        assert result.exit_code == 0, result.output
        # Sorted listing in the text warning, dead one absent.
        assert "pid=1001 fp=aaaa000000000000, pid=3003 fp=cccc000000000000" in result.output
        assert "2002" not in result.output

    def test_stop_all_skips_dead_and_continues_on_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """`stop --all` must (a) never signal a dead-pid orphan, (b) keep going
        when one SIGTERM raises OSError, and (c) still exit 0 (fail-open)."""
        import signal as _signal

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        self._write_foreign(tmp_path, "aaaa000000000000", pid=1001)  # alive, kill raises
        self._write_foreign(tmp_path, "bbbb000000000000", pid=2002)  # dead, never signalled
        self._write_foreign(tmp_path, "cccc000000000000", pid=3003)  # alive, kill succeeds
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: pid != 2002)
        _probe_all(monkeypatch)
        killed: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            if pid == 1001:
                raise OSError("ESRCH")
            killed.append((pid, sig))

        monkeypatch.setattr(os, "kill", fake_kill)

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 0, result.output
        assert killed == [(3003, _signal.SIGTERM)]  # 2002 never tried, loop continued past 1001
        assert "could not signal daemon pid=1001" in result.output
        assert "sent SIGTERM to daemon pid=3003" in result.output

    def test_status_running_with_foreign(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """A live current daemon AND a foreign orphan: the running-branch `info`
        must still carry `foreign`, and the token must never leak."""
        import json
        import time as _time

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))

        async def fake_ping(config, *, timeout=2.0):
            return {
                "pid": 11,
                "host": "127.0.0.1",
                "port": 4567,
                "ltm": "warm",
                "created_at": _time.time(),
            }

        monkeypatch.setattr("memtomem_stm.daemon.client.ping", fake_ping)
        self._write_foreign(tmp_path, "foreignfp000000", pid=98765)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        _probe_all(monkeypatch)

        result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)
        assert info["state"] == "running"
        assert info["pid"] == 11
        assert info["foreign"] == [
            {"fingerprint": "foreignfp000000", "pid": 98765, "host": "127.0.0.1", "port": 4242}
        ]
        assert "secret-token" not in result.output

        result = CliRunner().invoke(_cli(), ["daemon", "status"])
        assert result.exit_code == 0, result.output
        assert "running  pid=11" in result.output  # current daemon line
        assert "pid=98765 fp=foreignfp000000" in result.output  # foreign warning
        assert "secret-token" not in result.output

    def test_stop_all_no_foreign(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 0, result.output
        assert "no daemons running under a different config" in result.output

    def test_foreign_detection_cross_platform(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The connect-probe re-enables Windows (#519): `_live_foreign_daemons` no
        longer branches on the platform, so with Windows' `is_pid_alive` (always
        True for a positive pid) the *connect* carries the whole signal — a
        listening endpoint is detected, a dead one is not."""
        from memtomem_stm.cli import daemon_cmd

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        self._write_foreign(tmp_path, "foreignfp000000", pid=98765)
        # Windows-like: is_pid_alive can't discriminate (always True for a positive
        # pid). No `os.name` patch — that would poison pathlib (WindowsPath); the
        # point is precisely that the code path is now platform-independent.
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)

        # Endpoint accepting → reported (previously [] on Windows).
        _probe_all(monkeypatch, listening=True)
        assert daemon_cmd._live_foreign_daemons(STMConfig()) == [
            {"fingerprint": "foreignfp000000", "pid": 98765, "host": "127.0.0.1", "port": 4242}
        ]
        # Endpoint gone → still nothing (no false positive from the always-True pid).
        _probe_all(monkeypatch, listening=False)
        assert daemon_cmd._live_foreign_daemons(STMConfig()) == []

    def test_recycled_pid_skipped_by_connect_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The #519 gate: an alive pid (recycled to an unrelated process) whose
        recorded endpoint no longer accepts must NOT be reported as running, and
        ``stop --all`` must not SIGTERM it."""
        from click.testing import CliRunner

        from memtomem_stm.cli import daemon_cmd

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        self._write_foreign(tmp_path, "foreignfp000000", pid=98765)
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)  # alive
        _probe_all(monkeypatch, listening=False)  # but nothing accepts on its port
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

        assert daemon_cmd._live_foreign_daemons(STMConfig()) == []

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])
        assert result.exit_code == 0, result.output
        assert killed == []  # recycled pid never signalled
        assert "no daemons running under a different config" in result.output

    def test_stop_all_reprobes_before_kill(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """TOCTOU between enumeration and `os.kill` (#519): a foreign daemon passes
        the enumeration probe, then exits and its pid is recycled before the kill.
        The action-time re-probe (not just `is_pid_alive`, a no-op on Windows) must
        catch it so `stop --all` does not SIGTERM the recycled pid."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        self._write_foreign(tmp_path, "foreignfp000000", pid=98765)
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        # True at enumeration, False at the action-time re-check (endpoint vanished).
        calls = {"n": 0}

        def flaky_probe(host, port, **kw):
            calls["n"] += 1
            return calls["n"] == 1

        monkeypatch.setattr("memtomem_stm.daemon.client.probe_listening", flaky_probe)
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 0, result.output
        assert killed == []  # re-probe failed → recycled pid not signalled
        assert calls["n"] == 2  # probed at enumeration AND again before the kill

    def test_listening_foreign_detected_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """No mocks on the probe: a real listening socket + this process's own
        (alive) pid is detected via the actual TCP connect, and a since-closed
        port is not — pins the probe wired through ``_live_foreign_daemons``."""
        import socket

        from memtomem_stm.cli import daemon_cmd

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        _host, port = srv.getsockname()
        # A genuine foreign daemon: alive pid (ours) + a real accepting endpoint.
        self._write_foreign(tmp_path, "foreignfp000000", pid=os.getpid(), port=port)
        try:
            assert daemon_cmd._live_foreign_daemons(STMConfig()) == [
                {
                    "fingerprint": "foreignfp000000",
                    "pid": os.getpid(),
                    "host": "127.0.0.1",
                    "port": port,
                }
            ]
        finally:
            srv.close()
        # Endpoint now closed → the alive pid alone is not enough to report it.
        assert daemon_cmd._live_foreign_daemons(STMConfig()) == []


class TestDaemonRestartCli:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock semantics")
    def test_stop_then_start(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))

        calls = {"shutdown": 0, "spawns": 0}

        async def fake_shutdown(config):
            calls["shutdown"] += 1
            return True

        async def fake_ping(config, *, timeout=2.0):
            return {"pid": 7, "port": 9} if calls["spawns"] else None

        def fake_request_spawn(config):
            calls["spawns"] += 1
            return True

        monkeypatch.setattr("memtomem_stm.daemon.client.shutdown", fake_shutdown)
        monkeypatch.setattr("memtomem_stm.daemon.client.ping", fake_ping)
        monkeypatch.setattr("memtomem_stm.daemon.spawn.request_spawn", fake_request_spawn)

        result = CliRunner().invoke(_cli(), ["daemon", "restart"])

        assert result.exit_code == 0, result.output
        assert calls["shutdown"] == 1
        assert calls["spawns"] >= 1
        assert "daemon started" in result.output

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock semantics")
    def test_wedged_teardown_reports_clearly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """When the old daemon never frees its lifetime lock, restart must
        report the wedged teardown instead of a misleading start timeout."""
        from click.testing import CliRunner

        from memtomem_stm.daemon.locking import lock_path

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)

        # Fast-forward the 5s lock-wait: daemon_cmd's module-level ``time``
        # is swapped for a fake that advances 1s per time() call and no-ops
        # sleep(), so the deadline loop exits after ~5 iterations without
        # wall-clock waiting.
        class _FakeTime:
            def __init__(self) -> None:
                self.now = 1_000.0

            def time(self) -> float:
                self.now += 1.0
                return self.now

            def sleep(self, _s: float) -> None:
                return None

        monkeypatch.setattr("memtomem_stm.cli.daemon_cmd.time", _FakeTime())

        config = STMConfig()
        lp = lock_path(config.data_dir.expanduser(), discovery.config_fingerprint(config))
        lp.parent.mkdir(parents=True, exist_ok=True)
        with single_owner_lock(lp) as held:
            assert held is True
            result = CliRunner().invoke(_cli(), ["daemon", "restart"])

        assert result.exit_code == 0, result.output
        assert "still shutting down" in result.output


# ── daemon run + handshake coercion helpers ─────────────────────────────────


class TestRunCmd:
    """``mms daemon run`` — the long-lived entry point had no direct test.

    ``run_cmd`` is three load-bearing lines: load config, route logging by
    ``--detached``, and exit with the server loop's return code. The server
    loop itself is covered in ``tests/daemon/test_server.py``; here it is
    stubbed so only the command layer is under test.
    """

    def _invoke_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: list[str], rc: int
    ):
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        seen: dict = {}

        def fake_run(config):
            seen["config"] = config
            return rc

        def fake_configure_logging(config, *, detached):
            seen["detached"] = detached

        # run_cmd imports memtomem_stm.daemon.server.run lazily at call time,
        # so patching the source module attribute is sufficient.
        monkeypatch.setattr("memtomem_stm.daemon.server.run", fake_run)
        monkeypatch.setattr(
            "memtomem_stm.cli.daemon_cmd._configure_logging", fake_configure_logging
        )
        return CliRunner().invoke(_cli(), ["daemon", "run", *args]), seen

    def test_exit_code_is_server_return_code(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        result, seen = self._invoke_run(monkeypatch, tmp_path, [], rc=3)
        assert result.exit_code == 3
        assert isinstance(seen["config"], STMConfig)

    def test_foreground_default_routes_logging_to_stderr_handler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result, seen = self._invoke_run(monkeypatch, tmp_path, [], rc=0)
        assert result.exit_code == 0
        assert seen["detached"] is False

    def test_detached_flag_routes_logging_to_file_handler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        result, seen = self._invoke_run(monkeypatch, tmp_path, ["--detached"], rc=0)
        assert result.exit_code == 0
        assert seen["detached"] is True


class TestHandshakeCoercionHelpers:
    """Direct unit table for ``_as_int`` / ``_as_float``.

    The stop/status corrupted-handshake tests above exercise these only
    through full CLI invocations; this pins each rejection branch —
    bool-reject (JSON ``true`` must not become pid 1), TypeError,
    ValueError, and OverflowError — at the helper level.
    """

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (42, 42),
            ("42", 42),
            (42.9, 42),
            (True, -1),  # bool-reject: JSON true would coerce to pid 1
            (False, -1),
            (None, -1),  # TypeError
            ("junk", -1),  # ValueError
            (float("inf"), -1),  # OverflowError (JSON Infinity)
            (float("-inf"), -1),
            (float("nan"), -1),  # ValueError
        ],
    )
    def test_as_int(self, value, expected):
        from memtomem_stm.cli.daemon_cmd import _as_int

        assert _as_int(value, default=-1) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1.5, 1.5),
            ("1.5", 1.5),
            (7, 7.0),
            (True, 0.0),  # bool-reject
            (False, 0.0),
            (None, 0.0),  # TypeError
            ("junk", 0.0),  # ValueError
            (10**400, 0.0),  # OverflowError: huge int -> float
        ],
    )
    def test_as_float(self, value, expected):
        from memtomem_stm.cli.daemon_cmd import _as_float

        assert _as_float(value, default=0.0) == expected

    def test_as_float_passes_through_infinity(self):
        """float('inf') IS a valid float — _as_float guards conversion
        failures only; range/semantic checks belong to the call sites."""
        from memtomem_stm.cli.daemon_cmd import _as_float

        assert _as_float(float("inf"), default=0.0) == float("inf")
