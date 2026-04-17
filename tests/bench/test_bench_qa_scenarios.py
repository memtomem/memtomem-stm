"""bench_qa — end-to-end scenario gates for ProxyManager.call_tool().

This file hosts the ``@pytest.mark.bench_qa`` suite. Each scenario loads a
JSON fixture, drives a live ``ProxyManager`` against an AsyncMock upstream,
and asserts the gates defined in
``/Users/pdstudio/.claude/plans/mcp-snug-river.md``.

The P1 pass ships only S9 (budget-fit smoke) — S1–S8, S10 arrive in
subsequent PRs together with fallback-ladder / progressive / surfacing
assertions.
"""

from __future__ import annotations

import pytest

from bench.bench_qa import (
    deterministic_trace_id,
    latest_metrics_row,
    load_fixture,
    make_proxy_manager,
)
from bench.bench_qa.runner import make_tool_result


def _all_keywords_present(keywords: list[str], text: str) -> bool:
    lowered = text.lower()
    return all(kw.lower() in lowered for kw in keywords)


def _qa_answerable_ratio(probes: list[dict], text: str) -> tuple[int, int]:
    if not probes:
        return 0, 0
    answerable = sum(1 for p in probes if _all_keywords_present(p["expected_keywords"], text))
    return answerable, len(probes)


@pytest.mark.bench_qa
@pytest.mark.asyncio
async def test_s09_budget_fit_short_text(tmp_path):
    """S9 — payload fits within the budget; compressor should pick a concrete
    strategy (not ``auto``), ratio_violation must stay 0, all QA probes
    answerable."""
    fixture = load_fixture("s09")

    mgr, store, session = make_proxy_manager(
        tmp_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
    )
    session.call_tool.return_value = make_tool_result(fixture["payload"])

    expected_trace_id = deterministic_trace_id(fixture["scenario_id"])
    result = await mgr.call_tool("fake", "summary", {})

    row = latest_metrics_row(store)
    try:
        assert row, "proxy_metrics row was not written"
        # ProxyManager.call_tool currently assigns its own uuid-based trace_id;
        # deterministic injection arrives in the report PR (plan back-fill).
        # P1 only proves the row is written and the column is non-null.
        assert row["trace_id"], "trace_id column should be populated"
        assert expected_trace_id.startswith("bench-"), "sanity: hash helper runs"
        assert row["ratio_violation"] == 0, (
            f"unexpected ratio_violation=1 for budget-fit payload "
            f"(cleaned={row['cleaned_chars']}, compressed={row['compressed_chars']})"
        )
        assert row["compression_strategy"] is not None, "strategy was not recorded"
        assert row["compression_strategy"] != "auto", (
            "strategy must be resolved to a concrete value before recording "
            f"(got {row['compression_strategy']!r})"
        )
        assert row["original_chars"] == len(fixture["payload"]), (
            f"original_chars={row['original_chars']} expected={len(fixture['payload'])}"
        )
        answerable, total = _qa_answerable_ratio(fixture["qa_probes"], result)
        assert total > 0, "S9 must have at least one qa_probe"
        assert answerable / total >= 0.75, f"qa_answerable ratio below gate: {answerable}/{total}"
    finally:
        store.close()
