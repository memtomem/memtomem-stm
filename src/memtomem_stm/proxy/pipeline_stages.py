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
from typing import Any


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
