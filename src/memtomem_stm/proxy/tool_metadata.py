"""Tool metadata helpers — description truncation, schema distilling, convention hints."""

from __future__ import annotations

from typing import Any

from memtomem_stm.proxy.config import CompressionStrategy, HybridConfig, TailMode


def truncate_description(desc: str, max_chars: int) -> str:
    """Truncate description at sentence boundary within budget."""
    if not desc or len(desc) <= max_chars:
        return desc
    # Try to cut at last sentence boundary
    truncated = desc[:max_chars]
    for sep in (". ", ".\n", "! ", "? "):
        idx = truncated.rfind(sep)
        if idx > max_chars // 3:  # don't cut too early
            return truncated[: idx + 1].rstrip()
    # Fall back to word boundary
    idx = truncated.rfind(" ")
    if idx > max_chars // 3:
        return truncated[:idx] + "..."
    return truncated + "..."


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
