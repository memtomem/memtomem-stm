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
        ("first", "non-numeric string"),
        (None, "null"),
        (True, "bool — used to pass as True == 1"),
        (1.0, "integral float — used to be cast to 1"),
        ("1", "numeric string — used to be cast to 1"),
        (1.5, "fractional float — used to be truncated to 1"),
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
    # The first, valid rank stands, so the numbers improve rather than drop:
    # main recorded the duplicate's rank 3 (MRR 0.33, at_1 a miss).
    alignment = report["production"]["selected_tool_alignment"]
    assert alignment["mrr"] == {"value": 1.0, "denominator": 1}
    assert alignment["at_1"]["numerator"] == 1
    assert report["production"]["coverage"]["selected_rank_known"] == 1


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


def test_absent_rank_key_is_counted_not_raised(tmp_path: Path) -> None:
    """An absent key is a distinct input class from a null value: it hit KeyError."""
    log = tmp_path / "selection.jsonl"
    selection = _selection()
    del selection["candidate_features"]["ranked_candidates"][0]["rank"]
    _write_jsonl(log, _malformed_plus_good(selection))

    report = evaluate_selection(telemetry_path=log).data

    assert report["status"] == "invalid"
    assert report["data_quality"]["invariant_violations"] == 1
    assert report["production"]["coverage"]["selected_rank_known"] == 1


def test_unusable_rank_on_a_non_selected_entry_is_still_counted(tmp_path: Path) -> None:
    """The violation does not depend on the entry being the selected one."""
    log = tmp_path / "selection.jsonl"
    selection = _selection()
    selection["candidate_features"]["ranked_candidates"][1]["rank"] = 2.0
    _write_jsonl(log, [selection, _execution()])

    report = evaluate_selection(telemetry_path=log).data

    assert report["status"] == "invalid"
    assert report["data_quality"]["invariant_violations"] == 1
    # The selected entry is untouched, so its rank still reaches the metrics.
    assert report["production"]["coverage"]["selected_rank_known"] == 1
    assert report["production"]["selected_tool_alignment"]["mrr"]["value"] == 1.0


def test_unhashable_candidate_with_a_count_mismatch_is_counted_once(tmp_path: Path) -> None:
    """A count mismatch short-circuits before `set()`, so this never crashed."""
    log = tmp_path / "selection.jsonl"
    selection = _selection()
    # candidate_count stays 2 while the list grows to 3.
    selection["candidate_tools"] = ["demo__search", "demo__write", {"x": 1}]
    _write_jsonl(log, [selection, _execution()])

    report = evaluate_selection(telemetry_path=log).data

    assert report["status"] == "invalid"
    assert report["data_quality"]["invariant_violations"] == 1
    assert report["production"]["coverage"]["selected_rank_known"] == 1


def test_container_shape_violations_are_unchanged_by_the_entry_gate(tmp_path: Path) -> None:
    """`entry_ok` governs entries; the containers around them keep their own paths."""
    not_a_dict = _selection()
    not_a_dict["candidate_features"]["ranked_candidates"][0] = "not-a-dict"
    log = tmp_path / "entry.jsonl"
    _write_jsonl(log, [not_a_dict, _execution()])
    entry_report = evaluate_selection(telemetry_path=log).data

    not_a_list = _selection()
    not_a_list["candidate_features"]["ranked_candidates"] = "not-a-list"
    other = tmp_path / "container.jsonl"
    _write_jsonl(other, [not_a_list, _execution()])
    container_report = evaluate_selection(telemetry_path=other).data

    assert entry_report["data_quality"]["invariant_violations"] == 1
    assert entry_report["production"]["coverage"]["rankable_selections"] == 1
    # A non-list container never reaches the entry loop at all: no violation.
    assert container_report["status"] == "ok"
    assert container_report["data_quality"]["invariant_violations"] == 0
    assert container_report["production"]["coverage"]["rankable_selections"] == 0


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
    # Two gates fire: the non-string candidate element and the non-string tool.
    assert report["data_quality"]["invariant_violations"] == 2
    assert report["production"]["coverage"]["rankable_selections"] == 1
    assert report["production"]["coverage"]["selected_rank_known"] == 0
    assert report["production"]["selected_tool_alignment"]["mrr"]["denominator"] == 0


_SCORE_FIELDS = ("relevance_score", "risk_penalty", "final_score")
_EXECUTION_FIELDS = ("latency_ms", "retry_count", "cost")
# A JSON integer literal is unbounded, so an admitted record can hold one no
# float can represent; `json.loads` also accepts the `NaN` literal (#856).
_UNUSABLE = (
    (10**400, "oversized int"),
    (float("nan"), "NaN"),
    (float("inf"), "inf"),
    ("fast", "string"),
    (True, "bool"),
    ([1], "array"),
    ({"a": 1}, "object"),
)


@pytest.mark.parametrize("field", _SCORE_FIELDS)
@pytest.mark.parametrize(("value", "case"), _UNUSABLE)
def test_unusable_score_field_skips_parity_without_raising(
    tmp_path: Path, field: str, value: object, case: str
) -> None:
    log = tmp_path / "selection.jsonl"
    selection = _selection()
    selection["candidate_features"]["ranked_candidates"][0][field] = value
    _write_jsonl(log, [selection, _execution()])

    report = evaluate_selection(telemetry_path=log)

    assert json.loads(report.to_json())["status"] == "invalid", case
    quality = report.data["data_quality"]
    assert quality["unusable_numbers"] == 1
    # Parity was not checked, and its denominator says so — `parity_mismatches:
    # 0` must not read as a clean bill of health.
    assert quality["parity_mismatches"] == 0
    assert quality["parity_checked"] == 1
    # The entry is otherwise well-formed, so it still ranks.
    assert report.data["production"]["coverage"]["rankable_selections"] == 1
    assert report.data["production"]["coverage"]["selected_rank_known"] == 1


@pytest.mark.parametrize("field", _EXECUTION_FIELDS)
@pytest.mark.parametrize(("value", "case"), _UNUSABLE)
def test_unusable_execution_field_is_dropped_from_its_sample(
    tmp_path: Path, field: str, value: object, case: str
) -> None:
    log = tmp_path / "selection.jsonl"
    execution = _execution()
    # `cost` defaults to None, which would make its count 0 either way.
    execution["cost"] = 0.02
    execution[field] = value
    _write_jsonl(log, [_selection(), execution])

    report = evaluate_selection(telemetry_path=log)

    # A non-finite value used to build a report that then failed to serialize.
    assert json.loads(report.to_json())["status"] == "invalid", case
    assert report.data["data_quality"]["unusable_numbers"] == 1
    production = report.data["production"]
    assert production["execution"]["success_rate"]["numerator"] == 1
    counts = {
        "latency_ms": production["execution"]["latency_ms"]["count"],
        "retry_count": production["coverage"]["retry_count_executions"],
        "cost": production["coverage"]["cost_executions"],
    }
    # Dropped from its own sample only; the other two are unaffected.
    assert counts[field] == 0
    assert [name for name, count in counts.items() if count == 0] == [field]


@pytest.mark.parametrize("field", _SCORE_FIELDS + _EXECUTION_FIELDS)
def test_absent_or_null_numeric_field_is_not_unusable(tmp_path: Path, field: str) -> None:
    """Nothing to read is not the same as something unreadable (#856).

    `cost` is nullable in the writer's own shape, so counting a null would flag
    every ordinary log.
    """
    for value in (None, "absent"):
        log = tmp_path / f"{field}-{value}.jsonl"
        selection, execution = _selection(), _execution()
        holder = (
            selection["candidate_features"]["ranked_candidates"][0]
            if field in _SCORE_FIELDS
            else execution
        )
        if value is None:
            holder[field] = None
        else:
            holder.pop(field, None)
        _write_jsonl(log, [selection, execution])

        report = evaluate_selection(telemetry_path=log).data

        assert report["data_quality"]["unusable_numbers"] == 0, (field, value)
        assert report["status"] == "ok"


def test_finite_values_whose_sum_overflows_still_report_their_mean(tmp_path: Path) -> None:
    """Guarding the inputs does not bound the arithmetic (#856)."""
    log = tmp_path / "selection.jsonl"
    records: list[dict] = []
    for sid in ("sel-1", "sel-2"):
        execution = _execution(sid)
        execution["cost"] = 1e308
        execution["latency_ms"] = 1e308
        records += [_selection(sid), execution]
    _write_jsonl(log, records)

    report = evaluate_selection(telemetry_path=log)

    # Each value is finite and admitted; their sum is not.
    payload = json.loads(report.to_json())
    assert payload["status"] == "ok"
    assert report.data["data_quality"]["unusable_numbers"] == 0
    execution_metrics = report.data["production"]["execution"]
    # The mean of two 1e308 values IS 1e308 — the summing form overflowed, the
    # answer was always representable, and refusing it would lose an answer.
    assert execution_metrics["cost_mean"] == {"value": 1e308, "denominator": 2}
    assert execution_metrics["latency_ms"]["count"] == 2
    assert execution_metrics["latency_ms"]["p50"] == 1e308


def test_unusable_numbers_survives_a_ranker_mismatch(tmp_path: Path) -> None:
    """A cohort verdict abandons the record; the data-quality count must not."""
    log = tmp_path / "selection.jsonl"
    execution = _execution()
    execution["ranker_version"] = "v9-some-future-ranker"
    execution["latency_ms"] = float("nan")
    execution["retry_count"] = "many"
    execution["cost"] = 10**400
    _write_jsonl(log, [_selection(), execution])

    report = evaluate_selection(telemetry_path=log).data

    assert report["data_quality"]["ranker_mismatches"] == 1
    assert report["data_quality"]["unusable_numbers"] == 3


def test_unusable_numbers_survives_a_structural_violation(tmp_path: Path) -> None:
    """Same for the `candidate_tools` gate, which abandons the record earlier."""
    log = tmp_path / "selection.jsonl"
    selection = _selection()
    selection["candidate_tools"] = "not-a-list"
    selection["candidate_features"]["ranked_candidates"][0]["final_score"] = float("nan")
    execution = _execution()
    execution["latency_ms"] = "fast"
    _write_jsonl(log, [selection, execution])

    report = evaluate_selection(telemetry_path=log).data

    assert report["data_quality"]["invariant_violations"] == 1
    assert report["data_quality"]["unusable_numbers"] == 2


def test_unusable_numbers_counts_records_the_fold_discards(tmp_path: Path) -> None:
    """The count is a property of the file, not of what survives the join."""
    log = tmp_path / "selection.jsonl"
    no_id = _execution()
    no_id.pop("selection_id")
    no_id["latency_ms"] = float("nan")
    # A conflicting pair: neither copy survives, but both hold unreadable values.
    left, right = _execution("sel-9"), _execution("sel-9")
    left["latency_ms"] = float("nan")
    right["latency_ms"] = "fast"
    right["ok"] = False
    _write_jsonl(log, [no_id, left, right])

    report = evaluate_selection(telemetry_path=log).data

    assert report["data_quality"]["missing_selection_id"] == 1
    assert report["data_quality"]["conflicting_records"] == 1
    assert report["data_quality"]["unusable_numbers"] == 3


def test_unusable_numbers_counts_a_duplicated_line_twice(tmp_path: Path) -> None:
    """Physically, not logically — the file holds the value on both lines."""
    log = tmp_path / "selection.jsonl"
    execution = _execution()
    execution["cost"] = float("inf")
    _write_jsonl(log, [_selection(), execution, execution])

    report = evaluate_selection(telemetry_path=log).data

    assert report["data_quality"]["duplicate_records"] == 1
    assert report["data_quality"]["unusable_numbers"] == 2


def test_records_the_reader_rejects_are_not_unusable_numbers(tmp_path: Path) -> None:
    """The count is physical over ADMITTED records; rejection has its own counters."""
    log = tmp_path / "selection.jsonl"
    unsupported = _execution("sel-9")
    unsupported["schema_version"] = 99
    unsupported["latency_ms"] = float("nan")
    unknown_event = {"schema_version": 1, "event": "telemetry", "latency_ms": float("nan")}
    _write_jsonl(
        log,
        [_selection(), _execution(), unsupported, unknown_event],
        partial='{"event": "execution", "latency_ms": ',
    )

    report = evaluate_selection(telemetry_path=log).data

    quality = report["data_quality"]
    assert quality["unsupported_schema_records"] == 1
    # None of the three unadmitted lines contributes, though each holds a NaN.
    assert quality["unusable_numbers"] == 0


def test_active_only_excludes_rotated_unusable_numbers(tmp_path: Path) -> None:
    """The count follows the same segments as the rest of the report."""
    log = tmp_path / "selection.jsonl"
    rotated = tmp_path / "selection.jsonl.1"
    _write_jsonl(log, [_selection(), _execution()])
    rotated_execution = _execution("sel-9")
    rotated_execution["latency_ms"] = float("nan")
    _write_jsonl(rotated, [_selection("sel-9"), rotated_execution])

    with_rotated = evaluate_selection(telemetry_path=log).data
    active_only = evaluate_selection(telemetry_path=log, include_rotated=False).data

    assert with_rotated["data_quality"]["unusable_numbers"] == 1
    assert active_only["data_quality"]["unusable_numbers"] == 0


def test_feedback_records_contribute_no_unusable_numbers(tmp_path: Path) -> None:
    """They carry none of the six fields; a stray numeric key is not one."""
    log = tmp_path / "selection.jsonl"
    feedback = {
        "schema_version": 1,
        "event": "feedback",
        "selection_id": "sel-1",
        "user_corrected": True,
        "latency_ms": float("nan"),
    }
    _write_jsonl(log, [_selection(), _execution(), feedback])

    report = evaluate_selection(telemetry_path=log).data

    assert report["production"]["coverage"]["feedback_selections"] == 1
    assert report["data_quality"]["unusable_numbers"] == 0


def test_mean_of_three_maximum_floats_is_that_maximum(tmp_path: Path) -> None:
    """Both the summing and the dividing form overflow here; the mean does not."""
    log = tmp_path / "selection.jsonl"
    records: list[dict] = []
    for sid in ("sel-1", "sel-2", "sel-3"):
        execution = _execution(sid)
        execution["cost"] = sys.float_info.max
        records += [_selection(sid), execution]
    _write_jsonl(log, records)

    report = evaluate_selection(telemetry_path=log).data

    assert report["production"]["execution"]["cost_mean"] == {
        "value": sys.float_info.max,
        "denominator": 3,
    }


def test_ordinary_means_are_unchanged_to_six_decimals(tmp_path: Path) -> None:
    """The overflow path must not move the digits every ordinary report shows."""
    log = tmp_path / "selection.jsonl"
    costs = [1.0 / 3.0, 2.0 / 7.0, 1e-7, 123456.789]
    records: list[dict] = []
    for index, cost in enumerate(costs):
        execution = _execution(f"sel-{index}")
        execution["cost"] = cost
        records += [_selection(f"sel-{index}"), execution]
    _write_jsonl(log, records)

    report = evaluate_selection(telemetry_path=log).data

    assert report["production"]["execution"]["cost_mean"]["value"] == round(
        sum(costs) / len(costs), 6
    )


def test_opposite_sign_extremes_interpolate_to_a_finite_percentile(tmp_path: Path) -> None:
    """A percentile lies between its samples, so it is always representable."""
    log = tmp_path / "selection.jsonl"
    records: list[dict] = []
    for sid, latency in (("sel-1", -1e308), ("sel-2", 1e308)):
        execution = _execution(sid)
        execution["latency_ms"] = latency
        records += [_selection(sid), execution]
    _write_jsonl(log, records)

    report = evaluate_selection(telemetry_path=log)

    assert json.loads(report.to_json())["status"] == "ok"
    latency = report.data["production"]["execution"]["latency_ms"]
    assert latency["count"] == 2
    # The difference form would give inf here; the weighted form gives 0.
    assert latency["p50"] == 0.0


@pytest.mark.parametrize(
    ("value", "case"),
    [(float("inf"), "inf"), (float("nan"), "NaN"), (10**400, "oversized int")],
)
def test_non_finite_baseline_is_a_clean_error(value: object, case: str) -> None:
    """`ge=0` admits `inf`, and `float()` raises on an oversized int (#856)."""
    with pytest.raises(SelectionEvaluationError, match="invalid baseline"):
        evaluate_selection(baseline_graph_scale=value)  # type: ignore[arg-type]

    with pytest.raises(SelectionEvaluationError, match="invalid baseline"):
        evaluate_selection(baseline_review_penalty=value)  # type: ignore[arg-type]


def test_oversized_graph_risk_score_in_a_dataset_is_a_clean_error(tmp_path: Path) -> None:
    """The dataset path reports its own errors; it must not raise OverflowError."""
    dataset = json.loads(builtin_dataset_path().read_text(encoding="utf-8"))
    dataset["cases"][0]["candidates"][0].setdefault("signals", {})["graph_risk_score"] = 10**400
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")

    with pytest.raises(SelectionEvaluationError, match="graph_risk_score must be in"):
        load_selection_dataset(path)


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
