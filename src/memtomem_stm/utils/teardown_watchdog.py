"""A hard-exit backstop for a teardown that never finishes.

Bounding each teardown step is not the same as guaranteeing the process exits.
``asyncio.wait_for`` cancels *the wait*, not the work: ``ProxyManager.stop()``
reaches ``_UpstreamConnectionOwner.close()``, which awaits a shielded task, and
a child ignoring the transport's terminate keeps that shield closed for as long
as it likes. The teardown can also be wedged somewhere with no ``await`` at all
to cancel. Either way the process is past the last byte its client will ever
send and has nothing left to do but exit — which is exactly the state #906
found 57 processes in, eight days later, still holding an LTM child.

So the guarantee lives outside the event loop. Arming starts a plain daemon
thread; if teardown has not disarmed it by the deadline, that thread reaps what
it can and calls ``os._exit``. It exists only for the window between "teardown
started" and "teardown finished", so it can never fire during normal operation.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)


class TeardownWatchdog:
    """Exit the process if teardown has not finished within *timeout_seconds*.

    A non-positive timeout disables it: :meth:`arm` becomes a no-op, so an
    operator can turn the backstop off without the call sites growing a branch.
    """

    def __init__(
        self, timeout_seconds: float, *, before_exit: Callable[[], None] | None = None
    ) -> None:
        self._timeout = timeout_seconds
        self._before_exit = before_exit
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def arm(self) -> None:
        """Start the countdown. Idempotent; harmless when already disarmed."""
        if self._timeout <= 0 or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._wait_and_fire, name="stm-teardown-watchdog", daemon=True
        )
        self._thread.start()

    def disarm(self) -> None:
        """Teardown finished — stand the countdown down."""
        self._done.set()

    def _wait_and_fire(self) -> None:
        if self._done.wait(self._timeout):
            return
        self._fire()

    def _fire(self) -> None:
        # Never stdout: by the time this fires, the client's pipe is gone and
        # stdout is the MCP transport. Logging goes to stderr and the optional
        # rotating file, and the handlers are flushed by hand because os._exit
        # runs no atexit hooks.
        logger.critical(
            "Teardown did not finish within %.1fs — exiting the hard way. "
            "A stop() is wedged; the process would otherwise outlive its client "
            "(#906). Children it abandoned are terminated first.",
            self._timeout,
        )
        if self._before_exit is not None:
            try:
                self._before_exit()
            except Exception:
                logger.critical("Pre-exit cleanup failed", exc_info=True)
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:  # pragma: no cover — a handler that cannot flush
                pass
        os._exit(1)
