"""Secret classification for ``mms import`` (RFC §7.2.1).

When ``mms import`` reads a host's MCP definitions, each entry's
``env`` block may contain mixed secret and non-secret values. The
classifier here decides which keys to redact in ``--plan`` output (and
prompt-on-write for ambiguous cases under ``--apply``).

Two signals, OR-combined into the final classification:

1. **Key pattern** (case-insensitive substring): ``TOKEN`` / ``KEY`` /
   ``SECRET`` / ``PASSWORD`` / ``PASS`` / ``AUTH`` / ``CREDENTIAL`` /
   ``API_KEY``. Hits even if the value is short ("API_KEY=test" still
   classified — RFC explicit edge case).
2. **Value heuristic**: length ≥ 32 AND mostly base64- or hex-charset.
   Catches opaque tokens stored under unusual key names.

Output is a ``ClassifiedEnv`` dict mapping key → :class:`Classification`
so callers can render different UX per kind (key-pattern hit prints the
matched pattern; heuristic hit recommends prompt-on-apply; non-secret
prints the value).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# Order matters: longer alternatives first so the matched pattern in the
# classification reason is the most-specific one. ``API_KEY`` is more
# informative than ``KEY`` when both could match.
_SECRET_KEY_PATTERNS: tuple[str, ...] = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "AUTH",
    "PASS",
    "KEY",
)

# Length below which the value heuristic doesn't even consider a value —
# short opaque-looking strings are dominated by false positives ("abcd1234"
# hex-shaped but obviously not a credential).
_VALUE_HEURISTIC_MIN_LEN = 32

# Charset regexes for the value heuristic. Allow common separators in
# base64-url variants (``-``, ``_``); ``=`` for padding.
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=_-]+$")
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")


class Kind(Enum):
    """Why a value was classified as secret (or not)."""

    KEY_PATTERN = "key_pattern"
    VALUE_HEURISTIC = "value_heuristic"
    NON_SECRET = "non_secret"


@dataclass(frozen=True)
class Classification:
    """Per-env-key classification result.

    ``is_secret`` is the primary boolean used by ``--plan`` for
    redaction. ``kind`` distinguishes the *reason* so the apply flow
    can prompt only on heuristic-only hits (where the user might
    legitimately want to keep the value plaintext) and silently accept
    key-pattern hits.

    ``reason`` is a short human-readable label suitable for ``--plan``
    output (e.g. ``"matches *KEY*"`` or ``"32+ chars hex"``). Empty for
    NON_SECRET.
    """

    is_secret: bool
    kind: Kind
    reason: str


def _matched_key_pattern(key: str) -> str | None:
    """Return the first matching secret key pattern, or None."""
    upper = key.upper()
    for pattern in _SECRET_KEY_PATTERNS:
        if pattern in upper:
            return pattern
    return None


def _value_looks_secret(value: str) -> str | None:
    """Return the matched charset label (``"base64"`` / ``"hex"``) or None.

    Length floor + charset gate; both must hold. Whitespace makes the
    value clearly *not* opaque, so reject early.
    """
    if len(value) < _VALUE_HEURISTIC_MIN_LEN:
        return None
    if any(c.isspace() for c in value):
        return None
    if _HEX_RE.match(value):
        return "hex"
    if _BASE64_RE.match(value):
        return "base64"
    return None


def classify_env(env: dict[str, str]) -> dict[str, Classification]:
    """Classify every env entry. Order-preserving.

    Key pattern beats value heuristic when both match — the reason
    label leans on the more *specific* signal (pattern names beat
    "32+ chars hex" since the pattern tells the user *why* it's a
    secret rather than just that it looks opaque).
    """
    result: dict[str, Classification] = {}
    for key, value in env.items():
        pattern = _matched_key_pattern(key)
        if pattern is not None:
            result[key] = Classification(
                is_secret=True,
                kind=Kind.KEY_PATTERN,
                reason=f"matches *{pattern}*",
            )
            continue

        charset = _value_looks_secret(value)
        if charset is not None:
            result[key] = Classification(
                is_secret=True,
                kind=Kind.VALUE_HEURISTIC,
                reason=f"32+ chars {charset}",
            )
            continue

        result[key] = Classification(is_secret=False, kind=Kind.NON_SECRET, reason="")
    return result


REDACTED_DISPLAY = "<REDACTED>"
"""Single-place constant for ``--plan`` output's redaction marker."""


def redact_for_plan(
    env: dict[str, str], classification: dict[str, Classification]
) -> dict[str, str]:
    """Return a *display* copy of ``env`` with secret values masked.

    ``--apply`` writes the original ``env`` to disk; ``--plan`` prints
    the redacted copy. The classification dict is the source of truth
    for which keys to mask — never re-classify here, since the caller
    may want to override (e.g. ``--show-imported`` returns ``env`` as-is).
    """
    return {
        key: REDACTED_DISPLAY if classification[key].is_secret else value
        for key, value in env.items()
    }
