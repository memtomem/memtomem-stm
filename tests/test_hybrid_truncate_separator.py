"""HybridCompressor truncate-tail separator must not double the truncation marker.

In ``tail_mode=truncate`` the tail is run through TruncateCompressor, whose suffix
already reports ``... (truncated, original: N chars)``. The hybrid separator used
to ALSO say ``Remaining content (N chars, truncated):``, so the same char count
and the word "truncated" appeared twice in the wire payload. The separator is now
terse (``Remaining content:``); the inner suffix remains the single source of the
truncation signal. The default TOC mode is unaffected.
"""

from __future__ import annotations

from memtomem_stm.proxy.compression import HybridCompressor
from memtomem_stm.proxy.config import TailMode

# Plain prose tail (no headings/code/lists) so TruncateCompressor takes its
# position-based fallback and appends the ``... (truncated, original: N chars)``
# suffix — the marker the separator must not duplicate.
_TEXT = (
    "# Intro\n\n"
    + "Important head context here. " * 8
    + ("\n\nTail prose sentence number that keeps going on and on. " * 200)
)


def test_truncate_tail_emits_single_truncation_marker() -> None:
    c = HybridCompressor(head_chars=100, tail_mode=TailMode.TRUNCATE)
    result = c.compress(_TEXT, max_chars=600)

    # Boundary marker present, but terse — the old duplicated wording is gone.
    assert "Remaining content:" in result
    assert "chars, truncated" not in result, "separator must not repeat count+word"
    # The truncation is still signaled exactly once, by the inner suffix.
    assert "truncated, original:" in result
    assert result.count("truncated") == 1, "the word 'truncated' must appear once"


def test_toc_tail_separator_unchanged() -> None:
    # No-regression: the default TOC separator still carries the char count and
    # the Table-of-Contents label.
    c = HybridCompressor(head_chars=100, tail_mode=TailMode.TOC)
    result = c.compress(_TEXT, max_chars=600)
    assert "Table of Contents:" in result
    assert "Remaining content (" in result, "TOC separator keeps the (N chars) count"
