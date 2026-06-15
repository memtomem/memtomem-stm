"""bench_qa — determinism diff gate.

Two runs of the same scenario under the same ``run_seed`` must produce
byte-identical ``ScenarioReport`` entries after stripping wall-clock
timings. This is the load-bearing assumption that makes ``report.json``
diffs meaningful as a regression signal: without it, every PR's bench
artifact differs for reasons unrelated to the code under test.

Scope: the full recorded suite. Every fixture that the live bench_qa run
records as a ``ScenarioReport`` row is replayed twice in fresh
``tmp_path`` dirs via :func:`bench.bench_qa.run_scenario_once`, which
drives the same pipeline the gate tests use (happy path / fallback ladder
/ surfacing) and records the same fields — so a desynchronised compressor,
fallback ladder, or surfacing path shows up here as a report diff. The
``s09``-only predecessor only proved the property for one AUTO scenario;
this gate covers the compaction strategies (AUTO/SELECTIVE/SKELETON), the
three fallback tiers, and the surfacing recall path.

Cross-process note: the recorded fields are all hash-seed-independent
(byte counts, categorical strategy, ``sha256`` chunk IDs, recall floats,
``sha256``-derived trace_id), verified by replaying every scenario under
several ``PYTHONHASHSEED`` values. The two runs below therefore share a
process safely — drift would only appear cross-process if a recorded
field became hash-ordering-dependent, which the roster tripwire and a
varied-seed manual run would catch.
"""

from __future__ import annotations

import pytest

from bench.bench_qa import (
    SUITE_SCENARIOS,
    canonicalize_report,
    deterministic_trace_id,
    fixtures_dir,
    run_scenario_once,
)

# Every fixture that records a ``ScenarioReport`` row into ``report.json``.
# Sourced from the canonical roster in ``bench_qa.replay`` (shared with the
# cross-version drift gate) so the two gates can never drift apart; the
# tripwire below still forces a conscious include/exclude decision when a
# fixture is added. Not globbed — the roster is explicit on purpose.
DETERMINISM_SCENARIOS = SUITE_SCENARIOS

# Fixtures intentionally outside the determinism gate, each with its reason.
_EXCLUDED_FIXTURES = {
    "s11": (
        "F2 min_score sweep — a bench_qa_sweep measurement run emitting a "
        "curve sidecar, not a ScenarioReport row "
        "(test_bench_qa_scenarios.test_s11_min_score_sweep)."
    ),
}


@pytest.mark.bench_qa
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_id", DETERMINISM_SCENARIOS)
async def test_two_runs_same_seed_produce_canonically_equal_reports(scenario_id, tmp_path_factory):
    tmp_a = tmp_path_factory.mktemp(f"bench_det_{scenario_id}_a")
    tmp_b = tmp_path_factory.mktemp(f"bench_det_{scenario_id}_b")

    report_a = await run_scenario_once(scenario_id, tmp_a)
    report_b = await run_scenario_once(scenario_id, tmp_b)

    canon_a = canonicalize_report(report_a)
    canon_b = canonicalize_report(report_b)

    # trace_id must survive canonicalization — it's deterministic and
    # differences there indicate an injection bug, not wall-clock noise.
    scenario_a = canon_a["scenarios"][0]
    assert scenario_a["trace_id"] == deterministic_trace_id(scenario_id), (
        f"{scenario_id}: trace_id is not the deterministic value — injection bug"
    )

    assert canon_a == canon_b, (
        f"bench_qa determinism broken — two runs of {scenario_id} at run_seed=0 "
        f"diverged after canonicalization. Inspect:\nA={canon_a!r}\nB={canon_b!r}"
    )


@pytest.mark.bench_qa
def test_determinism_roster_covers_every_fixture():
    """Tripwire against silent under-coverage: every ``s*.json`` fixture must
    be either in :data:`DETERMINISM_SCENARIOS` or :data:`_EXCLUDED_FIXTURES`
    (with a documented reason). A new fixture trips this until it's classified.
    """
    on_disk = {p.stem for p in fixtures_dir().glob("s*.json")}
    covered = set(DETERMINISM_SCENARIOS) | set(_EXCLUDED_FIXTURES)

    unclassified = on_disk - covered
    assert not unclassified, (
        f"new fixture(s) {sorted(unclassified)} are neither in DETERMINISM_SCENARIOS "
        f"nor _EXCLUDED_FIXTURES — add each to the determinism gate or document why "
        f"it is excluded."
    )
    stale = set(DETERMINISM_SCENARIOS) - on_disk
    assert not stale, f"DETERMINISM_SCENARIOS references missing fixture(s): {sorted(stale)}"


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


@pytest.mark.bench_qa
def test_canonicalize_strips_llm_judge_agreement():
    """The item-9 keys ``llm_judge_b`` + ``llm_judge_agreement`` are derived
    from drift-prone LLM scores, so canonicalization must strip them alongside
    ``llm_judge`` — otherwise the multi-model agreement marker would break the
    two-run determinism diff (and the #487 drift snapshot)."""
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
                "llm_judge": {"model": "gpt-4.1-nano", "overall": 0.9},
                "llm_judge_b": {"model": "claude-haiku-4-5-20251001", "overall": 0.7},
                "llm_judge_agreement": {
                    "model_a": "gpt-4.1-nano",
                    "model_b": "claude-haiku-4-5-20251001",
                    "agreement_score": 0.8,
                },
            }
        ],
        "tier_histogram": {"none": 1},
        "totals": {"scenarios": 1.0},
    }
    scenario = canonicalize_report(sample)["scenarios"][0]  # type: ignore[arg-type]
    assert "llm_judge" not in scenario
    assert "llm_judge_b" not in scenario
    assert "llm_judge_agreement" not in scenario
    # Determinism-safe fields still survive the strip.
    assert scenario["metrics"]["compressed_chars"] == 80
    assert scenario["trace_id"] == "bench-abc"
    # Original must not be mutated.
    assert "llm_judge_agreement" in sample["scenarios"][0]


@pytest.mark.bench_qa
def test_compute_agreement_metric_math():
    """Pin the item-9 agreement metric: per-dimension absolute diffs, the
    ``1 - mean(diffs)`` score (rounded 4 dp, clamped to [0, 1]), and the
    either-error degrade path that omits numerics so a failed judge can't
    poison the cross-scenario correlation. No API key — runs in default CI."""
    from bench.bench_qa.llm_judge_adapter import compute_agreement

    report_a = {
        "model": "gpt-4.1-nano",
        "overall": 0.80,
        "factual_completeness": 0.90,
        "structural_coherence": 0.70,
        "answer_sufficiency": 1.00,
    }
    report_b = {
        "model": "claude-haiku-4-5-20251001",
        "overall": 0.60,
        "factual_completeness": 0.90,
        "structural_coherence": 0.50,
        "answer_sufficiency": 0.80,
    }
    agreement = compute_agreement(report_a, report_b)  # type: ignore[arg-type]
    assert agreement["model_a"] == "gpt-4.1-nano"
    assert agreement["model_b"] == "claude-haiku-4-5-20251001"
    assert agreement["overall_a"] == 0.80
    assert agreement["overall_b"] == 0.60
    assert agreement["diff_overall"] == 0.2
    assert agreement["diff_factual_completeness"] == 0.0
    assert agreement["diff_structural_coherence"] == 0.2
    assert agreement["diff_answer_sufficiency"] == 0.2
    # 1 - mean(0.2, 0.0, 0.2, 0.2) = 1 - 0.15 = 0.85
    assert agreement["agreement_score"] == 0.85
    assert "error" not in agreement

    # Either-error degrade: numeric fields omitted, error carries both sides.
    degraded = compute_agreement(
        {"model": "gpt-4.1-nano", "overall": 0.0, "error": "boom"},  # type: ignore[arg-type]
        report_b,  # type: ignore[arg-type]
    )
    assert degraded["model_a"] == "gpt-4.1-nano"
    assert degraded["model_b"] == "claude-haiku-4-5-20251001"
    assert "error" in degraded
    assert "boom" in degraded["error"]
    assert "agreement_score" not in degraded
    assert "diff_overall" not in degraded
