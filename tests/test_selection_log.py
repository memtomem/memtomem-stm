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


# ── find_selection / discover_log_files (#469 labelling) ───────────────────


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

    def test_non_tty_does_not_prompt(self, tmp_path):
        """A script must not hang waiting for a confirmation nobody can give."""
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
        real_find = selection_log_module.find_selection
        state = {"evicted": False}

        def evict_then_find(*args, **kwargs):
            # The verify pass is the one that looks up an exact id.
            if kwargs.get("selection_id") and not state["evicted"]:
                state["evicted"] = True
                # ``max_backups=0`` rotation unlinks the active file outright,
                # taking the resolved selection with it.
                log_path.unlink()
                log_path.write_text("", encoding="utf-8")
            return real_find(*args, **kwargs)

        def must_not_write(log, **fields):  # pragma: no cover - asserts absence
            raise AssertionError("wrote a label for an evicted selection")

        monkeypatch.setattr(selection_cmd, "find_selection", evict_then_find)
        monkeypatch.setattr(selection_cmd, "_write_label", must_not_write)
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "log_rotated"
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

        real_find = selection_log_module.find_selection

        def probing_find(*args, **kwargs):
            probe("verify" if kwargs.get("selection_id") else "resolve")
            return real_find(*args, **kwargs)

        real_write = selection_cmd._write_label

        def probing_write(log, **fields):
            probe("append")
            return real_write(log, **fields)

        monkeypatch.setattr(selection_cmd, "find_selection", probing_find)
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

    def test_an_unreadable_segment_is_not_reported_as_no_match(self, tmp_path):
        """ "I could not look there" must not read as "no such selection"."""
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
        assert _feedback_records(log_path) == []

    def test_a_short_write_is_not_reported_as_recorded(self, tmp_path, monkeypatch):
        """``os.write`` may write fewer bytes; the record is then truncated and
        not on disk, so the status must not say otherwise."""
        log_path = tmp_path / "log.jsonl"
        _seed_log(log_path, rows=[{"server": "gh", "tool": "gh__a"}])
        real_write = os.write

        def short_write(fd, data):
            return real_write(fd, data[: len(data) // 2])

        monkeypatch.setattr(selection_log_module.os, "write", short_write)
        result = _run_feedback(tmp_path, log_path, "--last", "--user-corrected", "--json")
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "write_failed"

    def test_a_repaired_newline_only_short_write_is_a_success(self, tmp_path, monkeypatch):
        """When the ONLY missing byte was the newline, the repair restores the
        exact intended bytes — the record is complete and readable, so calling
        it a failure would be false, and would invite a retry that duplicates
        the label."""
        log_path = tmp_path / "log.jsonl"
        log = SelectionTelemetryLog(log_path)
        log.initialize()
        real_write = os.write
        calls = {"n": 0}

        def drop_final_newline(fd, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_write(fd, data[:-1])
            return real_write(fd, data)

        monkeypatch.setattr(selection_log_module.os, "write", drop_final_newline)
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
        real_write = os.write
        calls = {"n": 0}

        def short_first(fd, data):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_write(fd, data[: len(data) // 2])
            return real_write(fd, data)

        monkeypatch.setattr(selection_log_module.os, "write", short_first)
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

    def test_a_rotating_writer_does_not_silently_lose_a_contender(self, tmp_path):
        """Two writers, ``max_backups=0``: the loser must not claim success.

        That configuration rotates by unlinking, so an append racing it can be
        written into an inode about to disappear. Reported as a failure rather
        than counted — a lost record that was announced as written is worse
        than a refused one.
        """
        log_path = tmp_path / "log.jsonl"
        rotator = SelectionTelemetryLog(log_path, max_bytes=1, max_backups=0)
        rotator.initialize()
        contender = SelectionTelemetryLog(log_path, max_bytes=1, max_backups=0)
        rotator.log_selection(
            server="gh",
            selected_tool="gh__a",
            candidate_tools=["gh__a"],
            arguments={"q": "x"},
            trace_id="t",
        )
        with selection_log_module.rotation_lock(log_path) as acquired:  # the rotator
            assert acquired
            status = contender.log_feedback(selection_id="x", user_corrected=True)
        assert status == "failed"
        assert contender.events_written == 0
        assert contender.write_errors == 1

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
