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

import hashlib
import json
import re

import pytest

from bench.bench_qa import (
    deterministic_trace_id,
    latest_metrics_row,
    load_fixture,
    make_proxy_manager,
    qa_answerable_ratio,
    surfacing_recall_at_k,
)
from bench.bench_qa.runner import make_surfacing_proxy_manager, make_tool_result
from bench.bench_qa.schema import SurfacingResult

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


_SURFACING_ID_RE = re.compile(r"Surfacing ID:\s*([a-f0-9]{16})")


@pytest.mark.bench_qa
@pytest.mark.asyncio
async def test_s10_surfacing_recall_at_k(tmp_path, bench_qa_report):
    """Fake LTM serves 3 fixture-declared seeds; top-2 match the expected ranks.

    The ``path`` argument (not ``_context_query``) drives ``ContextExtractor``
    because ``ProxyManager.call_tool`` strips ``_context_query`` from
    ``upstream_args`` before invoking surfacing (see ``manager.py:973``);
    ``_context_query`` is today a compression hint, not a surfacing hint.
    ``path`` tokenizes to ``"src auth jwt rotation py"`` which clears
    ``min_query_tokens=3``. The fake LTM ignores the query and returns
    fixture seeds verbatim, so query content does not affect recall —
    the test exercises the pipeline wiring, not query semantics.

    The fake server's per-call UUID suffix exists only in default mode
    to defeat ``sha256(content)`` dedup — bench_qa ``--seeds`` mode omits
    it so ``sha256(content)[:16]`` is deterministic, and the expected
    chunk IDs below (derived from ``fixture["surfacing_seeds"][i].content``)
    line up with what ``surfacing_events.memory_ids`` records. Future
    changes to the fake must preserve both properties.
    """
    fixture = load_fixture("s10")
    assert fixture.get("force_tier") is None, "s10 is a happy-path surfacing scenario"
    assert fixture["surfacing_seeds"], "s10 fixture must declare surfacing_seeds"
    assert fixture["surfacing_eval"], "s10 fixture must declare surfacing_eval"

    seeds_path = tmp_path / "s10_seeds.json"
    seeds_path.write_text(json.dumps(fixture["surfacing_seeds"]), encoding="utf-8")

    source_to_chunk_id = {
        seed["source"]: hashlib.sha256(seed["content"].encode()).hexdigest()[:16]
        for seed in fixture["surfacing_seeds"]
    }
    expected_chunk_ids = [
        source_to_chunk_id[src] for src in fixture["surfacing_eval"]["expected_ids"]
    ]

    mgr, store, session, adapter, engine, tracker = make_surfacing_proxy_manager(
        tmp_path,
        seeds_path=seeds_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
    )
    session.call_tool.return_value = make_tool_result(fixture["payload"])
    expected_trace_id = deterministic_trace_id(fixture["scenario_id"])

    await adapter.start()
    try:
        result = await mgr.call_tool(
            "fake",
            "tool_s10",
            {
                "path": "src/auth/jwt_rotation.py",
                "_context_query": fixture["surfacing_eval"]["query"],
            },
            trace_id=expected_trace_id,
        )

        row = latest_metrics_row(store)
        assert row, "s10: proxy_metrics row was not written"
        assert row["trace_id"] == expected_trace_id, (
            f"s10: trace_id mismatch — got {row['trace_id']!r}, expected {expected_trace_id!r}"
        )

        # Text-level reachability: the surfaced block is present AND at
        # least one seed's content surfaced through the formatter. DB-only
        # recall could pass while the formatter silently dropped content;
        # text-only coverage is fragile to formatter changes. Both guards
        # together cover each failure class.
        assert "<surfaced-memories>" in result, (
            f"s10: <surfaced-memories> block missing: {result[-400:]!r}"
        )
        assert any(seed["content"][:40] in result for seed in fixture["surfacing_seeds"]), (
            f"s10: no seed content surfaced into result: {result[-800:]!r}"
        )

        # DB-level recall@k via the surfacing_id embedded in the formatter's
        # output. Keying on surfacing_id (not ``ORDER BY created_at DESC``)
        # ties the assertion to *this* call's row, so the test survives a
        # future bench scenario writing additional rows into the same DB.
        id_match = _SURFACING_ID_RE.search(result)
        assert id_match, f"s10: surfacing_id not found in result: {result[-400:]!r}"
        surfacing_id = id_match.group(1)
        returned_ids = tracker.store.get_memory_ids_for_surfacing(surfacing_id)
        assert returned_ids, f"s10: no memory_ids recorded for surfacing_id={surfacing_id}"

        k = fixture["surfacing_eval"]["k"]
        recall = surfacing_recall_at_k(returned_ids, expected_chunk_ids, k)
        assert recall == 1.0, (
            f"s10: happy-path recall@{k} must be 1.0 — got {recall} "
            f"(returned_ids[:{k}]={returned_ids[:k]}, expected={expected_chunk_ids})"
        )

        answerable, total = qa_answerable_ratio(fixture["qa_probes"], result)
        assert total > 0, "s10: must define at least one qa_probe"
        ratio = answerable / total
        gate_min = fixture.get("qa_gate_min", 0.75)
        assert ratio >= gate_min, (
            f"s10: qa_answerable ratio {answerable}/{total}={ratio:.2f} below {gate_min} gate"
        )

        bench_qa_report.record_scenario(
            scenario_id="s10",
            trace_id=row["trace_id"],
            row=row,
            qa_answerable=answerable,
            qa_total=total,
            original_chars=len(fixture["payload"]),
            verdict="pass",
            surfacing=SurfacingResult(
                recall_at_k=recall,
                returned_ids=returned_ids[:k],
                expected_ids=expected_chunk_ids,
            ),
        )
    finally:
        await adapter.stop()
        tracker.close()
        store.close()
