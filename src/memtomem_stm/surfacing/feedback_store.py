"""SQLite persistence for surfacing events and feedback."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TypedDict

from memtomem_stm.utils.json_out import (
    escape_lone_surrogates,
    has_lone_surrogate,
    require_utf8_identifier,
)
from memtomem_stm.utils.sqlite_private import ensure_private_db_files
from memtomem_stm.utils.sqlite_tuning import tune_connection

logger = logging.getLogger(__name__)

_NEGATIVE_FEEDBACK_RATINGS = ("not_relevant", "already_known")

_FAULT_SUMMARY_WINDOW_DAYS = 7
"""Lookback window for the fault counters in ``read_surfacing_summary``,
counted in whole UTC calendar days (today plus the prior
``_FAULT_SUMMARY_WINDOW_DAYS - 1``). Recent-window rather than all-time:
the counters answer "is surfacing degraded *now*", and a long-fixed
incident from months ago shouldn't keep the stats output warning forever.

Calendar-day, not a rolling ``now - 7*86400`` cutoff: ``surfacing_faults``
is aggregated one row per ``(day, server, tool, kind)``, so the finest
honest filter granularity is the ``day`` column. A sub-day rolling cutoff
on ``last_at`` would pass a boundary day's whole ``count`` — including
faults from earlier that day that predate the cutoff — and over-report the
window it advertises. Filtering on ``day`` keeps the count exact for the
stored granularity at the cost of naming the window in calendar days."""

_HASHED_QUERY_RE = re.compile(r"sha256:[0-9a-f]{16}")
"""Exact shape of the opaque ID written under
``SurfacingConfig.persist_query_text=False`` (#352 part 3): the literal
prefix ``sha256:`` followed by 16 lowercase hex chars (23 chars total).
Prefix-only matching would misclassify legitimate raw queries that
happen to start with ``sha256:`` — e.g. a user-typed checksum search —
and bypass the 80-char preview clip, leaking unbounded user-derived
text. ``re.fullmatch`` against this pattern is the gate."""


def _load_safe_memory_ids(raw: object) -> list[str]:
    """Decode identity-bearing JSON without rewriting legacy bad IDs."""
    if not isinstance(raw, (str, bytes, bytearray)):
        return []
    try:
        values = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and not has_lone_surrogate(value)]


def _load_numeric_scores(raw: object) -> list[int | float]:
    """Decode only the numeric score leaves this store can safely expose."""
    if not isinstance(raw, (str, bytes, bytearray)):
        return []
    try:
        values = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(values, list):
        return []
    return [
        value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS surfacing_events (
    id          TEXT    PRIMARY KEY,
    server      TEXT    NOT NULL,
    tool        TEXT    NOT NULL,
    query       TEXT,
    memory_ids  TEXT    NOT NULL,
    scores      TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    -- Core-reported scale of the ``scores`` values (#1781): 'rrf', 'bm25',
    -- 'dense', 'none', 'rerank', or NULL when the core did not name one
    -- (pre-#1781 cores, compact format, compose bundles).
    score_scale TEXT
);

CREATE TABLE IF NOT EXISTS surfacing_feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    surfacing_id    TEXT    NOT NULL REFERENCES surfacing_events(id),
    memory_id       TEXT,
    rating          TEXT    NOT NULL,
    created_at      REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_memories (
    memory_id       TEXT    PRIMARY KEY,
    first_seen_at   REAL    NOT NULL,
    last_seen_at    REAL    NOT NULL,
    seen_count      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS auto_tune_adjustments (
    tool        TEXT    PRIMARY KEY,
    min_score   REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);

-- Durable per-day fault counters for the surfacing pipeline. The in-memory
-- ``SurfacingObservability`` counters die with the process (and the daemon
-- idle-exits routinely), so ``mms stats`` — which reads on-disk stores only —
-- could not distinguish "surfacing intentionally quiet" from "surfacing dead
-- on LTM timeouts / open breaker". Day-aggregated upserts keep cardinality
-- bounded: one row per (day, server, tool, kind).
CREATE TABLE IF NOT EXISTS surfacing_faults (
    day         TEXT    NOT NULL,
    server      TEXT    NOT NULL,
    tool        TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    last_at     REAL    NOT NULL,
    last_recovered_at REAL,
    PRIMARY KEY (day, server, tool, kind)
);

CREATE INDEX IF NOT EXISTS idx_feedback_surfacing ON surfacing_feedback(surfacing_id);
CREATE INDEX IF NOT EXISTS idx_feedback_memory_rating ON surfacing_feedback(memory_id, rating);
CREATE INDEX IF NOT EXISTS idx_events_tool ON surfacing_events(tool);
-- #584: the stats-retention delete and get_stats both filter/order on
-- created_at; without this index each is a full scan on a large history.
CREATE INDEX IF NOT EXISTS idx_events_created ON surfacing_events(created_at);
CREATE INDEX IF NOT EXISTS idx_seen_last ON seen_memories(last_seen_at);
"""

_REQUIRED_TABLES = tuple(
    re.findall(r"CREATE TABLE IF NOT EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", _SCHEMA)
)

# Fault kinds accepted by ``record_fault``. Mirrors the degraded-dependency
# subset of the in-memory observability taxonomy: ``FAULT_SKIP_REASONS``
# (``memtomem_stm.surfacing.observability``) plus the two error outcomes.
# Healthy skips (cooldown, thresholds, no-results) stay in-memory only —
# persisting them would add per-call write traffic for signals that carry
# no "surfacing is broken" information.
FAULT_KINDS: frozenset[str] = frozenset(
    {
        "error_timeout",
        "error_other",
        "circuit_open",
        "ltm_draining",
        "ltm_unavailable",
        "ltm_call_failed",
        "ltm_parse_empty",
    }
)

DIAGNOSTIC_KINDS: frozenset[str] = frozenset(
    {
        "score_ceiling_below_min",
        "score_scale_mismatch",
    }
)
"""Advisory signals stored in ``surfacing_faults`` for schema reuse.

They are partitioned from real degraded-dependency faults at read time so the
CLI never describes a healthy-but-miscalibrated search as a timeout/failure.

``score_ceiling_below_min`` is the streak heuristic (five consecutive
non-empty searches under the threshold, scale unknown);
``score_scale_mismatch`` is its definitive tier — the core NAMED a non-RRF
``score_scale`` (#1781) while the ceiling sat below the RRF-calibrated
``min_score``, so it fires on first observation without streak evidence.
"""


def _relax_surfacing_events_query_notnull(db: sqlite3.Connection) -> None:
    """Migrate the legacy NOT NULL constraint off ``surfacing_events.query``.

    Pre-#352 schemas declared ``query TEXT NOT NULL`` because the column
    was assumed to be load-bearing for stats. The #352-part-2 retention
    workflow needs to clear the column on rows older than the retention
    window while keeping the row itself for aggregate counts — UPDATE-to-
    NULL is rejected by the legacy NOT NULL, so the constraint has to
    come off on existing DBs too. SQLite ``ALTER TABLE`` cannot relax a
    column-level constraint in place; the standard recipe is to recreate
    the table without it and copy rows over.

    No-op when ``surfacing_events.query`` is already nullable (fresh DBs
    created from the current ``_SCHEMA`` definition).
    """
    row = db.execute(
        "SELECT \"notnull\" FROM pragma_table_info('surfacing_events') WHERE name = 'query'"
    ).fetchone()
    if row is None or row[0] == 0:
        # column missing (fresh CREATE just ran with the relaxed schema, or
        # the table genuinely doesn't have a `query` column on some future
        # variant) — nothing to migrate.
        return
    db.executescript(
        """
        BEGIN IMMEDIATE;
        CREATE TABLE surfacing_events__migrate_352 (
            id          TEXT    PRIMARY KEY,
            server      TEXT    NOT NULL,
            tool        TEXT    NOT NULL,
            query       TEXT,
            memory_ids  TEXT    NOT NULL,
            scores      TEXT    NOT NULL,
            created_at  REAL    NOT NULL
        );
        INSERT INTO surfacing_events__migrate_352
            (id, server, tool, query, memory_ids, scores, created_at)
        SELECT id, server, tool, query, memory_ids, scores, created_at
        FROM surfacing_events;
        DROP TABLE surfacing_events;
        ALTER TABLE surfacing_events__migrate_352 RENAME TO surfacing_events;
        CREATE INDEX IF NOT EXISTS idx_events_tool ON surfacing_events(tool);
        -- Recreate the #584 created_at index too: DROP TABLE above dropped the
        -- one _SCHEMA created, and initialize() does not re-run _SCHEMA after
        -- this migration.
        CREATE INDEX IF NOT EXISTS idx_events_created ON surfacing_events(created_at);
        COMMIT;
        """
    )
    logger.info("Migrated surfacing_events: relaxed NOT NULL on query column (#352 part 2)")


def _add_fault_recovery_column(db: sqlite3.Connection) -> None:
    """Add the episode recovery marker to databases created by older STM versions."""
    columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info('surfacing_faults')").fetchall()
    }
    if columns and "last_recovered_at" not in columns:
        db.execute("ALTER TABLE surfacing_faults ADD COLUMN last_recovered_at REAL")
        db.commit()


def _add_events_score_scale_column(db: sqlite3.Connection) -> None:
    """Add the core-reported score-scale label (#1781) to pre-existing databases.

    Ordering is load-bearing: this must run AFTER
    :func:`_relax_surfacing_events_query_notnull` in ``initialize()`` — that
    migration recreates ``surfacing_events`` from a hardcoded pre-#352 column
    list, so a column added before it would be silently dropped on legacy
    NOT-NULL databases.
    """
    columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info('surfacing_events')").fetchall()
    }
    if columns and "score_scale" not in columns:
        db.execute("ALTER TABLE surfacing_events ADD COLUMN score_scale TEXT")
        db.commit()


class FeedbackDbStatus(TypedDict):
    """Read-only schema snapshot returned by :func:`inspect_feedback_db`."""

    path: str
    exists: bool
    initialized: bool
    missing_tables: list[str]
    error: str | None


def inspect_feedback_db(db_path: Path) -> FeedbackDbStatus:
    """Inspect surfacing feedback DB schema without creating or migrating it."""
    resolved = db_path.expanduser().resolve()
    status: FeedbackDbStatus = {
        "path": str(resolved),
        "exists": resolved.exists(),
        "initialized": False,
        "missing_tables": list(_REQUIRED_TABLES),
        "error": None,
    }
    if not resolved.exists():
        return status

    try:
        db = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        status["error"] = str(exc)
        return status

    try:
        placeholders = ", ".join("?" for _ in _REQUIRED_TABLES)
        rows = db.execute(
            f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})",
            _REQUIRED_TABLES,
        ).fetchall()
    except sqlite3.Error as exc:
        status["error"] = str(exc)
        return status
    finally:
        db.close()

    present = {row[0] for row in rows}
    missing = [name for name in _REQUIRED_TABLES if name not in present]
    status["missing_tables"] = missing
    status["initialized"] = not missing
    return status


def read_surfacing_summary(db_path: Path, tool: str | None = None) -> dict[str, object]:
    """Read surfacing event + feedback aggregates read-only from disk.

    Like :func:`inspect_feedback_db`, opens the DB read-only via ``?mode=ro``
    and never creates or migrates it. Deliberately excludes the ``recent``
    query previews that :meth:`FeedbackStore.get_stats` can surface — a stats
    summary must not leak (possibly unredacted) query text — so only counts
    and the rating distribution are returned.

    ``available`` is ``False`` when the file is missing or has no
    ``surfacing_events`` table. The optional ``tool`` filter matches the raw
    tool name.
    """
    resolved = db_path.expanduser().resolve()
    summary: dict[str, object] = {
        "path": str(resolved),
        "available": False,
        "events_total": 0,
        "distinct_tools": 0,
        "total_feedback": 0,
        "rating_distribution": {},
        "faults": {},
        "faults_last_at": None,
        "faults_window_days": _FAULT_SUMMARY_WINDOW_DAYS,
        "active_faults": {},
        "faults_recovery_supported": True,
        "diagnostics": {},
        "diagnostics_last_at": None,
        "active_diagnostics": {},
        "diagnostics_recovery_supported": True,
        "diagnostics_window_days": _FAULT_SUMMARY_WINDOW_DAYS,
        "error": None,
    }
    if not resolved.exists():
        return summary

    try:
        db = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        summary["error"] = str(exc)
        return summary

    try:
        tables = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "surfacing_events" not in tables:
            return summary

        # Schema-capability probe, hoisted above every empty-result early return:
        # the ``*_recovery_supported`` flags describe the FILE, not the filter,
        # so a refused filter must not report a pre-``last_recovered_at`` DB as
        # recovery-capable. Same placement rule as ``schema_outdated`` in
        # ``read_compression_summary``. Faults and diagnostics share the column
        # but get separate flags so a reader never gates fault rendering on a
        # diagnostics-named capability.
        fault_columns: set[str] = set()
        if "surfacing_faults" in tables:
            fault_columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info('surfacing_faults')").fetchall()
            }
            recovery_supported = "last_recovered_at" in fault_columns
            summary["diagnostics_recovery_supported"] = recovery_supported
            summary["faults_recovery_supported"] = recovery_supported

        if tool is not None and has_lone_surrogate(tool):
            # Cannot be bound as a SQLite parameter and can never match a stored
            # row, so report empty-but-available rather than raising.
            summary["available"] = True
            return summary

        params: list[object] = []
        where = ""
        if tool is not None:
            where = " WHERE tool = ?"
            params.append(tool)
        summary["events_total"] = db.execute(
            f"SELECT COUNT(*) FROM surfacing_events{where}", params
        ).fetchone()[0]
        summary["distinct_tools"] = db.execute(
            f"SELECT COUNT(DISTINCT tool) FROM surfacing_events{where}", params
        ).fetchone()[0]

        if "surfacing_feedback" in tables:
            if tool is not None:
                rating_rows = db.execute(
                    "SELECT f.rating, COUNT(*) FROM surfacing_feedback f "
                    "JOIN surfacing_events e ON f.surfacing_id = e.id "
                    "WHERE e.tool = ? GROUP BY f.rating",
                    (tool,),
                ).fetchall()
            else:
                rating_rows = db.execute(
                    "SELECT rating, COUNT(*) FROM surfacing_feedback GROUP BY rating"
                ).fetchall()
            distribution = {row[0]: row[1] for row in rating_rows}
            summary["rating_distribution"] = distribution
            summary["total_feedback"] = sum(distribution.values())

        # Fault counters (durable degraded-dependency signal). Guarded on
        # table presence like ``surfacing_feedback`` above: a DB last written
        # by a pre-faults version simply reports empty counters rather than
        # erroring the whole summary.
        if "surfacing_faults" in tables:
            # Filter on the ``day`` column (calendar-day granularity matching
            # the row aggregation), not a sub-day ``last_at`` cutoff that would
            # over-count a boundary day's whole bucket. Lower bound is inclusive
            # of today plus the prior WINDOW-1 days.
            cutoff_day = time.strftime(
                "%Y-%m-%d",
                time.gmtime(time.time() - (_FAULT_SUMMARY_WINDOW_DAYS - 1) * 86400.0),
            )
            fault_where = " WHERE day >= ?"
            fault_params: list[object] = [cutoff_day]
            if tool is not None:
                fault_where += " AND tool = ?"
                fault_params.append(tool)
            signal_rows = db.execute(
                "SELECT kind, SUM(count), MAX(last_at) FROM surfacing_faults"
                f"{fault_where} GROUP BY kind",
                fault_params,
            ).fetchall()
            fault_rows = [row for row in signal_rows if row[0] in FAULT_KINDS]
            diagnostic_rows = [row for row in signal_rows if row[0] in DIAGNOSTIC_KINDS]
            summary["faults"] = {row[0]: row[1] for row in fault_rows}
            summary["faults_last_at"] = max((row[2] for row in fault_rows), default=None)
            summary["faults_window_days"] = _FAULT_SUMMARY_WINDOW_DAYS
            summary["diagnostics"] = {row[0]: row[1] for row in diagnostic_rows}
            summary["diagnostics_last_at"] = max((row[2] for row in diagnostic_rows), default=None)
            summary["diagnostics_window_days"] = _FAULT_SUMMARY_WINDOW_DAYS
            if "last_recovered_at" in fault_columns:
                # One episode-aware pass over both partitions: a kind is still
                # "active" when its newest occurrence postdates its newest
                # recovery. Partitioned in Python like ``signal_rows`` above so
                # faults and diagnostics stay separable for readers that must
                # never describe a miscalibrated-but-healthy search as a fault.
                #
                # Episodes are per ``(server, tool, kind)``, so the HAVING must
                # run in an inner query grouped that way and only THEN roll up
                # by kind. Comparing the maxima of an already-kind-wide group
                # lets one key's newer recovery cancel out another key's older
                # but still-open fault — a false all-clear whenever two servers
                # or tools share a kind.
                active_rows = db.execute(
                    "SELECT kind, SUM(events) FROM ("
                    "SELECT server, tool, kind, SUM(count) AS events "
                    "FROM surfacing_faults"
                    f"{fault_where} GROUP BY server, tool, kind "
                    "HAVING MAX(last_at) > COALESCE(MAX(last_recovered_at), 0)"
                    ") GROUP BY kind",
                    fault_params,
                ).fetchall()
                summary["active_diagnostics"] = {
                    row[0]: row[1] for row in active_rows if row[0] in DIAGNOSTIC_KINDS
                }
                summary["active_faults"] = {
                    row[0]: row[1] for row in active_rows if row[0] in FAULT_KINDS
                }
            # No ``else``: the ``*_recovery_supported`` flags were already set
            # from ``fault_columns`` above, before any early return could skip
            # them.

        summary["available"] = True
    except sqlite3.Error as exc:
        summary["error"] = str(exc)
        return summary
    finally:
        db.close()

    return summary


class FeedbackStore:
    """SQLite store for surfacing events and feedback ratings.

    **Thread-safe, and the writes are meant to run off the event loop.**
    Every method is synchronous, but a caller living on an asyncio loop must
    hand the write paths (``record_surfacing`` / ``record_feedback`` /
    ``mark_surfaced`` / ``save_adjustment`` / the fault counters / the
    ``cleanup_*`` sweeps) to the shared worker in
    ``memtomem_stm.surfacing.store_io``. They used to run inline, which was
    accepted while one MCP client served one call at a time; #874 made the
    daemon run several surfacing calls at once, and the write lock here is
    file-wide across every process pointing at this DB (the proxy's own store,
    ``mms tune``'s retention purge). A write that waits out a peer inside
    ``busy_timeout`` froze the whole loop — including the ``asyncio.timeout_at``
    timers meant to shed the requests that were piling up behind it (#996).

    Two connections make that split safe:

    - ``_db`` is the writer, serialized by ``_lock``. Schema and migrations run
      on it during :meth:`initialize`, before anything else can reach it.
    - ``_read_db`` is a second connection for the read/stat methods, serialized
      by ``_read_lock``. Under WAL a reader never waits on a writer, so a read
      left on the loop cannot block behind a worker write that is waiting out a
      peer. It opens with a deliberately short lock budget: the only thing it
      can still wait for is WAL recovery, and a loop-side read must not spend
      the writer's multi-second budget on that. Two readers do still exclude
      each other — the loop's demotion lookup can wait for the tuner's counts
      on the worker — but the lock is taken per method, and the methods that
      run there are indexed aggregates over a retention-bounded table, so the
      wait is one query rather than a lock budget. Methods issuing several
      queries take :meth:`_reading` instead, which pins one snapshot for the
      answer.

    Both connections are ``check_same_thread=False`` and are re-read *inside*
    the lock by every method, so a :meth:`close` racing an in-flight write on
    the worker degrades to this class's documented no-op instead of raising
    from a half-closed connection.

    Four writes still run on the caller's thread, all of them outside request
    service and named here so the exception list stays honest:
    :meth:`initialize` (schema and migrations), the engine constructor's
    startup retention sweep, and ``AutoTuner``'s re-clamp of persisted
    adjustments — those three run while the server is being built, before it
    can accept a call — plus :meth:`close` at teardown.
    """

    _READ_BUSY_TIMEOUT_MS = 250

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: sqlite3.Connection | None = None
        self._read_db: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._read_lock = threading.Lock()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(str(self._db_path), check_same_thread=False)
        read_db: sqlite3.Connection | None = None
        try:
            ensure_private_db_files(self._db_path)
            tune_connection(db)
            db.executescript(_SCHEMA)
            _relax_surfacing_events_query_notnull(db)
            _add_fault_recovery_column(db)
            # Must stay after the relax migration — see its docstring.
            _add_events_score_scale_column(db)
            # Opened only once the schema is final: the reader runs no DDL of
            # its own, so it must never observe a half-migrated table.
            read_db = sqlite3.connect(str(self._db_path), check_same_thread=False)
            tune_connection(read_db, busy_timeout_ms=self._READ_BUSY_TIMEOUT_MS)
        except Exception:
            if read_db is not None:
                read_db.close()
            db.close()
            raise
        self._db = db
        self._read_db = read_db

    @contextlib.contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection | None]:
        """Read under one snapshot, for the methods that issue several queries.

        Writes now land on a worker thread, so a peer statement can commit
        between two of a reader's queries and hand back an answer that
        contradicts itself — an event total that predates the per-tool
        breakdown counted beside it. An explicit read transaction pins one
        version of the database for the whole method; under WAL it blocks no
        writer, it only stops this reader from seeing that writer land
        mid-answer. Yields ``None`` when the store is closed, which is every
        caller's existing empty-result path.
        """
        with self._read_lock:
            db = self._read_db
            if db is None:
                yield None
                return
            db.execute("BEGIN")
            try:
                yield db
            finally:
                # Read-only: rollback is how the snapshot is released.
                db.rollback()

    def close(self) -> None:
        """Close both connections, waiting out an in-flight write.

        Taking ``_lock`` is what makes the wait: a write already running on the
        worker finishes and commits rather than having the connection closed
        from under it. How long that takes is the statement's own business —
        ``busy_timeout`` caps waiting for the file's lock, not the runtime of a
        wide ``DELETE`` that already holds it — so this is not a bounded wait.
        It is paid at teardown, when this process has no requests left to
        serve, and the engine's ``stop()`` has already given the queue a
        bounded chance to drain before anyone calls this.

        A write queued but not started is a different case: it runs after this
        returns, finds a closed store, and takes the no-op path every method
        documents. The engine's two-statement delivery write (the event row
        then the dedup rows) can therefore be split by a ``close`` that lands
        between them, leaving an event row whose memories were never marked
        seen. The cost is one memory that may surface again in the next
        session; the alternative — holding one lock across both — would make a
        teardown wait on the wider write rather than the narrower one.
        """
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
        with self._read_lock:
            if self._read_db is not None:
                self._read_db.close()
                self._read_db = None

    def record_surfacing(
        self,
        surfacing_id: str,
        server: str,
        tool: str,
        query: str,
        memory_ids: list[str],
        scores: list[float],
        score_scale: str | None = None,
    ) -> bool:
        """Write one surfacing event row. ``False`` when the store is closed.

        The caller advertises this row's ID to the agent, so "closed" cannot
        be a silent success: a teardown that closes the store while a call is
        still in flight would otherwise leave the agent holding a feedback
        handle that resolves to nothing.
        """
        if self._db is None:
            return False
        require_utf8_identifier(surfacing_id, "surfacing_id")
        require_utf8_identifier(server, "server")
        require_utf8_identifier(tool, "tool")
        for index, memory_id in enumerate(memory_ids):
            require_utf8_identifier(memory_id, f"memory_ids[{index}]")
        safe_query = escape_lone_surrogates(query)
        safe_score_scale = escape_lone_surrogates(score_scale) if score_scale is not None else None
        with self._lock:
            db = self._db
            if db is None:
                return False
            db.execute(
                "INSERT OR IGNORE INTO surfacing_events "
                "(id, server, tool, query, memory_ids, scores, created_at, score_scale) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    surfacing_id,
                    server,
                    tool,
                    safe_query,
                    json.dumps(memory_ids),
                    json.dumps(scores),
                    time.time(),
                    safe_score_scale,
                ),
            )
            db.commit()
        return True

    def record_fault(self, server: str, tool: str, kind: str, *, at: float | None = None) -> None:
        """Increment the durable per-day fault counter for (server, tool, kind).

        Unknown *kind* values are dropped (defensively, not raised): the
        caller sits on the surfacing hot path's failure branches, where a
        taxonomy drift must degrade to a missing counter, never to a new
        exception. Day buckets are UTC so counters aggregate stably across
        processes regardless of host timezone.

        A new fault reopens the episode by clearing the row's recovery stamp
        (``reset_recovery``), mirroring :meth:`record_diagnostic`: readers
        treat a kind as active while its newest occurrence postdates its
        newest recovery, so a re-break must not read as still-recovered.

        *at* is when the fault was observed, defaulting to now. A caller that
        queues this write on a worker must pass the observation time: the
        recovery guard compares ``last_at <= recovered_at``, so a fault that
        executes late — after the caller took the timestamp for a later
        recovery — would otherwise stamp a row the recovery can no longer
        close, and the episode would read active on evidence that disproved it.

        This orders a queue against itself, not against a peer process. A
        recovery a PEER wrote while this fault sat in the queue updates rows
        that exist at that moment, so it cannot match a row this write has yet
        to insert, and the insert then opens an episode the peer's success had
        already answered. Nothing in a shared day-aggregated row can close that
        window — the peer would have to leave a recovery watermark for a key
        with no row yet. It stays open only until the next healthy round trip
        on that key, in either process, because the recovery write is
        deliberately unlatched and re-runs on every success.
        """
        self._record_signal(server, tool, kind, FAULT_KINDS, reset_recovery=True, at=at)

    def record_diagnostic(
        self, server: str, tool: str, kind: str, *, at: float | None = None
    ) -> None:
        """Increment a durable advisory diagnostic counter.

        Diagnostics share the day-aggregated fault table for bounded storage,
        but readers partition them so operator guidance remains accurate.
        *at* carries the observation time for a queued write, exactly as in
        :meth:`record_fault`.
        """
        self._record_signal(
            server,
            tool,
            kind,
            DIAGNOSTIC_KINDS,
            reset_recovery=True,
            at=at,
        )

    def record_diagnostic_recovery(self, server: str, tool: str, kind: str) -> None:
        """Mark all existing rows for one diagnostic episode as recovered.

        Thin wrapper over :meth:`record_diagnostic_recoveries` so the
        already-closed guard and the kind filter live in one place.
        """
        self.record_diagnostic_recoveries(server, tool, frozenset({kind}), recovered_at=time.time())

    def record_diagnostic_recoveries(
        self,
        server: str,
        tool: str,
        kinds: frozenset[str] = DIAGNOSTIC_KINDS,
        *,
        recovered_at: float,
    ) -> None:
        """Close one key's diagnostic episodes in ONE statement.

        The engine writes this on every healthy or scale-suspended batch
        rather than latching "already recovered" per process, mirroring
        :meth:`record_fault_recovery`: the rows are shared by every process
        pointing at this DB, so a per-process latch goes stale the moment a
        peer reopens the episode. Already-closed rows are left alone, so the
        repeat writes that rule implies cost one statement matching no row.
        """
        self._close_episodes(
            ((server, tool, kinds),),
            DIAGNOSTIC_KINDS,
            recovered_at=recovered_at,
            what="diagnostic recovery",
        )

    def record_fault_recovery(
        self,
        server: str,
        tool: str,
        *,
        recovered_at: float,
        kinds: frozenset[str] = FAULT_KINDS,
    ) -> None:
        """Mark the fault episodes disproved by a successful surfacing closed.

        By default closes *every* :data:`FAULT_KINDS` episode for
        ``(server, tool)`` in one statement: the caller has proved a full LTM
        round trip succeeded, which disproves each degraded-dependency kind at
        once, and they are not independently observable from the success side.
        The engine narrows *kinds* to ``circuit_open`` for the keys its breaker
        blocked, which the success proves nothing else about.

        Always keyed — an un-keyed sweep would let one process's healthy round
        trip stamp a peer's still-open episode, and the rows are shared by
        everything pointing at this DB.

        *recovered_at* is the moment the round trip succeeded, not the moment
        of this write, and rows are matched with ``last_at <= recovered_at``:
        a fault recorded after that instant — by this process or a peer, whose
        writes this store's lock does not order — stays active, because the
        success is no evidence about it. A fault sharing the timestamp exactly,
        which a coarse clock makes possible, is stamped recovered; these
        counters are advisory and the next fault reopens the episode.

        Already-closed rows are left alone (``last_recovered_at`` is only
        advanced while the episode is open), so re-running this on an
        unchanged key writes nothing.

        A row is a day-aggregate shared by every process writing this DB, so
        closing it closes what a peer recorded for the same key too. That is
        the granularity the table has; the peer's next fault on that key
        reopens the episode through :meth:`record_fault`'s reset.
        """
        self.record_fault_recoveries(((server, tool, kinds),), recovered_at=recovered_at)

    def record_fault_recoveries(
        self,
        entries: Iterable[tuple[str, str, frozenset[str]]],
        *,
        recovered_at: float,
    ) -> None:
        """Close several keys' episodes in ONE transaction.

        The engine closes the successful key and the keys its breaker turned
        away together. Committing them separately would let a mid-batch
        failure leave the DB half-recovered, and a restart before the retry
        would show the survivors as broken with the breaker long closed.
        Per-key semantics are :meth:`record_fault_recovery`'s.
        """
        self._close_episodes(entries, FAULT_KINDS, recovered_at=recovered_at, what="fault recovery")

    def _close_episodes(
        self,
        entries: Iterable[tuple[str, str, frozenset[str]]],
        allowed_kinds: frozenset[str],
        *,
        recovered_at: float,
        what: str,
    ) -> None:
        """Close every entry's still-open episodes in one transaction.

        Shared by the fault and diagnostic recovery paths so the WHERE guard
        — including ``last_recovered_at IS NULL OR last_recovered_at <
        last_at``, which makes a repeat write match nothing — cannot drift
        between them. *allowed_kinds* keeps each caller inside its own
        taxonomy: a fault recovery must never stamp a diagnostic row.
        """
        if self._db is None:
            return
        statements: list[tuple[str, tuple[object, ...]]] = []
        for server, tool, kinds in entries:
            if has_lone_surrogate(server) or has_lone_surrogate(tool):
                continue
            selected = sorted(kinds & allowed_kinds)
            if not selected:
                continue
            kind_placeholders = ", ".join("?" for _ in selected)
            statements.append(
                (
                    "UPDATE surfacing_faults SET last_recovered_at = ? "
                    f"WHERE server = ? AND tool = ? AND kind IN ({kind_placeholders}) "
                    "AND last_at <= ? "
                    "AND (last_recovered_at IS NULL OR last_recovered_at < last_at)",
                    (recovered_at, server, tool, *selected, recovered_at),
                )
            )
        if not statements:
            return
        with self._lock:
            db = self._db
            if db is None:
                return
            try:
                for sql, params in statements:
                    db.execute(sql, params)
                db.commit()
            except Exception:
                self._abandon_transaction(db, what)
                raise

    def _abandon_transaction(self, db: sqlite3.Connection, what: str) -> None:
        """Leave no pending transaction behind after a failed write.

        A failing ``execute`` or ``commit`` leaves the transaction OPEN, and
        the next unrelated write on this connection commits those rows as a
        side effect of its own work — a fault counter silently publishing a
        half-applied recovery, say. Rolling back is what prevents that.

        A rollback that ITSELF fails is only logged. Dropping the connection
        looks safer but is not: a ``None`` connection makes every write a SILENT
        no-op, and ``record_surfacing`` returning without writing leaves the
        agent holding an advertised feedback ID that resolves to nothing —
        whereas a raising write makes the engine re-render without the dead
        handle. A connection whose rollback fails is broken anyway, so its next
        write raises and degrades through the paths that already exist.

        Called from inside ``self._lock`` with the connection the failed write
        used, so a concurrent :meth:`close` cannot swap it for ``None`` between
        the failure and the rollback; never raises, so the caller's original
        exception is what propagates.
        """
        try:
            db.rollback()
        except Exception:
            logger.warning(
                "Rollback after a failed %s write failed; the surfacing feedback "
                "connection may hold an uncommitted transaction",
                what,
                exc_info=True,
            )

    def _record_signal(
        self,
        server: str,
        tool: str,
        kind: str,
        allowed_kinds: frozenset[str],
        *,
        reset_recovery: bool = False,
        at: float | None = None,
    ) -> None:
        if self._db is None or kind not in allowed_kinds:
            return
        if has_lone_surrogate(server) or has_lone_surrogate(tool):
            return
        now = time.time() if at is None else at
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        # A queued write can execute after a peer already recorded a NEWER
        # observation for the same row, so neither field may be assigned
        # unconditionally. ``last_at`` keeps the newest of the two — moving it
        # backward would let a recovery taken between them satisfy
        # ``last_at <= recovered_at`` and close an episode the peer's fault
        # should hold open. The recovery stamp clears only for a fault that is
        # not OLDER than it: ties still reopen, which is the documented rule on
        # a clock too coarse to separate two adjacent writes (see
        # ``record_fault_recovery``), while a fault that recovery already
        # answered no longer reopens the episode after the fact.
        recovery_update = (
            ", last_recovered_at = CASE"
            " WHEN excluded.last_at >= COALESCE(surfacing_faults.last_recovered_at, 0)"
            " THEN NULL ELSE surfacing_faults.last_recovered_at END"
            if reset_recovery
            else ""
        )
        with self._lock:
            db = self._db
            if db is None:
                return
            try:
                db.execute(
                    "INSERT INTO surfacing_faults (day, server, tool, kind, count, last_at) "
                    "VALUES (?, ?, ?, ?, 1, ?) "
                    "ON CONFLICT(day, server, tool, kind) "
                    "DO UPDATE SET count = count + 1, "
                    "last_at = MAX(surfacing_faults.last_at, excluded.last_at)" + recovery_update,
                    (day, server, tool, kind, now),
                )
                db.commit()
            except Exception:
                # Same guarantee the recovery batch gets: a failed counter
                # write must not leave a row pending for someone else's commit
                # to publish. The engine's breaker bookkeeping reads a raised
                # exception as "nothing landed", so that has to be true.
                self._abandon_transaction(db, "fault counter")
                raise

    def delete_faults_older_than(self, retention_seconds: float) -> int:
        """Delete day-aggregated fault rows whose ``last_at`` is past the
        retention window. Returns the number of rows deleted. Rows are one
        per (day, server, tool, kind), so the table stays tiny even before
        cleanup — this bound exists for symmetry with the #584 event-row
        retention, not because the scan cost is material.
        """
        if self._db is None or retention_seconds <= 0:
            return 0
        cutoff = time.time() - retention_seconds
        with self._lock:
            db = self._db
            if db is None:
                return 0
            cur = db.execute("DELETE FROM surfacing_faults WHERE last_at < ?", (cutoff,))
            db.commit()
        return cur.rowcount if cur.rowcount is not None and cur.rowcount > 0 else 0

    def record_feedback(
        self,
        surfacing_id: str,
        rating: str,
        memory_id: str | None = None,
    ) -> bool:
        if self._db is None:
            return False
        if has_lone_surrogate(surfacing_id):
            return False
        if memory_id is not None and has_lone_surrogate(memory_id):
            return False
        with self._lock:
            db = self._db
            if db is None:
                return False
            # Verify surfacing event exists
            event = db.execute(
                "SELECT memory_ids FROM surfacing_events WHERE id = ?", (surfacing_id,)
            ).fetchone()
            if not event:
                return False
            if memory_id is not None:
                try:
                    event_memory_ids = json.loads(event[0])
                except (json.JSONDecodeError, TypeError):
                    return False
                if memory_id not in event_memory_ids:
                    return False
            db.execute(
                "INSERT INTO surfacing_feedback (surfacing_id, memory_id, rating, created_at) "
                "VALUES (?, ?, ?, ?)",
                (surfacing_id, memory_id, rating, time.time()),
            )
            db.commit()
        return True

    def get_memory_ids_for_surfacing(self, surfacing_id: str) -> list[str]:
        """Return memory_ids from a surfacing event."""
        with self._read_lock:
            db = self._read_db
            if db is None or has_lone_surrogate(surfacing_id):
                return []
            row = db.execute(
                "SELECT memory_ids FROM surfacing_events WHERE id = ?", (surfacing_id,)
            ).fetchone()
            if not row:
                return []
            return _load_safe_memory_ids(row[0])

    def get_feedback_count(self, tool: str | None = None) -> int:
        """Return the durable feedback watermark for auto-tuning."""
        with self._read_lock:
            db = self._read_db
            if db is None:
                return 0
            if tool is None:
                row = db.execute("SELECT COUNT(*) FROM surfacing_feedback").fetchone()
            else:
                if has_lone_surrogate(tool):
                    return 0
                row = db.execute(
                    "SELECT COUNT(*) FROM surfacing_feedback f "
                    "JOIN surfacing_events e ON e.id = f.surfacing_id WHERE e.tool = ?",
                    (tool,),
                ).fetchone()
            return int(row[0]) if row else 0

    def get_negative_feedback_counts(self, memory_ids: list[str]) -> dict[str, int]:
        """Return durable negative-feedback event counts for memory IDs.

        Counts distinct ``surfacing_id`` values, not raw feedback rows, so
        repeated submissions for the same surfacing event cannot trigger
        demotion by themselves. Explicit per-memory feedback is counted
        directly from ``surfacing_feedback.memory_id``. Legacy blanket
        negatives (``memory_id IS NULL``) are expanded from the parent
        event's ``memory_ids`` JSON without relying on SQLite JSON1.
        """
        with self._reading() as db:
            if db is None or not memory_ids:
                return {}

            # Drop the unencodable ids, not the batch. They cannot be bound as
            # SQLite parameters, but they also cannot match a stored row — the
            # write paths refuse them — so their count is known to be 0 without
            # asking. Failing the whole call instead would answer 0 for every
            # *valid* id too, and the caller reads that as "nothing has enough
            # negatives": a memory the agent rated ``not_relevant`` past the
            # threshold would resurface for as long as one bad id rode along in
            # the same candidate set. Same leaf-filtering shape as
            # ``_load_safe_memory_ids``.
            target_ids = [
                mid
                for mid in dict.fromkeys(str(mid) for mid in memory_ids)
                if not has_lone_surrogate(mid)
            ]
            if not target_ids:
                return {}
            event_ids_by_memory: dict[str, set[str]] = {mid: set() for mid in target_ids}
            target_set = set(target_ids)

            placeholders = ", ".join("?" for _ in target_ids)
            rating_placeholders = ", ".join("?" for _ in _NEGATIVE_FEEDBACK_RATINGS)
            explicit_rows = db.execute(
                "SELECT DISTINCT memory_id, surfacing_id FROM surfacing_feedback "
                f"WHERE memory_id IN ({placeholders}) "
                f"AND rating IN ({rating_placeholders})",
                (*target_ids, *_NEGATIVE_FEEDBACK_RATINGS),
            ).fetchall()
            for memory_id, surfacing_id in explicit_rows:
                event_ids_by_memory[str(memory_id)].add(str(surfacing_id))

            blanket_rows = db.execute(
                "SELECT DISTINCT f.surfacing_id, e.memory_ids FROM surfacing_feedback f "
                "JOIN surfacing_events e ON f.surfacing_id = e.id "
                "WHERE f.memory_id IS NULL "
                f"AND f.rating IN ({rating_placeholders})",
                _NEGATIVE_FEEDBACK_RATINGS,
            ).fetchall()
            for surfacing_id, event_memory_ids_json in blanket_rows:
                try:
                    event_memory_ids = json.loads(event_memory_ids_json)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(event_memory_ids, list):
                    continue
                for memory_id in event_memory_ids:
                    mid = str(memory_id)
                    if mid in target_set:
                        event_ids_by_memory[mid].add(str(surfacing_id))

            return {mid: len(event_ids) for mid, event_ids in event_ids_by_memory.items()}

    def get_surfacing_event(self, surfacing_id: str) -> dict | None:
        """Return ``{server, tool, memory_ids}`` for a surfacing event.

        Used by cache-invalidation on negative feedback — the feedback
        handler needs (server, tool) along with memory_ids to key the
        in-memory invalidation set against ``SurfacingCache`` entries.
        Returns ``None`` if the event does not exist.
        """
        with self._read_lock:
            db = self._read_db
            if db is None or has_lone_surrogate(surfacing_id):
                return None
            row = db.execute(
                "SELECT server, tool, memory_ids FROM surfacing_events WHERE id = ?",
                (surfacing_id,),
            ).fetchone()
            if not row:
                return None
            try:
                memory_ids = json.loads(row[2])
            except (json.JSONDecodeError, TypeError):
                memory_ids = []
            return {"server": row[0], "tool": row[1], "memory_ids": memory_ids}

    def get_tool_feedback_summary(self, tool: str | None = None) -> dict:
        """Get feedback summary, optionally filtered by tool."""
        with self._reading() as db:
            if db is None:
                return {"total_surfacings": 0, "total_feedback": 0, "by_rating": {}}
            if tool is not None and has_lone_surrogate(tool):
                return {"total_surfacings": 0, "total_feedback": 0, "by_rating": {}}

            if tool:
                total_surfacings = db.execute(
                    "SELECT COUNT(*) FROM surfacing_events WHERE tool = ?", (tool,)
                ).fetchone()[0]
                rows = db.execute(
                    "SELECT f.rating, COUNT(*) FROM surfacing_feedback f "
                    "JOIN surfacing_events e ON f.surfacing_id = e.id "
                    "WHERE e.tool = ? GROUP BY f.rating",
                    (tool,),
                ).fetchall()
            else:
                total_surfacings = db.execute("SELECT COUNT(*) FROM surfacing_events").fetchone()[0]
                rows = db.execute(
                    "SELECT rating, COUNT(*) FROM surfacing_feedback GROUP BY rating"
                ).fetchall()

            by_rating = {r[0]: r[1] for r in rows}
            total_feedback = sum(by_rating.values())

            return {
                "total_surfacings": total_surfacings,
                "total_feedback": total_feedback,
                "by_rating": by_rating,
            }

    def get_stats(
        self,
        tool: str | None = None,
        since: float | None = None,
        limit: int = 10,
    ) -> dict:
        """Aggregate surfacing_events + surfacing_feedback for observability.

        Shape mirrors ``CompressionFeedbackStore.get_stats`` in spirit but
        is wider because surfacing has a richer event record (query,
        memory_ids, scores). Empty DB / empty filter range returns zeros
        with all collections empty — callers can rely on keys always
        being present.

        Args:
            tool: If set, restrict to one upstream tool.
            since: Unix timestamp lower bound for ``created_at``.
            limit: Max rows in the ``recent`` tail (``<=0`` disables).
        """
        with self._reading() as db:
            empty = {
                "events_total": 0,
                "distinct_tools": 0,
                "date_range": {"first": None, "last": None},
                "per_tool_breakdown": [],
                "rating_distribution": {},
                "total_feedback": 0,
                "recent": [],
                "score_distribution": {"count": 0, "min": None, "max": None},
                "score_scale_distribution": {},
            }
            if db is None:
                return empty
            if tool is not None and has_lone_surrogate(tool):
                return empty

            event_filters: list[str] = []
            event_params: list[object] = []
            if tool is not None:
                event_filters.append("tool = ?")
                event_params.append(tool)
            if since is not None:
                event_filters.append("created_at >= ?")
                event_params.append(since)
            where_sql = (" WHERE " + " AND ".join(event_filters)) if event_filters else ""

            events_total = db.execute(
                f"SELECT COUNT(*) FROM surfacing_events{where_sql}", event_params
            ).fetchone()[0]

            if events_total == 0:
                # Still surface feedback with zero events? No — feedback rows
                # without their parent event in the filter range aren't
                # meaningful here. Return empty shape.
                return empty

            distinct_tools = db.execute(
                f"SELECT COUNT(DISTINCT tool) FROM surfacing_events{where_sql}", event_params
            ).fetchone()[0]

            first, last = db.execute(
                f"SELECT MIN(created_at), MAX(created_at) FROM surfacing_events{where_sql}",
                event_params,
            ).fetchone()

            # Per-tool: events + average memory_ids length. Average is computed
            # in Python because memory_ids is JSON-encoded and SQLite's JSON1
            # extension isn't universally guaranteed on the shipping wheels.
            # The same pass aggregates the score distribution (count/min/max)
            # for the flat-score tripwire (#560): min == max over a large
            # enough sample means the upstream score channel carries no
            # ranking information. min/max is O(1) memory and is exactly the
            # "all scores equal" predicate — no need to hold the value set.
            rows = db.execute(
                f"SELECT tool, memory_ids, scores, score_scale FROM surfacing_events{where_sql}",
                event_params,
            ).fetchall()
            per_tool: dict[str, dict[str, float]] = {}
            score_count = 0
            score_min: float | None = None
            score_max: float | None = None
            # Per-event count of the core-reported scale label (#1781). NULL rows
            # (pre-#1781 cores, compose bundles, legacy events) bucket under
            # "unknown" so the distribution always sums to events_total.
            score_scale_distribution: dict[str, int] = {}
            for tool_name, memory_ids_json, scores_json, score_scale in rows:
                scale_key = (
                    escape_lone_surrogates(score_scale)
                    if isinstance(score_scale, str) and score_scale
                    else "unknown"
                )
                score_scale_distribution[scale_key] = score_scale_distribution.get(scale_key, 0) + 1
                n = len(_load_safe_memory_ids(memory_ids_json))
                bucket = per_tool.setdefault(tool_name, {"events": 0, "sum_memory_count": 0})
                bucket["events"] += 1
                bucket["sum_memory_count"] += n
                for score in _load_numeric_scores(scores_json):
                    score_count += 1
                    score_min = score if score_min is None else min(score_min, score)
                    score_max = score if score_max is None else max(score_max, score)

            # Per-tool feedback counts (total + negative) within the same
            # event filter. Powers the AutoTuner readiness signal: with these
            # the formatter can render "feedback N (negative R%)" and decide
            # whether the tool has hit auto_tune_min_samples.
            per_tool_feedback_filter = " AND ".join(f"e.{f}" for f in event_filters)
            per_tool_feedback_where = (
                (" WHERE " + per_tool_feedback_filter) if per_tool_feedback_filter else ""
            )
            feedback_rows = db.execute(
                "SELECT e.tool, f.rating, COUNT(*) FROM surfacing_feedback f "
                "JOIN surfacing_events e ON f.surfacing_id = e.id"
                f"{per_tool_feedback_where} GROUP BY e.tool, f.rating",
                event_params,
            ).fetchall()
            per_tool_feedback: dict[str, dict[str, int]] = {}
            for tool_name, rating, count in feedback_rows:
                bucket_fb = per_tool_feedback.setdefault(
                    tool_name,
                    {"total": 0, "not_relevant": 0, "negative": 0},
                )
                bucket_fb["total"] += count
                if rating == "not_relevant":
                    bucket_fb["not_relevant"] += count
                if rating in _NEGATIVE_FEEDBACK_RATINGS:
                    bucket_fb["negative"] += count

            per_tool_breakdown: list[dict] = [
                {
                    "tool": t,
                    "events": int(b["events"]),
                    "avg_memory_count": round(b["sum_memory_count"] / b["events"], 2)
                    if b["events"]
                    else 0.0,
                    "feedback_count": per_tool_feedback.get(t, {}).get("total", 0),
                    "not_relevant_count": per_tool_feedback.get(t, {}).get("not_relevant", 0),
                    "negative_count": per_tool_feedback.get(t, {}).get("negative", 0),
                }
                for t, b in sorted(per_tool.items(), key=lambda kv: kv[1]["events"], reverse=True)
            ]

            # Feedback ratings JOINed against the same event filter.
            rating_join_filter = " AND ".join(f"e.{f}" for f in event_filters)
            rating_where = (" WHERE " + rating_join_filter) if rating_join_filter else ""
            rating_rows = db.execute(
                "SELECT f.rating, COUNT(*) FROM surfacing_feedback f "
                "JOIN surfacing_events e ON f.surfacing_id = e.id"
                f"{rating_where} GROUP BY f.rating",
                event_params,
            ).fetchall()
            rating_distribution = {r[0]: r[1] for r in rating_rows}
            total_feedback = sum(rating_distribution.values())

            recent: list[dict] = []
            if limit > 0:
                recent_rows = db.execute(
                    f"SELECT created_at, tool, query, memory_ids, scores, score_scale "
                    f"FROM surfacing_events{where_sql} "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    [*event_params, limit],
                ).fetchall()
                for ts, tool_name, query, memory_ids_json, scores_json, score_scale in recent_rows:
                    memory_ids = _load_safe_memory_ids(memory_ids_json)
                    scores = _load_numeric_scores(scores_json)
                    # #352 part 2: ``query`` is now nullable.
                    # ``cleanup_expired_queries`` clears the column on rows
                    # older than ``query_retention_days`` while preserving
                    # the row itself for stats aggregates, so a SELECT can
                    # legitimately yield ``None`` here. ``len(None)`` would
                    # crash ``stm_surfacing_stats`` once retention has
                    # actually swept anything — render a stable placeholder.
                    # #352 part 3: only the exact ``sha256:<16-hex>`` shape
                    # written by the engine under ``persist_query_text=False``
                    # bypasses the 80-char clip. Prefix-only matching would
                    # misclassify legitimate raw queries that happen to
                    # start with ``sha256:`` (e.g. a user-typed checksum
                    # search) and leak unbounded text under the default
                    # config. Raw text — including any ``sha256:``-prefixed
                    # user query — keeps the legacy 80-char clip.
                    if query is None:
                        preview = "<expired>"
                    elif _HASHED_QUERY_RE.fullmatch(query):
                        preview = query
                    else:
                        safe_query = escape_lone_surrogates(query)
                        preview = safe_query if len(safe_query) <= 80 else safe_query[:77] + "..."
                    recent.append(
                        {
                            "ts": ts,
                            "tool": tool_name,
                            "query_preview": preview,
                            "memory_ids": memory_ids,
                            "scores": scores,
                            "score_scale": (
                                escape_lone_surrogates(score_scale)
                                if isinstance(score_scale, str)
                                else score_scale
                            ),
                        }
                    )

            return {
                "events_total": events_total,
                "distinct_tools": distinct_tools,
                "date_range": {"first": first, "last": last},
                "per_tool_breakdown": per_tool_breakdown,
                "rating_distribution": rating_distribution,
                "total_feedback": total_feedback,
                "recent": recent,
                "score_distribution": {"count": score_count, "min": score_min, "max": score_max},
                "score_scale_distribution": score_scale_distribution,
            }

    # ── Cross-session dedup ────────────────────────────────────────────

    def mark_surfaced(self, memory_ids: list[str]) -> None:
        """Record memory IDs as surfaced for cross-session dedup."""
        if self._db is None or not memory_ids:
            return
        for index, memory_id in enumerate(memory_ids):
            require_utf8_identifier(memory_id, f"memory_ids[{index}]")
        now = time.time()
        with self._lock:
            db = self._db
            if db is None:
                return
            for mid in memory_ids:
                db.execute(
                    "INSERT INTO seen_memories (memory_id, first_seen_at, last_seen_at, seen_count) "
                    "VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(memory_id) DO UPDATE SET "
                    "last_seen_at = excluded.last_seen_at, "
                    "seen_count = seen_count + 1",
                    (mid, now, now),
                )
            db.commit()

    def get_seen_ids(self, ttl_seconds: float) -> set[str]:
        """Return memory IDs surfaced within the TTL window."""
        with self._read_lock:
            db = self._read_db
            if db is None:
                return set()
            cutoff = time.time() - ttl_seconds
            rows = db.execute(
                "SELECT memory_id FROM seen_memories WHERE last_seen_at >= ?", (cutoff,)
            ).fetchall()
            return {r[0] for r in rows}

    def cleanup_expired(self, ttl_seconds: float) -> int:
        """Delete seen_memories entries older than TTL. Returns count deleted."""
        if self._db is None:
            return 0
        cutoff = time.time() - ttl_seconds
        with self._lock:
            db = self._db
            if db is None:
                return 0
            cursor = db.execute("DELETE FROM seen_memories WHERE last_seen_at < ?", (cutoff,))
            db.commit()
            return cursor.rowcount

    def cleanup_expired_queries(self, retention_seconds: float) -> int:
        """Null out ``surfacing_events.query`` on rows older than the
        retention window. The row itself is preserved so aggregate counts
        in ``stm_surfacing_stats`` stay accurate; only the user-derived
        query text is cleared. Returns the number of rows actually
        updated (``query IS NOT NULL`` before the sweep). Issue #352
        part 2."""
        if self._db is None or retention_seconds <= 0:
            return 0
        cutoff = time.time() - retention_seconds
        with self._lock:
            db = self._db
            if db is None:
                return 0
            cursor = db.execute(
                "UPDATE surfacing_events SET query = NULL "
                "WHERE created_at < ? AND query IS NOT NULL",
                (cutoff,),
            )
            db.commit()
            return cursor.rowcount

    def delete_events_older_than(self, retention_seconds: float) -> int:
        """Delete ``surfacing_events`` (and their ``surfacing_feedback``) rows
        older than the retention window. Returns the number of event rows
        deleted (#584).

        Unlike :meth:`cleanup_expired_queries`, which only nulls the query
        column and keeps the row for aggregates, this bounds the table so
        :meth:`get_stats` cannot full-scan an unbounded history on the event
        loop. The feedback rows are removed first (they reference events by
        ``surfacing_id``), then the events, in one transaction. ``<= 0``
        disables deletion."""
        if self._db is None or retention_seconds <= 0:
            return 0
        cutoff = time.time() - retention_seconds
        with self._lock:
            db = self._db
            if db is None:
                return 0
            db.execute(
                "DELETE FROM surfacing_feedback WHERE surfacing_id IN "
                "(SELECT id FROM surfacing_events WHERE created_at < ?)",
                (cutoff,),
            )
            cursor = db.execute("DELETE FROM surfacing_events WHERE created_at < ?", (cutoff,))
            db.commit()
            return cursor.rowcount

    def _get_tool_rating_ratio(
        self,
        tool: str | None,
        ratings: tuple[str, ...],
        min_samples: int,
    ) -> float | None:
        # AutoTuner-facing ratios count only feedback earned on RRF or
        # unstamped surfacings: the tuner moves an RRF-calibrated threshold,
        # and ratings earned on a scale-gated (pass-all) batch measure a
        # different filtering policy on a different scale. The LEFT JOIN +
        # IS NULL keeps two row classes counting as before: events rows with
        # no reported scale, and orphaned feedback whose events row was aged
        # out by retention.
        with self._reading() as db:
            scale_pred = "(e.score_scale IS NULL OR e.score_scale = 'rrf')"
            if db is None:
                return None
            if tool is not None and has_lone_surrogate(tool):
                return None

            placeholders = ", ".join("?" for _ in ratings)
            if tool is not None:
                total = db.execute(
                    "SELECT COUNT(*) FROM surfacing_feedback f "
                    "JOIN surfacing_events e ON f.surfacing_id = e.id "
                    f"WHERE e.tool = ? AND {scale_pred}",
                    (tool,),
                ).fetchone()[0]
                if total < min_samples:
                    return None
                matching = db.execute(
                    "SELECT COUNT(*) FROM surfacing_feedback f "
                    "JOIN surfacing_events e ON f.surfacing_id = e.id "
                    f"WHERE e.tool = ? AND {scale_pred} AND f.rating IN ({placeholders})",
                    (tool, *ratings),
                ).fetchone()[0]
            else:
                total = db.execute(
                    "SELECT COUNT(*) FROM surfacing_feedback f "
                    "LEFT JOIN surfacing_events e ON f.surfacing_id = e.id "
                    f"WHERE {scale_pred}",
                ).fetchone()[0]
                if total < min_samples:
                    return None
                matching = db.execute(
                    "SELECT COUNT(*) FROM surfacing_feedback f "
                    "LEFT JOIN surfacing_events e ON f.surfacing_id = e.id "
                    f"WHERE {scale_pred} AND f.rating IN ({placeholders})",
                    ratings,
                ).fetchone()[0]
            return matching / total if total > 0 else 0.0

    def get_tool_negative_ratio(self, tool: str | None, min_samples: int = 20) -> float | None:
        """Return ratio of negative feedback. None if insufficient samples.

        Negative feedback is ``not_relevant`` or ``already_known``. If tool is
        None, returns the global ratio across all tools (used as a cold-start
        fallback when a specific tool has too few samples).
        """
        return self._get_tool_rating_ratio(tool, _NEGATIVE_FEEDBACK_RATINGS, min_samples)

    def get_tool_not_relevant_ratio(self, tool: str | None, min_samples: int = 20) -> float | None:
        """Return ratio of not_relevant feedback. None if insufficient samples.

        If tool is None, returns the global ratio across all tools (used
        as a cold-start fallback when a specific tool has too few samples).
        """
        return self._get_tool_rating_ratio(tool, ("not_relevant",), min_samples)

    def get_tool_helpful_ratio(self, tool: str | None, min_samples: int = 20) -> float | None:
        """Return ratio of ``helpful`` feedback. None if insufficient samples.

        Strictly counts the explicit positive signal — ``partially_helpful``
        is intentionally excluded so a tool whose feedback is mostly
        "useful context but not directly used" does not pull
        ``min_score`` down. Mirrors :meth:`get_tool_negative_ratio` for
        symmetric AutoTuner band checks after #353 part 2.
        """
        return self._get_tool_rating_ratio(tool, ("helpful",), min_samples)

    # ── AutoTuner persistence ──────────────────────────────────────────

    def load_adjustments(self) -> dict[str, float]:
        """Return persisted per-tool min_score adjustments.

        Lets ``AutoTuner`` resume after a process restart instead of
        losing every tuning decision the moment the server bounces.
        Returns ``{}`` when the store is closed or empty.
        """
        with self._read_lock:
            db = self._read_db
            if db is None:
                return {}
            rows = db.execute("SELECT tool, min_score FROM auto_tune_adjustments").fetchall()
            return {tool: score for tool, score in rows}

    def save_adjustment(self, tool: str, min_score: float) -> None:
        """Upsert one per-tool min_score adjustment."""
        if self._db is None:
            return
        require_utf8_identifier(tool, "tool")
        with self._lock:
            db = self._db
            if db is None:
                return
            db.execute(
                "INSERT INTO auto_tune_adjustments (tool, min_score, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(tool) DO UPDATE SET "
                "min_score = excluded.min_score, "
                "updated_at = excluded.updated_at",
                (tool, min_score, time.time()),
            )
            db.commit()

    def get_per_tool_feedback_counts(self) -> dict[str, int]:
        """Return total feedback rows per tool, ignoring any time window.

        Mirrors what ``AutoTuner.maybe_adjust`` actually sees — the tuner
        decides readiness from the full feedback history, not from any
        ``since`` window an operator passes to ``stm_surfacing_stats``.
        Used by the server formatter to compute "auto-tune ready" /
        "need N more" labels that don't contradict the tuner just because
        the stats query is windowed.

        Returns ``{}`` when the store is closed.
        """
        with self._read_lock:
            db = self._read_db
            if db is None:
                return {}
            rows = db.execute(
                "SELECT e.tool, COUNT(*) FROM surfacing_feedback f "
                "JOIN surfacing_events e ON f.surfacing_id = e.id "
                "GROUP BY e.tool"
            ).fetchall()
            return {tool: count for tool, count in rows}
