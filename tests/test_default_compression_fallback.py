"""Regression tests for ``ProxyConfig.default_compression`` (#292).

The field was previously declared but unread, so an operator setting
``default_compression`` in ``stm_proxy.json`` saw no effect on any upstream.
``_resolve_tool_config`` now uses ``UpstreamServerConfig.model_fields_set`` to
distinguish "operator omitted compression" (→ honour the global default) from
"operator explicitly typed compression: auto" (→ honour their explicit choice).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker


def _make_manager(proxy_cfg: ProxyConfig, server_cfg: UpstreamServerConfig) -> ProxyManager:
    mgr = ProxyManager(proxy_cfg, TokenTracker())
    mgr._connections["srv"] = UpstreamConnection(
        name="srv",
        config=server_cfg,
        session=AsyncMock(),
        tools=[],
    )
    return mgr


class TestDefaultCompressionFallback:
    def test_global_default_applies_when_server_omits_compression(self):
        """Server config omits ``compression`` → global default wins."""
        server_cfg = UpstreamServerConfig(prefix="x")
        proxy_cfg = ProxyConfig(
            upstream_servers={"srv": server_cfg},
            default_compression=CompressionStrategy.SELECTIVE,
        )
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc = mgr._resolve_tool_config("srv", "any_tool")
        assert tc.compression == CompressionStrategy.SELECTIVE

    def test_explicit_per_server_wins_over_global(self):
        """Server explicitly sets a non-default strategy → per-server wins."""
        server_cfg = UpstreamServerConfig(
            prefix="x",
            compression=CompressionStrategy.TRUNCATE,
        )
        proxy_cfg = ProxyConfig(
            upstream_servers={"srv": server_cfg},
            default_compression=CompressionStrategy.SELECTIVE,
        )
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc = mgr._resolve_tool_config("srv", "any_tool")
        assert tc.compression == CompressionStrategy.TRUNCATE

    def test_explicit_per_server_auto_preserved_against_global(self):
        """Operator explicitly types ``compression: auto`` on a server while
        the global default is ``selective`` → server's explicit AUTO wins.

        This is the contract that ``model_fields_set`` enables and that a
        plain default-equality check would silently violate (because the
        explicit ``auto`` is indistinguishable from "field not provided").
        """
        server_cfg = UpstreamServerConfig(
            prefix="x",
            compression=CompressionStrategy.AUTO,
        )
        proxy_cfg = ProxyConfig(
            upstream_servers={"srv": server_cfg},
            default_compression=CompressionStrategy.SELECTIVE,
        )
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc = mgr._resolve_tool_config("srv", "any_tool")
        assert tc.compression == CompressionStrategy.AUTO

    def test_default_global_default_preserves_auto_behavior(self):
        """No operator change anywhere → AUTO, unchanged from pre-#292 behavior."""
        server_cfg = UpstreamServerConfig(prefix="x")
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg})
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc = mgr._resolve_tool_config("srv", "any_tool")
        assert tc.compression == CompressionStrategy.AUTO
