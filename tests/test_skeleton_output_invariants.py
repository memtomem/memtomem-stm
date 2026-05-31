"""Output-contract invariants for SkeletonCompressor's final tier.

Skeleton's whole contract is "preserves ALL headings": every section heading
survives so no part of the document is silently lost. But the per-section
content budget floors each section at 60 chars (``max(60, …)``), so with many
sections the assembled skeleton (headings + first body lines + footer) overflows
the budget. The pre-fix final tier handled that overflow with a raw
``result[:max_chars]`` slice, which (a) dropped trailing headings, (b) cut the
last heading mid-line, and (c) sliced away the ``(skeleton — …)`` footer — three
violations of the documented invariant. An 80-section doc at a routine 2000-char
budget already lost the footer and mid-cut a heading.

This file is the regression net. The final tier now degrades gracefully:

1. **Tier 2** — every heading fits beside the footer: keep all headings and
   refill body lines (relevance-weighted) up to the budget.
2. **Tier 3** — the bare heading lines overflow: keep the longest WHOLE-heading
   prefix and record the rest in a ``... (N of M sections omitted)`` marker.
3. **Floor** — a budget too small for even one heading + marker + footer: keep
   the longest whole-heading prefix (still no mid-cut). A hard slice happens
   only below the width of a single heading — a budget the retention ladder
   never produces.

Invariants asserted for every fixture × budget: ``len(out) <= max(0, budget)``;
no heading line is ever cut mid-way (above the single-heading floor); the footer
survives whenever it fits; and every dropped section is accounted for in the
marker. Byte-identity with the pre-query path is preserved in the degraded
tiers, mirroring ``tests/test_query_aware_structural.py``.
"""

from __future__ import annotations

import re

import pytest

from memtomem_stm.proxy.compression import BM25Scorer, SkeletonCompressor

_HEADING_RE = re.compile(r"^#{1,6}\s.+$", re.MULTILINE)


def _headings(text: str) -> list[str]:
    return _HEADING_RE.findall(text)


# ── Fixtures, one per tier ───────────────────────────────────────────────────


def _many_short_headings(n: int = 80) -> str:
    """Many sections with short headings + two short body lines each. The
    headings alone overflow a tight budget → Tier 3 / floor; a roomy budget →
    Tier 2. ``## Section {i} Title Here`` is >= 23 chars (the single-heading
    floor width used in the no-mid-cut tests)."""
    return "".join(
        f"## Section {i} Title Here\nbody line {i} content here.\nmore body for {i}.\n\n"
        for i in range(n)
    )


def _few_rich_sections() -> str:
    """Four sections, each with 20 body lines tagged by a unique term, so the
    per-section (relevance-weighted) budget governs how many lines survive."""
    sections = []
    for name, term in (
        ("Auth", "jwtauth"),
        ("Billing", "invoicepay"),
        ("Logging", "logrotate"),
        ("Metrics", "countergauge"),
    ):
        body = "\n".join(f"{term} detail line {j} explains the topic in depth" for j in range(20))
        sections.append(f"## {name}\n{body}\n")
    return "\n".join(sections) + "\n"


def _headings_only(n: int = 8) -> str:
    """Sections with empty bodies — the heading lines are the whole payload."""
    return "".join(f"## Heading number {i}\n\n" for i in range(n))


def _one_giant_heading() -> str:
    """A first heading wider than the small budgets, forcing the floor."""
    return "## " + "X" * 200 + "\nbody a\n\n## Short heading\nbody b\n"


_FIXTURES = {
    "many_short_headings": _many_short_headings(),
    "few_rich_sections": _few_rich_sections(),
    "headings_only": _headings_only(),
    "one_giant_heading": _one_giant_heading(),
}


# ── A. Universal output invariants ───────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(_FIXTURES))
@pytest.mark.parametrize("budget", [4000, 2000, 800, 500, 200, 80, 30, 10, 5, 2, 1, 0])
def test_output_never_exceeds_budget(name: str, budget: int) -> None:
    """Invariant A (the core regression): ``len(out) <= max(0, budget)``. Pre-fix
    the footer was appended *after* the body filled the budget, then the whole
    thing was hard-sliced — overshooting and corrupting the tail."""
    out = SkeletonCompressor().compress(_FIXTURES[name], max_chars=budget)
    assert len(out) <= max(0, budget), f"{name}@{budget}: {len(out)} chars"


@pytest.mark.parametrize("name", sorted(_FIXTURES))
@pytest.mark.parametrize("budget", [4000, 2000, 800, 500, 200, 80, 30])
def test_no_heading_is_cut_mid_line(name: str, budget: int) -> None:
    """Invariant B: every heading line emitted is a COMPLETE original heading —
    never a mid-line fragment like ``## Secti``. The one exception is a budget
    smaller than a single heading's width (``one_giant_heading``), where a hard
    slice is the documented last resort — guarded out below."""
    doc = _FIXTURES[name]
    original = set(_headings(doc))
    if budget < len(_headings(doc)[0]):
        pytest.skip("below the single-heading floor — hard slice is the last resort")
    out = SkeletonCompressor().compress(doc, max_chars=budget)
    for h in _headings(out):
        assert h in original, f"{name}@{budget}: mid-cut heading {h!r}"


# Only the large fixtures actually overflow these budgets; the tiny ones
# (headings_only, one_giant_heading) pass through unchanged with no footer.
_COMPRESSED_FIXTURES = ["few_rich_sections", "many_short_headings"]


@pytest.mark.parametrize("name", _COMPRESSED_FIXTURES)
@pytest.mark.parametrize("budget", [4000, 2000, 800, 500])
def test_footer_present_when_budget_is_roomy(name: str, budget: int) -> None:
    """Invariant C: the ``(skeleton — …)`` footer survives at every non-floor
    budget where the payload is actually compressed. Pre-fix the slice dropped
    it whenever the body overflowed."""
    out = SkeletonCompressor().compress(_FIXTURES[name], max_chars=budget)
    assert out != _FIXTURES[name]  # genuinely compressed, not passthrough
    assert "sections)" in out and "body_trimmed_chars" in out


# ── B. Every heading survives, or is accounted for in the marker ─────────────


@pytest.mark.parametrize("budget", [6000, 4000, 3000, 2200])
def test_all_headings_survive_when_they_fit(budget: int) -> None:
    """Tier 2 (and passthrough): once the bare heading lines + footer fit, every
    original heading is present verbatim and no marker is emitted."""
    doc = _many_short_headings(80)
    out = SkeletonCompressor().compress(doc, max_chars=budget)
    assert "sections omitted" not in out
    for h in _headings(doc):
        assert h in out, f"heading dropped at budget {budget}: {h!r}"


@pytest.mark.parametrize("budget", [1500, 1000, 700, 500, 300])
def test_dropped_sections_are_accounted_for_in_the_marker(budget: int) -> None:
    """Tier 3: kept headings + the marker's omitted-count reconcile to the total
    (no off-by-one), and the marker's total equals the footer's section count."""
    doc = _many_short_headings(80)
    out = SkeletonCompressor().compress(doc, max_chars=budget)
    m = re.search(r"\.\.\. \((\d+) of (\d+) sections omitted\)", out)
    assert m, f"expected an omission marker at budget {budget}"
    omitted, total = int(m.group(1)), int(m.group(2))
    kept = len(_headings(out))
    assert kept + omitted == total == 80
    assert omitted > 0 and kept > 0
    footer = re.search(r"(\d+) sections\)", out)
    assert footer and int(footer.group(1)) == total


def test_headings_are_an_order_preserving_prefix_in_tier3() -> None:
    """The kept headings are the leading prefix of the document, in order."""
    doc = _many_short_headings(80)
    out = SkeletonCompressor().compress(doc, max_chars=600)
    kept = _headings(out)
    assert kept == _headings(doc)[: len(kept)]


# ── C. Byte-identity with the pre-query path (load-bearing) ──────────────────


@pytest.mark.parametrize("budget", [2000, 800, 500, 200])
def test_no_query_is_byte_identical_in_degraded_path(budget: int) -> None:
    """``context_query=None`` and the no-query default take the same uniform
    budget branch, so the degraded output is byte-for-byte identical."""
    doc = _many_short_headings(80)
    c = SkeletonCompressor()
    base = c.compress(doc, max_chars=budget)
    assert c.compress(doc, max_chars=budget, context_query=None) == base


@pytest.mark.parametrize("budget", [2000, 800, 500])
def test_irrelevant_query_is_byte_identical_in_degraded_path(budget: int) -> None:
    """A query with no BM25 signal falls back to the uniform split → identical."""
    doc = _many_short_headings(80)
    c = SkeletonCompressor()
    base = c.compress(doc, max_chars=budget)
    assert c.compress(doc, max_chars=budget, context_query="zzzzz qqqqq") == base


def test_fits_case_is_unchanged() -> None:
    """When the assembled skeleton already fits, output is the pre-fix build
    verbatim (footer with the real body_trimmed count, no marker)."""
    doc = _few_rich_sections()
    out = SkeletonCompressor().compress(doc, max_chars=4000)
    assert len(out) <= 4000
    assert "sections omitted" not in out
    assert out.endswith("4 sections)")
    for h in _headings(doc):
        assert h in out


def test_passthrough_when_input_fits() -> None:
    """Input already within budget is returned verbatim, with no footer."""
    doc = "## A\nshort\n\n## B\nshort\n"
    assert SkeletonCompressor().compress(doc, max_chars=10_000) == doc


# ── D. Tier 3 packs whole headings without wasteful gaps ─────────────────────


@pytest.mark.parametrize("budget", [1000, 700, 500])
def test_tier3_packs_headings_without_waste(budget: int) -> None:
    """Tier 3 fits as many whole headings as the budget allows — it does not
    collapse far under budget (the old slice "filled" only by overshooting)."""
    out = SkeletonCompressor().compress(_many_short_headings(80), max_chars=budget)
    assert len(out) <= budget
    assert len(out) > 0.85 * budget, f"only {len(out)}/{budget} used"


# ── E. Floor / pathological budgets ──────────────────────────────────────────


@pytest.mark.parametrize("budget", [3, 2, 1, 0])
def test_floor_stays_within_budget(budget: int) -> None:
    out = SkeletonCompressor().compress(_many_short_headings(80), max_chars=budget)
    assert len(out) <= max(0, budget)


def test_negative_budget_yields_empty() -> None:
    """A negative budget must not slice from the end (Python ``s[:-5]``)."""
    assert SkeletonCompressor().compress(_many_short_headings(80), max_chars=-5) == ""


def test_floor_keeps_whole_headings_without_mid_cut() -> None:
    """At a budget too small for the marker/footer but large enough for some
    whole headings, those headings survive intact (no mid-cut, no footer)."""
    doc = _many_short_headings(80)
    out = SkeletonCompressor().compress(doc, max_chars=80)
    assert len(out) <= 80
    kept = _headings(out)
    assert kept and all(h in set(_headings(doc)) for h in kept)


def test_one_giant_heading_degrades_within_budget() -> None:
    out = SkeletonCompressor().compress(_one_giant_heading(), max_chars=50)
    assert len(out) <= 50  # no crash, budget held


def test_marker_survives_when_footer_does_not() -> None:
    """Floor A: a budget too small for the footer but big enough for the marker
    keeps the ``... (N of M sections omitted)`` signal (and the kept-heading
    count stays monotonic vs. the next-larger budget)."""
    doc = _many_short_headings(80)
    small = SkeletonCompressor().compress(doc, max_chars=120)
    larger = SkeletonCompressor().compress(doc, max_chars=200)
    assert "sections omitted" in small and "sections)" not in small  # marker, no footer
    assert len(small) <= 120
    # monotonic: a smaller budget never shows MORE headings than a larger one
    assert len(_headings(small)) <= len(_headings(larger))


# ── F. Negative scores survive the clamp ─────────────────────────────────────
#
# Relevance reweighting only takes effect in the fits-case (the degraded tiers
# are reached precisely when the per-section budget hits its 60-char floor,
# where the split is uniform regardless of query) — that path is covered by
# tests/test_query_aware_structural.py::TestSkeletonQueryAware.


def test_negative_scores_do_not_drop_a_heading() -> None:
    """An EmbeddingScorer can return negative cosine sims; clamped to >= 0 they
    must not skew the budget split into dropping a heading."""

    class _NegScorer:
        def score_sections(self, q, sections):
            return [0.9, -0.8, 0.7, -0.7][: len(sections)]

    doc = _few_rich_sections()
    out = SkeletonCompressor(_NegScorer()).compress(doc, max_chars=500, context_query="q")
    assert len(out) <= 500
    for h in ("## Auth", "## Billing", "## Logging", "## Metrics"):
        assert h in out


def test_injected_scorer_defaults_to_bm25() -> None:
    assert isinstance(SkeletonCompressor()._scorer, BM25Scorer)


# ── G. Direct coverage of the _fit_skeleton helper ───────────────────────────


class TestFitSkeletonTiers:
    def test_heading_prefix_counts_whole_lines_only(self) -> None:
        headings = ["## Alpha", "## Beta", "## Gamma"]  # 8 + 7 + 8, sep "\n\n"=2
        # budget for "## Alpha\n\n## Beta" = 8 + 2 + 7 = 17, reserve 0
        assert SkeletonCompressor._heading_prefix(headings, "\n\n", 17, 0) == 2
        assert SkeletonCompressor._heading_prefix(headings, "\n\n", 16, 0) == 1
        assert SkeletonCompressor._heading_prefix(headings, "\n\n", 7, 0) == 0

    def test_footer_and_marker_formats_are_stable(self) -> None:
        assert SkeletonCompressor._footer(100, 40, 5) == (
            "\n(skeleton — 100 chars original, 40 body_trimmed_chars, 5 sections)"
        )
        assert SkeletonCompressor._omitted_marker(3, 9) == "\n... (3 of 9 sections omitted)"

    def test_body_line_longer_than_remaining_budget_is_dropped_whole(self) -> None:
        # One section, one very long body line: Tier 2 keeps the heading, the
        # oversized line is omitted whole (never partially added).
        doc = "## Head one\n" + "x" * 5000 + "\n\n## Head two\nshort body line\n"
        out = SkeletonCompressor().compress(doc, max_chars=120)
        assert len(out) <= 120
        assert "## Head one" in out and "## Head two" in out
        assert "x" * 5000 not in out  # the giant line did not sneak in partial/whole
