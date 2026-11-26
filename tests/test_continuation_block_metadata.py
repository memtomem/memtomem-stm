"""A text block's ``annotations`` and ``_meta`` ride every continuation (#1529).

The first progressive / selective / hybrid response keeps the upstream text
block's metadata. ``stm_proxy_read_more`` and ``stm_proxy_select_chunks`` serve
the rest of that same block, so they carry it too: text an upstream addressed
to ``audience: ["assistant"]`` keeps that hint from the second chunk on.
"""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock

import pytest
from mcp.types import Annotations, CallToolResult, TextContent

from memtomem_stm.proxy.config import (
    CleaningConfig,
    ProgressiveConfig,
    ProxyConfig,
    SelectiveConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.server import _continuation_result

ANNOTATIONS = Annotations(audience=["assistant"], priority=0.25)
BLOCK_META = {"origin": {"doc": 7}}
SOURCE = "## A\n" + "alpha " * 600 + "\n\n## B\n" + "beta " * 300


def _manager(tmp_path, strategy, fmt, *, sqlite=False, budget=4000):
    selective = (
        SelectiveConfig(pending_store="sqlite", pending_store_path=tmp_path / "pending.db")
        if sqlite
        else None
    )
    upstream = UpstreamServerConfig(
        prefix="doc",
        compression=strategy,
        max_result_chars=budget,
        delivery_format=fmt,
        cleaning=CleaningConfig(enabled=False),
        progressive=ProgressiveConfig(chunk_size=1000),
        selective=selective,
    )
    cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": upstream},
        min_result_retention=0,
    )
    proxy = ProxyManager(cfg, TokenTracker())
    session = AsyncMock()
    proxy._connections["srv"] = UpstreamConnection(
        name="srv", config=upstream, session=session, tools=[]
    )
    return proxy, session


def _block(text=SOURCE, *, annotations=ANNOTATIONS, meta=BLOCK_META):
    return TextContent(type="text", text=text, annotations=annotations, _meta=meta)


def _key(initial, strategy, fmt):
    """The continuation key (and progressive offset) the first response advertises."""
    text = initial if isinstance(initial, str) else initial.content[0].text
    if fmt == "structured":
        payload = json.loads(text)["stm"]["payload"]
        return payload["key"], payload.get("next_offset")
    if strategy == "progressive":
        match = re.search(r'stm_proxy_read_more\(key="([0-9a-f]+)", offset=(\d+)\)', text)
        assert match is not None, text[-300:]
        return match.group(1), int(match.group(2))
    match = re.search(r"stm_proxy_select_chunks\(key='([0-9a-f]+)'", text)
    assert match is not None, text[-300:]
    return match.group(1), None


def _continue(proxy, strategy, key, offset, response_format=None):
    if strategy == "progressive":
        value = proxy.read_more(key, offset, response_format=response_format)
    else:
        value = proxy.select_chunks(key, ["B"], response_format=response_format)
    return _continuation_result(value, response_format)


async def _publish(proxy, session, strategy, fmt, block):
    session.call_tool.return_value = CallToolResult(content=[block])
    initial = await proxy.call_tool("srv", "read", {})
    assert isinstance(initial, (str, CallToolResult))
    return _key(initial, strategy, fmt)


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["legacy", "structured"])
@pytest.mark.parametrize("strategy", ["progressive", "selective", "hybrid"])
async def test_continuation_keeps_the_text_block_metadata(tmp_path, strategy, fmt):
    proxy, session = _manager(tmp_path, strategy, fmt)
    key, offset = await _publish(proxy, session, strategy, fmt, _block())

    more = _continue(proxy, strategy, key, offset)

    assert more.content[0].annotations == ANNOTATIONS
    assert more.content[0].meta == BLOCK_META
    # A continuation, not an STM message: progressive serves the next slice of
    # SOURCE, select the "B" section.
    expected = "alpha alpha" if strategy == "progressive" else "beta beta"
    assert expected in more.content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["legacy", "structured"])
@pytest.mark.parametrize("strategy", ["progressive", "selective"])
async def test_unannotated_continuation_stays_a_bare_block(tmp_path, strategy, fmt):
    proxy, session = _manager(tmp_path, strategy, fmt)
    key, offset = await _publish(
        proxy, session, strategy, fmt, TextContent(type="text", text=SOURCE)
    )

    more = _continue(proxy, strategy, key, offset)

    wire = json.loads(more.model_dump_json(by_alias=True, exclude_none=True))
    assert set(wire["content"][0]) == {"type", "text"}
    expected = "alpha alpha" if strategy == "progressive" else "beta beta"
    assert expected in more.content[0].text
    if fmt == "structured":
        assert more.structured_content["stm"]["kind"] in ("progressive", "selection")
    else:
        assert more.structured_content == {"result": more.content[0].text}


@pytest.mark.asyncio
async def test_hybrid_publishes_a_hybrid_envelope(tmp_path):
    proxy, session = _manager(tmp_path, "hybrid", "structured")
    session.call_tool.return_value = CallToolResult(content=[_block()])
    initial = await proxy.call_tool("srv", "read", {})
    assert json.loads(initial.content[0].text)["stm"]["kind"] == "hybrid"
    assert initial.content[0].annotations == ANNOTATIONS


@pytest.mark.asyncio
@pytest.mark.parametrize(("published", "size"), [("legacy", 2000), ("structured", 300)])
async def test_a_read_past_the_end_carries_no_metadata(tmp_path, published, size):
    """No upstream text is served there, and block metadata larger than the
    structured cap (the legacy key) must not refuse the terminal frame a bare
    one fits."""
    proxy, session = _manager(tmp_path, "progressive", published, budget=1500)
    key, _ = await _publish(
        proxy, session, "progressive", published, _block(meta={"blob": "z" * size})
    )
    for read in ("legacy", "structured"):
        end = _continue(proxy, "progressive", key, len(SOURCE), response_format=read)
        assert end.content[0].annotations is None and end.content[0].meta is None
        if read == "structured":
            assert end.structured_content["stm"]["payload"]["content"] == ""


@pytest.mark.asyncio
async def test_stm_messages_do_not_carry_the_upstream_metadata(tmp_path):
    proxy, session = _manager(tmp_path, "progressive", "legacy")
    key, _ = await _publish(proxy, session, "progressive", "legacy", _block())
    end = _continuation_result(proxy.read_more(key, len(SOURCE) + 10), None)
    assert end.content[0].text == "(no more content)"
    assert end.content[0].annotations is None and end.content[0].meta is None

    proxy, session = _manager(tmp_path / "sel", "selective", "legacy")
    key, _ = await _publish(proxy, session, "selective", "legacy", _block())
    miss = _continuation_result(proxy.select_chunks(key, ["nope"]), None)
    assert miss.content[0].text.startswith("No matching sections found")
    assert miss.content[0].annotations is None and miss.content[0].meta is None


# ── budget: each structured frame repeats the metadata and still fits ─────────


def _wire_len(result):
    from memtomem_stm.proxy.delivery import wire_json

    return len(wire_json(result))


@pytest.mark.asyncio
async def test_structured_progressive_frames_fit_with_the_metadata(tmp_path):
    proxy, session = _manager(tmp_path, "progressive", "structured", budget=1600)
    block = _block(meta={"blob": "z" * 500})
    key, offset = await _publish(proxy, session, "progressive", "structured", block)
    first_offset = offset
    pieces = []
    while offset is not None:
        more = _continue(proxy, "progressive", key, offset)
        assert _wire_len(more) <= 1600
        assert more.content[0].meta == {"blob": "z" * 500}
        payload = more.structured_content["stm"]["payload"]
        pieces.append(payload["content"])
        offset = payload["next_offset"]
    assert first_offset and len(pieces) > 1
    assert "".join(pieces) == SOURCE[first_offset:]


@pytest.mark.asyncio
async def test_float_metadata_is_counted_as_the_sdk_sends_it(tmp_path):
    """Under a projector ``wire_json`` writes floats with ``json`` (``1e-05``);
    the SDK sends ``0.00001``. A frame is either refused or fits as sent."""
    from memtomem_stm.proxy.delivery import DeliveryBudgetExceeded, wire_projection_scope

    served = refused = 0
    for count in range(0, 160, 4):
        proxy, session = _manager(tmp_path / str(count), "progressive", "legacy", budget=1500)
        meta = {"n": [1e-5] * count} if count else None
        key, offset = await _publish(proxy, session, "progressive", "legacy", _block(meta=meta))
        with wire_projection_scope(lambda data: data):
            try:
                more = _continue(proxy, "progressive", key, offset, response_format="structured")
            except DeliveryBudgetExceeded:
                refused += 1
                continue
        served += 1
        assert len(more.model_dump_json(by_alias=True, exclude_none=True)) <= 1500, count
    assert served and refused


@pytest.mark.asyncio
async def test_an_issued_progressive_key_can_always_be_read(tmp_path):
    """Publication preflights a continuation that repeats the metadata: a key it
    issues never leads to a frame the cap refuses."""
    from memtomem_stm.proxy.delivery import DeliveryError

    issued = declined = 0
    for size in range(0, 1400, 25):
        proxy, session = _manager(tmp_path / str(size), "progressive", "structured", budget=1600)
        session.call_tool.return_value = CallToolResult(
            content=[_block(meta={"blob": "z" * size} if size else None)]
        )
        initial = await proxy.call_tool("srv", "read", {})
        stm = json.loads(initial.content[0].text)["stm"]
        if stm["kind"] != "progressive":
            declined += 1
            continue
        issued += 1
        payload = stm["payload"]
        try:
            proxy.read_more(payload["key"], payload["next_offset"])
        except DeliveryError as exc:
            pytest.fail(f"size {size}: issued key refused its continuation ({exc.code})")
    assert issued and declined


@pytest.mark.asyncio
async def test_structured_select_refuses_rather_than_overshooting(tmp_path):
    from memtomem_stm.proxy.delivery import DeliveryBudgetExceeded

    outcomes = set()
    for size in range(0, 1200, 40):
        proxy, session = _manager(tmp_path / str(size), "selective", "structured")
        meta = {"blob": "z" * size} if size else None
        key, _ = await _publish(proxy, session, "selective", "structured", _block(meta=meta))
        try:
            more = _continue(proxy, "selective", key, None)
        except DeliveryBudgetExceeded:
            outcomes.add("refused")
            continue
        assert _wire_len(more) <= 4000, size
        outcomes.add("served")
    assert outcomes == {"refused", "served"}


# ── cross-format reads, in memory and after a SQLite restart ───────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlite", [False, True])
@pytest.mark.parametrize("strategy", ["progressive", "selective"])
@pytest.mark.parametrize(
    ("published", "read"), [("legacy", "structured"), ("structured", "legacy")]
)
async def test_override_reads_keep_the_metadata(tmp_path, strategy, published, read, sqlite):
    proxy, session = _manager(tmp_path, strategy, published, sqlite=sqlite)
    key, offset = await _publish(proxy, session, strategy, published, _block())
    if sqlite:
        proxy, _ = _manager(tmp_path, strategy, published, sqlite=True)

    more = _continue(proxy, strategy, key, offset, response_format=read)

    assert more.content[0].annotations == ANNOTATIONS
    assert more.content[0].meta == BLOCK_META
    if read == "structured":
        assert more.structured_content["stm"]["kind"] in ("progressive", "selection")
    else:
        assert more.structured_content == {"result": more.content[0].text}
    expected = "alpha alpha" if strategy == "progressive" else "beta beta"
    assert expected in more.content[0].text
    assert _wire_len(more) <= 4000


@pytest.mark.asyncio
async def test_legacy_key_whose_metadata_outgrows_the_structured_cap(tmp_path):
    from memtomem_stm.proxy.delivery import DeliveryError

    proxy, session = _manager(tmp_path, "progressive", "legacy", budget=1500)
    block = _block(meta={"blob": "z" * 2000})
    key, offset = await _publish(proxy, session, "progressive", "legacy", block)

    with pytest.raises(DeliveryError) as refused:
        proxy.read_more(key, offset, response_format="structured")
    assert refused.value.code == "budget_exceeded"

    more = _continue(proxy, "progressive", key, offset, response_format="legacy")
    assert more.content[0].meta == {"blob": "z" * 2000}
    assert more.content[0].text.startswith(SOURCE[offset : offset + 20])


# ── stored rows: invalid values, unreadable containers, non-finite floats ──────


def _nested(depth):
    value: object = "x"
    for _ in range(depth):
        value = [value]
    return value


# Values no publisher writes, as a corrupt row or another writer could hold them.
INVALID_BLOCKS = [
    "bogus",
    None,
    {"annotations": {"audience": ["nobody"]}},
    {"annotations": {"priority": "high"}},
    {"_meta": {"d": _nested(1000)}},  # json decodes it; a recursive scrub cannot
]
INVALID_IDS = ["string", "null", "audience", "priority", "depth-1000"]


def _rows(path):
    import sqlite3

    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT key, chunks_json, metadata_json, created_at FROM pending_selections"
        ).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [*INVALID_BLOCKS, "raw:not json", "raw:[1, 2]"],
    ids=[*INVALID_IDS, "undecodable", "array"],
)
async def test_invalid_progressive_text_block_is_an_error_and_keeps_the_row(tmp_path, bad):
    import sqlite3

    from memtomem_stm.proxy.delivery import DeliveryError

    proxy, session = _manager(tmp_path, "progressive", "legacy", sqlite=True)
    key, offset = await _publish(proxy, session, "progressive", "legacy", _block())
    db_path = tmp_path / "pending.db"
    with sqlite3.connect(db_path) as db:
        (raw,) = db.execute("SELECT metadata_json FROM pending_selections").fetchone()
        assert json.loads(raw) == {
            "text_block": {
                "annotations": ANNOTATIONS.model_dump(mode="json", exclude_none=True),
                "_meta": BLOCK_META,
            }
        }
        stored = bad if isinstance(bad, str) and bad.startswith("raw:") else None
        container = stored[4:] if stored else json.dumps({"text_block": bad})
        db.execute("UPDATE pending_selections SET metadata_json = ?", (container,))
    before = _rows(db_path)

    restarted, _ = _manager(tmp_path, "progressive", "legacy", sqlite=True)
    with pytest.raises(DeliveryError):
        restarted.read_more(key, offset)
    assert _rows(db_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlite", [False, True])
async def test_expiry_passes_do_not_decode_block_metadata(tmp_path, monkeypatch, sqlite):
    """Every publication's expiry pass decodes each live row's ``__meta__``;
    block metadata, which can be large, stays out of it."""
    import memtomem_stm.proxy.pending_store as pending_store

    proxy, session = _manager(tmp_path, "progressive", "legacy", sqlite=sqlite)
    big = {"blob": "z" * 60_000}  # under the 64 KiB retention cap
    for i in range(3):
        await _publish(
            proxy, session, "progressive", "legacy", _block(text=SOURCE + str(i), meta=big)
        )
    decoded: list[int] = []
    real = pending_store._parse_progressive_metadata
    monkeypatch.setattr(
        pending_store,
        "_parse_progressive_metadata",
        lambda raw: decoded.append(len(raw or "")) or real(raw),
    )

    key, offset = await _publish(proxy, session, "progressive", "legacy", _block(meta=big))

    assert len(decoded) >= 3  # the pass ran over the earlier rows
    assert max(decoded) < 2_000
    more = _continue(proxy, "progressive", key, offset)
    assert more.content[0].meta == big


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", INVALID_BLOCKS, ids=INVALID_IDS)
async def test_invalid_selective_text_block_is_an_error_and_keeps_the_row(tmp_path, bad):
    import sqlite3

    from memtomem_stm.proxy.delivery import DeliveryError

    proxy, session = _manager(tmp_path, "selective", "legacy", sqlite=True)
    key, _ = await _publish(proxy, session, "selective", "legacy", _block())
    db_path = tmp_path / "pending.db"
    with sqlite3.connect(db_path) as db:
        (raw,) = db.execute("SELECT metadata_json FROM pending_selections").fetchone()
        meta = json.loads(raw)
        meta["text_block"] = bad
        db.execute("UPDATE pending_selections SET metadata_json = ?", (json.dumps(meta),))
    before = _rows(db_path)

    with pytest.raises(DeliveryError):
        proxy.select_chunks(key, ["B"])
    assert _rows(db_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("container", ["not json", "[1, 2]"])
async def test_explicit_legacy_select_over_an_unreadable_container(tmp_path, container):
    import sqlite3

    proxy, session = _manager(tmp_path, "selective", "legacy", sqlite=True)
    key, _ = await _publish(proxy, session, "selective", "legacy", _block())
    with sqlite3.connect(tmp_path / "pending.db") as db:
        db.execute("UPDATE pending_selections SET metadata_json = ?", (container,))

    more = _continue(proxy, "selective", key, None, response_format="legacy")

    assert more.content[0].text.startswith("## B\nbeta")
    assert more.content[0].annotations is None


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["progressive", "selective"])
async def test_non_finite_meta_is_stored_in_its_wire_form(tmp_path, strategy):
    proxy, session = _manager(tmp_path, strategy, "legacy", sqlite=True)
    block = _block(meta={"v": float("inf"), "k": 1})
    key, offset = await _publish(proxy, session, strategy, "legacy", block)

    more = _continue(proxy, strategy, key, offset)

    assert more.content[0].meta == {"v": None, "k": 1}


# ── credentials in the metadata never reach the SQLite pending store ───────────

SECRET = "hunter2-" + "Q" * 30
CREDENTIAL_META = {
    "label": {"password": SECRET},
    "value": {"note": f'"password": "{SECRET}"'},
    "key": {f'"password": "{SECRET}"': "event"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["progressive", "selective"])
@pytest.mark.parametrize("case", sorted(CREDENTIAL_META))
async def test_credential_in_metadata_refuses_the_persistent_write(
    tmp_path, monkeypatch, strategy, case
):
    from memtomem_stm.proxy.pending_store import SQLitePendingStore

    proxy, session = _manager(tmp_path, strategy, "legacy", sqlite=True)
    kept, kept_offset = await _publish(proxy, session, strategy, "legacy", _block())
    before = _rows(tmp_path / "pending.db")
    evictions = []
    for name in ("evict_expired", "evict_oldest", "evict_progressive_expired"):
        monkeypatch.setattr(SQLitePendingStore, name, lambda *a, _n=name, **k: evictions.append(_n))

    session.call_tool.return_value = CallToolResult(content=[_block(meta=CREDENTIAL_META[case])])
    degraded = await proxy.call_tool("srv", "read", {})

    assert evictions == []
    assert _rows(tmp_path / "pending.db") == before
    assert SECRET not in (tmp_path / "pending.db").read_bytes().decode("utf-8", "replace")
    assert "stm_proxy_" not in degraded.content[0].text
    assert _continue(proxy, strategy, kept, kept_offset).content[0].meta == BLOCK_META
    # Positive control: the patch does see the evictions of a write that lands.
    evictions.clear()
    await _publish(proxy, session, strategy, "legacy", _block(text=SOURCE + " more"))
    assert evictions
    monkeypatch.undo()


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["progressive", "selective"])
async def test_literal_escape_text_is_not_a_credential(tmp_path, strategy):
    proxy, session = _manager(tmp_path, strategy, "legacy", sqlite=True)
    meta = {"note": "\\u0070assword = example-value-that-is-long"}
    key, offset = await _publish(proxy, session, strategy, "legacy", _block(meta=meta))

    assert _continue(proxy, strategy, key, offset).content[0].meta == meta


# ── no SDK wire projector: legacy continuations still carry the metadata ───────


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["progressive", "selective"])
async def test_legacy_continuations_do_not_need_the_sdk_projector(tmp_path, strategy):
    from memtomem_stm.proxy.delivery import DeliveryError, wire_projection_scope

    def unsupported(data):
        raise DeliveryError("no projector", code="unsupported_sdk_wire_format")

    proxy, session = _manager(tmp_path, strategy, "legacy", sqlite=True)
    with wire_projection_scope(unsupported):
        key, offset = await _publish(proxy, session, strategy, "legacy", _block())
        more = _continue(proxy, strategy, key, offset)
    assert more.content[0].annotations == ANNOTATIONS


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["legacy", "structured"])
@pytest.mark.parametrize("sqlite", [False, True])
async def test_one_read_decodes_the_block_metadata_once(tmp_path, monkeypatch, fmt, sqlite):
    import memtomem_stm.proxy.compression as compression
    import memtomem_stm.proxy.delivery as delivery

    validated: list[object] = []
    real = delivery.stored_text_block

    def counting(meta):
        validated.append(meta)
        return real(meta)

    for module in (delivery, compression):
        monkeypatch.setattr(module, "stored_text_block", counting)
    for strategy in ("progressive", "selective"):
        proxy, session = _manager(tmp_path / strategy, strategy, fmt, sqlite=sqlite)
        key, offset = await _publish(proxy, session, strategy, fmt, _block())
        validated.clear()
        loads: list[str] = []
        real_loads = compression.json.loads
        monkeypatch.setattr(
            compression.json,
            "loads",
            lambda s, *a, **k: loads.append(s) or real_loads(s, *a, **k),
        )

        more = _continue(proxy, strategy, key, offset)

        monkeypatch.setattr(compression.json, "loads", real_loads)
        assert more.content[0].meta == BLOCK_META
        assert len(validated) == 1, strategy
        # ``json.loads`` is the one module object both readers use, so this
        # counts every decode of the container holding the block metadata.
        assert sum('"text_block"' in s for s in loads if isinstance(s, str)) == 1, strategy


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["progressive", "selective"])
async def test_oversized_block_metadata_is_not_retained(tmp_path, strategy):
    """Pending stores bound entries by count only; block metadata past 64 KiB
    serialized stays on the first response and is not kept for continuations."""
    from memtomem_stm.proxy.manager import _MAX_CONTINUED_TEXT_BLOCK_BYTES

    proxy, session = _manager(tmp_path, strategy, "legacy", sqlite=True)
    fits = {"blob": "z" * (_MAX_CONTINUED_TEXT_BLOCK_BYTES - 100)}
    over = {"blob": "z" * (_MAX_CONTINUED_TEXT_BLOCK_BYTES + 1)}

    key, offset = await _publish(proxy, session, strategy, "legacy", _block(meta=fits))
    assert _continue(proxy, strategy, key, offset).content[0].meta == fits

    session.call_tool.return_value = CallToolResult(content=[_block(text=SOURCE + "!", meta=over)])
    initial = await proxy.call_tool("srv", "read", {})
    assert initial.content[0].meta == over
    key, offset = _key(initial, strategy, "legacy")
    more = _continue(proxy, strategy, key, offset)
    assert more.content[0].meta is None and more.content[0].annotations is None
    assert "z" * 1000 not in "".join(str(r) for r in _rows(tmp_path / "pending.db")[-1:])
