"""SELECTIVE rows expire by the TTL they were published under (#1370).

Since #1365 every tool resolving to one pending-store backend shares one store.
Expiry used to apply ONE TTL to the whole store — the TTL of whichever
instance ran the sweep — so a write by a tool with a short
``pending_ttl_seconds`` deleted a longer-TTL tool's rows early, and
``stm_proxy_select_chunks`` expired rows by the TTL most recently resolved for
the store. Each row now carries its own ``ttl_seconds``; the caller's TTL is only
the fallback for a row written before that.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from memtomem_stm.proxy.compression import (
    HybridCompressor,
    PendingSelection,
    SelectiveCompressor,
)
from memtomem_stm.proxy.delivery import DeliveryBudgetExceeded, delivery_scope
from memtomem_stm.proxy.pending_store import InMemoryPendingStore, SQLitePendingStore

_TEXT = "\n\n".join(f"## S{i}\n{chr(97 + i) * 300}" for i in range(6))


class _Backend:
    """A store plus a way to age one of its rows by *seconds*."""

    def __init__(self, store: InMemoryPendingStore | SQLitePendingStore) -> None:
        self.store = store

    def age(self, key: str, seconds: float) -> None:
        if isinstance(self.store, InMemoryPendingStore):
            self.store._data[key].created_at -= seconds
        else:
            db = self.store._get_db()
            db.execute(
                "UPDATE pending_selections SET created_at = created_at - ? WHERE key = ?",
                (seconds, key),
            )
            db.commit()

    def set_raw_ttl(self, key: str, value: object) -> None:
        if isinstance(self.store, InMemoryPendingStore):
            self.store._data[key].ttl_seconds = value  # type: ignore[assignment]
        else:
            db = self.store._get_db()
            db.execute("UPDATE pending_selections SET ttl_seconds = ? WHERE key = ?", (value, key))
            db.commit()

    def has(self, key: str) -> bool:
        if isinstance(self.store, InMemoryPendingStore):
            return key in self.store._data
        row = (
            self.store._get_db()
            .execute("SELECT 1 FROM pending_selections WHERE key = ?", (key,))
            .fetchone()
        )
        return row is not None


@pytest.fixture(params=["memory", "sqlite"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[_Backend]:
    if request.param == "memory":
        yield _Backend(InMemoryPendingStore())
        return
    store = SQLitePendingStore(tmp_path / "pending.db")
    store.initialize()
    try:
        yield _Backend(store)
    finally:
        store.close()


def _key(out: object) -> str:
    descriptor = getattr(out, "descriptor", None)
    if isinstance(descriptor, dict):
        return descriptor["payload"]["key"]
    return json.loads(str(out))["selection_key"]


def _publish(compressor: SelectiveCompressor) -> str:
    return _key(compressor.compress(_TEXT, max_chars=200))


def _served(compressor: SelectiveCompressor, key: str) -> bool:
    return compressor.select_if_present(key, ["S5"]) is not None


def _selection(ttl: float | None) -> PendingSelection:
    return PendingSelection(
        chunks={"S0": "x" * 60},
        format="markdown",
        created_at=time.monotonic(),
        total_chars=60,
        ttl_seconds=ttl,
    )


# --- publication -----------------------------------------------------------


def test_a_short_ttl_write_keeps_a_long_ttl_row(backend: _Backend) -> None:
    """The issue's repro: before #1370 the 1 s tool's write expired the
    300 s tool's row store-wide."""
    owner = SelectiveCompressor(store=backend.store)
    long_ttl = owner.with_policy(pending_ttl_seconds=300.0)
    short_ttl = owner.with_policy(pending_ttl_seconds=1.0)
    kept = _publish(long_ttl)
    backend.age(kept, 5.0)

    _publish(short_ttl)

    assert backend.has(kept), "a shorter-TTL tool's write expired a longer-TTL row"


def test_a_short_ttl_row_still_expires_on_a_long_ttl_write(backend: _Backend) -> None:
    owner = SelectiveCompressor(store=backend.store)
    gone = _publish(owner.with_policy(pending_ttl_seconds=1.0))
    backend.age(gone, 5.0)

    _publish(owner.with_policy(pending_ttl_seconds=300.0))

    assert not backend.has(gone), "a longer-TTL writer kept a row past its own TTL"


def test_the_row_carries_the_ttl_its_toc_reports(backend: _Backend) -> None:
    """One source for both: structured reports the stored float; legacy keeps
    its existing ``int()`` truncation, which never outlives the row."""
    view = SelectiveCompressor(store=backend.store).with_policy(pending_ttl_seconds=2.5)

    legacy = view.compress(_TEXT, max_chars=200)
    legacy_key = _key(legacy)
    with delivery_scope("structured", 16000):
        structured = view.compress(_TEXT, max_chars=200)
    structured_key = _key(structured)

    stored = backend.store.get(structured_key)
    assert stored is not None and stored.ttl_seconds == 2.5
    assert structured.descriptor["payload"]["ttl_seconds_remaining"] == stored.ttl_seconds
    legacy_stored = backend.store.get(legacy_key)
    assert legacy_stored is not None and legacy_stored.ttl_seconds == 2.5
    assert json.loads(str(legacy))["ttl_seconds_remaining"] == int(legacy_stored.ttl_seconds)


def test_a_hybrid_row_carries_the_hybrid_tools_ttl(backend: _Backend) -> None:
    owner = SelectiveCompressor(store=backend.store)
    hybrid = HybridCompressor(
        head_chars=300, selective_compressor=owner.with_policy(pending_ttl_seconds=42.0)
    )
    out = hybrid.compress(_TEXT + "\n\n" + _TEXT.replace("## S", "## T"), max_chars=1200)
    key = json.loads(str(out)[str(out).index('{"type"') :])["selection_key"]
    stored = backend.store.get(key)
    assert stored is not None and stored.ttl_seconds == 42.0


# --- select ----------------------------------------------------------------


def test_select_serves_a_row_past_the_owners_shorter_ttl(backend: _Backend) -> None:
    owner = SelectiveCompressor(store=backend.store, pending_ttl_seconds=1.0)
    key = _publish(owner.with_policy(pending_ttl_seconds=300.0))
    backend.age(key, 5.0)

    assert _served(owner, key), "select expired a row by the owner's TTL, not its own"


def test_select_expires_a_row_before_the_owners_longer_ttl(backend: _Backend) -> None:
    owner = SelectiveCompressor(store=backend.store, pending_ttl_seconds=300.0)
    key = _publish(owner.with_policy(pending_ttl_seconds=1.0))
    backend.age(key, 5.0)

    assert not _served(owner, key), "select kept a row past the TTL its TOC advertised"


def test_a_successful_select_renews_the_row_under_its_own_ttl(backend: _Backend) -> None:
    owner = SelectiveCompressor(store=backend.store, pending_ttl_seconds=1.0)
    key = _publish(owner.with_policy(pending_ttl_seconds=300.0))
    backend.age(key, 200.0)
    assert _served(owner, key)  # renews: age back to ~0
    backend.age(key, 250.0)  # 450 s after publication, 250 s after renewal

    _publish(owner.with_policy(pending_ttl_seconds=1.0))  # a 1 s neighbour sweeps

    assert _served(owner, key)
    stored = backend.store.get(key)
    assert stored is not None and stored.ttl_seconds == 300.0, "renewal changed the row's TTL"


def test_a_select_matching_no_section_does_not_renew(backend: _Backend) -> None:
    owner = SelectiveCompressor(store=backend.store, pending_ttl_seconds=1.0)
    key = _publish(owner.with_policy(pending_ttl_seconds=300.0))
    backend.age(key, 200.0)
    assert "No matching sections" in str(owner.select(key, ["nope"]))
    backend.age(key, 150.0)  # 350 s since publication, no renewal

    assert not _served(owner, key)


def test_a_select_over_the_structured_budget_does_not_renew(backend: _Backend) -> None:
    owner = SelectiveCompressor(store=backend.store, pending_ttl_seconds=1.0)
    with delivery_scope("structured", 16000, continuation_budget=100):
        key = _publish(owner.with_policy(pending_ttl_seconds=300.0))
    backend.age(key, 200.0)
    with pytest.raises(DeliveryBudgetExceeded):
        owner.select(key, ["S5"])
    backend.age(key, 150.0)

    assert owner.select_if_present(key, ["S5"], "legacy") is None


# --- rows without an entry-local TTL ---------------------------------------


@pytest.mark.parametrize(("fallback", "kept"), [(1.0, False), (300.0, True)])
def test_a_row_without_a_ttl_expires_by_the_callers(
    backend: _Backend, fallback: float, kept: bool
) -> None:
    backend.store.put("legacy", _selection(None))
    backend.age("legacy", 5.0)

    backend.store.evict_expired(fallback, exclude_format="progressive")

    assert backend.has("legacy") is kept


@pytest.mark.parametrize(
    ("owner_ttl", "kept"),
    # Before #1370 select used the TTL of the latest route installed onto the
    # owner; a row with no TTL of its own now expires by the owner's own TTL.
    [(300.0, True), (1.0, False)],
)
def test_a_legacy_row_expires_by_the_owners_ttl_on_select(
    backend: _Backend, owner_ttl: float, kept: bool
) -> None:
    owner = SelectiveCompressor(store=backend.store, pending_ttl_seconds=owner_ttl)
    backend.store.put("legacy", _selection(None))
    backend.age("legacy", 5.0)

    assert (owner.select_if_present("legacy", ["S0"]) is not None) is kept


def test_a_legacy_row_selected_regularly_stays_a_fallback_row(backend: _Backend) -> None:
    """Renewal does not stamp a TTL, so a legacy row an agent keeps selecting
    stays judged by the fallback indefinitely — documented, not bounded."""
    owner = SelectiveCompressor(store=backend.store, pending_ttl_seconds=300.0)
    backend.store.put("legacy", _selection(None))
    for _ in range(5):
        backend.age("legacy", 200.0)
        assert owner.select_if_present("legacy", ["S0"]) is not None
    stored = backend.store.get("legacy")
    assert stored is not None and stored.ttl_seconds is None


# --- invalid entry-local TTLs ----------------------------------------------


@pytest.mark.parametrize("value", [-1.0, math.inf, -math.inf])
def test_an_invalid_ttl_expires_rather_than_falling_back(backend: _Backend, value: float) -> None:
    backend.store.put("bad", _selection(300.0))
    backend.set_raw_ttl("bad", value)

    backend.store.evict_expired(300.0, exclude_format="progressive")

    assert not backend.has("bad")


def test_a_text_ttl_written_by_hand_expires(tmp_path: Path) -> None:
    store = SQLitePendingStore(tmp_path / "pending.db")
    store.initialize()
    try:
        backend = _Backend(store)
        store.put("bad", _selection(300.0))
        backend.set_raw_ttl("bad", "abc")
        assert store.get("bad") is None, "a read before the sweep handed out an invalid row"

        store.evict_expired(300.0, exclude_format="progressive")

        assert not backend.has("bad")
    finally:
        store.close()


def test_a_nan_written_by_hand_is_stored_as_null_and_falls_back(tmp_path: Path) -> None:
    """REAL affinity stores NaN as NULL, so it cannot be told from a legacy
    row: the guarantee for foreign values is what the column holds."""
    store = SQLitePendingStore(tmp_path / "pending.db")
    store.initialize()
    try:
        backend = _Backend(store)
        store.put("nan", _selection(1.0))
        backend.set_raw_ttl("nan", math.nan)
        backend.age("nan", 5.0)
        store.evict_expired(300.0, exclude_format="progressive")
        assert backend.has("nan")
        stored = store.get("nan")
        assert stored is not None and stored.ttl_seconds is None
    finally:
        store.close()


def test_progressive_rows_are_untouched_by_the_selective_sweep(backend: _Backend) -> None:
    progressive = _selection(None)
    progressive.format = "progressive"
    backend.store.put("prog", progressive)
    backend.age("prog", 5.0)

    backend.store.evict_expired(1.0, exclude_format="progressive")

    assert backend.has("prog")


# --- SQLite schema ---------------------------------------------------------


def _pre_1370_db(path: Path, rows: Callable[[sqlite3.Connection], None]) -> None:
    """The schema as of #1371: seq and metadata_json, no ttl_seconds."""
    db = sqlite3.connect(str(path))
    db.execute(
        """CREATE TABLE pending_selections (
            key TEXT PRIMARY KEY,
            chunks_json TEXT NOT NULL,
            format TEXT NOT NULL,
            created_at REAL NOT NULL,
            total_chars INTEGER NOT NULL,
            seq INTEGER,
            metadata_json TEXT
        )"""
    )
    rows(db)
    db.commit()
    db.close()


def _old_insert(db: sqlite3.Connection, key: str, created_at: float) -> None:
    # The #1371 writer's statement: named columns, no ttl_seconds.
    db.execute(
        "INSERT OR REPLACE INTO pending_selections "
        "(key, chunks_json, format, created_at, total_chars, metadata_json, seq) "
        "VALUES (?, ?, ?, ?, ?, ?, "
        "(SELECT IFNULL(MAX(seq), 0) + 1 FROM pending_selections))",
        (key, '{"S0": "x"}', "markdown", created_at, 1, None),
    )


def test_a_pre_1370_file_gains_the_column_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "pending.db"
    _pre_1370_db(path, lambda db: _old_insert(db, "old", time.time()))

    for _ in range(2):  # reopening is idempotent
        store = SQLitePendingStore(path)
        store.initialize()
        try:
            columns = [
                r[1] for r in store._get_db().execute("PRAGMA table_info(pending_selections)")
            ]
            assert columns.count("ttl_seconds") == 1
            stored = store.get("old")
            assert stored is not None and stored.ttl_seconds is None
            store.evict_expired(300.0, exclude_format="progressive")
            assert store.get("old") is not None
        finally:
            store.close()


def test_the_previous_writer_still_writes_into_an_upgraded_file(tmp_path: Path) -> None:
    """Unlike #901's positional insert, the #1371 writer names its columns, so
    it keeps writing after the upgrade; its rows carry no TTL and fall back."""
    path = tmp_path / "pending.db"
    store = SQLitePendingStore(path)
    store.initialize()
    old = sqlite3.connect(str(path))
    try:
        _old_insert(old, "from-old", time.time() - 5.0)
        old.commit()
        stored = store.get("from-old")
        assert stored is not None and stored.ttl_seconds is None
        store.evict_expired(1.0, exclude_format="progressive")
        assert store.get("from-old") is None
    finally:
        old.close()
        store.close()


def test_the_ttl_column_is_added_under_the_write_lock(tmp_path: Path) -> None:
    """Same contract as the seq migration: a cheap check first, then BEGIN
    IMMEDIATE, an authoritative re-check, and the ALTER, committed."""
    path = tmp_path / "pending.db"
    _pre_1370_db(path, lambda db: None)
    statements: list[str] = []
    real_connect = sqlite3.connect

    def recording_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        conn.set_trace_callback(lambda sql: statements.append(" ".join(sql.split())))
        return conn

    store = SQLitePendingStore(path)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sqlite3, "connect", recording_connect)
        store.initialize()
    store.close()

    alter = next(i for i, s in enumerate(statements) if "ADD COLUMN ttl_seconds" in s)
    begin = max(i for i, s in enumerate(statements[:alter]) if s.startswith("BEGIN IMMEDIATE"))
    checks = [i for i, s in enumerate(statements[:alter]) if s.startswith("PRAGMA table_info")]
    assert any(c < begin for c in checks), f"no cheap pre-lock check: {statements}"
    assert any(begin < c for c in checks), f"no re-check under the lock: {statements}"
    assert not [s for s in statements[begin:alter] if s.startswith(("COMMIT", "ROLLBACK", "END"))]
    assert any(s.startswith("COMMIT") for s in statements[alter:]), "the ALTER never committed"
