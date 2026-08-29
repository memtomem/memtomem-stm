"""A hard-exit backstop for a teardown that never finishes.

Bounding each teardown step is not the same as guaranteeing the process exits.
``asyncio.wait_for`` cancels the awaitable and then waits for that cancellation
to *land*, so a ``stop()`` that swallows ``CancelledError`` — or that is stuck
in a synchronous call the loop cannot interrupt, where the timeout callback
never even runs — outlives its ceiling. Either way the process is past the last
byte its client will ever send and has nothing left to do but exit, which is
exactly the state #906 found 57 processes in, eight days later, still holding
an LTM child.

So the guarantee lives outside the event loop. Arming starts a plain daemon
thread; if teardown has not disarmed it by the deadline, that thread reaps what
it can and calls ``os._exit``. It exists only for the window between "teardown
started" and "teardown finished", so it can never fire during normal operation.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

# ``threading.Event.wait`` raises OverflowError past the platform's timeout
# ceiling, which would kill the watchdog thread and take the guarantee with it.
# A timeout this long is not a bound anyone chose; clamping keeps it one.
_MAX_WAIT_SECONDS = threading.TIMEOUT_MAX

# How long the diagnostics and the child sweep get before the exit happens
# anyway. Long enough for a pgrep and a SIGTERM grace, short enough that a
# wedged handler cannot turn the backstop into another way of not exiting.
_PRE_EXIT_BUDGET_SECONDS = 10.0


class TeardownWatchdog:
    """Exit the process if teardown has not finished within *timeout_seconds*.

    A non-positive timeout disables it: :meth:`arm` becomes a no-op, so an
    operator can turn the backstop off without the call sites growing a branch.
    """

    def __init__(
        self, timeout_seconds: float, *, before_exit: Callable[[], None] | None = None
    ) -> None:
        if math.isnan(timeout_seconds):
            # NaN is not a deadline: it compares False against every check and
            # would collapse inside the wait, killing the thread. There is no
            # honest deadline to substitute, so it reads as "off" — the same
            # answer as 0, reached without a dead thread pretending to guard.
            # The config field rejects it; these models are mutable and
            # assignment is not re-validated, so the backstop does not rely on
            # that.
            timeout_seconds = 0.0
        self._timeout = min(timeout_seconds, _MAX_WAIT_SECONDS)
        self._before_exit = before_exit
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def arm(self) -> None:
        """Start the countdown. Idempotent; harmless when already disarmed.

        Raises nothing an ``Exception`` covers. Arming is the first thing a
        teardown does, and thread exhaustion is exactly the state a wedged
        process reaches — failing here must cost the backstop, not the shutdown
        it was guarding. A ``BaseException`` (an interrupt arriving on the main
        thread mid-arm) still propagates: that is the operator asking to stop,
        not a failure to swallow.
        """
        if self._timeout <= 0 or self._thread is not None:
            return
        try:
            thread = threading.Thread(
                target=self._wait_and_fire, name="stm-teardown-watchdog", daemon=True
            )
            thread.start()
        except Exception:
            self._quiet(
                lambda: logger.warning(
                    "Could not start the teardown watchdog — shutting down without a backstop",
                    exc_info=True,
                )
            )
            return
        self._thread = thread

    def disarm(self) -> None:
        """Teardown finished — stand the countdown down."""
        self._done.set()

    def _wait_and_fire(self) -> None:
        if self._done.wait(self._timeout):
            return
        self._fire()

    def _fire(self) -> None:
        # Everything here is best-effort and everything here can block or raise
        # — a logging handler can wedge on its own lock, the sweep shells out to
        # pgrep. None of it may cost us the exit, which is the only thing this
        # class actually promises, so the exit is in a finally and each step is
        # guarded on its own.
        try:
            # On its own thread with a join ceiling, because swallowing errors
            # does not bound a *hang*: a logging handler can block on its own
            # lock and the sweep shells out to pgrep. Whatever has not finished
            # by then is abandoned — the exit is the only thing promised here.
            helper = threading.Thread(
                target=self._best_effort_cleanup, name="stm-teardown-watchdog-exit", daemon=True
            )
            helper.start()
            helper.join(timeout=_PRE_EXIT_BUDGET_SECONDS)
        except Exception:
            pass
        finally:
            os._exit(1)

    def _best_effort_cleanup(self) -> None:
        # Never stdout: by the time this runs, the client's pipe is gone and
        # stdout is the MCP transport. Logging goes to stderr and the optional
        # rotating file.
        self._quiet(
            lambda: logger.critical(
                "Teardown did not finish within %.1fs — exiting the hard way. "
                "A stop() is wedged; the process would otherwise outlive its client "
                "(#906). Children it abandoned are terminated first.",
                self._timeout,
            )
        )
        if self._before_exit is not None:
            self._quiet(self._before_exit)
        # By hand: os._exit runs no atexit hooks, so nothing else flushes.
        for handler in logging.getLogger().handlers:
            self._quiet(handler.flush)

    @staticmethod
    def _quiet(step: Callable[[], object]) -> None:
        try:
            step()
        except BaseException:  # noqa: BLE001 — nothing may cost us the exit
            pass
