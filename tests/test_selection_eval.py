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
