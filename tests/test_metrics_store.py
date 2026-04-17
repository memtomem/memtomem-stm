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

import pytest

from memtomem_stm.proxy.metrics import CallMetrics
from memtomem_stm.proxy.metrics_store import MetricsStore


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
