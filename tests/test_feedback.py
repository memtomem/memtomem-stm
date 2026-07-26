"""Tests for FeedbackStore, FeedbackTracker, and AutoTuner."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from memtomem_stm.proxy.compression_feedback_store import CompressionFeedbackStore
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.feedback import AutoTuner, FeedbackTracker
from memtomem_stm.surfacing.feedback_store import (
    FeedbackStore,
    _REQUIRED_TABLES,
    inspect_feedback_db,
)


# ---------------------------------------------------------------------------
# FeedbackStore
# ---------------------------------------------------------------------------


class TestFeedbackStore:
    @pytest.mark.parametrize("surrogate", ["\ud800", "\udbff", "\udc00", "\udfff"])
    def test_record_surfacing_escapes_content(self, feedback_store, surrogate):
        feedback_store.record_surfacing(
            "surfacing",
            "server",
            "tool",
            f"query{surrogate}",
            ["memory"],
            [0.5],
            f"scale{surrogate}",
        )
        row = feedback_store._db.execute(
            "SELECT query, score_scale FROM surfacing_events"
        ).fetchone()
        literal = f"\\u{ord(surrogate):04x}"
        assert row == (f"query{literal}", f"scale{literal}")
        row[0].encode("utf-8")
        row[1].encode("utf-8")

    @pytest.mark.parametrize("field", ["surfacing_id", "server", "tool", "memory_id"])
    def test_record_surfacing_refuses_unencodable_identifiers(self, feedback_store, field):
        values = {
            "surfacing_id": "surfacing",
            "server": "server",
            "tool": "tool",
            "memory_id": "memory",
        }
        values[field] += "\ud800"
        with pytest.raises(ValueError, match="must be a valid UTF-8 identifier"):
            feedback_store.record_surfacing(
                values["surfacing_id"],
                values["server"],
                values["tool"],
                "query",
                [values["memory_id"]],
                [0.5],
            )
        assert feedback_store.get_tool_feedback_summary()["total_surfacings"] == 0

    def test_legacy_memory_id_reads_skip_raw_surrogate_without_aliasing_literal(
        self, feedback_store
    ):
        import json

        raw = "memory\ud800"
        literal = r"memory\ud800"
        feedback_store.record_surfacing("surfacing", "server", "tool", "query", [literal], [0.5])
        feedback_store._db.execute(
            "UPDATE surfacing_events SET memory_ids = ? WHERE id = ?",
            (json.dumps([raw, literal]), "surfacing"),
        )
        feedback_store._db.commit()

        assert feedback_store.get_memory_ids_for_surfacing("surfacing") == [literal]
        assert feedback_store.get_stats()["recent"][0]["memory_ids"] == [literal]
        assert feedback_store.get_surfacing_event("surfacing")["memory_ids"] == [
            raw,
            literal,
        ]
        assert feedback_store.record_feedback("surfacing", "not_relevant", literal)
        assert not feedback_store.record_feedback("surfacing", "not_relevant", raw)

    def test_inspect_feedback_db_missing(self, tmp_path: Path):
        status = inspect_feedback_db(tmp_path / "missing.db")

        assert status["exists"] is False
        assert status["initialized"] is False
        assert status["missing_tables"] == list(_REQUIRED_TABLES)

    def test_inspect_feedback_db_existing_without_surfacing_tables(self, tmp_path: Path):
        db_path = tmp_path / "stm_feedback.db"
        db_path.touch()

        status = inspect_feedback_db(db_path)

        assert status["exists"] is True
        assert status["initialized"] is False
        assert "surfacing_events" in status["missing_tables"]

    def test_inspect_feedback_db_initialized(self, tmp_path: Path):
        db_path = tmp_path / "stm_feedback.db"
        store = FeedbackStore(db_path)
        store.initialize()
        try:
            status = inspect_feedback_db(db_path)
        finally:
            store.close()

        assert status["exists"] is True
        assert status["initialized"] is True
        assert status["missing_tables"] == []

    def test_inspect_feedback_db_pre_auto_tune_schema(self, tmp_path: Path):
        # Regression: pre-#332 DBs with only the three original tables must
        # report not-initialized so AutoTuner SQL errors don't follow a
        # "ready" signal from the inspector.
        import sqlite3

        db_path = tmp_path / "legacy.db"
        db = sqlite3.connect(str(db_path))
        try:
            db.executescript(
                "CREATE TABLE surfacing_events (id TEXT PRIMARY KEY);"
                "CREATE TABLE surfacing_feedback (id INTEGER PRIMARY KEY);"
                "CREATE TABLE seen_memories (memory_id TEXT PRIMARY KEY);"
            )
            db.commit()
        finally:
            db.close()

        status = inspect_feedback_db(db_path)

        assert status["exists"] is True
        assert status["initialized"] is False
        assert status["missing_tables"] == ["auto_tune_adjustments", "surfacing_faults"]

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
        assert stats["score_distribution"] == {"count": 0, "min": None, "max": None}

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
            "negative_count": 1,
        }
        assert stats["per_tool_breakdown"][1] == {
            "tool": "tool_b",
            "events": 1,
            "avg_memory_count": 1.0,
            "feedback_count": 1,
            "not_relevant_count": 0,
            "negative_count": 1,
        }
        assert stats["rating_distribution"] == {
            "helpful": 1,
            "not_relevant": 1,
            "already_known": 1,
        }
        assert stats["total_feedback"] == 3
        assert stats["date_range"]["first"] is not None
        assert stats["date_range"]["last"] is not None

    def test_get_stats_score_distribution_aggregates(self, feedback_store: FeedbackStore):
        """#560 step 3 — count/min/max span every score of every event in
        the filter window, and the tool filter narrows the aggregate to
        that tool's events only."""
        feedback_store.record_surfacing("s1", "srv", "tool_a", "q1", ["m1", "m2"], [0.03, 0.03])
        feedback_store.record_surfacing("s2", "srv", "tool_a", "q2", ["m3"], [0.03])
        feedback_store.record_surfacing("s3", "srv", "tool_b", "q3", ["m4", "m5"], [0.0164, 0.0325])

        stats = feedback_store.get_stats()
        assert stats["score_distribution"] == {"count": 5, "min": 0.0164, "max": 0.0325}

        # tool_a alone is flat: min == max is the zero-variance predicate.
        stats_a = feedback_store.get_stats(tool="tool_a")
        assert stats_a["score_distribution"] == {"count": 3, "min": 0.03, "max": 0.03}

    def test_get_stats_score_distribution_skips_malformed_scores(
        self, feedback_store: FeedbackStore
    ):
        """Corrupt scores JSON (or non-numeric entries) must not crash the
        aggregate or skew min/max — the row's memory counts still count."""
        feedback_store.record_surfacing("s1", "srv", "tool_a", "q1", ["m1"], [0.02])
        feedback_store._db.execute(  # type: ignore[union-attr]
            "UPDATE surfacing_events SET scores = ? WHERE id = ?",
            ("not-json", "s1"),
        )
        feedback_store._db.commit()  # type: ignore[union-attr]
        feedback_store.record_surfacing("s2", "srv", "tool_a", "q2", ["m2"], [0.03])
        # A JSON ``true`` must not sneak in as score 1.0 (bool is an int
        # subclass in Python).
        feedback_store._db.execute(  # type: ignore[union-attr]
            "UPDATE surfacing_events SET scores = ? WHERE id = ?",
            ("[true, 0.03]", "s2"),
        )
        feedback_store._db.commit()  # type: ignore[union-attr]

        stats = feedback_store.get_stats()
        assert stats["events_total"] == 2
        assert stats["score_distribution"] == {"count": 1, "min": 0.03, "max": 0.03}

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

    def test_negative_ratio_counts_already_known(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing("s1", "sv", "t", "q", ["m1"], [0.5])
        for i in range(20):
            feedback_store.record_feedback("s1", "already_known" if i < 12 else "helpful")

        ratio = feedback_store.get_tool_negative_ratio("t", min_samples=20)
        assert ratio is not None
        assert abs(ratio - 0.6) < 0.01

    def test_ratio_excludes_feedback_from_nonrrf_scale_events(self, feedback_store: FeedbackStore):
        """AutoTuner-facing ratios move an RRF-calibrated threshold, so
        feedback earned on a scale-gated (core-named non-RRF) surfacing must
        not count; rrf-stamped and unstamped events keep counting."""
        feedback_store.record_surfacing("s-rrf", "sv", "t", "q1", ["m1"], [0.02], score_scale="rrf")
        feedback_store.record_surfacing("s-bare", "sv", "t", "q2", ["m2"], [0.02])
        feedback_store.record_surfacing(
            "s-rr", "sv", "t", "q3", ["m3"], [-0.17], score_scale="rerank"
        )
        for _ in range(10):
            feedback_store.record_feedback("s-rrf", "not_relevant")
            feedback_store.record_feedback("s-bare", "helpful")
        # 20 more not_relevant on the rerank event — would drag the ratio to
        # 0.75 if pass-all-scale feedback leaked into the tuner's evidence.
        for _ in range(20):
            feedback_store.record_feedback("s-rr", "not_relevant")

        ratio = feedback_store.get_tool_not_relevant_ratio("t", min_samples=20)
        assert ratio is not None
        assert abs(ratio - 0.5) < 0.01

    def test_global_ratio_excludes_nonrrf_but_keeps_orphaned_feedback(
        self, feedback_store: FeedbackStore
    ):
        """The global (tool=None) cold-start fallback applies the same scale
        scoping, while feedback whose events row was aged out by retention
        (LEFT JOIN → NULL scale) keeps counting as before."""
        feedback_store.record_surfacing("s-rrf", "sv", "t", "q1", ["m1"], [0.02], score_scale="rrf")
        feedback_store.record_surfacing(
            "s-rr", "sv", "t", "q2", ["m2"], [-0.17], score_scale="rerank"
        )
        feedback_store.record_surfacing("s-gone", "sv", "t", "q3", ["m3"], [0.02])
        for _ in range(10):
            feedback_store.record_feedback("s-rrf", "not_relevant")
            feedback_store.record_feedback("s-gone", "helpful")
        for _ in range(30):
            feedback_store.record_feedback("s-rr", "not_relevant")
        # Orphan s-gone's feedback: simulate the retention sweep deleting the
        # events row while its feedback rows survive.
        assert feedback_store._db is not None
        feedback_store._db.execute("DELETE FROM surfacing_events WHERE id = ?", ("s-gone",))
        feedback_store._db.commit()

        ratio = feedback_store.get_tool_not_relevant_ratio(None, min_samples=20)
        assert ratio is not None
        # 10 not_relevant / (10 rrf + 10 orphaned) — the 30 rerank ratings
        # are out of the denominator too.
        assert abs(ratio - 0.5) < 0.01

    def test_negative_feedback_counts_distinct_surfacing_events(
        self, feedback_store: FeedbackStore
    ):
        feedback_store.record_surfacing("s1", "sv", "t", "q", ["m1"], [0.5])
        feedback_store.record_surfacing("s2", "sv", "t", "q", ["m1"], [0.5])
        feedback_store.record_feedback("s1", "not_relevant", "m1")
        feedback_store.record_feedback("s1", "not_relevant", "m1")
        feedback_store.record_feedback("s1", "helpful", "m1")
        feedback_store.record_feedback("s2", "already_known", "m1")

        counts = feedback_store.get_negative_feedback_counts(["m1", "missing"])

        assert counts == {"m1": 2, "missing": 0}

    def test_negative_feedback_counts_expand_blanket_feedback(self, feedback_store: FeedbackStore):
        feedback_store.record_surfacing("s1", "sv", "t", "q", ["m1", "m2"], [0.5, 0.4])
        feedback_store.record_surfacing("s2", "sv", "t", "q", ["m2"], [0.4])
        feedback_store.record_feedback("s1", "not_relevant")
        feedback_store.record_feedback("s1", "already_known")
        feedback_store.record_feedback("s2", "helpful")

        counts = feedback_store.get_negative_feedback_counts(["m1", "m2"])

        assert counts == {"m1": 1, "m2": 1}

    def test_negative_feedback_counts_dedupes_blanket_and_explicit_same_event(
        self, feedback_store: FeedbackStore
    ):
        feedback_store.record_surfacing("s1", "sv", "t", "q", ["m1", "m2"], [0.5, 0.4])
        feedback_store.record_surfacing("s2", "sv", "t", "q", ["m1"], [0.5])
        feedback_store.record_feedback("s1", "not_relevant")
        feedback_store.record_feedback("s1", "already_known", "m1")
        feedback_store.record_feedback("s2", "not_relevant", "m1")

        counts = feedback_store.get_negative_feedback_counts(["m1", "m2"])

        assert counts == {"m1": 2, "m2": 1}

    @pytest.mark.parametrize("surrogate", ["\ud800", "\udbff", "\udc00", "\udfff"])
    def test_negative_feedback_counts_drop_only_the_unencodable_id(
        self, feedback_store: FeedbackStore, surrogate: str
    ):
        """One bad id must not blank the counts for the valid ones.

        The caller (``SurfacingEngine._demoted_ids``) reads a missing/zero
        count as "not enough negatives", so bailing on the whole batch would
        resurface a memory already rated past the demotion threshold for as
        long as one unencodable id rode along in the same candidate set.
        """
        feedback_store.record_surfacing("s1", "sv", "t", "q", ["m1"], [0.5])
        feedback_store.record_surfacing("s2", "sv", "t", "q", ["m1"], [0.5])
        feedback_store.record_feedback("s1", "not_relevant", "m1")
        feedback_store.record_feedback("s2", "already_known", "m1")

        counts = feedback_store.get_negative_feedback_counts([f"bad{surrogate}", "m1", "missing"])

        # The unencodable id is absent rather than 0: it can never match a
        # stored row, and absent reads identically at the call site.
        assert counts == {"m1": 2, "missing": 0}

    @pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
    def test_negative_feedback_counts_empty_when_every_id_unencodable(
        self, feedback_store: FeedbackStore, surrogate: str
    ):
        feedback_store.record_surfacing("s1", "sv", "t", "q", ["m1"], [0.5])
        feedback_store.record_feedback("s1", "not_relevant", "m1")

        assert feedback_store.get_negative_feedback_counts([f"bad{surrogate}"]) == {}

    def test_per_tool_breakdown_includes_feedback_counts(self, feedback_store: FeedbackStore):
        """Per-tool breakdown exposes total, not_relevant, and negative counts."""
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
        assert by_tool["tool_a"]["negative_count"] == 2
        assert by_tool["tool_b"]["feedback_count"] == 0
        assert by_tool["tool_b"]["not_relevant_count"] == 0
        assert by_tool["tool_b"]["negative_count"] == 0

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
        assert row["negative_count"] == 0


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
    def test_bootstrap_status(self, tmp_path: Path):
        tracker = FeedbackTracker(SurfacingConfig(), db_path=tmp_path / "fb.db")
        try:
            status = tracker.bootstrap_status()
        finally:
            tracker.close()

        assert status["exists"] is True
        assert status["initialized"] is True
        assert status["missing_tables"] == []

    def test_invalid_rating_rejected(self, tmp_path: Path):
        tracker = FeedbackTracker(SurfacingConfig(), db_path=tmp_path / "fb.db")
        try:
            result = tracker.record_feedback("s1", "invalid_rating")
            assert "Error" in result
        finally:
            tracker.close()

    @pytest.mark.parametrize("surrogate", ["\ud800", "\udbff", "\udc00", "\udfff"])
    @pytest.mark.parametrize("field", ["surfacing_id", "memory_id"])
    def test_refused_identifier_error_omits_unencodable_value(self, tmp_path, field, surrogate):
        tracker = FeedbackTracker(
            SurfacingConfig(), db_path=tmp_path / f"{field}-{ord(surrogate)}.db"
        )
        values = {"surfacing_id": "surfacing", "memory_id": "memory"}
        values[field] += surrogate
        try:
            result = tracker.record_feedback(
                values["surfacing_id"],
                "helpful",
                values["memory_id"],
            )
            assert result == f"Error: {field} must be a valid UTF-8 identifier"
            assert surrogate not in result
            result.encode("utf-8")
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

    def _seed_feedback(
        self,
        store: FeedbackStore,
        tool: str,
        not_relevant: int,
        helpful: int,
        already_known: int = 0,
        partially_helpful: int = 0,
    ):
        store.record_surfacing("s1", "sv", tool, "q", ["m1"], [0.5])
        for _ in range(not_relevant):
            store.record_feedback("s1", "not_relevant")
        for _ in range(already_known):
            store.record_feedback("s1", "already_known")
        for _ in range(helpful):
            store.record_feedback("s1", "helpful")
        for _ in range(partially_helpful):
            store.record_feedback("s1", "partially_helpful")

    def test_high_not_relevant_raises_threshold(self, feedback_store: FeedbackStore):
        self._seed_feedback(feedback_store, "t", not_relevant=5, helpful=1)
        tuner = self._make_tuner(feedback_store)
        result = tuner.maybe_adjust("t")
        assert result is not None
        assert result > 0.02

    def test_already_known_raises_threshold(self, feedback_store: FeedbackStore):
        self._seed_feedback(feedback_store, "t", not_relevant=0, helpful=0, already_known=5)
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

    def test_adjustment_persists_across_restart(self, tmp_path: Path):
        db_path = tmp_path / "fb.db"
        store1 = FeedbackStore(db_path)
        store1.initialize()
        try:
            self._seed_feedback(store1, "t", not_relevant=8, helpful=0)
            tuner1 = self._make_tuner(store1)
            adjusted = tuner1.maybe_adjust("t")
            assert adjusted is not None
        finally:
            store1.close()

        # Simulate restart: fresh store + tuner on same DB file.
        store2 = FeedbackStore(db_path)
        store2.initialize()
        try:
            tuner2 = self._make_tuner(store2)
            # The previously-persisted tuning is visible without any
            # further feedback being recorded.
            assert tuner2.adjustments == {"t": adjusted}
            assert tuner2.get_effective_min_score("t") == adjusted
        finally:
            store2.close()

    def test_save_and_load_adjustments_roundtrip(self, feedback_store: FeedbackStore):
        feedback_store.save_adjustment("tool_a", 0.025)
        feedback_store.save_adjustment("tool_b", 0.015)
        feedback_store.save_adjustment("tool_a", 0.030)  # upsert overwrites
        assert feedback_store.load_adjustments() == {"tool_a": 0.030, "tool_b": 0.015}

    # ── #353 part 2: partially_helpful is neutral ───────────────────────

    def test_partially_helpful_does_not_lower_threshold(self, feedback_store: FeedbackStore):
        """A tool whose feedback is mostly ``partially_helpful`` must NOT
        trigger the lower-threshold branch — that signal means "useful
        context but not directly used," not "surface more aggressively."
        Pre-#353 the old ``negative_ratio < 0.2`` branch fired on this
        shape because ``partially_helpful`` diluted the negative ratio
        to ~0; the split helpful/negative branches make it a true no-op.
        """
        self._seed_feedback(feedback_store, "t", not_relevant=0, helpful=0, partially_helpful=10)
        tuner = self._make_tuner(feedback_store)
        assert tuner.maybe_adjust("t") is None

    def test_partially_helpful_does_not_count_as_helpful(self, feedback_store: FeedbackStore):
        """Mixed helpful + partially_helpful at the boundary: 7 helpful +
        3 partially_helpful gives a helpful ratio of 0.7, below the 0.8
        lower-threshold gate, so the tuner stays put. If
        ``partially_helpful`` were aliased into ``helpful`` the ratio
        would be 1.0 and the threshold would drop.
        """
        self._seed_feedback(feedback_store, "t", not_relevant=0, helpful=7, partially_helpful=3)
        tuner = self._make_tuner(feedback_store)
        assert tuner.maybe_adjust("t") is None

    def test_partially_helpful_counts_in_denominator_dilutes_negative(
        self, feedback_store: FeedbackStore
    ):
        """5 not_relevant + 5 partially_helpful = total 10, negative ratio
        0.5 — below the 0.6 raise gate. Without ``partially_helpful`` in
        the denominator (numerator only) the ratio would be 1.0 and fire.
        """
        self._seed_feedback(feedback_store, "t", not_relevant=5, helpful=0, partially_helpful=5)
        tuner = self._make_tuner(feedback_store)
        assert tuner.maybe_adjust("t") is None

    def test_partially_helpful_records_as_valid_rating(self, tmp_path: Path):
        """VALID_RATINGS extension end-to-end — tracker accepts the value
        and the row lands in the store with the expected rating string.
        """
        from memtomem_stm.surfacing.feedback import FeedbackTracker

        tracker = FeedbackTracker(SurfacingConfig(), db_path=tmp_path / "fb.db")
        try:
            tracker.record_surfacing("s1", "sv", "t", "q", ["m1"], [0.5])
            result = tracker.record_feedback("s1", "partially_helpful")
            assert "recorded" in result.lower()
            assert "partially_helpful" in result
            summary = tracker.store.get_tool_feedback_summary(tool="t")
            assert summary["by_rating"].get("partially_helpful") == 1
        finally:
            tracker.close()
