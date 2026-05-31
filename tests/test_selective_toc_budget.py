"""Budget-aware SELECTIVE TOC: the standalone path honors ``max_chars``.

``_store_and_build_toc`` historically built one entry per section with a fixed
80-char preview and ignored ``max_chars`` entirely, so a many-section document
produced a TOC far larger than the requested budget (a 50-section doc at
``max_chars=300`` emitted ~9.5k chars, flat across every budget). Only the
HybridCompressor layer guarded the budget; the STANDALONE SELECTIVE path forwarded
the oversized TOC unclamped.

The fix shrinks ONLY the per-entry preview (uniformly, via a budget-filling
search) while keeping every entry — the two-phase ``select()`` protocol needs the
full index, and the manager exempts SELECTIVE from the ratio guard for exactly
this reason. At extreme section counts even a zero-preview TOC may exceed the
budget; that is accepted by design (entries are never dropped).
"""

from __future__ import annotations

import json

from memtomem_stm.proxy.compression import SelectiveCompressor


def _doc(n_sections: int, body: int = 4) -> str:
    return "\n\n".join(
        f"## Section number {i} heading\n\n" + ("lorem ipsum dolor sit amet " * body)
        for i in range(n_sections)
    )


def test_many_section_toc_honors_budget() -> None:
    """A 50-section doc honors the budget by shrinking previews while still
    listing every section — the standalone-path overflow the fix targets.

    The budgets below sit inside the window where shrinking actually applies:
    above the structural floor (~5.3k — the zero-preview, 50-entry key/type/size
    scaffold, never dropped because the two-phase index needs every key) and
    below the raw doc length (~7k — at/above which ``compress`` passes the raw
    text through and never builds a TOC). The sub-floor regime (even empty
    previews overflow) is the documented trade-off pinned by
    ``test_tight_budget_keeps_entries_over_strict_fit``.
    """
    doc = _doc(50)
    for budget in (6900, 6500, 6000, 5500):
        out = SelectiveCompressor().compress(doc, max_chars=budget)
        parsed = json.loads(out)  # valid JSON
        assert len(out) <= budget, f"@{budget}: produced {len(out)} (+{len(out) - budget})"
        assert len(parsed["entries"]) == 50, "every section must stay listed (2-phase index)"


def test_toc_length_is_monotonic_in_budget() -> None:
    """A larger budget never yields a shorter TOC (the preview cap only grows)."""
    doc = _doc(50)
    prev = None
    for budget in range(120, 2001, 20):
        out = SelectiveCompressor().compress(doc, max_chars=budget)
        if prev is not None:
            assert len(out) >= prev, f"@{budget}: {len(out)} < prev {prev}"
        prev = len(out)


def test_select_resolves_section_omitted_from_preview() -> None:
    """Shrinking previews must not affect retrieval: every section is still
    addressable by key via select(), even at a tight budget."""
    doc = _doc(50)
    sc = SelectiveCompressor()
    toc = json.loads(sc.compress(doc, max_chars=200))
    resolved = sc.select(toc["selection_key"], ["Section number 49 heading"])
    assert "lorem ipsum" in resolved


def test_preview_unchanged_when_full_toc_fits() -> None:
    """When the full 80-char-preview TOC already fits the budget, previews are
    NOT shrunk — backward-compatible output for the common case.

    The doc must exceed the budget (else ``compress`` passes the raw text
    through untouched) while the full-preview TOC fits under it, so the sections
    carry long bodies: doc ~3.3k, full TOC ~0.8k, budget 2k sits between.
    """
    doc = _doc(3, body=40)
    out = SelectiveCompressor().compress(doc, max_chars=2000)
    parsed = json.loads(out)
    # A non-inline section keeps the full 80-char preview (no shrink applied).
    long_entries = [e for e in parsed["entries"] if not e["inline"]]
    assert long_entries, "fixture should have at least one long section"
    assert any(len(e["preview"]) == 80 for e in long_entries)


def test_tight_budget_keeps_entries_over_strict_fit() -> None:
    """At a budget too small even for zero-preview entries, the TOC keeps every
    entry (valid JSON) rather than dropping sections — the documented trade-off
    that preserves the two-phase protocol."""
    doc = _doc(50)
    out = SelectiveCompressor().compress(doc, max_chars=40)
    parsed = json.loads(out)  # still valid JSON
    assert len(parsed["entries"]) == 50
