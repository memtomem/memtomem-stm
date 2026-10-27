"""Encode metadata for Core's line-oriented Markdown reader, including Unicode."""

from __future__ import annotations

import json

_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def metadata_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def metadata_text(value: object) -> str:
    """Keep field text on one physical/Unicode line and inert in Markdown."""
    text = str(value)
    return "".join(f"\\u{ord(c):04x}" if c in _LINE_BREAKS or c in "`<>" else c for c in text)
