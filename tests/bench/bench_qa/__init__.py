"""End-to-end bench_qa — drives ProxyManager.call_tool() through fixture
scenarios and asserts compression efficiency + information loss gates.

See ``/Users/pdstudio/.claude/plans/mcp-snug-river.md`` for the design
(Context → Goals → Harness → Metrics → Integration).
"""

from .drift import classify_drift, format_drift_md
from .judge import qa_answerable_ratio, surfacing_recall_at_k
from .loader import fixtures_dir, load_fixture
from .replay import SUITE_SCENARIOS, run_full_suite, run_scenario_once
from .report import BenchReportCollector, canonicalize_report
from .runner import (
    deterministic_trace_id,
    latest_metrics_row,
    make_proxy_manager,
)
from .schema import (
    BenchFixture,
    BenchReport,
    QAProbe,
    ScenarioReport,
    SurfacingEval,
)

__all__ = [
    "SUITE_SCENARIOS",
    "BenchFixture",
    "BenchReport",
    "BenchReportCollector",
    "QAProbe",
    "ScenarioReport",
    "SurfacingEval",
    "canonicalize_report",
    "classify_drift",
    "deterministic_trace_id",
    "fixtures_dir",
    "format_drift_md",
    "latest_metrics_row",
    "load_fixture",
    "make_proxy_manager",
    "qa_answerable_ratio",
    "run_full_suite",
    "run_scenario_once",
    "surfacing_recall_at_k",
]
