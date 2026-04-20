"""Tests for ``ProgressiveReadsStore`` and ``ProgressiveReadsTracker``.

Exercises schema/aggregation invariants without touching the proxy
manager — manager-level hot-path integration lives in
``test_progressive.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem_stm.proxy.progressive_reads import ProgressiveReadsTracker
from memtomem_stm.proxy.progressive_reads_store import ProgressiveReadsStore


# ---------------------------------------------------------------------------
# ProgressiveReadsStore
# ---------------------------------------------------------------------------


class TestProgressiveReadsStore:
    def test_empty_stats(self, tmp_path: Path):
        store = ProgressiveReadsStore(tmp_path / "pr.db")
        store.initialize()
        try:
            stats = store.get_stats()
            assert stats == {
                "total_reads": 0,
                "total_responses": 0,
                "follow_up_rate": 0.0,
                "avg_chars_served": 0.0,
                "avg_total_chars": 0.0,
                "avg_coverage": 0.0,
                "by_tool": {},
            }
        finally:
            store.close()

    def test_single_initial_row(self, tmp_path: Path):
        store = ProgressiveReadsStore(tmp_path / "pr.db")
        store.initialize()
        try:
            store.record(
                key="k1",
                trace_id="t1",
                server="docfix",
                tool="get_document",
                offset=0,
                chars=4000,
                served_to=4000,
                total_chars=10000,
            )
            stats = store.get_stats()
            assert stats["total_reads"] == 1
            assert stats["total_responses"] == 1
            assert stats["follow_up_rate"] == 0.0
            assert stats["avg_chars_served"] == 4000.0
            assert stats["avg_total_chars"] == 10000.0
            assert stats["avg_coverage"] == pytest.approx(0.4)
            assert stats["by_tool"] == {"get_document": {"responses": 1, "follow_up_rate": 0.0}}
        finally:
            store.close()

    def test_full_follow_up_rate(self, tmp_path: Path):
        """Initial + 2 follow-ups on the same key → follow_up_rate 1.0."""
        store = ProgressiveReadsStore(tmp_path / "pr.db")
        store.initialize()
        try:
            store.record("k1", "t1", "docfix", "tool_a", 0, 4000, 4000, 10000)
            store.record("k1", "t1", "docfix", "tool_a", 4000, 4000, 8000, 10000)
            store.record("k1", "t1", "docfix", "tool_a", 8000, 2000, 10000, 10000)
            stats = store.get_stats()
            assert stats["total_reads"] == 3
            assert stats["total_responses"] == 1
            assert stats["follow_up_rate"] == 1.0
            assert stats["avg_chars_served"] == 10000.0
            assert stats["avg_coverage"] == pytest.approx(1.0)
        finally:
            store.close()

    def test_mixed_follow_up_rate(self, tmp_path: Path):
        """Two responses: one multi-read, one single-read → rate 0.5."""
        store = ProgressiveReadsStore(tmp_path / "pr.db")
        store.initialize()
        try:
            # k1: full coverage across 2 reads
            store.record("k1", "t1", "s", "tool_a", 0, 4000, 4000, 8000)
            store.record("k1", "t1", "s", "tool_a", 4000, 4000, 8000, 8000)
            # k2: initial only, no follow-up
            store.record("k2", "t2", "s", "tool_b", 0, 4000, 4000, 12000)

            stats = store.get_stats()
            assert stats["total_reads"] == 3
            assert stats["total_responses"] == 2
            assert stats["follow_up_rate"] == pytest.approx(0.5)
            # avg of (8000, 4000) = 6000
            assert stats["avg_chars_served"] == pytest.approx(6000.0)
            assert stats["avg_total_chars"] == pytest.approx(10000.0)
            # avg of (1.0, 4000/12000) = (1.0 + 1/3) / 2
            assert stats["avg_coverage"] == pytest.approx((1.0 + 1 / 3) / 2)
            assert stats["by_tool"] == {
                "tool_a": {"responses": 1, "follow_up_rate": 1.0},
                "tool_b": {"responses": 1, "follow_up_rate": 0.0},
            }
        finally:
            store.close()

    def test_tool_filter_empties_by_tool(self, tmp_path: Path):
        store = ProgressiveReadsStore(tmp_path / "pr.db")
        store.initialize()
        try:
            store.record("k1", None, "s", "tool_a", 0, 1000, 1000, 5000)
            store.record("k2", None, "s", "tool_b", 0, 1000, 1000, 5000)
            stats = store.get_stats(tool="tool_a")
            assert stats["total_reads"] == 1
            assert stats["total_responses"] == 1
            assert stats["by_tool"] == {}
        finally:
            store.close()

    def test_coverage_capped_at_one(self, tmp_path: Path):
        """Defensive: served_to > total_chars should cap coverage at 1.0."""
        store = ProgressiveReadsStore(tmp_path / "pr.db")
        store.initialize()
        try:
            store.record("k1", None, "s", "t", 0, 10000, 10000, 8000)
            stats = store.get_stats()
            assert stats["avg_coverage"] == pytest.approx(1.0)
        finally:
            store.close()

    def test_record_after_close_is_noop(self, tmp_path: Path):
        store = ProgressiveReadsStore(tmp_path / "pr.db")
        store.initialize()
        store.close()
        # No exception — telemetry must never raise into the hot path
        store.record("k1", None, "s", "t", 0, 10, 10, 10)

    def test_get_stats_on_closed_store_returns_empty(self, tmp_path: Path):
        store = ProgressiveReadsStore(tmp_path / "pr.db")
        store.initialize()
        store.close()
        stats = store.get_stats()
        assert stats["total_reads"] == 0
        assert stats["by_tool"] == {}

    def test_close_is_idempotent(self, tmp_path: Path):
        store = ProgressiveReadsStore(tmp_path / "pr.db")
        store.initialize()
        store.close()
        store.close()  # second close must not raise

    def test_trace_id_nullable(self, tmp_path: Path):
        store = ProgressiveReadsStore(tmp_path / "pr.db")
        store.initialize()
        try:
            store.record("k1", None, "s", "t", 0, 100, 100, 500)
            stats = store.get_stats()
            assert stats["total_reads"] == 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# ProgressiveReadsTracker
# ---------------------------------------------------------------------------


class TestProgressiveReadsTracker:
    def test_record_initial_stores_offset_zero(self, tmp_path: Path):
        tracker = ProgressiveReadsTracker(tmp_path / "pr.db")
        try:
            tracker.record_initial(
                key="k1",
                trace_id="t1",
                server="docfix",
                tool="search",
                initial_chars=2500,
                total_chars=8000,
            )
            stats = tracker.get_stats()
            assert stats["total_reads"] == 1
            assert stats["avg_chars_served"] == 2500.0
            assert stats["avg_total_chars"] == 8000.0
        finally:
            tracker.close()

    def test_record_follow_up_computes_served_to(self, tmp_path: Path):
        tracker = ProgressiveReadsTracker(tmp_path / "pr.db")
        try:
            tracker.record_initial(
                key="k1",
                trace_id="t1",
                server="s",
                tool="t",
                initial_chars=4000,
                total_chars=10000,
            )
            tracker.record_follow_up(
                key="k1",
                trace_id="t1",
                server="s",
                tool="t",
                offset=4000,
                chars=4000,
                total_chars=10000,
            )
            stats = tracker.get_stats()
            # Last served_to on key k1 = 4000 + 4000 = 8000
            assert stats["avg_chars_served"] == 8000.0
            assert stats["follow_up_rate"] == 1.0
        finally:
            tracker.close()

    def test_record_after_close_is_swallowed(self, tmp_path: Path):
        tracker = ProgressiveReadsTracker(tmp_path / "pr.db")
        tracker.close()
        # Must not raise — telemetry failure cannot break the hot path
        tracker.record_initial(
            key="k1",
            trace_id=None,
            server="s",
            tool="t",
            initial_chars=100,
            total_chars=500,
        )
        tracker.record_follow_up(
            key="k1",
            trace_id=None,
            server="s",
            tool="t",
            offset=100,
            chars=100,
            total_chars=500,
        )

    def test_path_expansion(self, tmp_path: Path, monkeypatch):
        """``~`` in db_path should be expanded to ``$HOME``."""
        monkeypatch.setenv("HOME", str(tmp_path))
        tracker = ProgressiveReadsTracker(Path("~/pr_expand.db"))
        try:
            assert (tmp_path / "pr_expand.db").exists()
        finally:
            tracker.close()
