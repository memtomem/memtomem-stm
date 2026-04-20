"""SQLite persistence for progressive-delivery read events.

Every progressive response generates one row at creation (``offset=0``,
``chars=<initial chunk>``) plus one additional row per follow-up
``stm_proxy_read_more`` call. The table is append-only and purely
observational — writes feed ``stm_progressive_stats`` and future
analysis of nudge-strength vs. follow-up rate; they never affect the
response served to the caller.

The store shares its SQLite file with surfacing feedback and compression
feedback (``~/.memtomem/stm_feedback.db`` by default). SQLite WAL
journaling makes concurrent connections to the same file safe, and the
table namespaces are disjoint so the three subsystems don't conflict.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from memtomem_stm.utils.sqlite_tuning import tune_connection

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS progressive_reads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT    NOT NULL,
    trace_id    TEXT,
    server      TEXT    NOT NULL,
    tool        TEXT    NOT NULL,
    offset      INTEGER NOT NULL,
    chars       INTEGER NOT NULL,
    served_to   INTEGER NOT NULL,
    total_chars INTEGER NOT NULL,
    created_at  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pr_key     ON progressive_reads(key);
CREATE INDEX IF NOT EXISTS idx_pr_trace   ON progressive_reads(trace_id);
CREATE INDEX IF NOT EXISTS idx_pr_tool    ON progressive_reads(tool);
CREATE INDEX IF NOT EXISTS idx_pr_created ON progressive_reads(created_at);
"""


class ProgressiveReadsStore:
    """SQLite store for per-event progressive-delivery reads.

    Opens its own connection to ``db_path`` — safe to share the file
    with other stores (e.g. surfacing ``FeedbackStore`` and
    ``CompressionFeedbackStore``) because SQLite WAL journaling permits
    multiple concurrent connections and the tables are disjoint.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(str(self._db_path), check_same_thread=False)
        try:
            tune_connection(db)
            db.executescript(_SCHEMA)
            db.commit()
        except Exception:
            db.close()
            raise
        self._db = db

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    def record(
        self,
        key: str,
        trace_id: str | None,
        server: str,
        tool: str,
        offset: int,
        chars: int,
        served_to: int,
        total_chars: int,
    ) -> None:
        """Persist one read event.

        No-op when the store is closed; callers are expected to
        construct the tracker via ``app_lifespan`` and rely on the
        finally-block close, so hitting a closed store from the hot
        path would be a shutdown-race and losing a row is preferable
        to raising into the response path.
        """
        if self._db is None:
            return
        with self._lock:
            self._db.execute(
                "INSERT INTO progressive_reads "
                "(key, trace_id, server, tool, offset, chars, served_to, "
                "total_chars, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    trace_id,
                    server,
                    tool,
                    offset,
                    chars,
                    served_to,
                    total_chars,
                    time.time(),
                ),
            )
            self._db.commit()

    def get_stats(self, tool: str | None = None) -> dict:
        """Return aggregate stats for ``stm_progressive_stats``.

        Shape::

            {
                "total_reads": int,          # row count
                "total_responses": int,      # DISTINCT keys
                "follow_up_rate": float,     # keys w/ >1 row / total_responses
                "avg_chars_served": float,   # avg MAX(served_to) per key
                "avg_total_chars": float,    # avg total_chars per key
                "avg_coverage": float,       # avg MAX(served_to)/total_chars per key
                "by_tool": {tool: {"responses": int, "follow_up_rate": float}},
            }

        When ``tool`` is provided, ``by_tool`` is returned as an empty
        dict so callers can rely on the key always being present
        (parity with ``CompressionFeedbackStore.get_stats``). Averages
        are computed per-key so a 5-follow-up response is weighted the
        same as a 0-follow-up response.
        """
        empty = {
            "total_reads": 0,
            "total_responses": 0,
            "follow_up_rate": 0.0,
            "avg_chars_served": 0.0,
            "avg_total_chars": 0.0,
            "avg_coverage": 0.0,
            "by_tool": {},
        }
        if self._db is None:
            return empty

        where = ""
        params: tuple = ()
        if tool is not None:
            where = "WHERE tool = ?"
            params = (tool,)

        total_reads = self._db.execute(
            f"SELECT COUNT(*) FROM progressive_reads {where}", params
        ).fetchone()[0]
        if total_reads == 0:
            return empty

        # Per-key aggregate: last served_to (= cumulative coverage),
        # total_chars, tool, and row count per key.
        per_key_rows = self._db.execute(
            "SELECT key, MAX(served_to), MAX(total_chars), tool, COUNT(*) "
            f"FROM progressive_reads {where} GROUP BY key",
            params,
        ).fetchall()
        total_responses = len(per_key_rows)
        if total_responses == 0:
            return empty

        followed_up = sum(1 for _, _, _, _, n in per_key_rows if n > 1)
        follow_up_rate = followed_up / total_responses
        avg_chars_served = sum(r[1] for r in per_key_rows) / total_responses
        avg_total_chars = sum(r[2] for r in per_key_rows) / total_responses
        # Coverage is bounded to 1.0 per key: rare edge case where a
        # misbehaving upstream revises total_chars downward between
        # rows would otherwise push the ratio above 1.0 and skew the
        # average.
        avg_coverage = (
            sum(min(1.0, r[1] / r[2]) if r[2] > 0 else 0.0 for r in per_key_rows) / total_responses
        )

        by_tool: dict[str, dict] = {}
        if tool is None:
            # Re-group per-key rows by tool for the breakdown.
            tool_buckets: dict[str, list[int]] = {}
            for _, _, _, tool_name, n in per_key_rows:
                tool_buckets.setdefault(tool_name, []).append(n)
            for tool_name, counts in tool_buckets.items():
                responses = len(counts)
                followed = sum(1 for n in counts if n > 1)
                by_tool[tool_name] = {
                    "responses": responses,
                    "follow_up_rate": followed / responses if responses else 0.0,
                }

        return {
            "total_reads": total_reads,
            "total_responses": total_responses,
            "follow_up_rate": follow_up_rate,
            "avg_chars_served": avg_chars_served,
            "avg_total_chars": avg_total_chars,
            "avg_coverage": avg_coverage,
            "by_tool": by_tool,
        }
