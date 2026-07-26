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
from memtomem_stm.surfacing.mcp_client import (
    KNOWN_SCORE_SCALES,
    LtmCapabilities,
    LtmTransportError,
    SearchOutcome,
)
from memtomem_stm.surfacing.observability import _NOOP_OBSERVABILITY, SurfacingObservability
from memtomem_stm.surfacing.relevance import RateClaim, RelevanceGate
from memtomem_stm.utils.circuit_breaker import CircuitBreaker
from memtomem_stm.utils.redact import redact_exception_text, redact_url_userinfo

logger = logging.getLogger(__name__)


def _fifo_prune(d: dict[Any, None], cap: int) -> None:
    """Evict oldest (first-inserted) entries once *cap* is exceeded.

    Prunes down to roughly half the cap so steady-state inserts don't
    re-trip the prune on every call. Shared by the engine's bounded
    insertion-ordered guard maps (boost dedup, cache invalidation,
    surfaced-id dedup) so the eviction policy can't drift per site.
    """
    if len(d) <= cap:
        return
    excess = len(d) - cap // 2
    for k in list(d)[:excess]:
        del d[k]


_QUERY_HASH_PREFIX = "sha256:"
"""Marker prefixed to the truncated sha256 digest written to
``surfacing_events.query`` when ``SurfacingConfig.persist_query_text`` is
``False`` (#352 part 3). Lets the stats formatter recognize the
hashed-form row without re-reading config and lets ad-hoc DB inspection
tell user-derived text apart from a stable opaque ID. The full stored
value is ``"sha256:" + 16-hex-char digest`` → 23 chars total."""

_MAX_ABANDONED_OPS = 4
"""How many cancelled LTM operations may still be unwinding before this engine
stops starting new ones.

``_run_within`` abandons an operation rather than waiting out an unwind it
cannot bound, so an LTM that never lets go leaves one behind per attempt, each
still holding whatever it held (its session, the per-key lock). The circuit
breaker throttles but does not stop that: every reset lets another probe
through. Rather than evict the references — the point of keeping them is that
the operation is still running — refuse the *next* attempt while this many are
stuck, which is also the honest reading of a dependency that will not let go.
Small on purpose: past one or two, the LTM is not answering anyway.

Counts only *abandoned* operations, not every one in flight. Reserving on every
attempt would give a hard instantaneous cap, but nothing at admission time
knows which attempts will get stuck, so it would equally cap healthy
concurrency (the daemon serializes surfacing; ``ProxyManager`` does not) and
report ordinary saturation as this dependency fault. The cost is that a burst
already in flight when the LTM wedges can overshoot — each of those read the
count before any of them had timed out. That is bounded, since everything after
they land is refused, and it errs toward letting a healthy LTM work.
"""

_ABANDONED_DRAIN_SECONDS = 1.0
"""How long :meth:`SurfacingEngine.stop` waits on LTM operations abandoned at
timeout before giving up on them.

Not a tuning knob: an abandoned operation is already cancelled and its result
discarded, so this only buys a tidy unwind (the adapter's session marking) on
the way out. It is bounded because that unwind is not — the reason
``_run_within`` stopped waiting on it in the first place — and the daemon's
teardown calls this before the adapter's own bounded stop.
"""

_UNSET: Any = object()
"""Sentinel distinguishing "caller did not pass a derived score scale" from a
genuine ``None`` (core reported no scale). Lets ``_observe_score_scale`` accept
the surface hot path's already-derived pair while test/hook callers omit it."""

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
        # Feedback-loop enablement, not event persistence. When a tracker is
        # attached, telemetry writes (``surfacing_events`` rows, ``seen_memories``
        # dedup, fault counters) gate on tracker presence alone — so paths like
        # the hook daemon stay visible in ``stm_surfacing_stats`` / ``mms stats``.
        # This flag gates only the feedback loop on top of that telemetry: the
        # advertised ``stm_surfacing_feedback(surfacing_id=...)`` rating prompt
        # and the durable-demotion read. The hook daemon path sets it ``False``
        # because it has no in-band channel for the model to return a rating
        # (the prompt would be unresolvable). Raw-query privacy is
        # ``persist_query_text``'s job (``_persistable_query`` digests the
        # query), not this flag's.
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
        # LTM operations abandoned at timeout/cancellation, kept apart from
        # ``_background_tasks`` because they are the one thing here whose
        # unwind is *not* known to be bounded — that is why ``_run_within``
        # stops waiting on them — so ``stop()`` must not block on them the way
        # it does on webhooks.
        self._abandoned_ops: set[asyncio.Task] = set()
        # Latch for the ``ltm_draining`` warning: one per draining episode,
        # re-armed by the next admission. A refusal records neither breaker
        # success nor failure, so once the breaker's reset window elapses
        # every eligible call reaches admission and is refused — warning on
        # each would flood the log at call rate for as long as the LTM stays
        # wedged. The skip counter and fault row stay per-call: they are the
        # count.
        self._draining_warning_latched = False

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
        # One-shot WARNING when a declared compose surface raises. The
        # ``ltm_call_failed`` counter already records it, but a compose
        # contract break (e.g. core renamed a top-level key) is silent to an
        # operator who is not reading counters — same rationale as #349.
        self._warned_compose_failed: bool = False
        # Per-upstream-tool score-scale tripwire (#672). Only real LTM
        # searches update this state; cache hits have no raw scores and are
        # deliberately neutral. Entries disappear on recovery/reset, so the
        # map is naturally bounded by the configured upstream tool set.
        self._score_scale_streaks: dict[tuple[str, str], _ScoreScaleStreak] = {}
        # A healthy observation closes at most one persisted episode per key.
        # Re-arm only after a subsequent below-threshold observation so the
        # healthy hot path does not UPDATE+commit on every search.
        self._score_scale_recovery_persisted: set[tuple[str, str]] = set()
        # Keys with an open ``score_scale_mismatch`` episode (#1781): the core
        # NAMED a non-RRF scale while the ceiling sat below min_score, so the
        # diagnostic fired without streak evidence. Fires once per episode;
        # cleared unconditionally on a healthy observation so a later mismatch
        # can warn even when persistence is unavailable. Deliberately NOT
        # cleared on empty results — alternating empty/below-threshold searches
        # must not re-fire the WARNING.
        self._score_scale_mismatch_active: set[tuple[str, str]] = set()
        # Keys whose definitive-tier episode became healthy but whose durable
        # recovery UPDATE has not succeeded yet (#729). Kept separate from the
        # warning latch above so logging re-arms immediately while transient DB
        # failures retry on later healthy observations.
        self._score_scale_mismatch_recovery_pending: set[tuple[str, str]] = set()
        # Last core-reported score scale / reranker model ID (#1781), fed to
        # ``get_min_score_snapshot`` for stm_surfacing_stats. ``None`` until a
        # capable core names one this process; kept at the last REPORTED value
        # across unstamped paths (compose bundles, compact format) so a mixed
        # compose/search flow doesn't flip the stats line to "unknown".
        self._last_score_scale: str | None = None
        self._last_reranker: str | None = None
        # Keys whose open score-scale episodes were closed by the scale gate
        # (scale_gated_min_score): the gate suspends the filter, so a
        # pre-existing ``score_scale_mismatch``/``score_ceiling_below_min``
        # episode describes a problem that no longer exists. Closed once per
        # key per process, guarded on persistence success so a trackerless
        # engine retries (a cheap no-op) instead of never writing.
        self._scale_gate_recovery_persisted: set[tuple[str, str]] = set()
        # Warn-once INFO latch for the first suspended batch this process.
        self._scale_gate_logged: bool = False

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
        # A newly opened episode re-arms the scale-gate recovery latch: the
        # next suspended batch must re-attempt the recovery UPDATE that closes
        # THIS episode, not skip it because an EARLIER episode was already
        # recovered. Mirrors ``_score_scale_recovery_persisted``'s discard on
        # every below-threshold observation. Fires before the tracker guard so
        # the in-memory latch stays correct regardless of persistence backend
        # (a trackerless engine never arms the latch, so this is a no-op there).
        self._scale_gate_recovery_persisted.discard((server, tool))
        if self._feedback_tracker is None:
            return
        try:
            self._feedback_tracker.record_diagnostic(server, tool, kind)
        except Exception:
            logger.debug("Failed to persist surfacing diagnostic counter", exc_info=True)

    def _persist_diagnostic_recovery(self, server: str, tool: str, kind: str) -> bool:
        if self._feedback_tracker is None:
            return False
        try:
            self._feedback_tracker.record_diagnostic_recovery(server, tool, kind)
            return True
        except Exception:
            logger.debug("Failed to persist surfacing diagnostic recovery", exc_info=True)
            return False

    def _reset_score_scale_streak(self, server: str, tool: str) -> None:
        self._score_scale_streaks.pop((server, tool), None)

    @staticmethod
    def _result_score_scale(results: list[Any]) -> tuple[str | None, str | None]:
        """Return the core-reported ``(score_scale, reranker)`` stamped on *results*.

        Reads the first non-pinned result via ``getattr`` — engine tests use
        lightweight result stubs (same precedent as the ``pinned`` reads).
        Both the structured ``mem_search`` path and a compose schema-4 core
        (#1796) stamp retrieved results; compact parses, pinned compose
        blocks, and pre-#1781 cores stamp nothing, so those paths structurally
        yield ``(None, None)``. Core labels one scale per response; a
        hypothetical mixed payload reads as its first retrieved element.
        """
        for r in results:
            if getattr(r, "pinned", False):
                continue
            scale = getattr(r, "score_scale", None)
            reranker = getattr(r, "reranker", None)
            return (
                scale if isinstance(scale, str) and scale else None,
                reranker if isinstance(reranker, str) and reranker else None,
            )
        return None, None

    async def _run_within(self, coro: Any, timeout: float) -> Any:
        """Run *coro* under *timeout*, turning an abort *this* call's timer
        started into :class:`asyncio.TimeoutError` (#720).

        (A :class:`asyncio.TimeoutError` raised by *coro* itself also reaches
        the caller, and :meth:`surface` books it the same way. That predates
        this and is not what the machinery below is about.)

        Telling that apart from a cancellation of the call itself is the whole
        job here, because only the timeout books the fault, the log, and the
        breaker failure (#579) — and charging a *cancelled* call would open the
        breaker on a healthy LTM. It cannot be decided after the fact: neither
        elapsed time, nor the caller's deadline, nor a timeout scope's own
        ``expired()`` distinguishes "my timer fired first" from "my timer also
        fired, later, while something else was cancelling me". So the flag is
        set inside the timer callback itself. The loop runs due callbacks in
        scheduled order, and this timer is always scheduled ahead of a caller's
        backstop, so it cannot be preempted even when a stall makes both come
        due on the same iteration — which is exactly how the backstop used to
        cancel :meth:`surface` from outside and skip the bookkeeping entirely.

        The ``shield`` is what keeps that prompt: cancelling it wakes us
        immediately instead of after the cancelled LTM adapter has finished
        unwinding, which is unbounded — a stdio child can be slow to give up.
        The abandoned operation is cancelled but not awaited. The MCP adapter
        already expects a caller to leave mid-RPC — it shields its own owner
        request precisely so the op can outlive the caller (#664) — and marks
        the session for lazy reconnect while unwinding (#290/#296). Parking the
        task keeps it from being garbage-collected mid-unwind and lets
        :meth:`stop` drain what it can at shutdown.
        """
        op = asyncio.ensure_future(coro)
        shielded = asyncio.shield(op)
        timed_out = False

        def _fire() -> None:
            nonlocal timed_out
            # ``op`` first: a shield resolves its wrapper from a queued
            # callback, so there is one loop batch where the operation has
            # finished but ``shielded`` has not caught up. Firing on the
            # wrapper alone would charge an LTM that answered inside its
            # window.
            if op.done() or shielded.done():
                return
            timed_out = True
            # Only the wrapper: ``op`` keeps unwinding on its own time.
            shielded.cancel()

        handle = asyncio.get_running_loop().call_later(timeout, _fire)
        try:
            return await shielded
        except asyncio.CancelledError:
            self._abandon(op)
            if timed_out:
                raise asyncio.TimeoutError from None
            raise
        except BaseException:
            self._abandon(op)
            raise
        finally:
            handle.cancel()

    def _abandon(self, op: asyncio.Future) -> None:
        """Cancel an operation we are no longer waiting on and park it so it is
        not garbage-collected mid-unwind."""
        op.cancel()
        if isinstance(op, asyncio.Task) and not op.done():
            self._abandoned_ops.add(op)
            op.add_done_callback(self._on_abandoned_op_done)

    def _on_abandoned_op_done(self, task: asyncio.Task) -> None:
        """Retire an operation abandoned at timeout/cancellation, consuming
        any exception it raised on the way out so asyncio does not report it
        as never retrieved."""
        self._abandoned_ops.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug("Abandoned surfacing operation failed while unwinding: %s", exc)

    def _effective_timeout(self, deadline_monotonic: float | None) -> float:
        """This call's LTM window: the configured ceiling, lowered to what is
        left of a deadline-bounded caller's absolute deadline.

        Reading the clock *here* — after the gate, query extraction, and
        privacy scan — makes the engine's own pre-timeout work debit its own
        window instead of the caller's response margin (#720); a relative
        budget captured before that work silently spent the margin. The
        deadline only ever shrinks the window — an operator's
        ``timeout_seconds`` stays the upper bound. ``None``, non-finite, or
        non-positive values (a ``time.monotonic()`` reading is always
        positive, so those are caller bugs, not elapsed time) are ignored
        rather than turned into an instant timeout: a bad value must not
        silently open the breaker on a healthy LTM. A *plausible* deadline
        that pre-work has already consumed returns a non-positive remainder —
        real time did pass, so :meth:`surface` books the timeout without
        starting an LTM round trip it would have to cancel mid-RPC.
        """
        ceiling = self._config.timeout_seconds
        if (
            deadline_monotonic is None
            or not math.isfinite(deadline_monotonic)
            or deadline_monotonic <= 0
        ):
            return ceiling
        return min(ceiling, deadline_monotonic - time.monotonic())

    def _observe_score_scale(
        self,
        server: str,
        tool: str,
        results: list[Any],
        min_score: float,
        scale: str | None | object = _UNSET,
        reranker: str | None = None,
        *,
        filter_suspended: bool = False,
    ) -> None:
        """Warn once per episode when healthy search scores stay below floor.

        ``scale``/``reranker`` may be passed as the caller's already-derived
        :meth:`_result_score_scale` pair for *results*, so the surface hot
        path derives it once; omitted (``_UNSET``) they are derived here.

        Empty results and a candidate at/above the active threshold reset the
        episode. A threshold change also resets it so evidence gathered under
        one operator/auto-tuned policy is never combined with another.

        Two evidence tiers share this tripwire (#1781): when the core NAMES a
        non-RRF scale on the results, a below-threshold ceiling is a definitive
        calibration mismatch — ``min_score`` is calibrated against RRF — and
        the ``score_scale_mismatch`` diagnostic fires on first observation.
        Both the structured ``mem_search`` path and a compose schema-4 core
        (#1796) report the scale, so the definitive tier now covers the
        compose path too. Without a reported scale (compact format, pre-#1781
        cores, compose on a pre-#1796 core) the streak-of-
        ``_SCORE_SCALE_WARNING_STREAK`` heuristic and its
        ``score_ceiling_below_min`` diagnostic behave exactly as before.

        ``filter_suspended=True`` means the scale gate did not apply
        ``min_score`` to this batch (:meth:`_scale_gate_suspends`): "below the
        threshold" carries no evidence when the threshold isn't enforced, and
        the ``max >= min_score`` healthy comparison is meaningless on e.g. a
        logit scale — so the batch resets the streak, closes any open episode
        (the gate itself is the healthy state), and returns. The definitive
        tier therefore fires exactly when the filter actually applies to a
        core-named non-RRF scale: gate disabled, or a per-tool pin present.
        """
        if scale is _UNSET:
            scale, reranker = self._result_score_scale(results)

        key = (server, tool)
        if not results:
            self._score_scale_streaks.pop(key, None)
            return

        if filter_suspended:
            self._score_scale_streaks.pop(key, None)
            if key in self._score_scale_mismatch_active:
                self._score_scale_mismatch_recovery_pending.add(key)
            self._score_scale_mismatch_active.discard(key)
            if key not in self._scale_gate_recovery_persisted:
                closed_mismatch = self._persist_diagnostic_recovery(
                    server, tool, "score_scale_mismatch"
                )
                closed_ceiling = self._persist_diagnostic_recovery(
                    server, tool, "score_ceiling_below_min"
                )
                # ``record_diagnostic_recovery`` is a WHERE-guarded UPDATE, so
                # writing blind is safe when no episode exists; this clears a
                # stale pre-gate episode so ``mms doctor`` recovers on the
                # first suspended batch instead of FAILing for the full
                # 7-day window on a setup the gate just fixed.
                if closed_mismatch:
                    self._score_scale_mismatch_recovery_pending.discard(key)
                if closed_mismatch and closed_ceiling:
                    self._scale_gate_recovery_persisted.add(key)
            return

        scores = [float(r.score) for r in results]
        if not all(math.isfinite(score) for score in scores):
            self._score_scale_streaks.pop(key, None)
            return
        result_max = max(scores)
        if result_max >= min_score:
            self._score_scale_streaks.pop(key, None)
            if (
                key not in self._score_scale_recovery_persisted
                and self._persist_diagnostic_recovery(server, tool, "score_ceiling_below_min")
            ):
                self._score_scale_recovery_persisted.add(key)
            # Re-arm the definitive-tier log UNCONDITIONALLY on a healthy
            # observation. Durable recovery is tracked separately so a DB
            # failure retries without suppressing a later genuine warning.
            if key in self._score_scale_mismatch_active:
                self._score_scale_mismatch_recovery_pending.add(key)
                self._score_scale_mismatch_active.discard(key)
            if (
                key in self._score_scale_mismatch_recovery_pending
                and self._persist_diagnostic_recovery(server, tool, "score_scale_mismatch")
            ):
                self._score_scale_mismatch_recovery_pending.discard(key)
            return

        self._score_scale_recovery_persisted.discard(key)

        if scale is not None and scale in KNOWN_SCORE_SCALES and scale != "rrf":
            # Definitive tier: no streak needed — the core itself says the
            # scores are not on the scale min_score was calibrated against.
            # Unrecognized labels stay on the heuristic tier: a renamed core
            # label must not fire a diagnostic whose wording it may not match.
            if key not in self._score_scale_mismatch_active:
                # Fix guidance names why the filter still applied: with the
                # scale gate shipped, this tier only fires when a per-tool
                # pin keeps the filter active or the gate is disabled — and
                # a "rerank" label under the default rerank=false also means
                # the per-call bypass isn't taking (surfacing.rerank forcing
                # it, or the core not honoring the param). Deliberately NOT
                # "pin context_tools.<tool>.min_score to this scale": the pin
                # is clamped to [0, 1] and e.g. rerank logits are unbounded
                # with a negative median, so a pin cannot express a foreign
                # scale. And NOT "upgrade the core": score_scale only exists
                # on cores that already ship the bypass, so an upgrade can
                # never be the fix for this fire.
                pin_cfg = self._config.context_tools.get(tool)
                if pin_cfg is not None and pin_cfg.min_score is not None:
                    scale_fix = (
                        f"the context_tools.{tool}.min_score pin keeps the "
                        "RRF-calibrated filter active on this scale; adjust "
                        "or remove the pin"
                    )
                else:
                    scale_fix = (
                        "set surfacing.scale_gated_min_score=true to suspend "
                        "the filter on core-named non-RRF scales"
                    )
                if scale == "rerank":
                    remedy = (
                        "check surfacing.rerank (default false should return "
                        "RRF scores; the core may not honor the bypass param) "
                        "or " + scale_fix
                    )
                else:
                    remedy = scale_fix
                logger.warning(
                    "Surfacing score-scale mismatch for %s/%s: core reports "
                    "score_scale=%r%s but min_score=%.4f is calibrated for the "
                    "RRF scale (observed ceiling=%.4f). Every result is being "
                    "filtered out; %s. STM did not change the threshold.",
                    server,
                    tool,
                    scale,
                    f" (reranker={reranker})" if reranker else "",
                    min_score,
                    result_max,
                    remedy,
                )
                self._persist_diagnostic(server, tool, "score_scale_mismatch")
                self._score_scale_mismatch_active.add(key)
            # The named scale supersedes streak evidence: keep the heuristic
            # counter out of a window the definitive diagnostic already covers.
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
            if scale == "rrf":
                # Core confirmed the scale, so the wide "may be running
                # single-leg/BM25-only" hedge collapses to the two causes
                # that survive on a confirmed RRF scale.
                cause = (
                    "Core confirms score_scale=rrf, so the fusion is running "
                    "single-leg (one retriever contributing) or min_score is "
                    "intentionally high; check embedding extras and LTM logs"
                )
            else:
                cause = (
                    "LTM may be running single-leg/BM25-only or min_score may "
                    "be intentionally high; check embedding extras and LTM logs"
                )
            logger.warning(
                "Surfacing score-scale mismatch for %s/%s: %d consecutive "
                "non-empty LTM searches had max score below active min_score "
                "(observed ceiling=%.4f, min_score=%.4f). %s. "
                "STM did not lower the threshold.",
                server,
                tool,
                _SCORE_SCALE_WARNING_STREAK,
                streak.observed_max,
                min_score,
                cause,
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
        digest = hashlib.sha256(query.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
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

    def _scale_gate_suspends(self, tool: str, score_scale: str | None) -> bool:
        """Return True when this batch's core-reported scale suspends the
        global/auto-tuned ``min_score`` filter (see
        ``SurfacingConfig.scale_gated_min_score``).

        Only a KNOWN non-RRF label suspends: an absent scale (compact format,
        pre-#1781 cores, compose on a pre-#1796 core) or an unrecognized label
        keeps unconditional filtering — the gate never guesses. A compose
        schema-4 core (#1796) reports the scale, so the compose path is now
        gated too. A per-tool ``context_tools.<tool>.min_score`` pin is
        explicit operator intent and always keeps the filter active.
        """
        if not self._config.scale_gated_min_score:
            return False
        if score_scale is None or score_scale not in KNOWN_SCORE_SCALES or score_scale == "rrf":
            return False
        tool_cfg = self._config.context_tools.get(tool)
        return tool_cfg is None or tool_cfg.min_score is None

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
            # Last core-REPORTED scale (#1781/#1796): ``None`` means no
            # structured search or compose bundle this process carried the key
            # (pre-#1781 core, compact format, or compose on a pre-#1796 core)
            # — not that scores are RRF.
            # ``filter_suspended`` is the process-level summary — "the last
            # reported scale suspends the filter for unpinned tools" — not a
            # per-tool verdict (pinned tools always keep the filter).
            "score_scale": {
                "last_reported": self._last_score_scale,
                "reranker": self._last_reranker,
                "gate_enabled": self._config.scale_gated_min_score,
                "filter_suspended": (
                    self._config.scale_gated_min_score
                    and self._last_score_scale is not None
                    and self._last_score_scale in KNOWN_SCORE_SCALES
                    and self._last_score_scale != "rrf"
                ),
            },
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
        deadline_monotonic: float | None = None,
    ) -> str:
        """Surface relevant memories and inject into response_text.

        Returns the original response_text unmodified if:
        - surfacing is disabled
        - circuit breaker is open
        - relevance gate rejects the call
        - timeout exceeded

        ``deadline_monotonic`` — an absolute ``time.monotonic()`` point — lets
        a caller that is itself deadline-bounded (the daemon, whose client
        gives up after ``hook.daemon_timeout_seconds``) shrink this call's
        timeout below ``timeout_seconds``. It never *raises* the configured
        ceiling. Passing it keeps the abort inside :meth:`surface` instead of
        letting the caller cancel it from outside: the
        :class:`asyncio.TimeoutError` path below records the fault, logs, and
        counts the failure toward the breaker (#579), and an outside
        cancellation skips all three, so the breaker never opens and every
        subsequent call pays the full timeout and respawns the LTM child again.
        :meth:`_run_within` is what makes that abort reliably ours to raise
        rather than a race against the caller's backstop (#720). A cancellation
        that reaches here is therefore one this call did not start — a
        shutdown, a client hanging up — and propagates unbooked rather than
        charging a healthy LTM.
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
        # On pass the gate eagerly claims a rate-limit slot; the claim token
        # rides along so a path that ends up starting no LTM work can give
        # back exactly its own slot (``release_claim``).
        rate_claim = self._gate.should_surface(server, tool, query)
        if rate_claim is None:
            # Gate has already recorded the specific reason internally. Avoid
            # double-counting by not recording at the engine level here.
            logger.debug(
                "Surfacing skipped: gate rejected %s/%s (query=%s)",
                server,
                tool,
                query[:50],
            )
            return response_text

        effective_timeout = self._effective_timeout(deadline_monotonic)
        try:
            if effective_timeout <= 0:
                # Pre-timeout work (gate, query extraction, privacy scan)
                # consumed the caller's whole window (#720). Book the abort
                # through the branch below without starting an LTM round trip
                # that would be cancelled mid-RPC and force a stdio child
                # respawn on the next call (#290/#296). No LTM work started
                # also means no rate-limit attempt was made (the cap counts
                # attempts because an attempt spent LTM resources — see
                # ``release_claim``); the timeout/breaker booking still
                # stands, since real time did pass.
                self._gate.release_claim(rate_claim)
                raise asyncio.TimeoutError
            result = await self._run_within(
                self._do_surface(
                    server,
                    tool,
                    arguments,
                    response_text,
                    query,
                    rate_claim=rate_claim,
                    trace_id=trace_id,
                ),
                effective_timeout,
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
                # Clamped: the pre-work branch above reaches here with a
                # window already spent, and a negative "limit" reads as a bug.
                max(effective_timeout, 0.0),
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
                    _fifo_prune(self._boosted_event_ids, self._boosted_event_ids_max)
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
                _fifo_prune(self._boosted_event_ids, self._boosted_event_ids_max)
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
        _fifo_prune(self._invalidated_ids, self._invalidated_ids_max)

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
        _fifo_prune(self._surfaced_ids, self._surfaced_ids_max)

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
        # See the miss path: the event row is tracker-gated telemetry;
        # ``record_feedback_events`` gates only whether the ID is advertised
        # as a rating prompt, so a no-tracker path skips the row and a
        # feedback-loop-off path records it prompt-free.
        surfacing_id: str | None = None
        if self._feedback_tracker is not None:
            surfacing_id = uuid.uuid4().hex[:16]
        advertised_id = surfacing_id if self._record_feedback_events else None
        manifest = self._formatter.render(
            response_text,
            cached,
            query,
            surfacing_id=advertised_id,
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
                    # Cached entries keep the scale they were stamped with at
                    # miss time, so the hit-path row carries it too.
                    score_scale=self._result_score_scale(cached)[0],
                )
            except Exception:
                logger.warning("Failed to record cached surfacing event", exc_info=True)
                # Kept symmetric with the miss path; the cached path fires no
                # webhook, so this reset has no consumer here — it just states
                # "no row was written" for the next reader.
                surfacing_id = None
                if advertised_id is not None:
                    # Same contract as the miss path: withdraw the dead
                    # feedback handle; a prompt-free render stands as-is.
                    advertised_id = None
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
        rate_claim: RateClaim,
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
                    rate_claim=rate_claim,
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
        rate_claim: RateClaim,
        trace_id: str | None = None,
    ) -> str:
        """Admission for the one path that starts LTM work, then the work.

        Deliberately not at the top of :meth:`surface`: gating there would also
        refuse cache hits, which need no LTM at all, and would relabel every
        ``no_query`` or gate rejection as ``ltm_draining``.
        """
        if len(self._abandoned_ops) >= _MAX_ABANDONED_OPS:
            # No LTM work started, so this is not an "attempt" by the rate
            # limiter's own definition — give the slot back rather than let a
            # run of refusals spend the throttle on nothing.
            self._gate.release_claim(rate_claim)
            self._observability.record_skip(tool, "ltm_draining")
            self._persist_fault(server, tool, "ltm_draining")
            # Warn once per draining episode (see the latch's init comment):
            # refusals record nothing on the breaker, so with the reset window
            # elapsed every call lands here and per-call warnings would flood
            # the log for as long as the LTM stays wedged.
            if self._draining_warning_latched:
                logger.debug(
                    "Surfacing skipped for %s/%s: %d cancelled LTM operation(s) still unwinding",
                    server,
                    tool,
                    len(self._abandoned_ops),
                )
            else:
                self._draining_warning_latched = True
                logger.warning(
                    "Surfacing skipped for %s/%s: %d cancelled LTM operation(s) still "
                    "unwinding — the LTM is not releasing cancelled calls (further "
                    "refusals log at debug until the pile drains)",
                    server,
                    tool,
                    len(self._abandoned_ops),
                )
            raise _OperationalSkip("ltm_draining")

        # Admission passed: the pile drained below the bound, so the episode
        # is over and the next one deserves its own warning.
        self._draining_warning_latched = False
        return await self._do_surface_miss_admitted(
            server,
            tool,
            arguments,
            response_text,
            query,
            cache_key,
            trace_id=trace_id,
        )

    async def _do_surface_miss_admitted(
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
        tool_cfg = self._config.context_tools.get(tool)
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
        compose_transport_failed = False
        try:
            capabilities = getattr(self._mcp_adapter, "capabilities", None)
            compose = getattr(self._mcp_adapter, "context_compose", None)
            bundle = None
            if (
                isinstance(capabilities, LtmCapabilities)
                and capabilities.context_compose_schema >= 2
                and callable(compose)
            ):
                try:
                    compose_max_chars = self._config.effective_max_injection_chars()
                    if capabilities.context_compose_schema >= 3 and ctx_win:
                        # Schema 3 carries whole adjacent chunks on the wire.
                        # Give each requested before/after distance one base
                        # budget while the formatter still enforces the
                        # original injection cap. Core clamps windows at 10.
                        compose_max_chars *= 1 + 2 * min(ctx_win, 10)
                    bundle = await compose(
                        query,
                        max_chars=compose_max_chars,
                        top_k=max_results * 2,
                        namespace=namespace,
                        context_window=ctx_win,
                        trace_id=trace_id,
                    )
                except LtmTransportError:
                    logger.debug("Core context composition transport failed", exc_info=True)
                    compose_transport_failed = True
                except (RuntimeError, ValueError) as exc:
                    # A declared compose surface is authoritative: classify
                    # its failure like a legacy upstream call failure, but do
                    # not hide it with a second search request.
                    logger.debug("Core context composition failed", exc_info=True)
                    if not self._warned_compose_failed:
                        logger.warning(
                            "Surfacing degraded: LTM declared context_compose "
                            "schema %s but the call failed (%s). Subsequent "
                            "failures counted as 'ltm_call_failed' in "
                            "stm_surfacing_stats.",
                            capabilities.context_compose_schema,
                            redact_exception_text(str(exc), self._config.ltm_mcp_url or ""),
                        )
                        self._warned_compose_failed = True
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
            elif compose_transport_failed:
                results, hints, outcome = [], [], "transport_error"
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
        score_scale, reranker_id = self._result_score_scale(retrieved_results)
        # min_score precedence: tool_cfg override > scale gate > auto-tune >
        # global default — resolved AFTER retrieval so the gate can see the
        # batch's core-reported scale (nothing upstream consumes min_score).
        # maybe_adjust is skipped on a suspended batch so the RRF-calibrated
        # tuner doesn't move on evidence from a foreign scale; on a pinned
        # tool ``_active_min_score`` returns the pin before the tuner runs,
        # so the pre-existing "pinned tools don't learn" behavior holds.
        filter_suspended = self._scale_gate_suspends(tool, score_scale)
        min_score = self._active_min_score(tool, adjust_auto_tuner=not filter_suspended)
        self._observe_score_scale(
            server,
            tool,
            retrieved_results,
            min_score,
            score_scale,
            reranker_id,
            filter_suspended=filter_suspended,
        )
        if score_scale is not None:
            self._last_score_scale = score_scale
            self._last_reranker = reranker_id

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
        if filter_suspended:
            # Core-named non-RRF scale: the RRF-calibrated floor does not
            # apply. Keep the finite guard the ``>=`` comparison used to
            # provide — a NaN score must not be injected or persisted into
            # ``surfacing_events.scores``. Volume stays bounded by the
            # ``max_results`` cap below.
            scored = [r for r in retrieved_results if math.isfinite(float(r.score))]
            if not self._scale_gate_logged:
                self._scale_gate_logged = True
                logger.info(
                    "Surfacing min_score filter suspended for %s/%s: core "
                    "reports score_scale=%r and min_score=%.4f is calibrated "
                    "for the RRF scale. Results stay bounded by max_results; "
                    "per-tool context_tools.<tool>.min_score pins still "
                    "apply; set surfacing.scale_gated_min_score=false to "
                    "restore unconditional filtering.",
                    server,
                    tool,
                    score_scale,
                    min_score,
                )
            else:
                logger.debug(
                    "Scale gate active for %s/%s (score_scale=%r): min_score not applied",
                    server,
                    tool,
                    score_scale,
                )
        else:
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
            if filter_suspended:
                logger.debug(
                    "Surfacing: no results after dedup/demotion for %s/%s "
                    "(min_score suspended, score_scale=%r)",
                    server,
                    tool,
                    score_scale,
                )
            else:
                logger.debug(
                    "Surfacing: no results above min_score=%.2f for %s/%s",
                    min_score,
                    server,
                    tool,
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

        # Mint an event ID whenever a tracker is attached: the
        # ``surfacing_events`` row is telemetry (stm_surfacing_stats /
        # mms stats / mms doctor), written like ``seen_memories`` and the fault
        # counters regardless of the feedback loop. ``record_feedback_events``
        # gates only whether the ID is ADVERTISED as a
        # ``stm_surfacing_feedback(...)`` rating prompt — the hook daemon path
        # has no in-band channel to resolve one, so it records the row (query
        # already digest-substituted via ``persist_query_text=False``) but
        # renders no prompt. Only the no-tracker ``mms hook`` /
        # feedback-disabled server paths skip the row entirely.
        surfacing_id: str | None = None
        if self._feedback_tracker is not None:
            surfacing_id = uuid.uuid4().hex[:16]
        advertised_id = surfacing_id if self._record_feedback_events else None

        # Inject memories into response
        manifest = self._formatter.render(
            response_text,
            relevant,
            query,
            surfacing_id=advertised_id,
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
                    score_scale=score_scale,
                )
            except Exception:
                logger.warning("Failed to record surfacing event", exc_info=True)
                # No row was written, so the webhook payload must carry None.
                surfacing_id = None
                if advertised_id is not None:
                    # The rendered prompt references an unresolvable event ID.
                    # Re-render without it instead of handing the agent a dead
                    # feedback handle. When nothing was advertised (feedback
                    # loop off) the rendered text carries no ID, so the output
                    # stands as-is.
                    advertised_id = None
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
                        "score_scale": score_scale,
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
        """Cancel and drain pending background tasks (webhooks), then give
        abandoned LTM operations a bounded chance to finish unwinding.

        The two are drained differently on purpose. A webhook POST is bounded
        by its own client timeout, so waiting for it is safe. An abandoned LTM
        operation is not — ``_run_within`` stopped waiting on it precisely
        because a stdio child can be slow, or refuse, to give up — so waiting
        without a bound would let one such unwind hold the daemon's shutdown
        open, and this runs before the adapter's own bounded stop. They only
        get ``_ABANDONED_DRAIN_SECONDS``; anything still in flight is left to
        the loop's teardown rather than blocking it.

        They are also *not* re-cancelled here. ``_abandon`` already cancelled
        each one, and what they are doing now is the cleanup that cancellation
        asked for — the adapter marking its session for lazy reconnect, locks
        unwinding. A second cancellation lands inside that cleanup and aborts
        it, which is the opposite of draining.
        """
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        stragglers = {task for task in self._abandoned_ops if not task.done()}
        if stragglers:
            _, pending = await asyncio.wait(stragglers, timeout=_ABANDONED_DRAIN_SECONDS)
            if pending:
                # Deliberately still referenced: the set exists to keep an
                # unwinding op from being collected mid-flight, and giving up
                # waiting is not the same as it having finished. Each one
                # retires itself through ``_on_abandoned_op_done``.
                logger.debug(
                    "%d abandoned surfacing operation(s) still unwinding at stop", len(pending)
                )

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
