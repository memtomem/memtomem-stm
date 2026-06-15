"""Single-scenario replay driver for bench_qa determinism checks.

Drives any fixture scenario through the same ``ProxyManager`` pipeline the
gate tests use — minus the gate assertions — and returns a single-scenario
``BenchReport`` dict ready for :func:`canonicalize_report`. Centralizing
the per-scenario driving here keeps the full-suite determinism gate thin
*and* faithful: the driver records the exact fields the live suite records
(``metrics`` byte counts, ``qa`` ratio, ``progressive`` round-trip,
``surfacing`` recall), so a non-deterministic compressor / fallback ladder
/ surfacing path surfaces as a report diff in the gate rather than silently
drifting in CI's ``report.json``.

Dispatch mirrors the gate tests' split (``test_bench_qa_scenarios.py`` +
``test_bench_qa_fallback.py``):

* ``surfacing_seeds`` present → live surfacing pipeline (s10)
* ``force_tier`` set         → forced fallback ladder (s01/s06/s08)
* otherwise                  → happy path (s02/s03/s04/s05/s07/s09)

The driver intentionally does **not** assert the gates — it only needs the
recorded report row. The gate assertions stay in the scenario test files;
this module is the determinism gate's faithful, assertion-free replay.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

from .judge import qa_answerable_ratio, surfacing_recall_at_k
from .loader import load_fixture
from .progressive import reassemble
from .report import BenchReportCollector
from .runner import (
    deterministic_trace_id,
    latest_metrics_row,
    make_proxy_manager,
    make_surfacing_proxy_manager,
    make_tool_result,
)
from .schema import BenchFixture, SurfacingResult

# Canonical roster: every fixture that records a ``ScenarioReport`` row into
# ``report.json``. This is the single source of truth shared by the determinism
# gate (``test_bench_qa_determinism.py``) and the cross-version drift gate
# (``test_bench_qa_drift.py`` + ``run_full_suite`` below) — both operate on
# exactly this set, so duplicating the list in two test files would risk drift.
# Sweep fixtures (s11) emit only a sidecar, not a ScenarioReport row, and are
# excluded (see ``test_bench_qa_determinism._EXCLUDED_FIXTURES``).
SUITE_SCENARIOS = [
    "s01",  # Tier-2 hybrid_fallback
    "s02",  # AUTO → extract_fields
    "s03",  # AUTO → truncate
    "s04",  # AUTO → truncate
    "s05",  # SKELETON (chat transcript)
    "s06",  # Tier-1 progressive_fallback (round-trip)
    "s07",  # SELECTIVE TOC
    "s08",  # Tier-3 truncate_fallback
    "s09",  # AUTO → none (fits budget)
    "s10",  # surfacing recall@k
]

# Mirror of ``test_bench_qa_scenarios.py::_SURFACING_ID_RE`` — matches the
# formatter's ``_surfacing_id: <id>_`` line and the rating-spec callable
# rendered just below it.
_SURFACING_ID_RE = re.compile(r"surfacing_id[:=]\s*\"?([a-f0-9]{16})")


def _stub_raise(*_args, **_kwargs):
    raise RuntimeError("forced fallback-ladder failure (bench_qa replay)")


async def _record_plain(
    fixture: BenchFixture, tmp_path: Path, collector: BenchReportCollector
) -> None:
    """Happy path: AUTO / SELECTIVE / SKELETON resolve a concrete strategy
    and the content fits the (possibly lowered) retention floor."""
    scenario_id = fixture["scenario_id"]
    mgr, store, session = make_proxy_manager(
        tmp_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
        min_retention=fixture.get("min_retention", 0.65),
    )
    session.call_tool.return_value = make_tool_result(fixture["payload"])
    trace_id = deterministic_trace_id(scenario_id)
    try:
        result = await mgr.call_tool("fake", f"tool_{scenario_id}", {}, trace_id=trace_id)
        row = latest_metrics_row(store)
        answerable, total = qa_answerable_ratio(fixture.get("qa_probes", []), result)
        collector.record_scenario(
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


async def _record_fallback(
    fixture: BenchFixture, tmp_path: Path, collector: BenchReportCollector
) -> None:
    """Forced fallback ladder: stub ``_apply_compression`` to overshoot the
    retention floor, then stub the higher tiers off per ``force_tier`` so a
    specific ladder rung runs. Tier-1 records the progressive round-trip."""
    scenario_id = fixture["scenario_id"]
    force_tier = fixture["force_tier"] or 0
    mgr, store, session = make_proxy_manager(
        tmp_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
    )
    session.call_tool.return_value = make_tool_result(fixture["payload"])
    mgr._apply_compression = AsyncMock(return_value=("x" * 50, None))
    if force_tier >= 2:
        mgr._apply_progressive = _stub_raise  # type: ignore[method-assign]
    if force_tier >= 3:
        mgr._apply_hybrid = _stub_raise  # type: ignore[method-assign]
    trace_id = deterministic_trace_id(scenario_id)
    try:
        first_chunk = await mgr.call_tool("fake", f"tool_{scenario_id}", {}, trace_id=trace_id)
        row = latest_metrics_row(store)
        progressive = None
        if force_tier == 1:
            # Round-trip equality is the gate test's assertion; here we only
            # need the (deterministic) chunk count + total chars it records.
            reassembled = reassemble(mgr, first_chunk)
            progressive = {
                "round_trip_equal": True,
                "chunks": reassembled.chunks,
                "total_chars": len(reassembled.content),
            }
        collector.record_scenario(
            scenario_id=scenario_id,
            trace_id=row["trace_id"],
            row=row,
            qa_answerable=0,
            qa_total=0,
            original_chars=len(fixture["payload"]),
            verdict="pass",
            progressive=progressive,
        )
    finally:
        store.close()


async def _record_surfacing(
    fixture: BenchFixture, tmp_path: Path, collector: BenchReportCollector
) -> None:
    """Live surfacing pipeline: spawn the fake LTM (``--seeds``) so chunk IDs
    are deterministic, then record recall@k off the formatter-injected
    surfacing_id (not ``ORDER BY created_at``) so the row is self-contained."""
    scenario_id = fixture["scenario_id"]
    seeds = fixture["surfacing_seeds"]
    seeds_path = tmp_path / f"{scenario_id}_seeds.json"
    seeds_path.write_text(json.dumps(seeds), encoding="utf-8")

    source_to_chunk_id = {
        seed["source"]: hashlib.sha256(seed["content"].encode()).hexdigest()[:16] for seed in seeds
    }
    expected_chunk_ids = [
        source_to_chunk_id[src] for src in fixture["surfacing_eval"]["expected_ids"]
    ]
    k = fixture["surfacing_eval"]["k"]

    mgr, store, session, adapter, _engine, tracker = make_surfacing_proxy_manager(
        tmp_path,
        seeds_path=seeds_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
    )
    session.call_tool.return_value = make_tool_result(fixture["payload"])
    trace_id = deterministic_trace_id(scenario_id)

    await adapter.start()
    try:
        result = await mgr.call_tool(
            "fake",
            f"tool_{scenario_id}",
            {"_context_query": fixture["surfacing_eval"]["query"]},
            trace_id=trace_id,
        )
        row = latest_metrics_row(store)
        id_match = _SURFACING_ID_RE.search(result)
        returned_ids = (
            tracker.store.get_memory_ids_for_surfacing(id_match.group(1)) if id_match else []
        )
        recall = surfacing_recall_at_k(returned_ids, expected_chunk_ids, k)
        answerable, total = qa_answerable_ratio(fixture.get("qa_probes", []), result)
        collector.record_scenario(
            scenario_id=scenario_id,
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


async def _dispatch(fixture: BenchFixture, tmp_path: Path, collector: BenchReportCollector) -> None:
    """Record one scenario into *collector*, dispatching on fixture shape.

    A new scenario is covered automatically once it declares the standard
    fields (``force_tier`` / ``surfacing_seeds``). Shared by
    :func:`run_scenario_once` (one scenario) and :func:`run_full_suite`
    (the whole roster into a single collector).
    """
    if fixture.get("surfacing_seeds"):
        await _record_surfacing(fixture, tmp_path, collector)
    elif fixture.get("force_tier"):
        await _record_fallback(fixture, tmp_path, collector)
    else:
        await _record_plain(fixture, tmp_path, collector)


async def run_scenario_once(scenario_id: str, tmp_path: Path, *, run_seed: int = 0) -> dict:
    """Drive *scenario_id* through the full bench pipeline once and return a
    single-scenario ``BenchReport`` dict.

    Two calls with the same ``scenario_id`` (fresh ``tmp_path`` each) must
    canonicalize-equal — that is what the determinism gate asserts.
    """
    fixture = load_fixture(scenario_id)
    collector = BenchReportCollector()
    await _dispatch(fixture, tmp_path, collector)
    return dict(collector.build_report(run_seed=run_seed))


async def run_full_suite(
    base_dir: Path, *, scenario_ids: list[str] | None = None, run_seed: int = 0
) -> dict:
    """Replay the whole roster into one report — the canonical full ``report.json``.

    Each scenario runs in its own ``base_dir/<scenario_id>`` subdir so the
    per-run stores never cross-talk (``latest_metrics_row`` reads the most
    recent ``proxy_metrics`` row, so a shared DB would mis-attribute rows).
    Returns the merged multi-scenario ``BenchReport`` dict — feed it through
    :func:`canonicalize_report` before diffing against the committed drift
    baseline. The result is byte-deterministic across runs and hash seeds,
    which is what makes a committed snapshot baseline sound.

    ``scenario_ids`` defaults to :data:`SUITE_SCENARIOS`; pass an explicit
    subset only in tests that need a narrower roster.
    """
    roster = SUITE_SCENARIOS if scenario_ids is None else scenario_ids
    collector = BenchReportCollector()
    for scenario_id in roster:
        fixture = load_fixture(scenario_id)
        scenario_dir = base_dir / scenario_id
        scenario_dir.mkdir(parents=True, exist_ok=True)
        await _dispatch(fixture, scenario_dir, collector)
    return dict(collector.build_report(run_seed=run_seed))
