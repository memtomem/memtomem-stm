"""Unit tests for ``GraphConsultCache`` (#494) — the tool-graph consult disk cache."""

from __future__ import annotations

import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from memtomem_stm.proxy.tool_eligibility import (
    GRAPH_VALUE_UNRECOGNIZED,
    sanitize_graph_facts_row,
)
from memtomem_stm.proxy.toolgraph_cache import IDENTITY_POLICY, GraphConsultCache

_PROV = "prov-fp"
_AGENT = "stm-proxy"
_PROFILE = "strict"


@pytest.fixture
def cache(tmp_path):
    c = GraphConsultCache(tmp_path / "tg.db")
    c.initialize()
    yield c
    c.close()


def _facts(risk):
    """Fact rows for ``{ref: risk_score}``, the shape a consult hands ``put``."""
    return {ref: sanitize_graph_facts_row({"risk_score": score}) for ref, score in risk.items()}


def _put(cache, *, generation=11, cand="hashA", rejects=None, tnf=None, risk=None, had_risk=True):
    cache.put(
        _PROV,
        _AGENT,
        _PROFILE,
        cand,
        generation,
        rejects=rejects or {},
        tool_not_found_refs=tnf or [],
        graph_facts=_facts(risk or {}),
        risk_scores={ref: score for ref, score in (risk or {}).items() if score > 0},
        had_risk_scores=had_risk,
    )


class TestScopeKeyIsFramed:
    """A scope-key collision serves one scope's consult facts for another (#794).

    ``agent_id`` and ``profile`` are free-form and adjacent, and
    ``validate_toolgraph_identifier`` refuses lone surrogates but not NUL, so
    before framing a NUL in either shifted the component boundary. The result
    is a policy decision made from the wrong row, which is why this is pinned
    live (both rows readable) and not only at the digest.
    """

    def test_nul_shifted_scope_identifiers_do_not_collide(self):
        from memtomem_stm.proxy.toolgraph_cache import _scope_key

        assert _scope_key(_PROV, "a\x00b", "c", "h", 1) != _scope_key(_PROV, "a", "b\x00c", "h", 1)

    def test_nul_cannot_shift_across_distant_boundaries(self):
        """Not just the adjacent pair — every boundary must hold.

        The shift here crosses TWO boundaries: content moves from
        ``provider_fp`` all the way into ``candidate_hash``, with ``agent_id``
        and ``profile`` in between. Both tuples joined to the identical
        ``p\\x00a\\x00b\\x00c\\x00h\\x001`` under the old derivation, so a
        framing that only separated the adjacent free-form pair would still
        let these two share a row.

        ``provider_fp`` and ``candidate_hash`` are hex digests in production,
        which is why this is the weaker of the two cases — but the derivation
        takes plain strings and must not depend on its callers for injectivity.
        """
        from memtomem_stm.proxy.toolgraph_cache import _scope_key

        assert _scope_key("p\x00a", "b", "c", "h", 1) != _scope_key("p", "a", "b", "c\x00h", 1)

    def test_generation_stays_distinguishing(self):
        from memtomem_stm.proxy.toolgraph_cache import _scope_key

        assert _scope_key(_PROV, "a", "b", "h", 1) != _scope_key(_PROV, "a", "b", "h", 2)

    def test_shifted_scopes_are_separate_rows(self, cache):
        """The live shape: before framing the second write took the first's row."""
        cache.put(
            _PROV,
            "a\x00b",
            "c",
            "hashA",
            1,
            rejects={"s::first": "TOOLGRAPH_NOT_GRANTED"},
            tool_not_found_refs=[],
            graph_facts={},
            risk_scores={},
            had_risk_scores=True,
        )
        cache.put(
            _PROV,
            "a",
            "b\x00c",
            "hashA",
            1,
            rejects={"s::second": "TOOLGRAPH_NOT_GRANTED"},
            tool_not_found_refs=[],
            graph_facts={},
            risk_scores={},
            had_risk_scores=True,
        )

        first = cache.get(_PROV, "a\x00b", "c", "hashA", 1)
        second = cache.get(_PROV, "a", "b\x00c", "hashA", 1)
        assert first is not None and second is not None
        assert first["rejects"] == {"s::first": "TOOLGRAPH_NOT_GRANTED"}
        assert second["rejects"] == {"s::second": "TOOLGRAPH_NOT_GRANTED"}


class TestScopeKeySchemaPurge:
    """Rows keyed under the pre-framing derivation are unreachable, so nothing
    else ever drops them.

    The ``IDENTITY_POLICY`` stamp in ``_row_shape_ok`` cannot: it only fires on
    a row that is actually read, and an orphaned row is by definition never
    looked up. Without this purge they sit against ``max_scopes`` until
    ``_trim`` ages them out.
    """

    @staticmethod
    def _seed_pre_794(db_path, *, user_version: int = 0):
        """A database as #794 found it: rows, and no ``toolgraph_meta`` at all."""
        c = GraphConsultCache(db_path)
        c.initialize()
        _put(c)
        c._db.execute("DROP TABLE toolgraph_meta")
        c._db.execute(f"PRAGMA user_version = {user_version}")
        c._db.commit()
        c.close()

    def test_pre_versioning_rows_are_purged_once(self, tmp_path):
        db_path = tmp_path / "tg.db"
        self._seed_pre_794(db_path)

        reopened = GraphConsultCache(db_path)
        reopened.initialize()
        try:
            count = reopened._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0]
            assert count == 0
            (version,) = reopened._db.execute(
                "SELECT value FROM toolgraph_meta WHERE key = 'scope_key_version'"
            ).fetchone()
            assert version == 1
        finally:
            reopened.close()

    def test_purge_still_runs_when_the_file_carries_another_component_version(self, tmp_path):
        """``PRAGMA user_version`` belongs to the DATABASE, not to this table.

        ``consult_cache_path`` takes an arbitrary path, so the file can be one
        another component already stamped — the response cache stamps 5. Keying
        the migration on that pragma made ``5 < 1`` false and skipped the purge,
        leaving rows that no lookup can reach. Pinned with a bare number rather
        than by opening a real ``ProxyCache`` so this keeps testing the sharing
        hazard even if that cache's own version changes.
        """
        db_path = tmp_path / "tg.db"
        self._seed_pre_794(db_path, user_version=5)

        reopened = GraphConsultCache(db_path)
        reopened.initialize()
        try:
            count = reopened._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0]
            assert count == 0
            # The foreign stamp is left exactly as found — not ours to rewrite.
            (pragma,) = reopened._db.execute("PRAGMA user_version").fetchone()
            assert pragma == 5
        finally:
            reopened.close()

    def test_current_version_rows_survive_reopen(self, tmp_path):
        db_path = tmp_path / "tg.db"
        c = GraphConsultCache(db_path)
        c.initialize()
        _put(c, rejects={"s::a": "TOOLGRAPH_NOT_GRANTED"})
        c.close()

        reopened = GraphConsultCache(db_path)
        reopened.initialize()
        try:
            row = reopened.get(_PROV, _AGENT, _PROFILE, "hashA", 11)
            assert row is not None
            assert row["rejects"] == {"s::a": "TOOLGRAPH_NOT_GRANTED"}
        finally:
            reopened.close()


class TestRoundTrip:
    def test_put_get_round_trip(self, cache):
        _put(
            cache,
            rejects={"s::a": "TOOLGRAPH_NOT_GRANTED"},
            tnf=["s::b"],
            risk={"s::c": 0.5},
            had_risk=True,
        )
        row = cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11)
        assert row is not None
        assert row["rejects"] == {"s::a": "TOOLGRAPH_NOT_GRANTED"}
        assert row["tool_not_found_refs"] == ["s::b"]
        assert row["graph_facts"]["s::c"]["risk_score"] == 0.5
        assert row["had_risk_scores"] is True

    def test_a_nonzero_deny_path_count_survives_the_round_trip(self, cache):
        """Sanitizing happens on write and again on read, so the fact must
        survive being sanitized twice — otherwise a warm start reports facts
        the cold start that filled the row did not."""
        cache.put(
            _PROV,
            _AGENT,
            _PROFILE,
            "hashA",
            11,
            rejects={},
            tool_not_found_refs=[],
            graph_facts={"s::a": sanitize_graph_facts_row({"deny_paths": [["x"], ["y"]]})},
            risk_scores={},
            had_risk_scores=True,
        )
        row = cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11)
        assert row["graph_facts"]["s::a"]["deny_path_count"] == 2

    def test_the_penalty_map_is_stored_not_re_derived(self, cache):
        """The live parser keeps the last POSITIVE score for a repeated ref,
        which a deduplicated facts map cannot express — so the map travels with
        the facts rather than being recomputed on a hit."""
        cache.put(
            _PROV,
            _AGENT,
            _PROFILE,
            "hashA",
            11,
            rejects={},
            tool_not_found_refs=[],
            # The facts say 0.0 (the last row); the penalty says 0.4 (the last
            # positive one). A hit must reproduce both.
            graph_facts={"s::a": sanitize_graph_facts_row({"risk_score": 0.0})},
            risk_scores={"s::a": 0.4},
            had_risk_scores=True,
        )
        row = cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11)
        assert row["graph_facts"]["s::a"]["risk_score"] == 0.0
        assert row["risk_scores"] == {"s::a": 0.4}

    def test_had_risk_scores_false_round_trips(self, cache):
        _put(cache, had_risk=False)
        row = cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11)
        assert row is not None
        assert row["had_risk_scores"] is False


class TestMisses:
    def test_generation_mismatch_misses(self, cache):
        _put(cache, generation=11)
        assert cache.get(_PROV, _AGENT, _PROFILE, "hashA", 12) is None

    def test_candidate_hash_mismatch_misses(self, cache):
        _put(cache, cand="hashA")
        assert cache.get(_PROV, _AGENT, _PROFILE, "hashB", 11) is None

    def test_agent_mismatch_misses(self, cache):
        _put(cache)
        assert cache.get(_PROV, "other-agent", _PROFILE, "hashA", 11) is None

    def test_profile_mismatch_misses(self, cache):
        _put(cache)
        assert cache.get(_PROV, _AGENT, "lenient", "hashA", 11) is None

    def test_provider_fingerprint_mismatch_misses(self, cache):
        _put(cache)
        assert cache.get("other-prov", _AGENT, _PROFILE, "hashA", 11) is None

    def test_empty_store_misses(self, cache):
        assert cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11) is None


class TestScopeReplace:
    def test_new_generation_supersedes_old(self, cache):
        _put(cache, generation=11)
        _put(cache, generation=12)
        assert cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11) is None  # superseded
        assert cache.get(_PROV, _AGENT, _PROFILE, "hashA", 12) is not None
        # Exactly one row remains for the scope.
        n = cache._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0]
        assert n == 1

    def test_different_candidate_set_coexists(self, cache):
        _put(cache, cand="hashA", generation=11)
        _put(cache, cand="hashB", generation=11)
        assert cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11) is not None
        assert cache.get(_PROV, _AGENT, _PROFILE, "hashB", 11) is not None


class TestMaxScopesTrim:
    def test_default_max_scopes(self, tmp_path):
        c = GraphConsultCache(tmp_path / "tg.db")
        assert c._max_scopes == 64  # ctor default; preserves pre-config behavior

    def test_trim_caps_row_count_at_configured_max(self, tmp_path):
        # Each put with a distinct candidate set is a distinct scope row; once the
        # count exceeds max_scopes, _trim deletes the oldest down to the cap.
        c = GraphConsultCache(tmp_path / "tg.db", max_scopes=3)
        c.initialize()
        try:
            for i in range(6):
                _put(c, cand=f"hash{i}")
            count = c._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0]
            assert count == 3
        finally:
            c.close()


class TestUninitialized:
    def test_get_put_no_op_when_uninitialized(self, tmp_path):
        c = GraphConsultCache(tmp_path / "tg.db")  # no initialize()
        c.put(
            _PROV,
            _AGENT,
            _PROFILE,
            "hashA",
            11,
            rejects={},
            tool_not_found_refs=[],
            graph_facts={},
            risk_scores={},
            had_risk_scores=True,
        )
        assert c.get(_PROV, _AGENT, _PROFILE, "hashA", 11) is None


class TestCandidateHash:
    def test_order_independent(self):
        assert GraphConsultCache.candidate_hash(
            ["s::b", "s::a"]
        ) == GraphConsultCache.candidate_hash(["s::a", "s::b"])

    def test_different_set_differs(self):
        assert GraphConsultCache.candidate_hash(["s::a"]) != GraphConsultCache.candidate_hash(
            ["s::a", "s::b"]
        )


class TestProviderFingerprint:
    @staticmethod
    def _cfg(command="toolgraph", args=("serve",), env=None):
        return SimpleNamespace(command=command, args=list(args), env=env)

    def test_deterministic(self):
        assert GraphConsultCache.provider_fingerprint(
            self._cfg()
        ) == GraphConsultCache.provider_fingerprint(self._cfg())

    def test_env_values_ignored_keys_matter(self):
        base = GraphConsultCache.provider_fingerprint(self._cfg(env={"NEO4J_URI": "bolt://a"}))
        same_keys = GraphConsultCache.provider_fingerprint(self._cfg(env={"NEO4J_URI": "bolt://b"}))
        added_key = GraphConsultCache.provider_fingerprint(
            self._cfg(env={"NEO4J_URI": "bolt://a", "EXTRA": "1"})
        )
        assert base == same_keys  # value change with same keys → same fingerprint
        assert base != added_key  # new key → different fingerprint

    def test_command_and_args_matter(self):
        base = GraphConsultCache.provider_fingerprint(self._cfg())
        assert base != GraphConsultCache.provider_fingerprint(self._cfg(command="other"))
        assert base != GraphConsultCache.provider_fingerprint(self._cfg(args=("serve", "--x")))

    def test_none_env_equals_empty_env(self):
        assert GraphConsultCache.provider_fingerprint(
            self._cfg(env=None)
        ) == GraphConsultCache.provider_fingerprint(self._cfg(env={}))


class TestCorruptRow:
    """A malformed/old-schema/corrupt row is a best-effort MISS — it must never
    raise during startup (the caller subscripts the raw-fact keys) and is dropped
    so the next consult re-mints a fresh row."""

    def test_malformed_dict_row_is_dropped_as_miss(self, cache, caplog):
        _put(cache)  # valid row at scope (gen 11, hashA)
        cache._db.execute("UPDATE toolgraph_consult SET verdict_json = ?", ('{"rejects":"oops"}',))
        cache._db.commit()
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.toolgraph_cache"):
            assert cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11) is None
        assert cache._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0] == 0
        # "consult cache" is load-bearing: the #716 hit-test sweep filters on exactly
        # this substring, so rewording it out of the WARNING would silently drop the
        # malformed-row path from the sweep (#717). Representative pin — every
        # corrupt-row variant funnels into this one warning call.
        assert any("consult cache" in r.message for r in caplog.records)

    def test_missing_keys_row_is_dropped_as_miss(self, cache):
        _put(cache)
        # Valid JSON dict but missing the required raw-fact keys (old schema).
        cache._db.execute(
            "UPDATE toolgraph_consult SET verdict_json = ?", ('{"graph_generation":11}',)
        )
        cache._db.commit()
        assert cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11) is None
        assert cache._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0] == 0

    def test_invalid_json_row_is_dropped_as_miss(self, cache):
        _put(cache)
        cache._db.execute("UPDATE toolgraph_consult SET verdict_json = ?", ("{not valid json",))
        cache._db.commit()
        assert cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11) is None
        assert cache._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0] == 0

    @pytest.mark.parametrize(
        "verdict_json",
        [
            # Right containers, wrong LEAF types — would crash the caller's on-hit
            # frozenset()/dict() reconstruction if returned (it runs outside
            # the on_* knob try). Each must be dropped as a miss, not raised.
            '{"rejects":{},"tool_not_found_refs":[123],"graph_facts":{}}',
            '{"rejects":{"s::a":7},"tool_not_found_refs":[],"graph_facts":{}}',
            '{"rejects":{},"tool_not_found_refs":[],"graph_facts":{"s::a":0.5}}',
        ],
        ids=["nonstr_tnf", "nonstr_reject", "nonmapping_facts"],
    )
    def test_wrong_leaf_type_row_is_dropped_as_miss(self, cache, verdict_json):
        _put(cache)
        cache._db.execute("UPDATE toolgraph_consult SET verdict_json = ?", (verdict_json,))
        cache._db.commit()
        assert cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11) is None
        assert cache._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0] == 0

    def test_numeric_risk_scores_remain_a_valid_hit(self, cache):
        # Guard against over-strict leaf validation: a well-formed numeric row must
        # still be a HIT (not falsely dropped by the new leaf-type check).
        _put(cache, rejects={"s::a": "X"}, tnf=["s::b"], risk={"s::c": 1, "s::d": 0.5})
        row = cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11)
        assert row is not None
        assert row["graph_facts"]["s::c"]["risk_score"] == 1.0
        assert row["graph_facts"]["s::d"]["risk_score"] == 0.5

    def test_corrupt_fact_leaf_is_sanitized_rather_than_dropped(self, cache):
        """Inside a fact ROW the leaf rule is sanitize, not reject (#469).

        The row is a bag of best-effort telemetry facts, so a corrupted leaf
        must degrade to ``None`` — the same value a graph that could not answer
        produces — rather than cost the whole consult a cache hit. What still
        drops the row is a corrupted *identifier* or container, pinned above.
        """
        _put(cache)
        cache._db.execute(
            "UPDATE toolgraph_consult SET verdict_json = ?",
            (
                json.dumps(
                    {
                        "identity_policy": IDENTITY_POLICY,
                        "rejects": {},
                        "tool_not_found_refs": [],
                        "risk_scores": {},
                        "graph_facts": {
                            "s::a": {
                                "risk_score": "not-a-float",
                                "is_drifted": "yes",
                                "verdict": "MADE_UP",
                                "deny_paths": ["a->b"],
                            }
                        },
                    },
                    separators=(",", ":"),
                ),
            ),
        )
        cache._db.commit()
        row = cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11)
        assert row is not None
        facts = row["graph_facts"]["s::a"]
        assert facts["risk_score"] is None
        assert facts["is_drifted"] is None
        # An unknown verdict stays visible as "not one of ours" and never as
        # the upstream string itself.
        assert facts["verdict"] == GRAPH_VALUE_UNRECOGNIZED
        # Evidence paths are dropped on read too, not just on write.
        assert "deny_paths" not in facts

    @pytest.mark.parametrize(
        "facts",
        [
            {
                "rejects": {"s::bad\ud800": "NOT_GRANTED"},
                "tool_not_found_refs": [],
                "graph_facts": {},
            },
            {
                "rejects": {},
                "tool_not_found_refs": ["s::bad\udbff"],
                "graph_facts": {},
            },
            {
                "rejects": {},
                "tool_not_found_refs": [],
                "graph_facts": {"s::bad\udfff": {"risk_score": 0.5}},
            },
        ],
        ids=["reject", "tool_not_found", "facts"],
    )
    def test_legacy_unencodable_identifier_row_is_dropped(self, cache, facts):
        """The stored identifier itself is what must fail the shape check.

        ``identity_policy`` is stamped in deliberately: without it the row
        would be dropped for being pre-policy and this would pass no matter
        what the identifiers held.
        """
        _put(cache)
        cache._db.execute(
            "UPDATE toolgraph_consult SET verdict_json = ?",
            (json.dumps({"identity_policy": IDENTITY_POLICY, **facts}, separators=(",", ":")),),
        )
        cache._db.commit()
        assert cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11) is None
        assert cache._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0] == 0

    @pytest.mark.parametrize("stamp", [None, 0, "1", IDENTITY_POLICY + 1])
    def test_row_written_under_another_identity_policy_is_dropped(self, cache, stamp):
        """A warm hit reconstructs only rejects/refs/graph_facts, so the response
        fields the provider validates (agent, profile, eligible, tool_key) are
        not in the row and cannot be revalidated after upgrade. A pre-policy row
        could have been minted by a verdict today's policy refuses, and serving
        it would let a warm start come up clean where cold fail_starts."""
        _put(cache)
        facts = {
            "graph_generation": 11,
            "rejects": {"s::c": "NOT_GRANTED"},
            "tool_not_found_refs": [],
            "graph_facts": {},
        }
        if stamp is not None:
            facts["identity_policy"] = stamp
        cache._db.execute(
            "UPDATE toolgraph_consult SET verdict_json = ?",
            (json.dumps(facts, separators=(",", ":")),),
        )
        cache._db.commit()

        assert cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11) is None
        # Dropped, so the next consult re-mints under the current policy
        # instead of the stale row being re-read on every later start.
        assert cache._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0] == 0

    def test_put_stamps_the_current_identity_policy(self, cache):
        _put(cache)
        stored = json.loads(
            cache._db.execute("SELECT verdict_json FROM toolgraph_consult").fetchone()[0]
        )
        assert stored["identity_policy"] == IDENTITY_POLICY
        assert cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11) is not None

    def test_literal_surrogate_text_remains_a_valid_distinct_identifier(self, cache):
        literal = r"s::bad\ud800"
        _put(cache, rejects={literal: "NOT_GRANTED"})
        row = cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11)
        assert row["rejects"] == {literal: "NOT_GRANTED"}


class TestIdentifierWriteBoundary:
    def test_put_refuses_unencodable_fact_without_rewriting(self, cache):
        _put(cache, rejects={"s::bad\ud800": "NOT_GRANTED"})
        assert cache._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0] == 0

    def test_scope_identifier_misses_and_is_not_written(self, cache):
        cache.put(
            _PROV,
            "agent\ud800",
            _PROFILE,
            "hashA",
            11,
            rejects={},
            tool_not_found_refs=[],
            graph_facts={},
            risk_scores={},
            had_risk_scores=True,
        )
        assert cache.get(_PROV, "agent\ud800", _PROFILE, "hashA", 11) is None
        assert cache._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0] == 0


class _RaisingConn:
    """Stand-in connection whose ``execute`` always raises a sqlite fault.

    ``sqlite3.Connection.execute`` is a read-only C attribute and cannot be
    monkeypatched, so we swap the whole handle to simulate a runtime fault
    (database locked / disk I/O error / page-level corruption) during a live op.
    """

    def __init__(self, exc: sqlite3.Error) -> None:
        self._exc = exc
        self.rolled_back = False

    def execute(self, *_a, **_k):
        raise self._exc

    def commit(self, *_a, **_k):  # pragma: no cover - never reached past execute
        raise self._exc

    def rollback(self) -> None:
        # The put() guard rolls back a possibly-open transaction; model a clean
        # rollback so the no-op write path completes without raising.
        self.rolled_back = True

    def close(self) -> None:
        pass


class TestSqliteFaultIsBestEffort:
    """A runtime sqlite fault on get()/put() must degrade to a miss / no-op, never
    raise — these run inside ``_consult_toolgraph`` (the last statement of
    ``ProxyManager.start()``), whose callers don't catch ``sqlite3.Error``, so an
    escaping fault would crash proxy startup. Distinct from the corrupt-ROW tests
    above: those cover a successful read of malformed data; these cover ``execute``
    itself raising (database locked / disk I/O error / page-level corruption)."""

    def test_get_returns_miss_when_execute_raises(self, cache, caplog):
        _put(cache)  # real DB write while the handle is still live
        cache._db = _RaisingConn(sqlite3.OperationalError("database is locked"))
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.toolgraph_cache"):
            assert cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11) is None
        # "consult cache" is load-bearing for the #716 hit-test sweep (#717); exc_info
        # is what surfaces the sqlite traceback in pytest's captured-log section.
        degrades = [r for r in caplog.records if "consult cache" in r.message]
        assert degrades and degrades[0].exc_info

    def test_put_no_ops_when_execute_raises(self, cache, caplog):
        conn = _RaisingConn(sqlite3.OperationalError("disk I/O error"))
        cache._db = conn
        # Must not raise; the consult simply goes uncached.
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.toolgraph_cache"):
            _put(cache)
        # A fault mid-write must roll back any possibly-open transaction so the
        # connection does not retain the write lock.
        assert conn.rolled_back is True
        # Same load-bearing-substring pin as the get() test above (#716 sweep / #717).
        degrades = [r for r in caplog.records if "consult cache" in r.message]
        assert degrades and degrades[0].exc_info
