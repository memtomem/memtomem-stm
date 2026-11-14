from __future__ import annotations

import pytest

from memtomem_stm.daemon.latency import (
    DaemonLatencyTracker,
    WINDOW_SIZE,
    _ceil_half,
    _percentile,
    hook_timeout_recommendation,
    recommendation_from_summary,
)


@pytest.mark.parametrize(
    ("values", "percentile", "expected"),
    [
        ([], 50, 0.0),
        ([3.0], 99, 3.0),
        ([1.0, 2.0, 3.0, 4.0], 50, 2.5),
        ([4.0, 1.0, 3.0, 2.0], 50, 2.5),
        ([1.0, 2.0, 3.0, 4.0], 0, 1.0),
        ([1.0, 2.0, 3.0, 4.0], 100, 4.0),
        ([10.0, 20.0], 25, 12.5),
        ([1.0, 2.0, 3.0], 50, 2.0),
    ],
)
def test_percentile_direct(
    values: list[float], percentile: float, expected: float
) -> None:
    assert _percentile(values, percentile) == expected


def test_percentile_does_not_reorder_input() -> None:
    values = [4.0, 1.0, 3.0, 2.0]
    assert _percentile(values, 50) == 2.5
    assert values == [4.0, 1.0, 3.0, 2.0]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, 0.0),
        (0.1, 0.5),
        (0.5, 0.5),
        (0.51, 1.0),
        (1.0, 1.0),
        (1.26, 1.5),
        (2.75, 3.0),
    ],
)
def test_ceil_half_direct(value: float, expected: float) -> None:
    assert _ceil_half(value) == expected


def test_recommendation_requires_five_uncensored_samples() -> None:
    summary = {
        "samples": 4,
        "p95_ms": 6_000.0,
        "p99_ms": 6_000.0,
        "timeout_samples": 2,
    }
    assert recommendation_from_summary(summary) == {
        "status": "collecting",
        "seconds": None,
        "censored": True,
    }


def test_recommendation_formula_and_readiness_thresholds() -> None:
    provisional = recommendation_from_summary(
        {"samples": 5, "p95_ms": 6_000.0, "p99_ms": 6_000.0, "timeout_samples": 0}
    )
    # max(1.5*6 + .25, 6 + .5) = 9.25, rounded up to the next 0.5s.
    assert provisional == {
        "status": "provisional",
        "seconds": 9.5,
        "raw_seconds": 9.25,
        "censored": False,
    }
    ready = recommendation_from_summary(
        {"samples": 20, "p95_ms": 500.0, "p99_ms": 900.0, "timeout_samples": 0}
    )
    assert ready["status"] == "ready"
    assert ready["seconds"] == 1.5


def test_too_slow_is_not_silently_clamped_to_thirty_seconds() -> None:
    recommendation = recommendation_from_summary(
        {"samples": 20, "p95_ms": 25_000.0, "p99_ms": 26_000.0, "timeout_samples": 0}
    )
    assert recommendation["status"] == "too_slow"
    assert recommendation["seconds"] is None
    assert recommendation["raw_seconds"] > 30.0


def test_tracker_window_is_bounded_and_censored_samples_stay_out_of_percentiles() -> None:
    tracker = DaemonLatencyTracker()
    for value in range(WINDOW_SIZE + 10):
        tracker.record("retrieval", float(value), "success")
    tracker.record("retrieval", 99_999.0, "timeout")
    tracker.record("retrieval", 1.0, "cold")
    tracker.record("retrieval", 1.0, "error")

    summary = tracker.snapshot()["retrieval"]
    assert summary["samples"] == WINDOW_SIZE
    assert summary["max_ms"] == float(WINDOW_SIZE + 9)
    assert summary["timeout_samples"] == 1
    assert summary["cold_samples"] == 1
    assert summary["error_samples"] == 1


def test_hook_timeout_preserves_inner_budget_plus_loopback_margin() -> None:
    surface = {
        "recommendation": {
            "status": "provisional",
            "seconds": 9.5,
        }
    }
    assert (
        hook_timeout_recommendation(
            surfacing_timeout_seconds=12.0,
            surface_summary=surface,
        )
        == 12.5
    )
