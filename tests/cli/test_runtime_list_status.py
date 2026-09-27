"""List and status must agree with startup on proxy environment parsing (#1051)."""

import json

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import cli


@pytest.mark.parametrize(
    "env_items,expected_var",
    [
        ([("MEMTOMEM_STM_PROXY", "[]")], "MEMTOMEM_STM_PROXY"),
        ([("MEMTOMEM_STM_PROXY", "[1]")], "MEMTOMEM_STM_PROXY"),
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
        assert "server cannot start" in human.output
        assert "config file present but fails validation" not in human.output
        assert human.output.count("runtime configuration invalid") == 1
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


def test_list_masks_environment_url_credentials_and_args(tmp_path, monkeypatch):
    path = tmp_path / "proxy.json"
    path.write_text('{"enabled": true, "upstream_servers": {}}')
    monkeypatch.setenv(
        "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS",
        json.dumps(
            {
                "remote": {
                    "prefix": "r",
                    "transport": "streamable_http",
                    "url": "https://user:tok123@h.example/mcp?api_key=zzz#fragsecret",
                },
                "local": {
                    "prefix": "l",
                    "command": "echo",
                    "args": ["--token", "secret-arg"],
                },
            }
        ),
    )
    result = CliRunner().invoke(cli, ["list", "--config", str(path)])
    assert result.exit_code == 0, result.output
    remote = next(line for line in result.stdout.splitlines() if line.startswith("remote "))
    local = next(line for line in result.stdout.splitlines() if line.startswith("local "))
    assert "https://h.example/mcp" in remote
    assert "tok123" not in result.stdout
    assert "api_key=zzz" not in result.stdout
    assert "fragsecret" not in result.stdout
    assert "[args hidden]" in local
    assert "secret-arg" not in result.stdout


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


def test_file_origin_marker_agrees_with_status_for_coerced_pruned_flag(tmp_path):
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "upstream_servers": {
                    "s": {
                        "prefix": "s",
                        "command": "echo",
                        "origin": {
                            "source": {"kind": "claude-user", "pruned": "true"},
                            "original": {"command": "echo"},
                        },
                    }
                },
            }
        )
    )
    runner = CliRunner()
    listed = runner.invoke(cli, ["list", "--config", str(path)])
    status = runner.invoke(cli, ["status", "--config", str(path)])
    assert listed.exit_code == status.exit_code == 0
    row = next(line for line in listed.stdout.splitlines() if line.startswith("s "))
    assert "claude-user" in row
    assert "claude-user*" not in row
    assert "host original pruned" not in listed.stdout
    assert "host-pruned" not in status.stdout


def test_invalid_file_shows_runtime_env_fallback_and_keeps_raw_file_map(tmp_path, monkeypatch):
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "default_compression": "invalid",
                "upstream_servers": {"a": {"prefix": "a", "command": "echo"}},
            }
        )
    )
    monkeypatch.setenv(
        "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS",
        json.dumps({"b": {"prefix": "b", "command": "echo"}}),
    )
    runner = CliRunner()
    listed = runner.invoke(cli, ["list", "--config", str(path), "--json"])
    assert listed.exit_code == 0, listed.output
    detail = json.loads(listed.stdout)
    assert detail["config_valid"] is False
    assert set(detail["servers"]) == {"a"}
    assert set(detail["effective_servers"]) == {"b"}
    assert detail["server_sources"] == {"b": "env"}
    human_list = runner.invoke(cli, ["list", "--config", str(path)])
    assert human_list.exit_code == 0, human_list.output
    assert any(line.startswith("b ") and " env " in line for line in human_list.stdout.splitlines())
    assert not any(line.startswith("a ") for line in human_list.stdout.splitlines())

    human = runner.invoke(cli, ["status", "--config", str(path)])
    assert human.exit_code == 0, human.stdout
    assert "Enabled: no (env/default fallback)" in human.stdout
    assert "Servers: 1 env/default fallback (1 file)" in human.stdout
    data = json.loads(runner.invoke(cli, ["status", "--config", str(path), "--json"]).stdout)
    assert data["config_valid"] is False
    assert data["server_count"] == 1
    assert data["effective_server_count"] == 1
    assert data["enabled"] is False


def test_invalid_file_keeps_startup_completed_env_server(tmp_path, monkeypatch):
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            {
                "default_compression": "invalid",
                "upstream_servers": {
                    "same": {
                        "prefix": "file",
                        "command": "file-command",
                        "origin": {"source": {"kind": "claude-user", "pruned": True}},
                    }
                },
            }
        )
    )
    monkeypatch.setenv(
        "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS",
        json.dumps({"same": {"prefix": "env", "command": "env-command"}}),
    )
    runner = CliRunner()
    detail = json.loads(runner.invoke(cli, ["list", "--config", str(path), "--json"]).stdout)
    assert detail["config_valid"] is False
    assert detail["servers"]["same"]["command"] == "file-command"
    assert detail["effective_servers"]["same"]["prefix"] == "env"
    assert detail["server_sources"] == {"same": "file+env"}
    human = runner.invoke(cli, ["list", "--config", str(path)])
    assert human.exit_code == 0, human.output
    row = next(line for line in human.stdout.splitlines() if line.startswith("same "))
    assert "env-command" in row
    assert "file-command" not in row
    assert "claude-user*" in row
