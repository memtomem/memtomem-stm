"""One selective pending store per pending-store backend (#1365).

The manager used to cache ONE selective compressor plus the whole
``SelectiveConfig`` it was built from, and rebuilt it (closing the old store)
whenever a SELECTIVE/HYBRID call came from a server with a different selective
block. A TOC ``selection_key`` handed out by one server died as soon as another
server ran a SELECTIVE/HYBRID call.

Stores are now kept per backend, calls compress through a per-call policy view,
and an off-thread write resolves its (server, tool) route at write time (#898).
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from memtomem_stm.proxy.compression import BM25Scorer, SelectiveCompressor
from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    SelectiveConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.delivery import DeliveryError
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker

_ROW_HAS = "SELECT 1 FROM pending_selections WHERE instr(chunks_json, ?) > 0"
_SECTIONS = 30
STRATEGIES = [CompressionStrategy.SELECTIVE, CompressionStrategy.HYBRID]


def _text(marker: str) -> str:
    return "\n\n".join(f"## S{i}\n{marker * 500}" for i in range(_SECTIONS))


def _key(out: object) -> str:
    match = re.search(r'"selection_key":\s*"([0-9a-f]{16})"', str(out))
    assert match is not None, str(out)[:300]
    return match.group(1)


def _on_disk(db_path: Path, marker: str) -> bool:
    if not db_path.exists():
        return False
    db = sqlite3.connect(db_path)
    try:
        return db.execute(_ROW_HAS, (marker * 100,)).fetchone() is not None
    finally:
        db.close()


def _sqlite(path: Path, **kw: object) -> SelectiveConfig:
    return SelectiveConfig(pending_store="sqlite", pending_store_path=path, **kw)


def _cfg(tmp_path: Path, servers: dict[str, SelectiveConfig | None]) -> ProxyConfig:
    return ProxyConfig(
        enabled=True,
        config_path=tmp_path / "absent.json",
        upstream_servers={
            name: UpstreamServerConfig(prefix=name, selective=sel) for name, sel in servers.items()
        },
    )


def _manager(cfg: ProxyConfig) -> ProxyManager:
    mgr = ProxyManager(cfg, TokenTracker())
    for name, srv in cfg.upstream_servers.items():
        mgr._connections[name] = UpstreamConnection(name=name, config=srv, session=None, tools=[])
    return mgr


def _reload(mgr: ProxyManager, servers: dict[str, SelectiveConfig | None]) -> ProxyConfig:
    """Seed a newer generation with *servers*' selective blocks replaced."""
    old = mgr._config
    new = old.model_copy(
        update={
            "upstream_servers": {
                name: srv.model_copy(update={"selective": servers.get(name, srv.selective)})
                for name, srv in old.upstream_servers.items()
            }
        }
    )
    mgr._config_loader.seed(new)
    return new


async def _call(
    mgr: ProxyManager,
    server: str,
    marker: str,
    *,
    tool: str = "t",
    strategy: CompressionStrategy = CompressionStrategy.SELECTIVE,
    cfg: ProxyConfig | None = None,
    context_query: str | None = None,
) -> str:
    cfg = cfg if cfg is not None else mgr._config
    sel = mgr._resolve_tool_config(server, tool, cfg).selective
    out, _ = await mgr._apply_compression(
        _text(marker),
        strategy,
        1500,
        sel,
        None,
        None,
        server,
        tool,
        context_query=context_query,
        cfg_snap=cfg,
    )
    return _key(out)


def _serves(mgr: ProxyManager, key: str, marker: str) -> bool:
    """*key* reads back its own content (each publication is one marker)."""
    return marker * 50 in str(mgr.select_chunks(key, [f"S{_SECTIONS - 1}"]))


class _GatedScorer:
    """Blocks where an embedding scorer blocks, so the compress runs off-thread
    and a reload can land between the decision and the write."""

    uses_blocking_io = True

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.started = threading.Event()

    def score_sections(self, query: str, sections: list[tuple[str, str]]) -> list[float]:
        self.started.set()
        assert self.gate.wait(timeout=10), "test gate never released"
        return [0.0] * len(sections)


def _gate_next_call(mgr: ProxyManager) -> _GatedScorer:
    """The next view built gets a gated scorer; later ones get BM25."""
    gated = _GatedScorer()
    pending = [gated]

    def scorer_for(cfg: ProxyConfig):
        return pending.pop() if pending else BM25Scorer()

    mgr._relevance_scorer_for = scorer_for  # type: ignore[method-assign]
    return gated


async def _started(scorer: _GatedScorer) -> None:
    while not scorer.started.is_set():
        await asyncio.sleep(0.005)


# ── the issue's reproductions ────────────────────────────────────────


@pytest.mark.parametrize("strategy", STRATEGIES)
async def test_key_survives_a_call_on_another_backend(tmp_path, strategy):
    mgr = _manager(_cfg(tmp_path, {"m": None, "d": _sqlite(tmp_path / "pending.db")}))
    try:
        key = await _call(mgr, "m", "a", strategy=strategy)
        assert _serves(mgr, key, "a"), "positive control: served before the other call"
        await _call(mgr, "d", "b", strategy=strategy)
        assert _serves(mgr, key, "a")
    finally:
        await mgr.stop()


@pytest.mark.parametrize("strategy", STRATEGIES)
async def test_key_survives_a_call_with_another_policy(tmp_path, strategy):
    mgr = _manager(
        _cfg(
            tmp_path, {"m": SelectiveConfig(max_pending=100), "d": SelectiveConfig(max_pending=50)}
        )
    )
    try:
        key = await _call(mgr, "m", "a", strategy=strategy)
        await _call(mgr, "d", "b", strategy=strategy)
        assert _serves(mgr, key, "a")
    finally:
        await mgr.stop()


async def test_key_survives_a_reload_of_its_selective_block(tmp_path):
    mgr = _manager(_cfg(tmp_path, {"m": SelectiveConfig(max_pending=100)}))
    try:
        key = await _call(mgr, "m", "a")
        _reload(mgr, {"m": _sqlite(tmp_path / "pending.db")})
        await _call(mgr, "m", "b")
        assert _serves(mgr, key, "a")
    finally:
        await mgr.stop()


async def test_no_selective_call_stays_off_another_servers_sqlite_file(tmp_path):
    db = tmp_path / "pending.db"
    mgr = _manager(_cfg(tmp_path, {"m": None, "d": _sqlite(db)}))
    try:
        await _call(mgr, "d", "a")
        kb = await _call(mgr, "m", "b")
        assert _on_disk(db, "a"), "positive control: the sqlite row is on disk"
        assert not _on_disk(db, "b")
        assert _serves(mgr, kb, "b")
    finally:
        await mgr.stop()


# ── select_chunks over several stores ───────────────────────────────


async def test_selected_text_that_reads_like_a_miss_is_served(tmp_path):
    """A hit is a hit by what the store says, not by what the text says: the
    selected section's content contains the miss message, and a later store
    is still probed after it under a substring check."""
    mgr = _manager(_cfg(tmp_path, {"m": None, "d": _sqlite(tmp_path / "pending.db")}))
    try:
        await _call(mgr, "m", "a")
        await _call(mgr, "d", "b")
        assert len(mgr._selective_owners) == 2
        owner = next(iter(mgr._selective_owners.values()))
        lookalike = "Selection key 'x' not found or expired."
        toc = owner.with_policy().compress(
            f"## Only\n{lookalike}\n\n## Other\n{'o' * 400}", max_chars=100
        )
        key = _key(toc)
        assert lookalike in str(mgr.select_chunks(key, ["Only"]))
    finally:
        await mgr.stop()


async def test_restart_recovery_finds_the_key_in_any_configured_store(tmp_path):
    servers = {"a": _sqlite(tmp_path / "a.db"), "b": _sqlite(tmp_path / "b.db")}
    first = _manager(_cfg(tmp_path, servers))
    try:
        key = await _call(first, "b", "z")
    finally:
        await first.stop()

    second = _manager(_cfg(tmp_path, servers))
    try:
        assert second._selective_owners == {}
        assert _serves(second, key, "z")
        assert len(second._selective_owners) == 2, "recovery keeps every store it opened"
    finally:
        await second.stop()


async def test_no_store_at_all_is_the_inactive_sentinel(tmp_path):
    mgr = _manager(_cfg(tmp_path, {"m": None}))
    try:
        assert "not active" in mgr.select_chunks("0" * 16, ["S0"])
        with pytest.raises(DeliveryError):
            mgr.select_chunks("0" * 16, ["S0"], response_format="structured")
        await _call(mgr, "m", "a")
        assert "not found or expired" in mgr.select_chunks("0" * 16, ["S0"])
    finally:
        await mgr.stop()


async def test_a_store_opened_for_recovery_that_raises_is_dropped(tmp_path, monkeypatch):
    mgr = _manager(_cfg(tmp_path, {"d": _sqlite(tmp_path / "pending.db")}))

    def boom(self, *args, **kwargs):
        raise RuntimeError("corrupt row")

    monkeypatch.setattr(SelectiveCompressor, "select_if_present", boom)
    try:
        with pytest.raises(RuntimeError):
            mgr.select_chunks("0" * 16, ["S0"])
        assert mgr._selective_owners == {}, "a reader stayed open on a store opened for nothing"
    finally:
        await mgr.stop()


async def test_a_refused_selection_keeps_the_store_it_opened(tmp_path, monkeypatch):
    """A structured refusal means the store holds the key: keep it open."""
    mgr = _manager(_cfg(tmp_path, {"d": _sqlite(tmp_path / "pending.db")}))

    def refuse(self, *args, **kwargs):
        raise DeliveryError("No matching sections found", code="no_matching_sections")

    monkeypatch.setattr(SelectiveCompressor, "select_if_present", refuse)
    try:
        with pytest.raises(DeliveryError):
            mgr.select_chunks("0" * 16, ["S0"])
        assert len(mgr._selective_owners) == 1
    finally:
        await mgr.stop()


async def test_after_stop_a_miss_opens_nothing(tmp_path):
    mgr = _manager(_cfg(tmp_path, {"d": _sqlite(tmp_path / "pending.db")}))
    await mgr.stop()
    assert "not active" in mgr.select_chunks("0" * 16, ["S0"])
    assert mgr._selective_owners == {}


# ── a write in flight across a reload (#898, per server/tool) ─────────


@pytest.mark.parametrize("strategy", STRATEGIES)
async def test_paused_write_follows_its_tools_new_backend_after_another_tool_decides(
    tmp_path, strategy
):
    """Tool ``a`` is scoring when a reload moves its server off SQLite; tool
    ``b`` on the same server is the first call to see the new generation. The
    write ``a`` finishes lands where its tool is now configured, not in the
    file the reload removed."""
    db = tmp_path / "pending.db"
    mgr = _manager(_cfg(tmp_path, {"s": _sqlite(db)}))
    try:
        gated = _gate_next_call(mgr)
        task = asyncio.create_task(
            _call(mgr, "s", "a", tool="a", strategy=strategy, context_query="q")
        )
        await _started(gated)
        _reload(mgr, {"s": SelectiveConfig()})
        await _call(mgr, "s", "b", tool="b", strategy=strategy)
        gated.gate.set()
        key = await task

        assert not _on_disk(db, "a"), "the write landed in the file the reload removed"
        assert _serves(mgr, key, "a")
    finally:
        await mgr.stop()


async def test_paused_write_evicts_under_its_tools_current_policy(tmp_path):
    """The eviction half: a paused write under ``max_pending=1`` must not trim
    the shared file after the reload raised the limit and another tool wrote
    there."""
    db = tmp_path / "pending.db"
    mgr = _manager(_cfg(tmp_path, {"s": _sqlite(db, max_pending=1)}))
    try:
        gated = _gate_next_call(mgr)
        task = asyncio.create_task(_call(mgr, "s", "a", tool="a", context_query="q"))
        await _started(gated)
        _reload(mgr, {"s": _sqlite(db, max_pending=100)})
        later = [await _call(mgr, "s", m, tool="b") for m in ("b", "c", "d")]
        gated.gate.set()
        await task

        assert all(_on_disk(db, m) for m in ("b", "c", "d")), (
            "a superseded max_pending evicted the keys the current generation wrote"
        )
        assert all(_serves(mgr, k, m) for k, m in zip(later, ("b", "c", "d"), strict=True))
    finally:
        await mgr.stop()


async def test_paused_write_is_not_retargeted_into_another_servers_backend(tmp_path):
    db = tmp_path / "pending.db"
    mgr = _manager(_cfg(tmp_path, {"m": None, "d": _sqlite(db)}))
    try:
        gated = _gate_next_call(mgr)
        task = asyncio.create_task(_call(mgr, "m", "a", context_query="q"))
        await _started(gated)
        await _call(mgr, "d", "b")
        gated.gate.set()
        key = await task

        assert _on_disk(db, "b"), "positive control: d wrote its file"
        assert not _on_disk(db, "a"), "m's write followed d's call into d's file"
        assert _serves(mgr, key, "a")
    finally:
        await mgr.stop()


async def test_stop_during_a_paused_write_defers_the_store_close(tmp_path):
    db = tmp_path / "pending.db"
    mgr = _manager(_cfg(tmp_path, {"d": _sqlite(db)}))
    gated = _gate_next_call(mgr)
    task = asyncio.create_task(_call(mgr, "d", "a", context_query="q"))
    await _started(gated)
    owner = next(iter(mgr._selective_owners.values()))
    await mgr.stop()
    assert owner._store._db is not None, "stop() closed the store under an in-flight write"
    gated.gate.set()
    await task
    assert _on_disk(db, "a")
    assert owner._store._db is None, "the deferred close never ran"


# ── policy per call ──────────────────────────────────────────────────


async def test_scorer_reload_applies_to_the_next_call(tmp_path):
    from memtomem_stm.proxy.config import RelevanceScorerConfig

    mgr = _manager(_cfg(tmp_path, {"m": None}))
    try:
        await _call(mgr, "m", "a")
        before = mgr._selective_routes[("m", "t")]._scorer
        new = mgr._config.model_copy(
            update={"relevance_scorer": RelevanceScorerConfig(scorer="bm25", embedding_timeout=7.0)}
        )
        mgr._config_loader.seed(new)
        await _call(mgr, "m", "b")
        after = mgr._selective_routes[("m", "t")]._scorer
        assert after is mgr._relevance_scorer_for(new)
        assert after is not before
    finally:
        await mgr.stop()


async def test_a_route_whose_server_is_gone_is_dropped_on_the_next_generation(tmp_path):
    mgr = _manager(_cfg(tmp_path, {"m": None, "d": None}))
    try:
        await _call(mgr, "m", "a")
        del mgr._connections["m"]
        _reload(mgr, {"d": SelectiveConfig(max_pending=7)})
        await _call(mgr, "d", "b")
        assert set(mgr._selective_routes) == {("d", "t")}
    finally:
        await mgr.stop()


async def test_a_refreshed_route_reads_back_under_its_new_ttl(tmp_path, monkeypatch):
    """A route refreshed by another server's call must also move its store's
    select-time TTL: the paused write publishes under the new TTL, and the key
    has to survive past the old one."""
    import memtomem_stm.proxy.pending_store as pending_store_mod

    clock = [1000.0]
    monkeypatch.setattr(pending_store_mod.time, "time", lambda: clock[0])
    db = tmp_path / "a.db"
    mgr = _manager(_cfg(tmp_path, {"a": _sqlite(db, pending_ttl_seconds=1.0), "b": None}))
    try:
        gated = _gate_next_call(mgr)
        task = asyncio.create_task(_call(mgr, "a", "a", context_query="q"))
        await _started(gated)
        _reload(mgr, {"a": _sqlite(db, pending_ttl_seconds=300.0)})
        await _call(mgr, "b", "b")
        gated.gate.set()
        key = await task

        clock[0] += 5.0
        assert _serves(mgr, key, "a"), "the key expired under the superseded TTL"
    finally:
        await mgr.stop()


async def test_a_locked_sqlite_store_does_not_block_a_key_held_elsewhere(tmp_path):
    """A miss in one store is a read: probing a SQLite store whose file another
    connection holds a write lock on must not fail a key another store holds."""
    db = tmp_path / "pending.db"
    mgr = _manager(_cfg(tmp_path, {"d": _sqlite(db), "m": None}))
    holder = None
    try:
        await _call(mgr, "d", "a")
        key = await _call(mgr, "m", "b")
        assert list(mgr._selective_owners)[0][0] == "sqlite", "the sqlite store is probed first"
        for owner in mgr._selective_owners.values():
            db_conn = getattr(owner._store, "_db", None)
            if db_conn is not None:
                db_conn.execute("PRAGMA busy_timeout = 100")
        holder = sqlite3.connect(db, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        assert _serves(mgr, key, "b")
    finally:
        if holder is not None:
            holder.execute("ROLLBACK")
            holder.close()
        await mgr.stop()


async def test_a_failed_decision_leaves_every_store_ttl_alone(tmp_path, monkeypatch):
    """A decision whose own store will not open installs no route, so it must
    not move any store's read TTL either: the paused write still publishes
    under its old route's TTL and has to read back by it."""
    import memtomem_stm.proxy.pending_store as pending_store_mod

    clock = [1000.0]
    monkeypatch.setattr(pending_store_mod.time, "time", lambda: clock[0])
    db = tmp_path / "a.db"
    bad = tmp_path / "as_dir"
    bad.mkdir()
    mgr = _manager(_cfg(tmp_path, {"a": _sqlite(db, pending_ttl_seconds=300.0), "b": None}))
    try:
        gated = _gate_next_call(mgr)
        task = asyncio.create_task(_call(mgr, "a", "a", context_query="q"))
        await _started(gated)
        _reload(mgr, {"a": _sqlite(db, pending_ttl_seconds=1.0), "b": _sqlite(bad)})
        with pytest.raises(sqlite3.Error):
            await _call(mgr, "b", "b")
        gated.gate.set()
        key = await task

        clock[0] += 5.0
        assert _serves(mgr, key, "a"), "a failed decision moved the store's read TTL"
    finally:
        await mgr.stop()


async def test_two_spellings_of_one_sqlite_path_share_one_store(tmp_path, monkeypatch):
    """``pending.db`` and ``sub/../pending.db`` are one file. Two owners on it
    would each expire the shared rows by their own read TTL, so the short-TTL
    alias deleted a key the long-TTL alias had just published."""
    import memtomem_stm.proxy.pending_store as pending_store_mod

    clock = [1000.0]
    monkeypatch.setattr(pending_store_mod.time, "time", lambda: clock[0])
    (tmp_path / "sub").mkdir()
    plain = tmp_path / "pending.db"
    alias = tmp_path / "sub" / ".." / "pending.db"
    mgr = _manager(
        _cfg(
            tmp_path,
            {
                "a": _sqlite(plain, pending_ttl_seconds=1.0),
                "b": _sqlite(alias, pending_ttl_seconds=300.0),
            },
        )
    )
    try:
        await _call(mgr, "a", "a")
        key = await _call(mgr, "b", "b")
        assert len(mgr._selective_owners) == 1, "one file, one store"

        clock[0] += 5.0
        assert _serves(mgr, key, "b")
    finally:
        await mgr.stop()
