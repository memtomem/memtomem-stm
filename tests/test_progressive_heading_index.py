"""Work-count and output contracts for response-owned heading indexes (#1153)."""

import json
import re
import time

import pytest

from memtomem_stm.proxy import heading_index as headings
from memtomem_stm.proxy import progressive
from memtomem_stm.proxy.compression import PendingSelection
from memtomem_stm.proxy.config import ProxyConfig
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.pending_store import InMemoryPendingStore, SQLitePendingStore
from memtomem_stm.proxy.progressive import (
    ProgressiveChunker,
    ProgressiveResponse,
    ProgressiveStoreAdapter,
)


@pytest.fixture(params=["memory", "sqlite"])
def backend(request, tmp_path):
    store = (
        InMemoryPendingStore()
        if request.param == "memory"
        else SQLitePendingStore(tmp_path / "p.db")
    )
    if isinstance(store, SQLitePendingStore):
        store.initialize()
    yield store
    if isinstance(store, SQLitePendingStore):
        store.close()


def response(content, hints=True):
    return ProgressiveResponse(
        content=content,
        total_chars=len(content),
        total_lines=0,
        content_type="text",
        structure_hint="",
        created_at=time.monotonic(),
        chunk_size=40,
        include_structure_hint=hints,
    )


def old_hint(content, offset):
    start = offset - 1 if offset > 0 and content[offset - 1] == "\n" else offset
    titles = (
        re.findall(r"(?:^|\n)#{1,6}\s+(.+)", content[start:])
        if start == 0
        else re.compile(r"(?:^|\n)#{1,6}\s+(.+)").findall(content, start)
    )
    result = ", ".join(f'"{h.strip()}"' for h in titles[:5])
    if len(titles) > 5:
        result += f", ... (+{len(titles) - 5} more)"
    return result


@pytest.mark.parametrize(
    "content",
    [
        "ordinary text\n" * 100,
        "# first\nbody\n" + "## title \nmore text\n" * 12,
        "# \n# nested\n## last\n",
        "###\t\n\n# title\n## next\n",
        "x\n# 한글 제목\n" * 8,
    ],
)
def test_index_preserves_old_hints_at_every_offset(content):
    index = headings.build_heading_index(content)
    assert headings.valid_heading_index(index, len(content))
    for offset in range(len(content) + 1):
        assert headings.remaining_headings(content, offset, index) == old_hint(content, offset)


@pytest.mark.parametrize("content", ["plain line\n" * 50, "# title\nbody\n" * 50])
@pytest.mark.parametrize("legacy", [False, True])
def test_manager_reuses_index_with_fresh_chunkers(backend, tmp_path, monkeypatch, content, legacy):
    builds = []
    original = headings.build_heading_index

    def counted(text):
        builds.append(len(text))
        return original(text)

    monkeypatch.setattr(headings, "build_heading_index", counted)
    monkeypatch.setattr(progressive, "build_heading_index", counted)
    adapter = ProgressiveStoreAdapter(backend)
    if legacy:
        backend.put(
            "key",
            PendingSelection(
                chunks={"__content__": content, "__meta__": json.dumps({"chunk_size": 40})},
                format="progressive",
                created_at=time.monotonic(),
                total_chars=len(content),
            ),
        )
    else:
        adapter.put("key", response(content))
    manager = ProxyManager(ProxyConfig(config_path=tmp_path / "absent.json"), TokenTracker())
    manager._progressive_store = adapter
    before = backend.get("key").created_at
    # Ordinary non-renewing reads may repair metadata but cannot refresh TTL.
    adapter.get("key")
    assert backend.get("key").created_at == before
    for offset in (0, 80, 41, 0, 200):
        assert "[progressive:" in manager.read_more("key", offset)
    assert builds == [len(content)]
    meta = json.loads(backend.get("key").chunks["__meta__"])
    assert headings.INDEX_KEY in meta  # includes the cached empty result


def test_legacy_index_survives_sqlite_reopen(tmp_path, monkeypatch):
    path = tmp_path / "p.db"
    content = "# title\nbody\n" * 50
    store = SQLitePendingStore(path)
    store.initialize()
    store.put(
        "key",
        PendingSelection(
            chunks={"__content__": content, "__meta__": "{}"},
            format="progressive",
            created_at=0,
            total_chars=len(content),
        ),
    )
    assert ProgressiveStoreAdapter(store).get("key").heading_index
    store.close()

    def forbidden(_):
        pytest.fail("persisted heading index was rebuilt")

    monkeypatch.setattr(headings, "build_heading_index", forbidden)
    monkeypatch.setattr(progressive, "build_heading_index", forbidden)
    for _ in range(2):
        reopened = SQLitePendingStore(path)
        reopened.initialize()
        try:
            resp = ProgressiveStoreAdapter(reopened).get("key")
            chunker = ProgressiveChunker(40, heading_index=resp.heading_index)
            assert "Remaining:" in chunker.read_chunk(content, 0)
        finally:
            reopened.close()


def test_replacement_and_eviction_own_index_lifetime(backend):
    adapter = ProgressiveStoreAdapter(backend)
    adapter.put("key", response("# old\nbody\n" * 10))
    adapter.put("key", response("plain body\n" * 10))
    assert adapter.get("key").heading_index == ()
    adapter.evict(1800, 0)
    assert backend.get("key") is None
    adapter.put("key", response("# new\nbody\n" * 10))
    assert adapter.get("key").heading_index


@pytest.mark.parametrize("index", [None, {}, [[True, 1, 2, 1, 1]], [[0, 2, 5, 0, 99]]])
def test_malformed_optional_index_repaired(backend, index):
    content = "# title\nbody\n" * 10
    backend.put(
        "key",
        PendingSelection(
            chunks={"__content__": content, "__meta__": json.dumps({headings.INDEX_KEY: index})},
            format="progressive",
            created_at=time.monotonic(),
            total_chars=len(content),
        ),
    )
    actual = ProgressiveStoreAdapter(backend).get("key")
    assert actual.heading_index == headings.build_heading_index(content)


def test_disabled_hints_do_not_scan(backend, monkeypatch):
    def forbidden(_):
        pytest.fail("disabled hints scanned headings")

    monkeypatch.setattr(headings, "build_heading_index", forbidden)
    monkeypatch.setattr(progressive, "build_heading_index", forbidden)
    adapter = ProgressiveStoreAdapter(backend)
    adapter.put("key", response("# title\nbody\n" * 100, hints=False))
    resp = adapter.get("key")
    assert resp.heading_index is None
    assert "Remaining:" not in ProgressiveChunker(40, include_hint=False).first_chunk(
        resp.content, "key"
    )


def test_chunker_reconstruction_and_content_replacement():
    chunker = ProgressiveChunker(40)
    for content in ("# first\nbody\n" * 10, "# second\nbody\n" * 10):
        offset = 0
        parts = []
        while offset < len(content):
            result = chunker.read_chunk_result(content, offset)
            parts.append(result.text[: result.payload_chars])
            offset += result.payload_chars
        assert "".join(parts) == content


def test_repeated_reads_do_not_decode_or_validate_index_again(backend, monkeypatch):
    from memtomem_stm.proxy import pending_store

    adapter = ProgressiveStoreAdapter(backend)
    adapter.put("key", response("# title\nbody\n" * 100))
    adapter.get("key")

    def forbidden(*args):
        pytest.fail("unchanged metadata was decoded or validated again")

    monkeypatch.setattr(pending_store, "_parse_progressive_metadata", forbidden)
    monkeypatch.setattr(headings, "valid_heading_index", forbidden)
    for _ in range(10):
        assert adapter.get("key", renew=True).heading_index
    adapter.delete("key")
    assert backend._progressive_meta.meta is None


def test_external_sqlite_replacement_invalidates_metadata_snapshot(tmp_path):
    path = tmp_path / "p.db"
    reader, writer = SQLitePendingStore(path), SQLitePendingStore(path)
    reader.initialize()
    writer.initialize()
    try:
        write_adapter = ProgressiveStoreAdapter(writer)
        read_adapter = ProgressiveStoreAdapter(reader)
        write_adapter.put("key", response("# old title\nbody\n" * 20))
        first = read_adapter.get("key")
        write_adapter.put("key", response("# new different title\nbody\n" * 20))
        new = read_adapter.get("key")
        assert new.heading_index != first.heading_index
        assert '"new different title"' in headings.remaining_headings(
            new.content, 0, new.heading_index
        )
        write_adapter.delete("key")
        assert read_adapter.get("key") is None
        assert reader._progressive_meta.meta is None
    finally:
        reader.close()
        writer.close()


def test_expired_legacy_row_is_not_indexed(backend, monkeypatch):
    content = "# title\nbody\n"
    backend.put(
        "key",
        PendingSelection(
            chunks={"__content__": content, "__meta__": '{"ttl_seconds":0}'},
            format="progressive",
            created_at=time.monotonic(),
            total_chars=len(content),
        ),
    )

    def forbidden(_):
        pytest.fail("expired body indexed")

    monkeypatch.setattr(headings, "build_heading_index", forbidden)
    assert ProgressiveStoreAdapter(backend).get("key") is None


def test_legacy_index_repair_writes_once_without_renewing(tmp_path):
    store = SQLitePendingStore(tmp_path / "p.db")
    store.initialize()
    try:
        content = "# title\nbody\n" * 20
        store.put(
            "key",
            PendingSelection(
                chunks={"__content__": content, "__meta__": "{}"},
                format="progressive",
                created_at=0,
                total_chars=len(content),
            ),
        )
        before = (
            store._get_db()
            .execute("SELECT created_at, seq FROM pending_selections WHERE key='key'")
            .fetchone()
        )
        statements = []
        store._get_db().set_trace_callback(statements.append)
        adapter = ProgressiveStoreAdapter(store)
        for _ in range(3):
            adapter.get("key")
        writes = [
            s for s in statements if s.startswith("UPDATE pending_selections SET chunks_json")
        ]
        assert len(writes) == 1
        assert (
            before
            == store._get_db()
            .execute("SELECT created_at, seq FROM pending_selections WHERE key='key'")
            .fetchone()
        )
    finally:
        store.close()
