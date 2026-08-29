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


_teardown_completed = False


def teardown_completed() -> None:
    """Record that the lifespan teardown ran to the end."""
    global _teardown_completed
    _teardown_completed = True


def exit_after_signal_shutdown() -> None:
    """End the process now that a signalled teardown has finished.

    Returning from ``main`` is not enough. The MCP SDK reads stdin on a
    non-daemon worker thread, and that read is still blocked on a pipe whose
    write end the client — very much alive, it only sent us a signal — still
    holds. The interpreter waits for that thread at exit, so the teardown can
    complete perfectly and the process still never goes away, which is the #906
    shape this whole change is about.

    The status says which of those two things happened. A teardown that ran to
    the end earned a 0; one cut short — a ``CancelledError`` out of a step whose
    guard only catches ``Exception`` skips every later close — did not, and
    reporting success there would be this code asserting something it does not
    know. ``os._exit`` skips atexit hooks and finalizers either way, so the
    difference is exactly whether the explicit close sequence completed.
    """
    if not _teardown_completed:
        logger.warning(
            "Shutdown was signalled but teardown did not run to the end — "
            "exiting anyway; some resources were closed by the OS, not by us"
        )
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:  # pragma: no cover — a handler that cannot flush
            pass
    os._exit(0 if _teardown_completed else 1)


def shutdown_was_signalled() -> bool:
    """True once a shutdown signal has been handled in this process.

    ``mcp.run()`` may return normally after the cancellation unwind rather than
    raising — measured: it does exactly that when the proxy owns an upstream
    connection — so the exit cannot be hung off the exception alone.
    """
    return _signalled


def was_signal_shutdown(exc: BaseException) -> bool:
    """True when *exc* is the unwind a shutdown signal asked for.

    Requires both halves: a signal was delivered, and the exception is nothing
    but cancellation. A real failure that happens to arrive after a signal is
    still a failure, and cancellation with no signal behind it is not this.

    anyio's strict task groups deliver the unwind as a ``BaseExceptionGroup``
    (``CancelledError`` is a ``BaseException``, so the group cannot be a plain
    ``ExceptionGroup``), and those nest, hence the recursion — with the
    requirement that something actually matched, since ``all([])`` would
    otherwise read an empty group as a clean shutdown.
    """
    if not _signalled:
        return False
    return _is_only_cancellation(exc)


def _is_only_cancellation(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.CancelledError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(
            _is_only_cancellation(inner) for inner in exc.exceptions
        )
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
        self._tearing_down = False
        self._installed: list[int] = []
        self._foreign_tasks: frozenset[asyncio.Task[object]] = frozenset()

    def install(self) -> None:
        """Take over SIGTERM/SIGINT. Never raises: a server that cannot install
        a handler still has to serve."""
        global _signalled
        _signalled = False  # per-run: a previous run's signal is not ours
        # Tasks that predate us belong to whoever is hosting this lifespan, the
        # same way children that predate us do. Cancelling those would be this
        # module reaching outside its own process ownership.
        try:
            self._foreign_tasks = frozenset(asyncio.all_tasks())
        except RuntimeError:  # pragma: no cover — no running loop
            self._foreign_tasks = frozenset()
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

    def entering_teardown(self) -> None:
        """Teardown has begun: from here a signal means "stop waiting".

        Deliberately not a ``remove()``. Handing the signals back would put the
        default disposition in charge for the whole of teardown — SIGTERM
        killing the process mid-sweep, SIGINT raising into a main thread that
        unwinds nothing — which is precisely the window this shutdown exists to
        survive. Instead the next signal takes the immediate-exit path, and a
        signal that started this teardown does not re-enter it.
        """
        self._tearing_down = True

    def remove(self) -> None:
        """Give the signals back — for a lifespan that ends without exiting the
        process (a host unmounting us), where leaving handlers installed would
        outlive what they were guarding."""
        loop = asyncio.get_running_loop()
        for sig in self._installed:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError, ValueError, OSError):
                logger.debug("Could not remove the handler for %s", sig, exc_info=True)
        self._installed.clear()

    def _on_signal(self, sig: int) -> None:
        global _signalled
        if self._signalled or self._tearing_down:
            # Either a second signal, or one arriving while we are already
            # shutting down. Both mean the same thing: stop waiting.
            self._exit_now(sig)
            return
        self._signalled = True
        _signalled = True
        # Armed before anything that can block — a wedged logging handler must
        # not be able to cost us the backstop.
        self._arm_watchdog()
        logger.info(
            "Received %s — shutting down. Signal again to exit immediately.",
            signal.Signals(sig).name,
        )
        self._cancel_everything()

    def _cancel_everything(self) -> None:
        """Unwind the SDK's task group so the lifespan's ``finally`` runs.

        Cancelling tasks rather than one we own, because the task doing the
        blocking read belongs to the SDK and there is no handle to it here —
        but not tasks that predate us, which belong to a host that mounted this
        lifespan rather than to this shutdown. Never raises: the watchdog is
        what has to survive this.
        """
        try:
            for task in asyncio.all_tasks():
                if task not in self._foreign_tasks:
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
