"""Turn SIGTERM/SIGINT into the shutdown the server already knows how to do.

Without a handler the default disposition applies: the process dies where it
stands, the lifespan ``finally`` never runs, and every stdio child it owns is
reparented — the #906 end state, reached by the one command an operator reaches
for first. So an operator's signal should not be a blunter instrument than the
client simply going away.

The server's own shutdown trigger is stdin EOF, read several layers down inside
the MCP SDK's ``stdio_server``. Closing fd 0 to fake that EOF looks like the
tidy answer and does not work: the read is already blocked in a worker thread,
and closing the descriptor underneath it does not wake it (measured — the
process then sat until the watchdog killed it). What does work is cancelling
the loop's tasks: the SDK's task group unwinds, the lifespan's ``finally``
runs, and shutdown takes the same bounded, child-sweeping path a departing
client produces.

That unwind surfaces as a ``CancelledError`` out of ``mcp.run()``, which is a
clean exit here and nothing like the crash it would otherwise be classified as,
so :func:`was_signal_shutdown` lets ``main`` tell the two apart.

The watchdog is armed at the same moment regardless, because none of this is a
promise: it is the mechanism, and the watchdog is the guarantee. A second
signal skips straight to the exit, because an operator who signals twice has
said what they want.

For the record, what the defaults do without any of this: SIGTERM kills the
process where it stands, leaving every stdio child reparented, and SIGINT
raises KeyboardInterrupt into a main thread parked inside the event loop, where
it unwinds nothing — the process simply never exits. Both are #906.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Set once, process-wide, by the first shutdown signal. ``main`` reads it to
# classify the CancelledError that the resulting unwind delivers: identical in
# shape to a crash, opposite in meaning.
_signalled = False


def exit_after_signal_shutdown() -> None:
    """End the process now that a signalled teardown has finished.

    Returning from ``main`` is not enough. The MCP SDK reads stdin on a
    non-daemon worker thread, and that read is still blocked on a pipe whose
    write end the client — very much alive, it only sent us a signal — still
    holds. The interpreter waits for that thread at exit, so the teardown can
    complete perfectly and the process still never goes away, which is the #906
    shape this whole change is about.

    Everything that needed to happen has happened: the teardown ran, the
    children were swept. All that is left is the exit, so take it.
    """
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:  # pragma: no cover — a handler that cannot flush
            pass
    os._exit(0)


def was_signal_shutdown(exc: BaseException) -> bool:
    """True when *exc* is the unwind a shutdown signal asked for.

    Requires both halves: a signal was delivered, and the exception is nothing
    but cancellation. A real failure that happens to arrive after a signal is
    still a failure, and cancellation with no signal behind it is not this.
    """
    if not _signalled:
        return False
    if isinstance(exc, asyncio.CancelledError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return all(was_signal_shutdown(inner) for inner in exc.exceptions)
    return False


# 128 + signum, the shell convention for "died from this signal".
_SIGNAL_EXIT_BASE = 128

_SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class ShutdownSignals:
    """Routes SIGTERM/SIGINT into the normal shutdown, then into a hard exit.

    *arm_watchdog* is called on the first signal so the exit is guaranteed even
    if the unwind does not reach the end. *hard_exit_cleanup* runs before the
    second signal's immediate exit — that path skips every ``finally`` in the
    process, so anything that must happen has to happen there.
    """

    def __init__(
        self,
        *,
        arm_watchdog: Callable[[], None],
        hard_exit_cleanup: Callable[[], None] | None = None,
    ) -> None:
        self._arm_watchdog = arm_watchdog
        self._hard_exit_cleanup = hard_exit_cleanup
        self._signalled = False
        self._installed: list[int] = []

    def install(self) -> None:
        """Take over SIGTERM/SIGINT. Never raises: a server that cannot install
        a handler still has to serve."""
        loop = asyncio.get_running_loop()
        for sig in _SHUTDOWN_SIGNALS:
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except (NotImplementedError, RuntimeError, ValueError, OSError):
                # Windows has no add_signal_handler, and a non-main thread
                # cannot install one at all. Neither is worth failing startup.
                logger.debug("Could not install a handler for %s", sig, exc_info=True)
                continue
            self._installed.append(sig)

    def remove(self) -> None:
        """Give the signals back. Called as teardown begins, so a signal
        arriving mid-teardown gets the default disposition rather than a
        second, re-entrant shutdown."""
        loop = asyncio.get_running_loop()
        for sig in self._installed:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError, OSError):
                logger.debug("Could not remove the handler for %s", sig, exc_info=True)
        self._installed.clear()

    def _on_signal(self, sig: int) -> None:
        global _signalled
        if self._signalled:
            self._exit_now(sig)
            return
        self._signalled = True
        _signalled = True
        logger.info(
            "Received %s — shutting down. Signal again to exit immediately.",
            signal.Signals(sig).name,
        )
        # Armed first: cancellation is the mechanism, this is the guarantee.
        self._arm_watchdog()
        self._cancel_everything()

    @staticmethod
    def _cancel_everything() -> None:
        """Unwind the SDK's task group so the lifespan's ``finally`` runs.

        Cancelling every task rather than one we own, because the task doing
        the blocking read belongs to the SDK and there is no handle to it here.
        Never raises: the watchdog is what has to survive this.
        """
        try:
            for task in asyncio.all_tasks():
                task.cancel()
        except RuntimeError:  # pragma: no cover — no running loop
            logger.debug("Could not cancel tasks to start shutdown", exc_info=True)

    def _exit_now(self, sig: int) -> None:
        logger.warning("Received %s again — exiting immediately.", signal.Signals(sig).name)
        if self._hard_exit_cleanup is not None:
            try:
                self._hard_exit_cleanup()
            except BaseException:  # noqa: BLE001 — nothing may cost us the exit
                pass
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:  # pragma: no cover — a handler that cannot flush
                pass
        os._exit(_SIGNAL_EXIT_BASE + sig)
