"""Tests for server.py MCP tool handlers — stm_proxy_*, stm_surfacing_*, stm_compression_*."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from memtomem_stm.proxy.config import ProxyConfig, UpstreamServerConfig
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.server import (
    STMContext,
    stm_compression_feedback,
    stm_compression_stats,
    stm_proxy_cache_clear,
    stm_proxy_health,
    stm_proxy_read_more,
    stm_proxy_select_chunks,
    stm_proxy_stats,
    stm_surfacing_feedback,
    stm_surfacing_stats,
    stm_tuning_recommendations,
)

# We also need the STMConfig for building the context
from pathlib import Path

from memtomem_stm.config import STMConfig


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_ctx(
    proxy_manager: ProxyManager | None = None,
    tracker: TokenTracker | None = None,
    surfacing_engine: object | None = None,
    feedback_tracker: object | None = None,
    compression_feedback_tracker: object | None = None,
) -> SimpleNamespace:
    """Build a fake CtxType that _get_ctx() can unwrap.

    The real CtxType is ``Context[ServerSession, STMContext]``; the only
    path through ``_get_ctx`` is ``ctx.request_context.lifespan_context``.
    """
    if tracker is None:
        tracker = TokenTracker()
    if proxy_manager is None:
        cfg = ProxyConfig(config_path="/tmp/proxy.json", upstream_servers={})
        proxy_manager = ProxyManager(cfg, tracker)

    app = STMContext(
        config=STMConfig(),
        proxy_manager=proxy_manager,
        tracker=tracker,
        surfacing_engine=surfacing_engine,
        feedback_tracker=feedback_tracker,
        compression_feedback_tracker=compression_feedback_tracker,
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _make_proxy_manager(tmp_path=None):
    cfg = ProxyConfig(
        config_path=(tmp_path / "p.json") if tmp_path else "/tmp/p.json",
        upstream_servers={},
    )
    return ProxyManager(cfg, TokenTracker())


# ── stm_proxy_stats ──────────────────────────────────────────────────────


class TestProxyStats:
    async def test_basic_output(self):
        """stm_proxy_stats returns formatted stats string."""
        ctx = _make_ctx()
        result = await stm_proxy_stats(ctx=ctx)
        assert "STM Proxy Stats" in result
        assert "Total calls:" in result
        assert "Savings:" in result

    async def test_with_errors(self):
        """When errors exist, the error section is included."""
        from memtomem_stm.proxy.metrics import CallMetrics, ErrorCategory

        tracker = TokenTracker()
        tracker.record_error(
            CallMetrics(
                server="srv",
                tool="t",
                original_chars=0,
                compressed_chars=0,
                is_error=True,
                error_category=ErrorCategory.TRANSPORT,
            )
        )
        ctx = _make_ctx(tracker=tracker)
        result = await stm_proxy_stats(ctx=ctx)
        assert "Errors:" in result

    async def test_surfacing_status(self):
        """Surfacing enabled/disabled line changes with engine presence."""
        ctx_off = _make_ctx(surfacing_engine=None)
        result_off = await stm_proxy_stats(ctx=ctx_off)
        assert "Surfacing: disabled" in result_off

        mock_engine = MagicMock()
        mock_engine.surface = AsyncMock()
        ctx_on = _make_ctx(surfacing_engine=mock_engine)
        result_on = await stm_proxy_stats(ctx=ctx_on)
        assert "Surfacing: enabled" in result_on


# ── stm_proxy_select_chunks ──────────────────────────────────────────────


class TestSelectChunks:
    async def test_delegates(self):
        """stm_proxy_select_chunks delegates to proxy_manager.select_chunks."""
        pm = _make_proxy_manager()
        pm._selective_compressor = MagicMock()
        pm._selective_compressor.select.return_value = "chunk data"

        ctx = _make_ctx(proxy_manager=pm)
        result = await stm_proxy_select_chunks(key="k1", sections=["a"], ctx=ctx)
        assert result == "chunk data"


# ── stm_proxy_read_more ──────────────────────────────────────────────────


class TestReadMore:
    async def test_negative_offset(self):
        """Negative offset returns an error message."""
        ctx = _make_ctx()
        result = await stm_proxy_read_more(key="k1", offset=-1, ctx=ctx)
        assert "offset must be >= 0" in result.lower()

    async def test_delegates(self):
        """stm_proxy_read_more delegates to proxy_manager.read_more."""
        pm = _make_proxy_manager()
        with patch.object(pm, "read_more", return_value="more content") as mock_rm:
            ctx = _make_ctx(proxy_manager=pm)
            result = await stm_proxy_read_more(key="k1", offset=100, limit=50, ctx=ctx)

        assert result == "more content"
        mock_rm.assert_called_once_with("k1", 100, 50)


# ── stm_proxy_cache_clear ────────────────────────────────────────────────


class TestCacheClear:
    async def test_no_cache(self):
        """When cache is not enabled, returns informative message."""
        pm = _make_proxy_manager()
        pm._cache = None
        ctx = _make_ctx(proxy_manager=pm)
        result = await stm_proxy_cache_clear(ctx=ctx)
        assert "not enabled" in result.lower()

    async def test_with_filters(self):
        """Clears cache and reports count."""
        pm = _make_proxy_manager()
        mock_cache = MagicMock()
        mock_cache.clear.return_value = 5
        pm._cache = mock_cache

        ctx = _make_ctx(proxy_manager=pm)
        result = await stm_proxy_cache_clear(server="srv", tool="t", ctx=ctx)
        assert "5" in result
        assert "srv/t" in result
        mock_cache.clear.assert_called_once_with(server="srv", tool="t")


# ── stm_proxy_health ─────────────────────────────────────────────────────


class TestHealth:
    async def test_no_servers(self):
        """Empty connections returns 'No upstream servers' message."""
        pm = _make_proxy_manager()
        ctx = _make_ctx(proxy_manager=pm)
        result = await stm_proxy_health(ctx=ctx)
        assert "No upstream servers configured" in result

    async def test_with_servers(self):
        """Reports connection status for each server."""
        pm = _make_proxy_manager()
        conn = UpstreamConnection(
            name="srv",
            config=UpstreamServerConfig(prefix="test"),
            session=AsyncMock(),
            tools=[MagicMock(), MagicMock()],
        )
        pm._connections["srv"] = conn
        ctx = _make_ctx(proxy_manager=pm)
        result = await stm_proxy_health(ctx=ctx)
        assert "srv: connected (2 tools)" in result


# ── stm_surfacing_feedback ───────────────────────────────────────────────


class TestSurfacingFeedback:
    async def test_via_engine(self):
        """Routes through SurfacingEngine when available."""
        mock_engine = AsyncMock()
        mock_engine.handle_feedback.return_value = "Feedback recorded via engine."

        ctx = _make_ctx(surfacing_engine=mock_engine)
        result = await stm_surfacing_feedback(surfacing_id="s1", rating="helpful", ctx=ctx)
        assert "recorded" in result.lower()
        mock_engine.handle_feedback.assert_awaited_once_with("s1", "helpful", None)

    async def test_no_engine_no_tracker(self):
        """Without engine or tracker, returns 'not enabled' message."""
        ctx = _make_ctx(surfacing_engine=None, feedback_tracker=None)
        result = await stm_surfacing_feedback(surfacing_id="s1", rating="helpful", ctx=ctx)
        assert "not enabled" in result.lower()

    async def test_fallback_to_tracker(self):
        """Without engine but with tracker, records via tracker."""
        mock_tracker = MagicMock()
        mock_tracker.record_feedback.return_value = "Recorded."

        ctx = _make_ctx(surfacing_engine=None, feedback_tracker=mock_tracker)
        result = await stm_surfacing_feedback(
            surfacing_id="s1", rating="not_relevant", memory_id="m1", ctx=ctx
        )
        assert result == "Recorded."
        mock_tracker.record_feedback.assert_called_once_with("s1", "not_relevant", "m1")


# ── stm_surfacing_stats ──────────────────────────────────────────────────


class TestSurfacingStats:
    async def test_no_tracker(self):
        """Without feedback tracker, returns 'not enabled'."""
        ctx = _make_ctx(feedback_tracker=None)
        result = await stm_surfacing_stats(ctx=ctx)
        assert "not enabled" in result.lower()

    async def test_with_data(self):
        """Returns formatted stats when data is available."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "total_surfacings": 10,
            "total_feedback": 5,
            "by_rating": {"helpful": 3, "not_relevant": 2},
        }
        ctx = _make_ctx(feedback_tracker=mock_tracker)
        result = await stm_surfacing_stats(tool="t", ctx=ctx)

        assert "Surfacing Stats" in result
        assert "Total surfacings: 10" in result
        assert "helpful: 3" in result
        assert "Helpfulness: 60.0%" in result
        assert "(filtered by tool: t)" in result


# ── stm_compression_feedback ─────────────────────────────────────────────


class TestCompressionFeedback:
    async def test_no_tracker(self):
        """Without compression feedback tracker, returns 'not enabled'."""
        ctx = _make_ctx(compression_feedback_tracker=None)
        result = await stm_compression_feedback(
            server="srv", tool="t", missing="example code", ctx=ctx
        )
        assert "not enabled" in result.lower()

    async def test_records(self):
        """Records compression feedback via tracker."""
        mock_tracker = MagicMock()
        mock_tracker.record.return_value = "Feedback recorded."

        ctx = _make_ctx(compression_feedback_tracker=mock_tracker)
        result = await stm_compression_feedback(
            server="srv",
            tool="t",
            missing="example code",
            kind="missing_example",
            trace_id="abc123",
            ctx=ctx,
        )
        assert result == "Feedback recorded."
        mock_tracker.record.assert_called_once_with(
            server="srv",
            tool="t",
            missing="example code",
            kind="missing_example",
            trace_id="abc123",
        )


# ── stm_compression_stats ────────────────────────────────────────────────


class TestCompressionStats:
    async def test_no_tracker(self):
        """Without tracker, returns 'not enabled'."""
        ctx = _make_ctx(compression_feedback_tracker=None)
        result = await stm_compression_stats(ctx=ctx)
        assert "not enabled" in result.lower()

    async def test_with_data(self):
        """Returns formatted stats with breakdown."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "total_feedback": 8,
            "by_kind": {"truncated": 5, "missing_example": 3},
            "by_tool": {"get_doc": 6, "search": 2},
        }
        ctx = _make_ctx(compression_feedback_tracker=mock_tracker)
        result = await stm_compression_stats(ctx=ctx)

        assert "Compression Feedback Stats" in result
        assert "Total feedback: 8" in result
        assert "truncated: 5" in result
        assert "By tool:" in result


# ── stm_tuning_recommendations ────────────────────────────────────────────


class TestTuningRecommendations:
    async def test_no_metrics_store(self):
        """Without metrics store, returns 'not enabled'."""
        tracker = TokenTracker(metrics_store=None)
        ctx = _make_ctx(tracker=tracker)
        result = await stm_tuning_recommendations(ctx=ctx)
        assert "not enabled" in result.lower()


# ── app_lifespan ──────────────────────────────────────────────────────────


class TestLifespan:
    async def test_proxy_disabled(self):
        """When proxy is disabled, ProxyManager.start() should not be called."""
        from memtomem_stm.server import app_lifespan, mcp

        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock()
        mock_pm_instance.stop = AsyncMock()
        mock_pm_instance.get_proxy_tools.return_value = []

        with (
            patch("memtomem_stm.server.STMConfig") as MockConfig,
            patch("memtomem_stm.server.ProxyManager", return_value=mock_pm_instance),
        ):
            mock_cfg = MockConfig.return_value
            mock_cfg.proxy = MagicMock()
            mock_cfg.proxy.enabled = False
            mock_cfg.proxy.config_path = Path("/tmp/proxy.json")
            mock_cfg.surfacing = MagicMock()
            mock_cfg.surfacing.enabled = False
            mock_cfg.langfuse = MagicMock()
            mock_cfg.langfuse.enabled = False

            async with app_lifespan(mcp) as _ctx:
                # ProxyManager.start() should NOT be called when proxy is disabled
                mock_pm_instance.start.assert_not_awaited()

    async def test_feedback_tracker_init_failure_degrades_gracefully(self):
        """FeedbackTracker raising at init should log and fall back to
        feedback_tracker=None — learning-loop feature must not crash the
        server. Mirrors the CompressionFeedbackTracker guard pattern."""
        from memtomem_stm.server import app_lifespan, mcp

        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock()
        mock_pm_instance.stop = AsyncMock()
        mock_pm_instance.get_proxy_tools.return_value = []

        mock_adapter = MagicMock()
        mock_adapter.start = AsyncMock()
        mock_adapter.stop = AsyncMock()

        captured_engine_kwargs: dict = {}

        def _capture_engine(*_args, **kwargs):
            captured_engine_kwargs.update(kwargs)
            engine = MagicMock()
            engine.stop = AsyncMock()
            return engine

        with (
            patch("memtomem_stm.server.STMConfig") as MockConfig,
            patch("memtomem_stm.server.ProxyManager", return_value=mock_pm_instance),
            patch(
                "memtomem_stm.surfacing.mcp_client.McpClientSearchAdapter",
                return_value=mock_adapter,
            ),
            patch(
                "memtomem_stm.server.FeedbackTracker",
                side_effect=RuntimeError("disk full"),
            ),
            patch("memtomem_stm.server.SurfacingEngine", side_effect=_capture_engine),
        ):
            mock_cfg = MockConfig.return_value
            mock_cfg.proxy = MagicMock()
            mock_cfg.proxy.enabled = True
            mock_cfg.proxy.config_path = Path("/tmp/proxy.json")
            mock_cfg.proxy.metrics.enabled = False
            mock_cfg.proxy.compression_feedback.enabled = False
            mock_cfg.proxy.cache.enabled = False
            mock_cfg.surfacing = MagicMock()
            mock_cfg.surfacing.enabled = True
            mock_cfg.surfacing.feedback_enabled = True
            mock_cfg.langfuse = MagicMock()
            mock_cfg.langfuse.enabled = False

            async with app_lifespan(mcp) as ctx:
                assert ctx.feedback_tracker is None
                assert captured_engine_kwargs.get("feedback_tracker") is None

    async def test_init_failure_after_mcp_adapter_runs_cleanup(self):
        """If a post-mcp_adapter init step raises (e.g. proxy_manager.start()),
        the mcp_adapter stdio subprocess must still be stopped. Without the
        outer try/finally, a partial init leaked the surfacing subprocess and
        metrics/cache sqlite connections because the cleanup block only ran
        after reaching `yield`."""
        import pytest

        from memtomem_stm.server import app_lifespan, mcp

        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock(side_effect=RuntimeError("upstream down"))
        mock_pm_instance.stop = AsyncMock()
        mock_pm_instance.get_proxy_tools.return_value = []

        mock_adapter = MagicMock()
        mock_adapter.start = AsyncMock()
        mock_adapter.stop = AsyncMock()

        with (
            patch("memtomem_stm.server.STMConfig") as MockConfig,
            patch("memtomem_stm.server.ProxyManager", return_value=mock_pm_instance),
            patch(
                "memtomem_stm.surfacing.mcp_client.McpClientSearchAdapter",
                return_value=mock_adapter,
            ),
            patch("memtomem_stm.server.SurfacingEngine", return_value=MagicMock()),
            # Prevent the file-load block at the top of app_lifespan from
            # overwriting our mocked ProxyConfig with the real on-disk one
            # (or its defaults when the file is missing).
            patch("memtomem_stm.server.ProxyConfig.load_from_file", return_value=None),
        ):
            mock_cfg = MockConfig.return_value
            mock_cfg.proxy = MagicMock()
            mock_cfg.proxy.enabled = True
            mock_cfg.proxy.config_path = Path("/tmp/proxy.json")
            mock_cfg.proxy.metrics.enabled = False
            mock_cfg.proxy.compression_feedback.enabled = False
            mock_cfg.proxy.cache.enabled = False
            mock_cfg.surfacing = MagicMock()
            mock_cfg.surfacing.enabled = True
            mock_cfg.surfacing.feedback_enabled = False
            mock_cfg.langfuse = MagicMock()
            mock_cfg.langfuse.enabled = False

            with pytest.raises(RuntimeError, match="upstream down"):
                async with app_lifespan(mcp) as _ctx:
                    pass  # Never reached — start() raises before yield.

        # The cleanup block must run even though yield was never reached.
        mock_adapter.stop.assert_awaited_once()
        mock_pm_instance.stop.assert_awaited_once()
