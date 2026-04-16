"""Tests for ProxyManager pipeline methods — compression, surfacing, indexing, chunks, read_more."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ExtractionConfig,
    HybridConfig,
    LLMCompressorConfig,
    LLMProvider,
    ProxyConfig,
    SelectiveConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_manager(
    tmp_path: Path | None = None,
    compression: CompressionStrategy = CompressionStrategy.NONE,
    max_result_chars: int = 50000,
    **extra_proxy_kwargs: object,
) -> ProxyManager:
    """Create a ProxyManager with a mocked upstream connection."""
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=compression,
        max_result_chars=max_result_chars,
    )
    config_path = (tmp_path / "proxy.json") if tmp_path else Path("/tmp/proxy.json")
    proxy_cfg = ProxyConfig(
        config_path=config_path,
        upstream_servers={"srv": server_cfg},
    )
    tracker = TokenTracker()
    return ProxyManager(proxy_cfg, tracker, **extra_proxy_kwargs)


def _inject_connection(mgr: ProxyManager, text: str = "ok") -> AsyncMock:
    """Inject a mocked upstream connection returning *text*."""
    session = AsyncMock()
    session.call_tool.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        isError=False,
    )
    conn = UpstreamConnection(
        name="srv",
        config=UpstreamServerConfig(prefix="test"),
        session=session,
        tools=[],
    )
    mgr._connections["srv"] = conn
    return session


# ── _apply_compression ───────────────────────────────────────────────────


class TestApplyCompression:
    async def test_auto_short_text_noop(self, tmp_path):
        """AUTO resolves to NONE for text shorter than max_chars."""
        mgr = _make_manager(tmp_path=tmp_path, max_result_chars=1000)
        text = "Short text."
        result, fallback = await mgr._apply_compression(
            text,
            CompressionStrategy.AUTO,
            max_chars=1000,
            sel_cfg=None,
            llm_cfg=None,
            hybrid_cfg=None,
            server="srv",
            tool="t",
        )
        assert result == text
        assert fallback is None

    async def test_auto_long_text_compresses(self, tmp_path):
        """AUTO resolves to a concrete strategy for text exceeding max_chars."""
        mgr = _make_manager(tmp_path=tmp_path, max_result_chars=100)
        text = "x" * 500
        result, _ = await mgr._apply_compression(
            text,
            CompressionStrategy.AUTO,
            max_chars=100,
            sel_cfg=None,
            llm_cfg=None,
            hybrid_cfg=None,
            server="srv",
            tool="t",
        )
        assert len(result) <= len(text)

    async def test_hybrid_delegates(self, tmp_path):
        """HYBRID dispatches to _apply_hybrid."""
        mgr = _make_manager(tmp_path=tmp_path)
        with patch.object(
            mgr, "_apply_hybrid", new_callable=AsyncMock, return_value="hybrid-out"
        ) as mock_hybrid:
            result, _ = await mgr._apply_compression(
                "some text",
                CompressionStrategy.HYBRID,
                max_chars=5000,
                sel_cfg=None,
                llm_cfg=None,
                hybrid_cfg=HybridConfig(),
                server="srv",
                tool="t",
            )
        assert result == "hybrid-out"
        mock_hybrid.assert_awaited_once()

    async def test_selective_toc(self, tmp_path):
        """SELECTIVE returns TOC-format output."""
        mgr = _make_manager(tmp_path=tmp_path)
        text = "# Section A\nContent A\n\n# Section B\nContent B\n"
        result, _ = await mgr._apply_compression(
            text,
            CompressionStrategy.SELECTIVE,
            max_chars=50,
            sel_cfg=SelectiveConfig(),
            llm_cfg=None,
            hybrid_cfg=None,
            server="srv",
            tool="t",
        )
        # Selective compressor produces a TOC with a selection_key
        assert "selection_key" in result.lower() or "section" in result.lower()

    async def test_llm_no_config_fallback(self, tmp_path, caplog):
        """LLM_SUMMARY without LLM config falls back to truncate."""
        mgr = _make_manager(tmp_path=tmp_path)
        text = "x" * 200
        result, fallback = await mgr._apply_compression(
            text,
            CompressionStrategy.LLM_SUMMARY,
            max_chars=50,
            sel_cfg=None,
            llm_cfg=None,
            hybrid_cfg=None,
            server="srv",
            tool="t",
        )
        assert fallback == "no_config"
        assert len(result) <= 200
        assert "falling back to truncate" in caplog.text

    async def test_truncate_with_context_query(self, tmp_path):
        """TRUNCATE passes context_query to the compressor."""
        mgr = _make_manager(tmp_path=tmp_path)
        text = "important data " * 100
        result, _ = await mgr._apply_compression(
            text,
            CompressionStrategy.TRUNCATE,
            max_chars=100,
            sel_cfg=None,
            llm_cfg=None,
            hybrid_cfg=None,
            server="srv",
            tool="t",
            context_query="find important data",
        )
        assert len(result) <= len(text)


# ── LLMCompressor lifecycle (regression for #61) ────────────────────────


def _make_llm_instance_mock() -> MagicMock:
    """Return a MagicMock that stands in for an LLMCompressor instance."""
    inst = MagicMock()
    inst.compress = AsyncMock(return_value="compressed")
    inst.close = AsyncMock()
    inst.last_fallback = None
    return inst


class TestLLMCompressorLifecycle:
    async def test_singleton_reused_across_calls(self, tmp_path):
        """Repeated LLM_SUMMARY calls with the same config must reuse one instance."""
        mgr = _make_manager(tmp_path=tmp_path)
        cfg = LLMCompressorConfig(provider=LLMProvider.OPENAI, api_key="k")
        instance = _make_llm_instance_mock()

        with patch(
            "memtomem_stm.proxy.manager.LLMCompressor", return_value=instance
        ) as mock_cls:
            for _ in range(3):
                await mgr._apply_compression(
                    "x" * 500,
                    CompressionStrategy.LLM_SUMMARY,
                    max_chars=50,
                    sel_cfg=None,
                    llm_cfg=cfg,
                    hybrid_cfg=None,
                    server="srv",
                    tool="t",
                )

        mock_cls.assert_called_once_with(cfg)
        assert instance.compress.await_count == 3
        instance.close.assert_not_awaited()  # still live
        assert mgr._llm_compressor is instance

    async def test_recreated_and_old_closed_on_config_change(self, tmp_path):
        """Changing llm_cfg must close the previous compressor and create a new one."""
        mgr = _make_manager(tmp_path=tmp_path)
        cfg1 = LLMCompressorConfig(provider=LLMProvider.OPENAI, api_key="k1")
        cfg2 = LLMCompressorConfig(provider=LLMProvider.OPENAI, api_key="k2")

        inst1 = _make_llm_instance_mock()
        inst2 = _make_llm_instance_mock()

        with patch(
            "memtomem_stm.proxy.manager.LLMCompressor", side_effect=[inst1, inst2]
        ) as mock_cls:
            await mgr._apply_compression(
                "x" * 500,
                CompressionStrategy.LLM_SUMMARY,
                max_chars=50,
                sel_cfg=None,
                llm_cfg=cfg1,
                hybrid_cfg=None,
                server="srv",
                tool="t",
            )
            await mgr._apply_compression(
                "x" * 500,
                CompressionStrategy.LLM_SUMMARY,
                max_chars=50,
                sel_cfg=None,
                llm_cfg=cfg2,
                hybrid_cfg=None,
                server="srv",
                tool="t",
            )

        assert mock_cls.call_count == 2
        inst1.close.assert_awaited_once()
        inst2.close.assert_not_awaited()
        assert mgr._llm_compressor is inst2

    async def test_stop_closes_llm_compressor(self, tmp_path):
        """ProxyManager.stop() must close any cached LLM compressor."""
        mgr = _make_manager(tmp_path=tmp_path)
        cfg = LLMCompressorConfig(provider=LLMProvider.OPENAI, api_key="k")
        instance = _make_llm_instance_mock()

        with patch("memtomem_stm.proxy.manager.LLMCompressor", return_value=instance):
            await mgr._apply_compression(
                "x" * 500,
                CompressionStrategy.LLM_SUMMARY,
                max_chars=50,
                sel_cfg=None,
                llm_cfg=cfg,
                hybrid_cfg=None,
                server="srv",
                tool="t",
            )

        await mgr.stop()
        instance.close.assert_awaited_once()
        assert mgr._llm_compressor is None

    async def test_config_swap_does_not_tear_down_in_flight_old_instance(self, tmp_path):
        """Scenario A (regression): a compress() call holding the old
        LLMCompressor instance must not see its httpx client closed when a
        concurrent ``_apply_compression`` swaps in a new config.

        The pre-fix code called ``await old.close()`` inline under the lock,
        which tore the httpx client down while the first call was still
        awaiting ``self._client.post(...)`` — surfacing as ``ClosedError``
        / ``RuntimeError('stream has been closed')``.  The fix makes
        ``close()`` wait on an in-flight gate so the old instance stays
        usable until its in-flight caller drains.
        """
        import asyncio

        from memtomem_stm.proxy.compression import LLMCompressor

        mgr = _make_manager(tmp_path=tmp_path)
        cfg1 = LLMCompressorConfig(provider=LLMProvider.OPENAI, api_key="k1")
        cfg2 = LLMCompressorConfig(provider=LLMProvider.OPENAI, api_key="k2")

        release_first = asyncio.Event()
        first_started = asyncio.Event()
        first_client_was_alive_at_completion = False

        real_init = LLMCompressor.__init__
        instances: list[LLMCompressor] = []

        async def slow_first_call(text: str, *, max_chars: int) -> str:
            first_started.set()
            await release_first.wait()
            nonlocal first_client_was_alive_at_completion
            first_client_was_alive_at_completion = instances[0]._client is not None
            return "first-summary"

        async def fast_second_call(text: str, *, max_chars: int) -> str:
            return "second-summary"

        def tracked_init(self: LLMCompressor, config: LLMCompressorConfig) -> None:
            real_init(self, config)
            instances.append(self)
            # Route _call_api based on config identity so the second
            # instance (created during swap) gets the fast mock even though
            # it hasn't been returned from any patch() context yet.
            if config is cfg1:
                self._call_api = slow_first_call  # type: ignore[method-assign]
            else:
                self._call_api = fast_second_call  # type: ignore[method-assign]

        with patch.object(LLMCompressor, "__init__", tracked_init):
            # Task A: compress with cfg1. Creates instances[0] with a slow
            # _call_api that parks inside compress().
            task_a = asyncio.create_task(
                mgr._apply_compression(
                    "x" * 500,
                    CompressionStrategy.LLM_SUMMARY,
                    max_chars=50,
                    sel_cfg=None,
                    llm_cfg=cfg1,
                    hybrid_cfg=None,
                    server="srv",
                    tool="t",
                )
            )
            await first_started.wait()

            # Task B: compress with cfg2. Triggers swap whose close() must
            # wait for A to drain before aclose()ing the cfg1 client.
            task_b = asyncio.create_task(
                mgr._apply_compression(
                    "x" * 500,
                    CompressionStrategy.LLM_SUMMARY,
                    max_chars=50,
                    sel_cfg=None,
                    llm_cfg=cfg2,
                    hybrid_cfg=None,
                    server="srv",
                    tool="t",
                )
            )
            for _ in range(20):
                await asyncio.sleep(0)
            assert not task_a.done(), "A should still be in flight"
            assert not task_b.done(), "B's swap-close should be waiting on A"
            assert instances[0]._client is not None, (
                "cfg1 client aclose'd before A drained — fix not applied"
            )

            release_first.set()
            result_a, fb_a = await task_a
            result_b, fb_b = await task_b

        assert result_a == "first-summary"
        assert result_b == "second-summary"
        assert first_client_was_alive_at_completion, (
            "cfg1 client was closed while A's compress was still mid-call"
        )
        assert instances[0]._client is None, "cfg1 client should have been aclose'd"
        assert len(instances) == 2
        assert mgr._llm_compressor is instances[1]


# ── _apply_surfacing ─────────────────────────────────────────────────────


class TestApplySurfacing:
    async def test_no_engine_passthrough(self, tmp_path):
        """Without surfacing engine, text passes through unchanged."""
        mgr = _make_manager(tmp_path=tmp_path)
        mgr._surfacing_engine = None
        result = await mgr._apply_surfacing("srv", "t", {}, "original")
        assert result == "original"

    async def test_engine_called(self, tmp_path):
        """Surfacing engine.surface() is called with correct args."""
        mgr = _make_manager(tmp_path=tmp_path)
        mock_engine = AsyncMock()
        mock_engine.surface.return_value = "surfaced text"
        mgr._surfacing_engine = mock_engine

        result = await mgr._apply_surfacing("srv", "t", {"q": "x"}, "original")

        assert result == "surfaced text"
        mock_engine.surface.assert_awaited_once_with(
            server="srv", tool="t", arguments={"q": "x"}, response_text="original",
            trace_id=None,
        )

    async def test_engine_failure_returns_original(self, tmp_path, caplog):
        """If surfacing raises, original text is returned and warning logged."""
        mgr = _make_manager(tmp_path=tmp_path)
        mock_engine = AsyncMock()
        mock_engine.surface.side_effect = RuntimeError("boom")
        mgr._surfacing_engine = mock_engine

        result = await mgr._apply_surfacing("srv", "t", {}, "original")

        assert result == "original"
        assert "Surfacing failed" in caplog.text


# ── select_chunks ────────────────────────────────────────────────────────


class TestSelectChunks:
    def test_no_compressor(self, tmp_path):
        """Without selective compressor, returns a descriptive message."""
        mgr = _make_manager(tmp_path=tmp_path)
        mgr._selective_compressor = None
        result = mgr.select_chunks("key123", ["sec_a"])
        assert "not active" in result.lower()

    def test_delegates(self, tmp_path):
        """select_chunks delegates to the selective compressor."""
        mgr = _make_manager(tmp_path=tmp_path)
        mock_comp = MagicMock()
        mock_comp.select.return_value = "chunk content"
        mgr._selective_compressor = mock_comp

        result = mgr.select_chunks("key123", ["sec_a", "sec_b"])

        assert result == "chunk content"
        mock_comp.select.assert_called_once_with("key123", ["sec_a", "sec_b"])


# ── read_more ────────────────────────────────────────────────────────────


class TestReadMore:
    def test_key_not_found(self, tmp_path):
        """read_more with nonexistent key returns a 'not found' message."""
        mgr = _make_manager(tmp_path=tmp_path)
        result = mgr.read_more("nonexistent", 0)
        assert "not found" in result.lower()

    def test_valid_key(self, tmp_path):
        """read_more retrieves content from the progressive store."""
        from memtomem_stm.proxy.progressive import ProgressiveResponse
        import time

        mgr = _make_manager(tmp_path=tmp_path)
        store = mgr._get_progressive_store()
        resp = ProgressiveResponse(
            content="Hello world! " * 100,
            total_chars=1300,
            total_lines=1,
            content_type="text",
            structure_hint="",
            created_at=time.monotonic(),
            ttl_seconds=300.0,
        )
        store.put("testkey", resp)

        result = mgr.read_more("testkey", 0, 100)
        assert len(result) > 0

    def test_progressive_store_uses_sqlite_when_configured(self, tmp_path):
        """_get_progressive_store respects SelectiveConfig.pending_store='sqlite'."""
        from memtomem_stm.proxy.pending_store import SQLitePendingStore

        db_path = tmp_path / "progressive.db"
        sel_cfg = SelectiveConfig(pending_store="sqlite", pending_store_path=db_path)
        mgr = _make_manager(tmp_path=tmp_path)

        store = mgr._get_progressive_store(sel_cfg)
        assert isinstance(store._store, SQLitePendingStore)

    def test_progressive_store_defaults_to_memory(self, tmp_path):
        """Without sel_cfg, progressive store uses InMemoryPendingStore."""
        from memtomem_stm.proxy.pending_store import InMemoryPendingStore

        mgr = _make_manager(tmp_path=tmp_path)
        store = mgr._get_progressive_store()
        assert isinstance(store._store, InMemoryPendingStore)


class TestSelectiveHotReload:
    """Selective compressor must be recreated when config changes via hot-reload."""

    async def test_selective_recreated_on_config_change(self, tmp_path):
        from memtomem_stm.proxy.compression import SelectiveCompressor

        mgr = _make_manager(
            tmp_path=tmp_path, compression=CompressionStrategy.SELECTIVE
        )
        _inject_connection(mgr, "section1\n---\nsection2\n---\nsection3")

        sel_cfg_a = SelectiveConfig(json_depth=1)
        sel_cfg_b = SelectiveConfig(json_depth=3)

        async with mgr._selective_lock:
            mgr._selective_compressor = mgr._create_selective(sel_cfg_a)
            mgr._selective_compressor_cfg = sel_cfg_a
        comp_a = mgr._selective_compressor

        async with mgr._selective_lock:
            if mgr._selective_compressor_cfg != sel_cfg_b:
                mgr._selective_compressor = mgr._create_selective(sel_cfg_b)
                mgr._selective_compressor_cfg = sel_cfg_b
        comp_b = mgr._selective_compressor

        assert comp_a is not comp_b
        assert mgr._selective_compressor_cfg == sel_cfg_b

    async def test_selective_not_recreated_when_config_same(self, tmp_path):
        mgr = _make_manager(
            tmp_path=tmp_path, compression=CompressionStrategy.SELECTIVE
        )
        sel_cfg = SelectiveConfig(json_depth=2)

        async with mgr._selective_lock:
            mgr._selective_compressor = mgr._create_selective(sel_cfg)
            mgr._selective_compressor_cfg = sel_cfg
        comp_first = mgr._selective_compressor

        async with mgr._selective_lock:
            if mgr._selective_compressor_cfg != sel_cfg:
                mgr._selective_compressor = mgr._create_selective(sel_cfg)
                mgr._selective_compressor_cfg = sel_cfg
        comp_second = mgr._selective_compressor

        assert comp_first is comp_second


# ── _auto_index_response ─────────────────────────────────────────────────


class TestAutoIndex:
    async def test_writes_file_and_indexes(self, tmp_path):
        """_auto_index_response writes a .md file and calls index_engine."""
        mock_indexer = AsyncMock()
        mock_indexer.index_file.return_value = SimpleNamespace(indexed_chunks=3)

        mgr = _make_manager(tmp_path=tmp_path, index_engine=mock_indexer)
        # Override auto_index config to use tmp_path
        from memtomem_stm.proxy.config import AutoIndexConfig  # noqa: F811

        with patch.object(
            type(mgr),
            "_config",
            new_callable=lambda: property(
                lambda self: ProxyConfig(
                    config_path=tmp_path / "proxy.json",
                    upstream_servers={"srv": UpstreamServerConfig(prefix="test")},
                    auto_index=AutoIndexConfig(enabled=True, memory_dir=tmp_path / "index"),
                )
            ),
        ):
            result = await mgr._auto_index_response(
                server="srv",
                tool="t",
                arguments={"q": "test"},
                text="Full content here",
                agent_summary="Summary",
                compression_strategy="truncate",
                original_chars=500,
                compressed_chars=100,
            )

        assert "[Indexed]" in result
        assert "3 chunks" in result
        mock_indexer.index_file.assert_awaited_once()

    async def test_auto_index_failure_returns_surfaced(self, tmp_path, caplog):
        """When _auto_index_response raises, call_tool returns the surfaced
        response — optional stages must not kill the agent response."""
        from memtomem_stm.proxy.config import AutoIndexConfig

        mock_indexer = AsyncMock()
        proxy_cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={"srv": UpstreamServerConfig(prefix="test")},
            auto_index=AutoIndexConfig(enabled=True, min_chars=10, memory_dir=tmp_path / "idx"),
        )
        tracker = TokenTracker()
        mgr = ProxyManager(proxy_cfg, tracker, index_engine=mock_indexer)

        response_text = "upstream content " * 20
        _inject_connection(mgr, text=response_text)

        with patch.object(
            mgr, "_auto_index_response", new_callable=AsyncMock,
            side_effect=OSError("disk full"),
        ):
            result = await mgr.call_tool("srv", "some_tool", {})

        assert isinstance(result, str)
        assert "upstream content" in result
        assert "Auto-index failed" in caplog.text


# ── _extract_and_store ────────────────────────────────────────────────────


class TestExtractAndStore:
    async def test_dedup_skips_duplicate(self, tmp_path):
        """Duplicate facts are skipped when dedup is enabled."""
        mock_indexer = AsyncMock()
        mock_indexer.is_duplicate.return_value = True
        mock_indexer.index_file = AsyncMock()

        from memtomem_stm.proxy.extraction import ExtractedFact

        mock_extractor = AsyncMock()
        mock_extractor.extract.return_value = [
            ExtractedFact(content="Fact 1", category="technical", confidence=0.9),
        ]

        mgr = _make_manager(tmp_path=tmp_path, index_engine=mock_indexer)
        mgr._extractor = mock_extractor

        with patch.object(
            type(mgr),
            "_config",
            new_callable=lambda: property(
                lambda self: ProxyConfig(
                    config_path=tmp_path / "proxy.json",
                    upstream_servers={"srv": UpstreamServerConfig(prefix="test")},
                    extraction=ExtractionConfig(
                        enabled=True,
                        memory_dir=tmp_path / "facts",
                        dedup_threshold=0.9,
                    ),
                )
            ),
        ):
            await mgr._extract_and_store("srv", "t", {}, "Some long response text")

        # index_file should NOT be called because the fact was a duplicate
        mock_indexer.index_file.assert_not_awaited()


# ── get_upstream_health ───────────────────────────────────────────────────


class TestGetUpstreamHealth:
    def test_returns_per_server_health(self):
        """get_upstream_health returns connection status for each server."""
        mgr = _make_manager()
        _inject_connection(mgr)

        health = mgr.get_upstream_health()

        assert "srv" in health
        assert health["srv"]["connected"] is True
        assert health["srv"]["tools"] == 0
