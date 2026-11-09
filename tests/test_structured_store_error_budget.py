"""A structured SELECTIVE/HYBRID call whose pending store fails degrades to a
truncation that still fits the serialized budget (#1358); a structured
PROGRESSIVE call's passthrough is held to the same budget (#1368)."""

import itertools
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

from test_structured_delivery import manager
from memtomem_stm.proxy._fastmcp_compat import to_call_tool_result
from memtomem_stm.proxy.compression import SensitivePayloadNotStored, TruncateCompressor
from memtomem_stm.proxy.config import (
    CleaningConfig,
    ProgressiveConfig,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.delivery import wire_json
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker

_WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]


def _quoted(lines: int) -> str:
    # Distinct line prefixes: truncation would collapse look-alike lines into
    # a short "similar lines omitted" summary that fits any budget.
    triples = itertools.islice(itertools.product(_WORDS, repeat=3), lines)
    return "## Section 1\n" + "".join(f'"{a} {b} says {c}"\n' for a, b, c in triples)


def _plain_wire(text: str) -> int:
    return len(wire_json(CallToolResult(content=[TextContent(type="text", text=text)])))


async def _degraded_call(tmp_path, strategy, source, budget, exc):
    proxy, session = manager(tmp_path, strategy, budget=budget)
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    # The SELECTIVE/HYBRID store write fails inside the compressor call.
    proxy._apply_compression = AsyncMock(side_effect=exc)
    return to_call_tool_result(await proxy.call_tool("srv", "read", {}))


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["selective", "hybrid"])
@pytest.mark.parametrize(
    "exc",
    [sqlite3.OperationalError("database is locked"), SensitivePayloadNotStored()],
    ids=["store-error", "sensitive"],
)
# 33 lines: short enough to return unchanged by character count (the #1358
# report); 40 lines: truncated at the character budget, still over the wire.
@pytest.mark.parametrize("lines", [33, 40])
async def test_degraded_truncation_fits_the_serialized_budget(tmp_path, strategy, exc, lines):
    source = _quoted(lines)
    first_cut = TruncateCompressor().compress(source, max_chars=1000)
    # Path proof: the character-budgeted degrade alone overflows the wire.
    assert _plain_wire(first_cut) > 1000

    result = await _degraded_call(tmp_path, strategy, source, 1000, exc)

    assert not result.is_error
    assert len(wire_json(result)) <= 1000
    text = result.content[0].text
    assert "... (truncated, original:" in text
    assert source.startswith(text.split("\n... (truncated", 1)[0])
    # The search keeps most of the budget rather than any small truncation.
    assert len(wire_json(result)) > 900


@pytest.mark.asyncio
async def test_degraded_truncation_that_fits_is_unchanged(tmp_path):
    source = "## Section 1\n" + "".join(
        f"{a} {b} says {c}\n"
        for a, b, c in itertools.islice(itertools.product(_WORDS, repeat=3), 33)
    )
    expected = TruncateCompressor().compress(source, max_chars=1000)
    assert _plain_wire(expected) <= 1000

    result = await _degraded_call(
        tmp_path, "selective", source, 1000, sqlite3.OperationalError("database is locked")
    )

    assert not result.is_error
    assert result.content[0].text == expected


@pytest.mark.asyncio
async def test_budget_too_small_for_any_truncation_is_budget_exceeded(tmp_path):
    result = await _degraded_call(
        tmp_path, "selective", _quoted(40), 40, sqlite3.OperationalError("database is locked")
    )

    assert result.is_error
    assert json.loads(result.content[0].text)["stm"]["payload"]["code"] == "budget_exceeded"


class _CountingScorer:
    uses_blocking_io = False

    def __init__(self) -> None:
        self.calls = 0

    def score_sections(self, query, sections):
        self.calls += 1
        return [float(i + 1) for i in range(len(sections))]


@pytest.mark.asyncio
async def test_the_fit_scores_the_sections_once(tmp_path):
    # Each budget the fit tries reruns a query-aware truncation over the same
    # sections. An embedding scorer would repeat its call, or its timeout, per
    # budget; the scores are computed once and reused.
    source = "".join(
        f"## Part {a} {b}\n" + "".join(f'"{a}" "{b}" "says" "{c}"\n' for c in _WORDS)
        for a, b in itertools.islice(itertools.product(_WORDS, repeat=2), 12)
    )
    # Path proof: the first query-aware cut overflows the wire, so the fit runs.
    first = TruncateCompressor(scorer=_CountingScorer()).compress(
        source, max_chars=1000, context_query="says"
    )
    assert _plain_wire(first) > 1000
    scorer = _CountingScorer()
    proxy, session = manager(tmp_path, "selective", budget=1000)
    proxy._relevance_scorer_for = lambda cfg: scorer
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    proxy._apply_compression = AsyncMock(side_effect=sqlite3.OperationalError("locked"))

    result = to_call_tool_result(await proxy.call_tool("srv", "read", {"_context_query": "says"}))

    assert not result.is_error
    assert len(wire_json(result)) <= 1000
    assert scorer.calls == 1


# ── PROGRESSIVE (#1368) ─────────────────────────────────────────────────────
# The PROGRESSIVE degrade returns the cleaned response unchanged; in structured
# mode it is held to the same serialized budget as the SELECTIVE/HYBRID one.


def _fail_progressive(proxy, exc):
    def _raise(*args, **kwargs):
        raise exc

    proxy._apply_progressive = _raise


async def _progressive_degraded_call(tmp_path, source, budget, exc):
    proxy, session = manager(tmp_path, "progressive", budget=budget)
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    _fail_progressive(proxy, exc)
    return to_call_tool_result(await proxy.call_tool("srv", "read", {}))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [sqlite3.OperationalError("database is locked"), SensitivePayloadNotStored()],
    ids=["store-error", "sensitive"],
)
async def test_progressive_degrade_fits_the_serialized_budget(tmp_path, exc):
    # The #1368 report: 5105 chars of plain text at a 1600 budget.
    source = "## S\n" + "plain words here\n" * 300
    assert _plain_wire(source) > 1600

    result = await _progressive_degraded_call(tmp_path, source, 1600, exc)

    assert not result.is_error
    assert len(wire_json(result)) <= 1600
    text = result.content[0].text
    # Look-alike lines collapse into a summary; nothing names a progressive key.
    assert text != source
    assert text.startswith("## S\nplain words here\n")
    assert "similar lines omitted" in text
    assert "stm_proxy_read_more" not in text


@pytest.mark.asyncio
async def test_progressive_degrade_fits_after_json_escaping(tmp_path):
    # Quoted lines: a character-budgeted cut fits by count, not on the wire.
    source = _quoted(60)
    first_cut = TruncateCompressor().compress(source, max_chars=1600)
    assert _plain_wire(first_cut) > 1600

    result = await _progressive_degraded_call(
        tmp_path, source, 1600, sqlite3.OperationalError("database is locked")
    )

    assert not result.is_error
    assert len(wire_json(result)) <= 1600
    text = result.content[0].text
    assert "... (truncated, original:" in text
    assert source.startswith(text.split("\n... (truncated", 1)[0])
    # The search keeps most of the budget rather than any small truncation.
    assert len(wire_json(result)) > 1400


@pytest.mark.asyncio
async def test_progressive_degrade_that_fits_stays_zero_loss(tmp_path):
    # Longer than one chunk, so the call took the progressive path, but its
    # plain result fits: the passthrough is returned unchanged.
    source = "## S\n" + "plain words here\n" * 70
    assert len(source) > 1000
    assert _plain_wire(source) <= 1600

    result = await _progressive_degraded_call(
        tmp_path, source, 1600, sqlite3.OperationalError("database is locked")
    )

    assert not result.is_error
    assert result.content[0].text == source


@pytest.mark.asyncio
async def test_progressive_budget_too_small_for_any_truncation_is_budget_exceeded(tmp_path):
    result = await _progressive_degraded_call(
        tmp_path, _quoted(60), 40, sqlite3.OperationalError("database is locked")
    )

    assert result.is_error
    assert json.loads(result.content[0].text)["stm"]["payload"]["code"] == "budget_exceeded"


def _legacy_manager(tmp_path, budget):
    upstream = UpstreamServerConfig(
        prefix="doc",
        compression="progressive",
        max_result_chars=budget,
        delivery_format="legacy",
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
    return proxy, session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [sqlite3.OperationalError("database is locked"), SensitivePayloadNotStored()],
    ids=["store-error", "sensitive"],
)
async def test_legacy_progressive_degrade_over_budget_stays_zero_loss(tmp_path, exc):
    # Legacy delivery has no serialized budget: the degrade keeps the full
    # cleaned response even when it is far over ``max_result_chars``.
    source = "## S\n" + "plain words here\n" * 300
    proxy, session = _legacy_manager(tmp_path, 1600)
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    _fail_progressive(proxy, exc)

    result = await proxy.call_tool("srv", "read", {})

    assert result == source
