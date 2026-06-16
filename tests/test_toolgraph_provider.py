"""Tests for the optional tool-graph eligibility provider (#465, PR1).

Covers the ``toolgraph`` config block (defaults / failure-knob defaults /
file parse) and the connectable ``ToolgraphConsultAdapter`` (stdio lifecycle
+ ``eligible_tools`` consult against a real fake stdio child). PR1 wires
nothing into the eligibility filter; the only runtime touch is the
"enabled but inert" startup warning, asserted here too.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from memtomem_stm.proxy.config import ProxyConfig, ToolgraphConfig
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics import TokenTracker
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


# ── manager "enabled but inert" startup warning (PR1) ───────────────────────


class TestToolgraphInertWarning:
    async def test_enabled_warns_inert_at_startup(self, tmp_path, caplog):
        cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={},
            toolgraph=ToolgraphConfig(enabled=True),
        )
        mgr = ProxyManager(cfg, TokenTracker())
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            await mgr.start()
        try:
            assert any(
                "toolgraph.enabled is set" in r.message and "inert" in r.message
                for r in caplog.records
            )
        finally:
            await mgr.stop()

    async def test_disabled_does_not_warn(self, tmp_path, caplog):
        cfg = ProxyConfig(config_path=tmp_path / "proxy.json", upstream_servers={})
        mgr = ProxyManager(cfg, TokenTracker())
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            await mgr.start()
        try:
            assert not any("toolgraph.enabled" in r.message for r in caplog.records)
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
