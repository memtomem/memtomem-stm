"""Invariants for the manager-level compression retention ladder.

Pins three corrections, kept separate from the TruncateCompressor output
invariants (see test_truncate_output_invariants.py):

1. Heading detection in the hybrid-fallback gate uses the canonical markdown
   regex (shared with auto_select_strategy), not a naive ``count("\\n#")`` that
   missed an offset-0 heading and over-counted non-heading ``#``.
2. The truncate (tier-3) fallback re-checks its own ratio against the floor and
   logs distinctly when it lands below — symmetric with the hybrid tier, which
   gates on the floor.
3. ``context_query`` reaches the LLM-no-config and truncate-fallback
   compressor calls, not only the main TRUNCATE path.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.compression import count_markdown_headings
from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore


# ── 1. Canonical heading count ──────────────────────────────────────────


class TestCountMarkdownHeadings:
    def test_counts_offset_zero_heading(self) -> None:
        # Heading at position 0 (no leading newline) must be counted — the old
        # ``count("\n#")`` missed it.
        text = "# Title\n## A\n### B"
        assert count_markdown_headings(text) == 3
        assert text.count("\n#") == 2  # the bug the regex fixes

    def test_does_not_overcount_non_headings(self) -> None:
        # ``#`` not followed by whitespace (shebang, no-space comment, 7+ hashes)
        # is not a heading; the naive count would inflate these.
        text = "# Real heading\n#!/bin/bash\n#nospace\n####### too many"
        assert count_markdown_headings(text) == 1
        assert text.count("\n#") == 3  # naive overcount

    def test_empty(self) -> None:
        assert count_markdown_headings("") == 0
        assert count_markdown_headings("no headings here at all") == 0


# ── Ladder integration harness (mirrors test_fallback_ladder.py) ─────────


def _make_result(text: str):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], isError=False)


def _make_manager(
    tmp_path: Path, *, min_retention: float = 0.65, max_result_chars: int = 500
) -> tuple[ProxyManager, MetricsStore]:
    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=CompressionStrategy.TRUNCATE,
        max_result_chars=max_result_chars,
        max_retries=0,
        reconnect_delay_seconds=0.0,
    )
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": server_cfg},
        min_result_retention=min_retention,
    )
    mgr = ProxyManager(proxy_cfg, TokenTracker(metrics_store=store))
    mgr._connections["srv"] = UpstreamConnection(
        name="srv", config=server_cfg, session=AsyncMock(), tools=[]
    )
    return mgr, store


def _latest_strategy(store: MetricsStore) -> str:
    row = store._db.execute(
        "SELECT compression_strategy FROM proxy_metrics ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0]


# ── 2. Heading detection drives the ladder consistently ─────────────────


@pytest.mark.asyncio
class TestLadderHeadingDetection:
    async def test_offset_zero_heading_routes_to_hybrid(self, tmp_path) -> None:
        """A markdown doc whose first heading sits at offset 0, with exactly the
        3-heading minimum, now routes to the hybrid tier. The old
        ``count("\\n#")`` saw only 2 (missing the offset-0 heading) and fell
        through to truncate."""
        body = "Detail paragraph text. " * 22  # ~500 chars/section
        # First heading at offset 0 (no leading newline); 3 headings total.
        text = f"# Section 0\n\n{body}" + "".join(
            f"\n## Section {i}\n\n{body}" for i in range(1, 3)
        )
        assert count_markdown_headings(text) == 3
        assert text.count("\n#") == 2  # old gate would skip hybrid
        assert len(text) < 4000  # under progressive chunk_size

        mgr, store = _make_manager(tmp_path)
        mgr._connections["srv"].session.call_tool.return_value = _make_result(text)
        # Force the ratio guard to fire so the ladder runs.
        mgr._apply_compression = AsyncMock(return_value=("x" * 40, None))

        await mgr.call_tool("srv", "tool", {})

        assert "→hybrid_fallback" in _latest_strategy(store)
        store.close()


# ── 3. Truncate fallback re-checks its ratio against the floor ──────────


@pytest.mark.asyncio
class TestTruncateFallbackRatioRecheck:
    async def test_below_floor_truncate_logs_warning(self, tmp_path, caplog) -> None:
        """Repetitive content with a tail anomaly truncates to far below the
        budget, so the terminal truncate tier lands under the floor and must say
        so (symmetric with the hybrid tier's floor gate)."""
        # No headings (skip hybrid), under chunk_size (skip progressive),
        # highly repetitive with a single tail anomaly → tail-anomaly truncate
        # yields a tiny output relative to the cleaned length.
        text = "\n".join(f"row {i} identical-shape line aaaa" for i in range(100))
        text += "\nTOTALLY DIFFERENT ANOMALOUS TAIL LINE"
        assert len(text) < 4000

        mgr, store = _make_manager(tmp_path)
        mgr._connections["srv"].session.call_tool.return_value = _make_result(text)
        mgr._apply_compression = AsyncMock(return_value=("x" * 40, None))

        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.manager"):
            await mgr.call_tool("srv", "tool", {})

        assert "→truncate_fallback" in _latest_strategy(store)
        assert any("still below floor" in r.message for r in caplog.records), (
            "truncate fallback below the floor should log the distinct warning"
        )
        store.close()


# ── 4. context_query reaches the fallback compressor calls ──────────────


class TestContextQueryPropagation:
    """Shape-identical TruncateCompressor.compress call sites are hard to tell
    apart with a spy, so pin them by source inspection (repo precedent:
    test_both_apply_progressive_call_sites_pass_trace_id_kwarg)."""

    def test_llm_no_config_fallback_passes_context_query(self) -> None:
        src = inspect.getsource(ProxyManager._apply_compression)
        # The no-config branch returns a TruncateCompressor result tagged
        # "no_config"; it must thread context_query like the main TRUNCATE path.
        idx = src.index('"no_config"')
        window = src[max(0, idx - 400) : idx]
        assert "context_query=context_query" in window

    def test_truncate_fallback_passes_context_query(self) -> None:
        # The ratio-guard ladder (incl. the Tier-3 truncate fallback) lives in
        # ``_compress_and_surface`` since the A1 PR3 stage extraction.
        src = inspect.getsource(ProxyManager._compress_and_surface)
        idx = src.index("→truncate_fallback")
        window = src[max(0, idx - 400) : idx]
        assert "context_query=context_query" in window
