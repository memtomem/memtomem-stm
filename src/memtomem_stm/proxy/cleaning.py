"""Content cleaning — noise removal before compression and indexing."""

from __future__ import annotations

import logging as _logging
import re
import unicodedata
from collections.abc import Iterator
from typing import Protocol

_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```|`[^`\n]+`")
_GENERIC_RE = re.compile(r"[A-Z]\w{0,60}<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>[\s\S]*?</\1>", re.I)
_HTML_TAG_RE = re.compile(r"<[a-zA-Z][\w.-]*(?:\s[^>]*)?\s*/?>")
_CLOSE_TAG_RE = re.compile(r"</[a-zA-Z][\w.-]*\s*>")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_LINK_LINE_RE = re.compile(r"^\s*[-*]\s*\[.*?\]\(https?://\S+\)")
_BARE_URL_LINE_RE = re.compile(r"^\s*[-*]?\s*https?://\S+\s*$")

# Filler for a protected region while the removal patterns scan. It only ever
# reaches a scan, never the result — every character of the output is sliced
# from the ORIGINAL text by offset — so an upstream copy of it cannot make the
# cleaner drop or duplicate anything.
_MASK_CHAR = "\x00"
_MASK_RUN_RE = re.compile(r"\x00+")


# Prompt injection heuristic patterns — common LLM manipulation attempts
_INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|context)"),
    re.compile(r"(?i)you\s+are\s+now\s+(a|an|the)\s+"),
    re.compile(r"(?i)system\s*:\s*you\s+(are|must|should|will)"),
    re.compile(r"(?i)new\s+instructions?\s*:"),
    re.compile(r"(?i)forget\s+(everything|all|your)\s+(above|previous|prior)"),
    re.compile(r"(?i)disregard\s+(all\s+)?(previous|prior|above)"),
    re.compile(r"(?i)<\s*system\s*>"),
]

_logger = _logging.getLogger(__name__)


def _find_spans(pattern: re.Pattern[str], text: str) -> list[int]:
    """Every match of ``pattern`` as a flat ``[start, end, start, end, ...]``.

    Flat rather than a list of pairs: a fence-dense response can hold hundreds
    of thousands of matches, and a tuple per match costs several times what two
    entries in one list do.
    """
    out: list[int] = []
    for match in pattern.finditer(text):
        out.append(match.start())
        out.append(match.end())
    return out


def _merge_spans(first: list[int], second: list[int]) -> list[int]:
    """Merge two ascending flat span lists, coalescing overlap and adjacency.

    Both inputs come from ``finditer`` and are already ordered, so this is one
    linear pass — sorting a concatenation would allocate a third list and cost
    a comparison sort for order the matches already have. A generic can enclose
    a fence, which is what makes the coalescing necessary.
    """
    out: list[int] = []
    i = j = 0
    len_first, len_second = len(first), len(second)
    while i < len_first or j < len_second:
        if j >= len_second or (i < len_first and first[i] <= second[j]):
            span_start, span_end = first[i], first[i + 1]
            i += 2
        else:
            span_start, span_end = second[j], second[j + 1]
            j += 2
        if out and span_start <= out[-1]:
            if span_end > out[-1]:
                out[-1] = span_end
        else:
            out.append(span_start)
            out.append(span_end)
    return out


def _mask_spans(text: str, spans: list[int]) -> str:
    """Blank out ``spans`` while keeping every other offset where it was.

    Same length in, same length out — that is what lets a match found on the
    masked copy be read back as a span of the original.
    """
    if not spans:
        return text
    parts: list[str] = []
    pos = 0
    for i in range(0, len(spans), 2):
        start, end = spans[i], spans[i + 1]
        parts.append(text[pos:start])
        parts.append(_MASK_CHAR * (end - start))
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def _drop_from_segments(segments: list[int], matches: Iterator[re.Match[str]]) -> list[int] | None:
    """Remove the spans ``matches`` reports — offsets into the segments joined
    end to end — from ``segments``, or return ``None`` when there were none.

    ``segments`` is a flat ``[start, end, start, end, ...]`` in original
    coordinates. It and the matches are both sorted and non-overlapping, so one
    merge walks them together without a search per match, and the matches are
    consumed straight off the iterator: a response at the ``max_upstream_chars``
    ceiling produces hundreds of thousands of them, and materializing a list of
    pairs first costs more than the rest of the pass.

    The removal patterns run one after another over the surviving text, so each
    sees what the previous one left *joined*, exactly as sequential ``sub``
    calls on a shrinking string did. Masking a match in place instead would
    leave the two sides apart, and a close tag formed only by the join would
    survive.
    """
    match = next(matches, None)
    if match is None:
        return None
    cut_start, cut_end = match.span()
    out: list[int] = []
    view_pos = 0
    for i in range(0, len(segments), 2):
        seg_start = segments[i]
        seg_end = segments[i + 1]
        seg_view_end = view_pos + seg_end - seg_start
        keep_from = seg_start
        while match is not None and cut_start < seg_view_end:
            if cut_end > view_pos:
                start_here = seg_start + cut_start - view_pos if cut_start > view_pos else seg_start
                end_here = seg_start + cut_end - view_pos if cut_end < seg_view_end else seg_end
                if start_here > keep_from:
                    out.append(keep_from)
                    out.append(start_here)
                if end_here > keep_from:
                    keep_from = end_here
                if cut_end > seg_view_end:
                    break
            match = next(matches, None)
            if match is not None:
                cut_start, cut_end = match.span()
        if keep_from < seg_end:
            out.append(keep_from)
            out.append(seg_end)
        view_pos = seg_view_end
    return out


class ContentCleaner(Protocol):
    def clean(self, text: str) -> str: ...


class DefaultContentCleaner:
    def __init__(self, config: object | None = None) -> None:
        # Accept a CleaningConfig (or any object with strip_html/deduplicate/collapse_links)
        self._strip_html = getattr(config, "strip_html", True) if config else True
        self._dedup = getattr(config, "deduplicate", True) if config else True
        self._collapse = getattr(config, "collapse_links", True) if config else True

    def clean(self, text: str) -> str:
        if not text:
            return text
        # Normalize line endings — upstream servers on Windows may send \r\n
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self._check_injection(text)
        if self._strip_html:
            text = self._strip_html_jsx(text)
        if self._dedup:
            text = self._deduplicate_paragraphs(text)
        if self._collapse:
            text = self._collapse_link_floods(text)
        text = self._normalize_whitespace(text)
        return text.strip()

    @staticmethod
    def _check_injection(text: str) -> None:
        """Log a warning if the text contains likely prompt injection patterns.

        Detection-only: the output is never altered or blocked, so a miss
        costs a log line, not an exploit. The scan cost is bounded to 20,000
        chars: a response up to that size is scanned in FULL (one window, so
        a pattern straddling any offset is still seen); a larger one gets the
        first and last 10,000 chars — appending the payload after a large
        benign body is the cheap way around a head-only window — while an
        injection buried in its MIDDLE goes unlogged by design.
        """
        if len(text) <= 20_000:
            samples = [text]
        else:
            samples = [text[:10_000], text[-10_000:]]
        for raw in samples:
            # NFKC-normalize to defeat Unicode confusable bypasses (e.g.
            # Cyrillic or fullwidth substitutions for ASCII letters).
            sample = unicodedata.normalize("NFKC", raw)
            for pat in _INJECTION_PATTERNS:
                m = pat.search(sample)
                if m:
                    _logger.warning(
                        "Possible prompt injection detected in upstream response: %r",
                        m.group(0)[:80],
                    )
                    return

    def _strip_html_jsx(self, text: str) -> str:
        r"""Strip HTML/JSX tags, leaving code fences and generic types intact.

        The protected regions are never substituted into the text. An earlier
        design swapped each fence and generic for a NUL-delimited marker, and
        that spelling was not reserved: the substitution wrote the very
        sequences it later searched for. Two markers standing next to each
        other spelled a third across their boundary, so ``\`a\`GEN0\`b\``` lost
        both fences, and a marker the upstream sent itself was restored at
        every occurrence, so a small response could expand quadratically
        (#948).

        Instead the regions are located as spans and blanked out on a
        SEPARATE, equal-length copy. The removal patterns scan that copy — so
        a protected region is opaque to them, which is what the markers were
        for — while the result is sliced from the original text by offset.
        Nothing an upstream can send is ever read back as a marker.
        """
        fences = _find_spans(_CODE_FENCE_RE, text)
        # Generics are found on a copy where fences are already opaque, so a
        # fence inside one (``Map<`K`, V>``) cannot end the `[^>]+` early.
        with_fences_hidden = _mask_spans(text, fences)
        protected = _merge_spans(fences, _find_spans(_GENERIC_RE, with_fences_hidden))
        if not protected:
            # Nothing to keep out of the patterns' way, so there is nothing for
            # the offset bookkeeping below to preserve: remove in place.
            stripped = _SCRIPT_STYLE_RE.sub("", text)
            stripped = _HTML_TAG_RE.sub("", stripped)
            return _CLOSE_TAG_RE.sub("", stripped)

        masked = _mask_spans(text, protected)

        # Every removal pattern opens on "<" and closes on ">", and a masked
        # region is all _MASK_CHAR, so a match can neither begin nor end inside
        # one: a protected region is removed whole or not at all. When the run
        # count and lengths still line up afterwards — and the text brought no
        # _MASK_CHAR of its own to be mistaken for one — no protected region
        # was removed and no two were brought together, so the k-th run is the
        # k-th region and the originals splice straight back in order. Tags and
        # script blocks around them may well have been removed; that is the
        # point, and it does not disturb the alignment.
        stripped = _SCRIPT_STYLE_RE.sub("", masked)
        stripped = _HTML_TAG_RE.sub("", stripped)
        stripped = _CLOSE_TAG_RE.sub("", stripped)
        if _MASK_CHAR not in text:
            # Checked and spliced in the same walk: a separate pass to validate
            # would materialize one string per run, and a fence-dense response
            # has hundreds of thousands of them. A run that does not line up
            # abandons the attempt and the segment walk below takes over.
            parts: list[str] = []
            pos = 0
            index = 0
            aligned = True
            for run_match in _MASK_RUN_RE.finditer(stripped):
                run_start = run_match.start()
                if index >= len(protected) or (
                    run_match.end() - run_start != protected[index + 1] - protected[index]
                ):
                    aligned = False
                    break
                parts.append(stripped[pos:run_start])
                parts.append(text[protected[index] : protected[index + 1]])
                pos = run_match.end()
                index += 2
            if aligned and index == len(protected):
                parts.append(stripped[pos:])
                return "".join(parts)
            del parts

        total = len(text)
        segments = [0, total]
        view = masked
        patterns = (_SCRIPT_STYLE_RE, _HTML_TAG_RE, _CLOSE_TAG_RE)
        for index, pattern in enumerate(patterns):
            dropped = _drop_from_segments(segments, pattern.finditer(view))
            if dropped is None:
                continue
            segments = dropped
            if index + 1 < len(patterns):
                view = "".join(
                    masked[segments[i] : segments[i + 1]] for i in range(0, len(segments), 2)
                )
        if segments == [0, total]:
            return text
        return "".join(text[segments[i] : segments[i + 1]] for i in range(0, len(segments), 2))

    def _deduplicate_paragraphs(self, text: str) -> str:
        paragraphs = re.split(r"\n{2,}", text)
        seen: set[str] = set()
        unique: list[str] = []
        for p in paragraphs:
            normalized = " ".join(p.split())
            if normalized in seen:
                continue
            seen.add(normalized)
            unique.append(p)
        return "\n\n".join(unique)

    def _collapse_link_floods(self, text: str) -> str:
        paragraphs = re.split(r"\n{2,}", text)
        result: list[str] = []
        for p in paragraphs:
            lines = p.strip().split("\n")
            if len(lines) >= 10:
                link_count = sum(
                    1 for ln in lines if _LINK_LINE_RE.match(ln) or _BARE_URL_LINE_RE.match(ln)
                )
                if link_count / len(lines) >= 0.8:
                    result.append(f"[{link_count} links omitted]")
                    continue
            result.append(p)
        return "\n\n".join(result)

    def _normalize_whitespace(self, text: str) -> str:
        return _MULTI_NEWLINE_RE.sub("\n\n", text)
