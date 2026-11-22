"""Response-cache counts persisted per UTC day, server and tool (#1329).

``TokenTracker``'s cache counters live in memory; ``mms stats`` runs in another
process and cannot see them. The tracker now also buffers attributed deltas and
hands them to the metrics row write that the next recorded call already makes,
plus once at shutdown, so the hit path itself never touches SQLite. Pinned here:

- the buffered deltas reach ``proxy_cache_daily`` exactly once per successful
  commit, and are kept for the next write when one fails;
- a cache failure never costs the metrics row it rides with;
- concurrent stores on one file add, never overwrite;
- the hit path makes no store call;
- the reader is read-only and never invents a coverage start.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from memtomem_stm.proxy import metrics as metrics_module
from memtomem_stm.proxy import metrics_store as metrics_store_module
from memtomem_stm.proxy.metrics import CallMetrics, TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore, read_cache_summary

_TABLE = "proxy_cache_daily"
_MARKER = "cache_rollup_installed_at"


def _open(path: Path) -> MetricsStore:
    store = MetricsStore(path, max_history=1_000_000)
    store.initialize()
    return store


def _rows(path: Path) -> list[tuple[Any, ...]]:
    db = sqlite3.connect(path)
    try:
        return db.execute(
            f"SELECT day, server, tool, hits, hit_chars, misses, unstorable FROM {_TABLE} "
            "ORDER BY day, server, tool"
        ).fetchall()
    finally:
        db.close()


def _metrics_rows(path: Path) -> int:
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT COUNT(*) FROM proxy_metrics").fetchone()[0]
    finally:
        db.close()


def _seeded(tmp_path: Path) -> Path:
    """A metrics DB whose cache table holds one valid bucket: one hit."""
    path = tmp_path / "m.db"
    store = _open(path)
    store.record_cache_counts({("2026-11-21", "s", "t"): [1, 0, 0, 0]})
    store.close()
    return path


def _insert(path: Path, *rows: tuple[str, ...]) -> None:
    """Write cache rows the way an outside writer could: each ``row`` is the
    SQL for ``server, tool, hits, hit_chars, misses, unstorable``."""
    db = sqlite3.connect(path)
    try:
        for row in rows:
            db.execute(f"INSERT INTO {_TABLE} VALUES ('2026-11-21', {', '.join(row)})")
        db.commit()
    finally:
        db.close()


def _call(server: str = "docs", tool: str = "search") -> CallMetrics:
    return CallMetrics(server=server, tool=tool, original_chars=100, compressed_chars=40)


_FROZEN_DAY = "2026-11-21"


def _today() -> str:
    """The day every cache event in these tests is counted under."""
    return _FROZEN_DAY


@pytest.fixture(autouse=True)
def _frozen_cache_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the event day so a run crossing UTC midnight cannot split a
    bucket; the rollover test restores the real clock itself."""
    monkeypatch.setattr(metrics_module, "_utc_day", lambda: _FROZEN_DAY)


_REAL_UTC_DAY = metrics_module._utc_day


class TestTrackerBuffer:
    def test_next_recorded_call_writes_the_attributed_counts(self, tmp_path: Path) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=120, server="docs", tool="search")
        tracker.record_cache_hit(chars=30, server="docs", tool="search")
        tracker.record_cache_miss(server="docs", tool="search")
        tracker.record_cache_miss(server="api", tool="get")
        tracker.record_cache_unstorable(server="api", tool="get")

        assert _rows(path) == []  # buffered, nothing written yet
        tracker.record(_call())
        store.close()

        assert _rows(path) == [
            (_today(), "api", "get", 0, 0, 1, 1),
            (_today(), "docs", "search", 2, 150, 1, 0),
        ]
        # The in-memory totals are unchanged by the persistence.
        summary = tracker.get_summary()
        assert (summary["cache_hits"], summary["cache_hit_chars"]) == (2, 150)
        assert (summary["cache_misses"], summary["cache_unstorable"]) == (2, 1)

    def test_written_once_then_buffer_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=5, server="s", tool="t")
        tracker.record(_call())
        tracker.record(_call())
        tracker.record_error(
            CallMetrics(server="s", tool="t", original_chars=0, compressed_chars=0, is_error=True)
        )
        tracker.flush_cache_counts()
        store.close()
        assert _rows(path) == [(_today(), "s", "t", 1, 5, 0, 0)]

    def test_record_error_also_carries_the_counts(self, tmp_path: Path) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_miss(server="s", tool="t")
        tracker.record_error(
            CallMetrics(server="s", tool="t", original_chars=0, compressed_chars=0, is_error=True)
        )
        store.close()
        assert _rows(path) == [(_today(), "s", "t", 0, 0, 1, 0)]

    def test_hit_path_makes_no_store_call(self) -> None:
        calls: list[str] = []

        class SpyStore:
            def __getattr__(self, name: str):
                calls.append(name)
                raise AssertionError(f"store touched on the hit path: {name}")

        tracker = TokenTracker(metrics_store=SpyStore())  # type: ignore[arg-type]
        for _ in range(50):
            tracker.record_cache_hit(chars=10, server="s", tool="t")
            tracker.record_cache_miss(server="s", tool="t")
            tracker.record_cache_unstorable(server="s", tool="t")
        assert calls == []

    def test_unattributed_calls_stay_in_memory(self, tmp_path: Path) -> None:
        """The pre-#1329 call shapes keep working and count live only."""
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(10)
        tracker.record_cache_miss()
        tracker.record_cache_unstorable()
        tracker.record(_call())
        tracker.flush_cache_counts()
        store.close()
        assert _rows(path) == []
        assert tracker.get_summary()["cache_hits"] == 1

    def test_utc_day_boundary_splits_buckets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        midnight = 1_800_000_000 - (1_800_000_000 % 86400)  # a UTC midnight
        clock = iter([midnight - 1.0, midnight + 1.0])
        monkeypatch.setattr(metrics_module, "_utc_day", _REAL_UTC_DAY)
        monkeypatch.setattr(metrics_module._time, "time", lambda: next(clock))
        tracker.record_cache_hit(chars=1, server="s", tool="t")
        tracker.record_cache_hit(chars=2, server="s", tool="t")
        monkeypatch.undo()
        tracker.flush_cache_counts()
        store.close()
        before = time.strftime("%Y-%m-%d", time.gmtime(midnight - 1))
        after = time.strftime("%Y-%m-%d", time.gmtime(midnight + 1))
        assert _rows(path) == [(before, "s", "t", 1, 1, 0, 0), (after, "s", "t", 1, 2, 0, 0)]

    def test_unencodable_label_is_skipped_beside_a_valid_one(self, tmp_path: Path) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=7, server="s", tool="bad\udcff")
        tracker.record_cache_hit(chars=3, server="s", tool="t")
        tracker.flush_cache_counts()
        store.close()
        assert _rows(path) == [(_today(), "s", "t", 1, 3, 0, 0)]
        assert tracker.get_summary()["cache_hits"] == 2  # live count keeps both

    def test_bucket_cap_drops_new_buckets_with_one_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(metrics_module, "MAX_CACHE_BUCKETS", 2)
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        with caplog.at_level("WARNING"):
            for tool in ("a", "b", "c", "d"):
                tracker.record_cache_hit(chars=1, server="s", tool=tool)
            tracker.record_cache_hit(chars=1, server="s", tool="a")  # existing bucket
        tracker.flush_cache_counts()
        store.close()
        assert [row[2] for row in _rows(path)] == ["a", "b"]
        assert _rows(path)[0][3] == 2
        assert sum("cache count" in r.getMessage() for r in caplog.records) == 1

    def test_no_store_buffers_nothing(self) -> None:
        tracker = TokenTracker()
        tracker.record_cache_hit(chars=1, server="s", tool="t")
        assert tracker._cache_deltas == {}
        tracker.flush_cache_counts()  # no-op, no error


class TestFailureAndRetry:
    def test_cache_failure_keeps_the_metrics_row_and_retries_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=4, server="s", tool="a")
        tracker.record_cache_miss(server="s", tool="b")

        seen: list[int] = []
        original = MetricsStore._upsert_cache_counts

        def failing(self: MetricsStore, db: sqlite3.Connection, deltas: Any) -> None:
            # The first UPSERT lands, then the write fails on the second.
            seen.append(len(deltas))
            first_key, first_counts = next(iter(deltas.items()))
            db.execute(metrics_store_module._CACHE_UPSERT, (*first_key, *first_counts))
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(MetricsStore, "_upsert_cache_counts", failing)
        tracker.record(_call())
        assert _metrics_rows(path) == 1  # the metrics row survived
        assert _rows(path) == []  # the partial cache write was rolled back

        monkeypatch.setattr(MetricsStore, "_upsert_cache_counts", original)
        tracker.record(_call())
        tracker.record(_call())
        store.close()
        assert seen == [2]
        assert _metrics_rows(path) == 3
        assert _rows(path) == [
            (_today(), "s", "a", 1, 4, 0, 0),
            (_today(), "s", "b", 0, 0, 1, 0),
        ]

    def test_metrics_insert_failure_keeps_the_counts(self, tmp_path: Path) -> None:
        """A row the store refuses (an unencodable server) raises before any
        write; the cache counts must survive for the next call."""
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=9, server="s", tool="t")
        tracker.record(_call(server="bad\udcff"))
        tracker.record(_call())
        store.close()
        assert _rows(path) == [(_today(), "s", "t", 1, 9, 0, 0)]

    def test_shutdown_flush_failure_is_logged_not_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = _open(tmp_path / "m.db")
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=1, server="s", tool="t")
        store.close()  # the connection is gone; record_cache_counts cannot write
        with caplog.at_level("WARNING"):
            tracker.flush_cache_counts()
        assert tracker._cache_deltas  # kept, not silently dropped
        assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
            "Failed to persist cache counts"
        ]

    def test_record_on_a_closed_store_keeps_the_counts_for_later(self, tmp_path: Path) -> None:
        """``record()`` returns False without writing when the store is
        closed; the tracker keeps the counts, and they land once later."""
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=4, server="s", tool="t")
        store.close()

        tracker.record(_call())  # record() -> False: nothing written

        assert tracker._cache_deltas == {(_today(), "s", "t"): [1, 4, 0, 0]}
        store.initialize()
        tracker.record(_call())
        tracker.record(_call())
        store.close()
        assert _rows(path) == [(_today(), "s", "t", 1, 4, 0, 0)]
        assert _metrics_rows(path) == 2


class TestConcurrency:
    def test_two_stores_on_one_file_add(self, tmp_path: Path) -> None:
        path = tmp_path / "m.db"
        a, b = _open(path), _open(path)
        ta, tb = TokenTracker(metrics_store=a), TokenTracker(metrics_store=b)
        for _ in range(3):
            ta.record_cache_hit(chars=10, server="s", tool="t")
            tb.record_cache_hit(chars=1, server="s", tool="t")
            ta.record(_call())
            tb.record(_call())
        a.close()
        b.close()
        assert _rows(path) == [(_today(), "s", "t", 6, 33, 0, 0)]

    def test_overlapping_writers_on_separate_connections_add(self, tmp_path: Path) -> None:
        """Writers released together by a barrier: a read-modify-write would
        lose increments here; the UPSERT adds inside SQLite."""
        import threading

        path = tmp_path / "m.db"
        _open(path).close()
        writers, rounds = 4, 25
        barrier = threading.Barrier(writers, timeout=10)
        errors: list[BaseException] = []

        def write() -> None:
            store = _open(path)
            try:
                barrier.wait()
                for _ in range(rounds):
                    store.record_cache_counts({("2026-11-21", "s", "t"): [1, 2, 0, 0]})
            except BaseException as exc:  # surfaced below
                errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=write) for _ in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert errors == []
        assert _rows(path) == [("2026-11-21", "s", "t", 100, 200, 0, 0)]


class _CommitFailsOnce:
    """A connection whose next ``commit`` raises; everything else delegates."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.armed = True

    def commit(self) -> None:
        if self.armed:
            self.armed = False
            raise sqlite3.OperationalError("disk I/O error (injected at commit)")
        self._conn.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class TestFailureAfterTheSavepoint:
    """The cache UPSERTs succeeded and their savepoint was released; a later
    failure in the same transaction must roll back both writes, and the counts
    must land exactly once on the next write."""

    def _assert_nothing_visible_then_retry_once(self, path: Path, tracker: TokenTracker) -> None:
        assert _metrics_rows(path) == 0  # read through a second connection
        assert _rows(path) == []
        tracker.record(_call())
        tracker.record(_call())
        assert _rows(path) == [(_today(), "s", "t", 1, 7, 0, 0)]

    def test_trim_failure_rolls_back_both_and_retries_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=7, server="s", tool="t")

        def failing_trim(db: Any, source: Any) -> None:
            raise sqlite3.OperationalError("injected trim failure")

        monkeypatch.setattr(store, "_should_trim", lambda source, rowid: True)
        monkeypatch.setattr(store, "_trim_statement", failing_trim)
        tracker.record(_call())
        monkeypatch.undo()
        monkeypatch.setattr(metrics_module, "_utc_day", lambda: _FROZEN_DAY)

        self._assert_nothing_visible_then_retry_once(path, tracker)
        store.close()

    def test_commit_failure_rolls_back_both_and_retries_once(self, tmp_path: Path) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=7, server="s", tool="t")
        assert store._db is not None
        store._db = _CommitFailsOnce(store._db)  # type: ignore[assignment]

        tracker.record(_call())

        self._assert_nothing_visible_then_retry_once(path, tracker)
        store.close()

    def test_sqlite_rollback_inside_the_savepoint_reports_the_real_error(
        self, tmp_path: Path
    ) -> None:
        """A RAISE(ROLLBACK) ends the whole transaction, savepoint included;
        the caller must see that error, not "no such savepoint"."""
        path = tmp_path / "m.db"
        store = _open(path)
        db = sqlite3.connect(path)
        db.execute(
            f"CREATE TRIGGER cache_rollback BEFORE INSERT ON {_TABLE} "
            "BEGIN SELECT RAISE(ROLLBACK, 'cache failure'); END"
        )
        db.commit()
        db.close()

        with pytest.raises(sqlite3.IntegrityError, match="cache failure"):
            store.record(_call(), cache_deltas={(_today(), "s", "t"): [1, 7, 0, 0]})

        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=7, server="s", tool="t")
        tracker.record(_call())  # swallowed by the tracker, counts kept
        assert tracker._cache_deltas == {(_today(), "s", "t"): [1, 7, 0, 0]}
        db = sqlite3.connect(path)
        db.execute("DROP TRIGGER cache_rollback")
        db.commit()
        db.close()
        self._assert_nothing_visible_then_retry_once(path, tracker)
        store.close()


class TestInsertFailureDoesNotLeak:
    def test_failed_metrics_insert_is_not_committed_by_a_later_flush(self, tmp_path: Path) -> None:
        """An AFTER INSERT trigger raising FAIL aborts the statement but keeps
        its row in the open transaction; the next commit, here the shutdown
        flush, must not publish it."""
        path = tmp_path / "m.db"
        store = _open(path)
        db = sqlite3.connect(path)
        db.execute(
            "CREATE TRIGGER fail_insert AFTER INSERT ON proxy_metrics "
            "BEGIN SELECT RAISE(FAIL, 'injected'); END"
        )
        db.commit()
        db.close()
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=2, server="s", tool="t")

        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            store.record(_call())
        tracker.flush_cache_counts()
        store.close()

        assert _metrics_rows(path) == 0
        assert _rows(path) == [(_today(), "s", "t", 1, 2, 0, 0)]


class TestInstallAndReader:
    def test_install_creates_the_table_and_marker(self, tmp_path: Path) -> None:
        path = tmp_path / "m.db"
        before = time.time()
        _open(path).close()
        summary = read_cache_summary(path)
        assert summary["available"] is True
        assert summary["installed_at"] is not None
        assert before - 1 <= summary["installed_at"] <= time.time() + 1
        assert (summary["hits"], summary["misses"]) == (0, 0)
        assert summary["storable_hit_rate"] is None

    def test_reader_on_a_db_without_the_table_is_unavailable_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        store.close()
        db = sqlite3.connect(path)
        db.execute(f"DROP TABLE {_TABLE}")
        db.commit()
        db.close()
        mtime = path.stat().st_mtime_ns
        summary = read_cache_summary(path)
        assert summary["available"] is False
        assert path.stat().st_mtime_ns == mtime
        db = sqlite3.connect(path)
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_TABLE,)
            ).fetchone()
            is None
        )
        db.close()

    def test_missing_file_is_unavailable(self, tmp_path: Path) -> None:
        summary = read_cache_summary(tmp_path / "nope.db")
        assert summary["available"] is False
        assert not (tmp_path / "nope.db").exists()

    def test_marker_is_not_recreated_for_an_existing_table(self, tmp_path: Path) -> None:
        """A populated table whose marker was erased keeps an unknown start:
        writing "now" would claim coverage that began later than it did."""
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=3, server="s", tool="t")
        tracker.flush_cache_counts()
        store.close()
        db = sqlite3.connect(path)
        db.execute("DELETE FROM metrics_meta WHERE key = ?", (_MARKER,))
        db.execute("UPDATE metrics_meta SET value = 'stale' WHERE key = 'schema_fingerprint'")
        db.commit()
        db.close()

        _open(path).close()  # stale stamp → slow path runs the install again

        summary = read_cache_summary(path)
        assert summary["available"] is True
        assert summary["installed_at"] is None
        assert summary["hits"] == 1

    def test_unparseable_marker_reads_as_unknown(self, tmp_path: Path) -> None:
        path = tmp_path / "m.db"
        _open(path).close()
        db = sqlite3.connect(path)
        db.execute("UPDATE metrics_meta SET value = 'garbage' WHERE key = ?", (_MARKER,))
        db.commit()
        db.close()
        assert read_cache_summary(path)["installed_at"] is None

    def test_marker_that_is_not_utf8_leaves_the_counters_readable(self, tmp_path: Path) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        store.record_cache_counts({("2026-11-21", "s", "t"): [3, 30, 1, 0]})
        store.close()
        db = sqlite3.connect(path)
        db.execute(
            "UPDATE metrics_meta SET value = CAST(X'FFFE31' AS TEXT) WHERE key = ?", (_MARKER,)
        )
        db.commit()
        db.close()
        summary = read_cache_summary(path)
        assert summary["available"] is True
        assert summary["installed_at"] is None
        assert summary["hits"] == 3

    def test_restamp_keeps_rows_and_first_marker(self, tmp_path: Path) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=3, server="s", tool="t")
        tracker.flush_cache_counts()
        store.close()
        first = read_cache_summary(path)["installed_at"]
        db = sqlite3.connect(path)
        db.execute("UPDATE metrics_meta SET value = 'older' WHERE key = 'schema_fingerprint'")
        db.commit()
        db.close()
        _open(path).close()
        summary = read_cache_summary(path)
        assert summary["installed_at"] == first
        assert summary["hits"] == 1

    @pytest.mark.parametrize(
        ("hits", "misses", "unstorable", "rate"),
        [
            (0, 0, 0, None),
            (3, 1, 0, 0.75),
            (1, 0, 1, 1.0),  # unstorable > misses clamps the storable misses to 0
            (0, 2, 2, None),  # no storable lookups at all
            (2, 4, 2, 0.5),
        ],
    )
    def test_storable_hit_rate_uses_the_live_formula(
        self, tmp_path: Path, hits: int, misses: int, unstorable: int, rate: float | None
    ) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        for _ in range(hits):
            tracker.record_cache_hit(chars=1, server="s", tool="t")
        for _ in range(misses):
            tracker.record_cache_miss(server="s", tool="t")
        for _ in range(unstorable):
            tracker.record_cache_unstorable(server="s", tool="t")
        tracker.flush_cache_counts()
        store.close()
        summary = read_cache_summary(path)
        assert summary["storable_hit_rate"] == rate

    def test_counts_past_2_53_stay_exact(self, tmp_path: Path) -> None:
        """A float sum would round 2**53 + 1 misses down to 2**53 and report
        a 100% rate; the exact integer sum gives 1 storable miss, 50%."""
        path = tmp_path / "m.db"
        store = _open(path)
        store.record_cache_counts(
            {("2026-11-21", "s", "t"): [1, 0, 9_007_199_254_740_993, 9_007_199_254_740_992]}
        )
        store.close()
        summary = read_cache_summary(path)
        assert summary["misses"] == 9_007_199_254_740_993
        assert summary["unstorable"] == 9_007_199_254_740_992
        assert summary["storable_hit_rate"] == 0.5

    def test_sum_past_int64_stays_exact(self, tmp_path: Path) -> None:
        """Two in-range buckets adding past int64. 18e18 itself is exactly a
        float, so the ``+ 1`` is what a float sum would lose."""
        path = tmp_path / "m.db"
        store = _open(path)
        store.record_cache_counts(
            {
                ("2026-11-21", "s", "a"): [9_000_000_000_000_000_000, 0, 0, 0],
                ("2026-11-21", "s", "b"): [9_000_000_000_000_000_001, 0, 0, 0],
            }
        )
        store.close()
        summary = read_cache_summary(path)
        assert summary["available"] is True
        assert summary["error"] is None
        assert summary["hits"] == 18_000_000_000_000_000_001
        assert type(summary["hits"]) is int

    def test_one_overflowing_counter_leaves_the_others_exact(self, tmp_path: Path) -> None:
        """``hit_chars`` adding past int64 must not turn the lookup counts into
        floats: 2**53 + 1 misses would round to 2**53 and the rate to 1.0."""
        path = tmp_path / "m.db"
        store = _open(path)
        store.record_cache_counts(
            {
                ("2026-11-21", "s", "a"): [1, 9_000_000_000_000_000_000, 2**53 + 1, 2**53],
                ("2026-11-21", "s", "b"): [0, 9_000_000_000_000_000_000, 0, 0],
            }
        )
        store.close()
        summary = read_cache_summary(path)
        assert summary["hit_chars"] == 18_000_000_000_000_000_000
        assert (summary["misses"], summary["unstorable"]) == (2**53 + 1, 2**53)
        assert summary["storable_hit_rate"] == 0.5

    def test_huge_real_buckets_add_to_a_finite_count(self, tmp_path: Path) -> None:
        """Two outside-written ``1e308`` buckets: a float total is ``inf``;
        whole-number cells add as ints, so the block stays readable."""
        path = _seeded(tmp_path)
        _insert(
            path, ("'s'", "'a'", "1e308", "0", "0", "0"), ("'s'", "'b'", "1e308", "0", "0", "0")
        )
        summary = read_cache_summary(path)
        assert summary["available"] is True
        assert summary["error"] is None
        assert summary["hits"] == 2 * int(1e308) + 1
        assert summary["storable_hit_rate"] == 1.0

    def test_real_past_int64_and_an_int_add_exactly(self, tmp_path: Path) -> None:
        path = _seeded(tmp_path)
        _insert(path, ("'s'", "'a'", "9223372036854775808.0", "0", "0", "0"))
        summary = read_cache_summary(path)
        assert summary["hits"] == 2**63 + 1

    @pytest.mark.parametrize("cell", ["-1", "1.5", "-0.0"])
    def test_negative_or_fractional_cell_counts_zero(self, tmp_path: Path, cell: str) -> None:
        path = _seeded(tmp_path)
        _insert(path, ("'s'", "'a'", cell, "0", "0", "0"))
        summary = read_cache_summary(path)
        assert summary["hits"] == 1
        assert type(summary["hits"]) is int

    def test_invalid_utf8_text_cell_leaves_the_counters_readable(self, tmp_path: Path) -> None:
        path = _seeded(tmp_path)
        _insert(path, ("'s'", "'a'", "CAST(X'ff' AS TEXT)", "0", "0", "0"))
        summary = read_cache_summary(path)
        assert summary["available"] is True
        assert summary["error"] is None
        assert summary["hits"] == 1

    @pytest.mark.parametrize("cell", ["NULL", "'many'", "X'00'", "9e999", "-9e999"])
    def test_non_numeric_or_infinite_cell_counts_zero(self, tmp_path: Path, cell: str) -> None:
        path = _seeded(tmp_path)
        _insert(path, ("'s'", "'a'", cell, "0", "0", "0"))
        summary = read_cache_summary(path)
        assert summary["available"] is True
        assert summary["hits"] == 1

    def test_tool_filter(self, tmp_path: Path) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=5, server="a", tool="search")
        tracker.record_cache_hit(chars=7, server="b", tool="search")
        tracker.record_cache_hit(chars=9, server="a", tool="fetch")
        tracker.flush_cache_counts()
        store.close()
        summary = read_cache_summary(path, tool="search")
        assert (summary["hits"], summary["hit_chars"]) == (2, 12)
        assert read_cache_summary(path, tool="bad\udcff")["hits"] == 0


class TestShutdownFlush:
    async def test_lifespan_exit_writes_the_buffered_counts(self, tmp_path: Path) -> None:
        """Counts buffered after the last recorded call reach disk when the
        server stops, before the store closes."""
        from unittest.mock import AsyncMock, MagicMock

        db_path = tmp_path / "m.db"
        manager = MagicMock()
        manager.start = AsyncMock()
        manager.stop = AsyncMock()
        manager.get_proxy_tools.return_value = []

        async def body(ctx: Any) -> None:
            ctx.tracker.record_cache_hit(chars=5, server="s", tool="t")
            assert _rows(db_path) == []

        await _run_lifespan(tmp_path, manager, body)
        assert _rows(db_path) == [(_today(), "s", "t", 1, 5, 0, 0)]

    async def test_counts_reach_disk_before_a_cancelled_stop(self, tmp_path: Path) -> None:
        """The SDK's cancelled scope can cut teardown short at its first
        await (#1458); the counts buffered by then are already written."""
        from unittest.mock import AsyncMock, MagicMock

        db_path = tmp_path / "m.db"
        manager = MagicMock()
        manager.start = AsyncMock()
        manager.stop = AsyncMock(side_effect=asyncio.CancelledError())
        manager.get_proxy_tools.return_value = []

        async def body(ctx: Any) -> None:
            ctx.tracker.record_cache_hit(chars=5, server="s", tool="t")

        with pytest.raises(asyncio.CancelledError):
            await _run_lifespan(tmp_path, manager, body)
        assert _rows(db_path) == [(_today(), "s", "t", 1, 5, 0, 0)]

    async def test_a_locked_db_does_not_delay_the_stop(self, tmp_path: Path) -> None:
        """The early teardown flush has the short lock budget: with another
        connection holding the write lock, the manager's stop starts well
        within the store's 3 s busy timeout, and the final flush writes the
        counts once the lock is gone."""
        from unittest.mock import AsyncMock, MagicMock

        db_path = tmp_path / "m.db"
        holder: list[sqlite3.Connection] = []
        timing: dict[str, float] = {}

        async def stop() -> None:
            timing["stop"] = time.monotonic()
            holder[0].rollback()  # the other writer finishes
            holder[0].close()

        manager = MagicMock()
        manager.start = AsyncMock()
        manager.stop = AsyncMock(side_effect=stop)
        manager.get_proxy_tools.return_value = []

        async def body(ctx: Any) -> None:
            ctx.tracker.record_cache_hit(chars=5, server="s", tool="t")
            other = sqlite3.connect(db_path)
            other.execute("BEGIN IMMEDIATE")
            holder.append(other)
            timing["exit"] = time.monotonic()

        await _run_lifespan(tmp_path, manager, body)
        assert timing["stop"] - timing["exit"] < 1.0
        assert _rows(db_path) == [(_today(), "s", "t", 1, 5, 0, 0)]


async def _run_lifespan(tmp_path: Path, manager: Any, body: Any) -> None:
    """Drive ``app_lifespan`` with a real metrics store at ``tmp_path/m.db``
    and ``manager`` as the proxy manager; ``body(ctx)`` runs inside it."""
    from unittest.mock import MagicMock, patch

    from memtomem_stm.server import app_lifespan, mcp

    db_path = tmp_path / "m.db"
    with (
        patch("memtomem_stm.server.STMConfig") as config_cls,
        patch("memtomem_stm.server.ProxyManager", return_value=manager),
    ):
        cfg = config_cls.return_value
        cfg.proxy = MagicMock()
        cfg.proxy.enabled = True
        cfg.proxy.config_path = tmp_path / "proxy.json"
        cfg.proxy.metrics.enabled = True
        cfg.proxy.metrics.db_path = db_path
        cfg.proxy.metrics.max_history = 1000
        cfg.proxy.compression_feedback.enabled = False
        cfg.proxy.progressive_reads.enabled = False
        cfg.proxy.selection_telemetry.enabled = False
        cfg.proxy.cache.enabled = False
        cfg.surfacing = MagicMock()
        cfg.surfacing.enabled = False
        cfg.langfuse = MagicMock()
        cfg.langfuse.enabled = False
        cfg.otlp = MagicMock()
        cfg.otlp.enabled = False
        cfg.parent_liveness_poll_seconds = 0.0
        cfg.parent_liveness_grace_seconds = 900.0

        async with app_lifespan(mcp) as ctx:
            await body(ctx)


class TestPeriodicFlush:
    """The timer writes counts no metrics write has carried, so a proxy that
    only serves cache hits still reaches ``mms stats`` while it runs."""

    async def test_counts_become_visible_to_another_reader_while_running(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=5, server="s", tool="t")
        task = asyncio.create_task(tracker.run_cache_flush_loop(0.01))
        try:
            deadline = time.monotonic() + 5.0
            while read_cache_summary(path)["hits"] != 1:
                assert time.monotonic() < deadline, "the periodic flush never wrote"
                await asyncio.sleep(0.01)
            assert not task.done()
            assert tracker._cache_deltas == {}
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            store.close()

    def test_a_locked_db_fails_fast_and_keeps_the_counts(self, tmp_path: Path) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        assert store._db is not None
        before = store._db.execute("PRAGMA busy_timeout").fetchone()[0]
        assert before >= 1000  # the full budget the timer must not inherit
        tracker = TokenTracker(metrics_store=store)
        tracker.record_cache_hit(chars=5, server="s", tool="t")
        other = sqlite3.connect(path)
        other.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            tracker.flush_cache_counts(periodic=True)
            assert time.monotonic() - started < 1.0
            assert tracker._cache_deltas == {(_today(), "s", "t"): [1, 5, 0, 0]}
            assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] == before
        finally:
            other.rollback()
            other.close()
        tracker.flush_cache_counts(periodic=True)
        # Not merely "under 1 s": the timer's connection has the short budget.
        assert store._fast_db is not None
        assert (
            store._fast_db.execute("PRAGMA busy_timeout").fetchone()[0]
            == metrics_module.CACHE_FLUSH_BUSY_TIMEOUT_MS
        )
        store.close()
        assert _rows(path) == [(_today(), "s", "t", 1, 5, 0, 0)]

    def test_periodic_failures_warn_once_per_streak(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        tracker = TokenTracker(metrics_store=store)

        def warnings() -> int:
            return sum(1 for r in caplog.records if r.levelname == "WARNING")

        with caplog.at_level("WARNING"):
            tracker.record_cache_hit(chars=1, server="s", tool="t")
            store.close()
            tracker.flush_cache_counts(periodic=True)
            tracker.flush_cache_counts(periodic=True)
            assert warnings() == 1

            # Recovery through the timer re-arms the warning.
            store.initialize()
            tracker.flush_cache_counts(periodic=True)
            tracker.record_cache_hit(chars=1, server="s", tool="t")
            store.close()
            tracker.flush_cache_counts(periodic=True)
            assert warnings() == 2

            # So does recovery through a per-call metrics write.
            store.initialize()
            tracker.record(_call())
            tracker.record_cache_hit(chars=1, server="s", tool="t")
            store.close()
            tracker.flush_cache_counts(periodic=True)
            assert warnings() == 3
        assert _rows(path) == [(_today(), "s", "t", 2, 2, 0, 0)]

    async def test_manager_runs_the_timer_between_start_and_stop(self, tmp_path: Path) -> None:
        from memtomem_stm.proxy.config import ProxyConfig
        from memtomem_stm.proxy.manager import ProxyManager

        store = _open(tmp_path / "m.db")
        manager = ProxyManager(
            ProxyConfig(config_path=tmp_path / "proxy.json"), TokenTracker(metrics_store=store)
        )
        await manager.start()
        task = manager._cache_flush_task
        assert task is not None and not task.done()
        await manager.stop()
        assert manager._cache_flush_task is None
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        store.close()

    async def test_no_timer_without_a_metrics_store(self, tmp_path: Path) -> None:
        from memtomem_stm.proxy.config import ProxyConfig
        from memtomem_stm.proxy.manager import ProxyManager

        manager = ProxyManager(ProxyConfig(config_path=tmp_path / "proxy.json"), TokenTracker())
        await manager.start()
        assert manager._cache_flush_task is None
        await manager.stop()

    async def test_stop_ends_the_timer_before_its_first_await(self, tmp_path: Path) -> None:
        """A teardown cancelled at ``stop()``'s first await has still ended
        the timer, so it cannot write into a store that is about to close,
        and the synchronous shutdown gates are already set."""
        from memtomem_stm.proxy.config import ProxyConfig
        from memtomem_stm.proxy.manager import ProxyManager

        store = _open(tmp_path / "m.db")
        manager = ProxyManager(
            ProxyConfig(config_path=tmp_path / "proxy.json"), TokenTracker(metrics_store=store)
        )
        await manager.start()
        task = manager._cache_flush_task
        assert task is not None
        # A pending background task makes stop() suspend in its drain; run it
        # only up to that first suspension, where a cancelled teardown stops.
        background = asyncio.create_task(asyncio.sleep(60))
        manager._background_tasks.add(background)
        stopping = manager.stop()
        stopping.send(None)
        stopping.close()
        assert manager._stopped is True
        assert manager._background_closed is True
        assert manager._cache_flush_task is None
        with pytest.raises(asyncio.CancelledError):
            await task
        background.cancel()
        await manager.stop()
        store.close()


class TestFlushConnection:
    """The periodic flush writes through its own short-budget connection, so
    the main connection's lock budget is never changed (#1329)."""

    def test_a_failed_periodic_write_leaves_the_main_budget_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "m.db"
        store = _open(path)
        assert store._db is not None
        before = store._db.execute("PRAGMA busy_timeout").fetchone()[0]
        other = sqlite3.connect(path)
        other.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(sqlite3.OperationalError):
                store.record_cache_counts(
                    {("2026-11-21", "s", "t"): [1, 0, 0, 0]}, busy_timeout_ms=50
                )
        finally:
            other.rollback()
            other.close()
        assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] == before
        assert store._fast_db is None  # dropped after the failure
        store.record_cache_counts({("2026-11-21", "s", "t"): [1, 0, 0, 0]}, busy_timeout_ms=50)
        fast = store._fast_db
        assert fast is not None
        # Tuned like the main connection, with the short budget actually set.
        assert fast.execute("PRAGMA busy_timeout").fetchone()[0] == 50
        assert (
            fast.execute("PRAGMA synchronous").fetchone()[0]
            == (store._db.execute("PRAGMA synchronous").fetchone()[0])
        )
        # A different budget on reuse is honoured, not the one it opened with.
        store.record_cache_counts({("2026-11-21", "s", "t"): [0, 0, 0, 0]}, busy_timeout_ms=20)
        assert store._fast_db is not None
        assert store._fast_db.execute("PRAGMA busy_timeout").fetchone()[0] == 20
        store.close()
        assert store._fast_db is None
        assert _rows(path) == [("2026-11-21", "s", "t", 1, 0, 0, 0)]
