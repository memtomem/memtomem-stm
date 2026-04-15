"""Proxy manager — upstream MCP server connection, tool discovery, and forwarding."""

from __future__ import annotations

import asyncio
import logging
import time as _time
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from memtomem_stm.proxy.cache import ProxyCache
    from memtomem_stm.proxy.protocols import FileIndexer
    from memtomem_stm.proxy.relevance import RelevanceScorer
    from memtomem_stm.surfacing.engine import SurfacingEngine

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.compression import (
    HybridCompressor,
    LLMCompressor,
    SelectiveCompressor,
    TruncateCompressor,
    auto_select_strategy,
    get_compressor,
)
from memtomem_stm.proxy.config import (
    CleaningConfig,
    CompressionStrategy,
    HybridConfig,
    LLMCompressorConfig,
    ProgressiveConfig,
    ProxyConfig,
    ProxyConfigLoader,
    SelectiveConfig,
    TransportType,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.extraction import FactExtractor
from memtomem_stm.proxy.memory_ops import auto_index_response, extract_and_store, format_fact_md
from memtomem_stm.proxy.tool_metadata import (
    convention_suffix,
    distill_schema,
    truncate_description,
)
from memtomem_stm.proxy.progressive import (
    ProgressiveChunker,
    ProgressiveResponse,
    ProgressiveStoreAdapter,
)
from memtomem_stm.proxy.metrics import CallMetrics, ErrorCategory, TokenTracker
from memtomem_stm.observability.tracing import traced

# JSON-RPC error codes that indicate bad input, not connection problems.
# Retrying these wastes time and can damage the connection.
_NO_RETRY_CODES = {-32600, -32601, -32602, -32603}  # INVALID_REQUEST/METHOD/PARAMS/INTERNAL

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProxyToolInfo:
    prefixed_name: str
    description: str
    input_schema: dict[str, Any]
    server: str
    original_name: str
    annotations: Any = None  # MCP ToolAnnotations (readOnlyHint, destructiveHint, etc.)


@dataclass(frozen=True, slots=True)
class ToolConfig:
    """Resolved per-tool configuration for compression/indexing/extraction."""

    compression: CompressionStrategy
    max_chars: int
    llm: LLMCompressorConfig | None
    auto_index_enabled: bool
    selective: SelectiveConfig | None
    cleaning: CleaningConfig
    hybrid: HybridConfig | None
    extraction_enabled: bool = False
    progressive: ProgressiveConfig | None = None
    retention_floor: float | None = None


@dataclass
class UpstreamConnection:
    name: str
    config: UpstreamServerConfig
    session: ClientSession
    tools: list[Any]
    stack: AsyncExitStack | None = None


def _mark_recorded(exc: BaseException) -> None:
    """Tag *exc* so the outer ``call_tool`` wrapper does not double-record it.

    The pipeline records its own typed metrics rows for upstream / transport /
    timeout / protocol errors. The ``call_tool`` outer wrapper catches anything
    else as ``INTERNAL_ERROR``; this marker keeps the two paths from racing.
    """
    try:
        exc._stm_metrics_recorded = True  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass


class ProxyManager:
    def __init__(
        self,
        config: ProxyConfig,
        tracker: TokenTracker,
        index_engine: FileIndexer | None = None,
        surfacing_engine: SurfacingEngine | None = None,
        cache: ProxyCache | None = None,
        env_overrides: dict[str, Any] | None = None,
    ) -> None:
        self._config_loader = ProxyConfigLoader(config.config_path, env_overrides=env_overrides)
        self._config_loader.seed(config)
        self.tracker = tracker
        self._index_engine = index_engine
        self._surfacing_engine = surfacing_engine
        self._cache = cache
        self._connections: dict[str, UpstreamConnection] = {}
        self._stack: AsyncExitStack | None = None
        self._selective_compressor: SelectiveCompressor | None = None
        self._selective_lock = asyncio.Lock()
        self._extractor: FactExtractor | None = None
        self._extractor_lock = asyncio.Lock()
        self._progressive_store: ProgressiveStoreAdapter | None = None
        self._progressive_lock = asyncio.Lock()
        self._llm_compressor: LLMCompressor | None = None
        self._llm_compressor_cfg: LLMCompressorConfig | None = None
        self._llm_compressor_lock = asyncio.Lock()
        self._relevance_scorer_instance = self._create_scorer(config)
        self._relevance_scorer_cfg = config.relevance_scorer
        self._background_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Connect to all upstream servers, discover their tools."""
        # Guard against double start — close previous stack to avoid leaking connections
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                logger.debug("Failed to close previous stack in double-start guard", exc_info=True)
            self._connections.clear()
        self._stack = AsyncExitStack()

        servers = self._config.upstream_servers
        if not servers:
            loaded = ProxyConfig.load_from_file(self._config.config_path)
            servers = loaded.upstream_servers if loaded else {}

        # Warn about dangerous config: compression active but auto_index disabled
        # means compressed-away content is permanently lost (no LTM recovery).
        ai_cfg = self._config.auto_index
        if ai_cfg.enabled and self._index_engine is None:
            logger.warning(
                "auto_index.enabled=true but no index engine configured — "
                "indexed content will not be stored"
            )
        for srv_name, srv_cfg in servers.items():
            if (
                srv_cfg.compression not in (CompressionStrategy.NONE, CompressionStrategy.AUTO)
                and not ai_cfg.enabled
            ):
                logger.warning(
                    "Server '%s' uses compression=%s but auto_index is disabled — "
                    "compressed-away content is permanently lost",
                    srv_name,
                    srv_cfg.compression.value,
                )

        seen_prefixed: set[str] = set()
        for name, cfg in servers.items():
            try:
                await self._connect_server(name, cfg, seen_prefixed)
            except Exception:
                logger.exception("Failed to connect to upstream server '%s'", name)

    def _open_transport(self, cfg: UpstreamServerConfig):  # noqa: ANN201
        match cfg.transport:
            case TransportType.SSE:
                return sse_client(cfg.url, headers=cfg.headers)
            case TransportType.STREAMABLE_HTTP:
                return streamablehttp_client(cfg.url, headers=cfg.headers)
            case _:
                return stdio_client(
                    StdioServerParameters(command=cfg.command, args=cfg.args, env=cfg.env)
                )

    async def _connect_server(
        self, name: str, cfg: UpstreamServerConfig, seen_prefixed: set[str]
    ) -> None:
        if self._stack is None:
            raise RuntimeError("ProxyManager.start() not called")

        if cfg.transport != TransportType.STDIO and not cfg.url:
            logger.warning("Skipping server '%s': transport=%s requires url", name, cfg.transport)
            return

        transport_ctx = self._open_transport(cfg)
        streams = await self._stack.enter_async_context(transport_ctx)
        read, write = streams[0], streams[1]
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), timeout=cfg.connect_timeout_seconds)

        result = await session.list_tools()
        valid_tools = []
        for t in result.tools:
            prefixed = f"{cfg.prefix}__{t.name}"
            if prefixed in seen_prefixed:
                logger.warning("Skipping duplicate tool: %s", prefixed)
                continue
            seen_prefixed.add(prefixed)
            valid_tools.append(t)

        self._connections[name] = UpstreamConnection(
            name=name, config=cfg, session=session, tools=valid_tools
        )
        logger.info("Connected to '%s' (%s tools)", name, len(valid_tools))

    async def _reconnect_server(self, name: str) -> None:
        conn = self._connections[name]
        cfg = conn.config

        if conn.stack is not None:
            try:
                await conn.stack.aclose()
            except Exception:
                logger.debug("Failed to close previous stack for '%s'", name, exc_info=True)

        conn_stack = AsyncExitStack()
        transport_ctx = self._open_transport(cfg)
        streams = await conn_stack.enter_async_context(transport_ctx)
        read, write = streams[0], streams[1]
        session = await conn_stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), timeout=cfg.connect_timeout_seconds)
        result = await session.list_tools()

        conn.session = session
        conn.stack = conn_stack
        conn.tools = list(result.tools)
        logger.info("Reconnected to '%s' (%s tools)", name, len(conn.tools))

    async def stop(self) -> None:
        # Cancel and drain background tasks (extraction, etc.)
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        # Close httpx clients
        if self._llm_compressor is not None:
            await self._llm_compressor.close()
            self._llm_compressor = None
        if self._extractor is not None:
            await self._extractor.close()
        for conn in self._connections.values():
            if conn.stack is not None:
                try:
                    await conn.stack.aclose()
                except Exception:
                    logger.debug("Failed to close connection stack", exc_info=True)
        if self._stack:
            await self._stack.aclose()
            self._stack = None
        self._connections.clear()

    @property
    def _config(self) -> ProxyConfig:
        return self._config_loader.get()

    @property
    def _relevance_scorer(self) -> "RelevanceScorer":
        """Return the cached scorer, recreating if config changed via hot-reload."""
        current_cfg = self._config.relevance_scorer
        if current_cfg != self._relevance_scorer_cfg:
            self._relevance_scorer_instance = self._create_scorer(self._config)
            self._relevance_scorer_cfg = current_cfg
        return self._relevance_scorer_instance

    # Delegates to proxy.tool_metadata module (backward-compatible)
    _truncate_description = staticmethod(truncate_description)
    _distill_schema = staticmethod(distill_schema)
    _convention_suffix = staticmethod(convention_suffix)

    def get_proxy_tools(self) -> list[ProxyToolInfo]:
        result: list[ProxyToolInfo] = []
        global_max_desc = self._config.max_description_chars
        global_strip = self._config.strip_schema_descriptions

        for conn in self._connections.values():
            cfg = conn.config
            max_desc = cfg.max_description_chars
            strip = cfg.strip_schema_descriptions or global_strip

            for t in conn.tools:
                # Check per-tool hidden override
                override = cfg.tool_overrides.get(t.name)
                if override is not None and override.hidden:
                    continue

                # Resolve effective compression + hybrid config for convention suffix
                effective_compression = cfg.compression
                effective_hybrid = cfg.hybrid
                if override is not None:
                    if override.compression is not None:
                        effective_compression = override.compression
                    if override.hybrid is not None:
                        effective_hybrid = override.hybrid

                suffix = self._convention_suffix(effective_compression, effective_hybrid)

                # Resolve description (reduce budget by suffix length)
                desc = t.description or ""
                if override is not None and override.description_override is not None:
                    desc = override.description_override
                budget = min(max_desc, global_max_desc)
                if suffix:
                    # Reserve room for suffix + possible "..." from truncation
                    budget = max(budget - len(suffix) - 3, 40)
                desc = self._truncate_description(desc, budget)
                if suffix:
                    desc = desc + suffix

                # Resolve schema
                schema = t.inputSchema or {"type": "object"}
                if strip:
                    schema = self._distill_schema(schema, True)

                result.append(
                    ProxyToolInfo(
                        prefixed_name=f"{cfg.prefix}__{t.name}",
                        description=desc,
                        input_schema=schema,
                        server=conn.name,
                        original_name=t.name,
                        annotations=getattr(t, "annotations", None),
                    )
                )
        return result

    @staticmethod
    def _create_scorer(config: ProxyConfig) -> RelevanceScorer:
        """Create a RelevanceScorer from proxy config."""
        from memtomem_stm.proxy.relevance import create_scorer

        sc = config.relevance_scorer
        return create_scorer(
            scorer_type=sc.scorer,
            provider=sc.embedding_provider,
            model=sc.embedding_model,
            base_url=sc.embedding_base_url,
            timeout=sc.embedding_timeout,
        )

    def _create_selective(self, sel_cfg: SelectiveConfig | None) -> SelectiveCompressor:
        """Create a SelectiveCompressor with the appropriate PendingStore backend."""
        kwargs: dict[str, Any] = {}
        store = None
        if sel_cfg is not None:
            kwargs = {
                "max_pending": sel_cfg.max_pending,
                "pending_ttl_seconds": sel_cfg.pending_ttl_seconds,
                "json_depth": sel_cfg.json_depth,
                "min_section_chars": sel_cfg.min_section_chars,
            }
            if sel_cfg.pending_store == "sqlite":
                from memtomem_stm.proxy.pending_store import SQLitePendingStore

                store = SQLitePendingStore(sel_cfg.pending_store_path.expanduser())
                store.initialize()
        if store is not None:
            kwargs["store"] = store
        return SelectiveCompressor(**kwargs)

    def _resolve_tool_config(
        self, server: str, tool: str, proxy_cfg: ProxyConfig | None = None
    ) -> ToolConfig:
        config = proxy_cfg or self._config
        conn = self._connections[server]
        cfg = conn.config

        compression = cfg.compression
        # Use model-aware budget if server uses default max_result_chars
        _default_server_max = UpstreamServerConfig.model_fields["max_result_chars"].default
        if cfg.max_result_chars == _default_server_max:
            max_chars = config.effective_max_result_chars()
        else:
            max_chars = cfg.max_result_chars
        llm_cfg = cfg.llm
        sel_cfg = cfg.selective
        hybrid_cfg = cfg.hybrid
        cleaning_cfg = cfg.cleaning or CleaningConfig()

        auto_index_enabled = config.auto_index.enabled
        if cfg.auto_index is not None:
            auto_index_enabled = cfg.auto_index

        extraction_enabled = config.extraction.enabled
        if cfg.extraction is not None:
            extraction_enabled = cfg.extraction

        progressive_cfg = cfg.progressive
        retention_floor = cfg.retention_floor

        override = cfg.tool_overrides.get(tool)
        if override is not None:
            if override.compression is not None:
                compression = override.compression
            if override.max_result_chars is not None:
                max_chars = override.max_result_chars
            if override.retention_floor is not None:
                retention_floor = override.retention_floor
            if override.llm is not None:
                llm_cfg = override.llm
            if override.selective is not None:
                sel_cfg = override.selective
            if override.hybrid is not None:
                hybrid_cfg = override.hybrid
            if override.progressive is not None:
                progressive_cfg = override.progressive
            if override.cleaning is not None:
                cleaning_cfg = override.cleaning
            if override.auto_index is not None:
                auto_index_enabled = override.auto_index
            if override.extraction is not None:
                extraction_enabled = override.extraction

        return ToolConfig(
            compression=compression,
            max_chars=max_chars,
            llm=llm_cfg,
            auto_index_enabled=auto_index_enabled,
            selective=sel_cfg,
            cleaning=cleaning_cfg,
            hybrid=hybrid_cfg,
            extraction_enabled=extraction_enabled,
            progressive=progressive_cfg,
            retention_floor=retention_floor,
        )

    def _clean_content(self, text: str, cleaning_cfg: CleaningConfig) -> str:
        if not cleaning_cfg.enabled:
            return text
        return DefaultContentCleaner(cleaning_cfg).clean(text)

    async def _apply_compression(
        self,
        text: str,
        compression: CompressionStrategy,
        max_chars: int,
        sel_cfg: SelectiveConfig | None,
        llm_cfg: LLMCompressorConfig | None,
        hybrid_cfg: HybridConfig | None,
        server: str,
        tool: str,
        *,
        context_query: str | None = None,
    ) -> tuple[str, str | None]:
        """Return (compressed_text, llm_fallback_reason_or_None)."""
        if compression == CompressionStrategy.AUTO:
            resolved = auto_select_strategy(text, max_chars=max_chars)
            logger.debug("auto_select_strategy → %s for %s/%s", resolved.value, server, tool)
            if resolved == CompressionStrategy.NONE:
                return text, None
            return await self._apply_compression(
                text,
                resolved,
                max_chars,
                sel_cfg,
                llm_cfg,
                hybrid_cfg,
                server,
                tool,
                context_query=context_query,
            )

        if compression == CompressionStrategy.HYBRID:
            result = await self._apply_hybrid(
                text, max_chars, hybrid_cfg, sel_cfg, context_query=context_query
            )
            return result, None

        if compression == CompressionStrategy.SELECTIVE:
            async with self._selective_lock:
                if self._selective_compressor is None:
                    self._selective_compressor = self._create_selective(sel_cfg)
            return self._selective_compressor.compress(text, max_chars=max_chars), None

        if compression == CompressionStrategy.LLM_SUMMARY:
            if llm_cfg is not None:
                async with self._llm_compressor_lock:
                    if self._llm_compressor is None or self._llm_compressor_cfg != llm_cfg:
                        if self._llm_compressor is not None:
                            await self._llm_compressor.close()
                        self._llm_compressor = LLMCompressor(llm_cfg)
                        self._llm_compressor_cfg = llm_cfg
                result = await self._llm_compressor.compress(text, max_chars=max_chars)
                return result, self._llm_compressor.last_fallback
            logger.warning(
                "LLM_SUMMARY requested for %s/%s but no llm config found; falling back to truncate",
                server,
                tool,
            )
            return (
                TruncateCompressor(scorer=self._relevance_scorer).compress(
                    text, max_chars=max_chars
                ),
                "no_config",
            )

        if compression == CompressionStrategy.TRUNCATE:
            return (
                TruncateCompressor(scorer=self._relevance_scorer).compress(
                    text, max_chars=max_chars, context_query=context_query
                ),
                None,
            )

        return get_compressor(compression).compress(text, max_chars=max_chars), None

    async def _apply_surfacing(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        text: str,
        *,
        trace_id: str | None = None,
    ) -> str:
        """Apply proactive memory surfacing if eligible."""
        if self._surfacing_engine is None:
            return text
        try:
            return await self._surfacing_engine.surface(
                server=server,
                tool=tool,
                arguments=arguments,
                response_text=text,
                trace_id=trace_id,
            )
        except Exception:
            logger.warning(
                "Surfacing failed for %s/%s, using compressed response",
                server,
                tool,
                exc_info=True,
            )
            return text

    async def _apply_hybrid(
        self,
        text: str,
        max_chars: int,
        hybrid_cfg: HybridConfig | None,
        sel_cfg: SelectiveConfig | None,
        *,
        context_query: str | None = None,
    ) -> str:
        cfg = hybrid_cfg or HybridConfig()
        async with self._selective_lock:
            if self._selective_compressor is None:
                self._selective_compressor = self._create_selective(sel_cfg)

        compressor = HybridCompressor(
            head_chars=cfg.head_chars,
            tail_mode=cfg.tail_mode,
            min_toc_budget=cfg.min_toc_budget,
            min_head_chars=cfg.min_head_chars,
            head_ratio=cfg.head_ratio,
            selective_compressor=self._selective_compressor,
        )
        return compressor.compress(text, max_chars=max_chars, context_query=context_query)

    async def _auto_index_response(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        text: str,
        agent_summary: str,
        compression_strategy: str | None = None,
        original_chars: int | None = None,
        compressed_chars: int | None = None,
        context_query: str | None = None,
    ) -> str:
        if self._index_engine is None:
            raise RuntimeError("index_engine not available")
        return await auto_index_response(
            index_engine=self._index_engine,
            ai_cfg=self._config.auto_index,
            server=server,
            tool=tool,
            arguments=arguments,
            text=text,
            agent_summary=agent_summary,
            compression_strategy=compression_strategy,
            original_chars=original_chars,
            compressed_chars=compressed_chars,
            context_query=context_query,
        )

    async def _get_extractor(self) -> FactExtractor:
        async with self._extractor_lock:
            if self._extractor is None:
                self._extractor = FactExtractor(self._config.extraction)
            return self._extractor

    async def _extract_and_store(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        text: str,
        *,
        context_query: str | None = None,
    ) -> None:
        """Extract facts from response and store as individual memory entries."""
        extractor = await self._get_extractor()
        await extract_and_store(
            index_engine=self._index_engine,
            extractor=extractor,
            ext_cfg=self._config.extraction,
            server=server,
            tool=tool,
            arguments=arguments,
            text=text,
            context_query=context_query,
        )

    # Backward-compatible delegate
    _format_fact_md = staticmethod(format_fact_md)

    def select_chunks(self, key: str, sections: list[str]) -> str:
        if self._selective_compressor is None:
            return "Selective compression not active — no pending TOC selections."
        return self._selective_compressor.select(key, sections)

    def _get_progressive_store(self) -> ProgressiveStoreAdapter:
        if self._progressive_store is None:
            from memtomem_stm.proxy.pending_store import InMemoryPendingStore

            self._progressive_store = ProgressiveStoreAdapter(InMemoryPendingStore())
        return self._progressive_store

    def _apply_progressive(self, text: str, cfg: ProgressiveConfig, server: str, tool: str) -> str:
        store = self._get_progressive_store()
        store.evict(cfg.ttl_seconds, cfg.max_stored)

        key = uuid.uuid4().hex[:16]
        resp = ProgressiveResponse(
            content=text,
            total_chars=len(text),
            total_lines=text.count("\n") + 1,
            content_type=ProgressiveChunker.detect_content_type(text),
            structure_hint=ProgressiveChunker.structure_hint(text),
            created_at=_time.monotonic(),
            ttl_seconds=cfg.ttl_seconds,
        )
        store.put(key, resp)

        chunker = ProgressiveChunker(
            chunk_size=cfg.chunk_size,
            include_hint=cfg.include_structure_hint,
        )
        return chunker.first_chunk(text, key, ttl_seconds=cfg.ttl_seconds)

    def read_more(self, key: str, offset: int, limit: int | None = None) -> str:
        """Return next chunk from a progressive delivery response."""
        store = self._get_progressive_store()
        resp = store.get(key)
        if resp is None:
            return f"Progressive delivery key '{key}' not found or expired."
        store.touch(key)
        chunk_size = limit or 4000
        chunker = ProgressiveChunker(chunk_size=chunk_size, include_hint=True)
        return chunker.read_chunk(
            resp.content, offset, limit, key=key, ttl_seconds=resp.ttl_seconds
        )

    def get_upstream_health(self) -> dict[str, dict]:
        """Return per-server health: connection status, tool count."""
        health: dict[str, dict] = {}
        for name, conn in self._connections.items():
            health[name] = {
                "connected": conn.session is not None,
                "tools": len(conn.tools),
            }
        return health

    async def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> str | list:
        """Forward a tool call to upstream, compress, surface, and return.

        Wraps the entire call pipeline in a Langfuse observation span when
        Langfuse is configured. The span carries ``server``, ``tool``, and
        ``trace_id`` metadata so it can be correlated with the matching row
        in ``proxy_metrics.db``. When Langfuse is not configured, ``traced()``
        returns ``nullcontext()`` and the wrapper is a no-op — no perf cost,
        no behavior change for users who don't opt in.
        """
        if server not in self._connections:
            raise KeyError(f"Unknown upstream server: '{server}'")
        trace_id = uuid.uuid4().hex[:16]
        with traced(
            "proxy_call",
            metadata={"server": server, "tool": tool, "trace_id": trace_id},
        ):
            try:
                return await self._call_tool_inner(server, tool, arguments, trace_id=trace_id)
            except Exception as exc:
                # Upstream/transport/timeout/protocol errors are already
                # recorded inside _call_tool_inner via record_error(); a raise
                # that escapes to here means a CLEAN/COMPRESS/SURFACE/INDEX
                # stage threw after the upstream call had already returned.
                # Without this guard the metrics row was silently skipped,
                # leaving operators blind to in-pipeline failures.
                if not getattr(exc, "_stm_metrics_recorded", False):
                    try:
                        self.tracker.record_error(
                            CallMetrics(
                                server=server,
                                tool=tool,
                                original_chars=0,
                                compressed_chars=0,
                                trace_id=trace_id,
                                error_category=ErrorCategory.INTERNAL_ERROR,
                            )
                        )
                    except Exception:
                        logger.debug("Failed to record INTERNAL_ERROR metrics row", exc_info=True)
                raise

    async def _call_tool_inner(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        trace_id: str | None = None,
    ) -> str | list:
        # Public entry point ``call_tool`` generates the trace_id and passes
        # it in so it can match the enclosing Langfuse span. Direct callers
        # (tests and internal dispatch) that don't care about tracing omit
        # the argument and we generate one here.
        if trace_id is None:
            trace_id = uuid.uuid4().hex[:16]
        logger.debug("trace_id=%s server=%s tool=%s", trace_id, server, tool)

        # Snapshot config once to avoid intra-request inconsistency from
        # hot-reload changing the config between accesses.
        cfg_snap = self._config

        # Extract _context_query before forwarding
        context_query = arguments.get("_context_query") if arguments else None
        upstream_args = (
            {k: v for k, v in arguments.items() if k != "_context_query"} if arguments else {}
        )

        # ── Cache lookup ──
        if self._cache is not None:
            cached = self._cache.get(server, tool, upstream_args)
            if cached is not None:
                self.tracker.record_cache_hit()
                # Re-apply surfacing on cache hit so memories stay fresh.
                # Use original arguments (with _context_query) so the
                # surfacing engine can use the agent's explicit query hint.
                with traced(
                    "proxy_call_cache_hit",
                    metadata={"server": server, "tool": tool},
                ):
                    cached = await self._apply_surfacing(
                        server, tool, arguments, cached, trace_id=trace_id
                    )
                return cached
            self.tracker.record_cache_miss()

        conn = self._connections[server]
        cfg = conn.config
        delay = cfg.reconnect_delay_seconds

        # Propagate trace context to upstream server for end-to-end correlation.
        if trace_id is not None:
            upstream_args["_trace_id"] = trace_id

        for attempt in range(cfg.max_retries + 1):
            try:
                result = await conn.session.call_tool(tool, upstream_args)
                break
            except Exception as exc:
                err_code = getattr(getattr(exc, "error", None), "code", None)
                # Only retry transport/connection errors and MCP errors.
                # Programming errors (TypeError, AttributeError, etc.)
                # propagate immediately to avoid masking bugs.
                if (
                    not isinstance(exc, (OSError, ConnectionError, asyncio.TimeoutError, EOFError))
                    and err_code is None
                ):
                    self.tracker.record_error(
                        CallMetrics(
                            server=server,
                            tool=tool,
                            original_chars=0,
                            compressed_chars=0,
                            is_error=True,
                            error_category=ErrorCategory.PROGRAMMING,
                            trace_id=trace_id,
                        )
                    )
                    _mark_recorded(exc)
                    raise

                # Protocol errors (bad params, unknown method) — don't retry,
                # reconnect to keep the connection healthy for the next call.
                if err_code in _NO_RETRY_CODES:
                    logger.debug(
                        "Protocol error %s for %s/%s, skipping retry", err_code, server, tool
                    )
                    self.tracker.record_error(
                        CallMetrics(
                            server=server,
                            tool=tool,
                            original_chars=0,
                            compressed_chars=0,
                            is_error=True,
                            error_category=ErrorCategory.PROTOCOL,
                            error_code=err_code,
                            trace_id=trace_id,
                        )
                    )
                    _mark_recorded(exc)
                    try:
                        await self._reconnect_server(server)
                    except Exception:
                        # Expected fallback: the primary error is already being
                        # re-raised and carries the actionable trace. The
                        # reconnect attempt here is best-effort for the NEXT
                        # call — a failure is noise for current operators.
                        logger.debug("Post-protocol-error reconnect failed", exc_info=True)
                    raise

                if attempt >= cfg.max_retries:
                    cat = (
                        ErrorCategory.TIMEOUT
                        if isinstance(exc, asyncio.TimeoutError)
                        else ErrorCategory.TRANSPORT
                    )
                    self.tracker.record_error(
                        CallMetrics(
                            server=server,
                            tool=tool,
                            original_chars=0,
                            compressed_chars=0,
                            is_error=True,
                            error_category=cat,
                            trace_id=trace_id,
                        )
                    )
                    _mark_recorded(exc)
                    # Reconnect before raising so the NEXT call starts fresh
                    try:
                        await self._reconnect_server(server)
                    except Exception:
                        # Same reasoning as the protocol-error path above:
                        # the primary failure is being re-raised, the
                        # reconnect attempt is just pre-emptive cleanup.
                        logger.debug("Post-failure reconnect failed", exc_info=True)
                    raise
                logger.warning(
                    "Tool call %s/%s failed (attempt %d/%d): %s",
                    server,
                    tool,
                    attempt + 1,
                    cfg.max_retries,
                    exc,
                )
                await asyncio.sleep(delay)
                delay = min(max(delay * 2, 0.1), cfg.max_reconnect_delay_seconds)
                self.tracker.record_reconnect()
                try:
                    await self._reconnect_server(server)
                    conn = self._connections[server]
                except Exception as reconnect_exc:
                    logger.error("Reconnect to '%s' failed: %s", server, reconnect_exc)
                    raise

        # Separate text and non-text content
        text_parts: list[str] = []
        non_text_content: list = []
        for content in result.content:
            if content.type == "text":
                text_parts.append(content.text)
            else:
                non_text_content.append(content)

        # Non-text only → pass through without compression but record metrics
        if not text_parts:
            if non_text_content:
                self.tracker.record(
                    CallMetrics(
                        server=server,
                        tool=tool,
                        original_chars=0,
                        compressed_chars=0,
                        trace_id=trace_id,
                    )
                )
                return non_text_content
            return "[empty response]"

        original_text = "\n".join(text_parts)

        if result.isError:
            self.tracker.record_error(
                CallMetrics(
                    server=server,
                    tool=tool,
                    original_chars=len(original_text),
                    compressed_chars=len(original_text),
                    is_error=True,
                    error_category=ErrorCategory.UPSTREAM_ERROR,
                    trace_id=trace_id,
                )
            )
            # Propagate upstream isError so FastMCP sets isError=true on the
            # proxied response instead of silently converting to a normal result.
            from mcp.server.fastmcp.exceptions import ToolError

            tool_err = ToolError(original_text)
            _mark_recorded(tool_err)
            raise tool_err

        # Resolve effective settings (using config snapshot)
        tc = self._resolve_tool_config(server, tool, proxy_cfg=cfg_snap)

        # ── Stage 1: CLEAN ──
        with traced(
            "proxy_call_clean",
            metadata={"server": server, "tool": tool},
        ):
            _t0 = _time.monotonic()
            cleaned = self._clean_content(original_text, tc.cleaning)
            _clean_ms = (_time.monotonic() - _t0) * 1000

        # ── Stage 2: COMPRESS (or PROGRESSIVE) ──
        # ``effective_compression`` is the strategy actually used (with AUTO
        # already resolved). ``ratio_violation`` is set by the post-compression
        # guard below when the compressor cut more than ``min_result_retention``
        # allows — it feeds into metrics for auditing R4 after the fact.
        effective_compression: CompressionStrategy = tc.compression
        ratio_violation = False
        _pre_scorer_fb = getattr(self._relevance_scorer, "fallback_count", 0)
        if tc.compression == CompressionStrategy.PROGRESSIVE and tc.progressive:
            pcfg = tc.progressive
            if len(cleaned) <= pcfg.chunk_size:
                # Content fits in one chunk — passthrough
                compressed = cleaned
            else:
                compressed = self._apply_progressive(cleaned, pcfg, server, tool)
            _compress_ms = 0.0
            compressed_chars_for_metrics = len(cleaned)
            # Skip surfacing for progressive — injecting memories would shift offsets
            _surface_ms = 0.0
            surfaced = compressed
        else:
            # Enforce minimum retention: budget must preserve at least N% of cleaned content.
            # Dynamic scaling: shorter content → higher retention (less to gain from cutting).
            # This is the SINGLE place where retention is enforced — compressors trust max_chars.
            effective_max_chars = tc.max_chars
            min_retention = getattr(cfg_snap, "min_result_retention", 0.65)
            dynamic = 0.0  # effective retention floor applied to this call (0 = unset)
            if min_retention > 0:
                n = len(cleaned)
                if tc.retention_floor is not None:
                    # Per-tool override from config (set by operator or auto-tuner).
                    dynamic = tc.retention_floor
                elif n < 1000:
                    dynamic = max(min_retention, 0.9)
                elif n < 3000:
                    dynamic = max(min_retention, 0.75)
                elif n < 10000:
                    dynamic = max(min_retention, 0.65)
                else:
                    dynamic = min_retention  # use config value for very large content
                min_budget = int(n * dynamic)
                if effective_max_chars < min_budget:
                    effective_max_chars = min_budget

            # Resolve AUTO early so downstream metrics know which strategy ran.
            if effective_compression == CompressionStrategy.AUTO:
                effective_compression = auto_select_strategy(cleaned, max_chars=effective_max_chars)

            with traced(
                "proxy_call_compress",
                metadata={
                    "server": server,
                    "tool": tool,
                    "strategy": effective_compression.value,
                    "max_chars": effective_max_chars,
                },
            ):
                _t0 = _time.monotonic()
                compressed, llm_fallback = await self._apply_compression(
                    cleaned,
                    effective_compression,
                    effective_max_chars,
                    tc.selective,
                    tc.llm,
                    tc.hybrid,
                    server,
                    tool,
                    context_query=context_query,
                )
                _compress_ms = (_time.monotonic() - _t0) * 1000

            # ── Compression ratio guard (R4 defense + fallback ladder) ──
            # When the compressor cuts below the dynamic retention floor,
            # try progressive delivery first (zero-loss, agent retrieves via
            # stm_proxy_read_more).  If progressive fails (store error, etc.),
            # fall back to boundary-aware TruncateCompressor at the effective
            # budget — lossy but immediate and guaranteed.
            # PROGRESSIVE is excluded above — it is zero-loss by construction.
            cleaned_len = len(cleaned)
            metrics_strategy = effective_compression.value
            if llm_fallback:
                metrics_strategy = f"llm_summary→{llm_fallback}_fallback"
            progressive_fallback = False  # set when progressive replaces the response
            if cleaned_len > 0 and dynamic > 0:
                compressed_ratio = len(compressed) / cleaned_len
                if compressed_ratio < dynamic:
                    ratio_violation = True
                    # SELECTIVE returns a compact TOC by design — the agent
                    # retrieves full content via stm_proxy_select_chunks.
                    # Replacing the TOC would break the two-phase protocol.
                    if effective_compression == CompressionStrategy.SELECTIVE:
                        logger.warning(
                            "Compression ratio below floor for %s/%s: %.3f < %.3f "
                            "(strategy=selective — no fallback, TOC is intentionally compact)",
                            server,
                            tool,
                            compressed_ratio,
                            dynamic,
                        )
                    else:
                        original_strategy = effective_compression.value
                        pcfg = tc.progressive or ProgressiveConfig()
                        hybrid_fallback = False
                        # Tier 1: progressive (zero-loss, best-effort).
                        # Skip when content fits in a single chunk —
                        # progressive adds footer overhead without benefit
                        # (has_more=False, nothing to read_more).
                        if cleaned_len > pcfg.chunk_size:
                            try:
                                compressed = self._apply_progressive(cleaned, pcfg, server, tool)
                                metrics_strategy = f"{original_strategy}→progressive_fallback"
                                progressive_fallback = True
                                logger.info(
                                    "Progressive fallback for %s/%s: %s "
                                    "(ratio %.3f < %.3f, ttl=%ds)",
                                    server,
                                    tool,
                                    metrics_strategy,
                                    compressed_ratio,
                                    dynamic,
                                    int(pcfg.ttl_seconds),
                                )
                            except Exception:
                                logger.debug(
                                    "Progressive fallback failed for %s/%s, "
                                    "falling through to hybrid/truncate",
                                    server,
                                    tool,
                                    exc_info=True,
                                )
                        # Tier 2: hybrid (structure-preserving, best-effort).
                        # Fires when progressive didn't run or failed, AND the
                        # content has enough heading structure for head+TOC to
                        # be meaningful.  Minimum 3 headings — below that,
                        # truncate loses little structural information.
                        _MIN_HEADINGS_FOR_HYBRID = 3
                        if not progressive_fallback:
                            heading_count = cleaned.count("\n#")
                            if heading_count >= _MIN_HEADINGS_FOR_HYBRID:
                                try:
                                    compressed = await self._apply_hybrid(
                                        cleaned,
                                        effective_max_chars,
                                        tc.hybrid,
                                        tc.selective,
                                        context_query=context_query,
                                    )
                                    if len(compressed) / cleaned_len >= dynamic:
                                        metrics_strategy = f"{original_strategy}→hybrid_fallback"
                                        hybrid_fallback = True
                                        logger.info(
                                            "Hybrid fallback for %s/%s: %s (ratio %.3f→%.3f)",
                                            server,
                                            tool,
                                            metrics_strategy,
                                            compressed_ratio,
                                            len(compressed) / cleaned_len,
                                        )
                                except Exception:
                                    logger.debug(
                                        "Hybrid fallback failed for %s/%s, "
                                        "falling through to truncate",
                                        server,
                                        tool,
                                        exc_info=True,
                                    )
                        # Tier 3: truncate (guaranteed floor) — also the
                        # direct path when content is too small for progressive
                        # and lacks structure for hybrid.
                        if not progressive_fallback and not hybrid_fallback:
                            compressed = TruncateCompressor(scorer=self._relevance_scorer).compress(
                                cleaned, max_chars=effective_max_chars
                            )
                            metrics_strategy = f"{original_strategy}→truncate_fallback"
                            logger.warning(
                                "Ratio guard truncate fallback for %s/%s: %s "
                                "(ratio %.3f→%.3f, budget=%d)",
                                server,
                                tool,
                                metrics_strategy,
                                compressed_ratio,
                                (len(compressed) / cleaned_len if cleaned_len else 0),
                                effective_max_chars,
                            )

            # Record metrics BEFORE surfacing (surfacing adds content, not compresses)
            compressed_chars_for_metrics = len(compressed)

            # ── Stage 3: SURFACE (proactive memory injection) ──
            # Skip surfacing when progressive fallback fired — injecting
            # memories would shift character offsets for stm_proxy_read_more.
            if progressive_fallback:
                _surface_ms = 0.0
                surfaced = compressed
            else:
                with traced(
                    "proxy_call_surface",
                    metadata={"server": server, "tool": tool},
                ):
                    _t0 = _time.monotonic()
                    surfaced = await self._apply_surfacing(
                        server, tool, upstream_args, compressed, trace_id=trace_id
                    )
                    _surface_ms = (_time.monotonic() - _t0) * 1000

        # ── Stage 4: INDEX (optional) ──
        ai_cfg = cfg_snap.auto_index
        if (
            tc.auto_index_enabled
            and self._index_engine is not None
            and len(cleaned) >= ai_cfg.min_chars
        ):
            with traced(
                "proxy_call_index",
                metadata={"server": server, "tool": tool},
            ):
                final_result = await self._auto_index_response(
                    server,
                    tool,
                    upstream_args,
                    cleaned,
                    agent_summary=surfaced,
                    compression_strategy=tc.compression.value,
                    original_chars=len(original_text),
                    compressed_chars=len(surfaced),
                    context_query=context_query,
                )
        else:
            final_result = surfaced

        # ── Stage 4b: EXTRACT (optional, background by default) ──
        ext_cfg = cfg_snap.extraction
        if (
            tc.extraction_enabled
            and self._index_engine is not None
            and len(cleaned) >= ext_cfg.min_response_chars
        ):
            if ext_cfg.background:
                task = asyncio.create_task(
                    self._extract_and_store(
                        server,
                        tool,
                        upstream_args,
                        cleaned,
                        context_query=context_query,
                    )
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            else:
                await self._extract_and_store(
                    server,
                    tool,
                    upstream_args,
                    cleaned,
                    context_query=context_query,
                )

        # Record metrics (using pre-surfacing compressed size)
        # Approximate token counts: chars / 3.5 (average for mixed en/code/json).
        # Not exact but sufficient for budget tracking and cost estimation.
        _orig_tokens = max(1, int(len(original_text) / 3.5))
        _comp_tokens = max(1, int(compressed_chars_for_metrics / 3.5))

        self.tracker.record(
            CallMetrics(
                server=server,
                tool=tool,
                original_chars=len(original_text),
                compressed_chars=compressed_chars_for_metrics,
                cleaned_chars=len(cleaned),
                original_tokens=_orig_tokens,
                compressed_tokens=_comp_tokens,
                trace_id=trace_id,
                clean_ms=_clean_ms,
                compress_ms=_compress_ms,
                surface_ms=_surface_ms,
                surfaced_chars=len(surfaced),
                compression_strategy=metrics_strategy,
                ratio_violation=ratio_violation,
                scorer_fallback=(
                    getattr(self._relevance_scorer, "fallback_count", 0) > _pre_scorer_fb
                ),
            )
        )

        # ── Cache store (pre-surfacing content so memories stay fresh on hit) ──
        if self._cache is not None and not non_text_content:
            self._cache.set(
                server,
                tool,
                upstream_args,
                compressed,
                ttl_seconds=cfg_snap.cache.default_ttl_seconds,
            )

        # Combine compressed text with preserved non-text content
        if non_text_content:
            from mcp.types import TextContent

            return [TextContent(type="text", text=final_result), *non_text_content]

        return final_result
