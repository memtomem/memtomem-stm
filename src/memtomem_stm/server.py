"""STM MCP server — proxy gateway with proactive memory surfacing."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession

from memtomem_stm.config import STMConfig
from memtomem_stm.proxy.compression_feedback import CompressionFeedbackTracker
from memtomem_stm.proxy.config import ProxyConfig, collect_proxy_env_overrides
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.progressive_reads import ProgressiveReadsTracker
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.observability import (
    FAULT_SKIP_REASONS,
    HEALTHY_SKIP_REASONS,
    SurfacingObservability,
)
from memtomem_stm.observability.tracing import traced
from memtomem_stm.surfacing.feedback import FeedbackTracker

logger = logging.getLogger(__name__)

_HASHED_QUERY_PREVIEW_RE = re.compile(r"sha256:[0-9a-f]{16}")
"""Exact shape of the opaque ID `FeedbackStore.get_stats` passes through
verbatim for rows persisted under ``persist_query_text=False`` (#352
part 3). Used by ``stm_surfacing_stats`` to decide whether to emit the
hash-legend line. A raw query starting with ``sha256:`` (e.g. a
user-typed checksum search) would be 80-char-clipped by the store but
still carry the literal prefix; matching the full 23-char digest shape
keeps the legend from misfiring on those rows."""


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
            # The adapter's MCP connection is deferred to the first
            # surfacing RPC (see ``McpClientSearchAdapter._heal_if_needed``);
            # eagerly awaiting ``start()`` here used to block the proxy's
            # own MCP initialize handshake long enough for hosts (e.g.
            # codex with a 60s startup_timeout) to time out and respawn
            # the proxy, leaving two parallel LTM children.
            if config.surfacing.enabled:
                try:
                    from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

                    mcp_adapter = McpClientSearchAdapter(config.surfacing)
                    logger.info(
                        "Surfacing engine MCP client configured for lazy start: %s",
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
                        observability=SurfacingObservability(),
                    )
                    feedback_db = "disabled"
                    feedback_tables = "disabled"
                    if feedback_tracker is not None:
                        db_status = feedback_tracker.bootstrap_status()
                        feedback_db = str(db_status["path"])
                        feedback_tables = (
                            "ready"
                            if db_status["initialized"]
                            else "missing " + ", ".join(str(t) for t in db_status["missing_tables"])
                        )
                    logger.info(
                        "Surfacing path wired: engine=enabled feedback=%s "
                        "feedback_db=%s feedback_tables=%s",
                        "enabled" if feedback_tracker is not None else "disabled",
                        feedback_db,
                        feedback_tables,
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
        "MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS", "false"
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
    "stm_index_stats",
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

    bootstrap_lines = _surfacing_bootstrap_lines(app)

    health = pm.get_upstream_health()
    if not health:
        head = "No upstream servers configured."
        return "\n".join([head, *bootstrap_lines]) if bootstrap_lines else head

    lines = ["Upstream Server Health", "====================="]
    for name, info in health.items():
        status = "connected" if info["connected"] else "DISCONNECTED"
        lines.append(f"  {name}: {status} ({info['tools']} tools)")

    surfacing = app.surfacing_engine
    if surfacing is not None:
        cb = surfacing._circuit_breaker
        cb_state = "open (failing)" if cb.is_open else "closed (healthy)"
        lines.append(f"\nSurfacing circuit breaker: {cb_state}")

    lines.extend(bootstrap_lines)
    return "\n".join(lines)


def _surfacing_bootstrap_lines(app: STMContext) -> list[str]:
    # Mirrors mms health's bootstrap section: lets agents see surfacing
    # readiness via MCP without shelling out to the CLI.
    surfacing_cfg = app.config.surfacing
    if not surfacing_cfg.enabled:
        return []

    lines = ["", "Surfacing Bootstrap", "==================="]
    # app_lifespan skips the entire surfacing init block when
    # proxy.enabled=false (the default control-only mode), so a None
    # tracker there is expected — not a runtime failure.
    if not app.config.proxy.enabled:
        lines.append("  feedback tracking: inactive (proxy disabled)")
        return lines

    tracker = app.feedback_tracker
    if tracker is None:
        if surfacing_cfg.feedback_enabled:
            lines.append("  feedback tracking: enabled in config — runtime init failed")
        else:
            lines.append("  feedback tracking: disabled")
        return lines

    db = tracker.bootstrap_status()
    lines.append("  feedback tracking: enabled")
    lines.append(f"  feedback db: {db['path']}")
    if db.get("error"):
        lines.append(f"  feedback tables: error — {db['error']}")
    elif not db.get("exists"):
        lines.append("  feedback tables: missing (DB has not been created)")
    elif db.get("initialized"):
        lines.append("  feedback tables: ready")
    else:
        missing = ", ".join(str(t) for t in db.get("missing_tables", []))
        lines.append(
            f"  feedback tables: missing ({missing}) — surfacing has not initialized this DB"
        )
    return lines


# ---------------------------------------------------------------------------
# Tool: stm_surfacing_feedback
# ---------------------------------------------------------------------------


@mcp.tool()
async def stm_surfacing_feedback(
    surfacing_id: str,
    rating: str | None = None,
    memory_id: str | None = None,
    ratings: list[dict] | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Provide feedback on proactively surfaced memories.

    This helps improve future surfacing relevance via auto-tuning.

    Args:
        surfacing_id: The surfacing ID shown in the memory section.
        rating: One of 'helpful', 'not_relevant', 'already_known' (legacy
            single-rating path).
        memory_id: Optional specific memory chunk ID for the legacy path.
        ratings: Batched per-memory ratings — a list of objects shaped
            ``{"memory_id": str, "rating": str}`` (additional keys are
            ignored). Mutually exclusive with the legacy ``rating`` /
            ``memory_id`` fields. The engine fan-outs internally to the
            same record / invalidation / boost routines as the
            single-call path; a ``helpful`` boost across multiple
            entries collapses to one ``increment_access`` call per
            surfacing event.
    """
    legacy_set = rating is not None or memory_id is not None
    batched_set = ratings is not None
    if legacy_set and batched_set:
        return (
            "Error: cannot mix legacy (`rating`/`memory_id`) and batched (`ratings`) "
            "feedback fields in one call — pick one shape."
        )
    if not legacy_set and not batched_set:
        return "Error: either `rating` or `ratings` is required."

    app = _get_ctx(ctx)
    with traced(
        "stm_surfacing_feedback",
        metadata={
            "surfacing_id": surfacing_id,
            "rating": rating,
            "memory_id": memory_id,
            "ratings_count": len(ratings) if ratings is not None else None,
        },
    ):
        # Route through SurfacingEngine to trigger access boost for helpful feedback
        if app.surfacing_engine is not None:
            if ratings is not None:
                return await app.surfacing_engine.handle_feedback_batch(surfacing_id, ratings)
            return await app.surfacing_engine.handle_feedback(surfacing_id, rating, memory_id)
        if app.feedback_tracker is None:
            return "Feedback tracking is not enabled."
        if ratings is not None:
            return _record_batched_via_tracker(app.feedback_tracker, surfacing_id, ratings)
        return app.feedback_tracker.record_feedback(surfacing_id, rating, memory_id)


def _record_batched_via_tracker(
    tracker: Any,
    surfacing_id: str,
    ratings: list[dict],
) -> str:
    """Fallback fan-out when the engine is unavailable but the tracker is.

    Mirrors :meth:`SurfacingEngine.handle_feedback_batch` minus the cache
    invalidation and access-boost side effects (the engine owns those).
    Used by the proxy-disabled / engine-absent path so the batched MCP
    surface still records rows for operators running in tracker-only mode.
    """
    if not ratings:
        return "Error: `ratings` must contain at least one entry."

    # Two-pass to match SurfacingEngine.handle_feedback_batch fail-fast
    # semantics: parse + shape-validate every entry up-front so a bad
    # entry mid-batch does not leave earlier ``record_feedback`` rows
    # committed. The engine path does this via its own ``parsed`` list;
    # an earlier inline-validate-then-record loop here silently diverged
    # and let a malformed batch persist its prefix.
    parsed: list[tuple[str, str]] = []
    for i, entry in enumerate(ratings):
        if not isinstance(entry, dict):
            return f"Error: ratings[{i}] must be an object."
        mid = entry.get("memory_id")
        rat = entry.get("rating")
        if not isinstance(mid, str) or not mid:
            return f"Error: ratings[{i}] missing string `memory_id`."
        if not isinstance(rat, str):
            return f"Error: ratings[{i}] missing string `rating`."
        parsed.append((mid, rat))

    recorded = 0
    errors: list[str] = []
    for i, (mid, rat) in enumerate(parsed):
        result = tracker.record_feedback(surfacing_id, rat, mid)
        if isinstance(result, str) and result.startswith("Error"):
            errors.append(f"ratings[{i}]: {result}")
        else:
            recorded += 1
    summary = f"Feedback recorded: {recorded}/{len(parsed)} entries"
    if errors:
        summary += " — " + "; ".join(errors)
    return summary


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

        # Min score block — show the default, whether auto-tune is on, and any
        # per-tool adjustments AutoTuner has made this process. Surfaced before
        # the per-tool breakdown so the reader can interpret the breakdown's
        # "feedback N (negative R%)" against the auto-tune readiness threshold.
        # When ``tool=`` is set we restrict the per-tool sublists to that tool
        # only — otherwise the lists would contradict the trailing
        # "(filtered by tool: ...)" marker by leaking unrelated thresholds.
        min_samples_required = 0
        adjusted_scores: dict[str, float] = {}
        overrides: dict[str, float] = {}
        auto_tune_active = False
        # Readiness must be computed against the same unfiltered sample set
        # AutoTuner sees — a ``since=`` window would otherwise under-report
        # eligibility for a tool with enough historical samples but only a
        # handful of recent events. Falls back to the row's filtered fb only
        # when the unfiltered counts aren't available (e.g. tracker missing).
        # ``readiness_total_fb`` mirrors the global pool AutoTuner consults
        # via the documented cold-start fallback: a tool with too few of
        # its own samples still becomes eligible once total feedback across
        # all tools reaches ``min_samples`` (feedback.py::maybe_adjust).
        readiness_counts: dict[str, int] = {}
        if app.feedback_tracker is not None:
            try:
                readiness_counts = app.feedback_tracker.store.get_per_tool_feedback_counts()
            except Exception:
                readiness_counts = {}
        readiness_total_fb = sum(readiness_counts.values())
        if app.surfacing_engine is not None:
            snap = app.surfacing_engine.get_min_score_snapshot()
            adjusted_scores = snap["adjusted"]
            overrides = snap.get("overrides", {})
            min_samples_required = snap["auto_tune_min_samples"]
            auto_tune_active = snap["auto_tune_enabled"]
            auto_state = "on" if auto_tune_active else "off"
            lines.append(
                f"\nMin score:       {snap['default']:.3f} "
                f"(auto-tune {auto_state}, min {min_samples_required} samples)"
            )
            visible_adjusted = (
                {t: s for t, s in adjusted_scores.items() if t == tool}
                if tool is not None
                else adjusted_scores
            )
            visible_overrides = (
                {t: s for t, s in overrides.items() if t == tool} if tool is not None else overrides
            )
            if visible_adjusted:
                lines.append("  Per-tool adjustments:")
                for t, s in sorted(visible_adjusted.items()):
                    lines.append(f"    {t}: {s:.3f}")
            if visible_overrides:
                lines.append("  Per-tool pinned (bypass auto-tune):")
                for t, s in sorted(visible_overrides.items()):
                    lines.append(f"    {t}: {s:.3f}")

        if stats["per_tool_breakdown"]:
            lines.append("\nBy tool:")
            for row in stats["per_tool_breakdown"]:
                fb = row.get("feedback_count", 0)
                neg = row.get("negative_count", row.get("not_relevant_count", 0))
                # Readiness annotations only render when auto-tune is actually
                # active and the tool isn't pinned via context_tools.<tool>.min_score.
                # A pinned override bypasses the tuner entirely (engine.py:458-460),
                # so any "ready" / "need N more" label would imply the tuner could
                # change a threshold the operator has explicitly fixed.
                detail_parts = [
                    f"{row['events']} events",
                    f"avg {row['avg_memory_count']} memories",
                ]
                if fb > 0:
                    ratio_pct = round(neg / fb * 100, 1)
                    detail_parts.append(f"feedback {fb} (negative {ratio_pct}%)")
                else:
                    detail_parts.append("feedback 0")
                tool_name = row["tool"]
                if tool_name in overrides:
                    detail_parts.append(f"pinned {overrides[tool_name]:.3f}")
                elif auto_tune_active and min_samples_required > 0:
                    # Check ``adjusted_scores`` first: AutoTuner has a documented
                    # cold-start fallback (feedback.py::maybe_adjust) that tunes a
                    # tool from the global feedback pool even when the tool's own
                    # feedback count is below ``min_samples``. So an "auto-tuned"
                    # row can have fb < min_samples — gating on fb first would
                    # contradict both the per-tool adjustment block and the
                    # effective threshold the engine actually used.
                    if tool_name in adjusted_scores:
                        detail_parts.append("auto-tuned")
                    else:
                        # AutoTuner is eligible when either the tool's own
                        # feedback or the global pool has hit ``min_samples``
                        # (cold-start fallback). Reporting only the per-tool
                        # gate would label a tool "need N more" even when the
                        # very next surfacing would tune it from the global
                        # pool with ratio outside the [0.2, 0.6] no-op band.
                        # Falls back to the windowed fb when the store didn't
                        # supply a count for this tool (closed/missing).
                        eligible_fb = readiness_counts.get(tool_name, fb)
                        if (
                            eligible_fb >= min_samples_required
                            or readiness_total_fb >= min_samples_required
                        ):
                            detail_parts.append("auto-tune ready")
                        else:
                            # Both per-tool and global-pool gaps surface so
                            # operators don't read the binding figure as their
                            # own tool's shortfall (#361). The global gap is
                            # always ≤ the per-tool gap (eligible_fb is a
                            # subset of readiness_total_fb), so the global
                            # number is the one the tuner is actually waiting
                            # on; the per-tool number tells operators what
                            # their tool itself would need without the
                            # cold-start fallback. When a single tool owns
                            # all the feedback the two gaps coincide, so the
                            # legacy single-number label is preserved to
                            # avoid redundant text.
                            global_gap = min_samples_required - readiness_total_fb
                            per_tool_gap = min_samples_required - eligible_fb
                            if per_tool_gap > global_gap:
                                detail_parts.append(
                                    f"need {global_gap} more (global pool) "
                                    f"or {per_tool_gap} more for this tool"
                                )
                            else:
                                detail_parts.append(f"need {global_gap} more for auto-tune")
                lines.append(f"  {tool_name}: {', '.join(detail_parts)}")

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
            # #352 part 3: when ``persist_query_text=False`` was active for
            # any of the recent rows, ``query_preview`` carries an opaque
            # ``sha256:<16-hex>`` ID instead of the raw query text.
            # Surface a one-line legend so operators reading the output
            # don't mistake the digest for a malformed query. Data-driven
            # so the legend also appears for rows persisted before the
            # operator flipped the flag back to ``True``. The match is
            # ``fullmatch`` against the exact 23-char shape — a raw query
            # that starts with ``sha256:`` (e.g. a user-typed checksum
            # search) would be 80-char-clipped by the store but still
            # carry the literal prefix; prefix-only matching here would
            # misfire the legend on that row.
            if any(
                isinstance(row.get("query_preview"), str)
                and _HASHED_QUERY_PREVIEW_RE.fullmatch(row["query_preview"])
                for row in stats["recent"]
            ):
                lines.append(
                    "  (queries shown as sha256:<digest> for rows written "
                    "under persist_query_text=false; #352 part 3)"
                )
            for row in stats["recent"]:
                ts_iso = datetime.fromtimestamp(row["ts"]).isoformat(timespec="seconds")
                n_mem = len(row["memory_ids"])
                lines.append(f"  [{ts_iso}] {row['tool']}: {row['query_preview']}")
                lines.append(f"    memories: {n_mem}")

        # Skip-reason / outcome / cache aggregates (in-memory, since process
        # start). Appended after the existing DB-backed stats so callers that
        # script against the legacy shape see a strict superset. Suppressed
        # entirely when surfacing has not been invoked, preserving the
        # zero-traffic output byte-for-byte.
        if app.surfacing_engine is not None and app.surfacing_engine.observability is not None:
            snapshot = app.surfacing_engine.observability.snapshot()
            if snapshot["any_call"]:
                lines.extend(_format_observability_sections(snapshot, tool_filter=tool))

        if tool:
            lines.append(f"\n(filtered by tool: {tool})")
        if since:
            lines.append(f"(since: {since})")

        return "\n".join(lines)


def _ordered_tool_keys(per_tool: dict) -> list[str]:
    """Pin ``__total__`` first regardless of ASCII order.

    ``sorted()`` would put ``__total__`` first only because ``_`` (0x5F)
    sorts before lowercase letters (0x61+). A PascalCase tool name would
    sort under ``A`` (0x41) and bury the aggregate row mid-list. Pinning
    the total explicitly removes the dependency on naming convention.
    """
    keys = sorted(t for t in per_tool if t != "__total__")
    if "__total__" in per_tool:
        keys.insert(0, "__total__")
    return keys


def _split_skip_reasons_by_category(
    skip_reasons: dict[str, dict[str, int]],
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Partition per-tool skip-reason counts by healthy / fault category.

    Returns ``(healthy_view, fault_view)``. Tools with no reasons in a
    given category are omitted from that view so empty per-tool sublists
    don't render. Unclassified reasons are silently dropped on the
    assumption that ``test_skip_reason_categorization_is_exhaustive``
    catches missing classifications at CI time — a new ``SkipReason``
    enum value without a category would otherwise vanish from the
    rendered output instead of failing loudly. See observability.py
    for the categorization rationale (#362).
    """
    healthy_view: dict[str, dict[str, int]] = {}
    fault_view: dict[str, dict[str, int]] = {}
    for tool_name, reasons in skip_reasons.items():
        h = {r: c for r, c in reasons.items() if r in HEALTHY_SKIP_REASONS}
        f = {r: c for r, c in reasons.items() if r in FAULT_SKIP_REASONS}
        if h:
            healthy_view[tool_name] = h
        if f:
            fault_view[tool_name] = f
    return healthy_view, fault_view


def _format_observability_sections(snapshot: dict, *, tool_filter: str | None) -> list[str]:
    """Render the skip-reason / outcome / cache sections for stm_surfacing_stats.

    Skip reasons are split into two subsections — healthy (gate / threshold
    / no-results) and fault (LTM / circuit) — so 1000 ``gate_cooldown`` and
    1000 ``ltm_unavailable`` don't render identically (#362). Returns a list
    of lines starting with a blank separator. When ``tool_filter`` is set,
    per-tool dicts are restricted to that tool plus the ``__total__``
    aggregate so the operator can compare a single tool's counters against
    the total without re-running the call.
    """
    skip_reasons: dict[str, dict[str, int]] = snapshot["skip_reasons"]
    outcomes: dict[str, dict[str, int]] = snapshot["outcomes"]
    cache: dict[str, int] = snapshot["cache"]

    if tool_filter is not None:
        skip_reasons = {
            t: r for t, r in skip_reasons.items() if t == tool_filter or t == "__total__"
        }
        outcomes = {t: o for t, o in outcomes.items() if t == tool_filter or t == "__total__"}

    lines: list[str] = []

    healthy_skips, fault_skips = _split_skip_reasons_by_category(skip_reasons)

    if healthy_skips:
        lines.append("\nHealthy skips — gate / threshold / no-results (since process start):")
        for tool_name in _ordered_tool_keys(healthy_skips):
            reasons = healthy_skips[tool_name]
            lines.append(f"  {tool_name}:")
            for reason, count in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])):
                lines.append(f"    {reason}: {count}")

    if fault_skips:
        lines.append("\nFault skips — LTM / circuit (since process start):")
        for tool_name in _ordered_tool_keys(fault_skips):
            reasons = fault_skips[tool_name]
            lines.append(f"  {tool_name}:")
            for reason, count in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])):
                lines.append(f"    {reason}: {count}")

    if outcomes:
        lines.append("\nOutcomes (since process start):")
        for tool_name in _ordered_tool_keys(outcomes):
            tool_outcomes = outcomes[tool_name]
            if not tool_outcomes:
                continue
            lines.append(f"  {tool_name}:")
            for outcome, count in sorted(tool_outcomes.items(), key=lambda kv: (-kv[1], kv[0])):
                lines.append(f"    {outcome}: {count}")

    hits = cache.get("hit", 0)
    misses = cache.get("miss", 0)
    total = hits + misses
    if total > 0:
        ratio = round(hits / total * 100, 1)
        lines.append(
            f"\nCache (since process start): hits {hits}, misses {misses}, hit ratio {ratio}%"
        )

    return lines


# ---------------------------------------------------------------------------
# Tool: stm_index_stats
# ---------------------------------------------------------------------------


@_obs_tool
async def stm_index_stats(
    tool: str | None = None,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Show STM-driven LTM write statistics (both INDEX paths).

    Mirrors ``stm_surfacing_stats`` for the write side. Covers both
    STM-driven LTM-write code paths: ``auto_index_response`` (verbatim
    response → markdown chunk) and ``extract_and_store`` (response →
    LLM-extracted facts → markdown chunks). Both run on the proxy hot
    path (sequential stages 4 and 4b in ``manager.call_tool``) and both
    write to LTM via ``index_engine.index_file``.

    Snapshot shape:

    - ``attempts[tool][path]`` per-tool / per-path call counts; path is
      ``extract`` or ``auto_index``. ``__total__`` aggregates across tools.
    - ``outcomes[tool][label]`` per-tool 4-label outcomes shared across
      both paths. ``stored`` and ``error`` fire for both; ``dedup_skip``
      and ``extracted_zero_facts`` only fire for the extract path
      (auto_index has no extraction or dedup phase).

    **Quality signal absent by design.** SURFACE has surfacing-feedback;
    INDEX has no equivalent. Adding one would have to choose between
    two non-equivalent observables (positive-value vs harm-prevented)
    that conflate distinct questions if bundled — see the
    ``index_observability`` module docstring for the rationale.
    Operators reading this tool should treat
    ``outcomes[__total__][stored]`` as the raw mem_add count
    (across both paths) and
    ``outcomes[__total__][extracted_zero_facts] / attempts[__total__][extract]``
    as the extract-path no-op rate — neither is a value-judgment, just
    a volume distribution.

    Args:
        tool: Optional filter by upstream tool name. The ``__total__``
              aggregate row is always included so a single tool's
              counters can be compared against the total without
              re-running the call.
    """
    app = _get_ctx(ctx)
    pm = app.proxy_manager
    if pm is None:
        return "Proxy is not enabled."

    with traced("stm_index_stats", metadata={"tool": tool}):
        snapshot = pm.index_observability.snapshot()
        if not snapshot["any_call"]:
            return (
                "No INDEX activity recorded since process start "
                "(neither auto_index_response nor extract_and_store "
                "has been invoked)."
            )
        lines = ["Index Stats", "==========="]
        lines.extend(_format_index_observability_sections(snapshot, tool_filter=tool))
        if tool:
            lines.append(f"\n(filtered by tool: {tool})")
        return "\n".join(lines)


def _format_index_observability_sections(snapshot: dict, *, tool_filter: str | None) -> list[str]:
    """Render Attempts / Outcomes sections for ``stm_index_stats``.

    Returns a list of lines (no leading blank — caller appends to header).
    When ``tool_filter`` is set, per-tool dicts are restricted to that
    tool plus the ``__total__`` aggregate so the operator can compare a
    single tool's counters against the total without re-running.

    Mirror of ``_format_observability_sections`` for surfacing, but with
    the INDEX shape: ``attempts`` is per-tool / per-path
    (``{tool: {path: count}}``) so operators see the
    extract / auto_index breakdown; ``outcomes`` is per-tool 4-label.
    No cache section — INDEX has no caching layer.
    """
    attempts: dict[str, dict[str, int]] = snapshot["attempts"]
    outcomes: dict[str, dict[str, int]] = snapshot["outcomes"]

    if tool_filter is not None:
        attempts = {t: a for t, a in attempts.items() if t == tool_filter or t == "__total__"}
        outcomes = {t: o for t, o in outcomes.items() if t == tool_filter or t == "__total__"}

    lines: list[str] = []

    if attempts:
        lines.append("\nAttempts (since process start):")
        for tool_name in _ordered_tool_keys(attempts):
            tool_attempts = attempts[tool_name]
            if not tool_attempts:
                continue
            lines.append(f"  {tool_name}:")
            for path, count in sorted(tool_attempts.items(), key=lambda kv: (-kv[1], kv[0])):
                lines.append(f"    {path}: {count}")

    if outcomes:
        lines.append("\nOutcomes (since process start):")
        for tool_name in _ordered_tool_keys(outcomes):
            tool_outcomes = outcomes[tool_name]
            if not tool_outcomes:
                continue
            lines.append(f"  {tool_name}:")
            for outcome, count in sorted(tool_outcomes.items(), key=lambda kv: (-kv[1], kv[0])):
                lines.append(f"    {outcome}: {count}")

    return lines


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
