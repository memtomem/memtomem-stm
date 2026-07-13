"""MCP Client adapter for surfacing — connects to a remote memtomem server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.utils.numeric import safe_float
from memtomem_stm.utils.redact import redact_exception_text, redact_url_userinfo

logger = logging.getLogger(__name__)

# #295: outcome typing for ``McpClientSearchAdapter.search`` — five
# different failure modes used to collapse to ``([], [])`` and looked
# identical to a healthy empty namespace at the engine's observability
# layer. The engine consumes this enum to record distinct skip labels
# (``ltm_unavailable`` / ``ltm_call_failed`` / ``ltm_parse_empty``) so
# operators can tell why surfacing is not firing.
SearchOutcome = Literal[
    "ok",
    "no_session",
    "transport_error",
    "call_error",
    "empty_content",
    "empty_results",
    "upstream_error",
    "parse_error",
    "daemon_starting",
    "daemon_busy",
]


@dataclass(frozen=True, slots=True)
class LtmCapabilities:
    """Version-negotiated optional memtomem core surfaces."""

    context_compose_schema: int = 0
    candidate_propose_schema: int = 0
    structured_scratch: bool = False
    increment_access: bool = False


class LtmTransportError(RuntimeError):
    """A compose RPC still failed after the adapter's reconnect attempt."""


@dataclass(frozen=True, slots=True)
class ContextComposeResult:
    """Structured pinned-first bundle returned by a compatible core."""

    pinned: tuple[RemoteSearchResult, ...]
    retrieved: tuple[RemoteSearchResult, ...]
    warnings: tuple[str, ...] = ()
    omitted_block_ids: tuple[str, ...] = ()


@dataclass
class RemoteSearchResult:
    """Lightweight search result parsed from mem_search text output."""

    class _FakeMeta:
        def __init__(self, source: str, namespace: str):
            self.source_file = Path(source)
            self.namespace = namespace

    class _FakeChunk:
        def __init__(self, content: str, source: str, namespace: str):
            self.content = content
            self.metadata = RemoteSearchResult._FakeMeta(source, namespace)
            # Compact parser has no real chunk id, so derive a stable surrogate
            # from the content. The formatter renders this as the agent-facing
            # ``memory_id`` (EN-2/3); the structured parser overwrites it with
            # the real ``chunk_id`` from core (see ResultParser). The surrogate
            # is good enough for STM-side invalidation but cannot drive the LTM
            # ``increment_access`` boost — see ``SurfacingConfig.result_format``.
            self.id = hashlib.sha256(content.encode()).hexdigest()[:16]

    def __init__(
        self,
        content: str,
        score: float,
        source: str = "",
        namespace: str = "default",
        *,
        pinned: bool = False,
    ):
        self.chunk = self._FakeChunk(content, source, namespace)
        self.score = score
        self.pinned = pinned


class SurfacingLtmAdapter(Protocol):
    """Structural contract consumed by :class:`SurfacingEngine`."""

    async def warm_up(self) -> None: ...

    async def stop(self) -> None: ...

    async def search(
        self,
        query: str,
        top_k: int | None = None,
        namespace: str | list[str] | None = None,
        context_window: int | None = None,
        *,
        trace_id: str | None = None,
        **kwargs: Any,
    ) -> tuple[list[RemoteSearchResult], list[str], SearchOutcome]: ...

    async def increment_access(
        self, chunk_ids: list[str], *, trace_id: str | None = None
    ) -> None: ...

    async def scratch_list(self, *, trace_id: str | None = None) -> list[dict]: ...

    @property
    def capabilities(self) -> LtmCapabilities: ...

    async def context_compose(
        self,
        query: str,
        *,
        agent_id: str | None = None,
        max_chars: int = 3000,
        top_k: int = 10,
        trace_id: str | None = None,
    ) -> ContextComposeResult | None: ...

    async def candidate_propose(
        self,
        content: str,
        *,
        source: str,
        source_ref: str,
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> dict[str, Any] | None: ...


class ResultParser:
    """Strategy interface for parsing mem_search text output.

    Returns a ``(results, hints)`` tuple. ``hints`` carries parent-side
    trust-UX annotations (parent commit ``7d184f1``, PR #231) when the
    structured format is in use; compact format always returns ``[]``.
    The hints list is opportunistic — parent is alpha, the field may be
    renamed or removed, so callers must tolerate an empty list silently.
    """

    def parse(
        self,
        text: str,
        *,
        max_content_chars: int = 500,
    ) -> tuple[list[RemoteSearchResult], list[str]]:
        raise NotImplementedError


_BLOCK_SPLIT_RE = re.compile(r"^(?=\[\d+\]\s+\d+\.?\d*\s*\|)", flags=re.MULTILINE)
_HEADER_RE = re.compile(r"\[(\d+)\]\s+(\d+\.?\d*)\s*\|(.+)")
_NS_RE = re.compile(r"\[([^\]]+)\]\s*(.*)")
_RANK_SUFFIX_RE = re.compile(r"\s*\[\d+/\d+\]\s*$")
_FIRST_TOKEN_RE = re.compile(r"(\S+)")


class CompactResultParser(ResultParser):
    """Parse core's compact format: ``[rank] score | source > hierarchy``.

    The compact format carries no hints channel; the second tuple slot is
    always an empty list.
    """

    def parse(
        self,
        text: str,
        *,
        max_content_chars: int = 500,
    ) -> tuple[list[RemoteSearchResult], list[str]]:
        results: list[RemoteSearchResult] = []
        if not text or not text.strip():
            return results, []

        blocks = _BLOCK_SPLIT_RE.split(text)

        for block in blocks:
            block = block.strip()
            if not block:
                continue

            first_line, _, rest = block.partition("\n")

            header_match = _HEADER_RE.match(first_line)
            if not header_match:
                continue

            score = float(header_match.group(2))
            remainder = header_match.group(3).strip()

            ns_match = _NS_RE.match(remainder)
            if ns_match:
                namespace = ns_match.group(1)
                remainder = ns_match.group(2)
            else:
                namespace = "default"

            remainder = _RANK_SUFFIX_RE.sub("", remainder)

            source_match = _FIRST_TOKEN_RE.match(remainder)
            source = source_match.group(1) if source_match else "unknown"

            content = rest.strip() if rest else ""
            if content:
                if len(content) > max_content_chars:
                    logger.debug(
                        "Truncating search result content from %d to %d chars (source=%s)",
                        len(content),
                        max_content_chars,
                        source,
                    )
                results.append(
                    RemoteSearchResult(
                        content=content[:max_content_chars],
                        score=score,
                        source=source,
                        namespace=namespace,
                    )
                )

        return results, []


class StructuredResultParser(ResultParser):
    """Parse core's structured JSON format: ``{"results": [...]}``.

    Each ``results`` element contains ``rank``, ``score``, ``source``,
    ``hierarchy``, ``namespace``, ``chunk_id``, and ``content`` fields.
    An optional top-level ``hints`` list carries parent-side trust-UX
    annotations (parent commit ``7d184f1``, PR #231). Hints are read
    opportunistically via ``data.get("hints", [])`` — the field is not
    asserted on and its absence degrades silently to an empty list.
    """

    def parse(
        self,
        text: str,
        *,
        max_content_chars: int = 500,
    ) -> tuple[list[RemoteSearchResult], list[str]]:
        if not text or not text.strip():
            return [], []

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.warning("StructuredResultParser: invalid JSON, falling back to empty")
            return [], []

        raw_hints = data.get("hints", [])
        hints: list[str] = (
            [str(h) for h in raw_hints if isinstance(h, str)] if isinstance(raw_hints, list) else []
        )

        raw_results = data.get("results", [])
        results: list[RemoteSearchResult] = []
        for item in raw_results:
            content = item.get("content", "")
            if not content:
                continue
            if len(content) > max_content_chars:
                logger.debug(
                    "Truncating search result content from %d to %d chars (source=%s)",
                    len(content),
                    max_content_chars,
                    item.get("source", "unknown"),
                )
            result = RemoteSearchResult(
                content=content[:max_content_chars],
                score=safe_float(item.get("score", 0.0), 0.0),
                source=item.get("source", "unknown"),
                namespace=item.get("namespace", "default"),
            )
            # Preserve chunk_id from core instead of sha256(content)
            chunk_id = item.get("chunk_id")
            if chunk_id:
                result.chunk.id = chunk_id
            results.append(result)

        return results, hints

    def parse_checked(
        self, text: str, *, max_content_chars: int = 500
    ) -> tuple[list[RemoteSearchResult], list[str], bool]:
        """Parse while distinguishing malformed JSON from a healthy empty set."""
        if not text or not text.strip():
            return [], [], True
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return [], [], False
        if not isinstance(data, dict) or not isinstance(data.get("results", []), list):
            return [], [], False
        results, hints = self.parse(text, max_content_chars=max_content_chars)
        return results, hints, True


def get_parser(fmt: str = "compact") -> ResultParser:
    """Return a ``ResultParser`` for the given format name."""
    if fmt == "structured":
        return StructuredResultParser()
    return CompactResultParser()


_compact_parser = CompactResultParser()


@dataclass
class _OwnerRequest:
    """A lifecycle op marshalled to the adapter's owner task (#663).

    ``fut`` mirrors the op's outcome back to the submitting caller.
    ``cancel_requested`` marks a request whose caller was cancelled while
    the request was still queued — the owner skips it instead of running
    an op nobody is waiting for.
    """

    op: Literal["start", "reconnect", "close"]
    fut: asyncio.Future[None]
    cancel_requested: bool = False
    expected_generation: int | None = None


def _discard_future_result(fut: asyncio.Future[None]) -> None:
    """Retrieve a resolved future's result/exception so asyncio never warns
    it went unobserved.

    Used when the submitting caller was cancelled and left while the owner
    task may still resolve ``req.fut`` afterward (the abandoned op finishes
    with a result or an exception, or a queued-skip cancels it). A cancelled
    future has nothing to retrieve.
    """
    if fut.cancelled():
        return
    try:
        fut.exception()
    except asyncio.CancelledError:  # pragma: no cover - resolved-cancelled race
        pass


class McpClientSearchAdapter:
    """Connects to a memtomem MCP server and calls mem_search.

    Implements enough of the SearchPipeline interface for SurfacingEngine.
    """

    def __init__(self, config: SurfacingConfig) -> None:
        self._config = config
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._parser = get_parser(getattr(config, "result_format", "compact"))
        self._capabilities = LtmCapabilities()
        # #290: SurfacingEngine wraps adapter calls in ``asyncio.wait_for``;
        # an outer-timeout cancellation interrupts ``call_tool`` mid-RPC and
        # leaves the MCP session in a mid-message state. ``_TRANSPORT_ERRORS``
        # below intentionally does not catch ``CancelledError`` (cooperative
        # cancellation must propagate), so we mark the session here and let
        # the next caller heal the connection lazily before issuing an RPC.
        self._needs_reconnect = False
        self._generation = 0
        self._dirty_generation: int | None = None
        self._reconnect_lock = asyncio.Lock()
        self._reconnect_flights: dict[int, asyncio.Task[None]] = {}
        # Lazy MCP client start: the LTM subprocess used to be spawned
        # eagerly from app_lifespan, but its initialize+version_negotiate
        # round trip blocked the proxy's own MCP startup long enough for
        # hosts (e.g. codex with a 60s startup_timeout) to time out and
        # respawn the proxy — creating two parallel LTM children. Defer
        # the start to the first RPC. The lock serializes concurrent
        # first-callers; the flag prevents retrying a permanently failing
        # start on every subsequent RPC (matches the prior single-shot
        # lifespan behavior).
        self._start_attempted = False
        self._start_lock = asyncio.Lock()
        # #663: anyio cancel scopes are task-affine — the transport/session
        # contexts entered by ``_do_start`` MUST be exited by the same task.
        # Under lazy start (#338) the first RPC arrives in a short-lived
        # request-handler task of the lowlevel MCP server; entering the
        # contexts there corrupted that task's scope stack when it exited
        # (killing the whole STM server right after the first successful
        # surfacing) and made a later ``stop()`` from the lifespan task raise
        # "Attempted to exit cancel scope in a different task than it was
        # entered in". All lifecycle ops (start / reconnect / close) are
        # therefore marshalled through a single long-lived owner task that
        # both enters and exits every context. ``call_tool`` itself rides
        # anyio memory-object streams and is task-agnostic, so the RPC
        # bodies (``search`` / ``increment_access`` / ``scratch_list``) keep
        # running in their caller tasks. A per-op ``anyio.CancelScope`` in
        # the owner would NOT work instead: the transport scopes entered
        # inside the op outlive the op (until close), so wrapping them would
        # violate LIFO scope exit — the very RuntimeError being fixed here.
        self._requests: asyncio.Queue[_OwnerRequest] = asyncio.Queue()
        self._owner_task: asyncio.Task[None] | None = None
        self._current_req: _OwnerRequest | None = None
        self._stopped = False

    async def start(self) -> None:
        """Connect to the memtomem MCP server.

        The actual context setup runs in the adapter's owner task (#663);
        this wrapper only marshals the request and mirrors its outcome —
        exceptions included — back to the caller. Cancelling the caller does
        NOT cancel the op: an in-flight start is abandoned to finish in the
        owner task so the LTM child keeps warming (#664).
        """
        await self._submit("start")

    async def warm_up(self) -> None:
        """Best-effort, single-shot background warm-up of the LTM child (#664).

        Awaits ``start()`` and swallows every failure (including the
        ``RuntimeError`` ``_submit`` raises when the adapter is already
        stopped — a normal shutdown race). Deliberately never touches
        ``_start_attempted``: a failed warm-up leaves the lazy-start gate
        armed, so ``_heal_if_needed`` still performs its one lazy retry on
        the first real call. Racing that first lazy start is safe — the
        owner task serializes ops and the second start no-ops on
        ``_do_start``'s live-session guard.
        """
        try:
            await self.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info(
                "LTM warm-up failed — lazy start will retry on first use: %s",
                self._scrub_exc(exc),
            )

    @property
    def warming(self) -> bool:
        """True while a start/reconnect op is in flight and no session is
        published yet — the "child is warming" window a warm-up task or an
        abandoned start (#664) leaves behind. Cheap read for status probes
        (daemon ``_ltm_warmth``)."""
        req = self._current_req
        return self._session is None and req is not None and req.op in ("start", "reconnect")

    async def _do_start(self) -> None:
        """Enter the transport/session contexts. Owner-task only (#663)."""
        if self._session is not None:
            # An abandoned start (#664) already warmed the session while this
            # op sat in the queue; starting again would overwrite _stack and
            # leak the live transport/child.
            return
        stack = AsyncExitStack()
        self._stack = stack
        try:
            transport = self._open_transport()
            streams = await stack.enter_async_context(transport)
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            await session.initialize()
            logger.info(
                "MCP client connected to memtomem server via %s: %s",
                self._config.ltm_mcp_transport,
                self._target_display(),
            )
            await self._negotiate_format(session)
            # Publish only after initialize + negotiation succeed: `_session`
            # is the readiness signal `_heal_if_needed`'s fast path trusts
            # without taking `_start_lock`, and an abandoned start (#664) runs
            # concurrently with later callers — publishing earlier would hand
            # them a session that never completed the MCP handshake.
            self._session = session
            self._generation += 1
        except BaseException:
            # Roll back any contexts we entered (transport subprocess, session
            # streams) so a failed start — common during reconnect storms —
            # doesn't leak file descriptors and child processes across retries.
            try:
                await stack.aclose()
            except Exception:
                logger.debug("Error during MCP client start() cleanup", exc_info=True)
            self._stack = None
            self._session = None
            raise

    def _open_transport(self):  # noqa: ANN201
        match self._config.ltm_mcp_transport:
            case "sse":
                return sse_client(
                    self._config.ltm_mcp_url,
                    headers=self._config.ltm_mcp_headers,
                )
            case "streamable_http":
                return streamablehttp_client(
                    self._config.ltm_mcp_url,
                    headers=self._config.ltm_mcp_headers,
                )
            case _:
                params = StdioServerParameters(
                    command=self._config.ltm_mcp_command,
                    args=self._config.ltm_mcp_args,
                )
                return stdio_client(params)

    def _target_display(self) -> str:
        """Loggable connection target — URL userinfo is redacted.

        ``ltm_mcp_url`` may carry ``user:password@`` credentials (basic-auth
        proxies in front of a network LTM, #398), and this string goes to
        INFO logs in ``start()`` and ``_reconnect()``. Only the display is
        redacted; the transport itself receives the configured URL verbatim.
        """
        if self._config.ltm_mcp_transport == "stdio":
            return self._config.ltm_mcp_command
        return redact_url_userinfo(self._config.ltm_mcp_url)

    def _scrub_exc(self, exc: BaseException) -> str:
        """Render *exc* for logging with URL userinfo scrubbed.

        httpx exceptions embed the full request URL (credentials included),
        so every log line in this module that interpolates an exception must
        go through here rather than passing ``exc`` to the logger raw.
        """
        return redact_exception_text(str(exc), self._config.ltm_mcp_url)

    async def _negotiate_format(self, session: ClientSession | None = None) -> None:
        """Negotiate additive core capabilities and the search result format.

        Called at the end of ``_do_start`` with the not-yet-published session
        (``self._session`` only becomes visible after negotiation succeeds,
        #664). When ``result_format`` is ``"structured"``, asks the remote
        server for its capabilities via ``mem_do(action="version")``.  If the
        response doesn't list ``"structured"`` in
        ``capabilities.search_formats`` — or if the call fails (older core
        versions don't implement this action) — the parser is silently
        downgraded to ``CompactResultParser``.
        """
        # Capabilities belong to one negotiated session.  Clear them before
        # every attempt so reconnects cannot retain features from a previous
        # core (including a structured -> compact downgrade).
        self._capabilities = LtmCapabilities()
        if session is None:
            session = self._session
        if session is None:
            return
        if not isinstance(self._parser, StructuredResultParser):
            # Preserve the legacy compact path's zero-negotiation behavior.
            # Optional compose/formation features require the default
            # structured contract.
            return

        data: dict[str, Any] = {}
        try:
            result = await session.call_tool("mem_do", {"action": "version"})
            text_parts = [c.text or "" for c in result.content if c.type == "text"]
            if text_parts:
                parsed = json.loads(text_parts[0])
                if isinstance(parsed, dict):
                    data = parsed
        except Exception as exc:
            logger.debug("Version negotiation failed (older core?): %s", self._scrub_exc(exc))

        raw_caps = data.get("capabilities", {})
        caps = raw_caps if isinstance(raw_caps, dict) else {}

        def schema_version(name: str) -> int:
            value = caps.get(name)
            if value is True:
                return 1
            if isinstance(value, int) and not isinstance(value, bool):
                return max(0, value)
            if isinstance(value, dict):
                raw = value.get("schema_version", value.get("schema", 0))
                return max(0, raw) if isinstance(raw, int) and not isinstance(raw, bool) else 0
            return 0

        formats = caps.get("search_formats", [])
        if isinstance(formats, list) and "structured" in formats:
            scratch_formats = caps.get("scratch_formats", [])
            self._capabilities = LtmCapabilities(
                context_compose_schema=schema_version("context_compose"),
                candidate_propose_schema=schema_version("candidate_propose"),
                structured_scratch=(
                    isinstance(scratch_formats, list) and "structured" in scratch_formats
                ),
                increment_access=bool(caps.get("increment_access", False)),
            )
            logger.info("Core supports structured format — keeping StructuredResultParser")
        else:
            logger.info("Core does not advertise structured format — falling back to compact")
            self._parser = CompactResultParser()

    @property
    def capabilities(self) -> LtmCapabilities:
        return self._capabilities

    # Bounded join for the owner task at ``stop()``. Generous: a healthy
    # owner only has to finish (or roll back) the current lifecycle op and
    # aclose the exit stack.
    _STOP_TIMEOUT_SECONDS = 5.0

    async def stop(self) -> None:
        """Disconnect from the memtomem MCP server.

        Safe to call from any task: the context teardown runs in the owner
        task that entered the contexts (#663). Idempotent; stop before any
        start is a no-op. If an abandoned start (#664) is still in flight,
        the bounded join below cancels the owner after
        ``_STOP_TIMEOUT_SECONDS`` — the cancel lands inside ``_do_start``,
        whose rollback acloses the contexts in-task, so nothing leaks.
        """
        self._stopped = True
        owner = self._owner_task
        if owner is None or owner.done():
            # Never started, or the owner already exited. If the owner died
            # with contexts still open (external cancellation mid-lifetime),
            # no task can exit those scopes safely anymore — abandon them
            # (the daemon's leak sweep covers the child process) instead of
            # tripping the cross-task cancel-scope RuntimeError.
            if self._stack is not None:
                logger.warning(
                    "LTM adapter owner task already gone — abandoning open MCP client contexts"
                )
            self._stack = None
            self._session = None
            return
        req = _OwnerRequest("close", asyncio.get_running_loop().create_future())
        self._requests.put_nowait(req)
        try:
            # ``wait_for`` cancels the owner on timeout (delivered inside the
            # owner task → any in-flight op rolls back in-task) and awaits it
            # before raising, so no extra cancel/join pass is needed.
            await asyncio.wait_for(owner, timeout=self._STOP_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("LTM adapter owner task did not stop within timeout — cancelled")
        finally:
            # The close future may have been failed by the owner's drain
            # (close queued behind a stuck op) — consume it so it never
            # surfaces as an "exception was never retrieved" warning.
            if not req.fut.done():
                req.fut.cancel()
            elif not req.fut.cancelled():
                req.fut.exception()
            self._stack = None
            self._session = None
            flights = list(self._reconnect_flights.values())
            if flights:
                await asyncio.gather(*flights, return_exceptions=True)
            self._reconnect_flights.clear()

    async def _do_close(self, *, swallow_errors: bool = False) -> None:
        """Exit the transport/session contexts. Owner-task only (#663)."""
        if self._stack:
            try:
                await self._stack.aclose()
            except Exception:
                if not swallow_errors:
                    raise
                logger.debug("Error closing MCP client stack during reconnect", exc_info=True)
            finally:
                self._stack = None
                self._session = None

    def _ensure_owner(self) -> asyncio.Task[None]:
        """Return the live owner task, (re)creating it if needed.

        Created lazily on the first lifecycle op so constructing the adapter
        in ``app_lifespan`` stays I/O-free (#338). If a previous owner died
        without ``stop()`` (external cancellation), recreate it so the
        adapter degrades to "start can be retried" instead of hanging every
        subsequent caller; the sticky ``_start_attempted`` flag still governs
        whether a retry actually happens.
        """
        owner = self._owner_task
        if owner is None or owner.done():
            if owner is not None and not self._stopped:
                logger.warning("LTM adapter owner task exited unexpectedly — recreating")
                if self._stack is not None:
                    # The dead owner entered these contexts; their anyio
                    # cancel scopes are affine to that (now-gone) task, so a
                    # replacement owner must NEVER aclose them — doing so
                    # would re-raise the very cross-task cancel-scope error
                    # this design fixes (and ``reconnect`` would swallow it,
                    # leaking the LTM child). Abandon them unclosed instead;
                    # the daemon leak sweep / process exit reaps the child.
                    logger.warning("abandoning MCP client contexts entered by the dead owner task")
                    self._stack = None
                    self._session = None
            owner = asyncio.create_task(self._owner_loop(), name="ltm-adapter-owner")
            self._owner_task = owner
        return owner

    async def _submit(
        self, op: Literal["start", "reconnect"], *, expected_generation: int | None = None
    ) -> None:
        """Marshal a lifecycle op to the owner task and await its outcome."""
        if self._stopped:
            raise RuntimeError("MCP adapter is stopped")
        self._ensure_owner()
        req = _OwnerRequest(
            op, asyncio.get_running_loop().create_future(), expected_generation=expected_generation
        )
        self._requests.put_nowait(req)
        try:
            # ``shield`` so that cancelling the *caller* (SurfacingEngine's
            # ``asyncio.wait_for`` timeout) does NOT cancel ``req.fut`` — the
            # owner keeps running the op after the caller leaves, and it must
            # be able to resolve the future (success or failure) so the
            # discard callback below can observe the outcome.
            await asyncio.shield(req.fut)
        except asyncio.CancelledError:
            # The caller was cancelled while the op was queued or in flight.
            # A queued op is flagged so the owner skips it (nobody is waiting
            # for it). An in-flight op is deliberately ABANDONED — left
            # running in the owner task — so a slow LTM start (~9s ONNX model
            # load vs the 3s surfacing timeout, #664) completes and warms
            # ``_session`` for the next caller; cancelling the owner here
            # used to roll back the half-warmed child on every timeout, so
            # the LTM never reached a warm state.
            req.cancel_requested = True
            # The caller is leaving, but the owner may still resolve req.fut
            # (the abandoned op sets a result or an exception, or the
            # queued-skip cancels it). Retrieve that result so it is never
            # reported as unobserved.
            if req.fut.done():
                _discard_future_result(req.fut)
            else:
                req.fut.add_done_callback(_discard_future_result)
            raise

    async def _owner_loop(self) -> None:
        """Serve lifecycle ops in one task so scope enter/exit match (#663)."""
        try:
            while True:
                try:
                    req = await self._requests.get()
                    if req.cancel_requested or req.fut.cancelled():
                        # Caller gave up while queued. Resolve the (shielded,
                        # still-pending) future so it is not left dangling.
                        if not req.fut.done():
                            req.fut.cancel()
                        continue
                    self._current_req = req
                    try:
                        if req.op == "close":
                            await self._do_close()
                            if not req.fut.done():
                                req.fut.set_result(None)
                            return
                        if req.op == "reconnect":
                            # Close-then-start as one atomic op so another
                            # queued op can never interleave between them.
                            await self._do_close(swallow_errors=True)
                        await self._do_start()
                        if req.op == "reconnect":
                            # Clear the flag owner-side: a caller cancelled
                            # mid-reconnect never reaches the clear in
                            # ``_heal_if_needed``, and without this the next
                            # call would run a redundant close+start cycle
                            # against the fresh session just built here.
                            self._needs_reconnect = False
                            if self._dirty_generation == req.expected_generation:
                                self._dirty_generation = None
                        if not req.fut.done():
                            req.fut.set_result(None)
                    except asyncio.CancelledError:
                        # If the cancel did NOT come from this op's caller
                        # (its future would already be cancelled), the caller
                        # is still awaiting — fail its future so it degrades
                        # to ``no_session`` instead of hanging forever.
                        if not req.fut.done():
                            req.fut.set_exception(RuntimeError("LTM adapter owner task stopped"))
                        raise
                    except BaseException as exc:
                        if not req.fut.done():
                            req.fut.set_exception(exc)
                        else:
                            # Caller already left (cancelled future) — log
                            # instead of losing the failure entirely.
                            logger.debug(
                                "LTM lifecycle op %s failed after caller left: %s",
                                req.op,
                                self._scrub_exc(exc),
                            )
                    finally:
                        self._current_req = None
                except asyncio.CancelledError:
                    if self._stopped:
                        # ``stop()``'s bounded join cancelled the owner (a
                        # caller timeout never does — it abandons the op,
                        # #664). Exit cleanly: the in-flight op's own rollback
                        # already aclosed its stack in this task, and
                        # ``uncancel``-ing (rather than re-raising) makes
                        # ``await owner`` complete normally so ``stop()``
                        # never sees a stray ``CancelledError``.
                        task = asyncio.current_task()
                        if task is not None:
                            while task.uncancel() > 0:
                                pass
                        return
                    raise
        finally:
            # Fail-fast everything still queued so no caller ever hangs on
            # an abandoned future (they surface as ``no_session`` through
            # ``_heal_if_needed``'s exception path).
            self._current_req = None
            while not self._requests.empty():
                leftover = self._requests.get_nowait()
                if not leftover.fut.done():
                    leftover.fut.set_exception(RuntimeError("LTM adapter owner task stopped"))

    # Transient failure modes that warrant tearing down and rebuilding the
    # connection. stdio pipe failures surface as OSError / EOFError /
    # BrokenPipeError; the sse and streamable_http clients (#398) raise
    # httpx.TransportError subclasses (ConnectError, ReadTimeout,
    # RemoteProtocolError, ...) whose MRO has no OSError ancestor — without
    # the httpx entry a network blip fell through to the generic handler,
    # which never reconnects, leaving surfacing dead until restart.
    # httpx.HTTPStatusError / DecodingError stay OUT: the server answered,
    # so the transport is healthy and reconnecting would mask real errors.
    _TRANSPORT_ERRORS = (
        OSError,
        ConnectionError,
        EOFError,
        BrokenPipeError,
        asyncio.TimeoutError,
        httpx.TransportError,
    )

    async def _reconnect(self, expected_generation: int | None = None) -> None:
        """Tear down and re-establish the MCP connection.

        Runs close-then-start as one atomic op in the owner task (#663);
        close errors are swallowed there, matching the old stop-then-start
        behavior.
        """
        logger.info(
            "Attempting MCP adapter reconnect via %s to %s",
            self._config.ltm_mcp_transport,
            self._target_display(),
        )
        await self._submit("reconnect", expected_generation=expected_generation)
        logger.info("MCP adapter reconnected successfully")

    def _mark_dirty(self, session: ClientSession, generation: int) -> None:
        """Mark only the currently published session generation as dirty."""
        if self._session is session and self._generation == generation:
            self._dirty_generation = generation
            self._needs_reconnect = True

    def _finish_reconnect_flight(self, generation: int, completed: asyncio.Future[None]) -> None:
        """Observe and evict one completed reconnect flight.

        A failed flight can be replaced for the same generation before its
        callback runs, so remove the mapping only when it still points at the
        completed task.
        """
        _discard_future_result(completed)
        if self._reconnect_flights.get(generation) is completed:
            self._reconnect_flights.pop(generation, None)

    async def _shared_reconnect(self, generation: int) -> None:
        """Join the single reconnect flight for a dirty generation."""
        async with self._reconnect_lock:
            flight = self._reconnect_flights.get(generation)
            if flight is None or flight.done():
                flight = asyncio.create_task(
                    self._reconnect_generation(generation),
                    name=f"ltm-reconnect-generation-{generation}",
                )

                def finish(completed: asyncio.Future[None]) -> None:
                    self._finish_reconnect_flight(generation, completed)

                flight.add_done_callback(finish)
                self._reconnect_flights[generation] = flight
        await asyncio.shield(flight)

    async def _reconnect_generation(self, generation: int) -> None:
        if self._dirty_generation != generation:
            return
        await self._reconnect(generation)
        # Normally the owner clears these atomically with publishing the new
        # session. Keep this idempotent fallback for injected/test lifecycle
        # implementations that replace ``_reconnect`` itself.
        if self._dirty_generation == generation:
            self._dirty_generation = None
            self._needs_reconnect = False

    async def _rpc(
        self, session: ClientSession, generation: int, tool: str, args: dict[str, Any]
    ) -> Any:
        """Run one RPC and dirty only the session cancelled mid-call."""
        try:
            return await session.call_tool(tool, args)
        except asyncio.CancelledError:
            self._mark_dirty(session, generation)
            raise

    async def _heal_if_needed(self) -> bool:
        """Ready the session for the next RPC.

        Three transitions:

        * No session yet → lazy-start the LTM client. First concurrent
          caller wins; failure is sticky for this adapter's lifetime to
          avoid respawning the LTM subprocess on every cycle (matches
          the prior single-shot ``app_lifespan`` behavior).
        * Session marked for reconnect (#290) → tear down and rebuild
          before the RPC.
        * Healthy session → fast path, no I/O.

        Returns ``True`` when the session is ready, ``False`` when
        callers should treat the adapter as unavailable.
        """
        # Heal a dirty generation before considering the session-less lazy
        # start path. A failed reconnect may have closed the old session; the
        # dirty marker must remain retryable instead of becoming sticky-off.
        if self._needs_reconnect and self._dirty_generation is None:
            # Compatibility for state restored by older callers/tests; all
            # new dirty writes go through ``_mark_dirty``.
            self._dirty_generation = self._generation
        dirty_generation = self._dirty_generation
        if dirty_generation is not None:
            try:
                await self._shared_reconnect(dirty_generation)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Lazy reconnect failed: %s", self._scrub_exc(exc))
                return False
            return self._session is not None

        if self._session is None:
            async with self._start_lock:
                # Re-check under lock: another coroutine may have raced
                # us through start() while we were waiting on the lock.
                if self._session is not None:
                    return True
                if self._start_attempted:
                    return False
                self._start_attempted = True
                try:
                    await self.start()
                except asyncio.CancelledError:
                    # Outer wait_for / timeout cancelled us mid-init
                    # (SurfacingEngine wraps adapter calls in
                    # ``asyncio.wait_for``). The in-flight start keeps
                    # running abandoned in the owner task (#664); reset
                    # the sticky flag so the next call re-enters this
                    # path instead of short-circuiting — its start op
                    # then either no-ops on ``_do_start``'s session
                    # guard (the abandoned start succeeded) or genuinely
                    # retries (it failed). Propagate per the cooperative
                    # cancellation contract (#290).
                    self._start_attempted = False
                    raise
                except Exception as exc:
                    logger.warning(
                        "Lazy MCP adapter start failed — surfacing disabled: %s",
                        self._scrub_exc(exc),
                    )
                    return False
                return True
        return True

    async def search(
        self,
        query: str,
        top_k: int | None = None,
        namespace: str | list[str] | None = None,
        context_window: int | None = None,
        *,
        trace_id: str | None = None,
        **kwargs: Any,
    ) -> tuple[list[RemoteSearchResult], list[str], SearchOutcome]:
        """Call mem_search on the remote server and parse results.

        Returns ``(results, hints, outcome)``. ``hints`` carries parent-side
        trust-UX annotations from the structured format (parent PR #231)
        and is always ``[]`` for the compact format or when the call
        fails. ``outcome`` lets the engine distinguish "LTM unavailable"
        from "LTM call failed" from "parser hit an empty response" from
        "call OK, zero hits" (#295) — every failure path used to collapse
        to ``([], [])`` and looked identical to a healthy empty
        namespace. Callers that don't care about hints/outcome should
        still accept the tuple to stay compatible with mypy's inferred
        signature.
        """
        if not await self._heal_if_needed():
            return [], [], "no_session"
        # ``_heal_if_needed`` guarantees a live session on True; the asserts
        # make that invariant visible to mypy (``self._session`` is typed
        # ``ClientSession | None``).
        assert self._session is not None
        session = self._session
        generation = self._generation

        args: dict[str, Any] = {"query": query}
        if top_k is not None:
            args["top_k"] = top_k
        if namespace is not None:
            # Core's mem_search accepts str|None; normalize lists to
            # comma-separated strings which NamespaceFilter.parse() handles.
            args["namespace"] = ",".join(namespace) if isinstance(namespace, list) else namespace
        if context_window is not None and context_window > 0:
            args["context_window"] = context_window
        if trace_id is not None:
            args["_trace_id"] = trace_id
        if isinstance(self._parser, StructuredResultParser):
            args["output_format"] = "structured"

        try:
            result = await self._rpc(session, generation, "mem_search", args)
        except self._TRANSPORT_ERRORS as exc:
            logger.warning("MCP transport error, attempting reconnect: %s", self._scrub_exc(exc))
            self._mark_dirty(session, generation)
            retry_session = session
            retry_generation = generation
            try:
                await self._shared_reconnect(generation)
                assert self._session is not None
                retry_session = self._session
                retry_generation = self._generation
                result = await self._rpc(retry_session, retry_generation, "mem_search", args)
            except asyncio.CancelledError:
                raise
            except self._TRANSPORT_ERRORS as retry_exc:
                self._mark_dirty(retry_session, retry_generation)
                logger.warning(
                    "MCP mem_search failed after reconnect: %s", self._scrub_exc(retry_exc)
                )
                return [], [], "transport_error"
            except Exception as retry_exc:
                # Upstream LTM is unreachable; surfacing will return empty.
                logger.warning(
                    "MCP mem_search failed after reconnect: %s", self._scrub_exc(retry_exc)
                )
                return [], [], "transport_error"
        except asyncio.CancelledError:
            # #290: outer wait_for cancelled us mid-RPC. Mark for lazy
            # reconnect on the next call (the session's read/write streams
            # are now in a half-read state) and propagate the cancellation
            # so the caller's wait_for can surface its TimeoutError.
            self._mark_dirty(session, generation)
            raise
        except Exception as exc:
            logger.warning("MCP mem_search failed: %s", self._scrub_exc(exc))
            return [], [], "call_error"

        # MagicMock/fake results often omit the optional field and fabricate a
        # truthy attribute on access. Only the protocol's literal boolean true
        # is an error envelope.
        if getattr(result, "isError", False) is True:
            logger.warning("MCP mem_search returned isError=true")
            return [], [], "upstream_error"

        # Parse text response into results
        # ``result.content or []`` tolerates spec-noncompliant upstreams that
        # return ``None`` instead of an empty list (mirrors PR #114 in proxy).
        # ``c.text or ""`` further tolerates ``TextContent.text=None`` from
        # spec-noncompliant servers (mirrors manager.py:1042). The ``cast``
        # narrows the call_tool union (TextContent | Image | Audio | …) to
        # the branch the ``c.type == "text"`` filter already selects.
        text_parts = [
            cast(TextContent, c).text or "" for c in (result.content or []) if c.type == "text"
        ]
        if not text_parts:
            return [], [], "empty_content"

        text = "\n".join(text_parts)
        if isinstance(self._parser, StructuredResultParser):
            results, hints, valid = self._parser.parse_checked(
                text, max_content_chars=self._config.result_content_max_chars
            )
            if not valid:
                return [], [], "parse_error"
        else:
            results, hints = self._parser.parse(
                text, max_content_chars=self._config.result_content_max_chars
            )
        outcome: SearchOutcome = "ok" if results else "empty_results"
        return results, hints, outcome

    async def _call_mem_do(
        self,
        action: str,
        params: dict[str, Any],
        *,
        trace_id: str | None = None,
    ) -> Any:
        """Call a negotiated core action with the standard reconnect contract."""
        if not await self._heal_if_needed():
            raise RuntimeError("LTM session unavailable")
        assert self._session is not None
        session = self._session
        generation = self._generation
        args: dict[str, Any] = {"action": action, "params": params}
        if trace_id is not None:
            args["_trace_id"] = trace_id
        try:
            return await self._rpc(session, generation, "mem_do", args)
        except self._TRANSPORT_ERRORS:
            self._mark_dirty(session, generation)
            await self._shared_reconnect(generation)
            assert self._session is not None
            retry_session = self._session
            retry_generation = self._generation
            try:
                return await self._rpc(retry_session, retry_generation, "mem_do", args)
            except asyncio.CancelledError:
                self._mark_dirty(retry_session, retry_generation)
                raise
            except self._TRANSPORT_ERRORS:
                self._mark_dirty(retry_session, retry_generation)
                raise
        except asyncio.CancelledError:
            self._mark_dirty(session, generation)
            raise

    @staticmethod
    def _result_text(result: Any) -> str:
        parts = [
            cast(TextContent, item).text or ""
            for item in (getattr(result, "content", None) or [])
            if item.type == "text"
        ]
        return "\n".join(parts)

    async def context_compose(
        self,
        query: str,
        *,
        agent_id: str | None = None,
        max_chars: int = 3000,
        top_k: int = 10,
        trace_id: str | None = None,
    ) -> ContextComposeResult | None:
        """Return a pinned-first structured bundle when core advertises it."""
        if not await self._heal_if_needed():
            return None
        if self._capabilities.context_compose_schema < 1:
            return None
        params: dict[str, Any] = {
            "query": query,
            "max_chars": max_chars,
            "top_k": top_k,
        }
        if agent_id:
            params["agent_id"] = agent_id
        try:
            result = await self._call_mem_do("context_compose", params, trace_id=trace_id)
        except self._TRANSPORT_ERRORS as exc:
            raise LtmTransportError("core context_compose transport unavailable") from exc
        if getattr(result, "isError", False) is True:
            raise RuntimeError("core context_compose returned isError=true")
        try:
            payload = json.loads(self._result_text(result))
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("core context_compose returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("core context_compose result is not an object")

        pinned: list[RemoteSearchResult] = []
        for item in payload.get("pinned", []):
            if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                raise ValueError("core context_compose pinned item has invalid shape")
            entry = RemoteSearchResult(
                item["content"][: self._config.result_content_max_chars],
                1.0,
                source=str(item.get("source_path") or item.get("block_id") or "pinned"),
                namespace=str(item.get("scope") or "default"),
                pinned=True,
            )
            block_id = item.get("block_id") or item.get("id")
            if isinstance(block_id, str) and block_id:
                entry.chunk.id = block_id
            pinned.append(entry)

        retrieved: list[RemoteSearchResult] = []
        for item in payload.get("retrieved", []):
            if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                raise ValueError("core context_compose retrieved item has invalid shape")
            entry = RemoteSearchResult(
                item["content"][: self._config.result_content_max_chars],
                safe_float(item.get("score"), 0.0),
                source=str(item.get("source") or "unknown"),
                namespace=str(item.get("namespace") or "default"),
            )
            memory_id = item.get("id") or item.get("chunk_id")
            if isinstance(memory_id, str) and memory_id:
                entry.chunk.id = memory_id
            retrieved.append(entry)

        raw_warnings = payload.get("warnings", [])
        raw_omitted = payload.get("omitted_block_ids", [])
        warnings = tuple(str(v) for v in raw_warnings) if isinstance(raw_warnings, list) else ()
        omitted = tuple(str(v) for v in raw_omitted) if isinstance(raw_omitted, list) else ()
        return ContextComposeResult(tuple(pinned), tuple(retrieved), warnings, omitted)

    async def candidate_propose(
        self,
        content: str,
        *,
        source: str,
        source_ref: str,
        idempotency_key: str,
        trace_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Submit a pending review candidate; never fall back to direct mem_add."""
        if not await self._heal_if_needed():
            raise RuntimeError("LTM session unavailable")
        if self._capabilities.candidate_propose_schema < 1:
            return None
        result = await self._call_mem_do(
            "candidate_propose",
            {
                "content": content,
                "source": source,
                "source_ref": source_ref,
                "idempotency_key": idempotency_key,
            },
            trace_id=trace_id,
        )
        if getattr(result, "isError", False) is True:
            raise RuntimeError("core candidate_propose returned isError=true")
        try:
            payload = json.loads(self._result_text(result))
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("core candidate_propose returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("core candidate_propose result is not an object")
        return payload

    async def increment_access(self, chunk_ids: list[str], *, trace_id: str | None = None) -> None:
        """Boost the access_count of the given chunks via mem_do(increment_access).

        Used by ``SurfacingEngine.handle_feedback`` when an agent rates a
        surfaced memory as ``helpful``. Failures are silent (debug log
        only) — feedback recording itself must never depend on the boost
        round trip succeeding.
        """
        if not chunk_ids:
            return
        if not await self._heal_if_needed():
            return
        assert self._session is not None  # ``_heal_if_needed`` guarantees live session
        session = self._session
        generation = self._generation

        call_args: dict[str, Any] = {
            "action": "increment_access",
            "params": {"chunk_ids": chunk_ids},
        }
        if trace_id is not None:
            call_args["_trace_id"] = trace_id

        try:
            await self._rpc(session, generation, "mem_do", call_args)
        except self._TRANSPORT_ERRORS as exc:
            logger.warning(
                "MCP transport error in increment_access, reconnecting: %s", self._scrub_exc(exc)
            )
            self._mark_dirty(session, generation)
            retry_session = session
            retry_generation = generation
            try:
                await self._shared_reconnect(generation)
                assert self._session is not None
                retry_session = self._session
                retry_generation = self._generation
                await self._rpc(retry_session, retry_generation, "mem_do", call_args)
            except asyncio.CancelledError:
                raise
            except self._TRANSPORT_ERRORS as retry_exc:
                self._mark_dirty(retry_session, retry_generation)
                logger.debug(
                    "MCP mem_do(increment_access) failed after reconnect: %s",
                    self._scrub_exc(retry_exc),
                )
            except Exception as retry_exc:
                logger.debug(
                    "MCP mem_do(increment_access) failed after reconnect: %s",
                    self._scrub_exc(retry_exc),
                )
        except asyncio.CancelledError:
            # #290: see search() — mid-RPC cancellation marks the session
            # for lazy reconnect; propagate per the cooperative model.
            self._mark_dirty(session, generation)
            raise
        except Exception as exc:
            logger.debug("MCP mem_do(increment_access) failed: %s", self._scrub_exc(exc))

    async def scratch_list(self, *, trace_id: str | None = None) -> list[dict]:
        """Fetch working memory entries via mem_do(action="scratch_get").

        The remote core's ``mem_scratch_get`` returns a human-readable
        listing when called with no key. We parse it back into the
        ``[{"key": ..., "value": ...}, ...]`` shape that
        :class:`SurfacingFormatter` expects.

        Returns an empty list if the session is not started, the call
        fails, or working memory is empty — surfacing must always be
        able to silently skip session-context injection without losing
        the LTM hits.
        """
        if not await self._heal_if_needed():
            return []
        assert self._session is not None  # ``_heal_if_needed`` guarantees live session
        session = self._session
        generation = self._generation

        call_args: dict[str, Any] = {"action": "scratch_get", "params": {}}
        if trace_id is not None:
            call_args["_trace_id"] = trace_id

        try:
            result = await self._rpc(session, generation, "mem_do", call_args)
        except self._TRANSPORT_ERRORS as exc:
            logger.warning(
                "MCP transport error in scratch_list, reconnecting: %s", self._scrub_exc(exc)
            )
            self._mark_dirty(session, generation)
            retry_session = session
            retry_generation = generation
            try:
                await self._shared_reconnect(generation)
                assert self._session is not None
                retry_session = self._session
                retry_generation = self._generation
                result = await self._rpc(retry_session, retry_generation, "mem_do", call_args)
            except asyncio.CancelledError:
                raise
            except self._TRANSPORT_ERRORS:
                self._mark_dirty(retry_session, retry_generation)
                return []
            except Exception:
                return []
        except asyncio.CancelledError:
            # #290: see search() — mid-RPC cancellation marks the session
            # for lazy reconnect; propagate per the cooperative model.
            self._mark_dirty(session, generation)
            raise
        except Exception as exc:
            logger.debug("MCP mem_do(scratch_get) failed: %s", self._scrub_exc(exc))
            return []

        # ``result.content or []`` tolerates spec-noncompliant upstreams that
        # return ``None`` instead of an empty list (mirrors PR #114 in proxy).
        # ``c.text or ""`` further tolerates ``TextContent.text=None`` from
        # spec-noncompliant servers; see ``search()`` for the full rationale.
        text_parts = [
            cast(TextContent, c).text or "" for c in (result.content or []) if c.type == "text"
        ]
        if not text_parts:
            return []

        return self._parse_scratch_list("\n".join(text_parts))

    @staticmethod
    def _parse_scratch_list(text: str) -> list[dict]:
        """Parse ``mem_scratch_get`` listing output into entry dicts.

        Expected format from core (mem_scratch_get with key=None)::

            Working memory: 2 entries

              key1: value preview... (expires: 2026-04-09T12:00:00) [promoted]
              key2: another value...

        Each entry line starts with two leading spaces. The trailing
        ``...`` marker is stripped (core always appends it after the
        truncated preview); ``(expires: ...)`` and ``[promoted]`` are
        captured into optional fields.

        Keys may contain ``: `` (e.g., ``db: config``).  Core always
        appends ``...`` after the value preview, so we split from the
        right at the *last* ``: `` that precedes a value ending in
        ``...`` (or metadata).  If the text has no trailing ``...``,
        fall back to the first ``: `` split (best-effort).
        """
        if not text or "Working memory is empty" in text:
            return []

        entries: list[dict] = []
        for line in text.splitlines():
            if not line.startswith("  "):
                continue
            body = line[2:]

            # Best-effort key/value split.  Core always appends "..."
            # after the value preview, so look for the last ": " that
            # sits before trailing markers.  Fall back to first ": ".
            if "..." in body:
                # Find the ": " closest to the trailing "..." marker
                trail_pos = body.rfind("...")
                sep_pos = body.rfind(": ", 0, trail_pos)
                if sep_pos < 0:
                    sep_pos = body.find(": ")
            else:
                sep_pos = body.find(": ")

            if sep_pos < 0:
                continue
            key = body[:sep_pos]
            rest = body[sep_pos + 2 :]

            value_part = rest
            promoted = False
            if value_part.endswith(" [promoted]"):
                value_part = value_part[: -len(" [promoted]")]
                promoted = True

            expires_at: str | None = None
            expires_match = re.search(r"\s*\(expires:\s*([^)]+)\)\s*$", value_part)
            if expires_match:
                expires_at = expires_match.group(1)
                value_part = value_part[: expires_match.start()]

            if value_part.endswith("..."):
                value_part = value_part[:-3]

            entry: dict = {"key": key, "value": value_part}
            if expires_at is not None:
                entry["expires_at"] = expires_at
            if promoted:
                entry["promoted"] = True
            entries.append(entry)

        return entries

    @staticmethod
    def _parse_results(text: str) -> list[RemoteSearchResult]:
        """Parse mem_search formatted output into RemoteSearchResult objects.

        Delegates to :class:`CompactResultParser`. Kept as a static method
        for backward compatibility with existing tests and callers.
        Compact parser never emits hints; this helper drops the second
        tuple slot and returns only the results list.
        """
        results, _ = _compact_parser.parse(text)
        return results
