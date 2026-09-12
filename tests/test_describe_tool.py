"""Recover the registered generation of metadata across host truncation (#1014)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from memtomem_stm.cli.description_diagnostics import description_budget_doctor_checks
from memtomem_stm.proxy._fastmcp_compat import register_proxy_tool
from memtomem_stm.proxy.config import CompressionStrategy, ProxyConfig, ToolOverrideConfig
from memtomem_stm.proxy.staged_status import ProbeStage, StagedProbeResult
from memtomem_stm.proxy.tool_metadata import PROXIED_PREFIX, RECOVERY_SUFFIX, convention_suffix
from memtomem_stm.server import stm_proxy_describe_tool
from test_tool_metadata import _fake_tool, _make_manager_with_tools


def _manager(tools, *, host_cap=None, **kwargs):
    kwargs.setdefault("max_description_chars", 4000)
    kwargs.setdefault("server_max_desc", 4000)
    manager = _make_manager_with_tools(tools, **kwargs)
    manager._config.host_description_cap = host_cap
    return manager


def _register(manager):
    infos = {i.prefixed_name: i for i in manager.get_proxy_tools()}
    manager.retain_registered_advertisement(list(infos), registered_infos=infos)
    return infos


def _doctor(manager, tools):
    probe = StagedProbeResult(
        stage=ProbeStage.TOOLS_DISCOVERED,
        tools=len(tools),
        description_chars=tuple((t.name, len(t.description)) for t in tools),
        schema_description_tools=tuple(t.name for t in tools),
    )
    return description_budget_doctor_checks(
        manager._config, {"srv": probe}, path_hint="config.json"
    )[0]


async def test_host_cap_listed_hint_and_full_response_through_mcp():
    tail = "Prefer targeted rg. Cite URLs without the .mdx extension."
    source = "x" * (2593 - len(tail)) + tail
    manager = _manager([_fake_tool("query_docs", source)], host_cap=2048)
    server = MCPServer("description-recovery-test")
    infos = manager.get_proxy_tools()

    async def upstream_handler(**kwargs):
        raise AssertionError("Reading metadata must not execute the upstream tool")

    registered = {
        i.prefixed_name: i for i in infos if register_proxy_tool(server, upstream_handler, i)
    }
    manager.retain_registered_advertisement(list(registered), registered_infos=registered)
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=SimpleNamespace(proxy_manager=manager))
    )

    @server.tool(name="stm_proxy_describe_tool")
    async def describe(name: str) -> dict[str, Any]:
        return await stm_proxy_describe_tool(name, ctx=ctx)

    async with InMemoryTransport(server) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            listed = await session.list_tools()
            advertised = next(t for t in listed.tools if t.name == "test__query_docs")
            # Emulate the host's independent cut, not just our own length check.
            received = advertised.description[:2048]
            assert len(advertised.description) <= 2048
            assert received.endswith(RECOVERY_SUFFIX)
            assert tail not in received
            result = await session.call_tool("stm_proxy_describe_tool", {"name": advertised.name})
    assert not result.is_error
    assert result.structured_content["description"] == source
    assert tail in "".join(c.text for c in result.content)
    manager._connections["srv"].session.call_tool.assert_not_called()


@pytest.mark.parametrize("cap", [None, 32, 41, 42, 53, 54, 85, 86, 100, 2048])
@pytest.mark.parametrize(
    "strategy",
    [CompressionStrategy.NONE, CompressionStrategy.SELECTIVE, CompressionStrategy.PROGRESSIVE],
)
def test_host_boundaries_keep_complete_hints(cap, strategy):
    source = "한국어 설명과 필수 사용 지침 " * 400
    tool = _fake_tool("t", source)
    manager = _manager([tool], host_cap=cap, compression=strategy)
    info = _register(manager)["test__t"]
    effective_cap = cap or 4000
    advertised = PROXIED_PREFIX + info.description
    assert len(advertised) <= effective_cap
    suffix = convention_suffix(strategy, None)
    available = effective_cap - len(PROXIED_PREFIX)
    compression_fits = bool(suffix) and len(suffix) <= available
    if compression_fits:
        assert suffix.strip() in advertised
        available -= len(suffix)
    elif suffix:
        assert suffix.strip() not in advertised
    assert (RECOVERY_SUFFIX.strip() in advertised) == (available >= len(RECOVERY_SUFFIX))
    assert manager.describe_tool("test__t")["description"] == source.strip()
    if cap is not None and available < len(RECOVERY_SUFFIX):
        check = _doctor(manager, [tool])
        assert check[2] == "WARN"
        assert "recovery hint is dropped" in check[3]


@pytest.mark.parametrize("length", [100, 2038, 2039, 5000])
def test_recovery_only_when_needed_and_host_unset_is_unknown(length):
    source = "z" * length
    unknown = _register(_manager([_fake_tool("t", source)]))["test__t"]
    assert (RECOVERY_SUFFIX in unknown.description) == (length > 3990)
    capped = _register(_manager([_fake_tool("t", source)], host_cap=2048))["test__t"]
    assert (RECOVERY_SUFFIX in capped.description) == (length > 2038)


def test_schema_recovery_includes_nested_descriptions_and_proxy_context():
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string", "description": "필수 검색어", "examples": ["예제"]}},
        "$defs": {"value": {"anyOf": [{"type": "string", "description": "원문"}]}},
    }
    tool = _fake_tool("t", "Short description", schema)
    manager = _manager([tool], strip_schema_descriptions=True, advertise_context_query=True)
    info = _register(manager)["test__t"]
    assert RECOVERY_SUFFIX in info.description
    assert "description" not in info.input_schema["properties"]["q"]
    recovered = manager.describe_tool("test__t")
    assert recovered["input_schema"]["properties"]["q"] == schema["properties"]["q"]
    assert recovered["input_schema"]["$defs"] == schema["$defs"]
    assert (
        recovered["input_schema"]["properties"]["_context_query"]
        == info.input_schema["properties"]["_context_query"]
    )
    assert "_context_query" not in schema["properties"]
    recovered["input_schema"]["properties"]["q"]["description"] = "mutated reply"
    tool.input_schema["properties"]["q"]["description"] = "mutated upstream"
    assert (
        manager.describe_tool("test__t")["input_schema"]["properties"]["q"]["description"]
        == "필수 검색어"
    )


@pytest.mark.parametrize("override", [None, "", "  Custom instructions.  "])
def test_override_and_empty_description_are_unambiguous(override):
    manager = _manager(
        [_fake_tool("t", "  Original text.  ")],
        tool_overrides={"t": ToolOverrideConfig(description_override=override)},
    )
    _register(manager)
    result = manager.describe_tool("test__t")
    assert result["description"] == (
        "Original text." if override is None else override.strip() or "test__t"
    )
    if override is None:
        assert "upstream_description" not in result
    else:
        assert result["upstream_description"] == "  Original text.  "


def test_snapshot_changes_only_when_registration_commits_and_failed_removal_keeps_old():
    tool = _fake_tool("t", "Old generation")
    manager = _manager([tool])
    old = _register(manager)
    tool.description = "New generation"
    desired = {i.prefixed_name: i for i in manager.get_proxy_tools()}
    assert manager.describe_tool("test__t")["description"] == "Old generation"
    # Removal failed: the old Tool is still installed in the server registry.
    manager.retain_registered_advertisement(list(old), registered_infos=old)
    assert manager.describe_tool("test__t")["description"] == "Old generation"
    manager.retain_registered_advertisement(list(desired), registered_infos=desired)
    assert manager.describe_tool("test__t")["description"] == "New generation"
    manager.retain_registered_advertisement([], registered_infos={})
    with pytest.raises(ToolError, match="unavailable"):
        manager.describe_tool("test__t")


async def test_registration_decline_unknown_hidden_and_stopped_do_not_reveal_metadata():
    manager = _manager(
        [_fake_tool("visible", "Public"), _fake_tool("hidden", "Hidden instructions")],
        tool_overrides={"hidden": ToolOverrideConfig(hidden=True)},
    )
    manager.get_proxy_tools()
    manager.retain_registered_advertisement([])
    for name in ["test__visible", "test__hidden", "visible", "missing", "", "\ud800"]:
        with pytest.raises(ToolError, match="^Tool metadata is unavailable for this name.$"):
            manager.describe_tool(name)
    _register(manager)
    manager._connections.clear()
    await manager.stop()
    with pytest.raises(ToolError, match="unavailable"):
        manager.describe_tool("test__visible")


def test_full_override_tail_is_scanned_before_registration():
    manager = _manager(
        [_fake_tool("t", "Benign upstream description")],
        host_cap=100,
        tool_overrides={
            "t": ToolOverrideConfig(description_override="x" * 300 + " api_key=sk-" + "a" * 24)
        },
    )
    assert _register(manager) == {}
    assert manager._advertised_reject_reasons["test__t"] == "sensitive_metadata"
    with pytest.raises(ToolError, match="unavailable"):
        manager.describe_tool("test__t")


def test_live_toolgraph_denial_uses_generic_error_without_disclosing_reason():
    manager = _manager([_fake_tool("t", "Instructions")])
    _register(manager)
    with patch.object(
        manager, "_enforce_toolgraph_call_policy", side_effect=ToolError("private policy reason")
    ) as gate:
        with pytest.raises(ToolError, match="^Tool metadata is unavailable for this name.$"):
            manager.describe_tool("test__t")
    gate.assert_called_once_with("srv", "t")


def test_lookup_uses_exact_registered_name_not_separator_parsing():
    manager = _manager([_fake_tool("a__b", "Instructions")])
    _register(manager)
    assert manager.describe_tool("test__a__b")["name"] == "test__a__b"


def test_doctor_reports_host_limit_without_recommending_a_larger_stm_cap():
    tools = [_fake_tool("t", "x" * 2593)]
    manager = _manager(tools, host_cap=2048)
    check = _doctor(manager, tools)
    assert check[2] == "WARN"
    assert "host 2048" in check[3]
    assert "host limit binds" in check[3]
    assert "stm_proxy_describe_tool" in check[4]
    assert 'set "max_description_chars"' not in check[4]


def test_schema_only_recovery_hint_loss_is_reported_by_doctor():
    tool = _fake_tool("t", "Short", {"type": "object", "description": "Schema instructions"})
    manager = _manager([tool], host_cap=32, strip_schema_descriptions=True)
    info = _register(manager)["test__t"]
    assert info.description == "Short"
    check = _doctor(manager, [tool])
    assert check[2] == "WARN"
    assert "recovery hint is dropped" in check[3]


def test_host_limit_config_roundtrip_and_validation(tmp_path):
    config = ProxyConfig(config_path=tmp_path / "proxy.json")
    assert config.host_description_cap is None
    assert ProxyConfig.model_validate_json(config.model_dump_json()).host_description_cap is None
    for cap in (32, 2048):
        config.host_description_cap = cap
        assert ProxyConfig.model_validate_json(config.model_dump_json()).host_description_cap == cap
    for cap in (-1, 0, 31):
        with pytest.raises(ValidationError):
            ProxyConfig(host_description_cap=cap)


def test_failed_removal_does_not_restore_rejected_metadata():
    manager = _manager([_fake_tool("t", "Old instructions")])
    old = _register(manager)
    manager._connections["srv"].config.tool_overrides["t"] = ToolOverrideConfig(hidden=True)
    assert manager.get_proxy_tools() == []
    # The host refused to remove the now-hidden tool, but that cannot turn a
    # successful old registration into permission to expose its metadata.
    manager.retain_registered_advertisement(list(old), registered_infos=old)
    with pytest.raises(ToolError, match="unavailable"):
        manager.describe_tool("test__t")


@pytest.mark.parametrize(
    "server_cap,global_cap,host_cap,expected",
    [
        (100, 200, 300, 100),
        (300, 100, 200, 100),
        (300, 200, 100, 100),
        (100, 100, 100, 100),
        (300, 200, None, 200),
    ],
)
def test_three_description_limits_compose_by_minimum(server_cap, global_cap, host_cap, expected):
    manager = _manager(
        [_fake_tool("t", "x" * 1000)],
        server_max_desc=server_cap,
        max_description_chars=global_cap,
        host_cap=host_cap,
    )
    info = _register(manager)["test__t"]
    assert len(PROXIED_PREFIX + info.description) == expected


def test_empty_upstream_uses_name_fallback():
    manager = _manager([_fake_tool("t", "  ")])
    _register(manager)
    assert manager.describe_tool("test__t")["description"] == "test__t"
