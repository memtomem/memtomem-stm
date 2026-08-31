"""Refcounted per-key ``asyncio.Lock`` registry.

The naive shape — ``setdefault`` a lock, ``pop`` it in ``finally`` — serializes
every caller that registered *before* the first pop, however many those are.
What it gets wrong is removing the entry while waiters are still queued on that
lock: the next arrival then installs a **fresh** lock under the same key, and
can therefore run concurrently with a queued waiter — whether they actually
overlap depends on the critical sections yielding. So mutual exclusion on the
key silently degrades once callers span a non-final release (#878).

Here the entry lives as long as any holder *or* waiter references it, and the
last releaser removes it.  Every dict/refcount mutation happens without an
intervening ``await``, so under asyncio's cooperative scheduling the
acquire-or-create and the release-and-maybe-remove are each atomic.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class _Entry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Holders + waiters currently referencing this entry. The entry is removed
    # when this reaches zero, which is exactly when no coroutine can still be
    # queued on ``lock`` — the condition the pop-in-finally shape got wrong.
    refs: int = 0


class KeyedLocks:
    """Serialize work per key, with entries bounded by the in-flight key set."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def __len__(self) -> int:
        """Live entries — i.e. keys with at least one holder or waiter."""
        return len(self._entries)

    def total_refs(self) -> int:
        """Live references (holders + waiters) summed over every key.

        Exposed so a caller can observe that a coroutine has actually
        REGISTERED — reached the refcount claim inside :meth:`hold` — instead
        of inferring it from scheduler yields, which asyncio does not promise.

        It says nothing on its own about *queuing*: the sum spans keys, and a
        reference belongs to a holder just as much as to a waiter. Only with a
        known active holder on the same key does an added reference mean
        someone is waiting behind it.
        """
        return sum(entry.refs for entry in self._entries.values())

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        """Hold ``key``'s lock for the duration of the ``async with`` body."""
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry()
            self._entries[key] = entry
        # Claimed BEFORE the first await so a releaser that runs while we are
        # queued cannot drop the entry we are about to wait on.
        entry.refs += 1
        try:
            async with entry.lock:
                yield
        finally:
            # Reached by a cancelled waiter too (the ``async with`` raises
            # before the body runs), so a cancellation cannot leak an entry.
            entry.refs -= 1
            # Identity check: never let a stale entry delete a successor that
            # some other coroutine has already installed under this key.
            if entry.refs <= 0 and self._entries.get(key) is entry:
                del self._entries[key]
