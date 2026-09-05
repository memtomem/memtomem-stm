"""Tests for SurfacingObservability counters + stm_surfacing_stats render helper.

The engine/gate integration with observability is covered in
``test_surfacing_engine.py::TestSurfacingObservabilityIntegration`` and
``test_relevance_gate.py::TestRelevanceGateObservability`` — this file is
focused on the standalone counter contract and the stats formatter.
"""

from __future__ import annotations

import asyncio
from typing import get_args

from memtomem_stm.server import (
    _format_observability_sections,
    _ordered_tool_keys,
    _surfacing_verdict_line,
)
from memtomem_stm.surfacing.observability import (
    FAULT_OUTCOMES,
    FAULT_SKIP_REASONS,
    HEALTHY_SKIP_REASONS,
    SEARCH_COMPLETED_SKIP_REASONS,
    SURFACED_OUTCOMES,
    Outcome,
    SkipReason,
    SurfacingObservability,
    attribute_call,
    record_ltm_rpc,
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

    def test_verdict_category_sets_are_consistent(self):
        """#363's denominator sets must stay consistent with the taxonomy
        they slice. ``SEARCH_COMPLETED_SKIP_REASONS`` names the healthy skips
        that still cost an LTM round trip, so it must be a subset of
        ``HEALTHY_SKIP_REASONS`` (a fault reason leaking in would be counted
        as a healthy completion and drag the verdict toward HEALTHY during an
        outage), and the two outcome sets must partition ``Outcome`` (an
        unclassified outcome would silently vanish from the denominator)."""
        assert SEARCH_COMPLETED_SKIP_REASONS <= HEALTHY_SKIP_REASONS
        assert SEARCH_COMPLETED_SKIP_REASONS & FAULT_SKIP_REASONS == set()
        all_outcomes = set(get_args(Outcome))
        assert SURFACED_OUTCOMES | FAULT_OUTCOMES == all_outcomes, (
            f"unclassified Outcome values: {all_outcomes - (SURFACED_OUTCOMES | FAULT_OUTCOMES)}"
        )
        assert SURFACED_OUTCOMES & FAULT_OUTCOMES == set()

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

    def test_no_results_demoted_renders_under_healthy_subsection(self):
        """``no_results_demoted`` (#404) must survive the healthy/fault
        split. It was originally recorded as a bare string in engine.py
        with no ``SkipReason`` Literal member and no category assignment,
        so ``_split_skip_reasons_by_category`` silently dropped it — the
        exhaustiveness test above only inspects ``get_args(SkipReason)``
        and stayed green. This pins the full render path so the label
        cannot leak through unclassified again."""
        snap = {
            "any_call": True,
            "skip_reasons": {
                "read_file": {"no_results_demoted": 7},
                "__total__": {"no_results_demoted": 7},
            },
            "outcomes": {},
            "cache": {},
        }
        lines = _format_observability_sections(snap, tool_filter=None)
        joined = "\n".join(lines)
        assert "Healthy skips" in joined
        assert "no_results_demoted: 7" in joined

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


class TestSurfacingVerdict:
    """Pins the top-line verdict introduced in #363 (#351 part 1).

    The verdict is an advisory an operator acts on, so a wrong word is worse
    than no word: these tests pin each band, the sample floor, and the
    denominator's composition rather than only asserting the string renders.
    """

    @staticmethod
    def _snap(skips: dict, outcomes: dict) -> dict:
        return {
            "any_call": True,
            "skip_reasons": {"__total__": skips} if skips else {},
            "outcomes": {"__total__": outcomes} if outcomes else {},
            "cache": {},
        }

    def test_healthy_when_faults_are_a_small_minority(self):
        line = _surfacing_verdict_line(
            self._snap({"ltm_unavailable": 2}, {"surfaced_cache_miss": 90}),
            tool_filter=None,
        )
        assert line is not None
        assert line.startswith("Verdict (this process, since start): HEALTHY — ")
        assert "2 of 92 LTM attempts faulted (2.2%)" in line
        assert "top fault: ltm_unavailable 2" in line

    def test_degraded_between_the_bands(self):
        line = _surfacing_verdict_line(
            self._snap({}, {"error_timeout": 30, "surfaced_cache_miss": 41}),
            tool_filter=None,
        )
        assert line is not None
        assert "DEGRADED" in line
        assert "30 of 71 LTM attempts faulted (42.3%)" in line
        assert "top fault: error_timeout 30" in line

    def test_faulty_during_an_outage(self):
        """The dogfood outage shape: circuit_open dominates and the ratio
        saturates because circuit_open is recorded before the gate."""
        line = _surfacing_verdict_line(
            self._snap({"circuit_open": 1163}, {"surfaced_cache_miss": 17}),
            tool_filter=None,
        )
        assert line is not None
        assert "FAULTY" in line
        assert "1163 of 1180 LTM attempts faulted (98.6%)" in line
        assert "top fault: circuit_open 1163" in line

    def test_band_boundaries_are_inclusive_lower_edges(self):
        """DEGRADED starts at exactly 25% and FAULTY at exactly 75% — pinned
        so a future ``>`` / ``>=`` slip is caught. 24/96 = 25.0% and
        75/100 = 75.0% land exactly on the two edges."""
        degraded = _surfacing_verdict_line(
            self._snap({}, {"error_other": 24, "surfaced_cache_hit": 72}),
            tool_filter=None,
        )
        assert degraded is not None and "DEGRADED" in degraded
        # One fault fewer (23/96 = 24.0%) falls back to HEALTHY.
        healthy = _surfacing_verdict_line(
            self._snap({}, {"error_other": 23, "surfaced_cache_hit": 73}),
            tool_filter=None,
        )
        assert healthy is not None and "HEALTHY" in healthy
        faulty = _surfacing_verdict_line(
            self._snap({}, {"error_other": 75, "surfaced_cache_hit": 25}),
            tool_filter=None,
        )
        assert faulty is not None and "FAULTY" in faulty
        # One fault fewer (74/100 = 74.0%) stays DEGRADED.
        still_degraded = _surfacing_verdict_line(
            self._snap({}, {"error_other": 74, "surfaced_cache_hit": 26}),
            tool_filter=None,
        )
        assert still_degraded is not None and "DEGRADED" in still_degraded

    def test_insufficient_data_below_the_sample_floor(self):
        line = _surfacing_verdict_line(
            self._snap({}, {"error_timeout": 3, "surfaced_cache_miss": 6}),
            tool_filter=None,
        )
        assert line is not None
        assert "insufficient data — 9 LTM attempts (need 10)" in line
        # No verdict word — an advisory on 9 samples is the failure mode the
        # floor exists to prevent.
        assert "DEGRADED" not in line and "FAULTY" not in line and "HEALTHY" not in line
        # Exactly at the floor a real verdict renders.
        at_floor = _surfacing_verdict_line(
            self._snap({}, {"error_timeout": 3, "surfaced_cache_miss": 7}),
            tool_filter=None,
        )
        assert at_floor is not None and "DEGRADED" in at_floor

    def test_zero_faults_omits_the_top_fault_clause(self):
        line = _surfacing_verdict_line(
            self._snap({"no_results_score": 20}, {"surfaced_cache_hit": 37}),
            tool_filter=None,
        )
        assert line is not None
        assert "HEALTHY" in line
        assert "0 of 57 LTM attempts faulted (0.0%)" in line
        assert "top fault" not in line

    def test_pre_ltm_skips_are_excluded_from_the_denominator(self):
        """A thousand cooldown/gate skips must not dilute an outage into
        HEALTHY: they are decided before any LTM work is attempted. Same
        counters with and without the pre-LTM noise must give the same
        verdict and the same denominator."""
        faults_only = self._snap({"ltm_unavailable": 9}, {"surfaced_cache_miss": 2})
        with_noise = self._snap(
            {
                "ltm_unavailable": 9,
                "gate_cooldown": 1000,
                "response_too_short": 500,
                "gate_write_tool": 200,
                "no_query": 40,
                "daemon_busy": 5,
                "disabled": 3,
                "upstream_disabled": 2,
                "progressive_mode_conflict": 1,
            },
            {"surfaced_cache_miss": 2},
        )
        bare = _surfacing_verdict_line(faults_only, tool_filter=None)
        noisy = _surfacing_verdict_line(with_noise, tool_filter=None)
        assert bare == noisy
        assert noisy is not None
        assert "FAULTY" in noisy
        assert "9 of 11 LTM attempts faulted" in noisy

    def test_no_results_skips_count_as_completions(self):
        """``no_results_*`` means the search round trip completed and the
        candidates were filtered to nothing — a healthy completion, so it
        belongs in the denominator. Without it, a deployment whose threshold
        filters everything would show one timeout against one surfacing and
        read FAULTY."""
        line = _surfacing_verdict_line(
            self._snap(
                {"error_timeout": 0, "no_results_score": 30, "no_results_dedup": 9},
                {"error_timeout": 1, "surfaced_cache_miss": 1},
            ),
            tool_filter=None,
        )
        assert line is not None
        assert "HEALTHY" in line
        assert "1 of 41 LTM attempts faulted" in line

    def test_tool_filter_uses_that_tools_counters_not_the_total(self):
        snap = {
            "any_call": True,
            "skip_reasons": {
                "sick_tool": {"ltm_unavailable": 40},
                "well_tool": {"no_results_score": 5},
                "__total__": {"ltm_unavailable": 40, "no_results_score": 5},
            },
            "outcomes": {
                "sick_tool": {"surfaced_cache_miss": 2},
                "well_tool": {"surfaced_cache_hit": 95},
                "__total__": {"surfaced_cache_miss": 2, "surfaced_cache_hit": 95},
            },
            "cache": {},
        }
        sick = _surfacing_verdict_line(snap, tool_filter="sick_tool")
        well = _surfacing_verdict_line(snap, tool_filter="well_tool")
        total = _surfacing_verdict_line(snap, tool_filter=None)
        assert sick is not None and "FAULTY" in sick
        assert "40 of 42 LTM attempts faulted" in sick
        assert well is not None and "HEALTHY" in well
        assert "0 of 100 LTM attempts faulted" in well
        # The aggregate is its own slice, not either tool's.
        assert total is not None and "40 of 142 LTM attempts faulted" in total

    def test_unknown_tool_renders_no_verdict(self):
        """A tool the process has never surfaced for has no counters at all.
        Rendering ``insufficient data`` there would read as a health signal
        about that tool rather than as its absence from this process."""
        snap = self._snap({"ltm_unavailable": 5}, {"surfaced_cache_miss": 20})
        assert _surfacing_verdict_line(snap, tool_filter="never_seen") is None


class TestCallLedger:
    """Per-call attribution (#874).

    The daemon used to read *this* request's outcome as a before/after delta on
    the process-global ``__total__`` counters, which is only correct while
    exactly one surfacing call can be in flight. These pin the replacement.
    """

    def test_records_land_in_the_open_ledger_and_in_the_counters(self):
        obs = SurfacingObservability()
        with attribute_call() as ledger:
            obs.record_skip("Read", "no_results_score")
            obs.record_outcome("Read", "surfaced_cache_miss")
        assert ledger.skip_reasons == ["no_results_score"]
        assert ledger.outcomes == ["surfaced_cache_miss"]
        # The global aggregate still moves — the ledger is additive, not a
        # replacement for the counters ``stm_surfacing_stats`` renders.
        snap = obs.snapshot()
        assert snap["skip_reasons"]["__total__"] == {"no_results_score": 1}
        assert snap["outcomes"]["__total__"] == {"surfaced_cache_miss": 1}

    def test_recording_outside_any_ledger_is_fine(self):
        # The proxy path records without ever opening a ledger.
        obs = SurfacingObservability()
        obs.record_skip("Read", "gate_cooldown")
        assert obs.snapshot()["skip_reasons"]["__total__"] == {"gate_cooldown": 1}

    async def test_ledger_captures_records_made_in_child_tasks(self):
        # The engine runs the surfacing body in a task ``_run_within`` spawns
        # (`asyncio.ensure_future`), and the terminal outcome of a completed
        # search — ``surfaced_cache_miss`` — is recorded in there. A ledger
        # that only saw the calling task would miss it and the daemon would
        # file no latency sample for a request that did the work.
        #
        # (``error_timeout`` is NOT that case: it is raised out of the child
        # and caught by ``surface()`` in the caller's own task. It is covered
        # by the daemon tests instead.)
        obs = SurfacingObservability()
        with attribute_call() as ledger:
            await asyncio.ensure_future(
                _record_later(obs, "surfaced_cache_miss"),
            )
        assert ledger.outcomes == ["surfaced_cache_miss"]

    async def test_ledger_captures_the_rpc_mark_made_in_a_child_task(self):
        # The adapter marks the RPC from inside the same ``_run_within`` task
        # the search body runs in. Same carrier as the outcome above: a ledger
        # that only saw the calling task would read every completed search as
        # "never reached the LTM" and the daemon would file no sample at all.
        with attribute_call() as ledger:
            await asyncio.ensure_future(_mark_later())
        assert ledger.retrieval_attempted is True

    async def test_ledger_is_isolated_between_concurrent_tasks(self):
        # Two overlapping surfacing calls: neither may see the other's record.
        obs = SurfacingObservability()
        started = asyncio.Event()
        may_finish = asyncio.Event()

        async def slow_timeout() -> list[str]:
            with attribute_call() as ledger:
                started.set()
                await may_finish.wait()
                obs.record_outcome("Read", "error_timeout")
            return ledger.outcomes

        async def quick_success() -> list[str]:
            with attribute_call() as ledger:
                obs.record_outcome("Bash", "surfaced_cache_miss")
            return ledger.outcomes

        slow = asyncio.create_task(slow_timeout())
        await started.wait()
        quick = asyncio.create_task(quick_success())
        assert await quick == ["surfaced_cache_miss"]
        may_finish.set()
        assert await slow == ["error_timeout"]

    async def test_a_late_record_cannot_reach_another_requests_ledger(self):
        # An operation abandoned at timeout keeps running with the ledger of a
        # block that has already exited. Its late record must land nowhere a
        # consumer reads — never in the request that came after it.
        obs = SurfacingObservability()
        may_record = asyncio.Event()

        async def abandoned() -> None:
            await may_record.wait()
            obs.record_outcome("Read", "error_timeout")

        with attribute_call() as abandoned_ledger:
            straggler = asyncio.create_task(abandoned())
        with attribute_call() as next_ledger:
            may_record.set()
            await straggler

        assert next_ledger.outcomes == []
        assert abandoned_ledger.outcomes == ["error_timeout"]

    def test_retrieval_attempted_is_the_rpc_mark_not_the_terminal_decision(self):
        """The mark is set where the request is handed to the transport (#994).

        Two terminals a completed search records are also recorded on paths
        that issued no RPC: ``error_timeout`` when pre-work spent the whole
        window, and ``ltm_unavailable`` when session healing failed. Inferring
        the round trip from either files an STM duration into a series that is
        advice about the LTM.

        ``ltm_call_failed`` is deliberately not a third case here. It reads
        like one -- healing can fail on the compose path too -- but it is not
        reachable that way: ``context_compose`` returns ``None`` when its own
        heal fails, the engine falls back to ``search``, and that records
        ``ltm_unavailable``. Asserting it would pin a terminal production
        cannot produce.
        """
        obs = SurfacingObservability()
        with attribute_call() as pre_work_exhausted:
            obs.record_outcome("Read", "error_timeout")
        with attribute_call() as healing_failed:
            obs.record_skip("Read", "ltm_unavailable")
        with attribute_call() as rpc_timed_out:
            record_ltm_rpc()
            obs.record_outcome("Read", "error_timeout")
        with attribute_call() as rpc_unavailable:
            # A transport error mid-flight also lands on ``ltm_unavailable``;
            # that one did issue a request and stays a sample.
            record_ltm_rpc()
            obs.record_skip("Read", "ltm_unavailable")
        with attribute_call() as searched:
            record_ltm_rpc()
            obs.record_skip("Read", "no_results_score")
        with attribute_call() as gated:
            obs.record_skip("Read", "gate_cooldown")
        with attribute_call() as empty:
            pass

        assert pre_work_exhausted.retrieval_attempted is False
        assert pre_work_exhausted.timed_out is True
        assert healing_failed.retrieval_attempted is False
        assert rpc_timed_out.retrieval_attempted is True
        assert rpc_timed_out.timed_out is True
        assert rpc_unavailable.retrieval_attempted is True
        assert searched.retrieval_attempted is True
        assert gated.retrieval_attempted is False
        assert empty.retrieval_attempted is False
        assert empty.timed_out is False

    def test_marking_outside_any_ledger_is_fine(self):
        # The proxy path and the daemon's raw LTM ops issue RPCs with no ledger
        # open; the mark has nowhere to land and must not care.
        record_ltm_rpc()
        with attribute_call() as ledger:
            pass
        assert ledger.retrieval_attempted is False

    async def test_a_late_mark_cannot_reach_another_requests_ledger(self):
        # Same safety property the outcomes have, asserted for the new mutable
        # flag: an operation abandoned at timeout keeps a reference to a ledger
        # whose block has exited. Its late mark must land there, never in the
        # request that came after it -- which would file a latency sample for a
        # call that issued nothing.
        may_mark = asyncio.Event()

        async def abandoned() -> None:
            await may_mark.wait()
            record_ltm_rpc()

        with attribute_call() as abandoned_ledger:
            straggler = asyncio.create_task(abandoned())
        with attribute_call() as next_ledger:
            may_mark.set()
            await straggler

        assert next_ledger.retrieval_attempted is False
        assert abandoned_ledger.retrieval_attempted is True

    def test_faulted_reads_both_buckets_and_is_independent_of_the_mark(self):
        # ``faulted`` buckets a duration that ``retrieval_attempted`` already
        # admitted; the two answer different questions and a consumer reads
        # both. A fault with no RPC behind it is not a sample at all.
        obs = SurfacingObservability()
        with attribute_call() as rpc_then_dropped:
            record_ltm_rpc()
            obs.record_skip("Read", "ltm_unavailable")
        with attribute_call() as rpc_then_raised:
            record_ltm_rpc()
            obs.record_outcome("Read", "error_other")
        with attribute_call() as searched_and_filtered:
            record_ltm_rpc()
            obs.record_skip("Read", "no_results_score")
        with attribute_call() as healing_failed:
            obs.record_skip("Read", "ltm_unavailable")

        assert rpc_then_dropped.faulted is True
        assert rpc_then_dropped.retrieval_attempted is True
        assert rpc_then_raised.faulted is True
        # A completed search that filtered everything out is healthy, not a
        # fault -- the same call the percentiles exist to measure.
        assert searched_and_filtered.faulted is False
        assert searched_and_filtered.retrieval_attempted is True
        assert healing_failed.faulted is True
        assert healing_failed.retrieval_attempted is False

    def test_the_mark_is_per_ledger(self):
        # An RPC issued under one request's ledger says nothing about the next.
        with attribute_call() as first:
            record_ltm_rpc()
        with attribute_call() as second:
            pass
        assert first.retrieval_attempted is True
        assert second.retrieval_attempted is False


async def _mark_later() -> None:
    await asyncio.sleep(0)
    record_ltm_rpc()


async def _record_later(obs: SurfacingObservability, outcome: Outcome) -> None:
    await asyncio.sleep(0)
    obs.record_outcome("Read", outcome)
