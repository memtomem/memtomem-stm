"""SQLite persistent metrics store for proxy call history."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from memtomem_stm.proxy.metrics import CallMetrics
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


class MetricsStore:
    """SQLite-backed persistent metrics for proxy calls."""

    def __init__(self, db_path: Path, max_history: int = 10000) -> None:
        self._db_path = db_path
        self._max_history = max_history
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
        db = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=5.0)
        try:
            try:
                self._db_path.chmod(0o600)
            except OSError:
                pass
            tune_connection(db)
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
        """
        existing = {row[1] for row in db.execute("PRAGMA table_info(proxy_metrics)")}
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
        }
        for col, ddl in migrations.items():
            if col not in existing:
                db.execute(ddl)
        db.commit()

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    def record(self, metrics: CallMetrics) -> None:
        if self._db is None:
            return
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO proxy_metrics "
                "(server, tool, original_chars, compressed_chars, cleaned_chars, "
                "is_error, error_category, error_code, trace_id, "
                "compression_strategy, ratio_violation, scorer_fallback, "
                "index_ok, index_error, chunks_indexed, "
                "extract_ok, extract_error, "
                "surfacing_on_progressive_ok, surface_error, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    metrics.server,
                    metrics.tool,
                    metrics.original_chars,
                    metrics.compressed_chars,
                    metrics.cleaned_chars,
                    int(metrics.is_error),
                    metrics.error_category.value if metrics.error_category else None,
                    metrics.error_code,
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
                WHERE created_at >= ?
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
                    WHERE server = ? AND tool = ? AND created_at >= ?
                    ORDER BY original_chars ASC
                    LIMIT 1 OFFSET MAX(0, CAST(
                        (SELECT COUNT(*) FROM proxy_metrics
                         WHERE server = ? AND tool = ? AND created_at >= ?)
                        * 0.95 AS INTEGER) - 1)
                    """,
                    (server, tool, cutoff, server, tool, cutoff),
                ).fetchone()
                # Dominant strategy
                strat_row = self._db.execute(
                    """
                    SELECT compression_strategy FROM proxy_metrics
                    WHERE server = ? AND tool = ? AND created_at >= ?
                        AND compression_strategy IS NOT NULL
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
