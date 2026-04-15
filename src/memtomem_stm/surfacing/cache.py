"""Surfacing result cache — avoid redundant LTM searches."""

from __future__ import annotations

import hashlib
import time
from typing import Any


class SurfacingCache:
    """In-memory TTL cache for surfacing results.

    Keyed by query hash. Avoids repeated LTM searches when the same
    tool is called with similar arguments in quick succession.

    Eviction is insertion-ordered FIFO (matching ``InMemoryPendingStore``):
    overflow drops the first-inserted entry in O(1). Expiry is handled
    lazily on ``get()``.
    """

    def __init__(self, ttl: float = 60.0, max_entries: int = 200) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._cache: dict[str, tuple[float, list[Any]]] = {}

    def get(self, query: str) -> list[Any] | None:
        key = self._hash(query)
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, results = entry
        if time.monotonic() - ts > self._ttl:
            del self._cache[key]
            return None
        return results

    def set(self, query: str, results: list[Any]) -> None:
        key = self._hash(query)
        # Re-insert to move an existing key to the tail (preserves FIFO order).
        if key in self._cache:
            del self._cache[key]
        while len(self._cache) >= self._max_entries:
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
        self._cache[key] = (time.monotonic(), results)

    def clear(self) -> None:
        self._cache.clear()

    @staticmethod
    def _hash(query: str) -> str:
        return hashlib.md5(query.encode()).hexdigest()
