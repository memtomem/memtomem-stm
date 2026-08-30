"""Tests for the CompressionTuner auto-tuning analysis engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem_stm.proxy.compression_feedback_store import CompressionFeedbackStore
from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    ToolOverrideConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.metrics import CallMetrics
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.proxy.tuner import (
    CompressionTuner,
    TuningAction,
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

    def test_feedback_not_pooled_across_servers_sharing_a_tool_name(
        self, metrics_store: MetricsStore, feedback_store: CompressionFeedbackStore
    ):
        """Feedback joins on (server, tool), not tool name alone: with two
        upstreams exposing `search`, server_a's reports must not surface on
        server_b's profile — the tuner turns a profile's feedback into a
        per-server config recommendation (`mms tune --apply` writes it)."""
        _seed_metrics(metrics_store, "server_a", "search", 5)
        _seed_metrics(metrics_store, "server_b", "search", 5)
        for note in ("one", "two", "three"):
            feedback_store.record("server_a", "search", "truncated", note, None)

        tuner = CompressionTuner(metrics_store, feedback_store)
        by_server = {p.server: p for p in tuner.get_profiles()}
        assert by_server["server_a"].feedback_count == 3
        assert by_server["server_a"].feedback_dominant_kind == "truncated"
        assert by_server["server_b"].feedback_count == 0
        assert by_server["server_b"].feedback_dominant_kind is None


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

    def test_budget_advice_measures_against_the_global_default(self, metrics_store: MetricsStore):
        """H1 must not "increase" a budget to less than it already is (#926).

        The server omits `max_result_chars`, so calls run under the global
        `default_max_result_chars`. Reading the server's own default saw 8000
        where calls had 16000, and recommended 14400 as an increase — a cut.
        """
        cfg = ProxyConfig(
            upstream_servers={"srv": UpstreamServerConfig(prefix="test")},
            default_max_result_chars=16000,
        )
        _seed_metrics(metrics_store, "srv", "t1", 10, original_chars=18000, violation=True)
        tuner = CompressionTuner(metrics_store, config=cfg)
        recs = tuner.analyze()
        budget = [a for rec in recs for a in rec.actions if a.field == "max_result_chars"]
        # Pin the action itself, not just a property of however many there are:
        # `all()` over an empty list would pass while H1 stayed silent.
        assert len(budget) == 1
        assert budget[0].current == "16000"
        assert budget[0].recommended == str(max(int(18000 * 0.8), 16000 + 2000))

    def test_no_budget_advice_against_a_per_tool_token_budget(self, metrics_store: MetricsStore):
        """A per-tool `max_result_tokens` outranks the field `--apply` writes."""
        cfg = ProxyConfig(
            upstream_servers={
                "srv": UpstreamServerConfig(
                    prefix="test",
                    tool_overrides={
                        "t1": ToolOverrideConfig(max_result_tokens=400, chars_per_token=2.5)
                    },
                )
            }
        )
        _seed_metrics(metrics_store, "srv", "t1", 10, original_chars=18000, violation=True)
        tuner = CompressionTuner(metrics_store, config=cfg)
        recs = tuner.analyze()
        budget = [a for rec in recs for a in rec.actions if a.field == "max_result_chars"]
        assert budget == []

    def test_a_server_token_budget_does_not_suppress_advice(self, metrics_store: MetricsStore):
        """`--apply` writes a per-tool override, which CLEARS a server token budget.

        So the advice takes effect and must not be withheld — and it is measured
        against the chars that token budget currently yields (400 × 2.5).
        """
        cfg = ProxyConfig(
            upstream_servers={
                "srv": UpstreamServerConfig(
                    prefix="test", max_result_tokens=400, chars_per_token=2.5
                )
            }
        )
        _seed_metrics(metrics_store, "srv", "t1", 10, original_chars=18000, violation=True)
        tuner = CompressionTuner(metrics_store, config=cfg)
        recs = tuner.analyze()
        budget = [a for rec in recs for a in rec.actions if a.field == "max_result_chars"]
        assert len(budget) == 1
        assert budget[0].current == "1000"
        assert budget[0].recommended == str(max(int(18000 * 0.8), 1000 + 2000))

    def test_h2_skips_only_against_a_per_tool_token_budget(self, tmp_path: Path):
        """The same rule on the reduction side, which H1's cases cannot reach.

        H2 requires zero violations, so a violation-seeded test exercises H1
        alone and would stay green with H2's gate removed.
        """
        counter = iter(range(100))

        def _recs(server_cfg: UpstreamServerConfig) -> list[TuningAction]:
            store = MetricsStore(tmp_path / f"h2_{next(counter)}.db")
            store.initialize()
            try:
                _seed_metrics(store, "srv", "t1", 10, original_chars=2000, compressed_chars=1990)
                cfg = ProxyConfig(upstream_servers={"srv": server_cfg})
                recs = CompressionTuner(store, config=cfg).analyze()
                return [a for rec in recs for a in rec.actions if a.field == "max_result_chars"]
            finally:
                store.close()

        # 20000 tokens x 2.5 = 50000 chars, so a reduction to ~2200 is a real cut.
        server_token = _recs(
            UpstreamServerConfig(prefix="test", max_result_tokens=20000, chars_per_token=2.5)
        )
        assert len(server_token) == 1
        assert server_token[0].current == "50000"
        assert server_token[0].recommended == str(max(int(2000 * 1.1), 1000))

        per_tool_token = _recs(
            UpstreamServerConfig(
                prefix="test",
                tool_overrides={
                    "t1": ToolOverrideConfig(max_result_tokens=20000, chars_per_token=2.5)
                },
            )
        )
        assert per_tool_token == []

    def test_h4_budget_advice_obeys_the_same_rule(
        self, metrics_store: MetricsStore, feedback_store: CompressionFeedbackStore
    ):
        """H4's "missing_example" branch recommends a budget, so it is gated too.

        Its `compression` recommendations are not — only the budget field is
        outranked by a per-tool token budget.
        """
        cfg = ProxyConfig(
            upstream_servers={
                "srv": UpstreamServerConfig(
                    prefix="test",
                    tool_overrides={
                        "t1": ToolOverrideConfig(max_result_tokens=400, chars_per_token=2.5)
                    },
                )
            }
        )
        _seed_metrics(metrics_store, "srv", "t1", 10)
        for note in ("one", "two", "three"):
            feedback_store.record("srv", "t1", "missing_example", note, None)
        tuner = CompressionTuner(metrics_store, feedback_store, config=cfg)
        recs = tuner.analyze()
        assert [a for rec in recs for a in rec.actions if a.field == "max_result_chars"] == []

    def test_a_global_default_is_not_recommended_as_a_pin(self, metrics_store: MetricsStore):
        """A globally pinned strategy is already pinned (#926).

        The server omits ``compression``, so calls run under
        ``default_compression``. Reading only the per-server field saw ``auto``
        and recommended pinning the strategy the operator had already chosen,
        with a reason claiming AUTO was resolving at runtime.
        """
        cfg = ProxyConfig(
            upstream_servers={"srv": UpstreamServerConfig(prefix="test")},
            default_compression=CompressionStrategy.HYBRID,
        )
        _seed_metrics(metrics_store, "srv", "t1", 12, strategy="hybrid")
        tuner = CompressionTuner(metrics_store, config=cfg)
        recs = tuner.analyze()
        strat_actions = [a for rec in recs for a in rec.actions if a.field == "compression"]
        assert strat_actions == []

    def test_an_explicit_auto_still_recommends_pinning(self, metrics_store: MetricsStore):
        """The global default must not swallow a genuine AUTO.

        Typing ``compression: auto`` is a choice, so calls really do resolve
        per response and the pin advice is real — even when the global default
        names a strategy.
        """
        cfg = ProxyConfig(
            upstream_servers={
                "srv": UpstreamServerConfig(prefix="test", compression=CompressionStrategy.AUTO)
            },
            default_compression=CompressionStrategy.HYBRID,
        )
        _seed_metrics(metrics_store, "srv", "t1", 12, strategy="hybrid")
        tuner = CompressionTuner(metrics_store, config=cfg)
        recs = tuner.analyze()
        strat_actions = [a for rec in recs for a in rec.actions if a.field == "compression"]
        assert any(a.recommended == "hybrid" for a in strat_actions)

    def test_a_per_tool_override_beats_the_global_default(self, metrics_store: MetricsStore):
        """Tool override wins, so an overridden tool is pinned and not re-pinned."""
        cfg = ProxyConfig(
            upstream_servers={
                "srv": UpstreamServerConfig(
                    prefix="test",
                    tool_overrides={
                        "t1": ToolOverrideConfig(compression=CompressionStrategy.HYBRID)
                    },
                )
            },
            default_compression=CompressionStrategy.AUTO,
        )
        _seed_metrics(metrics_store, "srv", "t1", 12, strategy="hybrid")
        tuner = CompressionTuner(metrics_store, config=cfg)
        recs = tuner.analyze()
        strat_actions = [a for rec in recs for a in rec.actions if a.field == "compression"]
        assert strat_actions == []

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
