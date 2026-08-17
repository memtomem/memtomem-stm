"""STM MCP server — proxy gateway with proactive memory surfacing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

# Module-level on purpose: the proxy handler's ``-> CallToolResult`` return
# annotation is a STRING under ``from __future__ import annotations``, and
# the SDK's ``func_metadata`` resolves it against this module's globals at
# ``add_tool`` time — a function-local import would NameError there and send
# every proxied tool down the registration degradation path.
from mcp.types import CallToolResult

from memtomem_stm import __version__ as _stm_version
from memtomem_stm.config import STMConfig
from memtomem_stm.logging_setup import STDERR_FORMAT, configure_server_logging
from memtomem_stm.proxy.compression_feedback import CompressionFeedbackTracker
from memtomem_stm.proxy.config import (
    ProxyConfig,
    collect_proxy_env_overrides,
    env_var_hint_for_validation_error,
    model_upstream_inert_state,
    warn_if_upstreams_inert,
)
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.progressive_reads import ProgressiveReadsTracker
from memtomem_stm.proxy.selection_log import aggregate_selection_log
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.observability import (
    FAULT_SKIP_REASONS,
    HEALTHY_SKIP_REASONS,
    SurfacingObservability,
)
from memtomem_stm.observability.tracing import traced
from memtomem_stm.surfacing.feedback import FeedbackTracker
from memtomem_stm.utils.anyio_shutdown import is_clean_cancel_scope_shutdown
from memtomem_stm.utils.json_out import escape_lone_surrogates, require_utf8_identifier

logger = logging.getLogger(__name__)

_HASHED_QUERY_PREVIEW_RE = re.compile(r"sha256:[0-9a-f]{16}")
"""Exact shape of the opaque ID `FeedbackStore.get_stats` passes through
verbatim for rows persisted under ``persist_query_text=False`` (#352
part 3). Used by ``stm_surfacing_stats`` to decide whether to emit the
hash-legend line. A raw query starting with ``sha256:`` (e.g. a
user-typed checksum search) would be 80-char-clipped by the store but
still carry the literal prefix; matching the full 23-char digest shape
keeps the legend from misfiring on those rows."""

_FLAT_SCORE_WARNING_MIN_SAMPLES = 10
"""Minimum recorded scores before ``stm_surfacing_stats`` warns about a
zero-variance score distribution (#560 step 3). A handful of identical
scores is expected noise (a single query surfaced twice, a tiny window);
ten identical values with zero spread is not — the compact-format
rounding artifact this tripwire exists to catch produced *months* of a
single constant. Deliberately a constant, not config: the warning is
advisory text, and a knob would imply operators should tune it."""


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
    proxy_config_error: str | None = None
    """Set when the proxy config file exists but failed to parse/validate at
    startup (#611): the server is running env/default config — typically with
    the proxy off — and only a buried stderr warning says why. Surfaced by
    ``stm_proxy_health`` so the failure is visible from inside the client."""


# mcp 2.0 reordered the parameters: 1.x was ``Context[ServerSessionT,
# LifespanContextT]``, 2.0 is ``Context[LifespanContextT, RequestT]`` —
# ``RequestT`` being the transport request object, which no handler here
# touches.
CtxType = Context[STMContext, Any]


def _apply_proxy_file_config(config: STMConfig, proxy_env_overrides: dict[str, Any]) -> str | None:
    """Load the JSON config file and overlay env vars on top of it, in place.

    Returns the load error when the file exists but failed to parse/validate
    (``None`` otherwise — including the deliberate missing-file no-swap), so
    the lifespan can pin it on ``STMContext.proxy_config_error`` instead of
    the failure living only in a stderr warning (#611).

    The documented precedence (env > file > defaults) is enforced by
    ``load_from_file``'s deep-merge: every ``MEMTOMEM_STM_PROXY__*`` var —
    including ``ENABLED`` — wins over its file counterpart, while file-only
    fields (notably ``upstream_servers``) survive. The load used to be
    skipped entirely whenever ``MEMTOMEM_STM_PROXY__ENABLED`` was set, which
    started the proxy enabled but with zero upstreams — proxying nothing.

    This is not the only place the file and the environment meet: a per-field
    override of a file-declared upstream server is completed from the file
    during ``STMConfig()`` itself, by ``UpstreamServerCompletionSource``, or
    validation would reject the env fragment before reaching here (#835). That
    source supplies only the server names the environment mentions, so the
    file's own upstreams still arrive through this load — with the warnings a
    settings source has no way to emit.

    When the file is MISSING, ``config.proxy`` is deliberately left as
    constructed: ``STMConfig()``'s pydantic-settings parse already applied
    every env var, so rebuilding from the overlay can only add failure modes
    — a validation failure in that rebuild would silently collapse a working
    env-only setup to defaults. (The overlay now decodes JSON-encoded complex
    values the same way settings does (#834), so the two agree on content;
    this stays the authoritative parse regardless.) ``missing_ok=False``
    makes that decision inside the single ``load_from_file`` call (missing →
    ``None`` → no swap), so there is no separate existence pre-check to race
    with file deletion.

    Replacing ``config.proxy`` happens after ``STMConfig.model_post_init``
    already ran, so its ``consumer_model`` propagation is re-applied here —
    otherwise a consumer_model set only in the file reaches ``config.proxy``
    but never surfacing's model-aware budgets
    (``effective_max_injection_chars`` / ``effective_max_results``).
    """
    result = ProxyConfig.load_from_file_with_status(
        config.proxy.config_path, env_overrides=proxy_env_overrides, missing_ok=False
    )
    if result.config is not None:
        config.proxy = result.config
    else:
        # No swap: either the file is missing (the env-only startup described
        # above) or it failed to load. `load_from_file` warned in every case it
        # inspected a file, but the no-swap path leaves `config.proxy` as
        # pydantic-settings built it — so the inert-upstream advisory (#831)
        # has to be raised here, against the config that will actually run.
        warn_if_upstreams_inert(
            model_upstream_inert_state(config.proxy),
            len(config.proxy.upstream_servers),
            config.proxy.config_path,
            logger_=logger,
        )
    if config.proxy.consumer_model and not config.surfacing.consumer_model:
        config.surfacing.consumer_model = config.proxy.consumer_model
    return result.error


def _build_ltm_adapter(config: STMConfig, daemon_config: STMConfig) -> Any:
    """Select the standalone surfacing route before lifespan warm-up."""
    if config.surfacing.use_daemon is True:
        from memtomem_stm.surfacing.daemon_adapter import DaemonLtmAdapter

        return DaemonLtmAdapter(daemon_config)
    from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

    return McpClientSearchAdapter(config.surfacing)


@asynccontextmanager
async def app_lifespan(server: MCPServer) -> AsyncIterator[STMContext]:
    config = STMConfig()
    # Daemon discovery/spawn must use the same env/default-only basis the
    # detached daemon loads. The proxy file may later propagate a file-only
    # consumer_model into surfacing; using that mutated config for discovery
    # would create a fingerprint the detached process can never publish.
    daemon_config = config.model_copy(deep=True)
    proxy_env_overrides = collect_proxy_env_overrides()
    proxy_config_error = _apply_proxy_file_config(config, proxy_env_overrides)

    # Shared state — populated only when proxy is enabled
    from memtomem_stm.proxy.cache import ProxyCache
    from memtomem_stm.proxy.metrics_store import MetricsStore
    from memtomem_stm.proxy.selection_log import SelectionTelemetryLog

    metrics_store: MetricsStore | None = None
    selection_log: SelectionTelemetryLog | None = None
    proxy_cache: ProxyCache | None = None
    surfacing_engine: SurfacingEngine | None = None
    mcp_adapter: Any = None
    feedback_tracker: FeedbackTracker | None = None
    compression_feedback_tracker: CompressionFeedbackTracker | None = None
    progressive_reads_tracker: ProgressiveReadsTracker | None = None
    langfuse_client = None
    otlp_emitter: Any = None
    tracker = TokenTracker()
    proxy_manager: ProxyManager | None = None
    warmup_task: asyncio.Task[None] | None = None

    # Wrap init + yield in a single try/finally so a failure between
    # resource acquisition and yield (e.g. proxy_cache.initialize() or
    # proxy_manager.start() raising after mcp_adapter.start() succeeded)
    # still runs the cleanup block. Without this, partial init leaks the
    # mcp_adapter stdio subprocess, open sqlite handles, etc.
    try:
        if config.proxy.enabled:
            # Metrics store
            if config.proxy.metrics.enabled:
                try:
                    metrics_store = MetricsStore(
                        config.proxy.metrics.db_path.expanduser().resolve(),
                        max_history=config.proxy.metrics.max_history,
                    )
                    metrics_store.initialize()
                except Exception:
                    # A corrupt/locked metrics DB (or a lost migration race)
                    # must not take down every proxied tool — telemetry is
                    # optional. Degrade to no metrics, like the sibling
                    # trackers below. ``initialize`` closes its own handle on
                    # failure, so dropping the object leaks nothing.
                    logger.warning(
                        "Metrics store init failed — proxy metrics disabled",
                        exc_info=True,
                    )
                    metrics_store = None
            tracker = TokenTracker(metrics_store=metrics_store)

            # Compression feedback tracker — learning loop for agent-reported
            # information loss. Reads ``metrics_store`` read-only for
            # best-effort trace_id correlation when the caller omits it.
            if config.proxy.compression_feedback.enabled:
                try:
                    compression_feedback_tracker = CompressionFeedbackTracker(
                        config.proxy.compression_feedback.db_path,
                        metrics_store=metrics_store,
                        retention_days=config.proxy.compression_feedback.retention_days,
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
                        retention_days=config.proxy.progressive_reads.retention_days,
                    )
                except Exception:
                    logger.warning(
                        "Progressive reads tracker init failed — tool will be disabled",
                        exc_info=True,
                    )
                    progressive_reads_tracker = None

            # Selection/execution telemetry (#467) — append-only JSONL log
            # of which tool the client picked from the advertised set and
            # how the call went. Opt-in; read at startup like
            # ``metrics.enabled`` (no hot-reload).
            if config.proxy.selection_telemetry.enabled:
                try:
                    st_cfg = config.proxy.selection_telemetry
                    selection_log = SelectionTelemetryLog(
                        st_cfg.path,
                        max_bytes=st_cfg.max_bytes,
                        max_backups=st_cfg.max_backups,
                        sample_rate=st_cfg.sample_rate,
                    )
                    selection_log.initialize()
                except Exception:
                    logger.warning(
                        "Selection telemetry init failed — telemetry disabled",
                        exc_info=True,
                    )
                    selection_log = None

            # Surfacing engine — LTM access is always remote-only via the
            # MCP client adapter. The adapter spawns (or connects to) a
            # memtomem MCP server using
            # config.surfacing.ltm_mcp_command / ltm_mcp_args.
            # The adapter's MCP connection is deferred to the first
            # surfacing RPC (see ``McpClientSearchAdapter._heal_if_needed``);
            # eagerly awaiting ``start()`` here used to block the proxy's
            # own MCP initialize handshake long enough for hosts (e.g.
            # codex with a 60s startup_timeout) to time out and respawn
            # the proxy, leaving two parallel LTM children. The adapter
            # runs its lifecycle ops in an internal owner task (#663), so
            # the deferred start no longer ties anyio cancel scopes to the
            # request-handler task that happens to trigger it, and the
            # lifespan ``stop()`` below is task-safe. The warm-up spawned
            # below (#664) pre-pays the ~9s LTM cold start in a background
            # task — initialize is still never blocked, and the lazy start
            # remains the fallback when warm-up is disabled or fails.
            if config.surfacing.enabled:
                try:
                    mcp_adapter = _build_ltm_adapter(config, daemon_config)
                    logger.info(
                        "Surfacing LTM route configured for lazy start: %s",
                        "shared daemon"
                        if config.surfacing.use_daemon is True
                        else config.surfacing.ltm_mcp_command,
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
                    if config.surfacing.warmup_enabled:
                        warmup_task = asyncio.create_task(mcp_adapter.warm_up(), name="ltm-warmup")

            # Response cache
            if config.proxy.cache.enabled:
                try:
                    proxy_cache = ProxyCache(
                        config.proxy.cache.db_path.expanduser().resolve(),
                        max_entries=config.proxy.cache.max_entries,
                    )
                    proxy_cache.initialize()
                except Exception:
                    # A corrupt/locked cache DB must not take down every
                    # proxied call — the cache is an optional optimization.
                    # Degrade to cache-disabled (ProxyManager already handles
                    # cache=None). ``initialize`` closes its own handle on
                    # failure, so dropping the object leaks nothing.
                    logger.warning(
                        "Response cache init failed — proxy cache disabled",
                        exc_info=True,
                    )
                    proxy_cache = None

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
            if config.surfacing.enabled and "enabled" in config.surfacing.model_fields_set:
                # Mirror the #288 "enabled but inert" pattern: the surfacing
                # engine only initializes inside the proxy branch above, so a
                # surfacing-enabled config silently does nothing here —
                # otherwise visible only on demand via stm_proxy_health.
                # Gated on model_fields_set because surfacing.enabled defaults
                # to True: only an EXPLICIT enablement (env/config) is a
                # config-says-enabled mismatch; the plain control-only default
                # startup must stay warning-free.
                logger.warning(
                    "surfacing explicitly enabled but the proxy is disabled — "
                    "config is enabled but inert; no memories will be surfaced. "
                    "Enable the proxy or set surfacing.enabled=false to silence."
                )

        # OTLP span export (optional, #789). Deliberately OUTSIDE the
        # ``config.proxy.enabled`` branch above: ``traced()`` also wraps the
        # STM control tools (e.g. stm_surfacing_feedback), so gating this on
        # the proxy would make ``otlp.enabled=true`` silently inert in
        # control-only mode.
        #
        # Initialization failures are NOT degraded away when export is
        # enabled. Degrade-open covers export failures *after* a working
        # exporter exists; swallowing a construction error instead would leave
        # explicitly-enabled telemetry silently inert — the exact failure the
        # fail-fast config contract exists to prevent. (An invalid standard
        # `OTEL_EXPORTER_OTLP_*` value, e.g. a bad compression setting, raises
        # here rather than at config validation, so this is reachable.)
        from memtomem_stm.observability.otlp import init_otlp

        otlp_emitter = init_otlp(config.otlp)

        # Initialize proxy manager (always created for STM control tools like stm_proxy_stats)
        proxy_manager = ProxyManager(
            config.proxy,
            tracker,
            surfacing_engine=surfacing_engine,
            cache=proxy_cache,
            env_overrides=proxy_env_overrides,
            progressive_reads_tracker=progressive_reads_tracker,
            selection_log=selection_log,
        )

        if config.proxy.enabled:
            await proxy_manager.start()

            # Register proxy tools with upstream schema + annotations
            from memtomem_stm.proxy._fastmcp_compat import (
                register_proxy_tool,
                to_call_tool_result,
            )

            def _make_proxy_handler(pm: ProxyManager, server_name: str, tool_name: str):  # noqa: ANN202
                # The bare ``-> CallToolResult`` annotation matters: the SDK's
                # ``func_metadata`` special-cases it (return without output
                # validation) but REJECTS a Union containing it, and the
                # returned envelope passes through ``convert_result`` and the
                # lowlevel handler verbatim — preserving structuredContent,
                # result-level _meta, isError, and content order end to end.
                async def proxy_tool(**kwargs: object) -> CallToolResult:
                    return to_call_tool_result(
                        await pm.call_tool(server_name, tool_name, dict(kwargs))
                    )

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
            proxy_config_error=proxy_config_error,
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
            (selection_log, "selection_log"),
        ]:
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Failed to close %s", name, exc_info=True)
        if warmup_task is not None:
            # Cancelling mid-start abandons the op (#664) — it finishes in
            # the adapter's owner task, and ``stop()``'s bounded join below
            # closes or cancels it in-task.
            warmup_task.cancel()
            try:
                await warmup_task
            except (asyncio.CancelledError, Exception):
                pass
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
        if otlp_emitter is not None:
            # Last: every span-producing component above has stopped, so the
            # drain has a finite set to wait on. Bounded by its own config —
            # a hung collector delays shutdown by at most that budget.
            try:
                from memtomem_stm.observability.otlp import shutdown_otlp

                shutdown_otlp(otlp_emitter, timeout_seconds=config.otlp.flush_timeout_seconds)
            except Exception:
                logger.warning("Failed to shut down OTLP export", exc_info=True)


# ``version=`` pins ``serverInfo.version`` in the ``initialize`` response to
# this package's version. It has never been right without it: 1.x substituted
# ``importlib.metadata.version("mcp")``, so handshakes advertised the MCP SDK
# version as ours, and 2.0 substitutes an empty string. Mirrors the fix core
# made in memtomem#383.
mcp = MCPServer(
    "memtomem-stm",
    instructions=(
        "Short-term memory proxy gateway with proactive memory surfacing. "
        "Proxies upstream MCP servers with response compression and caching, "
        "and automatically surfaces relevant memories from memtomem LTM."
    ),
    version=_stm_version,
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


def _should_advertise_formation_tool() -> bool:
    """Formation is opt-in and import-time gated like observability tools."""
    return os.environ.get("MEMTOMEM_STM_FORMATION__ENABLED", "false").strip().lower() not in (
        "false",
        "0",
        "no",
    )


def _formation_tool(fn):
    if _should_advertise_formation_tool():
        return mcp.tool()(fn)
    return fn


# The observability tools gated behind ``@_obs_tool`` — the ones hidden from
# ``tools/list`` when ``MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS`` is off.
# Source of truth for the "N tools hidden" discoverability hint (#613) so the
# count stays in sync with the decorated set; a regression test pins this tuple
# against the actually-gated tools (flag-on minus flag-off).
_OBSERVABILITY_TOOL_NAMES: tuple[str, ...] = (
    "stm_proxy_stats",
    "stm_proxy_cache_clear",
    "stm_proxy_health",
    "stm_surfacing_stats",
    "stm_selection_stats",
    "stm_compression_stats",
    "stm_progressive_stats",
    "stm_tuning_recommendations",
)


def _hidden_obs_tools_hint() -> str | None:
    """One-line hint that observability tools are hidden, or ``None`` (#613).

    Returns ``None`` when the tools are advertised. Driven off
    ``_should_advertise_obs_tools()`` — the same signal that actually gates
    registration — so the hint never claims tools are hidden when they aren't
    (or vice versa).

    Consumed by the ``mms health`` CLI, not by ``stm_proxy_health``: that MCP
    tool is itself one of the gated tools, so it is unreachable over MCP in the
    exact state (flag off) where the hint applies. The CLI command is always
    available regardless of the flag, so it is the reachable operator surface.
    """
    if _should_advertise_obs_tools():
        return None
    return (
        f"{len(_OBSERVABILITY_TOOL_NAMES)} observability tools hidden; "
        "set MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=true to expose them"
    )


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
    "stm_selection_stats",
    "stm_compression_feedback",
    "stm_compression_stats",
    "stm_progressive_stats",
    "stm_tuning_recommendations",
) + (("stm_memory_propose",) if _should_advertise_formation_tool() else ())


def _move_stm_tools_to_end(server: MCPServer) -> None:
    """Re-insert STM utility tools so proxied tools advertise first (#228).

    STM utility tools are registered at module import via ``@_obs_tool`` /
    ``@mcp.tool()`` decorators, before ``app_lifespan`` runs; proxied tools
    are registered inside the lifespan once upstream servers are reachable.
    The SDK's ``_tool_manager._tools`` is an insertion-ordered dict, so
    without this step ``tools/list`` yields STM utility tools before the
    domain tools users are actually reaching for — a picker-UX papercut
    reported in #228.

    This pops each STM utility entry and reinserts it, moving them to the
    end of the insertion order without changing their attributes. Missing
    entries (e.g. observability tools hidden by
    ``MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=false``) are skipped
    silently. Touches the same private ``_tool_manager._tools`` the proxy
    already reaches into in ``_fastmcp_compat.py``; any SDK API shift
    surfaces as a ``AttributeError`` and the reorder is skipped with a
    warning rather than breaking server startup.
    """
    try:
        tools_dict = server._tool_manager._tools
    except AttributeError:
        logger.warning(
            "Cannot reorder advertise list — MCPServer internal API changed. "
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

    cache_hits = summary["cache_hits"]
    cache_misses = summary["cache_misses"]
    cache_lookups = cache_hits + cache_misses
    cache_hit_rate = (cache_hits / cache_lookups * 100) if cache_lookups else 0.0

    # Every rendered number reconciles (#558): ``Total calls`` counts
    # successful live pipeline calls only (the denominator of the savings/
    # latency figures) — cache hits and failed calls never enter it — and the
    # ``Total calls`` line states the sum explicitly so "live + failed +
    # cache-served = invocations" needs no operator arithmetic. The failed
    # component is rendered only when non-zero, matching the Errors section.
    total_errors = summary.get("total_errors", 0)
    total_invocations = summary.get(
        "total_invocations", summary["total_calls"] + cache_hits + total_errors
    )
    failed_component = f" + {total_errors} failed" if total_errors else ""
    hits_suffix = (
        f"  ({summary.get('cache_hit_chars', 0):,} chars served from cache, no upstream I/O)"
        if cache_hits
        else ""
    )

    lines = [
        "STM Proxy Stats",
        "===============",
        f"Total calls:     {summary['total_calls']} live{failed_component}"
        f" + {cache_hits} cache-served = {total_invocations} invocations",
        f"Original chars:  {summary['total_original_chars']}",
        f"Compressed:      {summary['total_compressed_chars']}",
        f"Savings:         {summary['total_savings_pct']:.1f}%  (compression of live calls)",
        f"Token savings:   {summary.get('total_token_savings_pct', 0):.1f}%",
        f"Cache hits:      {cache_hits}{hits_suffix}",
        f"Cache misses:    {cache_misses}",
    ]

    # Misses the cache can never convert to hits (mixed/non-text/empty
    # responses, transient retrieval keys). Quiet when zero so the common
    # all-text deployment keeps its familiar output. When present, the hit-rate
    # line also shows the rate over STORABLE lookups — otherwise a workload
    # heavy in never-cacheable responses reads as a depressed hit rate, which
    # is exactly the operator confusion the counter exists to resolve (#558).
    cache_unstorable = summary.get("cache_unstorable", 0)
    hit_rate_line = f"Cache hit rate:  {cache_hit_rate:.1f}%"
    if cache_unstorable > 0:
        lines.append(
            f"Unstorable:      {cache_unstorable}  (of misses; response shape is never cacheable)"
        )
        storable_lookups = cache_hits + max(cache_misses - cache_unstorable, 0)
        if storable_lookups > 0:
            effective_rate = cache_hits / storable_lookups * 100
            hit_rate_line += f"  ({effective_rate:.1f}% of storable lookups)"
        else:
            hit_rate_line += "  (no storable lookups)"

    lines += [
        hit_rate_line,
        f"Reconnects:      {summary.get('reconnects', 0)}",
    ]

    # Cache occupancy / eviction visibility (size vs max_entries, expired backlog,
    # lifetime evictions) — distinct from the per-call hit/miss counters above and
    # only available when the response cache is wired. Read inside this
    # operator-invoked tool, so the synchronous sqlite COUNT stays off the hot path.
    pm = getattr(app, "proxy_manager", None)
    response_cache = getattr(pm, "_cache", None) if pm is not None else None
    if response_cache is not None:
        cstats = response_cache.stats()
        lines.append(
            f"Cache entries:   {cstats['total_entries']} "
            f"(expired {cstats['expired_entries']}, evicted {cstats['evictions']})"
        )

    # Error summary. ``error_rate``'s denominator is live upstream attempts
    # (successful + failed calls) — cache hits can't error, so folding them in
    # would only dilute the diagnostic. Label it so the percentage doesn't read
    # as inconsistent next to the hits-inclusive invocation total (#558).
    if total_errors > 0:
        lines.append(
            f"\nErrors: {total_errors} ({summary.get('error_rate', 0):.1f}% of live attempts)"
        )
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
    """Clear the proxy caches.

    An unfiltered call flushes BOTH the SQLite response cache and the in-memory
    surfacing result cache. A filtered call (``server`` and/or ``tool``) targets
    only the response cache — the surfacing cache is keyed by query hash and has
    no server/tool axis, so it cannot be selectively cleared. The startup-only
    tool-graph consult disk cache is not touched (it only affects the next
    restart).

    Args:
        server: If given, only clear response-cache entries for this upstream server name (the name used in mms add, not the prefix).
        tool: If given, only clear response-cache entries for this tool (across all servers, or scoped to server if both provided).
    """
    app = _get_ctx(ctx)
    pm = app.proxy_manager
    proxy_cache = getattr(pm, "_cache", None)
    engine = app.surfacing_engine

    # Filtered: response cache only (surfacing has no server/tool dimension).
    # Preserve the original proxy-only behavior and messages exactly.
    if server or tool:
        if proxy_cache is None:
            return "Cache not enabled. Set proxy.cache.enabled = true in stm_proxy.json."
        removed = proxy_cache.clear(server=server, tool=tool)
        # Escaped for the reply only. These are echoed back to the client, and
        # the SDK's serialization of a ``TextContent`` refuses a lone
        # surrogate — so guarding the SQLite bind alone would just move the
        # failure one step further out (#781).
        shown_server = escape_lone_surrogates(server or "")
        shown_tool = escape_lone_surrogates(tool or "")
        if server and tool:
            return f"Cleared {removed} cache entries for {shown_server}/{shown_tool}."
        elif server:
            return f"Cleared {removed} cache entries for server '{shown_server}'."
        else:
            return f"Cleared {removed} cache entries for tool '{shown_tool}'."

    # Unfiltered "clear all": flush each enabled cache independently.
    parts: list[str] = []
    if proxy_cache is not None:
        parts.append(f"{proxy_cache.clear()} response-cache")
    if engine is not None:
        parts.append(f"{engine.clear_cache()} surfacing-cache")
    if not parts:
        return (
            "No caches enabled (response cache and surfacing are both not enabled). "
            "Set proxy.cache.enabled = true and/or surfacing.enabled = true in stm_proxy.json."
        )
    return "Cleared all caches: " + ", ".join(parts) + " entries."


# ---------------------------------------------------------------------------
# Tool: stm_proxy_health
# ---------------------------------------------------------------------------

# Shared by the surfacing breaker line and the per-upstream breaker lines
# (#608) so both render identical state labels.
_CB_STATE_LABELS = {
    "open": "open (failing)",
    "half-open": "half-open (probe eligible)",
    "closed": "closed (healthy)",
}


@_obs_tool
async def stm_proxy_health(
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Check upstream server connectivity and proxy health status."""
    app = _get_ctx(ctx)
    pm = app.proxy_manager

    bootstrap_lines = _surfacing_bootstrap_lines(app)

    # A broken config file is the likeliest cause of the "No upstream
    # servers configured." symptom below — lead with it in both branches.
    config_warning = []
    if app.proxy_config_error:
        config_warning.append(
            "WARNING: proxy config file present but failed to parse — "
            f"running env/default config: {app.proxy_config_error}"
        )

    # NB: the observability-tools discoverability hint (#613) is intentionally
    # NOT emitted here. ``stm_proxy_health`` is itself gated by ``@_obs_tool``,
    # so in the only state where the hint applies (flag off) this tool is not in
    # ``tools/list`` and an MCP client cannot reach it; with the flag on there
    # is nothing to hint. The reachable operator surface is ``mms health`` (a
    # CLI command, always available), which carries the hint instead.
    # OTLP export state belongs in BOTH branches: it is independent of the
    # proxy block, so a control-only server with export enabled must still
    # show it here rather than appearing to have no telemetry at all.
    otlp_lines = _otlp_health_lines()

    health = pm.get_upstream_health()
    if not health:
        head = "No upstream servers configured."
        return "\n".join([*config_warning, head, *bootstrap_lines, *otlp_lines])

    lines = [*config_warning, "Upstream Server Health", "====================="]
    for name, info in health.items():
        status = "connected" if info["connected"] else "DISCONNECTED"
        lines.append(
            f"  {name}: {status} "
            f"({info['tools']} tools discovered, {info['advertised_tools']} advertised)"
        )
        # A startup-failed upstream (#580) carries the connect error so the
        # DISCONNECTED status is actionable, not just visible.
        error = info.get("error")
        if error:
            lines.append(f"      startup connect failed: {error}")
        # Per-upstream circuit breaker (#608). Key absent = breaker disabled
        # (circuit_max_failures=0) or startup-failed server (no connection) —
        # render nothing rather than a misleading "closed (healthy)".
        circuit_state = info.get("circuit_state")
        if circuit_state is not None:
            cb_label = _CB_STATE_LABELS.get(circuit_state, circuit_state)
            reset_in = info.get("circuit_reset_in")
            suffix = f", retry in ~{reset_in:.0f}s" if reset_in is not None else ""
            lines.append(f"      circuit breaker: {cb_label}{suffix}")

    surfacing = app.surfacing_engine
    if surfacing is not None:
        cb = surfacing._circuit_breaker
        # Render all three states from the pure ``cb.state`` (#600), not just
        # the open/closed split off ``is_open``: once the reset window elapses
        # an open breaker reads as ``half-open`` with ``is_open == False``, so
        # an ``is_open``-only check would report ``closed (healthy)`` before any
        # probe has actually succeeded — hiding a still-degraded dependency.
        cb_state = _CB_STATE_LABELS.get(cb.state, cb.state)
        lines.append(f"\nSurfacing circuit breaker: {cb_state}")

    lines.extend(_toolgraph_health_lines(pm.get_toolgraph_status()))

    lines.extend(bootstrap_lines)
    lines.extend(otlp_lines)
    return "\n".join(lines)


def _toolgraph_health_lines(status: dict | None) -> list[str]:
    """Render the external tool-graph eligibility provider status (#465).

    Empty when the provider is disabled. When enabled, the line makes a
    DEGRADED or withhold-all posture loud so an operator never mistakes a
    skipped external rule family for active enforcement.
    """
    if status is None:
        return []
    lines = ["", "Tool-graph eligibility provider", "=============================="]
    if status["withholding_all"]:
        lines.append(
            f"  WITHHOLDING ALL tools — consult failed ({status['withholding_all']}), "
            "knob is 'closed'"
        )
    elif status["degraded"] and status.get("using_last_known_good"):
        lines.append(
            f"  DEGRADED — bundle reload failed ({status['degraded_reason']}); "
            "last-known-good policy remains active"
        )
    elif status["degraded"]:
        lines.append(
            f"  DEGRADED — external enforcement NOT active ({status['degraded_reason']}); "
            "advertising per STM-native rules only"
        )
    elif status["graph_generation"] is None:
        # Enabled, no failure, but no usable generation → the consult was
        # skipped (no upstream tools discovered). Not "active" enforcement.
        lines.append("  enabled, not consulted (no upstream tools discovered)")
    else:
        cache_note = ", from cache" if status.get("from_cache") else ""
        source_note = ", portable bundle" if status.get("source") == "bundle" else ""
        line = (
            f"  active (graph generation {status['graph_generation']}{cache_note}{source_note}, "
            f"{status['external_reject_count']} tool(s) rejected by the graph"
        )
        risk_count = status.get("risk_penalty_count", 0)
        if risk_count:
            line += f"; {risk_count} carry a graph risk penalty"
        lines.append(line + ")")
        if status.get("source") == "bundle":
            lines.append(
                f"  graph instance: {status.get('graph_instance_id')}; "
                f"bundle digest: {status.get('bundle_digest')}"
            )
            lines.append(f"  review would-block calls: {status.get('would_block_calls', 0)}")
    if not status["withholding_all"]:
        # Also render under review-mode DEGRADED: the last-known-good snapshot
        # is still enforcing and still rebinds against a changing catalog, so
        # an all-bind failure there is live, not history. Fail-closed is the one
        # state to skip — nothing is withheld for a binding reason (the manager
        # clears the diagnosis when that supersedes binding, so this is belt and
        # braces).
        lines.extend(_toolgraph_bind_failure_lines(status))
    return lines


# Cause → the one thing an operator should go check. A catalog-wide bind
# failure is almost never N independent problems; naming the likely single
# cause is the whole point of surfacing this at all.
_BIND_FAILURE_HINTS: dict[str, str] = {
    "unmapped": (
        "the bundle maps none of them — check that toolgraph.server_name_map "
        "matches the server names Toolgraph crawled"
    ),
    "drifted": (
        "every contract digest disagrees — the bundle is likely built from a "
        "stale catalog, or its producer's digest algorithm no longer matches "
        "this STM version"
    ),
    "mixed": (
        "none of them bind — check toolgraph.server_name_map and republish the "
        "bundle from the current catalog"
    ),
}


def _toolgraph_bind_failure_lines(status: dict) -> list[str]:
    """Name the likely cause when policy binding rejected the WHOLE catalog.

    Without this the posture reads as ordinary enforcement: under ``strict``
    every tool is withheld, and ``external_reject_count`` alone cannot tell a
    mass misconfiguration from a catalog of deliberate denials.
    """
    cause = status.get("all_bind_failure")
    if not cause:
        return []
    stats = status.get("bind_stats") or {}
    total = stats.get("catalog_total", 0)
    hint = _BIND_FAILURE_HINTS.get(cause, "check toolgraph.server_name_map and the bundle")
    lines = [f"  ALL {total} live tool(s) failed to bind: {hint}"]
    if cause == "mixed":
        lines.append(
            f"  ({stats.get('stm_unmapped', 0)} unmapped, {stats.get('stm_drifted', 0)} drifted)"
        )
    return lines


def _otlp_health_lines() -> list[str]:
    """Render the OTLP exporter's counters, or nothing when export is off.

    ``export_failures`` counts failed export attempts, not lost spans — the
    batch processor's own queue-overflow drops are logged by the SDK and are
    deliberately not folded in here.
    """
    try:
        from memtomem_stm.observability.otlp import get_otlp

        emitter = get_otlp()
        if emitter is None:
            return []
        counters = emitter.snapshot()
    except Exception:
        return []
    return [
        "",
        "OTLP Span Export",
        "================",
        f"  spans: {counters['spans_started']} started, {counters['spans_ended']} ended",
        f"  export failures: {counters['export_failures']}",
        f"  attributes redacted: {counters['attributes_redacted']}",
        f"  logs redacted: {counters['logs_redacted']}",
        f"  shutdown flush timeouts: {counters['shutdown_flush_timeout']}",
    ]


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
# Tool: stm_memory_propose (opt-in, review-first)
# ---------------------------------------------------------------------------


def _escape_within(value: str, limit: int) -> str | None:
    """``value`` with lone surrogates escaped, or ``None`` if it exceeds ``limit``.

    Every client string reaching ``stm_memory_propose`` needs escaping before
    it is used at all: a lone surrogate is unencodable, and the digest's
    ``.encode()`` and the SDK's serialization of the outbound ``mem_do`` params
    both refuse one.

    The limit is authoritative on the ESCAPED form, which is the only
    denomination that holds on both routes. In daemon mode these same three
    limits are re-applied by ``daemon/server.py`` to what we actually send, and
    its rejection is an opaque ``status="invalid"`` that reaches the client as
    ``candidate_submit_failed``. Measuring the pre-escape form would accept
    here what the far side refuses, so a value near the limit would be
    delivered or not depending on the transport.

    Hence the two checks. The raw one is not a duplicate: escaping expands a
    code unit to six characters and so can only ever lengthen a string, which
    makes an already-oversized input refusable before the scan-and-expand runs
    rather than after allocating up to six times its size. It therefore refuses
    exactly what the second check would have, only sooner (#777).
    """
    if len(value) > limit:
        return None
    escaped = escape_lone_surrogates(value)
    return None if len(escaped) > limit else escaped


@_formation_tool
async def stm_memory_propose(
    content: str,
    source_ref: str = "",
    idempotency_key: str = "",
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Submit a pending memory candidate to a compatible memtomem core.

    This never writes durable memory. Review the returned candidate with the
    core's ``mm review`` or candidate-review MCP action; only core-side
    approval may promote it to long-term or Pinned Context.
    """

    def response(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False)

    app = _get_ctx(ctx)
    cfg = app.config.formation
    if not cfg.enabled:
        return response({"ok": False, "reason": "formation_disabled"})
    stripped = content.strip()
    if not stripped:
        return response({"ok": False, "reason": "content_empty"})
    body = _escape_within(stripped, cfg.max_content_chars)
    if body is None:
        return response(
            {
                "ok": False,
                "reason": "content_too_large",
                "max_content_chars": cfg.max_content_chars,
            }
        )
    ref = _escape_within(source_ref.strip(), 512)
    if ref is None:
        return response({"ok": False, "reason": "source_ref_too_large"})
    key = _escape_within(idempotency_key.strip(), 256)
    if key is None:
        return response({"ok": False, "reason": "idempotency_key_too_large"})
    key = key or hashlib.sha256(f"memtomem-stm\0{ref}\0{body}".encode()).hexdigest()
    if app.surfacing_engine is None:
        return response({"ok": False, "reason": "ltm_unavailable"})
    try:
        result = await app.surfacing_engine.propose_candidate(
            body,
            source="memtomem-stm",
            source_ref=ref,
            idempotency_key=key,
        )
    except Exception:
        logger.warning("Review-first candidate submission failed", exc_info=True)
        return response({"ok": False, "reason": "candidate_submit_failed"})
    if result is None:
        return response({"ok": False, "reason": "formation_unsupported"})
    allowed = {"candidate_id", "status", "created_at", "duplicate", "review_hint"}
    payload = {key: value for key, value in result.items() if key in allowed}
    payload.setdefault("ok", True)
    payload.setdefault("status", "pending")
    payload.setdefault("review_hint", "Run `mm review list` and approve or reject the candidate.")
    return response(payload)


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
        rating: One of 'helpful', 'partially_helpful', 'not_relevant',
            'already_known' (legacy single-rating path).
            ``partially_helpful`` is neutral — useful context but not
            directly used; it counts toward the auto-tune denominator
            but contributes to neither the raise nor the lower band.
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
            # Legacy single-memory path requires a rating — the param is
            # optional only to admit the batched ``ratings`` shape. Reject a
            # ``memory_id``-only call so ``None`` never reaches the callee's
            # ``rating: str`` (this also narrows ``rating`` for the type check).
            if rating is None:
                return "Error: `rating` is required for single-memory feedback."
            return await app.surfacing_engine.handle_feedback(surfacing_id, rating, memory_id)
        if app.feedback_tracker is None:
            return "Feedback tracking is not enabled."
        if ratings is not None:
            return _record_batched_via_tracker(app.feedback_tracker, surfacing_id, ratings)
        if rating is None:
            return "Error: `rating` is required for single-memory feedback."
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
    try:
        require_utf8_identifier(tool, "tool")
    except ValueError as exc:
        return f"Error: {exc}"

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

        # #560 step 3: zero-variance tripwire. A flat score distribution
        # means the upstream score channel carries no ranking information
        # (e.g. the compact-format 2-decimal rounding artifact) — the
        # min_score filter degenerates to a step function and the
        # auto-tuner moves along a gradient that doesn't exist. Rendered
        # from the same filtered event set as the counts above, so a
        # ``tool=`` / ``since=`` window warns about exactly the rows it
        # shows. ``.get`` guards older/mocked stats dicts without the key.
        sd = stats.get("score_distribution") or {}
        if (
            sd.get("count", 0) >= _FLAT_SCORE_WARNING_MIN_SAMPLES
            and sd.get("min") is not None
            and sd["min"] == sd["max"]
        ):
            lines.append(
                f"\nWARNING: zero score variance — all {sd['count']} recorded "
                f"scores in this window equal {sd['min']:.4f}. The upstream "
                "relevance score carries no ranking information here, so "
                "min_score filtering and auto-tune have no signal to act on. "
                "Check the LTM search path / result_format (#560)."
            )

        # #1781: per-scale event counts for the same filtered window. "unknown"
        # buckets rows the core did not label (pre-#1781 cores, compose bundles,
        # legacy rows), so the counts sum to events_total. A mix of scales in
        # one window flags an LTM whose scale shifted mid-window — the exact
        # miscalibration the score_scale_mismatch diagnostic is about. ``.get``
        # guards older/mocked stats dicts without the key.
        ssd = stats.get("score_scale_distribution") or {}
        if ssd:
            rendered = ", ".join(f"{label} {count}" for label, count in sorted(ssd.items()))
            lines.append(f"Score scales:    {rendered}")

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
            scale_info = snap.get("score_scale") or {}
            last_reported = scale_info.get("last_reported")
            if last_reported:
                reranker_id = scale_info.get("reranker")
                reranker_note = f"; reranker: {reranker_id}" if reranker_id else ""
                gate_note = (
                    "; min_score filter suspended for unpinned tools"
                    if scale_info.get("filter_suspended")
                    else ""
                )
                lines.append(
                    f"Score scale:     {last_reported} (core-reported{reranker_note}{gate_note})"
                )
            else:
                lines.append("Score scale:     unknown (core did not report one)")
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
# Tool: stm_selection_stats
# ---------------------------------------------------------------------------


@_obs_tool
async def stm_selection_stats(
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Show tool-selection telemetry recorded by the proxy (#467).

    Surfaces the selection/execution log the proxy writes when
    ``proxy.selection_telemetry.enabled`` is set — the substrate for
    offline replay/eval (#468). Two views:

    - **Live counters** — this process's write-path counters (events
      written / sampled out / redaction drops / write errors), reset at
      restart.
    - **Persisted aggregate** — read back off the active JSONL log: event
      counts, selections by ranker version (the #468 cohort split), by
      server and tool, execution ok/error + latency percentiles, and the
      #465 hard-filter reject-reason tally.

    Takes no arguments. Rotated backups are noted but not parsed (the
    active log only).
    """
    app = _get_ctx(ctx)
    pm = app.proxy_manager
    if pm is None:
        return "Proxy is not enabled."
    sink = pm.selection_log
    if sink is None:
        return (
            "Selection telemetry is disabled "
            "(set proxy.selection_telemetry.enabled = true to record it)."
        )

    with traced("stm_selection_stats"):
        agg = aggregate_selection_log(sink.path)
        live = sink.snapshot()
        # An empty disk log with no live activity is genuinely "no data".
        # But if events were written and then rotated/sampled away, the live
        # counters still explain where they went — render rather than hide.
        if (not agg["exists"] or agg["total_lines"] == 0) and not any(live.values()):
            return f"No selection telemetry recorded yet at {agg['path']}."
        lines = ["Selection Stats", "==============="]
        lines.extend(_format_selection_stats_sections(agg, live))
        return "\n".join(lines)


def _append_top_section(lines: list[str], title: str, top: list[list[Any]], distinct: int) -> None:
    """Append a ``title`` section listing ``[key, count]`` rows, if any.

    Notes truncation as ``(top N of M)`` when the aggregate held more
    distinct keys than the rendered slice, so a capped list never reads as
    the full set.
    """
    if not top:
        return
    suffix = f" (top {len(top)} of {distinct})" if distinct > len(top) else ""
    lines.append(f"\n{title}{suffix}:")
    for key, count in top:
        lines.append(f"  {key}: {count}")


def _format_selection_stats_sections(agg: dict, live: dict[str, int]) -> list[str]:
    """Render the sections for ``stm_selection_stats``.

    Returns lines (no leading blank; caller appends to the header). ``agg``
    is an ``aggregate_selection_log`` result (persisted, off disk); ``live``
    is ``SelectionTelemetryLog.snapshot`` (this process only).
    """
    lines: list[str] = [f"\nLog: {agg['path']}"]
    if agg["rotated_backups"]:
        lines.append(f"  ({agg['rotated_backups']} rotated backup(s) present — not included below)")

    lines.append("\nLive counters (this process):")
    for label in ("events_written", "events_sampled_out", "redaction_drops", "write_errors"):
        lines.append(f"  {label}: {live[label]}")

    ev = agg["events"]
    lines.append("\nEvents (persisted):")
    for label in ("selection", "execution", "feedback"):
        lines.append(f"  {label}: {ev[label]}")
    if agg["malformed"]:
        lines.append(f"  malformed (skipped): {agg['malformed']}")

    if agg["by_ranker_version"]:
        lines.append("\nSelections by ranker version:")
        for rv, count in agg["by_ranker_version"]:
            lines.append(f"  {rv}: {count}")

    _append_top_section(lines, "Selections by server", agg["by_server"], agg["by_server_distinct"])
    _append_top_section(
        lines, "Selections by tool", agg["by_selected_tool"], agg["by_selected_tool_distinct"]
    )

    if ev["execution"]:
        out = agg["outcomes"]
        lines.append("\nExecution outcomes:")
        lines.append(f"  ok: {out['ok']}")
        lines.append(f"  error: {out['error']}")
        lines.append(f"  error_rate: {out['error_rate']:.4f}")
        lat = agg["latency_ms"]
        if lat["count"]:
            lines.append(
                f"  latency_ms p50/p95/p99: {lat['p50']} / {lat['p95']} / {lat['p99']} "
                f"(n={lat['count']})"
            )
        cache = agg["cache"]
        if cache["hit"] or cache["miss"] or cache["unknown"]:
            unknown = f", unknown {cache['unknown']}" if cache["unknown"] else ""
            lines.append(
                f"  cache hit/miss: {cache['hit']} / {cache['miss']} "
                f"(hit_rate {cache['hit_rate']:.4f}{unknown})"
            )
    _append_top_section(
        lines, "Execution error types", agg["by_error_type"], agg["by_error_type_distinct"]
    )
    _append_top_section(
        lines,
        "Reject reasons (#465 hard filter)",
        agg["reject_reasons"],
        agg["reject_reasons_distinct"],
    )

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
    try:
        require_utf8_identifier(tool, "tool")
    except ValueError as exc:
        return f"Error: {exc}"

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

    Also surfaces primary-store *degradation*: when the primary
    ``PROGRESSIVE`` store path fails, the proxy degrades to an
    uncached full-content passthrough (recorded in the metrics
    store as ``→passthrough_on_error``). That count is reported
    here — independently of the reads tracker — so a failing
    backing store does not go silent.

    Args:
        tool: Optional filter by upstream tool name.
    """
    try:
        require_utf8_identifier(tool, "tool")
    except ValueError as exc:
        return f"Error: {exc}"

    app = _get_ctx(ctx)

    # Primary-store degradation (a failed PROGRESSIVE store path degrading to
    # an uncached full-content passthrough) is recorded in the metrics store,
    # independently of the reads tracker. Compute it up front so the fault
    # stays visible even when reads tracking itself is disabled.
    degradation = ""
    metrics_store = app.tracker._metrics_store
    if metrics_store is not None:
        deg = metrics_store.get_progressive_degradations(tool=tool)
        if deg["total"] > 0:
            deg_lines = [
                f"\nPrimary-store degradation (last 24h): {deg['total']} passthrough-on-error"
            ]
            for entry in deg["by_server_tool"][:10]:
                deg_lines.append(f"  {entry['server']}/{entry['tool']}: {entry['count']}")
            degradation = "\n".join(deg_lines)

    if app.progressive_reads_tracker is None:
        if degradation:
            return (
                "Progressive Reads Stats\n=======================\n"
                "(reads tracking disabled)" + degradation
            )
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

    if degradation:
        lines.append(degradation)

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
    read-only — apply them with ``mms tune --apply`` (or edit
    ``stm_proxy.json`` manually).

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
    # STMConfig folds MEMTOMEM_STM_LOG_LEVEL and MEMTOMEM_STM_LOG_FILE into
    # one env surface (#612). A bad env value used to fall back to WARNING
    # silently here while the lifespan's own STMConfig() raised the same
    # ValidationError moments later WITHOUT any logging configured — failing
    # here, after a plain stderr basicConfig, is the readable version.
    try:
        startup_config = STMConfig()
    except Exception as exc:
        logging.basicConfig(level=logging.WARNING, format=STDERR_FORMAT)
        # The traceback renders the failing LOCATION; the hint renders the
        # variable name the operator has to edit, which for a missing field is
        # never the location itself (#835).
        logger.exception(
            "Invalid MEMTOMEM_STM_* environment configuration%s",
            env_var_hint_for_validation_error(exc),
        )
        raise
    configure_server_logging(startup_config)
    # Exception barrier (#209): without this, an unhandled exception from
    # ``mcp.run()`` (e.g. a background task crashing the event loop) ends the
    # process with only stderr output — operators get no ERROR-level log, and
    # clients see stdio EOF without any signal about WHY STM died. Re-raise
    # after logging so the process still terminates; we only add observability.
    try:
        mcp.run()
    except (RuntimeError, ExceptionGroup) as e:
        # The bare RuntimeError occurs when the cancel-scope error escapes
        # outside any task group; the ExceptionGroup shape is what anyio's
        # strict task groups actually deliver on stdio EOF (#410 follow-up).
        if is_clean_cancel_scope_shutdown(e):
            logger.debug(
                "STM MCP server ignored a known AnyIO cancel-scope cleanup condition: %s", e
            )
            return
        logger.exception("STM MCP server terminated with an unhandled exception")
        raise
    except Exception:
        logger.exception("STM MCP server terminated with an unhandled exception")
        raise


if __name__ == "__main__":
    main()
