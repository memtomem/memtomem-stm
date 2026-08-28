"""Bounded lock acquisition (#208 — P1b follow-up to #206).

An ``asyncio.Lock`` hang (deadlock, stuck holder) is a class of internal
bug that #206's upstream ``call_timeout_seconds`` does not cover. This
module provides a diagnostic-friendly bounded acquisition helper.

Semantically distinct from #207's LLM compression timeout: that one
degrades gracefully (falls back to truncate). A lock timeout here
propagates as an MCP error so the stuck holder is visible in logs +
metrics. It USUALLY indicates a bug — the exception to that is a hold a
caller knows to be legitimate, such as shutdown, which it declares through
``expected`` so the timeout is not reported as a likely deadlock.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class LockTimeoutError(asyncio.TimeoutError):
    """Raised by ``bounded_lock`` when acquisition exceeds the timeout.

    Subclasses ``asyncio.TimeoutError`` so callers that already handle
    generic timeouts keep working. The specific subclass lets the
    pipeline error classifier tag the metric as
    ``ErrorCategory.LOCK_TIMEOUT`` rather than the generic
    ``ErrorCategory.TIMEOUT`` (which is reserved for upstream
    ``call_tool`` timeouts, #206).
    """

    def __init__(self, lock_name: str, timeout: float) -> None:
        super().__init__(
            f"bounded_lock timeout acquiring {lock_name!r} after {timeout:.1f}s — "
            "likely deadlock or stuck holder"
        )
        self.lock_name = lock_name
        self.timeout_seconds = timeout


@asynccontextmanager
async def bounded_lock(
    lock: asyncio.Lock,
    *,
    timeout: float,
    name: str,
    expected: Callable[[], bool] | None = None,
) -> AsyncIterator[None]:
    """Acquire *lock* within *timeout* seconds or raise ``LockTimeoutError``.

    Intended for internal state locks where a timeout ordinarily indicates a
    bug (deadlock, stuck holder), not a slow dependency. Emits
    ``logger.error`` with current-task diagnostics on timeout so the
    deadlocked holder is visible in production logs — unless the caller
    declares the hold legitimate through ``expected``, below.

    ``expected`` lets a caller that knows of a LEGITIMATE long hold say so:
    it is consulted only on timeout, and a true answer downgrades the ERROR
    to INFO. The exception is unchanged — only the operational alarm is.
    Shutdown is the motivating case: teardown holds some of these locks
    across awaits by design, and reporting that as a likely deadlock spends
    the alarm's credibility on an event that is not one.
    """
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        current = asyncio.current_task()
        current_name = current.get_name() if current else "<no-task>"
        anticipated = False
        if expected is not None:
            try:
                anticipated = bool(expected())
            except Exception:  # pragma: no cover - predicate must not mask the timeout
                logger.debug("bounded_lock 'expected' predicate raised", exc_info=True)
        log = logger.info if anticipated else logger.error
        log(
            "bounded_lock timeout acquiring %r after %.1fs — %s (current task: %s)",
            name,
            timeout,
            "holder is shutting down, as anticipated by the caller"
            if anticipated
            else "likely deadlock or stuck holder",
            current_name,
        )
        raise LockTimeoutError(name, timeout) from exc
    try:
        yield
    finally:
        lock.release()
