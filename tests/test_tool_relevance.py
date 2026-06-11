"""Tests for deterministic tool-relevance ranking (#466 v0).

Pins the acceptance criteria: deterministic, reproducible ranking given the
same inputs (byte-identical ``ranked_candidates``, alphabetical tie-break,
no dependence on upstream discovery order), the privacy posture (raw query
text never enters telemetry — sha256/length/source only), and the v0
boundary (ranking is telemetry input via ``candidate_features``; exposure
never changes).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    ToolRelevanceConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, ProxyToolInfo, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.selection_log import SelectionTelemetryLog
from memtomem_stm.proxy.tool_relevance import (
    RANKER_VERSION_BM25,
    ToolRelevanceRanker,
    build_candidate_features,
    derive_query,
)

FEATURES_KEYS = {"query_source", "query_sha256", "query_chars", "ranked_candidates"}
RANKED_ENTRY_KEYS = {"tool", "rank", "relevance_score", "risk_penalty", "final_score"}


def _info(name: str, description: str, schema: dict | None = None) -> ProxyToolInfo:
    return ProxyToolInfo(
        prefixed_name=f"test__{name}",
        description=description,
        input_schema=schema or {"type": "object"},
        server="srv",
        original_name=name,
    )


CANDIDATES = [
    _info(
        "send_message",
        "Send a message to a Slack channel",
        {"type": "object", "properties": {"channel": {"type": "string"}}},
    ),
    _info("read_file", "Read a file from the local filesystem"),
    _info("create_issue", "Create a GitHub issue in a repository"),
]


# ── derive_query ─────────────────────────────────────────────────────────


class TestDeriveQuery:
    def test_context_query_wins(self):
        q = derive_query({"_context_query": "deploy the service", "path": "/tmp/x"})
        assert q == ("deploy the service", "context_query")

    def test_fallback_joins_string_values_sorted_by_key(self):
        q = derive_query({"b": "world", "a": "hello", "n": 3, "flag": True})
        assert q == ("hello world", "args")

    def test_underscore_keys_excluded_from_fallback(self):
        assert derive_query({"_private": "secret hint", "q": "real"}) == ("real", "args")

    def test_no_signal_returns_none(self):
        assert derive_query(None) is None
        assert derive_query({}) is None
        assert derive_query({"n": 3, "flag": False}) is None
        assert derive_query({"blank": "   "}) is None

    def test_query_is_capped(self):
        q = derive_query({"text": "x" * 10_000})
        assert q is not None and len(q[0]) == 512


# ── ToolRelevanceRanker ──────────────────────────────────────────────────


class TestRanker:
    def test_relevant_tool_ranks_first(self):
        ranked = ToolRelevanceRanker().rank("send a slack message to the channel", CANDIDATES)
        assert ranked[0]["tool"] == "test__send_message"
        assert ranked[0]["rank"] == 1
        assert ranked[0]["relevance_score"] > ranked[-1]["relevance_score"]

    def test_byte_identical_across_runs(self):
        a = ToolRelevanceRanker().rank("read the config file", CANDIDATES)
        b = ToolRelevanceRanker().rank("read the config file", CANDIDATES)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def test_no_match_ties_break_alphabetically_not_by_discovery_order(self):
        # Query shares no token with any candidate → all scores equal (0).
        ranked = ToolRelevanceRanker().rank("zzz qqq", list(reversed(CANDIDATES)))
        assert [r["tool"] for r in ranked] == [
            "test__create_issue",
            "test__read_file",
            "test__send_message",
        ]

    def test_top_n_caps_output(self):
        ranked = ToolRelevanceRanker(top_n=2).rank("file", CANDIDATES)
        assert len(ranked) == 2
        assert [r["rank"] for r in ranked] == [1, 2]

    def test_entry_shape_pinned(self):
        """Per-candidate key set is replay contract — a change here must be
        a deliberate decision, mirrored in docs/selection-telemetry.md."""
        ranked = ToolRelevanceRanker().rank("slack", CANDIDATES)
        for entry in ranked:
            assert set(entry) == RANKED_ENTRY_KEYS
            assert entry["risk_penalty"] == 0.0
            assert entry["final_score"] == entry["relevance_score"]

    def test_empty_candidates(self):
        assert ToolRelevanceRanker().rank("anything", []) == []


# ── build_candidate_features ─────────────────────────────────────────────


class TestFeatures:
    def test_shape_and_hash(self):
        ranked = ToolRelevanceRanker().rank("slack", CANDIDATES)
        feats = build_candidate_features("send to slack", "context_query", ranked)
        assert set(feats) == FEATURES_KEYS
        assert feats["query_sha256"] == hashlib.sha256(b"send to slack").hexdigest()
        assert feats["query_chars"] == len("send to slack")

    def test_raw_query_never_in_features(self):
        feats = build_candidate_features("hunter2 secret task", "context_query", [])
        assert "hunter2" not in json.dumps(feats)


# ── ProxyManager wire-in ─────────────────────────────────────────────────


def _make_result(text: str):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], isError=False)


def _make_manager(
    tmp_path: Path, *, relevance_enabled: bool = True
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
        tool_relevance=ToolRelevanceConfig(enabled=relevance_enabled),
    )
    log = SelectionTelemetryLog(tmp_path / "log.jsonl")
    log.initialize()
    mgr = ProxyManager(proxy_cfg, TokenTracker(), selection_log=log)

    tools = [
        SimpleNamespace(
            name="send_message",
            description="Send a message to a Slack channel",
            inputSchema={"type": "object", "properties": {"channel": {"type": "string"}}},
        ),
        SimpleNamespace(
            name="read_file",
            description="Read a file from the local filesystem",
            inputSchema={"type": "object"},
        ),
    ]
    session = AsyncMock()
    session.call_tool.return_value = _make_result("ok!")
    mgr._connections["srv"] = UpstreamConnection(
        name="srv", config=server_cfg, session=session, tools=tools
    )
    mgr.get_proxy_tools()  # advertise → snapshot names + infos
    return mgr, log


def _events(log: SelectionTelemetryLog) -> list[dict]:
    return [
        json.loads(line)
        for line in log.path.read_text(encoding="utf-8").splitlines()
        if line
    ]


class TestManagerWireIn:
    async def test_ranked_call_populates_features_and_version_on_both_events(self, tmp_path):
        mgr, log = _make_manager(tmp_path)
        query = "send a slack message to the alerts channel"
        await mgr.call_tool("srv", "send_message", {"_context_query": query})

        selection, execution = _events(log)
        assert selection["ranker_version"] == RANKER_VERSION_BM25
        assert execution["ranker_version"] == RANKER_VERSION_BM25
        feats = selection["candidate_features"]
        assert set(feats) == FEATURES_KEYS
        assert feats["query_source"] == "context_query"
        assert feats["query_sha256"] == hashlib.sha256(query.encode()).hexdigest()
        assert feats["ranked_candidates"][0]["tool"] == "test__send_message"
        assert {r["tool"] for r in feats["ranked_candidates"]} == {
            "test__send_message",
            "test__read_file",
        }
        # Privacy: the raw query text exists nowhere in the log file.
        assert query not in log.path.read_text(encoding="utf-8")
        # Exposure unchanged: ranking never reorders the advertised snapshot.
        assert selection["candidate_tools"] == ["test__send_message", "test__read_file"]

    async def test_no_query_signal_keeps_unranked_baseline(self, tmp_path):
        mgr, log = _make_manager(tmp_path)
        await mgr.call_tool("srv", "read_file", {})

        selection, execution = _events(log)
        assert selection["candidate_features"] is None
        assert selection["ranker_version"] == "v0-passthrough"
        assert execution["ranker_version"] == "v0-passthrough"

    async def test_disabled_config_keeps_unranked_baseline(self, tmp_path):
        mgr, log = _make_manager(tmp_path, relevance_enabled=False)
        await mgr.call_tool("srv", "read_file", {"_context_query": "read the file"})

        selection, _ = _events(log)
        assert selection["candidate_features"] is None
        assert selection["ranker_version"] == "v0-passthrough"

    async def test_upstream_error_still_mirrors_ranker_version(self, tmp_path):
        import pytest

        mgr, log = _make_manager(tmp_path)
        mgr._connections["srv"].session.call_tool.side_effect = RuntimeError("boom")

        with pytest.raises(Exception):
            await mgr.call_tool("srv", "send_message", {"_context_query": "send slack"})

        selection, execution = _events(log)
        assert execution["ok"] is False
        assert selection["ranker_version"] == execution["ranker_version"] == RANKER_VERSION_BM25
