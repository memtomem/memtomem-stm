"""Upstream metadata alone over the structured budget (#1359).

The key-publishing strategies (PROGRESSIVE, SELECTIVE, HYBRID with a TOC tail)
hold their result to the serialized budget, which reserves the upstream
metadata the final result keeps. An empty text block must not skip that check.
"""

import json
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

from test_structured_delivery import manager
from memtomem_stm.proxy._fastmcp_compat import to_call_tool_result
from memtomem_stm.proxy.config import (
    CleaningConfig,
    HybridConfig,
    ProgressiveConfig,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.delivery import wire_json
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker


def _plain_wire(text: str) -> int:
    return len(wire_json(CallToolResult(content=[TextContent(type="text", text=text)])))


def _upstream(text: str, meta_chars: int, where: str) -> CallToolResult:
    if where == "block":
        block = TextContent(type="text", text=text, _meta={"a": "m" * meta_chars})
        return CallToolResult(content=[block])
    return CallToolResult(
        content=[TextContent(type="text", text=text)], _meta={"note": "m" * meta_chars}
    )


async def _call(tmp_path, strategy: str, upstream: CallToolResult, budget: int = 500):
    proxy, session = manager(tmp_path, strategy, budget=budget)
    session.call_tool.return_value = upstream
    return to_call_tool_result(await proxy.call_tool("srv", "read", {}))


def _code(result: CallToolResult) -> str:
    return json.loads(result.content[0].text)["stm"]["payload"]["code"]


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["selective", "hybrid", "progressive"])
@pytest.mark.parametrize("where", ["result", "block"])
async def test_empty_text_with_metadata_over_budget_is_budget_exceeded(tmp_path, strategy, where):
    # The #1359 report: SELECTIVE and HYBRID returned 599 wire chars at 500.
    result = await _call(tmp_path, strategy, _upstream("", 500, where))

    assert result.is_error
    assert _code(result) == "budget_exceeded"
    assert len(wire_json(result)) <= 500


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["selective", "hybrid", "progressive"])
@pytest.mark.parametrize("where", ["result", "block"])
async def test_short_text_with_metadata_over_budget_is_budget_exceeded(tmp_path, strategy, where):
    # Already the outcome with any text; the empty case now matches it.
    result = await _call(tmp_path, strategy, _upstream("hello world", 500, where))

    assert result.is_error
    assert _code(result) == "budget_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["selective", "hybrid", "progressive"])
async def test_empty_text_with_metadata_that_fits_is_returned(tmp_path, strategy):
    result = await _call(tmp_path, strategy, _upstream("", 50, "result"))

    assert not result.is_error
    assert result.content[0].text == ""
    assert result.meta == {"note": "m" * 50}
    assert len(wire_json(result)) <= 500


# ── Unicode token budget ─────────────────────────────────────────────────────
# A response within ``max_result_tokens`` is returned intact (strategy NONE).
# For a structured key publisher that passthrough is still a serialized MCP
# result, so it must fit like the compressors' own short-response passthrough.


def _token_manager(tmp_path, strategy: str):
    upstream = UpstreamServerConfig(
        prefix="doc",
        compression=strategy,
        max_result_tokens=143,  # x 3.5 chars per token = a 500-char cap
        token_estimation_mode="unicode",
        delivery_format="structured",
        cleaning=CleaningConfig(enabled=False),
        progressive=ProgressiveConfig(chunk_size=1000),
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
    assert proxy._resolve_tool_config("srv", "read").max_chars == 500
    return proxy, session


async def _token_call(tmp_path, strategy: str, upstream: CallToolResult):
    proxy, session = _token_manager(tmp_path, strategy)
    session.call_tool.return_value = upstream
    return to_call_tool_result(await proxy.call_tool("srv", "read", {}))


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["selective", "hybrid", "progressive"])
@pytest.mark.parametrize("text", ["", "hello world"], ids=["empty", "short"])
async def test_within_token_budget_metadata_over_budget_is_budget_exceeded(
    tmp_path, strategy, text
):
    result = await _token_call(tmp_path, strategy, _upstream(text, 500, "result"))

    assert result.is_error
    assert _code(result) == "budget_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["selective", "hybrid", "progressive"])
async def test_within_token_budget_escaped_text_stays_within_the_budget(tmp_path, strategy):
    # About 100 estimated tokens, but every quote and newline is escaped on
    # the wire: the intact passthrough would exceed the 500-char frame.
    source = "## S\n" + "".join(f'"q{i}" "said" "x"\n' for i in range(25))
    assert len(source) <= 500
    assert _plain_wire(source) > 500

    result = await _token_call(
        tmp_path, strategy, CallToolResult(content=[TextContent(type="text", text=source)])
    )

    assert len(wire_json(result)) <= 500


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["selective", "hybrid", "progressive"])
async def test_within_token_budget_response_that_fits_is_intact(tmp_path, strategy):
    source = "## S\nplain words here\n"
    result = await _token_call(
        tmp_path, strategy, CallToolResult(content=[TextContent(type="text", text=source)])
    )

    assert not result.is_error
    assert result.content[0].text == source


# ── Not held to the serialized budget ────────────────────────────────────────


def _plain_manager(tmp_path, strategy: str, *, delivery: str, hybrid=None, budget: int = 500):
    upstream = UpstreamServerConfig(
        prefix="doc",
        compression=strategy,
        max_result_chars=budget,
        delivery_format=delivery,
        cleaning=CleaningConfig(enabled=False),
        progressive=ProgressiveConfig(chunk_size=1000),
        hybrid=hybrid,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["selective", "hybrid"])
async def test_legacy_empty_text_with_large_metadata_is_returned(tmp_path, strategy):
    # A 40-char budget is smaller than even an empty serialized result, so a
    # structured-only check that leaked into legacy delivery would refuse it.
    assert _plain_wire("") > 40
    proxy, session = _plain_manager(tmp_path, strategy, delivery="legacy", budget=40)
    session.call_tool.return_value = _upstream("", 500, "result")

    result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))

    assert not result.is_error
    # Returned as is, not rerouted through a progressive fallback.
    assert result.content[0].text == ""
    assert result.meta == {"note": "m" * 500}


@pytest.mark.asyncio
async def test_hybrid_truncate_tail_keeps_its_character_budget(tmp_path):
    # Not a key publisher: structured delivery does not hold it to the frame.
    proxy, session = _plain_manager(
        tmp_path, "hybrid", delivery="structured", hybrid=HybridConfig(tail_mode="truncate")
    )
    session.call_tool.return_value = _upstream("", 500, "result")

    result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))

    assert not result.is_error
    assert result.meta == {"note": "m" * 500}
