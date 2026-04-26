"""Tests for RelevanceGate — surfacing eligibility decisions."""

from __future__ import annotations

from memtomem_stm.surfacing.config import SurfacingConfig, ToolSurfacingConfig
from memtomem_stm.surfacing.observability import SurfacingObservability
from memtomem_stm.surfacing.relevance import RelevanceGate


def _gate(**kwargs) -> RelevanceGate:
    return RelevanceGate(SurfacingConfig(**kwargs))


class TestRelevanceGateBasic:
    def test_returns_false_when_disabled(self):
        gate = _gate(enabled=False)
        assert not gate.should_surface("s", "read_file", "query")

    def test_returns_false_when_query_is_none(self):
        gate = _gate()
        assert not gate.should_surface("s", "read_file", None)

    def test_returns_true_for_normal_read_tool(self):
        gate = _gate()
        assert gate.should_surface("s", "read_file", "some query here")


class TestRelevanceGateExclusions:
    def test_excluded_by_pattern(self):
        gate = _gate(exclude_tools=["*search*"])
        assert not gate.should_surface("s", "search_code", "query text")

    def test_excluded_by_prefixed_name(self):
        gate = _gate(exclude_tools=["gh__*"])
        assert not gate.should_surface("gh", "list_repos", "query text")

    def test_write_tool_heuristic(self):
        gate = _gate()
        for tool in ["write_file", "create_issue", "delete_branch", "push_commit", "send_message", "remove_label"]:
            assert not gate.should_surface("s", tool, "query text"), f"Should skip {tool}"

    def test_read_tool_passes(self):
        gate = _gate(cooldown_seconds=0.0)
        assert gate.should_surface("s", "read_file", "query about reading files")
        assert gate.should_surface("s", "list_repos", "query about listing repos")
        assert gate.should_surface("s", "get_issue", "query about getting issues")


class TestRelevanceGatePerTool:
    def test_per_tool_disabled(self):
        gate = _gate(context_tools={"read_file": ToolSurfacingConfig(enabled=False)})
        assert not gate.should_surface("s", "read_file", "query text")

    def test_per_tool_enabled(self):
        gate = _gate(context_tools={"read_file": ToolSurfacingConfig(enabled=True)})
        assert gate.should_surface("s", "read_file", "query text")


class TestRelevanceGateRateLimit:
    def test_rate_limit_exceeded(self):
        gate = _gate(max_surfacings_per_minute=3)
        for i in range(3):
            q = f"different query {i}"
            assert gate.should_surface("s", f"tool_{i}", q)
            gate.record_surfacing(q)
        # 4th should be rejected
        assert not gate.should_surface("s", "tool_x", "another different query")


class TestRelevanceGateCooldown:
    def test_duplicate_query_rejected(self):
        gate = _gate(cooldown_seconds=10.0)
        assert gate.should_surface("s", "t1", "exact same query text")
        gate.record_surfacing("exact same query text")
        assert not gate.should_surface("s", "t2", "exact same query text")

    def test_different_query_accepted(self):
        gate = _gate(cooldown_seconds=10.0)
        assert gate.should_surface("s", "t1", "first query about topic A")
        gate.record_surfacing("first query about topic A")
        assert gate.should_surface("s", "t2", "completely different query about topic B")


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
        assert not gate.should_surface("s", "search_code", "query")
        assert obs.snapshot()["skip_reasons"]["search_code"] == {"gate_excluded_tool": 1}

    def test_write_tool_records_gate_write_tool(self):
        gate, obs = self._gate_with_obs()
        assert not gate.should_surface("s", "write_file", "query")
        assert obs.snapshot()["skip_reasons"]["write_file"] == {"gate_write_tool": 1}

    def test_per_tool_disabled_records_gate_tool_disabled(self):
        gate, obs = self._gate_with_obs(
            context_tools={"read_file": ToolSurfacingConfig(enabled=False)}
        )
        assert not gate.should_surface("s", "read_file", "query")
        assert obs.snapshot()["skip_reasons"]["read_file"] == {"gate_tool_disabled": 1}

    def test_rate_limit_records_gate_rate_limit(self):
        gate, obs = self._gate_with_obs(max_surfacings_per_minute=2)
        for i in range(2):
            assert gate.should_surface("s", f"tool_{i}", f"query {i}")
        assert not gate.should_surface("s", "tool_x", "another query")
        assert obs.snapshot()["skip_reasons"]["tool_x"] == {"gate_rate_limit": 1}

    def test_cooldown_records_gate_cooldown(self):
        gate, obs = self._gate_with_obs(cooldown_seconds=10.0)
        assert gate.should_surface("s", "t1", "exact same query text")
        gate.record_surfacing("exact same query text")
        assert not gate.should_surface("s", "t2", "exact same query text")
        assert obs.snapshot()["skip_reasons"]["t2"] == {"gate_cooldown": 1}

    def test_disabled_does_not_record_at_gate(self):
        """``disabled`` is recorded by the engine before it calls the gate.
        If the gate also recorded it, we'd double-count."""
        gate, obs = self._gate_with_obs(enabled=False)
        assert not gate.should_surface("s", "read_file", "query")
        assert obs.snapshot()["skip_reasons"] == {}

    def test_query_none_does_not_record_at_gate(self):
        """Same as ``disabled`` — engine records ``no_query`` upstream."""
        gate, obs = self._gate_with_obs()
        assert not gate.should_surface("s", "read_file", None)
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
