"""Read-only advice about configured, unmodified two-leg RRF scores (#1012).

This module must not construct a tuner, open a feedback store, or query Core.
The profile is a collection/negotiation-time snapshot, not live search evidence.
"""

from __future__ import annotations

import json
import math
from typing import Any

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.rrf_profile import FusionGap, check_two_leg_fusion

DoctorCheck = tuple[str, str, str, str, str | None]
_SCOPE = (
    "Configured snapshot; unmodified two-leg RRF only, before rescue/rerank/decay/boost. "
    "Not a live score or auto-tuned threshold check; reconnect after Core config changes."
)

# Doctor wording for each structural gap; the checks themselves are shared with
# surfacing's relevance buckets (``surfacing.rrf_profile``).
_GAP_ADVICE: dict[FusionGap, tuple[str, str | None]] = {
    FusionGap.NO_PROFILE: (
        "RRF settings unavailable",
        "upgrade to a Core exposing runtime_profile.search fusion settings, then reconnect",
    ),
    FusionGap.INCOMPLETE: (
        "Core does not expose all four RRF settings",
        "upgrade Core, then restart the LTM/daemon to refresh its configuration snapshot",
    ),
    FusionGap.INVALID: ("Core reported invalid RRF settings; no threshold recommended", None),
    FusionGap.NOT_TWO_LEG: ("Two positive-weight, enabled retrieval legs are not reported", None),
}


def _recommendation(low: float, high: float) -> float | None:
    """Middle four-decimal grid point safe on both raw and rounded scores.

    Compose carries raw scores; structured mem_search rounds to four decimals.
    Requiring both avoids a recommendation that changes meaning on fallback.
    """
    lower = max(low, round(low, 4))
    upper = min(high, round(high, 4), 1.0)
    if lower >= upper:
        return None
    first = math.floor(lower * 10_000) + 1
    last = math.floor(upper * 10_000)
    # Correct a multiplication rounding exactly onto an integer boundary.
    while first / 10_000 <= lower:
        first += 1
    while last / 10_000 > upper:
        last -= 1
    while (last + 1) / 10_000 <= upper:
        last += 1
    if first > last:
        return None
    return ((first + last + 1) // 2) / 10_000


def rrf_boundary_doctor_checks(
    profile: Any, config: SurfacingConfig, *, effective_format: str | None = None
) -> list[DoctorCheck]:
    """Assess configured floors, preserving the existing WARN-only exit contract."""
    if not config.enabled:
        return []

    def unavailable(reason: str, action: str | None = None) -> list[DoctorCheck]:
        return [("ltm_rrf_boundary", "ltm RRF boundary", "WARN", f"{reason}. {_SCOPE}", action)]

    fusion = check_two_leg_fusion(profile)
    if isinstance(fusion, FusionGap):
        reason, action = _GAP_ADVICE[fusion]
        return unavailable(reason, action)
    k, weights = fusion
    search = profile["search"]
    if config.result_format != "structured" or effective_format != "structured":
        return unavailable(
            "Structured output is not confirmed; compact or unknown scores cannot support "
            "this four-decimal boundary check",
            "set MEMTOMEM_STM_SURFACING__RESULT_FORMAT=structured, restart the LTM/daemon, "
            "then rerun mms doctor to confirm format negotiation",
        )
    rerank = profile.get("rerank")
    # A configured bypass is only sent after successful tool-schema negotiation.
    # This snapshot carries no negotiated bypass evidence (including via daemon
    # ping), so require Core itself to report reranking disabled.
    if not isinstance(rerank, dict) or rerank.get("enabled") is not False:
        return unavailable(
            "Surfacing may use reranked scores; an RRF threshold is not applicable",
            "Core must report reranking disabled before this snapshot can recommend a score pin; "
            "surfacing.rerank=false alone does not prove a negotiated bypass",
        )

    # The engine uses the same top_k for compose and mem_search fallback.
    cases: list[tuple[str | None, float, int]] = [
        (None, config.min_score, config.effective_max_results())
    ]
    cases.extend(
        (
            name,
            cfg.min_score if cfg.min_score is not None else config.min_score,
            cfg.max_results if cfg.max_results is not None else config.effective_max_results(),
        )
        for name, cfg in sorted(config.context_tools.items())
        if cfg.enabled
    )
    checks: list[DoctorCheck] = []
    for name, threshold, max_results in cases:
        top_k = max_results * 2
        c1 = max(search["bm25_candidates"], top_k)
        c2 = max(search["dense_candidates"], top_k)
        # Huge untrusted integers can overflow Python's int-to-float conversion.
        try:
            low = max(weights) / (k + 1)
            high = weights[0] / (k + c1) + weights[1] / (k + c2)
        except OverflowError:
            return unavailable("Core RRF settings exceed the diagnostic's numeric range")
        if not math.isfinite(low) or not math.isfinite(high):
            return unavailable("Core RRF score bounds are non-finite")
        recommendation = _recommendation(low, high)
        valid = low < threshold <= high and round(low, 4) < threshold <= round(high, 4)
        detail = (
            f"configured min_score={threshold:g}; k={k}, weights={list(weights)}, "
            f"candidate limits=[{c1}, {c2}] (request top_k={top_k}); "
            f"raw interval=({low:.8g}, {high:.8g}], "
            f"four-decimal interval=({round(low, 4):.4f}, {round(high, 4):.4f}]. "
        )
        action = None
        if valid:
            detail += "Configured floor lies inside both theoretical intervals. "
        elif recommendation is None:
            reason = (
                "No separating interval exists"
                if low >= high
                else "No four-decimal threshold separates both score formats within [0, 1]"
            )
            detail += f"{reason}; no threshold recommended. "
        else:
            detail += f"Suggested min_score={recommendation:.4f}. "
            # A snippet, not a shell command; retain every other configured tool.
            tool_key = json.dumps(name if name is not None else "<tool>", ensure_ascii=True)
            action = (
                "Review the two-leg assumptions, then merge "
                f'{{{tool_key}: {{"min_score": {recommendation:.4f}}}}} into '
                "MEMTOMEM_STM_SURFACING__CONTEXT_TOOLS (preserve other entries). "
                "A per-tool pin overrides auto-tuning and the score-scale gate."
            )
        check_id = "ltm_rrf_boundary" if name is None else f"ltm_rrf_boundary:{name}"
        label = "ltm RRF boundary" if name is None else f"ltm RRF boundary: {name}"
        checks.append(
            (
                check_id,
                label,
                "PASS" if valid else "WARN",
                detail + _SCOPE,
                action,
            )
        )
    return checks
