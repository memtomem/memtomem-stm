"""In-memory counters for STM-driven LTM writes (the INDEX paths).

Tracks both STM-driven LTM-write paths the proxy maintains: the
verbatim ``auto_index_response`` (full response → markdown chunk) and
the LLM-based ``extract_and_store`` (response → fact list → markdown
chunks). Both write to LTM via ``index_engine.index_file()`` and both
fire on the same proxy hot path (sequential stages 4 and 4b in
``manager.call_tool``); aggregating both keeps the counter aligned
with the total STM-initiated LTM-write rate rather than a single
sub-path.

Mirrors ``surfacing/observability.py`` in spirit and shape for library
callers, but **deliberately omits a quality dimension**.
SURFACE has a quality signal (``surfacing-feedback``) that lets
operators ask "did the agent find this useful?". INDEX has no
equivalent. The absence is a design finding, not an oversight: a
quality dimension for INDEX would have to choose between two
non-equivalent observables — positive-value (does an extracted fact
later get retrieved on a related query?) and harm-prevented (what
fraction of writes would have leaked sensitive content absent
redaction?). Bundling them in one signal would conflate two questions;
deferring lets a future quality-signal PR scope to one or both
deliberately. Until then, the schema-level asymmetry self-documents
the architectural state.

State is per-process in-memory only. Counters reset on restart,
mirroring ``SurfacingObservability``. The cross-call aggregate is the
load-bearing addition over ``ExtractOutcome.facts_stored`` and
``AutoIndexOutcome.chunks_indexed``, which are per-call only and
discard as soon as the caller drops the return value.

Snapshot output is a flat dict so library callers can consume it without
coupling to internal data structures. The bundled server exposes no MCP tool
for these counters.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Literal

# Per-fact OR per-call outcome labels (mixed granularity by design):
# - ``stored`` / ``dedup_skip``: per fact processed inside one call (extract
#   path); per call (auto_index path — one chunk per response).
# - ``extracted_zero_facts``: per call, terminal — extractor returned ``[]``
#   so the loop body never ran. Only the extract path produces this label;
#   auto_index has no extraction step that can return empty.
# - ``error``: per error event. Outer ``extractor.extract`` failure counts
#   once (per call); per-fact ``index_file`` failure counts per fact;
#   auto_index ``index_file`` failure counts once per call.
# - ``privacy_skip``: per call, terminal — the content matched a privacy
#   pattern before anything was written (#453), so no file and no
#   ``index_file`` call exist for this attempt. Fires from both paths:
#   auto_index scans the exact markdown it would persist; extract scans
#   the response text + rendered arguments before the extractor runs.
#
# Both write paths share this 5-label outcome set deliberately. auto_index
# only fires ``stored``, ``error``, and ``privacy_skip``; ``dedup_skip`` and
# ``extracted_zero_facts`` are extract-specific. Operators read the raw
# mem_add count as ``outcomes["__total__"]["stored"]`` regardless of
# which path produced the write — path attribution lives in ``attempts``.
#
# Math contract for ``snapshot()``:
#   sum(outcomes["__total__"][k] for k in ALL) is **not** equal to
#   sum(attempts["__total__"][p] for p in PATHS) because per-fact outcomes
#   can multi-count one attempt. Useful operator ratios:
#   - ``outcomes["__total__"]["stored"]`` = literal mem_add count into LTM
#     per process — the raw write-volume signal across both paths and all
#     tools. Callers compute per-turn or per-second rates at the read site.
#   - ``outcomes["__total__"]["extracted_zero_facts"] /
#     attempts["__total__"]["extract"]`` = fraction of *extract* calls
#     producing zero LTM writes — direct measurement of "extraction fired
#     on a tool call but found nothing worth storing," architecturally
#     distinct from "facts existed but were duplicates" (``dedup_skip``)
#     and from "facts existed and were stored" (``stored``). Denominator
#     is the extract path's attempt count, not the grand total — auto_index
#     calls don't have an extraction step that could return empty.
IndexOutcome = Literal[
    "stored",
    "dedup_skip",
    "extracted_zero_facts",
    "privacy_skip",
    "error",
]

# Which write path drove a given ``record_attempt`` call. ``extract`` is
# the LLM-based fact extraction path (``extract_and_store``); ``auto_index``
# is the verbatim response → markdown chunk path (``auto_index_response``).
# A single proxy ``call_tool`` invocation can record one attempt of each
# (sequential stages 4 and 4b in ``manager.call_tool``).
AttemptPath = Literal["extract", "auto_index"]

_TOTAL_KEY = "__total__"


class IndexObservability:
    """Aggregate per-tool counters for the INDEX (extract_and_store) pipeline.

    Thread-safe via a single coarse lock — same trade-off as
    ``SurfacingObservability``: O(1) dict writes, contention dwarfed by
    the LTM round-trip the counters observe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Per-tool, per-path attempt counts — ``{tool: {path: int}}``.
        # ``__total__`` aggregates across tools (still per path).
        self._attempts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._outcomes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._any_call = False

    def record_attempt(self, tool: str, path: AttemptPath) -> None:
        """Record one INDEX-pipeline invocation. Per-call.

        ``path`` distinguishes the two LTM-write code paths: ``extract``
        for ``extract_and_store`` (LLM-based fact extraction), ``auto_index``
        for ``auto_index_response`` (verbatim chunk indexing).
        Increments per-tool / per-path and the ``__total__`` aggregate.
        """
        with self._lock:
            self._any_call = True
            self._attempts[tool][path] += 1
            self._attempts[_TOTAL_KEY][path] += 1

    def record_outcome(self, tool: str, outcome: IndexOutcome) -> None:
        """Record one outcome event (per-fact or per-call — see module docstring)."""
        with self._lock:
            self._any_call = True
            self._outcomes[tool][outcome] += 1
            self._outcomes[_TOTAL_KEY][outcome] += 1

    def snapshot(self) -> dict:
        """Return a deep-copied point-in-time view of all counters.

        Empty (``any_call=False``) when neither ``extract_and_store`` nor
        ``auto_index_response`` has been invoked, allowing library callers to
        distinguish zero traffic from recorded attempts.
        """
        with self._lock:
            return {
                "any_call": self._any_call,
                "attempts": {tool: dict(per_path) for tool, per_path in self._attempts.items()},
                "outcomes": {tool: dict(o) for tool, o in self._outcomes.items()},
            }


class _NoOpIndexObservability:
    """No-op stand-in used when ``extract_and_store`` or
    ``auto_index_response`` is called without an explicit observability
    instance. Lets the recording call sites stay unconditional
    (``observability.record_attempt(...)``) instead of guarding every
    site — same pattern as ``surfacing/observability._NoOpObservability``.

    ``__slots__ = ()`` mirrors the surfacing no-op for the same reason:
    these are module-level singletons used purely as recording sinks, so
    instance dict bloat is unnecessary.

    ``snapshot()`` is intentionally absent. Library consumers short-circuit
    on ``ProxyManager.index_observability is None`` rather than calling
    through the no-op.
    """

    __slots__ = ()

    def record_attempt(self, tool: str, path: AttemptPath) -> None:
        return None

    def record_outcome(self, tool: str, outcome: IndexOutcome) -> None:
        return None


_NOOP_INDEX_OBSERVABILITY: _NoOpIndexObservability = _NoOpIndexObservability()
