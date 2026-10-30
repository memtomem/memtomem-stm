"""Best-effort host discovery survives decoder recursion failures (#1273)."""

import json

import pytest
from click.testing import CliRunner

from helpers import set_home
from memtomem_stm.cli.proxy import cli


@pytest.mark.parametrize("injected", [False, True], ids=["deep-file", "decoder-error"])
def test_add_from_clients_ignores_unreadable_host_json(tmp_path, monkeypatch, injected):
    set_home(monkeypatch, tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("MMS_NO_TUI", "1")
    monkeypatch.chdir(tmp_path)
    payload = "[" * 100_000 + "]" * 100_000
    host = tmp_path / ".claude.json"
    host.write_text(payload, encoding="utf-8")
    config = tmp_path / "proxy.json"
    config.write_text('{"upstream_servers": {}}', encoding="utf-8")
    calls = []
    real_loads = json.loads

    def loads(text, *args, **kwargs):
        if text == payload:
            calls.append(text)
            if injected:
                raise RecursionError("decoder recursion limit")
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", loads)
    result = CliRunner().invoke(cli, ["add", "--from-clients", "--config", str(config)])

    assert calls, "the real discovery path must read the host config"
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "No MCP servers found" in result.output
    assert host.read_text(encoding="utf-8") == payload
    assert json.loads(config.read_text(encoding="utf-8")) == {"upstream_servers": {}}
