"""SQLite persistence for ``stm_compression_feedback`` reports.

This store captures agent-reported information loss from compressed proxy
responses. It is a **learning signal**, not a safety net — writes here do
not repair the current turn; they feed future auto-tuning and manual
audits via ``stm_compression_stats``.

The store shares its SQLite file with surfacing feedback
(``~/.memtomem/stm_feedback.db`` by default). SQLite WAL journaling makes
two connections to the same file safe for concurrent read/write, and the
table namespaces are disjoint so the two subsystems don't conflict.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from memtomem_stm.utils.sqlite_private import ensure_private_db_files
from memtomem_stm.utils.sqlite_tuning import tune_connection

logger = logging.getLogger(__name__)


# Window for best-effort ``trace_id`` correlation when the caller does not
# supply one. Too narrow misses correlations in multi-turn conversations;
# too wide risks attaching a feedback report to the wrong prior call with
# the same ``(server, tool)`` pair. 30 minutes covers a typical chat
# session without running far enough to collide with an unrelated task.
# Constant rather than config: the right value is not tool-specific and
# tuning it rarely helps in practice.
TRACE_LOOKUP_WINDOW_SECONDS: float = 1800.0


# Accepted ``kind`` buckets for a feedback report. Intentionally small —
# free-form narrative belongs in ``missing``; ``kind`` exists to enable
# per-category rollups in ``get_stats``. Future: add ``"expired"`` for
# R2 (SELECTIVE/HYBRID TTL expiry) once enough reports land in ``other``
# to justify a dedicated bucket.
_VALID_KINDS: frozenset[str] = frozenset(
    {
        "truncated",
        "missing_example",
        "missing_metadata",
        "wrong_topic",
        "other",
    }
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS compression_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    server      TEXT    NOT NULL,
    tool        TEXT    NOT NULL,
    trace_id    TEXT,
    kind        TEXT    NOT NULL,
    missing     TEXT    NOT NULL,
    created_at  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cfb_tool ON compression_feedback(tool);
CREATE INDEX IF NOT EXISTS idx_cfb_trace ON compression_feedback(trace_id);
CREATE INDEX IF NOT EXISTS idx_cfb_created ON compression_feedback(created_at);
"""


def is_valid_kind(kind: str) -> bool:
    return kind in _VALID_KINDS


def valid_kinds() -> list[str]:
    return sorted(_VALID_KINDS)


class CompressionFeedbackStore:
    """SQLite store for agent-reported compression feedback.

    Opens its own connection to ``db_path`` — safe to share the file
    with other stores (e.g. surfacing ``FeedbackStore``) because SQLite
    WAL journaling permits multiple concurrent connections and the
    tables are disjoint.
    """

    def __init__(self, db_path: Path, retention_days: int = 0) -> None:
        self._db_path = db_path
        self._retention_days = retention_days
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(str(self._db_path), check_same_thread=False)
        try:
            ensure_private_db_files(self._db_path)
            tune_connection(db)
            db.executescript(_SCHEMA)
            # Startup purge of aged-out rows (#584) — the table is otherwise
            # unbounded. Runs once per process start, mirroring ProxyCache's
            # startup expiry purge. ``retention_days=0`` disables.
            if self._retention_days > 0:
                cutoff = time.time() - self._retention_days * 86400.0
                deleted = db.execute(
                    "DELETE FROM compression_feedback WHERE created_at < ?", (cutoff,)
                ).rowcount
                if deleted:
                    logger.info(
                        "Purged %d compression_feedback rows older than %d days (#584)",
                        deleted,
                        self._retention_days,
                    )
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
        server: str,
        tool: str,
        kind: str,
        missing: str,
        trace_id: str | None,
    ) -> None:
        """Persist a feedback row. ``kind`` is assumed already validated.

        Synchronous sqlite write on the asyncio event loop: while it
        runs, every runnable coroutine stalls — other in-flight proxied
        calls included, not just the feedback call being served.
        Accepted for the current local single-MCP-client deployment
        (low call volume; cheap local insert). Multi-client serving (or
        materially higher concurrency) is the reopen trigger: move the
        write off-loop (e.g. ``asyncio.to_thread``) — and note
        ``self._lock`` serializes the write paths only; reads share the
        connection unlocked, so a thread move also needs every
        connection access locked (the ``MetricsStore.__init__``
        reader/writer convention) or per-thread connections.
        """
        if self._db is None:
            return
        with self._lock:
            self._db.execute(
                "INSERT INTO compression_feedback "
                "(server, tool, trace_id, kind, missing, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (server, tool, trace_id, kind, missing, time.time()),
            )
            self._db.commit()

    def get_tool_feedback_summary(
        self, since_seconds: float = 86400.0
    ) -> dict[tuple[str, str], dict]:
        """Aggregate feedback per ``(server, tool)`` for auto-tuner analysis.

        Returns ``{(server, tool): {"total": int, "by_kind": {kind: count}}}``
        within the given time window.  Returns an empty dict when the
        store is closed or there are no rows.

        Keyed by ``(server, tool)``, not tool name alone: rows are recorded
        with both, and two upstreams can expose the same tool name — a
        tool-only aggregate would attribute one server's feedback to the
        other's profile, and the tuner turns that into a per-server config
        recommendation.
        """
        if self._db is None:
            return {}
        cutoff = time.time() - since_seconds
        rows = self._db.execute(
            "SELECT server, tool, kind, COUNT(*) FROM compression_feedback "
            "WHERE created_at >= ? GROUP BY server, tool, kind",
            (cutoff,),
        ).fetchall()
        result: dict[tuple[str, str], dict] = {}
        for server_name, tool_name, kind, count in rows:
            key = (server_name, tool_name)
            if key not in result:
                result[key] = {"total": 0, "by_kind": {}}
            result[key]["total"] += count
            result[key]["by_kind"][kind] = count
        return result

    def get_stats(self, tool: str | None = None) -> dict:
        """Return counts for ``stm_compression_stats``.

        Shape::

            {
                "total_feedback": int,
                "by_kind": {kind: count},
                "by_tool": {tool_name: count},  # empty when tool filter is set
            }

        When ``tool`` is provided, ``by_tool`` is returned as an empty
        dict so callers can rely on the key always being present without
        having to branch on the filter.
        """
        if self._db is None:
            return {"total_feedback": 0, "by_kind": {}, "by_tool": {}}

        if tool is not None:
            kind_rows = self._db.execute(
                "SELECT kind, COUNT(*) FROM compression_feedback WHERE tool = ? GROUP BY kind",
                (tool,),
            ).fetchall()
            total = self._db.execute(
                "SELECT COUNT(*) FROM compression_feedback WHERE tool = ?",
                (tool,),
            ).fetchone()[0]
            return {
                "total_feedback": total,
                "by_kind": {r[0]: r[1] for r in kind_rows},
                "by_tool": {},
            }

        kind_rows = self._db.execute(
            "SELECT kind, COUNT(*) FROM compression_feedback GROUP BY kind"
        ).fetchall()
        tool_rows = self._db.execute(
            "SELECT tool, COUNT(*) FROM compression_feedback GROUP BY tool"
        ).fetchall()
        total = sum(r[1] for r in kind_rows)
        return {
            "total_feedback": total,
            "by_kind": {r[0]: r[1] for r in kind_rows},
            "by_tool": {r[0]: r[1] for r in tool_rows},
        }
