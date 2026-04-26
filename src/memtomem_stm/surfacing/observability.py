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
    "no_results_invalidated",
    "no_results_empty_cache",
]

Outcome = Literal[
    "surfaced_cache_hit",
    "surfaced_cache_miss",
    "error_timeout",
    "error_other",
]

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
        """Record a per-tool reject. ``__total__`` aggregate is also updated."""
        with self._lock:
            self._any_call = True
            self._skip_reasons[tool][reason] += 1
            self._skip_reasons[_TOTAL_KEY][reason] += 1

    def record_outcome(self, tool: str, outcome: Outcome) -> None:
        """Record a per-tool outcome. ``__total__`` aggregate is also updated."""
        with self._lock:
            self._any_call = True
            self._outcomes[tool][outcome] += 1
            self._outcomes[_TOTAL_KEY][outcome] += 1

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
