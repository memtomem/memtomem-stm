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
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=f"***@{host}"))
