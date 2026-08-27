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
        pipeline stage — runs off a single snapshot."""
        calls = _count_loader_reads(mgr)
        await mgr.call_tool("srv", "tool", {"a": 1})
        assert calls[0] == 1, f"expected 1 loader read per request, got {calls[0]}"

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
        assert calls[0] == 1, f"expected 1 loader read on a cache hit, got {calls[0]}"

    async def test_snapshot_does_not_leak_across_requests(self, mgr):
        """One per request, not one per manager: the second call re-reads."""
        calls = _count_loader_reads(mgr)
        await mgr.call_tool("srv", "tool", {"a": 1})
        await mgr.call_tool("srv", "tool", {"b": 2})
        assert calls[0] == 2

    async def test_every_request_helper_receives_the_same_object(self, active_mgr, monkeypatch):
        """Counting alone cannot catch a helper that re-pins from a *stale*
        loader; pin object identity instead.

        Runs on the ``active_mgr`` fixture so the surfacing, auto-index and
        extraction stages actually execute — on a manager with those engines
        unset they return before ever consulting their config, and a helper
        that dropped its snapshot there would go unseen.
        """
        mgr = active_mgr
        expected = {
            "_enforce_toolgraph_call_policy",
            "_rank_candidates",
            "_resolve_cache_ttl",
            "_apply_compression",
            "_surfacing_enabled_for",
            "_auto_index_response",
            "_extract_and_store",
            "_get_extractor",
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

    async def test_extractor_is_built_from_the_requesting_snapshot(self, extract_mgr, tmp_path):
        """Extraction and storage agree for the call that builds the extractor.

        ``_extract_and_store`` persists with the request's ``cfg_snap``, so
        ``_get_extractor`` must construct from that same snapshot rather than a
        fresh read — otherwise provider/limits could come from one generation
        while namespace/dedup came from another.
        """
        mgr = extract_mgr
        await mgr.call_tool("srv", "tool", {"a": 1})
        assert mgr._extractor is not None, "extraction stage did not run"
        assert mgr._extractor._cfg == mgr._config.extraction

    async def test_extractor_is_not_rebuilt_on_config_change(self, extract_mgr, tmp_path):
        """Pins #890's KNOWN GAP so the follow-up has a red-to-green target.

        The extractor is a process-lifetime singleton with no rebuild, so an
        edit to the extraction block needs a restart. That predates this PR and
        is unchanged by it; a safe swap needs FactExtractor to expose a use
        token, since callers can already be holding the instance.
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
