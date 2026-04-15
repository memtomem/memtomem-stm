"""SQLite-backed response cache for proxied MCP tool calls."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memtomem_stm.utils.sqlite_tuning import tune_connection

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS proxy_cache (
    cache_key   TEXT    PRIMARY KEY,
    server      TEXT    NOT NULL,
    tool        TEXT    NOT NULL,
    result      TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    ttl_seconds REAL
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_proxy_cache_server_tool
ON proxy_cache (server, tool);
"""


@dataclass
class CacheEntry:
    result: str
    created_at: float
    ttl_seconds: float | None

    def is_expired(self) -> bool:
        if self.ttl_seconds is None:
            return False
        return time.time() >= self.created_at + self.ttl_seconds


def _make_key(server: str, tool: str, args: dict[str, Any]) -> str:
    raw = f"{server}:{tool}:{json.dumps(args, sort_keys=True)}"
    return hashlib.sha256(raw.encode()).hexdigest()


class ProxyCache:
    def __init__(self, db_path: Path, max_entries: int = 10000) -> None:
        self._db_path = db_path
        self._max_entries = max_entries
        self._db: sqlite3.Connection | None = None
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
            db.execute(_CREATE_TABLE)
            db.execute(_CREATE_INDEX)
            db.commit()
            # Startup purge of expired rows, running against the local
            # ``db`` before it is handed off to ``self._db``. Failures fall
            # through to the outer except so ``self._db`` stays ``None``.
            db.execute(
                "DELETE FROM proxy_cache WHERE ttl_seconds IS NOT NULL "
                "AND created_at + ttl_seconds <= ?",
                (time.time(),),
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

    def get(self, server: str, tool: str, args: dict[str, Any]) -> str | None:
        if self._db is None:
            return None
        key = _make_key(server, tool, args)
        with self._lock:
            row = self._db.execute(
                "SELECT result, created_at, ttl_seconds FROM proxy_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        entry = CacheEntry(result=row[0], created_at=row[1], ttl_seconds=row[2])
        if entry.is_expired():
            return None
        return entry.result

    def set(
        self,
        server: str,
        tool: str,
        args: dict[str, Any],
        result: str,
        ttl_seconds: float | None,
    ) -> None:
        if self._db is None:
            return
        key = _make_key(server, tool, args)
        now = time.time()
        with self._lock:
            self._db.execute(
                """
                INSERT INTO proxy_cache (cache_key, server, tool, result, created_at, ttl_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    result      = excluded.result,
                    created_at  = excluded.created_at,
                    ttl_seconds = excluded.ttl_seconds
                """,
                (key, server, tool, result, now, ttl_seconds),
            )
            self._db.commit()
            self._trim()

    def _trim(self) -> None:
        if self._db is None:
            return
        count = self._db.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0]
        if count > self._max_entries:
            excess = count - self._max_entries
            self._db.execute(
                "DELETE FROM proxy_cache WHERE cache_key IN "
                "(SELECT cache_key FROM proxy_cache ORDER BY created_at ASC LIMIT ?)",
                (excess,),
            )
            self._db.commit()

    def clear(self, *, server: str | None = None, tool: str | None = None) -> int:
        if self._db is None:
            return 0
        with self._lock:
            if server is not None and tool is not None:
                cur = self._db.execute(
                    "DELETE FROM proxy_cache WHERE server = ? AND tool = ?", (server, tool)
                )
            elif server is not None:
                cur = self._db.execute("DELETE FROM proxy_cache WHERE server = ?", (server,))
            elif tool is not None:
                cur = self._db.execute("DELETE FROM proxy_cache WHERE tool = ?", (tool,))
            else:
                cur = self._db.execute("DELETE FROM proxy_cache")
            self._db.commit()
            return cur.rowcount

    def purge_expired(self) -> int:
        if self._db is None:
            return 0
        with self._lock:
            now = time.time()
            cur = self._db.execute(
                "DELETE FROM proxy_cache WHERE ttl_seconds IS NOT NULL AND created_at + ttl_seconds <= ?",
                (now,),
            )
            self._db.commit()
            return cur.rowcount

    def stats(self) -> dict[str, int]:
        if self._db is None:
            return {"total_entries": 0, "expired_entries": 0}
        now = time.time()
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0]
            expired = self._db.execute(
                "SELECT COUNT(*) FROM proxy_cache WHERE ttl_seconds IS NOT NULL AND created_at + ttl_seconds <= ?",
                (now,),
            ).fetchone()[0]
        return {"total_entries": total, "expired_entries": expired}
