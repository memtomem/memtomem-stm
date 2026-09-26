"""#1062 invariant sweep: auto-tune never filters out a result both legs rank first.

Core rounds scores before STM sees them — ``round(score, 4)`` in the
structured ``mem_search`` JSON, ``f"{score:.2f}"`` in the compact text — so the
invariant is checked against the score Core actually delivers for the
reference ``sum(w) / (k + 1)``, not against the exact reference. Each case
drives the engine's own path: the batch cap (``_batch_score_ceiling``), a long
run of raise decisions (``AutoTuner.maybe_adjust``) and the filter read
(``_active_min_score``).

Known limit, not swept: a session without a usable ``runtime_profile`` is
stamped with the ``2/61`` baseline whatever Core's real weights are, so a
non-baseline Core without a profile is outside what any cap can know. Its
default ``min_score`` rests on the same assumption, and ``mms doctor``
reports the missing profile.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.feedback import AutoTuner

KS = [1, 5, 30, 60, 120, 1000]
WEIGHTS = [(1.0, 1.0), (0.8, 1.0), (0.5, 0.5), (1.0, 2.0), (0.3, 0.7), (2.0, 2.0), (0.01, 0.02)]


@dataclass
class _Result:
    score: float
    score_scale: str | None = None
    score_ceiling: float | None = None


def _store():
    store = MagicMock()
    store.get_tool_negative_ratio.return_value = 0.9
    store.get_tool_helpful_ratio.return_value = 0.0
    store.load_adjustments.return_value = {}
    count = iter(range(1, 100_000))
    store.get_feedback_count.side_effect = lambda _tool=None: next(count)
    return store


def _applied_after_raises(batch: list[_Result], min_score: float, stored: float | None) -> float:
    cfg = SurfacingConfig(
        enabled=True,
        auto_tune_enabled=True,
        min_score=min_score,
        auto_tune_score_floor=0.0,
    )
    engine = SurfacingEngine(config=cfg, mcp_adapter=AsyncMock())
    store = _store()
    if stored is not None:
        store.load_adjustments.return_value = {"read_file": stored}
    engine._auto_tuner = AutoTuner(cfg, store)
    ceiling = engine._batch_score_ceiling(batch)
    for _ in range(100):
        engine._auto_tuner.maybe_adjust("read_file", score_ceiling=ceiling)
    return engine._active_min_score("read_file", ceiling)


@pytest.mark.parametrize("stored", [None, 0.05])
@pytest.mark.parametrize(("k", "weights"), list(itertools.product(KS, WEIGHTS)))
def test_structured_stamped_top_result_always_passes(k, weights, stored):
    reference = sum(weights) / (k + 1)
    delivered = round(reference, 4)
    batch = [_Result(score=delivered, score_scale="rrf", score_ceiling=reference)]
    for min_score in (0.0, min(0.017, delivered)):
        applied = _applied_after_raises(batch, min_score, stored)
        assert delivered >= applied, (k, weights, min_score, stored, applied)


@pytest.mark.parametrize("stored", [None, 0.05])
@pytest.mark.parametrize("scale", [None, "some_future_scale"])
def test_compact_baseline_top_result_always_passes(scale, stored):
    delivered = float(f"{2 / 61:.2f}")
    batch = [_Result(score=delivered, score_scale=scale)]
    for min_score in (0.0, 0.017):
        applied = _applied_after_raises(batch, min_score, stored)
        assert delivered >= applied, (scale, min_score, stored, applied)


@pytest.mark.parametrize("stored", [None, 0.05])
def test_structured_unstamped_baseline_top_result_always_passes(stored):
    delivered = round(2 / 61, 4)
    batch = [_Result(score=delivered, score_scale="rrf")]
    applied = _applied_after_raises(batch, 0.017, stored)
    assert delivered >= applied
