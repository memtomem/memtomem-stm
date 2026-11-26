"""Never-pruned per-response rollup of ``progressive_reads`` (#1328, #1316 part B).

The core property: purging rows never changes what ``read_progressive_summary``
reports with ``all_time=True``. Everything else here is about the rollup's
installation markers and the states in which it reports itself unavailable.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from memtomem_stm.proxy import progressive_reads_store as prs
from memtomem_stm.proxy.progressive_reads_store import (
    ProgressiveReadsStore,
    read_progressive_summary,
)

DAY = 86400.0
FIELDS = (
    "available",
    "total_responses",
    "total_reads",
    "follow_up_responses",
    "unclassified_reads",
    "produced_chars",
    "delivered_chars",
    "delivered_ratio",
    "oldest_at",
)


def _open(db: Path, retention_days: int = 0) -> None:
    store = ProgressiveReadsStore(db, retention_days=retention_days)
    store.initialize()
    store.close()


def _insert(db: Path, rows: list[tuple[object, ...]]) -> list[int]:
    """Insert ``(key, tool, offset, chars, total_chars, created_at, is_initial)`` rows."""
    conn = sqlite3.connect(db)
    try:
        ids = []
        for key, tool, offset, chars, total, created, initial in rows:
            cur = conn.execute(
                "INSERT INTO progressive_reads (key, trace_id, server, tool, offset, chars, "
                "served_to, total_chars, created_at, is_initial) "
                "VALUES (?, NULL, 'srv', ?, ?, ?, ?, ?, ?, ?)",
                (key, tool, offset, chars, offset + 1, total, created, initial),
            )
            ids.append(int(cur.lastrowid or 0))
        conn.commit()
        return ids
    finally:
        conn.close()


def _sql(db: Path, *statements: str) -> None:
    conn = sqlite3.connect(db)
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def _meta(db: Path) -> dict[str, str]:
    conn = sqlite3.connect(db)
    try:
        return dict(conn.execute("SELECT key, value FROM progressive_reads_meta").fetchall())
    finally:
        conn.close()


def _legacy_db(db: Path) -> None:
    """The table as an older binary leaves it: classified, but no rollup."""
    conn = sqlite3.connect(db)
    try:
        conn.executescript(prs._SCHEMA)
        conn.execute("ALTER TABLE progressive_reads ADD COLUMN is_initial INTEGER DEFAULT NULL")
        conn.commit()
    finally:
        conn.close()


def _figures(summary: dict[str, object]) -> dict[str, object]:
    return {name: summary[name] for name in FIELDS}


def _seed_mixed(db: Path) -> None:
    """Rows that exercise every per-key rule, split across the 90-day cutoff."""
    now = time.time()
    old, new = now - 100 * DAY, now - DAY
    _insert(
        db,
        [
            # a: initial purged, follow-up retained — the response straddles the purge.
            ("a", "search", 0, 100, 1000, old, 1),
            ("a", "search", 100, 200, 1000, new, 0),
            # b: legacy rows without the classification, both purged.
            ("b", "fetch", 0, 50, 400, old, None),
            ("b", "fetch", 50, 50, 400, old - DAY, None),
            # c: only a follow-up row survives (its initial row was lost earlier).
            ("c", "search", 300, 300, 900, new, 0),
            # d: one key under two tools with different sizes; one half purged.
            ("d", "search", 0, 10, 500, old, 1),
            ("d", "fetch", 0, 20, 800, new, 1),
            # e: never followed up, retained.
            ("e", "search", 0, 70, 700, new, 1),
        ],
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "reads.db"
    _open(path)
    return path


class TestExactness:
    @pytest.mark.parametrize("tool", [None, "search", "fetch"])
    def test_purge_does_not_change_the_all_time_figures(self, db: Path, tool: str | None):
        _seed_mixed(db)
        before = read_progressive_summary(db, tool=tool)

        _open(db, retention_days=90)

        after = read_progressive_summary(db, tool=tool, all_time=True)
        retained = read_progressive_summary(db, tool=tool)
        assert after["scope"] == "all_time"
        assert _figures(after) == _figures(before)
        # The purge really removed rows, so the equality is the rollup's doing.
        assert retained["total_reads"] < before["total_reads"]

    def test_a_trigger_that_rolls_nothing_up_breaks_the_equality(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Negative control: the module itself defines a trigger that records
        # nothing, so the rollup still reads as installed and whole.
        noop = "progressive_reads BEGIN SELECT 1; END"
        monkeypatch.setattr(
            prs,
            "_ROLLUP_TRIGGER_DDL",
            "CREATE TRIGGER IF NOT EXISTS progressive_reads_rollup_on_delete_v1 "
            f"AFTER DELETE ON {noop}",
        )
        monkeypatch.setattr(
            prs,
            "_ROLLUP_TRIGGER_STORED_SQL",
            f"CREATE TRIGGER progressive_reads_rollup_on_delete_v1 AFTER DELETE ON {noop}",
        )
        db = tmp_path / "reads.db"
        _open(db)
        _seed_mixed(db)
        before = read_progressive_summary(db)

        _open(db, retention_days=90)

        after = read_progressive_summary(db, all_time=True)
        assert after["available"] is True
        assert _figures(after) != _figures(before)

    def test_the_first_open_rolls_up_its_own_purge(self, tmp_path: Path):
        # A DB written only by an older binary: the open that installs the
        # rollup is also the one that purges, and nothing may slip between.
        db = tmp_path / "old.db"
        _legacy_db(db)
        _seed_mixed(db)
        before = read_progressive_summary(db)

        _open(db, retention_days=90)

        after = read_progressive_summary(db, all_time=True)
        assert _figures(after) == _figures(before)
        assert after["rollup"]["lost_before_install"] == 0

    def test_a_purge_by_an_older_binary_is_rolled_up(self, db: Path):
        _seed_mixed(db)
        before = read_progressive_summary(db)

        _sql(db, "DELETE FROM progressive_reads")

        after = read_progressive_summary(db, all_time=True)
        assert _figures(after) == _figures(before)
        assert read_progressive_summary(db)["total_reads"] == 0


class TestMalformedValues:
    @pytest.mark.parametrize("order", ["valid_first", "rejected_first"])
    def test_malformed_rows_count_the_same_before_and_after_a_purge(self, db: Path, order: str):
        now = time.time()
        ids = _insert(
            db,
            [
                ("m", "t", 0, 10, 100, now, 1),
                ("m", "t", 10, "x", "bad", now, 0),
                ("m", "t", 20, float("inf"), float("inf"), "not a time", 0),
            ],
        )
        before = read_progressive_summary(db, all_time=True)

        for row_id in ids if order == "valid_first" else reversed(ids):
            _sql(db, f"DELETE FROM progressive_reads WHERE id = {row_id}")

        after = read_progressive_summary(db, all_time=True)
        assert after["error"] is None
        assert _figures(after) == _figures(before)
        # The rejected sizes contribute nothing; the valid maximum survives.
        assert after["produced_chars"] == 100
        assert after["delivered_chars"] == 10
        assert after["total_reads"] == 3

    def test_sums_past_int64_fall_back_without_raising(self, db: Path):
        now = time.time()
        ids = _insert(
            db,
            [
                ("big", "t", 0, 5 * 10**18, 10, now, 1),
                ("big", "t", 1, 5 * 10**18, 10, now, 0),
            ],
        )

        summary = read_progressive_summary(db, all_time=True)
        assert summary["error"] is None
        assert summary["delivered_chars"] == 10**19

        _sql(db, f"DELETE FROM progressive_reads WHERE id = {ids[0]}")
        _sql(db, f"DELETE FROM progressive_reads WHERE id = {ids[1]}")
        summary = read_progressive_summary(db, all_time=True)
        assert summary["error"] is None
        assert summary["delivered_chars"] == 10**19

    def test_a_value_past_the_documented_cutoff_counts_zero(self, db: Path):
        _insert(db, [("edge", "t", 0, 2**63 - 1, 10, time.time(), 1)])

        assert read_progressive_summary(db, all_time=True)["delivered_chars"] == 0


class TestInstallation:
    def test_a_fresh_db_is_complete_from_the_start(self, db: Path):
        rollup = read_progressive_summary(db, all_time=True)["rollup"]

        assert rollup["installed"] is True
        assert rollup["lost_before_install"] == 0
        assert rollup["incomplete_before"] is None

    def test_rows_purged_before_installation_are_counted(self, tmp_path: Path):
        db = tmp_path / "old.db"
        # What an older binary leaves behind: the table, and rows it purged.
        _legacy_db(db)
        ids = _insert(db, [("k", "t", i, 1, 1, time.time(), 1) for i in range(5)])
        _sql(db, f"DELETE FROM progressive_reads WHERE id IN ({ids[0]}, {ids[1]})")

        _open(db)

        meta = _meta(db)
        rollup = read_progressive_summary(db, all_time=True)["rollup"]
        assert rollup["lost_before_install"] == 2
        assert rollup["incomplete_before"] == float(meta["rollup_complete_from"])
        assert meta["rollup_complete_from"] == meta["rollup_installed_at"]
        # The lost rows' tool is unknown, so a filter matching nothing keeps the warning.
        filtered = read_progressive_summary(db, tool="other", all_time=True)
        assert filtered["total_responses"] == 0
        assert filtered["rollup"]["incomplete_before"] == rollup["incomplete_before"]

    def test_a_utf16_database_installs_reads_and_reopens_intact(self, tmp_path: Path):
        db = tmp_path / "utf16.db"
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA encoding = 'UTF-16le'")
        conn.execute("CREATE TABLE placeholder (x)")
        conn.commit()
        conn.close()
        _open(db)
        first = _meta(db)

        _open(db)

        rollup = read_progressive_summary(db, all_time=True)["rollup"]
        assert rollup["installed"] is True
        assert rollup["lost_before_install"] == 0
        assert _meta(db) == first

    def test_reopening_an_intact_rollup_changes_nothing(self, db: Path):
        first = _meta(db)

        _open(db)

        assert _meta(db) == first

    def test_a_failed_purge_rolls_back_the_installation(self, tmp_path: Path):
        # The installation succeeds and the purge after it fails: both must go,
        # or a later purge would run under markers this one never earned.
        db = tmp_path / "reads.db"
        _legacy_db(db)
        _insert(db, [("k", "t", 0, 1, 1, time.time() - 100 * DAY, 1)])
        _sql(
            db,
            "CREATE TRIGGER refuse_purge BEFORE DELETE ON progressive_reads "
            "BEGIN SELECT RAISE(ABORT, 'refused'); END",
        )

        with pytest.raises(sqlite3.Error, match="refused"):
            _open(db, retention_days=90)

        conn = sqlite3.connect(db)
        try:
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            remaining = conn.execute("SELECT COUNT(*) FROM progressive_reads").fetchone()[0]
        finally:
            conn.close()
        assert not names & {"progressive_reads_meta", "progressive_reads_rollup"}
        assert remaining == 1


class TestRepair:
    """Part of the rollup removed from outside: the loss becomes unknown, from now."""

    def _assert_repaired(self, db: Path, before_repair: float) -> None:
        meta = _meta(db)
        rollup = read_progressive_summary(db, all_time=True)["rollup"]
        assert rollup["installed"] is True
        assert rollup["lost_before_install"] is None
        assert meta["rollup_rows_lost_before_install"] == "unknown"
        assert rollup["incomplete_before"] is not None
        assert rollup["incomplete_before"] >= before_repair

    def test_trigger_dropped_and_a_post_install_row_purged(self, db: Path):
        installed_at = _meta(db)["rollup_installed_at"]
        _sql(db, "DROP TRIGGER progressive_reads_rollup_on_delete_v1")
        _insert(db, [("late", "t", 0, 5, 5, time.time(), 1)])
        _sql(db, "DELETE FROM progressive_reads")
        before_repair = time.time()

        _open(db)

        self._assert_repaired(db, before_repair)
        assert _meta(db)["rollup_installed_at"] == installed_at

    def test_table_and_trigger_dropped(self, db: Path):
        _sql(
            db,
            "DROP TRIGGER progressive_reads_rollup_on_delete_v1",
            "DROP TABLE progressive_reads_rollup",
        )
        before_repair = time.time()

        _open(db)

        self._assert_repaired(db, before_repair)

    def test_objects_gone_and_a_malformed_marker_left_is_not_a_first_install(self, db: Path):
        # A marker that is there but unreadable still says the rollup existed,
        # so rolled-up reads may have gone with the table.
        _sql(
            db,
            "DROP TRIGGER progressive_reads_rollup_on_delete_v1",
            "DROP TABLE progressive_reads_rollup",
            "DELETE FROM progressive_reads_meta WHERE key != 'rollup_installed_at'",
            "UPDATE progressive_reads_meta SET value = 'nan' WHERE key = 'rollup_installed_at'",
        )
        before_repair = time.time()

        _open(db)

        self._assert_repaired(db, before_repair)

    def test_a_trigger_moved_with_a_renamed_table_is_replaced(self, db: Path):
        _sql(db, "ALTER TABLE progressive_reads RENAME TO archived_reads")
        # A trigger on another table feeds nothing, so it is not installed.
        assert read_progressive_summary(db, all_time=True)["available"] is False
        before_repair = time.time()

        _open(db)
        _insert(db, [("k", "t", 0, 7, 7, time.time(), 1)])
        _sql(db, "DELETE FROM progressive_reads")

        self._assert_repaired(db, before_repair)
        assert read_progressive_summary(db, all_time=True)["delivered_chars"] == 7

    def test_a_renamed_rollup_table_takes_its_trigger_along(self, db: Path):
        # SQLite rewrites the trigger to write into the renamed table, so the
        # trigger must be replaced, not just the table re-created.
        _sql(db, "ALTER TABLE progressive_reads_rollup RENAME TO archived_rollup")
        assert read_progressive_summary(db, all_time=True)["available"] is False
        before_repair = time.time()

        _open(db)
        _insert(db, [("k", "t", 0, 7, 7, time.time(), 1)])
        _sql(db, "DELETE FROM progressive_reads")

        self._assert_repaired(db, before_repair)
        assert read_progressive_summary(db, all_time=True)["delivered_chars"] == 7

    def test_a_marker_that_is_not_utf8_is_damage_not_a_crash(self, db: Path):
        _sql(
            db,
            "UPDATE progressive_reads_meta SET value = CAST(x'80' AS TEXT) "
            "WHERE key = 'rollup_installed_at'",
        )
        summary = read_progressive_summary(db, all_time=True)
        assert summary["error"] is None
        assert summary["available"] is False
        before_repair = time.time()

        _open(db)

        self._assert_repaired(db, before_repair)

    def test_an_altered_rollup_table_is_set_aside_before_the_purge(self, db: Path):
        # A CHECK added from outside would fail the trigger, and with it every purge.
        _sql(db, "ALTER TABLE progressive_reads_rollup ADD COLUMN x INTEGER CHECK (reads <= 1)")
        assert read_progressive_summary(db, all_time=True)["available"] is False
        now = time.time()
        _insert(
            db, [("k", "t", 0, 3, 9, now - 100 * DAY, 1), ("k", "t", 3, 4, 9, now - 99 * DAY, 0)]
        )
        before_repair = time.time()

        _open(db, retention_days=90)

        self._assert_repaired(db, before_repair)
        summary = read_progressive_summary(db, all_time=True)
        assert (summary["total_reads"], summary["delivered_chars"]) == (2, 7)
        conn = sqlite3.connect(db)
        try:
            aside = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name LIKE 'progressive_reads_rollup_replaced_%'"
                )
            ]
        finally:
            conn.close()
        assert len(aside) == 1

    def test_a_moved_coverage_boundary_beside_a_known_loss_is_damage(self, tmp_path: Path):
        db = tmp_path / "old.db"
        _legacy_db(db)
        ids = _insert(db, [("k", "t", i, 1, 1, time.time(), 1) for i in range(3)])
        _sql(db, f"DELETE FROM progressive_reads WHERE id = {ids[0]}")
        _open(db)
        _sql(db, "UPDATE progressive_reads_meta SET value = '1' WHERE key = 'rollup_complete_from'")
        assert read_progressive_summary(db, all_time=True)["available"] is False
        before_repair = time.time()

        _open(db)

        self._assert_repaired(db, before_repair)

    def test_an_oversized_loss_marker_is_damage_not_a_crash(self, db: Path):
        _sql(
            db,
            "UPDATE progressive_reads_meta SET value = '" + "9" * 4301 + "' "
            "WHERE key = 'rollup_rows_lost_before_install'",
        )
        assert read_progressive_summary(db, all_time=True)["available"] is False
        before_repair = time.time()

        _open(db)

        self._assert_repaired(db, before_repair)

    @pytest.mark.parametrize(
        "key", ["rollup_installed_at", "rollup_rows_lost_before_install", "rollup_complete_from"]
    )
    def test_one_marker_deleted(self, db: Path, key: str):
        _sql(db, f"DELETE FROM progressive_reads_meta WHERE key = '{key}'")
        before_repair = time.time()

        _open(db)

        self._assert_repaired(db, before_repair)


class TestUnavailable:
    @pytest.mark.parametrize("value", ["nan", "inf", "-inf", "soon"])
    @pytest.mark.parametrize("key", ["rollup_installed_at", "rollup_complete_from"])
    def test_a_malformed_marker_makes_all_time_unavailable(self, db: Path, key: str, value: str):
        _insert(db, [("k", "t", 0, 1, 1, time.time(), 1)])
        _sql(db, f"UPDATE progressive_reads_meta SET value = '{value}' WHERE key = '{key}'")

        summary = read_progressive_summary(db, all_time=True)

        assert summary["available"] is False
        assert summary["rollup"]["installed"] is False
        assert read_progressive_summary(db)["total_responses"] == 1

    @pytest.mark.parametrize(
        "key", ["rollup_installed_at", "rollup_rows_lost_before_install", "rollup_complete_from"]
    )
    def test_a_missing_marker_makes_all_time_unavailable(self, db: Path, key: str):
        _sql(db, f"DELETE FROM progressive_reads_meta WHERE key = '{key}'")

        assert read_progressive_summary(db, all_time=True)["available"] is False

    def test_a_missing_trigger_makes_all_time_unavailable(self, db: Path):
        _sql(db, "DROP TRIGGER progressive_reads_rollup_on_delete_v1")

        assert read_progressive_summary(db, all_time=True)["available"] is False

    def test_the_retained_summary_carries_no_rollup(self, db: Path):
        summary = read_progressive_summary(db)

        assert summary["scope"] == "retained"
        assert "rollup" not in summary
