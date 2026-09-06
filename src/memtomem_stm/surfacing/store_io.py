"""The worker thread that keeps feedback-store SQLite off the event loop.

:class:`~memtomem_stm.surfacing.feedback_store.FeedbackStore` writes are cheap
when nothing else holds the database — 0.03 ms for a typical ``mark_surfaced``
— but the write lock is file-wide across every process pointing at
``stm_feedback.db``: the proxy's own store, ``mms tune``'s retention purge, a
second daemon. A write that has to wait one of those out spends the
connection's ``busy_timeout`` (3 s) inside a blocking C call, and on the event
loop that freezes *everything*: the other surfacing calls the daemon admitted
concurrently (#874), and the ``asyncio.timeout_at`` timers that exist to shed
requests whose client already gave up. Sending those writes here is what keeps
the loop answering while one of them waits (#996).

One worker, deliberately:

- **Its own executor, not** ``asyncio.to_thread``'s default pool. That pool is
  shared with the proxy's ``compress()`` and would put a feedback write behind
  an unrelated CPU-bound batch — the same coupling that delayed prompt upstream
  responses until the call timeout in #956.
- **A single thread**, so writes execute in submission order and the row order
  on disk matches the order the engine asked for. Concurrency here would buy
  nothing anyway: they all contend for one write lock.

The thread is created on first use and reclaimed at interpreter shutdown like
any other executor's, so there is no lifecycle to plumb through callers, and a
process that never surfaces never starts it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_EXECUTOR_THREAD_PREFIX = "stm-feedback-io"

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()

T = TypeVar("T")


def feedback_io_executor() -> ThreadPoolExecutor:
    """Return the shared single-thread executor for feedback-store I/O."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=_EXECUTOR_THREAD_PREFIX
            )
        return _executor


def submit_store_write(fn: Callable[..., T], /, *args: Any) -> Future[T]:
    """Queue *fn* on the worker without waiting for it.

    For the best-effort writes whose result nobody reads (fault counters,
    diagnostics, the retention sweeps). The caller owns the returned future:
    it must consume the exception — asyncio never sees it — and should keep a
    reference until it completes so shutdown can wait on what is outstanding.
    """
    return feedback_io_executor().submit(fn, *args)


async def run_off_loop(fn: Callable[..., T], /, *args: Any) -> T:
    """Run *fn* on the worker and await its result, unshielded.

    Cancelling the awaiting task cancels a still-queued call outright — it
    never runs — while one already executing runs to completion with its
    result discarded. Only for work a caller may freely abandon; anything
    whose in-memory consequences have already happened wants
    :func:`await_store_write`.
    """
    return await asyncio.get_running_loop().run_in_executor(feedback_io_executor(), fn, *args)


async def await_store_write(fn: Callable[..., T], /, *args: Any, timeout: float) -> T:
    """Run *fn* on the worker, keeping it once queued and bounding the wait.

    The shield is what keeps the write: by the time these run the caller's
    in-memory state already assumes the row exists, so letting a cancellation
    drop a still-queued write would leave the process acting on a row that
    never landed. A cancellation still reaches the caller — only the write
    survives it.

    The bound is for the queue, not the statement (``busy_timeout`` covers
    that): one FIFO worker means this call waits for everything ahead of it,
    including another caller's retention sweep. A write that outlives
    *timeout* still lands; the caller stops waiting and degrades as it would
    for a failed write.
    """
    future = asyncio.ensure_future(run_off_loop(fn, *args))
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        # Consume whatever it ends up doing so asyncio does not report an
        # orphaned exception from a write nobody is waiting for any more.
        future.add_done_callback(_consume_abandoned_write)
        raise


def _consume_abandoned_write(future: "asyncio.Future[Any]") -> None:
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        logger.debug("Abandoned feedback-store write failed: %s", exc)
