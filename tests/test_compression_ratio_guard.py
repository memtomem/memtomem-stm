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

from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProgressiveConfig,
    ProxyConfig,
    SelectiveConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import CallMetrics, TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.proxy.pending_store import SQLitePendingStore


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
        assert "strategy_auto_selected" in cols
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
        assert "strategy_auto_selected" in cols
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
        assert "strategy_auto_selected" in cols
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
                strategy_auto_selected=True,
                ratio_violation=True,
            )
        )
        row = store._db.execute(
            "SELECT compression_strategy, ratio_violation, strategy_auto_selected "
            "FROM proxy_metrics"
        ).fetchone()
        assert row == ("truncate", 1, 1)
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
            "SELECT compression_strategy, ratio_violation, strategy_auto_selected "
            "FROM proxy_metrics"
        ).fetchone()
        # The provenance flag is tri-state: an unrecorded strategy has no
        # provenance either, and NULL must not read as "not AUTO" (#933).
        assert row == (None, 0, None)
        store.close()


# ── ProxyManager ratio guard ─────────────────────────────────────────────


def _text_content(text: str):
    return SimpleNamespace(type="text", text=text)


def _make_result(text: str):
    return SimpleNamespace(content=[_text_content(text)], is_error=False)


def _make_manager_with_store(
    tmp_path: Path,
    *,
    min_retention: float = 0.65,
    compression: CompressionStrategy = CompressionStrategy.TRUNCATE,
    max_result_chars: int = 50000,
    progressive: ProgressiveConfig | None = None,
    selective: SelectiveConfig | None = None,
    max_result_tokens: int | None = None,
    token_estimation_mode: str | None = None,
) -> tuple[ProxyManager, MetricsStore]:
    """Build a ProxyManager wired to a real MetricsStore so tests can read
    persisted rows directly — closer to production than summary dicts."""
    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    extra: dict = {}
    if max_result_tokens is not None:
        extra["max_result_tokens"] = max_result_tokens
    if token_estimation_mode is not None:
        extra["token_estimation_mode"] = token_estimation_mode
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=compression,
        max_result_chars=max_result_chars,
        max_retries=0,
        reconnect_delay_seconds=0.0,
        progressive=progressive,
        selective=selective,
        **extra,
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
        "compression_strategy, ratio_violation, strategy_auto_selected "
        "FROM proxy_metrics ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "server": row[0],
        "tool": row[1],
        "cleaned_chars": row[2],
        "compressed_chars": row[3],
        "compression_strategy": row[4],
        "ratio_violation": row[5],
        "strategy_auto_selected": row[6],
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

    async def test_a_pinned_strategy_is_not_recorded_as_auto_selected(self, tmp_path):
        """The label alone cannot say who chose it (#933).

        `truncate` is written identically whether AUTO landed on it or the
        config named it, so the provenance flag is the only thing that keeps
        a pinned call out of AUTO's observed behavior.
        """
        mgr, store = _make_manager_with_store(tmp_path)
        mgr._connections["srv"].session.call_tool.return_value = _make_result("ok")
        await mgr.call_tool("srv", "tool", {})
        row = _latest_row(store)
        assert row["compression_strategy"] == "truncate"
        assert row["strategy_auto_selected"] == 0
        store.close()

    async def test_auto_resolution_is_recorded_as_auto_selected(self, tmp_path):
        """A call the selector resolved records that it did (#933)."""
        mgr, store = _make_manager_with_store(
            tmp_path,
            compression=CompressionStrategy.AUTO,
            max_result_chars=100,
        )
        mgr._connections["srv"].session.call_tool.return_value = _make_result("x" * 5000)
        await mgr.call_tool("srv", "tool", {})
        row = _latest_row(store)
        # Which strategy AUTO picked is `auto_select_strategy`'s business; what
        # this pins is that the row says AUTO picked it, and that the recorded
        # label is a concrete strategy rather than "auto".
        assert row["compression_strategy"] != "auto"
        assert row["strategy_auto_selected"] == 1
        store.close()

    async def test_a_budget_short_circuit_under_auto_is_not_a_resolution(self, tmp_path):
        """The token gate can settle the strategy before the selector runs.

        With a unicode token budget the response fits, the branch sets NONE
        and the AUTO check below never fires. The config said `auto`, so a
        flag meaning "AUTO was configured" would read True here; it means
        "the selector ran", which it did not (#933).
        """
        mgr, store = _make_manager_with_store(
            tmp_path,
            compression=CompressionStrategy.AUTO,
            max_result_chars=10,
            max_result_tokens=100000,
            token_estimation_mode="unicode",
        )
        mgr._connections["srv"].session.call_tool.return_value = _make_result("short body")
        await mgr.call_tool("srv", "tool", {})
        row = _latest_row(store)
        assert row["compression_strategy"] == "none"
        assert row["strategy_auto_selected"] == 0
        store.close()

        # Positive control: the same call without the gate reaches the
        # selector, which also answers "none" — so the label proves nothing
        # and only the flag separates the two paths.
        mgr2, store2 = _make_manager_with_store(
            tmp_path / "control",
            compression=CompressionStrategy.AUTO,
            max_result_chars=10,
        )
        mgr2._connections["srv"].session.call_tool.return_value = _make_result("short body")
        await mgr2.call_tool("srv", "tool", {})
        control = _latest_row(store2)
        assert control["compression_strategy"] == "none"
        assert control["strategy_auto_selected"] == 1
        store2.close()

    async def test_a_degraded_auto_call_still_records_the_resolution(self, tmp_path):
        """The ladder rewrites the label; it does not undo the resolution.

        AUTO resolved this call, then the ratio guard degraded it and the
        label became `X→…_fallback`. The provenance flag tracks the selector,
        not the final label, so the call stays in AUTO's population (#933).
        """
        mgr, store = _make_manager_with_store(
            tmp_path,
            compression=CompressionStrategy.AUTO,
            max_result_chars=500,
        )
        text = "No heading content. " * 150
        mgr._connections["srv"].session.call_tool.return_value = _make_result(text)
        mgr._apply_compression = AsyncMock(return_value=("x" * 50, None))
        await mgr.call_tool("srv", "tool", {})
        row = _latest_row(store)
        assert row["ratio_violation"] == 1
        assert "_fallback" in row["compression_strategy"]
        assert row["strategy_auto_selected"] == 1
        store.close()

    async def test_progressive_strategy_call_completes_and_records_metric(self, tmp_path):
        """End-to-end PROGRESSIVE-strategy call returns a first chunk and
        records ``"progressive"`` in the metrics row.

        Pre-fix the PROGRESSIVE branch never assigned ``metrics_strategy``,
        so every call configured with this strategy died with
        UnboundLocalError at the metrics record — no end-to-end test drove
        the branch until now.
        """
        mgr, store = _make_manager_with_store(
            tmp_path,
            compression=CompressionStrategy.PROGRESSIVE,
            progressive=ProgressiveConfig(chunk_size=500),
        )
        large_text = "content paragraph. " * 200  # > chunk_size → chunked
        mgr._connections["srv"].session.call_tool.return_value = _make_result(large_text)

        result = await mgr.call_tool("srv", "tool", {})

        assert "stm_proxy_read_more" in result
        row = _latest_row(store)
        assert row["compression_strategy"] == "progressive"
        assert row["ratio_violation"] == 0
        # The PROGRESSIVE branch returns before the AUTO check, so no
        # resolution happened and the row must not claim one (#933).
        assert row["strategy_auto_selected"] == 0
        store.close()

    async def test_primary_progressive_store_failure_falls_back_to_passthrough(self, tmp_path):
        """When the *primary* PROGRESSIVE strategy fails to build/store its
        first chunk (e.g. a SQLite pending/reads-store I/O error inside
        ``_apply_progressive``), the call must degrade to a zero-loss
        passthrough of the full cleaned upstream content — not let the
        exception escape ``_call_tool_inner`` and discard an otherwise
        successful upstream response.

        Pre-fix only the ratio-guard *fallback* progressive call (Tier 1) was
        wrapped in try/except; the primary PROGRESSIVE branch was unguarded,
        so a store error there was recorded as INTERNAL_ERROR and the upstream
        response was thrown away. This regresses that asymmetry.
        """
        mgr, store = _make_manager_with_store(
            tmp_path,
            compression=CompressionStrategy.PROGRESSIVE,
            progressive=ProgressiveConfig(chunk_size=500),
        )
        large_text = "content paragraph. " * 200  # > chunk_size → would be chunked
        mgr._connections["srv"].session.call_tool.return_value = _make_result(large_text)
        # Force the primary progressive build/store to fail.
        mgr._apply_progressive = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("pending store full")
        )

        # Must NOT raise — the successful upstream response is preserved.
        result = await mgr.call_tool("srv", "tool", {})

        # Zero-loss passthrough: full content returned, and crucially NO
        # progressive footer (nothing was stored, so there is no key to read).
        assert "content paragraph." in result
        assert len(result) > 3000
        assert "stm_proxy_read_more" not in result

        row = _latest_row(store)
        assert row["compression_strategy"] == "progressive→passthrough_on_error"
        assert row["ratio_violation"] == 0
        # passthrough keeps the full cleaned content (compressed == cleaned)
        assert row["compressed_chars"] == row["cleaned_chars"]
        store.close()

    async def test_progressive_passthrough_on_error_is_not_cached(self, tmp_path):
        """A passthrough triggered by a *transient* progressive store failure
        must NOT be cached. Caching it would pin the degraded (non-chunked)
        full response for the cache TTL and suppress progressive delivery on
        identical calls even after the store recovers — so the next identical
        call must miss the cache, re-run the pipeline, and re-attempt
        progressive delivery.
        """
        cache = ProxyCache(tmp_path / "cache.db", max_entries=100)
        cache.initialize()
        mgr, store = _make_manager_with_store(
            tmp_path,
            compression=CompressionStrategy.PROGRESSIVE,
            progressive=ProgressiveConfig(chunk_size=500),
        )
        mgr._cache = cache
        large_text = "content paragraph. " * 200  # > chunk_size → would be chunked
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _make_result(large_text)

        # Call 1: the primary progressive store fails → passthrough degradation.
        mgr._apply_progressive = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("pending store full")
        )
        first = await mgr.call_tool("srv", "tool", {})
        assert "stm_proxy_read_more" not in first  # passthrough, no footer
        # The degraded passthrough must not have entered the cache.
        assert cache.stats()["total_entries"] == 0  # nothing stored under ANY key

        # Call 2: store recovered (real method restored). It must be a cache
        # MISS that re-runs upstream + progressive — not a replay of the
        # cached passthrough.
        del mgr._apply_progressive  # restore the real bound method
        second = await mgr.call_tool("srv", "tool", {})
        assert session.call_tool.call_count == 2  # cache miss → upstream re-called
        assert "stm_proxy_read_more" in second  # progressive delivery re-attempted
        cache.close()
        store.close()

    async def test_single_chunk_progressive_passthrough_is_still_cached(self, tmp_path):
        """The cache skip must apply ONLY to the error-degraded passthrough. A
        normal single-chunk passthrough (content fits one chunk) is a complete,
        key-free response and stays cacheable, so an identical second call is a
        cache hit that does not re-call upstream.
        """
        cache = ProxyCache(tmp_path / "cache.db", max_entries=100)
        cache.initialize()
        mgr, store = _make_manager_with_store(
            tmp_path,
            compression=CompressionStrategy.PROGRESSIVE,
            progressive=ProgressiveConfig(chunk_size=5000),
        )
        mgr._cache = cache
        small_text = "fits in one chunk. " * 10  # < chunk_size → single-chunk passthrough
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _make_result(small_text)

        first = await mgr.call_tool("srv", "tool", {})
        assert "stm_proxy_read_more" not in first  # single chunk, no footer
        await mgr.call_tool("srv", "tool", {})  # identical call → should hit cache
        assert session.call_tool.call_count == 1  # cache hit → upstream NOT re-called
        cache.close()
        store.close()

    async def test_selective_store_failure_falls_back_to_truncate(self, tmp_path):
        """A SELECTIVE/HYBRID pending-store write failure — a raw ``sqlite3``
        error out of ``SQLitePendingStore.put`` (the store has no error
        handling) — must degrade to a boundary-aware truncation, not escape
        ``_call_tool_inner`` and discard the successful upstream response as
        INTERNAL_ERROR (mirrors the PROGRESSIVE passthrough guard).
        """
        mgr, store = _make_manager_with_store(
            tmp_path,
            compression=CompressionStrategy.SELECTIVE,
            max_result_chars=600,
        )
        large_text = "# Doc\n\n" + "\n\n".join(
            f"## Section {i}\n" + ("word " * 40) for i in range(12)
        )
        mgr._connections["srv"].session.call_tool.return_value = _make_result(large_text)
        # The pending-store write raises a raw sqlite error (lock past busy
        # timeout / disk-full / corrupt DB) from inside the real SELECTIVE path.
        mgr._apply_compression = AsyncMock(
            side_effect=sqlite3.OperationalError("database is locked")
        )

        result = await mgr.call_tool("srv", "tool", {})  # must NOT raise

        # Degraded to plain truncation — no chunk-TOC selection key.
        assert '"selection_key"' not in result
        row = _latest_row(store)
        assert row["compression_strategy"] == "selective→truncate_on_store_error"
        store.close()

    async def test_selective_store_error_is_not_cached(self, tmp_path):
        """The truncate degradation from a *transient* store failure must NOT
        be cached: caching the lossy truncation would pin it for the TTL and
        suppress the chunk-TOC protocol on identical calls after the store
        recovers. The next identical call must miss, re-run upstream, and
        re-attempt the real SELECTIVE TOC.
        """
        cache = ProxyCache(tmp_path / "cache.db", max_entries=100)
        cache.initialize()
        mgr, store = _make_manager_with_store(
            tmp_path,
            compression=CompressionStrategy.SELECTIVE,
            max_result_chars=600,
        )
        mgr._cache = cache
        large_text = "# Doc\n\n" + "\n\n".join(
            f"## Section {i}\n" + ("word " * 40) for i in range(12)
        )
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _make_result(large_text)

        # Call 1: the pending-store write fails → truncate degradation.
        mgr._apply_compression = AsyncMock(
            side_effect=sqlite3.OperationalError("database is locked")
        )
        first = await mgr.call_tool("srv", "tool", {})
        assert '"selection_key"' not in first  # truncated, no TOC key
        assert cache.stats()["total_entries"] == 0  # degradation not cached under ANY key

        # Call 2: store recovered (real method restored). Cache MISS that
        # re-runs upstream + the real SELECTIVE TOC — not a cached truncation.
        del mgr._apply_compression  # restore the real bound method
        second = await mgr.call_tool("srv", "tool", {})
        assert session.call_tool.call_count == 2  # miss → upstream re-called
        assert '"selection_key"' in second  # real SELECTIVE TOC re-attempted
        cache.close()
        store.close()

    async def test_selective_sqlite_store_real_put_failure_degrades(self, tmp_path, monkeypatch):
        """End-to-end with the REAL sqlite pending backend: a raw sqlite3
        error out of ``SQLitePendingStore.put`` degrades to truncation.

        Unlike the mocked tests above, this drives the real
        ``_create_selective`` → ``SelectiveCompressor.compress`` →
        ``SQLitePendingStore.put`` path, proving the store fault actually
        surfaces as the ``sqlite3.Error`` the guard catches (the store has no
        error handling of its own).
        """
        sel = SelectiveConfig(
            pending_store="sqlite",
            pending_store_path=tmp_path / "pending.db",
        )
        mgr, store = _make_manager_with_store(
            tmp_path,
            compression=CompressionStrategy.SELECTIVE,
            max_result_chars=600,
            selective=sel,
        )
        large_text = "# Doc\n\n" + "\n\n".join(
            f"## Section {i}\n" + ("word " * 40) for i in range(12)
        )
        mgr._connections["srv"].session.call_tool.return_value = _make_result(large_text)

        def _boom(self, key, selection):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(SQLitePendingStore, "put", _boom)

        result = await mgr.call_tool("srv", "tool", {})  # must NOT raise

        assert '"selection_key"' not in result  # degraded to plain truncation
        row = _latest_row(store)
        assert row["compression_strategy"] == "selective→truncate_on_store_error"
        store.close()

    async def test_non_selective_sqlite_error_is_not_converted(self, tmp_path):
        """The store-fault degrade is scoped to SELECTIVE/HYBRID. A
        ``sqlite3.Error`` escaping any other strategy's compression must NOT be
        relabeled as a store degradation — it propagates to the INTERNAL_ERROR
        path unchanged (``call_tool`` records then re-raises). Guards against
        the over-broad ``except`` codex flagged.
        """
        mgr, store = _make_manager_with_store(
            tmp_path,
            compression=CompressionStrategy.TRUNCATE,
            max_result_chars=600,
        )
        mgr._connections["srv"].session.call_tool.return_value = _make_result("x" * 5000)
        mgr._apply_compression = AsyncMock(
            side_effect=sqlite3.OperationalError("database is locked")
        )

        with pytest.raises(sqlite3.OperationalError):
            await mgr.call_tool("srv", "tool", {})
        # Not degraded: no truncate_on_store_error row was recorded as success.
        row = _latest_row(store)
        assert row["compression_strategy"] != "selective→truncate_on_store_error"
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

    async def test_violation_triggers_progressive_fallback(self, tmp_path):
        """When the compressor overshoots, the ratio guard falls back to
        progressive delivery (zero-loss, Tier 1).  The strategy should
        record ``"{original}→progressive_fallback"``."""
        mgr, store = _make_manager_with_store(tmp_path, min_retention=0.65, max_result_chars=500)
        # ~15KB upstream → cleaned length >= 10000 → dynamic = 0.65
        large_text = "content paragraph. " * 800  # ~15200 chars
        mgr._connections["srv"].session.call_tool.return_value = _make_result(large_text)
        # Return something far below the retention floor
        mgr._apply_compression = AsyncMock(return_value=("x" * 100, None))

        result = await mgr.call_tool("srv", "tool", {})

        row = _latest_row(store)
        assert row["cleaned_chars"] > 10000
        assert row["ratio_violation"] == 1
        assert "→progressive_fallback" in row["compression_strategy"]
        # Progressive first chunk includes footer with read_more instruction
        assert "stm_proxy_read_more" in result
        assert "has_more=True" in result
        store.close()

    async def test_progressive_fallback_includes_ttl(self, tmp_path):
        """Progressive fallback footer must expose TTL so the agent knows
        how long the stored content remains available."""
        mgr, store = _make_manager_with_store(tmp_path, min_retention=0.65, max_result_chars=500)
        large_text = "content paragraph. " * 800
        mgr._connections["srv"].session.call_tool.return_value = _make_result(large_text)
        mgr._apply_compression = AsyncMock(return_value=("x" * 100, None))

        result = await mgr.call_tool("srv", "tool", {})

        # Default ProgressiveConfig.ttl_seconds = 1800
        assert "ttl=1800s" in result
        store.close()

    async def test_progressive_fallback_failure_falls_to_truncate(self, tmp_path):
        """When progressive fallback fails (Tier 1), the ratio guard must
        fall through to TruncateCompressor (Tier 2, guaranteed floor)."""
        mgr, store = _make_manager_with_store(tmp_path, min_retention=0.65, max_result_chars=500)
        large_text = "content paragraph. " * 800
        mgr._connections["srv"].session.call_tool.return_value = _make_result(large_text)
        mgr._apply_compression = AsyncMock(return_value=("x" * 100, None))
        # Force progressive to fail — truncate must catch it
        mgr._apply_progressive = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("store full"))

        await mgr.call_tool("srv", "tool", {})

        row = _latest_row(store)
        assert row["ratio_violation"] == 1
        assert "→truncate_fallback" in row["compression_strategy"]
        assert row["compressed_chars"] > 5000  # truncate keeps ~65% budget
        store.close()

    async def test_truncate_fallback_preserves_heading_boundaries(self, tmp_path):
        """Tier 2 truncate fallback should cut at heading boundaries
        rather than mid-sentence when the input is markdown."""
        mgr, store = _make_manager_with_store(tmp_path, min_retention=0.65, max_result_chars=500)
        sections = []
        for i in range(20):
            sections.append(f"\n## Section {i}\n\n{'Detail text paragraph. ' * 30}")
        markdown_text = "".join(sections)  # ~14K chars, 20 headings
        mgr._connections["srv"].session.call_tool.return_value = _make_result(markdown_text)
        mgr._apply_compression = AsyncMock(return_value=("x" * 50, None))
        # Force progressive to fail so truncate tier runs
        mgr._apply_progressive = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))

        result = await mgr.call_tool("srv", "tool", {})

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
        mgr._apply_compression = AsyncMock(return_value=(large_text[:kept], None))

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
        mgr._apply_compression = AsyncMock(return_value=("x" * 10, None))

        await mgr.call_tool("srv", "tool", {})

        row = _latest_row(store)
        assert row["ratio_violation"] == 0
        store.close()


# ── MetricsStore.get_tool_profiles ──────────────────────────────────────


class TestGetToolProfiles:
    def test_empty_store(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        assert store.get_tool_profiles() == []
        store.close()

    def test_basic_aggregation(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        for i in range(10):
            store.record(
                CallMetrics(
                    server="srv",
                    tool="t1",
                    original_chars=5000 + i * 100,
                    compressed_chars=3000,
                    cleaned_chars=5000 + i * 100,
                    compression_strategy="hybrid",
                    strategy_auto_selected=True,
                    ratio_violation=(i < 2),
                )
            )
        profiles = store.get_tool_profiles(since_seconds=3600.0)
        assert len(profiles) == 1
        p = profiles[0]
        assert p["server"] == "srv"
        assert p["tool"] == "t1"
        assert p["call_count"] == 10
        assert p["violation_count"] == 2
        assert p["auto_dominant_strategy"] == "hybrid"
        assert p["auto_dominant_strategy_count"] == 10
        assert p["auto_strategy_count"] == 10
        assert p["avg_ratio"] is not None
        assert 0 < p["avg_ratio"] < 1
        assert p["ratio_count"] == 10
        assert p["p95_original_chars"] >= 5800
        store.close()

    def test_ratio_count_counts_only_the_rows_avg_ratio_reads(self, tmp_path):
        """`avg_ratio`'s population is narrower than `call_count` (#934).

        The average is taken over rows with `cleaned_chars > 0 AND
        is_error = 0`; error rows and rows that recorded no cleaned length
        contribute nothing to it, so the tuner cannot label an average over
        one response from a count of twenty-five.
        """
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        store.record(
            CallMetrics(
                server="srv",
                tool="mixed",
                original_chars=2000,
                compressed_chars=1990,
                cleaned_chars=2000,
                compression_strategy="truncate",
            )
        )
        for _ in range(3):
            store.record(
                CallMetrics(
                    server="srv",
                    tool="mixed",
                    original_chars=2000,
                    compressed_chars=2000,
                    cleaned_chars=2000,
                    compression_strategy=None,
                    is_error=True,
                )
            )
        for _ in range(2):
            store.record(
                CallMetrics(
                    server="srv",
                    tool="mixed",
                    original_chars=2000,
                    compressed_chars=2000,
                    cleaned_chars=0,
                    compression_strategy="truncate",
                )
            )
        for _ in range(3):
            store.record(
                CallMetrics(
                    server="srv",
                    tool="errors_only",
                    original_chars=1000,
                    compressed_chars=1000,
                    cleaned_chars=1000,
                    compression_strategy=None,
                    is_error=True,
                )
            )
        by_tool = {p["tool"]: p for p in store.get_tool_profiles(since_seconds=3600.0)}

        mixed = by_tool["mixed"]
        assert mixed["call_count"] == 6
        assert mixed["ratio_count"] == 1
        assert mixed["avg_ratio"] == 0.995

        errors_only = by_tool["errors_only"]
        assert errors_only["call_count"] == 3
        assert errors_only["ratio_count"] == 0
        assert errors_only["avg_ratio"] is None
        store.close()

    def test_strategy_counts_exclude_calls_with_no_recorded_strategy(self, tmp_path):
        """The dominant count and its denominator share one population (#928).

        Both count only AUTO-resolved rows carrying a strategy, so their ratio
        is a share; `call_count` also includes the NULL-strategy rows error
        paths write.
        """
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        seeds = [("hybrid", 6), ("truncate", 4), (None, 2)]
        for strategy, count in seeds:
            for _ in range(count):
                store.record(
                    CallMetrics(
                        server="srv",
                        tool="mixed",
                        original_chars=1000,
                        compressed_chars=500,
                        cleaned_chars=1000,
                        compression_strategy=strategy,
                        strategy_auto_selected=strategy is not None,
                    )
                )
        for _ in range(3):
            store.record(
                CallMetrics(
                    server="srv",
                    tool="errors_only",
                    original_chars=1000,
                    compressed_chars=1000,
                    cleaned_chars=1000,
                    compression_strategy=None,
                )
            )
        by_tool = {p["tool"]: p for p in store.get_tool_profiles(since_seconds=3600.0)}

        mixed = by_tool["mixed"]
        assert mixed["call_count"] == 12
        assert mixed["auto_strategy_count"] == 10
        assert mixed["auto_dominant_strategy"] == "hybrid"
        assert mixed["auto_dominant_strategy_count"] == 6

        errors_only = by_tool["errors_only"]
        assert errors_only["call_count"] == 3
        assert errors_only["auto_strategy_count"] == 0
        assert errors_only["auto_dominant_strategy"] is None
        assert errors_only["auto_dominant_strategy_count"] == 0
        store.close()

    def test_an_auto_call_that_resolved_none_stays_in_the_denominator(self, tmp_path):
        """Resolving to `none` is a resolution, unlike recording no strategy.

        AUTO answering "this already fits" records the label `"none"` with
        provenance, so the call belongs in the population AUTO's consistency
        is measured over — a NULL strategy, which records no resolution at
        all, does not. The tuner declines to recommend `none` as a pin
        separately; that is a different gate from this count.
        """
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        for strategy, count in (("hybrid", 7), ("none", 3)):
            for _ in range(count):
                store.record(
                    CallMetrics(
                        server="srv",
                        tool="t1",
                        original_chars=1000,
                        compressed_chars=500,
                        cleaned_chars=1000,
                        compression_strategy=strategy,
                        strategy_auto_selected=True,
                    )
                )
        p = store.get_tool_profiles(since_seconds=3600.0)[0]
        assert p["auto_strategy_count"] == 10
        assert p["auto_dominant_strategy"] == "hybrid"
        assert p["auto_dominant_strategy_count"] == 7
        store.close()

    def test_auto_counts_exclude_pinned_and_unprovenanced_calls(self, tmp_path):
        """Only calls AUTO resolved can speak for what AUTO does (#933).

        The issue's input: nine calls made while the tool was pinned to
        `hybrid`, then one AUTO call resolving `truncate`. Counting the nine
        reports `hybrid` at 90% and pins a strategy AUTO has never chosen.
        A row written before the provenance column existed is unknown, not
        "not AUTO", and is excluded the same way.
        """
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        for _ in range(9):
            store.record(
                CallMetrics(
                    server="srv",
                    tool="switched",
                    original_chars=1000,
                    compressed_chars=500,
                    cleaned_chars=1000,
                    compression_strategy="hybrid",
                    strategy_auto_selected=False,
                )
            )
        store.record(
            CallMetrics(
                server="srv",
                tool="switched",
                original_chars=1000,
                compressed_chars=500,
                cleaned_chars=1000,
                compression_strategy="truncate",
                strategy_auto_selected=True,
            )
        )
        for _ in range(5):
            store.record(
                CallMetrics(
                    server="srv",
                    tool="legacy",
                    original_chars=1000,
                    compressed_chars=500,
                    cleaned_chars=1000,
                    compression_strategy="hybrid",
                )
            )
        by_tool = {p["tool"]: p for p in store.get_tool_profiles(since_seconds=3600.0)}

        switched = by_tool["switched"]
        assert switched["call_count"] == 10
        assert switched["auto_strategy_count"] == 1
        assert switched["auto_dominant_strategy"] == "truncate"
        assert switched["auto_dominant_strategy_count"] == 1

        legacy = by_tool["legacy"]
        assert legacy["call_count"] == 5
        assert legacy["auto_strategy_count"] == 0
        assert legacy["auto_dominant_strategy"] is None
        assert legacy["auto_dominant_strategy_count"] == 0
        store.close()

    def test_the_two_strategy_counts_come_from_one_snapshot(self, tmp_path):
        """A concurrent writer cannot pair a numerator with a stale denominator.

        The instance lock does not span processes, so a second proxy writing
        between two statements used to leave the dominant count larger than
        the population it was counted against — a share above 1.0.  Both now
        come from a single statement.  The injected write stands in for that
        other process: it lands after the main aggregation has been read.
        """
        import sqlite3 as _sq
        import time as _t

        db_path = tmp_path / "metrics.db"
        store = MetricsStore(db_path)
        store.initialize()
        for _ in range(3):
            store.record(
                CallMetrics(
                    server="srv",
                    tool="t1",
                    original_chars=1000,
                    compressed_chars=500,
                    cleaned_chars=1000,
                    compression_strategy="hybrid",
                    strategy_auto_selected=True,
                )
            )

        real_conn = store._db
        state = {"injected": False}

        class _InjectingConnection:
            """Delegates to the real connection, writing once from another one."""

            def __getattr__(self, name):
                return getattr(real_conn, name)

            def execute(self, sql, *args, **kwargs):
                cursor = real_conn.execute(sql, *args, **kwargs)
                if not state["injected"] and "GROUP BY server, tool" in sql:
                    state["injected"] = True
                    other = _sq.connect(str(db_path))
                    other.executemany(
                        "INSERT INTO proxy_metrics "
                        "(server, tool, original_chars, compressed_chars, cleaned_chars, "
                        " compression_strategy, strategy_auto_selected, source, "
                        " created_at, is_error, ratio_violation) "
                        "VALUES ('srv', 't1', 1000, 500, 1000, 'hybrid', 1, 'mcp', ?, 0, 0)",
                        [(_t.time(),) for _ in range(3)],
                    )
                    other.commit()
                    other.close()
                return cursor

        store._db = _InjectingConnection()  # type: ignore[assignment]
        try:
            p = store.get_tool_profiles(since_seconds=3600.0)[0]
        finally:
            store._db = real_conn

        assert state["injected"], "the probe never ran; the query shape changed"
        # call_count was read before the write and the strategy counts after,
        # which is what proves the two reads really straddle it — without this
        # the counts could agree simply by never having seen the new rows.
        assert p["call_count"] == 3
        assert p["auto_dominant_strategy_count"] == 6
        assert p["auto_strategy_count"] == 6
        store.close()

    def test_groups_by_server_tool(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        for tool in ("t1", "t2"):
            store.record(
                CallMetrics(
                    server="srv",
                    tool=tool,
                    original_chars=1000,
                    compressed_chars=500,
                    cleaned_chars=1000,
                    compression_strategy="truncate",
                )
            )
        profiles = store.get_tool_profiles(since_seconds=3600.0)
        tools = {p["tool"] for p in profiles}
        assert tools == {"t1", "t2"}
        store.close()

    def test_respects_time_window(self, tmp_path):
        """Rows outside the time window should be excluded."""
        import sqlite3 as _sq
        import time as _t

        db_path = tmp_path / "metrics.db"
        store = MetricsStore(db_path)
        store.initialize()
        store.record(
            CallMetrics(
                server="srv",
                tool="t1",
                original_chars=1000,
                compressed_chars=500,
                cleaned_chars=1000,
                compression_strategy="truncate",
            )
        )
        # Push the row 2 hours into the past
        conn = _sq.connect(str(db_path))
        conn.execute(
            "UPDATE proxy_metrics SET created_at = ?",
            (_t.time() - 7200,),
        )
        conn.commit()
        conn.close()
        # 1-hour window should miss the row
        assert store.get_tool_profiles(since_seconds=3600.0) == []
        store.close()
