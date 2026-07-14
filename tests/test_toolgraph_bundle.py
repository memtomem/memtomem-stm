"""Portable Toolgraph bundle parsing and gateway enforcement tests."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator
from mcp.server.fastmcp.exceptions import ToolError

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
from memtomem_stm.proxy.toolgraph_bundle import (
    PolicyBundleError,
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
        inputSchema={"type": "object"},
        annotations=SimpleNamespace(
            readOnlyHint=True,
            destructiveHint=None,
            idempotentHint=None,
            openWorldHint=None,
        ),
    )


def _bundle(tool: SimpleNamespace, *, profile: str = "strict", decision: str = "eligible"):
    digest = tool_contract_digest(
        server="graph-srv",
        name=tool.name,
        description=tool.description,
        input_schema=tool.inputSchema,
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
        readOnlyHint=contract["read_only_hint"],
        destructiveHint=contract["destructive_hint"],
        idempotentHint=contract["idempotent_hint"],
        openWorldHint=contract["open_world_hint"],
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


def test_vendored_contract_schema_accepts_reference_bundle():
    schema_path = files("memtomem_stm.data").joinpath("policy-bundle.schema.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(_bundle(_tool()))


@pytest.mark.parametrize("version", [0, 2, "1", True])
def test_parser_rejects_unknown_schema_versions(version):
    doc = _bundle(_tool())
    doc["schema_version"] = version
    with pytest.raises(PolicyBundleError, match="schema_version"):
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
    manager._call_tool_guarded = AsyncMock(return_value=("ok", False))
    assert await manager.call_tool("srv", "read", {}) == "ok"

    _write_bundle(path, _bundle(tool, decision="rejected"))
    with pytest.raises(ToolError, match="toolgraph_deny_violation"):
        await manager.call_tool("srv", "read", {})
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


def test_gateway_status_and_explain_bundle(tmp_path):
    config = tmp_path / "proxy.json"
    bundle = tmp_path / "bundle.json"
    _write_bundle(bundle, _bundle(_tool()))
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
