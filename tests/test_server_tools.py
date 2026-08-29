"""Tests for server.py MCP tool handlers — stm_proxy_*, stm_surfacing_*, stm_compression_*."""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memtomem_stm.proxy.config import ConfigLoadResult, ProxyConfig, UpstreamServerConfig
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.server import (
    STMContext,
    _hidden_obs_tools_hint,
    _OBSERVABILITY_TOOL_NAMES,
    _should_advertise_obs_tools,
    stm_compression_feedback,
    stm_compression_stats,
    stm_progressive_stats,
    stm_proxy_cache_clear,
    stm_proxy_health,
    stm_proxy_read_more,
    stm_proxy_select_chunks,
    stm_proxy_stats,
    stm_selection_stats,
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
    config: STMConfig | None = None,
    proxy_config_error: str | None = None,
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
    if config is None:
        config = STMConfig()

    app = STMContext(
        config=config,
        proxy_manager=proxy_manager,
        tracker=tracker,
        surfacing_engine=surfacing_engine,
        feedback_tracker=feedback_tracker,
        compression_feedback_tracker=compression_feedback_tracker,
        progressive_reads_tracker=progressive_reads_tracker,
        proxy_config_error=proxy_config_error,
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

    async def test_hit_rate_line(self):
        """Derived hit-rate % is shown alongside the raw hit/miss counts."""
        tracker = TokenTracker()
        for _ in range(3):
            tracker.record_cache_hit()
        tracker.record_cache_miss()  # 3 hits / 4 lookups → 75.0%
        ctx = _make_ctx(tracker=tracker)
        result = await stm_proxy_stats(ctx=ctx)
        assert "Cache hit rate:  75.0%" in result

    async def test_total_calls_line_reconciles_invocations(self):
        """#558: the Total calls line states live + cache-served = invocations
        explicitly, so hits are no longer invisible in the call count."""
        from memtomem_stm.proxy.metrics import CallMetrics

        tracker = TokenTracker()
        tracker.record(CallMetrics(server="s", tool="t", original_chars=10, compressed_chars=5))
        tracker.record_cache_hit(chars=5)
        tracker.record_cache_hit(chars=5)
        ctx = _make_ctx(tracker=tracker)
        result = await stm_proxy_stats(ctx=ctx)
        assert "Total calls:     1 live + 2 cache-served = 3 invocations" in result

    async def test_total_calls_line_includes_failed_calls(self):
        """#558 codex round 2: failed calls are a third mutually-exclusive
        component of the invocation total; the component renders only when
        non-zero (matching the Errors section)."""
        from memtomem_stm.proxy.metrics import CallMetrics

        tracker = TokenTracker()
        tracker.record(CallMetrics(server="s", tool="t", original_chars=10, compressed_chars=5))
        tracker.record_cache_hit(chars=5)
        tracker.record_error(
            CallMetrics(server="s", tool="t", original_chars=0, compressed_chars=0, is_error=True)
        )
        ctx = _make_ctx(tracker=tracker)
        result = await stm_proxy_stats(ctx=ctx)
        assert "Total calls:     1 live + 1 failed + 1 cache-served = 3 invocations" in result

    async def test_error_rate_labeled_as_live_attempts(self):
        """#558 codex round 3: error_rate's denominator is live attempts
        (successful + failed calls, no cache hits), so the rendered percentage
        must say so or it reads inconsistent next to the invocation total."""
        from memtomem_stm.proxy.metrics import CallMetrics

        tracker = TokenTracker()
        tracker.record(CallMetrics(server="s", tool="t", original_chars=10, compressed_chars=5))
        tracker.record_error(
            CallMetrics(server="s", tool="t", original_chars=0, compressed_chars=0, is_error=True)
        )
        for _ in range(2):
            tracker.record_cache_hit(chars=5)
        ctx = _make_ctx(tracker=tracker)
        result = await stm_proxy_stats(ctx=ctx)
        # 1 error / (1 live + 1 failed) = 50.0% — NOT 1/4 over the invocations.
        assert "Errors: 1 (50.0% of live attempts)" in result

    async def test_cache_hits_line_shows_served_chars(self):
        """#558: the cache's benefit (chars served with zero upstream I/O) is
        rendered on the hits line; quiet suffix when there are no hits."""
        tracker = TokenTracker()
        tracker.record_cache_hit(chars=1234)
        ctx = _make_ctx(tracker=tracker)
        result = await stm_proxy_stats(ctx=ctx)
        assert "Cache hits:      1  (1,234 chars served from cache, no upstream I/O)" in result

        result_no_hits = await stm_proxy_stats(ctx=_make_ctx(tracker=TokenTracker()))
        assert "chars served from cache" not in result_no_hits

    async def test_unstorable_line_only_when_nonzero(self):
        """#558: unstorable misses get their own line so a permanently
        re-missing tool is diagnosable; hidden in the common all-text case."""
        tracker = TokenTracker()
        tracker.record_cache_miss()
        tracker.record_cache_unstorable()
        ctx = _make_ctx(tracker=tracker)
        result = await stm_proxy_stats(ctx=ctx)
        assert "Unstorable:      1" in result

        result_zero = await stm_proxy_stats(ctx=_make_ctx(tracker=TokenTracker()))
        assert "Unstorable:" not in result_zero

    async def test_effective_hit_rate_over_storable_lookups(self):
        """#558 codex round 1: with unstorable misses present, the hit-rate
        line also shows the rate over storable lookups, so a never-cacheable-
        heavy workload doesn't read as a depressed hit rate."""
        tracker = TokenTracker()
        tracker.record_cache_hit()  # 1 hit
        for _ in range(3):
            tracker.record_cache_miss()  # 3 misses, 2 unstorable
        tracker.record_cache_unstorable()
        tracker.record_cache_unstorable()
        ctx = _make_ctx(tracker=tracker)
        result = await stm_proxy_stats(ctx=ctx)
        # raw: 1/4 = 25.0%; storable: 1 hit + (3-2) convertible miss → 1/2
        assert "Cache hit rate:  25.0%  (50.0% of storable lookups)" in result

    async def test_effective_hit_rate_no_storable_lookups(self):
        """Every lookup was an unstorable miss → no percentage is fabricated."""
        tracker = TokenTracker()
        tracker.record_cache_miss()
        tracker.record_cache_unstorable()
        ctx = _make_ctx(tracker=tracker)
        result = await stm_proxy_stats(ctx=ctx)
        assert "Cache hit rate:  0.0%  (no storable lookups)" in result

    async def test_hit_rate_line_unchanged_without_unstorable(self):
        """Zero unstorable → the pinned plain hit-rate line stays as-is."""
        tracker = TokenTracker()
        tracker.record_cache_hit()
        tracker.record_cache_miss()
        ctx = _make_ctx(tracker=tracker)
        result = await stm_proxy_stats(ctx=ctx)
        assert "Cache hit rate:  50.0%\n" in result

    async def test_cache_entries_line_when_cache_wired(self, tmp_path):
        """When the response cache is wired, occupancy/eviction is surfaced."""
        from memtomem_stm.proxy.cache import ProxyCache

        pm = _make_proxy_manager(tmp_path)
        cache = ProxyCache(tmp_path / "c.db", max_entries=100)
        cache.initialize()
        try:
            cache.set("s", "t", {"a": 1}, "r", ttl_seconds=60.0)
            pm._cache = cache
            ctx = _make_ctx(proxy_manager=pm)
            result = await stm_proxy_stats(ctx=ctx)
            assert "Cache entries:   1" in result
            assert "evicted 0" in result
        finally:
            cache.close()

    async def test_cache_entries_line_absent_without_cache(self):
        """No response cache wired → no occupancy line (hit-rate still shown)."""
        ctx = _make_ctx()
        result = await stm_proxy_stats(ctx=ctx)
        assert "Cache entries:" not in result
        assert "Cache hit rate:" in result


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

    async def test_unfiltered_flushes_both_caches(self):
        """An unfiltered call flushes the response cache AND the surfacing cache."""
        pm = _make_proxy_manager()
        mock_cache = MagicMock()
        mock_cache.clear.return_value = 7
        pm._cache = mock_cache
        engine = MagicMock()
        engine.clear_cache.return_value = 3

        ctx = _make_ctx(proxy_manager=pm, surfacing_engine=engine)
        result = await stm_proxy_cache_clear(ctx=ctx)

        mock_cache.clear.assert_called_once_with()  # no server/tool filter
        engine.clear_cache.assert_called_once_with()
        assert "7" in result and "3" in result
        assert "response-cache" in result and "surfacing-cache" in result

    async def test_filtered_does_not_touch_surfacing(self):
        """A server/tool-filtered call targets only the response cache.

        The surfacing cache is keyed by query hash with no server/tool axis, so
        a filtered clear must leave it untouched (and keep the old wording)."""
        pm = _make_proxy_manager()
        mock_cache = MagicMock()
        mock_cache.clear.return_value = 2
        pm._cache = mock_cache
        engine = MagicMock()

        ctx = _make_ctx(proxy_manager=pm, surfacing_engine=engine)
        result = await stm_proxy_cache_clear(server="srv", ctx=ctx)

        engine.clear_cache.assert_not_called()
        mock_cache.clear.assert_called_once_with(server="srv", tool=None)
        assert "server 'srv'" in result

    async def test_unfiltered_flushes_surfacing_when_proxy_cache_disabled(self):
        """Proxy cache off but surfacing on: the unfiltered call still flushes
        surfacing (the old handler returned early and could never reach it)."""
        pm = _make_proxy_manager()
        pm._cache = None
        engine = MagicMock()
        engine.clear_cache.return_value = 4

        ctx = _make_ctx(proxy_manager=pm, surfacing_engine=engine)
        result = await stm_proxy_cache_clear(ctx=ctx)

        engine.clear_cache.assert_called_once_with()
        assert "4" in result and "surfacing-cache" in result
        assert "response-cache" not in result  # proxy cache absent, not reported


# ── stm_proxy_health ─────────────────────────────────────────────────────


class TestHealth:
    async def test_no_servers(self):
        """Empty connections returns 'No upstream servers' message."""
        pm = _make_proxy_manager()
        ctx = _make_ctx(proxy_manager=pm)
        result = await stm_proxy_health(ctx=ctx)
        assert "No upstream servers configured" in result
        assert "failed to parse" not in result

    async def test_no_obs_tools_hint_over_mcp(self, monkeypatch):
        """#613: the hidden-tools hint is NOT emitted over MCP. stm_proxy_health
        is itself gated, so it is unreachable in the flag-off state where the
        hint would apply; emitting it here would be dead + misleading. The hint
        lives on the always-available ``mms health`` CLI instead."""
        monkeypatch.delenv("MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS", raising=False)
        pm = _make_proxy_manager()
        ctx = _make_ctx(proxy_manager=pm)
        result = await stm_proxy_health(ctx=ctx)
        assert "observability tools hidden" not in result

    async def test_config_error_shown_without_upstreams(self):
        """#611: a broken config file typically manifests as zero upstreams —
        the warning must lead the 'No upstream servers' branch, naming the
        cause of the symptom."""
        pm = _make_proxy_manager()
        ctx = _make_ctx(proxy_manager=pm, proxy_config_error="Expecting value: line 1")
        result = await stm_proxy_health(ctx=ctx)
        lines = result.splitlines()
        assert lines[0] == (
            "WARNING: proxy config file present but failed to parse — "
            "running env/default config: Expecting value: line 1"
        )
        assert "No upstream servers configured" in result

    async def test_config_error_shown_with_upstreams(self):
        """The warning also leads the populated branch (env-configured
        upstreams can coexist with a broken file)."""
        pm = _make_proxy_manager()
        pm._connections["srv"] = UpstreamConnection(
            name="srv",
            config=UpstreamServerConfig(prefix="test"),
            session=AsyncMock(),
            tools=[MagicMock()],
        )
        ctx = _make_ctx(proxy_manager=pm, proxy_config_error="boom")
        result = await stm_proxy_health(ctx=ctx)
        assert result.splitlines()[0].startswith("WARNING: proxy config file present")
        assert "srv: connected" in result

    async def test_with_servers(self):
        """Reports connection status with discovered vs advertised counts —
        since #465 the discovered catalogue can exceed what the eligibility
        filter actually exposed, and operators must be able to tell a
        withheld tool from a missing one."""
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
        assert "srv: connected (2 tools discovered, 0 advertised)" in result

    async def test_startup_failed_server_rendered_disconnected(self):
        """#580: a server that failed to connect at startup is rendered
        DISCONNECTED with its connect error, instead of being absent."""
        pm = _make_proxy_manager()
        pm._connections["ok"] = UpstreamConnection(
            name="ok",
            config=UpstreamServerConfig(prefix="ok"),
            session=AsyncMock(),
            tools=[MagicMock()],
        )
        pm._failed_servers["bad"] = "ConnectionError: host unreachable"
        ctx = _make_ctx(proxy_manager=pm)
        result = await stm_proxy_health(ctx=ctx)
        assert "ok: connected (1 tools discovered, 0 advertised)" in result
        assert "bad: DISCONNECTED" in result
        assert "startup connect failed: ConnectionError: host unreachable" in result

    @staticmethod
    def _proxy_enabled_config() -> STMConfig:
        cfg = STMConfig()
        cfg.proxy.enabled = True
        return cfg

    async def test_bootstrap_block_tracker_ready(self):
        """Bootstrap section reports ready when tracker.bootstrap_status() says so."""
        tracker = MagicMock()
        tracker.bootstrap_status.return_value = {
            "path": "/tmp/stm_feedback.db",
            "exists": True,
            "initialized": True,
            "missing_tables": [],
            "error": None,
        }
        ctx = _make_ctx(feedback_tracker=tracker, config=self._proxy_enabled_config())
        result = await stm_proxy_health(ctx=ctx)
        assert "Surfacing Bootstrap" in result
        assert "feedback tracking: enabled" in result
        assert "feedback db: /tmp/stm_feedback.db" in result
        assert "feedback tables: ready" in result

    async def test_bootstrap_block_tracker_missing_tables(self):
        """Bootstrap section lists missing tables when the inspector flags them."""
        tracker = MagicMock()
        tracker.bootstrap_status.return_value = {
            "path": "/tmp/stm_feedback.db",
            "exists": True,
            "initialized": False,
            "missing_tables": ["auto_tune_adjustments"],
            "error": None,
        }
        ctx = _make_ctx(feedback_tracker=tracker, config=self._proxy_enabled_config())
        result = await stm_proxy_health(ctx=ctx)
        assert "feedback tables: missing (auto_tune_adjustments)" in result

    async def test_bootstrap_block_init_failed(self):
        """When proxy is up + feedback_enabled but tracker is None, surface runtime-init failure."""
        ctx = _make_ctx(feedback_tracker=None, config=self._proxy_enabled_config())
        result = await stm_proxy_health(ctx=ctx)
        assert "Surfacing Bootstrap" in result
        assert "runtime init failed" in result

    async def test_bootstrap_block_proxy_disabled(self):
        """When proxy.enabled=false the lifespan skips surfacing init entirely —
        a None tracker is expected, not a runtime failure. Report it as such.
        """
        # Default STMConfig has proxy.enabled=False, so _make_ctx() suffices.
        ctx = _make_ctx(feedback_tracker=None)
        result = await stm_proxy_health(ctx=ctx)
        assert "Surfacing Bootstrap" in result
        assert "feedback tracking: inactive (proxy disabled)" in result
        assert "runtime init failed" not in result

    async def test_bootstrap_block_absent_when_surfacing_disabled(self):
        """No bootstrap section when surfacing is config-disabled."""
        cfg = STMConfig()
        cfg.surfacing.enabled = False
        ctx = _make_ctx(config=cfg)
        result = await stm_proxy_health(ctx=ctx)
        assert "Surfacing Bootstrap" not in result

    async def test_circuit_breaker_reports_three_states(self):
        """#600: the breaker line renders all three states from the pure
        cb.state. An elapsed open breaker reads half-open (is_open == False) and
        must NOT be reported as 'closed (healthy)' before a probe succeeds."""
        from memtomem_stm.utils.circuit_breaker import CircuitBreaker

        def _ctx_with_breaker(cb):
            # The breaker line only renders past the "no upstream servers"
            # early-return, so give health a connected upstream to report.
            pm = _make_proxy_manager()
            pm._connections["srv"] = UpstreamConnection(
                name="srv", config=UpstreamServerConfig(prefix="t"), session=AsyncMock(), tools=[]
            )
            engine = MagicMock()
            engine._circuit_breaker = cb
            return _make_ctx(proxy_manager=pm, surfacing_engine=engine)

        # closed
        cb_closed = CircuitBreaker(max_failures=1, reset_timeout=100.0)
        result = await stm_proxy_health(ctx=_ctx_with_breaker(cb_closed))
        assert "Surfacing circuit breaker: closed (healthy)" in result

        # open — just tripped, window not elapsed
        cb_open = CircuitBreaker(max_failures=1, reset_timeout=100.0)
        cb_open.record_failure()
        assert cb_open.state == "open"
        result = await stm_proxy_health(ctx=_ctx_with_breaker(cb_open))
        assert "Surfacing circuit breaker: open (failing)" in result

        # half-open — reset window elapsed, is_open is False but no probe yet
        cb_half = CircuitBreaker(max_failures=1, reset_timeout=0.0)
        cb_half.record_failure()
        assert cb_half.state == "half-open"
        assert cb_half.is_open is False
        result = await stm_proxy_health(ctx=_ctx_with_breaker(cb_half))
        assert "Surfacing circuit breaker: half-open (probe eligible)" in result
        assert "circuit breaker: closed (healthy)" not in result

    async def test_upstream_circuit_breaker_reports_three_states(self):
        """#608: each upstream with an enabled breaker gets its own circuit
        line, rendered from the same pure-read state labels as the surfacing
        line; an open breaker also shows the time until probe eligibility."""
        from memtomem_stm.utils.circuit_breaker import CircuitBreaker

        def _ctx_with_upstream_breaker(cb):
            pm = _make_proxy_manager()
            pm._connections["srv"] = UpstreamConnection(
                name="srv",
                config=UpstreamServerConfig(prefix="t"),
                session=AsyncMock(),
                tools=[],
                breaker=cb,
            )
            return _make_ctx(proxy_manager=pm)

        cb_closed = CircuitBreaker(max_failures=1, reset_timeout=100.0)
        result = await stm_proxy_health(ctx=_ctx_with_upstream_breaker(cb_closed))
        assert "      circuit breaker: closed (healthy)" in result

        cb_open = CircuitBreaker(max_failures=1, reset_timeout=100.0)
        cb_open.record_failure()
        result = await stm_proxy_health(ctx=_ctx_with_upstream_breaker(cb_open))
        assert "      circuit breaker: open (failing), retry in ~" in result

        cb_half = CircuitBreaker(max_failures=1, reset_timeout=0.0)
        cb_half.record_failure()
        assert cb_half.state == "half-open"
        result = await stm_proxy_health(ctx=_ctx_with_upstream_breaker(cb_half))
        assert "      circuit breaker: half-open (probe eligible)" in result
        assert "retry in ~" not in result  # no countdown once probe-eligible

    async def test_upstream_circuit_line_absent_when_disabled_or_failed(self):
        """#608: no circuit line for a breaker-disabled upstream
        (circuit_max_failures=0 → breaker None) or a startup-failed server
        (no connection, no breaker) — absence must not read as healthy."""
        pm = _make_proxy_manager()
        pm._connections["nobreaker"] = UpstreamConnection(
            name="nobreaker",
            config=UpstreamServerConfig(prefix="t"),
            session=AsyncMock(),
            tools=[],
        )
        pm._failed_servers["bad"] = "ConnectionError: host unreachable"
        ctx = _make_ctx(proxy_manager=pm)
        result = await stm_proxy_health(ctx=ctx)
        assert "nobreaker: connected" in result
        assert "bad: DISCONNECTED" in result
        assert "circuit breaker:" not in result


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

    async def test_batched_via_engine(self):
        """Batched ratings dispatch to engine.handle_feedback_batch."""
        mock_engine = AsyncMock()
        mock_engine.handle_feedback_batch.return_value = "Feedback recorded: 3/3 entries"

        ctx = _make_ctx(surfacing_engine=mock_engine)
        ratings = [
            {"memory_id": "m1", "rating": "helpful"},
            {"memory_id": "m2", "rating": "not_relevant"},
            {"memory_id": "m3", "rating": "already_known"},
        ]
        result = await stm_surfacing_feedback(surfacing_id="s1", ratings=ratings, ctx=ctx)
        assert result == "Feedback recorded: 3/3 entries"
        mock_engine.handle_feedback_batch.assert_awaited_once_with("s1", ratings)
        # Legacy single-rating path must not also fire.
        mock_engine.handle_feedback.assert_not_awaited()

    async def test_mixing_legacy_and_batched_fields_errors(self):
        """Caller must pick one shape — never both."""
        mock_engine = AsyncMock()
        ctx = _make_ctx(surfacing_engine=mock_engine)
        result = await stm_surfacing_feedback(
            surfacing_id="s1",
            rating="helpful",
            ratings=[{"memory_id": "m1", "rating": "helpful"}],
            ctx=ctx,
        )
        assert result.startswith("Error:")
        assert "cannot mix" in result
        # Neither dispatch fires when the request is rejected up-front.
        mock_engine.handle_feedback.assert_not_awaited()
        mock_engine.handle_feedback_batch.assert_not_awaited()

    async def test_mixing_legacy_memory_id_and_batched_fields_errors(self):
        """`memory_id` without `rating` still counts as the legacy shape."""
        mock_engine = AsyncMock()
        ctx = _make_ctx(surfacing_engine=mock_engine)
        result = await stm_surfacing_feedback(
            surfacing_id="s1",
            memory_id="m1",
            ratings=[{"memory_id": "m1", "rating": "helpful"}],
            ctx=ctx,
        )
        assert result.startswith("Error:")
        assert "cannot mix" in result

    async def test_no_rating_and_no_ratings_errors(self):
        """At least one of `rating` / `ratings` must be supplied."""
        ctx = _make_ctx(surfacing_engine=AsyncMock())
        result = await stm_surfacing_feedback(surfacing_id="s1", ctx=ctx)
        assert result.startswith("Error:")
        assert "required" in result

    async def test_legacy_memory_id_only_rejected_before_engine_dispatch(self):
        """`memory_id` without `rating` is the legacy shape but lacks the
        mandatory rating — reject up-front so `None` never reaches the
        engine's `rating: str` parameter."""
        mock_engine = AsyncMock()
        ctx = _make_ctx(surfacing_engine=mock_engine)
        result = await stm_surfacing_feedback(surfacing_id="s1", memory_id="m1", ctx=ctx)
        assert result.startswith("Error:")
        assert "rating" in result and "required" in result
        mock_engine.handle_feedback.assert_not_awaited()

    async def test_legacy_memory_id_only_rejected_before_tracker_dispatch(self):
        """Same guard on the engine-absent path — never reaches the tracker's
        `rating: str` parameter."""
        mock_tracker = MagicMock()
        ctx = _make_ctx(surfacing_engine=None, feedback_tracker=mock_tracker)
        result = await stm_surfacing_feedback(surfacing_id="s1", memory_id="m1", ctx=ctx)
        assert result.startswith("Error:")
        assert "rating" in result and "required" in result
        mock_tracker.record_feedback.assert_not_called()

    async def test_batched_fallback_to_tracker(self):
        """Engine-absent path fans out via the tracker without boost/invalidation."""
        mock_tracker = MagicMock()
        mock_tracker.record_feedback.side_effect = [
            "Feedback recorded: helpful",
            "Feedback recorded: not_relevant",
        ]

        ctx = _make_ctx(surfacing_engine=None, feedback_tracker=mock_tracker)
        result = await stm_surfacing_feedback(
            surfacing_id="s1",
            ratings=[
                {"memory_id": "m1", "rating": "helpful"},
                {"memory_id": "m2", "rating": "not_relevant"},
            ],
            ctx=ctx,
        )
        assert "2/2 entries" in result
        assert mock_tracker.record_feedback.call_count == 2

    async def test_batched_fallback_shape_error_short_circuits(self):
        """Malformed entry rejects the whole call without partial writes."""
        mock_tracker = MagicMock()
        ctx = _make_ctx(surfacing_engine=None, feedback_tracker=mock_tracker)
        result = await stm_surfacing_feedback(
            surfacing_id="s1",
            ratings=[{"memory_id": "m1"}],  # missing rating
            ctx=ctx,
        )
        assert result.startswith("Error:")
        mock_tracker.record_feedback.assert_not_called()

    async def test_batched_fallback_malformed_after_valid_persists_nothing(self):
        """Reviewer pin: a malformed entry *after* a valid one must not
        leave the valid entry's row committed. The engine path does
        fail-fast via its own two-pass parse; an earlier inline-validate
        loop in the tracker-only fallback let a partial prefix persist
        when validation failed mid-batch.
        """
        mock_tracker = MagicMock()
        ctx = _make_ctx(surfacing_engine=None, feedback_tracker=mock_tracker)
        result = await stm_surfacing_feedback(
            surfacing_id="s1",
            ratings=[
                {"memory_id": "m1", "rating": "helpful"},  # valid
                {"memory_id": "m2"},  # missing rating — must reject the whole batch
            ],
            ctx=ctx,
        )
        assert result.startswith("Error:")
        # CRITICAL: the valid first entry must NOT have been recorded.
        mock_tracker.record_feedback.assert_not_called()


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
                {
                    "tool": "t",
                    "events": 7,
                    "avg_memory_count": 3.0,
                    "feedback_count": 5,
                    "not_relevant_count": 2,
                },
                {
                    "tool": "u",
                    "events": 3,
                    "avg_memory_count": 2.0,
                    "feedback_count": 0,
                    "not_relevant_count": 0,
                },
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
        # No score_distribution key in this legacy-shaped dict — the
        # zero-variance tripwire must degrade to silence, not crash.
        assert "zero score variance" not in result

    async def test_verdict_is_the_first_line_after_the_header(self):
        """#363: the operator's first question is "is surfacing healthy?", so
        the verdict must be the top line — not merely present somewhere in the
        50+ line output. Position is pinned, not just the substring."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = self._stats_with_scores(
            {"count": 0, "min": None, "max": None}
        )
        mock_engine = MagicMock()
        mock_engine.get_min_score_snapshot.return_value = {
            "default": 0.030,
            "auto_tune_enabled": False,
            "auto_tune_min_samples": 20,
            "adjusted": {},
            "overrides": {},
        }
        mock_engine.observability.snapshot.return_value = {
            "any_call": True,
            "skip_reasons": {"__total__": {"circuit_open": 40, "gate_cooldown": 900}},
            "outcomes": {"__total__": {"surfaced_cache_miss": 5}},
            "cache": {"hit": 0, "miss": 5},
        }
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=mock_engine)
        result = await stm_surfacing_stats(ctx=ctx)

        lines = result.splitlines()
        assert lines[0] == "Surfacing Stats"
        assert lines[1] == "==============="
        assert lines[2].startswith("Verdict (this process, since start): FAULTY — ")
        assert "40 of 45 LTM attempts faulted (88.9%)" in lines[2]
        assert "top fault: circuit_open 40" in lines[2]
        # The 900 gate_cooldown skips still render in the Healthy section —
        # excluded from the verdict denominator, not from the output.
        assert "gate_cooldown: 900" in result

    async def test_verdict_absent_when_surfacing_never_invoked(self):
        """Zero-traffic output stays byte-for-byte: a wired-but-unused engine
        renders no verdict, same as the existing observability sections."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = self._stats_with_scores(
            {"count": 0, "min": None, "max": None}
        )
        mock_engine = MagicMock()
        mock_engine.get_min_score_snapshot.return_value = {
            "default": 0.030,
            "auto_tune_enabled": False,
            "auto_tune_min_samples": 20,
            "adjusted": {},
            "overrides": {},
        }
        mock_engine.observability.snapshot.return_value = {
            "any_call": False,
            "skip_reasons": {},
            "outcomes": {},
            "cache": {},
        }
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=mock_engine)
        result = await stm_surfacing_stats(ctx=ctx)
        assert "Verdict" not in result

        # Same for an engine with observability disabled entirely.
        mock_engine.observability = None
        result_no_obs = await stm_surfacing_stats(ctx=ctx)
        assert "Verdict" not in result_no_obs

    async def test_verdict_snapshot_taken_once_per_call(self):
        """The verdict and the skip sections must describe the same counters.
        Two ``snapshot()`` calls could straddle a concurrent surfacing and
        render a verdict the sections below contradict."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = self._stats_with_scores(
            {"count": 0, "min": None, "max": None}
        )
        mock_engine = MagicMock()
        mock_engine.get_min_score_snapshot.return_value = {
            "default": 0.030,
            "auto_tune_enabled": False,
            "auto_tune_min_samples": 20,
            "adjusted": {},
            "overrides": {},
        }
        mock_engine.observability.snapshot.return_value = {
            "any_call": True,
            "skip_reasons": {"__total__": {"ltm_unavailable": 2}},
            "outcomes": {"__total__": {"surfaced_cache_hit": 30}},
            "cache": {"hit": 30, "miss": 0},
        }
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=mock_engine)
        result = await stm_surfacing_stats(ctx=ctx)
        assert mock_engine.observability.snapshot.call_count == 1
        assert "HEALTHY" in result
        assert "Fault skips" in result

    @staticmethod
    def _stats_with_scores(score_distribution: dict) -> dict:
        """Minimal stats shape for exercising the flat-score tripwire."""
        return {
            "events_total": 21,
            "distinct_tools": 1,
            "date_range": {"first": 1_700_000_000.0, "last": 1_700_000_999.0},
            "per_tool_breakdown": [],
            "rating_distribution": {},
            "total_feedback": 0,
            "recent": [],
            "score_distribution": score_distribution,
        }

    async def test_flat_score_warning_renders_when_zero_variance(self):
        """#560 step 3: n>=threshold identical scores → explicit warning,
        with the sample count and the constant value in the text."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = self._stats_with_scores(
            {"count": 37, "min": 0.03, "max": 0.03}
        )
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=None)
        result = await stm_surfacing_stats(ctx=ctx)
        assert "WARNING: zero score variance" in result
        assert "all 37 recorded scores" in result
        assert "equal 0.0300" in result

    async def test_flat_score_warning_absent_when_scores_vary(self):
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = self._stats_with_scores(
            {"count": 37, "min": 0.0164, "max": 0.0325}
        )
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=None)
        result = await stm_surfacing_stats(ctx=ctx)
        assert "zero score variance" not in result

    async def test_flat_score_warning_absent_below_sample_threshold(self):
        """A handful of identical scores is expected noise — the tripwire
        stays quiet until _FLAT_SCORE_WARNING_MIN_SAMPLES."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = self._stats_with_scores(
            {"count": 9, "min": 0.03, "max": 0.03}
        )
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=None)
        result = await stm_surfacing_stats(ctx=ctx)
        assert "zero score variance" not in result

        # Boundary: exactly at the threshold the warning fires (>=).
        mock_tracker.get_stats.return_value = self._stats_with_scores(
            {"count": 10, "min": 0.03, "max": 0.03}
        )
        result = await stm_surfacing_stats(ctx=ctx)
        assert "WARNING: zero score variance" in result

    async def test_min_score_block_when_engine_present(self):
        """With a surfacing engine, output includes the min_score snapshot
        and per-tool auto-tune readiness annotations."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "events_total": 2,
            "distinct_tools": 2,
            "date_range": {"first": 1_700_000_000.0, "last": 1_700_000_999.0},
            "per_tool_breakdown": [
                {
                    "tool": "ready_tool",
                    "events": 25,
                    "avg_memory_count": 2.0,
                    "feedback_count": 25,
                    "not_relevant_count": 5,
                },
                {
                    "tool": "tuned_tool",
                    "events": 30,
                    "avg_memory_count": 2.0,
                    "feedback_count": 30,
                    "not_relevant_count": 22,
                },
                {
                    "tool": "cold_tool",
                    "events": 3,
                    "avg_memory_count": 1.0,
                    "feedback_count": 4,
                    "not_relevant_count": 1,
                },
            ],
            "rating_distribution": {"helpful": 25, "not_relevant": 28},
            "total_feedback": 59,
            "recent": [],
        }
        mock_tracker.store.get_per_tool_feedback_counts.return_value = {
            "ready_tool": 25,
            "tuned_tool": 30,
            "cold_tool": 4,
        }
        mock_engine = MagicMock()
        mock_engine.observability = None
        mock_engine.get_min_score_snapshot.return_value = {
            "default": 0.030,
            "auto_tune_enabled": True,
            "auto_tune_min_samples": 20,
            "adjusted": {"tuned_tool": 0.038},
            "overrides": {},
        }
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=mock_engine)
        result = await stm_surfacing_stats(ctx=ctx)

        assert "Min score:       0.030 (auto-tune on, min 20 samples)" in result
        assert "Per-tool adjustments:" in result
        assert "tuned_tool: 0.038" in result
        # Auto-tune readiness annotations:
        assert "tuned_tool: 30 events" in result and "auto-tuned" in result
        assert "ready_tool: 25 events" in result and "auto-tune ready" in result
        # cold_tool has only 4 of its own samples, but the global pool here
        # has 59 (25 + 30 + 4) — past the 20-sample threshold — so the
        # tuner's cold-start fallback would fire on the next surfacing.
        # Readiness label reflects that, not the per-tool count alone.
        cold_row = next(
            line for line in result.splitlines() if "cold_tool:" in line and "events" in line
        )
        assert "auto-tune ready" in cold_row
        assert "need" not in cold_row
        # Negative ratio rendered where feedback exists.
        assert "negative 73.3%" in result  # tuned: 22/30
        assert "negative 20.0%" in result  # ready: 5/25
        # Snapshot without a score_scale entry (older engine mock) renders
        # the honest fallback line rather than crashing or omitting it.
        assert "Score scale:     unknown (core did not report one)" in result

    async def test_score_scale_line_renders_core_reported_scale(self):
        """#1781: the snapshot's last core-reported scale (and reranker
        model ID, when present) lands in the stats output."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "events_total": 1,
            "distinct_tools": 1,
            "date_range": {"first": 1_700_000_000.0, "last": 1_700_000_999.0},
            "per_tool_breakdown": [],
            "rating_distribution": {},
            "total_feedback": 0,
            "recent": [],
        }
        mock_tracker.store.get_per_tool_feedback_counts.return_value = {}
        mock_engine = MagicMock()
        mock_engine.observability = None
        snapshot = {
            "default": 0.030,
            "auto_tune_enabled": False,
            "auto_tune_min_samples": 20,
            "adjusted": {},
            "overrides": {},
            "score_scale": {"last_reported": "rerank", "reranker": "fake-rr"},
        }
        mock_engine.get_min_score_snapshot.return_value = snapshot
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=mock_engine)

        result = await stm_surfacing_stats(ctx=ctx)
        assert "Score scale:     rerank (core-reported; reranker: fake-rr)" in result

        snapshot["score_scale"] = {"last_reported": "rrf", "reranker": None}
        result = await stm_surfacing_stats(ctx=ctx)
        assert "Score scale:     rrf (core-reported)" in result

    async def test_score_scale_line_annotates_suspended_filter(self):
        """Scale gate: when the engine snapshot says the last-reported scale
        suspends the filter, the stats line says so."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "events_total": 1,
            "distinct_tools": 1,
            "date_range": {"first": 1_700_000_000.0, "last": 1_700_000_999.0},
            "per_tool_breakdown": [],
            "rating_distribution": {},
            "total_feedback": 0,
            "recent": [],
        }
        mock_tracker.store.get_per_tool_feedback_counts.return_value = {}
        mock_engine = MagicMock()
        mock_engine.observability = None
        mock_engine.get_min_score_snapshot.return_value = {
            "default": 0.030,
            "auto_tune_enabled": False,
            "auto_tune_min_samples": 20,
            "adjusted": {},
            "overrides": {},
            "score_scale": {
                "last_reported": "rerank",
                "reranker": "fake-rr",
                "gate_enabled": True,
                "filter_suspended": True,
            },
        }
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=mock_engine)

        result = await stm_surfacing_stats(ctx=ctx)
        assert (
            "Score scale:     rerank (core-reported; reranker: fake-rr; "
            "min_score filter suspended for unpinned tools)" in result
        )

    async def test_score_scale_distribution_line_rendered(self):
        """#1781: get_stats' per-scale event counts render as a `Score scales:`
        line (engine-independent — it comes from the events store, not the
        min_score snapshot)."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "events_total": 3,
            "distinct_tools": 1,
            "date_range": {"first": 1_700_000_000.0, "last": 1_700_000_999.0},
            "per_tool_breakdown": [],
            "rating_distribution": {},
            "total_feedback": 0,
            "recent": [],
            "score_scale_distribution": {"rerank": 2, "unknown": 1},
        }
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=None)
        result = await stm_surfacing_stats(ctx=ctx)
        assert "Score scales:    rerank 2, unknown 1" in result

    async def test_need_more_uses_global_gap_when_pool_also_below_threshold(self):
        """When *both* the tool's own and the global pool are below
        ``min_samples``, the "need N more" message surfaces both numbers
        side-by-side (#361). The global gap is the one the tuner is
        actually waiting on (cold-start fallback), but the per-tool gap
        is what the row's own feedback would need without the fallback;
        operators previously read the single global figure as their own
        tool's shortfall, so both are now rendered when they diverge."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "events_total": 1,
            "distinct_tools": 1,
            "date_range": {"first": 1_700_000_000.0, "last": 1_700_000_999.0},
            "per_tool_breakdown": [
                {
                    "tool": "lonely_tool",
                    "events": 3,
                    "avg_memory_count": 1.0,
                    "feedback_count": 2,
                    "not_relevant_count": 1,
                },
            ],
            "rating_distribution": {"helpful": 1, "not_relevant": 1},
            "total_feedback": 2,
            "recent": [],
        }
        # Per-tool 2 + one other tool 5 → global 7, both well below 20.
        mock_tracker.store.get_per_tool_feedback_counts.return_value = {
            "lonely_tool": 2,
            "other_tool": 5,
        }
        mock_engine = MagicMock()
        mock_engine.observability = None
        mock_engine.get_min_score_snapshot.return_value = {
            "default": 0.030,
            "auto_tune_enabled": True,
            "auto_tune_min_samples": 20,
            "adjusted": {},
            "overrides": {},
        }
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=mock_engine)
        result = await stm_surfacing_stats(ctx=ctx)

        row = next(
            line for line in result.splitlines() if "lonely_tool:" in line and "events" in line
        )
        # Both gaps render side-by-side: global pool (20 - 7 = 13) is the
        # binding gate, but the per-tool figure (20 - 2 = 18) also surfaces
        # so the global number isn't misread as the tool's own shortfall.
        assert "need 13 more (global pool) or 18 more for this tool" in row
        # The legacy single-number form must NOT appear when the gaps diverge
        # (it would still parse as a substring of the dual form if we didn't
        # explicitly check the trailing context).
        assert "need 13 more for auto-tune" not in row

    async def test_need_more_renders_single_number_when_one_tool_owns_pool(self):
        """When a single tool owns the entire feedback pool the per-tool
        and global gaps coincide, so the legacy single-number label is
        preserved to avoid redundant `N more (global pool) or N more for
        this tool` output. Pins the equal-gap branch of the #361 split."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "events_total": 1,
            "distinct_tools": 1,
            "date_range": {"first": 1_700_000_000.0, "last": 1_700_000_999.0},
            "per_tool_breakdown": [
                {
                    "tool": "solo_tool",
                    "events": 4,
                    "avg_memory_count": 1.0,
                    "feedback_count": 5,
                    "not_relevant_count": 1,
                },
            ],
            "rating_distribution": {"helpful": 4, "not_relevant": 1},
            "total_feedback": 5,
            "recent": [],
        }
        # Single tool owns the whole pool: per-tool 5 == global 5, both
        # below the 20-sample threshold.
        mock_tracker.store.get_per_tool_feedback_counts.return_value = {"solo_tool": 5}
        mock_engine = MagicMock()
        mock_engine.observability = None
        mock_engine.get_min_score_snapshot.return_value = {
            "default": 0.030,
            "auto_tune_enabled": True,
            "auto_tune_min_samples": 20,
            "adjusted": {},
            "overrides": {},
        }
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=mock_engine)
        result = await stm_surfacing_stats(ctx=ctx)

        row = next(
            line for line in result.splitlines() if "solo_tool:" in line and "events" in line
        )
        assert "need 15 more for auto-tune" in row
        # No dual-figure rendering when the gaps coincide.
        assert "(global pool)" not in row
        assert "for this tool" not in row

    async def test_cold_start_tuned_tool_reported_as_tuned(self):
        """AutoTuner's cold-start fallback (feedback.py::maybe_adjust) can
        tune a tool from the global feedback pool even when the tool's own
        feedback count is below ``min_samples``. Such a tool appears in
        ``adjusted`` despite low per-tool feedback — the formatter must
        report it as ``auto-tuned``, not ``need N more for auto-tune``."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "events_total": 1,
            "distinct_tools": 1,
            "date_range": {"first": 1_700_000_000.0, "last": 1_700_000_999.0},
            "per_tool_breakdown": [
                {
                    "tool": "cold_but_tuned",
                    "events": 4,
                    "avg_memory_count": 1.0,
                    "feedback_count": 3,  # below the 20-sample threshold
                    "not_relevant_count": 0,
                },
            ],
            "rating_distribution": {"helpful": 3},
            "total_feedback": 3,
            "recent": [],
        }
        mock_tracker.store.get_per_tool_feedback_counts.return_value = {"cold_but_tuned": 3}
        mock_engine = MagicMock()
        mock_engine.observability = None
        mock_engine.get_min_score_snapshot.return_value = {
            "default": 0.030,
            "auto_tune_enabled": True,
            "auto_tune_min_samples": 20,
            # AutoTuner has already adjusted this tool from the global pool.
            "adjusted": {"cold_but_tuned": 0.026},
            "overrides": {},
        }
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=mock_engine)
        result = await stm_surfacing_stats(ctx=ctx)

        row = next(
            line for line in result.splitlines() if "cold_but_tuned:" in line and "events" in line
        )
        assert "auto-tuned" in row
        assert "need" not in row
        assert "auto-tune ready" not in row

    async def test_readiness_labels_suppressed_for_overridden_tools(self):
        """Tools pinned via context_tools.<tool>.min_score bypass the tuner
        entirely (engine.py:458-460). Stats output must show the pinned value
        and suppress readiness labels for them, while still reporting
        readiness for un-overridden siblings."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "events_total": 2,
            "distinct_tools": 2,
            "date_range": {"first": 1_700_000_000.0, "last": 1_700_000_999.0},
            "per_tool_breakdown": [
                {
                    "tool": "pinned_tool",
                    "events": 50,
                    "avg_memory_count": 2.0,
                    "feedback_count": 40,
                    "not_relevant_count": 30,
                },
                {
                    "tool": "tunable_tool",
                    "events": 25,
                    "avg_memory_count": 2.0,
                    "feedback_count": 25,
                    "not_relevant_count": 5,
                },
            ],
            "rating_distribution": {"helpful": 30, "not_relevant": 35},
            "total_feedback": 65,
            "recent": [],
        }
        mock_tracker.store.get_per_tool_feedback_counts.return_value = {
            "pinned_tool": 40,
            "tunable_tool": 25,
        }
        mock_engine = MagicMock()
        mock_engine.observability = None
        mock_engine.get_min_score_snapshot.return_value = {
            "default": 0.030,
            "auto_tune_enabled": True,
            "auto_tune_min_samples": 20,
            "adjusted": {},
            "overrides": {"pinned_tool": 0.045},
        }
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=mock_engine)
        result = await stm_surfacing_stats(ctx=ctx)

        # Pinned override listed in the Min score block:
        assert "Per-tool pinned (bypass auto-tune):" in result
        assert "pinned_tool: 0.045" in result
        # Filter to "By tool:" breakdown rows (they include "events"), not the
        # Min-score-block sublist row that also mentions pinned_tool.
        pinned_row = next(
            line for line in result.splitlines() if "pinned_tool:" in line and "events" in line
        )
        assert "pinned 0.045" in pinned_row
        assert "auto-tuned" not in pinned_row
        assert "auto-tune ready" not in pinned_row
        assert "for auto-tune" not in pinned_row
        # The un-overridden sibling still gets its readiness label:
        tunable_row = next(
            line for line in result.splitlines() if "tunable_tool:" in line and "events" in line
        )
        assert "auto-tune ready" in tunable_row

    async def test_readiness_labels_suppressed_when_auto_tune_off(self):
        """With auto_tune_enabled=False, the per-tool readiness labels must
        NOT render — otherwise output contradicts its own "auto-tune off"
        header for configs that have disabled tuning."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "events_total": 1,
            "distinct_tools": 1,
            "date_range": {"first": 1_700_000_000.0, "last": 1_700_000_999.0},
            "per_tool_breakdown": [
                {
                    "tool": "ready_tool",
                    "events": 25,
                    "avg_memory_count": 2.0,
                    "feedback_count": 25,
                    "not_relevant_count": 5,
                },
                {
                    "tool": "cold_tool",
                    "events": 3,
                    "avg_memory_count": 1.0,
                    "feedback_count": 4,
                    "not_relevant_count": 1,
                },
            ],
            "rating_distribution": {"helpful": 20, "not_relevant": 9},
            "total_feedback": 29,
            "recent": [],
        }
        mock_tracker.store.get_per_tool_feedback_counts.return_value = {
            "ready_tool": 25,
            "cold_tool": 4,
        }
        mock_engine = MagicMock()
        mock_engine.observability = None
        mock_engine.get_min_score_snapshot.return_value = {
            "default": 0.030,
            "auto_tune_enabled": False,
            "auto_tune_min_samples": 20,
            "adjusted": {},
            "overrides": {},
        }
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=mock_engine)
        result = await stm_surfacing_stats(ctx=ctx)

        assert "Min score:       0.030 (auto-tune off, min 20 samples)" in result
        # Negative ratio still rendered — orthogonal to auto-tune state.
        assert "negative 20.0%" in result
        # No readiness labels of any flavor:
        assert "auto-tuned" not in result
        assert "auto-tune ready" not in result
        assert "for auto-tune" not in result

    async def test_readiness_uses_unfiltered_count_under_since_window(self):
        """``since=`` filters the stats rows but AutoTuner sees the full
        history; readiness must reflect unfiltered counts or it will
        under-report eligibility for tools with sparse recent activity."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "events_total": 1,
            "distinct_tools": 1,
            "date_range": {"first": 1_700_000_000.0, "last": 1_700_000_999.0},
            "per_tool_breakdown": [
                {
                    "tool": "historic_tool",
                    "events": 2,
                    "avg_memory_count": 1.0,
                    # Only 3 feedback rows fell in the since window — the row
                    # count would otherwise drive a "need 17 more" label.
                    "feedback_count": 3,
                    "not_relevant_count": 1,
                },
            ],
            "rating_distribution": {"helpful": 2, "not_relevant": 1},
            "total_feedback": 3,
            "recent": [],
        }
        # The full history has 50 samples — well past the 20-sample threshold —
        # but not yet adjusted (ratios inside the tuner's no-op region:
        # negative at or below 0.6 and helpful at or below 0.8).
        mock_tracker.store.get_per_tool_feedback_counts.return_value = {"historic_tool": 50}
        mock_engine = MagicMock()
        mock_engine.observability = None
        mock_engine.get_min_score_snapshot.return_value = {
            "default": 0.030,
            "auto_tune_enabled": True,
            "auto_tune_min_samples": 20,
            "adjusted": {},
            "overrides": {},
        }
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=mock_engine)
        result = await stm_surfacing_stats(since="2026-04-01T00:00:00", ctx=ctx)

        row = next(
            line for line in result.splitlines() if "historic_tool:" in line and "events" in line
        )
        assert "auto-tune ready" in row
        # Windowed feedback count is still reported for the operator's audit,
        # but the readiness gate honors the unfiltered total.
        assert "feedback 3" in row
        assert "need" not in row

    async def test_min_score_lists_honor_tool_filter(self):
        """When ``tool=`` is set, the per-tool adjustment / pinned sublists
        must restrict to that tool — otherwise they contradict the trailing
        ``(filtered by tool: ...)`` marker by leaking unrelated thresholds."""
        mock_tracker = MagicMock()
        mock_tracker.get_stats.return_value = {
            "events_total": 1,
            "distinct_tools": 1,
            "date_range": {"first": 1_700_000_000.0, "last": 1_700_000_999.0},
            "per_tool_breakdown": [
                {
                    "tool": "alpha",
                    "events": 10,
                    "avg_memory_count": 1.0,
                    "feedback_count": 10,
                    "not_relevant_count": 0,
                },
            ],
            "rating_distribution": {"helpful": 10},
            "total_feedback": 10,
            "recent": [],
        }
        mock_tracker.store.get_per_tool_feedback_counts.return_value = {
            "alpha": 10,
            "beta": 30,
            "gamma": 30,
        }
        mock_engine = MagicMock()
        mock_engine.observability = None
        mock_engine.get_min_score_snapshot.return_value = {
            "default": 0.030,
            "auto_tune_enabled": True,
            "auto_tune_min_samples": 20,
            "adjusted": {"beta": 0.038},
            "overrides": {"gamma": 0.045},
        }
        ctx = _make_ctx(feedback_tracker=mock_tracker, surfacing_engine=mock_engine)
        result = await stm_surfacing_stats(tool="alpha", ctx=ctx)

        # Neither beta (adjusted) nor gamma (pinned) belong in an alpha-filtered
        # report — the sublists must drop them entirely.
        assert "beta:" not in result
        assert "gamma:" not in result
        # Without any alpha-specific adjustment/override, the sublists collapse.
        assert "Per-tool adjustments:" not in result
        assert "Per-tool pinned" not in result
        # Filter marker still present.
        assert "(filtered by tool: alpha)" in result

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

    @staticmethod
    def _tracker_with_degradation(tmp_path, server, tool):
        from memtomem_stm.proxy.metrics import CallMetrics
        from memtomem_stm.proxy.metrics_store import MetricsStore

        store = MetricsStore(tmp_path / "m.db")
        store.initialize()
        store.record(
            CallMetrics(
                server=server,
                tool=tool,
                original_chars=100,
                compressed_chars=100,
                compression_strategy="progressive→passthrough_on_error",
            )
        )
        return TokenTracker(metrics_store=store), store

    async def test_degradation_section_from_metrics_store(self, tmp_path):
        """Primary-store passthrough-on-error degradations recorded in the
        metrics store surface in stm_progressive_stats alongside read stats."""
        tracker, store = self._tracker_with_degradation(tmp_path, "gh", "search")
        try:
            mock_tracker = MagicMock()
            mock_tracker.get_stats.return_value = {
                "total_reads": 1,
                "total_responses": 1,
                "follow_up_rate": 0.0,
                "avg_chars_served": 9000.0,
                "avg_total_chars": 9000.0,
                "avg_coverage": 1.0,
                "by_tool": {},
            }
            ctx = _make_ctx(tracker=tracker, progressive_reads_tracker=mock_tracker)
            result = await stm_progressive_stats(ctx=ctx)

            assert "Progressive Reads Stats" in result
            assert "Primary-store degradation (last 24h): 1 passthrough-on-error" in result
            assert "gh/search: 1" in result
        finally:
            store.close()

    async def test_degradation_visible_when_reads_tracking_disabled(self, tmp_path):
        """A failing primary store must not go silent: the degradation count is
        reported even when the reads tracker itself is disabled."""
        tracker, store = self._tracker_with_degradation(tmp_path, "fs", "read")
        try:
            ctx = _make_ctx(tracker=tracker, progressive_reads_tracker=None)
            result = await stm_progressive_stats(ctx=ctx)

            assert "reads tracking disabled" in result
            assert "Primary-store degradation (last 24h): 1 passthrough-on-error" in result
            assert "fs/read: 1" in result
        finally:
            store.close()


@pytest.mark.parametrize("surrogate", ["\ud800", "\udbff", "\udc00", "\udfff"])
async def test_stats_tool_filters_return_serializable_identifier_errors(surrogate):
    """Refused filters must not be echoed after a store returns empty stats."""
    compression = MagicMock()
    surfacing = MagicMock()
    progressive = MagicMock()
    ctx = _make_ctx(
        compression_feedback_tracker=compression,
        feedback_tracker=surfacing,
        progressive_reads_tracker=progressive,
    )

    for handler in (
        stm_compression_stats,
        stm_surfacing_stats,
        stm_progressive_stats,
    ):
        result = await handler(tool=f"tool{surrogate}", ctx=ctx)
        assert result == "Error: tool must be a valid UTF-8 identifier"
        assert surrogate not in result
        result.encode("utf-8")

    compression.get_stats.assert_not_called()
    surfacing.get_stats.assert_not_called()
    progressive.get_stats.assert_not_called()


@pytest.mark.parametrize("surrogate", ["\ud800", "\udbff", "\udc00", "\udfff"])
async def test_surfacing_stats_invalid_since_repr_is_serializable_and_distinct(surrogate):
    tracker = MagicMock()
    raw_result = await stm_surfacing_stats(
        since=f"invalid{surrogate}",
        ctx=_make_ctx(feedback_tracker=tracker),
    )
    literal_result = await stm_surfacing_stats(
        since=f"invalid\\u{ord(surrogate):04x}",
        ctx=_make_ctx(feedback_tracker=tracker),
    )
    assert surrogate not in raw_result
    assert "invalid 'since' timestamp" in raw_result
    raw_result.encode("utf-8")
    literal_result.encode("utf-8")
    assert raw_result != literal_result
    tracker.get_stats.assert_not_called()


# ── stm_selection_stats ───────────────────────────────────────────────────


def _selection_log(tmp_path):
    from memtomem_stm.proxy.selection_log import SelectionTelemetryLog

    log = SelectionTelemetryLog(tmp_path / "sel.jsonl")
    log.initialize()
    return log


def _ctx_with_selection(tmp_path, *, log):
    cfg = ProxyConfig(config_path=tmp_path / "p.json", upstream_servers={})
    pm = ProxyManager(cfg, TokenTracker(), selection_log=log)
    return _make_ctx(proxy_manager=pm)


class TestSelectionStats:
    async def test_proxy_not_enabled(self):
        app = STMContext(
            config=STMConfig(),
            proxy_manager=None,  # type: ignore[arg-type]
            tracker=TokenTracker(),
            surfacing_engine=None,
            feedback_tracker=None,
            compression_feedback_tracker=None,
            progressive_reads_tracker=None,
        )
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))
        assert await stm_selection_stats(ctx=ctx) == "Proxy is not enabled."

    async def test_telemetry_disabled(self, tmp_path):
        ctx = _ctx_with_selection(tmp_path, log=None)
        result = await stm_selection_stats(ctx=ctx)
        assert "Selection telemetry is disabled" in result

    async def test_enabled_but_empty(self, tmp_path):
        ctx = _ctx_with_selection(tmp_path, log=_selection_log(tmp_path))
        result = await stm_selection_stats(ctx=ctx)
        assert "No selection telemetry recorded yet" in result

    async def test_populated_renders_all_sections(self, tmp_path):
        log = _selection_log(tmp_path)
        sid = log.log_selection(
            server="gh",
            selected_tool="gh__a",
            candidate_tools=["gh__a", "gh__b"],
            arguments={"q": "x"},
            trace_id=None,
            reject_reasons={"gh__b": "config_hidden"},
        )
        log.log_execution(
            selection_id=sid,
            trace_id=None,
            server="gh",
            selected_tool="gh__a",
            ok=True,
            latency_ms=12.0,
            cache_hit=True,
        )
        ctx = _ctx_with_selection(tmp_path, log=log)
        result = await stm_selection_stats(ctx=ctx)

        assert "Selection Stats" in result
        assert "Live counters (this process):" in result
        assert "events_written: 2" in result
        assert "Selections by ranker version:" in result
        assert "v0-passthrough: 1" in result
        assert "Selections by server:" in result
        assert "Execution outcomes:" in result
        assert "ok: 1" in result
        assert "cache hit/miss: 1 / 0" in result
        assert "Reject reasons (withheld from the advertisement):" in result
        assert "config_hidden: 1" in result

    async def test_sampled_out_still_renders_live_counters(self, tmp_path):
        from memtomem_stm.proxy.selection_log import SelectionTelemetryLog

        # sample_rate 0 → nothing persisted, but the live drop counter is real
        # signal the operator should still see (not "no data").
        log = SelectionTelemetryLog(tmp_path / "sel.jsonl", sample_rate=0.0)
        log.initialize()
        log.log_selection(
            server="gh",
            selected_tool="gh__a",
            candidate_tools=["gh__a"],
            arguments={},
            trace_id=None,
        )
        ctx = _ctx_with_selection(tmp_path, log=log)
        result = await stm_selection_stats(ctx=ctx)
        assert "Selection Stats" in result
        assert "events_sampled_out: 1" in result


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
            mock_cfg.otlp = MagicMock()
            mock_cfg.otlp.enabled = False

            async with app_lifespan(mcp) as _ctx:
                # ProxyManager.start() should NOT be called when proxy is disabled
                mock_pm_instance.start.assert_not_awaited()

    async def test_proxy_disabled_warns_when_surfacing_explicitly_on(self, caplog):
        """An EXPLICITLY surfacing-enabled config under a disabled proxy
        silently does nothing (the engine only initializes inside the proxy
        branch), previously visible only on demand via stm_proxy_health —
        startup must emit the #288-style "enabled but inert" warning. And
        ONLY then: surfacing.enabled defaults to True, so the plain
        control-only default startup (enabled-by-default, not explicitly
        set) must stay warning-free, as must an explicit opt-out."""
        from memtomem_stm.server import app_lifespan, mcp

        cases = [
            # (enabled, explicitly set?, expect warning)
            (True, {"enabled"}, True),  # operator turned it on → warn
            (True, set(), False),  # default-on, never touched → quiet
            (False, {"enabled"}, False),  # explicit opt-out → quiet
        ]
        for enabled, fields_set, expect_warning in cases:
            caplog.clear()
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
                mock_cfg.surfacing.enabled = enabled
                mock_cfg.surfacing.model_fields_set = fields_set
                mock_cfg.langfuse = MagicMock()
                mock_cfg.langfuse.enabled = False
                mock_cfg.otlp = MagicMock()
                mock_cfg.otlp.enabled = False

                with caplog.at_level("WARNING", logger="memtomem_stm.server"):
                    async with app_lifespan(mcp) as _ctx:
                        pass

            inert = [r for r in caplog.records if "enabled but inert" in r.getMessage()]
            assert bool(inert) is expect_warning, (enabled, fields_set)

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
            mock_cfg.proxy.selection_telemetry.enabled = False
            mock_cfg.proxy.cache.enabled = False
            mock_cfg.surfacing = MagicMock()
            mock_cfg.surfacing.enabled = True
            mock_cfg.surfacing.feedback_enabled = True
            # MagicMock attrs are truthy — an implicit warmup_enabled would
            # create_task() a non-coroutine mock warm_up and blow up.
            mock_cfg.surfacing.warmup_enabled = False
            mock_cfg.langfuse = MagicMock()
            mock_cfg.langfuse.enabled = False
            mock_cfg.otlp = MagicMock()
            mock_cfg.otlp.enabled = False

            async with app_lifespan(mcp) as ctx:
                assert ctx.feedback_tracker is None
                assert captured_engine_kwargs.get("feedback_tracker") is None

    async def _run_minimal_lifespan(self):
        """Drive app_lifespan with everything optional off — enough config for
        startup, so a teardown-only assertion is all the test carries."""
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
            mock_cfg.otlp = MagicMock()
            mock_cfg.otlp.enabled = False

            async with app_lifespan(mcp) as _ctx:
                pass

    async def test_teardown_terminates_a_child_that_survived_every_stop(
        self, monkeypatch, caplog
    ):
        """A stop() can return while abandoning a live stdio child (owner task
        lost, bounded join given up) — the child then outlives the exiting
        process as an orphan holding its own LTM (#906). Anything still a
        direct child after full teardown is that leak, so it must be
        terminated, and visibly."""
        killed = []
        monkeypatch.setattr("memtomem_stm.utils.child_reaper.direct_child_pids", lambda: {4242})
        monkeypatch.setattr("memtomem_stm.utils.child_reaper._has_exited", lambda _pid: False)
        monkeypatch.setattr(
            "memtomem_stm.utils.child_reaper.terminate_leaked_children", killed.append
        )

        with caplog.at_level("WARNING", logger="memtomem_stm.utils.child_reaper"):
            await self._run_minimal_lifespan()

        assert killed == [{4242}]
        assert any("leaked child process" in r.getMessage() for r in caplog.records)

    async def test_teardown_with_no_surviving_children_kills_nothing(self, monkeypatch, caplog):
        """The common case: every component reaped its own child, so the sweep
        finds nothing and stays silent — no warning for a clean shutdown."""
        killed = []
        monkeypatch.setattr(
            "memtomem_stm.utils.child_reaper.terminate_leaked_children", killed.append
        )

        with caplog.at_level("WARNING", logger="memtomem_stm.utils.child_reaper"):
            await self._run_minimal_lifespan()

        assert killed == []
        assert not any("leaked child process" in r.getMessage() for r in caplog.records)

    async def test_teardown_sweeps_even_when_a_stop_is_cancelled(self, monkeypatch, caplog):
        """A cancelled teardown is precisely when a stop() abandons its child,
        and CancelledError is not an Exception — it propagates past every
        `except Exception` guard in the teardown. The sweep must still run."""
        from memtomem_stm.server import app_lifespan, mcp

        killed = []
        monkeypatch.setattr("memtomem_stm.utils.child_reaper.direct_child_pids", lambda: {4242})
        monkeypatch.setattr("memtomem_stm.utils.child_reaper._has_exited", lambda _pid: False)
        monkeypatch.setattr(
            "memtomem_stm.utils.child_reaper.terminate_leaked_children", killed.append
        )

        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock()
        mock_pm_instance.stop = AsyncMock(side_effect=asyncio.CancelledError())
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
            mock_cfg.otlp = MagicMock()
            mock_cfg.otlp.enabled = False

            with pytest.raises(asyncio.CancelledError):
                async with app_lifespan(mcp) as _ctx:
                    pass

        assert killed == [{4242}]

    async def test_metrics_store_init_failure_degrades_gracefully(self):
        """A corrupt/locked metrics DB (or a lost migration race) raising at
        init must log and fall back to no metrics rather than crashing the
        server — every proxied tool would otherwise go down for an optional
        telemetry DB. Mirrors the sibling-tracker guard pattern."""
        from memtomem_stm.server import app_lifespan, mcp

        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock()
        mock_pm_instance.stop = AsyncMock()
        mock_pm_instance.get_proxy_tools.return_value = []

        with (
            patch("memtomem_stm.server.STMConfig") as MockConfig,
            patch("memtomem_stm.server.ProxyManager", return_value=mock_pm_instance),
            patch(
                "memtomem_stm.proxy.metrics_store.MetricsStore",
                side_effect=sqlite3.DatabaseError("file is not a database"),
            ),
        ):
            mock_cfg = MockConfig.return_value
            mock_cfg.proxy = MagicMock()
            mock_cfg.proxy.enabled = True
            mock_cfg.proxy.config_path = Path("/tmp/proxy.json")
            mock_cfg.proxy.metrics.enabled = True
            mock_cfg.proxy.compression_feedback.enabled = False
            mock_cfg.proxy.progressive_reads.enabled = False
            mock_cfg.proxy.selection_telemetry.enabled = False
            mock_cfg.proxy.cache.enabled = False
            mock_cfg.surfacing = MagicMock()
            mock_cfg.surfacing.enabled = False
            mock_cfg.langfuse = MagicMock()
            mock_cfg.langfuse.enabled = False
            mock_cfg.otlp = MagicMock()
            mock_cfg.otlp.enabled = False

            async with app_lifespan(mcp) as ctx:
                # Server came up; the tracker just has no backing store.
                assert ctx.tracker._metrics_store is None
                mock_pm_instance.start.assert_awaited_once()

    async def test_cache_init_failure_degrades_gracefully(self):
        """A corrupt/locked cache DB raising at init must degrade to
        cache-disabled (ProxyManager receives cache=None) instead of failing
        startup — the response cache is an optional optimization."""
        from memtomem_stm.server import app_lifespan, mcp

        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock()
        mock_pm_instance.stop = AsyncMock()
        mock_pm_instance.get_proxy_tools.return_value = []

        captured_pm_kwargs: dict = {}

        def _capture_pm(*_args, **kwargs):
            captured_pm_kwargs.update(kwargs)
            return mock_pm_instance

        with (
            patch("memtomem_stm.server.STMConfig") as MockConfig,
            patch("memtomem_stm.server.ProxyManager", side_effect=_capture_pm),
            patch(
                "memtomem_stm.proxy.cache.ProxyCache",
                side_effect=sqlite3.DatabaseError("file is not a database"),
            ),
        ):
            mock_cfg = MockConfig.return_value
            mock_cfg.proxy = MagicMock()
            mock_cfg.proxy.enabled = True
            mock_cfg.proxy.config_path = Path("/tmp/proxy.json")
            mock_cfg.proxy.metrics.enabled = False
            mock_cfg.proxy.compression_feedback.enabled = False
            mock_cfg.proxy.progressive_reads.enabled = False
            mock_cfg.proxy.selection_telemetry.enabled = False
            mock_cfg.proxy.cache.enabled = True
            mock_cfg.surfacing = MagicMock()
            mock_cfg.surfacing.enabled = False
            mock_cfg.langfuse = MagicMock()
            mock_cfg.langfuse.enabled = False
            mock_cfg.otlp = MagicMock()
            mock_cfg.otlp.enabled = False

            async with app_lifespan(mcp) as _ctx:
                assert captured_pm_kwargs.get("cache") is None
                mock_pm_instance.start.assert_awaited_once()

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
            patch(
                "memtomem_stm.server.ProxyConfig.load_from_file_with_status",
                return_value=ConfigLoadResult(config=None, error=None),
            ),
        ):
            mock_cfg = MockConfig.return_value
            mock_cfg.proxy = MagicMock()
            mock_cfg.proxy.enabled = True
            mock_cfg.proxy.config_path = Path("/tmp/proxy.json")
            mock_cfg.proxy.metrics.enabled = False
            mock_cfg.proxy.compression_feedback.enabled = False
            mock_cfg.proxy.progressive_reads.enabled = False
            mock_cfg.proxy.selection_telemetry.enabled = False
            mock_cfg.proxy.cache.enabled = False
            mock_cfg.surfacing = MagicMock()
            mock_cfg.surfacing.enabled = True
            mock_cfg.surfacing.feedback_enabled = False
            mock_cfg.surfacing.warmup_enabled = False
            mock_cfg.langfuse = MagicMock()
            mock_cfg.langfuse.enabled = False
            mock_cfg.otlp = MagicMock()
            mock_cfg.otlp.enabled = False

            with pytest.raises(RuntimeError, match="upstream down"):
                async with app_lifespan(mcp) as _ctx:
                    pass  # Never reached — start() raises before yield.

        # The cleanup block must run even though yield was never reached.
        mock_adapter.stop.assert_awaited_once()
        mock_pm_instance.stop.assert_awaited_once()

    async def _lifespan_with_warmup(self, warmup_enabled: bool) -> MagicMock:
        """Run app_lifespan with a surfacing-enabled mock config and return
        the mock adapter, so tests can assert whether warm_up() ran (#664)."""
        from memtomem_stm.server import app_lifespan, mcp

        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock()
        mock_pm_instance.stop = AsyncMock()
        mock_pm_instance.get_proxy_tools.return_value = []

        mock_adapter = MagicMock()
        mock_adapter.warm_up = AsyncMock()
        mock_adapter.stop = AsyncMock()

        with (
            patch("memtomem_stm.server.STMConfig") as MockConfig,
            patch("memtomem_stm.server.ProxyManager", return_value=mock_pm_instance),
            patch(
                "memtomem_stm.surfacing.mcp_client.McpClientSearchAdapter",
                return_value=mock_adapter,
            ),
            patch("memtomem_stm.server.SurfacingEngine", return_value=MagicMock(stop=AsyncMock())),
        ):
            mock_cfg = MockConfig.return_value
            mock_cfg.proxy = MagicMock()
            mock_cfg.proxy.enabled = True
            mock_cfg.proxy.config_path = Path("/tmp/proxy.json")
            mock_cfg.proxy.metrics.enabled = False
            mock_cfg.proxy.compression_feedback.enabled = False
            mock_cfg.proxy.progressive_reads.enabled = False
            mock_cfg.proxy.selection_telemetry.enabled = False
            mock_cfg.proxy.cache.enabled = False
            mock_cfg.surfacing = MagicMock()
            mock_cfg.surfacing.enabled = True
            mock_cfg.surfacing.feedback_enabled = False
            mock_cfg.surfacing.warmup_enabled = warmup_enabled
            mock_cfg.langfuse = MagicMock()
            mock_cfg.langfuse.enabled = False
            mock_cfg.otlp = MagicMock()
            mock_cfg.otlp.enabled = False

            async with app_lifespan(mcp) as _ctx:
                # Give the spawned warm-up task a turn to run.
                await asyncio.sleep(0)
        return mock_adapter

    async def test_warmup_task_spawned_when_enabled(self):
        """#664 PR 2: with warmup_enabled the lifespan kicks the LTM warm-up
        in a background task — the first surfacing call meets a warm child
        without the lifespan ever awaiting the ~9s cold start inline."""
        mock_adapter = await self._lifespan_with_warmup(warmup_enabled=True)
        mock_adapter.warm_up.assert_awaited_once()

    async def test_warmup_task_not_spawned_when_disabled(self):
        mock_adapter = await self._lifespan_with_warmup(warmup_enabled=False)
        mock_adapter.warm_up.assert_not_awaited()

    async def test_file_config_loaded_even_when_env_enabled(self, tmp_path, monkeypatch):
        """``MEMTOMEM_STM_PROXY__ENABLED`` used to make app_lifespan skip the
        JSON file load entirely — the proxy started enabled but with zero
        upstreams (``upstream_servers`` is a file-only field in practice).
        An existing file must be loaded regardless of the env var; env wins
        through the ``load_from_file_with_status`` overlay instead of a bypass."""
        from memtomem_stm.server import app_lifespan, mcp

        monkeypatch.setenv("MEMTOMEM_STM_PROXY__ENABLED", "1")
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text("{}", encoding="utf-8")

        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock()
        mock_pm_instance.stop = AsyncMock()
        mock_pm_instance.get_proxy_tools.return_value = []

        with (
            patch("memtomem_stm.server.STMConfig") as MockConfig,
            patch("memtomem_stm.server.ProxyManager", return_value=mock_pm_instance),
            patch(
                "memtomem_stm.server.ProxyConfig.load_from_file_with_status",
                return_value=ConfigLoadResult(config=None, error=None),
            ) as mock_load,
        ):
            mock_cfg = MockConfig.return_value
            mock_cfg.proxy = MagicMock()
            mock_cfg.proxy.enabled = False
            mock_cfg.proxy.config_path = cfg_file
            mock_cfg.surfacing = MagicMock()
            mock_cfg.surfacing.enabled = False
            mock_cfg.langfuse = MagicMock()
            mock_cfg.langfuse.enabled = False
            mock_cfg.otlp = MagicMock()
            mock_cfg.otlp.enabled = False

            async with app_lifespan(mcp) as _ctx:
                pass

        mock_load.assert_called_once()
        # The env overlay (not a file bypass) is how env keeps winning.
        assert mock_load.call_args.kwargs["env_overrides"].fragment.get("enabled") == "1"
        # missing → None → no swap is decided inside the single load call,
        # not by a separate exists() pre-check that races with deletion.
        assert mock_load.call_args.kwargs["missing_ok"] is False


class TestApplyProxyFileConfig:
    """``_apply_proxy_file_config`` — the app_lifespan config-resolution
    helper: JSON file loaded unconditionally, env deep-merged on top, and
    the ``consumer_model`` propagation from ``STMConfig.model_post_init``
    re-applied after the ``config.proxy`` swap (post-init ran before the
    swap, so a file-only consumer_model never reached surfacing's
    model-aware budgets)."""

    def _config(self, monkeypatch) -> STMConfig:
        for var in (
            "MEMTOMEM_STM_PROXY__ENABLED",
            "MEMTOMEM_STM_PROXY__CONSUMER_MODEL",
            "MEMTOMEM_STM_SURFACING__CONSUMER_MODEL",
        ):
            monkeypatch.delenv(var, raising=False)
        return STMConfig()

    def test_env_enabled_keeps_file_upstreams(self, tmp_path, monkeypatch):
        import json

        from memtomem_stm.proxy.config import collect_proxy_env_overrides
        from memtomem_stm.server import _apply_proxy_file_config

        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "enabled": False,
                    "upstream_servers": {"gh": {"prefix": "gh", "command": "echo"}},
                }
            ),
            encoding="utf-8",
        )
        config = self._config(monkeypatch)
        config.proxy.config_path = cfg_file
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__ENABLED", "1")

        _apply_proxy_file_config(config, collect_proxy_env_overrides())

        assert config.proxy.enabled is True  # env wins over the file's False
        assert set(config.proxy.upstream_servers) == {"gh"}  # file fields survive

    def test_env_only_inert_upstreams_warn_on_the_no_swap_path(
        self, tmp_path, monkeypatch, caplog
    ):
        """The env-only startup has no file, and `missing_ok=False` means the
        loader returns before it can warn about anything — so the advisory
        (#831) has to come from here, against the pydantic-settings config
        that will actually run. Explicitness comes from `model_fields_set`,
        since there is no raw dict to look for the key in."""
        import logging
        import os

        from memtomem_stm.proxy.config import collect_proxy_env_overrides
        from memtomem_stm.server import _apply_proxy_file_config

        missing = tmp_path / "stm_proxy.json"
        # STMConfig() reads the whole namespace, so an inherited upstream or
        # ENABLED would change both the asserted server set and the advisory.
        for name in [n for n in os.environ if n.startswith("MEMTOMEM_STM_PROXY")]:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FX__PREFIX", "fx")
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FX__COMMAND", "fx-server")

        for env_enabled, expected in ((None, '"enabled" is unset'), ("false", "explicitly")):
            caplog.clear()
            if env_enabled is None:
                monkeypatch.delenv("MEMTOMEM_STM_PROXY__ENABLED", raising=False)
            else:
                monkeypatch.setenv("MEMTOMEM_STM_PROXY__ENABLED", env_enabled)
            config = STMConfig()
            config.proxy.config_path = missing
            with caplog.at_level(logging.WARNING):
                _apply_proxy_file_config(config, collect_proxy_env_overrides())
            assert set(config.proxy.upstream_servers) == {"fx"}
            advisories = [r for r in caplog.records if "present but inert" in r.getMessage()]
            assert len(advisories) == 1, env_enabled
            assert expected in advisories[0].getMessage()

        caplog.clear()
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__ENABLED", "true")
        config = STMConfig()
        config.proxy.config_path = missing
        with caplog.at_level(logging.WARNING):
            _apply_proxy_file_config(config, collect_proxy_env_overrides())
        assert not [r for r in caplog.records if "present but inert" in r.getMessage()]

    def test_file_consumer_model_reaches_surfacing_budgets(self, tmp_path, monkeypatch):
        import json

        from memtomem_stm.server import _apply_proxy_file_config

        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"consumer_model": "gpt-4.1-mini"}), encoding="utf-8")
        config = self._config(monkeypatch)
        config.proxy.config_path = cfg_file
        # Pre-propagation: un-scaled default budget.
        assert config.surfacing.effective_max_injection_chars() == 3000

        _apply_proxy_file_config(config, {})

        assert config.surfacing.consumer_model == "gpt-4.1-mini"
        # Consumer-model scaling never exceeds the operator's explicit cap.
        assert config.surfacing.effective_max_injection_chars() == 3000

    def test_explicit_surfacing_consumer_model_not_clobbered(self, tmp_path, monkeypatch):
        """Same guard as ``model_post_init``: an explicitly-set surfacing
        consumer_model wins over the proxy-level one from the file."""
        import json

        from memtomem_stm.server import _apply_proxy_file_config

        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"consumer_model": "gpt-4.1-mini"}), encoding="utf-8")
        config = self._config(monkeypatch)
        config.surfacing.consumer_model = "claude-sonnet-4"
        config.proxy.config_path = cfg_file

        _apply_proxy_file_config(config, {})

        assert config.surfacing.consumer_model == "claude-sonnet-4"

    def test_missing_file_keeps_pydantic_settings_env_parse(self, tmp_path, monkeypatch):
        """Env-only mode with NO config file: ``STMConfig()``'s
        pydantic-settings parse supports JSON-encoded complex env values
        (``UPSTREAM_SERVERS``) that the raw-string overlay dict cannot
        represent. Rebuilding ``config.proxy`` from the overlay would fail
        validation and silently collapse a working env-only setup to
        defaults (disabled, zero upstreams) — when the file is missing the
        helper must leave the env-parsed config untouched."""
        from memtomem_stm.proxy.config import collect_proxy_env_overrides
        from memtomem_stm.server import _apply_proxy_file_config

        monkeypatch.setenv("MEMTOMEM_STM_PROXY__ENABLED", "1")
        monkeypatch.setenv(
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS",
            '{"gh": {"prefix": "gh", "command": "echo"}}',
        )
        config = STMConfig()
        assert config.proxy.enabled is True
        assert set(config.proxy.upstream_servers) == {"gh"}
        config.proxy.config_path = tmp_path / "nonexistent.json"

        _apply_proxy_file_config(config, collect_proxy_env_overrides())

        assert config.proxy.enabled is True
        assert set(config.proxy.upstream_servers) == {"gh"}

    def test_returns_error_when_file_present_but_broken(self, tmp_path, monkeypatch):
        """#611: the helper reports "present but broken" so the lifespan can
        pin it on STMContext instead of the failure living only in stderr."""
        from memtomem_stm.server import _apply_proxy_file_config

        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text("{ not valid json", encoding="utf-8")
        config = self._config(monkeypatch)
        config.proxy.config_path = cfg_file
        default_enabled = config.proxy.enabled

        error = _apply_proxy_file_config(config, {})

        assert error is not None
        assert config.proxy.enabled is default_enabled  # fell back, no swap

    def test_returns_none_for_good_and_missing_files(self, tmp_path, monkeypatch):
        import json

        from memtomem_stm.server import _apply_proxy_file_config

        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"enabled": True}), encoding="utf-8")
        config = self._config(monkeypatch)
        config.proxy.config_path = cfg_file
        assert _apply_proxy_file_config(config, {}) is None

        config2 = self._config(monkeypatch)
        config2.proxy.config_path = tmp_path / "nonexistent.json"
        assert _apply_proxy_file_config(config2, {}) is None


# ── advertise_observability_tools flag ──────────────────────────────────
#
# The flag hides 8 observability tools from the MCP ``tools/list`` surface
# while keeping them importable from Python. Registration happens at
# module import, so the end-to-end assertion uses a subprocess to get a
# fresh interpreter under the intended env var.


_FLAG_ENV = "MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS"
_FORMATION_ENV = "MEMTOMEM_STM_FORMATION__ENABLED"

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
    "stm_selection_stats",
    "stm_compression_stats",
    "stm_progressive_stats",
    "stm_tuning_recommendations",
}


class TestShouldAdvertiseObsTools:
    """Unit tests for the env-var helper. Monkeypatchable — no module reload."""

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(_FLAG_ENV, raising=False)
        assert _should_advertise_obs_tools() is False

    def test_false_variants_disable(self, monkeypatch):
        for value in ("false", "FALSE", "False", "0", "no", "NO", "  false  "):
            monkeypatch.setenv(_FLAG_ENV, value)
            assert _should_advertise_obs_tools() is False, f"{value!r} should disable"

    def test_other_values_passthrough(self, monkeypatch):
        for value in ("true", "yes", "1", "", "anything-else"):
            monkeypatch.setenv(_FLAG_ENV, value)
            assert _should_advertise_obs_tools() is True, f"{value!r} should opt in"


class TestAdvertiseObservabilityFlagEndToEnd:
    """Subprocess-based — confirms registration-time behavior end-to-end.

    We can't monkeypatch the flag after the current test-process has already
    imported ``memtomem_stm.server`` (registration runs at import, module
    is cached). A fresh subprocess gets a clean interpreter.
    """

    @staticmethod
    def _list_registered(env_override: str | None, *, formation_enabled: bool = False) -> list[str]:
        import json
        import os
        import subprocess
        import sys

        env = {k: v for k, v in os.environ.items() if k not in {_FLAG_ENV, _FORMATION_ENV}}
        if env_override is not None:
            env[_FLAG_ENV] = env_override
        if formation_enabled:
            env[_FORMATION_ENV] = "true"
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

    def test_default_keeps_only_model_facing(self):
        names = set(self._list_registered(env_override=None))
        assert names == _MODEL_FACING_TOOLS
        assert _OBSERVABILITY_TOOLS.isdisjoint(names)

    def test_flag_true_advertises_all_twelve(self):
        names = set(self._list_registered(env_override="true"))
        assert names == _MODEL_FACING_TOOLS | _OBSERVABILITY_TOOLS
        assert len(_OBSERVABILITY_TOOLS) == 8
        assert len(names) == 12
        assert "stm_index_stats" not in names

    def test_formation_is_an_independent_thirteenth_tool(self):
        formation_only = set(self._list_registered(env_override="false", formation_enabled=True))
        all_enabled = set(self._list_registered(env_override="true", formation_enabled=True))
        assert formation_only == _MODEL_FACING_TOOLS | {"stm_memory_propose"}
        assert len(formation_only) == 5
        assert all_enabled == _MODEL_FACING_TOOLS | _OBSERVABILITY_TOOLS | {"stm_memory_propose"}
        assert len(all_enabled) == 13

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

    def test_obs_tool_names_constant_matches_gated_set(self):
        """#613: ``_OBSERVABILITY_TOOL_NAMES`` (source of truth for the
        hidden-tools hint count) must equal the tools that actually appear
        only when the flag is on — otherwise the "N tools hidden" count drifts
        when an observability tool is added or removed."""
        on = set(self._list_registered(env_override="true"))
        off = set(self._list_registered(env_override="false"))
        gated = on - off
        assert set(_OBSERVABILITY_TOOL_NAMES) == gated

    def test_hidden_hint_count_matches_constant(self, monkeypatch):
        """The hint's number is derived from the constant, not hardcoded."""
        monkeypatch.delenv(_FLAG_ENV, raising=False)
        hint = _hidden_obs_tools_hint()
        assert hint is not None
        assert len(_OBSERVABILITY_TOOL_NAMES) == 8
        assert hint.startswith("8 observability tools hidden")

        monkeypatch.setenv(_FLAG_ENV, "true")
        assert _hidden_obs_tools_hint() is None


# ── advertise order — proxied before STM utility tools (#228) ─────────────


class TestAdvertiseOrder:
    """#228: STM utility tools register at module import; proxied tools
    register later, inside ``app_lifespan``. Without a reorder step,
    ``tools/list`` yields STM utility tools first and pushes proxied domain
    tools to the end of the picker. ``_move_stm_tools_to_end`` pops each
    STM utility entry from the insertion-ordered ``_tool_manager._tools``
    dict and reinserts it, placing proxied tools first."""

    def _make_server(self, tool_names):
        """Build a stand-in server with an insertion-ordered `_tool_manager._tools`.

        Only the attribute path `_tool_manager._tools` matters — the
        function pops/inserts string keys and does not touch tool values,
        so opaque sentinels suffice."""
        from types import SimpleNamespace

        tools = {name: SimpleNamespace(name=name) for name in tool_names}
        return SimpleNamespace(_tool_manager=SimpleNamespace(_tools=tools))

    def test_reorder_moves_stm_utility_tools_to_end(self):
        """Mixed insertion: STM utility tools first, proxied tools second
        (the production ordering before the fix). After ``_move_stm_tools_to_end``
        runs, every proxied tool precedes every STM utility tool."""
        from memtomem_stm.server import _move_stm_tools_to_end

        initial = [
            # STM utility tools (inserted first at module import)
            "stm_proxy_stats",
            "stm_proxy_read_more",
            "stm_surfacing_feedback",
            # Proxied tools (inserted later during lifespan)
            "fs__read_file",
            "gh__search_repositories",
            "langchain__search_docs_by_lang_chain",
        ]
        server = self._make_server(initial)
        _move_stm_tools_to_end(server)

        final = list(server._tool_manager._tools.keys())
        # All proxied tools are now ahead of every STM utility tool.
        proxied = [n for n in final if "__" in n]
        util = [n for n in final if n.startswith("stm_") and "__" not in n]
        assert final[: len(proxied)] == proxied, (
            f"proxied tools must lead the advertise list, got: {final}"
        )
        assert final[len(proxied) :] == util, (
            f"STM utility tools must trail the advertise list, got: {final}"
        )

    def test_reorder_skips_missing_stm_tools(self):
        """When ``MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=false`` hides
        the 8 observability tools, ``_tool_manager._tools`` only holds the
        4 model-facing STM tools. The reorder helper must not KeyError on
        the absent names — ``.pop(name, None)`` is the contract."""
        from memtomem_stm.server import _move_stm_tools_to_end

        # Only the 4 model-facing stm_* tools + proxied.
        initial = [
            "stm_proxy_select_chunks",
            "stm_proxy_read_more",
            "stm_surfacing_feedback",
            "stm_compression_feedback",
            "fs__read_file",
        ]
        server = self._make_server(initial)
        _move_stm_tools_to_end(server)

        final = list(server._tool_manager._tools.keys())
        assert final[0] == "fs__read_file"
        assert set(final) == set(initial)  # nothing added or dropped

    def test_reorder_survives_sdk_api_shift(self, caplog):
        """If the SDK renames/removes the private ``_tool_manager._tools``
        attribute, the reorder must degrade to a warning-and-skip rather
        than crashing server startup. Surfacing as a warning gives
        operators a visible signal in the log."""
        import logging
        from types import SimpleNamespace

        from memtomem_stm.server import _move_stm_tools_to_end

        # No ``_tool_manager`` attribute at all → AttributeError branch.
        server = SimpleNamespace()
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.server"):
            _move_stm_tools_to_end(server)  # must not raise
        assert any("MCPServer internal API changed" in rec.message for rec in caplog.records), [
            r.message for r in caplog.records
        ]

    async def test_reorder_pins_order_through_real_sdk_list_tools(self):
        """End-to-end #228 pin against the real ``MCPServer`` instance.

        The stand-in tests above validate the helper's dict surgery, but a
        SDK upgrade could change what ``_tool_manager._tools`` insertion
        order *means* — e.g. a ``list_tools()`` that re-sorts, or a tool
        manager that stops preserving insertion order — and every stand-in
        test would stay green while the #228 advertise order silently
        regressed. Register fake proxied tools through the public API on the
        real module-global server (after the module-import ``stm_*``
        registrations, exactly like the production lifespan), reorder, and
        assert through the public ``list_tools()`` — the surface MCP clients
        actually see.
        """
        from memtomem_stm import server

        tools_dict = server.mcp._tool_manager._tools
        snapshot = dict(tools_dict)
        try:

            @server.mcp.tool(name="fs__read_file")
            def _fake_read(path: str) -> str:  # pragma: no cover - never invoked
                return path

            @server.mcp.tool(name="gh__search_repositories")
            def _fake_search(query: str) -> str:  # pragma: no cover - never invoked
                return query

            server._move_stm_tools_to_end(server.mcp)

            advertised = [t.name for t in await server.mcp.list_tools()]
            positions = {name: idx for idx, name in enumerate(advertised)}
            proxied = [n for n in advertised if "__" in n]
            utility = [n for n in advertised if n in set(server._STM_UTILITY_TOOL_NAMES)]
            assert {"fs__read_file", "gh__search_repositories"} <= set(proxied)
            assert utility, "no STM utility tools advertised — test lost its subject"
            assert max(positions[n] for n in proxied) < min(positions[n] for n in utility), (
                f"proxied tools must advertise before STM utility tools, got: {advertised}"
            )
        finally:
            tools_dict.clear()
            tools_dict.update(snapshot)

    async def test_lifespan_reorders_after_proxy_registration(self):
        """Execute the real registration path end to end: ``app_lifespan``
        must leave proxied tools ahead of STM utility tools in the
        advertise order. The e2e test above drives the helper directly, so
        it alone would stay green if the lifespan dropped the reorder call,
        hoisted it above the ``register_proxy_tool`` loop, or reordered
        only mid-loop (later proxied tools would then append after the STM
        utilities). This test pins the OUTCOME — every call arrangement
        that leaves proxied tools first passes by design; pinning the call
        sequence itself would fail legitimate refactors that preserve the
        invariant. Mirrors
        the ``TestLifespan`` mocked-ProxyManager pattern; the registration
        loop and reorder run for real against the module-global server."""
        from memtomem_stm.proxy.manager import ProxyToolInfo
        from memtomem_stm.server import _STM_UTILITY_TOOL_NAMES, app_lifespan, mcp

        infos = [
            ProxyToolInfo(
                prefixed_name=name,
                description="fake proxied tool",
                input_schema={"type": "object", "properties": {}},
                server="fake",
                original_name=name.split("__", 1)[1],
            )
            for name in ("fake__alpha", "fake__beta")
        ]
        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock()
        mock_pm_instance.stop = AsyncMock()
        mock_pm_instance.get_proxy_tools.return_value = infos

        tools_dict = mcp._tool_manager._tools
        snapshot = dict(tools_dict)
        try:
            with (
                patch("memtomem_stm.server.STMConfig") as MockConfig,
                patch("memtomem_stm.server.ProxyManager", return_value=mock_pm_instance),
            ):
                mock_cfg = MockConfig.return_value
                mock_cfg.proxy = MagicMock()
                mock_cfg.proxy.enabled = True
                mock_cfg.proxy.config_path = Path("/tmp/proxy.json")
                mock_cfg.proxy.metrics.enabled = False
                mock_cfg.proxy.compression_feedback.enabled = False
                mock_cfg.proxy.progressive_reads.enabled = False
                mock_cfg.proxy.selection_telemetry.enabled = False
                mock_cfg.proxy.cache.enabled = False
                mock_cfg.surfacing = MagicMock()
                mock_cfg.surfacing.enabled = False
                mock_cfg.langfuse = MagicMock()
                mock_cfg.langfuse.enabled = False
                mock_cfg.otlp = MagicMock()
                mock_cfg.otlp.enabled = False

                async with app_lifespan(mcp) as _ctx:
                    advertised = [t.name for t in await mcp.list_tools()]

            positions = {name: idx for idx, name in enumerate(advertised)}
            utility = [n for n in advertised if n in set(_STM_UTILITY_TOOL_NAMES)]
            assert {"fake__alpha", "fake__beta"} <= set(advertised), advertised
            assert utility, "no STM utility tools advertised — test lost its subject"
            assert max(positions["fake__alpha"], positions["fake__beta"]) < min(
                positions[n] for n in utility
            ), f"proxied tools must advertise before STM utility tools, got: {advertised}"
        finally:
            tools_dict.clear()
            tools_dict.update(snapshot)

    def test_utility_tool_names_tuple_matches_registered_set(self):
        """Exhaustiveness guard: every STM utility tool registered by the
        ``@mcp.tool()`` / ``@_obs_tool`` decorators at module import must
        be listed in ``_STM_UTILITY_TOOL_NAMES``. A new ``stm_*`` tool
        that forgets to land in the tuple would silently skip the reorder
        and slip ahead of proxied tools again."""
        from memtomem_stm.server import _STM_UTILITY_TOOL_NAMES

        expected = _MODEL_FACING_TOOLS | _OBSERVABILITY_TOOLS
        assert set(_STM_UTILITY_TOOL_NAMES) == expected, (
            "_STM_UTILITY_TOOL_NAMES drifted from the actual registered set; "
            "add the new stm_* tool to the tuple so the advertise-order "
            "reorder still covers it."
        )


# ── lifespan teardown symmetry (#891) ────────────────────────────────────


class TestLifespanTeardownSymmetry:
    """#891: teardown must remove exactly the tools registration added.

    ``get_proxy_tools()`` is a live re-derivation (it re-reads ``conn.tools``,
    refreshes the toolgraph bundle, and rewrites the advertisement telemetry
    snapshot), so calling it a second time at teardown removes whatever the
    session looks like *then*, not what was registered."""

    @staticmethod
    def _mock_config(MockConfig):
        mock_cfg = MockConfig.return_value
        mock_cfg.proxy = MagicMock()
        mock_cfg.proxy.enabled = True
        mock_cfg.proxy.config_path = Path("/tmp/proxy.json")
        mock_cfg.proxy.metrics.enabled = False
        mock_cfg.proxy.compression_feedback.enabled = False
        mock_cfg.proxy.progressive_reads.enabled = False
        mock_cfg.proxy.selection_telemetry.enabled = False
        mock_cfg.proxy.cache.enabled = False
        mock_cfg.surfacing = MagicMock()
        mock_cfg.surfacing.enabled = False
        mock_cfg.langfuse = MagicMock()
        mock_cfg.langfuse.enabled = False
        mock_cfg.otlp = MagicMock()
        mock_cfg.otlp.enabled = False
        return mock_cfg

    @staticmethod
    def _infos(*names):
        from memtomem_stm.proxy.manager import ProxyToolInfo

        return [
            ProxyToolInfo(
                prefixed_name=name,
                description="fake proxied tool",
                input_schema={"type": "object", "properties": {}},
                server="fake",
                original_name=name.split("__", 1)[1],
            )
            for name in names
        ]

    async def test_teardown_removes_the_registered_set_not_a_fresh_derivation(self):
        """Upstream catalog drift between startup and shutdown (a
        ``tools/list_changed`` reassigns ``conn.tools``) must not change what
        teardown removes. The second, divergent advertisement is wired as a
        ``side_effect`` value that a correct teardown never consumes: the
        registry must come back to its pre-lifespan state — ``fake__alpha``
        removed even though it vanished upstream, and ``fake__gamma`` (never
        registered) never attempted. The removals are spied rather than only
        read off the registry, because a removal for an unregistered name
        raises ``ToolError`` and the guard would otherwise hide it."""
        from memtomem_stm.server import app_lifespan, mcp

        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock()
        mock_pm_instance.stop = AsyncMock()
        mock_pm_instance.get_proxy_tools.side_effect = [
            self._infos("fake__alpha", "fake__beta"),
            self._infos("fake__beta", "fake__gamma"),
        ]

        tools_dict = mcp._tool_manager._tools
        snapshot = dict(tools_dict)
        removed: list[str] = []
        real_remove = type(mcp).remove_tool

        def _remove(self_, name: str) -> None:
            removed.append(name)
            real_remove(self_, name)

        try:
            with (
                patch("memtomem_stm.server.STMConfig") as MockConfig,
                patch("memtomem_stm.server.ProxyManager", return_value=mock_pm_instance),
                patch.object(type(mcp), "remove_tool", _remove),
            ):
                self._mock_config(MockConfig)
                async with app_lifespan(mcp) as _ctx:
                    assert {"fake__alpha", "fake__beta"} <= set(tools_dict)

            assert sorted(removed) == ["fake__alpha", "fake__beta"], (
                f"teardown must remove exactly the registered names, got {removed}"
            )
            assert tools_dict == snapshot, (
                "teardown must remove exactly the registered proxied tools; "
                f"leftover={set(tools_dict) - set(snapshot)}"
            )
            assert mock_pm_instance.get_proxy_tools.call_count == 1, (
                "teardown re-derived the advertisement — that rewrites the "
                "snapshot stm_proxy_health and the ranker read, during shutdown"
            )
        finally:
            tools_dict.clear()
            tools_dict.update(snapshot)

    async def test_stop_runs_when_a_second_advertisement_pass_would_raise(self):
        """The teardown ``get_proxy_tools()`` call was the one statement in the
        cleanup block outside a guard, so anything it raised skipped
        ``proxy_manager.stop()``. Freezing the registered names removes the call
        and with it the failure mode; the raising second value is wired as a
        ``side_effect`` a correct teardown never reaches. The exception here
        stands for any escape from that pass, not for a specific live trigger —
        bundle loading has its own catch-all barrier."""
        from memtomem_stm.server import app_lifespan, mcp

        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock()
        mock_pm_instance.stop = AsyncMock()
        mock_pm_instance.get_proxy_tools.side_effect = [
            self._infos("fake__alpha"),
            RuntimeError("advertisement pass failed"),
        ]

        tools_dict = mcp._tool_manager._tools
        snapshot = dict(tools_dict)
        try:
            with (
                patch("memtomem_stm.server.STMConfig") as MockConfig,
                patch("memtomem_stm.server.ProxyManager", return_value=mock_pm_instance),
            ):
                self._mock_config(MockConfig)
                async with app_lifespan(mcp) as _ctx:
                    assert "fake__alpha" in tools_dict

            mock_pm_instance.stop.assert_awaited_once()
            assert set(tools_dict) == set(snapshot)
        finally:
            tools_dict.clear()
            tools_dict.update(snapshot)

    async def test_stop_runs_even_when_a_removal_raises(self):
        """A failing ``remove_tool`` must not skip ``proxy_manager.stop()``.
        Holds on the pre-#891 code too, through its ``except Exception: pass``;
        kept as a guard on the replacement loop, whose per-item handler is the
        only thing standing between a removal failure and the rest of
        teardown."""
        from memtomem_stm.server import app_lifespan, mcp

        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock()
        mock_pm_instance.stop = AsyncMock()
        mock_pm_instance.get_proxy_tools.return_value = self._infos("fake__alpha")

        tools_dict = mcp._tool_manager._tools
        snapshot = dict(tools_dict)

        def _boom(self_, name: str) -> None:
            raise RuntimeError("remove_tool exploded")

        try:
            with (
                patch("memtomem_stm.server.STMConfig") as MockConfig,
                patch("memtomem_stm.server.ProxyManager", return_value=mock_pm_instance),
                patch.object(type(mcp), "remove_tool", _boom),
            ):
                self._mock_config(MockConfig)
                async with app_lifespan(mcp) as _ctx:
                    assert "fake__alpha" in tools_dict

            mock_pm_instance.stop.assert_awaited_once()
        finally:
            tools_dict.clear()
            tools_dict.update(snapshot)

    async def test_failed_registration_is_not_removed_at_teardown(self):
        """``register_proxy_tool`` degrades (warns and returns) when
        ``add_tool`` raises, so the frozen list must hold only the names that
        actually landed — otherwise teardown removes a name it never
        registered, which under a stricter SDK is an error, not a no-op."""
        from memtomem_stm.server import app_lifespan, mcp

        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock()
        mock_pm_instance.stop = AsyncMock()
        mock_pm_instance.get_proxy_tools.return_value = self._infos("fake__alpha", "fake__beta")

        tools_dict = mcp._tool_manager._tools
        snapshot = dict(tools_dict)
        removed: list[str] = []
        real_add = type(mcp).add_tool
        real_remove = type(mcp).remove_tool

        def _add(self_, fn, **kwargs):
            if kwargs.get("name") == "fake__alpha":
                raise RuntimeError("add_tool exploded")
            return real_add(self_, fn, **kwargs)

        def _remove(self_, name: str) -> None:
            removed.append(name)
            real_remove(self_, name)

        try:
            with (
                patch("memtomem_stm.server.STMConfig") as MockConfig,
                patch("memtomem_stm.server.ProxyManager", return_value=mock_pm_instance),
                patch.object(type(mcp), "add_tool", _add),
                patch.object(type(mcp), "remove_tool", _remove),
            ):
                self._mock_config(MockConfig)
                async with app_lifespan(mcp) as _ctx:
                    assert "fake__alpha" not in tools_dict
                    assert "fake__beta" in tools_dict

            assert removed == ["fake__beta"], (
                f"teardown must skip the tool whose registration failed, got {removed}"
            )
            assert tools_dict == snapshot
        finally:
            tools_dict.clear()
            tools_dict.update(snapshot)

    async def test_disabled_proxy_never_builds_an_advertisement(self):
        """With the proxy disabled nothing is registered, so teardown has
        nothing to remove. The old teardown still called ``get_proxy_tools()``
        unconditionally, entering the advertisement build path for a server that
        proxies nothing."""
        from memtomem_stm.server import app_lifespan, mcp

        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock()
        mock_pm_instance.stop = AsyncMock()
        mock_pm_instance.get_proxy_tools.return_value = []

        tools_dict = mcp._tool_manager._tools
        snapshot = dict(tools_dict)
        try:
            with (
                patch("memtomem_stm.server.STMConfig") as MockConfig,
                patch("memtomem_stm.server.ProxyManager", return_value=mock_pm_instance),
            ):
                mock_cfg = self._mock_config(MockConfig)
                mock_cfg.proxy.enabled = False
                async with app_lifespan(mcp) as _ctx:
                    pass

            mock_pm_instance.get_proxy_tools.assert_not_called()
            assert tools_dict == snapshot
        finally:
            tools_dict.clear()
            tools_dict.update(snapshot)

    async def test_declined_names_are_narrowed_out_of_the_advertisement(self):
        """#908 lifespan seam: a real ``ProxyManager`` would have committed both
        names at exposure time; the collision below means only one of them is the
        proxy's — the host keeps its own tool under the other name — so the
        lifespan must hand the registered set back for narrowing. This
        pins the wiring and the list it passes — that the list is derived from a
        real collision is what keeps it from being tautological. What the
        narrowing then does to health, ranking and telemetry is pinned against a
        real manager in ``test_tool_eligibility``."""
        from memtomem_stm.server import app_lifespan, mcp

        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock()
        mock_pm_instance.stop = AsyncMock()
        mock_pm_instance.get_proxy_tools.return_value = self._infos("fake__alpha", "fake__beta")

        tools_dict = mcp._tool_manager._tools
        snapshot = dict(tools_dict)
        try:

            @mcp.tool(name="fake__alpha")
            def _host_owned(path: str) -> str:  # pragma: no cover - never invoked
                return path

            with (
                patch("memtomem_stm.server.STMConfig") as MockConfig,
                patch("memtomem_stm.server.ProxyManager", return_value=mock_pm_instance),
            ):
                self._mock_config(MockConfig)
                async with app_lifespan(mcp) as _ctx:
                    pass

            mock_pm_instance.retain_registered_advertisement.assert_called_once_with(
                ["fake__beta"]
            )
        finally:
            tools_dict.clear()
            tools_dict.update(snapshot)

    async def test_a_name_already_registered_is_neither_patched_nor_removed(self):
        """The SDK's ``add_tool`` treats a duplicate name as a successful no-op:
        it returns the tool already registered under that name instead of
        inserting the new handler, and ``MCPServer.add_tool`` drops that return
        value — so a collision is invisible at the call site. Claiming it would
        mean overwriting a caller's schema and then deleting their tool at
        teardown, which is exactly the embedded/library reuse #891 is about.
        Registration must decline the name, leave the existing tool byte-alike,
        and keep it out of the frozen removal list."""
        from memtomem_stm.server import app_lifespan, mcp

        mock_pm_instance = MagicMock()
        mock_pm_instance.start = AsyncMock()
        mock_pm_instance.stop = AsyncMock()
        mock_pm_instance.get_proxy_tools.return_value = self._infos("fake__alpha")

        tools_dict = mcp._tool_manager._tools
        snapshot = dict(tools_dict)
        try:

            @mcp.tool(name="fake__alpha")
            def _host_owned(path: str) -> str:  # pragma: no cover - never invoked
                return path

            incumbent = tools_dict["fake__alpha"]
            before = (incumbent.description, incumbent.parameters, incumbent.fn_metadata)

            with (
                patch("memtomem_stm.server.STMConfig") as MockConfig,
                patch("memtomem_stm.server.ProxyManager", return_value=mock_pm_instance),
            ):
                self._mock_config(MockConfig)
                async with app_lifespan(mcp) as _ctx:
                    assert tools_dict["fake__alpha"] is incumbent

            assert tools_dict.get("fake__alpha") is incumbent, (
                "teardown deleted a tool this lifespan never registered"
            )
            assert (
                incumbent.description,
                incumbent.parameters,
                incumbent.fn_metadata,
            ) == before, "registration overwrote a tool it does not own"
        finally:
            tools_dict.clear()
            tools_dict.update(snapshot)


# ── main() exception barrier (#209) ──────────────────────────────────────


class TestMainExceptionBarrier:
    """#209 Part A: unhandled exceptions from ``mcp.run()`` must be logged at
    ERROR level before the process terminates, so operators have a visible
    signal instead of only stderr tail output."""

    @pytest.fixture(autouse=True)
    def _neutralize_logging_setup(self):
        """``main()`` now configures root logging via
        ``configure_server_logging`` (#612), which calls
        ``basicConfig(force=True)`` — that removes pytest's caplog handler
        before ``mcp.run()`` raises, so the barrier's ERROR log would never be
        captured. These tests exercise the barrier, not logging setup, so
        stub it out (its own behavior is covered in test_logging_setup.py)."""
        with patch("memtomem_stm.server.configure_server_logging", return_value=None):
            yield

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
            f"clean exit should not emit ERROR logs; got: {[r.getMessage() for r in error_records]}"
        )

    def test_short_lived_full_mcp_proxy_session_has_clean_stderr(self, tmp_path):
        """Real outer MCP EOF plus a real proxied stdio child unwind cleanly."""
        import json
        import os
        import subprocess
        import sys

        repo_root = Path(__file__).resolve().parents[1]
        fake_server = Path(__file__).with_name("_fake_memtomem_server.py")
        isolated = tmp_path / "isolated"
        env = os.environ.copy()
        for key in tuple(env):
            if key.startswith("MEMTOMEM_STM_"):
                env.pop(key)
        for key, dirname in (
            ("HOME", "home"),
            ("XDG_CONFIG_HOME", "xdg-config"),
            ("XDG_CACHE_HOME", "xdg-cache"),
            ("MEMTOMEM_STM_DATA_DIR", "stm-data"),
        ):
            path = isolated / dirname
            path.mkdir(parents=True)
            env[key] = str(path)

        config_path = isolated / "stm_proxy.json"
        config_path.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "cache": {"enabled": False},
                    "metrics": {"enabled": False},
                    "upstream_servers": {
                        "fake": {
                            "prefix": "fake",
                            "transport": "stdio",
                            "command": sys.executable,
                            "args": [str(fake_server)],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        env.update(
            {
                "MEMTOMEM_STM_PROXY__ENABLED": "true",
                "MEMTOMEM_STM_PROXY__CONFIG_PATH": str(config_path),
                "MEMTOMEM_STM_SURFACING__ENABLED": "false",
                "MEMTOMEM_STM_LOG_LEVEL": "ERROR",
                "PYTHONWARNINGS": "default",
            }
        )

        script = """
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from memtomem_stm.server import main; main()"],
        env=dict(os.environ),
    )
    async with stdio_client(params, errlog=sys.stderr) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            result = await session.list_tools()
            names = [tool.name for tool in result.tools]
            assert any("mem_search" in name for name in names), names


asyncio.run(main())
"""
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        stderr = proc.stderr
        assert "AnyIO cancel scope" not in stderr
        assert "Attempted to exit" not in stderr
        assert "Exception ignored in" not in stderr
        assert "traceback" not in stderr.lower()

    def test_anyio_cancel_scope_shutdown_error_exits_cleanly(self, caplog):
        import logging

        from memtomem_stm import server

        errors = (
            RuntimeError(
                "Attempted to exit a cancel scope that isn't the current tasks's "
                "current cancel scope"
            ),
            RuntimeError(
                "Attempted to exit cancel scope in a different task than it was entered in"
            ),
        )

        for err in errors:
            caplog.clear()
            with (
                caplog.at_level(logging.DEBUG, logger="memtomem_stm.server"),
                patch.object(server.mcp, "run", side_effect=err),
            ):
                server.main()

            error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
            warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
            debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
            assert not error_records
            assert not warning_records
            assert any(
                "AnyIO cancel-scope cleanup condition" in r.getMessage() for r in debug_records
            )

    def test_exception_group_wrapped_cancel_scope_error_exits_cleanly(self, caplog):
        """anyio >= 4 strict task groups wrap the cancel-scope RuntimeError in
        an ``ExceptionGroup`` before it reaches ``main()`` — the bare shape
        the test above injects never occurs through ``mcp.run()`` in
        production. Both the single-wrap and the nested-group shape must be
        treated as a clean shutdown."""
        import logging

        from memtomem_stm import server

        cancel_err = RuntimeError(
            "Attempted to exit cancel scope in a different task than it was entered in"
        )
        shapes = (
            ExceptionGroup("unhandled errors in a TaskGroup", [cancel_err]),
            ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [ExceptionGroup("unhandled errors in a TaskGroup", [cancel_err])],
            ),
        )

        for group in shapes:
            caplog.clear()
            with (
                caplog.at_level(logging.DEBUG, logger="memtomem_stm.server"),
                patch.object(server.mcp, "run", side_effect=group),
            ):
                server.main()

            error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
            warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
            debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
            assert not error_records, (
                f"wrapped cancel-scope shutdown must not hit the barrier; "
                f"got: {[r.getMessage() for r in error_records]}"
            )
            assert not warning_records
            assert any(
                "AnyIO cancel-scope cleanup condition" in r.getMessage() for r in debug_records
            )

    def test_exception_group_with_real_failure_hits_barrier(self, caplog):
        """A group mixing the cancel-scope error with any other failure is NOT
        a clean shutdown — the real failure must be logged and re-raised."""
        import logging

        from memtomem_stm import server

        group = ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [
                RuntimeError(
                    "Attempted to exit cancel scope in a different task than it was entered in"
                ),
                ValueError("real bug"),
            ],
        )

        caplog.clear()
        with (
            caplog.at_level(logging.ERROR, logger="memtomem_stm.server"),
            patch.object(server.mcp, "run", side_effect=group),
        ):
            try:
                server.main()
            except ExceptionGroup:
                pass
            else:
                import pytest as _pytest

                _pytest.fail("main() must re-raise a group containing a real failure")

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("unhandled exception" in r.getMessage() for r in error_records)


def test_initialize_advertises_package_version_not_sdk_version():
    """serverInfo.version must be memtomem-stm's own release.

    Passing ``version=`` to the server constructor is the only thing that
    makes this true, and it has never been right by default: an e2e bare
    ``initialize`` handshake caught the 1.x SDK substituting
    ``importlib.metadata.version("mcp")`` — clients saw "1.28.1" for a 0.1.x
    STM — and 2.0 substitutes an empty string instead, which is quieter
    still. Both wrong answers are asserted against by name below, because a
    future SDK gets to pick a third one.

    Exercised through the same ``InitializationOptions`` the stdio
    handshake serializes, not through the constructor argument, so removing
    the argument fails here rather than only on the wire.
    """
    import importlib.metadata

    import memtomem_stm
    from memtomem_stm.server import mcp as stm_mcp

    opts = stm_mcp._lowlevel_server.create_initialization_options()
    assert opts.server_version == memtomem_stm.__version__

    assert opts.server_version, "2.0's unset default is an empty string"
    sdk_version = importlib.metadata.version("mcp")
    if sdk_version != memtomem_stm.__version__:  # pragma: no branch
        assert opts.server_version != sdk_version, "1.x's unset default was the SDK version"
