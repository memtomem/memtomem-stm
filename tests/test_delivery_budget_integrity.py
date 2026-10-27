"""Density-based budgets and truthful inline contents across delivery paths."""

from copy import deepcopy

import pytest
from mcp.types import CallToolResult, TextContent

from test_structured_delivery import manager, payload
from memtomem_stm.proxy._fastmcp_compat import to_call_tool_result
from memtomem_stm.proxy.compression import SelectiveCompressor
from memtomem_stm.proxy.config import TokenEstimationMode
from memtomem_stm.proxy.delivery import (
    DeliveryBudgetExceeded,
    delivery_scope,
    delivery_size,
    envelope,
    fit_toc,
    wire_json,
)
from memtomem_stm.proxy.pending_store import InMemoryPendingStore
from memtomem_stm.proxy.token_estimate import approx_tokens
from memtomem_stm.server import _continuation_result


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["selective", "hybrid", "progressive"])
@pytest.mark.parametrize("retention", [0, 0.95])
@pytest.mark.parametrize("with_metadata", [False, True])
async def test_unicode_budget_covers_initial_and_owned_continuations(
    tmp_path, strategy, retention, with_metadata
):
    source = "".join(f"# 섹션 {i}\n" + "한국어 본문 문장입니다 " * 40 + "\n" for i in range(12))
    proxy, session = manager(tmp_path, strategy, budget=16000)
    cfg = proxy._config.upstream_servers["srv"]
    cfg.max_result_chars = None
    cfg.max_result_tokens = 1500
    cfg.token_estimation_mode = TokenEstimationMode.UNICODE
    proxy._config.min_result_retention = retention
    tc = proxy._resolve_tool_config("srv", "read", proxy_cfg=proxy._config)
    limit = min(tc.max_chars, int(len(source) * 1500 / approx_tokens(source)))
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text=source)],
        _meta={"note": "kept " * 20} if with_metadata else None,
    )
    result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))
    assert not result.is_error
    assert len(wire_json(result)) <= limit
    assert approx_tokens(wire_json(result)) <= 1500
    desc = payload(result)
    assert desc["kind"] == "progressive"
    key = desc["payload"]["key"]
    assert proxy._progressive_store.get(key).delivery_budget == limit
    pieces = [desc["payload"]["content"]]
    while desc["payload"]["has_more"]:
        previous = desc["payload"]["next_offset"]
        more = proxy.read_more(key, previous)
        response = _continuation_result(more, None)
        assert not response.is_error
        assert len(wire_json(response)) <= limit
        assert approx_tokens(wire_json(response)) <= 1500
        desc = payload(response)
        part = desc["payload"]
        assert part["offset"] == previous
        assert part["content"] == source[previous : previous + len(part["content"])]
        pieces.append(part["content"])
    assert "".join(pieces) == source
    if proxy._selective_compressor is not None:
        assert len(proxy._selective_compressor._store) == 0


@pytest.mark.parametrize("budget", [7000, 8000, 9000])
def test_inline_table_that_cannot_fit_is_refused_before_publication(budget):
    source = "".join(f"# S{i}\nshort body number {i:03d} here ok\n" for i in range(40))
    store = InMemoryPendingStore()
    compressor = SelectiveCompressor(store=store)
    with delivery_scope("structured", budget), pytest.raises(DeliveryBudgetExceeded):
        compressor.compress(source, max_chars=500)
    assert len(store) == 0


def test_fitting_only_shortens_non_inline_previews():
    value = envelope(
        "selective",
        {
            "entries": [
                {"key": "short", "inline": True, "preview": "complete small section"},
                {"key": "long", "inline": False, "preview": "preview " * 100},
            ]
        },
    )
    original = deepcopy(value)
    limit = delivery_size(value, owned=False) - 200
    fitted = fit_toc(value, limit)
    assert delivery_size(fitted, owned=False) <= limit
    short, long = fitted["payload"]["entries"]
    assert short == original["payload"]["entries"][0]
    assert long["inline"] is False
    assert len(long["preview"]) < len(original["payload"]["entries"][1]["preview"])
    assert value == original


def test_inline_table_with_room_keeps_exact_source_content():
    source = "".join(f"# S{i}\nshort body number {i:03d} here ok\n" for i in range(40))
    with delivery_scope("structured", 20000):
        result = SelectiveCompressor().compress(source, max_chars=500)
    entries = result.descriptor["payload"]["entries"]
    assert len(entries) == 40
    for entry in entries:
        assert entry["inline"]
        assert entry["preview"] == source[entry["start"] : entry["end"]]
