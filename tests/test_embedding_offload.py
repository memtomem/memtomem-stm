"""Tests for #618: blocking embedding compression is off-loaded from the event loop.

``EmbeddingScorer`` makes a synchronous httpx call inside ``score_sections``;
every consumer runs on the asyncio event loop. ``ProxyManager`` routes each
scorer-carrying sync ``compress()`` through ``_compress_maybe_offthread``,
which hops to a worker thread only when the scorer the compressor actually
captured declares ``uses_blocking_io = True`` — the default BM25 path stays
inline.

    uv run pytest tests/test_embedding_offload.py -v
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sqlite3
import threading
from types import MethodType, SimpleNamespace

import pytest

from memtomem_stm.proxy.config import CompressionStrategy
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.relevance import BM25Scorer, EmbeddingScorer, create_scorer

# ── helpers ───────────────────────────────────────────────────────────


class _RecordingCompressor:
    """Records the thread each compress() call ran on."""

    def __init__(self) -> None:
        self.thread_ids: list[int] = []

    def compress(self, text: str, *, max_chars: int, context_query: str | None = None) -> str:
        self.thread_ids.append(threading.get_ident())
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


async def _run_helper(scorer: object, compressor: object, text: str = "x" * 200) -> str:
    # The helper gates on the explicitly passed scorer (the instance the
    # compressor captured), never on manager state — a bare stub self suffices.
    return await ProxyManager._compress_maybe_offthread(
        SimpleNamespace(), compressor, text, max_chars=100, context_query="q", scorer=scorer
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
        """The issue's acceptance test: a blocked compress must not stall the loop.

        Deterministic (no wall-clock bounds, so no flakiness on loaded CI):
        the fake compress blocks on a gate event in the worker thread, and the
        loop provably keeps running — it completes several sleeps and only
        then releases the gate — while the compress is guaranteed in flight.
        On the pre-#618 inline path not a single tick could run until
        compress returned, so the in-flight assertions fail.
        """
        started = threading.Event()
        release = threading.Event()

        class _GatedCompressor:
            def compress(self, text: str, *, max_chars: int, context_query: str | None) -> str:
                started.set()
                assert release.wait(timeout=10), "test gate never released"
                return text[:max_chars]

        task = asyncio.create_task(_run_helper(_BlockingScorer(), _GatedCompressor()))
        try:
            while not started.is_set():  # the loop is alive while compress starts
                await asyncio.sleep(0.001)
            ticks = 0
            for _ in range(5):  # the loop is alive while compress is blocked
                await asyncio.sleep(0.001)
                ticks += 1
            assert ticks == 5
            assert not task.done(), "compress finished before the gate was released?"
        finally:
            release.set()
        assert await task == "x" * 100

    async def test_exception_type_preserved_through_to_thread(self):
        """_compress_and_surface's ``except sqlite3.Error`` guard must keep
        catching pending-store faults when compress runs on a worker thread."""

        class _FaultingCompressor:
            def compress(self, text: str, *, max_chars: int, context_query: str | None) -> str:
                raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.Error):
            await _run_helper(_BlockingScorer(), _FaultingCompressor())


# ── SELECTIVE pins the compressor instead of holding the lock ─────────


class _GatedSelectiveFake:
    """Duck-typed selective compressor: gated compress + use-pin counters."""

    _scorer = _BlockingScorer()

    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.started = threading.Event()
        self.entered = 0
        self._entered_lock = threading.Lock()
        self.begin_calls = 0
        self.end_calls = 0

    def begin_use(self) -> None:
        self.begin_calls += 1

    def end_use(self) -> None:
        self.end_calls += 1

    def compress(self, text: str, *, max_chars: int, context_query: str | None = None) -> str:
        with self._entered_lock:
            self.entered += 1
        self.started.set()
        assert self.release.wait(timeout=10), "test gate never released"
        return text[:max_chars]


def _selective_stub(compressor: object, lock_timeout: float = 5.0) -> SimpleNamespace:
    stub = SimpleNamespace(
        _selective_lock=asyncio.Lock(),
        _config=SimpleNamespace(lock_timeout_seconds=lock_timeout),
        _selective_compressor=compressor,
        _selective_compressor_cfg=None,
    )
    stub._compress_maybe_offthread = MethodType(ProxyManager._compress_maybe_offthread, stub)
    return stub


def _selective_call(stub: SimpleNamespace, text: str = "z" * 300):
    return ProxyManager._apply_compression(
        stub,
        text,
        CompressionStrategy.SELECTIVE,
        100,
        None,
        None,
        None,
        "srv",
        "tool",
        context_query="q",
        cfg_snap=stub._config,
    )


class TestSelectivePinsCompressorNotLock:
    async def test_lock_released_and_compressor_pinned_during_compress(self):
        """The SELECTIVE branch must pin the compressor (begin_use under the
        lock) and release ``_selective_lock`` before the off-thread compress:
        holding the lock across a slow embedding compress turns normal
        backpressure into spurious LOCK_TIMEOUT errors (codex round 2 of
        #628), while pinning defers a concurrent rebuild's close() until the
        last in-flight user drains."""
        release = threading.Event()
        comp = _GatedSelectiveFake(release)
        stub = _selective_stub(comp)

        task = asyncio.create_task(_selective_call(stub))
        try:
            while not comp.started.is_set():
                await asyncio.sleep(0.001)
            assert comp.begin_calls == 1, "compressor must be pinned before compress starts"
            assert comp.end_calls == 0
            assert not stub._selective_lock.locked(), (
                "_selective_lock must be released during the off-thread "
                "compress — holding it serializes concurrent SELECTIVE calls "
                "into LOCK_TIMEOUT failures behind a slow embedding endpoint"
            )
        finally:
            release.set()
        compressed, llm_fallback = await task
        assert compressed == "z" * 100
        assert llm_fallback is None
        assert comp.end_calls == 1, "the pin must be balanced after compress"

    async def test_concurrent_selective_calls_run_in_parallel(self):
        """codex round-2 regression: N concurrent slow SELECTIVE calls must
        all be in flight simultaneously — with the lock held across compress,
        entry would cap at 1 and later waiters would hit LockTimeoutError at
        the (deliberately tiny) timeout below."""
        release = threading.Event()
        comp = _GatedSelectiveFake(release)
        stub = _selective_stub(comp, lock_timeout=0.5)

        tasks = [asyncio.create_task(_selective_call(stub)) for _ in range(4)]
        try:
            deadline = asyncio.get_running_loop().time() + 5.0
            while comp.entered < 4:
                assert asyncio.get_running_loop().time() < deadline, (
                    f"only {comp.entered}/4 compress calls in flight — "
                    "concurrent SELECTIVE calls are serializing"
                )
                await asyncio.sleep(0.001)
        finally:
            release.set()
        results = await asyncio.gather(*tasks)
        assert all(r == ("z" * 100, None) for r in results)
        assert comp.begin_calls == 4
        assert comp.end_calls == 4


class TestSelectiveDeferredClose:
    def _sqlite_compressor(self, tmp_path):
        from memtomem_stm.proxy.compression import SelectiveCompressor
        from memtomem_stm.proxy.pending_store import SQLitePendingStore

        store = SQLitePendingStore(tmp_path / "pending.db")
        store.initialize()
        return SelectiveCompressor(store=store), store

    def test_close_deferred_while_in_use(self, tmp_path):
        """close() during an in-flight use span must leave the store usable
        (a worker thread may be mid-put) and apply on the last end_use()."""
        comp, store = self._sqlite_compressor(tmp_path)
        comp.begin_use()
        comp.close()
        assert store._db is not None, "store closed under an in-flight compress"
        # The store still works mid-span — this is the mid-put scenario.
        assert comp.compress("## A\n" + "a" * 300, max_chars=100) is not None
        comp.end_use()
        assert store._db is None, "deferred close never applied"

    def test_close_immediate_when_idle(self, tmp_path):
        comp, store = self._sqlite_compressor(tmp_path)
        comp.close()
        assert store._db is None

    def test_end_use_without_close_keeps_store_open(self, tmp_path):
        comp, store = self._sqlite_compressor(tmp_path)
        comp.begin_use()
        comp.end_use()
        assert store._db is not None
        comp.close()
        assert store._db is None


# ── every scorer-carrying compress site routes through the helper ─────


class TestAllScorerSitesRouteThroughHelper:
    """Source-inspection pin (repo precedent: TestContextQueryPropagation):
    a future scorer-carrying compress() added without the gate would silently
    reintroduce the event-loop stall."""

    _RAW_SITE_RE = re.compile(r"scorer=(?:self\._relevance_scorer|_?\w*scorer)\)\s*\.compress\(")

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

    def test_selective_and_hybrid_gate_on_captured_scorer(self):
        """The gate must read the scorer the (possibly cached) compressor
        actually holds, not a fresh ``self._relevance_scorer``: after a
        scorer-only hot-reload the cached selective compressor keeps its old
        scorer until the next rebuild, and gating on the property would run an
        embedding-backed compress inline again (codex review of #628)."""
        pin = 'scorer=getattr(sel_compressor, "_scorer", None)'
        assert pin in self._source("_apply_compression")
        assert pin in self._source("_apply_hybrid")


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
