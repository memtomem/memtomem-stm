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

S7 is a dedicated test: it pins ``compression="selective"`` so the
SelectiveCompressor's TOC contract can be exercised directly. SELECTIVE
intentionally skips the fallback ladder (``manager.py:1300-1308``), so it
can't share the happy-path parametrize body.
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


@pytest.mark.bench_qa
@pytest.mark.asyncio
async def test_s07_selective_toc_preserves_top_results(tmp_path, bench_qa_report):
    """50-item ranked search → SELECTIVE TOC must keep top-ranked IDs visible.

    SELECTIVE is a two-phase protocol: the compressor returns a compact TOC,
    the agent then calls ``stm_proxy_select_chunks`` to retrieve full content.
    Because the TOC is intentionally compact, the ratio guard does *not*
    fall back for this strategy — so the scenario-level gate is the demotion
    guard instead: the qa_probes encode top-ranked identifiers that must all
    survive inside the 80-char preview window per entry.
    """
    fixture = load_fixture("s07")
    assert fixture.get("force_tier") is None, "s07 is a happy-path scenario"
    assert fixture["expected_compressor"] == "selective", (
        "s07 exercises SELECTIVE directly — do not switch it to AUTO"
    )

    mgr, store, session = make_proxy_manager(
        tmp_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
    )
    session.call_tool.return_value = make_tool_result(fixture["payload"])

    expected_trace_id = deterministic_trace_id(fixture["scenario_id"])
    result = await mgr.call_tool("fake", "tool_s07", {}, trace_id=expected_trace_id)

    row = latest_metrics_row(store)
    try:
        assert row, "s07: proxy_metrics row was not written"
        assert row["trace_id"] == expected_trace_id, (
            f"s07: trace_id mismatch — got {row['trace_id']!r}, expected {expected_trace_id!r}"
        )
        assert row["compression_strategy"] == "selective", (
            f"s07: strategy must remain 'selective' (no fallback ladder for this "
            f"strategy), got {row['compression_strategy']!r}"
        )
        assert row["original_chars"] == len(fixture["payload"])

        # TOC shape — `manager.py:1300-1308` documents that SELECTIVE skips the
        # fallback ladder even under ratio_violation, so the result must still
        # be the raw TOC envelope.
        assert '"type": "toc"' in result, (
            f"s07: result is not a SELECTIVE TOC envelope: {result[:160]!r}"
        )
        assert '"selection_key"' in result
        assert '"entries"' in result

        # Demotion guard: a bug that drops early entries or shrinks the
        # 80-char preview window would strip top-ranked IDs from the TOC.
        answerable, total = qa_answerable_ratio(fixture["qa_probes"], result)
        assert total > 0, "s07: must define at least one qa_probe"
        ratio = answerable / total
        gate_min = fixture.get("qa_gate_min", 0.75)
        assert ratio >= gate_min, (
            f"s07: demotion guard {answerable}/{total}={ratio:.2f} below "
            f"{gate_min} gate; top-ranked IDs may have fallen out of the TOC "
            f"(first 200 chars: {result[:200]!r})"
        )

        bench_qa_report.record_scenario(
            scenario_id="s07",
            trace_id=row["trace_id"],
            row=row,
            qa_answerable=answerable,
            qa_total=total,
            original_chars=len(fixture["payload"]),
            verdict="pass",
        )
    finally:
        store.close()
