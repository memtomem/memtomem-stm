"""Tests for #618: blocking embedding compression is off-loaded from the event loop.

``EmbeddingScorer`` makes a synchronous httpx call inside ``score_sections``;
every consumer runs on the asyncio event loop. ``ProxyManager`` routes each
scorer-carrying sync ``compress()`` through ``_compress_maybe_offthread``,
which hops to a worker thread only when the configured scorer declares
``uses_blocking_io = True`` — the default BM25 path stays inline.

    uv run pytest tests/test_embedding_offload.py -v
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.relevance import BM25Scorer, EmbeddingScorer, create_scorer

# ── helpers ───────────────────────────────────────────────────────────


class _RecordingCompressor:
    """Records the thread each compress() call ran on."""

    def __init__(self, sleep_seconds: float = 0.0) -> None:
        self.sleep_seconds = sleep_seconds
        self.thread_ids: list[int] = []

    def compress(self, text: str, *, max_chars: int, context_query: str | None = None) -> str:
        self.thread_ids.append(threading.get_ident())
        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)
        return text[:max_chars]


class _BlockingScorer:
    """Minimal scorer that declares blocking I/O."""

    uses_blocking_io = True

    def score_sections(self, query: str, sections: list[tuple[str, str]]) -> list[float]:
        return [0.0] * len(sections)


class _BareScorer:
    """Scorer with no uses_blocking_io attribute at all."""

    def score_sections(self, query: str, sections: list[tuple[str, str]]) -> list[float]:
        return [0.0] * len(sections)


def _manager_with(scorer: object) -> SimpleNamespace:
    """A stub self for the helper: it only reads ``self._relevance_scorer``."""
    return SimpleNamespace(_relevance_scorer=scorer)


async def _run_helper(scorer: object, compressor: object, text: str = "x" * 200) -> str:
    return await ProxyManager._compress_maybe_offthread(
        _manager_with(scorer), compressor, text, max_chars=100, context_query="q"
    )


# ── uses_blocking_io flag values ──────────────────────────────────────


class TestUsesBlockingIoFlags:
    def test_bm25_is_inline(self):
        assert BM25Scorer.uses_blocking_io is False
        assert BM25Scorer().uses_blocking_io is False

    def test_embedding_ollama_is_blocking(self):
        assert EmbeddingScorer.uses_blocking_io is True
        assert EmbeddingScorer(provider="ollama").uses_blocking_io is True

    def test_embedding_openai_is_blocking(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert EmbeddingScorer(provider="openai").uses_blocking_io is True

    def test_bare_custom_scorer_defaults_to_inline(self):
        """Scorers that predate the attribute keep the pre-#618 inline path."""
        assert getattr(_BareScorer(), "uses_blocking_io", False) is False

    def test_create_scorer_variants(self):
        assert getattr(create_scorer("bm25"), "uses_blocking_io", False) is False
        assert getattr(create_scorer("embedding"), "uses_blocking_io", False) is True


# ── the helper's thread routing ───────────────────────────────────────


class TestCompressMaybeOffthread:
    async def test_inline_scorer_runs_on_caller_thread(self):
        compressor = _RecordingCompressor()
        await _run_helper(BM25Scorer(), compressor)
        assert compressor.thread_ids == [threading.get_ident()]

    async def test_bare_scorer_runs_on_caller_thread(self):
        compressor = _RecordingCompressor()
        await _run_helper(_BareScorer(), compressor)
        assert compressor.thread_ids == [threading.get_ident()]

    async def test_blocking_scorer_runs_on_worker_thread(self):
        compressor = _RecordingCompressor()
        await _run_helper(_BlockingScorer(), compressor)
        assert compressor.thread_ids != [threading.get_ident()]

    async def test_result_identical_on_both_paths(self):
        text = "y" * 300
        inline = await _run_helper(BM25Scorer(), _RecordingCompressor(), text)
        offthread = await _run_helper(_BlockingScorer(), _RecordingCompressor(), text)
        assert inline == offthread == text[:100]

    async def test_event_loop_stays_responsive_during_blocking_compress(self):
        """The issue's acceptance test: a slow compress must not stall the loop.

        Dual assertion per repo convention (absolute + relative): the call
        itself takes at least the blocked duration, while concurrent heartbeat
        gaps stay far below it — both in absolute ms and relative to the block.
        """
        block_seconds = 0.3
        ticks: list[float] = []

        async def heartbeat() -> None:
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)  # let the heartbeat start ticking
        t0 = time.monotonic()
        await _run_helper(_BlockingScorer(), _RecordingCompressor(sleep_seconds=block_seconds))
        elapsed = time.monotonic() - t0
        hb.cancel()

        assert elapsed >= block_seconds, "compress must still pay its own wall time"
        assert len(ticks) >= 3, "heartbeat never ran while compress was in flight"
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        assert max(gaps) < 0.15, f"loop stalled {max(gaps):.3f}s during off-thread compress"
        assert max(gaps) < 0.5 * block_seconds, "loop stall not far below the blocked duration"

    async def test_exception_type_preserved_through_to_thread(self):
        """_compress_and_surface's ``except sqlite3.Error`` guard must keep
        catching pending-store faults when compress runs on a worker thread."""

        class _FaultingCompressor:
            def compress(self, text: str, *, max_chars: int, context_query: str | None) -> str:
                raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.Error):
            await _run_helper(_BlockingScorer(), _FaultingCompressor())


# ── every scorer-carrying compress site routes through the helper ─────


class TestAllScorerSitesRouteThroughHelper:
    """Source-inspection pin (repo precedent: TestContextQueryPropagation):
    a future scorer-carrying compress() added without the gate would silently
    reintroduce the event-loop stall."""

    _RAW_SITE_RE = re.compile(r"scorer=self\._relevance_scorer\)\s*\.compress\(")

    def _source(self, method_name: str) -> str:
        return inspect.getsource(getattr(ProxyManager, method_name))

    @pytest.mark.parametrize(
        ("method_name", "expected_helper_calls"),
        [
            ("_apply_compression", 5),  # selective, no_config, truncate, schema, skeleton
            ("_apply_hybrid", 1),
            ("_compress_and_surface", 2),  # store-fault fallback, ratio-guard tier 3
        ],
    )
    def test_sites_use_helper(self, method_name: str, expected_helper_calls: int):
        src = self._source(method_name)
        assert not self._RAW_SITE_RE.search(src), (
            f"{method_name} calls a scorer-carrying compress() inline — route it "
            "through _compress_maybe_offthread (#618)"
        )
        actual = src.count("_compress_maybe_offthread(")
        assert actual == expected_helper_calls, (
            f"{method_name}: expected {expected_helper_calls} "
            f"_compress_maybe_offthread call(s), found {actual} — if a site was "
            "added or removed deliberately, update this pin"
        )


# ── fallback_count under concurrent worker threads ────────────────────


class TestFallbackCountThreadSafety:
    def test_fallback_count_exact_under_threads(self):
        """Off-loaded compressions may fail over to BM25 concurrently; the
        locked increment must not lose a count (the metrics boolean-delta
        would silently flip to False)."""
        scorer = EmbeddingScorer(provider="ollama", base_url="http://localhost:99999", timeout=0.5)
        threads_n, calls_per_thread = 8, 5
        sections = [("## t", "body")]

        def hammer() -> None:
            for _ in range(calls_per_thread):
                scorer.score_sections("query", sections)

        threads = [threading.Thread(target=hammer) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert scorer.fallback_count == threads_n * calls_per_thread
