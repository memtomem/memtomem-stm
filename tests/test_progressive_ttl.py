"""Entry-local progressive TTL, clock domains, and atomic renewal (#1155)."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from memtomem_stm.proxy import pending_store as stores
from memtomem_stm.proxy.compression import PendingSelection
from memtomem_stm.proxy.config import ProxyConfig, SelectiveConfig, UpstreamServerConfig
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.progressive import ProgressiveStoreAdapter


@pytest.fixture(params=["memory", "sqlite"])
def storage(request, tmp_path, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(
        stores,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock.now,
            time=lambda: clock.now + 100_000,
        ),
    )
    if request.param == "memory":
        backend = stores.InMemoryPendingStore()
    else:
        backend = stores.SQLitePendingStore(tmp_path / "pending.db")
        backend.initialize()
    try:
        yield backend, ProgressiveStoreAdapter(backend), clock
    finally:
        if isinstance(backend, stores.SQLitePendingStore):
            backend.close()


def seed(backend, clock, key="key", meta='{"ttl_seconds": 10}', content="synthetic payload"):
    backend.put(
        key,
        PendingSelection(
            chunks={"__content__": content, "__meta__": meta},
            format="progressive",
            created_at=clock.now,
            total_chars=len(content),
        ),
    )


@pytest.mark.parametrize("age,live", [(0, True), (9.999, True), (10, False), (100, False)])
def test_manager_expiry_before_renewal(storage, tmp_path, age, live):
    backend, adapter, clock = storage
    seed(backend, clock)
    manager = ProxyManager(ProxyConfig(config_path=tmp_path / "absent.json"), TokenTracker())
    manager._progressive_store = adapter
    clock.now += age
    result = manager.read_more("key", 0)
    assert ("synthetic payload" in result) is live
    if live:
        expected = clock.now + (100_000 if isinstance(backend, stores.SQLitePendingStore) else 0)
        assert backend.get("key").created_at == expected
    else:
        assert "not found or expired" in result
        assert backend.get("key") is None
        assert "not found or expired" in manager.read_more("key", 0)


def test_successful_read_renews_sliding_lifetime(storage, tmp_path):
    backend, adapter, clock = storage
    seed(backend, clock)
    manager = ProxyManager(ProxyConfig(config_path=tmp_path / "absent.json"), TokenTracker())
    manager._progressive_store = adapter
    clock.now += 9
    assert "synthetic payload" in manager.read_more("key", 0)
    clock.now += 9
    assert adapter.get("key") is not None  # get does not itself renew
    clock.now += 1
    assert adapter.get("key", renew=True) is None


@pytest.mark.parametrize(
    "ttl,age,publisher_ttl,live", [(10, 100, 1800, False), (3600, 120, 60, True)]
)
def test_publication_eviction_uses_each_entry_ttl(storage, ttl, age, publisher_ttl, live):
    backend, adapter, clock = storage
    seed(backend, clock, meta=json.dumps({"ttl_seconds": ttl}))
    # SELECTIVE entries are outside the progressive policy, even when old.
    backend.put(
        "selective",
        PendingSelection(
            chunks={"a": "keep"},
            format="markdown",
            created_at=clock.now,
            total_chars=4,
        ),
    )
    clock.now += age
    adapter.evict(publisher_ttl, 200)
    assert (backend.get("key") is not None) is live
    assert backend.get("selective") is not None


@pytest.mark.parametrize(
    "meta",
    [
        '{"ttl_seconds": 0}',
        '{"ttl_seconds": -1}',
        '{"ttl_seconds": true}',
        '{"ttl_seconds": null}',
        '{"ttl_seconds": "10"}',
        '{"ttl_seconds": NaN}',
        '{"ttl_seconds": Infinity}',
        '{"ttl_seconds": -Infinity}',
        '{"ttl_seconds": []}',
        '{"ttl_seconds": {}}',
        "{not json",
        "[]",
        "null",
        "1",
    ],
)
@pytest.mark.parametrize("action", ["read", "evict", "touch"])
def test_invalid_metadata_cannot_be_renewed(storage, meta, action):
    backend, adapter, clock = storage
    seed(backend, clock, meta=meta)
    if action == "read":
        assert adapter.get("key", renew=True) is None
    elif action == "evict":
        adapter.evict(1800, 200)
    else:
        adapter.touch("key")
    assert backend.get("key") is None


def test_legacy_missing_ttl_keeps_default(storage):
    backend, adapter, clock = storage
    seed(backend, clock, meta="{}")
    clock.now += 1799
    assert adapter.get("key").ttl_seconds == 1800
    clock.now += 1
    assert adapter.get("key") is None


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize("expired", [False, True])
def test_sqlite_restart_and_alternate_recovery(tmp_path, monkeypatch, legacy, alternate, expired):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(stores, "time", SimpleNamespace(time=lambda: clock.now + 100_000))
    path = tmp_path / "b.db"
    backend = stores.SQLitePendingStore(path)
    backend.initialize()
    seed(backend, clock, meta="{}" if legacy else '{"ttl_seconds": 10}')
    backend.close()
    configs = {
        "b": UpstreamServerConfig(
            prefix="b",
            selective=SelectiveConfig(
                pending_store="sqlite",
                pending_store_path=path,
            ),
        )
    }
    if alternate:
        configs["a"] = UpstreamServerConfig(
            prefix="a",
            selective=SelectiveConfig(
                pending_store="sqlite",
                pending_store_path=tmp_path / "a.db",
            ),
        )
    manager = ProxyManager(
        ProxyConfig(config_path=tmp_path / "absent.json", upstream_servers=configs), TokenTracker()
    )
    try:
        if alternate:
            assert "not found" in manager.read_more("absent", 0)
            cached = manager._progressive_store
        clock.now += (1800 if legacy else 10) if expired else 5
        result = manager.read_more("key", 0)
        assert ("synthetic payload" in result) is not expired
        if alternate:
            assert manager._progressive_store is cached
        check = stores.SQLitePendingStore(path)
        check.initialize()
        try:
            row = check.get("key")
            if expired:
                assert row is None
            else:
                assert row.created_at == clock.now + 100_000
        finally:
            check.close()
    finally:
        if manager._progressive_store:
            manager._progressive_store.close()


@pytest.mark.parametrize("operation", ["read", "evict"])
@pytest.mark.parametrize("refresh", ["touch", "replace"])
def test_sqlite_expiry_serializes_with_other_instance(tmp_path, monkeypatch, operation, refresh):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(stores, "time", SimpleNamespace(time=lambda: clock.now + 100_000))
    writer = stores.SQLitePendingStore(tmp_path / "shared.db")
    reader = stores.SQLitePendingStore(tmp_path / "shared.db")
    writer.initialize()
    reader.initialize()
    try:
        seed(writer, clock)
        clock.now += 20
        db = writer._get_db()
        db.execute("BEGIN IMMEDIATE")
        if refresh == "touch":
            db.execute(
                "UPDATE pending_selections SET created_at=? WHERE key='key'", (clock.now + 100_000,)
            )
        else:
            db.execute("DELETE FROM pending_selections WHERE key='key'")
            chunks = json.dumps({"__content__": "replacement", "__meta__": '{"ttl_seconds": 50}'})
            db.execute(
                "INSERT INTO pending_selections VALUES (?, ?, ?, ?, ?, ?)",
                ("key", chunks, "progressive", clock.now + 100_000, 11, 2),
            )
        begin = threading.Event()
        reader._get_db().set_trace_callback(
            lambda sql: begin.set() if sql == "BEGIN IMMEDIATE" else None
        )
        adapter = ProgressiveStoreAdapter(reader)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = (
                pool.submit(adapter.get, "key", renew=True)
                if operation == "read"
                else pool.submit(adapter.evict, 1, 200)
            )
            try:
                assert begin.wait(2), "expiry must acquire the write lock before reading"
            finally:
                db.commit()
            result = future.result(timeout=5)
        assert writer.get("key") is not None
        if operation == "read":
            assert result.content == (
                "replacement" if refresh == "replace" else "synthetic payload"
            )
    finally:
        writer.close()
        reader.close()


def test_renewal_keeps_sqlite_payload_and_recency(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(stores, "time", SimpleNamespace(time=lambda: clock.now + 100_000))
    backend = stores.SQLitePendingStore(tmp_path / "pending.db")
    backend.initialize()
    try:
        seed(backend, clock, "first")
        seed(backend, clock, "second")
        # Updating chunks_json would fire this trigger even if bytes are equal.
        backend._get_db().execute(
            "CREATE TRIGGER no_payload_rewrite BEFORE UPDATE OF chunks_json "
            "ON pending_selections BEGIN SELECT RAISE(ABORT, 'payload rewritten'); END"
        )
        adapter = ProgressiveStoreAdapter(backend)
        assert adapter.get("first", renew=True) is not None
        adapter.evict(1800, 1)
        assert backend.get("first") is not None
        assert backend.get("second") is None
    finally:
        backend.close()


def test_missing_metadata_is_not_a_default_lifetime(storage):
    backend, adapter, clock = storage
    backend.put(
        "key",
        PendingSelection(
            chunks={"__content__": "synthetic"},
            format="progressive",
            created_at=clock.now,
            total_chars=9,
        ),
    )
    assert adapter.get("key", renew=True) is None


def test_sqlite_renewal_from_another_process(tmp_path, monkeypatch):
    import subprocess
    import sys

    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(stores, "time", SimpleNamespace(time=lambda: clock.now + 100_000))
    path = tmp_path / "shared.db"
    backend = stores.SQLitePendingStore(path)
    backend.initialize()
    try:
        seed(backend, clock)
        code = """
from pathlib import Path
from types import SimpleNamespace
import sys
from memtomem_stm.proxy import pending_store as stores
from memtomem_stm.proxy.progressive import ProgressiveStoreAdapter
stores.time = SimpleNamespace(time=lambda: 101009.0)
backend = stores.SQLitePendingStore(Path(sys.argv[1]))
backend.initialize()
try:
    assert ProgressiveStoreAdapter(backend).get("key", renew=True) is not None
finally:
    backend.close()
"""
        result = subprocess.run(
            [sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        assert backend.get("key").created_at == 101009.0
        clock.now = 1018.0
        adapter = ProgressiveStoreAdapter(backend)
        assert adapter.get("key") is not None
        clock.now = 1019.0
        assert adapter.get("key") is None
    finally:
        backend.close()


def test_adapter_parses_metadata_once(storage, monkeypatch):
    backend, adapter, clock = storage
    seed(backend, clock)
    parsed = []
    original = stores._parse_progressive_metadata

    def recording(raw):
        parsed.append(raw)
        return original(raw)

    monkeypatch.setattr(stores, "_parse_progressive_metadata", recording)
    assert adapter.get("key", renew=True).content == "synthetic payload"
    assert len(parsed) == 1


@pytest.mark.parametrize("key", ["absent", "selective"])
def test_sqlite_progressive_miss_does_not_wait_on_writer(tmp_path, key):
    writer = stores.SQLitePendingStore(tmp_path / "shared.db")
    reader = stores.SQLitePendingStore(tmp_path / "shared.db")
    writer.initialize()
    reader.initialize()
    try:
        writer.put(
            "selective",
            PendingSelection(
                chunks={"a": "keep"},
                format="markdown",
                created_at=0,
                total_chars=4,
            ),
        )
        reader._get_db().execute("PRAGMA busy_timeout=0")
        writer._get_db().execute("BEGIN IMMEDIATE")
        assert ProgressiveStoreAdapter(reader).get(key, renew=True) is None
    finally:
        writer._get_db().rollback()
        writer.close()
        reader.close()


def test_alternate_recovery_with_cached_store_locked(tmp_path):
    import time

    a = SelectiveConfig(pending_store="sqlite", pending_store_path=tmp_path / "a.db")
    b = SelectiveConfig(pending_store="sqlite", pending_store_path=tmp_path / "b.db")
    manager = ProxyManager(
        ProxyConfig(
            config_path=tmp_path / "absent.json",
            upstream_servers={
                "a": UpstreamServerConfig(prefix="a", selective=a),
                "b": UpstreamServerConfig(prefix="b", selective=b),
            },
        ),
        TokenTracker(),
    )
    writer = stores.SQLitePendingStore(a.pending_store_path)
    remote = stores.SQLitePendingStore(b.pending_store_path)
    writer.initialize()
    remote.initialize()
    try:
        seed(remote, SimpleNamespace(now=time.monotonic()))
        cached = manager._get_progressive_store(a)
        cached._store._get_db().execute("PRAGMA busy_timeout=0")
        writer._get_db().execute("BEGIN IMMEDIATE")
        assert "synthetic payload" in manager.read_more("key", 0)
        assert manager._progressive_store is cached
    finally:
        writer._get_db().rollback()
        writer.close()
        remote.close()
        if manager._progressive_store:
            manager._progressive_store.close()


@pytest.mark.parametrize(
    "chunks",
    [
        "{broken",
        "null",
        "[]",
        '{"__content__":"x","__meta__": NaN}',
        '{"__content__":"x","__meta__": Infinity}',
        r'{"__content__":"x","__meta__":"\ud800"}',
        '{"__content__":"x","__meta__": {}}',
        '{"__content__":7,"__meta__":"{}"}',
        '{"__content__":"x"}',
    ],
)
def test_sqlite_eviction_guards_malformed_outer_json(tmp_path, chunks):
    import time

    backend = stores.SQLitePendingStore(tmp_path / "pending.db")
    backend.initialize()
    try:
        seed(backend, SimpleNamespace(now=time.monotonic()), "valid")
        db = backend._get_db()
        with db:
            db.execute(
                "INSERT INTO pending_selections VALUES (?, ?, ?, ?, ?, ?)",
                ("invalid", chunks, "progressive", time.time(), 1, 2),
            )
        ProgressiveStoreAdapter(backend).evict(1800, 200)
        assert db.execute("SELECT key FROM pending_selections ORDER BY key").fetchall() == [
            ("valid",)
        ]
    finally:
        backend.close()


def test_sqlite_eviction_does_not_decode_response_bodies(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(stores, "time", SimpleNamespace(time=lambda: clock.now + 100_000))
    backend = stores.SQLitePendingStore(tmp_path / "pending.db")
    backend.initialize()
    try:
        seed(backend, clock, "expired", content="x" * 1_000_000)
        seed(backend, clock, "live", meta='{"ttl_seconds": 3600}', content="x" * 1_000_000)
        clock.now += 100
        parsed = []
        original = stores._parse_progressive_metadata

        def recording(raw):
            parsed.append(raw)
            return original(raw)

        def no_body_decode(*args):
            pytest.fail("publication eviction decoded a full response body")

        monkeypatch.setattr(stores, "_parse_progressive_metadata", recording)
        monkeypatch.setattr(backend, "_decode_row", no_body_decode)
        ProgressiveStoreAdapter(backend).evict(1800, 200)
        assert len(parsed) == 2
        assert all(len(raw) < 100 for raw in parsed)
        assert backend._get_db().execute("SELECT key FROM pending_selections").fetchall() == [
            ("live",)
        ]
    finally:
        backend.close()
