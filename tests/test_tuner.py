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
    STRATEGY_PIN_THRESHOLD,
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
    strategy: str | None = "truncate",
    violation: bool = False,
    is_error: bool = False,
) -> None:
    """Record ``count`` identical calls.

    ``strategy=None`` seeds NULL-strategy rows; ``is_error=True`` seeds rows
    the store excludes from ``avg_ratio`` (and therefore from the population
    H2 rests on).
    """
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
                is_error=is_error,
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
        assert p.dominant_strategy_count == 10
        assert p.strategy_count == 10
        assert p.dominant_strategy_share == 1.0
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

    def test_plurality_below_threshold_does_not_recommend_pinning(
        self, metrics_store: MetricsStore
    ):
        """A plurality is not dominance (#928).

        7 of 10 calls resolve `hybrid` — a plurality, as in the issue's 7 of
        12, at a higher share (70% against 58.3%) that is still short of the
        threshold.  H3 used to fire on the mere presence of a dominant label,
        so it recommended pinning a strategy that 30% of calls do not want,
        and said ">80% of calls" to justify it.
        """
        cfg = ProxyConfig(
            upstream_servers={
                "srv": UpstreamServerConfig(prefix="test", compression=CompressionStrategy.AUTO)
            }
        )
        _seed_metrics(metrics_store, "srv", "t1", 7, strategy="hybrid")
        _seed_metrics(metrics_store, "srv", "t1", 3, strategy="truncate")
        tuner = CompressionTuner(metrics_store, config=cfg)
        recs = tuner.analyze()
        strat_actions = [a for rec in recs for a in rec.actions if a.field == "compression"]
        assert strat_actions == []

    def test_dominant_above_threshold_reports_the_measured_share(
        self, metrics_store: MetricsStore
    ):
        """The reason states what was measured, not the threshold (#928)."""
        cfg = ProxyConfig(
            upstream_servers={
                "srv": UpstreamServerConfig(prefix="test", compression=CompressionStrategy.AUTO)
            }
        )
        _seed_metrics(metrics_store, "srv", "t1", 9, strategy="hybrid")
        _seed_metrics(metrics_store, "srv", "t1", 1, strategy="truncate")
        tuner = CompressionTuner(metrics_store, config=cfg)
        recs = tuner.analyze()
        strat_actions = [a for rec in recs for a in rec.actions if a.field == "compression"]
        assert len(strat_actions) == 1
        assert strat_actions[0].recommended == "hybrid"
        assert "9 of 10 calls with a recorded strategy" in strat_actions[0].reason
        assert "90.0%" in strat_actions[0].reason

    def test_a_share_exactly_at_the_threshold_does_not_fire(self, metrics_store: MetricsStore):
        """`STRATEGY_PIN_THRESHOLD` is a floor to exceed, not to reach (#928)."""
        cfg = ProxyConfig(
            upstream_servers={
                "srv": UpstreamServerConfig(prefix="test", compression=CompressionStrategy.AUTO)
            }
        )
        _seed_metrics(metrics_store, "srv", "t1", 8, strategy="hybrid")
        _seed_metrics(metrics_store, "srv", "t1", 2, strategy="truncate")
        tuner = CompressionTuner(metrics_store, config=cfg)
        assert tuner.get_profiles()[0].dominant_strategy_share == STRATEGY_PIN_THRESHOLD
        recs = tuner.analyze()
        strat_actions = [a for rec in recs for a in rec.actions if a.field == "compression"]
        assert strat_actions == []

    def test_calls_that_resolved_no_strategy_stay_out_of_the_denominator(
        self, metrics_store: MetricsStore
    ):
        """The share is over calls with a recorded strategy, not `call_count` (#928).

        A call that recorded no strategy cannot be in the numerator, so
        counting it in the denominator would withhold a pin the resolving
        calls do support.  Here 9 of 11 resolving calls are `hybrid` (82%,
        fires); over all 13 calls it would read 69% and stay silent.
        """
        cfg = ProxyConfig(
            upstream_servers={
                "srv": UpstreamServerConfig(prefix="test", compression=CompressionStrategy.AUTO)
            }
        )
        _seed_metrics(metrics_store, "srv", "t1", 9, strategy="hybrid")
        _seed_metrics(metrics_store, "srv", "t1", 2, strategy="truncate")
        _seed_metrics(metrics_store, "srv", "t1", 2, strategy=None)
        tuner = CompressionTuner(metrics_store, config=cfg)
        p = tuner.get_profiles()[0]
        assert p.call_count == 13
        assert p.strategy_count == 11
        recs = tuner.analyze()
        strat_actions = [a for rec in recs for a in rec.actions if a.field == "compression"]
        assert len(strat_actions) == 1
        assert "9 of 11 calls with a recorded strategy" in strat_actions[0].reason

    def test_the_volume_gate_counts_the_same_calls_as_the_share(
        self, metrics_store: MetricsStore
    ):
        """One resolved call is not evidence, whatever share it holds (#928).

        Gating volume on `call_count` while measuring the share over
        calls with a recorded strategy lets a single recorded call among nine that
        recorded none report a 100% share at medium confidence.
        """
        cfg = ProxyConfig(
            upstream_servers={
                "srv": UpstreamServerConfig(prefix="test", compression=CompressionStrategy.AUTO)
            }
        )
        _seed_metrics(metrics_store, "srv", "t1", 1, strategy="hybrid")
        _seed_metrics(metrics_store, "srv", "t1", 9, strategy=None)
        tuner = CompressionTuner(metrics_store, config=cfg)
        p = tuner.get_profiles()[0]
        assert p.call_count == 10
        assert p.strategy_count == 1
        assert p.dominant_strategy_share == 1.0
        recs = tuner.analyze()
        strat_actions = [a for rec in recs for a in rec.actions if a.field == "compression"]
        assert strat_actions == []

    def test_confidence_follows_the_calls_the_action_rests_on(
        self, metrics_store: MetricsStore
    ):
        """A label read off `call_count` overstates an H3-only recommendation.

        10 resolved calls and 15 that recorded none: the sole action rests on
        10 observations, but `call_count` of 25 would label it `high`.
        """
        cfg = ProxyConfig(
            upstream_servers={
                "srv": UpstreamServerConfig(prefix="test", compression=CompressionStrategy.AUTO)
            }
        )
        _seed_metrics(metrics_store, "srv", "t1", 10, strategy="hybrid")
        _seed_metrics(metrics_store, "srv", "t1", 15, strategy=None)
        tuner = CompressionTuner(metrics_store, config=cfg)
        p = tuner.get_profiles()[0]
        assert p.call_count == 25
        assert p.strategy_count == 10
        recs = tuner.analyze()
        assert len(recs) == 1
        assert [a.field for a in recs[0].actions] == ["compression"]
        assert recs[0].confidence == "medium"

    def test_h2_confidence_follows_the_rows_avg_ratio_reads(self, metrics_store: MetricsStore):
        """`avg_ratio` is an average over rows H2's label used to ignore (#934).

        The store averages only rows with `cleaned_chars > 0 AND is_error = 0`.
        One such row among 24 errors reported `high` on an average over a
        single response.
        """
        cfg = ProxyConfig(
            upstream_servers={"srv": UpstreamServerConfig(prefix="test", max_result_chars=50000)}
        )
        _seed_metrics(
            metrics_store,
            "srv",
            "t1",
            1,
            original_chars=2000,
            compressed_chars=1990,
            strategy=None,
        )
        _seed_metrics(
            metrics_store,
            "srv",
            "t1",
            24,
            original_chars=2000,
            compressed_chars=2000,
            strategy=None,
            is_error=True,
        )
        tuner = CompressionTuner(metrics_store, config=cfg)
        p = tuner.get_profiles()[0]
        assert p.call_count == 25
        assert p.ratio_count == 1
        recs = tuner.analyze()
        assert len(recs) == 1
        assert [a.field for a in recs[0].actions] == ["max_result_chars"]
        assert "over 1 of 25 calls" in recs[0].actions[0].reason
        assert recs[0].confidence == "low"

    def test_h2_confidence_rises_with_the_eligible_rows(self, metrics_store: MetricsStore):
        """Ten eligible rows among 25 calls are medium, not high."""
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
            strategy=None,
        )
        _seed_metrics(
            metrics_store,
            "srv",
            "t1",
            15,
            original_chars=2000,
            compressed_chars=2000,
            strategy=None,
            is_error=True,
        )
        tuner = CompressionTuner(metrics_store, config=cfg)
        p = tuner.get_profiles()[0]
        assert p.call_count == 25
        assert p.ratio_count == 10
        recs = tuner.analyze()
        assert len(recs) == 1
        assert [a.field for a in recs[0].actions] == ["max_result_chars"]
        assert recs[0].confidence == "medium"

    def test_h4_confidence_follows_the_feedback_reports(
        self,
        metrics_store: MetricsStore,
        feedback_store: CompressionFeedbackStore,
    ):
        """A feedback recommendation rests on reports, not on calls (#934).

        Three reports against 25 calls read as `high`.  The thresholds are
        reused deliberately: the label states how many observations the number
        in the reason rests on, whatever kind of observation it is.
        """
        for tool, reports in (("low", 3), ("med", 10), ("hi", 20)):
            # NULL strategies keep H3 silent; the default 0.6 ratio keeps H2
            # silent, so each recommendation carries the H4 action alone.
            _seed_metrics(metrics_store, "srv", tool, 25, strategy=None)
            for _ in range(reports):
                feedback_store.record("srv", tool, "truncated", "missing stuff", None)
        tuner = CompressionTuner(metrics_store, feedback_store)
        recs = tuner.analyze()
        by_tool = {r.tool: r for r in recs}
        assert set(by_tool) == {"low", "med", "hi"}
        for rec in by_tool.values():
            assert [a.field for a in rec.actions] == ["compression"]
        assert by_tool["low"].confidence == "low"
        assert by_tool["med"].confidence == "medium"
        assert by_tool["hi"].confidence == "high"

    def test_confidence_is_the_smallest_population_among_the_actions(
        self,
        metrics_store: MetricsStore,
        feedback_store: CompressionFeedbackStore,
    ):
        """One label covers every action, so the weakest evidence sets it.

        H1 rests on all 25 calls, H4 on 3 feedback reports; a `high` here
        would overstate the feedback action it also covers.
        """
        _seed_metrics(metrics_store, "srv", "t1", 25, violation=True, strategy=None)
        for _ in range(3):
            feedback_store.record("srv", "t1", "truncated", "missing stuff", None)
        tuner = CompressionTuner(metrics_store, feedback_store)
        recs = tuner.analyze()
        assert len(recs) == 1
        assert {a.field for a in recs[0].actions} == {"max_result_chars", "compression"}
        assert recs[0].confidence == "low"

    def test_a_fallback_label_is_never_recommended_as_a_pin(self, metrics_store: MetricsStore):
        """A degraded call records the path it took, which no config accepts.

        `compression_strategy` holds the strategy a call *ran under*, and a
        fallback records `hybrid→progressive_fallback`.  Recommending that as
        a `compression` value is advice the operator cannot apply — `mms tune
        --apply` fails schema validation on it.
        """
        cfg = ProxyConfig(
            upstream_servers={
                "srv": UpstreamServerConfig(prefix="test", compression=CompressionStrategy.AUTO)
            }
        )
        _seed_metrics(metrics_store, "srv", "t1", 12, strategy="hybrid→progressive_fallback")
        tuner = CompressionTuner(metrics_store, config=cfg)
        assert tuner.get_profiles()[0].dominant_strategy_share == 1.0
        recs = tuner.analyze()
        strat_actions = [a for rec in recs for a in rec.actions if a.field == "compression"]
        assert strat_actions == []

    def test_the_reason_carries_counts_a_rounded_percent_would_blur(
        self, metrics_store: MetricsStore
    ):
        """Near the boundary a rounded percent reads as the threshold itself.

        81 of 101 is 80.198%, which clears the strict gate but renders as
        "80%" at zero decimals — the gate and its stated reason would disagree
        again, which is the whole defect #928 is about.
        """
        cfg = ProxyConfig(
            upstream_servers={
                "srv": UpstreamServerConfig(prefix="test", compression=CompressionStrategy.AUTO)
            }
        )
        _seed_metrics(metrics_store, "srv", "t1", 81, strategy="hybrid")
        _seed_metrics(metrics_store, "srv", "t1", 20, strategy="truncate")
        tuner = CompressionTuner(metrics_store, config=cfg)
        recs = tuner.analyze()
        strat_actions = [a for rec in recs for a in rec.actions if a.field == "compression"]
        assert len(strat_actions) == 1
        reason = strat_actions[0].reason
        assert "81 of 101 calls with a recorded strategy" in reason
        assert "80.2%" in reason

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
