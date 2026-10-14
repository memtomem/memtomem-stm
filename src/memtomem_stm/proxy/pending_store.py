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
    """Protocol for pending TOC selection storage.

    Recency contract (#901). ``put`` and ``touch`` both mark a key
    most-recent, and ``evict_oldest`` drops the least-recently put-or-touched
    keys first. That order must be total and deterministic: a backend may not
    let two keys written close together come out in an arbitrary order, since
    the loser can be a selection key a client is holding. The contract is
    stated in terms of RELATIVE recency, not timestamps — the backends here
    keep different clocks (``InMemoryPendingStore`` monotonic,
    ``SQLitePendingStore`` wall), so ``created_at`` values are not comparable
    across them. ``evict_expired`` ages rows against the backend's own clock.
    """

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
    # in recency order (oldest left) — matching the SQLite backend, which keeps
    # the highest ``seq`` rows and takes the next ``seq`` on both put and touch,
    # so the two express the same operation order rather than two readings of a
    # clock (#901). Every mutation below maintains it; a
    # duplicate or stale ``_order`` entry makes evict_oldest silently drop a
    # fresh entry.

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
    """SQLite-backed pending store for multi-instance sharing.

    Instances sharing one ``pending_store_path`` must run the same version.
    The ``seq`` column added for #901 makes the table six columns wide, and a
    process still running the older code inserts positionally into five, so its
    writes fail against an upgraded file (the manager degrades those to a
    non-SELECTIVE strategy through its ``sqlite3.Error`` guard rather than
    failing the call). Reads and expiry from the older code keep working, so an
    upgrade strands no key that is already out — but the instances have to be
    upgraded together rather than one at a time.
    """

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
                    total_chars INTEGER NOT NULL,
                    seq INTEGER
                )"""
            )
            # ``seq`` is the eviction order (#901), added after the table
            # shipped. Detect it rather than stamping a schema version: this
            # file is shared, and a version pragma is a property of the DATABASE
            # rather than of a table, so another component's value would make
            # the migration silently not run (#797).
            #
            # Checked twice, and the cheap check comes first. Once the column
            # exists — every open after the first — this reads it without a
            # lock and returns, which matters because opening is not always a
            # write: ``select_chunks`` and ``read_more`` open throwaway stores
            # purely to probe for a key, and taking the write lock to tell them
            # a migration is not needed makes them wait out, or fail behind,
            # an unrelated writer.
            if not self._has_seq_column(db):
                # The migration itself is a write, and ALTER plus backfill have
                # to be ONE of them. ``BEGIN IMMEDIATE`` takes the write lock up
                # front, then the column is checked AGAIN — that second read is
                # the authoritative one, since another opener may have migrated
                # the file while this one waited for the lock. Nothing may land
                # between the ALTER and the backfill either: such a row would be
                # given a ``seq`` and then have the backfill overwrite it with a
                # rowid-derived one, colliding with a rank already handed out.
                # Existing rows seed from ``rowid``, the insertion order they
                # already carry.
                db.execute("BEGIN IMMEDIATE")
                try:
                    if not self._has_seq_column(db):
                        db.execute("ALTER TABLE pending_selections ADD COLUMN seq INTEGER")
                        db.execute("UPDATE pending_selections SET seq = rowid WHERE seq IS NULL")
                except Exception:
                    db.rollback()
                    raise
                db.commit()
        except Exception:
            db.close()
            raise
        self._db = db

    @staticmethod
    def _has_seq_column(db: sqlite3.Connection) -> bool:
        return any(row[1] == "seq" for row in db.execute("PRAGMA table_info(pending_selections)"))

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    def _get_db(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("SQLitePendingStore not initialized")
        return self._db

    # The next eviction rank, computed inside the writing statement so two
    # processes sharing this file cannot read the same maximum and mint the
    # same rank: SQLite serializes writers, so the subquery sees every earlier
    # commit (#901).
    _NEXT_SEQ = "(SELECT IFNULL(MAX(seq), 0) + 1 FROM pending_selections)"

    def put(self, key: str, selection: PendingSelection) -> None:
        with self._lock:
            self._get_db().execute(
                "INSERT OR REPLACE INTO pending_selections "
                "(key, chunks_json, format, created_at, total_chars, seq) "
                f"VALUES (?, ?, ?, ?, ?, {self._NEXT_SEQ})",
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
            # Bump the eviction rank as well as the timestamp: a key a reader
            # just selected must count as the most recently used, not the least
            # (#901). Only the two narrow columns are written — the row keeps
            # its identity and its payload. That matters here more than
            # anywhere: progressive stores an entire response body in
            # ``chunks_json``, and ``read_more`` touches the row once per chunk,
            # so a scheme that rewrote the payload to re-rank it would rewrite
            # the whole response on every chunk (3956 ms against this
            # implementation's 461 ms for a 2 MB response read in 4 KB chunks).
            self._get_db().execute(
                f"UPDATE pending_selections SET created_at = ?, seq = {self._NEXT_SEQ} "
                "WHERE key = ?",
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
            # Order on ``seq``, not ``created_at`` (#901). Recency here means
            # the order operations happened in, and ``seq`` records exactly
            # that: every put and every touch takes the next value.
            # ``created_at`` only ever approximated it, and badly in two ways —
            # rows written inside one clock tick tie, which left the survivors
            # unspecified and let a trim discard the key just handed to a client
            # (reproduced on the Windows runner); and the wall clock can step
            # BACKWARD, which ranks a later write as older however the ties are
            # broken. A shared store makes that worse, since the timestamps then
            # come from several machines' clocks while ``seq`` stays one
            # sequence per file. ``created_at`` remains the right basis for
            # ``evict_expired``, which asks about age rather than order.
            #
            # Every row this class writes carries a ``seq``, and the migration
            # backfills the ones that predate it, so a NULL rank means a row
            # some other writer put here by hand. Those sort last and are
            # trimmed first, deliberately: this store cannot know where such a
            # row belongs in its sequence, and no key it handed out is riding on
            # it. Falling back to ``rowid`` instead would be worse than useless
            # — the two are different number domains, so once touches push
            # ``seq`` past the rowid range a freshly inserted row would compare
            # as older than rows written long before it.
            self._get_db().execute(
                f"DELETE FROM pending_selections WHERE {where} AND key NOT IN "
                f"(SELECT key FROM pending_selections WHERE {where} "
                "ORDER BY seq DESC LIMIT ?)",
                [*params, *params, max_size],
            )
            self._get_db().commit()

    def __len__(self) -> int:
        with self._lock:
            row = self._get_db().execute("SELECT COUNT(*) FROM pending_selections").fetchone()
        return row[0] if row else 0
