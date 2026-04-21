"""STM MCP server — proxy gateway with proactive memory surfacing."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from memtomem_stm.config import STMConfig
from memtomem_stm.proxy.compression_feedback import CompressionFeedbackTracker
from memtomem_stm.proxy.config import ProxyConfig, collect_proxy_env_overrides
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.progressive_reads import ProgressiveReadsTracker
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.observability.tracing import traced
from memtomem_stm.surfacing.feedback import FeedbackTracker

logger = logging.getLogger(__name__)


@dataclass
class STMContext:
    """Dependency container for STM services."""

    config: STMConfig
    proxy_manager: ProxyManager
    tracker: TokenTracker
    surfacing_engine: SurfacingEngine | None
    feedback_tracker: FeedbackTracker | None
    compression_feedback_tracker: CompressionFeedbackTracker | None
    progressive_reads_tracker: ProgressiveReadsTracker | None


CtxType = Context[ServerSession, STMContext]


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[STMContext]:
    config = STMConfig()
    proxy_env_overrides = collect_proxy_env_overrides()

    # Load JSON config file and overlay env vars on top so the documented
    # precedence (env > file > defaults) holds. The CLI writes
    # ``"enabled": true`` to the JSON file, so a normal Quick Start enables
    # the proxy without requiring ``MEMTOMEM_STM_PROXY__ENABLED``. Without
    # the env overlay every other env-set field would be silently clobbered
    # by the file values.
    if not os.environ.get("MEMTOMEM_STM_PROXY__ENABLED"):
        file_cfg = ProxyConfig.load_from_file(
            config.proxy.config_path, env_overrides=proxy_env_overrides
        )
        if file_cfg is not None:
            config.proxy = file_cfg

    # Shared state — populated only when proxy is enabled
    from memtomem_stm.proxy.cache import ProxyCache
    from memtomem_stm.proxy.metrics_store import MetricsStore

    metrics_store: MetricsStore | None = None
    proxy_cache: ProxyCache | None = None
    surfacing_engine: SurfacingEngine | None = None
    mcp_adapter = None
    feedback_tracker: FeedbackTracker | None = None
    compression_feedback_tracker: CompressionFeedbackTracker | None = None
    progressive_reads_tracker: ProgressiveReadsTracker | None = None
    langfuse_client = None
    tracker = TokenTracker()
    proxy_manager: ProxyManager | None = None

    # Wrap init + yield in a single try/finally so a failure between
    # resource acquisition and yield (e.g. proxy_cache.initialize() or
    # proxy_manager.start() raising after mcp_adapter.start() succeeded)
    # still runs the cleanup block. Without this, partial init leaks the
    # mcp_adapter stdio subprocess, open sqlite handles, etc.
    try:
        if config.proxy.enabled:
            # Metrics store
            if config.proxy.metrics.enabled:
                metrics_store = MetricsStore(
                    config.proxy.metrics.db_path.expanduser().resolve(),
                    max_history=config.proxy.metrics.max_history,
                )
                metrics_store.initialize()
            tracker = TokenTracker(metrics_store=metrics_store)

            # Compression feedback tracker — learning loop for agent-reported
            # information loss. Reads ``metrics_store`` read-only for
            # best-effort trace_id correlation when the caller omits it.
            if config.proxy.compression_feedback.enabled:
                try:
                    compression_feedback_tracker = CompressionFeedbackTracker(
                        config.proxy.compression_feedback.db_path,
                        metrics_store=metrics_store,
                    )
                except Exception:
                    logger.warning(
                        "Compression feedback tracker init failed — tool will be disabled",
                        exc_info=True,
                    )
                    compression_feedback_tracker = None

            # Progressive reads telemetry — records one row per
            # ``_apply_progressive`` initial chunk plus one per
            # ``stm_proxy_read_more`` follow-up into ``progressive_reads``.
            # Surfaces via ``stm_progressive_stats``.
            if config.proxy.progressive_reads.enabled:
                try:
                    progressive_reads_tracker = ProgressiveReadsTracker(
                        config.proxy.progressive_reads.db_path,
                    )
                except Exception:
                    logger.warning(
                        "Progressive reads tracker init failed — tool will be disabled",
                        exc_info=True,
                    )
                    progressive_reads_tracker = None

            # Surfacing engine — LTM access is always remote-only via the
            # MCP client adapter. The adapter spawns (or connects to) a
            # memtomem MCP server using
            # config.surfacing.ltm_mcp_command / ltm_mcp_args.
            if config.surfacing.enabled:
                try:
                    from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

                    mcp_adapter = McpClientSearchAdapter(config.surfacing)
                    await mcp_adapter.start()
                    logger.info(
                        "Surfacing engine connected via MCP client to %s",
                        config.surfacing.ltm_mcp_command,
                    )
                except Exception:
                    logger.warning(
                        "MCP client surfacing initialization failed — surfacing disabled",
                        exc_info=True,
                    )
                    mcp_adapter = None

                if mcp_adapter is not None:
                    if config.surfacing.feedback_enabled:
                        try:
                            feedback_tracker = FeedbackTracker(config.surfacing)
                        except Exception:
                            logger.warning(
                                "FeedbackTracker init failed — surfacing feedback disabled",
                                exc_info=True,
                            )
                            feedback_tracker = None

                    surfacing_engine = SurfacingEngine(
                        config.surfacing,
                        mcp_adapter=mcp_adapter,
                        feedback_tracker=feedback_tracker,
                        token_tracker=tracker,
                    )

            # Response cache
            if config.proxy.cache.enabled:
                proxy_cache = ProxyCache(
                    config.proxy.cache.db_path.expanduser().resolve(),
                    max_entries=config.proxy.cache.max_entries,
                )
                proxy_cache.initialize()

            # Langfuse (optional)
            try:
                from memtomem_stm.observability.tracing import init_langfuse

                langfuse_client = init_langfuse(config.langfuse)
            except ImportError:
                pass
            except Exception:
                logger.warning("Langfuse init failed, continuing without tracing", exc_info=True)
        else:
            logger.info("Proxy disabled (enabled=false) — only STM control tools available")

        # Initialize proxy manager (always created for STM control tools like stm_proxy_stats)
        proxy_manager = ProxyManager(
            config.proxy,
            tracker,
            surfacing_engine=surfacing_engine,
            cache=proxy_cache,
            env_overrides=proxy_env_overrides,
            progressive_reads_tracker=progressive_reads_tracker,
        )

        if config.proxy.enabled:
            await proxy_manager.start()

            # Register proxy tools with upstream schema + annotations
            from memtomem_stm.proxy._fastmcp_compat import register_proxy_tool

            def _make_proxy_handler(pm: ProxyManager, server_name: str, tool_name: str):  # noqa: ANN202
                async def proxy_tool(**kwargs: object) -> str | list:
                    return await pm.call_tool(server_name, tool_name, dict(kwargs))

                return proxy_tool

            for info in proxy_manager.get_proxy_tools():
                register_proxy_tool(
                    server,
                    _make_proxy_handler(proxy_manager, info.server, info.original_name),
                    info,
                )

            # Proxied tools are now in front; re-insert STM utility tools at
            # the end so ``tools/list`` yields domain tools first (#228).
            _move_stm_tools_to_end(server)

        ctx = STMContext(
            config=config,
            proxy_manager=proxy_manager,
            tracker=tracker,
            surfacing_engine=surfacing_engine,
            feedback_tracker=feedback_tracker,
            compression_feedback_tracker=compression_feedback_tracker,
            progressive_reads_tracker=progressive_reads_tracker,
        )
        yield ctx
    finally:
        if proxy_manager is not None:
            for info in proxy_manager.get_proxy_tools():
                try:
                    server.remove_tool(info.prefixed_name)
                except Exception:
                    pass
            try:
                await proxy_manager.stop()
            except Exception:
                logger.warning("Failed to stop proxy manager", exc_info=True)
        if surfacing_engine is not None:
            try:
                await surfacing_engine.stop()
            except Exception:
                logger.warning("Failed to stop surfacing engine", exc_info=True)
        for resource, name in [
            (proxy_cache, "proxy_cache"),
            (metrics_store, "metrics_store"),
            (feedback_tracker, "feedback_tracker"),
            (compression_feedback_tracker, "compression_feedback_tracker"),
            (progressive_reads_tracker, "progressive_reads_tracker"),
        ]:
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Failed to close %s", name, exc_info=True)
        if mcp_adapter is not None:
            try:
                await mcp_adapter.stop()
            except Exception:
                logger.warning("Failed to stop MCP adapter", exc_info=True)
        if langfuse_client is not None:
            try:
                from memtomem_stm.observability.tracing import shutdown_langfuse

                shutdown_langfuse(langfuse_client)
            except Exception:
                pass


mcp = FastMCP(
    "memtomem-stm",
    instructions=(
        "Short-term memory proxy gateway with proactive memory surfacing. "
        "Proxies upstream MCP servers with response compression and caching, "
        "and automatically surfaces relevant memories from memtomem LTM."
    ),
    lifespan=app_lifespan,
)


def _should_advertise_obs_tools() -> bool:
    """Read the ``MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS`` env-var flag.

    Factored out so tests can monkeypatch this function directly instead of
    juggling env vars and module reloads. The env read is the source of
    truth; the matching ``STMConfig`` field exists for documentation and
    type-checking but is not consulted here — registration happens at
    module import, before ``app_lifespan`` loads the JSON config file.
    """
    return os.environ.get(
        "MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS", "true"
    ).strip().lower() not in ("false", "0", "no")


def _obs_tool(fn):
    """Register ``fn`` as an MCP tool only when the flag is on.

    When off, return the function unchanged — it stays importable and
    callable from Python (tests, CLI paths), but is not surfaced in the
    MCP ``tools/list``.
    """
    if _should_advertise_obs_tools():
        return mcp.tool()(fn)
    return fn


# STM utility tools exposed over MCP. Kept as an explicit tuple (not a
# ``stm_*`` prefix sweep) so the set is a deliberate choice and the
# advertise-order regression test can pin the exact membership. Order
# inside this tuple does not matter — only position *relative to proxied
# tools* matters; see ``_move_stm_tools_to_end``.
_STM_UTILITY_TOOL_NAMES: tuple[str, ...] = (
    "stm_proxy_stats",
    "stm_proxy_select_chunks",
    "stm_proxy_read_more",
    "stm_proxy_cache_clear",
    "stm_proxy_health",
    "stm_surfacing_feedback",
    "stm_surfacing_stats",
    "stm_compression_feedback",
    "stm_compression_stats",
    "stm_progressive_stats",
    "stm_tuning_recommendations",
)


def _move_stm_tools_to_end(server: FastMCP) -> None:
    """Re-insert STM utility tools so proxied tools advertise first (#228).

    STM utility tools are registered at module import via ``@_obs_tool`` /
    ``@mcp.tool()`` decorators, before ``app_lifespan`` runs; proxied tools
    are registered inside the lifespan once upstream servers are reachable.
    FastMCP's ``_tool_manager._tools`` is an insertion-ordered dict, so
    without this step ``tools/list`` yields STM utility tools before the
    domain tools users are actually reaching for — a picker-UX papercut
    reported in #228.

    This pops each STM utility entry and reinserts it, moving them to the
    end of the insertion order without changing their attributes. Missing
    entries (e.g. observability tools hidden by
    ``MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=false``) are skipped
    silently. Touches the same private ``_tool_manager._tools`` the proxy
    already reaches into in ``_fastmcp_compat.py``; any FastMCP API shift
    surfaces as a ``AttributeError`` and the reorder is skipped with a
    warning rather than breaking server startup.
    """
    try:
        tools_dict = server._tool_manager._tools
    except AttributeError:
        logger.warning(
            "Cannot reorder advertise list — FastMCP internal API changed. "
            "Tools are registered, but STM utility tools may appear before "
            "proxied tools in the picker (#228)."
        )
        return
    for name in _STM_UTILITY_TOOL_NAMES:
        tool = tools_dict.pop(name, None)
        if tool is not None:
            tools_dict[name] = tool


def _get_ctx(ctx: CtxType) -> STMContext:
    return ctx.request_context.lifespan_context


# ---------------------------------------------------------------------------
# Tool: stm_proxy_stats
# ---------------------------------------------------------------------------


@_obs_tool
async def stm_proxy_stats(
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Show token savings and cache statistics for proxied MCP tool calls."""
    app = _get_ctx(ctx)
    summary = app.tracker.get_summary()

    lines = [
        "STM Proxy Stats",
        "===============",
        f"Total calls:     {summary['total_calls']}",
        f"Original chars:  {summary['total_original_chars']}",
        f"Compressed:      {summary['total_compressed_chars']}",
        f"Savings:         {summary['total_savings_pct']:.1f}%",
        f"Token savings:   {summary.get('total_token_savings_pct', 0):.1f}%",
        f"Cache hits:      {summary['cache_hits']}",
        f"Cache misses:    {summary['cache_misses']}",
        f"Reconnects:      {summary.get('reconnects', 0)}",
    ]

    # Error summary
    total_errors = summary.get("total_errors", 0)
    if total_errors > 0:
        lines.append(f"\nErrors: {total_errors} ({summary.get('error_rate', 0):.1f}%)")
        errors_by_cat = summary.get("errors_by_category", {})
        for cat, count in sorted(errors_by_cat.items()):
            lines.append(f"  {cat}: {count}")

    # Latency percentiles
    latency = summary.get("latency_percentiles", {})
    if latency.get("total"):
        t = latency["total"]
        lines.append(f"\nLatency (ms):    p50={t['p50']}  p95={t['p95']}  p99={t['p99']}")

    # RPS
    rps = summary.get("current_rps", 0)
    if rps > 0:
        lines.append(f"Current RPS:     {rps:.1f}")

    # Progressive delivery
    prog_first = summary.get("progressive_first_chunks", 0)
    prog_cont = summary.get("progressive_continuations", 0)
    if prog_first > 0:
        lines.append(f"\nProgressive:     {prog_first} first chunks, {prog_cont} continuations")

    # LTM trust-UX hints (parent PR #231). Quiet when the parent never sent any.
    hint_events = summary.get("total_hint_events", 0)
    if hint_events > 0:
        last_hints = summary.get("last_hints", [])
        lines.append(f"\nLTM hints:       {hint_events} event(s)")
        for h in last_hints:
            lines.append(f"  last: {h}")

    if summary["by_server"]:
        lines.append("\nBy server:")
        for name, s in summary["by_server"].items():
            lines.append(
                f"  {name}: {s['calls']} calls, "
                f"{s['original_chars']} → {s['compressed_chars']} chars "
                f"({s['savings_pct']:.1f}% saved)"
            )

    surfacing = app.surfacing_engine
    if surfacing is not None:
        lines.append(f"\nSurfacing: enabled (min_score={app.config.surfacing.min_score})")
    else:
        lines.append("\nSurfacing: disabled")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: stm_proxy_select_chunks
# ---------------------------------------------------------------------------


@mcp.tool()
async def stm_proxy_select_chunks(
    key: str,
    sections: list[str],
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Retrieve selected sections from a TOC response.

    When a proxied tool returns a TOC (selective compression), use this tool
    to fetch only the sections you need.

    Args:
        key: The selection_key from the TOC response.
        sections: List of section keys to retrieve.
    """
    app = _get_ctx(ctx)
    return app.proxy_manager.select_chunks(key, sections)


# ---------------------------------------------------------------------------
# Tool: stm_proxy_read_more
# ---------------------------------------------------------------------------


@mcp.tool()
async def stm_proxy_read_more(
    key: str,
    offset: int = 0,
    limit: int | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Read more content from a progressive delivery response.

    When a proxied tool response includes progressive delivery metadata
    (has_more=true), use this tool to fetch the next chunk of content.

    Args:
        key: The continuation key from the progressive response footer.
        offset: Character offset to start reading from (use next_offset from footer).
        limit: Max characters to return. Defaults to the configured chunk_size.
    """
    if offset < 0:
        return "Error: offset must be >= 0"
    if limit is not None and limit < 1:
        return "Error: limit must be >= 1"
    app = _get_ctx(ctx)
    return app.proxy_manager.read_more(key, offset, limit)


# ---------------------------------------------------------------------------
# Tool: stm_proxy_cache_clear
# ---------------------------------------------------------------------------


@_obs_tool
async def stm_proxy_cache_clear(
    server: str | None = None,
    tool: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Clear the proxy response cache.

    Args:
        server: If given, only clear entries for this upstream server name (the name used in mms add, not the prefix).
        tool: If given, only clear entries for this tool (across all servers, or scoped to server if both provided).
    """
    app = _get_ctx(ctx)
    pm = app.proxy_manager
    if not hasattr(pm, "_cache") or pm._cache is None:
        return "Cache not enabled. Set proxy.cache.enabled = true in stm_proxy.json."

    removed = pm._cache.clear(server=server, tool=tool)
    if server and tool:
        return f"Cleared {removed} cache entries for {server}/{tool}."
    elif server:
        return f"Cleared {removed} cache entries for server '{server}'."
    elif tool:
        return f"Cleared {removed} cache entries for tool '{tool}'."
    return f"Cleared all {removed} cache entries."


# ---------------------------------------------------------------------------
# Tool: stm_proxy_health
# ---------------------------------------------------------------------------


@_obs_tool
async def stm_proxy_health(
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Check upstream server connectivity and proxy health status."""
    app = _get_ctx(ctx)
    pm = app.proxy_manager

    health = pm.get_upstream_health()
    if not health:
        return "No upstream servers configured."

    lines = ["Upstream Server Health", "====================="]
    for name, info in health.items():
        status = "connected" if info["connected"] else "DISCONNECTED"
        lines.append(f"  {name}: {status} ({info['tools']} tools)")

    surfacing = app.surfacing_engine
    if surfacing is not None:
        cb = surfacing._circuit_breaker
        cb_state = "open (failing)" if cb.is_open else "closed (healthy)"
        lines.append(f"\nSurfacing circuit breaker: {cb_state}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: stm_surfacing_feedback
# ---------------------------------------------------------------------------


@mcp.tool()
async def stm_surfacing_feedback(
    surfacing_id: str,
    rating: str,
    memory_id: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Provide feedback on proactively surfaced memories.

    This helps improve future surfacing relevance via auto-tuning.

    Args:
        surfacing_id: The surfacing ID shown in the memory section.
        rating: One of 'helpful', 'not_relevant', 'already_known'.
        memory_id: Optional specific memory chunk ID to rate.
    """
    app = _get_ctx(ctx)
    with traced(
        "stm_surfacing_feedback",
        metadata={"surfacing_id": surfacing_id, "rating": rating, "memory_id": memory_id},
    ):
        # Route through SurfacingEngine to trigger access boost for helpful feedback
        if app.surfacing_engine is not None:
            return await app.surfacing_engine.handle_feedback(surfacing_id, rating, memory_id)
        if app.feedback_tracker is None:
            return "Feedback tracking is not enabled."
        return app.feedback_tracker.record_feedback(surfacing_id, rating, memory_id)


# ---------------------------------------------------------------------------
# Tool: stm_surfacing_stats
# ---------------------------------------------------------------------------


@_obs_tool
async def stm_surfacing_stats(
    tool: str | None = None,
    since: str | None = None,
    limit: int = 10,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Show proactive surfacing statistics and feedback ratings.

    Mirrors ``stm_compression_stats`` in spirit: aggregates
    ``surfacing_events`` and ``surfacing_feedback`` from
    ``~/.memtomem/stm_feedback.db`` so agents and operators can self-observe
    what STM surfaced and how it was rated without writing SQL.

    Args:
        tool:  Optional filter by upstream tool name.
        since: Optional ISO-8601 timestamp (e.g. ``2026-04-20T00:00:00``) —
               restricts to events whose ``created_at`` is >= this moment.
        limit: Tail size for the ``Recent`` section (default 10, 0 hides).
    """
    app = _get_ctx(ctx)
    if app.feedback_tracker is None:
        return "Feedback tracking is not enabled."

    since_ts: float | None = None
    if since:
        try:
            since_ts = datetime.fromisoformat(since).timestamp()
        except ValueError:
            return f"Error: invalid 'since' timestamp: {since!r} (expected ISO-8601)"

    with traced(
        "stm_surfacing_stats",
        metadata={"tool": tool, "since": since, "limit": limit},
    ):
        stats = app.feedback_tracker.get_stats(tool=tool, since=since_ts, limit=limit)

        lines = [
            "Surfacing Stats",
            "===============",
            f"Events total:    {stats['events_total']}",
            f"Distinct tools:  {stats['distinct_tools']}",
            f"Total feedback:  {stats['total_feedback']}",
        ]

        dr = stats["date_range"]
        if dr["first"] is not None and dr["last"] is not None:
            first_iso = datetime.fromtimestamp(dr["first"]).isoformat(timespec="seconds")
            last_iso = datetime.fromtimestamp(dr["last"]).isoformat(timespec="seconds")
            lines.append(f"Date range:      {first_iso} — {last_iso}")

        if stats["per_tool_breakdown"]:
            lines.append("\nBy tool:")
            for row in stats["per_tool_breakdown"]:
                lines.append(
                    f"  {row['tool']}: {row['events']} events, "
                    f"avg {row['avg_memory_count']} memories"
                )

        if stats["rating_distribution"]:
            lines.append("\nRating distribution:")
            for rating, count in sorted(stats["rating_distribution"].items()):
                lines.append(f"  {rating}: {count}")

        if stats["total_feedback"] > 0:
            helpful = stats["rating_distribution"].get("helpful", 0)
            pct = round(helpful / stats["total_feedback"] * 100, 1)
            lines.append(f"\nHelpfulness: {pct}%")

        if stats["recent"]:
            lines.append("\nRecent:")
            for row in stats["recent"]:
                ts_iso = datetime.fromtimestamp(row["ts"]).isoformat(timespec="seconds")
                n_mem = len(row["memory_ids"])
                lines.append(f"  [{ts_iso}] {row['tool']}: {row['query_preview']}")
                lines.append(f"    memories: {n_mem}")

        if tool:
            lines.append(f"\n(filtered by tool: {tool})")
        if since:
            lines.append(f"(since: {since})")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: stm_compression_feedback
# ---------------------------------------------------------------------------


@mcp.tool()
async def stm_compression_feedback(
    server: str,
    tool: str,
    missing: str,
    kind: str = "other",
    trace_id: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Report missing information from a compressed proxy response.

    Use this after a prior ``stm_proxy_*`` call returned a response whose
    compression stripped something you needed for downstream work. This
    is a *learning signal* — it does not repair the current turn. Reports
    accumulate for later inspection via ``stm_compression_stats`` and
    will feed future auto-tuning of compression strategies per tool.

    Args:
        server: Upstream server name (e.g. ``"docfix"``).
        tool:   Upstream tool name (e.g. ``"get_document"``).
        missing: Free-form description of what was missing
                 (e.g. ``"example code for Query.select"``).
        kind:   One of ``"truncated"``, ``"missing_example"``,
                ``"missing_metadata"``, ``"wrong_topic"``, ``"other"``.
        trace_id: Optional. If omitted, the server correlates to the most
                  recent matching ``(server, tool)`` call within the last
                  30 minutes; if no match, the report is stored with a
                  NULL ``trace_id``.
    """
    app = _get_ctx(ctx)
    if app.compression_feedback_tracker is None:
        return "Compression feedback tracking is not enabled."
    return app.compression_feedback_tracker.record(
        server=server,
        tool=tool,
        missing=missing,
        kind=kind,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# Tool: stm_compression_stats
# ---------------------------------------------------------------------------


@_obs_tool
async def stm_compression_stats(
    tool: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Show compression feedback counts.

    Reports the total number of ``stm_compression_feedback`` calls, the
    breakdown by ``kind``, and (when no tool filter is passed) the
    breakdown by tool.

    Args:
        tool: Optional filter by upstream tool name.
    """
    app = _get_ctx(ctx)
    if app.compression_feedback_tracker is None:
        return "Compression feedback tracking is not enabled."

    stats = app.compression_feedback_tracker.get_stats(tool)

    lines = [
        "Compression Feedback Stats",
        "==========================",
        f"Total feedback: {stats['total_feedback']}",
    ]

    if stats["by_kind"]:
        lines.append("\nBy kind:")
        for kind_name, count in sorted(stats["by_kind"].items()):
            lines.append(f"  {kind_name}: {count}")

    if not tool and stats["by_tool"]:
        lines.append("\nBy tool:")
        for tool_name, count in sorted(
            stats["by_tool"].items(), key=lambda kv: kv[1], reverse=True
        ):
            lines.append(f"  {tool_name}: {count}")

    if tool:
        lines.append(f"\n(filtered by tool: {tool})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: stm_progressive_stats
# ---------------------------------------------------------------------------


@_obs_tool
async def stm_progressive_stats(
    tool: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Show progressive-delivery read statistics.

    Reports per-response follow-up rate and coverage across all
    progressive-compressed calls. Each initial chunk and each
    follow-up ``stm_proxy_read_more`` appears as a row in
    ``progressive_reads``; aggregates here collapse to one entry
    per cache key so a response with five follow-ups is weighted
    the same as one with none.

    Args:
        tool: Optional filter by upstream tool name.
    """
    app = _get_ctx(ctx)
    if app.progressive_reads_tracker is None:
        return "Progressive reads tracking is not enabled."

    stats = app.progressive_reads_tracker.get_stats(tool)

    lines = [
        "Progressive Reads Stats",
        "=======================",
        f"Total reads: {stats['total_reads']}",
        f"Total responses: {stats['total_responses']}",
        f"Follow-up rate: {stats['follow_up_rate'] * 100:.1f}%",
        f"Avg chars served: {stats['avg_chars_served']:.0f}",
        f"Avg total chars: {stats['avg_total_chars']:.0f}",
        f"Avg coverage: {stats['avg_coverage'] * 100:.1f}%",
    ]

    if not tool and stats["by_tool"]:
        lines.append("\nBy tool:")
        for tool_name, per_tool in sorted(
            stats["by_tool"].items(), key=lambda kv: kv[1]["responses"], reverse=True
        ):
            lines.append(
                f"  {tool_name}: responses={per_tool['responses']}, "
                f"follow_up_rate={per_tool['follow_up_rate'] * 100:.1f}%"
            )

    if tool:
        lines.append(f"\n(filtered by tool: {tool})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: stm_tuning_recommendations
# ---------------------------------------------------------------------------


@_obs_tool
async def stm_tuning_recommendations(
    since_hours: float = 24.0,
    tool: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Show per-tool compression tuning recommendations.

    Analyses proxy metrics (and compression feedback when available) to
    suggest ``max_result_chars``, ``compression`` strategy, and
    ``retention_floor`` adjustments per tool.  Recommendations are
    read-only — apply them manually to ``stm_proxy.json``.

    Args:
        since_hours: Analysis window in hours (default 24).
        tool: Optional filter to show recommendations for a single tool.
    """
    from memtomem_stm.proxy.tuner import CompressionTuner, format_recommendations

    app = _get_ctx(ctx)
    metrics_store = app.tracker._metrics_store
    if metrics_store is None:
        return "Metrics store is not enabled — no data to analyse."

    feedback_store = (
        app.compression_feedback_tracker.store if app.compression_feedback_tracker else None
    )

    tuner = CompressionTuner(
        metrics_store=metrics_store,
        feedback_store=feedback_store,
        config=app.proxy_manager._config,
    )
    since = since_hours * 3600.0
    profiles = tuner.get_profiles(since_seconds=since)
    recs = tuner.analyze(since_seconds=since, tool_filter=tool)
    return format_recommendations(recs, profiles, since_hours)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the STM MCP server."""
    level = os.environ.get("MEMTOMEM_STM_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
    )
    # Exception barrier (#209): without this, an unhandled exception from
    # ``mcp.run()`` (e.g. a background task crashing the event loop) ends the
    # process with only stderr output — operators get no ERROR-level log, and
    # clients see stdio EOF without any signal about WHY STM died. Re-raise
    # after logging so the process still terminates; we only add observability.
    try:
        mcp.run()
    except Exception:
        logger.exception("STM MCP server terminated with an unhandled exception")
        raise


if __name__ == "__main__":
    main()
