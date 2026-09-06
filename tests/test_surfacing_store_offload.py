"""Surfacing keeps the event loop while the feedback DB is held by a peer.

``stm_feedback.db`` is shared with the proxy's own stores, ``mms tune``, and
any second STM process, so a write waiting out one of them spends up to the
connection's ``busy_timeout`` inside a blocking call. Before #996 that call ran
on the event loop: every other in-flight surfacing froze with it, and so did
the ``asyncio.timeout_at`` timers meant to shed the ones that had already lost
their client. These tests pin the three things the move to a worker thread has
to deliver — a loop that keeps running, a timer that no longer mistakes a
contended write for a hung LTM, and writes that survive the cancellation that
mistake used to cause.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.feedback import FeedbackTracker
from memtomem_stm.surfacing.store_io import (
    MAX_QUEUED_WRITES,
    close_store_on_worker,
    queued_writes,
    run_off_loop,
    submit_store_write,
)

LONG_RESPONSE = "x" * 500


@dataclass
class FakeChunkMeta:
    source_file: Path = Path("/notes/test.md")
    namespace: str = "default"


@dataclass
class FakeChunk:
    id: str = ""
    content: str = "a memory worth surfacing"
    metadata: FakeChunkMeta | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid4())
        if self.metadata is None:
            self.metadata = FakeChunkMeta()


@dataclass
class FakeResult:
    chunk: FakeChunk = field(default_factory=FakeChunk)
    score: float = 0.9


def _config(tmp_path: Path, **overrides: Any) -> SurfacingConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "min_response_chars": 10,
        "timeout_seconds": 5.0,
        "min_score": 0.02,
        "max_results": 3,
        "cooldown_seconds": 0.0,
        "max_surfacings_per_minute": 1000,
        "auto_tune_enabled": False,
        "include_session_context": False,
        "fire_webhook": False,
        "cache_ttl_seconds": 0.0,
        "query_retention_days": 0,
        "stats_retention_days": 0,
        "feedback_db_path": tmp_path / "offload.db",
    }
    defaults.update(overrides)
    return SurfacingConfig(**defaults)


def _args(query: str) -> dict[str, str]:
    """The shape the proxy forwards: a tool argument plus the agent's query.

    Padded because the extractor drops a query too short to retrieve on, and
    each caller needs a DISTINCT one — the engine's per-key lock would
    otherwise collapse two of these into one surfacing.
    """
    return {"path": "src/app.py", "_context_query": f"{query} web framework architecture notes"}


def _adapter() -> AsyncMock:
    adapter = AsyncMock()
    adapter.search = AsyncMock(return_value=([FakeResult()], [], "ok"))
    return adapter


def _engine(config: SurfacingConfig, tracker: Any) -> SurfacingEngine:
    return SurfacingEngine(config, mcp_adapter=_adapter(), feedback_tracker=tracker)


def _hold_write_lock(db_path: Path) -> sqlite3.Connection:
    """Take the file's write lock the way a peer process would."""
    holder = sqlite3.connect(str(db_path))
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "INSERT INTO seen_memories (memory_id, first_seen_at, last_seen_at, seen_count)"
        " VALUES ('mem-peer', 1.0, 1.0, 1)"
    )
    return holder


def _event_count(db_path: Path) -> int:
    db = sqlite3.connect(str(db_path))
    try:
        return int(db.execute("SELECT COUNT(*) FROM surfacing_events").fetchone()[0])
    finally:
        db.close()


class _Heartbeat:
    """Measures how long the loop went without running a ready callback."""

    def __init__(self) -> None:
        self.max_gap = 0.0
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> _Heartbeat:
        loop = asyncio.get_running_loop()

        async def beat() -> None:
            last = loop.time()
            while True:
                await asyncio.sleep(0.01)
                now = loop.time()
                self.max_gap = max(self.max_gap, now - last)
                last = now

        self._task = asyncio.create_task(beat())
        await asyncio.sleep(0.05)  # let it establish a baseline
        self.max_gap = 0.0
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


async def test_a_contended_write_does_not_freeze_the_loop(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tracker = FeedbackTracker(config)
    engine = _engine(config, tracker)
    holder = _hold_write_lock(config.feedback_db_path)
    released = False
    try:
        async with _Heartbeat() as heartbeat:
            surfacing = asyncio.create_task(
                engine.surface("gh", "read_file", _args("held query"), LONG_RESPONSE)
            )
            # Long enough that an on-loop write would show up as a gap far
            # past the heartbeat's 10 ms period.
            await asyncio.sleep(0.4)
            gap_while_blocked = heartbeat.max_gap
            holder.rollback()
            holder.close()
            released = True
            await asyncio.wait_for(surfacing, timeout=10.0)

        assert gap_while_blocked < 0.15, (
            f"the loop stalled {gap_while_blocked:.2f}s while the DB was held"
        )
        await engine.drain_store_writes()
        assert _event_count(config.feedback_db_path) == 1
    finally:
        if not released:
            holder.rollback()
            holder.close()
        await engine.stop()
        tracker.close()


async def test_a_contended_write_is_not_booked_as_an_ltm_timeout(tmp_path: Path) -> None:
    # The surfacing timer exists to abort a hung LTM and charge the breaker for
    # it. Once the round trip has returned, a write waiting out a peer is STM's
    # own time, and three of these would otherwise open the breaker on a core
    # that answered every call inside its window.
    config = _config(tmp_path, timeout_seconds=0.2)
    tracker = FeedbackTracker(config)
    from memtomem_stm.surfacing.observability import SurfacingObservability

    observability = SurfacingObservability()
    engine = SurfacingEngine(
        config,
        mcp_adapter=_adapter(),
        feedback_tracker=tracker,
        observability=observability,
    )
    holder = _hold_write_lock(config.feedback_db_path)
    released = False
    try:
        surfacing = asyncio.create_task(
            engine.surface("gh", "read_file", _args("slow write"), LONG_RESPONSE)
        )
        await asyncio.sleep(0.4)  # twice the engine's own timeout
        holder.rollback()
        holder.close()
        released = True
        output = await asyncio.wait_for(surfacing, timeout=10.0)

        outcomes = observability.snapshot()["outcomes"].get("read_file", {})
        assert "error_timeout" not in outcomes, outcomes
        assert engine._circuit_breaker.failure_count == 0
        assert output != LONG_RESPONSE, "the memory should still have been injected"
        await engine.drain_store_writes()
        assert _event_count(config.feedback_db_path) == 1
    finally:
        if not released:
            holder.rollback()
            holder.close()
        await engine.stop()
        tracker.close()


class _GatedTracker:
    """Wraps a tracker so one write can be held inside the worker thread."""

    def __init__(self, inner: FeedbackTracker, method: str) -> None:
        self._inner = inner
        self._method = method
        self.entered = asyncio.Event()
        self._loop = asyncio.get_event_loop()
        self.release = __import__("threading").Event()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name != self._method:
            return attr

        def gated(*args: Any, **kwargs: Any) -> Any:
            self._loop.call_soon_threadsafe(self.entered.set)
            self.release.wait(timeout=10.0)
            return attr(*args, **kwargs)

        return gated


async def test_a_cancelled_call_still_lands_the_row_it_already_claimed(tmp_path: Path) -> None:
    # By the time the event row is queued, the delivered IDs are already in
    # ``_surfaced_ids`` and the gate's cooldown has been recorded. A write that
    # has not STARTED yet is cancellable — one already running is not — so
    # this parks the worker on an unrelated write first, which is what puts
    # the surfacing's own write in the cancellable state. Dropping it there
    # would leave this process suppressing memories the client never received,
    # with no row to show for it.
    config = _config(tmp_path)
    tracker = FeedbackTracker(config)
    engine = _engine(config, tracker)
    blocker = threading.Event()
    submit_store_write(lambda: blocker.wait(timeout=10.0))
    try:
        surfacing = asyncio.create_task(
            engine.surface("gh", "read_file", _args("cancelled"), LONG_RESPONSE)
        )
        await asyncio.sleep(0.1)
        assert engine._surfaced_ids, "the call must have claimed its IDs and be at the write"

        surfacing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await surfacing
        assert _event_count(config.feedback_db_path) == 0, "still queued behind the blocker"

        blocker.set()
        await engine.drain_store_writes()
        assert _event_count(config.feedback_db_path) == 1
    finally:
        blocker.set()
        await engine.stop()
        tracker.close()


async def test_a_claim_made_during_a_recovery_write_is_not_released_by_it(
    tmp_path: Path,
) -> None:
    # The recovery closes the episodes of the keys the breaker had blocked when
    # it was queued. A key blocked while that write is in flight has no
    # recovery row of its own, so clearing the whole set — which is what the
    # inline version did — would drop a claim that still owes an UPDATE.
    config = _config(tmp_path)
    tracker = FeedbackTracker(config)
    gated = _GatedTracker(tracker, "record_fault_recoveries")
    engine = _engine(config, gated)
    try:
        engine._persist_fault("gh", "early_tool", "circuit_open")
        assert set(engine._breaker_blocked_keys) == {("gh", "early_tool")}

        engine._persist_fault_recovery("gh", "read_file", time.time())
        await asyncio.wait_for(gated.entered.wait(), timeout=10.0)
        engine._persist_fault("gh", "late_tool", "circuit_open")

        gated.release.set()
        await engine.drain_store_writes()
        assert set(engine._breaker_blocked_keys) == {("gh", "late_tool")}
    finally:
        gated.release.set()
        await engine.stop()
        tracker.close()


async def test_a_key_reblocked_during_the_recovery_keeps_its_new_claim(
    tmp_path: Path,
) -> None:
    # The same key, not a different one: the recovery was queued to close the
    # episode of claim N, and while it sat there the breaker blocked that key
    # again under claim N+1. Releasing by key alone would drop N+1, whose own
    # ``circuit_open`` row this recovery never closed — leaving a fault row
    # active that no later probe knows to recover.
    config = _config(tmp_path)
    tracker = FeedbackTracker(config)
    gated = _GatedTracker(tracker, "record_fault_recoveries")
    engine = _engine(config, gated)
    try:
        engine._persist_fault("gh", "read_file", "circuit_open")
        first_claim = engine._breaker_blocked_keys[("gh", "read_file")]

        engine._persist_fault_recovery("gh", "other_tool", time.time())
        await asyncio.wait_for(gated.entered.wait(), timeout=10.0)
        engine._persist_fault("gh", "read_file", "circuit_open")
        assert engine._breaker_blocked_keys[("gh", "read_file")] != first_claim

        gated.release.set()
        await engine.drain_store_writes()
        assert set(engine._breaker_blocked_keys) == {("gh", "read_file")}
    finally:
        gated.release.set()
        await engine.stop()
        tracker.close()


async def test_a_write_stuck_behind_the_queue_stops_holding_the_response(
    tmp_path: Path,
) -> None:
    # The worker is one FIFO thread, so an awaited write waits for whatever was
    # queued ahead of it — another call's retention sweep, a burst of fault
    # counters. With the surfacing timer disarmed past the LTM round trip, and
    # with no caller deadline at all on the proxy path, nothing else would
    # bound that wait. The call gives up on the row and degrades the way it
    # does for a failed write.
    config = _config(tmp_path, timeout_seconds=0.3)
    tracker = FeedbackTracker(config)
    engine = _engine(config, tracker)
    blocker = threading.Event()
    submit_store_write(lambda: blocker.wait(timeout=10.0))
    try:
        started = asyncio.get_running_loop().time()
        output = await asyncio.wait_for(
            engine.surface("gh", "read_file", _args("stuck"), LONG_RESPONSE), timeout=5.0
        )
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 2.0, f"the response waited {elapsed:.2f}s on a queued write"
        # Content still delivered; only the feedback handle is withdrawn,
        # because this process cannot say whether the row will land.
        assert output != LONG_RESPONSE
        assert "stm_surfacing_feedback" not in output

        blocker.set()
        await engine.drain_store_writes()
        assert _event_count(config.feedback_db_path) == 1, "the write still lands"
    finally:
        blocker.set()
        await engine.stop()
        tracker.close()


async def test_a_busy_store_declines_the_rating_instead_of_raising(tmp_path: Path) -> None:
    # The rating write is bounded like the event row's. Past the ceiling the
    # agent gets a sentence it can act on — and specifically one telling it not
    # to re-submit, because the shielded write is still on its way.
    config = _config(tmp_path, timeout_seconds=0.3)
    tracker = FeedbackTracker(config)
    engine = _engine(config, tracker)
    blocker = threading.Event()
    try:
        await engine.surface("gh", "read_file", _args("rate me"), LONG_RESPONSE)
        await engine.drain_store_writes()
        db = sqlite3.connect(str(config.feedback_db_path))
        try:
            surfacing_id = db.execute("SELECT id FROM surfacing_events").fetchone()[0]
        finally:
            db.close()

        submit_store_write(lambda: blocker.wait(timeout=10.0))
        result = await asyncio.wait_for(
            engine.handle_feedback(surfacing_id, "helpful"), timeout=5.0
        )
        assert result.startswith("Error: the feedback store is busy")

        blocker.set()
        await engine.drain_store_writes()
        db = sqlite3.connect(str(config.feedback_db_path))
        try:
            assert db.execute("SELECT COUNT(*) FROM surfacing_feedback").fetchone()[0] == 1
        finally:
            db.close()
    finally:
        blocker.set()
        await engine.stop()
        tracker.close()


async def test_a_store_closed_under_the_call_withdraws_the_feedback_id(
    tmp_path: Path,
) -> None:
    # Teardown can close the store while a call is still in flight: its
    # queued write then runs against a closed store and takes the documented
    # no-op path. A no-op that reads as success would leave the agent holding
    # a rating prompt for an event row that was never written.
    config = _config(tmp_path)
    tracker = FeedbackTracker(config)
    engine = _engine(config, tracker)
    try:
        tracker.close()
        output = await engine.surface("gh", "read_file", _args("closed store"), LONG_RESPONSE)

        assert output != LONG_RESPONSE, "the memories are still delivered"
        assert "stm_surfacing_feedback" not in output, "no handle for a row that does not exist"
    finally:
        await engine.stop()


async def test_a_negative_rating_still_filters_the_cache_when_the_write_times_out(
    tmp_path: Path,
) -> None:
    # The rating is shielded and lands later, so the agent's rejection is
    # real. Skipping the in-memory filter because this call could not confirm
    # the row would let the memory it just rejected come straight back out of
    # a warm cache entry.
    config = _config(tmp_path, timeout_seconds=0.3, cache_ttl_seconds=60.0)
    tracker = FeedbackTracker(config)
    engine = _engine(config, tracker)
    blocker = threading.Event()
    try:
        await engine.surface("gh", "read_file", _args("rate me down"), LONG_RESPONSE)
        await engine.drain_store_writes()
        db = sqlite3.connect(str(config.feedback_db_path))
        try:
            surfacing_id, memory_ids = db.execute(
                "SELECT id, memory_ids FROM surfacing_events"
            ).fetchone()
        finally:
            db.close()
        memory_id = json.loads(memory_ids)[0]

        submit_store_write(lambda: blocker.wait(timeout=10.0))
        result = await asyncio.wait_for(
            engine.handle_feedback(surfacing_id, "not_relevant", memory_id), timeout=5.0
        )

        assert result.startswith("Error: the feedback store is busy")
        assert ("gh", "read_file", memory_id) in engine._invalidated_ids
    finally:
        blocker.set()
        await engine.stop()
        tracker.close()


async def test_the_best_effort_queue_stops_growing_at_its_ceiling(tmp_path: Path) -> None:
    # A store nobody can write to — a peer holding it for minutes, a dead
    # disk — would otherwise let this queue grow at call rate, since every
    # failure branch adds to it and nothing waits for the result.
    config = _config(tmp_path)
    tracker = FeedbackTracker(config)
    engine = _engine(config, tracker)
    blocker = threading.Event()
    submit_store_write(lambda: blocker.wait(timeout=10.0))
    try:
        for index in range(MAX_QUEUED_WRITES + 10):
            engine._persist_fault("gh", f"tool{index}", "error_timeout")
        assert queued_writes() == MAX_QUEUED_WRITES

        blocker.set()
        await engine.drain_store_writes()
        # Everything that was queued still landed; only the excess was dropped.
        db = sqlite3.connect(str(config.feedback_db_path))
        try:
            rows = db.execute("SELECT COUNT(*) FROM surfacing_faults").fetchone()[0]
        finally:
            db.close()
        # One slot went to the blocker itself, so the ceiling covers it too.
        assert rows == MAX_QUEUED_WRITES - 1
    finally:
        blocker.set()
        await engine.stop()
        tracker.close()


async def test_a_cancelled_call_gives_back_the_ids_it_reserved(tmp_path: Path) -> None:
    # The reservation is made before the event write so a concurrent call
    # cannot surface the same memories. A cancellation there means nothing
    # reaches the client — not the manifest, not the feedback ID — so holding
    # the reservation would suppress memories nobody saw for the rest of the
    # session.
    config = _config(tmp_path)
    tracker = FeedbackTracker(config)
    engine = _engine(config, tracker)
    blocker = threading.Event()
    submit_store_write(lambda: blocker.wait(timeout=10.0))
    try:
        surfacing = asyncio.create_task(
            engine.surface("gh", "read_file", _args("reserved"), LONG_RESPONSE)
        )
        await asyncio.sleep(0.1)
        assert engine._surfaced_ids, "the call must have reserved its IDs by now"

        surfacing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await surfacing
        assert engine._surfaced_ids == {}, "an undelivered call keeps no reservation"
    finally:
        blocker.set()
        await engine.stop()
        tracker.close()


async def test_an_awaited_write_also_refuses_to_join_a_full_queue(tmp_path: Path) -> None:
    # The ceiling covers awaited writes too, or a wedged store would let event
    # rows pile up at call rate: every caller stops waiting after its own
    # timeout, but what it queued stays. Refusing routes the call through the
    # same degradation a failed write gets — content delivered, no prompt.
    config = _config(tmp_path)
    tracker = FeedbackTracker(config)
    engine = _engine(config, tracker)
    blocker = threading.Event()
    for _ in range(MAX_QUEUED_WRITES):
        submit_store_write(lambda: blocker.wait(timeout=10.0))
    try:
        assert queued_writes() == MAX_QUEUED_WRITES
        output = await asyncio.wait_for(
            engine.surface("gh", "read_file", _args("refused"), LONG_RESPONSE), timeout=5.0
        )

        assert output != LONG_RESPONSE, "the memories are still delivered"
        assert "stm_surfacing_feedback" not in output
    finally:
        blocker.set()
        await engine.stop()
        tracker.close()


async def test_teardown_is_not_held_open_by_a_blocked_write(tmp_path: Path) -> None:
    # Closing from the event loop takes the store's writer lock, which an
    # in-flight write holds for as long as its statement runs — a peer holding
    # the database, a wide retention DELETE. Queueing the close on the same
    # worker makes it the last operation instead of a competing one, and the
    # wait for it is bounded.
    config = _config(tmp_path)
    tracker = FeedbackTracker(config)
    blocker = threading.Event()
    submit_store_write(lambda: blocker.wait(timeout=10.0))
    try:
        started = asyncio.get_running_loop().time()
        await close_store_on_worker(tracker.close, timeout=0.3)
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 1.5, f"teardown waited {elapsed:.2f}s on a blocked write"
        assert tracker.store._db is not None, "the close is queued, not skipped"

        blocker.set()
        await run_off_loop(_noop)
        assert tracker.store._db is None, "and it runs once the worker is free"
    finally:
        blocker.set()
        tracker.close()


def _noop() -> None:
    """FIFO marker: a call that returns proves its predecessors finished."""


async def test_a_failed_best_effort_write_does_not_change_the_response(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Fault counters are telemetry: a store that raises must cost a debug line
    # and nothing else, exactly as when the call ran inline behind a try/except.
    config = _config(tmp_path)
    tracker = FeedbackTracker(config)
    engine = _engine(config, tracker)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    tracker.record_fault = boom  # type: ignore[method-assign]
    try:
        with caplog.at_level(logging.DEBUG, logger="memtomem_stm.surfacing.engine"):
            engine._persist_fault("gh", "read_file", "error_timeout")
            await engine.drain_store_writes()

        assert any("surfacing fault counter" in r.getMessage() for r in caplog.records)
        # The claim bookkeeping ran on the loop and is unaffected by the write.
        assert set(engine._breaker_blocked_keys) == set()
    finally:
        await engine.stop()
        tracker.close()
