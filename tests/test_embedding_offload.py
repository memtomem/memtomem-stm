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
    """The SELECTIVE branch with the decision stubbed: *compressor* is both the
    backend's owner (it takes the pin) and the call's view (it compresses)."""
    stub = SimpleNamespace(
        _selective_lock=asyncio.Lock(),
        _config=SimpleNamespace(lock_timeout_seconds=lock_timeout),
        _decide_selective=lambda server, tool, sel_cfg, cfg_snap: (compressor, compressor),
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
        #628), while pinning defers a concurrent stop()'s close() until the
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
        """The gate must read the scorer the view actually holds, not a fresh
        ``self._relevance_scorer``: the view was built from the generation its
        call decided under, and gating on the property could run an
        embedding-backed compress inline after a reload (codex review of #628)."""
        pin = 'scorer=getattr(sel_compressor, "_scorer", None)'
        assert pin in self._source("_apply_compression")
        assert pin in self._source("_apply_hybrid")


# ── the off-thread write follows its route (#898, #1365) ──────────────


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


def _barrier_manager(tmp_path) -> ProxyManager:
    """A real manager with one upstream ``srv`` and no network."""
    from memtomem_stm.proxy.config import ProxyConfig, UpstreamServerConfig
    from memtomem_stm.proxy.manager import UpstreamConnection
    from memtomem_stm.proxy.metrics import TokenTracker

    srv = UpstreamServerConfig(prefix="t")
    cfg = ProxyConfig(config_path=tmp_path / "absent.json", upstream_servers={"srv": srv})
    mgr = ProxyManager(cfg, TokenTracker())
    mgr._connections["srv"] = UpstreamConnection(name="srv", config=srv, session=None, tools=[])
    return mgr


def _decide(mgr: ProxyManager, sel: SelectiveConfig | None = None) -> SelectiveCompressor:
    """Decide a call on ``srv``/``tool``, as the SELECTIVE branch does; the view."""
    return mgr._decide_selective("srv", "tool", sel, mgr._config)[1]


def _sqlite(tmp_path, name: str = "pending.db") -> SelectiveConfig:
    return SelectiveConfig(pending_store="sqlite", pending_store_path=tmp_path / name)


def _publish_gated(mgr: ProxyManager, gate: threading.Event) -> SelectiveCompressor:
    """Decide a call whose view scores through a gated scorer."""
    gated = _decide(mgr)
    scorer = _GatedScorer(gate)
    gated._scorer = scorer
    gated.started = scorer.started
    return gated


_TEXT = "## Alpha\n" + "a" * 400 + "\n\n## Beta\n" + "b" * 400


def _key_of(toc: str) -> str:
    return json.loads(toc)["selection_key"]


class TestSelectiveWriteBarrier:
    """#898: a compress that started under one generation must not mint its
    retrieval key into a store its tool no longer uses, or evict under a policy
    its tool no longer has. Since #1365 the target is the call's own
    (server, tool) route rather than one manager-wide slot."""

    def test_views_resolve_their_own_route(self, tmp_path):
        mgr = _barrier_manager(tmp_path)
        g1 = _decide(mgr, SelectiveConfig(max_pending=5))
        assert g1._live_provider() is g1, "a lone decision must resolve to itself"

        g2 = _decide(mgr, SelectiveConfig(max_pending=7))
        assert g1._live_provider() is g2, (
            "a superseded view must resolve writes to its route's newest view"
        )
        other = mgr._decide_selective("srv", "other", None, mgr._config)[1]
        assert g1._live_provider() is g2, "another tool's decision moved this route"
        assert other._live_provider() is other
        assert g1._publish_lock is mgr._selective_publish_mu

    def _swap_during_compress(self, mgr, gated, gate, sels: list) -> str:
        """Run gated.compress off-thread, decide again while it scores."""
        result: list[str] = []

        def worker() -> None:
            result.append(gated.compress(_TEXT, max_chars=200, context_query="alpha"))

        t = threading.Thread(target=worker)
        t.start()
        try:
            assert gated.started.wait(timeout=10), "scoring never started"
            for sel in sels:
                _decide(mgr, sel)
        finally:
            gate.set()
        t.join(timeout=10)
        assert result, "compress never returned"
        return result[0]

    async def test_key_lands_in_the_store_the_route_now_names(self, tmp_path):
        """The issue's sequence: V1 compressing, V2 decided on another backend,
        V1 finishes. The key it hands the client lands in V2's store."""
        mgr = _barrier_manager(tmp_path)
        try:
            gate = threading.Event()
            gated = _publish_gated(mgr, gate)

            key = _key_of(self._swap_during_compress(mgr, gated, gate, [_sqlite(tmp_path)]))

            live = mgr._selective_routes[("srv", "tool")]
            assert live is not gated, "the swap did not happen"
            assert live.select(key, ["Alpha"]) == "## Alpha\n" + "a" * 400 + "\n\n"
            assert gated._store.get(key) is None, "the write landed in the superseded store"
            assert mgr.select_chunks(key, ["Alpha"]) == "## Alpha\n" + "a" * 400 + "\n\n"
        finally:
            await mgr.stop()

    async def test_key_follows_repeated_swaps(self, tmp_path):
        """V1 → V2 → V3 while one compress is in flight: the write resolves at
        put time, so it lands in whichever view is current then."""
        mgr = _barrier_manager(tmp_path)
        try:
            gate = threading.Event()
            gated = _publish_gated(mgr, gate)
            sels = [_sqlite(tmp_path, "a.db"), _sqlite(tmp_path, "b.db")]

            key = _key_of(self._swap_during_compress(mgr, gated, gate, sels))

            live = mgr._selective_routes[("srv", "tool")]
            assert live.select(key, ["Beta"]) == "## Beta\n" + "b" * 400
            a_owner = mgr._selective_owner(sels[0])
            assert a_owner._store.get(key) is None, "the write stopped at an intermediate view"
        finally:
            await mgr.stop()

    def test_a_decision_cannot_interleave_with_the_write(self, tmp_path):
        """The window the scorer gate cannot reach: a decision arriving AFTER the
        target is resolved and WHILE its store write is running.

        Resolving the target under the lock and then writing outside it would
        let that decision move the route mid-write — the key would land in a
        store the tool no longer uses, and a superseded eviction policy would
        run over a shared path. What the lock guarantees, and what this pins,
        is serialization: the decision does not install until the write is done.
        """
        mgr = _barrier_manager(tmp_path)
        g1 = _decide(mgr, SelectiveConfig(max_pending=5))
        in_put = threading.Event()
        release = threading.Event()
        real_put = g1._store.put

        def gated_put(key, pending):
            in_put.set()
            assert release.wait(timeout=10), "test gate never released"
            real_put(key, pending)

        g1._store.put = gated_put  # type: ignore[method-assign]

        published: list[str] = []
        decided = threading.Event()

        def publisher() -> None:
            published.append(_key_of(g1.compress(_TEXT, max_chars=200)))

        def decider() -> None:
            _decide(mgr, SelectiveConfig(max_pending=9))
            decided.set()

        pt = threading.Thread(target=publisher)
        pt.start()
        assert in_put.wait(timeout=10), "the write never started"
        dt = threading.Thread(target=decider)
        dt.start()
        try:
            # The install is on the other side of the publish mutex; it must not
            # move the route out from under the in-flight write.
            assert not decided.wait(timeout=0.5), (
                "a decision installed while a selection write was in flight — "
                "the write is no longer protected by the publish lock"
            )
            assert mgr._selective_routes[("srv", "tool")] is g1, "the route moved mid-write"
        finally:
            release.set()
        pt.join(timeout=10)
        dt.join(timeout=10)

        assert decided.is_set(), "the decision never completed after the write drained"
        assert g1._store.get(published[0]) is not None, (
            "the write did not land in the store that was current when it started"
        )

    async def test_hybrid_tail_write_follows_the_route(self, tmp_path):
        """HYBRID compresses its tail through the SAME view, so its TOC key has
        the same exposure."""
        from memtomem_stm.proxy.compression import HybridCompressor

        mgr = _barrier_manager(tmp_path)
        try:
            gate = threading.Event()
            gated = _publish_gated(mgr, gate)
            hybrid = HybridCompressor(head_chars=100, selective_compressor=gated)

            result: list[str] = []

            def worker() -> None:
                result.append(hybrid.compress(_TEXT, max_chars=400, context_query="alpha"))

            t = threading.Thread(target=worker)
            t.start()
            try:
                assert gated.started.wait(timeout=10)
                _decide(mgr, _sqlite(tmp_path))
            finally:
                gate.set()
            t.join(timeout=10)

            match = re.search(r'"selection_key":\s*"([0-9a-f]+)"', result[0])
            assert match is not None, f"no TOC key in the hybrid output: {result[0][:200]}"
            key = match.group(1)
            assert mgr._selective_routes[("srv", "tool")]._store.get(key) is not None
            assert gated._store.get(key) is None, "the hybrid tail's key stayed behind"
        finally:
            await mgr.stop()

    def test_cleared_routes_fall_back_to_the_pinned_store(self, tmp_path):
        """``stop()`` clears the routes while a worker is still compressing. The
        write falls back to the view's own store — still open, because the
        owner's close defers to the last end_use (#628) — rather than raising
        into the caller's result."""
        mgr = _barrier_manager(tmp_path)
        owner, gated = mgr._decide_selective("srv", "tool", _sqlite(tmp_path), mgr._config)
        gate = threading.Event()
        scorer = _GatedScorer(gate)
        gated._scorer = scorer
        owner.begin_use()

        result: list[str] = []

        def worker() -> None:
            result.append(gated.compress(_TEXT, max_chars=200, context_query="alpha"))

        t = threading.Thread(target=worker)
        t.start()
        try:
            assert scorer.started.wait(timeout=10)
            with mgr._selective_publish_mu:
                mgr._selective_routes.clear()
                owner.close()
        finally:
            gate.set()
        t.join(timeout=10)

        key = _key_of(result[0])
        assert gated.select(key, ["Alpha"]) == "## Alpha\n" + "a" * 400 + "\n\n"
        owner.end_use()
        assert owner._store._db is None, "the deferred close never ran"

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
