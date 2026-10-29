"""Claude Code plugin MCP servers reported as bypassing STM (#1108).

Report-only: these servers are never import candidates, and no field of a
server entry (command, args, env, headers, url) is printed anywhere. The probe
is the ``claude plugin list --json`` call behind the ``_run_claude_mcp`` seam;
every test scripts that seam, so none depends on the plugins of the machine
running it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from helpers import set_home
from memtomem_stm.cli import proxy as proxy_mod
from memtomem_stm.cli.proxy import cli

# The record `claude plugin list --json` returned for the Firebase plugin,
# enabled at local scope in a scratch directory (2026-10-29, Claude Code
# 2.1.312). ``projectPath`` names the directory it was installed from, not the
# one it is enabled in, and ``projectEnabled`` is false while ``enabled`` is
# true; installPath is shortened.
FIREBASE = {
    "id": "firebase@firebase",
    "version": "1.0.0",
    "scope": "local",
    "enabled": True,
    "installPath": "/home/u/.claude/plugins/cache/firebase/firebase/1.0.0",
    "projectPath": "/private/tmp/claude-vibe-coding",
    "projectEnabled": False,
    "mcpServers": {
        "firebase": {
            "command": "npx",
            "args": ["-y", "firebase-tools", "mcp", "--dir", "."],
            "env": {"IS_FIREBASE_MCP": "true"},
        }
    },
}

# Values that must never reach any output stream.
CANARIES = ("cnry-arg", "cnry-user", "cnry-pass", "cnry-query", "cnry-env", "cnry-header")


def _leaky_plugin() -> dict:
    return {
        "id": "leaky@market",
        "scope": "user",
        "enabled": True,
        "mcpServers": {
            "stdio-srv": {
                "command": "node",
                "args": ["server.js", "--token", "cnry-arg"],
                "env": {"API_KEY": "cnry-env"},
            },
            "http-srv": {
                "type": "http",
                "url": "https://cnry-user:cnry-pass@example.com/mcp?token=cnry-query",
                "headers": {"Authorization": "Bearer cnry-header"},
            },
        },
    }


def _result(stdout: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


@pytest.fixture
def probe(monkeypatch):
    """Script ``claude plugin list --json``: set ``state["out"]`` to the reply.

    ``out`` is a list of records (JSON-encoded), a raw string, a result object
    or an exception to raise. Calls are recorded with their ``cwd``.
    """
    state: dict = {"out": [], "calls": []}

    def fake_run(cmd, timeout=5, cwd=None):
        state["calls"].append({"cmd": list(cmd), "cwd": cwd})
        out = state["out"]
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, SimpleNamespace):
            return out
        if isinstance(out, str):
            return _result(out)
        return _result(json.dumps(out))

    monkeypatch.setattr(proxy_mod.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(proxy_mod, "_run_claude_mcp", fake_run)
    return state


pytestmark = pytest.mark.real_plugin_discovery


class TestPluginBypassServers:
    def test_firebase_record_is_one_row(self, probe, tmp_path):
        probe["out"] = [FIREBASE]

        rows = proxy_mod._plugin_bypass_servers(tmp_path)

        assert [row.as_json() for row in rows] == [
            {
                "plugin_id": "firebase@firebase",
                "server": "firebase",
                "runtime_name": "plugin:firebase:firebase",
                "transport": "stdio",
                "scope": "local",
            }
        ]

    def test_probe_runs_in_the_resolved_cwd(self, probe, tmp_path):
        probe["out"] = []
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)

        proxy_mod._plugin_bypass_servers(link)

        assert probe["calls"] == [
            {"cmd": ["claude", "plugin", "list", "--json"], "cwd": str(tmp_path.resolve())}
        ]

    def test_enabled_is_the_only_filter(self, probe, tmp_path):
        # A foreign projectPath with projectEnabled false is still live here
        # (measured); a disabled record is not, whatever its projectPath says.
        disabled = {**FIREBASE, "id": "off@m", "enabled": False, "projectPath": str(tmp_path)}
        probe["out"] = [FIREBASE, disabled]

        rows = proxy_mod._plugin_bypass_servers(tmp_path)

        assert [row.plugin_id for row in rows] == ["firebase@firebase"]

    def test_repeated_record_is_one_row_and_shared_keys_are_two(self, probe, tmp_path):
        other = {**FIREBASE, "id": "other@m", "scope": "user"}
        probe["out"] = [FIREBASE, {**FIREBASE, "scope": "project"}, other]

        rows = proxy_mod._plugin_bypass_servers(tmp_path)

        assert [(row.runtime_name, row.scope) for row in rows] == [
            ("plugin:firebase:firebase", "local"),
            ("plugin:other:firebase", "user"),
        ]

    def test_http_and_sse_transports(self, probe, tmp_path):
        probe["out"] = [
            {
                "id": "web@m",
                "scope": "user",
                "enabled": True,
                "mcpServers": {
                    "h": {"type": "http", "url": "https://example.com/mcp"},
                    "s": {"type": "sse", "url": "https://example.com/sse"},
                },
            }
        ]

        rows = proxy_mod._plugin_bypass_servers(tmp_path)

        assert {row.server: row.transport for row in rows} == {
            "h": "streamable_http",
            "s": "sse",
        }

    def test_stms_own_ltm_server_is_not_reported(self, probe, tmp_path):
        probe["out"] = [
            {
                "id": "memtomem@memtomem",
                "scope": "user",
                "enabled": True,
                "mcpServers": {
                    "memtomem": {
                        "command": "uvx",
                        "args": ["--from", "memtomem[onnx]==0.6.1", "memtomem-server"],
                    }
                },
            }
        ]

        assert proxy_mod._plugin_bypass_servers(tmp_path) == []

    @pytest.mark.parametrize(
        "scope",
        [{"headers": {"Authorization": "Bearer cnry-scope"}}, 1, None, "global", "user\n"],
        ids=["dict", "int", "missing", "unsupported", "control-char"],
    )
    def test_unsupported_scope_reads_unknown(self, probe, tmp_path, scope, capsys):
        record = {**FIREBASE, "scope": scope}
        if scope is None:
            del record["scope"]
        probe["out"] = [record]

        rows = proxy_mod._plugin_bypass_servers(tmp_path)
        proxy_mod._echo_plugin_bypass(rows, err=False)

        assert [row.scope for row in rows] == ["unknown"]
        assert "cnry-scope" not in json.dumps([row.as_json() for row in rows])
        assert "cnry-scope" not in capsys.readouterr().out

    def test_malformed_entries_are_skipped_without_hiding_valid_ones(self, probe, tmp_path):
        probe["out"] = [
            "not a record",
            {"id": 7, "enabled": True, "mcpServers": {"x": {"command": "a"}}},
            {"id": "ctl\x1b@m", "enabled": True, "mcpServers": {"x": {"command": "a"}}},
            {"id": "list@m", "enabled": True, "mcpServers": ["x"]},
            {
                "id": "bad@m",
                "enabled": True,
                "mcpServers": {
                    "type-int": {"type": 1, "command": "node"},
                    "command-int": {"command": 1},
                    "args-int": {"command": "node", "args": [1, None]},
                    "env-list": {"command": "node", "env": ["A=b"]},
                    "not-dict": "node",
                    "no-endpoint": {"type": "http"},
                    "surrogate-\udcff": {"command": "node"},
                },
            },
            FIREBASE,
        ]

        rows = proxy_mod._plugin_bypass_servers(tmp_path)

        assert sorted(row.runtime_name for row in rows) == [
            "plugin:bad:args-int",
            "plugin:bad:env-list",
            "plugin:firebase:firebase",
        ]

    @pytest.mark.parametrize(
        ("out", "note"),
        [
            (_result(returncode=1), "exited 1"),
            (subprocess.TimeoutExpired(["claude"], 5), "TimeoutExpired"),
            (FileNotFoundError(2, "No such file"), "FileNotFoundError"),
            ("{not json", "did not return a JSON list"),
            (json.dumps({"id": "x"}), "did not return a JSON list"),
            ("[" * 100_000 + "]" * 100_000, "did not return a JSON list"),
        ],
        ids=["rc1", "timeout", "oserror", "garbage", "not-a-list", "deep-nesting"],
    )
    def test_cli_failures_fail_open_with_a_note(self, probe, tmp_path, capsys, out, note):
        probe["out"] = out

        assert proxy_mod._plugin_bypass_servers(tmp_path) == []

        captured = capsys.readouterr()
        assert captured.out == ""
        assert note in captured.err
        assert "plugin MCP servers are not reported" in captured.err

    def test_no_claude_on_path_is_silent(self, probe, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(proxy_mod.shutil, "which", lambda name: None)

        assert proxy_mod._plugin_bypass_servers(tmp_path) == []

        assert probe["calls"] == []
        assert capsys.readouterr() == ("", "")


class TestEchoPluginBypass:
    def test_no_server_field_value_reaches_the_report(self, probe, tmp_path, capsys):
        probe["out"] = [_leaky_plugin(), FIREBASE]

        rows = proxy_mod._plugin_bypass_servers(tmp_path)
        proxy_mod._echo_plugin_bypass(rows, err=False)
        text = capsys.readouterr().out
        document = json.dumps([row.as_json() for row in rows])

        assert "plugin:leaky:http-srv (streamable_http, user)" in text
        assert "plugin:leaky:stdio-srv (stdio, user)" in text
        assert "plugin:firebase:firebase (stdio, local)" in text
        for canary in CANARIES:
            assert canary not in text
            assert canary not in document
        for field_value in ("server.js", "example.com", "API_KEY", "Authorization", "npx"):
            assert field_value not in text

    def test_nothing_printed_without_rows(self, capsys):
        proxy_mod._echo_plugin_bypass([], err=False)

        assert capsys.readouterr() == ("", "")

    @pytest.mark.parametrize("platform", ["linux", "win32"])
    def test_disable_command_is_shell_quoted(self, monkeypatch, capsys, platform):
        monkeypatch.setattr(sys, "platform", platform)
        row = proxy_mod._PluginBypass("evil;rm -rf ~&x@m", "s", "stdio", "user")

        proxy_mod._echo_plugin_bypass([row], err=False)

        expected = proxy_mod._shell_join(["claude", "plugin", "disable", "evil;rm -rf ~&x@m"])
        assert f"or turn the plugin off: {expected}" in capsys.readouterr().out
        assert expected != "claude plugin disable evil;rm -rf ~&x@m"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MMS_NO_TUI", "1")
    monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: [])
    return {"home": tmp_path, "cwd": proj}


def _config(home: Path, servers: dict | None = None) -> Path:
    path = home / "stm_proxy.json"
    path.write_text(json.dumps({"upstream_servers": servers or {}}), encoding="utf-8")
    return path


class TestCommandsReport:
    def test_add_from_clients_with_only_plugin_servers(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = _config(sandbox["home"])

        result = CliRunner().invoke(cli, ["add", "--from-clients", "--config", str(config)])

        assert result.exit_code == 0, result.output
        out = result.stdout
        assert "1 MCP server(s) registered by Claude Code plugins bypass STM" in out
        assert "plugin:firebase:firebase (stdio, local)" in out
        assert "No importable MCP servers found" in out
        assert out.index("bypass STM") < out.index("No importable MCP servers found")

    def test_add_from_clients_without_plugin_servers_keeps_its_message(self, sandbox, probe):
        probe["out"] = []
        config = _config(sandbox["home"])

        result = CliRunner().invoke(cli, ["add", "--from-clients", "--config", str(config)])

        assert result.exit_code == 0, result.output
        assert "No MCP servers found in Claude Desktop" in result.stdout
        assert "bypass STM" not in result.output

    def test_add_from_clients_json_keeps_stdout_one_document(self, sandbox, probe):
        probe["out"] = [_leaky_plugin()]
        config = _config(sandbox["home"])

        result = CliRunner().invoke(
            cli, ["add", "--from-clients", "--all", "--json", "--config", str(config)]
        )

        assert result.exit_code == 0, result.output
        document = json.loads(result.stdout)
        assert [row["runtime_name"] for row in document["plugin_bypass"]] == [
            "plugin:leaky:http-srv",
            "plugin:leaky:stdio-srv",
        ]
        assert "bypass STM" in result.stderr
        for canary in CANARIES:
            assert canary not in result.output

    def test_select_naming_a_plugin_server_says_why_it_is_unknown(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = _config(sandbox["home"])

        result = CliRunner().invoke(
            cli,
            ["add", "--from-clients", "--select", "firebase", "--json", "--config", str(config)],
        )

        assert result.exit_code == 1
        document = json.loads(result.stdout)
        assert document["error"] == "unknown_server"
        assert document["unknown"] == ["firebase"]
        assert [row["runtime_name"] for row in document["plugin_bypass"]] == [
            "plugin:firebase:firebase"
        ]
        assert "firebase is registered by a Claude Code plugin" in result.stderr

    def test_prune_with_nothing_dual_registered_still_reports(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = _config(
            sandbox["home"], {"fs": {"prefix": "fs", "transport": "stdio", "command": "npx"}}
        )

        text = CliRunner().invoke(cli, ["prune", "--all", "--config", str(config)])
        as_json = CliRunner().invoke(cli, ["prune", "--all", "--json", "--config", str(config)])

        assert text.exit_code == 0, text.output
        assert "plugin:firebase:firebase (stdio, local)" in text.stdout
        assert "No dual-registered upstreams found." in text.stdout
        assert as_json.exit_code == 0, as_json.output
        document = json.loads(as_json.stdout)
        assert document["planned"] == []
        assert [row["runtime_name"] for row in document["plugin_bypass"]] == [
            "plugin:firebase:firebase"
        ]
        assert "bypass STM" in as_json.stderr

    def test_prune_not_dual_registered_carries_the_rows(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = _config(
            sandbox["home"], {"fs": {"prefix": "fs", "transport": "stdio", "command": "npx"}}
        )

        result = CliRunner().invoke(cli, ["prune", "firebase", "--json", "--config", str(config)])

        assert result.exit_code == 1
        document = json.loads(result.stdout)
        assert document["error"] == "not_dual_registered"
        assert document["plugin_bypass"][0]["runtime_name"] == "plugin:firebase:firebase"

    def test_init_text_reports_before_the_manual_prompt(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = sandbox["home"] / "new.json"

        result = CliRunner().invoke(
            cli,
            ["init", "--no-validate", "--client", "skip", "--config", str(config)],
            input="filesystem\nfs\nstdio\nnpx\n-y @modelcontextprotocol/server-fs\n",
        )

        assert result.exit_code == 0, result.output
        assert "plugin:firebase:firebase (stdio, local)" in result.stdout
        assert result.stdout.index("bypass STM") < result.stdout.index("Server name")

    def test_init_json_carries_the_rows_on_success(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = sandbox["home"] / "new.json"

        result = CliRunner().invoke(
            cli,
            [
                "init",
                "--no-validate",
                "--client",
                "skip",
                "--lang",
                "en",
                "--json",
                "--config",
                str(config),
            ],
            input="filesystem\nfs\nstdio\nnpx\n-y @modelcontextprotocol/server-fs\n",
        )

        assert result.exit_code == 0, result.output
        document = json.loads(result.stdout)
        assert document["ok"] is True
        assert "filesystem" in json.loads(config.read_text(encoding="utf-8"))["upstream_servers"]
        assert [row["runtime_name"] for row in document["plugin_bypass"]] == [
            "plugin:firebase:firebase"
        ]

    @pytest.mark.parametrize(
        "argv",
        [["--demo"], ["--resume"]],
        ids=["demo", "resume"],
    )
    def test_init_json_is_null_when_it_never_asked(self, sandbox, probe, argv):
        # Neither path runs discovery, so ``[]`` would claim no plugin server
        # bypasses STM when nothing was checked.
        probe["out"] = [FIREBASE]
        config = _config(sandbox["home"]) if argv == ["--resume"] else sandbox["home"] / "n.json"

        result = CliRunner().invoke(
            cli,
            ["init", *argv, "--client", "skip", "--no-validate", "--lang", "en", "--json"]
            + ["--config", str(config)],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["plugin_bypass"] is None
        assert probe["calls"] == []

    def test_init_json_is_null_when_it_fails_before_discovery(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = _config(sandbox["home"])  # exists, no --resume → refused

        result = CliRunner().invoke(cli, ["init", "--json", "--config", str(config)])

        assert result.exit_code == 1
        assert json.loads(result.stdout)["plugin_bypass"] is None

    def test_init_json_keeps_the_rows_on_prompt_eof(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = sandbox["home"] / "new.json"

        result = CliRunner().invoke(
            cli, ["init", "--client", "skip", "--json", "--config", str(config)], input=""
        )

        assert result.exit_code != 0
        document = json.loads(result.stdout)
        assert document["error"] == "setup_failed"
        assert [row["runtime_name"] for row in document["plugin_bypass"]] == [
            "plugin:firebase:firebase"
        ]

    def test_init_json_keeps_the_rows_on_a_write_failure(self, sandbox, probe, monkeypatch):
        probe["out"] = [FIREBASE]
        config = sandbox["home"] / "new.json"

        def denied(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(proxy_mod, "atomic_write_text", denied)

        result = CliRunner().invoke(
            cli,
            [
                "init",
                "--no-validate",
                "--client",
                "skip",
                "--lang",
                "en",
                "--json",
                "--config",
                str(config),
            ],
            input="filesystem\nfs\nstdio\nnpx\n-y @modelcontextprotocol/server-fs\n",
        )

        assert result.exit_code != 0
        document = json.loads(result.stdout)
        assert document["error"] == "config_write_failed"
        assert [row["runtime_name"] for row in document["plugin_bypass"]] == [
            "plugin:firebase:firebase"
        ]

    def test_register_payload_does_not_gain_the_key(self, sandbox, probe):
        # Only ``init`` probes; the JSON decorator it shares with ``register``
        # must not add a field ``register`` never fills.
        probe["out"] = [FIREBASE]
        config = _config(sandbox["home"])

        result = CliRunner().invoke(
            cli, ["register", "--mcp", "json", "--json", "--config", str(config)]
        )

        document = json.loads(result.stdout)
        assert document["action"] == "register"
        assert "plugin_bypass" not in document
        assert probe["calls"] == []
