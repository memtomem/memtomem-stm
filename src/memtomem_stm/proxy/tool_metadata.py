"""Tool metadata helpers — description truncation, schema distilling, convention hints."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from memtomem_stm.proxy.config import CompressionStrategy, HybridConfig, TailMode

#: Prepended to every advertised description at registration
#: (``_fastmcp_compat.register_proxy_tool``). Defined here, where descriptions
#: are composed, so the budgeting in ``manager.get_proxy_tools`` reserves
#: exactly what registration will add — the cap is on what the client sees
#: (#893).
PROXIED_PREFIX = "[proxied] "

_ELLIPSIS = "..."

RECOVERY_SUFFIX = " | full: stm_proxy_describe_tool"

#: Sentence terminators, each followed by the whitespace that ends the
#: sentence. The latest match across all of them wins, so the cut is chosen by
#: position rather than by the order this tuple happens to list (#1016).
_SENTENCE_SEPARATORS = (". ", ".\n", "! ", "? ")

#: Smallest share of the budget, in percent, that a sentence-boundary cut must
#: retain to be used (#1016).
#:
#: That branch appends nothing, so its result reads as a finished description:
#: neither the model nor the operator can tell text was dropped. It is worth
#: that only when it costs the budget almost nothing. A boundary further back
#: falls through to the word-boundary branch below, which spends three
#: characters on an ellipsis and so announces the cut -- and, for text whose
#: tail carries no sentence separator at all (a bullet list, a code block),
#: keeps considerably more of the budget as well.
_SENTENCE_MIN_RETAINED_PERCENT = 90

#: The word-boundary branch accepts a much earlier cut, keeping the pre-#1016
#: rule: it is already marked with an ellipsis, so retreating costs budget but
#: cannot be mistaken for complete text. Named here so the two thresholds read
#: as two different rules rather than one literal repeated twice.
_WORD_BOUNDARY_EARLIEST_DIVISOR = 3


def truncate_description(desc: str, max_chars: int) -> str:
    """Truncate description at sentence boundary within budget.

    For any non-negative ``max_chars`` the result never exceeds it: the
    ellipsis is spent from the budget rather than added on top of it. Below
    ``len(_ELLIPSIS) + 1`` there is no room for both, so the result is a hard
    slice with no ellipsis. A negative budget yields the empty string, the
    closest the contract can come to a length it cannot represent.

    The sentence boundary is the latest one in the budget, and it is used only
    when it retains at least ``_SENTENCE_MIN_RETAINED_PERCENT`` of that budget.
    Anything further back falls through to the marked branches, so a cut that
    discards a large part of the budget always shows that it did (#1016).

    A cut still cannot say how much text lies *beyond* the budget: a sentence
    cut inside the retention floor is unmarked, and reads as finished. That is
    the open half of #1016 and is deliberately unchanged here.
    """
    if not desc or len(desc) <= max_chars:
        return desc
    if max_chars < len(_ELLIPSIS) + 1:
        return desc[: max(max_chars, 0)]
    # Try to cut at the last sentence boundary. This branch appends nothing, so
    # it may spend the whole budget -- and so it must not retreat far.
    truncated = desc[:max_chars]
    # Rounded up, so the floor is never weaker than the percentage says.
    min_retained = (max_chars * _SENTENCE_MIN_RETAINED_PERCENT + 99) // 100
    idx = max(truncated.rfind(sep) for sep in _SENTENCE_SEPARATORS)
    if idx + 1 >= min_retained:
        return truncated[: idx + 1].rstrip()
    # The remaining branches append an ellipsis, so they cut short of the cap.
    body = desc[: max_chars - len(_ELLIPSIS)]
    idx = body.rfind(" ")
    if idx > max_chars // _WORD_BOUNDARY_EARLIEST_DIVISOR:
        return body[:idx] + _ELLIPSIS
    return body + _ELLIPSIS


def distill_schema(schema: dict, strip_descriptions: bool) -> dict:
    """Remove description/examples from schema properties to save tokens."""
    if not strip_descriptions or not isinstance(schema, dict):
        return schema
    result: dict[str, Any] = {}
    for k, v in schema.items():
        if k in ("description", "examples"):
            continue
        if isinstance(v, dict):
            result[k] = distill_schema(v, strip_descriptions)
        elif isinstance(v, list):
            result[k] = [
                distill_schema(item, True) if isinstance(item, dict) else item for item in v
            ]
        else:
            result[k] = v
    return result


def convention_suffix(
    compression: CompressionStrategy,
    hybrid_cfg: HybridConfig | None,
) -> str:
    """Return a convention hint for strategies that change agent interaction.

    Returns empty string for strategies that produce standard text responses.
    """
    if compression == CompressionStrategy.SELECTIVE:
        return " | TOC response: use stm_proxy_select_chunks"
    if compression == CompressionStrategy.PROGRESSIVE:
        return " | Chunked: use stm_proxy_read_more for more"
    if compression == CompressionStrategy.HYBRID:
        cfg = hybrid_cfg or HybridConfig()
        if cfg.tail_mode == TailMode.TOC:
            return " | Head+TOC: use stm_proxy_select_chunks"
    return ""


@dataclass(frozen=True)
class DescriptionBudget:
    """How one advertised description spends ``max_description_chars``.

    The cap is on what the CLIENT sees, so every fixed cost comes out of it:
    the ``[proxied] `` prefix registration prepends later, and the convention
    suffix. The configured limits compose as ``min(server, global, host)`` (omit an
    unknown host cap) rather than as an
    override, which is why an operator can raise the global one and change
    nothing (#1015). Holding that arithmetic here keeps the advertisement
    (``manager.get_proxy_tools``) and the ``mms doctor`` advisory on one rule
    with two readers, the way #926 requires of the strategy resolution feeding
    ``suffix``.
    """

    server_cap: int
    global_cap: int
    #: ``convention_suffix`` output for the resolved strategy; ``""`` when the
    #: strategy needs no hint.
    suffix: str = ""
    host_cap: int | None = None
    source_chars: int | None = None
    schema_removed: bool = False

    def for_source(self, chars: int, *, schema_removed: bool = False) -> DescriptionBudget:
        """Pin recovery budgeting from source length; diagnostics need no raw text."""
        return replace(self, source_chars=chars, schema_removed=schema_removed)

    @property
    def recovery_required(self) -> bool:
        return self.schema_removed or (
            self.source_chars is not None and self.source_chars > self._body_without_recovery
        )

    @property
    def recovery_fits(self) -> bool:
        # ``<=``, not ``<``: a budget with room for the hint and nothing else
        # still advertises something ACTIONABLE -- the tool name the client
        # already has, plus where to read the rest -- whereas the same budget
        # spent on source text advertises an unrecoverable fragment.
        #
        # Zero body WITH this hint carried lands at ``len(PROXIED_PREFIX) +
        # len(the suffix that fits) + len(RECOVERY_SUFFIX)``, which is two caps
        # for a strategy that HAS a convention suffix: 42, where that suffix is
        # too long and is dropped, and 86 (44-char selective/progressive) or 82
        # (40-char hybrid TOC) where it is carried too. A strategy with no
        # suffix has only the 42.
        #
        # Distinct from all of those is zero body with the convention suffix
        # alone, at ``len(PROXIED_PREFIX) + len(suffix)`` -- 54, 54, 50 -- where
        # this property is False because the recovery hint does not fit.
        #
        # Measured by sweeping every cap from MIN_DESCRIPTION_CHARS per
        # strategy. A sweep that fixes the suffix at "" sees only the 42
        # (codex R1); one that tests ``body == 0`` without also reading
        # ``recovery_fits`` folds the convention-only caps in (codex R2).
        return self.recovery_required and len(RECOVERY_SUFFIX) <= self._body_without_recovery

    @property
    def combined_suffix(self) -> str:
        return (self.suffix if self.suffix_fits else "") + (
            RECOVERY_SUFFIX if self.recovery_fits else ""
        )

    @property
    def _body_without_recovery(self) -> int:
        return self.total - len(self.suffix) if self.suffix_fits else self.total

    @property
    def cap(self) -> int:
        """The effective cap: neither level overrides the other."""
        return (
            min(self.server_cap, self.global_cap, self.host_cap)
            if self.host_cap is not None
            else min(self.server_cap, self.global_cap)
        )

    @property
    def binding(self) -> str:
        """Binding level: ``server``, ``global``, ``both``, or ``host``.

        A binding host limit takes precedence, including ties: STM cannot
        raise it. Otherwise ``both`` on a tie, because raising either alone leaves the cap
        where it is. That is the case an operator most often misreads.
        """
        if self.host_cap is not None and self.host_cap <= min(self.server_cap, self.global_cap):
            return "host"
        if self.server_cap < self.global_cap:
            return "server"
        if self.global_cap < self.server_cap:
            return "global"
        return "both"

    @property
    def total(self) -> int:
        """Chars left for the composed text once the prefix is reserved."""
        return self.cap - len(PROXIED_PREFIX)

    @property
    def suffix_fits(self) -> bool:
        """Whether a configured suffix can be carried whole.

        A suffix that cannot fit is dropped rather than cut: a cut hint is
        incomplete, and depending on where it lands may not name its tool at
        all (#893).
        """
        return bool(self.suffix) and len(self.suffix) <= self.total

    @property
    def body(self) -> int:
        """Chars left for upstream text after a suffix that fits took its own."""
        return self._body_without_recovery - (len(RECOVERY_SUFFIX) if self.recovery_fits else 0)

    def overflow(self, source_chars: int) -> int:
        """Chars of a ``source_chars``-long source this budget discards."""
        return max(0, source_chars - self.body)

    def cap_to_fit(self, source_chars: int) -> int:
        """Smallest cap that would advertise that source whole, suffix included.

        Written from the configured suffix rather than from the one that fits,
        so the answer stays true for a cap too small to carry it today.
        """
        return (
            source_chars
            + len(PROXIED_PREFIX)
            + len(self.suffix)
            + (len(RECOVERY_SUFFIX) if self.schema_removed else 0)
        )


@dataclass(frozen=True)
class ComposedDescription:
    """One advertised description, plus what composing it had to give up."""

    text: str
    #: Source chars the cap discarded. An ellipsis counts as shown text, so
    #: this is the loss the client sees, not the loss the budget caused.
    chars_cut: int
    #: A configured suffix was dropped whole for want of room.
    suffix_dropped: bool
    # No ``recovery_dropped`` twin: unlike the configured suffix, whether the
    # recovery hint was dropped is answerable from the BUDGET alone
    # (``recovery_required and not recovery_fits``), which is the form doctor
    # reads -- it never has the source text to compose. A second copy here
    # would be a rule with two readers and no second caller (#926).


def hint_text(suffix: str) -> str:
    """One convention hint as a standalone value, without the joining separator.

    ``convention_suffix`` returns text shaped to be APPENDED to a description,
    so it opens with the ``" | "`` separator that divides it from the body. A
    field carrying the hint on its OWN -- the recovery tool's
    ``response_hint`` -- must not inherit that separator, or the client reads a
    dangling ``|`` as the first character of the instruction (#1014).
    """
    return suffix.strip().removeprefix("|").strip()


def advertised_source_text(upstream: str | None, override: str | None, prefixed_name: str) -> str:
    """Resolve the text an advertisement budgets, before any cap applies.

    An override replaces upstream text outright; both are stripped, so
    whitespace-only counts as none. An upstream (or override) that supplies
    none falls back to the prefixed name -- the only text the proxy holds that
    says something true about the tool without inventing a claim (#922).
    """
    desc = (upstream or "").strip()
    if override is not None:
        desc = override.strip()
    return desc or prefixed_name


def compose_description(source: str, budget: DescriptionBudget) -> ComposedDescription:
    """Compose the client-visible description and report what it cost.

    The suffix wins over upstream text whenever it fits, since it is what tells
    the client which follow-up tool to call (#893). Call ``budget.for_source``
    before composing to include recovery hints; the raw budget remains useful
    for configuration-only diagnostics. Recovery comes after compression hints.
    Truncation owns its own ellipsis, so the result never exceeds
    ``budget.total``.
    """
    body = truncate_description(source, budget.body)
    suffix = budget.combined_suffix
    text = body + suffix if body else suffix.lstrip()
    return ComposedDescription(
        text=text,
        chars_cut=len(source) - len(body),
        suffix_dropped=bool(budget.suffix) and not budget.suffix_fits,
    )


def tag_title(title: str | None, server_name: str) -> str | None:
    """Prepend ``[server_name]`` to one display title, or pass it through.

    The shared rule behind both title surfaces a client may render: the
    top-level ``Tool.title`` (MCP ``BaseMetadata``, forwarded since #895) and
    ``annotations.title``. A falsy title is returned unchanged, because a
    client with no title falls back to the already-prefixed ``name`` and
    manufacturing one here would invent a display string the upstream never
    supplied.

    Kept as one function so the two surfaces cannot drift apart, and so the
    credential scan can ask for exactly the text registration will send
    (:func:`memtomem_stm.proxy.tool_eligibility._flags_sensitive_metadata`).
    """
    if not title:
        return title
    return f"[{server_name}] {title}"


def tag_annotations_title(annotations: Any, server_name: str) -> Any:
    """Prepend ``[server_name]`` to ``annotations.title`` for picker disambiguation.

    MCP clients such as Claude Code's ``/mcp`` picker display ``annotations.title``
    in place of the tool ``name`` when it is set. Upstream servers that populate
    ``title`` (e.g. Playwright's "Close browser") then appear unattributed in the
    picker, while servers that leave it blank fall back to the prefixed ``name``
    (e.g. "Context7__resolve-library-id"). Tagging the title with the source
    server restores a uniform ``[server] original title`` display without
    touching the invocation ``name`` or input schema.

    Returns the original annotations unchanged when:
    - ``annotations`` is ``None`` (clients fall back to the prefixed ``name``),
    - ``title`` is missing or empty (same fallback path),
    - the object is not a pydantic model with ``model_copy`` (unknown shape).
    """
    if annotations is None:
        return None
    title = getattr(annotations, "title", None)
    if not title:
        return annotations
    new_title = tag_title(title, server_name)
    model_copy = getattr(annotations, "model_copy", None)
    if callable(model_copy):
        try:
            return model_copy(update={"title": new_title})
        except Exception:
            return annotations
    return annotations
