"""Compression auto-tuner: analyse proxy metrics and produce per-tool
tuning recommendations.

Read-only analysis — does not modify config or data stores.  Agents
(or operators) inspect recommendations via ``stm_tuning_recommendations``
and apply them with ``mms tune --apply`` (or edit ``stm_proxy.json``
manually).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from memtomem_stm.proxy.compression_feedback_store import CompressionFeedbackStore
from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    effective_compression_pair,
    effective_max_result_chars,
)
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

SETTABLE_STRATEGIES = frozenset(s.value for s in CompressionStrategy)
"""Labels a ``compression`` key accepts.

The metrics column records the strategy a call *ran under*, which is not
always one of these: a degraded call records the path it took
(``hybrid→progressive_fallback``). Recommending one of those would be advice
no config can accept.
"""


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
    auto_dominant_strategy: str | None
    error_count: int
    auto_dominant_strategy_count: int = 0
    auto_strategy_count: int = 0
    ratio_count: int = 0
    """Calls ``avg_ratio`` is averaged over — non-error, non-empty-cleaned."""
    feedback_count: int = 0
    feedback_dominant_kind: str | None = None

    @property
    def auto_dominant_strategy_share(self) -> float:
        """Fraction of AUTO's own resolutions won by ``auto_dominant_strategy``.

        Numerator and denominator both count calls whose strategy AUTO chose
        at runtime, so this measures how consistently AUTO lands on one
        strategy — the only thing a pin can be judged against. Calls that ran
        under a pin, and calls whose provenance was never recorded, are in
        neither (#933).
        """
        if self.auto_strategy_count <= 0:
            return 0.0
        return self.auto_dominant_strategy_count / self.auto_strategy_count


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
            fb = feedback.get((r["server"], r["tool"]), {})
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
                    auto_dominant_strategy=r["auto_dominant_strategy"],
                    error_count=r["error_count"],
                    auto_dominant_strategy_count=r["auto_dominant_strategy_count"],
                    auto_strategy_count=r["auto_strategy_count"],
                    ratio_count=r["ratio_count"],
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

            actions, evidence = self._analyze_profile(p)
            if not actions:
                continue

            confidence = _confidence(evidence)
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

    def _analyze_profile(self, p: ToolProfile) -> tuple[list[TuningAction], int]:
        """Actions for this tool, and the observation count to label them with.

        The confidence label covers the whole recommendation, so it is the
        smallest population any emitted action rests on — the weakest link,
        since one label speaks for every action under it. H1 reads all calls;
        H2 the rows ``avg_ratio`` is averaged over, H3 the calls AUTO resolved,
        H4 the feedback reports, each of which can be far fewer than
        ``call_count``. A heuristic that narrows the label also
        states its population in its reason, so the report says what the
        number is over rather than leaving the label to carry it alone.
        """
        actions: list[TuningAction] = []
        evidence = p.call_count
        current_max, token_governed = self._current_budget(p.server, p.tool)

        # H1: High violation rate → increase budget
        if p.violation_rate > VIOLATION_RATE_THRESHOLD and not token_governed:
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
        if (
            p.avg_ratio is not None
            and p.avg_ratio > OVER_GENEROUS_RATIO
            and p.violation_count == 0
            and not token_governed
        ):
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
                            f"avg ratio {p.avg_ratio:.2f} over {p.ratio_count} "
                            f"of {p.call_count} calls — responses nearly "
                            "always fit, budget can be reduced to save context"
                        ),
                    )
                )
                # The average is over the non-error calls that recorded a
                # cleaned length; the rest contributed nothing to the number
                # this advice rests on (#934).
                evidence = min(evidence, p.ratio_count)

        # H3: Strategy pinning — AUTO settles on one settable strategy > 80% of
        # the time. Every count here is over the calls AUTO actually resolved:
        # the advice is "AUTO keeps reaching the same answer, so state it and
        # skip the detection", which a call that ran under a pin is no evidence
        # for (#933) — it never ran detection, and its label attests the pin.
        # The volume gate counts that same population, as the share does: one
        # AUTO call among nine pinned ones otherwise reports a 100% share.
        if (
            p.auto_dominant_strategy
            and p.auto_dominant_strategy != "none"
            and p.auto_dominant_strategy in SETTABLE_STRATEGIES
            and p.auto_strategy_count >= MEDIUM_CONFIDENCE_CALLS
            and p.auto_dominant_strategy_share > STRATEGY_PIN_THRESHOLD
        ):
            current_strat = self._current_strategy(p.server, p.tool)
            # The rows say AUTO ran; the config says whether it still does. A
            # tool pinned since those calls were made needs no pin, and the
            # window can outlive the change either way (#933).
            if current_strat in ("auto", None) and p.auto_dominant_strategy != "auto":
                actions.append(
                    TuningAction(
                        field="compression",
                        current=current_strat,
                        recommended=p.auto_dominant_strategy,
                        reason=(
                            f"AUTO selected {p.auto_dominant_strategy} for "
                            f"{p.auto_dominant_strategy_count} of "
                            f"{p.auto_strategy_count} calls it resolved "
                            f"({p.auto_dominant_strategy_share:.1%}) — "
                            "pin to skip detection overhead"
                        ),
                    )
                )
                evidence = min(evidence, p.auto_strategy_count)

        # H4: Feedback-driven — dominant kind informs strategy
        if p.feedback_count >= 3 and p.feedback_dominant_kind:
            fb_action = _feedback_recommendation(p)
            # Its "missing_example" branch recommends a budget, so it is subject
            # to the same rule as H1/H2: a per-tool token budget outranks the
            # per-tool ``max_result_chars`` that ``--apply`` writes. Its
            # ``compression`` recommendation is unaffected.
            if fb_action and not (token_governed and fb_action.field == "max_result_chars"):
                actions.append(fb_action)
                # Its reason reports the feedback count, and that is what it
                # rests on: three reports against twenty-five calls are three
                # observations, not twenty-five (#934).
                evidence = min(evidence, p.feedback_count)

        return actions, evidence

    # -- config lookups ---------------------------------------------------

    def _current_budget(self, server: str, tool: str) -> tuple[int | None, bool]:
        """The char budget this tool's calls run under, and whether tokens rule.

        Must match what the proxy resolves (#926). Reading the nominal
        ``max_result_chars`` saw a server's own default where calls ran under
        the model-aware global, so H1 could "increase" a budget to less than it
        already was, and H2 "reduce" it to more.

        The flag is true when a PER-TOOL token budget is in force. H1 and H2
        recommend a per-tool `max_result_chars`, which that budget outranks, so
        their advice would be an edit with no effect — they skip instead. A
        server-level token budget does not count: the per-tool write clears it.
        """
        if not self._config:
            return None, False
        srv = self._config.upstream_servers.get(server)
        if not srv:
            return None, False
        override = srv.tool_overrides.get(tool)
        max_chars, _ = effective_max_result_chars(srv, override, self._config)
        # ``mms tune --apply`` writes recommendations as PER-TOOL overrides, and
        # a per-tool ``max_result_chars`` clears an inherited server token
        # budget — so an action is a no-op only against a per-tool token
        # budget, which outranks the field being written. A server-level one is
        # superseded by the write, and suppressing there would withhold advice
        # that works.
        return max_chars, override is not None and override.max_result_tokens is not None

    def _current_strategy(self, server: str, tool: str) -> str | None:
        """The strategy this tool's calls actually run under.

        Must match what the proxy resolves, global default included (#926).
        Stopping at the per-server field read a server that omits
        ``compression`` as ``auto``, so H3 below would tell an operator who
        pinned a strategy globally to pin the one they already had — with a
        reason claiming AUTO was resolving at runtime, which it was not.
        """
        if not self._config:
            return None
        srv = self._config.upstream_servers.get(server)
        if not srv:
            return None
        override = srv.tool_overrides.get(tool)
        compression, _ = effective_compression_pair(srv, override, self._config)
        return compression.value


# ── helpers ─────────────────────────────────────────────────────────────


def _confidence(observations: int) -> str:
    """Label a recommendation by how many observations it rests on.

    Not always calls: an H4 recommendation rests on feedback reports, and an
    H2 one on the calls that contributed to ``avg_ratio``. The thresholds are
    shared because the label answers the same question in each case — how much
    the number stated in the reason carries.
    """
    if observations >= HIGH_CONFIDENCE_CALLS:
        return "high"
    if observations >= MEDIUM_CONFIDENCE_CALLS:
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
