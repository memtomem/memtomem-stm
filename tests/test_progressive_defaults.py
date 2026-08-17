"""Regression tests for progressive compression without a ``progressive`` block (#830).

``"compression": "progressive"`` used to require a sibling ``"progressive": {}``
block: without it ``_compress_and_surface`` skipped the progressive branch and
fell through to ``get_compressor(PROGRESSIVE)``, which is a ``NoopCompressor`` —
so the full upstream response was shipped verbatim, with no chunking, no footer
and no warning. Every other strategy works with no extra block, and the docs
document ``chunk_size`` as having a default, so the block is optional: an
omitted block now resolves to ``ProgressiveConfig()`` defaults.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProgressiveConfig,
    ProxyConfig,
    ToolOverrideConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore


def _make_manager(server_cfg: UpstreamServerConfig, proxy_cfg: ProxyConfig) -> ProxyManager:
    mgr = ProxyManager(proxy_cfg, TokenTracker())
    mgr._connections["srv"] = UpstreamConnection(
        name="srv",
        config=server_cfg,
        session=AsyncMock(),
        tools=[],
    )
    return mgr


# ── _resolve_tool_config defaulting ──────────────────────────────────────


class TestResolveProgressiveDefault:
    def test_defaults_when_block_omitted(self):
        """Server picks progressive but ships no block → documented defaults."""
        server_cfg = UpstreamServerConfig(prefix="x", compression=CompressionStrategy.PROGRESSIVE)
        mgr = _make_manager(server_cfg, ProxyConfig(upstream_servers={"srv": server_cfg}))

        tc = mgr._resolve_tool_config("srv", "any_tool")

        assert tc.compression == CompressionStrategy.PROGRESSIVE
        assert tc.progressive == ProgressiveConfig()

    def test_defaults_when_strategy_comes_from_tool_override(self):
        """A tool override selecting progressive gets defaults too — the
        default must be applied AFTER the override merge, not from the
        server-level strategy alone."""
        server_cfg = UpstreamServerConfig(
            prefix="x",
            compression=CompressionStrategy.TRUNCATE,
            tool_overrides={
                "chunked_tool": ToolOverrideConfig(compression=CompressionStrategy.PROGRESSIVE)
            },
        )
        mgr = _make_manager(server_cfg, ProxyConfig(upstream_servers={"srv": server_cfg}))

        tc = mgr._resolve_tool_config("srv", "chunked_tool")

        assert tc.compression == CompressionStrategy.PROGRESSIVE
        assert tc.progressive == ProgressiveConfig()

    def test_defaults_when_strategy_comes_from_global_default(self):
        server_cfg = UpstreamServerConfig(prefix="x")
        proxy_cfg = ProxyConfig(
            upstream_servers={"srv": server_cfg},
            default_compression=CompressionStrategy.PROGRESSIVE,
        )
        mgr = _make_manager(server_cfg, proxy_cfg)

        tc = mgr._resolve_tool_config("srv", "any_tool")

        assert tc.progressive == ProgressiveConfig()

    def test_explicit_block_is_preserved(self):
        """Defaulting must never overwrite an operator's explicit tuning."""
        server_cfg = UpstreamServerConfig(
            prefix="x",
            compression=CompressionStrategy.PROGRESSIVE,
            progressive=ProgressiveConfig(chunk_size=123),
        )
        mgr = _make_manager(server_cfg, ProxyConfig(upstream_servers={"srv": server_cfg}))

        tc = mgr._resolve_tool_config("srv", "any_tool")

        assert tc.progressive is not None
        assert tc.progressive.chunk_size == 123

    def test_non_progressive_strategy_keeps_none(self):
        """Non-progressive servers keep ``progressive=None`` so the cache
        fingerprint (which hashes this field) does not rotate for them."""
        server_cfg = UpstreamServerConfig(prefix="x", compression=CompressionStrategy.TRUNCATE)
        mgr = _make_manager(server_cfg, ProxyConfig(upstream_servers={"srv": server_cfg}))

        tc = mgr._resolve_tool_config("srv", "any_tool")

        assert tc.progressive is None


# ── end-to-end call path ─────────────────────────────────────────────────


def _make_result(text: str):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], is_error=False)


def _make_e2e_manager(tmp_path: Path) -> tuple[ProxyManager, MetricsStore]:
    """Progressive server with NO ``progressive`` block — the #830 config."""
    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=CompressionStrategy.PROGRESSIVE,
        max_result_chars=50000,
        max_retries=0,
        reconnect_delay_seconds=0.0,
    )
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": server_cfg},
    )
    mgr = ProxyManager(proxy_cfg, TokenTracker(metrics_store=store))
    mgr._connections["srv"] = UpstreamConnection(
        name="srv",
        config=server_cfg,
        session=AsyncMock(),
        tools=[],
    )
    return mgr, store


def _latest_strategy(store: MetricsStore) -> str | None:
    row = store._db.execute(
        "SELECT compression_strategy FROM proxy_metrics ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0]


@pytest.mark.asyncio
class TestProgressiveWithoutBlockEndToEnd:
    async def test_large_response_is_chunked(self, tmp_path):
        """The #830 repro: a response well past the default 4000-char chunk
        size must come back as a first chunk with a read-more footer, not the
        full body."""
        mgr, store = _make_e2e_manager(tmp_path)
        large_text = "content paragraph. " * 600  # ~11.4k chars > default chunk_size
        mgr._connections["srv"].session.call_tool.return_value = _make_result(large_text)

        result = await mgr.call_tool("srv", "tool", {})

        assert "stm_proxy_read_more" in result
        assert len(result) < len(large_text)
        assert _latest_strategy(store) == "progressive"
        store.close()

    async def test_small_response_passes_through_unchanged(self, tmp_path):
        """Content inside the default chunk size is a single chunk — returned
        as-is, with no footer."""
        mgr, store = _make_e2e_manager(tmp_path)
        small_text = "short answer."
        mgr._connections["srv"].session.call_tool.return_value = _make_result(small_text)

        result = await mgr.call_tool("srv", "tool", {})

        assert small_text in result
        assert "stm_proxy_read_more" not in result
        assert _latest_strategy(store) == "progressive"
        store.close()
