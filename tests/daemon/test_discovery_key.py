"""The daemon discovery fingerprint is keyed (#1416).

The fingerprint names the handshake and lock files and is stored in the
handshake, and it covers the LTM command line and ``ltm_mcp_headers``. These
tests pin that it is an HMAC under ``stm-discovery.key``, who may create that
key, and what each consumer does when the key is absent or unusable.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import signal
import stat
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.hook_adapter import ClaudeHookAdapter
from memtomem_stm.config import STMConfig
from memtomem_stm.daemon import client, discovery
from memtomem_stm.daemon.protocol import OP_LTM_SEARCH, OP_SHUTDOWN, PROTOCOL_VERSION
from memtomem_stm.logging_setup import FILE_FORMAT, DisplayEscapingFormatter
from memtomem_stm.surfacing.daemon_adapter import DaemonLtmAdapter
from memtomem_stm.surfacing.mcp_client import LtmTransportError
from memtomem_stm.utils import digest
from memtomem_stm.utils.local_key import LocalKeyError

_KEY_A = bytes(range(32))
_KEY_B = bytes(range(1, 33))
_HOOK_CALL = ClaudeHookAdapter().parse(
    {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/src/auth/jwt_handler.py"},
        "tool_response": {"content": "token validation and refresh flow. " * 400},
    }
)
posix_names = pytest.mark.skipif(
    sys.platform == "win32", reason="a newline is not a valid Windows file name character"
)
posix_modes = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")


def _cli():
    from memtomem_stm.cli.proxy import cli

    return cli


def _write_key(data_dir: Path, key: bytes) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    discovery.discovery_key_path(data_dir).write_bytes(key.hex().encode("ascii") + b"\n")


def _break_key(data_dir: Path) -> Path:
    """Leave a key file that exists and is not a key."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = discovery.discovery_key_path(data_dir)
    path.write_bytes(b"not a key\n")
    return path


def _loosen_key(data_dir: Path) -> os.stat_result:
    """Make the key readable by group and other, as a restore or a manual
    ``chmod`` would leave it; returns its stat from just after."""
    path = discovery.discovery_key_path(data_dir)
    path.chmod(0o644)
    return path.stat()


def _assert_key_untouched(data_dir: Path, before: os.stat_result) -> None:
    after = discovery.discovery_key_path(data_dir).stat()
    assert stat.S_IMODE(after.st_mode) == 0o644
    # The mode alone would also pass for a chmod that set the same bits.
    assert after.st_ctime_ns == before.st_ctime_ns


def _config(data_dir: Path) -> STMConfig:
    config = STMConfig(data_dir=data_dir)
    config.surfacing.timeout_seconds = 1.0
    config.surfacing.warmup_enabled = False
    return config


def _put_foreign(data_dir: Path, fingerprint: str, pid: int = 98765) -> Path:
    path = discovery.handshake_path(data_dir, fingerprint)
    data_dir.mkdir(parents=True, exist_ok=True)
    discovery.write_handshake(
        path,
        pid=pid,
        host="127.0.0.1",
        port=4242,
        token="secret-token",
        config_fingerprint=fingerprint,
        created_at=1.0,
        idle_shutdown_seconds=None,
    )
    return path


def _accepting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every recorded daemon is alive and accepting, without a socket."""
    monkeypatch.setattr("memtomem_stm.daemon.discovery.is_pid_alive", lambda pid: True)
    monkeypatch.setattr("memtomem_stm.daemon.client.probe_listening", lambda host, port, **kw: True)


@pytest.fixture
def formatted_log() -> Iterator[io.StringIO]:
    """Records as the server writes them: through ``DisplayEscapingFormatter``.

    A bare ``caplog`` shows the message a ``basicConfig`` handler would print,
    which is not what an operator reads — this formatter withholds exception
    text and leaves the message's own values to the call site.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(DisplayEscapingFormatter(FILE_FORMAT))
    root = logging.getLogger("memtomem_stm")
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


# ── derivation ───────────────────────────────────────────────────────────────


def test_fingerprint_is_an_hmac_under_the_discovery_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_key(tmp_path, _KEY_A)
    seen: list[tuple[tuple[str, ...], bytes, str]] = []

    def spy(components: tuple[str, ...], *, key: bytes, domain: str) -> str:
        seen.append((components, key, domain))
        return digest.keyed_framed_digest(components, key=key, domain=domain)

    monkeypatch.setattr(discovery, "keyed_framed_digest", spy)

    fingerprint = discovery.config_fingerprint(_config(tmp_path), create_key=False)

    [(components, key, domain)] = seen
    assert key == _KEY_A
    assert domain == "stm-discovery-v1"
    [blob] = components
    assert json.loads(blob)["surfacing"]["ltm_mcp_command"]  # the config, not a constant
    expected = digest.keyed_framed_digest((blob,), key=_KEY_A, domain="stm-discovery-v1")
    assert fingerprint == expected[:16]
    # What the previous build wrote for the same config, which anyone could
    # recompute from a guess.
    assert fingerprint != hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def test_one_config_under_two_keys_has_two_fingerprints(tmp_path: Path) -> None:
    """Fails for any derivation that reads the key and then does not use it."""
    first, second, twin = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    _write_key(first, _KEY_A)
    _write_key(second, _KEY_B)
    _write_key(twin, _KEY_A)

    fp_first = discovery.config_fingerprint(_config(first), create_key=False)
    fp_second = discovery.config_fingerprint(_config(second), create_key=False)
    fp_twin = discovery.config_fingerprint(_config(twin), create_key=False)

    assert fp_first != fp_second
    assert fp_first == fp_twin  # the key decides it, not the directory


def test_a_header_change_still_changes_the_fingerprint(tmp_path: Path) -> None:
    """A rotated credential must not be served by the daemon holding the old one."""
    _write_key(tmp_path, _KEY_A)
    old, new = _config(tmp_path), _config(tmp_path)
    old.surfacing.ltm_mcp_headers = {"Authorization": "Bearer old"}
    new.surfacing.ltm_mcp_headers = {"Authorization": "Bearer new"}

    assert discovery.config_fingerprint(old, create_key=False) != discovery.config_fingerprint(
        new, create_key=False
    )


def test_a_reader_gets_none_and_creates_nothing(tmp_path: Path) -> None:
    data_dir = tmp_path / "fresh"

    assert discovery.config_fingerprint(_config(data_dir), create_key=False) is None
    assert not data_dir.exists()


def test_a_creator_makes_the_key_every_later_caller_reads(tmp_path: Path) -> None:
    data_dir = tmp_path / "fresh"
    config = _config(data_dir)

    created = discovery.config_fingerprint(config, create_key=True)

    key_path = data_dir / "stm-discovery.key"
    assert key_path.is_file()
    if sys.platform != "win32":
        assert key_path.stat().st_mode & 0o777 == 0o600
    assert discovery.config_fingerprint(config, create_key=False) == created
    assert discovery.config_fingerprint(config, create_key=True) == created
    # Neither the handshake glob nor a lock name may ever match the key.
    assert discovery.iter_foreign_handshakes(data_dir, None, include_unreadable=True) == []


def test_tilde_and_the_expanded_home_share_one_key() -> None:
    by_default = STMConfig()
    spelled_out = STMConfig(data_dir=Path.home() / ".memtomem")
    assert str(by_default.data_dir).startswith("~")

    created = discovery.config_fingerprint(by_default, create_key=True)

    assert discovery.config_fingerprint(spelled_out, create_key=False) == created
    assert discovery.discovery_key_path(by_default.data_dir) == discovery.discovery_key_path(
        spelled_out.data_dir
    )


@pytest.mark.parametrize("create_key", [False, True])
def test_an_unusable_key_raises_instead_of_falling_back(tmp_path: Path, create_key: bool) -> None:
    key_path = _break_key(tmp_path)

    with pytest.raises(LocalKeyError, match="stm-discovery.key"):
        discovery.config_fingerprint(_config(tmp_path), create_key=create_key)
    assert key_path.read_bytes() == b"not a key\n"  # never overwritten


@posix_modes
def test_a_reader_leaves_a_permissive_key_as_it_is_and_a_creator_tightens_it(
    tmp_path: Path,
) -> None:
    """Dropping group/other bits is a write to the file's metadata, so it
    belongs to the side that is allowed to write (#1420)."""
    config = _config(tmp_path)
    fingerprint = discovery.config_fingerprint(config, create_key=True)
    before = _loosen_key(tmp_path)

    assert discovery.config_fingerprint(config, create_key=False) == fingerprint
    _assert_key_untouched(tmp_path, before)

    assert discovery.config_fingerprint(config, create_key=True) == fingerprint
    mode = stat.S_IMODE(discovery.discovery_key_path(tmp_path).stat().st_mode)
    assert mode == 0o600


@pytest.mark.asyncio
async def test_daemon_adapter_and_reader_agree_on_a_fresh_data_dir(tmp_path: Path) -> None:
    """The adapter runs first on the standalone route, so it creates the key —
    and the daemon it then asks for has to land on the same fingerprint."""
    from memtomem_stm.daemon.server import DaemonServer

    data_dir = tmp_path / "fresh"
    config = _config(data_dir)

    adapter = DaemonLtmAdapter(config)

    assert discovery.discovery_key_path(data_dir).is_file()
    assert adapter._fingerprint is not None
    assert DaemonServer(config)._fingerprint == adapter._fingerprint
    assert discovery.config_fingerprint(config, create_key=False) == adapter._fingerprint


# ── the reader funnel in ``daemon.client`` ───────────────────────────────────

_READERS = {
    "ping": (lambda config: client.ping(config, timeout=0.2), None),
    "surface": (lambda config: client.surface(config, _HOOK_CALL, timeout=0.2), None),
    "ltm_request": (
        lambda config: client.ltm_request(config, OP_LTM_SEARCH, {"query": "q"}, timeout=0.2),
        ("missing", None),
    ),
    "shutdown": (lambda config: client.shutdown(config, timeout=0.2), False),
}


@pytest.mark.parametrize("name", sorted(_READERS))
def test_client_readers_find_nothing_without_a_key_and_build_no_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    call, not_found = _READERS[name]
    data_dir = tmp_path / "fresh"

    def no_path(*args: object) -> Path:
        raise AssertionError("a reader with no fingerprint must not build a handshake path")

    monkeypatch.setattr(client, "handshake_path", no_path)

    assert asyncio.run(call(_config(data_dir))) == not_found
    assert not data_dir.exists()


@posix_modes
@pytest.mark.parametrize("name", sorted(_READERS))
def test_client_readers_leave_a_permissive_key_as_it_is(tmp_path: Path, name: str) -> None:
    """The hook's ``surface`` runs on every eligible tool call; it must not be
    the thing that rewrites the key's mode."""
    call, not_found = _READERS[name]
    config = _config(tmp_path)
    discovery.config_fingerprint(config, create_key=True)
    before = _loosen_key(tmp_path)

    assert asyncio.run(call(config)) == not_found  # a key, but no daemon
    _assert_key_untouched(tmp_path, before)


@pytest.mark.parametrize("name", sorted(_READERS))
def test_client_readers_raise_on_an_unusable_key(tmp_path: Path, name: str) -> None:
    call, _ = _READERS[name]
    _break_key(tmp_path)

    with pytest.raises(LocalKeyError):
        asyncio.run(call(_config(tmp_path)))


def test_a_caller_supplied_fingerprint_is_used_without_reading_the_key(tmp_path: Path) -> None:
    """The adapter froze its fingerprint; a key damaged later must not turn
    its requests into errors."""
    _break_key(tmp_path)
    config = _config(tmp_path)
    frozen = "f" * 16

    assert asyncio.run(client.ping(config, timeout=0.2, fingerprint=frozen)) is None
    assert asyncio.run(
        client.ltm_request(config, OP_LTM_SEARCH, {"query": "q"}, timeout=0.2, fingerprint=frozen)
    ) == ("missing", None)


# ── spawn and the daemon process ─────────────────────────────────────────────


def test_request_spawn_creates_the_key_before_it_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem_stm.daemon import spawn

    data_dir = tmp_path / "fresh"
    key_seen: list[bool] = []
    monkeypatch.setattr(
        spawn,
        "_spawn_detached",
        lambda: key_seen.append(discovery.discovery_key_path(data_dir).is_file()),
    )

    assert spawn.request_spawn(_config(data_dir)) is True
    assert key_seen == [True]


def test_request_spawn_raises_on_an_unusable_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returning ``False`` here is what ``mms daemon start`` would poll on for
    ten seconds."""
    from memtomem_stm.daemon import spawn

    _break_key(tmp_path)
    launched: list[None] = []
    monkeypatch.setattr(spawn, "_spawn_detached", lambda: launched.append(None))

    with pytest.raises(LocalKeyError):
        spawn.request_spawn(_config(tmp_path))
    assert launched == []


def _run_daemon_with_broken_key(data_dir: Path) -> int:
    from memtomem_stm.daemon.server import run

    _break_key(data_dir)
    return run(_config(data_dir))


def test_daemon_run_reports_an_unusable_key_in_one_line(
    tmp_path: Path, formatted_log: io.StringIO
) -> None:
    assert _run_daemon_with_broken_key(tmp_path) == 1

    [line] = formatted_log.getvalue().splitlines()
    assert "daemon cannot start" in line
    assert "stm-discovery.key" in line
    assert "[message withheld]" not in line


@posix_names
def test_daemon_run_escapes_a_hostile_data_dir_in_its_log_line(
    tmp_path: Path, formatted_log: io.StringIO
) -> None:
    assert _run_daemon_with_broken_key(tmp_path / "x\nWARNING forged: line") == 1

    text = formatted_log.getvalue()
    assert len(text.splitlines()) == 1
    assert "x\\u000AWARNING forged" in text


# ── ``mms daemon`` ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["daemon", "status"], "stopped"),
        (["daemon", "status", "--json"], '"state": "stopped"'),
        (["daemon", "stop"], "no running daemon"),
        (["daemon", "stop", "--all"], "no running daemon"),
    ],
    ids=["status", "status-json", "stop", "stop-all"],
)
def test_looking_at_a_fresh_data_dir_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], expected: str
) -> None:
    data_dir = tmp_path / "fresh"
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(data_dir))

    result = CliRunner().invoke(_cli(), args)

    assert result.exit_code == 0, result.output
    assert expected in result.output
    assert not data_dir.exists()


@pytest.mark.parametrize(
    "env",
    [
        {"MEMTOMEM_STM_HOOK__USE_DAEMON": "0"},
        {"MEMTOMEM_STM_HOOK": '{"use_daemon": false}'},
    ],
    ids=["flat", "nested"],
)
def test_status_names_the_setting_not_one_spelling_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    # The hook routes on ``hook.use_daemon`` in whatever form it was set
    # (#1495), so the hint must not name a variable the operator never set.
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path / "fresh"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    text = CliRunner().invoke(_cli(), ["daemon", "status"])
    as_json = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])

    assert text.exit_code == 0, text.output
    assert "hook will use daemon: no (hook.use_daemon is false)" in text.output
    assert json.loads(as_json.output)["hook_will_use_daemon"] is False


@posix_modes
@pytest.mark.parametrize(
    "args",
    [["daemon", "status"], ["daemon", "status", "--json"], ["daemon", "stop"]],
    ids=["status", "status-json", "stop"],
)
def test_commands_that_only_look_leave_a_permissive_key_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    discovery.config_fingerprint(STMConfig(), create_key=True)
    before = _loosen_key(tmp_path)

    result = CliRunner().invoke(_cli(), args)

    assert result.exit_code == 0, result.output
    _assert_key_untouched(tmp_path, before)


def test_status_lists_a_previous_builds_daemon_as_foreign_without_creating_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Right after an upgrade: the old daemon's handshake, no key yet."""
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    _put_foreign(tmp_path, "0ldunkeyedfp0000")
    _accepting(monkeypatch)
    before = sorted(p.name for p in tmp_path.iterdir())

    result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])

    assert result.exit_code == 0, result.output
    info = json.loads(result.output)
    assert info["state"] == "stopped"
    assert [d["fingerprint"] for d in info["foreign"]] == ["0ldunkeyedfp0000"]
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM fallback")
@pytest.mark.parametrize("key", ["absent", "unusable"])
def test_stop_all_reaches_every_published_daemon_without_a_usable_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    if key == "unusable":
        _break_key(tmp_path)
    handshake = _put_foreign(tmp_path, "0ldunkeyedfp0000")
    _accepting(monkeypatch)
    killed: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        killed.append((pid, sig))
        handshake.unlink()  # teardown done

    monkeypatch.setattr(os, "kill", fake_kill)

    result = CliRunner().invoke(_cli(), ["daemon", "stop", "--all"])

    assert killed == [(98765, signal.SIGTERM)]
    assert "daemon pid=98765 stopped (fp=0ldunkeyedfp0000)" in result.output
    if key == "unusable":
        assert result.exit_code == 1
        # Reported after the cleanup it could still do, not instead of it.
        assert result.output.index("stopped (fp=") < result.output.index("stm-discovery.key")
    else:
        assert result.exit_code == 0, result.output


# ── one fingerprint per command (#1423) ──────────────────────────────────────


def _key_reads(monkeypatch: pytest.MonkeyPatch, *, then: str) -> list[Path]:
    """Record every read of the discovery key, and change the key right after
    the first one: ``removed``, ``corrupted``, or ``kept``.

    A command that reads the key a second time then finds none, or one it
    cannot use — so what the command goes on to report shows whether it did.
    """
    real = discovery.load_key
    reads: list[Path] = []

    def spy(path: Path, **kwargs: bool) -> bytes | None:
        key = real(path, **kwargs)
        reads.append(path)
        if len(reads) == 1 and then == "removed":
            path.unlink()
        elif len(reads) == 1 and then == "corrupted":
            path.write_bytes(b"not a key\n")
        return key

    monkeypatch.setattr(discovery, "load_key", spy)
    return reads


def _answering_daemon(monkeypatch: pytest.MonkeyPatch, data_dir: Path, pid: int) -> Path:
    """This config's daemon, without a socket: its handshake is published
    under the current fingerprint and every request to it succeeds. A shutdown
    request also removes the handshake, as teardown does.

    Only the wire is replaced. ``client.ping`` and ``client.shutdown`` are the
    real ones, so a fingerprint they derive for themselves is a real read.
    """
    current = discovery.config_fingerprint(STMConfig(), create_key=True)
    handshake = _put_foreign(data_dir, current, pid=pid)

    async def request(
        hs: dict[str, object], op: str, payload: object, **kwargs: object
    ) -> dict[str, object]:
        assert hs["pid"] == pid
        if op == OP_SHUTDOWN:
            handshake.unlink()
        return {"ok": True, "v": PROTOCOL_VERSION, "ltm": "warm"}

    monkeypatch.setattr(client, "_request", request)
    return handshake


@pytest.mark.parametrize("then", ["removed", "corrupted"])
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_status_reports_the_daemon_that_answered_once_when_the_key_changes_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, then: str, as_json: bool
) -> None:
    """``status`` derives the fingerprint once and pings with it. Derived a
    second time after the ping, a key removed in between listed the daemon
    that had just answered as a foreign one too, and a key corrupted in
    between replaced the answer with a key error."""
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    _answering_daemon(monkeypatch, tmp_path, pid=4321)
    _accepting(monkeypatch)  # were it listed as foreign, it would count as live
    reads = _key_reads(monkeypatch, then=then)

    result = CliRunner().invoke(_cli(), ["daemon", "status", *(["--json"] if as_json else [])])

    assert result.exit_code == 0, result.output
    assert len(reads) == 1
    if as_json:
        info = json.loads(result.output)
        assert (info["state"], info["pid"]) == ("running", 4321)
        assert info["foreign"] == []
    else:
        assert "running  pid=4321" in result.output
        assert "different config" not in result.output


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_status_without_a_key_reads_for_it_once_and_asks_no_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, as_json: bool
) -> None:
    """No key, no fingerprint, so there is nothing to ping: the ping is
    skipped rather than left to look for the key again."""
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    tmp_path.joinpath("unrelated").touch()

    async def no_request(*args: object, **kwargs: object) -> None:
        pytest.fail("no daemon can be addressed without a fingerprint")

    monkeypatch.setattr(client, "_request", no_request)
    reads = _key_reads(monkeypatch, then="kept")

    result = CliRunner().invoke(_cli(), ["daemon", "status", *(["--json"] if as_json else [])])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output
    assert len(reads) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["unrelated"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM fallback")
@pytest.mark.parametrize("then", ["removed", "corrupted"])
@pytest.mark.parametrize("stop_all", [False, True], ids=["stop", "stop-all"])
def test_stop_acts_on_the_fingerprint_it_started_with_when_the_key_changes_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, then: str, stop_all: bool
) -> None:
    """``stop`` hands the fingerprint it derived to the graceful request. When
    that request derived it again, a key removed in between left it with no
    daemon to ask, and ``stop`` fell back to signalling its own daemon by pid;
    a key corrupted in between ended the run with the key error — with
    ``--all``, after trying to signal that daemon as a foreign one."""
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    _answering_daemon(monkeypatch, tmp_path, pid=4321)
    foreign = _put_foreign(tmp_path, "0ldunkeyedfp0000", pid=98765)
    _accepting(monkeypatch)
    killed: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        killed.append((pid, sig))
        foreign.unlink()  # teardown done

    monkeypatch.setattr(os, "kill", fake_kill)
    reads = _key_reads(monkeypatch, then=then)

    result = CliRunner().invoke(_cli(), ["daemon", "stop", *(["--all"] if stop_all else [])])

    assert result.exit_code == 0, result.output
    assert len(reads) == 1
    # Stopped over the wire, as this config's daemon; never by pid.
    assert "daemon stopped" in result.output.splitlines()
    assert killed == ([(98765, signal.SIGTERM)] if stop_all else [])
    assert "stm-discovery.key" not in result.output


@pytest.mark.parametrize("can_signal", [True, False], ids=["posix", "windows"])
def test_stop_with_an_unusable_key_names_the_file_and_the_way_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, can_signal: bool
) -> None:
    """The hint must not send a Windows operator to ``stop --all``, which
    refuses to signal by pid there."""
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("memtomem_stm.cli.daemon_cmd._can_force_terminate", lambda: can_signal)
    key_path = _break_key(tmp_path)
    _put_foreign(tmp_path, "0ldunkeyedfp0000")
    _accepting(monkeypatch)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    result = CliRunner().invoke(_cli(), ["daemon", "stop"])

    assert result.exit_code == 1
    assert str(key_path) in result.output
    assert ("mms daemon stop --all" in result.output) is can_signal
    assert ("Task Manager" in result.output) is not can_signal
    # What it says ``stop --all`` does has to be what it does: a daemon that
    # is not accepting is waited on, never signalled.
    said = "signals the accepting daemons under data_dir and waits for the others to finish"
    assert (said in result.output) is can_signal
    assert killed == []


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_status_with_an_unusable_key_is_unknown_and_still_lists_what_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, as_json: bool
) -> None:
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    key_path = _break_key(tmp_path)
    _put_foreign(tmp_path, "0ldunkeyedfp0000")
    _accepting(monkeypatch)

    result = CliRunner().invoke(_cli(), ["daemon", "status", *(["--json"] if as_json else [])])

    assert result.exit_code == 1
    assert not isinstance(result.exception, LocalKeyError)
    if as_json:
        info = json.loads(result.output)
        assert info["state"] == "unknown"
        assert str(key_path) in info["error"]
        assert [d["fingerprint"] for d in info["foreign"]] == ["0ldunkeyedfp0000"]
    else:
        assert str(key_path) in result.output
        assert "state unknown" in result.output
        assert "fp=0ldunkeyedfp0000" in result.output


@pytest.mark.parametrize("first_failure", ["ping", "spawn"])
def test_start_with_an_unusable_key_fails_at_once_naming_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_failure: str
) -> None:
    """Both places the error can first surface. The spawn one sits inside the
    retry loop, next to an ``except (OSError, RuntimeError)`` that would have
    turned an ``OSError`` into ten seconds of polling."""
    from memtomem_stm.daemon import spawn

    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    key_path = _break_key(tmp_path)
    if first_failure == "spawn":

        async def no_daemon(config: STMConfig, *, timeout: float = 2.0) -> None:
            return None

        monkeypatch.setattr("memtomem_stm.daemon.client.ping", no_daemon)

    def no_wait(seconds: float) -> None:
        raise AssertionError("start must not poll once the key is known to be unusable")

    monkeypatch.setattr("memtomem_stm.cli.daemon_cmd.time.sleep", no_wait)
    launched: list[None] = []
    monkeypatch.setattr(spawn, "_spawn_detached", lambda: launched.append(None))

    result = CliRunner().invoke(_cli(), ["daemon", "start"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert str(key_path) in result.output
    assert "did not become ready" not in result.output
    assert launched == []


@posix_names
def test_cli_key_errors_escape_a_hostile_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "x\nWARNING forged: line"
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(data_dir))
    _break_key(data_dir)

    for args in (["daemon", "start"], ["daemon", "stop"], ["daemon", "status"]):
        result = CliRunner().invoke(_cli(), args)

        assert result.exit_code == 1, args
        assert "x\\u000AWARNING forged" in result.output, args
        assert "\nWARNING forged" not in result.output, args

    # ``--json`` carries the path itself; the JSON encoding is what keeps it
    # on one line there.
    result = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
    assert str(data_dir) in json.loads(result.output)["error"]
    assert "\nWARNING forged" not in result.output


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs a privilege")
def test_a_broken_data_dir_ancestor_is_not_read_as_no_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key reads as absent through a dangling link too, and "absent" is
    what lets ``status`` and ``stop`` answer without looking any further."""
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "gone")
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(link / "data"))

    status = CliRunner().invoke(_cli(), ["daemon", "status", "--json"])
    stop = CliRunner().invoke(_cli(), ["daemon", "stop"])

    assert status.exit_code == 0, status.output
    info = json.loads(status.output)
    assert info["state"] == "unknown"
    assert "data_dir could not be inspected" in info["error"]
    assert stop.exit_code == 1
    assert "no running daemon" not in stop.output
    assert "cannot be determined" in stop.output
    assert [p.name for p in tmp_path.iterdir()] == ["link"]


def test_restart_without_a_key_goes_straight_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing of this build can hold a lock yet, and probing one would create
    it under a fingerprint nothing uses."""
    data_dir = tmp_path / "fresh"
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(data_dir))
    spawned: list[None] = []

    def no_probe(path: Path) -> None:
        raise AssertionError(f"restart probed a lock with no key present: {path}")

    async def fake_ping(config: STMConfig, *, timeout: float = 2.0) -> dict[str, int] | None:
        return {"pid": 7, "port": 9} if spawned else None

    def fake_request_spawn(config: STMConfig, *, propagate_errors: bool = False) -> bool:
        spawned.append(None)
        return True

    monkeypatch.setattr("memtomem_stm.cli.daemon_cmd._probe_lock", no_probe)
    monkeypatch.setattr("memtomem_stm.daemon.client.ping", fake_ping)
    monkeypatch.setattr("memtomem_stm.daemon.spawn.request_spawn", fake_request_spawn)

    result = CliRunner().invoke(_cli(), ["daemon", "restart"])

    assert result.exit_code == 0, result.output
    assert "no running daemon" in result.output
    assert "daemon started" in result.output
    assert not data_dir.exists()  # only the (faked) spawn would have created it


# ── ``mms hook`` ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fallback", ["skip", "cold"])
def test_hook_degrades_to_its_fallback_on_an_unusable_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: str
) -> None:
    from memtomem_stm.cli import hook_cmd
    from memtomem_stm.daemon import spawn

    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__FALLBACK", fallback)
    _break_key(tmp_path)
    launched: list[None] = []
    monkeypatch.setattr(spawn, "_spawn_detached", lambda: launched.append(None))
    cold = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "cold"}}
    cold_path = AsyncMock(return_value=cold)
    monkeypatch.setattr(hook_cmd, "run_surfacing_hook", cold_path)

    out = asyncio.run(hook_cmd._run_hook(_HOOK_CALL))

    assert out == (cold if fallback == "cold" else {})
    assert cold_path.await_count == (1 if fallback == "cold" else 0)
    assert launched == []


# ── the standalone surfacing adapter ─────────────────────────────────────────


class _Recorder:
    """Stand-ins for everything the adapter could use to reach a daemon.

    They record instead of raising: ``_spawn_best_effort`` swallows exceptions,
    so an ``AssertionError`` from a stand-in would pass unnoticed.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        base = "memtomem_stm.surfacing.daemon_adapter"

        async def ltm_request(*args: object, **kwargs: object) -> tuple[str, None]:
            self.calls.append("ltm_request")
            return "missing", None

        async def ping(*args: object, **kwargs: object) -> None:
            self.calls.append("ping")

        def request_spawn(*args: object, **kwargs: object) -> bool:
            self.calls.append("request_spawn")
            return False

        monkeypatch.setattr(f"{base}.client.ltm_request", ltm_request)
        monkeypatch.setattr(f"{base}.client.ping", ping)
        monkeypatch.setattr(f"{base}.request_spawn", request_spawn)


_UNAVAILABLE_RESULTS = {
    "warm_up": (lambda a: a.warm_up(), None),
    "search": (lambda a: a.search("q"), ([], [], "transport_error")),
    "context_compose": (lambda a: a.context_compose("q"), LtmTransportError),
    "candidate_propose": (
        lambda a: a.candidate_propose("c", source="s", source_ref="r", idempotency_key="k"),
        RuntimeError,
    ),
    "increment_access": (lambda a: a.increment_access(["chunk"]), None),
    "scratch_list": (lambda a: a.scratch_list(), []),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", sorted(_UNAVAILABLE_RESULTS))
async def test_adapter_with_an_unusable_key_reports_a_transport_failure_and_reaches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    call, expected = _UNAVAILABLE_RESULTS[method]
    _break_key(tmp_path)
    recorder = _Recorder(monkeypatch)

    adapter = DaemonLtmAdapter(_config(tmp_path))

    for _ in range(2):  # the same answer every time, not a retry that settles
        if isinstance(expected, type):
            with pytest.raises(expected):
                await call(adapter)
        else:
            assert await call(adapter) == expected
    assert recorder.calls == []


def test_surfacing_still_gets_an_adapter_when_the_key_is_unusable(
    tmp_path: Path, formatted_log: io.StringIO
) -> None:
    """Raising from the constructor would disable surfacing for the process."""
    from memtomem_stm.server import _build_ltm_adapter

    key_path = _break_key(tmp_path)
    config = _config(tmp_path)
    config.surfacing.use_daemon = True

    adapter = _build_ltm_adapter(config, config)

    assert isinstance(adapter, DaemonLtmAdapter)
    [line] = formatted_log.getvalue().splitlines()
    assert str(key_path) in line
    assert "[message withheld]" not in line


@posix_names
@pytest.mark.asyncio
async def test_adapter_warning_escapes_a_hostile_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, formatted_log: io.StringIO
) -> None:
    data_dir = tmp_path / "x\nWARNING forged: line"
    _break_key(data_dir)
    _Recorder(monkeypatch)

    adapter = DaemonLtmAdapter(_config(data_dir))
    await adapter.search("q")
    await adapter.scratch_list()

    text = formatted_log.getvalue()
    assert len(text.splitlines()) == 1  # one warning for the adapter's lifetime
    assert "x\\u000AWARNING forged" in text


@pytest.mark.asyncio
async def test_adapter_stops_asking_once_the_key_goes_bad_at_run_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, formatted_log: io.StringIO
) -> None:
    """A key damaged after construction, then a daemon that exits: the spawn
    request is where the error shows. Swallowed like any other spawn failure,
    every later call would report a daemon that is starting and never does."""
    from memtomem_stm.daemon import spawn

    config = _config(tmp_path)
    adapter = DaemonLtmAdapter(config)
    request = AsyncMock(return_value=("missing", None))
    monkeypatch.setattr("memtomem_stm.surfacing.daemon_adapter.client.ltm_request", request)
    launched: list[None] = []
    monkeypatch.setattr(spawn, "_spawn_detached", lambda: launched.append(None))
    _break_key(tmp_path)

    # The call that finds out was already answering "missing".
    assert await adapter.search("q") == ([], [], "daemon_starting")
    assert await adapter.search("q") == ([], [], "transport_error")
    assert await adapter.search("q") == ([], [], "transport_error")

    assert request.await_count == 1
    assert launched == []
    assert len(formatted_log.getvalue().splitlines()) == 1


@pytest.mark.asyncio
async def test_two_requests_finding_the_bad_key_together_log_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, formatted_log: io.StringIO
) -> None:
    adapter = DaemonLtmAdapter(_config(tmp_path))
    monkeypatch.setattr(
        "memtomem_stm.surfacing.daemon_adapter.client.ltm_request",
        AsyncMock(return_value=("missing", None)),
    )
    # Both spawn requests are inside ``request_spawn`` before either fails.
    both_inside = threading.Barrier(2, timeout=10)

    def failing_spawn(config: STMConfig) -> bool:
        both_inside.wait()
        raise LocalKeyError("cannot read the key file stm-discovery.key: gone bad")

    monkeypatch.setattr("memtomem_stm.surfacing.daemon_adapter.request_spawn", failing_spawn)

    await asyncio.gather(adapter.search("q"), adapter.search("q"))

    assert len(formatted_log.getvalue().splitlines()) == 1
    assert await adapter.search("q") == ([], [], "transport_error")
