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
from unittest import mock

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


_STUBBORN_CHILD_CODE = (
    "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "sys.stdout.write('ready\\n'); sys.stdout.flush(); time.sleep(60)"
)

_LEADER_CODE = (
    "import subprocess, sys;"
    "c = subprocess.Popen([sys.executable, '-c', sys.argv[1]], stdout=subprocess.PIPE);"
    "print(c.pid, flush=True);"
    "print(c.stdout.readline().decode(), end='', flush=True)"
)


def _departed_leader_with_stubborn_child() -> tuple[subprocess.Popen[bytes], int]:
    """A session leader that exits leaving a SIGTERM-ignoring child in its
    process group — the abandoned ``uv run memtomem`` wrapper shape. The group
    outlives the leader either way, whether it is reaped or lingers as a
    zombie, and the pgid number cannot be recycled while it has members."""
    leader = subprocess.Popen(
        [sys.executable, "-c", _LEADER_CODE, _STUBBORN_CHILD_CODE],
        start_new_session=True,
        stdout=subprocess.PIPE,
    )
    assert leader.stdout is not None
    grandchild_pid = int(leader.stdout.readline())
    assert leader.stdout.readline() == b"ready\n"
    # Wait for the leader itself to go; only the grandchild is left in the group.
    deadline = time.monotonic() + 5.0
    while leader.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    return leader, grandchild_pid


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


def test_a_child_that_died_on_sigterm_is_not_signalled_again() -> None:
    # Pids are reused, so a late SIGKILL can land on an unrelated process. Uses
    # a real child so the decision goes through the waitid branch that the
    # production path actually takes, not the not-ours fallback.
    child = _sleeping_child()
    signalled: list[tuple[int, int]] = []
    real_signal = child_reaper.signal_pid

    def _record(pid: int, sig: int) -> None:
        signalled.append((pid, int(sig)))
        real_signal(pid, sig)

    try:
        with mock.patch.object(child_reaper, "signal_pid", _record):
            child_reaper.terminate_leaked_children({child.pid}, escalate_seconds=1.0)
        assert child.wait(timeout=5.0) == -signal.SIGTERM
        assert signalled == [(child.pid, int(signal.SIGTERM))]
    finally:
        _reap(child)


def test_a_departed_leaders_group_still_gets_the_escalation() -> None:
    """The sweep signals a process *group*, so the escalation has to ask about
    the group. An abandoned wrapper's server keeps running in the group of a
    leader that is already gone: judging by the leader alone drops it from the
    escalation and leaks exactly the child the sweep exists to reach."""
    leader, grandchild_pid = _departed_leader_with_stubborn_child()
    try:
        child_reaper.terminate_leaked_children({leader.pid}, escalate_seconds=0.3)
        deadline = time.monotonic() + 5.0
        while child_reaper.is_pid_alive(grandchild_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not child_reaper.is_pid_alive(grandchild_pid)
    finally:
        child_reaper.signal_pid(grandchild_pid, signal.SIGKILL)
        _reap(leader)


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
    monkeypatch.setattr(child_reaper, "probe_child_pids", lambda: {111, 222})
    killed: list[set[int]] = []
    monkeypatch.setattr(child_reaper, "terminate_leaked_children", killed.append)

    child_reaper.spawn_claimed(lambda: 222)
    assert child_reaper.leaked_child_pids(set()) == {111}

    with caplog.at_level(logging.WARNING, logger=child_reaper.__name__):
        child_reaper.sweep_leaked_children(baseline=set())
    assert killed == [{111}]
    assert "222" not in "".join(r.getMessage() for r in caplog.records)


def test_sweep_never_raises_into_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    # It is the last step of shutdown: a failure here must not replace one leak
    # with a different one.
    def _boom() -> set[int]:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(child_reaper, "probe_child_pids", _boom)
    child_reaper.sweep_leaked_children(baseline=set())


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


def test_a_sweep_cannot_run_between_the_spawn_and_its_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The detached spawn happens on a worker thread (``request_spawn`` under
    ``asyncio.to_thread``), so a sweep must never observe a child that exists
    but is not yet claimed. ``spawn_claimed`` holds the lock across the spawn —
    and the sweep probes *before* taking that lock, so a spawn completing
    between the two reads still lands on the sparing side."""
    monkeypatch.setattr(child_reaper, "_detached_pids", set())
    spawn_started = threading.Event()
    probed = threading.Event()

    def _probe() -> set[int]:
        # The probe is a pgrep round trip; simulate the spawn completing during
        # it, which is the ordering that a claims-first read gets wrong.
        probed.set()
        assert spawn_started.wait(5.0)
        worker.join(timeout=5.0)
        return {777}

    def _spawn() -> int:
        spawn_started.set()
        return 777

    monkeypatch.setattr(child_reaper, "probe_child_pids", _probe)
    worker = threading.Thread(
        target=lambda: probed.wait(5.0) and child_reaper.spawn_claimed(_spawn)
    )
    worker.start()
    try:
        assert child_reaper.leaked_child_pids(set()) == set()
    finally:
        worker.join(timeout=5.0)


def test_a_wedged_spawn_cannot_park_the_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every other step of the sweep is capped so it can never be the reason a
    process fails to exit; waiting on the claim lock is the one that has no
    other ceiling. A spawn stuck inside Popen must yield "unknown", not a hang
    — so the assertion is on the wait completing, not just on its answer."""
    monkeypatch.setattr(child_reaper, "_detached_pids", set())
    monkeypatch.setattr(child_reaper, "probe_child_pids", lambda: {888})
    monkeypatch.setattr(child_reaper, "_CLAIM_LOCK_WAIT_SECONDS", 0.1)
    wedged = threading.Event()
    release = threading.Event()
    answered: list[set[int] | None] = []

    def _stuck_spawn() -> int:
        wedged.set()
        release.wait(30.0)
        return 888

    spawner = threading.Thread(target=lambda: child_reaper.spawn_claimed(_stuck_spawn))
    sweeper = threading.Thread(
        target=lambda: answered.append(child_reaper.leaked_child_pids(set()))
    )
    spawner.start()
    try:
        assert wedged.wait(5.0)
        sweeper.start()
        # An unbounded acquire() would still be blocked here, so this join is
        # the real assertion: the sweep gave up rather than parking shutdown.
        sweeper.join(timeout=5.0)
        assert not sweeper.is_alive()
        assert answered == [None]  # unknown → sweep nothing
    finally:
        release.set()
        spawner.join(timeout=5.0)
        sweeper.join(timeout=5.0)


def test_an_unknown_baseline_sweeps_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # A startup probe that failed knows nothing about pre-existing children.
    # Reading that as "there were none" would arm the sweep against every one
    # of them — the failure mode this module's bias exists to prevent.
    monkeypatch.setattr(child_reaper, "probe_child_pids", lambda: {10, 20})
    killed: list[set[int]] = []
    monkeypatch.setattr(child_reaper, "terminate_leaked_children", killed.append)
    child_reaper.sweep_leaked_children(baseline=None)
    assert killed == []


def test_a_claim_is_never_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pids are recycled, so a claim could in principle outlive its process and
    spare a later leak that lands on the same pid. Expiring claims to recover
    that leak trades the cheap mistake for the expensive one: the check would
    also mis-sentence the live daemon whose Popen something else already
    reaped. The sweep is biased toward sparing, so the claim stands."""
    monkeypatch.setattr(child_reaper, "_detached_pids", set())
    monkeypatch.setattr(child_reaper, "probe_child_pids", lambda: {555})
    child_reaper.spawn_claimed(lambda: 555)
    assert child_reaper.leaked_child_pids(set()) == set()


def test_a_zombie_leader_is_still_swept(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child nobody reaped lingers as a zombie that pgrep still lists — and
    an abandoned wrapper (``uv run memtomem``) is exactly that, with the live
    server it wrapped still in its process group. The zombie pins its pid, so
    the pgid cannot have been recycled, and signalling the group is the only
    way to reach the grandchild. Skipping the corpse would skip the leak."""
    monkeypatch.setattr(child_reaper, "_detached_pids", set())
    monkeypatch.setattr(child_reaper, "probe_child_pids", lambda: {666})
    monkeypatch.setattr(child_reaper, "_has_exited", lambda _pid: True)
    assert child_reaper.leaked_child_pids(set()) == {666}


def test_signal_pid_reaches_the_group_of_a_zombie_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # getpgid raises ESRCH for a zombie leader on some platforms. Giving up
    # there abandons the live grandchild; start_new_session makes the leader's
    # pid its own pgid, so that is the fallback.
    killed: list[tuple[str, int]] = []

    def _getpgid(pid: int) -> int:
        if pid == 0:
            return 4242
        raise ProcessLookupError

    monkeypatch.setattr(child_reaper.os, "getpgid", _getpgid)
    monkeypatch.setattr(child_reaper.os, "killpg", lambda pid, _s: killed.append(("pg", pid)))
    child_reaper.signal_pid(999, signal.SIGTERM)
    assert killed == [("pg", 999)]


def test_children_that_predate_us_are_not_ours_to_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    # app_lifespan does not always own the process it runs in; a host's own
    # subprocesses must survive our teardown.
    monkeypatch.setattr(child_reaper, "_detached_pids", set())
    monkeypatch.setattr(child_reaper, "probe_child_pids", lambda: {10, 20})
    killed: list[set[int]] = []
    monkeypatch.setattr(child_reaper, "terminate_leaked_children", killed.append)
    child_reaper.sweep_leaked_children(baseline={10})
    assert killed == [{20}]
