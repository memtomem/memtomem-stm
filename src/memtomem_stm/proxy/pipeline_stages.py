"""Stage-result dataclasses for the proxy call pipeline.

Each stage of ``ProxyManager._call_tool_inner`` returns one of these small,
explicit carriers so the cross-stage contract is typed rather than threaded
through one long method's locals (the A1 refactor). These are pure data with no
``ProxyManager`` dependency — mirroring ``AutoIndexOutcome`` / ``ExtractOutcome``
in ``memory_ops`` and ``EligibilityResult`` in ``tool_eligibility``.

Frozen + slotted, with no defaults for branch-set fields: a code path that
forgets to set a field fails at construction (``TypeError``) instead of silently
carrying a stale value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from memtomem_stm.proxy.config import CompressionStrategy


@dataclass(frozen=True, slots=True)
class ShapePassthrough:
    """The stage-3 early-exit verdict — the text/non-text split produced no text.

    ``has_non_text`` True → the caller records the 0/0 passthrough metric and
    returns the non-text content list; False → the caller records the same 0/0
    metric (#558 — an empty response is still a live call, so it must appear in
    ``total_calls`` for the invocation reconciliation) and returns the
    ``"[empty response]"`` sentinel. The shaping helper never records metrics or
    returns itself, so the orchestrator stays the single owner of metric writes
    and return shapes.
    """

    has_non_text: bool


@dataclass(frozen=True, slots=True)
class ShapedResponse:
    """Stage 3 output: the text/non-text split of an upstream tool result.

    ``original_text`` is the joined text payload (``""`` when ``passthrough`` is
    set). ``non_text_content`` is the preserved non-text content list (consumed
    by the cache-store gate and the final return shape). When ``passthrough`` is
    non-None the caller short-circuits.

    ``non_text_before_first_text`` is the number of non-text blocks that
    preceded the FIRST text block in the upstream content array. The final
    return shape puts the (single, merged) processed text back at that
    position, so non-text blocks keep their relative order and a leading
    image/resource stays leading. 0 when there is no text (passthrough) or the
    content started with text.
    """

    original_text: str
    non_text_content: list[Any]
    passthrough: ShapePassthrough | None = None
    non_text_before_first_text: int = 0
    first_text_content: Any | None = None


@dataclass(frozen=True, slots=True)
class CompressionResult:
    """Stage-2+3 output: the compressed payload, its surfaced form, and the
    metrics/cache facts the orchestrator consumes.

    Carries BOTH ``compressed`` (pre-surfacing — the cache payload) and
    ``surfaced`` (post-surfacing — the return/index input) as distinct fields:
    the cache stores the former while the agent and the index footer see the
    latter. ``compressed_chars_for_metrics`` is branch-dependent —
    ``len(compressed)`` on the compress branch but ``len(cleaned)`` on the
    zero-loss progressive branch. ``metrics_strategy`` is the fully-mutated label
    (e.g. ``"truncate→progressive_fallback"``). ``progressive_passthrough_on_error``
    gates the cache store: a transient progressive-store-failure passthrough must
    not be cached.
    """

    compressed: str
    surfaced: str
    compressed_chars_for_metrics: int
    metrics_strategy: str
    ratio_violation: bool
    effective_compression: CompressionStrategy
    progressive_passthrough_on_error: bool
    surfacing_on_progressive_ok: bool | None
    surface_error: str | None
    compress_ms: float
    surface_ms: float
    # A SELECTIVE/HYBRID pending-store write failed and the call degraded to a
    # boundary-aware truncation. Gates the cache store like
    # ``progressive_passthrough_on_error`` — the truncation is lossy and
    # transient, and must not be pinned for the cache TTL. No default (the
    # module contract above): the compress branch always sets it explicitly.
    selective_store_error: bool
    # The opt-in unicode token gate evaluated THIS response (token budget set,
    # ``token_estimation_mode == "unicode"``, non-progressive branch). Keys the
    # metrics estimator switch in ``_call_tool_inner`` so recorded token counts
    # always reconcile with the gate decision — and never flip the progressive
    # branch's deliberate ``len(cleaned)`` accounting basis. No default: both
    # branches set it explicitly.
    unicode_token_gate: bool


@dataclass(frozen=True, slots=True)
class IndexResult:
    """Stage-4 (INDEX) output. ``final_result`` is the response body the caller
    returns/continues with — the ``[Indexing…] · scheduled`` footer on the
    background path, the indexed summary on the sync path, or ``surfaced`` when
    indexing is skipped/disabled. ``index_ok`` is tri-state: ``None`` records
    no outcome, ``True`` success, ``False`` non-success (failed, or shed
    before it could run — ``index_error`` says which).
    """

    final_result: str
    index_ok: bool | None
    index_error: str | None
    chunks_indexed: int


@dataclass(frozen=True, slots=True)
class ExtractResult:
    """Stage-4b (EXTRACT) output. ``ok`` / ``error`` are populated on the sync
    path, ``None`` for a scheduled background run (which never reports back
    here), and ``False`` / ``background_shed`` when the background run was
    refused (#868)."""

    ok: bool | None
    error: str | None
