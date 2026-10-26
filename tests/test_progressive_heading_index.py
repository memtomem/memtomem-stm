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
        re.findall(r"(?:^|\n)#{1,6}[ \t]+(.+)", content[start:])
        if start == 0
        else re.compile(r"(?:^|\n)#{1,6}[ \t]+(.+)").findall(content, start)
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
    chunks = backend.get("key").chunks
    assert headings.INDEX_KEY in chunks  # includes the persisted empty result
    assert headings.INDEX_KEY not in json.loads(chunks["__meta__"])


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
    assert adapter.get("key").heading_index == b""
    adapter.evict(1800, 0)
    assert backend.get("key") is None
    adapter.put("key", response("# new\nbody\n" * 10))
    assert adapter.get("key").heading_index


@pytest.mark.parametrize("index", ["", "{}", "v2:not-base64!", "v2:eA=="])
def test_malformed_optional_index_repaired(backend, index):
    content = "# title\nbody\n" * 10
    backend.put(
        "key",
        PendingSelection(
            chunks={"__content__": content, "__meta__": "{}", headings.INDEX_KEY: index},
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


def test_alternating_reads_only_inspect_logarithmic_and_displayed_rows(backend, monkeypatch):
    adapter = ProgressiveStoreAdapter(backend)
    content = "# title\nbody\n" * 4000
    for key in ("a", "b"):
        adapter.put(key, response(content))

    def forbidden(*args):
        pytest.fail("alternating reads rebuilt index")

    monkeypatch.setattr(headings, "build_heading_index", forbidden)
    monkeypatch.setattr(progressive, "build_heading_index", forbidden)
    original = headings._field
    reads = []

    def counted(*args):
        reads.append(1)
        return original(*args)

    monkeypatch.setattr(headings, "_field", counted)
    for key in ("a", "b") * 5:
        resp = adapter.get(key, renew=True)
        reads.clear()
        chunker = ProgressiveChunker(40, heading_index=resp.heading_index)
        assert "Remaining:" in chunker.read_chunk(resp.content, 0)
        assert len(reads) <= 20  # binary search plus count, never a full validation walk


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


def test_eviction_only_parses_small_ttl_metadata(backend, monkeypatch):
    from memtomem_stm.proxy import pending_store

    adapter = ProgressiveStoreAdapter(backend)
    adapter.put("key", response("# comment\ncode()\n" * 40000))
    chunks = backend.get("key").chunks
    assert len(chunks["__meta__"]) < 1024
    assert len(chunks[headings.INDEX_KEY]) <= headings.MAX_ENCODED
    seen = []
    original = pending_store._parse_progressive_metadata

    def parsed(raw):
        seen.append(len(raw))
        return original(raw)

    def forbidden(*args):
        pytest.fail("expiry cleanup decoded the heading index")

    monkeypatch.setattr(pending_store, "_parse_progressive_metadata", parsed)
    monkeypatch.setattr(headings, "decode_heading_index", forbidden)
    adapter.evict(1800, 200)
    assert seen and max(seen) < 1024
    assert backend.get("key") is not None


def test_codec_roundtrip_and_size_on_comment_heavy_source():
    content = "\n".join(
        f"# comment number {i} explaining things\nx_{i} = compute({i})" for i in range(40000)
    )
    index = headings.build_heading_index(content)
    encoded = headings.encode_heading_index(index)
    assert encoded != "scan"
    assert len(encoded) < len(content) // 20
    assert headings.decode_heading_index(encoded) == index
    for offset in (0, 1, 4000, len(content) // 2, len(content)):
        assert headings.remaining_headings(content, offset, index) == old_hint(content, offset)


@pytest.mark.parametrize("limit", ["rows", "encoded"])
def test_index_limit_persists_fallback_without_rebuilding(backend, monkeypatch, limit):
    if limit == "rows":
        monkeypatch.setattr(headings, "MAX_BYTES", 20)
    else:
        monkeypatch.setattr(headings, "MAX_ENCODED", 5)
    content = "# title\nbody\n" * 20
    adapter = ProgressiveStoreAdapter(backend)
    adapter.put("key", response(content))
    assert backend.get("key").chunks[headings.INDEX_KEY] == "scan"

    def forbidden(*args):
        pytest.fail("oversized index rebuilt")

    monkeypatch.setattr(headings, "build_heading_index", forbidden)
    for _ in range(3):
        resp = adapter.get("key")
        assert resp.heading_index == headings.FALLBACK
        assert headings.remaining_headings(content, 40, resp.heading_index) == old_hint(content, 40)


def test_decoder_bounds_expansion_and_rejects_trailing_stream():
    import base64
    import zlib

    oversized = "v2:" + base64.b64encode(zlib.compress(b"x" * (headings.MAX_BYTES + 20))).decode()
    assert headings.decode_heading_index(oversized) is None
    trailing = "v2:" + base64.b64encode(zlib.compress(b"") + b"trailing").decode()
    assert headings.decode_heading_index(trailing) is None
    assert headings.decode_heading_index("v2:" + "A" * headings.MAX_ENCODED) is None


def test_bad_touched_record_falls_back_without_crashing():
    import struct

    content = "# title\nbody\n"
    for row in [(0, 2, 999, 1, 1), (0, 2, 7, 0, 1), (0, 2, 7, 1, 999)]:
        index = struct.pack("<IIIII", *row)
        assert headings.remaining_headings(content, 0, index) == old_hint(content, 0)
