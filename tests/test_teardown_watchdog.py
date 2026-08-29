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
    watchdog = TeardownWatchdog(5.0)
    watchdog.arm()
    watchdog.disarm()
    time.sleep(0.2)
    assert no_real_exit.codes == []


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


def test_a_failing_pre_exit_hook_still_exits(no_real_exit: _Exits) -> None:
    # Best-effort cleanup must not become the new reason we fail to exit.
    def _boom() -> None:
        raise RuntimeError("sweep exploded")

    TeardownWatchdog(0.05, before_exit=_boom).arm()
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
