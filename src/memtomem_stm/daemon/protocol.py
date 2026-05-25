"""Wire protocol for the hook↔daemon link.

Dead-simple by design: **newline-delimited JSON, one request line and one
response line per connection** (connection-per-request — no multiplexing, no
session state). ``json.dumps`` escapes any newline inside string values as
``\\n``, so a serialized message is always a single physical line and
``StreamReader.readline()`` is a correct frame boundary.

Request  (hook → daemon)::

    {"v": 1, "token": "<hex>", "op": "surface", "payload": {<PostToolUse JSON>}}

Response (daemon → hook)::

    {"v": 1, "ok": true, "output": {<hook-output JSON, possibly {}>}}      # surface
    {"v": 1, "ok": true, "status": "ready", "ltm": "warm"}                 # ping

``op`` is one of :data:`OP_SURFACE`, :data:`OP_PING`, :data:`OP_SHUTDOWN`. The
token gates *use* of the loopback port (any local process can ``connect`` to
it); it is validated with :func:`secrets.compare_digest` on every request and
never logged. Messages are capped at :data:`MAX_MESSAGE_BYTES` so a malformed
or oversized stdin can't blow up daemon memory — set the same value as the
stream ``limit`` on both ends so ``readline`` enforces it.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

PROTOCOL_VERSION = 1

# Upper bound on a single framed message. A ``surface`` request embeds the
# built-in tool's output (a large ``Read`` can be hundreds of KB), so this sits
# well above the asyncio default 64 KiB stream limit; pass it as ``limit=`` to
# both ``start_server`` and ``open_connection``.
MAX_MESSAGE_BYTES = 4 * 1024 * 1024

OP_SURFACE = "surface"
OP_PING = "ping"
OP_SHUTDOWN = "shutdown"


class ProtocolError(Exception):
    """Raised on a malformed/oversized/closed-early frame. Callers treat it as
    a clean failure: the daemon closes the connection, the hook degrades."""


def encode_line(obj: dict[str, Any]) -> bytes:
    """Serialize ``obj`` to a single newline-terminated UTF-8 frame."""
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read and parse one framed JSON object, or raise :class:`ProtocolError`.

    Relies on the stream having been opened with ``limit=MAX_MESSAGE_BYTES`` so
    an over-long line surfaces as a read error rather than unbounded buffering.
    """
    try:
        line = await reader.readline()
    except Exception as exc:  # LimitOverrunError, ValueError, transport errors
        raise ProtocolError(f"frame read failed: {exc}") from exc
    if not line:
        raise ProtocolError("connection closed before a frame was read")
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProtocolError("malformed JSON frame") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("frame is not a JSON object")
    return obj


def build_request(token: str, op: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Construct a request frame body."""
    req: dict[str, Any] = {"v": PROTOCOL_VERSION, "token": token, "op": op}
    if payload is not None:
        req["payload"] = payload
    return req


def surface_response(output: dict[str, Any]) -> dict[str, Any]:
    """Construct a successful ``surface`` response carrying the hook-output dict."""
    return {"v": PROTOCOL_VERSION, "ok": True, "output": output}


def token_matches(expected: str, received: Any) -> bool:
    """Constant-time token check tolerant of a missing/wrong-typed value."""
    if not isinstance(received, str) or not expected:
        return False
    return secrets.compare_digest(expected, received)
