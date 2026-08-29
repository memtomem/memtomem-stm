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

import pytest

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
    GRAPH_FACT_KEYS,
    MAX_DENY_PATH_COUNT,
    GRAPH_VALUE_UNRECOGNIZED,
    REASON_CONFIG_HIDDEN,
    REASON_DUPLICATE_NAME,
    REASON_NAME_OVERFLOW,
    REASON_PROFILE_EXCLUDED,
    REASON_SENSITIVE_METADATA,
    REASON_TASK_REQUIRED,
    REASON_TOOLGRAPH_AMBIGUOUS,
    REASON_TOOLGRAPH_DENY_GOVERNED,
    REASON_TOOLGRAPH_DENY_VIOLATION,
    REASON_TOOLGRAPH_DRIFTED,
    REASON_TOOLGRAPH_NOT_GRANTED,
    REASON_TOOLGRAPH_REJECTED,
    REASON_TOOLGRAPH_TOOL_NOT_FOUND,
    REASON_TOOLGRAPH_UNMAPPED,
    REASON_TOOLGRAPH_UNREACHABLE,
    REASON_UNHEALTHY,
    UPSTREAM_ERROR_CATEGORIES,
    ExposureCandidate,
    compute_health_flags,
    filter_tools,
    interpret_verdict,
    parse_graph_facts,
    parse_graph_features,
    parse_risk_scores,
    sanitize_graph_facts_row,
    toolgraph_reject_code,
)
from memtomem_stm.proxy.tool_relevance import (
    PENALTY_SOURCE_NONE,
    PENALTY_SOURCE_REVIEW,
    RANKER_VERSION_BM25_RISK,
)
from memtomem_stm.proxy.toolgraph_provider import ToolgraphProtocolError


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
    task_support: str | None = None,
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
        raw_task_support=task_support,
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

    def test_task_required_rejected_in_every_profile(self):
        # ``execution.taskSupport: "required"`` names a tool the synchronous
        # call path cannot serve, so every profile withholds it — there is
        # nothing for review to observe when the failure is certain (#892).
        cfg = _server_cfg()
        cands = [_cand("bg", cfg, task_support="required"), _cand("ok", cfg)]
        for exposure in (STRICT, REVIEW, EXPLORE):
            result = filter_tools(cands, exposure)
            assert _names(result) == ["test__ok"]
            assert result.reject_reasons == {"test__bg": REASON_TASK_REQUIRED}
            assert result.risk_penalties == {}

    def test_task_optional_forbidden_and_absent_are_advertised(self):
        # ``optional`` is the deliberate downgrade: advertised, and (pinned
        # in test_fastmcp_compat) without ``execution``. ``forbidden`` and an
        # absent ``execution`` were never in question.
        cfg = _server_cfg()
        cands = [
            _cand("opt", cfg, task_support="optional"),
            _cand("forb", cfg, task_support="forbidden"),
            _cand("absent", cfg),
        ]
        for exposure in (STRICT, REVIEW, EXPLORE):
            result = filter_tools(cands, exposure)
            assert _names(result) == ["test__opt", "test__forb", "test__absent"]
            assert result.reject_reasons == {}
            assert result.risk_penalties == {}

    def test_config_hidden_outranks_task_required(self):
        # Config rules run before the structural block: an operator-hidden
        # tool reports the operator's intent, not the task-support fact.
        cfg = _server_cfg(tool_overrides={"bg": ToolOverrideConfig(hidden=True)})
        result = filter_tools([_cand("bg", cfg, task_support="required")], STRICT)
        assert result.eligible == []
        assert result.reject_reasons == {"test__bg": REASON_CONFIG_HIDDEN}

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
            _cand("bg", cfg, task_support="required"),
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
            "test__bg": REASON_TASK_REQUIRED,
        }

    def test_review_keeps_signal_flagged_tools_with_penalties(self):
        # ``bg`` stays withheld here: task_required is structural, so review
        # has no demoted-but-advertised variant of it.
        result = self._mixed_result(REVIEW)
        assert _names(result) == ["test__creds", "test__flaky", "test__ok"]
        assert result.reject_reasons["test__bg"] == REASON_TASK_REQUIRED
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
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], is_error=False)


def _make_manager(
    tmp_path: Path,
    *,
    exposure: ExposureConfig | None = None,
    tool_overrides: dict[str, ToolOverrideConfig] | None = None,
    unhealthy: frozenset[tuple[str, str]] = frozenset(),
    task_supports: dict[str, str] | None = None,
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
            input_schema={"type": "object", "properties": {"channel": {"type": "string"}}},
        ),
        SimpleNamespace(
            name="read_file",
            description="Read a file from the local filesystem",
            input_schema={"type": "object"},
        ),
    ]
    # Only the named tools grow an ``execution``; any tool a caller leaves
    # unnamed stays bare, so a caller that names none of them exercises the
    # pre-field shape (an SDK, or a fake, without ``execution`` at all).
    for tool in tools:
        if task_supports and tool.name in task_supports:
            tool.execution = SimpleNamespace(task_support=task_supports[tool.name])
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

    async def test_task_required_reject_reaches_telemetry_and_never_ranks(self, tmp_path):
        """#892 end-to-end: the upstream's ``execution.taskSupport`` reaches
        the filter, a ``required`` tool is withheld under the new code, and an
        ``optional`` one is still advertised and callable synchronously."""
        mgr, log = _make_manager(
            tmp_path,
            task_supports={"read_file": "required", "send_message": "optional"},
        )
        assert [i.prefixed_name for i in mgr.get_proxy_tools()] == ["test__send_message"]

        # The optional-task tool is not merely listed: it answers on the
        # synchronous call path the downgrade promises.
        assert "ok!" in await mgr.call_tool(
            "srv", "send_message", {"_context_query": "read a file"}
        )
        selection, _execution = _events(log)
        assert selection["candidate_tools"] == ["test__send_message"]
        assert selection["reject_reasons"] == {"test__read_file": REASON_TASK_REQUIRED}
        ranked = selection["candidate_features"]["ranked_candidates"]
        assert {r["tool"] for r in ranked} == {"test__send_message"}

    async def test_optional_task_upstream_reaches_the_client_without_execution(self, tmp_path):
        """The optional-task downgrade, end to end (#892).

        The eligibility test above proves an ``optional`` candidate survives
        the filter; this one carries such an upstream tool through the real
        advertisement path — ``get_proxy_tools`` → ``register_proxy_tool`` →
        a real ``ClientSession`` over an in-memory transport — and pins what
        the client actually receives: a tool it decodes with no ``execution``
        — the spec's "forbidden" — which answers an ordinary non-task call.
        The assertion is on the decoded model, so it pins the ABSENCE the
        client observes, not which wire shape produced it (today the server
        supplies no value and the serializer excludes ``None``, so the field
        is omitted; an explicit ``null`` would decode the same and is equally
        acceptable).
        ``read_file`` is left bare here, which also pins that a tool object
        carrying no ``execution`` at all still advertises.

        This is a forward pin, not a red-first test: in the pinned SDK the
        registration path has no seam that could express task support at all
        — ``add_tool`` takes no such kwarg and the server-side ``Tool`` model
        has no such field — so nothing here can be made to fail by forwarding
        today. It fails the day an SDK grows that seam and something fills it
        from an upstream tool, which is the regression worth catching.
        """
        from mcp import ClientSession
        from mcp.client._memory import InMemoryTransport
        from mcp.server.mcpserver import MCPServer

        from memtomem_stm.proxy._fastmcp_compat import register_proxy_tool, to_call_tool_result

        mgr, _log = _make_manager(tmp_path, task_supports={"send_message": "optional"})
        server = MCPServer("task-support-test")

        def _handler(info):
            async def proxy_tool(**kwargs: object):
                return to_call_tool_result(
                    await mgr.call_tool(info.server, info.original_name, dict(kwargs))
                )

            return proxy_tool

        advertised = mgr.get_proxy_tools()
        assert [i.prefixed_name for i in advertised] == ["test__send_message", "test__read_file"]
        for info in advertised:
            register_proxy_tool(server, _handler(info), info)

        async with InMemoryTransport(server) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                listed = await session.list_tools()
                (tool,) = [t for t in listed.tools if t.name == "test__send_message"]
                assert tool.execution is None
                result = await session.call_tool("test__send_message", {"channel": "#general"})

        assert result.is_error is not True

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
        assert entry["risk_penalty_source"] == PENALTY_SOURCE_REVIEW
        assert entry["final_score"] == round(entry["relevance_score"] * 0.5, 6)
        clean = next(
            r
            for r in selection["candidate_features"]["ranked_candidates"]
            if r["tool"] == "test__send_message"
        )
        assert clean["risk_penalty"] == 0.0
        assert clean["risk_penalty_source"] == PENALTY_SOURCE_NONE
        assert clean["final_score"] == clean["relevance_score"]

    async def test_strict_without_flags_keeps_v1_version(self, tmp_path):
        mgr, log = _make_manager(tmp_path)
        await mgr.call_tool("srv", "read_file", {"_context_query": "read the file"})
        (selection, _execution) = _events(log)
        assert selection["ranker_version"] == "v1-bm25-tool-relevance"
        assert selection["reject_reasons"] == {}

    async def test_advertisement_is_stable_across_calls(self, tmp_path):
        """``get_proxy_tools()`` re-derives on every call and each pass
        rewrites the advertisement snapshot that ``stm_proxy_health`` reports
        per upstream and the relevance ranker scores against. Unchanged session
        state must therefore produce the identical advertised names and reject
        reasons: a pass that reshuffled or dropped tools on its own would make
        that shared snapshot depend on how often something happened to ask for
        the tool list. (Teardown no longer calls it — see #891 — so this is
        snapshot determinism, not a removal contract.)"""
        mgr, _log = _make_manager(tmp_path, unhealthy=frozenset({("srv", "read_file")}))
        first = [i.prefixed_name for i in mgr.get_proxy_tools()]
        rejects = dict(mgr._advertised_reject_reasons)
        second = [i.prefixed_name for i in mgr.get_proxy_tools()]
        assert first == second
        assert mgr._advertised_reject_reasons == rejects

    async def test_retain_registered_moves_declines_out_of_the_advertisement(self, tmp_path):
        """#908: exposure is decided — and snapshotted — before the server tries
        to register anything, so a name registration then declines used to stay
        in the snapshot that health counts, the ranker scores and selection
        telemetry records as a candidate. Narrowing must drop it from all of
        those and account for it as a ``registration_declined`` reject rather
        than letting it disappear unexplained."""
        mgr, _log = _make_manager(tmp_path)
        advertised = list(mgr._advertised_tools)
        assert {"test__send_message", "test__read_file"} == set(advertised)
        mgr._advertised_risk_penalties["test__read_file"] = 0.5
        mgr._advertised_risk_penalty_sources["test__read_file"] = "graph"
        mgr._advertised_graph_facts["test__read_file"] = {"tier": "governed"}

        dropped = mgr.retain_registered_advertisement(["test__send_message"])

        assert dropped == ["test__read_file"]
        assert mgr._advertised_tools == ["test__send_message"]
        assert [i.prefixed_name for i in mgr._advertised_infos] == ["test__send_message"]
        assert mgr._advertised_reject_reasons["test__read_file"] == "registration_declined"
        assert "test__read_file" not in mgr._advertised_risk_penalties
        assert "test__read_file" not in mgr._advertised_risk_penalty_sources
        assert "test__read_file" not in mgr._advertised_graph_facts
        assert mgr.get_upstream_health()["srv"]["advertised_tools"] == 1

    async def test_declined_tool_reaches_the_selection_log_as_a_reject(self, tmp_path):
        """The narrowing has to land in the recorded telemetry, not just in the
        in-memory maps: a declined name must leave ``candidate_tools`` and
        appear in ``reject_reasons``, so replay sees a withhold with a cause
        instead of a tool that quietly stopped being a candidate."""
        mgr, log = _make_manager(tmp_path)
        mgr.retain_registered_advertisement(["test__send_message"])

        await mgr.call_tool("srv", "send_message", {"_context_query": "send a message"})

        (selection, _execution) = _events(log)
        assert selection["candidate_tools"] == ["test__send_message"]
        assert selection["reject_reasons"] == {"test__read_file": "registration_declined"}

    async def test_retain_registered_is_a_no_op_when_everything_registered(self, tmp_path):
        """The normal path must not touch the snapshot — in particular it must
        not invent reject reasons for tools that registered fine, since those
        values are a closed vocabulary the selection log records."""
        mgr, _log = _make_manager(tmp_path)
        mgr._advertised_risk_penalties["test__read_file"] = 0.5
        mgr._advertised_risk_penalty_sources["test__read_file"] = "graph"
        mgr._advertised_graph_facts["test__read_file"] = {"tier": "governed"}

        def snapshot():
            return (
                list(mgr._advertised_tools),
                list(mgr._advertised_infos),
                dict(mgr._advertised_reject_reasons),
                dict(mgr._advertised_risk_penalties),
                dict(mgr._advertised_risk_penalty_sources),
                dict(mgr._advertised_graph_facts),
            )

        before = snapshot()

        assert mgr.retain_registered_advertisement(list(mgr._advertised_tools)) == []

        assert snapshot() == before

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

    async def test_start_derived_health_flows_into_exposure(self, tmp_path):
        """End-to-end seam: failing metrics history → ``start()`` *derives*
        ``_unhealthy_tools`` → ``get_proxy_tools()`` drops the unhealthy tool
        from exposure and records ``REASON_UNHEALTHY`` — with the set never
        injected.

        The two existing tests cover the halves in isolation:
        ``test_start_computes_health_flags_from_metrics_store`` proves
        derivation→set, and ``test_unhealthy_strict_rejected_and_recorded``
        proves injected-set→exposure. Neither joins them, so a key-format
        drift between ``compute_health_flags`` (which keys by upstream-server
        name) and the exposure-filter lookup would pass both yet silently stop
        filtering. This pins the join: derived ``("srv", "broken")`` must reach
        the filter that advertises by prefix.
        """
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        for _ in range(5):
            _record(store, "broken", error=ErrorCategory.UPSTREAM_ERROR)

        server_cfg = UpstreamServerConfig(
            prefix="test",
            compression=CompressionStrategy.NONE,
            max_retries=0,
            reconnect_delay_seconds=0.0,
        )
        # upstream_servers empty + a non-existent config_path → start() makes no
        # real connection (and the fresh-manager path skips the double-start
        # guard), so the injected connection below survives into derivation.
        cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={},
            exposure=ExposureConfig(),  # strict default profile
        )
        mgr = ProxyManager(cfg, TokenTracker(metrics_store=store))

        session = AsyncMock()
        session.call_tool.return_value = _make_result("ok!")
        mgr._connections["srv"] = UpstreamConnection(
            name="srv",
            config=server_cfg,
            session=session,
            tools=[
                SimpleNamespace(
                    name="broken",
                    description="A flaky tool",
                    input_schema={"type": "object"},
                ),
                SimpleNamespace(
                    name="send_message",
                    description="Send a message to a Slack channel",
                    input_schema={"type": "object"},
                ),
            ],
        )

        await mgr.start()
        try:
            # Derivation ran from the seeded store — the set is NOT injected.
            assert mgr._unhealthy_tools == frozenset({("srv", "broken")})
            # ...and that derived key actually reaches the exposure filter.
            advertised = [i.prefixed_name for i in mgr.get_proxy_tools()]
            assert advertised == ["test__send_message"]
            assert mgr._advertised_reject_reasons == {"test__broken": REASON_UNHEALTHY}
        finally:
            await mgr.stop()
            store.close()


# ── interpret_verdict (pure parse of the external consult, #465) ────────────


def _verdict(*, agent_found=True, rejected=None, graph_generation=11):
    v: dict[str, Any] = {
        "agent": "stm-proxy",
        "agent_found": agent_found,
        "profile": "strict",
        "eligible": [],
        "rejected": rejected or [],
    }
    if graph_generation is not _MISSING:
        v["graph_generation"] = graph_generation
    return v


_MISSING = object()


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("TOOL_NOT_FOUND", REASON_TOOLGRAPH_TOOL_NOT_FOUND),
        ("AMBIGUOUS_TOOL", REASON_TOOLGRAPH_AMBIGUOUS),
        ("NOT_GRANTED", REASON_TOOLGRAPH_NOT_GRANTED),
        ("DENY_VIOLATION", REASON_TOOLGRAPH_DENY_VIOLATION),
        ("DENY_GOVERNED", REASON_TOOLGRAPH_DENY_GOVERNED),
        ("DRIFTED", REASON_TOOLGRAPH_DRIFTED),
        ("UNMAPPED", REASON_TOOLGRAPH_UNMAPPED),
        ("FUTURE_REASON", REASON_TOOLGRAPH_REJECTED),
    ],
)
def test_toolgraph_reject_code_is_the_shared_reason_boundary(reason, expected):
    assert toolgraph_reject_code(reason) == expected


class TestInterpretVerdict:
    def test_success_maps_reject_reasons(self):
        v = _verdict(
            rejected=[
                {"candidate": "s::blocked", "reason": "NOT_GRANTED"},
                {"candidate": "s::dangerous", "reason": "DENY_VIOLATION"},
            ]
        )
        interp = interpret_verdict(v)
        assert interp.agent_found is True
        assert interp.graph_generation == 11
        assert interp.rejects == {
            "s::blocked": "toolgraph_not_granted",
            "s::dangerous": "toolgraph_deny_violation",
        }
        assert interp.tool_not_found_refs == frozenset()

    def test_unknown_reason_falls_back_to_generic_reject(self):
        # An upstream reason STM does not recognize still withholds (fail-safe),
        # never silently advertises a tool the graph rejected.
        interp = interpret_verdict(
            _verdict(rejected=[{"candidate": "s::x", "reason": "SOME_NEW_REASON"}])
        )
        assert interp.rejects == {"s::x": REASON_TOOLGRAPH_REJECTED}

    def test_unmapped_reason_has_dedicated_code(self):
        # UNMAPPED is a first-class reject in the graph's strict profile (the
        # default query_profile), so it gets a 1:1 code, not the generic fallback.
        interp = interpret_verdict(_verdict(rejected=[{"candidate": "s::u", "reason": "UNMAPPED"}]))
        assert interp.rejects == {"s::u": REASON_TOOLGRAPH_UNMAPPED}

    def test_tool_not_found_is_mapped_and_tracked(self):
        interp = interpret_verdict(
            _verdict(rejected=[{"candidate": "s::missing", "reason": "TOOL_NOT_FOUND"}])
        )
        assert interp.rejects == {"s::missing": REASON_TOOLGRAPH_TOOL_NOT_FOUND}
        assert interp.tool_not_found_refs == frozenset({"s::missing"})

    def test_agent_not_found_aborts_with_generation(self):
        interp = interpret_verdict(_verdict(agent_found=False))
        assert interp.agent_found is False
        assert interp.rejects == {}
        assert interp.tool_not_found_refs == frozenset()
        assert interp.graph_generation == 11

    def test_missing_generation_on_abort_is_protocol_error(self):
        # The server stamps graph_generation on EVERY path via _with_generation
        # (the abort included), so a missing one is contract drift even when
        # agent_found is False.
        with pytest.raises(ToolgraphProtocolError):
            interpret_verdict(_verdict(agent_found=False, graph_generation=_MISSING))

    def test_non_bool_agent_found_is_protocol_error(self):
        with pytest.raises(ToolgraphProtocolError):
            interpret_verdict({"agent_found": "yes", "graph_generation": 1, "rejected": []})

    def test_missing_generation_when_found_is_protocol_error(self):
        with pytest.raises(ToolgraphProtocolError):
            interpret_verdict(_verdict(graph_generation=_MISSING))

    def test_bool_generation_is_protocol_error(self):
        # bool is an int subclass but never a valid generation.
        with pytest.raises(ToolgraphProtocolError):
            interpret_verdict(_verdict(graph_generation=True))

    def test_rejected_not_a_list_is_protocol_error(self):
        with pytest.raises(ToolgraphProtocolError):
            interpret_verdict({"agent_found": True, "graph_generation": 1, "rejected": {}})

    def test_reject_row_missing_fields_is_protocol_error(self):
        with pytest.raises(ToolgraphProtocolError):
            interpret_verdict(_verdict(rejected=[{"candidate": "s::x"}]))


# ── parse_risk_scores (rank_features risk_score enrichment, #493) ───────────


class TestParseRiskScores:
    def test_keeps_positive_scores_skips_zero_and_none(self):
        v = {
            "agent": "stm-proxy",
            "agent_found": True,
            "features": [
                {"candidate": "s::clean", "risk_score": 0.0},
                {"candidate": "s::risky", "risk_score": 0.4},
                {"candidate": "s::violation", "risk_score": 1.0},
                {"candidate": "s::unresolved", "risk_score": None},
            ],
            "graph_generation": 11,
        }
        assert parse_risk_scores(v) == {"s::risky": 0.4, "s::violation": 1.0}

    def test_int_score_coerced_to_float(self):
        v = {"features": [{"candidate": "s::a", "risk_score": 1}]}
        scores = parse_risk_scores(v)
        assert scores == {"s::a": 1.0}
        assert isinstance(scores["s::a"], float)

    def test_bool_score_rejected(self):
        # bool is an int subclass but never a valid risk_score.
        v = {"features": [{"candidate": "s::a", "risk_score": True}]}
        assert parse_risk_scores(v) == {}

    def test_lenient_on_malformed_payload(self):
        # Best-effort enrichment: a malformed shape yields an empty map, never
        # raises (unlike interpret_verdict's contract failures).
        assert parse_risk_scores({}) == {}
        assert parse_risk_scores({"features": "nope"}) == {}
        assert parse_risk_scores({"features": [None, 42, "x"]}) == {}
        assert parse_risk_scores({"features": [{"candidate": 5, "risk_score": 0.4}]}) == {}
        assert parse_risk_scores({"features": [{"candidate": "s::a", "risk_score": "hi"}]}) == {}
        assert parse_risk_scores({"features": [{"risk_score": 0.4}]}) == {}

    def test_agent_not_found_has_no_features(self):
        v = {"agent_found": False, "features": [], "graph_generation": 11}
        assert parse_risk_scores(v) == {}


# ── parse_graph_facts / sanitize_graph_facts_row (#469 feature logging) ────


_REAL_ROW = {
    "candidate": "s::risky",
    "tool_key": "s::risky",
    "found": True,
    "ambiguous": False,
    "permitted": True,
    "verdict": "ALLOW",
    "classification": None,
    "deny_paths": [],
    "is_drifted": False,
    "is_unmapped": False,
    "has_unbacked_edges": True,
    "read_only_hint": False,
    "destructive_hint": True,
    "idempotent_hint": False,
    "open_world_hint": None,
    "risk_score": 0.4,
}


class TestSanitizeGraphFactsRow:
    def test_copies_the_recordable_facts_verbatim(self):
        row = sanitize_graph_facts_row(_REAL_ROW)
        assert row["found"] is True
        assert row["permitted"] is True
        assert row["has_unbacked_edges"] is True
        assert row["destructive_hint"] is True
        assert row["open_world_hint"] is None
        assert row["verdict"] == "ALLOW"
        assert row["risk_score"] == 0.4

    def test_drops_identifiers_and_evidence_paths(self):
        """The redaction line: what the selection log may never carry.

        ``tool_key`` and ``deny_paths`` are graph-authored text — a
        server-qualified identifier and the policy-evidence chain behind a
        DENY. The selection log's redaction is structural, so these are
        dropped at the parser rather than screened downstream; ``deny_paths``
        survives only as a count.
        """
        row = sanitize_graph_facts_row(
            {**_REAL_ROW, "deny_paths": [["Agent", "READS", "Secret"], ["x"]], "candidates": ["a"]}
        )
        assert set(row) == set(GRAPH_FACT_KEYS)
        assert "tool_key" not in row
        assert "deny_paths" not in row
        assert "candidates" not in row
        assert row["deny_path_count"] == 2

    def test_missing_deny_paths_is_unknown_not_zero(self):
        # "the row reported no list" and "the row reported an empty list" are
        # different facts; a count of 0 must mean the latter.
        assert sanitize_graph_facts_row({})["deny_path_count"] is None
        assert sanitize_graph_facts_row({"deny_paths": []})["deny_path_count"] == 0

    def test_unknown_enum_becomes_the_sentinel(self):
        """An upstream that grows a verdict stays visible without leaking it.

        Recording the raw string would put upstream-authored free text in the
        telemetry file; dropping the row would hide a fact the graph did
        report. The sentinel does neither.
        """
        row = sanitize_graph_facts_row({**_REAL_ROW, "verdict": "QUARANTINED"})
        assert row["verdict"] == GRAPH_VALUE_UNRECOGNIZED
        row = sanitize_graph_facts_row({**_REAL_ROW, "classification": "brand-new-thing"})
        assert row["classification"] == GRAPH_VALUE_UNRECOGNIZED

    def test_known_classification_survives(self):
        row = sanitize_graph_facts_row({**_REAL_ROW, "classification": "violation"})
        assert row["classification"] == "violation"

    def test_wrong_types_degrade_to_none_never_raise(self):
        row = sanitize_graph_facts_row(
            {
                "found": "yes",
                "permitted": 1,  # int, not bool
                "verdict": 7,
                "deny_paths": "path",
                "risk_score": "0.4",
            }
        )
        assert set(row) == set(GRAPH_FACT_KEYS)
        assert all(row[key] is None for key in ("found", "permitted", "verdict", "risk_score"))
        assert row["deny_path_count"] is None

    def test_key_set_is_always_complete(self):
        # Dense by construction: a reader never has to tell "field absent"
        # from "fact unknown".
        assert set(sanitize_graph_facts_row({})) == set(GRAPH_FACT_KEYS)


class TestGraphFactsEdgeCases:
    """Inputs a malformed upstream can send (codex round 1, PR #852)."""

    def test_an_oversized_integer_score_does_not_raise(self):
        """``float()`` is not total. This enrichment is documented best-effort
        and runs inside ``start()``, so an escaping ``OverflowError`` would
        abort the proxy over a telemetry field."""
        v = {"features": [{"candidate": "s::a", "risk_score": 10**400}]}
        assert parse_graph_facts(v)["s::a"]["risk_score"] is None
        assert parse_risk_scores(v) == {}

    @pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
    def test_non_finite_scores_are_refused(self, value):
        """``NaN``/``Infinity`` are not JSON: one would reach the telemetry file
        and the consult cache as a token strict readers reject, and as a penalty
        ``inf`` drives every final score to ``-inf``."""
        v = {"features": [{"candidate": "s::a", "risk_score": value}]}
        assert parse_graph_facts(v)["s::a"]["risk_score"] is None
        assert parse_risk_scores(v) == {}

    def test_out_of_range_but_finite_scores_are_recorded(self):
        """Positive control for the two above: the refusals are about values
        that cannot be represented or serialized, not about the graph's
        ``[0,1]`` promise, which is the graph's to keep."""
        v = {"features": [{"candidate": "s::a", "risk_score": 4.2}]}
        assert parse_graph_facts(v)["s::a"]["risk_score"] == 4.2

    def test_sanitizing_a_sanitized_row_is_a_no_op(self):
        """The consult cache sanitizes on write AND on read, so a rule that
        read ``deny_paths`` only would erase the count it had just derived —
        making a warm start disagree with the cold start that filled it."""
        once = sanitize_graph_facts_row({**_REAL_ROW, "deny_paths": [["a"], ["b"]]})
        twice = sanitize_graph_facts_row(once)
        assert once["deny_path_count"] == 2
        assert twice == once

    def test_a_bogus_stored_count_does_not_survive_sanitizing(self):
        assert sanitize_graph_facts_row({"deny_path_count": -1})["deny_path_count"] is None
        assert sanitize_graph_facts_row({"deny_path_count": "two"})["deny_path_count"] is None
        assert sanitize_graph_facts_row({"deny_path_count": True})["deny_path_count"] is None

    def test_an_absurd_count_records_as_unknown(self):
        """An unbounded integer is not a large answer, it is a corrupt one —
        and not portable as a learning feature (no float, no fixed-width
        column). Above the bound the fact records as unknown."""
        assert (
            sanitize_graph_facts_row({"deny_path_count": MAX_DENY_PATH_COUNT})["deny_path_count"]
            == MAX_DENY_PATH_COUNT
        )
        assert (
            sanitize_graph_facts_row({"deny_path_count": MAX_DENY_PATH_COUNT + 1})[
                "deny_path_count"
            ]
            is None
        )
        assert sanitize_graph_facts_row({"deny_path_count": 10**400})["deny_path_count"] is None
        # A row with that many real paths is capped the same way.
        many = sanitize_graph_facts_row({"deny_paths": [["x"]] * (MAX_DENY_PATH_COUNT + 1)})
        assert many["deny_path_count"] is None

    @pytest.mark.parametrize("later", [0.0, None, -1.0, "nope"])
    def test_a_repeat_row_never_deletes_an_existing_penalty(self, later):
        """Inherited semantics: the pre-#469 parser assigned on a positive
        score and skipped otherwise, so a repeat row could not erase a
        demotion. A candidate must not lose its penalty — and its cohort
        stamp — to a duplicate the graph should not have sent.
        """
        v = {
            "features": [
                {"candidate": "s::a", "risk_score": 0.4},
                {"candidate": "s::a", "risk_score": later},
            ]
        }
        assert parse_risk_scores(v) == {"s::a": 0.4}
        # The FACTS still follow the last row — they describe that row.
        expected = later if isinstance(later, float) else None
        assert parse_graph_facts(v)["s::a"]["risk_score"] == expected

    def test_a_later_positive_row_wins(self):
        v = {
            "features": [
                {"candidate": "s::a", "risk_score": 0.4},
                {"candidate": "s::a", "risk_score": 0.9},
            ]
        }
        assert parse_risk_scores(v) == {"s::a": 0.9}


class TestParseGraphFacts:
    def test_keeps_clean_and_unresolved_rows(self):
        """The gap this closes: the sparse penalty map renders both as absent.

        A ranker trained on these needs "the graph looked and found nothing
        wrong" (0.0) to be a different example from "the graph could not look"
        (None).
        """
        v = {
            "agent_found": True,
            "features": [
                {"candidate": "s::clean", "found": True, "risk_score": 0.0},
                {"candidate": "s::risky", "found": True, "risk_score": 0.4},
                {"candidate": "s::unresolved", "found": False, "risk_score": None},
            ],
        }
        facts = parse_graph_facts(v)
        assert set(facts) == {"s::clean", "s::risky", "s::unresolved"}
        assert facts["s::clean"]["risk_score"] == 0.0
        assert facts["s::unresolved"]["risk_score"] is None
        assert facts["s::unresolved"]["found"] is False
        # And the same payload still yields the sparse penalty map.
        assert parse_risk_scores(v) == {"s::risky": 0.4}

    def test_lenient_on_malformed_payload(self):
        assert parse_graph_facts({}) == {}
        assert parse_graph_facts({"features": "nope"}) == {}
        assert parse_graph_facts({"features": [None, 42, "x"]}) == {}
        assert parse_graph_facts({"features": [{"risk_score": 0.4}]}) == {}
        assert parse_graph_facts({"features": [{"candidate": 5}]}) == {}

    def test_last_row_wins_on_a_duplicated_ref(self):
        v = {
            "features": [
                {"candidate": "s::a", "risk_score": 0.4},
                {"candidate": "s::a", "risk_score": 1.0},
            ]
        }
        assert parse_graph_facts(v)["s::a"]["risk_score"] == 1.0
        assert parse_risk_scores(v) == {"s::a": 1.0}

    def test_both_views_come_from_one_traversal(self):
        """One response, one traversal — the two views cannot disagree.

        Asserted against the pair-returning parser the consult actually calls,
        not by comparing two separate parses: equal outputs from two walks
        would prove agreement on this input, not that there is one walk.
        """
        v = {
            "features": [
                {"candidate": "s::a", "risk_score": 0.4},
                {"candidate": "s::b", "risk_score": True},  # bool → not a score
                {"candidate": "s::c", "risk_score": 0.0},
            ]
        }
        facts, scores = parse_graph_features(v)
        assert scores == {"s::a": 0.4}
        assert set(facts) == {"s::a", "s::b", "s::c"}
        assert facts["s::b"]["risk_score"] is None
        # The single-product wrappers are that pair, projected.
        assert parse_graph_facts(v) == facts
        assert parse_risk_scores(v) == scores


# ── filter_tools external_rejects + withhold_all (#465) ─────────────────────


class TestExternalRejects:
    def _ext(self, profile, code="toolgraph_not_granted"):
        cfg = _server_cfg()
        return filter_tools(
            [_cand("a", cfg), _cand("b", cfg)],
            profile,
            external_rejects={("srv", "a"): code},
        )

    def test_strict_rejects_with_the_code(self):
        result = self._ext(STRICT)
        assert _names(result) == ["test__b"]
        assert result.reject_reasons == {"test__a": REASON_TOOLGRAPH_NOT_GRANTED}

    def test_review_demotes_not_rejects(self):
        result = self._ext(REVIEW)
        assert _names(result) == ["test__a", "test__b"]
        assert result.reject_reasons == {}
        assert result.risk_penalties["test__a"] == REVIEW.review_risk_penalty

    def test_explore_ignores(self):
        result = self._ext(EXPLORE)
        assert _names(result) == ["test__a", "test__b"]
        assert result.reject_reasons == {}
        assert result.risk_penalties == {}

    def test_external_outranks_unhealthy(self):
        # A tool flagged by BOTH the graph and the health heuristic records the
        # explicit graph code, not the heuristic one.
        cfg = _server_cfg()
        result = filter_tools(
            [_cand("a", cfg)],
            STRICT,
            unhealthy=frozenset({("srv", "a")}),
            external_rejects={("srv", "a"): REASON_TOOLGRAPH_NOT_GRANTED},
        )
        assert result.reject_reasons == {"test__a": REASON_TOOLGRAPH_NOT_GRANTED}

    def test_sensitive_outranks_external(self):
        # Credential-in-metadata (upstream compromise) outranks a graph verdict.
        cfg = _server_cfg()
        cand = _cand("a", cfg, raw_desc="token: ghp_" + "x" * 36)
        result = filter_tools(
            [cand], STRICT, external_rejects={("srv", "a"): REASON_TOOLGRAPH_NOT_GRANTED}
        )
        assert result.reject_reasons == {"test__a": REASON_SENSITIVE_METADATA}


class TestWithholdAll:
    def test_withholds_every_tool_with_one_code(self):
        cfg = _server_cfg()
        result = filter_tools(
            [_cand("a", cfg), _cand("b", cfg)],
            STRICT,
            withhold_all=REASON_TOOLGRAPH_UNREACHABLE,
        )
        assert result.eligible == []
        assert result.reject_reasons == {
            "test__a": REASON_TOOLGRAPH_UNREACHABLE,
            "test__b": REASON_TOOLGRAPH_UNREACHABLE,
        }
        assert result.risk_penalties == {}

    def test_withhold_all_is_profile_independent(self):
        # Even explore (which skips signal rules) honors a closed-knob withhold.
        cfg = _server_cfg()
        result = filter_tools([_cand("a", cfg)], EXPLORE, withhold_all=REASON_TOOLGRAPH_UNREACHABLE)
        assert result.eligible == []
        assert result.reject_reasons == {"test__a": REASON_TOOLGRAPH_UNREACHABLE}
