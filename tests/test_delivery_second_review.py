"""Boundary cases from the agy and Claude review of 4214b1ba."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, TextContent, ImageContent, Tool

from test_structured_delivery import manager, payload
from memtomem_stm.proxy._fastmcp_compat import to_call_tool_result
from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.compression import SelectiveCompressor
from memtomem_stm.proxy.config import CleaningConfig
from memtomem_stm.proxy.delivery import delivery_scope
from memtomem_stm.proxy.metrics import CallMetrics
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.server import stm_proxy_read_more, stm_proxy_select_chunks
from memtomem_stm.surfacing.mcp_client import decode_context_compose_context
from memtomem_stm.utils.redact import ResponseShapeError


@pytest.mark.asyncio
@pytest.mark.parametrize("native", ["structured", "image"])
async def test_native_payload_caps_and_cleans_text_and_accounts_cache(tmp_path, native):
    proxy, session = manager(tmp_path, "truncate")
    proxy._config.upstream_servers["srv"].cleaning = CleaningConfig()
    cache = ProxyCache(tmp_path / "cache.db")
    cache.initialize()
    proxy._cache = cache
    blocks = [TextContent(type="text", text="<b>hello</b> " + "word " * 1600)]
    if native == "image":
        blocks.append(ImageContent(type="image", data="YWJj", mime_type="image/png"))
    session.call_tool.return_value = CallToolResult(
        content=blocks, structured_content={"data": [1, 2]} if native == "structured" else None
    )
    try:
        result = await proxy.call_tool("srv", "read", {})
        delivered = "".join(b.text for b in result.content if b.type == "text")
        assert len(delivered) <= 1600
        assert "<b>" not in delivered
        assert "truncated" in delivered
        assert result.structured_content == session.call_tool.return_value.structured_content
        if native == "image":
            assert result.content[1] == blocks[1]
        summary = proxy.tracker.get_summary()
        assert summary["cache_misses"] == summary["cache_unstorable"] == 1
        assert summary["total_compressed_chars"] == len(delivered)
        assert cache._db.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0] == 0
    finally:
        cache.close()


@pytest.mark.asyncio
async def test_delivery_format_switch_does_not_reuse_legacy_cached_native_body(tmp_path):
    proxy, session = manager(tmp_path, "truncate")
    cfg = proxy._config.upstream_servers["srv"]
    cfg.delivery_format = "legacy"
    cache = ProxyCache(tmp_path / "cache.db")
    cache.initialize()
    proxy._cache = cache
    try:
        session.call_tool.return_value = CallToolResult(
            content=[TextContent(type="text", text="first")], structured_content={"revision": 1}
        )
        await proxy.call_tool("srv", "read", {})
        assert cache._db.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0] == 1
        cfg.delivery_format = "structured"
        session.call_tool.return_value = CallToolResult(
            content=[TextContent(type="text", text="second")], structured_content={"revision": 2}
        )
        result = await proxy.call_tool("srv", "read", {})
        assert session.call_tool.await_count == 2
        assert result.structured_content == {"revision": 2}
    finally:
        cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["native", "budget_error"])
async def test_early_returns_invalidate_disabled_cache(tmp_path, monkeypatch, shape):
    proxy, session = manager(tmp_path, budget=50)
    cache = ProxyCache(tmp_path / "cache.db")
    cache.initialize()
    proxy._cache = cache
    cfg = proxy._config.upstream_servers["srv"]
    cfg.cache_ttl_seconds = 0
    fingerprint = proxy._cache_key_fingerprint("srv", "read", cfg_snap=proxy._config)
    cache.set("srv", "read", {}, "stale", ttl_seconds=None, config_fingerprint=fingerprint)
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="x" * 6000)],
        structured_content={"data": 1} if shape == "native" else None,
    )
    try:
        await proxy.call_tool("srv", "read", {})
        assert cache._db.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0] == 0
    finally:
        cache.close()


@pytest.mark.asyncio
async def test_proxied_budget_error_does_not_override_upstream_output_schema(tmp_path):
    proxy, session = manager(tmp_path, budget=50)
    proxy._connections["srv"].tools = [
        Tool(
            name="read",
            input_schema={"type": "object"},
            output_schema={"type": "object", "required": ["data"]},
        )
    ]
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="x" * 6000)]
    )
    result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))
    assert result.is_error
    assert result.structured_content is None
    assert payload(result)["payload"]["code"] == "budget_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlite", [False, True])
@pytest.mark.parametrize("override", [None, "legacy", "structured"])
async def test_selection_mismatch_follows_stored_format(tmp_path, monkeypatch, sqlite, override):
    proxy, session = manager(tmp_path, "selective", sqlite=sqlite, budget=5000)
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text=json.dumps({"a": "x" * 8000}))]
    )
    key = payload(await proxy.call_tool("srv", "read", {}))["payload"]["key"]
    if sqlite:
        proxy, _ = manager(tmp_path, "selective", sqlite=True, budget=5000)
    monkeypatch.setattr(
        "memtomem_stm.server._get_ctx", lambda ctx: SimpleNamespace(proxy_manager=proxy)
    )
    result = await stm_proxy_select_chunks(key, ["missing"], response_format=override)
    assert bool(result.is_error) == (override != "legacy")
    if override != "legacy":
        assert payload(result)["payload"]["code"] == "no_matching_sections"
    else:
        assert "No matching sections" in result.content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlite", [False, True])
@pytest.mark.parametrize("bad_args", [{"offset": -1}, {"limit": 0}, {"limit": -1}])
async def test_invalid_read_follows_stored_format_without_renewal(
    tmp_path, monkeypatch, sqlite, bad_args
):
    proxy, session = manager(tmp_path, sqlite=sqlite)
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="x" * 6000)]
    )
    key = payload(await proxy.call_tool("srv", "read", {}))["payload"]["key"]
    backend = proxy._progressive_store._store
    before = backend.get(key).created_at
    if sqlite:
        proxy, _ = manager(tmp_path, sqlite=True)
    monkeypatch.setattr(
        "memtomem_stm.server._get_ctx", lambda ctx: SimpleNamespace(proxy_manager=proxy)
    )
    result = await stm_proxy_read_more(key, **bad_args)
    assert result.is_error
    assert payload(result)["payload"]["code"] == "invalid_request"
    assert backend.get(key).created_at == before
    legacy = await stm_proxy_read_more(key, **bad_args, response_format="legacy")
    assert not legacy.is_error
    assert "Error:" in legacy.content[0].text
    assert backend.get(key).created_at == before


@pytest.mark.parametrize("source", ['{"a":NaN,"b":1}', '{"a":1,"a":2,"b":3}', '{"\\ud800":1}'])
def test_legacy_noncanonical_json_keeps_legacy_toc(source):
    with delivery_scope("legacy"):
        fmt, chunks = SelectiveCompressor()._detect_and_parse(source)
    assert fmt == "json"
    assert chunks


@pytest.mark.parametrize(
    "prefix",
    ["[guide](https://example.com)", "[1]: https://example.com", "[![badge](x)](y)", "{intro}"],
)
def test_markdown_prefix_does_not_force_json_progressive(prefix):
    source = prefix + "\n\n## First\nbody\n## Last\nmore"
    with delivery_scope("structured"):
        fmt, chunks = SelectiveCompressor()._detect_and_parse(source)
    assert fmt == "markdown"
    assert "First" in chunks


@pytest.mark.parametrize("identity", ["", "bad\ud800"])
@pytest.mark.parametrize("side", ["before", "after"])
def test_adjacent_chunk_ids_rejected_in_direct_mcp_decode(identity, side):
    raw = {"before": [], "after": [], side: [{"id": identity, "content": "body", "source": "doc"}]}
    with pytest.raises(ResponseShapeError):
        decode_context_compose_context(raw, max_content_chars=100, context_window=1)


def test_historical_structured_bypass_does_not_dilute_tuner(tmp_path):
    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    try:
        store.record(
            CallMetrics(
                server="srv",
                tool="read",
                original_chars=1000,
                cleaned_chars=1000,
                compressed_chars=500,
                compression_strategy="truncate",
                compression_accounting="initial_response_v1",
            )
        )
        for server in ("srv", "bypass_only"):
            store.record(
                CallMetrics(
                    server=server,
                    tool="read",
                    original_chars=500000,
                    compressed_chars=500000,
                    compression_strategy="none→structured_bypass",
                    compression_accounting="initial_response_v1",
                )
            )
        profiles = store.get_tool_profiles()
        assert len(profiles) == 1
        assert profiles[0]["call_count"] == 1
        assert profiles[0]["p95_original_chars"] == 1000
        assert profiles[0]["avg_ratio"] == 0.5
    finally:
        store.close()
