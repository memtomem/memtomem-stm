"""Bounded lock acquisition (#208 — P1b follow-up to #206).

An ``asyncio.Lock`` hang (deadlock, stuck holder) is a class of internal
bug that #206's upstream ``call_timeout_seconds`` does not cover. This
module provides a diagnostic-friendly bounded acquisition helper.

Semantically distinct from #207's LLM compression timeout: that one
degrades gracefully (falls back to truncate). A lock timeout here
indicates a bug and propagates as an MCP error so the stuck holder is
visible in logs + metrics.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
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
async def bounded_lock(lock: asyncio.Lock, *, timeout: float, name: str) -> AsyncIterator[None]:
    """Acquire *lock* within *timeout* seconds or raise ``LockTimeoutError``.

    Intended for internal state locks where a timeout indicates a bug
    (deadlock, stuck holder), not a slow dependency. Emits
    ``logger.error`` with current-task diagnostics on timeout so the
    deadlocked holder is visible in production logs.
    """
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        current = asyncio.current_task()
        current_name = current.get_name() if current else "<no-task>"
        logger.error(
            "bounded_lock timeout acquiring %r after %.1fs — "
            "likely deadlock or stuck holder (current task: %s)",
            name,
            timeout,
            current_name,
        )
        raise LockTimeoutError(name, timeout) from exc
    try:
        yield
    finally:
        lock.release()
