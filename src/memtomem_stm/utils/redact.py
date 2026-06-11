"""Display/log redaction helpers."""

from __future__ import annotations

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
