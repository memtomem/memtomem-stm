"""End-to-end bench_qa — drives ProxyManager.call_tool() through fixture
scenarios and asserts compression efficiency + information loss gates.

See ``/Users/pdstudio/.claude/plans/mcp-snug-river.md`` for the design
(Context → Goals → Harness → Metrics → Integration).
"""

from .loader import fixtures_dir, load_fixture
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
    "BenchFixture",
    "BenchReport",
    "QAProbe",
    "ScenarioReport",
    "SurfacingEval",
    "deterministic_trace_id",
    "fixtures_dir",
    "latest_metrics_row",
    "load_fixture",
    "make_proxy_manager",
]
