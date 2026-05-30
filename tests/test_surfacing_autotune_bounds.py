"""AutoTuner configurable bounds + pinned-adjustment purge tests.

Companion to test_surfacing_autotune.py. Covers the work that made the
0.05 ceiling / 0.005 floor configurable + validated and that suppresses
persisted adjustments for operator-pinned tools.

Self-contained on purpose: builds its own ``MagicMock`` store and
``SurfacingConfig`` so it does not depend on the sibling file's fixtures.
"""

from __future__ import annotations

import logging

import pytest

from memtomem_stm.surfacing.config import SurfacingConfig, ToolSurfacingConfig
from memtomem_stm.surfacing.feedback import AutoTuner


def _store(*, neg=None, helpful=None, adjustments=None):
    from unittest.mock import MagicMock

    store = MagicMock()
    store.get_tool_negative_ratio.return_value = neg
    store.get_tool_helpful_ratio.return_value = helpful
    store.load_adjustments.return_value = dict(adjustments or {})
    return store


class TestConfigurableBounds:
    """The clamp values come from config, replacing the old hard-coded
    0.05 / 0.005 literals."""

    def test_ceiling_caps_the_raise(self):
        # Custom ceiling 0.04 < the legacy 0.05 — a raising tool stops at
        # 0.04, proving the literal is gone.
        cfg = SurfacingConfig(
            auto_tune_enabled=True,
            min_score=0.03,
            auto_tune_score_increment=0.005,
            auto_tune_score_ceiling=0.04,
        )
        tuner = AutoTuner(cfg, _store(neg=0.9, helpful=0.0))
        for _ in range(20):
            tuner.maybe_adjust("read_file")
        assert tuner.get_effective_min_score("read_file") == cfg.auto_tune_score_ceiling

    def test_floor_caps_the_lower(self):
        cfg = SurfacingConfig(
            auto_tune_enabled=True,
            min_score=0.03,
            auto_tune_score_increment=0.005,
            auto_tune_score_floor=0.02,
        )
        tuner = AutoTuner(cfg, _store(neg=0.0, helpful=0.95))
        for _ in range(20):
            tuner.maybe_adjust("read_file")
        assert tuner.get_effective_min_score("read_file") == cfg.auto_tune_score_floor

    def test_default_bounds_unchanged(self):
        cfg = SurfacingConfig()
        assert cfg.auto_tune_score_ceiling == 0.05
        assert cfg.auto_tune_score_floor == 0.005


class TestBoundsValidation:
    def test_unset_bounds_widen_to_admit_raised_min_score(self):
        cfg = SurfacingConfig(min_score=0.1)
        assert cfg.auto_tune_score_ceiling == 0.1
        assert cfg.auto_tune_score_floor == 0.005

    def test_unset_floor_widens_to_admit_lowered_min_score(self):
        cfg = SurfacingConfig(min_score=0.0)
        assert cfg.auto_tune_score_floor == 0.0

    def test_explicit_bound_on_wrong_side_rejected(self):
        with pytest.raises(ValueError, match="floor <= min_score <= ceiling"):
            SurfacingConfig(min_score=0.1, auto_tune_score_ceiling=0.05)

    def test_increment_larger_than_band_rejected(self):
        with pytest.raises(ValueError, match="exceeds the auto-tune band"):
            SurfacingConfig(auto_tune_score_increment=0.1)

    def test_increment_within_band_accepted(self):
        # 0.04 fits inside the default 0.045 band — the guard only rejects a
        # step bigger than the whole band, not the old 5-step rule.
        cfg = SurfacingConfig(auto_tune_score_increment=0.04)
        assert cfg.auto_tune_score_increment == 0.04


class TestPinPurge:
    """A persisted adjustment for a pinned tool is dropped from the
    in-memory map on construction (inert at read-time; would otherwise
    pollute snapshots/stats). The persisted row is left intact."""

    def test_pinned_tool_adjustment_suppressed(self):
        cfg = SurfacingConfig(context_tools={"read_file": ToolSurfacingConfig(min_score=0.2)})
        tuner = AutoTuner(cfg, _store(adjustments={"read_file": 0.04, "grep": 0.02}))
        assert "read_file" not in tuner.adjustments
        assert tuner.adjustments.get("grep") == 0.02

    def test_unpinned_tool_adjustment_kept(self):
        cfg = SurfacingConfig(context_tools={"read_file": ToolSurfacingConfig()})
        tuner = AutoTuner(cfg, _store(adjustments={"read_file": 0.04}))
        assert tuner.adjustments.get("read_file") == 0.04

    def test_purge_is_in_memory_only(self):
        # No save/delete round-trip on the store — suppression is in-memory.
        cfg = SurfacingConfig(context_tools={"read_file": ToolSurfacingConfig(min_score=0.2)})
        store = _store(adjustments={"read_file": 0.04})
        AutoTuner(cfg, store)
        store.save_adjustment.assert_not_called()

    def test_purge_logs_count(self, caplog):
        cfg = SurfacingConfig(context_tools={"read_file": ToolSurfacingConfig(min_score=0.2)})
        with caplog.at_level(logging.INFO, logger="memtomem_stm.surfacing.feedback"):
            AutoTuner(cfg, _store(adjustments={"read_file": 0.04}))
        assert any("suppressed 1 persisted adjustment" in r.message for r in caplog.records)
