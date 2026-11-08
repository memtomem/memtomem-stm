"""One progressive store per pending-store backend (#1362).

The manager used to cache a single progressive store and treat a publication
with no selective config as "reuse whatever is cached", so a tool configured
for the in-memory default could write into another server's SQLite file, and a
publication on a different backend (or merely a different selective config)
closed the store holding earlier keys.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from memtomem_stm.proxy.config import (
    ProgressiveConfig,
    ProxyConfig,
    SelectiveConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics import TokenTracker

_ROW_HAS = "SELECT 1 FROM pending_selections WHERE instr(chunks_json, ?) > 0"


def _key(out: object) -> str:
    match = re.search(r'key="([0-9a-f]{16})"', str(out))
    assert match is not None, out
    return match.group(1)


def _on_disk(db_path: Path, marker: str) -> bool:
    db = sqlite3.connect(db_path)
    try:
        return db.execute(_ROW_HAS, (marker * 100,)).fetchone() is not None
    finally:
        db.close()


def _manager(tmp_path: Path, servers: dict[str, UpstreamServerConfig] | None = None):
    cfg = ProxyConfig(
        enabled=True, config_path=tmp_path / "absent.json", upstream_servers=servers or {}
    )
    return ProxyManager(cfg, TokenTracker()), cfg


def _publish(mgr, cfg, marker: str, server: str, sel: SelectiveConfig | None) -> str:
    return _key(
        mgr._apply_progressive(
            marker * 10000,
            ProgressiveConfig(),
            server=server,
            tool="t",
            sel_cfg=sel,
            cfg_snap=cfg,
        )
    )


def _readable(mgr, key: str) -> bool:
    return "not found or expired" not in mgr.read_more(key, 100)


async def test_no_selective_publication_stays_off_another_servers_sqlite_file(tmp_path):
    mgr, cfg = _manager(tmp_path)
    disk = SelectiveConfig(pending_store="sqlite", pending_store_path=tmp_path / "pending.db")
    try:
        _publish(mgr, cfg, "a", "disk", disk)
        kb = _publish(mgr, cfg, "b", "mem", None)

        assert _on_disk(tmp_path / "pending.db", "a"), "positive control: sqlite row is on disk"
        assert not _on_disk(tmp_path / "pending.db", "b")
        assert _readable(mgr, kb)
    finally:
        await mgr.stop()


async def test_memory_key_survives_a_later_sqlite_publication(tmp_path):
    mgr, cfg = _manager(tmp_path)
    disk = SelectiveConfig(pending_store="sqlite", pending_store_path=tmp_path / "pending.db")
    try:
        km = _publish(mgr, cfg, "m", "mem", None)
        kd = _publish(mgr, cfg, "d", "disk", disk)
        km2 = _publish(mgr, cfg, "n", "mem", None)

        assert _readable(mgr, km)
        assert _readable(mgr, kd)
        assert _readable(mgr, km2)
    finally:
        await mgr.stop()


async def test_memory_backends_differing_in_unrelated_fields_share_one_store(tmp_path):
    mgr, cfg = _manager(tmp_path)
    try:
        k1 = _publish(mgr, cfg, "x", "s1", SelectiveConfig(max_pending=100))
        k2 = _publish(mgr, cfg, "y", "s2", SelectiveConfig(max_pending=50))
        k3 = _publish(mgr, cfg, "z", "s3", None)

        assert _readable(mgr, k1)
        assert _readable(mgr, k2)
        assert _readable(mgr, k3)
        assert len(mgr._progressive_stores) == 1
    finally:
        await mgr.stop()


async def test_hot_reload_removing_sqlite_selective_moves_publications_off_the_file(tmp_path):
    disk = SelectiveConfig(pending_store="sqlite", pending_store_path=tmp_path / "pending.db")
    mgr, old = _manager(tmp_path, {"srv": UpstreamServerConfig(prefix="s", selective=disk)})
    try:
        kd = _publish(mgr, old, "d", "srv", disk)

        new = old.model_copy(update={"upstream_servers": {"srv": UpstreamServerConfig(prefix="s")}})
        mgr._config_loader.seed(new)
        assert mgr._pin_is_live_generation(new)
        sel_new = new.upstream_servers["srv"].selective
        assert sel_new is None
        km = _publish(mgr, new, "m", "srv", sel_new)

        assert _on_disk(tmp_path / "pending.db", "d"), "positive control"
        assert not _on_disk(tmp_path / "pending.db", "m")
        assert _readable(mgr, kd), "the key published before the reload stays readable"
        assert _readable(mgr, km)
    finally:
        await mgr.stop()


async def _persist_key(tmp_path: Path, disk: SelectiveConfig, servers) -> str:
    writer, cfg = _manager(tmp_path, servers)
    key = _publish(writer, cfg, "p", "disk", disk)
    await writer.stop()
    return key


async def test_sole_sqlite_store_is_recovered_after_a_memory_publication(tmp_path, monkeypatch):
    disk = SelectiveConfig(pending_store="sqlite", pending_store_path=tmp_path / "pending.db")
    servers = {"disk": UpstreamServerConfig(prefix="d", selective=disk)}
    persisted = await _persist_key(tmp_path, disk, servers)

    mgr, cfg = _manager(tmp_path, servers)
    opened: list[object] = []
    factory = mgr._open_progressive_adapter

    def track(sel):
        adapter = factory(sel)
        opened.append(adapter)
        return adapter

    monkeypatch.setattr(mgr, "_open_progressive_adapter", track)
    try:
        km = _publish(mgr, cfg, "m", "mem", None)
        assert len(opened) == 1  # the in-memory store

        assert _readable(mgr, persisted)
        assert len(opened) == 2  # the sqlite store, opened once
        assert _readable(mgr, persisted)
        assert len(opened) == 2, "a follow-up read must not reopen the recovered store"
        assert _readable(mgr, km)
    finally:
        await mgr.stop()


async def test_recovery_opens_nothing_once_stop_has_begun(tmp_path, monkeypatch):
    """The window between ``_stopped = True`` and the stores being cleared:
    a cached memory store must not let a miss open the SQLite store (#1128)."""
    disk = SelectiveConfig(pending_store="sqlite", pending_store_path=tmp_path / "pending.db")
    servers = {"disk": UpstreamServerConfig(prefix="d", selective=disk)}
    persisted = await _persist_key(tmp_path, disk, servers)

    mgr, cfg = _manager(tmp_path, servers)
    _publish(mgr, cfg, "m", "mem", None)
    opened: list[object] = []
    monkeypatch.setattr(mgr, "_open_progressive_adapter", lambda sel: opened.append(sel))
    mgr._stopped = True
    try:
        assert not _readable(mgr, persisted)
        assert opened == []
    finally:
        mgr._stopped = False
        await mgr.stop()


async def test_stop_closes_every_backend_store_once(tmp_path):
    from unittest.mock import patch

    mgr, cfg = _manager(tmp_path)
    disk = SelectiveConfig(pending_store="sqlite", pending_store_path=tmp_path / "pending.db")
    _publish(mgr, cfg, "m", "mem", None)
    _publish(mgr, cfg, "d", "disk", disk)
    adapters = list(mgr._progressive_stores.values())
    assert len(adapters) == 2

    with (
        patch.object(adapters[0], "close", wraps=adapters[0].close) as first,
        patch.object(adapters[1], "close", wraps=adapters[1].close) as second,
    ):
        await mgr.stop()

    first.assert_called_once()
    second.assert_called_once()
    assert mgr._progressive_stores == {}
