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
    PROTOCOL_VERSION,
    build_request,
    encode_line,
    read_message,
)

if TYPE_CHECKING:
    from memtomem_stm.cli.hook_adapter import CanonicalHookCall
    from memtomem_stm.config import STMConfig

logger = logging.getLogger(__name__)


async def _request(
    handshake: dict[str, Any],
    op: str,
    payload: dict[str, Any] | None,
    *,
    timeout: float,
    deadline_monotonic: float | None = None,
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
            writer.write(
                encode_line(
                    build_request(
                        token, op, payload, deadline_monotonic=deadline_monotonic
                    )
                )
            )
            await writer.drain()
            resp = await read_message(reader)
        # Protocol-version guard. A wire-incompatible daemon keys to a different
        # fingerprint and is normally invisible here; this rejects a stray reply
        # (e.g. a hand-crafted peer, or a future renegotiation path) rather than
        # acting on a frame shape this client may not understand.
        if resp.get("v") != PROTOCOL_VERSION:
            logger.debug(
                "daemon response protocol mismatch (got v=%s, want %s)",
                resp.get("v"),
                PROTOCOL_VERSION,
            )
            return None
        return resp
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
    config: STMConfig, call: "CanonicalHookCall", *, timeout: float
) -> dict[str, Any] | None:
    """Round-trip a ``surface`` request for a normalized :class:`CanonicalHookCall`.

    Sends the call's host-agnostic wire form (``to_wire``) so the daemon needs no
    host knowledge. Returns the hook-output dict, or ``None`` if the daemon is
    unavailable/stale/slow/version-mismatched (caller degrades)."""
    hs = _live_handshake_candidate(config)
    if hs is None:
        return None
    deadline = asyncio.get_running_loop().time() + timeout
    resp = await _request(
        hs, OP_SURFACE, call.to_wire(), timeout=timeout, deadline_monotonic=deadline
    )
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


# Bare connect-probe — the cross-platform liveness discriminator for the
# foreign-orphan and same-config SIGTERM paths (see #519). Unlike ``ping``, it
# sends no protocol frame and needs no token, so it tells a *listening* daemon
# (any ``PROTOCOL_VERSION``) apart from a stale handshake naming a dead or
# OS-recycled pid: the former still accepts a connect, the latter's port is not
# bound. It proves only that *something* accepts on ``host:port`` — not that it
# is the recorded pid (an ephemeral port could be reassigned) — so callers pair
# it with ``is_pid_alive`` for a two-factor check, materially stronger than pid
# alone yet still a heuristic, not identity proof.
PROBE_TIMEOUT_SECONDS = 1.0


async def _can_connect(host: object, port: object, *, timeout: float) -> bool:
    """Open then immediately close a TCP connection to ``host:port``.

    ``True`` iff the connect succeeds within ``timeout``. No bytes are sent, so a
    foreign daemon at an incompatible protocol still answers. Any failure
    (refused, unreachable, malformed endpoint, over-budget) is a quiet ``False``;
    on loopback a closed port is refused instantly, so the timeout only bites a
    genuinely wedged peer.
    """
    if not isinstance(host, str) or not isinstance(port, int) or port <= 0:
        return False
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(timeout):
            _reader, writer = await asyncio.open_connection(host, port, limit=MAX_MESSAGE_BYTES)
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    except Exception:
        logger.debug("connect probe failed (%s:%s)", host, port, exc_info=True)
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                pass


def probe_listening(host: object, port: object, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """Sync wrapper over :func:`_can_connect` for the CLI ops paths.

    ``True`` iff a TCP connect to ``host:port`` succeeds — i.e. a daemon (of any
    protocol version) is still accepting there. Used by ``mms daemon status`` /
    ``stop`` to gate action on a recorded pid behind proof its endpoint is live.
    """
    return asyncio.run(_can_connect(host, port, timeout=timeout))
