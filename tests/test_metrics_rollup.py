"""The never-pruned daily rollup of ``proxy_metrics`` (#1316).

The rollup holds the rows that have LEFT the raw table, filled by an AFTER
DELETE trigger; an all-time total is the rollup plus the rows still retained.
The load-bearing properties, each pinned below:

- conservation: whatever deletes rows (per-write trim, startup reconcile, an
  older binary's own SQL), all-time totals equal what was recorded;
- the trigger cannot fail, because a failing trigger fails every trim and so
  rolls back every ``record`` on every binary;
- installation is atomic and first-writer-wins;
- the reader is read-only and never reports an all-time figure for a DB whose
  rollup is not (fully) installed;
- coverage warnings depend on real timestamps, so trimming a row never changes
  them.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from memtomem_stm.proxy import metrics_store as metrics_store_module
from memtomem_stm.proxy.metrics import CallMetrics
from memtomem_stm.proxy.metrics_store import (
    MetricsStore,
    read_compression_all_time,
    read_compression_summary,
)

_DAILY = "proxy_metrics_daily"
_TRIGGER = "proxy_metrics_rollup_on_delete_v1"

# The per-source trim exactly as an older STM build issues it. Copied, not
# imported: an old binary has its own copy, and the point is that the rollup
# works for SQL this module did not run.
_OLD_BINARY_TRIM = (
    "DELETE FROM proxy_metrics WHERE id IN "
    "(SELECT id FROM proxy_metrics WHERE source = ? "
    "ORDER BY created_at ASC "
    "LIMIT max(0, (SELECT COUNT(*) FROM proxy_metrics WHERE source = ?) - ?))"
)

DAY = 86400.0


def _mixed_metrics() -> list[CallMetrics]:
    """One row of every accounting class the summary distinguishes."""
    rows: list[CallMetrics] = []
    for i in range(3):
        rows += [
            CallMetrics(
                server="docs",
                tool="search",
                original_chars=1000 + i,
                compressed_chars=300,
                compression_accounting="initial_response_v1",
            ),
            # Legacy/unclassified: MCP, unstamped, success, sized.
            CallMetrics(server="docs", tool="search", original_chars=500, compressed_chars=200),
            # Error row: unstamped but not "unclassified".
            CallMetrics(
                server="docs", tool="fetch", original_chars=0, compressed_chars=0, is_error=True
            ),
            # Structured with and without measured wire bytes (nullable).
            CallMetrics(
                server="api",
                tool="get",
                original_chars=800,
                compressed_chars=700,
                compression_accounting="initial_structured_v1",
                wire_bytes=None if i % 2 else 750,
            ),
            CallMetrics(
                server="builtin",
                tool="Bash",
                original_chars=400 + i,
                compressed_chars=400 + i,
                source="hook",
            ),
        ]
    return rows


def _comparable(summary: dict[str, Any]) -> dict[str, Any]:
    """The keys both readers share, with per-tool rows order-independent."""
    out = {k: v for k, v in summary.items() if k not in ("path", "rollup")}
    out["by_tool"] = sorted(
        ({k: v for k, v in row.items() if k != "tuner_calls"} for row in out["by_tool"]),
        key=lambda row: (row["server"], row["tool"]),
    )
    return out


def _open(path: Path, max_history: int = 1_000_000, **kwargs: Any) -> MetricsStore:
    store = MetricsStore(path, max_history=max_history, **kwargs)
    store.initialize()
    return store


def _record_all(path: Path, rows: list[CallMetrics], max_history: int) -> None:
    store = _open(path, max_history=max_history)
    try:
        for row in rows:
            store.record(row)
    finally:
        store.close()


def _insert_raw(
    path: Path, *, created_at: object, source: str = "mcp", tool: str = "t", **cols: object
) -> None:
    """Insert one row with an explicit ``created_at``, as any writer could."""
    values = {
        "server": "s",
        "tool": tool,
        "original_chars": 100,
        "compressed_chars": 40,
        "source": source,
        "created_at": created_at,
        **cols,
    }
    db = sqlite3.connect(str(path))
    try:
        names = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        db.execute(f"INSERT INTO proxy_metrics ({names}) VALUES ({marks})", list(values.values()))
        db.commit()
    finally:
        db.close()


def _strip_rollup(path: Path) -> None:
    """Turn a current DB into a pre-#1316 one: no rollup objects, stale stamp."""
    db = sqlite3.connect(str(path))
    try:
        db.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER}")
        db.execute(f"DROP TABLE IF EXISTS {_DAILY}")
        db.execute("DELETE FROM metrics_meta")
        db.commit()
    finally:
        db.close()


def _objects(path: Path) -> set[tuple[str, str]]:
    db = sqlite3.connect(str(path))
    try:
        return {(r[0], r[1]) for r in db.execute("SELECT type, name FROM sqlite_master")}
    finally:
        db.close()


def _meta(path: Path) -> dict[str, str]:
    db = sqlite3.connect(str(path))
    try:
        return dict(db.execute("SELECT key, value FROM metrics_meta").fetchall())
    finally:
        db.close()


class TestConservation:
    def test_per_write_trim_conserves_every_counter(self, tmp_path):
        """Same rows into an untrimmed store and a store capped at 2 per source:
        the capped store's all-time totals equal the untrimmed retained ones."""
        rows = _mixed_metrics()
        _record_all(tmp_path / "full.db", rows, max_history=1_000_000)
        _record_all(tmp_path / "capped.db", rows, max_history=2)

        retained = read_compression_summary(tmp_path / "capped.db")
        assert retained["total_calls"] == 4, "positive control: the cap really trimmed"

        expected = read_compression_summary(tmp_path / "full.db")
        actual = read_compression_all_time(tmp_path / "capped.db")
        assert actual["available"] is True
        assert _comparable(actual) == _comparable(expected)

    @pytest.mark.parametrize("source", [None, "mcp", "hook"])
    def test_startup_reconcile_conserves_and_honours_filters(self, tmp_path, source):
        path = tmp_path / "m.db"
        _record_all(path, _mixed_metrics(), max_history=1_000_000)
        before = read_compression_summary(path, source=source)
        before_tool = read_compression_summary(path, tool="search")

        _open(path, max_history=1).close()  # reconcile every source down to 1 row

        assert read_compression_summary(path)["total_calls"] == 2
        assert _comparable(read_compression_all_time(path, source=source)) == _comparable(before)
        assert _comparable(read_compression_all_time(path, tool="search")) == _comparable(
            before_tool
        )

    def test_an_older_binarys_inserts_and_deletes_are_counted(self, tmp_path):
        """A writer that has never heard of the rollup inserts with only the base
        columns and trims with its own SQL. An insert-side counter would miss
        both; the delete trigger lives in the DB and sees them."""
        path = tmp_path / "m.db"
        _open(path).close()
        now = time.time()
        old = sqlite3.connect(str(path))
        try:
            for i in range(6):
                old.execute(
                    "INSERT INTO proxy_metrics (server, tool, original_chars, compressed_chars, "
                    "cleaned_chars, created_at) VALUES ('legacy', 'q', ?, 10, 0, ?)",
                    (100 + i, now - i),
                )
            old.commit()
        finally:
            old.close()
        before = read_compression_summary(path)

        old = sqlite3.connect(str(path))
        try:
            old.execute(_OLD_BINARY_TRIM, ("mcp", "mcp", 1))
            old.commit()
        finally:
            old.close()

        assert read_compression_summary(path)["total_calls"] == 1
        assert _comparable(read_compression_all_time(path)) == _comparable(before)

    def test_null_wire_bytes_neither_aborts_nor_blanks_a_bucket(self, tmp_path):
        """``wire_bytes`` is nullable. The trim must not fail on it, and a NULL row
        sharing a daily bucket with measured rows must count as 0 — un-coalesced,
        ``750 + NULL`` would blank the bucket's measured bytes."""
        path = tmp_path / "m.db"
        structured = [
            CallMetrics(
                server="api",
                tool="get",
                original_chars=80,
                compressed_chars=70,
                compression_accounting="initial_structured_v1",
                wire_bytes=None if i % 2 else 750,
            )
            for i in range(4)
        ]
        _record_all(path, structured, max_history=1)  # per-write trim path
        assert read_compression_summary(path)["total_calls"] == 1
        _record_all(path, structured, max_history=1_000_000)
        assert read_compression_summary(path)["total_calls"] == 5
        _open(path, max_history=1).close()  # reconcile path moves 4 more
        assert read_compression_summary(path)["total_calls"] == 1
        result = read_compression_all_time(path)
        assert result["structured_delivery"] == {
            "calls": 8,
            "original_chars": 640,
            "serialized_base_chars": 560,
            "wire_bytes": 3000,
        }


class TestTriggerCannotFail:
    @pytest.mark.parametrize("created_at", ["not-a-timestamp", 1e300, -1e20])
    def test_hostile_values_in_every_read_column_survive_a_raw_delete(self, tmp_path, created_at):
        path = tmp_path / "m.db"
        _open(path).close()
        _insert_raw(
            path,
            created_at=created_at,
            original_chars="x",
            compressed_chars="y",
            is_error="z",
            wire_bytes="w",
            compression_accounting=5,
        )
        db = sqlite3.connect(str(path))
        try:
            db.execute("DELETE FROM proxy_metrics")
            db.commit()
            day = db.execute(f"SELECT day, calls, min_created_at FROM {_DAILY}").fetchall()
        finally:
            db.close()
        assert day == [("unknown", 1, None)]
        result = read_compression_all_time(path)
        assert result["total_calls"] == 1
        assert result["rollup"]["unknown_date_calls"] == 1

    def test_non_finite_sizes_in_one_bucket_survive_every_delete_path(self, tmp_path):
        """``inf + -inf`` is NULL in SQLite: summed raw, it would abort the DELETE
        under NOT NULL counters, or blank the bucket for every later row. Non-finite
        sizes contribute 0, and the finite rows after them still count."""
        path = tmp_path / "m.db"
        store = _open(path, max_history=1, reconcile_on_init=False)
        try:
            for size in (float("inf"), float("-inf"), 5, 100):
                store.record(
                    CallMetrics(server="s", tool="t", original_chars=size, compressed_chars=1)
                )
        finally:
            store.close()
        assert read_compression_summary(path)["total_calls"] == 1, "record trims ran"
        now = time.time()
        _insert_raw(path, created_at=now - 2, original_chars=float("inf"))
        _insert_raw(path, created_at=now - 1, original_chars=float("-inf"))
        _open(path, max_history=1).close()  # reconcile
        _insert_raw(path, created_at=now, original_chars=float("inf"))
        db = sqlite3.connect(str(path))
        try:
            db.execute("DELETE FROM proxy_metrics")  # raw
            db.commit()
        finally:
            db.close()
        result = read_compression_all_time(path)
        assert result["total_calls"] == 7
        assert result["total_original_chars"] == 105
        assert result["total_compressed_chars"] == 4 + 3 * 40  # record rows + raw rows

    def test_undatable_row_survives_both_trim_paths(self, tmp_path):
        """``-1e20`` sorts oldest, so the trims really pick it."""
        path = tmp_path / "m.db"
        _open(path).close()
        _insert_raw(path, created_at=-1e20, wire_bytes=None)
        _insert_raw(path, created_at=-2e20, wire_bytes=None)

        store = _open(path, max_history=1, reconcile_on_init=False)
        try:
            store.record(CallMetrics(server="s", tool="t", original_chars=1, compressed_chars=1))
        finally:
            store.close()
        _insert_raw(path, created_at=-3e20)
        _open(path, max_history=1).close()

        result = read_compression_all_time(path)
        assert result["total_calls"] == 4
        assert result["rollup"]["unknown_date_calls"] == 3


class TestInstallation:
    def test_fresh_db_installs_with_nothing_lost(self, tmp_path):
        path = tmp_path / "m.db"
        _open(path).close()
        assert {("table", _DAILY), ("trigger", _TRIGGER)} <= _objects(path)
        meta = _meta(path)
        assert float(meta["rollup_installed_at"]) <= time.time()
        assert meta["rollup_rows_lost_before_install"] == "0"

    def test_pre_rollup_db_counts_rows_already_trimmed(self, tmp_path):
        path = tmp_path / "m.db"
        _open(path).close()
        _strip_rollup(path)
        for i in range(10):
            _insert_raw(path, created_at=1_000_000.0 + i)
        db = sqlite3.connect(str(path))
        db.execute("DELETE FROM proxy_metrics WHERE id <= 4")  # no trigger yet: lost
        db.commit()
        db.close()

        _open(path).close()

        meta = _meta(path)
        assert meta["rollup_rows_lost_before_install"] == "4"
        rollup = read_compression_all_time(path)["rollup"]
        assert rollup["lost_before_install"] == 4
        assert rollup["incomplete_before"] == float(meta["rollup_installed_at"])

    def test_failed_install_leaves_no_rollup_objects(self, tmp_path, monkeypatch):
        """A failure between the daily table and the stamp rolls ALL of it back.

        Without the explicit transaction the daily table would survive with no
        trigger feeding it — a reader would take that as complete history."""
        path = tmp_path / "m.db"
        real_connect = sqlite3.connect

        class _FailsAtStamp:
            def __init__(self, conn: sqlite3.Connection) -> None:
                self._conn = conn

            def execute(self, sql: str, *args: Any) -> Any:
                if sql.startswith("INSERT INTO metrics_meta") and "schema_fingerprint" in str(args):
                    raise sqlite3.OperationalError("injected stamp failure")
                return self._conn.execute(sql, *args)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._conn, name)

        monkeypatch.setattr(
            "memtomem_stm.proxy.metrics_store.sqlite3.connect",
            lambda *a, **k: _FailsAtStamp(real_connect(*a, **k)),
        )
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            MetricsStore(path).initialize()
        monkeypatch.undo()

        objects = _objects(path)
        assert ("table", "proxy_metrics") in objects, "positive control: base DDL ran"
        assert ("table", _DAILY) not in objects
        assert ("trigger", _TRIGGER) not in objects
        assert ("table", "metrics_meta") not in objects

        _open(path).close()
        assert {("table", _DAILY), ("trigger", _TRIGGER)} <= _objects(path)

    def test_restamp_by_an_older_binary_keeps_the_first_install(self, tmp_path):
        path = tmp_path / "m.db"
        _record_all(path, _mixed_metrics(), max_history=1_000_000)
        first = _meta(path)
        before = read_compression_summary(path)

        db = sqlite3.connect(str(path))
        db.execute(
            "UPDATE metrics_meta SET value = '1|id,server|idx' WHERE key = 'schema_fingerprint'"
        )
        db.commit()
        db.close()
        _open(path, max_history=1).close()  # slow path again, then reconcile

        after = _meta(path)
        assert after["rollup_installed_at"] == first["rollup_installed_at"]
        assert after["rollup_rows_lost_before_install"] == "0"
        db = sqlite3.connect(str(path))
        triggers = db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'"
        ).fetchone()[0]
        db.close()
        assert triggers == 1
        assert _comparable(read_compression_all_time(path)) == _comparable(before)

    def test_racing_first_openers_install_once(self, tmp_path, monkeypatch):
        """Four openers forced to reach the install transaction together."""
        path = tmp_path / "m.db"
        barrier = threading.Barrier(4, timeout=10)
        install = MetricsStore._install_rollup_and_stamp
        reached: list[int] = []

        def _synchronised(db: sqlite3.Connection) -> None:
            reached.append(1)
            barrier.wait()
            install(db)

        monkeypatch.setattr(MetricsStore, "_install_rollup_and_stamp", staticmethod(_synchronised))

        def _init(_: int) -> None:
            _open(path).close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(_init, range(4)))
        assert len(reached) == 4, "every opener took the install path concurrently"
        db = sqlite3.connect(str(path))
        triggers = db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'"
        ).fetchone()[0]
        markers = db.execute(
            "SELECT COUNT(*) FROM metrics_meta WHERE key = 'rollup_installed_at'"
        ).fetchone()[0]
        db.close()
        assert (triggers, markers) == (1, 1)

    def test_install_under_a_held_write_lock_fails_fast_then_recovers(self, tmp_path):
        """The best-effort hook opener (250 ms busy timeout) must not stall on the
        install transaction, and must leave nothing half-built."""
        path = tmp_path / "m.db"
        _open(path).close()
        _strip_rollup(path)
        traced: list[str] = []
        real_connect = sqlite3.connect

        def _connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(traced.append)
            return conn

        blocker = real_connect(str(path), timeout=0.1)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("memtomem_stm.proxy.metrics_store.sqlite3.connect", _connect)
                start = time.monotonic()
                with pytest.raises(sqlite3.OperationalError):
                    MetricsStore(path, busy_timeout_ms=250, reconcile_on_init=False).initialize()
                elapsed = time.monotonic() - start
        finally:
            blocker.rollback()
            blocker.close()

        assert elapsed < 2.0
        assert ("table", _DAILY) not in _objects(path)
        _open(path, busy_timeout_ms=250, reconcile_on_init=False).close()
        assert ("trigger", _TRIGGER) in _objects(path)
        # The lock was met at the install transaction itself, not earlier: this
        # test exercises the new boundary, which a pre-installed DB would skip.
        assert traced[-1] == "BEGIN IMMEDIATE", traced[-3:]


class TestReader:
    def test_never_writes_and_reports_a_pre_rollup_db_as_unavailable(self, tmp_path):
        path = tmp_path / "m.db"
        _record_all(path, _mixed_metrics(), max_history=1_000_000)
        _strip_rollup(path)
        objects = _objects(path)
        mtime = path.stat().st_mtime_ns

        result = read_compression_all_time(path)

        assert result["available"] is False
        assert result["rollup"]["installed"] is False
        assert result["total_calls"] == 0
        assert _objects(path) == objects
        assert path.stat().st_mtime_ns == mtime
        # The retained reader is unaffected.
        assert read_compression_summary(path)["total_calls"] == 15

    @pytest.mark.parametrize("drop", ["trigger", "marker", "table"])
    def test_partial_installation_is_not_installed(self, tmp_path, drop):
        path = tmp_path / "m.db"
        _open(path).close()
        db = sqlite3.connect(str(path))
        if drop == "trigger":
            db.execute(f"DROP TRIGGER {_TRIGGER}")
        elif drop == "marker":
            db.execute("DELETE FROM metrics_meta WHERE key = 'rollup_installed_at'")
        else:
            db.execute(f"DROP TRIGGER {_TRIGGER}")
            db.execute(f"DROP TABLE {_DAILY}")
        db.commit()
        db.close()
        result = read_compression_all_time(path)
        assert (result["available"], result["rollup"]["installed"]) == (False, False)

    def test_missing_file_and_pre_is_error_db(self, tmp_path):
        missing = read_compression_all_time(tmp_path / "absent.db")
        assert (missing["available"], missing["rollup"]["installed"]) == (False, False)
        assert not (tmp_path / "absent.db").exists()

        path = tmp_path / "old.db"
        db = sqlite3.connect(str(path))
        db.execute(metrics_store_module._CREATE)
        db.commit()
        db.close()
        old = read_compression_all_time(path)
        assert old["schema_outdated"] is True
        assert (old["available"], old["rollup"]["installed"]) == (False, False)

    def test_lone_surrogate_filter_is_empty_but_available(self, tmp_path):
        path = tmp_path / "m.db"
        _record_all(path, _mixed_metrics(), max_history=1)
        result = read_compression_all_time(path, tool="bad\udcff")
        assert (result["available"], result["total_calls"]) == (True, 0)
        assert result["rollup"]["installed"] is True

    def test_rows_only_in_the_rollup_are_found_by_filters(self, tmp_path):
        path = tmp_path / "m.db"
        _record_all(path, _mixed_metrics(), max_history=1_000_000)
        before = read_compression_summary(path, tool="fetch")
        db = sqlite3.connect(str(path))
        db.execute("DELETE FROM proxy_metrics WHERE tool = 'fetch'")
        db.commit()
        db.close()
        assert read_compression_summary(path, tool="fetch")["total_calls"] == 0
        assert _comparable(read_compression_all_time(path, tool="fetch")) == _comparable(before)

    def test_all_time_by_tool_has_no_tuner_calls(self, tmp_path):
        path = tmp_path / "m.db"
        _record_all(path, _mixed_metrics(), max_history=1_000_000)
        assert all("tuner_calls" in r for r in read_compression_summary(path)["by_tool"])
        assert all("tuner_calls" not in r for r in read_compression_all_time(path)["by_tool"])


def test_documented_reset_order_empties_the_history(tmp_path):
    """docs/cli.md: delete ``proxy_metrics`` first, then the daily table. The
    other order refills the rollup from the rows being deleted."""
    for order, expected in (
        (("proxy_metrics", _DAILY), 0),
        ((_DAILY, "proxy_metrics"), 2),
    ):
        path = tmp_path / f"{order[0]}.db"
        _record_all(path, _mixed_metrics()[:2], max_history=1_000_000)
        db = sqlite3.connect(str(path))
        for table in order:
            db.execute(f"DELETE FROM {table}")
        db.commit()
        db.close()
        assert read_compression_all_time(path)["total_calls"] == expected, order


class TestCoverage:
    @staticmethod
    def _pre_install_db_with_loss(path: Path, installed_at: float) -> None:
        """A DB that lost rows before installation, installed at ``installed_at``."""
        _open(path).close()
        _strip_rollup(path)
        _insert_raw(path, created_at=installed_at - DAY)
        db = sqlite3.connect(str(path))
        db.execute("DELETE FROM proxy_metrics")
        db.commit()
        db.close()
        _open(path).close()
        db = sqlite3.connect(str(path))
        db.execute(
            "UPDATE metrics_meta SET value = ? WHERE key = 'rollup_installed_at'",
            (str(installed_at),),
        )
        db.commit()
        db.close()

    def test_warning_does_not_change_when_a_post_install_row_is_trimmed(self, tmp_path):
        """Install at noon, record at 13:00, trim that row. A coverage check that
        reconstructed the rolled-up row's time as midnight would start warning."""
        path = tmp_path / "m.db"
        noon = 1_798_200_000.0 - (1_798_200_000.0 % DAY) + DAY / 2
        self._pre_install_db_with_loss(path, noon)
        _insert_raw(path, created_at=noon + 3600, tool="late")

        live = read_compression_all_time(path)["rollup"]
        assert live["lost_before_install"] == 1, "positive control: there was loss"
        assert live["incomplete_before"] is None

        db = sqlite3.connect(str(path))
        db.execute("DELETE FROM proxy_metrics")
        db.commit()
        db.close()
        for kwargs in ({}, {"tool": "late"}):
            rolled = read_compression_all_time(path, **kwargs)["rollup"]
            assert rolled["incomplete_before"] is None, kwargs
            assert rolled["earliest_created_at"] == noon + 3600

    def test_same_day_pre_install_row_warns(self, tmp_path):
        """Morning row retained at a noon install: day-granular checks miss it."""
        path = tmp_path / "m.db"
        noon = 1_798_200_000.0 - (1_798_200_000.0 % DAY) + DAY / 2
        self._pre_install_db_with_loss(path, noon)
        _insert_raw(path, created_at=noon - 3600, tool="morning")
        rollup = read_compression_all_time(path, tool="morning")["rollup"]
        assert rollup["incomplete_before"] == noon
        assert read_compression_all_time(path, tool="other")["rollup"]["incomplete_before"] is None

    def test_no_loss_means_no_warning_even_for_old_rows(self, tmp_path):
        path = tmp_path / "m.db"
        _open(path).close()
        _insert_raw(path, created_at=1_000.0)
        rollup = read_compression_all_time(path)["rollup"]
        assert rollup["lost_before_install"] == 0
        assert rollup["incomplete_before"] is None
        assert rollup["earliest_created_at"] == 1_000.0


def _sql(path: Path, *statements: str) -> None:
    db = sqlite3.connect(str(path))
    try:
        for statement in statements:
            db.execute(statement)
        db.commit()
    finally:
        db.close()


class TestInt64RangeTotals:
    """Rows ``_finite`` admits must never take the summary down (#1348).

    ``SUM`` raised "integer overflow" as soon as two admitted rows added past
    int64, and the reader's ``except sqlite3.Error`` turned the whole summary
    into ``available: False``."""

    BIG = 5 * 10**18  # in range for one row; two of them are not

    def test_retained_and_all_time_totals_survive_an_int64_overflow(self, tmp_path):
        path = tmp_path / "m.db"
        _open(path).close()
        for _ in range(2):
            _insert_raw(path, created_at=time.time(), original_chars=self.BIG, compressed_chars=0)

        for reader in (read_compression_summary, read_compression_all_time):
            result = reader(path)
            assert result["available"] is True, (reader.__name__, result.get("error"))
            assert result["total_original_chars"] == 2 * self.BIG
            assert type(result["total_original_chars"]) is int
            assert type(result["total_calls"]) is int

    def test_sums_below_int64_stay_exact(self, tmp_path):
        """The float fallback is for overflow only: ``2**53 + 1`` is exact in
        ``SUM`` and not in ``TOTAL``."""
        path = tmp_path / "m.db"
        _open(path).close()
        _insert_raw(path, created_at=time.time(), original_chars=2**53, compressed_chars=0)
        _insert_raw(path, created_at=time.time(), original_chars=1, compressed_chars=0)

        for reader in (read_compression_summary, read_compression_all_time):
            assert reader(path)["total_original_chars"] == 2**53 + 1, reader.__name__

    def test_all_time_total_is_unchanged_when_the_rows_are_trimmed(self, tmp_path):
        """The trigger's ``+`` turns REAL past int64; the reader must not treat
        that legitimate bucket as damage and zero it."""
        path = tmp_path / "m.db"
        _open(path).close()
        for _ in range(2):
            _insert_raw(path, created_at=time.time(), original_chars=self.BIG, compressed_chars=0)
        before = read_compression_all_time(path)

        _sql(path, "DELETE FROM proxy_metrics")
        db = sqlite3.connect(str(path))
        bucket = db.execute(
            f"SELECT legacy_original_chars, typeof(legacy_original_chars) FROM {_DAILY}"
        ).fetchall()
        db.close()
        assert bucket == [(2.0 * self.BIG, "real")], "positive control: the bucket overflowed"

        after = read_compression_all_time(path)
        assert after["available"] is True, after.get("error")
        assert after["total_original_chars"] == before["total_original_chars"] == 2 * self.BIG
        assert after["total_calls"] == before["total_calls"] == 2

    @pytest.mark.parametrize("damage", ["'not a number'", "9e999", "-9e999", "NULL"])
    def test_a_damaged_daily_counter_contributes_zero(self, tmp_path, damage):
        path = tmp_path / "m.db"
        _open(path).close()
        _insert_raw(path, created_at=time.time())
        _insert_raw(path, created_at=time.time())
        _sql(path, "DELETE FROM proxy_metrics WHERE id = 1")
        _sql(path, f"UPDATE {_DAILY} SET legacy_original_chars = {damage}")

        result = read_compression_all_time(path)

        assert result["available"] is True, result.get("error")
        assert result["total_original_chars"] == 100  # the retained row only
        assert result["total_calls"] == 2

    def test_fractional_sizes_keep_their_fraction(self, tmp_path):
        """Only an outside write stores a fractional size; the reader reports it
        as ``SUM`` did rather than truncating it into a different ratio."""
        path = tmp_path / "m.db"
        _open(path).close()
        _insert_raw(path, created_at=time.time(), original_chars=1.5, compressed_chars=0.5)

        result = read_compression_summary(path)

        assert result["total_original_chars"] == 1.5
        assert result["total_compressed_chars"] == 0.5
        assert result["saved_ratio"] == pytest.approx(2 / 3, abs=1e-4)


class TestMarkerRepair:
    """Erased rollup markers must not be rebuilt into a fabricated loss (#1348)."""

    def _running_rollup(self, path: Path) -> dict[str, str]:
        """Five rows, three trimmed AFTER installation: nothing was ever lost."""
        _open(path).close()
        for i in range(5):
            _insert_raw(path, created_at=1_000_000.0 + i)
        _sql(path, "DELETE FROM proxy_metrics WHERE id <= 3")
        rollup = read_compression_all_time(path)["rollup"]
        assert rollup["lost_before_install"] == 0, "positive control"
        assert rollup["incomplete_before"] is None, "positive control"
        return _meta(path)

    def test_documented_repair_keeps_the_markers(self, tmp_path):
        path = tmp_path / "m.db"
        first = self._running_rollup(path)

        _sql(path, "DELETE FROM metrics_meta WHERE key = 'schema_fingerprint'")
        _open(path).close()

        after = _meta(path)
        assert after["rollup_installed_at"] == first["rollup_installed_at"]
        assert after["rollup_rows_lost_before_install"] == "0"
        assert after["schema_fingerprint"] == first["schema_fingerprint"]

    @pytest.mark.parametrize("reset_first", [False, True])
    def test_erased_markers_on_a_running_rollup_record_an_unknown_loss(self, tmp_path, reset_first):
        """``seq - COUNT(*)`` would call the three rolled-up rows lost — and after
        a documented history reset, every row ever written."""
        path = tmp_path / "m.db"
        self._running_rollup(path)
        if reset_first:
            _sql(path, "DELETE FROM proxy_metrics", f"DELETE FROM {_DAILY}")
            _insert_raw(path, created_at=1_000_100.0)

        _sql(path, "DELETE FROM metrics_meta")
        _open(path).close()

        meta = _meta(path)
        assert meta["rollup_rows_lost_before_install"] == "unknown"
        rollup = read_compression_all_time(path)["rollup"]
        assert rollup["installed"] is True
        assert rollup["lost_before_install"] is None

    def test_first_install_still_counts_the_rows_already_trimmed(self, tmp_path):
        """Control for the rule above: no daily table before the transaction is a
        first installation, and its count is exact."""
        path = tmp_path / "m.db"
        _open(path).close()
        _strip_rollup(path)
        for i in range(6):
            _insert_raw(path, created_at=1_000_000.0 + i)
        _sql(path, "DELETE FROM proxy_metrics WHERE id <= 2")

        _open(path).close()

        assert _meta(path)["rollup_rows_lost_before_install"] == "2"
