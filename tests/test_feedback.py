"""Tests for FeedbackStore, FeedbackTracker, and AutoTuner."""

from __future__ import annotations

import time
from pathlib import Path

from memtomem_stm.proxy.compression_feedback_store import CompressionFeedbackStore
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.feedback import AutoTuner, FeedbackTracker
from memtomem_stm.surfacing.feedback_store import FeedbackStore


# ---------------------------------------------------------------------------
# FeedbackStore
# ---------------------------------------------------------------------------


class TestFeedbackStore:
    def test_record_and_retrieve_surfacing(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing(
            "surf1", "server", "tool", "query", ["mem1", "mem2"], [0.9, 0.8]
        )
        stats = feedback_store.get_tool_feedback_summary("tool")
        assert stats["total_surfacings"] == 1

    def test_record_feedback_valid_id(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing("surf1", "s", "t", "q", ["m1"], [0.9])
        ok = feedback_store.record_feedback("surf1", "helpful")
        assert ok is True

    def test_record_feedback_unknown_id(self, feedback_store: FeedbackStore):
        ok = feedback_store.record_feedback("nonexistent", "helpful")
        assert ok is False

    def test_feedback_summary_by_rating(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing("s1", "sv", "tool_a", "q", ["m1"], [0.5])
        feedback_store.record_feedback("s1", "helpful")
        feedback_store.record_feedback("s1", "helpful")
        feedback_store.record_feedback("s1", "not_relevant")

        stats = feedback_store.get_tool_feedback_summary("tool_a")
        assert stats["total_feedback"] == 3
        assert stats["by_rating"]["helpful"] == 2
        assert stats["by_rating"]["not_relevant"] == 1

    def test_feedback_summary_all_tools(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing("s1", "sv", "t1", "q", ["m1"], [0.5])
        feedback_store.record_surfacing("s2", "sv", "t2", "q", ["m2"], [0.5])
        feedback_store.record_feedback("s1", "helpful")
        feedback_store.record_feedback("s2", "not_relevant")

        stats = feedback_store.get_tool_feedback_summary()
        assert stats["total_surfacings"] == 2
        assert stats["total_feedback"] == 2

    def test_not_relevant_ratio_insufficient_samples(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing("s1", "sv", "t", "q", ["m1"], [0.5])
        feedback_store.record_feedback("s1", "helpful")
        ratio = feedback_store.get_tool_not_relevant_ratio("t", min_samples=20)
        assert ratio is None

    def test_get_stats_empty_db(self, feedback_store: FeedbackStore):
        stats = feedback_store.get_stats()
        assert stats["events_total"] == 0
        assert stats["distinct_tools"] == 0
        assert stats["date_range"] == {"first": None, "last": None}
        assert stats["per_tool_breakdown"] == []
        assert stats["rating_distribution"] == {}
        assert stats["total_feedback"] == 0
        assert stats["recent"] == []

    def test_get_stats_multi_tool_breakdown(self, feedback_store: FeedbackStore):
        # tool_a: 2 events (2 + 3 memories) → avg 2.5
        feedback_store.record_surfacing("s1", "srv", "tool_a", "q1", ["m1", "m2"], [0.9, 0.8])
        feedback_store.record_surfacing(
            "s2", "srv", "tool_a", "q2", ["m3", "m4", "m5"], [0.7, 0.6, 0.5]
        )
        # tool_b: 1 event (1 memory) → avg 1.0
        feedback_store.record_surfacing("s3", "srv", "tool_b", "q3", ["m6"], [0.4])

        feedback_store.record_feedback("s1", "helpful")
        feedback_store.record_feedback("s2", "not_relevant")
        feedback_store.record_feedback("s3", "already_known")

        stats = feedback_store.get_stats()
        assert stats["events_total"] == 3
        assert stats["distinct_tools"] == 2
        # Descending by event count.
        assert stats["per_tool_breakdown"][0] == {
            "tool": "tool_a",
            "events": 2,
            "avg_memory_count": 2.5,
            "feedback_count": 2,
            "not_relevant_count": 1,
        }
        assert stats["per_tool_breakdown"][1] == {
            "tool": "tool_b",
            "events": 1,
            "avg_memory_count": 1.0,
            "feedback_count": 1,
            "not_relevant_count": 0,
        }
        assert stats["rating_distribution"] == {
            "helpful": 1,
            "not_relevant": 1,
            "already_known": 1,
        }
        assert stats["total_feedback"] == 3
        assert stats["date_range"]["first"] is not None
        assert stats["date_range"]["last"] is not None

    def test_get_stats_since_filter(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing("s_old", "srv", "tool_a", "q_old", ["m1"], [0.9])
        # Backdate the "old" event to well before the since cutoff so it's
        # excluded. record_surfacing() stamps created_at = time.time(); this
        # patches the row directly.
        feedback_store._db.execute(  # type: ignore[union-attr]
            "UPDATE surfacing_events SET created_at = ? WHERE id = ?",
            (time.time() - 3600, "s_old"),
        )
        feedback_store._db.commit()  # type: ignore[union-attr]

        feedback_store.record_surfacing("s_new", "srv", "tool_a", "q_new", ["m2"], [0.9])

        cutoff = time.time() - 60
        stats = feedback_store.get_stats(since=cutoff)
        assert stats["events_total"] == 1
        # Old event excluded; only s_new in recent.
        assert stats["recent"][0]["query_preview"] == "q_new"

    def test_get_stats_recent_limit_and_preview(self, feedback_store: FeedbackStore):
        long_query = "x" * 200
        feedback_store.record_surfacing("s1", "srv", "t", long_query, ["m1"], [0.9])
        for i in range(2, 6):
            feedback_store.record_surfacing(f"s{i}", "srv", "t", f"q{i}", ["m"], [0.5])

        stats = feedback_store.get_stats(limit=3)
        assert len(stats["recent"]) == 3
        # Ordered DESC by created_at — the most recently written rows first.
        # s1 (long query) was first, so it should NOT be in the limit=3 tail.
        previews = [r["query_preview"] for r in stats["recent"]]
        assert all(p.startswith("q") for p in previews)

        # Now exercise the preview truncation by pulling all with high limit.
        stats_all = feedback_store.get_stats(limit=10)
        row_s1 = next(r for r in stats_all["recent"] if r["query_preview"].startswith("x"))
        assert row_s1["query_preview"].endswith("...")
        assert len(row_s1["query_preview"]) == 80

    def test_get_stats_recent_tied_created_at_breaks_by_insert_order(
        self, feedback_store: FeedbackStore
    ):
        for i in range(1, 6):
            feedback_store.record_surfacing(f"s{i}", "srv", "t", f"q{i}", ["m"], [0.5])
        # Force a tie on created_at — Windows time.time() resolution can collapse
        # rapid inserts onto identical timestamps; without a secondary sort key,
        # SQLite's tie-break is unspecified and "recent" becomes nondeterministic.
        feedback_store._db.executemany(  # type: ignore[union-attr]
            "UPDATE surfacing_events SET created_at = ? WHERE id = ?",
            [(1000.0, f"s{i}") for i in range(1, 6)],
        )
        feedback_store._db.commit()  # type: ignore[union-attr]

        stats = feedback_store.get_stats(limit=3)
        previews = [r["query_preview"] for r in stats["recent"]]
        assert previews == ["q5", "q4", "q3"]

    def test_get_stats_tool_filter_excludes_other_ratings(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing("s_a", "srv", "tool_a", "q", ["m1"], [0.9])
        feedback_store.record_surfacing("s_b", "srv", "tool_b", "q", ["m2"], [0.9])
        feedback_store.record_feedback("s_a", "helpful")
        feedback_store.record_feedback("s_b", "not_relevant")

        stats = feedback_store.get_stats(tool="tool_a")
        assert stats["events_total"] == 1
        assert stats["rating_distribution"] == {"helpful": 1}
        assert all(r["tool"] == "tool_a" for r in stats["recent"])

    def test_get_per_tool_feedback_counts_is_unfiltered(self, feedback_store: FeedbackStore):
        """Powers the auto-tune readiness gate in ``stm_surfacing_stats`` —
        must always return the full history, not honor any time window."""
        feedback_store.record_surfacing("s_old", "sv", "t1", "q", ["m1"], [0.9])
        feedback_store._db.execute(  # type: ignore[union-attr]
            "UPDATE surfacing_events SET created_at = ? WHERE id = ?",
            (time.time() - 7200, "s_old"),
        )
        feedback_store._db.commit()  # type: ignore[union-attr]
        feedback_store.record_feedback("s_old", "helpful")
        feedback_store.record_feedback("s_old", "not_relevant")

        feedback_store.record_surfacing("s_new", "sv", "t2", "q", ["m2"], [0.9])
        feedback_store.record_feedback("s_new", "helpful")

        counts = feedback_store.get_per_tool_feedback_counts()
        # Both tools present despite t1's feedback being well before "now".
        assert counts == {"t1": 2, "t2": 1}

    def test_get_per_tool_feedback_counts_empty(self, feedback_store: FeedbackStore):
        assert feedback_store.get_per_tool_feedback_counts() == {}

    def test_not_relevant_ratio_computed(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing("s1", "sv", "t", "q", ["m1"], [0.5])
        for i in range(20):
            feedback_store.record_feedback("s1", "not_relevant" if i < 12 else "helpful")

        ratio = feedback_store.get_tool_not_relevant_ratio("t", min_samples=20)
        assert ratio is not None
        assert abs(ratio - 0.6) < 0.01

    def test_per_tool_breakdown_includes_feedback_counts(self, feedback_store: FeedbackStore):
        """Per-tool breakdown exposes feedback_count + not_relevant_count."""
        feedback_store.record_surfacing("s_a1", "sv", "tool_a", "q", ["m1"], [0.9])
        feedback_store.record_surfacing("s_a2", "sv", "tool_a", "q", ["m2"], [0.9])
        feedback_store.record_surfacing("s_b", "sv", "tool_b", "q", ["m3"], [0.9])
        feedback_store.record_feedback("s_a1", "helpful")
        feedback_store.record_feedback("s_a2", "not_relevant")
        feedback_store.record_feedback("s_a2", "not_relevant")
        # tool_b has no feedback yet.

        stats = feedback_store.get_stats()
        by_tool = {row["tool"]: row for row in stats["per_tool_breakdown"]}
        assert by_tool["tool_a"]["feedback_count"] == 3
        assert by_tool["tool_a"]["not_relevant_count"] == 2
        assert by_tool["tool_b"]["feedback_count"] == 0
        assert by_tool["tool_b"]["not_relevant_count"] == 0

    def test_per_tool_feedback_honors_since_filter(self, feedback_store: FeedbackStore):
        """since= filters feedback rows via their parent event's created_at."""
        feedback_store.record_surfacing("s_old", "sv", "t", "q", ["m1"], [0.9])
        feedback_store._db.execute(  # type: ignore[union-attr]
            "UPDATE surfacing_events SET created_at = ? WHERE id = ?",
            (time.time() - 3600, "s_old"),
        )
        feedback_store._db.commit()  # type: ignore[union-attr]
        feedback_store.record_feedback("s_old", "not_relevant")

        feedback_store.record_surfacing("s_new", "sv", "t", "q", ["m2"], [0.9])
        feedback_store.record_feedback("s_new", "helpful")

        stats = feedback_store.get_stats(since=time.time() - 60)
        row = stats["per_tool_breakdown"][0]
        assert row["feedback_count"] == 1
        assert row["not_relevant_count"] == 0


class TestFeedbackStoreCoexistence:
    """Regression: surfacing tables coexist with compression tables on the same DB.

    Real ``~/.memtomem/stm_feedback.db`` files in the wild may have been
    initialized by ``CompressionFeedbackStore`` alone (compression_feedback +
    progressive_reads) before surfacing was ever enabled. When surfacing
    starts up against such a DB, ``FeedbackStore.initialize()`` must add its
    own tables without disturbing existing rows or schema.
    """

    def test_initialize_against_compression_only_db(self, tmp_path: Path):
        db_path = tmp_path / "shared.db"

        # Simulate the production state: compression store created first,
        # with a row already written.
        cstore = CompressionFeedbackStore(db_path)
        cstore.initialize()
        cstore.record(
            server="server-x",
            tool="tool-x",
            kind="missing_field",
            missing="x.y",
            trace_id="trace-1",
        )
        cstore.close()

        # FeedbackStore opens the same file later. Surfacing tables should
        # appear; compression rows must remain.
        fstore = FeedbackStore(db_path)
        fstore.initialize()
        try:
            tables = {
                r[0]
                for r in fstore._db.execute(  # type: ignore[union-attr]
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert {"surfacing_events", "surfacing_feedback", "seen_memories"} <= tables
            assert "compression_feedback" in tables

            preserved = fstore._db.execute(  # type: ignore[union-attr]
                "SELECT server, tool FROM compression_feedback"
            ).fetchall()
            assert preserved == [("server-x", "tool-x")]
        finally:
            fstore.close()

    def test_initialize_is_idempotent(self, tmp_path: Path):
        """Re-opening must be a no-op — no data loss, no schema errors."""
        db_path = tmp_path / "shared.db"
        fstore = FeedbackStore(db_path)
        fstore.initialize()
        fstore.record_surfacing("s1", "sv", "t", "q", ["m1"], [0.9])
        fstore.record_feedback("s1", "helpful")
        fstore.close()

        fstore2 = FeedbackStore(db_path)
        fstore2.initialize()  # second initialize on existing schema
        try:
            stats = fstore2.get_stats()
            assert stats["events_total"] == 1
            assert stats["rating_distribution"] == {"helpful": 1}
        finally:
            fstore2.close()


# ---------------------------------------------------------------------------
# FeedbackTracker
# ---------------------------------------------------------------------------


class TestFeedbackTracker:
    def test_invalid_rating_rejected(self, tmp_path: Path):
        tracker = FeedbackTracker(SurfacingConfig(), db_path=tmp_path / "fb.db")
        try:
            result = tracker.record_feedback("s1", "invalid_rating")
            assert "Error" in result
        finally:
            tracker.close()

    def test_valid_feedback_recorded(self, tmp_path: Path):
        tracker = FeedbackTracker(SurfacingConfig(), db_path=tmp_path / "fb.db")
        try:
            tracker.record_surfacing("s1", "sv", "t", "q", ["m1"], [0.5])
            result = tracker.record_feedback("s1", "helpful")
            assert "recorded" in result.lower()
        finally:
            tracker.close()

    def test_get_stats(self, tmp_path: Path):
        tracker = FeedbackTracker(SurfacingConfig(), db_path=tmp_path / "fb.db")
        try:
            stats = tracker.get_stats()
            # New rich shape mirroring stm_compression_stats.
            assert stats["events_total"] == 0
            assert stats["distinct_tools"] == 0
            assert stats["date_range"] == {"first": None, "last": None}
            assert stats["per_tool_breakdown"] == []
            assert stats["rating_distribution"] == {}
            assert stats["total_feedback"] == 0
            assert stats["recent"] == []
        finally:
            tracker.close()


# ---------------------------------------------------------------------------
# AutoTuner
# ---------------------------------------------------------------------------


class TestAutoTuner:
    def _make_tuner(self, feedback_store: FeedbackStore, min_score: float = 0.02) -> AutoTuner:
        cfg = SurfacingConfig(
            auto_tune_enabled=True,
            auto_tune_min_samples=5,
            auto_tune_score_increment=0.005,
            min_score=min_score,
        )
        return AutoTuner(cfg, feedback_store)

    def _seed_feedback(self, store: FeedbackStore, tool: str, not_relevant: int, helpful: int):
        store.record_surfacing("s1", "sv", tool, "q", ["m1"], [0.5])
        for _ in range(not_relevant):
            store.record_feedback("s1", "not_relevant")
        for _ in range(helpful):
            store.record_feedback("s1", "helpful")

    def test_high_not_relevant_raises_threshold(self, feedback_store: FeedbackStore):
        self._seed_feedback(feedback_store, "t", not_relevant=5, helpful=1)
        tuner = self._make_tuner(feedback_store)
        result = tuner.maybe_adjust("t")
        assert result is not None
        assert result > 0.02

    def test_low_not_relevant_lowers_threshold(self, feedback_store: FeedbackStore):
        self._seed_feedback(feedback_store, "t", not_relevant=1, helpful=10)
        tuner = self._make_tuner(feedback_store)
        result = tuner.maybe_adjust("t")
        assert result is not None
        assert result < 0.02

    def test_insufficient_samples_no_adjustment(self, feedback_store: FeedbackStore):
        store = feedback_store
        store.record_surfacing("s1", "sv", "t", "q", ["m1"], [0.5])
        store.record_feedback("s1", "not_relevant")
        tuner = self._make_tuner(store)
        assert tuner.maybe_adjust("t") is None

    def test_upper_bound_respected(self, feedback_store: FeedbackStore):
        self._seed_feedback(feedback_store, "t", not_relevant=10, helpful=0)
        tuner = self._make_tuner(feedback_store, min_score=0.048)
        result = tuner.maybe_adjust("t")
        assert result is not None
        assert result <= 0.05

    def test_lower_bound_respected(self, feedback_store: FeedbackStore):
        self._seed_feedback(feedback_store, "t", not_relevant=0, helpful=10)
        tuner = self._make_tuner(feedback_store, min_score=0.007)
        result = tuner.maybe_adjust("t")
        assert result is not None
        assert result >= 0.005

    def test_get_effective_min_score_default(self, feedback_store: FeedbackStore):
        tuner = self._make_tuner(feedback_store)
        assert tuner.get_effective_min_score("unknown_tool") == 0.02

    def test_get_effective_min_score_adjusted(self, feedback_store: FeedbackStore):
        self._seed_feedback(feedback_store, "t", not_relevant=8, helpful=0)
        tuner = self._make_tuner(feedback_store)
        tuner.maybe_adjust("t")
        score = tuner.get_effective_min_score("t")
        assert score > 0.02
