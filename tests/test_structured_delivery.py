"""Delivery contracts: real proxy calls, restart recovery and MCP validation."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent
from mcp.server.mcpserver.utilities.func_metadata import func_metadata

from memtomem_stm.proxy.config import (
    CleaningConfig,
    ProgressiveConfig,
    ProxyConfig,
    SelectiveConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.delivery import (
    DeliveryError,
    DeliveryText,
    delivery_result,
    delivery_scope,
    delivery_size,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.pending_store import SQLitePendingStore
from memtomem_stm.proxy.compression import PendingSelection, SelectiveCompressor
from memtomem_stm.proxy.progressive import ProgressiveResponse, ProgressiveStoreAdapter
from memtomem_stm.server import stm_proxy_read_more, stm_proxy_select_chunks, _continuation_result


def manager(tmp_path, strategy="progressive", *, sqlite=False, budget=1600):
    selective = (
        SelectiveConfig(pending_store="sqlite", pending_store_path=tmp_path / "pending.db")
        if sqlite
        else None
    )
    upstream = UpstreamServerConfig(
        prefix="doc",
        compression=strategy,
        max_result_chars=budget,
        delivery_format="structured",
        cleaning=CleaningConfig(enabled=False),
        progressive=ProgressiveConfig(chunk_size=1000),
        selective=selective,
    )
    cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": upstream},
        min_result_retention=0,
    )
    proxy = ProxyManager(cfg, TokenTracker())
    session = AsyncMock()
    proxy._connections["srv"] = UpstreamConnection(
        name="srv", config=upstream, session=session, tools=[]
    )
    return proxy, session


def payload(result):
    return json.loads(result.content[0].text)["stm"]


@pytest.mark.asyncio
async def test_proxy_progressive_roundtrip_and_actual_whole_base_budget(tmp_path):
    proxy, session = manager(tmp_path)
    source = "before\n---\n[progressive: chars=example]\nafter\n" + "\n".join(
        f'{i} 😀 한글 \\"quoted\\"' for i in range(350)
    )
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    initial = await proxy.call_tool("srv", "read", {})
    desc = payload(initial)
    assert delivery_size(desc, owned=False) <= 1600
    pieces = [desc["payload"]["content"]]
    while desc["payload"]["next_offset"] is not None:
        p = desc["payload"]
        more = proxy.read_more(p["key"], p["next_offset"])
        assert isinstance(more, DeliveryText)
        desc = more.descriptor
        wire = _continuation_result(more, None)
        assert len(wire.model_dump_json(by_alias=True, exclude_none=True)) <= 1600
        assert wire.structured_content["stm"] == desc
        pieces.append(desc["payload"]["content"])
    assert "".join(pieces) == source
    assert desc["offset_unit"] == "unicode_codepoint"


@pytest.mark.asyncio
async def test_selective_paths_types_and_restart_default_format(tmp_path):
    proxy, session = manager(tmp_path, "selective", sqlite=True, budget=5000)
    source = json.dumps(
        {"a.b": True, "a": {"b": None}, "empty": {}, "rows": [1, 2], "large": "x" * 7000}
    )
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    initial = payload(await proxy.call_tool("srv", "read", {}))
    assert initial["kind"] == "selective"
    entries = {e["key"]: e for e in initial["payload"]["entries"]}
    assert entries["a.b"]["json_path"] == ["a.b"]
    assert entries["a.b"]["value_type"] == "boolean"
    assert entries["a.b#2"]["json_path"] == ["a", "b"]
    assert entries["a.b#2"]["value_type"] == "null"
    assert entries["empty"]["value_type"] == "object"
    restarted, _ = manager(tmp_path, "selective", sqlite=True, budget=5000)
    result = restarted.select_chunks(initial["payload"]["key"], ["a.b", "a.b#2", "empty"])
    assert result.response_format == "structured"
    assert [r["content"] for r in result.descriptor["payload"]["chunks"]] == ["true", "null", "{}"]


@pytest.mark.asyncio
async def test_upstream_structured_payload_is_preserved_without_stm_pointer(tmp_path):
    proxy, session = manager(tmp_path)
    upstream = CallToolResult(
        content=[TextContent(type="text", text="x" * 6000)], structured_content={"data": [1, 2]}
    )
    session.call_tool.return_value = upstream
    result = await proxy.call_tool("srv", "read", {})
    assert result.structured_content == {"data": [1, 2]}
    assert result.content[0].text == upstream.content[0].text
    assert proxy._progressive_store is None


@pytest.mark.parametrize("handler", [stm_proxy_read_more, stm_proxy_select_chunks])
def test_advertised_output_and_both_wire_modes(handler):
    metadata = func_metadata(handler)
    assert metadata.output_schema["required"] == ["result"]
    assert "stm" in metadata.output_schema["properties"]
    old = _continuation_result("body", None)
    assert old.structured_content == {"result": "body"}
    assert metadata.convert_result(old) is old
    new = delivery_result({"schema_version": 1, "kind": "selection", "payload": {"chunks": []}})
    assert metadata.convert_result(new) is new
    with pytest.raises(ValueError):
        metadata.convert_result(CallToolResult(content=[], structured_content={"stm": {}}))


@pytest.mark.parametrize("bad", [None, [], {"a": 12}])
def test_valid_json_is_not_sufficient_for_pending_row(bad):
    assert SQLitePendingStore._decode_row("k", (json.dumps(bad), "json", time.time(), 3)) is None


def test_unknown_progressive_delivery_version_neither_renews_nor_deletes(tmp_path):
    store = SQLitePendingStore(tmp_path / "pending.db")
    store.initialize()
    store.put(
        "key",
        PendingSelection(
            {
                "__content__": "body",
                "__meta__": json.dumps(
                    {
                        "ttl_seconds": 1800,
                        "delivery": {"schema_version": 999, "delivery_format": "structured"},
                    }
                ),
            },
            "progressive",
            time.monotonic(),
            4,
        ),
    )
    before = store.get("key").created_at
    adapter = ProgressiveStoreAdapter(store)
    with pytest.raises(DeliveryError):
        adapter.get("key", renew=True)
    assert store.get("key").created_at == before
    assert adapter.get("key", renew=True, response_format="legacy").content == "body"
    store.close()


def test_markdown_keeps_fences_headings_whitespace_and_ranges():
    text = "intro\n\n## Example\n\n```python\n# code\nprint(1)\n```\n\n## End\nfinish\n"
    compressor = SelectiveCompressor()
    fmt, chunks = compressor._detect_and_parse(text)
    assert fmt == "markdown"
    assert list(chunks) == ["Preamble", "Example", "End"]
    assert "".join(chunks.values()) == text
    assert chunks.records["Example"]["heading"] == {"title": "Example", "level": 2}


@pytest.mark.parametrize("text", ['{"a":NaN}', '{"a":1,"a":2}', '{"\\ud800":1}'])
def test_noncanonical_json_routes_to_lossless_progressive(text):
    with delivery_scope("structured"), pytest.raises(DeliveryError):
        SelectiveCompressor()._detect_and_parse(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["progressive", "selective", "hybrid"])
async def test_all_local_structured_keys_bypass_cache(tmp_path, strategy):
    from memtomem_stm.proxy.cache import ProxyCache

    proxy, session = manager(tmp_path, strategy, budget=4500)
    cache = ProxyCache(tmp_path / "cache.db")
    cache.initialize()
    proxy._cache = cache
    source = "\n".join(f"## Section {i}\n" + "text " * 300 for i in range(8))
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    try:
        first = payload(await proxy.call_tool("srv", "read", {}))
        second = payload(await proxy.call_tool("srv", "read", {}))
        assert first["payload"]["key"] != second["payload"]["key"]
        assert session.call_tool.await_count == 2
        assert cache._db.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0] == 0
    finally:
        cache.close()


@pytest.mark.parametrize("kind", ["progressive", "selective", "hybrid", "selection"])
def test_startup_purges_structured_keys_and_keeps_ordinary_json(tmp_path, kind):
    from memtomem_stm.proxy.cache import ProxyCache, response_carries_transient_key
    from memtomem_stm.proxy.delivery import envelope, delivery_json

    db = tmp_path / "cache.db"
    response = delivery_json(envelope(kind, {"key": "opaque-key"}))
    assert response_carries_transient_key(response)
    seed = ProxyCache(db)
    seed.initialize()
    seed.set("srv", "read", {}, response, ttl_seconds=None)
    ordinary = json.dumps({"stm": {"schema_version": 1, "kind": kind, "payload": {"value": 2}}})
    seed.set("srv", "ordinary", {}, ordinary, ttl_seconds=None)
    seed.close()
    reopened = ProxyCache(db)
    reopened.initialize()
    try:
        assert reopened.get("srv", "read", {}) is None
        assert reopened.get("srv", "ordinary", {}) == ordinary
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_initial_budget_includes_retained_upstream_metadata(tmp_path):
    proxy, session = manager(tmp_path)
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="word " * 2000, _meta={"note": "a" * 100})],
        _meta={"source": "b" * 100},
    )
    result = await proxy.call_tool("srv", "read", {})
    assert len(result.model_dump_json(by_alias=True, exclude_none=True)) <= 1600
    assert result.meta == {"source": "b" * 100}
    assert result.content[0].meta == {"note": "a" * 100}
    assert proxy.tracker.get_summary()["structured_delivery"]["serialized_base_chars"] == len(
        result.model_dump_json(by_alias=True, exclude_none=True)
    )


def test_impossible_toc_does_not_publish_or_evict():
    from memtomem_stm.proxy.pending_store import InMemoryPendingStore

    store = InMemoryPendingStore()
    compressor = SelectiveCompressor(store=store)
    with delivery_scope("structured", 10), pytest.raises(DeliveryError):
        compressor.compress('{"a": 1, "b": 2}', max_chars=10)
    assert len(store) == 0


def test_selective_unknown_version_preserves_row_and_explicit_legacy_reads(tmp_path):
    store = SQLitePendingStore(tmp_path / "pending.db")
    store.initialize()
    store.put(
        "key",
        PendingSelection(
            {"a": "body"},
            "json",
            time.monotonic(),
            4,
            json.dumps({"schema_version": 99, "delivery_format": "structured"}),
        ),
    )
    compressor = SelectiveCompressor(store=store)
    before = store.get("key").created_at
    with pytest.raises(DeliveryError):
        compressor.select("key", ["a"])
    assert store.get("key").created_at == before
    assert compressor.select("key", ["a"], response_format="legacy") == "body"
    store.close()


@pytest.mark.parametrize("separator", list("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"))
def test_fact_metadata_stays_on_core_splitlines_boundaries(separator):
    from memtomem_stm.proxy.extraction import ExtractedFact
    from memtomem_stm.proxy.memory_ops import format_fact_md

    value = "before" + separator + "---" + separator + "after"
    fact = ExtractedFact(content="Real content.", category=value, confidence=0.9, tags=[value])
    document = format_fact_md(fact, value, "tool", {})
    lines = document.splitlines()
    assert lines.count("---") == 2
    assert (
        json.loads(next(line[8:] for line in lines if line.startswith("source: ")))
        == f"extracted/{value}/tool"
    )
    assert json.loads(next(line[6:] for line in lines if line.startswith("tags: "))) == [value]
    assert not any(line.startswith("tags: ") for line in lines[lines.index("---", 1) + 1 :])


def test_shared_heading_scanner_respects_fence_length_and_unicode_lines():
    from memtomem_stm.proxy.markdown_structure import headings
    from memtomem_stm.proxy.heading_index import legacy_remaining_headings

    source = "## Top\r\n~~~~\r\n## fenced\r\n~~~\r\n## still fenced\r\n~~~~\r\n    ## code\n## End\n#\u2028not a heading\n"
    positions = list(headings(source))
    assert [h.title for h in positions] == ["Top", "End"]
    assert legacy_remaining_headings(source, source.index("fenced")) == '"End"'


def test_delivery_format_config_env_and_precedence():
    from memtomem_stm.proxy.config import (
        ToolOverrideConfig,
        collect_proxy_env_overrides,
        effective_delivery_format,
    )

    env = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__DELIVERY_FORMAT": "structured"})
    cfg = ProxyConfig.model_validate(env.fragment)
    server = UpstreamServerConfig(prefix="test")
    assert effective_delivery_format(server, None, cfg) == "structured"
    server.delivery_format = "legacy"
    assert effective_delivery_format(server, None, cfg) == "legacy"
    assert (
        effective_delivery_format(server, ToolOverrideConfig(delivery_format="structured"), cfg)
        == "structured"
    )
    assert ProxyConfig().delivery_format == "legacy"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_ttl_configs_refuse_nonfinite(value):
    from pydantic import ValidationError
    from memtomem_stm.proxy.config import CacheConfig, ToolOverrideConfig

    for cls, field in [
        (ProgressiveConfig, "ttl_seconds"),
        (SelectiveConfig, "pending_ttl_seconds"),
        (CacheConfig, "default_ttl_seconds"),
        (ToolOverrideConfig, "cache_ttl_seconds"),
        (UpstreamServerConfig, "cache_ttl_seconds"),
    ]:
        with pytest.raises(ValidationError):
            cls.model_validate({field: value})


def test_structured_metrics_are_separate_after_sqlite_restart(tmp_path):
    from memtomem_stm.proxy.metrics import CallMetrics
    from memtomem_stm.proxy.metrics_store import MetricsStore, read_compression_summary

    path = tmp_path / "metrics.db"
    store = MetricsStore(path)
    store.initialize()
    legacy = CallMetrics(
        server="srv",
        tool="read",
        original_chars=1000,
        compressed_chars=500,
        compression_accounting="initial_response_v1",
    )
    structured = CallMetrics(
        server="srv",
        tool="read",
        original_chars=2000,
        compressed_chars=1500,
        compression_accounting="initial_structured_v1",
        delivery_format="structured",
        wire_bytes=1700,
    )
    tracker = TokenTracker()
    for event in (legacy, structured):
        tracker.record(event)
        store.record(event)
    store.close()
    summary = read_compression_summary(path)
    assert summary["total_calls"] == 2
    assert summary["total_original_chars"] == 1000
    assert summary["saved_ratio"] == 0.5
    assert (
        summary["structured_delivery"]
        == tracker.get_summary()["structured_delivery"]
        == {"calls": 1, "original_chars": 2000, "serialized_base_chars": 1500, "wire_bytes": 1700}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [
        {"op": "ltm_increment_access", "payload": {"chunk_ids": ["bad\ud800"]}},
        {"op": "ltm_increment_access", "payload": {"chunk_ids": [None]}},
        {"op": "ltm_candidate_propose", "payload": {"idempotency_key": "bad\ud800"}},
        {"op": "surface", "payload": {"session_id": "bad\ud800"}},
        {"ok": True, "results": [{"chunk_id": "bad\ud800"}]},
        {"ok": True, "bundle": {"pinned": [{"block_id": "bad\ud800"}]}},
        {
            "ok": True,
            "retrieved": [
                {"chunk_id": "valid", "context": {"window_before": [{"id": "bad\ud800"}]}}
            ],
        },
    ],
)
async def test_daemon_refuses_raw_ids_before_scrubbing_on_both_boundaries(frame):
    from memtomem_stm.daemon.protocol import encode_line, read_message, ProtocolError

    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps(frame, ensure_ascii=True) + "\n").encode())
    reader.feed_eof()
    with pytest.raises(ProtocolError):
        await read_message(reader)
    with pytest.raises(ProtocolError):
        encode_line(frame)


@pytest.mark.asyncio
@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
async def test_daemon_nonfinite_numbers_are_protocol_errors(number):
    from memtomem_stm.daemon.protocol import read_message, ProtocolError

    reader = asyncio.StreamReader()
    reader.feed_data(('{"score":' + number + "}\n").encode())
    reader.feed_eof()
    with pytest.raises(ProtocolError):
        await read_message(reader)


@pytest.mark.asyncio
async def test_daemon_keeps_literal_escape_ids_distinct_from_surrogates():
    from memtomem_stm.daemon.protocol import encode_line, read_message

    frame = {"op": "ltm_increment_access", "payload": {"chunk_ids": ["literal\\ud800"]}}
    reader = asyncio.StreamReader()
    reader.feed_data(encode_line(frame))
    reader.feed_eof()
    assert await read_message(reader) == frame


def test_compact_ids_include_source_namespace_and_frame_boundaries():
    from memtomem_stm.surfacing.mcp_client import RemoteSearchResult

    results = [
        RemoteSearchResult(content, 1.0, source, namespace)
        for content, source, namespace in [
            ("same", "a", "default"),
            ("same", "b", "default"),
            ("same", "a", "project"),
            ("bc", "a", ""),
            ("c", "ab", ""),
        ]
    ]
    ids = [result.chunk.id for result in results]
    assert len(set(ids)) == len(ids)
    assert all(value.startswith("stm:compact:v2:") for value in ids)
    assert RemoteSearchResult("same", 1.0, "a", "default").chunk.id == ids[0]


@pytest.mark.asyncio
async def test_progressive_wire_totals_measure_final_initial_and_owned_reads(tmp_path):
    from memtomem_stm.proxy.progressive_reads import ProgressiveReadsTracker

    proxy, session = manager(tmp_path)
    tracker = ProgressiveReadsTracker(tmp_path / "reads.db", retention_days=0)
    proxy._progressive_reads_tracker = tracker
    source = '한글😀quoted "\\\n' * 1000
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    try:
        initial = await proxy.call_tool("srv", "read", {})
        p = payload(initial)["payload"]
        more = _continuation_result(proxy.read_more(p["key"], p["next_offset"]), None)
        stats = tracker.store.get_stats()
        assert stats["initial_wire_bytes"] == len(
            initial.model_dump_json(by_alias=True, exclude_none=True).encode()
        )
        assert stats["follow_up_wire_bytes"] == len(
            more.model_dump_json(by_alias=True, exclude_none=True).encode()
        )
        assert stats["wire_measured_reads"] == 2
    finally:
        tracker.close()


def test_mixed_version_heading_indexes_do_not_repair_each_other(tmp_path):
    from memtomem_stm.proxy.heading_index import INDEX_KEY, LEGACY_INDEX_KEY

    store = SQLitePendingStore(tmp_path / "pending.db")
    store.initialize()
    adapter = ProgressiveStoreAdapter(store)
    source = "## One\nbody\n## Two\nend"
    response = ProgressiveResponse(
        content=source,
        total_chars=len(source),
        total_lines=4,
        content_type="markdown",
        structure_hint="2 headings",
        created_at=time.monotonic(),
    )
    adapter.put("key", response)
    try:
        row = store.get("key")
        assert row.chunks[LEGACY_INDEX_KEY] == "scan"
        assert INDEX_KEY != LEGACY_INDEX_KEY
        current = row.chunks[INDEX_KEY]
        row.chunks[LEGACY_INDEX_KEY] = "old-reader-rebuilt-v2-index"
        store.put("key", row)
        adapter.get("key", renew=False)
        after = store.get("key")
        assert after.chunks[INDEX_KEY] == current
        assert after.chunks[LEGACY_INDEX_KEY] == "old-reader-rebuilt-v2-index"
    finally:
        store.close()


@pytest.mark.parametrize(
    "presentation",
    [
        None,
        [],
        "unknown",
        {"schema_version": 999, "budget": None},
        {"schema_version": 1, "budget": float("nan")},
    ],
)
def test_explicit_legacy_reads_ignore_invalid_optional_presentation(tmp_path, presentation):
    store = SQLitePendingStore(tmp_path / "pending.db")
    store.initialize()
    store.put(
        "key",
        PendingSelection(
            {
                "__content__": "body",
                "__meta__": json.dumps({"ttl_seconds": 1800, "delivery": presentation}),
            },
            "progressive",
            time.monotonic(),
            4,
        ),
    )
    adapter = ProgressiveStoreAdapter(store)
    try:
        with pytest.raises(DeliveryError):
            adapter.get("key")
        assert adapter.get("key", response_format="legacy").content == "body"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_hybrid_budget_and_ranges_with_default_retention_floor(tmp_path):
    proxy, session = manager(tmp_path, "hybrid", budget=12000)
    proxy._config.min_result_retention = 0.65
    source = "".join(f"## Section {i}\n" + f"part{i} " * 210 + "\n\n" for i in range(20))
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    initial = await proxy.call_tool("srv", "read", {})
    descriptor = payload(initial)
    assert descriptor["kind"] == "hybrid"
    assert len(initial.model_dump_json(by_alias=True, exclude_none=True)) <= 12000
    p = descriptor["payload"]
    assert p["source_offset"] == len(p["head"])
    assert p["total_chars"] == len(source)
    entry = next(e for e in p["entries"] if "start" in e)
    more = proxy.select_chunks(p["key"], [entry["key"]])
    chunk = more.descriptor["payload"]["chunks"][0]
    assert source[chunk["start"] : chunk["end"]] == chunk["content"]


def test_unknown_metadata_in_alternate_progressive_store_closes_reader(tmp_path, monkeypatch):
    from memtomem_stm.proxy.config import UpstreamServerConfig

    proxy, _ = manager(tmp_path, sqlite=True)
    alternate = tmp_path / "alternate.db"
    proxy._config.upstream_servers["zzother"] = UpstreamServerConfig(
        prefix="other",
        selective=SelectiveConfig(pending_store="sqlite", pending_store_path=alternate),
    )
    assert "not found" in proxy.read_more("missing", 0)
    writer = SQLitePendingStore(alternate)
    writer.initialize()
    writer.put(
        "bad-version",
        PendingSelection(
            {
                "__content__": "body",
                "__meta__": json.dumps({"ttl_seconds": 1800, "delivery": {"schema_version": 99}}),
            },
            "progressive",
            time.monotonic(),
            4,
        ),
    )
    writer.close()
    opened = []
    factory = proxy._open_progressive_adapter

    def track(cfg):
        adapter = factory(cfg)
        opened.append(adapter)
        return adapter

    monkeypatch.setattr(proxy, "_open_progressive_adapter", track)
    with pytest.raises(DeliveryError):
        proxy.read_more("bad-version", 0)
    assert opened
    assert all(adapter._store._db is None for adapter in opened)
    proxy._progressive_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["2025-11-25", "2026-07-28"])
@pytest.mark.parametrize("owned", [False, True])
async def test_budget_projection_matches_real_sdk_serializer(version, owned):
    from types import SimpleNamespace
    from mcp.server.mcpserver import MCPServer
    from mcp.server.runner import ServerRunner
    from memtomem_stm.proxy._fastmcp_compat import install_delivery_wire_context
    from memtomem_stm.proxy.delivery import fit_progressive, wire_json

    server = MCPServer("Unicode 한글😀", version="test-version", title="Fixture")
    install_delivery_wire_context(server)
    source = '한글😀 "quoted" \\line\n' * 100
    measured = []

    async def handler(ctx):
        await asyncio.sleep(0)
        desc = fit_progressive(
            source,
            key="key",
            offset=0,
            limit=1000,
            source_format="text",
            ttl=1800,
            budget=1100,
            owned=owned,
        )
        result = delivery_result(desc, owned=owned)
        measured.append(wire_json(result))
        return result

    result = await server.middleware[-1](
        SimpleNamespace(method="tools/call", protocol_version=version), handler
    )

    class ActualSerializer:
        _serialize = ServerRunner._serialize
        _stamp_server_info = ServerRunner._stamp_server_info

        def __init__(self):
            self.server = server._lowlevel_server

    actual = ActualSerializer()._serialize("tools/call", version, result)
    actual_json = json.dumps(actual, ensure_ascii=False, separators=(",", ":"))
    assert measured == [actual_json]
    assert len(actual_json) <= 1100
    assert ("_meta" in actual) == (version == "2026-07-28")
    # A request's negotiated projection must not leak into direct library calls.
    assert wire_json(result) == result.model_dump_json(by_alias=True, exclude_none=True)


@pytest.mark.asyncio
async def test_manager_metrics_include_negotiated_sdk_wire_metadata(tmp_path):
    from types import SimpleNamespace
    from mcp.server.mcpserver import MCPServer
    from mcp.server.runner import ServerRunner
    from memtomem_stm.proxy._fastmcp_compat import install_delivery_wire_context
    from memtomem_stm.proxy.progressive_reads import ProgressiveReadsTracker

    server = MCPServer("memtomem-stm", version="test")
    install_delivery_wire_context(server)
    proxy, session = manager(tmp_path, budget=1600)
    tracker = ProgressiveReadsTracker(tmp_path / "reads.db", retention_days=0)
    proxy._progressive_reads_tracker = tracker
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="한글😀" * 2000)]
    )

    class ActualSerializer:
        _serialize = ServerRunner._serialize
        _stamp_server_info = ServerRunner._stamp_server_info

        def __init__(self):
            self.server = server._lowlevel_server

    serialize = ActualSerializer()._serialize
    context = SimpleNamespace(method="tools/call", protocol_version="2026-07-28")

    async def initial(ctx):
        return await proxy.call_tool("srv", "read", {})

    try:
        first = await server.middleware[-1](context, initial)
        first_wire = json.dumps(
            serialize("tools/call", context.protocol_version, first),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        data = payload(first)["payload"]

        async def more(ctx):
            value = proxy.read_more(data["key"], data["next_offset"])
            return _continuation_result(value, None)

        followup = await server.middleware[-1](context, more)
        more_wire = json.dumps(
            serialize("tools/call", context.protocol_version, followup),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        assert max(len(first_wire), len(more_wire)) <= 1600
        stats = tracker.store.get_stats()
        assert stats["initial_wire_bytes"] == len(first_wire.encode())
        assert stats["follow_up_wire_bytes"] == len(more_wire.encode())
        summary = proxy.tracker.get_summary()["structured_delivery"]
        assert summary["wire_bytes"] == len(first_wire.encode())
        assert summary["serialized_base_chars"] == len(first_wire)
    finally:
        tracker.close()
