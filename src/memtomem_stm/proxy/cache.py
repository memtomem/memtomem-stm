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

from memtomem_stm.proxy.privacy import contains_sensitive_content
from memtomem_stm.utils.sqlite_private import ensure_private_db_files
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


# Substrings that flag a response embedding a TRANSIENT retrieval key pointing
# into a process-local pending store. Such payloads must never be cached/served:
# the key outlives neither a process restart nor the (shorter) pending-store TTL,
# so a cache hit would hand the agent a dead ``stm_proxy_read_more`` /
# ``stm_proxy_select_chunks`` key with the response tail unrecoverable.
#   - progressive first-chunks carry ``progressive.PROGRESSIVE_FOOTER_TOKEN``.
#   - SELECTIVE / HYBRID chunk TOCs are JSON objects carrying BOTH a
#     ``"selection_key"`` field and a ``"ttl_seconds_remaining"`` field
#     (``SelectiveCompressor`` in compression.py). We require the PAIR — not
#     ``"selection_key"`` alone — so an arbitrary upstream JSON that merely
#     contains a ``selection_key`` field is not misclassified as transient. The
#     pair is spacing-invariant, matching both the spaced selective dump and the
#     compact ``HybridCompressor._fit_toc_tail`` dump (whose abbreviated call
#     hint drops ``stm_proxy_select_chunks(``, which is why we key on fields).
# Kept here (the persistence layer) so the store-side guard and the startup
# legacy purge below can never diverge.
_PROGRESSIVE_MARKER = "\n---\n[progressive: chars="
_SELECTION_KEY_MARKER = '"selection_key"'
_TOC_SHAPE_MARKER = '"ttl_seconds_remaining"'


def response_carries_transient_key(text: str) -> bool:
    """True if ``text`` embeds a transient pending-store retrieval key.

    Used by ``ProxyManager`` to skip caching such responses and by
    :meth:`ProxyCache.initialize` to purge any that pre-date the guard.
    """
    if _PROGRESSIVE_MARKER in text:
        return True
    return _SELECTION_KEY_MARKER in text and _TOC_SHAPE_MARKER in text


class ProxyCache:
    """SQLite-backed cross-restart cache for proxied tool results.

    Every method does synchronous sqlite I/O on the asyncio event loop:
    while a ``get``/``set`` runs, every runnable coroutine stalls — other
    in-flight proxied calls included, not just the one being served.
    Accepted for the current local single-MCP-client deployment, where
    call volume is low and the I/O is far cheaper than the upstream call
    a hit avoids. Multi-client serving (or materially higher concurrency)
    is the reopen trigger: move the I/O off-loop (e.g.
    ``asyncio.to_thread``) — all connection access here already goes
    through ``self._lock``, so this store is lock-ready for that move.
    """

    def __init__(self, db_path: Path, max_entries: int = 10000) -> None:
        self._db_path = db_path
        self._max_entries = max_entries
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        # Process-lifetime count of rows dropped by ``_trim`` (max_entries
        # overflow). Surfaced via ``stats()`` so an operator can see the cache
        # thrashing instead of it evicting silently. Resets on restart.
        self._evictions = 0

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=5.0)
        try:
            ensure_private_db_files(self._db_path)
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
            # One-time purge of legacy rows whose cached result embeds a
            # transient retrieval key (progressive/SELECTIVE/HYBRID TOC). These
            # pre-date the store-side guard and would otherwise serve a dead key
            # after restart/TTL skew until they expire (or forever for no-TTL
            # caches). ``instr`` is a case-sensitive literal substring match, so
            # the predicate mirrors ``response_carries_transient_key`` exactly
            # (progressive footer OR the selection_key + ttl_seconds_remaining
            # pair) — no LIKE wildcard/case-folding divergence.
            db.execute(
                "DELETE FROM proxy_cache WHERE instr(result, ?) > 0 "
                "OR (instr(result, ?) > 0 AND instr(result, ?) > 0)",
                (_PROGRESSIVE_MARKER, _SELECTION_KEY_MARKER, _TOC_SHAPE_MARKER),
            )
            db.commit()
            # Purge of legacy rows cached before the privacy gate in ``set()``
            # (#453) — they may embed secret-looking content that SECURITY.md
            # promises is never persisted. Privacy patterns are Python regexes,
            # so unlike the marker purge above this scans rows in Python; the
            # scan and the gate share ``contains_sensitive_content`` so they
            # can never diverge. Runs on every startup (matching the marker
            # purge) — after the first pass it finds nothing, because ``set()``
            # refuses new matching rows. Rows written LATER by an older
            # still-running pre-gate process are outside this purge's reach;
            # the read-side guard in ``get()`` refuses and evicts those.
            stale_keys = [
                key
                for key, result in db.execute("SELECT cache_key, result FROM proxy_cache")
                if contains_sensitive_content(result)
            ]
            if stale_keys:
                db.executemany(
                    "DELETE FROM proxy_cache WHERE cache_key = ?",
                    [(key,) for key in stale_keys],
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
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT result, created_at, ttl_seconds FROM proxy_cache WHERE cache_key = ?",
                    (key,),
                ).fetchone()
        except sqlite3.Error:
            # A lookup fault (disk I/O error, page-level corruption surfacing
            # mid-session, an external writer holding the file past the busy
            # timeout) must degrade to a plain MISS — the cache is an optional
            # optimization and a read fault must never fail the proxied call.
            # Mirrors the privacy-eviction guard below and GraphConsultCache.get.
            logger.warning(
                "Cache lookup failed for %s/%s — serving a miss",
                server,
                tool,
                exc_info=True,
            )
            return None
        if row is None:
            return None
        entry = CacheEntry(result=row[0], created_at=row[1], ttl_seconds=row[2])
        if contains_sensitive_content(entry.result):
            # Read-side mirror of the ``set()`` gate: a row can land here
            # without passing ``set()`` — written by an older still-running
            # pre-gate process or an external SQL writer — and the startup
            # purge only covers rows present at ``initialize()``. Serving it
            # would break the SECURITY.md exclusion, so evict and miss.
            # Checked BEFORE expiry: an expired sensitive row must still be
            # deleted, not left resting on disk until the next startup.
            try:
                with self._lock:
                    self._db.execute("DELETE FROM proxy_cache WHERE cache_key = ?", (key,))
                    self._db.commit()
                logger.debug(
                    "Evicted cached response for %s/%s: result matches a privacy pattern",
                    server,
                    tool,
                )
            except sqlite3.Error:
                # Eviction is best-effort: a concurrent writer holding the
                # file lock must degrade this to a plain miss, never abort
                # the caller's request. The row is retried on the next
                # ``get()`` and swept by the next startup purge.
                logger.warning(
                    "Privacy eviction failed for %s/%s — serving a miss",
                    server,
                    tool,
                    exc_info=True,
                )
            return None
        if entry.is_expired():
            return None
        return entry.result

    def invalidate(self, server: str, tool: str, args: dict[str, Any]) -> None:
        """Delete any cached row for ``(server, tool, args)`` — a single
        best-effort DELETE, keyed exactly as ``set``/``get`` (``_make_key``).

        Two callers: the ``set(ttl<=0)`` do-not-store short-circuit below, and
        the manager's non-text / mixed disabled-cache path, which never reaches
        ``set`` and so otherwise leaves a stale row from an earlier text response
        for the same key (#541)."""
        if self._db is None:
            return
        key = _make_key(server, tool, args)
        with self._lock:
            self._db.execute("DELETE FROM proxy_cache WHERE cache_key = ?", (key,))
            self._db.commit()

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
        if ttl_seconds is not None and ttl_seconds <= 0:
            # A non-positive TTL makes every row born-expired (``is_expired`` is
            # ``now >= created_at + 0`` → always true), so storing one only burns
            # write+trim I/O for a guaranteed miss — a "cache enabled, 0% hits"
            # footgun. Treat it as do-not-store: set ``cache.enabled=False`` to turn
            # caching off, or a positive TTL to cache. (``None`` is the distinct
            # "never expires" sentinel and still stores.)
            #
            # Still INVALIDATE any existing row for this key: an earlier call may
            # have cached it under a positive TTL (e.g. before the TTL was lowered
            # to 0 via hot-reload), and leaving that live row would keep serving
            # stale content. This mirrors the pre-short-circuit behavior, which
            # overwrote the key with a born-expired row.
            self.invalidate(server, tool, args)
            return
        if contains_sensitive_content(result):
            # SECURITY.md: responses that look like secrets are never
            # persisted to the response cache. Enforced at the store
            # chokepoint so no caller can bypass it (#453); a false positive
            # only costs one un-cached response, never correctness.
            logger.debug(
                "Skipping cache store for %s/%s: response matches a privacy pattern",
                server,
                tool,
            )
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
            cur = self._db.execute(
                "DELETE FROM proxy_cache WHERE cache_key IN "
                "(SELECT cache_key FROM proxy_cache ORDER BY created_at ASC LIMIT ?)",
                (excess,),
            )
            self._db.commit()
            self._evictions += cur.rowcount

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
            return {"total_entries": 0, "expired_entries": 0, "evictions": self._evictions}
        now = time.time()
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0]
            expired = self._db.execute(
                "SELECT COUNT(*) FROM proxy_cache WHERE ttl_seconds IS NOT NULL AND created_at + ttl_seconds <= ?",
                (now,),
            ).fetchone()[0]
        return {"total_entries": total, "expired_entries": expired, "evictions": self._evictions}
