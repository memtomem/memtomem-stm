"""Explicit default-valued budgets must survive loading and CLI generation (#1040)."""

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import _normalize_client_entry, _save, cli
from memtomem_stm.proxy.config import (
    ProxyConfig,
    ToolOverrideConfig,
    UpstreamServerConfig,
    effective_max_result_chars,
)


@pytest.mark.parametrize("budget", [None, 7999, 8000, 8001])
def test_explicit_budget_survives_config_round_trip(tmp_path, budget):
    entry = {"prefix": "s", "command": "echo"}
    if budget is not None:
        entry["max_result_chars"] = budget
    raw = {"default_max_result_chars": 16000, "upstream_servers": {"s": entry}}
    cfg = ProxyConfig.model_validate(raw)
    path = tmp_path / "proxy.json"
    _save(path, cfg.model_dump(mode="json", exclude_unset=True))
    loaded = ProxyConfig.load_from_file(path)
    assert loaded is not None
    srv = loaded.upstream_servers["s"]
    assert ("max_result_chars" in srv.model_fields_set) == (budget is not None)
    assert effective_max_result_chars(srv, None, loaded) == (budget or 16000, None)


def test_environment_can_explicitly_set_default_budget(tmp_path):
    path = tmp_path / "proxy.json"
    _save(path, {"upstream_servers": {"s": {"prefix": "s", "command": "echo"}}})
    loaded = ProxyConfig.load_from_file(
        path, env_overrides={"upstream_servers": {"s": {"max_result_chars": "8000"}}}
    )
    assert loaded is not None
    assert effective_max_result_chars(loaded.upstream_servers["s"], None, loaded) == (8000, None)


def test_tool_and_token_precedence_still_apply():
    srv = UpstreamServerConfig(prefix="s", max_result_chars=8000, max_result_tokens=2000)
    cfg = ProxyConfig(chars_per_token=3.5)
    assert effective_max_result_chars(srv, None, cfg) == (7000, 2000)
    assert effective_max_result_chars(srv, ToolOverrideConfig(max_result_chars=1234), cfg) == (
        1234,
        None,
    )
    override = ToolOverrideConfig(max_result_chars=1234, max_result_tokens=500)
    assert effective_max_result_chars(srv, override, cfg) == (1750, 500)


def test_imported_server_uses_the_explicit_cli_budget():
    entry = _normalize_client_entry({"command": "echo"})
    assert entry is not None
    srv = UpstreamServerConfig.model_validate({**entry, "prefix": "s"})
    assert effective_max_result_chars(srv, None, ProxyConfig()) == (8000, None)


def test_add_default_budget_matches_the_generated_configuration(tmp_path):
    path = tmp_path / "proxy.json"
    result = CliRunner().invoke(
        cli, ["add", "s", "--prefix", "s", "--command", "echo", "--config", str(path)]
    )
    assert result.exit_code == 0, result.output
    cfg = ProxyConfig.load_from_file(path)
    assert cfg is not None
    assert effective_max_result_chars(cfg.upstream_servers["s"], None, cfg) == (8000, None)
