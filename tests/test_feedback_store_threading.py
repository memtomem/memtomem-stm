"""``FeedbackStore`` under concurrent access, which is now the normal case.

Its writes run on the shared feedback-I/O worker while reads stay on the
caller's thread (#996), and the file is shared with every other process
pointing at ``stm_feedback.db``. These tests pin what that split has to
guarantee: a read never waits out a contended write, a ``close`` never lands
in the middle of one, and the fault timestamps survive a queued write running
later than the caller that recorded it.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from memtomem_stm.surfacing.feedback_store import (
    FAULT_KINDS,
    FeedbackStore,
    read_surfacing_summary,
)


def _store(tmp_path: Path) -> FeedbackStore:
    store = FeedbackStore(tmp_path / "feedback.db")
    store.initialize()
    return store


class TestReaderDoesNotWaitOnAWriter:
    """The reason there are two connections at all."""

    def test_a_read_returns_while_a_write_waits_out_a_peer(self, tmp_path: Path) -> None:
        # A peer process holding the write lock is ordinary here: the proxy's
        # own store, ``mms tune``'s retention purge, a second daemon. Under one
        # shared connection the reader queued behind the blocked writer and,
        # on the event loop, that wait was the whole stall #996 is about.
        store = _store(tmp_path)
        store.mark_surfaced(["mem-seeded"])
        holder = sqlite3.connect(str(store.db_path))
        write_started = threading.Event()
        write_done = threading.Event()

        def blocked_write() -> None:
            write_started.set()
            try:
                store.mark_surfaced(["mem-blocked"])
            except sqlite3.OperationalError:
                pass  # the peer outlasted the busy budget; the read is the subject
            finally:
                write_done.set()

        writer = threading.Thread(target=blocked_write)
        try:
            holder.execute("BEGIN IMMEDIATE")
            holder.execute(
                "INSERT INTO seen_memories (memory_id, first_seen_at, last_seen_at, seen_count)"
                " VALUES ('mem-peer', 1.0, 1.0, 1)"
            )
            writer.start()
            assert write_started.wait(timeout=5.0)
            time.sleep(0.05)  # let the writer reach the busy handler

            started = time.monotonic()
            seen = store.get_seen_ids(ttl_seconds=10_000_000.0)
            elapsed = time.monotonic() - started

            assert not write_done.is_set(), "the write must still be blocked for this to prove it"
            assert elapsed < 0.5, f"read waited {elapsed:.2f}s behind a blocked write"
            assert "mem-seeded" in seen
        finally:
            holder.rollback()
            holder.close()
            writer.join(timeout=10.0)
            store.close()

    def test_the_blocked_write_still_lands_once_the_peer_releases(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        holder = sqlite3.connect(str(store.db_path))
        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "INSERT INTO seen_memories (memory_id, first_seen_at, last_seen_at, seen_count)"
            " VALUES ('mem-peer', 1.0, 1.0, 1)"
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(store.mark_surfaced, ["mem-waited"])
            time.sleep(0.05)
            holder.rollback()
            holder.close()
            future.result(timeout=10.0)
        try:
            assert "mem-waited" in store.get_seen_ids(ttl_seconds=10_000_000.0)
        finally:
            store.close()


class TestConcurrentReadersAndWriters:
    """The ``MetricsStore.__init__`` reader/writer convention, applied here.

    ``test_readers_and_writer_concurrent`` is the sibling of the same test in
    ``tests/test_metrics_store.py``. Honest about what each guard earns: the
    cross-query assertions below fail without the ``_reading`` snapshot, while
    ``_read_lock`` itself is not what they prove — with the GIL serializing
    each ``sqlite3`` call, dropping it does not by itself tear a read. What
    that lock rules out is a ``close`` landing between the connection lookup
    and the ``execute`` that uses it, which ``TestCloseRacingAWrite`` covers.
    """

    def test_readers_and_writer_concurrent(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        total_writes = 150
        reads_per_worker = 100

        def writer() -> None:
            for i in range(total_writes):
                store.record_surfacing(
                    f"sid-{i}",
                    "gh",
                    f"tool{i % 3}",
                    "a query",
                    [f"mem-{i}"],
                    [0.5],
                )
                store.mark_surfaced([f"mem-{i}"])
                store.record_fault("gh", f"tool{i % 3}", "error_timeout")

        def read_stats() -> None:
            for _ in range(reads_per_worker):
                stats = store.get_stats(limit=5)
                # The real tear test: ``events_total`` and the per-tool
                # breakdown come from different queries, so a writer landing
                # between them hands back a total that its own breakdown
                # contradicts. Only a snapshot read makes these agree.
                assert sum(r["events"] for r in stats["per_tool_breakdown"]) == (
                    stats["events_total"]
                )
                assert sum(stats["score_scale_distribution"].values()) == stats["events_total"]
                for row in stats["per_tool_breakdown"]:
                    assert row["events"] >= 1
                    assert isinstance(row["tool"], str)

        def read_seen() -> None:
            for _ in range(reads_per_worker):
                assert all(mid.startswith("mem-") for mid in store.get_seen_ids(10_000_000.0))

        def read_ratios() -> None:
            for _ in range(reads_per_worker):
                assert store.get_tool_negative_ratio("tool0", min_samples=1) in (None, 0.0)
                assert store.get_feedback_count() == 0

        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(writer),
                    pool.submit(read_stats),
                    pool.submit(read_seen),
                    pool.submit(read_ratios),
                ]
                for future in futures:
                    future.result(timeout=60)

            assert store.get_stats(limit=1)["events_total"] == total_writes
            assert len(store.get_seen_ids(10_000_000.0)) == total_writes
        finally:
            store.close()


class TestCloseRacingAWrite:
    def test_close_waits_for_the_writer_lock(self, tmp_path: Path) -> None:
        # ``close`` takes the writer lock precisely so it cannot pull the
        # connection out from under a worker mid-statement. Holding the lock
        # here stands in for that in-flight write: without the acquire in
        # ``close``, the connection would be closed underneath it and the
        # write would raise ``ProgrammingError`` on a path whose contract is
        # to degrade quietly.
        store = _store(tmp_path)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with store._lock:
                closing = pool.submit(store.close)
                time.sleep(0.05)
                assert not closing.done(), "close must wait for the in-flight write"
                assert store._db is not None
            closing.result(timeout=5.0)

        assert store._db is None
        assert store._read_db is None

    def test_a_write_that_beat_close_is_on_disk(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.mark_surfaced(["mem-before-close"])
        store.close()
        reopened = _store(tmp_path)
        try:
            assert "mem-before-close" in reopened.get_seen_ids(10_000_000.0)
        finally:
            reopened.close()

    def test_calls_after_close_are_no_ops_not_errors(self, tmp_path: Path) -> None:
        # Every method re-reads its connection INSIDE the lock, so a call that
        # lost the race to ``close`` returns the method's documented empty
        # value instead of raising from a half-closed connection.
        store = _store(tmp_path)
        store.close()

        store.mark_surfaced(["mem-after"])
        store.record_fault("gh", "read_file", "error_timeout")
        store.record_surfacing("sid", "gh", "read_file", "q", ["mem"], [0.5])
        store.save_adjustment("read_file", 0.04)

        assert store.record_feedback("sid", "helpful") is False
        assert store.get_seen_ids(10_000_000.0) == set()
        assert store.get_stats()["events_total"] == 0
        assert store.load_adjustments() == {}
        assert store.cleanup_expired(1.0) == 0
        assert store.delete_events_older_than(1.0) == 0


class TestQueuedWriteTimestamps:
    def test_a_late_fault_is_still_closed_by_the_recovery_that_disproved_it(
        self, tmp_path: Path
    ) -> None:
        # The engine takes the observation time when it QUEUES a fault, and the
        # round trip that disproves it takes its own a moment later. Both then
        # run on the worker, the fault first. Stamping the fault at execution
        # time instead would put it after ``recovered_at``, and the recovery's
        # ``last_at <= recovered_at`` guard would leave a disproved episode
        # open for the whole retention window.
        store = _store(tmp_path)
        # Both timestamps sit well in the past, so the fault's stamp is the
        # one the caller passed rather than "whenever the worker got to it":
        # a write stamped at execution time would land after ``recovered_at``
        # and the recovery's ``last_at <= recovered_at`` guard would miss it.
        now = time.time()
        observed_at = now - 5.0
        recovered_at = now - 2.0
        try:
            # Executed in that order, but the fault carries the earlier time.
            store.record_fault("gh", "read_file", "error_timeout", at=observed_at)
            store.record_fault_recoveries(
                [("gh", "read_file", FAULT_KINDS)], recovered_at=recovered_at
            )
            summary = read_surfacing_summary(store.db_path)
            assert summary["faults"] == {"error_timeout": 1}
            assert summary["active_faults"] == {}
        finally:
            store.close()

    def test_a_fault_recorded_after_the_success_stays_active(self, tmp_path: Path) -> None:
        # The mirror: a fault the caller observed AFTER the successful round
        # trip is no longer disproved by it, and must survive as active.
        store = _store(tmp_path)
        recovered_at = time.time()
        try:
            store.record_fault_recoveries(
                [("gh", "read_file", FAULT_KINDS)], recovered_at=recovered_at
            )
            store.record_fault("gh", "read_file", "error_timeout", at=recovered_at + 1.0)
            assert read_surfacing_summary(store.db_path)["active_faults"] == {"error_timeout": 1}
        finally:
            store.close()


def test_initialize_failure_leaves_no_connection_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both connections, not just the writer (the reader opens last)."""
    import memtomem_stm.surfacing.feedback_store as module

    calls = {"n": 0}
    real_tune = module.tune_connection

    def fail_on_the_reader(conn: sqlite3.Connection, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated reader tuning failure")
        real_tune(conn, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "tune_connection", fail_on_the_reader)
    store = FeedbackStore(tmp_path / "feedback.db")
    with pytest.raises(RuntimeError, match="simulated reader tuning failure"):
        store.initialize()

    assert store._db is None
    assert store._read_db is None
