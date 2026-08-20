"""Tests for ``read_surfacing_summary`` — the read-only stats helper behind
``mms stats``.

It must open the feedback DB read-only (never create/migrate it), return only
counts + the rating distribution (no ``recent`` query previews — a stats
summary must not leak query text), and degrade cleanly on a missing DB.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from memtomem_stm.surfacing.feedback_store import FeedbackStore, read_surfacing_summary


def _seed(db_path: Path) -> None:
    store = FeedbackStore(db_path)
    store.initialize()
    try:
        store.record_surfacing("s1", "c7", "query-docs", "secret query text", ["m1", "m2"], [0.9, 0.5])
        store.record_surfacing("s2", "lf", "search", "another query", ["m3"], [0.8])
        store.record_feedback("s1", "helpful", "m1")
        store.record_feedback("s2", "not_relevant", "m3")
    finally:
        store.close()


class TestReadSurfacingSummary:
    def test_counts_and_ratings(self, tmp_path):
        db_path = tmp_path / "feedback.db"
        _seed(db_path)

        summary = read_surfacing_summary(db_path)

        assert summary["available"] is True
        assert summary["events_total"] == 2
        assert summary["distinct_tools"] == 2
        assert summary["total_feedback"] == 2
        assert summary["rating_distribution"] == {"helpful": 1, "not_relevant": 1}
        # Never leaks query text — no recent rows / query previews.
        assert "recent" not in summary
        assert "secret query text" not in str(summary)

    def test_tool_filter(self, tmp_path):
        db_path = tmp_path / "feedback.db"
        _seed(db_path)

        summary = read_surfacing_summary(db_path, tool="query-docs")

        assert summary["events_total"] == 1
        assert summary["distinct_tools"] == 1
        assert summary["total_feedback"] == 1
        assert summary["rating_distribution"] == {"helpful": 1}

    def test_missing_db_unavailable_and_not_created(self, tmp_path):
        db_path = tmp_path / "nope.db"

        summary = read_surfacing_summary(db_path)

        assert summary["available"] is False
        assert summary["events_total"] == 0
        assert not db_path.exists()

    def test_unrelated_db_is_unavailable(self, tmp_path):
        db_path = tmp_path / "other.db"
        db = sqlite3.connect(db_path)
        db.execute("CREATE TABLE something_else (x INTEGER)")
        db.commit()
        db.close()

        summary = read_surfacing_summary(db_path)

        assert summary["available"] is False

    def test_does_not_mutate_existing_db(self, tmp_path):
        db_path = tmp_path / "feedback.db"
        _seed(db_path)
        before = db_path.stat().st_mtime_ns

        read_surfacing_summary(db_path)

        assert db_path.stat().st_mtime_ns == before
