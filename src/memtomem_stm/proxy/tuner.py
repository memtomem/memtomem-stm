"""Compression auto-tuner: analyse proxy metrics and produce per-tool
tuning recommendations.

Read-only analysis — does not modify config or data stores.  Agents
(or operators) inspect recommendations via ``stm_tuning_recommendations``
and apply them manually to ``stm_proxy.json``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from memtomem_stm.proxy.compression_feedback_store import CompressionFeedbackStore
from memtomem_stm.proxy.config import ProxyConfig
from memtomem_stm.proxy.metrics_store import MetricsStore

# ── Thresholds ──────────────────────────────────────────────────────────

MIN_CALLS = 5
"""Minimum calls before producing any recommendation for a tool."""

HIGH_CONFIDENCE_CALLS = 20
MEDIUM_CONFIDENCE_CALLS = 10

VIOLATION_RATE_THRESHOLD = 0.15
"""Recommend budget increase when violation_rate exceeds this."""

OVER_GENEROUS_RATIO = 0.95
"""Recommend budget decrease when avg_ratio stays above this."""

STRATEGY_PIN_THRESHOLD = 0.80
"""Recommend pinning strategy when one dominates above this fraction."""


# ── Data types ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ToolProfile:
    server: str
    tool: str
    call_count: int
    violation_count: int
    violation_rate: float
    avg_ratio: float | None
    p95_original_chars: int
    dominant_strategy: str | None
    error_count: int
    feedback_count: int = 0
    feedback_dominant_kind: str | None = None


@dataclass(frozen=True, slots=True)
class TuningAction:
    field: str  # "max_result_chars" | "compression" | "retention_floor"
    current: str | None
    recommended: str
    reason: str


@dataclass(frozen=True, slots=True)
class TuningRecommendation:
    server: str
    tool: str
    confidence: str  # "high" | "medium" | "low"
    actions: list[TuningAction] = field(default_factory=list)


# ── Tuner ───────────────────────────────────────────────────────────────


class CompressionTuner:
    """Analyse accumulated metrics and produce per-tool tuning recommendations.

    Instantiate on-demand (lightweight); call :meth:`analyze` or
    :meth:`get_profiles` as needed.  Thread-safe as long as the
    underlying stores are (they use internal locks).
    """

    def __init__(
        self,
        metrics_store: MetricsStore,
        feedback_store: CompressionFeedbackStore | None = None,
        config: ProxyConfig | None = None,
    ) -> None:
        self._metrics = metrics_store
        self._feedback = feedback_store
        self._config = config

    # -- public API -------------------------------------------------------

    def get_profiles(self, since_seconds: float = 86400.0) -> list[ToolProfile]:
        raw = self._metrics.get_tool_profiles(since_seconds=since_seconds)
        feedback = (
            self._feedback.get_tool_feedback_summary(since_seconds=since_seconds)
            if self._feedback
            else {}
        )

        profiles: list[ToolProfile] = []
        for r in raw:
            fb = feedback.get(r["tool"], {})
            fb_total = fb.get("total", 0)
            fb_kinds = fb.get("by_kind", {})
            fb_dominant = max(fb_kinds, key=fb_kinds.get) if fb_kinds else None
            call_count = r["call_count"]
            violation_count = r["violation_count"]
            profiles.append(
                ToolProfile(
                    server=r["server"],
                    tool=r["tool"],
                    call_count=call_count,
                    violation_count=violation_count,
                    violation_rate=(violation_count / call_count if call_count > 0 else 0.0),
                    avg_ratio=r["avg_ratio"],
                    p95_original_chars=r["p95_original_chars"],
                    dominant_strategy=r["dominant_strategy"],
                    error_count=r["error_count"],
                    feedback_count=fb_total,
                    feedback_dominant_kind=fb_dominant,
                )
            )
        return profiles

    def analyze(
        self,
        since_seconds: float = 86400.0,
        tool_filter: str | None = None,
    ) -> list[TuningRecommendation]:
        profiles = self.get_profiles(since_seconds=since_seconds)
        if tool_filter:
            profiles = [p for p in profiles if p.tool == tool_filter]

        recommendations: list[TuningRecommendation] = []
        for p in profiles:
            if p.call_count < MIN_CALLS:
                continue

            actions = self._analyze_profile(p)
            if not actions:
                continue

            confidence = _confidence(p.call_count)
            recommendations.append(
                TuningRecommendation(
                    server=p.server,
                    tool=p.tool,
                    confidence=confidence,
                    actions=actions,
                )
            )
        return recommendations

    # -- heuristics -------------------------------------------------------

    def _analyze_profile(self, p: ToolProfile) -> list[TuningAction]:
        actions: list[TuningAction] = []
        current_max = self._current_max_chars(p.server, p.tool)

        # H1: High violation rate → increase budget
        if p.violation_rate > VIOLATION_RATE_THRESHOLD:
            recommended = max(
                int(p.p95_original_chars * 0.8),
                (current_max or 8000) + 2000,
            )
            if current_max is None or recommended > current_max:
                actions.append(
                    TuningAction(
                        field="max_result_chars",
                        current=str(current_max) if current_max else None,
                        recommended=str(recommended),
                        reason=(
                            f"violation rate {p.violation_rate:.0%} "
                            f"(p95 response {p.p95_original_chars} chars)"
                        ),
                    )
                )

        # H2: Over-generous budget → reduce budget
        if p.avg_ratio is not None and p.avg_ratio > OVER_GENEROUS_RATIO and p.violation_count == 0:
            recommended = max(
                int(p.p95_original_chars * 1.1),
                1000,
            )
            if current_max and recommended < current_max:
                actions.append(
                    TuningAction(
                        field="max_result_chars",
                        current=str(current_max),
                        recommended=str(recommended),
                        reason=(
                            f"avg ratio {p.avg_ratio:.2f} — responses nearly "
                            "always fit, budget can be reduced to save context"
                        ),
                    )
                )

        # H3: Strategy pinning — dominant strategy > 80%
        if (
            p.dominant_strategy
            and p.dominant_strategy != "none"
            and p.call_count >= MEDIUM_CONFIDENCE_CALLS
        ):
            current_strat = self._current_strategy(p.server, p.tool)
            if current_strat in ("auto", None) and p.dominant_strategy != "auto":
                actions.append(
                    TuningAction(
                        field="compression",
                        current=current_strat,
                        recommended=p.dominant_strategy,
                        reason=(
                            f"AUTO resolves to {p.dominant_strategy} in "
                            f">{STRATEGY_PIN_THRESHOLD:.0%} of calls — "
                            "pin to skip detection overhead"
                        ),
                    )
                )

        # H4: Feedback-driven — dominant kind informs strategy
        if p.feedback_count >= 3 and p.feedback_dominant_kind:
            fb_action = _feedback_recommendation(p)
            if fb_action:
                actions.append(fb_action)

        return actions

    # -- config lookups ---------------------------------------------------

    def _current_max_chars(self, server: str, tool: str) -> int | None:
        if not self._config:
            return None
        srv = self._config.upstream_servers.get(server)
        if not srv:
            return None
        override = srv.tool_overrides.get(tool)
        if override and override.max_result_chars is not None:
            return override.max_result_chars
        return srv.max_result_chars

    def _current_strategy(self, server: str, tool: str) -> str | None:
        if not self._config:
            return None
        srv = self._config.upstream_servers.get(server)
        if not srv:
            return None
        override = srv.tool_overrides.get(tool)
        if override and override.compression is not None:
            return override.compression.value
        return srv.compression.value


# ── helpers ─────────────────────────────────────────────────────────────


def _confidence(call_count: int) -> str:
    if call_count >= HIGH_CONFIDENCE_CALLS:
        return "high"
    if call_count >= MEDIUM_CONFIDENCE_CALLS:
        return "medium"
    return "low"


def _feedback_recommendation(p: ToolProfile) -> TuningAction | None:
    kind = p.feedback_dominant_kind
    if kind == "truncated":
        return TuningAction(
            field="compression",
            current=None,
            recommended="hybrid",
            reason=(
                f"{p.feedback_count} feedback reports, dominant kind "
                f'"{kind}" — switch to hybrid to preserve structure'
            ),
        )
    if kind == "missing_metadata":
        return TuningAction(
            field="compression",
            current=None,
            recommended="extract_fields",
            reason=(
                f"{p.feedback_count} feedback reports, dominant kind "
                f'"{kind}" — switch to extract_fields for metadata preservation'
            ),
        )
    if kind == "missing_example":
        return TuningAction(
            field="max_result_chars",
            current=None,
            recommended=str(max(p.p95_original_chars, 16000)),
            reason=(
                f"{p.feedback_count} feedback reports, dominant kind "
                f'"{kind}" — increase budget to preserve examples'
            ),
        )
    return None


# ── formatting ──────────────────────────────────────────────────────────


def format_recommendations(
    recs: list[TuningRecommendation],
    profiles: list[ToolProfile],
    since_hours: float,
) -> str:
    """Format recommendations as a human-readable text report."""
    total_calls = sum(p.call_count for p in profiles)
    lines = [
        f"Tuning Recommendations ({since_hours:.0f}h window, "
        f"{total_calls} calls, {len(profiles)} tools analyzed)",
        "=" * 60,
    ]

    if not recs:
        lines.append("\nNo recommendations — all tools within healthy parameters.")
        return "\n".join(lines)

    for rec in recs:
        profile = next(
            (p for p in profiles if p.server == rec.server and p.tool == rec.tool),
            None,
        )
        lines.append(
            f"\n{rec.server}/{rec.tool}  "
            f"[{rec.confidence.upper()} confidence"
            f"{f', {profile.call_count} calls' if profile else ''}]"
        )
        if profile:
            lines.append(
                f"  Violation rate: {profile.violation_rate:.1%} "
                f"({profile.violation_count}/{profile.call_count})"
            )
            if profile.avg_ratio is not None:
                lines.append(f"  Avg compression ratio: {profile.avg_ratio:.2f}")
        for a in rec.actions:
            current = a.current or "default"
            lines.append(f"  -> {a.field}: {current} -> {a.recommended}")
            lines.append(f"     {a.reason}")

    return "\n".join(lines)
