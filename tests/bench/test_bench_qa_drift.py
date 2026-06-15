"""bench_qa — cross-version drift gate (post-gate roadmap item 8).

The per-PR ``bench_qa`` gate enforces *absolute* per-scenario floors
(``qa_gate_min``, ``ratio_violation``). A metric can sit just above its floor
forever while eroding across many PRs — each one passing its own floor, the
slow slide invisible. This gate closes that gap: it pins the WHOLE canonical
``report.json`` to a committed baseline (``fixtures/drift_baseline.json``) so
any move — even one still above the floor — must pass through a reviewed
baseline diff.

Why an exact snapshot rather than per-metric tolerances: the suite is
byte-deterministic (the determinism gate, #486, proves report.json is
identical run-to-run and across PYTHONHASHSEED). With zero run-to-run
variance, a tolerance can only *mask* a real regression smaller than itself —
exactly the slow slide this gate exists to catch. So equality is the gate; a
non-gating directional classifier (``bench_qa.drift``) only frames the diff
as REGRESSION / IMPROVEMENT / NEUTRAL for the human reading the failure.

Marker split:
* ``@pytest.mark.bench_qa_drift`` — the baseline-comparison gate + the heavy
  full-suite rebuilds. ADVISORY: runs only in the dedicated CI job, excluded
  from the required ``bench_qa`` gate and the default ``test`` job, so a
  forgotten re-baseline never blocks merge during the observation window.
* unmarked — the machinery guards (classifier coverage, roster lockstep,
  marker pin, strip sanity). These run in the required ``test`` job: a broken
  invariant *should* block merge even while the gate itself is advisory.

When this gate reds on a legitimate change, run
``uv run python tests/bench/bench_qa/regen_drift_baseline.py`` and commit the
regenerated baseline in the same PR.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from bench.bench_qa import (
    SUITE_SCENARIOS,
    canonicalize_report,
    classify_drift,
    fixtures_dir,
    format_drift_md,
    run_full_suite,
)
from bench.bench_qa.drift import (
    SCENARIO_DIRECTION,
    SCENARIO_EXCLUDED,
    TOTALS_DIRECTION,
)
from bench.bench_qa.regen_drift_baseline import serialize_baseline

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASELINE_PATH = fixtures_dir() / "drift_baseline.json"
_REGEN_CMD = "uv run python tests/bench/bench_qa/regen_drift_baseline.py"


def _load_baseline() -> dict:
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# The gate (advisory marker)
# --------------------------------------------------------------------------


@pytest.mark.bench_qa_drift
@pytest.mark.asyncio
async def test_live_report_matches_committed_baseline(tmp_path):
    """The full canonical report must deep-equal the committed baseline."""
    baseline = _load_baseline()
    live = canonicalize_report(await run_full_suite(tmp_path))

    if live.get("schema_version") != baseline.get("schema_version"):
        pytest.fail(
            f"report schema_version bumped "
            f"({baseline.get('schema_version')} → {live.get('schema_version')}). "
            f"Regenerate the drift baseline: {_REGEN_CMD}"
        )

    if live != baseline:
        drifts = classify_drift(baseline, live)
        # classify_drift walks the whole report, so this fallback should be
        # unreachable — it guards against ever printing "No drift" inside a red
        # failure if a future field escapes the walk.
        summary = (
            format_drift_md(drifts)
            if drifts
            else "Reports differ but no field-level drift was classified — "
            "inspect the report.json deep-equal directly."
        )
        # Persist the directional summary as a CI artifact when a report dir
        # is configured (the advisory job sets BENCH_QA_REPORT_DIR).
        report_dir = os.environ.get("BENCH_QA_REPORT_DIR")
        if report_dir:
            out = Path(report_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / "drift.md").write_text(summary, encoding="utf-8")
        pytest.fail(
            "bench_qa report drifted from the committed baseline.\n\n"
            f"{summary}\n"
            f"If this change is intended, regenerate the baseline:\n  {_REGEN_CMD}\n"
            "then commit tests/bench/fixtures/drift_baseline.json in this PR with a "
            "'## Baseline change' callout.\n"
            "(A large all-float diff usually means a rounding-precision change in report.py.)"
        )


@pytest.mark.bench_qa_drift
@pytest.mark.asyncio
async def test_regen_serialization_is_byte_identical_to_committed(tmp_path):
    """Re-serializing a fresh full-suite report must byte-match the committed
    file — catches a hand-edited baseline (wrong precision / key order /
    trailing newline) that a parsed-dict compare would silently tolerate."""
    live = canonicalize_report(await run_full_suite(tmp_path))
    regenerated = serialize_baseline(live)
    committed = _BASELINE_PATH.read_text(encoding="utf-8")
    assert regenerated == committed, (
        "serialized full-suite report is not byte-identical to the committed "
        f"baseline — regenerate it with: {_REGEN_CMD}"
    )


@pytest.mark.bench_qa_drift
@pytest.mark.asyncio
async def test_full_suite_is_rerun_stable(tmp_path_factory):
    """Two independent full-suite runs canonicalize-equal — a false-positive
    guard so the gate can never red on an unchanged tree."""
    a = canonicalize_report(await run_full_suite(tmp_path_factory.mktemp("drift_a")))
    b = canonicalize_report(await run_full_suite(tmp_path_factory.mktemp("drift_b")))
    assert a == b, "run_full_suite is not deterministic across two runs"


# --------------------------------------------------------------------------
# Machinery guards (unmarked → required test job)
# --------------------------------------------------------------------------


def test_drift_baseline_matches_suite_roster():
    """The committed baseline must cover exactly the canonical roster — guards
    against a silently shrunk report (a dropped scenario blessed by a regen)."""
    baseline = _load_baseline()
    ids = [s["scenario_id"] for s in baseline["scenarios"]]
    assert ids == sorted(SUITE_SCENARIOS), (
        f"baseline scenarios {ids} != SUITE_SCENARIOS {sorted(SUITE_SCENARIOS)} — "
        f"a fixture was added/removed without regenerating; run {_REGEN_CMD}"
    )
    assert baseline["schema_version"] == 1
    # Coverage floor: a snapshot scheme can't fully prevent a self-blessing
    # same-PR roster shrink (delete fixture + drop from SUITE_SCENARIOS +
    # regen), but pinning a minimum forces that reduction to be a deliberate,
    # reviewable edit to this number rather than a silent one.
    assert len(SUITE_SCENARIOS) >= 10, (
        f"SUITE_SCENARIOS shrank to {len(SUITE_SCENARIOS)} below the canonical 10 "
        "(s01–s10) — lowering coverage must be intentional; drop this floor "
        "explicitly with reviewer sign-off."
    )


def test_drift_baseline_has_no_nonreproducible_fields():
    """The committed baseline must already be canonicalized — no wall-clock
    stage timings and no llm_judge block (those would never reproduce)."""
    from bench.bench_qa.report import _STAGE_TIMING_FIELDS

    baseline = _load_baseline()
    for scenario in baseline["scenarios"]:
        assert "llm_judge" not in scenario, (
            f"{scenario['scenario_id']}: llm_judge leaked into baseline"
        )
        for field in _STAGE_TIMING_FIELDS:
            assert field not in scenario["metrics"], (
                f"{scenario['scenario_id']}: timing field {field} leaked into baseline"
            )


def test_direction_table_covers_every_schema_field():
    """Every field that can appear in a canonical ScenarioReport must be either
    direction-classified or explicitly excluded — no silent UNKNOWN drift.
    Parallel to the determinism roster tripwire."""
    from bench.bench_qa.report import _STAGE_TIMING_FIELDS
    from bench.bench_qa.schema import (
        MetricSummary,
        ProgressiveResult,
        QAResult,
        RuleJudgeResult,
        ScenarioReport,
        SurfacingResult,
    )

    sections = {
        "metrics": MetricSummary,
        "qa": QAResult,
        "surfacing": SurfacingResult,
        "progressive": ProgressiveResult,
        "rule_judge": RuleJudgeResult,
    }
    expected: set[str] = set()
    for section, td in sections.items():
        for field in td.__annotations__:
            expected.add(f"{section}.{field}")
    # Stage timings are stripped by canonicalize_report — never seen by the classifier.
    expected -= {f"metrics.{f}" for f in _STAGE_TIMING_FIELDS}
    # Scenario-level scalars — derived from the schema (not hard-coded) so a NEW
    # top-level ScenarioReport field (e.g. a cache_hit mirroring #485) trips this
    # until it is given a deliberate direction. llm_judge is stripped by
    # canonicalize_report; scenario_id is the row key.
    top_scalars = set(ScenarioReport.__annotations__) - set(sections) - {"scenario_id", "llm_judge"}
    expected |= top_scalars

    classified = set(SCENARIO_DIRECTION) | set(SCENARIO_EXCLUDED)
    missing = expected - classified
    assert not missing, (
        f"unclassified ScenarioReport fields {sorted(missing)} — add each to "
        f"SCENARIO_DIRECTION or SCENARIO_EXCLUDED in bench_qa/drift.py"
    )
    assert {
        "totals.scenarios",
        "totals.passing",
        "totals.failing",
        "totals.tokens_saved_approx",
    } <= set(TOTALS_DIRECTION)


def test_drift_marker_registered():
    """docs-sync-style pin: the bench_qa_drift marker must be registered in
    pyproject AND referenced in ci.yml, and excluded from the default test job
    filter — loud-fail 'update test + workflow together'."""
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    ci = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "bench_qa_drift:" in pyproject, (
        "bench_qa_drift marker missing from pyproject [tool.pytest] markers"
    )
    assert "bench_qa_drift" in ci, "bench_qa_drift not referenced in ci.yml (advisory job missing?)"
    assert "and not bench_qa_drift" in ci, (
        "default test job filter must exclude bench_qa_drift so the advisory gate "
        "does not run in (and block) the required test job"
    )


# --------------------------------------------------------------------------
# Classifier unit tests — planted drifts must be caught + labelled (unmarked)
# --------------------------------------------------------------------------


def _scenario_row(sid: str = "s03") -> dict:
    return {
        "scenario_id": sid,
        "trace_id": "bench-deadbeefdeadbeef",
        "metrics": {
            "original_chars": 1672,
            "cleaned_chars": 1671,
            "compressed_chars": 1379,
            "compression_ratio": 0.8253,
            "compression_strategy": "truncate",
            "ratio_violation": 0,
            "surfacing_on_progressive_ok": None,
            "surface_error": None,
        },
        "rule_judge": {"score": 0.0, "missing_keywords": []},
        "qa": {"answerable": 2, "total": 3, "ratio": 0.6667},
        "tier": "truncate",
        "verdict": "pass",
    }


def _report(scenario: dict, *, passing: float = 1.0, failing: float = 0.0) -> dict:
    return {
        "schema_version": 1,
        "run_seed": 0,
        "scenarios": [scenario],
        "tier_histogram": {scenario["tier"]: 1},
        "totals": {
            "scenarios": 1.0,
            "passing": passing,
            "failing": failing,
            "tokens_saved_approx": 73.0,
        },
    }


def _find(drifts, scope):
    return next((d for d in drifts if d.scope == scope), None)


def test_classify_catches_compression_ratio_regression():
    old = _report(_scenario_row())
    new_row = copy.deepcopy(_scenario_row())
    new_row["metrics"]["compression_ratio"] = 0.91
    new_row["metrics"]["compressed_chars"] = 1520
    drifts = classify_drift(old, _report(new_row))
    ratio = _find(drifts, "s03.metrics.compression_ratio")
    assert ratio is not None and ratio.label == "REGRESSION" and "up" in ratio.note
    bytes_drift = _find(drifts, "s03.metrics.compressed_chars")
    assert bytes_drift is not None and bytes_drift.label == "REGRESSION"


def test_classify_catches_qa_and_verdict_regression():
    old = _report(_scenario_row(), passing=1.0, failing=0.0)
    new_row = copy.deepcopy(_scenario_row())
    new_row["qa"] = {"answerable": 1, "total": 3, "ratio": 0.3333}
    new_row["verdict"] = "fail"
    drifts = classify_drift(old, _report(new_row, passing=0.0, failing=1.0))
    qa = _find(drifts, "s03.qa.ratio")
    assert qa is not None and qa.label == "REGRESSION"
    verdict = _find(drifts, "s03.verdict")
    assert verdict is not None and verdict.label == "REGRESSION" and verdict.note == "pass→fail"
    assert _find(drifts, "totals.passing").label == "REGRESSION"
    assert _find(drifts, "totals.failing").label == "REGRESSION"


def test_classify_compression_improvement_is_not_a_regression():
    old = _report(_scenario_row())
    new_row = copy.deepcopy(_scenario_row())
    new_row["metrics"]["compression_ratio"] = 0.70  # more aggressive = better
    drifts = classify_drift(old, _report(new_row))
    ratio = _find(drifts, "s03.metrics.compression_ratio")
    assert ratio is not None and ratio.label == "IMPROVEMENT"


def test_classify_strategy_change_is_neutral():
    old = _report(_scenario_row())
    new_row = copy.deepcopy(_scenario_row())
    new_row["metrics"]["compression_strategy"] = "extract_fields"
    new_row["tier"] = "extract_fields"
    drifts = classify_drift(old, _report(new_row))
    strat = _find(drifts, "s03.metrics.compression_strategy")
    assert strat is not None and strat.label == "NEUTRAL"


def test_classify_dropped_scenario_is_regression():
    old = _report(_scenario_row("s03"))
    new = {**_report(_scenario_row("s03")), "scenarios": []}
    drifts = classify_drift(old, new)
    dropped = _find(drifts, "s03")
    assert dropped is not None and dropped.label == "REGRESSION" and "dropped" in dropped.note


def test_classify_equal_reports_no_drift():
    old = _report(_scenario_row())
    assert classify_drift(old, copy.deepcopy(old)) == []


def test_classify_surfaces_top_level_scalar_drift():
    """A run_seed-only diff (or any top-level scalar) must surface — otherwise
    the gate would red while the summary said 'No drift'."""
    old = _report(_scenario_row())
    new = copy.deepcopy(old)
    new["run_seed"] = 1
    drifts = classify_drift(old, new)
    seed = _find(drifts, "run_seed")
    assert seed is not None and seed.label == "NEUTRAL"
    assert not format_drift_md(drifts).startswith("No drift")


def test_classify_surfaces_unknown_scenario_field():
    """A NEW top-level ScenarioReport field (not in the direction table) must
    be surfaced as NEUTRAL 'unclassified', never silently dropped — guards the
    classifier's invariant that it stays complete vs the gate's deep-equal."""
    old = _report(_scenario_row())
    new = copy.deepcopy(old)
    new["scenarios"][0]["cache_hit"] = True  # hypothetical future field
    drifts = classify_drift(old, new)
    unknown = _find(drifts, "s03.cache_hit")
    assert unknown is not None and unknown.label == "NEUTRAL"
    assert "unclassified" in unknown.note


def test_format_drift_md_groups_by_label():
    old = _report(_scenario_row())
    new_row = copy.deepcopy(_scenario_row())
    new_row["metrics"]["compression_ratio"] = 0.91
    md = format_drift_md(classify_drift(old, _report(new_row)))
    assert "REGRESSION" in md and "compression_ratio" in md
    assert format_drift_md([]).startswith("No drift")
