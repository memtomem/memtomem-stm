"""The daemon server loop — one warm engine behind a loopback socket.

Holds a single long-lived :class:`SurfacingEngine` and a single LTM
``McpClientSearchAdapter`` for the process lifetime — warmed at startup by a
background task when ``surfacing.warmup_enabled`` (#664), lazily started on
first use otherwise — so each ``mms hook`` call reuses the warm connection
instead of paying connection startup again. Actual latency is exposed through
the additive ping telemetry rather than assumed to be sub-second. Requests are
token-authenticated and dispatched over the newline-JSON
:mod:`~memtomem_stm.daemon.protocol`.

Single-instance is enforced by the lifetime ownership lock
(:mod:`~memtomem_stm.daemon.locking`): ``serve`` acquires it (with a brief
retry) **before** building the engine and holds it until teardown, so a
redundant/late daemon exits without warming an LTM. This replaces the older
``ping``-based guard, closing its TOCTOU window and recycled-PID weakness.

Engine wiring is the **feedback-loop-off** variant of ``server.py``'s
app_lifespan: a ``FeedbackTracker`` is attached so cross-session dedup
(``seen_memories``) survives restarts and successful surfacing is recorded as
``surfacing_events`` telemetry (``server='builtin'``, query digest-substituted
via the forced ``persist_query_text=False``), but
``record_feedback_events=False`` means no rating prompt is emitted (the
pure-hook path has no in-band feedback channel) and no demotion read runs.

Concurrency: LTM RPCs share one MCP ``ClientSession`` whose stdio framing is not
safe under interleaved calls, so ``surface`` requests are serialized on a single
lock. A warm search is sub-second and the engine's result cache absorbs repeats,
so this is acceptable for the MVP; narrowing the lock to just the adapter RPC is
a later optimization.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import secrets
import signal
import time
from collections.abc import Awaitable, Callable
from typing import Any

from memtomem_stm.cli.hook_adapter import CanonicalHookCall
from memtomem_stm.cli.hook_cmd import run_surfacing_hook
from memtomem_stm.config import STMConfig, _is_loopback_host, log_stm_config_failure
from memtomem_stm.daemon import discovery, locking
from memtomem_stm.daemon.latency import DaemonLatencyTracker, LatencyKind, LatencyOutcome
from memtomem_stm.utils import child_reaper
from memtomem_stm.utils.anyio_shutdown import is_clean_cancel_scope_shutdown
from memtomem_stm.daemon.protocol import (
    MAX_CONTEXT_COMPOSE_SCHEMA,
    MAX_MESSAGE_BYTES,
    OP_LTM_CANDIDATE_PROPOSE,
    OP_LTM_CONTEXT_COMPOSE,
    OP_LTM_INCREMENT_ACCESS,
    OP_LTM_SCRATCH_LIST,
    OP_LTM_SEARCH,
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

# Outbound write+drain budget — symmetric to the read bound. A peer that sent
# its request and then stopped reading (half-open socket, full receive buffer)
# would otherwise block drain() forever, pinning the handler task and its
# buffered response for the process lifetime.
_WRITE_TIMEOUT_SECONDS = 30.0


async def _quiet(coro: Any, what: str) -> None:
    """Await a cleanup coroutine, swallowing (and debug-logging) any error."""
    try:
        await coro
    except Exception:
        logger.debug("%s failed", what, exc_info=True)


# The sweep itself lives in ``utils.child_reaper`` — the stdio MCP server needs
# the same machinery (#906) and must not import this module to get it. What the
# daemon's own sweep varies is kept here under the names it has always used, so
# its call sites and the tests that monkeypatch them by attribute are unchanged.
# ``signal_pid`` is deliberately not re-aliased: the escalation resolves it
# inside ``child_reaper``, so an alias here would look patchable and silently
# not be.
_direct_child_pids = child_reaper.direct_child_pids
_LEAK_KILL_ESCALATE_SECONDS = child_reaper.LEAK_KILL_ESCALATE_SECONDS


async def _terminate_leaked_children(pids: set[int]) -> None:
    """SIGTERM each leaked child's process group, then SIGKILL stragglers."""
    # Reads the module constant per call rather than binding the shared default
    # at import, so shrinking ``_LEAK_KILL_ESCALATE_SECONDS`` still shortens the
    # escalation the way it did when the body lived here.
    await child_reaper.terminate_leaked_children(pids, escalate_seconds=_LEAK_KILL_ESCALATE_SECONDS)


# Reserved out of the client's deadline for encoding + the loopback write/read
# of the response. The engine's deadline is the client's minus this margin, so
# the engine's own timeout fires *before* the client gives up and
# `_run_admitted`'s `asyncio.timeout_at` backstop cancels it from outside. The
# margin no longer has to be sized to absorb anything else (#720): the engine
# re-reads the clock right before its LTM attempt, so its pre-timeout work
# debits its own window, and it books its timeout off which timer fired rather
# than off having beaten the backstop by enough wall clock.
_DEADLINE_RESPONSE_MARGIN_SECONDS = 0.15

# Below this, an LTM round trip cannot plausibly complete, so starting one only
# cancels the adapter mid-RPC — which forces a stdio child respawn on the next
# call (#290/#296) and buys nothing. Skip instead.
_MIN_SURFACE_BUDGET_SECONDS = 0.25


def _usable_deadline(req: dict[str, Any]) -> float | None:
    """The request's ``deadline_monotonic`` as a float, or ``None`` when it
    is not a usable monotonic point.

    Rejects non-numbers and ``bool`` (JSON ``true`` is not a deadline); an
    int too large for a float (JSON integers are unbounded, and ``float()``
    raises ``OverflowError`` on one rather than answering); and non-finite
    values — ``NaN`` compares ``False`` against everything, so it would slip
    past an expiry comparison and reach ``asyncio.timeout_at(NaN)``, and
    ``±inf`` is a backstop that can never fire (or one that already has).
    Callers decide what "no usable deadline" means for them:
    ``_run_admitted`` answers ``expired``, ``_surface_deadline`` starts no
    LTM attempt.
    """
    deadline = req.get("deadline_monotonic")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return None
    try:
        value = float(deadline)
    except OverflowError:
        return None
    if not math.isfinite(value):
        return None
    return value


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
        self._max_pending_requests = config.daemon.max_pending_requests
        self._pending_slots = asyncio.Semaphore(self._max_pending_requests)
        self._active_requests = 0
        self._busy_rejections = 0
        self._latency = DaemonLatencyTracker()
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
        warmup_task: asyncio.Task[None] | None = None
        # ``_adapter is not None`` matters beyond type-narrowing: tests stub
        # ``_build_engine`` with an engine-only wiring that leaves the
        # adapter unset.
        if self._adapter is not None and self._config.surfacing.warmup_enabled:
            warmup_task = asyncio.create_task(self._adapter.warm_up(), name="ltm-warmup")
        try:
            async with server:
                await self._shutdown_event.wait()
        finally:
            if warmup_task is not None:
                # Cancelling mid-start abandons the op (#664) — it finishes
                # in the adapter's owner task and ``_teardown``'s
                # ``adapter.stop()`` bounded join closes it in-task.
                warmup_task.cancel()
                try:
                    await warmup_task
                except (asyncio.CancelledError, Exception):
                    pass
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
            # The hook/daemon path never persists raw query text, even if an
            # operator flips feedback events on. This is the guarantee that
            # matters here: ``surfacing_events`` telemetry rows ARE written on
            # this path (``server='builtin'``), with the query
            # digest-substituted by ``_persistable_query``.
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
            # Feedback loop off: the tracker still writes ``seen_memories``
            # dedup and ``surfacing_events`` telemetry, but this mode emits no
            # rating prompt so it never generates feedback rows. Don't let
            # AutoTuner learn min_score from feedback rows (possibly written
            # by the proxy path sharing the same DB) — the daemon's ranking must
            # be self-contained, not nudged by feedback this mode never records.
            updates["auto_tune_enabled"] = False
        surfacing_cfg = self._config.surfacing.model_copy(update=updates)

        self._adapter = McpClientSearchAdapter(surfacing_cfg)  # lazy LTM start
        # No tracker when dedup is off and the feedback loop is off — that is
        # an explicit opt-out of all feedback-DB persistence, including
        # ``surfacing_events`` telemetry.
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
            await self._stop_adapter()
        if self._handshake_written:
            discovery.remove_handshake_if_owner(
                discovery.handshake_path(self._config.data_dir, self._fingerprint),
                pid=os.getpid(),
                token=self._token,
            )

    async def _stop_adapter(self) -> None:
        """Stop the LTM adapter without leaking its warm child process.

        Since #663 the adapter marshals its lifecycle ops through an internal
        owner task, so scope enter/exit happen in the same task and
        ``adapter.stop()`` succeeds cleanly in the common case. The sweep is
        retained as belt-and-braces — but it must run whether ``stop()``
        raised OR returned: ``stop()`` now *returns normally* while abandoning
        a live child in two cases (owner task lost mid-lifetime, so its
        contexts are abandoned unclosed; or ``stop()``'s own bounded-join
        timeout against a stuck ``aclose``). Keying the sweep off an exception
        (as the pre-#663 cross-task error did) would silently skip it there.
        So always compare the direct children before and after: one present in
        both snapshots is the leaked LTM child — this process spawns no other
        children — so terminate it. A clean stop reaps its child (mcp's stdio
        shutdown awaits exit), so the child is gone from the post snapshot and
        nothing is killed. (Sweep removal is tracked as a follow-up once #663
        has soaked.)
        """
        before = _direct_child_pids()
        try:
            await self._adapter.stop()
        except Exception as exc:
            if is_clean_cancel_scope_shutdown(exc):
                logger.debug(
                    "LTM adapter stop hit a known AnyIO cancel-scope cleanup condition — "
                    "sweeping for a leaked LTM child"
                )
            else:
                logger.debug("LTM adapter stop failed", exc_info=True)
        leaked = before & _direct_child_pids()
        if not leaked:
            return
        logger.warning("terminating leaked LTM child process(es): %s", sorted(leaked))
        await _terminate_leaked_children(leaked)

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
                encoded = encode_line(resp)
                if len(encoded) > MAX_MESSAGE_BYTES:
                    encoded = encode_line(
                        {"v": PROTOCOL_VERSION, "ok": False, "status": "unavailable"}
                    )
                writer.write(encoded)
                # Timeout → the generic handler below logs it and the finally
                # block closes the writer, dropping the stuck consumer.
                await asyncio.wait_for(writer.drain(), timeout=_WRITE_TIMEOUT_SECONDS)
        except Exception:
            logger.debug("daemon connection handler error", exc_info=True)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, req: dict[str, Any]) -> dict[str, Any] | None:
        # Protocol-version guard. A wire-incompatible client normally keys to a
        # different config_fingerprint and never finds this daemon's handshake;
        # this rejects a stray/mismatched authenticated request rather than
        # acting on a payload shape this version may not understand.
        if req.get("v") != PROTOCOL_VERSION:
            logger.debug("daemon rejected request with protocol v=%s", req.get("v"))
            return {"v": PROTOCOL_VERSION, "ok": False, "error": "unsupported protocol version"}
        op = req.get("op")
        if op == OP_PING:
            return {
                "v": PROTOCOL_VERSION,
                "ok": True,
                "status": "ready",
                "ltm": self._ltm_warmth(),
                "latency": self._latency.snapshot(),
                "queue": self._queue_snapshot(),
                "core": {"runtime_profile": getattr(self._adapter, "runtime_profile", None)},
            }
        if op == OP_SHUTDOWN:
            logger.info("daemon received shutdown request")
            self._shutdown_event.set()
            return {"v": PROTOCOL_VERSION, "ok": True, "status": "shutting_down"}
        if op == OP_SURFACE:
            # The wire carries a host-agnostic CanonicalHookCall (the hook
            # normalized it before sending), so the daemon needs no host
            # knowledge — just rehydrate and run the shared core.
            call = CanonicalHookCall.from_wire(req.get("payload"))
            if call is None:
                return surface_response({})

            async def surface_call() -> dict[str, Any]:
                # Computed here, not at admission: the admission queue and the
                # serialization lock have already eaten part of the client's
                # deadline by the time we run.
                surface_deadline = self._surface_deadline(req)
                if surface_deadline is None:
                    logger.debug("surface skipped: no budget left in the client deadline")
                    return surface_response({})
                output = await run_surfacing_hook(
                    call, engine=self._engine, deadline_monotonic=surface_deadline
                )
                return surface_response(output)

            return await self._run_admitted(
                req,
                surface_call,
                latency_kind="surface",
                activity_counter=self._surfacing_retrieval_count,
                timeout_counter=self._surfacing_timeout_count,
            )
        if op in {
            OP_LTM_SEARCH,
            OP_LTM_CONTEXT_COMPOSE,
            OP_LTM_CANDIDATE_PROPOSE,
            OP_LTM_INCREMENT_ACCESS,
            OP_LTM_SCRATCH_LIST,
        }:
            # These operations expose raw LTM search/scratch data and are
            # intentionally stricter than the legacy hook surface operation.
            if not _is_loopback_host(self._host):
                return {"v": PROTOCOL_VERSION, "ok": False, "status": "unavailable"}
            payload = req.get("payload")
            if not isinstance(payload, dict):
                return {"v": PROTOCOL_VERSION, "ok": False, "status": "invalid"}
            if op == OP_LTM_SEARCH:
                parsed = self._parse_search_payload(payload)
                if parsed is None:
                    return {"v": PROTOCOL_VERSION, "ok": False, "status": "invalid"}

                async def search_call() -> dict[str, Any]:
                    results, hints, outcome = await self._adapter.search(**parsed)
                    encoded_results = []
                    for result in results:
                        entry: dict[str, Any] = {
                            "content": str(result.chunk.content),
                            "score": float(result.score),
                            "source": str(result.chunk.metadata.source_file),
                            "namespace": str(result.chunk.metadata.namespace),
                            "chunk_id": str(result.chunk.id),
                        }
                        # Core-reported score scale (#1781): additive keys,
                        # omitted when the core did not name one, so older
                        # clients and older daemons interoperate without a
                        # PROTOCOL_VERSION bump (readers use .get()).
                        scale = getattr(result, "score_scale", None)
                        if isinstance(scale, str) and scale:
                            entry["score_scale"] = scale
                        reranker = getattr(result, "reranker", None)
                        if isinstance(reranker, str) and reranker:
                            entry["reranker"] = reranker
                        encoded_results.append(entry)
                    return {
                        "v": PROTOCOL_VERSION,
                        "ok": True,
                        "results": encoded_results,
                        "hints": [str(hint) for hint in hints],
                        "outcome": outcome,
                    }

                return await self._run_admitted(req, search_call, latency_kind="retrieval")
            if op == OP_LTM_CONTEXT_COMPOSE:
                query = payload.get("query")
                agent_id = payload.get("agent_id")
                max_chars = payload.get("max_chars")
                top_k = payload.get("top_k")
                namespace = payload.get("namespace")
                context_window = payload.get("context_window")
                max_compose_schema = payload.get("context_compose_max_schema")
                trace_id = payload.get("trace_id")
                if (
                    isinstance(max_compose_schema, bool)
                    or not isinstance(max_compose_schema, int)
                    or max_compose_schema < 2
                ):
                    return {"v": PROTOCOL_VERSION, "ok": False, "status": "unsupported"}
                if (
                    not isinstance(query, str)
                    or (agent_id is not None and not isinstance(agent_id, str))
                    or not isinstance(max_chars, int)
                    or isinstance(max_chars, bool)
                    or max_chars <= 0
                    or not isinstance(top_k, int)
                    or isinstance(top_k, bool)
                    or top_k <= 0
                    or (
                        isinstance(namespace, list)
                        and not all(isinstance(item, str) for item in namespace)
                    )
                    or (
                        not isinstance(namespace, list)
                        and namespace is not None
                        and not isinstance(namespace, str)
                    )
                    or (
                        context_window is not None
                        and (
                            isinstance(context_window, bool)
                            or not isinstance(context_window, int)
                            or context_window < 0
                        )
                    )
                    or (trace_id is not None and not isinstance(trace_id, str))
                ):
                    return {"v": PROTOCOL_VERSION, "ok": False, "status": "invalid"}

                async def compose_call() -> dict[str, Any]:
                    compose = getattr(self._adapter, "context_compose", None)
                    if not callable(compose):
                        return {"v": PROTOCOL_VERSION, "ok": False, "status": "unsupported"}
                    bundle = await compose(
                        query,
                        agent_id=agent_id,
                        max_chars=max_chars,
                        top_k=top_k,
                        namespace=namespace,
                        context_window=context_window,
                        trace_id=trace_id,
                    )
                    # ``McpClientSearchAdapter.context_compose`` heals and
                    # negotiates before consulting capabilities. Check only
                    # after that call: reading the default schema 0 while the
                    # daemon's fire-and-forget warm-up is still running would
                    # misclassify a capable cold core as unsupported.
                    if getattr(self._adapter, "capabilities_ready", True) is False:
                        return {"v": PROTOCOL_VERSION, "ok": False, "status": "unavailable"}
                    capabilities = getattr(self._adapter, "capabilities", None)
                    actual_schema = getattr(capabilities, "context_compose_schema", 0)
                    selected_schema = min(
                        max_compose_schema, MAX_CONTEXT_COMPOSE_SCHEMA, actual_schema
                    )
                    if selected_schema < 2:
                        return {"v": PROTOCOL_VERSION, "ok": False, "status": "unsupported"}
                    if bundle is None:
                        return {"v": PROTOCOL_VERSION, "ok": False, "status": "unavailable"}

                    def encode_result(result: Any) -> dict[str, Any]:
                        encoded: dict[str, Any] = {
                            "content": str(result.chunk.content),
                            "score": float(result.score),
                            "source": str(result.chunk.metadata.source_file),
                            "namespace": str(result.chunk.metadata.namespace),
                            "chunk_id": str(result.chunk.id),
                        }
                        context = getattr(result, "context", None)
                        if selected_schema >= 3 and context is not None:

                            def encode_context_chunk(chunk: Any) -> dict[str, str]:
                                return {
                                    "id": str(chunk.id),
                                    "content": str(chunk.content),
                                    "source": str(chunk.source),
                                    "namespace": str(chunk.namespace),
                                }

                            encoded["context"] = {
                                "before": [
                                    encode_context_chunk(chunk) for chunk in context.window_before
                                ],
                                "after": [
                                    encode_context_chunk(chunk) for chunk in context.window_after
                                ],
                                "chunk_position": context.chunk_position,
                                "total_chunks_in_file": context.total_chunks_in_file,
                            }
                        return encoded

                    response: dict[str, Any] = {
                        "v": PROTOCOL_VERSION,
                        "ok": True,
                        "selected_context_compose_schema": selected_schema,
                        "pinned": [encode_result(item) for item in bundle.pinned],
                        "retrieved": [encode_result(item) for item in bundle.retrieved],
                        "warnings": list(bundle.warnings),
                        "omitted_block_ids": list(bundle.omitted_block_ids),
                    }
                    # Compose-envelope score scale (core #1796): additive
                    # top-level keys emitted only at the negotiated schema 4, so
                    # a client that asked for max schema 3 receives a
                    # byte-identical schema-3 response. Source is the decoded
                    # bundle; absence mirrors the core's empty-retrieved omission
                    # (bundle fields are None exactly when the core omitted them).
                    # No PROTOCOL_VERSION bump — readers use .get() (#1781/#727).
                    #
                    # Deliberate asymmetry: emission is schema-gated here while
                    # the client decoders read the keys presence-based. It only
                    # diverges from direct mode if capabilities lag below 4 while
                    # the core still emits the keys — but a #1796 core advertises
                    # schema 4 whenever it emits them, so the clamp reaches 4 too.
                    # Gating on selected_schema keeps the negotiated contract the
                    # authority (an old client's schema-3 answer stays clean).
                    if selected_schema >= 4:
                        scale = getattr(bundle, "score_scale", None)
                        if isinstance(scale, str) and scale:
                            response["score_scale"] = scale
                        reranker = getattr(bundle, "reranker", None)
                        if isinstance(reranker, str) and reranker:
                            response["reranker"] = reranker
                    return response

                return await self._run_admitted(req, compose_call, latency_kind="retrieval")
            if op == OP_LTM_CANDIDATE_PROPOSE:
                fields = ("content", "source", "source_ref", "idempotency_key")
                trace_id = payload.get("trace_id")
                if (
                    any(not isinstance(payload.get(field), str) for field in fields)
                    or not payload.get("content", "").strip()
                    or len(payload.get("content", "")) > 2_000
                    or len(payload.get("source_ref", "")) > 512
                    or not payload.get("idempotency_key", "")
                    or len(payload.get("idempotency_key", "")) > 256
                    or (trace_id is not None and not isinstance(trace_id, str))
                ):
                    return {"v": PROTOCOL_VERSION, "ok": False, "status": "invalid"}

                async def propose_call() -> dict[str, Any]:
                    propose = getattr(self._adapter, "candidate_propose", None)
                    if not callable(propose):
                        return {"v": PROTOCOL_VERSION, "ok": False, "status": "unsupported"}
                    candidate = await propose(
                        payload["content"],
                        source=payload["source"],
                        source_ref=payload["source_ref"],
                        idempotency_key=payload["idempotency_key"],
                        trace_id=trace_id,
                    )
                    if candidate is None:
                        return {"v": PROTOCOL_VERSION, "ok": False, "status": "unsupported"}
                    return {"v": PROTOCOL_VERSION, "ok": True, "candidate": candidate}

                return await self._run_admitted(req, propose_call)
            if op == OP_LTM_INCREMENT_ACCESS:
                chunk_ids = payload.get("chunk_ids")
                trace_id = payload.get("trace_id")
                if (
                    not isinstance(chunk_ids, list)
                    or not all(isinstance(item, str) and item for item in chunk_ids)
                    or (trace_id is not None and not isinstance(trace_id, str))
                ):
                    return {"v": PROTOCOL_VERSION, "ok": False, "status": "invalid"}

                async def increment_call() -> dict[str, Any]:
                    await self._adapter.increment_access(chunk_ids, trace_id=trace_id)
                    return {"v": PROTOCOL_VERSION, "ok": True}

                return await self._run_admitted(req, increment_call)

            trace_id = payload.get("trace_id")
            if trace_id is not None and not isinstance(trace_id, str):
                return {"v": PROTOCOL_VERSION, "ok": False, "status": "invalid"}

            async def scratch_call() -> dict[str, Any]:
                items = await self._adapter.scratch_list(trace_id=trace_id)
                return {"v": PROTOCOL_VERSION, "ok": True, "items": items}

            return await self._run_admitted(req, scratch_call)
        return {"v": PROTOCOL_VERSION, "ok": False, "error": "unknown op"}

    def _surface_deadline(self, req: dict[str, Any]) -> float | None:
        """Absolute monotonic point by which this request's LTM attempt must
        be over, or ``None`` for "don't start one".

        The margin keeps the engine's own timeout ahead of the client's
        give-up point, so the abort is the engine's (fault row + log + breaker
        count, #579) rather than a silent outside cancellation. It is a
        *deadline*, not the remaining budget, so the engine can re-read the
        clock right before its LTM attempt and its own pre-timeout work cannot
        silently eat the margin (#720). ``_run_admitted`` already rejected a
        missing or expired deadline; this re-reads it because queue/lock wait
        may have consumed the rest since admission.
        """
        deadline = _usable_deadline(req)
        if deadline is None:
            return None
        surface_deadline = deadline - _DEADLINE_RESPONSE_MARGIN_SECONDS
        if surface_deadline - time.monotonic() < _MIN_SURFACE_BUDGET_SECONDS:
            return None
        return surface_deadline

    async def _run_admitted(
        self,
        req: dict[str, Any],
        operation: Callable[[], Awaitable[dict[str, Any]]],
        *,
        latency_kind: LatencyKind | None = None,
        activity_counter: Callable[[], int] | None = None,
        timeout_counter: Callable[[], int] | None = None,
    ) -> dict[str, Any]:
        """Run one LTM operation under the shared queue, deadline, and lock.

        ``timeout_counter`` reports an abort the *operation* handled itself, so
        it never surfaces as an exception here. Surfacing does exactly that now
        that it runs under a propagated budget: it returns a well-formed empty
        result on timeout, which is otherwise indistinguishable from "nothing
        relevant" and would be filed as a ``success`` sample of roughly the
        whole budget — censored data in the percentiles the timeout
        recommendation is derived from.
        """
        deadline = _usable_deadline(req)
        if deadline is None or deadline <= time.monotonic():
            return {"v": PROTOCOL_VERSION, "ok": False, "status": "expired"}
        if self._pending_slots.locked():
            self._busy_rejections += 1
            return {"v": PROTOCOL_VERSION, "ok": False, "status": "busy"}
        # Advice is based on user-observed end-to-end latency, intentionally
        # including admission-queue and serialization-lock wait time.
        started = time.monotonic()
        warm_at_start = self._ltm_warmth() == "warm"
        activity_before: int | None = None
        timeouts_before: int | None = None
        await self._pending_slots.acquire()
        self._last_request = time.monotonic()
        self._active_requests += 1
        try:
            async with asyncio.timeout_at(deadline):
                async with self._surface_lock:
                    # Read under the lock, not before the queue: admissions
                    # overlap (`max_pending_requests` is 32), and the engine's
                    # counters only move inside a serialized run. Captured
                    # earlier, a request that merely *waited* while another one
                    # timed out would see the delta and inherit its outcome.
                    activity_before = activity_counter() if activity_counter is not None else None
                    timeouts_before = timeout_counter() if timeout_counter is not None else None
                    response = await operation()
            activity_observed = (
                activity_counter is None
                or activity_before is None
                or activity_counter() > activity_before
            )
            if latency_kind is not None and activity_observed:
                elapsed_ms = (time.monotonic() - started) * 1000.0
                self_timed_out = (
                    timeout_counter is not None
                    and timeouts_before is not None
                    and timeout_counter() > timeouts_before
                )
                if not warm_at_start:
                    outcome: LatencyOutcome = "cold"
                elif self_timed_out:
                    # Same precedence as the `asyncio.TimeoutError` branch below,
                    # which this replaces for surfacing: cold first, then timeout.
                    outcome = "timeout"
                elif response.get("ok") is not True or (
                    latency_kind == "retrieval"
                    and response.get("outcome") not in (None, "ok", "empty_results")
                ):
                    outcome = "error"
                else:
                    outcome = "success"
                self._latency.record(latency_kind, elapsed_ms, outcome)
            return response
        except asyncio.TimeoutError:
            if latency_kind is not None:
                outcome = "timeout" if warm_at_start else "cold"
                self._latency.record(latency_kind, (time.monotonic() - started) * 1000.0, outcome)
            return {"v": PROTOCOL_VERSION, "ok": False, "status": "expired"}
        except Exception:
            if latency_kind is not None:
                outcome = "error" if warm_at_start else "cold"
                self._latency.record(latency_kind, (time.monotonic() - started) * 1000.0, outcome)
            logger.debug("daemon LTM operation failed", exc_info=True)
            return {"v": PROTOCOL_VERSION, "ok": False, "status": "unavailable"}
        finally:
            self._active_requests -= 1
            self._pending_slots.release()
            self._last_request = time.monotonic()

    def _queue_snapshot(self) -> dict[str, int]:
        """Bounded numeric admission/serialization telemetry for operators."""
        in_flight = 1 if self._surface_lock.locked() else 0
        return {
            "active": self._active_requests,
            "in_flight": in_flight,
            "queued": max(0, self._active_requests - in_flight),
            "capacity": self._max_pending_requests,
            "available": max(0, self._max_pending_requests - self._active_requests),
            "busy_rejections": self._busy_rejections,
        }

    def _surfacing_retrieval_count(self) -> int:
        """Number of engine terminal decisions that require an LTM attempt.

        Hook allowlist and engine gate skips must not dilute warm-search
        percentiles with near-zero samples. The daemon wires a real
        ``SurfacingObservability`` and serializes surface calls, so comparing
        this aggregate before/after one admitted request is deterministic —
        provided the "before" read happens under ``_surface_lock`` too, which
        is why ``_run_admitted`` captures it there rather than at admission.
        """
        observability = getattr(self._engine, "observability", None)
        if observability is None:
            return 0
        snapshot = observability.snapshot()
        total_skips = snapshot.get("skip_reasons", {}).get("__total__", {})
        total_outcomes = snapshot.get("outcomes", {}).get("__total__", {})
        retrieval_skips = {
            "no_results_score",
            "no_results_dedup",
            "no_results_demoted",
            "ltm_unavailable",
            "ltm_call_failed",
            "ltm_parse_empty",
        }
        retrieval_outcomes = {"surfaced_cache_miss", "error_timeout", "error_other"}
        return sum(int(total_skips.get(key, 0)) for key in retrieval_skips) + sum(
            int(total_outcomes.get(key, 0)) for key in retrieval_outcomes
        )

    def _surfacing_timeout_count(self) -> int:
        """Engine-internal surfacing timeouts recorded so far.

        Sibling of :meth:`_surfacing_retrieval_count`, read the same way and
        deterministic for the same reason: the daemon wires a real
        ``SurfacingObservability``, and ``_run_admitted`` brackets the
        before/after reads around ``operation()`` *inside* ``_surface_lock``,
        so the window spans exactly one request even while others are queued.
        """
        observability = getattr(self._engine, "observability", None)
        if observability is None:
            return 0
        snapshot = observability.snapshot()
        total_outcomes = snapshot.get("outcomes", {}).get("__total__", {})
        return int(total_outcomes.get("error_timeout", 0))

    @staticmethod
    def _parse_search_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
        query = payload.get("query")
        top_k = payload.get("top_k")
        namespace = payload.get("namespace")
        context_window = payload.get("context_window")
        trace_id = payload.get("trace_id")
        if not isinstance(query, str) or not query:
            return None
        if top_k is not None and (
            isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0
        ):
            return None
        if isinstance(namespace, list):
            if not all(isinstance(item, str) for item in namespace):
                return None
        elif namespace is not None and not isinstance(namespace, str):
            return None
        if context_window is not None and (
            isinstance(context_window, bool)
            or not isinstance(context_window, int)
            or context_window < 0
        ):
            return None
        if trace_id is not None and not isinstance(trace_id, str):
            return None
        return {
            "query": query,
            "top_k": top_k,
            "namespace": namespace,
            "context_window": context_window,
            "trace_id": trace_id,
        }

    def _ltm_warmth(self) -> str:
        """Best-effort LTM connection state for ``ping`` (status nicety).

        Priority: warm > warming > down > cold. ``warming`` covers a
        start/reconnect op in flight (the startup warm-up task, or an
        abandoned lazy start still finishing, #664). A *failed* warm-up
        reads ``cold`` — accurate enough, since the lazy retry on first
        use is still pending.
        """
        if getattr(self._adapter, "_session", None) is not None:
            return "warm"
        if getattr(self._adapter, "warming", False):
            return "warming"
        if getattr(self._adapter, "_start_attempted", False):
            return "down"
        return "cold"

    # ── idle + signals ────────────────────────────────────────────────────

    async def _idle_watch(self) -> None:
        if self._idle_timeout <= 0:
            return
        # Poll at half the timeout, floored low enough that sub-second
        # idle_timeout values (tests, aggressive resource configs) shut down
        # near the configured threshold instead of ~1s late, and capped so a
        # huge timeout still notices the shutdown event reasonably often.
        interval = min(30.0, max(0.05, self._idle_timeout / 2))
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._shutdown_event.is_set():
                return
            if (
                self._active_requests == 0
                and time.monotonic() - self._last_request >= self._idle_timeout
            ):
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
    """Synchronous entry point used by ``mms daemon run``.

    Exception barrier (mirrors the MCP server's #209 barrier): a detached
    daemon runs with ``stderr=DEVNULL``, so an uncaught crash during startup
    — e.g. ``_build_engine`` (SurfacingEngine ctor) or ``asyncio.start_server``
    raising inside ``serve()`` — would otherwise leave *no* trace: the
    traceback goes to the devnulled stderr, never through the file logger, so
    ``stm-daemon.log`` (which ``mms daemon start`` points the operator at)
    stays empty. ``_configure_logging`` has already installed the file handler
    by the time ``run()`` is called (``daemon_cmd.run_cmd``), so logging here
    lands in that file. Re-raise after logging so the exit code still reflects
    the failure — this only adds observability.
    """
    try:
        cfg = config if config is not None else STMConfig()
    except Exception as exc:
        # The construction used to sit outside the barrier below, so a broken
        # MEMTOMEM_STM_* env killed the daemon with no trace in the file log
        # this docstring promises (#847). Log with the env-var hint, re-raise.
        log_stm_config_failure(exc, logger=logger, context="starting the daemon server")
        raise
    try:
        return asyncio.run(DaemonServer(cfg).serve())
    except (RuntimeError, ExceptionGroup) as e:
        # A clean anyio cancel-scope teardown on shutdown is not a crash — the
        # main server's barrier ignores the same shape (#410 follow-up).
        if is_clean_cancel_scope_shutdown(e):
            logger.debug("daemon ignored a known AnyIO cancel-scope cleanup condition: %s", e)
            return 0
        logger.exception("daemon terminated with an unhandled exception")
        raise
    except Exception:
        logger.exception("daemon terminated with an unhandled exception")
        raise
