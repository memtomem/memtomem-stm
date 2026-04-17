"""Frozen fixture + report schemas for bench_qa.

``schema_version`` is bumped whenever on-disk fixture format changes. The
loader rejects mismatched fixtures immediately so CI fails fast instead of
silently feeding the runner stale payloads.
"""

from __future__ import annotations

from typing import Literal, TypedDict

FIXTURE_SCHEMA_VERSION: Literal[1] = 1
REPORT_SCHEMA_VERSION: Literal[1] = 1


class QAProbe(TypedDict):
    """Single answerability probe.

    Semantics: ``answerable=1`` iff every keyword in ``expected_keywords``
    appears as a case-insensitive substring of the compressed output.
    Whole-word matching is not required — numeric answers, code fragments,
    and ID tokens all work.
    """

    question: str
    expected_keywords: list[str]


class SurfacingEval(TypedDict):
    """Ground truth for surfacing_recall@k on ``surf-*`` scenarios.

    ``returned_top_k`` is the first k IDs from ``surfacing_events.memory_ids``.
    ``recall@k = |returned_top_k ∩ expected_ids| / min(k, len(expected_ids))``.
    """

    query: str
    k: int
    expected_ids: list[str]


class BenchFixture(TypedDict, total=False):
    """On-disk scenario fixture (``tests/bench/fixtures/<scenario_id>.json``).

    Required fields: ``schema_version``, ``scenario_id``, ``payload``,
    ``expected_compressor``.  All others are optional so simple smoke
    fixtures stay terse.
    """

    schema_version: Literal[1]
    scenario_id: str
    description: str
    payload: str
    content_type: Literal["json", "markdown", "code", "text"]
    expected_compressor: str
    max_result_chars: int
    force_tier: int | None
    # Repeat ``payload`` N times on load. Keeps fixture files small when a
    # scenario only needs a large (10–20 KB) payload with repeating structure.
    payload_multiplier: int
    expected_keywords: list[str]
    qa_probes: list[QAProbe]
    # Minimum acceptable ``qa_answerable/total`` ratio for this scenario.
    # The 0.75 global gate in the plan is the *target* after 1–2 weeks of
    # observation; real compressors drop tail-side keywords so per-scenario
    # floors start looser and tighten as the suite stabilises.
    qa_gate_min: float
    # Per-scenario override for ``min_result_retention`` (default 0.65).
    # S5 lowers this so the SKELETON-only test can exercise the compressor
    # output directly without triggering the ratio-guard fallback ladder.
    min_retention: float
    surfacing_seeds: list[dict]
    surfacing_eval: SurfacingEval


class MetricSummary(TypedDict):
    """Subset of proxy_metrics row + computed fields recorded per scenario."""

    original_chars: int
    cleaned_chars: int
    compressed_chars: int
    compression_ratio: float
    compression_strategy: str | None
    ratio_violation: int
    surfacing_on_progressive_ok: int | None
    surface_error: str | None
    clean_ms: float
    compress_ms: float
    surface_ms: float


class RuleJudgeResult(TypedDict):
    """Keyword/heading/JSON score from ``tests.bench.judge.RuleBasedJudge``."""

    score: float
    missing_keywords: list[str]


class QAResult(TypedDict):
    """Probe-level answerability summary for a single scenario."""

    answerable: int
    total: int
    ratio: float


class ProgressiveResult(TypedDict):
    round_trip_equal: bool
    chunks: int
    total_chars: int


class SurfacingResult(TypedDict):
    recall_at_k: float
    returned_ids: list[str]
    expected_ids: list[str]


class ScenarioReport(TypedDict, total=False):
    scenario_id: str
    trace_id: str
    metrics: MetricSummary
    rule_judge: RuleJudgeResult
    qa: QAResult
    progressive: ProgressiveResult
    surfacing: SurfacingResult
    tier: str
    verdict: Literal["pass", "fail", "advisory"]


class BenchReport(TypedDict):
    schema_version: Literal[1]
    run_seed: int
    scenarios: list[ScenarioReport]
    tier_histogram: dict[str, int]
    totals: dict[str, float]
