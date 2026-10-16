"""Persistable suffix-heading index for progressive responses (#1153)."""

import re
from bisect import bisect_left
from typing import Any

# Lookahead records overlapping candidates too: ``# \n# title`` can match
# differently when a read starts at the second line. Links reproduce findall's
# non-overlapping matches from any requested offset without rescanning text.
_CANDIDATES = re.compile(r"(?=((?:^|\n)#{1,6}\s+(.+)))")
INDEX_KEY = "heading_index_v1"
# Each record: regex start, title start, match end, next record, suffix count.
HeadingIndex = tuple[tuple[int, ...], ...]


def build_heading_index(content: str) -> HeadingIndex:
    rows = [[m.start(1), m.start(2), m.end(1), 0, 0] for m in _CANDIDATES.finditer(content)]
    for i in range(len(rows) - 1, -1, -1):
        nxt = bisect_left(rows, rows[i][2], lo=i + 1, key=lambda row: row[0])
        rows[i][3] = nxt
        rows[i][4] = 1 + (rows[nxt][4] if nxt < len(rows) else 0)
    return tuple(tuple(row) for row in rows)


def valid_heading_index(value: Any, length: int) -> bool:
    """Reject malformed optional indexes; legacy/corrupt indexes are rebuilt."""
    if not isinstance(value, (list, tuple)):
        return False
    previous = -1
    for row in value:
        if (
            not isinstance(row, (list, tuple))
            or len(row) != 5
            or any(type(v) is not int for v in row)
        ):
            return False
        start, title, end, _, _ = row
        if not previous < start < title < end <= length:
            return False
        previous = start
    for i in range(len(value) - 1, -1, -1):
        nxt = bisect_left(value, value[i][2], lo=i + 1, key=lambda row: row[0])
        if value[i][3] != nxt or value[i][4] != 1 + (value[nxt][4] if nxt < len(value) else 0):
            return False
    return True


def ensure_heading_index(content: str, meta: dict[str, Any]) -> bool:
    """Populate once while the caller holds the entry's mutation lock."""
    if not meta.get("include_structure_hint", True):
        return False
    if valid_heading_index(meta.get(INDEX_KEY), len(content)):
        return False
    meta[INDEX_KEY] = build_heading_index(content)
    return True


def remaining_headings(content: str, offset: int, rows: HeadingIndex) -> str:
    start = offset - 1 if offset > 0 and content[offset - 1] == "\n" else offset
    i = bisect_left(rows, start, key=lambda row: row[0])
    if i == len(rows):
        return ""
    count = rows[i][4]
    shown = []
    for _ in range(min(5, count)):
        row = rows[i]
        shown.append(f'"{content[row[1] : row[2]].strip()}"')
        i = row[3]
    result = ", ".join(shown)
    if count > 5:
        result += f", ... (+{count - 5} more)"
    return result
