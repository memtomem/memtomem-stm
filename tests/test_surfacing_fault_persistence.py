"""Durable surfacing fault counters — ``surfacing_faults`` persistence.

The in-memory ``SurfacingObservability`` counters die with the process, and
both fault-heavy processes (the idle-exiting daemon, MCP session servers)
restart routinely — so a timeout/breaker loop spanning hours was invisible to
``mms stats``, which reads on-disk stores only. These tests pin the durable
counterpart: engine fault branches upsert day-aggregated rows through the
feedback store, and ``read_surfacing_summary`` exposes them to the CLI.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from memtomem_stm.cli.proxy import _render_surfacing_block
from memtomem_stm.surfacing.config import SurfacingConfig, ToolSurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.feedback import FeedbackTracker
from memtomem_stm.surfacing.feedback_store import (
    DIAGNOSTIC_KINDS,
    FAULT_KINDS,
    FeedbackStore,
    read_surfacing_summary,
)
from memtomem_stm.surfacing.observability import FAULT_SKIP_REASONS

# ── Helpers ──────────────────────────────────────────────────────────────


@dataclass
class FakeChunkMeta:
    source_file: Path = Path("/notes/test.md")
    namespace: str = "default"


@dataclass
class FakeChunk:
    id: str = ""
    content: str = "some memory content"
    metadata: FakeChunkMeta | None = None

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid4())
        if self.metadata is None:
            self.metadata = FakeChunkMeta()


def _make_config(tmp_path: Path, **overrides) -> SurfacingConfig:
    defaults = {
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
        "cache_ttl_seconds": 60.0,
        "query_retention_days": 0,
        "stats_retention_days": 0,
        "feedback_db_path": tmp_path / "faults.db",
    }
    defaults.update(overrides)
    return SurfacingConfig(**defaults)


def _fault_rows(db_path: Path) -> list[tuple[str, str, str, int]]:
    db = sqlite3.connect(str(db_path))
    try:
        return db.execute(
            "SELECT server, tool, kind, count FROM surfacing_faults ORDER BY kind"
        ).fetchall()
    finally:
        db.close()


def _episode_is_open(db_path: Path, server: str, tool: str, kind: str) -> bool:
    """Is ONE key's diagnostic episode still open?

    ``read_surfacing_summary``'s ``active_diagnostics`` rolls every key up per
    kind, so it cannot answer this when other keys hold the same kind open —
    which is exactly the shape of an eviction test (#880).
    """
    db = sqlite3.connect(str(db_path))
    try:
        row = db.execute(
            "SELECT last_at > COALESCE(last_recovered_at, 0) FROM surfacing_faults "
            "WHERE server = ? AND tool = ? AND kind = ?",
            (server, tool, kind),
        ).fetchone()
    finally:
        db.close()
    assert row is not None, f"no {kind} row for {server}/{tool}"
    return bool(row[0])


def _recovery_call_counter(tracker: FeedbackTracker):
    """Count engine recovery batches from here on, keeping the write."""
    real = tracker.record_fault_recoveries
    calls = {"n": 0}

    def counting(entries, **kwargs) -> None:
        calls["n"] += 1
        real(entries, **kwargs)

    tracker.record_fault_recoveries = counting  # type: ignore[method-assign]
    return lambda: calls["n"]


class _FailingCommit:
    """sqlite3.Connection proxy whose ``commit`` — and optionally whose
    ``rollback`` — raises. ``Connection.commit`` is a read-only attribute, so
    the failure has to be injected around the connection, not onto it."""

    def __init__(self, db, *, fail_rollback: bool = False) -> None:
        self._db = db
        self._fail_rollback = fail_rollback

    def commit(self):
        raise sqlite3.OperationalError("disk I/O error")

    def rollback(self):
        if self._fail_rollback:
            raise sqlite3.OperationalError("disk I/O error")
        return self._db.rollback()

    def __getattr__(self, name):
        return getattr(self._db, name)


class _FailNthRecoveryUpdate:
    """sqlite3.Connection proxy that fails the Nth recovery UPDATE.

    ``Connection.execute`` is a read-only attribute, so the interruption has
    to be injected around the connection rather than onto it.
    """

    def __init__(self, db, *, fail_on: int) -> None:
        self._db = db
        self._fail_on = fail_on
        self.attempts = 0

    def execute(self, sql, *args):
        if sql.startswith("UPDATE surfacing_faults SET last_recovered_at"):
            self.attempts += 1
            if self.attempts == self._fail_on:
                raise sqlite3.OperationalError("disk I/O error")
        return self._db.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._db, name)


def _other_args(marker: str) -> dict:
    """Args carrying a DISTINCT query: the extractor reads ``_context_query``,
    so varying only ``path`` would hit the result cache instead of LTM."""
    return {"path": f"src/{marker}.py", "_context_query": f"{marker} architecture notes"}


LONG_RESPONSE = "x" * 200
VALID_ARGS = {"path": "src/app.py", "_context_query": "Flask web framework architecture"}


# ── Store-level ──────────────────────────────────────────────────────────


class TestRecordFault:
    def test_upsert_increments_single_row(self, tmp_path):
        store = FeedbackStore(tmp_path / "f.db")
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        store.record_fault("gh", "read_file", "error_timeout")
        rows = _fault_rows(tmp_path / "f.db")
        assert rows == [("gh", "read_file", "error_timeout", 2)]

    def test_unknown_kind_is_dropped(self, tmp_path):
        store = FeedbackStore(tmp_path / "f.db")
        store.initialize()
        store.record_fault("gh", "read_file", "not_a_fault_kind")
        assert _fault_rows(tmp_path / "f.db") == []

    def test_uninitialized_store_is_noop(self, tmp_path):
        store = FeedbackStore(tmp_path / "f.db")
        store.record_fault("gh", "read_file", "error_timeout")  # must not raise

    def test_fault_kinds_cover_fault_skip_reasons(self):
        # Taxonomy pin: every degraded-dependency skip reason must be
        # persistable, plus the two error outcomes. A new FAULT_SKIP_REASONS
        # member added without a FAULT_KINDS entry would silently drop its
        # durable counter (record_fault ignores unknown kinds by design).
        assert FAULT_KINDS == FAULT_SKIP_REASONS | {"error_timeout", "error_other"}

    def test_diagnostic_upsert_is_separate_and_unknown_kind_is_dropped(self, tmp_path):
        store = FeedbackStore(tmp_path / "f.db")
        store.initialize()
        store.record_diagnostic("gh", "read_file", "score_ceiling_below_min")
        store.record_diagnostic("gh", "read_file", "score_ceiling_below_min")
        store.record_diagnostic("gh", "read_file", "unknown_diagnostic")
        assert DIAGNOSTIC_KINDS == {"score_ceiling_below_min", "score_scale_mismatch"}
        assert _fault_rows(tmp_path / "f.db") == [("gh", "read_file", "score_ceiling_below_min", 2)]

    def test_delete_faults_older_than(self, tmp_path):
        store = FeedbackStore(tmp_path / "f.db")
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        # Backdate the row past the window.
        store._db.execute("UPDATE surfacing_faults SET last_at = ?", (time.time() - 100.0,))
        store._db.commit()
        assert store.delete_faults_older_than(50.0) == 1
        assert _fault_rows(tmp_path / "f.db") == []

    def test_delete_disabled_on_nonpositive_retention(self, tmp_path):
        store = FeedbackStore(tmp_path / "f.db")
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        assert store.delete_faults_older_than(0.0) == 0
        assert len(_fault_rows(tmp_path / "f.db")) == 1


class TestRecordDiagnosticRecovery:
    """Diagnostic episodes close on the same rules as fault episodes (#944).

    The engine now writes this on every healthy or scale-suspended batch
    instead of latching "already recovered" per process, so the guard that
    makes a repeat write inert is what keeps that affordable — and the kind
    partition is what keeps it from touching a fault row.
    """

    def test_repeating_recovery_leaves_a_closed_row_untouched(self, tmp_path, monkeypatch):
        clock = [1_700_000_000.0]
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: clock[0],
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_diagnostic("gh", "read_file", "score_scale_mismatch")
        clock[0] += 10.0
        store.record_diagnostic_recoveries("gh", "read_file", recovered_at=clock[0])
        stamped = store._db.execute("SELECT last_recovered_at FROM surfacing_faults").fetchone()[0]

        clock[0] += 10.0
        store.record_diagnostic_recoveries("gh", "read_file", recovered_at=clock[0])
        assert (
            store._db.execute("SELECT last_recovered_at FROM surfacing_faults").fetchone()[0]
            == stamped
        ), "a closed episode must not be re-stamped on every healthy observation"

        # Positive control: a new diagnostic reopens the episode and the next
        # recovery does advance the stamp.
        clock[0] += 10.0
        store.record_diagnostic("gh", "read_file", "score_scale_mismatch")
        clock[0] += 10.0
        store.record_diagnostic_recoveries("gh", "read_file", recovered_at=clock[0])
        assert (
            store._db.execute("SELECT last_recovered_at FROM surfacing_faults").fetchone()[0]
            > stamped
        )
        store.close()

    def test_both_kinds_close_in_one_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: 1_700_000_000.0,
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_diagnostic("gh", "read_file", "score_scale_mismatch")
        store.record_diagnostic("gh", "read_file", "score_ceiling_below_min")

        store.record_diagnostic_recoveries("gh", "read_file", recovered_at=1_700_000_000.0)
        assert read_surfacing_summary(db_path)["active_diagnostics"] == {}
        store.close()

    def test_recovery_leaves_faults_open(self, tmp_path, monkeypatch):
        """The mirror of ``test_recovery_leaves_diagnostics_open``: a healthy
        score scale says nothing about a timed-out dependency."""
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: 1_700_000_000.0,
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        store.record_diagnostic("gh", "read_file", "score_scale_mismatch")

        store.record_diagnostic_recoveries("gh", "read_file", recovered_at=1_700_000_000.0)
        summary = read_surfacing_summary(db_path)
        assert summary["active_diagnostics"] == {}
        assert summary["active_faults"] == {"error_timeout": 1}
        store.close()

    def test_single_kind_wrapper_carries_the_guard(self, tmp_path, monkeypatch):
        """``record_diagnostic_recovery`` closes only the kind it is given and
        inherits the already-closed guard from the batched form."""
        clock = [1_700_000_000.0]
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: clock[0],
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_diagnostic("gh", "read_file", "score_scale_mismatch")
        store.record_diagnostic("gh", "read_file", "score_ceiling_below_min")

        clock[0] += 10.0
        store.record_diagnostic_recovery("gh", "read_file", "score_scale_mismatch")
        assert read_surfacing_summary(db_path)["active_diagnostics"] == {
            "score_ceiling_below_min": 1
        }
        stamped = store._db.execute(
            "SELECT last_recovered_at FROM surfacing_faults WHERE kind = ?",
            ("score_scale_mismatch",),
        ).fetchone()[0]

        clock[0] += 10.0
        store.record_diagnostic_recovery("gh", "read_file", "score_scale_mismatch")
        assert (
            store._db.execute(
                "SELECT last_recovered_at FROM surfacing_faults WHERE kind = ?",
                ("score_scale_mismatch",),
            ).fetchone()[0]
            == stamped
        )
        store.close()


class TestRecordFaultRecovery:
    """A fault episode must close when a later surfacing proves LTM healthy.

    Without it every ``circuit_open``/``error_timeout`` row stayed unrecovered
    for its whole retention window, so ``mms stats`` reported a breaker loop
    that ended days ago as ongoing breakage (#869).
    """

    def test_repeating_recovery_leaves_a_closed_row_untouched(self, tmp_path, monkeypatch):
        # Recovery runs on every healthy miss, so it must be a no-op once the
        # episode is closed rather than re-stamping every historical row.
        clock = [1_700_000_000.0]
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: clock[0],
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        clock[0] += 10.0
        store.record_fault_recovery("gh", "read_file", recovered_at=clock[0])
        stamped = store._db.execute("SELECT last_recovered_at FROM surfacing_faults").fetchone()[0]

        clock[0] += 10.0
        store.record_fault_recovery("gh", "read_file", recovered_at=clock[0])
        assert (
            store._db.execute("SELECT last_recovered_at FROM surfacing_faults").fetchone()[0]
            == stamped
        )
        # Positive control: a new fault reopens the episode, and the next
        # recovery does advance the stamp.
        clock[0] += 10.0
        store.record_fault("gh", "read_file", "error_timeout")
        clock[0] += 10.0
        store.record_fault_recovery("gh", "read_file", recovered_at=clock[0])
        assert (
            store._db.execute("SELECT last_recovered_at FROM surfacing_faults").fetchone()[0]
            > stamped
        )
        store.close()

    def test_recovery_only_covers_the_requested_kinds(self, tmp_path, monkeypatch):
        # The engine closes ONLY ``circuit_open`` for keys its breaker blocked:
        # the probe that closed the breaker says nothing about their timeouts.
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: 1_700_000_000.0,
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "circuit_open")
        store.record_fault("gh", "read_file", "error_timeout")

        store.record_fault_recovery(
            "gh",
            "read_file",
            recovered_at=1_700_000_000.0,
            kinds=frozenset({"circuit_open"}),
        )
        assert read_surfacing_summary(db_path)["active_faults"] == {"error_timeout": 1}
        store.close()

    def test_a_failed_commit_leaves_no_open_transaction(self, tmp_path, monkeypatch):
        """A commit that raises must not leave the transaction open.

        The next unrelated write on the connection would otherwise commit
        these half-applied recovery rows as a side effect of its own work.
        """
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: 1_700_000_000.0,
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")

        real_db = store._db
        store._db = _FailingCommit(real_db)  # type: ignore[assignment]
        with pytest.raises(sqlite3.OperationalError):
            store.record_fault_recovery("gh", "read_file", recovered_at=1_700_000_000.0)
        assert real_db.in_transaction is False
        store._db = real_db  # type: ignore[assignment]

        # An unrelated write must not carry the abandoned recovery in with it.
        store.record_fault("gh", "other_tool", "error_timeout")
        assert read_surfacing_summary(db_path)["active_faults"] == {"error_timeout": 2}
        store.close()

    def test_a_failed_counter_commit_leaves_nothing_pending(self, tmp_path, monkeypatch):
        """The engine reads a raised ``record_fault`` as "no row landed".

        A counter write whose commit fails used to leave the row pending for
        the next unrelated write to publish, so the engine released a breaker
        claim for a row that then appeared anyway.
        """
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: 1_700_000_000.0,
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()

        real_db = store._db
        store._db = _FailingCommit(real_db)  # type: ignore[assignment]
        with pytest.raises(sqlite3.OperationalError):
            store.record_fault("gh", "read_file", "circuit_open")
        assert real_db.in_transaction is False
        store._db = real_db  # type: ignore[assignment]

        store.record_fault("gh", "other_tool", "error_timeout")
        assert read_surfacing_summary(db_path)["active_faults"] == {"error_timeout": 1}
        store.close()

    def test_a_failed_rollback_keeps_the_connection(self, tmp_path, monkeypatch, caplog):
        """A failed rollback is logged, not answered by dropping the store.

        Dropping it would make every write a SILENT no-op, and a
        ``record_surfacing`` that returns without writing leaves the agent
        holding an advertised feedback ID that resolves to nothing — while a
        raising write makes the engine re-render without the dead handle.
        """
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: 1_700_000_000.0,
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")

        real_db = store._db
        store._db = _FailingCommit(real_db, fail_rollback=True)  # type: ignore[assignment]
        with caplog.at_level("WARNING"):
            with pytest.raises(sqlite3.OperationalError):
                store.record_fault_recovery("gh", "read_file", recovered_at=1_700_000_000.0)
        assert store._db is not None
        assert "may hold an uncommitted transaction" in caplog.text

        store._db = real_db  # type: ignore[assignment]
        store.record_surfacing("sid-1", "gh", "read_file", "q", ["m1"], [0.5])
        assert store.get_stats()["events_total"] == 1
        store.close()

    def test_recovery_closes_episode_and_a_new_fault_reopens_it(self, tmp_path, monkeypatch):
        # Windows can return the same time.time() for adjacent writes, so the
        # re-open must come from ``record_fault``'s explicit recovery reset,
        # not from ``>`` between two timestamps of coarse resolution.
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: 1_700_000_000.0,
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        assert read_surfacing_summary(db_path)["active_faults"] == {"error_timeout": 1}

        store.record_fault_recovery("gh", "read_file", recovered_at=1_700_000_000.0)
        summary = read_surfacing_summary(db_path)
        assert summary["active_faults"] == {}
        # The historical counter is untouched — recovery closes the episode,
        # it does not erase the evidence the operator is reading.
        assert summary["faults"] == {"error_timeout": 1}

        store.record_fault("gh", "read_file", "error_timeout")
        assert read_surfacing_summary(db_path)["active_faults"] == {"error_timeout": 2}
        store.close()

    def test_recovery_spans_every_day_bucket(self, tmp_path, monkeypatch):
        # Rows are per calendar day, so a recovery that only stamped today's
        # bucket would leave yesterday's episode reading active forever.
        now = [1_700_000_000.0]
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: now[0],
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        now[0] += 86400.0
        store.record_fault("gh", "read_file", "error_timeout")
        assert len(_fault_rows(db_path)) == 2
        assert read_surfacing_summary(db_path)["active_faults"] == {"error_timeout": 2}

        store.record_fault_recovery("gh", "read_file", recovered_at=now[0])
        assert read_surfacing_summary(db_path)["active_faults"] == {}
        store.close()

    def test_recovery_leaves_diagnostics_open(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: 1_700_000_000.0,
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        store.record_diagnostic("gh", "read_file", "score_ceiling_below_min")

        store.record_fault_recovery("gh", "read_file", recovered_at=1_700_000_000.0)
        summary = read_surfacing_summary(db_path)
        # Positive control: the fault on the same key DID close, so the
        # surviving diagnostic is a partition boundary, not an inert write.
        assert summary["active_faults"] == {}
        assert summary["active_diagnostics"] == {"score_ceiling_below_min": 1}
        store.close()

    def test_recovery_is_scoped_to_its_own_key(self, tmp_path, monkeypatch):
        """Every kind, ``circuit_open`` included, stays keyed.

        An un-keyed ``circuit_open`` sweep would let one process's healthy
        round trip stamp a peer process's still-open breaker episode — the
        rows are shared by everything pointing at this DB, and a breaker is
        process-local.
        """
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: 1_700_000_000.0,
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "circuit_open")
        store.record_fault("gh", "other_tool", "circuit_open")

        store.record_fault_recovery("gh", "other_tool", recovered_at=1_700_000_000.0)
        assert read_surfacing_summary(db_path)["active_faults"] == {"circuit_open": 1}

        store.record_fault_recovery("gh", "read_file", recovered_at=1_700_000_000.0)
        assert read_surfacing_summary(db_path)["active_faults"] == {}
        store.close()

    def test_one_keys_recovery_does_not_clear_another_keys_episode(self, tmp_path, monkeypatch):
        """Episodes are per ``(server, tool, kind)``.

        Rolling the recovery check up by kind first let a newer recovery on one
        key cancel an older but still-open fault on another, so the CLI printed
        an all-clear while a server was still broken.
        """
        clock = [1_700_000_000.0]
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: clock[0],
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gl", "read_file", "error_timeout")  # peer, recovered later
        clock[0] += 10.0
        store.record_fault("gh", "read_file", "error_timeout")  # stays broken
        clock[0] += 10.0
        store.record_fault_recovery("gl", "read_file", recovered_at=clock[0])

        assert read_surfacing_summary(db_path)["active_faults"] == {"error_timeout": 1}
        # Same collision across two servers sharing one tool name, under the
        # tool filter that the CLI uses.
        assert read_surfacing_summary(db_path, tool="read_file")["active_faults"] == {
            "error_timeout": 1
        }
        store.close()

    def test_two_stores_on_one_db_interleave_recovery_and_refault(self, tmp_path, monkeypatch):
        """Two stores on one file, as two proxy processes share one DB.

        Episode state lives in the row, not in either process, so B's new fault
        must read active even though A just recovered the key — this is the
        shared-DB half of what the engine's unlatched retry depends on.
        """
        clock = [1_700_000_000.0]
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: clock[0],
        )
        db_path = tmp_path / "f.db"
        process_a = FeedbackStore(db_path)
        process_a.initialize()
        process_b = FeedbackStore(db_path)
        process_b.initialize()

        process_b.record_fault("gh", "read_file", "error_timeout")
        clock[0] += 10.0
        process_a.record_fault_recovery("gh", "read_file", recovered_at=clock[0])
        assert read_surfacing_summary(db_path)["active_faults"] == {}

        clock[0] += 10.0
        process_b.record_fault("gh", "read_file", "error_timeout")
        assert read_surfacing_summary(db_path)["active_faults"] == {"error_timeout": 2}
        clock[0] += 10.0
        process_a.record_fault_recovery("gh", "read_file", recovered_at=clock[0])
        assert read_surfacing_summary(db_path)["active_faults"] == {}
        process_a.close()
        process_b.close()

    def test_recovery_leaves_a_concurrent_newer_fault_active(self, tmp_path, monkeypatch):
        # ``last_at <= now`` bound: a fault written while this recovery is in
        # flight describes breakage the successful call cannot disprove.
        clock = [1_700_000_000.0]
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: clock[0],
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        clock[0] = 1_700_000_100.0
        store.record_fault("gh", "read_file", "error_timeout")
        # The round trip succeeded BEFORE that fault was recorded, so the
        # boundary it passes cannot disprove it.
        store.record_fault_recovery("gh", "read_file", recovered_at=1_700_000_050.0)
        assert read_surfacing_summary(db_path)["active_faults"] == {"error_timeout": 1}
        store.close()

    def test_uninitialized_store_is_noop(self, tmp_path):
        store = FeedbackStore(tmp_path / "f.db")
        store.record_fault_recovery("gh", "read_file", recovered_at=1_700_000_000.0)  # must not raise

    @pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
    def test_unencodable_key_is_refused(self, tmp_path, monkeypatch, surrogate):
        # A lone surrogate cannot be bound as a SQLite parameter. Refuse the
        # whole write rather than let it raise out of the surfacing hot path.
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: 1_700_000_000.0,
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        store.record_fault_recovery(f"gh{surrogate}", "read_file", recovered_at=1_700_000_000.0)
        store.record_fault_recovery("gh", f"read_file{surrogate}", recovered_at=1_700_000_000.0)
        # Positive control: an encodable key does close the same episode.
        assert read_surfacing_summary(db_path)["active_faults"] == {"error_timeout": 1}
        store.record_fault_recovery("gh", "read_file", recovered_at=1_700_000_000.0)
        assert read_surfacing_summary(db_path)["active_faults"] == {}
        store.close()


# ── Engine wiring ────────────────────────────────────────────────────────


class TestEngineFaultPersistence:
    async def test_timeout_persists_fault(self, tmp_path):
        async def slow_search(*args, **kwargs):
            await asyncio.sleep(10)
            return [], [], "empty_results"

        adapter = AsyncMock()
        adapter.search = slow_search
        config = _make_config(tmp_path, timeout_seconds=0.05)
        engine = SurfacingEngine(
            config=config,
            mcp_adapter=adapter,
            feedback_tracker=FeedbackTracker(config),
        )
        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        await engine.drain_store_writes()
        assert _fault_rows(config.feedback_db_path) == [("gh", "read_file", "error_timeout", 1)]

    async def test_circuit_open_persists_fault(self, tmp_path):
        async def slow_search(*args, **kwargs):
            await asyncio.sleep(10)
            return [], [], "empty_results"

        adapter = AsyncMock()
        adapter.search = slow_search
        config = _make_config(
            tmp_path, timeout_seconds=0.05, circuit_max_failures=1, circuit_reset_seconds=60
        )
        engine = SurfacingEngine(
            config=config,
            mcp_adapter=adapter,
            feedback_tracker=FeedbackTracker(config),
        )
        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)  # opens breaker
        await engine.surface("gh", "read_file", {"path": "y"}, LONG_RESPONSE)  # skipped
        await engine.drain_store_writes()
        rows = dict(
            ((server, tool, kind), count)
            for server, tool, kind, count in _fault_rows(config.feedback_db_path)
        )
        assert rows[("gh", "read_file", "circuit_open")] == 1
        assert rows[("gh", "read_file", "error_timeout")] == 1

    async def test_ltm_unavailable_persists_fault(self, tmp_path):
        adapter = AsyncMock()
        adapter.search = AsyncMock(return_value=([], [], "transport_error"))
        config = _make_config(tmp_path)
        engine = SurfacingEngine(
            config=config,
            mcp_adapter=adapter,
            feedback_tracker=FeedbackTracker(config),
        )
        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        await engine.drain_store_writes()
        assert _fault_rows(config.feedback_db_path) == [("gh", "read_file", "ltm_unavailable", 1)]

    async def test_scale_gate_recovers_stale_mismatch_episode(self, tmp_path):
        """A pre-existing score-scale episode (recorded while the gate was
        disabled, or before it existed) is marked recovered by the first
        scale-gated batch — ``mms doctor`` stops FAILing on a setup the gate
        just fixed instead of lingering for the full 7-day window."""

        @dataclass
        class _StampedResult:
            chunk: FakeChunk
            score: float
            score_scale: str | None = None
            reranker: str | None = None

        adapter = AsyncMock()
        adapter.search = AsyncMock(
            return_value=([_StampedResult(FakeChunk(), -0.17, "rerank")], [], "ok")
        )
        config = _make_config(tmp_path)
        tracker = FeedbackTracker(config)
        tracker.record_diagnostic("gh", "read_file", "score_scale_mismatch")
        tracker.record_diagnostic("gh", "read_file", "score_ceiling_below_min")
        assert read_surfacing_summary(config.feedback_db_path)["active_diagnostics"] == {
            "score_scale_mismatch": 1,
            "score_ceiling_below_min": 1,
        }

        engine = SurfacingEngine(config=config, mcp_adapter=adapter, feedback_tracker=tracker)
        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)

        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_diagnostics"] == {}

    @dataclass
    class _ScaleResult:
        """Minimal search result carrying a core-reported score scale."""

        chunk: FakeChunk
        score: float
        score_scale: str | None = None
        reranker: str | None = None

    async def test_peer_closing_the_episode_does_not_silence_this_process(self, tmp_path, caplog):
        """#944 — the WARNING latch is per process, the row is shared.

        Process A observing the tool healthy closes the row. B is still
        mismatched, and its latch keeps it from re-WARNING; if persistence
        were latched too, B would never reopen the row and ``mms doctor``
        would report all-clear while the fault continued.
        """
        config = _make_config(tmp_path)
        engine_b = SurfacingEngine(
            config=config, mcp_adapter=AsyncMock(), feedback_tracker=FeedbackTracker(config)
        )
        engine_a = SurfacingEngine(
            config=config, mcp_adapter=AsyncMock(), feedback_tracker=FeedbackTracker(config)
        )
        named_low = [self._ScaleResult(FakeChunk(), 0.001, "bm25")]
        healthy = [self._ScaleResult(FakeChunk(), 0.9)]

        with caplog.at_level(logging.WARNING):
            engine_b._observe_score_scale("gh", "read_file", named_low, 0.03)
            await engine_b.drain_store_writes()
            assert _episode_is_open(
                config.feedback_db_path, "gh", "read_file", "score_scale_mismatch"
            )

            engine_a._observe_score_scale("gh", "read_file", healthy, 0.03)
            await engine_a.drain_store_writes()
            assert not _episode_is_open(
                config.feedback_db_path, "gh", "read_file", "score_scale_mismatch"
            )

            # B is still mismatched: its next observation must reopen the row.
            engine_b._observe_score_scale("gh", "read_file", named_low, 0.03)
            await engine_b.drain_store_writes()

        assert _episode_is_open(
            config.feedback_db_path, "gh", "read_file", "score_scale_mismatch"
        ), "a peer's healthy observation silenced this process for the whole 7-day window"
        assert [r for r in _fault_rows(config.feedback_db_path) if r[2] == "score_scale_mismatch"][
            0
        ][3] == 2, "the reopening write must also count the observation"
        # B's WARNING stays latched — the log is per episode, per process.
        assert len([r for r in caplog.records if "score-scale mismatch" in r.message]) == 1

    async def test_row_tracks_the_latest_observation_under_mixed_configs(self, tmp_path):
        """Two processes configured differently make the row oscillate.

        A has the scale gate on (its batches are suspended: no problem from
        where it stands); B pins ``min_score`` for the tool, so its filter
        stays live and the mismatch is real. The row is keyed
        ``(server, tool, kind)`` with no process column, so it answers "was
        the newest observation healthy?", exactly as the fault rows do —
        ``record_fault_recovery`` documents the same property.

        The trade is deliberate and not free: A can now read a doctor FAIL
        its own suspended filter does not justify, where before it read PASS.
        What that buys is the end of the unbounded failure — B used to be
        latched silent after A's first close, so the row stayed closed for the
        whole retention window no matter how long B kept failing (#944).
        Making both verdicts right at once needs per-process ownership in the
        schema.
        """
        # Real wiring, not a hand-passed flag: A runs with the scale gate on,
        # so a core-named non-RRF batch suspends its filter; B pins min_score
        # for the same tool, which keeps the filter — and the mismatch — live.
        results = [self._ScaleResult(FakeChunk(content="logit hit"), -0.17, "rerank")]
        config_a = _make_config(tmp_path, scale_gated_min_score=True)
        config_b = _make_config(
            tmp_path,
            scale_gated_min_score=True,
            context_tools={"read_file": ToolSurfacingConfig(min_score=0.03)},
        )
        adapter_a = AsyncMock()
        adapter_a.search = AsyncMock(return_value=(results, [], "ok"))
        adapter_b = AsyncMock()
        adapter_b.search = AsyncMock(return_value=(results, [], "ok"))
        engine_a = SurfacingEngine(
            config=config_a, mcp_adapter=adapter_a, feedback_tracker=FeedbackTracker(config_a)
        )
        engine_b = SurfacingEngine(
            config=config_b, mcp_adapter=adapter_b, feedback_tracker=FeedbackTracker(config_b)
        )

        def open_now() -> bool:
            return _episode_is_open(
                config_a.feedback_db_path, "gh", "read_file", "score_scale_mismatch"
            )

        def q(i: int) -> dict:
            return {"_context_query": f"distinct mixed config query {i}"}

        await engine_b.surface("gh", "read_file", q(1), LONG_RESPONSE)
        await engine_b.drain_store_writes()
        assert open_now(), "the pinned process sees a real mismatch"
        await engine_a.surface("gh", "read_file", q(2), LONG_RESPONSE)
        await engine_a.drain_store_writes()
        assert not open_now(), "the gated process closes it: its filter is suspended"
        await engine_b.surface("gh", "read_file", q(3), LONG_RESPONSE)
        await engine_b.drain_store_writes()
        assert open_now(), "B must reopen — this is the #944 fix"
        await engine_a.surface("gh", "read_file", q(4), LONG_RESPONSE)
        await engine_a.drain_store_writes()
        assert not open_now(), "last writer wins; the row has no per-process column"
        # B is never permanently silenced: it reopens again, indefinitely.
        await engine_b.surface("gh", "read_file", q(5), LONG_RESPONSE)
        await engine_b.drain_store_writes()
        assert open_now()

    async def test_healthy_observation_closes_an_episode_only_a_peer_recorded(self, tmp_path):
        """The close side is unlatched for the same reason: this process never
        saw the episode open, and must still close it on healthy evidence."""
        config = _make_config(tmp_path)
        peer = FeedbackTracker(config)
        peer.record_diagnostic("gh", "read_file", "score_scale_mismatch")
        peer.record_diagnostic("gh", "read_file", "score_ceiling_below_min")
        peer.close()

        engine = SurfacingEngine(
            config=config, mcp_adapter=AsyncMock(), feedback_tracker=FeedbackTracker(config)
        )
        assert ("gh", "read_file") not in engine._score_scale_mismatch_active
        engine._observe_score_scale(
            "gh", "read_file", [self._ScaleResult(FakeChunk(), 0.9)], 0.03
        )
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_diagnostics"] == {}

    async def test_suspended_batch_closes_an_episode_only_a_peer_recorded(self, tmp_path):
        config = _make_config(tmp_path)
        peer = FeedbackTracker(config)
        peer.record_diagnostic("gh", "read_file", "score_scale_mismatch")
        peer.close()

        engine = SurfacingEngine(
            config=config, mcp_adapter=AsyncMock(), feedback_tracker=FeedbackTracker(config)
        )
        engine._observe_score_scale(
            "gh",
            "read_file",
            [self._ScaleResult(FakeChunk(), -0.17, "rerank")],
            0.03,
            filter_suspended=True,
        )
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_diagnostics"] == {}

    async def test_mismatch_path_persists_every_observation(self, tmp_path, caplog):
        """Counts track observations, not episodes — same as the fault rows."""
        config = _make_config(tmp_path)
        engine = SurfacingEngine(
            config=config, mcp_adapter=AsyncMock(), feedback_tracker=FeedbackTracker(config)
        )
        named_low = [self._ScaleResult(FakeChunk(), 0.001, "bm25")]
        unstamped_low = [self._ScaleResult(FakeChunk(), 0.001)]

        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                engine._observe_score_scale("gh", "read_file", named_low, 0.03)
            for _ in range(6):
                engine._observe_score_scale("gh", "other_tool", unstamped_low, 0.03)

        await engine.drain_store_writes()
        rows = {(r[1], r[2]): r[3] for r in _fault_rows(config.feedback_db_path)}
        assert rows[("read_file", "score_scale_mismatch")] == 3
        # Six observations, saturating at five: writes on the 5th and 6th.
        assert rows[("other_tool", "score_ceiling_below_min")] == 2
        warnings = [r.message for r in caplog.records if "score-scale mismatch" in r.message]
        assert len(warnings) == 2, "one WARNING per tier, still once per episode"

    async def test_healthy_path_issues_one_recovery_statement_per_observation(self, tmp_path):
        config = _make_config(tmp_path)
        tracker = FeedbackTracker(config)
        engine = SurfacingEngine(
            config=config, mcp_adapter=AsyncMock(), feedback_tracker=tracker
        )
        tracker.record_diagnostic("gh", "read_file", "score_scale_mismatch")
        probe = _FailNthRecoveryUpdate(tracker.store._db, fail_on=0)
        tracker.store._db = probe

        healthy = [self._ScaleResult(FakeChunk(), 0.9)]
        for _ in range(5):
            engine._observe_score_scale("gh", "read_file", healthy, 0.03)

        await engine.drain_store_writes()
        assert probe.attempts == 5, "one batched UPDATE covering both kinds per observation"
        tracker.store._db = probe._db
        assert read_surfacing_summary(config.feedback_db_path)["active_diagnostics"] == {}

    async def test_success_closes_the_episode_and_a_later_fault_reopens_it(self, tmp_path):
        """The whole point of #869: a healthy round trip closes the episode,
        and a later fault reopens one that the NEXT success closes again."""
        adapter = AsyncMock()
        adapter.search = AsyncMock(return_value=([], [], "empty_results"))
        config = _make_config(tmp_path)
        tracker = FeedbackTracker(config)
        tracker.record_fault("gh", "read_file", "error_timeout")
        engine = SurfacingEngine(config=config, mcp_adapter=adapter, feedback_tracker=tracker)

        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {}

        engine._persist_fault("gh", "read_file", "error_timeout")
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {
            "error_timeout": 2
        }
        await engine.surface("gh", "read_file", _other_args("third"), LONG_RESPONSE)
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {}

    async def test_a_different_key_closing_the_breaker_closes_the_blocked_key(self, tmp_path):
        """Open the real breaker, let it block one key, then let ANOTHER key
        make the half-open probe.

        The breaker is engine-global but ``circuit_open`` rows are per key, so
        the prober is usually not the key that was turned away. Closing only
        the prober's episode leaves the blocked key reading broken with the
        breaker already closed.
        """
        calls = {"n": 0}

        async def failing_then_ok(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("LTM exploded")
            return [], [], "empty_results"

        adapter = AsyncMock()
        adapter.search = failing_then_ok
        config = _make_config(tmp_path, circuit_max_failures=1, circuit_reset_seconds=60.0)
        tracker = FeedbackTracker(config)
        engine = SurfacingEngine(config=config, mcp_adapter=adapter, feedback_tracker=tracker)

        # search_code fails and opens the breaker; read_file is then turned
        # away. (The gate refuses write-shaped tool names, so every key here is
        # a read-shaped one.)
        await engine.surface("gh", "search_code", _other_args("boom"), LONG_RESPONSE)
        assert engine._circuit_breaker.is_open
        await engine.surface("gh", "read_file", _other_args("blocked"), LONG_RESPONSE)
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {
            "error_other": 1,
            "circuit_open": 1,
        }

        # Let the reset window elapse so the breaker reads half-open, then let
        # a THIRD key make the probe that closes it.
        engine._circuit_breaker._opened_at = time.monotonic() - 3600.0
        await engine.surface("gh", "list_docs", _other_args("probe"), LONG_RESPONSE)
        assert engine._circuit_breaker.state == "closed"
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {
            # read_file's circuit_open is closed by the probe; search_code's
            # own error_other is NOT — a probe on another key is no evidence
            # about search_code's LTM call.
            "error_other": 1
        }

    async def test_a_mid_batch_failure_leaves_no_partial_recovery(self, tmp_path):
        """The probe key and the blocked keys close in one transaction.

        Committing them one by one would let a failure part-way through leave
        the survivors reading broken with the breaker already closed — and a
        restart before the in-process retry would make that permanent.
        """
        adapter = AsyncMock()
        adapter.search = AsyncMock(return_value=([], [], "empty_results"))
        config = _make_config(tmp_path)
        tracker = FeedbackTracker(config)
        tracker.record_fault("gh", "read_file", "error_timeout")
        tracker.record_fault("gh", "blocked_tool", "circuit_open")
        engine = SurfacingEngine(config=config, mcp_adapter=adapter, feedback_tracker=tracker)
        engine._breaker_blocked_keys[("gh", "blocked_tool")] = 1

        real_db = tracker.store._db
        failing = _FailNthRecoveryUpdate(real_db, fail_on=2)
        tracker.store._db = failing  # type: ignore[assignment]
        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        # Drain BEFORE restoring: the recovery write is queued on the worker,
        # so putting the real connection back first would let it run against
        # that one and the probe would never see the batch.
        await engine.drain_store_writes()
        tracker.store._db = real_db  # type: ignore[assignment]
        assert failing.attempts == 2  # the batch really did reach the second key

        # Neither key recovered — not the one whose UPDATE had already run.
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {
            "error_timeout": 1,
            "circuit_open": 1,
        }
        # The blocked key is still owed a recovery, so the next success pays it.
        assert set(engine._breaker_blocked_keys) == {("gh", "blocked_tool")}
        await engine.surface("gh", "read_file", _other_args("retry"), LONG_RESPONSE)
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {}

    async def test_a_failed_fault_write_keeps_the_breaker_claim(self, tmp_path):
        """A claim must survive a fault write that raises.

        The two mistakes are not symmetric. A claim whose row never landed is
        inert. But a write whose commit AND rollback both fail leaves the row
        pending for an unrelated later write to publish — and if the claim was
        dropped, that key's episode then stays open until it happens to
        surface on its own, which is the whole bug #869 is about.
        """
        adapter = AsyncMock()
        adapter.search = AsyncMock(return_value=([], [], "empty_results"))
        config = _make_config(tmp_path)
        tracker = FeedbackTracker(config)
        engine = SurfacingEngine(config=config, mcp_adapter=adapter, feedback_tracker=tracker)

        real_db = tracker.store._db
        tracker.store._db = _FailingCommit(real_db, fail_rollback=True)  # type: ignore[assignment]
        engine._persist_fault("gh", "blocked_tool", "circuit_open")
        # The fault write is queued; it has to run against the failing
        # connection, so drain before putting the real one back.
        await engine.drain_store_writes()
        tracker.store._db = real_db  # type: ignore[assignment]
        assert set(engine._breaker_blocked_keys) == {("gh", "blocked_tool")}

        # The abandoned INSERT rides out on an unrelated later write — count 2
        # from one durable fault plus the one that was left pending.
        tracker.record_fault("gh", "blocked_tool", "circuit_open")
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {
            "circuit_open": 2
        }

        # A different key's probe still closes it, because the claim survived.
        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {}

    async def test_trackerless_engine_does_not_accumulate_blocked_keys(self, tmp_path):
        # Nothing durable exists to recover, and the recovery path returns
        # before the set is spent — so it must never fill up.
        engine = SurfacingEngine(config=_make_config(tmp_path), mcp_adapter=AsyncMock())
        engine._persist_fault("gh", "read_file", "circuit_open")
        assert set(engine._breaker_blocked_keys) == set()

    async def test_recovery_is_reattempted_on_every_miss_path_success(self, tmp_path):
        """No once-per-process latch: another process can open an episode on
        this key at any time, and this process must still close it."""
        adapter = AsyncMock()
        adapter.search = AsyncMock(return_value=([], [], "empty_results"))
        config = _make_config(tmp_path)
        tracker = FeedbackTracker(config)
        engine = SurfacingEngine(config=config, mcp_adapter=adapter, feedback_tracker=tracker)

        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        calls = _recovery_call_counter(tracker)
        # A peer process opens an episode this engine never saw.
        peer = FeedbackTracker(config)
        peer.record_fault("gh", "read_file", "error_timeout")
        peer.close()
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {
            "error_timeout": 1
        }

        await engine.surface("gh", "read_file", _other_args("second"), LONG_RESPONSE)
        await engine.drain_store_writes()
        assert calls() == 1
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {}

    async def test_first_success_closes_a_previous_process_episode(self, tmp_path):
        # Rows outlive the process that wrote them, so recovery cannot be
        # gated on having seen the fault in THIS process — that is why every
        # live row from a long-dead daemon sat unrecovered (#869).
        adapter = AsyncMock()
        adapter.search = AsyncMock(return_value=([], [], "empty_results"))
        config = _make_config(tmp_path)
        seed = FeedbackTracker(config)
        seed.record_fault("gh", "read_file", "circuit_open")
        seed.close()

        engine = SurfacingEngine(
            config=config, mcp_adapter=adapter, feedback_tracker=FeedbackTracker(config)
        )
        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {}

    async def test_cache_hit_does_not_close_the_episode(self, tmp_path):
        """A cache hit is served without touching LTM, so it proves nothing
        about LTM's current health — only the miss path may close an episode."""
        adapter = AsyncMock()
        adapter.search = AsyncMock(return_value=([], [], "empty_results"))
        config = _make_config(tmp_path)
        tracker = FeedbackTracker(config)
        engine = SurfacingEngine(config=config, mcp_adapter=adapter, feedback_tracker=tracker)

        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)  # populates cache
        tracker.record_fault("gh", "read_file", "error_timeout")

        calls = _recovery_call_counter(tracker)
        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)  # cache hit
        await engine.drain_store_writes()
        assert calls() == 0
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {
            "error_timeout": 1
        }

        # Positive control: a miss on the same key does close it.
        await engine.surface("gh", "read_file", _other_args("miss"), LONG_RESPONSE)
        await engine.drain_store_writes()
        assert calls() == 1
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {}

    async def test_recovery_write_failure_retries_on_the_next_success(self, tmp_path):
        adapter = AsyncMock()
        adapter.search = AsyncMock(return_value=([], [], "empty_results"))
        config = _make_config(tmp_path)
        tracker = FeedbackTracker(config)
        tracker.record_fault("gh", "read_file", "error_timeout")
        engine = SurfacingEngine(config=config, mcp_adapter=adapter, feedback_tracker=tracker)

        real = tracker.record_fault_recoveries
        failures = {"left": 1}

        def flaky(entries, **kwargs):
            if failures["left"]:
                failures["left"] -= 1
                raise sqlite3.OperationalError("database is locked")
            real(entries, **kwargs)

        tracker.record_fault_recoveries = flaky  # type: ignore[method-assign]
        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {
            "error_timeout": 1
        }

        await engine.surface("gh", "read_file", _other_args("retry"), LONG_RESPONSE)
        await engine.drain_store_writes()
        assert read_surfacing_summary(config.feedback_db_path)["active_faults"] == {}

    async def test_no_tracker_does_not_raise(self, tmp_path):
        async def slow_search(*args, **kwargs):
            await asyncio.sleep(10)
            return [], [], "empty_results"

        adapter = AsyncMock()
        adapter.search = slow_search
        engine = SurfacingEngine(
            config=_make_config(tmp_path, timeout_seconds=0.05),
            mcp_adapter=adapter,
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert output == LONG_RESPONSE

    async def test_stats_retention_sweeps_faults(self, tmp_path):
        adapter = AsyncMock()
        adapter.search = AsyncMock(return_value=([], [], "transport_error"))
        config = _make_config(tmp_path, stats_retention_days=1)
        tracker = FeedbackTracker(config)
        engine = SurfacingEngine(config=config, mcp_adapter=adapter, feedback_tracker=tracker)
        tracker.record_fault("gh", "old_tool", "error_timeout")
        tracker.store._db.execute(
            "UPDATE surfacing_faults SET last_at = ?", (time.time() - 3 * 86400.0,)
        )
        tracker.store._db.commit()
        engine._run_stats_retention(tracker.store)
        await engine.drain_store_writes()
        assert _fault_rows(config.feedback_db_path) == []


# ── Summary + CLI rendering ──────────────────────────────────────────────


class TestSummaryFaults:
    def test_score_diagnostic_recovery_is_persisted_and_reactivates(self, tmp_path, monkeypatch):
        # Windows can return the same time.time() value for adjacent writes.
        # A new diagnostic must re-arm explicitly rather than depend on `>`
        # between timestamps having different resolution.
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: 1_700_000_000.0,
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_diagnostic("gh", "read_file", "score_ceiling_below_min")
        assert read_surfacing_summary(db_path)["active_diagnostics"] == {
            "score_ceiling_below_min": 1
        }

        store.record_diagnostic_recovery("gh", "read_file", "score_ceiling_below_min")
        assert read_surfacing_summary(db_path)["active_diagnostics"] == {}

        store.record_diagnostic("gh", "read_file", "score_ceiling_below_min")
        assert read_surfacing_summary(db_path)["active_diagnostics"] == {
            "score_ceiling_below_min": 2
        }
        store.close()

    def test_scale_mismatch_partitions_records_and_recovers(self, tmp_path, monkeypatch):
        """The #1781 definitive kind flows through the same diagnostics
        partition, active-episode filter, and recovery UPDATE as the
        streak heuristic — and the two kinds recover independently."""
        monkeypatch.setattr(
            "memtomem_stm.surfacing.feedback_store.time.time",
            lambda: 1_700_000_000.0,
        )
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_diagnostic("gh", "read_file", "score_scale_mismatch")
        store.record_diagnostic("gh", "other_tool", "score_ceiling_below_min")

        summary = read_surfacing_summary(db_path)
        assert summary["diagnostics"] == {
            "score_scale_mismatch": 1,
            "score_ceiling_below_min": 1,
        }
        assert summary["faults"] == {}
        assert summary["active_diagnostics"] == {
            "score_scale_mismatch": 1,
            "score_ceiling_below_min": 1,
        }

        store.record_diagnostic_recovery("gh", "read_file", "score_scale_mismatch")
        assert read_surfacing_summary(db_path)["active_diagnostics"] == {
            "score_ceiling_below_min": 1
        }
        store.close()

    def test_initialize_migrates_legacy_fault_recovery_column(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store._db.execute("ALTER TABLE surfacing_faults DROP COLUMN last_recovered_at")
        store._db.commit()
        store.close()

        legacy = read_surfacing_summary(db_path)
        assert legacy["diagnostics_recovery_supported"] is False
        store = FeedbackStore(db_path)
        store.initialize()
        columns = {row[1] for row in store._db.execute("PRAGMA table_info('surfacing_faults')")}
        assert "last_recovered_at" in columns
        store.close()

    @pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
    def test_refused_filter_still_reports_the_recovery_capability(self, tmp_path, surrogate):
        """``diagnostics_recovery_supported`` describes the FILE, not the filter.

        The unencodable-filter guard returns early, so probing the capability
        after it would report a pre-``last_recovered_at`` DB as recovery-capable
        purely because the filter matched nothing. Mirrors the ``schema_outdated``
        placement rule in ``read_compression_summary``.
        """
        db_path = tmp_path / "legacy.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store._db.execute("ALTER TABLE surfacing_faults DROP COLUMN last_recovered_at")
        store._db.commit()
        store.close()

        summary = read_surfacing_summary(db_path, tool=f"t{surrogate}")

        assert summary["available"] is True
        assert summary["diagnostics_recovery_supported"] is False
        assert summary["events_total"] == 0
        # Positive control: the unfiltered read already agreed.
        assert read_surfacing_summary(db_path)["diagnostics_recovery_supported"] is False

    def test_summary_includes_recent_faults(self, tmp_path):
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        store.record_fault("gh", "read_file", "error_timeout")
        store.record_fault("gh", "other_tool", "circuit_open")
        store.close()
        summary = read_surfacing_summary(db_path)
        assert summary["available"] is True
        assert summary["faults"] == {"error_timeout": 2, "circuit_open": 1}
        assert isinstance(summary["faults_last_at"], float)

    def test_summary_tool_filter_applies_to_faults(self, tmp_path):
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        store.record_fault("gh", "other_tool", "circuit_open")
        store.close()
        summary = read_surfacing_summary(db_path, tool="read_file")
        assert summary["faults"] == {"error_timeout": 1}

    def test_summary_partitions_diagnostics_and_applies_tool_filter(self, tmp_path):
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        store.record_diagnostic("gh", "read_file", "score_ceiling_below_min")
        store.record_diagnostic("gh", "other_tool", "score_ceiling_below_min")
        store.close()

        summary = read_surfacing_summary(db_path, tool="read_file")
        assert summary["faults"] == {"error_timeout": 1}
        assert summary["diagnostics"] == {"score_ceiling_below_min": 1}
        assert isinstance(summary["diagnostics_last_at"], float)
        assert summary["diagnostics_window_days"] == 7

    def test_summary_faults_outside_window_excluded(self, tmp_path):
        # The window filter is on the calendar-day bucket, not last_at, so a
        # row whose *day* predates the window is excluded even though its
        # last_at value is irrelevant to the filter. Backdate both to an old
        # UTC day 8 days ago.
        old_ts = time.time() - 8 * 86400.0
        old_day = time.strftime("%Y-%m-%d", time.gmtime(old_ts))
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        store._db.execute("UPDATE surfacing_faults SET day = ?, last_at = ?", (old_day, old_ts))
        store._db.commit()
        store.close()
        summary = read_surfacing_summary(db_path)
        assert summary["faults"] == {}
        assert summary["faults_last_at"] is None

    def test_summary_window_is_calendar_day_exact(self, tmp_path):
        # Regression (codex review #666): the window filter must not sum a
        # boundary day's whole bucket when only part of it is in range. With
        # day-granular filtering, a bucket dated just inside the window is
        # fully counted and one dated just outside is fully excluded — no
        # partial-day over-count. Row A on the inclusive cutoff day (today −
        # (WINDOW−1)) counts; row B one day older does not.
        from memtomem_stm.surfacing.feedback_store import _FAULT_SUMMARY_WINDOW_DAYS

        now = time.time()
        in_day = time.strftime(
            "%Y-%m-%d", time.gmtime(now - (_FAULT_SUMMARY_WINDOW_DAYS - 1) * 86400.0)
        )
        out_day = time.strftime("%Y-%m-%d", time.gmtime(now - _FAULT_SUMMARY_WINDOW_DAYS * 86400.0))
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store._db.executemany(
            "INSERT INTO surfacing_faults (day, server, tool, kind, count, last_at) "
            "VALUES (?, 'gh', 'read_file', 'error_timeout', ?, ?)",
            [(in_day, 5, now), (out_day, 9, now)],
        )
        store._db.commit()
        store.close()
        summary = read_surfacing_summary(db_path)
        assert summary["faults"] == {"error_timeout": 5}

    def test_summary_tolerates_pre_faults_schema(self, tmp_path):
        # A DB last written by a pre-faults version has no surfacing_faults
        # table; the summary must stay available with empty counters instead
        # of erroring out the whole stats block.
        db_path = tmp_path / "old.db"
        db = sqlite3.connect(str(db_path))
        db.execute(
            "CREATE TABLE surfacing_events ("
            "id TEXT PRIMARY KEY, server TEXT NOT NULL, tool TEXT NOT NULL, query TEXT, "
            "memory_ids TEXT NOT NULL, scores TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        db.commit()
        db.close()
        summary = read_surfacing_summary(db_path)
        assert summary["available"] is True
        assert summary["faults"] == {}
        assert summary["diagnostics"] == {}

    def test_render_block_shows_faults_and_warning(self, tmp_path, capsys):
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("builtin", "Bash", "error_timeout")
        store.close()
        _render_surfacing_block(read_surfacing_summary(db_path))
        out = capsys.readouterr().out
        assert "pipeline faults (last 7 UTC days):" in out
        assert "error_timeout" in out
        assert "last fault:" in out
        assert "degraded-LTM faults" in out

    def test_summary_tool_filter_applies_to_active_faults(self, tmp_path):
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        store.record_fault("gh", "other_tool", "ltm_unavailable")
        store.close()
        assert read_surfacing_summary(db_path, tool="read_file")["active_faults"] == {
            "error_timeout": 1
        }

    def test_summary_active_faults_need_the_recovery_column(self, tmp_path):
        # A DB written before the column existed cannot tell an open episode
        # from a recovered one, so it reports neither rather than guessing.
        db_path = tmp_path / "legacy.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        store._db.execute("ALTER TABLE surfacing_faults DROP COLUMN last_recovered_at")
        store._db.commit()
        store.close()

        summary = read_surfacing_summary(db_path)
        assert summary["faults_recovery_supported"] is False
        assert summary["active_faults"] == {}
        assert summary["faults"] == {"error_timeout": 1}

    def test_render_block_recovered_faults_drop_the_warning(self, tmp_path, capsys):
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("builtin", "Bash", "error_timeout")
        store.record_fault_recovery("builtin", "Bash", recovered_at=time.time())
        store.close()
        _render_surfacing_block(read_surfacing_summary(db_path))
        out = capsys.readouterr().out
        # The history still renders — recovery closes the episode, it does not
        # hide the evidence.
        assert "pipeline faults (last 7 UTC days):" in out
        assert "error_timeout" in out
        assert "degraded-LTM faults" not in out
        assert "all listed fault episodes recovered" in out

    def test_render_block_warns_when_recovery_is_unsupported(self, tmp_path, capsys):
        # Pre-column DBs keep today's unconditional warning: silence there
        # would claim a recovery the file cannot record.
        db_path = tmp_path / "legacy.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("builtin", "Bash", "error_timeout")
        store._db.execute("ALTER TABLE surfacing_faults DROP COLUMN last_recovered_at")
        store._db.commit()
        store.close()
        _render_surfacing_block(read_surfacing_summary(db_path))
        out = capsys.readouterr().out
        assert "degraded-LTM faults" in out
        assert "all listed fault episodes recovered" not in out

    def test_render_block_silent_without_faults(self, tmp_path, capsys):
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.close()
        _render_surfacing_block(read_surfacing_summary(db_path))
        out = capsys.readouterr().out
        assert "pipeline faults" not in out
        assert "degraded-LTM" not in out
        assert "score-scale diagnostics" not in out

    def test_render_block_shows_diagnostic_without_fault_guidance(self, tmp_path, capsys):
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_diagnostic("gh", "read_file", "score_ceiling_below_min")
        store.close()

        _render_surfacing_block(read_surfacing_summary(db_path))
        out = capsys.readouterr().out
        assert "score-scale diagnostics (last 7 UTC days):" in out
        assert "score_ceiling_below_min" in out
        assert "single-leg/BM25-only" in out
        assert "STM did not lower the threshold" in out
        assert "pipeline faults" not in out
        assert "degraded-LTM faults" not in out

    def test_render_block_mixed_fault_and_diagnostic_guidance(self, tmp_path, capsys):
        db_path = tmp_path / "f.db"
        store = FeedbackStore(db_path)
        store.initialize()
        store.record_fault("gh", "read_file", "error_timeout")
        store.record_diagnostic("gh", "read_file", "score_ceiling_below_min")
        store.close()

        _render_surfacing_block(read_surfacing_summary(db_path))
        out = capsys.readouterr().out
        assert "pipeline faults" in out
        assert "degraded-LTM faults" in out
        assert "score-scale diagnostics" in out
        assert "single-leg/BM25-only" in out
