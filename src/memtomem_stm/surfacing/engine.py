"""Proactive memory surfacing engine."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any

from memtomem_stm.observability.tracing import traced
from memtomem_stm.proxy.privacy import contains_sensitive_content
from memtomem_stm.surfacing.cache import SurfacingCache
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.context_extractor import ContextExtractor
from memtomem_stm.surfacing.formatter import SurfacingFormatter
from memtomem_stm.surfacing.mcp_client import LtmCapabilities, SearchOutcome
from memtomem_stm.surfacing.observability import _NOOP_OBSERVABILITY, SurfacingObservability
from memtomem_stm.surfacing.relevance import RelevanceGate
from memtomem_stm.utils.circuit_breaker import CircuitBreaker
from memtomem_stm.utils.redact import redact_url_userinfo

logger = logging.getLogger(__name__)

_QUERY_HASH_PREFIX = "sha256:"
"""Marker prefixed to the truncated sha256 digest written to
``surfacing_events.query`` when ``SurfacingConfig.persist_query_text`` is
``False`` (#352 part 3). Lets the stats formatter recognize the
hashed-form row without re-reading config and lets ad-hoc DB inspection
tell user-derived text apart from a stable opaque ID. The full stored
value is ``"sha256:" + 16-hex-char digest`` → 23 chars total."""

_SCORE_SCALE_WARNING_STREAK = 5
"""Fresh LTM searches required before warning about a score-scale mismatch.

This is an advisory tripwire rather than a tuning knob: exposing it as config
would imply that operators should calibrate the detector instead of fixing the
embedding/search path or intentionally choosing a lower ``min_score``.
"""


@dataclass
class _ScoreScaleStreak:
    threshold: float
    count: int
    observed_max: float


class _DependencyFault(RuntimeError):
    """Internal signal: return passthrough but count the dependency failure."""


class _OperationalSkip(RuntimeError):
    """Internal signal for daemon startup/load shedding without breaker mutation."""


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
        token_tracker: Any | None = None,
        observability: SurfacingObservability | None = None,
        record_feedback_events: bool = True,
    ) -> None:
        self._config = config
        self._mcp_adapter = mcp_adapter
        self._webhook_manager = webhook_manager
        self._feedback_tracker = feedback_tracker
        self._token_tracker = token_tracker
        # Decouple cross-session dedup from feedback-event recording. When a
        # tracker is attached, it normally does two independent jobs: (1) seed
        # ``_surfaced_ids`` + ``mark_surfaced`` for dedup (``seen_memories`` —
        # memory IDs only, privacy-clean), and (2) mint a ``surfacing_id`` +
        # ``record_surfacing`` the extracted query into ``surfacing_events`` +
        # advertise a ``stm_surfacing_feedback`` rating prompt. The hook daemon
        # path wants (1) but not (2): it has no in-band channel for the model to
        # return a rating (the prompt would be unresolvable), and the query for
        # ``Bash`` may carry secrets we refuse to persist. Set this ``False`` to
        # keep dedup while skipping all feedback-event recording/prompting.
        self._record_feedback_events = record_feedback_events
        # Public ``observability`` property still returns the original
        # ``SurfacingObservability | None`` so consumers (server.py) can
        # short-circuit on absence; ``_observability`` is the internal
        # recording sink and never None — the no-op stand-in lets the call
        # sites stay unconditional. Same shape inside RelevanceGate.
        self._observability_public = observability
        self._observability = observability if observability is not None else _NOOP_OBSERVABILITY
        self._auto_tuner = None
        if config.auto_tune_enabled and feedback_tracker is not None:
            from memtomem_stm.surfacing.feedback import AutoTuner

            self._auto_tuner = AutoTuner(config, feedback_tracker.store)
        self._extractor = ContextExtractor()
        self._gate = RelevanceGate(config, observability=observability)
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
        # Bound surfacing_events once at startup (#584): stm_surfacing_stats
        # reads get_stats directly and can be called before the first surface()
        # fires this session, so the opportunistic _maybe_cleanup_expired path
        # alone would leave that first read scanning an unbounded table after a
        # restart. Runs after the store is known to be open (seed block above
        # already read from it).
        if feedback_tracker is not None:
            self._run_stats_retention(feedback_tracker.store)
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
        # #349: one-shot WARNING when the LTM adapter reports
        # ``no_session`` / ``transport_error`` for the first time. The
        # ``ltm_unavailable`` skip counter is the durable signal, but
        # operators easily miss a counter — symmetric to the #348
        # prepend-on-progressive WARNING-once pattern.
        self._warned_ltm_unavailable: bool = False
        # Per-upstream-tool score-scale tripwire (#672). Only real LTM
        # searches update this state; cache hits have no raw scores and are
        # deliberately neutral. Entries disappear on recovery/reset, so the
        # map is naturally bounded by the configured upstream tool set.
        self._score_scale_streaks: dict[tuple[str, str], _ScoreScaleStreak] = {}

    @property
    def observability(self) -> SurfacingObservability | None:
        """Return the observability counter, if wired. Read by
        ``server.py::stm_surfacing_stats`` to extend its output with
        per-tool skip/outcome/cache aggregates. Always the value the
        engine was constructed with — the internal no-op stand-in used
        for unconditional recording is not exposed here."""
        return self._observability_public

    async def propose_candidate(
        self,
        content: str,
        *,
        source: str,
        source_ref: str,
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Delegate a pending candidate to core without durable-write fallback."""
        capabilities = getattr(self._mcp_adapter, "capabilities", None)
        if not isinstance(capabilities, LtmCapabilities):
            return None
        propose = getattr(self._mcp_adapter, "candidate_propose", None)
        if not callable(propose):
            return None
        return await propose(
            content,
            source=source,
            source_ref=source_ref,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )

    def clear_cache(self) -> int:
        """Flush the in-memory surfacing result cache (operator flush, reached
        from ``server.py::stm_proxy_cache_clear``). Returns the number of cached
        query entries dropped.

        Scoped to the result cache ONLY. The cross-session ``_surfaced_ids``
        dedup set and the ``_invalidated_ids`` feedback set are left intact:
        clearing the former would resurface memories already shown this session,
        and the latter is a strict subset of surfacings that is inert once the
        result cache is empty (it only filters cache hits)."""
        return self._cache.clear()

    def _persist_fault(self, server: str, tool: str, kind: str) -> None:
        """Best-effort durable counterpart to the in-memory fault counters.

        The ``SurfacingObservability`` counters answer "why isn't surfacing
        firing" only for the *current* process; the daemon idle-exits and MCP
        session servers restart, so a timeout/breaker loop that spans hours is
        invisible to ``mms stats`` (which reads on-disk stores only). Persist
        the degraded-dependency subset — never healthy skips — through the
        feedback store's day-aggregated upsert. Recording sits on failure
        branches already returning the original response, so any store error
        degrades to a missing counter, never a raised exception.
        """
        if self._feedback_tracker is None:
            return
        try:
            self._feedback_tracker.record_fault(server, tool, kind)
        except Exception:
            logger.debug("Failed to persist surfacing fault counter", exc_info=True)

    def _persist_diagnostic(self, server: str, tool: str, kind: str) -> None:
        """Best-effort durable counter for advisory pipeline diagnostics."""
        if self._feedback_tracker is None:
            return
        try:
            self._feedback_tracker.record_diagnostic(server, tool, kind)
        except Exception:
            logger.debug("Failed to persist surfacing diagnostic counter", exc_info=True)

    def _reset_score_scale_streak(self, server: str, tool: str) -> None:
        self._score_scale_streaks.pop((server, tool), None)

    def _observe_score_scale(
        self,
        server: str,
        tool: str,
        results: list[Any],
        min_score: float,
    ) -> None:
        """Warn once per episode when healthy search scores stay below floor.

        Empty results and a candidate at/above the active threshold reset the
        episode. A threshold change also resets it so evidence gathered under
        one operator/auto-tuned policy is never combined with another.
        """
        key = (server, tool)
        if not results:
            self._score_scale_streaks.pop(key, None)
            return

        scores = [float(r.score) for r in results]
        if not all(math.isfinite(score) for score in scores):
            self._score_scale_streaks.pop(key, None)
            return
        result_max = max(scores)
        if result_max >= min_score:
            self._score_scale_streaks.pop(key, None)
            return

        previous = self._score_scale_streaks.get(key)
        if previous is None or previous.threshold != min_score:
            streak = _ScoreScaleStreak(
                threshold=min_score,
                count=1,
                observed_max=result_max,
            )
        else:
            streak = _ScoreScaleStreak(
                threshold=min_score,
                count=min(previous.count + 1, _SCORE_SCALE_WARNING_STREAK),
                observed_max=max(previous.observed_max, result_max),
            )
        self._score_scale_streaks[key] = streak

        if streak.count == _SCORE_SCALE_WARNING_STREAK and (
            previous is None or previous.count < _SCORE_SCALE_WARNING_STREAK
        ):
            logger.warning(
                "Surfacing score-scale mismatch for %s/%s: %d consecutive "
                "non-empty LTM searches had max score below active min_score "
                "(observed ceiling=%.4f, min_score=%.4f). LTM may be running "
                "single-leg/BM25-only or min_score may be intentionally high; "
                "check embedding extras and LTM logs. STM did not lower the threshold.",
                server,
                tool,
                _SCORE_SCALE_WARNING_STREAK,
                streak.observed_max,
                min_score,
            )
            self._persist_diagnostic(server, tool, "score_ceiling_below_min")

    def _persistable_query(self, query: str) -> str:
        """Return the form of ``query`` that gets written to
        ``surfacing_events.query`` (#352 part 3).

        When ``persist_query_text=True`` (default, backward-compatible)
        this is just ``query``. When ``False``, the engine substitutes a
        truncated sha256 digest with a ``sha256:`` prefix so the persisted
        value is a stable opaque ID rather than user-derived text. The
        in-memory ``RelevanceGate.record_surfacing(query)`` cooldown
        signal (and the in-flight ``query`` argument used for similarity
        comparison, dedup, formatter rendering, etc.) intentionally keeps
        the raw text — this knob only governs **what gets persisted to
        disk**, nothing about the in-process surface call.

        Secret guard: regardless of ``persist_query_text``, a query whose
        text matches a known sensitive pattern (API key, bearer token, JWT,
        ``password=``/``api_key=`` assignment, private-key header, email
        address) is hashed before persistence, so an inline credential in a
        Bash ``command`` argument or a tokenized URL never reaches disk
        verbatim on the proxy path (the hook/daemon cold paths already
        disable persistence). This deliberately scans the FULL default set
        — credentials *and* PII — unlike the LLM compression routing gate,
        which scans ``CREDENTIAL_PATTERNS`` only: an email is fine to show
        an external summarizer but not fine to persist. Only the persisted
        value is affected — the in-flight ``query`` keeps its raw text as
        described above.
        """
        if not self._config.persist_query_text:
            return self._hashed_query(query)
        if contains_sensitive_content(query):
            return self._hashed_query(query)
        return query

    @staticmethod
    def _hashed_query(query: str) -> str:
        """Return the stable ``sha256:`` + 16-hex-char digest form of *query*."""
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
        return f"{_QUERY_HASH_PREFIX}{digest}"

    def _active_min_score(self, tool: str, *, adjust_auto_tuner: bool) -> float:
        """Return the score floor currently used for surfacing decisions."""
        tool_cfg = self._config.context_tools.get(tool)
        if tool_cfg is not None and tool_cfg.min_score is not None:
            return tool_cfg.min_score
        if self._auto_tuner is not None:
            if adjust_auto_tuner:
                self._auto_tuner.maybe_adjust(tool)
            return self._auto_tuner.get_effective_min_score(tool)
        return self._config.min_score

    @property
    def injection_mode(self) -> str:
        """Formatter injection mode — ``"prepend"``, ``"append"``, or ``"section"``.

        Read by ``ProxyManager`` to decide whether progressive-path surfacing
        is safe: only ``append``/``section`` keep the
        ``PROGRESSIVE_FOOTER_TOKEN`` concat invariant that
        ``stm_proxy_read_more`` depends on.
        """
        return self._config.injection_mode

    def get_min_score_snapshot(self) -> dict:
        """Return the current min_score state for observability.

        Read by ``server.py::stm_surfacing_stats`` to render which tools
        have been auto-tuned away from the default and which are still
        accumulating samples. ``adjusted`` only contains tools whose
        ``AutoTuner.maybe_adjust`` has fired this process; an empty dict
        with ``enabled=True`` means auto-tuning is on but no tool has
        moved off the default yet (either insufficient samples or ratio
        inside the [0.2, 0.6] no-op band).

        ``overrides`` maps tool name → pinned ``min_score`` from
        ``context_tools.<tool>.min_score``. These tools bypass the
        auto-tuner entirely (see ``_do_surface`` — tool_cfg override
        takes precedence over ``maybe_adjust``), so the formatter must
        suppress readiness labels for them or it implies the tuner
        could change a threshold the operator has explicitly pinned.
        """
        adjusted: dict[str, float] = (
            self._auto_tuner.adjustments if self._auto_tuner is not None else {}
        )
        overrides: dict[str, float] = {
            tool: tcfg.min_score
            for tool, tcfg in self._config.context_tools.items()
            if tcfg.min_score is not None
        }
        return {
            "default": self._config.min_score,
            "auto_tune_enabled": self._config.auto_tune_enabled and self._auto_tuner is not None,
            "auto_tune_min_samples": self._config.auto_tune_min_samples,
            "adjusted": adjusted,
            "overrides": overrides,
        }

    async def surface(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        response_text: str,
        *,
        trace_id: str | None = None,
        context_query: str | None = None,
        source_response_chars: int | None = None,
    ) -> str:
        """Surface relevant memories and inject into response_text.

        Returns the original response_text unmodified if:
        - surfacing is disabled
        - circuit breaker is open
        - relevance gate rejects the call
        - timeout exceeded
        """
        if not self._config.enabled:
            self._observability.record_skip(tool, "disabled")
            return response_text

        self._maybe_cleanup_expired()

        gate_chars = len(response_text) if source_response_chars is None else source_response_chars
        # An explicit caller query is an intentional retrieval request and may
        # override the automatic response-size heuristic.
        if gate_chars < self._config.min_response_chars and not context_query:
            self._observability.record_skip(tool, "response_too_short")
            return response_text

        if self._circuit_breaker.is_open:
            self._observability.record_skip(tool, "circuit_open")
            self._persist_fault(server, tool, "circuit_open")
            logger.debug("Surfacing skipped: circuit breaker open for %s/%s", server, tool)
            return response_text

        query = self._extractor.extract_query(
            server, tool, arguments, self._config, context_query=context_query
        )
        if query is None:
            self._observability.record_skip(tool, "no_query")
            logger.debug("Surfacing skipped: no query extracted for %s/%s", server, tool)
            return response_text
        if contains_sensitive_content(query):
            # Never send raw credentials/PII to a remote LTM. A stable digest
            # preserves cache/cooldown behavior without disclosing the source.
            query = self._hashed_query(query)
        if not self._gate.should_surface(server, tool, query):
            # Gate has already recorded the specific reason internally. Avoid
            # double-counting by not recording at the engine level here.
            logger.debug(
                "Surfacing skipped: gate rejected %s/%s (query=%s)",
                server,
                tool,
                query[:50],
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
            self._observability.record_outcome(tool, "error_timeout")
            self._persist_fault(server, tool, "error_timeout")
            logger.warning(
                "Surfacing timed out for %s/%s (%.1fs limit)",
                server,
                tool,
                self._config.timeout_seconds,
            )
            # A hung LTM must open the breaker like an erroring one (#579): a
            # timeout is a degraded dependency, and it also cancels the adapter
            # mid-RPC, forcing a stdio child respawn on the next call (#290/#296).
            # Without counting it, every eligible call pays the full timeout
            # indefinitely and the breaker never opens.
            self._circuit_breaker.record_failure()
            return response_text
        except _DependencyFault:
            self._circuit_breaker.record_failure()
            return response_text
        except _OperationalSkip:
            return response_text
        except Exception:
            self._observability.record_outcome(tool, "error_other")
            self._persist_fault(server, tool, "error_other")
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

        if isinstance(result, str) and result.startswith("Error"):
            return result

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
                        await self._increment_access_with_timeout(target_ids)
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

    async def handle_feedback_batch(
        self,
        surfacing_id: str,
        ratings: list[dict],
    ) -> str:
        """Record feedback for multiple memories from one surfacing event.

        Each entry is ``{"memory_id": str, "rating": str}``; unknown keys
        (e.g. a future ``note``) are ignored. Fans out to the same
        record / invalidation / boost routines as :meth:`handle_feedback`
        so the single-call and batched paths share side-effect semantics.
        ``helpful`` boosts collapse to one ``increment_access`` call
        covering every helpful memory id, keeping the per-event guard
        enforceable when single and batched calls interleave for the
        same surfacing event.
        """
        if self._feedback_tracker is None:
            return "Feedback tracking is not enabled."
        if not ratings:
            return "Error: `ratings` must contain at least one entry."

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
        helpful_ids: list[str] = []
        seen_helpful: set[str] = set()
        for i, (mid, rat) in enumerate(parsed):
            result = self._feedback_tracker.record_feedback(surfacing_id, rat, mid)
            if isinstance(result, str) and result.startswith("Error"):
                errors.append(f"ratings[{i}]: {result}")
                continue
            recorded += 1
            if rat in ("not_relevant", "already_known"):
                self._invalidate_cache_for_feedback(surfacing_id, mid)
            elif rat == "helpful" and mid not in seen_helpful:
                seen_helpful.add(mid)
                helpful_ids.append(mid)

        if helpful_ids and surfacing_id not in self._boosted_event_ids:
            # Claim the guard before the await so a concurrent helpful
            # rating for the same surfacing_id short-circuits — same shape
            # as the single-rating path.
            self._boosted_event_ids[surfacing_id] = None
            try:
                with traced(
                    "surfacing_feedback_boost",
                    metadata={
                        "surfacing_id": surfacing_id,
                        "chunk_count": len(helpful_ids),
                    },
                ):
                    await self._increment_access_with_timeout(helpful_ids)
                if len(self._boosted_event_ids) > self._boosted_event_ids_max:
                    excess = len(self._boosted_event_ids) - self._boosted_event_ids_max // 2
                    for k in list(self._boosted_event_ids)[:excess]:
                        del self._boosted_event_ids[k]
            except Exception:
                self._boosted_event_ids.pop(surfacing_id, None)
                logger.debug(
                    "Failed to boost access_count for surfacing %s",
                    surfacing_id,
                    exc_info=True,
                )

        summary = f"Feedback recorded: {recorded}/{len(parsed)} entries"
        if errors:
            summary += " — " + "; ".join(errors)
        return summary

    async def _increment_access_with_timeout(self, chunk_ids: list[str]) -> None:
        """Best-effort LTM boost for helpful feedback.

        Feedback recording must not block behind LTM startup or a stalled
        network transport. Bound the whole adapter call, including lazy
        connection setup, with the same surfacing timeout budget.
        """
        await asyncio.wait_for(
            self._mcp_adapter.increment_access(chunk_ids),
            timeout=self._config.timeout_seconds,
        )

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

    def _feedback_demoted_ids(self, memory_ids: list[str]) -> set[str]:
        """Return IDs that accumulated enough durable negative feedback.

        This is STM-side shadow demotion for memories the LTM still ranks
        above ``min_score`` after repeated ``not_relevant`` / ``already_known``
        ratings. It deliberately filters only the current candidate set and
        leaves the LTM rank untouched until core grows a symmetric demotion
        action.
        """
        if (
            self._feedback_tracker is None
            or not self._record_feedback_events
            or not self._config.feedback_demotion_enabled
            or not memory_ids
        ):
            return set()
        try:
            counts = self._feedback_tracker.store.get_negative_feedback_counts(memory_ids)
        except Exception:
            logger.debug("Failed to load negative feedback counts", exc_info=True)
            return set()
        threshold = self._config.feedback_demotion_negative_threshold
        return {mid for mid, count in counts.items() if count >= threshold}

    def _claim_surfaced_ids(self, ids: list[str]) -> None:
        """Record memory IDs as surfaced this session, FIFO-pruning to
        ``_surfaced_ids_max``. Shared by the miss path and the cache-hit path so
        a memory injected either way is deduped against later misses (evicting
        the oldest half once the cap is exceeded)."""
        for mid in ids:
            self._surfaced_ids[mid] = None
        if len(self._surfaced_ids) > self._surfaced_ids_max:
            excess = len(self._surfaced_ids) - self._surfaced_ids_max // 2
            for k in list(self._surfaced_ids)[:excess]:
                del self._surfaced_ids[k]

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
        TTL window — and re-applies the durable feedback-demotion filter,
        symmetric with the miss path: a memory that crossed the
        negative-feedback threshold after this entry was cached (e.g.
        feedback recorded by another process sharing ``stm_feedback.db``,
        which never reaches this engine's in-memory sets) must not keep
        resurfacing from a warm entry for the rest of the TTL. A
        filtered-empty result pass-throughs like the natural empty case.
        """
        # Track whether the entry started non-empty so we can distinguish a
        # ``no_results_invalidated`` outcome (had results, all rejected) from
        # ``no_results_empty_cache`` (the deliberate "no results" cache entry
        # written by ``_do_surface_miss`` when LTM returned nothing relevant).
        was_empty = not cached
        if cached and self._invalidated_ids:
            original_count = len(cached)
            cached = [
                r
                for r in cached
                if getattr(r, "pinned", False)
                or (server, tool, str(r.chunk.id)) not in self._invalidated_ids
            ]
            if len(cached) < original_count:
                logger.debug(
                    "Surfacing cache filter: %s/%s %d→%d (invalidated)",
                    server,
                    tool,
                    original_count,
                    len(cached),
                )
        # ``_feedback_demoted_ids`` gates itself on the tracker /
        # ``record_feedback_events`` / ``feedback_demotion_enabled``, so the
        # dedup-only daemon path and no-tracker engines skip the DB read.
        demoted_all = False
        if cached:
            demoted_ids = self._feedback_demoted_ids(
                [str(r.chunk.id) for r in cached if not getattr(r, "pinned", False)]
            )
            if demoted_ids:
                original_count = len(cached)
                cached = [
                    r
                    for r in cached
                    if getattr(r, "pinned", False) or str(r.chunk.id) not in demoted_ids
                ]
                demoted_all = not cached
                logger.debug(
                    "Surfacing cache filter: %s/%s %d→%d (demoted)",
                    server,
                    tool,
                    original_count,
                    len(cached),
                )
        if not cached:
            if was_empty:
                self._observability.record_skip(tool, "no_results_empty_cache")
            elif demoted_all:
                # Same label as the miss path's all-demoted branch: the
                # operator sees demotion (not invalidation) suppressing the
                # cached result.
                self._observability.record_skip(tool, "no_results_demoted")
            else:
                self._observability.record_skip(tool, "no_results_invalidated")
            logger.debug("Surfacing cache hit (empty) for %s/%s", server, tool)
            return response_text
        self._observability.record_outcome(tool, "surfaced_cache_hit")
        logger.debug("Surfacing cache hit (%d results) for %s/%s", len(cached), server, tool)
        # Reclaim the injected IDs into the session-dedup set, symmetric with
        # the miss path, so a memory re-shown from cache is deduped against
        # later misses (e.g. one FIFO-evicted from _surfaced_ids but still in a
        # cache entry). We deliberately do NOT *filter* the cached list against
        # _surfaced_ids: the miss already claimed these IDs, so filtering would
        # empty a normal repeated-query cache hit and break the
        # concurrent-identical-query contract. Cooldown refresh
        # (record_surfacing) and cross-session mark_surfaced stay miss-only by
        # design.
        # See ``_do_surface_miss``: only advertise a feedback ID we actually
        # recorded, so neither a no-tracker path nor the dedup-only daemon path
        # (``record_feedback_events=False``) prompts for an unresolvable ID.
        surfacing_id: str | None = None
        if self._feedback_tracker is not None and self._record_feedback_events:
            surfacing_id = uuid.uuid4().hex[:16]
        manifest = self._formatter.render(
            response_text,
            cached,
            query,
            surfacing_id=surfacing_id,
            score_floor=self._active_min_score(tool, adjust_auto_tuner=False),
        )
        delivered_ids = list(manifest.delivered_ids)
        delivered_set = set(delivered_ids)
        # Gate on rendered bullets, not delivered IDs: a bullet whose ID fails
        # the formatter's display gate is still delivered content and must not
        # drop the whole injection (the id-less degradation contract).
        if manifest.rendered_bullets == 0:
            return response_text
        self._claim_surfaced_ids(delivered_ids)
        if surfacing_id is not None:
            assert self._feedback_tracker is not None
            try:
                self._feedback_tracker.record_surfacing(
                    surfacing_id=surfacing_id,
                    server=server,
                    tool=tool,
                    query=self._persistable_query(query),
                    memory_ids=delivered_ids,
                    scores=[
                        r.score
                        for r in cached
                        if not getattr(r, "pinned", False) and str(r.chunk.id) in delivered_set
                    ],
                )
            except Exception:
                logger.warning("Failed to record cached surfacing event", exc_info=True)
                surfacing_id = None
                manifest = self._formatter.render(
                    response_text,
                    cached,
                    query,
                    score_floor=self._active_min_score(tool, adjust_auto_tuner=False),
                )
        return manifest.text

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
            self._observability.record_cache("hit")
            return self._render_cached(cached, response_text, query, server, tool)

        lock = self._key_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            try:
                # Double-check inside the lock: a coroutine that held the
                # lock ahead of us may have populated the cache already.
                cached = self._cache.get(cache_key)
                if cached is not None:
                    self._observability.record_cache("hit")
                    return self._render_cached(cached, response_text, query, server, tool)
                self._observability.record_cache("miss")
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
        # min_score precedence: tool_cfg override > auto-tune > global default.
        # When the operator pins per-tool min_score, skip maybe_adjust so the
        # tuner doesn't learn a value that will never be applied.
        tool_cfg = self._config.context_tools.get(tool)
        min_score = self._active_min_score(tool, adjust_auto_tuner=True)
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

        # Ask a capable core to compose Pinned Context + retrieval under one
        # budget. Capability absence alone falls back to legacy mem_search;
        # a declared compose surface that fails is a dependency fault and is
        # deliberately not hidden by a second legacy request.
        ctx_win = self._config.context_window_size or None
        search_kwargs: dict[str, Any] = {}
        if ctx_win:
            search_kwargs["context_window"] = ctx_win
        compose_failed = False
        try:
            capabilities = getattr(self._mcp_adapter, "capabilities", None)
            compose = getattr(self._mcp_adapter, "context_compose", None)
            bundle = None
            if isinstance(capabilities, LtmCapabilities) and callable(compose):
                try:
                    bundle = await compose(
                        query,
                        max_chars=self._config.effective_max_injection_chars(),
                        top_k=max_results * 2,
                        trace_id=trace_id,
                    )
                except (RuntimeError, ValueError):
                    # A declared compose surface is authoritative: classify
                    # its failure like a legacy upstream call failure, but do
                    # not hide it with a second search request.
                    logger.debug("Core context composition failed", exc_info=True)
                    compose_failed = True
            if bundle is not None:
                results = [*bundle.pinned, *bundle.retrieved]
                hints = [*bundle.warnings]
                if bundle.omitted_block_ids:
                    hints.append(
                        "Pinned Context omitted by budget: "
                        + ", ".join(bundle.omitted_block_ids[:5])
                    )
                outcome: SearchOutcome = "ok" if results else "empty_results"
            elif compose_failed:
                results, hints, outcome = [], [], "call_error"
            else:
                results, hints, outcome = await self._mcp_adapter.search(
                    query=query,
                    top_k=max_results * 2,
                    namespace=namespace,
                    trace_id=trace_id,
                    **search_kwargs,
                )
        except asyncio.CancelledError:
            self._reset_score_scale_streak(server, tool)
            raise
        except Exception:
            self._reset_score_scale_streak(server, tool)
            raise

        # #295: branch on the adapter outcome before doing any further work
        # so the operator-facing skip label distinguishes "LTM unavailable"
        # (``no_session``/``transport_error``) from "LTM call raised"
        # (``call_error``) from "core returned no text" (``empty_content``).
        # ``ok`` and ``empty_results`` both fall through to the existing
        # min_score / dedup / ``no_results_score`` path so that operators
        # tuning min_score keep seeing the same signal for the genuine
        # empty-namespace case.
        if outcome in ("daemon_starting", "daemon_busy"):
            self._reset_score_scale_streak(server, tool)
            self._observability.record_skip(tool, outcome)
            raise _OperationalSkip(outcome)
        if outcome in ("no_session", "transport_error"):
            self._reset_score_scale_streak(server, tool)
            if not self._warned_ltm_unavailable:
                if self._config.use_daemon:
                    ltm_transport = "shared daemon"
                    ltm_target = "mms daemon status"
                elif self._config.ltm_mcp_transport == "stdio":
                    ltm_transport = self._config.ltm_mcp_transport
                    ltm_target = self._config.ltm_mcp_command
                else:
                    ltm_transport = self._config.ltm_mcp_transport
                    # Display-only: a network URL may carry basic-auth
                    # credentials that must not reach the WARNING line.
                    ltm_target = redact_url_userinfo(self._config.ltm_mcp_url)
                logger.warning(
                    "Surfacing skipped: LTM MCP %s target %r is not reachable "
                    "(outcome=%s). Subsequent skips counted as 'ltm_unavailable' "
                    "in stm_surfacing_stats. Run `mms health` to diagnose or "
                    "set `surfacing.enabled=false` to silence.",
                    ltm_transport,
                    ltm_target,
                    outcome,
                )
                self._warned_ltm_unavailable = True
            self._observability.record_skip(tool, "ltm_unavailable")
            self._persist_fault(server, tool, "ltm_unavailable")
            raise _DependencyFault(outcome)
        if outcome in ("call_error", "upstream_error"):
            self._reset_score_scale_streak(server, tool)
            self._observability.record_skip(tool, "ltm_call_failed")
            self._persist_fault(server, tool, "ltm_call_failed")
            raise _DependencyFault(outcome)
        if outcome in ("empty_content", "parse_error"):
            self._reset_score_scale_streak(server, tool)
            self._observability.record_skip(tool, "ltm_parse_empty")
            self._persist_fault(server, tool, "ltm_parse_empty")
            raise _DependencyFault(outcome)

        retrieved_results = [r for r in results if not getattr(r, "pinned", False)]
        self._observe_score_scale(server, tool, retrieved_results, min_score)

        # Parent trust-UX hints (parent PR #231): log at INFO even when results
        # are empty or get filtered out below, since an operator may want to
        # see "3 results found before filtering" notices regardless of whether
        # SURFACE ultimately injects anything. Hints are NOT forwarded to the
        # downstream agent — that is a prepend-body policy change, out of
        # scope here. See B3 plan § "forward hints to downstream".
        if hints:
            logger.info("LTM hints for %s/%s: %s", server, tool, "; ".join(hints))
            if self._token_tracker is not None:
                try:
                    self._token_tracker.record_hints(hints)
                except Exception:
                    logger.debug("token_tracker.record_hints failed", exc_info=True)

        # Filter by score, then locally demote memories with repeated durable
        # negative feedback, then exclude already-surfaced memories in this
        # session. Demotion sits before cache write so a rejected memory does
        # not keep reappearing from a cached query after process restart;
        # ``_render_cached`` re-applies it on the hit path for entries whose
        # memories cross the threshold mid-TTL (e.g. via another process).
        pinned_results = [r for r in results if getattr(r, "pinned", False)]
        scored = [r for r in retrieved_results if r.score >= min_score]
        demoted_ids = self._feedback_demoted_ids([str(r.chunk.id) for r in scored])
        if demoted_ids:
            logger.debug(
                "Surfacing demotion filter: %s/%s skipped %d memory IDs",
                server,
                tool,
                len(demoted_ids),
            )
        # ``seen`` dedups WITHIN this single result set. ``_surfaced_ids`` only
        # excludes IDs claimed by *prior* surfacings — it is populated by
        # ``_claim_surfaced_ids`` AFTER this loop, so it cannot catch a duplicate
        # appearing twice in the same ``scored`` list. Under
        # ``result_format='compact'`` a chunk's id is ``sha256(content)[:16]``
        # (``mcp_client``), so two results with byte-identical content collide on
        # one id and would otherwise render as two identical bullets to the agent
        # (they also share a ``memory_id``, so feedback already treats them as one).
        relevant = list(pinned_results)
        # Treat core output as untrusted: a retrieved item reusing a pinned
        # block ID must not render the same memory twice.
        seen: set[str] = {str(r.chunk.id) for r in pinned_results}
        retrieved_count = 0
        for r in scored:
            mid = str(r.chunk.id)
            if mid in demoted_ids or mid in self._surfaced_ids or mid in seen:
                continue
            relevant.append(r)
            seen.add(mid)
            retrieved_count += 1
            if retrieved_count >= max_results:
                break

        # Cache result (even empty, to avoid repeated searches)
        self._cache.set(cache_key, relevant)

        if not relevant:
            # Distinguish "score filter killed everything" from "score filter
            # passed but session-dedup killed everything" so an operator can
            # tell whether to lower min_score (former) or whether the dedup
            # is over-aggressive on long sessions (latter).
            if demoted_ids and all(str(r.chunk.id) in demoted_ids for r in scored):
                self._observability.record_skip(tool, "no_results_demoted")
            elif scored:
                self._observability.record_skip(tool, "no_results_dedup")
            else:
                self._observability.record_skip(tool, "no_results_score")
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
        new_ids = [str(r.chunk.id) for r in relevant if not getattr(r, "pinned", False)]
        self._claim_surfaced_ids(new_ids)

        logger.info("Surfacing %d memories for %s/%s", len(relevant), server, tool)
        logger.debug(
            "Surfacing %d memories for %s/%s (query=%s)", len(relevant), server, tool, query[:50]
        )

        # Session context (working memory): when enabled, fetch scratchpad
        # entries via the MCP adapter and inject alongside LTM hits. Failures
        # are silent — surfacing must still deliver the LTM hits even if
        # working memory is unavailable.
        scratch_items: list[dict] | None = None
        if self._config.include_session_context:
            try:
                scratch_items = await asyncio.wait_for(
                    self._mcp_adapter.scratch_list(trace_id=trace_id),
                    timeout=max(0.05, min(0.5, self._config.timeout_seconds / 3)),
                )
            except Exception:
                logger.debug("Failed to fetch session scratch items", exc_info=True)
                scratch_items = None

        # Generate surfacing ID and record event. Only mint an ID when a
        # tracker is attached *and* feedback-event recording is on — otherwise
        # the formatter would advertise a ``stm_surfacing_feedback(...)`` prompt
        # for an event no in-band channel can resolve. Three cases skip it: the
        # no-tracker ``mms hook`` / feedback-disabled server paths, and the hook
        # daemon's dedup-only wiring (``record_feedback_events=False``) which
        # keeps a tracker for ``seen_memories`` dedup but persists no query.
        surfacing_id: str | None = None
        if self._feedback_tracker is not None and self._record_feedback_events:
            surfacing_id = uuid.uuid4().hex[:16]

        # Inject memories into response
        manifest = self._formatter.render(
            response_text,
            relevant,
            query,
            surfacing_id=surfacing_id,
            scratch_items=scratch_items,
            score_floor=min_score,
        )
        delivered_ids = list(manifest.delivered_ids)
        delivered_set = set(delivered_ids)
        # Reservations close the concurrent window, but only rendered IDs are
        # committed to session/cross-session dedup.
        for mid in new_ids:
            if mid not in delivered_set:
                self._surfaced_ids.pop(mid, None)
        # Gate on rendered bullets, not delivered IDs: a bullet whose ID fails
        # the formatter's display gate is still delivered content and must not
        # drop the whole injection (the id-less degradation contract).
        if manifest.rendered_bullets == 0:
            return response_text

        self._gate.record_surfacing(query)
        if surfacing_id is not None:
            assert self._feedback_tracker is not None
            delivered_results = [
                r
                for r in relevant
                if not getattr(r, "pinned", False) and str(r.chunk.id) in delivered_set
            ]
            try:
                self._feedback_tracker.record_surfacing(
                    surfacing_id=surfacing_id,
                    server=server,
                    tool=tool,
                    query=self._persistable_query(query),
                    memory_ids=delivered_ids,
                    scores=[r.score for r in delivered_results],
                )
            except Exception:
                logger.warning("Failed to record surfacing event", exc_info=True)
                # The rendered prompt references an unresolvable event ID. Re-render
                # without it instead of handing the agent a dead feedback handle.
                surfacing_id = None
                manifest = self._formatter.render(
                    response_text,
                    relevant,
                    query,
                    scratch_items=scratch_items,
                    score_floor=min_score,
                )
        if self._feedback_tracker is not None:
            try:
                self._feedback_tracker.store.mark_surfaced(delivered_ids)
            except Exception:
                logger.warning("Failed to persist seen memory IDs", exc_info=True)

        self._observability.record_outcome(tool, "surfaced_cache_miss")

        # Fire webhook (fire-and-forget)
        if self._webhook_manager and self._config.fire_webhook:
            task = asyncio.create_task(
                self._webhook_manager.fire(
                    "surface",
                    {
                        "server": server,
                        "tool": tool,
                        "query": query,
                        "memory_ids": delivered_ids,
                        "scores": [r.score for r in relevant if str(r.chunk.id) in delivered_set],
                        "surfacing_id": surfacing_id,
                    },
                )
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._on_webhook_done)

        return manifest.text

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

    def _run_stats_retention(self, store: Any) -> None:
        """Delete ``surfacing_events`` rows past the stats-retention window so
        ``get_stats`` cannot full-scan an unbounded history (#584).

        Called both opportunistically from ``surface()`` (via
        ``_maybe_cleanup_expired``) and once at startup: ``stm_surfacing_stats``
        reads ``get_stats`` directly and an operator can call it before the
        first ``surface()`` fires after a restart, so relying on the surface()
        path alone would leave that first stats read scanning the whole table.
        ``stats_retention_days <= 0`` disables it.
        """
        stats_retention_days = self._config.stats_retention_days
        if stats_retention_days <= 0:
            return
        try:
            deleted = store.delete_events_older_than(stats_retention_days * 86400.0)
            if deleted:
                logger.info(
                    "Deleted %d surfacing_events rows older than %d days (#584)",
                    deleted,
                    stats_retention_days,
                )
        except Exception:
            logger.warning("Failed to delete expired surfacing_events", exc_info=True)
        # Fault counters share the stats-retention knob: they are read by the
        # same stats surfaces and one day-aggregated row per (day, server,
        # tool, kind) never grows fast enough to deserve its own setting.
        try:
            deleted = store.delete_faults_older_than(stats_retention_days * 86400.0)
            if deleted:
                logger.info(
                    "Deleted %d surfacing_faults rows older than %d days",
                    deleted,
                    stats_retention_days,
                )
        except Exception:
            logger.warning("Failed to delete expired surfacing_faults", exc_info=True)

    def _maybe_cleanup_expired(self) -> None:
        """Run periodic store maintenance at most once per cleanup interval.

        Called opportunistically from surface() — no separate timer thread
        needed. Each sub-task is synchronous (SQLite DELETE / UPDATE) and
        fast enough to run inline. Sub-tasks are independent: an operator
        can disable cross-session dedup (``dedup_ttl_seconds=0``) while
        keeping query retention on, and vice versa. The interval check
        is shared so the loop fires once and visits both.
        """
        if self._feedback_tracker is None:
            return
        dedup_ttl = self._config.dedup_ttl_seconds
        retention_days = self._config.query_retention_days
        stats_retention_days = self._config.stats_retention_days
        if dedup_ttl <= 0 and retention_days <= 0 and stats_retention_days <= 0:
            return
        now = time.monotonic()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        store = self._feedback_tracker.store
        if dedup_ttl > 0:
            try:
                deleted = store.cleanup_expired(dedup_ttl)
                if deleted:
                    logger.info("Cleaned up %d expired seen_memories entries", deleted)
            except Exception:
                logger.warning("Failed to clean up expired seen_memories", exc_info=True)
        # Delete aged-out event rows BEFORE nulling queries: a row past the
        # stats window is gone entirely, so nulling its query first would be
        # wasted work. Bounds the table get_stats scans (#584).
        self._run_stats_retention(store)
        if retention_days > 0:
            try:
                nulled = store.cleanup_expired_queries(retention_days * 86400.0)
                if nulled:
                    logger.info(
                        "Nulled query column on %d surfacing_events rows older than %d days (#352)",
                        nulled,
                        retention_days,
                    )
            except Exception:
                logger.warning("Failed to clean up expired surfacing queries", exc_info=True)
