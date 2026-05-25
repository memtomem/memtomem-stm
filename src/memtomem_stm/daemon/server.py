"""The daemon server loop — one warm engine behind a loopback socket.

Holds a single long-lived :class:`SurfacingEngine` and a single lazily-started
LTM ``McpClientSearchAdapter`` for the process lifetime, so each ``mms hook``
call is a sub-second round trip instead of a ~6s cold start. Requests are
token-authenticated and dispatched over the newline-JSON
:mod:`~memtomem_stm.daemon.protocol`.

Single-instance is enforced by the lifetime ownership lock
(:mod:`~memtomem_stm.daemon.locking`): ``serve`` acquires it (with a brief
retry) **before** building the engine and holds it until teardown, so a
redundant/late daemon exits without warming an LTM. This replaces the older
``ping``-based guard, closing its TOCTOU window and recycled-PID weakness.

Engine wiring is the **dedup-only** variant of ``server.py``'s app_lifespan: a
``FeedbackTracker`` is attached so cross-session dedup (``seen_memories``)
survives restarts, but ``record_feedback_events=False`` means no query text is
persisted and no rating prompt is emitted (the pure-hook path has no in-band
feedback channel, and Bash queries may carry secrets). ``persist_query_text``
is additionally forced off as defense-in-depth.

Concurrency: LTM RPCs share one MCP ``ClientSession`` whose stdio framing is not
safe under interleaved calls, so ``surface`` requests are serialized on a single
lock. A warm search is sub-second and the engine's result cache absorbs repeats,
so this is acceptable for the MVP; narrowing the lock to just the adapter RPC is
a later optimization.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import time
from typing import Any

from memtomem_stm.cli.hook_cmd import run_surfacing_hook
from memtomem_stm.config import STMConfig
from memtomem_stm.daemon import discovery, locking
from memtomem_stm.daemon.protocol import (
    MAX_MESSAGE_BYTES,
    OP_PING,
    OP_SHUTDOWN,
    OP_SURFACE,
    PROTOCOL_VERSION,
    ProtocolError,
    encode_line,
    read_message,
    surface_response,
    token_matches,
)

logger = logging.getLogger(__name__)

# Per-frame read budget. Bounds a wedged or half-open client from holding a
# handler open forever; well above a warm surface round trip.
_READ_TIMEOUT_SECONDS = 30.0


async def _quiet(coro: Any, what: str) -> None:
    """Await a cleanup coroutine, swallowing (and debug-logging) any error."""
    try:
        await coro
    except Exception:
        logger.debug("%s failed", what, exc_info=True)


class DaemonServer:
    """A single-process loopback surfacing daemon."""

    # Lifetime-lock acquire: retry briefly so a losing/late child becomes a warm
    # standby that grabs the lock the instant an incumbent releases (closes the
    # crash-during-build dead window) without ever building an engine to lose it.
    _LOCK_ACQUIRE_RETRY_SECONDS = 1.5
    _LOCK_ACQUIRE_POLL_SECONDS = 0.05

    def __init__(self, config: STMConfig) -> None:
        self._config = config
        self._host = config.daemon.host
        self._idle_timeout = config.daemon.idle_timeout_seconds
        # Freeze the config fingerprint at construction and reuse it for the lock
        # path, handshake path, and handshake content — one value, so the lock we
        # hold, the file we publish, and the fingerprint we advertise can never
        # disagree (and a mid-run env change can't repoint us at a different file).
        self._fingerprint = discovery.config_fingerprint(config)
        self._token = secrets.token_hex(32)
        self._port = 0
        self._started_at = time.time()
        self._last_request = time.monotonic()
        self._shutdown_event = asyncio.Event()
        self._surface_lock = asyncio.Lock()
        self._handshake_written = False
        self._engine: Any = None
        self._adapter: Any = None
        self._tracker: Any = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def serve(self) -> int:
        """Run until shutdown (signal, idle timeout, or ``shutdown`` op).

        Holds the lifetime ownership lock for the whole run: a second daemon
        that can't acquire it (within a brief retry window) exits *before*
        warming an LTM, so "lock held" is the authoritative single-owner signal.
        Returns a process exit code.
        """
        try:
            fd = locking.open_lock_fd(locking.lock_path(self._config.data_dir, self._fingerprint))
        except OSError:
            logger.warning("daemon could not open the lock file — exiting", exc_info=True)
            return 0
        try:
            if not await self._acquire_lifetime_lock(fd):
                return 0
            return await self._serve_owned()
        finally:
            locking.release_lock(fd)

    async def _acquire_lifetime_lock(self, fd: int) -> bool:
        """Take the lifetime lock, retrying briefly so a losing/late child becomes
        a warm standby that grabs it the instant an incumbent releases. Retry the
        lock acquisition ONLY — the (expensive) engine build happens once, after.
        Async sleep (never ``time.sleep``) so it doesn't block the event loop."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._LOCK_ACQUIRE_RETRY_SECONDS
        while not locking.try_lock(fd):
            if loop.time() >= deadline:
                # Best-effort pid for a friendly log; gate nothing on it.
                raw = discovery.read_handshake(
                    discovery.handshake_path(self._config.data_dir, self._fingerprint)
                )
                pid = raw.get("pid") if isinstance(raw, dict) else None
                logger.warning("another daemon already owns the lock (pid=%s) — exiting", pid)
                return False
            await asyncio.sleep(self._LOCK_ACQUIRE_POLL_SECONDS)
        return True

    async def _serve_owned(self) -> int:
        """The serve body, run with the lifetime lock held."""
        self._build_engine()
        self._last_request = time.monotonic()
        server = await asyncio.start_server(
            self._handle_conn, self._host, 0, limit=MAX_MESSAGE_BYTES
        )
        self._port = server.sockets[0].getsockname()[1]
        self._install_signals()
        discovery.write_handshake(
            discovery.handshake_path(self._config.data_dir, self._fingerprint),
            pid=os.getpid(),
            host=self._host,
            port=self._port,
            token=self._token,
            config_fingerprint=self._fingerprint,
            created_at=self._started_at,
        )
        self._handshake_written = True
        logger.info("STM daemon listening on %s:%d (pid=%d)", self._host, self._port, os.getpid())

        idle_task = asyncio.create_task(self._idle_watch())
        try:
            async with server:
                await self._shutdown_event.wait()
        finally:
            idle_task.cancel()
            try:
                await idle_task
            except (asyncio.CancelledError, Exception):
                pass
            await self._teardown()
        logger.info("STM daemon stopped")
        return 0

    def _build_engine(self) -> None:
        """Construct the single warm engine + adapter (+ dedup tracker)."""
        from memtomem_stm.surfacing.engine import SurfacingEngine
        from memtomem_stm.surfacing.feedback import FeedbackTracker
        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter
        from memtomem_stm.surfacing.observability import SurfacingObservability

        record_events = self._config.hook.record_feedback_events
        updates: dict[str, Any] = {
            # Defense-in-depth: the hook/daemon path never persists raw query
            # text, even if an operator flips feedback events on.
            # ``record_feedback_events`` already gates whether any event row is
            # written at all.
            "persist_query_text": False,
            # Disable the result cache. A cache hit intentionally short-circuits
            # the in-session ``_surfaced_ids`` dedup (documented engine
            # behavior), which in a long-lived warm daemon would re-inject an
            # already-seen memory for a repeated identical query within the TTL
            # window. The daemon serializes requests against a warm LTM
            # connection, so the cache's stampede/latency benefit is redundant
            # here — turning it off makes dedup (``_surfaced_ids`` +
            # ``seen_memories``) authoritative.
            "cache_ttl_seconds": 0.0,
        }
        if not record_events:
            # Dedup-only: the tracker exists solely for ``seen_memories``. Don't
            # let AutoTuner learn min_score from feedback rows (possibly written
            # by the proxy path sharing the same DB) — the daemon's ranking must
            # be self-contained, not nudged by feedback this mode never records.
            updates["auto_tune_enabled"] = False
        surfacing_cfg = self._config.surfacing.model_copy(update=updates)

        self._adapter = McpClientSearchAdapter(surfacing_cfg)  # lazy LTM start
        want_tracker = surfacing_cfg.dedup_ttl_seconds > 0 or record_events
        if want_tracker:
            try:
                self._tracker = FeedbackTracker(surfacing_cfg)
            except Exception:
                logger.warning(
                    "FeedbackTracker init failed — cross-session dedup disabled", exc_info=True
                )
                self._tracker = None
        self._engine = SurfacingEngine(
            surfacing_cfg,
            mcp_adapter=self._adapter,
            feedback_tracker=self._tracker,
            observability=SurfacingObservability(),
            record_feedback_events=record_events,
        )
        logger.info(
            "daemon engine wired: dedup=%s record_feedback_events=%s",
            "on" if self._tracker is not None else "off",
            record_events,
        )

    async def _teardown(self) -> None:
        """Graceful resource release, mirroring app_lifespan's order: engine →
        tracker → LTM adapter (terminates the warm LTM child), then handshake."""
        if self._engine is not None:
            await _quiet(self._engine.stop(), "engine stop")
        if self._tracker is not None:
            try:
                self._tracker.close()
            except Exception:
                logger.debug("tracker close failed", exc_info=True)
        if self._adapter is not None:
            await _quiet(self._adapter.stop(), "LTM adapter stop")
        if self._handshake_written:
            discovery.remove_handshake_if_owner(
                discovery.handshake_path(self._config.data_dir, self._fingerprint),
                pid=os.getpid(),
                token=self._token,
            )

    # ── connection handling ──────────────────────────────────────────────

    async def _handle_conn(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            try:
                req = await asyncio.wait_for(read_message(reader), timeout=_READ_TIMEOUT_SECONDS)
            except (ProtocolError, asyncio.TimeoutError):
                return  # malformed/oversized/slow → drop silently
            if not token_matches(self._token, req.get("token")):
                logger.debug("daemon rejected request with bad/missing token")
                return  # never respond to an unauthenticated peer
            resp = await self._dispatch(req)
            if resp is not None:
                writer.write(encode_line(resp))
                await writer.drain()
        except Exception:
            logger.debug("daemon connection handler error", exc_info=True)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, req: dict[str, Any]) -> dict[str, Any] | None:
        op = req.get("op")
        if op == OP_PING:
            return {"v": PROTOCOL_VERSION, "ok": True, "status": "ready", "ltm": self._ltm_warmth()}
        if op == OP_SHUTDOWN:
            logger.info("daemon received shutdown request")
            self._shutdown_event.set()
            return {"v": PROTOCOL_VERSION, "ok": True, "status": "shutting_down"}
        if op == OP_SURFACE:
            payload = req.get("payload")
            if not isinstance(payload, dict):
                return surface_response({})
            self._last_request = time.monotonic()
            # Serialize: one LTM RPC at a time over the shared MCP session.
            async with self._surface_lock:
                output = await run_surfacing_hook(payload, engine=self._engine)
            return surface_response(output)
        return {"v": PROTOCOL_VERSION, "ok": False, "error": "unknown op"}

    def _ltm_warmth(self) -> str:
        """Best-effort LTM connection state for ``ping`` (status nicety)."""
        if getattr(self._adapter, "_session", None) is not None:
            return "warm"
        if getattr(self._adapter, "_start_attempted", False):
            return "down"
        return "cold"

    # ── idle + signals ────────────────────────────────────────────────────

    async def _idle_watch(self) -> None:
        if self._idle_timeout <= 0:
            return
        interval = min(30.0, max(1.0, self._idle_timeout / 2))
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._shutdown_event.is_set():
                return
            if time.monotonic() - self._last_request >= self._idle_timeout:
                logger.info("daemon idle for %.0fs — shutting down", self._idle_timeout)
                self._shutdown_event.set()
                return

    def _install_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._shutdown_event.set)
            except (NotImplementedError, RuntimeError):
                # Windows: no add_signal_handler — fall back to signal.signal,
                # which is also what ``mms daemon stop`` relies on via the
                # ``shutdown`` op as the primary path there.
                try:
                    signal.signal(sig, self._signal_fallback)
                except (ValueError, OSError):  # pragma: no cover - platform/thread dependent
                    pass

    def _signal_fallback(self, signum: int, frame: Any) -> None:  # pragma: no cover - Windows
        try:
            asyncio.get_event_loop().call_soon_threadsafe(self._shutdown_event.set)
        except Exception:
            pass


def run(config: STMConfig | None = None) -> int:
    """Synchronous entry point used by ``mms daemon run``."""
    cfg = config if config is not None else STMConfig()
    return asyncio.run(DaemonServer(cfg).serve())
