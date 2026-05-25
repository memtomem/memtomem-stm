"""Client side of the hook↔daemon link.

Shared by ``mms hook`` (hot path) and ``mms daemon status/start`` (ops). Every
helper is failure-tolerant: a missing/stale handshake, a refused connect, a
fingerprint mismatch, a timeout, or a malformed reply all return ``None`` (or
``False``) so the caller degrades cleanly — the hook to ``{}``, the CLI to a
"not running" report. Nothing here ever raises on an unreachable daemon.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from memtomem_stm.daemon.discovery import (
    HANDSHAKE_VERSION,
    config_fingerprint,
    handshake_path,
    read_handshake,
)
from memtomem_stm.daemon.protocol import (
    MAX_MESSAGE_BYTES,
    OP_PING,
    OP_SHUTDOWN,
    OP_SURFACE,
    build_request,
    encode_line,
    read_message,
)

if TYPE_CHECKING:
    from memtomem_stm.config import STMConfig

logger = logging.getLogger(__name__)


async def _request(
    handshake: dict[str, Any], op: str, payload: dict[str, Any] | None, *, timeout: float
) -> dict[str, Any] | None:
    """One connection-per-request round trip. ``None`` on any failure.

    ``timeout`` is a single wall-clock budget for connect + write + read
    together (via ``asyncio.timeout``), not applied per-step — so a half-open
    peer can't stretch the call to roughly ``2 * timeout``.
    """
    token = handshake.get("token")
    host = handshake.get("host")
    port = handshake.get("port")
    if not isinstance(token, str) or not isinstance(host, str) or not isinstance(port, int):
        return None
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(host, port, limit=MAX_MESSAGE_BYTES)
            writer.write(encode_line(build_request(token, op, payload)))
            await writer.drain()
            return await read_message(reader)
    except (OSError, asyncio.TimeoutError):
        return None  # unreachable / refused / over-budget — quiet
    except Exception:
        logger.debug("daemon request failed (op=%s)", op, exc_info=True)
        return None
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                pass


def _live_handshake_candidate(config: STMConfig) -> dict[str, Any] | None:
    """Read *this config's* handshake and reject it on version/fingerprint drift.

    The handshake path is keyed by ``config``'s fingerprint, so we only ever read
    the file a daemon under *our* config published — a different-config daemon's
    handshake lives at a different path and is invisible here. The in-file
    ``config_fingerprint`` check is then a belt-and-suspenders guard against a
    corrupted/hand-edited file whose content doesn't match its keyed name; a
    version mismatch likewise means "not the daemon we want".
    """
    fp = config_fingerprint(config)
    hs = read_handshake(handshake_path(config.data_dir, fp))
    if hs is None:
        return None
    if hs.get("v") != HANDSHAKE_VERSION:
        return None
    if hs.get("config_fingerprint") != fp:
        return None
    return hs


async def ping(config: STMConfig, *, timeout: float = 2.0) -> dict[str, Any] | None:
    """Return the handshake dict iff a live, matching daemon answers ``ping``."""
    hs = _live_handshake_candidate(config)
    if hs is None:
        return None
    resp = await _request(hs, OP_PING, None, timeout=timeout)
    if resp is not None and resp.get("ok"):
        # Fold the ping status (e.g. LTM warmth) into the returned handshake so
        # callers like ``mms daemon status`` can render it without a 2nd trip.
        merged = dict(hs)
        merged["ltm"] = resp.get("ltm")
        return merged
    return None


async def surface(
    config: STMConfig, payload: dict[str, Any], *, timeout: float
) -> dict[str, Any] | None:
    """Round-trip a ``surface`` request. Returns the hook-output dict, or
    ``None`` if the daemon is unavailable/stale/slow (caller degrades)."""
    hs = _live_handshake_candidate(config)
    if hs is None:
        return None
    resp = await _request(hs, OP_SURFACE, payload, timeout=timeout)
    if resp is not None and resp.get("ok") and isinstance(resp.get("output"), dict):
        return resp["output"]
    return None


async def shutdown(config: STMConfig, *, timeout: float = 5.0) -> bool:
    """Ask a running daemon to shut down gracefully. ``True`` if it acked."""
    hs = _live_handshake_candidate(config)
    if hs is None:
        return False
    resp = await _request(hs, OP_SHUTDOWN, None, timeout=timeout)
    return resp is not None and bool(resp.get("ok"))
