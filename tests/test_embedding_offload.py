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
import json
import re
import sqlite3
import threading
from types import MethodType, SimpleNamespace

import pytest

from memtomem_stm.proxy.compression import SelectiveCompressor
from memtomem_stm.proxy.config import CompressionStrategy, SelectiveConfig
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
        # ``_apply_compression`` asks whether the caller's pin is still the
        # newest generation before it fills a shared slot; ``current`` is None
        # here, which reads as "no generation to compare against" and leaves
        # the passed selective config alone.
        _config_loader=SimpleNamespace(current=None),
        _selective_compressor=compressor,
        _selective_compressor_cfg=None,
    )
    stub._compress_maybe_offthread = MethodType(ProxyManager._compress_maybe_offthread, stub)
    stub._pin_is_live_generation = MethodType(ProxyManager._pin_is_live_generation, stub)
    stub._publication_pair = MethodType(ProxyManager._publication_pair, stub)
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


# ── the off-thread write follows the live generation (#898) ───────────


class _GatedScorer:
    """Blocks where the real embedding scorer blocks: a network round trip.

    The gate belongs HERE, not in parsing. Scoring is why the compress was
    off-loaded to a worker thread in the first place, so it is the widest
    window a config reload can land in — and the ordering that keeps it
    ahead of the write is the thing under test. A parse-only gate passes
    whether or not scoring precedes publication.
    """

    uses_blocking_io = True

    def __init__(self, gate: threading.Event) -> None:
        self._gate = gate
        self.started = threading.Event()

    def score_sections(self, query: str, sections: list[tuple[str, str]]) -> list[float]:
        self.started.set()
        assert self._gate.wait(timeout=10), "test gate never released"
        return [0.0] * len(sections)


def _barrier_stub() -> SimpleNamespace:
    """Manager stub carrying the real generation-swap machinery."""
    stub = SimpleNamespace(
        _selective_compressor=None,
        _selective_compressor_cfg=None,
        _selective_publish_mu=threading.Lock(),
        _relevance_scorer=BM25Scorer(),
    )
    stub._create_selective = MethodType(ProxyManager._create_selective, stub)
    stub._rebuild_selective_compressor = MethodType(
        ProxyManager._rebuild_selective_compressor, stub
    )
    return stub


def _publish_gated(stub: SimpleNamespace, gate: threading.Event) -> SelectiveCompressor:
    """Install a scorer-gated compressor as the stub's current generation."""
    scorer = _GatedScorer(gate)
    gated = SelectiveCompressor(
        scorer=scorer,
        live_provider=lambda: stub._selective_compressor,
        publish_lock=stub._selective_publish_mu,
    )
    gated.started = scorer.started
    stub._selective_compressor = gated
    stub._selective_compressor_cfg = SelectiveConfig()
    return gated


_TEXT = "## Alpha\n" + "a" * 400 + "\n\n## Beta\n" + "b" * 400


def _key_of(toc: str) -> str:
    return json.loads(toc)["selection_key"]


class TestSelectiveWriteBarrier:
    """#898: a compress that started under one generation must not mint its
    retrieval key into a store the readers no longer consult."""

    def test_create_selective_wires_the_live_provider(self):
        stub = _barrier_stub()
        g1 = stub._rebuild_selective_compressor(SelectiveConfig(max_pending=5))
        assert g1._live_provider() is g1, "a lone generation must resolve to itself"

        g2 = stub._rebuild_selective_compressor(SelectiveConfig(max_pending=7))
        assert g1._live_provider() is g2, (
            "the retired compressor must resolve writes to the live generation"
        )
        assert g1._publish_lock is stub._selective_publish_mu

    def _swap_during_compress(self, stub, gated, gate, swaps: int) -> str:
        """Run gated.compress off-thread, swap generations while it scores."""
        result: list[str] = []

        def worker() -> None:
            result.append(gated.compress(_TEXT, max_chars=200, context_query="alpha"))

        t = threading.Thread(target=worker)
        t.start()
        try:
            assert gated.started.wait(timeout=10), "scoring never started"
            for n in range(swaps):
                stub._rebuild_selective_compressor(SelectiveConfig(max_pending=10 + n))
        finally:
            gate.set()
        t.join(timeout=10)
        assert result, "compress never returned"
        return result[0]

    def test_key_lands_in_the_generation_the_reader_consults(self):
        """The issue's sequence: G1 pinned and compressing, G2 published, G1
        finishes. The key it hands the client must resolve through G2 — the
        instance ``select_chunks`` reads off the manager's slot."""
        stub = _barrier_stub()
        gate = threading.Event()
        gated = _publish_gated(stub, gate)

        key = _key_of(self._swap_during_compress(stub, gated, gate, swaps=1))

        live = stub._selective_compressor
        assert live is not gated, "the swap did not happen"
        assert live.select(key, ["Alpha"]) == "a" * 400, (
            "the key was minted into the retired store — the client holds a "
            "key that was never reachable (#898)"
        )
        assert gated._store.get(key) is None, "the write should not touch the retired store"

    def test_key_follows_repeated_swaps(self):
        """G1 → G2 → G3 while one compress is in flight: the write resolves at
        put time, so it lands in whichever generation is current then."""
        stub = _barrier_stub()
        gate = threading.Event()
        gated = _publish_gated(stub, gate)

        key = _key_of(self._swap_during_compress(stub, gated, gate, swaps=2))

        assert stub._selective_compressor.select(key, ["Beta"]) == "b" * 400

    def test_a_rebuild_cannot_interleave_with_the_write(self):
        """The window the scorer gate cannot reach: a rebuild arriving AFTER the
        target is resolved and WHILE its store write is running.

        Resolving the target under the lock and then writing outside it would
        let that rebuild retire the target mid-write — the key would land in a
        store the reader no longer consults, and the retired generation's
        eviction policy would run over a shared path. Both are the defect this
        change exists to close, so the write has to stay inside the lock. What
        that guarantees, and what this pins, is serialization: the rebuild does
        not complete until the write has.
        """
        stub = _barrier_stub()
        g1 = stub._rebuild_selective_compressor(SelectiveConfig(max_pending=5))
        in_put = threading.Event()
        release = threading.Event()
        real_put = g1._store.put

        def gated_put(key, pending):
            in_put.set()
            assert release.wait(timeout=10), "test gate never released"
            real_put(key, pending)

        g1._store.put = gated_put  # type: ignore[method-assign]

        published: list[str] = []
        rebuilt = threading.Event()

        def publisher() -> None:
            published.append(_key_of(g1.compress(_TEXT, max_chars=200)))

        def rebuilder() -> None:
            stub._rebuild_selective_compressor(SelectiveConfig(max_pending=9))
            rebuilt.set()

        pt = threading.Thread(target=publisher)
        pt.start()
        assert in_put.wait(timeout=10), "the write never started"
        rt = threading.Thread(target=rebuilder)
        rt.start()
        try:
            # The rebuild is on the other side of the publish mutex; it must not
            # be able to swap the slot out from under the in-flight write.
            assert not rebuilt.wait(timeout=0.5), (
                "a rebuild completed while a selection write was in flight — "
                "the write is no longer protected by the publish lock"
            )
            assert stub._selective_compressor is g1, "the slot was swapped mid-write"
        finally:
            release.set()
        pt.join(timeout=10)
        rt.join(timeout=10)

        assert rebuilt.is_set(), "the rebuild never completed after the write drained"
        assert g1._store.get(published[0]) is not None, (
            "the write did not land in the generation that was live when it started"
        )

    def test_restart_recovery_cannot_install_over_an_in_flight_write(self, tmp_path):
        """The other writer of the slot: ``select_chunks`` restart recovery.

        It fills a COLD slot (#583), and its pre-#898 argument for needing no
        lock — no await between the check and the assignment — only ever covered
        the event loop. An off-thread compress now reads the same slot to pick
        its write target, so an unguarded install can land between that read and
        the worker's put and leave the returned key behind. Recovery takes the
        publish mutex for the install, which serializes the two.
        """
        from memtomem_stm.proxy.config import (
            ProxyConfig,
            SelectiveConfig,
            UpstreamServerConfig,
        )

        db = tmp_path / "pending.db"
        sel_cfg = SelectiveConfig(pending_store="sqlite", pending_store_path=db)
        cfg = ProxyConfig(upstream_servers={"srv": UpstreamServerConfig(prefix="test", selective=sel_cfg)})

        stub = _barrier_stub()
        stub._config = cfg
        # The scorer cache is not what this test is about; the stub's single
        # BM25 instance stands in for the per-generation resolution.
        stub._relevance_scorer_for = lambda _cfg: stub._relevance_scorer
        for name in (
            "select_chunks",
            "_distinct_sqlite_selective_cfgs",
            "_sqlite_cfg_holding_key",
            "_is_recovery_miss",
        ):
            setattr(stub, name, MethodType(getattr(ProxyManager, name), stub))

        # A compressor on the configured store, published as the live one, then
        # the slot goes cold the way stop() leaves it.
        writer = stub._rebuild_selective_compressor(sel_cfg)
        in_put = threading.Event()
        release = threading.Event()
        real_put = writer._store.put

        def gated_put(k, pending):
            in_put.set()
            assert release.wait(timeout=10), "test gate never released"
            real_put(k, pending)

        writer._store.put = gated_put  # type: ignore[method-assign]
        stub._selective_compressor = None
        stub._selective_compressor_cfg = None

        published: list[str] = []
        recovered = threading.Event()

        def publisher() -> None:
            published.append(_key_of(writer.compress(_TEXT, max_chars=200)))

        def recoverer() -> None:
            stub.select_chunks("does-not-exist", ["Alpha"])
            recovered.set()

        pt = threading.Thread(target=publisher)
        pt.start()
        assert in_put.wait(timeout=10), "the write never started"
        rt = threading.Thread(target=recoverer)
        rt.start()
        try:
            assert not recovered.wait(timeout=0.5), (
                "restart recovery installed a compressor while a selection write "
                "was in flight — the install bypasses the publish mutex"
            )
        finally:
            release.set()
        pt.join(timeout=10)
        rt.join(timeout=10)
        assert recovered.is_set(), "recovery never completed after the write drained"

        # Same configured path, so the recovered generation serves the key the
        # worker published while the slot was cold.
        assert stub._selective_compressor is not None
        assert stub._selective_compressor.select(published[0], ["Alpha"]) == "a" * 400

    def test_hybrid_tail_write_follows_the_live_generation(self):
        """HYBRID compresses its tail through the SAME shared selective
        instance, so its TOC key has the same exposure."""
        from memtomem_stm.proxy.compression import HybridCompressor

        stub = _barrier_stub()
        gate = threading.Event()
        gated = _publish_gated(stub, gate)
        hybrid = HybridCompressor(head_chars=100, selective_compressor=gated)

        result: list[str] = []

        def worker() -> None:
            result.append(hybrid.compress(_TEXT, max_chars=400, context_query="alpha"))

        t = threading.Thread(target=worker)
        t.start()
        try:
            assert gated.started.wait(timeout=10)
            stub._rebuild_selective_compressor(SelectiveConfig(max_pending=11))
        finally:
            gate.set()
        t.join(timeout=10)

        match = re.search(r'"selection_key":\s*"([0-9a-f]+)"', result[0])
        assert match is not None, f"no TOC key in the hybrid output: {result[0][:200]}"
        key = match.group(1)
        assert stub._selective_compressor._store.get(key) is not None, (
            "the hybrid tail's key was stranded in the retired store"
        )

    def test_cleared_slot_falls_back_to_the_pinned_store(self):
        """``stop()`` clears the slot while a worker is still compressing. The
        write falls back to the pinned instance's own store — still open,
        because its close defers to the last end_use (#628) — rather than
        raising into the caller's result."""
        stub = _barrier_stub()
        gate = threading.Event()
        gated = _publish_gated(stub, gate)
        gated.begin_use()

        result: list[str] = []

        def worker() -> None:
            result.append(gated.compress(_TEXT, max_chars=200, context_query="alpha"))

        t = threading.Thread(target=worker)
        t.start()
        try:
            assert gated.started.wait(timeout=10)
            with stub._selective_publish_mu:
                gated.close()
                stub._selective_compressor = None
        finally:
            gate.set()
        t.join(timeout=10)

        key = _key_of(result[0])
        assert gated.select(key, ["Alpha"]) == "a" * 400
        gated.end_use()

    def test_single_generation_writes_to_its_own_store(self):
        """A standalone compressor (no manager, no provider) keeps the plain
        put-then-evict path."""
        comp = SelectiveCompressor()
        key = _key_of(comp.compress(_TEXT, max_chars=200))
        assert comp._store.get(key) is not None


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
