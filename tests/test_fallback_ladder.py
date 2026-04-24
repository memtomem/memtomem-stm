"""Tests for the enhanced 3-tier fallback ladder.

Covers:
- Hybrid fallback fires for structured content with enough headings
- Hybrid fallback skipped when content lacks headings (falls to truncate)
- Per-tool retention_floor overrides global dynamic scaling
- Three-tier cascade order: progressive → hybrid → truncate
- Metrics strategy labels for each tier
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    ToolOverrideConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore


def _text_content(text: str):
    return SimpleNamespace(type="text", text=text)


def _make_result(text: str):
    return SimpleNamespace(content=[_text_content(text)], isError=False)


def _make_manager(
    tmp_path: Path,
    *,
    min_retention: float = 0.65,
    compression: CompressionStrategy = CompressionStrategy.TRUNCATE,
    max_result_chars: int = 50000,
    retention_floor: float | None = None,
    tool_retention_floor: float | None = None,
) -> tuple[ProxyManager, MetricsStore]:
    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    tool_overrides = {}
    if tool_retention_floor is not None:
        tool_overrides["tool"] = ToolOverrideConfig(retention_floor=tool_retention_floor)
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=compression,
        max_result_chars=max_result_chars,
        max_retries=0,
        reconnect_delay_seconds=0.0,
        retention_floor=retention_floor,
        tool_overrides=tool_overrides,
    )
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": server_cfg},
        min_result_retention=min_retention,
    )
    tracker = TokenTracker(metrics_store=store)
    mgr = ProxyManager(proxy_cfg, tracker)
    session = AsyncMock()
    mgr._connections["srv"] = UpstreamConnection(
        name="srv",
        config=server_cfg,
        session=session,
        tools=[],
    )
    return mgr, store


def _latest_row(store: MetricsStore) -> dict:
    row = store._db.execute(
        "SELECT server, tool, cleaned_chars, compressed_chars, "
        "compression_strategy, ratio_violation "
        "FROM proxy_metrics ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "server": row[0],
        "tool": row[1],
        "cleaned_chars": row[2],
        "compressed_chars": row[3],
        "compression_strategy": row[4],
        "ratio_violation": row[5],
    }


def _markdown_with_headings(heading_count: int, body_chars: int = 500) -> str:
    """Generate markdown with the given number of headings."""
    body = "Detail text paragraph. " * (body_chars // 23 + 1)
    sections = [f"\n## Section {i}\n\n{body}" for i in range(heading_count)]
    return "".join(sections)


# ── Hybrid fallback tier ────────────────────────────────────────────────


@pytest.mark.asyncio
class TestHybridFallback:
    async def test_fires_for_structured_content(self, tmp_path):
        """When progressive can't fire (content too small) and content has
        >= 3 headings, hybrid fallback should produce a head+TOC result."""
        mgr, store = _make_manager(tmp_path, min_retention=0.65, max_result_chars=500)
        # ~3500 chars with 6 headings — too small for progressive (chunk_size=4000)
        # but has enough structure for hybrid.
        text = _markdown_with_headings(6, body_chars=400)
        assert len(text) < 4000  # must be under progressive chunk_size
        mgr._connections["srv"].session.call_tool.return_value = _make_result(text)
        mgr._apply_compression = AsyncMock(return_value=("x" * 50, None))

        await mgr.call_tool("srv", "tool", {})

        row = _latest_row(store)
        assert row["ratio_violation"] == 1
        assert "→hybrid_fallback" in row["compression_strategy"]
        store.close()

    async def test_skipped_without_headings(self, tmp_path):
        """Content without headings should skip hybrid and fall to truncate."""
        mgr, store = _make_manager(tmp_path, min_retention=0.65, max_result_chars=500)
        # Plain text, no markdown headings — ~3000 chars
        text = "No heading content. " * 150
        assert len(text) < 4000
        mgr._connections["srv"].session.call_tool.return_value = _make_result(text)
        mgr._apply_compression = AsyncMock(return_value=("x" * 50, None))

        await mgr.call_tool("srv", "tool", {})

        row = _latest_row(store)
        assert row["ratio_violation"] == 1
        assert "→truncate_fallback" in row["compression_strategy"]
        store.close()

    async def test_hybrid_fallback_still_below_floor_falls_to_truncate(self, tmp_path):
        """If hybrid compression output still violates the floor, fall to truncate."""
        mgr, store = _make_manager(tmp_path, min_retention=0.65, max_result_chars=500)
        text = _markdown_with_headings(6, body_chars=400)
        assert len(text) < 4000
        mgr._connections["srv"].session.call_tool.return_value = _make_result(text)
        mgr._apply_compression = AsyncMock(return_value=("x" * 50, None))
        # Force hybrid to also produce undersized output
        mgr._apply_hybrid = AsyncMock(return_value="y" * 50)

        await mgr.call_tool("srv", "tool", {})

        row = _latest_row(store)
        assert row["ratio_violation"] == 1
        assert "→truncate_fallback" in row["compression_strategy"]
        store.close()

    async def test_hybrid_fallback_exception_falls_to_truncate(self, tmp_path):
        """If hybrid raises an exception, fall to truncate gracefully."""
        mgr, store = _make_manager(tmp_path, min_retention=0.65, max_result_chars=500)
        text = _markdown_with_headings(6, body_chars=400)
        assert len(text) < 4000
        mgr._connections["srv"].session.call_tool.return_value = _make_result(text)
        mgr._apply_compression = AsyncMock(return_value=("x" * 50, None))
        mgr._apply_hybrid = AsyncMock(side_effect=RuntimeError("hybrid boom"))

        await mgr.call_tool("srv", "tool", {})

        row = _latest_row(store)
        assert row["ratio_violation"] == 1
        assert "→truncate_fallback" in row["compression_strategy"]
        store.close()


# ── Cascade order ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestCascadeOrder:
    async def test_progressive_has_priority_over_hybrid(self, tmp_path):
        """Large content with headings should use progressive (Tier 1),
        not hybrid (Tier 2)."""
        mgr, store = _make_manager(tmp_path, min_retention=0.65, max_result_chars=500)
        # ~12K chars with 10 headings — large enough for progressive
        text = _markdown_with_headings(10, body_chars=1000)
        assert len(text) > 4000
        mgr._connections["srv"].session.call_tool.return_value = _make_result(text)
        mgr._apply_compression = AsyncMock(return_value=("x" * 100, None))

        await mgr.call_tool("srv", "tool", {})

        row = _latest_row(store)
        assert row["ratio_violation"] == 1
        assert "→progressive_fallback" in row["compression_strategy"]
        store.close()


# ── Per-tool retention_floor ────────────────────────────────────────────


@pytest.mark.asyncio
class TestRetentionFloor:
    async def test_server_level_override(self, tmp_path):
        """Server-level retention_floor should replace the dynamic scaling."""
        mgr, store = _make_manager(
            tmp_path, min_retention=0.65, max_result_chars=500, retention_floor=0.5
        )
        # ~2000 chars: without override, dynamic would be 0.75 (< 3KB tier)
        text = "content word. " * 150
        mgr._connections["srv"].session.call_tool.return_value = _make_result(text)
        # Compress to 55% of cleaned — above 0.5 floor, below 0.75 default
        kept = int(len(text) * 0.55)
        mgr._apply_compression = AsyncMock(return_value=(text[:kept], None))

        await mgr.call_tool("srv", "tool", {})

        row = _latest_row(store)
        # With retention_floor=0.5, 55% should NOT be a violation
        assert row["ratio_violation"] == 0
        store.close()

    async def test_tool_level_override_takes_precedence(self, tmp_path):
        """Tool-level retention_floor should override server-level."""
        mgr, store = _make_manager(
            tmp_path,
            min_retention=0.65,
            max_result_chars=500,
            retention_floor=0.9,  # server says 90%
            tool_retention_floor=0.3,  # tool says 30%
        )
        text = "content word. " * 150
        mgr._connections["srv"].session.call_tool.return_value = _make_result(text)
        # Keep 35% — above tool floor (0.3) but below server floor (0.9)
        kept = int(len(text) * 0.35)
        mgr._apply_compression = AsyncMock(return_value=(text[:kept], None))

        await mgr.call_tool("srv", "tool", {})

        row = _latest_row(store)
        # Tool override (0.3) takes precedence, so 35% is not a violation
        assert row["ratio_violation"] == 0
        store.close()

    async def test_no_override_uses_dynamic_scaling(self, tmp_path):
        """Without retention_floor, dynamic scaling applies as before."""
        mgr, store = _make_manager(tmp_path, min_retention=0.65, max_result_chars=500)
        # ~2000 chars → dynamic = 0.75
        text = "content word. " * 150
        mgr._connections["srv"].session.call_tool.return_value = _make_result(text)
        # Keep 55% — below 0.75 dynamic
        kept = int(len(text) * 0.55)
        mgr._apply_compression = AsyncMock(return_value=(text[:kept], None))

        await mgr.call_tool("srv", "tool", {})

        row = _latest_row(store)
        # 55% < 0.75 dynamic → violation
        assert row["ratio_violation"] == 1
        store.close()
