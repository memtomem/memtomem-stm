"""Pending selection storage backends for SelectiveCompressor."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Protocol

from memtomem_stm.proxy.compression import PendingSelection
from memtomem_stm.utils.json_out import dumps as _json_dumps
from memtomem_stm.utils.json_out import scrub_lone_surrogates
from memtomem_stm.utils.sqlite_private import ensure_private_db_files
from memtomem_stm.utils.sqlite_tuning import tune_connection

logger = logging.getLogger(__name__)


class PendingStore(Protocol):
    """Protocol for pending TOC selection storage."""

    def put(self, key: str, selection: PendingSelection) -> None: ...
    def get(self, key: str) -> PendingSelection | None: ...
    def touch(self, key: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def evict_expired(
        self, ttl: float, *, format: str | None = None, exclude_format: str | None = None
    ) -> None: ...
    def evict_oldest(
        self, max_size: int, *, format: str | None = None, exclude_format: str | None = None
    ) -> None: ...
    def __len__(self) -> int: ...


class InMemoryPendingStore:
    """In-memory pending store (default, single-instance)."""

    def __init__(self) -> None:
        self._data: dict[str, PendingSelection] = {}
        self._order: deque[str] = deque()
        self._lock = threading.Lock()

    # Invariant: ``_order`` holds exactly the keys of ``_data``, once each,
    # in recency order (oldest left) — matching the SQLite backend, whose
    # evict_oldest keeps the most recent ``created_at`` rows. Every mutation
    # below maintains it; a duplicate or stale ``_order`` entry makes
    # evict_oldest silently drop a fresh entry.

    def put(self, key: str, selection: PendingSelection) -> None:
        with self._lock:
            if key in self._data:
                self._order.remove(key)
            self._data[key] = selection
            self._order.append(key)

    def get(self, key: str) -> PendingSelection | None:
        with self._lock:
            return self._data.get(key)

    def touch(self, key: str) -> None:
        with self._lock:
            sel = self._data.get(key)
            if sel is not None:
                sel.created_at = time.monotonic()
                self._order.remove(key)
                self._order.append(key)

    def delete(self, key: str) -> None:
        with self._lock:
            if self._data.pop(key, None) is not None:
                self._order.remove(key)

    def evict_expired(
        self, ttl: float, *, format: str | None = None, exclude_format: str | None = None
    ) -> None:
        with self._lock:
            now = time.monotonic()
            expired = {
                k
                for k, v in self._data.items()
                if (now - v.created_at) > ttl
                and (format is None or v.format == format)
                and (exclude_format is None or v.format != exclude_format)
            }
            for k in expired:
                self._data.pop(k, None)
            if expired:
                self._order = deque(k for k in self._order if k not in expired)

    def evict_oldest(
        self, max_size: int, *, format: str | None = None, exclude_format: str | None = None
    ) -> None:
        with self._lock:
            scoped = [
                key
                for key in self._order
                if (format is None or self._data[key].format == format)
                and (exclude_format is None or self._data[key].format != exclude_format)
            ]
            for oldest in scoped[: max(0, len(scoped) - max_size)]:
                self._data.pop(oldest, None)
                self._order.remove(oldest)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class SQLitePendingStore:
    """SQLite-backed pending store for multi-instance sharing."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=5.0)
        try:
            ensure_private_db_files(self._db_path)
            tune_connection(db)
            db.execute(
                """CREATE TABLE IF NOT EXISTS pending_selections (
                    key TEXT PRIMARY KEY,
                    chunks_json TEXT NOT NULL,
                    format TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    total_chars INTEGER NOT NULL
                )"""
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

    def _get_db(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("SQLitePendingStore not initialized")
        return self._db

    def put(self, key: str, selection: PendingSelection) -> None:
        with self._lock:
            self._get_db().execute(
                "INSERT OR REPLACE INTO pending_selections VALUES (?, ?, ?, ?, ?)",
                (
                    key,
                    _json_dumps(selection.chunks, ensure_ascii=False),
                    selection.format,
                    time.time(),
                    selection.total_chars,
                ),
            )
            self._get_db().commit()

    def get(self, key: str) -> PendingSelection | None:
        with self._lock:
            row = (
                self._get_db()
                .execute(
                    "SELECT chunks_json, format, created_at, total_chars "
                    "FROM pending_selections WHERE key = ?",
                    (key,),
                )
                .fetchone()
            )
        if row is None:
            return None
        try:
            # Scrub on the way out as well as in. The writer escapes, but this
            # is a plain ``json.loads``: a ``\ud800`` escape sitting in a row
            # some other writer (or a hand edit) put there decodes straight back
            # into the code unit, and would then raise at the next encode
            # downstream rather than here (#761).
            chunks = scrub_lone_surrogates(json.loads(row[0]))
        except json.JSONDecodeError:
            logger.warning(
                "Corrupted chunks_json in pending_selections for key=%s; treating as miss",
                key,
            )
            return None
        return PendingSelection(
            chunks=chunks,
            format=row[1],
            created_at=row[2],
            total_chars=row[3],
        )

    def touch(self, key: str) -> None:
        with self._lock:
            self._get_db().execute(
                "UPDATE pending_selections SET created_at = ? WHERE key = ?",
                (time.time(), key),
            )
            self._get_db().commit()

    def delete(self, key: str) -> None:
        with self._lock:
            self._get_db().execute("DELETE FROM pending_selections WHERE key = ?", (key,))
            self._get_db().commit()

    def evict_expired(
        self, ttl: float, *, format: str | None = None, exclude_format: str | None = None
    ) -> None:
        cutoff = time.time() - ttl
        where = "created_at < ?"
        params: list[object] = [cutoff]
        if format is not None:
            where += " AND format = ?"
            params.append(format)
        if exclude_format is not None:
            where += " AND format != ?"
            params.append(exclude_format)
        with self._lock:
            self._get_db().execute(f"DELETE FROM pending_selections WHERE {where}", params)
            self._get_db().commit()

    def evict_oldest(
        self, max_size: int, *, format: str | None = None, exclude_format: str | None = None
    ) -> None:
        where = "1=1"
        params: list[object] = []
        if format is not None:
            where += " AND format = ?"
            params.append(format)
        if exclude_format is not None:
            where += " AND format != ?"
            params.append(exclude_format)
        with self._lock:
            self._get_db().execute(
                f"DELETE FROM pending_selections WHERE {where} AND key NOT IN "
                f"(SELECT key FROM pending_selections WHERE {where} "
                "ORDER BY created_at DESC LIMIT ?)",
                [*params, *params, max_size],
            )
            self._get_db().commit()

    def __len__(self) -> int:
        with self._lock:
            row = self._get_db().execute("SELECT COUNT(*) FROM pending_selections").fetchone()
        return row[0] if row else 0
