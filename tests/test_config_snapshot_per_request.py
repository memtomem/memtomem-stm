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
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore

UPSTREAM_CHARS = 5000
MAX_RESULT_CHARS = 200


def _result(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        is_error=False,
        structured_content=None,
        meta=None,
    )


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
    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
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
    mgr = ProxyManager(proxy_cfg, TokenTracker(metrics_store=store))
    session = AsyncMock()
    session.call_tool.return_value = _result("upstream content " * body_repeat)
    mgr._connections["srv"] = UpstreamConnection(
        name="srv", config=server_cfg, session=session, tools=[]
    )
    cache = ProxyCache(tmp_path / "cache.db", max_entries=100)
    cache.initialize()
    mgr._cache = cache
    return mgr, store, cache


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


class _FakeSurfacingEngine:
    """Minimal surfacing engine: the manager reads ``injection_mode`` /
    ``observability`` and awaits ``surface(...)``. Appends rather than
    prepends so a progressive footer would survive."""

    injection_mode = "append"
    observability = None

    async def surface(self, *, response_text: str, **_kwargs) -> str:
        return response_text + " [mem]"


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
    manager._surfacing_engine = _FakeSurfacingEngine()
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
    manager._surfacing_engine = _FakeSurfacingEngine()
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


def _count_loader_reads(manager: ProxyManager) -> list[int]:
    """Wrap the manager's loader so every ``get()`` bumps a counter."""
    calls = [0]
    real_get = manager._config_loader.get

    def counting_get():
        calls[0] += 1
        return real_get()

    manager._config_loader.get = counting_get  # type: ignore[method-assign]
    return calls


class TestPerRequestSnapshot:
    async def test_one_loader_read_per_call_tool(self, mgr):
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
        calls = _count_loader_reads(mgr)
        await mgr.call_tool("srv", "tool", {"a": 1})
        assert calls[0] == 2, f"expected 1 snapshot + 1 enforcement read, got {calls[0]}"

    async def test_building_a_long_lived_component_takes_one_live_read(self, mgr):
        """The build path reads live config exactly once more, by design.

        A component cached for the process — here the selective compressor's
        scorer — must not be baked from a snapshot pinned before the upstream
        call, or a reload landing mid-call freezes the pre-edit generation for
        every later request. Per-call decisions use the snapshot; things that
        outlive the call read live. This pins the price of that rule so it
        cannot grow unnoticed.
        """
        calls = _count_loader_reads(mgr)
        await mgr.call_tool("srv", "tool", {"a": 1})
        assert calls[0] == 3, f"expected 2 baseline + 1 live build read, got {calls[0]}"
        assert mgr._selective_compressor is not None

    async def test_one_loader_read_on_the_cache_hit_path(self, truncate_mgr):
        """The hit path re-applies surfacing and so reads config of its own;
        it must ride the same single snapshot."""
        mgr = truncate_mgr
        await mgr.call_tool("srv", "tool", {"a": 1})
        assert mgr._cache.stats()["total_entries"] == 1, "nothing cached; no hit path to test"
        session = mgr._connections["srv"].session
        upstream_calls = session.call_tool.await_count

        calls = _count_loader_reads(mgr)
        await mgr.call_tool("srv", "tool", {"a": 1})
        assert session.call_tool.await_count == upstream_calls, "second call was not a cache hit"
        assert calls[0] == 2, f"expected the 2-read baseline on a cache hit, got {calls[0]}"

    async def test_extraction_request_read_counts(self, extract_mgr):
        """The extraction path's cost, warm and cold, pinned separately.

        ``_get_extractor`` splits by lifetime: the lock timeout rides the
        snapshot, the extractor is built LIVE. So a cold extraction request
        pays one extra read for that construction and a warm one pays none —
        counting only the compression build would have missed this entirely.
        """
        mgr = extract_mgr
        calls = _count_loader_reads(mgr)
        await mgr.call_tool("srv", "tool", {"a": 1})
        cold = calls[0]
        assert mgr._extractor is not None, "extraction stage did not run"

        calls[0] = 0
        await mgr.call_tool("srv", "tool", {"b": 2})
        warm = calls[0]

        assert warm == 2, f"expected the 2-read baseline for a warm request, got {warm}"
        # baseline + 1 live extractor build. TRUNCATE here, so no selective
        # compressor is constructed; the combined case is covered below.
        assert cold == 3, f"expected 2 baseline + 1 live build read, got {cold}"

    async def test_combined_construction_read_count(self, tmp_path):
        """Both build paths on one request: the reads add, they do not multiply.

        This is the worst case a single request can reach — 1 snapshot plus one
        live read per long-lived component it constructs. Pinned so a third
        such component cannot be added without the count moving here.
        """
        mgr, store, cache = _build_mgr(
            tmp_path, compression=CompressionStrategy.SELECTIVE, extraction=True
        )
        mgr._surfacing_engine = _FakeSurfacingEngine()
        mgr._index_engine = _fake_index_engine()
        try:
            calls = _count_loader_reads(mgr)
            await mgr.call_tool("srv", "tool", {"a": 1})
            assert mgr._selective_compressor is not None and mgr._extractor is not None
            assert calls[0] == 4, f"expected 2 baseline + 2 live build reads, got {calls[0]}"

            calls[0] = 0
            await mgr.call_tool("srv", "tool", {"b": 2})
            assert calls[0] == 2, f"expected the 2-read baseline once built, got {calls[0]}"
        finally:
            await _drain(mgr)
            cache.close()
            store.close()

    async def test_snapshot_does_not_leak_across_requests(self, mgr):
        """One per request, not one per manager: the second call re-reads."""
        await mgr.call_tool("srv", "tool", {"warm": 1})
        calls = _count_loader_reads(mgr)
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
        """A never-rebuilt singleton takes the FRESHEST config, not the pinned one.

        Everything else on the request path reads ``cfg_snap``, because those
        are decisions scoped to one call. This instance outlives every request,
        so whichever generation builds it wins until restart — and pinning it to
        a snapshot captured before the upstream call would freeze the PRE-edit
        generation whenever a reload lands mid-call.

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

    async def test_extractor_is_not_rebuilt_on_config_change(self, extract_mgr, tmp_path):
        """Pins #890's KNOWN GAP so the follow-up has a red-to-green target.

        The extractor is a process-lifetime singleton with no rebuild, so an
        edit to the extraction block needs a restart. That predates this PR and
        is unchanged by it; a safe swap needs an explicit lease, since callers
        can already be holding the instance when a replacement is published.
        """
        import os

        mgr = extract_mgr
        await mgr.call_tool("srv", "tool", {"a": 1})
        assert mgr._extractor is not None
        assert mgr._extractor._cfg.max_facts == 10

        cfg_file = tmp_path / "proxy.json"
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

        await mgr.call_tool("srv", "tool", {"b": 2})
        # The new generation reaches ext_cfg at the call site...
        assert mgr._config.extraction.max_facts == 3
        # ...but not the cached extractor. When #890 lands, this flips to 3.
        assert mgr._extractor._cfg.max_facts == 10

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
