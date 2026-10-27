"""Lossy fallback availability and bounded cross-format read regressions."""

import time
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.types import CallToolResult, TextContent

from test_structured_delivery import manager
from memtomem_stm.proxy._fastmcp_compat import to_call_tool_result
from memtomem_stm.proxy.compression import SelectiveCompressor
from memtomem_stm.proxy.pending_store import InMemoryPendingStore, SQLitePendingStore
from memtomem_stm.proxy.progressive import (
    ProgressiveChunker,
    ProgressiveResponse,
    ProgressiveStoreAdapter,
)
from memtomem_stm.surfacing.mcp_client import decode_context_compose_context


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "structured"])
@pytest.mark.parametrize("strategy", ["truncate", "skeleton"])
@pytest.mark.parametrize("budget", [400, 700, 1600])
async def test_lossy_ratio_ladder_keeps_success_when_progressive_cannot_fit(
    tmp_path, monkeypatch, mode, strategy, budget
):
    proxy, session = manager(tmp_path, strategy, budget=budget)
    proxy._config.upstream_servers["srv"].delivery_format = mode
    proxy._config.min_result_retention = 0.5
    source = "\n".join(f"line {i} of the document body" for i in range(300))
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    monkeypatch.setattr(proxy, "_apply_compression", AsyncMock(return_value=("short", None)))
    result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))
    assert not result.is_error
    assert result.content[0].text != "short"
    if mode == "structured" and budget < 1600:
        assert "stm" not in (result.structured_content or {})
        assert proxy._progressive_store is None or len(proxy._progressive_store._store) == 0


def test_whitespace_preamble_is_omitted_without_moving_source_ranges():
    source = "\n \t\n# A\nbody a\n# B\n# C\nbody c\n"
    chunks = SelectiveCompressor()._parse_markdown(source)
    assert list(chunks) == ["A", "B", "C"]
    for key, content in chunks.items():
        record = chunks.records[key]
        assert content == source[record["start"] : record["end"]]
    assert chunks["B"] == "# B\n"


@pytest.mark.parametrize("identity", ["", "bad\ud800"])
@pytest.mark.parametrize("side", ["before", "after"])
def test_bad_adjacent_identity_drops_only_that_neighbor(identity, side):
    neighbors = [
        {"id": "valid-1", "content": "first", "source": "doc"},
        {"id": identity, "content": "invalid", "source": "doc"},
        {"id": "valid-2", "content": "last", "source": "doc"},
    ]
    context = decode_context_compose_context(
        {side: neighbors, "chunk_position": 3, "total_chunks_in_file": 8},
        max_content_chars=100,
        context_window=3,
    )
    retained = context.window_before if side == "before" else context.window_after
    assert [chunk.id for chunk in retained] == ["valid-1", "valid-2"]
    assert context.chunk_position == 3
    assert context.total_chunks_in_file == 8


@pytest.mark.parametrize("sqlite", [False, True])
def test_legacy_structured_classification_is_cached_across_reads_and_restart(
    tmp_path, monkeypatch, sqlite
):
    path = tmp_path / "progressive.db"
    store = SQLitePendingStore(path) if sqlite else InMemoryPendingStore()
    if sqlite:
        store.initialize()
    adapter = ProgressiveStoreAdapter(store)
    clock = time.time if sqlite else time.monotonic
    detect = Mock(wraps=ProgressiveChunker.detect_content_type)
    monkeypatch.setattr(ProgressiveChunker, "detect_content_type", detect)

    def publish(content):
        adapter.put(
            "key",
            ProgressiveResponse(
                content=content,
                total_chars=len(content),
                total_lines=0,
                content_type="text",
                structure_hint="",
                created_at=clock(),
                include_structure_hint=False,
            ),
        )

    try:
        publish('{"body":"' + "x" * 2_000_000 + '"}')
        assert adapter.get("key").content_type == "text"
        detect.assert_not_called()
        first = adapter.get("key", renew=True, response_format="structured")
        assert first.content_type == "json"
        if sqlite:
            adapter.close()
            store = SQLitePendingStore(path)
            store.initialize()
            adapter = ProgressiveStoreAdapter(store)
        for _ in range(3):
            more = adapter.get("key", response_format="structured")
            assert more.content_type == "json"
            assert more.created_at == first.created_at
        assert detect.call_count == 1
        publish("# Changed\nbody\n")
        assert adapter.get("key", response_format="structured").content_type == "markdown"
        assert detect.call_count == 2
    finally:
        adapter.close()


@pytest.mark.parametrize("sqlite", [False, True])
def test_expired_legacy_read_does_not_classify_or_renew(tmp_path, monkeypatch, sqlite):
    store = SQLitePendingStore(tmp_path / "expired.db") if sqlite else InMemoryPendingStore()
    if sqlite:
        store.initialize()
    adapter = ProgressiveStoreAdapter(store)
    now = 1000.0
    monkeypatch.setattr(
        "memtomem_stm.proxy.pending_store.time." + ("time" if sqlite else "monotonic"),
        lambda: now,
    )
    adapter.put(
        "expired",
        ProgressiveResponse(
            content="# Old\nbody",
            total_chars=10,
            total_lines=0,
            content_type="text",
            structure_hint="",
            created_at=now,
            ttl_seconds=1,
            include_structure_hint=False,
        ),
    )
    detect = Mock(wraps=ProgressiveChunker.detect_content_type)
    monkeypatch.setattr(ProgressiveChunker, "detect_content_type", detect)
    now += 100
    try:
        assert adapter.get("expired", renew=True, response_format="structured") is None
        detect.assert_not_called()
        assert len(store) == 0
    finally:
        adapter.close()
