"""Tests for utils/parent_liveness.py — the #914 parent-liveness backstop.

The watcher polls in real time (a short interval) but reads the *clock* through
an injected callable, so the grace window is stepped explicitly rather than
waited out.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from memtomem_stm.utils import parent_liveness
from memtomem_stm.utils.parent_liveness import ParentLivenessWatcher

_POLL = 0.01


class _Clock:
    """A clock the test moves, so grace expiry is an assertion, not a sleep."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Parent:
    """The value ``getppid`` returns, which the test reparents at will.

    Assigning ``pid`` reparents from the next poll on. Passing *script* instead
    fixes what each individual *sample* sees — the last entry repeats — which is
    how a bad sample gets pinned to exactly one poll rather than to a sleep that
    happens to be shorter than the interval.
    """

    def __init__(self, pid: int = 4242, script: list[int] | None = None) -> None:
        self.pid = pid
        self._script = list(script or [])

    def __call__(self) -> int:
        if self._script:
            self.pid = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        return self.pid


def _watcher(
    parent: _Parent,
    clock: _Clock,
    reasons: list[str],
    *,
    grace_seconds: float = 0.0,
) -> ParentLivenessWatcher:
    return ParentLivenessWatcher(
        poll_seconds=_POLL,
        grace_seconds=grace_seconds,
        on_parent_gone=reasons.append,
        getppid=parent,
        clock=clock,
    )


async def _run_until(
    watcher: ParentLivenessWatcher, done: object, *, polls: int = 40
) -> asyncio.Task[None]:
    """Start the watcher and give it up to *polls* intervals to reach *done*."""
    task = asyncio.create_task(watcher.run())
    for _ in range(polls):
        if (done() if callable(done) else done):
            break
        await asyncio.sleep(_POLL)
    return task


async def _finish(task: asyncio.Task[None], watcher: ParentLivenessWatcher) -> None:
    watcher.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_a_parent_that_stays_put_is_never_shut_down() -> None:
    parent, clock, reasons = _Parent(), _Clock(), []
    watcher = _watcher(parent, clock, reasons)
    task = asyncio.create_task(watcher.run())
    await asyncio.sleep(_POLL * 10)
    assert reasons == []
    watcher.stop()
    await asyncio.wait_for(task, timeout=1.0)  # stop() ends the loop on its own


async def test_a_reparented_process_with_no_traffic_shuts_down() -> None:
    # The #906 branch (b) shape: no EOF, no signal, only the process tree.
    parent, clock, reasons = _Parent(4242), _Clock(), []
    watcher = _watcher(parent, clock, reasons)
    task = asyncio.create_task(watcher.run())
    await asyncio.sleep(_POLL * 2)
    parent.pid = 1
    for _ in range(60):
        if reasons:
            break
        await asyncio.sleep(_POLL)
    assert reasons == ["Parent process gone (ppid 4242 -> 1)"]
    await asyncio.wait_for(task, timeout=1.0)  # the loop returns after firing
    assert len(reasons) == 1  # and never fires twice


async def test_a_single_bad_sample_is_not_evidence() -> None:
    """A pid observed mid-reparent must not cost a live session, so a change has
    to survive a second poll."""
    # Sample 1 (startup) records 4242, sample 2 sees 1, and every sample after
    # is 4242 again — one bad sample, exactly.
    parent = _Parent(script=[4242, 1, 4242])
    clock, reasons = _Clock(), []
    watcher = _watcher(parent, clock, reasons)
    task = asyncio.create_task(watcher.run())
    await asyncio.sleep(_POLL * 12)
    assert reasons == []
    assert not parent._script[:-1]  # the whole script really was consumed
    await _finish(task, watcher)


async def test_recent_traffic_defers_the_shutdown_rather_than_cancelling_it() -> None:
    """A wrapper shell that exits leaves a live client talking to us, and that
    traffic is the only thing separating it from a leaked descriptor. But the
    veto is a deferral: silence past the grace still shuts us down."""
    parent, clock, reasons = _Parent(4242), _Clock(), []
    watcher = _watcher(parent, clock, reasons, grace_seconds=900.0)
    task = asyncio.create_task(watcher.run())
    await asyncio.sleep(_POLL * 2)

    parent.pid = 1
    watcher.note_activity()
    await asyncio.sleep(_POLL * 10)
    assert reasons == []  # reparented, but the client is still speaking

    clock.now += 901.0  # and then it stops
    for _ in range(60):
        if reasons:
            break
        await asyncio.sleep(_POLL)
    assert reasons == ["Parent process gone (ppid 4242 -> 1)"]
    await asyncio.wait_for(task, timeout=1.0)


async def test_a_zero_grace_asks_only_for_the_confirmed_change() -> None:
    parent, clock, reasons = _Parent(4242), _Clock(), []
    watcher = _watcher(parent, clock, reasons, grace_seconds=0.0)
    task = asyncio.create_task(watcher.run())
    await asyncio.sleep(_POLL * 2)
    watcher.note_activity()  # would veto if the grace were on
    parent.pid = 1
    for _ in range(60):
        if reasons:
            break
        await asyncio.sleep(_POLL)
    assert reasons
    await asyncio.wait_for(task, timeout=1.0)


async def test_being_a_child_of_pid_1_is_not_being_orphaned() -> None:
    """The detached daemon is reparented to init at birth, and on macOS every
    reparent lands on launchd — so ``== 1`` says nothing. Only a change from the
    recorded value does."""
    parent, clock, reasons = _Parent(1), _Clock(), []
    watcher = _watcher(parent, clock, reasons)
    task = asyncio.create_task(watcher.run())
    await asyncio.sleep(_POLL * 10)
    assert reasons == []
    await _finish(task, watcher)


async def test_cancellation_is_not_swallowed() -> None:
    # The shutdown this watcher asks for cancels the loop's tasks, this one
    # included. Swallowing that would leave it running through a teardown it
    # started itself.
    parent, clock, reasons = _Parent(), _Clock(), []
    watcher = _watcher(parent, clock, reasons)
    task = asyncio.create_task(watcher.run())
    await asyncio.sleep(_POLL * 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_the_inference_is_not_made_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    # Reparenting semantics differ and pids are reused there.
    monkeypatch.setattr(parent_liveness.sys, "platform", "win32")
    parent, clock, reasons = _Parent(4242), _Clock(), []
    watcher = _watcher(parent, clock, reasons)
    parent.pid = 1
    await asyncio.wait_for(watcher.run(), timeout=1.0)
    assert reasons == []


async def test_a_non_positive_interval_is_off() -> None:
    parent, clock, reasons = _Parent(4242), _Clock(), []
    watcher = ParentLivenessWatcher(
        poll_seconds=0.0,
        grace_seconds=0.0,
        on_parent_gone=reasons.append,
        getppid=parent,
        clock=clock,
    )
    parent.pid = 1
    await asyncio.wait_for(watcher.run(), timeout=1.0)
    assert reasons == []


async def test_the_log_carries_the_evidence_for_each_step(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Whoever reads the log after an unexplained exit needs the pids and the
    idle time, the way #910's sweep and #911's watchdog name theirs."""
    caplog.set_level(logging.INFO, logger=parent_liveness.__name__)
    parent, clock, reasons = _Parent(4242), _Clock(), []
    watcher = _watcher(parent, clock, reasons, grace_seconds=900.0)
    task = asyncio.create_task(watcher.run())
    await asyncio.sleep(_POLL * 2)
    parent.pid = 77
    watcher.note_activity()
    await asyncio.sleep(_POLL * 10)
    clock.now += 901.0
    for _ in range(60):
        if reasons:
            break
        await asyncio.sleep(_POLL)
    await asyncio.wait_for(task, timeout=1.0)

    text = caplog.text
    assert "watching ppid 4242" in text
    assert "recorded ppid 4242, now 77 — confirming at the next poll" in text
    assert "inside the 900s grace" in text
    assert "no request for 901s" in text
    # The veto explains itself once per episode, not once per poll.
    assert text.count("not shutting down yet") == 1
