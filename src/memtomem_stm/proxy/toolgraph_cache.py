"""SQLite-backed disk cache for the tool-graph startup consult (#494).

The tool-graph eligibility provider consults the graph once per
``ProxyManager.start()`` (see ``manager._consult_toolgraph``). The verdict is
stable for a given ``graph_generation``, so a restart with an unchanged
generation can reuse it instead of re-running the Neo4j-heavy per-candidate
evaluation. This store persists the **raw ref-level facts** of a successful,
agent-found consult, keyed by ``(provider_fingerprint, agent_id, query_profile,
candidate_hash, graph_generation)``.

Correctness model (Model A, strictly-fresh): the cache is only ever read after a
*live* cheap generation probe, so it can never mask a degraded/unreachable graph.
A degraded / agent-not-found verdict is never written.

Stored data is STM/graph-derived only — server-qualified tool refs
(``"server::tool"``), reason codes, and the sanitized per-candidate fact rows
(booleans, closed-vocabulary verdicts, counts, risk floats — see
``tool_eligibility.sanitize_graph_facts_row``). No upstream payloads or
secrets, so (unlike ``ProxyCache``) there is no privacy scan.

Like the sibling stores, every method does synchronous sqlite I/O on the asyncio
event loop; accepted for the local single-MCP-client deployment, and lock-ready
(``self._lock``) for a future off-loop move.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memtomem_stm.proxy.tool_eligibility import sanitize_graph_facts_row
from memtomem_stm.utils.json_out import has_lone_surrogate
from memtomem_stm.utils.digest import framed_digest
from memtomem_stm.utils.sqlite_private import ensure_private_db_files
from memtomem_stm.utils.sqlite_tuning import tune_connection

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from memtomem_stm.proxy.config import ToolgraphConfig

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS toolgraph_consult (
    scope_key            TEXT    PRIMARY KEY,
    provider_fingerprint TEXT    NOT NULL,
    agent_id             TEXT    NOT NULL,
    query_profile        TEXT    NOT NULL,
    candidate_hash       TEXT    NOT NULL,
    graph_generation     INTEGER NOT NULL,
    had_risk_scores      INTEGER NOT NULL DEFAULT 0,
    verdict_json         TEXT    NOT NULL,
    created_at           REAL    NOT NULL
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_toolgraph_consult_scope
ON toolgraph_consult (provider_fingerprint, agent_id, query_profile, candidate_hash);
"""

# Schema bookkeeping in a table of our own rather than in ``PRAGMA
# user_version``. That pragma is a property of the DATABASE, not of a table,
# and ``consult_cache_path`` takes an arbitrary path — point it at the response
# cache's file (which stamps its own version) and this cache reads a number it
# never wrote, so the migration below silently does not run. Verified: with the
# response cache's stamp of 5 already present, a pre-#794 row survived a purge
# that should have dropped it. A private table cannot be read or written by
# another component sharing the file.
_CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS toolgraph_meta (
    key   TEXT    PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

_SCOPE_KEY_VERSION_KEY = "scope_key_version"

IDENTITY_POLICY = 2
"""Version of the identity-validation policy the stored facts were written under.

A hit reconstructs only ``rejects`` / ``tool_not_found_refs`` / ``graph_facts``,
so the response fields ``_validate_verdict_identifiers`` checks — ``agent``,
``profile``, ``eligible``, ``tool_key``, auxiliary ``candidates`` — are not in
the row and cannot be revalidated after an upgrade. A pre-#783 row could
therefore be minted by a verdict that today's policy refuses, and then serve a
warm start that skips the full consult entirely: cold would ``fail_start``
while warm came up clean, the exact divergence the pre-cache validation exists
to prevent. Stamping the policy and rejecting rows that lack it forces one full
consult after upgrade, which re-derives the verdict under current rules.

Bump this whenever the set of refused identifier fields changes, for the same
reason: a row written under a laxer policy is not evidence for a stricter one.

v2 (#469): rows carry ``graph_facts`` instead of ``risk_scores``. A v1 row
holds no facts and cannot have them back-derived (the sparse score map dropped
every clean and unresolved row), so serving one would look like a successful
enrichment that recorded nothing. Rejecting it costs one full consult after
upgrade, exactly like every other policy bump.
"""


# Bump when the scope-key derivation changes shape, so ``initialize()`` can
# purge rows written under an older one. Such rows are opaque hashes no current
# lookup can produce: unreachable, therefore never read, therefore never
# dropped by the ``IDENTITY_POLICY`` check in ``_row_shape_ok`` (which only
# fires on a row that IS read) — they would just occupy scope slots against
# ``max_scopes`` until ``_trim`` aged them out. Stored in ``toolgraph_meta``,
# NOT in ``PRAGMA user_version`` — see ``_CREATE_META_TABLE``.
# v1: framed scope-key derivation (#794).
_SCOPE_KEY_SCHEMA_VERSION = 1


def _scope_key(
    provider_fp: str, agent_id: str, profile: str, candidate_hash: str, generation: int
) -> str:
    # Framed, not joined: ``agent_id`` and ``profile`` are free-form strings
    # sitting next to each other, and nothing on the path rejects a NUL in
    # either (``validate_toolgraph_identifier`` refuses lone surrogates only),
    # so ``agent_id="a\0b", profile="c"`` and ``agent_id="a", profile="b\0c"``
    # used to produce the same key — and a collision here serves one scope's
    # cached consult facts for a different scope, i.e. a policy decision made
    # from the wrong row. Same defect the response cache had (#784, #794).
    #
    # ``generation`` is stringified rather than framed as an int because
    # ``framed_digest`` takes strings; ``str(int)`` is injective, so this adds
    # no aliasing of its own.
    return framed_digest(
        (
            str(_SCOPE_KEY_SCHEMA_VERSION),
            provider_fp,
            agent_id,
            profile,
            candidate_hash,
            str(generation),
        )
    )


class GraphConsultCache:
    """Cross-restart cache of a successful tool-graph consult's raw facts."""

    def __init__(self, db_path: Path, max_scopes: int = 64) -> None:
        self._db_path = db_path
        self._max_scopes = max_scopes
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    @staticmethod
    def candidate_hash(refs: Iterable[str]) -> str:
        """Order-independent hash of the candidate ref set."""
        raw = json.dumps(sorted(refs), separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def provider_fingerprint(config: ToolgraphConfig) -> str:
        """Backend identity: ``command`` + ``args`` + sorted env *keys*.

        Keys only — never env *values* (``NEO4J_PASSWORD`` etc.) — so the
        fingerprint stays non-secret. Distinguishes a different graph
        binary/args/backend-shape on the shared user-wide DB so two backends
        cannot collide on the same ``(agent, profile, refs, generation)``. Two
        backends with identical command/args/env-keys but different env *values*
        (e.g. only ``NEO4J_URI`` differs) still collide — such setups should use
        a distinct ``consult_cache_path``.
        """
        env_keys = sorted((config.env or {}).keys())
        raw = json.dumps([config.command, list(config.args), env_keys], separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=5.0)
        try:
            ensure_private_db_files(self._db_path)
            tune_connection(db)
            db.execute(_CREATE_TABLE)
            db.execute(_CREATE_INDEX)
            db.execute(_CREATE_META_TABLE)
            # One-time purge on a scope-key shape change. The tables are
            # created first because the purge reads and writes both; on a fresh
            # database the version reads as 0 and the DELETE is a no-op. DELETE
            # rather than DROP: the row shape is unchanged, only the derivation
            # moved, so there is nothing to recreate.
            row = db.execute(
                "SELECT value FROM toolgraph_meta WHERE key = ?", (_SCOPE_KEY_VERSION_KEY,)
            ).fetchone()
            scope_key_version = row[0] if row is not None else 0
            if scope_key_version < _SCOPE_KEY_SCHEMA_VERSION:
                db.execute("DELETE FROM toolgraph_consult")
                db.execute(
                    "INSERT INTO toolgraph_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (_SCOPE_KEY_VERSION_KEY, _SCOPE_KEY_SCHEMA_VERSION),
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
        provider_fp: str,
        agent_id: str,
        profile: str,
        candidate_hash: str,
        generation: int,
    ) -> dict[str, Any] | None:
        """Return the cached raw facts for an exact scope+generation, or ``None``.

        The returned dict carries ``rejects`` / ``tool_not_found_refs`` /
        ``graph_facts`` (the raw graph facts) plus ``had_risk_scores`` (whether
        the ``rank_features`` enrichment succeeded when the row was written).

        Fact rows are re-sanitized on read, so a hit and a live consult hand the
        caller the same key set and the same closed vocabularies even when the
        file was written by a different version of this package — a stored row
        is data from disk, not a value this process produced.
        """
        if self._db is None:
            return None
        if has_lone_surrogate(agent_id) or has_lone_surrogate(profile):
            return None
        key = _scope_key(provider_fp, agent_id, profile, candidate_hash, generation)
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT verdict_json, had_risk_scores FROM toolgraph_consult WHERE scope_key = ?",
                    (key,),
                ).fetchone()
        except sqlite3.Error:
            # Best-effort: a runtime sqlite fault on the lookup (database locked,
            # disk I/O error, page-level corruption) must degrade to a plain MISS,
            # never raise. get()/put() run inside ``_consult_toolgraph`` — the last
            # statement of ``ProxyManager.start()`` — whose callers do NOT catch
            # sqlite3.Error, so an escaping fault would crash proxy startup and take
            # every proxied tool down for a non-fatal cache problem. A miss instead
            # re-runs the live full consult (strictly-fresh preserved). Mirrors the
            # _delete_scope guard below and the initialize()-time best-effort contract.
            logger.warning(
                "Tool-graph consult cache read failed (scope %s) — treating as a miss",
                key[:12],
                exc_info=True,
            )
            return None
        if row is None:
            return None
        try:
            verdict = json.loads(row[0])
        except (ValueError, TypeError):
            verdict = None
        # Best-effort: a row that is not valid JSON, not a dict, or is missing the
        # expected raw-fact shape (e.g. an old-schema or externally-corrupted row)
        # is treated as a MISS and dropped — the caller subscripts these keys, so a
        # malformed row must never raise during startup. The next consult re-mints
        # a fresh row.
        if not self._row_shape_ok(verdict):
            logger.warning(
                "Tool-graph consult cache row (scope %s) is malformed, or was written "
                "before the current identity-validation policy — treating as a miss "
                "and dropping it",
                key[:12],
            )
            self._delete_scope(key)
            return None
        verdict["graph_facts"] = {
            ref: sanitize_graph_facts_row(facts) for ref, facts in verdict["graph_facts"].items()
        }
        verdict["had_risk_scores"] = bool(row[1])
        return verdict

    @staticmethod
    def _row_shape_ok(verdict: object) -> bool:
        """True only if ``verdict`` matches exactly what :meth:`put` writes.

        Validates **leaf** value types, not just the containers — the caller's
        on-hit reconstruction does ``dict(rejects)`` / ``frozenset(refs)``
        outside the ``on_*``-knob ``try``, so a row that passed a
        containers-only check would crash startup. Matching ``put``'s shape
        (``rejects: {str: str}``, ``tool_not_found_refs: [str]``,
        ``graph_facts: {str: object}``) guarantees the reconstruction can never
        raise on a hit. The fact rows themselves need no leaf check here:
        ``get`` runs each one through ``sanitize_graph_facts_row``, which is
        total over any mapping, so a corrupted leaf becomes ``None`` rather
        than an exception.
        """
        if not isinstance(verdict, dict):
            return False
        # A row minted under an older (or absent) identity policy is not
        # evidence under this one — see ``IDENTITY_POLICY``. Checked here rather
        # than only in ``get`` so ``put``'s pre-write self-check also proves the
        # stamp was written.
        if verdict.get("identity_policy") != IDENTITY_POLICY:
            return False
        rejects = verdict.get("rejects")
        refs = verdict.get("tool_not_found_refs")
        graph_facts = verdict.get("graph_facts")
        if not (
            isinstance(rejects, dict) and isinstance(refs, list) and isinstance(graph_facts, dict)
        ):
            return False
        if not all(
            isinstance(k, str)
            and isinstance(v, str)
            and not has_lone_surrogate(k)
            and not has_lone_surrogate(v)
            for k, v in rejects.items()
        ):
            return False
        if not all(isinstance(ref, str) and not has_lone_surrogate(ref) for ref in refs):
            return False
        # Fact rows are keyed by candidate ref — an identifier, held to the same
        # encodability rule as every other one here. The row itself only has to
        # be a mapping; ``sanitize_graph_facts_row`` handles its contents.
        return all(
            isinstance(k, str) and not has_lone_surrogate(k) and isinstance(v, dict)
            for k, v in graph_facts.items()
        )

    def _delete_scope(self, scope_key: str) -> None:
        """Best-effort drop of a single (corrupt) row by ``scope_key``."""
        if self._db is None:
            return
        try:
            with self._lock:
                self._db.execute("DELETE FROM toolgraph_consult WHERE scope_key = ?", (scope_key,))
                self._db.commit()
        except sqlite3.Error:
            logger.debug("Failed to drop malformed consult cache row", exc_info=True)

    def put(
        self,
        provider_fp: str,
        agent_id: str,
        profile: str,
        candidate_hash: str,
        generation: int,
        *,
        rejects: Mapping[str, str],
        tool_not_found_refs: Iterable[str],
        graph_facts: Mapping[str, Mapping[str, Any]],
        had_risk_scores: bool,
    ) -> None:
        """Persist the raw facts of a successful consult (scope-replacing)."""
        if self._db is None:
            return
        if has_lone_surrogate(agent_id) or has_lone_surrogate(profile):
            logger.warning("Tool-graph consult cache write refused unencodable scope identifier")
            return
        raw_facts: dict[str, Any] = {
            "identity_policy": IDENTITY_POLICY,
            "rejects": dict(rejects),
            "tool_not_found_refs": list(tool_not_found_refs),
            # Sanitized on write as well as on read: what lands on disk is then
            # exactly what a live consult produces, so a hit can never widen the
            # fact vocabulary a fresh consult would have narrowed.
            "graph_facts": {ref: sanitize_graph_facts_row(row) for ref, row in graph_facts.items()},
        }
        if not self._row_shape_ok(raw_facts):
            logger.warning(
                "Tool-graph consult cache write refused malformed or unencodable "
                "identifier facts — consult not cached"
            )
            return
        key = _scope_key(provider_fp, agent_id, profile, candidate_hash, generation)
        verdict_json = json.dumps(
            {
                "graph_generation": generation,
                **raw_facts,
            },
            separators=(",", ":"),
        )
        now = time.time()
        try:
            with self._lock:
                # Scope-replace: a newer generation for the same (provider, agent,
                # profile, candidate-set) supersedes prior generations — one row per
                # scope keeps growth bounded.
                self._db.execute(
                    "DELETE FROM toolgraph_consult WHERE provider_fingerprint = ? AND agent_id = ? "
                    "AND query_profile = ? AND candidate_hash = ? AND graph_generation != ?",
                    (provider_fp, agent_id, profile, candidate_hash, generation),
                )
                self._db.execute(
                    """
                    INSERT INTO toolgraph_consult
                        (scope_key, provider_fingerprint, agent_id, query_profile,
                         candidate_hash, graph_generation, had_risk_scores, verdict_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(scope_key) DO UPDATE SET
                        had_risk_scores = excluded.had_risk_scores,
                        verdict_json    = excluded.verdict_json,
                        created_at      = excluded.created_at
                    """,
                    (
                        key,
                        provider_fp,
                        agent_id,
                        profile,
                        candidate_hash,
                        generation,
                        1 if had_risk_scores else 0,
                        verdict_json,
                        now,
                    ),
                )
                self._db.commit()
                self._trim()
        except sqlite3.Error:
            # Best-effort write (covers the DELETE/INSERT/commit and the _trim sweep
            # it calls under the same lock): a runtime sqlite fault must no-op, never
            # raise. See the get() guard above for why an escaping fault is fatal to
            # startup. A skipped write just means the next consult re-mints the row.
            # A fault mid-transaction (e.g. the DELETE commits implicitly-open work,
            # then INSERT/commit/_trim raises) can leave a transaction holding the
            # write lock; roll it back so the connection stays cleanly reusable.
            try:
                self._db.rollback()
            except sqlite3.Error:
                logger.debug("Tool-graph consult cache rollback failed", exc_info=True)
            logger.warning(
                "Tool-graph consult cache write failed (scope %s) — consult not cached",
                key[:12],
                exc_info=True,
            )
            return

    def _trim(self) -> None:
        if self._db is None:
            return
        count = self._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0]
        if count > self._max_scopes:
            excess = count - self._max_scopes
            self._db.execute(
                "DELETE FROM toolgraph_consult WHERE scope_key IN "
                "(SELECT scope_key FROM toolgraph_consult ORDER BY created_at ASC LIMIT ?)",
                (excess,),
            )
            self._db.commit()
