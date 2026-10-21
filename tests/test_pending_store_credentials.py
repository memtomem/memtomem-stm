"""Credential-bearing selections never reach the SQLite pending store (#1148).

The verdict runs ``CREDENTIAL_PATTERNS`` over real bytes only: the response as
received (checked by the publisher) and the row as it would be written (checked
by ``SQLitePendingStore.put``). A refusal degrades the call the way a store
fault does: SELECTIVE/HYBRID to truncation, PROGRESSIVE to passthrough.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.compression import (
    HybridCompressor,
    SelectiveCompressor,
    SensitivePayloadNotStored,
)
from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProgressiveConfig,
    ProxyConfig,
    SelectiveConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.proxy.pending_store import InMemoryPendingStore, SQLitePendingStore

SECRET = "hunter2-" + "Q" * 30
PAD = "y" * 400

# Each payload carries a credential the pending file must not hold. The comment
# names which half of the verdict sees it; the store-side cases are matches the
# compressor creates (or decodes) that the response text does not have.
REFUSED = {
    # response side
    "password": json.dumps({"password": SECRET, "pad": PAD}),
    "nested_password": json.dumps({"db": {"password": SECRET}, "pad": PAD}),
    "nested_session_token": json.dumps({"db": {"session_token": SECRET}, "pad": PAD}),
    "nested_secret_access_key": json.dumps({"db": {"secret_access_key": SECRET}, "pad": PAD}),
    "nested_x_amz_security_token": json.dumps({"db": {"x-amz-security-token": SECRET}, "pad": PAD}),
    "duplicate_key": '{"password": "%s", "password": null, "pad": "%s"}' % (SECRET, PAD),
    # store side: the decoded key is written as ``"password": "..."``
    "escaped_key": '{"\\u0070assword": "%s", "pad": "%s"}' % (SECRET, PAD),
    # store side: the decoded value is written as ``password = ...``
    "escaped_value": '{"config": "password \\u003d %s", "pad": "%s"}' % (SECRET, PAD),
    # store side: a bare number under the label is written as a quoted string
    "numeric_password": '{"password": 12345678901234567890, "pad": "%s"}' % PAD,
}

ALLOWED = {
    "json_schema_property": json.dumps(
        {"properties": {"access_token": {"type": "string"}}, "pad": PAD}
    ),
    "prefixed_key": json.dumps({"tools.api_key": "healthy", "pad": PAD}),
    "working_directory": json.dumps({"pwd": "/home/user", "pad": PAD}),
    "email_only": json.dumps({"author": "someone@example.com", "pad": PAD}),
    # Only real bytes are scanned; no decoded view is synthesized. These match
    # neither the response nor the written row, though a decoded view would.
    # The first is the documented residual: the escaped label is written under
    # the flattened key ``db.password``, with its value in another string.
    "escaped_label_under_nested_object": '{"db": {"\\u0070assword": "%s"}, "pad": "%s"}'
    % (SECRET, PAD),
    # A literal backslash-u sequence in a value stays literal in the row too.
    "literal_escape_text": json.dumps({"note": "\\u0070assword = example", "pad": PAD}),
}


def _sqlite_store(tmp_path: Path) -> SQLitePendingStore:
    store = SQLitePendingStore(tmp_path / "pending.db")
    store.initialize()
    return store


def _file_bytes(tmp_path: Path) -> bytes:
    return b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("pending.db"))


def _row_count(store: SQLitePendingStore) -> int:
    return len(store)


def _marker(name: str) -> bytes:
    return b"12345678901234567890" if name == "numeric_password" else SECRET.encode()


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_sqlite_store_refuses_credential_bearing_selection(tmp_path, name):
    store = _sqlite_store(tmp_path)
    compressor = SelectiveCompressor(store=store)
    try:
        with pytest.raises(SensitivePayloadNotStored):
            compressor.compress(REFUSED[name], max_chars=100)
        assert _row_count(store) == 0
        assert _marker(name) not in _file_bytes(tmp_path)
    finally:
        store.close()


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_memory_store_keeps_credential_bearing_selection(name):
    """The default backend never writes a file, so nothing is refused."""
    compressor = SelectiveCompressor(store=InMemoryPendingStore())
    toc = json.loads(compressor.compress(REFUSED[name], max_chars=100))
    first = toc["entries"][0]["key"]
    assert "not found" not in compressor.select(toc["selection_key"], [first])


@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_sqlite_store_keeps_benign_selection(tmp_path, name):
    store = _sqlite_store(tmp_path)
    compressor = SelectiveCompressor(store=store)
    try:
        toc = json.loads(compressor.compress(ALLOWED[name], max_chars=100))
        assert store.get(toc["selection_key"]) is not None
    finally:
        store.close()


def test_store_checks_the_row_it_writes(tmp_path):
    """``put`` refuses on its own, for any publisher: the serialized row is what
    lands in the file."""
    from memtomem_stm.proxy.compression import PendingSelection

    store = _sqlite_store(tmp_path)
    try:
        with pytest.raises(SensitivePayloadNotStored):
            store.put(
                "k",
                PendingSelection(
                    chunks={"password": "123456789"}, format="json", created_at=0.0, total_chars=1
                ),
            )
        assert _row_count(store) == 0
    finally:
        store.close()


def test_memory_store_skips_the_response_scan(monkeypatch):
    calls = []
    import memtomem_stm.proxy.compression as compression

    real = compression.payload_has_credentials
    monkeypatch.setattr(
        compression,
        "payload_has_credentials",
        lambda text: calls.append(text) or real(text),
    )
    SelectiveCompressor(store=InMemoryPendingStore()).compress(REFUSED["password"], max_chars=100)
    assert calls == []


def test_sensitivity_source_covers_a_sliced_input(tmp_path):
    """A slice can hold the value while its label stays in the part not
    stored; the unsliced source decides."""
    store = _sqlite_store(tmp_path)
    try:
        tail = "\n\n".join(f"Paragraph {i} " + "word " * 30 for i in range(6))
        with pytest.raises(SensitivePayloadNotStored):
            SelectiveCompressor(store=store).compress(
                tail, max_chars=100, sensitivity_source=f"password: {SECRET}\n\n{tail}"
            )
        assert _row_count(store) == 0
    finally:
        store.close()


def test_hybrid_passes_the_unsliced_response(tmp_path):
    store = _sqlite_store(tmp_path)
    try:
        sections = "\n\n".join(f"## Section {i}\n" + "word " * 40 for i in range(20))
        text = f"password: {SECRET}\n\n{sections}"
        hybrid = HybridCompressor(
            head_chars=200, selective_compressor=SelectiveCompressor(store=store)
        )
        with pytest.raises(SensitivePayloadNotStored):
            hybrid.compress(text, max_chars=1500)
        assert _row_count(store) == 0
    finally:
        store.close()


def _live_pair(own_store, live_store):
    """An instance whose publication retargets to a live generation (#898)."""
    lock = threading.Lock()
    live = SelectiveCompressor(store=live_store, publish_lock=lock)
    live._live_provider = lambda: live
    own = SelectiveCompressor(store=own_store, live_provider=lambda: live, publish_lock=lock)
    return own


def test_retarget_onto_a_persisting_store_is_refused(tmp_path):
    store = _sqlite_store(tmp_path)
    try:
        own = _live_pair(InMemoryPendingStore(), store)
        with pytest.raises(SensitivePayloadNotStored):
            own.compress(REFUSED["password"], max_chars=100)
        assert _row_count(store) == 0
    finally:
        store.close()


def test_retarget_onto_memory_store_is_kept(tmp_path):
    store = _sqlite_store(tmp_path)
    live_store = InMemoryPendingStore()
    try:
        own = _live_pair(store, live_store)
        toc = json.loads(own.compress(REFUSED["password"], max_chars=100))
        assert live_store.get(toc["selection_key"]) is not None
        assert _row_count(store) == 0
    finally:
        store.close()


# ── manager degrade paths ────────────────────────────────────────────────

_CREATED: list[ProxyManager] = []


@pytest.fixture(autouse=True)
async def _close_managers():
    first = len(_CREATED)
    try:
        yield
    finally:
        for mgr in reversed(_CREATED[first:]):
            await mgr.stop()
        del _CREATED[first:]


def _manager(
    tmp_path: Path,
    compression: CompressionStrategy,
    *,
    max_result_chars: int,
    server: str = "srv",
    max_stored: int = 200,
    chunk_size: int = 500,
    min_retention: float = 0.0,
):
    metrics = MetricsStore(tmp_path / "metrics.db")
    metrics.initialize()
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=compression,
        max_result_chars=max_result_chars,
        max_retries=0,
        reconnect_delay_seconds=0.0,
        progressive=ProgressiveConfig(chunk_size=chunk_size, max_stored=max_stored),
        selective=SelectiveConfig(
            pending_store="sqlite", pending_store_path=tmp_path / "pending.db"
        ),
    )
    mgr = ProxyManager(
        ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={server: server_cfg},
            min_result_retention=min_retention,
        ),
        TokenTracker(metrics_store=metrics),
    )
    _CREATED.append(mgr)
    mgr._connections[server] = UpstreamConnection(
        name=server, config=server_cfg, session=AsyncMock(), tools=[]
    )
    cache = ProxyCache(tmp_path / "cache.db", max_entries=100)
    cache.initialize()
    mgr._cache = cache
    return mgr, metrics, cache


def _respond(mgr: ProxyManager, text: str, server: str = "srv") -> None:
    mgr._connections[server].session.call_tool.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], is_error=False
    )


def _strategy(metrics: MetricsStore) -> str:
    return metrics._db.execute(
        "SELECT compression_strategy FROM proxy_metrics ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]


def _pending_rows(tmp_path: Path) -> int:
    probe = _sqlite_store(tmp_path)
    try:
        return len(probe)
    finally:
        probe.close()


async def test_selective_refusal_degrades_to_truncation(tmp_path, caplog):
    mgr, metrics, cache = _manager(tmp_path, CompressionStrategy.SELECTIVE, max_result_chars=600)
    sections = "\n\n".join(f"## Section {i}\n" + "word " * 40 for i in range(12))
    _respond(mgr, f"# Doc\n\npassword: {SECRET}\n\n{sections}")
    with caplog.at_level(logging.INFO, logger="memtomem_stm.proxy.manager"):
        result = await mgr.call_tool("srv", "tool", {})
    assert '"selection_key"' not in result
    assert _strategy(metrics) == "selective→truncate_on_sensitive"
    assert _pending_rows(tmp_path) == 0
    assert SECRET.encode() not in _file_bytes(tmp_path)
    assert cache.stats()["total_entries"] == 0
    refusals = [r for r in caplog.records if "not written to the persistent" in r.message]
    assert [r.levelno for r in refusals] == [logging.INFO]
    assert refusals[0].exc_info is None
    cache.close()
    metrics.close()


async def test_progressive_refusal_degrades_to_passthrough(tmp_path):
    """The quoted pair is written with escaped quotes inside the row, so only
    the response-side check sees it."""
    mgr, metrics, cache = _manager(
        tmp_path, CompressionStrategy.PROGRESSIVE, max_result_chars=50000
    )
    text = "line of content\n" * 100 + f'{{"password": "{SECRET}"}}\n' + "tail\n" * 50
    _respond(mgr, text)
    evictions = []
    real_get = mgr._get_progressive_store

    def spy(*args, **kwargs):
        adapter = real_get(*args, **kwargs)
        adapter.evict = lambda *a, **kw: evictions.append(a)
        return adapter

    mgr._get_progressive_store = spy
    result = await mgr.call_tool("srv", "tool", {})
    assert SECRET in result
    assert "stm_proxy_read_more" not in result
    assert _strategy(metrics) == "progressive→passthrough_on_sensitive"
    assert evictions == []
    assert _pending_rows(tmp_path) == 0
    assert SECRET.encode() not in _file_bytes(tmp_path)
    assert cache.stats()["total_entries"] == 0
    cache.close()
    metrics.close()


async def test_progressive_benign_response_still_stored(tmp_path):
    mgr, metrics, cache = _manager(
        tmp_path, CompressionStrategy.PROGRESSIVE, max_result_chars=50000
    )
    _respond(mgr, "line of content\n" * 200)
    result = await mgr.call_tool("srv", "tool", {})
    assert "stm_proxy_read_more" in result
    assert _strategy(metrics) == "progressive"
    assert _pending_rows(tmp_path) == 1
    cache.close()
    metrics.close()


async def test_row_only_selective_refusal_is_not_cached(tmp_path):
    """The response text matches nothing (the label is JSON-escaped), so the
    cache's own privacy scan would store the truncation; only the degrade flag
    keeps it out."""
    mgr, metrics, cache = _manager(tmp_path, CompressionStrategy.SELECTIVE, max_result_chars=600)
    fields = ", ".join(f'"field{i}": "{"word " * 30}"' for i in range(8))
    _respond(mgr, '{"\\u0070assword": "%s", %s}' % (SECRET, fields))
    result = await mgr.call_tool("srv", "tool", {})
    assert '"selection_key"' not in result
    assert _strategy(metrics) == "selective→truncate_on_sensitive"
    assert _pending_rows(tmp_path) == 0
    assert cache.stats()["total_entries"] == 0
    cache.close()
    metrics.close()


async def test_row_only_progressive_refusal_keeps_existing_keys(tmp_path):
    """The response is benign but the row's metadata carries the server name.
    The row is refused before eviction runs, so keys already handed out
    survive, and the passthrough is not cached."""
    server = "password=source"
    mgr, metrics, cache = _manager(
        tmp_path,
        CompressionStrategy.PROGRESSIVE,
        max_result_chars=50000,
        server=server,
        max_stored=1,
    )
    store = _sqlite_store(tmp_path)
    try:
        from memtomem_stm.proxy.progressive import ProgressiveResponse, ProgressiveStoreAdapter

        adapter = ProgressiveStoreAdapter(store)
        for key in ("a", "b"):
            adapter.put(
                key,
                ProgressiveResponse(
                    content="x" * 600,
                    total_chars=600,
                    total_lines=0,
                    content_type="text",
                    structure_hint="",
                    created_at=0.0,
                    ttl_seconds=1800.0,
                ),
            )
        assert len(store) == 2
    finally:
        store.close()
    _respond(mgr, "line of content\n" * 200, server=server)
    result = await mgr.call_tool(server, "tool", {})
    assert "stm_proxy_read_more" not in result
    assert _strategy(metrics) == "progressive→passthrough_on_sensitive"
    assert _pending_rows(tmp_path) == 2
    assert cache.stats()["total_entries"] == 0
    cache.close()
    metrics.close()


def _refuse(*args, **kwargs):
    raise SensitivePayloadNotStored("refused")


def _fail(*args, **kwargs):
    raise RuntimeError("store down")


@pytest.mark.parametrize(
    ("tier", "refusal", "cached"),
    [
        ("hybrid", _refuse, 0),
        ("progressive", _refuse, 0),
        # control: an ordinary tier failure keeps the truncation cacheable
        ("hybrid", _fail, 1),
    ],
)
async def test_ratio_ladder_refusal_keeps_truncation_out_of_cache(tmp_path, tier, refusal, cached):
    """A refusal inside the ratio-guard ladder falls through to truncation like
    any tier failure, but that truncation must not be cached: the response
    carried a credential the cache's own scan may not see (a row-only match)."""
    mgr, metrics, cache = _manager(
        tmp_path,
        CompressionStrategy.TRUNCATE,
        max_result_chars=2000,
        chunk_size=500 if tier == "progressive" else 100_000,
        min_retention=0.9,
    )
    body = "\n\n".join(f"## Section {i}\n" + "word " * 60 for i in range(10))
    _respond(mgr, f"# Doc\n\n{body}")
    mgr._apply_compression = AsyncMock(return_value=("tiny", None))
    if tier == "hybrid":
        mgr._apply_hybrid = AsyncMock(side_effect=refusal)
    else:
        mgr._apply_progressive = refusal
    await mgr.call_tool("srv", "tool", {})
    assert _strategy(metrics) == "truncate→truncate_fallback"
    assert cache.stats()["total_entries"] == cached
    cache.close()
    metrics.close()
