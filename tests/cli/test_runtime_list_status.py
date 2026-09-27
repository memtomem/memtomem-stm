"""List and status must agree with startup on proxy environment parsing (#1051)."""

import json

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import cli


@pytest.mark.parametrize(
    "env_items,expected_var",
    [
        ([("MEMTOMEM_STM_PROXY", "[]")], "MEMTOMEM_STM_PROXY"),
        (
            [
                ("MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS", "secret-not-a-list"),
                ("MEMTOMEM_STM_PROXY__TOOLGRAPH", '{"args":["serve"]}'),
            ],
            "MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS",
        ),
        (
            [
                ("MEMTOMEM_STM_PROXY__TOOLGRAPH", '{"args":["serve"]}'),
                ("MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS", "secret-not-a-list"),
            ],
            "MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS",
        ),
    ],
)
def test_invalid_environment_agrees_with_startup(tmp_path, monkeypatch, env_items, expected_var):
    path = tmp_path / "proxy.json"
    path.write_text(json.dumps({"upstream_servers": {"s": {"prefix": "s", "command": "echo"}}}))
    for name, value in env_items:
        monkeypatch.setenv(name, value)
    runner = CliRunner()
    for command in ("list", "status"):
        result = runner.invoke(cli, [command, "--config", str(path), "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["config_valid"] is False
        assert expected_var in data["config_error"]
        assert "secret-not-a-list" not in result.output
        if command == "list":
            assert data["effective_compression"] == {}
            assert data["effective_servers"] == {}
        else:
            assert data["effective_server_count"] is None
        human = runner.invoke(cli, [command, "--config", str(path)])
        assert human.exit_code == 0, human.output
        assert expected_var in human.output
        assert "secret-not-a-list" not in human.output
        if command == "list":
            row = next(line for line in human.output.splitlines() if line.startswith("s "))
            assert row.split()[3] == "unknown"


def test_env_only_and_mixed_upstreams_are_listed_without_changing_raw_map(tmp_path, monkeypatch):
    path = tmp_path / "proxy.json"
    raw = {"enabled": True, "upstream_servers": {"s": {"prefix": "s", "command": "echo"}}}
    path.write_text(json.dumps(raw))
    monkeypatch.setenv(
        "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS",
        json.dumps({"e": {"prefix": "e", "command": "echo"}}),
    )
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__S__COMPRESSION", "none")
    runner = CliRunner()
    listed = runner.invoke(cli, ["list", "--config", str(path), "--json"])
    assert listed.exit_code == 0, listed.output
    data = json.loads(listed.output)
    assert data["config_valid"] is True
    assert data["servers"] == raw["upstream_servers"]
    assert set(data["effective_servers"]) == {"s", "e"}
    assert data["server_sources"] == {"s": "file+env", "e": "env"}
    assert data["effective_compression"]["s"]["strategy"] == "none"
    table = runner.invoke(cli, ["list", "--config", str(path)])
    assert table.exit_code == 0, table.output
    assert "SOURCE" in table.output
    assert "file+env" in table.output
    assert any(line.startswith("e ") and " env " in line for line in table.output.splitlines())
    status = runner.invoke(cli, ["status", "--config", str(path), "--json"])
    summary = json.loads(status.output)
    assert summary["config_valid"] is True
    assert summary["server_count"] == 1
    assert summary["effective_server_count"] == 2
    assert "2 effective (1 file)" in runner.invoke(cli, ["status", "--config", str(path)]).output


def test_effective_server_summary_does_not_print_env_secrets(tmp_path, monkeypatch):
    path = tmp_path / "proxy.json"
    path.write_text('{"enabled": true, "upstream_servers": {}}')
    monkeypatch.setenv(
        "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS",
        json.dumps({"e": {"prefix": "e", "command": "echo", "env": {"TOKEN": "secret-value"}}}),
    )
    result = CliRunner().invoke(cli, ["list", "--config", str(path), "--json"])
    assert result.exit_code == 0, result.output
    assert "secret-value" not in result.output
    assert set(json.loads(result.output)["effective_servers"]) == {"e"}


@pytest.mark.parametrize("command", ["list", "status"])
@pytest.mark.parametrize("json_output", [False, True])
def test_invalid_file_does_not_log_values(tmp_path, caplog, command, json_output):
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            {
                "upstream_servers": {
                    "a": {"prefix": "tok_SECRET", "command": "echo"},
                    "b": {"prefix": "tok_SECRET", "command": "echo"},
                }
            }
        )
    )
    args = [command, "--config", str(path)]
    if json_output:
        args.append("--json")
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert "tok_SECRET" not in result.stderr
    assert "input_value" not in result.stderr
    assert "tok_SECRET" not in caplog.text
    assert "input_value" not in caplog.text
    if json_output:
        data = json.loads(result.stdout)
        assert data["config_valid"] is False
        assert data["config_error"] == "1 validation error(s): value_error"
    else:
        warning = result.stdout.splitlines()[0]
        assert "tok_SECRET" not in warning
        assert "mms config validate" in warning


def test_settings_error_names_candidates_across_config_sections(tmp_path, monkeypatch):
    path = tmp_path / "proxy.json"
    path.write_text('{"upstream_servers": {}}')
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS", "secret-bad-args")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__EXCLUDE_TOOLS", "secret-[bad-json")
    result = CliRunner().invoke(cli, ["status", "--config", str(path), "--json"])
    assert result.exit_code == 0, result.output
    error = json.loads(result.stdout)["config_error"]
    assert "check one of:" in error
    assert "MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS" in error
    assert "MEMTOMEM_STM_SURFACING__EXCLUDE_TOOLS" in error
    assert "secret-" not in error
