"""Tests for RelevanceGate — surfacing eligibility decisions."""

from __future__ import annotations

from memtomem_stm.surfacing.config import SurfacingConfig, ToolSurfacingConfig
from memtomem_stm.surfacing.observability import SurfacingObservability
from memtomem_stm.surfacing.relevance import RateClaim, RelevanceGate


def _gate(**kwargs) -> RelevanceGate:
    return RelevanceGate(SurfacingConfig(**kwargs))


class TestRelevanceGateBasic:
    def test_returns_false_when_disabled(self):
        gate = _gate(enabled=False)
        assert not isinstance(gate.should_surface("s", "read_file", "query"), RateClaim)

    def test_returns_false_when_query_is_none(self):
        gate = _gate()
        assert not isinstance(gate.should_surface("s", "read_file", None), RateClaim)

    def test_returns_true_for_normal_read_tool(self):
        gate = _gate()
        assert isinstance(gate.should_surface("s", "read_file", "some query here"), RateClaim)


class TestRelevanceGateExclusions:
    def test_excluded_by_pattern(self):
        gate = _gate(exclude_tools=["*search*"])
        assert not isinstance(gate.should_surface("s", "search_code", "query text"), RateClaim)

    def test_excluded_by_prefixed_name(self):
        gate = _gate(exclude_tools=["gh__*"])
        assert not isinstance(gate.should_surface("gh", "list_repos", "query text"), RateClaim)

    def test_write_tool_heuristic(self):
        gate = _gate()
        for tool in ["write_file", "create_issue", "delete_branch", "push_commit", "send_message", "remove_label"]:
            assert not isinstance(
                gate.should_surface("s", tool, "query text"), RateClaim
            ), f"Should skip {tool}"

    def test_read_tool_passes(self):
        gate = _gate(cooldown_seconds=0.0)
        assert isinstance(
            gate.should_surface("s", "read_file", "query about reading files"), RateClaim
        )
        assert isinstance(
            gate.should_surface("s", "list_repos", "query about listing repos"), RateClaim
        )
        assert isinstance(
            gate.should_surface("s", "get_issue", "query about getting issues"), RateClaim
        )

    def test_write_pattern_matches_server_prefixed_full_name(self):
        # write_tool_patterns now matches the ``server__tool`` full name too
        # (symmetric with exclude_tools), so a server-qualified write pattern
        # gates that server's tool. Pre-fix it only matched the bare tool name,
        # so ``github__sync_*`` never fired.
        gate = _gate(write_tool_patterns=["github__sync_*"])
        assert not isinstance(
            gate.should_surface("github", "sync_state", "query text here"), RateClaim
        )
        # The same tool name on another server is not gated by the qualified
        # pattern (and bare ``sync_state`` is not a default write verb).
        assert isinstance(gate.should_surface("gitlab", "sync_state", "query text here"), RateClaim)


class TestRelevanceGatePerTool:
    def test_per_tool_disabled(self):
        gate = _gate(context_tools={"read_file": ToolSurfacingConfig(enabled=False)})
        assert not isinstance(gate.should_surface("s", "read_file", "query text"), RateClaim)

    def test_per_tool_enabled(self):
        gate = _gate(context_tools={"read_file": ToolSurfacingConfig(enabled=True)})
        assert isinstance(gate.should_surface("s", "read_file", "query text"), RateClaim)


class TestRelevanceGateRateLimit:
    def test_rate_limit_exceeded(self):
        gate = _gate(max_surfacings_per_minute=3)
        for i in range(3):
            q = f"different query {i}"
            assert isinstance(gate.should_surface("s", f"tool_{i}", q), RateClaim)
            gate.record_surfacing(q)
        # 4th should be rejected
        assert not isinstance(
            gate.should_surface("s", "tool_x", "another different query"), RateClaim
        )

    def test_configured_limit_above_200_is_enforced(self):
        gate = _gate(max_surfacings_per_minute=250, cooldown_seconds=0.0)

        for i in range(250):
            assert isinstance(
                gate.should_surface("s", f"tool_{i}", f"distinct query {i}"), RateClaim
            )

        assert not isinstance(
            gate.should_surface("s", "tool_over_limit", "one query too many"), RateClaim
        )


class TestRelevanceGateReleaseClaim:
    def test_release_returns_the_callers_own_claim(self):
        # A and B claim concurrently; A is refused downstream and refunds.
        # Popping the newest would hand back B's claim: the surviving slot
        # would then be A's OLDER timestamp, whose earlier expiry frees
        # capacity before B's real LTM attempt should allow. The claim token
        # makes the refund exact.
        gate = _gate(max_surfacings_per_minute=2, cooldown_seconds=0.0)
        claim_a = gate.should_surface("s", "t1", "query about topic A")
        claim_b = gate.should_surface("s", "t2", "completely different topic B query")
        assert isinstance(claim_a, RateClaim) and isinstance(claim_b, RateClaim)
        gate.release_claim(claim_a)
        assert list(gate._surfacing_timestamps) == [claim_b]

    def test_release_tolerates_an_already_pruned_claim(self, monkeypatch):
        # A claim can leave the deque before its caller refunds — pruned by
        # window expiry. The refund must
        # neither raise nor take someone else's slot with it. The clock is
        # frozen because two claims really can share one time.monotonic()
        # reading (Windows ticks at ~15.6ms, where CI caught exactly this):
        # the token must name the claim by identity, not by a timestamp
        # value that a *different* caller's live slot can collide with.
        from memtomem_stm.surfacing import relevance as relevance_module

        monkeypatch.setattr(relevance_module.time, "monotonic", lambda: 790.828)
        gate = _gate(max_surfacings_per_minute=2, cooldown_seconds=0.0)
        claim_a = gate.should_surface("s", "t1", "query about topic A")
        assert isinstance(claim_a, RateClaim)
        gate._surfacing_timestamps.clear()  # pruned by expiry
        claim_b = gate.should_surface("s", "t2", "completely different topic B query")
        gate.release_claim(claim_a)
        assert list(gate._surfacing_timestamps) == [claim_b]


class TestRelevanceGateCooldown:
    def test_duplicate_query_rejected(self):
        gate = _gate(cooldown_seconds=10.0)
        assert isinstance(gate.should_surface("s", "t1", "exact same query text"), RateClaim)
        gate.record_surfacing("exact same query text")
        assert not isinstance(gate.should_surface("s", "t2", "exact same query text"), RateClaim)

    def test_different_query_accepted(self):
        gate = _gate(cooldown_seconds=10.0)
        assert isinstance(gate.should_surface("s", "t1", "first query about topic A"), RateClaim)
        gate.record_surfacing("first query about topic A")
        assert isinstance(
            gate.should_surface("s", "t2", "completely different query about topic B"), RateClaim
        )


class TestRelevanceGateObservability:
    """The gate is responsible for recording its own 5 reject reasons.

    The engine records ``disabled`` and ``no_query`` instead — see the engine's
    skip-reason tests. Recording either of those here would double-count.
    """

    def _gate_with_obs(self, **kwargs) -> tuple[RelevanceGate, SurfacingObservability]:
        obs = SurfacingObservability()
        gate = RelevanceGate(SurfacingConfig(**kwargs), observability=obs)
        return gate, obs

    def test_excluded_tool_records_gate_excluded_tool(self):
        gate, obs = self._gate_with_obs(exclude_tools=["*search*"])
        assert gate.should_surface("s", "search_code", "query") == "gate_excluded_tool"
        assert obs.snapshot()["skip_reasons"]["search_code"] == {"gate_excluded_tool": 1}

    def test_write_tool_records_gate_write_tool(self):
        gate, obs = self._gate_with_obs()
        assert gate.should_surface("s", "write_file", "query") == "gate_write_tool"
        assert obs.snapshot()["skip_reasons"]["write_file"] == {"gate_write_tool": 1}

    def test_per_tool_disabled_records_gate_tool_disabled(self):
        gate, obs = self._gate_with_obs(
            context_tools={"read_file": ToolSurfacingConfig(enabled=False)}
        )
        assert gate.should_surface("s", "read_file", "query") == "gate_tool_disabled"
        assert obs.snapshot()["skip_reasons"]["read_file"] == {"gate_tool_disabled": 1}

    def test_rate_limit_records_gate_rate_limit(self):
        gate, obs = self._gate_with_obs(max_surfacings_per_minute=2)
        for i in range(2):
            assert isinstance(gate.should_surface("s", f"tool_{i}", f"query {i}"), RateClaim)
        assert gate.should_surface("s", "tool_x", "another query") == "gate_rate_limit"
        assert obs.snapshot()["skip_reasons"]["tool_x"] == {"gate_rate_limit": 1}

    def test_cooldown_records_gate_cooldown(self):
        gate, obs = self._gate_with_obs(cooldown_seconds=10.0)
        assert isinstance(gate.should_surface("s", "t1", "exact same query text"), RateClaim)
        gate.record_surfacing("exact same query text")
        assert gate.should_surface("s", "t2", "exact same query text") == "gate_cooldown"
        assert obs.snapshot()["skip_reasons"]["t2"] == {"gate_cooldown": 1}

    def test_disabled_does_not_record_at_gate(self):
        """``disabled`` is recorded by the engine before it calls the gate.
        If the gate also recorded it, we'd double-count."""
        gate, obs = self._gate_with_obs(enabled=False)
        assert gate.should_surface("s", "read_file", "query") == "disabled"
        assert obs.snapshot()["skip_reasons"] == {}

    def test_query_none_does_not_record_at_gate(self):
        """Same as ``disabled`` — engine records ``no_query`` upstream."""
        gate, obs = self._gate_with_obs()
        assert gate.should_surface("s", "read_file", None) == "no_query"
        assert obs.snapshot()["skip_reasons"] == {}


class TestJaccardSimilarity:
    def test_identical_strings(self):
        assert RelevanceGate._jaccard_similarity("hello world", "hello world") == 1.0

    def test_disjoint_strings(self):
        assert RelevanceGate._jaccard_similarity("hello world", "foo bar") == 0.0

    def test_partial_overlap(self):
        sim = RelevanceGate._jaccard_similarity("hello world foo", "hello world bar")
        assert 0.3 < sim < 0.8

    def test_empty_string(self):
        assert RelevanceGate._jaccard_similarity("", "hello") == 0.0
        assert RelevanceGate._jaccard_similarity("hello", "") == 0.0
