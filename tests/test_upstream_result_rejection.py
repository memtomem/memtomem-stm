"""An upstream reply the MCP SDK rejects is recorded as ``protocol``, not as STM's bug.

``ClientSession.call_tool`` validates the reply before STM sees it. A reply outside
the negotiated schema raises ``pydantic.ValidationError``; on a 2026-07-28
connection an ``InputRequiredResult`` raises ``RuntimeError``; a reply that breaks
the tool's own ``outputSchema`` raises ``RuntimeError`` too. Each exception here
is produced by the real SDK against a hand-written upstream, so the
classification is pinned to what the installed SDK raises, not to a stand-in.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from mcp.client.client import _synthesize_discover
from mcp.client.session import ClientSession
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCResponse
from pydantic import ValidationError

from memtomem_stm.proxy.config import CompressionStrategy, ProxyConfig, UpstreamServerConfig
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection, _safe_error_text
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.utils.circuit_breaker import CircuitBreaker

SENTINEL = "SENTINEL-1131-upstream-text"

_PLAIN_TOOLS = {"tools": [{"name": "t", "inputSchema": {"type": "object"}}]}
_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
_SCHEMA_TOOLS = {
    "tools": [{"name": "t", "inputSchema": {"type": "object"}, "outputSchema": _SCHEMA}]
}
_BAD_REF_TOOLS = {
    "tools": [
        {
            "name": "t",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object", "properties": {"n": {"$ref": "#/$defs/missing"}}},
        }
    ]
}
_TEXT = [{"type": "text", "text": SENTINEL}]


async def _sdk_error(
    reply: dict[str, Any],
    *,
    modern: bool = False,
    tools: dict[str, Any] = _PLAIN_TOOLS,
    arguments: Any = None,
) -> BaseException:
    """Return what the real ``ClientSession.call_tool`` raises for ``reply``."""
    c2s_send, c2s_recv = anyio.create_memory_object_stream[SessionMessage](16)
    s2c_send, s2c_recv = anyio.create_memory_object_stream[SessionMessage](16)

    async def upstream() -> None:
        async for msg in c2s_recv:
            method = getattr(msg.message, "method", None)
            if method == "initialize":
                result: dict[str, Any] = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "0"},
                }
            elif method == "tools/list":
                result = tools
            elif method == "tools/call":
                result = reply
            else:
                continue
            response = JSONRPCResponse(jsonrpc="2.0", id=msg.message.id, result=result)
            await s2c_send.send(SessionMessage(response))

    caught: BaseException | None = None
    async with anyio.create_task_group() as tg:
        tg.start_soon(upstream)
        async with ClientSession(s2c_recv, c2s_send) as session:
            if modern:
                session.adopt(_synthesize_discover("2026-07-28"))
            else:
                await session.initialize()
            with anyio.fail_after(10):
                try:
                    await session.call_tool("t", {} if arguments is None else arguments)
                except Exception as exc:  # noqa: BLE001 — the exception is the result
                    caught = exc
        tg.cancel_scope.cancel()
    assert caught is not None, "the SDK accepted the reply; the fixture is wrong"
    return caught


def _make_manager(tmp_path: Path) -> ProxyManager:
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=CompressionStrategy.NONE,
        max_result_chars=50000,
        max_retries=2,
        reconnect_delay_seconds=0.0,
        max_reconnect_delay_seconds=0.0,
        circuit_max_failures=3,
    )
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": server_cfg},
    )
    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    mgr = ProxyManager(proxy_cfg, TokenTracker(metrics_store=store))
    read_only = SimpleNamespace(read_only_hint=True, destructive_hint=False)
    mgr._connections["srv"] = UpstreamConnection(
        name="srv",
        config=server_cfg,
        session=AsyncMock(),
        tools=[SimpleNamespace(name="tool", annotations=read_only)],
        breaker=CircuitBreaker(max_failures=3, reset_timeout=60.0, name="upstream-srv"),
    )
    mgr._test_store = store  # type: ignore[attr-defined]
    return mgr


def _error_rows(mgr: ProxyManager) -> list[tuple]:
    return mgr._test_store._db.execute(  # type: ignore[attr-defined]
        "SELECT is_error, error_category, error_code, error_message FROM proxy_metrics"
    ).fetchall()


async def _call_with(mgr: ProxyManager, exc: BaseException) -> AsyncMock:
    session = mgr._connections["srv"].session
    assert isinstance(session, AsyncMock)
    session.call_tool.side_effect = exc
    with (
        patch.object(mgr, "_reconnect_server", new_callable=AsyncMock) as reconnect,
        patch.object(mgr, "_schedule_reconnect_for_next_call") as schedule,
    ):
        with pytest.raises(type(exc)):
            await mgr.call_tool("srv", "tool", {})
    assert reconnect.await_count == 0
    assert schedule.call_count == 0
    return session


_REJECTED = {
    "legacy_list_structured_content": lambda: _sdk_error(
        {"content": _TEXT, "structuredContent": [SENTINEL]}
    ),
    "legacy_input_required": lambda: _sdk_error(
        {"resultType": "input_required", "inputRequests": {}, "requestState": SENTINEL}
    ),
    "legacy_is_error_null": lambda: _sdk_error({"content": [], "isError": None}),
    # A 2026-07-28 result must carry ``resultType``. (A list ``structuredContent``
    # is valid there, so it is not a rejection on a modern connection.)
    "modern_result_type_missing": lambda: _sdk_error(
        {"content": _TEXT, "structuredContent": [SENTINEL]}, modern=True
    ),
    "modern_content_not_a_list": lambda: _sdk_error(
        {"content": SENTINEL, "resultType": "complete"}, modern=True
    ),
    "modern_input_required": lambda: _sdk_error(
        {"resultType": "input_required", "inputRequests": {}, "requestState": SENTINEL},
        modern=True,
    ),
    "output_schema_violated": lambda: _sdk_error(
        {"content": _TEXT, "structuredContent": {"n": SENTINEL}}, tools=_SCHEMA_TOOLS
    ),
    "output_schema_structured_content_missing": lambda: _sdk_error(
        {"content": _TEXT}, tools=_SCHEMA_TOOLS
    ),
    "output_schema_unresolvable_ref": lambda: _sdk_error(
        {"content": _TEXT, "structuredContent": {"n": 1}}, tools=_BAD_REF_TOOLS
    ),
}


_SENTINEL_IN_SDK_MESSAGE = {
    "legacy_list_structured_content",
    "modern_content_not_a_list",
    "output_schema_violated",
}


@pytest.mark.parametrize("case", list(_REJECTED))
async def test_rejected_upstream_reply_is_recorded_as_protocol(case: str, tmp_path: Path) -> None:
    exc = await _REJECTED[case]()
    mgr = _make_manager(tmp_path)
    # The tool passes STM's real replay gate, so "called once" below means the
    # rejection was not retried, not that the tool was never replay-eligible.
    assert mgr._tool_idempotent_for_retry("srv", "tool", cfg_snap=mgr._config)
    breaker = mgr._connections["srv"].breaker
    assert breaker is not None
    breaker.record_failure()
    assert breaker.failure_count == 1

    session = await _call_with(mgr, exc)

    assert session.call_tool.await_count == 1
    assert _error_rows(mgr) == [(1, "protocol", None, _safe_error_text(exc))]
    # Where the SDK's own message quotes the upstream's text, the row must not.
    assert (SENTINEL in str(exc)) == (case in _SENTINEL_IN_SDK_MESSAGE)
    assert SENTINEL not in _error_rows(mgr)[0][3]
    # A reply arrived: the round trip completed, so the breaker's earlier
    # failure is no longer "consecutive".
    assert breaker.failure_count == 0


class _StmRuntimeError(RuntimeError):
    pass


_NOT_REJECTED = {
    # Nothing was sent: the SDK refused to build the request.
    "request_params_invalid": lambda: _sdk_error({"content": _TEXT}, arguments={1: "x"}),
    # The tools/call reply was valid; the SDK's own tools/list refresh failed.
    # Charging that to the invoked tool would be wrong, so it keeps today's category.
    "tools_list_refresh_invalid": lambda: _sdk_error(
        {"content": _TEXT}, tools={"tools": [{"name": 5}]}
    ),
}


_NOT_REJECTED_TITLES = {
    "request_params_invalid": "CallToolRequestParams",
    "tools_list_refresh_invalid": "ListToolsResult",
}


async def _plain(exc: BaseException) -> BaseException:
    return exc


_NOT_REJECTED.update(
    {
        "type_error": lambda: _plain(TypeError("bad")),
        "bare_runtime_error": lambda: _plain(RuntimeError("boom")),
        "runtime_error_subclass_with_sdk_text": lambda: _plain(
            _StmRuntimeError("Server returned InputRequiredResult; pass allow_input_required")
        ),
    }
)


@pytest.mark.parametrize("case", list(_NOT_REJECTED))
async def test_other_failures_stay_programming(case: str, tmp_path: Path) -> None:
    exc = await _NOT_REJECTED[case]()
    # The two SDK fixtures must fail where they claim to, or this test would
    # pass on any error the predicate happens not to match.
    expected_title = _NOT_REJECTED_TITLES.get(case)
    if expected_title is not None:
        assert isinstance(exc, ValidationError)
        assert exc.title == expected_title
    mgr = _make_manager(tmp_path)

    session = mgr._connections["srv"].session
    session.call_tool.side_effect = exc
    with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
        with pytest.raises(type(exc)):
            await mgr.call_tool("srv", "tool", {})

    assert session.call_tool.await_count == 1
    assert [row[1] for row in _error_rows(mgr)] == ["programming"]
