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
    REASON_TOOLGRAPH_AGENT_NOT_FOUND,
    REASON_TOOLGRAPH_NOT_GRANTED,
    REASON_TOOLGRAPH_PROTOCOL_ERROR,
    REASON_TOOLGRAPH_TOOL_NOT_FOUND,
    REASON_TOOLGRAPH_UNREACHABLE,
)
from memtomem_stm.server import _toolgraph_health_lines
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

    async def test_eligible_tools_before_start_raises_unreachable(self):
        adapter = _adapter()
        with pytest.raises(ToolgraphUnreachableError):
            await adapter.eligible_tools(["s::a"])

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
                "external_reject_count": 0,
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
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], isError=False)


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
    tg_kwargs: dict = {"command": sys.executable, "args": [str(_FAKE_TOOLGRAPH)]}
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
            SimpleNamespace(name=n, description=f"{n} description", inputSchema={"type": "object"})
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


class TestToolgraphConsultWiring:
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
        assert [i.prefixed_name for i in mgr.get_proxy_tools()] == [
            "srv__read_file",
            "srv__missing_x",
        ]

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
            }
        )
        body = "\n".join(lines)
        assert "active" in body
        assert "11" in body
        assert "2 tool(s) rejected" in body

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
