"""Tests for the selection-telemetry JSONL sink (#467).

Covers the acceptance criteria: stable versioned schema (exact key sets
pinned per event type), structural redaction (no raw argument text ever
reaches disk) plus the sensitive-line backstop, sampling and rotation
config, private file modes (#458), and the ProxyManager wire-in with
counter snapshots.
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.selection_log import (
    RANKER_VERSION,
    SCHEMA_VERSION,
    SelectionTelemetryLog,
    _canonical_args,
    aggregate_selection_log,
)

_skip_on_windows = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX 0o700/0o600 modes are unenforceable on Windows; chmod only toggles read-only",
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_log(tmp_path: Path, **kwargs) -> SelectionTelemetryLog:
    log = SelectionTelemetryLog(tmp_path / "sel" / "log.jsonl", **kwargs)
    log.initialize()
    return log


def _read_events(log: SelectionTelemetryLog) -> list[dict]:
    if not log.path.exists():
        return []
    return [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines() if line]


def _log_pair(log: SelectionTelemetryLog) -> str | None:
    sid = log.log_selection(
        server="srv",
        selected_tool="test__tool",
        candidate_tools=["test__tool", "test__other"],
        arguments={"q": "hello"},
        trace_id="t" * 16,
    )
    if sid is not None:
        log.log_execution(
            selection_id=sid,
            trace_id="t" * 16,
            server="srv",
            selected_tool="test__tool",
            ok=True,
            latency_ms=12.345,
        )
    return sid


# ── Schema stability ─────────────────────────────────────────────────────


SELECTION_KEYS = {
    "schema_version",
    "ranker_version",
    "event",
    "ts",
    "selection_id",
    "trace_id",
    "server",
    "selected_tool",
    "candidate_tools",
    "candidate_count",
    "reject_reasons",
    "candidate_features",
    "graph_generation",
    "args_sha256",
    "args_chars",
}

EXECUTION_KEYS = {
    "schema_version",
    "ranker_version",
    "event",
    "ts",
    "selection_id",
    "trace_id",
    "server",
    "selected_tool",
    "ok",
    "latency_ms",
    "error_type",
    "retry_count",
    "cost",
    "cache_hit",
}

FEEDBACK_KEYS = {
    "schema_version",
    "ranker_version",
    "event",
    "ts",
    "selection_id",
    "trace_id",
    "user_corrected",
    "operator_override",
}


class TestSchemaStability:
    def test_selection_and_execution_key_sets_pinned(self, tmp_path):
        """Replayability contract: the exact v1 key set per event type.

        Adding/removing/renaming a field must fail here and force a
        ``SCHEMA_VERSION`` bump decision, not silently change the log.
        """
        log = _make_log(tmp_path)
        sid = _log_pair(log)
        log.log_feedback(selection_id=sid, user_corrected=True)

        selection, execution, feedback = _read_events(log)
        assert set(selection) == SELECTION_KEYS
        assert set(execution) == EXECUTION_KEYS
        assert set(feedback) == FEEDBACK_KEYS
        for record in (selection, execution, feedback):
            assert record["schema_version"] == SCHEMA_VERSION == 1
            assert record["ranker_version"] == RANKER_VERSION

    def test_pair_shares_selection_id_and_trace_id(self, tmp_path):
        log = _make_log(tmp_path)
        sid = _log_pair(log)

        selection, execution = _read_events(log)
        assert selection["event"] == "selection"
        assert execution["event"] == "execution"
        assert selection["selection_id"] == execution["selection_id"] == sid
        assert selection["trace_id"] == execution["trace_id"] == "t" * 16

    def test_v0_reserved_fields(self, tmp_path):
        """Reserved-until-later fields are present and null/empty in v0 so
        the schema doesn't change shape when later work populates them."""
        log = _make_log(tmp_path)
        _log_pair(log)

        selection, execution = _read_events(log)
        assert selection["reject_reasons"] == {}
        assert selection["candidate_features"] is None
        assert selection["graph_generation"] is None
        assert execution["retry_count"] is None
        assert execution["cost"] is None

    def test_cache_hit_is_per_call_populatable(self, tmp_path):
        """The cache_hit seam is populated, not added — passing it sets the
        field without changing the top-level key set, and omitting it keeps
        the unattributable ``None`` default."""
        log = _make_log(tmp_path)
        for value in (True, False, None):
            log.log_execution(
                selection_id="s",
                trace_id=None,
                server="srv",
                selected_tool="test__tool",
                ok=True,
                latency_ms=1.0,
                cache_hit=value,
            )
        records = _read_events(log)
        assert all(set(r) == EXECUTION_KEYS for r in records)
        assert [r["cache_hit"] for r in records] == [True, False, None]

    def test_reject_reasons_are_per_call_populatable(self, tmp_path):
        """#465 wires the hard filter's verdict into the reserved seam: the
        top-level key set must NOT change — reject_reasons is populated,
        not added — and omitting it keeps the v0 empty-map default."""
        log = _make_log(tmp_path)
        rejects = {"test__hidden": "config_hidden", "test__flaky": "unhealthy"}
        log.log_selection(
            server="srv",
            selected_tool="test__tool",
            candidate_tools=["test__tool"],
            arguments={},
            trace_id=None,
            reject_reasons=rejects,
        )
        (selection,) = _read_events(log)
        assert set(selection) == SELECTION_KEYS
        assert selection["reject_reasons"] == rejects

    def test_lines_are_sorted_key_compact_json(self, tmp_path):
        log = _make_log(tmp_path)
        _log_pair(log)
        first = log.path.read_text(encoding="utf-8").splitlines()[0]
        assert first == json.dumps(
            json.loads(first), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )

    def test_ranker_fields_are_per_call_overridable(self, tmp_path):
        """#466 wires real values into the reserved seams: the top-level
        key sets must NOT change — candidate_features is populated, not
        added, and ranker_version is stamped per record on both halves."""
        log = _make_log(tmp_path)
        feats = {
            "query_source": "args",
            "query_sha256": "0" * 64,
            "query_chars": 4,
            "ranked_candidates": [],
        }
        sid = log.log_selection(
            server="srv",
            selected_tool="test__tool",
            candidate_tools=["test__tool"],
            arguments={},
            trace_id=None,
            candidate_features=feats,
            ranker_version="v1-bm25-tool-relevance",
        )
        log.log_execution(
            selection_id=sid,
            trace_id=None,
            server="srv",
            selected_tool="test__tool",
            ok=True,
            latency_ms=1.0,
            ranker_version="v1-bm25-tool-relevance",
        )
        selection, execution = _read_events(log)
        assert set(selection) == SELECTION_KEYS
        assert set(execution) == EXECUTION_KEYS
        assert selection["candidate_features"] == feats
        assert selection["ranker_version"] == "v1-bm25-tool-relevance"
        assert execution["ranker_version"] == "v1-bm25-tool-relevance"


# ── Redaction ────────────────────────────────────────────────────────────


class TestRedaction:
    def test_raw_arguments_never_reach_disk(self, tmp_path):
        """Arguments appear only as sha256 + char count — a secret-bearing
        payload must not be findable anywhere in the log file."""
        log = _make_log(tmp_path)
        secret_args = {
            "api_key": "sk-" + "a" * 24,
            "query": "the password: hunter2 must stay out",
            "nested": {"token": "ghp_" + "b" * 36},
        }
        sid = log.log_selection(
            server="srv",
            selected_tool="test__tool",
            candidate_tools=["test__tool"],
            arguments=secret_args,
            trace_id=None,
        )
        assert sid is not None

        raw = log.path.read_text(encoding="utf-8")
        assert "hunter2" not in raw
        assert "sk-" + "a" * 24 not in raw
        assert "ghp_" not in raw

        (selection,) = _read_events(log)
        canonical = _canonical_args(secret_args)
        assert selection["args_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
        assert selection["args_chars"] == len(canonical)
        assert log.events_written == 1
        assert log.redaction_drops == 0

    def test_args_hash_is_order_independent(self, tmp_path):
        assert _canonical_args({"a": 1, "b": 2}) == _canonical_args({"b": 2, "a": 1})
        assert _canonical_args(None) == _canonical_args({}) == "{}"

    def test_sensitive_looking_field_drops_whole_record(self, tmp_path):
        """Backstop (#460/#462 never-persist rule): a secret smuggled through
        a field nobody expected — here an upstream tool *name* — drops the
        record and counts it instead of persisting."""
        log = _make_log(tmp_path)
        sid = log.log_selection(
            server="srv",
            selected_tool="test__ghp_" + "c" * 36,
            candidate_tools=[],
            arguments={},
            trace_id=None,
        )
        # The selection_id is still minted (caller pairing unaffected) but
        # nothing was persisted.
        assert sid is not None
        assert _read_events(log) == []
        assert log.redaction_drops == 1
        assert log.events_written == 0


# ── Sampling ─────────────────────────────────────────────────────────────


class TestSampling:
    def test_rate_zero_samples_everything_out(self, tmp_path):
        log = _make_log(tmp_path, sample_rate=0.0)
        assert _log_pair(log) is None
        assert _read_events(log) == []
        assert log.events_sampled_out == 1
        assert log.events_written == 0

    def test_rate_one_keeps_everything(self, tmp_path):
        log = _make_log(tmp_path, sample_rate=1.0)
        assert _log_pair(log) is not None
        assert len(_read_events(log)) == 2
        assert log.events_sampled_out == 0

    def test_fractional_rate_is_deterministic_with_seeded_rng(self, tmp_path):
        log = _make_log(tmp_path, sample_rate=0.5, rng=random.Random(42))
        decisions = [_log_pair(log) is not None for _ in range(20)]
        ref = random.Random(42)
        expected = [ref.random() < 0.5 for _ in range(20)]
        # Sampled-out pairs consume no rng draw beyond their own; the
        # sequence of keep/drop decisions must follow the injected rng.
        assert decisions == expected
        assert log.events_sampled_out == decisions.count(False)


# ── Rotation ─────────────────────────────────────────────────────────────


class TestRotation:
    def test_rotates_at_max_bytes_and_bounds_backups(self, tmp_path):
        log = _make_log(tmp_path, max_bytes=1, max_backups=2)
        # Every append rotates first (file already ≥ 1 byte), so N pairs
        # produce a deep shift chain; only .1 and .2 may survive.
        for _ in range(4):
            _log_pair(log)

        base = log.path
        assert base.exists()
        assert base.with_name(base.name + ".1").exists()
        assert base.with_name(base.name + ".2").exists()
        assert not base.with_name(base.name + ".3").exists()
        # Nothing was lost to errors — every event landed in some file.
        assert log.write_errors == 0
        assert log.events_written == 8

    def test_zero_backups_truncates(self, tmp_path):
        log = _make_log(tmp_path, max_bytes=1, max_backups=0)
        for _ in range(3):
            _log_pair(log)
        assert not log.path.with_name(log.path.name + ".1").exists()
        # Each append truncated then wrote: exactly one event remains.
        assert len(_read_events(log)) == 1

    def test_no_rotation_below_threshold(self, tmp_path):
        log = _make_log(tmp_path, max_bytes=50_000_000)
        for _ in range(3):
            _log_pair(log)
        assert len(_read_events(log)) == 6
        assert not log.path.with_name(log.path.name + ".1").exists()


# ── File modes / failure isolation ───────────────────────────────────────


class TestFilesystemPosture:
    @_skip_on_windows
    def test_log_file_created_0600_with_0700_parent(self, tmp_path):
        log = _make_log(tmp_path)
        assert log.path.stat().st_mode & 0o777 == 0o600
        assert log.path.parent.stat().st_mode & 0o777 == 0o700

    @_skip_on_windows
    def test_initialize_tightens_preexisting_mode(self, tmp_path):
        path = tmp_path / "log.jsonl"
        path.touch()
        path.chmod(0o644)
        log = SelectionTelemetryLog(path)
        log.initialize()
        assert path.stat().st_mode & 0o777 == 0o600

    @_skip_on_windows
    def test_rotated_backup_stays_0600(self, tmp_path):
        log = _make_log(tmp_path, max_bytes=1, max_backups=1)
        _log_pair(log)
        backup = log.path.with_name(log.path.name + ".1")
        assert backup.exists()
        assert backup.stat().st_mode & 0o777 == 0o600

    def test_write_failure_is_swallowed_and_counted(self, tmp_path):
        target = tmp_path / "dir-not-file"
        target.mkdir()
        log = SelectionTelemetryLog(target)  # appending to a directory fails
        sid = log.log_selection(
            server="srv",
            selected_tool="test__tool",
            candidate_tools=[],
            arguments={},
            trace_id=None,
        )
        # Nothing reached disk, so the caller must skip the paired
        # execution event — write failures never produce orphan halves.
        assert sid is None
        assert log.write_errors == 1
        assert log.events_written == 0


# ── ProxyManager wire-in ─────────────────────────────────────────────────


def _text_content(text: str):
    return SimpleNamespace(type="text", text=text)


def _make_result(text: str):
    return SimpleNamespace(content=[_text_content(text)], isError=False)


def _make_manager(
    tmp_path: Path, *, cache=None, **log_kwargs
) -> tuple[ProxyManager, SelectionTelemetryLog]:
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=CompressionStrategy.NONE,
        max_retries=0,
        reconnect_delay_seconds=0.0,
    )
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": server_cfg},
        min_result_retention=0.0,
    )
    log = _make_log(tmp_path, **log_kwargs)
    mgr = ProxyManager(proxy_cfg, TokenTracker(), cache=cache, selection_log=log)

    session = AsyncMock()
    tool = SimpleNamespace(name="tool", description="a tool", inputSchema={"type": "object"})
    conn = UpstreamConnection(name="srv", config=server_cfg, session=session, tools=[tool])
    mgr._connections["srv"] = conn
    return mgr, log


class TestManagerWireIn:
    async def test_success_call_writes_paired_events_with_counters(self, tmp_path):
        mgr, log = _make_manager(tmp_path)
        # Advertisement snapshot: candidate_tools comes from the last
        # get_proxy_tools() result, in prefixed-name vocabulary.
        infos = mgr.get_proxy_tools()
        assert [i.prefixed_name for i in infos] == ["test__tool"]

        mgr._connections["srv"].session.call_tool.return_value = _make_result("ok!")
        result = await mgr.call_tool("srv", "tool", {"q": "x"}, trace_id="deadbeef00000000")
        assert result == "ok!"

        selection, execution = _read_events(log)
        assert selection["candidate_tools"] == ["test__tool"]
        assert selection["selected_tool"] == "test__tool"
        assert selection["server"] == "srv"
        assert execution["ok"] is True
        assert execution["error_type"] is None
        assert execution["latency_ms"] >= 0
        assert execution["selection_id"] == selection["selection_id"]
        assert selection["trace_id"] == execution["trace_id"] == "deadbeef00000000"
        # Counter snapshot (wire-in contract): exactly one pair, no drops.
        assert log.events_written == 2
        assert log.events_sampled_out == 0
        assert log.write_errors == 0

    async def test_upstream_error_writes_ok_false_execution(self, tmp_path):
        mgr, log = _make_manager(tmp_path)
        mgr.get_proxy_tools()
        mgr._connections["srv"].session.call_tool.side_effect = RuntimeError("boom")

        with pytest.raises(Exception):
            await mgr.call_tool("srv", "tool", {})

        events = _read_events(log)
        assert [e["event"] for e in events] == ["selection", "execution"]
        execution = events[1]
        assert execution["ok"] is False
        assert isinstance(execution["error_type"], str) and execution["error_type"]
        assert log.events_written == 2

    async def test_sampled_out_call_emits_nothing(self, tmp_path):
        mgr, log = _make_manager(tmp_path, sample_rate=0.0)
        mgr.get_proxy_tools()
        mgr._connections["srv"].session.call_tool.return_value = _make_result("ok!")

        result = await mgr.call_tool("srv", "tool", {})
        assert result == "ok!"
        assert _read_events(log) == []
        assert log.events_sampled_out == 1
        assert log.events_written == 0

    async def test_selection_write_failure_skips_execution_event(self, tmp_path):
        """A failed selection write must not leave an orphan execution
        record: the pair is skipped atomically. ``write_errors == 2``
        would mean the execution write was still attempted."""
        server_cfg = UpstreamServerConfig(
            prefix="test", compression=CompressionStrategy.NONE, max_retries=0
        )
        proxy_cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={"srv": server_cfg},
        )
        broken_dir = tmp_path / "log-as-dir"
        broken_dir.mkdir()
        log = SelectionTelemetryLog(broken_dir)  # every append fails
        mgr = ProxyManager(proxy_cfg, TokenTracker(), selection_log=log)
        session = AsyncMock()
        session.call_tool.return_value = _make_result("ok!")
        mgr._connections["srv"] = UpstreamConnection(
            name="srv", config=server_cfg, session=session, tools=[]
        )

        assert await mgr.call_tool("srv", "tool", {}) == "ok!"
        assert log.write_errors == 1
        assert log.events_written == 0

    async def test_no_selection_log_is_default_noop(self, tmp_path):
        """Default construction (no selection_log kwarg) keeps the call
        path telemetry-free — the existing-manager-tests invariant."""
        server_cfg = UpstreamServerConfig(
            prefix="test", compression=CompressionStrategy.NONE, max_retries=0
        )
        proxy_cfg = ProxyConfig(
            config_path=tmp_path / "proxy.json",
            upstream_servers={"srv": server_cfg},
        )
        mgr = ProxyManager(proxy_cfg, TokenTracker())
        session = AsyncMock()
        session.call_tool.return_value = _make_result("ok!")
        mgr._connections["srv"] = UpstreamConnection(
            name="srv", config=server_cfg, session=session, tools=[]
        )
        assert await mgr.call_tool("srv", "tool", {}) == "ok!"

    async def test_cache_hit_field_true_on_hit_false_on_live(self, tmp_path):
        """The execution event records whether the result came from the
        response cache: ``True`` on a fast-path hit, ``False`` on a live
        upstream call. Wires through ``_call_tool_guarded``'s
        ``(result, cache_hit)`` return (the #467 cache_hit field)."""
        from memtomem_stm.proxy.cache import ProxyCache

        cache = ProxyCache(tmp_path / "cache.db")
        cache.initialize()
        mgr, log = _make_manager(tmp_path, cache=cache)
        # Seed AFTER building the manager: the lookup key includes the
        # compression-settings fingerprint the manager derives per tool.
        cache.set(
            "srv",
            "tool",
            {"q": "hit"},
            "cached!",
            ttl_seconds=300.0,
            config_fingerprint=mgr._cache_key_fingerprint("srv", "tool", cfg_snap=mgr._config),
        )
        mgr.get_proxy_tools()
        mgr._connections["srv"].session.call_tool.return_value = _make_result("live!")

        # Pre-cached args → fast-path hit (no upstream call).
        assert await mgr.call_tool("srv", "tool", {"q": "hit"}) == "cached!"
        # Un-cached args → live upstream call.
        assert await mgr.call_tool("srv", "tool", {"q": "miss"}) == "live!"

        executions = [e for e in _read_events(log) if e["event"] == "execution"]
        assert len(executions) == 2
        # Events append in call order: first call (hit) then second (live).
        assert executions[0]["cache_hit"] is True
        assert executions[1]["cache_hit"] is False

    async def test_pipeline_error_leaves_cache_hit_null(self, tmp_path):
        """A raise escaping the guarded call means the hit/miss was never
        attributed — the execution event stays ``cache_hit=None`` rather
        than guessing."""
        mgr, log = _make_manager(tmp_path)
        mgr.get_proxy_tools()
        mgr._connections["srv"].session.call_tool.side_effect = RuntimeError("boom")

        with pytest.raises(Exception):
            await mgr.call_tool("srv", "tool", {})

        executions = [e for e in _read_events(log) if e["event"] == "execution"]
        assert len(executions) == 1
        assert executions[0]["ok"] is False
        assert executions[0]["cache_hit"] is None


# ── snapshot (live write-path counters) ──────────────────────────────────


class TestSnapshot:
    def test_snapshot_keys_and_initial_zeros(self, tmp_path):
        log = _make_log(tmp_path)
        snap = log.snapshot()
        assert snap == {
            "events_written": 0,
            "events_sampled_out": 0,
            "redaction_drops": 0,
            "write_errors": 0,
        }

    def test_snapshot_reflects_writes(self, tmp_path):
        log = _make_log(tmp_path)
        _log_pair(log)  # one selection + one execution
        assert log.snapshot()["events_written"] == 2

    def test_snapshot_reflects_sampling(self, tmp_path):
        log = _make_log(tmp_path, sample_rate=0.0)
        assert _log_pair(log) is None  # sampled out, no execution
        snap = log.snapshot()
        assert snap["events_sampled_out"] == 1
        assert snap["events_written"] == 0

    def test_snapshot_reflects_redaction_drop(self, tmp_path):
        log = _make_log(tmp_path)
        # A credential-looking selected_tool trips the storage backstop.
        sid = log.log_selection(
            server="srv",
            selected_tool="sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd",
            candidate_tools=["x"],
            arguments={},
            trace_id=None,
        )
        # Drop still returns an id (documented left-outer), but nothing
        # reached disk and the counter recorded the drop.
        assert sid is not None
        assert log.snapshot()["redaction_drops"] == 1
        assert log.snapshot()["events_written"] == 0


# ── aggregate_selection_log (read-side stats) ────────────────────────────


def _write_lines(path: Path, lines: list) -> None:
    """Write raw JSONL; dict entries are dumped, str entries written verbatim
    (so malformed/non-object lines can be exercised)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for item in lines:
            fh.write((item if isinstance(item, str) else json.dumps(item)) + "\n")


def _sel(**kw) -> dict:
    base = {
        "event": "selection",
        "ranker_version": RANKER_VERSION,
        "server": "srv",
        "selected_tool": "srv__a",
        "reject_reasons": {},
    }
    base.update(kw)
    return base


def _exec(**kw) -> dict:
    base = {"event": "execution", "ok": True, "latency_ms": 10.0, "error_type": None}
    base.update(kw)
    return base


class TestAggregateSelectionLog:
    def test_absent_file_returns_zeroed_shape(self, tmp_path):
        agg = aggregate_selection_log(tmp_path / "nope.jsonl")
        assert agg["exists"] is False
        assert agg["total_lines"] == 0
        assert agg["events"] == {"selection": 0, "execution": 0, "feedback": 0}
        assert agg["by_ranker_version"] == []
        assert agg["outcomes"] == {"ok": 0, "error": 0, "error_rate": 0.0}

    def test_empty_file_exists_but_no_lines(self, tmp_path):
        p = tmp_path / "log.jsonl"
        p.write_text("", encoding="utf-8")
        agg = aggregate_selection_log(p)
        assert agg["exists"] is True
        assert agg["total_lines"] == 0

    def test_malformed_and_unknown_lines_skipped_and_counted(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_lines(
            p,
            [
                _sel(),
                "{ this is not json",  # bad JSON
                "[1, 2, 3]",  # valid JSON, not an object
                {"event": "mystery"},  # unknown event type
                "",  # blank line ignored, not counted as malformed
            ],
        )
        agg = aggregate_selection_log(p)
        # blank line skipped before counting; 4 real lines counted
        assert agg["total_lines"] == 4
        assert agg["malformed"] == 3
        assert agg["events"]["selection"] == 1

    def test_event_counts_and_ranker_cohorts(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_lines(
            p,
            [
                _sel(ranker_version="v0-passthrough"),
                _sel(ranker_version="v1-bm25-tool-relevance"),
                _sel(ranker_version="v1-bm25-tool-relevance"),
                _exec(),
                {"event": "feedback", "selection_id": "z"},
            ],
        )
        agg = aggregate_selection_log(p)
        assert agg["events"] == {"selection": 3, "execution": 1, "feedback": 1}
        # full, sorted by count desc then key asc
        assert agg["by_ranker_version"] == [
            ["v1-bm25-tool-relevance", 2],
            ["v0-passthrough", 1],
        ]

    def test_by_server_and_tool_with_topn_truncation(self, tmp_path):
        p = tmp_path / "log.jsonl"
        rows = [_sel(selected_tool=f"srv__t{i}") for i in range(5)]
        # make t0 the most frequent
        rows += [_sel(selected_tool="srv__t0") for _ in range(3)]
        _write_lines(p, rows)
        agg = aggregate_selection_log(p, top_n=2)
        assert agg["by_selected_tool_distinct"] == 5
        assert len(agg["by_selected_tool"]) == 2
        assert agg["by_selected_tool"][0] == ["srv__t0", 4]
        # server is the same for all → single distinct
        assert agg["by_server_distinct"] == 1
        assert agg["by_server"] == [["srv", 8]]

    def test_outcomes_error_rate_and_error_types(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_lines(
            p,
            [
                _exec(ok=True),
                _exec(ok=False, error_type="TimeoutError"),
                _exec(ok=False, error_type="TimeoutError"),
                _exec(ok=False, error_type="ValueError"),
            ],
        )
        agg = aggregate_selection_log(p)
        assert agg["outcomes"] == {"ok": 1, "error": 3, "error_rate": 0.75}
        assert agg["by_error_type"] == [["TimeoutError", 2], ["ValueError", 1]]
        assert agg["by_error_type_distinct"] == 2

    def test_reject_reasons_tallied_across_selections(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_lines(
            p,
            [
                _sel(reject_reasons={"srv__b": "config_hidden", "srv__c": "unhealthy"}),
                _sel(reject_reasons={"srv__d": "config_hidden"}),
                _sel(reject_reasons={}),
            ],
        )
        agg = aggregate_selection_log(p)
        assert agg["reject_reasons"] == [["config_hidden", 2], ["unhealthy", 1]]
        assert agg["reject_reasons_distinct"] == 2

    def test_latency_percentiles_and_bool_excluded(self, tmp_path):
        p = tmp_path / "log.jsonl"
        rows = [_exec(latency_ms=float(v)) for v in (10, 20, 30, 40, 50)]
        # a malformed/bool latency must not poison the percentile pool
        rows.append(_exec(latency_ms=True))
        _write_lines(p, rows)
        agg = aggregate_selection_log(p)
        assert agg["latency_ms"]["count"] == 5
        assert agg["latency_ms"]["p50"] == 30.0

    def test_rotated_backups_counted_not_parsed(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_lines(p, [_sel()])
        (tmp_path / "log.jsonl.1").write_text("ignored\n", encoding="utf-8")
        (tmp_path / "log.jsonl.2").write_text("ignored\n", encoding="utf-8")
        agg = aggregate_selection_log(p)
        assert agg["rotated_backups"] == 2
        # only the active file's one selection is in the aggregate
        assert agg["events"]["selection"] == 1

    def test_cache_hit_miss_unknown_tally_and_hit_rate(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_lines(
            p,
            [
                _exec(cache_hit=True),
                _exec(cache_hit=True),
                _exec(cache_hit=True),
                _exec(cache_hit=False),
                _exec(cache_hit=None),  # in-pipeline raise: unattributable
                _exec(),  # missing field → unknown
            ],
        )
        agg = aggregate_selection_log(p)
        # hit_rate denominator excludes the 2 unknowns: 3 / (3 + 1) = 0.75
        assert agg["cache"] == {"hit": 3, "miss": 1, "unknown": 2, "hit_rate": 0.75}

    def test_cache_zeroed_when_no_executions(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_lines(p, [_sel()])
        agg = aggregate_selection_log(p)
        assert agg["cache"] == {"hit": 0, "miss": 0, "unknown": 0, "hit_rate": 0.0}
