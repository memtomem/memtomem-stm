"""Issue #352 part 2 — ``FeedbackStore`` query-text retention.

Three angles:

1. ``cleanup_expired_queries`` nulls only rows older than the configured
   retention window, leaves newer rows alone, and preserves the event row
   itself so aggregate counts in ``stm_surfacing_stats`` survive.
2. ``retention_seconds <= 0`` is the documented "disabled" sentinel and
   must short-circuit before touching the DB.
3. Legacy DBs created with the pre-#352 ``query TEXT NOT NULL`` schema
   migrate to the relaxed schema on the next ``initialize`` so the
   retention UPDATE can actually land.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from memtomem_stm.surfacing.feedback_store import (
    FeedbackStore,
    _relax_surfacing_events_query_notnull,
)


def _record_event(store: FeedbackStore, surfacing_id: str, *, age_seconds: float) -> None:
    """Insert one ``surfacing_events`` row with ``created_at = now - age``.

    ``record_surfacing`` stamps ``time.time()`` itself, so we patch the row
    after insert rather than monkeypatching the clock — pinning the
    actual stored ``created_at`` is the contract under test.
    """
    store.record_surfacing(
        surfacing_id=surfacing_id,
        server="s",
        tool="read_file",
        query=f"raw query for {surfacing_id}",
        memory_ids=["m1", "m2"],
        scores=[0.7, 0.3],
    )
    assert store._db is not None
    store._db.execute(
        "UPDATE surfacing_events SET created_at = ? WHERE id = ?",
        (time.time() - age_seconds, surfacing_id),
    )
    store._db.commit()


class TestCleanupExpiredQueries:
    def test_nulls_only_rows_older_than_retention(self, tmp_path: Path) -> None:
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()

        _record_event(store, "old", age_seconds=10_000)
        _record_event(store, "new", age_seconds=10)

        nulled = store.cleanup_expired_queries(retention_seconds=1_000)

        assert nulled == 1
        assert store._db is not None
        rows = dict(
            store._db.execute("SELECT id, query FROM surfacing_events ORDER BY id").fetchall()
        )
        assert rows == {"new": "raw query for new", "old": None}
        store.close()

    def test_preserves_event_row_for_stats(self, tmp_path: Path) -> None:
        """The row itself must survive so per-tool surfacing counts in
        ``stm_surfacing_stats`` (which read ``SELECT COUNT(*) FROM
        surfacing_events``) stay accurate. Only the query column is
        cleared."""
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()

        _record_event(store, "old", age_seconds=10_000)

        store.cleanup_expired_queries(retention_seconds=1_000)

        assert store._db is not None
        count = store._db.execute("SELECT COUNT(*) FROM surfacing_events").fetchone()[0]
        assert count == 1
        # Sibling columns also intact.
        row = store._db.execute(
            "SELECT server, tool, memory_ids, scores FROM surfacing_events WHERE id = 'old'"
        ).fetchone()
        assert row == ("s", "read_file", '["m1", "m2"]', "[0.7, 0.3]")
        store.close()

    def test_zero_retention_is_disabled(self, tmp_path: Path) -> None:
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()

        _record_event(store, "old", age_seconds=10_000)

        assert store.cleanup_expired_queries(retention_seconds=0) == 0
        assert store.cleanup_expired_queries(retention_seconds=-1) == 0

        assert store._db is not None
        (preserved,) = store._db.execute(
            "SELECT query FROM surfacing_events WHERE id = 'old'"
        ).fetchone()
        assert preserved == "raw query for old"
        store.close()

    def test_idempotent_on_already_nulled_rows(self, tmp_path: Path) -> None:
        """Re-running the cleanup must not double-count already-nulled
        rows — the ``WHERE query IS NOT NULL`` clause is load-bearing for
        the returned count operators see in INFO logs."""
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()

        _record_event(store, "old", age_seconds=10_000)

        first = store.cleanup_expired_queries(retention_seconds=1_000)
        second = store.cleanup_expired_queries(retention_seconds=1_000)

        assert (first, second) == (1, 0)
        store.close()

    def test_no_db_short_circuits(self, tmp_path: Path) -> None:
        store = FeedbackStore(tmp_path / "fb.db")
        # Not initialized — _db is None.
        assert store.cleanup_expired_queries(retention_seconds=1_000) == 0


class TestGetStatsHandlesNullQuery:
    """``query`` becomes nullable in this PR. ``get_stats`` walks every
    recent row and previously did ``len(query)`` unconditionally, which
    crashes on ``None``. After retention has actually swept rows the
    column legitimately yields ``None`` from SELECT, so the stats path
    must degrade gracefully — operators reading ``stm_surfacing_stats``
    can otherwise see a TypeError 30 days into using the default."""

    def test_get_stats_renders_placeholder_for_nulled_query(self, tmp_path: Path) -> None:
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()

        _record_event(store, "old", age_seconds=10_000)
        _record_event(store, "new", age_seconds=10)
        store.cleanup_expired_queries(retention_seconds=1_000)

        stats = store.get_stats(limit=5)
        previews = {row["ts"]: row["query_preview"] for row in stats["recent"]}
        # The newer row still carries its raw query text; the aged row
        # is rendered with a stable placeholder rather than crashing.
        assert "raw query for new" in previews.values()
        assert "<expired>" in previews.values()

    def test_get_stats_does_not_crash_on_all_nulled(self, tmp_path: Path) -> None:
        """Edge case: retention swept every row in the recent window.
        ``get_stats`` must still return a well-formed dict, not raise."""
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()

        for i in range(3):
            _record_event(store, f"old-{i}", age_seconds=10_000)
        nulled = store.cleanup_expired_queries(retention_seconds=1_000)
        assert nulled == 3

        stats = store.get_stats(limit=5)
        assert len(stats["recent"]) == 3
        for row in stats["recent"]:
            assert row["query_preview"] == "<expired>"


class TestLegacyNotNullMigration:
    """Pre-#352 DBs have ``query TEXT NOT NULL``. The migration on next
    ``initialize`` must relax that constraint so the retention UPDATE can
    set ``query = NULL`` without an ``IntegrityError``."""

    @staticmethod
    def _seed_legacy_db(path: Path) -> None:
        db = sqlite3.connect(str(path))
        db.executescript(
            """
            CREATE TABLE surfacing_events (
                id          TEXT    PRIMARY KEY,
                server      TEXT    NOT NULL,
                tool        TEXT    NOT NULL,
                query       TEXT    NOT NULL,
                memory_ids  TEXT    NOT NULL,
                scores      TEXT    NOT NULL,
                created_at  REAL    NOT NULL
            );
            INSERT INTO surfacing_events
                (id, server, tool, query, memory_ids, scores, created_at)
            VALUES ('legacy-1', 's', 'read_file', 'old query', '["m1"]', '[0.5]', 0.0);
            """
        )
        db.commit()
        db.close()

    def test_initialize_migrates_notnull_off(self, tmp_path: Path) -> None:
        path = tmp_path / "fb.db"
        self._seed_legacy_db(path)

        # Sanity: legacy NOT NULL rejects UPDATE-to-NULL.
        legacy = sqlite3.connect(str(path))
        try:
            assert (
                legacy.execute(
                    "SELECT \"notnull\" FROM pragma_table_info('surfacing_events') "
                    "WHERE name = 'query'"
                ).fetchone()[0]
                == 1
            )
        finally:
            legacy.close()

        store = FeedbackStore(path)
        store.initialize()
        try:
            assert store._db is not None
            assert (
                store._db.execute(
                    "SELECT \"notnull\" FROM pragma_table_info('surfacing_events') "
                    "WHERE name = 'query'"
                ).fetchone()[0]
                == 0
            )
            # Seeded row survived the table swap.
            row = store._db.execute(
                "SELECT id, query FROM surfacing_events WHERE id = 'legacy-1'"
            ).fetchone()
            assert row == ("legacy-1", "old query")
            # Index recreated.
            idx_names = {
                r[0]
                for r in store._db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' "
                    "AND tbl_name = 'surfacing_events'"
                ).fetchall()
            }
            assert "idx_events_tool" in idx_names
            # Retention UPDATE now lands.
            store._db.execute("UPDATE surfacing_events SET created_at = 0 WHERE id = 'legacy-1'")
            store._db.commit()
            assert store.cleanup_expired_queries(retention_seconds=1.0) == 1
            (q,) = store._db.execute(
                "SELECT query FROM surfacing_events WHERE id = 'legacy-1'"
            ).fetchone()
            assert q is None
        finally:
            store.close()

    def test_migration_is_idempotent_on_fresh_db(self, tmp_path: Path) -> None:
        """Calling the helper on a current-schema DB is a no-op."""
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()
        try:
            assert store._db is not None
            # Second call must not recreate the table or raise.
            _relax_surfacing_events_query_notnull(store._db)
            assert (
                store._db.execute(
                    "SELECT \"notnull\" FROM pragma_table_info('surfacing_events') "
                    "WHERE name = 'query'"
                ).fetchone()[0]
                == 0
            )
        finally:
            store.close()


class TestDeleteEventsOlderThan:
    """#584 — row-level retention deletes aged-out events (and their
    feedback), bounding the table get_stats scans."""

    def test_deletes_old_events_and_cascades_feedback(self, tmp_path: Path) -> None:
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()

        _record_event(store, "old", age_seconds=10_000)
        _record_event(store, "new", age_seconds=10)
        # A feedback row on the old event must go too.
        assert store.record_feedback("old", "helpful", memory_id="m1")
        assert store.record_feedback("new", "helpful", memory_id="m1")

        deleted = store.delete_events_older_than(retention_seconds=1_000)

        assert deleted == 1
        assert store._db is not None
        events = [r[0] for r in store._db.execute("SELECT id FROM surfacing_events").fetchall()]
        assert events == ["new"]
        feedback_ids = [
            r[0]
            for r in store._db.execute("SELECT surfacing_id FROM surfacing_feedback").fetchall()
        ]
        assert feedback_ids == ["new"]  # the old event's feedback cascaded away
        store.close()

    def test_zero_retention_is_disabled(self, tmp_path: Path) -> None:
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()
        _record_event(store, "old", age_seconds=10_000)
        assert store.delete_events_older_than(retention_seconds=0) == 0
        assert store._db is not None
        assert store._db.execute("SELECT COUNT(*) FROM surfacing_events").fetchone()[0] == 1
        store.close()
