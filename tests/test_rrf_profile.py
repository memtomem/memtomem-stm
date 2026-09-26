"""The shared two-leg RRF profile check and the bucket ceiling built on it (#1034)."""

from __future__ import annotations

import sys

import pytest

from memtomem_stm.surfacing.rrf_profile import (
    RRF_BASELINE_CEILING,
    FusionGap,
    check_two_leg_fusion,
    floor_to_decimals,
    read_score_ceiling_hint,
    rrf_score_ceiling,
    two_leg_fusion,
)


def profile(**search):
    return {
        "schema_version": 1,
        "config_state": "ok",
        "search": {
            "rrf_k": 60,
            "rrf_weights": [1.0, 1.0],
            "bm25_candidates": 50,
            "dense_candidates": 50,
            "enable_bm25": True,
            "enable_dense": True,
            "effective_mode": "hybrid",
            **search,
        },
    }


def test_baseline_constant_is_two_legs_at_default_k():
    assert RRF_BASELINE_CEILING == 2 / 61


@pytest.mark.parametrize(
    "search,expected",
    [
        ({}, 2 / 61),
        ({"rrf_k": 10}, 2 / 11),
        ({"rrf_weights": [0.5, 1.5]}, 2 / 61),
        ({"rrf_weights": [1.0, 3.0], "rrf_k": 1}, 2.0),
    ],
)
def test_ceiling_is_weight_sum_over_k_plus_one(search, expected):
    assert rrf_score_ceiling(profile(**search)) == pytest.approx(expected)


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"schema_version": 1},
        profile(rrf_weights=[1, 0]),  # one leg carries no weight
        profile(rrf_weights=[0, 0]),
        profile(effective_mode="bm25_only"),  # single-leg mode
        profile(enable_dense=False),
        profile(rrf_k=10**400),  # int too large for float division
        profile(rrf_k=True),
        profile(rrf_k=60.0),
        profile(rrf_weights=[1e308, 1e308]),  # sum overflows to inf
        profile(rrf_weights=[5e-324, 5e-324], rrf_k=10**20),  # underflows to 0.0
        profile(rrf_weights="[1, 1]"),
    ],
)
def test_unusable_profiles_fall_back_to_baseline(data):
    assert rrf_score_ceiling(data) == RRF_BASELINE_CEILING


def test_the_non_finite_and_zero_guards_are_reached():
    # Positive controls for the two arithmetic guards: both profiles pass the
    # structural check, so only the guard can produce the baseline.
    assert two_leg_fusion(profile(rrf_weights=[1e308, 1e308])) is not None
    assert two_leg_fusion(profile(rrf_weights=[5e-324, 5e-324], rrf_k=10**20)) is not None
    assert two_leg_fusion(profile(rrf_k=10**400)) is not None


def _missing(key: str) -> dict:
    data = profile()
    del data["search"][key]
    return data


@pytest.mark.parametrize(
    "data,gap",
    [
        (None, FusionGap.NO_PROFILE),
        ({**profile(), "schema_version": 2}, FusionGap.NO_PROFILE),
        ({**profile(), "config_state": "degraded"}, FusionGap.NO_PROFILE),
        (_missing("dense_candidates"), FusionGap.INCOMPLETE),
        (profile(bm25_candidates=0), FusionGap.INVALID),
        (profile(rrf_weights=[-1, 1]), FusionGap.INVALID),
        (profile(enable_bm25=False), FusionGap.NOT_TWO_LEG),
    ],
)
def test_check_names_each_gap(data, gap):
    assert check_two_leg_fusion(data) is gap
    assert two_leg_fusion(data) is None


def test_check_returns_k_and_weights():
    assert check_two_leg_fusion(profile(rrf_k=10, rrf_weights=[1, 2])) == (10, (1.0, 2.0))


@pytest.mark.parametrize("raw", [2 / 61, 1 / 61, 0.5, 3])
def test_ceiling_hint_accepts_finite_positive_numbers(raw):
    assert read_score_ceiling_hint(raw) == float(raw)


@pytest.mark.parametrize(
    "raw", [None, "x", "0.03", True, False, 0, -1, -0.5, float("inf"), float("nan"), 10**400, [1]]
)
def test_ceiling_hint_rejects_everything_else(raw):
    assert read_score_ceiling_hint(raw) is None


@pytest.mark.parametrize(
    ("value", "decimals", "expected"),
    [
        (2 / 61, 4, 0.0327),
        (2 / 61, 2, 0.03),
        (1.8 / 61, 4, 0.0295),
        (0.03, 4, 0.03),  # already exact: binary noise must not floor it
        (0.07, 2, 0.07),
        (1e-05, 4, 0.0),
        (5e-324, 4, 0.0),
        (1e29, 4, 1e29),  # beyond Decimal's default 28-digit precision
        (sys.float_info.max, 4, sys.float_info.max),
    ],
)
def test_floor_to_decimals(value, decimals, expected):
    assert floor_to_decimals(value, decimals) == expected
    assert floor_to_decimals(value, decimals) <= round(value, decimals)
