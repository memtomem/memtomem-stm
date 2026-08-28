"""SQLite persistent metrics store for proxy call history."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from memtomem_stm.proxy.metrics import CallMetrics
from memtomem_stm.utils.json_out import (
    escape_lone_surrogates,
    has_lone_surrogate,
    require_utf8_identifier,
)
from memtomem_stm.utils.sqlite_private import ensure_private_db_files
from memtomem_stm.utils.sqlite_tuning import tune_connection

logger = logging.getLogger(__name__)

_CREATE = """
CREATE TABLE IF NOT EXISTS proxy_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    server          TEXT    NOT NULL,
    tool            TEXT    NOT NULL,
    original_chars  INTEGER NOT NULL,
    compressed_chars INTEGER NOT NULL,
    cleaned_chars   INTEGER NOT NULL DEFAULT 0,
    created_at      REAL    NOT NULL
);
"""

_INDEX = "CREATE INDEX IF NOT EXISTS idx_metrics_created ON proxy_metrics(created_at);"

_SOURCE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_metrics_source_created ON proxy_metrics(source, created_at)"
)

# Backs the predecessor lookup that schedules the per-write trim. The
# (source, created_at) index above cannot serve it: ordering by id inside a
# source degrades to scanning the whole partition there — measured 1.00 ms at
# the 10,000 cap, against 0.0023 ms with this one.
_SOURCE_ID_INDEX = "CREATE INDEX IF NOT EXISTS idx_metrics_source_id ON proxy_metrics(source, id)"

# Index names as they appear in the CREATE statements above, parsed from
# them rather than repeated: the fingerprint has to describe the indexes the
# slow path actually builds, and a hand-copied second list would drift.
_INDEX_NAMES = tuple(
    ddl.split("CREATE INDEX IF NOT EXISTS ", 1)[1].split(maxsplit=1)[0]
    for ddl in (_INDEX, _SOURCE_INDEX, _SOURCE_ID_INDEX)
)

# Columns declared by _CREATE above. Kept beside it so the fingerprint below
# describes the whole schema, base columns included.
_BASE_COLUMNS = frozenset(
    {
        "id",
        "server",
        "tool",
        "original_chars",
        "compressed_chars",
        "cleaned_chars",
        "created_at",
    }
)

# Columns introduced after the initial schema, applied by ``_migrate``.
# Module-level so ``_SCHEMA_FINGERPRINT`` is derived from the same dict the
# migration runs — adding an entry here cannot get out of sync with the
# fingerprint, so there is no version number to forget to bump.
_MIGRATIONS: dict[str, str] = {
    "is_error": "ALTER TABLE proxy_metrics ADD COLUMN is_error INTEGER NOT NULL DEFAULT 0",
    "error_category": "ALTER TABLE proxy_metrics ADD COLUMN error_category TEXT DEFAULT NULL",
    "error_code": "ALTER TABLE proxy_metrics ADD COLUMN error_code INTEGER DEFAULT NULL",
    "trace_id": "ALTER TABLE proxy_metrics ADD COLUMN trace_id TEXT DEFAULT NULL",
    "compression_strategy": (
        "ALTER TABLE proxy_metrics ADD COLUMN compression_strategy TEXT DEFAULT NULL"
    ),
    "ratio_violation": (
        "ALTER TABLE proxy_metrics ADD COLUMN ratio_violation INTEGER NOT NULL DEFAULT 0"
    ),
    "scorer_fallback": (
        "ALTER TABLE proxy_metrics ADD COLUMN scorer_fallback INTEGER NOT NULL DEFAULT 0"
    ),
    "index_ok": "ALTER TABLE proxy_metrics ADD COLUMN index_ok INTEGER DEFAULT NULL",
    "index_error": "ALTER TABLE proxy_metrics ADD COLUMN index_error TEXT DEFAULT NULL",
    "chunks_indexed": (
        "ALTER TABLE proxy_metrics ADD COLUMN chunks_indexed INTEGER NOT NULL DEFAULT 0"
    ),
    "extract_ok": "ALTER TABLE proxy_metrics ADD COLUMN extract_ok INTEGER DEFAULT NULL",
    "extract_error": "ALTER TABLE proxy_metrics ADD COLUMN extract_error TEXT DEFAULT NULL",
    "surfacing_on_progressive_ok": (
        "ALTER TABLE proxy_metrics ADD COLUMN surfacing_on_progressive_ok INTEGER DEFAULT NULL"
    ),
    "surface_error": "ALTER TABLE proxy_metrics ADD COLUMN surface_error TEXT DEFAULT NULL",
    "error_message": "ALTER TABLE proxy_metrics ADD COLUMN error_message TEXT DEFAULT NULL",
    # Provenance: pre-existing rows are all proxied MCP calls, so the
    # backfill default is ``'mcp'``; ``mms hook`` writes ``'hook'`` for
    # native built-in tools. NOT NULL + DEFAULT keeps old rows readable.
    "source": "ALTER TABLE proxy_metrics ADD COLUMN source TEXT NOT NULL DEFAULT 'mcp'",
}

# Schema bookkeeping in a table of our own rather than in ``PRAGMA
# user_version``. That pragma is a property of the DATABASE, not of a table,
# and ``metrics.db_path`` takes an arbitrary path — point it at a file another
# component stamps (the response cache does) and this store would read a number
# it never wrote, so the migration below would silently not run (#797). Nothing
# stops another component from touching a named table, but no other component in
# this codebase writes this one, whereas ``user_version`` is a single slot they
# all inevitably share.
_META_CREATE = """
CREATE TABLE IF NOT EXISTS metrics_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_SCHEMA_FINGERPRINT_KEY = "schema_fingerprint"

# Version of the fingerprint's own ENCODING, not of the schema. Bump it when the
# string below changes shape — a new field, a different separator — so stamps
# written by an older layout cannot accidentally compare equal to a new one.
#
# Additive schema changes need no bump: a new column changes ``_MIGRATIONS`` and
# a new index changes ``_INDEX_NAMES``, and both feed the fingerprint directly.
# A bump is also NOT a way to apply a *non*-additive change (an existing
# column's type or default, a dropped index): the slow path only ever adds
# (``IF NOT EXISTS`` DDL, ``_migrate`` skips columns that exist by name), so it
# would stamp "current" over a schema it never changed. Those need a real
# migration step first — a pre-existing limit of this store, not one the
# fingerprint introduces.
_SCHEMA_EPOCH = 1


def _schema_fingerprint() -> str:
    """Describe the schema ``initialize`` would produce, as a stable string.

    Covers exactly what the slow path can apply: the set of column *names* and
    the index names, plus the epoch. Derived from ``_MIGRATIONS`` rather than
    hand-maintained, so adding a column invalidates every stamped DB on its own
    with no version to bump — see ``_SCHEMA_EPOCH`` for what it deliberately
    does not cover. Plain text, not a hash, so a stale stamp is readable with
    ``sqlite3 proxy_metrics.db 'SELECT * FROM metrics_meta'``.
    """
    columns = ",".join(sorted(_BASE_COLUMNS | set(_MIGRATIONS)))
    return f"{_SCHEMA_EPOCH}|{columns}|{','.join(_INDEX_NAMES)}"


_SCHEMA_FINGERPRINT = _schema_fingerprint()


# Ceiling on how many inserts one per-write trim may cover. See
# ``MetricsStore._trim_interval``.
_MAX_TRIM_INTERVAL = 64


def _tristate(value: bool | None) -> int | None:
    """Map a tri-state bool to SQLite-friendly ``int | None``.

    ``None`` is preserved as SQL ``NULL``; ``True`` and ``False`` map to ``1``
    and ``0``. Readers must distinguish ``NULL`` (no outcome recorded) from
    ``0`` (recorded non-success).
    """
    if value is None:
        return None
    return 1 if value else 0


def _saved_ratio(original: int, compressed: int) -> float:
    """Fraction of chars removed by compression, guarding div-by-zero."""
    if original <= 0:
        return 0.0
    return round(1.0 - (compressed / original), 4)


def read_compression_summary(
    db_path: Path, tool: str | None = None, source: str | None = None
) -> dict[str, object]:
    """Aggregate per-``(server, tool)`` compression stats read-only from disk.

    Unlike :meth:`MetricsStore.initialize`, this NEVER creates or migrates the
    DB — it opens the file read-only via the ``?mode=ro`` URI (mirroring
    :func:`memtomem_stm.surfacing.feedback_store.inspect_feedback_db`) so a CLI
    *read* command can't write DDL into the user's store. ``tune_connection``
    is deliberately not called: ``PRAGMA journal_mode=WAL`` would write, and a
    server-created DB is already in WAL mode (concurrent reads are safe).

    The returned dict always has the same keys so callers can render a stable
    shape. ``available`` is ``False`` when the file is missing or isn't a
    metrics DB; ``schema_outdated`` is ``True`` for a pre-migration DB that
    lacks the ``is_error`` column (added by :meth:`MetricsStore._migrate`), in
    which case ``error_count`` degrades to ``0`` rather than crashing.

    The optional ``tool`` filter matches the raw tool name, so it can span
    multiple servers that expose a same-named tool. The optional ``source``
    filter selects a provenance (``'mcp'`` proxied calls vs. ``'hook'`` native
    built-in tools). On a pre-``source`` DB the column is absent: a request for
    any source other than the legacy ``'mcp'`` default returns an empty (but
    ``available``) summary, since no row can carry that source.
    """
    resolved = db_path.expanduser().resolve()
    summary: dict[str, object] = {
        "path": str(resolved),
        "available": False,
        "schema_outdated": False,
        "total_calls": 0,
        "error_count": 0,
        "total_original_chars": 0,
        "total_compressed_chars": 0,
        "saved_chars": 0,
        "saved_ratio": 0.0,
        "by_tool": [],
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
        cols = {row[1] for row in db.execute("PRAGMA table_info(proxy_metrics)")}
        if "original_chars" not in cols:
            # Missing file would have returned above; an empty column set here
            # means the file exists but isn't a recognizable metrics DB.
            return summary
        has_is_error = "is_error" in cols
        has_source = "source" in cols
        # ``schema_outdated`` flags ONLY the missing ``is_error`` column (its
        # historical meaning: "error counts degraded to 0"). A DB that has
        # ``is_error`` but predates ``source`` still reports accurate error
        # counts, so it must not raise this flag — the missing-``source`` case is
        # handled by the ``source``-filter guard below, not this flag.
        summary["schema_outdated"] = not has_is_error
        error_expr = "SUM(is_error)" if has_is_error else "0"

        if (tool is not None and has_lone_surrogate(tool)) or (
            source is not None and has_lone_surrogate(source)
        ):
            # An unencodable filter cannot be bound as a SQLite parameter and
            # can never match a stored row (the write path refuses such
            # identifiers), so report the same empty-but-available shape as the
            # sibling guard below. Placed AFTER ``schema_outdated`` so the two
            # empty-summary exits describe the DB identically — a pre-migration
            # DB is outdated regardless of which filter came back empty.
            # ``mms stats`` refuses this at the CLI boundary; direct callers of
            # this reader still land here.
            summary["available"] = True
            return summary

        if source is not None and not has_source and source != "mcp":
            # Pre-``source`` DB: every row is the legacy ``'mcp'`` default, so a
            # request for any other provenance matches nothing. Report an empty
            # but available summary rather than over-counting every legacy row.
            summary["available"] = True
            return summary

        conditions: list[str] = []
        params: list[object] = []
        if tool is not None:
            conditions.append("tool = ?")
            params.append(tool)
        if source is not None and has_source:
            conditions.append("source = ?")
            params.append(source)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = db.execute(
            "SELECT server, tool, COUNT(*), SUM(original_chars), "
            f"SUM(compressed_chars), {error_expr} "
            f"FROM proxy_metrics{where} GROUP BY server, tool "
            "ORDER BY COUNT(*) DESC",
            params,
        ).fetchall()
    except sqlite3.Error as exc:
        summary["error"] = str(exc)
        return summary
    finally:
        db.close()

    by_tool: list[dict[str, object]] = []
    total_calls = total_error = total_orig = total_comp = 0
    for server, tool_name, calls, orig, comp, errors in rows:
        calls = calls or 0
        orig = orig or 0
        comp = comp or 0
        errors = errors or 0
        total_calls += calls
        total_error += errors
        total_orig += orig
        total_comp += comp
        by_tool.append(
            {
                "server": server,
                "tool": tool_name,
                "calls": calls,
                "original_chars": orig,
                "compressed_chars": comp,
                "saved_ratio": _saved_ratio(orig, comp),
            }
        )

    summary["available"] = True
    summary["total_calls"] = total_calls
    summary["error_count"] = total_error
    summary["total_original_chars"] = total_orig
    summary["total_compressed_chars"] = total_comp
    summary["saved_chars"] = total_orig - total_comp
    summary["saved_ratio"] = _saved_ratio(total_orig, total_comp)
    summary["by_tool"] = by_tool
    return summary


class MetricsStore:
    """SQLite-backed persistent metrics for proxy calls."""

    def __init__(
        self,
        db_path: Path,
        max_history: int = 10000,
        *,
        busy_timeout_ms: int | None = None,
        reconcile_on_init: bool = True,
    ) -> None:
        self._db_path = db_path
        self._max_history = max_history
        # ``reconcile_on_init=False`` is for openers whose ``max_history``
        # does NOT come from the effective proxy JSON — the ``mms hook`` path
        # builds an env-only STMConfig, so its (possibly default) cap must
        # not be applied to OTHER sources' retention at startup. Such openers
        # still trim their own source on write; only the cap-authoritative
        # openers (server lifespan, ``mms tune``) reconcile every source.
        self._reconcile_on_init = reconcile_on_init
        # Best-effort writers (the ``mms hook`` native-tool metrics path) pass a
        # small ``busy_timeout_ms`` so a locked shared ``proxy_metrics.db`` makes
        # initialize/record fast-fail (degrade to no row) instead of stalling the
        # host's synchronous tool call up to the shared 3000 ms busy timeout.
        # ``None`` keeps the long-lived-store default (5 s connect + 3000 ms busy).
        self._busy_timeout_ms = busy_timeout_ms
        self._db: sqlite3.Connection | None = None
        # Readers and writers share ``self._lock`` defensively. Current
        # callers are all asyncio single-thread, but the connection uses
        # ``check_same_thread=False`` so any future move to a thread-pool
        # executor (or another thread-spawning caller) would race reads
        # against in-flight ``record()`` writes without this guard. The
        # uncontended acquire cost is negligible given cold-path call
        # frequency (tuner drift analysis + feedback correlation lookups).
        self._lock = threading.Lock()

    def initialize(self) -> None:
        """Open the DB, building or migrating the schema only when it is stale.

        Trade-off worth knowing about: because a current stamp short-circuits
        the DDL, this no longer re-creates a table, column or index that went
        missing *after* the stamp was written. It used to, incidentally — the
        unconditional ``IF NOT EXISTS`` cycle repaired such a DB on the next
        open. Only external damage gets there; nothing in this codebase drops
        them. What each kind of loss then looks like:

        - a dropped *column* or the whole ``proxy_metrics`` table is loud —
          ``record`` fails on its insert;
        - a dropped *index* is **silent**. ``record`` does not need one, so the
          only symptom is that its ``COUNT``/``ORDER BY`` scans and the reader
          aggregations get slower. Accepted knowingly: an index nothing drops
          is not worth a per-invocation check.

        Repair is to clear the stamp (``DELETE FROM metrics_meta``) and reopen,
        which rebuilds the indexes, the table, and every ``_MIGRATIONS`` column.
        It does **not** restore a dropped *base* column — ``_CREATE`` is
        ``IF NOT EXISTS`` and cannot alter an existing table — so that one case
        needs the file rebuilt. Losing ``metrics_meta`` itself needs no action:
        the probe reads that as "not current" and the slow path recreates it.

        Paying the probe on every one of thousands of daily hook invocations to
        keep an accidental repair for a state nothing produces is the worse
        trade (#870).
        """
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connect_timeout = (
            self._busy_timeout_ms / 1000.0 if self._busy_timeout_ms is not None else 5.0
        )
        db = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=connect_timeout)
        try:
            ensure_private_db_files(self._db_path)
            # Pass the budget IN rather than overriding the pragma afterwards
            # (#901): tuning's WAL retry spends the lock budget during this
            # call, so a later override would arrive after the wait it was
            # meant to shorten. A best-effort writer fast-fails on a locked DB —
            # the DDL/INSERT below then raise quickly and the caller degrades.
            if self._busy_timeout_ms is not None:
                tune_connection(db, busy_timeout_ms=int(self._busy_timeout_ms))
            else:
                tune_connection(db)
            if not self._schema_is_current(db):
                db.execute(_CREATE)
                db.execute(_INDEX)
                db.commit()
                # Run migrations against the local ``db`` before it is exposed
                # as ``self._db`` so a failure here falls through to the outer
                # except and leaves the store un-initialized.
                self._migrate(db)
                # ``source`` is a migrated column (absent from the base _CREATE),
                # so the index backing the per-source _trim scan can only be
                # created after _migrate has run.
                db.execute(_SOURCE_INDEX)
                db.execute(_SOURCE_ID_INDEX)
                # Stamp LAST. This is an ORDERING guarantee, not an atomic one:
                # the base DDL above and _migrate each commit before this runs,
                # so a failure part-way leaves that partial DDL committed. What
                # ordering buys is that the stamp never becomes CURRENT for a
                # schema whose build did not finish — a fresh DB is left with no
                # stamp and an upgrading one keeps its old (now stale) value, and
                # both read as "not current", so the next opener redoes the full
                # path. Concurrent openers both land here and write the same
                # value (the DDL is idempotent and _migrate tolerates the
                # duplicate-column race), so last-writer-wins is a no-op.
                db.execute(_META_CREATE)
                db.execute(
                    "INSERT INTO metrics_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (_SCHEMA_FINGERPRINT_KEY, _SCHEMA_FINGERPRINT),
                )
                db.commit()
            # Reconcile every source once at startup: the per-write _trim only
            # covers the writer's own source, so an over-cap source that has
            # stopped receiving writes (e.g. after lowering max_history) would
            # otherwise never shrink. Gated: see reconcile_on_init in __init__.
            if self._reconcile_on_init:
                for (src,) in db.execute("SELECT DISTINCT source FROM proxy_metrics").fetchall():
                    self._trim_source(db, src)
        except Exception:
            db.close()
            raise
        self._db = db

    def _schema_is_current(self, db: sqlite3.Connection) -> bool:
        """Whether this DB already carries the schema ``initialize`` would build.

        One SELECT on a one-row table that lets the hot path skip the DDL +
        ``PRAGMA table_info`` + conditional-ALTER cycle. ``mms hook`` runs as a
        one-shot subprocess on every host built-in tool call, so that cycle ran
        thousands of times a day to reach a schema that had not changed since
        the first run (#870).

        Of the ``OperationalError``s, only a missing ``metrics_meta`` is treated
        as "not current" — that is a fresh or pre-#870 DB, and the slow path
        builds it. (An absent row or a stale value is not an error at all; both
        simply return ``False`` below.) Every other ``OperationalError``
        propagates rather than being retried as DDL: the
        caller would hit the same condition one statement later, and reporting
        it from the DDL instead of from here would name the wrong statement.
        The hook wraps this whole path in its own best-effort catch, so a
        propagated error still degrades to no row.

        Says nothing about whether the columns are really there: it reads the
        stamp, not the schema. See ``initialize`` for what that costs.
        """
        try:
            row = db.execute(
                "SELECT value FROM metrics_meta WHERE key = ?", (_SCHEMA_FINGERPRINT_KEY,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            return False
        return bool(row) and str(row[0]) == _SCHEMA_FINGERPRINT

    def _migrate(self, db: sqlite3.Connection) -> None:
        """Add columns introduced after initial schema (idempotent).

        Idempotency is guaranteed per-column via ``PRAGMA table_info`` — a
        column that already exists is skipped, so running this against an
        already-migrated DB executes no ALTER statements. The existence check
        covers all migration states (fresh, pre-migration, already-migrated),
        and it — not the ``_SCHEMA_FINGERPRINT`` gate in ``initialize`` —
        decides *what* runs. The fingerprint only decides whether this method
        is worth calling at all; because it is derived from ``_MIGRATIONS``,
        adding a column below still requires no version bump.

        Boolean columns use ``INTEGER NOT NULL DEFAULT 0`` so existing rows
        get a deterministic value. Tri-state columns (``index_ok``,
        ``extract_ok``, ``surfacing_on_progressive_ok``) are nullable
        ``INTEGER DEFAULT NULL`` — ``NULL`` means no outcome was recorded,
        which readers must distinguish from ``0`` (recorded non-success).

        The ``table_info`` snapshot makes each ALTER conditional, but the
        check and the ALTER are not one atomic step across processes: two
        ``mms`` sessions starting right after a column-adding upgrade can
        both read the pre-migration schema before either ALTERs, and the
        loser's ALTER then raises ``duplicate column name``. That race is
        tolerated per-column below (the column exists afterwards either way,
        which is the desired end state); any other ``OperationalError`` is a
        real failure and propagates.
        """
        existing = self._existing_columns(db)
        for col, ddl in _MIGRATIONS.items():
            if col not in existing:
                try:
                    db.execute(ddl)
                except sqlite3.OperationalError as exc:
                    # A concurrent process won the race and added this column
                    # between our ``_existing_columns`` read and this ALTER —
                    # the column now exists, so treat the duplicate as a no-op.
                    if "duplicate column name" not in str(exc).lower():
                        raise
        db.commit()

    def _existing_columns(self, db: sqlite3.Connection) -> set[str]:
        """Current ``proxy_metrics`` column names — the migration snapshot.

        Its own method so the concurrent-migration race (a stale snapshot
        that misses a column another process just added) can be reproduced
        deterministically in tests.
        """
        return {row[1] for row in db.execute("PRAGMA table_info(proxy_metrics)")}

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    def record(self, metrics: CallMetrics) -> None:
        """Persist one per-call metrics row.

        Synchronous sqlite write on the asyncio event loop: while it
        runs, every runnable coroutine stalls — other in-flight proxied
        calls included, not just the one that produced the row.
        Accepted for the current local single-MCP-client deployment,
        where call volume is low and a local WAL insert is far cheaper
        than the upstream call it accounts for. Multi-client serving
        (or materially higher concurrency) is the reopen trigger: move
        persistence off-loop (e.g. ``asyncio.to_thread``) — readers and
        writers here already share ``self._lock`` (see ``__init__``),
        so this store is lock-ready for that move.
        """
        if self._db is None:
            return
        require_utf8_identifier(metrics.server, "server")
        require_utf8_identifier(metrics.tool, "tool")
        require_utf8_identifier(metrics.trace_id, "trace_id")
        require_utf8_identifier(metrics.compression_strategy, "compression_strategy")
        require_utf8_identifier(metrics.source, "source")
        now = time.time()
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO proxy_metrics "
                "(server, tool, original_chars, compressed_chars, cleaned_chars, "
                "is_error, error_category, error_code, error_message, trace_id, "
                "compression_strategy, ratio_violation, scorer_fallback, "
                "index_ok, index_error, chunks_indexed, "
                "extract_ok, extract_error, "
                "surfacing_on_progressive_ok, surface_error, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    metrics.server,
                    metrics.tool,
                    metrics.original_chars,
                    metrics.compressed_chars,
                    metrics.cleaned_chars,
                    int(metrics.is_error),
                    metrics.error_category.value if metrics.error_category else None,
                    metrics.error_code,
                    (
                        escape_lone_surrogates(metrics.error_message)
                        if metrics.error_message is not None
                        else None
                    ),
                    metrics.trace_id,
                    metrics.compression_strategy,
                    int(metrics.ratio_violation),
                    int(metrics.scorer_fallback),
                    _tristate(metrics.index_ok),
                    (
                        escape_lone_surrogates(metrics.index_error)
                        if metrics.index_error is not None
                        else None
                    ),
                    metrics.chunks_indexed,
                    _tristate(metrics.extract_ok),
                    (
                        escape_lone_surrogates(metrics.extract_error)
                        if metrics.extract_error is not None
                        else None
                    ),
                    _tristate(metrics.surfacing_on_progressive_ok),
                    (
                        escape_lone_surrogates(metrics.surface_error)
                        if metrics.surface_error is not None
                        else None
                    ),
                    metrics.source,
                    now,
                ),
            )
            # One transaction for the insert and the trim it may trigger. If the
            # DELETE fails, the crossing row goes with it: left committed it
            # would become the next row's predecessor, land in the same bucket
            # and consume the crossing, so a failing trim could defer itself
            # indefinitely. Losing one best-effort metrics row is the cheaper
            # side of that trade. Also one commit instead of two.
            try:
                if self._should_trim(metrics.source, cursor.lastrowid):
                    self._trim_statement(self._db, metrics.source)
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def _trim_interval(self) -> int:
        """How many inserts one trim is allowed to cover, as a divisor of the cap.

        The trim's cost is its ``COUNT(*)``, a covering-index scan proportional
        to the partition — 0.39 ms at the default 10,000 cap, and it grows with
        ``max_history``. Running it once per interval amortizes that, at the
        price of letting a partition sit above the cap between trims.

        Deriving the interval from the cap keeps that excess proportional (~1%)
        rather than absolute: a 100-row cap trims on every write and stays
        exact, while the default cap trims every 64th. The ceiling bounds the
        interval where the proportion stops mattering, so an enormous cap
        cannot defer trimming further and further.
        """
        return max(1, min(_MAX_TRIM_INTERVAL, self._max_history // 100))

    def _previous_row_id(self, source: str, before_id: int) -> int | None:
        """Id of this source's newest row *older than* ``before_id``.

        A seek in ``idx_metrics_source_id``, which covers both columns —
        0.0023 ms against a partition at the 10,000 cap, versus 0.38 ms for the
        ``COUNT(*)`` the trim itself runs. That ratio is what makes the
        scheduling in ``_should_trim`` affordable on every write.

        Keyed on ``id < before_id`` rather than on "the second-newest row",
        which is only the caller's predecessor when nothing else interleaves.
        Two writers that both commit before either asks would otherwise each
        see the same newer row — one of them comparing itself against itself —
        and both skip a crossing they should have taken. Ids are a total order
        and are assigned by the INSERT, so this answer is caller-relative
        whatever the other writers or their clocks do.
        """
        if self._db is None:
            return None
        row = self._db.execute(
            "SELECT MAX(id) FROM proxy_metrics WHERE source = ? AND id < ?",
            (source, before_id),
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def _should_trim(self, source: str, row_id: int | None) -> bool:
        """Whether the row just inserted is the one that pays for the trim.

        Fires when this source's rows cross a multiple of ``interval`` — that
        is, when the new row falls in a different ``id // interval`` bucket than
        this source's previous row. Ids come from AUTOINCREMENT, so they advance
        by exactly one per insert *no matter which process wrote it*.

        This bounds **every** source at ``cap + interval`` while it is being
        written, with no state to keep: a source can add at most ``interval - 1``
        rows inside one bucket, and its next row is necessarily in the next
        bucket and trims. Two rules that look equivalent do not hold:

        - A random sample (``random() < 1/interval``) has a geometric tail and
          so no bound at all — 65 straight misses at interval 64 alone would
          have probability ~36%.
        - "Fire when ``id % interval == 0``" silently starves a source whose
          rows never land on a multiple. Reproduced at interval 5 with a source
          taking every 5th insert (ids 1, 6, 11, … — always 1 mod 5): it never
          trimmed, reaching 700 rows against a cap of 500 and still climbing.
          An in-memory watermark fixes that only for a store that writes a
          source more than once, which is exactly what ``mms hook`` — one row
          per subprocess — never does.

        Deriving the schedule from the table instead of from memory is what
        makes the one-shot writer behave like the long-lived one. It is also why
        ``record`` commits the insert and the trim together: a crossing row that
        stayed committed after its own DELETE failed would become the next row's
        predecessor, put it in the same bucket, and silently consume the
        crossing. Rolling both back leaves the next write to cross again.

        ``lastrowid`` is ``None`` only if the INSERT produced no row, in which
        case there is nothing to trim for.
        """
        interval = self._trim_interval()
        if interval <= 1:
            return True
        if row_id is None:
            return False
        previous = self._previous_row_id(source, row_id)
        return previous is None or row_id // interval != previous // interval

    def _trim(self, source: str) -> None:
        """Enforce ``max_history`` per ``source``, scoped to the writer's own.

        The cap used to be table-wide with FIFO eviction, and the two writers
        have wildly different rates: the ``mms hook`` path (``source='hook'``,
        one row per built-in Read/Grep/Glob/Bash call) outpaces proxied MCP
        traffic by orders of magnitude, so hook churn evicted nearly every
        ``'mcp'`` row and starved the ``source='mcp'``-scoped aggregations
        (``get_tool_profiles``, error stats; ``read_compression_summary``
        filters by source only when one is passed) down to a useless sample.
        Trimming within the
        just-written source means neither writer can evict the other's rows;
        the table's worst case becomes ``max_history × distinct sources``
        (currently two).

        ``record`` runs this only on rows that cross an interval boundary (see
        ``_should_trim``) rather than on every write, so a partition can sit
        above the cap in between — by at most ``interval - 1`` further writes
        from that source, not by a fixed number of table inserts, since an
        interleaved source can cross on consecutive writes of its own. The
        statement always removes the *full* excess, so one firing returns the
        partition to the cap rather than converging a row at a time; the
        reconcile pass in ``initialize`` remains exact.

        The residual this leaves: a source that stops being written to keeps
        whatever excess it had at its last insert — at most one interval's
        rows — until some cap-authoritative opener reconciles. Nothing removes
        it in a hook-only deployment that never starts a server or ``mms tune``.
        """
        if self._db is None:
            return
        self._trim_source(self._db, source)

    def _trim_statement(self, db: sqlite3.Connection, source: str) -> None:
        """The DELETE alone, leaving the transaction open for the caller.

        Split from ``_trim_source`` so ``record`` can commit it together with
        the insert that triggered it; the reconcile path wants its own commit.
        """
        # The excess is computed INSIDE the DELETE so count-and-delete is one
        # atomic statement: reconciliation runs from every initialize and the
        # hook opens a short-lived store per call, so two processes can trim
        # the same source concurrently — a pre-computed excess would be stale
        # for the loser and over-delete below the cap. ``max(0, ...)`` matters:
        # a negative LIMIT means "unlimited" in SQLite and would empty the
        # source. When nothing is over cap the statement deletes zero rows.
        db.execute(
            "DELETE FROM proxy_metrics WHERE id IN "
            "(SELECT id FROM proxy_metrics WHERE source = ? "
            "ORDER BY created_at ASC "
            "LIMIT max(0, (SELECT COUNT(*) FROM proxy_metrics WHERE source = ?) - ?))",
            (source, source, self._max_history),
        )

    def _trim_source(self, db: sqlite3.Connection, source: str) -> None:
        self._trim_statement(db, source)
        db.commit()

    def get_tool_profiles(self, since_seconds: float = 86400.0) -> list[dict]:
        """Aggregate per ``(server, tool)`` stats for auto-tuner analysis.

        Returns a list of dicts with keys: ``server``, ``tool``,
        ``call_count``, ``violation_count``, ``avg_ratio``,
        ``p95_original_chars``, ``dominant_strategy``, ``error_count``.
        Only non-error rows with ``cleaned_chars > 0`` contribute to
        ``avg_ratio``.  ``p95_original_chars`` is approximated by taking
        the value at the 95th percentile rank within each group.

        Scoped to ``source = 'mcp'``: the tuner adjusts *proxy* compression
        budgets for upstream MCP tools, so native built-in tool rows written by
        ``mms hook`` (``source='hook'``) must not be aggregated here — otherwise
        an unrelated ``builtin/Bash`` row would yield a bogus recommendation.
        """
        if self._db is None:
            return []
        cutoff = time.time() - since_seconds
        with self._lock:
            # Main aggregation
            rows = self._db.execute(
                """
                SELECT
                    server,
                    tool,
                    COUNT(*)                                          AS call_count,
                    SUM(ratio_violation)                              AS violation_count,
                    AVG(
                        CASE WHEN cleaned_chars > 0 AND is_error = 0
                             THEN CAST(compressed_chars AS REAL) / cleaned_chars
                        END
                    )                                                 AS avg_ratio,
                    SUM(is_error)                                     AS error_count
                FROM proxy_metrics
                WHERE created_at >= ? AND source = 'mcp'
                GROUP BY server, tool
                """,
                (cutoff,),
            ).fetchall()

            profiles: list[dict] = []
            for server, tool, call_count, violation_count, avg_ratio, error_count in rows:
                # p95 approximation: pick the value at rank ceil(0.95 * N)
                p95_row = self._db.execute(
                    """
                    SELECT original_chars FROM proxy_metrics
                    WHERE server = ? AND tool = ? AND created_at >= ? AND source = 'mcp'
                    ORDER BY original_chars ASC
                    LIMIT 1 OFFSET MAX(0, CAST(
                        (SELECT COUNT(*) FROM proxy_metrics
                         WHERE server = ? AND tool = ? AND created_at >= ? AND source = 'mcp')
                        * 0.95 AS INTEGER) - 1)
                    """,
                    (server, tool, cutoff, server, tool, cutoff),
                ).fetchone()
                # Dominant strategy
                strat_row = self._db.execute(
                    """
                    SELECT compression_strategy FROM proxy_metrics
                    WHERE server = ? AND tool = ? AND created_at >= ?
                        AND compression_strategy IS NOT NULL AND source = 'mcp'
                    GROUP BY compression_strategy
                    ORDER BY COUNT(*) DESC
                    LIMIT 1
                    """,
                    (server, tool, cutoff),
                ).fetchone()
                profiles.append(
                    {
                        "server": server,
                        "tool": tool,
                        "call_count": call_count,
                        "violation_count": violation_count or 0,
                        "avg_ratio": round(avg_ratio, 4) if avg_ratio is not None else None,
                        "p95_original_chars": p95_row[0] if p95_row else 0,
                        "dominant_strategy": strat_row[0] if strat_row else None,
                        "error_count": error_count or 0,
                    }
                )
        return profiles

    def get_progressive_degradations(
        self, since_seconds: float = 86400.0, tool: str | None = None
    ) -> dict:
        """Count progressive-delivery primary-store degradations in the window.

        A row whose ``compression_strategy`` ends in ``→passthrough_on_error``
        records the proxy degrading a failed primary ``PROGRESSIVE`` store to an
        uncached full-content passthrough (see ``ProxyManager``). The
        ``passthrough_on_error`` suffix is unique to this family, so a
        ``LIKE '%passthrough_on_error'`` match never collides with the other
        ``X→Y_fallback`` strategy labels (e.g. ``timeout_fallback``).

        Returns ``{"total": int, "by_server_tool": [{"server", "tool",
        "count"}, ...]}`` (breakdown sorted by count desc) so an operator can
        answer "is progressive delivery frequently degrading because the
        backing store is failing, and on which server/tool?".
        """
        empty: dict = {"total": 0, "by_server_tool": []}
        if self._db is None:
            return empty
        if tool is not None and has_lone_surrogate(tool):
            return empty
        cutoff = time.time() - since_seconds
        # ``source = 'mcp'``: progressive delivery is a proxy-only path; native
        # built-in (``source='hook'``) rows never carry this strategy, but scope
        # explicitly so the count can't drift if that ever changes.
        where = (
            "created_at >= ? AND source = 'mcp' "
            "AND compression_strategy LIKE '%passthrough_on_error'"
        )
        params: list = [cutoff]
        if tool is not None:
            where += " AND tool = ?"
            params.append(tool)
        with self._lock:
            total = self._db.execute(
                f"SELECT COUNT(*) FROM proxy_metrics WHERE {where}", params
            ).fetchone()[0]
            rows = self._db.execute(
                f"SELECT server, tool, COUNT(*) AS n FROM proxy_metrics WHERE {where} "
                "GROUP BY server, tool ORDER BY n DESC, server ASC, tool ASC",
                params,
            ).fetchall()
        return {
            "total": total,
            "by_server_tool": [{"server": r[0], "tool": r[1], "count": r[2]} for r in rows],
        }

    def get_tool_error_stats(
        self, since_seconds: float, error_categories: tuple[str, ...]
    ) -> dict[tuple[str, str], tuple[int, int]]:
        """Per ``(server, tool)``: ``(call_count, matching_error_count)``.

        Counts rows inside the look-back window; an error row contributes to
        ``matching_error_count`` only when its ``error_category`` is in
        *error_categories* — the tool-exposure health filter (#465) passes
        the upstream-attributable categories so a proxy-internal pipeline
        failure never counts against the upstream tool. An empty category
        tuple therefore yields zero matching errors (calls still counted).
        ``tool`` keys are raw upstream names, matching ``record()``.
        """
        if self._db is None:
            return {}
        cutoff = time.time() - since_seconds
        placeholders = ",".join("?" for _ in error_categories) or "NULL"
        with self._lock:
            # ``source = 'mcp'``: the exposure health filter (#465) gauges
            # upstream tool health; native built-in (``source='hook'``) rows must
            # not enter the per-(server, tool) call/error counts.
            rows = self._db.execute(
                "SELECT server, tool, COUNT(*), "
                "SUM(CASE WHEN is_error = 1 AND error_category IN "
                f"({placeholders}) THEN 1 ELSE 0 END) "
                "FROM proxy_metrics WHERE created_at >= ? AND source = 'mcp' "
                "GROUP BY server, tool",
                (*error_categories, cutoff),
            ).fetchall()
        return {(r[0], r[1]): (r[2], r[3] or 0) for r in rows}

    def get_history(self, limit: int = 100) -> list[dict]:
        if self._db is None:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT server, tool, original_chars, compressed_chars, cleaned_chars, created_at "
                "FROM proxy_metrics ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "server": r[0],
                "tool": r[1],
                "original_chars": r[2],
                "compressed_chars": r[3],
                "cleaned_chars": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]

    def lookup_recent_trace_id(
        self,
        server: str,
        tool: str,
        within_seconds: float,
    ) -> str | None:
        """Return the ``trace_id`` of the freshest ``(server, tool)`` row
        recorded within the last ``within_seconds`` seconds, or ``None``
        if the store is closed or nothing matches.

        Best-effort correlation helper used by ``stm_compression_feedback``
        when the caller omits an explicit ``trace_id``. The window should
        stay narrow enough (see ``TRACE_LOOKUP_WINDOW_SECONDS`` in
        ``compression_feedback_store``) that we don't attach a feedback
        report to an unrelated historical call with the same ``(server,
        tool)`` pair.
        """
        if self._db is None:
            return None
        try:
            require_utf8_identifier(server, "server")
            require_utf8_identifier(tool, "tool")
        except ValueError:
            return None
        cutoff = time.time() - within_seconds
        with self._lock:
            row = self._db.execute(
                "SELECT trace_id FROM proxy_metrics "
                "WHERE server = ? AND tool = ? AND created_at >= ? "
                "AND trace_id IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (server, tool, cutoff),
            ).fetchone()
        return row[0] if row else None
