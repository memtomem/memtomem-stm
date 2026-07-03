"""SQLite persistent metrics store for proxy call history."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from memtomem_stm.proxy.metrics import CallMetrics
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


def _tristate(value: bool | None) -> int | None:
    """Map a tri-state bool to SQLite-friendly ``int | None``.

    ``None`` (stage did not run) is preserved as SQL ``NULL``; ``True`` and
    ``False`` map to ``1`` and ``0``. Readers must distinguish ``NULL`` from
    ``0`` — the former is "not observed", the latter is "observed failure".
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
        self, db_path: Path, max_history: int = 10000, *, busy_timeout_ms: int | None = None
    ) -> None:
        self._db_path = db_path
        self._max_history = max_history
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
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connect_timeout = (
            self._busy_timeout_ms / 1000.0 if self._busy_timeout_ms is not None else 5.0
        )
        db = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=connect_timeout)
        try:
            ensure_private_db_files(self._db_path)
            tune_connection(db)
            if self._busy_timeout_ms is not None:
                # Override tune_connection's shared 3000 ms busy timeout so a
                # best-effort writer fast-fails on a locked DB. Lower bound only —
                # the DDL/INSERT below then raise quickly and the caller degrades.
                db.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
            db.execute(_CREATE)
            db.execute(_INDEX)
            db.commit()
            # Run migrations against the local ``db`` before it is exposed
            # as ``self._db`` so a failure here falls through to the outer
            # except and leaves the store un-initialized.
            self._migrate(db)
        except Exception:
            db.close()
            raise
        self._db = db

    def _migrate(self, db: sqlite3.Connection) -> None:
        """Add columns introduced after initial schema (idempotent).

        Idempotency is guaranteed per-column via ``PRAGMA table_info`` — a
        column that already exists is skipped, so restarting against an
        already-migrated DB runs no ALTER statements. This is stronger than
        a single ``user_version`` gate because adding a new column below
        doesn't require bumping a version number; the existence check covers
        all migration states (fresh, pre-migration, already-migrated).

        Boolean columns use ``INTEGER NOT NULL DEFAULT 0`` so existing rows
        get a deterministic value. Tri-state columns (``index_ok``,
        ``extract_ok``, ``surfacing_on_progressive_ok``) are nullable
        ``INTEGER DEFAULT NULL`` — ``NULL`` means "stage did not run", which
        readers must distinguish from ``0`` (stage ran and failed).

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
        migrations = {
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
            "extract_error": (
                "ALTER TABLE proxy_metrics ADD COLUMN extract_error TEXT DEFAULT NULL"
            ),
            "surfacing_on_progressive_ok": (
                "ALTER TABLE proxy_metrics ADD COLUMN surfacing_on_progressive_ok "
                "INTEGER DEFAULT NULL"
            ),
            "surface_error": (
                "ALTER TABLE proxy_metrics ADD COLUMN surface_error TEXT DEFAULT NULL"
            ),
            "error_message": (
                "ALTER TABLE proxy_metrics ADD COLUMN error_message TEXT DEFAULT NULL"
            ),
            # Provenance: pre-existing rows are all proxied MCP calls, so the
            # backfill default is ``'mcp'``; ``mms hook`` writes ``'hook'`` for
            # native built-in tools. NOT NULL + DEFAULT keeps old rows readable.
            "source": "ALTER TABLE proxy_metrics ADD COLUMN source TEXT NOT NULL DEFAULT 'mcp'",
        }
        for col, ddl in migrations.items():
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
        now = time.time()
        with self._lock:
            self._db.execute(
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
                    metrics.error_message,
                    metrics.trace_id,
                    metrics.compression_strategy,
                    int(metrics.ratio_violation),
                    int(metrics.scorer_fallback),
                    _tristate(metrics.index_ok),
                    metrics.index_error,
                    metrics.chunks_indexed,
                    _tristate(metrics.extract_ok),
                    metrics.extract_error,
                    _tristate(metrics.surfacing_on_progressive_ok),
                    metrics.surface_error,
                    metrics.source,
                    now,
                ),
            )
            self._db.commit()
            self._trim()

    def _trim(self) -> None:
        if self._db is None:
            return
        count = self._db.execute("SELECT COUNT(*) FROM proxy_metrics").fetchone()[0]
        if count > self._max_history:
            excess = count - self._max_history
            self._db.execute(
                "DELETE FROM proxy_metrics WHERE id IN "
                "(SELECT id FROM proxy_metrics ORDER BY created_at ASC LIMIT ?)",
                (excess,),
            )
            self._db.commit()

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
