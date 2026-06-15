"""bench_qa — per-call latency regression gate (advisory).

This is the one bench_qa surface that is *not* byte-deterministic. The
determinism gate (#486) and cross-version drift gate (#487) work because
``canonicalize_report`` strips exactly the three fields this file cares about
— ``clean_ms`` / ``compress_ms`` / ``surface_ms`` — since "stage latencies
differ run-to-run by definition." A perf gate therefore cannot be
snapshot-equality; it is a threshold gate and carries inherent run-to-run
variance. It ships **advisory** (``continue-on-error: true`` in its own CI
job), to be promoted to required after a real-fleet observation window,
mirroring the #482/#487 rollout.

Design (the contested decisions, resolved):

* **What is measured** — per-call ``total_ms`` (= ``clean_ms`` +
  ``compress_ms``; ``surface_ms`` is 0 here because :func:`make_proxy_manager`
  wires no surfacing engine) read from the *production* ``TokenTracker``
  (``mgr.tracker``), the same aggregator the live proxy exposes via
  ``stm_proxy_stats``. We reuse the real instrumentation rather than
  re-timing in the test, so the gate measures the real pipeline.
* **Statistic = MEDIAN of N calls, not P95 over many runs.** The user's
  standing guidance (auto-memory ``feedback_latency_test_dual_assertion``)
  is explicit: "Don't substitute p50/percentile samples over many runs —
  two cheap assertions per test beat one expensive statistical one." At
  these sub-millisecond magnitudes a within-burst P95 over only N samples is
  barely distinguishable from the top sample and adds nothing over the median
  (measured s07: median ~1.08 ms, p95 ~1.4 ms — both far under the ceiling).
  The median *gates*; the P95 is recorded in the sweep sidecar advisory-only
  (computed with the *production* estimator, ``metrics._percentile``, so it is
  directly comparable to ``stm_proxy_stats``), satisfying the plan's literal
  "P95" surface at zero added runtime.
* **Dual assertion = absolute + relative** (the orthogonal failure modes
  from the feedback memory):
  - *Absolute* ``median_auto < ABS_CEILING_MS`` — the "everything got
    slower" tripwire. Measured medians are 0.23/0.72/1.08 ms (s03/s05/s07
    on a dev box), so the 25 ms ceiling is ~25-100x headroom: it survives
    any CI neighbor swing (those are wall-clock job/spawn noise, not
    in-process sub-ms CPU work on <13 KB strings) and only reds on a >20x
    global regression (a per-call fsync, a sync network hop in the hot
    path, an O(n^2) clean).
  - *Relative* ``work < REL_MULT * max(median_none, FLOOR_CLAMP_MS)`` where
    ``work = median_auto - median_none``. The relative *anchor* is an
    in-run, same-process, same-fixture run of the *identical* pipeline with
    ``CompressionStrategy.NONE`` on the *same payload bytes*: NONE still
    runs CLEAN (regex strip) but COMPRESS is a near-passthrough, so it
    captures *this machine's* per-call fixed overhead (dispatch + AsyncMock
    round-trip + CLEAN + ``metrics.record`` + ``monotonic`` reads) at *this
    instant*. ``work`` therefore isolates the *compression-specific* cost
    from raw CPU speed. This replaces F4's fake 500 ms sleep (there is no
    sleep here): a slow runner inflates ``median_auto`` and ``median_none``
    together, so ``work``-over-floor cancels machine speed and the ratio is
    machine-invariant by construction. A 2-3x compression regression grows
    ``work`` past the threshold because the NONE floor does not slow with it.
    The ``FLOOR_CLAMP_MS`` keeps the ratio well-defined when NONE is nearly
    free (load-bearing for s03, the small ~1.6 KB payload whose NONE floor
    sits below the clamp; inert for the larger ~12 KB s05/s07, where CLEAN
    keeps the NONE floor well above it). The NONE-anchor assumption —
    "NONE = AUTO minus only the
    compression stage" — is load-bearing: if a refactor makes NONE skip a
    stage AUTO runs, recalibrate ``REL_MULT``.

* **surface_ms is deliberately NOT gated** on any fixture. It is
  subprocess-spawn-dominated (the surfacing path spawns a stdio fake-LTM
  child), tens of ms, with a 50-100 ms CI swing, and is not size-bound — a
  threshold there would either flake or be useless. This is a documented
  coverage hole; the determinism/drift gates still catch surfacing
  *correctness* regressions, just not its latency.

* **Stays out of report.json / determinism / drift.** Perf numbers are the
  one non-reproducible surface and would red both snapshot gates instantly.
  They are emitted via the existing :meth:`BenchReportCollector.record_sweep`
  sidecar (``sweep_perf.json``), never :meth:`record_scenario`, so they
  contribute zero ``ScenarioReport`` rows. The determinism roster tripwire
  is unaffected: this gate *reuses* s03/s05/s07 and adds no new ``s*.json``.

Thresholds below are deliberately generous placeholders, calibrated to a dev
box plus reasoned headroom. They are meant to be ratcheted from real GitHub
Actions artifacts during the advisory window, exactly as #482/#487 prescribe
— that ratcheting is a later reviewed edit, not a pre-coding decision.
"""

from __future__ import annotations

import statistics

import pytest

from memtomem_stm.proxy.config import CompressionStrategy
from memtomem_stm.proxy.metrics import _percentile

from bench.bench_qa import (
    deterministic_trace_id,
    load_fixture,
    make_proxy_manager,
)
from bench.bench_qa.runner import make_tool_result

# Gate parameters — advisory placeholders; ratchet from GHA artifacts.
_N = 7  # median-of-N per stat; NOT a percentile over many runs.
_ABS_CEILING_MS = 25.0  # global "everything got slower" guard (~25-100x dev headroom).
_REL_MULT = 6.0  # compression cost may be at most REL_MULT x the in-run NONE floor.
_FLOOR_CLAMP_MS = 0.2  # keeps the ratio well-defined when NONE is ~free (s03; inert for s05/s07).

# Compression-bearing, non-surfacing happy-path fixtures. Each exercises a
# distinct strategy so the gate covers the real compaction code paths:
#   s03 AUTO -> truncate, s05 SKELETON, s07 SELECTIVE.
_PERF_FIXTURES = ("s03", "s05", "s07")


async def _drive_median_total_ms(
    scenario_id: str,
    compression: str | CompressionStrategy,
    tmp_path,
) -> tuple[float, float, list[float]]:
    """Drive *scenario_id* through ``call_tool`` *_N* times under *compression*
    and return ``(median_total_ms, p95_total_ms, per_call_total_ms)``.

    Each call goes through the identical real ``ProxyManager.call_tool``
    pipeline; per-call ``total_ms`` is read from the production
    ``TokenTracker`` deque (``mgr.tracker._total_latencies``) at full float
    precision — ``get_summary()`` would round to 2 decimals, which is too
    coarse for the sub-millisecond ``work`` difference. Reaching into the
    deque is acceptable here: the test owns the tracker, and the wiring it
    relies on is pinned by :func:`test_bench_qa_perf_pipeline_timing_wiring`.

    ``make_proxy_manager`` passes no ``ProxyCache``, so every call runs the
    full pipeline (no cache-hit bypass that would skip stage timing). The
    ``min_retention`` override is forwarded only when the fixture carries one
    (s05 lowers it so SKELETON's sub-floor ratio does not trip the fallback
    ladder, which would change *what* is timed); s03/s07 keep the 0.65
    default.
    """
    fixture = load_fixture(scenario_id)
    mgr, store, session = make_proxy_manager(
        tmp_path,
        compression=compression,
        max_result_chars=fixture["max_result_chars"],
        min_retention=fixture.get("min_retention", 0.65),
    )
    session.call_tool.return_value = make_tool_result(fixture["payload"])
    try:
        for i in range(_N):
            await mgr.call_tool(
                "fake",
                f"tool_{scenario_id}",
                {},
                trace_id=deterministic_trace_id(scenario_id, run_seed=i),
            )
        latencies = list(mgr.tracker._total_latencies)
    finally:
        store.close()

    assert len(latencies) == _N, (
        f"{scenario_id}/{compression}: expected {_N} recorded latencies but got "
        f"{len(latencies)} — call_tool stopped feeding TokenTracker (cache-hit "
        "bypass or a timing-wiring regression). The perf gate is meaningless "
        "without this; see test_bench_qa_perf_pipeline_timing_wiring."
    )
    s = sorted(latencies)
    median = statistics.median(s)
    # p95 via the production estimator (linear-interpolated, identical to
    # TokenTracker.get_summary / stm_proxy_stats) so the sidecar value is
    # directly comparable. Advisory only — recorded, never asserted.
    p95 = _percentile(s, 95)
    return median, p95, latencies


@pytest.mark.asyncio
async def test_bench_qa_perf_pipeline_timing_wiring(tmp_path):
    """Machinery guard — UNMARKED so it runs in the required ``test`` job.

    The advisory perf *number* can wait for a real-fleet baseline, but the
    *wiring* it depends on must not silently rot: ``call_tool`` must keep
    feeding per-call ``total_ms`` into ``TokenTracker``, and the summary must
    keep exposing the ``total_ms`` percentile block. If a refactor breaks
    either, this reds the required gate immediately — even while the perf
    thresholds themselves stay advisory (the marked/unmarked split mirrors
    the determinism gate's ``test_determinism_roster_covers_every_fixture``).

    Deterministic and sub-millisecond: one call, structural assertions only,
    no latency threshold — safe for a required job.
    """
    fixture = load_fixture("s03")
    mgr, store, session = make_proxy_manager(
        tmp_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
    )
    session.call_tool.return_value = make_tool_result(fixture["payload"])
    try:
        await mgr.call_tool("fake", "tool_s03", {}, trace_id=deterministic_trace_id("s03"))
        latencies = list(mgr.tracker._total_latencies)
        summary = mgr.tracker.get_summary()
    finally:
        store.close()

    assert len(latencies) == 1, (
        "call_tool did not record exactly one per-call latency into "
        f"TokenTracker._total_latencies (got {len(latencies)}). The perf gate's "
        "median is read from this deque; a cache-hit bypass or a moved timing "
        "site breaks it."
    )
    pcts = summary["latency_percentiles"]["total_ms"]
    assert {"p50", "p95", "p99"} <= set(pcts), (
        "TokenTracker.get_summary() no longer exposes total_ms p50/p95/p99 — "
        f"the perf gate reads this shape. Got: {sorted(pcts)}."
    )


@pytest.mark.bench_qa_perf
@pytest.mark.asyncio
async def test_bench_qa_perf_stage_latency(bench_qa_report, tmp_path):
    """Per-call latency regression gate over s03/s05/s07 (advisory).

    For each fixture, drive the real pipeline *_N* times under its expected
    compressor and *_N* times under ``NONE`` on the same bytes, then apply
    the absolute + relative dual assertion (see module docstring). All three
    fixtures are checked before failing so a breach names every offender, and
    one ``sweep_perf.json`` row per fixture is emitted for human ratcheting.

    Mirrors the s11 sweep's loop-and-record-once shape (single
    ``record_sweep`` call, no module-level cross-test state) so a future
    pytest-xdist split cannot desynchronise the sidecar.
    """
    rows: list[dict] = []
    violations: list[str] = []

    for scenario_id in _PERF_FIXTURES:
        fixture = load_fixture(scenario_id)
        auto_dir = tmp_path / f"{scenario_id}_auto"
        none_dir = tmp_path / f"{scenario_id}_none"
        auto_dir.mkdir()
        none_dir.mkdir()

        median_auto, p95_auto, _ = await _drive_median_total_ms(
            scenario_id, fixture["expected_compressor"], auto_dir
        )
        median_none, _, _ = await _drive_median_total_ms(
            scenario_id, CompressionStrategy.NONE, none_dir
        )

        work = median_auto - median_none
        rel_threshold = _REL_MULT * max(median_none, _FLOOR_CLAMP_MS)

        rows.append(
            {
                "fixture": scenario_id,
                "strategy": fixture["expected_compressor"],
                "n": _N,
                "auto_median_ms": round(median_auto, 4),
                "none_median_ms": round(median_none, 4),
                "work_ms": round(work, 4),
                "auto_p95_ms": round(p95_auto, 4),  # advisory only — never asserted
                "abs_ceiling_ms": _ABS_CEILING_MS,
                "rel_mult": _REL_MULT,
                "rel_threshold_ms": round(rel_threshold, 4),
                "floor_clamp_ms": _FLOOR_CLAMP_MS,
            }
        )

        if median_auto >= _ABS_CEILING_MS:
            violations.append(
                f"{scenario_id}: absolute — median total {median_auto:.3f}ms >= "
                f"{_ABS_CEILING_MS}ms ceiling (>20x global regression)."
            )
        if work >= rel_threshold:
            violations.append(
                f"{scenario_id}: relative — compression work {work:.3f}ms "
                f"(auto {median_auto:.3f} - none {median_none:.3f}) >= "
                f"{_REL_MULT}x NONE floor {rel_threshold:.3f}ms "
                f"(compression-specific slowdown)."
            )

    # Single sidecar write with all rows; record_sweep overwrites by name, so
    # the loop-and-record-once shape avoids parametrize clobbering.
    bench_qa_report.record_sweep("perf", rows)

    assert not violations, (
        "bench_qa perf gate breached (advisory):\n  "
        + "\n  ".join(violations)
        + "\nThresholds are advisory placeholders — if this is a real, intended "
        "cost change, ratchet _ABS_CEILING_MS / _REL_MULT from the latest "
        "bench-qa-perf-report artifact; if it is a regression, fix the pipeline."
    )
