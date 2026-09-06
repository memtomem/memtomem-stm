"""bench_qa — event-loop stall from the store writes that still run on the loop (#879).

Measurement only, not a gate: ``bench_qa_sweep`` keeps it out of default and
advisory CI, and it is invoked by hand.

#879 asks whether the remaining synchronous SQLite/file writes on the asyncio
event loop should move to a worker, and answers its own question with
"measure first — the fix only pays off if real stall evidence shows up". The
three surviving sites each carry the same reopen trigger in their own
docstring (``cache.py``, ``metrics_store.py``, ``selection_log.py``):
*"Accepted for the current local single-MCP-client deployment ... Multi-client
serving (or materially higher concurrency) is the reopen trigger."* So this
file exists to produce the evidence that trigger asks for, not to move code.

**What is measured is the harm, not the cost.** The claim under test is that
one write stalls *every* in-flight call, so timing the write alone can neither
confirm nor refute it. The primary oracle is a watchdog coroutine that awaits
``asyncio.sleep(WATCHDOG_PERIOD_S)`` in a loop and records how much longer than
that each hop actually took: that overshoot is exactly the delay an unrelated
in-flight proxied call absorbs. Per-site wall time is recorded alongside as a
secondary, by wrapping the real methods — never by re-implementing them.

**Arms.** ``metrics_only`` is the control — it wires no cache and no selection
log, but it still records metrics, because ``MetricsStore`` is not optional on
this path. It is named for what it contains rather than for what it lacks: the
attributable numbers below are therefore the cost of the cache write ON TOP OF
the metrics write, not the cost of all store I/O. ``stores`` adds the cache, so
both sites that are on by DEFAULT are live (``metrics.enabled`` and
``cache.enabled`` are both ``True``; ``selection_telemetry.enabled`` is
``False``, so its append is opt-in and gets its own arm). ``contended`` adds a
second connection that repeatedly takes and releases the cache DB's write lock,
which is the "contended disk / busy WAL" precondition #879 names — without it
this bench would only re-confirm that a healthy SSD is sub-millisecond, which
nobody disputes.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import statistics
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from bench.bench_qa.runner import make_tool_result
from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.proxy.selection_log import SelectionTelemetryLog

# ── Decision rule ────────────────────────────────────────────────────────────
#
# Written down BEFORE the first run, on purpose. A threshold picked after
# seeing the numbers proves nothing — it can only agree with them.
#
# The anchor is the vocabulary the existing per-call perf gate already uses
# (``test_bench_qa_perf.py``): ABS_CEILING_MS there is 25 ms for a whole
# proxied call's CLEAN+COMPRESS work, against measured medians of 0.23–1.08 ms.
# A store write that stalls the loop for that long is, in this codebase's own
# terms, as expensive as an entire budgeted call — and it stalls calls that are
# not even the one being served.
OFFLOAD_MEDIAN_MS = 25.0
OFFLOAD_P95_MS = 100.0

# The verdict this bench can return, per site:
#
#   offload  — the site's stall attributable over the ``none`` control reaches
#              OFFLOAD_MEDIAN_MS at the median or OFFLOAD_P95_MS at p95 in ANY
#              arm. #879 is upheld for that site; it earns its own PR.
#   reject   — it stays under both in every arm, contention included. #879 is
#              rejected for that site and its docstring's reopen trigger is
#              re-stated with these numbers and this date.
#
# A site is judged on the arm that a real deployment can actually reach. The
# contended arm models a busy WAL, which a second STM process on one machine
# produces today; it is not a synthetic worst case.

WATCHDOG_PERIOD_S = 0.001
CALLS_PER_ARM = 120
CONTENTION_HOLD_S = 0.02
CONTENTION_GAP_S = 0.005
UPSTREAM_LATENCY_S = 0.002

PAYLOAD = "lorem ipsum dolor sit amet. " * 400


def _p95(samples: list[float]) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[idx]


class _LoopWatchdog:
    """Records how much longer each ``sleep(period)`` hop took than it asked.

    The overshoot IS the harm: a coroutine that asked for 1 ms and got 40 ms
    was held off the loop for 39 ms by whatever ran synchronously in between.
    """

    def __init__(self, period: float = WATCHDOG_PERIOD_S) -> None:
        self._period = period
        self._stop = asyncio.Event()
        self._running = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.overshoot_ms: list[float] = []

    async def _run(self) -> None:
        self._running.set()
        while not self._stop.is_set():
            before = time.perf_counter()
            await asyncio.sleep(self._period)
            elapsed = time.perf_counter() - before
            self.overshoot_ms.append(max(0.0, (elapsed - self._period) * 1000.0))

    async def start(self) -> None:
        """Create the task AND wait until it is parked in its first sleep.

        ``create_task`` only schedules; the coroutine body does not begin until
        the creating task yields. The workload here never yields — every await
        in the proxied-call path completes without suspending against an
        AsyncMock upstream — so without this handoff the watchdog would not run
        at all and every arm would report zero samples, which reads like "no
        stall" when it actually means "not measured".
        """
        self._task = asyncio.get_running_loop().create_task(self._run(), name="stall-watchdog")
        await self._running.wait()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    def summary(self) -> dict[str, float]:
        s = self.overshoot_ms
        return {
            "samples": float(len(s)),
            "median_ms": statistics.median(s) if s else 0.0,
            "p95_ms": _p95(s),
            "max_ms": max(s) if s else 0.0,
        }


class _SiteTimer:
    """Wraps a real bound method and records its wall time, unchanged otherwise."""

    def __init__(self) -> None:
        self.by_site: dict[str, list[float]] = {}

    def wrap(self, obj: Any, name: str, site: str) -> None:
        real = getattr(obj, name)
        bucket = self.by_site.setdefault(site, [])

        def timed(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return real(*args, **kwargs)
            finally:
                bucket.append((time.perf_counter() - started) * 1000.0)

        setattr(obj, name, timed)

    def summary(self) -> dict[str, dict[str, float]]:
        return {
            site: {
                "calls": float(len(v)),
                "median_ms": statistics.median(v) if v else 0.0,
                "p95_ms": _p95(v),
                "total_ms": sum(v),
            }
            for site, v in self.by_site.items()
        }


class _WalContender:
    """A peer holding and releasing the cache DB's write lock, on its own thread.

    ``BEGIN IMMEDIATE`` takes SQLite's write lock for the whole file, so a write
    on the loop waits it out under ``busy_timeout``. That is the "contended disk
    (or a busy WAL)" #879 names as its precondition — and a second STM process
    on one machine produces it today.

    Read the duty cycle before reading the verdict: this holds the lock
    ``CONTENTION_HOLD_S`` and releases it ``CONTENTION_GAP_S``, so the file is
    locked ~80% of the time. That is sustained contention, an upper bound —
    a peer's retention purge arrives in bursts, not as a steady 80%. The arm
    answers "what does contention cost when it is present", not "how often is
    it present".
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        db = sqlite3.connect(str(self._db_path), timeout=30.0)
        try:
            while not self._stop.is_set():
                try:
                    db.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError:
                    time.sleep(CONTENTION_GAP_S)
                    continue
                self._stop.wait(CONTENTION_HOLD_S)
                db.rollback()
                self._stop.wait(CONTENTION_GAP_S)
        finally:
            db.close()

    def __enter__(self) -> _WalContender:
        self._thread = threading.Thread(target=self._run, name="wal-contender", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


def _build_manager(
    tmp_path: Path,
    *,
    with_stores: bool,
    with_selection_log: bool = False,
) -> tuple[ProxyManager, AsyncMock, ProxyCache | None, MetricsStore, SelectionTelemetryLog | None]:
    """A ProxyManager wired the way the server lifespan wires one.

    ``make_proxy_manager`` in the bench runner passes neither ``cache`` nor
    ``selection_log`` — both default to ``None`` on ``ProxyManager`` — so two
    of the four #879 sites never run under it. This builds the wiring the
    lifespan actually produces (``server.py:470`` for the cache,
    ``server.py:382`` for the selection log) so the sites are live.
    """
    store = MetricsStore(tmp_path / "proxy_metrics.db")
    store.initialize()

    cache: ProxyCache | None = None
    if with_stores:
        cache = ProxyCache(tmp_path / "proxy_cache.db", max_entries=10_000)
        cache.initialize()

    selection_log: SelectionTelemetryLog | None = None
    if with_selection_log:
        selection_log = SelectionTelemetryLog(tmp_path / "selection.jsonl")
        selection_log.initialize()

    server_cfg = UpstreamServerConfig(
        prefix="fake",
        compression=CompressionStrategy.NONE,
        max_result_chars=50_000,
        max_retries=0,
        reconnect_delay_seconds=0.0,
    )
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"fake": server_cfg},
    )
    mgr = ProxyManager(
        proxy_cfg,
        TokenTracker(metrics_store=store),
        cache=cache,
        selection_log=selection_log,
    )
    session = AsyncMock()

    async def _upstream(*_args: Any, **_kwargs: Any) -> Any:
        # A real suspension point, because a real upstream is one. Returning a
        # value outright — which a bare AsyncMock does — means no await in the
        # whole proxied-call path ever yields, so the event loop gets control
        # exactly once per RUN instead of once per call. The watchdog then
        # records a single overshoot the size of the entire workload, which is
        # true but attributes nothing: it folds compression, cleaning and the
        # store writes into one number. Suspending here restores the per-call
        # boundary the measurement needs, and models what the proxy actually
        # waits on.
        await asyncio.sleep(UPSTREAM_LATENCY_S)
        return make_tool_result(PAYLOAD)

    session.call_tool = _upstream
    mgr._connections["fake"] = UpstreamConnection(
        name="fake", config=server_cfg, session=session, tools=[]
    )
    return mgr, session, cache, store, selection_log


async def _drive(mgr: ProxyManager, calls: int) -> None:
    """One distinct proxied call per iteration, so every call is a cache MISS.

    A repeated argument set would be served from the cache after the first
    call and would never reach ``set()`` again — measuring the write path
    requires the write path to run.
    """
    for i in range(calls):
        await mgr.call_tool("fake", "read_file", {"path": f"/f/{i}.md"})


async def _run_arm(
    tmp_path: Path,
    *,
    with_stores: bool,
    with_selection_log: bool = False,
    contend_db: str | None = None,
) -> dict[str, Any]:
    mgr, _session, cache, store, selection_log = _build_manager(
        tmp_path, with_stores=with_stores, with_selection_log=with_selection_log
    )
    timer = _SiteTimer()
    if cache is not None:
        timer.wrap(cache, "set", "ProxyCache.set")
    timer.wrap(store, "record", "MetricsStore.record")
    if selection_log is not None:
        timer.wrap(selection_log, "_append", "SelectionTelemetryLog._append")

    watchdog = _LoopWatchdog()
    await watchdog.start()
    started = time.perf_counter()
    try:
        if contend_db is not None:
            with _WalContender(tmp_path / contend_db):
                await _drive(mgr, CALLS_PER_ARM)
        else:
            await _drive(mgr, CALLS_PER_ARM)
    finally:
        await watchdog.stop()
    wall_s = time.perf_counter() - started

    if cache is not None:
        cache.close()
    store.close()
    if selection_log is not None:
        selection_log.close()

    return {
        "calls": CALLS_PER_ARM,
        "wall_seconds": round(wall_s, 3),
        "loop_stall": watchdog.summary(),
        "sites": timer.summary(),
    }


@pytest.mark.bench_qa
@pytest.mark.bench_qa_sweep
@pytest.mark.asyncio
async def test_store_write_loop_stall_sweep(tmp_path):
    """Emit the #879 evidence: loop stall per arm, plus per-site wall time.

    Assertions are deliberately minimal — this is a measurement run, and a
    threshold assertion here would turn a machine-dependent number into a
    gate. The verdict is read off the emitted artifact against the decision
    rule at the top of this file.
    """
    arms: dict[str, Any] = {}
    arms["metrics_only"] = await _run_arm(tmp_path / "metrics_only", with_stores=False)
    arms["stores"] = await _run_arm(tmp_path / "stores", with_stores=True)
    # Contend each store's OWN file. Contending only the cache DB and then
    # reporting that the metrics write is unaffected would be an artifact of
    # which file was locked, not a property of the metrics write: the two live
    # in different databases and SQLite's write lock is per file.
    arms["cache_db_contended"] = await _run_arm(
        tmp_path / "cache_contended", with_stores=True, contend_db="proxy_cache.db"
    )
    arms["metrics_db_contended"] = await _run_arm(
        tmp_path / "metrics_contended", with_stores=True, contend_db="proxy_metrics.db"
    )
    arms["stores_selection_log"] = await _run_arm(
        tmp_path / "sel", with_stores=True, with_selection_log=True
    )

    control_median = arms["metrics_only"]["loop_stall"]["median_ms"]
    control_p95 = arms["metrics_only"]["loop_stall"]["p95_ms"]
    verdicts: dict[str, Any] = {}
    for name, arm in arms.items():
        if name == "metrics_only":
            continue
        attributable_median = arm["loop_stall"]["median_ms"] - control_median
        attributable_p95 = arm["loop_stall"]["p95_ms"] - control_p95
        verdicts[name] = {
            "attributable_median_ms": round(attributable_median, 4),
            "attributable_p95_ms": round(attributable_p95, 4),
            "verdict": (
                "offload"
                if attributable_median >= OFFLOAD_MEDIAN_MS or attributable_p95 >= OFFLOAD_P95_MS
                else "reject"
            ),
        }

    report = {
        "issue": 879,
        "decision_rule": {
            "offload_median_ms": OFFLOAD_MEDIAN_MS,
            "offload_p95_ms": OFFLOAD_P95_MS,
            "anchor": "test_bench_qa_perf.ABS_CEILING_MS (25 ms per whole proxied call)",
        },
        "calls_per_arm": CALLS_PER_ARM,
        "arms": arms,
        "verdicts": verdicts,
    }

    out_dir = Path(os.environ.get("BENCH_QA_REPORT_DIR", "/tmp"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "sweep_879_store_stall.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n#879 store-stall report written to: {out_path}")
    print(json.dumps(verdicts, indent=2))

    # The only assertions: the bench measured something, and the control is a
    # control. A run where the store arms never wrote proves nothing at all —
    # and neither does one where the watchdog never got a hop, which is why
    # the sample count is checked rather than trusted. Zero samples reads
    # identically to zero stall in the artifact, so it must fail loudly here.
    for arm_name, arm in arms.items():
        assert arm["loop_stall"]["samples"] > 0, (
            f"{arm_name}: watchdog collected no samples — the arm measured nothing, "
            "so its zeros are absence of data, not absence of stall"
        )
    assert arms["stores"]["sites"]["ProxyCache.set"]["calls"] > 0
    assert arms["stores"]["sites"]["MetricsStore.record"]["calls"] > 0
    assert arms["stores_selection_log"]["sites"]["SelectionTelemetryLog._append"]["calls"] > 0
    assert "ProxyCache.set" not in arms["metrics_only"]["sites"]
