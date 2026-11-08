"""``_compress_maybe_offthread`` releases a store pin exactly once (#1369).

A SELECTIVE/HYBRID call pins its pending store before an off-thread compress.
Cancelling the awaiting coroutine does not stop a worker thread that already
started, and a job still queued in the executor may never start, so the release
cannot live in the caller's ``finally``. These pin every path the helper's
hand-off has to cover.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from pathlib import Path

import pytest

from memtomem_stm.proxy.config import ProxyConfig
from memtomem_stm.proxy.delivery import delivery_format, delivery_scope
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.pending_store import SQLitePendingStore


class _BlockingScorer:
    uses_blocking_io = True


class _Inline:
    uses_blocking_io = False


class _Compressor:
    """Records each compress; optionally blocks on a gate and/or raises."""

    def __init__(self, *, gate: threading.Event | None = None, error: bool = False) -> None:
        self.gate = gate
        self.error = error
        self.started = threading.Event()
        self.calls = 0
        self.formats: list[str] = []

    def compress(self, text: str, *, max_chars: int, context_query: str | None) -> str:
        self.calls += 1
        self.formats.append(delivery_format())
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(10), "test gate never released"
        if self.error:
            raise ZeroDivisionError("compress failed")
        return text[:max_chars]


class _Release:
    def __init__(self) -> None:
        self.count = 0
        self.done = threading.Event()

    def __call__(self) -> None:
        self.count += 1
        self.done.set()


def _mgr(tmp_path: Path) -> ProxyManager:
    return ProxyManager(
        ProxyConfig(enabled=True, config_path=tmp_path / "absent.json"), TokenTracker()
    )


async def _run(mgr, compressor, release, scorer=None):
    return await mgr._compress_maybe_offthread(
        compressor,
        "abcdef",
        max_chars=3,
        context_query=None,
        scorer=scorer or _BlockingScorer(),
        release=release,
    )


def _capture_loop_errors() -> list[dict]:
    errors: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: errors.append(ctx))
    return errors


# --- inline ----------------------------------------------------------------


async def test_inline_releases_once_on_success(tmp_path):
    release = _Release()
    assert await _run(_mgr(tmp_path), _Compressor(), release, _Inline()) == "abc"
    assert release.count == 1


async def test_inline_releases_once_when_compress_raises(tmp_path):
    release = _Release()
    with pytest.raises(ZeroDivisionError):
        await _run(_mgr(tmp_path), _Compressor(error=True), release, _Inline())
    assert release.count == 1


# --- off-thread, caller awaiting -------------------------------------------


async def test_offthread_releases_once_on_success(tmp_path):
    release = _Release()
    assert await _run(_mgr(tmp_path), _Compressor(), release) == "abc"
    assert release.count == 1, "the caller released too, or the worker did not"


async def test_offthread_worker_error_propagates_and_releases_once(tmp_path):
    release = _Release()
    with pytest.raises(ZeroDivisionError):
        await _run(_mgr(tmp_path), _Compressor(error=True), release)
    assert release.count == 1


async def test_the_callers_context_reaches_the_worker_and_stays_its_own(tmp_path):
    mgr = _mgr(tmp_path)
    gate = threading.Event()
    structured, legacy = _Compressor(gate=gate), _Compressor(gate=gate)
    release = _Release()

    async def call(compressor, fmt):
        with delivery_scope(fmt):
            return await _run(mgr, compressor, release)

    calls = asyncio.gather(call(structured, "structured"), call(legacy, "legacy"))
    for compressor in (structured, legacy):
        assert await asyncio.to_thread(compressor.started.wait, 10)
    gate.set()
    await calls
    assert structured.formats == ["structured"] and legacy.formats == ["legacy"]
    assert release.count == 2


# --- off-thread, caller cancelled ------------------------------------------


async def test_a_cancelled_caller_leaves_the_release_to_its_running_worker(tmp_path):
    """Released when the worker ends, not when the caller is cancelled; the
    worker's later exception reaches nobody, as with ``asyncio.to_thread``."""
    errors = _capture_loop_errors()
    gate = threading.Event()
    compressor = _Compressor(gate=gate, error=True)
    release = _Release()
    task = asyncio.create_task(_run(_mgr(tmp_path), compressor, release))
    assert await asyncio.to_thread(compressor.started.wait, 10)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert release.count == 0, "released while the worker was still running"

    gate.set()
    assert await asyncio.to_thread(release.done.wait, 10)
    await asyncio.sleep(0.05)
    assert release.count == 1
    assert errors == [], f"the abandoned worker's exception reached the loop: {errors}"


async def test_a_job_cancelled_while_queued_is_released_and_never_compresses(tmp_path):
    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    blocker_gate = threading.Event()
    blocker = loop.run_in_executor(None, blocker_gate.wait, 10)
    compressor = _Compressor()
    release = _Release()
    try:
        task = asyncio.create_task(_run(_mgr(tmp_path), compressor, release))
        await asyncio.sleep(0.05)  # submitted and queued behind the blocker
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)  # the future's done-callbacks
        assert release.count == 1, "a queued job's pin was never released"
    finally:
        blocker_gate.set()
        await blocker
    await asyncio.sleep(0.05)
    assert compressor.calls == 0
    assert release.count == 1


async def test_a_job_the_executor_drops_unrun_is_released(tmp_path):
    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    blocker_gate = threading.Event()
    blocker = loop.run_in_executor(None, blocker_gate.wait, 10)
    compressor = _Compressor()
    release = _Release()
    task = asyncio.create_task(_run(_mgr(tmp_path), compressor, release))
    await asyncio.sleep(0.05)
    executor.shutdown(wait=False, cancel_futures=True)
    blocker_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await blocker
    assert compressor.calls == 0
    assert release.count == 1


class _QueueThenRaise(concurrent.futures.ThreadPoolExecutor):
    """``submit`` that queues the job and then fails to start a thread."""

    def submit(self, fn, /, *args, **kwargs):
        self.queued = (fn, args, kwargs)
        raise RuntimeError("can't start new thread")


async def test_a_failed_submit_releases_once_and_its_queued_job_skips(tmp_path):
    loop = asyncio.get_running_loop()
    executor = _QueueThenRaise(max_workers=1)
    loop.set_default_executor(executor)
    compressor = _Compressor()
    release = _Release()
    with pytest.raises(RuntimeError, match="can't start new thread"):
        await _run(_mgr(tmp_path), compressor, release)
    assert release.count == 1

    fn, args, kwargs = executor.queued
    fn(*args, **kwargs)  # the queued item runs later on another thread

    assert compressor.calls == 0
    assert release.count == 1, "the late job released a pin it did not own"


# --- SQLitePendingStore.close ----------------------------------------------


class _ObservedLock:
    """The store's lock, reporting when a thread starts waiting for it."""

    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self.waiting = threading.Event()

    def __enter__(self) -> None:
        self.waiting.set()
        self._lock.acquire()

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


def test_close_waits_for_an_operation_in_progress(tmp_path):
    store = SQLitePendingStore(tmp_path / "pending.db")
    store.initialize()
    real = threading.Lock()
    observed = _ObservedLock(real)
    store._lock = observed  # type: ignore[assignment]
    closed = threading.Event()
    real.acquire()
    try:
        closer = threading.Thread(target=lambda: (store.close(), closed.set()))
        closer.start()
        assert observed.waiting.wait(5), "close() never asked for the store lock"
        assert not closed.wait(0.2), "close() did not wait for the lock holder"
        assert store._db is not None
    finally:
        real.release()
    assert closed.wait(5)
    closer.join(5)
    assert store._db is None
