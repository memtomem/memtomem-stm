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

    ``has_non_text`` True → the caller records the non-text passthrough metric and
    returns the non-text content list; False → the caller returns the
    ``"[empty response]"`` sentinel and records nothing. The shaping helper never
    records metrics or returns itself, so the orchestrator stays the single owner
    of metric writes and return shapes.
    """

    has_non_text: bool


@dataclass(frozen=True, slots=True)
class ShapedResponse:
    """Stage 3 output: the text/non-text split of an upstream tool result.

    ``original_text`` is the joined text payload (``""`` when ``passthrough`` is
    set). ``non_text_content`` is the preserved non-text content list (consumed
    by the cache-store gate and the final return shape). When ``passthrough`` is
    non-None the caller short-circuits.
    """

    original_text: str
    non_text_content: list[Any]
    passthrough: ShapePassthrough | None = None


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
