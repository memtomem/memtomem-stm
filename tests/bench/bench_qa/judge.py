"""Rule-based judging helpers for bench_qa scenarios.

Intentionally separate from ``tests.bench.judge`` (the existing
``RuleBasedJudge`` that scores a ``BenchTask``). bench_qa fixtures carry
``qa_probes`` — structured ``{question, expected_keywords}`` pairs whose
answerability is gated strictly: *every* keyword of a probe must appear
in the compressed output (case-insensitive substring) for the probe to
count as answerable.

The surfacing_recall@k helper lives here too (used by S10 and later
surfacing scenarios).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence


def _all_keywords_present(keywords: Iterable[str], text: str) -> bool:
    """True iff every keyword is a case-insensitive substring of text."""
    lowered = text.lower()
    return all(kw.lower() in lowered for kw in keywords)


def qa_answerable_ratio(probes: Sequence[dict], text: str) -> tuple[int, int]:
    """Count answerable probes out of the total.

    Each probe is expected to shape like ``{"expected_keywords": [...]}``
    (matching :class:`bench.bench_qa.schema.QAProbe`). Returns
    ``(answerable, total)``. An empty probe list returns ``(0, 0)``.
    """
    total = len(probes)
    if total == 0:
        return 0, 0
    answerable = sum(
        1 for probe in probes if _all_keywords_present(probe["expected_keywords"], text)
    )
    return answerable, total


def surfacing_recall_at_k(
    returned_ids: Sequence[str],
    expected_ids: Sequence[str],
    k: int,
) -> float:
    """recall@k = |returned_top_k ∩ expected| / min(k, |expected|).

    ``returned_ids`` is typically the ordered ``memory_ids`` list from a
    ``surfacing_events`` row; the first ``k`` entries are considered.
    Returns 1.0 when ``expected_ids`` is empty (no ground truth to miss).
    """
    if not expected_ids:
        return 1.0
    top_k = set(returned_ids[:k])
    hit = len(top_k & set(expected_ids))
    denom = min(k, len(expected_ids))
    return hit / denom if denom else 1.0
