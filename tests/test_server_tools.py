"""Tests for server.py MCP tool handlers — stm_proxy_*, stm_surfacing_*, stm_compression_*."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from memtomem_stm.proxy.config import ProxyConfig, UpstreamServerConfig
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.server import (
    STMContext,
    _should_advertise_obs_tools,
    stm_compression_feedback,
    stm_compression_stats,
    stm_progressive_stats,
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
    progressive_reads_tracker: object | None = None,
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
        progressive_reads_tracker=progressive_reads_tracker,
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

    async def test_hints_section_appears_when_events_recorded(self):
        """B3 — when parent emitted trust-UX hints during the run, the
        stats output shows an ``LTM hints`` section with the latest
        snapshot. Quiet when zero events."""
        tracker = TokenTracker()
        tracker.record_hints(["2 results filtered by namespace"])
        ctx = _make_ctx(tracker=tracker)
        result = await stm_proxy_stats(ctx=ctx)
        assert "LTM hints:" in result
        assert "1 event(s)" in result
        assert "2 results filtered by namespace" in result

    async def test_hints_section_omitted_when_no_events(self):
        tracker = TokenTracker()
        ctx = _make_ctx(tracker=tracker)
        result = await stm_proxy_stats(ctx=ctx)
        assert "LTM hints:" not in result


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

    async def test_negative_limit(self):
        """Negative limit returns an error message."""
        ctx = _make_ctx()
        result = await stm_proxy_read_more(key="k1", offset=0, limit=-1, ctx=ctx)
        assert "limit must be >= 1" in result.lower()

    async def test_zero_limit(self):
        """Zero limit returns an error message."""
        ctx = _make_ctx()
        result = await stm_proxy_read_more(key="k1", offset=0, limit=0, ctx=ctx)
        assert "limit must be >= 1" in result.lower()

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
            "events_total": 10,
            "distinct_tools": 2,
            "date_range": {"first": 1_700_000_000.0, "last": 1_700_000_999.0},
            "per_tool_breakdown": [
                {"tool": "t", "events": 7, "avg_memory_count": 3.0},
                {"tool": "u", "events": 3, "avg_memory_count": 2.0},
            ],
            "rating_distribution": {"helpful": 3, "not_relevant": 2},
            "total_feedback": 5,
            "recent": [
                {
                    "ts": 1_700_000_999.0,
                    "tool": "t",
                    "query_preview": "hello world",
                    "memory_ids": ["m1", "m2"],
                    "scores": [0.9, 0.8],
                }
            ],
        }
        ctx = _make_ctx(feedback_tracker=mock_tracker)
        result = await stm_surfacing_stats(tool="t", ctx=ctx)

        assert "Surfacing Stats" in result
        assert "Events total:    10" in result
        assert "Distinct tools:  2" in result
        assert "Total feedback:  5" in result
        assert "By tool:" in result
        assert "t: 7 events" in result
        assert "helpful: 3" in result
        assert "Helpfulness: 60.0%" in result
        assert "Recent:" in result
        assert "hello world" in result
        assert "(filtered by tool: t)" in result

    async def test_invalid_since(self):
        """Malformed ISO timestamp is rejected cleanly, not raised."""
        mock_tracker = MagicMock()
        ctx = _make_ctx(feedback_tracker=mock_tracker)
        result = await stm_surfacing_stats(since="not-a-date", ctx=ctx)
        assert "invalid 'since' timestamp" in result
        mock_tracker.get_stats.assert_not_called()


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


# ── stm_progressive_stats ────────────────────────────────────────────────


class TestProgressiveStats:
    async def test_no_tracker(self):
        """Without tracker, returns 'not enabled'."""
        ctx = _make_ctx(progressive_reads_tracker=None)
        result = await stm_progressive_stats(ctx=ctx)
        assert "not enabled" in result.lower()

    async def test_with_data(self):
        """Returns formatted stats with per-tool breakdown."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "total_reads": 12,
            "total_responses": 5,
            "follow_up_rate": 0.4,
            "avg_chars_served": 7200.0,
            "avg_total_chars": 9500.0,
            "avg_coverage": 0.76,
            "by_tool": {
                "docfix:get_doc": {"responses": 3, "follow_up_rate": 0.667},
                "next:search": {"responses": 2, "follow_up_rate": 0.0},
            },
        }
        ctx = _make_ctx(progressive_reads_tracker=mock_tracker)
        result = await stm_progressive_stats(ctx=ctx)

        assert "Progressive Reads Stats" in result
        assert "Total reads: 12" in result
        assert "Total responses: 5" in result
        assert "Follow-up rate: 40.0%" in result
        assert "Avg coverage: 76.0%" in result
        assert "By tool:" in result
        assert "docfix:get_doc" in result
        assert "responses=3" in result

    async def test_tool_filter_omits_by_tool(self):
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "total_reads": 2,
            "total_responses": 1,
            "follow_up_rate": 1.0,
            "avg_chars_served": 9000.0,
            "avg_total_chars": 9000.0,
            "avg_coverage": 1.0,
            "by_tool": {},
        }
        ctx = _make_ctx(progressive_reads_tracker=mock_tracker)
        result = await stm_progressive_stats(tool="docfix:get_doc", ctx=ctx)

        assert "By tool:" not in result
        assert "filtered by tool: docfix:get_doc" in result
        mock_tracker.get_stats.assert_called_once_with("docfix:get_doc")


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
            mock_cfg.proxy.progressive_reads.enabled = False
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
            mock_cfg.proxy.progressive_reads.enabled = False
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


# ── advertise_observability_tools flag ──────────────────────────────────
#
# The flag hides 6 observability tools from the MCP ``tools/list`` surface
# while keeping them importable from Python. Registration happens at
# module import, so the end-to-end assertion uses a subprocess to get a
# fresh interpreter under the intended env var.


_FLAG_ENV = "MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS"

_MODEL_FACING_TOOLS = {
    "stm_proxy_read_more",
    "stm_proxy_select_chunks",
    "stm_surfacing_feedback",
    "stm_compression_feedback",
}

_OBSERVABILITY_TOOLS = {
    "stm_proxy_stats",
    "stm_proxy_health",
    "stm_proxy_cache_clear",
    "stm_surfacing_stats",
    "stm_compression_stats",
    "stm_progressive_stats",
    "stm_tuning_recommendations",
}


class TestShouldAdvertiseObsTools:
    """Unit tests for the env-var helper. Monkeypatchable — no module reload."""

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(_FLAG_ENV, raising=False)
        assert _should_advertise_obs_tools() is True

    def test_false_variants_disable(self, monkeypatch):
        for value in ("false", "FALSE", "False", "0", "no", "NO", "  false  "):
            monkeypatch.setenv(_FLAG_ENV, value)
            assert _should_advertise_obs_tools() is False, f"{value!r} should disable"

    def test_other_values_passthrough(self, monkeypatch):
        for value in ("true", "yes", "1", "", "anything-else"):
            monkeypatch.setenv(_FLAG_ENV, value)
            assert _should_advertise_obs_tools() is True, f"{value!r} should keep default-on"


class TestAdvertiseObservabilityFlagEndToEnd:
    """Subprocess-based — confirms registration-time behavior end-to-end.

    We can't monkeypatch the flag after the current test-process has already
    imported ``memtomem_stm.server`` (registration runs at import, module
    is cached). A fresh subprocess gets a clean interpreter.
    """

    @staticmethod
    def _list_registered(env_override: str | None) -> list[str]:
        import json
        import os
        import subprocess
        import sys

        env = {k: v for k, v in os.environ.items() if k != _FLAG_ENV}
        if env_override is not None:
            env[_FLAG_ENV] = env_override
        script = (
            "import json\n"
            "from memtomem_stm import server\n"
            "names = [t.name for t in server.mcp._tool_manager.list_tools()]\n"
            "print(json.dumps(sorted(names)))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_default_advertises_all_eleven(self):
        names = set(self._list_registered(env_override=None))
        assert names == _MODEL_FACING_TOOLS | _OBSERVABILITY_TOOLS
        assert len(names) == 11

    def test_flag_false_keeps_only_model_facing(self):
        names = set(self._list_registered(env_override="false"))
        assert names == _MODEL_FACING_TOOLS
        assert _OBSERVABILITY_TOOLS.isdisjoint(names)

    def test_hidden_functions_stay_importable(self):
        """When flag hides them from MCP, Python import and direct call still work."""
        # These imports already succeeded at test-file load with flag=True,
        # but the functions exist unconditionally — the flag only gates the
        # @_obs_tool registration wrapper, not the `async def`.
        assert callable(stm_proxy_stats)
        assert callable(stm_surfacing_stats)
        assert callable(stm_tuning_recommendations)


# ── main() exception barrier (#209) ──────────────────────────────────────


class TestMainExceptionBarrier:
    """#209 Part A: unhandled exceptions from ``mcp.run()`` must be logged at
    ERROR level before the process terminates, so operators have a visible
    signal instead of only stderr tail output."""

    def test_unhandled_exception_is_logged_then_reraised(self, caplog):
        import logging

        from memtomem_stm import server

        class _ServerBoom(RuntimeError):
            pass

        caplog.clear()
        with (
            caplog.at_level(logging.ERROR, logger="memtomem_stm.server"),
            patch.object(server.mcp, "run", side_effect=_ServerBoom("event loop crashed")),
        ):
            try:
                server.main()
            except _ServerBoom:
                pass
            else:
                import pytest as _pytest

                _pytest.fail("main() must re-raise the underlying exception")

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, "expected at least one ERROR-level log from the barrier"
        # The logger.exception call must attach the traceback so operators
        # can diagnose WHY the server died — not just that it did.
        assert any(r.exc_info is not None for r in error_records)
        assert any("unhandled exception" in r.getMessage() for r in error_records)

    def test_clean_exit_does_not_log_error(self, caplog):
        import logging

        from memtomem_stm import server

        caplog.clear()
        with (
            caplog.at_level(logging.ERROR, logger="memtomem_stm.server"),
            patch.object(server.mcp, "run", return_value=None),
        ):
            server.main()

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_records, (
            f"clean exit should not emit ERROR logs; got: "
            f"{[r.getMessage() for r in error_records]}"
        )
