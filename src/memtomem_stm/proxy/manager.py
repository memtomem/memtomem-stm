"""Proxy manager — upstream MCP server connection, tool discovery, and forwarding."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import json
import logging
import sqlite3
import time as _time
import uuid
from collections import Counter
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.types import CallToolResult

    from memtomem_stm.proxy.cache import ProxyCache
    from memtomem_stm.proxy.pending_store import PendingStore
    from memtomem_stm.proxy.protocols import FileIndexer
    from memtomem_stm.proxy.relevance import RelevanceScorer
    from memtomem_stm.proxy.selection_log import SelectionTelemetryLog
    from memtomem_stm.surfacing.engine import SurfacingEngine
    from memtomem_stm.surfacing.observability import SkipReason

from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

from memtomem_stm.proxy import tool_name_budget
from memtomem_stm.proxy.cache import _make_key as _cache_key
from memtomem_stm.proxy.cache import response_carries_transient_key
from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.compression import (
    HybridCompressor,
    LLMCompressor,
    SchemaPruningCompressor,
    SelectiveCompressor,
    SkeletonCompressor,
    TruncateCompressor,
    auto_select_strategy,
    count_markdown_headings,
    get_compressor,
)
from memtomem_stm.proxy.config import (
    CleaningConfig,
    CompressionStrategy,
    ExposureProfile,
    ExtractionStrategy,
    HybridConfig,
    LLMCompressorConfig,
    ProgressiveConfig,
    ProxyConfig,
    ProxyConfigLoader,
    RelevanceScorerConfig,
    SelectiveConfig,
    ToolgraphConfig,
    TransportType,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.extraction import FactExtractor
from memtomem_stm.proxy.index_observability import IndexObservability
from memtomem_stm.proxy.memory_ops import (
    AutoIndexOutcome,
    ExtractOutcome,
    auto_index_response,
    compose_index_footer,
    extract_and_store,
    format_fact_md,
    index_content_matches_privacy,
)
from memtomem_stm.proxy.pipeline_stages import (
    CompressionResult,
    ExtractResult,
    IndexResult,
    ShapedResponse,
    ShapePassthrough,
)
from memtomem_stm.proxy.privacy import CREDENTIAL_PATTERNS as PRIVACY_CREDENTIAL_PATTERNS
from memtomem_stm.proxy.token_estimate import tokens_to_chars
from memtomem_stm.proxy.tool_eligibility import (
    REASON_CONFIG_HIDDEN,
    REASON_PROFILE_EXCLUDED,
    REASON_TOOLGRAPH_AGENT_NOT_FOUND,
    REASON_TOOLGRAPH_PROTOCOL_ERROR,
    REASON_TOOLGRAPH_UNREACHABLE,
    ExposureCandidate,
    InterpretedVerdict,
    compute_health_flags,
    filter_tools,
    interpret_verdict,
    parse_risk_scores,
)
from memtomem_stm.proxy.toolgraph_cache import GraphConsultCache
from memtomem_stm.proxy.toolgraph_provider import (
    TRANSPORT_ERRORS as TOOLGRAPH_TRANSPORT_ERRORS,
    ToolgraphConsultAdapter,
    ToolgraphConsultError,
    ToolgraphProtocolError,
    ToolgraphUnreachableError,
)
from memtomem_stm.proxy.tool_relevance import (
    PENALTY_SOURCE_BOTH,
    PENALTY_SOURCE_GRAPH,
    RANKER_VERSION_BM25,
    RANKER_VERSION_BM25_GRAPH_RISK,
    RANKER_VERSION_BM25_RISK,
    ToolRelevanceRanker,
    build_candidate_features,
    compose_risk_penalty,
    derive_query,
    penalty_source,
)
from memtomem_stm.proxy.tool_metadata import (
    convention_suffix,
    distill_schema,
    truncate_description,
)
from memtomem_stm.proxy.progressive import (
    PROGRESSIVE_FOOTER_TOKEN,
    ProgressiveChunker,
    ProgressiveResponse,
    ProgressiveStoreAdapter,
)
from memtomem_stm.proxy.progressive_reads import ProgressiveReadsTracker
from memtomem_stm.proxy._locks import LockTimeoutError, bounded_lock
from memtomem_stm.proxy.metrics import (
    MAX_ERROR_MESSAGE_CHARS,
    CallMetrics,
    ErrorCategory,
    TokenTracker,
    format_error_message_from_exc,
)
from memtomem_stm.observability.tracing import traced
from memtomem_stm.utils.circuit_breaker import CircuitBreaker
from memtomem_stm.utils.redact import redact_exception_text

# JSON-RPC error codes that indicate bad input, not connection problems.
# Retrying these wastes time and can damage the connection.
_NO_RETRY_CODES = {-32600, -32601, -32602, -32603}  # INVALID_REQUEST/METHOD/PARAMS/INTERNAL

# ToolError message for an upstream isError result whose content carries no
# text at all (non-text-only or empty). ToolError is text-only, so this
# placeholder is the best signal we can propagate; the non-text blocks and any
# structuredContent/_meta on the errored result are dropped (structured-error
# propagation is deferred with structured-result caching).
_NON_TEXT_ERROR_TEXT = "[upstream error: non-text error content]"

logger = logging.getLogger(__name__)


def _describe_llm_destination(llm: LLMCompressorConfig) -> str:
    """Human-readable provider + endpoint for the #610 startup warning."""
    if llm.base_url:
        return f"{llm.provider.value} ({llm.base_url})"
    return llm.provider.value


def _llm_compression_leaks(
    compression: CompressionStrategy, llm: LLMCompressorConfig | None
) -> bool:
    """True when an LLM_SUMMARY path will send raw text UNSCANNED off-machine.

    Requires an explicit ``llm_summary`` strategy (``auto`` resolves at runtime
    and would produce noisy static false positives), an attached LLM config
    with the privacy scan disabled, and an external destination (#610).
    """
    return (
        compression == CompressionStrategy.LLM_SUMMARY
        and llm is not None
        and not llm.privacy_scan_enabled
        and llm.is_external_destination()
    )


# Errors the tool-graph consult treats as "graph unreachable" → on_unreachable.
# ``eligible_tools()`` already wraps transport failures into
# ``ToolgraphUnreachableError``; ``start()`` re-raises them RAW, so the manager
# catches both. Built here (not inline in the ``except``) so mypy sees a typed
# exception tuple rather than a star-unpack.
_TOOLGRAPH_UNREACHABLE_ERRORS: tuple[type[BaseException], ...] = (
    ToolgraphUnreachableError,
    *TOOLGRAPH_TRANSPORT_ERRORS,
)


class ToolgraphStartupError(RuntimeError):
    """A ``fail_start`` tool-graph failure aborts proxy startup (#465).

    Raised from ``ProxyManager.start()`` when an enabled provider's consult
    fails (unreachable / agent-not-found / protocol error) and the matching
    ``on_*`` knob is ``fail_start`` (the default for the contract-class
    failures). Propagates out of ``start()`` so the lifespan refuses to bring
    the proxy up — loud and recoverable, never a silent fail-open. The operator
    sets the knob to ``open`` (degrade) or ``closed`` (withhold all) to choose
    a different posture.
    """


@dataclass(frozen=True, slots=True)
class ProxyToolInfo:
    prefixed_name: str
    description: str
    input_schema: dict[str, Any]
    server: str
    original_name: str
    annotations: Any = None  # MCP ToolAnnotations (readOnlyHint, destructiveHint, etc.)
    # Upstream tools/list envelope fields, advertised verbatim (description
    # budgeting and schema distillation apply to the INPUT side only).
    output_schema: dict[str, Any] | None = None
    meta: dict[str, Any] | None = None  # tool-level ``_meta``


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


# Field classification for ``compression_fingerprint``. Every ``ToolConfig``
# field must appear in exactly one of these sets (pinned by a test), so adding
# a field forces the author to decide whether it can change the compressed
# bytes stored in the response cache.
#   - Fingerprinted: settings that change what COMPRESS (or Stage-1 CLEAN)
#     writes into ``comp.compressed``, or that gate whether a full body may be
#     stored/served at all (``progressive.chunk_size``).
#   - Excluded: Stage-4 side-band stages (auto-index / extraction) consume the
#     pipeline output but never alter the cached payload; keying on them would
#     invalidate the cache on unrelated toggles.
_FINGERPRINT_FIELDS = frozenset(
    {
        "compression",
        "max_chars",
        "retention_floor",
        "cleaning",
        "hybrid",
        "selective",
        "progressive",
        "llm",
    }
)
_FINGERPRINT_EXCLUDED_FIELDS = frozenset({"auto_index_enabled", "extraction_enabled"})


def compression_fingerprint(
    tc: ToolConfig,
    min_result_retention: float,
    max_upstream_chars: int,
    relevance_scorer: RelevanceScorerConfig,
) -> str:
    """SHA-256 fingerprint of the settings that shape the cached response body.

    Folded into the response-cache key so a body produced under one
    configuration is never served after the configuration changes (hot reload
    included). Fingerprints the RESOLVED ``ToolConfig`` — post
    default/server/tool-override merge and token→char budget conversion — so
    the fingerprint changes exactly when the effective settings do, plus the
    ``ProxyConfig`` globals that alter the cached bytes but live outside
    ``ToolConfig``:

    - ``min_result_retention`` — the global floor of the compression ratio
      guard (``manager.py`` Stage 2).
    - ``max_upstream_chars`` — the Stage-3 SHAPE truncation applied to
      ``original_text`` BEFORE cleaning/compression (``_shape_response``); an
      oversized response is cut to this budget, and that cut text is what gets
      compressed and cached, so lowering the limit must rotate the key.
    - ``relevance_scorer`` — the query-aware compressors (TRUNCATE,
      SCHEMA_PRUNING, SKELETON) allocate budget with this scorer, so switching
      bm25↔embedding or changing the embedding model changes the cached bytes
      for a query-bearing call. Those compressors are rebuilt per call from the
      current scorer (``self._relevance_scorer``), so the bytes track the live
      scorer; keying on ``cfg_snap.relevance_scorer`` rotates the key on the
      same change. (The cached SELECTIVE/HYBRID compressor lifecycle does NOT
      reach cached bytes: their cacheable paths are the query-blind truncate
      fallbacks, while the scorer-driven TOC paths carry a transient retrieval
      key and are never stored.)

    Secret / environment-injected fields are excluded so the fingerprint stays
    machine-independent: ``llm.api_key`` (validator injects from env), and the
    scorer's OpenAI embedding key is likewise read from ``OPENAI_API_KEY`` and
    never lives in ``RelevanceScorerConfig``.
    """
    payload: dict[str, Any] = {
        "compression": tc.compression.value,
        "max_chars": tc.max_chars,
        "retention_floor": tc.retention_floor,
        "cleaning": tc.cleaning.model_dump(mode="json"),
        "hybrid": tc.hybrid.model_dump(mode="json") if tc.hybrid is not None else None,
        "selective": tc.selective.model_dump(mode="json") if tc.selective is not None else None,
        "progressive": (
            tc.progressive.model_dump(mode="json") if tc.progressive is not None else None
        ),
        "llm": (
            tc.llm.model_dump(mode="json", exclude={"api_key"}) if tc.llm is not None else None
        ),
        "min_result_retention": min_result_retention,
        "max_upstream_chars": max_upstream_chars,
        "relevance_scorer": relevance_scorer.model_dump(mode="json"),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class UpstreamConnection:
    name: str
    config: UpstreamServerConfig
    session: ClientSession
    tools: list[Any]
    stack: AsyncExitStack | None = None
    # Serialize concurrent ``_reconnect_server`` calls for this server (#586).
    # Four unserialized retry-loop sites can reconnect the same connection at
    # once; without this, two interleaving reconnects each build a fresh
    # ``AsyncExitStack`` and the last writer wins — orphaning the loser's stack
    # (a leaked stdio child + fds per race). The generation counter lets a
    # waiter that wakes after another reconnect already completed skip its own,
    # collapsing a reconnect storm into one transport spawn.
    reconnect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reconnect_generation: int = 0
    # Per-upstream circuit breaker (#608). Lives on the connection because
    # ``_reconnect_server`` mutates the connection in place (never replaces
    # it), so breaker state survives reconnects. ``None`` when the upstream's
    # ``circuit_max_failures`` is 0 (breaker disabled).
    breaker: CircuitBreaker | None = None
    # Damper for config-change reconnects: the ``_connection_fingerprint`` of
    # the last hot-reloaded connection config that FAILED to connect. While
    # the file still carries this exact config, calls keep serving on the old
    # connection instead of re-attempting the broken edit on every request;
    # any further edit (different fingerprint) or any successful reconnect
    # clears it. Lives on the connection for the same stable-identity reason
    # as the lock/generation/breaker.
    last_failed_connection_fp: tuple[Any, ...] | None = None


def _connection_fingerprint(cfg: UpstreamServerConfig) -> tuple[Any, ...]:
    """Frozen view of the fields that define the live transport's identity.

    A change in any of these requires re-establishing the connection to take
    effect (hot-reload classification, PR ⑦); everything else on
    ``UpstreamServerConfig`` is read per call and needs no reconnect. Only the
    fields the ACTIVE transport actually consumes are included, so editing an
    inactive field (a ``command`` on an SSE server, a ``url`` on a stdio one)
    does not churn the connection. ``transport`` itself is always present, so
    switching transport type reconnects. ``connect_timeout_seconds`` counts
    only for network transports, where it is baked into the SDK client factory
    as the httpx connect budget; for stdio it is consumed per connection
    attempt, so an edit simply applies to the next reconnect. The tuple doubles
    as the damping key for failed config-change reconnects.
    """
    if cfg.transport == TransportType.STDIO:
        return (
            cfg.transport,
            cfg.command,
            tuple(cfg.args),
            tuple(sorted((cfg.env or {}).items())),
        )
    return (
        cfg.transport,
        cfg.url,
        tuple(sorted((cfg.headers or {}).items())),
        cfg.connect_timeout_seconds,
    )


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


def _mark_cache_invalidated(exc: BaseException) -> None:
    """Tag *exc* as having already run the disabled-cache invalidation.

    The ``isError`` branch in ``_call_tool_inner`` invalidates a stale row
    (#541) and then raises; the raised-failure backstop in
    ``_call_tool_guarded`` invalidates on any OTHER raise. This marker keeps
    the two from issuing a second DELETE for the same call (same idiom as
    ``_mark_recorded``).
    """
    try:
        exc._stm_cache_invalidated = True  # type: ignore[attr-defined]
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
        progressive_reads_tracker: ProgressiveReadsTracker | None = None,
        selection_log: SelectionTelemetryLog | None = None,
    ) -> None:
        self._config_loader = ProxyConfigLoader(config.config_path, env_overrides=env_overrides)
        self._config_loader.seed(config)
        self.tracker = tracker
        self._index_engine = index_engine
        self._surfacing_engine = surfacing_engine
        self._cache = cache
        self._progressive_reads_tracker = progressive_reads_tracker
        self._selection_log = selection_log
        # Snapshots of the advertisement last returned by
        # ``get_proxy_tools()`` — the candidate set the client model picked
        # from: names go into selection telemetry (#467), the full infos
        # feed tool-relevance ranking (#466), the hard filter's verdict
        # (#465) rides along as reject reasons + review-profile risk
        # penalties. Empty until the first advertisement.
        self._advertised_tools: list[str] = []
        self._advertised_infos: list[ProxyToolInfo] = []
        self._advertised_reject_reasons: dict[str, str] = {}
        # Composed ranking demotions for the advertised set (#466), keyed by
        # prefixed name: the #465 review demotion stacked with the #493 graph
        # risk penalty (``compose_risk_penalty``). ``_sources`` records each
        # one's provenance for #468 replay attribution. Both sparse (penalized
        # tools only) and rebuilt every get_proxy_tools() pass.
        self._advertised_risk_penalties: dict[str, float] = {}
        self._advertised_risk_penalty_sources: dict[str, str] = {}
        # Health flags for the #465 filter, computed ONCE per start() from
        # the persisted metrics store and held for the session — exposure
        # must not drift between the startup advertisement (what the client
        # registered) and later get_proxy_tools() calls (teardown, tests),
        # or telemetry would lie about the candidate set the client saw.
        self._unhealthy_tools: frozenset[tuple[str, str]] = frozenset()
        # #465 optional external tool-graph eligibility provider. Consulted
        # ONCE per start() (beside the health-flag precompute) so the
        # advertised set stays session-stable; the snapshots below feed every
        # get_proxy_tools() pass. ``_toolgraph_external_rejects`` are
        # per-candidate verdicts (profile-gated in filter_tools);
        # ``_toolgraph_withhold_all`` is the whole-call fail-closed code (a
        # ``closed`` knob fired); ``_graph_generation`` pins selection
        # telemetry to the graph state (#468); ``_toolgraph_degraded`` records
        # that a failure resolved to ``open`` so the external rule family was
        # SKIPPED — surfaced loudly so a one-time ``open`` cannot silently
        # become a permanent enforcement blind spot.
        self._toolgraph_external_rejects: dict[tuple[str, str], str] = {}
        # #493 per-candidate graph ``risk_score`` mapped to a relevance
        # ``risk_penalty`` (scaled by ``risk_penalty_scale``), keyed by
        # ``(server, original_name)``. A best-effort enrichment of the consult:
        # ranking telemetry only, never exposure — so a rank_features failure
        # leaves this empty rather than touching the on_* knobs.
        self._toolgraph_risk_penalties: dict[tuple[str, str], float] = {}
        self._toolgraph_withhold_all: str | None = None
        self._graph_generation: int | None = None
        self._toolgraph_degraded: bool = False
        self._toolgraph_degraded_reason: str | None = None
        # #494 consult disk cache (lazy-opened per session, closed on stop() and
        # in the double-start guard so a reconfigured path reopens). ``from_cache``
        # records whether THIS session's verdict was served from a cache hit.
        self._toolgraph_cache: GraphConsultCache | None = None
        self._toolgraph_from_cache: bool = False
        self._connections: dict[str, UpstreamConnection] = {}
        # Configured servers whose startup connect FAILED (#580). Entries never
        # land in ``_connections`` (created only on a successful connect), so
        # without this map a startup-failed upstream is invisible in
        # ``stm_proxy_health`` — an operator sees only healthy servers and no
        # anomaly. name → short error summary; an entry is cleared if the
        # server later connects (e.g. via reconnect).
        self._failed_servers: dict[str, str] = {}
        self._stack: AsyncExitStack | None = None
        self._selective_compressor: SelectiveCompressor | None = None
        self._selective_compressor_cfg: SelectiveConfig | None = None
        self._selective_lock = asyncio.Lock()
        self._extractor: FactExtractor | None = None
        self._extractor_lock = asyncio.Lock()
        # In-memory counters for both INDEX-pipeline write paths
        # (auto_index_response + extract_and_store). Always instantiated for
        # library callers that inspect ``index_observability.snapshot()``.
        # See ``proxy/index_observability.py`` for the counter contract.
        self.index_observability = IndexObservability()
        self._progressive_store: ProgressiveStoreAdapter | None = None
        self._progressive_store_cfg: SelectiveConfig | None = None
        self._progressive_lock = asyncio.Lock()
        self._llm_compressor: LLMCompressor | None = None
        self._llm_compressor_cfg: LLMCompressorConfig | None = None
        self._llm_compressor_lock = asyncio.Lock()
        self._relevance_scorer_instance = self._create_scorer(config)
        self._relevance_scorer_cfg = config.relevance_scorer
        self._background_tasks: set[asyncio.Task] = set()
        # #557 ``tools/list_changed`` refresh bookkeeping. ``dirty`` marks
        # servers whose advertised tool list is known-stale; ``running`` holds
        # servers with a drain task in flight so notification bursts coalesce
        # into one refresh chain. See ``_schedule_tools_refresh``.
        self._tools_refresh_dirty: set[str] = set()
        self._tools_refresh_running: set[str] = set()
        # Per-key stampede guard — identical concurrent ``call_tool`` invocations
        # serialize on the same lock so a cache miss triggers one upstream
        # call rather than N. Entries are popped when the work completes so
        # the dict stays bounded by the number of in-flight unique keys.
        # Named ``_key_locks`` to match the same pattern used by
        # ``SurfacingEngine`` (extractable into a shared helper later).
        self._key_locks: dict[str, asyncio.Lock] = {}
        # F6: one WARNING on first progressive call when ``injection_mode`` is
        # ``"prepend"`` — that mode still skips surfacing on progressive to
        # preserve the ``stm_proxy_read_more`` offset invariant.
        self._warned_prepend_on_progressive = False

    async def start(self) -> None:
        """Connect to all upstream servers, discover their tools."""
        # Guard against double start — close previous stack to avoid leaking connections
        if self._stack is not None:
            for conn in self._connections.values():
                if conn.stack is not None:
                    try:
                        await conn.stack.aclose()
                    except Exception as cleanup_exc:
                        # Redact + no exc_info (#605): this stack wraps a transport
                        # opened with the credentialed ``conn.config.url``, so a
                        # close failure's traceback tail could leak the token even
                        # at DEBUG. Route through the #593 choke point.
                        logger.debug(
                            "Failed to close connection stack for '%s' in double-start guard: %s",
                            conn.name,
                            self._redacted_error(cleanup_exc, conn.config.url),
                        )
            try:
                await self._stack.aclose()
            except Exception:
                logger.debug("Failed to close previous stack in double-start guard", exc_info=True)
            self._connections.clear()
            # Drop stale startup-failure records (#580): a manager reused across
            # ``start()`` calls (new config, or a server removed) must not keep
            # reporting a previous session's failed upstream in
            # ``stm_proxy_health``. The next connect pass repopulates from the
            # current config.
            self._failed_servers.clear()
            # Reset the #557 refresh bookkeeping alongside the connections it
            # tracks (same rationale as in ``stop()``): a ``running`` entry
            # orphaned by a never-started drain task would silently drop every
            # ``list_changed`` notification for that server this session.
            self._tools_refresh_dirty.clear()
            self._tools_refresh_running.clear()
            # Close the consult cache too so a re-entry that changed
            # ``toolgraph.consult_cache_path`` reopens the right DB (#494). Mirror
            # the stack-close try/except above: always null the handle so a failed
            # close cannot leave a stale closed connection for the next start.
            if self._toolgraph_cache is not None:
                try:
                    self._toolgraph_cache.close()
                except Exception:
                    logger.debug(
                        "Failed to close tool-graph consult cache in double-start guard",
                        exc_info=True,
                    )
                self._toolgraph_cache = None
        self._stack = AsyncExitStack()

        servers = self._config.upstream_servers
        if not servers:
            # Fallback re-load of a file the server startup path typically
            # already loaded (env-enabled proxy, upstreams only in the file).
            # log_warnings=False so the advisory permissive-mode /
            # unknown-key warnings don't fire twice per startup (#611);
            # a parse *failure* still logs.
            loaded = ProxyConfig.load_from_file(self._config.config_path, log_warnings=False)
            servers = loaded.upstream_servers if loaded else {}

        ext_cfg = self._config.extraction

        # #610: warn loudly when an LLM path will send raw upstream responses
        # UNSCANNED to an external provider (privacy_scan_enabled=false). The
        # scan is default-on; an operator who flips it off otherwise gets no
        # signal that credential redaction (#289) is now off. Scoped to
        # *external* destinations — a local Ollama endpoint never leaves the
        # machine, so a scan-off local path is not flagged.
        #
        # Compression (Stage 2) runs in every deployment, so its warning is
        # unconditional. Extraction (Stage 4b) only fires when an index engine
        # is wired (gated at the call site, manager.py ~L3768); the standalone
        # ``mms`` server has none, so extraction never reaches the provider
        # there — its warning is gated on the same condition to stay accurate.
        comp_leak_paths: list[str] = []
        comp_dests: set[str] = set()
        default_comp = self._config.default_compression
        for srv_name, srv_cfg in servers.items():
            srv_comp = (
                srv_cfg.compression if "compression" in srv_cfg.model_fields_set else default_comp
            )
            srv_llm = srv_cfg.llm
            srv_leaks = _llm_compression_leaks(srv_comp, srv_llm)
            if srv_leaks and srv_llm is not None:
                comp_leak_paths.append(f"server '{srv_name}'")
                comp_dests.add(_describe_llm_destination(srv_llm))
            for tool_name, override in srv_cfg.tool_overrides.items():
                tool_comp = override.compression if override.compression is not None else srv_comp
                tool_llm = override.llm or srv_llm
                # A tool that inherits both strategy and llm from the server is
                # already covered by the server-level entry — don't double-list.
                inherits = override.compression is None and override.llm is None
                if (
                    _llm_compression_leaks(tool_comp, tool_llm)
                    and tool_llm is not None
                    and not (srv_leaks and inherits)
                ):
                    comp_leak_paths.append(f"server '{srv_name}' tool '{tool_name}'")
                    comp_dests.add(_describe_llm_destination(tool_llm))
        if comp_leak_paths:
            logger.warning(
                "LLM compression enabled (%s) with privacy_scan_enabled=false — raw "
                "upstream responses will be sent UNSCANNED to %s; credential "
                "redaction (#289) is off",
                ", ".join(comp_leak_paths),
                ", ".join(sorted(comp_dests)),
            )

        if self._index_engine is not None:
            ext_llm = ext_cfg.effective_llm()
            if (
                ext_cfg.strategy in (ExtractionStrategy.LLM, ExtractionStrategy.HYBRID)
                and not ext_llm.privacy_scan_enabled
                and ext_llm.is_external_destination()
            ):
                ext_leak_paths: list[str] = []
                if ext_cfg.enabled:
                    ext_leak_paths.append("extraction.enabled")
                for srv_name, srv_cfg in servers.items():
                    if srv_cfg.extraction is True:
                        ext_leak_paths.append(f"server '{srv_name}'")
                    for tool_name, override in srv_cfg.tool_overrides.items():
                        if override.extraction is True:
                            ext_leak_paths.append(f"server '{srv_name}' tool '{tool_name}'")
                if ext_leak_paths:
                    logger.warning(
                        "LLM extraction enabled (%s) with privacy_scan_enabled=false — "
                        "raw upstream responses will be sent UNSCANNED to %s; credential "
                        "redaction (#289) is off",
                        ", ".join(ext_leak_paths),
                        _describe_llm_destination(ext_llm),
                    )

        # Composed-name uniqueness (prefix uniqueness is validated in
        # ``ProxyConfig._check_unique_upstream_prefixes``; ``model_construct()``
        # or a future config source could bypass it) and the 64-char overflow
        # rule are enforced at EXPOSURE time by the #465 eligibility filter
        # in ``get_proxy_tools()`` — the single choke point, so the verdicts
        # reach telemetry and the reconnect path gets the same treatment.
        # Registration iterates the filter's output, so two handlers can
        # never race for one composed name. ``_connect_server`` keeps every
        # discovered tool and only logs guidance.
        for name, cfg in servers.items():
            try:
                await self._connect_server(name, cfg)
            except Exception as exc:
                # Redact the exception text ONCE, then reuse it for both the
                # operator log and the health record (#580). httpx transport
                # exceptions embed the credentialed request URL, so an unredacted
                # path would leak the token — via ``stm_proxy_health`` to the MCP
                # client/model, or via the log. Do NOT use ``logger.exception``:
                # its traceback tail repeats the raw, unredacted exception string.
                redacted = self._redacted_error(exc, cfg.url)
                logger.error("Failed to connect to upstream server '%s': %s", name, redacted)
                # Record the failure so ``get_upstream_health`` can report the
                # configured-but-dead server — otherwise it is absent from
                # ``stm_proxy_health`` entirely (no ``_connections`` entry is
                # created on a failed connect) and the degradation is
                # undiagnosable from inside the session.
                self._failed_servers[name] = redacted

        # #465: evaluate per-tool health once per session, before the first
        # advertisement. get_proxy_tools() applies this cached snapshot so
        # the advertised set stays stable for the session; the next start()
        # re-evaluates (startup-grained half-open probing — see
        # proxy/tool_eligibility.py). Without a metrics store (metrics
        # disabled) there is no health signal and the filter runs on
        # config/structural rules alone.
        exposure_cfg = self._config.exposure
        self._unhealthy_tools = compute_health_flags(self.tracker.metrics_store, exposure_cfg)
        if (
            self.tracker.metrics_store is None
            and exposure_cfg.profile is not ExposureProfile.EXPLORE
        ):
            logger.info(
                "Exposure profile '%s' active without a metrics store — "
                "health-based eligibility signals unavailable",
                exposure_cfg.profile.value,
            )

        # #465: consult the optional external tool-graph eligibility provider
        # once, beside the health-flag precompute, so its verdict joins the
        # exposure filter for the rest of the session. A ``fail_start`` knob
        # raises here — before any tool is advertised — which is the intended
        # loud failure (a broken/typo'd provider must not silently disable
        # enforcement).
        if self._config.toolgraph.enabled:
            await self._consult_toolgraph()

    def _build_toolgraph_candidates(self) -> tuple[dict[str, list[tuple[str, str]]], list[str]]:
        """Map every discovered upstream tool to its graph candidate ref.

        A ref is ``"<graph-server>::<tool>"`` where ``<graph-server>`` is the
        tool-graph's CRAWLED server name — an independent string from STM's own
        connection key, hence the ``server_name_map`` translation (identity
        when unmapped). Bare names risk ``AMBIGUOUS_TOOL``, so refs are always
        server-qualified. Refs are deduplicated for the batch consult; the
        returned map fans one ref's verdict back to EVERY STM
        ``(server, original_name)`` key that shares it (two upstreams mapped to
        one graph server with a same-named tool both inherit that verdict).
        """
        name_map = self._config.toolgraph.server_name_map
        ref_to_keys: dict[str, list[tuple[str, str]]] = {}
        for conn in self._connections.values():
            graph_server = name_map.get(conn.name, conn.name)
            for t in conn.tools:
                ref = f"{graph_server}::{t.name}"
                ref_to_keys.setdefault(ref, []).append((conn.name, t.name))
        return ref_to_keys, list(ref_to_keys.keys())

    async def _consult_toolgraph(self) -> None:
        """Run the one-shot startup consult and cache its verdict (#465).

        Mirrors :func:`compute_health_flags`: consult once, hold the snapshot
        for the session so the advertised set stays stable. The adapter is
        started, consulted, and stopped here — there is no per-request lazy
        start, so nothing holds the stdio child past startup. Failures are
        classified onto the configured ``on_*`` knobs; ``fail_start`` raises
        :class:`ToolgraphStartupError` (out of ``start()``) before any tool is
        advertised.
        """
        cfg = self._config.toolgraph
        # Reset the cached snapshot first: start() is a supported re-entry path
        # (the double-start guard re-runs discovery + compute_health_flags), so
        # a recovered graph on a second start must not inherit the previous
        # session's withhold-all / degraded / reject state.
        self._toolgraph_external_rejects = {}
        self._toolgraph_risk_penalties = {}
        self._toolgraph_withhold_all = None
        self._graph_generation = None
        self._toolgraph_degraded = False
        self._toolgraph_degraded_reason = None
        self._toolgraph_from_cache = False

        ref_to_keys, refs = self._build_toolgraph_candidates()
        if not refs:
            logger.info(
                "Tool-graph provider enabled but no upstream tools were discovered "
                "— consult skipped"
            )
            return

        adapter = ToolgraphConsultAdapter(cfg)
        risk_scores: dict[str, float] = {}
        try:
            try:
                # ``start()`` re-raises raw transport errors (caught below as
                # unreachable); the consult itself is bounded by the adapter's
                # ``timeout_seconds`` (the realistic hang is a slow Neo4j query,
                # which happens during the consult, not during initialize()).
                await adapter.start()
                # #494: probe-then-hit/miss. The probe + any full consult run
                # INSIDE this try, so their transport/protocol faults still ride
                # the on_* knobs below — the disk cache can never mask a degraded
                # graph. Returns the same ``(interp, risk_scores)`` the rest of
                # this method already expects.
                interp, risk_scores = await self._run_consult(adapter, cfg, refs)
            finally:
                try:
                    await adapter.stop()
                except Exception:
                    logger.debug("Tool-graph adapter stop() failed", exc_info=True)
        except _TOOLGRAPH_UNREACHABLE_ERRORS as exc:
            self._tg_whole_call(cfg.on_unreachable, REASON_TOOLGRAPH_UNREACHABLE, str(exc))
            return
        except ToolgraphProtocolError as exc:
            self._tg_whole_call(cfg.on_protocol_error, REASON_TOOLGRAPH_PROTOCOL_ERROR, str(exc))
            return

        # Reachable + well-formed. Pin the generation even on an abort — the
        # graph responded, so it is a meaningful telemetry key.
        self._graph_generation = interp.graph_generation

        if not interp.agent_found:
            self._tg_whole_call(
                cfg.on_agent_not_found,
                REASON_TOOLGRAPH_AGENT_NOT_FOUND,
                f"agent_id {cfg.agent_id!r} is not registered in the tool-graph",
            )
            return

        # Per-candidate verdicts. TOOL_NOT_FOUND (the graph's blind spot) obeys
        # ``on_tool_not_found``: ``open`` keeps the working tool advertised,
        # ``closed`` rejects the uncrawled candidate.
        rejects = dict(interp.rejects)
        if cfg.on_tool_not_found == "open":
            for ref in interp.tool_not_found_refs:
                rejects.pop(ref, None)
        self._toolgraph_external_rejects = {
            key: code for ref, code in rejects.items() for key in ref_to_keys.get(ref, ())
        }
        if self._toolgraph_external_rejects:
            logger.info(
                "Tool-graph consult: %d candidate(s) rejected by the graph (generation %s)",
                len(self._toolgraph_external_rejects),
                self._graph_generation,
            )

        # #493 map each positive ``risk_score`` to a relevance ``risk_penalty``,
        # scaled by ``risk_penalty_scale`` and clamped to the ranker's [0,1]
        # range, fanned to every STM key sharing the ref. Ranking telemetry
        # only — never an exposure input, so a rejected-but-risky ref simply
        # never reaches the ranker (it is outside ``eligible``).
        scale = cfg.risk_penalty_scale
        self._toolgraph_risk_penalties = {
            key: min(score * scale, 1.0)
            for ref, score in risk_scores.items()
            for key in ref_to_keys.get(ref, ())
        }
        if self._toolgraph_risk_penalties:
            logger.info(
                "Tool-graph consult: %d candidate(s) carry a graph risk penalty (generation %s)",
                len(self._toolgraph_risk_penalties),
                self._graph_generation,
            )
        self._warn_server_name_mismatch(interp.tool_not_found_refs, ref_to_keys)

    def _open_consult_cache(self, cfg: ToolgraphConfig) -> GraphConsultCache | None:
        """Lazily open the #494 consult disk cache; ``None`` when disabled.

        Best-effort: a cache that cannot be opened (bad path / perms / disk)
        degrades to no-cache for the session rather than failing startup — the
        consult itself is unaffected. Re-used across the session; the
        double-start guard closes it so a reconfigured path reopens.
        """
        if not cfg.consult_cache_enabled:
            return None
        if self._toolgraph_cache is None:
            try:
                cache = GraphConsultCache(
                    cfg.consult_cache_path.expanduser(),
                    max_scopes=cfg.consult_cache_max_scopes,
                )
                cache.initialize()
            except Exception:
                logger.warning(
                    "Tool-graph consult cache could not be opened at %s — consulting "
                    "without the disk cache this session.",
                    cfg.consult_cache_path,
                    exc_info=True,
                )
                return None
            self._toolgraph_cache = cache
        return self._toolgraph_cache

    async def _run_consult(
        self, adapter: ToolgraphConsultAdapter, cfg: ToolgraphConfig, refs: list[str]
    ) -> tuple[InterpretedVerdict, dict[str, float]]:
        """Probe-then-hit/miss consult (#494). Returns ``(interp, risk_scores)``.

        Model A (strictly-fresh): with the cache enabled, a cheap
        ``eligible_tools([])`` probe reads the live ``graph_generation`` on EVERY
        start; the expensive ``eligible_tools(refs)`` + ``rank_features(refs)``
        evaluation is skipped only when the probed generation (plus provider,
        agent, profile, and candidate set) matches a cached row. Because the
        probe always contacts the graph, a degraded/unreachable graph is always
        re-detected (its fault propagates to the caller's ``on_*`` knobs) and
        never masked by the cache. Sets ``self._toolgraph_from_cache``.

        Only an agent-found full consult is written; a degraded / agent-not-found
        verdict is never cached. ``had_risk_scores`` records whether enrichment
        actually succeeded (not merely that it was wanted), so a transient
        ``rank_features`` failure is not cached as "no penalties".
        """
        want_risk = cfg.risk_penalty_scale > 0.0
        cache = self._open_consult_cache(cfg)
        if cache is None:
            # Cache disabled / unavailable — the pre-#494 path, verbatim.
            interp = interpret_verdict(await adapter.eligible_tools(refs))
            risk_scores: dict[str, float] = {}
            if interp.agent_found and want_risk:
                risk_scores, _ = await self._fetch_risk_scores(adapter, refs)
            return interp, risk_scores

        probe = interpret_verdict(await adapter.eligible_tools([]))
        if not probe.agent_found:
            # Agent unknown — no full consult; the caller maps this onto
            # ``on_agent_not_found`` exactly as the full-verdict abort would.
            return probe, {}

        cand_hash = GraphConsultCache.candidate_hash(refs)
        prov_fp = GraphConsultCache.provider_fingerprint(cfg)
        row = cache.get(prov_fp, cfg.agent_id, cfg.query_profile, cand_hash, probe.graph_generation)
        if row is not None and (row["had_risk_scores"] or not want_risk):
            self._toolgraph_from_cache = True
            interp = InterpretedVerdict(
                agent_found=True,
                rejects=dict(row["rejects"]),
                tool_not_found_refs=frozenset(row["tool_not_found_refs"]),
                graph_generation=probe.graph_generation,
            )
            risk_scores = (
                {ref: float(score) for ref, score in row["risk_scores"].items()}
                if want_risk
                else {}
            )
            return interp, risk_scores

        # Miss — full consult, then cache the raw facts on agent-found success.
        interp = interpret_verdict(await adapter.eligible_tools(refs))
        risk_scores, risk_ok = ({}, False)
        if interp.agent_found and want_risk:
            risk_scores, risk_ok = await self._fetch_risk_scores(adapter, refs)
        if interp.agent_found:
            cache.put(
                prov_fp,
                cfg.agent_id,
                cfg.query_profile,
                cand_hash,
                interp.graph_generation,
                rejects=interp.rejects,
                tool_not_found_refs=interp.tool_not_found_refs,
                risk_scores=risk_scores,
                had_risk_scores=risk_ok,
            )
        return interp, risk_scores

    async def _fetch_risk_scores(
        self, adapter: ToolgraphConsultAdapter, refs: list[str]
    ) -> tuple[dict[str, float], bool]:
        """Best-effort ``rank_features`` enrichment (#493): ``({ref: risk_score}, ok)``.

        The graph's per-candidate ``risk_score`` is a ranking-telemetry signal
        only — never exposure, never a startup gate — so unlike the
        ``eligible_tools`` verdict (whose failures ride the ``on_*`` knobs) any
        fault here degrades silently to "no penalties", logged once at WARNING.

        Returns ``(scores, ok)`` where ``ok`` is ``False`` only on a consult
        error. The flag lets the #494 disk cache distinguish a *successful empty*
        enrichment (cacheable as "risk facts captured") from a *transient
        failure* (must not be cached as success, else a later same-generation
        start would skip enrichment and serve no penalties forever).
        """
        try:
            verdict = await adapter.rank_features(refs)
        except ToolgraphConsultError as exc:
            logger.warning(
                "Tool-graph risk enrichment (rank_features) failed (%s) — ranking "
                "proceeds without graph risk penalties this session.",
                exc,
            )
            return {}, False
        # A non-error response whose ``features`` is not a list is a *malformed*
        # enrichment: ``parse_risk_scores`` leniently yields no penalties, but it
        # must NOT be cached as a successful "no risk facts" capture (#494) — else
        # a later same-generation start would skip ``rank_features`` and serve no
        # penalties forever. Report ``ok=False`` so the cache re-tries (the active
        # session degrades to no penalties either way).
        if not isinstance(verdict.get("features"), list):
            logger.warning(
                "Tool-graph risk enrichment (rank_features) returned a malformed "
                "payload (no 'features' list) — ranking proceeds without graph risk "
                "penalties this session."
            )
            return {}, False
        return parse_risk_scores(verdict), True

    def _tg_whole_call(self, knob: str, code: str, detail: str) -> None:
        """Apply a whole-call tool-graph failure per its ``on_*`` knob.

        ``fail_start`` raises (aborts startup); ``closed`` withholds every tool
        under ``code`` (profile-independent); ``open`` degrades to STM-native
        rules and records the skip loudly so the lost enforcement is never
        silent (a one-time ``open`` must not quietly become a permanent blind
        spot — see ``stm_proxy_health``).
        """
        if knob == "fail_start":
            raise ToolgraphStartupError(
                f"tool-graph consult failed ({code}: {detail}); the matching on_* knob "
                f"is 'fail_start'. Fix the provider, or set the knob to 'open' (degrade "
                f"to STM-native rules) or 'closed' (withhold all tools)."
            )
        if knob == "closed":
            self._toolgraph_withhold_all = code
            logger.warning(
                "Tool-graph consult failed (%s: %s) — knob is 'closed': withholding "
                "ALL tools this session.",
                code,
                detail,
            )
            return
        # "open" — degrade.
        self._toolgraph_degraded = True
        self._toolgraph_degraded_reason = code
        logger.warning(
            "Tool-graph consult failed (%s: %s) — knob is 'open': DEGRADED. The "
            "external eligibility rule family is SKIPPED this session; tools are "
            "advertised per STM-native rules only. Tool-graph enforcement is NOT active.",
            code,
            detail,
        )

    def _warn_server_name_mismatch(
        self,
        tool_not_found_refs: frozenset[str],
        ref_to_keys: dict[str, list[tuple[str, str]]],
    ) -> None:
        """Heuristic: warn when an entire upstream's tools are unknown to the graph.

        STM cannot precisely verify its connection key against the graph's
        crawled server name (they are independent strings and no ``list_servers``
        MCP tool exists to reconcile them), so it infers a likely
        ``server_name_map`` gap: if EVERY candidate from one
        upstream came back ``TOOL_NOT_FOUND`` and that upstream has no map
        entry, the names probably don't line up. Conservative by design (fires
        only at a 100% miss for an unmapped server) so a partially-crawled
        server never trips a false positive.
        """
        name_map = self._config.toolgraph.server_name_map
        sent: Counter[str] = Counter()
        missed: Counter[str] = Counter()
        for ref, keys in ref_to_keys.items():
            for server, _tool in keys:
                sent[server] += 1
                if ref in tool_not_found_refs:
                    missed[server] += 1
        for server, n in sent.items():
            if n > 0 and missed[server] == n and server not in name_map:
                logger.warning(
                    "All %d tool(s) from upstream '%s' are unknown to the tool-graph. "
                    "If '%s' is crawled under a different name, add a "
                    "toolgraph.server_name_map['%s'] entry; otherwise these tools are "
                    "simply not in the graph.",
                    n,
                    server,
                    server,
                    server,
                )

    def _open_transport(self, cfg: UpstreamServerConfig):  # noqa: ANN201
        # ``timeout=`` is the httpx connect budget (the transport-socket leg of
        # the timeout contract); ``sse_read_timeout`` is deliberately left at
        # the SDK default — long-lived streams must not inherit the connect
        # budget or legitimately slow tool calls (bounded separately by
        # ``call_timeout_seconds``) would be killed mid-read.
        match cfg.transport:
            case TransportType.SSE:
                return sse_client(cfg.url, headers=cfg.headers, timeout=cfg.connect_timeout_seconds)
            case TransportType.STREAMABLE_HTTP:
                return streamablehttp_client(
                    cfg.url, headers=cfg.headers, timeout=cfg.connect_timeout_seconds
                )
            case _:
                return stdio_client(
                    StdioServerParameters(command=cfg.command, args=cfg.args, env=cfg.env)
                )

    @staticmethod
    def _redacted_error(exc: BaseException, url: str) -> str:
        """Exception text with *url* userinfo scrubbed, capped for storage/logs
        (#580). One choke point for every credential-safe rendering of a
        connect/cleanup exception: httpx transport errors embed the credentialed
        request URL, so any log or health field built from them must go through
        here. Redacts the FULL message BEFORE the cap so a long credential can't
        be truncated past ``redact_exception_text``'s reach.
        """
        return redact_exception_text(f"{type(exc).__name__}: {exc}", url)[:MAX_ERROR_MESSAGE_CHARS]

    async def _establish_connection(
        self, name: str, cfg: UpstreamServerConfig
    ) -> tuple[ClientSession, AsyncExitStack, list[Any]]:
        """Open transport + session and discover tools under ONE end-to-end
        ``connect_timeout_seconds`` deadline.

        Transport entry, ``initialize()``, and ``tools/list`` each get
        ``deadline - now`` — a slow phase cannot grant later phases a fresh
        budget (same contract as the CLI probe's ``_probe_one``). The session
        ``__aenter__`` is not wrapped: it is an in-process task-group start
        with no I/O, and it still sits inside the wall-clock deadline. On any
        failure the partial stack is rolled back (redacted log — the transport
        was opened with the credentialed ``cfg.url``) and the exception
        re-raised; the caller sees either a fully-discovered connection or
        nothing.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + cfg.connect_timeout_seconds

        def remaining() -> float:
            # Clamp like _probe_one: a deadline that expired between phases
            # still surfaces as asyncio.TimeoutError, not ValueError.
            return max(1e-3, deadline - loop.time())

        conn_stack = AsyncExitStack()
        try:
            streams = await asyncio.wait_for(
                conn_stack.enter_async_context(self._open_transport(cfg)), timeout=remaining()
            )
            session = await conn_stack.enter_async_context(
                ClientSession(
                    streams[0], streams[1], message_handler=self._make_message_handler(name)
                )
            )
            await asyncio.wait_for(session.initialize(), timeout=remaining())
            result = await asyncio.wait_for(session.list_tools(), timeout=remaining())
        except BaseException:
            # Roll back any contexts we entered (transport subprocess, session
            # streams) — including on cancellation, so a timed-out phase can't
            # leak file descriptors or child processes.
            try:
                await conn_stack.aclose()
            except Exception as cleanup_exc:
                # Redact + no exc_info (#580): the transport was opened with
                # the credentialed ``cfg.url``, so a cleanup failure's
                # traceback tail could otherwise leak the token even at DEBUG.
                logger.debug(
                    "Error during connection cleanup for '%s': %s",
                    name,
                    self._redacted_error(cleanup_exc, cfg.url),
                )
            raise
        return session, conn_stack, list(result.tools)

    async def _connect_server(self, name: str, cfg: UpstreamServerConfig) -> None:
        if self._stack is None:
            raise RuntimeError("ProxyManager.start() not called")

        if cfg.transport != TransportType.STDIO and not cfg.url:
            logger.warning("Skipping server '%s': transport=%s requires url", name, cfg.transport)
            # Record the misconfiguration (#580) instead of returning silently:
            # this early return raises nothing, so ``start()``'s except never
            # fires and the configured-but-unconnected server would otherwise
            # stay invisible in ``stm_proxy_health`` — the exact false-green
            # this surfacing is meant to close. Static message (no url), so
            # nothing to redact.
            self._failed_servers[name] = (
                f"configuration error: transport={cfg.transport.value} requires a url"
            )
            return

        session, conn_stack, tools = await self._establish_connection(name, cfg)

        # #261 operator guidance, logged at discovery where the config
        # context is at hand. The tool itself stays in ``conn.tools``: the
        # actual exclusion happens at exposure time in ``get_proxy_tools()``
        # (eligibility filter, reason ``name_overflow``), so the verdict is
        # one choke point and reaches telemetry. Clients (Claude Code,
        # Antigravity, Anthropic SDK) silently drop tools whose composed
        # name overflows the 64-char regex — better one withheld tool than
        # a mystery-missing one.
        for t in tools:
            if tool_name_budget.overflows(cfg.prefix, t.name):
                logger.warning(
                    "Tool '%s' from upstream '%s' will not be advertised: "
                    "composed client name 'mcp__%s__%s__%s' is %d chars, "
                    "exceeds the %d-char MCP limit. Shorten the '%s' prefix "
                    "in stm_proxy.json, or register STM under 'mms' (3 chars) "
                    "in your MCP client config to save 9 chars of overhead.",
                    t.name,
                    name,
                    tool_name_budget.client_server_name(),
                    cfg.prefix,
                    t.name,
                    tool_name_budget.composed_length(cfg.prefix, t.name),
                    tool_name_budget.TOOL_NAME_LIMIT,
                    cfg.prefix,
                )

        breaker = (
            CircuitBreaker(
                max_failures=cfg.circuit_max_failures,
                reset_timeout=cfg.circuit_reset_seconds,
                name=f"upstream-{name}",
            )
            if cfg.circuit_max_failures > 0
            else None
        )
        self._connections[name] = UpstreamConnection(
            name=name,
            config=cfg,
            session=session,
            tools=tools,
            stack=conn_stack,
            breaker=breaker,
        )
        # A successful connect clears any prior startup-failure record (#580).
        self._failed_servers.pop(name, None)
        logger.info("Connected to '%s' (%s tools discovered)", name, len(tools))

    async def _reconnect_server(self, name: str, cfg: UpstreamServerConfig | None = None) -> None:
        conn = self._connections[name]

        # A reconnect applies the config the CALLER resolved and will damp /
        # redact against — passed in so the fingerprint recorded on failure and
        # the url used to scrub the error both match the config actually
        # attempted (a re-read of ``self._config`` here could diverge from the
        # caller's snapshot mid-race, damping the wrong fingerprint and
        # redacting with the wrong token). ``None`` (direct callers / tests)
        # falls back to the current snapshot, and to the connect-time config if
        # the server key vanished from the file — adding/removing servers stays
        # restart-only.
        if cfg is None:
            cfg = self._config.upstream_servers.get(name) or conn.config

        # Serialize concurrent reconnects for this server (#586). Capture the
        # generation BEFORE acquiring: if it advanced while we waited, another
        # caller already reconnected this connection, so we skip — no redundant
        # transport spawn, and no stack to orphan. The lock lives on ``conn``,
        # which is mutated in place (never replaced), so its identity is stable.
        generation_at_entry = conn.reconnect_generation
        async with conn.reconnect_lock:
            if conn.reconnect_generation != generation_at_entry:
                logger.debug(
                    "Skipping reconnect for '%s' — another reconnect already completed", name
                )
                return

            # Prepare-first: build the replacement connection while the old one
            # stays untouched. If _establish_connection raises, ``conn`` still
            # holds the previous session/stack/config — for a config-change
            # reconnect the old (healthy) connection keeps serving, and for a
            # failure-triggered reconnect the caller's raise/skip semantics are
            # unchanged.
            #
            # The establish runs in a CHILD task so the new transport's anyio
            # cancel scopes never land on THIS task's scope stack. With them
            # here, the old-stack aclose() below would be a same-task
            # out-of-order scope exit, and a real stdio transport's close
            # (which cancels its task group) then cancels THIS task — the
            # CancelledError escapes every except-Exception guard and kills
            # the tool call that triggered the reconnect. In the child, the
            # scopes die with the task and the later close of this stack is a
            # cross-task exit — the mode every reconnect-opened stack already
            # exercises in production (opened in a since-finished request
            # task) and the cleanup guards already swallow.
            establish = asyncio.create_task(self._establish_connection(name, cfg))
            try:
                session, conn_stack, tools = await establish
            except asyncio.CancelledError:
                # Our caller was cancelled while we waited: don't orphan the
                # child — its own BaseException handler rolls back the
                # partial stack once the cancel lands.
                establish.cancel()
                raise

            old_stack = conn.stack
            # The old stack wraps a transport opened with the OLD credentialed
            # url — capture it before the swap so the cleanup log redacts the
            # right token.
            old_url = conn.config.url
            conn.session = session
            conn.stack = conn_stack
            conn.tools = tools
            conn.config = cfg
            # Any successful reconnect proves the current config connects —
            # clear the config-change damper so detection resumes normally.
            conn.last_failed_connection_fp = None
            # Bump the generation only on a successful reconnect so a waiter
            # skips its own; a failed attempt leaves it unchanged so the next
            # caller retries rather than silently skipping.
            conn.reconnect_generation += 1

            if old_stack is not None:
                try:
                    await old_stack.aclose()
                except Exception as cleanup_exc:
                    # Redact + no exc_info (#605): a close failure's traceback
                    # tail could leak the token at DEBUG.
                    logger.debug(
                        "Failed to close previous stack for '%s': %s",
                        name,
                        self._redacted_error(cleanup_exc, old_url),
                    )
            logger.info("Reconnected to '%s' (%s tools discovered)", name, len(conn.tools))

    def _make_message_handler(self, name: str) -> Any:
        """Per-upstream MCP message handler wired into ``ClientSession``.

        The only message acted on is ``notifications/tools/list_changed``. The
        cache-eligibility gate (``_tool_cache_eligible``) reads tool annotations
        from the ``conn.tools`` snapshot, which is otherwise populated only at
        connect/reconnect — so an upstream that re-declares a tool from
        read-only to may-mutate at runtime would keep replaying the pre-flip
        cached response until the next error-driven reconnect (#557). Every
        other message (server->client requests, other notifications, stream
        exceptions) falls through untouched, matching the SDK's default
        handler behavior.
        """

        async def _handler(message: Any) -> None:
            # ``ServerNotification`` is a RootModel; requests arrive as
            # ``RequestResponder`` and stream errors as ``Exception``, neither
            # of which carries a ``root`` attribute.
            root = getattr(message, "root", None)
            if isinstance(root, mcp_types.ToolListChangedNotification):
                self._schedule_tools_refresh(name)

        return _handler

    def _schedule_tools_refresh(self, name: str) -> None:
        """Coalesce ``tools/list_changed`` notifications into at most one
        in-flight refresh per server.

        A notification only says "the list changed, re-fetch it", so a burst
        collapses to a single ``list_tools`` as long as that call starts after
        the last notification arrived. Single-threaded event loop: the
        dirty/running bookkeeping here and in ``_drain_tools_refresh`` runs
        without an intervening ``await``, so the handler and the drain task
        cannot interleave mid-decision.
        """
        self._tools_refresh_dirty.add(name)
        if name in self._tools_refresh_running:
            return
        self._tools_refresh_running.add(name)
        task = asyncio.create_task(self._drain_tools_refresh(name))
        self._background_tasks.add(task)
        task.add_done_callback(
            functools.partial(self._on_background_task_done, "tools_refresh", name, "*")
        )

    async def _drain_tools_refresh(self, name: str) -> None:
        """Refresh until no notification arrived during the previous pass.

        If a refresh raises (upstream torn down mid-call, transport error) the
        loop exits and the failure is logged by ``_on_background_task_done``;
        the server may remain marked dirty, and the next notification — or the
        error-driven reconnect, which re-lists on the new session — heals it.
        """
        try:
            while name in self._tools_refresh_dirty:
                self._tools_refresh_dirty.discard(name)
                await self._refresh_server_tools(name)
        finally:
            self._tools_refresh_running.discard(name)

    async def _refresh_server_tools(self, name: str) -> None:
        """Re-list an upstream's tools and invalidate cache rows the change
        made unsafe.

        After ``conn.tools`` is reassigned the eligibility gate already refuses
        lookups for a tool that now self-declares as a writer; the row deletion
        on top of that is deliberate — a pre-flip row would otherwise outlive
        the flip and could be served again if the annotations (or a ``cache``
        override) later move the verdict back to eligible (#557). Rows for
        tools that disappeared from the list are also dropped: they could only
        ever serve calls the upstream no longer answers, masking the removal.
        """
        conn = self._connections.get(name)
        if conn is None:
            return
        session = conn.session
        result = await session.list_tools()
        if self._connections.get(name) is not conn or conn.session is not session:
            # A reconnect replaced the session mid-refresh. Its ``list_tools``
            # ran against the new session; applying this (possibly older)
            # snapshot over it would clobber fresher state.
            return
        cfg_snap = self._config
        old_names = {n for t in conn.tools if (n := getattr(t, "name", None)) is not None}
        new_tools = list(result.tools)
        new_names = {t.name for t in new_tools}
        all_names = old_names | new_names
        was_eligible = {t: self._tool_cache_eligible(name, t, cfg_snap=cfg_snap) for t in all_names}
        conn.tools = new_tools
        stale = [
            t
            for t in sorted(all_names)
            if (was_eligible[t] and not self._tool_cache_eligible(name, t, cfg_snap=cfg_snap))
            or t not in new_names
        ]
        invalidated = 0
        if self._cache is not None:
            for t in stale:
                try:
                    invalidated += self._cache.clear(server=name, tool=t)
                except Exception:
                    logger.warning(
                        "Cache invalidation failed for %s/%s after tools/list_changed",
                        name,
                        t,
                        exc_info=True,
                    )
        logger.info(
            "Refreshed tools for '%s' after tools/list_changed "
            "(%d tools advertised, %d cache rows invalidated across %d tools)",
            name,
            len(new_tools),
            invalidated,
            len(stale),
        )

    def _on_background_task_done(
        self,
        stage: str,
        server: str,
        tool: str,
        task: asyncio.Task,
    ) -> None:
        """Done-callback for background index/extract tasks.

        Drops the finished task from the tracking set and surfaces any
        exception that escaped the coroutine's own inner handling. The inner
        ``auto_index_response`` / ``extract_and_store`` handlers already capture
        expected failures into their outcome (and log them); this guard catches
        the residual escapes (mkdir / atomic-write / genuinely unexpected
        errors) that would otherwise show up only as a non-deterministic,
        unstructured "Task exception was never retrieved" warning at
        garbage-collection time. Cancellation during ``stop()`` is expected and
        is not logged.
        """
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning(
                "Background %s task failed for %s/%s: %s",
                stage,
                server,
                tool,
                exc,
                exc_info=exc,
            )

    async def stop(self) -> None:
        # Cancel and drain background tasks (extraction, etc.). Loop until
        # the set is empty — a concurrent call_tool may schedule a new
        # extraction task during our gather await (``call_tool`` adds to
        # ``_background_tasks`` after ``asyncio.create_task(...)``), and
        # ``asyncio.gather(*snapshot)`` only awaits the snapshot, leaving
        # a late task pending. A second iteration catches and cancels it.
        # Bound the loop so a pathological task that keeps scheduling
        # replacements can't spin forever.
        for _ in range(8):
            if not self._background_tasks:
                break
            batch = list(self._background_tasks)
            for task in batch:
                task.cancel()
            await asyncio.gather(*batch, return_exceptions=True)
            for task in batch:
                self._background_tasks.discard(task)
        else:
            logger.warning(
                "ProxyManager.stop(): %d background tasks still pending after "
                "drain loop; leaking them",
                len(self._background_tasks),
            )
        self._background_tasks.clear()
        # Reset the #557 refresh bookkeeping. A drain task cancelled before its
        # first step never enters its ``finally`` (the coroutine body never
        # runs), so ``running`` can retain the server name — and a stop→start
        # reuse of this manager would then drop every later ``list_changed``
        # notification for that server ("running" but no task). ``dirty`` is
        # cleared for symmetry; a stale entry there is merely unconsumed.
        self._tools_refresh_dirty.clear()
        self._tools_refresh_running.clear()
        # Close httpx clients
        if self._llm_compressor is not None:
            await self._llm_compressor.close()
            self._llm_compressor = None
        if self._extractor is not None:
            await self._extractor.close()
            # Null it like _llm_compressor above: _get_extractor() rebuilds on
            # None, so a stop->start cycle gets a fresh httpx client instead of
            # the closed instance (whose extract() asserts _client is not None).
            self._extractor = None
        # Close the #494 consult disk cache (re-opened lazily on the next start).
        # Always null the handle so a failed close cannot leave a stale closed
        # connection that the next start() would reuse.
        if self._toolgraph_cache is not None:
            try:
                self._toolgraph_cache.close()
            except Exception:
                logger.debug("Failed to close tool-graph consult cache", exc_info=True)
            self._toolgraph_cache = None
        for conn in self._connections.values():
            if conn.stack is not None:
                try:
                    await conn.stack.aclose()
                except Exception as cleanup_exc:
                    # Redact + no exc_info (#605): this stack wraps a transport
                    # opened with the credentialed ``conn.config.url``, so a close
                    # failure's traceback tail could leak the token at DEBUG.
                    logger.debug(
                        "Failed to close connection stack for '%s': %s",
                        conn.name,
                        self._redacted_error(cleanup_exc, conn.config.url),
                    )
        if self._stack:
            await self._stack.aclose()
            self._stack = None
        self._connections.clear()
        # Close the lazily-built selective/progressive stores (#583): a restart
        # recovery or a SELECTIVE/HYBRID/progressive call may have opened a
        # SQLite-backed store that ``_connections`` cleanup never touches, so
        # without this the connection leaks across stop()/reuse.
        if self._selective_compressor is not None:
            try:
                self._selective_compressor.close()
            except Exception:
                logger.debug("Failed to close selective compressor on stop", exc_info=True)
            self._selective_compressor = None
            self._selective_compressor_cfg = None
        if self._progressive_store is not None:
            try:
                self._progressive_store.close()
            except Exception:
                logger.debug("Failed to close progressive store on stop", exc_info=True)
            self._progressive_store = None
            self._progressive_store_cfg = None
        # Clear startup-failure records too (#580) so a stopped manager reports
        # no upstreams — mirrors the double-start reset in ``start()``.
        self._failed_servers.clear()

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

    @property
    def selection_log(self) -> SelectionTelemetryLog | None:
        """The selection-telemetry sink, or ``None`` when disabled.

        Public read accessor for ``stm_selection_stats`` — parity with
        ``index_observability``; ``None`` is the disabled signal the stats
        tool renders distinctly from "enabled but empty".
        """
        return self._selection_log

    @property
    def index_engine(self) -> "FileIndexer | None":
        """The LTM write engine, or ``None`` when INDEX is unwired (#288).

        Public read accessor for library integrations; ``None`` is the
        structural-inactive signal. The bundled ``mms`` server constructs
        ``ProxyManager`` without an engine, so Stage 4 is skipped and this
        reads ``None`` there.
        """
        return self._index_engine

    # Delegates to proxy.tool_metadata module (backward-compatible)
    _truncate_description = staticmethod(truncate_description)
    _distill_schema = staticmethod(distill_schema)
    _convention_suffix = staticmethod(convention_suffix)

    def get_proxy_tools(self) -> list[ProxyToolInfo]:
        """Advertise upstream tools, gated by the #465 eligibility filter.

        Builds the would-be advertisement for EVERY discovered tool (hidden
        ones included — they used to be skipped inline here, and now become
        structured ``config_hidden`` rejects instead) and hands the set to
        ``tool_eligibility.filter_tools``, the single exposure choke point.
        Only the filter's eligible output is returned/registered; the
        reject reasons and review-profile risk penalties are snapshotted
        alongside the advertisement so selection telemetry (#467) and
        relevance ranking (#466) describe exactly this exposure decision.
        """
        candidates: list[ExposureCandidate] = []
        global_max_desc = self._config.max_description_chars
        global_strip = self._config.strip_schema_descriptions

        for conn in self._connections.values():
            # Deliberately the connect-time snapshot: the advertisement is
            # session-stable (``prefix`` is restart-only, and the exposed set
            # must not drift between startup registration and later calls),
            # unlike per-call tool-config resolution which hot-reloads.
            cfg = conn.config
            max_desc = cfg.max_description_chars
            strip = cfg.strip_schema_descriptions or global_strip

            for t in conn.tools:
                override = cfg.tool_overrides.get(t.name)

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

                candidates.append(
                    ExposureCandidate(
                        info=ProxyToolInfo(
                            prefixed_name=f"{cfg.prefix}__{t.name}",
                            description=desc,
                            input_schema=schema,
                            server=conn.name,
                            original_name=t.name,
                            annotations=getattr(t, "annotations", None),
                            output_schema=getattr(t, "outputSchema", None),
                            meta=getattr(t, "meta", None),
                        ),
                        raw_description=t.description or "",
                        raw_schema=t.inputSchema,
                        server_config=cfg,
                    )
                )

        verdict = filter_tools(
            candidates,
            self._config.exposure,
            self._unhealthy_tools,
            external_rejects=self._toolgraph_external_rejects or None,
            withhold_all=self._toolgraph_withhold_all,
        )
        if verdict.reject_reasons != self._advertised_reject_reasons:
            self._log_exposure_rejects(verdict.reject_reasons)
        self._advertised_infos = verdict.eligible
        self._advertised_tools = [info.prefixed_name for info in verdict.eligible]
        self._advertised_reject_reasons = verdict.reject_reasons
        self._advertised_risk_penalties, self._advertised_risk_penalty_sources = (
            self._compose_advertised_penalties(verdict.eligible, verdict.risk_penalties)
        )
        return verdict.eligible

    def _compose_advertised_penalties(
        self, eligible: list[ProxyToolInfo], native: dict[str, float]
    ) -> tuple[dict[str, float], dict[str, str]]:
        """Merge the two ranking-demotion sources over the advertised set (#493).

        Composes the #465 ``review``-profile demotion (*native*, keyed by
        prefixed name) with the #493 graph risk penalty
        (``_toolgraph_risk_penalties``, keyed by ``(server, original_name)``)
        via :func:`compose_risk_penalty`, returning parallel
        ``{prefixed_name: penalty}`` / ``{prefixed_name: source}`` maps. Both
        are sparse (penalized tools only); an absent tool ranks with no penalty
        and source ``none``. The graph penalty is applied to every advertised
        tool regardless of profile — it is ranking telemetry, not an exposure
        signal — whereas *native* is review-profile-only by construction.
        """
        penalties: dict[str, float] = {}
        sources: dict[str, str] = {}
        for info in eligible:
            n = native.get(info.prefixed_name, 0.0)
            g = self._toolgraph_risk_penalties.get((info.server, info.original_name), 0.0)
            if n <= 0.0 and g <= 0.0:
                continue
            penalties[info.prefixed_name] = compose_risk_penalty(n, g)
            sources[info.prefixed_name] = penalty_source(n, g)
        return penalties, sources

    @staticmethod
    def _log_exposure_rejects(reject_reasons: dict[str, str]) -> None:
        """One line per advertisement *change*, so operators see what was
        withheld and why without per-call noise. Config-driven rejects
        (``hidden``, profile scoping) are the operator's own choices — those
        log at DEBUG; everything else (structural, signal) is news and logs
        at WARNING."""
        if not reject_reasons:
            return
        expected = {REASON_CONFIG_HIDDEN, REASON_PROFILE_EXCLUDED}
        news = {t: r for t, r in reject_reasons.items() if r not in expected}
        if news:
            logger.warning(
                "Exposure filter withheld %d tool(s): %s",
                len(news),
                ", ".join(f"{t} ({r})" for t, r in sorted(news.items())),
            )
        if len(news) < len(reject_reasons):
            logger.debug(
                "Exposure filter applied config rejects: %s",
                ", ".join(f"{t} ({r})" for t, r in sorted(reject_reasons.items()) if r in expected),
            )

    @staticmethod
    def _create_scorer(config: ProxyConfig) -> RelevanceScorer:
        """Create a RelevanceScorer from proxy config."""
        from memtomem_stm.proxy.relevance import create_scorer

        sc = config.relevance_scorer
        # ``embedding_base_url`` is typed ``str | None`` but the
        # ``_apply_provider_default_url`` model validator substitutes the
        # provider default (Ollama / OpenAI) whenever the field is omitted,
        # so by this point it is always populated.
        assert sc.embedding_base_url is not None
        return create_scorer(
            scorer_type=sc.scorer,
            provider=sc.embedding_provider,
            model=sc.embedding_model,
            base_url=sc.embedding_base_url,
            timeout=sc.embedding_timeout,
        )

    def _distinct_sqlite_selective_cfgs(self) -> list[SelectiveConfig]:
        """All configured SQLite ``SelectiveConfig`` s, deduped by resolved
        path, in deterministic order (#583).

        ``SelectiveConfig`` is nested per-server (``UpstreamServerConfig.selective``)
        and per-tool (``ToolOverrideConfig.selective``); there is no global one.
        Scans servers (sorted), server-level ``selective`` first then each
        server's tool overrides (sorted), keeping the first config seen for each
        distinct on-disk path. The in-memory default contributes nothing — it
        has no persisted rows to recover.
        """
        out: list[SelectiveConfig] = []
        seen_paths: set[str] = set()
        for _srv_name, srv_cfg in sorted(self._config.upstream_servers.items()):
            candidates: list[SelectiveConfig | None] = [srv_cfg.selective]
            candidates.extend(
                srv_cfg.tool_overrides[t].selective for t in sorted(srv_cfg.tool_overrides)
            )
            for sel in candidates:
                if sel is not None and sel.pending_store == "sqlite":
                    path = str(sel.pending_store_path.expanduser())
                    if path not in seen_paths:
                        seen_paths.add(path)
                        out.append(sel)
        return out

    def _sqlite_cfg_holding_key(self, key: str) -> SelectiveConfig | None:
        """The distinct configured SQLite ``SelectiveConfig`` whose store
        currently holds *key*, or ``None`` if none does (#583).

        ``pending_store_path`` is configurable per server and per tool, so
        several distinct stores may exist. After a restart the retrieval
        endpoints (``stm_proxy_select_chunks`` / ``stm_proxy_read_more``) can
        receive a key whose row still lives in one of them while the lazily
        built compressor/store points at a different one. Probe each distinct
        store read-only (a cheap ``get`` that opens then closes its own
        connection) and return the config whose store holds the key.

        Single-store / no-store configs return ``None`` **without probing** —
        callers fall back to the sole (or first) configured store, so the common
        case pays no extra open and only genuinely multi-store configs probe.
        """
        cfgs = self._distinct_sqlite_selective_cfgs()
        if len(cfgs) <= 1:
            return None

        from memtomem_stm.proxy.pending_store import SQLitePendingStore

        for sel_cfg in cfgs:
            try:
                probe = SQLitePendingStore(sel_cfg.pending_store_path.expanduser())
                probe.initialize()
            except Exception:
                logger.warning(
                    "Could not open configured SQLite pending store %s while probing "
                    "for a persisted key",
                    sel_cfg.pending_store_path,
                    exc_info=True,
                )
                continue
            try:
                if probe.get(key) is not None:
                    return sel_cfg
            finally:
                probe.close()
        return None

    def _rebuild_selective_compressor(self, sel_cfg: SelectiveConfig | None) -> SelectiveCompressor:
        """Replace the cached selective compressor, closing the superseded one
        so a SQLite-backed store from a changed config does not leak its
        connection (#583). Returns the new compressor. Only called when the cfg
        changed or nothing is cached; the recovery path in ``select_chunks``
        builds inline because it needs its own degrade-on-open-failure handling.

        Build the replacement BEFORE closing the old one: if ``_create_selective``
        fails to open the new SQLite store it raises here, leaving the still-open
        old compressor cached and usable rather than a closed store behind the
        cfg-equality fast path.
        """
        new = self._create_selective(sel_cfg)
        old = self._selective_compressor
        self._selective_compressor = new
        self._selective_compressor_cfg = sel_cfg
        if old is not None:
            try:
                old.close()
            except Exception:
                logger.debug("Failed to close superseded selective compressor", exc_info=True)
        return new

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
        # Inject the manager's relevance scorer so SELECTIVE/HYBRID rank their
        # TOC with the operator's configured scorer (e.g. embedding) instead of
        # SelectiveCompressor's built-in BM25 default. With the default bm25
        # scorer this is a no-op (passing a BM25Scorer == the class default).
        # The scorer is read via the self-refreshing property, so a compressor
        # built after a scorer change picks up the new scorer; a scorer-only
        # hot-reload that does not also change the selective config keeps the
        # cached compressor (and its scorer) until the next rebuild — an
        # accepted edge case, tracked as a follow-up.
        kwargs["scorer"] = self._relevance_scorer
        return SelectiveCompressor(**kwargs)

    def _resolve_tool_config(
        self, server: str, tool: str, proxy_cfg: ProxyConfig | None = None
    ) -> ToolConfig:
        config = proxy_cfg or self._config
        conn = self._connections[server]
        # Per-server fields ride the hot-reloaded snapshot: compression /
        # cleaning / tool_overrides / budget edits apply on the next tool call
        # without a reconnect (the behavior docs/configuration.md promises).
        cfg = self._server_cfg(conn, config)

        # #292: ``ProxyConfig.default_compression`` was previously unread, so
        # an operator setting it in ``stm_proxy.json`` saw no effect on any
        # upstream — every server fell back to its own default of AUTO. Use
        # ``model_fields_set`` to distinguish "operator omitted compression"
        # (→ honour the global default) from "operator explicitly typed
        # compression: auto" (→ honour their explicit choice). The pattern
        # mirrors the auto_index / extraction overrides above and below.
        if "compression" in cfg.model_fields_set:
            compression = cfg.compression
        else:
            compression = config.default_compression
        # Token-equivalent budget takes precedence over char budget when set.
        # Resolution order for chars_per_token: tool override → server → proxy default.
        # Resolution order for max_result_tokens: tool override → server.
        # Falls back to existing char-budget paths when neither override is set.
        _default_server_max = UpstreamServerConfig.model_fields["max_result_chars"].default
        server_token_budget = cfg.max_result_tokens
        if server_token_budget is not None:
            cpt = cfg.chars_per_token if cfg.chars_per_token is not None else config.chars_per_token
            max_chars = tokens_to_chars(server_token_budget, cpt)
        elif cfg.max_result_chars == _default_server_max:
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
            # Per-tool budget override. Token override wins over char override
            # if both are set. Resolution order for chars_per_token (when token
            # budget is used): tool override → server → proxy default.
            if override.max_result_tokens is not None:
                cpt = (
                    override.chars_per_token
                    if override.chars_per_token is not None
                    else cfg.chars_per_token
                    if cfg.chars_per_token is not None
                    else config.chars_per_token
                )
                max_chars = tokens_to_chars(override.max_result_tokens, cpt)
            elif override.max_result_chars is not None:
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

    def _cache_key_fingerprint(self, server: str, tool: str, *, cfg_snap: ProxyConfig) -> str:
        """Compression-settings fingerprint for the response-cache key.

        An unknown server (direct dispatch / tests with no registered
        connection) yields ``""`` — mirroring the unknown-server posture of
        ``_resolve_cache_ttl`` and ``_tool_cache_eligible``, and keeping
        ``_resolve_tool_config``'s ``self._connections[server]`` lookup from
        raising here. Such a call fails at the upstream fetch before any
        store, so the placeholder key is never persisted.
        """
        if server not in self._connections:
            return ""
        tc = self._resolve_tool_config(server, tool, proxy_cfg=cfg_snap)
        return compression_fingerprint(
            tc,
            cfg_snap.min_result_retention,
            cfg_snap.max_upstream_chars,
            cfg_snap.relevance_scorer,
        )

    def _clean_content(self, text: str, cleaning_cfg: CleaningConfig) -> str:
        if not cleaning_cfg.enabled:
            return text
        return DefaultContentCleaner(cleaning_cfg).clean(text)

    async def _compress_maybe_offthread(
        self,
        compressor: Any,
        text: str,
        *,
        max_chars: int,
        context_query: str | None,
        scorer: Any,
    ) -> str:
        """Run a sync, scorer-carrying ``compress()`` without stalling the loop.

        Every scorer-injected compressor call in the async pipeline routes
        through here (#618): when the relevance scorer the compressor will
        actually use does blocking I/O (``EmbeddingScorer``'s sync httpx call
        — see ``RelevanceScorer.uses_blocking_io``), the whole sync
        ``compress()`` runs in a worker thread via ``asyncio.to_thread`` so an
        unresponsive embedding endpoint can't freeze every other proxied call
        for up to the embedding timeout. With the default BM25 scorer the gate
        is false and the call is made inline — no thread hop, byte-identical
        behavior. ``getattr`` defaults unknown scorers to False (inline),
        preserving the status quo for custom scorers that don't opt in.

        ``scorer`` must be the instance the compressor captured, not a fresh
        ``self._relevance_scorer`` read: after a scorer-only hot-reload the
        cached selective compressor keeps its old scorer until the next
        rebuild (see the note in ``_build_compressor_kwargs``), and gating on
        the property would run a still-embedding-backed compress inline —
        re-introducing the stall this helper exists to prevent.

        Callers off-loading a compressor with shared mutable state (the cached
        ``SelectiveCompressor`` and its pending store, alone or inside a
        ``HybridCompressor``) must pin it with ``begin_use()`` while still
        holding ``_selective_lock`` and balance with ``end_use()`` after this
        call: a concurrent config-change rebuild (or ``stop()``) would
        otherwise ``close()`` the store out from under the worker thread
        (``SQLitePendingStore.close`` is not synchronized with ``put``). The
        pin defers the close to the last in-flight user rather than holding
        the lock across the compress, so concurrent SELECTIVE/HYBRID calls
        run in parallel instead of surfacing spurious ``LOCK_TIMEOUT`` behind
        a slow embedding endpoint.

        Exceptions propagate with their original type (``asyncio.to_thread``
        re-raises), so callers' ``except`` clauses — notably the
        ``sqlite3.Error`` pending-store guard in ``_compress_and_surface`` —
        behave identically on both paths.
        """
        if getattr(scorer, "uses_blocking_io", False):
            return await asyncio.to_thread(
                compressor.compress, text, max_chars=max_chars, context_query=context_query
            )
        return compressor.compress(text, max_chars=max_chars, context_query=context_query)

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
            async with bounded_lock(
                self._selective_lock,
                timeout=self._config.lock_timeout_seconds,
                name="selective_lock",
            ):
                if self._selective_compressor is None or self._selective_compressor_cfg != sel_cfg:
                    sel_compressor = self._rebuild_selective_compressor(sel_cfg)
                else:
                    sel_compressor = self._selective_compressor
                # Pin the compressor before the lock drops: the off-thread
                # compress below must not lose its pending store to a
                # concurrent config-change rebuild's close(). begin_use/
                # end_use defer that close to the last in-flight user, so
                # concurrent SELECTIVE calls run in parallel instead of
                # queueing on the lock (which would surface as spurious
                # LOCK_TIMEOUT under a slow embedding endpoint).
                _begin = getattr(sel_compressor, "begin_use", None)
                if _begin is not None:
                    _begin()
            try:
                return (
                    await self._compress_maybe_offthread(
                        sel_compressor,
                        text,
                        max_chars=max_chars,
                        context_query=context_query,
                        scorer=getattr(sel_compressor, "_scorer", None),
                    ),
                    None,
                )
            finally:
                _end = getattr(sel_compressor, "end_use", None)
                if _end is not None:
                    _end()

        if compression == CompressionStrategy.LLM_SUMMARY:
            if llm_cfg is not None:
                async with bounded_lock(
                    self._llm_compressor_lock,
                    timeout=self._config.lock_timeout_seconds,
                    name="llm_compressor_lock",
                ):
                    if self._llm_compressor is None or self._llm_compressor_cfg != llm_cfg:
                        if self._llm_compressor is not None:
                            await self._llm_compressor.close()
                        self._llm_compressor = LLMCompressor(llm_cfg)
                        self._llm_compressor_cfg = llm_cfg
                    # Capture the current instance under the lock so a later
                    # concurrent config swap can't re-bind ``self._llm_compressor``
                    # before we read ``.last_fallback`` below.
                    compressor = self._llm_compressor
                # #289: scan for API keys / JWT / private keys before sending
                # the response to the LLM provider. Default-on; operators
                # disable per-config when the upstream is known sensitive-free
                # or the provider is local/trusted. CREDENTIALS only — email
                # addresses (the PII set) appear in ordinary compressible
                # content (git logs, issue threads) and routing on them
                # silently degraded the chosen strategy to truncation; the
                # surfacing persistence path still scans the full default set.
                privacy_patterns = (
                    PRIVACY_CREDENTIAL_PATTERNS if llm_cfg.privacy_scan_enabled else None
                )
                result = await compressor.compress(
                    text, max_chars=max_chars, privacy_patterns=privacy_patterns
                )
                return result, compressor.last_fallback
            logger.warning(
                "LLM_SUMMARY requested for %s/%s but no llm config found; falling back to truncate",
                server,
                tool,
            )
            scorer = self._relevance_scorer
            return (
                await self._compress_maybe_offthread(
                    TruncateCompressor(scorer=scorer),
                    text,
                    max_chars=max_chars,
                    context_query=context_query,
                    scorer=scorer,
                ),
                "no_config",
            )

        if compression == CompressionStrategy.TRUNCATE:
            scorer = self._relevance_scorer
            return (
                await self._compress_maybe_offthread(
                    TruncateCompressor(scorer=scorer),
                    text,
                    max_chars=max_chars,
                    context_query=context_query,
                    scorer=scorer,
                ),
                None,
            )

        # SCHEMA_PRUNING / SKELETON are query-aware (the manager's relevance
        # scorer is injected and context_query forwarded), so they get explicit
        # branches rather than the query-blind generic dispatch below. The
        # remaining strategies routed through get_compressor (NONE, PROGRESSIVE,
        # EXTRACT_FIELDS) take neither a scorer nor a context_query.
        if compression == CompressionStrategy.SCHEMA_PRUNING:
            scorer = self._relevance_scorer
            return (
                await self._compress_maybe_offthread(
                    SchemaPruningCompressor(scorer=scorer),
                    text,
                    max_chars=max_chars,
                    context_query=context_query,
                    scorer=scorer,
                ),
                None,
            )

        if compression == CompressionStrategy.SKELETON:
            scorer = self._relevance_scorer
            return (
                await self._compress_maybe_offthread(
                    SkeletonCompressor(scorer=scorer),
                    text,
                    max_chars=max_chars,
                    context_query=context_query,
                    scorer=scorer,
                ),
                None,
            )

        return get_compressor(compression).compress(text, max_chars=max_chars), None

    def _surfacing_enabled_for(self, server: str) -> bool:
        """Whether this upstream opts into surfacing.

        Read from the hot-reloaded ``stm_proxy.json`` (``self._config``) so a
        ``mms surfacing <server> off`` takes effect without a restart. This is
        the per-upstream enforcement point the ``SurfacingEngine`` gate cannot
        be: the engine is built once at startup from the top-level
        ``SurfacingConfig`` and never sees per-upstream config. Unknown servers
        fail open (``True``) — surfacing stays best-effort.
        """
        cfg = self._config.upstream_servers.get(server)
        return cfg.surfacing_enabled if cfg is not None else True

    def _record_surfacing_skip(self, tool: str, reason: SkipReason) -> None:
        """Record a pre-engine surfacing skip so ``stm_surfacing_stats`` shows
        it (the engine cannot, since we short-circuit before calling it)."""
        if self._surfacing_engine is None:
            return
        obs = self._surfacing_engine.observability
        if obs is not None:
            obs.record_skip(tool, reason)

    async def _apply_surfacing(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        text: str,
        *,
        trace_id: str | None = None,
        context_query: str | None = None,
    ) -> str:
        """Apply proactive memory surfacing if eligible."""
        if self._surfacing_engine is None:
            return text
        if not self._surfacing_enabled_for(server):
            self._record_surfacing_skip(tool, "upstream_disabled")
            return text
        try:
            source_response_chars = arguments.get("_stm_source_response_chars")
            surface_arguments = {
                k: v for k, v in arguments.items() if k != "_stm_source_response_chars"
            }
            if (
                "source_response_chars"
                in inspect.signature(self._surfacing_engine.surface).parameters
            ):
                return await self._surfacing_engine.surface(
                    server=server,
                    tool=tool,
                    arguments=surface_arguments,
                    response_text=text,
                    trace_id=trace_id,
                    context_query=context_query,
                    source_response_chars=source_response_chars,
                )
            return await self._surfacing_engine.surface(
                server=server,
                tool=tool,
                arguments=surface_arguments,
                response_text=text,
                trace_id=trace_id,
                context_query=context_query,
            )
        except Exception:
            logger.warning(
                "Surfacing failed for %s/%s, using compressed response",
                server,
                tool,
                exc_info=True,
            )
            return text

    async def _apply_surfacing_on_progressive(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        text: str,
        *,
        trace_id: str | None = None,
        context_query: str | None = None,
    ) -> tuple[str, bool | None, str | None]:
        """Surface on a progressive first-chunk when the formatter mode keeps
        the ``PROGRESSIVE_FOOTER_TOKEN`` concat invariant intact.

        Returns ``(text, ok, error)``:

        - ``ok=None`` — engine missing, or ``injection_mode == "prepend"``
          which would shift offsets for ``stm_proxy_read_more``. The call
          returns the compressed response unchanged.
        - ``ok=True`` — surfacing injected successfully.
        - ``ok=False`` — surfacing raised; ``error`` carries the exception
          class name. The call still returns the compressed response.

        ``"prepend"`` users keep pre-F6 behavior. See
        ``tests/test_progressive.py::TestProgressiveContentIntegrity::
        test_concat_invariant_under_surfacing`` for the empirical proof of
        which modes are safe.
        """
        if self._surfacing_engine is None:
            return text, None, None
        if not self._surfacing_enabled_for(server):
            self._record_surfacing_skip(tool, "upstream_disabled")
            return text, None, None
        if self._surfacing_engine.injection_mode == "prepend":
            if not self._warned_prepend_on_progressive:
                logger.warning(
                    "Progressive surfacing skipped: injection_mode='prepend' "
                    "would shift offsets for stm_proxy_read_more. Set "
                    "injection_mode to 'append' or 'section' to enable it."
                )
                self._warned_prepend_on_progressive = True
            # #348: counter so ``stm_surfacing_stats`` reflects the skip
            # instead of operators relying on the one-shot WARNING above.
            obs = self._surfacing_engine.observability
            if obs is not None:
                obs.record_skip(tool, "progressive_mode_conflict")
            return text, None, None
        try:
            source_response_chars = arguments.get("_stm_source_response_chars")
            surface_arguments = {
                k: v for k, v in arguments.items() if k != "_stm_source_response_chars"
            }
            if (
                "source_response_chars"
                in inspect.signature(self._surfacing_engine.surface).parameters
            ):
                surfaced = await self._surfacing_engine.surface(
                    server=server,
                    tool=tool,
                    arguments=surface_arguments,
                    response_text=text,
                    trace_id=trace_id,
                    context_query=context_query,
                    source_response_chars=source_response_chars,
                )
            else:
                surfaced = await self._surfacing_engine.surface(
                    server=server,
                    tool=tool,
                    arguments=surface_arguments,
                    response_text=text,
                    trace_id=trace_id,
                    context_query=context_query,
                )
            return surfaced, True, None
        except Exception as exc:
            logger.warning(
                "Surfacing failed for %s/%s on progressive path, using compressed response",
                server,
                tool,
                exc_info=True,
            )
            return text, False, type(exc).__name__

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
        async with bounded_lock(
            self._selective_lock,
            timeout=self._config.lock_timeout_seconds,
            name="selective_lock",
        ):
            if self._selective_compressor is None or self._selective_compressor_cfg != sel_cfg:
                self._rebuild_selective_compressor(sel_cfg)

            sel_compressor = self._selective_compressor
            compressor = HybridCompressor(
                head_chars=cfg.head_chars,
                tail_mode=cfg.tail_mode,
                min_toc_budget=cfg.min_toc_budget,
                min_head_chars=cfg.min_head_chars,
                head_ratio=cfg.head_ratio,
                selective_compressor=sel_compressor,
            )
            # Pin the shared selective compressor before the lock drops —
            # same rebuild/close race as the SELECTIVE branch: the hybrid TOC
            # path writes through its pending store. The scorer gate reads
            # the selective compressor's captured scorer (HybridCompressor
            # has no scorer of its own; its truncate tail mode is
            # query-blind).
            _begin = getattr(sel_compressor, "begin_use", None)
            if _begin is not None:
                _begin()
        try:
            return await self._compress_maybe_offthread(
                compressor,
                text,
                max_chars=max_chars,
                context_query=context_query,
                scorer=getattr(sel_compressor, "_scorer", None),
            )
        finally:
            _end = getattr(sel_compressor, "end_use", None)
            if _end is not None:
                _end()

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
    ) -> AutoIndexOutcome:
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
            observability=self.index_observability,
        )

    async def _get_extractor(self) -> FactExtractor:
        async with bounded_lock(
            self._extractor_lock,
            timeout=self._config.lock_timeout_seconds,
            name="extractor_lock",
        ):
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
    ) -> ExtractOutcome:
        """Extract facts from response and store as individual memory entries."""
        extractor = await self._get_extractor()
        return await extract_and_store(
            index_engine=self._index_engine,
            extractor=extractor,
            ext_cfg=self._config.extraction,
            server=server,
            tool=tool,
            arguments=arguments,
            text=text,
            context_query=context_query,
            observability=self.index_observability,
        )

    # Backward-compatible delegate
    _format_fact_md = staticmethod(format_fact_md)

    @staticmethod
    def _is_recovery_miss(text: str) -> bool:
        """Whether a select/read_more result is a store MISS (key absent) as
        opposed to a hit or a section-mismatch. Both endpoints render the miss
        with this stable suffix; used to decide whether to re-probe other
        configured SQLite stores for the key (#583)."""
        return "not found or expired" in text

    def select_chunks(self, key: str, sections: list[str]) -> str:
        cfgs = self._distinct_sqlite_selective_cfgs()
        if self._selective_compressor is None:
            # Restart recovery (#583): no compress call has run yet this process,
            # so the compressor (and its store handle) hasn't been built — but a
            # configured SQLite pending store may hold a selection persisted
            # before the restart. Build the configured compressor on first use so
            # the key becomes reachable — from the store that actually holds it
            # (multi-store) or the first configured store otherwise. A later
            # SELECTIVE/HYBRID compress with the same cfg reuses it via the
            # cfg-equality check. If nothing configures SQLite, or the store
            # can't be opened, degrade to the existing sentinel and cache NOTHING
            # (a failed open must not pin a bad state). No await runs between the
            # check and the assignment, and neither do the lock-holding compress
            # paths, so this can't interleave with them on the event loop — no
            # _selective_lock needed here.
            sel_cfg = self._sqlite_cfg_holding_key(key) or (cfgs[0] if cfgs else None)
            if sel_cfg is None:
                return "Selective compression not active — no pending TOC selections."
            try:
                compressor = self._create_selective(sel_cfg)
            except Exception:
                logger.warning(
                    "Could not open the configured SQLite pending store for select_chunks recovery",
                    exc_info=True,
                )
                return "Selective compression not active — no pending TOC selections."
            self._selective_compressor = compressor
            self._selective_compressor_cfg = sel_cfg
        result = self._selective_compressor.select(key, sections)
        if len(cfgs) > 1 and self._is_recovery_miss(result):
            # The cached compressor's store did not hold the key, but with
            # multiple distinct stores configured an earlier call may have
            # cached a *different* store (#583). Probe the others; if one holds
            # the key, serve it from a temporary compressor without disturbing
            # the cache or this session's in-memory selections.
            alt = self._sqlite_cfg_holding_key(key)
            if alt is not None and alt != self._selective_compressor_cfg:
                try:
                    tmp = self._create_selective(alt)
                except Exception:
                    logger.warning(
                        "Could not open alternate SQLite pending store for select_chunks recovery",
                        exc_info=True,
                    )
                    return result
                try:
                    return tmp.select(key, sections)
                finally:
                    tmp.close()
        return result

    def _open_progressive_adapter(self, sel_cfg: SelectiveConfig | None) -> ProgressiveStoreAdapter:
        """Build (do NOT cache) a ProgressiveStoreAdapter for *sel_cfg* —
        SQLite-backed when configured, else in-memory. ``_get_progressive_store``
        uses it for the cached adapter; ``read_more``'s multi-store recovery uses
        it for a throwaway adapter it closes after the call (#583)."""
        store: PendingStore
        if sel_cfg is not None and sel_cfg.pending_store == "sqlite":
            from memtomem_stm.proxy.pending_store import SQLitePendingStore

            sqlite_store = SQLitePendingStore(sel_cfg.pending_store_path.expanduser())
            sqlite_store.initialize()
            store = sqlite_store
        else:
            from memtomem_stm.proxy.pending_store import InMemoryPendingStore

            store = InMemoryPendingStore()
        return ProgressiveStoreAdapter(store)

    def _get_progressive_store(
        self, sel_cfg: SelectiveConfig | None = None
    ) -> ProgressiveStoreAdapter:
        if self._progressive_store is None or (
            sel_cfg is not None and sel_cfg != self._progressive_store_cfg
        ):
            # Build the replacement BEFORE closing the old one (#583): if the new
            # SQLite store fails to open this raises with the old adapter still
            # cached and usable rather than a closed store behind the cfg check.
            new = self._open_progressive_adapter(sel_cfg)
            if self._progressive_store is not None:
                try:
                    self._progressive_store.close()
                except Exception:
                    logger.debug("Failed to close superseded progressive store", exc_info=True)
            self._progressive_store = new
            self._progressive_store_cfg = sel_cfg
        return self._progressive_store

    def _apply_progressive(
        self,
        text: str,
        cfg: ProgressiveConfig,
        server: str,
        tool: str,
        sel_cfg: SelectiveConfig | None = None,
        *,
        trace_id: str | None = None,
    ) -> str:
        store = self._get_progressive_store(sel_cfg)
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
            server=server,
            tool=tool,
            trace_id=trace_id,
            chunk_size=cfg.chunk_size,
            include_structure_hint=cfg.include_structure_hint,
        )
        store.put(key, resp)

        chunker = ProgressiveChunker(
            chunk_size=cfg.chunk_size,
            include_hint=cfg.include_structure_hint,
        )
        first = chunker.first_chunk(text, key, ttl_seconds=cfg.ttl_seconds)
        if self._progressive_reads_tracker is not None:
            # The first_chunk return concatenates ``chunk + footer`` where
            # the footer starts with PROGRESSIVE_FOOTER_TOKEN; splitting
            # once on that sentinel recovers the exact chunk length
            # without duplicating ``_find_boundary``.
            initial_chars = len(first.split(PROGRESSIVE_FOOTER_TOKEN, 1)[0])
            self._progressive_reads_tracker.record_initial(
                key=key,
                trace_id=trace_id,
                server=server,
                tool=tool,
                initial_chars=initial_chars,
                total_chars=len(text),
            )
        return first

    def read_more(self, key: str, offset: int, limit: int | None = None) -> str:
        """Return next chunk from a progressive delivery response.

        Wrapped in a ``proxy_call_read_more`` span (when Langfuse is
        configured) so that the revisit is filterable in the Langfuse UI
        as a cohort with the original ``proxy_call``. Correlation is
        metadata-tag based (both spans carry the same ``trace_id``
        attribute), not trace-tree merging — the two MCP turns run in
        separate OTel contexts, so this span is always a root. Other
        ``stm_proxy_*`` tools lack spans because they are local reads;
        ``read_more`` is the exception because it is a revisit of a prior
        upstream call and otherwise loses its link to the originating
        trace_id.
        """
        # Restart recovery (#583): if no progressive store has been built yet
        # this process, a configured SQLite backend may hold the key from before
        # the restart — but a bare _get_progressive_store() defaults to a fresh
        # in-memory store and reports "not found". Pass the fallback cfg ONLY
        # when nothing is cached: passing it once a store exists would trip the
        # cfg-mismatch rebuild and discard a live in-memory store still holding
        # keys written since startup. On a SQLite open failure, degrade to the
        # sentinel; _get_progressive_store caches only after initialize()
        # succeeds, so nothing bad is pinned and the next call retries.
        cfgs = self._distinct_sqlite_selective_cfgs()
        temp_store: ProgressiveStoreAdapter | None = None
        if self._progressive_store is None:
            fallback = self._sqlite_cfg_holding_key(key) or (cfgs[0] if cfgs else None)
            if fallback is not None:
                try:
                    store = self._get_progressive_store(fallback)
                except Exception:
                    logger.warning(
                        "Could not open the configured SQLite pending store for read_more recovery",
                        exc_info=True,
                    )
                    return f"Progressive delivery key '{key}' not found or expired."
            else:
                store = self._get_progressive_store()
        else:
            store = self._get_progressive_store()
        resp = store.get(key)
        if resp is None and len(cfgs) > 1:
            # The cached store did not hold the key, but with multiple distinct
            # stores configured an earlier call may have cached a *different*
            # one (#583). Probe the others and, if one holds the key, serve it
            # from a temporary adapter (closed below) without disturbing the
            # cache or this session's in-memory progressive responses.
            alt = self._sqlite_cfg_holding_key(key)
            if alt is not None and alt != self._progressive_store_cfg:
                try:
                    temp_store = self._open_progressive_adapter(alt)
                except Exception:
                    logger.warning(
                        "Could not open alternate SQLite pending store for read_more recovery",
                        exc_info=True,
                    )
                    temp_store = None
                if temp_store is not None:
                    alt_resp = temp_store.get(key)
                    if alt_resp is not None:
                        store, resp = temp_store, alt_resp
        try:
            if resp is None:
                return f"Progressive delivery key '{key}' not found or expired."
            with traced(
                "proxy_call_read_more",
                metadata={
                    "server": resp.server,
                    "tool": resp.tool,
                    "trace_id": resp.trace_id,
                    "key": key,
                },
            ):
                store.touch(key)
                # Continue with the chunking the first chunk used (persisted on
                # the ProgressiveResponse) — an explicit ``limit`` from the agent
                # still wins. Hardcoding ``4000`` here made follow-ups diverge
                # from a tuned ``progressive.chunk_size`` / hint preference.
                chunk_size = limit or resp.chunk_size
                chunker = ProgressiveChunker(
                    chunk_size=chunk_size, include_hint=resp.include_structure_hint
                )
                output = chunker.read_chunk(
                    resp.content, offset, limit, key=key, ttl_seconds=resp.ttl_seconds
                )
                # Skip telemetry when ``read_chunk`` short-circuits with the
                # ``(no more content)`` sentinel (offset >= len(content)) —
                # that response carries no footer and no payload, so logging
                # it would inflate ``follow_up_rate`` with calls that served
                # zero new bytes and push ``avg_chars_served`` above
                # ``total_chars``.
                if (
                    self._progressive_reads_tracker is not None
                    and PROGRESSIVE_FOOTER_TOKEN in output
                ):
                    chunk_chars = len(output.split(PROGRESSIVE_FOOTER_TOKEN, 1)[0])
                    self._progressive_reads_tracker.record_follow_up(
                        key=key,
                        trace_id=resp.trace_id,
                        server=resp.server,
                        tool=resp.tool,
                        offset=offset,
                        chars=chunk_chars,
                        total_chars=resp.total_chars,
                    )
                return output
        finally:
            if temp_store is not None:
                temp_store.close()

    def get_upstream_health(self) -> dict[str, dict]:
        """Return per-server health: connection status, tool counts.

        ``tools`` counts the DISCOVERED catalogue (everything the upstream
        listed — since #465 ``conn.tools`` is no longer pre-filtered at
        connect time); ``advertised_tools`` counts how many of them the
        last advertisement actually exposed, so operators can tell a
        withheld tool from a missing one. ``advertised_tools`` reflects
        the most recent ``get_proxy_tools()`` pass — in the server it runs
        at startup registration, before any health probe can observe it.

        A configured server that FAILED to connect at startup (#580) has no
        ``_connections`` entry, so it is reported here from ``_failed_servers``
        with ``connected: False`` and an ``error`` summary — making the
        existing ``DISCONNECTED`` rendering reachable instead of the server
        vanishing from health entirely.
        """
        health: dict[str, dict] = {}
        for name, conn in self._connections.items():
            health[name] = {
                "connected": conn.session is not None,
                "tools": len(conn.tools),
                "advertised_tools": sum(
                    1 for info in self._advertised_infos if info.server == name
                ),
            }
            # Per-upstream breaker (#608) — pure reads (#600). Absent when
            # the breaker is disabled (circuit_max_failures=0), so renderers
            # can distinguish "disabled" from "closed".
            if conn.breaker is not None:
                health[name]["circuit_state"] = conn.breaker.state
                health[name]["circuit_failures"] = conn.breaker.failure_count
                health[name]["circuit_reset_in"] = conn.breaker.time_until_reset
        for name, error in self._failed_servers.items():
            # A server can't be both connected and in the failed map (the
            # success path pops it), but guard against a stale entry anyway.
            if name in health:
                continue
            health[name] = {
                "connected": False,
                "tools": 0,
                "advertised_tools": 0,
                "error": error,
            }
        return health

    def get_toolgraph_status(self) -> dict[str, Any] | None:
        """External tool-graph eligibility provider status (#465), or ``None``
        when the block is disabled.

        Surfaces the once-per-startup consult outcome so an operator can
        confirm whether external enforcement is actually ACTIVE this session.
        This is load-bearing, not cosmetic: a failure that resolved to ``open``
        silently skips the rule family, and a one-time ``on_*: open`` must
        never become an unnoticed permanent enforcement blind spot. ``degraded``
        means the family was skipped (advertising per STM-native rules only);
        ``withholding_all`` means a ``closed`` knob withheld every tool;
        otherwise the consult succeeded and ``external_reject_count`` reflects
        the per-candidate verdicts in force. ``risk_penalty_count`` is the
        number of candidates the graph assigned a positive ``risk_score`` that
        STM mapped to a relevance demotion (#493) — ranking telemetry only,
        ``0`` when ``risk_penalty_scale`` is ``0`` or the enrichment degraded.
        """
        if not self._config.toolgraph.enabled:
            return None
        return {
            "enabled": True,
            "degraded": self._toolgraph_degraded,
            "degraded_reason": self._toolgraph_degraded_reason,
            "withholding_all": self._toolgraph_withhold_all,
            "graph_generation": self._graph_generation,
            "from_cache": self._toolgraph_from_cache,
            "external_reject_count": len(self._toolgraph_external_rejects),
            "risk_penalty_count": len(self._toolgraph_risk_penalties),
        }

    async def _on_cache_hit(
        self,
        cached: str,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        trace_id: str,
    ) -> str:
        """Shared hit path: record metric, trace span, re-apply surfacing.

        Called from both the stampede guard's fast-path check and its
        post-lock double-check so concurrent duplicate requests return
        through the same hit pipeline as a single call would.
        """
        self.tracker.record_cache_hit(chars=len(cached))
        context_query = arguments.get("_context_query") if arguments else None
        with traced("proxy_call_cache_hit", metadata={"server": server, "tool": tool}):
            return await self._apply_surfacing(
                server,
                tool,
                arguments,
                cached,
                trace_id=trace_id,
                context_query=context_query if isinstance(context_query, str) else None,
            )

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        trace_id: str | None = None,
    ) -> str | list | CallToolResult:
        """Forward a tool call to upstream, compress, surface, and return.

        Return shape: ``str`` (text-only response, or a cache hit), ``list``
        of content blocks (mixed/non-text response), or a full
        ``CallToolResult`` when the upstream result carries envelope fields
        (``structuredContent`` / result-level ``_meta``) that a bare
        text/list return would drop.

        Wraps the entire call pipeline in a Langfuse observation span when
        Langfuse is configured. The span carries ``server``, ``tool``, and
        ``trace_id`` metadata so it can be correlated with the matching row
        in ``proxy_metrics.db``. When Langfuse is not configured, ``traced()``
        returns ``nullcontext()`` and the wrapper is a no-op — no perf cost,
        no behavior change for users who don't opt in.

        ``trace_id`` is keyword-only and defaults to a fresh
        ``uuid.uuid4().hex[:16]``. Callers (e.g. the bench_qa harness) may
        pass a deterministic value so two runs produce identical
        ``proxy_metrics``/``surfacing_events`` rows under the same trace,
        enabling determinism diffs across runs.
        """
        if server not in self._connections:
            raise KeyError(f"Unknown upstream server: '{server}'")
        if trace_id is None:
            trace_id = uuid.uuid4().hex[:16]
        # Selection telemetry (#467): the prefixed name shares the
        # ``candidate_tools`` vocabulary, so replay tooling can match the
        # selected tool against the advertised set verbatim. Connect-time
        # snapshot on purpose — ``prefix`` is restart-only, like the
        # advertisement it must match.
        selected_tool = f"{self._connections[server].config.prefix}__{tool}"
        candidate_features, ranker_version = self._rank_candidates(arguments)
        selection_id = self._log_selection(
            server, selected_tool, arguments, trace_id, candidate_features, ranker_version
        )
        started = _time.perf_counter()
        with traced(
            "proxy_call",
            metadata={"server": server, "tool": tool, "trace_id": trace_id},
        ):
            try:
                result, cache_hit = await self._call_tool_guarded(
                    server, tool, arguments, trace_id=trace_id
                )
            except Exception as exc:
                # Upstream/transport/timeout/protocol errors are already
                # recorded inside _call_tool_inner via record_error(); a raise
                # that escapes to here means a CLEAN/COMPRESS/SURFACE/INDEX
                # stage threw after the upstream call had already returned.
                # Without this guard the metrics row was silently skipped,
                # leaving operators blind to in-pipeline failures.
                if not getattr(exc, "_stm_metrics_recorded", False):
                    # LockTimeoutError (#208) is a subclass of asyncio.TimeoutError
                    # but distinct from upstream TIMEOUT (#206) — an internal
                    # lock hang indicates a bug, not a slow dependency.
                    pipeline_category = (
                        ErrorCategory.LOCK_TIMEOUT
                        if isinstance(exc, LockTimeoutError)
                        else ErrorCategory.INTERNAL_ERROR
                    )
                    try:
                        self.tracker.record_error(
                            CallMetrics(
                                server=server,
                                tool=tool,
                                original_chars=0,
                                compressed_chars=0,
                                trace_id=trace_id,
                                error_category=pipeline_category,
                                # LOCK_TIMEOUT bubbles out of ``bounded_lock``
                                # before the per-stage ``index_error`` /
                                # ``extract_error`` / ``surface_error`` columns
                                # get populated, so without this the row is
                                # all-NULL across diagnostic text — same gap
                                # the rest of #253 closes for upstream errors.
                                error_message=format_error_message_from_exc(exc),
                            )
                        )
                    except Exception:
                        logger.debug(
                            "Failed to record %s metrics row",
                            pipeline_category.value,
                            exc_info=True,
                        )
                self._log_execution(
                    selection_id,
                    server,
                    selected_tool,
                    trace_id,
                    started,
                    ok=False,
                    error_type=type(exc).__name__,
                    ranker_version=ranker_version,
                )
                raise
            self._log_execution(
                selection_id,
                server,
                selected_tool,
                trace_id,
                started,
                ok=not (isinstance(result, mcp_types.CallToolResult) and result.isError is True),
                error_type=(
                    "UpstreamToolError"
                    if isinstance(result, mcp_types.CallToolResult) and result.isError is True
                    else None
                ),
                ranker_version=ranker_version,
                cache_hit=cache_hit,
            )
            return result

    def _rank_candidates(
        self, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Tool-relevance ranking for one call (#466 v0) — telemetry input only.

        Returns ``(candidate_features, ranker_version)`` for the selection
        event, or ``(None, None)`` when ranking did not run: telemetry off,
        ranking disabled, nothing advertised yet, no query signal in the
        call, or the ranker failed — the event then keeps the
        unranked-baseline ``ranker_version`` so replay can split cohorts.
        Runs before the sink's sampling decision, so a sampled-out call
        wastes one cheap BM25 pass rather than leaking the sampling
        decision out of the sink.
        """
        if self._selection_log is None:
            return None, None
        trc = self._config.tool_relevance
        if not trc.enabled or not self._advertised_infos:
            return None, None
        try:
            derived = derive_query(arguments)
            if derived is None:
                return None, None
            query, source = derived
            penalties = self._advertised_risk_penalties
            penalty_sources = self._advertised_risk_penalty_sources
            ranked = ToolRelevanceRanker(top_n=trc.top_n).rank(
                query, self._advertised_infos, penalties, penalty_sources
            )
            if not ranked:
                return None, None
            # The risk-penalty pathway changes the scoring function, so the
            # cohort stamp must change with it — but only when a penalty
            # actually shaped the scores (an all-zero map is v1 math). A
            # graph-derived component (#493) splits a finer cohort than a
            # native-review-only penalty, since it reaches every profile.
            if any(
                s in (PENALTY_SOURCE_GRAPH, PENALTY_SOURCE_BOTH) for s in penalty_sources.values()
            ):
                version = RANKER_VERSION_BM25_GRAPH_RISK
            elif any(p > 0.0 for p in penalties.values()):
                version = RANKER_VERSION_BM25_RISK
            else:
                version = RANKER_VERSION_BM25
            return build_candidate_features(query, source, ranked), version
        except Exception:
            logger.debug("Tool-relevance ranking failed", exc_info=True)
            return None, None

    def _log_selection(
        self,
        server: str,
        selected_tool: str,
        arguments: dict[str, Any],
        trace_id: str,
        candidate_features: dict[str, Any] | None = None,
        ranker_version: str | None = None,
    ) -> str | None:
        """Emit the selection-telemetry event for one proxied call (#467).

        Returns ``None`` when telemetry is disabled, the call was sampled
        out, or the write failed — the caller then skips the paired
        execution event so the log never contains orphan halves. A
        telemetry failure must never affect the proxied call.
        """
        if self._selection_log is None:
            return None
        try:
            return self._selection_log.log_selection(
                server=server,
                selected_tool=selected_tool,
                candidate_tools=self._advertised_tools,
                arguments=arguments,
                trace_id=trace_id,
                candidate_features=candidate_features,
                ranker_version=ranker_version,
                reject_reasons=self._advertised_reject_reasons,
                graph_generation=self._graph_generation,
            )
        except Exception:
            logger.debug("Selection telemetry write failed", exc_info=True)
            return None

    def _log_execution(
        self,
        selection_id: str | None,
        server: str,
        selected_tool: str,
        trace_id: str,
        started: float,
        *,
        ok: bool,
        error_type: str | None = None,
        ranker_version: str | None = None,
        cache_hit: bool | None = None,
    ) -> None:
        """Emit the execution-outcome event paired to ``selection_id``.

        ``error_type`` is the exception class name only; the typed error
        category and message live in ``proxy_metrics.db``, joinable via
        ``trace_id`` — telemetry never duplicates (or leaks) error text.
        ``ranker_version`` mirrors the paired selection so replay groups
        both halves under the same cohort. ``cache_hit`` is ``True``/``False``
        on a completed call (served from the response cache vs a live
        upstream call) and left ``None`` when a raise escaped before the
        hit/miss was attributable.
        """
        if self._selection_log is None or selection_id is None:
            return
        try:
            self._selection_log.log_execution(
                selection_id=selection_id,
                trace_id=trace_id,
                server=server,
                selected_tool=selected_tool,
                ok=ok,
                latency_ms=(_time.perf_counter() - started) * 1000.0,
                error_type=error_type,
                ranker_version=ranker_version,
                cache_hit=cache_hit,
            )
        except Exception:
            logger.debug("Execution telemetry write failed", exc_info=True)

    async def _call_tool_guarded(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        trace_id: str,
    ) -> tuple[str | list | CallToolResult, bool]:
        """Cache stampede guard: serialize identical concurrent ``call_tool``
        invocations on a per-key lock so a cold cache + duplicate requests
        trigger one upstream call rather than N.

        Structure: fast-path check (lock-free for cache hits) → per-key
        ``asyncio.Lock`` with double-check (another coroutine may have
        populated while we waited) → delegate to ``_call_tool_inner`` on
        confirmed miss. The ``_key_locks`` dict entry is popped in
        ``finally`` while the lock is still held so any waiter already
        queued on the same lock sees the cached result on its own
        double-check, and a new arrival after pop likewise finds the set
        value (stampede window closed).

        Returns ``(result, cache_hit)`` so ``call_tool`` can stamp the
        selection-telemetry execution event (#467): ``True`` on either
        cache-hit return path, ``False`` on a live ``_call_tool_inner``
        call (including the no-cache configuration and cache-ineligible tools)."""
        upstream_args = (
            {k: v for k, v in arguments.items() if k != "_context_query"} if arguments else {}
        )
        # Same isinstance-str coercion as ``_call_tool_inner`` so the key the
        # lookup computes here matches the key the store computes there.
        raw_context_query = arguments.get("_context_query") if arguments else None
        context_query = raw_context_query if isinstance(raw_context_query, str) else None
        # One snapshot for the whole guarded section (each ``self._config``
        # read is a loader call, and a hot reload mid-request must not split
        # the fast-path get key from the stampede-lock key). The same snapshot
        # is threaded into ``_call_tool_inner`` so a confirmed miss stores
        # under the fingerprint this lookup missed on.
        cfg_snap = self._config

        # No cache configured, OR a non-positive configured TTL disables it: go
        # straight through (no lookup, no store, no stampede lock). The TTL check
        # is on the LOOKUP path on purpose — a row cached earlier under a positive
        # TTL is still "live" on disk after the TTL is lowered to 0 (per-row TTL is
        # frozen at write time), so without bypassing the lookup that stale row
        # would keep serving. Skipping the lookup makes ttl<=0 behave like
        # ``cache.enabled=false``; the store-side ``ProxyCache.set`` short-circuit
        # additionally deletes such a row when the forced upstream call re-stores.
        # The resolved TTL honors any per-tool/server ``cache_ttl_seconds``
        # override, so a tool whose override is 0 bypasses the lookup for THAT
        # tool exactly as the global ttl<=0 does (per-row TTL is frozen at write).
        cache_ttl = self._resolve_cache_ttl(server, tool, cfg_snap=cfg_snap)
        if self._cache is None or (cache_ttl is not None and cache_ttl <= 0):
            try:
                return (
                    await self._call_tool_inner(
                        server, tool, arguments, trace_id=trace_id, cfg_snap=cfg_snap
                    ),
                    False,
                )
            except Exception as exc:
                # A raise (upstream fetch failure or a pipeline-stage error)
                # exits ``_call_tool_inner`` BEFORE any Stage-5 store, so under
                # a disabled (``ttl<=0``) cache a row frozen at an earlier
                # positive TTL would survive the call and resurface once the
                # TTL is raised back within its window — the same hole #548/
                # #550 closed for every *returned* response shape. Invalidate
                # (best-effort, gated on ttl<=0 inside the helper) so raised
                # failures behave like returned ones. The ``isError`` branch
                # already invalidated and marked its ToolError; skip its
                # second DELETE.
                if not getattr(exc, "_stm_cache_invalidated", False):
                    self._invalidate_disabled_cache(
                        server, tool, upstream_args, cfg_snap=cfg_snap, context_query=context_query
                    )
                raise

        # Cache-eligibility gate (MCP annotations / per-tool|server ``cache``
        # override): a tool the policy deems non-cacheable — e.g. a self-declared
        # writer under the default conservative policy — is never served from nor
        # stored in the cache. Behave as if no cache exists: straight through with
        # NO stampede lock, so every call re-runs and its side effect re-executes.
        # Gating the lookup (not just the store in ``_store_cache``) also refuses
        # to serve any row cached before this gate existed.
        if not self._tool_cache_eligible(server, tool, cfg_snap=cfg_snap):
            return (
                await self._call_tool_inner(
                    server, tool, arguments, trace_id=trace_id, cfg_snap=cfg_snap
                ),
                False,
            )

        # Computed once (after the zero-cost bypass paths above) and shared by
        # the fast-path get, the stampede-lock key, and the in-lock double-check
        # so all three derive from the same config snapshot.
        config_fp = self._cache_key_fingerprint(server, tool, cfg_snap=cfg_snap)

        # Fast-path: cache hit without lock contention.
        cached = self._cache.get(
            server, tool, upstream_args, context_query=context_query, config_fingerprint=config_fp
        )
        if cached is not None:
            return await self._on_cache_hit(cached, server, tool, arguments, trace_id), True

        # ``context_query`` is part of the lock key on purpose: two concurrent
        # calls that differ only in query context store DIFFERENT rows, so
        # collapsing them would serve one caller a body compressed for the
        # other's query — the collision the key exists to prevent.
        cache_key = _cache_key(
            server, tool, upstream_args, context_query=context_query, config_fingerprint=config_fp
        )
        lock = self._key_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            try:
                # Double-check inside the lock: a coroutine that held the
                # lock ahead of us may have populated the cache already.
                cached = self._cache.get(
                    server,
                    tool,
                    upstream_args,
                    context_query=context_query,
                    config_fingerprint=config_fp,
                )
                if cached is not None:
                    return await self._on_cache_hit(cached, server, tool, arguments, trace_id), True
                # Eligible cache lookup confirmed missing (the lock-free fast-path
                # AND this in-lock double-check). This is the SINGLE eligible-miss
                # exit, so account the miss here. The ineligible and no-cache paths
                # above deliberately record NOTHING — no lookup was attempted, and
                # counting a forced-forward writer (or a ``cache: false`` tool) as a
                # miss would skew the hit-rate diagnostics.
                self.tracker.record_cache_miss()
                return await self._call_tool_inner(
                    server, tool, arguments, trace_id=trace_id, cfg_snap=cfg_snap
                ), False
            finally:
                self._key_locks.pop(cache_key, None)

    def _server_cfg(self, conn: UpstreamConnection, cfg_snap: ProxyConfig) -> UpstreamServerConfig:
        """Latest per-server config from the hot-reloaded snapshot.

        Falls back to the connect-time snapshot when the key is absent (server
        removed from the file mid-session — adding/removing servers stays
        restart-only). Callers pass their per-request ``cfg_snap`` so one
        request never mixes two reload generations.
        """
        fresh = cfg_snap.upstream_servers.get(conn.name)
        return conn.config if fresh is None else fresh

    async def _maybe_reconnect_for_config_change(
        self, conn: UpstreamConnection, fresh_cfg: UpstreamServerConfig
    ) -> None:
        """Apply a hot-reloaded connection-affecting edit via live reconnect.

        Prepare-first semantics come from ``_reconnect_server``: the replacement
        is fully established before the swap, so on failure the OLD connection
        keeps serving — this wrapper swallows the failure, damps the exact
        failed fingerprint (no per-call retry storm against a broken edit), and
        lets the call proceed on the old connection. ``fresh_cfg`` is passed
        straight into ``_reconnect_server`` so the config attempted is exactly
        the one whose fingerprint we damp and whose url we redact against — no
        re-read can slip a different generation in between. Failure-TRIGGERED
        reconnects are deliberately not damped: they are driven by call
        failures with their own backoff and breaker accounting.
        """
        new_fp = _connection_fingerprint(fresh_cfg)
        if new_fp == _connection_fingerprint(conn.config):
            return
        if new_fp == conn.last_failed_connection_fp:
            return
        logger.info("Connection config for '%s' changed; applying via live reconnect", conn.name)
        try:
            await self._reconnect_server(conn.name, fresh_cfg)
        except Exception as exc:
            conn.last_failed_connection_fp = new_fp
            # Redact + no exc_info (#605): the attempt opened a transport with
            # the NEW credentialed url.
            logger.warning(
                "Live reconnect for changed config of '%s' failed; keeping the "
                "existing connection (won't retry until the config changes "
                "again): %s",
                conn.name,
                self._redacted_error(exc, fresh_cfg.url),
            )
        else:
            # A successful initialize + tools/list is a completed round-trip:
            # close the breaker so a config fix isn't fast-failed for up to
            # ``circuit_reset_seconds`` by the OLD config's failure streak.
            if conn.breaker is not None:
                conn.breaker.record_success()

    async def _fetch_upstream(
        self,
        server: str,
        tool: str,
        upstream_args: dict[str, Any],
        *,
        trace_id: str | None,
        cfg_snap: ProxyConfig | None = None,
    ) -> "CallToolResult":
        """Stage 1: fetch the upstream tool result with bounded retry + reconnect.

        Owns the connection handle, the per-attempt and overall deadlines, and the
        failure taxonomy (timeout / transport / protocol / programming). Returns the
        upstream ``CallToolResult`` on success. On failure it records the error
        metric, marks the exception via ``_mark_recorded`` (so the outer
        ``call_tool`` does not double-record), best-effort reconnects, and re-raises.
        ``upstream_args`` must already carry ``_trace_id`` — the caller injects it
        before this call so the cache-key snapshot stays trace-free.

        ``cfg_snap`` is the caller's per-request config snapshot, used by the
        timeout-replay guard (#578) and the per-server knobs below; ``None``
        (direct test callers) falls back to the live config.
        """
        conn = self._connections[server]
        # Per-server retry/deadline knobs come from the hot-reloaded snapshot
        # (edits apply on the next call), pinned once per call — a reload
        # mid-request can't move the deadline under a running retry loop.
        cfg = self._server_cfg(conn, cfg_snap if cfg_snap is not None else self._config)
        # Connection-affecting edits (url/headers/transport/command/args/env)
        # are applied here via live reconnect — BEFORE the breaker check, so a
        # config fix isn't fast-failed by the old config's failure streak. The
        # cache fast-path in ``_call_tool_guarded`` intentionally bypasses
        # this: a cache hit never touches the connection.
        await self._maybe_reconnect_for_config_change(conn, cfg)
        delay = cfg.reconnect_delay_seconds
        breaker = conn.breaker

        # Per-upstream circuit breaker fast-fail (#608). Checked here — after
        # the cache fast-path in ``_call_tool_guarded`` — so cached responses
        # keep serving while the upstream is down. ``is_open`` is a pure read
        # (#600): once ``circuit_reset_seconds`` elapses it returns False and
        # this call proceeds as the half-open probe (a probe may burn up to
        # ``max_retries + 1`` attempts, matching the surfacing breaker's
        # non-single-probe posture).
        if breaker is not None and breaker.is_open:
            remaining = breaker.time_until_reset or 0.0
            msg = (
                f"Upstream '{server}' temporarily unavailable: circuit breaker "
                f"open after {breaker.failure_count} consecutive failures; "
                f"retry in ~{remaining:.0f}s"
            )
            self.tracker.record_error(
                CallMetrics(
                    server=server,
                    tool=tool,
                    original_chars=0,
                    compressed_chars=0,
                    is_error=True,
                    error_category=ErrorCategory.CIRCUIT_OPEN,
                    error_message=msg[:MAX_ERROR_MESSAGE_CHARS],
                    trace_id=trace_id,
                )
            )
            from mcp.server.fastmcp.exceptions import ToolError

            tool_err = ToolError(msg)
            _mark_recorded(tool_err)
            raise tool_err

        # Overall-deadline policy: each attempt uses
        # ``min(call_timeout_seconds, remaining_deadline)`` as its effective
        # timeout. ``asyncio.wait_for`` cancels the inner ``call_tool`` on
        # timeout; the existing TimeoutError handler below runs
        # ``_reconnect_server``, which tears down the old ``ClientSession``
        # and its transport. That drops any orphaned ``request_id`` so a late
        # upstream response from the cancelled attempt cannot be delivered to
        # a subsequent caller.
        _call_started_at = _time.monotonic()

        for attempt in range(cfg.max_retries + 1):
            remaining_deadline = cfg.overall_deadline_seconds - (
                _time.monotonic() - _call_started_at
            )
            if remaining_deadline <= 0:
                deadline_exc = asyncio.TimeoutError(
                    f"{server}/{tool} exceeded overall_deadline_seconds "
                    f"({cfg.overall_deadline_seconds}s) after {attempt} attempt(s)"
                )
                self.tracker.record_error(
                    CallMetrics(
                        server=server,
                        tool=tool,
                        original_chars=0,
                        compressed_chars=0,
                        is_error=True,
                        error_category=ErrorCategory.TIMEOUT,
                        error_message=format_error_message_from_exc(deadline_exc),
                        trace_id=trace_id,
                    )
                )
                _mark_recorded(deadline_exc)
                if breaker is not None:
                    breaker.record_failure()
                try:
                    await self._reconnect_server(server, cfg)
                except Exception as reconnect_exc:
                    # Redact + no exc_info (#605): the reconnect reopens a transport
                    # with the credentialed ``cfg.url``, so a failure's traceback
                    # tail could leak the token at DEBUG.
                    logger.debug(
                        "Post-deadline reconnect failed for '%s': %s",
                        server,
                        self._redacted_error(reconnect_exc, cfg.url),
                    )
                raise deadline_exc
            per_attempt_timeout = min(cfg.call_timeout_seconds, remaining_deadline)
            try:
                result = await asyncio.wait_for(
                    conn.session.call_tool(tool, upstream_args),
                    timeout=per_attempt_timeout,
                )
                # Any completed round-trip proves the upstream is alive — an
                # ``isError`` result (classified UPSTREAM_ERROR downstream) is
                # a tool-level failure, not a dependency-health signal, so it
                # still closes the breaker.
                if breaker is not None:
                    breaker.record_success()
                return result
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
                            error_message=format_error_message_from_exc(exc),
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
                    # A JSON-RPC error response is a completed round-trip: the
                    # upstream received the request and replied, so the
                    # transport is healthy. Like an ``isError`` result, it
                    # closes the breaker — otherwise transport/timeout failures
                    # separated by a proven-alive protocol reply would still
                    # accumulate as "consecutive" and spuriously open it.
                    if breaker is not None:
                        breaker.record_success()
                    self.tracker.record_error(
                        CallMetrics(
                            server=server,
                            tool=tool,
                            original_chars=0,
                            compressed_chars=0,
                            is_error=True,
                            error_category=ErrorCategory.PROTOCOL,
                            error_code=err_code,
                            error_message=format_error_message_from_exc(exc),
                            trace_id=trace_id,
                        )
                    )
                    _mark_recorded(exc)
                    try:
                        await self._reconnect_server(server, cfg)
                    except Exception as reconnect_exc:
                        # Expected fallback: the primary error is already being
                        # re-raised and carries the actionable trace. The
                        # reconnect attempt here is best-effort for the NEXT
                        # call — a failure is noise for current operators.
                        # Redact + no exc_info (#605): the reconnect reopens a
                        # transport with the credentialed ``cfg.url``.
                        logger.debug(
                            "Post-protocol-error reconnect failed for '%s': %s",
                            server,
                            self._redacted_error(reconnect_exc, cfg.url),
                        )
                    raise

                # Timeout-replay guard (#578): a per-attempt timeout cancels OUR
                # wait — the request may already have executed upstream, so
                # re-invoking a non-read-only tool would manufacture duplicate
                # writes. Connection-level failures (refused / reset / EOF) stay
                # retryable for every tool: they overwhelmingly mean the request
                # never completed, and gating them would disable most useful
                # retries. MCP-error retries (err_code path) also re-execute but
                # are out of scope here — tracked as a follow-up on #578.
                replay_unsafe = isinstance(
                    exc, asyncio.TimeoutError
                ) and not self._tool_idempotent_for_retry(
                    server, tool, cfg_snap=cfg_snap if cfg_snap is not None else self._config
                )
                if attempt >= cfg.max_retries or replay_unsafe:
                    if replay_unsafe and attempt < cfg.max_retries:
                        logger.warning(
                            "Timeout on non-read-only tool %s/%s — not retrying "
                            "(replay guard); reconnecting for the next call",
                            server,
                            tool,
                        )
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
                            error_message=format_error_message_from_exc(exc),
                            trace_id=trace_id,
                        )
                    )
                    _mark_recorded(exc)
                    # One breaker count per *call* (this terminal site), not
                    # per attempt — the retry-continue path below records
                    # nothing on the breaker.
                    if breaker is not None:
                        breaker.record_failure()
                    # Reconnect before raising so the NEXT call starts fresh
                    try:
                        await self._reconnect_server(server, cfg)
                    except Exception as reconnect_exc:
                        # Same reasoning as the protocol-error path above:
                        # the primary failure is being re-raised, the
                        # reconnect attempt is just pre-emptive cleanup.
                        # Redact + no exc_info (#605): the reconnect reopens a
                        # transport with the credentialed ``cfg.url``.
                        logger.debug(
                            "Post-failure reconnect failed for '%s': %s",
                            server,
                            self._redacted_error(reconnect_exc, cfg.url),
                        )
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
                    await self._reconnect_server(server, cfg)
                    conn = self._connections[server]
                except Exception as reconnect_exc:
                    # Redact (#622, #605/#606 family): the reconnect reopens a
                    # transport with the credentialed ``cfg.url``, so an httpx
                    # error here embeds the token — the three sibling
                    # reconnect-failure sites in this loop already scrub it.
                    logger.error(
                        "Reconnect to '%s' failed: %s",
                        server,
                        self._redacted_error(reconnect_exc, cfg.url),
                    )
                    # Third terminal exit (#608): a mid-loop reconnect failure
                    # means the upstream is unreachable — count it, or a dead
                    # stdio upstream whose respawns fail escapes the breaker.
                    if breaker is not None:
                        breaker.record_failure()
                    raise

        # The loop always returns on success or raises on every failure path;
        # this is unreachable but satisfies the type checker.
        raise RuntimeError("unreachable: upstream retry loop exited without a result")

    def _shape_response(
        self,
        result: "CallToolResult",
        server: str,
        tool: str,
        *,
        cfg_snap: ProxyConfig,
    ) -> ShapedResponse:
        """Stage 3: split upstream content into text vs non-text under the
        ``max_upstream_chars`` OOM guard.

        ``max_upstream_chars`` is a hard guard against (mis-)behaving upstreams
        returning huge payloads — without it, a 100 MB ``ls -R /`` response would
        be loaded fully into memory and walk through the entire compression
        pipeline before any ``max_chars`` truncation could apply. ``result.content
        or []`` tolerates spec-noncompliant upstreams that return ``None`` instead
        of an empty list.

        Records no metrics and performs no early return: when no text remains it
        returns a ``ShapedResponse`` whose ``passthrough`` tells the caller to
        record the 0/0 passthrough metric and return either the non-text list or
        the ``"[empty response]"`` sentinel (#558). The caller stays the single
        owner of metric writes and return shapes (R8).
        """
        max_upstream = cfg_snap.max_upstream_chars
        text_parts: list[str] = []
        non_text_content: list = []
        non_text_before_first_text = 0
        first_text_content = None
        total_chars = 0
        oversize = False
        for content in result.content or []:
            if content.type == "text":
                if oversize:
                    continue
                if not text_parts:
                    # Anchor for the final return shape: the processed text is
                    # reinserted where the upstream's FIRST text block sat, so
                    # non-text blocks keep their relative positions.
                    non_text_before_first_text = len(non_text_content)
                    first_text_content = content
                remaining = max_upstream - total_chars
                if remaining <= 0:
                    oversize = True
                    continue
                # ``content.text or ""`` tolerates spec-noncompliant upstreams
                # that return ``None`` for a TextContent's ``text`` field.
                # MCP spec requires ``text: str`` but mirrors the same gap
                # that PR #114 fixed for ``result.content`` itself.
                text = content.text or ""
                if len(text) > remaining:
                    text_parts.append(text[:remaining])
                    total_chars += remaining
                    oversize = True
                    continue
                text_parts.append(text)
                total_chars += len(text)
            else:
                non_text_content.append(content)
        if oversize:
            notice = (
                f"\n\n[response truncated to {max_upstream} chars at "
                f"max_upstream_chars guard — upstream returned an oversized payload]"
            )
            text_parts.append(notice)
            logger.warning(
                "Upstream %s/%s exceeded max_upstream_chars=%d — truncating",
                server,
                tool,
                max_upstream,
            )

        if not text_parts:
            return ShapedResponse(
                original_text="",
                non_text_content=non_text_content,
                passthrough=ShapePassthrough(has_non_text=bool(non_text_content)),
            )
        return ShapedResponse(
            original_text="\n".join(text_parts),
            non_text_content=non_text_content,
            non_text_before_first_text=non_text_before_first_text,
            first_text_content=first_text_content,
        )

    async def _compress_and_surface(
        self,
        *,
        server: str,
        tool: str,
        upstream_args: dict[str, Any],
        cleaned: str,
        tc: ToolConfig,
        cfg_snap: ProxyConfig,
        context_query: str | None,
        trace_id: str | None,
    ) -> CompressionResult:
        """Stages 2+3: compress (or build a progressive first chunk), then surface.

        Kept atomic on purpose (R6): the PROGRESSIVE branch surfaces *before* the
        compress branch runs, and ``progressive_fallback`` (set only in the
        compress branch's ratio-guard ladder) selects which surfacing helper runs
        — splitting compress from surface would re-introduce the implicit flag
        threading this refactor removes.

        Returns a ``CompressionResult`` carrying both ``compressed`` (pre-surfacing,
        the cache payload) and ``surfaced`` (post-surfacing, the return/index
        input), the branch-dependent ``compressed_chars_for_metrics``, the
        fully-mutated ``metrics_strategy`` label, and the surfacing outcome. The
        scorer-fallback delta is computed by the caller (it brackets this call plus
        the later index/extract stages), so it is intentionally not a field here.
        """
        # ``effective_compression`` is the strategy actually used (with AUTO
        # already resolved). ``ratio_violation`` is set by the post-compression
        # guard below when the compressor cut more than ``min_result_retention``
        # allows — it feeds into metrics for auditing R4 after the fact.
        effective_compression: CompressionStrategy = tc.compression
        ratio_violation = False
        # F6: populated only when the call runs the progressive surfacing path.
        # ``None`` elsewhere (non-progressive path, or ``injection_mode='prepend'``
        # which skips for offset-invariant reasons).
        surfacing_on_progressive_ok: bool | None = None
        surface_error: str | None = None
        # Set True when the primary PROGRESSIVE path degrades to a full-content
        # passthrough after a store failure (below). Read at the cache-store
        # gate to keep that transient-failure-degraded response out of the cache.
        progressive_passthrough_on_error = False
        # Set True when the SELECTIVE/HYBRID path degrades to a boundary-aware
        # truncation after a pending-store write failure (below). Defined here
        # (not only in the else-branch) because the shared CompressionResult
        # construction at the end reads it on the PROGRESSIVE path too.
        selective_store_error = False
        if tc.compression == CompressionStrategy.PROGRESSIVE and tc.progressive:
            pcfg = tc.progressive
            if len(cleaned) <= pcfg.chunk_size:
                # Content fits in one chunk — passthrough
                compressed = cleaned
            else:
                try:
                    compressed = self._apply_progressive(
                        cleaned, pcfg, server, tool, sel_cfg=tc.selective, trace_id=trace_id
                    )
                except Exception:
                    # Progressive build/store failed (e.g. a SQLite-backed
                    # pending/reads store I/O error inside ``_apply_progressive``).
                    # Degrade to a zero-loss passthrough of the full cleaned
                    # upstream content instead of letting the exception escape
                    # ``_call_tool_inner`` — an escape here is recorded as
                    # INTERNAL_ERROR and DISCARDS an otherwise-successful upstream
                    # response. The ratio-guard fallback already wraps its Tier-1
                    # progressive call below; the primary PROGRESSIVE path now
                    # mirrors that best-effort handling, but stays zero-loss
                    # (passthrough, not lossy hybrid/truncate) because the
                    # PROGRESSIVE strategy has no retention floor to satisfy.
                    logger.warning(
                        "Progressive delivery failed for %s/%s; returning full "
                        "cleaned content (passthrough)",
                        server,
                        tool,
                        exc_info=True,
                    )
                    compressed = cleaned
                    progressive_passthrough_on_error = True
            _compress_ms = 0.0
            compressed_chars_for_metrics = len(cleaned)
            # The metrics record at the bottom reads ``metrics_strategy`` on
            # every path; mirror the assignment at the top of the compression
            # branch (pre-fix this branch left it unbound and every
            # PROGRESSIVE-strategy call died with UnboundLocalError).
            metrics_strategy = effective_compression.value
            if progressive_passthrough_on_error:
                # Surface the degradation in telemetry, consistent with the
                # ``X→Y_fallback`` labels the ratio-guard ladder records below.
                metrics_strategy = f"{effective_compression.value}→passthrough_on_error"
            # F6: surface on progressive when ``injection_mode`` is append/section.
            # ``prepend`` stays skipped (offset-shift); helper returns ``ok=None``
            # in that case and logs a one-time WARNING.
            _t0 = _time.monotonic()
            (
                surfaced,
                surfacing_on_progressive_ok,
                surface_error,
            ) = await self._apply_surfacing_on_progressive(
                server,
                tool,
                {**upstream_args, "_stm_source_response_chars": len(cleaned)},
                compressed,
                trace_id=trace_id,
                context_query=context_query,
            )
            _surface_ms = (_time.monotonic() - _t0) * 1000
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
                try:
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
                except sqlite3.Error:
                    # sqlite is reachable in this path ONLY via the
                    # SELECTIVE/HYBRID chunk-TOC pending-store write
                    # (``SQLitePendingStore``, opt-in). Scope the degrade to
                    # those two strategies: a sqlite error surfacing from any
                    # other path (a future sqlite-touching compressor, a mocked
                    # store in a test) is not this fault and must not be
                    # relabeled as a store degradation — re-raise it to the
                    # INTERNAL_ERROR path, exactly its behavior before this guard.
                    if effective_compression not in (
                        CompressionStrategy.SELECTIVE,
                        CompressionStrategy.HYBRID,
                    ):
                        raise
                    # SELECTIVE / HYBRID persist a chunk TOC to the (opt-in)
                    # sqlite pending store; a store fault there — a writer
                    # holding the lock past the busy timeout, disk-full, a
                    # corrupt DB — would otherwise escape ``_call_tool_inner``
                    # and DISCARD this otherwise-successful upstream response as
                    # INTERNAL_ERROR. Degrade to a lossy-but-immediate
                    # boundary-aware truncation at the retention budget (the
                    # same terminal tier the ratio-guard ladder falls back to),
                    # mirroring the PROGRESSIVE passthrough guard above.
                    logger.warning(
                        "Selective/hybrid pending-store write failed for %s/%s; "
                        "returning boundary-aware truncation (degraded)",
                        server,
                        tool,
                        exc_info=True,
                    )
                    _fb_scorer = self._relevance_scorer
                    compressed = await self._compress_maybe_offthread(
                        TruncateCompressor(scorer=_fb_scorer),
                        cleaned,
                        max_chars=effective_max_chars,
                        context_query=context_query,
                        scorer=_fb_scorer,
                    )
                    llm_fallback = None
                    selective_store_error = True
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
            if selective_store_error:
                # Terminal truncation already applied above — surface the
                # degradation in telemetry and skip the ratio-guard ladder
                # (truncate is the ladder's own terminal tier).
                metrics_strategy = f"{effective_compression.value}→truncate_on_store_error"
            if llm_fallback:
                metrics_strategy = f"llm_summary→{llm_fallback}_fallback"
            progressive_fallback = False  # set when progressive replaces the response
            if cleaned_len > 0 and dynamic > 0 and not selective_store_error:
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
                                compressed = self._apply_progressive(
                                    cleaned,
                                    pcfg,
                                    server,
                                    tool,
                                    sel_cfg=tc.selective,
                                    trace_id=trace_id,
                                )
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
                            # Canonical heading count (shared with
                            # auto_select_strategy) — the old ``count("\n#")``
                            # missed a heading at offset 0 and over-counted any
                            # ``#`` not followed by whitespace, so the ladder and
                            # AUTO could disagree on whether content is "markdown".
                            heading_count = count_markdown_headings(cleaned)
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
                            _fb_scorer = self._relevance_scorer
                            compressed = await self._compress_maybe_offthread(
                                TruncateCompressor(scorer=_fb_scorer),
                                cleaned,
                                max_chars=effective_max_chars,
                                context_query=context_query,
                                scorer=_fb_scorer,
                            )
                            metrics_strategy = f"{original_strategy}→truncate_fallback"
                            # Re-check the ratio symmetrically with the hybrid
                            # tier (which gates on the floor): truncate is the
                            # terminal tier, so we only record the outcome — but
                            # an operator still needs to see when even the
                            # "guaranteed floor" tier landed below it (e.g.
                            # tail-anomaly content that fills far less than the
                            # budget). Pre-fix this path logged unconditionally
                            # without comparing against the floor.
                            truncate_ratio = len(compressed) / cleaned_len if cleaned_len else 0
                            if truncate_ratio >= dynamic:
                                logger.info(
                                    "Ratio guard truncate fallback for %s/%s: %s "
                                    "(ratio %.3f→%.3f, budget=%d)",
                                    server,
                                    tool,
                                    metrics_strategy,
                                    compressed_ratio,
                                    truncate_ratio,
                                    effective_max_chars,
                                )
                            else:
                                logger.warning(
                                    "Ratio guard truncate fallback for %s/%s still below floor: %s "
                                    "(ratio %.3f→%.3f < %.3f, budget=%d)",
                                    server,
                                    tool,
                                    metrics_strategy,
                                    compressed_ratio,
                                    truncate_ratio,
                                    dynamic,
                                    effective_max_chars,
                                )

            # Record metrics BEFORE surfacing (surfacing adds content, not compresses)
            compressed_chars_for_metrics = len(compressed)

            # ── Stage 3: SURFACE (proactive memory injection) ──
            # F6: progressive_fallback now routes through the same
            # mode-aware helper the PROGRESSIVE branch uses — append/section
            # surfaces, prepend still skips to preserve offset arithmetic for
            # ``stm_proxy_read_more``.
            if progressive_fallback:
                with traced(
                    "proxy_call_surface",
                    metadata={"server": server, "tool": tool, "path": "progressive_fallback"},
                ):
                    _t0 = _time.monotonic()
                    (
                        surfaced,
                        surfacing_on_progressive_ok,
                        surface_error,
                    ) = await self._apply_surfacing_on_progressive(
                        server,
                        tool,
                        {**upstream_args, "_stm_source_response_chars": len(cleaned)},
                        compressed,
                        trace_id=trace_id,
                        context_query=context_query,
                    )
                    _surface_ms = (_time.monotonic() - _t0) * 1000
            else:
                with traced(
                    "proxy_call_surface",
                    metadata={"server": server, "tool": tool},
                ):
                    _t0 = _time.monotonic()
                    surfaced = await self._apply_surfacing(
                        server,
                        tool,
                        {**upstream_args, "_stm_source_response_chars": len(cleaned)},
                        compressed,
                        trace_id=trace_id,
                        context_query=context_query,
                    )
                    _surface_ms = (_time.monotonic() - _t0) * 1000

        return CompressionResult(
            compressed=compressed,
            surfaced=surfaced,
            compressed_chars_for_metrics=compressed_chars_for_metrics,
            metrics_strategy=metrics_strategy,
            ratio_violation=ratio_violation,
            effective_compression=effective_compression,
            progressive_passthrough_on_error=progressive_passthrough_on_error,
            surfacing_on_progressive_ok=surfacing_on_progressive_ok,
            surface_error=surface_error,
            compress_ms=_compress_ms,
            surface_ms=_surface_ms,
            selective_store_error=selective_store_error,
        )

    async def _run_index_stage(
        self,
        *,
        server: str,
        tool: str,
        upstream_args: dict[str, Any],
        tc: ToolConfig,
        cfg_snap: ProxyConfig,
        cleaned: str,
        original_text: str,
        surfaced: str,
        compressed_chars_for_metrics: int,
        context_query: str | None,
    ) -> IndexResult:
        """Stage 4: optional auto-indexing (privacy-skip / background-footer /
        sync / disabled). Returns the response body to continue with plus the
        tri-state index outcome. The gate reads the ``cfg_snap`` snapshot, but the
        inner ``_auto_index_response`` keeps reading live ``self._config`` exactly
        as before — this extraction relocates only the gate, not that behavior.
        """
        # Track outcome for CallMetrics below. ``None`` means "stage did not
        # run for this call" — either disabled, engine missing, or content
        # below min_chars. ``False`` means "ran and failed"; dashboards must
        # distinguish the two.
        index_ok: bool | None = None
        index_error: str | None = None
        chunks_indexed = 0
        ai_cfg = cfg_snap.auto_index
        if (
            tc.auto_index_enabled
            and self._index_engine is not None
            and len(cleaned) >= ai_cfg.min_chars
        ):
            if ai_cfg.background and index_content_matches_privacy(
                server, tool, upstream_args, cleaned, context_query=context_query
            ):
                # #453: the ``[Indexing…] · scheduled`` placeholder below
                # would promise an indexing run that the task's own privacy
                # gate is about to decline — pre-check with the SAME
                # predicate and return the un-footered response instead of
                # scheduling. Records what the skipped task would have
                # recorded, and mirrors the sync skip's metrics shape
                # (ok=True / 0 chunks).
                logger.info(
                    "Auto-index skipped for %s/%s: content matches a privacy pattern",
                    server,
                    tool,
                )
                self.index_observability.record_attempt(tool, "auto_index")
                self.index_observability.record_outcome(tool, "privacy_skip")
                final_result = surfaced
                index_ok = True
            elif ai_cfg.background:
                # F4: schedule indexing off the request path. The placeholder
                # footer ([Indexing…] … · scheduled, namespace dropped) ships
                # to the agent synchronously while the indexing task runs in
                # the background. Trade-off: response no longer guarantees
                # read-your-own-writes for the next tool call — opt-in only
                # (default ai_cfg.background = False preserves sync contract).
                ns = ai_cfg.namespace.format(server=server, tool=tool)
                final_result = compose_index_footer(
                    server=server,
                    tool=tool,
                    original_chars=len(original_text),
                    compressed_chars=compressed_chars_for_metrics,
                    text=cleaned,
                    agent_summary=surfaced,
                    ns=ns,
                    chunks=None,
                )
                index_task = asyncio.create_task(
                    self._auto_index_response(
                        server,
                        tool,
                        upstream_args,
                        cleaned,
                        agent_summary=surfaced,
                        compression_strategy=tc.compression.value,
                        original_chars=len(original_text),
                        compressed_chars=compressed_chars_for_metrics,
                        context_query=context_query,
                    )
                )
                self._background_tasks.add(index_task)
                index_task.add_done_callback(
                    functools.partial(self._on_background_task_done, "auto_index", server, tool)
                )
                # index_ok / index_error / chunks_indexed stay None / None / 0
                # — tri-state matches background extraction. Dashboards filter
                # background rows with WHERE index_ok IS NULL.
            else:
                with traced(
                    "proxy_call_index",
                    metadata={"server": server, "tool": tool},
                ):
                    try:
                        # A1 fix: pre-surfacing ``compressed_chars_for_metrics``
                        # matches what we record in the metrics row below, so the
                        # indexed frontmatter and the dashboards agree on what
                        # "compressed_chars" means for this call. Pre-A1 the
                        # frontmatter recorded ``len(surfaced)`` (post-surfacing),
                        # which drifted from the metrics value whenever surfacing
                        # added content.
                        outcome = await self._auto_index_response(
                            server,
                            tool,
                            upstream_args,
                            cleaned,
                            agent_summary=surfaced,
                            compression_strategy=tc.compression.value,
                            original_chars=len(original_text),
                            compressed_chars=compressed_chars_for_metrics,
                            context_query=context_query,
                        )
                        final_result = outcome.summary
                        index_ok = outcome.ok
                        index_error = outcome.error
                        chunks_indexed = outcome.chunks_indexed
                    except Exception as exc:
                        # Reaches here only for failures outside the inner
                        # ``index_file`` try/except in ``auto_index_response``
                        # (mkdir / atomic write / unexpected errors). The inner
                        # indexing failure is already captured in the outcome.
                        logger.warning(
                            "Auto-index failed for %s/%s — returning unindexed response",
                            server,
                            tool,
                            exc_info=True,
                        )
                        final_result = surfaced
                        index_ok = False
                        index_error = f"{type(exc).__name__}: {exc}"
        else:
            final_result = surfaced

        return IndexResult(
            final_result=final_result,
            index_ok=index_ok,
            index_error=index_error,
            chunks_indexed=chunks_indexed,
        )

    async def _run_extract_stage(
        self,
        *,
        server: str,
        tool: str,
        upstream_args: dict[str, Any],
        tc: ToolConfig,
        cfg_snap: ProxyConfig,
        cleaned: str,
        context_query: str | None,
    ) -> ExtractResult:
        """Stage 4b: optional fact extraction (background by default). Sync path
        populates ``ok`` / ``error``; background path leaves them ``None`` (the
        outcome arrives after the metrics row is committed; background failures
        stay visible via ``memory_ops.extract_and_store``'s WARNING log). Like the
        index stage, the gate reads ``cfg_snap`` but ``_extract_and_store`` keeps
        reading live ``self._config``.
        """
        extract_ok: bool | None = None
        extract_error: str | None = None
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
                task.add_done_callback(
                    functools.partial(self._on_background_task_done, "extract", server, tool)
                )
            else:
                extract_outcome = await self._extract_and_store(
                    server,
                    tool,
                    upstream_args,
                    cleaned,
                    context_query=context_query,
                )
                extract_ok = extract_outcome.ok
                extract_error = extract_outcome.error

        return ExtractResult(ok=extract_ok, error=extract_error)

    @staticmethod
    def _tool_annotations(conn: UpstreamConnection, tool: str) -> Any:
        """Raw MCP ``ToolAnnotations`` for ``tool`` on ``conn`` (``None`` if the
        tool is unknown or carries no annotations). The objects in ``conn.tools``
        are the upstream ``Tool`` records from ``session.list_tools()``, each
        retaining ``.name`` and ``.annotations`` (populated at connect/reconnect)."""
        for t in conn.tools:
            if getattr(t, "name", None) == tool:
                return getattr(t, "annotations", None)
        return None

    def _resolve_cache_ttl(self, server: str, tool: str, *, cfg_snap: ProxyConfig) -> float | None:
        """Effective response-cache TTL (seconds) for a ``(server, tool)`` call.

        Precedence mirrors ``_tool_cache_eligible``'s ``cache`` on/off override:

          1. explicit per-tool ``cache_ttl_seconds`` (``ToolOverrideConfig``),
          2. explicit per-server ``cache_ttl_seconds`` (``UpstreamServerConfig``),
          3. the global ``CacheConfig.default_ttl_seconds``.

        At levels 1-2, ``None`` means *inherit the next level* (NOT never-expires);
        only the global default's ``None`` means never-expires. Every level —
        per-tool, per-server, and the global default — is read from the caller's
        hot-reloaded ``cfg_snap``, so a TTL edit applies on the next call without
        a reconnect. An unknown server (direct dispatch / tests with no
        registered connection) falls back to the global, mirroring
        ``_tool_cache_eligible`` returning ``True`` on that path."""
        global_ttl = cfg_snap.cache.default_ttl_seconds
        conn = self._connections.get(server)
        if conn is None:
            return global_ttl
        srv_cfg = self._server_cfg(conn, cfg_snap)
        override = srv_cfg.tool_overrides.get(tool)
        if override is not None and override.cache_ttl_seconds is not None:
            return override.cache_ttl_seconds
        if srv_cfg.cache_ttl_seconds is not None:
            return srv_cfg.cache_ttl_seconds
        return global_ttl

    def _tool_cache_eligible(self, server: str, tool: str, *, cfg_snap: ProxyConfig) -> bool:
        """Whether a ``(server, tool)`` response may enter / be served from the
        response cache.

        Orthogonal to the privacy and transient-key store guards, which always
        apply in ``_store_cache`` regardless of this verdict. Resolution order:

          1. explicit per-tool ``cache`` override (``ToolOverrideConfig.cache``),
          2. explicit per-server ``cache`` override (``UpstreamServerConfig.cache``),
          3. the global ``CacheConfig.tool_annotation_policy`` applied to the
             upstream tool's ``readOnlyHint`` / ``destructiveHint``.

        An unknown server (direct dispatch / tests with no registered connection)
        is treated as eligible, preserving the pre-gate behavior on that path."""
        conn = self._connections.get(server)
        if conn is None:
            return True
        # Per-tool / per-server ``cache`` overrides ride the hot-reloaded
        # ``cfg_snap``, mirroring ``_resolve_tool_config`` — an override edited
        # after startup applies on the next call, same as the annotation POLICY
        # below. Tool *annotations*, by contrast, still come from the
        # ``conn.tools`` snapshot, refreshed only at connect/reconnect or on a
        # ``tools/list_changed`` notification (#557).
        srv_cfg = self._server_cfg(conn, cfg_snap)
        override = srv_cfg.tool_overrides.get(tool)
        if override is not None and override.cache is not None:
            return override.cache
        if srv_cfg.cache is not None:
            return srv_cfg.cache

        policy = cfg_snap.cache.tool_annotation_policy
        if policy == "ignore":
            return True
        ann = self._tool_annotations(conn, tool)
        read_only = getattr(ann, "readOnlyHint", None)
        destructive = getattr(ann, "destructiveHint", None)
        if policy == "strict":
            # Only an explicit read-only declaration qualifies; a missing
            # readOnlyHint defaults to may-mutate per the MCP spec.
            return read_only is True
        # conservative: cache unless the tool self-declares as a writer.
        # NOTE — contradictory annotations (readOnlyHint=True AND
        # destructiveHint=True) are treated as a writer ON PURPOSE, deviating
        # from the spec-literal reading (the MCP spec scopes destructiveHint to
        # readOnlyHint=false tools, under which an explicit read-only claim
        # would win). A tool contradicting itself is a mis-annotation signal,
        # and the failure directions are asymmetric: trusting the read-only
        # claim on a sloppily-annotated writer replays its side effect from
        # cache, while distrusting it merely costs one tool a cache slot — an
        # operator who knows better can force it back with a per-tool
        # ``cache: true``. Pinned by test_destructive_wins_over_read_only_claim.
        return not (read_only is False or destructive is True)

    def _tool_idempotent_for_retry(self, server: str, tool: str, *, cfg_snap: ProxyConfig) -> bool:
        """Whether a timed-out call to ``(server, tool)`` may be re-invoked (#578).

        A per-attempt timeout cancels OUR wait, not the upstream execution — the
        request may already have committed its side effect, so re-invoking a
        non-read-only tool manufactures duplicate writes the client never asked
        for. This gate is deliberately NOT ``_tool_cache_eligible``:

        - An explicit ``cache: true`` override still counts as the operator's
          "effectively read-only" assertion (a coherent replay-safety claim),
          but ``cache: false`` — documented for *volatile read* tools — says
          nothing about replay safety and falls through to the annotations
          instead of needlessly killing their timeout-retry.
        - Under BOTH ``strict`` and ``conservative`` annotation policies only an
          explicit ``readOnlyHint=True`` (without a contradicting
          ``destructiveHint=True``) qualifies. ``conservative`` is
          strict-shaped here on purpose: a conservative *cache* verdict serves
          a stored response (no re-execution), while a conservative *retry*
          verdict would RE-EXECUTE an unknown tool's side effect — the failure
          directions are not symmetric, so unknown means don't replay.
        - ``policy == "ignore"`` returns True: the operator declared the
          annotations untrustworthy, and deriving a new safety gate from data
          they opted out of would silently change behavior on known-bad input.
          They keep the pre-#578 replay-on-timeout semantics.

        An unknown server (direct dispatch / tests with no registered
        connection) is treated as idempotent, mirroring ``_tool_cache_eligible``.
        """
        conn = self._connections.get(server)
        if conn is None:
            return True
        # Overrides ride the hot-reloaded ``cfg_snap`` (same as
        # ``_tool_cache_eligible``): an operator's replay-safety assertion
        # applies on the next call, not the next reconnect.
        srv_cfg = self._server_cfg(conn, cfg_snap)
        override = srv_cfg.tool_overrides.get(tool)
        if override is not None and override.cache is not None:
            if override.cache is True:
                return True
            # ``cache: false`` set per-tool: fall through to annotations, and
            # skip the per-server override (the per-tool setting shadows it,
            # mirroring the cache-eligibility precedence).
        elif srv_cfg.cache is True:
            return True

        policy = cfg_snap.cache.tool_annotation_policy
        if policy == "ignore":
            return True
        ann = self._tool_annotations(conn, tool)
        read_only = getattr(ann, "readOnlyHint", None)
        destructive = getattr(ann, "destructiveHint", None)
        return read_only is True and destructive is not True

    def _invalidate_disabled_cache(
        self,
        server: str,
        tool: str,
        cache_args: dict[str, Any],
        *,
        cfg_snap: ProxyConfig,
        context_query: str | None = None,
    ) -> None:
        """Best-effort delete of any cached row for ``(server, tool, cache_args)``
        when the resolved TTL is ``<= 0`` (caching disabled), for a non-text /
        mixed response.

        The store-side ``ttl<=0`` self-heal in ``ProxyCache.set`` only runs on the
        TEXT store path (the store is gated text-only). A non-text / mixed response
        never reaches it, so a row left behind by an EARLIER text response for the
        same key survives — and once the TTL is raised back within that row's frozen
        window the lookup serves the stale text (#541). Resolving the TTL is a
        config read (no I/O); the DELETE runs only on the ``ttl<=0`` path, bounded
        to the non-text calls that actually occur — preserving #536's zero-I/O
        posture for the steady-state text / no-row disabled-cache path.

        The key fingerprint is computed HERE (cold path only) rather than by
        callers, keeping the steady-state paths free of the resolve. The DELETE
        therefore targets the current query+fingerprint row only; rows stored
        under other fingerprints are unreachable by any current lookup anyway.
        """
        if self._cache is None:
            return
        ttl = self._resolve_cache_ttl(server, tool, cfg_snap=cfg_snap)
        if ttl is not None and ttl <= 0:
            try:
                self._cache.invalidate(
                    server,
                    tool,
                    cache_args,
                    context_query=context_query,
                    config_fingerprint=self._cache_key_fingerprint(server, tool, cfg_snap=cfg_snap),
                )
            except Exception:
                logger.warning(
                    "Cache invalidation failed for %s/%s — response unaffected",
                    server,
                    tool,
                    exc_info=True,
                )

    def _record_unstorable_response(self, server: str, tool: str, *, cfg_snap: ProxyConfig) -> None:
        """Count a recorded cache miss whose response shape refuses the store.

        The gate mirrors the lookup path in ``_call_tool_guarded`` exactly
        (cache wired, resolved TTL positive/None, tool cache-eligible): a miss
        is recorded only behind that gate, so the counter must only move behind
        the same gate — otherwise force-forwarded (ineligible / ttl<=0 / no-
        cache) calls would report "unstorable" misses that were never counted
        as misses in the first place. Direct ``_call_tool_inner`` callers
        (tests) bypass the lookup AND this accounting stays consistent only
        through production's guarded path; see the miss-accounting comment in
        ``_call_tool_inner``.
        """
        if self._cache is None:
            return
        cache_ttl = self._resolve_cache_ttl(server, tool, cfg_snap=cfg_snap)
        if cache_ttl is not None and cache_ttl <= 0:
            return
        if not self._tool_cache_eligible(server, tool, cfg_snap=cfg_snap):
            return
        self.tracker.record_cache_unstorable()

    def _store_cache(
        self,
        *,
        server: str,
        tool: str,
        cache_args: dict[str, Any],
        comp: CompressionResult,
        non_text_content: list,
        has_result_envelope: bool,
        cfg_snap: ProxyConfig,
        context_query: str | None,
        config_fingerprint: str,
        postprocess_retry_required: bool = False,
    ) -> None:
        """Stage 5: best-effort cache store of the PRE-surfacing ``comp.compressed``
        (so memories stay fresh and surfacing re-runs on a hit).

        Only envelope-safe responses are ever stored: text-only content with no
        result-level ``structuredContent``/``_meta`` (``has_result_envelope``)
        — the ``result TEXT`` schema can reproduce nothing else, and a hit that
        dropped those fields would break the envelope-preservation contract.
        Rows written here are marked ``envelope_safe`` by ``ProxyCache.set``.

        Cache writes are an optional fast-path: a SQLite lock timeout, disk full,
        or any other store error must NOT propagate to the agent and discard a
        successful upstream response — log and continue.

        A response from a tool the cache-eligibility gate rejects
        (``_tool_cache_eligible``: a self-declared writer under the conservative/
        strict annotation policy, or an explicit ``cache: false`` override) is
        skipped too — the lookup path in ``_call_tool_guarded`` mirrors this so the
        tool is neither stored nor served. Beyond that, two responses are
        deliberately NOT cached even for an eligible tool:

        - ``comp.progressive_passthrough_on_error`` — a transient-store-failure
          passthrough. Caching it would pin the degraded (non-chunked) response
          for the cache TTL and suppress progressive delivery on identical calls
          even after the store recovers.
        - ``comp.selective_store_error`` — the SELECTIVE/HYBRID analogue: a
          pending-store write failed and the response degraded to a boundary-aware
          truncation. Caching the lossy truncation would pin it for the TTL and
          suppress the chunk-TOC protocol on identical calls after recovery.
        - one embedding a TRANSIENT retrieval key (progressive first-chunk,
          SELECTIVE/HYBRID TOC): ``compressed`` is a pointer into the process-local
          pending store, whose key dies on restart/eviction or its shorter TTL
          (progressive 1800s / selective 300s) well before the cache TTL (3600s).
          A later cache hit would hand back a dead ``stm_proxy_read_more`` /
          ``stm_proxy_select_chunks`` key.

        Skipping the store makes the next identical call re-run the pipeline and
        mint a fresh, live key. Detection is marker-based (shared with the startup
        legacy purge in ``ProxyCache.initialize``); a false positive only costs one
        un-cached response, never correctness.

        The key uses ``cache_args`` — the pre-``_trace_id`` snapshot — never the
        trace-mutated upstream args; otherwise every entry is keyed on a per-request
        hex and is unreachable by any future lookup (hit rate structurally 0%).
        """
        if self._cache is None:
            return
        if postprocess_retry_required:
            # Cache hits bypass INDEX/EXTRACT. Keep failed sync work and
            # not-yet-resolved background work retryable on the next call.
            self._record_unstorable_response(server, tool, cfg_snap=cfg_snap)
            return
        if non_text_content or has_result_envelope:
            # A non-text / mixed response is never STORED (only its text twin
            # for the same key could have been), and neither is a response
            # carrying result-level envelope fields (``structuredContent`` /
            # ``_meta``): the ``result TEXT`` cache serves plain text, so a
            # hit would silently drop those fields even when the content is
            # text-only. Invalidate the prior row when caching is disabled
            # (resolved ttl<=0); the explicit ttl<=0 branch below does the
            # same for text responses. The non-text-ONLY response
            # early-returns in ``_call_tool_inner`` and invalidates there
            # instead.
            self._record_unstorable_response(server, tool, cfg_snap=cfg_snap)
            self._invalidate_disabled_cache(
                server, tool, cache_args, cfg_snap=cfg_snap, context_query=context_query
            )
            return
        cache_ttl = self._resolve_cache_ttl(server, tool, cfg_snap=cfg_snap)
        if cache_ttl is not None and cache_ttl <= 0:
            # Caching is disabled (resolved ttl<=0): a text response is never
            # stored either — every branch below would either skip the store or
            # call ``set(ttl<=0)`` (which only deletes). Collapse them here so a
            # stale prior positive-TTL row is invalidated regardless of the skip
            # reason (cache-ineligible / progressive passthrough / transient key),
            # not only on the path that happens to reach ``set`` (#541; surfaced
            # by the codex review of #550).
            self._invalidate_disabled_cache(
                server, tool, cache_args, cfg_snap=cfg_snap, context_query=context_query
            )
            return
        if not self._tool_cache_eligible(server, tool, cfg_snap=cfg_snap):
            logger.debug(
                "Skipping cache store for %s/%s: tool is not cache-eligible "
                "(mutating tool under the annotation policy, or cache override=false)",
                server,
                tool,
            )
        elif comp.progressive_passthrough_on_error:
            self.tracker.record_cache_unstorable()
            logger.debug(
                "Skipping cache store for %s/%s: progressive passthrough "
                "degradation (transient store failure)",
                server,
                tool,
            )
        elif comp.selective_store_error:
            self.tracker.record_cache_unstorable()
            logger.debug(
                "Skipping cache store for %s/%s: selective/hybrid truncate "
                "degradation (transient store failure)",
                server,
                tool,
            )
        elif response_carries_transient_key(comp.compressed):
            self.tracker.record_cache_unstorable()
            logger.debug(
                "Skipping cache store for %s/%s: response carries a transient "
                "retrieval key (progressive/selective TOC)",
                server,
                tool,
            )
        else:
            try:
                self._cache.set(
                    server,
                    tool,
                    cache_args,
                    comp.compressed,
                    ttl_seconds=cache_ttl,
                    context_query=context_query,
                    config_fingerprint=config_fingerprint,
                )
            except Exception:
                logger.warning(
                    "Cache store failed for %s/%s — response unaffected",
                    server,
                    tool,
                    exc_info=True,
                )

    async def _call_tool_inner(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        trace_id: str | None = None,
        cfg_snap: ProxyConfig | None = None,
    ) -> str | list | CallToolResult:
        # Public entry point ``call_tool`` generates the trace_id and passes
        # it in so it can match the enclosing Langfuse span. Direct callers
        # (tests and internal dispatch) that don't care about tracing omit
        # the argument and we generate one here.
        if trace_id is None:
            trace_id = uuid.uuid4().hex[:16]
        logger.debug("trace_id=%s server=%s tool=%s", trace_id, server, tool)

        # Snapshot config once to avoid intra-request inconsistency from
        # hot-reload changing the config between accesses.
        # ``_call_tool_guarded`` passes ITS snapshot in so the Stage-5 store
        # keys on the same fingerprint the (missed) lookup used — otherwise a
        # hot reload landing between the two reads would store under a key the
        # stampede lock isn't holding. Direct callers (tests) omit it.
        if cfg_snap is None:
            cfg_snap = self._config

        # Extract _context_query before forwarding. Coerce non-str values to
        # None at the single extraction point — the cache-hit path already
        # sanitizes this way, and without the mirror here the same malformed
        # argument reaches scorers/compressors raw on a miss but is dropped on
        # a hit, so whether it raises depends on cache state.
        raw_context_query = arguments.get("_context_query") if arguments else None
        context_query = raw_context_query if isinstance(raw_context_query, str) else None
        upstream_args = (
            {k: v for k, v in arguments.items() if k != "_context_query"} if arguments else {}
        )
        # Cache-key fingerprint of the resolved compression settings, paired
        # with ``context_query`` everywhere a key is derived below (store +
        # disabled-cache invalidations).
        config_fp = self._cache_key_fingerprint(server, tool, cfg_snap=cfg_snap)

        # Cache lookup, hit path, and miss accounting are all owned by
        # ``_call_tool_guarded``: a hit returns there, an ELIGIBLE miss is
        # recorded there, and the ineligible / no-cache paths intentionally
        # record nothing. By the time we reach here the call is already past that
        # gate, so we just proceed with the upstream fetch. Direct callers (tests)
        # that invoke ``_call_tool_inner`` bypass the lookup and therefore the
        # miss counter too, matching the no-lookup semantics.

        # Snapshot the cache-key args BEFORE injecting ``_trace_id`` below.
        # The cache lookup at L771 used the original args (no ``_trace_id``);
        # if cache.set uses the mutated args, every stored entry is keyed on
        # a per-request random hex and is unreachable by any future lookup
        # (hit rate structurally 0%). Keep upstream args mutated for trace
        # propagation, but persist under the original key.
        cache_args = {**upstream_args}

        # Propagate trace context to upstream server for end-to-end correlation.
        if trace_id is not None:
            upstream_args["_trace_id"] = trace_id

        # ── Stage 1: UPSTREAM FETCH ──
        # Bounded retry + reconnect with per-attempt and overall deadlines.
        # Returns the upstream result or raises after recording the failure
        # metric (and ``_mark_recorded``-ing the exception so the outer
        # ``call_tool`` does not double-record). ``conn``/``cfg``/``delay`` are
        # owned entirely by the helper — nothing after the fetch reads them.
        result = await self._fetch_upstream(
            server, tool, upstream_args, trace_id=trace_id, cfg_snap=cfg_snap
        )

        # ── Stage 3: SHAPE (text/non-text split + max_upstream_chars guard) ──
        # The helper records no metrics and performs no return; when no text
        # remains it signals the early-exit via ``shaped.passthrough`` and the
        # orchestrator owns the metric write + return shape (R8).
        shaped = self._shape_response(result, server, tool, cfg_snap=cfg_snap)
        non_text_content = shaped.non_text_content
        original_text = shaped.original_text
        # Result-level envelope fields (MCP: ``structuredContent`` and
        # ``_meta``, exposed as ``meta`` on the pydantic model). Both are
        # ``dict | None`` per spec; the isinstance narrowing tolerates fakes
        # that model only ``content``/``isError`` (SimpleNamespace misses the
        # attribute, MagicMock fabricates a truthy non-dict) as well as
        # spec-noncompliant upstreams. When either field is present the
        # return shape below is a full ``CallToolResult`` so the fields reach
        # the client verbatim, and the Stage-5 store is bypassed — a
        # ``result TEXT`` cache hit could never reproduce them.
        raw_structured = getattr(result, "structuredContent", None)
        raw_meta = getattr(result, "meta", None)
        structured_content: dict[str, Any] | None = (
            raw_structured if isinstance(raw_structured, dict) else None
        )
        result_meta: dict[str, Any] | None = raw_meta if isinstance(raw_meta, dict) else None
        has_result_envelope = structured_content is not None or result_meta is not None

        # The isError check runs BEFORE the no-text passthrough early-return:
        # a non-text-only (or empty-content) error must surface as an error,
        # not as a passthrough success (previously it leaked as one).
        if result.isError:
            error_text = original_text or _NON_TEXT_ERROR_TEXT
            self.tracker.record_error(
                CallMetrics(
                    server=server,
                    tool=tool,
                    original_chars=len(original_text),
                    compressed_chars=len(original_text),
                    is_error=True,
                    error_category=ErrorCategory.UPSTREAM_ERROR,
                    error_message=error_text[:MAX_ERROR_MESSAGE_CHARS],
                    trace_id=trace_id,
                )
            )
            # The error returns before the Stage-5 store, so an error under a
            # disabled cache (ttl<=0) would otherwise leave a prior cached
            # text row for this key live. Invalidate every error shape.
            self._invalidate_disabled_cache(
                server, tool, cache_args, cfg_snap=cfg_snap, context_query=context_query
            )
            # Preserve the complete MCP error envelope. Raising FastMCP's
            # ToolError would collapse it to one text block and discard
            # images/resources, annotations, structuredContent, and _meta.
            if isinstance(result, mcp_types.CallToolResult):
                return result
            content: list[dict[str, Any]] = []
            for block in result.content or []:
                dumped = block.model_dump(by_alias=True) if hasattr(block, "model_dump") else None
                if isinstance(dumped, dict):
                    content.append(dumped)
                elif getattr(block, "type", None) == "text":
                    content.append({"type": "text", "text": getattr(block, "text", "")})
                else:
                    content.append(vars(block))
            return mcp_types.CallToolResult(
                content=content,
                structuredContent=structured_content,
                _meta=result_meta,
                isError=True,
            )

        if shaped.passthrough is not None:
            # Both passthrough shapes are live upstream calls, so both record the
            # 0/0 metric — otherwise an eligible empty response would carry a
            # recorded miss + unstorable count with no matching call, breaking
            # the ``total_invocations = total_calls + cache_hits + total_errors``
            # reconciliation (#558; deliberately supersedes the R8 "empty
            # records nothing" pin).
            self.tracker.record(
                CallMetrics(
                    server=server,
                    tool=tool,
                    original_chars=0,
                    compressed_chars=0,
                    trace_id=trace_id,
                )
            )
            # Neither shape is ever stored; count the store refusal against the
            # miss already recorded (#558) and mirror the mixed-branch
            # invalidation in ``_store_cache`` so a stale prior text row for
            # this key is dropped while caching is disabled (#541).
            self._record_unstorable_response(server, tool, cfg_snap=cfg_snap)
            self._invalidate_disabled_cache(
                server, tool, cache_args, cfg_snap=cfg_snap, context_query=context_query
            )
            if has_result_envelope:
                # A structured-only / meta-only response is a real result, not
                # an empty one — return the envelope (content preserved as-is,
                # possibly []) instead of the sentinel or a bare list that
                # would drop the fields. ``_meta=`` is the pydantic alias
                # (``populate_by_name`` is unset on mcp types).
                return mcp_types.CallToolResult(
                    content=list(non_text_content),
                    structuredContent=structured_content,
                    _meta=result_meta,
                )
            if shaped.passthrough.has_non_text:
                return non_text_content
            return "[empty response]"

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

        # ── Stages 2+3: COMPRESS (or PROGRESSIVE) + SURFACE ──
        # Capture the scorer fallback counter BEFORE compression so the metrics
        # record below sees a delta covering compression and everything after.
        _pre_scorer_fb = getattr(self._relevance_scorer, "fallback_count", 0)
        comp = await self._compress_and_surface(
            server=server,
            tool=tool,
            upstream_args=upstream_args,
            cleaned=cleaned,
            tc=tc,
            cfg_snap=cfg_snap,
            context_query=context_query,
            trace_id=trace_id,
        )
        # Unpack the fields the metrics record and the INDEX stage read. The
        # cache store reads ``comp`` directly (compressed + the cache-skip flag),
        # so those two are not unpacked here.
        surfaced = comp.surfaced
        compressed_chars_for_metrics = comp.compressed_chars_for_metrics
        metrics_strategy = comp.metrics_strategy
        ratio_violation = comp.ratio_violation
        surfacing_on_progressive_ok = comp.surfacing_on_progressive_ok
        surface_error = comp.surface_error
        _compress_ms = comp.compress_ms
        _surface_ms = comp.surface_ms

        # ── Stage 4: INDEX (optional) + Stage 4b: EXTRACT (optional) ──
        idx = await self._run_index_stage(
            server=server,
            tool=tool,
            upstream_args=upstream_args,
            tc=tc,
            cfg_snap=cfg_snap,
            cleaned=cleaned,
            original_text=original_text,
            surfaced=surfaced,
            compressed_chars_for_metrics=compressed_chars_for_metrics,
            context_query=context_query,
        )
        final_result = idx.final_result
        index_ok = idx.index_ok
        index_error = idx.index_error
        chunks_indexed = idx.chunks_indexed

        ext = await self._run_extract_stage(
            server=server,
            tool=tool,
            upstream_args=upstream_args,
            tc=tc,
            cfg_snap=cfg_snap,
            cleaned=cleaned,
            context_query=context_query,
        )
        extract_ok = ext.ok
        extract_error = ext.error

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
                index_ok=index_ok,
                index_error=index_error,
                chunks_indexed=chunks_indexed,
                extract_ok=extract_ok,
                extract_error=extract_error,
                surfacing_on_progressive_ok=surfacing_on_progressive_ok,
                surface_error=surface_error,
            )
        )

        # ── Stage 5: CACHE STORE (pre-surfacing content, keyed on cache_args) ──
        self._store_cache(
            server=server,
            tool=tool,
            cache_args=cache_args,
            comp=comp,
            non_text_content=non_text_content,
            has_result_envelope=has_result_envelope,
            cfg_snap=cfg_snap,
            context_query=context_query,
            config_fingerprint=config_fp,
            postprocess_retry_required=(
                index_ok is False
                or extract_ok is False
                or (
                    tc.auto_index_enabled
                    and self._index_engine is not None
                    and len(cleaned) >= cfg_snap.auto_index.min_chars
                    and cfg_snap.auto_index.background
                    and index_ok is None
                )
                or (
                    tc.extraction_enabled
                    and self._index_engine is not None
                    and len(cleaned) >= cfg_snap.extraction.min_response_chars
                    and cfg_snap.extraction.background
                    and extract_ok is None
                )
            ),
        )

        # Final return shape. The processed text (single, merged block) is
        # reinserted at the upstream's first-text position so non-text blocks
        # keep their relative order. When the result carries envelope fields
        # the shape is a full ``CallToolResult`` — the text twin is the
        # compressed, token-budgeted view while ``structuredContent``/``_meta``
        # pass verbatim (clients consuming structuredContent get full
        # fidelity). Progressive/selective continuations
        # (``stm_proxy_read_more`` / ``stm_proxy_select_chunks``) return plain
        # text and do not re-carry the envelope.
        if non_text_content:
            k = shaped.non_text_before_first_text
            template = shaped.first_text_content
            if template is not None and hasattr(template, "model_copy"):
                processed_text = template.model_copy(update={"text": final_result})
            else:
                from mcp.types import TextContent

                processed_text = TextContent(type="text", text=final_result)
            content_blocks: list = [
                *non_text_content[:k],
                processed_text,
                *non_text_content[k:],
            ]
            if has_result_envelope:
                return mcp_types.CallToolResult(
                    content=content_blocks,
                    structuredContent=structured_content,
                    _meta=result_meta,
                )
            return content_blocks

        if has_result_envelope:
            from mcp.types import TextContent

            return mcp_types.CallToolResult(
                content=[TextContent(type="text", text=final_result)],
                structuredContent=structured_content,
                _meta=result_meta,
            )
        return final_result
