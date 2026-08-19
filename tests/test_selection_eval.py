"""Offline selection replay and evaluation contracts (#468)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import cli
from memtomem_stm.proxy.selection_eval import (
    SelectionEvaluationError,
    _recommend_variant,
    builtin_dataset_path,
    evaluate_selection,
    load_selection_dataset,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "selection_eval"


def _write_jsonl(path: Path, records: list[dict], *, partial: str = "") -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in records) + partial,
        encoding="utf-8",
    )


def _selection(selection_id: str = "sel-1") -> dict:
    return {
        "schema_version": 1,
        "ranker_version": "v1-bm25-tool-relevance",
        "event": "selection",
        "selection_id": selection_id,
        "candidate_tools": ["demo__search", "demo__write"],
        "candidate_count": 2,
        "selected_tool": "demo__search",
        "reject_reasons": {"demo__hidden": "config_hidden"},
        "candidate_features": {
            "query_source": "args",
            "ranked_candidates": [
                {
                    "tool": "demo__search",
                    "rank": 1,
                    "relevance_score": 0.75,
                    "risk_penalty": 0.0,
                    "final_score": 0.75,
                },
                {
                    "tool": "demo__write",
                    "rank": 2,
                    "relevance_score": 0.5,
                    "risk_penalty": 0.2,
                    "final_score": 0.4,
                },
            ],
        },
    }


def _execution(selection_id: str = "sel-1") -> dict:
    return {
        "schema_version": 1,
        "ranker_version": "v1-bm25-tool-relevance",
        "event": "execution",
        "selection_id": selection_id,
        "ok": True,
        "latency_ms": 12.5,
        "cache_hit": False,
        "retry_count": 0,
        "cost": None,
    }


def test_builtin_corpus_contract_and_split_isolation() -> None:
    dataset = load_selection_dataset()

    assert builtin_dataset_path().is_file()
    assert len(dataset["cases"]) == 30
    assert dataset["_split_counts"] == {"test": 6, "train": 18, "validation": 6}
    group_splits: dict[str, str] = {}
    for case in dataset["cases"]:
        assert group_splits.setdefault(case["group_id"], case["split"]) == case["split"]


def test_corpus_identity_is_independent_of_checkout_line_endings(tmp_path: Path) -> None:
    expected = load_selection_dataset()["_sha256"]
    crlf = tmp_path / "selection-eval-crlf.json"
    source = builtin_dataset_path().read_text(encoding="utf-8")
    crlf.write_bytes(source.replace("\n", "\r\n").encode("utf-8"))

    assert load_selection_dataset(crlf)["_sha256"] == expected


def test_canonical_report_matches_exact_golden_hash() -> None:
    golden = json.loads((_FIXTURES / "golden.json").read_text(encoding="utf-8"))
    first = evaluate_selection()
    second = evaluate_selection()
    payload = first.to_json().encode()

    assert first.to_json() == second.to_json()
    assert hashlib.sha256(payload).hexdigest() == golden["report_sha256"]
    assert first.data["inputs"]["dataset"]["id"] == golden["dataset_id"]
    assert first.data["inputs"]["dataset"]["split_counts"] == golden["dataset_split_counts"]
    assert len(first.data["variants"]) == golden["variant_count"]
    assert first.data["comparison"]["status"] == golden["status"]
    assert first.data["comparison"]["recommended_variant"] == golden["recommended_variant"]


def test_safety_first_gate_keeps_policy_exposure_at_zero() -> None:
    report = evaluate_selection().data

    assert report["comparison"]["status"] == "keep_baseline"
    for variant in report["variants"]:
        for metrics in variant["splits"].values():
            assert metrics["policy_exposure_violation_count"] == 0
            assert metrics["unsafe_candidate_reject_rate"]["value"] == 1.0


def test_rotated_telemetry_join_and_partial_tail(tmp_path: Path) -> None:
    active = tmp_path / "selection.jsonl"
    _write_jsonl(active.with_suffix(".jsonl.1"), [_selection()])
    _write_jsonl(
        active,
        [
            _execution(),
            {
                "schema_version": 1,
                "ranker_version": "v1-bm25-tool-relevance",
                "event": "feedback",
                "selection_id": "sel-1",
                "user_corrected": False,
                "operator_override": True,
            },
        ],
        partial="{incomplete",
    )

    report = evaluate_selection(telemetry_path=active).data

    assert report["status"] == "ok"
    assert [row["name"] for row in report["inputs"]["telemetry_files"]] == [
        "selection.jsonl.1",
        "selection.jsonl",
    ]
    assert report["data_quality"]["truncated_tail_lines"] == 1
    assert report["production"]["coverage"]["paired_selections"] == 1
    assert report["production"]["selected_tool_alignment"]["at_1"]["value"] == 1.0
    assert report["production"]["execution"]["success_rate"]["value"] == 1.0
    assert report["production"]["feedback"]["operator_override_rate"]["value"] == 1.0
    assert "not causal" in report["production"]["interpretation"]


def test_bad_schema_marks_report_invalid(tmp_path: Path) -> None:
    log = tmp_path / "selection.jsonl"
    _write_jsonl(log, [{"schema_version": 99, "event": "selection"}])

    report = evaluate_selection(telemetry_path=log).data

    assert report["status"] == "invalid"
    assert report["data_quality"]["unsupported_schema_records"] == 1


def _malformed_plus_good(malformed: dict) -> list[dict]:
    """The bad row plus a well-formed pair, to pin that the scan continues (#854)."""
    return [malformed, _execution(), _selection("sel-2"), _execution("sel-2")]


@pytest.mark.parametrize(
    ("bad_rank", "case"),
    [
        ("first", "non-numeric"),
        (None, "missing"),
        (True, "bool"),
        (1.0, "float"),
        (0, "zero — would divide by zero in MRR"),
        (-1, "negative — would score as a rank-1 hit"),
        (10**400, "oversized — would overflow float()"),
    ],
)
def test_admitted_record_with_unusable_rank_is_counted_not_raised(
    tmp_path: Path, bad_rank: object, case: str
) -> None:
    log = tmp_path / "selection.jsonl"
    selection = _selection()
    selection["candidate_features"]["ranked_candidates"][0]["rank"] = bad_rank
    _write_jsonl(log, _malformed_plus_good(selection))

    report = evaluate_selection(telemetry_path=log).data

    assert report["status"] == "invalid", case
    assert report["data_quality"]["invariant_violations"] == 1
    # The bad row still ranks, but contributes no selected rank; sel-2 does.
    assert report["production"]["coverage"]["rankable_selections"] == 2
    assert report["production"]["coverage"]["selected_rank_known"] == 1
    assert report["production"]["coverage"]["paired_selections"] == 2
    # An unusable rank is an alignment miss, not a dropped denominator, and it
    # never reaches the 1/rank mean.
    alignment = report["production"]["selected_tool_alignment"]
    assert alignment["at_1"] == {"value": 0.5, "numerator": 1, "denominator": 2}
    assert alignment["mrr"] == {"value": 1.0, "denominator": 1}


def test_admitted_record_with_unhashable_candidate_tools_is_counted_not_raised(
    tmp_path: Path,
) -> None:
    log = tmp_path / "selection.jsonl"
    selection = _selection()
    # Both real tools stay listed so the only violation is the non-str element.
    selection["candidate_tools"] = ["demo__search", "demo__write", {"nested": 1}]
    selection["candidate_count"] = 3
    _write_jsonl(log, _malformed_plus_good(selection))

    report = evaluate_selection(telemetry_path=log).data

    assert report["status"] == "invalid"
    assert report["data_quality"]["invariant_violations"] == 1
    assert report["production"]["coverage"]["rankable_selections"] == 2
    assert report["production"]["coverage"]["paired_selections"] == 2


def test_selected_tool_outside_candidate_tools_scores_as_an_alignment_miss(
    tmp_path: Path,
) -> None:
    """A rank for a tool the record never offered is not a rank we can trust."""
    log = tmp_path / "selection.jsonl"
    selection = _selection()
    selection["candidate_tools"] = ["demo__write", "demo__other"]
    _write_jsonl(log, _malformed_plus_good(selection))

    report = evaluate_selection(telemetry_path=log).data

    assert report["status"] == "invalid"
    assert report["production"]["coverage"]["rankable_selections"] == 2
    assert report["production"]["coverage"]["selected_rank_known"] == 1
    assert report["production"]["selected_tool_alignment"]["mrr"]["denominator"] == 1


def test_duplicate_selected_tool_entry_does_not_overwrite_the_first_rank(
    tmp_path: Path,
) -> None:
    """The duplicate is itself a violation, so its rank must not win."""
    log = tmp_path / "selection.jsonl"
    selection = _selection()
    selection["candidate_features"]["ranked_candidates"].append(
        {
            "tool": "demo__search",
            "rank": 3,
            "relevance_score": 0.1,
            "risk_penalty": 0.0,
            "final_score": 0.1,
        }
    )
    _write_jsonl(log, [selection, _execution()])

    report = evaluate_selection(telemetry_path=log).data

    assert report["data_quality"]["invariant_violations"] == 1
    assert report["production"]["selected_tool_alignment"]["mrr"]["value"] == 1.0


def test_malformed_sibling_entry_does_not_erase_a_valid_selected_rank(
    tmp_path: Path,
) -> None:
    """Only the selected tool's own entry decides its rank."""
    log = tmp_path / "selection.jsonl"
    selection = _selection()
    selection["candidate_features"]["ranked_candidates"][1]["rank"] = "two"
    _write_jsonl(log, [selection, _execution()])

    report = evaluate_selection(telemetry_path=log).data

    assert report["data_quality"]["invariant_violations"] == 1
    assert report["production"]["coverage"]["selected_rank_known"] == 1
    assert report["production"]["selected_tool_alignment"]["mrr"]["value"] == 1.0


def test_hashable_non_string_candidate_tool_is_counted(tmp_path: Path) -> None:
    """A non-string element is a violation whether or not set() chokes on it."""
    log = tmp_path / "selection.jsonl"
    selection = _selection()
    # Hashable, so this never crashed — it was silently accepted (status "ok").
    selection["candidate_tools"] = ["demo__search", "demo__write", 42]
    selection["candidate_count"] = 3
    _write_jsonl(log, [selection, _execution()])

    report = evaluate_selection(telemetry_path=log).data

    assert report["status"] == "invalid"
    assert report["data_quality"]["invariant_violations"] == 1


def test_non_string_tool_matching_selected_tool_yields_no_rank(tmp_path: Path) -> None:
    """Equality with `selected_tool` does not make a non-string tool rankable."""
    log = tmp_path / "selection.jsonl"
    selection = _selection()
    selection["selected_tool"] = 42
    selection["candidate_tools"] = ["demo__search", "demo__write", 42]
    selection["candidate_count"] = 3
    selection["candidate_features"]["ranked_candidates"][0]["tool"] = 42
    _write_jsonl(log, [selection, _execution()])

    report = evaluate_selection(telemetry_path=log).data

    assert report["status"] == "invalid"
    assert report["production"]["coverage"]["rankable_selections"] == 1
    assert report["production"]["coverage"]["selected_rank_known"] == 0
    assert report["production"]["selected_tool_alignment"]["mrr"]["denominator"] == 0


def test_unknown_future_ranker_skips_v1_score_parity(tmp_path: Path) -> None:
    log = tmp_path / "selection.jsonl"
    selection = _selection()
    selection["ranker_version"] = "v4-learning-to-rank"
    selection["candidate_features"]["ranked_candidates"][0]["final_score"] = 0.123
    execution = _execution()
    execution["ranker_version"] = "v4-learning-to-rank"
    _write_jsonl(log, [selection, execution])

    report = evaluate_selection(telemetry_path=log).data

    assert report["status"] == "ok"
    assert report["data_quality"]["parity_mismatches"] == 0


def test_dataset_rejects_split_leakage(tmp_path: Path) -> None:
    dataset = load_selection_dataset()
    dataset.pop("_sha256")
    dataset.pop("_split_counts")
    dataset["cases"][1]["group_id"] = dataset["cases"][0]["group_id"]
    dataset["cases"][1]["split"] = "validation"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")

    with pytest.raises(SelectionEvaluationError, match="leaks across splits"):
        load_selection_dataset(path)


def test_dataset_requires_a_rankable_case_in_every_split(tmp_path: Path) -> None:
    dataset = load_selection_dataset()
    dataset.pop("_sha256")
    dataset.pop("_split_counts")
    for case in dataset["cases"]:
        if case["split"] == "validation":
            case["abstain_expected"] = True
    path = tmp_path / "all-abstain-validation.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")

    with pytest.raises(
        SelectionEvaluationError, match="validation split needs at least one rankable case"
    ):
        load_selection_dataset(path)


def test_recommendation_promotes_strict_no_regression_improvement() -> None:
    def metrics(*, unsafe: float, task1: float, task5: float, ndcg: float, mrr: float) -> dict:
        return {
            "policy_exposure_violation_count": 0,
            "unsafe_top1_rate": {"value": unsafe},
            "task_success_proxy_at": {
                "1": {"value": task1},
                "5": {"value": task5},
            },
            "ndcg_at_5": {"value": ndcg},
            "mrr": {"value": mrr},
            "hit_at": {"3": {"value": task5}},
        }

    baseline_metrics = metrics(unsafe=0.2, task1=0.5, task5=0.8, ndcg=0.5, mrr=0.5)
    improved_metrics = metrics(unsafe=0.1, task1=0.6, task5=0.9, ndcg=0.6, mrr=0.6)
    variants = [
        {
            "id": "review-0.5__graph-1",
            "parameters": {"review_risk_penalty": 0.5, "risk_penalty_scale": 1.0},
            "splits": {
                "train": baseline_metrics,
                "validation": baseline_metrics,
                "test": baseline_metrics,
            },
        },
        {
            "id": "review-0.75__graph-1",
            "parameters": {"review_risk_penalty": 0.75, "risk_penalty_scale": 1.0},
            "splits": {
                "train": baseline_metrics,
                "validation": improved_metrics,
                "test": improved_metrics,
            },
        },
    ]

    recommendation = _recommend_variant(variants, 0.5, 1.0)

    assert recommendation["status"] == "promote"
    assert recommendation["selected_on_validation"] == "review-0.75__graph-1"
    assert recommendation["recommended_variant"] == "review-0.75__graph-1"
    assert recommendation["config_patch"]["exposure"]["review_risk_penalty"] == 0.75


def test_cli_json_and_output_files_are_private(tmp_path: Path) -> None:
    output = tmp_path / "report"
    result = CliRunner().invoke(
        cli,
        ["selection", "replay", "--no-telemetry", "--json", "--output-dir", str(output)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert (output / "report.json").read_text(encoding="utf-8") == result.output
    assert (output / "summary.md").read_text(encoding="utf-8").startswith("Selection Replay")
    if sys.platform != "win32":
        assert os.stat(output / "report.json").st_mode & 0o777 == 0o600
        assert os.stat(output / "summary.md").st_mode & 0o777 == 0o600


def test_cli_rejects_conflicting_input_modes(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["selection", "replay", "--no-telemetry", "--log", str(tmp_path / "x")],
    )

    assert result.exit_code == 2
    assert "cannot be combined" in result.output
