"""Streamable-HTTP transport helper for the mcp 2.0 client SDK.

1.x's ``streamablehttp_client(url, headers=..., timeout=..., sse_read_timeout=...)``
built its own HTTP client. 2.0's ``streamable_http_client(url, *, http_client=...)``
takes one instead, and does **not** close a client the caller supplied — so
every call site has to own that lifetime. All three of ours (proxy upstreams,
the surfacing LTM client, the CLI probes) already run inside an
``AsyncExitStack`` on a single task, so this wrapper enters both the client
and the transport as one context manager and hands back the streams.

Timeout mapping from the old two-parameter shape:

* ``timeout=X`` (connect budget) and ``sse_read_timeout=Y`` (long-lived read)
  become ``httpx2.Timeout(X, read=Y)`` — ``Timeout(X)`` sets connect, write
  and pool to X, and ``read=`` overrides the read leg.
* Passing ``timeout=None`` keeps the SDK's own defaults (30s connect/write/pool,
  300s read), which is what a call site that passed no timeouts used to get.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Generic, TypeVar, cast

import anyio
import httpx2

from mcp import types as mcp_types
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.shared.dispatcher import coerce_request_id
from mcp.shared.message import SessionMessage

from memtomem_stm.utils.json_size import json_utf8_size_async

# The SDK's SSE-friendly read default. Named here because the proxy pins the
# read leg while overriding the connect leg: a long-lived stream must not
# inherit the connect budget.
SSE_READ_TIMEOUT_SECONDS = 300.0

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Private server-error code and data marker used only between the bounded
# stream wrapper and ProxyManager.  A response that is too large has already
# been decoded by the SDK transport, but must not disappear into the
# dispatcher's generic CONNECTION_CLOSED fan-out: preserving its request id
# lets the pending call receive this recognizable, non-retryable outcome.
INBOUND_MESSAGE_TOO_LARGE_CODE = -32098
_INBOUND_MESSAGE_TOO_LARGE_MARKER = "memtomem_stm.inbound_message_too_large"

# Ceiling on rejections still waiting for the write side. Each one is a small
# error frame, but a peer that floods oversize reverse requests must not be
# able to grow an unbounded task set behind a stalled writer.
_MAX_PENDING_REJECTIONS = 32


# Ceiling on request ids awaiting their reply's measurement. A slot outlives its
# call only when the reply never arrives, and that path ends in a reconnect that
# drops the whole correlator — this bound just keeps a pathological in-flight
# count from growing without limit.
_MAX_PENDING_CORRELATIONS = 4096


@dataclass
class ResponseSize:
    """What the wire check measured for the one response a call is waiting on.

    ``BoundedReadStream`` fills this in when it delivers a correlated response,
    so a caller can tell whether ``max_upstream_bytes`` was already enforced on
    that exact response instead of guessing from the connection it arrived on.
    Both fields stay ``None`` when no such measurement happened: an injected
    session with no bounded stream, a reply that never came, or one replaced by
    the oversize error (which fails the call before any caller reads this).

    ``envelope_bytes`` is that message's compact UTF-8 JSON size, the same
    number ``json_utf8_size`` produces anywhere else — not a count of raw bytes
    off the socket, which the SDK has already decoded by this point.

    A slot describes ONE request, and takes the first one sent while it is
    armed. That matters because an MCP call is not always one request: this
    SDK's ``call_tool`` follows a result with its own ``list_tools`` to validate
    the output schema, and a slot that kept accepting registrations would end up
    reporting that second, smaller envelope as though it were the result's.
    ``rearm()`` starts the next request's measurement — one per retry attempt.
    """

    envelope_bytes: int | None = None
    limit_applied: int | None = None
    armed: bool = True
    # Set by ``ResponseCorrelator.register`` so a slot can retire its own entry
    # when no reply ever comes for it. Typed loosely because the correlator is
    # defined below.
    _registration: Any = field(default=None, repr=False, compare=False)

    def rearm(self) -> None:
        """Discard any measurement and accept the next request sent."""
        self.release()
        self.envelope_bytes = None
        self.limit_applied = None
        self.armed = True

    def release(self) -> None:
        """Drop a registration whose reply never arrived.

        A cancelled or timed-out call leaves its request id waiting for a
        response the correlator will never see. Nothing else retires it — the
        session may well outlive the call — so the slot does it on the way out,
        which is why a long-lived connection cannot accumulate dead entries.
        """
        registration, self._registration = self._registration, None
        if registration is None:
            return
        correlator, key = registration
        if correlator.pending.get(key) is self:
            del correlator.pending[key]


_RESPONSE_SIZE: ContextVar[ResponseSize | None] = ContextVar(
    "memtomem_stm_response_size", default=None
)


@contextmanager
def measured_response() -> Iterator[ResponseSize]:
    """Open a slot for the response of the request sent inside this block.

    The MCP client SDK writes a request from the task that awaited it, so a
    context variable set here is visible to ``CorrelatingWriteStream.send``,
    which is what ties an id to this slot. Nesting is supported (the inner slot
    wins for the duration), and the slot is restored on exit either way.
    """
    slot = ResponseSize()
    token = _RESPONSE_SIZE.set(slot)
    try:
        yield slot
    finally:
        _RESPONSE_SIZE.reset(token)
        # A call that was cancelled or timed out never gets its reply; retiring
        # the id here is what keeps ``pending`` from growing for the life of a
        # connection.
        slot.release()


def current_response_size() -> ResponseSize | None:
    """The slot open in this context, if any."""
    return _RESPONSE_SIZE.get()


@dataclass
class ResponseCorrelator:
    """Request ids whose reply should report its measured envelope size.

    Shared by one connection's write and read wrappers: the write side records
    the id it just sent against the caller's slot, the read side fills that slot
    when the matching reply is measured. Keyed in the SDK's own correlation
    domain (``coerce_request_id``), so a peer echoing ``"7"`` for ``7`` still
    matches, exactly as the dispatcher's own pending table does.
    """

    pending: dict[Any, ResponseSize] = field(default_factory=dict)

    def register(self, request_id: Any, slot: ResponseSize) -> None:
        if not slot.armed:
            return
        if len(self.pending) >= _MAX_PENDING_CORRELATIONS:
            logger.warning(
                "Not correlating response size for request %r: %d already pending",
                request_id,
                len(self.pending),
            )
            # The arming is spent even though the id was refused. Leaving it
            # would let the NEXT request in this context claim the slot — the
            # SDK's follow-up ``list_tools``, whose smaller envelope would then
            # stand in for the tool result's and skip its size check.
            slot.armed = False
            return
        key = coerce_request_id(request_id)
        self.pending[key] = slot
        slot._registration = (self, key)
        # One request per arming: a later request in the same context (the
        # SDK's own follow-up ``list_tools``, say) must not claim this slot.
        slot.armed = False

    def take(self, request_id: Any) -> ResponseSize | None:
        slot = self.pending.pop(coerce_request_id(request_id), None)
        if slot is not None:
            slot._registration = None
        return slot


class CorrelatingWriteStream:
    """Write-stream wrapper that ties an outbound request to the caller's slot.

    Only outbound *requests* are registered, and only while a slot is open in
    the sending context — a notification has no reply, and a request sent with
    no slot (the SDK's own ``initialize``, a reverse-RPC answer) simply passes
    through. Unlike the write side ``BoundedReadStream`` borrows for rejections,
    this wrapper occupies ``ClientSession``'s write position, so it forwards
    ``aclose`` and the async-context protocol to the stream it wraps.
    """

    def __init__(self, stream: Any, correlator: ResponseCorrelator) -> None:
        self._stream = stream
        self._correlator = correlator

    async def send(self, item: Any) -> None:
        message = getattr(item, "message", item)
        if isinstance(message, mcp_types.JSONRPCRequest):
            slot = _RESPONSE_SIZE.get()
            if slot is not None:
                self._correlator.register(message.id, slot)
        await self._stream.send(item)

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def __aenter__(self) -> "CorrelatingWriteStream":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        await self.aclose()
        return None

    def __getattr__(self, name: str) -> Any:
        # Transport write streams carry helpers the SDK reaches for directly
        # (``send_nowait``, ``statistics``, ...). Forward anything not wrapped.
        return getattr(self._stream, name)


class BoundedReadStream(Generic[_T]):
    """Read-stream wrapper enforcing a per-message UTF-8 JSON byte limit.

    MCP transports parse framing before publishing ``SessionMessage`` values.
    This wrapper sits at the next shared boundary, before ``ClientSession``
    dispatch and result-model validation, and works for stdio, SSE, and
    streamable HTTP without forking SDK transport implementations.

    Given the paired write stream it also *answers* an oversize server->client
    request instead of dropping it (#960); the read side alone cannot, and the
    sender of such a request is blocked until it gets a reply. The write stream
    is borrowed, never closed here: ``JSONRPCDispatcher.run`` owns its
    lifetime.
    """

    def __init__(
        self,
        stream: Any,
        max_bytes: int | Callable[[], int],
        write_stream: Any | None = None,
        correlator: ResponseCorrelator | None = None,
    ) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._write_stream = write_stream
        # Shared with this connection's ``CorrelatingWriteStream``; reports the
        # measurement back to whoever is waiting on this reply.
        self._correlator = correlator
        # Rejections in flight. Held so the tasks are not garbage collected
        # mid-send, and so their number can be capped.
        self._pending_rejections: set[asyncio.Task[None]] = set()

    def _current_max_bytes(self) -> int:
        value = self._max_bytes() if callable(self._max_bytes) else self._max_bytes
        if value <= 0:  # defensive: ProxyConfig validates this before runtime
            raise ValueError("max_upstream_bytes must be positive")
        return value

    @property
    def last_context(self) -> Any:
        return getattr(self._stream, "last_context", None)

    async def receive(self) -> _T:
        while True:
            item = await self._stream.receive()
            if isinstance(item, Exception):
                return cast(_T, item)
            message = getattr(item, "message", item)
            max_bytes = self._current_max_bytes()
            # Cheap for the small messages that dominate this loop: the sizer
            # measures on this thread under its own sync budget and reaches its
            # own worker only for a plausibly large payload, so an inbound
            # notification never waits on the shared default executor (#956).
            size = await json_utf8_size_async(message, limit=max_bytes)
            if size <= max_bytes:
                # Report the measurement to the caller waiting on this reply, so
                # it need not measure the same bytes again (#957). Only an
                # admitted response is reported: the oversize branches below
                # fail the call outright, and a caller must never read a slot
                # that says "within the cap" for a message that was not.
                self._report_size(message, size, max_bytes)
                return item
            if isinstance(message, (mcp_types.JSONRPCResponse, mcp_types.JSONRPCError)):
                if self._correlator is not None:
                    # Retire the registration without filling it. The caller
                    # gets the oversize error below and fails; leaving the entry
                    # behind would keep one dead slot per rejected response for
                    # the life of the connection.
                    self._correlator.take(message.id)
                # Raising here crashes JSONRPCDispatcher.run(), whose finally
                # block wakes every waiter with the indistinguishable
                # "Connection closed" error. Replace only correlated responses
                # with a compact error carrying the same id, so the dispatcher
                # resolves exactly that pending request and ProxyManager can
                # classify it as OVERSIZE without retrying.
                return cast(
                    _T,
                    SessionMessage(
                        mcp_types.JSONRPCError(
                            jsonrpc="2.0",
                            id=message.id,
                            error=mcp_types.ErrorData(
                                code=INBOUND_MESSAGE_TOO_LARGE_CODE,
                                message=(
                                    f"Inbound MCP response exceeded max_upstream_bytes={max_bytes}"
                                ),
                                data={_INBOUND_MESSAGE_TOO_LARGE_MARKER: True},
                            ),
                        ),
                        metadata=getattr(item, "metadata", None),
                    ),
                )
            if isinstance(message, mcp_types.JSONRPCRequest) and self._write_stream is not None:
                # A server->client request IS correlated, just the other way
                # round: its id identifies a reverse RPC (sampling, roots,
                # elicitation) whose upstream sender is blocked on our answer.
                # Dropping it left that handler waiting until its own timeout,
                # or until our call timeout tore the session down (#960).
                self._spawn_oversize_rejection(self._write_stream, message.id, max_bytes)
                continue
            # A notification has no waiter to fail, and neither does a request
            # we have no write side for. Raising here would escape
            # ``JSONRPCDispatcher.run``'s ``async for`` — it catches only
            # ``ClosedResourceError`` — tearing down the task group and waking
            # EVERY in-flight call with the indistinguishable "Connection
            # closed" error, which is exactly what the branches above exist to
            # avoid. Drop the over-budget message and keep reading.
            logger.warning(
                "Dropping inbound MCP %s exceeding max_upstream_bytes=%d",
                type(message).__name__,
                max_bytes,
            )

    def _report_size(self, message: Any, size: int, max_bytes: int) -> None:
        """Fill the slot of the call this response answers, if one is waiting."""
        if self._correlator is None:
            return
        if not isinstance(message, (mcp_types.JSONRPCResponse, mcp_types.JSONRPCError)):
            return
        slot = self._correlator.take(message.id)
        if slot is None:
            return
        slot.envelope_bytes = size
        slot.limit_applied = max_bytes

    def _spawn_oversize_rejection(self, write_stream: Any, request_id: Any, max_bytes: int) -> None:
        """Send the rejection from its own task, never from the read loop.

        Client transports create both directions with a buffer size of 0, so a
        ``send`` is a rendezvous with the transport's writer task. Awaiting it
        here would park ``JSONRPCDispatcher.run``'s only read loop, and for
        stdio that is a deadlock rather than a stall: an upstream blocked
        writing to a stdout pipe we have stopped draining will not read its
        stdin, so the writer we are waiting on never drains either. The SDK
        keeps its own read loop clear the same way, spawning ``_write_error``
        instead of awaiting it.
        """
        if len(self._pending_rejections) >= _MAX_PENDING_REJECTIONS:
            logger.warning(
                "Dropping oversize-request rejection for %r: %d already awaiting the write side",
                request_id,
                len(self._pending_rejections),
            )
            return
        task = asyncio.create_task(
            self._reject_oversize_request(write_stream, request_id, max_bytes)
        )
        self._pending_rejections.add(task)
        task.add_done_callback(self._pending_rejections.discard)

    async def _reject_oversize_request(
        self, write_stream: Any, request_id: Any, max_bytes: int
    ) -> None:
        """Answer an oversize server->client request with a same-id error.

        The reply travels to the upstream server, so it carries a spec error
        code rather than ``INBOUND_MESSAGE_TOO_LARGE_CODE`` — that private code
        and its marker exist only for local correlation of an oversize
        *response* to the pending call it belongs to, and must not become an
        outbound convention.
        """
        logger.warning(
            "Rejecting inbound MCP JSONRPCRequest %r exceeding max_upstream_bytes=%d",
            request_id,
            max_bytes,
        )
        try:
            await write_stream.send(
                SessionMessage(
                    mcp_types.JSONRPCError(
                        jsonrpc="2.0",
                        id=request_id,
                        error=mcp_types.ErrorData(
                            code=mcp_types.INVALID_REQUEST,
                            message=(
                                f"Inbound MCP request exceeded max_upstream_bytes={max_bytes}"
                            ),
                        ),
                    )
                )
            )
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            # Mirrors ``JSONRPCDispatcher._write_error``. A write side that is
            # gone cannot be answered by any route, and failing the session
            # here would wake every in-flight call with the indistinguishable
            # "Connection closed" error this whole path exists to avoid; the
            # call timeout and reconnect own that recovery. Logged at WARNING
            # rather than DEBUG because the sender does stay unanswered.
            logger.warning(
                "Could not deliver oversize-request rejection for %r: write stream closed",
                request_id,
            )

    def __aiter__(self) -> BoundedReadStream[_T]:
        return self

    async def __anext__(self) -> _T:
        try:
            return await self.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def __aenter__(self) -> BoundedReadStream[_T]:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        await self.aclose()
        return None


def is_inbound_message_too_large_error(exc: BaseException) -> bool:
    """Return whether *exc* is the correlated local oversize outcome."""
    error = getattr(exc, "error", None)
    data = getattr(error, "data", None)
    return (
        getattr(error, "code", None) == INBOUND_MESSAGE_TOO_LARGE_CODE
        and isinstance(data, Mapping)
        and data.get(_INBOUND_MESSAGE_TOO_LARGE_MARKER) is True
    )


@asynccontextmanager
async def streamable_http_transport(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
) -> AsyncIterator[Any]:
    """Open a streamable-HTTP transport, owning the HTTP client's lifetime.

    Yields the SDK's ``TransportStreams`` (a tuple subclass, so ``streams[0]``
    / ``streams[1]`` indexing keeps working).
    """
    async with create_mcp_http_client(headers=headers, timeout=timeout) as http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            yield streams
