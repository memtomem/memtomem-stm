"""Tests for ProxyManager — ToolConfig resolution and pipeline logic."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.config import (
    AutoIndexConfig,
    CleaningConfig,
    CompressionStrategy,
    ExtractionConfig,
    ExtractionStrategy,
    LLMCompressorConfig,
    LLMProvider,
    ProxyConfig,
    RelevanceScorerConfig,
    SelectiveConfig,
    ToolOverrideConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, ToolConfig
from memtomem_stm.proxy.metrics import TokenTracker


# ── ToolConfig resolution tests ──────────────────────────────────────────
# These test the _resolve_tool_config logic by constructing ToolConfig manually
# from the same cascading rules the ProxyManager uses.


def _resolve(server_cfg: UpstreamServerConfig, tool: str, global_auto_index: bool = False):
    """Replicate _resolve_tool_config logic without needing a full ProxyManager."""
    compression = server_cfg.compression
    max_chars = server_cfg.max_result_chars
    llm_cfg = server_cfg.llm
    sel_cfg = server_cfg.selective
    hybrid_cfg = server_cfg.hybrid
    cleaning_cfg = server_cfg.cleaning or CleaningConfig()
    auto_index_enabled = global_auto_index
    if server_cfg.auto_index is not None:
        auto_index_enabled = server_cfg.auto_index

    override = server_cfg.tool_overrides.get(tool)
    if override is not None:
        if override.compression is not None:
            compression = override.compression
        if override.max_result_chars is not None:
            max_chars = override.max_result_chars
        if override.llm is not None:
            llm_cfg = override.llm
        if override.selective is not None:
            sel_cfg = override.selective
        if override.hybrid is not None:
            hybrid_cfg = override.hybrid
        if override.cleaning is not None:
            cleaning_cfg = override.cleaning
        if override.auto_index is not None:
            auto_index_enabled = override.auto_index

    return ToolConfig(
        compression=compression,
        max_chars=max_chars,
        llm=llm_cfg,
        auto_index_enabled=auto_index_enabled,
        selective=sel_cfg,
        cleaning=cleaning_cfg,
        hybrid=hybrid_cfg,
    )


class TestToolConfigResolution:
    def test_server_defaults(self):
        cfg = UpstreamServerConfig(prefix="gh", compression=CompressionStrategy.HYBRID)
        tc = _resolve(cfg, "list_repos")
        assert tc.compression == CompressionStrategy.HYBRID
        assert tc.max_chars == 8000  # UpstreamServerConfig default
        assert tc.cleaning.enabled is True

    def test_tool_override_compression(self):
        cfg = UpstreamServerConfig(
            prefix="gh",
            compression=CompressionStrategy.HYBRID,
            tool_overrides={
                "search_code": ToolOverrideConfig(compression=CompressionStrategy.TRUNCATE)
            },
        )
        tc_default = _resolve(cfg, "list_repos")
        tc_override = _resolve(cfg, "search_code")
        assert tc_default.compression == CompressionStrategy.HYBRID
        assert tc_override.compression == CompressionStrategy.TRUNCATE

    def test_tool_override_max_chars(self):
        cfg = UpstreamServerConfig(
            prefix="gh",
            max_result_chars=2000,
            tool_overrides={
                "get_file": ToolOverrideConfig(max_result_chars=50000)
            },
        )
        tc = _resolve(cfg, "get_file")
        assert tc.max_chars == 50000

    def test_tool_override_cleaning(self):
        cfg = UpstreamServerConfig(
            prefix="gh",
            tool_overrides={
                "raw_output": ToolOverrideConfig(cleaning=CleaningConfig(strip_html=False))
            },
        )
        tc = _resolve(cfg, "raw_output")
        assert tc.cleaning.strip_html is False

    def test_tool_override_auto_index(self):
        cfg = UpstreamServerConfig(
            prefix="gh",
            auto_index=False,
            tool_overrides={
                "search_code": ToolOverrideConfig(auto_index=True)
            },
        )
        tc_default = _resolve(cfg, "list_repos")
        tc_override = _resolve(cfg, "search_code")
        assert tc_default.auto_index_enabled is False
        assert tc_override.auto_index_enabled is True

    def test_global_auto_index_default(self):
        cfg = UpstreamServerConfig(prefix="gh")
        tc = _resolve(cfg, "tool", global_auto_index=True)
        assert tc.auto_index_enabled is True

    def test_server_auto_index_overrides_global(self):
        cfg = UpstreamServerConfig(prefix="gh", auto_index=False)
        tc = _resolve(cfg, "tool", global_auto_index=True)
        assert tc.auto_index_enabled is False

    def test_no_override_preserves_server_config(self):
        sel = SelectiveConfig(max_pending=50)
        cfg = UpstreamServerConfig(
            prefix="gh",
            selective=sel,
            compression=CompressionStrategy.SELECTIVE,
        )
        tc = _resolve(cfg, "any_tool")
        assert tc.selective is sel
        assert tc.selective.max_pending == 50


# ── Auto-index startup warning tests ────────────────────────────────────
# See also: ``tests/test_docs_sync.py::test_bundled_server_proxy_manager_omits_index_engine``
# and ``::test_configuration_md_stage4_inert_note_pinned`` — paired docs/code
# drift guards for the bundled ``mms`` server's engine-less construction (#299).


class TestInactiveIndexConfigNoStartupWarning:
    @pytest.mark.asyncio
    async def test_no_compression_auto_index_warning(self, caplog):
        """Compression active + auto_index disabled → startup warning."""
        config = ProxyConfig(
            enabled=True,
            auto_index=AutoIndexConfig(enabled=False),
            upstream_servers={
                "docs": UpstreamServerConfig(
                    prefix="dc",
                    compression=CompressionStrategy.HYBRID,
                    command="echo",
                ),
            },
        )
        mgr = ProxyManager(config, TokenTracker())
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            try:
                await mgr.start()
            except (Exception, asyncio.CancelledError):
                # The "echo" upstream exits before completing the JSON-RPC
                # handshake, which the MCP SDK's stdio reader can surface as
                # asyncio.CancelledError (BaseException, not Exception). The
                # warning we're asserting fires before that connect failure,
                # so swallowing both connection-error shapes is safe.
                pass
        assert not any(
            "compressed-away content is permanently lost" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_no_global_auto_index_missing_engine_warning(self, caplog, tmp_path):
        """auto_index.enabled=true but index_engine=None → startup warning."""
        config = ProxyConfig(
            enabled=True,
            config_path=tmp_path / "missing-proxy.json",
            auto_index=AutoIndexConfig(enabled=True),
        )
        mgr = ProxyManager(config, TokenTracker())  # index_engine defaults to None
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            await mgr.start()
        assert not any(
            "no index engine configured" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_no_global_extraction_missing_engine_warning(self, caplog, tmp_path):
        """extraction.enabled=true but index_engine=None → startup warning (#288)."""
        config = ProxyConfig(
            enabled=True,
            config_path=tmp_path / "missing-proxy.json",
            extraction=ExtractionConfig(enabled=True),
        )
        mgr = ProxyManager(config, TokenTracker())  # index_engine defaults to None
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            await mgr.start()
        assert not any(
            "no index engine configured" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_no_per_server_auto_index_missing_engine_warning(self, caplog):
        """Per-upstream auto_index=true with no engine → warning names the server (#288)."""
        config = ProxyConfig(
            enabled=True,
            upstream_servers={
                "github": UpstreamServerConfig(
                    prefix="gh",
                    command="echo",
                    auto_index=True,
                ),
            },
        )
        mgr = ProxyManager(config, TokenTracker())
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            try:
                await mgr.start()
            except (Exception, asyncio.CancelledError):
                # "echo" upstream exits before completing the JSON-RPC
                # handshake; the warning fires before that connect failure.
                pass
        assert not any(
            "no index engine configured" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_no_per_tool_extraction_missing_engine_warning(self, caplog):
        """Per-tool-override extraction=true with no engine → warning names server+tool (#288)."""
        config = ProxyConfig(
            enabled=True,
            upstream_servers={
                "github": UpstreamServerConfig(
                    prefix="gh",
                    command="echo",
                    tool_overrides={
                        "search_code": ToolOverrideConfig(extraction=True),
                    },
                ),
            },
        )
        mgr = ProxyManager(config, TokenTracker())
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            try:
                await mgr.start()
            except (Exception, asyncio.CancelledError):
                pass
        assert not any(
            "no index engine configured" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_no_warning_when_compression_none(self, caplog):
        """compression=none + auto_index disabled → no warning."""
        config = ProxyConfig(
            enabled=True,
            auto_index=AutoIndexConfig(enabled=False),
            upstream_servers={
                "pass": UpstreamServerConfig(
                    prefix="ps",
                    compression=CompressionStrategy.NONE,
                    command="echo",
                ),
            },
        )
        mgr = ProxyManager(config, TokenTracker())
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            try:
                await mgr.start()
            except (Exception, asyncio.CancelledError):
                # MCP SDK can surface upstream exit as CancelledError
                # (BaseException); safe to swallow — the warning we assert
                # on fires before the connect loop runs.
                pass
        assert not any("permanently lost" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_injected_engine_runs_index_and_extract_stages(self):
        config = ProxyConfig(
            enabled=True,
            auto_index=AutoIndexConfig(enabled=True, min_chars=1, background=False),
            extraction=ExtractionConfig(enabled=True, min_response_chars=1, background=False),
        )
        engine = object()
        mgr = ProxyManager(config, TokenTracker(), index_engine=engine)
        mgr._auto_index_response = AsyncMock(
            return_value=SimpleNamespace(summary="indexed", ok=True, chunks_indexed=1, error=None)
        )
        mgr._extract_and_store = AsyncMock(
            return_value=SimpleNamespace(ok=True, facts_stored=1, error=None)
        )
        tc = replace(
            _resolve(UpstreamServerConfig(prefix="gh", auto_index=True), "search_code"),
            extraction_enabled=True,
        )

        await mgr._run_index_stage(
            server="github",
            tool="search_code",
            upstream_args={},
            tc=tc,
            cfg_snap=config,
            cleaned="payload",
            original_text="payload",
            surfaced="summary",
            compressed_chars_for_metrics=7,
            context_query=None,
        )
        await mgr._run_extract_stage(
            server="github",
            tool="search_code",
            upstream_args={},
            tc=tc,
            cfg_snap=config,
            cleaned="payload",
            context_query=None,
        )

        assert mgr.index_engine is engine
        mgr._auto_index_response.assert_awaited_once()
        mgr._extract_and_store.assert_awaited_once()


# ── Privacy-scan-disabled startup warning tests (#610) ──────────────────
# When an LLM path (compression / extraction) is enabled with
# ``privacy_scan_enabled=false`` toward an EXTERNAL provider, raw upstream
# responses leave the machine without credential redaction (#289). Warn loudly
# at startup — but stay silent for local Ollama (never leaves the machine).


class TestPrivacyScanStartupWarning:
    @pytest.mark.asyncio
    async def test_warns_llm_compression_scan_off_external(self, caplog):
        """compression=llm_summary + privacy_scan_enabled=false + OpenAI → warning."""
        config = ProxyConfig(
            enabled=True,
            upstream_servers={
                "docs": UpstreamServerConfig(
                    prefix="dc",
                    command="echo",
                    compression=CompressionStrategy.LLM_SUMMARY,
                    llm=LLMCompressorConfig(
                        provider=LLMProvider.OPENAI,
                        api_key="sk-test",
                        privacy_scan_enabled=False,
                    ),
                ),
            },
        )
        mgr = ProxyManager(config, TokenTracker())
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            try:
                await mgr.start()
            except (Exception, asyncio.CancelledError):
                # "echo" upstream exits before the JSON-RPC handshake; the
                # warning fires before that connect failure.
                pass
        assert any(
            "LLM compression enabled" in r.message
            and "server 'docs'" in r.message
            and "privacy_scan_enabled=false" in r.message
            and "UNSCANNED" in r.message
            and "openai" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_warns_llm_extraction_scan_off_external(self, caplog, tmp_path):
        """extraction (LLM, scan off, external) with an index engine → warning."""
        config = ProxyConfig(
            enabled=True,
            config_path=tmp_path / "missing-proxy.json",
            extraction=ExtractionConfig(
                enabled=True,
                strategy=ExtractionStrategy.LLM,
                llm=LLMCompressorConfig(
                    provider=LLMProvider.ANTHROPIC,
                    api_key="ant-test",
                    privacy_scan_enabled=False,
                ),
            ),
        )
        # Extraction only reaches the provider when an index engine is wired
        # (Stage-4b gate); a sentinel is enough to exercise the warning path.
        mgr = ProxyManager(config, TokenTracker(), index_engine=object())
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            await mgr.start()
        assert any(
            "LLM extraction enabled" in r.message
            and "extraction.enabled" in r.message
            and "UNSCANNED" in r.message
            and "anthropic" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_no_extraction_warning_without_index_engine(self, caplog, tmp_path):
        """Same scan-off external extraction, but no index engine → no leak, no
        warning (extraction never runs in the bundled ``mms`` server)."""
        config = ProxyConfig(
            enabled=True,
            config_path=tmp_path / "missing-proxy.json",
            extraction=ExtractionConfig(
                enabled=True,
                strategy=ExtractionStrategy.LLM,
                llm=LLMCompressorConfig(
                    provider=LLMProvider.OPENAI,
                    api_key="sk-test",
                    privacy_scan_enabled=False,
                ),
            ),
        )
        mgr = ProxyManager(config, TokenTracker())  # index_engine=None
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            await mgr.start()
        assert not any("LLM extraction enabled" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_warning_for_local_ollama_scan_off(self, caplog, tmp_path):
        """Scan off but destination is local Ollama → no warning (never leaves box)."""
        config = ProxyConfig(
            enabled=True,
            config_path=tmp_path / "missing-proxy.json",
            extraction=ExtractionConfig(
                enabled=True,
                strategy=ExtractionStrategy.LLM,
                llm=LLMCompressorConfig(
                    provider=LLMProvider.OLLAMA,
                    base_url="http://localhost:11434",
                    privacy_scan_enabled=False,
                ),
            ),
        )
        mgr = ProxyManager(config, TokenTracker(), index_engine=object())
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            await mgr.start()
        assert not any("UNSCANNED" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_warning_when_scan_enabled(self, caplog):
        """privacy_scan_enabled=true (default) + external LLM → no warning."""
        config = ProxyConfig(
            enabled=True,
            upstream_servers={
                "docs": UpstreamServerConfig(
                    prefix="dc",
                    command="echo",
                    compression=CompressionStrategy.LLM_SUMMARY,
                    llm=LLMCompressorConfig(
                        provider=LLMProvider.OPENAI,
                        api_key="sk-test",
                        # privacy_scan_enabled defaults to True
                    ),
                ),
            },
        )
        mgr = ProxyManager(config, TokenTracker())
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            try:
                await mgr.start()
            except (Exception, asyncio.CancelledError):
                pass
        assert not any("UNSCANNED" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_warning_for_auto_compression(self, caplog):
        """compression=auto (not explicit llm_summary) → not statically flagged."""
        config = ProxyConfig(
            enabled=True,
            upstream_servers={
                "docs": UpstreamServerConfig(
                    prefix="dc",
                    command="echo",
                    compression=CompressionStrategy.AUTO,
                    llm=LLMCompressorConfig(
                        provider=LLMProvider.OPENAI,
                        api_key="sk-test",
                        privacy_scan_enabled=False,
                    ),
                ),
            },
        )
        mgr = ProxyManager(config, TokenTracker())
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            try:
                await mgr.start()
            except (Exception, asyncio.CancelledError):
                pass
        assert not any("LLM compression enabled" in r.message for r in caplog.records)


class TestToolConfigFrozen:
    def test_toolconfig_is_immutable(self):
        tc = ToolConfig(
            compression=CompressionStrategy.NONE,
            max_chars=1000,
            llm=None,
            auto_index_enabled=False,
            selective=None,
            cleaning=CleaningConfig(),
            hybrid=None,
        )
        with pytest.raises(AttributeError):
            tc.compression = CompressionStrategy.TRUNCATE


# ── RelevanceScorer hot-reload (regression for #62) ─────────────────────


class TestRelevanceScorerHotReload:
    def _make_manager(self, scorer_cfg: RelevanceScorerConfig, tmp_path) -> ProxyManager:
        cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={},
            relevance_scorer=scorer_cfg,
        )
        return ProxyManager(cfg, TokenTracker())

    def test_cached_when_config_unchanged(self, tmp_path):
        """Repeated access with no config change returns the same scorer instance."""
        mgr = self._make_manager(RelevanceScorerConfig(scorer="bm25"), tmp_path)
        first = mgr._relevance_scorer
        second = mgr._relevance_scorer
        assert first is second

    def test_recreated_when_scorer_type_changes(self, tmp_path):
        """Hot-reloading the scorer type must rebuild the instance."""
        mgr = self._make_manager(RelevanceScorerConfig(scorer="bm25"), tmp_path)
        before = mgr._relevance_scorer

        new_cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={},
            relevance_scorer=RelevanceScorerConfig(
                scorer="embedding",
                embedding_provider="ollama",
                embedding_model="nomic-embed-text",
            ),
        )
        mgr._config_loader.seed(new_cfg)

        after = mgr._relevance_scorer
        assert after is not before

    def test_recreated_when_embedding_model_changes(self, tmp_path):
        """Changing the embedding model on an embedding scorer must rebuild."""
        mgr = self._make_manager(
            RelevanceScorerConfig(scorer="embedding", embedding_model="nomic-embed-text"),
            tmp_path,
        )
        before = mgr._relevance_scorer

        new_cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={},
            relevance_scorer=RelevanceScorerConfig(
                scorer="embedding",
                embedding_model="mxbai-embed-large",
            ),
        )
        mgr._config_loader.seed(new_cfg)

        after = mgr._relevance_scorer
        assert after is not before

    def test_not_recreated_when_unrelated_field_changes(self, tmp_path):
        """Changing unrelated proxy config (e.g. upstream_servers) must not rebuild the scorer."""
        scorer_cfg = RelevanceScorerConfig(scorer="bm25")
        mgr = self._make_manager(scorer_cfg, tmp_path)
        before = mgr._relevance_scorer

        new_cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={"srv": UpstreamServerConfig(prefix="test")},
            relevance_scorer=scorer_cfg,
        )
        mgr._config_loader.seed(new_cfg)

        after = mgr._relevance_scorer
        assert after is before
