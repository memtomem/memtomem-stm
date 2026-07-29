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
from memtomem_stm.utils.json_out import dumps as _json_dumps
from memtomem_stm.utils.json_out import (
    escape_lone_surrogates,
    has_lone_surrogate,
    scrub_lone_surrogates,
)
from memtomem_stm.utils.sqlite_private import ensure_private_db_files
from memtomem_stm.utils.sqlite_tuning import tune_connection

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS proxy_cache (
    cache_key     TEXT    PRIMARY KEY,
    server        TEXT    NOT NULL,
    tool          TEXT    NOT NULL,
    result        TEXT    NOT NULL,
    created_at    REAL    NOT NULL,
    ttl_seconds   REAL,
    envelope_json TEXT,
    envelope_safe INTEGER NOT NULL DEFAULT 0
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


class CachedResponse(str):
    """Cached text plus an optional safe MCP result-level envelope."""

    structured_content: dict[str, Any] | None
    meta: dict[str, Any] | None

    def __new__(
        cls,
        value: str,
        *,
        structured_content: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> CachedResponse:
        obj = super().__new__(cls, value)
        obj.structured_content = structured_content
        obj.meta = meta
        return obj


# Bump when the key derivation OR the row contract changes shape so
# ``initialize()`` can purge rows written under an older scheme (opaque hashes
# make them unreachable but otherwise immortal for ``ttl_seconds NULL`` rows).
# Stored in SQLite's ``PRAGMA user_version``.
# v4: successful text responses may carry JSON-safe ``structuredContent`` and
# result-level ``_meta`` in ``envelope_json``.
# v5: framed (length-prefixed) key derivation, args/context_query serialized
# with ``ensure_ascii=False`` (#784).
_KEY_SCHEMA_VERSION = 5


def _make_key(
    server: str,
    tool: str,
    args: dict[str, Any],
    *,
    context_query: str | None = None,
    config_fingerprint: str = "",
) -> str:
    # ``context_query`` and ``config_fingerprint`` are part of the key because
    # the stored body is the COMPRESSED response: compression is query-aware
    # (BM25 relevance budgets) and config-dependent, so the same tool+args can
    # legitimately map to different cached bodies. ``json.dumps`` keeps ``None``
    # (absent) distinct from ``""`` and the literal string ``"null"``.
    #
    # The digest input must be injective over the SERIALIZED component tuple —
    # a collision serves one call's cached body for a different call (#784).
    # Serialized, not the Python objects: ``args`` is keyed by its JSON form,
    # so two argument trees that ``json.dumps`` renders identically
    # deliberately share a row. That is the correct equivalence class, because
    # the same rendering is what the upstream tool receives — a tuple and a
    # list both go out as ``[1, 2]``, an int dict key and its string spelling
    # both as ``"1"`` — so the upstream cannot tell them apart either and owes
    # them the same response. Widening the key past what the upstream sees
    # would only split rows that must not be split.
    #
    # - Each component is framed netstring-style (``len:data``) rather than
    #   joined on a separator. A joined string is ambiguous the moment a
    #   component can contain the separator, and nothing on the path rejects
    #   a NUL in an upstream server or tool name; frames parse left-to-right
    #   unambiguously, so no boundary can shift.
    # - ``ensure_ascii=False``: the default ASCII escaping renders an astral
    #   scalar as the same ``\uXXXX\uXXXX`` text as the two lone surrogate
    #   code units spelled separately, aliasing the two. Unescaped, the
    #   scalar and the lone pair encode to different bytes below.
    # - ``surrogatepass``, not the escaping helper. ``escape_lone_surrogates``
    #   is documented as non-injective — it maps the code unit U+D800 and the
    #   six literal characters ``\ud800`` onto the same text — so deriving
    #   the key through it let one identifier's row answer for a different
    #   one. Nothing decodes these bytes, so the usual objection to
    #   ``surrogatepass`` (it emits bytes that are not valid UTF-8) has no
    #   consumer to bite.
    #
    # All three derivations — ``get``, ``set`` and ``invalidate`` — come
    # through here, so they cannot disagree.
    digest = hashlib.sha256()
    for component in (
        str(_KEY_SCHEMA_VERSION),
        server,
        tool,
        json.dumps(args, sort_keys=True, ensure_ascii=False),
        config_fingerprint,
        json.dumps(context_query, ensure_ascii=False),
    ):
        data = component.encode("utf-8", errors="surrogatepass")
        digest.update(f"{len(data)}:".encode())
        digest.update(data)
    return digest.hexdigest()


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
            # One-time purge on schema change, run BEFORE table creation: rows
            # keyed under an older ``_make_key`` shape are opaque hashes no
            # current lookup can ever produce, so they would sit as dead
            # weight — forever, for ``ttl_seconds NULL`` rows — while still
            # counting against ``max_entries``. DROP (not DELETE) so the
            # recreate below also picks up column additions without a separate
            # ALTER migration.
            # ``user_version`` is 0 for both fresh and pre-versioning
            # databases; the DROP on a fresh database is a no-op.
            (schema_version,) = db.execute("PRAGMA user_version").fetchone()
            if schema_version < _KEY_SCHEMA_VERSION:
                db.execute("DROP TABLE IF EXISTS proxy_cache")
                db.execute(f"PRAGMA user_version = {_KEY_SCHEMA_VERSION}")
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
                for key, result, envelope in db.execute(
                    "SELECT cache_key, result, envelope_json FROM proxy_cache"
                )
                if contains_sensitive_content(result)
                or (envelope is not None and contains_sensitive_content(envelope))
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

    def get(
        self,
        server: str,
        tool: str,
        args: dict[str, Any],
        *,
        context_query: str | None = None,
        config_fingerprint: str = "",
    ) -> CachedResponse | None:
        if self._db is None:
            return None
        key = _make_key(
            server, tool, args, context_query=context_query, config_fingerprint=config_fingerprint
        )
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT result, created_at, ttl_seconds, envelope_safe, envelope_json "
                    "FROM proxy_cache WHERE cache_key = ?",
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
        envelope_json = row[4]
        if contains_sensitive_content(entry.result) or (
            envelope_json is not None and contains_sensitive_content(envelope_json)
        ):
            # Read-side mirror of the ``set()`` gate: a row can land here
            # without passing ``set()`` — written by an older still-running
            # pre-gate process or an external SQL writer — and the startup
            # purge only covers rows present at ``initialize()``. Serving it
            # would break the SECURITY.md exclusion, so evict and miss.
            # Checked BEFORE expiry: an expired sensitive row must still be
            # deleted, not left resting on disk until the next startup.
            self._evict_row(key, server, tool, reason="result matches a privacy pattern")
            return None
        if not row[3]:
            # Envelope-safety marker (v3): every row written by ``set()`` is
            # marked 1; an unmarked row can only come from an out-of-band
            # writer (an older binary, external SQL) and cannot prove it is a
            # text-only success without ``structuredContent``/``_meta``, so
            # serving it could silently drop envelope fields. Checked after
            # the privacy eviction (which must still fire for unmarked rows)
            # and before expiry (an unmarked row is a miss either way).
            return None
        if entry.is_expired():
            return None
        structured_content: dict[str, Any] | None = None
        meta: dict[str, Any] | None = None
        if envelope_json is not None:
            parsed = self._parse_envelope(envelope_json)
            if parsed is None:
                # ``set()`` validates before writing, so a malformed envelope
                # can only come from an out-of-band writer. Mirror the
                # sensitive-row eviction above: a plain miss would leave the
                # row as dead weight — immortal for ``ttl_seconds NULL`` rows
                # — while still counting against ``max_entries``.
                self._evict_row(key, server, tool, reason="envelope_json is malformed")
                return None
            structured_content, meta = parsed
        return CachedResponse(entry.result, structured_content=structured_content, meta=meta)

    @staticmethod
    def _parse_envelope(
        envelope_json: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None] | None:
        """Validated ``(structured_content, meta)`` from a stored envelope.

        ``None`` (as the whole tuple) means the row is malformed: non-JSON,
        an unknown ``schema_version``, or a non-dict field value.
        """
        try:
            # Scrub on the way out as well as in. The writer escapes, but this
            # is a plain ``json.loads``: the six characters ``\ud800`` it
            # stored decode straight back into the code unit, which would then
            # raise at the next encode downstream rather than here (#781).
            envelope = scrub_lone_surrogates(json.loads(envelope_json))
        except (TypeError, ValueError):
            return None
        if not isinstance(envelope, dict) or envelope.get("schema_version") != 1:
            return None
        raw_structured = envelope.get("structuredContent")
        raw_meta = envelope.get("_meta")
        if raw_structured is not None and not isinstance(raw_structured, dict):
            return None
        if raw_meta is not None and not isinstance(raw_meta, dict):
            return None
        return raw_structured, raw_meta

    def _evict_row(self, key: str, server: str, tool: str, *, reason: str) -> None:
        """Best-effort single-row eviction from the ``get()`` read path.

        A failure (e.g. a concurrent writer holding the file lock past the
        busy timeout) degrades to a plain miss — it must never abort the
        caller's request. The row is retried on the next ``get()`` and swept
        by the next startup purge.
        """
        if self._db is None:
            return
        try:
            with self._lock:
                self._db.execute("DELETE FROM proxy_cache WHERE cache_key = ?", (key,))
                self._db.commit()
            logger.debug("Evicted cached response for %s/%s: %s", server, tool, reason)
        except sqlite3.Error:
            logger.warning(
                "Cache eviction (%s) failed for %s/%s — serving a miss",
                reason,
                server,
                tool,
                exc_info=True,
            )

    def invalidate(
        self,
        server: str,
        tool: str,
        args: dict[str, Any],
        *,
        context_query: str | None = None,
        config_fingerprint: str = "",
    ) -> None:
        """Delete any cached row for ``(server, tool, args)`` — a single
        best-effort DELETE, keyed exactly as ``set``/``get`` (``_make_key``).

        Two callers: the ``set(ttl<=0)`` do-not-store short-circuit below, and
        the manager's non-text / mixed disabled-cache path, which never reaches
        ``set`` and so otherwise leaves a stale row from an earlier text response
        for the same key (#541)."""
        if self._db is None:
            return
        key = _make_key(
            server, tool, args, context_query=context_query, config_fingerprint=config_fingerprint
        )
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
        *,
        context_query: str | None = None,
        config_fingerprint: str = "",
        structured_content: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
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
            self.invalidate(
                server,
                tool,
                args,
                context_query=context_query,
                config_fingerprint=config_fingerprint,
            )
            return
        envelope_json: str | None = None
        if structured_content is not None or meta is not None:
            try:
                envelope_json = _json_dumps(
                    {
                        "schema_version": 1,
                        "structuredContent": structured_content,
                        "_meta": meta,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError):
                logger.debug(
                    "Skipping cache store for %s/%s: envelope is not JSON-safe", server, tool
                )
                return
        if contains_sensitive_content(result) or (
            envelope_json is not None and contains_sensitive_content(envelope_json)
        ):
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
        if has_lone_surrogate(server) or has_lone_surrogate(tool):
            # ``sqlite3`` encodes text parameters to UTF-8, so such an
            # identifier cannot be stored as one. Escaping it instead would
            # make the row unmatchable by its real name in ``clear()`` and
            # would alias it onto the distinct identifier spelled with those
            # six literal characters, so the honest answer is not to cache
            # this response. The caller treats a non-store as "response
            # unaffected", and this skips exactly as the sensitive-content and
            # non-JSON-envelope checks above do (#781).
            logger.debug(
                "Skipping cache store: server or tool name is not encodable (%r/%r)",
                server,
                tool,
            )
            return
        key = _make_key(
            server, tool, args, context_query=context_query, config_fingerprint=config_fingerprint
        )
        now = time.time()
        with self._lock:
            # ``envelope_safe`` is written on BOTH the insert and the conflict
            # branches: the manager's store-side gate guarantees everything
            # reaching ``set()`` is envelope-safe, and an upsert over an
            # unmarked out-of-band row must re-mark it or the key would miss
            # forever — ``set()`` always leaves a servable row.
            self._db.execute(
                """
                INSERT INTO proxy_cache
                    (cache_key, server, tool, result, created_at, ttl_seconds,
                     envelope_json, envelope_safe)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(cache_key) DO UPDATE SET
                    result        = excluded.result,
                    created_at    = excluded.created_at,
                    ttl_seconds   = excluded.ttl_seconds,
                    envelope_json = excluded.envelope_json,
                    envelope_safe = excluded.envelope_safe
                """,
                (
                    key,
                    server,
                    tool,
                    # The body is content, not an identity: nothing matches on
                    # it, so escaping is right where it would be wrong for the
                    # two names above. Without it ``sqlite3`` raises at
                    # ``execute`` and the response goes uncached (#781).
                    escape_lone_surrogates(result),
                    now,
                    ttl_seconds,
                    envelope_json,
                ),
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
        if has_lone_surrogate(server or "") or has_lone_surrogate(tool or ""):
            # ``set()`` refuses to store such an identifier, so no row can
            # carry one and the filter matches nothing. Binding it anyway
            # would raise out of an admin call that is semantically a no-op
            # here — and escaping it to make the bind succeed would delete the
            # rows of the *distinct* identifier spelled with those six literal
            # characters, which is worse than either (#781).
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
