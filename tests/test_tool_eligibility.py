"""Tests for the STM-native tool-exposure hard filter (#465).

Pins the acceptance criteria: every reject reason code fires and is
recorded, rule precedence is stable, the profile ladder behaves
(``strict`` rejects / ``review`` demotes via ``risk_penalties`` /
``explore`` ignores signal rules), structural and config rules apply in
every profile, health flags come from upstream-attributable errors only,
and the whole pass is deterministic and side-effect-free.
"""

from __future__ import annotations

import time
from typing import Any

from memtomem_stm.proxy.config import (
    ExposureConfig,
    ExposureProfile,
    ToolOverrideConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyToolInfo
from memtomem_stm.proxy.metrics import CallMetrics, ErrorCategory
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.proxy.tool_eligibility import (
    REASON_CONFIG_HIDDEN,
    REASON_DUPLICATE_NAME,
    REASON_NAME_OVERFLOW,
    REASON_PROFILE_EXCLUDED,
    REASON_SENSITIVE_METADATA,
    REASON_UNHEALTHY,
    UPSTREAM_ERROR_CATEGORIES,
    ExposureCandidate,
    compute_health_flags,
    filter_tools,
)


def _server_cfg(prefix: str = "test", **kwargs: Any) -> UpstreamServerConfig:
    return UpstreamServerConfig(prefix=prefix, **kwargs)


def _cand(
    name: str,
    server_cfg: UpstreamServerConfig,
    *,
    desc: str = "Reads project data",
    raw_desc: str | None = None,
    schema: dict[str, Any] | None = None,
    server: str = "srv",
) -> ExposureCandidate:
    schema = schema if schema is not None else {"type": "object"}
    return ExposureCandidate(
        info=ProxyToolInfo(
            prefixed_name=f"{server_cfg.prefix}__{name}",
            description=desc,
            input_schema=schema,
            server=server,
            original_name=name,
        ),
        raw_description=raw_desc if raw_desc is not None else desc,
        raw_schema=schema,
        server_config=server_cfg,
    )


def _names(result) -> list[str]:
    return [info.prefixed_name for info in result.eligible]


STRICT = ExposureConfig(profile=ExposureProfile.STRICT)
REVIEW = ExposureConfig(profile=ExposureProfile.REVIEW)
EXPLORE = ExposureConfig(profile=ExposureProfile.EXPLORE)


# ── config rules ─────────────────────────────────────────────────────────


class TestConfigRules:
    def test_hidden_override_rejected_in_every_profile(self):
        cfg = _server_cfg(tool_overrides={"a": ToolOverrideConfig(hidden=True)})
        cands = [_cand("a", cfg), _cand("b", cfg)]
        for exposure in (STRICT, REVIEW, EXPLORE):
            result = filter_tools(cands, exposure)
            assert _names(result) == ["test__b"]
            assert result.reject_reasons == {"test__a": REASON_CONFIG_HIDDEN}

    def test_tool_profiles_list_excludes_other_profiles(self):
        cfg = _server_cfg(
            tool_overrides={
                "danger": ToolOverrideConfig(expose_in_profiles=[ExposureProfile.EXPLORE])
            }
        )
        cands = [_cand("danger", cfg)]
        for exposure in (STRICT, REVIEW):
            result = filter_tools(cands, exposure)
            assert result.eligible == []
            assert result.reject_reasons == {"test__danger": REASON_PROFILE_EXCLUDED}
        assert _names(filter_tools(cands, EXPLORE)) == ["test__danger"]

    def test_server_profiles_apply_to_all_its_tools(self):
        cfg = _server_cfg(expose_in_profiles=[ExposureProfile.STRICT])
        result = filter_tools([_cand("a", cfg), _cand("b", cfg)], REVIEW)
        assert result.eligible == []
        assert set(result.reject_reasons.values()) == {REASON_PROFILE_EXCLUDED}

    def test_tool_profiles_override_server_profiles(self):
        # Server restricts to explore; the tool-level list re-admits strict.
        cfg = _server_cfg(
            expose_in_profiles=[ExposureProfile.EXPLORE],
            tool_overrides={
                "a": ToolOverrideConfig(expose_in_profiles=[ExposureProfile.STRICT])
            },
        )
        result = filter_tools([_cand("a", cfg), _cand("b", cfg)], STRICT)
        assert _names(result) == ["test__a"]
        assert result.reject_reasons == {"test__b": REASON_PROFILE_EXCLUDED}

    def test_empty_profiles_list_hides_everywhere(self):
        cfg = _server_cfg(tool_overrides={"a": ToolOverrideConfig(expose_in_profiles=[])})
        for exposure in (STRICT, REVIEW, EXPLORE):
            result = filter_tools([_cand("a", cfg)], exposure)
            assert result.reject_reasons == {"test__a": REASON_PROFILE_EXCLUDED}


# ── structural rules ─────────────────────────────────────────────────────


class TestStructuralRules:
    def test_name_overflow_rejected_in_every_profile(self):
        cfg = _server_cfg()
        long_name = "x" * 60  # 21 overhead + 4 prefix + 60 ≫ 64
        cands = [_cand(long_name, cfg), _cand("ok", cfg)]
        for exposure in (STRICT, REVIEW, EXPLORE):
            result = filter_tools(cands, exposure)
            assert _names(result) == ["test__ok"]
            assert result.reject_reasons == {f"test__{long_name}": REASON_NAME_OVERFLOW}

    def test_duplicate_name_first_wins(self):
        cfg = _server_cfg()
        first = _cand("dup", cfg, desc="first copy")
        second = _cand("dup", cfg, desc="second copy")
        result = filter_tools([first, second], STRICT)
        assert result.eligible == [first.info]
        assert result.reject_reasons == {"test__dup": REASON_DUPLICATE_NAME}

    def test_rejected_tool_does_not_claim_its_name(self):
        # The first copy is signal-rejected (credential in metadata); the
        # clean second copy must be the one advertised, not dropped as a
        # duplicate of a tool that never got exposed.
        cfg = _server_cfg()
        flagged = _cand("dup", cfg, raw_desc="api_key=sk-" + "a" * 24)
        clean = _cand("dup", cfg, desc="clean copy")
        result = filter_tools([flagged, clean], STRICT)
        assert result.eligible == [clean.info]
        assert result.reject_reasons == {"test__dup": REASON_SENSITIVE_METADATA}


# ── signal rules: sensitive metadata ─────────────────────────────────────


class TestSensitiveMetadata:
    CRED_DESC = "Call with api_key= to authenticate"

    def test_credential_in_description_rejected_under_strict(self):
        result = filter_tools([_cand("a", _server_cfg(), raw_desc=self.CRED_DESC)], STRICT)
        assert result.eligible == []
        assert result.reject_reasons == {"test__a": REASON_SENSITIVE_METADATA}

    def test_credential_in_raw_schema_rejected_under_strict(self):
        schema = {
            "type": "object",
            "properties": {"token": {"type": "string", "default": "ghp_" + "a" * 36}},
        }
        result = filter_tools([_cand("a", _server_cfg(), schema=schema)], STRICT)
        assert result.reject_reasons == {"test__a": REASON_SENSITIVE_METADATA}

    def test_credential_only_in_truncated_tail_still_caught(self):
        # The advertised description was truncated before the credential;
        # the scan runs over the RAW description, so it still fires.
        raw = ("benign text " * 50) + "password=hunter2hunter2"
        cand = _cand("a", _server_cfg(), desc=raw[:80], raw_desc=raw)
        result = filter_tools([cand], STRICT)
        assert result.reject_reasons == {"test__a": REASON_SENSITIVE_METADATA}

    def test_email_is_not_a_credential(self):
        # PII patterns are deliberately excluded — a contact email in a
        # description must not hide the tool.
        result = filter_tools(
            [_cand("a", _server_cfg(), desc="Maintained by ops@example.com")], STRICT
        )
        assert result.eligible != []
        assert result.reject_reasons == {}

    def test_review_demotes_instead_of_rejecting(self):
        result = filter_tools([_cand("a", _server_cfg(), raw_desc=self.CRED_DESC)], REVIEW)
        assert _names(result) == ["test__a"]
        assert result.reject_reasons == {}
        assert result.risk_penalties == {"test__a": REVIEW.review_risk_penalty}

    def test_explore_ignores_signal(self):
        result = filter_tools([_cand("a", _server_cfg(), raw_desc=self.CRED_DESC)], EXPLORE)
        assert _names(result) == ["test__a"]
        assert result.reject_reasons == {}
        assert result.risk_penalties == {}


# ── signal rules: health ─────────────────────────────────────────────────


class TestHealthRule:
    def test_unhealthy_rejected_under_strict(self):
        cfg = _server_cfg()
        result = filter_tools(
            [_cand("bad", cfg), _cand("good", cfg)],
            STRICT,
            unhealthy=frozenset({("srv", "bad")}),
        )
        assert _names(result) == ["test__good"]
        assert result.reject_reasons == {"test__bad": REASON_UNHEALTHY}

    def test_unhealthy_demoted_under_review(self):
        custom = ExposureConfig(profile=ExposureProfile.REVIEW, review_risk_penalty=0.25)
        result = filter_tools(
            [_cand("bad", _server_cfg())], custom, unhealthy=frozenset({("srv", "bad")})
        )
        assert _names(result) == ["test__bad"]
        assert result.risk_penalties == {"test__bad": 0.25}

    def test_unhealthy_ignored_under_explore(self):
        result = filter_tools(
            [_cand("bad", _server_cfg())], EXPLORE, unhealthy=frozenset({("srv", "bad")})
        )
        assert result.risk_penalties == {}
        assert result.reject_reasons == {}

    def test_health_keyed_by_server_and_raw_name(self):
        # Same bare tool name on a different connection stays exposed.
        result = filter_tools(
            [_cand("bad", _server_cfg(), server="other")],
            STRICT,
            unhealthy=frozenset({("srv", "bad")}),
        )
        assert _names(result) == ["test__bad"]


# ── result invariants ────────────────────────────────────────────────────


class TestResultInvariants:
    def _mixed_result(self, exposure: ExposureConfig):
        cfg = _server_cfg(
            tool_overrides={
                "hidden": ToolOverrideConfig(hidden=True),
                "scoped": ToolOverrideConfig(expose_in_profiles=[ExposureProfile.EXPLORE]),
            }
        )
        cands = [
            _cand("hidden", cfg),
            _cand("scoped", cfg),
            _cand("creds", cfg, raw_desc="secret_key: sk-" + "b" * 30),
            _cand("flaky", cfg),
            _cand("ok", cfg),
        ]
        return filter_tools(cands, exposure, unhealthy=frozenset({("srv", "flaky")}))

    def test_eligible_and_rejected_are_disjoint(self):
        for exposure in (STRICT, REVIEW, EXPLORE):
            result = self._mixed_result(exposure)
            assert set(_names(result)).isdisjoint(result.reject_reasons)

    def test_penalties_only_name_eligible_tools(self):
        for exposure in (STRICT, REVIEW, EXPLORE):
            result = self._mixed_result(exposure)
            assert set(result.risk_penalties) <= set(_names(result))

    def test_deterministic_across_runs(self):
        a = self._mixed_result(STRICT)
        b = self._mixed_result(STRICT)
        assert _names(a) == _names(b)
        assert a.reject_reasons == b.reject_reasons
        assert a.risk_penalties == b.risk_penalties

    def test_strict_full_verdict(self):
        result = self._mixed_result(STRICT)
        assert _names(result) == ["test__ok"]
        assert result.reject_reasons == {
            "test__hidden": REASON_CONFIG_HIDDEN,
            "test__scoped": REASON_PROFILE_EXCLUDED,
            "test__creds": REASON_SENSITIVE_METADATA,
            "test__flaky": REASON_UNHEALTHY,
        }

    def test_review_keeps_signal_flagged_tools_with_penalties(self):
        result = self._mixed_result(REVIEW)
        assert _names(result) == ["test__creds", "test__flaky", "test__ok"]
        assert result.risk_penalties == {
            "test__creds": REVIEW.review_risk_penalty,
            "test__flaky": REVIEW.review_risk_penalty,
        }


# ── compute_health_flags ─────────────────────────────────────────────────


def _record(
    store: MetricsStore,
    tool: str,
    *,
    error: ErrorCategory | None = None,
    server: str = "srv",
) -> None:
    store.record(
        CallMetrics(
            server=server,
            tool=tool,
            original_chars=10,
            compressed_chars=10,
            is_error=error is not None,
            error_category=error,
        )
    )


class TestComputeHealthFlags:
    def _store(self, tmp_path) -> MetricsStore:
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        return store

    def test_consistently_failing_tool_flagged(self, tmp_path):
        store = self._store(tmp_path)
        for _ in range(5):
            _record(store, "bad", error=ErrorCategory.UPSTREAM_ERROR)
        for _ in range(5):
            _record(store, "good")
        assert compute_health_flags(store, STRICT) == frozenset({("srv", "bad")})

    def test_below_min_calls_is_presumed_healthy(self, tmp_path):
        store = self._store(tmp_path)
        for _ in range(4):  # default health_min_calls = 5
            _record(store, "sparse", error=ErrorCategory.TIMEOUT)
        assert compute_health_flags(store, STRICT) == frozenset()

    def test_below_threshold_not_flagged(self, tmp_path):
        store = self._store(tmp_path)
        for _ in range(9):
            _record(store, "flaky", error=ErrorCategory.TRANSPORT)
        _record(store, "flaky")  # 90% < default 95%
        assert compute_health_flags(store, STRICT) == frozenset()

    def test_proxy_internal_errors_do_not_count(self, tmp_path):
        store = self._store(tmp_path)
        for _ in range(10):
            _record(store, "victim", error=ErrorCategory.INTERNAL_ERROR)
        assert compute_health_flags(store, STRICT) == frozenset()

    def test_old_failures_age_out_of_window(self, tmp_path):
        store = self._store(tmp_path)
        for _ in range(10):
            _record(store, "recovered", error=ErrorCategory.UPSTREAM_ERROR)
        # Rewind the rows beyond the window so a restart re-admits the tool.
        assert store._db is not None
        store._db.execute(
            "UPDATE proxy_metrics SET created_at = ?",
            (time.time() - STRICT.health_window_hours * 3600.0 - 60.0,),
        )
        store._db.commit()
        assert compute_health_flags(store, STRICT) == frozenset()

    def test_no_store_means_no_flags(self):
        assert compute_health_flags(None, STRICT) == frozenset()

    def test_store_read_failure_fails_open(self, tmp_path):
        store = self._store(tmp_path)
        store.close()  # closed store returns {} rather than raising

        class Exploding:
            def get_tool_error_stats(self, *a, **k):
                raise RuntimeError("disk on fire")

        assert compute_health_flags(Exploding(), STRICT) == frozenset()


# ── MetricsStore.get_tool_error_stats ────────────────────────────────────


class TestGetToolErrorStats:
    def test_counts_calls_and_matching_errors(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        _record(store, "t", error=ErrorCategory.UPSTREAM_ERROR)
        _record(store, "t", error=ErrorCategory.INTERNAL_ERROR)  # not upstream-attributable
        _record(store, "t")
        stats = store.get_tool_error_stats(3600.0, UPSTREAM_ERROR_CATEGORIES)
        assert stats == {("srv", "t"): (3, 1)}

    def test_window_excludes_old_rows(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        _record(store, "t", error=ErrorCategory.TIMEOUT)
        assert store._db is not None
        store._db.execute("UPDATE proxy_metrics SET created_at = created_at - 7200")
        store._db.commit()
        assert store.get_tool_error_stats(3600.0, UPSTREAM_ERROR_CATEGORIES) == {}

    def test_empty_categories_count_no_errors(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        _record(store, "t", error=ErrorCategory.UPSTREAM_ERROR)
        assert store.get_tool_error_stats(3600.0, ()) == {("srv", "t"): (1, 0)}

    def test_uninitialized_store_returns_empty(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        assert store.get_tool_error_stats(3600.0, UPSTREAM_ERROR_CATEGORIES) == {}
