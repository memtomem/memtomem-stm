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
from memtomem_stm.proxy.metrics_store import (
    _BASE_COLUMNS,
    _CREATE,
    _INDEX,
    _INDEX_NAMES,
    _MIGRATIONS,
    _SCHEMA_FINGERPRINT,
    _SCHEMA_FINGERPRINT_KEY,
    _SOURCE_ID_INDEX,
    _SOURCE_INDEX,
    MetricsStore,
    _schema_fingerprint,
    read_compression_summary,
)


NEW_COLUMNS = {
    "index_ok",
    "index_error",
    "chunks_indexed",
    "extract_ok",
    "extract_error",
    "surfacing_on_progressive_ok",
    "surface_error",
    "source",
}


def _column_names(db: sqlite3.Connection) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA table_info(proxy_metrics)")}


def _clear_schema_stamp(db_path) -> None:
    """Remove the ``initialize`` schema stamp so the next open takes the slow path.

    Models the window a concurrent opener really sees — it probes
    ``metrics_meta`` before the other process commits its stamp.
    """
    db = sqlite3.connect(str(db_path))
    try:
        db.execute("DELETE FROM metrics_meta")
        db.commit()
    finally:
        db.close()


def _read_schema_stamp(db_path) -> str | None:
    db = sqlite3.connect(str(db_path))
    try:
        row = db.execute(
            "SELECT value FROM metrics_meta WHERE key = ?", (_SCHEMA_FINGERPRINT_KEY,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # no metrics_meta table at all
    finally:
        db.close()
    return None if row is None else str(row[0])


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

    def test_already_migrated_db_is_noop(self, tmp_path, monkeypatch):
        """Re-running ``_migrate`` against an already-migrated DB must not raise.

        This is the critical invariant: if the ALTER statements are not guarded
        against "column already exists", SQLite raises ``OperationalError:
        duplicate column name`` and the store fails to initialize — i.e., the
        proxy cannot start on an existing DB.

        The stamp is cleared before each reopen and ``_migrate`` is asserted to
        have run. Since #870 a stamped DB fast-paths, so without clearing it
        these openers would skip ``_migrate`` entirely and the test would pass
        no matter how badly the re-run behaved.
        """
        db_path = tmp_path / "metrics.db"
        # First open: creates + migrates.
        store = MetricsStore(db_path)
        store.initialize()
        store.close()

        migrated: list[bool] = []
        real_migrate = MetricsStore._migrate
        monkeypatch.setattr(
            MetricsStore,
            "_migrate",
            lambda self, conn: (migrated.append(True), real_migrate(self, conn))[1],
        )
        for expected in (1, 2):
            _clear_schema_stamp(db_path)
            store = MetricsStore(db_path)
            try:
                store.initialize()  # must not raise
                assert NEW_COLUMNS.issubset(_column_names(store._db))
            finally:
                store.close()
            assert len(migrated) == expected, "reopen did not re-run the migration"

    def test_migrate_tolerates_lost_race_duplicate_column(self, tmp_path, monkeypatch):
        """A migration whose ``_existing_columns`` snapshot is stale (another
        process added the columns after the read) must not raise.

        Reproduces the cross-process race: two ``mms`` sessions start right
        after a column-adding upgrade, both read the pre-migration schema,
        and the loser's ALTER hits ``duplicate column name``. The loser must
        treat that as benign and initialize cleanly instead of crashing
        startup. Forcing the snapshot to ``set()`` makes EVERY new-column
        ALTER duplicate, so the tolerance is exercised for all of them.
        """
        db_path = tmp_path / "metrics.db"
        # Winner fully migrates the DB.
        winner = MetricsStore(db_path)
        winner.initialize()
        winner.close()
        # Drop the winner's schema stamp: in the real race the loser probes
        # ``metrics_meta`` BEFORE the winner's stamp commit, so it takes the
        # slow path. Without this the loser would fast-path out of ``_migrate``
        # entirely and the stale-snapshot tolerance below would go untested.
        _clear_schema_stamp(db_path)

        # Loser reads a stale (empty) snapshot → every ALTER targets an
        # already-present column and raises "duplicate column name".
        monkeypatch.setattr(MetricsStore, "_existing_columns", lambda self, db: set())
        loser = MetricsStore(db_path)
        try:
            loser.initialize()  # must not raise
            assert NEW_COLUMNS.issubset(_column_names(loser._db))
        finally:
            loser.close()

    def test_migrate_reraises_non_duplicate_operational_error(self, tmp_path, monkeypatch):
        """The duplicate-column tolerance must not swallow real ALTER
        failures — any other ``OperationalError`` still propagates so a
        genuinely broken migration is not silently skipped."""
        db_path = tmp_path / "metrics.db"

        class _AlterFailsConnection:
            """Wraps a real connection; makes only the ADD COLUMN ALTERs
            fail with a non-duplicate error, delegating everything else."""

            def __init__(self, real: sqlite3.Connection) -> None:
                self._real = real

            def execute(self, sql: str, *args):  # noqa: ANN002, ANN202
                if sql.startswith("ALTER TABLE proxy_metrics ADD COLUMN"):
                    raise sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, *args)

            def __getattr__(self, name: str):  # noqa: ANN202
                return getattr(self._real, name)

        real_connect = sqlite3.connect
        monkeypatch.setattr(
            "memtomem_stm.proxy.metrics_store.sqlite3.connect",
            lambda *a, **k: _AlterFailsConnection(real_connect(*a, **k)),
        )

        store = MetricsStore(db_path)
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            store.initialize()


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

    @pytest.mark.parametrize(
        "column",
        ["error_message", "index_error", "extract_error", "surface_error"],
    )
    def test_diagnostic_content_is_escaped_before_sqlite_bind(self, store, column):
        kwargs = {column: "diagnostic\ud800"}
        store.record(
            CallMetrics(
                server="server",
                tool="tool",
                original_chars=1,
                compressed_chars=1,
                **kwargs,
            )
        )
        value = store._db.execute(f"SELECT {column} FROM proxy_metrics").fetchone()[0]
        assert value == r"diagnostic\ud800"
        value.encode("utf-8")

    @pytest.mark.parametrize(
        "field",
        ["server", "tool", "trace_id", "compression_strategy", "source"],
    )
    def test_unencodable_identifier_is_refused(self, store, field):
        kwargs = {
            "server": "server",
            "tool": "tool",
            "trace_id": "trace",
            "compression_strategy": "truncate",
            "source": "mcp",
        }
        kwargs[field] += "\ud800"
        with pytest.raises(ValueError, match=rf"^{field} must be a valid UTF-8 identifier$"):
            store.record(
                CallMetrics(
                    original_chars=1,
                    compressed_chars=1,
                    **kwargs,
                )
            )
        assert store._db.execute("SELECT COUNT(*) FROM proxy_metrics").fetchone()[0] == 0

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

    def test_source_defaults_to_mcp_and_persists_hook(self, store):
        # Proxied calls omit ``source`` → stored as 'mcp'; ``mms hook`` passes
        # 'hook' so native built-in tool spend is separable in the shared store.
        store.record(CallMetrics(server="lf", tool="search", original_chars=1, compressed_chars=1))
        store.record(
            CallMetrics(
                server="builtin",
                tool="Bash",
                original_chars=900,
                compressed_chars=300,
                source="hook",
            )
        )
        rows = dict(
            store._db.execute("SELECT server, source FROM proxy_metrics ORDER BY server").fetchall()
        )
        assert rows == {"builtin": "hook", "lf": "mcp"}

    def test_get_tool_profiles_excludes_hook_source(self, store):
        # The tuner adjusts proxy compression budgets for upstream tools; a
        # native-tool hook row (source='hook') must never surface as a profile,
        # else the tuner emits a bogus 'builtin/Bash compression=truncate' rec.
        store.record(
            CallMetrics(
                server="lf",
                tool="search",
                original_chars=1000,
                compressed_chars=400,
                cleaned_chars=900,
                compression_strategy="truncate",
            )
        )
        store.record(
            CallMetrics(
                server="builtin",
                tool="Bash",
                original_chars=900,
                compressed_chars=300,
                cleaned_chars=900,
                compression_strategy="truncate",
                source="hook",
            )
        )
        profiles = store.get_tool_profiles(since_seconds=3600.0)
        keys = {(p["server"], p["tool"]) for p in profiles}
        assert ("lf", "search") in keys
        assert ("builtin", "Bash") not in keys

    def test_error_stats_exclude_hook_source(self, store):
        # The exposure health filter (#465) is proxy analytics; hook rows must
        # not enter its per-(server, tool) call/error counts.
        store.record(CallMetrics(server="gh", tool="search", original_chars=10, compressed_chars=5))
        store.record(
            CallMetrics(
                server="builtin",
                tool="Bash",
                original_chars=10,
                compressed_chars=5,
                source="hook",
            )
        )
        err = store.get_tool_error_stats(since_seconds=3600.0, error_categories=("upstream_error",))
        assert ("builtin", "Bash") not in err
        assert ("gh", "search") in err


class TestPerSourceTrim:
    """``max_history`` is enforced per ``source``, scoped to the writer's own.

    The cap used to be table-wide FIFO, and the hook writer (one row per
    built-in tool call) outpaces proxied MCP traffic by orders of magnitude —
    in live data hook rows held 99.7% of the cap and evicted the ``'mcp'``
    rows the mcp-scoped aggregations (``get_tool_profiles``, error stats)
    depend on. ``read_compression_summary`` filters by source only when one
    is passed.
    """

    def _mcp_row(self, i: int) -> CallMetrics:
        return CallMetrics(server="gh", tool=f"mcp{i}", original_chars=1, compressed_chars=1)

    def _hook_row(self, i: int) -> CallMetrics:
        return CallMetrics(
            server="builtin", tool=f"Bash{i}", original_chars=1, compressed_chars=1, source="hook"
        )

    def _counts_by_source(self, store: MetricsStore) -> dict[str, int]:
        return dict(
            store._db.execute(
                "SELECT source, COUNT(*) FROM proxy_metrics GROUP BY source"
            ).fetchall()
        )

    def test_hook_churn_cannot_evict_mcp_rows(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db", max_history=5)
        store.initialize()
        try:
            for i in range(3):
                store.record(self._mcp_row(i))
            for i in range(20):
                store.record(self._hook_row(i))
            assert self._counts_by_source(store) == {"mcp": 3, "hook": 5}
        finally:
            store.close()

    def test_writer_source_still_trimmed_to_cap(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db", max_history=5)
        store.initialize()
        try:
            for i in range(8):
                store.record(self._mcp_row(i))
            remaining = [
                row[0]
                for row in store._db.execute(
                    "SELECT tool FROM proxy_metrics ORDER BY id"
                ).fetchall()
            ]
            # Oldest rows go first; the newest max_history survive.
            assert remaining == [f"mcp{i}" for i in range(3, 8)]
        finally:
            store.close()

    def test_trim_after_concurrent_reconcile_deletes_nothing(self, tmp_path):
        """Repeated trims at cap are no-ops (idempotency), and the trim is a
        single self-limiting statement (codex review rounds 2-3).

        Reconciliation runs from every initialize and the hook opens a
        short-lived store per call, so two processes can trim the same
        source concurrently. What makes that safe is that count-and-delete
        is ONE statement — the excess is a scalar subquery inside the
        DELETE's own LIMIT, so a racer can never apply a stale pre-computed
        excess. The cross-process interleaving itself is not reproducible
        deterministically (the sequential calls below would also pass
        against a two-query implementation, which re-counts), so the
        single-statement shape is pinned by source inspection alongside the
        behavioral no-op check."""
        import inspect
        import re as _re

        # The statement lives in _trim_statement; _trim_source is that plus a commit.
        src = inspect.getsource(MetricsStore._trim_statement)
        assert len(_re.findall(r"db\.execute\(", src)) == 1, (
            "_trim_statement must stay a single count-and-delete statement; a "
            "separate COUNT reintroduces the stale-excess over-delete race"
        )
        assert "LIMIT max(0," in src
        db_path = tmp_path / "metrics.db"
        store = MetricsStore(db_path, max_history=5)
        store.initialize()
        try:
            for i in range(8):
                store.record(self._hook_row(i))
            assert self._counts_by_source(store) == {"hook": 5}
            store._trim_source(store._db, "hook")
            store._trim_source(store._db, "hook")
            assert self._counts_by_source(store) == {"hook": 5}
        finally:
            store.close()

    def test_initialize_reconciles_over_cap_sources(self, tmp_path):
        """Per-write trim only covers the writer's own source, so a source
        that stops receiving writes after ``max_history`` is lowered would
        otherwise stay over cap forever — initialize reconciles every source
        once (codex review round 1)."""
        db_path = tmp_path / "metrics.db"
        big = MetricsStore(db_path, max_history=10)
        big.initialize()
        try:
            for i in range(8):
                big.record(self._hook_row(i))
        finally:
            big.close()

        small = MetricsStore(db_path, max_history=5)
        small.initialize()
        try:
            # Startup reconciled the idle hook source down to the new cap.
            assert self._counts_by_source(small) == {"hook": 5}
            # And mcp-only traffic afterwards never has to repair it.
            for i in range(7):
                small.record(self._mcp_row(i))
            assert self._counts_by_source(small) == {"hook": 5, "mcp": 5}
        finally:
            small.close()

    def test_source_index_created_on_pre_source_db(self, tmp_path):
        # The (source, created_at) index can only be created after _migrate
        # adds ``source`` — initialize over the original pre-F2 schema must
        # not crash and must end up with the index.
        db_path = tmp_path / "metrics.db"
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
        raw.commit()
        raw.close()

        store = MetricsStore(db_path, max_history=5)
        store.initialize()
        try:
            names = {
                row[0]
                for row in store._db.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            assert "idx_metrics_source_created" in names
            # And the per-source trim works against the migrated DB.
            for i in range(8):
                store.record(self._hook_row(i))
            assert self._counts_by_source(store) == {"hook": 5}
        finally:
            store.close()


class TestBusyTimeout:
    """The hook passes a small ``busy_timeout_ms`` so a locked shared DB
    fast-fails instead of stalling the host's tool call."""

    def test_override_is_applied(self, tmp_path):
        store = MetricsStore(tmp_path / "m.db", busy_timeout_ms=250)
        store.initialize()
        try:
            assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] == 250
        finally:
            store.close()

    def test_default_keeps_shared_timeout(self, tmp_path):
        store = MetricsStore(tmp_path / "m.db")
        store.initialize()
        try:
            # tune_connection's shared BUSY_TIMEOUT_MS (3000) is untouched.
            assert store._db.execute("PRAGMA busy_timeout").fetchone()[0] == 3000
        finally:
            store.close()


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

            final_count = store._db.execute("SELECT COUNT(*) FROM proxy_metrics").fetchone()[0]
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
                CallMetrics(
                    server="c7", tool="query-docs", original_chars=1000, compressed_chars=400
                )
            )
            store.record(
                CallMetrics(
                    server="c7", tool="query-docs", original_chars=1000, compressed_chars=600
                )
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

    def _seed_mixed_sources(self, db_path):
        store = MetricsStore(db_path)
        store.initialize()
        try:
            store.record(
                CallMetrics(server="lf", tool="search", original_chars=1000, compressed_chars=400)
            )
            store.record(
                CallMetrics(
                    server="builtin",
                    tool="Bash",
                    original_chars=900,
                    compressed_chars=300,
                    source="hook",
                )
            )
            store.record(
                CallMetrics(
                    server="builtin",
                    tool="Read",
                    original_chars=5000,
                    compressed_chars=5000,
                    source="hook",
                )
            )
        finally:
            store.close()

    def test_source_filter(self, tmp_path):
        db_path = tmp_path / "metrics.db"
        self._seed_mixed_sources(db_path)

        hook = read_compression_summary(db_path, source="hook")
        assert hook["total_calls"] == 2
        assert {r["server"] for r in hook["by_tool"]} == {"builtin"}
        assert hook["total_original_chars"] == 5900

        mcp = read_compression_summary(db_path, source="mcp")
        assert mcp["total_calls"] == 1
        assert {r["tool"] for r in mcp["by_tool"]} == {"search"}

        # tool + source compose
        bash = read_compression_summary(db_path, tool="Bash", source="hook")
        assert bash["total_calls"] == 1
        assert bash["by_tool"][0]["saved_ratio"] == round(1 - 300 / 900, 4)

    def test_source_filter_on_pre_source_db_returns_empty_for_hook(self, tmp_path):
        # A DB created before the ``source`` column existed has only legacy
        # ('mcp') rows. ``source='hook'`` must report empty-but-available rather
        # than over-counting every legacy row (which a dropped guard would do).
        db_path = tmp_path / "legacy.db"
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

        hook = read_compression_summary(db_path, source="hook")
        assert hook["available"] is True
        assert hook["schema_outdated"] is True
        assert hook["total_calls"] == 0
        assert hook["by_tool"] == []

        # 'mcp' on a legacy DB still sees the legacy rows (all implicitly mcp).
        mcp = read_compression_summary(db_path, source="mcp")
        assert mcp["total_calls"] == 1

    def test_schema_outdated_false_when_only_source_missing(self, tmp_path):
        # A DB that has ``is_error`` but predates ``source``: error counts are
        # available, so ``schema_outdated`` (which flags is_error only) must be
        # False — otherwise the CLI shows a misleading "error counts" warning.
        db_path = tmp_path / "post_is_error_pre_source.db"
        db = sqlite3.connect(db_path)
        db.execute(
            "CREATE TABLE proxy_metrics ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, server TEXT, tool TEXT, "
            "original_chars INTEGER, compressed_chars INTEGER, "
            "cleaned_chars INTEGER DEFAULT 0, created_at REAL, "
            "is_error INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute(
            "INSERT INTO proxy_metrics "
            "(server, tool, original_chars, compressed_chars, created_at, is_error) "
            "VALUES ('s', 't', 100, 40, 0, 0)"
        )
        db.commit()
        db.close()

        summary = read_compression_summary(db_path)
        assert summary["available"] is True
        assert summary["schema_outdated"] is False  # is_error present → up to date
        assert summary["total_calls"] == 1
        # source filter still degrades correctly without the column:
        assert read_compression_summary(db_path, source="hook")["total_calls"] == 0
        assert read_compression_summary(db_path, source="mcp")["total_calls"] == 1

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

    @pytest.mark.parametrize("filter_field", ["tool", "source"])
    def test_unencodable_filter_still_reports_pre_migration_schema(self, tmp_path, filter_field):
        """Both empty-summary exits must describe the DB the same way.

        ``schema_outdated`` is a property of the file, not of the filter — a
        pre-migration DB is outdated whether the ``tool``/``source`` filter or
        the pre-``source`` filter is what returned nothing.
        """
        db_path = tmp_path / "legacy.db"
        db = sqlite3.connect(db_path)
        db.execute(
            "CREATE TABLE proxy_metrics ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, server TEXT, tool TEXT, "
            "original_chars INTEGER, compressed_chars INTEGER, "
            "cleaned_chars INTEGER DEFAULT 0, created_at REAL)"
        )
        db.commit()
        db.close()

        summary = read_compression_summary(db_path, **{filter_field: "t\ud800"})

        assert summary["available"] is True
        assert summary["schema_outdated"] is True
        assert summary["total_calls"] == 0
        # Baseline: the sibling pre-``source`` guard already agreed.
        assert read_compression_summary(db_path, source="hook")["schema_outdated"] is True

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


class TestProgressiveDegradations:
    """``get_progressive_degradations`` aggregates the ``→passthrough_on_error``
    family (a primary PROGRESSIVE store failure degrading to an uncached
    passthrough) without colliding with the other ``X→Y_fallback`` labels."""

    @pytest.fixture
    def store(self, tmp_path):
        s = MetricsStore(tmp_path / "metrics.db")
        s.initialize()
        yield s
        s.close()

    @staticmethod
    def _row(store, server, tool, strategy):
        store.record(
            CallMetrics(
                server=server,
                tool=tool,
                original_chars=100,
                compressed_chars=100,
                cleaned_chars=100,
                compression_strategy=strategy,
            )
        )

    def test_empty(self, store):
        assert store.get_progressive_degradations() == {"total": 0, "by_server_tool": []}

    def test_counts_and_breakdown_ignores_other_strategies(self, store):
        self._row(store, "gh", "search", "progressive→passthrough_on_error")
        self._row(store, "gh", "search", "progressive→passthrough_on_error")
        self._row(store, "fs", "read", "progressive→passthrough_on_error")
        # Unrelated rows must not count — including the sibling fallback family
        # that also uses the ``X→Y_fallback`` arrow convention.
        self._row(store, "gh", "search", "progressive")
        self._row(store, "gh", "search", "llm_summary→timeout_fallback")

        deg = store.get_progressive_degradations()
        assert deg["total"] == 3
        assert deg["by_server_tool"] == [
            {"server": "gh", "tool": "search", "count": 2},
            {"server": "fs", "tool": "read", "count": 1},
        ]

    def test_tool_filter(self, store):
        self._row(store, "gh", "search", "progressive→passthrough_on_error")
        self._row(store, "fs", "read", "progressive→passthrough_on_error")
        deg = store.get_progressive_degradations(tool="read")
        assert deg["total"] == 1
        assert deg["by_server_tool"] == [{"server": "fs", "tool": "read", "count": 1}]

    def test_window_excludes_old_rows(self, store):
        self._row(store, "gh", "search", "progressive→passthrough_on_error")
        # Backdate the row well outside a 60s look-back window.
        store._db.execute("UPDATE proxy_metrics SET created_at = created_at - 3600")
        store._db.commit()
        deg = store.get_progressive_degradations(since_seconds=60.0)
        assert deg["total"] == 0
        assert deg["by_server_tool"] == []


class TestInitializeFastPath:
    """``initialize`` must skip DDL/migration once the schema is stamped current.

    ``mms hook`` opens this store as a one-shot subprocess on every host
    built-in tool call, so the DDL + ``PRAGMA table_info`` + conditional-ALTER
    cycle ran thousands of times a day against a schema that had not changed
    since the first run (#870). These tests pin that it now runs once per
    schema, and that every state which is NOT current still takes the slow path.
    """

    @staticmethod
    def _traced(monkeypatch) -> list[list[str]]:
        """Record the SQL each new connection executes, one list per connect."""
        traces: list[list[str]] = []
        real_connect = sqlite3.connect

        def _connect(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            conn = real_connect(*args, **kwargs)
            statements: list[str] = []
            traces.append(statements)
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr("memtomem_stm.proxy.metrics_store.sqlite3.connect", _connect)
        return traces

    @staticmethod
    def _schema_work(statements: list[str]) -> list[str]:
        """The schema statements the fast path is supposed to elide."""
        prefixes = (
            "CREATE TABLE IF NOT EXISTS proxy_metrics",
            "CREATE INDEX",
            "ALTER TABLE",
            "PRAGMA table_info",
        )
        flat = [" ".join(s.split()) for s in statements]
        return [s for s in flat if s.startswith(prefixes)]

    def test_second_initialize_skips_ddl_and_migration(self, tmp_path, monkeypatch):
        """The core #870 property, with its own positive control.

        The first open must show schema work (proving the trace seam observes
        DDL at all — without that control an always-empty trace would pass),
        the second must show none.
        """
        db_path = tmp_path / "metrics.db"
        traces = self._traced(monkeypatch)

        first = MetricsStore(db_path)
        first.initialize()
        first.close()
        # Positive control: a fresh DB really does run the DDL we look for.
        assert self._schema_work(traces[0]), "trace seam saw no DDL on a fresh DB"
        assert any(s.startswith("PRAGMA table_info") for s in self._schema_work(traces[0]))

        second = MetricsStore(db_path)
        try:
            second.initialize()
        finally:
            second.close()
        assert self._schema_work(traces[1]) == [], (
            f"warm initialize re-ran schema work: {self._schema_work(traces[1])}"
        )

    def test_fingerprint_is_derived_from_the_migrations_it_gates(self):
        """No hand-maintained version number: the fingerprint's column list is
        exactly the set the slow path can produce, so adding a migration
        invalidates every stamped DB on its own.

        Parsed into fields rather than substring-matched: ``"id" in
        fingerprint`` is satisfied by ``trace_id``, so a substring check would
        pass even for a column the fingerprint omits.
        """
        epoch, columns, indexes = _SCHEMA_FINGERPRINT.split("|")
        assert epoch.isdigit()
        assert set(columns.split(",")) == set(_MIGRATIONS) | set(_BASE_COLUMNS)
        assert set(indexes.split(",")) == set(_INDEX_NAMES)

    def test_base_columns_match_the_create_statement(self):
        """``_BASE_COLUMNS`` is hand-written beside ``_CREATE``; this is what
        keeps the two from drifting, and it uses SQLite as the parser rather
        than re-implementing one.
        """
        db = sqlite3.connect(":memory:")
        try:
            db.execute(_CREATE)
            declared = {row[1] for row in db.execute("PRAGMA table_info(proxy_metrics)")}
        finally:
            db.close()
        assert declared == set(_BASE_COLUMNS)

    def test_fingerprint_index_names_match_the_ddl_that_builds_them(self):
        """The names are parsed from the CREATE statements, so they cannot
        drift from the indexes the slow path actually creates."""
        for name in _INDEX_NAMES:
            assert f"CREATE INDEX IF NOT EXISTS {name} " in (
                _INDEX + _SOURCE_INDEX + _SOURCE_ID_INDEX
            )

    def test_a_changed_migration_changes_the_fingerprint(self, monkeypatch):
        """The property the gate rests on: ship a new column, every stamped DB
        goes through the slow path once."""
        before = _schema_fingerprint()
        monkeypatch.setitem(_MIGRATIONS, "brand_new_column", "ALTER TABLE proxy_metrics ADD ...")
        assert _schema_fingerprint() != before
        assert "brand_new_column" in _schema_fingerprint()

    def test_initialize_stamps_the_current_fingerprint(self, tmp_path):
        db_path = tmp_path / "metrics.db"
        store = MetricsStore(db_path)
        store.initialize()
        store.close()
        assert _read_schema_stamp(db_path) == _SCHEMA_FINGERPRINT

    def test_stale_fingerprint_reruns_migration_and_restamps(self, tmp_path, monkeypatch):
        """A release that adds a column ships a different fingerprint; the
        stamped DB must fall back to the slow path exactly once."""
        db_path = tmp_path / "metrics.db"
        store = MetricsStore(db_path)
        store.initialize()
        store.close()

        db = sqlite3.connect(str(db_path))
        db.execute(
            "UPDATE metrics_meta SET value = ? WHERE key = ?",
            ("0|stale", _SCHEMA_FINGERPRINT_KEY),
        )
        db.commit()
        db.close()

        called: list[bool] = []
        real_migrate = MetricsStore._migrate

        def _spy(self, conn):  # noqa: ANN001, ANN202
            called.append(True)
            return real_migrate(self, conn)

        monkeypatch.setattr(MetricsStore, "_migrate", _spy)
        store = MetricsStore(db_path)
        try:
            store.initialize()
            assert called == [True], "stale stamp did not re-run the migration"
            assert NEW_COLUMNS.issubset(_column_names(store._db))
        finally:
            store.close()
        assert _read_schema_stamp(db_path) == _SCHEMA_FINGERPRINT

    def test_missing_meta_table_falls_back_to_the_slow_path(self, tmp_path):
        """The upgrade case: a fully-migrated pre-#870 DB has no stamp table."""
        db_path = tmp_path / "metrics.db"
        store = MetricsStore(db_path)
        store.initialize()
        store.close()

        db = sqlite3.connect(str(db_path))
        db.execute("DROP TABLE metrics_meta")
        db.commit()
        db.close()
        assert _read_schema_stamp(db_path) is None

        store = MetricsStore(db_path)
        try:
            store.initialize()  # must not raise
            assert NEW_COLUMNS.issubset(_column_names(store._db))
        finally:
            store.close()
        assert _read_schema_stamp(db_path) == _SCHEMA_FINGERPRINT

    @pytest.mark.parametrize(
        "failing_prefix",
        [
            "ALTER TABLE proxy_metrics ADD COLUMN",  # fails mid-migration
            "CREATE INDEX IF NOT EXISTS idx_metrics_source_id",  # fails at the LAST step
        ],
    )
    def test_no_stamp_is_written_when_the_schema_work_fails(
        self, tmp_path, monkeypatch, failing_prefix
    ):
        """A half-applied schema must never look current to the next opener.

        Parameterized over an early and the *last* pre-stamp statement: the
        ordering guarantee has to hold for a failure right before the stamp,
        not only for one that aborts before any DDL has committed.
        """
        db_path = tmp_path / "metrics.db"

        class _FailingConnection:
            def __init__(self, real: sqlite3.Connection) -> None:
                self._real = real

            def execute(self, sql: str, *args):  # noqa: ANN002, ANN202
                if sql.startswith(failing_prefix):
                    raise sqlite3.OperationalError("disk I/O error")
                return self._real.execute(sql, *args)

            def __getattr__(self, name: str):  # noqa: ANN202
                return getattr(self._real, name)

        real_connect = sqlite3.connect
        monkeypatch.setattr(
            "memtomem_stm.proxy.metrics_store.sqlite3.connect",
            lambda *a, **k: _FailingConnection(real_connect(*a, **k)),
        )
        store = MetricsStore(db_path)
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            store.initialize()

        monkeypatch.undo()
        assert _read_schema_stamp(db_path) is None, "stamped a schema that was never applied"

    def test_upgrade_failure_leaves_the_old_stamp_which_still_reads_as_stale(
        self, tmp_path, monkeypatch
    ):
        """The upgrade half of the ordering guarantee, driven through a real failure.

        A failed re-stamp keeps the PREVIOUS value rather than clearing it, so
        the promise is not "no stamp" but "never a *current* stamp": the old one
        still mismatches, and the opener after it must run the slow path.
        """
        db_path = tmp_path / "metrics.db"
        store = MetricsStore(db_path)
        store.initialize()
        store.close()
        # Model the pre-upgrade state: this DB was stamped by an older release.
        db = sqlite3.connect(str(db_path))
        db.execute(
            "UPDATE metrics_meta SET value = ? WHERE key = ?", ("0|stale", _SCHEMA_FINGERPRINT_KEY)
        )
        db.commit()
        db.close()

        class _IndexFailsConnection:
            def __init__(self, real: sqlite3.Connection) -> None:
                self._real = real

            def execute(self, sql: str, *args):  # noqa: ANN002, ANN202
                if sql.startswith("CREATE INDEX IF NOT EXISTS idx_metrics_source_id"):
                    raise sqlite3.OperationalError("disk I/O error")
                return self._real.execute(sql, *args)

            def __getattr__(self, name: str):  # noqa: ANN202
                return getattr(self._real, name)

        real_connect = sqlite3.connect
        monkeypatch.setattr(
            "memtomem_stm.proxy.metrics_store.sqlite3.connect",
            lambda *a, **k: _IndexFailsConnection(real_connect(*a, **k)),
        )
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            MetricsStore(db_path).initialize()
        monkeypatch.undo()

        # The old value survived, and it is still not current.
        assert _read_schema_stamp(db_path) == "0|stale"
        assert _read_schema_stamp(db_path) != _SCHEMA_FINGERPRINT

        # So the next opener redoes the full path and restamps.
        migrated: list[bool] = []
        real_migrate = MetricsStore._migrate
        monkeypatch.setattr(
            MetricsStore,
            "_migrate",
            lambda self, conn: (migrated.append(True), real_migrate(self, conn))[1],
        )
        store = MetricsStore(db_path)
        try:
            store.initialize()
        finally:
            store.close()
        assert migrated == [True], "a stale stamp must send the next opener down the slow path"
        assert _read_schema_stamp(db_path) == _SCHEMA_FINGERPRINT

    def test_probe_propagates_errors_that_are_not_a_missing_table(self, tmp_path, monkeypatch):
        """Only "no such table" means "not current".

        Anything else is reported from the statement that hit it instead of
        being retried as DDL that would fail the same way one statement later.
        """
        db_path = tmp_path / "metrics.db"
        store = MetricsStore(db_path)
        store.initialize()
        store.close()

        class _ProbeFailsConnection:
            def __init__(self, real: sqlite3.Connection) -> None:
                self._real = real

            def execute(self, sql: str, *args):  # noqa: ANN002, ANN202
                if sql.startswith("SELECT value FROM metrics_meta"):
                    raise sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, *args)

            def __getattr__(self, name: str):  # noqa: ANN202
                return getattr(self._real, name)

        real_connect = sqlite3.connect
        monkeypatch.setattr(
            "memtomem_stm.proxy.metrics_store.sqlite3.connect",
            lambda *a, **k: _ProbeFailsConnection(real_connect(*a, **k)),
        )
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            MetricsStore(db_path).initialize()

    def test_fast_path_store_still_records(self, tmp_path, monkeypatch):
        """End-to-end sanity: the second opener writes a readable row.

        Proves the opener skipped the schema work by spying on ``_migrate``.
        Checking ``_schema_is_current`` *after* ``initialize`` would prove
        nothing — the slow path stamps before returning, so it reads current
        either way.
        """
        db_path = tmp_path / "metrics.db"
        warm = MetricsStore(db_path)
        warm.initialize()
        warm.close()

        migrated: list[bool] = []
        real_migrate = MetricsStore._migrate
        monkeypatch.setattr(
            MetricsStore,
            "_migrate",
            lambda self, conn: (migrated.append(True), real_migrate(self, conn))[1],
        )
        store = MetricsStore(db_path, reconcile_on_init=False)
        store.initialize()
        assert migrated == [], "expected this opener to use the fast path"
        try:
            store.record(
                CallMetrics(
                    server="builtin",
                    tool="Read",
                    original_chars=1000,
                    compressed_chars=400,
                    source="hook",
                )
            )
        finally:
            store.close()
        summary = read_compression_summary(db_path)
        assert summary["total_calls"] == 1


class TestIntervalTrim:
    """``record`` trims on interval boundaries instead of on every write.

    The trim's cost is its ``COUNT(*)`` over the writer's partition — a
    covering-index scan that sits *at* ``max_history`` in steady state and
    grows with it (0.39 ms of a 0.43 ms ``record`` at the default 10,000 cap).
    ``mms hook`` pays that on every host built-in tool call (#870).
    """

    @staticmethod
    def _fill(store: MetricsStore, n: int, source: str = "hook", start: int = 0) -> None:
        """Write ``n`` rows tagged by ``original_chars`` so survivors are identifiable."""
        for i in range(start, start + n):
            store.record(
                CallMetrics(
                    server="builtin",
                    tool="Read",
                    original_chars=1000 + i,
                    compressed_chars=500,
                    source=source,
                )
            )

    @staticmethod
    def _count(store: MetricsStore, source: str = "hook") -> int:
        return store._db.execute(
            "SELECT COUNT(*) FROM proxy_metrics WHERE source = ?", (source,)
        ).fetchone()[0]

    def test_interval_keeps_the_excess_proportional_to_the_cap(self, tmp_path):
        """Small caps stay exact; large ones amortize, but never without bound."""
        intervals = {
            cap: MetricsStore(tmp_path / "m.db", max_history=cap)._trim_interval()
            for cap in (0, 1, 99, 100, 199, 1000, 10_000, 10**9)
        }
        assert intervals[0] == 1, "must not divide by zero or skip trimming entirely"
        assert intervals[1] == intervals[99] == intervals[100] == intervals[199] == 1
        assert intervals[1000] == 10  # ~1% of the cap
        assert intervals[10_000] == 64  # ceiling: ~0.6% of the default cap
        assert intervals[10**9] == 64, "interval must stay bounded for huge caps"

    def test_small_cap_trims_on_every_insert(self, tmp_path):
        store = MetricsStore(tmp_path / "m.db", max_history=5)
        store.initialize()
        try:
            assert all(store._should_trim("hook", row_id) for row_id in range(1, 50))
        finally:
            store.close()

    def test_the_trigger_fires_on_the_real_boundary_ids(self, tmp_path):
        """The rule as an observed sequence, not as hypothetical ids.

        Records real rows and captures which ones actually reached the trim, so
        the assertion is about the ids SQLite assigned and the predecessor each
        row really had. Pinned as the exact set with equal gaps: a coin would
        pass a "sometimes fires" check while allowing arbitrarily long ones.
        """
        fired: list[int] = []
        store = MetricsStore(tmp_path / "m.db", max_history=10_000)
        store.initialize()
        real_statement = MetricsStore._trim_statement

        def _spy(self, db, source):  # noqa: ANN001, ANN202
            fired.append(
                db.execute(
                    "SELECT MAX(id) FROM proxy_metrics WHERE source = ?", (source,)
                ).fetchone()[0]
            )
            return real_statement(self, db, source)

        try:
            assert store._trim_interval() == 64
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(MetricsStore, "_trim_statement", _spy)
                self._fill(store, 200)
        finally:
            store.close()
        # Row 1 has no predecessor and trims; after that only the crossings,
        # one interval apart (the first gap is 63, the bootstrap trim to row 64).
        assert fired == [1, 64, 128, 192]
        assert all(b - a == 64 for a, b in zip(fired[1:], fired[2:]))

    def test_the_bound_holds_with_a_fresh_store_per_write(self, tmp_path):
        """Production topology: ``mms hook`` builds a NEW store per invocation,
        writes one row and exits, so nothing survives in memory between writes.

        A schedule kept in the store object would silently degrade to "every
        write is the first write" here. This is also the shape in which the
        interleavings below have to hold.
        """
        cap, db_path = 500, tmp_path / "m.db"
        warm = MetricsStore(db_path, max_history=cap)
        warm.initialize()
        interval = warm._trim_interval()
        assert interval == 5
        warm.close()

        peak = 0
        for i in range(cap + 300):
            store = MetricsStore(db_path, max_history=cap, reconcile_on_init=False)
            store.initialize()
            try:
                self._fill(store, 1, start=i)
                peak = max(peak, self._count(store))
            finally:
                store.close()
        assert peak <= cap + interval, f"one-shot writers reached {peak}, cap {cap}"
        assert peak > cap, "positive control: it must actually exceed the cap sometimes"

    @pytest.mark.parametrize("mcp_slot", [0, 1])
    def test_no_interleaving_phase_can_starve_a_source(self, tmp_path, mcp_slot):
        """Regression, and the reason the trigger is not ``id % interval == 0``.

        That rule starves any source whose rows never land on a multiple: at
        interval 5 with ``mcp`` taking ids 1, 6, 11, ... (always 1 mod 5) it
        never trimmed, reaching 700 rows against a cap of 500 and still
        climbing. Parameterized over the two phases that matter — ``mcp`` owning
        every multiple, and ``mcp`` owning a non-multiple — and run through
        one-shot stores so neither source has in-memory history to lean on.
        """
        # Cap 200 -> interval 2, so BOTH sources take half the ids and each one
        # outgrows the cap within the loop. With a larger interval the sparse
        # source never reaches its cap in a test-sized run, and the assertion
        # below would hold no matter how badly the trigger behaved.
        cap, db_path = 200, tmp_path / "m.db"
        warm = MetricsStore(db_path, max_history=cap)
        warm.initialize()
        interval = warm._trim_interval()
        assert interval == 2
        warm.close()

        written: dict[str, int] = {}
        peak: dict[str, int] = {}
        for i in range(600):
            source = "mcp" if i % interval == mcp_slot else "hook"
            written[source] = written.get(source, 0) + 1
            store = MetricsStore(db_path, max_history=cap, reconcile_on_init=False)
            store.initialize()
            try:
                self._fill(store, 1, source=source, start=i)
                for src, n in store._db.execute(
                    "SELECT source, COUNT(*) FROM proxy_metrics GROUP BY source"
                ):
                    peak[src] = max(peak.get(src, 0), n)
            finally:
                store.close()
        # Precondition: untrimmed, every source would have blown past the cap,
        # so an untrimmed source cannot hide inside the bound.
        for src, n in written.items():
            assert n > cap + interval, f"{src} wrote {n} rows: cannot detect starvation"
        for src, n in peak.items():
            assert n <= cap + interval, f"{src} starved at {n} rows, cap {cap}"

    def test_partition_never_exceeds_cap_plus_interval_while_writing(self, tmp_path):
        """The bound itself, measured rather than asserted about the trigger.

        Writes well past the cap and checks the partition after *every* insert,
        so a regression to unbounded growth (or to an unbounded-tail sample)
        fails here even if the trigger arithmetic still looks plausible.
        """
        cap = 1000
        store = MetricsStore(tmp_path / "m.db", max_history=cap)
        store.initialize()
        try:
            interval = store._trim_interval()
            assert interval == 10
            high_water = 0
            for i in range(cap + 200):
                self._fill(store, 1, start=i)
                high_water = max(high_water, self._count(store))
            assert high_water <= cap + interval, f"partition reached {high_water}, cap {cap}"
            assert high_water > cap, "positive control: it must actually exceed the cap sometimes"
        finally:
            store.close()

    def test_uncovered_insert_runs_no_count_or_delete(self, tmp_path, monkeypatch):
        """The point of the change: the scan is off the write path in between.

        Includes the positive control — the same probe with the trim forced on
        must show it, so an always-empty trace cannot pass this.
        """
        statements: list[str] = []
        store = MetricsStore(tmp_path / "m.db", max_history=10_000)
        store.initialize()
        try:
            self._fill(store, 3)
            store._db.set_trace_callback(statements.append)

            monkeypatch.setattr(MetricsStore, "_should_trim", lambda self, source, row_id: False)
            self._fill(store, 1)
            assert not [s for s in statements if "DELETE" in s.upper()]
            assert not [s for s in statements if "COUNT(*)" in s.upper()]

            statements.clear()
            monkeypatch.setattr(MetricsStore, "_should_trim", lambda self, source, row_id: True)
            self._fill(store, 1)
            assert [s for s in statements if "DELETE" in s.upper()], "positive control failed"
        finally:
            store._db.set_trace_callback(None)
            store.close()

    def test_one_trim_returns_an_overshooting_partition_to_the_cap(self, tmp_path, monkeypatch):
        """Excess is transient: the DELETE always removes the FULL excess.

        This is what makes an approximate cap safe — a partition that drifted
        above it snaps back on the next trim rather than converging one row per
        write.
        """
        cap = 200
        store = MetricsStore(tmp_path / "m.db", max_history=cap)
        store.initialize()
        try:
            monkeypatch.setattr(MetricsStore, "_should_trim", lambda self, source, row_id: False)
            self._fill(store, cap + 50)
            assert self._count(store) == cap + 50, "trim-off did not let the partition grow"

            monkeypatch.setattr(MetricsStore, "_should_trim", lambda self, source, row_id: True)
            self._fill(store, 1, start=cap + 50)
            assert self._count(store) == cap, "one trim must remove the whole excess"
            # The survivors are the newest rows, as in the every-write regime:
            # the 51 oldest of the 251 written are the ones that went.
            oldest = store._db.execute(
                "SELECT MIN(original_chars) FROM proxy_metrics WHERE source = 'hook'"
            ).fetchone()[0]
            assert oldest == 1000 + 51
        finally:
            store.close()

    def test_initialize_reconcile_stays_exact(self, tmp_path, monkeypatch):
        """Interval trimming is a write-path amortization only — startup
        reconciliation must still bring every source exactly to the cap."""
        db_path = tmp_path / "m.db"
        cap = 200
        store = MetricsStore(db_path, max_history=cap)
        store.initialize()
        monkeypatch.setattr(MetricsStore, "_should_trim", lambda self, source, row_id: False)
        try:
            self._fill(store, cap + 50)
            assert self._count(store) == cap + 50, "precondition: partition must be over cap"
        finally:
            store.close()
        monkeypatch.undo()

        reconciler = MetricsStore(db_path, max_history=cap)
        reconciler.initialize()
        try:
            assert self._count(reconciler) == cap
        finally:
            reconciler.close()

    def test_trim_targets_only_the_written_source(self, tmp_path, monkeypatch):
        """Interval trimming must not have widened the trim's scope: a hook
        write still cannot evict 'mcp' rows."""
        store = MetricsStore(tmp_path / "m.db", max_history=5)
        store.initialize()
        try:
            self._fill(store, 3, source="mcp")
            self._fill(store, 40, source="hook")
            assert self._count(store, "mcp") == 3
            assert self._count(store, "hook") == 5
        finally:
            store.close()


class TestTrimSchedulingUnderFailure:
    """The two ways a boundary crossing can be silently consumed (#870 review)."""

    @staticmethod
    def _row(i: int, source: str = "hook") -> CallMetrics:
        return CallMetrics(
            server="builtin",
            tool="Read",
            original_chars=1000 + i,
            compressed_chars=500,
            source=source,
        )

    def test_predecessor_is_caller_relative_not_second_newest(self, tmp_path):
        """Two writers that both commit before either schedules must not both
        skip the crossing.

        "The second-newest row of this source" is the caller's predecessor only
        when nothing interleaves: with rows 202 and 203 both committed, the
        writer of 202 would find 202 and compare itself against itself, and 203
        would compare against 202 — neither crossing. Keying on ``id < row_id``
        makes the answer caller-relative regardless of commit order.
        """
        store = MetricsStore(tmp_path / "m.db", max_history=1000)
        store.initialize()
        try:
            for i in range(5):
                store.record(self._row(i))
            newest = store._db.execute(
                "SELECT MAX(id) FROM proxy_metrics WHERE source = 'hook'"
            ).fetchone()[0]
            # Asked about an EARLIER row while a newer one is already committed:
            # the answer must be that row's own predecessor, not the newest.
            assert store._previous_row_id("hook", newest) == newest - 1
            assert store._previous_row_id("hook", newest - 1) == newest - 2
            # And never the row itself.
            for row_id in range(2, newest + 1):
                assert store._previous_row_id("hook", row_id) < row_id
        finally:
            store.close()

    def test_previous_row_id_ignores_clock_inversion(self, tmp_path):
        """An older row stamped with a future ``created_at`` must not become
        the predecessor — ordering is by id, which the insert assigns."""
        store = MetricsStore(tmp_path / "m.db", max_history=1000)
        store.initialize()
        try:
            for i in range(4):
                store.record(self._row(i))
            store._db.execute(
                "UPDATE proxy_metrics SET created_at = created_at + 3600 WHERE id = 1"
            )
            store._db.commit()
            assert store._previous_row_id("hook", 4) == 3
        finally:
            store.close()

    def test_previous_row_id_is_stable_when_timestamps_tie(self, tmp_path):
        store = MetricsStore(tmp_path / "m.db", max_history=1000)
        store.initialize()
        try:
            for i in range(5):
                store.record(self._row(i))
            store._db.execute("UPDATE proxy_metrics SET created_at = 1.0")
            store._db.commit()
            assert store._previous_row_id("hook", 5) == 4
        finally:
            store.close()

    def test_a_failed_trim_rolls_back_its_crossing_row(self, tmp_path, monkeypatch):
        """A crossing row that survived its own failed DELETE would become the
        next row's predecessor, land in the same bucket, and consume the
        crossing. Insert and trim therefore commit together.

        The failing write is a row that crosses *naturally* — at interval 2 an
        even id crosses, so with 211 rows written the next one is id 212 — and
        the assertion afterwards is an exact return to the cap. Forcing the
        trigger and asserting only ``<= cap + interval`` would pass even for an
        implementation that committed the insert first and consumed the
        boundary.
        """
        cap = 200
        store = MetricsStore(tmp_path / "m.db", max_history=cap)
        store.initialize()
        try:
            interval = store._trim_interval()
            assert interval == 2
            for i in range(211):
                store.record(self._row(i))
            next_id = store._db.execute("SELECT MAX(id) FROM proxy_metrics").fetchone()[0] + 1
            assert store._should_trim("hook", next_id), "fixture must set up a real crossing"
            before = store._db.execute(
                "SELECT COUNT(*) FROM proxy_metrics WHERE source = 'hook'"
            ).fetchone()[0]

            boom = RuntimeError("trim failed")
            monkeypatch.setattr(
                MetricsStore,
                "_trim_statement",
                lambda self, db, source: (_ for _ in ()).throw(boom),
            )
            with pytest.raises(RuntimeError, match="trim failed"):
                store.record(self._row(9999))
            monkeypatch.undo()

            after = store._db.execute(
                "SELECT COUNT(*) FROM proxy_metrics WHERE source = 'hook'"
            ).fetchone()[0]
            assert after == before, "the crossing row survived its failed trim"

            # The boundary was not consumed: the next write crosses and trims
            # all the way back, rather than deferring to the one after it.
            store.record(self._row(10_000))
            assert (
                store._db.execute(
                    "SELECT COUNT(*) FROM proxy_metrics WHERE source = 'hook'"
                ).fetchone()[0]
                == cap
            )
        finally:
            store.close()
