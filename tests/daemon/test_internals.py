"""Unit tests for the daemon's protocol / discovery / locking primitives."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from memtomem_stm.config import STMConfig
from memtomem_stm.daemon import discovery, protocol
from memtomem_stm.daemon.locking import (
    lock_path,
    open_lock_fd,
    release_lock,
    single_owner_lock,
    try_lock,
)


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


async def test_encode_line_survives_a_lone_surrogate():
    # ``encode_line`` ends in an explicit ``.encode("utf-8")``, so a lone
    # surrogate anywhere in the frame raised and took the connection with it —
    # on the daemon side that meant the client saw a silently closed socket
    # rather than an error frame (#761).
    frame = protocol.surface_response({"text": "a\ud800b"})
    wire = protocol.encode_line(frame)

    assert wire.endswith(b"\n") and wire.count(b"\n") == 1
    assert b"\\ud800" in wire


async def test_a_surrogate_bearing_frame_round_trips_as_inert_text():
    # The write-side escape alone is not enough: ``json.loads`` decodes
    # ``\ud800`` straight back into the code unit, which would then raise in
    # the *receiving* process at its next encode. The read side scrubs so the
    # value arrives as the six literal characters and stays encodable.
    frame = protocol.surface_response({"text": "a\ud800b"})
    got = await protocol.read_message(_reader_with(protocol.encode_line(frame)))

    assert got["output"]["text"] == "a\\ud800b"
    protocol.encode_line(got)  # re-framing what we received must not raise


async def test_read_message_scrubs_a_surrogate_the_peer_sent_raw():
    # A peer that did not escape — an older build, or anything else speaking
    # this protocol — can still put a surrogate on the wire as a legal JSON
    # escape. The read side owns making what we hand upward encodable.
    raw = b'{"v":7,"ok":true,"output":{"text":"a\\ud800b"}}\n'
    got = await protocol.read_message(_reader_with(raw))

    assert got["output"]["text"] == "a\\ud800b"


async def test_read_message_leaves_a_clean_frame_identical():
    frame = protocol.build_request("tok", protocol.OP_SURFACE, {"msg": "서버 🚀"})
    got = await protocol.read_message(_reader_with(protocol.encode_line(frame)))
    assert got == frame


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
        idle_shutdown_seconds=900.0,
    )
    data = discovery.read_handshake(path)
    assert data is not None
    assert data["pid"] == 4321 and data["port"] == 5555 and data["token"] == "secret"
    assert data["idle_shutdown_seconds"] == 900.0
    if sys.platform != "win32":
        assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_read_handshake_missing_and_malformed(tmp_path: Path):
    assert discovery.read_handshake(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert discovery.read_handshake(bad) is None
    undecodable = tmp_path / "undecodable.json"
    undecodable.write_bytes(b'{"token": "\xff"}')
    assert discovery.read_handshake(undecodable) is None


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
        idle_shutdown_seconds=900.0,
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


def test_iter_foreign_handshakes_matches_what_glob_matched(tmp_path: Path):
    """#1249 replaced ``Path.glob`` with ``os.listdir``; the names it picks must
    stay the ones glob picked on this platform — on Windows that includes a
    ``.JSON`` extension, which glob matched case-insensitively."""
    names = [
        "stm-daemon-aaaa0000.json",
        "stm-daemon-bbbb1111.JSON",
        "STM-DAEMON-cccc2222.json",
        "stm-daemon-dddd3333.json.tmp",
        "stm-daemon.json",
        "stm-daemon.log",
        ".stm-daemon-eeee4444.json",
    ]
    for name in names:
        (tmp_path / name).write_text("{}", encoding="utf-8")
    (tmp_path / "stm-daemon-ffff5555.json").mkdir()

    globbed = sorted(p.name for p in tmp_path.glob("stm-daemon-*.json"))
    out = discovery.iter_foreign_handshakes(tmp_path, "cur00000", include_unreadable=True)

    assert [fp for fp, _ in out] == [Path(n).stem.removeprefix("stm-daemon-") for n in globbed]
    assert "stm-daemon-aaaa0000.json" in globbed
    if os.name == "nt":
        assert "stm-daemon-bbbb1111.JSON" in globbed


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


def test_config_fingerprint_separates_daemon_concurrency():
    # Both admission bounds change what the daemon does, so both split identity.
    # Without this a caller that configured the serialized daemon
    # (`max_concurrent_ltm_ops=1`) keys to the same handshake as a running
    # four-way one and silently reuses it (#874).
    fp = discovery.config_fingerprint(STMConfig())

    serialized = STMConfig()
    serialized.daemon.max_concurrent_ltm_ops = 1
    assert discovery.config_fingerprint(serialized) != fp

    wider = STMConfig()
    wider.daemon.max_concurrent_ltm_ops = 8
    assert discovery.config_fingerprint(wider) != fp
    assert discovery.config_fingerprint(wider) != discovery.config_fingerprint(serialized)

    admission = STMConfig()
    admission.daemon.max_pending_requests = 8
    assert discovery.config_fingerprint(admission) != fp


def test_config_fingerprint_survives_a_hostile_surface_tools_env(
    monkeypatch: pytest.MonkeyPatch,
):
    # ``MEMTOMEM_STM_HOOK_SURFACE_TOOLS`` is folded into the fingerprint material
    # verbatim, and on POSIX an environment variable holding a byte that is not
    # valid UTF-8 is decoded with ``surrogateescape`` — so a lone surrogate
    # reaches the digest's ``.encode("utf-8")`` without any config file being
    # involved (#761). Before the fix this raised ``UnicodeEncodeError`` out of
    # ``DaemonServer.__init__`` and out of ``client._live_handshake_candidate``,
    # i.e. no daemon could start and ``mms daemon status`` printed a traceback.
    clean = discovery.config_fingerprint(STMConfig())

    monkeypatch.setenv("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", "Read,\udcff")
    hostile = discovery.config_fingerprint(STMConfig())

    assert len(hostile) == 16 and all(c in "0123456789abcdef" for c in hostile)
    # Still a *fingerprint*: it is stable, and it separates this env from the
    # clean one rather than collapsing every unencodable value onto one digest.
    assert hostile == discovery.config_fingerprint(STMConfig())
    assert hostile != clean
    monkeypatch.setenv("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", "Read,\udcfe")
    assert discovery.config_fingerprint(STMConfig()) != hostile


def test_daemon_server_constructs_under_a_hostile_surface_tools_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    # The fingerprint is frozen in ``DaemonServer.__init__`` (before ``serve()``,
    # before any ``try``), so a raise there means the process never starts. Pin
    # the construction itself rather than only the helper (#761).
    from memtomem_stm.daemon.server import DaemonServer

    monkeypatch.setenv("MEMTOMEM_STM_HOOK_SURFACE_TOOLS", "Read,\udcff")
    config = STMConfig()
    config.data_dir = tmp_path
    server = DaemonServer(config)
    assert len(server._fingerprint) == 16
    # The frozen value is what names the lock and handshake files, so it has to
    # survive being turned into a path too.
    assert discovery.handshake_path(tmp_path, server._fingerprint).name.endswith(".json")


def test_config_fingerprint_includes_protocol_version(monkeypatch: pytest.MonkeyPatch):
    # A wire-protocol bump must move the fingerprint so a hook and a daemon built
    # at different PROTOCOL_VERSIONs key to distinct handshake/lock paths and
    # coexist (the stale one idle-times-out) instead of exchanging frames one
    # side can't parse.
    fp = discovery.config_fingerprint(STMConfig())
    monkeypatch.setattr("memtomem_stm.daemon.discovery.PROTOCOL_VERSION", 999)
    assert discovery.config_fingerprint(STMConfig()) != fp


def test_config_fingerprint_excludes_client_only_hook_fields():
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

    def fake_request_spawn(config, *, propagate_errors=False):
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

    def crash_looping_spawn(config, *, propagate_errors=False):
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
    assert result.exit_code == 1
    assert "did not become ready" in result.output
    from memtomem_stm.cli.daemon_cmd import _START_MAX_SPAWNS

    assert spawns["n"] == _START_MAX_SPAWNS  # capped, not ~33


def test_start_reports_a_failing_spawn_instead_of_retrying_it_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A spawn that cannot even fork (no thread, EAGAIN) leaves the lock free, so
    # it looks exactly like a deferral to a caller reading only the bool: the
    # retry budget is never spent, the window fills with ~33 identical failures,
    # and the timeout message sends the operator to a daemon log the child never
    # got to write. `start` asks for the error and charges the attempt.
    from types import SimpleNamespace

    from click.testing import CliRunner

    from memtomem_stm.cli.proxy import cli

    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))

    attempts = {"n": 0}

    def failing_spawn(config, *, propagate_errors=False):
        attempts["n"] += 1
        assert propagate_errors  # start must not read this failure as a deferral
        raise OSError("Resource temporarily unavailable")

    async def never_ready(config, *, timeout=2.0):
        return None

    monkeypatch.setattr("memtomem_stm.daemon.spawn.request_spawn", failing_spawn)
    monkeypatch.setattr("memtomem_stm.daemon.client.ping", never_ready)
    clock = {"t": 0.0}
    monkeypatch.setattr(
        "memtomem_stm.cli.daemon_cmd.time",
        SimpleNamespace(
            time=lambda: clock["t"],
            sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        ),
    )

    result = CliRunner().invoke(cli, ["daemon", "start"])
    assert result.exit_code == 1
    from memtomem_stm.cli.daemon_cmd import _START_MAX_SPAWNS

    assert attempts["n"] == _START_MAX_SPAWNS  # a failed spawn spends the budget
    assert "Resource temporarily unavailable" in result.output  # names the cause
    assert "stm-daemon.log" not in result.output  # and not a log that cannot exist


def test_start_points_at_the_daemon_log_once_a_child_actually_launched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # A fork that failed and then succeeded is a different story from one that
    # never succeeded: the child that launched can write the log, so reporting
    # the earlier OSError would send the operator somewhere the answer is not
    # and drop the only pointer they have.
    from types import SimpleNamespace

    from click.testing import CliRunner

    from memtomem_stm.cli.proxy import cli

    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))

    attempts = {"n": 0}

    def failing_then_launching(config, *, propagate_errors=False):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("Resource temporarily unavailable")
        return True  # a child got as far as exec, then crashed on startup

    async def never_ready(config, *, timeout=2.0):
        return None

    monkeypatch.setattr("memtomem_stm.daemon.spawn.request_spawn", failing_then_launching)
    monkeypatch.setattr("memtomem_stm.daemon.client.ping", never_ready)
    clock = {"t": 0.0}
    monkeypatch.setattr(
        "memtomem_stm.cli.daemon_cmd.time",
        SimpleNamespace(
            time=lambda: clock["t"],
            sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        ),
    )

    result = CliRunner().invoke(cli, ["daemon", "start"])
    assert result.exit_code == 1
    assert "stm-daemon.log" in result.output  # the launched child's log
    assert "Resource temporarily unavailable" not in result.output  # stale cause


def test_start_keeps_the_log_pointer_when_the_failure_comes_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # The mirror of the case above. A child launched, then a later fork failed:
    # the log the first child wrote is still the place to look, so a message
    # that reports only the fork failure walks the operator past the answer.
    from types import SimpleNamespace

    from click.testing import CliRunner

    from memtomem_stm.cli.proxy import cli

    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))

    attempts = {"n": 0}

    def launching_then_failing(config, *, propagate_errors=False):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return True  # launched, and the child crashed on startup
        raise OSError("Resource temporarily unavailable")

    async def never_ready(config, *, timeout=2.0):
        return None

    monkeypatch.setattr("memtomem_stm.daemon.spawn.request_spawn", launching_then_failing)
    monkeypatch.setattr("memtomem_stm.daemon.client.ping", never_ready)
    clock = {"t": 0.0}
    monkeypatch.setattr(
        "memtomem_stm.cli.daemon_cmd.time",
        SimpleNamespace(
            time=lambda: clock["t"],
            sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        ),
    )

    result = CliRunner().invoke(cli, ["daemon", "start"])
    assert result.exit_code == 1
    assert "stm-daemon.log" in result.output
    assert "Resource temporarily unavailable" not in result.output


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
        idle_shutdown_seconds=900.0,
    )

    state = {"spawns": 0}

    def fake_request_spawn(config, *, propagate_errors=False):
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


def test_spawn_detached_registers_the_child_as_meant_to_outlive_us(
    monkeypatch: pytest.MonkeyPatch,
):
    """The detached daemon is still a direct child — nothing double-forks here.
    A caller's teardown leaked-child sweep must be told, or it reads the shared
    daemon as a leak and kills it (with the LTM it holds for everyone) on exit
    (#906)."""
    import threading

    from memtomem_stm.daemon import spawn
    from memtomem_stm.utils import child_reaper

    monkeypatch.setattr(child_reaper, "_detached_claims", {})
    # Hold the child open for the assertion. A wait() that returns at once lets
    # the reaper retire the claim first, so the test would be asserting on a
    # race — and the reaper could outlive the monkeypatch and touch the restored
    # process-global claims.
    running = threading.Event()
    reaped = threading.Event()

    def _wait() -> int:
        running.wait(5)
        reaped.set()
        return 0

    monkeypatch.setattr(
        spawn.subprocess, "Popen", lambda *_a, **_kw: SimpleNamespace(pid=31337, wait=_wait)
    )
    reaper: list[threading.Thread] = []
    try:
        spawn._spawn_detached()
        reaper += [t for t in threading.enumerate() if t.name == "stm-daemon-reaper"]
        claim = child_reaper._detached_claims[31337]
        assert claim.retired_at is None  # claimed for as long as the child lives
    finally:
        # Release the child and join even when the assertion above fails: an
        # abandoned reaper would sit here until its own timeout and then retire
        # its claim against the globals this test is about to restore.
        running.set()
        for thread in reaper:
            thread.join(timeout=5)
    assert reaped.wait(5)
    assert 31337 not in child_reaper._detached_claims  # retired once reaped


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
    # propagate_errors is about the *spawn*, not the probe: an unopenable lock
    # file says nothing about whether a daemon is coming up, so `start` keeps
    # retrying it rather than reporting it as the reason it never became ready.
    assert spawn.request_spawn(cfg, propagate_errors=True) is False
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


_SURFACE_OUTPUT = {"hookSpecificOutput": {"hookEventName": "PostToolUse"}}


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ({"v": protocol.PROTOCOL_VERSION, "ok": True, "output": _SURFACE_OUTPUT}, _SURFACE_OUTPUT),
        # A live daemon that turned the call away: nothing to inject, and
        # nothing for the caller to spawn or redo cold (#1198).
        ({"v": protocol.PROTOCOL_VERSION, "ok": False, "status": "busy"}, {}),
        ({"v": protocol.PROTOCOL_VERSION, "ok": False, "status": "expired"}, {}),
        ({"v": protocol.PROTOCOL_VERSION, "ok": False, "status": "unavailable"}, None),
        # Malformed acknowledgements are not a turn-away; they keep the
        # recovery path.
        ({"v": protocol.PROTOCOL_VERSION, "ok": True, "status": "busy"}, None),
        ({"v": protocol.PROTOCOL_VERSION, "status": "expired"}, None),
        ({"v": protocol.PROTOCOL_VERSION, "ok": False, "status": "invalid"}, None),
        ({"v": protocol.PROTOCOL_VERSION, "ok": False, "error": "unsupported"}, None),
        (None, None),
    ],
)
async def test_surface_maps_each_daemon_reply(
    monkeypatch: pytest.MonkeyPatch, reply: dict | None, expected: dict | None
) -> None:
    from memtomem_stm.cli.hook_adapter import ClaudeHookAdapter
    from memtomem_stm.daemon import client

    sent: list[str] = []

    async def _request(hs, op, payload, **kwargs):
        sent.append(op)
        return reply

    monkeypatch.setattr(client, "_live_handshake_candidate", lambda config: {"token": "t"})
    monkeypatch.setattr(client, "_request", _request)
    call = ClaudeHookAdapter().parse(
        {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_response": "x"}
    )
    assert await client.surface(STMConfig(), call, timeout=1.0) == expected
    assert sent == [protocol.OP_SURFACE]


def test_remove_handshake_if_owner(tmp_path: Path):
    path = discovery.handshake_path(tmp_path, "fp")
    discovery.write_handshake(
        path,
        pid=10,
        host="127.0.0.1",
        port=1,
        token="t",
        config_fingerprint="fp",
        created_at=0.0,
        idle_shutdown_seconds=900.0,
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
# contract that ops commands degrade instead of raising on a corrupted /
# hand-edited handshake, which read_handshake's docstring calls an anticipated
# input (it validates dict-ness only, not field types).
#
# Degrading means exit 0 for a handshake that still PARSES: garbage field
# values are read tolerantly and the command completes. One that does not
# parse is a different answer — it leaves the daemon's state unknown, and a
# command that cannot confirm the state it was asked for exits 1 rather than
# claiming it (#954).


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


def _fake_stop_clock(monkeypatch: pytest.MonkeyPatch, on_sleep=None) -> dict[str, float]:
    """Drive ``stop``'s wait with a fake clock: each ``sleep`` advances it and
    calls ``on_sleep(polls)``, so a test can remove a handshake mid-wait."""
    from types import SimpleNamespace

    clock = {"t": 0.0, "polls": 0}

    def sleep(seconds: float) -> None:
        clock["t"] += seconds
        clock["polls"] += 1
        if on_sleep is not None:
            on_sleep(clock["polls"])

    monkeypatch.setattr(
        "memtomem_stm.cli.daemon_cmd.time", SimpleNamespace(time=lambda: clock["t"], sleep=sleep)
    )
    return clock


class TestHandshakeRemovedPredicate:
    """``_handshake_removed`` decides when ``mms daemon stop`` may report a stop.

    Exercised directly as well as through the CLI: the symlink cases below need
    a real filesystem *and* the privilege to create links, which an unelevated
    Windows lane does not have — and Windows gates merge here, so the
    distinction this predicate exists for must be pinned somewhere that runs
    everywhere.
    """

    def test_reports_removed_only_for_a_missing_entry(self, tmp_path: Path):
        from memtomem_stm.cli.daemon_cmd import _handshake_removed

        present = tmp_path / "hs.json"
        present.write_text("{}", encoding="utf-8")
        assert _handshake_removed(present) is False
        assert _handshake_removed(tmp_path / "absent.json") is True

    def test_does_not_follow_the_entry(self, tmp_path: Path):
        """``stat`` would answer for the target and call this removed."""
        from memtomem_stm.cli.daemon_cmd import _handshake_removed

        path = SimpleNamespace(
            lstat=lambda: SimpleNamespace(st_mode=0),
            stat=lambda: (_ for _ in ()).throw(FileNotFoundError()),
        )
        assert _handshake_removed(path) is False  # type: ignore[arg-type]

    def test_keeps_waiting_when_the_entry_cannot_be_reached(self, tmp_path: Path):
        """A read error is not an absence — teardown removes the file last, so
        an unanswerable path must hold the wait open rather than end it."""
        from memtomem_stm.cli.daemon_cmd import _handshake_removed

        path = SimpleNamespace(lstat=lambda: (_ for _ in ()).throw(PermissionError()))
        assert _handshake_removed(path) is False  # type: ignore[arg-type]

    def test_a_dangling_ancestor_is_not_a_removed_entry(self, tmp_path: Path):
        """A broken directory link raises the same FileNotFoundError while
        saying nothing about the handshake."""
        from memtomem_stm.cli.daemon_cmd import _handshake_removed

        link = tmp_path / "data"
        try:
            link.symlink_to(tmp_path / "gone", target_is_directory=True)
        except (OSError, NotImplementedError):  # unprivileged Windows
            pytest.skip("symlink creation not permitted here")
        assert _handshake_removed(link / "hs.json") is False

    def test_a_working_ancestor_link_still_reports_a_removed_entry(self, tmp_path: Path):
        """The dangling-ancestor guard must not hang the wait on a data_dir
        that is simply a symlink to a real directory."""
        from memtomem_stm.cli.daemon_cmd import _handshake_removed

        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "data"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):  # unprivileged Windows
            pytest.skip("symlink creation not permitted here")
        assert _handshake_removed(link / "hs.json") is True

    def test_a_missing_directory_reports_a_removed_entry(self, tmp_path: Path):
        from memtomem_stm.cli.daemon_cmd import _handshake_removed

        assert _handshake_removed(tmp_path / "never" / "made" / "hs.json") is True

    def test_an_ancestor_that_raises_is_not_a_removed_entry(self, tmp_path: Path):
        """The walk runs inside the FileNotFoundError handler, where the
        function's own ``except OSError`` cannot see what it raises — so it
        guards itself rather than leaving the command as a traceback."""
        from memtomem_stm.cli.daemon_cmd import _handshake_removed

        def _boom() -> bool:
            raise PermissionError()

        blocked = SimpleNamespace(exists=_boom, is_symlink=_boom, is_junction=_boom)
        path = SimpleNamespace(
            lstat=lambda: (_ for _ in ()).throw(FileNotFoundError()),
            parents=[blocked],
        )
        assert _handshake_removed(path) is False  # type: ignore[arg-type]

    def test_no_existing_ancestor_at_all_is_not_a_removed_entry(self, tmp_path: Path):
        """Only an ancestor that EXISTS proves the path was walkable. A root
        that is itself unreachable — a disconnected drive or UNC share on
        Windows — answers nothing, so the wait must stay open rather than
        report a stop."""
        from memtomem_stm.cli.daemon_cmd import _handshake_removed

        unreachable = SimpleNamespace(exists=lambda: False, is_symlink=lambda: False)
        unreachable.is_junction = lambda: False  # type: ignore[attr-defined]
        path = SimpleNamespace(
            lstat=lambda: (_ for _ in ()).throw(FileNotFoundError()),
            parents=[unreachable, unreachable],
        )
        assert _handshake_removed(path) is False  # type: ignore[arg-type]


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

    def test_graceful_ack_waits_for_confirmed_teardown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from types import SimpleNamespace

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _write_handshake(
            {
                "pid": 12345,
                "host": "127.0.0.1",
                "port": 4567,
                "token": "t",
            }
        )

        async def fake_shutdown(config):
            return True

        monkeypatch.setattr("memtomem_stm.daemon.client.shutdown", fake_shutdown)
        clock = {"t": 0.0}
        monkeypatch.setattr(
            "memtomem_stm.cli.daemon_cmd.time",
            SimpleNamespace(
                time=lambda: clock["t"],
                sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
            ),
        )

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 1, result.output
        assert "accepted shutdown but is still cleaning up" in result.output
        assert "daemon stopped" not in result.output

    def test_graceful_ack_reports_stop_when_recorded_port_is_reassigned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#954: the recorded host/port identified the daemon only when we read
        it. Once teardown removed the handshake the daemon is stopped, even if
        the OS handed that ephemeral port to an unrelated listener meanwhile —
        probing it used to spin out the full wait and exit 1 on a clean stop."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        hs_path = _write_handshake({"pid": 12345, "host": "127.0.0.1", "port": 4567, "token": "t"})

        async def fake_shutdown(config):
            # Teardown completes: the handshake, removed last, is gone.
            hs_path.unlink()
            return True

        monkeypatch.setattr("memtomem_stm.daemon.client.shutdown", fake_shutdown)
        # Something else now answers on the port the handshake recorded.
        monkeypatch.setattr(
            "memtomem_stm.daemon.client.probe_listening",
            lambda host, port, **kwargs: True,
        )

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 0, result.output
        assert "daemon stopped" in result.output
        assert "still cleaning up" not in result.output

    def test_graceful_ack_does_not_read_an_unparseable_handshake_as_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#954: ``read_handshake`` returns None for absent, unreadable AND
        malformed files. Only actual removal ends the wait — a handshake we
        cannot parse means teardown may still be running, so the command must
        keep waiting rather than announce a stop that never happened."""
        from types import SimpleNamespace

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        hs_path = _write_handshake({"pid": 12345, "host": "127.0.0.1", "port": 4567, "token": "t"})

        async def fake_shutdown(config):
            # Accepted, and the record is now unparseable but still present.
            hs_path.write_text("{ truncated", encoding="utf-8")
            return True

        monkeypatch.setattr("memtomem_stm.daemon.client.shutdown", fake_shutdown)
        clock = {"t": 0.0}
        monkeypatch.setattr(
            "memtomem_stm.cli.daemon_cmd.time",
            SimpleNamespace(
                time=lambda: clock["t"],
                sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
            ),
        )

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 1, result.output
        assert "accepted shutdown but is still cleaning up" in result.output
        assert "daemon stopped" not in result.output
        assert hs_path.exists()

    def test_graceful_ack_does_not_read_a_dangling_symlink_as_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#954: the wait asks whether the directory entry teardown removes is
        gone. ``stat()`` answers for a link's target, so a dangling handshake
        symlink would read as removed while the entry is still there."""
        from types import SimpleNamespace

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        hs_path = _write_handshake({"pid": 12345, "host": "127.0.0.1", "port": 4567, "token": "t"})

        async def fake_shutdown(config):
            hs_path.unlink()
            try:
                hs_path.symlink_to(tmp_path / "gone.json")
            except (OSError, NotImplementedError):  # unprivileged Windows
                pytest.skip("symlink creation not permitted here")
            return True

        monkeypatch.setattr("memtomem_stm.daemon.client.shutdown", fake_shutdown)
        clock = {"t": 0.0}
        monkeypatch.setattr(
            "memtomem_stm.cli.daemon_cmd.time",
            SimpleNamespace(
                time=lambda: clock["t"],
                sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
            ),
        )

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 1, result.output
        assert "accepted shutdown but is still cleaning up" in result.output
        assert "daemon stopped" not in result.output

    def test_declined_shutdown_with_an_unreadable_handshake_is_not_no_daemon(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#954: a handshake that was already unparseable when `stop` ran makes
        both the read and the graceful call come back empty — but 'no daemon'
        is only one of the three things that produces that, and the record may
        belong to a daemon that merely declined. Say so instead."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        hs_path = _write_handshake({"pid": 12345, "host": "127.0.0.1", "port": 4567, "token": "t"})
        hs_path.write_text("{ truncated", encoding="utf-8")
        _no_daemon(monkeypatch)

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 1, result.output
        assert "unreadable" in result.output
        assert "no running daemon" not in result.output
        assert hs_path.exists()  # never unlinked on a record we cannot read

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

    @pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM fallback")
    def test_stale_handshake_alive_pid_gets_sigterm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Graceful shutdown declined but the endpoint still accepts a connect →
        the daemon is there-but-unresponsive, so SIGTERM its recorded pid, then
        report the stop only once teardown has removed the handshake (#1238)."""
        import signal as _signal

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        hs = _write_handshake({"pid": 12345, "host": "127.0.0.1", "port": 1, "token": "t"})
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        monkeypatch.setattr(
            "memtomem_stm.daemon.client.probe_listening", lambda host, port, **kw: True
        )
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
        # Teardown finishes a few polls into the wait, not at signal time.
        _fake_stop_clock(monkeypatch, on_sleep=lambda polls: polls == 3 and hs.unlink())

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 0, result.output
        assert "sent SIGTERM to daemon pid=12345" in result.output
        assert "daemon stopped" in result.output
        assert killed == [(12345, _signal.SIGTERM)]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM fallback")
    def test_sigterm_without_teardown_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#1238: a signalled daemon whose handshake is still there when the
        budget runs out is not confirmed stopped — exit 1, naming the pid."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        hs = _write_handshake({"pid": 12345, "host": "127.0.0.1", "port": 1, "token": "t"})
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        monkeypatch.setattr(
            "memtomem_stm.daemon.client.probe_listening", lambda host, port, **kw: True
        )
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
        clock = _fake_stop_clock(monkeypatch)

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 1, result.output
        assert "daemon pid=12345 did not finish shutting down after SIGTERM" in result.stderr
        assert "daemon stopped" not in result.output
        assert hs.exists()  # never treated as stale
        assert clock["t"] >= 5.0  # the whole budget was spent

    @pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM fallback")
    def test_sigterm_permission_error_keeps_handshake_and_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#1238: ``is_pid_alive`` counts EPERM as alive, and the endpoint
        listens — a daemon we may not signal is not a stale record. It used to
        be unlinked and reported as cleaned up with exit 0."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        hs = _write_handshake({"pid": 12345, "host": "127.0.0.1", "port": 1, "token": "t"})
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        monkeypatch.setattr(
            "memtomem_stm.daemon.client.probe_listening", lambda host, port, **kw: True
        )

        def fake_kill(pid: int, sig: int) -> None:
            raise PermissionError("EPERM")

        monkeypatch.setattr(os, "kill", fake_kill)

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 1, result.output
        assert "could not signal daemon pid=12345 (PermissionError)" in result.stderr
        assert "cleaned stale handshake" not in result.output
        assert hs.exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM fallback")
    def test_sigterm_process_gone_is_stale_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The daemon exited between the probe and the signal: nothing is left
        running, so the record is stale and cleaned up as before (exit 0)."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        hs = _write_handshake({"pid": 12345, "host": "127.0.0.1", "port": 1, "token": "t"})
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        monkeypatch.setattr(
            "memtomem_stm.daemon.client.probe_listening", lambda host, port, **kw: True
        )

        def fake_kill(pid: int, sig: int) -> None:
            raise ProcessLookupError("ESRCH")

        monkeypatch.setattr(os, "kill", fake_kill)

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 0, result.output
        assert "cleaned stale handshake" in result.output
        assert not hs.exists()

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
        # The pid is named: the record is gone after this, and a daemon already
        # shutting down on its own lands here too (#1238).
        assert "no responsive daemon pid=12345; cleaned stale handshake" in result.output
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
        # #1238: nothing stopped it, so this is not a success.
        assert result.exit_code == 1, result.output
        assert "Windows does not provide the POSIX SIGTERM fallback" in result.stderr
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

    def test_status_json_carries_the_latency_the_guidance_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The concurrency and hook-timeout guidance both tell operators to read
        # these numbers from `mms daemon status`. The daemon has always sent
        # them; the status command dropped them on the floor (#874).
        import json
        import time as _time

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        latency = {
            "surface": {
                "samples": 12,
                "timeout_samples": 1,
                "p50_ms": 455.4,
                "p95_ms": 607.2,
                "recommendation": {"status": "provisional", "seconds": 1.5},
            }
        }

        async def fake_ping(config, *, timeout=2.0):
            return {
                "pid": 11,
                "host": "127.0.0.1",
                "port": 4567,
                "ltm": "warm",
                "created_at": _time.time(),
                "queue": {"active": 0, "in_flight": 0, "concurrency": 4},
                "latency": latency,
            }

        monkeypatch.setattr("memtomem_stm.daemon.client.ping", fake_ping)
        result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["latency"] == latency

        text = CliRunner().invoke(_cli(), ["daemon", "status"])
        assert text.exit_code == 0, text.output
        assert "in_flight=0/4" in text.output
        assert "p50=455ms" in text.output
        assert "suggested_timeout=1.5s" in text.output

    def test_status_shows_a_failing_daemon_that_has_no_successful_samples(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A round trip that failed mid-flight is filed as an error rather than
        # a success duration (#994). Gating the whole latency line on
        # ``samples`` therefore hid exactly the daemon an operator runs this
        # command to diagnose: every search failing reads as no line at all,
        # which is indistinguishable from a daemon nothing has used yet.
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
                "queue": {"active": 0, "in_flight": 0, "concurrency": 4},
                "latency": {
                    "surface": {
                        "samples": 0,
                        "timeout_samples": 0,
                        "error_samples": 7,
                        "p50_ms": None,
                        "p95_ms": None,
                        "recommendation": {"status": "collecting", "seconds": None},
                    }
                },
            }

        monkeypatch.setattr("memtomem_stm.daemon.client.ping", fake_ping)
        text = CliRunner().invoke(_cli(), ["daemon", "status"])

        assert text.exit_code == 0, text.output
        assert "errors=7" in text.output
        assert "no successful warm searches yet" in text.output
        # Percentiles over an empty window render as 0ms, which reads as
        # "instant" — the opposite of what this daemon is doing.
        assert "p50=" not in text.output
        assert "suggested_timeout=" not in text.output

    def test_status_prints_every_counter_that_turns_the_line_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A cold-only or pre-RPC-fault-only daemon switches the line on. If the
        # line then shows only samples/timeouts/errors it reads as a positive
        # report that nothing has happened -- worse than printing no line,
        # because the operator stops looking. Every counter in the gate is on
        # the line.
        import time as _time

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))

        async def fake_ping(config, *, timeout=2.0):
            return {
                "pid": 11,
                "host": "127.0.0.1",
                "port": 4567,
                "ltm": "cold",
                "created_at": _time.time(),
                "queue": {"active": 0, "in_flight": 0, "concurrency": 4},
                "latency": {
                    "surface": {
                        "samples": 0,
                        "timeout_samples": 0,
                        "error_samples": 0,
                        "cold_samples": 9,
                        "pre_rpc_faults": 4,
                        "p50_ms": None,
                        "p95_ms": None,
                    }
                },
            }

        monkeypatch.setattr("memtomem_stm.daemon.client.ping", fake_ping)
        text = CliRunner().invoke(_cli(), ["daemon", "status"])

        assert text.exit_code == 0, text.output
        assert "cold=9" in text.output
        assert "pre_rpc_faults=4" in text.output
        # "warm" is load-bearing: a cold-only daemon may have completed
        # searches, just none the percentiles accept.
        assert "no successful warm searches yet" in text.output
        assert "p50=" not in text.output

    def test_status_omits_the_latency_line_when_nothing_was_observed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The control for the test above: a daemon with no observations at all
        # still prints no latency line, so widening the condition did not turn
        # it into a permanent fixture that says nothing.
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
                "queue": {"active": 0, "in_flight": 0, "concurrency": 4},
                "latency": {
                    "surface": {
                        "samples": 0,
                        "timeout_samples": 0,
                        "error_samples": 0,
                        "cold_samples": 0,
                        "pre_rpc_faults": 0,
                    }
                },
            }

        monkeypatch.setattr("memtomem_stm.daemon.client.ping", fake_ping)
        text = CliRunner().invoke(_cli(), ["daemon", "status"])

        assert text.exit_code == 0, text.output
        assert "surface latency" not in text.output

    def test_status_tolerates_a_snapshot_without_the_newer_counters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The ping payload crosses a version boundary: an older daemon still
        # running on this machine answers without ``error_samples`` or
        # ``pre_rpc_faults``. The line must render those as zero rather than
        # raising, and the counters it does have must still be right.
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
                "queue": {"active": 0, "in_flight": 0, "concurrency": 4},
                "latency": {
                    "surface": {
                        "samples": 3,
                        "timeout_samples": 1,
                        "p50_ms": 300.0,
                        "p95_ms": 400.0,
                        "recommendation": {"status": "collecting", "seconds": None},
                    }
                },
            }

        monkeypatch.setattr("memtomem_stm.daemon.client.ping", fake_ping)
        text = CliRunner().invoke(_cli(), ["daemon", "status"])

        assert text.exit_code == 0, text.output
        assert "samples=3" in text.output
        # Absent is not zero: this daemon does not report those counters at
        # all, and printing 0 would read as "measured, and there were none".
        assert "errors=?" in text.output
        assert "pre_rpc_faults=?" in text.output
        assert "p50=300ms" in text.output

    def test_unreadable_handshake_is_stale_not_stopped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#954: a record we cannot parse is still a record. Reporting it as
        `stopped` is what sends someone off to start a replacement beside a
        daemon that may be mid-teardown — and `stop`'s timeout now points here
        for exactly this case."""
        import json

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        hs_path = _write_handshake({"pid": 12345, "host": "127.0.0.1", "port": 4567, "token": "t"})
        hs_path.write_text("{ truncated", encoding="utf-8")
        _no_daemon(monkeypatch)

        result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)
        assert info["state"] == "stale"
        assert info["handshake_unreadable"] is True

        plain = CliRunner().invoke(_cli(), ["daemon", "status"])
        assert plain.exit_code == 0, plain.output
        assert "unreadable" in plain.output
        assert "stopped" not in plain.output

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
    """`#517` — daemons under another config fingerprint (config/protocol drift)
    must be visible to ``status`` and stoppable via ``stop --all``, even when
    pinned with ``idle_timeout_seconds=0`` (so they never self-clear). `#1195` —
    ``status`` must not call one that exits on its own once idle stale.

    Liveness is two-factor (#519): the recorded pid must be alive *and* its
    endpoint must accept a connect. These tests mock both factors
    (``is_pid_alive`` + ``client.probe_listening``), so they are platform-agnostic
    — the connect-probe is what re-enabled Windows, where ``is_pid_alive`` alone
    is uninformative. ``test_foreign_detection_cross_platform`` pins that, and
    ``test_recycled_pid_skipped_by_connect_probe`` pins the recycled-pid gate."""

    def _write_foreign(
        self,
        tmp_path: Path,
        fingerprint: str,
        pid: int,
        *,
        port: int = 4242,
        idle_shutdown_seconds: float | None = None,
    ) -> None:
        # Defaults to a daemon that never exits when idle: the #517 case these
        # tests were written for.
        discovery.write_handshake(
            discovery.handshake_path(tmp_path, fingerprint),
            pid=pid,
            host="127.0.0.1",
            port=port,
            token="secret-token",
            config_fingerprint=fingerprint,
            created_at=0.0,
            idle_shutdown_seconds=idle_shutdown_seconds,
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
            {
                "fingerprint": "foreignfp000000",
                "pid": 98765,
                "host": "127.0.0.1",
                "port": 4242,
                "idle_shutdown_seconds": None,
                "exits_when_idle": False,
                "listening": True,
                "lock": None,
            }
        ]
        assert "secret-token" not in result.output

        # Text surface: a warning line + the actionable hint.
        result = CliRunner().invoke(_cli(), ["daemon", "status"])
        assert result.exit_code == 0, result.output
        assert "pid=98765 fp=foreignfp000000" in result.output
        assert "stop --all" in result.output

    @staticmethod
    def _status_text(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        from click.testing import CliRunner

        monkeypatch.setenv("NO_COLOR", "1")
        result = CliRunner().invoke(_cli(), ["daemon", "status"])
        assert result.exit_code == 0, result.output
        return result.output.splitlines()

    @staticmethod
    def _status_json(monkeypatch: pytest.MonkeyPatch) -> dict:
        import json

        from click.testing import CliRunner

        result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
        assert result.exit_code == 0, result.output

        def strict(token: str) -> None:
            raise AssertionError(f"status --json emitted non-standard JSON {token}")

        return json.loads(result.output, parse_constant=strict)

    def test_status_does_not_call_a_self_expiring_foreign_daemon_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#1195: a daemon that shuts down once idle cannot outlive its users, so
        the status names its idle timeout and does not tell the operator to stop
        it — one still in use may belong to an STM process with a different
        environment, which may only start another."""
        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        self._write_foreign(tmp_path, "foreignfp000000", pid=98765, idle_shutdown_seconds=900.0)
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        _probe_all(monkeypatch)

        (entry,) = self._status_json(monkeypatch)["foreign"]
        assert (entry["idle_shutdown_seconds"], entry["exits_when_idle"]) == (900.0, True)

        lines = self._status_text(monkeypatch)
        i = lines.index(
            "1 daemon(s) running under a different config: "
            "pid=98765 fp=foreignfp000000 (exits after 900s idle)"
        )
        assert lines[i + 1] == "each exits on its own once idle; nothing to stop"
        assert not any("stop --all" in line or "orphan" in line for line in lines)

    @pytest.mark.parametrize(
        ("recorded", "expected"),
        [
            # What a daemon writes when its idle shutdown is off (0 or inf).
            ({"idle_shutdown_seconds": None}, (None, False)),
            # A build that predates the field.
            ({}, (None, None)),
            # Values no daemon writes: a hand-edited or corrupted handshake.
            ({"idle_shutdown_seconds": True}, (None, None)),
            ({"idle_shutdown_seconds": "900"}, (None, None)),
            ({"idle_shutdown_seconds": -1}, (None, None)),
            ({"idle_shutdown_seconds": 0}, (None, None)),
            ({"idle_shutdown_seconds": float("inf")}, (None, None)),
            ({"idle_shutdown_seconds": float("nan")}, (None, None)),
            # Valid JSON, but no float can hold it.
            ({"idle_shutdown_seconds": 10**400}, (None, None)),
        ],
        ids=["never", "absent", "bool", "string", "negative", "zero", "inf", "nan", "huge-int"],
    )
    def test_status_keeps_the_cleanup_hint_for_a_daemon_that_may_linger(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recorded: dict,
        expected: tuple,
    ):
        """The #517 case — a daemon that never exits when idle, or whose handshake
        does not say — keeps the warning and the `stop --all` hint, and the JSON
        stays standard even when the handshake holds `Infinity`/`NaN`."""
        import json

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        payload = {
            "v": discovery.HANDSHAKE_VERSION,
            "pid": 98765,
            "host": "127.0.0.1",
            "port": 4242,
            "token": "secret-token",
            "created_at": 0.0,
            "config_fingerprint": "foreignfp000000",
            **recorded,
        }
        discovery.handshake_path(tmp_path, "foreignfp000000").write_text(json.dumps(payload))
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        _probe_all(monkeypatch)
        monkeypatch.setattr("memtomem_stm.cli.daemon_cmd._can_force_terminate", lambda: True)

        (entry,) = self._status_json(monkeypatch)["foreign"]
        assert (entry["idle_shutdown_seconds"], entry["exits_when_idle"]) == expected

        label = "never exits when idle" if expected[1] is False else "idle timeout unknown"
        lines = self._status_text(monkeypatch)
        i = lines.index(
            f"1 daemon(s) running under a different config: pid=98765 fp=foreignfp000000 ({label})"
        )
        assert lines[i + 1] == (
            "if no running STM process uses them, stop them with `mms daemon stop --all` "
            "(it stops every daemon listed above)"
        )

    def test_status_mixed_foreign_daemons_label_each_and_keep_the_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """One lingering daemon is enough for the hint, and the hint says `--all`
        also stops the self-expiring one listed beside it."""
        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        self._write_foreign(tmp_path, "aaaa000000000000", pid=1001, idle_shutdown_seconds=0.5)
        self._write_foreign(tmp_path, "bbbb000000000000", pid=2002)
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        _probe_all(monkeypatch)
        monkeypatch.setattr("memtomem_stm.cli.daemon_cmd._can_force_terminate", lambda: True)

        lines = self._status_text(monkeypatch)
        i = lines.index(
            "2 daemon(s) running under a different config: "
            "pid=1001 fp=aaaa000000000000 (exits after 0.5s idle), "
            "pid=2002 fp=bbbb000000000000 (never exits when idle)"
        )
        assert lines[i + 1] == (
            "if no running STM process uses them, stop them with `mms daemon stop --all` "
            "(it stops every daemon listed above)"
        )

    def test_status_hint_does_not_point_windows_at_stop_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """`stop --all` refuses to signal by pid where there is no POSIX SIGTERM
        fallback, so the hint there names the way that works."""
        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        self._write_foreign(tmp_path, "foreignfp000000", pid=98765)
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        _probe_all(monkeypatch)
        monkeypatch.setattr("memtomem_stm.cli.daemon_cmd._can_force_terminate", lambda: False)

        lines = self._status_text(monkeypatch)
        i = lines.index(
            "1 daemon(s) running under a different config: "
            "pid=98765 fp=foreignfp000000 (never exits when idle)"
        )
        assert lines[i + 1] == (
            "if no running STM process uses them, end those daemon processes from "
            "Task Manager (`mms daemon stop --all` cannot signal them on Windows)"
        )

    def test_status_skips_an_undecodable_foreign_handshake(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import json

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        discovery.handshake_path(tmp_path, "foreignfp000000").write_bytes(b'{"pid": "\xff"}')
        _no_daemon(monkeypatch)

        result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])

        assert result.exit_code == 0, result.output
        info = json.loads(result.output)
        assert (info["state"], info["foreign"]) == ("stopped", [])

    def test_status_reports_an_undecodable_current_handshake_as_unreadable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import json

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        current = discovery.config_fingerprint(STMConfig())
        discovery.handshake_path(tmp_path, current).write_bytes(b'{"pid": "\xff"}')
        _no_daemon(monkeypatch)

        result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])

        assert result.exit_code == 0, result.output
        info = json.loads(result.output)
        assert (info["state"], info.get("handshake_unreadable")) == ("stale", True)

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

    @pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM fallback")
    def test_stop_all_sigterms_foreign(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import signal as _signal

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        self._write_foreign(tmp_path, "foreignfp000000", pid=98765)
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        _probe_all(monkeypatch)
        killed: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            killed.append((pid, sig))
            discovery.handshake_path(tmp_path, "foreignfp000000").unlink()  # teardown done

        monkeypatch.setattr(os, "kill", fake_kill)

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 0, result.output
        assert killed == [(98765, _signal.SIGTERM)]
        assert "sent SIGTERM to daemon pid=98765 (fp=foreignfp000000)" in result.output
        assert "daemon pid=98765 stopped (fp=foreignfp000000)" in result.output

    def test_stop_all_continues_foreign_cleanup_after_current_stop_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A current daemon may acknowledge shutdown but miss its teardown
        wait. Preserve that nonzero result only after ``--all`` has performed
        the independently requested foreign-daemon cleanup.
        """
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        foreign_cleanup: list[STMConfig] = []

        def current_timeout(_config: STMConfig) -> None:
            raise SystemExit(1)

        monkeypatch.setattr(
            "memtomem_stm.cli.daemon_cmd._stop_current_config_daemon", current_timeout
        )
        monkeypatch.setattr(
            "memtomem_stm.cli.daemon_cmd._stop_foreign_daemons", foreign_cleanup.append
        )

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 1
        assert len(foreign_cleanup) == 1

    @pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM fallback")
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

        def fake_kill(pid: int, sig: int) -> None:
            killed.append((pid, sig))
            discovery.handshake_path(tmp_path, old_fp).unlink()  # teardown done

        monkeypatch.setattr(os, "kill", fake_kill)

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
        assert (
            "pid=1001 fp=aaaa000000000000 (never exits when idle), "
            "pid=3003 fp=cccc000000000000 (never exits when idle)"
        ) in result.output
        assert "2002" not in result.output

    @pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM fallback")
    def test_stop_all_skips_dead_and_continues_on_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """`stop --all` must (a) never signal a dead-pid orphan, (b) keep going
        when one SIGTERM raises OSError, and (c) exit 1 for that failure alone
        (#1238) — the daemon it did signal is confirmed stopped, so the timeout
        cannot be what fails the command."""
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
                raise OSError("EPERM")
            killed.append((pid, sig))
            discovery.handshake_path(tmp_path, "cccc000000000000").unlink()  # teardown done

        monkeypatch.setattr(os, "kill", fake_kill)

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 1, result.output
        assert killed == [(3003, _signal.SIGTERM)]  # 2002 never tried, loop continued past 1001
        assert "could not signal daemon pid=1001" in result.stderr
        assert "sent SIGTERM to daemon pid=3003" in result.output
        assert "daemon pid=3003 stopped" in result.output
        assert "did not finish" not in result.output

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
            {
                "fingerprint": "foreignfp000000",
                "pid": 98765,
                "host": "127.0.0.1",
                "port": 4242,
                "idle_shutdown_seconds": None,
                "exits_when_idle": False,
                "listening": True,
                "lock": None,
            }
        ]
        assert "secret-token" not in result.output

        result = CliRunner().invoke(_cli(), ["daemon", "status"])
        assert result.exit_code == 0, result.output
        assert "running  pid=11" in result.output  # current daemon line
        assert "pid=98765 fp=foreignfp000000" in result.output  # foreign warning
        assert "secret-token" not in result.output

    @pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM fallback")
    def test_stop_all_signals_every_target_before_one_shared_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#1238: every foreign daemon is signalled before the wait starts, and
        they share one budget — staggered teardowns all confirm, exit 0."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        self._write_foreign(tmp_path, "aaaa000000000000", pid=1001)
        self._write_foreign(tmp_path, "cccc000000000000", pid=3003)
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        _probe_all(monkeypatch)
        killed: list[int] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
        signalled_at_first_poll: list[int] = []

        def on_sleep(polls: int) -> None:
            if polls == 1:
                signalled_at_first_poll.extend(killed)
            if polls == 2:
                discovery.handshake_path(tmp_path, "aaaa000000000000").unlink()
            if polls == 5:
                discovery.handshake_path(tmp_path, "cccc000000000000").unlink()

        _fake_stop_clock(monkeypatch, on_sleep=on_sleep)

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 0, result.output
        assert signalled_at_first_poll == [1001, 3003]
        assert "daemon pid=1001 stopped (fp=aaaa000000000000)" in result.output
        assert "daemon pid=3003 stopped (fp=cccc000000000000)" in result.output

    @pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM fallback")
    def test_stop_all_names_the_daemon_that_did_not_finish(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#1238: one target confirms and one does not → exit 1 naming only the
        survivor, after a single budget rather than one per daemon. The first
        confirms halfway through, so a fresh budget per daemon would run past
        7 s instead of ending at 5 s."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        self._write_foreign(tmp_path, "aaaa000000000000", pid=1001)
        self._write_foreign(tmp_path, "cccc000000000000", pid=3003)
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        _probe_all(monkeypatch)
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
        clock = _fake_stop_clock(
            monkeypatch,
            on_sleep=lambda polls: (
                polls == 50 and discovery.handshake_path(tmp_path, "aaaa000000000000").unlink()
            ),
        )

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 1, result.output
        assert "daemon pid=1001 stopped (fp=aaaa000000000000)" in result.output
        assert (
            "daemon pid=3003 (fp=cccc000000000000) did not finish shutting down after SIGTERM"
        ) in result.stderr
        assert "pid=1001 (fp=aaaa000000000000) did not finish" not in result.output
        assert 5.0 <= clock["t"] < 5.2  # one shared budget

    @pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM fallback")
    def test_stop_all_target_gone_before_signal_still_needs_its_handshake_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#1238: ESRCH means the process went away, not that it finished its
        teardown — a crashed daemon leaves its handshake, and that must not
        count as a confirmed stop."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        self._write_foreign(tmp_path, "aaaa000000000000", pid=1001)
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        _probe_all(monkeypatch)

        def fake_kill(pid: int, sig: int) -> None:
            raise ProcessLookupError("ESRCH")

        monkeypatch.setattr(os, "kill", fake_kill)
        _fake_stop_clock(monkeypatch)

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 1, result.output
        assert "daemon pid=1001 (fp=aaaa000000000000) did not finish" in result.stderr
        assert "could not signal" not in result.output

    def test_stop_all_on_windows_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#1238: Windows cannot signal them, so nothing was stopped."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        self._write_foreign(tmp_path, "aaaa000000000000", pid=1001)
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        _probe_all(monkeypatch)
        monkeypatch.setattr("memtomem_stm.cli.daemon_cmd._can_force_terminate", lambda: False)
        killed: list[int] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 1, result.output
        assert "Windows cannot safely" in result.stderr
        assert killed == []

    def test_stop_all_foreign_failure_fails_after_current_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The reverse of the current-timeout case: the current config stopped
        cleanly, but a foreign daemon did not — the command still fails."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        current: list[STMConfig] = []

        def foreign_timeout(_config: STMConfig) -> None:
            raise SystemExit(1)

        monkeypatch.setattr(
            "memtomem_stm.cli.daemon_cmd._stop_current_config_daemon", current.append
        )
        monkeypatch.setattr("memtomem_stm.cli.daemon_cmd._stop_foreign_daemons", foreign_timeout)

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 1
        assert len(current) == 1

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
            {
                "fingerprint": "foreignfp000000",
                "pid": 98765,
                "host": "127.0.0.1",
                "port": 4242,
                "idle_shutdown_seconds": None,
                "exits_when_idle": False,
                "listening": True,
                "lock": None,
            }
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

    @pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM fallback")
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
                    "idle_shutdown_seconds": None,
                    "exits_when_idle": False,
                    "listening": True,
                    "lock": None,
                }
            ]
        finally:
            srv.close()
        # Endpoint now closed → the alive pid alone is not enough to report it.
        assert daemon_cmd._live_foreign_daemons(STMConfig()) == []


_LISTING_ERRORS = [PermissionError(13, "denied"), NotADirectoryError(20, "not a directory")]


def _unlistable(data_dir: Path) -> None:
    """Make ``data_dir`` writable and searchable but not listable (#1249)."""
    os.chmod(data_dir, 0o300)


def _skip_unless_permission_bits_bind() -> None:
    if os.name == "nt":
        pytest.skip("POSIX permission bits")
    if os.geteuid() == 0:
        pytest.skip("root lists any directory")


def test_iter_foreign_handshakes_unlistable_dir_raises(tmp_path: Path):
    """#1249: ``Path.glob`` returned ``[]`` for a directory it could not list, so
    a published foreign handshake read as "no daemons"."""
    _skip_unless_permission_bits_bind()
    _put_handshake(tmp_path, "aaaa0000", pid=30)
    _unlistable(tmp_path)
    try:
        assert (tmp_path / "stm-daemon-aaaa0000.json").exists()
        with pytest.raises(PermissionError):
            discovery.iter_foreign_handshakes(tmp_path, "cur00000")
    finally:
        os.chmod(tmp_path, 0o700)


@pytest.mark.parametrize("error", _LISTING_ERRORS, ids=lambda e: type(e).__name__)
def test_iter_foreign_handshakes_listing_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
):
    real_listdir = os.listdir

    def listdir(path):
        if Path(path) == tmp_path:
            raise error
        return real_listdir(path)

    monkeypatch.setattr(discovery.os, "listdir", listdir)
    with pytest.raises(type(error)):
        discovery.iter_foreign_handshakes(tmp_path, "cur00000")


class TestForeignListingFailure:
    """#1249: a data dir that cannot be listed leaves the daemons under other
    configs unknown — ``stop --all`` fails and ``status`` says so, instead of
    both reporting that there are none."""

    @staticmethod
    def _fail_listing(monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
        def iter_foreign_handshakes(*args, **kwargs):
            raise error

        monkeypatch.setattr(
            "memtomem_stm.daemon.discovery.iter_foreign_handshakes", iter_foreign_handshakes
        )

    @pytest.mark.parametrize("error", _LISTING_ERRORS, ids=lambda e: type(e).__name__)
    def test_stop_all_fails_when_listing_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
    ):
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        self._fail_listing(monkeypatch, error)

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 1, result.output
        kind = type(error).__name__
        assert (
            f"could not list data_dir ({kind}) — daemons under a different config are "
            "unknown; no foreign daemons were stopped"
        ) in result.stderr
        assert "no daemons running under a different config" not in result.output
        assert str(tmp_path) not in result.output

    def test_stop_all_fails_on_a_real_unlistable_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        _skip_unless_permission_bits_bind()
        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        _put_handshake(tmp_path, "foreignfp000000", pid=98765)
        _unlistable(tmp_path)
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])
        finally:
            os.chmod(tmp_path, 0o700)

        assert result.exit_code == 1, result.output
        assert "could not list data_dir (PermissionError)" in result.stderr
        assert "no daemons running under a different config" not in result.output

    def test_stop_all_reports_both_failures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import click
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))

        def current_fails(_config: STMConfig) -> None:
            click.echo("current-config stop not confirmed", err=True)
            raise SystemExit(1)

        monkeypatch.setattr(
            "memtomem_stm.cli.daemon_cmd._stop_current_config_daemon", current_fails
        )
        self._fail_listing(monkeypatch, PermissionError(13, "denied"))

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 1, result.output
        assert "current-config stop not confirmed" in result.stderr
        assert "could not list data_dir (PermissionError)" in result.stderr

    @pytest.mark.parametrize("error", _LISTING_ERRORS, ids=lambda e: type(e).__name__)
    def test_status_reports_foreign_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
    ):
        import json

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("NO_COLOR", "1")
        _no_daemon(monkeypatch)
        self._fail_listing(monkeypatch, error)
        kind = type(error).__name__

        result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)
        assert info["state"] == "stopped"
        assert info["foreign"] is None
        assert info["foreign_error"] == kind

        result = CliRunner().invoke(_cli(), ["daemon", "status"])
        assert result.exit_code == 0, result.output
        assert (
            f"could not list data_dir ({kind}) — daemons under a different config are unknown"
        ) in result.output.splitlines()
        assert "under a different config:" not in result.output
        assert str(tmp_path) not in result.output

    def test_status_on_a_real_unlistable_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import json

        from click.testing import CliRunner

        _skip_unless_permission_bits_bind()
        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        _put_handshake(tmp_path, "foreignfp000000", pid=98765)
        _unlistable(tmp_path)
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
        finally:
            os.chmod(tmp_path, 0o700)

        assert result.exit_code == 0, result.output
        info = json.loads(result.output)
        assert (info["foreign"], info["foreign_error"]) == (None, "PermissionError")

    def test_listable_empty_dir_still_reports_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import json

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)

        result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
        assert result.exit_code == 0, result.output
        info = json.loads(result.output)
        assert info["foreign"] == []
        assert "foreign_error" not in info

        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])
        assert result.exit_code == 0, result.output
        assert "no daemons running under a different config" in result.output

    def test_a_probe_error_is_not_reported_as_a_listing_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Only the enumeration is translated: an ``OSError`` from a per-entry
        probe must not come back as "could not list data_dir"."""
        from memtomem_stm.cli import daemon_cmd

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _put_handshake(tmp_path, "foreignfp000000", pid=98765)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: False)

        def probe_lock(_path: Path):
            raise OSError("probe failed")

        monkeypatch.setattr(daemon_cmd, "_probe_lock", probe_lock)

        with pytest.raises(OSError, match="probe failed") as excinfo:
            daemon_cmd._live_foreign_daemons(STMConfig())
        assert not isinstance(excinfo.value, daemon_cmd._ForeignListingError)


class _LockHolder:
    """Hold a lifetime lock on a second fd, as a daemon (or prober) would.

    flock locks belong to the open file description, so this process's own
    probe on another fd sees it as held."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: int | None = open_lock_fd(path)
        assert try_lock(self._fd) is True

    def release(self) -> None:
        if self._fd is not None:
            release_lock(self._fd)
            self._fd = None


def _current_lock_path() -> Path:
    config = STMConfig()
    return lock_path(config.data_dir.expanduser(), discovery.config_fingerprint(config))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock semantics")
class TestStopJudgesByLifetimeLock:
    """#1245 — ``stop`` must not take a daemon that has closed its listener for a
    stale record while its lifetime lock is held: teardown removes the handshake
    and only then releases the lock, and a starting daemon holds the lock before
    it publishes."""

    def _stale_current(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, alive=True):
        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        _probe_all(monkeypatch, listening=False)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: alive)
        monkeypatch.setattr(os, "kill", lambda pid, sig: pytest.fail("must not signal"))
        return _write_handshake({"pid": 12345, "host": "127.0.0.1", "port": 1, "token": "t"})

    def test_held_lock_keeps_the_record_and_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        hs = self._stale_current(tmp_path, monkeypatch)
        clock = _fake_stop_clock(monkeypatch)
        holder = _LockHolder(_current_lock_path())
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop"])
        finally:
            holder.release()

        assert result.exit_code == 1, result.output
        assert hs.exists()
        assert clock["polls"] > 0  # it waited
        assert "recorded daemon pid=12345 is not accepting" in result.output
        assert "lock is still held" in result.output
        assert "cleaned stale handshake" not in result.output

    def test_teardown_finishing_mid_wait_reports_stopped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        hs = self._stale_current(tmp_path, monkeypatch)
        holder = _LockHolder(_current_lock_path())
        seen: list[bool] = []

        def on_sleep(polls: int) -> None:
            if polls == 3:
                seen.append(hs.exists())  # not unlinked by stop while held
                hs.unlink()  # the daemon's teardown...
                holder.release()  # ...then serve() releases the lock

        clock = _fake_stop_clock(monkeypatch, on_sleep)
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop"])
        finally:
            holder.release()

        assert result.exit_code == 0, result.output
        assert seen == [True]
        assert clock["polls"] >= 3
        assert "daemon stopped" in result.output
        assert "cleaned stale handshake" not in result.output

    def test_lock_released_with_record_left_cleans_it_after_release(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A daemon that died mid-start releases the lock and leaves the
        predecessor's record: stale, but only once the lock is free."""
        from click.testing import CliRunner

        hs = self._stale_current(tmp_path, monkeypatch)
        holder = _LockHolder(_current_lock_path())
        seen: list[bool] = []

        def on_sleep(polls: int) -> None:
            if polls == 3:
                seen.append(hs.exists())
                holder.release()

        _fake_stop_clock(monkeypatch, on_sleep)
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop"])
        finally:
            holder.release()

        assert result.exit_code == 0, result.output
        assert seen == [True]
        assert not hs.exists()
        assert "no responsive daemon pid=12345; cleaned stale handshake" in result.output

    def test_handshake_gone_but_lock_held_keeps_waiting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The lock is the terminal condition: a handshake already removed while
        the lock is held is a teardown still running, not a stop."""
        from click.testing import CliRunner

        hs = self._stale_current(tmp_path, monkeypatch)
        holder = _LockHolder(_current_lock_path())

        def on_sleep(polls: int) -> None:
            if polls == 1:
                hs.unlink()

        _fake_stop_clock(monkeypatch, on_sleep)
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop"])
        finally:
            holder.release()

        assert result.exit_code == 1, result.output
        assert "lock is still held" in result.output
        assert "daemon stopped" not in result.output

    def test_free_lock_unlinks_while_holding_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from pathlib import Path as _Path

        from click.testing import CliRunner

        hs = self._stale_current(tmp_path, monkeypatch)
        lp = _current_lock_path()
        real_unlink = _Path.unlink
        observed: list[bool] = []

        def spying_unlink(self, missing_ok=False):
            if self == hs:
                fd = open_lock_fd(lp)
                try:
                    observed.append(try_lock(fd))
                finally:
                    release_lock(fd)
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(_Path, "unlink", spying_unlink)
        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 0, result.output
        assert observed == [False]  # a successor could not have taken the lock
        assert not hs.exists()
        assert "cleaned stale handshake" in result.output

    def test_unopenable_lock_is_unknown_not_free(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        hs = self._stale_current(tmp_path, monkeypatch)
        _fake_stop_clock(monkeypatch)

        def refuse(path):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("memtomem_stm.daemon.locking._open_lock_fd", refuse)
        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 1, result.output
        assert hs.exists()
        assert "lock could not be checked" in result.output
        assert "still held" not in result.output

    def test_dead_recorded_pid_does_not_skip_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A successor may own the fingerprint while its predecessor's record,
        naming a dead pid, is still on disk."""
        from click.testing import CliRunner

        hs = self._stale_current(tmp_path, monkeypatch, alive=False)
        _fake_stop_clock(monkeypatch)
        holder = _LockHolder(_current_lock_path())
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop"])
        finally:
            holder.release()

        assert result.exit_code == 1, result.output
        assert hs.exists()

    def test_no_handshake_waits_for_a_held_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        holder = _LockHolder(_current_lock_path())

        def on_sleep(polls: int) -> None:
            if polls == 2:
                holder.release()

        clock = _fake_stop_clock(monkeypatch, on_sleep)
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop"])
        finally:
            holder.release()

        assert result.exit_code == 0, result.output
        assert clock["polls"] >= 2
        assert "no running daemon" in result.output

    def test_no_handshake_and_no_lock_file_creates_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        data_dir = tmp_path / "never-created"
        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(data_dir))
        _no_daemon(monkeypatch)

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])

        assert result.exit_code == 0, result.output
        assert "no running daemon" in result.output
        assert not data_dir.exists()

    def test_broken_lock_link_is_unknown_not_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``exists`` reads a dangling or looping link as absent; the entry is
        there, so the probe answers — and cannot open it."""
        import json

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        _fake_stop_clock(monkeypatch)
        lp = _current_lock_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.symlink_to(lp)  # self-referential: open() raises ELOOP

        result = CliRunner().invoke(_cli(), ["daemon", "stop"])
        assert result.exit_code == 1, result.output
        assert "lock could not be" in result.output
        assert "no running daemon" not in result.output

        status = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
        assert (json.loads(status.output)["state"], json.loads(status.output)["lock"]) == (
            "unknown",
            "unknown",
        )

    def test_status_reports_lock_states(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import json

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        _probe_all(monkeypatch, listening=False)

        def status() -> dict:
            result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
            assert result.exit_code == 0, result.output
            return json.loads(result.output)

        assert status()["state"] == "stopped"
        holder = _LockHolder(_current_lock_path())
        try:
            assert (status()["state"], status()["lock"]) == ("starting", "held")
            text = CliRunner().invoke(_cli(), ["daemon", "status"]).output
            assert "no handshake yet, lock held" in text
            _write_handshake({"pid": 12345, "host": "127.0.0.1", "port": 1, "token": "t"})
            assert (status()["state"], status()["lock"]) == ("stale", "held")
            text = CliRunner().invoke(_cli(), ["daemon", "status"]).output
            assert "starting or shutting down" in text
            assert "stale handshake" not in text
        finally:
            holder.release()
        assert (status()["state"], status()["lock"]) == ("stale", "free")

    def test_status_unreadable_handshake_reports_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import json

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        current = discovery.config_fingerprint(STMConfig())
        discovery.handshake_path(tmp_path, current).write_bytes(b'{"pid": "\xff"}')

        def status() -> dict:
            result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
            assert result.exit_code == 0, result.output
            return json.loads(result.output)

        assert (status()["handshake_unreadable"], status()["lock"]) == (True, "free")
        holder = _LockHolder(_current_lock_path())
        try:
            assert status()["lock"] == "held"
        finally:
            holder.release()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock semantics")
class TestForeignStopJudgesByLifetimeLock:
    """#1245 — the foreign side: a daemon under another fingerprint that has
    closed its listener but holds its lock is listed, never signalled, and
    waited on."""

    FP = "foreignfp000000"

    def _foreign(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, listening=False):
        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        _probe_all(monkeypatch, listening=listening)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        _put_handshake(tmp_path, self.FP, pid=98765)
        return discovery.handshake_path(tmp_path, self.FP)

    def test_lock_only_daemon_is_listed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import json

        from click.testing import CliRunner

        from memtomem_stm.cli import daemon_cmd

        self._foreign(tmp_path, monkeypatch)
        assert daemon_cmd._live_foreign_daemons(STMConfig()) == []  # lock free → stale
        holder = _LockHolder(lock_path(tmp_path, self.FP))
        try:
            [entry] = daemon_cmd._live_foreign_daemons(STMConfig())
            assert (entry["pid"], entry["listening"], entry["lock"]) == (98765, False, "held")
            info = json.loads(CliRunner().invoke(_cli(), ["daemon", "status", "--json"]).output)
            assert [d["fingerprint"] for d in info["foreign"]] == [self.FP]
            text = CliRunner().invoke(_cli(), ["daemon", "status"]).output
        finally:
            holder.release()
        assert "1 daemon(s) under a different config" in text
        assert "running under a different config" not in text
        assert "not accepting; lock held — starting or shutting down" in text
        assert "nothing to stop" not in text
        assert "waits for the others to finish" in text

    def test_unreadable_record_with_held_lock_is_listed_without_a_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        from memtomem_stm.cli import daemon_cmd

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        discovery.handshake_path(tmp_path, self.FP).write_bytes(b'{"pid": "\xff"}')
        holder = _LockHolder(lock_path(tmp_path, self.FP))
        try:
            [entry] = daemon_cmd._live_foreign_daemons(STMConfig())
            text = CliRunner().invoke(_cli(), ["daemon", "status"]).output
        finally:
            holder.release()
        assert (entry["pid"], entry["handshake_unreadable"], entry["lock"]) == (None, True, "held")
        assert f"pid=? fp={self.FP} (handshake unreadable; lock held" in text

    def test_unknown_lock_is_listed_as_unchecked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        self._foreign(tmp_path, monkeypatch)

        def refuse(path):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("memtomem_stm.daemon.locking._open_lock_fd", refuse)
        text = CliRunner().invoke(_cli(), ["daemon", "status"]).output
        assert "not accepting; lock could not be checked" in text

    def test_stop_all_waits_on_lock_only_without_signalling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        hs = self._foreign(tmp_path, monkeypatch)
        monkeypatch.setattr(os, "kill", lambda pid, sig: pytest.fail("must not signal"))
        holder = _LockHolder(lock_path(tmp_path, self.FP))

        def on_sleep(polls: int) -> None:
            if polls == 2:
                hs.unlink()
                holder.release()

        _fake_stop_clock(monkeypatch, on_sleep)
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])
        finally:
            holder.release()

        assert result.exit_code == 0, result.output
        assert "waiting for it without a signal" in result.output
        assert f"daemon pid=98765 stopped (fp={self.FP})" in result.output

    def test_lock_only_handshake_gone_but_lock_held_times_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The lock, not the handshake, ends a lock-only wait."""
        from click.testing import CliRunner

        hs = self._foreign(tmp_path, monkeypatch)
        monkeypatch.setattr(os, "kill", lambda pid, sig: pytest.fail("must not signal"))
        holder = _LockHolder(lock_path(tmp_path, self.FP))

        def on_sleep(polls: int) -> None:
            if polls == 1:
                hs.unlink()

        _fake_stop_clock(monkeypatch, on_sleep)
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])
        finally:
            holder.release()

        assert result.exit_code == 1, result.output
        assert "still holds its lock" in result.output
        assert "stopped (fp=" not in result.output

    def test_lock_only_lock_freed_with_record_left_succeeds_without_unlinking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        hs = self._foreign(tmp_path, monkeypatch)
        monkeypatch.setattr(os, "kill", lambda pid, sig: pytest.fail("must not signal"))
        holder = _LockHolder(lock_path(tmp_path, self.FP))

        def on_sleep(polls: int) -> None:
            if polls == 2:
                holder.release()

        _fake_stop_clock(monkeypatch, on_sleep)
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])
        finally:
            holder.release()

        assert result.exit_code == 0, result.output
        assert f"daemon pid=98765 stopped (fp={self.FP})" in result.output
        assert hs.exists()  # foreign records are never unlinked

    def test_listener_closing_after_enumeration_joins_the_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The action-time re-probe used to ``continue`` silently and exit 0."""
        from click.testing import CliRunner

        self._foreign(tmp_path, monkeypatch, listening=True)
        answers = iter([True])  # enumeration sees it accepting, the re-probe does not
        monkeypatch.setattr(
            "memtomem_stm.daemon.client.probe_listening",
            lambda host, port, **kw: next(answers, False),
        )
        monkeypatch.setattr(os, "kill", lambda pid, sig: pytest.fail("must not signal"))
        _fake_stop_clock(monkeypatch)
        holder = _LockHolder(lock_path(tmp_path, self.FP))
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])
        finally:
            holder.release()

        assert result.exit_code == 1, result.output
        assert "still holds its lock" in result.output

    def test_windows_reclassifies_a_closed_listener_before_refusing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A target that stopped accepting since the listing no longer needs a
        signal, so Windows' refusal must not fire for it."""
        from click.testing import CliRunner

        hs = self._foreign(tmp_path, monkeypatch, listening=True)
        answers = iter([True])  # enumeration sees it accepting, the re-probe does not
        monkeypatch.setattr(
            "memtomem_stm.daemon.client.probe_listening",
            lambda host, port, **kw: next(answers, False),
        )
        monkeypatch.setattr("memtomem_stm.cli.daemon_cmd._can_force_terminate", lambda: False)
        monkeypatch.setattr(os, "kill", lambda pid, sig: pytest.fail("must not signal"))
        holder = _LockHolder(lock_path(tmp_path, self.FP))

        def on_sleep(polls: int) -> None:
            if polls == 2:
                hs.unlink()
                holder.release()

        _fake_stop_clock(monkeypatch, on_sleep)
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])
        finally:
            holder.release()

        assert result.exit_code == 0, result.output
        assert "Windows cannot safely" not in result.output
        assert f"daemon pid=98765 stopped (fp={self.FP})" in result.output

    def test_mixed_targets_each_resolve(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import signal as _signal

        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        # aaaa accepts on port 7; bbbb (port 1) holds its lock only.
        _put_handshake(tmp_path, "bbbb000000000000", pid=22222)
        discovery.write_handshake(
            discovery.handshake_path(tmp_path, "aaaa000000000000"),
            pid=11111,
            host="127.0.0.1",
            port=7,
            token="t",
            config_fingerprint="aaaa000000000000",
            created_at=0.0,
            idle_shutdown_seconds=None,
        )
        monkeypatch.setattr(
            "memtomem_stm.daemon.client.probe_listening", lambda host, port, **kw: port == 7
        )
        killed: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            killed.append((pid, sig))
            discovery.handshake_path(tmp_path, "aaaa000000000000").unlink()

        monkeypatch.setattr(os, "kill", fake_kill)
        holder = _LockHolder(lock_path(tmp_path, "bbbb000000000000"))

        def on_sleep(polls: int) -> None:
            if polls == 3:
                discovery.handshake_path(tmp_path, "bbbb000000000000").unlink()
                holder.release()

        _fake_stop_clock(monkeypatch, on_sleep)
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])
        finally:
            holder.release()

        assert result.exit_code == 0, result.output
        assert killed == [(11111, _signal.SIGTERM)]
        assert "daemon pid=11111 stopped (fp=aaaa000000000000)" in result.output
        assert "daemon pid=22222 stopped (fp=bbbb000000000000)" in result.output

    def test_mixed_targets_share_one_budget(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """The signalled target finishes at 4 s; the lock-only one releases at
        6 s. One shared 5 s budget times the second out — a wait that restarted
        the budget for lock-only targets would report both stopped."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        _put_handshake(tmp_path, "bbbb000000000000", pid=22222)  # port 1: lock-only
        discovery.write_handshake(
            discovery.handshake_path(tmp_path, "aaaa000000000000"),
            pid=11111,
            host="127.0.0.1",
            port=7,
            token="t",
            config_fingerprint="aaaa000000000000",
            created_at=0.0,
            idle_shutdown_seconds=None,
        )
        monkeypatch.setattr(
            "memtomem_stm.daemon.client.probe_listening", lambda host, port, **kw: port == 7
        )
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
        holder = _LockHolder(lock_path(tmp_path, "bbbb000000000000"))
        box: dict[str, dict[str, float]] = {}

        def on_sleep(polls: int) -> None:
            now = box["clock"]["t"]
            if now >= 4.0:
                discovery.handshake_path(tmp_path, "aaaa000000000000").unlink(missing_ok=True)
            if now >= 6.0:
                holder.release()

        box["clock"] = _fake_stop_clock(monkeypatch, on_sleep)
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])
        finally:
            holder.release()

        assert result.exit_code == 1, result.output
        assert "daemon pid=11111 stopped (fp=aaaa000000000000)" in result.output
        assert "pid=22222 (fp=bbbb000000000000) still holds its lock" in result.output
        assert box["clock"]["t"] <= 5.1

    def test_unknown_lock_timeout_says_unchecked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        self._foreign(tmp_path, monkeypatch)
        monkeypatch.setattr(os, "kill", lambda pid, sig: pytest.fail("must not signal"))
        _fake_stop_clock(monkeypatch)

        def refuse(path):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("memtomem_stm.daemon.locking._open_lock_fd", refuse)
        result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

        assert result.exit_code == 1, result.output
        assert "its lock could not be checked" in result.output
        assert "still holds its lock" not in result.output

    def test_windows_still_waits_on_lock_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        hs = self._foreign(tmp_path, monkeypatch)
        monkeypatch.setattr("memtomem_stm.cli.daemon_cmd._can_force_terminate", lambda: False)
        monkeypatch.setattr(os, "kill", lambda pid, sig: pytest.fail("must not signal"))
        holder = _LockHolder(lock_path(tmp_path, self.FP))

        def on_sleep(polls: int) -> None:
            if polls == 2:
                hs.unlink()
                holder.release()

        _fake_stop_clock(monkeypatch, on_sleep)
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])
        finally:
            holder.release()

        assert result.exit_code == 0, result.output
        assert "Windows cannot safely" not in result.output
        assert f"daemon pid=98765 stopped (fp={self.FP})" in result.output

    def test_windows_mixed_reports_the_unsignallable_and_waits_on_the_rest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        monkeypatch.setattr("memtomem_stm.cli.daemon_cmd._can_force_terminate", lambda: False)
        monkeypatch.setattr(os, "kill", lambda pid, sig: pytest.fail("must not signal"))
        _put_handshake(tmp_path, "bbbb000000000000", pid=22222)  # port 1: lock-only
        discovery.write_handshake(
            discovery.handshake_path(tmp_path, "aaaa000000000000"),
            pid=11111,
            host="127.0.0.1",
            port=7,
            token="t",
            config_fingerprint="aaaa000000000000",
            created_at=0.0,
            idle_shutdown_seconds=None,
        )
        monkeypatch.setattr(
            "memtomem_stm.daemon.client.probe_listening", lambda host, port, **kw: port == 7
        )
        holder = _LockHolder(lock_path(tmp_path, "bbbb000000000000"))

        def on_sleep(polls: int) -> None:
            if polls == 2:
                discovery.handshake_path(tmp_path, "bbbb000000000000").unlink()
                holder.release()

        _fake_stop_clock(monkeypatch, on_sleep)
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])
        finally:
            holder.release()

        assert result.exit_code == 1, result.output
        assert "Windows cannot safely" in result.output
        assert "daemon pid=22222 stopped (fp=bbbb000000000000)" in result.output
        assert "pid=11111 stopped" not in result.output


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

        def fake_request_spawn(config, *, propagate_errors=False):
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

    def test_unopenable_lock_file_fails_without_a_traceback_or_a_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#1247: a confirmed stop need not have opened the lock file, so
        restart's own probe can be the first to fail on it. That is UNKNOWN —
        not free, and not an uncaught ``OSError``."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _fake_stop_clock(monkeypatch)
        spawns: list[object] = []

        async def fake_shutdown(config):
            return True  # graceful ack, no handshake: stop confirms without a probe

        def refuse(path):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("memtomem_stm.daemon.client.shutdown", fake_shutdown)
        monkeypatch.setattr("memtomem_stm.daemon.locking._open_lock_fd", refuse)
        monkeypatch.setattr(
            "memtomem_stm.daemon.spawn.request_spawn",
            lambda config, *, propagate_errors=False: spawns.append(config) or True,
        )

        result = CliRunner().invoke(_cli(), ["daemon", "restart"])

        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit)
        assert "daemon stopped" in result.output
        assert "could not check the previous daemon's lock" in result.output
        assert "still shutting down" not in result.output
        assert spawns == []

    def test_transient_lock_open_error_starts_only_after_a_free_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from click.testing import CliRunner

        from memtomem_stm.cli import daemon_cmd
        from memtomem_stm.daemon import locking as daemon_locking

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _fake_stop_clock(monkeypatch)
        log: list[str] = []
        real_open = daemon_locking._open_lock_fd
        failures = iter([True, True])

        def flaky_open(path):
            if next(failures, False):
                raise PermissionError(13, "Permission denied")
            return real_open(path)

        real_probe = daemon_cmd._probe_lock

        def logging_probe(path):
            state = real_probe(path)
            log.append(state.value)
            return state

        async def fake_shutdown(config):
            return True

        async def fake_ping(config, *, timeout=2.0):
            return {"pid": 7, "port": 9} if "spawn" in log else None

        def fake_request_spawn(config, *, propagate_errors=False):
            log.append("spawn")
            return True

        monkeypatch.setattr("memtomem_stm.daemon.client.shutdown", fake_shutdown)
        monkeypatch.setattr("memtomem_stm.daemon.client.ping", fake_ping)
        monkeypatch.setattr("memtomem_stm.daemon.locking._open_lock_fd", flaky_open)
        monkeypatch.setattr("memtomem_stm.cli.daemon_cmd._probe_lock", logging_probe)
        monkeypatch.setattr("memtomem_stm.daemon.spawn.request_spawn", fake_request_spawn)

        result = CliRunner().invoke(_cli(), ["daemon", "restart"])

        assert result.exit_code == 0, result.output
        assert log == ["unknown", "unknown", "free", "spawn"]
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
        spawns: list[object] = []
        monkeypatch.setattr(
            "memtomem_stm.daemon.spawn.request_spawn",
            lambda config, *, propagate_errors=False: spawns.append(config) or True,
        )
        # The daemon accepted the shutdown and removed its handshake, but has
        # not released its lifetime lock yet: ``stop`` is confirmed, and the
        # wait that follows is restart's own.
        held_fd: list[int] = []

        async def fake_shutdown(config):
            fd = open_lock_fd(lp)
            assert try_lock(fd) is True
            held_fd.append(fd)
            return True

        monkeypatch.setattr("memtomem_stm.daemon.client.shutdown", fake_shutdown)
        try:
            result = CliRunner().invoke(_cli(), ["daemon", "restart"])
        finally:
            for fd in held_fd:
                release_lock(fd)

        # Exit 1: the daemon is down and did not come back — a script driving
        # the documented upgrade flow must see the failure (was exit 0).
        assert result.exit_code == 1, result.output
        assert "daemon stopped" in result.output
        assert "NOT restarted" in result.output
        assert "still shutting down" in result.output
        assert spawns == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock semantics")
    def test_held_lock_without_handshake_fails_stop_before_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#1245: no handshake but a held lifetime lock is a daemon starting or
        finishing its teardown, so ``stop`` itself fails and ``restart`` never
        reaches its own wait — and never spawns beside it."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        _fake_stop_clock(monkeypatch)
        spawns: list[object] = []
        monkeypatch.setattr(
            "memtomem_stm.daemon.spawn.request_spawn",
            lambda config, *, propagate_errors=False: spawns.append(config) or True,
        )
        config = STMConfig()
        lp = lock_path(config.data_dir.expanduser(), discovery.config_fingerprint(config))
        lp.parent.mkdir(parents=True, exist_ok=True)
        with single_owner_lock(lp) as held:
            assert held is True
            result = CliRunner().invoke(_cli(), ["daemon", "restart"])

        assert result.exit_code == 1, result.output
        assert "daemon lock is held" in result.output
        assert "no running daemon" not in result.output
        assert "NOT restarted" not in result.output
        assert spawns == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGTERM fallback")
    def test_unconfirmed_sigterm_stop_does_not_start_a_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """#1238: a SIGTERMed daemon still tearing down when the wait ends must
        stop ``restart`` before it spawns a second daemon beside it."""
        from click.testing import CliRunner

        monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
        _no_daemon(monkeypatch)
        _write_handshake({"pid": 12345, "host": "127.0.0.1", "port": 1, "token": "t"})
        monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
        monkeypatch.setattr(
            "memtomem_stm.daemon.client.probe_listening", lambda host, port, **kw: True
        )
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
        spawns: list[object] = []
        monkeypatch.setattr(
            "memtomem_stm.daemon.spawn.request_spawn",
            lambda config, *, propagate_errors=False: spawns.append(config) or True,
        )
        _fake_stop_clock(monkeypatch)

        result = CliRunner().invoke(_cli(), ["daemon", "restart"])

        assert result.exit_code == 1, result.output
        assert "did not finish shutting down after SIGTERM" in result.stderr
        assert spawns == []


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


class TestBrokenConfigObservability:
    """#847: a bare ``STMConfig()`` failure in the daemon paths must leave a
    legible trace. Outcomes are unchanged — both paths still raise."""

    @pytest.fixture(autouse=True)
    def _broken_proxy_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(tmp_path / "absent.json"))
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND", "x")

    def test_daemon_server_run_logs_before_raising(self, caplog):
        import logging

        from pydantic import ValidationError

        from memtomem_stm.daemon import server as daemon_server

        with caplog.at_level(logging.WARNING):
            with pytest.raises(ValidationError):
                daemon_server.run(config=None)

        # Previously the construction sat OUTSIDE run()'s exception barrier,
        # so the failure produced no log line at all.
        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND" in caplog.text

    def test_load_config_logs_before_exiting(self, caplog):
        import logging

        from pydantic import ValidationError

        from memtomem_stm.cli.daemon_cmd import _load_config

        with caplog.at_level(logging.WARNING):
            with pytest.raises(SystemExit) as exit_:
                _load_config()

        # Exit 1 without re-raising: the interpreter's second traceback would
        # bypass the escaping formatter (#1173). The cause stays attached.
        assert exit_.value.code == 1
        assert isinstance(exit_.value.__cause__, ValidationError)
        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND" in caplog.text
