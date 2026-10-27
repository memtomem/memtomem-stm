"""Source-preserving ATX headings outside fenced and indented code blocks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Iterator

_FENCE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^[ ]{0,3}(#{1,6})[ \t]+(.+?)[ \t]*$")
# Skip ordinary lines in the regex engine; fallback hints must retain fence
# state without running Python once per physical source line on every read.
_CANDIDATE = re.compile(r"^[ ]{0,3}(?:`{3,}|~{3,}|#{1,6}[ \t]+)[^\n]*", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Heading:
    start: int
    title_start: int
    end: int
    level: int
    title: str


def headings(text: str) -> Iterator[Heading]:
    """Offsets count Python Unicode code points, retaining original newlines.

    Always start at the document beginning: a suffix can start inside a fence.
    Only LF/CRLF are physical source lines; Unicode display separators remain
    content rather than silently creating Markdown syntax.
    """
    fence_char = ""
    fence_size = 0
    for candidate in _CANDIDATE.finditer(text):
        offset = candidate.start()
        body = candidate.group().rstrip("\r\n")
        fence = _FENCE.match(body)
        if fence:
            run, suffix = fence.groups()
            if fence_char:
                if run[0] == fence_char and len(run) >= fence_size and not suffix.strip():
                    fence_char = ""
            elif run[0] != "`" or "`" not in suffix:
                fence_char, fence_size = run[0], len(run)
        elif not fence_char:
            match = _HEADING.match(body)
            if match:
                title = re.sub(r"[ \t]+#+[ \t]*$", "", match.group(2)).strip(" \t")
                if title:
                    yield Heading(
                        offset,
                        offset + match.start(2),
                        offset + match.start(2) + len(title),
                        len(match.group(1)),
                        title,
                    )


def heading_matches(text: str) -> list[re.Match[str]]:
    """Adapter for the existing section-aware compression algorithms."""
    pattern = re.compile(r"(#{1,6}[ \t]+[^\r\n]*)")
    result = []
    for heading in headings(text):
        match = pattern.search(text, heading.start, heading.end)
        if match is not None:
            result.append(match)
    return result
