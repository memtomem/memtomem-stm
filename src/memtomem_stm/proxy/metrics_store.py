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


class MetricsStore:
    """SQLite-backed persistent metrics for proxy calls."""

    def __init__(self, db_path: Path, max_history: int = 10000) -> None:
        self._db_path = db_path
        self._max_history = max_history
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=5.0)
        try:
            self._db_path.chmod(0o600)
        except OSError:
            pass
        tune_connection(self._db)
        self._db.execute(_CREATE)
        self._db.execute(_INDEX)
        self._db.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after initial schema (idempotent)."""
        if self._db is None:
            return
        existing = {row[1] for row in self._db.execute("PRAGMA table_info(proxy_metrics)")}
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
        }
        for col, ddl in migrations.items():
            if col not in existing:
                self._db.execute(ddl)
        self._db.commit()

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
                "compression_strategy, ratio_violation, scorer_fallback, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        row = self._db.execute(
            "SELECT trace_id FROM proxy_metrics "
            "WHERE server = ? AND tool = ? AND created_at >= ? "
            "AND trace_id IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (server, tool, cutoff),
        ).fetchone()
        return row[0] if row else None
