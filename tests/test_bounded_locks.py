"""Tests for the ``bounded_lock`` helper (#208).

Covers:
- Timeout fires with diagnostic log when a holder keeps the lock past budget.
- ``LockTimeoutError`` is a subclass of ``asyncio.TimeoutError`` so existing
  generic timeout handlers still catch it.
- Normal uncontested/contested-but-fast paths acquire + release cleanly.
- The lock is released on normal exit and on exception inside the block.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from memtomem_stm.proxy._locks import LockTimeoutError, bounded_lock


class TestBoundedLock:
    @pytest.mark.asyncio
    async def test_uncontested_acquisition_is_transparent(self) -> None:
        lock = asyncio.Lock()
        async with bounded_lock(lock, timeout=1.0, name="test_lock"):
            assert lock.locked()
        assert not lock.locked()

    @pytest.mark.asyncio
    async def test_contended_but_fast_acquisition_succeeds(self) -> None:
        lock = asyncio.Lock()

        async def _hold_briefly() -> None:
            async with bounded_lock(lock, timeout=1.0, name="test_lock"):
                await asyncio.sleep(0.02)

        async def _wait_and_acquire() -> None:
            await asyncio.sleep(0.005)
            async with bounded_lock(lock, timeout=1.0, name="test_lock"):
                assert lock.locked()

        await asyncio.gather(_hold_briefly(), _wait_and_acquire())
        assert not lock.locked()

    @pytest.mark.asyncio
    async def test_timeout_raises_lock_timeout_error(self) -> None:
        lock = asyncio.Lock()
        holder_released = asyncio.Event()

        async def _hold_indefinitely() -> None:
            async with lock:
                await holder_released.wait()

        holder = asyncio.create_task(_hold_indefinitely())
        try:
            await asyncio.sleep(0.01)  # let holder acquire first
            with pytest.raises(LockTimeoutError) as exc_info:
                async with bounded_lock(lock, timeout=0.05, name="stuck_lock"):
                    pytest.fail("should not reach block body on timeout")
            assert exc_info.value.lock_name == "stuck_lock"
            assert exc_info.value.timeout_seconds == 0.05
            # subclass of asyncio.TimeoutError so generic handlers still match
            assert isinstance(exc_info.value, asyncio.TimeoutError)
        finally:
            holder_released.set()
            await holder

    @pytest.mark.asyncio
    async def test_timeout_emits_error_log_with_task_name(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        lock = asyncio.Lock()
        holder_released = asyncio.Event()

        async def _hold_indefinitely() -> None:
            async with lock:
                await holder_released.wait()

        holder = asyncio.create_task(_hold_indefinitely(), name="sleepy_holder")
        waiter_name = "eager_waiter"
        try:
            await asyncio.sleep(0.01)
            caplog.clear()
            with caplog.at_level(logging.ERROR, logger="memtomem_stm.proxy._locks"):

                async def _attempt() -> None:
                    with pytest.raises(LockTimeoutError):
                        async with bounded_lock(lock, timeout=0.05, name="stuck_lock"):
                            pass

                waiter = asyncio.create_task(_attempt(), name=waiter_name)
                await waiter
            error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
            assert any("stuck_lock" in r.getMessage() for r in error_records)
            assert any(waiter_name in r.getMessage() for r in error_records)
        finally:
            holder_released.set()
            await holder

    @pytest.mark.asyncio
    async def test_lock_released_when_block_raises(self) -> None:
        lock = asyncio.Lock()

        class _Boom(RuntimeError):
            pass

        with pytest.raises(_Boom):
            async with bounded_lock(lock, timeout=1.0, name="test_lock"):
                raise _Boom

        assert not lock.locked()
        # Next acquire should not block
        async with bounded_lock(lock, timeout=0.5, name="test_lock"):
            assert lock.locked()
        assert not lock.locked()

    @pytest.mark.asyncio
    async def test_lock_not_leaked_on_timeout(self) -> None:
        """If the first waiter times out, the original holder's eventual
        release must still leave the lock acquirable by a later waiter —
        i.e. the timeout path must not ``release()`` a lock it never held."""
        lock = asyncio.Lock()
        holder_released = asyncio.Event()

        async def _hold_then_release() -> None:
            async with lock:
                await holder_released.wait()

        holder = asyncio.create_task(_hold_then_release())
        try:
            await asyncio.sleep(0.01)
            with pytest.raises(LockTimeoutError):
                async with bounded_lock(lock, timeout=0.05, name="stuck_lock"):
                    pass
        finally:
            holder_released.set()
            await holder

        # Lock must now be acquirable: previous timeout must not have
        # release()'d a lock it never owned (which would un-balance the
        # reference count and could mask a future bug).
        async with bounded_lock(lock, timeout=0.5, name="stuck_lock"):
            assert lock.locked()
        assert not lock.locked()
