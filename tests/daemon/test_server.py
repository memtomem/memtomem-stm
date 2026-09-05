"""End-to-end transport + lifecycle tests for the surfacing daemon.

These never contact a *production* LTM server: the surface-hit path injects a
``SurfacingEngine`` over a mock adapter, and the real-wiring path exercises a
non-allowlisted tool so ``run_surfacing_hook`` returns ``{}`` before any LTM
RPC. The one exception is the teardown leak-sweep e2e
(``test_real_teardown_reaps_warm_ltm_child``), which deliberately warms a real
stdio MCP round trip — against the in-repo ``_fake_memtomem_server.py``, never
an installed memtomem. That keeps the suite deterministic even on a dev box
with a live LTM.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import memtomem_stm.daemon.server as daemon_server
from memtomem_stm.utils import child_reaper
from memtomem_stm.config import STMConfig
from memtomem_stm.daemon import client
from memtomem_stm.daemon.discovery import (
    config_fingerprint,
    handshake_path,
    is_pid_alive,
    read_handshake,
)
from memtomem_stm.daemon.protocol import (
    MAX_MESSAGE_BYTES,
    OP_PING,
    OP_SURFACE,
    PROTOCOL_VERSION,
    build_request,
    encode_line,
    read_message,
    surface_response,
)
from memtomem_stm.daemon.server import DaemonServer
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.observability import SurfacingObservability, record_search_rpc


@dataclass
class _Meta:
    source_file: Path = Path("/notes/test.md")
    namespace: str = "default"


@dataclass
class _Chunk:
    id: str = ""
    content: str = "remembered detail about jwt"
    metadata: _Meta | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid4())
        if self.metadata is None:
            self.metadata = _Meta()


@dataclass
class _Result:
    chunk: _Chunk
    score: float


_LONG = "JWT authentication handler. " * 50
_READ_PAYLOAD = {
    "hook_event_name": "PostToolUse",
    "tool_name": "Read",
    "tool_input": {"file_path": "/src/auth/jwt_handler.py"},
    "tool_response": {"content": _LONG},
}


def _canonical(payload: dict):
    """Parse a raw host payload into the CanonicalHookCall the hook/daemon now
    pass around (``client.surface`` serializes it onto the wire)."""
    from memtomem_stm.cli.hook_adapter import ClaudeHookAdapter

    return ClaudeHookAdapter().parse(payload)


def _config(tmp_path: Path) -> STMConfig:
    cfg = STMConfig()
    cfg.data_dir = tmp_path
    cfg.daemon.idle_timeout_seconds = 0.0
    s = cfg.surfacing
    s.feedback_db_path = tmp_path / "feedback.db"
    s.enabled = True
    # Warm-up off by default in tests: real-_build_engine tests would
    # otherwise eagerly spawn the (default memtomem-server) LTM child at
    # startup. The dedicated warm-up tests opt back in.
    s.warmup_enabled = False
    s.min_response_chars = 10
    s.timeout_seconds = 5.0
    s.min_score = 0.02
    s.cooldown_seconds = 0.0
    s.max_surfacings_per_minute = 1000
    s.auto_tune_enabled = False
    s.include_session_context = False
    s.fire_webhook = False
    return cfg


def _hs_path(cfg: STMConfig) -> Path:
    """This config's keyed handshake path — what the running daemon publishes."""
    return handshake_path(cfg.data_dir, config_fingerprint(cfg))


def _lock_path(cfg: STMConfig) -> Path:
    """This config's keyed lifetime-lock path."""
    from memtomem_stm.daemon.locking import lock_path

    return lock_path(cfg.data_dir, config_fingerprint(cfg))


def _engine_with_result() -> SurfacingEngine:
    adapter = AsyncMock()
    adapter.search = AsyncMock(return_value=([_Result(_Chunk(), 0.5)], [], "ok"))
    config = SurfacingConfig(
        enabled=True,
        min_response_chars=10,
        timeout_seconds=5.0,
        min_score=0.02,
        cooldown_seconds=0.0,
        max_surfacings_per_minute=1000,
        auto_tune_enabled=False,
        include_session_context=False,
        fire_webhook=False,
    )
    return SurfacingEngine(config, mcp_adapter=adapter)


async def _await_handshake(cfg: STMConfig, timeout: float = 3.0) -> None:
    hp = _hs_path(cfg)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if hp.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("daemon did not publish a handshake in time")


async def _start(cfg: STMConfig, engine: SurfacingEngine | None = None):
    server = DaemonServer(cfg)
    if engine is not None:
        # Bypass the real LTM/SQLite wiring; inject a ready engine instead.
        server._build_engine = lambda: setattr(server, "_engine", engine)  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    await _await_handshake(cfg)
    return server, task


async def _stop(cfg: STMConfig, task: asyncio.Task) -> None:
    await client.shutdown(cfg)
    await asyncio.wait_for(task, timeout=5.0)


async def test_ping_reports_ready_and_cold_ltm(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        hs = await client.ping(cfg, timeout=2.0)
        assert hs is not None
        assert hs["ltm"] == "cold"  # injected engine: adapter never started
        assert hs["latency"]["retrieval"]["samples"] == 0
        assert hs["latency"]["surface"]["samples"] == 0
        assert hs["queue"] == {
            "active": 0,
            "in_flight": 0,
            "queued": 0,
            "capacity": cfg.daemon.max_pending_requests,
            "concurrency": cfg.daemon.max_concurrent_ltm_ops,
            "available": cfg.daemon.max_pending_requests,
            "busy_rejections": 0,
        }
    finally:
        await _stop(cfg, task)


async def test_ping_reports_bounded_warm_latency_and_separates_timeouts(tmp_path: Path) -> None:
    server = DaemonServer(_config(tmp_path))
    server._ltm_warmth = lambda: "warm"  # type: ignore[method-assign]

    async def success() -> dict[str, object]:
        return {"v": PROTOCOL_VERSION, "ok": True, "outcome": "empty_results"}

    for _ in range(5):
        await server._run_admitted(
            {"deadline_monotonic": asyncio.get_running_loop().time() + 1.0},
            success,
            latency_kind="retrieval",
        )

    async def hangs() -> dict[str, object]:
        await asyncio.sleep(1.0)
        return {"v": PROTOCOL_VERSION, "ok": True}

    expired = await server._run_admitted(
        {"deadline_monotonic": asyncio.get_running_loop().time() + 0.01},
        hangs,
        latency_kind="retrieval",
    )
    assert expired["status"] == "expired"

    ping = await server._dispatch({"v": PROTOCOL_VERSION, "op": OP_PING})
    assert ping is not None
    retrieval = ping["latency"]["retrieval"]
    assert retrieval["samples"] == 5
    assert retrieval["timeout_samples"] == 1
    assert retrieval["recommendation"]["status"] == "provisional"


async def test_surface_round_trip_injects_memories(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        out = await client.surface(cfg, _canonical(_READ_PAYLOAD), timeout=3.0)
        assert out is not None
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "<surfaced-memories>" in ctx
        # Stage-1 invariant survives the daemon path: no unresolvable prompt.
        assert "surfacing_id" not in ctx
        assert "stm_surfacing_feedback" not in ctx
    finally:
        await _stop(cfg, task)


async def test_surface_rejects_expired_deadline(tmp_path: Path) -> None:
    server = DaemonServer(_config(tmp_path))
    call = _canonical(_READ_PAYLOAD)
    response = await server._dispatch(
        {
            "v": PROTOCOL_VERSION,
            "op": OP_SURFACE,
            "payload": call.to_wire(),
            "deadline_monotonic": asyncio.get_running_loop().time() - 1,
        }
    )
    assert response == {"v": PROTOCOL_VERSION, "ok": False, "status": "expired"}


class _DeadlineSpyEngine:
    """Records the ``deadline_monotonic`` the daemon hands to the engine."""

    injection_mode = "append"

    def __init__(self) -> None:
        self.calls: list[float | None] = []

    async def surface(self, *args, deadline_monotonic: float | None = None, **kwargs) -> str:
        self.calls.append(deadline_monotonic)
        return args[3] if len(args) > 3 else ""


async def test_admission_rejects_unusable_deadlines(tmp_path: Path) -> None:
    # _run_admitted's own validation, not just _surface_deadline's (#721
    # fixed only the latter): NaN passed the expiry comparison (nan <= now is
    # False) and reached asyncio.timeout_at(NaN), polluting the loop's timer
    # heap; +inf was admitted with a backstop that can never fire; and an int
    # too large for a float raised OverflowError out of the dispatch instead
    # of answering. All are the same thing — not a usable monotonic point —
    # and get the same answer a missing or past deadline gets.
    engine = _DeadlineSpyEngine()
    server = DaemonServer(_config(tmp_path))
    server._engine = engine
    for bad in (float("nan"), float("inf"), float("-inf"), 10**400):
        response = await server._dispatch(
            {
                "v": PROTOCOL_VERSION,
                "op": OP_SURFACE,
                "payload": _canonical(_READ_PAYLOAD).to_wire(),
                "deadline_monotonic": bad,
            }
        )
        assert response == {"v": PROTOCOL_VERSION, "ok": False, "status": "expired"}, bad
    assert engine.calls == []  # none of them reached the engine


async def test_surface_propagates_deadline_minus_response_margin(tmp_path: Path) -> None:
    # Without this, the client's deadline cancels surface() from OUTSIDE, which
    # skips the engine's fault/log/breaker bookkeeping (#579) — so the breaker
    # never opens and every call re-pays the timeout and respawns the LTM child.
    # It is an absolute deadline (#720): the engine re-reads the clock right
    # before its LTM attempt, so its own pre-work debits its window, not the
    # response margin — which is also why the value is exact, not approximate.
    engine = _DeadlineSpyEngine()
    server = DaemonServer(_config(tmp_path))
    server._engine = engine
    deadline = asyncio.get_running_loop().time() + 1.0
    response = await server._dispatch(
        {
            "v": PROTOCOL_VERSION,
            "op": OP_SURFACE,
            "payload": _canonical(_READ_PAYLOAD).to_wire(),
            "deadline_monotonic": deadline,
        }
    )

    assert response["ok"] is True
    assert len(engine.calls) == 1
    # Strictly ahead of the client's give-up point: the engine must abort
    # first, leaving room to encode and write the response.
    assert engine.calls[0] == pytest.approx(
        deadline - daemon_server._DEADLINE_RESPONSE_MARGIN_SECONDS, abs=1e-6
    )


async def test_engine_internal_timeout_is_not_a_success_latency_sample(tmp_path: Path) -> None:
    # Under a propagated budget the engine handles its own timeout and returns a
    # well-formed empty result, which is shape-identical to "nothing relevant".
    # Filed as `success` it would censor the percentiles the timeout
    # recommendation derives from with a sample the length of the whole budget.
    server = DaemonServer(_config(tmp_path))
    server._ltm_warmth = lambda: "warm"  # type: ignore[method-assign]
    obs = SurfacingObservability()

    async def surfaced_but_timed_out() -> dict[str, object]:
        # Exactly what the adapter and engine.surface() book when a round trip
        # is issued and then times out, recorded through a real observability
        # so the daemon reads it the way it does in production.
        record_search_rpc()
        obs.record_outcome("Read", "error_timeout")
        return surface_response({})

    await server._run_admitted(
        {"deadline_monotonic": asyncio.get_running_loop().time() + 1.0},
        surfaced_but_timed_out,
        latency_kind="surface",
        attribute=True,
    )

    surface_latency = server._latency.snapshot()["surface"]
    assert surface_latency["timeout_samples"] == 1
    assert surface_latency["samples"] == 0


async def test_pre_work_exhausted_timeout_is_not_a_latency_sample(tmp_path: Path) -> None:
    # The engine books ``error_timeout`` without issuing an RPC when the gate,
    # query extraction and privacy scan consumed the caller's whole window
    # (#720). The daemon used to read that terminal as "a search was attempted"
    # and file a timeout — a duration that measured STM pre-work and queue wait,
    # in a series that is advice about the LTM (#994). No RPC, no sample: not
    # in the percentiles and not in the timeout count either.
    server = DaemonServer(_config(tmp_path))
    server._ltm_warmth = lambda: "warm"  # type: ignore[method-assign]
    obs = SurfacingObservability()

    async def timed_out_before_the_rpc() -> dict[str, object]:
        obs.record_outcome("Read", "error_timeout")
        return surface_response({})

    await server._run_admitted(
        {"deadline_monotonic": asyncio.get_running_loop().time() + 1.0},
        timed_out_before_the_rpc,
        latency_kind="surface",
        attribute=True,
    )

    surface_latency = server._latency.snapshot()["surface"]
    assert surface_latency["samples"] == 0
    assert surface_latency["timeout_samples"] == 0


async def test_failed_session_healing_is_not_a_latency_sample(tmp_path: Path) -> None:
    # ``ltm_unavailable`` is what the engine records when the adapter answered
    # ``no_session`` — session healing failed before any request was sent. A
    # stretch of failed healing used to land every one of those durations in
    # the warm-search series (#994). The same terminal *after* a transport error
    # mid-flight did issue a request, and that one stays a sample.
    server = DaemonServer(_config(tmp_path))
    server._ltm_warmth = lambda: "warm"  # type: ignore[method-assign]
    obs = SurfacingObservability()

    async def healing_failed() -> dict[str, object]:
        obs.record_skip("Read", "ltm_unavailable")
        return surface_response({})

    async def connection_dropped_mid_flight() -> dict[str, object]:
        record_search_rpc()
        obs.record_skip("Read", "ltm_unavailable")
        return surface_response({})

    async def run(operation) -> dict[str, object]:
        await server._run_admitted(
            {"deadline_monotonic": asyncio.get_running_loop().time() + 1.0},
            operation,
            latency_kind="surface",
            attribute=True,
        )
        return server._latency.snapshot()["surface"]

    # Asserted after each call, not once at the end: a totals-only check on a
    # shared tracker passes just as well for the reversed implementation, which
    # files the unmarked call and drops the marked one.
    after_healing_failed = await run(healing_failed)
    assert after_healing_failed["samples"] == 0
    assert after_healing_failed["timeout_samples"] == 0
    assert after_healing_failed["error_samples"] == 0

    after_mid_flight = await run(connection_dropped_mid_flight)
    # The positive control did issue a request, so it is filed -- as an error,
    # not a success duration: the round trip is real LTM time but not a
    # measurement of a search that completed.
    assert after_mid_flight["error_samples"] == 1
    assert after_mid_flight["samples"] == 0
    assert after_mid_flight["timeout_samples"] == 0


async def test_a_faulted_round_trip_is_not_a_success_duration(tmp_path: Path) -> None:
    # The engine returns the caller's text unchanged on a dependency fault, so
    # the daemon's response is ``ok`` and shape-identical to a healthy call that
    # surfaced nothing. Classified from the response alone, every fault whose
    # RPC went out landed in the percentiles that answer how long a *successful*
    # search takes (#994). The ledger is what tells the two apart.
    server = DaemonServer(_config(tmp_path))
    server._ltm_warmth = lambda: "warm"  # type: ignore[method-assign]
    obs = SurfacingObservability()

    async def parse_failed() -> dict[str, object]:
        record_search_rpc()
        obs.record_skip("Read", "ltm_parse_empty")
        return surface_response({})

    async def searched_and_filtered_everything_out() -> dict[str, object]:
        # The healthy control: a completed round trip whose candidates were all
        # filtered out is a search that worked, and stays a success duration.
        record_search_rpc()
        obs.record_skip("Read", "no_results_score")
        return surface_response({})

    async def run(operation) -> dict[str, object]:
        await server._run_admitted(
            {"deadline_monotonic": asyncio.get_running_loop().time() + 1.0},
            operation,
            latency_kind="surface",
            attribute=True,
        )
        return server._latency.snapshot()["surface"]

    after_fault = await run(parse_failed)
    assert after_fault["error_samples"] == 1
    assert after_fault["samples"] == 0

    after_healthy = await run(searched_and_filtered_everything_out)
    assert after_healthy["samples"] == 1
    assert after_healthy["error_samples"] == 1  # unchanged by the healthy call


async def test_a_pre_rpc_fault_is_counted_even_though_it_is_not_sampled(
    tmp_path: Path,
) -> None:
    # Dropping these durations is right -- they measure STM, not the LTM -- but
    # dropping them silently made a daemon whose every request dies during
    # healing read exactly like a daemon nobody used: same zeros in every
    # counter (#994). The engine's own fault counters cannot answer it either,
    # since ``stm_surfacing_stats`` renders the MCP server process's engine and
    # this one lives in the daemon.
    server = DaemonServer(_config(tmp_path))
    server._ltm_warmth = lambda: "warm"  # type: ignore[method-assign]
    obs = SurfacingObservability()

    async def healing_failed() -> dict[str, object]:
        obs.record_skip("Read", "ltm_unavailable")
        return surface_response({})

    async def gated_out() -> dict[str, object]:
        # The control that keeps this counter meaningful: a cooldown skip is
        # not a fault, and surfacing does far more of those than anything else.
        # Counting them would bury the signal under healthy traffic.
        obs.record_skip("Read", "gate_cooldown")
        return surface_response({})

    async def run(operation) -> dict[str, object]:
        await server._run_admitted(
            {"deadline_monotonic": asyncio.get_running_loop().time() + 1.0},
            operation,
            latency_kind="surface",
            attribute=True,
        )
        return server._latency.snapshot()["surface"]

    after_fault = await run(healing_failed)
    assert after_fault["pre_rpc_faults"] == 1
    assert after_fault["samples"] == 0
    assert after_fault["error_samples"] == 0  # not a duration, not an error sample

    after_gate = await run(gated_out)
    assert after_gate["pre_rpc_faults"] == 1  # unchanged by the healthy skip


async def test_gate_skip_is_not_a_latency_sample(tmp_path: Path) -> None:
    # A call the gate rejected never reached the LTM, so its near-zero duration
    # is not a measurement of a warm search. Filed as a sample it would drag the
    # percentiles the hook-timeout recommendation is derived from toward zero.
    server = DaemonServer(_config(tmp_path))
    server._ltm_warmth = lambda: "warm"  # type: ignore[method-assign]
    obs = SurfacingObservability()

    async def gated_out() -> dict[str, object]:
        obs.record_skip("Read", "gate_cooldown")
        return surface_response({})

    await server._run_admitted(
        {"deadline_monotonic": asyncio.get_running_loop().time() + 1.0},
        gated_out,
        latency_kind="surface",
        attribute=True,
    )

    surface_latency = server._latency.snapshot()["surface"]
    assert surface_latency["samples"] == 0
    assert surface_latency["timeout_samples"] == 0


async def test_overlapping_request_does_not_inherit_another_requests_timeout(
    tmp_path: Path,
) -> None:
    # Now that surfacing calls really do overlap (#874), the two of them run at
    # the same time rather than one waiting for the other. A per-request outcome
    # read from the engine's process-global counters would let the healthy call
    # see the slow one's timeout and be filed as a timeout itself — dropping its
    # real duration from the percentiles in exactly the scenario (a slow LTM)
    # where the recommendation matters most.
    server = DaemonServer(_config(tmp_path))
    server._ltm_warmth = lambda: "warm"  # type: ignore[method-assign]
    obs = SurfacingObservability()
    slow_started = asyncio.Event()
    healthy_done = asyncio.Event()

    async def times_out_internally() -> dict[str, object]:
        slow_started.set()
        # Still running while the healthy call records and returns: this is the
        # interleaving, not a queue behind a lock. A daemon that serializes the
        # two deadlocks here instead, so the wait is bounded and the failure is
        # reported as this operation not completing rather than as a hang.
        try:
            await asyncio.wait_for(healthy_done.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            raise AssertionError("the second request never overlapped the first") from None
        record_search_rpc()
        obs.record_outcome("Read", "error_timeout")
        return surface_response({})

    async def healthy() -> dict[str, object]:
        record_search_rpc()
        obs.record_outcome("Read", "surfaced_cache_miss")
        healthy_done.set()
        return surface_response({})

    deadline = asyncio.get_running_loop().time() + 5.0
    first = asyncio.create_task(
        server._run_admitted(
            {"deadline_monotonic": deadline},
            times_out_internally,
            latency_kind="surface",
            attribute=True,
        )
    )
    await slow_started.wait()
    second = asyncio.create_task(
        server._run_admitted(
            {"deadline_monotonic": deadline},
            healthy,
            latency_kind="surface",
            attribute=True,
        )
    )
    slow_response, healthy_response = await asyncio.gather(first, second)

    # Both completed on their own terms — the overlap is what the rest of the
    # assertions are about, so it must not be the thing that failed.
    assert slow_response["ok"] is True
    assert healthy_response["ok"] is True
    surface_latency = server._latency.snapshot()["surface"]
    assert surface_latency["timeout_samples"] == 1  # only the call that timed out
    assert surface_latency["samples"] == 1  # the healthy call kept its duration


async def test_request_expiring_while_waiting_for_a_slot_never_touches_the_engine(
    tmp_path: Path,
) -> None:
    # Shedding at the slot is what makes the wait cheap: a client that has
    # already given up must not start an LTM round trip nobody will read, and
    # cancelling one mid-RPC forces a stdio child respawn on the next call.
    cfg = _config(tmp_path)
    cfg.daemon.max_concurrent_ltm_ops = 1
    server = DaemonServer(cfg)
    engine = _DeadlineSpyEngine()
    server._engine = engine
    holder_may_finish = asyncio.Event()

    async def holds_the_slot() -> dict[str, object]:
        await holder_may_finish.wait()
        return surface_response({})

    holder = asyncio.create_task(
        server._run_admitted(
            {"deadline_monotonic": asyncio.get_running_loop().time() + 5.0},
            holds_the_slot,
        )
    )
    await asyncio.sleep(0)  # let the holder take the only slot

    response = await server._dispatch(
        {
            "v": PROTOCOL_VERSION,
            "op": OP_SURFACE,
            "payload": _canonical(_READ_PAYLOAD).to_wire(),
            "deadline_monotonic": asyncio.get_running_loop().time() + 0.05,
        }
    )
    assert response == {"v": PROTOCOL_VERSION, "ok": False, "status": "expired"}
    assert engine.calls == []

    holder_may_finish.set()
    await holder
    assert server._queue_snapshot()["in_flight"] == 0


async def test_queue_snapshot_counts_every_in_flight_operation(tmp_path: Path) -> None:
    # `in_flight` used to be a lock's own state and could only read 0 or 1, so
    # an operator watching a saturated daemon saw the same number as an idle
    # one. It is the real count now, reported against the bound it is measured
    # against.
    cfg = _config(tmp_path)
    cfg.daemon.max_concurrent_ltm_ops = 2
    server = DaemonServer(cfg)
    both_running = asyncio.Event()
    may_finish = asyncio.Event()
    running = 0

    async def parks() -> dict[str, object]:
        nonlocal running
        running += 1
        if running == 2:
            both_running.set()
        await may_finish.wait()
        return surface_response({})

    deadline = asyncio.get_running_loop().time() + 5.0
    parked = [
        asyncio.create_task(server._run_admitted({"deadline_monotonic": deadline}, parks))
        for _ in range(2)
    ]
    # Bounded: without real overlap the second operation never starts, and an
    # unbounded wait here would hang the suite instead of reporting the defect.
    await asyncio.wait_for(both_running.wait(), timeout=2.0)

    snapshot = server._queue_snapshot()
    assert snapshot["in_flight"] == 2
    assert snapshot["queued"] == 0
    assert snapshot["concurrency"] == 2

    may_finish.set()
    await asyncio.gather(*parked)
    assert server._queue_snapshot()["in_flight"] == 0


class _GateSkippingEngine:
    """Engine stub that records a gate skip, as a rejected call really does."""

    injection_mode = "append"

    def __init__(self) -> None:
        self.observability = SurfacingObservability()

    async def surface(self, *args, **kwargs) -> str:
        self.observability.record_skip("Read", "gate_cooldown")
        return args[3] if len(args) > 3 else ""


async def test_surface_dispatch_asks_for_attribution(tmp_path: Path) -> None:
    # Every other attribution test calls `_run_admitted` directly and passes
    # `attribute=True` itself, so all of them stay green if the production
    # dispatch stops asking for it — and then a gate skip silently becomes a
    # warm-search latency sample. This drives the real OP_SURFACE path.
    server = DaemonServer(_config(tmp_path))
    server._ltm_warmth = lambda: "warm"  # type: ignore[method-assign]
    server._engine = _GateSkippingEngine()

    response = await server._dispatch(
        {
            "v": PROTOCOL_VERSION,
            "op": OP_SURFACE,
            "payload": _canonical(_READ_PAYLOAD).to_wire(),
            "deadline_monotonic": asyncio.get_running_loop().time() + 5.0,
        }
    )

    assert response["ok"] is True
    assert server._latency.snapshot()["surface"]["samples"] == 0


async def test_surface_skips_ltm_when_deadline_leaves_no_budget(tmp_path: Path) -> None:
    # Admitted (deadline not yet expired) but too little left for a round trip.
    # Starting one would only cancel the adapter mid-RPC and force a stdio child
    # respawn on the next call, so the engine must not be touched at all.
    engine = _DeadlineSpyEngine()
    server = DaemonServer(_config(tmp_path))
    server._engine = engine
    starved = (
        daemon_server._DEADLINE_RESPONSE_MARGIN_SECONDS
        + daemon_server._MIN_SURFACE_BUDGET_SECONDS
        - 0.05
    )
    response = await server._dispatch(
        {
            "v": PROTOCOL_VERSION,
            "op": OP_SURFACE,
            "payload": _canonical(_READ_PAYLOAD).to_wire(),
            "deadline_monotonic": asyncio.get_running_loop().time() + starved,
        }
    )

    # Fail-open: a well-formed empty output, not an error the hook must interpret.
    assert response["ok"] is True
    assert response["output"] == {}
    assert engine.calls == []
    # Shedding here leaves the ledger empty, which the classification reads as
    # "no observation" -- so without an explicit count a daemon shedding every
    # request at this floor reports the same zeros as one nobody is calling.
    surface_latency = server._latency.snapshot()["surface"]
    assert surface_latency["pre_rpc_faults"] == 1
    assert surface_latency["samples"] == 0
    assert surface_latency["timeout_samples"] == 0


def test_surface_deadline_rejects_non_finite_deadlines(tmp_path: Path) -> None:
    # NaN and ±inf pass the isinstance check but are not usable monotonic
    # points: +inf would reach the engine as a "real" deadline that its
    # non-finite guard then treats as no deadline at all (the full configured
    # ceiling, behind a client that is not actually infinitely patient), and
    # NaN poisons every comparison it meets. Both mean "don't start LTM work"
    # — the same answer a missing deadline gets.
    # 10**400 is an int the isinstance check accepts but float() cannot
    # represent — math.isfinite would raise OverflowError instead of
    # rejecting it.
    server = DaemonServer(_config(tmp_path))
    for bad in (float("nan"), float("inf"), float("-inf"), 10**400, True, False, "1.0", None):
        assert server._surface_deadline({"deadline_monotonic": bad}) is None, bad


async def test_surface_rejects_when_pending_queue_is_full(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.daemon.max_pending_requests = 1
    server = DaemonServer(cfg)
    await server._pending_slots.acquire()
    call = _canonical(_READ_PAYLOAD)
    response = await server._dispatch(
        {
            "v": PROTOCOL_VERSION,
            "op": OP_SURFACE,
            "payload": call.to_wire(),
            "deadline_monotonic": asyncio.get_running_loop().time() + 1,
        }
    )
    assert response == {"v": PROTOCOL_VERSION, "ok": False, "status": "busy"}
    ping = await server._dispatch({"v": PROTOCOL_VERSION, "op": OP_PING})
    assert ping["queue"]["busy_rejections"] == 1
    assert ping["queue"]["capacity"] == 1


async def test_noop_surface_for_non_allowlisted_tool_real_wiring(tmp_path: Path) -> None:
    # Real _build_engine (FeedbackTracker + lazy LTM adapter). A Write tool is
    # not allowlisted, so run_surfacing_hook returns {} without touching LTM.
    cfg = _config(tmp_path)
    _, task = await _start(cfg)
    try:
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "/x"},
            "tool_response": {"content": _LONG},
        }
        out = await client.surface(cfg, _canonical(payload), timeout=3.0)
        assert out == {}
    finally:
        await _stop(cfg, task)


async def test_bad_token_is_rejected(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        hs = read_handshake(_hs_path(cfg))
        assert hs is not None
        reader, writer = await asyncio.open_connection(
            hs["host"], hs["port"], limit=MAX_MESSAGE_BYTES
        )
        writer.write(
            encode_line(
                build_request("wrong-token", OP_SURFACE, _canonical(_READ_PAYLOAD).to_wire())
            )
        )
        await writer.drain()
        # Server closes the connection without responding to an unauthenticated peer.
        data = await asyncio.wait_for(reader.read(), timeout=3.0)
        assert data == b""
        writer.close()
    finally:
        await _stop(cfg, task)


async def test_server_rejects_mismatched_protocol_version(tmp_path: Path) -> None:
    # An authenticated request carrying a wrong protocol `v` gets an explicit
    # error frame, not action on a payload shape this version may not understand.
    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        hs = read_handshake(_hs_path(cfg))
        assert hs is not None
        reader, writer = await asyncio.open_connection(
            hs["host"], hs["port"], limit=MAX_MESSAGE_BYTES
        )
        # Correct token, wrong version — craft the frame directly (build_request
        # always stamps the current PROTOCOL_VERSION).
        frame = {"v": 999, "token": hs["token"], "op": OP_PING}
        writer.write(encode_line(frame))
        await writer.drain()
        resp = await asyncio.wait_for(read_message(reader), timeout=3.0)
        assert resp["ok"] is False
        assert "version" in resp["error"]
        assert resp["v"] == PROTOCOL_VERSION
        writer.close()
    finally:
        await _stop(cfg, task)


async def test_client_rejects_mismatched_protocol_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A live v2 daemon answers, but a client built at a different protocol
    # version must discard the reply (defense beyond the fingerprint split).
    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        # Sanity: same-version ping works.
        assert await client.ping(cfg, timeout=3.0) is not None
        # Now pretend the client speaks a different protocol version. The daemon
        # still replies with its own v (== real PROTOCOL_VERSION), which the
        # client's response guard rejects → None.
        monkeypatch.setattr("memtomem_stm.daemon.client.PROTOCOL_VERSION", 999)
        assert await client.ping(cfg, timeout=3.0) is None
        assert await client.surface(cfg, _canonical(_READ_PAYLOAD), timeout=3.0) is None
    finally:
        await _stop(cfg, task)


async def test_surface_returns_none_when_daemon_absent(tmp_path: Path) -> None:
    # No daemon → client.surface returns None so the hook degrades to {}.
    cfg = _config(tmp_path)
    out = await client.surface(cfg, _canonical(_READ_PAYLOAD), timeout=0.5)
    assert out is None


async def test_oversized_wire_frame_degrades_to_none(tmp_path: Path) -> None:
    # _bounded_call caps tool_response_text, but to_wire ships tool_input
    # uncapped. If an operator adds write/edit to the surface allowlist, a
    # multi-MB tool_input (file contents / new_string) can push the frame past
    # MAX_MESSAGE_BYTES. The contract (documented in hook_cmd._SAFE_DAEMON_BUDGET)
    # is that this degrades to None — the server's readline limit drops the
    # oversized frame and the client gets no parseable reply — and never raises
    # to the host. A live daemon makes this exercise the server-side drop, not
    # just the daemon-absent path above.
    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        huge = "z" * (MAX_MESSAGE_BYTES + 4096)
        call = _canonical({**_READ_PAYLOAD, "tool_input": {"file_path": "/x", "content": huge}})
        out = await client.surface(cfg, call, timeout=3.0)
        assert out is None  # degraded, no exception raised
    finally:
        await _stop(cfg, task)


async def test_hook_run_hook_routes_to_live_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end: ``_run_hook`` (daemon enabled, env-driven config) reaches a
    # live daemon and returns its surfaced output — the full hook→client→
    # daemon→engine→back path, no LTM (engine injected).
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH", str(tmp_path / "fb.db"))
    monkeypatch.setenv("MEMTOMEM_STM_DAEMON__IDLE_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")

    from memtomem_stm.cli.hook_cmd import _run_hook

    cfg = STMConfig()  # reads the env above → data_dir == tmp_path
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        out = await _run_hook(_canonical(_READ_PAYLOAD))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "<surfaced-memories>" in ctx
    finally:
        await _stop(cfg, task)


async def test_build_engine_dedup_only_wiring(tmp_path: Path) -> None:
    # Default (record_feedback_events=False) → feedback loop off: result cache
    # off so _surfaced_ids/seen_memories dedup is authoritative (F1), AutoTuner
    # off so shared feedback rows can't nudge ranking (F3), query text never
    # persisted raw. The tracker is still wired — for dedup AND for
    # surfacing_events telemetry (see the e2e test below).
    cfg = _config(tmp_path)
    assert cfg.hook.record_feedback_events is False
    server = DaemonServer(cfg)
    server._build_engine()
    try:
        ec = server._engine._config
        assert ec.cache_ttl_seconds == 0.0
        assert ec.auto_tune_enabled is False
        assert ec.persist_query_text is False
        assert server._engine._auto_tuner is None
        assert server._engine._record_feedback_events is False
        assert server._tracker is not None  # tracker still wired for dedup
    finally:
        if server._tracker is not None:
            server._tracker.close()
        await server._adapter.stop()


async def test_build_engine_default_records_surfacing_event_telemetry(tmp_path: Path) -> None:
    # The L0 telemetry gap regression test: with the default daemon wiring
    # (record_feedback_events=False) a successful surfacing must still write a
    # surfacing_events row — server='builtin', digest-substituted query — so
    # stm_surfacing_stats / mms stats / mms doctor see hook-path activity. The
    # rendered output must stay prompt-free (no advertised surfacing_id).
    cfg = _config(tmp_path)
    server = DaemonServer(cfg)
    server._build_engine()
    try:
        adapter = AsyncMock()
        adapter.search = AsyncMock(return_value=([_Result(_Chunk(), 0.5)], [], "ok"))
        server._engine._mcp_adapter = adapter

        out = await server._engine.surface(
            "builtin", "Read", {"file_path": "/src/auth/jwt_handler.py"}, _LONG
        )

        assert "remembered detail about jwt" in out
        assert "stm_surfacing_feedback" not in out
        assert "surfacing_id" not in out
        assert server._tracker is not None
        rows = server._tracker.store._db.execute(
            "SELECT server, query FROM surfacing_events"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "builtin"
        assert rows[0][1].startswith("sha256:")
    finally:
        if server._tracker is not None:
            server._tracker.close()
        await server._adapter.stop()


async def test_handler_write_timeout_drops_stuck_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A peer that sends its request then never reads must not pin the handler
    # task for the process lifetime — the bounded drain drops the connection
    # and the finally block closes the writer.
    server = DaemonServer(_config(tmp_path))
    monkeypatch.setattr(daemon_server, "_WRITE_TIMEOUT_SECONDS", 0.1)

    reader = asyncio.StreamReader()
    reader.feed_data(encode_line(build_request(server._token, OP_PING)))
    reader.feed_eof()

    class _StuckWriter:
        closed = False

        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            await asyncio.Event().wait()  # peer never reads; buffer never drains

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    writer = _StuckWriter()
    await asyncio.wait_for(server._handle_conn(reader, writer), timeout=5.0)
    assert writer.closed


async def test_sub_second_idle_timeout_shuts_down_promptly(tmp_path: Path) -> None:
    # The idle poll used to floor at 1.0s, so idle_timeout=0.2 shut down ~1s+
    # after start (the test below only proves *eventual* shutdown). With the
    # lowered floor the daemon exits near the configured threshold.
    cfg = _config(tmp_path)
    cfg.daemon.idle_timeout_seconds = 0.2
    server = DaemonServer(cfg)
    server._build_engine = lambda: setattr(server, "_engine", _engine_with_result())  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    await _await_handshake(cfg)
    t0 = asyncio.get_running_loop().time()
    await asyncio.wait_for(task, timeout=5.0)
    elapsed = asyncio.get_running_loop().time() - t0
    # Pre-fix lower bound was the 1.0s poll floor; expected now ≈ 0.2-0.4s.
    assert elapsed < 0.9, f"idle shutdown took {elapsed:.2f}s — poll floor regressed?"


async def test_idle_shutdown_stops_daemon(tmp_path: Path) -> None:
    # With a tiny idle timeout and no requests, the daemon shuts itself down
    # and removes its handshake — no leaked warm process after a quiet session.
    cfg = _config(tmp_path)
    cfg.daemon.idle_timeout_seconds = 0.2
    server = DaemonServer(cfg)
    server._build_engine = lambda: setattr(server, "_engine", _engine_with_result())  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    await _await_handshake(cfg)
    await asyncio.wait_for(task, timeout=5.0)  # self-terminates on idle
    assert not _hs_path(cfg).exists()


async def test_hook_run_hook_skips_when_daemon_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Daemon enabled but none running, default fallback=skip → {} (no cold path),
    # AND auto-spawn (default on) kicks off a background spawn for the next call.
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")
    monkeypatch.delenv("MEMTOMEM_STM_HOOK__FALLBACK", raising=False)
    monkeypatch.delenv("MEMTOMEM_STM_HOOK__AUTO_SPAWN", raising=False)

    from memtomem_stm.daemon import spawn

    calls: list[int] = []
    monkeypatch.setattr(spawn, "request_spawn", lambda cfg: bool(calls.append(1)) or True)

    from memtomem_stm.cli.hook_cmd import _run_hook

    out = await _run_hook(_canonical(_READ_PAYLOAD))
    assert out == {}
    assert calls == [1]  # fire-and-forget spawn requested


async def test_hook_run_hook_autospawn_runs_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # request_spawn does blocking work (flock probe + fork/exec); _run_hook
    # must offload it via asyncio.to_thread so the event loop stays free
    # while the outer wait_for budget clock runs.
    import threading

    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")

    from memtomem_stm.daemon import spawn

    threads: list[bool] = []
    monkeypatch.setattr(
        spawn,
        "request_spawn",
        lambda cfg: threads.append(threading.current_thread() is not threading.main_thread()),
    )

    from memtomem_stm.cli.hook_cmd import _run_hook

    out = await _run_hook(_canonical(_READ_PAYLOAD))
    assert out == {}
    assert threads == [True]  # ran in a worker thread, not on the loop thread


async def test_hook_run_hook_no_autospawn_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AUTO_SPAWN=0 → degrade to {} without requesting a spawn.
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__AUTO_SPAWN", "0")

    from memtomem_stm.daemon import spawn

    calls: list[int] = []
    monkeypatch.setattr(spawn, "request_spawn", lambda cfg: calls.append(1))

    from memtomem_stm.cli.hook_cmd import _run_hook

    out = await _run_hook(_canonical(_READ_PAYLOAD))
    assert out == {}
    assert calls == []


async def test_hook_run_hook_autospawn_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A spawn failure must never break the hook — it still degrades to {}.
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")

    from memtomem_stm.daemon import spawn

    def _boom(cfg):
        raise RuntimeError("spawn blew up")

    monkeypatch.setattr(spawn, "request_spawn", _boom)

    from memtomem_stm.cli.hook_cmd import _run_hook

    out = await _run_hook(_canonical(_READ_PAYLOAD))
    assert out == {}


async def test_hook_run_hook_autospawn_with_fallback_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fallback=cold → request a spawn (for next call) AND still run the cold
    # in-process path for THIS call. The two are independent.
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__FALLBACK", "cold")

    import memtomem_stm.cli.hook_cmd as hook_cmd
    from memtomem_stm.daemon import spawn

    spawned: list[int] = []
    monkeypatch.setattr(spawn, "request_spawn", lambda cfg: spawned.append(1))

    cold: list = []

    async def _fake_cold(call):
        cold.append(call)
        return {"cold": True}

    monkeypatch.setattr(hook_cmd, "run_surfacing_hook", _fake_cold)

    out = await hook_cmd._run_hook(_canonical(_READ_PAYLOAD))
    assert spawned == [1]  # spawn kicked off for next call
    # Cold path ran this call with the normalized CanonicalHookCall (Read→read).
    assert len(cold) == 1 and cold[0].tool_name == "Read" and cold[0].canonical_tool == "read"
    assert out == {"cold": True}


async def test_hook_run_hook_no_autospawn_when_daemon_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A live daemon answers surface → no spawn requested (we only spawn on None).
    monkeypatch.setenv("MEMTOMEM_STM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH", str(tmp_path / "fb.db"))
    monkeypatch.setenv("MEMTOMEM_STM_DAEMON__IDLE_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__USE_DAEMON", "1")

    from memtomem_stm.daemon import spawn

    calls: list[int] = []
    monkeypatch.setattr(spawn, "request_spawn", lambda cfg: calls.append(1))

    from memtomem_stm.cli.hook_cmd import _run_hook

    cfg = STMConfig()
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        out = await _run_hook(_canonical(_READ_PAYLOAD))
        assert "<surfaced-memories>" in out["hookSpecificOutput"]["additionalContext"]
        assert calls == []  # live daemon → no duplicate spawn
    finally:
        await _stop(cfg, task)


# ── teardown leak sweep (E-3) ─────────────────────────────────────────────────


_CANCEL_SCOPE_MSG = "Attempted to exit cancel scope in a different task than it was entered in"

_FAKE_LTM_SERVER = Path(__file__).parent.parent / "_fake_memtomem_server.py"


class _StopRaisingAdapter:
    """Adapter stub whose ``stop()`` fails like a cross-task scope exit."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def stop(self) -> None:
        raise self._exc


def _sleeping_child() -> subprocess.Popen:
    """A real direct child mimicking a leaked warm LTM process: its own
    session (like mcp's stdio child) and a sleep only a signal can end."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals/process groups")
@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError(_CANCEL_SCOPE_MSG),
        ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError(_CANCEL_SCOPE_MSG)]),
    ],
    ids=["bare", "group"],
)
async def test_teardown_kills_leaked_child_on_cancel_scope_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exc: BaseException,
) -> None:
    # Legacy/injected adapter shape: the narrow fallback must still sweep the
    # child, but the classified cleanup condition itself is DEBUG noise; the
    # actual leaked-child termination remains an operator-visible warning.
    server = DaemonServer(_config(tmp_path))
    server._adapter = _StopRaisingAdapter(exc)
    child = _sleeping_child()
    try:
        monkeypatch.setattr(daemon_server, "_direct_child_pids", lambda: {child.pid})
        monkeypatch.setattr(daemon_server, "_LEAK_KILL_ESCALATE_SECONDS", 0.2)
        with caplog.at_level(logging.DEBUG, logger="memtomem_stm.daemon.server"):
            await server._teardown()
        assert child.wait(timeout=5.0) == -signal.SIGTERM
        messages = [r.getMessage() for r in caplog.records]
        assert any("known AnyIO cancel-scope cleanup condition" in m for m in messages)
        assert any("leaked LTM child" in m for m in messages)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5.0)


async def test_teardown_sweeps_on_generic_stop_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Any stop() failure leaves the unwind incomplete, so the sweep runs —
    # and kills exactly the children that survived it (before ∩ after).
    server = DaemonServer(_config(tmp_path))
    server._adapter = _StopRaisingAdapter(ValueError("boom"))
    snapshots = iter([{111, 222}, {222}])
    monkeypatch.setattr(daemon_server, "_direct_child_pids", lambda: next(snapshots))
    killed: list[set[int]] = []

    async def _record(pids: set[int]) -> None:
        killed.append(pids)

    monkeypatch.setattr(daemon_server, "_terminate_leaked_children", _record)
    await server._teardown()
    assert killed == [{222}]


async def test_teardown_clean_adapter_stop_still_probes_but_kills_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Since #663 stop() can RETURN normally while abandoning a live child
    # (owner lost mid-lifetime, or the bounded-join timeout), so the sweep
    # must run even on a normal return — it can no longer be keyed off an
    # exception. A genuinely clean stop reaps its child, so the post-stop
    # snapshot is empty and nothing is killed.
    server = DaemonServer(_config(tmp_path))
    server._adapter = AsyncMock()
    probes: list[int] = []
    monkeypatch.setattr(daemon_server, "_direct_child_pids", lambda: probes.append(1) or set())
    killed: list[set[int]] = []

    async def _record(pids: set[int]) -> None:
        killed.append(pids)

    monkeypatch.setattr(daemon_server, "_terminate_leaked_children", _record)
    await server._teardown()
    assert probes == [1, 1]  # both the pre- and post-stop snapshots
    assert killed == []  # nothing survived a clean stop


async def test_teardown_sweeps_leaked_child_on_normal_stop_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #663 round-4: stop() returns normally but abandons a live child (owner
    # lost / bounded-join timeout). The sweep must still terminate a child
    # present both before and after the normal-return stop().
    server = DaemonServer(_config(tmp_path))
    server._adapter = AsyncMock()  # stop() returns normally, no raise
    snapshots = iter([{111, 222}, {222}])  # 222 survives the abandoning stop
    monkeypatch.setattr(daemon_server, "_direct_child_pids", lambda: next(snapshots))
    killed: list[set[int]] = []

    async def _record(pids: set[int]) -> None:
        killed.append(pids)

    monkeypatch.setattr(daemon_server, "_terminate_leaked_children", _record)
    await server._teardown()
    assert killed == [{222}]


def test_direct_child_pids_delegates_to_the_shared_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The behaviour lives in tests/test_child_reaper.py; what the daemon needs
    # pinned is that its own name is a live delegation rather than a value
    # captured at import, so stubbing the shared probe reaches its sweep too.
    monkeypatch.setattr(child_reaper, "probe_child_pids", lambda: {4242})
    assert daemon_server._direct_child_pids() == {4242}


@pytest.mark.real_child_sweep
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process probes")
async def test_real_teardown_reaps_warm_ltm_child(tmp_path: Path) -> None:
    # E-3 end-to-end: real engine wiring with a stdio LTM (the fake memtomem
    # server). The lazy LTM start happens inside a connection-handler task,
    # so daemon shutdown exits the transport's cancel scopes from the serve
    # task — whatever path that unwind takes, no live LTM child may survive.
    cfg = _config(tmp_path)
    cfg.surfacing.timeout_seconds = 15.0
    cfg.surfacing.ltm_mcp_command = sys.executable
    cfg.surfacing.ltm_mcp_args = [str(_FAKE_LTM_SERVER)]
    before = daemon_server._direct_child_pids()
    _, task = await _start(cfg)  # real _build_engine
    try:
        out = await client.surface(cfg, _canonical(_READ_PAYLOAD), timeout=15.0)
        assert out is not None
        ltm_children = daemon_server._direct_child_pids() - before
        assert ltm_children  # the surface call warmed a real stdio LTM child
    finally:
        await client.shutdown(cfg)
        await asyncio.wait_for(task, timeout=20.0)
    deadline = asyncio.get_running_loop().time() + 5.0
    while any(is_pid_alive(pid) for pid in ltm_children):
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"leaked LTM child process(es) after teardown: {ltm_children}")
        await asyncio.sleep(0.1)


# ── burst concurrency against a real warm LTM (#874) ──────────────────────────


def _burst_config(tmp_path: Path, concurrency: int) -> STMConfig:
    """A daemon wired to a real stdio LTM child that takes 0.3s per search."""
    cfg = _config(tmp_path)
    cfg.daemon.max_concurrent_ltm_ops = concurrency
    cfg.surfacing.timeout_seconds = 15.0
    cfg.surfacing.ltm_mcp_command = sys.executable
    cfg.surfacing.ltm_mcp_args = [str(_FAKE_LTM_SERVER), "--search-delay", str(_BURST_SEARCH_SECONDS)]
    return cfg


_BURST_SEARCH_SECONDS = 0.3

# Distinct *queries*, not just distinct paths. Identical queries serialize on
# the engine's per-key stampede lock however much concurrency the daemon
# allows, so a burst of those would prove nothing about #874 -- and a numeric
# suffix does not make them distinct, because `_tokenize_path` drops purely
# numeric segments (`jwt_handler_1.py` and `jwt_handler_2.py` both extract to
# "src auth jwt handler py").
_BURST_PATH_WORDS = (
    "auth",
    "billing",
    "catalog",
    "delivery",
    "events",
    "fulfilment",
    "gateway",
    "inventory",
)
_BURST_SIZE = len(_BURST_PATH_WORDS)


async def _warm_and_burst(cfg: STMConfig) -> tuple[DaemonServer, list[dict | None], float]:
    """Warm the LTM child, then fire a burst of distinct read hooks at once."""
    server, task = await _start(cfg)  # real _build_engine
    try:
        # The first call pays process start; the burst is what is being timed.
        assert await client.surface(cfg, _canonical(_READ_PAYLOAD), timeout=15.0) is not None
        calls = [
            _canonical(
                {
                    **_READ_PAYLOAD,
                    "tool_input": {"file_path": f"/src/{word}/handler.py"},
                }
            )
            for word in _BURST_PATH_WORDS
        ]
        started = asyncio.get_running_loop().time()
        outputs = await asyncio.gather(
            *(client.surface(cfg, call, timeout=2.5) for call in calls)
        )
        return server, list(outputs), asyncio.get_running_loop().time() - started
    finally:
        await _stop(cfg, task)


@pytest.mark.skipif(sys.platform == "win32", reason="stdio child + timing")
async def test_a_burst_of_distinct_hook_calls_overlaps_on_the_warm_ltm(tmp_path: Path) -> None:
    # The shape from the live fault data: a tool burst arrives inside one
    # client deadline. Serialized, the tail of the burst reaches the engine
    # with nothing left of its budget and books timeouts that open the breaker
    # for every tool. Overlapped, the whole burst costs about one search.
    cfg = _burst_config(tmp_path, concurrency=4)
    server, outputs, elapsed = await _warm_and_burst(cfg)

    # `{}` is the fail-open shape for an engine timeout, an adapter error, or
    # nothing found, so "not None" would pass while every search failed.
    for out in outputs:
        assert out, "the daemon dropped part of the burst"
        context = out["hookSpecificOutput"]["additionalContext"]
        assert "<surfaced-memories>" in context, context[:200]
    # Two waves of four searches, not eight in a row.
    assert elapsed < _BURST_SEARCH_SECONDS * 4, f"burst took {elapsed:.2f}s"
    # And no request paid for another's wait.
    outcomes = server._engine.observability.snapshot()["outcomes"]["__total__"]
    assert outcomes.get("error_timeout", 0) == 0, outcomes


@pytest.mark.skipif(sys.platform == "win32", reason="stdio child + timing")
async def test_a_serialized_daemon_spends_the_whole_deadline_on_the_same_burst(
    tmp_path: Path,
) -> None:
    # The bug, pinned: with the escape-hatch value the same burst costs the sum
    # of its searches, which is what pushed the tail past the 2.5s client
    # deadline. Asserted against the burst's own arithmetic rather than a wall
    # clock guess -- this is the arm the fix is measured against.
    cfg = _burst_config(tmp_path, concurrency=1)
    _, outputs, elapsed = await _warm_and_burst(cfg)

    assert elapsed > _BURST_SEARCH_SECONDS * _BURST_SIZE * 0.5, f"burst took {elapsed:.2f}s"
    # And the tail pays for it: some of the burst gets nothing back.
    assert any(out is None or out == {} for out in outputs)


# ── startup warm-up (#664 PR 2) ───────────────────────────────────────────────


async def test_daemon_warmup_warms_ltm_without_a_surface_call(tmp_path: Path) -> None:
    # End-to-end for #664 PR 2: with warmup_enabled the daemon's startup task
    # warms the (fake) stdio LTM child on its own — no hook/surface traffic.
    # Once it publishes the session (polled below), a call meets a warm
    # session; the warm-up only pre-pays the cold start, it does not
    # guarantee the very first call arrives after it completes.
    cfg = _config(tmp_path)
    cfg.surfacing.warmup_enabled = True
    cfg.surfacing.ltm_mcp_command = sys.executable
    cfg.surfacing.ltm_mcp_args = [str(_FAKE_LTM_SERVER)]
    server, task = await _start(cfg)  # real _build_engine
    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        while getattr(server._adapter, "_session", None) is None:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("warm-up never published an LTM session")
            await asyncio.sleep(0.05)
        hs = await client.ping(cfg, timeout=2.0)
        assert hs is not None
        assert hs["ltm"] == "warm"
    finally:
        await _stop(cfg, task)


async def test_daemon_skips_warmup_when_disabled(tmp_path: Path) -> None:
    cfg = _config(tmp_path)  # warmup_enabled=False from the helper
    cfg.surfacing.ltm_mcp_command = sys.executable
    cfg.surfacing.ltm_mcp_args = [str(_FAKE_LTM_SERVER)]
    server, task = await _start(cfg)  # real _build_engine
    try:
        await asyncio.sleep(0.3)  # a would-be warm-up gets plenty of turns
        assert getattr(server._adapter, "_session", None) is None
        hs = await client.ping(cfg, timeout=2.0)
        assert hs is not None
        assert hs["ltm"] == "cold"
    finally:
        await _stop(cfg, task)


class _WarmthStub:
    """Plain attribute stub for _ltm_warmth — NOT AsyncMock/MagicMock, whose
    auto-created attributes are truthy and would false-positive ``warming``."""

    def __init__(self, session: object | None, warming: bool, start_attempted: bool) -> None:
        self._session = session
        self.warming = warming
        self._start_attempted = start_attempted


async def test_ltm_warmth_priority(tmp_path: Path) -> None:
    # warm > warming > down > cold (#664). "warming beats down" is the fix
    # for the PR-1-deferred misreport: an in-flight lazy start has
    # _start_attempted=True and must read "warming", not "down".
    server = DaemonServer(_config(tmp_path))
    cases = [
        (_WarmthStub(session=object(), warming=True, start_attempted=True), "warm"),
        (_WarmthStub(session=None, warming=True, start_attempted=True), "warming"),
        (_WarmthStub(session=None, warming=False, start_attempted=True), "down"),
        (_WarmthStub(session=None, warming=False, start_attempted=False), "cold"),
    ]
    for stub, expected in cases:
        server._adapter = stub  # type: ignore[assignment]
        assert server._ltm_warmth() == expected, expected


# ── lifetime ownership lock ───────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="same-process flock contention")
async def test_second_daemon_returns_without_building_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # While daemon 1 owns the lifetime lock, a second daemon must exit 0 WITHOUT
    # building an engine (the load-bearing ordering: no orphaned warm LTM).
    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        monkeypatch.setattr(DaemonServer, "_LOCK_ACQUIRE_RETRY_SECONDS", 0.2)
        d2 = DaemonServer(cfg)
        built: list[int] = []
        d2._build_engine = lambda: built.append(1)  # type: ignore[method-assign]
        rc = await d2.serve()
        assert rc == 0
        assert built == []  # loser never warms an engine
        assert d2._handshake_written is False
    finally:
        await _stop(cfg, task)


@pytest.mark.skipif(sys.platform == "win32", reason="same-process flock contention")
async def test_daemon_holds_lock_for_lifetime(tmp_path: Path) -> None:
    from memtomem_stm.daemon.locking import single_owner_lock

    cfg = _config(tmp_path)
    _, task = await _start(cfg, engine=_engine_with_result())
    try:
        with single_owner_lock(_lock_path(cfg)) as got:
            assert got is False  # held by the serving daemon
    finally:
        await _stop(cfg, task)
    # Released on teardown → acquirable again.
    with single_owner_lock(_lock_path(cfg)) as got:
        assert got is True


@pytest.mark.skipif(sys.platform == "win32", reason="same-process flock contention")
async def test_lifetime_lock_retry_acquires_after_incumbent_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A late/losing daemon retries the acquire and becomes the owner the instant
    # the incumbent releases — no dead window, and it builds exactly once after.
    from memtomem_stm.daemon import locking

    cfg = _config(tmp_path)
    monkeypatch.setattr(DaemonServer, "_LOCK_ACQUIRE_RETRY_SECONDS", 3.0)

    fd = locking.open_lock_fd(_lock_path(cfg))
    assert locking.try_lock(fd) is True  # hold it like a (mock) incumbent

    server = DaemonServer(cfg)
    server._build_engine = lambda: setattr(server, "_engine", _engine_with_result())  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.2)  # daemon is now retrying the acquire
    assert not _hs_path(cfg).exists()  # hasn't built/published yet
    locking.release_lock(fd)  # incumbent leaves
    try:
        await _await_handshake(cfg)  # proves it acquired → built → published
    finally:
        await _stop(cfg, task)


# ── run() top-level exception barrier (#581) ─────────────────────────────


def test_run_logs_traceback_on_startup_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A crash inside serve() (e.g. _build_engine / start_server) must reach the
    logger — for a detached daemon its stderr is DEVNULL, so without the barrier
    the traceback would be lost and stm-daemon.log (the log the start hint points
    at) would stay empty."""
    cfg = _config(tmp_path)

    async def _boom(self):
        raise ValueError("engine build blew up")

    monkeypatch.setattr(DaemonServer, "serve", _boom)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValueError, match="engine build blew up"):
            daemon_server.run(cfg)

    assert "daemon terminated with an unhandled exception" in caplog.text
    # The original traceback is attached (exc_info), not just the message.
    assert any(r.exc_info for r in caplog.records if r.levelno >= logging.ERROR)


def test_run_ignores_clean_cancel_scope_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean anyio cancel-scope teardown is not a crash: the barrier swallows
    it, returns 0, and does not re-raise."""
    cfg = _config(tmp_path)

    async def _clean_teardown(self):
        raise RuntimeError("Attempted to exit cancel scope in a different task")

    monkeypatch.setattr(DaemonServer, "serve", _clean_teardown)
    monkeypatch.setattr(daemon_server, "is_clean_cancel_scope_shutdown", lambda _e: True)

    rc = daemon_server.run(cfg)
    assert rc == 0
