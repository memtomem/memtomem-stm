"""Source provenance, wire accounting and SDK compatibility."""

import builtins
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.types import CallToolResult, TextContent

from test_structured_delivery import manager, payload
from memtomem_stm.proxy._fastmcp_compat import install_delivery_wire_context, to_call_tool_result
from memtomem_stm.proxy.compression import HybridCompressor, SelectiveCompressor
from memtomem_stm.proxy.config import HybridConfig
from memtomem_stm.proxy.delivery import delivery_scope, wire_projection_scope
from memtomem_stm.proxy.pending_store import InMemoryPendingStore, SQLitePendingStore
from memtomem_stm.proxy.progressive import ProgressiveChunker
from memtomem_stm.proxy.progressive_reads import ProgressiveReadsTracker
from memtomem_stm.server import stm_proxy_select_chunks, _continuation_result


def document():
    return "".join(f"## Sec{i}\n" + (f"body{i} 한글 😀 " * 85) + "\n" for i in range(5))


@pytest.mark.parametrize("sqlite", [False, True])
def test_legacy_hybrid_structured_read_has_full_source_ranges(tmp_path, sqlite):
    path = tmp_path / "pending.db"
    store = SQLitePendingStore(path) if sqlite else InMemoryPendingStore()
    if sqlite:
        store.initialize()
    selective = SelectiveCompressor(store=store)
    source = document()
    with delivery_scope("legacy", 16000):
        initial = HybridCompressor(head_chars=500, selective_compressor=selective).compress(
            source, max_chars=2200
        )
    key = initial.descriptor["payload"]["key"]
    if sqlite:
        store.close()
        store = SQLitePendingStore(path)
        store.initialize()
        selective = SelectiveCompressor(store=store)
    try:
        result = selective.select(key, ["Sec2"], response_format="structured").descriptor["payload"]
        chunk = result["chunks"][0]
        assert result["metadata_complete"]
        assert result["source_offset"] > 0
        assert source[chunk["start"] : chunk["end"]] == chunk["content"]
        assert source[chunk["start"] :].startswith("## Sec2\n")
    finally:
        selective.close()


@pytest.mark.asyncio
async def test_legacy_progressive_defers_analysis_until_structured_override(tmp_path, monkeypatch):
    proxy, session = manager(tmp_path)
    proxy._config.upstream_servers["srv"].delivery_format = "legacy"
    source = json.dumps({"body": "x" * 8000})
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    detect = Mock(wraps=ProgressiveChunker.detect_content_type)
    hint = Mock(wraps=ProgressiveChunker.structure_hint)
    monkeypatch.setattr(ProgressiveChunker, "detect_content_type", detect)
    monkeypatch.setattr(ProgressiveChunker, "structure_hint", hint)
    result = await proxy.call_tool("srv", "read", {})
    detect.assert_not_called()
    hint.assert_not_called()
    key = (
        result.descriptor["payload"]["key"]
        if hasattr(result, "descriptor")
        else next(iter(proxy._progressive_store._store._data))
    )
    more = proxy.read_more(key, 1000, response_format="structured")
    assert more.descriptor["payload"]["source_format"] == "json"
    assert detect.call_count == 1
    hint.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_ordinary_result_does_not_project_whole_native_payload(tmp_path):
    proxy, session = manager(tmp_path, "truncate", budget=16000)
    proxy._config.upstream_servers["srv"].delivery_format = "legacy"
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="body " * 2000)],
        structured_content={"large": "x" * 100000},
        _meta={"origin": "y" * 1000},
    )
    project = Mock(side_effect=lambda data: data)
    with wire_projection_scope(project):
        result = await proxy.call_tool("srv", "read", {})
    assert result.structured_content == {"large": "x" * 100000}
    project.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("track", [False, True])
async def test_legacy_progressive_measures_wire_only_for_enabled_tracker(tmp_path, track):
    proxy, session = manager(tmp_path)
    proxy._config.upstream_servers["srv"].delivery_format = "legacy"
    tracker = ProgressiveReadsTracker(tmp_path / "reads.db", retention_days=0) if track else None
    proxy._progressive_reads_tracker = tracker
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="x" * 8000)]
    )
    project = Mock(side_effect=lambda data: data)
    try:
        with wire_projection_scope(project):
            result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))
        assert not result.is_error
        assert project.call_count == int(track)
        if tracker:
            assert tracker.store.get_stats()["initial_wire_bytes"] > 0
    finally:
        if tracker:
            tracker.close()


@pytest.mark.asyncio
async def test_structured_index_and_call_metrics_use_same_serialized_base(tmp_path, monkeypatch):
    proxy, session = manager(tmp_path)
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="x" * 8000)], _meta={"note": "y" * 100}
    )
    stage = AsyncMock(wraps=proxy._run_index_stage)
    record = Mock(wraps=proxy.tracker.record)
    monkeypatch.setattr(proxy, "_run_index_stage", stage)
    monkeypatch.setattr(proxy.tracker, "record", record)
    result = await proxy.call_tool("srv", "read", {})
    assert payload(result)["kind"] == "progressive"
    assert (
        stage.call_args.kwargs["compressed_chars_for_metrics"]
        == record.call_args.args[0].compressed_chars
    )


@pytest.mark.asyncio
async def test_structured_lossy_ratio_ladder_does_not_publish_rejected_hybrid(
    tmp_path, monkeypatch
):
    proxy, session = manager(tmp_path, "skeleton", budget=3500)
    cfg = proxy._config.upstream_servers["srv"]
    cfg.progressive.chunk_size = 100000
    cfg.hybrid = HybridConfig(head_chars=200, min_head_chars=100, min_toc_budget=300)
    proxy._config.min_result_retention = 0.95
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text=document())]
    )
    monkeypatch.setattr(proxy, "_apply_compression", AsyncMock(return_value=("short", None)))
    result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))
    assert not result.is_error
    if proxy._selective_compressor is not None:
        assert len(proxy._selective_compressor._store) == 0
    assert proxy._progressive_store is None


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["legacy", "structured"])
@pytest.mark.parametrize("track", [False, True])
@pytest.mark.parametrize("missing", ["module", "serializer"])
async def test_sdk_projection_import_failure_keeps_legacy_and_refuses_unmeasured_structured(
    tmp_path, monkeypatch, format, track, missing
):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("compat")
    original = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if missing == "module" and name == "mcp_types.version":
            raise ImportError("simulated SDK move")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    if missing == "serializer":
        monkeypatch.delattr("mcp_types.methods.serialize_server_result")
    install_delivery_wire_context(server)
    proxy, session = manager(tmp_path)
    proxy._config.upstream_servers["srv"].delivery_format = format
    tracker = (
        ProgressiveReadsTracker(tmp_path / "compat-reads.db", retention_days=0) if track else None
    )
    proxy._progressive_reads_tracker = tracker
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="x" * 8000)]
    )

    async def call(ctx):
        return to_call_tool_result(await proxy.call_tool("srv", "read", {}))

    try:
        result = await server.middleware[-1](SimpleNamespace(method="tools/call"), call)
        assert bool(result.is_error) == (format == "structured")
        if format == "structured":
            assert result.structured_content is None
            assert payload(result)["payload"]["code"] == "unsupported_sdk_wire_format"
        else:
            key = next(iter(proxy._progressive_store._store._data))

            async def more(ctx):
                return _continuation_result(proxy.read_more(key, 1000), None)

            followup = await server.middleware[-1](SimpleNamespace(method="tools/call"), more)
            assert not followup.is_error
            assert followup.content[0].text.startswith("x")
    finally:
        if tracker:
            tracker.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_selection_failure_distinguishes_inactive_and_live_mismatch(
    tmp_path, monkeypatch, active
):
    proxy, session = manager(tmp_path, "selective", budget=5000)
    key = "unknown"
    if active:
        session.call_tool.return_value = CallToolResult(
            content=[TextContent(type="text", text=json.dumps({"a": "x" * 8000}))]
        )
        key = payload(await proxy.call_tool("srv", "read", {}))["payload"]["key"]
    monkeypatch.setattr(
        "memtomem_stm.server._get_ctx", lambda ctx: SimpleNamespace(proxy_manager=proxy)
    )
    result = await stm_proxy_select_chunks(key, ["missing"], response_format="structured")
    assert result.is_error
    assert payload(result)["payload"]["code"] == (
        "no_matching_sections" if active else "unavailable"
    )
