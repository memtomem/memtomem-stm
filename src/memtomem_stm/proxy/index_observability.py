"""In-memory counters for STM-driven LTM writes (the INDEX path).

Mirrors ``surfacing/observability.py`` in spirit and shape so ``stm_index_stats``
and ``stm_surfacing_stats`` stay symmetric at the operator surface — but
**deliberately omits a quality dimension**. SURFACE has a quality signal
(``surfacing-feedback``) that lets operators ask "did the agent find this
useful?". INDEX has no equivalent. The absence is a design finding, not
an oversight: a quality dimension for INDEX would have to choose between
two non-equivalent observables — positive-value (does an extracted fact
later get retrieved on a related query?) and harm-prevented (what fraction
of writes would have leaked sensitive content absent redaction?). Bundling
them in one signal would conflate two questions; deferring lets a future
quality-signal PR scope to one or both deliberately. Until then, the
schema-level asymmetry self-documents the architectural state.

State is per-process in-memory only. Counters reset on restart, mirroring
``SurfacingObservability``. The cross-call aggregate is the load-bearing
addition over ``ExtractOutcome.facts_stored``, which is per-call only and
discards as soon as the caller drops the return value.

Snapshot output is a flat dict so ``server.py::stm_index_stats`` formats
without coupling to internal data structures.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Literal

# Per-fact OR per-call outcome labels (mixed granularity by design):
# - ``stored`` / ``dedup_skip``: per fact processed inside one call.
# - ``extracted_zero_facts``: per call, terminal — extractor returned ``[]``
#   so the loop body never ran.
# - ``error``: per error event. Outer ``extractor.extract`` failure counts
#   once (per call); per-fact ``index_file`` failure counts per fact.
#
# Math contract for ``snapshot()``:
#   sum(outcomes["__total__"][k] for k in ALL) is **not** equal to
#   ``attempts["__total__"]`` because per-fact outcomes can multi-count
#   one attempt. Useful operator ratios:
#   - ``outcomes["__total__"]["stored"]`` = literal mem_add count into LTM
#     per process — the raw write-volume signal across windows. Callers
#     compute per-turn or per-second rates at the read site if needed.
#   - ``outcomes["__total__"]["extracted_zero_facts"] / attempts["__total__"]``
#     = fraction of calls where INDEX produced zero LTM writes — the
#     direct measurement of "extraction fired on a tool call but found
#     nothing worth storing," architecturally distinct from "facts existed
#     but were duplicates" (``dedup_skip``) and from "facts existed and
#     were stored" (``stored``).
IndexOutcome = Literal[
    "stored",
    "dedup_skip",
    "extracted_zero_facts",
    "error",
]

_TOTAL_KEY = "__total__"


class IndexObservability:
    """Aggregate per-tool counters for the INDEX (extract_and_store) pipeline.

    Thread-safe via a single coarse lock — same trade-off as
    ``SurfacingObservability``: O(1) dict writes, contention dwarfed by
    the LTM round-trip the counters observe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: dict[str, int] = defaultdict(int)
        self._outcomes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._any_call = False

    def record_attempt(self, tool: str) -> None:
        """Record one ``extract_and_store`` invocation. Per-call.

        Increments per-tool counter and the ``__total__`` aggregate.
        """
        with self._lock:
            self._any_call = True
            self._attempts[tool] += 1
            self._attempts[_TOTAL_KEY] += 1

    def record_outcome(self, tool: str, outcome: IndexOutcome) -> None:
        """Record one outcome event (per-fact or per-call — see module docstring)."""
        with self._lock:
            self._any_call = True
            self._outcomes[tool][outcome] += 1
            self._outcomes[_TOTAL_KEY][outcome] += 1

    def snapshot(self) -> dict:
        """Return a deep-copied point-in-time view of all counters.

        Empty (``any_call=False``) when ``extract_and_store`` has never
        been invoked — ``stm_index_stats`` uses this to short-circuit on
        zero-traffic deployments, mirroring ``stm_surfacing_stats``.
        """
        with self._lock:
            return {
                "any_call": self._any_call,
                "attempts": dict(self._attempts),
                "outcomes": {tool: dict(o) for tool, o in self._outcomes.items()},
            }


class _NoOpIndexObservability:
    """No-op stand-in used when ``extract_and_store`` is called without an
    explicit observability instance. Lets the recording call sites stay
    unconditional (``observability.record_attempt(...)``) instead of
    guarding every site — same pattern as
    ``surfacing/observability._NoOpObservability``.

    ``snapshot()`` is intentionally absent — consumers (``stm_index_stats``)
    short-circuit on ``ProxyManager.index_observability is None`` rather
    than calling through the no-op.
    """

    __slots__ = ()

    def record_attempt(self, tool: str) -> None:
        return None

    def record_outcome(self, tool: str, outcome: IndexOutcome) -> None:
        return None


_NOOP_INDEX_OBSERVABILITY: _NoOpIndexObservability = _NoOpIndexObservability()
