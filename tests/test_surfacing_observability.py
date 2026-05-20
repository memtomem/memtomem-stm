"""Tests for SurfacingObservability counters + stm_surfacing_stats render helper.

The engine/gate integration with observability is covered in
``test_surfacing_engine.py::TestSurfacingObservabilityIntegration`` and
``test_relevance_gate.py::TestRelevanceGateObservability`` — this file is
focused on the standalone counter contract and the stats formatter.
"""

from __future__ import annotations

from typing import get_args

from memtomem_stm.server import _format_observability_sections, _ordered_tool_keys
from memtomem_stm.surfacing.observability import (
    FAULT_SKIP_REASONS,
    HEALTHY_SKIP_REASONS,
    SkipReason,
    SurfacingObservability,
)


class TestObservabilityCounters:
    def test_initial_snapshot_is_empty(self):
        obs = SurfacingObservability()
        snap = obs.snapshot()
        assert snap["any_call"] is False
        assert snap["skip_reasons"] == {}
        assert snap["outcomes"] == {}
        assert snap["cache"] == {}

    def test_record_skip_increments_per_tool_and_total(self):
        obs = SurfacingObservability()
        obs.record_skip("read_file", "gate_cooldown")
        obs.record_skip("read_file", "gate_cooldown")
        obs.record_skip("read_file", "response_too_short")
        snap = obs.snapshot()
        assert snap["any_call"] is True
        assert snap["skip_reasons"]["read_file"] == {
            "gate_cooldown": 2,
            "response_too_short": 1,
        }
        assert snap["skip_reasons"]["__total__"] == {
            "gate_cooldown": 2,
            "response_too_short": 1,
        }

    def test_record_outcome_increments_per_tool_and_total(self):
        obs = SurfacingObservability()
        obs.record_outcome("search", "surfaced_cache_miss")
        obs.record_outcome("search", "surfaced_cache_hit")
        obs.record_outcome("read_file", "error_timeout")
        snap = obs.snapshot()
        assert snap["outcomes"]["search"] == {
            "surfaced_cache_miss": 1,
            "surfaced_cache_hit": 1,
        }
        assert snap["outcomes"]["read_file"] == {"error_timeout": 1}
        assert snap["outcomes"]["__total__"] == {
            "surfaced_cache_miss": 1,
            "surfaced_cache_hit": 1,
            "error_timeout": 1,
        }

    def test_record_cache_tracks_hit_and_miss_independently(self):
        obs = SurfacingObservability()
        for _ in range(3):
            obs.record_cache("hit")
        for _ in range(7):
            obs.record_cache("miss")
        snap = obs.snapshot()
        assert snap["cache"] == {"hit": 3, "miss": 7}

    def test_snapshot_returns_independent_copy(self):
        """Mutating the snapshot must not bleed into the live counters —
        otherwise downstream consumers (stm_surfacing_stats) could corrupt
        state by accident on a typo."""
        obs = SurfacingObservability()
        obs.record_skip("foo", "gate_rate_limit")
        snap = obs.snapshot()
        snap["skip_reasons"]["foo"]["gate_rate_limit"] = 999
        snap["skip_reasons"]["foo"]["bogus"] = 1
        # Live counters unchanged
        snap2 = obs.snapshot()
        assert snap2["skip_reasons"]["foo"] == {"gate_rate_limit": 1}


class TestFormatObservabilitySections:
    def test_empty_snapshot_renders_nothing(self):
        snap = {"any_call": False, "skip_reasons": {}, "outcomes": {}, "cache": {}}
        assert _format_observability_sections(snap, tool_filter=None) == []

    def test_full_snapshot_renders_all_three_sections(self):
        snap = {
            "any_call": True,
            "skip_reasons": {
                "read_file": {"response_too_short": 12, "gate_cooldown": 3},
                "__total__": {"response_too_short": 12, "gate_cooldown": 3},
            },
            "outcomes": {
                "read_file": {"surfaced_cache_miss": 5, "surfaced_cache_hit": 2},
                "__total__": {"surfaced_cache_miss": 5, "surfaced_cache_hit": 2},
            },
            "cache": {"hit": 2, "miss": 5},
        }
        lines = _format_observability_sections(snap, tool_filter=None)
        joined = "\n".join(lines)
        # Both reasons are healthy → only the Healthy skips subsection
        # renders, fault subsection is omitted entirely (#362).
        assert "Healthy skips" in joined
        assert "Fault skips" not in joined
        assert "response_too_short: 12" in joined
        assert "Outcomes" in joined
        assert "surfaced_cache_miss: 5" in joined
        assert "Cache" in joined
        # 2/(2+5) = 28.6%
        assert "hit ratio 28.6%" in joined

    def test_tool_filter_restricts_per_tool_dicts_but_keeps_total(self):
        snap = {
            "any_call": True,
            "skip_reasons": {
                "read_file": {"gate_cooldown": 1},
                "search_code": {"gate_rate_limit": 5},
                "__total__": {"gate_cooldown": 1, "gate_rate_limit": 5},
            },
            "outcomes": {
                "read_file": {"surfaced_cache_miss": 1},
                "search_code": {"surfaced_cache_hit": 2},
                "__total__": {"surfaced_cache_miss": 1, "surfaced_cache_hit": 2},
            },
            "cache": {"hit": 2, "miss": 1},
        }
        lines = _format_observability_sections(snap, tool_filter="read_file")
        joined = "\n".join(lines)
        assert "read_file" in joined
        assert "search_code" not in joined
        # __total__ is still present so the operator can compare
        assert "__total__" in joined

    def test_cache_section_omitted_when_no_lookups(self):
        snap = {
            "any_call": True,
            "skip_reasons": {"read_file": {"disabled": 3}},
            "outcomes": {},
            "cache": {},
        }
        lines = _format_observability_sections(snap, tool_filter=None)
        joined = "\n".join(lines)
        assert "Healthy skips" in joined
        assert "Cache" not in joined

    def test_total_pinned_first_regardless_of_ascii_order(self):
        """Reviewer feedback on PR #256: ``sorted()`` happens to put
        ``__total__`` first because ``_`` (0x5F) sorts before lowercase
        letters (0x61+), but a PascalCase tool name like ``ReadFile``
        (0x52 starting) would sort before ``__total__`` and bury the
        aggregate row mid-list. ``_ordered_tool_keys`` must pin
        ``__total__`` first explicitly."""
        per_tool = {
            "ReadFile": {"disabled": 1},
            "__total__": {"disabled": 3},
            "alpha_tool": {"disabled": 2},
        }
        ordered = _ordered_tool_keys(per_tool)
        assert ordered == ["__total__", "ReadFile", "alpha_tool"]

    def test_ordered_tool_keys_no_total_returns_sorted(self):
        """When the per-tool dict has no aggregate row (zero-traffic edge),
        the helper must not synthesize one — just return the sorted keys."""
        per_tool = {"b": {}, "a": {}}
        assert _ordered_tool_keys(per_tool) == ["a", "b"]

    def test_descending_sort_within_a_tool(self):
        """The most frequent reason should appear first under each tool —
        operators scanning a long list should see the dominant skip first."""
        snap = {
            "any_call": True,
            "skip_reasons": {
                "read_file": {
                    "gate_cooldown": 2,
                    "response_too_short": 50,
                    "gate_rate_limit": 10,
                },
            },
            "outcomes": {},
            "cache": {},
        }
        lines = _format_observability_sections(snap, tool_filter=None)
        joined = "\n".join(lines)
        i_short = joined.index("response_too_short")
        i_rl = joined.index("gate_rate_limit")
        i_cd = joined.index("gate_cooldown")
        assert i_short < i_rl < i_cd


class TestSkipReasonCategorization:
    """Pins the healthy / fault partition introduced in #362 (#351 part 2).

    Without these tests, a new ``SkipReason`` enum value added without a
    category assignment would silently drop out of ``stm_surfacing_stats``
    output — the formatter only renders reasons that match one of the two
    sets, so an unclassified reason becomes invisible.
    """

    def test_categorization_is_exhaustive_and_disjoint(self):
        """Every ``SkipReason`` Literal member must appear in exactly one
        of the two category sets. Failure here means a recently-added enum
        value was not classified — fix observability.py rather than this
        test."""
        all_reasons = set(get_args(SkipReason))
        union = HEALTHY_SKIP_REASONS | FAULT_SKIP_REASONS
        # No reason is uncategorized (would silently drop from rendering).
        assert union == all_reasons, f"unclassified SkipReason values: {all_reasons - union}"
        # No reason is in both sets (would render twice with the same count).
        overlap = HEALTHY_SKIP_REASONS & FAULT_SKIP_REASONS
        assert overlap == set(), f"SkipReason values classified twice: {overlap}"

    def test_fault_skips_render_under_fault_subsection(self):
        """A snapshot with only LTM / circuit reasons renders the Fault
        skips subsection and omits the Healthy skips header — the whole
        point of the split is so operators reading the rendered output
        can tell at a glance whether the bypass count is healthy backoff
        or degraded LTM."""
        snap = {
            "any_call": True,
            "skip_reasons": {
                "read_file": {"ltm_unavailable": 23, "circuit_open": 4},
                "__total__": {"ltm_unavailable": 23, "circuit_open": 4},
            },
            "outcomes": {},
            "cache": {},
        }
        lines = _format_observability_sections(snap, tool_filter=None)
        joined = "\n".join(lines)
        assert "Fault skips" in joined
        # No Healthy section when zero healthy skips recorded.
        assert "Healthy skips" not in joined
        assert "ltm_unavailable: 23" in joined
        assert "circuit_open: 4" in joined

    def test_mixed_skips_render_both_subsections_independently(self):
        """When a tool has skips in both categories the formatter must
        emit two subsections — that's the whole point of the split. A
        1000/1000 healthy/fault row previously looked identical to a
        2000/0 row in the count-sorted single list."""
        snap = {
            "any_call": True,
            "skip_reasons": {
                "tool_a": {
                    "gate_cooldown": 412,
                    "no_results_score": 88,
                    "ltm_unavailable": 23,
                    "circuit_open": 2,
                },
                "__total__": {
                    "gate_cooldown": 412,
                    "no_results_score": 88,
                    "ltm_unavailable": 23,
                    "circuit_open": 2,
                },
            },
            "outcomes": {},
            "cache": {},
        }
        lines = _format_observability_sections(snap, tool_filter=None)
        joined = "\n".join(lines)
        # Both headers render.
        assert "Healthy skips" in joined
        assert "Fault skips" in joined
        # Headers appear in the expected order (healthy before fault) so
        # operators read the "normal backoff" volume before the "something
        # is wrong" volume rather than the other way around.
        assert joined.index("Healthy skips") < joined.index("Fault skips")
        # The Healthy subsection contains only healthy reasons, the Fault
        # subsection only fault reasons — never the inverse.
        healthy_block = joined[joined.index("Healthy skips") : joined.index("Fault skips")]
        fault_block = joined[joined.index("Fault skips") :]
        assert "gate_cooldown: 412" in healthy_block
        assert "no_results_score: 88" in healthy_block
        assert "ltm_unavailable" not in healthy_block
        assert "circuit_open" not in healthy_block
        assert "ltm_unavailable: 23" in fault_block
        assert "circuit_open: 2" in fault_block
        assert "gate_cooldown" not in fault_block
        assert "no_results_score" not in fault_block

    def test_tool_with_only_one_category_skipped_from_the_other_subsection(self):
        """When one tool has only healthy skips and another has only fault
        skips, each subsection lists only the tools that contributed to
        it — empty per-tool rows ``  tool_b:`` followed by nothing would
        be visual noise."""
        snap = {
            "any_call": True,
            "skip_reasons": {
                "healthy_only": {"gate_cooldown": 5},
                "fault_only": {"ltm_unavailable": 7},
                "__total__": {"gate_cooldown": 5, "ltm_unavailable": 7},
            },
            "outcomes": {},
            "cache": {},
        }
        lines = _format_observability_sections(snap, tool_filter=None)
        joined = "\n".join(lines)
        healthy_block = joined[joined.index("Healthy skips") : joined.index("Fault skips")]
        fault_block = joined[joined.index("Fault skips") :]
        # healthy_only appears only in the Healthy subsection.
        assert "healthy_only:" in healthy_block
        assert "healthy_only:" not in fault_block
        # fault_only appears only in the Fault subsection.
        assert "fault_only:" in fault_block
        assert "fault_only:" not in healthy_block
        # __total__ appears in both (split by category contents).
        assert "__total__:" in healthy_block
        assert "__total__:" in fault_block
