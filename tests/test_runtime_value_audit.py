"""Runtime error surfaces never carry credential-bearing configuration values (#1082).

The runtime twin of ``tests/cli/test_health_doctor_value_audit.py``. Every
credential leaf of an upstream entry — URL userinfo, query and fragment, a
header value, an env value, an ``args`` item — gets a distinct canary; an
exception that echoes them (whole, in part, or the way httpx quotes the request
URL) is raised on the real path to each surface, and the surface is scanned.

Surfaces, by audience:

* the ``ToolError`` an MCP client and its model receive (``server.py``
  ``_make_proxy_handler`` → ``ProxyManager.safe_upstream_error``);
* ``proxy_metrics.error_message`` rows (persisted), including the fixed
  summary an upstream ``isError`` result stores (#1084);
* the startup-failure record that ``stm_proxy_health`` shows the client;
* the ``extract_error`` / ``index_error`` columns (persisted).

The one deliberate exception is the ``isError`` tool result itself, which
reaches the client as the upstream wrote it, within the shared byte limit and
the lone-surrogate scrub (#1084); its test pins that too.

Each case asserts a positive control proving the path ran, so a pass means a
clean surface and not an unreached branch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import httpx2
import pytest
from pydantic import TypeAdapter, ValidationError

from memtomem_stm.proxy.config import (
    AutoIndexConfig,
    CacheConfig,
    CompressionStrategy,
    ExtractionConfig,
    ProxyConfig,
    TransportType,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.memory_ops import auto_index_response, extract_and_store
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore

_URL = "http://cnryUrlUser:cnryUrlPass@127.0.0.1:9/mcp?token=cnryQueryTok#cnryFrag"
_HEADER = "Bearer cnryHdrVal"
_ENV = "cnryEnvVal"
_ARG = "--api-key=cnryArgVal"

_CANARIES = (
    "cnryUrlUser",
    "cnryUrlPass",
    "cnryQueryTok",
    "cnryFrag",
    "cnryHdrVal",
    "cnryEnvVal",
    "cnryArgVal",
)


def _server_cfg() -> UpstreamServerConfig:
    return UpstreamServerConfig(
        prefix="test",
        transport=TransportType.STREAMABLE_HTTP,
        url=_URL,
        headers={"Authorization": _HEADER},
        env={"TOKEN": _ENV},
        args=[_ARG],
        compression=CompressionStrategy.NONE,
        max_retries=0,
        reconnect_delay_seconds=0.0,
        max_reconnect_delay_seconds=0.0,
    )


def _http_status_error(module: Any) -> BaseException:
    request = module.Request("GET", _URL)
    response = module.Response(401, request=request)
    try:
        response.raise_for_status()
    except module.HTTPStatusError as exc:
        return exc
    raise AssertionError("raise_for_status did not raise")


def _validation_error() -> ValidationError:
    try:
        TypeAdapter(int).validate_python(f"{_ENV} {_HEADER}")
    except ValidationError as exc:
        return exc
    raise AssertionError("validation did not fail")


def _echo(kind: str) -> BaseException:
    """An exception whose text quotes configured values the way *kind* does."""
    if kind == "whole":
        return RuntimeError(f"connect {_URL} failed; sent {_HEADER}, env {_ENV}, argv {_ARG}")
    if kind == "partial":
        # Servers quote one token of a header or the value half of an
        # ``--opt=value`` argument; neither is a whole configured value.
        return RuntimeError("rejected token cnryHdrVal for option value cnryArgVal")
    if kind == "httpx":
        return _http_status_error(httpx)
    if kind == "httpx2":
        return _http_status_error(httpx2)
    if kind == "validation":
        return _validation_error()
    if kind == "jsonrpc":
        from mcp.shared.exceptions import MCPError

        # An upstream JSON-RPC error quoting the request back.
        return MCPError(-32602, f"invalid params for {_URL}: {_ARG}, auth {_HEADER}")
    if kind == "group":
        return ExceptionGroup("unhandled errors in a TaskGroup", [_http_status_error(httpx2)])
    raise AssertionError(kind)


_KINDS = ["whole", "partial", "httpx", "httpx2", "validation", "jsonrpc", "group"]

# What each surface may say instead: the type name, the HTTP status, or the
# ValidationError's locations and error types.
_EXPECTED = {
    "whole": "RuntimeError",
    "partial": "RuntimeError",
    "httpx": "HTTP 401 (HTTPStatusError)",
    "httpx2": "HTTP 401 (HTTPStatusError)",
    "validation": "ValidationError: int_parsing",
    "jsonrpc": "MCPError -32602 (Invalid params)",
    "group": "HTTP 401 (HTTPStatusError)",
}


def _assert_clean(text: str) -> None:
    leaked = [c for c in _CANARIES if c in text]
    assert leaked == [], f"canaries in output: {leaked}\n{text}"


def _assert_vocabulary(kind: str, text: str) -> None:
    assert _EXPECTED[kind] in text, text


def _manager(tmp_path: Path, *, store: MetricsStore | None = None) -> ProxyManager:
    cfg = _server_cfg()
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": cfg},
        cache=CacheConfig(tool_annotation_policy="conservative"),
    )
    mgr = ProxyManager(proxy_cfg, TokenTracker(metrics_store=store))
    tool = SimpleNamespace(
        name="tool",
        description="d",
        inputSchema={"type": "object", "properties": {}},
        input_schema={"type": "object", "properties": {}},
        annotations=None,
    )
    mgr._connections["srv"] = UpstreamConnection(
        name="srv", config=cfg, session=AsyncMock(), tools=[tool]
    )
    return mgr


# ── ToolError + proxy_metrics rows (one call, both surfaces) ─────────────


@pytest.mark.parametrize("kind", _KINDS)
async def test_tool_error_and_metrics_row_carry_no_config_values(tmp_path, kind):
    from mcp import ClientSession
    from mcp.client._memory import InMemoryTransport
    from mcp.server.mcpserver import MCPServer

    from memtomem_stm.proxy._fastmcp_compat import register_proxy_tool
    from memtomem_stm.server import _make_proxy_handler

    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    mgr = _manager(tmp_path, store=store)
    session = mgr._connections["srv"].session
    session.call_tool.side_effect = _echo(kind)

    server = MCPServer("value-audit")
    (info,) = mgr.get_proxy_tools()
    register_proxy_tool(server, _make_proxy_handler(mgr, info.server, info.original_name), info)

    with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
        async with InMemoryTransport(server) as streams:
            async with ClientSession(streams[0], streams[1]) as client:
                await client.initialize()
                result = await client.call_tool(info.prefixed_name, {})

    store.close()
    with sqlite3.connect(tmp_path / "metrics.db") as db:
        rows = [r[0] for r in db.execute("SELECT error_message FROM proxy_metrics")]
    tool_error = "\n".join(getattr(c, "text", "") for c in result.content)

    # Positive controls: the upstream was called, the failure reached the
    # client as a tool error, and exactly one error row was persisted.
    assert session.call_tool.await_count == 1
    assert result.is_error is True
    assert len(rows) == 1 and rows[0], rows
    surfaces = {"ToolError": tool_error, "error_message row": rows[0]}
    leaked = {name: [c for c in _CANARIES if c in text] for name, text in surfaces.items()}
    assert leaked == {name: [] for name in surfaces}, f"canaries in output: {leaked}\n{surfaces}"
    for text in surfaces.values():
        _assert_vocabulary(kind, text)


# ── startup failure → stm_proxy_health ────────────────────────────────────


@pytest.mark.parametrize("kind", _KINDS)
async def test_startup_failure_health_carries_no_config_values(tmp_path, kind):
    from memtomem_stm.config import STMConfig
    from memtomem_stm.server import stm_proxy_health

    mgr = _manager(tmp_path)
    mgr._connections.clear()

    with patch.object(mgr, "_connect_server", AsyncMock(side_effect=_echo(kind))):
        await mgr.start()
    try:
        record = mgr.get_upstream_health()["srv"]
        # Positive control: the failed connect was recorded for health.
        assert record["connected"] is False
        _assert_clean(record["error"])
        _assert_vocabulary(kind, record["error"])

        app = SimpleNamespace(
            proxy_manager=mgr,
            proxy_config_error=None,
            config=STMConfig(),
            surfacing_engine=None,
        )
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))
        rendered = await stm_proxy_health(ctx=ctx)
        assert f"startup connect failed: {_EXPECTED[kind]}" in rendered
        _assert_clean(rendered)
    finally:
        await mgr.stop()


# ── persisted stage-error columns ─────────────────────────────────────────


class _RaisingIndexer:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def index_file(self, path: Path, namespace: str | None = None) -> Any:
        raise self.exc


class _RaisingExtractor:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def extract(self, text: str, **_: Any) -> Any:
        raise self.exc


@pytest.mark.parametrize("kind", _KINDS)
async def test_auto_index_error_carries_no_config_values(tmp_path, kind):
    outcome = await auto_index_response(
        _RaisingIndexer(_echo(kind)),  # type: ignore[arg-type]
        AutoIndexConfig(enabled=True, memory_dir=tmp_path / "idx", namespace="p-{server}"),
        server="srv",
        tool="tool",
        arguments={},
        text="body",
        agent_summary="summary",
    )
    assert outcome.ok is False and outcome.error
    _assert_clean(outcome.error)
    _assert_vocabulary(kind, outcome.error)


@pytest.mark.parametrize("kind", _KINDS)
async def test_extract_error_carries_no_config_values(tmp_path, kind):
    outcome = await extract_and_store(
        None,
        _RaisingExtractor(_echo(kind)),  # type: ignore[arg-type]
        ExtractionConfig(enabled=True),
        server="srv",
        tool="tool",
        arguments={},
        text="body",
    )
    assert outcome.ok is False and outcome.error
    _assert_clean(outcome.error)
    _assert_vocabulary(kind, outcome.error)


@pytest.mark.parametrize("kind", _KINDS)
async def test_extractor_start_failure_carries_no_config_values(tmp_path, kind):
    mgr = _manager(tmp_path)
    with patch.object(mgr, "_get_extractor", AsyncMock(side_effect=_echo(kind))):
        outcome = await mgr._extract_and_store("srv", "tool", {}, "body", cfg_snap=mgr._config)
    assert outcome.ok is False and outcome.error
    _assert_clean(outcome.error)
    _assert_vocabulary(kind, outcome.error)


# ── the same columns, persisted by a full proxied call ───────────────────


def _stage_manager(tmp_path: Path, store: MetricsStore, indexer: Any) -> ProxyManager:
    cfg = _server_cfg()
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": cfg},
        auto_index=AutoIndexConfig(enabled=True, min_chars=10, memory_dir=tmp_path / "idx"),
        extraction=ExtractionConfig(enabled=True, background=False, min_response_chars=10),
    )
    mgr = ProxyManager(proxy_cfg, TokenTracker(metrics_store=store), index_engine=indexer)
    session = AsyncMock()
    session.call_tool.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="upstream content " * 100)],
        is_error=False,
    )
    mgr._connections["srv"] = UpstreamConnection(name="srv", config=cfg, session=session, tools=[])
    return mgr


@pytest.mark.parametrize("kind", _KINDS)
async def test_stage_error_columns_carry_no_config_values(tmp_path, kind):
    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    mgr = _stage_manager(tmp_path, store, _RaisingIndexer(_echo(kind)))
    try:
        with patch.object(mgr, "_get_extractor", AsyncMock(side_effect=_echo(kind))):
            await mgr.call_tool("srv", "tool", {})
    finally:
        await mgr.stop()
    store.close()
    with sqlite3.connect(tmp_path / "metrics.db") as db:
        rows = db.execute(
            "SELECT index_ok, index_error, extract_ok, extract_error FROM proxy_metrics"
        ).fetchall()
    # Positive control: one call, and both stages ran and failed.
    assert len(rows) == 1, rows
    index_ok, index_error, extract_ok, extract_error = rows[0]
    assert index_ok == 0 and extract_ok == 0, rows
    surfaces = {"index_error": index_error, "extract_error": extract_error}
    leaked = {name: [c for c in _CANARIES if c in text] for name, text in surfaces.items()}
    assert leaked == {name: [] for name in surfaces}, f"canaries in output: {leaked}"
    for text in surfaces.values():
        _assert_vocabulary(kind, text)


# ── upstream isError results: verbatim to the client, summary on disk ─────


async def test_upstream_is_error_reaches_the_client_and_not_the_db(tmp_path):
    """An upstream ``isError`` result is the tool-result channel: the model
    reads it to correct its call, and the upstream is trusted with what it
    echoes there, so its text, ``structuredContent``, ``_meta`` and non-text
    blocks reach the client as written (within the shared byte limit and
    lone-surrogate scrub, which this ASCII, in-limit result does not trip). The
    persisted row, which outlives the conversation, keeps only the length
    (#1084). The canaries stand in for a request the upstream quoted back."""
    from mcp import ClientSession
    from mcp import types as mcp_types
    from mcp.client._memory import InMemoryTransport
    from mcp.server.mcpserver import MCPServer

    from memtomem_stm.proxy._fastmcp_compat import register_proxy_tool
    from memtomem_stm.server import _make_proxy_handler

    echo = " ".join(_CANARIES)
    image = mcp_types.ImageContent(type="image", data="aW1n", mimeType="image/png")
    upstream = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=f"bad request: {echo}"), image],
        structured_content={"echo": echo},
        _meta={"echo": echo},
        is_error=True,
    )

    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    mgr = _manager(tmp_path, store=store)
    session = mgr._connections["srv"].session
    session.call_tool.return_value = upstream

    server = MCPServer("value-audit")
    (info,) = mgr.get_proxy_tools()
    register_proxy_tool(server, _make_proxy_handler(mgr, info.server, info.original_name), info)

    async with InMemoryTransport(server) as streams:
        async with ClientSession(streams[0], streams[1]) as client:
            await client.initialize()
            result = await client.call_tool(info.prefixed_name, {})

    store.close()
    with sqlite3.connect(tmp_path / "metrics.db") as db:
        rows = db.execute("SELECT error_category, error_message FROM proxy_metrics").fetchall()

    assert session.call_tool.await_count == 1
    assert result.is_error is True
    assert result.content[0].text == f"bad request: {echo}"
    assert result.content[1].data == image.data
    assert result.structured_content == {"echo": echo}
    assert result.meta == {"echo": echo}
    text_chars = len(f"bad request: {echo}")
    assert rows == [("upstream_error", f"upstream isError ({text_chars} chars)")]


# ── what the vocabulary deliberately keeps ────────────────────────────────


def _mcp_error(code: int, message: str) -> Any:
    from mcp.shared.exceptions import MCPError

    return MCPError(code=code, message=message)


@pytest.mark.parametrize("grouped", [False, True], ids=["bare", "grouped"])
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (-32602, "MCPError -32602 (Invalid params)"),
        (-32601, "MCPError -32601 (Method not found)"),
        # Not a reserved code: the upstream chose it, so it is not shown.
        (-31337, "MCPError"),
        (40123, "MCPError"),
    ],
)
def test_json_rpc_error_shows_only_a_reserved_code(tmp_path, grouped, code, expected):
    """An upstream JSON-RPC error's message can quote STM's request back — a
    URL query, an argument, one token of a header — so it is rendered like any
    other exception, on every surface. A reserved code is kept; any other code
    is upstream-chosen and dropped. A group is unwrapped first."""
    from memtomem_stm.proxy.manager import _safe_error_text

    exc: BaseException = _mcp_error(code, f"bad query token cnryQueryTok via {_URL} {_ARG}")
    if grouped:
        exc = ExceptionGroup("unhandled errors in a TaskGroup", [exc])

    assert _manager(tmp_path).safe_upstream_error("srv", exc) == expected
    assert _safe_error_text(exc) == expected


def _stm_composed() -> list[tuple[BaseException, str]]:
    from mcp.server.mcpserver.exceptions import ToolError

    from memtomem_stm.proxy._locks import LockTimeoutError
    from memtomem_stm.proxy.manager import ManagerStoppingError

    return [
        (ToolError("circuit breaker open; retry in ~5s"), "ToolError: circuit breaker open"),
        (LockTimeoutError("compression", 2.0), "LockTimeoutError: bounded_lock timeout"),
        (ManagerStoppingError("proxy manager is stopping"), "ManagerStoppingError: proxy"),
    ]


@pytest.mark.parametrize("index", range(3))
def test_stm_composed_failures_keep_their_text(tmp_path, index):
    from memtomem_stm.proxy.manager import _safe_error_text

    exc, expected = _stm_composed()[index]
    assert _safe_error_text(exc).startswith(expected)
    assert _manager(tmp_path).safe_upstream_error("srv", exc).startswith(expected)


async def test_unknown_server_and_deadline_keep_their_text(tmp_path):
    mgr = _manager(tmp_path)
    with pytest.raises(KeyError) as unknown:
        await mgr.call_tool("nope", "tool", {})
    assert mgr.safe_upstream_error("nope", unknown.value) == (
        "KeyError: Unknown upstream server: 'nope'"
    )

    mgr = _manager(tmp_path)
    conn = mgr._connections["srv"]
    conn.config = conn.config.model_copy(update={"overall_deadline_seconds": 0.0})
    mgr._config.upstream_servers["srv"] = conn.config
    conn.session.call_tool.side_effect = ConnectionError("down")
    with patch.object(mgr, "_reconnect_server", new_callable=AsyncMock):
        with pytest.raises(TimeoutError) as deadline:
            await mgr.call_tool("srv", "tool", {})
    assert "exceeded overall_deadline_seconds" in mgr.safe_upstream_error("srv", deadline.value)


def test_rendered_text_is_capped(tmp_path):
    """No surface grows unbounded: a long STM-composed message is cut at the
    persistence cap on the log/metrics path and at the client boundary."""
    from mcp.server.mcpserver.exceptions import ToolError

    from memtomem_stm.proxy.manager import _safe_error_text
    from memtomem_stm.proxy.metrics import MAX_ERROR_MESSAGE_CHARS

    long = ToolError("x" * (MAX_ERROR_MESSAGE_CHARS * 2))
    assert len(_safe_error_text(long)) == MAX_ERROR_MESSAGE_CHARS
    assert len(_manager(tmp_path).safe_upstream_error("srv", long)) == MAX_ERROR_MESSAGE_CHARS


def test_validation_summary_names_types_not_locations():
    """A location can be a key of the data that failed (#1082 review)."""
    from memtomem_stm.utils.redact import exception_summary

    try:
        TypeAdapter(dict[str, int]).validate_python({"cnryHdrVal": "bad", "cnryEnvVal": "x"})
    except ValidationError as exc:
        assert exception_summary(exc) == "ValidationError: int_parsing"
