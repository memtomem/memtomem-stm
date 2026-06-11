"""Tests for IndexObservability counters + stm_index_stats render helper.

The end-to-end wire-in is pinned by
``test_proxy_manager_pipeline.py::TestExtractAndStore::test_dedup_skips_duplicate``
(extract path) and
``test_proxy_manager_pipeline.py::TestAutoIndexResponse``
(auto_index path) — this file focuses on the standalone counter
contract and the stats formatter, mirroring
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

    def test_record_attempt_increments_per_tool_path_and_total(self):
        obs = IndexObservability()
        obs.record_attempt("read_file", "extract")
        obs.record_attempt("read_file", "extract")
        obs.record_attempt("read_file", "auto_index")
        obs.record_attempt("search_code", "auto_index")
        snap = obs.snapshot()
        assert snap["any_call"] is True
        assert snap["attempts"]["read_file"] == {"extract": 2, "auto_index": 1}
        assert snap["attempts"]["search_code"] == {"auto_index": 1}
        assert snap["attempts"]["__total__"] == {"extract": 2, "auto_index": 2}

    def test_record_attempt_path_separation(self):
        """``extract`` and ``auto_index`` must accumulate into distinct
        sub-keys so operators can see the per-path call distribution. A
        single ``record_attempt`` per path keeps each bucket independent —
        no cross-path bleed even when the same tool drove both."""
        obs = IndexObservability()
        obs.record_attempt("t", "extract")
        obs.record_attempt("t", "auto_index")
        snap = obs.snapshot()
        assert snap["attempts"]["t"] == {"extract": 1, "auto_index": 1}
        assert snap["attempts"]["__total__"] == {"extract": 1, "auto_index": 1}

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

    def test_all_outcome_labels_independent(self):
        """The 5-label split must keep each label as a separate slot —
        fusing ``extracted_zero_facts`` into ``stored=0`` would lose the
        signal "extraction fired but produced nothing", which is
        architecturally distinct from "facts existed but were duplicates"
        (``dedup_skip``), from "facts existed and were stored"
        (``stored``), and from "content was refused before any write"
        (``privacy_skip``, #453)."""
        obs = IndexObservability()
        obs.record_outcome("t", "stored")
        obs.record_outcome("t", "dedup_skip")
        obs.record_outcome("t", "extracted_zero_facts")
        obs.record_outcome("t", "privacy_skip")
        obs.record_outcome("t", "error")
        snap = obs.snapshot()
        assert snap["outcomes"]["t"] == {
            "stored": 1,
            "dedup_skip": 1,
            "extracted_zero_facts": 1,
            "privacy_skip": 1,
            "error": 1,
        }

    def test_outcomes_shared_across_paths(self):
        """``stored`` and ``error`` fire from both extract and auto_index;
        the outcome label set is shared (not per-path) so the raw mem_add
        count reads as a single ``outcomes[__total__][stored]`` regardless
        of which path drove the write. This test pins that contract:
        outcomes do not carry a path key, only attempts do."""
        obs = IndexObservability()
        # Auto-index style: one stored, one error.
        obs.record_attempt("t", "auto_index")
        obs.record_outcome("t", "stored")
        obs.record_attempt("t", "auto_index")
        obs.record_outcome("t", "error")
        # Extract style: one stored.
        obs.record_attempt("t", "extract")
        obs.record_outcome("t", "stored")
        snap = obs.snapshot()
        # Outcomes accumulate across both paths under flat labels.
        assert snap["outcomes"]["t"] == {"stored": 2, "error": 1}
        assert snap["outcomes"]["__total__"] == {"stored": 2, "error": 1}
        # Path attribution lives in attempts, not outcomes.
        assert snap["attempts"]["t"] == {"auto_index": 2, "extract": 1}

    def test_any_call_flips_on_attempt_alone(self):
        """An attempt with no outcome (e.g., extractor crashed before any
        outcome recorded — though current code always records ``error`` in
        that path) still flips ``any_call``, so ``stm_index_stats`` shows
        the section."""
        obs = IndexObservability()
        obs.record_attempt("t", "extract")
        assert obs.snapshot()["any_call"] is True

    def test_snapshot_returns_independent_copy(self):
        """Mutating the snapshot must not bleed into live counters."""
        obs = IndexObservability()
        obs.record_attempt("foo", "extract")
        obs.record_outcome("foo", "stored")
        snap = obs.snapshot()
        snap["attempts"]["foo"]["extract"] = 999
        snap["attempts"]["foo"]["bogus"] = 7
        snap["outcomes"]["foo"]["stored"] = 999
        snap["outcomes"]["foo"]["bogus"] = 1
        snap2 = obs.snapshot()
        assert snap2["attempts"]["foo"] == {"extract": 1}
        assert snap2["outcomes"]["foo"] == {"stored": 1}

    def test_noop_record_does_not_raise(self):
        """The module-level no-op singleton lets ``extract_and_store`` and
        ``auto_index_response`` callers omit the ``observability`` kwarg
        without guarding every record call. Verifying the no-op never
        raises preserves that contract."""
        _NOOP_INDEX_OBSERVABILITY.record_attempt("t", "extract")
        _NOOP_INDEX_OBSERVABILITY.record_attempt("t", "auto_index")
        _NOOP_INDEX_OBSERVABILITY.record_outcome("t", "stored")


class TestFormatIndexObservabilitySections:
    def test_empty_attempts_and_outcomes_renders_nothing(self):
        snap = {"any_call": False, "attempts": {}, "outcomes": {}}
        assert _format_index_observability_sections(snap, tool_filter=None) == []

    def test_full_snapshot_renders_both_sections(self):
        snap = {
            "any_call": True,
            "attempts": {
                "read_file": {"extract": 3, "auto_index": 2},
                "__total__": {"extract": 3, "auto_index": 2},
            },
            "outcomes": {
                "read_file": {"stored": 4, "extracted_zero_facts": 1},
                "__total__": {"stored": 4, "extracted_zero_facts": 1},
            },
        }
        lines = _format_index_observability_sections(snap, tool_filter=None)
        joined = "\n".join(lines)
        assert "Attempts" in joined
        assert "extract: 3" in joined
        assert "auto_index: 2" in joined
        assert "Outcomes" in joined
        assert "stored: 4" in joined
        assert "extracted_zero_facts: 1" in joined

    def test_tool_filter_restricts_per_tool_dicts_but_keeps_total(self):
        snap = {
            "any_call": True,
            "attempts": {
                "read_file": {"extract": 3},
                "search_code": {"auto_index": 7},
                "__total__": {"extract": 3, "auto_index": 7},
            },
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
            "ReadFile": {"extract": 1},
            "__total__": {"extract": 3},
            "alpha_tool": {"extract": 2},
        }
        ordered = _ordered_tool_keys(per_tool)
        assert ordered == ["__total__", "ReadFile", "alpha_tool"]

    def test_descending_outcome_sort_within_a_tool(self):
        """Most frequent outcome should appear first under each tool, so
        operators scanning a long list see the dominant outcome first."""
        snap = {
            "any_call": True,
            "attempts": {
                "t": {"extract": 60},
                "__total__": {"extract": 60},
            },
            "outcomes": {
                "t": {"stored": 3, "extracted_zero_facts": 50, "dedup_skip": 7},
                "__total__": {"stored": 3, "extracted_zero_facts": 50, "dedup_skip": 7},
            },
        }
        lines = _format_index_observability_sections(snap, tool_filter=None)
        joined = "\n".join(lines)
        # __total__ block renders first; per-tool ``t`` block follows. Slice
        # at the per-tool Outcomes header. NOTE: the sentinel ``"\n  t:\n"``
        # would collide if a future outcome label or path key starts with
        # "t" *and* has zero count (rendered as "  t: 0"). With current 4
        # outcome labels and 2 path keys none start with "t"; bump the
        # sentinel if a label name like "timeout" lands.
        sub = joined.split("\n  t:\n", 1)[1]
        # Sort key is (-count, label) — counts here are 50 > 7 > 3 so order
        # within the tool block is zero > dedup > stored.
        assert sub.index("extracted_zero_facts") < sub.index("dedup_skip") < sub.index("stored")
        # Sanity: labels show up in __total__ block too.
        assert "extracted_zero_facts" in joined
        assert "dedup_skip" in joined
        assert "stored" in joined

    def test_attempts_renders_per_path_breakdown(self):
        """Operators need to see extract vs auto_index call distribution
        inside each tool's attempts row, not just an aggregate count."""
        snap = {
            "any_call": True,
            "attempts": {
                "read_file": {"extract": 12, "auto_index": 30},
                "__total__": {"extract": 12, "auto_index": 30},
            },
            "outcomes": {},
        }
        lines = _format_index_observability_sections(snap, tool_filter=None)
        joined = "\n".join(lines)
        # Per-path keys appear under each tool block.
        assert "extract: 12" in joined
        assert "auto_index: 30" in joined
        # Sort-by-count (descending): auto_index (30) before extract (12).
        assert joined.index("auto_index: 30") < joined.index("extract: 12")
