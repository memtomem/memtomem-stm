"""Tests for ``read_surfacing_summary`` — the read-only stats helper behind
``mms stats``.

It must open the feedback DB read-only (never create/migrate it), return only
counts + the rating distribution (no ``recent`` query previews — a stats
summary must not leak query text), and degrade cleanly on a missing DB.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from helpers import seed_foreign_feedback_vocabulary
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


class TestForeignVocabulary:
    """Stored decisions and ratings STM never writes are counted, not shown (#1091)."""

    DECISIONS = {
        "surfaced": 1,
        "skip:gate_cooldown": 1,
        "error:timeout": 1,
        "error:TimeoutError": 1,
        "other": 4,
    }
    RATINGS = {"helpful": 1, "not_relevant": 1, "other": 2}

    def test_offline_summary_folds_foreign_values(self, tmp_path):
        db_path = tmp_path / "feedback.db"
        seed_foreign_feedback_vocabulary(db_path)

        summary = read_surfacing_summary(db_path)

        assert summary["opportunity_decisions"] == self.DECISIONS
        assert summary["opportunities_total"] == 8
        assert summary["rating_distribution"] == self.RATINGS
        assert summary["total_feedback"] == 4

    def test_live_stats_fold_foreign_values(self, tmp_path):
        db_path = tmp_path / "feedback.db"
        seed_foreign_feedback_vocabulary(db_path)
        store = FeedbackStore(db_path)
        store.initialize()
        try:
            stats = store.get_stats()
        finally:
            store.close()

        assert stats["opportunity_decisions"] == self.DECISIONS
        assert stats["opportunities_total"] == 8
        assert stats["rating_distribution"] == self.RATINGS
        assert stats["total_feedback"] == 4

    def test_every_value_stm_writes_is_kept(self):
        """Drift guard: a new skip reason, outcome or rating is recognized
        without editing the reader, as is an ASCII exception-type label."""
        from typing import get_args

        from memtomem_stm.surfacing import feedback_store
        from memtomem_stm.surfacing.engine import _OUTCOME_LABELS, _skip_label
        from memtomem_stm.surfacing.feedback import VALID_RATINGS
        from memtomem_stm.surfacing.observability import SkipReason

        written = {
            *_OUTCOME_LABELS.values(),
            *(_skip_label(reason) for reason in get_args(SkipReason)),
            f"error:{TimeoutError.__name__}",
            f"error:{BaseExceptionGroup.__name__}",
        }
        assert len(written) > 20
        assert [d for d in written if not feedback_store._known_gate_decision(d)] == []
        assert [r for r in VALID_RATINGS if not feedback_store._known_rating(r)] == []
        assert not feedback_store._known_gate_decision(feedback_store._UNRECOGNIZED)
        assert not feedback_store._known_rating(feedback_store._UNRECOGNIZED)


def test_load_hmac_key_open_failure_names_the_type_not_the_message(tmp_path, monkeypatch):
    from memtomem_stm.surfacing import feedback_store

    db_path = tmp_path / "feedback.db"
    seed_foreign_feedback_vocabulary(db_path)

    def failing_connect(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open cnrySchemaText")

    monkeypatch.setattr(feedback_store.sqlite3, "connect", failing_connect)
    with pytest.raises(RuntimeError) as info:
        feedback_store.load_hmac_key(db_path)
    assert str(info.value) == "cannot open surfacing feedback DB: OperationalError"
