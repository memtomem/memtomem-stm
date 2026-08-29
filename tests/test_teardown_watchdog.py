"""Tests for utils/teardown_watchdog.py — the hard-exit backstop (#906)."""

from __future__ import annotations

import threading
import time

import pytest

from memtomem_stm.utils import teardown_watchdog
from memtomem_stm.utils.teardown_watchdog import TeardownWatchdog


class _Exits:
    """Records hard exits and lets a test wait for one."""

    def __init__(self) -> None:
        self.codes: list[int] = []
        self.fired = threading.Event()

    def record(self, code: int) -> None:
        self.codes.append(code)
        self.fired.set()

    def wait(self) -> bool:
        return self.fired.wait(5.0)


@pytest.fixture
def no_real_exit(monkeypatch: pytest.MonkeyPatch) -> _Exits:
    """Catch the hard exit — an unpatched ``os._exit`` kills the test run."""
    exits = _Exits()
    monkeypatch.setattr(teardown_watchdog.os, "_exit", exits.record)
    return exits


def test_a_teardown_that_never_finishes_exits_the_process(no_real_exit: _Exits) -> None:
    # The whole point: a wedged stop() must not be able to keep the process
    # alive, since it has nothing left to do but exit.
    TeardownWatchdog(0.05).arm()
    assert no_real_exit.wait()
    assert no_real_exit.codes == [1]


def test_a_finished_teardown_is_left_alone(no_real_exit: _Exits) -> None:
    # The wait has to run past the deadline for this to mean anything: with a
    # long timeout and a short sleep it would pass even if disarm were a no-op.
    watchdog = TeardownWatchdog(0.05)
    watchdog.arm()
    watchdog.disarm()
    assert watchdog._thread is not None
    watchdog._thread.join(timeout=5.0)
    assert not watchdog._thread.is_alive()  # it woke and stood down
    assert no_real_exit.codes == []


def test_a_timeout_that_is_not_a_bound_does_not_disable_the_backstop(
    no_real_exit: _Exits,
) -> None:
    """`+inf` and NaN are not "never" — they are non-answers that would kill the
    watchdog thread inside its own wait (Event.wait raises past TIMEOUT_MAX,
    NaN compares False against every check) and silently take the guarantee
    with them. The config field rejects both, but these models are mutable and
    assignment is not re-validated."""
    assert TeardownWatchdog(float("inf"))._timeout == threading.TIMEOUT_MAX
    nan_watchdog = TeardownWatchdog(float("nan"))
    nan_watchdog.arm()
    assert nan_watchdog._thread is None  # degraded to "off", not to a crash


def test_arming_never_raises_at_the_cost_of_the_shutdown(
    no_real_exit: _Exits, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Arming is the first thing a teardown does; thread exhaustion is exactly
    # the state a wedged process reaches. Losing the backstop is survivable,
    # losing the shutdown it guards is not.
    def _cannot_start(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", _cannot_start)
    watchdog = TeardownWatchdog(0.05)
    with caplog.at_level("WARNING", logger=teardown_watchdog.__name__):
        watchdog.arm()
    assert watchdog._thread is None
    assert any("without a backstop" in r.getMessage() for r in caplog.records)


def test_zero_disables_the_backstop(no_real_exit: _Exits) -> None:
    # The off switch has to work without the call sites growing a branch.
    watchdog = TeardownWatchdog(0.0)
    watchdog.arm()
    assert watchdog._thread is None
    time.sleep(0.2)
    assert no_real_exit.codes == []


def test_arming_twice_starts_one_countdown(no_real_exit: _Exits) -> None:
    watchdog = TeardownWatchdog(5.0)
    watchdog.arm()
    first = watchdog._thread
    watchdog.arm()
    assert watchdog._thread is first
    watchdog.disarm()


def test_children_are_reaped_before_the_hard_exit(no_real_exit: _Exits) -> None:
    """os._exit runs no atexit hooks and no finally blocks, so whatever the
    wedged teardown abandoned is gone for good unless it is collected here —
    and a child outliving the process is the whole of #906."""
    swept: list[int] = []
    TeardownWatchdog(0.05, before_exit=lambda: swept.append(1)).arm()
    assert no_real_exit.wait()
    assert swept == [1]


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("sweep exploded"), KeyboardInterrupt()],
    ids=["exception", "base-exception"],
)
def test_a_failing_pre_exit_hook_still_exits(no_real_exit: _Exits, failure: BaseException) -> None:
    # Best-effort cleanup must not become the new reason we fail to exit — and
    # not only for Exception: a BaseException escaping would kill the daemon
    # thread and take the one guarantee this class makes with it.
    def _boom() -> None:
        raise failure

    TeardownWatchdog(0.05, before_exit=_boom).arm()
    assert no_real_exit.wait()
    assert no_real_exit.codes == [1]


def test_a_wedged_logger_still_exits(no_real_exit: _Exits, monkeypatch: pytest.MonkeyPatch) -> None:
    # The diagnostics are best-effort too: a handler stuck on its own lock, or
    # one that raises, must not swallow the exit.
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("log target gone")

    monkeypatch.setattr(teardown_watchdog.logger, "critical", _boom)
    TeardownWatchdog(0.05).arm()
    assert no_real_exit.wait()
    assert no_real_exit.codes == [1]


def test_it_reports_why_before_exiting(
    no_real_exit: _Exits, caplog: pytest.LogCaptureFixture
) -> None:
    # The hard exit truncates everything after it, so the reason has to be on
    # the record before it happens or the shutdown looks merely abrupt.
    with caplog.at_level("CRITICAL", logger=teardown_watchdog.__name__):
        TeardownWatchdog(0.05).arm()
        assert no_real_exit.wait()
    assert any("Teardown did not finish" in r.getMessage() for r in caplog.records)
