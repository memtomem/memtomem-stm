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
    path = discovery.handshake_path(tmp_path)
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
    path = discovery.handshake_path(tmp_path)
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
