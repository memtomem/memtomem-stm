"""Tests for IndexObservability counters + stm_index_stats render helper.

The end-to-end wire-in (manager → extract_and_store → counter increment)
is covered by integration tests in ``test_proxy_manager_*.py`` /
``test_extract_and_store*.py`` — this file is focused on the standalone
counter contract and the stats formatter, mirroring
``test_surfacing_observability.py``.
"""

from __future__ import annotations

from memtomem_stm.proxy.index_observability import (
    _NOOP_INDEX_OBSERVABILITY,
    IndexObservability,
)
from memtomem_stm.server import (
    _format_index_observability_sections,
    _ordered_tool_keys,
)


class TestIndexObservabilityCounters:
    def test_initial_snapshot_is_empty(self):
        obs = IndexObservability()
        snap = obs.snapshot()
        assert snap["any_call"] is False
        assert snap["attempts"] == {}
        assert snap["outcomes"] == {}

    def test_record_attempt_increments_per_tool_and_total(self):
        obs = IndexObservability()
        obs.record_attempt("read_file")
        obs.record_attempt("read_file")
        obs.record_attempt("search_code")
        snap = obs.snapshot()
        assert snap["any_call"] is True
        assert snap["attempts"]["read_file"] == 2
        assert snap["attempts"]["search_code"] == 1
        assert snap["attempts"]["__total__"] == 3

    def test_record_outcome_increments_per_tool_and_total(self):
        obs = IndexObservability()
        obs.record_outcome("read_file", "stored")
        obs.record_outcome("read_file", "stored")
        obs.record_outcome("read_file", "dedup_skip")
        obs.record_outcome("search_code", "extracted_zero_facts")
        snap = obs.snapshot()
        assert snap["outcomes"]["read_file"] == {"stored": 2, "dedup_skip": 1}
        assert snap["outcomes"]["search_code"] == {"extracted_zero_facts": 1}
        assert snap["outcomes"]["__total__"] == {
            "stored": 2,
            "dedup_skip": 1,
            "extracted_zero_facts": 1,
        }

    def test_all_four_outcome_labels_independent(self):
        """The 4-label split must keep each label as a separate slot —
        fusing ``extracted_zero_facts`` into ``stored=0`` would lose the
        signal "extraction fired but produced nothing", which is
        architecturally distinct from "facts existed but were duplicates"
        (``dedup_skip``) and from "facts existed and were stored"
        (``stored``)."""
        obs = IndexObservability()
        obs.record_outcome("t", "stored")
        obs.record_outcome("t", "dedup_skip")
        obs.record_outcome("t", "extracted_zero_facts")
        obs.record_outcome("t", "error")
        snap = obs.snapshot()
        assert snap["outcomes"]["t"] == {
            "stored": 1,
            "dedup_skip": 1,
            "extracted_zero_facts": 1,
            "error": 1,
        }

    def test_any_call_flips_on_attempt_alone(self):
        """An attempt with no outcome (e.g., extractor crashed before any
        outcome recorded — though current code always records ``error`` in
        that path) still flips ``any_call``, so ``stm_index_stats`` shows
        the section."""
        obs = IndexObservability()
        obs.record_attempt("t")
        assert obs.snapshot()["any_call"] is True

    def test_snapshot_returns_independent_copy(self):
        """Mutating the snapshot must not bleed into live counters."""
        obs = IndexObservability()
        obs.record_attempt("foo")
        obs.record_outcome("foo", "stored")
        snap = obs.snapshot()
        snap["attempts"]["foo"] = 999
        snap["outcomes"]["foo"]["stored"] = 999
        snap["outcomes"]["foo"]["bogus"] = 1
        snap2 = obs.snapshot()
        assert snap2["attempts"]["foo"] == 1
        assert snap2["outcomes"]["foo"] == {"stored": 1}

    def test_noop_record_does_not_raise(self):
        """The module-level no-op singleton lets ``extract_and_store``
        callers omit the ``observability`` kwarg without guarding every
        record call. Verifying the no-op never raises preserves that
        contract."""
        _NOOP_INDEX_OBSERVABILITY.record_attempt("t")
        _NOOP_INDEX_OBSERVABILITY.record_outcome("t", "stored")


class TestFormatIndexObservabilitySections:
    def test_empty_attempts_and_outcomes_renders_nothing(self):
        snap = {"any_call": False, "attempts": {}, "outcomes": {}}
        assert _format_index_observability_sections(snap, tool_filter=None) == []

    def test_full_snapshot_renders_both_sections(self):
        snap = {
            "any_call": True,
            "attempts": {"read_file": 5, "__total__": 5},
            "outcomes": {
                "read_file": {"stored": 3, "extracted_zero_facts": 2},
                "__total__": {"stored": 3, "extracted_zero_facts": 2},
            },
        }
        lines = _format_index_observability_sections(snap, tool_filter=None)
        joined = "\n".join(lines)
        assert "Attempts" in joined
        assert "read_file: 5" in joined
        assert "Outcomes" in joined
        assert "stored: 3" in joined
        assert "extracted_zero_facts: 2" in joined

    def test_tool_filter_restricts_per_tool_dicts_but_keeps_total(self):
        snap = {
            "any_call": True,
            "attempts": {"read_file": 3, "search_code": 7, "__total__": 10},
            "outcomes": {
                "read_file": {"stored": 1},
                "search_code": {"stored": 5, "dedup_skip": 2},
                "__total__": {"stored": 6, "dedup_skip": 2},
            },
        }
        lines = _format_index_observability_sections(snap, tool_filter="read_file")
        joined = "\n".join(lines)
        assert "read_file" in joined
        assert "search_code" not in joined
        # __total__ preserved so operator can compare
        assert "__total__" in joined

    def test_total_pinned_first_in_attempts(self):
        """Mirror of the surfacing-side regression: PascalCase tool names
        must not bury the aggregate row under sorted-ASCII order."""
        per_tool = {
            "ReadFile": 1,
            "__total__": 3,
            "alpha_tool": 2,
        }
        ordered = _ordered_tool_keys(per_tool)
        assert ordered == ["__total__", "ReadFile", "alpha_tool"]

    def test_descending_outcome_sort_within_a_tool(self):
        """Most frequent outcome should appear first under each tool, so
        operators scanning a long list see the dominant outcome first."""
        snap = {
            "any_call": True,
            "attempts": {"t": 60, "__total__": 60},
            "outcomes": {
                "t": {"stored": 3, "extracted_zero_facts": 50, "dedup_skip": 7},
                "__total__": {"stored": 3, "extracted_zero_facts": 50, "dedup_skip": 7},
            },
        }
        lines = _format_index_observability_sections(snap, tool_filter=None)
        joined = "\n".join(lines)
        i_zero = joined.index("extracted_zero_facts")
        i_dedup = joined.index("dedup_skip")
        i_stored = joined.index("stored")
        # The dominant outcome (50 zero) should appear first within the tool block.
        # Sort key is (-count, label) — equal counts fall back to label asc.
        # Here counts differ: 50 > 7 > 3 → zero, dedup, stored.
        # But __total__ appears first overall; check ordering inside the per-tool block by
        # finding the second occurrence (under ``  t:``).
        first_block_end = joined.index("  t:")
        sub = joined[first_block_end:]
        assert sub.index("extracted_zero_facts") < sub.index("dedup_skip") < sub.index("stored")
        # Sanity: ensure the labels show up in __total__ block too.
        assert i_zero >= 0 and i_dedup >= 0 and i_stored >= 0
