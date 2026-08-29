"""Tests for utils/child_reaper.py — the leaked-child sweep (#906).

The sweep signals real process groups, so the escalation tests use real
children; everything about *which* pids get signalled is exercised with fakes.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import threading
import time

import pytest

from memtomem_stm.utils import child_reaper

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals/process groups"),
    # These spawn and reap their own children, so they need the real probe.
    pytest.mark.real_child_sweep,
]


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


def test_terminate_leaked_children_terminates_a_real_child() -> None:
    child = _sleeping_child()
    try:
        child_reaper.terminate_leaked_children({child.pid}, escalate_seconds=0.2)
        assert child.wait(timeout=5.0) == -signal.SIGTERM
    finally:
        _reap(child)


def test_terminate_leaked_children_escalates_to_sigkill() -> None:
    # The escalation is the whole reason the sweep is a backstop rather than a
    # polite request: a child that ignores SIGTERM must still die.
    child = _sigterm_ignoring_child()
    try:
        child_reaper.terminate_leaked_children({child.pid}, escalate_seconds=0.3)
        assert child.wait(timeout=5.0) == -signal.SIGKILL
    finally:
        _reap(child)


def test_terminate_leaked_children_skips_escalation_for_pids_that_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pid that died on SIGTERM must not be signalled again: pids are reused,
    # so a late SIGKILL can land on an unrelated process.
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(
        child_reaper, "signal_pid", lambda pid, sig: signalled.append((pid, int(sig)))
    )
    monkeypatch.setattr(child_reaper, "is_pid_alive", lambda _pid: False)
    child_reaper.terminate_leaked_children({111}, escalate_seconds=0.05)
    assert signalled == [(111, int(signal.SIGTERM))]


def test_escalation_does_not_wait_out_a_child_nobody_reaped() -> None:
    # A fire-and-forget child (Popen discarded, as daemon spawn does) that dies
    # on SIGTERM stays a zombie: a signal-0 probe still calls it alive, so the
    # poll would burn the whole window and then SIGKILL a corpse. The escalation
    # window is a ceiling on real stragglers, not a fixed shutdown tax.
    child = _sleeping_child()
    started = time.monotonic()
    child_reaper.terminate_leaked_children({child.pid}, escalate_seconds=5.0)
    assert time.monotonic() - started < 2.0
    assert child.wait(timeout=5.0) == -signal.SIGTERM


def test_sweep_spares_a_child_meant_to_outlive_us(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The shared surfacing daemon is spawned detached but is still a direct
    # child (nothing double-forks), and it is meant to outlive us. Sweeping it
    # would take down the daemon and the LTM it holds for every other consumer.
    monkeypatch.setattr(child_reaper, "_detached_pids", set())
    monkeypatch.setattr(child_reaper, "_has_exited", lambda _pid: False)
    monkeypatch.setattr(child_reaper, "direct_child_pids", lambda: {111, 222})
    killed: list[set[int]] = []
    monkeypatch.setattr(child_reaper, "terminate_leaked_children", killed.append)

    with child_reaper.spawning_detached_child() as claim:
        claim(222)
    assert child_reaper.leaked_child_pids() == {111}

    with caplog.at_level(logging.WARNING, logger=child_reaper.__name__):
        child_reaper.sweep_leaked_children()
    assert killed == [{111}]
    assert "222" not in "".join(r.getMessage() for r in caplog.records)


def test_sweep_never_raises_into_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    # It is the last step of shutdown: a failure here must not replace one leak
    # with a different one.
    def _boom() -> set[int]:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(child_reaper, "direct_child_pids", _boom)
    child_reaper.sweep_leaked_children()


def test_escalation_handles_a_mixed_batch() -> None:
    # One child dies on SIGTERM, one has to be killed — the batch must not let
    # either outcome hide the other.
    plain = _sleeping_child()
    stubborn = _sigterm_ignoring_child()
    try:
        child_reaper.terminate_leaked_children({plain.pid, stubborn.pid}, escalate_seconds=0.3)
        assert plain.wait(timeout=5.0) == -signal.SIGTERM
        assert stubborn.wait(timeout=5.0) == -signal.SIGKILL
    finally:
        _reap(plain)
        _reap(stubborn)


def test_a_sweep_cannot_run_between_the_spawn_and_its_claim() -> None:
    """The detached spawn happens on a worker thread (``request_spawn`` under
    ``asyncio.to_thread``), so registering the pid after ``Popen`` returns
    leaves a window where the child exists and nothing claims it. The claim
    holds the sweep's lock across the spawn, so a concurrent sweep sees either
    no child or a claimed one — never an unclaimed live one."""
    observed: list[set[int]] = []
    ready = threading.Event()
    release = threading.Event()

    def _spawn() -> None:
        with child_reaper.spawning_detached_child() as claim:
            ready.set()
            release.wait(5.0)  # the "Popen" is still in flight here
            claim(777)

    worker = threading.Thread(target=_spawn)
    worker.start()
    try:
        assert ready.wait(5.0)
        sweeper = threading.Thread(
            target=lambda: observed.append(child_reaper.leaked_child_pids())
        )
        sweeper.start()
        # The sweep must block on the claim rather than read a half-registered
        # set: it is still waiting while the spawn is mid-flight.
        sweeper.join(timeout=0.2)
        assert sweeper.is_alive()
        release.set()
        sweeper.join(timeout=5.0)
    finally:
        release.set()
        worker.join(timeout=5.0)
    assert observed == [set()]  # 777 was claimed before the sweep could read


def test_a_claim_is_dropped_once_that_process_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pids are recycled. A claim kept forever would spare a genuinely leaked
    # child that later lands on the detached daemon's old pid — the one case
    # the sweep exists for, silently skipped.
    monkeypatch.setattr(child_reaper, "_detached_pids", set())
    monkeypatch.setattr(child_reaper, "direct_child_pids", lambda: {555})
    with child_reaper.spawning_detached_child() as claim:
        claim(555)

    monkeypatch.setattr(child_reaper, "_has_exited", lambda _pid: False)
    assert child_reaper.leaked_child_pids() == set()  # daemon still running

    monkeypatch.setattr(child_reaper, "_has_exited", lambda _pid: True)
    assert child_reaper.leaked_child_pids() == {555}  # pid reused by a leak


def test_children_that_predate_us_are_not_ours_to_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    # app_lifespan does not always own the process it runs in; a host's own
    # subprocesses must survive our teardown.
    monkeypatch.setattr(child_reaper, "_detached_pids", set())
    monkeypatch.setattr(child_reaper, "direct_child_pids", lambda: {10, 20})
    killed: list[set[int]] = []
    monkeypatch.setattr(child_reaper, "terminate_leaked_children", killed.append)
    child_reaper.sweep_leaked_children(baseline={10})
    assert killed == [{20}]
