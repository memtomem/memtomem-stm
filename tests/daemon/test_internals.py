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
    # record_feedback_events DOES drive daemon engine wiring → must move it.
    c = STMConfig()
    c.hook.record_feedback_events = True
    assert discovery.config_fingerprint(c) != fp


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
