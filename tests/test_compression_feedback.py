"""Tests for CompressionFeedbackStore, CompressionFeedbackTracker, and
the ``lookup_recent_trace_id`` helper on MetricsStore.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from memtomem_stm.proxy.compression_feedback import CompressionFeedbackTracker
from memtomem_stm.proxy.compression_feedback_store import (
    TRACE_LOOKUP_WINDOW_SECONDS,
    CompressionFeedbackStore,
    is_valid_kind,
    valid_kinds,
)
from memtomem_stm.proxy.metrics import CallMetrics
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.surfacing.feedback_store import FeedbackStore


# ---------------------------------------------------------------------------
# Schema + kind registry
# ---------------------------------------------------------------------------


class TestKindRegistry:
    def test_valid_kinds_sorted_and_complete(self):
        assert valid_kinds() == [
            "missing_example",
            "missing_metadata",
            "other",
            "truncated",
            "wrong_topic",
        ]

    def test_is_valid_kind(self):
        assert is_valid_kind("truncated") is True
        assert is_valid_kind("missing_example") is True
        assert is_valid_kind("other") is True
        assert is_valid_kind("expired") is False
        assert is_valid_kind("") is False


# ---------------------------------------------------------------------------
# CompressionFeedbackStore
# ---------------------------------------------------------------------------


class TestCompressionFeedbackStore:
    def test_record_and_get_stats_empty(self, tmp_path: Path):
        store = CompressionFeedbackStore(tmp_path / "cfb.db")
        store.initialize()
        try:
            stats = store.get_stats()
            assert stats == {"total_feedback": 0, "by_kind": {}, "by_tool": {}}
        finally:
            store.close()

    def test_record_persists_single_row(self, tmp_path: Path):
        store = CompressionFeedbackStore(tmp_path / "cfb.db")
        store.initialize()
        try:
            store.record(
                server="docfix",
                tool="get_document",
                kind="truncated",
                missing="example for Query.select",
                trace_id="abc123",
            )
            stats = store.get_stats()
            assert stats["total_feedback"] == 1
            assert stats["by_kind"] == {"truncated": 1}
            assert stats["by_tool"] == {"get_document": 1}
        finally:
            store.close()

    def test_get_stats_aggregates_by_kind_and_tool(self, tmp_path: Path):
        store = CompressionFeedbackStore(tmp_path / "cfb.db")
        store.initialize()
        try:
            store.record("docfix", "get_document", "truncated", "m1", None)
            store.record("docfix", "get_document", "missing_example", "m2", None)
            store.record("docfix", "search", "wrong_topic", "m3", None)
            store.record("next", "docs", "truncated", "m4", None)

            stats = store.get_stats()
            assert stats["total_feedback"] == 4
            assert stats["by_kind"] == {
                "truncated": 2,
                "missing_example": 1,
                "wrong_topic": 1,
            }
            assert stats["by_tool"] == {
                "get_document": 2,
                "search": 1,
                "docs": 1,
            }
        finally:
            store.close()

    def test_get_stats_filtered_by_tool(self, tmp_path: Path):
        store = CompressionFeedbackStore(tmp_path / "cfb.db")
        store.initialize()
        try:
            store.record("docfix", "get_document", "truncated", "m1", None)
            store.record("docfix", "get_document", "missing_example", "m2", None)
            store.record("docfix", "search", "wrong_topic", "m3", None)

            stats = store.get_stats(tool="get_document")
            assert stats["total_feedback"] == 2
            assert stats["by_kind"] == {"truncated": 1, "missing_example": 1}
            # by_tool is intentionally empty when a tool filter is set,
            # so callers can always ``stats["by_tool"]`` without branching.
            assert stats["by_tool"] == {}
        finally:
            store.close()

    def test_coexists_with_surfacing_feedback_store(self, tmp_path: Path):
        """Opening both stores on the same DB file must not conflict."""
        db_path = tmp_path / "stm_feedback.db"

        surfacing = FeedbackStore(db_path)
        surfacing.initialize()
        compression = CompressionFeedbackStore(db_path)
        compression.initialize()

        try:
            surfacing.record_surfacing(
                "surf1", "docfix", "search", "query", ["m1"], [0.9]
            )
            compression.record(
                "docfix", "search", "other", "compression report", None
            )

            # Both subsystems see only their own rows.
            surf_stats = surfacing.get_tool_feedback_summary()
            assert surf_stats["total_surfacings"] == 1
            cfb_stats = compression.get_stats()
            assert cfb_stats["total_feedback"] == 1
        finally:
            compression.close()
            surfacing.close()

    def test_close_is_idempotent(self, tmp_path: Path):
        store = CompressionFeedbackStore(tmp_path / "cfb.db")
        store.initialize()
        store.close()
        store.close()  # must not raise


# ---------------------------------------------------------------------------
# MetricsStore.lookup_recent_trace_id
# ---------------------------------------------------------------------------


def _record_metric(
    store: MetricsStore,
    server: str,
    tool: str,
    trace_id: str | None,
) -> None:
    store.record(
        CallMetrics(
            server=server,
            tool=tool,
            original_chars=100,
            compressed_chars=50,
            cleaned_chars=80,
            trace_id=trace_id,
        )
    )


def _backdate_last_row(db_path: Path, delta_seconds: float) -> None:
    """Push the most recent proxy_metrics row's ``created_at`` into the past."""
    conn = sqlite3.connect(str(db_path))
    try:
        now = time.time()
        conn.execute(
            "UPDATE proxy_metrics SET created_at = ? "
            "WHERE id = (SELECT MAX(id) FROM proxy_metrics)",
            (now - delta_seconds,),
        )
        conn.commit()
    finally:
        conn.close()


class TestMetricsStoreLookup:
    def test_empty_store_returns_none(self, tmp_path: Path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        try:
            assert store.lookup_recent_trace_id("docfix", "get_document", 1800.0) is None
        finally:
            store.close()

    def test_finds_most_recent_matching(self, tmp_path: Path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        try:
            _record_metric(store, "docfix", "get_document", "old_trace")
            _record_metric(store, "docfix", "get_document", "new_trace")
            assert (
                store.lookup_recent_trace_id("docfix", "get_document", 1800.0)
                == "new_trace"
            )
        finally:
            store.close()

    def test_filters_by_server_and_tool(self, tmp_path: Path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        try:
            _record_metric(store, "docfix", "get_document", "doc_trace")
            _record_metric(store, "docfix", "search", "search_trace")
            _record_metric(store, "next", "get_document", "next_trace")
            assert (
                store.lookup_recent_trace_id("docfix", "get_document", 1800.0)
                == "doc_trace"
            )
            assert (
                store.lookup_recent_trace_id("docfix", "search", 1800.0)
                == "search_trace"
            )
        finally:
            store.close()

    def test_skips_rows_with_null_trace_id(self, tmp_path: Path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        try:
            _record_metric(store, "docfix", "get_document", "first")
            _record_metric(store, "docfix", "get_document", None)
            # Freshest row has NULL trace_id; lookup must fall back to the
            # previous row rather than returning NULL.
            assert (
                store.lookup_recent_trace_id("docfix", "get_document", 1800.0)
                == "first"
            )
        finally:
            store.close()

    def test_filters_by_window(self, tmp_path: Path):
        db_path = tmp_path / "metrics.db"
        store = MetricsStore(db_path)
        store.initialize()
        try:
            _record_metric(store, "docfix", "get_document", "stale")
            # Push the row far enough into the past that a 30-minute window
            # will not see it.
            _backdate_last_row(db_path, delta_seconds=3600.0)
            assert (
                store.lookup_recent_trace_id(
                    "docfix", "get_document", TRACE_LOOKUP_WINDOW_SECONDS
                )
                is None
            )
        finally:
            store.close()


# ---------------------------------------------------------------------------
# CompressionFeedbackTracker
# ---------------------------------------------------------------------------


@pytest.fixture
def metrics_store(tmp_path: Path) -> MetricsStore:
    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    yield store
    store.close()


class TestCompressionFeedbackTracker:
    def test_invalid_kind_rejected(self, tmp_path: Path):
        tracker = CompressionFeedbackTracker(tmp_path / "cfb.db")
        try:
            result = tracker.record(
                server="docfix",
                tool="get_document",
                missing="x",
                kind="bogus",
            )
            assert "kind must be one of" in result
            # The bogus call must not have persisted anything.
            assert tracker.get_stats()["total_feedback"] == 0
        finally:
            tracker.close()

    def test_missing_fields_rejected(self, tmp_path: Path):
        tracker = CompressionFeedbackTracker(tmp_path / "cfb.db")
        try:
            assert "server and tool" in tracker.record(
                server="", tool="x", missing="m"
            )
            assert "server and tool" in tracker.record(
                server="x", tool="", missing="m"
            )
            assert "missing description" in tracker.record(
                server="x", tool="y", missing=""
            )
            assert tracker.get_stats()["total_feedback"] == 0
        finally:
            tracker.close()

    def test_explicit_trace_id_persisted(self, tmp_path: Path):
        tracker = CompressionFeedbackTracker(tmp_path / "cfb.db")
        try:
            result = tracker.record(
                server="docfix",
                tool="get_document",
                missing="example code",
                kind="missing_example",
                trace_id="explicit_trace",
            )
            assert "explicit_trace" in result
            stats = tracker.get_stats()
            assert stats["by_kind"] == {"missing_example": 1}
        finally:
            tracker.close()

    def test_trace_id_lookup_within_window(
        self, tmp_path: Path, metrics_store: MetricsStore
    ):
        _record_metric(metrics_store, "docfix", "get_document", "live_trace")

        tracker = CompressionFeedbackTracker(
            tmp_path / "cfb.db", metrics_store=metrics_store
        )
        try:
            result = tracker.record(
                server="docfix",
                tool="get_document",
                missing="headings only",
                kind="truncated",
            )
            # The tracker echoes the resolved trace_id in its status string.
            assert "live_trace" in result
        finally:
            tracker.close()

    def test_trace_id_lookup_outside_window_stores_null(
        self, tmp_path: Path, metrics_store: MetricsStore
    ):
        metrics_db = tmp_path / "metrics.db"
        _record_metric(metrics_store, "docfix", "get_document", "stale_trace")
        _backdate_last_row(metrics_db, delta_seconds=3600.0)

        tracker = CompressionFeedbackTracker(
            tmp_path / "cfb.db", metrics_store=metrics_store
        )
        try:
            result = tracker.record(
                server="docfix",
                tool="get_document",
                missing="headings only",
                kind="truncated",
            )
            assert "unresolved" in result
        finally:
            tracker.close()

    def test_trace_id_lookup_different_tool_stores_null(
        self, tmp_path: Path, metrics_store: MetricsStore
    ):
        _record_metric(metrics_store, "docfix", "get_document", "wrong_tool")

        tracker = CompressionFeedbackTracker(
            tmp_path / "cfb.db", metrics_store=metrics_store
        )
        try:
            result = tracker.record(
                server="docfix",
                tool="search",  # different tool
                missing="x",
                kind="other",
            )
            assert "unresolved" in result
        finally:
            tracker.close()

    def test_record_without_metrics_store_stores_null(self, tmp_path: Path):
        tracker = CompressionFeedbackTracker(tmp_path / "cfb.db")
        try:
            result = tracker.record(
                server="docfix",
                tool="get_document",
                missing="x",
                kind="other",
            )
            assert "unresolved" in result
        finally:
            tracker.close()

    def test_get_stats_passthrough(self, tmp_path: Path):
        tracker = CompressionFeedbackTracker(tmp_path / "cfb.db")
        try:
            tracker.record(
                server="docfix", tool="get_document", missing="x", kind="truncated"
            )
            tracker.record(
                server="docfix", tool="search", missing="y", kind="wrong_topic"
            )

            all_stats = tracker.get_stats()
            assert all_stats["total_feedback"] == 2
            assert all_stats["by_tool"] == {"get_document": 1, "search": 1}

            filtered = tracker.get_stats(tool="search")
            assert filtered["total_feedback"] == 1
            assert filtered["by_kind"] == {"wrong_topic": 1}
            assert filtered["by_tool"] == {}
        finally:
            tracker.close()
