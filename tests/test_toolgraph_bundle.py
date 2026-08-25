"""Portable Toolgraph bundle parsing and gateway enforcement tests."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator
from mcp.server.mcpserver.exceptions import ToolError

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ExposureConfig,
    ExposureProfile,
    ProxyConfig,
    ToolgraphConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, ToolgraphStartupError, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.tool_eligibility import (
    REASON_TOOLGRAPH_DRIFTED,
    REASON_TOOLGRAPH_UNMAPPED,
)
from memtomem_stm.proxy import toolgraph_bundle as toolgraph_bundle_mod
from memtomem_stm.proxy.toolgraph_bundle import (
    PolicyBundleError,
    bundle_provenance_warnings,
    canonical_json_bytes,
    parse_policy_bundle,
    tool_contract_digest,
)
from memtomem_stm.cli.proxy import cli
from memtomem_stm.cli import proxy as proxy_cli
from memtomem_stm.proxy import manager as manager_mod


def _tool(name: str = "read", *, description: str = "Read data") -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=description,
        input_schema={"type": "object"},
        annotations=SimpleNamespace(
            read_only_hint=True,
            destructive_hint=None,
            idempotent_hint=None,
            open_world_hint=None,
        ),
    )


def _bundle(tool: SimpleNamespace, *, profile: str = "strict", decision: str = "eligible"):
    digest = tool_contract_digest(
        server="graph-srv",
        name=tool.name,
        description=tool.description,
        input_schema=tool.input_schema,
        annotations=tool.annotations,
    )
    row = {
        "tool_key": f"graph-srv::{tool.name}",
        "tool_contract_digest": digest,
        "decision": decision,
        "risk_score": 0.8,
    }
    if decision == "rejected":
        row["reason"] = "DENY_VIOLATION"
    return {
        "schema_version": 1,
        "kind": "toolgraph.policy-bundle",
        "created_at": "2026-07-14T00:00:00+00:00",
        "graph_state": {"instance_id": "graph-1", "generation": 7},
        "agent": "stm-proxy",
        "agent_found": True,
        "profile": profile,
        "governance_digest": "a" * 64,
        "catalog_digest": "b" * 64,
        "tools": [row],
    }


def _schema_errors(doc: dict) -> list:
    schema = json.loads(
        files("memtomem_stm.data").joinpath("policy-bundle.schema.json").read_text(encoding="utf-8")
    )
    return list(Draft202012Validator(schema).iter_errors(doc))


def _schema_rejects(doc: dict, *, validator: str, at: list, names: str) -> bool:
    """True when the schema rejects ``doc`` for exactly the intended reason.

    Pinning the keyword, the location, AND the field named in the message keeps a
    conformance assertion from passing on some unrelated error that happens to
    share a validator keyword.
    """
    return any(
        error.validator == validator and list(error.absolute_path) == at and names in error.message
        for error in _schema_errors(doc)
    )


def _parse(doc: dict, *, profile: str = "strict"):
    return parse_policy_bundle(
        canonical_json_bytes(doc), expected_agent="stm-proxy", expected_profile=profile
    )


def _write_bundle(path: Path, doc: dict) -> None:
    temporary = path.with_suffix(".next")
    temporary.write_bytes(canonical_json_bytes(doc))
    temporary.replace(path)


def _manager(tmp_path: Path, *, profile: ExposureProfile = ExposureProfile.STRICT):
    bundle_path = tmp_path / "policy-bundle.json"
    upstream = UpstreamServerConfig(prefix="srv", compression=CompressionStrategy.NONE)
    config = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": upstream},
        exposure=ExposureConfig(profile=profile),
        toolgraph=ToolgraphConfig(
            enabled=True,
            source="bundle",
            bundle_path=bundle_path,
            agent_id="stm-proxy",
            query_profile=profile.value,
            server_name_map={"srv": "graph-srv"},
        ),
    )
    manager = ProxyManager(config, TokenTracker())
    live_tool = _tool()
    session = AsyncMock()
    manager._connections["srv"] = UpstreamConnection(
        name="srv", config=upstream, session=session, tools=[live_tool]
    )
    return manager, bundle_path, live_tool


def test_scale_zero_records_no_bundle_penalty(tmp_path: Path):
    """``risk_penalty_scale: 0`` turns demotion off, so the map must stay empty.

    A stored ``0.0`` changes no score, but ``risk_penalty_count`` would then
    report demotions in the exact configuration that disabled them. The stdio
    consult already filtered these; the bundle path did not.
    """
    manager, bundle_path, live_tool = _manager(tmp_path)
    manager._config.toolgraph.risk_penalty_scale = 0.0
    bundle_path.write_text(json.dumps(_bundle(live_tool)), encoding="utf-8")
    manager._refresh_toolgraph_bundle(startup=True)
    assert manager._toolgraph_risk_penalties == {}
    assert manager.get_toolgraph_status()["risk_penalty_count"] == 0
    # Positive control: the same bundle DOES penalize at the default scale.
    manager._config.toolgraph.risk_penalty_scale = 1.0
    manager._apply_toolgraph_policy_snapshot(manager._toolgraph_policy_snapshot)
    assert manager._toolgraph_risk_penalties == {("srv", live_tool.name): 0.8}


def test_parser_accepts_additive_fields_and_pins_exact_bytes():
    doc = _bundle(_tool())
    doc["future"] = {"safe": True}
    payload = canonical_json_bytes(doc)
    snapshot = parse_policy_bundle(payload, expected_agent="stm-proxy", expected_profile="strict")
    assert snapshot.instance_id == "graph-1"
    assert snapshot.generation == 7
    assert len(snapshot.bundle_digest) == 64


def test_toolgraph_golden_fixtures_pin_cross_repo_bytes_and_digest():
    root = files("memtomem_stm.data").joinpath("toolgraph-contract-v1")
    fixture = json.loads(root.joinpath("tool-contract-v1.json").read_text(encoding="utf-8"))
    contract = fixture["contract"]
    annotations = SimpleNamespace(
        read_only_hint=contract["read_only_hint"],
        destructive_hint=contract["destructive_hint"],
        idempotent_hint=contract["idempotent_hint"],
        open_world_hint=contract["open_world_hint"],
    )
    digest = tool_contract_digest(
        server=contract["server"],
        name=contract["name"],
        description=contract["description"],
        input_schema=contract["input_schema"],
        annotations=annotations,
    )
    assert digest == fixture["expected_digest"]

    payload = root.joinpath("policy-bundle-v1.json").read_bytes()
    expected_bundle_digest = root.joinpath("policy-bundle-v1.sha256").read_text().strip()
    snapshot = parse_policy_bundle(
        payload, expected_agent="fixture-agent", expected_profile="review"
    )
    assert snapshot.bundle_digest == expected_bundle_digest
    assert snapshot.decisions["도구-서버::publish"].contract_digest == digest
    document = json.loads(payload)
    assert document["fixture_extension"] == {"accepted": True}
    assert canonical_json_bytes(document) == payload


def test_toolgraph_rejected_golden_fixture_pins_reason_paths_and_exact_bytes():
    root = files("memtomem_stm.data").joinpath("toolgraph-contract-v1")
    payload = root.joinpath("policy-bundle-v1-rejected.json").read_bytes()
    expected = root.joinpath("policy-bundle-v1-rejected.sha256").read_text().strip()
    snapshot = parse_policy_bundle(
        payload, expected_agent="fixture-agent", expected_profile="strict"
    )
    assert snapshot.bundle_digest == expected
    decision = snapshot.decisions["도구-서버::publish"]
    assert decision.decision == "rejected"
    assert decision.reason == "DENY_VIOLATION"
    assert decision.reject_code == "toolgraph_deny_violation"
    document = json.loads(payload)
    assert document["tools"][0]["paths"] == [
        "(fixture-agent)-[:CAN_CALL]->(도구-서버::publish)-[:WRITES]->"
        "(file:///demo/drafts)-[:GOVERNED_BY]->(draft-publish-deny)"
    ]
    assert canonical_json_bytes(document) == payload


def test_vendored_contract_schema_accepts_reference_bundle():
    assert _schema_errors(_bundle(_tool())) == []
    rejected = json.loads(
        files("memtomem_stm.data")
        .joinpath("toolgraph-contract-v1", "policy-bundle-v1-rejected.json")
        .read_text(encoding="utf-8")
    )
    assert _schema_errors(rejected) == []


class TestSchemaParserConformance:
    """The vendored schema and the hand-written parser deliberately diverge.

    Equivalence relation: this matrix covers DOCUMENT-INTRINSIC constraints only.
    The parser also rejects an ``agent``/``profile`` that does not match the
    caller's ``expected_*`` — that is caller-context binding the schema cannot
    express, not a divergence, so it is out of scope here.

    | mutation                            | schema | parser |
    |-------------------------------------|--------|--------|
    | created_at missing / wrong type      | reject | accept |
    | risk_score key absent                | reject | accept |
    | unknown reason on a rejected row     | reject | accept |
    | non-string reason on an eligible row | reject | accept |
    | extra key in graph_state             | reject | accept |
    | profile outside the enum             | reject | accept |
    | paths wrong type (outer or item)     | reject | accept |
    | duplicate tool_key, exact-copy rows  | reject | reject |
    | duplicate tool_key, differing rows   | accept | reject |
    | graph_state.generation as 1.0        | accept | reject |

    Since toolgraph#41 the parser-tolerant rows split three ways:

    - **Contract-blessed** — the schema's MAY-ignore prose now covers tolerating
      ``created_at`` (any state), an absent ``risk_score``, an unknown reason on
      a rejected row (generic mapping via
      ``tool_eligibility._TOOLGRAPH_REASON_MAP``), a non-string reason on an
      eligible row, and ``paths`` in any state.
    - **Deliberate local design, not blessed** — a free-string profile
      (``ToolgraphConfig.query_profile``); the schema enum is unconditional.
    - **Local leniency, not blessed** — extra ``graph_state`` keys; the schema
      declares that object closed by design (extending it is a schema_version
      bump), so this parser is simply more tolerant than the contract asks.

    Duplicate ``tool_key`` is likewise no longer a divergence in substance. The
    contract now declares such a bundle invalid and says consumers MUST NOT
    arbitrate between conflicting rows, so rejecting is conforming — the schema
    simply cannot express the rule for differing rows (``uniqueItems`` compares
    whole items), which is why the prose carries it and this row still reads
    accept/reject.

    That leaves ``generation: 1.0`` as the one live availability risk: a
    schema-valid bundle is still withheld wholesale. Only a lexical rule could
    close it, so the contract documents the producer's int-literal obligation
    instead.

    Every test asserts BOTH verdicts, so a change on either side of the vendored
    contract flips a test rather than drifting silently.
    """

    def test_created_at_violations_fail_schema_but_parse(self):
        missing = _bundle(_tool())
        del missing["created_at"]
        assert _schema_rejects(missing, validator="required", at=[], names="created_at")
        assert _parse(missing).instance_id == "graph-1"

        mistyped = _bundle(_tool())
        mistyped["created_at"] = 123
        assert _schema_rejects(mistyped, validator="type", at=["created_at"], names="string")
        assert _parse(mistyped).instance_id == "graph-1"

    def test_absent_risk_score_fails_schema_but_parses_as_none(self):
        doc = _bundle(_tool())
        del doc["tools"][0]["risk_score"]
        assert _schema_rejects(doc, validator="required", at=["tools", 0], names="risk_score")
        assert _parse(doc).decisions["graph-srv::read"].risk_score is None

    def test_unknown_reject_reason_fails_schema_but_maps_to_generic_code(self):
        doc = _bundle(_tool(), decision="rejected")
        doc["tools"][0]["reason"] = "SOME_FUTURE_REASON"
        assert _schema_rejects(
            doc, validator="enum", at=["tools", 0, "reason"], names="SOME_FUTURE_REASON"
        )
        decision = _parse(doc).decisions["graph-srv::read"]
        assert decision.reason == "SOME_FUTURE_REASON"
        assert decision.reject_code == "toolgraph_rejected"

    def test_non_string_reason_on_eligible_row_fails_schema_but_normalizes_to_none(self):
        doc = _bundle(_tool())
        doc["tools"][0]["reason"] = 123
        assert _schema_rejects(doc, validator="enum", at=["tools", 0, "reason"], names="123")
        assert _parse(doc).decisions["graph-srv::read"].reason is None

    def test_extra_graph_state_key_fails_schema_but_parses(self):
        doc = _bundle(_tool())
        doc["graph_state"]["region"] = "kr"
        assert _schema_rejects(
            doc, validator="additionalProperties", at=["graph_state"], names="region"
        )
        assert _parse(doc).generation == 7

    def test_out_of_enum_profile_fails_schema_but_parses_when_expected(self):
        doc = _bundle(_tool(), profile="custom")
        assert _schema_rejects(doc, validator="enum", at=["profile"], names="custom")
        assert _parse(doc, profile="custom").profile == "custom"

    def test_paths_type_violations_fail_schema_but_parse(self):
        outer = _bundle(_tool())
        outer["tools"][0]["paths"] = 42
        assert _schema_rejects(outer, validator="type", at=["tools", 0, "paths"], names="array")
        assert _parse(outer).instance_id == "graph-1"

        item = _bundle(_tool())
        item["tools"][0]["paths"] = [123]
        assert _schema_rejects(item, validator="type", at=["tools", 0, "paths", 0], names="string")
        assert _parse(item).instance_id == "graph-1"

    def test_exact_duplicate_rows_now_fail_schema_and_parser(self):
        """The ``uniqueItems`` backstop (toolgraph#41) covers only this case."""
        doc = _bundle(_tool())
        doc["tools"].append(dict(doc["tools"][0]))
        assert _schema_rejects(doc, validator="uniqueItems", at=["tools"], names="non-unique")
        with pytest.raises(PolicyBundleError, match="duplicate tool_key"):
            _parse(doc)

    def test_duplicate_tool_key_passes_schema_but_parser_rejects(self):
        """uniqueItems could not catch this either: the rows differ.

        Rejecting is contract-conforming — the bundle is invalid per the
        ``tools`` prose — but the schema alone cannot say so, which is exactly
        why the uniqueness rule is normative prose rather than a keyword.
        """
        doc = _bundle(_tool())
        twin = dict(doc["tools"][0])
        twin["risk_score"] = 0.2
        doc["tools"].append(twin)
        assert _schema_errors(doc) == []
        with pytest.raises(PolicyBundleError, match="duplicate tool_key"):
            _parse(doc)

    def test_float_generation_passes_schema_but_parser_rejects(self):
        """Draft 2020-12 reads a mathematically integral 1.0 as an integer."""
        doc = _bundle(_tool())
        doc["graph_state"]["generation"] = 1.0
        assert _schema_errors(doc) == []
        with pytest.raises(PolicyBundleError, match="generation"):
            _parse(doc)


@pytest.mark.parametrize("version", [0, 2, "1", True])
def test_parser_rejects_unknown_schema_versions(version):
    doc = _bundle(_tool())
    doc["schema_version"] = version
    with pytest.raises(PolicyBundleError, match="schema_version"):
        parse_policy_bundle(
            canonical_json_bytes(doc), expected_agent="stm-proxy", expected_profile="strict"
        )


@pytest.mark.parametrize("tool_key", ["server", "::tool", "server::", "a:b::c", "a::b:c"])
def test_parser_matches_schema_for_qualified_tool_keys(tool_key):
    doc = _bundle(_tool())
    doc["tools"][0]["tool_key"] = tool_key
    with pytest.raises(PolicyBundleError, match="tool_key must be server-qualified"):
        parse_policy_bundle(
            canonical_json_bytes(doc), expected_agent="stm-proxy", expected_profile="strict"
        )


@pytest.mark.asyncio
async def test_strict_list_and_direct_call_use_same_denial(tmp_path):
    manager, path, tool = _manager(tmp_path)
    _write_bundle(path, _bundle(tool, decision="rejected"))
    manager._refresh_toolgraph_bundle(force=True, startup=True)

    assert manager.get_proxy_tools() == []
    with pytest.raises(ToolError, match="toolgraph_deny_violation"):
        await manager.call_tool("srv", "read", {})


@pytest.mark.asyncio
async def test_strict_call_gate_runs_before_pipeline_and_reloads_atomically(tmp_path):
    manager, path, tool = _manager(tmp_path)
    _write_bundle(path, _bundle(tool))
    manager._refresh_toolgraph_bundle(force=True, startup=True)
    initial_stamp = manager._toolgraph_bundle_stamp
    initial_digest = manager._toolgraph_bundle_digest
    manager._call_tool_guarded = AsyncMock(return_value=("ok", False))
    assert await manager.call_tool("srv", "read", {}) == "ok"

    _write_bundle(path, _bundle(tool, decision="rejected"))
    with pytest.raises(ToolError, match="toolgraph_deny_violation"):
        await manager.call_tool("srv", "read", {})
    # Runs on Windows CI too: replacement must change the observed stamp and
    # adopt the new exact-byte digest before the denial reaches the call gate.
    assert manager._toolgraph_bundle_stamp != initial_stamp
    assert manager._toolgraph_bundle_digest != initial_digest
    manager._call_tool_guarded.assert_awaited_once()


@pytest.mark.asyncio
async def test_unchanged_bundle_rebinds_once_after_live_catalog_change(tmp_path, monkeypatch):
    manager, path, tool = _manager(tmp_path)
    _write_bundle(path, _bundle(tool))
    manager._refresh_toolgraph_bundle(force=True, startup=True)
    digest_spy = Mock(wraps=manager_mod.tool_contract_digest)
    monkeypatch.setattr(manager_mod, "tool_contract_digest", digest_spy)
    manager._connections["srv"].session.list_tools.return_value = SimpleNamespace(
        tools=[tool, _tool("new_tool")]
    )
    await manager._refresh_server_tools("srv")
    manager._call_tool_guarded = AsyncMock(return_value=("unexpected", False))

    with pytest.raises(ToolError, match="toolgraph_unmapped"):
        await manager.call_tool("srv", "new_tool", {})
    assert digest_spy.call_count == 1
    with pytest.raises(ToolError, match="toolgraph_unmapped"):
        await manager.call_tool("srv", "new_tool", {})
    assert digest_spy.call_count == 1
    manager._call_tool_guarded.assert_not_awaited()


@pytest.mark.asyncio
async def test_unchanged_bundle_does_not_rehash_catalog_on_list_or_call(tmp_path, monkeypatch):
    manager, path, tool = _manager(tmp_path)
    _write_bundle(path, _bundle(tool))
    manager._refresh_toolgraph_bundle(force=True, startup=True)
    digest_spy = Mock(wraps=manager_mod.tool_contract_digest)
    monkeypatch.setattr(manager_mod, "tool_contract_digest", digest_spy)
    manager._call_tool_guarded = AsyncMock(return_value=("ok", False))

    assert [item.original_name for item in manager.get_proxy_tools()] == ["read"]
    assert await manager.call_tool("srv", "read", {}) == "ok"
    assert await manager.call_tool("srv", "read", {}) == "ok"
    digest_spy.assert_not_called()


@pytest.mark.asyncio
async def test_review_keeps_tool_and_records_would_block(tmp_path):
    manager, path, tool = _manager(tmp_path, profile=ExposureProfile.REVIEW)
    _write_bundle(path, _bundle(tool, profile="review", decision="rejected"))
    manager._refresh_toolgraph_bundle(force=True, startup=True)
    manager._call_tool_guarded = AsyncMock(return_value=("ok", False))

    assert [item.original_name for item in manager.get_proxy_tools()] == ["read"]
    assert await manager.call_tool("srv", "read", {}) == "ok"
    assert manager.get_toolgraph_status()["would_block_calls"] == 1

    path.write_text("broken", encoding="utf-8")
    assert await manager.call_tool("srv", "read", {}) == "ok"
    status = manager.get_toolgraph_status()
    assert status["using_last_known_good"] is True
    assert status["would_block_calls"] == 2


@pytest.mark.asyncio
async def test_explore_bundle_rejection_is_non_enforcing(tmp_path):
    manager, path, tool = _manager(tmp_path, profile=ExposureProfile.EXPLORE)
    _write_bundle(path, _bundle(tool, profile="explore", decision="rejected"))
    manager._refresh_toolgraph_bundle(force=True, startup=True)
    manager._call_tool_guarded = AsyncMock(return_value=("ok", False))

    assert [item.original_name for item in manager.get_proxy_tools()] == ["read"]
    assert await manager.call_tool("srv", "read", {}) == "ok"
    assert manager.get_toolgraph_status()["would_block_calls"] == 0


@pytest.mark.asyncio
async def test_start_drops_stale_bundle_state_when_toolgraph_is_disabled(tmp_path):
    config = ProxyConfig(config_path=tmp_path / "missing.json", upstream_servers={})
    manager = ProxyManager(config, TokenTracker())
    await manager.start()
    manager._toolgraph_external_rejects = {("old", "tool"): "stale"}
    manager._toolgraph_risk_penalties = {("old", "tool"): 0.9}
    manager._toolgraph_withhold_all = "stale"
    manager._graph_generation = 99
    manager._toolgraph_degraded = True
    manager._toolgraph_degraded_reason = "stale"
    manager._toolgraph_policy_snapshot = object()
    manager._toolgraph_bundle_stamp = (1, 2, 3)
    manager._toolgraph_bundle_digest = "a" * 64
    manager._graph_instance_id = "old-instance"
    manager._toolgraph_would_block_calls = 7
    manager._tool_catalog_revision = 4
    manager._toolgraph_bound_catalog_revision = 4

    await manager.start()

    assert manager._toolgraph_external_rejects == {}
    assert manager._toolgraph_risk_penalties == {}
    assert manager._toolgraph_withhold_all is None
    assert manager._graph_generation is None
    assert manager._toolgraph_degraded is False
    assert manager._toolgraph_degraded_reason is None
    assert manager._toolgraph_policy_snapshot is None
    assert manager._toolgraph_bundle_stamp is None
    assert manager._toolgraph_bundle_digest is None
    assert manager._graph_instance_id is None
    assert manager._toolgraph_would_block_calls == 0
    assert manager._tool_catalog_revision == 0
    assert manager._toolgraph_bound_catalog_revision is None
    await manager.stop()


@pytest.mark.asyncio
async def test_review_rebinds_last_known_good_once_when_catalog_changes(tmp_path, monkeypatch):
    manager, path, tool = _manager(tmp_path, profile=ExposureProfile.REVIEW)
    _write_bundle(path, _bundle(tool, profile="review"))
    manager._refresh_toolgraph_bundle(force=True, startup=True)
    path.write_text("broken", encoding="utf-8")
    digest_spy = Mock(wraps=manager_mod.tool_contract_digest)
    monkeypatch.setattr(manager_mod, "tool_contract_digest", digest_spy)
    manager._connections["srv"].session.list_tools.return_value = SimpleNamespace(
        tools=[tool, _tool("new_tool")]
    )
    await manager._refresh_server_tools("srv")
    manager._call_tool_guarded = AsyncMock(return_value=("ok", False))

    assert await manager.call_tool("srv", "new_tool", {}) == "ok"
    assert manager._toolgraph_external_rejects[("srv", "new_tool")] == REASON_TOOLGRAPH_UNMAPPED
    assert digest_spy.call_count == 1
    assert await manager.call_tool("srv", "new_tool", {}) == "ok"
    assert digest_spy.call_count == 1


def test_missing_and_contract_drift_are_fail_closed(tmp_path):
    manager, path, tool = _manager(tmp_path)
    missing = _bundle(tool)
    missing["tools"] = []
    _write_bundle(path, missing)
    manager._refresh_toolgraph_bundle(force=True, startup=True)
    assert manager._toolgraph_external_rejects[("srv", "read")] == REASON_TOOLGRAPH_UNMAPPED

    drifted = _bundle(tool)
    drifted["tools"][0]["tool_contract_digest"] = "c" * 64
    _write_bundle(path, drifted)
    manager._refresh_toolgraph_bundle(force=True, startup=True)
    assert manager._toolgraph_external_rejects[("srv", "read")] == REASON_TOOLGRAPH_DRIFTED


def test_invalid_startup_fails_strict_but_review_degrades(tmp_path):
    strict, strict_path, _ = _manager(tmp_path / "strict")
    strict_path.parent.mkdir(parents=True)
    strict_path.write_text("not json", encoding="utf-8")
    with pytest.raises(ToolgraphStartupError, match="Invalid Toolgraph policy bundle"):
        strict._refresh_toolgraph_bundle(force=True, startup=True)

    review, review_path, _ = _manager(tmp_path / "review", profile=ExposureProfile.REVIEW)
    review_path.parent.mkdir(parents=True)
    review_path.write_text("not json", encoding="utf-8")
    review._refresh_toolgraph_bundle(force=True, startup=True)
    status = review.get_toolgraph_status()
    assert status["degraded"] is True
    assert status["withholding_all"] is None


def test_unexpected_refresh_error_fails_strict_but_review_degrades(tmp_path, monkeypatch):
    # #866: only OSError/PolicyBundleError were caught around the bundle
    # reload, so any other exception class escaping it — here an internal
    # loader crash on a well-formed bundle — aborted the whole MCP server at
    # startup instead of degrading the toolgraph feature. It must ride the same
    # semantics as the expected ones: strict fails startup loudly with
    # ToolgraphStartupError, review degrades with a logged fault.
    def _boom(*args, **kwargs):
        raise RuntimeError("unexpected loader crash")

    monkeypatch.setattr(manager_mod, "load_policy_bundle", _boom)

    strict, strict_path, tool = _manager(tmp_path / "strict")
    strict_path.parent.mkdir(parents=True)
    _write_bundle(strict_path, _bundle(tool))
    # "reload failed", not "invalid bundle": the artifact here is well-formed,
    # so naming it invalid would send the operator to republish a good file.
    with pytest.raises(ToolgraphStartupError, match="policy bundle reload failed"):
        strict._refresh_toolgraph_bundle(force=True, startup=True)

    review, review_path, tool = _manager(tmp_path / "review", profile=ExposureProfile.REVIEW)
    review_path.parent.mkdir(parents=True)
    _write_bundle(review_path, _bundle(tool))
    review._refresh_toolgraph_bundle(force=True, startup=True)
    status = review.get_toolgraph_status()
    assert status["degraded"] is True
    assert status["withholding_all"] is None


def test_unexpected_runtime_refresh_error_does_not_crash_the_call_gate(tmp_path, monkeypatch):
    # The same reload runs on every proxied tools/call
    # (_enforce_toolgraph_call_policy) and each advertisement build; an
    # unexpected exception class there crashed the in-flight request. It must
    # degrade instead — and under the strict profile fail closed (withhold,
    # ToolError) rather than crash.
    manager, path, tool = _manager(tmp_path)
    _write_bundle(path, _bundle(tool))
    manager._refresh_toolgraph_bundle(force=True, startup=True)  # healthy load first

    def _boom(*args, **kwargs):
        raise RuntimeError("unexpected loader crash")

    monkeypatch.setattr(manager_mod, "load_policy_bundle", _boom)
    # Deterministic stamp invalidation: touch() depends on filesystem mtime
    # granularity; an explicit +1s utime always changes st_mtime_ns.
    before = path.stat()
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 10**9))
    with pytest.raises(ToolError, match="Policy denied"):
        manager._enforce_toolgraph_call_policy("srv", "read")
    assert manager.get_toolgraph_status()["withholding_all"] is not None


def test_unexpanded_user_bundle_path_rejects_instead_of_escaping(tmp_path):
    # Path.expanduser() raises RuntimeError (not OSError) for ``~nosuchuser``
    # on POSIX; that ran OUTSIDE the reload barrier, so review startup still
    # died with the raw exception. Both profiles must take the rejection
    # semantics. (On Windows expanduser resolves without checking the user
    # exists, and the subsequent stat() fails with OSError — the asserted
    # outcomes are identical either way.)
    strict, _, _ = _manager(tmp_path / "strict")
    strict._config.toolgraph.bundle_path = Path("~mms-no-such-user-866/bundle.json")
    # The summary differs by platform (POSIX RuntimeError -> "reload failed";
    # Windows OSError -> "Invalid ... bundle"), so pin only the shared stem.
    with pytest.raises(ToolgraphStartupError, match="Toolgraph policy bundle"):
        strict._refresh_toolgraph_bundle(force=True, startup=True)

    review, _, _ = _manager(tmp_path / "review", profile=ExposureProfile.REVIEW)
    review._config.toolgraph.bundle_path = Path("~mms-no-such-user-866/bundle.json")
    review._refresh_toolgraph_bundle(force=True, startup=True)
    assert review.get_toolgraph_status()["degraded"] is True


def test_transient_stat_failure_recovers_when_path_returns_unchanged(tmp_path):
    # A first-region rejection (expanduser/stat) must not stick: after a
    # transient failure the path can come back with the SAME (ino, size,
    # mtime_ns) stamp, and the unchanged-stamp early return would then skip
    # the full reload that clears degraded/withhold-all state. Rejection
    # therefore invalidates the stored stamp so the next refresh re-adopts.
    manager, path, tool = _manager(tmp_path)
    _write_bundle(path, _bundle(tool))
    manager._refresh_toolgraph_bundle(force=True, startup=True)  # healthy load

    hidden = path.with_suffix(".hidden")
    path.rename(hidden)  # stat -> OSError; rename preserves ino/size/mtime
    manager._refresh_toolgraph_bundle()
    assert manager.get_toolgraph_status()["withholding_all"] is not None

    hidden.rename(path)  # path recovers byte- and stamp-identical
    manager._refresh_toolgraph_bundle()
    status = manager.get_toolgraph_status()
    assert status["withholding_all"] is None
    assert status["degraded"] is False


def test_rebind_bug_on_unchanged_stamp_propagates_not_withholds(tmp_path, monkeypatch):
    # The unchanged-stamp catalog rebind is deliberately OUTSIDE the reject
    # barrier: an internal binding bug is a programming error, not an invalid
    # bundle. Converting it to withhold-all would misdiagnose the fault and
    # stick — the unchanged-stamp early return never re-clears it.
    manager, path, tool = _manager(tmp_path)
    _write_bundle(path, _bundle(tool))
    manager._refresh_toolgraph_bundle(force=True, startup=True)  # healthy load

    manager._tool_catalog_revision += 1  # catalog moved; stamp unchanged

    def _boom(*args, **kwargs):
        raise RuntimeError("binding bug")

    monkeypatch.setattr(manager, "_apply_toolgraph_policy_snapshot", _boom)
    with pytest.raises(RuntimeError, match="binding bug"):
        manager._refresh_toolgraph_bundle()
    assert manager.get_toolgraph_status()["withholding_all"] is None


def test_bundle_mode_rejects_profile_split_brain(tmp_path):
    manager, path, tool = _manager(tmp_path)
    manager._config.toolgraph.query_profile = "review"
    _write_bundle(path, _bundle(tool))
    with pytest.raises(ToolgraphStartupError, match="query_profile must match"):
        manager._refresh_toolgraph_bundle(force=True, startup=True)


def test_gateway_mode_preview_then_atomic_apply(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / "proxy.json"
    config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}))
    runner = CliRunner()

    preview = runner.invoke(cli, ["gateway", "mode", "review", "--config", str(config)])
    assert preview.exit_code == 0, preview.output
    assert "Preview only" in preview.output
    assert "toolgraph" not in json.loads(config.read_text())

    applied = runner.invoke(cli, ["gateway", "mode", "review", "--config", str(config), "--apply"])
    assert applied.exit_code == 0, applied.output
    saved = json.loads(config.read_text())
    assert saved["exposure"]["profile"] == "review"
    assert saved["toolgraph"]["source"] == "bundle"
    assert saved["toolgraph"]["query_profile"] == "review"


def test_gateway_mode_preview_does_not_mutate_loaded_config(monkeypatch):
    data = {"enabled": True, "upstream_servers": {}}
    monkeypatch.setattr(proxy_cli, "_load", lambda _path: data)
    runner = CliRunner()

    preview = runner.invoke(cli, ["gateway", "mode", "review", "--config", "unused.json"])

    assert preview.exit_code == 0, preview.output
    assert data == {"enabled": True, "upstream_servers": {}}


def test_gateway_mode_strict_apply_warns_until_bundle_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / "proxy.json"
    config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}))
    missing = tmp_path / "not-published.json"
    runner = CliRunner()

    warned = runner.invoke(
        cli,
        [
            "gateway",
            "mode",
            "strict",
            "--config",
            str(config),
            "--bundle",
            str(missing),
            "--apply",
        ],
    )
    assert warned.exit_code == 0, warned.output
    assert "strict mode will refuse to start" in warned.output

    _write_bundle(missing, _bundle(_tool()))
    ready = runner.invoke(
        cli,
        [
            "gateway",
            "mode",
            "strict",
            "--config",
            str(config),
            "--bundle",
            str(missing),
            "--apply",
        ],
    )
    assert ready.exit_code == 0, ready.output
    assert "strict mode will refuse to start" not in ready.output


def test_gateway_status_and_explain_bundle(tmp_path):
    config = tmp_path / "proxy.json"
    bundle = tmp_path / "bundle.json"
    document = _bundle(_tool())
    document["tools"][0]["risk_score"] = None
    _write_bundle(bundle, document)
    config.write_text(
        json.dumps(
            {
                "enabled": True,
                "upstream_servers": {},
                "exposure": {"profile": "strict"},
                "toolgraph": {
                    "enabled": True,
                    "source": "bundle",
                    "bundle_path": str(bundle),
                    "agent_id": "stm-proxy",
                    "query_profile": "strict",
                },
            }
        )
    )
    runner = CliRunner()
    status = runner.invoke(cli, ["gateway", "status", "--config", str(config), "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["valid"] is True

    explain = runner.invoke(
        cli,
        ["gateway", "explain", "graph-srv::read", "--config", str(config), "--json"],
    )
    assert explain.exit_code == 0, explain.output
    assert json.loads(explain.output)["decision"] == "eligible"

    plain = runner.invoke(
        cli,
        ["gateway", "explain", "graph-srv::read", "--config", str(config)],
    )
    assert plain.exit_code == 0, plain.output
    assert "risk score: n/a" in plain.output


class TestBindFailureDiagnostics:
    """A catalog-wide bind failure names its likely cause exactly once.

    The reject codes cannot carry this: ``_TOOLGRAPH_REASON_MAP`` maps a
    producer-declared ``DRIFTED``/``UNMAPPED`` reason onto the same code STM
    emits for a computed digest mismatch or a missing decision, so only the
    STM-computed branches may drive the diagnostic.
    """

    def test_all_unmapped_blames_server_name_map(self, tmp_path, caplog):
        manager, bundle_path, tool = _manager(tmp_path)
        manager._config.toolgraph.server_name_map = {"srv": "wrong-name"}
        _write_bundle(bundle_path, _bundle(tool))
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)
        assert "server_name_map" in caplog.text
        status = manager.get_toolgraph_status()
        assert status["all_bind_failure"] == "unmapped"
        assert status["bind_stats"] == {
            "catalog_total": 1,
            "stm_unmapped": 1,
            "stm_drifted": 0,
        }

    def test_all_drifted_blames_digest_or_stale_catalog(self, tmp_path, caplog):
        manager, bundle_path, tool = _manager(tmp_path)
        doc = _bundle(tool)
        doc["tools"][0]["tool_contract_digest"] = "c" * 64
        _write_bundle(bundle_path, doc)
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)
        assert "digest algorithm" in caplog.text
        status = manager.get_toolgraph_status()
        assert status["all_bind_failure"] == "drifted"
        assert status["bind_stats"]["stm_drifted"] == 1

    def test_producer_declared_drift_is_not_an_stm_bind_failure(self, tmp_path, caplog):
        """Positive control: the SAME final reject code, no false warning."""
        manager, bundle_path, tool = _manager(tmp_path)
        doc = _bundle(tool, decision="rejected")
        doc["tools"][0]["reason"] = "DRIFTED"
        _write_bundle(bundle_path, doc)
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)

        # The tool IS rejected, with the same code an STM-computed drift emits...
        assert manager._toolgraph_external_rejects[("srv", "read")] == REASON_TOOLGRAPH_DRIFTED
        # ...yet this is the producer's own verdict, so nothing is diagnosed.
        assert "digest algorithm" not in caplog.text
        status = manager.get_toolgraph_status()
        assert status["all_bind_failure"] is None
        assert status["bind_stats"] == {
            "catalog_total": 1,
            "stm_unmapped": 0,
            "stm_drifted": 0,
        }

    def test_producer_declared_unmapped_is_not_an_stm_bind_failure(self, tmp_path, caplog):
        manager, bundle_path, tool = _manager(tmp_path)
        doc = _bundle(tool, decision="rejected")
        doc["tools"][0]["reason"] = "UNMAPPED"
        _write_bundle(bundle_path, doc)
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)

        assert manager._toolgraph_external_rejects[("srv", "read")] == REASON_TOOLGRAPH_UNMAPPED
        assert "server_name_map" not in caplog.text
        assert manager.get_toolgraph_status()["all_bind_failure"] is None

    def test_partial_failure_is_not_warned(self, tmp_path, caplog):
        manager, bundle_path, tool = _manager(tmp_path)
        other = _tool(name="write", description="Write data")
        manager._connections["srv"].tools = [tool, other]
        _write_bundle(bundle_path, _bundle(tool))  # only `read` is in the bundle
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)

        assert manager._toolgraph_external_rejects[("srv", "write")] == REASON_TOOLGRAPH_UNMAPPED
        assert "server_name_map" not in caplog.text
        status = manager.get_toolgraph_status()
        assert status["all_bind_failure"] is None
        assert status["bind_stats"] == {
            "catalog_total": 2,
            "stm_unmapped": 1,
            "stm_drifted": 0,
        }

    def test_mixed_total_failure_reports_both_counts(self, tmp_path, caplog):
        manager, bundle_path, tool = _manager(tmp_path)
        other = _tool(name="write", description="Write data")
        manager._connections["srv"].tools = [tool, other]
        doc = _bundle(tool)
        doc["tools"][0]["tool_contract_digest"] = "c" * 64  # read drifts, write unmapped
        _write_bundle(bundle_path, doc)
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)

        assert "1 unmapped" in caplog.text and "1 drifted" in caplog.text
        status = manager.get_toolgraph_status()
        assert status["all_bind_failure"] == "mixed"

    def test_empty_catalog_is_not_a_bind_failure(self, tmp_path, caplog):
        manager, bundle_path, tool = _manager(tmp_path)
        manager._connections.clear()
        _write_bundle(bundle_path, _bundle(tool))
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)

        status = manager.get_toolgraph_status()
        assert status["all_bind_failure"] is None
        assert status["bind_stats"]["catalog_total"] == 0

    def test_warning_is_once_per_episode_and_rearms_after_recovery(self, tmp_path, caplog):
        manager, bundle_path, tool = _manager(tmp_path)
        manager._config.toolgraph.server_name_map = {"srv": "wrong-name"}
        _write_bundle(bundle_path, _bundle(tool))

        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)
            manager._refresh_toolgraph_bundle(force=True)
        assert caplog.text.count("server_name_map") == 1, "an episode must warn once"

        # Recover, then regress: the operator must hear about it again.
        caplog.clear()
        manager._config.toolgraph.server_name_map = {"srv": "graph-srv"}
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)
        assert manager.get_toolgraph_status()["all_bind_failure"] is None

        manager._config.toolgraph.server_name_map = {"srv": "wrong-name"}
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)
        assert caplog.text.count("server_name_map") == 1, "a recurrence must warn again"

    def test_cause_shift_within_an_episode_does_not_rewarn(self, tmp_path, caplog):
        """The episode is "the whole catalog is failing", not a given cause."""
        manager, bundle_path, tool = _manager(tmp_path)
        manager._config.toolgraph.server_name_map = {"srv": "wrong-name"}
        _write_bundle(bundle_path, _bundle(tool))
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)
        assert manager.get_toolgraph_status()["all_bind_failure"] == "unmapped"

        # Still totally failing, but now partly by drift: same episode.
        caplog.clear()
        manager._config.toolgraph.server_name_map = {"srv": "graph-srv"}
        other = _tool(name="write", description="Write data")
        manager._connections["srv"].tools = [tool, other]
        doc = _bundle(tool)
        doc["tools"][0]["tool_contract_digest"] = "c" * 64
        _write_bundle(bundle_path, doc)
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)

        status = manager.get_toolgraph_status()
        assert status["all_bind_failure"] == "mixed", "the reported cause still tracks reality"
        assert "Toolgraph" not in caplog.text, "but the same episode must not warn twice"

    @pytest.mark.asyncio
    async def test_restart_drops_the_previous_catalogs_diagnostic(self, tmp_path, caplog):
        """stop() → start() bypasses the double-start guard; state must still reset."""
        manager, bundle_path, tool = _manager(tmp_path)
        manager._config.toolgraph.server_name_map = {"srv": "wrong-name"}
        _write_bundle(bundle_path, _bundle(tool))
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)
        assert manager.get_toolgraph_status()["all_bind_failure"] == "unmapped"

        # A restart whose bundle is invalid degrades before binding anything:
        # reporting the dead catalog's stats would misdirect the operator.
        manager._config.exposure.profile = ExposureProfile.REVIEW
        manager._config.toolgraph.query_profile = "review"
        manager._config.upstream_servers = {}
        bundle_path.write_bytes(b"{ not json")
        with caplog.at_level("WARNING"):
            await manager.start()

        status = manager.get_toolgraph_status()
        assert status["all_bind_failure"] is None
        assert status["bind_stats"] == {}


class TestBindFailureHealthRendering:
    """`stm_proxy_health` must actually carry the diagnostic to the operator."""

    def _status(self, **over):
        status = {
            "enabled": True,
            "degraded": False,
            "degraded_reason": None,
            "withholding_all": None,
            "graph_generation": 7,
            "from_cache": False,
            "external_reject_count": 2,
            "risk_penalty_count": 0,
            "source": "bundle",
            "graph_instance_id": "graph-1",
            "bundle_digest": "d" * 64,
            "would_block_calls": 0,
            "using_last_known_good": False,
            "bind_stats": {"catalog_total": 2, "stm_unmapped": 2, "stm_drifted": 0},
            "all_bind_failure": "unmapped",
        }
        status.update(over)
        return status

    def test_all_unmapped_names_server_name_map(self):
        from memtomem_stm.server import _toolgraph_health_lines

        text = "\n".join(_toolgraph_health_lines(self._status()))
        assert "ALL 2 live tool(s) failed to bind" in text
        assert "toolgraph.server_name_map" in text

    def test_all_drifted_names_the_digest_mismatch(self):
        from memtomem_stm.server import _toolgraph_health_lines

        text = "\n".join(
            _toolgraph_health_lines(
                self._status(
                    all_bind_failure="drifted",
                    bind_stats={"catalog_total": 2, "stm_unmapped": 0, "stm_drifted": 2},
                )
            )
        )
        assert "digest algorithm" in text

    def test_mixed_failure_reports_both_counts(self):
        from memtomem_stm.server import _toolgraph_health_lines

        text = "\n".join(
            _toolgraph_health_lines(
                self._status(
                    all_bind_failure="mixed",
                    bind_stats={"catalog_total": 3, "stm_unmapped": 1, "stm_drifted": 2},
                )
            )
        )
        assert "1 unmapped, 2 drifted" in text

    def test_healthy_binding_renders_no_diagnostic(self):
        """Positive control: the line appears only for an all-fail episode."""
        from memtomem_stm.server import _toolgraph_health_lines

        text = "\n".join(_toolgraph_health_lines(self._status(all_bind_failure=None)))
        assert "failed to bind" not in text
        assert "active (graph generation 7" in text

    def test_strict_reload_failure_drops_the_stale_bind_diagnosis(self, tmp_path, caplog):
        """Fail-closed supersedes binding; the old cause must not survive it."""
        manager, bundle_path, tool = _manager(tmp_path)
        manager._config.toolgraph.server_name_map = {"srv": "wrong-name"}
        _write_bundle(bundle_path, _bundle(tool))
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)
        assert manager.get_toolgraph_status()["all_bind_failure"] == "unmapped"

        bundle_path.write_bytes(b"{ corrupt")
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)

        status = manager.get_toolgraph_status()
        assert status["withholding_all"] == "toolgraph_protocol_error"
        assert status["all_bind_failure"] is None, "a protocol error is not a mapping failure"
        assert status["bind_stats"] == {}

    def test_review_mode_last_known_good_keeps_reporting_its_bind_failure(self, tmp_path, caplog):
        """The LKG snapshot still enforces, so its diagnosis is still live."""
        manager, bundle_path, tool = _manager(tmp_path, profile=ExposureProfile.REVIEW)
        manager._config.toolgraph.server_name_map = {"srv": "wrong-name"}
        _write_bundle(bundle_path, _bundle(tool, profile="review"))
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)

        bundle_path.write_bytes(b"{ corrupt")
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)

        status = manager.get_toolgraph_status()
        assert status["using_last_known_good"] is True
        assert status["all_bind_failure"] == "unmapped"
        from memtomem_stm.server import _toolgraph_health_lines

        text = "\n".join(_toolgraph_health_lines(status))
        assert "DEGRADED" in text
        assert "toolgraph.server_name_map" in text, "the hint must survive degradation"

    def test_fail_closed_hides_the_bind_diagnostic(self):
        """Nothing is withheld for a binding reason when a knob fired closed."""
        from memtomem_stm.server import _toolgraph_health_lines

        text = "\n".join(
            _toolgraph_health_lines(
                self._status(withholding_all="toolgraph_protocol_error", degraded=True)
            )
        )
        assert "WITHHOLDING ALL" in text
        assert "failed to bind" not in text

    def test_degraded_last_known_good_still_shows_the_diagnostic(self):
        from memtomem_stm.server import _toolgraph_health_lines

        text = "\n".join(
            _toolgraph_health_lines(
                self._status(
                    degraded=True,
                    degraded_reason="toolgraph_protocol_error",
                    using_last_known_good=True,
                )
            )
        )
        assert "DEGRADED" in text
        assert "toolgraph.server_name_map" in text


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
class TestBundleProvenanceAdvisory:
    """The bundle is unsigned enforcement authority: who can replace it matters.

    Every case here is about *write* access, never read access -- a 0644 bundle
    holds no secrets and must stay silent.
    """

    def test_a_normal_private_bundle_is_silent(self, tmp_path):
        """Positive control: the default install must not cry wolf."""
        bundle = tmp_path / "policy-bundle.json"
        bundle.write_bytes(b"{}")
        bundle.chmod(0o644)
        tmp_path.chmod(0o755)
        assert bundle_provenance_warnings(bundle) == []

    def test_world_readable_is_not_a_finding(self, tmp_path):
        """0644 is fine -- this check is about substitution, not secrecy."""
        bundle = tmp_path / "policy-bundle.json"
        bundle.write_bytes(b"{}")
        bundle.chmod(0o644)
        assert bundle_provenance_warnings(bundle) == []

    @pytest.mark.parametrize("mode", [0o666, 0o622, 0o646])
    def test_group_or_world_writable_file_is_a_finding(self, tmp_path, mode):
        bundle = tmp_path / "policy-bundle.json"
        bundle.write_bytes(b"{}")
        bundle.chmod(mode)
        findings = bundle_provenance_warnings(bundle)
        assert any("rename its entries" in f or "writable by group/other" in f for f in findings)

    def test_a_symlink_we_alone_can_replace_is_silent(self, tmp_path):
        """Positive control: a link is only a vector if someone can re-point it.

        POSIX has no way to edit a link's target in place -- replacing it needs
        write and search on the directory holding it. A link in a directory only
        we can write redirects nothing.
        """
        real = tmp_path / "real.json"
        real.write_bytes(b"{}")
        real.chmod(0o644)
        link = tmp_path / "policy-bundle.json"
        link.symlink_to(real)
        tmp_path.chmod(0o755)
        assert bundle_provenance_warnings(link) == []

    def test_a_foreign_symlink_in_a_sticky_dir_is_a_finding(self, tmp_path, monkeypatch):
        """Sticky stops others touching OUR entries -- not an owner touching theirs.

        `/tmp` at 01777 is the case: a link we do not own is its owner's to
        unlink and recreate at will, while the chain it resolves into stays
        perfectly secure. Only the link's st_uid is faked -- patching geteuid
        would make the parent foreign too and fire the check for another reason.
        """
        home = tmp_path / "sticky"
        home.mkdir()
        real = tmp_path / "real.json"
        real.write_bytes(b"{}")
        real.chmod(0o644)
        link = home / "policy-bundle.json"
        link.symlink_to(real)
        home.chmod(0o1777)
        real_lstat = os.lstat

        def fake_lstat(p, *a, **kw):
            info = real_lstat(p, *a, **kw)
            if str(p) != str(link):
                return info
            fields = list(info)
            fields[4] = 999999  # st_uid
            return os.stat_result(fields)

        monkeypatch.setattr(toolgraph_bundle_mod.os, "lstat", fake_lstat)
        try:
            findings = bundle_provenance_warnings(link)
            assert any(str(link) in f and "uid 999999" in f for f in findings)
        finally:
            home.chmod(0o755)

    def test_a_foreign_symlink_in_a_group_only_sticky_dir_warns_conservatively(
        self, tmp_path, monkeypatch
    ):
        """A 01770 dir warns even though we cannot prove the owner is in its group.

        A deliberate false positive: resolving a foreign uid's groups means
        pwd/grp lookups that fail or stall exactly where it matters (LDAP,
        containers, no local passwd entry). When membership cannot be
        established, warn and let a human judge.
        """
        home = tmp_path / "groupsticky"
        home.mkdir()
        real = tmp_path / "real.json"
        real.write_bytes(b"{}")
        real.chmod(0o644)
        link = home / "policy-bundle.json"
        link.symlink_to(real)
        home.chmod(0o1770)
        real_lstat = os.lstat

        def fake_lstat(p, *a, **kw):
            info = real_lstat(p, *a, **kw)
            if str(p) != str(link):
                return info
            fields = list(info)
            fields[4] = 999999  # st_uid
            return os.stat_result(fields)

        monkeypatch.setattr(toolgraph_bundle_mod.os, "lstat", fake_lstat)
        try:
            findings = bundle_provenance_warnings(link)
            assert any(str(link) in f and "uid 999999" in f for f in findings)
        finally:
            home.chmod(0o755)

    def test_our_own_symlink_in_a_sticky_dir_is_silent(self, tmp_path):
        """Positive control: sticky genuinely protects the entries we own."""
        home = tmp_path / "sticky"
        home.mkdir()
        real = tmp_path / "real.json"
        real.write_bytes(b"{}")
        real.chmod(0o644)
        link = home / "policy-bundle.json"
        link.symlink_to(real)
        home.chmod(0o1777)
        try:
            assert bundle_provenance_warnings(link) == []
        finally:
            home.chmod(0o755)

    def test_a_symlink_others_can_replace_is_a_finding(self, tmp_path):
        home = tmp_path / "toolgraph"
        home.mkdir()
        real = tmp_path / "real.json"
        real.write_bytes(b"{}")
        real.chmod(0o644)
        link = home / "policy-bundle.json"
        link.symlink_to(real)
        home.chmod(0o777)
        try:
            findings = bundle_provenance_warnings(link)
            assert any(str(link) in f and "re-pointed" in f for f in findings)
        finally:
            home.chmod(0o755)

    def test_world_writable_parent_lets_the_bundle_be_replaced(self, tmp_path):
        home = tmp_path / "toolgraph"
        home.mkdir()
        bundle = home / "policy-bundle.json"
        bundle.write_bytes(b"{}")
        bundle.chmod(0o644)
        home.chmod(0o777)
        try:
            findings = bundle_provenance_warnings(bundle)
            assert any("can be replaced" in f and str(home) in f for f in findings)
        finally:
            home.chmod(0o755)

    def test_sticky_world_writable_parent_is_not_a_finding(self, tmp_path):
        """Positive control: /tmp-style dirs already stop non-owners renaming."""
        home = tmp_path / "sticky"
        home.mkdir()
        bundle = home / "policy-bundle.json"
        bundle.write_bytes(b"{}")
        bundle.chmod(0o644)
        home.chmod(0o1777)
        try:
            assert bundle_provenance_warnings(bundle) == []
        finally:
            home.chmod(0o755)

    def test_a_grandparent_is_walked_too(self, tmp_path):
        outer = tmp_path / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        bundle = inner / "policy-bundle.json"
        bundle.write_bytes(b"{}")
        bundle.chmod(0o644)
        outer.chmod(0o777)
        try:
            findings = bundle_provenance_warnings(bundle)
            assert any(str(outer) in f and "can be replaced" in f for f in findings)
        finally:
            outer.chmod(0o755)

    def test_foreign_owner_is_reported_for_the_file_and_for_ancestors(self, tmp_path, monkeypatch):
        """Another user owning either can chmod and rewrite the policy at will.

        Both halves are asserted by NAME: pretending to be a foreign euid makes
        the whole tree foreign, so a generic "some finding says foreign" check
        would still pass with the file-owner branch deleted.
        """
        bundle = tmp_path / "policy-bundle.json"
        bundle.write_bytes(b"{}")
        bundle.chmod(0o644)
        monkeypatch.setattr(toolgraph_bundle_mod.os, "geteuid", lambda: 999999)
        findings = bundle_provenance_warnings(bundle)
        # Exact prefixes: `str(tmp_path)` is a substring of `str(bundle)`, so a
        # loose `in` check would let the file's finding satisfy both assertions.
        assert any(f.startswith(f"{bundle} is owned by uid ") for f in findings)
        assert any(f.startswith(f"{tmp_path} is owned by uid ") for f in findings)

    @pytest.mark.parametrize("mode", [0o722, 0o702])
    def test_a_write_bit_without_search_cannot_rename_and_is_silent(self, tmp_path, mode):
        """Positive control: renaming needs write AND execute on the directory."""
        home = tmp_path / "toolgraph"
        home.mkdir()
        bundle = home / "policy-bundle.json"
        bundle.write_bytes(b"{}")
        bundle.chmod(0o644)
        home.chmod(mode)
        try:
            assert bundle_provenance_warnings(bundle) == []
        finally:
            home.chmod(0o755)

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            (0o755, ""),  # the normal case
            (0o777, "group/other"),
            (0o775, "group"),
            (0o757, "other"),
            (0o722, ""),  # write, no search: names inside cannot be resolved
            (0o702, ""),
            (0o622, ""),  # not reachable via lstat either, but pin the rule
            (0o1777, ""),  # sticky: only an entry's owner may rename it
        ],
    )
    def test_entry_renamers_needs_write_and_search_in_the_same_class(self, mode, expected):
        """The permission rule itself, independent of any reachable fixture."""
        assert toolgraph_bundle_mod._entry_renamers(mode) == expected

    def test_a_symlinked_component_followed_by_dotdot_inspects_what_loads(
        self, tmp_path, monkeypatch
    ):
        """`link/..` resolves against the link's TARGET, not the text before it.

        A lexical abspath would inspect `<cwd>/bundle` while the loader opens
        `<target parent>/bundle` -- reporting all-clear about a file nothing
        enforces.
        """
        exposed = tmp_path / "exposed"
        real = exposed / "real"
        work = tmp_path / "work"
        real.mkdir(parents=True)
        work.mkdir()
        bundle = exposed / "policy-bundle.json"
        bundle.write_bytes(b"{}")
        bundle.chmod(0o644)
        (work / "link").symlink_to(real)
        # A decoy at the path a lexical `..` collapse would land on.
        decoy = work / "policy-bundle.json"
        decoy.write_bytes(b"{}")
        decoy.chmod(0o644)
        work.chmod(0o755)
        exposed.chmod(0o777)
        monkeypatch.chdir(work)
        try:
            configured = Path("link/../policy-bundle.json")
            assert os.path.realpath(configured) == str(bundle), "fixture: the loader opens `bundle`"
            findings = bundle_provenance_warnings(configured)
            # The exposed directory is on the RESOLVED chain; a lexical `..`
            # collapse would have landed on `work` and reported all-clear.
            assert any(str(exposed) in f and "can be replaced" in f for f in findings)
            assert not any(str(decoy) in f for f in findings), "the decoy is not what loads"
            # `work` holds the link but only we can write it, so the link itself
            # redirects nothing -- both rules compose.
            assert not any("re-pointed" in f for f in findings)
        finally:
            exposed.chmod(0o755)

    def test_a_relative_path_still_walks_above_the_working_directory(self, tmp_path, monkeypatch):
        """`bundle_path` may be relative; `Path.parents` would stop at `.`."""
        outer = tmp_path / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        bundle = inner / "policy-bundle.json"
        bundle.write_bytes(b"{}")
        bundle.chmod(0o644)
        inner.chmod(0o755)
        outer.chmod(0o777)
        monkeypatch.chdir(inner)
        try:
            findings = bundle_provenance_warnings(Path("policy-bundle.json"))
            assert any(str(outer) in f and "can be replaced" in f for f in findings)
        finally:
            outer.chmod(0o755)

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS chmod +a ACL syntax")
    def test_an_acl_granting_write_is_a_documented_blind_spot(self, tmp_path):
        """Pins the SCOPE claim: mode/uid only, so an ACL reads as clean.

        Not a wish -- `everyone allow write` on a 0644 file really is invisible
        here. The docstring says so; this makes the limit fail loudly if anyone
        later believes silence is an assurance.
        """
        bundle = tmp_path / "policy-bundle.json"
        bundle.write_bytes(b"{}")
        bundle.chmod(0o644)
        tmp_path.chmod(0o755)
        added = subprocess.run(
            ["chmod", "+a", "everyone allow write,delete", str(bundle)],
            capture_output=True,
        )
        if added.returncode != 0:  # pragma: no cover - filesystem without ACLs
            pytest.skip("filesystem does not support ACLs")
        # Assert the ACL stuck, NOT how it prints: `ls -le` renders the
        # principal as `group:everyone` on some macOS hosts and as its
        # well-known UUID on others, so matching the name passes only where it
        # happens to be spelled that way.
        listing = subprocess.run(["ls", "-le", str(bundle)], capture_output=True, text=True).stdout
        # `allow` AND `write` on the same ACE: an unrelated `allow read` would
        # make this pass while proving nothing about the blind spot.
        assert re.search(r"^\s*\d+:.*\ballow\b[^\n]*\bwrite\b", listing, re.MULTILINE), (
            f"fixture: a write-granting ACL is really there -- got {listing!r}"
        )
        assert stat.S_IMODE(bundle.stat().st_mode) == 0o644, "fixture: mode still looks private"
        assert bundle_provenance_warnings(bundle) == [], (
            "mode/uid analysis cannot see ACLs -- if this ever starts failing, the "
            "scope note in bundle_provenance_warnings' docstring is now wrong"
        )

    def test_a_chained_symlink_hop_in_an_exposed_dir_is_a_finding(self, tmp_path):
        """`a/bundle -> b/link2 -> c/real`: the middle hop is on neither walk.

        The ancestor analysis follows the resolved chain (`c`), and stopping at
        the configured path's first link never reaches `b`. Anyone with
        write+search on `b` re-points what the loader opens.
        """
        a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
        for d in (a, b, c):
            d.mkdir()
        real = c / "real.json"
        real.write_bytes(b"{}")
        real.chmod(0o644)
        (b / "link2").symlink_to(real)
        bundle = a / "policy-bundle.json"
        bundle.symlink_to(b / "link2")
        a.chmod(0o755)
        c.chmod(0o755)
        b.chmod(0o777)
        try:
            findings = bundle_provenance_warnings(bundle)
            assert any(str(b / "link2") in f and "re-pointed" in f for f in findings)
        finally:
            b.chmod(0o755)

    def test_a_chained_symlink_through_secure_dirs_is_silent(self, tmp_path):
        """Positive control: chasing hops must not invent findings."""
        a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
        for d in (a, b, c):
            d.mkdir()
            d.chmod(0o755)
        real = c / "real.json"
        real.write_bytes(b"{}")
        real.chmod(0o644)
        (b / "link2").symlink_to(real)
        bundle = a / "policy-bundle.json"
        bundle.symlink_to(b / "link2")
        assert bundle_provenance_warnings(bundle) == []

    def test_one_link_reached_by_two_spellings_reports_once(self, tmp_path):
        """`link/../link/x` traverses the same entry twice; it is one fact."""
        sub = tmp_path / "sub"
        sub.mkdir()
        real = sub / "policy-bundle.json"
        real.write_bytes(b"{}")
        real.chmod(0o644)
        (tmp_path / "link").symlink_to(sub)
        tmp_path.chmod(0o777)
        try:
            findings = bundle_provenance_warnings(
                tmp_path / "link" / ".." / "link" / "policy-bundle.json"
            )
            assert len([f for f in findings if "re-pointed" in f]) == 1
        finally:
            tmp_path.chmod(0o755)

    def test_a_hard_linked_twin_is_judged_on_its_own_parent(self, tmp_path):
        """Identity is the directory ENTRY, not the inode.

        Symlinks can be hard-linked, so two entries under parents with
        different permissions share one inode. Keying dedup on the inode let a
        secure sighting suppress the exposed twin traversed right after it.
        """
        secure, exposed = tmp_path / "secure", tmp_path / "exposed"
        secure.mkdir()
        exposed.mkdir()
        # One inode, two entries; the content routes through the other parent,
        # so a single scan meets the secure entry first and the exposed second.
        (secure / "link").symlink_to("../exposed/link")
        os.link(secure / "link", exposed / "link", follow_symlinks=False)
        assert os.lstat(secure / "link").st_ino == os.lstat(exposed / "link").st_ino
        tmp_path.chmod(0o755)
        secure.chmod(0o755)
        exposed.chmod(0o777)
        try:
            findings = bundle_provenance_warnings(secure / "link")
            assert any("exposed" in f and "re-pointed" in f for f in findings)
        finally:
            exposed.chmod(0o755)

    def test_an_unstattable_parent_skips_the_report_not_the_rest_of_the_scan(
        self, tmp_path, monkeypatch
    ):
        """Judging one entry needs its parent; the hops beyond it do not."""
        first, exposed = tmp_path / "first", tmp_path / "exposed"
        first.mkdir()
        exposed.mkdir()
        real = exposed / "real.json"
        real.write_bytes(b"{}")
        real.chmod(0o644)
        (exposed / "hop2").symlink_to(real)
        bundle = first / "policy-bundle.json"
        bundle.symlink_to(exposed / "hop2")
        tmp_path.chmod(0o755)
        first.chmod(0o755)
        exposed.chmod(0o777)
        real_stat = os.stat

        def fake_stat(p, *a, **kw):
            # Only the FIRST link's parent is unstattable (a race, in practice).
            if str(p) == str(first):
                raise PermissionError("simulated race")
            return real_stat(p, *a, **kw)

        monkeypatch.setattr(toolgraph_bundle_mod.os, "stat", fake_stat)
        try:
            findings = bundle_provenance_warnings(bundle)
            assert any(str(exposed / "hop2") in f and "re-pointed" in f for f in findings), (
                "the downstream hop must still be reported"
            )
        finally:
            exposed.chmod(0o755)

    def test_a_symlink_cycle_terminates(self, tmp_path):
        """A loop must exhaust the hop budget, not the stack."""
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.symlink_to(second)
        second.symlink_to(first)
        assert bundle_provenance_warnings(first) == []

    def test_a_relative_link_target_resolves_against_the_links_own_dir(self, tmp_path):
        exposed = tmp_path / "exposed"
        exposed.mkdir()
        real = exposed / "real.json"
        real.write_bytes(b"{}")
        real.chmod(0o644)
        (exposed / "link2").symlink_to("real.json")  # relative target
        bundle = tmp_path / "policy-bundle.json"
        bundle.symlink_to(exposed / "link2")
        tmp_path.chmod(0o755)
        exposed.chmod(0o777)
        try:
            findings = bundle_provenance_warnings(bundle)
            assert any(str(exposed / "link2") in f and "re-pointed" in f for f in findings)
        finally:
            exposed.chmod(0o755)

    def test_an_unresolvable_home_is_not_this_diagnostic_s_problem(self):
        """`expanduser` raises RuntimeError, not OSError, for `~nosuchuser`."""
        assert bundle_provenance_warnings(Path("~nosuchuser/policy-bundle.json")) == []

    def test_a_missing_bundle_is_not_this_diagnostic_s_problem(self, tmp_path):
        assert bundle_provenance_warnings(tmp_path / "absent.json") == []

    def test_windows_is_a_noop(self, tmp_path, monkeypatch):
        bundle = tmp_path / "policy-bundle.json"
        bundle.write_bytes(b"{}")
        bundle.chmod(0o666)
        assert bundle_provenance_warnings(bundle), "sanity: POSIX would flag this"
        monkeypatch.setattr(toolgraph_bundle_mod.sys, "platform", "win32")
        assert bundle_provenance_warnings(bundle) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
class TestBundleProvenanceWiring:
    def test_adopting_an_exposed_bundle_warns_once_per_finding_set(self, tmp_path, caplog):
        manager, bundle_path, tool = _manager(tmp_path)
        _write_bundle(bundle_path, _bundle(tool))
        tmp_path.chmod(0o777)
        try:
            with caplog.at_level("WARNING"):
                manager._refresh_toolgraph_bundle(force=True)
                manager._refresh_toolgraph_bundle(force=True)
            assert caplog.text.count("not protected from substitution") == 1
            # Advisory: the bundle is adopted regardless.
            assert manager._toolgraph_policy_snapshot is not None
            assert manager._toolgraph_withhold_all is None
        finally:
            tmp_path.chmod(0o755)

    def test_a_safe_bundle_stays_silent(self, tmp_path, caplog):
        manager, bundle_path, tool = _manager(tmp_path)
        _write_bundle(bundle_path, _bundle(tool))
        tmp_path.chmod(0o755)
        with caplog.at_level("WARNING"):
            manager._refresh_toolgraph_bundle(force=True)
        assert "not protected from substitution" not in caplog.text


def test_contract_digest_survives_a_surrogate_in_upstream_metadata():
    """``name``/``description``/``input_schema`` come from live ``tools/list``
    metadata, which nothing gates — the entry-field check #758 added covers what
    *this machine's* config can carry, not what an upstream sends back. The
    digest encodes its canonical bytes, so such metadata raised (#761).
    """
    digest = tool_contract_digest(
        server="graph-srv",
        name="read",
        description="hostile\ud800",
        input_schema={"type": "object", "title": "t\udfffx"},
        annotations=None,
    )
    assert len(digest) == 64

    # Still a fingerprint: the escape happens before the hash, so distinct
    # metadata keeps producing distinct digests rather than collapsing.
    other = tool_contract_digest(
        server="graph-srv",
        name="read",
        description="hostile\udfff",
        input_schema={"type": "object", "title": "t\udfffx"},
        annotations=None,
    )
    assert other != digest


def test_contract_digest_is_byte_identical_for_clean_metadata():
    """The producer (``toolgraph.artifacts.canonical_json_bytes``) uses exactly
    these arguments and raises on a surrogate itself, so no published bundle can
    hold a digest for surrogate-bearing metadata. Escaping only has to leave
    clean metadata alone — which is what keeps every existing bundle binding.
    """
    contract = {
        "server": "graph-srv",
        "name": "read",
        "description": "Read data 서버 🚀",
        "input_schema": {"type": "object"},
        "read_only_hint": True,
        "destructive_hint": None,
        "idempotent_hint": None,
        "open_world_hint": None,
    }
    expected = (
        json.dumps(contract, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    assert canonical_json_bytes(contract) == expected


def test_metadata_drifting_to_a_surrogate_drifts_one_tool_not_the_catalog(tmp_path):
    """The reachable shape is metadata *drift*, which is what the digest exists
    to detect. A bundle can only ever be published for clean metadata — the
    producer's own ``canonical_json_bytes`` raises otherwise — but it keys on the
    tool *name*, so a tool crawled clean and later serving a surrogate in its
    description binds by name, reaches the digest, and raised.

    The digest runs inside the bind loop over every tool of every connection,
    and neither call site guards ``UnicodeEncodeError``: the reload site's
    ``except`` catches only ``(OSError, PolicyBundleError)`` and the startup
    apply sits outside the ``try`` entirely. So one drifted tool took down every
    ``tools/list`` and ``tools/call`` in bundle mode, not just its own (#761).
    """
    manager, path, tool = _manager(tmp_path, profile=ExposureProfile.REVIEW)
    # Bundle published while BOTH tools were clean.
    clean_publish = _tool(name="publish", description="Publish a draft")
    doc = _bundle(tool, profile="review")
    doc["tools"].append(
        {
            "tool_key": "graph-srv::publish",
            "tool_contract_digest": tool_contract_digest(
                server="graph-srv",
                name=clean_publish.name,
                description=clean_publish.description,
                input_schema=clean_publish.input_schema,
                annotations=clean_publish.annotations,
            ),
            "decision": "eligible",
            "risk_score": 0.1,
        }
    )
    _write_bundle(path, doc)

    # Upstream now serves a surrogate in that tool's description.
    connection = manager._connections["srv"]
    manager._connections["srv"] = UpstreamConnection(
        name=connection.name,
        config=connection.config,
        session=connection.session,
        tools=[tool, _tool(name="publish", description="Publish a draft\ud800")],
    )

    manager._refresh_toolgraph_bundle(force=True, startup=True)

    # Fail-closed on the drifted tool, which is the correct outcome: the escaped
    # digest cannot match a digest no producer could have published.
    assert manager._toolgraph_external_rejects[("srv", "publish")] == REASON_TOOLGRAPH_DRIFTED
    # The point of the test: its clean sibling still bound normally.
    assert ("srv", "read") not in manager._toolgraph_external_rejects
    assert manager._toolgraph_bind_stats["catalog_total"] == 2
    assert manager._toolgraph_bind_stats["stm_drifted"] == 1
