"""Bounded, privacy-safe latency telemetry for the warm daemon.

Only numeric durations and outcome counters are retained.  Query text, tool
arguments, result content, and trace identifiers never enter this tracker.
The window is deliberately process-local: it describes the currently warm
daemon rather than mixing cold starts and older deployments into one timeout
recommendation.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

LatencyKind = Literal["retrieval", "surface"]
LatencyOutcome = Literal["success", "timeout", "error", "cold"]

WINDOW_SIZE = 256
MIN_RECOMMENDATION_SAMPLES = 5
READY_RECOMMENDATION_SAMPLES = 20
MAX_RECOMMENDED_TIMEOUT_SECONDS = 30.0


def _percentile(values: list[float], percentile: float) -> float:
    """Linear-interpolated percentile (same estimator as proxy metrics)."""

    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile / 100.0
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _ceil_half(value: float) -> float:
    return math.ceil(value * 2.0) / 2.0


def recommendation_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Return a timeout recommendation from one telemetry summary.

    Successful durations are uncensored samples.  Timeout observations are
    reported separately and never folded into percentiles.  Fewer than five
    successes cannot support a numeric recommendation; five through nineteen
    are explicitly provisional, and twenty or more are ready for operational
    use.
    """

    samples = summary.get("samples")
    p95_ms = summary.get("p95_ms")
    p99_ms = summary.get("p99_ms")
    timeout_samples = summary.get("timeout_samples", 0)
    if (
        isinstance(samples, bool)
        or not isinstance(samples, int)
        or samples < MIN_RECOMMENDATION_SAMPLES
        or isinstance(p95_ms, bool)
        or not isinstance(p95_ms, (int, float))
        or isinstance(p99_ms, bool)
        or not isinstance(p99_ms, (int, float))
    ):
        return {
            "status": "collecting",
            "seconds": None,
            "censored": bool(timeout_samples),
        }

    p95_seconds = max(0.0, float(p95_ms) / 1000.0)
    p99_seconds = max(0.0, float(p99_ms) / 1000.0)
    raw = max(1.5 * p95_seconds + 0.25, p99_seconds + 0.5)
    rounded = max(1.0, _ceil_half(raw))
    if rounded > MAX_RECOMMENDED_TIMEOUT_SECONDS:
        return {
            "status": "too_slow",
            "seconds": None,
            "raw_seconds": round(raw, 3),
            "censored": bool(timeout_samples),
        }
    return {
        "status": "ready" if samples >= READY_RECOMMENDATION_SAMPLES else "provisional",
        "seconds": rounded,
        "raw_seconds": round(raw, 3),
        "censored": bool(timeout_samples),
    }


@dataclass
class _Series:
    durations_ms: deque[float] = field(default_factory=lambda: deque(maxlen=WINDOW_SIZE))
    timeout_samples: int = 0
    error_samples: int = 0
    cold_samples: int = 0

    def record(self, duration_ms: float, outcome: LatencyOutcome) -> None:
        if outcome == "success":
            self.durations_ms.append(max(0.0, float(duration_ms)))
        elif outcome == "timeout":
            self.timeout_samples += 1
        elif outcome == "error":
            self.error_samples += 1
        else:
            self.cold_samples += 1

    def snapshot(self) -> dict[str, Any]:
        values = list(self.durations_ms)
        summary: dict[str, Any] = {
            "window_size": WINDOW_SIZE,
            "samples": len(values),
            "timeout_samples": self.timeout_samples,
            "error_samples": self.error_samples,
            "cold_samples": self.cold_samples,
            "p50_ms": round(_percentile(values, 50), 2) if values else None,
            "p95_ms": round(_percentile(values, 95), 2) if values else None,
            "p99_ms": round(_percentile(values, 99), 2) if values else None,
            "max_ms": round(max(values), 2) if values else None,
        }
        summary["recommendation"] = recommendation_from_summary(summary)
        return summary


class DaemonLatencyTracker:
    """Two bounded series used by daemon ping and ``mms doctor``."""

    def __init__(self) -> None:
        self._series: dict[LatencyKind, _Series] = {
            "retrieval": _Series(),
            "surface": _Series(),
        }

    def record(self, kind: LatencyKind, duration_ms: float, outcome: LatencyOutcome) -> None:
        self._series[kind].record(duration_ms, outcome)

    def snapshot(self) -> dict[str, Any]:
        return {kind: series.snapshot() for kind, series in self._series.items()}


def hook_timeout_recommendation(
    *, surfacing_timeout_seconds: float, surface_summary: dict[str, Any] | None
) -> float:
    """Outer hook budget required to let the inner surfacing budget finish."""

    observed = None
    if isinstance(surface_summary, dict):
        rec = surface_summary.get("recommendation")
        if isinstance(rec, dict) and isinstance(rec.get("seconds"), (int, float)):
            observed = float(rec["seconds"])
    return _ceil_half(max(float(surfacing_timeout_seconds) + 0.5, observed or 0.0))
