"""Content cleaning — noise removal before compression and indexing."""

from __future__ import annotations

import logging as _logging
import re
import unicodedata
from typing import Protocol

_HTML_TAG_RE = re.compile(r"<[a-zA-Z][\w.-]*(?:\s[^>]*)?\s*/?>")
_CLOSE_TAG_RE = re.compile(r"</[a-zA-Z][\w.-]*\s*>")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_LINK_LINE_RE = re.compile(r"^\s*[-*]\s*\[.*?\]\(https?://\S+\)")
_BARE_URL_LINE_RE = re.compile(r"^\s*[-*]?\s*https?://\S+\s*$")

# The regions `_strip_html_jsx` recognizes. Each body is written once and
# only reaches the engine through `_PROTECTED_RE`, so there is no second copy
# of a rule to drift out of step with this one.
_FENCE_SRC = r"```[\s\S]*?```|`[^`\n]+`"
_GENERIC_SRC = r"[A-Z]\w{0,60}<[^>]+>"
# Scoped case-insensitivity, because a module-level `re.I` would reach
# `_GENERIC_SRC`'s `[A-Z]` and make it match lowercase. The backreference must
# be named: a numbered one cannot refer out of the still-open alternation.
_SCRIPT_STYLE_SRC = r"(?i:<(?P<tag>script|style)\b[^>]*>[\s\S]*?</(?P=tag)>)"

# One alternation, so a single left-to-right pass decides the regions by
# position: the earlier START wins. That is what makes a `<script>` opened
# outside a fence swallow a fence inside it, while a `<script>` written
# *inside* a fence is left alone. Alternative order carries no meaning here —
# the three begin with a backtick, `<` and `[A-Z]` respectively, so no two can
# start at the same offset for the order to break a tie between.
_PROTECTED_RE = re.compile(
    f"(?P<fence>{_FENCE_SRC})|(?P<drop>{_SCRIPT_STYLE_SRC})|(?P<generic>{_GENERIC_SRC})"
)

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


def _strip_tags(segment: str) -> str:
    """Remove HTML tags from a segment that lies outside every protected region."""
    segment = _HTML_TAG_RE.sub("", segment)
    return _CLOSE_TAG_RE.sub("", segment)


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

        The protected regions are never substituted for a marker. An earlier
        design swapped each fence and generic for a NUL-delimited token, and
        that spelling is not reserved: the substitution wrote the very
        sequences it later searched for. Two tokens standing next to each
        other spelled a third across their boundary — ``\`a\`GEN0\`b\``` lost
        both fences — and a token the upstream sent itself was restored at
        every occurrence, so a small response could expand quadratically
        (#948). Walking spans instead means nothing is ever written into the
        text and looked for again, so neither is expressible.
        """
        out: list[str] = []
        pos = 0
        for m in _PROTECTED_RE.finditer(text):
            out.append(_strip_tags(text[pos : m.start()]))
            # `drop` is the <script>/<style> block, content and tags alike;
            # a fence or generic is carried over exactly as it arrived.
            if m.group("drop") is None:
                out.append(m.group(0))
            pos = m.end()
        out.append(_strip_tags(text[pos:]))
        return "".join(out)

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
