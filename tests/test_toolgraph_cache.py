"""Unit tests for ``GraphConsultCache`` (#494) — the tool-graph consult disk cache."""

from __future__ import annotations

import json
import logging
import sqlite3
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
            # float()/frozenset()/dict() reconstruction if returned (it runs outside
            # the on_* knob try). Each must be dropped as a miss, not raised.
            '{"rejects":{},"tool_not_found_refs":[],"risk_scores":{"s::a":"not-a-float"}}',
            '{"rejects":{},"tool_not_found_refs":[123],"risk_scores":{}}',
            '{"rejects":{"s::a":7},"tool_not_found_refs":[],"risk_scores":{}}',
            '{"rejects":{},"tool_not_found_refs":[],"risk_scores":{"s::a":true}}',
        ],
        ids=["nonfloat_risk", "nonstr_tnf", "nonstr_reject", "bool_risk"],
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
        assert row["risk_scores"] == {"s::c": 1.0, "s::d": 0.5}

    @pytest.mark.parametrize(
        "facts",
        [
            {
                "rejects": {"s::bad\ud800": "NOT_GRANTED"},
                "tool_not_found_refs": [],
                "risk_scores": {},
            },
            {
                "rejects": {},
                "tool_not_found_refs": ["s::bad\udbff"],
                "risk_scores": {},
            },
            {
                "rejects": {},
                "tool_not_found_refs": [],
                "risk_scores": {"s::bad\udfff": 0.5},
            },
        ],
        ids=["reject", "tool_not_found", "risk"],
    )
    def test_legacy_unencodable_identifier_row_is_dropped(self, cache, facts):
        _put(cache)
        cache._db.execute(
            "UPDATE toolgraph_consult SET verdict_json = ?",
            (json.dumps(facts, separators=(",", ":")),),
        )
        cache._db.commit()
        assert cache.get(_PROV, _AGENT, _PROFILE, "hashA", 11) is None
        assert cache._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0] == 0

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
