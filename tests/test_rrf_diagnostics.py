"""Operator advice must remain passive and valid across both score formats."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.rrf_diagnostics import rrf_boundary_doctor_checks
from memtomem_stm.surfacing.config import SurfacingConfig, ToolSurfacingConfig


def profile(**search):
    return {
        "schema_version": 1,
        "config_state": "ok",
        "search": {
            "rrf_k": 60,
            "rrf_weights": [1.0, 1.0],
            "bm25_candidates": 50,
            "dense_candidates": 50,
            "enable_bm25": True,
            "enable_dense": True,
            "configured_mode": "hybrid",
            "effective_mode": "hybrid",
            **search,
        },
        "rerank": {"enabled": False},
        "dependencies": {},
        "missing_extras": [],
    }


def test_baseline_is_valid_without_changing_config():
    cfg = SurfacingConfig()
    original = cfg.model_dump()
    data = profile()
    snapshot = copy.deepcopy(data)
    result = rrf_boundary_doctor_checks(data, cfg)
    assert result[0][0] == "ltm_rrf_boundary"
    assert result[0][2] == "PASS"
    assert "configured min_score=0.017" in result[0][3]
    assert "Not a live score or auto-tuned threshold check" in result[0][3]
    assert result[0][4] is None
    assert cfg.model_dump() == original
    assert data == snapshot


def test_existing_precise_pin_can_pass_without_a_four_decimal_recommendation():
    data = profile(rrf_weights=[0.00909, 0.00909])
    cfg = SurfacingConfig(min_score=0.000155)
    check = rrf_boundary_doctor_checks(data, cfg)[0]
    assert check[2] == "PASS"
    assert check[4] is None


def test_model_cap_and_zero_pin_are_not_replaced_by_global_defaults():
    cfg = SurfacingConfig(
        max_results=100,
        context_tools={"read_file": ToolSurfacingConfig(min_score=0.0)},
    )
    # The existing model-budget method owns this clamp.
    cfg.consumer_model = "qwen2.5-coder:7b"
    from unittest.mock import patch

    with patch.object(SurfacingConfig, "_context_tokens", return_value=32000):
        checks = rrf_boundary_doctor_checks(profile(), cfg)
    assert "request top_k=4" in checks[0][3]
    assert checks[0][2] == "PASS"
    assert "configured min_score=0;" in checks[1][3]
    assert checks[1][2] == "WARN"


@pytest.mark.parametrize("score", [0.0164, 0.0182])
def test_a_floor_must_pass_both_raw_and_rounded_boundaries(score):
    check = rrf_boundary_doctor_checks(profile(), SurfacingConfig(min_score=score))[0]
    assert check[2] == "WARN"
    assert "Suggested" in check[3]


@pytest.mark.parametrize(
    "settings,expected",
    [
        ({"rrf_weights": [0.5, 0.5]}, 0.0087),
        ({"rrf_k": 100}, 0.0117),
        ({"bm25_candidates": 20, "dense_candidates": 80}, 0.0181),
    ],
)
def test_recommendation_separates_every_unmodified_rank(settings, expected):
    data = profile(**settings)
    cfg = SurfacingConfig(min_score=0.5)
    check = rrf_boundary_doctor_checks(data, cfg)[0]
    assert check[2] == "WARN"
    assert f"Suggested min_score={expected:.4f}" in check[3]
    search = data["search"]
    k = search["rrf_k"]
    w1, w2 = search["rrf_weights"]
    c1, c2 = search["bm25_candidates"], search["dense_candidates"]
    for score in [w1 / (k + r) for r in range(1, c1 + 1)] + [
        w2 / (k + r) for r in range(1, c2 + 1)
    ]:
        assert score < expected and round(score, 4) < expected
    for r1 in range(1, c1 + 1):
        for r2 in range(1, c2 + 1):
            score = w1 / (k + r1) + w2 / (k + r2)
            assert score >= expected and round(score, 4) >= expected
    assert '"min_score":' in check[4]
    assert "preserve other entries" in check[4]


@pytest.mark.parametrize(
    "settings,reason",
    [
        ({"bm25_candidates": 200, "dense_candidates": 200}, "No separating interval"),
        ({"rrf_weights": [10.0, 1.0]}, "No separating interval"),
        ({"rrf_weights": [0.001, 0.001]}, "No four-decimal threshold"),
        ({"rrf_weights": [1000.0, 1000.0]}, "within [0, 1]"),
    ],
)
def test_no_safe_pin_is_invented(settings, reason):
    check = rrf_boundary_doctor_checks(profile(**settings), SurfacingConfig())[0]
    assert check[2] == "WARN"
    assert reason in check[3]
    assert check[4] is None


@pytest.mark.parametrize(
    "settings",
    [
        {"rrf_weights": [-1, 1]},
        {"rrf_weights": [float("nan"), 1]},
        {"rrf_weights": [float("inf"), 1]},
        {"rrf_weights": [True, 1]},
        {"rrf_weights": [10**400, 1]},
        {"rrf_weights": [1]},
        {"rrf_weights": "[1, 1]"},
        {"rrf_k": True},
        {"rrf_k": 0},
        {"rrf_k": 10**400},
        {"bm25_candidates": -1},
        {"dense_candidates": 50.0},
    ],
)
def test_malformed_remote_fields_warn_without_a_pin(settings):
    check = rrf_boundary_doctor_checks(profile(**settings), SurfacingConfig())[0]
    assert check[2] == "WARN"
    assert check[4] is None


@pytest.mark.parametrize("data", [None, [], {}, {"schema_version": True}, profile(rrf_k=None)])
def test_unknown_profile_is_not_replaced_with_baseline(data):
    check = rrf_boundary_doctor_checks(data, SurfacingConfig())[0]
    assert check[2] == "WARN"
    assert "Suggested" not in check[3]


def test_old_profile_missing_one_field_is_unsupported():
    data = profile()
    del data["search"]["dense_candidates"]
    check = rrf_boundary_doctor_checks(data, SurfacingConfig())[0]
    assert check[2] == "WARN"
    assert "all four" in check[3]
    assert "restart" in check[4]


@pytest.mark.parametrize(
    "settings",
    [{"rrf_weights": [0, 1]}, {"enable_dense": False}, {"effective_mode": "bm25_only"}],
)
def test_single_leg_has_no_rrf_pin(settings):
    check = rrf_boundary_doctor_checks(profile(**settings), SurfacingConfig())[0]
    assert check[2] == "WARN"
    assert "Two positive-weight" in check[3]
    assert check[4] is None


def test_compact_requires_structured_before_any_pin():
    cfg = SurfacingConfig(result_format="compact")
    check = rrf_boundary_doctor_checks(profile(), cfg)[0]
    assert check[2] == "WARN"
    assert "RESULT_FORMAT=structured" in check[4]


@pytest.mark.parametrize("rerank", [True, None])
def test_possible_rerank_is_not_treated_as_rrf(rerank):
    data = profile()
    data["rerank"]["enabled"] = True
    check = rrf_boundary_doctor_checks(data, SurfacingConfig(rerank=rerank))[0]
    assert check[2] == "WARN" and "reranked" in check[3]
    assert "min_score" not in (check[4] or "")


def test_result_count_and_per_tool_overrides_use_engine_request_limits():
    cfg = SurfacingConfig(
        max_results=100,
        context_tools={
            "read_file": ToolSurfacingConfig(max_results=3, min_score=0.017),
            "ignored": ToolSurfacingConfig(enabled=False),
        },
    )
    checks = rrf_boundary_doctor_checks(profile(), cfg)
    assert len(checks) == 2
    assert checks[0][2] == "WARN" and "[200, 200]" in checks[0][3]
    assert checks[1][0] == "ltm_rrf_boundary:read_file"
    assert checks[1][2] == "PASS" and "request top_k=6" in checks[1][3]
    assert rrf_boundary_doctor_checks(None, SurfacingConfig(enabled=False)) == []


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("half_weights", [False, True])
def test_doctor_renders_passive_advice_without_saving(monkeypatch, tmp_path, as_json, half_weights):
    from memtomem_stm.cli import proxy
    from memtomem_stm.surfacing.feedback import AutoTuner

    config = tmp_path / "proxy.json"
    config.write_text(json.dumps({"enabled": True, "cache": {"tool_annotation_policy": "strict"}}))
    data = profile(rrf_weights=[0.5, 0.5] if half_weights else [1.0, 1.0])
    monkeypatch.setattr(
        proxy,
        "_ltm_status",
        lambda *a, **kw: {"connected": True, "runtime_profile": data, "route": "direct"},
    )
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH", str(tmp_path / "feedback.db"))
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__MIN_SCORE", "0.017")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__CONTEXT_TOOLS", "{}")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__ENABLED", "true")

    def forbidden(*a, **kw):
        pytest.fail("doctor must not construct a tuner")

    monkeypatch.setattr(AutoTuner, "__init__", forbidden)
    before = config.read_bytes()
    result = CliRunner().invoke(
        proxy.cli, ["doctor", "--config", str(config), *(["--json"] if as_json else [])]
    )
    assert result.exit_code == 0, result.output
    if as_json:
        payload = json.loads(result.output)
        checks = {c["id"]: c for c in payload["checks"]}
        assert checks["ltm_rrf_boundary"]["status"] == ("WARN" if half_weights else "PASS")
        assert payload["surfacing"]["rrf_boundary_checks"] == [checks["ltm_rrf_boundary"]]
    else:
        assert "ltm RRF boundary" in result.output
    assert ("Suggested min_score=0.0087" in result.output) is half_weights
    assert config.read_bytes() == before
    assert not (tmp_path / "feedback.db").exists()


@pytest.mark.asyncio
async def test_profile_crosses_real_mcp_and_negotiated_adapter(tmp_path):
    from memtomem_stm.cli.proxy import _probe_ltm_mcp_server
    from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

    path = tmp_path / "profile.json"
    data = profile(rrf_weights=[0.5, 0.5])
    path.write_text(json.dumps(data))
    args = [
        str(Path(__file__).with_name("_fake_memtomem_server.py")),
        "--runtime-profile",
        str(path),
    ]
    cfg = SurfacingConfig(ltm_mcp_command=sys.executable, ltm_mcp_args=args)
    adapter = McpClientSearchAdapter(cfg)
    await adapter.start()
    try:
        assert adapter.runtime_profile == data
    finally:
        await adapter.stop()
    with (tmp_path / "stderr.log").open("w+") as errlog:
        result = await _probe_ltm_mcp_server("stdio", sys.executable, args, "", None, 15, errlog)
    assert result["connected"] is True
    assert result["runtime_profile"] == data
    assert "0.0087" in rrf_boundary_doctor_checks(result["runtime_profile"], cfg)[0][3]


def test_daemon_ping_profile_is_used_without_direct_probe(monkeypatch):
    from memtomem_stm.cli import proxy
    from memtomem_stm.config import STMConfig
    from memtomem_stm.daemon import client

    data = profile(rrf_weights=[0.5, 0.5])

    async def ping(*a, **kw):
        return {"ltm": "warm", "core": {"runtime_profile": data}}

    def forbidden(*a, **kw):
        pytest.fail("daemon diagnostic must not start a private Core")

    monkeypatch.setattr(client, "ping", ping)
    monkeypatch.setattr(proxy, "_ltm_mcp_status", forbidden)
    cfg = STMConfig(surfacing=SurfacingConfig(use_daemon=True))
    result = proxy._ltm_status(cfg, 1)
    assert result["runtime_profile"] == data
    assert "0.0087" in rrf_boundary_doctor_checks(result["runtime_profile"], cfg.surfacing)[0][3]
