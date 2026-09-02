"""SQLite-backed response cache for proxied MCP tool calls."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from memtomem_stm.proxy import privacy
from memtomem_stm.utils.digest import framed_digest
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

# Policy bookkeeping is deliberately table-scoped rather than stored in
# ``PRAGMA user_version``. That pragma already belongs to the response-cache
# key/row schema below and, as a database-global slot, can also be stamped by a
# different component when configurable store paths share one SQLite file.
_CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS proxy_cache_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# A small trigger-maintained queue separates "which rows need checking" from
# the (potentially large) response bodies.  The triggers live in SQLite, so
# they also observe writes from an older process that knows only the
# ``proxy_cache`` table. A current writer removes only the key whose body it
# has just checked under the database's published policy; legacy or older-policy
# inserts and body updates remain queued for the next startup sweep.
_CREATE_PRIVACY_UNVERIFIED_TABLE = """
CREATE TABLE IF NOT EXISTS proxy_cache_privacy_unverified (
    cache_key TEXT PRIMARY KEY
);
"""

# Trigger bodies are compared with what the database is actually running and
# replaced when they differ, rather than created with ``IF NOT EXISTS``: a
# trigger already present in the file would make the ``CREATE`` a silent no-op,
# so a later change to what the queue tracks (an extra ``UPDATE OF`` column,
# say) would never reach an existing database. Text that already matches is
# left untouched, which is what lets the comparison mean something.
# Bumping ``_PRIVACY_POLICY_EPOCH`` cannot cover that — it forces one rescan,
# not new trigger text — and the stamp would then assert a policy the stored
# triggers do not implement. Replacement runs inside the write reservation that
# publishes the stamp, so no write can land in the window where the queue is
# untracked and no peer can swap the triggers between the check and the
# publication (see ``_ensure_current_triggers``).
_CREATE_PRIVACY_UNVERIFIED_TRIGGERS = (
    """
    CREATE TRIGGER proxy_cache_privacy_track_insert
    AFTER INSERT ON proxy_cache
    BEGIN
        INSERT INTO proxy_cache_privacy_unverified (cache_key)
        VALUES (NEW.cache_key)
        ON CONFLICT(cache_key) DO NOTHING;
    END;
    """,
    """
    CREATE TRIGGER proxy_cache_privacy_track_body_update
    AFTER UPDATE OF cache_key, result, envelope_json ON proxy_cache
    BEGIN
        DELETE FROM proxy_cache_privacy_unverified
        WHERE cache_key = OLD.cache_key;
        INSERT INTO proxy_cache_privacy_unverified (cache_key)
        VALUES (NEW.cache_key)
        ON CONFLICT(cache_key) DO NOTHING;
    END;
    """,
    """
    CREATE TRIGGER proxy_cache_privacy_track_delete
    AFTER DELETE ON proxy_cache
    BEGIN
        DELETE FROM proxy_cache_privacy_unverified
        WHERE cache_key = OLD.cache_key;
    END;
    """,
)

# Every trigger this store owns carries this prefix, so the set to remove is
# read from the database rather than listed: a build that shipped a FOURTH
# trigger leaves one behind that no static list names, and it would keep
# running while the currentness check — which counts every prefixed trigger —
# stayed false forever.
_PRIVACY_TRIGGER_NAME_PREFIX = "proxy_cache_privacy_track_"
_PRIVACY_TRIGGER_SELECTOR = (len(_PRIVACY_TRIGGER_NAME_PREFIX), _PRIVACY_TRIGGER_NAME_PREFIX)

# ``substr``, not ``LIKE``: every underscore in the prefix is a
# single-character wildcard to LIKE, so the pattern would also match — and this
# store would then DROP — a trigger some other component named
# ``proxy-cache-privacy-track-…`` in the same database.
#
# ``COLLATE NOCASE`` because SQLite identifiers are case-insensitive, so a
# trigger stored as ``PROXY_CACHE_PRIVACY_TRACK_DELETE`` IS one of this store's
# and fires as one. A case-sensitive test would leave it out of both the
# currentness answer and the drop set: the store would call itself current
# while running that trigger, and a ``CREATE`` of the canonical name would
# collide with it.
_SELECT_PRIVACY_TRIGGERS = """
SELECT name, sql FROM sqlite_master
WHERE type = 'trigger' AND substr(name, 1, ?) = ? COLLATE NOCASE;
"""

_SELECT_UNVERIFIED_PRIVACY_ROWS = """
SELECT p.cache_key, p.result, p.envelope_json
FROM proxy_cache AS p
WHERE p.cache_key IN (
    SELECT cache_key FROM proxy_cache_privacy_unverified
);
"""

_PRIVACY_POLICY_FINGERPRINT_KEY = "privacy_policy_fingerprint"

# Written into the stamp slot for the duration of an unlocked sweep. It is not
# a hex digest, so it matches NO process's policy fingerprint — which is the
# point: while it is published, neither a current nor an older-policy writer's
# ``set()`` dequeues its own key, so every write racing the sweep stays queued
# for the reservation-held pass to re-decide.
_PRIVACY_REPAIR_TOKEN_PREFIX = "repair-in-progress:"

# How many times the unlocked shape may be superseded by a competing
# initializer before the last attempt finishes under the reservation. An
# initializer must not return until the policy it enforces is the published
# one, so losing the race is retried rather than accepted; the final attempt
# cannot be superseded, which is what makes the loop terminate.
_PRIVACY_REPAIR_ATTEMPTS = 3

# Version of the fingerprint encoding and any scanner semantics not expressed
# by ``DEFAULT_PATTERNS`` itself. Pattern additions/removals change the digest
# automatically; bump this only when the same pattern tuple would be evaluated
# under a meaningfully different storage policy. Epoch 2 establishes the
# trigger-backed unverified-key baseline, invalidating stamps created before
# legacy writes could be tracked.
_PRIVACY_POLICY_EPOCH = 2


def _privacy_policy_fingerprint() -> str:
    """Stable digest of the exact default pattern policy used by this store."""
    return framed_digest((str(_PRIVACY_POLICY_EPOCH), *privacy.DEFAULT_PATTERNS))


def _normalize_trigger_sql(sql: str) -> str:
    """Whitespace-insensitive form of a CREATE TRIGGER statement.

    ``sqlite_master.sql`` holds the statement as it was submitted, minus its
    trailing semicolon, so a comparison has to ignore layout and that
    semicolon — and nothing else.
    """
    return " ".join(sql.split()).rstrip(";")


def _installed_triggers_are_current(db: sqlite3.Connection) -> bool:
    """True when the database's queue triggers are the ones this build ships.

    Read BEFORE the triggers are replaced. Recreating them is not enough on
    its own: whichever triggers were installed before tracked some other set
    of writes, and a row they failed to queue stays invisible to the fast path
    while the policy stamp still vouches for it. Comparing against
    ``sqlite_master`` — rather than a version this build recorded — asks what
    the database actually ran, so a trigger replaced out of band counts too.

    This is what keeps trigger changes safe in BOTH directions. An opener that
    finds text other than its own cannot trust the queue at all, so it sweeps
    the whole table under the reservation before publishing — which also covers
    the writes a foreign trigger failed to queue while it was installed.
    """
    installed = {
        _normalize_trigger_sql(sql)
        for _name, sql in db.execute(_SELECT_PRIVACY_TRIGGERS, _PRIVACY_TRIGGER_SELECTOR)
        if sql is not None
    }
    expected = {_normalize_trigger_sql(sql) for sql in _CREATE_PRIVACY_UNVERIFIED_TRIGGERS}
    return installed == expected


def _row_is_sensitive(result: str, envelope: str | None) -> bool:
    """True when a stored row's body or envelope matches the current policy."""
    return privacy.contains_sensitive_content(result) or (
        envelope is not None and privacy.contains_sensitive_content(envelope)
    )


def _ensure_current_triggers(db: sqlite3.Connection) -> bool:
    """Install this build's queue triggers, reporting whether they were already
    the ones running. Caller holds the write reservation.

    This is the ONLY place the triggers are written, and it is called from
    inside the reservation that also decides and publishes the stamp — so a
    build cannot swap the triggers between the check and the publication.
    Checking earlier, or writing them earlier, destroys the very evidence the
    check needs: replacing the text unconditionally at open makes every later
    comparison say "current" no matter what the database was actually running.
    """
    if _installed_triggers_are_current(db):
        return True
    for name, _sql in db.execute(_SELECT_PRIVACY_TRIGGERS, _PRIVACY_TRIGGER_SELECTOR).fetchall():
        # Identifier, so it cannot be bound as a parameter. Every name here was
        # matched against this store's own prefix; quoting it is belt and
        # braces for a name some other build chose.
        quoted = name.replace('"', '""')
        db.execute(f'DROP TRIGGER IF EXISTS "{quoted}"')
    for trigger_sql in _CREATE_PRIVACY_UNVERIFIED_TRIGGERS:
        db.execute(trigger_sql)
    return False


def _publish_privacy_stamp(db: sqlite3.Connection, value: str) -> None:
    """Write the policy stamp slot. Caller holds the write reservation."""
    db.execute(
        "INSERT INTO proxy_cache_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_PRIVACY_POLICY_FINGERPRINT_KEY, value),
    )


def _sweep_stale_keys(db: sqlite3.Connection) -> set[str]:
    """Keys of every stored row the current policy considers sensitive."""
    return {
        key
        for key, result, envelope in db.execute(
            "SELECT cache_key, result, envelope_json FROM proxy_cache"
        )
        if _row_is_sensitive(result, envelope)
    }


def _purge_queued_rows(db: sqlite3.Connection, swept_stale_keys: set[str]) -> None:
    """Re-decide every queued row, then empty the queue. Caller holds the write
    reservation, so no writer can queue a new key until it commits."""
    # The IN subquery makes SQLite drive the lookup from the small queue and
    # probe proxy_cache by its primary-key index. A plain JOIN can be
    # reordered into a full proxy_cache table scan.
    queued_rows = db.execute(_SELECT_UNVERIFIED_PRIVACY_ROWS).fetchall()
    stale_keys = {
        key for key, result, envelope in queued_rows if _row_is_sensitive(result, envelope)
    }
    # A key still queued was written or rewritten after the unlocked sweep read
    # it, so the verdict just reached on its current body supersedes the
    # sweep's — which may have judged a body no longer stored under this key.
    stale_keys |= swept_stale_keys - {key for key, _, _ in queued_rows}
    if stale_keys:
        db.executemany(
            "DELETE FROM proxy_cache WHERE cache_key = ?",
            [(key,) for key in stale_keys],
        )
    db.execute("DELETE FROM proxy_cache_privacy_unverified")


@dataclass
class CacheEntry:
    result: str
    created_at: float
    ttl_seconds: float | None

    def is_expired(self) -> bool:
        if self.ttl_seconds is None:
            return False
        return time.time() >= self.created_at + self.ttl_seconds


def _purge_dead_rows(db: sqlite3.Connection) -> None:
    """Delete rows no lookup can ever serve. Caller holds the write
    reservation, and calls this BEFORE anything reads a body.

    Ordering is the point: these two predicates are pure SQL over indexed or
    literal terms, while the scans that follow run every privacy pattern over
    every body they are given. Sweeping first would regex the very rows this
    deletes — the cost #872 exists to remove — and would hold the reservation
    for longer while doing it.
    """
    # Startup purge of expired rows. It runs after ``_ensure_current_triggers``
    # and therefore under triggers this build wrote: a DELETE executed against
    # whatever the database was carrying would run a stale trigger this build
    # cannot vouch for, and one that raises would abort initialization before
    # the repair that would have removed it. A failure here propagates out of
    # ``initialize`` so ``self._db`` stays ``None``.
    db.execute(
        "DELETE FROM proxy_cache WHERE ttl_seconds IS NOT NULL AND created_at + ttl_seconds <= ?",
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


def _repair_privacy_policy(db: sqlite3.Connection) -> None:
    """Purge rows the current privacy policy rejects, then publish its stamp.

    Rows can predate the privacy gate in ``set()`` (#453), predate the current
    pattern policy, or arrive later from a writer that never passed through the
    gate. The regex sweep is stamp-gated because feeding every cached body
    through every pattern on every startup makes initialization proportional to
    cache volume (#872): a matching stamp scans only the keys the triggers put
    in the small unverified queue, while a missing or stale one runs the full
    repair once.

    The fast path does everything under one reservation. The full sweep cannot:
    it is the one piece of work proportional to cache volume, and holding the
    reservation across it makes a peer's ``initialize()`` fail on
    ``busy_timeout`` — which ``server.py`` answers by running with the cache
    disabled for that process's whole lifetime — and a live peer's ``set()``
    fail for the same window. So the sweep reads a WAL snapshot with no lock
    held, and a repair token published first is what keeps its verdicts honest:
    while the token stands it equals no build's fingerprint, so no writer's
    ``set()`` dequeues its own key and every write racing the sweep is still
    queued when the reservation is finally taken.

    Returning without the current policy published would leave rows only this
    build calls sensitive persisted with nothing scheduled to remove them, so a
    superseded attempt retries instead of accepting the other initializer's
    outcome. Taking over a token — a peer's, or one abandoned by a crash — is
    always safe: this attempt sweeps the whole table itself, and the peer will
    find its own token gone and retry in turn.

    Deletions, dequeue and stamp always commit together, so any failure rolls
    the repair back for the next opener. The read-side guard in ``get()``
    remains an immediate backstop for post-startup writes.
    """
    policy_fingerprint = _privacy_policy_fingerprint()
    for attempt in range(_PRIVACY_REPAIR_ATTEMPTS):
        db.execute("BEGIN IMMEDIATE")
        triggers_were_current = _ensure_current_triggers(db)
        _purge_dead_rows(db)
        stamp_row = db.execute(
            "SELECT value FROM proxy_cache_meta WHERE key = ?",
            (_PRIVACY_POLICY_FINGERPRINT_KEY,),
        ).fetchone()
        if triggers_were_current and stamp_row is not None and stamp_row[0] == policy_fingerprint:
            _purge_queued_rows(db, set())
            db.commit()
            return
        if not triggers_were_current or attempt == _PRIVACY_REPAIR_ATTEMPTS - 1:
            # Two cases sweep under the reservation already held. Triggers that
            # were not this build's mean the queue was maintained by other
            # rules, so nothing in it — or missing from it — can be trusted.
            # The last attempt means the unlocked shape keeps losing the race.
            # Holding the reservation blocks writers for the duration, which is
            # exactly what the unlocked shape exists to avoid, but it cannot be
            # superseded and it re-verifies the whole table under this build's
            # own triggers.
            _purge_queued_rows(db, _sweep_stale_keys(db))
            _publish_privacy_stamp(db, policy_fingerprint)
            db.commit()
            return
        repair_token = f"{_PRIVACY_REPAIR_TOKEN_PREFIX}{uuid4().hex}"
        _publish_privacy_stamp(db, repair_token)
        db.commit()
        swept_stale_keys = _sweep_stale_keys(db)
        db.execute("BEGIN IMMEDIATE")
        stamp_row = db.execute(
            "SELECT value FROM proxy_cache_meta WHERE key = ?",
            (_PRIVACY_POLICY_FINGERPRINT_KEY,),
        ).fetchone()
        if (
            stamp_row is None
            or stamp_row[0] != repair_token
            or not _installed_triggers_are_current(db)
        ):
            # Another initializer took the repair over, possibly running a
            # different policy; or a build with different triggers changed what
            # the queue tracks while the sweep ran, so a write the sweep missed
            # may never have been queued. Neither these verdicts nor a dequeue
            # of the rows another process is relying on may be applied, so
            # leave the queue exactly as found and start over — the next
            # attempt reinstalls the triggers and sweeps under the reservation.
            db.rollback()
            logger.debug("Privacy repair superseded by a concurrent initializer; retrying")
            continue
        _purge_queued_rows(db, swept_stale_keys)
        _publish_privacy_stamp(db, policy_fingerprint)
        db.commit()
        return


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
    # - ``framed_digest`` length-prefixes each component instead of joining
    #   them, so no boundary can shift — nothing on the path rejects a NUL in
    #   an upstream server or tool name. It also owns the ``surrogatepass``
    #   encode; see its docstring for why neither is optional.
    # - ``ensure_ascii=False``: the default ASCII escaping renders an astral
    #   scalar as the same ``\uXXXX\uXXXX`` text as the two lone surrogate
    #   code units spelled separately, aliasing the two. Unescaped, the
    #   scalar and the lone pair encode to different bytes below.
    # - The identifiers go in RAW, not through ``escape_lone_surrogates``.
    #   That helper is documented as non-injective — it maps the code unit
    #   U+D800 and the six literal characters ``\ud800`` onto the same text —
    #   so deriving the key through it let one identifier's row answer for a
    #   different one.
    #
    # All three derivations — ``get``, ``set`` and ``invalidate`` — come
    # through here, so they cannot disagree.
    return framed_digest(
        (
            str(_KEY_SCHEMA_VERSION),
            server,
            tool,
            json.dumps(args, sort_keys=True, ensure_ascii=False),
            config_fingerprint,
            json.dumps(context_query, ensure_ascii=False),
        )
    )


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
            db.execute("BEGIN IMMEDIATE")
            (schema_version,) = db.execute("PRAGMA user_version").fetchone()
            if schema_version < _KEY_SCHEMA_VERSION:
                db.execute("DROP TABLE IF EXISTS proxy_cache")
                db.execute(f"PRAGMA user_version = {_KEY_SCHEMA_VERSION}")
            db.execute(_CREATE_TABLE)
            db.execute(_CREATE_INDEX)
            db.execute(_CREATE_META_TABLE)
            db.execute(_CREATE_PRIVACY_UNVERIFIED_TABLE)
            # The triggers themselves are installed by the repair below, under
            # the reservation that publishes the stamp — see
            # ``_ensure_current_triggers``. Writing them here would erase the
            # evidence of what the database was actually running.
            db.commit()
            _repair_privacy_policy(db)
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
        if privacy.contains_sensitive_content(entry.result) or (
            envelope_json is not None and privacy.contains_sensitive_content(envelope_json)
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
        if privacy.contains_sensitive_content(result) or (
            envelope_json is not None and privacy.contains_sensitive_content(envelope_json)
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
            # The INSERT/UPDATE trigger conservatively queued this key before
            # the write became visible. ``set()`` checked the exact body and
            # envelope above, so dequeue this key in the same transaction only
            # when the database's published stamp matches this process's
            # policy. During a rolling policy upgrade, an older process must
            # leave its writes queued for the newer process to verify.
            self._db.execute(
                "DELETE FROM proxy_cache_privacy_unverified WHERE cache_key = ? "
                "AND EXISTS ("
                "SELECT 1 FROM proxy_cache_meta WHERE key = ? AND value = ?"
                ")",
                (
                    key,
                    _PRIVACY_POLICY_FINGERPRINT_KEY,
                    _privacy_policy_fingerprint(),
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
