"""One config snapshot per proxied request (#871).

``ProxyManager._config`` is a property over ``ProxyConfigLoader.get()``, and
every ``get()`` does a ``Path.stat()``. The manager reads that property from
dozens of places, so an unsnapshotted request issued a burst of redundant
syscalls *and* could mix two reload generations within a single call — the
policy gate deciding on one config while a later stage ran on another.

These tests pin the bound (one loader read per request) and the hot-reload
granularity that comes with it (a config edit lands on the next request, not
mid-pipeline).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore


def _result(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        is_error=False,
        structured_content=None,
        meta=None,
    )


def _build_mgr(tmp_path: Path) -> tuple[ProxyManager, MetricsStore]:
    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=CompressionStrategy.TRUNCATE,
        max_result_chars=200,
        max_retries=0,
        reconnect_delay_seconds=0.0,
    )
    cfg_path = tmp_path / "proxy.json"
    cfg_path.write_text(json.dumps({"enabled": True}))
    proxy_cfg = ProxyConfig(
        config_path=cfg_path,
        enabled=True,
        upstream_servers={"srv": server_cfg},
    )
    mgr = ProxyManager(proxy_cfg, TokenTracker(metrics_store=store))
    session = AsyncMock()
    session.call_tool.return_value = _result("x" * 5000)
    mgr._connections["srv"] = UpstreamConnection(
        name="srv", config=server_cfg, session=session, tools=[]
    )
    return mgr, store


@pytest.fixture
def mgr(tmp_path):
    manager, store = _build_mgr(tmp_path)
    yield manager
    store.close()


def _count_loader_reads(manager: ProxyManager) -> list[int]:
    """Wrap the manager's loader so every ``get()`` bumps a counter."""
    calls = [0]
    real_get = manager._config_loader.get

    def counting_get():
        calls[0] += 1
        return real_get()

    manager._config_loader.get = counting_get  # type: ignore[method-assign]
    return calls


class TestPerRequestSnapshot:
    async def test_one_loader_read_per_call_tool(self, mgr):
        """The whole request — policy gate, ranking, cache lookup, and every
        pipeline stage — runs off a single snapshot."""
        calls = _count_loader_reads(mgr)
        await mgr.call_tool("srv", "tool", {"a": 1})
        assert calls[0] == 1, f"expected 1 loader read per request, got {calls[0]}"

    async def test_snapshot_does_not_leak_across_requests(self, mgr):
        """One per request, not one per manager: the second call re-reads."""
        calls = _count_loader_reads(mgr)
        await mgr.call_tool("srv", "tool", {"a": 1})
        await mgr.call_tool("srv", "tool", {"b": 2})
        assert calls[0] == 2

    async def test_config_edit_lands_on_the_next_request(self, mgr, tmp_path):
        """Snapshotting moves hot-reload to a request boundary; it must not
        turn into "restart required"."""
        await mgr.call_tool("srv", "tool", {"a": 1})
        assert mgr._config.default_max_result_chars != 1234

        (tmp_path / "proxy.json").write_text(
            json.dumps({"enabled": True, "default_max_result_chars": 1234})
        )
        # Force an mtime the loader is guaranteed to notice.
        import os

        seen = mgr._config_loader._mtime
        os.utime(tmp_path / "proxy.json", (seen + 10, seen + 10))

        await mgr.call_tool("srv", "tool", {"b": 2})
        assert mgr._config.default_max_result_chars == 1234
