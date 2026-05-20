"""Format surfaced memories for injection into tool responses."""

from __future__ import annotations

from typing import Any

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.feedback import VALID_RATINGS


class SurfacingFormatter:
    """Inject surfaced memories into a tool response."""

    def __init__(self, config: SurfacingConfig) -> None:
        self._config = config

    def inject(
        self,
        response_text: str,
        results: list[Any],
        query: str,
        surfacing_id: str | None = None,
        scratch_items: list[dict] | None = None,
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
        lines.append("")

        for r in results:
            chunk = r.chunk
            meta = chunk.metadata
            ns_badge = f" [{meta.namespace}]" if meta.namespace != "default" else ""
            source = str(meta.source_file.name) if meta.source_file else ""

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

            lines.append(f"- **{source}**{ns_badge} (score={r.score:.2f}): {preview}")

        if scratch_items:
            lines.append("")
            lines.append("**Working Memory:**")
            for item in scratch_items[:3]:
                key = item.get("key", "")
                value = str(item.get("value", ""))[:200].replace("\n", " ")
                lines.append(f"- `{key}`: {value}")

        memory_block = "\n".join(lines)

        # Enforce injection size limit to prevent context bloat
        max_chars = self._config.effective_max_injection_chars()
        if max_chars and len(memory_block) > max_chars:
            memory_block = memory_block[:max_chars] + "\n... (memory block truncated)"

        match self._config.injection_mode:
            case "prepend":
                return (
                    f"<surfaced-memories>\n{memory_block}\n</surfaced-memories>\n\n{response_text}"
                )
            case "append" | "section" | _:
                return (
                    f"{response_text}\n\n<surfaced-memories>\n{memory_block}\n</surfaced-memories>"
                )
