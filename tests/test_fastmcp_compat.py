"""Unit tests for the MCPServer compatibility layer.

Focused on the ``tag_annotations_title`` helper that prepends a ``[server]``
scope tag to ``ToolAnnotations.title`` for ``/mcp`` picker disambiguation.
"""

from __future__ import annotations

# CallToolResult is module-level so the ``-> CallToolResult`` string
# annotation on test handlers resolves in this module's globals when
# the SDK's func_metadata eval's it.
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from memtomem_stm.proxy.tool_metadata import tag_annotations_title


def test_tag_title_prepends_server_when_title_present() -> None:
    annotations = ToolAnnotations(title="Close browser", destructive_hint=True)
    tagged = tag_annotations_title(annotations, "playwright")
    assert tagged is not annotations  # copy-on-write
    assert tagged.title == "[playwright] Close browser"


def test_tag_title_preserves_other_hint_fields() -> None:
    annotations = ToolAnnotations(
        title="Read file",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
    tagged = tag_annotations_title(annotations, "fs")
    assert tagged.read_only_hint is True
    assert tagged.destructive_hint is False
    assert tagged.idempotent_hint is True
    assert tagged.open_world_hint is False


def test_tag_title_returns_none_unchanged_when_annotations_is_none() -> None:
    assert tag_annotations_title(None, "playwright") is None


def test_tag_title_passthrough_when_title_missing() -> None:
    annotations = ToolAnnotations(read_only_hint=True)
    tagged = tag_annotations_title(annotations, "Context7")
    assert tagged is annotations


def test_tag_title_passthrough_when_title_empty_string() -> None:
    annotations = ToolAnnotations(title="", read_only_hint=True)
    tagged = tag_annotations_title(annotations, "Context7")
    assert tagged is annotations


def test_tag_title_passthrough_on_unknown_shape() -> None:
    class _Opaque:
        title = "Close browser"

    opaque = _Opaque()
    tagged = tag_annotations_title(opaque, "playwright")
    assert tagged is opaque


def test_tag_title_falls_back_to_original_when_model_copy_raises() -> None:
    class _BrokenCopy:
        title = "Close browser"

        def model_copy(self, update: dict | None = None) -> object:
            raise RuntimeError("simulated copy failure")

    broken = _BrokenCopy()
    tagged = tag_annotations_title(broken, "playwright")
    assert tagged is broken


def test_register_proxy_tool_degrades_when_schema_fields_renamed(caplog) -> None:
    # The .parameters/.fn_metadata overrides poke fastmcp internals; a future
    # fastmcp that renames those pydantic fields raises on assignment. The
    # caller loops register_proxy_tool over every proxied tool with no
    # per-tool guard, so this must degrade to a warning — not abort the
    # whole registration loop and fail server startup.
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from memtomem_stm.proxy._fastmcp_compat import register_proxy_tool

    class _FrozenTool:
        def __setattr__(self, name: str, value: object) -> None:
            raise ValueError(f"pydantic: {name!r} is not a field")

    server = MagicMock()
    server._tool_manager._tools.get.return_value = _FrozenTool()
    info = SimpleNamespace(
        prefixed_name="srv_tool",
        description="desc",
        annotations=None,
        server="srv",
        input_schema={"type": "object"},
    )

    register_proxy_tool(server, lambda: None, info)  # must not raise

    assert "Cannot override schema for 'srv_tool'" in caplog.text
    server.add_tool.assert_called_once()  # the tool itself stayed registered


def test_register_proxy_tool_partial_failure_keeps_default_schema(caplog) -> None:
    # fn_metadata is written FIRST: if that write fails, parameters must not
    # be written either. The reverse partial state — advertising the
    # upstream schema while still validating with the original
    # signature-derived model — would reject the very args the advertised
    # schema invites, which is worse than staying on the default schema.
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from memtomem_stm.proxy._fastmcp_compat import register_proxy_tool

    class _MetadataFrozenTool:
        def __setattr__(self, name: str, value: object) -> None:
            if name == "fn_metadata":
                raise ValueError("pydantic: 'fn_metadata' is not a field")
            object.__setattr__(self, name, value)

    tool = _MetadataFrozenTool()
    server = MagicMock()
    server._tool_manager._tools.get.return_value = tool
    info = SimpleNamespace(
        prefixed_name="srv_tool",
        description="desc",
        annotations=None,
        server="srv",
        input_schema={"type": "object"},
    )

    register_proxy_tool(server, lambda: None, info)

    assert not hasattr(tool, "parameters")  # no partial patch
    assert "Cannot override schema for 'srv_tool'" in caplog.text


# ── result-envelope support: to_call_tool_result / output_schema / meta ──


def test_to_call_tool_result_wraps_str_like_lowlevel_synthesis() -> None:
    from mcp.types import CallToolResult, TextContent

    from memtomem_stm.proxy._fastmcp_compat import to_call_tool_result

    res = to_call_tool_result("hello")
    # Exactly the envelope the lowlevel server synthesizes for a str return.
    assert res == CallToolResult(
        content=[TextContent(type="text", text="hello")],
        structured_content=None,
        is_error=False,
    )


def test_to_call_tool_result_wraps_block_list() -> None:
    from mcp.types import TextContent

    from memtomem_stm.proxy._fastmcp_compat import to_call_tool_result

    blocks = [TextContent(type="text", text="a"), TextContent(type="text", text="b")]
    res = to_call_tool_result(blocks)
    assert res.content == blocks
    assert res.structured_content is None
    assert res.is_error is False


def test_to_call_tool_result_passes_envelope_through_identically() -> None:
    from mcp.types import CallToolResult, TextContent

    from memtomem_stm.proxy._fastmcp_compat import to_call_tool_result

    envelope = CallToolResult(
        content=[TextContent(type="text", text="t")],
        structured_content={"a": 1},
        _meta={"m": 2},
        is_error=True,
    )
    assert to_call_tool_result(envelope) is envelope


def test_proxy_func_metadata_never_validates_call_tool_result() -> None:
    # TRAP regression: the base FuncMetadata.convert_result asserts an
    # output_model whenever output_schema is set — with an upstream schema
    # carried for tools/list advertisement (and no output_model, since none
    # can be built from an arbitrary JSON schema) every proxied call would
    # die on that assert. The subclass must pass the envelope through.
    from mcp.types import CallToolResult

    from memtomem_stm.proxy._fastmcp_compat import _ProxyFuncMetadata, _ProxyPassthroughArgs

    md = _ProxyFuncMetadata(
        arg_model=_ProxyPassthroughArgs,
        output_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
        output_model=None,
        wrap_output=False,
    )
    envelope = CallToolResult(content=[], structured_content={"unvalidated": True})
    assert md.convert_result(envelope) is envelope


class _RecordingTool:
    """Plain-attribute stand-in for the SDK's registered Tool."""


def _register(info):
    from unittest.mock import MagicMock

    from memtomem_stm.proxy._fastmcp_compat import register_proxy_tool

    server = MagicMock()
    tool = _RecordingTool()
    server._tool_manager._tools.get.return_value = tool
    register_proxy_tool(server, lambda: None, info)
    return server, tool


def test_register_passes_meta_kwarg_only_when_set() -> None:
    from types import SimpleNamespace

    base = dict(
        prefixed_name="srv_tool",
        description="desc",
        annotations=None,
        server="srv",
        input_schema={"type": "object"},
        output_schema=None,
    )
    server, _ = _register(SimpleNamespace(**base, meta={"upstream": "meta"}))
    assert server.add_tool.call_args.kwargs["meta"] == {"upstream": "meta"}

    server, _ = _register(SimpleNamespace(**base, meta=None))
    assert "meta" not in server.add_tool.call_args.kwargs  # pre-envelope exact call


def test_register_carries_output_schema_on_fn_metadata() -> None:
    from types import SimpleNamespace

    from memtomem_stm.proxy._fastmcp_compat import _PASSTHROUGH_METADATA, _ProxyFuncMetadata

    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    info = SimpleNamespace(
        prefixed_name="srv_tool",
        description="desc",
        annotations=None,
        server="srv",
        input_schema={"type": "object"},
        output_schema=schema,
        meta=None,
    )
    _, tool = _register(info)
    assert isinstance(tool.fn_metadata, _ProxyFuncMetadata)
    assert tool.fn_metadata.output_schema == schema

    # No upstream outputSchema → the shared singleton (object identity pinned:
    # per-tool instances only when a schema must be carried).
    info.output_schema = None
    _, tool = _register(info)
    assert tool.fn_metadata is _PASSTHROUGH_METADATA


async def test_real_fastmcp_advertises_output_schema_and_meta() -> None:
    # End-to-end through a real MCPServer: tools/list must carry the upstream
    # outputSchema (via the fn_metadata overwrite feeding the Tool.output_schema
    # cached_property) and the tool-level _meta (via add_tool meta=). The
    # handler's bare ``-> CallToolResult`` annotation mirrors the real
    # server.py handler, including that CallToolResult resolves in the
    # DEFINING module's globals — a function-local import NameErrors inside
    # func_metadata and silently degrades registration (caught here first).
    from mcp.server.mcpserver import MCPServer

    from memtomem_stm.proxy._fastmcp_compat import register_proxy_tool
    from memtomem_stm.proxy.manager import ProxyToolInfo

    schema = {"type": "object", "properties": {"answer": {"type": "integer"}}}
    meta = {"origin": "upstream"}

    async def handler(**kwargs: object) -> CallToolResult:
        raise NotImplementedError

    server = MCPServer("test")
    register_proxy_tool(
        server,
        handler,
        ProxyToolInfo(
            prefixed_name="srv__tool",
            description="desc",
            input_schema={"type": "object"},
            server="srv",
            original_name="tool",
            output_schema=schema,
            meta=meta,
        ),
    )
    tools = await server.list_tools()
    (tool,) = [t for t in tools if t.name == "srv__tool"]
    assert tool.output_schema == schema
    assert tool.meta == meta


def test_proxy_tool_info_has_no_execution_field() -> None:
    # #892: the advertisement type carries no ``execution``, which is what
    # makes the synchronous downgrade structural — there is no field for a
    # future edit to fill from an upstream tool, so it cannot regress into
    # forwarding task support the proxy has no call path for. This pins the
    # ABSENCE only; that it survives to a real client is
    # ``test_optional_task_upstream_reaches_the_client_without_execution``.
    from memtomem_stm.proxy.manager import ProxyToolInfo

    assert not hasattr(
        ProxyToolInfo(
            prefixed_name="srv__tool",
            description="desc",
            input_schema={"type": "object"},
            server="srv",
            original_name="tool",
        ),
        "execution",
    )


async def test_proxy_tool_round_trips_over_the_wire() -> None:
    """The registration tests above assert what the shim *writes*; this one
    asserts the SDK still *reads* it.

    ``register_proxy_tool`` overwrites private attributes (``fn_metadata``,
    ``parameters``) on a registered Tool. Those writes can keep succeeding
    while an SDK release quietly stops consulting them — the schema surgery
    would then be a no-op that no attribute-level assertion notices. Driving
    a real client through an in-memory transport is the only shape that
    catches it: it pins the advertised schemas AND that arbitrary kwargs
    (which the signature-derived model would reject) reach the handler.

    Uses ``mcp.client._memory``, a private module — deliberately, and no
    worse than the SDK internals the shim itself rides on.
    """
    from mcp import ClientSession
    from mcp.client._memory import InMemoryTransport
    from mcp.server.mcpserver import MCPServer

    from memtomem_stm.proxy._fastmcp_compat import register_proxy_tool
    from memtomem_stm.proxy.manager import ProxyToolInfo

    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "opts": {"type": "object"}},
        "required": ["query"],
    }
    output_schema = {"type": "object", "properties": {"answer": {"type": "integer"}}}
    seen: dict[str, object] = {}

    async def handler(**kwargs: object) -> CallToolResult:
        seen.update(kwargs)
        # Structured content is required of a tool that advertises an output
        # schema — the mcp 2.0 *client* validates that pairing on receipt.
        return CallToolResult(
            content=[TextContent(type="text", text="ok")],
            structured_content={"answer": 42},
        )

    server = MCPServer("wire-test")
    register_proxy_tool(
        server,
        handler,
        ProxyToolInfo(
            prefixed_name="srv__tool",
            description="desc",
            input_schema=input_schema,
            server="srv",
            original_name="tool",
            output_schema=output_schema,
            meta={"origin": "upstream"},
        ),
    )

    async with InMemoryTransport(server) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            listed = await session.list_tools()
            (tool,) = [t for t in listed.tools if t.name == "srv__tool"]
            assert tool.input_schema == input_schema
            assert tool.output_schema == output_schema

            result = await session.call_tool(
                "srv__tool", {"query": "hello", "opts": {"nested": [1, 2]}}
            )

    assert result.is_error is not True
    assert result.structured_content == {"answer": 42}
    # The signature-derived model would have collapsed these into a single
    # ``kwargs`` field (or rejected them outright).
    assert seen == {"query": "hello", "opts": {"nested": [1, 2]}}
