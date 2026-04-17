"""Progressive fallback round-trip helpers for bench_qa.

When ``ProxyManager.call_tool()`` trips the ratio guard into Tier-1
progressive delivery, the first chunk carries a footer with a
``stm_proxy_read_more`` pointer. bench_qa reproduces what an agent would
do — follow that pointer until ``has_more=False`` — and asserts that the
concatenated content is byte-identical to the cleaned payload
(PR #160/#165 invariant).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.progressive import PROGRESSIVE_FOOTER_TOKEN

_READ_MORE_POINTER = re.compile(r'stm_proxy_read_more\(key="([^"]+)",\s*offset=(\d+)\)')
_HAS_MORE_FALSE = "has_more=False"
_HAS_MORE_TRUE = "has_more=True"


@dataclass
class ReassemblyResult:
    """Outcome of reading the first chunk + every follow-up ``read_more``."""

    content: str
    chunks: int
    has_more_final: bool


def split_chunk(text: str) -> tuple[str, str]:
    """Return ``(content, footer)`` by splitting on ``PROGRESSIVE_FOOTER_TOKEN``.

    The token is the canonical agent-side contract — never split on bare
    ``\\n---\\n``, which naturally appears in markdown and YAML.
    """
    idx = text.find(PROGRESSIVE_FOOTER_TOKEN)
    if idx == -1:
        return text, ""
    return text[:idx], text[idx:]


def parse_pointer(footer: str) -> tuple[str, int]:
    """Extract ``(key, next_offset)`` from a progressive footer.

    Raises ``AssertionError`` if the pointer is missing — that indicates
    either the fallback did not run or the footer format drifted from
    ``ProgressiveChunker._build_footer``.
    """
    match = _READ_MORE_POINTER.search(footer)
    assert match, f"progressive footer missing read_more pointer: {footer!r}"
    return match.group(1), int(match.group(2))


def reassemble(mgr: ProxyManager, first_chunk: str, *, max_steps: int = 200) -> ReassemblyResult:
    """Follow ``stm_proxy_read_more`` calls until ``has_more=False``.

    ``max_steps`` is a hard ceiling — larger values would indicate an
    offset-arithmetic bug (e.g. zero-width chunks) rather than a legitimate
    payload, and infinite-looping here would hide the regression.
    """
    content, footer = split_chunk(first_chunk)
    if _HAS_MORE_FALSE in footer:
        return ReassemblyResult(content=content, chunks=1, has_more_final=False)

    assert _HAS_MORE_TRUE in footer, f"first chunk lacks both has_more markers: {footer!r}"
    key, offset = parse_pointer(footer)

    parts = [content]
    for step in range(max_steps):
        chunk = mgr.read_more(key, offset)
        piece, footer = split_chunk(chunk)
        if piece == "(no more content)" or chunk.startswith("(no more content)"):
            return ReassemblyResult(content="".join(parts), chunks=len(parts), has_more_final=False)
        parts.append(piece)
        offset += len(piece)
        if _HAS_MORE_FALSE in footer:
            return ReassemblyResult(content="".join(parts), chunks=len(parts), has_more_final=False)
    raise AssertionError(
        f"reassembly exceeded {max_steps} chunks — suspected offset-arithmetic bug"
    )
