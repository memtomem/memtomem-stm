"""Tests for PendingStore backends and SelectiveCompressor integration."""

from __future__ import annotations

import json
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


# ── InMemoryPendingStore ────────────────────────────────────────────────


class TestInMemoryPendingStore:
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
        # A re-put key must count as the NEWEST entry (SQLite: INSERT OR
        # REPLACE rewrites created_at). A duplicate _order entry used to make
        # evict_oldest pop the re-put key first — dropping the fresh data.
        store = InMemoryPendingStore()
        store.put("k1", _make_selection())
        store.put("k2", _make_selection())
        store.put("k1", _make_selection({"sec": "fresh"}))
        store.evict_oldest(max_size=1)
        assert store.get("k2") is None  # oldest
        fresh = store.get("k1")
        assert fresh is not None and fresh.chunks == {"sec": "fresh"}

    def test_touch_moves_key_to_back_of_eviction_order(self):
        # touch() refreshes created_at; eviction order must follow (SQLite
        # orders by created_at), else a just-touched entry is dropped first.
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
        store = self._make_store(tmp_path)
        for i in range(5):
            store.put(f"k{i}", _make_selection())
            time.sleep(0.01)  # ensure different timestamps
        assert len(store) == 5
        store.evict_oldest(max_size=2)
        assert len(store) == 2
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
        first = mgr1._apply_progressive(text, prog_cfg, "srv", "some_tool", sel)
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
        first = mgr._apply_progressive(text, prog_cfg, "srv", "some_tool", None)
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
        first = mgr1._apply_progressive(text, ProgressiveConfig(chunk_size=40), "b", "tool", sel_b)
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
            text, ProgressiveConfig(chunk_size=40), "b", "tool", sel_b
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
        mgr._apply_progressive("y " * 50, ProgressiveConfig(chunk_size=40), "srv", "t", sel)
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
