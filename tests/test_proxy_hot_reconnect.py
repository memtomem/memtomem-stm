"""PR ⑦ — hot-reload of upstream connection config via reconnect.

Pins the acceptance criteria of the runtime-lifecycle plan section:
a reconnect applies the CURRENT config snapshot (not the connect-time one),
the replacement connection is prepared before the old one is torn down, and
a failed replacement leaves the previous connection serving.

All transports are mocked (CLAUDE.md: no live upstreams). The config loader
is reseeded via ``ProxyConfigLoader.seed`` with a ``tmp_path``-based config
path — the path never exists on disk, so the mtime probe keeps returning the
seeded snapshot deterministically (a real file at a shared path like
``/tmp/proxy.json`` could shadow the reseed mid-run).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from memtomem_stm.proxy.config import (
    ProxyConfig,
    TransportType,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_manager(tmp_path: Path, servers: dict[str, UpstreamServerConfig]) -> ProxyManager:
    proxy_cfg = ProxyConfig(config_path=tmp_path / "proxy.json", upstream_servers=servers)
    return ProxyManager(proxy_cfg, TokenTracker())


def _reseed(mgr: ProxyManager, tmp_path: Path, servers: dict[str, UpstreamServerConfig]) -> None:
    """Simulate a config-file hot-reload: install a new snapshot in the loader."""
    mgr._config_loader.seed(
        ProxyConfig(config_path=tmp_path / "proxy.json", upstream_servers=servers)
    )


def _seed_connection(
    mgr: ProxyManager, name: str, cfg: UpstreamServerConfig, *, stack: AsyncMock | None = None
) -> UpstreamConnection:
    conn = UpstreamConnection(
        name=name,
        config=cfg,
        session=AsyncMock(),
        tools=[],
        stack=stack if stack is not None else AsyncMock(),
    )
    mgr._connections[name] = conn
    return conn


def _mock_session() -> AsyncMock:
    s = AsyncMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.initialize = AsyncMock()
    s.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[]))
    return s


def _mock_transport() -> AsyncMock:
    t = AsyncMock()
    t.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
    t.__aexit__ = AsyncMock(return_value=False)
    return t


def _sse_cfg(url: str) -> UpstreamServerConfig:
    return UpstreamServerConfig(prefix="srv", transport=TransportType.SSE, url=url)


# ── reconnect reads the current snapshot ────────────────────────────────


class TestReconnectUsesCurrentSnapshot:
    async def test_reconnect_uses_current_config_snapshot(self, tmp_path):
        """An url edit that hot-reloaded after connect is applied by the next
        reconnect: the new transport opens with the CURRENT config, and
        ``conn.config`` is refreshed to it."""
        cfg_a = _sse_cfg("https://old.example/sse")
        cfg_b = _sse_cfg("https://new.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        _seed_connection(mgr, "srv", cfg_a)
        _reseed(mgr, tmp_path, {"srv": cfg_b})

        captured: list[UpstreamServerConfig] = []
        mock_session = _mock_session()

        def _capture_open(cfg):
            captured.append(cfg)
            return _mock_transport()

        with (
            patch.object(mgr, "_open_transport", side_effect=_capture_open),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
        ):
            await mgr._reconnect_server("srv")

        conn = mgr._connections["srv"]
        assert captured == [cfg_b]
        assert conn.config is cfg_b
        assert conn.session is mock_session
        assert conn.reconnect_generation == 1

    async def test_reconnect_falls_back_when_server_key_removed(self, tmp_path):
        """Removing a server from the file stays restart-only: a reconnect for
        a connection whose key vanished reuses the connect-time snapshot
        instead of crashing or tearing the server down."""
        cfg_a = _sse_cfg("https://old.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        _seed_connection(mgr, "srv", cfg_a)
        _reseed(mgr, tmp_path, {})

        captured: list[UpstreamServerConfig] = []
        mock_session = _mock_session()

        def _capture_open(cfg):
            captured.append(cfg)
            return _mock_transport()

        with (
            patch.object(mgr, "_open_transport", side_effect=_capture_open),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
        ):
            await mgr._reconnect_server("srv")

        conn = mgr._connections["srv"]
        assert captured == [cfg_a]
        assert conn.config is cfg_a
        assert conn.reconnect_generation == 1


# ── prepare-first swap ───────────────────────────────────────────────────


class TestPrepareFirstSwap:
    async def test_reconnect_failure_keeps_old_connection(self, tmp_path):
        """The replacement is prepared BEFORE the old connection is touched:
        when it can't be established, the previous session/stack/config all
        survive and the old stack is never closed."""
        cfg_a = _sse_cfg("https://old.example/sse")
        cfg_b = _sse_cfg("https://new.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg_a})
        old_stack = AsyncMock()
        conn = _seed_connection(mgr, "srv", cfg_a, stack=old_stack)
        old_session = conn.session
        _reseed(mgr, tmp_path, {"srv": cfg_b})

        failing_transport = _mock_transport()
        failing_transport.__aenter__ = AsyncMock(side_effect=ConnectionError("refused"))

        with (
            patch.object(mgr, "_open_transport", return_value=failing_transport),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=_mock_session()),
        ):
            with pytest.raises(ConnectionError, match="refused"):
                await mgr._reconnect_server("srv")

        assert conn.session is old_session
        assert conn.stack is old_stack
        assert conn.config is cfg_a
        assert conn.reconnect_generation == 0
        old_stack.aclose.assert_not_awaited()

    async def test_reconnect_success_closes_old_stack_after_new_ready(self, tmp_path):
        """Swap ordering: the old stack is closed only after the replacement
        has fully initialized and discovered tools."""
        cfg = _sse_cfg("https://up.example/sse")
        mgr = _make_manager(tmp_path, {"srv": cfg})
        events: list[str] = []

        old_stack = AsyncMock()

        async def _old_close():
            events.append("old_close")

        old_stack.aclose = AsyncMock(side_effect=_old_close)
        _seed_connection(mgr, "srv", cfg, stack=old_stack)

        mock_session = _mock_session()

        async def _init():
            events.append("initialize")

        async def _list_tools():
            events.append("list_tools")
            return SimpleNamespace(tools=[])

        mock_session.initialize = AsyncMock(side_effect=_init)
        mock_session.list_tools = AsyncMock(side_effect=_list_tools)

        with (
            patch.object(mgr, "_open_transport", return_value=_mock_transport()),
            patch("memtomem_stm.proxy.manager.ClientSession", return_value=mock_session),
        ):
            await mgr._reconnect_server("srv")

        assert events == ["initialize", "list_tools", "old_close"]
        old_stack.aclose.assert_awaited_once()
        assert mgr._connections["srv"].reconnect_generation == 1
