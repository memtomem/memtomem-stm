from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from types import SimpleNamespace

import anyio
import pytest

from mcp import ClientSession
from mcp import types as mcp_types
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage

from memtomem_stm.utils import json_size as json_size_module
from memtomem_stm.utils.json_size import json_utf8_size
from memtomem_stm.utils.mcp_transport import (
    INBOUND_MESSAGE_TOO_LARGE_CODE,
    _MAX_PENDING_REJECTIONS,
    BoundedReadStream,
    is_inbound_message_too_large_error,
)


class _OneItemStream:
    def __init__(self, item):
        self.item = item
        self.closed = False

    async def receive(self):
        if self.item is None:
            raise anyio.EndOfStream
        item, self.item = self.item, None
        return item

    async def aclose(self):
        self.closed = True


class _QueueStream:
    def __init__(self, *items):
        self.items = list(items)

    async def receive(self):
        if not self.items:
            raise anyio.EndOfStream
        return self.items.pop(0)

    async def aclose(self):
        pass


class _CollectingWriteStream:
    """Minimal ``WriteStream``-shaped double: records what was sent.

    ``attempted`` fires on every send, delivered or raised, so a test can wait
    for the spawned rejection instead of guessing at scheduler turns.
    """

    def __init__(self, raises: BaseException | None = None):
        self.sent = []
        self.raises = raises
        self.attempted = asyncio.Event()

    async def send(self, item):
        self.attempted.set()
        if self.raises is not None:
            raise self.raises
        self.sent.append(item)

    async def aclose(self):
        pass


class _BlockingWriteStream:
    """A write side whose ``send`` never completes — a stalled writer task.

    Client transports create both directions with a buffer size of 0, so a
    send is a rendezvous. This is what one looks like when nobody is there to
    receive it.
    """

    def __init__(self):
        self.entered = asyncio.Event()
        self.calls = 0

    async def send(self, item):
        self.calls += 1
        self.entered.set()
        await asyncio.Event().wait()

    async def aclose(self):
        pass


def test_json_utf8_size_exact_boundary_without_materializing_envelope():
    value = {"payload": "한글\nvalue"}
    exact = len('{"payload":"한글\\nvalue"}'.encode())
    assert json_utf8_size(value, limit=exact) == exact
    assert json_utf8_size(value, limit=exact - 1) == exact


def test_json_utf8_size_is_exact_across_native_chunks():
    value = {"payload": "a" * 70_000 + '"\n한글\x00tail'}
    exact = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
    assert json_utf8_size(value, limit=exact) == exact


async def test_json_utf8_size_async_runs_accounting_off_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    worker_threads: list[int] = []

    def probe(value, *, limit):
        worker_threads.append(threading.get_ident())
        return 7

    monkeypatch.setattr(json_size_module, "json_utf8_size", probe)

    assert await json_size_module.json_utf8_size_async({"payload": "x"}, limit=100) == 7
    assert worker_threads and worker_threads[0] != loop_thread


async def test_bounded_read_stream_accepts_message_at_cap():
    message = SimpleNamespace(payload="abc")
    item = SimpleNamespace(message=message)
    exact = json_utf8_size(message, limit=10_000)
    stream = BoundedReadStream(_OneItemStream(item), exact)
    assert await stream.receive() is item


def _oversize_response(request_id: int = 1):
    return SessionMessage(
        mcp_types.JSONRPCResponse(
            jsonrpc="2.0",
            id=request_id,
            result={"content": [{"type": "text", "text": "x" * 1_000}], "isError": False},
        )
    )


async def test_bounded_read_stream_replaces_oversize_response_with_correlated_error():
    stream = BoundedReadStream(_OneItemStream(_oversize_response(7)), 20)
    received = await stream.receive()
    assert received.message.id == 7
    assert received.message.error.code == INBOUND_MESSAGE_TOO_LARGE_CODE
    assert "max_upstream_bytes=20" in received.message.error.message


async def test_bounded_read_stream_drops_oversize_uncorrelated_message(caplog):
    """An oversize notification has no waiter to fail. Dropping it keeps the
    dispatcher's read loop alive; raising would tear down every in-flight call
    with an indistinguishable "Connection closed"."""
    oversize = SessionMessage(
        mcp_types.JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/message",
            params={"data": "x" * 1_000},
        )
    )
    keeper = SimpleNamespace(message=SimpleNamespace(payload="ok"))
    stream = BoundedReadStream(_QueueStream(oversize, keeper), 200)

    with caplog.at_level("WARNING"):
        assert await stream.receive() is keeper

    assert "max_upstream_bytes=200" in caplog.text


async def test_bounded_read_stream_reads_live_limit_provider_on_each_message():
    current_limit = 20
    stream = BoundedReadStream(
        _QueueStream(_oversize_response(1), _oversize_response(2)),
        lambda: current_limit,
    )

    first = await stream.receive()
    assert first.message.error.code == INBOUND_MESSAGE_TOO_LARGE_CODE

    current_limit = 100_000
    second = await stream.receive()
    assert isinstance(second.message, mcp_types.JSONRPCResponse)
    assert second.message.id == 2


async def test_oversize_response_survives_real_dispatcher_with_request_id():
    inbound_send, inbound_receive = anyio.create_memory_object_stream(1)
    outbound_send, outbound_receive = anyio.create_memory_object_stream(1)
    bounded = BoundedReadStream(inbound_receive, 200)

    async with inbound_send, outbound_receive, ClientSession(bounded, outbound_send) as session:
        pending = asyncio.create_task(session.call_tool("read_only", {}))
        outbound = await outbound_receive.receive()
        request = outbound.message
        assert isinstance(request, mcp_types.JSONRPCRequest)

        await inbound_send.send(
            SessionMessage(
                mcp_types.JSONRPCResponse(
                    jsonrpc="2.0",
                    id=request.id,
                    result={
                        "content": [{"type": "text", "text": "x" * 1_000}],
                        "isError": False,
                    },
                )
            )
        )

        with pytest.raises(MCPError) as raised:
            await pending

        assert raised.value.error.code == INBOUND_MESSAGE_TOO_LARGE_CODE
        assert is_inbound_message_too_large_error(raised.value)
        assert "max_upstream_bytes=200" in str(raised.value)


def _oversize_request(request_id: int = 9):
    return SessionMessage(
        mcp_types.JSONRPCRequest(
            jsonrpc="2.0",
            id=request_id,
            method="sampling/createMessage",
            params={
                "messages": [{"role": "user", "content": {"type": "text", "text": "x" * 1_000}}]
            },
        )
    )


async def test_bounded_read_stream_answers_oversize_server_request_with_same_id_error(caplog):
    """#960: a server->client request is correlated the other way round — its
    sender blocks on our answer, so dropping it strands the upstream handler."""
    keeper = SimpleNamespace(message=SimpleNamespace(payload="ok"))
    write_stream = _CollectingWriteStream()
    stream = BoundedReadStream(
        _QueueStream(_oversize_request(9), keeper), 200, write_stream=write_stream
    )

    with caplog.at_level("WARNING"):
        assert await stream.receive() is keeper
        # The reply is sent from its own task; wait for it rather than
        # assuming a scheduler turn happened.
        await asyncio.wait_for(write_stream.attempted.wait(), 5)

    assert len(write_stream.sent) == 1
    reply = write_stream.sent[0].message
    assert isinstance(reply, mcp_types.JSONRPCError)
    assert reply.id == 9
    # A spec code, not the private response-correlation marker: this frame
    # travels to the upstream server.
    assert reply.error.code == mcp_types.INVALID_REQUEST
    assert reply.error.code != INBOUND_MESSAGE_TOO_LARGE_CODE
    assert reply.error.data is None
    assert "max_upstream_bytes=200" in reply.error.message
    assert "Dropping" not in caplog.text
    assert "Rejecting" in caplog.text


async def test_bounded_read_stream_drops_oversize_notification_even_with_write_stream(caplog):
    """Positive control for the drop branch: nothing waits on a notification,
    so it is still dropped and nothing is written back."""
    oversize = SessionMessage(
        mcp_types.JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/message",
            params={"data": "x" * 1_000},
        )
    )
    keeper = SimpleNamespace(message=SimpleNamespace(payload="ok"))
    write_stream = _CollectingWriteStream()
    stream = BoundedReadStream(_QueueStream(oversize, keeper), 200, write_stream=write_stream)

    with caplog.at_level("WARNING"):
        assert await stream.receive() is keeper

    assert write_stream.sent == []
    assert "Dropping inbound MCP JSONRPCNotification" in caplog.text


async def test_bounded_read_stream_without_write_stream_still_drops_oversize_request(caplog):
    """No write side, no answer to give: the legacy constructor keeps dropping."""
    keeper = SimpleNamespace(message=SimpleNamespace(payload="ok"))
    stream = BoundedReadStream(_QueueStream(_oversize_request(3), keeper), 200)

    with caplog.at_level("WARNING"):
        assert await stream.receive() is keeper

    assert "Dropping inbound MCP JSONRPCRequest" in caplog.text


async def test_oversize_server_request_reply_survives_closed_write_stream():
    """A teardown race on the write side must not escape the read loop."""
    keeper = SimpleNamespace(message=SimpleNamespace(payload="ok"))
    write_stream = _CollectingWriteStream(raises=anyio.ClosedResourceError())
    stream = BoundedReadStream(
        _QueueStream(_oversize_request(4), keeper), 200, write_stream=write_stream
    )

    assert await stream.receive() is keeper
    await asyncio.wait_for(write_stream.attempted.wait(), 5)
    # The failure is contained in the rejection task, which must finish rather
    # than leave a never-retrieved exception behind.
    for task in list(stream._pending_rejections):
        await asyncio.wait_for(task, 5)
    assert stream._pending_rejections == set()


async def test_oversize_server_request_is_answered_through_real_dispatcher():
    inbound_send, inbound_receive = anyio.create_memory_object_stream(1)
    outbound_send, outbound_receive = anyio.create_memory_object_stream(1)
    bounded = BoundedReadStream(inbound_receive, 200, write_stream=outbound_send)

    async with inbound_send, outbound_receive, ClientSession(bounded, outbound_send):
        await inbound_send.send(_oversize_request(11))
        # Bounded so a regression fails red instead of hanging the suite: with
        # the request dropped, nothing is ever written back.
        outbound = await asyncio.wait_for(outbound_receive.receive(), 5)

    reply = outbound.message
    assert isinstance(reply, mcp_types.JSONRPCError)
    assert reply.id == 11
    assert reply.error.code == mcp_types.INVALID_REQUEST
    assert "max_upstream_bytes=200" in reply.error.message


async def test_oversize_request_rejection_never_blocks_the_read_loop():
    """#960 follow-up: the rejection must not be awaited from ``receive()``.

    A client transport's write stream has a buffer size of 0, so a send waits
    for the transport's writer task. Awaiting it inside the dispatcher's only
    read loop stalls every inbound message behind it — and for stdio that is a
    deadlock, because an upstream blocked writing to a stdout pipe we stopped
    draining never reads its stdin either.
    """
    keeper = SimpleNamespace(message=SimpleNamespace(payload="ok"))
    write_stream = _BlockingWriteStream()
    stream = BoundedReadStream(
        _QueueStream(_oversize_request(5), keeper), 200, write_stream=write_stream
    )

    # Returns the next message while the send is still parked.
    assert await asyncio.wait_for(stream.receive(), 5) is keeper
    await asyncio.wait_for(write_stream.entered.wait(), 5)
    assert len(stream._pending_rejections) == 1

    for task in list(stream._pending_rejections):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_pending_oversize_rejections_are_capped(caplog):
    """A peer flooding oversize requests behind a stalled writer must not grow
    an unbounded task set."""
    write_stream = _BlockingWriteStream()
    keeper = SimpleNamespace(message=SimpleNamespace(payload="ok"))
    flood = [_oversize_request(i) for i in range(_MAX_PENDING_REJECTIONS + 5)]
    stream = BoundedReadStream(_QueueStream(*flood, keeper), 200, write_stream=write_stream)

    with caplog.at_level("WARNING"):
        assert await asyncio.wait_for(stream.receive(), 5) is keeper

    assert len(stream._pending_rejections) == _MAX_PENDING_REJECTIONS
    assert write_stream.calls == _MAX_PENDING_REJECTIONS
    assert "already awaiting the write side" in caplog.text

    for task in list(stream._pending_rejections):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
