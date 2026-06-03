"""Tests for ``proxy.metrics_store`` — schema migration idempotency and
persistence of the observability fields introduced for PR 1 (F2).

Most of the store is exercised indirectly through
``test_error_metrics.py``; these tests target the migration machinery
directly because a botched ALTER TABLE can wedge every deployed instance
on the next restart.
"""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from memtomem_stm.proxy.metrics import CallMetrics
from memtomem_stm.proxy.metrics_store import MetricsStore, read_compression_summary


NEW_COLUMNS = {
    "index_ok",
    "index_error",
    "chunks_indexed",
    "extract_ok",
    "extract_error",
    "surfacing_on_progressive_ok",
    "surface_error",
}


def _column_names(db: sqlite3.Connection) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA table_info(proxy_metrics)")}


class TestMigrationIdempotency:
    """Three DB states must produce the same schema with no errors.

    State (a) fresh empty DB — first-ever install.
    State (b) pre-F2 DB — has earlier columns but not the new ones.
    State (c) already-migrated DB — the new columns exist. This is the
        case on every restart after F2 ships; if ALTER re-runs it will
        raise ``OperationalError`` and take the proxy down on reboot.
    """

    def test_fresh_db_gets_all_columns(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        try:
            cols = _column_names(store._db)
            assert NEW_COLUMNS.issubset(cols), f"missing columns on fresh DB: {NEW_COLUMNS - cols}"
        finally:
            store.close()

    def test_pre_f2_db_gets_migrated(self, tmp_path):
        db_path = tmp_path / "metrics.db"
        # Hand-craft a DB that matches the original _CREATE schema from
        # before the F2 migration — no new columns, a pre-existing row
        # that must survive the migration with NULLs in the new fields.
        raw = sqlite3.connect(str(db_path))
        raw.execute(
            "CREATE TABLE proxy_metrics ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "server TEXT NOT NULL, tool TEXT NOT NULL, "
            "original_chars INTEGER NOT NULL, "
            "compressed_chars INTEGER NOT NULL, "
            "cleaned_chars INTEGER NOT NULL DEFAULT 0, "
            "created_at REAL NOT NULL)"
        )
        raw.execute(
            "INSERT INTO proxy_metrics "
            "(server, tool, original_chars, compressed_chars, cleaned_chars, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy", "tool", 1000, 500, 800, time.time()),
        )
        raw.commit()
        raw.close()

        store = MetricsStore(db_path)
        store.initialize()
        try:
            cols = _column_names(store._db)
            assert NEW_COLUMNS.issubset(cols)
            # Legacy row survives with NULL in the new columns — callers
            # must treat NULL as "not observed", distinct from 0/False.
            row = store._db.execute(
                "SELECT server, index_ok, index_error, chunks_indexed, "
                "extract_ok, surfacing_on_progressive_ok "
                "FROM proxy_metrics WHERE server = 'legacy'"
            ).fetchone()
            assert row is not None
            server, index_ok, index_error, chunks_indexed, extract_ok, surf_ok = row
            assert server == "legacy"
            assert index_ok is None
            assert index_error is None
            assert chunks_indexed == 0  # NOT NULL DEFAULT 0
            assert extract_ok is None
            assert surf_ok is None
        finally:
            store.close()

    def test_already_migrated_db_is_noop(self, tmp_path):
        """Closing and reopening an already-migrated store must not raise.

        This is the critical invariant: every proxy restart after F2
        ships re-runs ``_migrate``. If the ALTER statements are not
        guarded against "column already exists", SQLite raises
        ``OperationalError: duplicate column name`` and the store fails
        to initialize — i.e., the proxy cannot start on an existing DB.
        """
        db_path = tmp_path / "metrics.db"
        # First open: creates + migrates.
        store = MetricsStore(db_path)
        store.initialize()
        store.close()
        # Second open: every migration must be a no-op.
        store = MetricsStore(db_path)
        try:
            store.initialize()  # must not raise
            cols = _column_names(store._db)
            assert NEW_COLUMNS.issubset(cols)
        finally:
            store.close()
        # Third open for good measure — migrations run on every init().
        store = MetricsStore(db_path)
        try:
            store.initialize()
        finally:
            store.close()


class TestRecordPersistsNewFields:
    """``CallMetrics`` → SQLite round-trip for the F2 observability fields."""

    @pytest.fixture
    def store(self, tmp_path):
        s = MetricsStore(tmp_path / "metrics.db")
        s.initialize()
        yield s
        s.close()

    def test_sync_index_success_row(self, store):
        store.record(
            CallMetrics(
                server="gh",
                tool="read",
                original_chars=1000,
                compressed_chars=400,
                index_ok=True,
                chunks_indexed=5,
                extract_ok=True,
            )
        )
        row = store._db.execute(
            "SELECT index_ok, index_error, chunks_indexed, extract_ok, extract_error "
            "FROM proxy_metrics"
        ).fetchone()
        assert row == (1, None, 5, 1, None)

    def test_index_failure_row(self, store):
        store.record(
            CallMetrics(
                server="gh",
                tool="read",
                original_chars=1000,
                compressed_chars=400,
                index_ok=False,
                index_error="RuntimeError: embedding service down",
                chunks_indexed=0,
            )
        )
        row = store._db.execute(
            "SELECT index_ok, index_error, chunks_indexed FROM proxy_metrics"
        ).fetchone()
        assert row == (0, "RuntimeError: embedding service down", 0)

    def test_stage_not_run_preserves_null(self, store):
        """A call that did not run the INDEX stage (auto_index disabled,
        body below min_chars, …) records ``NULL`` — distinct from
        ``0``/False. Dashboards filtering on ``index_ok = 0`` must NOT
        see these rows."""
        store.record(
            CallMetrics(
                server="gh",
                tool="read",
                original_chars=100,
                compressed_chars=100,
                # index_ok / extract_ok default to None
            )
        )
        row = store._db.execute(
            "SELECT index_ok, extract_ok, surfacing_on_progressive_ok FROM proxy_metrics"
        ).fetchone()
        assert row == (None, None, None)

    def test_count_failures_distinct_from_unobserved(self, store):
        """Aggregate query on ``index_ok = 0`` returns only observed
        failures, not rows where the stage didn't run."""
        # Observed success
        store.record(
            CallMetrics(
                server="s",
                tool="t",
                original_chars=1,
                compressed_chars=1,
                index_ok=True,
                chunks_indexed=2,
            )
        )
        # Observed failure
        store.record(
            CallMetrics(
                server="s",
                tool="t",
                original_chars=1,
                compressed_chars=1,
                index_ok=False,
                index_error="disk full",
            )
        )
        # Stage did not run
        store.record(CallMetrics(server="s", tool="t", original_chars=1, compressed_chars=1))
        failures = store._db.execute(
            "SELECT COUNT(*) FROM proxy_metrics WHERE index_ok = 0"
        ).fetchone()[0]
        unobserved = store._db.execute(
            "SELECT COUNT(*) FROM proxy_metrics WHERE index_ok IS NULL"
        ).fetchone()[0]
        assert failures == 1
        assert unobserved == 1


class TestReadPathConcurrency:
    """Cross-thread reader/writer safety.

    The connection is opened with ``check_same_thread=False``, and
    ``record()`` / ``_trim()`` serialize via ``self._lock`` against
    thread-pool writers. Reader paths used to be lockless on the
    assumption that all callers live on the asyncio single-thread; that
    assumption is a fragile convention. This test pins the invariant by
    driving concurrent writers and readers through a thread pool — if a
    future refactor drops the reader locks and moves a caller to
    ``run_in_executor``, this test catches it before users do.
    """

    def test_readers_and_writer_concurrent(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        total_writes = 200
        reads_per_worker = 100

        def writer() -> None:
            for i in range(total_writes):
                store.record(
                    CallMetrics(
                        server="s",
                        tool=f"t{i % 5}",
                        original_chars=1000,
                        compressed_chars=400,
                        cleaned_chars=800,
                        trace_id=f"tr-{i}",
                    )
                )

        def run_get_tool_profiles() -> None:
            for _ in range(reads_per_worker):
                result = store.get_tool_profiles(since_seconds=3600.0)
                assert isinstance(result, list)
                for row in result:
                    # If a torn read ever happened the dict would be
                    # missing keys or carry mismatched values; asserting
                    # the contract forces a failure rather than silent
                    # corruption.
                    assert row["call_count"] >= 1
                    assert isinstance(row["server"], str)

        def run_get_history() -> None:
            for _ in range(reads_per_worker):
                result = store.get_history(limit=50)
                assert isinstance(result, list)
                for row in result:
                    assert row["original_chars"] == 1000
                    assert row["compressed_chars"] == 400

        def run_lookup_recent_trace_id() -> None:
            for _ in range(reads_per_worker):
                trace = store.lookup_recent_trace_id("s", "t0", within_seconds=3600.0)
                # trace_id is either a valid match or ``None`` if the
                # writer hasn't produced a ``t0`` row yet. Never an
                # empty string or malformed value.
                assert trace is None or trace.startswith("tr-")

        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(writer),
                    pool.submit(run_get_tool_profiles),
                    pool.submit(run_get_history),
                    pool.submit(run_lookup_recent_trace_id),
                ]
                # ``f.result()`` re-raises any exception the worker hit,
                # so a race-induced SQLite error fails the test here.
                for f in futures:
                    f.result(timeout=30)

            final_count = store._db.execute(
                "SELECT COUNT(*) FROM proxy_metrics"
            ).fetchone()[0]
            assert final_count == total_writes
        finally:
            store.close()


class TestReadCompressionSummary:
    """``read_compression_summary`` aggregates per-(server, tool) compression
    stats read-only — it must never create or migrate the DB (the CLI ``mms
    stats`` read path depends on this) and must degrade, not crash, on a
    missing file or a pre-migration schema."""

    def _seed(self, db_path):
        store = MetricsStore(db_path)
        store.initialize()
        try:
            store.record(
                CallMetrics(server="c7", tool="query-docs", original_chars=1000, compressed_chars=400)
            )
            store.record(
                CallMetrics(server="c7", tool="query-docs", original_chars=1000, compressed_chars=600)
            )
            store.record(
                CallMetrics(server="lf", tool="search", original_chars=500, compressed_chars=500)
            )
            store.record(
                CallMetrics(
                    server="lf", tool="search", original_chars=0, compressed_chars=0, is_error=True
                )
            )
        finally:
            store.close()

    def test_totals_and_per_tool(self, tmp_path):
        db_path = tmp_path / "metrics.db"
        self._seed(db_path)

        summary = read_compression_summary(db_path)

        assert summary["available"] is True
        assert summary["schema_outdated"] is False
        assert summary["total_calls"] == 4
        assert summary["error_count"] == 1
        assert summary["total_original_chars"] == 2500
        assert summary["total_compressed_chars"] == 1500
        assert summary["saved_chars"] == 1000
        assert summary["saved_ratio"] == 0.4  # 1 - 1500/2500

        by_tool = {(r["server"], r["tool"]): r for r in summary["by_tool"]}
        qd = by_tool[("c7", "query-docs")]
        assert qd["calls"] == 2
        assert qd["original_chars"] == 2000
        assert qd["compressed_chars"] == 1000
        assert qd["saved_ratio"] == 0.5
        # busiest tool sorts first
        assert summary["by_tool"][0]["tool"] == "query-docs"

    def test_tool_filter(self, tmp_path):
        db_path = tmp_path / "metrics.db"
        self._seed(db_path)

        summary = read_compression_summary(db_path, tool="search")

        assert summary["total_calls"] == 2
        assert {r["tool"] for r in summary["by_tool"]} == {"search"}

    def test_missing_db_is_unavailable_and_not_created(self, tmp_path):
        db_path = tmp_path / "does_not_exist.db"

        summary = read_compression_summary(db_path)

        assert summary["available"] is False
        assert summary["total_calls"] == 0
        assert not db_path.exists()  # read path must not create the file

    def test_pre_migration_schema_degrades(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        # Original pre-F2 schema: no ``is_error`` column.
        db = sqlite3.connect(db_path)
        db.execute(
            "CREATE TABLE proxy_metrics ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, server TEXT, tool TEXT, "
            "original_chars INTEGER, compressed_chars INTEGER, "
            "cleaned_chars INTEGER DEFAULT 0, created_at REAL)"
        )
        db.execute(
            "INSERT INTO proxy_metrics (server, tool, original_chars, compressed_chars, created_at) "
            "VALUES ('s', 't', 100, 40, 0)"
        )
        db.commit()
        db.close()

        summary = read_compression_summary(db_path)

        assert summary["available"] is True
        assert summary["schema_outdated"] is True
        assert summary["error_count"] == 0  # degraded, not crashed
        assert summary["total_original_chars"] == 100
        assert summary["saved_ratio"] == 0.6

    def test_unrelated_db_is_unavailable(self, tmp_path):
        db_path = tmp_path / "other.db"
        db = sqlite3.connect(db_path)
        db.execute("CREATE TABLE something_else (x INTEGER)")
        db.commit()
        db.close()

        summary = read_compression_summary(db_path)

        assert summary["available"] is False

    def test_does_not_mutate_existing_db(self, tmp_path):
        db_path = tmp_path / "metrics.db"
        self._seed(db_path)
        before = db_path.stat().st_mtime_ns
        cols_before = self._columns(db_path)

        read_compression_summary(db_path)

        assert db_path.stat().st_mtime_ns == before
        assert self._columns(db_path) == cols_before

    @staticmethod
    def _columns(db_path):
        db = sqlite3.connect(db_path)
        try:
            return {row[1] for row in db.execute("PRAGMA table_info(proxy_metrics)")}
        finally:
            db.close()
