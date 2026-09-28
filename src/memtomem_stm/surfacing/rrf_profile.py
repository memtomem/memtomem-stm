"""Two-leg RRF settings read from Core's ``runtime_profile`` snapshot.

One validator for two readers: ``mms doctor``'s RRF boundary advice
(:mod:`memtomem_stm.cli.rrf_diagnostics`) and the relevance-bucket ceiling the
LTM adapter stamps on each ``rrf`` result (:func:`rrf_score_ceiling`). The
profile is untrusted remote data captured at connect time, so every field is
checked before use.
"""

from __future__ import annotations

import math
from decimal import ROUND_FLOOR, Decimal, localcontext
from enum import StrEnum
from typing import Any

# The score of a result ranked first by both legs at Core's default fusion
# (``k=60``, weights ``[1, 1]``). Used whenever the profile cannot describe a
# positive-weight two-leg fusion.
RRF_BASELINE_CEILING = 2 / 61


# Decimal places Core rounds a score to before STM sees it: ``round(score, 4)``
# in the structured ``mem_search`` JSON and ``f"{score:.2f}"`` in the compact
# text format. A threshold between the rounded and the exact reference rejects
# a result both legs rank first (#1062).
STRUCTURED_SCORE_DECIMALS = 4
COMPACT_SCORE_DECIMALS = 2


def floor_to_decimals(value: float, decimals: int) -> float:
    """*value* rounded down to *decimals* places, so it never exceeds the
    score Core delivers for *value* at that precision.

    Goes through ``repr`` so binary noise (``0.03`` stored as
    ``0.0299999…``) does not floor a value that is already exact. The context
    precision covers any finite float (up to 309 integer digits), which the
    default 28 digits would reject for a huge stamp.
    """
    step = Decimal(1).scaleb(-decimals)
    with localcontext(prec=320 + decimals):
        return float(Decimal(repr(value)).quantize(step, rounding=ROUND_FLOOR))


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


def is_schema_one(profile: Any) -> bool:
    """Whether *profile* is a schema-1 ``runtime_profile`` dict.

    Strict: JSON ``true`` and ``1.0`` compare equal to ``1`` in Python, and
    every reader must agree on what schema 1 is, or a profile is judged by one
    and rendered as absent by another (PR #1094 review).
    """
    return (
        isinstance(profile, dict)
        and type(profile.get("schema_version")) is int
        and profile["schema_version"] == 1
    )


def check_two_leg_fusion(profile: Any) -> tuple[int, tuple[float, float]] | FusionGap:
    """``(rrf_k, weights)`` for a positive-weight two-leg hybrid, else the gap."""
    if (
        not is_schema_one(profile)
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


# The closed sets Core's ``collect_runtime_profile`` draws these fields from.
_CONFIG_STATES = frozenset({"ok", "error"})
RETRIEVAL_MODES = frozenset({"hybrid", "bm25_only", "dense_only", "disabled"})
_DEPENDENCIES = ("fastembed", "kiwipiepy")
_REQUIRED_FOR = frozenset({"embedding", "rerank", "tokenizer"})
_MISSING_EXTRAS = frozenset({"onnx", "korean"})
_UNRECOGNIZED = "unrecognized"


def _known(value: Any, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else _UNRECOGNIZED


def _setting(value: Any) -> int | None:
    # No upper bound: Core accepts any positive int, and the digits are
    # already capped by JSON parsing.
    return value if _positive_int(value) else None


def _strict_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _members(value: Any, allowed: frozenset[str]) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item in allowed))


def project_runtime_profile(profile: Any) -> dict[str, Any] | None:
    """Core's ``runtime_profile`` as a report shows it: the fields STM reads,
    each rendered from a closed set.

    For display only (#1082). The diagnostics judge the raw snapshot, so this
    never decides a verdict; it decides what a health or doctor report prints.
    A string is kept only when it is one of the values Core draws that field
    from (``"unrecognized"`` otherwise), a number only when it is a strict
    positive int, a flag only when it is a bool, a list only with its known
    members, each once (``None`` when it is not a list). A field STM does not
    read (the embedding model, the tokenizer, provider names, dependency
    versions, unknown keys) is dropped, and an absent key stays absent.
    ``None`` for anything that is not a schema-1 profile.
    """
    if not is_schema_one(profile):
        return None
    projected: dict[str, Any] = {"schema_version": 1}
    if "config_state" in profile:
        projected["config_state"] = _known(profile["config_state"], _CONFIG_STATES)

    search = profile.get("search")
    if isinstance(search, dict):
        out: dict[str, Any] = {}
        for key in ("rrf_k", "bm25_candidates", "dense_candidates"):
            if key in search:
                out[key] = _setting(search[key])
        if "rrf_weights" in search:
            weights = search["rrf_weights"]
            out["rrf_weights"] = list(weights) if _weights(weights) is not None else None
        for key in ("enable_bm25", "enable_dense"):
            if key in search:
                out[key] = _strict_bool(search[key])
        for key in ("configured_mode", "effective_mode"):
            if key in search:
                out[key] = _known(search[key], RETRIEVAL_MODES)
        projected["search"] = out

    rerank = profile.get("rerank")
    if isinstance(rerank, dict):
        projected["rerank"] = (
            {"enabled": _strict_bool(rerank["enabled"])} if "enabled" in rerank else {}
        )

    dependencies = profile.get("dependencies")
    if isinstance(dependencies, dict):
        deps: dict[str, Any] = {}
        for name in _DEPENDENCIES:
            entry = dependencies.get(name)
            if isinstance(entry, dict):
                dep: dict[str, Any] = {}
                if "available" in entry:
                    dep["available"] = _strict_bool(entry["available"])
                if "required_for" in entry:
                    dep["required_for"] = _members(entry["required_for"], _REQUIRED_FOR)
                deps[name] = dep
        projected["dependencies"] = deps

    if "missing_extras" in profile:
        projected["missing_extras"] = _members(profile["missing_extras"], _MISSING_EXTRAS)
    return projected
