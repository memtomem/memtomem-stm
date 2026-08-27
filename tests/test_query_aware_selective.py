"""Query-aware wiring for SELECTIVE / Hybrid-TOC compression.

Closes the two gaps where ``context_query`` was threaded into the pipeline but
dropped before it reached the SELECTIVE compressor and the Hybrid TOC tail.
With a query the SELECTIVE table-of-contents now surfaces the most relevant
sections first; selection-by-key is unaffected. Propagation is pinned with
behavioral spies (not source inspection) per review feedback.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from memtomem_stm.proxy.compression import HybridCompressor, SelectiveCompressor
from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics import TokenTracker


def _doc() -> str:
    return (
        "## Authentication\n\n"
        + "JWT tokens and oauth login flows. " * 8
        + "\n\n## Billing\n\n"
        + "Invoices payments and subscription charges. " * 8
        + "\n\n## Logging\n\n"
        + "Log files rotation and verbosity levels. " * 8
    )


class TestSelectiveQueryAware:
    def test_default_preserves_insertion_order(self) -> None:
        toc = json.loads(SelectiveCompressor().compress(_doc(), max_chars=120))
        assert [e["key"] for e in toc["entries"]] == ["Authentication", "Billing", "Logging"]

    def test_query_surfaces_relevant_section_first(self) -> None:
        toc = json.loads(
            SelectiveCompressor().compress(
                _doc(), max_chars=120, context_query="billing invoice payment"
            )
        )
        assert toc["entries"][0]["key"] == "Billing"
        # All sections are still listed — ranking reorders, never drops.
        assert {e["key"] for e in toc["entries"]} == {"Authentication", "Billing", "Logging"}

    def test_select_by_key_unaffected_by_reorder(self) -> None:
        c = SelectiveCompressor()
        toc = json.loads(c.compress(_doc(), max_chars=120, context_query="billing"))
        key = toc["selection_key"]
        selected = c.select(key, ["Authentication"])
        assert "JWT tokens" in selected  # retrieval works regardless of TOC order

    def test_irrelevant_query_keeps_insertion_order(self) -> None:
        # No section scores against the query → original order preserved.
        toc = json.loads(
            SelectiveCompressor().compress(_doc(), max_chars=120, context_query="zzzzz qqqqq")
        )
        assert [e["key"] for e in toc["entries"]] == ["Authentication", "Billing", "Logging"]


class TestHybridForwardsQuery:
    def test_hybrid_forwards_context_query_to_selective(self) -> None:
        received: dict[str, object] = {}
        sel = SelectiveCompressor()
        original = sel.compress

        def spy(text, *, max_chars, context_query=None):
            received["context_query"] = context_query
            return original(text, max_chars=max_chars, context_query=context_query)

        sel.compress = spy  # type: ignore[method-assign]
        HybridCompressor(head_chars=120, selective_compressor=sel).compress(
            _doc(), max_chars=400, context_query="billing"
        )
        assert received["context_query"] == "billing"


@pytest.mark.asyncio
class TestManagerSelectiveForwardsQuery:
    async def test_apply_compression_selective_forwards_query(self, tmp_path) -> None:
        proxy_cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={
                "srv": UpstreamServerConfig(
                    prefix="test", compression=CompressionStrategy.SELECTIVE
                )
            },
        )
        mgr = ProxyManager(proxy_cfg, TokenTracker(metrics_store=None))

        captured: dict[str, object] = {}

        def spy_compress(text, *, max_chars, context_query=None):
            captured["context_query"] = context_query
            return "{}"

        # Pre-seed the cached compressor (cfg None matches the None passed below)
        # so _apply_compression reuses our spy instead of building a real one.
        mgr._selective_compressor = SimpleNamespace(compress=spy_compress)
        mgr._selective_compressor_cfg = None

        out, fallback = await mgr._apply_compression(
            "x" * 500,
            CompressionStrategy.SELECTIVE,
            100,
            None,  # sel_cfg
            None,  # llm_cfg
            None,  # hybrid_cfg
            "srv",
            "tool",
            context_query="billing invoice",
            cfg_snap=mgr._config,
        )
        assert fallback is None
        assert captured["context_query"] == "billing invoice"
