"""Tool metadata helpers — description truncation, schema distilling, convention hints."""

from __future__ import annotations

from typing import Any

from memtomem_stm.proxy.config import CompressionStrategy, HybridConfig, TailMode

#: Prepended to every advertised description at registration
#: (``_fastmcp_compat.register_proxy_tool``). Defined here, where descriptions
#: are composed, so the budgeting in ``manager.get_proxy_tools`` reserves
#: exactly what registration will add — the cap is on what the client sees
#: (#893).
PROXIED_PREFIX = "[proxied] "

_ELLIPSIS = "..."


def truncate_description(desc: str, max_chars: int) -> str:
    """Truncate description at sentence boundary within budget.

    For any non-negative ``max_chars`` the result never exceeds it: the
    ellipsis is spent from the budget rather than added on top of it. Below
    ``len(_ELLIPSIS) + 1`` there is no room for both, so the result is a hard
    slice with no ellipsis. A negative budget yields the empty string, the
    closest the contract can come to a length it cannot represent.
    """
    if not desc or len(desc) <= max_chars:
        return desc
    if max_chars < len(_ELLIPSIS) + 1:
        return desc[: max(max_chars, 0)]
    # Try to cut at last sentence boundary. This branch appends nothing, so it
    # may spend the whole budget.
    truncated = desc[:max_chars]
    for sep in (". ", ".\n", "! ", "? "):
        idx = truncated.rfind(sep)
        if idx > max_chars // 3:  # don't cut too early
            return truncated[: idx + 1].rstrip()
    # The remaining branches append an ellipsis, so they cut short of the cap.
    body = desc[: max_chars - len(_ELLIPSIS)]
    idx = body.rfind(" ")
    if idx > max_chars // 3:
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
