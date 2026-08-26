"""Tests for the library-facing IndexObservability counter snapshots.

The end-to-end wire-in is pinned by
``test_proxy_manager_pipeline.py::TestExtractAndStore::test_dedup_skips_duplicate``
(extract path) and
``test_proxy_manager_pipeline.py::TestAutoIndexResponse``
(auto_index path) — this file focuses on the standalone counter contract for
custom ``ProxyManager`` embedders.
"""

from __future__ import annotations

from memtomem_stm.proxy.index_observability import (
    _NOOP_INDEX_OBSERVABILITY,
    IndexObservability,
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
        """The 6-label split must keep each label as a separate slot —
        fusing ``extracted_zero_facts`` into ``stored=0`` would lose the
        signal "extraction fired but produced nothing", which is
        architecturally distinct from "facts existed but were duplicates"
        (``dedup_skip``), from "facts existed and were stored"
        (``stored``), from "content was refused before any write"
        (``privacy_skip``, #453), and from "the background stage was never
        scheduled at all" (``shed``, #868 — nothing ran and nothing will,
        unlike a NULL ok column, which means work is still pending)."""
        obs = IndexObservability()
        obs.record_outcome("t", "stored")
        obs.record_outcome("t", "dedup_skip")
        obs.record_outcome("t", "extracted_zero_facts")
        obs.record_outcome("t", "privacy_skip")
        obs.record_outcome("t", "shed")
        obs.record_outcome("t", "error")
        snap = obs.snapshot()
        assert snap["outcomes"]["t"] == {
            "stored": 1,
            "dedup_skip": 1,
            "extracted_zero_facts": 1,
            "privacy_skip": 1,
            "shed": 1,
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
        that path) still flips ``any_call`` in the library snapshot."""
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


