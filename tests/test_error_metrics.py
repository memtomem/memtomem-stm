"""Tests for error classification metrics (Phase 1 of gateway improvements)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from memtomem_stm.proxy.config import CompressionStrategy, ProxyConfig, UpstreamServerConfig
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import (
    MAX_ERROR_MESSAGE_CHARS,
    CallMetrics,
    ErrorCategory,
    TokenTracker,
)
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.utils.circuit_breaker import CircuitBreaker


# ── ErrorCategory enum ───────────────────────────────────────────────────


class TestErrorCategory:
    def test_values(self):
        assert ErrorCategory.TRANSPORT == "transport"
        assert ErrorCategory.TIMEOUT == "timeout"
        assert ErrorCategory.PROTOCOL == "protocol"
        assert ErrorCategory.UPSTREAM_ERROR == "upstream_error"
        assert ErrorCategory.PROGRAMMING == "programming"

    def test_is_strenum(self):
        assert isinstance(ErrorCategory.TRANSPORT, str)


# ── CallMetrics error fields ─────────────────────────────────────────────


class TestCallMetricsErrorFields:
    def test_defaults(self):
        m = CallMetrics(server="s", tool="t", original_chars=100, compressed_chars=50)
        assert m.is_error is False
        assert m.error_category is None
        assert m.error_code is None

    def test_error_fields(self):
        m = CallMetrics(
            server="s",
            tool="t",
            original_chars=0,
            compressed_chars=0,
            is_error=True,
            error_category=ErrorCategory.PROTOCOL,
            error_code=-32601,
        )
        assert m.is_error is True
        assert m.error_category == ErrorCategory.PROTOCOL
        assert m.error_code == -32601


# ── TokenTracker.record_error ────────────────────────────────────────────


class TestTokenTrackerRecordError:
    def test_record_error_increments_total(self):
        tracker = TokenTracker()
        tracker.record_error(
            CallMetrics(
                server="s",
                tool="t",
                original_chars=0,
                compressed_chars=0,
                is_error=True,
                error_category=ErrorCategory.TRANSPORT,
            )
        )
        assert tracker._total_errors == 1

    def test_record_error_by_category(self):
        tracker = TokenTracker()
        tracker.record_error(
            CallMetrics(
                server="s",
                tool="t",
                original_chars=0,
                compressed_chars=0,
                is_error=True,
                error_category=ErrorCategory.TRANSPORT,
            )
        )
        tracker.record_error(
            CallMetrics(
                server="s",
                tool="t",
                original_chars=0,
                compressed_chars=0,
                is_error=True,
                error_category=ErrorCategory.TRANSPORT,
            )
        )
        tracker.record_error(
            CallMetrics(
                server="s",
                tool="t",
                original_chars=0,
                compressed_chars=0,
                is_error=True,
                error_category=ErrorCategory.PROTOCOL,
            )
        )
        assert tracker._errors_by_category["transport"] == 2
        assert tracker._errors_by_category["protocol"] == 1

    def test_record_error_by_server(self):
        tracker = TokenTracker()
        tracker.record_error(
            CallMetrics(
                server="srv1",
                tool="t",
                original_chars=0,
                compressed_chars=0,
                is_error=True,
                error_category=ErrorCategory.TIMEOUT,
            )
        )
        tracker.record_error(
            CallMetrics(
                server="srv2",
                tool="t",
                original_chars=0,
                compressed_chars=0,
                is_error=True,
                error_category=ErrorCategory.TIMEOUT,
            )
        )
        assert tracker._errors_by_server["srv1"] == 1
        assert tracker._errors_by_server["srv2"] == 1

    def test_record_error_none_category(self):
        tracker = TokenTracker()
        tracker.record_error(
            CallMetrics(
                server="s",
                tool="t",
                original_chars=0,
                compressed_chars=0,
                is_error=True,
            )
        )
        assert tracker._total_errors == 1
        assert len(tracker._errors_by_category) == 0


# ── TokenTracker.record_hints (B3 — parent trust-UX forwarding) ──────────


class TestTokenTrackerRecordHints:
    def test_record_hints_increments_events_and_snapshots(self):
        tracker = TokenTracker()
        tracker.record_hints(["first notice", "second notice"])
        assert tracker._total_hint_events == 1
        assert tracker._last_hints == ["first notice", "second notice"]

    def test_record_hints_empty_is_noop(self):
        tracker = TokenTracker()
        tracker.record_hints([])
        assert tracker._total_hint_events == 0
        assert tracker._last_hints == []

    def test_record_hints_overwrites_snapshot(self):
        tracker = TokenTracker()
        tracker.record_hints(["call 1 hint"])
        tracker.record_hints(["call 2 hint A", "call 2 hint B"])
        assert tracker._total_hint_events == 2
        assert tracker._last_hints == ["call 2 hint A", "call 2 hint B"]

    def test_record_hints_stores_defensive_copy(self):
        """Caller-owned list mutations must not bleed into the snapshot."""
        tracker = TokenTracker()
        hints = ["only hint"]
        tracker.record_hints(hints)
        hints.append("mutated after call")
        assert tracker._last_hints == ["only hint"]

    def test_get_summary_exposes_hints(self):
        tracker = TokenTracker()
        tracker.record_hints(["visible"])
        summary = tracker.get_summary()
        assert summary["total_hint_events"] == 1
        assert summary["last_hints"] == ["visible"]

    def test_get_summary_hints_defaults_when_none(self):
        tracker = TokenTracker()
        summary = tracker.get_summary()
        assert summary["total_hint_events"] == 0
        assert summary["last_hints"] == []


# ── get_summary error fields ─────────────────────────────────────────────


class TestSummaryErrorFields:
    def test_no_errors(self):
        tracker = TokenTracker()
        s = tracker.get_summary()
        assert s["total_errors"] == 0
        assert s["errors_by_category"] == {}
        assert s["error_rate"] == 0.0

    def test_error_rate_calculation(self):
        tracker = TokenTracker()
        # 3 successful calls
        for _ in range(3):
            tracker.record(
                CallMetrics(server="s", tool="t", original_chars=100, compressed_chars=50)
            )
        # 1 error
        tracker.record_error(
            CallMetrics(
                server="s",
                tool="t",
                original_chars=0,
                compressed_chars=0,
                is_error=True,
                error_category=ErrorCategory.TRANSPORT,
            )
        )
        s = tracker.get_summary()
        assert s["total_calls"] == 3
        assert s["total_errors"] == 1
        # error_rate = 1 / (3 + 1) * 100 = 25.0%
        assert s["error_rate"] == 25.0

    def test_all_errors(self):
        tracker = TokenTracker()
        for _ in range(5):
            tracker.record_error(
                CallMetrics(
                    server="s",
                    tool="t",
                    original_chars=0,
                    compressed_chars=0,
                    is_error=True,
                    error_category=ErrorCategory.TIMEOUT,
                )
            )
        s = tracker.get_summary()
        assert s["error_rate"] == 100.0
        assert s["errors_by_category"] == {"timeout": 5}


# ── MetricsStore migration ──────────────────────────────────────────────


class TestMetricsStoreMigration:
    def test_fresh_db_has_error_columns(self, tmp_path):
        store = MetricsStore(tmp_path / "test.db")
        store.initialize()
        cols = {row[1] for row in store._db.execute("PRAGMA table_info(proxy_metrics)")}
        assert "is_error" in cols
        assert "error_category" in cols
        assert "error_code" in cols
        store.close()

    def test_existing_db_migrated(self, tmp_path):
        """Pre-existing DB without error columns gets migrated."""
        import sqlite3

        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE proxy_metrics ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "server TEXT NOT NULL, tool TEXT NOT NULL, "
            "original_chars INTEGER NOT NULL, compressed_chars INTEGER NOT NULL, "
            "cleaned_chars INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)"
        )
        conn.commit()
        conn.close()

        store = MetricsStore(db_path)
        store.initialize()
        cols = {row[1] for row in store._db.execute("PRAGMA table_info(proxy_metrics)")}
        assert "error_category" in cols
        store.close()

    def test_record_with_error_fields(self, tmp_path):
        store = MetricsStore(tmp_path / "test.db")
        store.initialize()
        store.record(
            CallMetrics(
                server="srv",
                tool="tool",
                original_chars=0,
                compressed_chars=0,
                is_error=True,
                error_category=ErrorCategory.PROTOCOL,
                error_code=-32601,
            )
        )
        row = store._db.execute(
            "SELECT is_error, error_category, error_code FROM proxy_metrics"
        ).fetchone()
        assert row == (1, "protocol", -32601)
        store.close()

    def test_record_success_has_defaults(self, tmp_path):
        store = MetricsStore(tmp_path / "test.db")
        store.initialize()
        store.record(
            CallMetrics(
                server="srv",
                tool="tool",
                original_chars=100,
                compressed_chars=50,
            )
        )
        row = store._db.execute(
            "SELECT is_error, error_category, error_code FROM proxy_metrics"
        ).fetchone()
        assert row == (0, None, None)
        store.close()

    def test_fresh_db_has_error_message_column(self, tmp_path):
        store = MetricsStore(tmp_path / "test.db")
        store.initialize()
        cols = {row[1] for row in store._db.execute("PRAGMA table_info(proxy_metrics)")}
        assert "error_message" in cols
        store.close()

    def test_existing_db_migrates_error_message(self, tmp_path):
        """Pre-existing DB without error_message gets migrated."""
        import sqlite3

        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE proxy_metrics ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "server TEXT NOT NULL, tool TEXT NOT NULL, "
            "original_chars INTEGER NOT NULL, compressed_chars INTEGER NOT NULL, "
            "cleaned_chars INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)"
        )
        conn.commit()
        conn.close()

        store = MetricsStore(db_path)
        store.initialize()
        cols = {row[1] for row in store._db.execute("PRAGMA table_info(proxy_metrics)")}
        assert "error_message" in cols
        store.close()

    def test_record_persists_error_message(self, tmp_path):
        store = MetricsStore(tmp_path / "test.db")
        store.initialize()
        store.record(
            CallMetrics(
                server="srv",
                tool="tool",
                original_chars=0,
                compressed_chars=0,
                is_error=True,
                error_category=ErrorCategory.PROTOCOL,
                error_code=-32602,
                error_message="ValueError: Invalid params: page_path",
            )
        )
        row = store._db.execute(
            "SELECT error_category, error_code, error_message FROM proxy_metrics"
        ).fetchone()
        assert row == ("protocol", -32602, "ValueError: Invalid params: page_path")
        store.close()


# ── ProxyManager integration ─────────────────────────────────────────────


def _text_content(text: str):
    return SimpleNamespace(type="text", text=text)


def _make_result(text: str, is_error: bool = False):
    return SimpleNamespace(content=[_text_content(text)], isError=is_error)


def _make_manager(max_retries: int = 0) -> ProxyManager:
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=CompressionStrategy.NONE,
        max_result_chars=50000,
        max_retries=max_retries,
        reconnect_delay_seconds=0.0,
    )
    proxy_cfg = ProxyConfig(
        config_path=Path("/tmp/proxy.json"),
        upstream_servers={"srv": server_cfg},
    )
    mgr = ProxyManager(proxy_cfg, TokenTracker())
    session = AsyncMock()
    mgr._connections["srv"] = UpstreamConnection(
        name="srv",
        config=server_cfg,
        session=session,
        tools=[],
    )
    return mgr


class TestManagerErrorRecording:
    async def test_programming_error_records_metric(self):
        mgr = _make_manager()
        mgr._connections["srv"].session.call_tool.side_effect = TypeError("bad")
        with pytest.raises(TypeError):
            await mgr.call_tool("srv", "tool", {})
        s = mgr.tracker.get_summary()
        assert s["total_errors"] == 1
        assert s["errors_by_category"]["programming"] == 1

    async def test_protocol_error_records_metric(self):
        mgr = _make_manager()
        exc = Exception("bad params")
        exc.error = SimpleNamespace(code=-32602)
        mgr._connections["srv"].session.call_tool.side_effect = exc
        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(Exception):
                await mgr.call_tool("srv", "tool", {})
        s = mgr.tracker.get_summary()
        assert s["errors_by_category"]["protocol"] == 1

    async def test_transport_error_records_metric(self):
        mgr = _make_manager(max_retries=0)
        mgr._connections["srv"].session.call_tool.side_effect = ConnectionError("down")
        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})
        s = mgr.tracker.get_summary()
        assert s["errors_by_category"]["transport"] == 1

    async def test_timeout_error_records_timeout_category(self):
        mgr = _make_manager(max_retries=0)
        mgr._connections["srv"].session.call_tool.side_effect = asyncio.TimeoutError()
        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(asyncio.TimeoutError):
                await mgr.call_tool("srv", "tool", {})
        s = mgr.tracker.get_summary()
        assert s["errors_by_category"]["timeout"] == 1

    async def test_upstream_error_records_metric(self):
        from mcp.server.fastmcp.exceptions import ToolError

        mgr = _make_manager()
        mgr._connections["srv"].session.call_tool.return_value = _make_result(
            "Error: not found", is_error=True
        )
        with pytest.raises(ToolError, match="not found"):
            await mgr.call_tool("srv", "tool", {})
        s = mgr.tracker.get_summary()
        assert s["errors_by_category"]["upstream_error"] == 1

    async def test_success_does_not_record_error(self):
        mgr = _make_manager()
        mgr._connections["srv"].session.call_tool.return_value = _make_result("ok")
        await mgr.call_tool("srv", "tool", {})
        s = mgr.tracker.get_summary()
        assert s["total_errors"] == 0
        assert s["total_calls"] == 1


# ── error_message persistence (issue #253) ───────────────────────────────


def _make_manager_with_store(tmp_path: Path, max_retries: int = 0) -> ProxyManager:
    """Like ``_make_manager`` but with a real MetricsStore so SQL assertions work."""
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=CompressionStrategy.NONE,
        max_result_chars=50000,
        max_retries=max_retries,
        reconnect_delay_seconds=0.0,
    )
    proxy_cfg = ProxyConfig(
        config_path=Path("/tmp/proxy.json"),
        upstream_servers={"srv": server_cfg},
    )
    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    tracker = TokenTracker(metrics_store=store)
    mgr = ProxyManager(proxy_cfg, tracker)
    mgr._connections["srv"] = UpstreamConnection(
        name="srv",
        config=server_cfg,
        session=AsyncMock(),
        tools=[],
    )
    mgr._test_store = store  # noqa: SLF001 — held for SQL assertions in tests
    return mgr


def _read_error_row(mgr: ProxyManager) -> tuple:
    return mgr._test_store._db.execute(  # noqa: SLF001
        "SELECT error_category, error_code, error_message FROM proxy_metrics"
    ).fetchone()


class TestErrorMessagePersistence:
    """Each ErrorCategory writes ``error_message`` to ``proxy_metrics.db``.

    Without this, post-mortem inspection cannot distinguish (e.g.) a wrong
    argument name from a rate-limit hit when both surface as ``upstream_error``
    or ``protocol`` rows. See issue #253.
    """

    async def test_programming_persists_message(self, tmp_path):
        mgr = _make_manager_with_store(tmp_path)
        mgr._connections["srv"].session.call_tool.side_effect = TypeError("bad arg")
        with pytest.raises(TypeError):
            await mgr.call_tool("srv", "tool", {})
        cat, code, msg = _read_error_row(mgr)
        assert cat == "programming"
        assert code is None
        assert msg == "TypeError: bad arg"

    async def test_protocol_persists_message_with_code(self, tmp_path):
        mgr = _make_manager_with_store(tmp_path)
        exc = Exception("Invalid params: page_path")
        exc.error = SimpleNamespace(code=-32602)
        mgr._connections["srv"].session.call_tool.side_effect = exc
        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(Exception):
                await mgr.call_tool("srv", "tool", {})
        cat, code, msg = _read_error_row(mgr)
        assert cat == "protocol"
        assert code == -32602
        assert msg is not None and "Invalid params: page_path" in msg

    async def test_transport_persists_message(self, tmp_path):
        mgr = _make_manager_with_store(tmp_path, max_retries=0)
        mgr._connections["srv"].session.call_tool.side_effect = ConnectionError("down")
        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})
        cat, _code, msg = _read_error_row(mgr)
        assert cat == "transport"
        assert msg == "ConnectionError: down"

    async def test_timeout_persists_message(self, tmp_path):
        mgr = _make_manager_with_store(tmp_path, max_retries=0)
        mgr._connections["srv"].session.call_tool.side_effect = asyncio.TimeoutError()
        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(asyncio.TimeoutError):
                await mgr.call_tool("srv", "tool", {})
        cat, _code, msg = _read_error_row(mgr)
        assert cat == "timeout"
        assert msg is not None and msg.startswith("TimeoutError")

    async def test_upstream_error_persists_original_text(self, tmp_path):
        from mcp.server.fastmcp.exceptions import ToolError

        mgr = _make_manager_with_store(tmp_path)
        mgr._connections["srv"].session.call_tool.return_value = _make_result(
            "Error: page slug 'foo' not found", is_error=True
        )
        with pytest.raises(ToolError):
            await mgr.call_tool("srv", "tool", {})
        cat, _code, msg = _read_error_row(mgr)
        assert cat == "upstream_error"
        assert msg == "Error: page slug 'foo' not found"

    async def test_message_truncated_at_cap(self, tmp_path):
        from mcp.server.fastmcp.exceptions import ToolError

        mgr = _make_manager_with_store(tmp_path)
        long_text = "x" * (MAX_ERROR_MESSAGE_CHARS + 250)
        mgr._connections["srv"].session.call_tool.return_value = _make_result(
            long_text, is_error=True
        )
        with pytest.raises(ToolError):
            await mgr.call_tool("srv", "tool", {})
        _cat, _code, msg = _read_error_row(mgr)
        assert msg is not None
        assert len(msg) == MAX_ERROR_MESSAGE_CHARS

    async def test_success_persists_null_message(self, tmp_path):
        mgr = _make_manager_with_store(tmp_path)
        mgr._connections["srv"].session.call_tool.return_value = _make_result("ok")
        await mgr.call_tool("srv", "tool", {})
        row = mgr._test_store._db.execute(  # noqa: SLF001
            "SELECT is_error, error_message FROM proxy_metrics"
        ).fetchone()
        assert row == (0, None)

    async def test_internal_error_persists_message(self, tmp_path):
        """A pipeline-stage exception (CLEAN/COMPRESS/SURFACE/INDEX) escaping
        ``_call_tool_inner`` is recorded as INTERNAL_ERROR by the outer wrapper.
        The per-stage error column may not be set on every path — error_message
        gives a single column to query for any pipeline failure text."""
        mgr = _make_manager_with_store(tmp_path)
        mgr._connections["srv"].session.call_tool.return_value = _make_result("hello")
        with patch.object(
            mgr, "_apply_compression", new_callable=AsyncMock, side_effect=RuntimeError("boom")
        ):
            with pytest.raises(RuntimeError, match="boom"):
                await mgr.call_tool("srv", "tool", {})
        cat, _code, msg = _read_error_row(mgr)
        assert cat == "internal_error"
        assert msg == "RuntimeError: boom"

    async def test_lock_timeout_persists_message(self, tmp_path):
        """LockTimeoutError (#208) escapes ``bounded_lock`` *before* the
        per-stage ``index_error`` / ``extract_error`` / ``surface_error``
        columns get populated. Without ``error_message`` the lock_timeout row
        was all-NULL across diagnostic text, defeating the post-mortem use
        case this PR is meant to enable."""
        from memtomem_stm.proxy._locks import LockTimeoutError

        mgr = _make_manager_with_store(tmp_path)
        with patch.object(
            mgr,
            "_call_tool_guarded",
            new_callable=AsyncMock,
            side_effect=LockTimeoutError("stm_lock", 5.0),
        ):
            with pytest.raises(LockTimeoutError):
                await mgr.call_tool("srv", "tool", {})
        cat, _code, msg = _read_error_row(mgr)
        assert cat == "lock_timeout"
        assert msg is not None
        assert "stm_lock" in msg
        assert msg.startswith("LockTimeoutError")


# ── CircuitBreaker properties ────────────────────────────────────────────


class TestCircuitBreakerProperties:
    def test_initial_state(self):
        cb = CircuitBreaker(max_failures=3, reset_timeout=60.0)
        assert cb.state == "closed"
        assert cb.failure_count == 0
        assert cb.time_until_reset is None

    def test_state_after_failures(self):
        cb = CircuitBreaker(max_failures=2, reset_timeout=60.0)
        cb.record_failure()
        assert cb.state == "closed"
        assert cb.failure_count == 1
        cb.record_failure()
        assert cb.state == "open"
        assert cb.failure_count == 2
        assert cb.time_until_reset is not None
        assert cb.time_until_reset > 0

    def test_state_after_success(self):
        cb = CircuitBreaker(max_failures=2, reset_timeout=60.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "open"
        # Simulate timeout elapsed
        cb._opened_at = time.monotonic() - 61.0
        assert cb.state == "half-open"
        cb.record_success()
        assert cb.state == "closed"
        assert cb.failure_count == 0

    def test_time_until_reset_decreases(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()
        t1 = cb.time_until_reset
        assert t1 is not None
        assert t1 <= 10.0
        cb._opened_at = time.monotonic() - 8.0
        t2 = cb.time_until_reset
        assert t2 is not None
        assert t2 <= 2.5
