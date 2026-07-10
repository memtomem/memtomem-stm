"""Display/log redaction helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit


def redact_url_userinfo(url: str) -> str:
    """Strip ``user:password@`` userinfo from *url* for display/logging.

    ``ltm_mcp_url`` may carry basic-auth credentials in front of a network
    LTM (#398); every operator-facing rendering of it (adapter connect logs,
    the engine's unreachable-LTM warning, ``mms health`` output) must go
    through here. The connection itself always uses the configured URL
    verbatim — only displays are redacted.

    A URL the stdlib cannot parse is replaced wholesale rather than echoed —
    an unparseable value could still embed credentials.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if parts.netloc:
        if "@" not in parts.netloc:
            return url
        host = parts.netloc.rpartition("@")[2]
        return urlunsplit(parts._replace(netloc=f"***@{host}"))
    # No netloc: urlsplit parses a scheme-less "alice:pw@host/path" as a bare
    # path without raising, so a credential-looking value would be echoed
    # verbatim. Anything '@'-bearing that we couldn't decompose is replaced
    # wholesale.
    if "@" in url:
        return "<unparseable url>"
    return url


def redact_exception_text(text: str, url: str) -> str:
    """Scrub *url*'s userinfo out of arbitrary *text* (exception messages).

    httpx exception strings embed the full request URL — userinfo included —
    so a log line rendering a transport exception for a credentialed endpoint
    leaks even when the *display* string was redacted. Best-effort string
    replacement: the exact configured URL, then its ``user:pw@`` prefix (which
    also catches URL variants httpx derives from the original).
    """
    if not text or not url or "@" not in url:
        return text
    out = text.replace(url, redact_url_userinfo(url))
    try:
        netloc = urlsplit(url).netloc
    except ValueError:
        netloc = ""
    userinfo = netloc.rpartition("@")[0]
    if userinfo:
        out = out.replace(f"{userinfo}@", "***@")
    return out


def sanitize_secrets(
    text: str, secret_values: Iterable[str], *, placeholder: str = "<REDACTED>"
) -> str:
    """Replace every occurrence of each secret value in *text* with *placeholder*.

    Central sanitizer for **free-form strings** — exception messages, probe
    failure causes, log lines — where the configured ``env``/``headers``
    values (or URL credentials) may be echoed verbatim by an SDK or
    validation error. Structured mapping *outputs* (``--json`` server dumps)
    are a different contract: they mask every value by key position via
    ``_mask_mapping_values`` and never need to know the values. This helper
    is for text that already interpolated the values.

    Substitution rules are normalized deliberately:

    - **Empty values are dropped** — a naive ``text.replace("", ph)`` would
      interleave the placeholder between every character of the message.
    - **Duplicate values are deduplicated** — each distinct value is
      substituted once.
    - **Longer values are matched first** — if ``"abc"`` were matched
      before ``"abcdef"``, the leftover ``"def"`` suffix of the longer
      secret would leak. Ties break lexicographically for determinism.

    Replacement is a **single regex pass over the original text**, not
    sequential ``str.replace`` calls: sequential passes let a later, shorter
    secret rewrite a placeholder a previous pass just inserted (e.g. a
    secret ``"RED"`` corrupting the ``"<REDACTED>"`` already written), which
    both mangles the message and can re-expose fragments. A single pass
    consumes each matched span once and never re-scans inserted text.
    """
    if not text:
        return text
    values = sorted({v for v in secret_values if v}, key=lambda v: (-len(v), v))
    if not values:
        return text
    # Ordered alternation: at any position Python's regex engine tries the
    # alternatives left-to-right, so longest-first ordering makes the longest
    # matching secret win, matching the rule above.
    pattern = re.compile("|".join(re.escape(v) for v in values))
    return pattern.sub(lambda _m: placeholder, text)
