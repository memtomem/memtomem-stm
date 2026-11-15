"""Secret classification for ``mms import`` (RFC §7.2.1).

When ``mms import`` reads a host's MCP definitions, each entry's
``env`` block may contain mixed secret and non-secret values. The
classifier here decides which keys to redact in ``--plan`` output (and
prompt-on-write for ambiguous cases under ``--apply``).

Three signals, checked in this order; the first hit decides the reason:

1. **Key pattern** (case-insensitive substring): ``TOKEN`` / ``KEY`` /
   ``SECRET`` / ``PASSWORD`` / ``PASS`` / ``AUTH`` / ``CREDENTIAL`` /
   ``API_KEY`` / ``COOKIE`` / ``BEARER`` / ``DSN``. Hits even if the value
   is short ("API_KEY=test" still classified — RFC explicit edge case).
2. **Value pattern**: the value is a URL with a password in its userinfo
   (``postgresql://user:pw@host/db``), or it carries something the proxy's
   own credential scanner recognizes (a JWT, a provider token such as
   ``ghp_…`` or ``sk-…``, a PEM private-key header, a ``password=`` label).
   Catches connection strings and tokens stored under neutral key names
   such as ``DATABASE_URL``, which the key pattern does not see.
3. **Value heuristic**: length ≥ 32 AND mostly base64- or hex-charset.
   Catches opaque tokens stored under unusual key names.

Output is a dict mapping key → :class:`Classification`. Callers read
``is_secret`` to decide what to mask and ``reason`` to say why; the
reason is a fixed label and never quotes the value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from memtomem_stm.proxy.privacy import CREDENTIAL_PATTERNS, contains_sensitive_content

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
    "COOKIE",
    "BEARER",
    # A Sentry-style DSN carries its key as userinfo with no password, so the
    # URL value pattern below deliberately does not see it; the key name does.
    "DSN",
    "KEY",
)

_URI_PASSWORD_REASON = "URI with password"
_CREDENTIAL_FORMAT_REASON = "credential format"

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
    VALUE_PATTERN = "value_pattern"
    VALUE_HEURISTIC = "value_heuristic"
    NON_SECRET = "non_secret"


@dataclass(frozen=True)
class Classification:
    """Per-env-key classification result.

    ``is_secret`` is the primary boolean: ``mms import`` / ``mms host
    sync`` mask the value when it is set, and ``mms eject`` treats it as
    a secret that would land on the ``claude mcp add-json`` argv.
    ``kind`` records which signal decided.

    ``reason`` is a short human-readable label suitable for ``--plan``
    output (e.g. ``"matches *KEY*"``, ``"URI with password"`` or
    ``"32+ chars hex"``). Empty for NON_SECRET.
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


def _uri_has_password(value: str) -> bool:
    """True when ``value``, read as a URL, has a non-empty password.

    The whole value is parsed with :func:`urllib.parse.urlsplit`, the way a
    URL client would read it, so a password with a raw space or ``@`` still
    counts and a JSON or YAML fragment that merely contains a URL and an
    ``@`` elsewhere does not. A user with no password (``git@host``, a
    Sentry DSN key) is not a password. A URL embedded inside a longer value
    (``--db=postgresql://u:p@h``) is not parsed as one; the credential
    format signal still sees a ``password=`` label there.

    ``urlsplit`` raises ``ValueError`` on malformed brackets. Such a value
    counts as a secret when it contains an ``@``, since userinfo is then
    possible and the parse cannot rule it out.
    """
    try:
        return bool(urlsplit(value).password)
    except ValueError:
        return "@" in value


def _value_pattern(value: str) -> str | None:
    """Return the value-pattern reason label, or None."""
    if _uri_has_password(value):
        return _URI_PASSWORD_REASON
    if contains_sensitive_content(value, CREDENTIAL_PATTERNS):
        return _CREDENTIAL_FORMAT_REASON
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

    Key pattern beats value pattern, which beats value heuristic, when
    more than one matches — the reason label leans on the more *specific*
    signal (pattern names beat "32+ chars hex" since the pattern tells
    the user *why* it's a secret rather than just that it looks opaque).

    The value pattern reuses the proxy's ``CREDENTIAL_PATTERNS`` whole,
    including its label rules, so a value such as ``--password=false``
    classifies as secret. That is deliberate: a value that carries a
    password label is usually a credential, and the cost of being wrong
    is one masked value or one ``--allow-argv-secrets``.
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

        value_reason = _value_pattern(value)
        if value_reason is not None:
            result[key] = Classification(
                is_secret=True,
                kind=Kind.VALUE_PATTERN,
                reason=value_reason,
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
