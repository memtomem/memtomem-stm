"""Tests for PendingStore backends and SelectiveCompressor integration."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from memtomem_stm.proxy.compression import PendingSelection, SelectiveCompressor
from memtomem_stm.proxy.pending_store import InMemoryPendingStore, SQLitePendingStore


def _make_selection(chunks: dict[str, str] | None = None) -> PendingSelection:
    return PendingSelection(
        chunks=chunks or {"sec1": "content1", "sec2": "content2"},
        format="markdown",
        created_at=time.monotonic(),
        total_chars=100,
    )


def _progressive_selection() -> PendingSelection:
    return PendingSelection(
        chunks={"__content__": "body", "__meta__": "{}"},
        format="progressive",
        created_at=time.monotonic(),
        total_chars=4,
    )


# ── InMemoryPendingStore ────────────────────────────────────────────────


class TestInMemoryPendingStore:
    def test_scoped_eviction_does_not_cross_delivery_formats(self):
        store = InMemoryPendingStore()
        store.put("selective", _make_selection())
        store.put("progressive", _progressive_selection())

        store.evict_oldest(0, exclude_format="progressive")

        assert store.get("selective") is None
        assert store.get("progressive") is not None

    def test_put_and_get(self):
        store = InMemoryPendingStore()
        sel = _make_selection()
        store.put("k1", sel)
        assert store.get("k1") is sel
        assert store.get("missing") is None

    def test_touch_updates_created_at(self):
        store = InMemoryPendingStore()
        sel = _make_selection()
        original_time = sel.created_at
        store.put("k1", sel)
        # Windows time.monotonic() falls back to GetTickCount64 with ~15.6 ms
        # resolution; sleep at least one tick + headroom so the second sample
        # is guaranteed to advance.
        time.sleep(0.05)
        store.touch("k1")
        assert store.get("k1").created_at > original_time

    def test_evict_expired(self):
        store = InMemoryPendingStore()
        sel = _make_selection()
        sel.created_at = time.monotonic() - 100  # expired
        store.put("old", sel)
        store.put("new", _make_selection())
        store.evict_expired(ttl=50)
        assert store.get("old") is None
        assert store.get("new") is not None

    def test_evict_oldest(self):
        store = InMemoryPendingStore()
        for i in range(5):
            store.put(f"k{i}", _make_selection())
        assert len(store) == 5
        store.evict_oldest(max_size=3)
        assert len(store) == 3
        # k0 and k1 should be evicted (oldest)
        assert store.get("k0") is None
        assert store.get("k1") is None
        assert store.get("k4") is not None

    def test_len(self):
        store = InMemoryPendingStore()
        assert len(store) == 0
        store.put("k1", _make_selection())
        assert len(store) == 1
        store.delete("k1")
        assert len(store) == 0

    def test_reput_refreshes_recency_for_eviction(self):
        # A re-put key must count as the NEWEST entry (SQLite: the re-put takes
        # the next ``seq``). A duplicate _order entry used to make evict_oldest
        # pop the re-put key first — dropping the fresh data.
        store = InMemoryPendingStore()
        store.put("k1", _make_selection())
        store.put("k2", _make_selection())
        store.put("k1", _make_selection({"sec": "fresh"}))
        store.evict_oldest(max_size=1)
        assert store.get("k2") is None  # oldest
        fresh = store.get("k1")
        assert fresh is not None and fresh.chunks == {"sec": "fresh"}

    def test_touch_moves_key_to_back_of_eviction_order(self):
        # touch() marks a key most-recent; eviction order must follow (SQLite
        # gives it the next ``seq``), else a just-touched entry is dropped
        # first.
        store = InMemoryPendingStore()
        store.put("k1", _make_selection())
        store.put("k2", _make_selection())
        store.touch("k1")
        store.evict_oldest(max_size=1)
        assert store.get("k2") is None
        assert store.get("k1") is not None

    def test_delete_then_reput_does_not_leave_stale_order_entry(self):
        # delete() must drop the _order entry too: a stale entry plus a later
        # re-put would otherwise duplicate the key and resurface the
        # evict-the-fresh-entry bug through the delete path.
        store = InMemoryPendingStore()
        store.put("k1", _make_selection())
        store.delete("k1")
        store.put("k2", _make_selection())
        store.put("k1", _make_selection({"sec": "fresh"}))
        store.evict_oldest(max_size=1)
        assert store.get("k2") is None  # oldest live entry
        assert store.get("k1") is not None


# ── SQLitePendingStore ──────────────────────────────────────────────────


class TestSQLitePendingStore:
    def test_scoped_eviction_does_not_cross_delivery_formats(self, tmp_path):
        store = SQLitePendingStore(tmp_path / "pending.db")
        store.initialize()
        try:
            store.put("selective", _make_selection())
            store.put("progressive", _progressive_selection())

            store.evict_oldest(0, exclude_format="progressive")

            assert store.get("selective") is None
            assert store.get("progressive") is not None
        finally:
            store.close()

    def _make_store(self, tmp_path: Path) -> SQLitePendingStore:
        store = SQLitePendingStore(tmp_path / "pending.db")
        store.initialize()
        return store

    def test_put_and_get(self, tmp_path):
        store = self._make_store(tmp_path)
        sel = _make_selection({"a": "alpha", "b": "beta"})
        store.put("k1", sel)
        result = store.get("k1")
        assert result is not None
        assert result.chunks == {"a": "alpha", "b": "beta"}
        assert result.format == "markdown"
        assert result.total_chars == 100
        assert store.get("missing") is None
        store.close()

    def test_touch(self, tmp_path):
        store = self._make_store(tmp_path)
        store.put("k1", _make_selection())
        original = store.get("k1").created_at
        time.sleep(0.05)
        store.touch("k1")
        updated = store.get("k1").created_at
        assert updated > original
        store.close()

    def test_evict_expired(self, tmp_path):
        store = self._make_store(tmp_path)
        sel = _make_selection()
        store.put("k1", sel)
        # Manually set old timestamp
        store._get_db().execute(
            "UPDATE pending_selections SET created_at = ? WHERE key = ?",
            (time.time() - 1000, "k1"),
        )
        store._get_db().commit()
        store.put("k2", _make_selection())
        store.evict_expired(ttl=500)
        assert store.get("k1") is None
        assert store.get("k2") is not None
        store.close()

    def test_evict_oldest(self, tmp_path):
        # No sleep between puts: eviction ranks rows by insertion order rather
        # than by their timestamps (#901), so which keys survive does not
        # consult the clock at all and five rapid puts are deterministic
        # everywhere. Asserting WHICH ones — not just how many — is what makes
        # this an ordering test rather than a counting one.
        store = self._make_store(tmp_path)
        for i in range(5):
            store.put(f"k{i}", _make_selection())
        assert len(store) == 5
        store.evict_oldest(max_size=2)
        assert len(store) == 2
        assert {k for k in (f"k{i}" for i in range(5)) if store.get(k) is not None} == {"k3", "k4"}
        store.close()

    def test_persistence_across_reopen(self, tmp_path):
        """Data survives close + reopen."""
        db_path = tmp_path / "persist.db"
        store1 = SQLitePendingStore(db_path)
        store1.initialize()
        store1.put("k1", _make_selection({"x": "data"}))
        store1.close()

        store2 = SQLitePendingStore(db_path)
        store2.initialize()
        result = store2.get("k1")
        assert result is not None
        assert result.chunks == {"x": "data"}
        store2.close()

    def test_get_returns_none_on_corrupted_json(self, tmp_path, caplog):
        """Corrupted chunks_json should be treated as a cache miss, not crash."""
        store = self._make_store(tmp_path)
        # Bypass put() and insert invalid JSON directly
        store._get_db().execute(
            "INSERT INTO pending_selections "
            "(key, chunks_json, format, created_at, total_chars) "
            "VALUES (?, ?, ?, ?, ?)",
            ("bad", "{not valid json", "markdown", time.time(), 10),
        )
        store._get_db().commit()
        with caplog.at_level("WARNING"):
            assert store.get("bad") is None
        assert any("Corrupted chunks_json" in r.message for r in caplog.records)
        store.close()

    def test_concurrent_access(self, tmp_path):
        """Multiple threads can put/get without errors."""
        store = self._make_store(tmp_path)
        errors: list[Exception] = []

        def writer(tid: int):
            try:
                for i in range(20):
                    store.put(f"t{tid}_k{i}", _make_selection())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(store) == 60  # 3 threads × 20 entries
        store.close()


# ── SelectiveCompressor + Store integration ─────────────────────────────


class TestSelectiveCompressorWithStore:
    def test_inmemory_default(self):
        """Default compressor uses InMemoryPendingStore."""
        comp = SelectiveCompressor()
        assert isinstance(comp._store, InMemoryPendingStore)

    def test_inmemory_compress_and_select(self):
        """Basic compress → select cycle with InMemory."""
        comp = SelectiveCompressor()
        text = "# Title\n\n## A\n" + "Content A " * 50 + "\n\n## B\n" + "Content B " * 50 + "\n"
        result = comp.compress(text, max_chars=50)
        toc = json.loads(result)
        key = toc["selection_key"]
        selected = comp.select(key, ["A"])
        assert "Content A" in selected

    def test_sqlite_compress_and_select(self, tmp_path):
        """Compress → select cycle with SQLite store."""
        store = SQLitePendingStore(tmp_path / "sel.db")
        store.initialize()
        comp = SelectiveCompressor(store=store)
        text = "# Doc\n\n## X\n" + "Data X " * 50 + "\n\n## Y\n" + "Data Y " * 50 + "\n"
        result = comp.compress(text, max_chars=50)
        toc = json.loads(result)
        key = toc["selection_key"]
        selected = comp.select(key, ["X"])
        assert "Data X" in selected
        store.close()

    def test_multi_instance_shared_sqlite(self, tmp_path):
        """Two compressors sharing one SQLite DB can cross-select."""
        db_path = tmp_path / "shared.db"

        store1 = SQLitePendingStore(db_path)
        store1.initialize()
        comp1 = SelectiveCompressor(store=store1)

        store2 = SQLitePendingStore(db_path)
        store2.initialize()
        comp2 = SelectiveCompressor(store=store2)

        # comp1 creates a TOC
        text = (
            "# Title\n\n## Section1\n"
            + "Hello world " * 50
            + "\n\n## Section2\n"
            + "Goodbye " * 50
            + "\n"
        )
        result = comp1.compress(text, max_chars=50)
        toc = json.loads(result)
        key = toc["selection_key"]

        # comp2 can retrieve from the same DB
        selected = comp2.select(key, ["Section1"])
        assert "Hello world" in selected

        store1.close()
        store2.close()

    def test_config_memory_store(self):
        """pending_store='memory' creates InMemoryPendingStore."""
        from memtomem_stm.proxy.config import SelectiveConfig

        cfg = SelectiveConfig(pending_store="memory")
        assert cfg.pending_store == "memory"

    def test_config_sqlite_store(self, tmp_path):
        """pending_store='sqlite' creates SQLitePendingStore via manager helper."""
        from memtomem_stm.proxy.config import SelectiveConfig

        cfg = SelectiveConfig(
            pending_store="sqlite",
            pending_store_path=tmp_path / "test.db",
        )
        assert cfg.pending_store == "sqlite"

        # Simulate what ProxyManager._create_selective does
        store = SQLitePendingStore(cfg.pending_store_path)
        store.initialize()
        comp = SelectiveCompressor(
            max_pending=cfg.max_pending,
            pending_ttl_seconds=cfg.pending_ttl_seconds,
            store=store,
        )
        text = "# T\n\n## A\n" + "Content " * 50 + "\n\n## B\n" + "More " * 50 + "\n"
        result = comp.compress(text, max_chars=50)
        assert "selection_key" in result
        store.close()


# ── Restart reachability of the SQLite pending store (#583) ───────────────


def _sqlite_manager(tmp_path, *, override: bool = False):
    """A ProxyManager configured with a SQLite selective pending store, either
    at the server level or (``override=True``) only in a per-tool override."""
    from memtomem_stm.proxy.config import (
        ProxyConfig,
        SelectiveConfig,
        ToolOverrideConfig,
        UpstreamServerConfig,
    )
    from memtomem_stm.proxy.manager import ProxyManager
    from memtomem_stm.proxy.metrics import TokenTracker

    sel = SelectiveConfig(pending_store="sqlite", pending_store_path=tmp_path / "pending.db")
    if override:
        srv = UpstreamServerConfig(
            prefix="t", tool_overrides={"some_tool": ToolOverrideConfig(selective=sel)}
        )
    else:
        srv = UpstreamServerConfig(prefix="t", selective=sel)
    cfg = ProxyConfig(config_path=tmp_path / "proxy.json", upstream_servers={"srv": srv})
    return ProxyManager(cfg, TokenTracker()), sel


class TestSelectChunksRestartRecovery:
    def test_finds_key_persisted_before_restart(self, tmp_path):
        """A selection written by a prior process is reachable via select_chunks
        on a fresh manager, before any compress call rebuilds the compressor."""
        # Pre-restart: seed a selection directly into the SQLite store.
        store = SQLitePendingStore(tmp_path / "pending.db")
        store.initialize()
        store.put("k1", _make_selection({"A": "recovered content"}))
        store.close()

        # Post-restart: a brand-new manager, no compress call has run.
        mgr, sel = _sqlite_manager(tmp_path)
        assert mgr._selective_compressor is None
        result = mgr.select_chunks("k1", ["A"])
        assert result == "recovered content"
        assert mgr._selective_compressor_cfg == sel

    def test_override_only_sqlite_is_found(self, tmp_path):
        """The fallback scan reaches a SQLite store configured only in a per-tool
        override (server-level compression is memory/unset)."""
        store = SQLitePendingStore(tmp_path / "pending.db")
        store.initialize()
        store.put("k1", _make_selection({"A": "from override"}))
        store.close()

        mgr, _sel = _sqlite_manager(tmp_path, override=True)
        assert mgr.select_chunks("k1", ["A"]) == "from override"

    def test_no_sqlite_configured_returns_sentinel(self, tmp_path):
        """With no SQLite backend anywhere, the endpoint keeps the sentinel."""
        from memtomem_stm.proxy.config import ProxyConfig, UpstreamServerConfig
        from memtomem_stm.proxy.manager import ProxyManager
        from memtomem_stm.proxy.metrics import TokenTracker

        cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={"srv": UpstreamServerConfig(prefix="t")},
        )
        mgr = ProxyManager(cfg, TokenTracker())
        assert "not active" in mgr.select_chunks("k1", ["A"])

    def test_unopenable_path_degrades_without_caching(self, tmp_path):
        """A store that can't be opened degrades to the sentinel and pins no
        compressor (a failed open must not cache a bad state)."""
        from memtomem_stm.proxy.config import (
            ProxyConfig,
            SelectiveConfig,
            UpstreamServerConfig,
        )
        from memtomem_stm.proxy.manager import ProxyManager
        from memtomem_stm.proxy.metrics import TokenTracker

        # Point the DB path at a directory → sqlite open fails.
        bad_dir = tmp_path / "as_dir"
        bad_dir.mkdir()
        sel = SelectiveConfig(pending_store="sqlite", pending_store_path=bad_dir)
        cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={"srv": UpstreamServerConfig(prefix="t", selective=sel)},
        )
        mgr = ProxyManager(cfg, TokenTracker())
        assert "not active" in mgr.select_chunks("k1", ["A"])
        assert mgr._selective_compressor is None


class TestReadMoreRestartRecovery:
    def test_finds_progressive_key_persisted_before_restart(self, tmp_path):
        """A progressive response written by a prior process is reachable via
        read_more on a fresh manager (SQLite backend), rather than reporting
        'not found' from a fresh in-memory store."""
        from memtomem_stm.proxy.config import ProgressiveConfig

        # Pre-restart: write a progressive response through mgr1's SQLite store.
        mgr1, sel = _sqlite_manager(tmp_path)
        prog_cfg = ProgressiveConfig(chunk_size=40)
        text = "First chunk content. " * 20
        first = mgr1._apply_progressive(
            text, prog_cfg, "srv", "some_tool", sel, cfg_snap=mgr1._config
        )
        # Recover the key from the footer.
        import re

        m = re.search(r'key="([0-9a-f]{16})"', first)
        assert m is not None, first
        key = m.group(1)

        # Post-restart: a fresh manager over the same config finds the key.
        mgr2, _sel2 = _sqlite_manager(tmp_path)
        assert mgr2._progressive_store is None
        out = mgr2.read_more(key, len(text) // 4)
        assert "not found or expired" not in out

    def test_open_failure_degrades_to_sentinel(self, tmp_path):
        """A SQLite open failure in read_more recovery degrades to the sentinel
        and caches no store."""
        from memtomem_stm.proxy.config import (
            ProxyConfig,
            SelectiveConfig,
            UpstreamServerConfig,
        )
        from memtomem_stm.proxy.manager import ProxyManager
        from memtomem_stm.proxy.metrics import TokenTracker

        bad_dir = tmp_path / "as_dir"
        bad_dir.mkdir()
        sel = SelectiveConfig(pending_store="sqlite", pending_store_path=bad_dir)
        cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={"srv": UpstreamServerConfig(prefix="t", selective=sel)},
        )
        mgr = ProxyManager(cfg, TokenTracker())
        assert "not found or expired" in mgr.read_more("k1", 0)
        assert mgr._progressive_store is None

    def test_no_clobber_of_live_inmemory_store(self, tmp_path):
        """A key written to a live in-memory store (no sel_cfg) stays readable;
        read_more must not rebuild the store from the fallback cfg and lose it."""
        from memtomem_stm.proxy.config import ProgressiveConfig

        mgr, _sel = _sqlite_manager(tmp_path)
        prog_cfg = ProgressiveConfig(chunk_size=40)
        text = "In memory chunk. " * 20
        # Write WITHOUT sel_cfg → builds/caches an in-memory store.
        first = mgr._apply_progressive(
            text, prog_cfg, "srv", "some_tool", None, cfg_snap=mgr._config
        )
        import re

        m = re.search(r'key="([0-9a-f]{16})"', first)
        assert m is not None, first
        key = m.group(1)

        store_before = mgr._progressive_store
        out = mgr.read_more(key, len(text) // 4)
        assert "not found or expired" not in out
        # The live in-memory store was not rebuilt from the fallback cfg.
        assert mgr._progressive_store is store_before


# ── Multi-path recovery + store lifecycle (#583 follow-up) ────────────────


def _two_store_manager(tmp_path):
    """A ProxyManager with two servers, each configured to a DISTINCT SQLite
    pending store, so restart recovery must probe both to reach a key."""
    from memtomem_stm.proxy.config import (
        ProxyConfig,
        SelectiveConfig,
        UpstreamServerConfig,
    )
    from memtomem_stm.proxy.manager import ProxyManager
    from memtomem_stm.proxy.metrics import TokenTracker

    sel_a = SelectiveConfig(pending_store="sqlite", pending_store_path=tmp_path / "a.db")
    sel_b = SelectiveConfig(pending_store="sqlite", pending_store_path=tmp_path / "b.db")
    cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={
            "a": UpstreamServerConfig(prefix="a", selective=sel_a),
            "b": UpstreamServerConfig(prefix="b", selective=sel_b),
        },
    )
    return ProxyManager(cfg, TokenTracker()), sel_a, sel_b


class TestMultiPathRecovery:
    def test_select_chunks_probes_all_stores(self, tmp_path):
        """A key persisted in the SECOND of two distinct SQLite stores is
        reachable — recovery probes every configured path, not just the first."""
        store_b = SQLitePendingStore(tmp_path / "b.db")
        store_b.initialize()
        store_b.put("kB", _make_selection({"A": "in store B"}))
        store_b.close()

        mgr, _sel_a, sel_b = _two_store_manager(tmp_path)
        assert mgr.select_chunks("kB", ["A"]) == "in store B"
        # It bound the store that actually held the key, not merely the first.
        assert mgr._selective_compressor_cfg == sel_b

    def test_select_chunks_absent_key_falls_back_to_first(self, tmp_path):
        """When no configured store holds the key, recovery falls back to the
        first store so the key still degrades through the normal not-found
        path (and the common single-store case stays a straight passthrough)."""
        for name in ("a.db", "b.db"):
            s = SQLitePendingStore(tmp_path / name)
            s.initialize()
            s.close()

        mgr, sel_a, _sel_b = _two_store_manager(tmp_path)
        assert "not found" in mgr.select_chunks("missing", ["A"])
        assert mgr._selective_compressor_cfg == sel_a

    def test_read_more_probes_all_stores(self, tmp_path):
        """A progressive key persisted in the second store is reachable via
        read_more on a fresh manager."""
        import re

        from memtomem_stm.proxy.config import ProgressiveConfig

        mgr1, _sel_a, sel_b = _two_store_manager(tmp_path)
        text = "Second store chunk. " * 20
        first = mgr1._apply_progressive(
            text, ProgressiveConfig(chunk_size=40), "b", "tool", sel_b, cfg_snap=mgr1._config
        )
        m = re.search(r'key="([0-9a-f]{16})"', first)
        assert m is not None, first
        key = m.group(1)
        mgr1._progressive_store.close()  # release b.db before reopening

        mgr2, *_ = _two_store_manager(tmp_path)
        assert mgr2._progressive_store is None
        assert "not found or expired" not in mgr2.read_more(key, len(text) // 4)

    def test_select_chunks_reprobes_after_caching_other_store(self, tmp_path):
        """Even once a compressor is cached for the FIRST store (e.g. an earlier
        absent-key call), a later key that lives in the SECOND store is still
        reached — select_chunks re-probes on a miss instead of trusting the
        cached store's not-found."""
        store_b = SQLitePendingStore(tmp_path / "b.db")
        store_b.initialize()
        store_b.put("kB", _make_selection({"A": "in store B"}))
        store_b.close()

        mgr, sel_a, sel_b = _two_store_manager(tmp_path)
        # First call is for an absent key → caches the first store (a.db).
        assert "not found" in mgr.select_chunks("absent", ["A"])
        assert mgr._selective_compressor_cfg == sel_a
        # A later key that lives in b.db is still served, via the miss re-probe.
        assert mgr.select_chunks("kB", ["A"]) == "in store B"
        # The cache was NOT disturbed — the temp store served it.
        assert mgr._selective_compressor_cfg == sel_a

    def test_read_more_reprobes_after_caching_other_store(self, tmp_path):
        """read_more re-probes the other configured stores on a miss even after
        a progressive store is already cached for a different path."""
        import re

        from memtomem_stm.proxy.config import ProgressiveConfig

        # Persist a progressive key into b.db via a manager pointed at it.
        writer, _sa, sel_b = _two_store_manager(tmp_path)
        text = "Second store progressive. " * 20
        first = writer._apply_progressive(
            text, ProgressiveConfig(chunk_size=40), "b", "tool", sel_b, cfg_snap=writer._config
        )
        key = re.search(r'key="([0-9a-f]{16})"', first).group(1)
        writer._progressive_store.close()

        mgr, _sa2, _sb2 = _two_store_manager(tmp_path)
        # Force a cached progressive store for a.db via a miss on an absent key.
        assert "not found or expired" in mgr.read_more("absent", 0)
        cached = mgr._progressive_store
        assert cached is not None
        # The b.db key is still reachable through the miss re-probe...
        assert "not found or expired" not in mgr.read_more(key, len(text) // 4)
        # ...without disturbing the cached a.db adapter.
        assert mgr._progressive_store is cached


class TestStoreLifecycleCleanup:
    async def test_stop_closes_recovered_stores(self, tmp_path):
        """stop() closes the lazily-built selective and progressive stores and
        nulls them, so a recovery-opened SQLite connection does not leak."""
        from unittest.mock import patch

        from memtomem_stm.proxy.config import ProgressiveConfig

        store = SQLitePendingStore(tmp_path / "pending.db")
        store.initialize()
        store.put("k1", _make_selection({"A": "x"}))
        store.close()

        mgr, sel = _sqlite_manager(tmp_path)
        mgr.select_chunks("k1", ["A"])  # opens a SQLite-backed selective compressor
        mgr._apply_progressive(
            "y " * 50, ProgressiveConfig(chunk_size=40), "srv", "t", sel, cfg_snap=mgr._config
        )
        sel_comp = mgr._selective_compressor
        prog = mgr._progressive_store
        assert sel_comp is not None and prog is not None

        with (
            patch.object(sel_comp, "close", wraps=sel_comp.close) as sel_spy,
            patch.object(prog, "close", wraps=prog.close) as prog_spy,
        ):
            await mgr.stop()

        sel_spy.assert_called_once()
        prog_spy.assert_called_once()
        assert mgr._selective_compressor is None
        assert mgr._selective_compressor_cfg is None
        assert mgr._progressive_store is None
        assert mgr._progressive_store_cfg is None

    def test_rebuild_selective_closes_superseded(self, tmp_path):
        """Rebuilding the selective compressor for a changed config closes the
        superseded one first, so its SQLite store does not leak."""
        from unittest.mock import patch

        mgr, sel = _sqlite_manager(tmp_path)
        mgr._rebuild_selective_compressor(sel)
        first = mgr._selective_compressor
        assert first is not None

        with patch.object(first, "close", wraps=first.close) as spy:
            mgr._rebuild_selective_compressor(sel)

        spy.assert_called_once()
        assert mgr._selective_compressor is not first

    def test_progressive_rebuild_closes_superseded(self, tmp_path):
        """A progressive-store rebuild for a changed cfg closes the old adapter."""
        from unittest.mock import patch

        mgr, sel_a, sel_b = _two_store_manager(tmp_path)
        first = mgr._get_progressive_store(sel_a)
        assert first is not None

        with patch.object(first, "close", wraps=first.close) as spy:
            mgr._get_progressive_store(sel_b)  # different path → rebuild

        spy.assert_called_once()
        assert mgr._progressive_store is not first

    def test_rebuild_selective_failure_leaves_old_usable(self, tmp_path):
        """If building the replacement compressor fails (bad SQLite path), the
        old compressor stays cached and usable instead of a closed store behind
        the cfg-equality fast path."""
        from memtomem_stm.proxy.config import SelectiveConfig

        mgr, sel = _sqlite_manager(tmp_path)
        mgr._rebuild_selective_compressor(sel)
        first = mgr._selective_compressor
        assert first is not None

        bad_dir = tmp_path / "as_dir"
        bad_dir.mkdir()
        bad = SelectiveConfig(pending_store="sqlite", pending_store_path=bad_dir)
        with pytest.raises(Exception):
            mgr._rebuild_selective_compressor(bad)

        # Old compressor untouched: still cached, same cfg, and NOT closed —
        # a select on it still works (its store connection is live).
        assert mgr._selective_compressor is first
        assert mgr._selective_compressor_cfg == sel
        assert "not found or expired" in first.select("missing", ["A"])

    def test_progressive_rebuild_failure_leaves_old_usable(self, tmp_path):
        """A failed progressive-store rebuild leaves the old adapter cached and
        usable rather than closing it before the replacement is built."""
        from memtomem_stm.proxy.config import SelectiveConfig

        mgr, sel_a, _sel_b = _two_store_manager(tmp_path)
        first = mgr._get_progressive_store(sel_a)
        assert first is not None

        bad_dir = tmp_path / "as_dir"
        bad_dir.mkdir()
        bad = SelectiveConfig(pending_store="sqlite", pending_store_path=bad_dir)
        with pytest.raises(Exception):
            mgr._get_progressive_store(bad)

        assert mgr._progressive_store is first
        assert first.get("missing") is None  # still live, not closed


class TestLoneSurrogateDefenseInDepth:
    """These stores sit behind the ingest scrub, so a surrogate should never
    reach them in normal operation. They are hardened anyway because their
    failure mode is bad out of proportion to the cause (#761).

    ``sqlite3`` encodes text parameters to UTF-8, so the raise lands at
    ``execute`` time — and the guard around the only caller catches
    ``sqlite3.Error``, which a ``UnicodeEncodeError`` is not. It therefore
    escaped ``_call_tool_inner`` and discarded an otherwise-successful upstream
    response as an internal error.
    """

    def test_put_stores_a_surrogate_bearing_chunk(self, tmp_path: Path):
        store = SQLitePendingStore(tmp_path / "p.db")
        store.initialize()
        try:
            store.put("k", _make_selection({"sec": "x\ud800y"}))
            assert store.get("k") is not None
        finally:
            store.close()

    def test_get_never_returns_a_raw_surrogate(self, tmp_path: Path):
        """The write-side escape does not close the loop on its own: the reader
        is a plain ``json.loads``, which decodes a ``\\ud800`` escape sitting in
        the stored JSON straight back into the code unit. Written here with raw
        SQL because that is the only way such a row can exist — a hand-edited
        or externally-written DB."""
        store = SQLitePendingStore(tmp_path / "p.db")
        store.initialize()
        try:
            # Columns named, not positional: such a row comes from outside this
            # class by definition, so it is written the way an external writer
            # would — and it leaves ``seq`` unset, which is also what a process
            # running the pre-#901 code would do.
            store._get_db().execute(
                "INSERT OR REPLACE INTO pending_selections "
                "(key, chunks_json, format, created_at, total_chars) VALUES (?,?,?,?,?)",
                ("k", '{"sec": "x\\ud800y"}', "markdown", time.time(), 10),
            )
            store._get_db().commit()

            selection = store.get("k")

            assert selection is not None
            assert selection.chunks == {"sec": "x\\ud800y"}
            # The encode every downstream consumer performs on this text.
            json.dumps(selection.chunks, ensure_ascii=False).encode("utf-8")
        finally:
            store.close()

    def test_a_clean_round_trip_is_unchanged(self, tmp_path: Path):
        store = SQLitePendingStore(tmp_path / "p.db")
        store.initialize()
        try:
            chunks = {"sec": "ordinary 서버 🚀 text"}
            store.put("k", _make_selection(chunks))
            got = store.get("k")
            assert got is not None and got.chunks == chunks
        finally:
            store.close()


# ── eviction ranks rows by operation order, not by the clock (#901) ────


def _tie_all_created_at(store: SQLitePendingStore, value: float = 1000.0) -> None:
    """Give every row the same timestamp, the way a coarse clock does.

    Rows written inside one clock tick carry the same wall-clock ``created_at``
    in production — the state PR #900's Windows runner hit. Writing it
    explicitly makes the tests reach that state on any platform instead of
    hoping the local clock is coarse enough to produce it, and it removes the
    timestamp as a possible explanation for the survivors: what is left to
    decide them is the operation order.
    """
    store._get_db().execute("UPDATE pending_selections SET created_at = ?", (value,))
    store._get_db().commit()


class TestSQLiteEvictionOrder:
    """The SQLite twins of the in-memory ordering pins.

    ``InMemoryPendingStore`` has kept an explicit ``_order`` deque all along and
    is pinned by three tests above; the SQLite backend had no equivalents, which
    is how it shipped ordering by ``created_at`` — a clock reading standing in
    for the operation order it was meant to express.
    """

    def _store(self, tmp_path: Path) -> SQLitePendingStore:
        store = SQLitePendingStore(tmp_path / "pending.db")
        store.initialize()
        return store

    def _survivors(self, store: SQLitePendingStore, keys: tuple[str, ...]) -> set[str]:
        return {k for k in keys if store.get(k) is not None}

    def test_reput_counts_as_newest(self, tmp_path: Path):
        """Twin of test_reput_refreshes_recency_for_eviction."""
        store = self._store(tmp_path)
        try:
            store.put("k1", _make_selection())
            store.put("k2", _make_selection())
            store.put("k1", _make_selection({"sec": "fresh"}))
            _tie_all_created_at(store)

            store.evict_oldest(max_size=1)

            assert store.get("k2") is None
            fresh = store.get("k1")
            assert fresh is not None and fresh.chunks == {"sec": "fresh"}
        finally:
            store.close()

    def test_touch_counts_as_newest(self, tmp_path: Path):
        """Twin of test_touch_moves_key_to_back_of_eviction_order — and the
        reason ordering on rowid was not enough on its own.

        ``touch`` used to be an UPDATE, which keeps the row's rowid. A key that
        a reader had just selected would then rank as the LEAST recent row in
        the store and be the first evicted — the opposite of what touching a key
        means, and of what the in-memory backend does. Three keys with
        ``max_size=2`` separates the two orders: only a backend that treats the
        touched key as newest keeps k1 alongside k3.
        """
        store = self._store(tmp_path)
        try:
            for k in ("k1", "k2", "k3"):
                store.put(k, _make_selection())
            store.touch("k1")
            _tie_all_created_at(store)

            store.evict_oldest(max_size=2)

            assert self._survivors(store, ("k1", "k2", "k3")) == {"k1", "k3"}
        finally:
            store.close()

    def test_delete_then_reput_counts_as_newest(self, tmp_path: Path):
        """Twin of test_delete_then_reput_does_not_leave_stale_order_entry."""
        store = self._store(tmp_path)
        try:
            store.put("k1", _make_selection())
            store.delete("k1")
            store.put("k2", _make_selection())
            store.put("k1", _make_selection({"sec": "fresh"}))
            _tie_all_created_at(store)

            store.evict_oldest(max_size=1)

            assert store.get("k2") is None
            assert store.get("k1") is not None
        finally:
            store.close()

    def test_a_backward_clock_step_does_not_reorder_recency(self, tmp_path: Path):
        """Recency is the order operations happened in, not what the clock said.

        The wall clock can move backward — an NTP correction, a manual set, a
        container starting with a skewed clock against a store shared with a
        host whose clock is right. A later write then carries a SMALLER
        ``created_at`` than an earlier one, so ordering on the timestamp ranks
        it as the older row and the trim discards the key just written. A
        tiebreak behind ``created_at`` does not help: the timestamps differ, so
        the tiebreak is never consulted.
        """
        store = self._store(tmp_path)
        try:
            store.put("first_write", _make_selection())
            store.put("second_write", _make_selection())
            # The second write's clock reading lands before the first's.
            store._get_db().execute(
                "UPDATE pending_selections SET created_at = ? WHERE key = ?",
                (2000.0, "first_write"),
            )
            store._get_db().execute(
                "UPDATE pending_selections SET created_at = ? WHERE key = ?",
                (1000.0, "second_write"),
            )
            store._get_db().commit()

            store.evict_oldest(max_size=1)

            assert store.get("second_write") is not None, (
                "the later write was evicted because the clock had stepped back"
            )
            assert store.get("first_write") is None
        finally:
            store.close()

    def test_a_just_published_key_is_never_the_one_discarded(self, tmp_path: Path):
        """The #900 failure, reduced. Its Windows runner published a selection
        into a store at ``max_pending``, the new row tied with an older one, and
        the trim discarded the key the compress had just returned to the client.
        """
        store = self._store(tmp_path)
        try:
            for i in range(3):
                store.put(f"filler{i}", _make_selection())
            store.put("published", _make_selection())
            _tie_all_created_at(store)

            store.evict_oldest(max_size=2)

            assert store.get("published") is not None, (
                "the trim discarded the key that was just handed to the client"
            )
        finally:
            store.close()

    def test_both_backends_agree_on_the_survivors(self, tmp_path: Path):
        """The contract belongs to the Protocol, not to one implementation:
        the same sequence must keep the same keys on either backend.

        Three keys and ``max_size=2``, not two and one: with a single survivor
        the untied backend can agree by luck — the touched key happens to sit
        where an arbitrary tie order leaves it — and the test would pass against
        the defect it exists to catch.
        """
        sqlite_store = self._store(tmp_path)
        memory_store = InMemoryPendingStore()
        try:
            for store in (sqlite_store, memory_store):
                for k in ("a", "b", "c"):
                    store.put(k, _make_selection())
                store.touch("a")
            _tie_all_created_at(sqlite_store)

            for store in (sqlite_store, memory_store):
                store.evict_oldest(max_size=2)

            keys = ("a", "b", "c")
            assert self._survivors(sqlite_store, keys) == {"a", "c"}
            assert {k for k in keys if memory_store.get(k) is not None} == {"a", "c"}
        finally:
            sqlite_store.close()

    def test_an_existing_database_upgrades_in_place(self, tmp_path: Path):
        """A store written before ``seq`` existed must keep working, and its
        rows must keep the order they were written in — ``rowid`` carries that,
        so the migration seeds from it rather than leaving the column NULL."""
        db_path = tmp_path / "pending.db"
        self._legacy_db(db_path, ("old1", "old2", "old3"))

        store = SQLitePendingStore(db_path)
        store.initialize()
        try:
            assert store.get("old1") is not None, "the upgrade lost a row"
            seqs = dict(store._get_db().execute("SELECT key, seq FROM pending_selections"))
            assert all(v is not None for v in seqs.values()), "rows were left unranked"
            assert seqs["old1"] < seqs["old2"] < seqs["old3"], "write order was not preserved"

            store.put("new", _make_selection())
            store.evict_oldest(max_size=2)

            assert self._survivors(store, ("old1", "old2", "old3", "new")) == {"old3", "new"}
        finally:
            store.close()

    def _legacy_db(self, db_path: Path, keys: tuple[str, ...]) -> None:
        """A database as the pre-#901 code left it: five columns, no ``seq``."""
        legacy = sqlite3.connect(str(db_path))
        legacy.execute(
            """CREATE TABLE pending_selections (
                key TEXT PRIMARY KEY,
                chunks_json TEXT NOT NULL,
                format TEXT NOT NULL,
                created_at REAL NOT NULL,
                total_chars INTEGER NOT NULL
            )"""
        )
        for k in keys:
            legacy.execute(
                "INSERT INTO pending_selections VALUES (?,?,?,?,?)",
                (k, '{"sec": "x"}', "markdown", 1000.0, 1),
            )
        legacy.commit()
        legacy.close()

    def test_concurrent_openers_leave_one_consistent_schema(self, tmp_path: Path):
        """Four openers against one legacy file: none errors, every row ends up
        ranked, and the ranks stay unique as they then write through their own
        connections.

        Scope note: this does NOT deterministically reproduce the interleaving
        that makes the migration's atomicity load-bearing — it passes against a
        detect-then-write migration too, because the threads rarely land inside
        the window. The atomicity itself is pinned by
        ``test_migration_runs_in_one_immediate_transaction`` below; this test
        covers the outcome an operator sees.
        """
        db_path = tmp_path / "pending.db"
        self._legacy_db(db_path, ("old1", "old2"))

        stores = [SQLitePendingStore(db_path) for _ in range(4)]
        errors: list[BaseException] = []
        barrier = threading.Barrier(len(stores))

        def opener(store: SQLitePendingStore) -> None:
            try:
                barrier.wait(timeout=10)
                store.initialize()
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=opener, args=(s,)) for s in stores]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        try:
            assert not errors, f"concurrent initialize() raised: {errors}"

            ranks = [
                row[0] for row in stores[0]._get_db().execute("SELECT seq FROM pending_selections")
            ]
            assert all(r is not None for r in ranks), "a row was left unranked"
            assert len(set(ranks)) == len(ranks), f"duplicate ranks after the race: {ranks}"

            # Writes through the different connections must keep minting
            # distinct, increasing ranks.
            for i, store in enumerate(stores):
                store.put(f"new{i}", _make_selection())
            all_ranks = [
                row[0] for row in stores[0]._get_db().execute("SELECT seq FROM pending_selections")
            ]
            assert len(set(all_ranks)) == len(all_ranks), f"duplicate ranks: {all_ranks}"
        finally:
            for store in stores:
                store.close()

    def test_migration_runs_in_one_immediate_transaction(self, tmp_path: Path):
        """Detect, ALTER and backfill must be one write transaction, acquired
        before the schema check.

        Two openers that each merely *check* for the column can both find it
        missing and race the ALTER; and a writer landing between another's ALTER
        and its backfill gets a rank the backfill then overwrites with a
        rowid-derived one, colliding with a rank already handed out. Taking the
        write lock ahead of the check closes both.

        Pinned by recording the statements the migration issues, because the
        interleaving itself cannot be provoked deterministically from threads:
        the order and the transaction boundaries are the guarantee, so those are
        what this asserts.
        """
        db_path = tmp_path / "pending.db"
        self._legacy_db(db_path, ("old1",))

        statements: list[str] = []
        real_connect = sqlite3.connect

        def recording_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(lambda sql: statements.append(" ".join(sql.split())))
            return conn

        store = SQLitePendingStore(db_path)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sqlite3, "connect", recording_connect)
            store.initialize()
        try:

            def index_of(prefix: str) -> int:
                matches = [i for i, s in enumerate(statements) if s.startswith(prefix)]
                assert matches, f"no {prefix!r} statement in {statements}"
                return matches[0]

            begin = index_of("BEGIN IMMEDIATE")
            alter = index_of("ALTER TABLE")
            backfill = index_of("UPDATE pending_selections SET seq")
            checks = [i for i, s in enumerate(statements) if s.startswith("PRAGMA table_info")]
            assert len(checks) >= 2, (
                "the column must be checked twice — cheaply before the lock, "
                f"then authoritatively under it: {statements}"
            )
            assert any(begin < c < alter for c in checks), (
                "no schema check ran between taking the write lock and the "
                "ALTER, so an opener that waited for the lock while another "
                "migrated the file would migrate it a second time"
            )
            assert alter < backfill, (
                f"unexpected migration order: {statements[begin : backfill + 1]}"
            )
            # Order alone is not the guarantee: BEGIN, ALTER, COMMIT, backfill
            # has the right order and still leaves the backfill outside the
            # transaction. The span from BEGIN to the backfill must contain no
            # end-of-transaction at all, and the commit must come after it.
            span = statements[begin:backfill]
            assert not [s for s in span if s.startswith(("COMMIT", "ROLLBACK", "END"))], (
                f"the transaction ended before the backfill: {span}"
            )
            assert any(s.startswith("COMMIT") for s in statements[backfill:]), (
                "the migration never committed"
            )
            assert store.get("old1") is not None
        finally:
            store.close()

    def test_an_already_migrated_file_opens_without_the_write_lock(self, tmp_path: Path):
        """Opening is not always a write.

        ``select_chunks`` and ``read_more`` open throwaway stores purely to
        probe for a key. Taking the write lock on every open to discover that
        no migration is needed makes those probes wait out — or fail behind —
        an unrelated writer, which is a cost the migration has no business
        imposing once it has run. The column check has to be cheap first and
        locked only when it finds work.
        """
        db_path = tmp_path / "pending.db"
        SQLitePendingStore(db_path).initialize()  # first open migrates

        statements: list[str] = []
        real_connect = sqlite3.connect

        def recording_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(lambda sql: statements.append(" ".join(sql.split())))
            return conn

        store = SQLitePendingStore(db_path)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sqlite3, "connect", recording_connect)
            store.initialize()
        try:
            assert not [s for s in statements if s.startswith("BEGIN")], (
                f"a routine open took the write lock: {statements}"
            )
            assert store.get("missing") is None
        finally:
            store.close()

    def test_a_failed_backfill_leaves_the_old_schema(self, tmp_path: Path):
        """The other half of atomicity: if the backfill fails, the ALTER must go
        with it rather than leaving a column the rows were never ranked into.

        A trigger on the legacy table makes the backfill's UPDATE abort. SQLite
        rolls DDL back with the rest of the transaction, so the file must come
        back out as it went in — five columns, rows intact — and the next open
        must be able to migrate it for real.
        """
        db_path = tmp_path / "pending.db"
        self._legacy_db(db_path, ("old1", "old2"))
        setup = sqlite3.connect(str(db_path))
        setup.execute(
            "CREATE TRIGGER fail_backfill BEFORE UPDATE ON pending_selections "
            "BEGIN SELECT RAISE(ABORT, 'backfill blocked'); END"
        )
        setup.commit()
        setup.close()

        with pytest.raises(sqlite3.Error):
            SQLitePendingStore(db_path).initialize()

        after = sqlite3.connect(str(db_path))
        try:
            columns = [row[1] for row in after.execute("PRAGMA table_info(pending_selections)")]
            assert "seq" not in columns, f"a half-migrated schema was left behind: {columns}"
            assert after.execute("SELECT COUNT(*) FROM pending_selections").fetchone()[0] == 2
            after.execute("DROP TRIGGER fail_backfill")
            after.commit()
        finally:
            after.close()

        store = SQLitePendingStore(db_path)
        store.initialize()
        try:
            ranks = [
                row[0] for row in store._get_db().execute("SELECT seq FROM pending_selections")
            ]
            assert all(r is not None for r in ranks), "the retry did not migrate the file"
        finally:
            store.close()

    def test_an_upgraded_file_rejects_the_older_writer(self, tmp_path: Path):
        """The documented cost of the new column, pinned rather than assumed.

        A process still on the pre-#901 code inserts positionally into five
        columns, which the upgraded six-column table refuses. That is why
        instances sharing a ``pending_store_path`` have to be upgraded
        together. Its reads and its expiry sweep keep working, so an upgrade
        does not strand a key that is already out.
        """
        store = self._store(tmp_path)
        try:
            store.put("k", _make_selection())
            old = sqlite3.connect(str(tmp_path / "pending.db"))
            try:
                with pytest.raises(sqlite3.OperationalError, match="6 columns but 5 values"):
                    old.execute(
                        "INSERT OR REPLACE INTO pending_selections VALUES (?,?,?,?,?)",
                        ("legacy", '{"sec": "x"}', "markdown", 1000.0, 1),
                    )
                # Reads and the expiry sweep are unaffected.
                assert (
                    old.execute(
                        "SELECT chunks_json FROM pending_selections WHERE key = ?", ("k",)
                    ).fetchone()
                    is not None
                )
                old.execute("DELETE FROM pending_selections WHERE created_at < ?", (0.0,))
                old.commit()
            finally:
                old.close()
            assert store.get("k") is not None
        finally:
            store.close()

    def test_touch_does_not_rewrite_the_payload(self, tmp_path: Path):
        """``touch`` must re-rank a row without rewriting its payload.

        Progressive keeps an entire response body in ``chunks_json`` and
        ``read_more`` touches the row once per chunk, so a re-rank that rewrote
        the payload would rewrite the whole response on every chunk: 3956 ms
        against 461 ms for a 2 MB response read in 4 KB chunks.

        Four guards, because each alone is passable and each covers a shape the
        others miss:

        - ``UPDATE OF chunks_json`` catches an in-place rewrite —
          ``SET chunks_json = chunks_json`` costs the same as a real one and
          keeps the rowid, so the rowid check would not see it;
        - ``BEFORE DELETE`` catches an explicit delete-and-reinsert, which
          ``UPDATE OF`` does not fire on and which the rowid check can miss:
          on a sole row SQLite hands the reinsert the same rowid back;
        - ``BEFORE INSERT`` catches ``INSERT OR REPLACE``, whose implicit
          delete does NOT fire the DELETE trigger while ``recursive_triggers``
          is off (the default), and which can carry the rowid forward;
        - the rowid check catches a reinsert that lands somewhere else.
        """
        store = self._store(tmp_path)
        try:
            store.put("other", _make_selection())
            store.put("k", _make_selection({"__content__": "x" * 50_000}))
            store._get_db().execute(
                "CREATE TRIGGER no_payload_write BEFORE UPDATE OF chunks_json "
                "ON pending_selections BEGIN "
                "SELECT RAISE(ABORT, 'touch rewrote chunks_json'); END"
            )
            store._get_db().execute(
                "CREATE TRIGGER no_row_delete BEFORE DELETE ON pending_selections "
                "WHEN OLD.key = 'k' BEGIN "
                "SELECT RAISE(ABORT, 'touch deleted and reinserted the row'); END"
            )
            # Created last, so the fixture rows above are already in: any write
            # that re-inserts this key aborts here. INSERT OR REPLACE is the
            # reason it is needed — with recursive_triggers off (the default)
            # its implicit delete does NOT fire the DELETE trigger, and a
            # replacement that carries the rowid forward keeps the rowid check
            # happy too, so this is the only guard that sees it.
            store._get_db().execute(
                "CREATE TRIGGER no_row_insert BEFORE INSERT ON pending_selections "
                "WHEN NEW.key = 'k' BEGIN "
                "SELECT RAISE(ABORT, 'touch re-inserted the row'); END"
            )
            before = (
                store._get_db()
                .execute("SELECT rowid FROM pending_selections WHERE key = ?", ("k",))
                .fetchone()[0]
            )

            store.touch("k")

            after = (
                store._get_db()
                .execute("SELECT rowid FROM pending_selections WHERE key = ?", ("k",))
                .fetchone()[0]
            )
            assert after == before, "touch replaced the row instead of updating it in place"
        finally:
            store.close()

    def test_touch_preserves_the_row(self, tmp_path: Path):
        """``touch`` rewrites two narrow columns; the payload it carries must
        come through unchanged, and a missing key must stay a no-op."""
        store = self._store(tmp_path)
        try:
            chunks = {"sec": "서버 🚀 payload"}
            store.put("k1", _make_selection(chunks))
            before = store.get("k1")
            assert before is not None

            store.touch("k1")
            store.touch("absent")

            after = store.get("k1")
            assert after is not None
            assert after.chunks == chunks
            assert after.format == before.format
            assert after.total_chars == before.total_chars
            # Deliberately NOT asserting the timestamp advanced: this change
            # exists because the wall clock can move backward, so a test that
            # required it to move forward would contradict its own premise and
            # flake on an NTP correction. That touch re-ranks the row is
            # asserted through eviction, not through the clock.
            assert len(store) == 1, "touching an absent key must not create a row"
        finally:
            store.close()


# ── the retired generation's eviction policy (#898) ─────────────────────


class TestSupersededEvictionPolicy:
    """A pending write resolves the LIVE generation before it evicts.

    ``SelectiveCompressor._evict`` reads instance policy (``max_pending``,
    TTL) and applies it store-WIDE. When two generations share one
    ``pending_store_path`` — the common case for a config edit that leaves the
    path alone — a compress that started before the swap would otherwise run
    the superseded policy over the store the live generation is using and
    delete its keys.
    """

    def _shared_pair(
        self,
        tmp_path: Path,
        *,
        old_max: int = 1,
        old_ttl: float = 300.0,
        live_max: int = 100,
        live_ttl: float = 300.0,
    ):
        """G1 (superseded) and G2 (live), one SQLite file.

        Both policies are parameters: asserting only that the live keys SURVIVE
        would pass just as well if the publish skipped eviction altogether, so
        the tests below also give the live generation a policy tight enough to
        bite and check that it actually ran.
        """
        path = tmp_path / "pending.db"
        stores = []
        for _ in range(2):
            store = SQLitePendingStore(path)
            store.initialize()
            stores.append(store)

        live: list[SelectiveCompressor] = []
        g1 = SelectiveCompressor(
            max_pending=old_max,
            pending_ttl_seconds=old_ttl,
            store=stores[0],
            live_provider=lambda: live[0],
            publish_lock=threading.Lock(),
        )
        g2 = SelectiveCompressor(
            max_pending=live_max, pending_ttl_seconds=live_ttl, store=stores[1]
        )
        live.append(g2)
        return g1, g2

    def test_superseded_max_pending_does_not_evict_the_live_keys(self, tmp_path: Path):
        g1, g2 = self._shared_pair(tmp_path, old_max=1)
        try:
            for i in range(3):
                g2._store.put(f"live{i}", _make_selection())

            # G1's late write: on the retired instance's own policy this
            # evicts down to a single key across the shared file.
            g1.compress("## S\n" + "s" * 400, max_chars=200)

            survivors = [k for k in ("live0", "live1", "live2") if g2._store.get(k) is not None]
            assert survivors == ["live0", "live1", "live2"], (
                "a superseded max_pending evicted the live generation's keys"
            )
        finally:
            g1._store.close()
            g2._store.close()

    def test_superseded_ttl_does_not_expire_the_live_keys(self, tmp_path: Path):
        """The TTL half of the same policy: an old generation configured with a
        tiny ``pending_ttl_seconds`` must not expire keys the live generation
        still considers fresh."""
        g1, g2 = self._shared_pair(tmp_path, old_max=100, old_ttl=0.01)
        try:
            g2._store.put("live", _make_selection())
            time.sleep(0.05)

            g1.compress("## S\n" + "s" * 400, max_chars=200)

            assert g2._store.get("live") is not None, (
                "a superseded TTL expired a key the live generation still holds"
            )
        finally:
            g1._store.close()
            g2._store.close()

    def test_the_live_max_pending_is_the_one_that_runs(self, tmp_path: Path):
        """The positive half: the publish must APPLY the live generation's
        policy, not merely spare the live keys. With eviction dropped from the
        publish entirely, the surviving-keys assertions above would still pass —
        here G2's tight ``max_pending`` has to bite on its own store."""
        g1, g2 = self._shared_pair(tmp_path, old_max=100, live_max=2)
        try:
            # Plain puts: eviction ranks rows by the order they were written
            # (#901), so these three and the key published below have a defined
            # order without any clock manipulation. What is under test here is
            # WHOSE policy runs, not how the order is derived.
            for i in range(3):
                g2._store.put(f"live{i}", _make_selection())

            key = json.loads(g1.compress("## S\n" + "s" * 400, max_chars=200))["selection_key"]

            assert g2._store.get(key) is not None, "the new key must survive its own eviction"
            survivors = [k for k in ("live0", "live1", "live2") if g2._store.get(k) is not None]
            assert survivors == ["live2"], (
                f"expected G2's max_pending=2 to trim the store to the newest key "
                f"plus the one just published, got survivors={survivors}"
            )
        finally:
            g1._store.close()
            g2._store.close()

    def test_the_live_ttl_is_the_one_that_runs(self, tmp_path: Path):
        """The TTL twin of the test above: a live generation with a tiny TTL
        must expire its own stale keys when a retired generation publishes."""
        # The margins are deliberately wide: the store expires on wall time,
        # and Windows' clock granularity is ~15.6 ms. The stale key must age
        # well past the TTL while the key published below stays far short of it.
        g1, g2 = self._shared_pair(tmp_path, old_ttl=300.0, live_ttl=0.2)
        try:
            g2._store.put("stale", _make_selection())
            time.sleep(0.3)

            key = json.loads(g1.compress("## S\n" + "s" * 400, max_chars=200))["selection_key"]

            assert g2._store.get("stale") is None, "the live generation's TTL never ran"
            assert g2._store.get(key) is not None
        finally:
            g1._store.close()
            g2._store.close()

    def test_lone_generation_still_applies_its_own_policy(self, tmp_path: Path):
        """No provider (standalone construction) keeps the historical
        put-then-evict — the eviction must not become a no-op."""
        store = SQLitePendingStore(tmp_path / "pending.db")
        store.initialize()
        comp = SelectiveCompressor(max_pending=1, store=store)
        try:
            store.put("old", _make_selection())
            # No clock manipulation: this row is written before the compress
            # publishes its key, and eviction now ranks on that order (#901).
            # The explicit ageing this used to need was a workaround for the
            # tie that made it fail on the Windows runner, where the trim could
            # discard the key just handed to the caller.
            key = json.loads(comp.compress("## S\n" + "s" * 400, max_chars=200))["selection_key"]
            assert store.get("old") is None, "max_pending stopped being enforced"
            assert store.get(key) is not None
        finally:
            store.close()
