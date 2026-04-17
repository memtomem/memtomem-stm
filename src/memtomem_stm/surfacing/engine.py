"""Proactive memory surfacing engine."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from memtomem_stm.observability.tracing import traced
from memtomem_stm.surfacing.cache import SurfacingCache
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.context_extractor import ContextExtractor
from memtomem_stm.surfacing.formatter import SurfacingFormatter
from memtomem_stm.surfacing.relevance import RelevanceGate
from memtomem_stm.utils.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


class SurfacingEngine:
    """Core proactive memory surfacing engine.

    On each proxied tool call, extracts context, searches LTM via the
    MCP client adapter, and injects relevant memories into the response.

    LTM access is always remote-only via the MCP protocol (no in-process
    SearchPipeline coupling). The adapter is responsible for talking to a
    memtomem MCP server, whether spawned as a child process or addressed
    over an existing transport.
    """

    def __init__(
        self,
        config: SurfacingConfig,
        *,
        mcp_adapter: Any,
        webhook_manager: Any | None = None,
        feedback_tracker: Any | None = None,
    ) -> None:
        self._config = config
        self._mcp_adapter = mcp_adapter
        self._webhook_manager = webhook_manager
        self._feedback_tracker = feedback_tracker
        self._auto_tuner = None
        if config.auto_tune_enabled and feedback_tracker is not None:
            from memtomem_stm.surfacing.feedback import AutoTuner

            self._auto_tuner = AutoTuner(config, feedback_tracker.store)
        self._extractor = ContextExtractor()
        self._gate = RelevanceGate(config)
        self._formatter = SurfacingFormatter(config)
        self._cache = SurfacingCache(ttl=config.cache_ttl_seconds)
        self._circuit_breaker = CircuitBreaker(
            max_failures=config.circuit_max_failures,
            reset_timeout=config.circuit_reset_seconds,
        )
        # Track memory IDs surfaced — insertion-ordered dict for FIFO eviction.
        # Seeded from persistent store for cross-session dedup.
        # Cap at 10k entries to prevent unbounded growth in long sessions.
        self._surfaced_ids: dict[str, None] = {}
        self._surfaced_ids_max = 10000
        if feedback_tracker is not None and config.dedup_ttl_seconds > 0:
            try:
                self._surfaced_ids = dict.fromkeys(
                    feedback_tracker.store.get_seen_ids(config.dedup_ttl_seconds)
                )
                if self._surfaced_ids:
                    logger.debug(
                        "Loaded %d seen memory IDs for cross-session dedup",
                        len(self._surfaced_ids),
                    )
            except Exception:
                logger.warning("Failed to load cross-session seen IDs", exc_info=True)
        # In-memory boost guard — at most one mem_do(increment_access) call
        # per surfacing event, even if the agent fires multiple "helpful"
        # ratings for it. Insertion-ordered dict for FIFO eviction; cap at
        # 10k matches the sibling _surfaced_ids bound.
        self._boosted_event_ids: dict[str, None] = {}
        self._boosted_event_ids_max = 10000
        # Cache invalidation set — (server, tool, memory_id) tuples the agent
        # rated ``not_relevant`` or ``already_known``. ``_render_cached``
        # filters cache hits through this set so a cached query does not
        # resurface memories the agent already rejected within the cache TTL
        # window. Scoped in-memory per session (matching ``SurfacingCache``
        # lifetime); bounded by the same 10k FIFO cap as sibling sets since
        # invalidations are a strict subset of surfacings.
        self._invalidated_ids: dict[tuple[str, str, str], None] = {}
        self._invalidated_ids_max = 10000
        self._background_tasks: set[asyncio.Task] = set()
        # Per-key stampede guard — identical concurrent ``_do_surface`` calls
        # serialize on the same lock so a cache miss triggers one LTM search
        # rather than N and the losing coroutine cannot overwrite the
        # winning coroutine's populated cache entry with its own (empty due
        # to session dedup) result. Entries are popped while the lock is
        # still held so any queued waiter sees the cached result on its
        # own double-check. Named ``_key_locks`` to match the same pattern
        # used by ``ProxyManager`` (extractable into a shared helper later).
        self._key_locks: dict[str, asyncio.Lock] = {}
        # Opportunistic cleanup: run cleanup_expired at most once per hour
        self._cleanup_interval = 3600.0
        self._last_cleanup: float = time.monotonic()

    @property
    def injection_mode(self) -> str:
        """Formatter injection mode — ``"prepend"``, ``"append"``, or ``"section"``.

        Read by ``ProxyManager`` to decide whether progressive-path surfacing
        is safe: only ``append``/``section`` keep the ``split("\\n---\\n")[0]``
        concat invariant that ``stm_proxy_read_more`` depends on.
        """
        return self._config.injection_mode

    async def surface(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        response_text: str,
        *,
        trace_id: str | None = None,
    ) -> str:
        """Surface relevant memories and inject into response_text.

        Returns the original response_text unmodified if:
        - surfacing is disabled
        - circuit breaker is open
        - relevance gate rejects the call
        - timeout exceeded
        """
        if not self._config.enabled:
            return response_text

        self._maybe_cleanup_expired()

        if len(response_text) < self._config.min_response_chars:
            return response_text

        if self._circuit_breaker.is_open:
            logger.debug("Surfacing skipped: circuit breaker open for %s/%s", server, tool)
            return response_text

        query = self._extractor.extract_query(server, tool, arguments, self._config)
        if query is None or not self._gate.should_surface(server, tool, query):
            logger.debug(
                "Surfacing skipped: gate rejected %s/%s (query=%s)",
                server,
                tool,
                query[:50] if query else None,
            )
            return response_text

        try:
            result = await asyncio.wait_for(
                self._do_surface(server, tool, arguments, response_text, query, trace_id=trace_id),
                timeout=self._config.timeout_seconds,
            )
            self._circuit_breaker.record_success()
            return result
        except asyncio.TimeoutError:
            logger.warning(
                "Surfacing timed out for %s/%s (%.1fs limit)",
                server,
                tool,
                self._config.timeout_seconds,
            )
            return response_text
        except Exception:
            logger.warning("Surfacing failed for %s/%s", server, tool, exc_info=True)
            self._circuit_breaker.record_failure()
            return response_text

    async def handle_feedback(
        self,
        surfacing_id: str,
        rating: str,
        memory_id: str | None = None,
    ) -> str:
        """Record feedback for a surfaced memory.

        On a ``helpful`` rating, also boosts the chunk's ``access_count`` in
        core via ``mem_do(action="increment_access")`` so the search pipeline
        can rank it higher next time. The boost is guarded with an in-memory
        per-event set so multiple "helpful" ratings for the same surfacing
        event only trigger one increment.

        On a ``not_relevant`` or ``already_known`` rating, adds the
        ``(server, tool, memory_id)`` tuples to ``_invalidated_ids`` so
        subsequent cache hits for the same ``server/tool/query`` filter
        them out. Without this, a repeat query inside the ``SurfacingCache``
        TTL window would resurface the memory the agent just rejected.
        """
        if self._feedback_tracker is None:
            return "Feedback tracking is not enabled."

        result = self._feedback_tracker.record_feedback(surfacing_id, rating, memory_id)

        if rating in ("not_relevant", "already_known"):
            self._invalidate_cache_for_feedback(surfacing_id, memory_id)

        if rating == "helpful" and surfacing_id not in self._boosted_event_ids:
            # Claim the guard optimistically BEFORE the increment_access
            # await so a concurrent "helpful" for the same surfacing_id
            # observes the claim and short-circuits. Rolled back on failure
            # so the documented "retry on failure" behavior is preserved.
            self._boosted_event_ids[surfacing_id] = None
            try:
                if memory_id:
                    target_ids: list[str] = [memory_id]
                else:
                    target_ids = self._feedback_tracker.store.get_memory_ids_for_surfacing(
                        surfacing_id
                    )
                if target_ids:
                    with traced(
                        "surfacing_feedback_boost",
                        metadata={
                            "surfacing_id": surfacing_id,
                            "chunk_count": len(target_ids),
                        },
                    ):
                        await self._mcp_adapter.increment_access(target_ids)
                    # Prune if exceeded cap — evict oldest (first-inserted) entries.
                    if len(self._boosted_event_ids) > self._boosted_event_ids_max:
                        excess = len(self._boosted_event_ids) - self._boosted_event_ids_max // 2
                        for k in list(self._boosted_event_ids)[:excess]:
                            del self._boosted_event_ids[k]
                else:
                    # No memories to boost — release the guard so a later
                    # call with a resolvable memory_id can retry.
                    self._boosted_event_ids.pop(surfacing_id, None)
            except Exception:
                self._boosted_event_ids.pop(surfacing_id, None)
                logger.debug(
                    "Failed to boost access_count for surfacing %s",
                    surfacing_id,
                    exc_info=True,
                )

        return result

    def _invalidate_cache_for_feedback(self, surfacing_id: str, memory_id: str | None) -> None:
        """Populate ``_invalidated_ids`` from a surfacing event.

        Looks up the event's ``(server, tool, memory_ids)`` and adds one
        tuple per memory id to the filter set. When ``memory_id`` is given
        only that id is invalidated; otherwise every memory recorded for
        the surfacing event is invalidated (i.e. a blanket rejection).
        """
        if self._feedback_tracker is None:
            return
        try:
            event = self._feedback_tracker.store.get_surfacing_event(surfacing_id)
        except Exception:
            logger.debug(
                "Failed to look up surfacing event for invalidation of %s",
                surfacing_id,
                exc_info=True,
            )
            return
        if event is None:
            return
        server = event["server"]
        tool = event["tool"]
        target_ids = [memory_id] if memory_id else event["memory_ids"]
        for mid in target_ids:
            self._invalidated_ids[(server, tool, mid)] = None
        if len(self._invalidated_ids) > self._invalidated_ids_max:
            excess = len(self._invalidated_ids) - self._invalidated_ids_max // 2
            for k in list(self._invalidated_ids)[:excess]:
                del self._invalidated_ids[k]

    def _render_cached(
        self,
        cached: list[Any],
        response_text: str,
        query: str,
        server: str,
        tool: str,
    ) -> str:
        """Render a cached surfacing result into the response_text, or pass
        the response through unchanged if the cache entry is an empty list
        (the deliberate "no results for this query" case).

        Registers a new surfacing event in the feedback tracker so the agent
        can submit ``stm_surfacing_feedback`` for the rendered surfacing_id.
        Without this, cached hits generate orphan IDs that the feedback store
        cannot resolve, silently breaking the feedback learning loop.

        Filters out memory IDs in ``_invalidated_ids`` — memories the agent
        already rated ``not_relevant`` or ``already_known`` within the cache
        TTL window. A filtered-empty result pass-throughs like the natural
        empty case.
        """
        if cached and self._invalidated_ids:
            original_count = len(cached)
            cached = [
                r for r in cached if (server, tool, str(r.chunk.id)) not in self._invalidated_ids
            ]
            if len(cached) < original_count:
                logger.debug(
                    "Surfacing cache filter: %s/%s %d→%d (invalidated)",
                    server,
                    tool,
                    original_count,
                    len(cached),
                )
        if not cached:
            logger.debug("Surfacing cache hit (empty) for %s/%s", server, tool)
            return response_text
        logger.debug("Surfacing cache hit (%d results) for %s/%s", len(cached), server, tool)
        surfacing_id: str | None = uuid.uuid4().hex[:16]
        if self._feedback_tracker is not None:
            try:
                self._feedback_tracker.record_surfacing(
                    surfacing_id=surfacing_id,
                    server=server,
                    tool=tool,
                    query=query,
                    memory_ids=[str(r.chunk.id) for r in cached],
                    scores=[r.score for r in cached],
                )
            except Exception:
                logger.warning("Failed to record cached surfacing event", exc_info=True)
                surfacing_id = None
        return self._formatter.inject(
            response_text,
            cached,
            query,
            surfacing_id=surfacing_id,
        )

    async def _do_surface(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        response_text: str,
        query: str,
        *,
        trace_id: str | None = None,
    ) -> str:
        # Check surfacing cache (keyed by server+tool+query). The full miss
        # path lives in ``_do_surface_miss``; this shell handles the
        # cache-check fast path, per-key stampede lock, and post-lock
        # double-check so identical concurrent queries share a single LTM
        # search and the losing coroutine cannot poison the cache with an
        # empty result (see ``_key_locks`` init docstring).
        cache_key = f"{server}/{tool}/{query}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return self._render_cached(cached, response_text, query, server, tool)

        lock = self._key_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            try:
                # Double-check inside the lock: a coroutine that held the
                # lock ahead of us may have populated the cache already.
                cached = self._cache.get(cache_key)
                if cached is not None:
                    return self._render_cached(cached, response_text, query, server, tool)
                return await self._do_surface_miss(
                    server,
                    tool,
                    arguments,
                    response_text,
                    query,
                    cache_key,
                    trace_id=trace_id,
                )
            finally:
                self._key_locks.pop(cache_key, None)

    async def _do_surface_miss(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        response_text: str,
        query: str,
        cache_key: str,
        *,
        trace_id: str | None = None,
    ) -> str:
        # Resolve effective config (auto-tuned if enabled)
        tool_cfg = self._config.context_tools.get(tool)
        if self._auto_tuner is not None:
            self._auto_tuner.maybe_adjust(tool)
            min_score = self._auto_tuner.get_effective_min_score(tool)
        else:
            min_score = (
                tool_cfg.min_score if tool_cfg and tool_cfg.min_score else self._config.min_score
            )
        max_results = (
            tool_cfg.max_results
            if tool_cfg and tool_cfg.max_results
            else self._config.effective_max_results()
        )
        namespace = (
            tool_cfg.namespace
            if tool_cfg and tool_cfg.namespace
            else self._config.default_namespace
        )

        # Search LTM via remote MCP client
        ctx_win = self._config.context_window_size or None
        search_kwargs: dict[str, Any] = {}
        if ctx_win:
            search_kwargs["context_window"] = ctx_win
        results, _stats = await self._mcp_adapter.search(
            query=query,
            top_k=max_results * 2,
            namespace=namespace,
            trace_id=trace_id,
            **search_kwargs,
        )

        # Filter by score, then exclude already-surfaced memories in this session
        scored = [r for r in results if r.score >= min_score]
        relevant = []
        for r in scored:
            mid = str(r.chunk.id)
            if mid not in self._surfaced_ids:
                relevant.append(r)
                if len(relevant) >= max_results:
                    break

        # Cache result (even empty, to avoid repeated searches)
        self._cache.set(cache_key, relevant)

        if not relevant:
            logger.debug(
                "Surfacing: no results above min_score=%.2f for %s/%s", min_score, server, tool
            )
            return response_text

        # Record in-memory surfaced IDs EAGERLY — before any await — so a
        # concurrent ``_do_surface`` for an overlapping memory observes the
        # claim at L261 and excludes it. Without this, the await at
        # ``scratch_list`` below opens an interleaving window where both
        # coroutines build ``relevant`` including the same memory and
        # violate the documented session-dedup invariant.
        new_ids = [str(r.chunk.id) for r in relevant]
        for mid in new_ids:
            self._surfaced_ids[mid] = None
        # Prune if exceeded cap — evict oldest (first-inserted) entries.
        if len(self._surfaced_ids) > self._surfaced_ids_max:
            excess = len(self._surfaced_ids) - self._surfaced_ids_max // 2
            keys = list(self._surfaced_ids)[:excess]
            for k in keys:
                del self._surfaced_ids[k]

        self._gate.record_surfacing(query)
        logger.info(
            "Surfacing %d memories for %s/%s (query=%s)", len(relevant), server, tool, query[:50]
        )

        # Session context (working memory): when enabled, fetch scratchpad
        # entries via the MCP adapter and inject alongside LTM hits. Failures
        # are silent — surfacing must still deliver the LTM hits even if
        # working memory is unavailable.
        scratch_items: list[dict] | None = None
        if self._config.include_session_context:
            try:
                scratch_items = await self._mcp_adapter.scratch_list(trace_id=trace_id)
            except Exception:
                logger.debug("Failed to fetch session scratch items", exc_info=True)
                scratch_items = None

        # Generate surfacing ID and record event
        surfacing_id: str | None = uuid.uuid4().hex[:16]
        if self._feedback_tracker is not None:
            try:
                self._feedback_tracker.record_surfacing(
                    surfacing_id=surfacing_id,
                    server=server,
                    tool=tool,
                    query=query,
                    memory_ids=new_ids,
                    scores=[r.score for r in relevant],
                )
            except Exception:
                logger.warning("Failed to record surfacing event", exc_info=True)
                surfacing_id = None

        # Persist seen IDs for cross-session dedup (in-memory guard was
        # claimed above to close the concurrent window).
        if self._feedback_tracker is not None:
            try:
                self._feedback_tracker.store.mark_surfaced(new_ids)
            except Exception:
                logger.warning("Failed to persist seen memory IDs", exc_info=True)

        # Inject memories into response
        result = self._formatter.inject(
            response_text,
            relevant,
            query,
            surfacing_id=surfacing_id,
            scratch_items=scratch_items,
        )

        # Fire webhook (fire-and-forget)
        if self._webhook_manager and self._config.fire_webhook:
            task = asyncio.create_task(
                self._webhook_manager.fire(
                    "surface",
                    {
                        "server": server,
                        "tool": tool,
                        "query": query,
                        "memory_ids": [str(r.chunk.id) for r in relevant],
                        "scores": [r.score for r in relevant],
                        "surfacing_id": surfacing_id,
                    },
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._on_webhook_done)

        return result

    def _on_webhook_done(self, task: asyncio.Task) -> None:
        """Log exceptions from fire-and-forget webhook tasks."""
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("Webhook fire-and-forget task failed: %s", exc)

    async def stop(self) -> None:
        """Cancel and drain pending background tasks (webhooks)."""
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

    def _maybe_cleanup_expired(self) -> None:
        """Run cleanup_expired() at most once per cleanup interval.

        Called opportunistically from surface() — no separate timer thread
        needed. The cleanup itself is synchronous (SQLite DELETE) and fast
        enough to run inline.
        """
        if self._feedback_tracker is None or self._config.dedup_ttl_seconds <= 0:
            return
        now = time.monotonic()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        try:
            deleted = self._feedback_tracker.store.cleanup_expired(self._config.dedup_ttl_seconds)
            if deleted:
                logger.info("Cleaned up %d expired seen_memories entries", deleted)
        except Exception:
            logger.warning("Failed to clean up expired seen_memories", exc_info=True)
