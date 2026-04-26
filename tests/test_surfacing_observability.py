"""Tests for SurfacingObservability counters + stm_surfacing_stats render helper.

The engine/gate integration with observability is covered in
``test_surfacing_engine.py::TestSurfacingObservabilityIntegration`` and
``test_relevance_gate.py::TestRelevanceGateObservability`` — this file is
focused on the standalone counter contract and the stats formatter.
"""

from __future__ import annotations

from memtomem_stm.server import _format_observability_sections
from memtomem_stm.surfacing.observability import SurfacingObservability


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
        assert "Skip reasons" in joined
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
        assert "Skip reasons" in joined
        assert "Cache" not in joined

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
