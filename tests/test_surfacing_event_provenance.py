"""Event provenance and memory-path rows on ``surfacing_events``.

Each delivered surfacing records collection-time facts on its event row (the
host's ids for the call, how much was injected, whether the feedback id was
shown, a digest of the block's header line) and one ``surfacing_memory_paths``
row per delivered memory holding only keyed hashes of its path and preview.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.cli.hook_adapter import get_adapter
from memtomem_stm.cli.hook_cmd import _extract_surfaced_block, run_surfacing_hook
from memtomem_stm.surfacing import feedback_store as feedback_store_module
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine, _memory_path_inputs
from memtomem_stm.surfacing.feedback import FeedbackTracker
from memtomem_stm.surfacing.feedback_store import (
    EventProvenance,
    FeedbackStore,
    MemoryPathInput,
)
from memtomem_stm.surfacing.formatter import SurfacingFormatter
from memtomem_stm.surfacing.grams import (
    ancestor_keys,
    basename_key,
    gram_hashes,
    keyed_hash,
    path_key,
    snippet_grams,
)

FIXTURES = Path(__file__).parent / "fixtures" / "hooks" / "claude"
NEW_EVENT_COLUMNS = (
    "injected_chars",
    "tool_use_id",
    "host_session_id",
    "host_agent_id",
    "id_advertised",
    "header_digest",
)


# ── fakes ──────────────────────────────────────────────────────────────


@dataclass
class FakeMeta:
    source_file: Path | None = None
    namespace: str = "default"


@dataclass
class FakeChunk:
    id: str
    content: str
    metadata: FakeMeta = field(default_factory=FakeMeta)


@dataclass
class FakeResult:
    chunk: FakeChunk
    score: float = 0.5
    pinned: bool = False


def _result(memory_id: str, content: str, source: str | None) -> FakeResult:
    meta = FakeMeta(source_file=None if source is None else Path(source))
    return FakeResult(chunk=FakeChunk(id=memory_id, content=content, metadata=meta))


def _config(**overrides: Any) -> SurfacingConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "min_response_chars": 10,
        "timeout_seconds": 5.0,
        "min_score": 0.02,
        "max_results": 5,
        "min_query_tokens": 1,
        "cooldown_seconds": 0.0,
        "max_surfacings_per_minute": 1000,
        "auto_tune_enabled": False,
        "include_session_context": False,
        "fire_webhook": False,
        "cache_ttl_seconds": 60.0,
    }
    defaults.update(overrides)
    return SurfacingConfig(**defaults)


def _adapter(results: list[FakeResult]) -> AsyncMock:
    adapter = AsyncMock()
    adapter.search = AsyncMock(return_value=(list(results), [], "ok"))
    return adapter


def _engine(tmp_path: Path, results: list[FakeResult], **config: Any):
    cfg = _config(**config)
    tracker = FeedbackTracker(config=cfg, db_path=tmp_path / "feedback.db")
    record_events = cfg.feedback_enabled
    engine = SurfacingEngine(
        config=cfg,
        mcp_adapter=_adapter(results),
        feedback_tracker=tracker,
        record_feedback_events=record_events,
    )
    return engine, tracker


def _rows(path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


def _event_rows(path: Path) -> list[sqlite3.Row]:
    return _rows(path, "SELECT * FROM surfacing_events ORDER BY created_at")


def _path_rows(path: Path, surfacing_id: str) -> dict[str, sqlite3.Row]:
    rows = _rows(
        path, "SELECT * FROM surfacing_memory_paths WHERE surfacing_id = ?", (surfacing_id,)
    )
    return {row["memory_id"]: row for row in rows}


def _key(path: Path) -> bytes:
    (row,) = _rows(path, "SELECT value FROM stm_meta WHERE name = 'hmac_key'")
    return bytes(row["value"])


ARGS = {"path": "src/app.py", "_context_query": "flask web framework"}
RESPONSE = "response body " * 20


# ── schema and migration ───────────────────────────────────────────────


def _columns(path: Path, table: str) -> list[str]:
    return [row[1] for row in _rows(path, f"PRAGMA table_info('{table}')")]


def _seed_pre_352(path: Path) -> None:
    db = sqlite3.connect(str(path))
    db.executescript(
        """
        CREATE TABLE surfacing_events (
            id          TEXT    PRIMARY KEY,
            server      TEXT    NOT NULL,
            tool        TEXT    NOT NULL,
            query       TEXT    NOT NULL,
            memory_ids  TEXT    NOT NULL,
            scores      TEXT    NOT NULL,
            created_at  REAL    NOT NULL
        );
        INSERT INTO surfacing_events
            (id, server, tool, query, memory_ids, scores, created_at)
        VALUES ('legacy-1', 's', 'read_file', 'old query', '["m1"]', '[0.5]', 0.0);
        """
    )
    db.commit()
    db.close()


def _init_store(path: str, barrier: Any) -> None:
    barrier.wait()
    store = FeedbackStore(Path(path))
    store.initialize()
    store.close()


class TestMigration:
    def test_fresh_db_has_new_columns_and_tables(self, tmp_path: Path) -> None:
        path = tmp_path / "fb.db"
        store = FeedbackStore(path)
        store.initialize()
        store.close()
        assert set(NEW_EVENT_COLUMNS) <= set(_columns(path, "surfacing_events"))
        assert _columns(path, "surfacing_memory_paths") == [
            "surfacing_id",
            "memory_id",
            "eligible",
            "path_hash_lexical",
            "dir_hashes",
            "basename_hash",
            "snippet_grams",
        ]
        assert _columns(path, "stm_meta") == ["name", "value"]

    def test_pre_352_db_keeps_every_added_column_and_its_row(self, tmp_path: Path) -> None:
        path = tmp_path / "fb.db"
        _seed_pre_352(path)
        for _ in range(2):  # idempotent
            store = FeedbackStore(path)
            store.initialize()
            store.close()
        columns = _columns(path, "surfacing_events")
        assert {"score_scale", *NEW_EVENT_COLUMNS} <= set(columns)
        (row,) = _event_rows(path)
        assert row["id"] == "legacy-1"
        assert all(row[name] is None for name in NEW_EVENT_COLUMNS)

    def test_concurrent_initialize_of_a_legacy_db(self, tmp_path: Path) -> None:
        """Two processes upgrading one pre-#352 file at the same moment both
        succeed and leave one complete schema — the check-then-act steps run
        under a single write lock."""
        ctx = multiprocessing.get_context("spawn")
        workers = 6
        for attempt in range(4):
            path = tmp_path / f"fb{attempt}.db"
            _seed_pre_352(path)
            barrier = ctx.Barrier(workers)
            procs = [
                ctx.Process(target=_init_store, args=(str(path), barrier)) for _ in range(workers)
            ]
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join(timeout=60)
            assert [proc.exitcode for proc in procs] == [0] * workers
            columns = _columns(path, "surfacing_events")
            assert {"score_scale", *NEW_EVENT_COLUMNS} <= set(columns)
            assert len(columns) == len(set(columns))
            assert [row["id"] for row in _event_rows(path)] == ["legacy-1"]
            (notnull,) = _rows(
                path,
                "SELECT \"notnull\" FROM pragma_table_info('surfacing_events') "
                "WHERE name = 'query'",
            )
            assert notnull[0] == 0


class TestHmacKey:
    def test_committed_at_initialize_and_shared(self, tmp_path: Path) -> None:
        path = tmp_path / "fb.db"
        first = FeedbackStore(path)
        first.initialize()
        second = FeedbackStore(path)
        second.initialize()
        try:
            # A separate read-only connection sees the key before any event.
            ro = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                (value,) = ro.execute(
                    "SELECT value FROM stm_meta WHERE name = 'hmac_key'"
                ).fetchone()
            finally:
                ro.close()
            assert isinstance(value, bytes) and len(value) == 32
            assert first._hmac_key == second._hmac_key == value
            assert _event_rows(path) == []
            # The first write after initialize must not trip over an open
            # transaction left behind by the migration.
            assert first.record_surfacing("e1", "s", "t", "q", ["m1"], [0.5]) is True
        finally:
            first.close()
            second.close()


# ── the event write transaction ────────────────────────────────────────


class TestEventTransaction:
    def _store(self, tmp_path: Path) -> FeedbackStore:
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()
        return store

    def _paths(self) -> list[MemoryPathInput]:
        return [MemoryPathInput("m1", "/notes/a.md", "alpha beta gamma delta", True)]

    def test_path_insert_failure_rolls_back_the_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = self._store(tmp_path)
        try:
            real_rows = store._memory_path_rows
            # A row one column short fails inside executemany, after the event
            # INSERT already ran on this connection.
            monkeypatch.setattr(
                store,
                "_memory_path_rows",
                lambda *a: [row[:-1] for row in real_rows(*a)],
            )
            with pytest.raises(sqlite3.Error):
                store.record_surfacing(
                    "e1", "s", "t", "q", ["m1"], [0.5], memory_paths=self._paths()
                )
            # _persist_surfacing's finally commits dedup rows on the same
            # connection; that commit must not publish half an event.
            store.mark_surfaced(["m1"])
            assert _event_rows(store.db_path) == []
            assert _path_rows(store.db_path, "e1") == {}

            monkeypatch.setattr(store, "_memory_path_rows", real_rows)
            assert store.record_surfacing(
                "e1", "s", "t", "q", ["m1"], [0.5], memory_paths=self._paths()
            )
            assert [row["id"] for row in _event_rows(store.db_path)] == ["e1"]
            assert set(_path_rows(store.db_path, "e1")) == {"m1"}
        finally:
            store.close()

    def test_hashing_failure_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = self._store(tmp_path)
        try:

            def boom(*_: Any) -> list[str]:
                raise RuntimeError("hash failure")

            monkeypatch.setattr(feedback_store_module, "snippet_grams", boom)
            with pytest.raises(RuntimeError):
                store.record_surfacing(
                    "e1", "s", "t", "q", ["m1"], [0.5], memory_paths=self._paths()
                )
            assert _event_rows(store.db_path) == []
        finally:
            store.close()

    def test_replayed_id_adds_no_paths(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        try:
            store.record_surfacing("e1", "s", "t", "q", ["m1"], [0.5])
            store.record_surfacing("e1", "s", "t", "q", ["m1"], [0.5], memory_paths=self._paths())
            assert _path_rows(store.db_path, "e1") == {}
        finally:
            store.close()

    def test_provenance_columns_by_value(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        try:
            store.record_surfacing(
                "e1",
                "s",
                "t",
                "q",
                ["m1"],
                [0.5],
                provenance=EventProvenance(
                    injected_chars=123,
                    tool_use_id="toolu_1",
                    host_session_id="sess_1",
                    host_agent_id="agent_1",
                    id_advertised=False,
                    header_digest="ab" * 32,
                ),
            )
            (row,) = _event_rows(store.db_path)
            assert (
                row["injected_chars"],
                row["tool_use_id"],
                row["host_session_id"],
                row["host_agent_id"],
                row["id_advertised"],
                row["header_digest"],
            ) == (123, "toolu_1", "sess_1", "agent_1", 0, "ab" * 32)
        finally:
            store.close()


# ── memory-path rows ───────────────────────────────────────────────────


class TestMemoryPathRows:
    def test_hashes_follow_the_shared_rules(self, tmp_path: Path) -> None:
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()
        note = tmp_path / "notes" / "A.md"
        note.parent.mkdir()
        note.write_text("x")
        preview = "the staging \\<api\\> uses port eight thousand"
        try:
            store.record_surfacing(
                "e1",
                "s",
                "t",
                "q",
                ["m1"],
                [0.5],
                memory_paths=[MemoryPathInput("m1", str(note), preview, True)],
            )
            key = _key(store.db_path)
            row = _path_rows(store.db_path, "e1")["m1"]
            lexical = path_key(str(note))
            assert row["eligible"] == 1
            assert row["path_hash_lexical"] == keyed_hash(lexical, key)
            assert json.loads(row["dir_hashes"]) == [
                keyed_hash(a, key) for a in ancestor_keys(lexical)
            ]
            assert row["basename_hash"] == keyed_hash(basename_key(lexical), key)
            assert json.loads(row["snippet_grams"]) == snippet_grams(preview, key)
        finally:
            store.close()

    def test_path_rows_never_touch_the_filesystem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No lookup can stall the shared store worker: every filesystem probe
        raises here, and the event and its path rows still land."""
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()

        def forbidden(*_: Any, **__: Any) -> Any:
            raise AssertionError("filesystem lookup on the store worker")

        for name in ("exists", "realpath", "lexists", "isfile", "isdir", "islink", "stat"):
            if hasattr(os.path, name):
                monkeypatch.setattr(os.path, name, forbidden)
        monkeypatch.setattr(os, "stat", forbidden)
        try:
            assert store.record_surfacing(
                "e1",
                "s",
                "t",
                "q",
                ["m1"],
                [0.5],
                memory_paths=[
                    MemoryPathInput("m1", "/notes/a.md", "some preview words here", True)
                ],
            )
            assert set(_path_rows(store.db_path, "e1")) == {"m1"}
        finally:
            monkeypatch.undo()
            store.close()

    def test_deleted_file_stays_eligible(self, tmp_path: Path) -> None:
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()
        gone = tmp_path / "gone.md"  # never created: deleted before the write
        try:
            store.record_surfacing(
                "e1",
                "s",
                "t",
                "q",
                ["m1"],
                [0.5],
                memory_paths=[MemoryPathInput("m1", str(gone), "some preview text here", True)],
            )
            row = _path_rows(store.db_path, "e1")["m1"]
            assert row["eligible"] == 1
            assert row["path_hash_lexical"] == keyed_hash(path_key(str(gone)), _key(store.db_path))
        finally:
            store.close()

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_symlinked_source_is_keyed_by_its_own_path(self, tmp_path: Path) -> None:
        """Nothing is resolved through the filesystem: a symlink keys as written."""
        target = tmp_path / "real.md"
        target.write_text("x")
        link = tmp_path / "link.md"
        link.symlink_to(target)
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()
        try:
            store.record_surfacing(
                "e1",
                "s",
                "t",
                "q",
                ["m1"],
                [0.5],
                memory_paths=[MemoryPathInput("m1", str(link), "preview", True)],
            )
            key = _key(store.db_path)
            row = _path_rows(store.db_path, "e1")["m1"]
            assert row["path_hash_lexical"] == keyed_hash(path_key(str(link)), key)
            assert row["path_hash_lexical"] != keyed_hash(path_key(os.path.realpath(target)), key)
        finally:
            store.close()

    def test_eligibility_from_render_time_strings(self) -> None:
        results = [
            _result("m-abs", "absolute note body", "/notes/a.md"),
            _result("m-unknown", "compact parser body", "unknown"),
            _result("m-rel", "relative source body", "notes/b.md"),
            _result("m-empty", "daemon empty source", ""),
            FakeResult(
                chunk=FakeChunk(id="blk_1", content="pinned body", metadata=FakeMeta()),
                pinned=True,
            ),
        ]
        manifest = SurfacingFormatter(_config()).render(RESPONSE, results, "q")
        inputs = {item.memory_id: item for item in _memory_path_inputs(manifest, results)}
        # Pinned bullets carry no delivered id and get no row at all.
        assert set(inputs) == {"m-abs", "m-unknown", "m-rel", "m-empty"}
        assert {mid: item.eligible for mid, item in inputs.items()} == {
            "m-abs": True,
            "m-unknown": False,
            "m-rel": False,
            "m-empty": False,  # Path("") renders as "."
        }
        # The preview is the bullet's own rendered preview.
        assert inputs["m-abs"].preview == "absolute note body"

    def test_truncated_bullet_gets_no_row(self) -> None:
        results = [
            _result("m1", "first memory " * 5, "/notes/a.md"),
            _result("m2", "second memory " * 40, "/notes/b.md"),
        ]
        manifest = SurfacingFormatter(_config(max_injection_chars=260)).render(
            RESPONSE, results, "q"
        )
        assert manifest.truncated
        assert manifest.delivered_ids == ("m1",)
        assert [item.memory_id for item in _memory_path_inputs(manifest, results)] == ["m1"]


# ── the engine end to end ──────────────────────────────────────────────


class TestEngineWritesProvenance:
    async def test_miss_and_hit_rows_carry_host_ids_and_paths(self, tmp_path: Path) -> None:
        results = [
            _result("m1", "flask routing uses blueprints for modules", "/notes/flask.md"),
            _result("m2", "relative source never anchors a match", "rel/x.md"),
        ]
        engine, tracker = _engine(tmp_path, results)
        try:
            outputs = []
            for tool_use_id in ("toolu_miss", "toolu_hit"):
                outputs.append(
                    await engine.surface(
                        "gh",
                        "read_file",
                        ARGS,
                        RESPONSE,
                        session_id="sess_1",
                        cwd="/secret/project/dir",
                        tool_use_id=tool_use_id,
                        agent_id="agent_7",
                    )
                )
            await engine.drain_store_writes()
            path = tracker.store.db_path
            rows = _event_rows(path)
            assert [row["tool_use_id"] for row in rows] == ["toolu_miss", "toolu_hit"]
            for row, output in zip(rows, outputs, strict=True):
                assert row["host_session_id"] == "sess_1"
                assert row["host_agent_id"] == "agent_7"
                block = _extract_surfaced_block(RESPONSE, output, "append")
                assert block is not None
                assert row["injected_chars"] == len(block) == len(output) - len(RESPONSE) - 2
                assert row["id_advertised"] == 1
                assert row["header_digest"] == hashlib.sha256(b"## Relevant Memories").hexdigest()
                paths = _path_rows(path, row["id"])
                assert {mid: p["eligible"] for mid, p in paths.items()} == {"m1": 1, "m2": 0}
                key = _key(path)
                assert set(json.loads(paths["m1"]["snippet_grams"])) == gram_hashes(
                    "flask routing uses blueprints for modules", key
                )
            _assert_no_raw_text(
                path,
                forbidden=[
                    "/secret/project/dir",
                    "/notes/flask.md",
                    "flask.md",
                    "rel/x.md",
                    "blueprints",
                ],
            )
        finally:
            await engine.stop()
            tracker.close()

    async def test_proxy_call_stores_null_ids_but_keeps_paths(self, tmp_path: Path) -> None:
        engine, tracker = _engine(
            tmp_path, [_result("m1", "flask routing body text", "/notes/flask.md")]
        )
        try:
            await engine.surface("gh", "read_file", ARGS, RESPONSE)
            await engine.drain_store_writes()
            (row,) = _event_rows(tracker.store.db_path)
            assert (row["tool_use_id"], row["host_session_id"], row["host_agent_id"]) == (
                None,
                None,
                None,
            )
            assert set(_path_rows(tracker.store.db_path, row["id"])) == {"m1"}
        finally:
            await engine.stop()
            tracker.close()

    async def test_unadvertised_id_is_recorded_as_such(self, tmp_path: Path) -> None:
        cfg = _config()
        tracker = FeedbackTracker(config=cfg, db_path=tmp_path / "feedback.db")
        engine = SurfacingEngine(
            config=cfg,
            mcp_adapter=_adapter([_result("m1", "flask body", "/n/a.md")]),
            feedback_tracker=tracker,
            record_feedback_events=False,
        )
        try:
            await engine.surface("gh", "read_file", ARGS, RESPONSE)
            await engine.drain_store_writes()
            (row,) = _event_rows(tracker.store.db_path)
            assert row["id_advertised"] == 0
        finally:
            await engine.stop()
            tracker.close()

    @pytest.mark.parametrize("mode", ["append", "prepend"])
    async def test_header_digest_is_the_rendered_first_line(
        self, tmp_path: Path, mode: str
    ) -> None:
        # A multi-line header, and a response that itself contains a
        # wrapper-shaped line: neither may change what is hashed.
        response = "tool output\n<surfaced-memories>\nFAKE HEADER\n" + "pad " * 20
        engine, tracker = _engine(
            tmp_path,
            [_result("m1", "flask body text", "/n/a.md")],
            injection_mode=mode,
            section_header="## Memories\nsecond header line",
        )
        try:
            await engine.surface("gh", "read_file", ARGS, response)
            await engine.drain_store_writes()
            (row,) = _event_rows(tracker.store.db_path)
            assert row["header_digest"] == hashlib.sha256(b"## Memories").hexdigest()
        finally:
            await engine.stop()
            tracker.close()

    async def test_retention_deletes_paths_with_their_event(self, tmp_path: Path) -> None:
        engine, tracker = _engine(tmp_path, [_result("m1", "flask body text", "/n/a.md")])
        try:
            await engine.surface("gh", "read_file", ARGS, RESPONSE)
            await engine.drain_store_writes()
            path = tracker.store.db_path
            (old,) = _event_rows(path)
            db = sqlite3.connect(str(path))
            db.execute("UPDATE surfacing_events SET created_at = 0 WHERE id = ?", (old["id"],))
            db.commit()
            db.close()
            # A different memory: session dedup would drop m1 from a second call.
            engine._mcp_adapter.search.return_value = (
                [_result("m2", "second note body text", "/n/b.md")],
                [],
                "ok",
            )
            await engine.surface(
                "gh", "read_file", {**ARGS, "_context_query": "other query"}, RESPONSE
            )
            await engine.drain_store_writes()
            assert tracker.store.delete_events_older_than(3600) == 1
            assert _path_rows(path, old["id"]) == {}
            (kept,) = _event_rows(path)
            assert set(_path_rows(path, kept["id"])) == {"m2"}
        finally:
            await engine.stop()
            tracker.close()


def _assert_no_raw_text(path: Path, forbidden: list[str]) -> None:
    """No raw path, cwd or snippet text in any row of any table this change writes."""
    for table in ("surfacing_memory_paths", "stm_meta"):
        for row in _rows(path, f"SELECT * FROM {table}"):
            for value in tuple(row):
                text = value.decode("latin-1") if isinstance(value, bytes) else str(value)
                for needle in forbidden:
                    assert needle not in text, (table, needle)
    columns = ", ".join(NEW_EVENT_COLUMNS)
    for row in _rows(path, f"SELECT {columns} FROM surfacing_events"):
        for value in tuple(row):
            for needle in forbidden:
                assert needle not in str(value), ("surfacing_events", needle)


# ── the Claude hook payload, parsed through to the row ─────────────────


class TestClaudeHookPayload:
    @pytest.mark.parametrize(
        ("fixture", "agent_id", "tool_use_id"),
        [
            ("inbound_read_posttooluse_subagent.json", "agent_demo_explore", "toolu_demo_sub"),
            ("inbound_read_posttooluse.json", None, "toolu_demo_main"),
        ],
    )
    async def test_ids_reach_the_row(
        self, tmp_path: Path, fixture: str, agent_id: str | None, tool_use_id: str
    ) -> None:
        payload = json.loads((FIXTURES / fixture).read_text())
        payload["tool_response"]["text"] = "[auth]\ntoken_ttl = 3600\n" + "pad " * 20
        call = get_adapter("claude").parse(payload)
        assert call is not None
        engine, tracker = _engine(
            tmp_path, [_result("m1", "auth token ttl note", "/notes/auth.md")]
        )
        try:
            out = await run_surfacing_hook(call, engine=engine, deadline_monotonic=None)
            assert out, "the hook surfaced nothing, so this test would prove nothing"
            await engine.drain_store_writes()
            (row,) = _event_rows(tracker.store.db_path)
            # The recorded length is what the host actually received.
            assert row["injected_chars"] == len(out["hookSpecificOutput"]["additionalContext"])
            assert row["host_session_id"] == "sess_demo"
            assert row["host_agent_id"] == agent_id
            assert row["tool_use_id"] == tool_use_id
            _assert_no_raw_text(tracker.store.db_path, forbidden=["/project"])
        finally:
            await engine.stop()
            tracker.close()


def test_now_is_monotonic_enough() -> None:
    # Guard for the retention test above: created_at = 0 is older than any
    # cutoff this machine's clock can produce.
    assert time.time() - 3600 > 0
