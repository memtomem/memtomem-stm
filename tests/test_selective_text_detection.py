"""Non-JSON prefixes retain paragraph selection without invented provenance."""

import pytest
from mcp.types import CallToolResult, TextContent

from test_structured_delivery import manager, payload
from memtomem_stm.proxy._fastmcp_compat import to_call_tool_result
from memtomem_stm.proxy.compression import SelectiveCompressor
from memtomem_stm.proxy.config import HybridConfig
from memtomem_stm.proxy.delivery import delivery_scope
from memtomem_stm.proxy.pending_store import InMemoryPendingStore, SQLitePendingStore


def paragraphs(kind):
    bodies = [
        f"Paragraph {i}: " + " ".join(["processed batch with a detailed description"] * 18)
        for i in range(12)
    ]
    if kind == "logs":
        return [f"[2026-10-27 10:{i:02d}:00] INFO {body}" for i, body in enumerate(bodies)]
    prefix = {
        "badge": "[![badge](https://example.com/status.svg)](https://example.com)",
        "brace": "{status} template output",
        "incomplete_json": '{"message": "incomplete source',
    }[kind]
    bodies[0] = prefix + "\n" + bodies[0]
    return bodies


@pytest.mark.parametrize("kind", ["logs", "badge", "brace", "incomplete_json"])
@pytest.mark.parametrize("mode", ["legacy", "structured"])
def test_unparsed_prefix_uses_paragraph_detection(kind, mode):
    expected = paragraphs(kind)
    with delivery_scope(mode):
        fmt, chunks = SelectiveCompressor()._detect_and_parse("\n\n".join(expected))
    assert fmt == "text"
    assert list(chunks.values()) == expected
    assert list(chunks) == [f"Paragraph {i + 1}" for i in range(len(expected))]
    assert not getattr(chunks, "records", {})


@pytest.mark.parametrize("kind", ["logs", "badge", "brace", "incomplete_json"])
@pytest.mark.parametrize("sqlite", [False, True])
def test_plain_text_toc_and_restart_do_not_claim_json_or_source_ranges(tmp_path, kind, sqlite):
    path = tmp_path / "pending.db"
    store = SQLitePendingStore(path) if sqlite else InMemoryPendingStore()
    if sqlite:
        store.initialize()
    compressor = SelectiveCompressor(store=store)
    expected = paragraphs(kind)
    try:
        with delivery_scope("structured", 16000):
            result = compressor.compress("\n\n".join(expected), max_chars=2000)
        desc = result.descriptor
        assert desc["kind"] == "selective"
        initial = desc["payload"]
        assert initial["source_format"] == "text"
        assert initial["metadata_complete"] is False
        assert len(initial["entries"]) == len(expected)
        for entry in initial["entries"]:
            assert not {"start", "end", "json_path"}.intersection(entry)
        if sqlite:
            compressor.close()
            store = SQLitePendingStore(path)
            store.initialize()
            compressor = SelectiveCompressor(store=store)
        selected = compressor.select(
            initial["key"], ["Paragraph 2"], response_format="structured"
        ).descriptor["payload"]
        assert selected["metadata_complete"] is False
        assert selected["chunks"][0]["content"] == expected[1]
        assert not {"start", "end", "json_path"}.intersection(selected["chunks"][0])
    finally:
        compressor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["logs", "badge", "brace", "incomplete_json"])
@pytest.mark.parametrize("strategy", ["selective", "hybrid"])
async def test_non_json_document_keeps_manager_section_selection(tmp_path, kind, strategy):
    source = "\n\n".join(paragraphs(kind))
    proxy, session = manager(tmp_path, strategy, budget=6000)
    proxy._config.upstream_servers["srv"].hybrid = HybridConfig(head_chars=1000)
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))
    assert not result.is_error
    desc = payload(result)
    assert desc["kind"] == strategy
    assert desc["payload"]["source_format"] == "text"
    assert desc["payload"]["metadata_complete"] is False
    selected = proxy.select_chunks(desc["payload"]["key"], ["Paragraph 2"])
    assert selected.descriptor["payload"]["chunks"]
    assert proxy._progressive_store is None
