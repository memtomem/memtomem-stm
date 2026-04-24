"""Tests for observability foundation (Phase 4 of gateway improvements)."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memtomem_stm.proxy.config import CompressionStrategy, ProxyConfig, UpstreamServerConfig
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import CallMetrics, RPSTracker, TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore


# ── RPSTracker ───────────────────────────────────────────────────────────


class TestRPSTracker:
    def test_empty_returns_zero(self):
        t = RPSTracker(window_seconds=60.0)
        assert t.rps() == 0.0

    def test_single_record(self):
        t = RPSTracker(window_seconds=60.0)
        t.record()
        assert t.rps() > 0
        assert t.rps() <= 1.0  # 1 / 60 ≈ 0.017

    def test_burst(self):
        t = RPSTracker(window_seconds=60.0)
        for _ in range(100):
            t.record()
        rps = t.rps()
        # 100 / 60 ≈ 1.67
        assert 1.0 < rps < 2.0

    def test_window_expiry(self):
        t = RPSTracker(window_seconds=1.0)
        t.record()
        assert t.rps() > 0
        # Simulate time passing
        t._timestamps[0] = time.monotonic() - 2.0
        assert t.rps() == 0.0

    def test_reset(self):
        t = RPSTracker()
        for _ in range(50):
            t.record()
        t.reset()
        assert t.rps() == 0.0

    def test_small_window(self):
        t = RPSTracker(window_seconds=1.0)
        for _ in range(10):
            t.record()
        rps = t.rps()
        assert rps == 10.0  # 10 / 1


# ── TokenTracker RPS integration ─────────────────────────────────────────


class TestTokenTrackerRPS:
    def test_summary_includes_current_rps(self):
        tracker = TokenTracker()
        s = tracker.get_summary()
        assert "current_rps" in s
        assert s["current_rps"] == 0.0

    def test_record_increments_rps(self):
        tracker = TokenTracker()
        for _ in range(5):
            tracker.record(
                CallMetrics(server="s", tool="t", original_chars=100, compressed_chars=50)
            )
        s = tracker.get_summary()
        assert s["current_rps"] > 0

    def test_record_error_increments_rps(self):
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
        s = tracker.get_summary()
        assert s["current_rps"] > 0


# ── trace_id generation and propagation ──────────────────────────────────


def _text_content(text: str):
    return SimpleNamespace(type="text", text=text)


def _make_result(text: str, is_error: bool = False):
    return SimpleNamespace(content=[_text_content(text)], isError=is_error)


def _make_manager() -> ProxyManager:
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=CompressionStrategy.NONE,
        max_result_chars=50000,
        max_retries=0,
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


class TestTraceIdPropagation:
    async def test_success_path_has_trace_id(self):
        mgr = _make_manager()
        mgr._connections["srv"].session.call_tool.return_value = _make_result("ok")

        recorded: list[CallMetrics] = []
        original_record = mgr.tracker.record

        def capture(m):
            recorded.append(m)
            original_record(m)

        mgr.tracker.record = capture
        await mgr.call_tool("srv", "tool", {})

        assert len(recorded) == 1
        assert recorded[0].trace_id is not None
        assert len(recorded[0].trace_id) == 16

    async def test_error_path_has_trace_id(self):
        mgr = _make_manager()
        mgr._connections["srv"].session.call_tool.side_effect = ConnectionError("down")

        recorded_errors: list[CallMetrics] = []
        original_record_error = mgr.tracker.record_error

        def capture(m):
            recorded_errors.append(m)
            original_record_error(m)

        mgr.tracker.record_error = capture
        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})

        assert len(recorded_errors) == 1
        assert recorded_errors[0].trace_id is not None
        assert len(recorded_errors[0].trace_id) == 16

    async def test_trace_ids_unique_per_call(self):
        mgr = _make_manager()
        mgr._connections["srv"].session.call_tool.return_value = _make_result("ok")

        trace_ids: list[str] = []
        original_record = mgr.tracker.record

        def capture(m):
            trace_ids.append(m.trace_id)
            original_record(m)

        mgr.tracker.record = capture
        await mgr.call_tool("srv", "tool", {})
        await mgr.call_tool("srv", "tool", {})

        assert len(trace_ids) == 2
        assert trace_ids[0] != trace_ids[1]


# ── MetricsStore trace_id ────────────────────────────────────────────────


class TestMetricsStoreTraceId:
    def test_trace_id_column_exists(self, tmp_path):
        store = MetricsStore(tmp_path / "test.db")
        store.initialize()
        cols = {row[1] for row in store._db.execute("PRAGMA table_info(proxy_metrics)")}
        assert "trace_id" in cols
        store.close()

    def test_trace_id_stored(self, tmp_path):
        store = MetricsStore(tmp_path / "test.db")
        store.initialize()
        store.record(
            CallMetrics(
                server="srv",
                tool="tool",
                original_chars=100,
                compressed_chars=50,
                trace_id="abc123def456gh",
            )
        )
        row = store._db.execute("SELECT trace_id FROM proxy_metrics").fetchone()
        assert row[0] == "abc123def456gh"
        store.close()


# ── get_upstream_health ──────────────────────────────────────────────────


class TestUpstreamHealth:
    def test_single_connected_server(self):
        mgr = _make_manager()
        health = mgr.get_upstream_health()
        assert "srv" in health
        assert health["srv"]["connected"] is True
        assert health["srv"]["tools"] == 0

    def test_server_with_tools(self):
        mgr = _make_manager()
        fake_tools = [SimpleNamespace(name=f"t{i}") for i in range(5)]
        mgr._connections["srv"].tools = fake_tools
        health = mgr.get_upstream_health()
        assert health["srv"]["tools"] == 5

    def test_empty_connections(self):
        mgr = _make_manager()
        mgr._connections.clear()
        health = mgr.get_upstream_health()
        assert health == {}


# ── Langfuse tracing integration ─────────────────────────────────────────


class TestLangfuseTracing:
    """MVP wiring: call_tool() wraps the pipeline in a Langfuse observation.

    These tests patch the module-level ``_langfuse_client`` singleton in
    ``memtomem_stm.observability.tracing``. When no client is set (default),
    ``traced()`` returns ``nullcontext()`` — which is the path every other
    test in this file already exercises implicitly.
    """

    def test_traced_no_client_returns_nullcontext(self, monkeypatch):
        """Without a configured Langfuse client, traced() is a no-op context manager."""
        monkeypatch.setattr("memtomem_stm.observability.tracing._langfuse_client", None)
        from memtomem_stm.observability.tracing import traced

        with traced("proxy_call", metadata={"server": "srv"}) as span:
            assert span is None  # nullcontext yields None

    def test_traced_with_client_delegates_to_sdk(self, monkeypatch):
        """With a client set, traced() forwards name+kwargs to start_as_current_observation."""
        mock_client = MagicMock()
        monkeypatch.setattr("memtomem_stm.observability.tracing._langfuse_client", mock_client)
        from memtomem_stm.observability.tracing import traced

        traced("proxy_call", metadata={"server": "srv", "tool": "t"})

        mock_client.start_as_current_observation.assert_called_once_with(
            name="proxy_call",
            metadata={"server": "srv", "tool": "t"},
        )

    async def test_call_tool_wraps_pipeline_in_span(self, monkeypatch):
        """ProxyManager.call_tool creates a Langfuse observation per invocation
        with nested sub-spans for each pipeline stage."""
        mock_client = MagicMock()
        monkeypatch.setattr("memtomem_stm.observability.tracing._langfuse_client", mock_client)

        mgr = _make_manager()
        mgr._connections["srv"].session.call_tool.return_value = _make_result("ok")

        result = await mgr.call_tool("srv", "tool", {})

        # Top-level span + nested sub-spans for clean, compress, surface
        calls = mock_client.start_as_current_observation.call_args_list
        span_names = [c.kwargs["name"] for c in calls]
        assert span_names[0] == "proxy_call"
        assert "proxy_call_clean" in span_names
        assert "proxy_call_compress" in span_names
        assert "proxy_call_surface" in span_names

        # Top-level span has correct metadata
        top_call = calls[0]
        metadata = top_call.kwargs["metadata"]
        assert metadata["server"] == "srv"
        assert metadata["tool"] == "tool"
        assert isinstance(metadata["trace_id"], str)
        assert len(metadata["trace_id"]) == 16

        # Return value still flows through
        assert result == "ok"

    def test_sampling_skips_tracing(self, monkeypatch):
        """When sampling_rate < 1.0 and the roll misses, traced() returns nullcontext."""
        mock_client = MagicMock()
        monkeypatch.setattr("memtomem_stm.observability.tracing._langfuse_client", mock_client)
        monkeypatch.setattr("memtomem_stm.observability.tracing._sampling_rate", 0.0)
        from memtomem_stm.observability.tracing import traced

        ctx = traced("proxy_call", metadata={})
        # With rate=0.0, every call should be sampled out → nullcontext
        assert type(ctx).__name__ == "nullcontext"
        mock_client.start_as_current_observation.assert_not_called()

    def test_sampling_rate_one_always_traces(self, monkeypatch):
        """sampling_rate=1.0 means every call is traced."""
        mock_client = MagicMock()
        monkeypatch.setattr("memtomem_stm.observability.tracing._langfuse_client", mock_client)
        monkeypatch.setattr("memtomem_stm.observability.tracing._sampling_rate", 1.0)
        from memtomem_stm.observability.tracing import traced

        traced("proxy_call", metadata={})
        mock_client.start_as_current_observation.assert_called_once()

    async def test_call_tool_span_wraps_error_path(self, monkeypatch):
        """On upstream failure, the span is still created and the exception propagates."""
        mock_client = MagicMock()
        monkeypatch.setattr("memtomem_stm.observability.tracing._langfuse_client", mock_client)

        mgr = _make_manager()
        mgr._connections["srv"].session.call_tool.side_effect = ConnectionError("down")

        with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
            with pytest.raises(ConnectionError):
                await mgr.call_tool("srv", "tool", {})

        mock_client.start_as_current_observation.assert_called_once()
        call = mock_client.start_as_current_observation.call_args
        assert call.kwargs["name"] == "proxy_call"
        assert call.kwargs["metadata"]["server"] == "srv"


# ── Surfacing tool spans ─────────────────────────────────────────────────


class TestSurfacingToolSpans:
    """Verify that stm_surfacing_feedback/stats create Langfuse observations."""

    async def test_surfacing_feedback_creates_span(self, monkeypatch):
        mock_client = MagicMock()
        monkeypatch.setattr("memtomem_stm.observability.tracing._langfuse_client", mock_client)

        mock_engine = AsyncMock()
        mock_engine.handle_feedback.return_value = "ok"

        from memtomem_stm.server import STMContext, stm_surfacing_feedback
        from memtomem_stm.config import STMConfig

        cfg = ProxyConfig(config_path="/tmp/p.json", upstream_servers={})
        app = STMContext(
            config=STMConfig(),
            proxy_manager=ProxyManager(cfg, TokenTracker()),
            tracker=TokenTracker(),
            surfacing_engine=mock_engine,
            feedback_tracker=None,
            compression_feedback_tracker=None,
        )
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))

        await stm_surfacing_feedback(surfacing_id="s1", rating="helpful", memory_id="m1", ctx=ctx)

        calls = mock_client.start_as_current_observation.call_args_list
        span_names = [c.kwargs["name"] for c in calls]
        assert "stm_surfacing_feedback" in span_names

        fb_call = next(c for c in calls if c.kwargs["name"] == "stm_surfacing_feedback")
        meta = fb_call.kwargs["metadata"]
        assert meta["surfacing_id"] == "s1"
        assert meta["rating"] == "helpful"
        assert meta["memory_id"] == "m1"

    async def test_surfacing_stats_creates_span(self, monkeypatch):
        mock_client = MagicMock()
        monkeypatch.setattr("memtomem_stm.observability.tracing._langfuse_client", mock_client)

        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "total_surfacings": 5,
            "total_feedback": 2,
            "by_rating": {"helpful": 2},
        }

        from memtomem_stm.server import STMContext, stm_surfacing_stats
        from memtomem_stm.config import STMConfig

        cfg = ProxyConfig(config_path="/tmp/p.json", upstream_servers={})
        app = STMContext(
            config=STMConfig(),
            proxy_manager=ProxyManager(cfg, TokenTracker()),
            tracker=TokenTracker(),
            surfacing_engine=None,
            feedback_tracker=mock_tracker,
            compression_feedback_tracker=None,
        )
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))

        await stm_surfacing_stats(tool="t", ctx=ctx)

        calls = mock_client.start_as_current_observation.call_args_list
        span_names = [c.kwargs["name"] for c in calls]
        assert "stm_surfacing_stats" in span_names

        stats_call = next(c for c in calls if c.kwargs["name"] == "stm_surfacing_stats")
        assert stats_call.kwargs["metadata"]["tool"] == "t"


# ── Trace ID propagation to upstream ──────────────────────────────────────


class TestTraceIdUpstreamPropagation:
    """Verify _trace_id is included in upstream MCP call arguments."""

    async def test_trace_id_propagated_to_upstream_args(self):
        """call_tool passes _trace_id to upstream session.call_tool args."""
        mgr = _make_manager()
        session = mgr._connections["srv"].session
        session.call_tool.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")], isError=False
        )

        await mgr.call_tool("srv", "tool", {"q": "test"})

        call_args = session.call_tool.call_args
        upstream_args = call_args.args[1]  # second positional arg is the dict
        assert "_trace_id" in upstream_args
        assert len(upstream_args["_trace_id"]) == 16

    async def test_mcp_client_search_includes_trace_id(self):
        """McpClientSearchAdapter.search() passes _trace_id when provided."""
        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter
        from memtomem_stm.surfacing.config import SurfacingConfig

        adapter = McpClientSearchAdapter(SurfacingConfig())
        mock_session = AsyncMock()
        mock_session.call_tool.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="No results")]
        )
        adapter._session = mock_session

        await adapter.search("test query", trace_id="abc123")

        call_args = mock_session.call_tool.call_args
        assert call_args.args[1]["_trace_id"] == "abc123"

    async def test_mcp_client_search_no_trace_id(self):
        """Without trace_id, _trace_id is not included in args."""
        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter
        from memtomem_stm.surfacing.config import SurfacingConfig

        adapter = McpClientSearchAdapter(SurfacingConfig())
        mock_session = AsyncMock()
        mock_session.call_tool.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="No results")]
        )
        adapter._session = mock_session

        await adapter.search("test query")

        call_args = mock_session.call_tool.call_args
        assert "_trace_id" not in call_args.args[1]

    async def test_mcp_client_increment_access_includes_trace_id(self):
        """increment_access passes _trace_id when provided."""
        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter
        from memtomem_stm.surfacing.config import SurfacingConfig

        adapter = McpClientSearchAdapter(SurfacingConfig())
        mock_session = AsyncMock()
        adapter._session = mock_session

        await adapter.increment_access(["chunk1"], trace_id="xyz789")

        call_args = mock_session.call_tool.call_args
        assert call_args.args[1]["_trace_id"] == "xyz789"

    async def test_mcp_client_scratch_list_includes_trace_id(self):
        """scratch_list passes _trace_id when provided."""
        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter
        from memtomem_stm.surfacing.config import SurfacingConfig

        adapter = McpClientSearchAdapter(SurfacingConfig())
        mock_session = AsyncMock()
        mock_session.call_tool.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="")]
        )
        adapter._session = mock_session

        await adapter.scratch_list(trace_id="trace456")

        call_args = mock_session.call_tool.call_args
        assert call_args.args[1]["_trace_id"] == "trace456"
