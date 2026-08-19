"""Deterministic offline evaluation for tool selection telemetry (#468).

Production telemetry is intentionally observational: it contains query hashes,
not raw queries or candidate metadata, so it cannot honestly replay a lexical
ranker.  Counterfactual ranking and weight tuning therefore run against a
shipped, sanitized, labelled corpus while production JSONL contributes schema,
join, cohort, and execution-quality diagnostics only.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from memtomem_stm.proxy.config import (
    ExposureConfig,
    ExposureProfile,
    ToolOverrideConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyToolInfo
from memtomem_stm.proxy.privacy import contains_sensitive_content
from memtomem_stm.proxy.selection_log import (
    MAX_LINE_BYTES,
    SCHEMA_VERSION,
    discover_log_files,
    records_conflict,
)
from memtomem_stm.proxy.tool_eligibility import ExposureCandidate, filter_tools
from memtomem_stm.utils import json_out
from memtomem_stm.proxy.tool_relevance import (
    RANKER_VERSION_BM25,
    RANKER_VERSION_BM25_GRAPH_RISK,
    RANKER_VERSION_BM25_RISK,
    ToolRelevanceRanker,
    compose_risk_penalty,
    penalty_source,
)

REPORT_SCHEMA_VERSION = 1
DATASET_SCHEMA_VERSION = 1
EVALUATOR_VERSION = "v1"
GRID_REVIEW = (0.0, 0.25, 0.5, 0.75, 1.0)
GRID_GRAPH = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0)
PARITY_RANKER_VERSIONS = frozenset(
    (RANKER_VERSION_BM25, RANKER_VERSION_BM25_RISK, RANKER_VERSION_BM25_GRAPH_RISK)
)


class SelectionEvaluationError(ValueError):
    """Invalid or unreadable evaluation input."""


@dataclass(frozen=True)
class SelectionEvaluationReport:
    """Stable report wrapper returned by :func:`evaluate_selection`."""

    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self.to_json())

    def to_json(self, *, indent: int = 2) -> str:
        # Surrogate-safe (#757): this is what `mms selection replay --json`
        # echoes and what `--output-dir` writes, and the report embeds
        # telemetry strings such as ``query_source`` that arrive through
        # ``json.loads``, where ``"\udcff"`` is a legal escape.
        return (
            json_out.dumps(
                self.data, indent=indent, sort_keys=True, ensure_ascii=False, allow_nan=False
            )
            + "\n"
        )


def builtin_dataset_path() -> Path:
    """Return the packaged v1 corpus path."""
    return Path(str(files("memtomem_stm.data").joinpath("selection_eval_v1.json")))


def _ratio(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "value": round(numerator / denominator, 6) if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    rank = (len(ordered) - 1) * percentile / 100.0
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return round(ordered[low], 6)
    value = ordered[low] + (ordered[high] - ordered[low]) * (rank - low)
    return round(value, 6)


def _dataset_bytes(path: Path) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SelectionEvaluationError(f"cannot read dataset: {path.name}: {exc}") from exc
    if contains_sensitive_content(raw.decode("utf-8", errors="replace")):
        raise SelectionEvaluationError("dataset matched sensitive-content patterns")
    return raw


def load_selection_dataset(path: Path | str | None = None) -> dict[str, Any]:
    """Load and strictly validate a sanitized selection-evaluation corpus."""
    resolved = Path(path).expanduser() if path is not None else builtin_dataset_path()
    raw_bytes = _dataset_bytes(resolved)
    try:
        data = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise SelectionEvaluationError(f"invalid dataset JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != DATASET_SCHEMA_VERSION:
        raise SelectionEvaluationError(f"dataset schema_version must be {DATASET_SCHEMA_VERSION}")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise SelectionEvaluationError("dataset cases must be a non-empty list")
    seen_cases: set[str] = set()
    group_splits: dict[str, str] = {}
    split_counts: Counter[str] = Counter()
    rankable_counts: Counter[str] = Counter()
    for case in cases:
        if not isinstance(case, dict):
            raise SelectionEvaluationError("each dataset case must be an object")
        case_id = case.get("case_id")
        group_id = case.get("group_id")
        split = case.get("split")
        if not isinstance(case_id, str) or not case_id or case_id in seen_cases:
            raise SelectionEvaluationError(f"duplicate or invalid case_id: {case_id!r}")
        if not isinstance(group_id, str) or not group_id:
            raise SelectionEvaluationError(f"{case_id}: invalid group_id")
        if split not in ("train", "validation", "test"):
            raise SelectionEvaluationError(f"{case_id}: invalid split {split!r}")
        previous = group_splits.setdefault(group_id, split)
        if previous != split:
            raise SelectionEvaluationError(f"group {group_id!r} leaks across splits")
        query = case.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 512:
            raise SelectionEvaluationError(f"{case_id}: query must be 1..512 chars")
        candidates = case.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise SelectionEvaluationError(f"{case_id}: candidates must be non-empty")
        seen_candidate_ids: set[str] = set()
        success_candidates = 0
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise SelectionEvaluationError(f"{case_id}: candidate must be an object")
            candidate_id = candidate.get("candidate_id")
            if (
                not isinstance(candidate_id, str)
                or not candidate_id
                or candidate_id in seen_candidate_ids
            ):
                raise SelectionEvaluationError(
                    f"{case_id}: duplicate or invalid candidate_id {candidate_id!r}"
                )
            labels = candidate.get("labels")
            signals = candidate.get("signals", {})
            if not isinstance(labels, dict) or not isinstance(signals, dict):
                raise SelectionEvaluationError(f"{case_id}/{candidate_id}: invalid labels/signals")
            grade = labels.get("relevance_grade")
            safety = labels.get("safety")
            exposure = labels.get("expected_exposure")
            success = labels.get("task_success")
            if not isinstance(grade, int) or isinstance(grade, bool) or not 0 <= grade <= 3:
                raise SelectionEvaluationError(f"{case_id}/{candidate_id}: grade must be 0..3")
            if safety not in ("safe", "risky", "forbidden"):
                raise SelectionEvaluationError(f"{case_id}/{candidate_id}: invalid safety")
            if exposure not in ("allow", "reject") or not isinstance(success, bool):
                raise SelectionEvaluationError(f"{case_id}/{candidate_id}: invalid gold labels")
            if safety == "forbidden" and exposure != "reject":
                raise SelectionEvaluationError(
                    f"{case_id}/{candidate_id}: forbidden candidates must be rejected"
                )
            if success and (grade <= 0 or safety != "safe" or exposure != "allow"):
                raise SelectionEvaluationError(
                    f"{case_id}/{candidate_id}: task_success requires safe relevant allow"
                )
            graph_score = signals.get("graph_risk_score", 0.0)
            if (
                not isinstance(graph_score, (int, float))
                or isinstance(graph_score, bool)
                or not 0.0 <= float(graph_score) <= 1.0
            ):
                raise SelectionEvaluationError(
                    f"{case_id}/{candidate_id}: graph_risk_score must be in [0,1]"
                )
            success_candidates += int(success)
            seen_candidate_ids.add(candidate_id)
        if not case.get("abstain_expected", False) and success_candidates == 0:
            raise SelectionEvaluationError(f"{case_id}: non-abstain case needs task-success gold")
        if not case.get("abstain_expected", False):
            rankable_counts[split] += 1
        seen_cases.add(case_id)
        split_counts[split] += 1
    for split in ("train", "validation", "test"):
        if rankable_counts[split] == 0:
            raise SelectionEvaluationError(f"{split} split needs at least one rankable case")
    # Encoded on the next expression, so this is one of the sites that
    # raises rather than one that only measures (#757): a fixture case may
    # carry a legal ``"\ud800"`` escape, and `mms selection replay` does not
    # catch `UnicodeEncodeError`.
    canonical = json_out.dumps(
        data,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    data["_sha256"] = hashlib.sha256(canonical).hexdigest()
    data["_split_counts"] = dict(sorted(split_counts.items()))
    return data


def _candidate_from_fixture(raw: dict[str, Any]) -> tuple[ExposureCandidate, dict[str, Any]]:
    server = str(raw.get("server", "fixture"))
    prefix = str(raw.get("prefix", server))
    name = str(raw["name"])
    signals = raw.get("signals", {})
    override = ToolOverrideConfig(
        hidden=bool(signals.get("hidden", False)),
        expose_in_profiles=signals.get("tool_expose_in_profiles"),
    )
    server_cfg = UpstreamServerConfig(
        prefix=prefix,
        expose_in_profiles=signals.get("server_expose_in_profiles"),
        tool_overrides={name: override},
    )
    description = str(raw.get("description", ""))
    input_schema = raw.get("input_schema") or {"type": "object"}
    raw_description = str(raw.get("raw_description", description))
    if signals.get("synthetic_sensitive_metadata"):
        # Never store a credential-shaped value in the shipped fixture. Build
        # a deterministic synthetic match in memory solely to exercise the gate.
        raw_description += " password=" + "synthetic_" + "test_secret_12345"
    info = ProxyToolInfo(
        prefixed_name=f"{prefix}__{name}",
        description=description,
        input_schema=input_schema,
        server=server,
        original_name=name,
    )
    return (
        ExposureCandidate(
            info=info,
            raw_description=raw_description,
            raw_schema=raw.get("raw_schema", input_schema),
            server_config=server_cfg,
        ),
        raw,
    )


def _rank_case(case: dict[str, Any], review_penalty: float, graph_scale: float) -> dict[str, Any]:
    pairs = [_candidate_from_fixture(raw) for raw in case["candidates"]]
    candidates = [pair[0] for pair in pairs]
    unhealthy = frozenset(
        (candidate.info.server, candidate.info.original_name)
        for candidate, raw in pairs
        if raw.get("signals", {}).get("unhealthy")
    )
    external_rejects = {
        (candidate.info.server, candidate.info.original_name): str(reason)
        for candidate, raw in pairs
        if (reason := raw.get("signals", {}).get("external_reject")) is not None
    }
    exposure = ExposureConfig(
        profile=ExposureProfile(case.get("profile", "strict")),
        review_risk_penalty=review_penalty,
    )
    verdict = filter_tools(
        candidates,
        exposure,
        unhealthy,
        external_rejects=external_rejects or None,
        withhold_all=case.get("withhold_all"),
    )
    by_key = {
        (candidate.info.server, candidate.info.original_name): raw for candidate, raw in pairs
    }
    penalties: dict[str, float] = {}
    sources: dict[str, str] = {}
    for info in verdict.eligible:
        raw = by_key[(info.server, info.original_name)]
        native = verdict.risk_penalties.get(info.prefixed_name, 0.0)
        graph = min(float(raw.get("signals", {}).get("graph_risk_score", 0.0)) * graph_scale, 1.0)
        combined = compose_risk_penalty(native, graph)
        if combined > 0.0:
            penalties[info.prefixed_name] = combined
            sources[info.prefixed_name] = penalty_source(native, graph)
    ranked = ToolRelevanceRanker(top_n=20).rank(case["query"], verdict.eligible, penalties, sources)
    label_by_tool: dict[str, dict[str, Any]] = {}
    for candidate, raw in pairs:
        label_by_tool[candidate.info.prefixed_name] = raw["labels"]
    return {
        "ranked": ranked,
        "labels": label_by_tool,
        "eligible": [info.prefixed_name for info in verdict.eligible],
        "reject_reasons": verdict.reject_reasons,
        "abstain_expected": bool(case.get("abstain_expected", False)),
    }


def _dcg(grades: list[int], k: int) -> float:
    return sum((2**grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(grades[:k]))


def _empty_accumulator() -> dict[str, Any]:
    return {
        "cases": 0,
        "rank_cases": 0,
        "hit": Counter(),
        "task": Counter(),
        "mrr": [],
        "ndcg5": [],
        "forbidden": 0,
        "forbidden_rejected": 0,
        "expected_allow": 0,
        "expected_allow_rejected": 0,
        "policy_exposure": 0,
        "unsafe_top1": 0,
        "unsafe_top5": 0,
        "abstain": 0,
        "abstain_false_positive": 0,
    }


def _accumulate_case(acc: dict[str, Any], result: dict[str, Any]) -> None:
    acc["cases"] += 1
    ranked_tools = [entry["tool"] for entry in result["ranked"]]
    labels = result["labels"]
    rejected = set(result["reject_reasons"])
    eligible = set(result["eligible"])
    for tool, label in labels.items():
        if label["safety"] == "forbidden":
            acc["forbidden"] += 1
            acc["forbidden_rejected"] += int(tool in rejected)
            acc["policy_exposure"] += int(tool in eligible)
        if label["expected_exposure"] == "allow":
            acc["expected_allow"] += 1
            acc["expected_allow_rejected"] += int(tool in rejected)
    if result["abstain_expected"]:
        acc["abstain"] += 1
        acc["abstain_false_positive"] += int(bool(ranked_tools))
        return
    acc["rank_cases"] += 1
    relevant_positions = [
        i
        for i, tool in enumerate(ranked_tools, start=1)
        if labels[tool]["relevance_grade"] > 0 and labels[tool]["safety"] == "safe"
    ]
    success_positions = [
        i for i, tool in enumerate(ranked_tools, start=1) if labels[tool]["task_success"]
    ]
    for k in (1, 3, 5, 20):
        acc["hit"][k] += int(any(pos <= k for pos in relevant_positions))
        acc["task"][k] += int(any(pos <= k for pos in success_positions))
    acc["mrr"].append(1.0 / relevant_positions[0] if relevant_positions else 0.0)
    actual_grades = [
        int(labels[tool]["relevance_grade"]) if labels[tool]["safety"] == "safe" else 0
        for tool in ranked_tools
    ]
    ideal_grades = sorted(
        [
            int(label["relevance_grade"])
            for label in labels.values()
            if label["safety"] == "safe" and label["expected_exposure"] == "allow"
        ],
        reverse=True,
    )
    ideal = _dcg(ideal_grades, 5)
    acc["ndcg5"].append(_dcg(actual_grades, 5) / ideal if ideal else 0.0)
    if ranked_tools:
        acc["unsafe_top1"] += int(labels[ranked_tools[0]]["safety"] != "safe")
        acc["unsafe_top5"] += int(
            any(labels[tool]["safety"] != "safe" for tool in ranked_tools[:5])
        )


def _finalize_metrics(acc: dict[str, Any]) -> dict[str, Any]:
    rank_cases = int(acc["rank_cases"])
    return {
        "cases": acc["cases"],
        "rank_cases": rank_cases,
        "hit_at": {str(k): _ratio(acc["hit"][k], rank_cases) for k in (1, 3, 5, 20)},
        "task_success_proxy_at": {
            str(k): _ratio(acc["task"][k], rank_cases) for k in (1, 3, 5, 20)
        },
        "mrr": {"value": _mean(acc["mrr"]), "denominator": len(acc["mrr"])},
        "ndcg_at_5": {"value": _mean(acc["ndcg5"]), "denominator": len(acc["ndcg5"])},
        "unsafe_candidate_reject_rate": _ratio(acc["forbidden_rejected"], acc["forbidden"]),
        "unnecessary_rejection_rate": _ratio(acc["expected_allow_rejected"], acc["expected_allow"]),
        "policy_exposure_violation_count": acc["policy_exposure"],
        "unsafe_top1_rate": _ratio(acc["unsafe_top1"], rank_cases),
        "unsafe_top5_rate": _ratio(acc["unsafe_top5"], rank_cases),
        "abstain_false_positive_rate": _ratio(acc["abstain_false_positive"], acc["abstain"]),
    }


def _evaluate_variant(
    dataset: dict[str, Any], review_penalty: float, graph_scale: float
) -> dict[str, Any]:
    accumulators = {split: _empty_accumulator() for split in ("train", "validation", "test")}
    for case in sorted(dataset["cases"], key=lambda row: row["case_id"]):
        _accumulate_case(accumulators[case["split"]], _rank_case(case, review_penalty, graph_scale))
    variant_id = f"review-{review_penalty:g}__graph-{graph_scale:g}"
    return {
        "id": variant_id,
        "parameters": {
            "review_risk_penalty": review_penalty,
            "risk_penalty_scale": graph_scale,
        },
        "splits": {split: _finalize_metrics(acc) for split, acc in accumulators.items()},
    }


def _metric_value(metrics: dict[str, Any], path: tuple[str, ...]) -> float:
    value: Any = metrics
    for key in path:
        value = value[key]
    if isinstance(value, dict):
        value = value.get("value")
    return float(value or 0.0)


def _recommend_variant(
    variants: list[dict[str, Any]], baseline_review: float, baseline_graph: float
) -> dict[str, Any]:
    baseline = next(
        variant
        for variant in variants
        if variant["parameters"]
        == {
            "review_risk_penalty": baseline_review,
            "risk_penalty_scale": baseline_graph,
        }
    )
    base_train = baseline["splits"]["train"]
    eligible: list[dict[str, Any]] = []
    for variant in variants:
        train = variant["splits"]["train"]
        if train["policy_exposure_violation_count"] > base_train["policy_exposure_violation_count"]:
            continue
        if _metric_value(train, ("unsafe_top1_rate",)) > _metric_value(
            base_train, ("unsafe_top1_rate",)
        ):
            continue
        if _metric_value(train, ("task_success_proxy_at", "5")) < _metric_value(
            base_train, ("task_success_proxy_at", "5")
        ):
            continue
        eligible.append(variant)

    def key(variant: dict[str, Any]) -> tuple[Any, ...]:
        metrics = variant["splits"]["validation"]
        params = variant["parameters"]
        distance = abs(params["review_risk_penalty"] - baseline_review) + abs(
            params["risk_penalty_scale"] - baseline_graph
        )
        return (
            metrics["policy_exposure_violation_count"],
            _metric_value(metrics, ("unsafe_top1_rate",)),
            -_metric_value(metrics, ("task_success_proxy_at", "1")),
            -_metric_value(metrics, ("ndcg_at_5",)),
            -_metric_value(metrics, ("mrr",)),
            -_metric_value(metrics, ("hit_at", "3")),
            distance,
            params["review_risk_penalty"],
            params["risk_penalty_scale"],
        )

    chosen = min(eligible or [baseline], key=key)
    base_test = baseline["splits"]["test"]
    chosen_test = chosen["splits"]["test"]
    no_regression = (
        chosen_test["policy_exposure_violation_count"]
        <= base_test["policy_exposure_violation_count"]
        and _metric_value(chosen_test, ("unsafe_top1_rate",))
        <= _metric_value(base_test, ("unsafe_top1_rate",))
        and _metric_value(chosen_test, ("task_success_proxy_at", "5"))
        >= _metric_value(base_test, ("task_success_proxy_at", "5"))
        and _metric_value(chosen_test, ("ndcg_at_5",)) >= _metric_value(base_test, ("ndcg_at_5",))
    )
    improves = any(
        current > old
        for current, old in (
            (
                -_metric_value(chosen_test, ("unsafe_top1_rate",)),
                -_metric_value(base_test, ("unsafe_top1_rate",)),
            ),
            (
                _metric_value(chosen_test, ("task_success_proxy_at", "1")),
                _metric_value(base_test, ("task_success_proxy_at", "1")),
            ),
            (
                _metric_value(chosen_test, ("ndcg_at_5",)),
                _metric_value(base_test, ("ndcg_at_5",)),
            ),
            (
                _metric_value(chosen_test, ("mrr",)),
                _metric_value(base_test, ("mrr",)),
            ),
        )
    )
    promoted = chosen is not baseline and no_regression and improves
    recommended = chosen if promoted else baseline
    return {
        "status": "promote" if promoted else "keep_baseline",
        "baseline_variant": baseline["id"],
        "selected_on_validation": chosen["id"],
        "recommended_variant": recommended["id"],
        "recommended_weights": recommended["parameters"],
        "config_patch": {
            "exposure": {"review_risk_penalty": recommended["parameters"]["review_risk_penalty"]},
            "toolgraph": {"risk_penalty_scale": recommended["parameters"]["risk_penalty_scale"]},
        },
    }


def _read_telemetry(
    path: Path, include_rotated: bool
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    files_report: list[dict[str, Any]] = []
    quality: Counter[str] = Counter()
    warnings: list[str] = []
    files_to_read = discover_log_files(path, include_rotated=include_rotated)
    for log_path in files_to_read:
        try:
            size = log_path.stat().st_size
            with log_path.open("rb") as fh:
                data = fh.read(size)
        except OSError as exc:
            raise SelectionEvaluationError(
                f"cannot read telemetry: {log_path.name}: {exc}"
            ) from exc
        is_active = log_path == path
        partial_tail = bool(data and not data.endswith(b"\n"))
        lines = data.splitlines()
        if partial_tail and is_active:
            lines = lines[:-1]
            quality["truncated_tail_lines"] += 1
            warnings.append("active log ended with an incomplete line; tail skipped")
        files_report.append(
            {
                "name": log_path.name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "lines": len(lines),
            }
        )
        for line_no, raw_line in enumerate(lines, start=1):
            if not raw_line.strip():
                continue
            if len(raw_line) > MAX_LINE_BYTES:
                quality["oversized_lines"] += 1
                continue
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                quality["malformed_lines"] += 1
                continue
            if not isinstance(record, dict):
                quality["malformed_lines"] += 1
                continue
            if record.get("schema_version") != SCHEMA_VERSION:
                quality["unsupported_schema_records"] += 1
                continue
            if record.get("event") not in ("selection", "execution", "feedback"):
                quality["unknown_event_records"] += 1
                continue
            record["_order"] = (len(files_report) - 1, line_no)
            records.append(record)
    invalid = sum(
        quality[name]
        for name in (
            "oversized_lines",
            "malformed_lines",
            "unsupported_schema_records",
            "unknown_event_records",
        )
    )
    return records, {
        "files": files_report,
        "exists": bool(files_to_read),
        "status": "invalid" if invalid else "ok",
        "warnings": sorted(set(warnings)),
        **{name: quality[name] for name in sorted(quality)},
    }


def _observed_telemetry(records: list[dict[str, Any]], quality: dict[str, Any]) -> dict[str, Any]:
    selections: dict[str, dict[str, Any]] = {}
    executions: dict[str, dict[str, Any]] = {}
    feedback: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    duplicate_bytes = conflicting = 0
    poisoned: set[tuple[str, str]] = set()
    events: Counter[str] = Counter()
    for record in records:
        event = str(record["event"])
        events[event] += 1
        sid = record.get("selection_id")
        if not isinstance(sid, str) or not sid:
            quality["status"] = "invalid"
            quality["missing_selection_id"] = quality.get("missing_selection_id", 0) + 1
            continue
        if event == "feedback":
            feedback[sid].append(record)
            continue
        bucket = selections if event == "selection" else executions
        if (event, sid) in poisoned:
            # Once two copies disagreed the id is unusable, and a THIRD copy
            # does not settle the vote — it is one more claim about a record
            # whose history is already contradictory. Tracked monotonically so
            # a later copy cannot resurrect the selection, which is also what
            # `mms selection feedback` refuses to label (`records_conflict`).
            conflicting += 1
            continue
        existing = bucket.get(sid)
        if existing is None:
            bucket[sid] = record
            continue
        if not records_conflict(existing, record):
            duplicate_bytes += 1
        else:
            conflicting += 1
            poisoned.add((event, sid))
            bucket.pop(sid, None)
    if conflicting:
        quality["status"] = "invalid"
    quality["duplicate_records"] = duplicate_bytes
    quality["conflicting_records"] = conflicting
    quality["selection_only"] = len(set(selections) - set(executions))
    quality["execution_only"] = len(set(executions) - set(selections))

    rankers: Counter[str] = Counter()
    query_sources: Counter[str] = Counter()
    reject_reasons: Counter[str] = Counter()
    selected_ranks: list[int] = []
    rankable = selected_at_1 = selected_at_3 = selected_at_5 = 0
    parity_mismatches = invariant_violations = ranker_mismatches = 0
    latencies: list[float] = []
    ok = errors = cache_hit = cache_miss = cache_unknown = 0
    retry_values: list[float] = []
    cost_values: list[float] = []
    corrections = correction_den = overrides = override_den = 0
    for sid, selection in selections.items():
        ranker = str(selection.get("ranker_version", "unknown"))
        rankers[ranker] += 1
        candidate_tools = selection.get("candidate_tools")
        reject = selection.get("reject_reasons")
        if not isinstance(candidate_tools, list) or not isinstance(reject, dict):
            invariant_violations += 1
            continue
        if selection.get("candidate_count") != len(candidate_tools) or set(candidate_tools) & set(
            reject
        ):
            invariant_violations += 1
        for reason in reject.values():
            reject_reasons[str(reason)] += 1
        features = selection.get("candidate_features")
        if isinstance(features, dict):
            query_sources[str(features.get("query_source", "unknown"))] += 1
            ranked = features.get("ranked_candidates")
            if isinstance(ranked, list):
                seen_tools: set[str] = set()
                expected_rank = 1
                selected_rank: int | None = None
                for entry in ranked:
                    if not isinstance(entry, dict):
                        invariant_violations += 1
                        break
                    tool = entry.get("tool")
                    if (
                        not isinstance(tool, str)
                        or tool in seen_tools
                        or tool not in candidate_tools
                        or entry.get("rank") != expected_rank
                    ):
                        invariant_violations += 1
                    seen_tools.add(str(tool))
                    expected_rank += 1
                    relevance = entry.get("relevance_score")
                    penalty = entry.get("risk_penalty", 0.0)
                    final = entry.get("final_score")
                    if (
                        ranker in PARITY_RANKER_VERSIONS
                        and isinstance(relevance, (int, float))
                        and not isinstance(relevance, bool)
                        and isinstance(penalty, (int, float))
                        and not isinstance(penalty, bool)
                        and isinstance(final, (int, float))
                        and not isinstance(final, bool)
                    ):
                        expected = round(float(relevance) * (1.0 - float(penalty)), 6)
                        if abs(expected - float(final)) > 1e-6:
                            parity_mismatches += 1
                    if tool == selection.get("selected_tool"):
                        selected_rank = int(entry["rank"])
                rankable += 1
                if selected_rank is not None:
                    selected_ranks.append(selected_rank)
                    selected_at_1 += int(selected_rank <= 1)
                    selected_at_3 += int(selected_rank <= 3)
                    selected_at_5 += int(selected_rank <= 5)
        execution = executions.get(sid)
        if execution is not None:
            if execution.get("ranker_version") != selection.get("ranker_version"):
                ranker_mismatches += 1
                continue
            if execution.get("ok") is True:
                ok += 1
            else:
                errors += 1
            latency = execution.get("latency_ms")
            if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                latencies.append(float(latency))
            cache = execution.get("cache_hit")
            if cache is True:
                cache_hit += 1
            elif cache is False:
                cache_miss += 1
            else:
                cache_unknown += 1
            retry = execution.get("retry_count")
            cost = execution.get("cost")
            if isinstance(retry, (int, float)) and not isinstance(retry, bool):
                retry_values.append(float(retry))
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                cost_values.append(float(cost))
        folded: dict[str, Any] = {}
        for row in sorted(feedback.get(sid, []), key=lambda item: item["_order"]):
            for field in ("user_corrected", "operator_override"):
                if row.get(field) is not None:
                    folded[field] = row[field]
        if "user_corrected" in folded:
            correction_den += 1
            corrections += int(folded["user_corrected"] is True)
        if "operator_override" in folded:
            override_den += 1
            overrides += int(folded["operator_override"] is True)

    if invariant_violations or parity_mismatches or ranker_mismatches:
        quality["status"] = "invalid"
    quality["invariant_violations"] = invariant_violations
    quality["parity_mismatches"] = parity_mismatches
    quality["ranker_mismatches"] = ranker_mismatches
    attributable_cache = cache_hit + cache_miss
    return {
        "records": {name: events[name] for name in ("selection", "execution", "feedback")},
        "cohorts": dict(sorted(rankers.items())),
        "query_sources": dict(sorted(query_sources.items())),
        "reject_reasons": dict(sorted(reject_reasons.items())),
        "coverage": {
            "paired_selections": len(set(selections) & set(executions)),
            "rankable_selections": rankable,
            "selected_rank_known": len(selected_ranks),
            "feedback_selections": len(feedback),
            "retry_count_executions": len(retry_values),
            "cost_executions": len(cost_values),
        },
        "selected_tool_alignment": {
            "at_1": _ratio(selected_at_1, rankable),
            "at_3": _ratio(selected_at_3, rankable),
            "at_5": _ratio(selected_at_5, rankable),
            "mrr": {
                "value": _mean([1.0 / rank for rank in selected_ranks]),
                "denominator": len(selected_ranks),
            },
        },
        "execution": {
            "success_rate": _ratio(ok, ok + errors),
            "latency_ms": {
                "count": len(latencies),
                "p50": _percentile(latencies, 50),
                "p95": _percentile(latencies, 95),
                "p99": _percentile(latencies, 99),
            },
            "cache_hit_rate": _ratio(cache_hit, attributable_cache),
            "cache_unknown": cache_unknown,
            "retry_count_mean": {"value": _mean(retry_values), "denominator": len(retry_values)},
            "cost_mean": {"value": _mean(cost_values), "denominator": len(cost_values)},
        },
        "feedback": {
            "user_correction_rate": _ratio(corrections, correction_den),
            "operator_override_rate": _ratio(overrides, override_den),
        },
        "interpretation": (
            "Observational only: execution success is not task success, and ranker cohorts "
            "are not causal A/B groups."
        ),
    }


def evaluate_selection(
    *,
    telemetry_path: Path | str | None = None,
    dataset_path: Path | str | None = None,
    include_rotated: bool = True,
    baseline_review_penalty: float = 0.5,
    baseline_graph_scale: float = 1.0,
) -> SelectionEvaluationReport:
    """Evaluate production telemetry plus a labelled fixed corpus.

    The function is pure with respect to its inputs: it never writes files or
    modifies runtime configuration.  ``telemetry_path=None`` runs corpus-only.
    """
    if not 0.0 <= baseline_review_penalty <= 1.0 or baseline_graph_scale < 0.0:
        raise SelectionEvaluationError("invalid baseline penalty values")
    dataset = load_selection_dataset(dataset_path)
    grid = {(review, graph) for review in GRID_REVIEW for graph in GRID_GRAPH}
    grid.add((float(baseline_review_penalty), float(baseline_graph_scale)))
    variants = [_evaluate_variant(dataset, review, graph) for review, graph in sorted(grid)]
    recommendation = _recommend_variant(
        variants, float(baseline_review_penalty), float(baseline_graph_scale)
    )
    if telemetry_path is None:
        quality: dict[str, Any] = {
            "status": "ok",
            "exists": False,
            "files": [],
            "warnings": ["telemetry disabled for this evaluation"],
        }
        observed: dict[str, Any] = {
            "available": False,
            "interpretation": "No production telemetry was requested.",
        }
    else:
        telemetry = Path(telemetry_path).expanduser()
        records, quality = _read_telemetry(telemetry, include_rotated)
        observed = (
            _observed_telemetry(records, quality)
            if quality["exists"]
            else {
                "available": False,
                "interpretation": "No production telemetry file exists; corpus evaluation completed.",
            }
        )
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "inputs": {
            "dataset": {
                "id": dataset.get("dataset_id", "selection-eval-v1"),
                "schema_version": dataset["schema_version"],
                "sha256": dataset["_sha256"],
                "cases": len(dataset["cases"]),
                "split_counts": dataset["_split_counts"],
            },
            "telemetry_files": quality.get("files", []),
        },
        "data_quality": {k: v for k, v in quality.items() if k != "files"},
        "production": observed,
        "variants": variants,
        "comparison": recommendation,
        "status": "invalid" if quality.get("status") == "invalid" else "ok",
    }
    # Last-resort structural privacy check. Reports contain only aggregates,
    # case-free metrics, hashes, and basenames; a match indicates a regression.
    serialized = json.dumps(report, sort_keys=True, ensure_ascii=False)
    if contains_sensitive_content(serialized):
        raise SelectionEvaluationError("generated report matched sensitive-content patterns")
    return SelectionEvaluationReport(report)


def format_selection_report(report: SelectionEvaluationReport) -> str:
    """Render a compact deterministic human summary."""
    data = report.data
    lines = [
        "Selection Replay",
        "================",
        f"Status: {data['status']}",
        f"Dataset: {data['inputs']['dataset']['id']} ({data['inputs']['dataset']['cases']} cases)",
    ]
    production = data["production"]
    if production.get("available") is False:
        lines.append("Production telemetry: unavailable")
    else:
        records = production["records"]
        lines.append(
            "Production telemetry: "
            f"{records['selection']} selections, {records['execution']} executions"
        )
        success = production["execution"]["success_rate"]["value"]
        lines.append(f"Execution success (not task success): {success}")
    comparison = data["comparison"]
    lines.extend(
        [
            f"Recommendation: {comparison['status']}",
            f"Variant: {comparison['recommended_variant']}",
            "Config preview: "
            f"review_risk_penalty={comparison['recommended_weights']['review_risk_penalty']}, "
            f"risk_penalty_scale={comparison['recommended_weights']['risk_penalty_scale']}",
        ]
    )
    return "\n".join(lines) + "\n"
