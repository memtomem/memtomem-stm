"""bench_qa — LLM-as-judge advisory (opt-in via ``-m bench_qa_llm_judge``).

The marker is intentionally NOT ``bench_qa`` so the default CI job
(``-m bench_qa``) does not incur API cost. Runs only when
``OPENAI_API_KEY`` or ``ANTHROPIC_API_KEY`` is set — skipped otherwise.

Scope: happy-path scenarios (no ``force_tier``) where the compression
pipeline produces a semantic compressed output. Fallback ladder scenarios
(s01/s06/s08) require additional stubbing that mirrors the fallback
suite; S10 uses the surfacing harness. Both are follow-up candidates.

Determinism: both providers use ``temperature=0`` (OpenAI also ``seed=42``);
results cache to ``tests/bench/.llm_judge_cache/`` so re-runs are free.
``canonicalize_report`` strips the whole ``llm_judge`` block so the
existing s09 two-run determinism gate is unaffected.

Assertions are advisory: the test fails only if the adapter returns
``None`` despite an API key being present. Dimension scores and the
keyword-↔-LLM correlation (Pearson/Spearman) are logged at INFO for
review but never gate the run. A per-scenario ``llm_gate_min`` ratchet
belongs to a follow-up PR once scores stabilise over 1–2 weeks.
"""

from __future__ import annotations

import logging
import os

import pytest

from bench.bench_qa import (
    deterministic_trace_id,
    latest_metrics_row,
    load_fixture,
    make_proxy_manager,
    qa_answerable_ratio,
)
from bench.bench_qa.llm_judge_adapter import score_scenario, to_report_dict
from bench.bench_qa.runner import make_tool_result
from bench.llm_judge import compute_correlation

logger = logging.getLogger(__name__)

LLM_JUDGE_SCENARIOS = ["s02", "s03", "s04", "s05", "s07", "s09"]

_keyword_vs_llm_pairs: list[tuple[float, float]] = []


def _have_api_key() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))


@pytest.fixture(scope="module", autouse=True)
def _reset_correlation_buffer():
    _keyword_vs_llm_pairs.clear()
    yield


@pytest.mark.bench_qa_llm_judge
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_id", LLM_JUDGE_SCENARIOS)
async def test_llm_judge_scores_scenario(scenario_id: str, tmp_path, bench_qa_report) -> None:
    if not _have_api_key():
        pytest.skip("No LLM judge API key (OPENAI_API_KEY / ANTHROPIC_API_KEY)")

    fixture = load_fixture(scenario_id)
    assert fixture.get("force_tier") is None, (
        f"{scenario_id}: force_tier set — LLM judge covers happy-path only in this PR"
    )

    mgr, store, session = make_proxy_manager(
        tmp_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
    )
    session.call_tool.return_value = make_tool_result(fixture["payload"])

    trace_id = deterministic_trace_id(fixture["scenario_id"])
    compressed = await mgr.call_tool("fake", f"tool_{scenario_id}", {}, trace_id=trace_id)
    try:
        row = latest_metrics_row(store)
        assert row, f"{scenario_id}: proxy_metrics row was not written"

        probes = fixture.get("qa_probes", [])
        answerable, total = qa_answerable_ratio(probes, compressed)
        keyword_ratio = answerable / total if total else 1.0

        judge_result = score_scenario(
            scenario_id=scenario_id,
            description=fixture.get("description", ""),
            original=fixture["payload"],
            compressed=compressed,
            probes=probes,
            content_type=fixture.get("content_type", "text"),
        )
        assert judge_result is not None, (
            f"{scenario_id}: score_scenario returned None despite API key present"
        )

        report = to_report_dict(judge_result)
        bench_qa_report.record_llm_judge(scenario_id=scenario_id, llm_judge=report)

        logger.info(
            "llm_judge scenario=%s keyword=%.3f overall=%.3f "
            "(fc=%.3f sc=%.3f as=%.3f) cached=%s tokens=%d/%d%s",
            scenario_id,
            keyword_ratio,
            report["overall"],
            report["factual_completeness"],
            report["structural_coherence"],
            report["answer_sufficiency"],
            report["cached"],
            report["prompt_tokens"],
            report["completion_tokens"],
            f" error={report['error']!r}" if report.get("error") else "",
        )
        if not report.get("error"):
            _keyword_vs_llm_pairs.append((keyword_ratio, report["overall"]))
    finally:
        store.close()


@pytest.mark.bench_qa_llm_judge
def test_zz_keyword_vs_llm_judge_correlation() -> None:
    """Log Pearson/Spearman between keyword gate and LLM judge. Advisory."""
    if not _have_api_key():
        pytest.skip("No LLM judge API key")
    if len(_keyword_vs_llm_pairs) < 3:
        pytest.skip(f"need >=3 scenario results for correlation; have {len(_keyword_vs_llm_pairs)}")
    keyword_scores = [p[0] for p in _keyword_vs_llm_pairs]
    llm_scores = [p[1] for p in _keyword_vs_llm_pairs]
    corr = compute_correlation(keyword_scores, llm_scores)
    logger.info(
        "llm_judge correlation vs keyword: n=%d pearson=%.3f spearman=%.3f mad=%.3f",
        corr.n,
        corr.pearson_r,
        corr.spearman_rho,
        corr.mean_abs_diff,
    )
