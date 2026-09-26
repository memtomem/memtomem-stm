"""The randomized holdout: a drawn hook call is ``shown`` or ``withheld``.

A ``withheld`` call returns the tool response unchanged, yet every durable
write up to and including its event row is the same as the ``shown`` call's:
the claim, the cooldown, the event row (except ``arm`` and
``injected_chars``), the dedup rows and the memory-path rows. The arm is fixed
before the write, so it holds on every way the write can end.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.hook_adapter import get_adapter
from memtomem_stm.cli.hook_cmd import run_surfacing_hook
from memtomem_stm.cli.proxy import cli
from memtomem_stm.config import STMConfig
from memtomem_stm.server import _surfacing_verdict_line, stm_surfacing_stats
from memtomem_stm.surfacing import store_io
from memtomem_stm.surfacing.config import SurfacingConfig, holdout_startup_warnings
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.feedback import FeedbackTracker
from memtomem_stm.surfacing.feedback_store import (
    EventProvenance,
    FeedbackStore,
    read_surfacing_summary,
)
from memtomem_stm.surfacing.observability import SurfacingObservability
from memtomem_stm.surfacing.store_io import submit_store_write

from helpers import set_home
from test_surfacing_opportunities import (
    ARGS,
    FIXTURES,
    RESPONSE,
    FakeChunk,
    FakeMeta,
    FakeResult,
    FixedRng,
    _close,
    _adapter,
    _config,
    _engine,
    _events,
    _opps,
    _result,
    _rows,
)

HOST = {"session_id": "sess-1", "tool_use_id": "toolu_1", "host": "claude"}
WITHHELD_DRAW = 0.1  # < 0.5
SHOWN_DRAW = 0.9  # >= 0.5
DRAWS = {"withheld": WITHHELD_DRAW, "shown": SHOWN_DRAW}


def _holdout_engine(
    tmp_path: Path,
    arm: str | None,
    results: list[FakeResult] | None = None,
    *,
    adapter: Any = None,
    **config: Any,
) -> tuple[SurfacingEngine, FeedbackTracker, FixedRng]:
    rng = FixedRng([] if arm is None else [DRAWS[arm]])
    config.setdefault("holdout_rate", 0.5)
    engine, tracker = _engine(
        tmp_path, results or [_result("m1")], adapter=adapter, rng=rng, **config
    )
    return engine, tracker, rng


PINNED = FakeResult(
    chunk=FakeChunk(id="p1", content="pinned rule: always run tests with uv"), pinned=True
)
SCRATCH = [{"key": "current_task", "value": "wiring the holdout draw"}]


def _whole_block_adapter() -> Any:
    """A retrieved memory, a pinned one and a session-context item."""
    adapter = _adapter([_result("m1"), PINNED])
    adapter.scratch_list = AsyncMock(return_value=list(SCRATCH))
    return adapter


async def _warm(engine: SurfacingEngine) -> None:
    """Fill the result cache with an undrawn (proxy-shaped) call."""
    first = await engine.surface("gh", "read_file", ARGS, RESPONSE)
    assert first != RESPONSE, "the warm-up surfaced nothing"
    await engine.drain_store_writes()


async def _drawn(engine: SurfacingEngine) -> str:
    return await engine.surface("gh", "read_file", ARGS, RESPONSE, **HOST)


async def _settle(engine: SurfacingEngine) -> None:
    await engine.drain_store_writes()
    # An abandoned write's done-callback runs on the loop after the worker's
    # future completes; give it the iterations to get there.
    for _ in range(5):
        await asyncio.sleep(0)


def _unrecorded(engine: SurfacingEngine) -> dict[str, int]:
    obs = engine.observability
    assert obs is not None
    return obs.snapshot()["holdout_unrecorded"]


def _outcomes(engine: SurfacingEngine) -> dict[str, int]:
    obs = engine.observability
    assert obs is not None
    return obs.snapshot()["outcomes"].get("__total__", {})


def _without(row: sqlite3.Row, *keys: str) -> dict[str, Any]:
    return {k: row[k] for k in row.keys() if k not in keys}


@pytest.fixture
def blocker() -> Any:
    """Park the store worker so the next queued write waits behind it."""
    event = threading.Event()
    submit_store_write(lambda: event.wait(timeout=10.0))
    yield event
    event.set()


@pytest.fixture
def short_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store_io, "STORE_WRITE_BUDGET_SECONDS", 0.3)


# ── parity (both arms write the same) ──────────────────────────────────


class TestParity:
    @pytest.mark.parametrize("path_kind", ["miss", "hit"])
    async def test_withheld_writes_what_shown_writes(self, tmp_path: Path, path_kind: str) -> None:
        webhooks = {}
        runs: dict[str, dict[str, Any]] = {}
        for arm in ("withheld", "shown"):
            engine, tracker, rng = _holdout_engine(
                tmp_path / arm,
                arm,
                adapter=_whole_block_adapter(),
                fire_webhook=True,
                include_session_context=True,
            )
            webhook = MagicMock()
            webhook.fire = AsyncMock()
            engine._webhook_manager = webhook
            webhooks[arm] = webhook
            if arm == "shown":
                # Same key, so the keyed path hashes compare across the two DBs.
                tracker.store._hmac_key = runs["withheld"]["key"]
            cooldowns: list[str] = []
            real_record = engine._gate.record_surfacing
            engine._gate.record_surfacing = lambda q, _r=real_record, _c=cooldowns: (  # type: ignore[method-assign]
                _c.append(q),
                _r(q),
            )[1]
            try:
                if path_kind == "hit":
                    await _warm(engine)
                    webhook.fire.reset_mock()
                out = await _drawn(engine)
                await engine.drain_store_writes()
                path = tracker.store.db_path
                event = _events(path)[-1]
                runs[arm] = {
                    "key": tracker.store._hmac_key,
                    "out": out,
                    "rng_calls": rng.calls,
                    "event": event,
                    "paths": [
                        _without(r, "surfacing_id")
                        for r in _rows(
                            path,
                            "SELECT * FROM surfacing_memory_paths WHERE surfacing_id = ?",
                            (event["id"],),
                        )
                    ],
                    "seen": [
                        (r["memory_id"], r["seen_count"])
                        for r in _rows(path, "SELECT * FROM seen_memories ORDER BY memory_id")
                    ],
                    "claims": sorted(engine._surfaced_ids),
                    "cooldowns": list(cooldowns),
                    "opp": _opps(path)[-1],
                    "outcomes": _outcomes(engine),
                }
            finally:
                await _close(engine, tracker)

        withheld, shown = runs["withheld"], runs["shown"]
        # The treatment is the whole block: shown carries the retrieved and
        # pinned memories (and, on the miss path, the session-context item);
        # withheld returns the response byte for byte.
        assert withheld["out"] == RESPONSE
        assert shown["out"] != RESPONSE and "<surfaced-memories>" in shown["out"]
        assert "flask routing uses blueprints" in shown["out"]
        assert "always run tests with uv" in shown["out"]
        if path_kind == "miss":
            assert "wiring the holdout draw" in shown["out"]
        assert withheld["rng_calls"] == shown["rng_calls"] == 1

        ignore = ("id", "created_at", "arm", "injected_chars")
        assert _without(withheld["event"], *ignore) == _without(shown["event"], *ignore)
        assert (withheld["event"]["arm"], withheld["event"]["injected_chars"]) == ("withheld", 0)
        assert shown["event"]["arm"] == "shown" and shown["event"]["injected_chars"] > 0
        assert withheld["event"]["holdout_rate"] == shown["event"]["holdout_rate"] == 0.5

        assert withheld["paths"] and withheld["paths"] == shown["paths"]
        assert withheld["paths"][0]["snippet_grams"] not in (None, "[]")
        assert withheld["seen"] and withheld["seen"] == shown["seen"]
        assert withheld["claims"] and withheld["claims"] == shown["claims"]
        assert withheld["cooldowns"] == shown["cooldowns"]

        assert withheld["opp"]["gate_decision"] == "held_out"
        assert shown["opp"]["gate_decision"] == "surfaced"
        assert (withheld["opp"]["arm"], shown["opp"]["arm"]) == ("withheld", "shown")
        assert withheld["opp"]["holdout_rate"] == 0.5
        assert withheld["opp"]["surfacing_id"] == withheld["event"]["id"]

        hit_or_miss = "surfaced_cache_hit" if path_kind == "hit" else "surfaced_cache_miss"
        assert withheld["outcomes"].get("held_out") == 1
        assert withheld["outcomes"].get(hit_or_miss, 0) == 0
        assert shown["outcomes"].get(hit_or_miss) == 1
        assert "held_out" not in shown["outcomes"]

        webhooks["withheld"].fire.assert_not_called()
        if path_kind == "miss":
            webhooks["shown"].fire.assert_called_once()

    async def test_held_out_completes_the_verdict_but_is_not_a_surfacing(self) -> None:
        line = _surfacing_verdict_line(
            {"skip_reasons": {}, "outcomes": {"__total__": {"held_out": 3}}},
            tool_filter=None,
        )
        assert line is not None and "insufficient data — 3 LTM attempts" in line


# ── who is drawn ───────────────────────────────────────────────────────


class TestWhoIsDrawn:
    @pytest.mark.parametrize(
        ("config", "call"),
        [
            ({"holdout_rate": 0.0}, HOST),
            ({}, {**HOST, "tool_use_id": None}),
            ({}, {**HOST, "session_id": None}),
            ({}, {**HOST, "host": "codex"}),
            ({}, {}),  # the proxy path passes no host
        ],
        ids=["rate-zero", "no-tool-use-id", "no-session-id", "other-host", "proxy"],
    )
    async def test_never_drawn(
        self, tmp_path: Path, config: dict[str, Any], call: dict[str, Any]
    ) -> None:
        engine, tracker, rng = _holdout_engine(tmp_path, None, **config)
        try:
            out = await engine.surface("gh", "read_file", ARGS, RESPONSE, **call)
            await engine.drain_store_writes()
            assert out != RESPONSE
            (event,) = _events(tracker.store.db_path)
            assert (event["arm"], event["holdout_rate"]) == (None, None)
            assert _opps(tracker.store.db_path)[-1]["arm"] is None
            assert rng.calls == 0
        finally:
            await _close(engine, tracker)

    async def test_no_eligible_memory_is_not_drawn(self, tmp_path: Path) -> None:
        relative = FakeResult(
            chunk=FakeChunk(
                id="m1",
                content="flask routing uses blueprints",
                metadata=FakeMeta(source_file=Path("notes/m1.md")),
            )
        )
        engine, tracker, rng = _holdout_engine(tmp_path, None, [relative])
        try:
            out = await _drawn(engine)
            await engine.drain_store_writes()
            assert out != RESPONSE
            (event,) = _events(tracker.store.db_path)
            assert event["arm"] is None and rng.calls == 0
        finally:
            await _close(engine, tracker)

    async def test_tracker_less_engine_never_draws(self) -> None:
        rng = FixedRng([])
        engine = SurfacingEngine(
            config=_config(holdout_rate=0.5),
            mcp_adapter=AsyncMock(search=AsyncMock(return_value=([_result("m1")], [], "ok"))),
            rng=rng,
        )
        try:
            assert await _drawn(engine) != RESPONSE
            assert rng.calls == 0
        finally:
            await engine.stop()

    async def test_drawn_row_is_never_sampled_out(self, tmp_path: Path) -> None:
        # Only the draw consumes the RNG: sampling is skipped for a drawn call.
        engine, tracker, rng = _holdout_engine(
            tmp_path, "withheld", opportunities_sample_rate=0.0
        )
        try:
            await _drawn(engine)
            await engine.drain_store_writes()
            (opp,) = _opps(tracker.store.db_path)
            assert opp["arm"] == "withheld" and rng.calls == 1
            assert (_events(tracker.store.db_path)[0]["arm"]) == "withheld"
        finally:
            await _close(engine, tracker)

    async def test_hook_path_forwards_the_host(self, tmp_path: Path) -> None:
        payload = json.loads((FIXTURES / "inbound_read_posttooluse.json").read_text())
        payload["tool_response"]["text"] = "[auth]\ntoken_ttl = 3600\n" + "pad " * 20
        call = get_adapter("claude").parse(payload)
        assert call is not None and call.host_tag == "claude"
        engine, tracker, _rng = _holdout_engine(
            tmp_path, "withheld", [_result("m1", "auth token ttl note")]
        )
        try:
            out = await run_surfacing_hook(call, engine=engine, deadline_monotonic=None)
            await engine.drain_store_writes()
            (event,) = _events(tracker.store.db_path)
            assert event["arm"] == "withheld"
            assert not out, "a withheld call must add no context"
        finally:
            await _close(engine, tracker)


# ── config ─────────────────────────────────────────────────────────────


class TestConfig:
    def test_env_value_clamps_and_keeps_the_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMTOMEM_STM_SURFACING__HOLDOUT_RATE", "0.9")
        cfg = STMConfig().surfacing
        assert (cfg.holdout_rate, cfg.holdout_rate_requested) == (0.5, 0.9)
        # The daemon's copy keeps the record; so does re-validating the model.
        copied = cfg.model_copy(update={"cache_ttl_seconds": 0.0})
        assert (copied.holdout_rate, copied.holdout_rate_requested) == (0.5, 0.9)
        revalidated = SurfacingConfig.model_validate(cfg, from_attributes=True)
        assert revalidated.holdout_rate == 0.5

    @pytest.mark.parametrize(("value", "clamped"), [(-1.0, 0.0), (0.3, 0.3), (7.0, 0.5)])
    def test_direct_construction(self, value: float, clamped: float) -> None:
        cfg = SurfacingConfig(holdout_rate=value)
        assert cfg.holdout_rate == clamped
        assert cfg.holdout_rate_requested == (None if value == clamped else value)

    def test_nan_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            SurfacingConfig(holdout_rate=float("nan"))

    def test_clamp_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        # The hook process builds the config more than once per call with no
        # logging configured; a warning here would reach the host every call.
        with caplog.at_level(logging.DEBUG):
            SurfacingConfig(holdout_rate=0.9)
        assert caplog.records == []

    async def test_row_records_the_clamped_rate(self, tmp_path: Path) -> None:
        engine, tracker, _rng = _holdout_engine(tmp_path, "shown", holdout_rate=0.9)
        try:
            await _drawn(engine)
            await engine.drain_store_writes()
            assert _events(tracker.store.db_path)[0]["holdout_rate"] == 0.5
        finally:
            await _close(engine, tracker)

    def test_startup_warnings(self) -> None:
        assert holdout_startup_warnings(SurfacingConfig(), proxy_path=True, has_tracker=True) == []
        clamped = SurfacingConfig(holdout_rate=0.9)
        (clamp,) = holdout_startup_warnings(clamped, proxy_path=False, has_tracker=True)
        assert "0.9" in clamp and "0.5" in clamp
        (proxy,) = holdout_startup_warnings(
            SurfacingConfig(holdout_rate=0.2), proxy_path=True, has_tracker=True
        )
        assert "proxy path never draws" in proxy
        (no_tracker,) = holdout_startup_warnings(
            SurfacingConfig(holdout_rate=0.2), proxy_path=False, has_tracker=False
        )
        assert "no feedback tracker" in no_tracker


# ── the arm holds on every write path ──────────────────────────────────


def _refuse(*_a: Any, **_k: Any) -> bool:
    raise sqlite3.OperationalError("disk I/O error")


@pytest.mark.parametrize("path_kind", ["miss", "hit"])
@pytest.mark.parametrize("arm", ["withheld", "shown"])
class TestWritePaths:
    async def _setup(
        self, tmp_path: Path, arm: str, path_kind: str
    ) -> tuple[SurfacingEngine, FeedbackTracker]:
        engine, tracker, _rng = _holdout_engine(tmp_path, arm)
        if path_kind == "hit":
            await _warm(engine)
        return engine, tracker

    def _assert_returned(self, out: str, arm: str, surfacing_id: str) -> None:
        if arm == "withheld":
            assert out == RESPONSE
        else:
            assert out != RESPONSE and "<surfaced-memories>" in out
            assert surfacing_id not in out, "the dead feedback id must be withdrawn"

    @pytest.mark.parametrize("how", ["raises", "store-closed"])
    async def test_fails_outright(self, tmp_path: Path, arm: str, path_kind: str, how: str) -> None:
        # ``store-closed``: the tracker answers False instead of raising; each
        # path turns that into its own RuntimeError, which must count once.
        engine, tracker = await self._setup(tmp_path, arm, path_kind)
        try:
            landed = len(_events(tracker.store.db_path))
            tracker.record_surfacing = (  # type: ignore[method-assign]
                _refuse if how == "raises" else (lambda *_a, **_k: False)
            )
            out = await _drawn(engine)
            await _settle(engine)
            opp = _opps(tracker.store.db_path)[-1]
            self._assert_returned(out, arm, opp["surfacing_id"])
            assert _unrecorded(engine) == {arm: 1}
            assert len(_events(tracker.store.db_path)) == landed
            # The orphan opportunity row is the durable trace of the draw.
            assert (opp["arm"], opp["surfacing_id"] is not None) == (arm, True)
        finally:
            await _close(engine, tracker)

    async def test_times_out_and_lands_later(
        self, tmp_path: Path, arm: str, path_kind: str, short_budget: None
    ) -> None:
        engine, tracker = await self._setup(tmp_path, arm, path_kind)
        event = threading.Event()
        submit_store_write(lambda: event.wait(timeout=10.0))
        try:
            out = await _drawn(engine)
            event.set()
            await _settle(engine)
            late = _events(tracker.store.db_path)[-1]
            self._assert_returned(out, arm, late["id"])
            assert late["arm"] == arm
            assert late["id_advertised"] == 1
            assert _unrecorded(engine) == {}
        finally:
            event.set()
            await _close(engine, tracker)

    async def test_cancelled_lands_with_its_arm(
        self, tmp_path: Path, arm: str, path_kind: str
    ) -> None:
        engine, tracker = await self._setup(tmp_path, arm, path_kind)
        event = threading.Event()
        submit_store_write(lambda: event.wait(timeout=10.0))
        try:
            task = asyncio.create_task(_drawn(engine))
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            event.set()
            await _settle(engine)
            assert _events(tracker.store.db_path)[-1]["arm"] == arm
            opp = _opps(tracker.store.db_path)[-1]
            assert (opp["gate_decision"], opp["arm"]) == ("skip:cancelled", arm)
            assert _unrecorded(engine) == {}
        finally:
            event.set()
            await _close(engine, tracker)

    @pytest.mark.parametrize("how", ["timeout", "cancel"])
    async def test_abandoned_then_fails(
        self, tmp_path: Path, arm: str, path_kind: str, how: str, short_budget: None
    ) -> None:
        engine, tracker = await self._setup(tmp_path, arm, path_kind)
        landed = len(_events(tracker.store.db_path))
        tracker.record_surfacing = _refuse  # type: ignore[method-assign]
        event = threading.Event()
        submit_store_write(lambda: event.wait(timeout=10.0))
        try:
            if how == "timeout":
                await _drawn(engine)
            else:
                task = asyncio.create_task(_drawn(engine))
                await asyncio.sleep(0.1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert _unrecorded(engine) == {}, "not counted while the write can still land"
            event.set()
            await _settle(engine)
            assert _unrecorded(engine) == {arm: 1}
            assert len(_events(tracker.store.db_path)) == landed
            assert _rows(tracker.store.db_path, "SELECT * FROM surfacing_faults") == []
        finally:
            event.set()
            await _close(engine, tracker)

    async def test_queued_at_shutdown(
        self, tmp_path: Path, arm: str, path_kind: str, short_budget: None
    ) -> None:
        engine, tracker = await self._setup(tmp_path, arm, path_kind)
        landed = len(_events(tracker.store.db_path))
        event = threading.Event()
        submit_store_write(lambda: event.wait(timeout=10.0))
        try:
            await _drawn(engine)  # times out, still queued behind the blocker
            store_io.shutdown_worker(wait=False)
            event.set()
            for _ in range(5):
                await asyncio.sleep(0)
            # Documented residual: the write is dropped, nothing counts it.
            assert _unrecorded(engine) == {}
            assert len(_events(tracker.store.db_path)) == landed
        finally:
            event.set()
            await _close(engine, tracker)

    async def test_queue_full(
        self, tmp_path: Path, arm: str, path_kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine, tracker = await self._setup(tmp_path, arm, path_kind)
        path = tracker.store.db_path
        before = (len(_events(path)), len(_opps(path)))
        try:
            with monkeypatch.context() as patch:
                patch.setattr(store_io, "MAX_QUEUED_WRITES", 0)
                out = await _drawn(engine)
            await _settle(engine)
            if arm == "withheld":
                assert out == RESPONSE
            assert (len(_events(path)), len(_opps(path))) == before
            assert _unrecorded(engine) == {arm: 1}
        finally:
            await _close(engine, tracker)


# ── stats ──────────────────────────────────────────────────────────────


def _seed(path: Path) -> FeedbackStore:
    store = FeedbackStore(path)
    store.initialize()
    for sid, tool, arm in (
        ("e-null", "read_file", None),
        ("e-shown", "read_file", "shown"),
        ("e-held", "read_file", "withheld"),
        ("e-held-2", "grep", "withheld"),
    ):
        store.record_surfacing(
            surfacing_id=sid,
            server="builtin",
            tool=tool,
            query="q",
            memory_ids=["m1"],
            scores=[0.5],
            provenance=EventProvenance(arm=arm, holdout_rate=None if arm is None else 0.5),
        )
    return store


class TestStats:
    def test_get_stats_counts_shown_and_reports_withheld(self, tmp_path: Path) -> None:
        store = _seed(tmp_path / "f.db")
        try:
            stats = store.get_stats()
            assert (stats["events_total"], stats["withheld_total"]) == (2, 2)
            assert stats["distinct_tools"] == 1
            assert [t["tool"] for t in stats["per_tool_breakdown"]] == ["read_file"]
            assert len(stats["recent"]) == 2
            # A window holding only withheld events still reports them.
            only_grep = store.get_stats(tool="grep")
            assert (only_grep["events_total"], only_grep["withheld_total"]) == (0, 1)
            assert store.get_tool_feedback_summary()["total_surfacings"] == 2
            assert store.get_tool_feedback_summary("grep")["total_surfacings"] == 0
        finally:
            store.close()

    def test_read_surfacing_summary(self, tmp_path: Path) -> None:
        _seed(tmp_path / "f.db").close()
        summary = read_surfacing_summary(tmp_path / "f.db")
        assert (summary["events_total"], summary["withheld_total"]) == (2, 2)
        assert summary["distinct_tools"] == 1
        filtered = read_surfacing_summary(tmp_path / "f.db", tool="grep")
        assert (filtered["events_total"], filtered["withheld_total"]) == (0, 1)

    def test_summary_on_a_db_without_the_arm_column(self, tmp_path: Path) -> None:
        path = tmp_path / "f.db"
        _seed(path).close()
        db = sqlite3.connect(str(path))
        db.execute("ALTER TABLE surfacing_events DROP COLUMN holdout_rate")
        db.execute("ALTER TABLE surfacing_events DROP COLUMN arm")
        db.commit()
        db.close()
        summary = read_surfacing_summary(path)
        assert (summary["events_total"], summary["withheld_total"]) == (4, 0)
        columns = {r[1] for r in sqlite3.connect(str(path)).execute("PRAGMA table_info('surfacing_events')")}
        assert "arm" not in columns, "the read-only summary must not migrate"

    def test_mms_stats_on_a_pre_migration_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_home(monkeypatch, tmp_path)
        path = tmp_path / ".memtomem" / "stm_feedback.db"
        path.parent.mkdir(parents=True)
        _seed(path).close()
        db = sqlite3.connect(str(path))
        db.execute("ALTER TABLE surfacing_events DROP COLUMN holdout_rate")
        db.execute("ALTER TABLE surfacing_events DROP COLUMN arm")
        db.commit()
        db.close()
        result = CliRunner().invoke(cli, ["stats"])
        assert result.exit_code == 0, result.output
        assert "surfaced events: 4" in result.output
        assert "withheld" not in result.output

    def test_mms_stats_shows_withheld_and_opportunities(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_home(monkeypatch, tmp_path)
        path = tmp_path / ".memtomem" / "stm_feedback.db"
        path.parent.mkdir(parents=True)
        store = _seed(path)
        from memtomem_stm.surfacing.feedback_store import OpportunityRow

        for i, decision in enumerate(["held_out", "surfaced", "surfaced"]):
            store.record_opportunity(
                OpportunityRow(
                    id=f"o{i}",
                    server="builtin",
                    tool="read_file",
                    arg_shape_json="{}",
                    response_len=10,
                    gate_decision=decision,
                )
            )
        store.close()
        result = CliRunner().invoke(cli, ["stats"])
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert "  surfaced events: 2  (distinct tools: 1)" in lines
        assert "  withheld (holdout): 2  (not counted as surfaced)" in lines
        assert "  opportunities: 3" in lines
        assert "    by decision: surfaced 2, held_out 1" in lines
        payload = json.loads(CliRunner().invoke(cli, ["stats", "--json"]).output)
        assert payload["surfacing"]["withheld_total"] == 2
        assert payload["surfacing"]["opportunities_total"] == 3

    async def test_stm_surfacing_stats_lines(self) -> None:
        tracker = MagicMock()
        tracker.get_stats.return_value = {
            "events_total": 1,
            "withheld_total": 2,
            "distinct_tools": 1,
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
        tracker.store.get_per_tool_feedback_counts.return_value = {}
        obs = SurfacingObservability()
        obs.record_outcome("t", "held_out")
        obs.record_holdout_unrecorded("withheld")
        obs.record_holdout_unrecorded("shown")
        obs.record_holdout_unrecorded("shown")
        engine = SurfacingEngine(config=_config(), mcp_adapter=AsyncMock(), observability=obs)
        app = SimpleNamespace(feedback_tracker=tracker, surfacing_engine=engine)
        ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))
        lines = (await stm_surfacing_stats(ctx=ctx)).splitlines()
        assert "Withheld:        2 (holdout, not counted above)" in lines
        assert "Holdout unrecorded (this process): shown 2, withheld 1" in lines
        filtered = (await stm_surfacing_stats(tool="t", ctx=ctx)).splitlines()
        assert not any(line.startswith("Holdout unrecorded") for line in filtered)


# ── migration ──────────────────────────────────────────────────────────


class TestMigration:
    def test_adds_both_columns_to_both_tables(self, tmp_path: Path) -> None:
        path = tmp_path / "f.db"
        store = _seed(path)
        store.close()
        db = sqlite3.connect(str(path))
        for table in ("surfacing_events", "surfacing_opportunities"):
            db.execute(f"ALTER TABLE {table} DROP COLUMN holdout_rate")
            db.execute(f"ALTER TABLE {table} DROP COLUMN arm")
        db.commit()
        db.close()
        for _ in range(2):  # idempotent
            store = FeedbackStore(path)
            store.initialize()
            store.close()
        for table in ("surfacing_events", "surfacing_opportunities"):
            cols = {r[1]: r[2] for r in sqlite3.connect(str(path)).execute(f"PRAGMA table_info('{table}')")}
            assert (cols["arm"], cols["holdout_rate"]) == ("TEXT", "REAL")
        assert {r["arm"] for r in _events(path)} == {None}
