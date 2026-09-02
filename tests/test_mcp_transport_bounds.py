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
from memtomem_stm.utils.json_size import _SYNC_MEASURE_BYTES, json_utf8_size
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
        self.reached = asyncio.Event()
        self.expected = 1

    async def send(self, item):
        self.calls += 1
        self.entered.set()
        if self.calls >= self.expected:
            self.reached.set()
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


def _record_measuring_threads(monkeypatch) -> list[str]:
    """Record the thread each accounting pass runs on, keeping real sizes."""
    seen: list[str] = []
    real = json_size_module.json_utf8_size

    def probe(value, *, limit):
        seen.append(threading.current_thread().name)
        return real(value, limit=limit)

    monkeypatch.setattr(json_size_module, "json_utf8_size", probe)
    return seen


def _forbid_executor(monkeypatch) -> None:
    """Make any hop to the sizer's worker a loud failure."""

    def boom():
        raise AssertionError("measurement hopped to a thread")

    monkeypatch.setattr(json_size_module, "_size_executor", boom)


async def test_small_payload_is_measured_once_on_the_loop_thread(monkeypatch):
    """#956: the read loop calls this on every inbound message. A small one must
    not pay a scheduling round trip through any executor."""
    loop_thread = threading.current_thread().name
    seen = _record_measuring_threads(monkeypatch)
    _forbid_executor(monkeypatch)

    value = {"payload": "x" * 100}
    exact = json_utf8_size(value, limit=10_000)
    assert await json_size_module.json_utf8_size_async(value, limit=41_943_040) == exact
    assert seen == [loop_thread]


async def test_large_payload_is_measured_on_the_dedicated_executor(monkeypatch):
    """Over the sync budget the real measurement moves off the loop — and onto
    the sizer's own worker, not the shared default pool."""
    loop_thread = threading.current_thread().name
    seen = _record_measuring_threads(monkeypatch)

    value = {"payload": "x" * (2 * _SYNC_MEASURE_BYTES)}
    exact = json_utf8_size(value, limit=10**9)
    assert await json_size_module.json_utf8_size_async(value, limit=41_943_040) == exact

    # One authoritative measurement, and it happened off the loop. Routing is
    # the estimate's job and never calls the real sizer.
    assert len(seen) == 1
    assert seen[0] != loop_thread
    assert seen[0].startswith(json_size_module._EXECUTOR_THREAD_PREFIX)


async def test_large_payload_does_not_use_the_shared_default_executor(monkeypatch):
    """The failure scenario in #956 is a saturated default pool. Measurement
    must not depend on it at all."""

    async def forbidden(*args, **kwargs):
        raise AssertionError("measurement used asyncio.to_thread")

    monkeypatch.setattr(json_size_module.asyncio, "to_thread", forbidden)

    value = {"payload": "x" * (2 * _SYNC_MEASURE_BYTES)}
    exact = json_utf8_size(value, limit=10**9)
    assert await json_size_module.json_utf8_size_async(value, limit=41_943_040) == exact


async def test_an_over_cap_payload_is_measured_once(monkeypatch):
    """The over-cap sentinel is settled by one bounded pass, whichever thread
    runs it — ``json_utf8_size`` stops at the limit either way."""
    seen = _record_measuring_threads(monkeypatch)

    assert await json_size_module.json_utf8_size_async({"payload": "x" * 1_000}, limit=20) == 21
    assert len(seen) == 1


async def test_a_tiny_payload_under_a_tiny_cap_stays_on_the_loop(monkeypatch):
    """A small limit must not force every message off-loop: a payload that fits
    is still measured here."""
    loop_thread = threading.current_thread().name
    seen = _record_measuring_threads(monkeypatch)
    _forbid_executor(monkeypatch)

    assert await json_size_module.json_utf8_size_async({"a": "b"}, limit=200) == 9
    assert seen == [loop_thread]


async def test_bounded_read_stream_small_message_never_leaves_the_loop(monkeypatch):
    """The read-loop caller inherits the on-loop path for a small message."""
    _forbid_executor(monkeypatch)
    item = SimpleNamespace(message=SimpleNamespace(payload="abc"))
    stream = BoundedReadStream(_OneItemStream(item), 41_943_040)
    assert await stream.receive() is item


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
    # ``add_done_callback`` schedules the discard rather than running it inline,
    # so give the loop the turn that retires the entry.
    await asyncio.sleep(0)
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
    write_stream.expected = _MAX_PENDING_REJECTIONS
    keeper = SimpleNamespace(message=SimpleNamespace(payload="ok"))
    flood = [_oversize_request(i) for i in range(_MAX_PENDING_REJECTIONS + 5)]
    stream = BoundedReadStream(_QueueStream(*flood, keeper), 200, write_stream=write_stream)

    with caplog.at_level("WARNING"):
        assert await asyncio.wait_for(stream.receive(), 5) is keeper

    assert len(stream._pending_rejections) == _MAX_PENDING_REJECTIONS
    # The rejections run in their own tasks; wait for them rather than assuming
    # the read loop handed the scheduler a turn on the way past.
    await asyncio.wait_for(write_stream.reached.wait(), 5)
    assert write_stream.calls == _MAX_PENDING_REJECTIONS
    assert "already awaiting the write side" in caplog.text

    for task in list(stream._pending_rejections):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_large_model_is_never_dumped_on_the_calling_thread(monkeypatch):
    """#956, second half: routing must not itself materialize the payload.

    ``model_dump`` builds the whole subtree before one byte is counted, so
    measuring a large pydantic message on the loop thread would keep the stall
    this change exists to remove — the hop would just happen after the cost.
    """
    loop_thread = threading.current_thread().name
    dumped_on: list[str] = []
    real_dump = mcp_types.JSONRPCResponse.model_dump

    def spy(self, *args, **kwargs):
        dumped_on.append(threading.current_thread().name)
        return real_dump(self, *args, **kwargs)

    monkeypatch.setattr(mcp_types.JSONRPCResponse, "model_dump", spy)

    big = mcp_types.JSONRPCResponse(
        jsonrpc="2.0",
        id=1,
        result={"content": [{"type": "text", "text": "y" * 400} for _ in range(400)]},
    )
    exact = json_utf8_size(big, limit=10**9)
    assert exact > _SYNC_MEASURE_BYTES
    dumped_on.clear()  # drop this test's own setup measurement

    assert await json_size_module.json_utf8_size_async(big, limit=41_943_040) == exact
    assert dumped_on and loop_thread not in dumped_on


async def test_a_small_model_is_measured_exactly_without_a_hop(monkeypatch):
    """The routing estimate never becomes the answer: a small model still gets
    the same byte count the synchronous helper produces."""
    _forbid_executor(monkeypatch)
    small = mcp_types.JSONRPCResponse(
        jsonrpc="2.0", id=1, result={"content": [{"type": "text", "text": "x" * 300}]}
    )
    exact = json_utf8_size(small, limit=10**9)
    assert await json_size_module.json_utf8_size_async(small, limit=41_943_040) == exact


async def test_a_failed_estimate_falls_back_to_the_worker(monkeypatch):
    """Routing must never fail the caller. The estimate skips a model's own
    serializers by design, so a value it cannot walk is treated as large and
    measured off-loop by the authoritative sizer instead of raising."""
    real_value = json_size_module._Sizer.value

    def hostile(self, value):
        if self.approximate:
            raise RuntimeError("this value cannot be walked")
        return real_value(self, value)

    monkeypatch.setattr(json_size_module._Sizer, "value", hostile)

    message = mcp_types.JSONRPCResponse(
        jsonrpc="2.0", id=1, result={"content": [{"type": "text", "text": "x"}]}
    )
    seen = _record_measuring_threads(monkeypatch)
    size = await json_size_module.json_utf8_size_async(message, limit=41_943_040)

    assert size > 0
    assert seen and seen[0].startswith(json_size_module._EXECUTOR_THREAD_PREFIX)


class TestRoutingEstimateStaysConservative:
    """The estimate walks a model's declared fields, so a model that serializes
    as something else could be judged small and then dumped on the event loop —
    the exact stall #956 removes. Such a model must route off-loop instead."""

    @staticmethod
    def _models():
        from pydantic import BaseModel, computed_field, field_serializer

        class Computed(BaseModel):
            seed: str = "s"

            @computed_field  # type: ignore[prop-decorator]
            @property
            def payload(self) -> str:
                return "z" * 200_000

        class CustomSerializer(BaseModel):
            seed: str = "s"

            @field_serializer("seed")
            def _expand(self, value: str) -> str:
                return "z" * 200_000

        class WithExtras(BaseModel, extra="allow"):
            seed: str = "s"

        return {
            "computed field": Computed(),
            "custom serializer": CustomSerializer(),
            "extras": WithExtras(seed="s", blob="z" * 200_000),
        }

    @pytest.mark.parametrize("label", sorted(_models.__func__()))
    def test_a_model_that_outgrows_its_fields_is_estimated_over_the_limit(self, label):
        model = self._models()[label]
        assert json_utf8_size(model, limit=10**9) > _SYNC_MEASURE_BYTES
        assert json_size_module._approx_json_size(model, limit=_SYNC_MEASURE_BYTES) > (
            _SYNC_MEASURE_BYTES
        ), f"{label}: estimated small, so the loop thread would dump it"

    @pytest.mark.parametrize("label", sorted(_models.__func__()))
    async def test_such_a_model_is_never_dumped_on_the_calling_thread(self, label, monkeypatch):
        from pydantic import BaseModel

        loop_thread = threading.current_thread().name
        dumped_on: list[str] = []
        real_dump = BaseModel.model_dump

        def spy(self, *args, **kwargs):
            dumped_on.append(threading.current_thread().name)
            return real_dump(self, *args, **kwargs)

        monkeypatch.setattr(BaseModel, "model_dump", spy)

        model = self._models()[label]
        dumped_on.clear()
        size = await json_size_module.json_utf8_size_async(model, limit=41_943_040)

        assert size > _SYNC_MEASURE_BYTES
        assert loop_thread not in dumped_on

    def test_a_model_outside_the_vouched_packages_is_never_walked(self):
        """The estimate cannot see every way a model serializes — an
        ``Annotated`` functional serializer is not in the decorator registry at
        all — so an unfamiliar model routes off-loop on principle."""
        from typing import Annotated

        from pydantic import BaseModel, PlainSerializer

        class Stranger(BaseModel):
            seed: Annotated[str, PlainSerializer(lambda v: "z" * 200_000, return_type=str)] = "x"

        model = Stranger()
        assert json_utf8_size(model, limit=10**9) > _SYNC_MEASURE_BYTES
        assert json_size_module._approx_json_size(model, limit=_SYNC_MEASURE_BYTES) > (
            _SYNC_MEASURE_BYTES
        )
        assert json_size_module._model_walk_is_faithful(Stranger) is False

    def test_the_mcp_wire_models_stay_on_the_fast_path(self):
        """The allowlist has to still admit what the proxy actually measures."""
        for cls in (
            mcp_types.JSONRPCResponse,
            mcp_types.JSONRPCRequest,
            mcp_types.JSONRPCNotification,
            mcp_types.CallToolResult,
        ):
            assert json_size_module._model_walk_is_faithful(cls) is True, cls

    async def test_a_context_dependent_size_agrees_across_both_paths(self):
        """``run_in_executor`` carries no context of its own, while the
        ``asyncio.to_thread`` it replaced did. The two measurement paths must
        stay interchangeable."""
        import contextvars

        from pydantic import BaseModel, field_serializer

        width = contextvars.ContextVar("width", default=1)

        class Contextual(BaseModel):
            seed: str = "x"

            @field_serializer("seed")
            def _expand(self, value: str) -> str:
                return "z" * width.get()

        token = width.set(200_000)
        try:
            model = Contextual()
            on_loop = json_utf8_size(model, limit=10**9)
            off_loop = await json_size_module.json_utf8_size_async(model, limit=41_943_040)
            assert on_loop > _SYNC_MEASURE_BYTES
            assert off_loop == on_loop
        finally:
            width.reset(token)

    def test_a_plain_model_is_still_estimated_close_to_its_real_size(self):
        """Conservatism must not collapse into "always hop": an ordinary MCP
        message has to keep the on-loop path."""
        message = mcp_types.JSONRPCResponse(
            jsonrpc="2.0", id=1, result={"content": [{"type": "text", "text": "x" * 500}]}
        )
        exact = json_utf8_size(message, limit=10**9)
        approx = json_size_module._approx_json_size(message, limit=_SYNC_MEASURE_BYTES)
        assert approx <= _SYNC_MEASURE_BYTES
        assert abs(approx - exact) < 64, (approx, exact)
