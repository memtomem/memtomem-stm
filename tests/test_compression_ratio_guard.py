"""Tests for the compression ratio guard (P0-2).

Covers:
- CallMetrics defaults for the new compression_strategy / ratio_violation fields
- MetricsStore schema migration for the two new columns (fresh + legacy DB)
- MetricsStore.record persistence of the new fields
- ProxyManager.call_tool integration: AUTO resolution is recorded, and the
  ratio guard flags calls where the compressor cut below the dynamic
  min_result_retention floor.
"""

from __future__ import annotations

import sqlite3
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
from memtomem_stm.proxy.metrics import CallMetrics, TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore


# ── CallMetrics compression fields ───────────────────────────────────────


class TestCallMetricsCompressionFields:
    def test_defaults(self):
        m = CallMetrics(server="s", tool="t", original_chars=100, compressed_chars=50)
        assert m.compression_strategy is None
        assert m.ratio_violation is False

    def test_explicit_values(self):
        m = CallMetrics(
            server="s",
            tool="t",
            original_chars=100,
            compressed_chars=50,
            compression_strategy="truncate",
            ratio_violation=True,
        )
        assert m.compression_strategy == "truncate"
        assert m.ratio_violation is True


# ── MetricsStore migration ───────────────────────────────────────────────


class TestMetricsStoreCompressionMigration:
    def test_fresh_db_has_compression_columns(self, tmp_path):
        store = MetricsStore(tmp_path / "fresh.db")
        store.initialize()
        cols = {row[1] for row in store._db.execute("PRAGMA table_info(proxy_metrics)")}
        assert "compression_strategy" in cols
        assert "ratio_violation" in cols
        store.close()

    def test_legacy_db_gets_migrated(self, tmp_path):
        """Pre-existing DB without the new columns should be upgraded."""
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE proxy_metrics ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "server TEXT NOT NULL, tool TEXT NOT NULL, "
            "original_chars INTEGER NOT NULL, compressed_chars INTEGER NOT NULL, "
            "cleaned_chars INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)"
        )
        conn.commit()
        conn.close()

        store = MetricsStore(db_path)
        store.initialize()
        cols = {row[1] for row in store._db.execute("PRAGMA table_info(proxy_metrics)")}
        assert "compression_strategy" in cols
        assert "ratio_violation" in cols
        store.close()

    def test_migration_is_idempotent(self, tmp_path):
        """Running initialize twice must not fail or duplicate columns."""
        db_path = tmp_path / "idempotent.db"
        store = MetricsStore(db_path)
        store.initialize()
        store.close()

        # Second open on the already-migrated DB should be a no-op.
        store2 = MetricsStore(db_path)
        store2.initialize()
        cols = {row[1] for row in store2._db.execute("PRAGMA table_info(proxy_metrics)")}
        assert "compression_strategy" in cols
        assert "ratio_violation" in cols
        store2.close()

    def test_record_persists_compression_fields(self, tmp_path):
        store = MetricsStore(tmp_path / "record.db")
        store.initialize()
        store.record(
            CallMetrics(
                server="srv",
                tool="tool",
                original_chars=10000,
                compressed_chars=500,
                cleaned_chars=10000,
                compression_strategy="truncate",
                ratio_violation=True,
            )
        )
        row = store._db.execute(
            "SELECT compression_strategy, ratio_violation FROM proxy_metrics"
        ).fetchone()
        assert row == ("truncate", 1)
        store.close()

    def test_record_success_defaults(self, tmp_path):
        """A call recorded without the new fields should default to NULL / 0."""
        store = MetricsStore(tmp_path / "defaults.db")
        store.initialize()
        store.record(
            CallMetrics(
                server="srv",
                tool="tool",
                original_chars=100,
                compressed_chars=100,
                cleaned_chars=100,
            )
        )
        row = store._db.execute(
            "SELECT compression_strategy, ratio_violation FROM proxy_metrics"
        ).fetchone()
        assert row == (None, 0)
        store.close()


# ── ProxyManager ratio guard ─────────────────────────────────────────────


def _text_content(text: str):
    return SimpleNamespace(type="text", text=text)


def _make_result(text: str):
    return SimpleNamespace(content=[_text_content(text)], isError=False)


def _make_manager_with_store(
    tmp_path: Path,
    *,
    min_retention: float = 0.65,
    compression: CompressionStrategy = CompressionStrategy.TRUNCATE,
    max_result_chars: int = 50000,
) -> tuple[ProxyManager, MetricsStore]:
    """Build a ProxyManager wired to a real MetricsStore so tests can read
    persisted rows directly — closer to production than summary dicts."""
    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=compression,
        max_result_chars=max_result_chars,
        max_retries=0,
        reconnect_delay_seconds=0.0,
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


@pytest.mark.asyncio
class TestProxyManagerRatioGuard:
    async def test_records_effective_strategy(self, tmp_path):
        """Calls that pass compression should record the concrete strategy."""
        mgr, store = _make_manager_with_store(tmp_path)
        mgr._connections["srv"].session.call_tool.return_value = _make_result("ok")
        await mgr.call_tool("srv", "tool", {})
        row = _latest_row(store)
        assert row["compression_strategy"] == "truncate"
        assert row["ratio_violation"] == 0
        store.close()

    async def test_auto_is_resolved_before_metrics(self, tmp_path):
        """AUTO should be resolved to a concrete strategy before recording.

        A tiny response fits the budget, so auto_select_strategy returns
        NONE — that is what the metrics row should reflect, not 'auto'.
        """
        mgr, store = _make_manager_with_store(tmp_path, compression=CompressionStrategy.AUTO)
        mgr._connections["srv"].session.call_tool.return_value = _make_result("small response")
        await mgr.call_tool("srv", "tool", {})
        row = _latest_row(store)
        assert row["compression_strategy"] == "none"
        assert row["ratio_violation"] == 0
        store.close()

    async def test_violation_triggers_truncate_fallback(self, tmp_path):
        """When the compressor overshoots, the ratio guard falls back to
        boundary-aware TruncateCompressor at the effective budget.

        The fallback output should contain substantially more content
        than the original compression, and the strategy should record
        the ``"{original}→truncate_fallback"`` transition so SQL queries
        can track fallback frequency."""
        mgr, store = _make_manager_with_store(tmp_path, min_retention=0.65, max_result_chars=500)
        # ~15KB upstream → cleaned length >= 10000 → dynamic = 0.65
        large_text = "content paragraph. " * 800  # ~15200 chars
        mgr._connections["srv"].session.call_tool.return_value = _make_result(large_text)
        # Return something far below the retention floor
        mgr._apply_compression = AsyncMock(return_value="x" * 100)

        await mgr.call_tool("srv", "tool", {})

        row = _latest_row(store)
        assert row["cleaned_chars"] > 10000
        # Fallback should produce substantially more than the 100-char stub.
        # effective_max_chars = max(500, ~15200 * 0.65) ≈ 9880
        assert row["compressed_chars"] > 5000
        assert row["ratio_violation"] == 1
        assert "→truncate_fallback" in row["compression_strategy"]
        store.close()

    async def test_fallback_preserves_heading_boundaries(self, tmp_path):
        """Fallback via TruncateCompressor should cut at heading
        boundaries rather than mid-sentence when the input is markdown."""
        mgr, store = _make_manager_with_store(tmp_path, min_retention=0.65, max_result_chars=500)
        sections = []
        for i in range(20):
            sections.append(f"\n## Section {i}\n\n{'Detail text paragraph. ' * 30}")
        markdown_text = "".join(sections)  # ~14K chars, 20 headings
        mgr._connections["srv"].session.call_tool.return_value = _make_result(markdown_text)
        mgr._apply_compression = AsyncMock(return_value="x" * 50)

        result = await mgr.call_tool("srv", "tool", {})

        # The returned text should contain heading markers — TruncateCompressor
        # cuts at section boundaries and appends remaining section titles.
        assert "## Section" in result
        row = _latest_row(store)
        assert "→truncate_fallback" in row["compression_strategy"]
        assert row["ratio_violation"] == 1
        store.close()

    async def test_no_violation_when_compressor_respects_budget(self, tmp_path):
        """Compressor staying within the dynamic floor should not trip
        the guard."""
        mgr, store = _make_manager_with_store(tmp_path, min_retention=0.65, max_result_chars=50000)
        large_text = "content paragraph. " * 800  # ~15200 chars
        mgr._connections["srv"].session.call_tool.return_value = _make_result(large_text)
        # Simulate a compressor that keeps ~80% of the content — above the
        # 0.65 floor, so no violation should fire.
        kept = int(len(large_text) * 0.8)
        mgr._apply_compression = AsyncMock(return_value=large_text[:kept])

        await mgr.call_tool("srv", "tool", {})

        row = _latest_row(store)
        assert row["ratio_violation"] == 0
        store.close()

    async def test_min_retention_zero_disables_guard(self, tmp_path):
        """min_result_retention=0 means the operator opted out of the floor;
        the guard must not flag anything, even for extreme compression."""
        mgr, store = _make_manager_with_store(tmp_path, min_retention=0.0, max_result_chars=500)
        large_text = "content paragraph. " * 800
        mgr._connections["srv"].session.call_tool.return_value = _make_result(large_text)
        mgr._apply_compression = AsyncMock(return_value="x" * 10)

        await mgr.call_tool("srv", "tool", {})

        row = _latest_row(store)
        assert row["ratio_violation"] == 0
        store.close()
