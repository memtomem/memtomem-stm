"""Pre-publication fitting, safe fallbacks and delivery accounting guards."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.types import CallToolResult, TextContent

from test_structured_delivery import manager, payload
from memtomem_stm.proxy._fastmcp_compat import to_call_tool_result
from memtomem_stm.proxy.compression import (
    HybridCompressor,
    PendingSelection,
    SelectiveCompressor,
    SensitivePayloadNotStored,
)
from memtomem_stm.proxy.config import TailMode
from memtomem_stm.proxy.delivery import DeliveryBudgetExceeded, delivery_scope
from memtomem_stm.proxy.pending_store import InMemoryPendingStore
from memtomem_stm.server import _continuation_result


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["progressive", "selective", "hybrid"])
async def test_impossible_structured_budget_returns_typed_error(tmp_path, strategy):
    proxy, session = manager(tmp_path, strategy, budget=50)
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="x" * 6000)]
    )
    result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))
    assert result.is_error
    assert payload(result)["payload"]["code"] == "budget_exceeded"
    if proxy._progressive_store is not None:
        assert len(proxy._progressive_store._store) == 0


@pytest.mark.asyncio
async def test_single_source_chunk_still_obeys_serialized_budget(tmp_path):
    proxy, session = manager(tmp_path, budget=1600)
    proxy._config.upstream_servers["srv"].progressive.chunk_size = 4000
    source = '😀 "value"\n' * 250
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))
    assert payload(result)["kind"] == "progressive"
    pieces = [payload(result)["payload"]["content"]]
    next_chunk = payload(result)["payload"]
    while next_chunk["has_more"]:
        more = proxy.read_more(next_chunk["key"], next_chunk["next_offset"])
        wire = _continuation_result(more, None)
        assert len(wire.model_dump_json(by_alias=True, exclude_none=True)) <= 1600
        next_chunk = more.descriptor["payload"]
        pieces.append(next_chunk["content"])
    assert "".join(pieces) == source


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["selective", "hybrid"])
@pytest.mark.parametrize(
    "failure", [sqlite3.OperationalError("locked"), SensitivePayloadNotStored("refused")]
)
async def test_budget_fallback_store_errors_reach_safe_degradation(
    tmp_path, monkeypatch, strategy, failure
):
    proxy, session = manager(tmp_path, strategy)
    source = '{"a":1,"a":2,"body":"' + "x" * 8000 + '"}'
    if strategy == "hybrid":
        # Force the real pre-publication structured TOC floor failure first.
        source = "".join(f"## Heading {i}\nbody {i}\n" for i in range(180))
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    fallback = Mock(side_effect=failure)
    monkeypatch.setattr(proxy, "_apply_progressive", fallback)
    record = Mock(wraps=proxy.tracker.record)
    monkeypatch.setattr(proxy.tracker, "record", record)
    result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))
    assert not result.is_error
    fallback.assert_called_once()
    assert len(result.content[0].text) <= 1600
    suffix = "sensitive" if isinstance(failure, SensitivePayloadNotStored) else "store_error"
    assert record.call_args.args[0].compression_strategy == f"{strategy}→truncate_on_{suffix}"


@pytest.mark.asyncio
async def test_budget_fallback_uses_progressive_surfacing_and_metrics(tmp_path, monkeypatch):
    proxy, session = manager(tmp_path, "selective")
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text='{"a":1,"a":2,"body":"' + "x" * 6000 + '"}')]
    )
    progressive = AsyncMock(return_value=("", True, None))
    ordinary = AsyncMock(return_value="")
    monkeypatch.setattr(proxy, "_apply_surfacing_on_progressive", progressive)
    monkeypatch.setattr(proxy, "_apply_surfacing", ordinary)
    record = Mock(wraps=proxy.tracker.record)
    monkeypatch.setattr(proxy.tracker, "record", record)
    result = await proxy.call_tool("srv", "read", {})
    assert payload(result)["kind"] == "progressive"
    progressive.assert_awaited_once()
    ordinary.assert_not_awaited()
    event = record.call_args.args[0]
    assert event.compression_strategy == "selective→progressive_fallback"
    assert event.surfacing_on_progressive_ok is True


@pytest.mark.parametrize("budget", [10, 100])
def test_hybrid_toc_does_not_silently_emit_truncated_legacy_text(budget):
    with delivery_scope("structured", budget), pytest.raises(DeliveryBudgetExceeded):
        HybridCompressor().compress("document " * 2000, max_chars=budget)


def test_hybrid_validates_full_frame_before_publication_or_eviction():
    store = InMemoryPendingStore()
    store.put("existing", PendingSelection({"x": "keep"}, "text", time.monotonic(), 4))
    selective = SelectiveCompressor(store=store, max_pending=1)
    hybrid = HybridCompressor(head_chars=700, selective_compressor=selective)
    source = "## First\n" + "a" * 2000 + "\n## Last\n" + "b" * 2000
    with delivery_scope("structured", 1400), pytest.raises(DeliveryBudgetExceeded):
        hybrid.compress(source, max_chars=1400)
    assert store.get("existing") is not None
    assert len(store) == 1


def test_structured_previews_use_complete_frame_budget_not_legacy_tail_allowance():
    source = json.dumps({"one": "a" * 1200, "two": "b" * 1200})
    with delivery_scope("structured", 4000):
        result = SelectiveCompressor().compress(source, max_chars=500)
    assert all(len(e["preview"]) == 80 for e in result.descriptor["payload"]["entries"])


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["before", "after"])
async def test_actual_daemon_context_wire_keys_validate_raw_identities(field):
    from memtomem_stm.daemon.protocol import ProtocolError, read_message, encode_line

    frame = {
        "ok": True,
        "retrieved": [{"chunk_id": "valid", "context": {field: [{"id": "bad\ud800"}]}}],
    }
    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps(frame) + "\n").encode())
    reader.feed_eof()
    with pytest.raises(ProtocolError):
        await read_message(reader)
    with pytest.raises(ProtocolError):
        encode_line(frame)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, ""])
async def test_daemon_compose_normalizes_nullable_metadata_without_empty_identity(
    monkeypatch, value
):
    from memtomem_stm.config import STMConfig
    from memtomem_stm.surfacing.daemon_adapter import DaemonLtmAdapter, client

    adapter = DaemonLtmAdapter(STMConfig())
    adapter._compose_schema = 3
    response = {
        "ok": True,
        "selected_context_compose_schema": 3,
        "pinned": [],
        "retrieved": [
            {"content": "body", "score": 0.5, "source": value, "namespace": value, "chunk_id": ""}
        ],
        "warnings": [],
        "omitted_block_ids": [],
    }
    monkeypatch.setattr(client, "ltm_request", AsyncMock(return_value=("ok", response)))
    result = await adapter.context_compose("query")
    assert result.retrieved[0].chunk.id.startswith("stm:compact:v2:")
    assert result.retrieved[0].chunk.metadata.namespace == "default"


@pytest.mark.asyncio
async def test_initial_key_is_not_issued_if_owned_continuation_cannot_progress(tmp_path):
    proxy, session = manager(tmp_path, budget=500)
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="x" * 6000)]
    )
    result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))
    assert result.is_error
    assert payload(result)["payload"]["code"] == "budget_exceeded"
    assert proxy._progressive_store is None or len(proxy._progressive_store._store) == 0


@pytest.mark.asyncio
async def test_continuation_uses_full_tool_budget_without_initial_upstream_metadata(tmp_path):
    proxy, session = manager(tmp_path, budget=1600)
    source = "x" * 2400
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text=source)], _meta={"note": "m" * 1100}
    )
    initial = to_call_tool_result(await proxy.call_tool("srv", "read", {}))
    assert not initial.is_error
    assert len(initial.model_dump_json(by_alias=True, exclude_none=True)) <= 1600
    next_chunk = payload(initial)["payload"]
    assert proxy._progressive_store.get(next_chunk["key"]).delivery_budget == 1600
    pieces = [next_chunk["content"]]
    while next_chunk["has_more"]:
        more = proxy.read_more(next_chunk["key"], next_chunk["next_offset"])
        assert (
            len(_continuation_result(more, None).model_dump_json(by_alias=True, exclude_none=True))
            <= 1600
        )
        next_chunk = more.descriptor["payload"]
        pieces.append(next_chunk["content"])
    assert "".join(pieces) == source


@pytest.mark.parametrize("title", ["\u00a0Title", "\u3000Title"])
def test_heading_index_preserves_unicode_title_prefix_and_source_span(title):
    from memtomem_stm.proxy.markdown_structure import headings
    from memtomem_stm.proxy.heading_index import build_heading_index, remaining_headings

    source = "# " + title + "\nbody\n"
    heading = next(headings(source))
    assert source[heading.title_start : heading.end] == title == heading.title
    assert remaining_headings(source, 0, build_heading_index(source)) == json.dumps(
        title, ensure_ascii=False
    )


def test_explicit_truncate_tail_is_ordinary_compression_in_both_formats():
    source = "document line\n" * 600
    compressor = HybridCompressor(head_chars=500, tail_mode=TailMode.TRUNCATE)
    legacy = compressor.compress(source, max_chars=1500)
    with delivery_scope("structured", 1500):
        structured = compressor.compress(source, max_chars=1500)
    assert structured == legacy
    assert not hasattr(structured, "descriptor")


@pytest.mark.asyncio
async def test_explicit_truncate_tail_keeps_configured_retention_floor(tmp_path, monkeypatch):
    from memtomem_stm.proxy.config import HybridConfig

    source = "line-content\n" * 1000
    budgets = []
    for mode in ("legacy", "structured"):
        proxy, session = manager(tmp_path, "hybrid", budget=4000)
        cfg = proxy._config.upstream_servers["srv"]
        cfg.delivery_format = mode
        cfg.hybrid = HybridConfig(tail_mode=TailMode.TRUNCATE)
        proxy._config.min_result_retention = 0.65
        session.call_tool.return_value = CallToolResult(
            content=[TextContent(type="text", text=source)]
        )
        compress = AsyncMock(wraps=proxy._apply_compression)
        monkeypatch.setattr(proxy, "_apply_compression", compress)
        await proxy.call_tool("srv", "read", {})
        budgets.append((compress.call_args.args[2], compress.call_args.kwargs["min_chars"]))
    expected = int(len(source) * 0.65)
    assert budgets == [(expected, expected), (expected, expected)]


def test_structured_wire_measurements_do_not_enter_content_retention_ratios(tmp_path):
    from memtomem_stm.proxy.metrics import CallMetrics
    from memtomem_stm.proxy.metrics_store import MetricsStore, read_compression_summary

    path = tmp_path / "metrics.db"
    store = MetricsStore(path)
    store.initialize()
    try:
        store.record(
            CallMetrics(
                server="srv",
                tool="read",
                original_chars=1000,
                cleaned_chars=1000,
                compressed_chars=1800,
                compression_accounting="initial_structured_v1",
                delivery_format="structured",
            )
        )
        profile = store.get_tool_profiles()[0]
        assert profile["avg_ratio"] is None and profile["ratio_count"] == 0
        store.record(
            CallMetrics(
                server="srv",
                tool="read",
                original_chars=1000,
                cleaned_chars=1000,
                compressed_chars=500,
                compression_accounting="initial_response_v1",
            )
        )
        profile = store.get_tool_profiles()[0]
        assert profile["avg_ratio"] == 0.5 and profile["ratio_count"] == 1
        assert read_compression_summary(path)["structured_delivery"]["calls"] == 1
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["selective", "hybrid"])
async def test_actual_sqlite_privacy_refusal_after_budget_fallback_stays_uncached(
    tmp_path, strategy
):
    from memtomem_stm.proxy.cache import ProxyCache

    proxy, session = manager(tmp_path, strategy, sqlite=True)
    proxy._config.upstream_servers["srv"].cache = True
    cache = ProxyCache(tmp_path / "cache.db")
    cache.initialize()
    proxy._cache = cache
    sensitive = 'api_key="sk-abcdefghijklmnopqrstuvwxyz123456"'
    if strategy == "selective":
        source = '{"a":1,"a":2,"body":' + json.dumps(sensitive + "x" * 8000) + "}"
    else:
        source = sensitive + "\n" + "".join(f"## Heading {i}\nbody {i}\n" for i in range(180))
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    try:
        for _ in range(2):
            result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))
            assert not result.is_error
        assert session.call_tool.await_count == 2
        assert cache._db.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0] == 0
        if proxy._progressive_store is not None:
            assert len(proxy._progressive_store._store) == 0
    finally:
        cache.close()
        if proxy._progressive_store is not None:
            proxy._progressive_store.close()
        if proxy._selective_compressor is not None:
            proxy._selective_compressor.close()
