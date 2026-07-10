"""Tests for ProxyManager error paths — transport failure, protocol error, reconnect, timeout."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from memtomem_stm.proxy.config import (
    CacheConfig,
    CompressionStrategy,
    ProgressiveConfig,
    ProxyConfig,
    ToolOverrideConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.utils.circuit_breaker import CircuitBreaker


# ── Helpers ──────────────────────────────────────────────────────────────


def _text_content(text: str):
    """Create a mock MCP TextContent."""
    return SimpleNamespace(type="text", text=text)


def _make_result(text: str, is_error: bool = False):
    """Create a mock call_tool result."""
    return SimpleNamespace(content=[_text_content(text)], isError=is_error)


def _ann(*, read_only=None, destructive=None):
    """A stand-in for MCP ``ToolAnnotations`` (``getattr``-based consumers, so a
    SimpleNamespace matches the real model). Mirrors test_cache_eligibility."""
    return SimpleNamespace(readOnlyHint=read_only, destructiveHint=destructive)


def _tool(name, ann=None):
    return SimpleNamespace(name=name, annotations=ann)


def _read_only_tools(name: str = "tool"):
    """Connection tool snapshot declaring ``name`` read-only, so the #578
    timeout-replay guard keeps retrying it — for tests that exercise the retry
    MECHANICS (backoff, deadlines, reconnect ordering) rather than the guard."""
    return [_tool(name, _ann(read_only=True, destructive=False))]


def _make_manager(
    max_retries: int = 3,
    reconnect_delay: float = 0.0,
    max_reconnect_delay: float = 0.0,
    compression: CompressionStrategy = CompressionStrategy.NONE,
    max_result_chars: int = 50000,
    tmp_path: Path | None = None,
    call_timeout: float = 90.0,
    overall_deadline: float = 180.0,
    progressive: ProgressiveConfig | None = None,
    tools: list | None = None,
    tool_overrides: dict[str, ToolOverrideConfig] | None = None,
    annotation_policy: str = "conservative",
    circuit_max_failures: int = 0,
    circuit_reset_seconds: float = 60.0,
) -> ProxyManager:
    """Create a ProxyManager with a mocked upstream connection.

    ``circuit_max_failures`` defaults to 0 (breaker disabled) so the
    pre-#608 error-path tests keep exercising the raw retry/deadline
    mechanics without breaker fast-fails intervening.
    """
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=compression,
        max_result_chars=max_result_chars,
        max_retries=max_retries,
        reconnect_delay_seconds=reconnect_delay,
        max_reconnect_delay_seconds=max_reconnect_delay,
        call_timeout_seconds=call_timeout,
        overall_deadline_seconds=overall_deadline,
        progressive=progressive,
        tool_overrides=tool_overrides or {},
        circuit_max_failures=circuit_max_failures,
        circuit_reset_seconds=circuit_reset_seconds,
    )
    config_path = (tmp_path / "proxy.json") if tmp_path else Path("/tmp/proxy.json")
    proxy_cfg = ProxyConfig(
        config_path=config_path,
        upstream_servers={"srv": server_cfg},
        cache=CacheConfig(tool_annotation_policy=annotation_policy),
    )
    tracker = TokenTracker()
    mgr = ProxyManager(proxy_cfg, tracker)

    # Inject a mocked connection (breaker built exactly as _connect_server does)
    session = AsyncMock()
    conn = UpstreamConnection(
        name="srv",
        config=server_cfg,
        session=session,
        tools=tools if tools is not None else [],
        breaker=(
            CircuitBreaker(
                max_failures=circuit_max_failures,
                reset_timeout=circuit_reset_seconds,
                name="upstream-srv",
            )
            if circuit_max_failures > 0
            else None
        ),
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
        # Read-only annotations so the TimeoutError case stays retryable under
        # the #578 replay guard (the guard itself is covered by
        # TestTimeoutReplayGuard).
        mgr = _make_manager(max_retries=3, tools=_read_only_tools())
        session = _get_session(mgr)
        session.call_tool.side_effect = [exc_type("fail"), _make_result("ok")]

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            result = await mgr.call_tool("srv", "tool", {})

        assert result == "ok"
        assert session.call_tool.call_count == 2
        mock_reconnect.assert_awaited_once_with("srv", ANY)

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

        async def track_reconnect(name, cfg=None):
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
        mock_reconnect.assert_awaited_once_with("srv", ANY)

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

        async def flaky_reconnect(name, cfg=None):
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

        def _fake_key(srv, tl, a, kw):
            # Mirror the real key inputs: context_query + config_fingerprint
            # are part of the key, so the fake store must not collapse them.
            return f"{srv}|{tl}|{a}|{kw.get('context_query')}|{kw.get('config_fingerprint')}"

        cache.get.side_effect = lambda srv, tl, a, **kw: cache_store.get(_fake_key(srv, tl, a, kw))

        def _set(srv, tl, a, result, ttl_seconds=None, **kw):
            cache_store[_fake_key(srv, tl, a, kw)] = result

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


# ── P0: call_timeout_seconds + overall_deadline_seconds ──────────────────


class TestCallTimeout:
    """Per-attempt ``call_timeout_seconds`` caps each ``session.call_tool``
    await; ``overall_deadline_seconds`` caps the sum across retries."""

    async def test_hanging_upstream_is_timed_out_and_retried(self):
        """A silent upstream must be cut off by ``call_timeout_seconds``; the
        retry path then calls ``_reconnect_server`` (drops the orphan
        ``request_id`` in the old session) and a subsequent attempt succeeds.
        """
        mgr = _make_manager(
            max_retries=1,
            reconnect_delay=0.0,
            max_reconnect_delay=0.0,
            call_timeout=0.05,
            overall_deadline=1.0,
            tools=_read_only_tools(),
        )
        session = _get_session(mgr)

        attempts = 0

        async def hang_once_then_ok(*_a, **_kw):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await asyncio.sleep(10)  # wait_for cancels this
            return _make_result("fresh")

        session.call_tool.side_effect = hang_once_then_ok

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            result = await mgr.call_tool("srv", "tool", {})

        assert "fresh" in result
        assert attempts == 2
        mock_reconnect.assert_awaited_with("srv", ANY)

    async def test_hanging_upstream_without_retries_raises_timeout(self):
        """With ``max_retries=0`` a hanging upstream must surface
        ``TimeoutError`` instead of hanging forever."""
        mgr = _make_manager(
            max_retries=0,
            call_timeout=0.05,
            overall_deadline=0.2,
        )
        session = _get_session(mgr)

        async def hang_forever(*_a, **_kw):
            await asyncio.sleep(10)

        session.call_tool.side_effect = hang_forever

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            with pytest.raises(asyncio.TimeoutError):
                await mgr.call_tool("srv", "tool", {})

        # Terminal retry path still reconnects before re-raising so the next
        # call (if any) starts on a fresh session.
        mock_reconnect.assert_awaited_with("srv", ANY)

    async def test_reconnect_between_attempts_drops_stale_session(self):
        """After a call times out, the next attempt must run on the freshly
        reconnected session, not on the one whose request was cancelled.
        This is the orphan-``request_id`` guard — without the reconnect, a
        late response from attempt N could be read by attempt N+1."""
        mgr = _make_manager(
            max_retries=1,
            reconnect_delay=0.0,
            max_reconnect_delay=0.0,
            call_timeout=0.05,
            overall_deadline=1.0,
            tools=_read_only_tools(),
        )
        session = _get_session(mgr)

        call_log: list[str] = []

        async def hang_once_then_ok(*_a, **_kw):
            call_log.append("invoked")
            if len(call_log) == 1:
                await asyncio.sleep(10)
            return _make_result("fresh")

        session.call_tool.side_effect = hang_once_then_ok

        reconnect_timing: list[int] = []

        async def tracking_reconnect(_name, _cfg=None):
            # Record how many call_tool invocations had happened by the time
            # reconnect fires — must be exactly 1 (post-timeout, pre-retry).
            reconnect_timing.append(len(call_log))

        with patch.object(mgr, "_reconnect_server", side_effect=tracking_reconnect):
            result = await mgr.call_tool("srv", "tool", {})

        assert "fresh" in result
        assert reconnect_timing == [1], (
            "reconnect must fire between the cancelled attempt and the retry "
            f"(saw {reconnect_timing})"
        )

    async def test_overall_deadline_aborts_before_max_retries(self):
        """When the overall deadline is shorter than
        ``call_timeout × (max_retries+1)``, the loop must abort early with a
        ``TimeoutError`` referencing ``overall_deadline_seconds`` — not burn
        through all retries."""
        mgr = _make_manager(
            max_retries=10,
            reconnect_delay=0.0,
            max_reconnect_delay=0.0,
            call_timeout=0.02,
            overall_deadline=0.05,
            tools=_read_only_tools(),
        )
        session = _get_session(mgr)

        async def always_hang(*_a, **_kw):
            await asyncio.sleep(10)

        session.call_tool.side_effect = always_hang

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(asyncio.TimeoutError, match="overall_deadline"):
                await mgr.call_tool("srv", "tool", {})

        # Upper bound: max_retries+1 = 11. Overall_deadline=0.05 with
        # call_timeout=0.02 permits ~2-3 attempts; well under 11.
        assert session.call_tool.call_count < 6, (
            "overall_deadline did not cap the retry loop: "
            f"saw {session.call_tool.call_count} attempts"
        )

    async def test_per_attempt_shrinks_to_remaining_deadline(self):
        """Once prior attempts have consumed most of the deadline, the next
        attempt's effective timeout must equal the remaining budget (not the
        full ``call_timeout_seconds``) so the total wall-clock stays within
        ``overall_deadline_seconds``."""
        mgr = _make_manager(
            max_retries=3,
            reconnect_delay=0.0,
            max_reconnect_delay=0.0,
            call_timeout=0.1,
            overall_deadline=0.12,
            tools=_read_only_tools(),
        )
        session = _get_session(mgr)

        captured_timeouts: list[float] = []
        real_wait_for = asyncio.wait_for

        async def spy_wait_for(coro, timeout):
            captured_timeouts.append(timeout)
            return await real_wait_for(coro, timeout)

        async def hang_forever(*_a, **_kw):
            await asyncio.sleep(10)

        session.call_tool.side_effect = hang_forever

        with (
            patch.object(mgr, "_reconnect_server", new_callable=AsyncMock),
            patch(
                "memtomem_stm.proxy.manager.asyncio.wait_for",
                side_effect=spy_wait_for,
            ),
        ):
            with pytest.raises(asyncio.TimeoutError):
                await mgr.call_tool("srv", "tool", {})

        assert len(captured_timeouts) >= 2, (
            f"expected at least two attempts within 0.12s budget, saw {len(captured_timeouts)}"
        )
        # First attempt uses full call_timeout_seconds.
        assert captured_timeouts[0] == pytest.approx(0.1, abs=0.005)
        # Second attempt must shrink to the remaining deadline (≈0.02s).
        assert captured_timeouts[1] < 0.1, (
            f"second attempt used {captured_timeouts[1]:.4f}s but remaining "
            "deadline was smaller; shrink logic not applied"
        )


class TestTimeoutReplayGuard:
    """#578: a per-attempt timeout cancels our wait, not the upstream execution,
    so a non-read-only tool must NOT be re-invoked (that would replay its side
    effect). The connection is still refreshed for the next call, and the
    original ``TimeoutError`` propagates. Connection-level failures stay
    retryable for every tool."""

    async def test_writer_timeout_not_retried(self):
        """A tool annotated ``readOnlyHint=False`` that times out is invoked
        exactly once, reconnected for the next call, and raises."""
        mgr = _make_manager(
            max_retries=3,
            call_timeout=0.05,
            overall_deadline=1.0,
            tools=[_tool("tool", _ann(read_only=False))],
        )
        session = _get_session(mgr)

        async def hang(*_a, **_kw):
            await asyncio.sleep(10)

        session.call_tool.side_effect = hang

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            with pytest.raises(asyncio.TimeoutError):
                await mgr.call_tool("srv", "tool", {})

        assert session.call_tool.call_count == 1, "writer tool must not be re-invoked on timeout"
        mock_reconnect.assert_awaited_once_with("srv", ANY)

    async def test_unannotated_timeout_not_retried(self):
        """The core case: a tool with NO annotations (may-mutate per spec) is
        treated as a writer under the default ``conservative`` policy."""
        mgr = _make_manager(
            max_retries=3,
            call_timeout=0.05,
            overall_deadline=1.0,
            tools=[],  # no annotations at all
        )
        session = _get_session(mgr)

        async def hang(*_a, **_kw):
            await asyncio.sleep(10)

        session.call_tool.side_effect = hang

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            with pytest.raises(asyncio.TimeoutError):
                await mgr.call_tool("srv", "tool", {})

        assert session.call_tool.call_count == 1
        mock_reconnect.assert_awaited_once_with("srv", ANY)

    async def test_readonly_timeout_is_retried(self):
        """A declared read-only tool is replay-safe: timeout still retries."""
        mgr = _make_manager(
            max_retries=1,
            call_timeout=0.05,
            overall_deadline=1.0,
            tools=_read_only_tools(),
        )
        session = _get_session(mgr)

        attempts = 0

        async def hang_once_then_ok(*_a, **_kw):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await asyncio.sleep(10)
            return _make_result("ok")

        session.call_tool.side_effect = hang_once_then_ok

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            result = await mgr.call_tool("srv", "tool", {})

        assert result == "ok"
        assert attempts == 2

    async def test_writer_connection_refused_is_retried(self):
        """A non-timeout connection failure (refused) provably never executed,
        so it stays retryable even for a writer tool."""
        mgr = _make_manager(
            max_retries=3,
            tools=[_tool("tool", _ann(read_only=False))],
        )
        session = _get_session(mgr)
        session.call_tool.side_effect = [ConnectionRefusedError("refused"), _make_result("ok")]

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as mock_reconnect:
            result = await mgr.call_tool("srv", "tool", {})

        assert result == "ok"
        assert session.call_tool.call_count == 2
        mock_reconnect.assert_awaited_once_with("srv", ANY)

    async def test_ignore_policy_keeps_replay(self):
        """``tool_annotation_policy=ignore`` opts out of annotation trust, so a
        writer timeout retries as it did pre-#578."""
        mgr = _make_manager(
            max_retries=1,
            call_timeout=0.05,
            overall_deadline=1.0,
            tools=[_tool("tool", _ann(read_only=False))],
            annotation_policy="ignore",
        )
        session = _get_session(mgr)

        attempts = 0

        async def hang_once_then_ok(*_a, **_kw):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await asyncio.sleep(10)
            return _make_result("ok")

        session.call_tool.side_effect = hang_once_then_ok

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            result = await mgr.call_tool("srv", "tool", {})

        assert result == "ok"
        assert attempts == 2

    async def test_per_tool_cache_true_override_keeps_replay(self):
        """An explicit per-tool ``cache: true`` is the operator's 'effectively
        read-only' assertion, so timeout-retry is preserved for an otherwise
        unannotated tool."""
        mgr = _make_manager(
            max_retries=1,
            call_timeout=0.05,
            overall_deadline=1.0,
            tools=[],
            tool_overrides={"tool": ToolOverrideConfig(cache=True)},
        )
        session = _get_session(mgr)

        attempts = 0

        async def hang_once_then_ok(*_a, **_kw):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await asyncio.sleep(10)
            return _make_result("ok")

        session.call_tool.side_effect = hang_once_then_ok

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            result = await mgr.call_tool("srv", "tool", {})

        assert result == "ok"
        assert attempts == 2

    async def test_contradictory_annotation_treated_as_writer(self):
        """``readOnlyHint=True`` AND ``destructiveHint=True`` is a mis-annotation;
        the guard treats it as a writer (unknown → don't replay)."""
        mgr = _make_manager(
            max_retries=3,
            call_timeout=0.05,
            overall_deadline=1.0,
            tools=[_tool("tool", _ann(read_only=True, destructive=True))],
        )
        session = _get_session(mgr)

        async def hang(*_a, **_kw):
            await asyncio.sleep(10)

        session.call_tool.side_effect = hang

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(asyncio.TimeoutError):
                await mgr.call_tool("srv", "tool", {})

        assert session.call_tool.call_count == 1

    async def test_gated_path_records_exactly_one_timeout_metric(self):
        """The replay-gated terminal path records exactly one TIMEOUT error, not
        one per would-be attempt, and the outer call_tool does not double-record."""
        mgr = _make_manager(
            max_retries=3,
            call_timeout=0.05,
            overall_deadline=1.0,
            tools=[_tool("tool", _ann(read_only=False))],
        )
        session = _get_session(mgr)

        async def hang(*_a, **_kw):
            await asyncio.sleep(10)

        session.call_tool.side_effect = hang

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(asyncio.TimeoutError):
                await mgr.call_tool("srv", "tool", {})

        summary = mgr.tracker.get_summary()
        assert summary["total_errors"] == 1
        assert summary["errors_by_category"].get("timeout") == 1


class TestTransientKeyResponsesNotCached:
    """Responses embedding a TRANSIENT pending-store retrieval key (progressive
    first-chunk, SELECTIVE/HYBRID TOC) must NOT be cached: the key dies on
    process restart / pending-store eviction / its shorter TTL, so a cache hit
    would hand back a dead ``stm_proxy_read_more`` / ``stm_proxy_select_chunks``
    key with the response tail unrecoverable. Skipping the store makes the next
    identical call re-run the pipeline and mint a fresh, live key."""

    # Enough markdown sections that SELECTIVE emits a chunk TOC (with a
    # transient ``selection_key``) rather than falling back to truncation.
    _TOC_TEXT = "# Doc\n\n" + "\n\n".join(
        f"## Section {i} heading text\n" + ("alpha beta gamma delta epsilon zeta " * 12)
        for i in range(12)
    )

    async def test_selective_toc_response_is_not_cached(self, tmp_path):
        from memtomem_stm.proxy.cache import ProxyCache

        mgr = _make_manager(
            compression=CompressionStrategy.SELECTIVE,
            max_result_chars=400,
            tmp_path=tmp_path,
        )
        session = _get_session(mgr)
        session.call_tool.side_effect = [
            _make_result(self._TOC_TEXT),
            _make_result(self._TOC_TEXT),
        ]
        cache = ProxyCache(tmp_path / "cache.db", max_entries=10)
        cache.initialize()
        mgr._cache = cache
        try:
            r1 = await mgr.call_tool("srv", "tool", {"x": 1})
            assert '"selection_key"' in r1, (
                "precondition: SELECTIVE must emit a TOC carrying a transient key"
            )
            # Identical second call must re-run upstream — the TOC was NOT cached.
            r2 = await mgr.call_tool("srv", "tool", {"x": 1})
            assert '"selection_key"' in r2
            assert session.call_tool.call_count == 2, (
                "transient-key TOC must not be cached; the second identical call "
                f"should re-run upstream (saw {session.call_tool.call_count})"
            )
            assert cache.stats()["total_entries"] == 0  # nothing stored under ANY key
        finally:
            cache.close()

    async def test_plain_response_is_still_cached(self, tmp_path):
        """Over-suppression guard: a key-free response must still be cached."""
        from memtomem_stm.proxy.cache import ProxyCache

        mgr = _make_manager(tmp_path=tmp_path)  # compression NONE
        session = _get_session(mgr)
        session.call_tool.side_effect = [_make_result("plain upstream payload")]
        cache = ProxyCache(tmp_path / "cache.db", max_entries=10)
        cache.initialize()
        mgr._cache = cache
        try:
            r1 = await mgr.call_tool("srv", "tool", {"x": 1})
            assert "plain upstream payload" in r1
            r2 = await mgr.call_tool("srv", "tool", {"x": 1})
            assert "plain upstream payload" in r2
            assert session.call_tool.call_count == 1, (
                "non-transient response should be cached; second call must hit cache"
            )
        finally:
            cache.close()

    _READ_MORE_RE = re.compile(r'stm_proxy_read_more\(key="([0-9a-f]+)", offset=(\d+)\)')

    async def test_progressive_first_chunk_is_not_cached(self, tmp_path):
        """Same invariant on the PROGRESSIVE-strategy branch, end to end.

        Until the ``metrics_strategy`` fix this branch could not be driven
        through ``call_tool`` at all (UnboundLocalError at the metrics
        record), so only the detector unit tests covered the progressive
        marker — not the store guard on a real PROGRESSIVE call."""
        from memtomem_stm.proxy.cache import ProxyCache

        mgr = _make_manager(
            compression=CompressionStrategy.PROGRESSIVE,
            progressive=ProgressiveConfig(chunk_size=500),
            tmp_path=tmp_path,
        )
        session = _get_session(mgr)
        large = "paragraph sentence here. " * 200  # 5000 chars > chunk_size
        session.call_tool.side_effect = [_make_result(large), _make_result(large)]
        cache = ProxyCache(tmp_path / "cache.db", max_entries=10)
        cache.initialize()
        mgr._cache = cache
        try:
            r1 = await mgr.call_tool("srv", "tool", {"x": 1})
            assert "stm_proxy_read_more" in r1
            r2 = await mgr.call_tool("srv", "tool", {"x": 1})
            assert "stm_proxy_read_more" in r2
            assert session.call_tool.call_count == 2, (
                "progressive first-chunk must not be cached; the second identical "
                f"call should re-run upstream (saw {session.call_tool.call_count})"
            )
            assert cache.stats()["total_entries"] == 0  # nothing stored under ANY key
        finally:
            cache.close()

    async def test_progressive_repeat_call_mints_live_key_after_store_eviction(self, tmp_path):
        """The original data-loss shape: progressive-store entry evicted
        between two identical calls. The second call must mint a fresh key
        whose tail is readable — not re-serve a chunk pointing at the dead
        one."""
        from memtomem_stm.proxy.cache import ProxyCache

        mgr = _make_manager(
            compression=CompressionStrategy.PROGRESSIVE,
            progressive=ProgressiveConfig(chunk_size=500),
            tmp_path=tmp_path,
        )
        session = _get_session(mgr)
        large = "alpha bravo charlie delta. " * 200
        session.call_tool.side_effect = [_make_result(large), _make_result(large)]
        cache = ProxyCache(tmp_path / "cache.db", max_entries=10)
        cache.initialize()
        mgr._cache = cache
        try:
            first = await mgr.call_tool("srv", "tool", {"x": 1})
            m1 = self._READ_MORE_RE.search(first)
            assert m1 is not None
            key1 = m1.group(1)

            # Simulate TTL / max_stored eviction between the two calls.
            mgr._get_progressive_store().delete(key1)

            second = await mgr.call_tool("srv", "tool", {"x": 1})
            m2 = self._READ_MORE_RE.search(second)
            assert m2 is not None, "second call must be re-chunked, not served from cache"
            key2, offset2 = m2.group(1), int(m2.group(2))
            assert key2 != key1

            follow_up = mgr.read_more(key2, offset2)
            assert "not found or expired" not in follow_up
            assert follow_up.strip(), "tail content must be reachable via the new key"
        finally:
            cache.close()

    async def test_progressive_passthrough_is_still_cached(self, tmp_path):
        """Content that fits one chunk passes through without a read_more
        key — the exclusion must stay narrow and keep caching it."""
        from memtomem_stm.proxy.cache import ProxyCache

        mgr = _make_manager(
            compression=CompressionStrategy.PROGRESSIVE,
            progressive=ProgressiveConfig(chunk_size=4000),
            tmp_path=tmp_path,
        )
        session = _get_session(mgr)
        session.call_tool.side_effect = [_make_result("short response body")]
        cache = ProxyCache(tmp_path / "cache.db", max_entries=10)
        cache.initialize()
        mgr._cache = cache
        try:
            r1 = await mgr.call_tool("srv", "tool", {"x": 1})
            assert "stm_proxy_read_more" not in r1
            r2 = await mgr.call_tool("srv", "tool", {"x": 1})
            assert "short response body" in r2
            assert session.call_tool.call_count == 1, (
                "single-chunk passthrough carries no key and should be cached"
            )
        finally:
            cache.close()


# ── Per-upstream circuit breaker (#608) ──────────────────────────────────


class TestUpstreamCircuitBreaker:
    """Terminal transport/timeout failures open the per-upstream breaker;
    an open breaker fast-fails without touching the upstream."""

    async def test_opens_after_terminal_failures_and_fast_fails(self):
        from mcp.server.fastmcp.exceptions import ToolError

        mgr = _make_manager(max_retries=0, circuit_max_failures=2)
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("down")

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            for _ in range(2):
                with pytest.raises(ConnectionError):
                    await mgr.call_tool("srv", "tool", {})
            assert mgr._connections["srv"].breaker.state == "open"

            with pytest.raises(ToolError, match="circuit breaker open"):
                await mgr.call_tool("srv", "tool", {})

        # The fast-fail never reached the upstream
        assert session.call_tool.call_count == 2

    async def test_fast_fail_counters_stay_reconciled(self):
        """Wire-in counters (#558 invariant): the fast-fail records exactly
        one ``circuit_open`` error row and nothing else."""
        from mcp.server.fastmcp.exceptions import ToolError

        from memtomem_stm.proxy.metrics import ErrorCategory

        mgr = _make_manager(max_retries=0, circuit_max_failures=2)
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("down")

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            for _ in range(2):
                with pytest.raises(ConnectionError):
                    await mgr.call_tool("srv", "tool", {})
            with pytest.raises(ToolError):
                await mgr.call_tool("srv", "tool", {})

        cats = mgr.tracker._errors_by_category
        assert cats[ErrorCategory.TRANSPORT.value] == 2
        assert cats[ErrorCategory.CIRCUIT_OPEN.value] == 1
        # _mark_recorded honored: the outer wrapper adds no INTERNAL_ERROR row
        assert cats[ErrorCategory.INTERNAL_ERROR.value] == 0
        summary = mgr.tracker.get_summary()
        assert summary["total_invocations"] == 3
        assert summary["total_errors"] == 3

    async def test_one_breaker_count_per_call_not_per_attempt(self):
        mgr = _make_manager(max_retries=2, circuit_max_failures=3, tools=_read_only_tools())
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("down")

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})

        breaker = mgr._connections["srv"].breaker
        assert session.call_tool.call_count == 3  # 3 attempts...
        assert breaker.failure_count == 1  # ...one breaker count
        assert breaker.state == "closed"

    async def test_success_resets_failure_count(self):
        mgr = _make_manager(max_retries=0, circuit_max_failures=3)
        session = _get_session(mgr)
        session.call_tool.side_effect = [ConnectionError("down"), _make_result("ok")]

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})
            breaker = mgr._connections["srv"].breaker
            assert breaker.failure_count == 1
            result = await mgr.call_tool("srv", "tool", {})

        assert result == "ok"
        assert breaker.failure_count == 0
        assert breaker.state == "closed"

    async def test_is_error_result_counts_as_breaker_success(self):
        """A tool-level ``isError`` result proves the upstream is alive —
        it closes the breaker instead of counting against it."""
        from mcp.server.fastmcp.exceptions import ToolError

        from memtomem_stm.proxy.metrics import ErrorCategory

        mgr = _make_manager(max_retries=0, circuit_max_failures=2)
        session = _get_session(mgr)
        session.call_tool.side_effect = [
            ConnectionError("down"),
            _make_result("tool blew up", is_error=True),
        ]

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})
            with pytest.raises(ToolError, match="tool blew up"):
                await mgr.call_tool("srv", "tool", {})

        breaker = mgr._connections["srv"].breaker
        assert breaker.failure_count == 0
        assert breaker.state == "closed"
        assert mgr.tracker._errors_by_category[ErrorCategory.UPSTREAM_ERROR.value] == 1

    async def test_protocol_and_programming_errors_do_not_count(self):
        mgr = _make_manager(max_retries=0, circuit_max_failures=1)
        session = _get_session(mgr)
        breaker = mgr._connections["srv"].breaker

        protocol_exc = Exception("bad params")
        protocol_exc.error = SimpleNamespace(code=-32602)
        session.call_tool.side_effect = protocol_exc
        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(Exception, match="bad params"):
                await mgr.call_tool("srv", "tool", {})
        assert breaker.failure_count == 0

        session.call_tool.side_effect = TypeError("wrong type")
        with pytest.raises(TypeError):
            await mgr.call_tool("srv", "tool", {})
        assert breaker.failure_count == 0
        assert breaker.state == "closed"

    async def test_protocol_round_trip_resets_failure_streak(self):
        """A JSON-RPC protocol reply is a completed round-trip: it proves the
        upstream is alive and must reset an accumulated transport-failure
        streak, so failures separated by a protocol reply are not treated as
        consecutive (regression for codex review of #608)."""
        mgr = _make_manager(max_retries=0, circuit_max_failures=3)
        session = _get_session(mgr)
        breaker = mgr._connections["srv"].breaker

        protocol_exc = Exception("bad params")
        protocol_exc.error = SimpleNamespace(code=-32602)

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            # Two transport failures build the streak...
            session.call_tool.side_effect = ConnectionError("down")
            for _ in range(2):
                with pytest.raises(ConnectionError):
                    await mgr.call_tool("srv", "tool", {})
            assert breaker.failure_count == 2

            # ...a protocol round-trip resets it (upstream proven alive)...
            session.call_tool.side_effect = protocol_exc
            with pytest.raises(Exception, match="bad params"):
                await mgr.call_tool("srv", "tool", {})
            assert breaker.failure_count == 0
            assert breaker.state == "closed"

            # ...so a subsequent transport failure starts a fresh streak and
            # does NOT open the breaker at the old count.
            session.call_tool.side_effect = ConnectionError("down")
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})
            assert breaker.failure_count == 1
            assert breaker.state == "closed"

    async def test_mid_loop_reconnect_failure_records_breaker_failure(self):
        """The third terminal exit: a retryable transport error whose
        follow-up ``_reconnect_server`` itself fails re-raises the reconnect
        error and must still count one breaker failure (#608), or a dead
        stdio upstream whose respawns fail escapes breaker counting."""
        mgr = _make_manager(max_retries=3, circuit_max_failures=2, tools=_read_only_tools())
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("down")
        breaker = mgr._connections["srv"].breaker

        with patch.object(
            mgr, "_reconnect_server", new_callable=AsyncMock, side_effect=OSError("respawn failed")
        ):
            with pytest.raises(OSError, match="respawn failed"):
                await mgr.call_tool("srv", "tool", {})

        # One breaker count for this call despite the mid-loop reconnect abort
        assert breaker.failure_count == 1
        assert breaker.state == "closed"

    async def test_half_open_probe_success_closes(self):
        mgr = _make_manager(max_retries=0, circuit_max_failures=1)
        session = _get_session(mgr)
        session.call_tool.side_effect = [ConnectionError("down"), _make_result("ok")]
        breaker = mgr._connections["srv"].breaker

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})
            assert breaker.state == "open"

            with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
                mock_time.monotonic.return_value = breaker.opened_at + 61.0
                result = await mgr.call_tool("srv", "tool", {})

        assert result == "ok"
        assert breaker.state == "closed"
        assert session.call_tool.call_count == 2  # the probe reached the session

    async def test_half_open_probe_failure_reopens(self):
        mgr = _make_manager(max_retries=0, circuit_max_failures=1)
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("still down")
        breaker = mgr._connections["srv"].breaker

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})
            first_opened_at = breaker.opened_at

            with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
                mock_time.monotonic.return_value = first_opened_at + 61.0
                with pytest.raises(ConnectionError):
                    await mgr.call_tool("srv", "tool", {})

        assert breaker.state == "open"
        assert breaker.opened_at != first_opened_at  # fresh window from the probe failure

    async def test_overall_deadline_terminal_records_breaker_failure(self):
        mgr = _make_manager(
            max_retries=10,
            call_timeout=0.01,
            overall_deadline=0.05,
            circuit_max_failures=3,
            tools=_read_only_tools(),
        )
        session = _get_session(mgr)

        async def hang(*args, **kwargs):
            await asyncio.sleep(10)

        session.call_tool.side_effect = hang

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(asyncio.TimeoutError, match="overall_deadline"):
                await mgr.call_tool("srv", "tool", {})

        breaker = mgr._connections["srv"].breaker
        assert breaker.failure_count == 1

    async def test_disabled_breaker_never_fast_fails(self):
        mgr = _make_manager(max_retries=0, circuit_max_failures=0)
        session = _get_session(mgr)
        session.call_tool.side_effect = ConnectionError("down")

        assert mgr._connections["srv"].breaker is None
        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            for _ in range(5):
                with pytest.raises(ConnectionError):
                    await mgr.call_tool("srv", "tool", {})

        # Every call reached the upstream — no fast-fail ever intervened
        assert session.call_tool.call_count == 5

    async def test_open_breaker_isolates_only_its_upstream(self):
        mgr = _make_manager(max_retries=0, circuit_max_failures=1)
        srv2_cfg = UpstreamServerConfig(prefix="other")
        srv2_session = AsyncMock()
        srv2_session.call_tool.return_value = _make_result("srv2 ok")
        mgr._connections["srv2"] = UpstreamConnection(
            name="srv2",
            config=srv2_cfg,
            session=srv2_session,
            tools=[],
            breaker=CircuitBreaker(max_failures=1, reset_timeout=60.0, name="upstream-srv2"),
        )

        # Open srv's breaker directly (same technique as the surfacing tests)
        mgr._connections["srv"].breaker.record_failure()
        assert mgr._connections["srv"].breaker.state == "open"

        result = await mgr.call_tool("srv2", "tool", {})
        assert result == "srv2 ok"
        assert mgr._connections["srv2"].breaker.state == "closed"

    async def test_cache_hit_still_serves_while_open(self):
        mgr = _make_manager(max_retries=0, circuit_max_failures=1)
        session = _get_session(mgr)
        session.call_tool.side_effect = AssertionError("should not be called")

        cache = MagicMock()
        cache.get.return_value = "cached result"
        mgr._cache = cache

        mgr._connections["srv"].breaker.record_failure()
        assert mgr._connections["srv"].breaker.state == "open"

        result = await mgr.call_tool("srv", "tool", {})
        assert result == "cached result"
        session.call_tool.assert_not_called()
