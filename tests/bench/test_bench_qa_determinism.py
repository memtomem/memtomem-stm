"""bench_qa — determinism diff gate.

Two runs of the same scenario under the same ``run_seed`` must produce
byte-identical ``ScenarioReport`` entries after stripping wall-clock
timings. This is the load-bearing assumption that makes ``report.json``
diffs meaningful as a regression signal: without it, every PR's bench
artifact differs for reasons unrelated to the code under test.

Scope: one scenario (s09, the smallest budget-fit fixture) exercised
twice in fresh ``tmp_path`` dirs — enough to prove the property per
scenario without pulling every bench_qa case into a single test. If a
future change desynchronises a specific compressor, the scenario-local
determinism check for that scenario can be added alongside the regular
gate assertions.
"""

from __future__ import annotations

import pytest

from bench.bench_qa import (
    BenchReportCollector,
    canonicalize_report,
    deterministic_trace_id,
    latest_metrics_row,
    load_fixture,
    make_proxy_manager,
)
from bench.bench_qa.runner import make_tool_result


async def _run_s09_once(tmp_path) -> dict:
    """Drive s09 through the full bench pipeline once, return the
    single-scenario ``BenchReport`` dict."""
    fixture = load_fixture("s09")
    mgr, store, session = make_proxy_manager(
        tmp_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
    )
    session.call_tool.return_value = make_tool_result(fixture["payload"])
    trace_id = deterministic_trace_id("s09")

    try:
        await mgr.call_tool("fake", "tool_s09", {}, trace_id=trace_id)
        row = latest_metrics_row(store)
    finally:
        store.close()

    collector = BenchReportCollector()
    collector.record_scenario(
        scenario_id="s09",
        trace_id=row["trace_id"],
        row=row,
        qa_answerable=0,
        qa_total=0,
        original_chars=len(fixture["payload"]),
        verdict="pass",
    )
    return dict(collector.build_report(run_seed=0))


@pytest.mark.bench_qa
@pytest.mark.asyncio
async def test_two_runs_same_seed_produce_canonically_equal_reports(tmp_path_factory):
    tmp_a = tmp_path_factory.mktemp("bench_det_a")
    tmp_b = tmp_path_factory.mktemp("bench_det_b")

    report_a = await _run_s09_once(tmp_a)
    report_b = await _run_s09_once(tmp_b)

    canon_a = canonicalize_report(report_a)
    canon_b = canonicalize_report(report_b)

    # trace_id must survive canonicalization — it's deterministic and
    # differences there indicate an injection bug, not wall-clock noise.
    scenario_a = canon_a["scenarios"][0]
    assert scenario_a["trace_id"] == deterministic_trace_id("s09")

    assert canon_a == canon_b, (
        "bench_qa determinism broken — two runs of s09 at run_seed=0 "
        "diverged after canonicalization. Inspect: "
        f"A={canon_a!r} B={canon_b!r}"
    )


@pytest.mark.bench_qa
def test_canonicalize_strips_only_stage_timings():
    """Guard against over-eager stripping — byte-counted and categorical
    fields must survive canonicalization, only wall-clock stage timings
    are removed."""
    from bench.bench_qa.report import _STAGE_TIMING_FIELDS

    sample = {
        "schema_version": 1,
        "run_seed": 0,
        "scenarios": [
            {
                "scenario_id": "s09",
                "trace_id": "bench-abc",
                "metrics": {
                    "original_chars": 100,
                    "cleaned_chars": 95,
                    "compressed_chars": 80,
                    "compression_ratio": 0.84,
                    "compression_strategy": "none",
                    "ratio_violation": 0,
                    "surfacing_on_progressive_ok": None,
                    "surface_error": None,
                    "clean_ms": 1.23,
                    "compress_ms": 4.56,
                    "surface_ms": 0.0,
                },
                "tier": "none",
                "verdict": "pass",
            }
        ],
        "tier_histogram": {"none": 1},
        "totals": {"scenarios": 1.0},
    }
    result = canonicalize_report(sample)  # type: ignore[arg-type]
    metrics = result["scenarios"][0]["metrics"]
    for field in _STAGE_TIMING_FIELDS:
        assert field not in metrics, f"{field} should have been stripped"
    # Byte counts + strategy survive.
    assert metrics["original_chars"] == 100
    assert metrics["compressed_chars"] == 80
    assert metrics["compression_strategy"] == "none"
    assert result["scenarios"][0]["trace_id"] == "bench-abc"
    # Original must not be mutated.
    assert "clean_ms" in sample["scenarios"][0]["metrics"]
