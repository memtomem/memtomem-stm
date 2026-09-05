"""In-memory counters for surfacing skip reasons, outcomes, and cache hits.

Every reject path through the pipeline (gate-level + engine-level + post-search)
records a counter so ``stm_surfacing_stats`` can answer the operator question
"why is surfacing not firing on tool X?" without grepping DEBUG logs.

State is per-process in-memory only. Counters reset on restart, mirroring the
shape of ``stm_compression_stats`` / ``stm_progressive_stats`` at the read
surface (those persist events to SQLite, but the in-process counter view is
the operationally useful aggregate). If longitudinal analysis becomes a need,
add a separate persistence phase — the noisy per-call cardinality is the
reason this stays in-memory for now.

Snapshot output is intentionally derivable from a flat dict so the consumer
(``server.py::stm_surfacing_stats``) can format it without knowing the
internal data structure shape.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

# Per-call decision tree categories. The engine records exactly one
# ``skip_reason`` OR one ``outcome`` per ``surface()`` call (mutually
# exclusive). Cache hit/miss is incremented separately and may co-occur with
# either bucket — a cache lookup that returns an empty/invalidated entry
# still counts toward cache hits even though the outcome is a no-results
# skip. See RFC § "Resolution order (Phase 1 specifics)" for the full
# decision tree.
SkipReason = Literal[
    "disabled",
    "response_too_short",
    "circuit_open",
    "no_query",
    "gate_excluded_tool",
    "gate_write_tool",
    "gate_tool_disabled",
    "gate_rate_limit",
    "gate_cooldown",
    "no_results_score",
    "no_results_dedup",
    # #404: durable negative-feedback demotion excluded every candidate
    # that passed the score filter — distinct from ``no_results_score`` /
    # ``no_results_dedup`` so an operator can tell the demotion filter
    # (not the threshold or session-dedup) is suppressing surfacing.
    "no_results_demoted",
    "no_results_invalidated",
    "no_results_empty_cache",
    # #720: previous attempts against this LTM were abandoned at timeout and
    # are still unwinding. Starting another only adds to the pile, so the
    # engine declines rather than evicting a running operation's reference.
    "ltm_draining",
    # #295: LTM adapter outcome typing — distinguish unreachable from
    # call_failed from parse_empty so operators can tell "session never
    # opened / transport down" from "core raised mid-call" from "core
    # returned no text content" without grepping logs.
    "ltm_unavailable",
    "ltm_call_failed",
    "ltm_parse_empty",
    # Shared-daemon operational load shedding. These are healthy skips: the
    # current tool response passes through and the local circuit breaker must
    # not treat daemon startup/queue pressure as an LTM dependency failure.
    "daemon_starting",
    "daemon_busy",
    # #348: progressive-delivery path skips surfacing when the engine's
    # injection mode would shift character offsets and break the
    # ``PROGRESSIVE_FOOTER_TOKEN`` concat invariant ``stm_proxy_read_more``
    # relies on. Today only ``injection_mode='prepend'`` triggers this; the
    # label is mode-agnostic so a future per-mode constraint can reuse it
    # without a new enum value.
    "progressive_mode_conflict",
    # Per-upstream opt-out: ``UpstreamServerConfig.surfacing_enabled=False``
    # short-circuits surfacing for every tool on that server. Recorded by
    # ``ProxyManager`` before the engine runs (the engine is built once from
    # the top-level ``SurfacingConfig`` and never sees per-upstream config),
    # so it lands here rather than as a ``gate_*`` reason.
    "upstream_disabled",
]

# Operator-facing categorization for ``stm_surfacing_stats`` (#362, #351 part 2).
# Healthy skips are expected gate/threshold/no-results outcomes that mean
# surfacing intentionally declined to fire; fault skips are degraded LTM /
# circuit-breaker states that indicate something is wrong. Without the split,
# 1000 ``gate_cooldown`` and 1000 ``ltm_unavailable`` render identically and
# operators can't tell at a glance whether the bypass count is healthy backoff
# or LTM trouble.
#
# Every ``SkipReason`` member must appear in exactly one set —
# ``test_skip_reason_categorization_is_exhaustive`` pins this so a new enum
# value added without classification fails CI rather than silently dropping
# out of the rendered output. ``ltm_parse_empty`` is classified fault because
# #295 introduced it specifically to distinguish "core returned no text
# content" from the healthy ``no_results_*`` family — it's the degraded shape,
# not a normal empty result.
HEALTHY_SKIP_REASONS: frozenset[str] = frozenset(
    {
        "disabled",
        "response_too_short",
        "no_query",
        "gate_excluded_tool",
        "gate_write_tool",
        "gate_tool_disabled",
        "gate_rate_limit",
        "gate_cooldown",
        "no_results_score",
        "no_results_dedup",
        "no_results_demoted",
        "no_results_invalidated",
        "no_results_empty_cache",
        "daemon_starting",
        "daemon_busy",
        "progressive_mode_conflict",
        "upstream_disabled",
    }
)
FAULT_SKIP_REASONS: frozenset[str] = frozenset(
    {
        "circuit_open",
        "ltm_draining",
        "ltm_unavailable",
        "ltm_call_failed",
        "ltm_parse_empty",
    }
)

Outcome = Literal[
    "surfaced_cache_hit",
    "surfaced_cache_miss",
    "error_timeout",
    "error_other",
]

# Inputs to the ``stm_surfacing_stats`` top-line verdict (#363, #351 part 1).
#
# The verdict answers "is surfacing healthy?" from a fault *ratio*, so it needs
# a denominator that only counts attempts which actually depended on the LTM.
# The three sets below name that denominator explicitly rather than leaving it
# implicit in the renderer:
#
#   attempts = faults + completed
#   faults    = FAULT_SKIP_REASONS skips + FAULT_OUTCOMES
#   completed = SURFACED_OUTCOMES + SEARCH_COMPLETED_SKIP_REASONS
#
# Pre-LTM healthy skips (``response_too_short``, ``gate_*``, ``no_query``,
# ``daemon_*``, ``disabled``, ``upstream_disabled``, ``progressive_mode_conflict``)
# are deliberately excluded from both sides: they are decided before any LTM
# work is attempted, so counting them would let a thousand cooldowns dilute a
# total LTM outage down to a "healthy" ratio. The ``no_results_*`` family is
# the opposite case — the search round trip completed and returned candidates
# that were then filtered to nothing, so those are healthy *completions* and
# belong in the denominator.
#
# ``circuit_open`` is recorded before the relevance gate (engine.py::surface),
# so during a sustained outage the ratio saturates toward 100% rather than
# being masked by cooldown skips. That matches the dogfood distribution the
# thresholds in server.py are anchored against.
SEARCH_COMPLETED_SKIP_REASONS: frozenset[str] = frozenset(
    {
        "no_results_score",
        "no_results_dedup",
        "no_results_demoted",
        "no_results_invalidated",
        "no_results_empty_cache",
    }
)

SURFACED_OUTCOMES: frozenset[str] = frozenset(
    {
        "surfaced_cache_hit",
        "surfaced_cache_miss",
    }
)

FAULT_OUTCOMES: frozenset[str] = frozenset(
    {
        "error_timeout",
        "error_other",
    }
)

CacheBucket = Literal["hit", "miss"]

# Sentinel string key for the "all tools" aggregate row in per-tool dicts.
# Stays a string (not an ``object()`` sentinel) so ``snapshot()`` keeps
# returning a JSON-serializable dict — the consumer
# (``server.py::stm_surfacing_stats``) renders the dict as text and a
# downstream JSON wrapper around the same shape would otherwise need a
# custom encoder. The double-underscore prefix means a real tool name
# would have to be literally ``__total__`` to collide, which is
# vanishingly unlikely; the consumer also uses an explicit
# ``__total__``-first ordering rather than relying on lexicographic sort
# so even a tool name starting with ``__`` would not bury the aggregate.
_TOTAL_KEY = "__total__"


@dataclass
class CallLedger:
    """What one ``surface()`` call recorded, for the caller that opened it.

    The counters this sits beside are process-global sums. A caller that needs
    to know what *its own* call did — the daemon, which files a latency sample
    per request — used to read those sums before and after and take the delta,
    which is only correct while exactly one call can be in flight. This is the
    same information without that requirement (#874).

    Cache buckets are not recorded here: no consumer asks a per-call question
    about them, and leaving them out keeps the ledger to the two mutually
    exclusive per-call decisions (``record_skip`` OR ``record_outcome``) plus
    the one fact neither of them can carry — whether a request actually left
    for the LTM (#994).
    """

    skip_reasons: list[str] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    # Set by :func:`record_search_rpc` at the moment a search request is handed
    # to the LTM transport — the one place that knows a round trip was issued.
    search_rpc_issued: bool = False

    @property
    def retrieval_attempted(self) -> bool:
        """Did a *search* RPC leave the process for this call?

        Read from the explicit mark :func:`record_search_rpc` sets in the
        adapter, never inferred from the terminal decision: ``error_timeout``
        and ``ltm_unavailable`` are both recorded on paths that sent nothing
        (pre-work that spent the window, #720; healing that failed), and a
        duration taken there measures STM, not the LTM (#994).

        Scope is the two search actions, ``mem_search`` and ``context_compose``.
        Lifecycle exchanges (version probe, ``list_tools``) and the bookkeeping
        RPCs around a search do not mark -- a connect is not a search, and
        marking one would put spawn and negotiation time into percentiles that
        are advice about a warm search. An unmarked call files no duration at
        all; when it ended badly the daemon counts it as ``pre_rpc_faults``.
        The reasoning behind each boundary is in the CHANGELOG entry for #994.
        """
        return self.search_rpc_issued

    @property
    def faulted(self) -> bool:
        """Did this call end on a degraded-dependency terminal?

        Read together with :attr:`retrieval_attempted` by a caller bucketing a
        duration: an RPC that went out and then failed mid-flight is a real
        measurement of the LTM, but not of a *successful* search, so it does
        not belong in the percentiles that answer how long one takes. The
        daemon cannot see this in the response — the engine returns the
        caller's text unchanged on a fault, so a surfacing reply is shaped
        exactly like a healthy one that surfaced nothing (#994).

        Deliberately the same membership as the ``stm_surfacing_stats`` fault
        ratio, so an operator comparing the two reads one definition of
        "fault". ``timed_out`` overlaps by way of ``error_timeout`` and is kept
        separate because its consumer buckets a timeout before an error.
        """
        return any(reason in FAULT_SKIP_REASONS for reason in self.skip_reasons) or any(
            outcome in FAULT_OUTCOMES for outcome in self.outcomes
        )

    @property
    def timed_out(self) -> bool:
        """Did this call abort on its own timeout?

        The engine handles that abort itself and returns a well-formed empty
        result, which is shape-identical to "nothing relevant" — so a caller
        that wants to keep the two apart has to be told.
        """
        return "error_timeout" in self.outcomes


_ACTIVE_LEDGER: ContextVar[CallLedger | None] = ContextVar(
    "memtomem_stm_surfacing_call_ledger", default=None
)


@contextmanager
def attribute_call() -> Iterator[CallLedger]:
    """Collect what the surfacing call made inside this block recorded.

    Opened per request by a caller that has to answer a per-call question the
    global counters cannot answer once calls overlap (#874) — today the daemon,
    deciding whether to file a latency sample and whether it is a timeout. A
    block that runs no surfacing yields an empty ledger, which reads as
    "nothing was attempted".

    Two rules make this reliable, both satisfied by the daemon's
    one-task-per-connection request handling:

    - Set and reset happen in the same task. A ``ContextVar`` token is only
      valid in the context that set it.
    - Work the call spawns records into the *same* ledger, because a new task
      copies the current context and the ledger is shared by reference. That is
      what carries the engine's ``_run_within`` operation task, where the
      timeout outcome is decided.

    The converse is the safety property: an operation abandoned at timeout
    keeps a reference to a ledger whose block has already exited, so a late
    record lands in an object nobody reads — never in another request's.

    Scoped to the *context*, not to one :class:`SurfacingObservability`: every
    instance recording inside this block appends here. A process with two
    engines surfacing under one block would therefore blend them, which is not
    a shape the daemon has (one engine, one adapter) and not one this is for.
    Nesting is likewise unsupported — an inner block would silently take the
    outer block's records.
    """
    ledger = CallLedger()
    token = _ACTIVE_LEDGER.set(ledger)
    try:
        yield ledger
    finally:
        _ACTIVE_LEDGER.reset(token)


def record_search_rpc() -> None:
    """Mark that the surfacing call in this context is issuing a search RPC.

    Called by the transport adapter immediately before a ``mem_search`` or
    ``context_compose`` request is handed to the session -- after healing has
    produced a live one, so a heal that fails (``no_session``) leaves the mark
    unset, and a retry after a transport error sets it again harmlessly. Other
    RPCs through the same adapter do not call this. Outside any
    :func:`attribute_call` block (the proxy path, a daemon operation that opens
    no ledger) this is a no-op.
    """
    ledger = _ACTIVE_LEDGER.get()
    if ledger is not None:
        ledger.search_rpc_issued = True


class SurfacingObservability:
    """Aggregate per-tool counters for the surfacing pipeline.

    Thread-safe via a single coarse lock — increments are O(1) dict writes,
    so contention on the surfacing hot path is negligible compared to the
    LTM round-trip the counters are observing.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._skip_reasons: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._outcomes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._cache: dict[str, int] = defaultdict(int)
        # Tracks whether ``surface()`` has been called at least once. Used by
        # ``stm_surfacing_stats`` to suppress the new sections entirely when
        # the engine is wired but never invoked, keeping the legacy output
        # byte-for-byte for callers that script against the unchanged shape.
        self._any_call = False

    def record_skip(self, tool: str, reason: SkipReason) -> None:
        """Record a per-tool reject. ``__total__`` aggregate is also updated.

        Also appended to the :class:`CallLedger` open in this context, if any.
        """
        with self._lock:
            self._any_call = True
            self._skip_reasons[tool][reason] += 1
            self._skip_reasons[_TOTAL_KEY][reason] += 1
        ledger = _ACTIVE_LEDGER.get()
        if ledger is not None:
            ledger.skip_reasons.append(reason)

    def record_outcome(self, tool: str, outcome: Outcome) -> None:
        """Record a per-tool outcome. ``__total__`` aggregate is also updated.

        Also appended to the :class:`CallLedger` open in this context, if any.
        """
        with self._lock:
            self._any_call = True
            self._outcomes[tool][outcome] += 1
            self._outcomes[_TOTAL_KEY][outcome] += 1
        ledger = _ACTIVE_LEDGER.get()
        if ledger is not None:
            ledger.outcomes.append(outcome)

    def record_cache(self, bucket: CacheBucket) -> None:
        """Record a single cache lookup (independent of outcome)."""
        with self._lock:
            self._any_call = True
            self._cache[bucket] += 1

    def snapshot(self) -> dict:
        """Return a deep-copied point-in-time view of all counters.

        Empty when ``surface()`` has never been invoked — the consumer uses
        the empty shape to skip rendering the new sections, preserving the
        legacy ``stm_surfacing_stats`` output for zero-traffic deployments.
        """
        with self._lock:
            return {
                "any_call": self._any_call,
                "skip_reasons": {
                    tool: dict(reasons) for tool, reasons in self._skip_reasons.items()
                },
                "outcomes": {tool: dict(outcomes) for tool, outcomes in self._outcomes.items()},
                "cache": dict(self._cache),
            }


class _NoOpObservability:
    """No-op stand-in used when the engine/gate is constructed without an
    explicit observability instance. Lets the recording call sites stay
    unconditional (``self._observability.record_skip(...)``) instead of
    18+ ``if self._observability is not None:`` guards across engine.py
    and relevance.py — the helper-extract threshold from the codebase's
    "4+ defensive sites recur" rule.

    ``snapshot()`` is intentionally absent — callers asking for a
    snapshot from a no-op instance is a programming error (the engine
    exposes ``observability`` as ``SurfacingObservability | None``, so
    consumers like ``stm_surfacing_stats`` can short-circuit on
    ``None``). Distinguishing the no-op from a real instance is the job
    of the engine's ``observability`` property accessor; the no-op is
    purely an internal recording sink.
    """

    __slots__ = ()

    def record_skip(self, tool: str, reason: SkipReason) -> None:
        return None

    def record_outcome(self, tool: str, outcome: Outcome) -> None:
        return None

    def record_cache(self, bucket: CacheBucket) -> None:
        return None


_NOOP_OBSERVABILITY: _NoOpObservability = _NoOpObservability()
