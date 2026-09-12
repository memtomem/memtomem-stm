"""Whole-result limits and lossless continuation through the real MCP handler."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from memtomem_stm.proxy.config import ToolOverrideConfig
from memtomem_stm.server import stm_proxy_describe_tool
from test_describe_tool import _manager, _read_all, _register
from test_tool_metadata import _fake_tool


@asynccontextmanager
async def _client(manager):
    @asynccontextmanager
    async def lifespan(server):
        yield SimpleNamespace(proxy_manager=manager)

    server = MCPServer("metadata-pages-test", lifespan=lifespan)
    server.tool()(stm_proxy_describe_tool)
    async with InMemoryTransport(server) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            yield session


def _check_result(result):
    assert not result.is_error
    # Check the actual SDK result rather than the production sizing helper.
    assert len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")) <= 16_384
    assert len(result.content) == 1
    assert json.loads(result.content[0].text) == result.structured_content
    return result.structured_content


@pytest.mark.parametrize("part", ["description", "input_schema"])
@pytest.mark.parametrize("body", ["a" * 1_120_000, '한국어🙂"\\\n\t\x00' * 3000])
async def test_complete_recovery_through_mcp_is_bounded_and_lossless(part, body):
    schema = {
        "type": "object",
        "properties": {"q": {"$ref": "#/$defs/query", "description": body}},
        "$defs": {"query": {"type": "string", "examples": [body]}},
    }
    manager = _manager(
        [_fake_tool("t", body + "TAIL", schema)],
        strip_schema_descriptions=True,
        advertise_context_query=True,
    )
    advertised = _register(manager)["test__t"]
    args = {"name": "test__t", "part": part, "limit": 16_000}
    chunks = []
    async with _client(manager) as session:
        listed = await session.list_tools()
        tool = next(t for t in listed.tools if t.name == "stm_proxy_describe_tool")
        assert tool.input_schema["properties"]["part"]["default"] == "description"
        assert set(tool.input_schema["required"]) == {"name"}
        generation = None
        while True:
            result = await session.call_tool("stm_proxy_describe_tool", args)
            page = _check_result(result)
            assert page["offset"] == args.get("offset", 0)
            assert page["part"] == part
            assert page["format"] == ("json" if part == "input_schema" else "text")
            if generation is None:
                generation = page["generation"]
            assert page["generation"] == generation
            chunks.append(page["text"])
            if page["next_offset"] is None:
                break
            assert page["next_offset"] == page["offset"] + len(page["text"])
            assert page["next_offset"] > page["offset"]
            args.update(offset=page["next_offset"], generation=generation)
        assert len(chunks) > 1
        text = "".join(chunks)
        assert len(text) == page["total_chars"]
        if part == "description":
            assert text == body + "TAIL"
        else:
            expected = {**schema, "properties": dict(schema["properties"])}
            expected["properties"]["_context_query"] = advertised.input_schema["properties"][
                "_context_query"
            ]
            assert json.loads(text) == expected
        args.update(offset=page["total_chars"], generation=generation)
        eof = _check_result(await session.call_tool("stm_proxy_describe_tool", args))
        assert eof["text"] == ""
        assert eof["next_offset"] is None
    manager._connections["srv"].session.call_tool.assert_not_called()


async def test_default_does_not_recover_schema_or_replaced_description():
    schema = {"description": "SCHEMA-SECRET" * 100_000}
    manager = _manager(
        [_fake_tool("t", "REPLACED-SECRET", schema)],
        tool_overrides={"t": ToolOverrideConfig(description_override="Instructions only.")},
        strip_schema_descriptions=True,
    )
    manager._config.recover_upstream_description = True
    _register(manager)
    async with _client(manager) as session:
        result = await session.call_tool("stm_proxy_describe_tool", {"name": "test__t"})
        page = _check_result(result)
        assert page["text"] == "Instructions only."
        assert page["part"] == "description"
        assert "SECRET" not in result.model_dump_json()


@pytest.mark.parametrize("opt_in,override", [(False, "Operator instructions"), (True, None)])
async def test_explicit_upstream_part_still_requires_opt_in_and_override(opt_in, override):
    manager = _manager(
        [_fake_tool("t", "WITHHELD-UPSTREAM-TEXT")],
        tool_overrides={"t": ToolOverrideConfig(description_override=override)},
    )
    manager._config.recover_upstream_description = opt_in
    _register(manager)
    async with _client(manager) as session:
        result = await session.call_tool(
            "stm_proxy_describe_tool", {"name": "test__t", "part": "upstream_description"}
        )
        assert result.is_error
        wire = result.model_dump_json(by_alias=True, exclude_none=True)
        assert "unavailable" in wire
        assert "WITHHELD-UPSTREAM-TEXT" not in wire
        assert len(wire.encode("utf-8")) <= 16_384


@pytest.mark.parametrize("mutation", ["description", "schema", "upstream_description"])
def test_only_committed_metadata_changes_invalidate_continuations(mutation):
    tool = _fake_tool("t", "x" * 17_000, {"description": "x" * 17_000})
    kwargs = {}
    if mutation == "upstream_description":
        kwargs["tool_overrides"] = {"t": ToolOverrideConfig(description_override="Override")}
    manager = _manager([tool], strip_schema_descriptions=True, **kwargs)
    manager._config.recover_upstream_description = True
    old = _register(manager)
    part = "input_schema" if mutation == "schema" else mutation
    first = manager.describe_tool("test__t", part=part)
    args = dict(part=part, offset=first["next_offset"], generation=first["generation"])
    assert manager.describe_tool("test__t", part=part) == first
    _register(manager)
    assert manager.describe_tool("test__t", part=part) == first

    # Change only a tail past the old lossy snapshot ceiling.
    if mutation == "schema":
        tool.input_schema["description"] += "NEW"
    else:
        tool.description += "NEW"
    desired = {i.prefixed_name: i for i in manager.get_proxy_tools()}
    assert desired["test__t"] != old["test__t"]
    assert manager.describe_tool("test__t", **args)["generation"] == first["generation"]
    # A failed removal preserves the old registration and its continuation.
    manager.retain_registered_advertisement(list(old), registered_infos=old)
    assert manager.describe_tool("test__t", **args)["generation"] == first["generation"]
    manager.retain_registered_advertisement(list(desired), registered_infos=desired)
    with pytest.raises(ToolError, match="changed; restart"):
        manager.describe_tool("test__t", **args)
    assert _read_all(manager, part=part).endswith("NEW" if mutation != "schema" else 'NEW"}')
    fresh = manager.describe_tool("test__t", part=part)
    assert fresh["generation"] != first["generation"]


@pytest.mark.parametrize("boundary", ["hidden", "declined", "policy", "stopped"])
async def test_every_continuation_rechecks_access(boundary):
    manager = _manager([_fake_tool("t", "x" * 20_000)])
    _register(manager)
    first = manager.describe_tool("test__t")
    args = dict(offset=first["next_offset"], generation=first["generation"])
    if boundary == "hidden":
        manager._advertised_tools.clear()
    elif boundary == "declined":
        manager.retain_registered_advertisement([], registered_infos={})
    elif boundary == "stopped":
        await manager.stop()
    if boundary == "policy":
        with patch.object(
            manager, "_enforce_toolgraph_call_policy", side_effect=ToolError("denied")
        ):
            with pytest.raises(ToolError, match="unavailable"):
                manager.describe_tool("test__t", **args)
    else:
        with pytest.raises(ToolError, match="unavailable"):
            manager.describe_tool("test__t", **args)


@pytest.mark.parametrize(
    "args,reason",
    [
        ({"offset": -1}, "nonnegative"),
        ({"offset": True}, "nonnegative"),
        ({"limit": 0}, "between"),
        ({"limit": -1}, "between"),
        ({"limit": 16_001}, "between"),
        ({"limit": True}, "between"),
        ({"part": "unknown"}, "Unknown"),
        ({"offset": 1}, "generation is required"),
        ({"generation": "old"}, "changed; restart"),
        ({"part": "upstream_description"}, "unavailable"),
    ],
)
def test_invalid_page_requests_do_not_echo_inputs(args, reason):
    manager = _manager([_fake_tool("t", "abc")])
    _register(manager)
    with pytest.raises(ToolError, match=reason):
        manager.describe_tool("test__t", **args)


@pytest.mark.parametrize("field", ["offset", "limit"])
@pytest.mark.parametrize("value", [True, False, 0.0, 1.0])
async def test_mcp_page_arguments_require_strict_integers(field, value):
    manager = _manager([_fake_tool("t", "abc")])
    _register(manager)
    generation = manager.describe_tool("test__t")["generation"]
    async with _client(manager) as session:
        # A valid token prevents a coerced offset=1 from merely failing the
        # continuation guard and disguising SDK argument coercion.
        with patch.object(manager, "describe_tool", wraps=manager.describe_tool) as describe:
            result = await session.call_tool(
                "stm_proxy_describe_tool",
                {"name": "test__t", "generation": generation, field: value},
            )
            assert result.is_error
            describe.assert_not_called()
        wire = result.model_dump_json(by_alias=True, exclude_none=True)
        assert "int_type" in wire
        assert len(wire.encode("utf-8")) <= 16_384


def test_offsets_limit_one_empty_source_and_detached_pages():
    manager = _manager(
        [_fake_tool("t", "")],
        tool_overrides={"t": ToolOverrideConfig(description_override="abc")},
    )
    manager._config.recover_upstream_description = True
    _register(manager)
    page = manager.describe_tool("test__t", limit=1)
    assert page["text"] == "a"
    assert _read_all(manager, limit=1) == "abc"
    assert _read_all(manager, part="upstream_description", limit=1) == ""
    with pytest.raises(ToolError, match="exceeds"):
        manager.describe_tool("test__t", offset=4, generation=page["generation"])
    page["text"] = "mutated"
    assert manager.describe_tool("test__t", limit=1)["text"] == "a"


async def test_oversized_envelope_returns_a_small_non_disclosing_error():
    manager = _manager([_fake_tool("t", "x")])
    infos = _register(manager)
    # Tool names are bounded earlier in normal registration. Exercise the
    # final envelope guard independently via a pathological embedded registry.
    infos["test__t"].description_details["name"] = "OVERSIZED-IDENTIFIER" * 2000
    manager.retain_registered_advertisement(list(infos), registered_infos=infos)
    async with _client(manager) as session:
        result = await session.call_tool("stm_proxy_describe_tool", {"name": "test__t"})
        assert result.is_error
        wire = result.model_dump_json(by_alias=True, exclude_none=True)
        assert len(wire.encode("utf-8")) <= 16_384
        assert "OVERSIZED-IDENTIFIER" not in wire
        assert "envelope exceeds" in wire


def test_transport_safe_surrogates_and_schema_escapes():
    tool = _fake_tool("t", "bad\ud800", {"description": "bad\ud800"})
    manager = _manager([tool])
    _register(manager)
    # Match the project's display policy for unencodable text; JSON schema
    # escapes remain JSON, and parse back to the original code unit.
    assert _read_all(manager) == "bad\\ud800"
    assert json.loads(_read_all(manager, part="input_schema")) == tool.input_schema


def test_removal_and_reregistration_does_not_revive_old_tokens():
    manager = _manager([_fake_tool("t", "x" * 5000)])
    _register(manager)
    first = manager.describe_tool("test__t")
    manager.retain_registered_advertisement([], registered_infos={})
    _register(manager)
    with pytest.raises(ToolError, match="changed; restart"):
        manager.describe_tool("test__t", offset=1, generation=first["generation"])
