"""Format surfaced memories for injection into tool responses."""

from __future__ import annotations

from typing import Any

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.feedback import VALID_RATINGS

# Header for the optional scratch / working-memory section. Shared between the
# render site and the truncation orphan-trim so the two cannot drift.
_WORKING_MEMORY_HEADER = "**Working Memory:**"


class SurfacingFormatter:
    """Inject surfaced memories into a tool response."""

    def __init__(self, config: SurfacingConfig) -> None:
        self._config = config

    def _relevance_bucket(self, score: float, score_floor: float | None = None) -> str:
        floor = self._config.min_score if score_floor is None else score_floor
        if floor >= 1.0:
            return "strong"

        band = 1.0 - floor
        related_start = floor + band / 3
        strong_start = floor + 2 * band / 3

        if score < related_start:
            return "weak"
        if score < strong_start:
            return "related"
        return "strong"

    @staticmethod
    def _format_source(source_file: Any) -> str:
        parts = getattr(source_file, "parts", None)
        if not parts:
            return str(source_file)

        anchor = getattr(source_file, "anchor", "")
        path_parts = [part for part in parts if part and part != anchor]
        if not path_parts:
            return str(source_file)
        return "/".join(path_parts[-2:])

    def _format_namespace_badge(self, namespace: str | None) -> str:
        if not namespace or namespace == self._config.default_namespace:
            return ""
        return f" [{namespace}]"

    def inject(
        self,
        response_text: str,
        results: list[Any],
        query: str,
        surfacing_id: str | None = None,
        scratch_items: list[dict] | None = None,
        score_floor: float | None = None,
    ) -> str:
        """Inject surfaced memories into ``response_text``.

        When ``response_text`` is a progressive first-chunk, only the
        ``append`` and ``section`` modes preserve the
        ``PROGRESSIVE_FOOTER_TOKEN`` concat invariant relied on by
        ``stm_proxy_read_more``; ``prepend`` would shift offsets and is
        therefore skipped by ``ProxyManager`` on the progressive path.
        """
        if not results and not scratch_items:
            return response_text

        # #350: surfacing_id + rating spec live above the bullet list so they
        # survive ``effective_max_injection_chars`` truncation. The previous
        # trailing ``_Surfacing ID: ...`` line was cut on the largest, most
        # expensive surfacings — exactly the cases where feedback matters
        # most. The rating values come from ``feedback.VALID_RATINGS`` (single
        # source of truth) so the agent-visible enumeration cannot drift from
        # the server-side validator.
        lines: list[str] = [self._config.section_header]
        if surfacing_id:
            # The rendered callable must be copy-pasteable as a single valid
            # call: ``rating="helpful" | "not_relevant" | "already_known"``
            # parses as ``BinOp(BitOr)`` and the validator rejects the
            # resulting non-string, so we keep one literal value in the
            # argument and list the alternatives in prose outside the call.
            # The example uses ``VALID_RATINGS[0]`` ("helpful") to pin the
            # canonical-order contract.
            options_list = " | ".join(f'"{r}"' for r in VALID_RATINGS)
            example_rating = VALID_RATINGS[0]
            lines.append(f"_surfacing_id: {surfacing_id}_")
            lines.append(
                f"> Rate (one of {options_list}): "
                f"`stm_surfacing_feedback(surfacing_id={surfacing_id!r}, "
                f"rating={example_rating!r})`"
            )
            # EN-2/3: per-memory feedback. Every bullet below carries its
            # ``memory_id`` (the backticked token after the namespace badge);
            # the batched form rates them individually so ``not_relevant`` /
            # ``already_known`` invalidate exactly those memories on the next
            # cache hit instead of the whole event. The single-call line above
            # stays first so the AST-scraped rating example keeps exactly one
            # literal ``rating=`` (the batched call uses ``ratings=`` instead).
            lines.append(
                f"> Or rate specific memories: "
                f"`stm_surfacing_feedback(surfacing_id={surfacing_id!r}, "
                f'ratings=[{{"memory_id": "<id from a bullet below>", '
                f'"rating": "{example_rating}"}}])`'
            )
        lines.append("")
        # Everything appended above is the preamble (header + feedback spec);
        # it is pinned through truncation below. Bullets/scratch are the body.
        body_start = len(lines)

        for r in results:
            chunk = r.chunk
            meta = chunk.metadata
            ns_badge = self._format_namespace_badge(meta.namespace)
            source = self._format_source(meta.source_file) if meta.source_file else ""

            ctx = getattr(r, "context", None)
            preview_cap = self._config.preview_max_chars
            # Hit-first budgeting: the matched chunk is always rendered (up to
            # the cap); ±150-char window snippets fill whatever budget remains.
            # A naive front-slice of the joined preview can drop the chunk
            # entirely when window_before is large.
            preview = chunk.content[:preview_cap].replace("\n", " ")
            if ctx and ctx.window_before:
                budget = min(150, preview_cap - len(preview) - len(" | ") - len("..."))
                if budget > 0:
                    snippet = ctx.window_before[-1].content[-budget:].replace("\n", " ")
                    preview = "..." + snippet + " | " + preview
            if ctx and ctx.window_after:
                budget = min(150, preview_cap - len(preview) - len(" | ") - len("..."))
                if budget > 0:
                    snippet = ctx.window_after[0].content[:budget].replace("\n", " ")
                    preview = preview + " | " + snippet + "..."

            bucket = self._relevance_bucket(float(r.score), score_floor)
            # The backticked ``chunk.id`` is the agent-copyable ``memory_id``
            # for ``stm_surfacing_feedback(ratings=...)`` (EN-2/3). It sits
            # before the ``[bucket]: `` marker so the preview parse (and the
            # ``preview_max_chars`` cap) is unaffected, and it matches the
            # ``str(r.chunk.id)`` key the engine invalidation filters on.
            # Rendered only when present — every production chunk carries an id
            # (a content surrogate under compact, the real chunk_id under
            # structured), but a chunk without one degrades to the id-less
            # bullet rather than crashing the whole injection.
            cid = getattr(chunk, "id", None)
            id_token = f" `{cid}`" if cid else ""
            lines.append(f"- **{source}**{ns_badge}{id_token} [{bucket}]: {preview}")

        if scratch_items:
            lines.append("")
            lines.append(_WORKING_MEMORY_HEADER)
            for item in scratch_items[:3]:
                key = item.get("key", "")
                value = str(item.get("value", ""))[:200].replace("\n", " ")
                lines.append(f"- `{key}`: {value}")

        memory_block = "\n".join(lines)

        # Enforce injection size limit to prevent context bloat. The preamble
        # (``lines[:body_start]`` — header + surfacing_id + rating spec) is
        # pinned so the feedback loop survives truncation on the largest, most
        # expensive surfacings (#350); the body (bullets + scratch) is dropped
        # on whole-line boundaries so a per-memory ``memory_id`` token is never
        # severed mid-string — a half-copied id silently no-ops batched
        # feedback invalidation (EN-2/3). A flat ``[:max_chars]`` slice broke
        # both. The preamble may overrun ``max_chars`` by a bounded amount,
        # which only happens at the tiny caps tests use; production caps
        # (default 3000) dwarf the ~300-char preamble.
        max_chars = self._config.effective_max_injection_chars()
        if max_chars and len(memory_block) > max_chars:
            marker = "\n... (memory block truncated)"
            kept = lines[:body_start]
            used = len("\n".join(kept))
            for line in lines[body_start:]:
                # +1 accounts for the newline that joins this line on.
                if used + len(line) + len(marker) + 1 > max_chars:
                    break
                kept.append(line)
                used += len(line) + 1
            # A whole-line drop can stop right after the working-memory header
            # (or a section's blank separator), leaving an orphan header with no
            # items beneath it. Trim trailing structural-only lines so the
            # truncated block stays well-formed.
            while len(kept) > body_start and kept[-1] in ("", _WORKING_MEMORY_HEADER):
                kept.pop()
            memory_block = "\n".join(kept) + marker

        match self._config.injection_mode:
            case "prepend":
                return (
                    f"<surfaced-memories>\n{memory_block}\n</surfaced-memories>\n\n{response_text}"
                )
            case "append" | "section" | _:
                return (
                    f"{response_text}\n\n<surfaced-memories>\n{memory_block}\n</surfaced-memories>"
                )
