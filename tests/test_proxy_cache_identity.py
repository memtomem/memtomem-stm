"""Response caches must name the connection that actually served the RPC."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import CallToolResult, TextContent

from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.config import CompressionStrategy, ProxyConfig, UpstreamServerConfig
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker


def _result(text):
    return CallToolResult(content=[TextContent(type="text", text=text)])


@pytest.fixture
async def setup_proxy(tmp_path):
    cfg_a = UpstreamServerConfig(
        prefix="srv",
        transport="sse",
        url="https://a.example/sse",
        compression=CompressionStrategy.NONE,
    )
    config = ProxyConfig(config_path=tmp_path / "absent.json", upstream_servers={"srv": cfg_a})
    cache = ProxyCache(tmp_path / "cache.db")
    cache.initialize()
    manager = ProxyManager(config, TokenTracker(), cache=cache)
    session = AsyncMock()
    session.call_tool.return_value = _result("record from A")
    manager._connections["srv"] = UpstreamConnection(
        name="srv",
        config=cfg_a,
        session=session,
        tools=[],
    )
    yield manager, cache, config, session
    await manager.stop()
    cache.close()


@pytest.mark.parametrize(
    "edit",
    [
        {"url": "https://b.example/sse"},
        {"headers": {"Authorization": "Bearer account-b"}},
        {"transport": "stdio", "command": "worker", "args": ["--tenant", "b"]},
    ],
)
async def test_identity_edit_cannot_serve_old_cache(setup_proxy, edit):
    mgr, cache, config, old_session = setup_proxy
    assert await mgr.call_tool("srv", "read", {"id": 1}) == "record from A"
    cfg_b = UpstreamServerConfig.model_validate(
        {**config.upstream_servers["srv"].model_dump(), **edit}
    )
    mgr._config_loader.seed(config.model_copy(update={"upstream_servers": {"srv": cfg_b}}))
    new_session = AsyncMock()
    new_session.call_tool.return_value = _result("record from B")
    with patch.object(
        mgr, "_establish_connection", AsyncMock(return_value=(new_session, None, []))
    ):
        assert await mgr.call_tool("srv", "read", {"id": 1}) == "record from B"
    # Both an already-swapped connection and a reopened persistent cache use B.
    cache.close()
    cache.initialize()
    assert await mgr.call_tool("srv", "read", {"id": 1}) == "record from B"
    assert old_session.call_tool.await_count == new_session.call_tool.await_count == 1
    rows = cache._db.execute("SELECT cache_key, envelope_json FROM proxy_cache").fetchall()
    assert "account-b" not in str(rows)
    assert "https://b.example" not in str(rows)


async def test_failed_edit_does_not_cache_fallback_as_new_identity(setup_proxy):
    mgr, cache, config, session = setup_proxy
    await mgr.call_tool("srv", "read", {})
    cfg_b = config.upstream_servers["srv"].model_copy(update={"url": "https://b.example/sse"})
    mgr._config_loader.seed(config.model_copy(update={"upstream_servers": {"srv": cfg_b}}))
    with patch.object(
        mgr, "_establish_connection", AsyncMock(side_effect=OSError("offline"))
    ) as open_b:
        for _ in range(2):
            assert await mgr.call_tool("srv", "read", {}) == "record from A"
    assert open_b.await_count == 1  # keep the existing failed-edit damping
    assert session.call_tool.await_count == 3  # fallback replies never populate B's key
    assert cache.stats()["total_entries"] == 1


@pytest.mark.parametrize(
    "edit",
    [
        {"command": "other-worker"},
        {"args": ["tenant-b"]},
        {"env": {"TENANT": "b"}},
    ],
)
async def test_active_stdio_fields_separate_cached_responses(setup_proxy, edit):
    mgr, _, config, old_session = setup_proxy
    cfg_a = UpstreamServerConfig(
        prefix="srv",
        command="worker",
        args=["tenant-a"],
        env={"TENANT": "a"},
        compression=CompressionStrategy.NONE,
    )
    mgr._connections["srv"].config = cfg_a
    mgr._config_loader.seed(config.model_copy(update={"upstream_servers": {"srv": cfg_a}}))
    assert await mgr.call_tool("srv", "read", {}) == "record from A"
    cfg_b = cfg_a.model_copy(update=edit)
    mgr._config_loader.seed(config.model_copy(update={"upstream_servers": {"srv": cfg_b}}))
    new_session = AsyncMock()
    new_session.call_tool.return_value = _result("record from B")
    with patch.object(
        mgr, "_establish_connection", AsyncMock(return_value=(new_session, None, []))
    ):
        assert await mgr.call_tool("srv", "read", {}) == "record from B"
    assert await mgr.call_tool("srv", "read", {}) == "record from B"
    assert old_session.call_tool.await_count == new_session.call_tool.await_count == 1


async def test_old_inflight_reply_cannot_poison_new_connection_cache(setup_proxy):
    mgr, _, config, old_session = setup_proxy
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_a(*args, **kwargs):
        entered.set()
        await release.wait()
        return _result("late reply from A")

    old_session.call_tool.side_effect = slow_a
    old_call = asyncio.create_task(mgr.call_tool("srv", "read", {"id": 1}))
    await asyncio.wait_for(entered.wait(), 2)
    cfg_b = config.upstream_servers["srv"].model_copy(update={"url": "https://b.example/sse"})
    mgr._config_loader.seed(config.model_copy(update={"upstream_servers": {"srv": cfg_b}}))
    new_session = AsyncMock()
    new_session.call_tool.return_value = _result("reply from B")
    try:
        with patch.object(
            mgr, "_establish_connection", AsyncMock(return_value=(new_session, None, []))
        ):
            assert await mgr.call_tool("srv", "read", {"id": 1}) == "reply from B"
    finally:
        release.set()
        await old_call
    assert await mgr.call_tool("srv", "read", {"id": 1}) == "reply from B"
    assert new_session.call_tool.await_count == 1


async def test_dispatch_identity_is_captured_before_rpc_await(setup_proxy):
    """A request pinned to B may fall back to A while a peer installs B."""
    mgr, cache, config, old_session = setup_proxy
    cfg_b = config.upstream_servers["srv"].model_copy(update={"url": "https://b.example/sse"})
    mgr._config_loader.seed(config.model_copy(update={"upstream_servers": {"srv": cfg_b}}))
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_a(*args, **kwargs):
        entered.set()
        await release.wait()
        return _result("late fallback A")

    old_session.call_tool.side_effect = slow_a
    with patch.object(mgr, "_establish_connection", AsyncMock(side_effect=OSError("offline"))):
        old_call = asyncio.create_task(mgr.call_tool("srv", "read", {}))
        await asyncio.wait_for(entered.wait(), 2)
    new_session = AsyncMock()
    new_session.call_tool.return_value = _result("B")
    try:
        with patch.object(
            mgr, "_establish_connection", AsyncMock(return_value=(new_session, None, []))
        ):
            await mgr._reconnect_server("srv", cfg_b)
    finally:
        release.set()
        assert await old_call == "late fallback A"
    assert cache.stats()["total_entries"] == 0
    assert await mgr.call_tool("srv", "read", {}) == "B"


async def test_stop_during_postprocessing_does_not_lose_successful_response(setup_proxy):
    mgr, _, _, _ = setup_proxy
    original = mgr._run_index_stage

    async def stop_after_index(**kwargs):
        result = await original(**kwargs)
        await mgr.stop()
        return result

    with patch.object(mgr, "_run_index_stage", side_effect=stop_after_index):
        assert await mgr.call_tool("srv", "read", {}) == "record from A"


@pytest.mark.parametrize(
    "edit", [{"command": "unused"}, {"args": ["unused"]}, {"env": {"UNUSED": "x"}}]
)
async def test_inactive_transport_fields_do_not_rotate_key(setup_proxy, edit):
    mgr, _, config, session = setup_proxy
    await mgr.call_tool("srv", "read", {})
    cfg_b = config.upstream_servers["srv"].model_copy(update=edit)
    mgr._config_loader.seed(config.model_copy(update={"upstream_servers": {"srv": cfg_b}}))
    assert await mgr.call_tool("srv", "read", {}) == "record from A"
    assert session.call_tool.await_count == 1
