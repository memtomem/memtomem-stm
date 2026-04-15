"""Tests for SurfacingCache — in-memory TTL cache for LTM search results."""

from __future__ import annotations

import time

from memtomem_stm.surfacing.cache import SurfacingCache


class TestSurfacingCacheBasic:
    def test_set_then_get(self):
        cache = SurfacingCache(ttl=60.0)
        cache.set("query1", ["result1", "result2"])
        assert cache.get("query1") == ["result1", "result2"]

    def test_cache_miss(self):
        cache = SurfacingCache(ttl=60.0)
        assert cache.get("nonexistent") is None

    def test_empty_list_cached(self):
        """Empty list should be cached (represents 'no results found')."""
        cache = SurfacingCache(ttl=60.0)
        cache.set("query", [])
        result = cache.get("query")
        assert result is not None
        assert result == []


class TestSurfacingCacheTTL:
    def test_expired_entry_returns_none(self):
        cache = SurfacingCache(ttl=0.01)
        cache.set("query", ["result"])
        time.sleep(0.02)
        assert cache.get("query") is None


class TestSurfacingCacheEviction:
    def test_max_entries_eviction(self):
        cache = SurfacingCache(ttl=60.0, max_entries=3)
        for i in range(5):
            cache.set(f"query_{i}", [f"result_{i}"])
        # Should have evicted oldest entries
        assert len(cache._cache) <= 3

    def test_fifo_eviction_drops_first_inserted(self):
        """Overflow drops the first-inserted entry, not an arbitrary one."""
        cache = SurfacingCache(ttl=60.0, max_entries=3)
        cache.set("q1", ["r1"])
        cache.set("q2", ["r2"])
        cache.set("q3", ["r3"])
        cache.set("q4", ["r4"])  # evicts q1
        assert cache.get("q1") is None
        assert cache.get("q2") == ["r2"]
        assert cache.get("q3") == ["r3"]
        assert cache.get("q4") == ["r4"]

    def test_reinsert_refreshes_order(self):
        """Setting an existing key moves it to the tail (youngest position)."""
        cache = SurfacingCache(ttl=60.0, max_entries=3)
        cache.set("q1", ["r1"])
        cache.set("q2", ["r2"])
        cache.set("q3", ["r3"])
        cache.set("q1", ["r1_new"])  # re-insert moves q1 to tail
        cache.set("q4", ["r4"])  # evicts q2 (now the oldest), not q1
        assert cache.get("q1") == ["r1_new"]
        assert cache.get("q2") is None
        assert cache.get("q3") == ["r3"]
        assert cache.get("q4") == ["r4"]

    def test_expired_entries_evicted_lazily_on_get(self):
        """Expired entries are removed on get() rather than scanned on set()."""
        cache = SurfacingCache(ttl=0.01, max_entries=3)
        cache.set("old1", ["r1"])
        time.sleep(0.02)
        # Lazy expiry: get() returns None and deletes the entry in place
        assert cache.get("old1") is None
        assert "old1" not in {k for k in cache._cache}


class TestSurfacingCacheClear:
    def test_clear_removes_all(self):
        cache = SurfacingCache(ttl=60.0)
        cache.set("q1", ["r1"])
        cache.set("q2", ["r2"])
        cache.clear()
        assert cache.get("q1") is None
        assert cache.get("q2") is None


class TestSurfacingCacheHashDeterminism:
    def test_same_query_same_hash(self):
        assert SurfacingCache._hash("hello") == SurfacingCache._hash("hello")

    def test_different_query_different_hash(self):
        assert SurfacingCache._hash("hello") != SurfacingCache._hash("world")
