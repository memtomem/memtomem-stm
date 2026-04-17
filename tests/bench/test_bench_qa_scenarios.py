"""bench_qa — end-to-end scenario gates for ``ProxyManager.call_tool()``.

Each scenario loads a JSON fixture from ``tests/bench/fixtures/``, drives a
live ``ProxyManager`` against an AsyncMock upstream, and asserts the gates
defined in ``/Users/pdstudio/.claude/plans/mcp-snug-river.md``.

This P2 pass covers the non-fallback happy-path scenarios (S2–S4, S9):
content fits the dynamic retention floor, so ``ratio_violation`` must stay
0, the compressor must resolve ``AUTO`` to a concrete strategy, and the
QA probes must remain answerable after compression.

Fallback-ladder (S1/S6/S8 variants), progressive round-trip, and
surfacing (S10) assertions live in later PRs.
"""

from __future__ import annotations

import pytest

from bench.bench_qa import (
    deterministic_trace_id,
    latest_metrics_row,
    load_fixture,
    make_proxy_manager,
    qa_answerable_ratio,
)
from bench.bench_qa.runner import make_tool_result

# Scenarios exercised by this file. Each must live in
# ``tests/bench/fixtures/<id>.json`` with a ``force_tier: null`` so the
# happy-path gates apply.
NORMAL_PATH_SCENARIOS = ["s02", "s03", "s04", "s09"]


@pytest.mark.bench_qa
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_id", NORMAL_PATH_SCENARIOS)
async def test_bench_qa_normal_path(scenario_id: str, tmp_path, bench_qa_report):
    fixture = load_fixture(scenario_id)
    assert fixture.get("force_tier") is None, (
        f"{scenario_id} has force_tier set; it belongs to the fallback-ladder suite"
    )

    mgr, store, session = make_proxy_manager(
        tmp_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
    )
    session.call_tool.return_value = make_tool_result(fixture["payload"])

    expected_trace_id = deterministic_trace_id(fixture["scenario_id"])
    result = await mgr.call_tool("fake", f"tool_{scenario_id}", {}, trace_id=expected_trace_id)

    row = latest_metrics_row(store)
    try:
        assert row, f"{scenario_id}: proxy_metrics row was not written"
        assert row["trace_id"] == expected_trace_id, (
            f"{scenario_id}: trace_id mismatch — "
            f"got {row['trace_id']!r}, expected {expected_trace_id!r}"
        )

        assert row["ratio_violation"] == 0, (
            f"{scenario_id}: unexpected ratio_violation=1 on happy path "
            f"(cleaned={row['cleaned_chars']}, compressed={row['compressed_chars']})"
        )
        assert row["compression_strategy"] is not None, (
            f"{scenario_id}: strategy column was not recorded"
        )
        assert row["compression_strategy"] != "auto", (
            f"{scenario_id}: strategy must be resolved before recording "
            f"(got {row['compression_strategy']!r})"
        )
        assert row["original_chars"] == len(fixture["payload"]), (
            f"{scenario_id}: original_chars={row['original_chars']} "
            f"expected={len(fixture['payload'])}"
        )

        answerable, total = qa_answerable_ratio(fixture["qa_probes"], result)
        assert total > 0, f"{scenario_id}: must define at least one qa_probe"
        ratio = answerable / total
        gate_min = fixture.get("qa_gate_min", 0.75)
        assert ratio >= gate_min, (
            f"{scenario_id}: qa_answerable ratio {answerable}/{total}={ratio:.2f} "
            f"below {gate_min} gate; strategy={row['compression_strategy']!r}"
        )

        bench_qa_report.record_scenario(
            scenario_id=scenario_id,
            trace_id=row["trace_id"],
            row=row,
            qa_answerable=answerable,
            qa_total=total,
            original_chars=len(fixture["payload"]),
            verdict="pass",
        )
    finally:
        store.close()
