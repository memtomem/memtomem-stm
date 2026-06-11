"""Tests for the STM-native tool-exposure hard filter (#465).

Pins the acceptance criteria: every reject reason code fires and is
recorded, rule precedence is stable, the profile ladder behaves
(``strict`` rejects / ``review`` demotes via ``risk_penalties`` /
``explore`` ignores signal rules), structural and config rules apply in
every profile, health flags come from upstream-attributable errors only,
and the whole pass is deterministic and side-effect-free. The manager
wire-in section pins the cross-issue contract: reject reasons land in
selection telemetry (#467), review penalties land in relevance ranking
(#466), and a hard-rejected tool can never be resurrected by ranking.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ExposureConfig,
    ExposureProfile,
    ProxyConfig,
    ToolOverrideConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, ProxyToolInfo, UpstreamConnection
from memtomem_stm.proxy.metrics import CallMetrics, ErrorCategory, TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.proxy.selection_log import SelectionTelemetryLog
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
from memtomem_stm.proxy.tool_relevance import RANKER_VERSION_BM25_RISK


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
            tool_overrides={"a": ToolOverrideConfig(expose_in_profiles=[ExposureProfile.STRICT])},
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

    def test_duplicate_name_withholds_the_whole_group(self):
        # A composed name carried by 2+ candidates is one callable entity
        # wearing several metadata claims (upstream calls route by raw
        # name), so no occurrence may be advertised — ambiguous names are
        # never auto-exposed, the original #465 regression criterion.
        cfg = _server_cfg()
        first = _cand("dup", cfg, desc="first copy")
        second = _cand("dup", cfg, desc="second copy")
        for exposure in (STRICT, REVIEW, EXPLORE):
            result = filter_tools([first, second], exposure)
            assert result.eligible == []
            assert result.reject_reasons == {"test__dup": REASON_DUPLICATE_NAME}

    def test_clean_copy_cannot_launder_a_flagged_sibling(self):
        # codex R3 attack: list `dup` once with a credential and once
        # clean. Advertising the clean copy would attach clean metadata to
        # a callable whose upstream-side identity also matched a credential
        # — the group is structurally ambiguous and fully withheld, with
        # the ambiguity (not the signal) as the recorded reason.
        cfg = _server_cfg()
        flagged = _cand("dup", cfg, raw_desc="api_key=sk-" + "a" * 24)
        clean = _cand("dup", cfg, desc="clean copy")
        result = filter_tools([flagged, clean], STRICT)
        assert result.eligible == []
        assert result.reject_reasons == {"test__dup": REASON_DUPLICATE_NAME}

    def test_ambiguity_prepass_precedes_config_rules(self):
        # Even an operator-hidden name stays duplicate_name when duplicated:
        # the pre-pass runs before every per-tool rule, so the verdict
        # names the structural problem, not whichever rule fired first.
        cfg = _server_cfg(tool_overrides={"dup": ToolOverrideConfig(hidden=True)})
        result = filter_tools([_cand("dup", cfg), _cand("dup", cfg)], STRICT)
        assert result.eligible == []
        assert result.reject_reasons == {"test__dup": REASON_DUPLICATE_NAME}


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

    def test_credential_deep_in_large_schema_still_caught(self):
        # The scan is a security gate with no downstream backstop on the
        # advertisement path (telemetry never carries the schema), so it
        # must NOT be length-capped: a credential buried past any cap in a
        # huge raw schema still rejects (codex R2).
        schema = {
            "type": "object",
            "properties": {
                f"field_{i:04d}": {"type": "string", "description": "x" * 40} for i in range(400)
            }
            | {"zzz_last": {"type": "string", "default": "ghp_" + "a" * 36}},
        }
        result = filter_tools([_cand("a", _server_cfg(), schema=schema)], STRICT)
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

    def test_disjointness_holds_with_same_named_occurrences(self):
        """Telemetry must never claim a name was both advertised and
        withheld — a multi-occurrence name is withheld outright in every
        profile (one callable entity, several metadata claims), so the
        disjointness is structural."""
        cfg = _server_cfg()
        cands = [
            _cand("dup", cfg, raw_desc="api_key=sk-" + "c" * 24),  # signal-flagged
            _cand("dup", cfg, desc="clean copy"),
            _cand("dup", cfg, desc="clean again"),
            _cand("unique", cfg),
        ]
        for exposure in (STRICT, REVIEW, EXPLORE):
            result = filter_tools(cands, exposure)
            assert _names(result) == ["test__unique"]
            assert result.reject_reasons == {"test__dup": REASON_DUPLICATE_NAME}
            assert set(result.reject_reasons).isdisjoint(_names(result))

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


# ── ProxyManager wire-in ─────────────────────────────────────────────────


def _make_result(text: str):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], isError=False)


def _make_manager(
    tmp_path: Path,
    *,
    exposure: ExposureConfig | None = None,
    tool_overrides: dict[str, ToolOverrideConfig] | None = None,
    unhealthy: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[ProxyManager, SelectionTelemetryLog]:
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=CompressionStrategy.NONE,
        max_retries=0,
        reconnect_delay_seconds=0.0,
        tool_overrides=tool_overrides or {},
    )
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": server_cfg},
        exposure=exposure or ExposureConfig(),
    )
    log = SelectionTelemetryLog(tmp_path / "log.jsonl")
    log.initialize()
    mgr = ProxyManager(proxy_cfg, TokenTracker(), selection_log=log)

    tools = [
        SimpleNamespace(
            name="send_message",
            description="Send a message to a Slack channel",
            inputSchema={"type": "object", "properties": {"channel": {"type": "string"}}},
        ),
        SimpleNamespace(
            name="read_file",
            description="Read a file from the local filesystem",
            inputSchema={"type": "object"},
        ),
    ]
    session = AsyncMock()
    session.call_tool.return_value = _make_result("ok!")
    mgr._connections["srv"] = UpstreamConnection(
        name="srv", config=server_cfg, session=session, tools=tools
    )
    mgr._unhealthy_tools = unhealthy
    mgr.get_proxy_tools()  # advertise → snapshot eligibility verdict
    return mgr, log


def _events(log: SelectionTelemetryLog) -> list[dict]:
    return [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines() if line]


class TestManagerWireIn:
    async def test_hidden_reject_reaches_telemetry_and_never_ranks(self, tmp_path):
        """The resurrection invariant end-to-end: a hard-rejected tool is
        absent from exposure, absent from candidate_tools, absent from
        ranked_candidates — present only in reject_reasons."""
        mgr, log = _make_manager(
            tmp_path, tool_overrides={"read_file": ToolOverrideConfig(hidden=True)}
        )
        assert [i.prefixed_name for i in mgr.get_proxy_tools()] == ["test__send_message"]

        await mgr.call_tool("srv", "send_message", {"_context_query": "read a file"})
        selection, _execution = _events(log)
        assert selection["candidate_tools"] == ["test__send_message"]
        assert selection["reject_reasons"] == {"test__read_file": REASON_CONFIG_HIDDEN}
        ranked = selection["candidate_features"]["ranked_candidates"]
        assert {r["tool"] for r in ranked} == {"test__send_message"}

    async def test_unhealthy_strict_rejected_and_recorded(self, tmp_path):
        mgr, log = _make_manager(tmp_path, unhealthy=frozenset({("srv", "read_file")}))
        mgr.get_proxy_tools()
        assert mgr._advertised_tools == ["test__send_message"]

        await mgr.call_tool("srv", "send_message", {})
        selection, _execution = _events(log)
        assert selection["reject_reasons"] == {"test__read_file": REASON_UNHEALTHY}

    async def test_review_penalty_flows_into_ranking_and_version(self, tmp_path):
        mgr, log = _make_manager(
            tmp_path,
            exposure=ExposureConfig(profile=ExposureProfile.REVIEW, review_risk_penalty=0.5),
            unhealthy=frozenset({("srv", "read_file")}),
        )
        mgr.get_proxy_tools()
        # review: still advertised, nothing rejected.
        assert mgr._advertised_tools == ["test__send_message", "test__read_file"]

        await mgr.call_tool("srv", "read_file", {"_context_query": "read the file"})
        selection, execution = _events(log)
        assert selection["reject_reasons"] == {}
        assert selection["ranker_version"] == RANKER_VERSION_BM25_RISK
        assert execution["ranker_version"] == RANKER_VERSION_BM25_RISK
        entry = next(
            r
            for r in selection["candidate_features"]["ranked_candidates"]
            if r["tool"] == "test__read_file"
        )
        assert entry["risk_penalty"] == 0.5
        assert entry["final_score"] == round(entry["relevance_score"] * 0.5, 6)
        clean = next(
            r
            for r in selection["candidate_features"]["ranked_candidates"]
            if r["tool"] == "test__send_message"
        )
        assert clean["risk_penalty"] == 0.0
        assert clean["final_score"] == clean["relevance_score"]

    async def test_strict_without_flags_keeps_v1_version(self, tmp_path):
        mgr, log = _make_manager(tmp_path)
        await mgr.call_tool("srv", "read_file", {"_context_query": "read the file"})
        (selection, _execution) = _events(log)
        assert selection["ranker_version"] == "v1-bm25-tool-relevance"
        assert selection["reject_reasons"] == {}

    async def test_advertisement_is_stable_across_calls(self, tmp_path):
        """Teardown calls get_proxy_tools() again — same session state must
        produce the identical advertisement and verdict."""
        mgr, _log = _make_manager(tmp_path, unhealthy=frozenset({("srv", "read_file")}))
        first = [i.prefixed_name for i in mgr.get_proxy_tools()]
        rejects = dict(mgr._advertised_reject_reasons)
        second = [i.prefixed_name for i in mgr.get_proxy_tools()]
        assert first == second
        assert mgr._advertised_reject_reasons == rejects

    async def test_start_computes_health_flags_from_metrics_store(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        for _ in range(5):
            _record(store, "broken", error=ErrorCategory.UPSTREAM_ERROR)
        cfg = ProxyConfig(config_path=tmp_path / "proxy.json", upstream_servers={})
        mgr = ProxyManager(cfg, TokenTracker(metrics_store=store))
        await mgr.start()
        try:
            assert mgr._unhealthy_tools == frozenset({("srv", "broken")})
        finally:
            await mgr.stop()
            store.close()
