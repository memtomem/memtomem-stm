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

from memtomem_stm.utils.sqlite_tuning import BUSY_TIMEOUT_MS

logger = logging.getLogger(__name__)

_EXECUTOR_THREAD_PREFIX = "stm-feedback-io"

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()

T = TypeVar("T")

MAX_QUEUED_WRITES = 64
"""Ceiling on writes waiting on the worker, awaited and queued alike.

A store nobody can write to — a peer holding it for minutes, a dead disk —
would otherwise let this queue grow at call rate: every caller stops waiting
after its own ceiling, but what it queued stays. Same shape and reason as the
background-task ceiling in ``ProxyManager`` (#868). Refusing is the honest
answer at that point: a best-effort write is dropped, and a write a caller
awaits raises, which is the same degradation those callers already handle for
a store that failed.
"""

_queued = 0
_queued_lock = threading.Lock()


class StoreWriteQueueFull(RuntimeError):
    """Raised instead of queueing past :data:`MAX_QUEUED_WRITES`."""


def queued_writes() -> int:
    """How many writes are queued or running on the worker."""
    with _queued_lock:
        return _queued


def _enqueue(fn: Callable[..., T], args: tuple[Any, ...]) -> Future[T]:
    global _queued
    with _queued_lock:
        if _queued >= MAX_QUEUED_WRITES:
            raise StoreWriteQueueFull(f"{_queued} feedback-store writes already queued")
        _queued += 1
    try:
        future = feedback_io_executor().submit(fn, *args)
    except BaseException:
        _release_slot()
        raise
    future.add_done_callback(lambda _f: _release_slot())
    return future


def _release_slot(*_args: Any) -> None:
    global _queued
    with _queued_lock:
        _queued -= 1


def feedback_io_executor() -> ThreadPoolExecutor:
    """Return the shared single-thread executor for feedback-store I/O."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=_EXECUTOR_THREAD_PREFIX
            )
        return _executor


def worker_started() -> bool:
    """Whether any write has been queued in this process yet.

    Lets a teardown skip the drain instead of starting a thread purely to
    watch it exit — the common case for a process that never surfaced.
    """
    with _executor_lock:
        return _executor is not None


STORE_WRITE_BUDGET_SECONDS = BUSY_TIMEOUT_MS / 1000.0 + 2.0
"""How long a caller waits for its own store write before giving up on it.

Derived from the store's own lock budget, not from the LTM timeout the rest of
a surfacing call is measured against: what this bounds is waiting out another
writer of ``stm_feedback.db``, which is what ``busy_timeout`` covers, plus
slack for a couple of those queued ahead on the single worker. Tying it to
``surfacing.timeout_seconds`` would make an operator who tightens the LTM
budget start seeing "store busy" answers from a database that is merely being
written by its neighbour.
"""

STORE_CLOSE_BUDGET_SECONDS = 2.0
"""How long a teardown waits for the store close it queued on the worker."""


async def close_store_on_worker(
    close: Callable[[], None], *, timeout: float = STORE_CLOSE_BUDGET_SECONDS
) -> None:
    """Close a store on the worker, after the writes already queued for it.

    Closing from the event loop takes the store's writer lock, which an
    in-flight write holds for as long as its statement runs — a peer process
    holding the database, or a wide retention ``DELETE``. That wait is not
    bounded by anything, and it lands at exactly the moment a shutdown is
    trying to finish. Sending the close down the same FIFO worker makes it the
    last operation rather than a competing one, and the wait here is bounded:
    past *timeout* the close still happens, just not before this returns.
    """
    if not worker_started():
        close()
        return
    try:
        await asyncio.wait_for(asyncio.shield(run_off_loop(close)), timeout=timeout)
    except asyncio.TimeoutError:
        # Its turn has not come. Retire the worker anyway — executor threads
        # are not daemons, so leaving one queued behind a long write would
        # hand that wait to interpreter exit — and finish the close on a
        # daemon thread, which releases the connection when the lock frees
        # without holding the process open for it.
        logger.debug("Feedback store close still queued behind an in-flight write")
        shutdown_worker()
        threading.Thread(target=close, name=f"{_EXECUTOR_THREAD_PREFIX}-close", daemon=True).start()
    else:
        shutdown_worker()


def shutdown_worker(*, wait: bool = False) -> None:
    """Retire the worker, dropping writes that have not started.

    Executor threads are not daemons, so the interpreter's own exit handler
    joins them — a teardown that gave up waiting for a close would still hand
    that wait to process exit, and everything queued behind it would run
    against a store that is already closed. Cancelling the queue and letting
    go is the honest end: what had not started was going to be a no-op anyway.

    The module goes back to its initial state, so a later write (a fresh
    server in the same process, the next test) starts a new worker rather than
    finding a dead one.
    """
    global _executor
    with _executor_lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=True)


def submit_store_write(fn: Callable[..., T], /, *args: Any) -> Future[T]:
    """Queue *fn* on the worker without waiting for it.

    For the best-effort writes whose result nobody reads (fault counters,
    diagnostics, the retention sweeps). The caller owns the returned future:
    it must consume the exception — asyncio never sees it — and should keep a
    reference until it completes so shutdown can wait on what is outstanding.

    Raises :class:`StoreWriteQueueFull` once the worker is that far behind.
    """
    return _enqueue(fn, args)


async def run_off_loop(fn: Callable[..., T], /, *args: Any) -> T:
    """Run *fn* on the worker and await its result, unshielded.

    Cancelling the awaiting task cancels a still-queued call outright — it
    never runs — while one already executing runs to completion with its
    result discarded. Only for work a caller may freely abandon; anything
    whose in-memory consequences have already happened wants
    :func:`await_store_write`.
    """
    return await asyncio.get_running_loop().run_in_executor(feedback_io_executor(), fn, *args)


async def await_store_write(fn: Callable[..., T], /, *args: Any, timeout: float | None = None) -> T:
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

    Raises :class:`StoreWriteQueueFull` rather than joining a queue already at
    its ceiling — the caller degrades as it would for a write that failed.
    *timeout* defaults to :data:`STORE_WRITE_BUDGET_SECONDS`.
    """
    # Read at call time, not bound as a default, so a test can shrink the
    # budget without every caller having to pass one.
    budget = STORE_WRITE_BUDGET_SECONDS if timeout is None else timeout
    future = asyncio.wrap_future(_enqueue(fn, args))
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=budget)
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
