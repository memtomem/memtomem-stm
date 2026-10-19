"""Upstream tool names are checked against the MCP tool-name grammar at ingest (#1090).

A tool name comes from the upstream's ``tools/list`` and is untrusted text. It
used to reach ~70 log lines, MCP responses, LTM frontmatter and telemetry, plus
the SDK's own registration warning, unchecked. A tool named outside the
grammar is now refused where the catalogue is published, and counted rather
than named everywhere it is reported.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import mcp.types as mcp_types
import pytest

from memtomem_stm.proxy import tool_name_budget
from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.config import CacheConfig, ProxyConfig, UpstreamServerConfig
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker

# Each one is outside the grammar for a different reason; the marker text in
# each is what the leak assertions look for.
BAD_NAMES = ("bad tool", "evilA\x1b[31m", "evilB‮", "evilC" + "x" * 124)
_LEAK_MARKERS = ("bad tool", "evilA", "evilB", "evilC")


def _tool(name: str) -> mcp_types.Tool:
    return mcp_types.Tool(name=name, description="d", input_schema={"type": "object"})


def _listing(*names: str) -> SimpleNamespace:
    return SimpleNamespace(tools=[_tool(n) for n in names])


def _assert_no_bad_name(text: str) -> None:
    for marker in _LEAK_MARKERS:
        assert marker not in text


class TestGrammar:
    @pytest.mark.parametrize("name", ["a", "read_file", "a.b-c_D9", "x" * 128])
    def test_conforming(self, name):
        assert tool_name_budget.is_conforming_tool_name(name)

    @pytest.mark.parametrize(
        "name",
        ["", "x" * 129, "read_file\n", "bad tool", "a,b", "a/b", "evil\x1b[31m", "x‮", "도구"],
    )
    def test_nonconforming(self, name):
        assert not tool_name_budget.is_conforming_tool_name(name)

    @pytest.mark.parametrize("name", [None, 7, b"read_file"])
    def test_non_string_is_nonconforming(self, name):
        assert not tool_name_budget.is_conforming_tool_name(name)

    def test_split_keeps_order_and_counts_the_rest(self):
        tools = [_tool("a"), _tool("bad tool"), _tool("b"), SimpleNamespace(name=None)]
        kept, refused = tool_name_budget.conforming_tools(tools)
        assert [t.name for t in kept] == ["a", "b"]
        assert refused == 2


# ── ProxyManager: connect, reconnect, refresh ────────────────────────────


def _cfg() -> UpstreamServerConfig:
    return UpstreamServerConfig(prefix="t", max_retries=0, reconnect_delay_seconds=0.0)


def _manager(tmp_path: Path, cfg: UpstreamServerConfig) -> ProxyManager:
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": cfg},
        cache=CacheConfig(default_ttl_seconds=3600.0),
    )
    return ProxyManager(proxy_cfg, TokenTracker())


def _session(listing: SimpleNamespace) -> AsyncMock:
    s = AsyncMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.initialize = AsyncMock()
    s.list_tools = AsyncMock(return_value=listing)
    return s


def _transport() -> AsyncMock:
    t = AsyncMock()
    t.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
    t.__aexit__ = AsyncMock(return_value=False)
    return t


def _real_owner_patches(mgr: ProxyManager, listing: SimpleNamespace):
    """Drive the real connection-owner task; only the transport and session
    are fakes, so the catalogue arrives the way ``tools/list`` delivers it."""
    return (
        patch.object(mgr, "_open_transport", side_effect=lambda c: _transport()),
        patch(
            "memtomem_stm.proxy.manager.ClientSession",
            side_effect=lambda *a, **k: _session(listing),
        ),
    )


async def _close(mgr: ProxyManager) -> None:
    for conn in mgr._connections.values():
        if conn.owner is not None:
            await conn.owner.close()


@pytest.mark.asyncio
class TestManagerIngest:
    async def test_connect_refuses_and_counts_without_naming(self, tmp_path, caplog):
        cfg = _cfg()
        mgr = _manager(tmp_path, cfg)
        mgr._stack = AsyncExitStack()
        p1, p2 = _real_owner_patches(mgr, _listing("ok_tool", *BAD_NAMES))

        with caplog.at_level(logging.DEBUG), p1, p2:
            await mgr._connect_server("srv", cfg)
        try:
            conn = mgr._connections["srv"]
            assert [t.name for t in conn.tools] == ["ok_tool"]
            assert conn.nonconforming_tools == len(BAD_NAMES)
            assert [i.original_name for i in mgr.get_proxy_tools()] == ["ok_tool"]
            assert "listed 4 tool(s) whose names are outside the MCP" in caplog.text
            _assert_no_bad_name(caplog.text)
        finally:
            await _close(mgr)

    async def test_reconnect_refuses_and_resets_the_count(self, tmp_path):
        """Reconnect publishes its own catalogue; it must pass the same check,
        and a clean relisting must clear the old count rather than keep it."""
        cfg = _cfg()
        mgr = _manager(tmp_path, cfg)
        mgr._connections["srv"] = UpstreamConnection(
            name="srv", config=cfg, session=AsyncMock(), tools=[], stack=AsyncMock()
        )
        conn = mgr._connections["srv"]
        try:
            p1, p2 = _real_owner_patches(mgr, _listing("ok_tool", *BAD_NAMES))
            with p1, p2:
                await mgr._reconnect_server("srv", cfg)
            assert [t.name for t in conn.tools] == ["ok_tool"]
            assert conn.nonconforming_tools == len(BAD_NAMES)

            p1, p2 = _real_owner_patches(mgr, _listing("ok_tool"))
            with p1, p2:
                await mgr._reconnect_server("srv", cfg)
            assert conn.nonconforming_tools == 0
        finally:
            await asyncio.gather(*mgr._background_tasks, return_exceptions=True)
            await _close(mgr)

    async def test_refresh_refuses_a_rename_and_drops_its_cache_rows(self, tmp_path, caplog):
        """A conforming tool renamed to a nonconforming one disappears from the
        catalogue, so the rows cached under the old name go with it."""
        cfg = _cfg()
        mgr = _manager(tmp_path, cfg)
        cache = ProxyCache(tmp_path / "c.db", max_entries=100)
        cache.initialize()
        mgr._cache = cache
        conn = UpstreamConnection(
            name="srv", config=cfg, session=AsyncMock(), tools=[_tool("search"), _tool("ls")]
        )
        mgr._connections["srv"] = conn
        try:
            assert [i.original_name for i in mgr.get_proxy_tools()] == ["search", "ls"]
            cache.set("srv", "search", {"q": 1}, "cached", 3600.0)
            conn.session.list_tools.return_value = _listing("ls", "search me")

            with caplog.at_level(logging.DEBUG):
                await mgr._refresh_server_tools("srv")

            assert [t.name for t in conn.tools] == ["ls"]
            assert conn.nonconforming_tools == 1
            assert cache.get("srv", "search", {"q": 1}) is None
            assert "search me" not in caplog.text
            # The advertisement is rebuilt from the refreshed catalogue: the
            # old name is withdrawn and the refused one never appears.
            assert [i.original_name for i in mgr.get_proxy_tools()] == ["ls"]

            conn.session.list_tools.return_value = _listing("ls")
            await mgr._refresh_server_tools("srv")
            assert conn.nonconforming_tools == 0
        finally:
            cache.close()

    async def test_stale_refresh_does_not_overwrite_the_count(self, tmp_path):
        """A refresh whose session was replaced mid-flight applies nothing,
        the count included."""
        cfg = _cfg()
        mgr = _manager(tmp_path, cfg)
        conn = UpstreamConnection(name="srv", config=cfg, session=AsyncMock(), tools=[])
        mgr._connections["srv"] = conn
        replacement = AsyncMock()

        async def _list_then_swap():
            conn.session = replacement
            return _listing("ok_tool", *BAD_NAMES)

        conn.session.list_tools = _list_then_swap

        await mgr._refresh_server_tools("srv")

        assert conn.tools == []
        assert conn.nonconforming_tools == 0

    async def test_sdk_registration_never_sees_a_refused_name(self, tmp_path, caplog):
        """The SDK logs a nonconforming name verbatim when STM registers it
        (``validate_and_warn_tool_name``). No per-site fix could reach that
        line; refusing at ingest is what keeps it from firing."""
        from mcp.server.mcpserver import MCPServer

        from memtomem_stm.proxy._fastmcp_compat import register_proxy_tool

        cfg = _cfg()
        mgr = _manager(tmp_path, cfg)
        mgr._stack = AsyncExitStack()
        p1, p2 = _real_owner_patches(mgr, _listing("ok_tool", *BAD_NAMES))
        with p1, p2:
            await mgr._connect_server("srv", cfg)
        try:
            server = MCPServer("grammar-test")
            with caplog.at_level(logging.DEBUG):
                for info in mgr.get_proxy_tools():

                    async def proxy_tool(**kwargs: object) -> str:
                        return "ok"

                    register_proxy_tool(server, proxy_tool, info)

            registered = [t.name for t in await server.list_tools()]
            assert registered == ["t__ok_tool"]
            assert not [r for r in caplog.records if r.name == "mcp.shared.tool_name_validation"]
            _assert_no_bad_name(caplog.text)
        finally:
            await _close(mgr)


# ── Health ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestHealth:
    async def test_upstream_health_counts_refused_tools(self, tmp_path):
        cfg = _cfg()
        mgr = _manager(tmp_path, cfg)
        mgr._connections["srv"] = UpstreamConnection(
            name="srv", config=cfg, session=AsyncMock(), tools=[_tool("a")], nonconforming_tools=3
        )
        mgr._failed_servers["down"] = "configuration error"

        health = mgr.get_upstream_health()

        assert health["srv"]["tools"] == 1
        assert health["srv"]["nonconforming_tools"] == 3
        assert health["down"]["nonconforming_tools"] == 0

    @pytest.mark.parametrize(
        ("tools", "refused", "advertised", "head", "note"),
        [
            # mixed: "discovered" stays the whole listing
            (1, 3, 1, "(4 tools discovered, 1 advertised)", True),
            # every listed tool refused
            (0, 2, 0, "(2 tools discovered, 0 advertised)", True),
            # conforming but withheld by the exposure filter: no grammar line
            (2, 0, 1, "(2 tools discovered, 1 advertised)", False),
        ],
    )
    async def test_proxy_health_text(self, tmp_path, tools, refused, advertised, head, note):
        from memtomem_stm.config import STMConfig
        from memtomem_stm.server import STMContext, stm_proxy_health

        tracker = TokenTracker()
        pm = _manager(tmp_path, _cfg())
        health = {
            "srv": {
                "connected": True,
                "tools": tools,
                "nonconforming_tools": refused,
                "advertised_tools": advertised,
            }
        }
        app = STMContext(
            config=STMConfig(),
            proxy_manager=pm,
            tracker=tracker,
            surfacing_engine=None,
            feedback_tracker=None,
            compression_feedback_tracker=None,
            progressive_reads_tracker=None,
        )
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))

        with patch.object(pm, "get_upstream_health", return_value=health):
            result = await stm_proxy_health(ctx=ctx)

        assert f"srv: connected {head}" in result
        line = f"{refused} tool(s) not proxied: name outside the MCP tool-name grammar"
        assert (line in result) is note


# ── CLI probe (mms health / mms doctor) ──────────────────────────────────


def test_probe_counts_refused_tools_and_names_none(monkeypatch):
    import mcp
    import mcp.client.stdio

    from memtomem_stm.cli.proxy import _probe_one

    long_ok = "ok_" + "x" * 60  # conforming, but overflows the 64-char budget

    @asynccontextmanager
    async def fake_stdio_client(params):
        yield (object(), object())

    class FakeSession:
        def __init__(self, *streams):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return None

        async def initialize(self):
            return None

        async def list_tools(self):
            # Every tool's schema carries a removable description, so each
            # per-tool field would name a refused tool if it read the raw list.
            schema = {"type": "object", "properties": {"q": {"type": "string", "description": "q"}}}
            return SimpleNamespace(
                tools=[
                    mcp_types.Tool(name=n, description="d", input_schema=schema)
                    for n in ("ok_tool", long_ok, *BAD_NAMES)
                ]
            )

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp, "ClientSession", FakeSession)

    result = asyncio.run(_probe_one({"command": "echo", "prefix": "t"}, 3.0))

    assert result.connected
    assert result.tools == 2
    assert result.nonconforming_tools == len(BAD_NAMES)
    assert result.overflowing == (long_ok,)
    assert [n for n, _ in result.description_chars] == ["ok_tool", long_ok]
    assert result.schema_description_tools == ("ok_tool", long_ok)
    payload = result.as_dict()
    assert payload["nonconforming_tools"] == len(BAD_NAMES)
    _assert_no_bad_name(repr(result))
