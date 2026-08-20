"""Tests for the optional tool-graph eligibility provider (#465).

Covers the ``toolgraph`` config block (defaults / failure-knob defaults /
file parse) and the connectable ``ToolgraphConsultAdapter`` (stdio lifecycle
+ ``eligible_tools`` consult against a real fake stdio child). The
``TestToolgraphConsultWiring`` section drives the startup consult END-TO-END
through ``ProxyManager`` against that same fake stdio child: per-candidate
rejects flow into the exposure filter and selection telemetry, the four
``on_*`` failure knobs each map correctly (fail_start raises out of start() /
open degrades / closed withholds all), ``graph_generation`` is pinned into the
log, and the server-name-mismatch heuristic fires.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ExposureConfig,
    ExposureProfile,
    ProxyConfig,
    ToolgraphConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import (
    ProxyManager,
    ToolgraphStartupError,
    UpstreamConnection,
)
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.selection_log import SelectionTelemetryLog
from memtomem_stm.proxy.tool_eligibility import (
    GRAPH_FACT_KEYS,
    REASON_TOOLGRAPH_AGENT_NOT_FOUND,
    REASON_TOOLGRAPH_NOT_GRANTED,
    REASON_TOOLGRAPH_PROTOCOL_ERROR,
    REASON_TOOLGRAPH_TOOL_NOT_FOUND,
    REASON_TOOLGRAPH_UNREACHABLE,
)
from memtomem_stm.proxy.tool_relevance import (
    PENALTY_SOURCE_BOTH,
    PENALTY_SOURCE_GRAPH,
    RANKER_VERSION_BM25_GRAPH_RISK,
)
from memtomem_stm.server import _toolgraph_health_lines
from memtomem_stm.proxy.toolgraph_cache import GraphConsultCache
from memtomem_stm.proxy.toolgraph_provider import (
    ToolgraphConsultAdapter,
    ToolgraphProtocolError,
    ToolgraphUnreachableError,
    _parse_text_verdict,
)

# Real stdio MCP child (mirrors tests/_fake_memtomem_server.py usage).
_FAKE_TOOLGRAPH = Path(__file__).resolve().parent / "_fake_toolgraph_server.py"


def _adapter(**overrides) -> ToolgraphConsultAdapter:
    """Adapter pointed at the fake stdio toolgraph server."""
    cfg = ToolgraphConfig(
        enabled=True,
        command=sys.executable,
        args=[str(_FAKE_TOOLGRAPH)],
        **overrides,
    )
    return ToolgraphConsultAdapter(cfg)


# ── config block ──────────────────────────────────────────────────────────


class TestToolgraphConfig:
    def test_toolgraph_config_parse_defaults_off(self):
        tg = ProxyConfig().toolgraph
        assert tg.enabled is False  # default-off
        assert tg.command == "toolgraph"
        assert tg.args == ["serve"]
        assert tg.env is None
        assert tg.agent_id == "stm-proxy"
        assert tg.server_name_map == {}
        assert tg.query_profile == "strict"
        assert tg.risk_penalty_scale == 1.0
        assert tg.timeout_seconds == 5.0
        assert tg.consult_cache_enabled is True  # #494: strictly-fresh, on by default
        assert tg.consult_cache_path == Path("~/.memtomem/toolgraph_consult.db")
        assert tg.consult_cache_max_scopes == 64  # matches GraphConsultCache ctor default

    def test_consult_cache_max_scopes_must_be_positive(self):
        # 0 would make _trim delete every row on each write (count > 0 always
        # true with the just-inserted row), defeating the cache.
        with pytest.raises(ValidationError):
            ToolgraphConfig(consult_cache_max_scopes=0)
        with pytest.raises(ValidationError):
            ToolgraphConfig(consult_cache_max_scopes=-1)
        ToolgraphConfig(consult_cache_max_scopes=1)  # minimum valid

    @pytest.mark.parametrize(
        "value", [float("nan"), float("inf"), float("-inf"), 10**400, -(10**400)]
    )
    def test_risk_penalty_scale_must_be_finite(self, value):
        # `ge=0` alone admits `inf`, which multiplies into a penalty and then
        # into a report that strict JSON refuses to serialize (#856).
        with pytest.raises(ValidationError):
            ToolgraphConfig(risk_penalty_scale=value)

    def test_risk_penalty_scale_still_accepts_ordinary_values(self):
        assert ToolgraphConfig(risk_penalty_scale=0.0).risk_penalty_scale == 0.0
        assert ToolgraphConfig(risk_penalty_scale=2.5).risk_penalty_scale == 2.5

    def test_failure_knob_defaults(self):
        tg = ProxyConfig().toolgraph
        # whole-call classes that silently disabling enforcement would be a
        # security footgun fail startup loudly by default; availability /
        # blind-spot classes stay open.
        assert tg.on_agent_not_found == "fail_start"
        assert tg.on_protocol_error == "fail_start"
        assert tg.on_unreachable == "open"
        assert tg.on_tool_not_found == "open"

    def test_block_parses_from_config_file(self, tmp_path):
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "toolgraph": {
                        "enabled": True,
                        "agent_id": "fleet-1",
                        "server_name_map": {"gh": "github"},
                        "on_unreachable": "closed",
                        "risk_penalty_scale": 0.5,
                    },
                }
            )
        )
        config = ProxyConfig.load_from_file(cfg_file)
        assert config is not None
        tg = config.toolgraph
        assert tg.enabled is True
        assert tg.agent_id == "fleet-1"
        assert tg.server_name_map == {"gh": "github"}
        assert tg.on_unreachable == "closed"
        assert tg.risk_penalty_scale == 0.5
        # unset knobs keep their defaults
        assert tg.on_agent_not_found == "fail_start"

    @pytest.mark.parametrize(
        "field",
        ["on_unreachable", "on_agent_not_found", "on_protocol_error", "on_tool_not_found"],
    )
    def test_invalid_failure_knob_rejected(self, field):
        with pytest.raises(ValidationError):
            ProxyConfig.model_validate({"toolgraph": {field: "bogus"}})


# ── adapter lifecycle + consult (real stdio child) ──────────────────────────


class TestToolgraphConsultAdapter:
    async def test_start_stop_lifecycle(self):
        adapter = _adapter()
        assert adapter.is_started is False
        await adapter.start()
        try:
            assert adapter.is_started is True
        finally:
            await adapter.stop()
        assert adapter.is_started is False

    async def test_eligible_tools_returns_structured_verdict(self):
        adapter = _adapter()
        await adapter.start()
        try:
            verdict = await adapter.eligible_tools(
                ["s::a", "s::blocked", "s::b"], agent="planner", profile="strict"
            )
        finally:
            await adapter.stop()
        assert verdict["agent"] == "planner"
        assert verdict["agent_found"] is True
        assert verdict["profile"] == "strict"
        # input order preserved; blocked candidate routed to rejected
        assert verdict["eligible"] == ["s::a", "s::b"]
        assert verdict["rejected"] == [
            {"candidate": "s::blocked", "tool_key": "s::blocked", "reason": "NOT_GRANTED"}
        ]
        assert verdict["graph_generation"] == 11

    async def test_eligible_tools_uses_config_agent_and_profile_defaults(self):
        adapter = _adapter(agent_id="default-agent", query_profile="review")
        await adapter.start()
        try:
            verdict = await adapter.eligible_tools(["s::a"])
        finally:
            await adapter.stop()
        assert verdict["agent"] == "default-agent"
        assert verdict["profile"] == "review"

    async def test_agent_not_found_is_structured_not_error(self):
        adapter = _adapter()
        await adapter.start()
        try:
            verdict = await adapter.eligible_tools(["s::a"], agent="ghost")
        finally:
            await adapter.stop()
        # agent_found=false is an abort SIGNAL carried as data, never an error
        assert verdict["agent_found"] is False
        assert verdict["eligible"] == []
        assert verdict["graph_generation"] == 11

    async def test_protocol_error_on_tool_error_result(self):
        adapter = _adapter()
        await adapter.start()
        try:
            with pytest.raises(ToolgraphProtocolError):
                await adapter.eligible_tools(["s::a"], profile="boom")
        finally:
            await adapter.stop()

    async def test_typed_backend_unavailable_is_availability_not_protocol(self):
        adapter = _adapter()
        await adapter.start()
        try:
            with pytest.raises(ToolgraphUnreachableError):
                await adapter.eligible_tools(["s::a"], profile="backend_unavailable")
        finally:
            await adapter.stop()

    @pytest.mark.parametrize(
        "structured",
        [
            None,
            {},
            {"error_kind": "future_error", "retryable": True},
            {"error_kind": "backend_unavailable", "retryable": False},
            {"error_kind": "backend_unavailable"},
        ],
    )
    async def test_untyped_unknown_or_malformed_error_stays_protocol(self, structured):
        adapter = _adapter()
        session = AsyncMock()
        session.call_tool.return_value = SimpleNamespace(
            is_error=True,
            structured_content=structured,
            content=[
                SimpleNamespace(
                    type="text",
                    text="backend_unavailable retry later misleading text",
                )
            ],
        )
        adapter._session = session

        with pytest.raises(ToolgraphProtocolError):
            await adapter.eligible_tools(["s::a"])

    async def test_typed_kind_wins_without_message_sniffing(self):
        adapter = _adapter()
        session = AsyncMock()
        session.call_tool.return_value = SimpleNamespace(
            is_error=True,
            structured_content={
                "error_kind": "backend_unavailable",
                "retryable": True,
                "message": "unknown profile contract failure",
            },
            content=[SimpleNamespace(type="text", text="unknown profile")],
        )
        adapter._session = session

        with pytest.raises(ToolgraphUnreachableError):
            await adapter.eligible_tools(["s::a"])

    async def test_eligible_tools_before_start_raises_unreachable(self):
        adapter = _adapter()
        with pytest.raises(ToolgraphUnreachableError):
            await adapter.eligible_tools(["s::a"])

    async def test_rank_features_returns_per_candidate_risk_scores(self):
        adapter = _adapter()
        await adapter.start()
        try:
            verdict = await adapter.rank_features(
                ["s::a", "s::risky_tool", "s::missing_x"], agent="planner"
            )
        finally:
            await adapter.stop()
        assert verdict["agent"] == "planner"
        assert verdict["agent_found"] is True
        # input order preserved; risk_score per the fixed table
        assert [f["candidate"] for f in verdict["features"]] == [
            "s::a",
            "s::risky_tool",
            "s::missing_x",
        ]
        assert [f["risk_score"] for f in verdict["features"]] == [0.0, 0.4, None]
        assert verdict["graph_generation"] == 11

    async def test_rank_features_uses_config_agent_default(self):
        adapter = _adapter(agent_id="default-agent")
        await adapter.start()
        try:
            verdict = await adapter.rank_features(["s::a"])
        finally:
            await adapter.stop()
        assert verdict["agent"] == "default-agent"

    async def test_rank_features_before_start_raises_unreachable(self):
        adapter = _adapter()
        with pytest.raises(ToolgraphUnreachableError):
            await adapter.rank_features(["s::a"])

    async def test_timeout_raises_unreachable(self):
        adapter = _adapter(timeout_seconds=0.3)
        await adapter.start()
        try:
            with pytest.raises(ToolgraphUnreachableError):
                await adapter.eligible_tools(["s::a"], profile="sleep")
        finally:
            await adapter.stop()

    async def test_start_failure_rolls_back_and_not_started(self):
        adapter = ToolgraphConsultAdapter(
            ToolgraphConfig(enabled=True, command="this-binary-does-not-exist-xyz", args=[])
        )
        with pytest.raises(OSError):  # spawn failure: FileNotFoundError
            await adapter.start()
        assert adapter.is_started is False


class TestToolgraphIdentifierBoundary:
    @pytest.mark.parametrize("surrogate", ["\ud800", "\udbff", "\udc00", "\udfff"])
    @pytest.mark.parametrize("field", ["agent", "profile", "candidate"])
    async def test_request_identifier_is_refused_without_value_leak(self, field, surrogate):
        adapter = _adapter()
        kwargs = {"agent": "agent", "profile": "strict"}
        candidates = ["s::tool"]
        if field == "candidate":
            candidates = [f"s::tool{surrogate}"]
        else:
            kwargs[field] += surrogate
        with pytest.raises(ToolgraphProtocolError) as exc_info:
            await adapter.eligible_tools(candidates, **kwargs)
        assert surrogate not in str(exc_info.value)
        assert "UTF-8-encodable identifier" in str(exc_info.value)

    @pytest.mark.parametrize("wire_path", ["structured", "text"])
    @pytest.mark.parametrize(
        ("field_path", "mutate"),
        [
            ("agent", lambda verdict, value: verdict.__setitem__("agent", value)),
            ("profile", lambda verdict, value: verdict.__setitem__("profile", value)),
            ("eligible", lambda verdict, value: verdict["eligible"].append(value)),
            (
                "candidate",
                lambda verdict, value: verdict["rejected"][0].__setitem__("candidate", value),
            ),
            (
                "tool_key",
                lambda verdict, value: verdict["rejected"][0].__setitem__("tool_key", value),
            ),
            (
                "candidates",
                lambda verdict, value: verdict["rejected"][0].__setitem__("candidates", [value]),
            ),
        ],
    )
    async def test_response_identifier_is_protocol_error_on_both_wire_paths(
        self, wire_path, field_path, mutate
    ):
        adapter = _adapter()
        verdict = {
            "agent": "agent",
            "profile": "strict",
            "eligible": [],
            "rejected": [
                {
                    "candidate": "s::tool",
                    "tool_key": "s::tool",
                    "reason": "NOT_GRANTED",
                }
            ],
        }
        mutate(verdict, "bad\ud800")
        result = SimpleNamespace(
            is_error=False,
            structured_content=verdict if wire_path == "structured" else None,
            content=(
                []
                if wire_path == "structured"
                else [SimpleNamespace(type="text", text=json.dumps(verdict))]
            ),
        )
        session = AsyncMock()
        session.call_tool.return_value = result
        adapter._session = session

        with pytest.raises(ToolgraphProtocolError) as exc_info:
            await adapter.eligible_tools(["s::tool"])
        assert "\ud800" not in str(exc_info.value)
        assert field_path in str(exc_info.value)

    async def test_literal_surrogate_text_is_not_refused_or_rewritten(self):
        literal = r"s::tool\ud800"
        verdict = {
            "agent": "agent",
            "profile": "strict",
            "eligible": [literal],
            "rejected": [],
        }
        session = AsyncMock()
        session.call_tool.return_value = SimpleNamespace(
            is_error=False,
            structured_content=verdict,
            content=[],
        )
        adapter = _adapter()
        adapter._session = session
        assert await adapter.eligible_tools([literal]) is verdict

    @pytest.mark.parametrize("field", ["candidate", "tool_key", "candidates"])
    async def test_rank_feature_response_identifiers_are_refused(self, field):
        row = {"candidate": "s::tool", "tool_key": "s::tool", "risk_score": 0.5}
        row[field] = ["bad\udfff"] if field == "candidates" else "bad\udfff"
        session = AsyncMock()
        session.call_tool.return_value = SimpleNamespace(
            is_error=False,
            structured_content={"agent": "agent", "features": [row]},
            content=[],
        )
        adapter = _adapter()
        adapter._session = session
        with pytest.raises(ToolgraphProtocolError):
            await adapter.rank_features(["s::tool"])


# ── manager startup: the provider is consulted, no longer inert ──────────────


class TestToolgraphStartupWiring:
    async def test_enabled_no_upstreams_skips_consult_no_inert_warning(self, tmp_path, caplog):
        # The startup consult replaced the earlier "enabled but inert" warning.
        # With no upstream tools there is nothing to consult, so the consult is
        # skipped (INFO) and the old inert WARNING must be gone.
        cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={},
            toolgraph=ToolgraphConfig(enabled=True),
        )
        mgr = ProxyManager(cfg, TokenTracker())
        with caplog.at_level(logging.INFO, logger="memtomem_stm.proxy.manager"):
            await mgr.start()
        try:
            assert not any("inert" in r.message for r in caplog.records)
            assert any("consult skipped" in r.message for r in caplog.records)
            status = mgr.get_toolgraph_status()
            assert status == {
                "enabled": True,
                "degraded": False,
                "degraded_reason": None,
                "withholding_all": None,
                "graph_generation": None,
                "from_cache": False,
                "external_reject_count": 0,
                "risk_penalty_count": 0,
                "graph_facts_count": 0,
            }
        finally:
            await mgr.stop()

    async def test_disabled_status_is_none_and_no_warning(self, tmp_path, caplog):
        cfg = ProxyConfig(config_path=tmp_path / "proxy.json", upstream_servers={})
        mgr = ProxyManager(cfg, TokenTracker())
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            await mgr.start()
        try:
            assert not any("toolgraph" in r.message.lower() for r in caplog.records)
            assert mgr.get_toolgraph_status() is None
        finally:
            await mgr.stop()


# ── text-JSON verdict parsing (the production wire path on mcp 1.27.x) ───────


def _text_result(*texts: str | None):
    """A minimal CallToolResult stand-in carrying text content parts."""
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=t) for t in texts])


class TestParseTextVerdict:
    def test_parses_json_dict(self):
        verdict = _parse_text_verdict(_text_result('{"agent": "a", "eligible": []}'))
        assert verdict == {"agent": "a", "eligible": []}

    def test_joins_multi_part_text(self):
        verdict = _parse_text_verdict(_text_result('{"agent":', ' "a"}'))
        assert verdict == {"agent": "a"}

    def test_malformed_json_returns_none(self):
        assert _parse_text_verdict(_text_result("not json")) is None

    def test_non_dict_json_returns_none(self):
        assert _parse_text_verdict(_text_result("[1, 2, 3]")) is None

    def test_empty_content_returns_none(self):
        assert _parse_text_verdict(SimpleNamespace(content=[])) is None

    def test_none_content_returns_none(self):
        # spec-noncompliant upstream returning content=None
        assert _parse_text_verdict(SimpleNamespace(content=None)) is None

    def test_none_text_part_returns_none(self):
        # TextContent.text=None degrades to "" → unparseable → None
        assert _parse_text_verdict(_text_result(None)) is None


# ── startup consult wired through ProxyManager (real fake stdio child) ───────


def _tool_result(text: str = "ok"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], is_error=False)


def _tg_manager(tmp_path, *, servers=None, exposure=None, **tg_overrides):
    """ProxyManager with upstream(s) wired + toolgraph pointed at the fake child.

    Connections are wired directly (no real upstream stdio) so the test
    exercises the toolgraph consult path in isolation; the caller then runs
    ``await mgr._consult_toolgraph()`` and ``mgr.get_proxy_tools()`` — or
    ``await mgr.start()`` to drive the full startup path (the empty-command
    upstream fails-and-logs in start()'s connect loop, leaving the pre-wired
    mock connection in place for the consult).
    """
    servers = servers or {"srv": ["read_file", "blocked"]}
    log = SelectionTelemetryLog(tmp_path / "log.jsonl")
    log.initialize()
    upstream_cfgs = {
        name: UpstreamServerConfig(
            prefix=name,
            compression=CompressionStrategy.NONE,
            max_retries=0,
            reconnect_delay_seconds=0.0,
        )
        for name in servers
    }
    tg_kwargs: dict = {
        "command": sys.executable,
        "args": [str(_FAKE_TOOLGRAPH)],
        # Isolate the #494 consult cache per-test (the production default points
        # at the real ~/.memtomem). Callers override for cross-restart / disabled
        # cache scenarios.
        "consult_cache_path": tmp_path / "tg_consult.db",
    }
    tg_kwargs.update(tg_overrides)  # callers may override command/args (unreachable tests)
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers=upstream_cfgs,
        exposure=exposure or ExposureConfig(),
        toolgraph=ToolgraphConfig(enabled=True, **tg_kwargs),
    )
    mgr = ProxyManager(proxy_cfg, TokenTracker(), selection_log=log)
    for name, tool_names in servers.items():
        tools = [
            SimpleNamespace(name=n, description=f"{n} description", input_schema={"type": "object"})
            for n in tool_names
        ]
        session = AsyncMock()
        session.call_tool.return_value = _tool_result()
        mgr._connections[name] = UpstreamConnection(
            name=name, config=upstream_cfgs[name], session=session, tools=tools
        )
    return mgr, log


def _events(log: SelectionTelemetryLog) -> list[dict]:
    return [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines() if line]


class TestConsultCacheMaxScopesWiring:
    def test_open_consult_cache_threads_config_max_scopes(self, tmp_path):
        # Proves manager.py:_open_consult_cache passes the config value into the
        # GraphConsultCache ctor rather than relying on the ctor's = 64 default.
        mgr, _ = _tg_manager(tmp_path, consult_cache_max_scopes=7)
        cache = mgr._open_consult_cache(mgr._config.toolgraph)
        assert cache is not None
        assert cache._max_scopes == 7

    def test_open_consult_cache_default_max_scopes(self, tmp_path):
        mgr, _ = _tg_manager(tmp_path)
        cache = mgr._open_consult_cache(mgr._config.toolgraph)
        assert cache is not None
        assert cache._max_scopes == 64


class TestToolgraphConsultWiring:
    @pytest.mark.parametrize("consult_cache_enabled", [False, True])
    async def test_unencodable_local_ref_is_a_protocol_error_cold_or_empty_cache(
        self, tmp_path, consult_cache_enabled
    ):
        """Cold path only — with the cache enabled this still MISSES.

        Deliberately not evidence for the manager's pre-cache validation: on a
        miss the eventual full ``eligible_tools(refs)`` performs the same check
        in the adapter, so this passes either way. The warm-hit case below is
        what pins the manager copy.
        """
        mgr, _ = _tg_manager(
            tmp_path,
            servers={"srv": ["bad\ud800"]},
            on_protocol_error="open",
            consult_cache_enabled=consult_cache_enabled,
        )
        try:
            await mgr._consult_toolgraph()
            status = mgr.get_toolgraph_status()
            assert status["degraded"] is True
            assert status["degraded_reason"] == REASON_TOOLGRAPH_PROTOCOL_ERROR
            assert status["from_cache"] is False
            assert status["external_reject_count"] == 0
        finally:
            await mgr.stop()

    async def test_unencodable_local_ref_is_refused_on_a_warm_cache_hit(self, tmp_path):
        """A seeded, matching row must NOT let the refusal be skipped.

        Without the pre-cache validation in ``_run_consult`` the probe succeeds,
        the scope hits, and STM comes up clean and un-degraded serving cached
        rejects for a candidate set it cannot encode — while the same config
        with the cache disabled raises. That divergence is the whole reason the
        manager validates before the cache branch, and it is invisible to any
        test that only ever misses.
        """
        bad_ref = "srv::bad\ud800"
        mgr, _ = _tg_manager(
            tmp_path,
            servers={"srv": ["bad\ud800"]},
            on_protocol_error="open",
        )
        cfg = mgr._config.toolgraph
        cache = GraphConsultCache(tmp_path / "tg_consult.db")
        cache.initialize()
        try:
            cache.put(
                GraphConsultCache.provider_fingerprint(cfg),
                cfg.agent_id,
                cfg.query_profile,
                GraphConsultCache.candidate_hash([bad_ref]),
                11,  # the fake graph's generation, as the sibling tests assert
                rejects={},
                tool_not_found_refs=[],
                graph_facts={},
                risk_scores={},
                had_risk_scores=True,
            )
        finally:
            cache.close()
        seeded = GraphConsultCache(tmp_path / "tg_consult.db")
        seeded.initialize()
        try:
            # Positive control: the row really is a hit for this exact scope, so
            # a passing assertion below cannot be an accidental miss.
            assert (
                seeded.get(
                    GraphConsultCache.provider_fingerprint(cfg),
                    cfg.agent_id,
                    cfg.query_profile,
                    GraphConsultCache.candidate_hash([bad_ref]),
                    11,
                )
                is not None
            )
        finally:
            seeded.close()

        try:
            await mgr._consult_toolgraph()
            status = mgr.get_toolgraph_status()
            assert status["degraded"] is True
            assert status["degraded_reason"] == REASON_TOOLGRAPH_PROTOCOL_ERROR
            assert status["from_cache"] is False
        finally:
            await mgr.stop()

    async def test_success_rejects_flow_into_exposure_and_telemetry(self, tmp_path):
        mgr, log = _tg_manager(tmp_path)  # tools: read_file, blocked
        await mgr._consult_toolgraph()
        # "srv::blocked" → NOT_GRANTED → withheld under strict.
        assert mgr._toolgraph_external_rejects == {("srv", "blocked"): REASON_TOOLGRAPH_NOT_GRANTED}
        assert mgr._graph_generation == 11
        advertised = [i.prefixed_name for i in mgr.get_proxy_tools()]
        assert advertised == ["srv__read_file"]
        assert mgr._advertised_reject_reasons == {"srv__blocked": REASON_TOOLGRAPH_NOT_GRANTED}

        await mgr.call_tool("srv", "read_file", {})
        selection, _execution = _events(log)
        assert selection["graph_generation"] == 11
        assert selection["reject_reasons"] == {"srv__blocked": REASON_TOOLGRAPH_NOT_GRANTED}

        status = mgr.get_toolgraph_status()
        assert status["degraded"] is False
        assert status["withholding_all"] is None
        assert status["external_reject_count"] == 1

    async def test_review_demotes_external_reject(self, tmp_path):
        mgr, _ = _tg_manager(tmp_path, exposure=ExposureConfig(profile=ExposureProfile.REVIEW))
        await mgr._consult_toolgraph()
        advertised = [i.prefixed_name for i in mgr.get_proxy_tools()]
        assert advertised == ["srv__read_file", "srv__blocked"]  # still advertised
        assert mgr._advertised_reject_reasons == {}
        assert mgr._advertised_risk_penalties["srv__blocked"] > 0

    async def test_explore_ignores_external_reject(self, tmp_path):
        mgr, _ = _tg_manager(tmp_path, exposure=ExposureConfig(profile=ExposureProfile.EXPLORE))
        await mgr._consult_toolgraph()
        advertised = [i.prefixed_name for i in mgr.get_proxy_tools()]
        assert advertised == ["srv__read_file", "srv__blocked"]
        assert mgr._advertised_reject_reasons == {}
        assert mgr._advertised_risk_penalties == {}

    # ── #493 graph risk_score → relevance risk_penalty ──────────────────────

    async def test_risky_tool_earns_graph_risk_penalty(self, tmp_path):
        # An eligible (not rejected) tool with a positive risk_score is demoted
        # in ranking telemetry — exposure is unchanged (still advertised).
        mgr, _ = _tg_manager(tmp_path, servers={"srv": ["read_file", "risky_tool"]})
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_risk_penalties == {("srv", "risky_tool"): 0.4}
        advertised = [i.prefixed_name for i in mgr.get_proxy_tools()]
        assert advertised == ["srv__read_file", "srv__risky_tool"]  # both advertised
        assert mgr._advertised_reject_reasons == {}
        # default scale 1.0 → penalty == risk_score, source is graph-only.
        assert mgr._advertised_risk_penalties == {"srv__risky_tool": 0.4}
        assert mgr._advertised_risk_penalty_sources == {"srv__risky_tool": PENALTY_SOURCE_GRAPH}
        assert mgr.get_toolgraph_status()["risk_penalty_count"] == 1

    async def test_risk_penalty_scale_scales_the_score(self, tmp_path):
        mgr, _ = _tg_manager(tmp_path, servers={"srv": ["risky_tool"]}, risk_penalty_scale=0.5)
        await mgr._consult_toolgraph()
        # 0.4 * 0.5 = 0.2
        assert mgr._toolgraph_risk_penalties == {("srv", "risky_tool"): 0.2}
        mgr.get_proxy_tools()
        assert mgr._advertised_risk_penalties == {"srv__risky_tool": 0.2}

    async def test_risk_penalty_scale_zero_disables_the_signal(self, tmp_path):
        # scale 0 zeroes every penalty. Since #469 the rank_features consult
        # still RUNS when a selection log exists (the facts are a second
        # product of that call), so this pins the penalty half specifically:
        # the map stays empty rather than filling with 0.0 entries that the
        # status count would then report as demotions.
        mgr, _ = _tg_manager(tmp_path, servers={"srv": ["risky_tool"]}, risk_penalty_scale=0.0)
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_risk_penalties == {}
        mgr.get_proxy_tools()
        assert mgr._advertised_risk_penalties == {}
        assert mgr.get_toolgraph_status()["risk_penalty_count"] == 0
        # Positive control for the pair below: the facts DID arrive, so the
        # empty penalty map above is the scale doing its job and not the
        # enrichment having been skipped.
        assert mgr._advertised_graph_facts["srv__risky_tool"]["risk_score"] == 0.4

    async def test_scale_zero_without_a_selection_log_skips_the_enrichment(self, tmp_path):
        """Neither consumer wants it → the extra consult must not happen (#469).

        The gate is "either the penalty scale or the feature sink", so with
        both off the ``rank_features`` call is still skipped — which is what
        keeps #469 from imposing a second startup round-trip on operators who
        run the graph with telemetry disabled.
        """
        mgr, _ = _tg_manager(tmp_path, servers={"srv": ["risky_tool"]}, risk_penalty_scale=0.0)
        mgr._selection_log = None
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_graph_facts == {}
        assert mgr._toolgraph_risk_penalties == {}
        # The eligibility verdict itself is unaffected — only the enrichment
        # was skipped.
        assert [i.prefixed_name for i in mgr.get_proxy_tools()] == ["srv__risky_tool"]

    async def test_review_reject_and_graph_risk_compose_to_both(self, tmp_path):
        # "riskyblocked" is rejected by eligible_tools (→ native review demote)
        # AND scored 0.4 by rank_features → under review the two stack via the
        # complement-product, tagged review+graph.
        mgr, _ = _tg_manager(
            tmp_path,
            servers={"srv": ["riskyblocked_tool"]},
            exposure=ExposureConfig(profile=ExposureProfile.REVIEW),
        )
        await mgr._consult_toolgraph()
        advertised = [i.prefixed_name for i in mgr.get_proxy_tools()]
        assert advertised == ["srv__riskyblocked_tool"]  # advertised under review
        # 1 - (1 - 0.5)(1 - 0.4) = 0.7  (review_risk_penalty default 0.5)
        assert mgr._advertised_risk_penalties["srv__riskyblocked_tool"] == 0.7
        assert mgr._advertised_risk_penalty_sources["srv__riskyblocked_tool"] == PENALTY_SOURCE_BOTH

    async def test_graph_facts_reach_the_selection_record(self, tmp_path):
        """The #469 payload: per-candidate facts on every ELIGIBLE candidate.

        ``risky_tool`` scores 0.4 and ``read_file`` scores 0.0 — the clean row
        the sparse penalty map drops. Both must be present and distinguishable,
        because "the graph looked and found nothing wrong" is the negative
        example a ranker trains on.
        """
        mgr, log = _tg_manager(tmp_path, servers={"srv": ["read_file", "risky_tool"]})
        await mgr._consult_toolgraph()
        mgr.get_proxy_tools()
        await mgr.call_tool("srv", "risky_tool", {"task": "do the risky thing now"})
        selection, _ = _events(log)
        ranked = {
            r["tool"]: r["graph_facts"]
            for r in selection["candidate_features"]["ranked_candidates"]
        }
        assert ranked["srv__risky_tool"]["risk_score"] == 0.4
        assert ranked["srv__risky_tool"]["verdict"] == "ALLOW"
        assert ranked["srv__risky_tool"]["found"] is True
        # The clean candidate: a recorded 0.0, not an absent row.
        assert ranked["srv__read_file"]["risk_score"] == 0.0
        assert ranked["srv__read_file"]["is_drifted"] is False
        # Graph-authored identifiers and evidence paths never ride along.
        assert set(ranked["srv__read_file"]) == set(GRAPH_FACT_KEYS)

    async def test_rank_features_failure_degrades_to_no_facts(self, tmp_path):
        """A failed enrichment records ``None``, not a fabricated clean row."""
        mgr, log = _tg_manager(
            tmp_path, servers={"srv": ["read_file", "risky_tool"]}, agent_id="rankboom"
        )
        await mgr._consult_toolgraph()
        mgr.get_proxy_tools()
        await mgr.call_tool("srv", "risky_tool", {"task": "do the risky thing now"})
        selection, _ = _events(log)
        for entry in selection["candidate_features"]["ranked_candidates"]:
            assert entry["graph_facts"] is None

    async def test_rank_features_failure_degrades_to_no_penalties(self, tmp_path, caplog):
        # rank_features fails (agent rankboom raises) but eligible_tools for the
        # same agent succeeds → exposure verdict intact, risk penalties empty,
        # startup NOT aborted, and the skip is logged loudly.
        mgr, _ = _tg_manager(
            tmp_path, servers={"srv": ["read_file", "risky_tool"]}, agent_id="rankboom"
        )
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            await mgr._consult_toolgraph()
        assert mgr._toolgraph_risk_penalties == {}
        # eligible_tools verdict is unaffected (read_file + risky_tool eligible).
        advertised = [i.prefixed_name for i in mgr.get_proxy_tools()]
        assert advertised == ["srv__read_file", "srv__risky_tool"]
        assert mgr._advertised_risk_penalties == {}
        assert any("risk enrichment" in r.message for r in caplog.records)

    async def test_graph_risk_stamps_v3_ranker_version(self, tmp_path):
        # End-to-end: a graph risk penalty splits the v3 replay cohort.
        mgr, log = _tg_manager(tmp_path, servers={"srv": ["read_file", "risky_tool"]})
        await mgr._consult_toolgraph()
        mgr.get_proxy_tools()
        await mgr.call_tool("srv", "risky_tool", {"task": "do the risky thing now"})
        selection, execution = _events(log)
        assert selection["ranker_version"] == RANKER_VERSION_BM25_GRAPH_RISK
        assert execution["ranker_version"] == RANKER_VERSION_BM25_GRAPH_RISK
        ranked = selection["candidate_features"]["ranked_candidates"]
        risky = next(r for r in ranked if r["tool"] == "srv__risky_tool")
        assert risky["risk_penalty"] == 0.4
        assert risky["risk_penalty_source"] == PENALTY_SOURCE_GRAPH

    async def test_multi_upstream_fan_out_to_one_graph_ref(self, tmp_path):
        # Two upstreams both crawled under graph server "srv", each exposing a
        # "blocked" tool → one graph ref "srv::blocked" fans its NOT_GRANTED
        # verdict back to BOTH STM keys.
        mgr, _ = _tg_manager(
            tmp_path,
            servers={"a": ["blocked"], "b": ["blocked"]},
            server_name_map={"a": "srv", "b": "srv"},
        )
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_external_rejects == {
            ("a", "blocked"): REASON_TOOLGRAPH_NOT_GRANTED,
            ("b", "blocked"): REASON_TOOLGRAPH_NOT_GRANTED,
        }
        assert mgr.get_proxy_tools() == []  # both withheld under strict

    async def test_agent_not_found_fail_start_raises_through_consult(self, tmp_path):
        mgr, _ = _tg_manager(tmp_path, agent_id="ghost")  # on_agent_not_found defaults fail_start
        with pytest.raises(ToolgraphStartupError):
            await mgr._consult_toolgraph()

    async def test_fail_start_aborts_public_start_before_advertising(self, tmp_path):
        # The load-bearing invariant: a fail_start failure must propagate out of
        # the PUBLIC start() (not just the private consult seam) so the lifespan
        # never advertises a tool. Driven through mgr.start().
        mgr, _ = _tg_manager(tmp_path, agent_id="ghost")
        with pytest.raises(ToolgraphStartupError):
            await mgr.start()
        assert mgr._advertised_infos == []  # nothing was advertised
        await mgr.stop()

    async def test_protocol_error_fail_start_aborts_public_start(self, tmp_path):
        mgr, _ = _tg_manager(
            tmp_path, query_profile="boom"
        )  # on_protocol_error defaults fail_start
        with pytest.raises(ToolgraphStartupError):
            await mgr.start()
        assert mgr._advertised_infos == []
        await mgr.stop()

    async def test_agent_not_found_open_degrades(self, tmp_path, caplog):
        mgr, _ = _tg_manager(tmp_path, agent_id="ghost", on_agent_not_found="open")
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            await mgr._consult_toolgraph()
        assert mgr._toolgraph_degraded is True
        assert mgr._toolgraph_degraded_reason == REASON_TOOLGRAPH_AGENT_NOT_FOUND
        assert mgr._toolgraph_external_rejects == {}
        # graph responded on the abort, so the generation is still pinned.
        assert mgr._graph_generation == 11
        advertised = [i.prefixed_name for i in mgr.get_proxy_tools()]
        assert advertised == ["srv__read_file", "srv__blocked"]
        assert mgr.get_toolgraph_status()["degraded"] is True
        # the loud-degrade WARNING is load-bearing — assert it was emitted.
        assert any("DEGRADED" in r.message and "NOT active" in r.message for r in caplog.records)

    async def test_agent_not_found_closed_withholds_all(self, tmp_path):
        mgr, _ = _tg_manager(tmp_path, agent_id="ghost", on_agent_not_found="closed")
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_withhold_all == REASON_TOOLGRAPH_AGENT_NOT_FOUND
        assert mgr.get_proxy_tools() == []
        assert mgr._advertised_reject_reasons == {
            "srv__read_file": REASON_TOOLGRAPH_AGENT_NOT_FOUND,
            "srv__blocked": REASON_TOOLGRAPH_AGENT_NOT_FOUND,
        }
        assert mgr.get_toolgraph_status()["withholding_all"] == REASON_TOOLGRAPH_AGENT_NOT_FOUND

    async def test_withhold_all_verdict_reaches_telemetry(self, tmp_path):
        # The whole-call (every candidate rejected, profile-independent) verdict
        # must reach the selection log just like the per-candidate path does.
        mgr, log = _tg_manager(tmp_path, agent_id="ghost", on_agent_not_found="closed")
        await mgr._consult_toolgraph()
        mgr.get_proxy_tools()
        await mgr.call_tool("srv", "read_file", {})
        selection, _execution = _events(log)
        assert selection["reject_reasons"] == {
            "srv__read_file": REASON_TOOLGRAPH_AGENT_NOT_FOUND,
            "srv__blocked": REASON_TOOLGRAPH_AGENT_NOT_FOUND,
        }

    async def test_protocol_error_open_degrades(self, tmp_path):
        mgr, _ = _tg_manager(tmp_path, query_profile="boom", on_protocol_error="open")
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_degraded is True
        assert mgr._toolgraph_degraded_reason == REASON_TOOLGRAPH_PROTOCOL_ERROR
        assert mgr._graph_generation is None  # no usable verdict

    async def test_protocol_error_closed_withholds_all(self, tmp_path):
        mgr, _ = _tg_manager(tmp_path, query_profile="boom", on_protocol_error="closed")
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_withhold_all == REASON_TOOLGRAPH_PROTOCOL_ERROR
        assert mgr.get_proxy_tools() == []

    async def test_typed_backend_unavailable_default_open_degrades(self, tmp_path):
        mgr, _ = _tg_manager(tmp_path, query_profile="backend_unavailable")
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_degraded is True
        assert mgr._toolgraph_degraded_reason == REASON_TOOLGRAPH_UNREACHABLE
        assert mgr._toolgraph_from_cache is False
        assert mgr._graph_generation is None
        assert [i.prefixed_name for i in mgr.get_proxy_tools()] == [
            "srv__read_file",
            "srv__blocked",
        ]

    async def test_typed_backend_unavailable_closed_withholds_all(self, tmp_path):
        mgr, _ = _tg_manager(
            tmp_path,
            query_profile="backend_unavailable",
            on_unreachable="closed",
        )
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_withhold_all == REASON_TOOLGRAPH_UNREACHABLE
        assert mgr.get_proxy_tools() == []

    async def test_timeout_maps_to_unreachable(self, tmp_path):
        # query_profile="sleep" blocks past timeout_seconds → the consult
        # wait_for fires → ToolgraphUnreachableError → on_unreachable default open.
        mgr, _ = _tg_manager(tmp_path, query_profile="sleep", timeout_seconds=0.5)
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_degraded is True
        assert mgr._toolgraph_degraded_reason == REASON_TOOLGRAPH_UNREACHABLE

    async def test_unreachable_closed_withholds_all(self, tmp_path):
        mgr, _ = _tg_manager(
            tmp_path, command="this-binary-does-not-exist-xyz", args=[], on_unreachable="closed"
        )
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_withhold_all == REASON_TOOLGRAPH_UNREACHABLE
        assert mgr.get_proxy_tools() == []

    async def test_unreachable_open_degrades(self, tmp_path):
        mgr, _ = _tg_manager(
            tmp_path, command="this-binary-does-not-exist-xyz", args=[]
        )  # on_unreachable default open
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_degraded is True
        assert mgr._toolgraph_degraded_reason == REASON_TOOLGRAPH_UNREACHABLE
        assert [i.prefixed_name for i in mgr.get_proxy_tools()] == [
            "srv__read_file",
            "srv__blocked",
        ]

    async def test_tool_not_found_open_keeps_advertised(self, tmp_path):
        mgr, _ = _tg_manager(tmp_path, servers={"srv": ["read_file", "missing_x"]})
        await mgr._consult_toolgraph()  # on_tool_not_found default open
        assert mgr._toolgraph_external_rejects == {}
        # An uncrawled (blind-spot) tool has no graph facts → risk_score None →
        # no graph risk penalty, even though it stays advertised.
        assert mgr._toolgraph_risk_penalties == {}
        assert [i.prefixed_name for i in mgr.get_proxy_tools()] == [
            "srv__read_file",
            "srv__missing_x",
        ]
        assert mgr._advertised_risk_penalties == {}

    async def test_tool_not_found_closed_rejects(self, tmp_path):
        mgr, _ = _tg_manager(
            tmp_path, servers={"srv": ["read_file", "missing_x"]}, on_tool_not_found="closed"
        )
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_external_rejects == {
            ("srv", "missing_x"): REASON_TOOLGRAPH_TOOL_NOT_FOUND
        }
        assert [i.prefixed_name for i in mgr.get_proxy_tools()] == ["srv__read_file"]

    async def test_server_name_mismatch_warns_when_all_unknown(self, tmp_path, caplog):
        # Every tool of "ext" comes back TOOL_NOT_FOUND and "ext" has no map
        # entry → likely a server_name_map gap.
        mgr, _ = _tg_manager(tmp_path, servers={"ext": ["missing_a", "missing_b"]})
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            await mgr._consult_toolgraph()
        assert any(
            "unknown to the tool-graph" in r.message and "'ext'" in r.message
            for r in caplog.records
        )

    async def test_no_mismatch_warning_when_partially_crawled(self, tmp_path, caplog):
        # read_file is eligible, so "srv" is not a 100% miss → no false positive.
        mgr, _ = _tg_manager(tmp_path, servers={"srv": ["read_file", "missing_x"]})
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            await mgr._consult_toolgraph()
        assert not any("unknown to the tool-graph" in r.message for r in caplog.records)

    async def test_no_mismatch_warning_when_server_is_mapped(self, tmp_path, caplog):
        # "ext" IS mapped, so even a 100% miss does not warn (operator declared
        # the mapping; the misses are genuinely uncrawled tools).
        mgr, _ = _tg_manager(
            tmp_path, servers={"ext": ["missing_a"]}, server_name_map={"ext": "srv"}
        )
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            await mgr._consult_toolgraph()
        assert not any("unknown to the tool-graph" in r.message for r in caplog.records)

    async def test_second_start_resets_recovered_graph(self, tmp_path):
        # A closed-knob withhold on the first consult must not persist to a
        # second consult where the graph has recovered.
        mgr, _ = _tg_manager(
            tmp_path, command="this-binary-does-not-exist-xyz", args=[], on_unreachable="closed"
        )
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_withhold_all == REASON_TOOLGRAPH_UNREACHABLE
        # "Recover" the graph by pointing the config at the working fake child,
        # then re-consult: the withhold-all state must be cleared.
        mgr._config.toolgraph.command = sys.executable
        mgr._config.toolgraph.args = [str(_FAKE_TOOLGRAPH)]
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_withhold_all is None
        assert mgr._toolgraph_degraded is False
        assert mgr._graph_generation == 11

    async def test_server_name_map_translates_ref(self, tmp_path):
        # STM key "local" maps to graph-crawled "srv"; the "blocked" tool there
        # still resolves to "srv::blocked" → NOT_GRANTED.
        mgr, _ = _tg_manager(
            tmp_path, servers={"local": ["read_file", "blocked"]}, server_name_map={"local": "srv"}
        )
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_external_rejects == {
            ("local", "blocked"): REASON_TOOLGRAPH_NOT_GRANTED
        }


# ── stm_proxy_health rendering of the toolgraph status (loud degrade) ─────────


def _read_calls(path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _consult_row_flags(cache) -> list[tuple[int]]:
    return cache._db.execute("SELECT had_risk_scores FROM toolgraph_consult").fetchall()


def _assert_consult_cached(mgr) -> None:
    # #714 — preconditions for a cache HIT, so a hit-assert failure names its
    # cause instead of presenting as a bare `False is True`.
    assert mgr._toolgraph_cache is not None  # else: the open degraded to no-cache
    # Exactly one row, risk-eligible — [(0,)] means a transient rank_features
    # failure was written (ineligible while risk is wanted); [] means the miss
    # never wrote the row.
    assert _consult_row_flags(mgr._toolgraph_cache) == [(1,)]


class TestToolgraphConsultCache:
    """#494 — disk-cache the startup consult (Model A: cheap generation probe)."""

    async def test_hit_skips_full_consult(self, tmp_path, caplog):
        call_log = tmp_path / "calls.txt"
        mgr, _ = _tg_manager(tmp_path, env={"FAKE_TG_CALL_LOG": str(call_log)})
        # finally-stop: a failing diagnostic assert must not leave the sqlite
        # handle open — Windows tmp_path cleanup would stack a PermissionError
        # on top of the real failure (#714).
        try:
            # No logger= — the degrade warnings span memtomem_stm.proxy.manager
            # AND memtomem_stm.proxy.toolgraph_cache.
            with caplog.at_level(logging.WARNING):
                # First consult: cold cache → miss → full consult (populates the cache).
                await mgr._consult_toolgraph()
                assert mgr._toolgraph_from_cache is False
                _assert_consult_cached(mgr)
                first_rejects = dict(mgr._toolgraph_external_rejects)
                n_after_first = len(_read_calls(call_log))

                # Second consult: same generation → cache HIT → only the [] probe runs.
                await mgr._consult_toolgraph()
            # Any open/read/write/malformed degrade is silent for the hit assert
            # below but never for the log (#714) — surface it by name.
            assert [r.message for r in caplog.records if "consult cache" in r.message] == []
            # Residual failure here ⇒ scope-key/generation drift, by elimination.
            assert mgr._toolgraph_from_cache is True
            assert mgr._toolgraph_external_rejects == first_rejects  # identical from cached facts
            assert mgr._graph_generation == 11
            second_calls = _read_calls(call_log)[n_after_first:]
            assert second_calls == [
                "eligible_tools:0"
            ]  # only the probe; no full consult / rank_features
        finally:
            await mgr.stop()

    async def test_open_failure_degrades_to_no_cache_and_warns(self, tmp_path, caplog, monkeypatch):
        # #714 — pins the silent half of the hit test above: an unopenable cache
        # DB degrades to no-cache (consult unaffected, NOT a graph degrade) and
        # says so at WARNING. Positive control for the "no consult cache
        # warnings" sweep in the hit tests.
        def _unopenable(self):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(GraphConsultCache, "initialize", _unopenable)
        mgr, _ = _tg_manager(tmp_path)
        try:
            with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
                await mgr._consult_toolgraph()
            # The consult itself ran live and succeeded.
            assert mgr._graph_generation == 11
            assert mgr._toolgraph_external_rejects == {
                ("srv", "blocked"): REASON_TOOLGRAPH_NOT_GRANTED
            }
            assert mgr._toolgraph_degraded is False
            # The cache silently sat out the session — silent except for the log.
            assert mgr._toolgraph_cache is None
            assert mgr._toolgraph_from_cache is False
            assert any("consult cache could not be opened" in r.message for r in caplog.records)
        finally:
            await mgr.stop()

    async def test_generation_bump_misses(self, tmp_path):
        gen_file = tmp_path / "gen.txt"
        gen_file.write_text("11")
        call_log = tmp_path / "calls.txt"
        mgr, _ = _tg_manager(
            tmp_path,
            env={"FAKE_TG_GENERATION_FILE": str(gen_file), "FAKE_TG_CALL_LOG": str(call_log)},
        )
        await mgr._consult_toolgraph()
        assert mgr._graph_generation == 11
        n_after_first = len(_read_calls(call_log))

        gen_file.write_text("12")  # the graph mutated → generation bumped
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_from_cache is False
        assert mgr._graph_generation == 12
        # The probe read gen 12, missed the gen-11 row, and ran the full consult.
        assert "eligible_tools:2" in _read_calls(call_log)[n_after_first:]
        await mgr.stop()

    async def test_degrade_with_populated_cache_stays_loud_and_serves_no_cache(
        self, tmp_path, caplog
    ):
        mgr, _ = _tg_manager(tmp_path)
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_external_rejects  # a real verdict was cached
        # The graph goes unreachable; the populated cache must NOT mask it.
        mgr._config.toolgraph.command = "this-binary-does-not-exist-xyz"
        mgr._config.toolgraph.args = []
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            await mgr._consult_toolgraph()
        assert mgr._toolgraph_degraded is True
        assert mgr._toolgraph_degraded_reason == REASON_TOOLGRAPH_UNREACHABLE
        assert mgr._toolgraph_from_cache is False
        assert mgr._graph_generation is None
        assert mgr._toolgraph_external_rejects == {}  # cache was not served
        assert any("DEGRADED" in r.message and "NOT active" in r.message for r in caplog.records)
        await mgr.stop()

    async def test_agent_not_found_not_cached(self, tmp_path):
        shared_db = tmp_path / "shared.db"
        mgr, _ = _tg_manager(
            tmp_path, agent_id="ghost", on_agent_not_found="open", consult_cache_path=shared_db
        )
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_degraded is True
        await mgr.stop()
        # The structured abort wrote no row — a degraded / agent-not-found verdict
        # is never cached, so it cannot poison a later valid-agent consult.
        check = GraphConsultCache(shared_db)
        check.initialize()
        try:
            n = check._db.execute("SELECT COUNT(*) FROM toolgraph_consult").fetchone()[0]
        finally:
            check.close()
        assert n == 0

    async def test_probe_protocol_error_routes_to_knob(self, tmp_path):
        # With the cache enabled, the [] probe is the first call — a protocol
        # error on it must ride on_protocol_error exactly as the full verdict would.
        mgr, _ = _tg_manager(tmp_path, query_profile="boom", on_protocol_error="open")
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_degraded is True
        assert mgr._toolgraph_degraded_reason == REASON_TOOLGRAPH_PROTOCOL_ERROR
        assert mgr._toolgraph_from_cache is False
        await mgr.stop()

    async def test_knob_change_remaps_on_hit(self, tmp_path, caplog):
        # Populate under on_tool_not_found=open (missing_x kept), then flip to
        # closed on a same-generation restart → the hit re-maps the cached RAW
        # facts under the new knob → missing_x is now rejected.
        mgr, _ = _tg_manager(tmp_path, servers={"srv": ["read_file", "missing_x"]})
        try:
            with caplog.at_level(logging.WARNING):
                await mgr._consult_toolgraph()  # on_tool_not_found default open
                assert mgr._toolgraph_external_rejects == {}
                _assert_consult_cached(mgr)
                mgr._config.toolgraph.on_tool_not_found = "closed"
                await mgr._consult_toolgraph()
            assert [r.message for r in caplog.records if "consult cache" in r.message] == []
            assert mgr._toolgraph_from_cache is True
            assert mgr._toolgraph_external_rejects == {
                ("srv", "missing_x"): REASON_TOOLGRAPH_TOOL_NOT_FOUND
            }
        finally:
            await mgr.stop()

    async def test_risk_scale_change_rescales_on_hit(self, tmp_path, caplog):
        mgr, _ = _tg_manager(tmp_path, servers={"srv": ["risky_tool"]})  # scale default 1.0
        try:
            with caplog.at_level(logging.WARNING):
                await mgr._consult_toolgraph()
                assert mgr._toolgraph_risk_penalties == {("srv", "risky_tool"): 0.4}
                _assert_consult_cached(mgr)
                mgr._config.toolgraph.risk_penalty_scale = 0.5
                await mgr._consult_toolgraph()
            assert [r.message for r in caplog.records if "consult cache" in r.message] == []
            assert mgr._toolgraph_from_cache is True
            # Re-scaled locally from the cached raw risk_score (0.4 * 0.5).
            assert mgr._toolgraph_risk_penalties == {("srv", "risky_tool"): 0.2}
        finally:
            await mgr.stop()

    async def test_transient_enrichment_failure_not_cached_as_success(self, tmp_path):
        # rank_features fails (agent "rankboom") while eligible_tools succeeds →
        # had_risk_scores=False → a later want-risk consult MISSES and re-runs the
        # full consult instead of serving "no penalties" off the cache forever.
        call_log = tmp_path / "calls.txt"
        mgr, _ = _tg_manager(
            tmp_path,
            servers={"srv": ["risky_tool"]},
            agent_id="rankboom",
            env={"FAKE_TG_CALL_LOG": str(call_log)},
        )
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_risk_penalties == {}  # enrichment failed this session
        n_after_first = len(_read_calls(call_log))
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_from_cache is False  # had_risk_scores False → miss under want_risk
        assert "eligible_tools:1" in _read_calls(call_log)[n_after_first:]  # full consult re-ran
        await mgr.stop()

    async def test_typed_backend_enrichment_failure_not_cached_as_success(self, tmp_path):
        call_log = tmp_path / "calls.txt"
        mgr, _ = _tg_manager(
            tmp_path,
            servers={"srv": ["risky_tool"]},
            agent_id="rankunavailable",
            env={"FAKE_TG_CALL_LOG": str(call_log)},
        )
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_risk_penalties == {}
        n_after_first = len(_read_calls(call_log))
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_from_cache is False
        assert "eligible_tools:1" in _read_calls(call_log)[n_after_first:]
        await mgr.stop()

    async def test_malformed_enrichment_not_cached_as_success(self, tmp_path):
        # rank_features returns a NON-error but malformed payload (no 'features'
        # list, agent "rankmalformed") → had_risk_scores=False → a later want-risk
        # consult MISSES and re-runs the full consult, never pinning "no penalties".
        call_log = tmp_path / "calls.txt"
        mgr, _ = _tg_manager(
            tmp_path,
            servers={"srv": ["risky_tool"]},
            agent_id="rankmalformed",
            env={"FAKE_TG_CALL_LOG": str(call_log)},
        )
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_risk_penalties == {}  # malformed enrichment → no penalties
        n_after_first = len(_read_calls(call_log))
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_from_cache is False
        assert "eligible_tools:1" in _read_calls(call_log)[n_after_first:]  # full consult re-ran
        await mgr.stop()

    async def test_cache_disabled_single_consult(self, tmp_path):
        call_log = tmp_path / "calls.txt"
        mgr, _ = _tg_manager(
            tmp_path, consult_cache_enabled=False, env={"FAKE_TG_CALL_LOG": str(call_log)}
        )
        await mgr._consult_toolgraph()
        assert mgr._toolgraph_from_cache is False
        calls = _read_calls(call_log)
        assert "eligible_tools:0" not in calls  # no probe
        assert "eligible_tools:2" in calls  # one full consult, today's behavior
        await mgr.stop()

    async def test_health_shows_from_cache(self, tmp_path):
        mgr, _ = _tg_manager(tmp_path)
        await mgr._consult_toolgraph()
        await mgr._consult_toolgraph()  # 2nd consult = cache hit
        assert mgr._toolgraph_from_cache is True
        body = "\n".join(_toolgraph_health_lines(mgr.get_toolgraph_status()))
        assert "from cache" in body
        await mgr.stop()


class TestToolgraphHealthRendering:
    def test_disabled_renders_nothing(self):
        assert _toolgraph_health_lines(None) == []

    def test_active_shows_generation_and_reject_count(self):
        lines = _toolgraph_health_lines(
            {
                "enabled": True,
                "degraded": False,
                "degraded_reason": None,
                "withholding_all": None,
                "graph_generation": 11,
                "external_reject_count": 2,
                "risk_penalty_count": 0,
            }
        )
        body = "\n".join(lines)
        assert "active" in body
        assert "11" in body
        assert "2 tool(s) rejected" in body
        # zero risk penalties / no recorded facts → no suffix, line closes
        # cleanly.
        assert "carry a graph risk penalty" not in body
        assert "recorded graph facts" not in body
        assert body.rstrip().endswith(")")

    def test_active_shows_risk_penalty_count(self):
        lines = _toolgraph_health_lines(
            {
                "enabled": True,
                "degraded": False,
                "degraded_reason": None,
                "withholding_all": None,
                "graph_generation": 11,
                "external_reject_count": 2,
                "risk_penalty_count": 3,
            }
        )
        body = "\n".join(lines)
        assert "2 tool(s) rejected" in body
        assert "3 carry a graph risk penalty" in body
        assert body.rstrip().endswith(")")  # suffix sits inside the closing paren

    def test_active_shows_recorded_graph_facts(self):
        """Facts captured with every penalty zeroed still show as enrichment.

        ``risk_penalty_scale: 0`` with telemetry on is exactly the state where
        the penalty count reads ``0`` while the enrichment did run (#469), so
        the health line has to distinguish it from a skipped consult.
        """
        lines = _toolgraph_health_lines(
            {
                "enabled": True,
                "degraded": False,
                "degraded_reason": None,
                "withholding_all": None,
                "graph_generation": 11,
                "external_reject_count": 0,
                "risk_penalty_count": 0,
                "graph_facts_count": 5,
            }
        )
        body = "\n".join(lines)
        assert "carry a graph risk penalty" not in body
        assert "5 with recorded graph facts" in body
        assert body.rstrip().endswith(")")

    def test_degraded_is_loud(self):
        lines = _toolgraph_health_lines(
            {
                "enabled": True,
                "degraded": True,
                "degraded_reason": REASON_TOOLGRAPH_UNREACHABLE,
                "withholding_all": None,
                "graph_generation": None,
                "external_reject_count": 0,
            }
        )
        body = "\n".join(lines)
        assert "DEGRADED" in body
        assert "NOT active" in body
        assert REASON_TOOLGRAPH_UNREACHABLE in body

    def test_withholding_all_is_loud(self):
        lines = _toolgraph_health_lines(
            {
                "enabled": True,
                "degraded": False,
                "degraded_reason": None,
                "withholding_all": REASON_TOOLGRAPH_PROTOCOL_ERROR,
                "graph_generation": None,
                "external_reject_count": 0,
            }
        )
        body = "\n".join(lines)
        assert "WITHHOLDING ALL" in body
        assert REASON_TOOLGRAPH_PROTOCOL_ERROR in body

    def test_not_consulted_when_no_generation(self):
        # enabled, no failure, but no usable generation → consult was skipped.
        lines = _toolgraph_health_lines(
            {
                "enabled": True,
                "degraded": False,
                "degraded_reason": None,
                "withholding_all": None,
                "graph_generation": None,
                "external_reject_count": 0,
            }
        )
        body = "\n".join(lines)
        assert "not consulted" in body
        assert "active" not in body
