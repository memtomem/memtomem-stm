"""Tests for TokenTracker memory bounds (issue #70).

Long-lived daemons and multi-tenant gateways can see churn in upstream
server/tool names. The per-key aggregation maps must not grow without bound.
"""

from __future__ import annotations

from memtomem_stm.proxy.metrics import (
    MAX_TRACKED_KEYS,
    CallMetrics,
    ErrorCategory,
    TokenTracker,
    _BoundedCounterDict,
)


class TestBoundedCounterDict:
    def test_lazy_insertion_with_factory(self):
        d = _BoundedCounterDict(lambda: {"calls": 0}, max_size=5)
        d["a"]["calls"] += 1
        assert d["a"]["calls"] == 1

    def test_evicts_oldest_past_max_size(self):
        d = _BoundedCounterDict(int, max_size=3)
        for key in ("a", "b", "c", "d"):
            d[key] = 1
        assert len(d) == 3
        assert "a" not in d  # oldest inserted → evicted
        assert {"b", "c", "d"} == set(iter(d))

    def test_getitem_is_a_touch(self):
        """Re-accessing a key should protect it from eviction (LRU)."""
        d = _BoundedCounterDict(int, max_size=3)
        d["a"] = 1
        d["b"] = 1
        d["c"] = 1
        _ = d["a"]  # touch a → most recently used
        d["d"] = 1  # evicts the LRU, which is now 'b'
        assert "a" in d
        assert "b" not in d
        assert "c" in d
        assert "d" in d

    def test_setitem_is_a_touch(self):
        d = _BoundedCounterDict(int, max_size=3)
        d["a"] = 1
        d["b"] = 1
        d["c"] = 1
        d["a"] = 2  # touch a
        d["d"] = 1  # evicts 'b'
        assert "a" in d and d["a"] == 2
        assert "b" not in d

    def test_increment_pattern_preserves_counts_until_eviction(self):
        """Typical `d[k] += 1` pattern must work across repeated updates."""
        d = _BoundedCounterDict(int, max_size=10)
        for _ in range(5):
            d["server"] += 1
        assert d["server"] == 5

    def test_eviction_happens_inside_getitem_for_new_key(self):
        """Creating a new key via __getitem__ must also respect max_size."""
        d = _BoundedCounterDict(lambda: {"n": 0}, max_size=2)
        d["a"]["n"] = 1
        d["b"]["n"] = 2
        d["c"]["n"] = 3  # triggers __getitem__ path with new key
        assert len(d) == 2
        assert "a" not in d


class TestTokenTrackerBounds:
    def _metrics(self, server: str, tool: str = "t") -> CallMetrics:
        return CallMetrics(
            server=server, tool=tool, original_chars=10, compressed_chars=5
        )

    def test_by_server_respects_max_tracked_keys(self):
        tracker = TokenTracker(max_tracked_keys=5)
        for i in range(20):
            tracker.record(self._metrics(f"srv{i}"))
        summary = tracker.get_summary()
        # Totals aggregate every call, per-server map is bounded.
        assert summary["total_calls"] == 20
        assert len(summary["by_server"]) == 5

    def test_by_tool_respects_max_tracked_keys(self):
        tracker = TokenTracker(max_tracked_keys=5)
        for i in range(20):
            tracker.record(self._metrics("srv", tool=f"tool{i}"))
        # by_tool is internal; verify via attribute access for this test.
        assert len(tracker._by_tool) == 5
        # Aggregate totals remain accurate even after eviction.
        assert tracker.get_summary()["total_calls"] == 20

    def test_errors_by_server_respects_max_tracked_keys(self):
        tracker = TokenTracker(max_tracked_keys=5)
        for i in range(20):
            m = CallMetrics(
                server=f"srv{i}",
                tool="t",
                original_chars=0,
                compressed_chars=0,
                is_error=True,
                error_category=ErrorCategory.PROTOCOL,
            )
            tracker.record_error(m)
        assert len(tracker._errors_by_server) == 5
        assert tracker.get_summary()["total_errors"] == 20

    def test_default_max_is_10k(self):
        assert MAX_TRACKED_KEYS == 10_000
        tracker = TokenTracker()  # default cap
        assert tracker._by_server._max_size == 10_000
        assert tracker._by_tool._max_size == 10_000
        assert tracker._errors_by_server._max_size == 10_000

    def test_existing_server_counts_accumulate(self):
        """LRU does not reset counts for keys that stay resident."""
        tracker = TokenTracker(max_tracked_keys=100)
        for _ in range(3):
            tracker.record(self._metrics("stable"))
        summary = tracker.get_summary()
        assert summary["by_server"]["stable"]["calls"] == 3
        assert summary["by_server"]["stable"]["original_chars"] == 30
