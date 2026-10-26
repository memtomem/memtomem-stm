"""Wire protocol for the hook↔daemon link.

Dead-simple by design: **newline-delimited JSON, one request line and one
response line per connection** (connection-per-request — no multiplexing, no
session state). ``json.dumps`` escapes any newline inside string values as
``\\n``, so a serialized message is always a single physical line and
``StreamReader.readline()`` is a correct frame boundary.

Request  (client → daemon)::

    {"v": 9, "token": "<hex>", "op": "surface", "payload": {<CanonicalHookCall wire>}}

Response (daemon → hook)::

    {"v": 9, "ok": true, "output": {<hook-output JSON, possibly {}>}}      # surface
    {"v": 9, "ok": true, "status": "ready", "ltm": "warm",
     "latency": {<bounded numeric telemetry>}}                               # ping
    # ping "ltm" is one of warm | warming | down | cold (#664); clients
    # treat it as an opaque display string.

``op`` includes hook ``surface``, lifecycle operations, and the narrowly typed
``ltm_search`` / ``ltm_context_compose`` / ``ltm_candidate_propose`` /
``ltm_scratch_list`` / ``ltm_increment_access`` operations
used by opt-in standalone proxy clients. The
token gates *use* of the loopback port (any local process can ``connect`` to
it); it is validated with :func:`secrets.compare_digest` on every request and
never logged. Messages are capped at :data:`MAX_MESSAGE_BYTES` so a malformed
or oversized stdin can't blow up daemon memory — set the same value as the
stream ``limit`` on both ends so ``readline`` enforces it.

Versioning. ``v`` is :data:`PROTOCOL_VERSION`, checked for an exact match on
both ends (the server rejects a mismatched request, the client discards a
mismatched response). The match is belt-and-suspenders: ``PROTOCOL_VERSION`` is
folded into the daemon's ``config_fingerprint``, so a hook and a daemon built at
different protocol versions key to different handshake/lock paths and never even
discover each other — a version bump makes the two coexist instead of exchanging
frames one side can't parse (the stale daemon idle-times-out under a finite
``idle_timeout_seconds``; a pinned ``idle_timeout_seconds=0`` daemon must be
stopped manually — see #517).

**v9:** refuses invalid raw identities before content escaping in both directions.
Non-finite JSON numbers are rejected. **v8:** the ``surface`` payload carries the host's ``session_id`` / ``cwd`` /
``tool_use_id`` / ``agent_id`` (a v7 daemon would silently drop them). **v7:** adds max/selected context-compose schema negotiation and schema 3
adjacent-context payloads. **v6:** adds schema-bound context-compose scope fields. **v5:** adds
context-compose and review-candidate LTM operations. **v4:** adds
typed low-level LTM operations for issue #688. **v3:** adds
absolute deadlines and bounded admission. **v2:** the ``surface`` payload is a serialized
:class:`~memtomem_stm.cli.hook_adapter.CanonicalHookCall` (``to_wire``), so the
daemon consumes a host-agnostic call and needs no host knowledge. **v1** sent
the host's raw Claude PostToolUse JSON and the daemon parsed it server-side.
"""

from __future__ import annotations

import asyncio
import json
import math
import secrets
from typing import Any

from memtomem_stm.utils.json_out import dumps as _json_dumps
from memtomem_stm.utils.json_out import scrub_lone_surrogates, has_lone_surrogate

PROTOCOL_VERSION = 9

# Upper bound on a single framed message. A ``surface`` request embeds the
# built-in tool's output (a large ``Read`` can be hundreds of KB), so this sits
# well above the asyncio default 64 KiB stream limit; pass it as ``limit=`` to
# both ``start_server`` and ``open_connection``.
MAX_MESSAGE_BYTES = 4 * 1024 * 1024

# Highest ``context_compose`` schema STM understands (core #1796). Single-sources
# the daemon-side ceiling: the server clamp, the adapter's optimistic default,
# and its requested-schema fallback all read this so a future bump is one line
# and the clamp can never drift from the default. The direct McpClient path is
# not bounded here — it trusts whatever schema the core advertises.
MAX_CONTEXT_COMPOSE_SCHEMA = 4

OP_SURFACE = "surface"
OP_LTM_SEARCH = "ltm_search"
OP_LTM_CONTEXT_COMPOSE = "ltm_context_compose"
OP_LTM_CANDIDATE_PROPOSE = "ltm_candidate_propose"
OP_LTM_INCREMENT_ACCESS = "ltm_increment_access"
OP_LTM_SCRATCH_LIST = "ltm_scratch_list"
OP_PING = "ping"
OP_SHUTDOWN = "shutdown"


class ProtocolError(Exception):
    """Raised on a malformed/oversized/closed-early frame. Callers treat it as
    a clean failure: the daemon closes the connection, the hook degrades."""


def encode_line(obj: dict[str, Any]) -> bytes:
    """Serialize ``obj`` to a single newline-terminated UTF-8 frame.

    ``json_out.dumps`` rather than ``json.dumps``: the ``.encode("utf-8")``
    below is exactly the encode a lone surrogate raises on, and a frame that
    cannot be encoded fails the whole connection rather than the request —
    on the daemon side the client just sees the socket close with no response
    (#761). Compact separators are inside that writer's documented contract.
    """
    _validate_wire_identities(obj)
    return (
        _json_dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _reject_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant")


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read and parse one framed JSON object, or raise :class:`ProtocolError`.

    Relies on the stream having been opened with ``limit=MAX_MESSAGE_BYTES`` so
    an over-long line surfaces as a read error rather than unbounded buffering.

    Both ends of the link are made surrogate-safe, not just the write end.
    ``encode_line``'s escape is the *JSON* one, which ``json.loads`` faithfully
    decodes back into the code unit — so escaping only on the way out would
    relocate the failure into the receiving process, at whichever encode it
    reaches next (its reply frame, the hook's output, an outbound LTM call).
    Scrubbing here turns it into inert literal text once, which also covers a
    peer that never escaped it (#761).
    """
    try:
        line = await reader.readline()
    except Exception as exc:  # LimitOverrunError, ValueError, transport errors
        raise ProtocolError(f"frame read failed: {exc}") from exc
    if not line:
        raise ProtocolError("connection closed before a frame was read")
    if len(line) > MAX_MESSAGE_BYTES:
        raise ProtocolError("frame exceeds message limit")
    try:
        obj = json.loads(line, parse_float=_finite_float, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ProtocolError("malformed JSON frame") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("frame is not a JSON object")
    try:
        _validate_wire_identities(obj)
        scrubbed: dict[str, Any] = scrub_lone_surrogates(obj)
    except RecursionError as exc:
        raise ProtocolError("JSON frame is too deeply nested") from exc
    return scrubbed


def build_request(
    token: str,
    op: str,
    payload: dict[str, Any] | None = None,
    *,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    """Construct a request frame body."""
    req: dict[str, Any] = {"v": PROTOCOL_VERSION, "token": token, "op": op}
    if payload is not None:
        req["payload"] = payload
    if deadline_monotonic is not None:
        req["deadline_monotonic"] = deadline_monotonic
    return req


def surface_response(output: dict[str, Any]) -> dict[str, Any]:
    """Construct a successful ``surface`` response carrying the hook-output dict."""
    return {"v": PROTOCOL_VERSION, "ok": True, "output": output}


def token_matches(expected: str, received: Any) -> bool:
    """Constant-time token check tolerant of a missing/wrong-typed value."""
    if not isinstance(received, str) or not received.isascii() or not expected:
        return False
    return secrets.compare_digest(expected, received)


def _validate_wire_identities(obj: dict[str, Any]) -> None:
    """Refuse raw invalid IDs BEFORE content scrubbing, in both directions.

    Only fields used as identities by the typed daemon operations are examined;
    an arbitrary tool_input.id remains ordinary content.
    """

    def identity(value: Any) -> None:
        if not isinstance(value, str) or not value or has_lone_surrogate(value):
            raise ProtocolError("Invalid wire identity")

    payload = obj.get("payload")
    if isinstance(payload, dict):
        op = obj.get("op")
        if op == OP_LTM_INCREMENT_ACCESS:
            ids = payload.get("chunk_ids")
            if not isinstance(ids, list):
                raise ProtocolError("Invalid wire identities")
            for value in ids:
                identity(value)
        if op == OP_LTM_CANDIDATE_PROPOSE:
            identity(payload.get("idempotency_key"))
        if op == OP_SURFACE:
            for name in ("session_id", "tool_use_id", "agent_id"):
                if payload.get(name) is not None:
                    identity(payload[name])

    def items(values: Any) -> None:
        if not isinstance(values, list):
            return
        for item in values:
            if not isinstance(item, dict):
                continue
            for name in ("id", "chunk_id", "block_id"):
                if name in item:
                    identity(item[name])
            ctx = item.get("context")
            if isinstance(ctx, dict):
                items(ctx.get("window_before"))
                items(ctx.get("window_after"))

    for name in ("results", "pinned", "retrieved"):
        items(obj.get(name))
    bundle = obj.get("bundle")
    if isinstance(bundle, dict):
        items(bundle.get("pinned"))
        items(bundle.get("retrieved"))
