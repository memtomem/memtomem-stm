"""Tests for the mms cross-process write lock (``state.write_lock``).

Two ``--apply`` runs (``mms host sync`` / ``mms import``) racing the same
registry + sidecar interleave their load→save spans: the loser's stale load
silently drops the winner's mutation on save. The lock serializes the span;
these tests pin the lock's own semantics (contention, timeout, release,
no-op modes) and the CLI wiring (held lock → clean error; ``--plan`` never
locks).

``flock`` contention is exercised through a SECOND file descriptor — flock
locks attach to the open file description, so two ``os.open``\\ s of the same
path contend even inside one process/thread. That makes the cross-process
race testable without subprocesses.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.mms_host import host_group
from memtomem_stm.cli.mms_import import import_command
from memtomem_stm.mms import state
from helpers import set_home

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock is POSIX-only; write_lock is documented as a no-op on Windows",
)

if sys.platform != "win32":
    import fcntl


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    """Repoint ``~/.mms`` at a sandbox dir; yield it."""
    set_home(monkeypatch, tmp_path)
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cli_sandbox(tmp_path, monkeypatch):
    """HOME + cwd sandbox for CLI invocations (mirrors tests/cli idiom)."""
    set_home(monkeypatch, tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return {"home": tmp_path, "cwd": cwd}


@contextmanager
def _hold_lock(home: Path):
    """Hold the write lock through a raw fd, as a foreign process would."""
    lock = home / ".mms" / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock


# ---------------------------------------------------------------------------
# Lock semantics
# ---------------------------------------------------------------------------


def test_lock_file_lives_under_mms_home_with_0600(sandbox_home):
    with state.write_lock():
        pass
    lock = sandbox_home / ".mms" / ".lock"
    assert state.write_lock_path() == lock
    assert lock.is_file()
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_held_lock_times_out_with_clean_error(sandbox_home):
    with state.write_lock():
        t0 = time.monotonic()
        with pytest.raises(state.WriteLockTimeout) as exc_info:
            with state.write_lock(timeout=0.2):
                pytest.fail("second acquisition must not succeed while held")
        assert time.monotonic() - t0 >= 0.2
        msg = str(exc_info.value)
        assert str(state.write_lock_path()) in msg
        assert "mms host sync --apply" in msg  # operator-facing attribution


def test_waiter_acquires_after_holder_releases(sandbox_home):
    acquired = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with state.write_lock():
            acquired.set()
            release.wait(timeout=5)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert acquired.wait(timeout=5)
        threading.Timer(0.2, release.set).start()
        # Generous timeout: the point is that polling SUCCEEDS after the
        # holder releases, not how fast.
        with state.write_lock(timeout=5):
            pass
    finally:
        release.set()
        t.join(timeout=5)


def test_release_allows_immediate_reacquire(sandbox_home):
    with state.write_lock():
        pass
    with state.write_lock(timeout=0.2):
        pass  # would raise WriteLockTimeout if the first exit leaked the lock


def test_disabled_lock_is_a_true_no_op(sandbox_home):
    with state.write_lock(enabled=False):
        pass
    assert not (sandbox_home / ".mms" / ".lock").exists()


def test_disabled_lock_ignores_a_held_lock(sandbox_home):
    with _hold_lock(sandbox_home):
        with state.write_lock(enabled=False, timeout=0.1):
            pass  # enters immediately despite the holder


# ---------------------------------------------------------------------------
# Generalized seam — lock_path / holder_hint parameterization (#475 PR2)
# ---------------------------------------------------------------------------


def test_custom_lock_path_creates_that_file_with_0600(sandbox_home):
    custom = sandbox_home / ".memtomem" / ".stm_proxy.lock"
    with state.write_lock(lock_path=custom):
        pass
    assert custom.is_file()
    assert stat.S_IMODE(custom.stat().st_mode) == 0o600
    # The default registry lock must not be touched by a custom-path span.
    assert not (sandbox_home / ".mms" / ".lock").exists()


def test_custom_holder_hint_lands_in_timeout_message(sandbox_home):
    custom = sandbox_home / ".memtomem" / ".stm_proxy.lock"
    custom.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(custom, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(state.WriteLockTimeout) as exc_info:
            with state.write_lock(
                lock_path=custom, holder_hint="a proxy-config writer", timeout=0.1
            ):
                pytest.fail("acquisition must not succeed while held")
    finally:
        os.close(fd)
    msg = str(exc_info.value)
    assert str(custom) in msg
    assert "a proxy-config writer" in msg
    assert "mms host sync --apply" not in msg  # default attribution replaced


def test_registry_and_custom_locks_are_independent(sandbox_home):
    """Holding the registry lock must not block a custom-path span — the
    two domains (mms registry vs proxy config) serialize separately."""
    custom = sandbox_home / ".memtomem" / ".stm_proxy.lock"
    with _hold_lock(sandbox_home):
        with state.write_lock(lock_path=custom, timeout=0.1):
            pass  # enters immediately: different file, different flock


# ---------------------------------------------------------------------------
# CLI wiring — both mutating commands acquire; --plan never does
# ---------------------------------------------------------------------------


def test_sync_apply_under_held_lock_exits_cleanly(runner, cli_sandbox, monkeypatch):
    monkeypatch.setattr(state, "WRITE_LOCK_TIMEOUT_SECONDS", 0.2)
    with _hold_lock(cli_sandbox["home"]):
        res = runner.invoke(host_group, ["sync", "--apply", "--yes"])
    assert res.exit_code == 1
    assert "Error:" in res.output  # ClickException render, not a traceback
    assert "write lock" in res.output
    assert "Traceback" not in res.output


def test_import_apply_under_held_lock_exits_cleanly(runner, cli_sandbox, monkeypatch):
    monkeypatch.setattr(state, "WRITE_LOCK_TIMEOUT_SECONDS", 0.2)
    with _hold_lock(cli_sandbox["home"]):
        res = runner.invoke(import_command, ["--apply"])
    assert res.exit_code == 1
    assert "Error:" in res.output
    assert "write lock" in res.output


def test_plan_invocations_skip_the_lock(runner, cli_sandbox, monkeypatch):
    # If --plan took the lock, the shrunken timeout would fail it fast and
    # the exit-0 asserts below would catch it.
    monkeypatch.setattr(state, "WRITE_LOCK_TIMEOUT_SECONDS", 0.05)
    with _hold_lock(cli_sandbox["home"]):
        res_sync = runner.invoke(host_group, ["sync"])  # --plan is the default
        res_import = runner.invoke(import_command, [])  # --plan is the default
    assert res_sync.exit_code == 0, res_sync.output
    assert res_import.exit_code == 0, res_import.output


def test_apply_still_mutates_after_lock_wraps_the_command(runner, cli_sandbox):
    """The decorator must not swallow the command's normal write path."""
    cfg = {"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]}}}
    import json

    (cli_sandbox["home"] / ".claude.json").write_text(json.dumps(cfg), encoding="utf-8")
    res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
    assert res.exit_code == 0, res.output
    registry = state.load_registry()
    assert "filesystem" in registry.servers
    assert (cli_sandbox["home"] / ".mms" / ".lock").is_file()  # lock was taken


def test_wrapped_commands_keep_help_docstrings(runner):
    res = runner.invoke(host_group, ["sync", "--help"])
    assert "Reconcile registry + sidecar" in res.output
    res = runner.invoke(import_command, ["--help"])
    assert "Import MCP definitions" in res.output
