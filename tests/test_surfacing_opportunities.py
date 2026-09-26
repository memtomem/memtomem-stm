"""The ``surfacing_opportunities`` log: one row per call that entered surfacing.

Each call past the ``disabled`` check ends with exactly one label — surfaced,
a skip and its reason, an empty render, an error, a cancellation — and engines
that own a feedback tracker queue one fire-and-forget row carrying it. Rows
hold counts about the arguments, never their keys or values.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem_stm.cli.hook_adapter import get_adapter
from memtomem_stm.cli.hook_cmd import run_surfacing_hook
from memtomem_stm.server import _surfacing_verdict_line, stm_surfacing_stats
from memtomem_stm.surfacing import engine as engine_module
from memtomem_stm.surfacing.arg_shape import arg_shape_json
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.feedback import FeedbackTracker
from memtomem_stm.surfacing.feedback_store import FeedbackStore, OpportunityRow
from memtomem_stm.surfacing.observability import CallLedger, SurfacingObservability
from memtomem_stm.surfacing.store_io import StoreWriteQueueFull

FIXTURES = Path(__file__).parent / "fixtures" / "hooks" / "claude"
ABS_ROOT = "C:\\" if sys.platform == "win32" else "/"


def _abs(relative: str) -> str:
    return ABS_ROOT + relative.replace("/", os.sep)


ARGS = {"path": "src/app.py", "_context_query": "flask web framework"}
RESPONSE = "response body " * 20


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


def _result(memory_id: str, content: str = "flask routing uses blueprints") -> FakeResult:
    meta = FakeMeta(source_file=Path(_abs(f"notes/{memory_id}.md")))
    return FakeResult(chunk=FakeChunk(id=memory_id, content=content, metadata=meta))


class FixedRng:
    """Returns queued draws; records how often it was asked."""

    def __init__(self, draws: list[float] | None = None) -> None:
        self.draws = list(draws or [])
        self.calls = 0

    def random(self) -> float:
        self.calls += 1
        return self.draws.pop(0)


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


def _adapter(results: list[FakeResult], outcome: str = "ok") -> AsyncMock:
    adapter = AsyncMock()
    adapter.search = AsyncMock(return_value=(list(results), [], outcome))
    return adapter


def _engine(
    tmp_path: Path,
    results: list[FakeResult] | None = None,
    *,
    outcome: str = "ok",
    adapter: Any = None,
    rng: Any = None,
    observability: SurfacingObservability | None = None,
    **config: Any,
) -> tuple[SurfacingEngine, FeedbackTracker]:
    cfg = _config(**config)
    tracker = FeedbackTracker(config=cfg, db_path=tmp_path / "feedback.db")
    engine = SurfacingEngine(
        config=cfg,
        mcp_adapter=adapter if adapter is not None else _adapter(results or [], outcome),
        feedback_tracker=tracker,
        observability=observability if observability is not None else SurfacingObservability(),
        rng=rng,
    )
    return engine, tracker


def _rows(path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


def _opps(path: Path) -> list[sqlite3.Row]:
    return _rows(path, "SELECT * FROM surfacing_opportunities ORDER BY created_at, rowid")


def _events(path: Path) -> list[sqlite3.Row]:
    return _rows(path, "SELECT * FROM surfacing_events ORDER BY created_at")


async def _close(engine: SurfacingEngine, tracker: FeedbackTracker) -> None:
    await engine.stop()
    tracker.close()


# ── one label per call ─────────────────────────────────────────────────


class TestOneRowPerCall:
    async def test_surfaced_miss_then_hit(self, tmp_path: Path) -> None:
        engine, tracker = _engine(tmp_path, [_result("m1")])
        try:
            first = await engine.surface("gh", "read_file", ARGS, RESPONSE)
            second = await engine.surface("gh", "read_file", ARGS, RESPONSE)
            assert first != RESPONSE and second != RESPONSE
            await engine.drain_store_writes()
            path = tracker.store.db_path
            opps = _opps(path)
            events = _events(path)
            assert [o["gate_decision"] for o in opps] == ["surfaced", "surfaced"]
            assert [o["surfacing_id"] for o in opps] == [e["id"] for e in events]
            assert all(o["host_session_id"] is None for o in opps)  # proxy call
            assert [o["server"] for o in opps] == ["gh", "gh"]
            assert [o["response_len"] for o in opps] == [len(RESPONSE)] * 2
        finally:
            await _close(engine, tracker)

    @pytest.mark.parametrize(
        ("setup", "expected"),
        [
            ({"tool": "write_file"}, "skip:gate_write_tool"),
            ({"response": "short"}, "skip:response_too_short"),
            ({"outcome": "no_session"}, "skip:ltm_unavailable"),
            ({"outcome": "call_error"}, "skip:ltm_call_failed"),
            ({"outcome": "daemon_busy"}, "skip:daemon_busy"),
            ({"min_score": 0.9}, "skip:no_results_score"),
        ],
    )
    async def test_each_exit_leaves_exactly_one_labelled_row(
        self, tmp_path: Path, setup: dict[str, Any], expected: str
    ) -> None:
        setup = dict(setup)
        tool = setup.pop("tool", "read_file")
        response = setup.pop("response", RESPONSE)
        arguments = setup.pop("arguments", ARGS)
        outcome = setup.pop("outcome", "ok")
        engine, tracker = _engine(tmp_path, [_result("m1")], outcome=outcome, **setup)
        try:
            out = await engine.surface("gh", tool, arguments, response)
            assert out == response
            await engine.drain_store_writes()
            (row,) = _opps(tracker.store.db_path)
            assert row["gate_decision"] == expected
            assert row["surfacing_id"] is None
            assert _events(tracker.store.db_path) == []
        finally:
            await _close(engine, tracker)

    async def test_no_query(self, tmp_path: Path) -> None:
        engine, tracker = _engine(tmp_path, [_result("m1")])
        engine._extractor.extract_query = lambda *a, **k: None  # type: ignore[method-assign]
        try:
            assert await engine.surface("gh", "read_file", ARGS, RESPONSE) == RESPONSE
            await engine.drain_store_writes()
            (row,) = _opps(tracker.store.db_path)
            assert (row["gate_decision"], row["query_digest"]) == ("skip:no_query", None)
        finally:
            await _close(engine, tracker)

    async def test_no_results_row_carries_the_batch_scale(self, tmp_path: Path) -> None:
        engine, tracker = _engine(tmp_path, [_result("m1")], min_score=0.9)
        engine._result_score_scale = lambda results: ("rrf", None)  # type: ignore[method-assign]
        try:
            await engine.surface("gh", "read_file", ARGS, RESPONSE)
            await engine.drain_store_writes()
            (row,) = _opps(tracker.store.db_path)
            assert (row["gate_decision"], row["score_scale"]) == ("skip:no_results_score", "rrf")
        finally:
            await _close(engine, tracker)

    @pytest.mark.parametrize(("reported", "stored"), [("bm25", "bm25"), ("/home/a/x", "other")])
    async def test_score_scale_is_a_known_label_or_other(
        self, tmp_path: Path, reported: str, stored: str
    ) -> None:
        engine, tracker = _engine(tmp_path, [_result("m1")], min_score=0.9)
        engine._result_score_scale = lambda results: (reported, None)  # type: ignore[method-assign]
        try:
            await engine.surface("gh", "read_file", ARGS, RESPONSE)
            await engine.drain_store_writes()
            assert _opps(tracker.store.db_path)[-1]["score_scale"] == stored
        finally:
            await _close(engine, tracker)

    async def test_cooldown_label_comes_from_the_gate(self, tmp_path: Path) -> None:
        engine, tracker = _engine(tmp_path, [_result("m1")], cooldown_seconds=60.0)
        try:
            await engine.surface("gh", "read_file", ARGS, RESPONSE)
            await engine.surface("gh", "read_file", ARGS, RESPONSE)
            await engine.drain_store_writes()
            labels = [o["gate_decision"] for o in _opps(tracker.store.db_path)]
            assert labels == ["surfaced", "skip:gate_cooldown"]
        finally:
            await _close(engine, tracker)

    @pytest.mark.parametrize("path_kind", ["miss", "hit"])
    async def test_empty_render_is_labelled_and_counted(
        self, tmp_path: Path, path_kind: str
    ) -> None:
        obs = SurfacingObservability()
        engine, tracker = _engine(tmp_path, [_result("m1")], observability=obs)
        real_render = engine._formatter.render
        try:
            if path_kind == "hit":
                await engine.surface("gh", "read_file", ARGS, RESPONSE)  # warm the cache
            engine._formatter.render = lambda *a, **k: dataclasses.replace(  # type: ignore[method-assign]
                real_render(*a, **k), rendered_bullets=0
            )
            out = await engine.surface("gh", "read_file", ARGS, RESPONSE)
            assert out == RESPONSE
            await engine.drain_store_writes()
            assert _opps(tracker.store.db_path)[-1]["gate_decision"] == "empty_render"
            assert obs.snapshot()["skip_reasons"]["read_file"]["empty_render"] == 1
        finally:
            await _close(engine, tracker)


# ── exits that do not return ───────────────────────────────────────────


class TestUnusualExits:
    async def test_timeout(self, tmp_path: Path) -> None:
        async def slow(*_a: Any, **_k: Any) -> Any:
            await asyncio.sleep(5)

        adapter = AsyncMock()
        adapter.search = slow
        engine, tracker = _engine(tmp_path, adapter=adapter, timeout_seconds=0.05)
        try:
            assert await engine.surface("gh", "read_file", ARGS, RESPONSE) == RESPONSE
            await engine.drain_store_writes()
            (row,) = _opps(tracker.store.db_path)
            assert row["gate_decision"] == "error:timeout"
        finally:
            await _close(engine, tracker)

    async def test_abandoned_operation_cannot_relabel_the_row(self, tmp_path: Path) -> None:
        # An adapter that swallows the cancellation (breaking #290) and later
        # reports a failure: the late label must not reach the queued row.
        finished = asyncio.Event()

        async def stubborn(*_a: Any, **_k: Any) -> Any:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                pass
            finished.set()
            return ([], [], "call_error")

        adapter = AsyncMock()
        adapter.search = stubborn
        engine, tracker = _engine(tmp_path, adapter=adapter, timeout_seconds=0.05)
        try:
            await engine.surface("gh", "read_file", ARGS, RESPONSE)
            await asyncio.wait_for(finished.wait(), 5)
            await asyncio.sleep(0.05)
            await engine.drain_store_writes()
            (row,) = _opps(tracker.store.db_path)
            assert row["gate_decision"] == "error:timeout"
        finally:
            await _close(engine, tracker)

    async def test_cancelled_during_the_ltm_call(self, tmp_path: Path) -> None:
        started = asyncio.Event()

        async def hang(*_a: Any, **_k: Any) -> Any:
            started.set()
            await asyncio.Event().wait()

        adapter = AsyncMock()
        adapter.search = hang
        obs = SurfacingObservability()
        engine, tracker = _engine(tmp_path, adapter=adapter, observability=obs)
        try:
            task = asyncio.create_task(engine.surface("gh", "read_file", ARGS, RESPONSE))
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await engine.drain_store_writes()
            (row,) = _opps(tracker.store.db_path)
            assert row["gate_decision"] == "skip:cancelled"
            assert obs.snapshot()["skip_reasons"]["read_file"] == {"cancelled": 1}
        finally:
            await _close(engine, tracker)

    async def test_cancelled_while_waiting_for_the_key_lock(self, tmp_path: Path) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def gated(*_a: Any, **_k: Any) -> Any:
            started.set()
            await release.wait()
            return ([_result("m1")], [], "ok")

        adapter = AsyncMock()
        adapter.search = gated
        engine, tracker = _engine(tmp_path, adapter=adapter)
        try:
            leader = asyncio.create_task(engine.surface("gh", "read_file", ARGS, RESPONSE))
            await asyncio.wait_for(started.wait(), 5)
            follower = asyncio.create_task(engine.surface("gh", "read_file", ARGS, RESPONSE))
            await asyncio.sleep(0.02)  # the follower is now queued on the key lock
            follower.cancel()
            with pytest.raises(asyncio.CancelledError):
                await follower
            release.set()
            await leader
            await engine.drain_store_writes()
            labels = sorted(o["gate_decision"] for o in _opps(tracker.store.db_path))
            assert labels == ["skip:cancelled", "surfaced"]
        finally:
            await _close(engine, tracker)

    @pytest.mark.parametrize("exc_type", [ValueError, KeyboardInterrupt])
    async def test_exception_before_the_inner_try_propagates_labelled(
        self, tmp_path: Path, exc_type: type[BaseException]
    ) -> None:
        engine, tracker = _engine(tmp_path, [_result("m1")])

        def boom(*_a: Any, **_k: Any) -> Any:
            raise exc_type("extractor broke")

        engine._extractor.extract_query = boom  # type: ignore[method-assign]
        try:
            with pytest.raises(exc_type, match="extractor broke"):
                await engine.surface("gh", "read_file", ARGS, RESPONSE)
            await engine.drain_store_writes()
            (row,) = _opps(tracker.store.db_path)
            assert row["gate_decision"] == f"error:{exc_type.__name__}"
        finally:
            await _close(engine, tracker)

    @pytest.mark.parametrize("path_kind", ["miss", "hit"])
    async def test_failed_event_write_keeps_the_attempted_id(
        self, tmp_path: Path, path_kind: str
    ) -> None:
        engine, tracker = _engine(tmp_path, [_result("m1")])
        try:
            if path_kind == "hit":
                await engine.surface("gh", "read_file", ARGS, RESPONSE)  # warm the cache
                await engine.drain_store_writes()
            landed = len(_events(tracker.store.db_path))

            def refuse(*_a: Any, **_k: Any) -> bool:
                raise sqlite3.OperationalError("disk I/O error")

            tracker.record_surfacing = refuse  # type: ignore[method-assign]
            await engine.surface("gh", "read_file", ARGS, RESPONSE)
            await engine.drain_store_writes()
            row = _opps(tracker.store.db_path)[-1]
            assert row["gate_decision"] == "surfaced"
            assert row["surfacing_id"] is not None and len(row["surfacing_id"]) == 16
            assert row["surfacing_id"] not in {e["id"] for e in _events(tracker.store.db_path)}
            assert len(_events(tracker.store.db_path)) == landed
        finally:
            await _close(engine, tracker)

    async def test_event_write_refused_by_a_full_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine, tracker = _engine(tmp_path, [_result("m1")])

        async def full(*_a: Any, **_k: Any) -> Any:
            raise StoreWriteQueueFull("full")

        monkeypatch.setattr(engine, "_await_store_write", full)
        try:
            await engine.surface("gh", "read_file", ARGS, RESPONSE)
            await engine.drain_store_writes()
            (row,) = _opps(tracker.store.db_path)
            assert row["surfacing_id"] is not None
            assert _events(tracker.store.db_path) == []
        finally:
            await _close(engine, tracker)

    async def test_full_queue_loses_the_row_quietly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine, tracker = _engine(tmp_path, [_result("m1")])
        real_submit = engine_module.submit_store_write

        def submit(fn: Any, *args: Any) -> Any:
            target = getattr(fn, "func", None)
            if getattr(target, "__name__", "") == "record_opportunity":
                raise StoreWriteQueueFull("full")
            return real_submit(fn, *args)

        monkeypatch.setattr(engine_module, "submit_store_write", submit)
        try:
            out = await engine.surface("gh", "read_file", ARGS, RESPONSE)
            assert out != RESPONSE
            await engine.drain_store_writes()
            assert _opps(tracker.store.db_path) == []
            assert len(_events(tracker.store.db_path)) == 1
        finally:
            await _close(engine, tracker)


# ── coverage and sampling ──────────────────────────────────────────────


class TestCoverageAndSampling:
    async def test_tracker_less_engine_writes_nothing_and_draws_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rng = FixedRng()
        engine = SurfacingEngine(
            config=_config(opportunities_sample_rate=0.5),
            mcp_adapter=_adapter([_result("m1")]),
            rng=rng,  # type: ignore[arg-type]
        )
        submitted: list[Any] = []
        monkeypatch.setattr(engine, "_submit_store_write", lambda fn, **_k: submitted.append(fn))
        try:
            await engine.surface("gh", "read_file", ARGS, RESPONSE)
            assert submitted == [] and rng.calls == 0
        finally:
            await engine.stop()

    async def test_disabled_by_config(self, tmp_path: Path) -> None:
        rng = FixedRng()
        engine, tracker = _engine(
            tmp_path,
            [_result("m1")],
            rng=rng,
            opportunities_enabled=False,
            opportunities_sample_rate=0.5,
        )
        try:
            await engine.surface("gh", "read_file", ARGS, RESPONSE)
            await engine.drain_store_writes()
            assert _opps(tracker.store.db_path) == [] and rng.calls == 0
            assert len(_events(tracker.store.db_path)) == 1
        finally:
            await _close(engine, tracker)

    async def test_rate_zero_stores_none_and_counts_them(self, tmp_path: Path) -> None:
        obs = SurfacingObservability()
        engine, tracker = _engine(
            tmp_path, [_result("m1")], opportunities_sample_rate=0.0, observability=obs
        )
        try:
            await engine.surface("gh", "read_file", ARGS, RESPONSE)
            await engine.surface("gh", "write_file", ARGS, RESPONSE)
            await engine.drain_store_writes()
            path = tracker.store.db_path
            assert _opps(path) == []
            (event,) = _events(path)
            assert _rows(
                path, "SELECT COUNT(*) FROM surfacing_memory_paths WHERE surfacing_id = ?",
                (event["id"],),
            )[0][0] == 1
            assert obs.snapshot()["opportunities_sampled_out"] == {
                "read_file": 1,
                "write_file": 1,
                "__total__": 2,
            }
        finally:
            await _close(engine, tracker)

    async def test_rate_one_never_touches_the_rng(self, tmp_path: Path) -> None:
        rng = FixedRng()
        engine, tracker = _engine(tmp_path, [_result("m1")], rng=rng)
        try:
            await engine.surface("gh", "read_file", ARGS, RESPONSE)
            await engine.surface("gh", "write_file", ARGS, RESPONSE)
            await engine.drain_store_writes()
            assert len(_opps(tracker.store.db_path)) == 2 and rng.calls == 0
        finally:
            await _close(engine, tracker)

    async def test_fractional_rate_follows_the_draws(self, tmp_path: Path) -> None:
        rng = FixedRng([0.1, 0.9, 0.49])
        engine, tracker = _engine(
            tmp_path, [_result("m1")], rng=rng, opportunities_sample_rate=0.5
        )
        try:
            for tool in ("write_a", "write_b", "write_c"):
                await engine.surface("gh", tool, ARGS, RESPONSE)
            await engine.drain_store_writes()
            assert [o["tool"] for o in _opps(tracker.store.db_path)] == ["write_a", "write_c"]
            assert rng.calls == 3
        finally:
            await _close(engine, tracker)

    async def test_sampled_out_without_observability(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The engine's no-op observability must take the sampled-out count too.
        cfg = _config(opportunities_sample_rate=0.0)
        tracker = FeedbackTracker(config=cfg, db_path=tmp_path / "feedback.db")
        engine = SurfacingEngine(
            config=cfg, mcp_adapter=_adapter([_result("m1")]), feedback_tracker=tracker
        )
        try:
            with caplog.at_level("DEBUG", logger=engine_module.logger.name):
                out = await engine.surface("gh", "read_file", ARGS, RESPONSE)
            assert out != RESPONSE
            assert "Failed to queue surfacing opportunity row" not in caplog.text
        finally:
            await _close(engine, tracker)

    async def test_a_failing_row_never_replaces_the_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(*_a: Any, **_k: Any) -> str:
            raise RuntimeError("shape broke")

        monkeypatch.setattr(engine_module, "arg_shape_json", broken)
        engine, tracker = _engine(tmp_path, [_result("m1")])
        try:
            out = await engine.surface("gh", "read_file", ARGS, RESPONSE)
            assert out != RESPONSE
            await engine.drain_store_writes()
            assert _opps(tracker.store.db_path) == []
        finally:
            await _close(engine, tracker)


# ── the hook path ──────────────────────────────────────────────────────


class TestHookPath:
    def _call(self, tool_name: str | None = None) -> Any:
        payload = json.loads((FIXTURES / "inbound_read_posttooluse.json").read_text())
        payload["tool_response"]["text"] = "[auth]\ntoken_ttl = 3600\n" + "pad " * 20
        if tool_name is not None:
            payload["tool_name"] = tool_name
        call = get_adapter("claude").parse(payload)
        assert call is not None
        return call

    async def test_eligible_call_stores_the_session(self, tmp_path: Path) -> None:
        engine, tracker = _engine(tmp_path, [_result("m1", "auth token ttl note")])
        try:
            out = await run_surfacing_hook(self._call(), engine=engine, deadline_monotonic=None)
            assert out, "the hook surfaced nothing, so this test would prove nothing"
            await engine.drain_store_writes()
            (row,) = _opps(tracker.store.db_path)
            assert (row["host_session_id"], row["gate_decision"]) == ("sess_demo", "surfaced")
        finally:
            await _close(engine, tracker)

    async def test_ineligible_tool_never_enters_surface(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine, tracker = _engine(tmp_path, [_result("m1", "auth token ttl note")])
        entered: list[str] = []
        real_surface = engine.surface

        async def spy(*args: Any, **kwargs: Any) -> str:
            entered.append(args[1])
            return await real_surface(*args, **kwargs)

        monkeypatch.setattr(engine, "surface", spy)
        try:
            await run_surfacing_hook(self._call("Write"), engine=engine, deadline_monotonic=None)
            await engine.drain_store_writes()
            assert entered == [] and _opps(tracker.store.db_path) == []
            # Positive control: the same engine records an eligible call.
            await run_surfacing_hook(self._call(), engine=engine, deadline_monotonic=None)
            await engine.drain_store_writes()
            assert entered == ["Read"] and len(_opps(tracker.store.db_path)) == 1
        finally:
            await _close(engine, tracker)


# ── privacy ────────────────────────────────────────────────────────────


class TestArgShape:
    def test_values_never_stored(self) -> None:
        arguments = {
            "file_path": _abs("secret/project/report.alice"),
            "password": "hunter2",
            "apiKey": "sk-live",
            "x_auth_token": "tok",
            "bad key!": 1,
            "k" * 40: 1,
            "offset": 10,
            7: "non-string key",
        }
        shape = json.loads(arg_shape_json(arguments, 3))
        assert shape == {
            "key_count": 8,
            "path_depth": len(Path(_abs("secret/project/report.alice")).parts),
            "ext": "other",
            "query_tokens": 3,
        }

    @pytest.mark.parametrize(
        ("value", "ext"), [("a/b.PY", ".py"), ("a/b", None), ("a/b.tar.gz", "other")]
    )
    def test_ext_is_a_known_type_or_other(self, value: str, ext: str | None) -> None:
        assert json.loads(arg_shape_json({"path": value}, None))["ext"] == ext

    def test_non_mapping_arguments(self) -> None:
        assert json.loads(arg_shape_json(["x"], None)) == {
            "key_count": 0,
            "path_depth": None,
            "ext": None,
            "query_tokens": None,
        }

    def test_caller_chosen_key_names_are_not_stored(self) -> None:
        # A tool that accepts arbitrary keys lets the caller name them, so an
        # identifier-shaped, credential-free key is still user text.
        shape = arg_shape_json({"home_alice": 1, "customer_name": 2}, None)
        assert "alice" not in shape and "customer" not in shape
        assert json.loads(shape)["key_count"] == 2

    async def test_no_raw_text_in_any_row(self, tmp_path: Path) -> None:
        engine, tracker = _engine(tmp_path, [_result("m1")])
        arguments = {
            "file_path": _abs("secret/project/report.alice"),
            "command": "curl https://internal.example/deploy",
        }
        cwd = _abs("private/workdir")
        try:
            await engine.surface(
                "gh", "read_file", arguments, RESPONSE, session_id="sess", cwd=cwd
            )
            await engine.surface(
                "gh", "read_file", {"_context_query": "password=hunter2"}, RESPONSE
            )
            await engine.drain_store_writes()
            rows = _opps(tracker.store.db_path)
            assert len(rows) == 2
            forbidden = ["secret", "report", "alice", "curl", "internal", "private", "hunter2"]
            for row in rows:
                for value in tuple(row):
                    for needle in forbidden:
                        assert needle not in str(value), needle
            # The digest is taken from the extracted query, before substitution.
            assert rows[1]["query_digest"] == engine._hashed_query("password=hunter2")
        finally:
            await _close(engine, tracker)


# ── store, stats and retention ─────────────────────────────────────────


def _row(**overrides: Any) -> OpportunityRow:
    fields: dict[str, Any] = {
        "id": "o1",
        "server": "gh",
        "tool": "read_file",
        "arg_shape_json": "{}",
        "response_len": 10,
        "gate_decision": "skip:gate_cooldown",
    }
    fields.update(overrides)
    return OpportunityRow(**fields)


class TestStore:
    def _store(self, tmp_path: Path) -> FeedbackStore:
        store = FeedbackStore(tmp_path / "fb.db")
        store.initialize()
        return store

    def test_stats_report_opportunities_without_events(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        try:
            store.record_opportunity(_row(id="o1"))
            store.record_opportunity(_row(id="o2", tool="grep"))
            store.record_opportunity(_row(id="o3", gate_decision="surfaced"))
            stats = store.get_stats()
            assert stats["events_total"] == 0
            assert stats["opportunities_total"] == 3
            assert stats["opportunity_decisions"] == {"skip:gate_cooldown": 2, "surfaced": 1}
            filtered = store.get_stats(tool="grep")
            assert filtered["opportunities_total"] == 1
            assert store.get_stats(since=time.time() + 60)["opportunities_total"] == 0
        finally:
            store.close()

    def test_closed_store_returns_false(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.close()
        assert store.record_opportunity(_row()) is False

    def test_initialize_is_idempotent(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.record_opportunity(_row())
        store.close()
        again = self._store(tmp_path)
        try:
            assert again.get_stats()["opportunities_total"] == 1
        finally:
            again.close()

    def test_retention_deletes_old_opportunities(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        try:
            store.record_opportunity(_row(id="old"))
            store.record_opportunity(_row(id="new"))
            db = sqlite3.connect(str(store.db_path))
            db.execute("UPDATE surfacing_opportunities SET created_at = 0 WHERE id = 'old'")
            db.commit()
            db.close()
            store.delete_events_older_than(3600)
            ids = [r["id"] for r in _rows(store.db_path, "SELECT id FROM surfacing_opportunities")]
            assert ids == ["new"]
        finally:
            store.close()

    def test_failed_sweep_rolls_back_every_delete(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        try:
            store.record_surfacing("ev1", "gh", "t", "q", ["m1"], [0.5])
            store.record_feedback("ev1", "helpful", "m1")
            db = sqlite3.connect(str(store.db_path))
            db.execute("UPDATE surfacing_events SET created_at = 0")
            db.commit()
            db.close()

            real = store._db
            assert real is not None

            class FailOnOpportunities:
                def execute(self, sql: str, *args: Any) -> Any:
                    if "surfacing_opportunities" in sql:
                        raise sqlite3.OperationalError("disk I/O error")
                    return real.execute(sql, *args)

                def __getattr__(self, name: str) -> Any:
                    return getattr(real, name)

            store._db = FailOnOpportunities()  # type: ignore[assignment]
            with pytest.raises(sqlite3.OperationalError):
                store.delete_events_older_than(3600)
            store._db = real
            # The next unrelated write commits; the earlier DELETE must not ride along.
            store.record_opportunity(_row())
            feedback = _rows(store.db_path, "SELECT COUNT(*) FROM surfacing_feedback")[0][0]
            assert feedback == 1
            assert len(_events(store.db_path)) == 1
        finally:
            store.close()


class TestStatsRendering:
    def _ctx(self, stats: dict[str, Any], sampled_out: dict[str, int]) -> Any:
        tracker = MagicMock()
        tracker.get_stats.return_value = stats
        tracker.store.get_per_tool_feedback_counts.return_value = {}
        obs = SurfacingObservability()
        obs.record_skip("t", "gate_cooldown")
        for tool, count in sampled_out.items():
            for _ in range(count):
                obs.record_opportunity_sampled_out(tool)
        engine = SurfacingEngine(config=_config(), mcp_adapter=AsyncMock(), observability=obs)
        app = SimpleNamespace(feedback_tracker=tracker, surfacing_engine=engine)
        return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))

    def _stats(self, **overrides: Any) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "events_total": 0,
            "distinct_tools": 0,
            "date_range": {"first": None, "last": None},
            "per_tool_breakdown": [],
            "rating_distribution": {},
            "total_feedback": 0,
            "recent": [],
            "score_distribution": {"count": 0, "min": None, "max": None},
            "score_scale_distribution": {},
            "opportunities_total": 0,
            "opportunity_decisions": {},
        }
        stats.update(overrides)
        return stats

    async def test_line_and_breakdown(self) -> None:
        out = await stm_surfacing_stats(
            ctx=self._ctx(
                self._stats(
                    opportunities_total=5,
                    opportunity_decisions={"surfaced": 1, "skip:gate_cooldown": 4},
                ),
                {"t": 2, "u": 1},
            )
        )
        assert "Opportunities:   5 stored (+3 sampled out, this process)" in out.splitlines()
        assert "  by decision:   skip:gate_cooldown 4, surfaced 1" in out.splitlines()

    async def test_tool_filter_uses_that_tools_sampled_out(self) -> None:
        out = await stm_surfacing_stats(
            tool="t",
            ctx=self._ctx(
                self._stats(opportunities_total=1, opportunity_decisions={"surfaced": 1}),
                {"t": 2, "u": 1},
            ),
        )
        assert "Opportunities:   1 stored (+2 sampled out, this process)" in out.splitlines()

    async def test_since_window_leaves_out_the_untimed_count(self) -> None:
        out = await stm_surfacing_stats(
            since="2026-01-01T00:00:00",
            ctx=self._ctx(
                self._stats(opportunities_total=1, opportunity_decisions={"surfaced": 1}),
                {"t": 2},
            ),
        )
        assert "Opportunities:   1 stored" in out.splitlines()

    async def test_no_line_without_opportunities(self) -> None:
        out = await stm_surfacing_stats(ctx=self._ctx(self._stats(), {}))
        assert "Opportunities" not in out


class TestVerdictAndLedger:
    def test_empty_render_completes_and_cancelled_does_not(self) -> None:
        line = _surfacing_verdict_line(
            {
                "skip_reasons": {"__total__": {"cancelled": 4, "empty_render": 3}},
                "outcomes": {},
            },
            tool_filter=None,
        )
        # These calls used to record nothing. An empty render follows a
        # completed search, so it is an attempt; a cancellation decided nothing.
        assert line is not None and "insufficient data — 3 LTM attempts" in line

    def test_cancelled_is_neither_a_fault_nor_a_timeout(self) -> None:
        ledger = CallLedger(skip_reasons=["cancelled"])
        assert not ledger.faulted and not ledger.timed_out
