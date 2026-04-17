"""Session-level plumbing for bench_qa — report collection + final write.

The ``bench_qa_report`` fixture returns a process-wide
:class:`bench.bench_qa.report.BenchReportCollector`. Scenario tests call
``record_scenario`` after their own gate assertions; the
``pytest_sessionfinish`` hook flushes ``report.json`` + ``summary.md``
to ``$BENCH_QA_REPORT_DIR`` (or ``/tmp/stm-qa-<ts>/`` when the env var
is unset) provided at least one bench_qa scenario ran.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from bench.bench_qa.report import BenchReportCollector

_COLLECTOR: BenchReportCollector | None = None


def _collector() -> BenchReportCollector:
    global _COLLECTOR
    if _COLLECTOR is None:
        _COLLECTOR = BenchReportCollector()
    return _COLLECTOR


@pytest.fixture(scope="session")
def bench_qa_report() -> BenchReportCollector:
    return _collector()


def _resolve_output_dir() -> Path:
    env = os.environ.get("BENCH_QA_REPORT_DIR")
    if env:
        return Path(env)
    return Path(f"/tmp/stm-qa-{int(time.time())}")


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 — pytest hook signature
    collector = _COLLECTOR
    if collector is None or not collector.has_data():
        return
    out_dir = _resolve_output_dir()
    path = collector.write(out_dir)
    # Emit to stdout so CI logs pick it up alongside the test summary.
    print(f"\nbench_qa report written to: {path}")
