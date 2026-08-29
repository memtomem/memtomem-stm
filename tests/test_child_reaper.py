"""Tests for utils/child_reaper.py — the leaked-child sweep (#906).

The sweep signals real process groups, so the escalation tests use real
children; everything about *which* pids get signalled is exercised with fakes.
"""

from __future__ import annotations

import signal
import subprocess
import sys

import pytest

from memtomem_stm.utils import child_reaper

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals/process groups")


def _sleeping_child() -> subprocess.Popen[bytes]:
    """A real direct child mimicking a leaked stdio child: its own session
    (like mcp's stdio child) and a sleep only a signal can end."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )


def _sigterm_ignoring_child() -> subprocess.Popen[bytes]:
    """A child that survives SIGTERM, announcing readiness first — installing
    the handler is not instant, and a SIGTERM racing python's startup would
    kill it and fake a pass for the escalation test."""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "sys.stdout.write('ready\\n'); sys.stdout.flush(); time.sleep(60)",
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
    )
    assert child.stdout is not None
    assert child.stdout.readline() == b"ready\n"
    return child


def _reap(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        child.kill()
        child.wait(timeout=5.0)


def test_direct_child_pids_sees_spawned_child() -> None:
    child = _sleeping_child()
    try:
        assert child.pid in child_reaper.direct_child_pids()
    finally:
        child.terminate()
        child.wait(timeout=5.0)
    # Reaped → no longer listed.
    assert child.pid not in child_reaper.direct_child_pids()


def test_direct_child_pids_degrades_to_empty_without_pgrep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The sweep runs on the shutdown path, so a missing/failing probe must
    # answer "nothing to do" rather than raise into teardown.
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(child_reaper.subprocess, "run", _boom)
    assert child_reaper.direct_child_pids() == set()


def test_signal_pid_tolerates_a_dead_pid() -> None:
    child = _sleeping_child()
    child.kill()
    child.wait(timeout=5.0)
    child_reaper.signal_pid(child.pid, signal.SIGTERM)  # must not raise


def test_signal_pid_never_signals_our_own_group(monkeypatch: pytest.MonkeyPatch) -> None:
    # A child sharing our process group must be signalled by pid: a group kill
    # there would take the test runner (and, in production, the server) down
    # with it.
    killed: list[tuple[str, int]] = []
    monkeypatch.setattr(child_reaper.os, "getpgid", lambda _pid: 4242)
    monkeypatch.setattr(child_reaper.os, "killpg", lambda pid, _s: killed.append(("pg", pid)))
    monkeypatch.setattr(child_reaper.os, "kill", lambda pid, _s: killed.append(("pid", pid)))
    child_reaper.signal_pid(999, signal.SIGTERM)
    assert killed == [("pid", 999)]


def test_is_pid_alive() -> None:
    child = _sleeping_child()
    try:
        assert child_reaper.is_pid_alive(child.pid) is True
    finally:
        _reap(child)
    assert child_reaper.is_pid_alive(child.pid) is False
    assert child_reaper.is_pid_alive(0) is False
    assert child_reaper.is_pid_alive(-1) is False


async def test_terminate_leaked_children_terminates_a_real_child() -> None:
    child = _sleeping_child()
    try:
        await child_reaper.terminate_leaked_children({child.pid}, escalate_seconds=0.2)
        assert child.wait(timeout=5.0) == -signal.SIGTERM
    finally:
        _reap(child)


async def test_terminate_leaked_children_escalates_to_sigkill() -> None:
    # The escalation is the whole reason the sweep is a backstop rather than a
    # polite request: a child that ignores SIGTERM must still die.
    child = _sigterm_ignoring_child()
    try:
        await child_reaper.terminate_leaked_children({child.pid}, escalate_seconds=0.3)
        assert child.wait(timeout=5.0) == -signal.SIGKILL
    finally:
        _reap(child)


async def test_terminate_leaked_children_skips_escalation_for_pids_that_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pid that died on SIGTERM must not be signalled again: pids are reused,
    # so a late SIGKILL can land on an unrelated process.
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(
        child_reaper, "signal_pid", lambda pid, sig: signalled.append((pid, int(sig)))
    )
    monkeypatch.setattr(child_reaper, "is_pid_alive", lambda _pid: False)
    await child_reaper.terminate_leaked_children({111}, escalate_seconds=0.05)
    assert signalled == [(111, int(signal.SIGTERM))]
