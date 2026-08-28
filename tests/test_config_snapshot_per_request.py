"""One config snapshot per proxied request (#871).

``ProxyManager._config`` is a property over ``ProxyConfigLoader.get()``, and
every ``get()`` does a ``Path.stat()``. The manager reads that property from
dozens of places, so an unsnapshotted request issued a burst of redundant
syscalls *and* could mix two reload generations within a single call — the
policy gate deciding on one config while a later stage ran on another.

The fixture deliberately runs an ACTIVE pipeline (SELECTIVE compression over a
response far past the budget, plus a live response cache) rather than the
cheapest possible one: with TRUNCATE and no cache, most of the newly threaded
branches early-exit, and a helper that forgot to pass its snapshot would go
unnoticed. The tests pin three things — the read count, the identity of the
object each request helper receives, and that hot reload still lands, observed
through the response rather than through the loader.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.config import (
    AutoIndexConfig,
    CacheConfig,
    CompressionStrategy,
    ExtractionConfig,
    ExtractionStrategy,
    ProxyConfig,
    RelevanceScorerConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics_store import MetricsStore
from helpers import (
    FakeSurfacingEngine,
    count_loader_reads,
    wire_proxy_manager,
)

UPSTREAM_CHARS = 5000
MAX_RESULT_CHARS = 200


def _file_config(
    tmp_path: Path,
    *,
    compression: CompressionStrategy = CompressionStrategy.SELECTIVE,
    auto_index: bool = False,
    extraction: bool = False,
    extraction_overrides: dict | None = None,
    **overrides,
) -> dict:
    """Full on-disk config, kept in sync with the in-memory one the manager is
    seeded with. Written whole every time: a partial rewrite would drop
    ``upstream_servers`` (or silently fall back to the ``auto`` default
    strategy) on reload and change behavior for a reason that has nothing to do
    with the field under test."""
    data = {
        "enabled": True,
        "default_compression": compression.value,
        "upstream_servers": {
            "srv": {
                # Field-for-field identical to the in-memory
                # UpstreamServerConfig below — ``command`` is omitted from both
                # so they take the same default. A divergence changes the
                # connection fingerprint on reload and provokes a real reconnect
                # attempt, a side effect these tests neither want nor assert on.
                "prefix": "test",
                "max_result_chars": MAX_RESULT_CHARS,
                "max_retries": 0,
                "reconnect_delay_seconds": 0.0,
            }
        },
        # min_chars/background pinned so both stages run INLINE and are
        # observable before call_tool returns; the defaults schedule
        # extraction on a background task and skip indexing under 2000 chars.
        "auto_index": {
            "enabled": auto_index,
            "background": False,
            "min_chars": 0,
            "memory_dir": str(tmp_path / "index"),
        },
        "extraction": {
            "enabled": extraction,
            "background": False,
            # HEURISTIC keeps extraction local. The LLM default would reach for
            # a provider endpoint, so the suite's behavior would depend on
            # whether the developer happens to be running Ollama.
            "strategy": "heuristic",
            "memory_dir": str(tmp_path / "facts"),
            **(extraction_overrides or {}),
        },
        "cache": {
            "db_path": str(tmp_path / "cache.db"),
            # Unannotated test tools are not cacheable under the default
            # conservative policy, and the hit path must actually run.
            "tool_annotation_policy": "ignore",
        },
    }
    data.update(overrides)
    return data


def _build_mgr(
    tmp_path: Path,
    *,
    compression: CompressionStrategy = CompressionStrategy.SELECTIVE,
    body_repeat: int = 320,
    auto_index: bool = False,
    extraction: bool = False,
) -> tuple[ProxyManager, MetricsStore, ProxyCache]:
    # No explicit ``compression`` on the server: the strategy then resolves
    # from ``default_compression``, which is what the hot-reload test edits.
    server_cfg = UpstreamServerConfig(
        prefix="test",
        max_result_chars=MAX_RESULT_CHARS,
        max_retries=0,
        reconnect_delay_seconds=0.0,
    )
    cfg_path = tmp_path / "proxy.json"
    cfg_path.write_text(
        json.dumps(
            _file_config(
                tmp_path, compression=compression, auto_index=auto_index, extraction=extraction
            )
        )
    )
    proxy_cfg = ProxyConfig(
        config_path=cfg_path,
        enabled=True,
        default_compression=compression,
        upstream_servers={"srv": server_cfg},
        auto_index=AutoIndexConfig(
            enabled=auto_index,
            background=False,
            min_chars=0,
            memory_dir=tmp_path / "index",
        ),
        extraction=ExtractionConfig(
            enabled=extraction,
            background=False,
            strategy=ExtractionStrategy.HEURISTIC,
            memory_dir=tmp_path / "facts",
        ),
        cache=CacheConfig(db_path=tmp_path / "cache.db", tool_annotation_policy="ignore"),
    )
    mgr, store, cache = wire_proxy_manager(
        proxy_cfg,
        server_cfg,
        tmp_path,
        with_cache=True,
        upstream_text="upstream content " * body_repeat,
    )
    assert cache is not None
    return mgr, store, cache


def _reload_with_max_facts(manager: ProxyManager, tmp_path: Path, max_facts: int) -> None:
    """Publish a new ``extraction`` generation on disk and make it visible.

    The loader gates on mtime, and tmp files written twice in the same tick can
    share one — so the timestamp is pushed past what the loader has already
    seen rather than left to the filesystem clock.
    """
    import os

    cfg_file = tmp_path / "proxy.json"
    cfg_file.write_text(
        json.dumps(
            _file_config(
                tmp_path,
                compression=CompressionStrategy.TRUNCATE,
                extraction=True,
                extraction_overrides={"max_facts": max_facts},
            )
        )
    )
    seen = manager._config_loader._mtime
    os.utime(cfg_file, (seen + 10, seen + 10))


@pytest.fixture
def mgr(tmp_path):
    manager, store, cache = _build_mgr(tmp_path)
    yield manager
    cache.close()
    store.close()


def _fake_index_engine() -> AsyncMock:
    """A bare AsyncMock returns AsyncMocks for ``indexed_chunks``, which then
    reach the metrics row and fail the sqlite bind with a swallowed warning.
    Give the fields real values so a failure here is the assertion, not that."""
    engine = AsyncMock()
    engine.index_file.return_value = SimpleNamespace(indexed_chunks=1, skipped_chunks=0)
    return engine


async def _drain(manager: ProxyManager) -> None:
    """Close manager-owned async resources the fixture caused to be built.

    ``_get_extractor`` builds a ``FactExtractor`` holding an httpx client; the
    manager only closes it in ``stop()``, which these tests never call."""
    if manager._extractor is not None:
        await manager._extractor.close()
        manager._extractor = None


@pytest.fixture
async def active_mgr(tmp_path):
    """A manager whose surfacing, auto-index and extraction stages all RUN.

    Without the engines those stages return before touching their config, so
    a snapshot dropped on one of those paths would be invisible.
    """
    manager, store, cache = _build_mgr(
        tmp_path,
        compression=CompressionStrategy.TRUNCATE,
        body_repeat=100,
        auto_index=True,
        extraction=True,
    )
    manager._surfacing_engine = FakeSurfacingEngine()
    manager._index_engine = _fake_index_engine()
    yield manager
    await _drain(manager)
    cache.close()
    store.close()


@pytest.fixture
async def extract_mgr(tmp_path):
    """Extraction on, auto-index OFF. The extract stage stores through the index
    engine, so the engine must exist; disabling the auto-index STAGE keeps these
    tests about extraction alone, with no index outcome in the metrics row to
    reason about."""
    manager, store, cache = _build_mgr(
        tmp_path,
        compression=CompressionStrategy.TRUNCATE,
        body_repeat=100,
        extraction=True,
    )
    manager._surfacing_engine = FakeSurfacingEngine()
    manager._index_engine = _fake_index_engine()
    yield manager
    await _drain(manager)
    cache.close()
    store.close()


@pytest.fixture
def truncate_mgr(tmp_path):
    """A response carrying a transient retrieval key is never stored
    (``response_carries_transient_key``), so the cache-hit path needs both a
    storable strategy and a body under the 4000-char progressive chunk size —
    otherwise progressive chunking wins and emits a read_more key."""
    manager, store, cache = _build_mgr(
        tmp_path, compression=CompressionStrategy.TRUNCATE, body_repeat=100
    )
    yield manager
    cache.close()
    store.close()


class TestPerRequestSnapshot:
    async def test_two_loader_reads_per_call_tool(self, mgr):
        """The whole request — policy gate, ranking, cache lookup, and every
        pipeline stage — runs off a single snapshot.

        Steady state is TWO loader reads, not one: the request snapshot, plus
        one live read in the Toolgraph enforcement gate. That gate is
        deliberately excluded from the snapshot — its verdict has to agree with
        withhold state derived from live config, and splitting them fails open
        (tests/test_toolgraph_bundle.py). Warmed up first: a request that
        CONSTRUCTS a long-lived component takes further live reads, pinned
        separately below.
        """
        await mgr.call_tool("srv", "tool", {"warm": 1})
        calls = count_loader_reads(mgr)
        await mgr.call_tool("srv", "tool", {"a": 1})
        assert calls[0] == 2, f"expected 1 snapshot + 1 enforcement read, got {calls[0]}"

    async def test_building_the_compressor_costs_no_extra_read(self, mgr):
        """Building the selective compressor adds NO loader read of its own.

        The component is cached for the process, so it must not be baked from a
        superseded generation — but "not superseded" is exactly what the
        request's own pin already is in the common case. The build publishes
        under one resolved generation and takes its scorer from that same
        object, so the extra read is paid only in the stale window. This pins
        the price so it cannot grow back unnoticed.
        """
        calls = count_loader_reads(mgr)
        await mgr.call_tool("srv", "tool", {"a": 1})
        assert calls[0] == 2, f"expected the 2-read baseline, got {calls[0]}"
        assert mgr._selective_compressor is not None

    async def test_cache_hit_path_stays_on_the_two_read_baseline(self, truncate_mgr):
        """The hit path re-applies surfacing and so reads config of its own;
        it must ride the same single snapshot."""
        mgr = truncate_mgr
        await mgr.call_tool("srv", "tool", {"a": 1})
        assert mgr._cache.stats()["total_entries"] == 1, "nothing cached; no hit path to test"
        session = mgr._connections["srv"].session
        upstream_calls = session.call_tool.await_count

        calls = count_loader_reads(mgr)
        await mgr.call_tool("srv", "tool", {"a": 1})
        assert session.call_tool.await_count == upstream_calls, "second call was not a cache hit"
        assert calls[0] == 2, f"expected the 2-read baseline on a cache hit, got {calls[0]}"

    async def test_extraction_request_read_counts(self, extract_mgr):
        """The extraction path's cost, warm and cold, pinned separately.

        ``_get_extractor`` splits by lifetime: the lock timeout rides the
        snapshot, the extractor is built LIVE. Warm and cold cost the SAME one
        extra read, because the rebuild predicate (#890) reads the live
        ``extraction`` block on every call and the build reuses that read
        rather than taking a second — pinned here so a predicate that starts
        reading twice, or one that goes back to skipping the check when the
        slot is filled, moves the count.
        """
        mgr = extract_mgr
        calls = count_loader_reads(mgr)
        await mgr.call_tool("srv", "tool", {"a": 1})
        cold = calls[0]
        assert mgr._extractor is not None, "extraction stage did not run"

        calls[0] = 0
        await mgr.call_tool("srv", "tool", {"b": 2})
        warm = calls[0]

        # baseline + 1 live extractor read. TRUNCATE here, so no selective
        # compressor is constructed; the combined case is covered below.
        assert warm == 3, f"expected 2 baseline + 1 live predicate read, got {warm}"
        assert cold == 3, f"expected 2 baseline + 1 live build read, got {cold}"

    async def test_combined_construction_read_count(self, tmp_path):
        """Both build paths on one request: the reads add, they do not multiply.

        Only the fact extractor still costs a read of its own — it is built
        from LIVE config by design and has no publication generation to ride,
        and since #890 its rebuild predicate re-reads that block on every call.
        So this one is the same warm and cold, unlike the compressor build.
        Pinned so a component that starts taking its own read again, or a third
        one, moves the count here.
        """
        mgr, store, cache = _build_mgr(
            tmp_path, compression=CompressionStrategy.SELECTIVE, extraction=True
        )
        mgr._surfacing_engine = FakeSurfacingEngine()
        mgr._index_engine = _fake_index_engine()
        try:
            calls = count_loader_reads(mgr)
            await mgr.call_tool("srv", "tool", {"a": 1})
            assert mgr._selective_compressor is not None and mgr._extractor is not None
            assert calls[0] == 3, f"expected 2 baseline + 1 live extractor read, got {calls[0]}"

            calls[0] = 0
            await mgr.call_tool("srv", "tool", {"b": 2})
            assert calls[0] == 3, (
                f"expected 2 baseline + 1 extractor predicate read, got {calls[0]}"
            )
        finally:
            await _drain(mgr)
            cache.close()
            store.close()

    async def test_snapshot_does_not_leak_across_requests(self, mgr):
        """One per request, not one per manager: the second call re-reads."""
        await mgr.call_tool("srv", "tool", {"warm": 1})
        calls = count_loader_reads(mgr)
        await mgr.call_tool("srv", "tool", {"a": 1})
        await mgr.call_tool("srv", "tool", {"b": 2})
        assert calls[0] == 4  # the 2-read baseline, twice

    async def test_every_request_helper_receives_the_same_object(self, active_mgr, monkeypatch):
        """Counting alone cannot catch a helper that re-pins from a *stale*
        loader; pin object identity instead.

        Toolgraph enforcement is deliberately absent: it reads live config, not
        the snapshot, so that its verdict cannot disagree with the withhold
        state it consults — see tests/test_toolgraph_bundle.py.

        Runs on the ``active_mgr`` fixture so the surfacing, auto-index and
        extraction stages actually execute — on a manager with those engines
        unset they return before ever consulting their config, and a helper
        that dropped its snapshot there would go unseen.
        """
        mgr = active_mgr
        expected = {
            "_rank_candidates",
            "_resolve_cache_ttl",
            "_apply_compression",
            "_surfacing_enabled_for",
            "_auto_index_response",
            "_extract_and_store",
        }
        seen: dict[str, list[object]] = {}

        def record(name: str):
            original = getattr(mgr, name)

            def wrapper(*args, **kwargs):
                seen.setdefault(name, []).append(kwargs.get("cfg_snap"))
                return original(*args, **kwargs)

            monkeypatch.setattr(mgr, name, wrapper)

        for name in expected:
            record(name)

        await mgr.call_tool("srv", "tool", {"a": 1})

        assert set(seen) == expected, f"stages that never ran: {expected - set(seen)}"
        received = [snap for snaps in seen.values() for snap in snaps]
        assert None not in received, seen
        first = received[0]
        assert all(snap is first for snap in received), "helpers saw different config objects"

    async def test_extractor_is_built_from_live_config_not_the_snapshot(
        self, extract_mgr, tmp_path
    ):
        """The long-lived instance takes the FRESHEST config, not the pinned one.

        Everything else on the request path reads ``cfg_snap``, because those
        are decisions scoped to one call. This instance outlives the request
        that builds it, so pinning it to a snapshot captured before the upstream
        call would freeze the PRE-edit generation whenever a reload lands
        mid-call — and the rebuild predicate would then compare against a
        generation nobody is running.

        The reload is driven from inside the mocked upstream call so the two
        candidate implementations diverge: a live read sees the post-reload
        value, a ``cfg_snap`` build sees the pre-reload one.
        """
        import os

        mgr = extract_mgr
        cfg_file = tmp_path / "proxy.json"
        session = mgr._connections["srv"].session
        upstream_result = session.call_tool.return_value

        async def reload_config_mid_request(*_args, **_kwargs):
            cfg_file.write_text(
                json.dumps(
                    _file_config(
                        tmp_path,
                        compression=CompressionStrategy.TRUNCATE,
                        extraction=True,
                        extraction_overrides={"max_facts": 3},
                    )
                )
            )
            seen = mgr._config_loader._mtime
            os.utime(cfg_file, (seen + 10, seen + 10))
            return upstream_result

        session.call_tool.side_effect = reload_config_mid_request

        await mgr.call_tool("srv", "tool", {"a": 1})

        assert mgr._extractor is not None, "extraction stage did not run"
        assert mgr._config.extraction.max_facts == 3, "the reload did not land"
        assert mgr._extractor._cfg.max_facts == 3, "extractor used the stale snapshot"

    async def test_extractor_is_rebuilt_on_config_change(self, extract_mgr, tmp_path):
        """An edit to the ``extraction`` block reaches the extractor itself (#890).

        The stage gate and ``ext_cfg`` at the call site always saw the new
        generation; the cached instance kept the old strategy/provider/limits
        until restart. That is the worst of both — the config LOOKS live while
        the behavior it names is not — so the instance now tracks the block it
        was built from and is replaced when they diverge.
        """
        mgr = extract_mgr
        await mgr.call_tool("srv", "tool", {"a": 1})
        first = mgr._extractor
        assert first is not None
        assert first._cfg.max_facts == 10

        _reload_with_max_facts(mgr, tmp_path, 3)
        await mgr.call_tool("srv", "tool", {"b": 2})

        # The new generation reaches ext_cfg at the call site...
        assert mgr._config.extraction.max_facts == 3
        # ...and the cached extractor with it.
        assert mgr._extractor is not first, "the slot still holds the pre-edit instance"
        assert mgr._extractor._cfg.max_facts == 3
        assert mgr._extractor_cfg == mgr._config.extraction

    async def test_superseded_extractor_is_closed_on_rebuild(self, extract_mgr, tmp_path):
        """The replaced instance is closed, not dropped on the floor.

        It owns an httpx client, so a rebuild that only reassigns the slot
        leaks a transport per config edit. Closing also flips the #867 gate,
        which is what makes the swap safe for a caller still holding the old
        reference: it takes the local heuristic instead of the provider.
        """
        mgr = extract_mgr
        await mgr.call_tool("srv", "tool", {"a": 1})
        old = mgr._extractor
        assert old is not None, "extraction stage did not run"
        assert old._client is not None

        _reload_with_max_facts(mgr, tmp_path, 3)
        await mgr.call_tool("srv", "tool", {"b": 2})

        assert mgr._extractor is not old, "the extractor was not rebuilt"
        assert old._gate.closed is True, "superseded extractor still admits callers"
        assert old._client is None, "superseded extractor leaked its httpx client"

    async def test_extractor_rebuild_failure_leaves_old_usable(
        self, extract_mgr, tmp_path, monkeypatch
    ):
        """A failed rebuild leaves the working instance installed and OPEN.

        Building before touching the slot is what buys this: closing first, or
        stamping the new cfg before the build succeeds, would park a closed
        extractor behind the equality fast path and take extraction down until
        the next config edit. Mirrors
        ``test_rebuild_selective_failure_leaves_old_usable``.
        """
        import memtomem_stm.proxy.manager as manager_mod

        mgr = extract_mgr
        old = await mgr._get_extractor(cfg_snap=mgr._config)
        assert mgr._extractor_cfg == old._cfg

        _reload_with_max_facts(mgr, tmp_path, 3)

        def boom(_cfg):
            raise RuntimeError("extractor build failed")

        monkeypatch.setattr(manager_mod, "FactExtractor", boom)
        with pytest.raises(RuntimeError, match="extractor build failed"):
            await mgr._get_extractor(cfg_snap=mgr._config)

        assert mgr._extractor is old, "a failed rebuild dropped the working extractor"
        assert mgr._extractor_cfg == old._cfg, "a failed rebuild restamped the cache"
        assert old._gate.closed is False, "old instance closed before a replacement existed"
        assert old._client is not None

        monkeypatch.undo()
        new = await mgr._get_extractor(cfg_snap=mgr._config)
        assert new is not old, "the retry did not rebuild"
        assert new._cfg.max_facts == 3

    async def test_rebuild_drains_the_in_flight_extractor(self, extract_mgr, tmp_path):
        """The superseded close waits out live callers, and does it AFTER
        publishing the replacement.

        Two properties in one: the transport is not torn down under a
        registered call (#867's drain), and the drain does not sit inside
        ``_extractor_lock`` — the new instance is visible while it runs, so a
        concurrent request is not queued behind another caller's shutdown.
        """
        import asyncio

        mgr = extract_mgr
        old = await mgr._get_extractor(cfg_snap=mgr._config)
        token = old._gate.try_enter(30.0)
        assert token is not None

        _reload_with_max_facts(mgr, tmp_path, 3)
        rebuild = asyncio.create_task(mgr._get_extractor(cfg_snap=mgr._config))
        await asyncio.sleep(0.05)

        assert not rebuild.done(), "the rebuild did not wait for the in-flight caller"
        assert old._client is not None, "transport closed under a registered call"
        assert mgr._extractor is not old, "the replacement was not published before the drain"

        # The load-bearing half: a second caller must get the replacement WHILE
        # the drain runs. Publishing before the drain is not enough on its own —
        # a close awaited inside ``_extractor_lock`` would satisfy every
        # assertion above and still block this one until the gate is released.
        concurrent = await asyncio.wait_for(
            mgr._get_extractor(cfg_snap=mgr._config), timeout=5
        )
        assert concurrent is mgr._extractor, "a concurrent caller was served the retired instance"
        assert not rebuild.done(), "the drain ended early; the concurrency check proved nothing"

        old._gate.leave(token)
        new = await asyncio.wait_for(rebuild, timeout=5)

        assert new is mgr._extractor
        assert old._client is None, "the drained instance was never closed"

    async def test_an_unstamped_slot_is_rebuilt(self, extract_mgr):
        """``None`` in the cfg stamp is not a wildcard.

        A predicate that only compares when a stamp exists would keep any
        externally installed instance forever — which is the pre-#890 behavior
        wearing the new field. The stamp is the claim "this instance was built
        from that block"; absent, there is no such claim to honor.
        """
        mgr = extract_mgr
        installed = await mgr._get_extractor(cfg_snap=mgr._config)
        mgr._extractor_cfg = None

        rebuilt = await mgr._get_extractor(cfg_snap=mgr._config)

        assert rebuilt is not installed, "an unstamped slot was treated as current"
        assert mgr._extractor_cfg == mgr._config.extraction

    async def test_stop_does_not_strand_a_replacement_published_mid_teardown(
        self, extract_mgr, tmp_path
    ):
        """``stop()`` and a rebuild contend for one slot; neither may drop an
        open instance on the floor.

        Teardown closes the extractor and clears the slot. A rebuild running
        concurrently used to be able to publish its replacement between those
        two steps, leaving an open httpx client that was neither shared nor
        closed. Driven through the real contention: the in-flight gate holds
        ``stop()`` inside its close while the rebuild tries to publish.

        The invariant is ownership, not ordering. Either outcome is legitimate
        — the rebuild declines while teardown owns the slot, or it lands after
        teardown finished and is indistinguishable from a post-stop request —
        so what is asserted is that whatever it returns is either installed
        (and so closed by the next stop) or already closed. An instance that is
        neither is the leak.
        """
        import asyncio

        mgr = extract_mgr
        installed = await mgr._get_extractor(cfg_snap=mgr._config)
        token = installed._gate.try_enter(30.0)
        assert token is not None

        stopping = asyncio.create_task(mgr.stop())
        await asyncio.sleep(0.05)
        assert not stopping.done(), "stop() did not reach the extractor drain"

        _reload_with_max_facts(mgr, tmp_path, 3)
        rebuild = asyncio.create_task(mgr._get_extractor(cfg_snap=mgr._config))
        await asyncio.sleep(0.05)

        installed._gate.leave(token)
        await asyncio.wait_for(stopping, timeout=10)
        published = await asyncio.wait_for(rebuild, timeout=10)

        assert installed._client is None, "teardown did not close the installed extractor"
        assert mgr._retiring_extractors == set(), "a superseded instance was left unclosed"
        if published is mgr._extractor:
            # Landed after teardown: tracked, so the next stop() closes it.
            assert mgr._extractor_cfg == mgr._config.extraction
        else:
            # Declined to publish: it must be the instance teardown closed,
            # never a fresh open one dropped on the floor.
            assert published is installed
            assert mgr._extractor is None
            assert mgr._extractor_cfg is None

    async def test_config_edit_lands_on_the_next_request(self, mgr, tmp_path):
        """Snapshotting moves hot-reload to a request boundary; it must not
        turn into "restart required". Observed through the RESPONSE — asserting
        on ``mgr._config`` would pass even if the request never re-read."""
        import os

        first = await mgr.call_tool("srv", "tool", {"a": 1})
        assert isinstance(first, str)
        assert len(first) <= MAX_RESULT_CHARS * 2  # SELECTIVE compressed it

        # Turn compression off at the file level. The server config sets no
        # explicit ``compression``, so ``default_compression`` governs.
        cfg_file = tmp_path / "proxy.json"
        cfg_file.write_text(json.dumps(_file_config(tmp_path, default_compression="none")))
        seen = mgr._config_loader._mtime
        os.utime(cfg_file, (seen + 10, seen + 10))

        second = await mgr.call_tool("srv", "tool", {"b": 2})
        assert isinstance(second, str)
        assert len(second) > MAX_RESULT_CHARS * 2, "config edit did not reach the next request"

    async def test_stale_pin_does_not_rebuild_the_shared_scorer_backward(self, mgr):
        """The scorer cache outlives the request, so a stale pin must not write it.

        ``_relevance_scorer_for`` takes the caller's snapshot, and the instance
        it stamps is shared by every later request. A call that pinned an older
        generation and resumed after a newer one already rebuilt the shared
        scorer forward would otherwise stamp the singleton BACK, and interleaved
        traffic would ping-pong it until the stale requests drain. The stale
        caller still gets a scorer built from its own config — it just does not
        publish it.
        """
        gen1 = mgr._config
        gen2 = gen1.model_copy(
            update={"relevance_scorer": RelevanceScorerConfig(scorer="bm25", embedding_timeout=7.0)}
        )
        mgr._config_loader.seed(gen2)

        forward = mgr._relevance_scorer_for(gen2)
        assert mgr._relevance_scorer_instance is forward
        assert mgr._relevance_scorer_cfg == gen2.relevance_scorer

        stale = mgr._relevance_scorer_for(gen1)

        assert stale is not None
        assert mgr._relevance_scorer_instance is forward, "stale pin rebuilt the shared scorer"
        assert mgr._relevance_scorer_cfg == gen2.relevance_scorer, "stale pin restamped the cache"

    def _stale_and_live(self, mgr):
        """Seed a NEWER generation so the manager's current pin reads stale.

        Returns ``(stale_cfg, live_cfg)`` differing in the per-server
        ``selective`` block, which is what the shared stores key their rebuild
        decision on.
        """
        from memtomem_stm.proxy.config import SelectiveConfig

        stale = mgr._config
        srv = stale.upstream_servers["srv"]
        live = stale.model_copy(
            update={
                "upstream_servers": {
                    "srv": srv.model_copy(update={"selective": SelectiveConfig(max_pending=77)})
                }
            }
        )
        mgr._config_loader.seed(live)
        assert mgr._pin_is_live_generation(stale) is False, "the pin should read superseded"
        assert mgr._pin_is_live_generation(live) is True
        return stale, live

    @pytest.mark.parametrize("strategy", ["selective", "hybrid"])
    async def test_warm_rebuild_stays_on_the_request_generation(self, mgr, strategy):
        """A live config change rebuilds the compressor without a second read.

        The rebuild is a fill too: whatever generation it publishes under has
        to be the one its scorer comes from. Reading the scorer separately
        would both cost a read and let ``get()`` re-load between the two, so
        the compressor would pair this request's selective config with a later
        scorer. Reaching this branch means the pin IS live, so the pin is that
        generation and nothing needs re-reading.
        """
        from memtomem_stm.proxy.config import CompressionStrategy as CS
        from memtomem_stm.proxy.config import HybridConfig, SelectiveConfig

        # HYBRID rebuilds the SAME compressor from its own call site, so both
        # branches need the assertion; one covering the other is how a dropped
        # argument stays green.
        compression = CS.SELECTIVE if strategy == "selective" else CS.HYBRID
        hybrid_cfg = None if strategy == "selective" else HybridConfig()

        # Warm the slot under a config the next call will disagree with.
        mgr._rebuild_selective_compressor(SelectiveConfig(max_pending=5))
        warm = mgr._selective_compressor
        cfg_snap = mgr._config
        sel_cfg = mgr._resolve_tool_config("srv", "tool", cfg_snap).selective
        assert mgr._selective_compressor_cfg != sel_cfg, "the rebuild branch must be taken"

        calls = count_loader_reads(mgr)
        await mgr._apply_compression(
            "word " * 400,
            compression,
            200,
            sel_cfg,
            None,
            hybrid_cfg,
            "srv",
            "tool",
            cfg_snap=cfg_snap,
        )

        assert mgr._selective_compressor is not warm, "the rebuild did not happen"
        assert calls[0] == 0, f"the warm rebuild took {calls[0]} loader read(s) of its own"
        assert mgr._selective_compressor_cfg == sel_cfg

    async def test_stale_request_key_survives_the_next_live_request(self, truncate_mgr):
        """The end-to-end property: a key handed out under a stale pin still reads.

        Refusing to overwrite a POPULATED shared slot is only half the guard.
        On a COLD slot a stale caller would publish its own generation, and the
        next live request — finding a config it disagrees with — would replace
        and close that store, taking the read-more key the stale caller already
        returned to its client with it. This drives the real
        ``_apply_progressive`` → ``read_more`` path rather than poking the
        store, because that is where the key's lifetime actually lives.
        """
        from memtomem_stm.proxy.config import ProgressiveConfig
        from memtomem_stm.proxy.progressive import PROGRESSIVE_FOOTER_TOKEN

        mgr = truncate_mgr
        assert mgr._progressive_store is None, "the slot must start cold"
        stale, live = self._stale_and_live(mgr)

        pcfg = ProgressiveConfig(chunk_size=2000, include_structure_hint=False)
        text = "z" * 12000

        first = mgr._apply_progressive(
            text,
            pcfg,
            server="srv",
            tool="tool",
            sel_cfg=mgr._resolve_tool_config("srv", "tool", stale).selective,
            cfg_snap=stale,
        )
        key = next(iter(mgr._get_progressive_store()._store._data.keys()))
        initial = len(first.split(PROGRESSIVE_FOOTER_TOKEN, 1)[0])
        assert initial == 2000

        # The live generation now runs the same stage.
        mgr._apply_progressive(
            text,
            pcfg,
            server="srv",
            tool="tool",
            sel_cfg=mgr._resolve_tool_config("srv", "tool", live).selective,
            cfg_snap=live,
        )

        chunk = mgr.read_more(key, offset=initial).split(PROGRESSIVE_FOOTER_TOKEN, 1)[0]
        assert chunk == "z" * 2000, "the stale request's key was stranded"

    async def test_stale_request_key_survives_a_warm_old_store(self, truncate_mgr):
        """The warm case of the same rule: a stale pin must not write into a
        store that is about to be closed.

        Refusing to let a superseded pin REPLACE the store fixes one direction
        and breaks the other if it then reuses whatever is cached: when the
        cached store belongs to an older generation, the stale request mints
        its key there and the next live request closes it, so the key it just
        handed to the client dies. Every request works on the newest
        generation's store, which makes both directions safe.
        """
        from memtomem_stm.proxy.config import ProgressiveConfig
        from memtomem_stm.proxy.progressive import PROGRESSIVE_FOOTER_TOKEN

        mgr = truncate_mgr
        stale, live = self._stale_and_live(mgr)
        pcfg = ProgressiveConfig(chunk_size=2000, include_structure_hint=False)
        text = "z" * 12000

        # Warm the slot under the STALE generation's store, the way a request
        # that ran before the reload would have left it.
        mgr._get_progressive_store(mgr._resolve_tool_config("srv", "tool", stale).selective)
        assert mgr._progressive_store is not None

        first = mgr._apply_progressive(
            text,
            pcfg,
            server="srv",
            tool="tool",
            sel_cfg=mgr._resolve_tool_config("srv", "tool", stale).selective,
            cfg_snap=stale,
        )
        key = next(iter(mgr._get_progressive_store()._store._data.keys()))
        initial = len(first.split(PROGRESSIVE_FOOTER_TOKEN, 1)[0])

        mgr._apply_progressive(
            text,
            pcfg,
            server="srv",
            tool="tool",
            sel_cfg=mgr._resolve_tool_config("srv", "tool", live).selective,
            cfg_snap=live,
        )

        chunk = mgr.read_more(key, offset=initial).split(PROGRESSIVE_FOOTER_TOKEN, 1)[0]
        assert chunk == "z" * 2000, "the stale request minted its key into a doomed store"

    async def test_cold_shared_slot_is_published_from_the_live_generation(self, mgr):
        """The mechanism behind the test above, on the compression path.

        A stale caller filling an empty slot publishes the LIVE selective
        config, not its own, so the next live request agrees with what it finds
        and leaves the store alone.
        """
        from memtomem_stm.proxy.config import CompressionStrategy as CS

        assert mgr._selective_compressor is None, "the slot must start cold"
        stale, live = self._stale_and_live(mgr)
        live_sel = mgr._resolve_tool_config("srv", "tool", live).selective
        stale_sel = mgr._resolve_tool_config("srv", "tool", stale).selective
        assert stale_sel != live_sel, "the two generations must actually differ"

        await mgr._apply_compression(
            "word " * 400,
            CS.SELECTIVE,
            200,
            stale_sel,
            None,
            None,
            "srv",
            "tool",
            cfg_snap=stale,
        )

        assert mgr._selective_compressor_cfg == live_sel, (
            "a stale pin published its own generation into the shared slot"
        )

    def test_threaded_helpers_require_a_snapshot(self):
        """An omitted ``cfg_snap`` must be a TypeError, not a silent live read.

        A defaulted parameter makes the omission indistinguishable from correct
        code at the call site: a new stage that forgets to thread the snapshot
        compiles, passes on every path a fixture does not exercise, and
        reintroduces the split this file exists to prevent. The pre-existing
        snapshot helpers already required it; these are the ones #871 added.
        """
        import inspect

        threaded = [
            "_apply_compression",
            "_apply_surfacing",
            "_apply_surfacing_on_progressive",
            "_surfacing_enabled_for",
            "_apply_hybrid",
            "_auto_index_response",
            "_get_extractor",
            "_extract_and_store",
            "_on_cache_hit",
            "_rank_candidates",
            "_call_tool_guarded",
            "_fetch_upstream",
            "_call_tool_inner",
            # pre-existing, required from the start — kept here so the whole
            # set is one list rather than two conventions
            "_cache_key_fingerprint",
            "_resolve_cache_ttl",
            "_tool_cache_eligible",
        ]
        for name in threaded:
            sig = inspect.signature(getattr(ProxyManager, name))
            param = sig.parameters.get("cfg_snap")
            assert param is not None, f"{name} lost its cfg_snap parameter"
            assert param.default is inspect.Parameter.empty, (
                f"{name}.cfg_snap has a default; an omitted snapshot must raise"
            )
