"""Health and doctor must agree with startup on proxy configuration validity (#1075).

The #1051 fix moved ``list`` and ``status`` onto the startup-aware runtime
read; these pin the same verdict and the same value-free errors for the two
remaining read-only diagnostics.
"""

import json

import pytest
from click.testing import CliRunner

from helpers import set_home
from memtomem_stm.cli import proxy as proxy_mod
from memtomem_stm.cli.proxy import cli
from memtomem_stm.proxy.staged_status import ProbeStage, StagedProbeResult

_REJECTED_ENVIRONMENTS = [
    ([("MEMTOMEM_STM_PROXY", "[]")], "MEMTOMEM_STM_PROXY"),
    ([("MEMTOMEM_STM_PROXY", "[1]")], "MEMTOMEM_STM_PROXY"),
    ([("MEMTOMEM_STM_PROXY", "{")], "MEMTOMEM_STM_PROXY"),
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
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    set_home(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND", "__missing_ltm__")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    async def fake_probe_servers(servers, timeout):
        return {
            name: StagedProbeResult(stage=ProbeStage.TOOLS_DISCOVERED, tools=1)
            for name in servers
        }

    monkeypatch.setattr(proxy_mod, "_probe_servers", fake_probe_servers)


def _write_healthy(path):
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "cache": {"tool_annotation_policy": "strict"},
                "upstream_servers": {"s": {"prefix": "s", "command": "echo"}},
            }
        )
    )


def _schema_check(output):
    checks = json.loads(output)["checks"]
    return next(check for check in checks if check["id"] == "config_schema")


@pytest.mark.parametrize("env_items,expected_var", _REJECTED_ENVIRONMENTS)
def test_invalid_environment_agrees_with_startup(tmp_path, monkeypatch, env_items, expected_var):
    path = tmp_path / "proxy.json"
    _write_healthy(path)
    for name, value in env_items:
        monkeypatch.setenv(name, value)
    runner = CliRunner()

    status = runner.invoke(cli, ["status", "--config", str(path), "--json"])
    assert json.loads(status.output)["config_valid"] is False

    result = runner.invoke(cli, ["health", "--config", str(path), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["config_valid"] is False
    assert expected_var in data["config_error"]
    assert "secret-not-a-list" not in result.output
    human = runner.invoke(cli, ["health", "--config", str(path)])
    assert human.exit_code == 0, human.output
    assert expected_var in human.output
    assert "secret-not-a-list" not in human.output
    assert "server cannot start" in human.output
    assert "config file present but fails validation" not in human.output

    result = runner.invoke(cli, ["doctor", "--config", str(path), "--json"])
    assert result.exit_code == 1, result.output
    check = _schema_check(result.output)
    assert check["status"] == "FAIL"
    assert "server cannot start" in check["detail"]
    assert expected_var in check["detail"]
    # The file is valid, so `config validate` would pass; name the variables.
    assert "MEMTOMEM_STM_*" in check["next_action"]
    assert "config validate" not in check["next_action"]
    assert "secret-not-a-list" not in result.output
    human = runner.invoke(cli, ["doctor", "--config", str(path)])
    assert human.exit_code == 1, human.output
    assert "secret-not-a-list" not in human.output
    schema_row = next(line for line in human.output.splitlines() if "config schema" in line)
    assert "FAIL" in schema_row
    assert expected_var in schema_row


@pytest.mark.parametrize("command", ["health", "doctor"])
@pytest.mark.parametrize("json_output", [False, True])
def test_invalid_file_does_not_log_values(tmp_path, caplog, command, json_output):
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "upstream_servers": {
                    "a": {"prefix": "tok_SECRET", "command": "echo"},
                    "b": {"prefix": "tok_SECRET", "command": "echo"},
                },
            }
        )
    )
    args = [command, "--config", str(path)]
    if json_output:
        args.append("--json")
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == (1 if command == "doctor" else 0), result.output
    assert "tok_SECRET" not in result.stderr
    assert "input_value" not in result.stderr
    assert "tok_SECRET" not in caplog.text
    assert "input_value" not in caplog.text
    if command == "health":
        # Health never renders prefixes, so the whole report must be clean.
        assert "tok_SECRET" not in result.stdout
        if json_output:
            data = json.loads(result.stdout)
            assert data["config_valid"] is False
            assert data["config_error"] == "1 validation error(s): value_error"
        else:
            warning = result.stdout.splitlines()[0]
            assert "mms config validate" in warning
    elif json_output:
        # Doctor's separate ``prefixes`` check names the colliding prefix by
        # design; only the schema check is a validation error.
        check = _schema_check(result.stdout)
        assert check["status"] == "FAIL"
        assert check["detail"].endswith(": 1 validation error(s): value_error")
        assert "tok_SECRET" not in check["detail"]
    else:
        schema_row = next(line for line in result.stdout.splitlines() if "config schema" in line)
        assert "FAIL" in schema_row
        assert "tok_SECRET" not in schema_row
        assert "mms config validate" in schema_row


def test_env_override_that_repairs_the_file_is_valid(tmp_path, monkeypatch):
    """Positive control: the runtime read, not a raw-file check, decides."""
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            {
                "enabled": "not-a-boolean",
                "cache": {"tool_annotation_policy": "strict"},
                "upstream_servers": {"s": {"prefix": "s", "command": "echo"}},
            }
        )
    )
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__ENABLED", "true")
    runner = CliRunner()

    result = runner.invoke(cli, ["health", "--config", str(path), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["config_valid"] is True
    assert data["config_error"] is None

    result = runner.invoke(cli, ["doctor", "--config", str(path), "--json"])
    assert _schema_check(result.output)["status"] == "PASS"


def test_doctor_rechecks_the_snapshot_its_later_checks_use(tmp_path, monkeypatch):
    """Doctor reads the file before the runtime read; an edit in between must
    FAIL the schema check, not crash the report."""
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "upstream_servers": {
                    "a": {"prefix": "tok_SECRET", "command": "echo"},
                    "b": {"prefix": "tok_SECRET", "command": "echo"},
                },
            }
        )
    )
    monkeypatch.setattr(
        proxy_mod,
        "_runtime_proxy_read",
        lambda *_args, **_kwargs: proxy_mod._RuntimeProxyRead(None, None, frozenset()),
    )
    result = CliRunner().invoke(cli, ["doctor", "--config", str(path), "--json"])
    assert result.exit_code == 1, result.output
    check = _schema_check(result.output)
    assert check["status"] == "FAIL"
    assert check["detail"].endswith(": 1 validation error(s): value_error")
    assert "config validate" in check["next_action"]
