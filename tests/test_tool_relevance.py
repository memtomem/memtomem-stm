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
    PENALTY_SOURCE_BOTH,
    PENALTY_SOURCE_GRAPH,
    PENALTY_SOURCE_NONE,
    PENALTY_SOURCE_REVIEW,
    RANKER_VERSION_BM25,
    ToolRelevanceRanker,
    build_candidate_features,
    compose_risk_penalty,
    derive_query,
    penalty_source,
)

FEATURES_KEYS = {"query_source", "query_sha256", "query_chars", "ranked_candidates"}
RANKED_ENTRY_KEYS = {
    "tool",
    "rank",
    "relevance_score",
    "risk_penalty",
    "risk_penalty_source",
    "final_score",
}


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
            assert entry["risk_penalty_source"] == PENALTY_SOURCE_NONE
            assert entry["final_score"] == entry["relevance_score"]

    def test_empty_candidates(self):
        assert ToolRelevanceRanker().rank("anything", []) == []

    def test_risk_penalty_demotes_by_final_score(self):
        """#465 review-profile demotion: order follows final_score =
        relevance * (1 - penalty), and both inputs stay in the record."""
        query = "send a slack message to the channel"
        baseline = ToolRelevanceRanker().rank(query, CANDIDATES)
        assert baseline[0]["tool"] == "test__send_message"

        ranked = ToolRelevanceRanker().rank(
            query, CANDIDATES, risk_penalties={"test__send_message": 1.0}
        )
        flagged = next(r for r in ranked if r["tool"] == "test__send_message")
        assert flagged["rank"] > 1  # fully penalized → sinks below the rest
        assert flagged["risk_penalty"] == 1.0
        assert flagged["final_score"] == 0.0
        assert flagged["relevance_score"] == baseline[0]["relevance_score"]
        # Unflagged candidates are untouched: penalty 0, final == relevance.
        for entry in ranked:
            if entry["tool"] != "test__send_message":
                assert entry["risk_penalty"] == 0.0
                assert entry["final_score"] == entry["relevance_score"]

    def test_zero_penalty_map_is_identical_to_no_map(self):
        query = "read the config file"
        a = ToolRelevanceRanker().rank(query, CANDIDATES)
        b = ToolRelevanceRanker().rank(query, CANDIDATES, risk_penalties={})
        c = ToolRelevanceRanker().rank(query, CANDIDATES, risk_penalties={"test__read_file": 0.0})
        assert json.dumps(a) == json.dumps(b) == json.dumps(c)

    def test_risk_penalty_source_echoed_into_record(self):
        """The provenance tag rides each record verbatim; absent tools = none."""
        query = "send a slack message to the channel"
        ranked = ToolRelevanceRanker().rank(
            query,
            CANDIDATES,
            risk_penalties={"test__send_message": 0.4, "test__read_file": 0.5},
            risk_penalty_sources={
                "test__send_message": PENALTY_SOURCE_GRAPH,
                "test__read_file": PENALTY_SOURCE_BOTH,
            },
        )
        by_tool = {r["tool"]: r for r in ranked}
        assert by_tool["test__send_message"]["risk_penalty_source"] == PENALTY_SOURCE_GRAPH
        assert by_tool["test__read_file"]["risk_penalty_source"] == PENALTY_SOURCE_BOTH
        # A tool with no source entry records "none" even if a stray penalty
        # would not apply (default-safe).
        assert by_tool["test__create_issue"]["risk_penalty_source"] == PENALTY_SOURCE_NONE


# ── compose_risk_penalty / penalty_source (#493) ──────────────────────────


class TestComposeRiskPenalty:
    def test_complement_product_stacks_two_demotions(self):
        # 1 - (1-0.5)(1-0.4) = 1 - 0.30 = 0.70
        assert compose_risk_penalty(0.5, 0.4) == 0.7

    def test_degenerates_to_single_nonzero_input(self):
        assert compose_risk_penalty(0.0, 0.4) == 0.4
        assert compose_risk_penalty(0.6, 0.0) == 0.6
        assert compose_risk_penalty(0.0, 0.0) == 0.0

    def test_commutative(self):
        assert compose_risk_penalty(0.3, 0.7) == compose_risk_penalty(0.7, 0.3)

    def test_stays_in_unit_interval(self):
        # Either input at the 1.0 ceiling saturates the combined penalty to 1.0
        # (the ranker zeroes final_score — max demotion, still advertised).
        assert compose_risk_penalty(1.0, 0.4) == 1.0
        assert compose_risk_penalty(0.4, 1.0) == 1.0
        assert 0.0 <= compose_risk_penalty(0.99, 0.99) <= 1.0


class TestPenaltySource:
    def test_tags_each_combination(self):
        assert penalty_source(0.0, 0.0) == PENALTY_SOURCE_NONE
        assert penalty_source(0.5, 0.0) == PENALTY_SOURCE_REVIEW
        assert penalty_source(0.0, 0.4) == PENALTY_SOURCE_GRAPH
        assert penalty_source(0.5, 0.4) == PENALTY_SOURCE_BOTH


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
    return [json.loads(line) for line in log.path.read_text(encoding="utf-8").splitlines() if line]


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
