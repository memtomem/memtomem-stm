"""Unit tests for ``GraphConsultCache`` (#494) — the tool-graph consult disk cache."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from memtomem_stm.proxy.toolgraph_cache import GraphConsultCache

_PROV = "prov-fp"
_AGENT = "stm-proxy"
_PROFILE = "strict"


@pytest.fixture
def cache(tmp_path):
    c = GraphConsultCache(tmp_path / "tg.db")
    c.initialize()
    yield c
    c.close()


def _put(cache, *, generation=11, cand="hashA", rejects=None, tnf=None, risk=None, had_risk=True):
    cache.put(
        _PROV,
        _AGENT,
        _PROFILE,
        cand,
        generation,
        rejects=rejects or {},
        tool_not_found_refs=tnf or [],
        risk_scores=risk or {},
        had_risk_scores=had_risk,
    )


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
        assert row["risk_scores"] == {"s::c": 0.5}
        assert row["had_risk_scores"] is True

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

    def test_malformed_dict_row_is_dropped_as_miss(self, cache):
        _put(cache)  # valid row at scope (gen 11, hashA)
        cache._db.execute("UPDATE toolgraph_consult SET verdict_json = ?", ('{"rejects":"oops"}',))
        cache._db.commit()
        assert cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11) is None
        assert cache._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0] == 0

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
