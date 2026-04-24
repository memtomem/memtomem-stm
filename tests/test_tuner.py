"""Tests for the CompressionTuner auto-tuning analysis engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem_stm.proxy.compression_feedback_store import CompressionFeedbackStore
from memtomem_stm.proxy.config import CompressionStrategy, ProxyConfig, UpstreamServerConfig
from memtomem_stm.proxy.metrics import CallMetrics
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.proxy.tuner import (
    CompressionTuner,
    format_recommendations,
)


# ── helpers ─────────────────────────────────────────────────────────────


def _seed_metrics(
    store: MetricsStore,
    server: str,
    tool: str,
    count: int,
    *,
    original_chars: int = 5000,
    compressed_chars: int = 3000,
    strategy: str = "truncate",
    violation: bool = False,
) -> None:
    for _ in range(count):
        store.record(
            CallMetrics(
                server=server,
                tool=tool,
                original_chars=original_chars,
                compressed_chars=compressed_chars,
                cleaned_chars=original_chars,
                compression_strategy=strategy,
                ratio_violation=violation,
            )
        )


@pytest.fixture
def metrics_store(tmp_path: Path) -> MetricsStore:
    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    yield store
    store.close()


@pytest.fixture
def feedback_store(tmp_path: Path) -> CompressionFeedbackStore:
    store = CompressionFeedbackStore(tmp_path / "feedback.db")
    store.initialize()
    yield store
    store.close()


# ── profiles ────────────────────────────────────────────────────────────


class TestGetProfiles:
    def test_empty_store(self, metrics_store: MetricsStore):
        tuner = CompressionTuner(metrics_store)
        assert tuner.get_profiles() == []

    def test_shape(self, metrics_store: MetricsStore):
        _seed_metrics(metrics_store, "srv", "t1", 10, strategy="hybrid")
        tuner = CompressionTuner(metrics_store)
        profiles = tuner.get_profiles()
        assert len(profiles) == 1
        p = profiles[0]
        assert p.server == "srv"
        assert p.tool == "t1"
        assert p.call_count == 10
        assert p.dominant_strategy == "hybrid"
        assert p.feedback_count == 0
        assert p.feedback_dominant_kind is None

    def test_feedback_merged(
        self, metrics_store: MetricsStore, feedback_store: CompressionFeedbackStore
    ):
        _seed_metrics(metrics_store, "srv", "t1", 5)
        feedback_store.record("srv", "t1", "truncated", "missing data", None)
        feedback_store.record("srv", "t1", "truncated", "missing more", None)
        tuner = CompressionTuner(metrics_store, feedback_store)
        profiles = tuner.get_profiles()
        assert profiles[0].feedback_count == 2
        assert profiles[0].feedback_dominant_kind == "truncated"


# ── analyze ─────────────────────────────────────────────────────────────


class TestAnalyze:
    def test_empty_metrics_no_recommendations(self, metrics_store: MetricsStore):
        tuner = CompressionTuner(metrics_store)
        assert tuner.analyze() == []

    def test_below_min_calls_excluded(self, metrics_store: MetricsStore):
        _seed_metrics(metrics_store, "srv", "t1", 3, violation=True)
        tuner = CompressionTuner(metrics_store)
        assert tuner.analyze() == []

    def test_high_violation_rate_recommends_budget_increase(self, metrics_store: MetricsStore):
        # 6 calls: 4 violations → 67% violation rate
        _seed_metrics(
            metrics_store,
            "srv",
            "t1",
            4,
            original_chars=10000,
            compressed_chars=3000,
            violation=True,
        )
        _seed_metrics(metrics_store, "srv", "t1", 2, original_chars=10000, compressed_chars=8000)
        tuner = CompressionTuner(metrics_store)
        recs = tuner.analyze()
        assert len(recs) == 1
        actions = recs[0].actions
        budget_actions = [a for a in actions if a.field == "max_result_chars"]
        assert len(budget_actions) >= 1
        assert int(budget_actions[0].recommended) > 8000

    def test_over_generous_budget_recommends_reduction(self, metrics_store: MetricsStore):
        cfg = ProxyConfig(
            upstream_servers={"srv": UpstreamServerConfig(prefix="test", max_result_chars=50000)}
        )
        _seed_metrics(
            metrics_store,
            "srv",
            "t1",
            10,
            original_chars=2000,
            compressed_chars=1990,
            strategy="truncate",
        )
        tuner = CompressionTuner(metrics_store, config=cfg)
        recs = tuner.analyze()
        assert len(recs) >= 1
        budget_actions = [a for a in recs[0].actions if a.field == "max_result_chars"]
        assert len(budget_actions) >= 1
        assert int(budget_actions[0].recommended) < 50000

    def test_consistent_strategy_recommends_pinning(self, metrics_store: MetricsStore):
        cfg = ProxyConfig(
            upstream_servers={
                "srv": UpstreamServerConfig(prefix="test", compression=CompressionStrategy.AUTO)
            }
        )
        _seed_metrics(metrics_store, "srv", "t1", 12, strategy="hybrid")
        tuner = CompressionTuner(metrics_store, config=cfg)
        recs = tuner.analyze()
        strat_actions = [a for rec in recs for a in rec.actions if a.field == "compression"]
        assert any(a.recommended == "hybrid" for a in strat_actions)

    def test_feedback_truncated_recommends_strategy_change(
        self,
        metrics_store: MetricsStore,
        feedback_store: CompressionFeedbackStore,
    ):
        _seed_metrics(metrics_store, "srv", "t1", 10, strategy="truncate")
        for _ in range(4):
            feedback_store.record("srv", "t1", "truncated", "missing stuff", None)
        tuner = CompressionTuner(metrics_store, feedback_store)
        recs = tuner.analyze()
        strat_actions = [a for rec in recs for a in rec.actions if a.field == "compression"]
        assert any(a.recommended == "hybrid" for a in strat_actions)

    def test_confidence_levels(self, metrics_store: MetricsStore):
        # low confidence: 5-9 calls
        _seed_metrics(metrics_store, "srv", "low", 5, violation=True)
        # medium confidence: 10-19 calls
        _seed_metrics(metrics_store, "srv", "med", 12, violation=True)
        # high confidence: 20+ calls
        _seed_metrics(metrics_store, "srv", "hi", 25, violation=True)
        tuner = CompressionTuner(metrics_store)
        recs = tuner.analyze()
        by_tool = {r.tool: r.confidence for r in recs}
        assert by_tool["low"] == "low"
        assert by_tool["med"] == "medium"
        assert by_tool["hi"] == "high"

    def test_tool_filter(self, metrics_store: MetricsStore):
        _seed_metrics(metrics_store, "srv", "t1", 10, violation=True)
        _seed_metrics(metrics_store, "srv", "t2", 10, violation=True)
        tuner = CompressionTuner(metrics_store)
        recs = tuner.analyze(tool_filter="t1")
        assert all(r.tool == "t1" for r in recs)


# ── formatting ──────────────────────────────────────────────────────────


class TestFormatRecommendations:
    def test_no_recommendations(self):
        output = format_recommendations([], [], 24.0)
        assert "No recommendations" in output

    def test_with_recommendations(self, metrics_store: MetricsStore):
        _seed_metrics(metrics_store, "srv", "t1", 10, violation=True)
        tuner = CompressionTuner(metrics_store)
        profiles = tuner.get_profiles()
        recs = tuner.analyze()
        output = format_recommendations(recs, profiles, 24.0)
        assert "srv/t1" in output
        assert "Violation rate" in output
