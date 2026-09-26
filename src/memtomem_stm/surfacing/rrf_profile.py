"""Two-leg RRF settings read from Core's ``runtime_profile`` snapshot.

One validator for two readers: ``mms doctor``'s RRF boundary advice
(:mod:`memtomem_stm.cli.rrf_diagnostics`) and the relevance-bucket ceiling the
LTM adapter stamps on each ``rrf`` result (:func:`rrf_score_ceiling`). The
profile is untrusted remote data captured at connect time, so every field is
checked before use.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Any

# The score of a result ranked first by both legs at Core's default fusion
# (``k=60``, weights ``[1, 1]``). Used whenever the profile cannot describe a
# positive-weight two-leg fusion.
RRF_BASELINE_CEILING = 2 / 61


class FusionGap(StrEnum):
    """Why a profile does not describe a usable two-leg RRF fusion."""

    NO_PROFILE = "no_profile"  # not schema 1, not ``ok``, or no ``search`` dict
    INCOMPLETE = "incomplete"  # one of the four fusion settings is missing
    INVALID = "invalid"  # a setting is present but malformed
    NOT_TWO_LEG = "not_two_leg"  # a zero weight, a disabled leg, or not hybrid


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _weights(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        try:
            weight = float(item)
        except OverflowError:
            return None
        if not math.isfinite(weight) or weight < 0:
            return None
        result.append(weight)
    return result[0], result[1]


def check_two_leg_fusion(profile: Any) -> tuple[int, tuple[float, float]] | FusionGap:
    """``(rrf_k, weights)`` for a positive-weight two-leg hybrid, else the gap."""
    if (
        not isinstance(profile, dict)
        or type(profile.get("schema_version")) is not int
        or profile["schema_version"] != 1
        or profile.get("config_state") != "ok"
        or not isinstance(profile.get("search"), dict)
    ):
        return FusionGap.NO_PROFILE
    search = profile["search"]
    required = ("rrf_k", "rrf_weights", "bm25_candidates", "dense_candidates")
    if any(key not in search for key in required):
        return FusionGap.INCOMPLETE
    weights = _weights(search["rrf_weights"])
    if weights is None or not all(
        _positive_int(search[key]) for key in ("rrf_k", "bm25_candidates", "dense_candidates")
    ):
        return FusionGap.INVALID
    if (
        min(weights) == 0
        or search.get("effective_mode") != "hybrid"
        or search.get("enable_bm25") is not True
        or search.get("enable_dense") is not True
    ):
        return FusionGap.NOT_TWO_LEG
    return search["rrf_k"], weights


def two_leg_fusion(profile: Any) -> tuple[int, tuple[float, float]] | None:
    """``(rrf_k, weights)`` when the profile describes a two-leg fusion, else ``None``."""
    fusion = check_two_leg_fusion(profile)
    return None if isinstance(fusion, FusionGap) else fusion


def rrf_score_ceiling(profile: Any) -> float:
    """Top of the relevance-bucket band for ``rrf``-stamped scores.

    ``sum(w) / (k + 1)``: the score of a result both legs rank first. It is a
    two-leg *reference*, not a maximum — Core's session-summary rescue leg and
    its decay/boost stages can push a score above it. Falls back to
    :data:`RRF_BASELINE_CEILING` when the profile is missing, single-leg,
    malformed, or its numbers overflow a float.
    """
    fusion = two_leg_fusion(profile)
    if fusion is None:
        return RRF_BASELINE_CEILING
    k, weights = fusion
    # Huge untrusted integers can overflow Python's int-to-float conversion.
    try:
        ceiling = sum(weights) / (k + 1)
    except OverflowError:
        return RRF_BASELINE_CEILING
    if not math.isfinite(ceiling) or ceiling <= 0:
        return RRF_BASELINE_CEILING
    return ceiling


def read_score_ceiling_hint(raw: Any) -> float | None:
    """A result's ``score_ceiling`` stamp when it is a finite positive number, else ``None``.

    The stamp crosses the daemon wire and sits on result objects that tests and
    fakes may build loosely, so every reader validates it; ``None`` means the
    renderer falls back to :data:`RRF_BASELINE_CEILING`.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = float(raw)
    except OverflowError:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value
