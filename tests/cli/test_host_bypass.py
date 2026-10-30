"""Report effective host registrations without importing them (#1269)."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from helpers import set_home
from memtomem_stm.cli import host_bypass as hb
from memtomem_stm.cli import proxy
from memtomem_stm.cli.proxy import cli

pytestmark = pytest.mark.real_codex_discovery


def record(name="plugin-server", **updates):
    return {
        "name": name,
        "enabled": True,
        "transport": {"type": "stdio", "command": "x", "args": ["canary-secret"]},
        **updates,
    }


@pytest.fixture
def probe(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND", "__missing_ltm__")
    monkeypatch.setenv("MMS_NO_TUI", "1")
    monkeypatch.setattr(hb.shutil, "which", lambda name: "/bin/codex" if name == "codex" else None)
    state = {"records": [record()], "error": None, "calls": []}

    def run(argv, **kwargs):
        if argv[-3:] != ["mcp", "list", "--json"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        state["calls"].append((argv, kwargs))
        if state["error"]:
            raise state["error"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(state["records"]), stderr="")

    monkeypatch.setattr(hb.subprocess, "run", run)
    return state


def test_effective_list_reports_only_extra_enabled_names(probe, tmp_path, capsys):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text('[mcp_servers.manual]\ncommand = "x"\n')
    probe["records"] = [
        record(),
        record(),
        record("manual"),
        record("disabled", enabled=False),
        record("memtomem-stm"),
        record("self", transport={"type": "stdio", "command": "mms"}),
        record("http-plugin", transport={"type": "streamable_http", "url": "https://canary-url"}),
        record("bad\nname"),
        record("bad-type", transport={"type": {}}),
        record("bad-command", transport={"type": "stdio", "command": 3}),
        None,
    ]
    assert hb.codex_extra_servers(tmp_path) == ["http-plugin", "plugin-server"]
    call, options = probe["calls"][0]
    assert call == ["/bin/codex", "mcp", "list", "--json"]
    assert options["cwd"] == str(tmp_path.resolve())
    assert options["timeout"] == 5
    assert options["stdin"] == subprocess.DEVNULL
    hb.echo_codex_extras(tmp_path)
    output = capsys.readouterr()
    assert output.out == ""
    assert "possibly plugin-provided; not imported" in output.err
    assert "canary" not in output.err


@pytest.mark.parametrize(
    "error", [OSError("canary-secret"), subprocess.TimeoutExpired("canary", 5)]
)
def test_probe_failure_is_nonfatal_and_does_not_echo_values(probe, tmp_path, capsys, error):
    probe["error"] = error
    assert hb.codex_extra_servers(tmp_path) == []
    output = capsys.readouterr()
    assert output.out == ""
    assert "additional registrations are not reported" in output.err
    assert "canary" not in output.err


def test_wrong_shape_is_nonfatal(probe, tmp_path, capsys):
    probe["records"] = {"canary": "secret"}
    assert hb.codex_extra_servers(tmp_path) == []
    assert "canary" not in capsys.readouterr().err


def test_absent_codex_does_not_probe(probe, tmp_path, monkeypatch):
    monkeypatch.setattr(hb.shutil, "which", lambda _: None)
    assert hb.codex_extra_servers(tmp_path) == []
    assert probe["calls"] == []


@pytest.mark.parametrize(
    "argv",
    [
        ["add", "--from-clients", "--all", "--json"],
        ["prune", "--all", "--json"],
        ["doctor", "--json"],
    ],
)
def test_proxy_commands_report_extras_without_importing(probe, tmp_path, monkeypatch, argv):
    monkeypatch.setattr(proxy, "_discover_candidates", lambda _: [])
    config = tmp_path / "proxy.json"
    config.write_text('{"upstream_servers": {}}')
    before = config.read_bytes()
    result = CliRunner().invoke(cli, [*argv, "--config", str(config)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    if argv[0] == "doctor":
        row = next(c for c in payload["checks"] if c["id"] == "codex_extra_servers")
        assert row["status"] == "WARN"
        assert "plugin-server" in row["detail"]
    else:
        assert "plugin-server" in result.stderr
    assert "canary" not in result.output
    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "argv",
    [
        ["import", "--from", "codex"],
        ["import", "--from", "codex", "--apply"],
        ["host", "status", "--json"],
        ["host", "scan", "--json"],
        ["host", "sync", "--json"],
    ],
)
def test_registry_commands_report_extras_without_mutating_registry(probe, tmp_path, argv):
    result = CliRunner().invoke(cli, argv)
    assert result.exit_code == 0, result.output
    if "--json" in argv:
        json.loads(result.stdout)
    assert "plugin-server" in result.stderr
    assert "canary" not in result.output
    assert not (tmp_path / ".mms/registry.toml").exists()


def test_host_filter_does_not_probe_other_hosts(probe):
    result = CliRunner().invoke(cli, ["import", "--from", "cursor"])
    assert result.exit_code == 0
    assert probe["calls"] == []


def test_init_reports_codex_extras(probe, tmp_path, monkeypatch):
    monkeypatch.setattr(proxy, "_discover_candidates", lambda _: [])
    result = CliRunner().invoke(
        cli,
        ["init", "--json", "--client", "skip", "--config", str(tmp_path / "proxy.json")],
        input="",
    )
    document = json.loads(result.stdout)
    assert document["error"] == "setup_failed"
    assert document["codex_extra_servers"] == ["plugin-server"]
    assert "canary" not in result.output
