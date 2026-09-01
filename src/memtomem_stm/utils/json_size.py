"""Bounded UTF-8 JSON size accounting without materializing one giant string."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel


_STRING_CHUNK_CHARS = 64 * 1024
_JSON_ESCAPE_RE = re.compile(r'["\\\x00-\x1f]')
_SHORT_JSON_ESCAPES = frozenset({'"', "\\", "\b", "\f", "\n", "\r", "\t"})


class _LimitExceeded(Exception):
    pass


class _Sizer:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0

    def add(self, size: int) -> None:
        self.total += size
        if self.total > self.limit:
            raise _LimitExceeded

    def string(self, value: str) -> None:
        self.add(2)  # quotes
        for offset in range(0, len(value), _STRING_CHUNK_CHARS):
            chunk = value[offset : offset + _STRING_CHUNK_CHARS]
            try:
                # CPython's UTF-8 encoder and regex engine account for the
                # common path in native code. Keep the slice bounded so a
                # permitted multi-megabyte string does not require another
                # full-size encoded copy merely to measure it.
                chunk_size = len(chunk.encode("utf-8"))
            except UnicodeEncodeError:
                # A JSON string may contain a lone surrogate. Its JSON wire
                # form is the six-byte ``\\udxxx`` escape, while UTF-8 rejects
                # it. Limit the rare scalar fallback to one bounded chunk.
                self._string_chunk_with_surrogates(chunk)
                continue

            for match in _JSON_ESCAPE_RE.finditer(chunk):
                # Quotes, backslashes, and the five short control escapes grow
                # from one UTF-8 byte to two. Other C0 controls grow to the
                # six-byte ``\\u00xx`` form.
                chunk_size += 1 if match.group() in _SHORT_JSON_ESCAPES else 5
            self.add(chunk_size)

    def _string_chunk_with_surrogates(self, chunk: str) -> None:
        for char in chunk:
            code = ord(char)
            if char in _SHORT_JSON_ESCAPES:
                self.add(2)
            elif code < 0x20 or 0xD800 <= code <= 0xDFFF:
                self.add(6)
            elif code <= 0x7F:
                self.add(1)
            elif code <= 0x7FF:
                self.add(2)
            elif code <= 0xFFFF:
                self.add(3)
            else:
                self.add(4)

    def value(self, value: Any) -> None:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json", by_alias=True, exclude_none=True)
        elif isinstance(value, Enum):
            value = value.value
        elif isinstance(value, Path):
            value = str(value)

        if value is None:
            self.add(4)
        elif value is True:
            self.add(4)
        elif value is False:
            self.add(5)
        elif isinstance(value, str):
            self.string(value)
        elif isinstance(value, int | float):
            self.add(len(json.dumps(value, allow_nan=True, separators=(",", ":"))))
        elif isinstance(value, Mapping):
            self.add(2)
            for index, (key, item) in enumerate(value.items()):
                if index:
                    self.add(1)
                self.string(str(key))
                self.add(1)
                self.value(item)
        elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
            self.add(2)
            for index, item in enumerate(value):
                if index:
                    self.add(1)
                self.value(item)
        elif isinstance(value, bytes | bytearray):
            # MCP JSON models encode binary fields as base64 strings. Unknown
            # raw bytes are conservatively counted as their hexadecimal form.
            self.string(bytes(value).hex())
        elif isinstance(value, SimpleNamespace):
            self.value({k: v for k, v in vars(value).items() if not k.startswith("_")})
        else:
            self.string(str(value))


def json_utf8_size(value: Any, *, limit: int) -> int:
    """Return encoded JSON bytes, or ``limit + 1`` once the cap is exceeded."""
    sizer = _Sizer(limit)
    try:
        sizer.value(value)
    except _LimitExceeded:
        return limit + 1
    return sizer.total


async def json_utf8_size_async(value: Any, *, limit: int) -> int:
    """Measure JSON size away from the event loop.

    MCP responses can legitimately approach the configured multi-megabyte
    ceiling. Even bounded accounting is CPU work, so both the dispatcher
    boundary and the call-level fallback use this helper rather than delaying
    unrelated proxy calls on the loop thread.
    """
    return await asyncio.to_thread(json_utf8_size, value, limit=limit)
