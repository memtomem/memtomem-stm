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
        the schema doesn't change shape when #465/toolgraph populate them."""
        log = _make_log(tmp_path)
        _log_pair(log)

        selection, execution = _read_events(log)
        assert selection["reject_reasons"] == {}
        assert selection["candidate_features"] is None
        assert selection["graph_generation"] is None
        assert execution["retry_count"] is None
        assert execution["cost"] is None
        assert execution["cache_hit"] is None

    def test_lines_are_sorted_key_compact_json(self, tmp_path):
        log = _make_log(tmp_path)
        _log_pair(log)
        first = log.path.read_text(encoding="utf-8").splitlines()[0]
        assert first == json.dumps(
            json.loads(first), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )


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


def _make_manager(tmp_path: Path, **log_kwargs) -> tuple[ProxyManager, SelectionTelemetryLog]:
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
    mgr = ProxyManager(proxy_cfg, TokenTracker(), selection_log=log)

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
