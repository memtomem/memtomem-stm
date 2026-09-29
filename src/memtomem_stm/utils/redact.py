"""Display/log redaction helpers."""

from __future__ import annotations

import re
import sys
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


def root_cause_exc(exc: BaseException) -> BaseException:
    """Walk into ``BaseExceptionGroup`` (anyio TaskGroup wraps probe failures
    as ``unhandled errors in a TaskGroup (N sub-exception)``) to surface the
    first non-group leaf so callers can dispatch on the real cause's type
    or message instead of the wrapper.
    """
    seen: set[int] = set()
    cur: BaseException = exc
    while isinstance(cur, BaseExceptionGroup) and cur.exceptions and id(cur) not in seen:
        seen.add(id(cur))
        cur = cur.exceptions[0]
    return cur


def _loaded_class(module: str, name: str) -> type[BaseException] | None:
    """*name* from *module* if that module is already imported, else ``None``.

    These helpers run inside ``except`` blocks, often on the event loop, so
    they never import. Nothing is lost: an exception can only be an instance of
    a class whose module is loaded. Importing here instead cost a cold
    ``mcp.shared.exceptions`` import (~240 ms) on the loop, and raised anew
    when the library could not be imported (#1082).
    """
    cls = getattr(sys.modules.get(module), name, None)
    return cls if isinstance(cls, type) and issubclass(cls, BaseException) else None


def http_status_code(exc: BaseException) -> int | None:
    """The status code of an httpx/httpx2 ``HTTPStatusError``, else ``None``.

    Only those two classes are trusted: another exception's ``response``
    attribute could carry anything into the rendered error.
    """
    trusted = tuple(
        cls
        for cls in (
            _loaded_class("httpx", "HTTPStatusError"),
            _loaded_class("httpx2", "HTTPStatusError"),
        )
        if cls is not None
    )
    if not trusted or not isinstance(exc, trusted):
        return None
    code = exc.response.status_code  # type: ignore[attr-defined]
    return code if type(code) is int else None


class ResponseShapeError(ValueError):
    """A reply failed STM's own shape check, described by STM.

    Raised where STM validates what a core, embedding provider or LLM sent back
    and writes the reason itself: which key is missing, what type a field has,
    a count or a dimension. The message never quotes STM's request, so
    ``exception_summary`` shows it — it is the only way an operator learns which
    side drifted (#1082). Anything that would interpolate request data, a URL
    or an upstream's free text must raise something else.
    """


# The JSON-RPC 2.0 reserved codes. Any other code is upstream-chosen and could
# itself carry a value, so it is not shown (#1082).
# The rendered text comes from this table, never from the code value itself.
_JSONRPC_STANDARD_ERRORS: dict[int, str] = {
    -32700: "-32700 (Parse error)",
    -32600: "-32600 (Invalid Request)",
    -32601: "-32601 (Method not found)",
    -32602: "-32602 (Invalid params)",
    -32603: "-32603 (Internal error)",
}


# SQLite's primary result codes (the low byte of an extended code). The
# message of a SQLite error can quote a table name out of the schema or part of
# the statement, so it is never shown; the code names the failure class
# instead, and the rendered name comes from this table, not from the exception
# (#1082).
_SQLITE_PRIMARY_CODES: dict[int, str] = {
    1: "SQLITE_ERROR",
    2: "SQLITE_INTERNAL",
    3: "SQLITE_PERM",
    4: "SQLITE_ABORT",
    5: "SQLITE_BUSY",
    6: "SQLITE_LOCKED",
    7: "SQLITE_NOMEM",
    8: "SQLITE_READONLY",
    9: "SQLITE_INTERRUPT",
    10: "SQLITE_IOERR",
    11: "SQLITE_CORRUPT",
    12: "SQLITE_NOTFOUND",
    13: "SQLITE_FULL",
    14: "SQLITE_CANTOPEN",
    15: "SQLITE_PROTOCOL",
    16: "SQLITE_EMPTY",
    17: "SQLITE_SCHEMA",
    18: "SQLITE_TOOBIG",
    19: "SQLITE_CONSTRAINT",
    20: "SQLITE_MISMATCH",
    21: "SQLITE_MISUSE",
    22: "SQLITE_NOLFS",
    23: "SQLITE_AUTH",
    24: "SQLITE_FORMAT",
    25: "SQLITE_RANGE",
    26: "SQLITE_NOTADB",
    27: "SQLITE_NOTICE",
    28: "SQLITE_WARNING",
}


def exception_summary(exc: BaseException) -> str:
    """An exception as the fixed vocabulary shared by every error surface.

    Never the exception's message: servers and SDKs quote the request URL
    (query included), an argument, or part of a header back in it, in forms no
    value list can anticipate (#1079, #1082). What is left is the root cause's
    type name, the status code of an HTTP error, a SQLite error's primary
    result-code name, a ``ResponseShapeError``'s
    STM-written message, a JSON-RPC error's code when
    it is one of the reserved ones, and — for a pydantic ``ValidationError`` —
    its error types. Not its locations: a location can be a key of the data that
    failed, and nothing marks which parts the schema owns. Not a JSON-RPC
    error's message either: an upstream can quote STM's request in it.
    """
    from pydantic import ValidationError

    mcp_error = _loaded_class("mcp.shared.exceptions", "MCPError")
    root = root_cause_exc(exc)
    if isinstance(root, ResponseShapeError):
        return f"{type(root).__name__}: {root}"
    if mcp_error is not None and isinstance(root, mcp_error):
        code = getattr(getattr(root, "error", None), "code", None)
        reserved = _JSONRPC_STANDARD_ERRORS.get(code) if type(code) is int else None
        if reserved is not None:
            return f"{type(root).__name__} {reserved}"
        return type(root).__name__
    if isinstance(root, ValidationError):
        kinds = dict.fromkeys(
            err["type"] for err in root.errors(include_url=False, include_input=False)
        )
        return f"ValidationError: {'; '.join(kinds)}"
    code = http_status_code(root)
    if code is not None:
        return f"HTTP {code} ({type(root).__name__})"
    sqlite_error = _loaded_class("sqlite3", "Error")
    if sqlite_error is not None and isinstance(root, sqlite_error):
        result = getattr(root, "sqlite_errorcode", None)
        name = _SQLITE_PRIMARY_CODES.get(result & 0xFF) if type(result) is int else None
        if name is not None:
            return f"{type(root).__name__} ({name})"
    return type(root).__name__


def diagnostic_url(url: str) -> str:
    """A configured URL as diagnostics show it: scheme, host, path.

    Userinfo becomes ``***@`` (``redact_url_userinfo``), and the query and
    fragment are dropped, since tokens are passed there too (#1079). Any
    ``@`` the parsed netloc does not hold fails closed as
    ``<unparseable url>``: a ``?``, ``#`` or ``/`` inside the userinfo
    (``http://u:p?x@host``, ``http://u/p@host``) makes the parser read part of
    it as the host or path, and no rule on the URL's shape can tell such a
    value from a legitimate ``@`` in a path or query, which fails closed too.
    An encoded ``@`` in the netloc and an unreadable port are treated the same
    way.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        return "<unparseable url>"
    if url.count("@") > parts.netloc.count("@") or "%40" in parts.netloc.lower():
        return "<unparseable url>"
    shown = redact_url_userinfo(url)
    if shown == "<unparseable url>":
        return shown
    return urlunsplit(urlsplit(shown)._replace(query="", fragment=""))
