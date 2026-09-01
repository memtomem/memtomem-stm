from __future__ import annotations

import asyncio
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
