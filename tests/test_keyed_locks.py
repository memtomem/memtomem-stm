"""``KeyedLocks`` — the refcounted per-key lock behind both stampede guards.

The shape it replaces (``setdefault`` + ``pop`` in ``finally``) serializes
every caller that registered before the first pop, however many those are.
What it gets wrong is removing the entry while waiters are still queued: an
arrival after that removal installs a fresh lock under the same key and can
run alongside the queued waiter (#878).

Ordering here never rests on how many ``sleep(0)`` yields it takes a task to
reach the registry — asyncio makes no such promise. Each ordering-sensitive
test waits on ``total_refs()`` or on an event the coroutine under test sets.
Each scenario runs under one ``asyncio.timeout``, so every wait in it —
including the plain ``await`` on an event or a task — is bounded and a liveness
regression reports instead of hanging the suite. That bound is cancellation
based, so it can still be defeated by code that swallows ``CancelledError``.
"""

from __future__ import annotations

import asyncio

import pytest

from memtomem_stm.utils.keyed_locks import KeyedLocks

pytestmark = pytest.mark.asyncio

# Wall-clock ceiling for a whole scenario. Sized to tolerate normal CI load
# while still being small enough that a deadlock reports instead of hanging the
# suite — no finite bound can rule out a false trip on a badly overloaded
# runner.
_SCENARIO_TIMEOUT = 5.0


async def _wait_until(predicate, what: str, *, timeout: float = 2.0) -> None:
    """Yield to the loop until ``predicate()`` holds, then return.

    Bounded by wall clock rather than by a yield count: a yield budget is a
    guess about scheduler fairness, which is the very thing these tests must
    not assume. Raises ``AssertionError`` rather than looping forever, so a
    test waiting on something that will never happen says so.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0)


class TestKeyedLocks:
    async def test_third_arrival_after_a_release_cannot_run_beside_a_waiter(self):
        """A, B, C on one key, as a three-call handoff rather than three
        simultaneous arrivals: C is started only AFTER A has released — the
        exact window in which the pop-in-finally shape hands C a brand-new
        lock while B is still queued on the old one. Concurrency inside the
        critical section must stay at 1.

        Ordering comes from events and from ``total_refs()`` reaching the
        expected count — never from assuming a fixed number of scheduler
        yields is enough to get a task queued."""
        async with asyncio.timeout(_SCENARIO_TIMEOUT):
            locks = KeyedLocks()
            inside = 0
            max_inside = 0
            entered: dict[str, asyncio.Event] = {n: asyncio.Event() for n in "ABC"}
            release: dict[str, asyncio.Event] = {n: asyncio.Event() for n in "ABC"}

            async def worker(name: str) -> None:
                nonlocal inside, max_inside
                async with locks.hold("k"):
                    inside += 1
                    max_inside = max(max_inside, inside)
                    entered[name].set()
                    await release[name].wait()
                    inside -= 1

            a = asyncio.create_task(worker("A"))
            await entered["A"].wait()
            b = asyncio.create_task(worker("B"))
            # B must be QUEUED before A releases — that is the precondition the
            # whole scenario rests on, so wait for the reference rather than
            # assuming one yield was enough.
            await _wait_until(lambda: locks.total_refs() == 2, "B to queue on the key")
            release["A"].set()
            await a

            # A has released, which already WOKE B: B is either scheduled to
            # resume or already inside the critical section — never still
            # queued. Either way it still REFERENCES the entry, and that
            # reference must have survived A's departure. This is the defect
            # stated directly: the rejected shape drops the whole entry here,
            # orphaning B onto a lock nobody else can find.
            assert locks.total_refs() == 1, (
                "the former waiter's reference did not survive the first caller's "
                "release — its entry was removed from the registry"
            )

            # A is gone; B holds or is about to. C arrives now.
            c = asyncio.create_task(worker("C"))
            # ``total_refs() == 2`` already proves C ran PAST the registration
            # point inside ``hold()``. Had the lock been free it would have
            # acquired and set its event without suspending in between, so the
            # event still being clear is proof C is queued — no extra "let
            # things settle" yields needed, and none would be sound anyway
            # (see ``_wait_until``).
            await _wait_until(lambda: locks.total_refs() == 2, "C to register on the key")
            assert not entered["C"].is_set(), (
                "C entered the critical section while B held/awaited the key — "
                "C was handed a second lock for the same key"
            )
            release["B"].set()
            release["C"].set()
            await asyncio.gather(b, c)

            assert max_inside == 1, f"{max_inside} coroutines ran concurrently for one key"
            assert len(locks) == 0, "entry leaked after every holder finished"

    async def test_cancelled_waiter_releases_only_its_own_reference(self):
        """A cancelled waiter must drop its own reference and NOT the entry
        the still-active holder is using.

        The post-hoc ``len == 0`` check alone is satisfied by the rejected
        design too (B's cancellation pops A's live entry, then A's own pop is
        a no-op), so the load-bearing assertions are the ones taken while A
        still holds: the entry survives B's cancellation, and a caller
        arriving afterwards is still excluded."""
        async with asyncio.timeout(_SCENARIO_TIMEOUT):
            locks = KeyedLocks()
            entered = asyncio.Event()
            release = asyncio.Event()
            c_entered = asyncio.Event()

            async def holder() -> None:
                async with locks.hold("k"):
                    entered.set()
                    await release.wait()

            async def waiter() -> None:
                async with locks.hold("k"):
                    pass

            async def late() -> None:
                async with locks.hold("k"):
                    c_entered.set()

            h = asyncio.create_task(holder())
            await entered.wait()
            w = asyncio.create_task(waiter())
            # Cancelling before the waiter registers would make the assertions
            # below vacuous, so pin that it actually took a reference first.
            await _wait_until(lambda: locks.total_refs() == 2, "the waiter to queue on the key")
            w.cancel()
            # Deliberately NOT ``pytest.raises(CancelledError): await w``. The
            # scenario timeout cancels *this* task, and that cancellation would
            # propagate into the await and be swallowed by the raises block —
            # the timeout would then exit normally and every wait after it
            # would be unbounded again, silently. Poll for completion under the
            # bounded helper instead and assert the outcome directly.
            await _wait_until(lambda: w.done(), "the cancelled waiter to finish")
            assert w.cancelled(), "the waiter did not end in cancellation"

            # The holder still holds: the cancellation must have dropped only the
            # waiter's own reference, not the entry the holder is using.
            assert locks.total_refs() == 1, "a cancelled waiter dropped more than its own reference"
            assert len(locks) == 1, "a cancelled waiter took the active holder's entry"

            c = asyncio.create_task(late())
            # As above: the reference count proves the late caller got past
            # registration, and it would have set its event without suspending
            # had the lock been free.
            await _wait_until(lambda: locks.total_refs() == 2, "the late caller to register")
            assert not c_entered.is_set(), (
                "a caller arriving after the cancellation entered while the "
                "original holder was still inside — it was handed a second lock"
            )

            release.set()
            await asyncio.gather(h, c)

            assert len(locks) == 0, "an entry leaked after every reference finished"
            assert locks.total_refs() == 0, "a reference leaked after every holder finished"

    async def test_exception_in_body_releases_and_removes(self):
        async with asyncio.timeout(_SCENARIO_TIMEOUT):
            locks = KeyedLocks()

            with pytest.raises(ValueError):
                async with locks.hold("k"):
                    raise ValueError("boom")

            assert len(locks) == 0
            # Not left locked: a subsequent hold must not block.
            await self._hold_once(locks, "k")

    @staticmethod
    async def _hold_once(locks: KeyedLocks, key: str) -> None:
        async with locks.hold(key):
            pass

    async def test_distinct_keys_do_not_serialize(self):
        async with asyncio.timeout(_SCENARIO_TIMEOUT):
            locks = KeyedLocks()
            inside = 0
            max_inside = 0
            release = asyncio.Event()

            async def worker(key: str) -> None:
                nonlocal inside, max_inside
                async with locks.hold(key):
                    inside += 1
                    max_inside = max(max_inside, inside)
                    await release.wait()
                    inside -= 1

            t1 = asyncio.create_task(worker("a"))
            t2 = asyncio.create_task(worker("b"))
            await _wait_until(lambda: locks.total_refs() == 2, "both keys to register")
            assert len(locks) == 2
            release.set()
            await asyncio.gather(t1, t2)
            assert max_inside == 2, "distinct keys were serialized"
            assert len(locks) == 0
