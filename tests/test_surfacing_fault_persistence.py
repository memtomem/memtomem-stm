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
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

from memtomem_stm.cli.proxy import _render_surfacing_block
from memtomem_stm.surfacing.config import SurfacingConfig
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
        assert DIAGNOSTIC_KINDS == {"score_ceiling_below_min"}
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
        assert _fault_rows(config.feedback_db_path) == [("gh", "read_file", "ltm_unavailable", 1)]

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
        assert _fault_rows(config.feedback_db_path) == []


# ── Summary + CLI rendering ──────────────────────────────────────────────


class TestSummaryFaults:
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
