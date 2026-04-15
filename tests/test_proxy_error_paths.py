"""Tests for ProxyManager error paths — transport failure, protocol error, reconnect, timeout."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker


# ── Helpers ──────────────────────────────────────────────────────────────


def _text_content(text: str):
    """Create a mock MCP TextContent."""
    return SimpleNamespace(type="text", text=text)


def _make_result(text: str, is_error: bool = False):
    """Create a mock call_tool result."""
    return SimpleNamespace(content=[_text_content(text)], isError=is_error)


def _make_manager(
    max_retries: int = 3,
    reconnect_delay: float = 0.0,
    max_reconnect_delay: float = 0.0,
    compression: CompressionStrategy = CompressionStrategy.NONE,
    max_result_chars: int = 50000,
    tmp_path: Path | None = None,
) -> ProxyManager:
    """Create a ProxyManager with a mocked upstream connection."""
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=compression,
        max_result_chars=max_result_chars,
        max_retries=max_retries,
        reconnect_delay_seconds=reconnect_delay,
        max_reconnect_delay_seconds=max_reconnect_delay,
    )
    config_path = (tmp_path / "proxy.json") if tmp_path else Path("/tmp/proxy.json")
    proxy_cfg = ProxyConfig(
        config_path=config_path,
        upstream_servers={"srv": server_cfg},
    )
    tracker = TokenTracker()
    mgr = ProxyManager(proxy_cfg, tracker)

    # Inject a mocked connection
    session = AsyncMock()
    conn = UpstreamConnection(
        name="srv",
        config=server_cfg,
        session=session,
        tools=[],
    )
    mgr._connections["srv"] = conn
    return mgr


def _get_session(mgr: ProxyManager) -> AsyncMock:
    return mgr._connections["srv"].session


# ── Transport failure: retry + reconnect ─────────────────────────────────


class TestTransportFailureRetry:
    """OSError, ConnectionError, TimeoutError, EOFError → retry with reconnect."""

    @pytest.mark.parametrize(
        "exc_type",
        [OSError, ConnectionError, asyncio.TimeoutError, EOFError],
        ids=["OSError", "ConnectionError", "TimeoutError", "EOFError"],
    )
    async def test_retryable_error_succeeds_on_second_attempt(self, exc_type):
        mgr = _make_manager(max_retries=3)
        session = _get_session(mgr)
        session.call_tool.side_effect = [exc_type("fail"), _make_result("ok")]

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            result = await mgr.call_tool("srv", "tool", {})

        assert result == "ok"
        assert session.call_tool.call_count == 2
        mock_reconnect.assert_awaited_once_with("srv")

    async def test_retries_exhaust_then_raises(self):
        mgr = _make_manager(max_retries=2)
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("down")

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError, match="down"):
                await mgr.call_tool("srv", "tool", {})

        # initial + 2 retries = 3 attempts
        assert session.call_tool.call_count == 3

    async def test_reconnect_called_on_each_retry(self):
        mgr = _make_manager(max_retries=2)
        session = _get_session(mgr)
        session.call_tool.side_effect = [
            OSError("fail1"),
            OSError("fail2"),
            _make_result("ok"),
        ]

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            result = await mgr.call_tool("srv", "tool", {})

        assert result == "ok"
        # 2 failures → 2 reconnects during retry loop
        assert mock_reconnect.await_count == 2

    async def test_tracker_records_reconnects(self):
        mgr = _make_manager(max_retries=3)
        session = _get_session(mgr)
        session.call_tool.side_effect = [
            ConnectionError("fail1"),
            ConnectionError("fail2"),
            _make_result("ok"),
        ]

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            await mgr.call_tool("srv", "tool", {})

        summary = mgr.tracker.get_summary()
        assert summary["reconnects"] == 2

    async def test_exponential_backoff_delay(self):
        mgr = _make_manager(max_retries=3, reconnect_delay=1.0, max_reconnect_delay=10.0)
        session = _get_session(mgr)
        session.call_tool.side_effect = [
            OSError("1"),
            OSError("2"),
            OSError("3"),
            _make_result("ok"),
        ]

        sleep_delays: list[float] = []

        async def capture_sleep(seconds):
            sleep_delays.append(seconds)

        with (
            patch.object(mgr, "_reconnect_server", new_callable=AsyncMock),
            patch("memtomem_stm.proxy.manager.asyncio.sleep", side_effect=capture_sleep),
        ):
            await mgr.call_tool("srv", "tool", {})

        # delay doubles: 1.0 → 2.0 → 4.0
        assert sleep_delays == [1.0, 2.0, 4.0]

    async def test_backoff_capped_at_max(self):
        mgr = _make_manager(max_retries=3, reconnect_delay=5.0, max_reconnect_delay=8.0)
        session = _get_session(mgr)
        session.call_tool.side_effect = [
            OSError("1"),
            OSError("2"),
            OSError("3"),
            _make_result("ok"),
        ]

        sleep_delays: list[float] = []

        async def capture_sleep(seconds):
            sleep_delays.append(seconds)

        with (
            patch.object(mgr, "_reconnect_server", new_callable=AsyncMock),
            patch("memtomem_stm.proxy.manager.asyncio.sleep", side_effect=capture_sleep),
        ):
            await mgr.call_tool("srv", "tool", {})

        # 5.0 → min(10.0, 8.0) = 8.0 → min(16.0, 8.0) = 8.0
        assert sleep_delays == [5.0, 8.0, 8.0]

    async def test_post_exhaustion_reconnect_attempted(self):
        """After all retries fail, a final reconnect is attempted before raising."""
        mgr = _make_manager(max_retries=1)
        session = _get_session(mgr)
        session.call_tool.side_effect = OSError("persistent")

        reconnect_calls: list[str] = []

        async def track_reconnect(name):
            reconnect_calls.append(name)

        with patch.object(mgr, "_reconnect_server", side_effect=track_reconnect):
            with pytest.raises(OSError):
                await mgr.call_tool("srv", "tool", {})

        # 1 reconnect during retry + 1 post-exhaustion reconnect
        assert len(reconnect_calls) == 2


# ── Protocol error: no retry ────────────────────────────────────────────


class TestProtocolError:
    """JSON-RPC errors with _NO_RETRY_CODES raise immediately, no retry."""

    @pytest.mark.parametrize(
        "code",
        [-32600, -32601, -32602, -32603],
        ids=["INVALID_REQUEST", "METHOD_NOT_FOUND", "INVALID_PARAMS", "INTERNAL_ERROR"],
    )
    async def test_protocol_error_no_retry(self, code):
        mgr = _make_manager(max_retries=3)
        session = _get_session(mgr)

        exc = Exception("protocol error")
        exc.error = SimpleNamespace(code=code)
        session.call_tool.side_effect = exc

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            with pytest.raises(Exception, match="protocol error"):
                await mgr.call_tool("srv", "tool", {})

        # Only 1 attempt — no retries
        assert session.call_tool.call_count == 1
        # But reconnect IS called to keep connection healthy
        mock_reconnect.assert_awaited_once_with("srv")

    async def test_protocol_error_reconnect_failure_still_raises_original(self):
        """If reconnect fails after protocol error, the original error propagates."""
        mgr = _make_manager(max_retries=3)
        session = _get_session(mgr)

        exc = Exception("bad params")
        exc.error = SimpleNamespace(code=-32602)
        session.call_tool.side_effect = exc

        with patch.object(
            mgr, "_reconnect_server", new_callable=AsyncMock, side_effect=OSError("reconnect fail")
        ):
            with pytest.raises(Exception, match="bad params"):
                await mgr.call_tool("srv", "tool", {})


# ── Non-retryable errors: immediate propagation ─────────────────────────


class TestNonRetryableErrors:
    """TypeError, AttributeError, ValueError etc. propagate immediately."""

    @pytest.mark.parametrize(
        "exc_type,msg",
        [
            (TypeError, "wrong type"),
            (AttributeError, "no attr"),
            (ValueError, "bad value"),
            (KeyError, "missing key"),
        ],
        ids=["TypeError", "AttributeError", "ValueError", "KeyError"],
    )
    async def test_programming_error_no_retry(self, exc_type, msg):
        mgr = _make_manager(max_retries=3)
        session = _get_session(mgr)
        session.call_tool.side_effect = exc_type(msg)

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            with pytest.raises(exc_type):
                await mgr.call_tool("srv", "tool", {})

        assert session.call_tool.call_count == 1
        mock_reconnect.assert_not_awaited()


# ── Reconnect failure scenarios ──────────────────────────────────────────


class TestReconnectFailure:
    """When _reconnect_server itself fails during retry loop."""

    async def test_reconnect_failure_during_retry_raises(self):
        mgr = _make_manager(max_retries=2)
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("transport down")

        with patch.object(
            mgr,
            "_reconnect_server",
            new_callable=AsyncMock,
            side_effect=OSError("cannot reconnect"),
        ):
            with pytest.raises(OSError, match="cannot reconnect"):
                await mgr.call_tool("srv", "tool", {})

        # Only 1 attempt — reconnect failed on first retry
        assert session.call_tool.call_count == 1

    async def test_reconnect_succeeds_then_fails(self):
        """First reconnect works, second fails."""
        mgr = _make_manager(max_retries=3)
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("fail")

        reconnect_count = 0

        async def flaky_reconnect(name):
            nonlocal reconnect_count
            reconnect_count += 1
            if reconnect_count >= 2:
                raise OSError("reconnect died")

        with patch.object(mgr, "_reconnect_server", side_effect=flaky_reconnect):
            with pytest.raises(OSError, match="reconnect died"):
                await mgr.call_tool("srv", "tool", {})

        # 2 call_tool attempts: initial + 1 successful reconnect
        assert session.call_tool.call_count == 2


# ── Reconnect cleans up partial stack on failure ────────────────────────


class TestReconnectServerCleansUpOnFailure:
    """If `_reconnect_server()` fails after entering the new transport+session
    contexts (e.g. `session.initialize()` raises against an unreachable server),
    the partially-entered AsyncExitStack must be aclosed so the new subprocess
    and stdio streams aren't leaked across retry storms — otherwise repeated
    transient failures pile up file descriptors and zombie processes.
    """

    async def test_initialize_failure_unwinds_stack(self, monkeypatch):
        from memtomem_stm.proxy import manager as mod

        transport_exited = asyncio.Event()
        session_exited = asyncio.Event()

        class FakeTransport:
            async def __aenter__(self):
                return (MagicMock(), MagicMock())

            async def __aexit__(self, *args):
                transport_exited.set()
                return None

        class FakeSession:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                session_exited.set()
                return None

            async def initialize(self):
                raise ConnectionError("simulated init failure")

        mgr = _make_manager()
        original_session = _get_session(mgr)
        monkeypatch.setattr(mod, "ClientSession", FakeSession)
        monkeypatch.setattr(mgr, "_open_transport", lambda cfg: FakeTransport())

        with pytest.raises(ConnectionError, match="simulated init failure"):
            await mgr._reconnect_server("srv")

        assert transport_exited.is_set(), "transport context must be aclosed on init failure"
        assert session_exited.is_set(), "session context must be aclosed on init failure"
        # Connection state must not be partially mutated on failure.
        assert mgr._connections["srv"].session is original_session
        assert mgr._connections["srv"].stack is None


# ── Background task race during stop() ──────────────────────────────────


class TestBackgroundTasksStopRace:
    """When ``stop()`` is draining ``_background_tasks``, a task added to the
    set after ``asyncio.gather(...)`` has snapshotted its arguments must
    still be cancelled. In production, a request-path coroutine can
    schedule a background extraction after ``stop()`` began its drain but
    before it completes, leaving the new task orphaned and potentially
    accessing ``_extractor`` / ``_index_engine`` after they have been
    closed."""

    async def test_task_added_during_stop_gather_is_cancelled(self):
        """Deterministic variant: an existing task, when cancelled by ``stop``,
        schedules a new background task before re-raising. This mirrors the
        production race (a ``call_tool`` in flight during shutdown scheduling
        extraction) without relying on sleep timing."""
        mgr = _make_manager()
        added: list[asyncio.Task] = []

        async def self_spawning_on_cancel():
            try:
                # Park indefinitely until cancelled.
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Simulate ``call_tool`` racing with shutdown — a background
                # extraction is scheduled and added to ``_background_tasks``
                # after ``stop()`` has already snapshotted the original set.
                new = asyncio.create_task(asyncio.sleep(5))
                mgr._background_tasks.add(new)
                added.append(new)
                raise

        existing = asyncio.create_task(self_spawning_on_cancel())
        mgr._background_tasks.add(existing)
        # Let the task start and park on the wait() before stop() cancels it,
        # so cancellation triggers the except block rather than a cancel-
        # before-start shortcut.
        await asyncio.sleep(0)

        await mgr.stop()

        assert added, "self_spawning_on_cancel did not schedule follow-up task"
        assert added[0].done() or added[0].cancelled(), (
            "Task added to _background_tasks during stop()'s gather was "
            "neither cancelled nor awaited — it leaks past stop()"
        )


# ── Zero retries configuration ──────────────────────────────────────────


class TestZeroRetries:
    async def test_no_retries_raises_immediately(self):
        mgr = _make_manager(max_retries=0)
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("down")

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})

        assert session.call_tool.call_count == 1
        # Post-failure reconnect still attempted
        mock_reconnect.assert_awaited_once()

    async def test_no_retries_success(self):
        mgr = _make_manager(max_retries=0)
        session = _get_session(mgr)
        session.call_tool.return_value = _make_result("works")

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            result = await mgr.call_tool("srv", "tool", {})

        assert result == "works"
        mock_reconnect.assert_not_awaited()


# ── Unknown server ───────────────────────────────────────────────────────


class TestUnknownServer:
    async def test_unknown_server_raises_key_error(self):
        mgr = _make_manager()
        with pytest.raises(KeyError, match="Unknown upstream server"):
            await mgr.call_tool("nonexistent", "tool", {})


# ── Error result from upstream (isError=True) ────────────────────────────


class TestErrorResult:
    async def test_error_result_raises_tool_error(self):
        """When upstream returns isError=True, ToolError is raised to propagate the error flag."""
        from mcp.server.fastmcp.exceptions import ToolError

        mgr = _make_manager(compression=CompressionStrategy.TRUNCATE, max_result_chars=10)
        session = _get_session(mgr)
        long_error = "Error: " + "x" * 500
        session.call_tool.return_value = _make_result(long_error, is_error=True)

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ToolError, match="Error:"):
                await mgr.call_tool("srv", "tool", {})


# ── Empty/non-text response ──────────────────────────────────────────────


class TestEdgeResponses:
    async def test_empty_response(self):
        mgr = _make_manager()
        session = _get_session(mgr)
        session.call_tool.return_value = SimpleNamespace(content=[], isError=False)

        result = await mgr.call_tool("srv", "tool", {})
        assert result == "[empty response]"

    async def test_none_content_degrades_to_empty(self):
        """Spec-noncompliant upstream returning ``content=None`` must not crash.

        The MCP spec requires ``content`` to be a list, but resilient proxies
        should degrade rather than raise ``TypeError`` from ``for c in None``.
        """
        mgr = _make_manager()
        session = _get_session(mgr)
        session.call_tool.return_value = SimpleNamespace(content=None, isError=False)

        result = await mgr.call_tool("srv", "tool", {})
        assert result == "[empty response]"

    async def test_text_field_none_degrades(self):
        """Spec-noncompliant upstream returning ``TextContent.text=None`` must not crash.

        Mirrors ``test_none_content_degrades_to_empty`` one level down: the MCP
        spec requires ``TextContent.text`` to be ``str``, but the same upstream
        servers that produce ``content=None`` also occasionally produce a
        TextContent whose ``text`` field is ``None``. Without the ``or ""``
        guard, ``len(text)`` raises ``TypeError`` and the failure propagates
        before the metrics row is recorded — the same failure mode #114 fixed
        for ``content`` itself.
        """
        mgr = _make_manager()
        session = _get_session(mgr)
        none_text = SimpleNamespace(type="text", text=None)
        session.call_tool.return_value = SimpleNamespace(content=[none_text], isError=False)

        # Should not raise; concrete return value is implementation-defined
        # (the empty text passes through compression as an empty payload).
        result = await mgr.call_tool("srv", "tool", {})
        assert isinstance(result, str)

    async def test_non_text_content_passthrough(self):
        mgr = _make_manager()
        session = _get_session(mgr)
        img = SimpleNamespace(type="image", data="base64data")
        session.call_tool.return_value = SimpleNamespace(content=[img], isError=False)

        result = await mgr.call_tool("srv", "tool", {})
        assert isinstance(result, list)
        assert result[0].type == "image"

    async def test_mixed_text_and_non_text(self):
        mgr = _make_manager()
        session = _get_session(mgr)
        text = _text_content("hello world")
        img = SimpleNamespace(type="image", data="png")
        session.call_tool.return_value = SimpleNamespace(content=[text, img], isError=False)

        result = await mgr.call_tool("srv", "tool", {})
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0].type == "text"
        assert result[1].type == "image"


# ── max_upstream_chars hard cap ──────────────────────────────────────────


class TestMaxUpstreamChars:
    """Guards against OOM from upstreams returning huge payloads (#108)."""

    async def test_oversized_response_truncated_with_notice(self):
        mgr = _make_manager(compression=CompressionStrategy.NONE)
        # Shrink the cap for fast testing; default is 10 M chars.
        mgr._config_loader._cached.max_upstream_chars = 100  # type: ignore[union-attr]

        session = _get_session(mgr)
        big = "a" * 500
        session.call_tool.return_value = _make_result(big)

        result = await mgr.call_tool("srv", "tool", {})

        assert isinstance(result, str)
        assert "max_upstream_chars guard" in result
        # Truncation was hard — text body cut to the cap (100), then the notice.
        body = result.split("\n\n[response truncated")[0]
        assert body == "a" * 100

    async def test_under_cap_passes_through_unchanged(self):
        mgr = _make_manager(compression=CompressionStrategy.NONE)
        mgr._config_loader._cached.max_upstream_chars = 1000  # type: ignore[union-attr]

        session = _get_session(mgr)
        session.call_tool.return_value = _make_result("hello world")

        result = await mgr.call_tool("srv", "tool", {})
        assert result == "hello world"
        assert "max_upstream_chars guard" not in result

    async def test_cap_applies_across_multiple_text_blocks(self):
        """The cap aggregates across blocks; not per-block."""
        mgr = _make_manager(compression=CompressionStrategy.NONE)
        mgr._config_loader._cached.max_upstream_chars = 50  # type: ignore[union-attr]

        session = _get_session(mgr)
        # Two 30-char blocks → 60 chars total > 50 cap
        block_a = _text_content("a" * 30)
        block_b = _text_content("b" * 30)
        session.call_tool.return_value = SimpleNamespace(content=[block_a, block_b], isError=False)

        result = await mgr.call_tool("srv", "tool", {})

        assert "max_upstream_chars guard" in result
        body = result.split("\n\n[response truncated")[0]
        # First block fully kept (30 chars), second cut to remaining 20 chars.
        # Joined with "\n" between text_parts.
        assert body == "a" * 30 + "\n" + "b" * 20


# ── Surfacing failure: graceful degradation ──────────────────────────────


class TestSurfacingFailure:
    async def test_surfacing_exception_returns_compressed(self):
        """If surfacing engine raises, compressed text is returned unchanged."""
        mgr = _make_manager()
        session = _get_session(mgr)
        session.call_tool.return_value = _make_result("hello world")

        engine = AsyncMock()
        engine.surface.side_effect = RuntimeError("LTM down")
        mgr._surfacing_engine = engine

        result = await mgr.call_tool("srv", "tool", {})
        # Should get the text back, not an exception
        assert "hello world" in result


# ── Pipeline-stage exceptions: must surface in proxy_metrics as INTERNAL_ERROR ─


class TestPipelineExceptionMetrics:
    async def test_compress_failure_records_internal_error(self):
        """If a COMPRESS-stage exception escapes _call_tool_inner, the outer
        wrapper must record an INTERNAL_ERROR metrics row before re-raising
        — otherwise operators are blind to in-pipeline failures."""
        from memtomem_stm.proxy.metrics import ErrorCategory

        mgr = _make_manager()
        session = _get_session(mgr)
        session.call_tool.return_value = _make_result("hello world")

        # Force the compression stage to raise after upstream succeeds
        with patch.object(
            mgr, "_apply_compression", new_callable=AsyncMock, side_effect=RuntimeError("boom")
        ):
            with pytest.raises(RuntimeError, match="boom"):
                await mgr.call_tool("srv", "tool", {})

        # An INTERNAL_ERROR row must be present
        assert mgr.tracker._errors_by_category[ErrorCategory.INTERNAL_ERROR.value] == 1
        # And no double-count from typed paths
        for cat in (
            ErrorCategory.TRANSPORT,
            ErrorCategory.TIMEOUT,
            ErrorCategory.PROTOCOL,
            ErrorCategory.UPSTREAM_ERROR,
            ErrorCategory.PROGRAMMING,
        ):
            assert mgr.tracker._errors_by_category[cat.value] == 0

    async def test_typed_upstream_error_not_double_recorded(self):
        """A transport error already records its own row; the outer wrapper
        must not add a second INTERNAL_ERROR row."""
        from memtomem_stm.proxy.metrics import ErrorCategory

        mgr = _make_manager(max_retries=0)
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("down")

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})

        # Exactly one row, classified TRANSPORT — not INTERNAL_ERROR
        assert mgr.tracker._errors_by_category[ErrorCategory.TRANSPORT.value] == 1
        assert mgr.tracker._errors_by_category[ErrorCategory.INTERNAL_ERROR.value] == 0

    async def test_upstream_iserror_not_double_recorded(self):
        """A result.isError=True path raises ToolError after recording an
        UPSTREAM_ERROR row; the outer wrapper must not add INTERNAL_ERROR."""
        from mcp.server.fastmcp.exceptions import ToolError

        from memtomem_stm.proxy.metrics import ErrorCategory

        mgr = _make_manager()
        session = _get_session(mgr)
        session.call_tool.return_value = _make_result("oops", is_error=True)

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ToolError):
                await mgr.call_tool("srv", "tool", {})

        assert mgr.tracker._errors_by_category[ErrorCategory.UPSTREAM_ERROR.value] == 1
        assert mgr.tracker._errors_by_category[ErrorCategory.INTERNAL_ERROR.value] == 0


# ── Context query stripping ──────────────────────────────────────────────


class TestContextQueryStripping:
    async def test_context_query_stripped_from_upstream_args(self):
        mgr = _make_manager()
        session = _get_session(mgr)
        session.call_tool.return_value = _make_result("result")

        await mgr.call_tool("srv", "tool", {"q": "test", "_context_query": "search query"})

        # _context_query should NOT be forwarded to upstream
        call_args = session.call_tool.call_args
        forwarded_args = call_args[0][1]  # second positional arg
        assert "_context_query" not in forwarded_args
        assert forwarded_args["q"] == "test"
        # _trace_id is expected (trace context propagation)
        assert "_trace_id" in forwarded_args


# ── Cache interaction with errors ────────────────────────────────────────


class TestCacheWithErrors:
    async def test_cache_miss_then_transport_error(self):
        """Cache miss followed by transport error still raises."""
        mgr = _make_manager(max_retries=0)
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("down")

        cache = MagicMock()
        cache.get.return_value = None  # cache miss
        mgr._cache = cache

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})

        # ``cache.get`` is called twice: once on the stampede guard's
        # lock-free fast-path, once on the post-lock double-check.
        assert cache.get.call_count == 2
        mgr.tracker.get_summary()  # should record cache miss
        assert mgr.tracker.get_summary()["cache_misses"] == 1

    async def test_cache_hit_bypasses_upstream(self):
        """Cache hit skips upstream call entirely."""
        mgr = _make_manager()
        session = _get_session(mgr)
        session.call_tool.side_effect = AssertionError("should not be called")

        cache = MagicMock()
        cache.get.return_value = "cached result"
        mgr._cache = cache

        result = await mgr.call_tool("srv", "tool", {})
        assert result == "cached result"
        session.call_tool.assert_not_called()
        assert mgr.tracker.get_summary()["cache_hits"] == 1

    async def test_cache_roundtrip_is_reachable_with_trace_id_propagation(self, tmp_path):
        """End-to-end cache round-trip: a real ``ProxyCache`` backed by
        SQLite must observe a hit on a second call with identical args,
        *even while ``_trace_id`` is being injected into the upstream args
        for observability*. The bug this regression protects against —
        ``upstream_args["_trace_id"] = trace_id`` mutating the same dict
        that later feeds ``cache.set`` — makes every stored entry keyed
        on a per-request random hex, so no future lookup can ever match
        (cache hit rate structurally 0%).

        Uses a real ``ProxyCache``, not ``MagicMock``, because mock-backed
        tests don't enforce key equality between ``get`` and ``set`` and
        so can't detect the mutation bug."""
        from memtomem_stm.proxy.cache import ProxyCache

        mgr = _make_manager(tmp_path=tmp_path)
        session = _get_session(mgr)
        session.call_tool.side_effect = [_make_result("upstream payload")]

        cache = ProxyCache(tmp_path / "cache.db", max_entries=10)
        cache.initialize()
        mgr._cache = cache
        try:
            # First call: miss → upstream → set.
            r1 = await mgr.call_tool("srv", "tool", {"x": 1})
            assert "upstream payload" in r1
            assert session.call_tool.call_count == 1

            # Second call with the SAME args: must hit cache (no upstream).
            r2 = await mgr.call_tool("srv", "tool", {"x": 1})
            assert "upstream payload" in r2
            assert session.call_tool.call_count == 1, (
                "Cache round-trip broken: second identical call went to "
                f"upstream ({session.call_tool.call_count} calls). "
                "cache.set key likely diverged from cache.get key "
                "(e.g. trace_id injection mutating the shared args dict)."
            )
        finally:
            cache.close()

    async def test_concurrent_identical_requests_stampede_on_miss(self):
        """Two concurrent ``call_tool`` invocations with identical
        ``(server, tool, args)`` and a cold cache should result in a
        **single** upstream ``session.call_tool`` — otherwise every
        duplicate request pays the full upstream cost (LLM tokens,
        rate-limit budget, latency). The ``cache.get`` fast-path and the
        eventual ``cache.set`` inside ``_call_tool_inner`` are separated
        by the ``await session.call_tool`` plus the compression /
        extraction pipeline, so without a per-key lock both coroutines
        see a miss before either writes back."""
        mgr = _make_manager()
        session = _get_session(mgr)

        call_count = 0

        async def slow_upstream(tool, args):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)  # keep the first call in flight
            return _make_result(f"result-{call_count}")

        session.call_tool.side_effect = slow_upstream

        cache_store: dict[str, str] = {}

        cache = MagicMock()
        cache.get.side_effect = lambda srv, tl, a: cache_store.get(f"{srv}|{tl}|{a}")

        def _set(srv, tl, a, result, ttl_seconds=None):
            cache_store[f"{srv}|{tl}|{a}"] = result

        cache.set.side_effect = _set
        mgr._cache = cache

        await asyncio.gather(
            mgr.call_tool("srv", "tool", {"x": 1}),
            mgr.call_tool("srv", "tool", {"x": 1}),
        )

        assert session.call_tool.call_count == 1, (
            "Cache stampede: identical concurrent requests both hit upstream "
            f"({session.call_tool.call_count} upstream calls for the same key)"
        )

    async def test_cache_set_failure_does_not_break_response(self):
        """A failing ``cache.set`` (SQLite lock timeout, disk full, etc.)
        must not discard a successful upstream response. Cache writes are
        an optional fast-path, not a correctness dependency: a swallowed
        warning is the expected behaviour."""
        mgr = _make_manager()
        session = _get_session(mgr)
        session.call_tool.return_value = _make_result("hello world")

        cache = MagicMock()
        cache.get.return_value = None  # miss → upstream is consulted
        cache.set.side_effect = RuntimeError("simulated SQLite lock timeout")
        mgr._cache = cache

        # Must not raise — response returns normally.
        result = await mgr.call_tool("srv", "tool", {})
        assert "hello world" in result
        cache.set.assert_called_once()
