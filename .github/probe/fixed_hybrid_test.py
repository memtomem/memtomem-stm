"""Hybrid tail headings come from the whole document; key publishers keep the
structured serialized budget on every path (#1347)."""

import json

import pytest
from mcp.types import CallToolResult, TextContent

from test_structured_delivery import manager
from memtomem_stm.proxy._fastmcp_compat import to_call_tool_result
from memtomem_stm.proxy.compression import HybridCompressor, SelectiveCompressor
from memtomem_stm.proxy.config import TailMode
from memtomem_stm.proxy.delivery import (
    DeliveryBudgetExceeded,
    DeliveryText,
    delivery_scope,
    delivery_size,
    wire_json,
)
from memtomem_stm.proxy.markdown_structure import headings, suffix_headings

FENCED = (
    "# Intro\n\n"
    + "Intro paragraph text. " * 15
    + "\n\n```python\n"
    + "x = 1\n" * 20
    + "# fake heading inside code fence\n"
    + "y = 2\n" * 30
    + "```\n\n## Real Section One\n\n"
    + "Real content one. " * 30
    + "\n\n## Real Section Two\n\n"
    + "Real content two. " * 30
    + "\n"
)
MID_LINE = "# A\n\n" + "Sentence one here. " * 25 + "# not a heading\n\n## Real\n\n" + "body " * 200
QUOTED = "## Section 1\n" + '"quoted line"\n' * 25


def _hybrid(head_chars: int, **kwargs) -> HybridCompressor:
    return HybridCompressor(
        head_chars=head_chars,
        tail_mode=TailMode.TOC,
        selective_compressor=SelectiveCompressor(min_section_chars=10),
        **kwargs,
    )


def _structured_entries(doc: str, head_chars: int) -> tuple[str, list[dict]]:
    with delivery_scope("structured", 4000, continuation_budget=4000):
        out = _hybrid(head_chars).compress(doc, max_chars=1200)
    assert isinstance(out, DeliveryText)
    payload = out.descriptor["payload"]
    return payload["head"], payload["entries"]


def _legacy_entries(doc: str, head_chars: int) -> tuple[str, list[dict]]:
    out = _hybrid(head_chars).compress(doc, max_chars=1200)
    marker = "Table of Contents:\n\n"
    head, _, toc = out.partition("\n---\nRemaining content")
    return head, json.loads(toc.split(marker, 1)[1])["entries"]


@pytest.mark.parametrize("entries_of", [_structured_entries, _legacy_entries])
def test_head_ending_inside_a_code_fence_keeps_tail_fence_state(entries_of):
    head, entries = entries_of(FENCED, 450)
    # Path proof: the head opens the fence and the tail holds its close.
    assert "```python" in head and head.count("```") == 1
    assert [e["key"] for e in entries] == ["Preamble", "Real Section One", "Real Section Two"]


def test_structured_tail_ranges_refer_to_the_whole_document():
    head, entries = _structured_entries(FENCED, 450)
    by_key = {e["key"]: e for e in entries}
    assert by_key["Preamble"]["start"] == len(head)
    for title in ("Real Section One", "Real Section Two"):
        assert by_key[title]["start"] == FENCED.index(f"## {title}")
        assert by_key[title]["heading"] == {"title": title, "level": 2}


@pytest.mark.parametrize("entries_of", [_structured_entries, _legacy_entries])
def test_head_ending_mid_line_does_not_make_a_heading(entries_of):
    head, entries = entries_of(MID_LINE, 480)
    # Path proof: the cut is inside the line that carries the "#" text.
    assert not head.endswith("\n")
    assert MID_LINE[len(head) :].split("\n", 1)[0].endswith("# not a heading")
    assert [e["key"] for e in entries] == ["Preamble", "Real"]


def test_suffix_headings_match_the_document_parse():
    for offset in range(len(FENCED) + 1):
        expected = [
            (h.start - offset, h.end - offset, h.level, h.title)
            for h in headings(FENCED)
            if h.start >= offset
        ]
        assert [
            (h.start, h.end, h.level, h.title) for h in suffix_headings(FENCED, offset)
        ] == expected


@pytest.mark.parametrize(
    "compress",
    [
        lambda text: SelectiveCompressor().compress(text, max_chars=500),
        lambda text: HybridCompressor().compress(text, max_chars=500),
    ],
    ids=["selective", "hybrid"],
)
def test_structured_passthrough_is_measured_serialized(compress):
    assert len(QUOTED) <= 500
    plain = CallToolResult(content=[TextContent(type="text", text=QUOTED)])
    assert len(wire_json(plain)) > 500
    # One section cannot fit a table at this budget either, so the publisher
    # hands the response to the progressive fallback instead of emitting it.
    with delivery_scope("structured", 500, continuation_budget=500):
        with pytest.raises(DeliveryBudgetExceeded):
            compress(QUOTED)


@pytest.mark.parametrize(
    "compress",
    [
        lambda text: SelectiveCompressor().compress(text, max_chars=500),
        lambda text: HybridCompressor().compress(text, max_chars=500),
        lambda text: HybridCompressor(tail_mode=TailMode.TRUNCATE).compress(text, max_chars=500),
    ],
    ids=["selective", "hybrid", "hybrid-truncate"],
)
def test_structured_passthrough_that_fits_is_unchanged(compress):
    text = "## Section 1\n" + "plain line\n" * 25
    with delivery_scope("structured", 500, continuation_budget=500):
        out = compress(text)
    assert out == text and not isinstance(out, DeliveryText)


def test_truncate_tail_keeps_its_character_budget_in_structured_mode():
    # Not a key publisher: like the TRUNCATE strategy it is not wire-checked.
    with delivery_scope("structured", 500, continuation_budget=500):
        assert (
            HybridCompressor(tail_mode=TailMode.TRUNCATE).compress(QUOTED, max_chars=500) == QUOTED
        )


# At 500 no STM frame fits, the same outcome PROGRESSIVE gives this input; at
# 1000 a table fits. Before #1347 both budgets returned the raw text over budget.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strategy", "repeat", "budget", "kind"),
    [
        ("progressive", 25, 500, "error"),
        ("selective", 25, 500, "error"),
        ("hybrid", 25, 500, "error"),
        ("selective", 60, 1000, "selective"),
        ("hybrid", 60, 1000, "hybrid"),
    ],
)
async def test_proxy_structured_result_stays_in_budget(tmp_path, strategy, repeat, budget, kind):
    source = "## Section 1\n" + '"quoted line"\n' * repeat
    assert len(source) <= budget
    proxy, session = manager(tmp_path, strategy, budget=budget)
    session.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text=source)])
    try:
        result = to_call_tool_result(await proxy.call_tool("srv", "read", {}))
    finally:
        # An unstopped manager is finalized by a later GC pass, which can land
        # inside an unrelated test's timing window.
        await proxy.stop()
    assert len(wire_json(result)) <= budget
    stm = json.loads(result.content[0].text)["stm"]
    assert stm["kind"] == kind
    if kind == "error":
        assert stm["payload"]["code"] == "budget_exceeded"


SECTIONED = "# H\n\n" + "word " * 200 + "\n\n## S1\n\n" + "a " * 400 + "\n\n## S2\n\n" + "b " * 400


@pytest.mark.parametrize("budget", [1200, 1400])
def test_structured_hybrid_shrinks_head_to_fit(budget):
    hybrid = HybridCompressor(head_chars=500, min_head_chars=100)
    with delivery_scope("structured", budget, continuation_budget=budget):
        out = hybrid.compress(SECTIONED, max_chars=budget)
    assert isinstance(out, DeliveryText)
    payload = out.descriptor["payload"]
    assert 100 <= len(payload["head"]) < 500
    assert SECTIONED.startswith(payload["head"])
    assert delivery_size(out.descriptor, owned=False) <= budget
    keys = [e["key"] for e in payload["entries"]]
    assert keys == ["Preamble", "S1", "S2"]
    assert payload["entries"][0]["start"] == len(payload["head"])


def test_structured_hybrid_below_min_head_still_falls_back():
    hybrid = HybridCompressor(head_chars=500, min_head_chars=100)
    with delivery_scope("structured", 1000, continuation_budget=1000):
        with pytest.raises(DeliveryBudgetExceeded):
            hybrid.compress(SECTIONED, max_chars=1000)


def test_structured_hybrid_never_cuts_below_min_head_chars():
    # A paragraph break 10 characters under the minimum used to be taken.
    source = "x" * 90 + "\n\n" + "x" * 1000 + "\n\n## S\n" + "b" * 2000
    hybrid = HybridCompressor(head_chars=500, min_head_chars=100)
    with delivery_scope("structured", 890, continuation_budget=890):
        with pytest.raises(DeliveryBudgetExceeded):
            hybrid.compress(source, max_chars=890)
    with delivery_scope("structured", 1000, continuation_budget=1000):
        out = hybrid.compress(source, max_chars=1000)
    assert len(out.descriptor["payload"]["head"]) >= 100


# Quotes cost four serialized characters; a lone surrogate costs seven, since
# the delivery serializer escapes what plain ``json.dumps`` leaves raw.
@pytest.mark.parametrize("filler", ['"', "\ud800"], ids=["quote", "lone-surrogate"])
def test_structured_hybrid_shrink_counts_escaped_head_cost(filler):
    # The head ends in escaped characters.
    # Removing the excess as raw characters skipped past the 375-character
    # cut that fits and landed on a minimum head with five more entries.
    prefix = "p" * 100 + "\n\n" + "".join(f"## Earlier {i}\n" + "a" * 40 + "\n\n" for i in range(5))
    source = prefix + "## First\n" + filler * 700 + "\n\n## Second\n" + "b" * 2000
    calls: list[int] = []
    selective = SelectiveCompressor()
    original = selective.compress

    def spy(text, **kwargs):
        calls.append(len(kwargs["hybrid_head"]))
        return original(text, **kwargs)

    selective.compress = spy  # type: ignore[method-assign]
    hybrid = HybridCompressor(head_chars=497, min_head_chars=100, selective_compressor=selective)
    with delivery_scope("structured", 1300, continuation_budget=1300):
        out = hybrid.compress(source, max_chars=1300)
    assert calls == [497, 375]
    assert out.descriptor["payload"]["head"] == source[:375]
    assert delivery_size(out.descriptor, owned=False) <= 1300
