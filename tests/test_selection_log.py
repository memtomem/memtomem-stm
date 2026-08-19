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
import os
import random
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.cli import selection_cmd
from memtomem_stm.proxy import selection_log as selection_log_module
from memtomem_stm.proxy.selection_log import (
    RANKER_VERSION,
    discover_log_files,
    find_selection,
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
    return SimpleNamespace(content=[_text_content(text)], is_error=False)


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
    tool = SimpleNamespace(name="tool", description="a tool", input_schema={"type": "object"})
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
    # ``schema_version`` is part of every record the writer emits, and
    # ``find_selection`` refuses to label one without a supported value — a
    # fixture missing it would be a shape production never produces.
    base = {
        "event": "selection",
        "schema_version": SCHEMA_VERSION,
        "ranker_version": RANKER_VERSION,
        "server": "srv",
        "selected_tool": "srv__a",
        "reject_reasons": {},
    }
    base.update(kw)
    return base


def _exec(**kw) -> dict:
    base = {
        "event": "execution",
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "latency_ms": 10.0,
        "error_type": None,
    }
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


# ── find_selection / discover_log_files (#469 labelling) ───────────────────


    def test_a_directory_it_cannot_list_costs_the_backup_count_not_the_stats(self, tmp_path):
        """The aggregate's own contract: it summarizes what it could read.

        Counting rotated backups lists the directory, and that listing can fail
        on its own — a mode change, an ACL lost on a mounted volume. It is a
        decoration on the summary, so it degrades to zero; raising would take
        down ``stm_selection_stats``, whose caller has no way to ask for less.
        """
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])

        def refuse(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        with mock.patch.object(selection_log_module, "discover_log_files", refuse):
            out = aggregate_selection_log(log_path)
        assert out["rotated_backups"] == 0
        assert out["rotated_backups_unknown"] is True, (
            "'I could not look' must not be reported as 'there is no history'"
        )
        assert out["total_lines"] >= 1, "the records it CAN read are still reported"
        assert out["events"]["selection"] >= 1

        # And the rendered view says so, rather than leaving the reader to
        # infer that the active file is the whole log.
        from memtomem_stm.server import _format_selection_stats_sections

        live = dict.fromkeys(
            ("events_written", "events_sampled_out", "redaction_drops", "write_errors"), 0
        )
        rendered = "\n".join(_format_selection_stats_sections(out, live))
        assert "could not be counted" in rendered

    def test_a_listable_directory_reports_the_count_as_known(self, tmp_path):
        """The positive control: the flag is off when the listing worked, so
        the assertion above is about the failure and not about the key
        existing."""
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        out = aggregate_selection_log(log_path)
        assert out["rotated_backups_unknown"] is False


class TestDiscoverLogFiles:
    def test_orders_backups_oldest_first_then_active(self, tmp_path):
        p = tmp_path / "log.jsonl"
        for name in ("log.jsonl", "log.jsonl.1", "log.jsonl.2"):
            (tmp_path / name).write_text("", encoding="utf-8")
        assert [f.name for f in discover_log_files(p)] == [
            "log.jsonl.2",
            "log.jsonl.1",
            "log.jsonl",
        ]

    def test_ignores_non_numeric_siblings(self, tmp_path):
        # The numeric suffix space belongs to rotation; an operator's backup
        # copy is not part of the log and must not be labelled through.
        p = tmp_path / "log.jsonl"
        p.write_text("", encoding="utf-8")
        (tmp_path / "log.jsonl.bak").write_text("", encoding="utf-8")
        (tmp_path / "log.jsonl.1").write_text("", encoding="utf-8")
        assert [f.name for f in discover_log_files(p)] == ["log.jsonl.1", "log.jsonl"]

    def test_active_only_excludes_backups(self, tmp_path):
        p = tmp_path / "log.jsonl"
        p.write_text("", encoding="utf-8")
        (tmp_path / "log.jsonl.1").write_text("", encoding="utf-8")
        assert discover_log_files(p, include_rotated=False) == [p]

    def test_absent_active_file_is_not_returned(self, tmp_path):
        p = tmp_path / "log.jsonl"
        (tmp_path / "log.jsonl.1").write_text("", encoding="utf-8")
        assert [f.name for f in discover_log_files(p)] == ["log.jsonl.1"]
        assert discover_log_files(p, include_rotated=False) == []


class TestFindSelection:
    def test_by_id_finds_an_exact_record(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_lines(p, [_sel(selection_id="a"), _sel(selection_id="b")])
        assert find_selection(p, selection_id="b")["selection_id"] == "b"

    def test_by_id_misses_return_none(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_lines(p, [_sel(selection_id="a")])
        assert find_selection(p, selection_id="zz") is None

    def test_last_match_wins_in_append_order(self, tmp_path):
        """Append order, not ``ts``: wall clock can step backwards.

        The records below carry a ``ts`` that decreases down the file, so a
        ts-ordered implementation would pick the FIRST one and label the wrong
        selection.
        """
        p = tmp_path / "log.jsonl"
        _write_lines(
            p,
            [
                _sel(selection_id="old", ts=500.0),
                _sel(selection_id="new", ts=100.0),
            ],
        )
        assert find_selection(p)["selection_id"] == "new"

    def test_filters_by_server_and_tool(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_lines(
            p,
            [
                _sel(selection_id="a", server="gh", selected_tool="gh__create"),
                _sel(selection_id="b", server="gh", selected_tool="gh__list"),
                _sel(selection_id="c", server="fs", selected_tool="fs__read"),
            ],
        )
        assert find_selection(p, tool="gh__create")["selection_id"] == "a"
        assert find_selection(p, server="gh")["selection_id"] == "b"
        assert find_selection(p, server="fs")["selection_id"] == "c"
        assert find_selection(p, server="gh", tool="fs__read") is None

    def test_rotated_backups_are_searched_oldest_first(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_lines(tmp_path / "log.jsonl.1", [_sel(selection_id="rotated")])
        _write_lines(p, [_sel(selection_id="active")])
        assert find_selection(p)["selection_id"] == "active"
        assert find_selection(p, selection_id="rotated")["selection_id"] == "rotated"
        # --active-only narrows to the live file: the rotated row is then
        # unreachable rather than silently resolved.
        assert find_selection(p, selection_id="rotated", include_rotated=False) is None

    def test_skips_malformed_and_other_events(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_lines(
            p,
            [
                _sel(selection_id="good"),
                "{ not json",
                "[1,2,3]",
                _exec(selection_id="exec-not-a-selection"),
                {"event": "feedback", "selection_id": "fb"},
            ],
        )
        assert find_selection(p)["selection_id"] == "good"

    def test_absent_log_returns_none(self, tmp_path):
        assert find_selection(tmp_path / "nope.jsonl") is None


class TestFeedbackRecord:
    def test_ranker_version_mirrors_the_labelled_selection(self, tmp_path):
        """A label belongs to the cohort of the call it labels.

        Stamping the emitter's own baseline would file every label under
        ``v0-passthrough`` the moment replay splits feedback by this field —
        a claim about a ranker that never ran for that call.
        """
        log = SelectionTelemetryLog(tmp_path / "log.jsonl")
        log.initialize()
        log.log_feedback(selection_id="s1", ranker_version="v3-bm25-graph-risk-penalty")
        record = json.loads((tmp_path / "log.jsonl").read_text(encoding="utf-8").strip())
        assert record["ranker_version"] == "v3-bm25-graph-risk-penalty"

    def test_ranker_version_defaults_to_the_baseline(self, tmp_path):
        log = SelectionTelemetryLog(tmp_path / "log.jsonl")
        log.initialize()
        log.log_feedback(selection_id="s1", user_corrected=True)
        record = json.loads((tmp_path / "log.jsonl").read_text(encoding="utf-8").strip())
        assert record["ranker_version"] == RANKER_VERSION


# ── ``mms selection feedback`` (the log's one production emitter, #469) ────


def _seed_log(path: Path, *, rows: list[dict]) -> None:
    log = SelectionTelemetryLog(path)
    log.initialize()
    for row in rows:
        log.log_selection(
            server=row["server"],
            selected_tool=row["tool"],
            candidate_tools=[row["tool"]],
            arguments={"q": "x"},
            trace_id=row.get("trace_id"),
            ranker_version=row.get("ranker_version"),
        )


def _feedback_records(path: Path) -> list[dict]:
    return [
        record
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for record in [json.loads(line)]
        if record.get("event") == "feedback"
    ]


def _run_feedback(tmp_path: Path, log_path: Path, *args: str, yes: bool = True):
    """Invoke the command. ``--last`` is non-interactive here, and the command
    requires explicit consent for that (a formatting flag or a pipe must not
    authorize a write), so ``--yes`` is added by default; the tests that pin
    the consent contract itself pass ``yes=False``."""
    from memtomem_stm.cli.proxy import cli

    consent = ["--yes"] if yes and "--last" in args and "--yes" not in args else []
    return CliRunner().invoke(
        cli,
        [
            "selection",
            "feedback",
            "--log",
            str(log_path),
            "--config",
            str(tmp_path / "absent-proxy.json"),
            *args,
            *consent,
        ],
    )


class TestSelectionFeedbackCommand:
    def test_last_labels_the_most_recent_selection(self, tmp_path):
        log_path = tmp_path / "log.jsonl"
        _seed_log(
            log_path,
            rows=[
                {"server": "gh", "tool": "gh__a", "trace_id": "t1"},
                {"server": "gh", "tool": "gh__b", "trace_id": "t2"},
            ],
        )
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected")
        assert result.exit_code == 0, result.output
        records = _feedback_records(log_path)
        assert len(records) == 1
        assert records[0]["user_corrected"] is True
        assert records[0]["operator_override"] is None
        assert records[0]["trace_id"] == "t2"
        # The resolved selection is echoed, so a wrong guess is visible to the
        # person making the judgement instead of landing silently.
        assert "gh__b" in result.output

    def test_filters_narrow_which_selection_is_labelled(self, tmp_path):
        log_path = tmp_path / "log.jsonl"
        _seed_log(
            log_path,
            rows=[
                {"server": "gh", "tool": "gh__a"},
                {"server": "fs", "tool": "fs__read"},
            ],
        )
        result = _run_feedback(
            tmp_path, log_path, "--last", "--tool", "gh__a", "--operator-override"
        )
        assert result.exit_code == 0, result.output
        assert "gh__a" in result.output
        assert _feedback_records(log_path)[0]["operator_override"] is True

    def test_false_label_is_recorded_not_omitted(self, tmp_path):
        """``--no-user-corrected`` is a positive example, not a missing one."""
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        result = _run_feedback(tmp_path, log_path, "--last", "--no-user-corrected")
        assert result.exit_code == 0, result.output
        assert _feedback_records(log_path)[0]["user_corrected"] is False

    def test_label_inherits_the_selection_cohort(self, tmp_path):
        log_path = tmp_path / "log.jsonl"
        _seed_log(
            log_path,
            rows=[{"server": "gh", "tool": "gh__a", "ranker_version": "v1-bm25-tool-relevance"}],
        )
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected")
        assert result.exit_code == 0, result.output
        assert _feedback_records(log_path)[0]["ranker_version"] == "v1-bm25-tool-relevance"

    def test_unknown_id_writes_nothing(self, tmp_path):
        """Resolution precedes the write: a typo must not append a dead label.

        A feedback record joining no selection is indistinguishable from one
        whose selection the redaction screen dropped, so it would corrupt the
        very join it exists to feed.
        """
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        result = _run_feedback(
            tmp_path, log_path, "--selection-id", "deadbeef", "--user-corrected", "--json"
        )
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "not_found"
        assert _feedback_records(log_path) == []

    def test_no_match_for_filters_writes_nothing(self, tmp_path):
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        result = _run_feedback(
            tmp_path, log_path, "--last", "--server", "nope", "--user-corrected", "--json"
        )
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "no_match"
        assert _feedback_records(log_path) == []

    def test_absent_log_is_a_stable_error_code(self, tmp_path):
        result = _run_feedback(
            tmp_path, tmp_path / "nope.jsonl", "--last", "--user-corrected", "--json"
        )
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "no_log"

    def test_json_result_document(self, tmp_path):
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a", "trace_id": "t9"}])
        result = _run_feedback(
            tmp_path, log_path, "--last", "--user-corrected", "--no-operator-override", "--json"
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["action"] == "selection-feedback"
        assert payload["ok"] is True
        assert payload["server"] == "gh"
        assert payload["selected_tool"] == "gh__a"
        assert payload["trace_id"] == "t9"
        assert payload["user_corrected"] is True
        assert payload["operator_override"] is False
        assert payload["selection_id"] == _feedback_records(log_path)[0]["selection_id"]

    @pytest.mark.parametrize(
        "args",
        [
            ["--last", "--selection-id", "x", "--user-corrected"],
            ["--user-corrected"],
            ["--last"],
            ["--selection-id", "x", "--server", "gh", "--user-corrected"],
        ],
        ids=["both_selectors", "no_selector", "no_label", "filter_without_last"],
    )
    def test_usage_errors(self, tmp_path, args):
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        result = _run_feedback(tmp_path, log_path, *args)
        assert result.exit_code == 2, result.output
        assert _feedback_records(log_path) == []

    def test_labels_accumulate_for_one_selection(self, tmp_path):
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        assert _run_feedback(tmp_path, log_path, "--last", "--user-corrected").exit_code == 0
        assert _run_feedback(tmp_path, log_path, "--last", "--operator-override").exit_code == 0
        records = _feedback_records(log_path)
        assert len(records) == 2
        # Same selection, one field each — the reader folds them.
        assert records[0]["selection_id"] == records[1]["selection_id"]

    def test_active_only_cannot_reach_a_rotated_selection(self, tmp_path):
        log_path = tmp_path / "log.jsonl"
        _seed_log(tmp_path / "log.jsonl.1", rows=[{"server": "gh", "tool": "gh__rotated"}])
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__active"}])
        result = _run_feedback(
            tmp_path,
            log_path,
            "--last",
            "--tool",
            "gh__rotated",
            "--user-corrected",
            "--active-only",
            "--json",
        )
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "no_match"
        # Positive control: without --active-only the same row resolves, so the
        # miss above is the flag doing its job and not a broken filter.
        assert (
            _run_feedback(
                tmp_path, log_path, "--last", "--tool", "gh__rotated", "--user-corrected"
            ).exit_code
            == 0
        )


def _short_write_on(log_path: Path, *, first_only: bool = True, drop_only_newline: bool = False):
    """An ``os.write`` stand-in that shortens writes to the LOG file only.

    Narrowed by inode rather than patched blanket, because ``open_lock_fd``
    writes one byte to the lock sidecar on Windows: a global patch would
    shorten THAT instead, and the test would exercise lock failure while
    claiming to cover short writes.
    """
    real_write = os.write
    state = {"n": 0}
    log_inode = log_path.stat().st_ino

    def patched(fd, data):
        try:
            is_log = os.fstat(fd).st_ino == log_inode
        except OSError:  # pragma: no cover - defensive
            is_log = False
        if not is_log:
            return real_write(fd, data)
        state["n"] += 1
        if first_only and state["n"] != 1:
            return real_write(fd, data)
        return real_write(fd, data[:-1] if drop_only_newline else data[: len(data) // 2])

    return patched


class TestResolverAgreesWithReplay:
    """A selection the resolver sees and replay does not is one whose label
    joins nothing. Each case is paired with a positive control, since the
    layouts differ only by the property under test."""

    def test_an_oversized_line_is_skipped_by_both(self, tmp_path):
        from memtomem_stm.proxy.selection_log import MAX_LINE_BYTES

        log_path = tmp_path / "log.jsonl"
        padding = "x" * MAX_LINE_BYTES
        _write_lines(
            log_path,
            [_sel(selection_id="older"), _sel(selection_id="huge", selected_tool=padding)],
        )
        assert _run_feedback(tmp_path, log_path, "--last", "--user-corrected").exit_code == 0
        assert [record["selection_id"] for record in _feedback_records(log_path)] == ["older"]

        control = tmp_path / "control.jsonl"
        _write_lines(control, [_sel(selection_id="older"), _sel(selection_id="huge")])
        assert _run_feedback(tmp_path, control, "--last", "--user-corrected").exit_code == 0
        assert [record["selection_id"] for record in _feedback_records(control)] == ["huge"]

    def test_the_active_files_unterminated_tail_is_not_a_selection_yet(self, tmp_path):
        """A trailing fragment is a record still being written.

        Replay skips it for that reason; labelling it would name a selection
        the reader never loads.
        """
        log_path = tmp_path / "log.jsonl"
        _write_lines(log_path, [_sel(selection_id="older")])
        with log_path.open("ab") as fh:
            fh.write(json.dumps(_sel(selection_id="half")).encode("utf-8"))  # no newline

        assert _run_feedback(tmp_path, log_path, "--last", "--user-corrected").exit_code == 0
        labels = [record["selection_id"] for record in _feedback_records(log_path)]
        assert labels == ["older"]

        # Same bytes, terminated: now it IS a record, and it wins.
        control = tmp_path / "control.jsonl"
        _write_lines(control, [_sel(selection_id="older"), _sel(selection_id="half")])
        assert _run_feedback(tmp_path, control, "--last", "--user-corrected").exit_code == 0
        assert [record["selection_id"] for record in _feedback_records(control)] == ["half"]

    def test_a_rotated_backups_unterminated_tail_is_still_a_record(self, tmp_path):
        """Only the ACTIVE file has a tail that is still being written.

        A backup is closed history; a missing final newline there is a
        hand-edit, and dropping its last record would hide selections replay
        loads.
        """
        backup = tmp_path / "log.jsonl.1"
        _write_lines(backup, [_sel(selection_id="rotated")])
        backup.write_bytes(backup.read_bytes().rstrip(b"\n"))
        log_path = tmp_path / "log.jsonl"
        _write_lines(log_path, [])

        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["selection_id"] == "rotated"

    def test_a_label_after_a_crashed_tail_is_readable_on_its_own_line(self, tmp_path):
        """The append must not fuse itself onto an unterminated last line.

        Skipping that tail at resolution is only half the contract: if the
        write then lands ON it, the label is reported written while every
        reader rejects the line it is part of — the fused-line failure the
        short-write repair already exists to prevent, arriving by the other
        door.
        """
        log_path = tmp_path / "log.jsonl"
        _write_lines(log_path, [_sel(selection_id="good")])
        with log_path.open("ab") as fh:
            fh.write(b'{"event": "selection", "selection_id": "half')  # crashed mid-record

        result = _run_feedback(
            tmp_path, log_path, "--selection-id", "good", "--user-corrected", "--json"
        )
        assert result.exit_code == 0, result.output

        lines = log_path.read_bytes().splitlines()
        parsed = []
        for raw_line in lines:
            try:
                parsed.append(json.loads(raw_line))
            except ValueError:
                parsed.append(None)
        # Three lines: the intact selection, the crashed fragment (still one
        # unreadable line, not two records fused into one), and the label.
        assert len(lines) == 3
        assert parsed[1] is None
        assert parsed[2]["event"] == "feedback"
        assert parsed[2]["selection_id"] == "good"

    def test_identical_duplicates_are_labellable_and_conflicting_ones_are_not(self, tmp_path):
        """Replay's own duplicate policy, both halves.

        ``_observed_telemetry`` folds byte-identical repeats (counting them)
        and, when two copies of one id DISAGREE, drops the selection outright
        and marks the run invalid. Labelling the second kind would attach a
        judgement to a selection its only reader refuses to load, and the
        cohort it inherited would come from whichever copy the resolver
        happened to prefer.
        """
        from memtomem_stm.proxy import selection_eval

        identical = tmp_path / "identical.jsonl"
        row = _sel(selection_id="dup", trace_id="t")
        _write_lines(identical, [row, dict(row)])
        result = _run_feedback(
            tmp_path, identical, "--selection-id", "dup", "--user-corrected", "--json"
        )
        assert result.exit_code == 0, result.output
        assert [record["ranker_version"] for record in _feedback_records(identical)] == [
            RANKER_VERSION
        ]

        conflicting = tmp_path / "conflicting.jsonl"
        _write_lines(
            conflicting,
            [
                _sel(selection_id="dup", ranker_version=RANKER_VERSION),
                _sel(selection_id="dup", ranker_version="v1-bm25-tool-relevance"),
            ],
        )
        result = _run_feedback(
            tmp_path, conflicting, "--selection-id", "dup", "--user-corrected", "--json"
        )
        payload = json.loads(result.output)
        assert result.exit_code == 1
        assert payload["error"] == "unusable_record"
        assert "disagree" in payload["message"]
        assert _feedback_records(conflicting) == []

        # The premise, measured on the reader rather than asserted: replay
        # keeps the identical case and discards the conflicting one.
        records, quality = selection_eval._read_telemetry(identical, include_rotated=False)
        selection_eval._observed_telemetry(records, quality)
        # The identical pair is folded and counted, NOT treated as a conflict —
        # (the fixture trips other quality counters, so the premise is read off
        # these two fields rather than off the overall status).
        assert quality["duplicate_records"] == 1 and quality["conflicting_records"] == 0

        records, quality = selection_eval._read_telemetry(conflicting, include_rotated=False)
        selection_eval._observed_telemetry(records, quality)
        assert quality["conflicting_records"] == 1
        assert quality["status"] == "invalid"

    def test_numerically_equal_copies_are_one_record_not_a_conflict(self, tmp_path):
        """``1`` and ``1.0`` are one value to the reader, so they must be one
        record here. Comparing serialized forms would call these a conflict and
        refuse a selection replay is perfectly happy to load."""
        from memtomem_stm.proxy import selection_eval

        log_path = tmp_path / "log.jsonl"
        _write_lines(
            log_path,
            [
                _sel(selection_id="dup", candidate_count=1, graph_generation=0.0),
                _sel(selection_id="dup", candidate_count=1.0, graph_generation=-0.0),
            ],
        )
        result = _run_feedback(
            tmp_path, log_path, "--selection-id", "dup", "--user-corrected", "--json"
        )
        assert result.exit_code == 0, result.output

        records, quality = selection_eval._read_telemetry(log_path, include_rotated=False)
        selection_eval._observed_telemetry(records, quality)
        assert quality["conflicting_records"] == 0 and quality["duplicate_records"] == 1

    def test_a_line_of_exactly_the_maximum_length_is_read_by_both(self, tmp_path):
        """The cut is measured on the same bytes as replay measures.

        Counting the newline on one side and not the other makes a record of
        exactly the limit exist for one reader and not the other — the whole
        class of divergence this shares a loader to prevent.
        """
        from memtomem_stm.proxy import selection_eval
        from memtomem_stm.proxy.selection_log import MAX_LINE_BYTES

        log_path = tmp_path / "log.jsonl"
        row = _sel(selection_id="edge", pad="")
        # Grow the padding until the line is exactly at the limit — computing
        # it from the serialized length is one arithmetic slip away from
        # testing a line that is merely near the boundary.
        row["pad"] = "x" * (MAX_LINE_BYTES - len(json.dumps(row).encode("utf-8")))
        line = json.dumps(row)
        assert len(line.encode("utf-8")) == MAX_LINE_BYTES, len(line.encode("utf-8"))
        log_path.write_bytes(line.encode("utf-8") + b"\n")

        records, _ = selection_eval._read_telemetry(log_path, include_rotated=False)
        assert [record["selection_id"] for record in records] == ["edge"]
        assert _run_feedback(tmp_path, log_path, "--last", "--user-corrected").exit_code == 0
        assert [record["selection_id"] for record in _feedback_records(log_path)] == ["edge"]

    def test_a_repeated_copy_does_not_make_a_selection_look_newer(self, tmp_path):
        """A, B, A — the third line is a duplicate of the first.

        Replay keeps the FIRST copy of an id, so the newest selection is B; a
        resolver that re-dated A by its duplicate would label a selection the
        reader considers older than one it just passed over.
        """
        log_path = tmp_path / "log.jsonl"
        first = _sel(selection_id="a", server="gh", selected_tool="gh__a")
        second = _sel(selection_id="b", server="gh", selected_tool="gh__b")
        _write_lines(log_path, [first, second, dict(first)])

        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["selection_id"] == "b"

    def test_a_duplicate_of_an_evicted_id_is_not_a_new_selection(self, tmp_path, monkeypatch):
        """`A, B, C, A` with room for two.

        A left the window, but it is not a NEW selection when its copy turns up
        again — replay dates it by its first line, where it is the oldest of
        the three. Re-admitting it would label the oldest selection while
        calling it the most recent.
        """
        monkeypatch.setattr(selection_log_module, "_MAX_FALLTHROUGH", 2)
        log_path = tmp_path / "log.jsonl"
        first = _sel(selection_id="a", selected_tool="srv__a")
        _write_lines(
            log_path,
            [
                first,
                _sel(selection_id="b", selected_tool="srv__b"),
                _sel(selection_id="c", selected_tool="srv__c"),
                dict(first),
            ],
        )
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["selection_id"] == "c"

    def test_a_scan_reads_the_segment_it_started_with(self, tmp_path, monkeypatch):
        """The proxy appends while this runs.

        A reader that followed the writer would resolve against a file nobody
        ever saw whole — and could be walked forward for as long as the writer
        keeps going. The scan stops at the size the segment had when it opened
        it.
        """
        log_path = tmp_path / "log.jsonl"
        _write_lines(log_path, [_sel(selection_id="present")])
        real_open = Path.open

        class AppendsWhileRead:
            """Grows the file once the scan has started reading it — the shape
            a live proxy append has, and the one a size snapshot taken at open
            is there to survive."""

            def __init__(self, wrapped, target):
                self._wrapped = wrapped
                self._target = target
                self._grown = False

            def __enter__(self):
                self._wrapped.__enter__()
                return self

            def __exit__(self, *exc):
                return self._wrapped.__exit__(*exc)

            def fileno(self):
                return self._wrapped.fileno()

            def __iter__(self):
                return self

            def __next__(self):
                if not self._grown:
                    self._grown = True
                    with real_open(self._target, "ab") as writer:
                        writer.write(
                            json.dumps(_sel(selection_id="arrived-later")).encode() + b"\n"
                        )
                return next(self._wrapped)

        def appending_open(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            if self == log_path and args and args[0] == "rb":
                return AppendsWhileRead(handle, self)
            return handle

        monkeypatch.setattr(Path, "open", appending_open)
        record, defect = selection_log_module.resolve_selection(log_path)
        monkeypatch.undo()

        assert defect is None
        assert record is not None and record["selection_id"] == "present"
        # The row really is in the file — the scan declined to read it, rather
        # than the write having failed.
        assert b"arrived-later" in log_path.read_bytes()

    def test_a_bare_carriage_return_frames_the_same_line_for_every_reader(self, tmp_path):
        """Stats, replay and resolution frame lines identically.

        A reader splitting only on newlines fuses two carriage-return-separated
        records into one line and reports a corruption the others do not see.
        """
        from memtomem_stm.proxy import selection_eval

        log_path = tmp_path / "log.jsonl"
        payload = (
            json.dumps(_sel(selection_id="one")).encode()
            + b"\r"
            + json.dumps(_sel(selection_id="two")).encode()
            + b"\n"
        )
        log_path.write_bytes(payload)

        summary = aggregate_selection_log(log_path)
        assert summary["events"]["selection"] == 2 and summary["malformed"] == 0
        records, _ = selection_eval._read_telemetry(log_path, include_rotated=False)
        assert [record["selection_id"] for record in records] == ["one", "two"]
        record, _ = selection_log_module.resolve_selection(log_path)
        assert record is not None and record["selection_id"] == "two"

    def test_a_third_copy_does_not_resurrect_a_conflicting_id(self, tmp_path):
        """A/B/A: the third copy is one more claim about a contradictory
        history, not a casting vote. Both the resolver and replay must keep the
        id poisoned — otherwise the label lands on a selection replay counts as
        conflicting."""
        from memtomem_stm.proxy import selection_eval

        first = _sel(selection_id="dup", ranker_version=RANKER_VERSION)
        second = _sel(selection_id="dup", ranker_version="v1-bm25-tool-relevance")
        log_path = tmp_path / "log.jsonl"
        # Split across a rotated backup and the active file, since that is
        # where the two readers could most easily part company.
        _write_lines(tmp_path / "log.jsonl.1", [dict(first), dict(second)])
        _write_lines(log_path, [dict(first)])

        result = _run_feedback(
            tmp_path, log_path, "--selection-id", "dup", "--user-corrected", "--json"
        )
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "unusable_record"
        assert _feedback_records(log_path) == []

        records, quality = selection_eval._read_telemetry(log_path, include_rotated=True)
        selection_eval._observed_telemetry(records, quality)
        assert quality["status"] == "invalid"
        # Two conflicts, not one: the third copy is counted rather than
        # reinstating the selection an earlier disagreement removed.
        assert quality["conflicting_records"] == 2

    def test_a_row_replaced_between_the_two_passes_is_not_returned(self, tmp_path, monkeypatch):
        """Resolution reads the log twice — candidates, then the copy replay
        keeps — and the file can change in between. The second pass's record is
        re-screened rather than trusted, so a row rewritten out of the filter
        cannot come back as the answer to a filtered question."""
        log_path = tmp_path / "log.jsonl"
        _write_lines(log_path, [_sel(selection_id="a", server="gh", selected_tool="gh__x")])
        real_iter = selection_log_module._iter_selection_records
        state = {"passes": 0}

        def rewriting_iter(*args, **kwargs):
            state["passes"] += 1
            if state["passes"] == 2:
                # Same id, a server the operator did not ask for.
                _write_lines(
                    log_path, [_sel(selection_id="a", server="fs", selected_tool="fs__read")]
                )
            yield from real_iter(*args, **kwargs)

        monkeypatch.setattr(selection_log_module, "_iter_selection_records", rewriting_iter)
        record, defect = selection_log_module.resolve_selection(log_path, server="gh")
        monkeypatch.undo()

        assert record is None, f"returned a record outside the requested filter: {record}"
        assert defect is not None

    def test_an_unsupported_copy_does_not_poison_a_supported_row(self, tmp_path):
        """Replay drops an unsupported ``schema_version`` before it can be a
        copy of anything, so it is not a disagreement — refusing the supported
        row would refuse a selection replay loads. An id with ONLY unsupported
        copies still reports the schema, not "no such selection"."""
        log_path = tmp_path / "log.jsonl"
        _write_lines(
            log_path,
            [
                _sel(selection_id="a"),
                _sel(selection_id="a", schema_version=99, server="other"),
                _sel(selection_id="future-only", schema_version=99),
            ],
        )
        result = _run_feedback(
            tmp_path, log_path, "--selection-id", "a", "--user-corrected", "--json"
        )
        assert result.exit_code == 0, result.output
        assert len(_feedback_records(log_path)) == 1

        result = _run_feedback(
            tmp_path, log_path, "--selection-id", "future-only", "--user-corrected", "--json"
        )
        payload = json.loads(result.output)
        assert result.exit_code == 1
        assert payload["error"] == "unusable_record"
        assert "schema_version" in payload["message"]

    def test_a_boolean_and_a_number_are_not_the_same_value(self, tmp_path):
        """``true`` and ``1`` are one value to Python's ``==`` and two to JSON.
        Two records that disagree about a boolean field are two records, and
        the id they share is poisoned."""
        log_path = tmp_path / "log.jsonl"
        _write_lines(
            log_path,
            [
                _sel(selection_id="dup", graph_generation=True),
                _sel(selection_id="dup", graph_generation=1),
            ],
        )
        result = _run_feedback(
            tmp_path, log_path, "--selection-id", "dup", "--user-corrected", "--json"
        )
        payload = json.loads(result.output)
        assert result.exit_code == 1
        assert payload["error"] == "unusable_record"
        assert _feedback_records(log_path) == []

    def test_copies_of_one_poisoned_id_do_not_crowd_out_an_older_row(self, tmp_path, monkeypatch):
        """The fallthrough budget counts distinct selections, not lines. A
        handful of copies of ONE bad id must not push the labellable row out of
        the window."""
        monkeypatch.setattr(selection_log_module, "_MAX_FALLTHROUGH", 2)
        log_path = tmp_path / "log.jsonl"
        _write_lines(
            log_path,
            [
                _sel(selection_id="older"),
                _sel(selection_id="bad", server="gh"),
                _sel(selection_id="bad", server="fs"),
                _sel(selection_id="bad", server="s3"),
            ],
        )
        assert _run_feedback(tmp_path, log_path, "--last", "--user-corrected").exit_code == 0
        assert [record["selection_id"] for record in _feedback_records(log_path)] == ["older"]

    @pytest.mark.parametrize("kind", ["conflicting", "defective"])
    def test_an_exhausted_fallthrough_says_so_rather_than_no_match(
        self, tmp_path, monkeypatch, kind
    ):
        """Beyond the budget the answer is "these are all unlabellable", which
        sends an operator to `--selection-id` — "nothing matches" would send
        them looking for a selection that is right there.

        Both reasons a row is unlabellable count toward the budget: screening
        the defective ones out before counting would report exhaustion as a
        miss for exactly the log the message is written for.
        """
        monkeypatch.setattr(selection_log_module, "_MAX_FALLTHROUGH", 1)
        log_path = tmp_path / "log.jsonl"
        newest = (
            [_sel(selection_id="bad", server="gh"), _sel(selection_id="bad", server="fs")]
            if kind == "conflicting"
            else [_sel(selection_id="bad", ranker_version=None)]
        )
        _write_lines(log_path, [_sel(selection_id="older"), *newest])
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        payload = json.loads(result.output)
        assert result.exit_code == 1
        assert payload["error"] == "unusable_record"
        assert "--selection-id" in payload["message"]
        # It counts what it actually examined: with the window at one, claiming
        # sixty-four would send an operator looking for selections that do not
        # exist.
        assert "1 most recent" not in payload["message"]
        assert "64" not in payload["message"]
        assert _feedback_records(log_path) == []

    def test_last_falls_through_a_conflicting_selection(self, tmp_path):
        """A conflicting id is not a labellable target, so ``--last`` resolves
        to the newest one that is — with the control that the same layout
        without the conflict picks the newer row."""
        log_path = tmp_path / "log.jsonl"
        _write_lines(
            log_path,
            [
                _sel(selection_id="older"),
                _sel(selection_id="newest", server="gh"),
                _sel(selection_id="newest", server="fs"),
            ],
        )
        assert _run_feedback(tmp_path, log_path, "--last", "--user-corrected").exit_code == 0
        assert [record["selection_id"] for record in _feedback_records(log_path)] == ["older"]

        control = tmp_path / "control.jsonl"
        _write_lines(control, [_sel(selection_id="older"), _sel(selection_id="newest")])
        assert _run_feedback(tmp_path, control, "--last", "--user-corrected").exit_code == 0
        assert [record["selection_id"] for record in _feedback_records(control)] == ["newest"]


class TestTailProbeFailsClosed:
    """The probe answers "does this file end on a record boundary". When it
    cannot answer, the append must assume it does not: the cost of guessing
    wrong that way is a blank line every reader skips, while the other guess
    fuses the record into an unreadable line and reports it written."""

    @pytest.mark.parametrize("failing", ["seek", "read"])
    def test_a_probe_that_cannot_answer_still_opens_a_new_line(
        self, tmp_path, monkeypatch, failing
    ):
        log_path = tmp_path / "log.jsonl"
        _write_lines(log_path, [_sel(selection_id="good")])
        # An unterminated tail: without the leading newline the label fuses
        # onto it.
        with log_path.open("ab") as fh:
            fh.write(b'{"event": "selection", "selection_id": "half')

        real_open = Path.open

        class Blinded:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._wrapped.__exit__(*exc)

            def seek(self, *args):
                if failing == "seek":
                    raise OSError(5, "I/O error")
                return self._wrapped.seek(*args)

            def read(self, *args):
                if failing == "read":
                    raise OSError(5, "I/O error")
                return self._wrapped.read(*args)

        def blinded_open(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            if self == log_path and args and args[0] == "rb":
                return Blinded(handle)
            return handle

        monkeypatch.setattr(Path, "open", blinded_open)
        log = SelectionTelemetryLog(log_path)
        assert log.log_feedback(selection_id="good", user_corrected=True) == "written"
        monkeypatch.undo()

        parsed = []
        for raw_line in log_path.read_bytes().splitlines():
            try:
                parsed.append(json.loads(raw_line))
            except ValueError:
                parsed.append(None)
        # The label is a line of its own and readable — the fragment did not
        # swallow it.
        assert parsed[-1] is not None
        assert parsed[-1]["event"] == "feedback"


class TestUnreadableSegmentIsNotAnAbsentOne:
    """"I could not look there" is not "no such selection".

    The command opens every segment before resolving, but a read that fails
    AFTER that preflight would silently drop a whole segment — and dropping the
    newest one promotes an older row to "most recent", which is a label on a
    selection the operator never chose.
    """

    def test_a_segment_that_fails_to_read_refuses_rather_than_resolving_older(
        self, tmp_path, monkeypatch
    ):
        log_path = tmp_path / "log.jsonl"
        _write_lines(tmp_path / "log.jsonl.1", [_sel(selection_id="older")])
        _write_lines(log_path, [_sel(selection_id="newest")])

        real_open = Path.open

        class FailsMidRead:
            """Opens fine, fails on the first read — the case the command's
            open-only preflight cannot catch."""

            def __init__(self, wrapped):
                self._wrapped = wrapped

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._wrapped.__exit__(*exc)

            def __iter__(self):
                raise OSError(5, "I/O error")

            def read(self, *args):
                raise OSError(5, "I/O error")

        def failing_open(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            if self == log_path and args and args[0] == "rb":
                return FailsMidRead(handle)
            return handle

        monkeypatch.setattr(Path, "open", failing_open)
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        monkeypatch.undo()

        payload = json.loads(result.output)
        assert result.exit_code == 1
        assert payload["error"] == "log_unreadable"
        assert "log.jsonl" in payload["message"]
        assert _feedback_records(log_path) == []

        # Control: the same two segments, readable, resolve to the newest —
        # so the refusal above is the read failure and not the layout.
        assert _run_feedback(tmp_path, log_path, "--last", "--user-corrected").exit_code == 0
        assert [record["selection_id"] for record in _feedback_records(log_path)] == ["newest"]

    def test_an_exact_id_reports_a_read_failure_it_meets_after_a_conflict(
        self, tmp_path, monkeypatch
    ):
        """Resolving by id scans to the end rather than stopping at the first
        disagreement. Stopping early would answer "this id conflicts" while a
        later segment — the one that might have held the deciding copy — was
        never opened at all."""
        backup = tmp_path / "log.jsonl.1"
        _write_lines(
            backup,
            [_sel(selection_id="dup", server="gh"), _sel(selection_id="dup", server="fs")],
        )
        log_path = tmp_path / "log.jsonl"
        _write_lines(log_path, [_sel(selection_id="other")])

        real_open = Path.open

        def failing_open(self, *args, **kwargs):
            if self == log_path and args and args[0] == "rb":
                raise OSError(5, "I/O error")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", failing_open)
        with pytest.raises(selection_log_module.SelectionLogUnreadable):
            selection_log_module.resolve_selection(log_path, selection_id="dup")
        monkeypatch.undo()


class TestCleanupFailuresDoNotOverruleACompleteWrite:
    """Once the record's bytes are complete, no cleanup fault may report
    ``failed`` — that is the one answer that sends an operator to write the
    same label a second time."""

    def test_a_failed_fsync_and_a_failed_close_together_are_unconfirmed(
        self, tmp_path, monkeypatch
    ):
        log_path = tmp_path / "log.jsonl"
        log = SelectionTelemetryLog(log_path)
        log.initialize()
        real_close = os.close
        log_inode = log_path.stat().st_ino

        def failing_fsync(fd):
            raise OSError(5, "I/O error")

        def failing_close(fd):
            try:
                is_log = os.fstat(fd).st_ino == log_inode
            except OSError:  # pragma: no cover - defensive
                is_log = False
            real_close(fd)
            if is_log:
                raise OSError(5, "I/O error on close")

        monkeypatch.setattr(selection_log_module.os, "fsync", failing_fsync)
        monkeypatch.setattr(selection_log_module.os, "close", failing_close)
        status = log.log_feedback(selection_id="a", user_corrected=True)
        monkeypatch.undo()

        assert status == "unconfirmed"
        assert [record["selection_id"] for record in _feedback_records(log_path)] == ["a"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory descriptors")
    def test_a_directory_descriptor_that_will_not_close_is_still_unconfirmed(
        self, tmp_path, monkeypatch
    ):
        """The directory sync has the same exposure as the append's own close:
        raising out of its cleanup would be re-read as a failed write."""
        log_path = tmp_path / "log.jsonl"
        log = SelectionTelemetryLog(log_path)
        real_close = os.close
        import stat as stat_module

        def failing_close(fd):
            try:
                is_dir = stat_module.S_ISDIR(os.fstat(fd).st_mode)
            except OSError:  # pragma: no cover - defensive
                is_dir = False
            real_close(fd)
            if is_dir:
                raise OSError(5, "I/O error on close")

        monkeypatch.setattr(selection_log_module.os, "close", failing_close)
        status = log.log_feedback(selection_id="a", user_corrected=True)
        monkeypatch.undo()

        # The directory WAS synced; only its descriptor misbehaved on close, so
        # the write stands as written rather than being downgraded — and above
        # all it is not "failed".
        assert status in ("written", "unconfirmed")
        assert [record["selection_id"] for record in _feedback_records(log_path)] == ["a"]


class TestEnvOverlayThatWasIgnoredEntirely:
    """A bare ``MEMTOMEM_STM_PROXY`` the overlay could not honor resolves to
    the same empty fragment as an unset environment — and the command would
    then label whichever log the DEFAULTS name. The server refuses to start on
    that environment; a writing command must not quietly proceed on it."""

    @pytest.mark.parametrize("payload", ["{", "[]", '"a string"'])
    @pytest.mark.parametrize("config_present", [False, True])
    def test_an_undecodable_bare_overlay_refuses_the_write(
        self, tmp_path, monkeypatch, payload, config_present
    ):
        from memtomem_stm.cli.proxy import cli

        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        # Both routes: with no file the defaults would name the log, and with a
        # file the FILE would — either way it is not the environment the
        # operator set, which is the whole hazard.
        config = tmp_path / "proxy.json"
        if config_present:
            config.write_text(
                json.dumps({"selection_telemetry": {"path": str(log_path)}}), encoding="utf-8"
            )
        else:
            config = tmp_path / "absent-proxy.json"
        monkeypatch.setenv("MEMTOMEM_STM_PROXY", payload)
        result = CliRunner().invoke(
            cli,
            [
                "selection",
                "feedback",
                "--config",
                str(config),
                "--last",
                "--user-corrected",
                "--yes",
                "--json",
            ],
        )
        payload_out = json.loads(result.output)
        assert result.exit_code == 1
        assert payload_out["error"] == "config_invalid"
        assert "MEMTOMEM_STM_PROXY" in payload_out["message"]
        assert _feedback_records(log_path) == []

    def test_a_null_bare_overlay_is_consistent_and_proceeds(self, tmp_path, monkeypatch):
        """The positive control: ``null`` resolves to the field defaults, which
        is exactly what an empty overlay expresses, so it is not a rejection —
        without this the test above would pass for a command that refuses on
        any environment at all.

        Routed through the CONFIG the way the refusal cases are (no ``--log``),
        since a path handed in on the command line never consults the config
        and would prove nothing about this."""
        from memtomem_stm.cli.proxy import cli

        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        config = tmp_path / "proxy.json"
        config.write_text(
            json.dumps({"selection_telemetry": {"path": str(log_path)}}), encoding="utf-8"
        )
        monkeypatch.setenv("MEMTOMEM_STM_PROXY", "null")
        result = CliRunner().invoke(
            cli,
            [
                "selection",
                "feedback",
                "--config",
                str(config),
                "--last",
                "--user-corrected",
                "--yes",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["log"] == str(log_path)
        assert len(_feedback_records(log_path)) == 1


class TestUnlabellableSelections:
    """Resolution must not accept a record offline replay will discard.

    ``selection_eval._read_telemetry`` drops records whose ``schema_version``
    it does not support and marks a run invalid when ``selection_id`` is
    missing; a label written against one of those joins nothing on the only
    reader that exists. ``ranker_version`` matters for the same reason in the
    other direction: the label INHERITS it, so a selection with no stamp would
    have its label filed under a cohort this command invented.
    """

    def test_an_unsupported_schema_is_refused_by_id_and_named(self, tmp_path):
        log_path = tmp_path / "log.jsonl"
        _write_lines(
            log_path,
            [_sel(selection_id="good"), _sel(selection_id="future", schema_version=99)],
        )
        result = _run_feedback(
            tmp_path, log_path, "--selection-id", "future", "--user-corrected", "--json"
        )
        payload = json.loads(result.output)
        assert result.exit_code == 1
        # Not "not_found": the row IS in the file and an operator can go read
        # it — the command's job here is to say what is wrong with it.
        assert payload["error"] == "unusable_record"
        assert "schema_version" in payload["message"]
        assert _feedback_records(log_path) == []

    def test_a_selection_with_no_cohort_stamp_is_refused(self, tmp_path):
        log_path = tmp_path / "log.jsonl"
        _write_lines(log_path, [_sel(selection_id="unstamped", ranker_version=None)])
        result = _run_feedback(
            tmp_path, log_path, "--selection-id", "unstamped", "--user-corrected", "--json"
        )
        payload = json.loads(result.output)
        assert result.exit_code == 1
        assert payload["error"] == "unusable_record"
        assert "ranker_version" in payload["message"]
        assert _feedback_records(log_path) == []

    def test_last_skips_a_record_it_could_not_label(self, tmp_path):
        """An inferred target that cannot be labelled is not the answer.

        The positive control matters here: the defective row is the newest, so
        a run that picks the older one only proves anything if the SAME layout
        with a valid newest row picks the newest.
        """
        log_path = tmp_path / "log.jsonl"
        _write_lines(
            log_path,
            [_sel(selection_id="older"), _sel(selection_id="newest", schema_version=99)],
        )
        assert _run_feedback(tmp_path, log_path, "--last", "--user-corrected").exit_code == 0
        assert [record["selection_id"] for record in _feedback_records(log_path)] == ["older"]

        control = tmp_path / "control.jsonl"
        _write_lines(control, [_sel(selection_id="older"), _sel(selection_id="newest")])
        assert _run_feedback(tmp_path, control, "--last", "--user-corrected").exit_code == 0
        assert [record["selection_id"] for record in _feedback_records(control)] == ["newest"]

    def test_a_line_truncated_mid_character_is_skipped_not_repaired(self, tmp_path):
        """Strict decoding, so the resolver and replay agree on what exists.

        ``errors="replace"`` turns a record truncated mid-character into a
        *different*, parseable string — labelling a selection whose id no
        reader of the same bytes agrees with.
        """
        log_path = tmp_path / "log.jsonl"
        _write_lines(log_path, [_sel(selection_id="older")])
        # The bad byte sits INSIDE the id string, which is what makes the two
        # decodings disagree instead of both rejecting the line: strictly it is
        # undecodable, while ``errors="replace"`` yields a perfectly parseable
        # record naming a selection ("newest\ufffd") that nothing else agrees
        # exists.
        with log_path.open("ab") as fh:
            line = json.dumps(_sel(selection_id="newestX")).encode("utf-8")
            fh.write(line.replace(b"newestX", b"newest\xed") + b"\n")

        assert _run_feedback(tmp_path, log_path, "--last", "--user-corrected").exit_code == 0
        # Read tolerantly: the undecodable line is still in the file, and the
        # point of the test is which record the LABEL names.
        labels = [
            record["selection_id"]
            for record in _tolerant_feedback_records(log_path)
        ]
        assert labels == ["older"]

        control = tmp_path / "control.jsonl"
        _write_lines(control, [_sel(selection_id="older"), _sel(selection_id="newest")])
        assert _run_feedback(tmp_path, control, "--last", "--user-corrected").exit_code == 0
        assert [record["selection_id"] for record in _feedback_records(control)] == ["newest"]


def _tolerant_feedback_records(path: Path) -> list[dict]:
    """``_feedback_records`` for a log that holds undecodable bytes."""
    records = []
    for raw_line in path.read_bytes().splitlines():
        try:
            record = json.loads(raw_line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(record, dict) and record.get("event") == "feedback":
            records.append(record)
    return records


class TestUnconfirmedWriteIsReported:
    def test_an_unflushed_label_is_neither_success_nor_a_clean_failure(
        self, tmp_path, monkeypatch
    ):
        """The operator is told what is and is not known.

        Reporting success would promise durability nothing proved; reporting a
        plain failure would invite a re-write while the label may already be
        there. The message must say which, and that a re-run is safe.
        """
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])

        def failing_fsync(fd):
            raise OSError(5, "I/O error")

        monkeypatch.setattr(selection_log_module.os, "fsync", failing_fsync)
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        monkeypatch.undo()

        payload = json.loads(result.output)
        assert result.exit_code == 1
        assert payload["error"] == "write_unconfirmed"
        # The retry it advises names the row it resolved. Another ``--last``
        # could infer a different selection by the time it runs, and the
        # judgement would land on that one instead.
        resolved = payload["selection_id"]
        assert f"--selection-id {resolved}" in payload["message"]
        # The message says the label reached the log; it must be true.
        records = _feedback_records(log_path)
        assert [record["selection_id"] for record in records] == [resolved]


def _fsync_recorder(monkeypatch):
    """Record what ``_append`` flushes, by kind. Returns the list of modes."""
    import stat as stat_module

    real_fsync = os.fsync
    kinds: list[str] = []

    def patched(fd):
        try:
            mode = os.fstat(fd).st_mode
            kinds.append("dir" if stat_module.S_ISDIR(mode) else "file")
        except OSError:  # pragma: no cover - defensive
            kinds.append("unknown")
        return real_fsync(fd)

    monkeypatch.setattr(selection_log_module.os, "fsync", patched)
    return kinds


class TestLabelDurability:
    """A label exists nowhere else, so "written" must mean it survives a crash.

    The call-path emitters deliberately do NOT pay this: their records are one
    sample among many that sampling may drop outright, and a device flush in
    front of every proxied call would be charged to the call it only accounts
    for.
    """

    def test_a_label_is_flushed_before_it_is_reported_written(self, tmp_path, monkeypatch):
        log_path = tmp_path / "log.jsonl"
        log = SelectionTelemetryLog(log_path)
        log.initialize()
        kinds = _fsync_recorder(monkeypatch)

        assert log.log_feedback(selection_id="a", user_corrected=True) == "written"
        assert "file" in kinds, "the label was reported written without being flushed"

    @pytest.mark.skipif(sys.platform == "win32", reason="no directory fsync on Windows")
    def test_the_creating_append_also_syncs_the_directory_entry(self, tmp_path, monkeypatch):
        """Flushing the descriptor does not make a NEW name durable.

        With the log absent (the first label after a crash that lost the active
        file, say) the record's bytes can be on disk under a filename the
        directory does not yet record.
        """
        log_path = tmp_path / "log.jsonl"
        log = SelectionTelemetryLog(log_path)
        assert not log_path.exists()
        kinds = _fsync_recorder(monkeypatch)

        assert log.log_feedback(selection_id="a", user_corrected=True) == "written"
        assert kinds.count("dir") == 1

        # The second label lands in a file whose name is already durable, so it
        # pays for the contents only — otherwise every append would sync the
        # directory and this test would pass without the ``created`` guard.
        kinds.clear()
        assert log.log_feedback(selection_id="b", user_corrected=True) == "written"
        assert kinds.count("dir") == 0

    def test_the_call_path_emitters_do_not_pay_for_durability(self, tmp_path, monkeypatch):
        log_path = tmp_path / "log.jsonl"
        log = SelectionTelemetryLog(log_path)
        log.initialize()
        kinds = _fsync_recorder(monkeypatch)

        selection_id = log.log_selection(
            server="gh",
            selected_tool="gh__a",
            candidate_tools=["gh__a"],
            arguments={"q": "x"},
            trace_id="t",
        )
        log.log_execution(
            selection_id=str(selection_id),
            trace_id="t",
            server="gh",
            selected_tool="gh__a",
            ok=True,
            latency_ms=1.0,
        )
        assert kinds == []

    def test_a_failed_flush_is_unconfirmed_rather_than_written_or_failed(
        self, tmp_path, monkeypatch
    ):
        """Neither verdict is available: the bytes ARE in the file.

        "written" would promise durability nothing proved; "failed" would send
        an operator to write the same label again on a claim this process
        cannot make.
        """
        log_path = tmp_path / "log.jsonl"
        log = SelectionTelemetryLog(log_path)
        log.initialize()

        def failing_fsync(fd):
            raise OSError(5, "I/O error")

        monkeypatch.setattr(selection_log_module.os, "fsync", failing_fsync)
        assert log.log_feedback(selection_id="a", user_corrected=True) == "unconfirmed"
        monkeypatch.undo()

        assert [record["selection_id"] for record in _feedback_records(log_path)] == ["a"]
        assert log.events_written == 0
        assert log.write_errors == 1

    def test_a_failed_close_after_a_complete_write_is_unconfirmed(self, tmp_path, monkeypatch):
        log_path = tmp_path / "log.jsonl"
        log = SelectionTelemetryLog(log_path)
        log.initialize()
        real_close = os.close
        log_inode = log_path.stat().st_ino

        def patched_close(fd):
            try:
                is_log = os.fstat(fd).st_ino == log_inode
            except OSError:  # pragma: no cover - defensive
                is_log = False
            real_close(fd)
            if is_log:
                raise OSError(5, "I/O error on close")

        monkeypatch.setattr(selection_log_module.os, "close", patched_close)
        assert log.log_feedback(selection_id="a", user_corrected=True) == "unconfirmed"
        monkeypatch.undo()

        # The record is intact: this is exactly the case that must not read as
        # a failure, since a retry would duplicate a label that is already here.
        assert [record["selection_id"] for record in _feedback_records(log_path)] == ["a"]
        assert log.events_written == 0
        assert log.write_errors == 1

    def test_a_directory_that_cannot_be_synced_is_unconfirmed(self, tmp_path, monkeypatch):
        log_path = tmp_path / "log.jsonl"
        log = SelectionTelemetryLog(log_path)
        monkeypatch.setattr(selection_log_module, "fsync_dir", lambda directory: False)

        assert log.log_feedback(selection_id="a", user_corrected=True) == "unconfirmed"
        assert [record["selection_id"] for record in _feedback_records(log_path)] == ["a"]
        assert log.write_errors == 1

    def test_a_short_write_leaves_no_record_even_though_a_fragment_remains(
        self, tmp_path, monkeypatch
    ):
        """What ``"failed"`` promises, stated exactly.

        Not "the file is unchanged" — a short write's fragment stays, because
        truncating it back would rewind a file other processes append to. What
        it promises is that no RECORD was written: nothing parses, so nothing
        joins a selection or lands in a cohort.
        """
        log_path = tmp_path / "log.jsonl"
        log = SelectionTelemetryLog(log_path)
        log.initialize()
        monkeypatch.setattr(selection_log_module.os, "write", _short_write_on(log_path))

        assert log.log_feedback(selection_id="a", user_corrected=True) == "failed"
        monkeypatch.undo()

        raw = log_path.read_bytes()
        assert raw, "the fragment is expected to remain; only records are promised absent"
        # Read the way a reader does — tolerantly — because the fragment is by
        # construction unparseable and ``_feedback_records`` would raise on it.
        summary = aggregate_selection_log(log_path)
        assert summary["events"]["feedback"] == 0
        assert summary["malformed"] == 1


class TestSelectionFeedbackRobustness:
    """The failure modes a first-round review found (codex, PR #853)."""

    def test_write_failure_is_reported_not_swallowed(self, tmp_path):
        """The sink swallows write faults; this caller must not.

        ``_append`` never raises — a telemetry problem must not break a
        proxied call — so a command that ignores its outcome tells an operator
        their label exists when the file holds nothing.
        """
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        log_path.chmod(0o444)
        try:
            result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        finally:
            log_path.chmod(0o644)
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "write_failed"
        assert _feedback_records(log_path) == []

    def test_redacted_write_is_reported_as_a_failure(self, tmp_path, monkeypatch):
        """A consumed-but-unwritten record is not a recorded label.

        ``log_selection`` treats a redaction drop as success on purpose (its
        pairing semantics are left-outer); for a human waiting to hear that
        their judgement was stored, the two outcomes are not the same.
        """
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        monkeypatch.setattr(
            "memtomem_stm.proxy.selection_log.contains_sensitive_content", lambda _line: True
        )
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "write_redacted"
        assert _feedback_records(log_path) == []

    @pytest.mark.parametrize(
        "flags",
        [
            ["--user-corrected", "--no-user-corrected"],
            ["--no-user-corrected", "--user-corrected"],
            ["--operator-override", "--no-operator-override"],
        ],
        ids=["corrected", "corrected_reversed", "override"],
    )
    def test_contradictory_labels_are_a_usage_error(self, tmp_path, flags):
        """Not last-flag-wins: the two flags assert opposite facts, and
        silently picking one inverts a training label."""
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        result = _run_feedback(tmp_path, log_path, "--last", *flags)
        assert result.exit_code == 2, result.output
        assert _feedback_records(log_path) == []

    def test_resolution_is_printed_before_the_write(self, tmp_path, monkeypatch):
        """Observed at the write itself, not inferred from line order.

        Output order alone would still pass if the append ran before either
        line was emitted, so the write is instrumented and asked what had
        already been printed when it fired.
        """
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        seen: dict[str, str] = {}
        real_write = selection_cmd._write_label

        def observing_write(log, **fields):
            # CliRunner wraps a BytesIO; flush the text layer first so anything
            # already echoed is visible in the buffer at this instant.
            sys.stdout.flush()
            seen["at_write"] = sys.stdout.buffer.getvalue().decode("utf-8")  # type: ignore[attr-defined]
            return real_write(log, **fields)

        monkeypatch.setattr(selection_cmd, "_write_label", observing_write)
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected")
        assert result.exit_code == 0, result.output
        assert "at_write" in seen, "the write never ran"
        assert seen["at_write"].startswith("Selection ")
        assert "gh__a" in seen["at_write"]
        assert "Labelled selection" not in seen["at_write"]

    def test_tty_confirmation_can_refuse_the_write(self, tmp_path):
        from memtomem_stm.cli.proxy import cli

        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        # CliRunner installs its own stdin, so patch the named decision rather
        # than the stream object the command would have consulted.
        with mock.patch("memtomem_stm.cli.selection_cmd._human_at_the_terminal", return_value=True):
            result = CliRunner().invoke(
                cli,
                [
                    "selection",
                    "feedback",
                    "--log",
                    str(log_path),
                    "--config",
                    str(tmp_path / "absent.json"),
                    "--last",
                    "--user-corrected",
                ],
                input="n\n",
            )
        assert result.exit_code == 1
        assert _feedback_records(log_path) == []

    def test_explicit_consent_writes_without_prompting(self, tmp_path):
        """With ``--yes`` there is nothing left to ask.

        Named for what it runs: the helper supplies ``--yes``, so this is the
        consent-given path, not the refusal one — that contract is pinned by
        the ``--yes``-less tests above, and a name promising it here would
        report a pass for a path this test never enters."""
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected")
        assert result.exit_code == 0, result.output
        assert "Label this selection?" not in result.output
        assert len(_feedback_records(log_path)) == 1

    def test_backups_without_an_active_file_are_labellable(self, tmp_path):
        """A crash between ``active -> .1`` and the next append leaves the whole
        history in backups; refusing to label it would contradict the default
        rotated search."""
        log_path = tmp_path / "log.jsonl"
        _seed_log(tmp_path / "log.jsonl.1", rows=[{"server": "gh", "tool": "gh__rot"}])
        assert not log_path.exists()
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["selected_tool"] == "gh__rot"

    def test_eviction_between_confirming_and_writing_refuses(self, tmp_path, monkeypatch):
        """Confirmation is human time, and a rotation can land inside it.

        The resolve runs under the rotation lock, but the agreement that
        follows does not — so the target is re-checked while rotation is
        excluded again. Simulated at that exact seam by evicting the selection
        just before the verify.
        """
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        real_resolve = selection_log_module.resolve_selection
        state = {"evicted": False}

        def evict_then_resolve(*args, **kwargs):
            # The verify pass is the one that looks up an exact id.
            if kwargs.get("selection_id") and not state["evicted"]:
                state["evicted"] = True
                # ``max_backups=0`` rotation unlinks the active file outright,
                # taking the resolved selection with it.
                log_path.unlink()
                log_path.write_text("", encoding="utf-8")
            return real_resolve(*args, **kwargs)

        def must_not_write(log, **fields):  # pragma: no cover - asserts absence
            raise AssertionError("wrote a label for an evicted selection")

        monkeypatch.setattr(selection_cmd, "resolve_selection", evict_then_resolve)
        monkeypatch.setattr(selection_cmd, "_write_label", must_not_write)
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "log_rotated"
        assert _feedback_records(log_path) == []

    def test_a_row_that_changes_under_the_confirmation_is_refused(self, tmp_path, monkeypatch):
        """Still present is not the same as still the row that was agreed to.

        A copy of the id can be APPENDED while the operator reads the prompt —
        by a replay ingest, or a second hand — and the two then disagree, which
        is exactly what replay refuses to load. The verify pass must catch that
        rather than seeing "the id is still there" and writing.
        """
        log_path = tmp_path / "log.jsonl"
        _write_lines(log_path, [_sel(selection_id="a", ranker_version=RANKER_VERSION)])
        real_resolve = selection_log_module.resolve_selection
        state = {"appended": False}

        def append_then_resolve(*args, **kwargs):
            if kwargs.get("selection_id") and not state["appended"]:
                state["appended"] = True
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(_sel(selection_id="a", ranker_version="v1-bm25-tool-relevance"))
                        + "\n"
                    )
            return real_resolve(*args, **kwargs)

        def must_not_write(log, **fields):  # pragma: no cover - asserts absence
            raise AssertionError("wrote a label for a record the operator never saw")

        monkeypatch.setattr(selection_cmd, "resolve_selection", append_then_resolve)
        monkeypatch.setattr(selection_cmd, "_write_label", must_not_write)
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        payload = json.loads(result.output)
        assert result.exit_code == 1
        assert payload["error"] == "selection_changed"
        assert "disagree" in payload["message"]
        assert _feedback_records(log_path) == []

    def test_a_row_replaced_under_the_confirmation_is_refused(self, tmp_path, monkeypatch):
        """The other shape: the id survives but its content is different — no
        conflict for replay to see, and the label would carry a cohort and
        trace the operator was never shown."""
        log_path = tmp_path / "log.jsonl"
        _write_lines(log_path, [_sel(selection_id="a", ranker_version=RANKER_VERSION)])
        real_resolve = selection_log_module.resolve_selection
        state = {"swapped": False}

        def swap_then_resolve(*args, **kwargs):
            if kwargs.get("selection_id") and not state["swapped"]:
                state["swapped"] = True
                _write_lines(
                    log_path, [_sel(selection_id="a", ranker_version="v1-bm25-tool-relevance")]
                )
            return real_resolve(*args, **kwargs)

        def must_not_write(log, **fields):  # pragma: no cover - asserts absence
            raise AssertionError("wrote a label for a record the operator never saw")

        monkeypatch.setattr(selection_cmd, "resolve_selection", swap_then_resolve)
        monkeypatch.setattr(selection_cmd, "_write_label", must_not_write)
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "selection_changed"
        assert _feedback_records(log_path) == []

    def test_a_piped_stdout_is_refused_like_a_piped_stdin(self, tmp_path, monkeypatch):
        """``mms ... | jq`` leaves stdin a terminal while the prompt itself —
        and the resolved selection above it — goes into the pipe. A
        confirmation nobody can read is not the check ``--last`` leans on."""
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        monkeypatch.setattr(selection_cmd.sys.stdin, "isatty", lambda: True, raising=False)
        assert selection_cmd._human_at_the_terminal() is False

        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", yes=False)
        assert result.exit_code == 2
        assert "--yes" in result.output
        assert _feedback_records(log_path) == []

    def test_non_interactive_last_is_refused_without_yes(self, tmp_path):
        """Consent, not a default. A pipe cannot answer a prompt, so the
        command must refuse rather than write on an inferred target."""
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", yes=False)
        assert result.exit_code == 2, result.output
        assert "--yes" in result.output
        assert _feedback_records(log_path) == []

    def test_json_last_is_refused_without_yes(self, tmp_path):
        """A formatting flag must not authorize a write (the CLI-wide rule),
        and a prompt would corrupt the single-document stdout contract."""
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        with mock.patch("memtomem_stm.cli.selection_cmd._human_at_the_terminal", return_value=True):
            result = _run_feedback(
                tmp_path, log_path, "--last", "--user-corrected", "--json", yes=False
            )
        assert result.exit_code == 2
        assert json.loads(result.output)["error"] == "confirmation_required"
        assert _feedback_records(log_path) == []

    def test_selection_id_never_requires_consent(self, tmp_path):
        """Positive control for the two refusals above: the id names its own
        target, so there is no inference to confirm."""
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        sid = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])["selection_id"]
        result = _run_feedback(
            tmp_path, log_path, "--selection-id", sid, "--user-corrected", yes=False
        )
        assert result.exit_code == 0, result.output
        assert len(_feedback_records(log_path)) == 1

    def test_resolution_reports_a_busy_log(self, tmp_path):
        """A held lock refuses the command rather than letting it scan."""
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        with selection_log_module.rotation_lock(log_path) as acquired:
            assert acquired
            result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "log_busy"
        assert _feedback_records(log_path) == []

    def test_each_critical_operation_runs_while_holding_the_lock(self, tmp_path, monkeypatch):
        """Ownership probed at each operation, not inferred from one refusal.

        An external lock held for the whole run proves nothing about *which*
        hold refused — with only the second one present the command reports the
        same ``log_busy``. So instead the command runs unimpeded and each
        critical operation asks: can a competing acquire succeed right now? It
        must not, at the resolve, at the post-confirmation verify, and at the
        append. ``flock`` is per open file description, so a competing acquire
        from this same process is a real test of the command's hold.
        """
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        competing: list[tuple[str, bool]] = []

        def probe(label: str) -> None:
            with selection_log_module.rotation_lock(log_path) as got:
                competing.append((label, got))

        real_resolve = selection_log_module.resolve_selection

        def probing_resolve(*args, **kwargs):
            probe("verify" if kwargs.get("selection_id") else "resolve")
            return real_resolve(*args, **kwargs)

        real_write = selection_cmd._write_label

        def probing_write(log, **fields):
            probe("append")
            return real_write(log, **fields)

        monkeypatch.setattr(selection_cmd, "resolve_selection", probing_resolve)
        monkeypatch.setattr(selection_cmd, "_write_label", probing_write)
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected")
        assert result.exit_code == 0, result.output
        assert [label for label, _ in competing] == ["resolve", "verify", "append"]
        assert all(not got for _, got in competing), (
            f"an operation ran outside the rotation lock: {competing}"
        )

    def test_verification_after_confirming_reports_a_busy_log(self, tmp_path, monkeypatch):
        """The second hold specifically: the lock is taken *during* the prompt.

        That is the window the second hold exists for, and a command that
        locked only its resolve would sail past it and write.
        """
        from memtomem_stm.cli.proxy import cli

        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        held: list[object] = []

        def confirm_and_lock(*args, **kwargs):
            ctx = selection_log_module.rotation_lock(log_path)
            assert ctx.__enter__() is True
            held.append(ctx)
            return True

        monkeypatch.setattr(selection_cmd, "_human_at_the_terminal", lambda: True)
        monkeypatch.setattr(selection_cmd.click, "confirm", confirm_and_lock)
        try:
            result = CliRunner().invoke(
                cli,
                [
                    "selection",
                    "feedback",
                    "--log",
                    str(log_path),
                    "--config",
                    str(tmp_path / "absent.json"),
                    "--last",
                    "--user-corrected",
                ],
            )
        finally:
            for ctx in held:
                ctx.__exit__(None, None, None)
        assert held, "the confirmation never ran"
        assert result.exit_code == 1
        assert "rotation lock is held" in result.output
        assert _feedback_records(log_path) == []

    def test_an_uncreatable_lock_is_a_stable_error(self, tmp_path, monkeypatch):
        """The lock is a sidecar FILE, so taking it can fail for reasons that
        are not contention. A traceback with empty stdout is not something a
        scripted caller can handle.

        The failure is injected rather than provoked with permissions: Windows
        is a required CI target and ``chmod`` cannot express "no create" there,
        so a permissions-based test would assert nothing on half the matrix.
        """
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])

        def refuse(_path):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(selection_log_module, "open_lock_fd", refuse)
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "lock_failed"
        assert _feedback_records(log_path) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory permissions")
    def test_an_uncreatable_lock_is_a_stable_error_for_real(self, tmp_path):
        """The same contract against a genuinely unwritable directory, so the
        injected test above cannot drift from what the OS actually does."""
        log_dir = tmp_path / "ro"
        log_dir.mkdir()
        log_path = log_dir / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        log_dir.chmod(0o555)
        try:
            result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        finally:
            log_dir.chmod(0o755)
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "lock_failed"

    def test_an_invalid_config_refuses_rather_than_labelling_the_default_log(self, tmp_path):
        """A writing command must not guess which file it is annotating.

        ``load_from_file`` returns ``None`` for a config that exists but does
        not parse, and falling back to defaults would silently label the
        DEFAULT log instead of the configured one.
        """
        from memtomem_stm.cli.proxy import cli

        bad_config = tmp_path / "proxy.json"
        bad_config.write_text("{ not json", encoding="utf-8")
        result = CliRunner().invoke(
            cli,
            [
                "selection",
                "feedback",
                "--config",
                str(bad_config),
                "--last",
                "--yes",
                "--user-corrected",
                "--json",
            ],
        )
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "config_invalid"

    def test_an_unreadable_segment_is_not_reported_as_no_match(self, tmp_path, monkeypatch):
        """ "I could not look there" must not read as "no such selection".

        The failure is injected: Windows is a required CI target and its
        ``chmod`` cannot express "unreadable", so a permissions-based version
        asserts nothing there. The POSIX companion below keeps this honest.
        """
        log_path = tmp_path / "log.jsonl"
        _seed_log(tmp_path / "log.jsonl.1", rows=[{"server": "gh", "tool": "gh__rot"}])
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        real_open = Path.open

        def refuse_backup(self, *args, **kwargs):
            if self.name.endswith(".1"):
                raise PermissionError(13, "Permission denied")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", refuse_backup)
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "log_unreadable"
        assert _feedback_records(log_path) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions")
    def test_an_unreadable_segment_is_not_reported_as_no_match_for_real(self, tmp_path):
        """The same contract against real permissions, so the injected test
        above cannot drift from what the OS actually does."""
        log_path = tmp_path / "log.jsonl"
        _seed_log(tmp_path / "log.jsonl.1", rows=[{"server": "gh", "tool": "gh__rot"}])
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        backup = tmp_path / "log.jsonl.1"
        backup.chmod(0o000)
        try:
            result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        finally:
            backup.chmod(0o644)
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "log_unreadable"

    def test_a_repaired_newline_only_short_write_is_a_success(self, tmp_path, monkeypatch):
        """When the ONLY missing byte was the newline, the repair restores the
        exact intended bytes — the record is complete and readable, so calling
        it a failure would be false, and would invite a retry that duplicates
        the label."""
        log_path = tmp_path / "log.jsonl"
        log = SelectionTelemetryLog(log_path)
        log.initialize()
        monkeypatch.setattr(
            selection_log_module.os, "write", _short_write_on(log_path, drop_only_newline=True)
        )
        assert log.log_feedback(selection_id="a", user_corrected=True) == "written"
        monkeypatch.undo()

        lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert [json.loads(line)["selection_id"] for line in lines] == ["a"]
        assert log.events_written == 1
        assert log.write_errors == 0

    def test_a_short_write_does_not_corrupt_the_next_record(self, tmp_path, monkeypatch):
        """The follow-up append must stay readable and honestly reported.

        A fragment with no newline swallows the next record into its line: that
        caller's own write succeeded, so it is told the record survived while
        every reader rejects the fused line — one fault becoming a cascade.
        """
        log_path = tmp_path / "log.jsonl"
        log = SelectionTelemetryLog(log_path)
        log.initialize()
        monkeypatch.setattr(selection_log_module.os, "write", _short_write_on(log_path))
        assert log.log_feedback(selection_id="a", user_corrected=True) == "failed"
        assert log.log_feedback(selection_id="b", user_corrected=True) == "written"
        monkeypatch.undo()

        lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        parsed = []
        for line in lines:
            try:
                parsed.append(json.loads(line))
            except ValueError:
                pass
        # Two lines: the unreadable fragment, and the intact follow-up.
        assert len(lines) == 2
        assert [record["selection_id"] for record in parsed] == ["b"]
        assert log.events_written == 1
        assert log.write_errors == 1

    @staticmethod
    def _over_size_pair(log_path):
        """A writer past its size threshold, plus a second writer on the log."""
        rotator = SelectionTelemetryLog(log_path, max_bytes=1, max_backups=0)
        rotator.initialize()
        rotator.log_selection(
            server="gh",
            selected_tool="gh__a",
            candidate_tools=["gh__a"],
            arguments={"q": "x"},
            trace_id="t",
        )
        return rotator, SelectionTelemetryLog(log_path, max_bytes=1, max_backups=0)

    def test_a_rotating_writer_does_not_silently_lose_a_contender(self, tmp_path):
        """Two writers, ``max_backups=0``: the loser must not claim success.

        That configuration rotates by unlinking, so an append racing it can be
        written into an inode about to disappear. Reported as a failure rather
        than counted — a lost record that was announced as written is worse
        than a refused one.

        The rotating writer is simulated by holding BOTH locks, which is what
        one holds: the rotation lock keeps the filenames still, and the
        rotation-active lock is the claim that a rotation is running. Holding
        only the first is a *reader*, and the companion test below pins that
        those two are not the same answer.
        """
        log_path = tmp_path / "log.jsonl"
        _, contender = self._over_size_pair(log_path)
        with selection_log_module.rotation_active_lock(log_path) as claimed:
            assert claimed
            with selection_log_module.rotation_lock(log_path) as acquired:
                assert acquired
                status = contender.log_feedback(selection_id="x", user_corrected=True)
        assert status == "failed"
        assert contender.events_written == 0
        assert contender.write_errors == 1

    def test_an_append_is_never_reported_written_into_an_unlinked_inode(self, tmp_path):
        """The window the guard exists to close, driven through the real path.

        ``max_backups=0`` rotates by unlinking, and rotation happens inside an
        append. Hold one appender between its ``open`` and its ``write`` and
        run the rotating append in that window: without a guard spanning both
        syscalls the record goes into an inode no name points at, and the
        writer is told it was written. Whoever loses here must lose *loudly* —
        what is forbidden is a ``"written"`` whose record is gone.
        """
        log_path = tmp_path / "log.jsonl"
        appender = SelectionTelemetryLog(log_path, max_bytes=10**9, max_backups=0)
        appender.initialize()
        rotator = SelectionTelemetryLog(log_path, max_bytes=1, max_backups=0)
        log_path.write_text('{"seed": 1}\n', encoding="utf-8")

        opened = threading.Event()
        rotated = threading.Event()
        real_write = os.write

        def slow_write(fd, data):
            if b"racer" in data:
                opened.set()
                rotated.wait(3.0)
            return real_write(fd, data)

        def rotate():
            opened.wait(3.0)
            rotator.log_feedback(selection_id="rotator", user_corrected=True)
            rotated.set()

        worker = threading.Thread(target=rotate)
        worker.start()
        with mock.patch.object(selection_log_module.os, "write", slow_write):
            status = appender.log_feedback(selection_id="racer", user_corrected=True)
        worker.join()

        on_disk = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        if status == "written":
            assert "racer" in on_disk, "a record reported as written must be on disk"
        else:
            assert status == "failed", status

    def test_a_reader_holding_the_lock_does_not_cost_the_writer_its_record(self, tmp_path):
        """The reader's hold is not a rotation, and must not be read as one.

        ``mms selection feedback`` holds the rotation lock across its whole
        scan — every segment, twice, on a log whose default ceiling is 50 MB —
        while rotating nothing. Treating that hold as a rotation in flight
        would drop every record appended for its duration, so an
        over-threshold writer under ``max_backups=0`` must still append: no
        rotation can begin while the reader holds the lock, so there is no
        inode for the record to be lost in.
        """
        log_path = tmp_path / "log.jsonl"
        _, contender = self._over_size_pair(log_path)
        with selection_log_module.rotation_lock(log_path) as acquired:  # a reader
            assert acquired
            status = contender.log_feedback(selection_id="x", user_corrected=True)
        assert status == "written"
        assert contender.events_written == 1
        assert contender.write_errors == 0
        assert "x" in log_path.read_text(encoding="utf-8")

    def test_a_listing_that_fails_under_the_lock_is_a_failure_not_a_traceback(
        self, tmp_path, monkeypatch
    ):
        """Discovery runs twice, and only the second one is under the lock.

        The command lists the log directory itself, then the reader lists it
        again inside the rotation lock — so the second can fail where the first
        succeeded, and that raise is a plain ``OSError``, not the
        ``SelectionLogUnreadable`` a failed segment READ raises. Uncaught it
        would leave a traceback and, under ``--json``, an empty document: the
        exact outcome this command refuses everywhere else.

        Patched on the reader's module binding only, which is what makes the
        two listings independent here.
        """
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])

        def refuse(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(selection_log_module, "discover_log_files", refuse)
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"] == "log_unreadable"
        assert _feedback_records(log_path) == []

    def test_a_listing_that_fails_only_after_confirmation_is_also_reported(
        self, tmp_path, monkeypatch
    ):
        """The second locked resolve has its own guard, and its own reason.

        The command resolves twice: once to identify the row, and again under
        the lock after confirmation, because a human pause is exactly when the
        log can change. The companion test above aborts in the FIRST resolve,
        so it says nothing about the second — and the second is the one whose
        window is human-length, which is when a directory's permissions
        realistically change underneath it.
        """
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])

        from memtomem_stm.cli import selection_cmd as selection_cmd_module

        real = selection_cmd_module.resolve_selection
        calls = {"n": 0}

        def fail_on_the_second_resolve(*args, **kwargs):
            # Counted per RESOLVE, not per directory listing: one resolve lists
            # the directory more than once, so counting listings would trip
            # inside the first resolve and pin the other arm instead.
            calls["n"] += 1
            if calls["n"] >= 2:
                raise PermissionError(13, "Permission denied")
            return real(*args, **kwargs)

        monkeypatch.setattr(selection_cmd_module, "resolve_selection", fail_on_the_second_resolve)
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        assert calls["n"] == 2, "the post-confirmation resolve must have been reached"
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "log_unreadable"
        assert _feedback_records(log_path) == []

    def test_the_lock_file_is_not_counted_as_a_rotated_backup(self, tmp_path):
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        assert _run_feedback(tmp_path, log_path, "--last", "--user-corrected").exit_code == 0
        assert selection_log_module.rotation_lock_path(log_path).exists()
        assert aggregate_selection_log(log_path)["rotated_backups"] == 0

    def test_writer_defers_rotation_while_a_reader_holds_the_lock(self, tmp_path):
        """The writer's half of the guarantee.

        Rotation must not rename segments under a reader — and must not block
        the proxied call either, so it skips this round and retries on the next
        append rather than waiting.
        """
        log_path = tmp_path / "log.jsonl"
        log = SelectionTelemetryLog(log_path, max_bytes=1, max_backups=3)
        log.initialize()

        def record(tool: str) -> None:
            log.log_selection(
                server="gh",
                selected_tool=tool,
                candidate_tools=[tool],
                arguments={"q": tool},
                trace_id=tool,
            )

        record("gh__a")
        assert log_path.stat().st_size > 1  # the next append would rotate

        with selection_log_module.rotation_lock(log_path) as acquired:
            assert acquired
            record("gh__b")
            assert not (tmp_path / "log.jsonl.1").exists(), "rotated under the reader"
            # Deferred, not dropped: the record still reached the active file.
            assert "gh__b" in log_path.read_text(encoding="utf-8")

        record("gh__c")  # lock released → rotation proceeds as normal
        assert (tmp_path / "log.jsonl.1").exists()
