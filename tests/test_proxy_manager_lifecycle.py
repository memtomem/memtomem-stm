"""Tests for ProxyManager lifecycle — start, stop, double-start guard."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from memtomem_stm.proxy.config import (
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_manager(
    servers: dict[str, UpstreamServerConfig] | None = None,
    tmp_path: Path | None = None,
) -> ProxyManager:
    """Create a ProxyManager with configurable upstream servers."""
    if servers is None:
        servers = {
            "srv": UpstreamServerConfig(prefix="test"),
        }
    config_path = (tmp_path / "proxy.json") if tmp_path else Path("/tmp/proxy.json")
    proxy_cfg = ProxyConfig(config_path=config_path, upstream_servers=servers)
    return ProxyManager(proxy_cfg, TokenTracker())


# ── start() ──────────────────────────────────────────────────────────────


class TestStart:
    async def test_start_connects_to_servers(self):
        """start() calls _connect_server for each configured upstream server."""
        mgr = _make_manager(
            servers={
                "a": UpstreamServerConfig(prefix="a"),
                "b": UpstreamServerConfig(prefix="b"),
            }
        )
        with patch.object(mgr, "_connect_server", new_callable=AsyncMock) as mock_conn:
            await mgr.start()

        called_names = [call.args[0] for call in mock_conn.call_args_list]
        assert sorted(called_names) == ["a", "b"]

    async def test_start_empty_servers_loads_file(self, tmp_path):
        """When upstream_servers is empty, start() falls back to load_from_file."""
        mgr = _make_manager(servers={}, tmp_path=tmp_path)
        loaded_cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={"file_srv": UpstreamServerConfig(prefix="fs")},
        )
        with (
            patch.object(ProxyConfig, "load_from_file", return_value=loaded_cfg),
            patch.object(mgr, "_connect_server", new_callable=AsyncMock) as mock_conn,
        ):
            await mgr.start()

        assert mock_conn.call_count == 1
        assert mock_conn.call_args_list[0].args[0] == "file_srv"

    async def test_start_empty_servers_no_file_noop(self, tmp_path):
        """No servers configured and no file — start() completes without error."""
        mgr = _make_manager(servers={}, tmp_path=tmp_path)
        with (
            patch.object(ProxyConfig, "load_from_file", return_value=None),
            patch.object(mgr, "_connect_server", new_callable=AsyncMock) as mock_conn,
        ):
            await mgr.start()

        assert mock_conn.call_count == 0

    async def test_start_server_failure_logged(self, caplog):
        """If _connect_server raises, start() logs and continues."""
        mgr = _make_manager(
            servers={
                "ok": UpstreamServerConfig(prefix="ok"),
                "bad": UpstreamServerConfig(prefix="bad"),
            }
        )

        async def _conditional_connect(name, cfg, seen):
            if name == "bad":
                raise ConnectionError("unreachable")

        with patch.object(mgr, "_connect_server", side_effect=_conditional_connect):
            await mgr.start()

        assert "Failed to connect to upstream server 'bad'" in caplog.text

    async def test_double_start_closes_previous(self):
        """Calling start() twice closes the previous AsyncExitStack."""
        mgr = _make_manager(servers={})
        with patch.object(ProxyConfig, "load_from_file", return_value=None):
            await mgr.start()

        first_stack = mgr._stack
        assert first_stack is not None

        with (
            patch.object(ProxyConfig, "load_from_file", return_value=None),
            patch.object(first_stack, "aclose", new_callable=AsyncMock) as mock_close,
        ):
            await mgr.start()

        mock_close.assert_awaited_once()
        assert mgr._stack is not first_stack


# ── stop() ────────────────────────────────────────────────────────────────


class TestStop:
    async def test_stop_cancels_background_tasks(self):
        """stop() cancels all background tasks and gathers them."""
        mgr = _make_manager(servers={})

        async def _forever():
            await asyncio.sleep(999)

        task = asyncio.create_task(_forever())
        mgr._background_tasks.add(task)

        await mgr.stop()

        assert task.cancelled()
        assert len(mgr._background_tasks) == 0

    async def test_stop_closes_extractor(self):
        """stop() calls close() on the extractor if present."""
        mgr = _make_manager(servers={})
        mock_ext = AsyncMock()
        mgr._extractor = mock_ext

        await mgr.stop()

        mock_ext.close.assert_awaited_once()

    async def test_stop_closes_connection_stacks(self):
        """stop() closes per-connection stacks and clears _connections."""
        mgr = _make_manager(servers={})

        mock_stack = AsyncMock()
        conn = UpstreamConnection(
            name="srv",
            config=UpstreamServerConfig(prefix="test"),
            session=AsyncMock(),
            tools=[],
            stack=mock_stack,
        )
        mgr._connections["srv"] = conn

        await mgr.stop()

        mock_stack.aclose.assert_awaited_once()
        assert len(mgr._connections) == 0
