"""Explicit default-valued budgets must survive loading and CLI generation (#1040)."""

import json

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import _normalize_client_entry, _save, cli
from memtomem_stm.proxy.config import (
    ProxyConfig,
    ToolOverrideConfig,
    UpstreamServerConfig,
    collect_proxy_env_overrides,
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


def _load_with_env(path, environ):
    """Load *path* the way the server does: parse the environment, then overlay."""
    loaded = ProxyConfig.load_from_file(path, env_overrides=collect_proxy_env_overrides(environ))
    assert loaded is not None
    return loaded


def test_environment_can_explicitly_set_default_budget(tmp_path):
    """A real ``…__MAX_RESULT_CHARS=8000`` variable states the budget (#1040).

    Goes through ``collect_proxy_env_overrides`` rather than a pre-decoded
    fragment: the string-to-int decode and the settings-owned ``__`` explosion
    are the layers that could drop the field's provenance, so a test that hands
    ``load_from_file`` an already-shaped dict cannot see them regress.
    """
    path = tmp_path / "proxy.json"
    _save(path, {"upstream_servers": {"s": {"prefix": "s", "command": "echo"}}})
    loaded = _load_with_env(
        path, {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__S__MAX_RESULT_CHARS": "8000"}
    )
    assert effective_max_result_chars(loaded.upstream_servers["s"], None, loaded) == (8000, None)


def test_environment_override_of_another_field_keeps_budget_omitted(tmp_path):
    """Overriding a sibling field must not materialize the omitted budget (#1040).

    The overlay merges a partial server entry onto the file's; if that merge
    completed the entry from model defaults, the server would silently acquire
    an explicit 8000 and stop inheriting the global budget.
    """
    path = tmp_path / "proxy.json"
    _save(
        path,
        {
            "default_max_result_chars": 16000,
            "upstream_servers": {"s": {"prefix": "s", "command": "echo"}},
        },
    )
    loaded = _load_with_env(path, {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__S__PREFIX": "zz"})
    srv = loaded.upstream_servers["s"]
    assert srv.prefix == "zz"
    assert "max_result_chars" not in srv.model_fields_set
    assert effective_max_result_chars(srv, None, loaded) == (16000, None)


def test_cli_mutation_round_trip_preserves_both_provenances(tmp_path):
    """A CLI write of an unrelated server leaves both provenances intact (#1040).

    ``mms add`` mutates the raw config dict and rewrites the whole file, so it
    is the shipping path that would erase omission — a rewrite that dumped the
    validated model instead would hand every server an explicit 8000.
    """
    path = tmp_path / "proxy.json"
    _save(
        path,
        {
            "default_max_result_chars": 16000,
            "upstream_servers": {
                "omit": {"prefix": "o", "command": "echo"},
                "expl": {"prefix": "e", "command": "echo", "max_result_chars": 8000},
            },
        },
    )
    result = CliRunner().invoke(
        cli, ["add", "new", "--prefix", "n", "--command", "echo", "--config", str(path)]
    )
    assert result.exit_code == 0, result.output

    written = json.loads(path.read_text(encoding="utf-8"))["upstream_servers"]
    # Positive control: without the rewrite the provenance assertions below
    # would hold against the untouched file and prove nothing.
    assert "new" in written
    assert "max_result_chars" not in written["omit"]
    assert written["expl"]["max_result_chars"] == 8000

    cfg = ProxyConfig.load_from_file(path)
    assert cfg is not None
    assert effective_max_result_chars(cfg.upstream_servers["omit"], None, cfg) == (16000, None)
    assert effective_max_result_chars(cfg.upstream_servers["expl"], None, cfg) == (8000, None)


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


def test_imported_server_states_no_budget_of_its_own():
    """Client import must not pin a budget the source entry never stated (#1040).

    ``_normalize_client_entry`` used to write a literal 8000, which read as
    omission only because the resolver compared against the field default.
    Under provenance that literal would become a stated budget, halving the
    inherited one — so the generator omits the field instead.
    """
    entry = _normalize_client_entry({"command": "echo"})
    assert entry is not None
    assert "max_result_chars" not in entry
    srv = UpstreamServerConfig.model_validate({**entry, "prefix": "s"})
    cfg = ProxyConfig(default_max_result_chars=16000)
    assert effective_max_result_chars(srv, None, cfg) == (16000, None)


def test_add_without_the_flag_leaves_the_budget_inherited(tmp_path):
    """``mms add`` writes no budget unless ``--max-chars`` was passed (#1040)."""
    path = tmp_path / "proxy.json"
    result = CliRunner().invoke(
        cli, ["add", "s", "--prefix", "s", "--command", "echo", "--config", str(path)]
    )
    assert result.exit_code == 0, result.output
    assert (
        "max_result_chars"
        not in json.loads(path.read_text(encoding="utf-8"))["upstream_servers"]["s"]
    )
    cfg = ProxyConfig.load_from_file(path)
    assert cfg is not None
    assert cfg.default_max_result_chars == 16000
    assert effective_max_result_chars(cfg.upstream_servers["s"], None, cfg) == (16000, None)


@pytest.mark.parametrize("budget", [7999, 8000, 8001])
def test_add_with_the_flag_pins_that_budget(tmp_path, budget):
    """``--max-chars 8000`` is as stated as any other value (#1040)."""
    path = tmp_path / "proxy.json"
    result = CliRunner().invoke(
        cli,
        [
            "add",
            "s",
            "--prefix",
            "s",
            "--command",
            "echo",
            "--max-chars",
            str(budget),
            "--config",
            str(path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (
        json.loads(path.read_text(encoding="utf-8"))["upstream_servers"]["s"]["max_result_chars"]
        == budget
    )
    cfg = ProxyConfig.load_from_file(path)
    assert cfg is not None
    assert effective_max_result_chars(cfg.upstream_servers["s"], None, cfg) == (budget, None)
