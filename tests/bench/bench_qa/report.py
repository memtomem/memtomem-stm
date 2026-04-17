"""bench_qa report collector — frozen JSON + readable markdown summary.

Scenario tests call :meth:`BenchReportCollector.record_scenario` after
their own gate assertions; after the pytest session finishes, the
conftest writes a single ``report.json`` + ``summary.md`` to the
configured output dir (default: ``/tmp/stm-qa-<ts>/``).

Determinism: ``report.json`` excludes wall-clock fields that would
break a two-run diff — latencies, timestamps, and the ``run_timestamp``
header are canonical strip candidates when diffing.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path

from .schema import (
    REPORT_SCHEMA_VERSION,
    BenchReport,
    MetricSummary,
    ProgressiveResult,
    QAResult,
    RuleJudgeResult,
    ScenarioReport,
    SurfacingResult,
)

logger = logging.getLogger(__name__)


def _coerce_metrics(row: dict, original_chars: int) -> MetricSummary:
    cleaned = row.get("cleaned_chars", 0) or 0
    compressed = row.get("compressed_chars", 0) or 0
    ratio = compressed / cleaned if cleaned else 0.0
    return MetricSummary(
        original_chars=row.get("original_chars", original_chars) or original_chars,
        cleaned_chars=cleaned,
        compressed_chars=compressed,
        compression_ratio=round(ratio, 4),
        compression_strategy=row.get("compression_strategy"),
        ratio_violation=int(row.get("ratio_violation") or 0),
        surfacing_on_progressive_ok=row.get("surfacing_on_progressive_ok"),
        surface_error=row.get("surface_error"),
        clean_ms=float(row.get("clean_ms") or 0.0),
        compress_ms=float(row.get("compress_ms") or 0.0),
        surface_ms=float(row.get("surface_ms") or 0.0),
    )


class BenchReportCollector:
    """Accumulates one :class:`ScenarioReport` per bench_qa test.

    Intentionally in-memory and process-local — the conftest session hook
    flushes to disk exactly once. Thread safety is not required because
    pytest bench_qa runs are single-process.
    """

    def __init__(self) -> None:
        self._rows: list[ScenarioReport] = []

    def has_data(self) -> bool:
        return bool(self._rows)

    def record_scenario(
        self,
        *,
        scenario_id: str,
        trace_id: str,
        row: dict,
        qa_answerable: int,
        qa_total: int,
        original_chars: int,
        verdict: str = "pass",
        rule_score: float | None = None,
        missing_keywords: list[str] | None = None,
        progressive: ProgressiveResult | None = None,
        surfacing: SurfacingResult | None = None,
    ) -> None:
        metrics = _coerce_metrics(row, original_chars)
        qa_ratio = qa_answerable / qa_total if qa_total else 1.0
        qa_result = QAResult(
            answerable=qa_answerable,
            total=qa_total,
            ratio=round(qa_ratio, 4),
        )
        rule = RuleJudgeResult(
            score=round(rule_score, 2) if rule_score is not None else 0.0,
            missing_keywords=list(missing_keywords or []),
        )
        entry: ScenarioReport = {
            "scenario_id": scenario_id,
            "trace_id": trace_id,
            "metrics": metrics,
            "rule_judge": rule,
            "qa": qa_result,
            "tier": metrics["compression_strategy"] or "unknown",
            "verdict": verdict,  # type: ignore[typeddict-item]
        }
        if progressive is not None:
            entry["progressive"] = progressive
        if surfacing is not None:
            entry["surfacing"] = surfacing
        self._rows.append(entry)

    def build_report(self, *, run_seed: int = 0) -> BenchReport:
        tier_hist: Counter[str] = Counter(r["tier"] for r in self._rows)
        totals = {
            "scenarios": float(len(self._rows)),
            "passing": float(sum(1 for r in self._rows if r["verdict"] == "pass")),
            "failing": float(sum(1 for r in self._rows if r["verdict"] == "fail")),
            "tokens_saved_approx": float(
                sum(
                    max(0, r["metrics"]["cleaned_chars"] - r["metrics"]["compressed_chars"])
                    for r in self._rows
                )
                // 4
            ),
        }
        return BenchReport(
            schema_version=REPORT_SCHEMA_VERSION,
            run_seed=run_seed,
            scenarios=sorted(self._rows, key=lambda r: r["scenario_id"]),
            tier_histogram=dict(tier_hist),
            totals=totals,
        )

    def write(self, out_dir: Path, *, run_seed: int = 0) -> Path:
        """Write ``report.json`` and ``summary.md`` under *out_dir*.

        Creates the directory if missing. Returns the output directory so
        callers can log it for CI artifact upload.
        """
        out_dir.mkdir(parents=True, exist_ok=True)
        report = self.build_report(run_seed=run_seed)
        (out_dir / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
        (out_dir / "summary.md").write_text(_format_summary_md(report), encoding="utf-8")
        logger.info("bench_qa report written to %s (%d scenarios)", out_dir, len(self._rows))
        return out_dir


def _format_summary_md(report: BenchReport) -> str:
    """Human-readable table for CI logs and manual review."""
    lines = [
        "# bench_qa summary",
        "",
        f"- Scenarios: {int(report['totals']['scenarios'])}",
        f"- Passing: {int(report['totals']['passing'])}",
        f"- Failing: {int(report['totals']['failing'])}",
        f"- Tokens saved (approx): {int(report['totals']['tokens_saved_approx'])}",
        "",
        "## Tier histogram",
        "",
    ]
    for tier, count in sorted(report["tier_histogram"].items()):
        lines.append(f"- `{tier}`: {count}")
    lines += [
        "",
        "## Scenarios",
        "",
        "| id | tier | ratio | violation | qa | verdict |",
        "|----|------|-------|-----------|----|---------|",
    ]
    for r in report["scenarios"]:
        m = r["metrics"]
        qa = r["qa"]
        lines.append(
            f"| {r['scenario_id']} | `{r['tier']}` | {m['compression_ratio']:.2f} | "
            f"{m['ratio_violation']} | {qa['answerable']}/{qa['total']} | "
            f"**{r['verdict']}** |"
        )
    lines.append("")
    return "\n".join(lines)
