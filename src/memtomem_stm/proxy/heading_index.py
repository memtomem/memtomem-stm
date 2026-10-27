"""Bounded, compact suffix-heading indexes for progressive responses (#1153)."""

import base64
import binascii
import struct
import zlib
from typing import Any

from memtomem_stm.proxy.markdown_structure import headings

INDEX_KEY = "__heading_index_v3__"
LEGACY_INDEX_KEY = "__heading_index__"
# Binary records: heading start, title start, title end, next record, suffix count.
_RECORD = struct.Struct("<IIIII")
MAX_ROWS = 100_000
MAX_BYTES = MAX_ROWS * _RECORD.size
MAX_ENCODED = 128 * 1024
FALLBACK = b"scan"
HeadingIndex = bytes


def _field(rows: bytes | bytearray, i: int, field: int) -> int:
    return struct.unpack_from("<I", rows, i * _RECORD.size + field * 4)[0]


def _lower_bound(rows: bytes | bytearray, start: int, lo: int = 0) -> int:
    hi = len(rows) // _RECORD.size
    while lo < hi:
        mid = (lo + hi) // 2
        if _field(rows, mid, 0) < start:
            lo = mid + 1
        else:
            hi = mid
    return lo


def build_heading_index(content: str) -> HeadingIndex:
    if len(content) >= 2**32:
        return FALLBACK
    rows = bytearray()
    # Stop before a pathological response allocates an unbounded index.
    for m in headings(content):
        if len(rows) == MAX_BYTES:
            return FALLBACK
        rows.extend(_RECORD.pack(m.start, m.title_start, m.end, 0, 0))
    n = len(rows) // _RECORD.size
    for i in range(n - 1, -1, -1):
        nxt = _lower_bound(rows, _field(rows, i, 2), i + 1)
        count = 1 + (_field(rows, nxt, 4) if nxt < n else 0)
        struct.pack_into("<II", rows, i * _RECORD.size + 12, nxt, count)
    return bytes(rows)


def valid_heading_index(value: Any, length: int) -> bool:
    # Structural bounds only. Query checks the O(log h + 5) records it touches;
    # never walk all headings on a read or a switch between response keys.
    return isinstance(value, bytes) and (
        value == FALLBACK or (len(value) <= MAX_BYTES and len(value) % _RECORD.size == 0)
    )


def encode_heading_index(rows: HeadingIndex) -> str:
    if rows == FALLBACK:
        return "scan"
    # Byte shuffling exposes similar high bytes to zlib without Python integer
    # arrays or per-record JSON. Inverse uses 20 C-level extended slice copies.
    shuffled = b"".join(rows[i :: _RECORD.size] for i in range(_RECORD.size))
    encoded = base64.b64encode(zlib.compress(shuffled)).decode("ascii")
    return "v3:" + encoded if len(encoded) + 3 <= MAX_ENCODED else "scan"


def decode_heading_index(value: Any) -> HeadingIndex | None:
    if value == "scan":
        return FALLBACK
    if not isinstance(value, str) or not value.startswith("v3:") or len(value) > MAX_ENCODED:
        return None
    try:
        packed = base64.b64decode(value[3:], validate=True)
        decoder = zlib.decompressobj()
        shuffled = decoder.decompress(packed, MAX_BYTES + 1)
        if (
            not decoder.eof
            or decoder.unused_data
            or len(shuffled) > MAX_BYTES
            or len(shuffled) % _RECORD.size
        ):
            return None
        n = len(shuffled) // _RECORD.size
        rows = bytearray(len(shuffled))
        for i in range(_RECORD.size):
            rows[i :: _RECORD.size] = shuffled[i * n : (i + 1) * n]
        return bytes(rows)
    except (ValueError, binascii.Error, zlib.error):
        return None


def ensure_heading_index(content: str, chunks: dict[str, str]) -> tuple[HeadingIndex, bool]:
    rows = decode_heading_index(chunks.get(INDEX_KEY))
    if rows is not None:
        return rows, False
    rows = build_heading_index(content)
    encoded = encode_heading_index(rows)
    chunks[INDEX_KEY] = encoded
    return (FALLBACK if encoded == "scan" else rows), True


def legacy_remaining_headings(content: str, offset: int) -> str:
    start = offset - 1 if offset > 0 and content[offset - 1] == "\n" else offset
    titles: list[str] = []
    count = 0
    for heading in headings(content):
        if heading.start >= start:
            count += 1
            if len(titles) < 5:
                titles.append(heading.title.strip(" \t"))
    return _render(titles, count)


def _render(titles: list[str], count: int) -> str:
    result = ", ".join(f'"{title}"' for title in titles)
    if count > 5:
        result += f", ... (+{count - 5} more)"
    return result


def remaining_headings(content: str, offset: int, rows: HeadingIndex) -> str:
    if rows == FALLBACK:
        return legacy_remaining_headings(content, offset)
    start = offset - 1 if offset > 0 and content[offset - 1] == "\n" else offset
    if not valid_heading_index(rows, len(content)):
        return legacy_remaining_headings(content, offset)
    n = len(rows) // _RECORD.size
    i = _lower_bound(rows, start)
    if i == n:
        return ""
    count = _field(rows, i, 4)
    if not 1 <= count <= n - i:
        return legacy_remaining_headings(content, offset)
    titles = []
    for step in range(min(5, count)):
        begin, title, end, nxt, remaining = _RECORD.unpack_from(rows, i * _RECORD.size)
        if not (
            start <= begin < title < end <= len(content)
            and i < nxt <= n
            and remaining == count - step
            and (nxt < n if remaining > 1 else nxt == n)
        ):
            return legacy_remaining_headings(content, offset)
        titles.append(content[title:end].strip(" \t"))
        start, i = end, nxt
    return _render(titles, count)
