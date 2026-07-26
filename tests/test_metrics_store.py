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
    "source",
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
