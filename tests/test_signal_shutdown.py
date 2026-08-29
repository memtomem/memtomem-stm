"""Tests for utils/signal_shutdown.py — SIGTERM/SIGINT routing (#906)."""

from __future__ import annotations

import asyncio
import contextlib
import signal

import pytest

from memtomem_stm.utils import signal_shutdown
from memtomem_stm.utils.signal_shutdown import ShutdownSignals


class _Recorder:
    def __init__(self) -> None:
        self.armed = 0
        self.cleaned = 0
        self.cancelled = 0
        self.exits: list[int] = []

    def arm(self) -> None:
        self.armed += 1

    def clean(self) -> None:
        self.cleaned += 1


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Neuter the two things a handler does that would end the test run."""
    recorder = _Recorder()
    monkeypatch.setattr(signal_shutdown.os, "_exit", recorder.exits.append)
    monkeypatch.setattr(
        signal_shutdown.ShutdownSignals,
        "_cancel_everything",
        staticmethod(lambda: setattr(recorder, "cancelled", recorder.cancelled + 1)),
    )
    monkeypatch.setattr(signal_shutdown, "_signalled", False)
    monkeypatch.setattr(signal_shutdown, "_teardown_completed", False)
    return recorder


def _signals(rec: _Recorder) -> ShutdownSignals:
    return ShutdownSignals(arm_watchdog=rec.arm, hard_exit_cleanup=rec.clean)


def test_the_first_signal_asks_for_the_normal_shutdown(rec: _Recorder) -> None:
    # Cancelling unwinds the SDK's task group so the lifespan's finally runs —
    # the same bounded, child-sweeping shutdown a departing client produces.
    # The watchdog is armed first: cancellation is the mechanism, not the
    # promise.
    handler = _signals(rec)
    handler._on_signal(signal.SIGTERM)
    assert rec.cancelled == 1
    assert rec.armed == 1
    assert rec.exits == []  # the teardown gets its chance


def test_the_second_signal_exits_immediately(rec: _Recorder) -> None:
    # An operator who signals twice has said what they want.
    handler = _signals(rec)
    handler._on_signal(signal.SIGINT)
    handler._on_signal(signal.SIGINT)
    assert rec.exits == [128 + int(signal.SIGINT)]
    assert rec.cleaned == 1  # os._exit runs no finally; the sweep happens here


def test_the_hard_exit_survives_a_failing_cleanup(rec: _Recorder) -> None:
    # Best-effort cleanup must not become the reason a twice-signalled process
    # stays alive — and not only for Exception.
    def _boom() -> None:
        raise KeyboardInterrupt

    handler = ShutdownSignals(arm_watchdog=rec.arm, hard_exit_cleanup=_boom)
    handler._on_signal(signal.SIGTERM)
    handler._on_signal(signal.SIGTERM)
    assert rec.exits == [128 + int(signal.SIGTERM)]


def test_a_failing_cancellation_still_leaves_the_watchdog(
    rec: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The unwind is the best effort; the armed watchdog is the promise. Targets
    # the real call so the guard, not a stub, is what is under test.
    def _boom() -> None:
        raise RuntimeError("no running event loop")

    monkeypatch.undo()  # restore the real _cancel_everything
    monkeypatch.setattr(signal_shutdown.os, "_exit", rec.exits.append)
    monkeypatch.setattr(signal_shutdown, "_signalled", False)
    monkeypatch.setattr(signal_shutdown.asyncio, "all_tasks", _boom)
    handler = _signals(rec)
    handler._on_signal(signal.SIGTERM)  # must not raise
    assert rec.armed == 1


def test_a_signalled_unwind_is_not_a_crash(rec: _Recorder) -> None:
    """The unwind arrives as a CancelledError, identical in shape to a real
    failure and opposite in meaning. Both halves are required: a signal, and
    nothing but cancellation."""
    cancelled = asyncio.CancelledError()
    assert signal_shutdown.was_signal_shutdown(cancelled) is False  # no signal yet

    _signals(rec)._on_signal(signal.SIGTERM)
    assert signal_shutdown.was_signal_shutdown(cancelled) is True
    assert signal_shutdown.was_signal_shutdown(BaseExceptionGroup("g", [cancelled])) is True
    # anyio nests groups, so the walk has to recurse rather than look one deep.
    nested = BaseExceptionGroup("outer", [BaseExceptionGroup("inner", [cancelled])])
    assert signal_shutdown.was_signal_shutdown(nested) is True
    # A real failure that merely arrives after a signal is still a failure.
    assert signal_shutdown.was_signal_shutdown(RuntimeError("boom")) is False
    mixed = BaseExceptionGroup("g", [cancelled, RuntimeError("boom")])
    assert signal_shutdown.was_signal_shutdown(mixed) is False


async def test_install_and_remove_take_and_return_both_signals(rec: _Recorder) -> None:
    handler = _signals(rec)
    loop = asyncio.get_running_loop()
    handler.install()
    assert handler._installed == [signal.SIGTERM, signal.SIGINT]
    try:
        # A real delivery goes through the loop's handler, not a direct call.
        signal.raise_signal(signal.SIGTERM)
        # Delivery reaches the loop through its self-pipe, so it needs a real
        # turn of the loop rather than a bare sleep(0).
        for _ in range(200):
            if rec.cancelled:
                break
            await asyncio.sleep(0.01)
        assert rec.cancelled == 1
    finally:
        handler.remove()
    assert handler._installed == []
    # Really given back: the loop no longer holds a handler for either.
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: None)
        loop.remove_signal_handler(sig)


async def test_install_never_fails_startup(rec: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-main thread cannot install one at all, and Windows has no
    # add_signal_handler. Neither is worth refusing to serve over.
    def _unsupported(*_args: object, **_kwargs: object) -> None:
        raise NotImplementedError

    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", _unsupported)
    handler = _signals(rec)
    handler.install()
    assert handler._installed == []
    handler.remove()  # symmetric: removing what was never installed is fine


def test_a_signal_during_teardown_stops_the_waiting(rec: _Recorder) -> None:
    """Handing the signals back at the start of teardown would put the default
    disposition in charge for the whole of it — SIGTERM killing the process
    mid-sweep, SIGINT raising into a main thread that unwinds nothing. That is
    the window this exists to survive, so a signal arriving there means "stop
    waiting" instead."""
    handler = _signals(rec)
    handler.entering_teardown()
    handler._on_signal(signal.SIGTERM)
    assert rec.exits == [128 + int(signal.SIGTERM)]
    assert rec.cancelled == 0  # not a second, re-entrant shutdown


def test_tasks_that_predate_us_are_not_ours_to_cancel(
    rec: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A host mounting this lifespan owns its own tasks, the same way it owns
    # children that predate us. Cancelling those reaches outside this process's
    # ownership of itself.
    monkeypatch.undo()  # the fixture stubs _cancel_everything; test the real one

    async def _drive() -> None:
        foreign = asyncio.ensure_future(asyncio.sleep(30))
        handler = _signals(rec)
        handler.install()
        try:
            mine = asyncio.ensure_future(asyncio.sleep(30))
            await asyncio.sleep(0)
            handler._cancel_everything()
            with contextlib.suppress(asyncio.CancelledError):
                await mine
            assert mine.cancelled()
            assert not foreign.cancelled() and not foreign.done()
        finally:
            handler.remove()
            for task in (foreign, mine):
                task.cancel()

    asyncio.run(_drive())


def test_a_truncated_teardown_does_not_report_success(rec: _Recorder) -> None:
    """A CancelledError out of a teardown step whose guard only catches
    Exception skips every later close. Exiting 0 there would be this code
    asserting something it does not know."""
    signal_shutdown.exit_after_signal_shutdown()
    assert rec.exits == [1]


def test_a_finished_signal_shutdown_takes_the_exit(rec: _Recorder) -> None:
    """Returning from main is not enough: the SDK reads stdin on a non-daemon
    thread, still blocked on a pipe whose write end the client — alive, it only
    signalled us — still holds. The interpreter waits for that thread, so a
    perfect teardown can still leave the process running, which is the #906
    shape itself. Measured: without this the process sat there until killed."""
    signal_shutdown.teardown_completed()
    signal_shutdown.exit_after_signal_shutdown()
    assert rec.exits == [0]
