"""Tests for ``memtomem_stm.cli.proxy`` — the user-facing proxy management CLI.

The CLI has no dedicated tests (issue #73). Logic worth locking in:

- ``_load`` config-corruption handling (silently broken CLI would otherwise
  give no error signal).
- ``add``'s security-relevant validations — dangerous env keys, duplicate
  prefixes, prefix format rules.
- Golden-path end-to-end flow: ``add`` → ``list``/``status`` → ``remove``.

Uses ``click.testing.CliRunner`` and a tmp path for the config — no real
home directory is touched.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import cli

_FAKE_SERVER = Path(__file__).resolve().parents[1] / "_fake_memtomem_server.py"


@pytest.fixture
def config(tmp_path: Path) -> Path:
    """Fresh config path inside a tmp dir — never collides with $HOME."""
    return tmp_path / "stm_proxy.json"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _cfg_args(config: Path) -> list[str]:
    """Shared ``--config`` flag for sub-tests."""
    return ["--config", str(config)]


# ── version command ─────────────────────────────────────────────────────


class TestVersion:
    def test_prints_package_version(self, runner):
        result = runner.invoke(cli, ["version"])
        assert result.exit_code == 0
        assert "memtomem-stm" in result.output
        assert result.output.strip().startswith("memtomem-stm ")


# ── style helpers / NO_COLOR contract ───────────────────────────────────


class TestStyleHelpers:
    """The style helpers are tiny but they enforce two contracts that CLI
    users rely on: (1) each signal uses a distinct SGR combo so a screen-
    reader / grep user can tell errors from warnings; (2) NO_COLOR disables
    everything per the https://no-color.org spec, including empty-string.

    Click 8.3 doesn't honor NO_COLOR on real TTYs, so we enforce it in the
    helpers. A regression here would silently re-break that promise."""

    def test_color_emits_expected_sgr(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        from memtomem_stm.cli.proxy import _bad, _err, _hdr, _ok, _warn

        assert _err("X") == "\x1b[31m\x1b[1mX\x1b[0m"  # red + bold
        assert _bad("X") == "\x1b[31mX\x1b[0m"  # red only
        assert _warn("X") == "\x1b[33mX\x1b[0m"  # yellow
        assert _ok("X") == "\x1b[32mX\x1b[0m"  # green
        assert _hdr("X") == "\x1b[1mX\x1b[0m"  # bold only

    @pytest.mark.parametrize("value", ["1", "", "anything"])
    def test_no_color_any_value_disables(self, monkeypatch, value):
        """Per no-color.org: presence of NO_COLOR (any value, incl. empty)
        disables color."""
        monkeypatch.setenv("NO_COLOR", value)
        from memtomem_stm.cli.proxy import _bad, _err, _hdr, _ok, _warn

        for fn in (_err, _warn, _ok, _bad, _hdr):
            assert fn("X") == "X"


# ── _load / config-corruption paths ──────────────────────────────────────


class TestConfigLoad:
    def test_status_handles_missing_config_gracefully(self, runner, config):
        """No config file → friendly hint, no crash."""
        result = runner.invoke(cli, ["status", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "Config not found" in result.output
        assert "mms add" in result.output

    def test_corrupt_json_surfaces_error_not_silent(self, runner, config):
        """Malformed JSON must fail with a message, not return a default dict."""
        config.write_text("{this is not: json", encoding="utf-8")
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 1
        assert "Failed to parse" in result.output

    @pytest.mark.parametrize(
        "payload,expected_type",
        [("[]", "list"), ("null", "NoneType"), ('"oops"', "str"), ("42", "int")],
    )
    def test_rejects_non_dict_top_level(self, runner, config, payload, expected_type):
        """Valid JSON that isn't an object (list/null/string/int) used to crash the
        CLI with an AttributeError traceback. Guard surfaces a clean error."""
        config.write_text(payload, encoding="utf-8")
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 1
        assert "top-level must be a JSON object" in result.output
        assert expected_type in result.output

    def test_rejects_non_dict_upstream_servers(self, runner, config):
        """`upstream_servers` must be a dict — a list/string here would crash
        downstream iteration with an AttributeError traceback."""
        config.write_text(json.dumps({"upstream_servers": "oops"}), encoding="utf-8")
        result = runner.invoke(cli, ["status", *_cfg_args(config)])
        assert result.exit_code == 1
        assert "'upstream_servers' must be an object" in result.output

    def test_list_empty_config(self, runner, config):
        """No config → ``list`` prints the empty-state message and exits 0."""
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "No upstream servers configured" in result.output


# ── status command ───────────────────────────────────────────────────────


class TestStatus:
    def test_shows_config_path_and_enabled_state(self, runner, config):
        config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["status", *_cfg_args(config)])
        assert result.exit_code == 0
        assert str(config) in result.output
        assert "Enabled: yes" in result.output
        assert "Servers: 0" in result.output

    def test_json_output(self, runner, config):
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {
                        "fs": {"prefix": "fs", "command": "uvx", "args": ["mcp-server-fs"]},
                    },
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["status", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["enabled"] is True
        assert "fs" in data["servers"]
        assert str(config) in data["config_path"]

    def test_json_missing_config(self, runner, config):
        result = runner.invoke(cli, ["status", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["error"] == "config_not_found"

    def test_shows_server_details(self, runner, config):
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {
                        "fs": {
                            "prefix": "fs",
                            "transport": "stdio",
                            "command": "uvx",
                            "args": ["mcp-server-fs"],
                            "compression": "auto",
                            "max_result_chars": 8000,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["status", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "fs" in result.output
        assert "prefix=fs" in result.output
        assert "uvx" in result.output
        assert "compression=auto" in result.output


# ── list command ─────────────────────────────────────────────────────────


class TestListServers:
    def test_list_one_stdio_server(self, runner, config):
        runner.invoke(
            cli,
            ["add", "fs", "--prefix", "fs", "--command", "uvx", *_cfg_args(config)],
        )
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "NAME" in result.output
        assert "fs" in result.output
        assert "stdio" in result.output
        assert "1 server(s) configured" in result.output

    def test_list_http_server_shows_url(self, runner, config):
        runner.invoke(
            cli,
            [
                "add",
                "remote",
                "--prefix",
                "rt",
                "--transport",
                "sse",
                "--url",
                "https://example.com/mcp",
                *_cfg_args(config),
            ],
        )
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert "example.com/mcp" in result.output


# ── add command — validation paths ───────────────────────────────────────


class TestAddValidation:
    def test_rejects_invalid_prefix_format(self, runner, config):
        result = runner.invoke(
            cli,
            ["add", "s", "--prefix", "1bad", "--command", "x", *_cfg_args(config)],
        )
        assert result.exit_code == 1
        assert "invalid prefix" in result.output

    def test_rejects_double_underscore_in_prefix(self, runner, config):
        """``__`` is the tool-namespace separator; user prefixes must not use it."""
        result = runner.invoke(
            cli,
            ["add", "s", "--prefix", "my__pref", "--command", "x", *_cfg_args(config)],
        )
        assert result.exit_code == 1
        assert "invalid prefix" in result.output

    def test_rejects_duplicate_server_name(self, runner, config):
        runner.invoke(
            cli,
            ["add", "fs", "--prefix", "fs", "--command", "x", *_cfg_args(config)],
        )
        result = runner.invoke(
            cli,
            ["add", "fs", "--prefix", "fs2", "--command", "y", *_cfg_args(config)],
        )
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_warns_on_duplicate_prefix_but_succeeds(self, runner, config):
        """Duplicate prefix is a warning, not an error — `add` still proceeds."""
        runner.invoke(
            cli,
            ["add", "fs1", "--prefix", "fs", "--command", "x", *_cfg_args(config)],
        )
        result = runner.invoke(
            cli,
            ["add", "fs2", "--prefix", "fs", "--command", "y", *_cfg_args(config)],
        )
        assert result.exit_code == 0
        assert "Warning" in result.output
        assert "already used by server 'fs1'" in result.output

    def test_stdio_requires_command(self, runner, config):
        result = runner.invoke(
            cli,
            ["add", "s", "--prefix", "s", *_cfg_args(config)],
        )
        assert result.exit_code == 1
        assert "--command is required" in result.output

    def test_sse_requires_url(self, runner, config):
        result = runner.invoke(
            cli,
            ["add", "s", "--prefix", "s", "--transport", "sse", *_cfg_args(config)],
        )
        assert result.exit_code == 1
        assert "--url is required" in result.output

    def test_env_requires_kv_format(self, runner, config):
        result = runner.invoke(
            cli,
            [
                "add",
                "s",
                "--prefix",
                "s",
                "--command",
                "x",
                "--env",
                "MALFORMED",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert "--env must be KEY=VALUE" in result.output

    def test_env_rejects_empty_key(self, runner, config):
        result = runner.invoke(
            cli,
            [
                "add",
                "s",
                "--prefix",
                "s",
                "--command",
                "x",
                "--env",
                "=value",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert "--env key must be non-empty" in result.output

    @pytest.mark.parametrize(
        "key",
        ["LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "NODE_OPTIONS"],
    )
    def test_env_blocks_dangerous_injection_keys(self, runner, config, key):
        """Security: these env vars can hijack spawned subprocesses.
        The block is the whole reason --env parsing exists as logic instead
        of a plain dict copy — a regression here would reopen an RCE vector."""
        result = runner.invoke(
            cli,
            [
                "add",
                "s",
                "--prefix",
                "s",
                "--command",
                "x",
                "--env",
                f"{key}=anything",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert "blocked for security reasons" in result.output
        assert key in result.output

    def test_env_dangerous_key_check_is_case_insensitive(self, runner, config):
        """The check upper-cases the key — a lowercase variant must also block."""
        result = runner.invoke(
            cli,
            [
                "add",
                "s",
                "--prefix",
                "s",
                "--command",
                "x",
                "--env",
                "ld_preload=bad.so",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert "blocked for security reasons" in result.output


class TestAtomicSave:
    """Direct exercises of ``_save``'s atomic-rename behaviour.

    A torn write would otherwise let the proxy's hot-reload watcher read
    a half-written JSON file (the previous ``Path.write_text`` path was
    truncate-then-write).
    """

    def test_save_writes_payload_and_leaves_no_temp_file(self, tmp_path: Path) -> None:
        from memtomem_stm.cli.proxy import _save

        target = tmp_path / "stm_proxy.json"
        _save(target, {"enabled": True, "upstream_servers": {}})

        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8")) == {
            "enabled": True,
            "upstream_servers": {},
        }
        # No leftover sibling temp files (mkstemp prefix matches target name)
        leftover = list(tmp_path.glob("stm_proxy.json.*.tmp"))
        assert leftover == []

    def test_save_overwrites_existing_atomically(self, tmp_path: Path) -> None:
        from memtomem_stm.cli.proxy import _save

        target = tmp_path / "stm_proxy.json"
        target.write_text('{"enabled": false, "upstream_servers": {}}\n', encoding="utf-8")
        old_inode = target.stat().st_ino

        _save(target, {"enabled": True, "upstream_servers": {}})

        # Content updated
        assert json.loads(target.read_text(encoding="utf-8"))["enabled"] is True
        # On POSIX, atomic rename replaces the file (new inode); the old
        # inode is unlinked. Skip strict inode check on platforms where
        # this is not guaranteed (Windows), but assert content correctness
        # everywhere.
        if hasattr(os, "fsync"):  # POSIX
            new_inode = target.stat().st_ino
            assert new_inode != old_inode

    def test_save_cleans_up_temp_on_failure(self, tmp_path: Path, monkeypatch) -> None:
        """If ``os.replace`` fails, the sibling temp file must be removed."""
        from memtomem_stm.cli.proxy import _save

        target = tmp_path / "stm_proxy.json"

        def boom(*_a, **_kw):  # pragma: no cover - patched per test
            raise OSError("simulated rename failure")

        # ``_save`` was migrated to delegate to ``utils.fileio.atomic_write_text``;
        # the rename now happens there, so patch the helper's ``os.replace``.
        monkeypatch.setattr("memtomem_stm.utils.fileio.os.replace", boom)

        with pytest.raises(OSError, match="simulated rename"):
            _save(target, {"enabled": True})

        leftover = list(tmp_path.glob("stm_proxy.json.*.tmp"))
        assert leftover == []
        assert not target.exists()  # original was never written


class TestAddPersistence:
    def test_add_persists_full_entry(self, runner, config):
        result = runner.invoke(
            cli,
            [
                "add",
                "fs",
                "--prefix",
                "fs",
                "--command",
                "uvx",
                "--args",
                "mcp-server-fs --root /tmp",
                "--compression",
                "selective",
                "--max-chars",
                "4000",
                "--env",
                "FOO=bar",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 0

        data = json.loads(config.read_text(encoding="utf-8"))
        srv = data["upstream_servers"]["fs"]
        assert srv["prefix"] == "fs"
        assert srv["command"] == "uvx"
        assert srv["args"] == ["mcp-server-fs", "--root", "/tmp"]
        assert srv["compression"] == "selective"
        assert srv["max_result_chars"] == 4000
        assert srv["env"] == {"FOO": "bar"}
        # Config file is chmod 0o600 (best-effort — skip on platforms that
        # don't support it; the CLI silently ignores the OSError).
        assert config.exists()

    def test_add_malformed_args_fails_cleanly(self, runner, config):
        """shlex can't parse unterminated quotes — must error, not crash."""
        result = runner.invoke(
            cli,
            [
                "add",
                "fs",
                "--prefix",
                "fs",
                "--command",
                "uvx",
                "--args",
                "--unterminated 'quote",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert "malformed --args" in result.output


# ── add --validate (probe before save) ──────────────────────────────────


class TestAddValidate:
    """`add --validate` reuses the health probe to reject unreachable servers
    at config-write time. The contract is: probe fails → exit 1 and *no*
    entry written; probe succeeds → entry written and tool count reported."""

    def test_validate_unreachable_aborts_and_skips_write(self, runner, config):
        result = runner.invoke(
            cli,
            [
                "add",
                "bad",
                "--prefix",
                "bad",
                "--command",
                "__nonexistent_cmd_12345__",
                "--validate",
                "--timeout",
                "3",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert "validation failed" in result.output

        # Config file must not carry a partial entry.
        if config.exists():
            data = json.loads(config.read_text(encoding="utf-8"))
            assert "bad" not in data.get("upstream_servers", {})

    def test_validate_success_writes_entry_with_tool_count(self, config):
        """Probe against the repo's fake MCP server — should report tool count.

        Runs via real subprocess rather than ``CliRunner`` because Click's
        runner replaces ``sys.stderr`` with a buffer that has no ``fileno()``,
        and the underlying MCP stdio client passes that stderr to a child
        ``asyncio.create_subprocess_exec`` call — which requires a real
        file descriptor. The live ``mms`` binary doesn't hit this."""
        import subprocess

        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "from memtomem_stm.cli.proxy import cli; cli()",
                "add",
                "fake",
                "--prefix",
                "fk",
                "--command",
                sys.executable,
                "--args",
                str(_FAKE_SERVER),
                "--validate",
                "--timeout",
                "15",
                "--config",
                str(config),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        assert "Validated:" in proc.stdout
        assert "tool(s) reachable" in proc.stdout
        assert "Added server 'fake'" in proc.stdout

        data = json.loads(config.read_text(encoding="utf-8"))
        assert data["upstream_servers"]["fake"]["command"] == sys.executable

    def test_validate_flag_absent_skips_probe(self, runner, config):
        """Without --validate, a nonexistent command is accepted (probe opt-in)."""
        result = runner.invoke(
            cli,
            [
                "add",
                "lazy",
                "--prefix",
                "lz",
                "--command",
                "__nonexistent_cmd_12345__",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 0
        assert "Validated" not in result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert "lazy" in data["upstream_servers"]


# ── init command (guided setup) ─────────────────────────────────────────


@pytest.fixture
def no_discovery(monkeypatch):
    """Stub discovery to an empty list so manual-flow tests aren't
    contaminated by whatever MCP configs happen to exist on the host
    running the tests (dev machine, CI, etc.)."""
    from memtomem_stm.cli import proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: [])


class TestInit:
    """`mms init` is an interactive wizard. Two entry points:

    * **Import flow:** if discovery finds servers registered in other MCP
      clients (Claude Code, Claude Desktop, ``.mcp.json``), the user picks
      from a numbered list and is only prompted for a prefix per pick.
    * **Manual flow:** discovery empty (or user picks ``none``) → falls
      back to free-form prompts (name → prefix → transport → command/url).

    Tests that only exercise the manual flow use the ``no_discovery``
    fixture to force an empty candidate list.

    The MCP-client registration step (``_run_mcp_integration``) is stubbed
    out here so existing tests don't shell out to ``claude`` on the test
    machine. Dedicated coverage for that flow lives in
    ``TestInitMcpRegistration`` / ``TestRegisterCommand``."""

    @pytest.fixture(autouse=True)
    def _stub_mcp_integration(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_run_mcp_integration", lambda *_a, **_kw: None)

    def test_init_happy_path_stdio_no_validate(self, runner, config, no_discovery):
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="filesystem\nfs\nstdio\nnpx\n-y @modelcontextprotocol/server-fs\n",
        )
        assert result.exit_code == 0, result.output
        assert "Saved to:" in result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        srv = data["upstream_servers"]["filesystem"]
        assert srv["prefix"] == "fs"
        assert srv["transport"] == "stdio"
        assert srv["command"] == "npx"
        assert srv["args"] == ["-y", "@modelcontextprotocol/server-fs"]

    def test_init_aborts_if_config_exists(self, runner, config):
        """Init is for first-time setup only; `mms add` handles append."""
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["init", *_cfg_args(config)])
        assert result.exit_code == 1
        assert "config already exists" in result.output
        assert "mms add" in result.output
        assert "mms list" in result.output

    def test_init_invalid_prefix_reprompts(self, runner, config, no_discovery):
        """Bad prefix → re-ask instead of aborting; saves on the retry value."""
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="srv\n1bad\nfs\nstdio\ncmd\n\n",
        )
        assert result.exit_code == 0, result.output
        assert "Invalid: must start with a letter" in result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        assert data["upstream_servers"]["srv"]["prefix"] == "fs"

    def test_init_sse_transport_prompts_for_url(self, runner, config, no_discovery):
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="docs\ndocs\nsse\nhttps://docs.example.com/mcp\n",
        )
        assert result.exit_code == 0, result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        srv = data["upstream_servers"]["docs"]
        assert srv["transport"] == "sse"
        assert srv["url"] == "https://docs.example.com/mcp"
        assert "command" not in srv

    def test_init_validate_failure_still_saves_with_warning(self, config, tmp_path):
        """Validation is advisory: probe failure warns and continues (a flaky
        network shouldn't block setup). Uses a real subprocess to dodge
        CliRunner's stderr-fileno interaction (see TestAddValidate).

        ``HOME``/``cwd`` are redirected to an empty tmp dir so discovery
        finds no candidates and drops into the manual flow — otherwise the
        host's real ``~/.claude.json`` would leak in."""
        import subprocess

        isolated_home = tmp_path / "home"
        isolated_home.mkdir()
        isolated_cwd = tmp_path / "cwd"
        isolated_cwd.mkdir()

        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "from memtomem_stm.cli.proxy import cli; cli()",
                "init",
                "--config",
                str(config),
            ],
            # server name, prefix, transport, command, args, validate=y,
            # mcp-register choice = 3 (skip, prevents shelling out to the
            # host's real `claude` CLI / polluting cwd with .mcp.json)
            input="bad\nbad\nstdio\n__nonexistent_cmd_12345__\n\ny\n3\n",
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(isolated_cwd),
            env={**os.environ, "HOME": str(isolated_home)},
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        combined = proc.stdout + proc.stderr
        assert "probe failed" in combined
        assert "Saving config anyway" in combined
        assert "Saved to:" in proc.stdout

        data = json.loads(config.read_text(encoding="utf-8"))
        assert "bad" in data["upstream_servers"]


# ── init: discovery + import helpers ────────────────────────────────────


class TestInitDiscoveryHelpers:
    """Pure-function tests for discovery building blocks. Keep these fast
    and source-free (no file I/O) so the import flow can be reasoned about
    piece by piece."""

    def test_normalize_stdio_entry(self):
        from memtomem_stm.cli.proxy import _normalize_client_entry

        entry = _normalize_client_entry(
            {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]}
        )
        assert entry is not None
        assert entry["transport"] == "stdio"
        assert entry["command"] == "npx"
        assert entry["args"] == ["-y", "@modelcontextprotocol/server-filesystem"]
        # Imported entries get our default compression/max_chars policy.
        assert entry["compression"] == "auto"
        assert entry["max_result_chars"] == 8000
        # Prefix is intentionally absent — the caller prompts per-server.
        assert "prefix" not in entry

    def test_normalize_http_entry(self):
        from memtomem_stm.cli.proxy import _normalize_client_entry

        entry = _normalize_client_entry({"type": "http", "url": "https://example.com/mcp"})
        assert entry is not None
        assert entry["transport"] == "streamable_http"
        assert entry["url"] == "https://example.com/mcp"

    def test_normalize_sse_entry(self):
        from memtomem_stm.cli.proxy import _normalize_client_entry

        entry = _normalize_client_entry({"type": "sse", "url": "https://example.com/sse"})
        assert entry is not None
        assert entry["transport"] == "sse"

    def test_normalize_strips_dangerous_env(self):
        """Same policy as `mms add --env`: never import env keys that could
        hijack a spawned process."""
        from memtomem_stm.cli.proxy import _normalize_client_entry

        entry = _normalize_client_entry(
            {
                "command": "node",
                "args": ["srv.js"],
                "env": {"API_KEY": "ok", "LD_PRELOAD": "/evil.so"},
            }
        )
        assert entry is not None
        assert entry["env"] == {"API_KEY": "ok"}

    def test_normalize_rejects_unsupported_shape(self):
        """No command + no url + no recognized type → can't import."""
        from memtomem_stm.cli.proxy import _normalize_client_entry

        assert _normalize_client_entry({}) is None
        assert _normalize_client_entry({"type": "http"}) is None  # url missing
        assert _normalize_client_entry({"command": ""}) is None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", []),
            ("none", []),
            ("all", [0, 1, 2]),
            ("ALL", [0, 1, 2]),
            ("1", [0]),
            ("1,3", [0, 2]),
            (" 2 , 1 ", [1, 0]),  # preserves user's ordering
            ("1,1,2", [0, 1]),  # dedupes
        ],
    )
    def test_parse_selection_valid(self, raw, expected):
        from memtomem_stm.cli.proxy import _parse_selection

        assert _parse_selection(raw, 3) == expected

    @pytest.mark.parametrize("raw", ["0", "4", "1,4", "abc", "1-3", "1.5"])
    def test_parse_selection_invalid(self, raw):
        """Out-of-range, ranges, and non-digit tokens reprompt (None)."""
        from memtomem_stm.cli.proxy import _parse_selection

        assert _parse_selection(raw, 3) is None

    def test_suggest_prefix_sanitizes_and_dedupes(self):
        from memtomem_stm.cli.proxy import _suggest_prefix

        assert _suggest_prefix("filesystem", set()) == "filesystem"
        # dashes / dots → underscores
        assert _suggest_prefix("my-server.name", set()) == "my_server_name"
        # leading digit → prefixed so it matches _PREFIX_RE
        assert _suggest_prefix("123", set()) == "s_123"
        # collision → numbered suffix
        assert _suggest_prefix("fs", {"fs"}) == "fs2"
        assert _suggest_prefix("fs", {"fs", "fs2"}) == "fs3"

    def test_is_self_reference_blocks_recursion(self):
        """STM (mms/memtomem-stm) and the LTM companion (memtomem/
        memtomem-server) must never be imported as upstream servers.

        STM self-proxy → infinite loop on tool calls.
        LTM as upstream → double-registered, since STM already reaches LTM
        via a separate mechanism (CLAUDE.md pipeline invariants)."""
        from memtomem_stm.cli.proxy import _is_self_reference

        # STM itself (command basename).
        assert _is_self_reference({"command": "mms"})
        assert _is_self_reference({"command": "/usr/local/bin/memtomem-stm"})
        assert _is_self_reference({"command": "memtomem-stm-proxy"})
        # LTM companion (command basename).
        assert _is_self_reference({"command": "memtomem-server"})
        assert _is_self_reference({"command": "memtomem"})
        # LTM via uvx wrapper — the blocked name hides in args, not command.
        # This is the shape Claude Code writes: `uvx --from memtomem memtomem-server`.
        assert _is_self_reference(
            {"command": "uvx", "args": ["--from", "memtomem", "memtomem-server"]}
        )
        # Legitimate neighbor names must not be over-matched (substring
        # collisions would flag user-written `memtomem-foo-bar`).
        assert not _is_self_reference({"command": "npx"})
        assert not _is_self_reference({"url": "http://x"})
        assert not _is_self_reference({"command": "uvx", "args": ["--from", "memtomem-notes"]})


class TestInitDiscoverySources:
    """`_discover_candidates` reads three source files (project `.mcp.json`,
    `~/.claude.json`, Claude Desktop config) and dedupes by name in that
    priority order. Each test sets up a sandbox HOME + cwd to simulate a
    user with specific configs already present."""

    def _setup_home(self, tmp_path: Path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.setenv("HOME", str(home))
        # Redirect the macOS-specific Desktop path into our sandbox too.
        desktop = home / "Library/Application Support/Claude"
        desktop.mkdir(parents=True)
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(
            proxy_mod,
            "_desktop_config_path",
            lambda: desktop / "claude_desktop_config.json",
        )
        return home, cwd, desktop

    def test_discovers_claude_code_user_scope(self, tmp_path, monkeypatch):
        from memtomem_stm.cli.proxy import _discover_candidates

        home, cwd, _ = self._setup_home(tmp_path, monkeypatch)
        (home / ".claude.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "filesystem": {"command": "npx", "args": ["-y", "@fs"]},
                        "docs": {"type": "http", "url": "https://docs.example/mcp"},
                    }
                }
            ),
            encoding="utf-8",
        )

        cands = _discover_candidates(cwd)
        names = {c["name"] for c in cands}
        assert names == {"filesystem", "docs"}
        fs = next(c for c in cands if c["name"] == "filesystem")
        assert fs["source"] == "Claude Code (user)"
        assert fs["entry"]["transport"] == "stdio"

    def test_discovers_desktop_config(self, tmp_path, monkeypatch):
        from memtomem_stm.cli.proxy import _discover_candidates

        _, cwd, desktop = self._setup_home(tmp_path, monkeypatch)
        (desktop / "claude_desktop_config.json").write_text(
            json.dumps({"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}),
            encoding="utf-8",
        )

        cands = _discover_candidates(cwd)
        assert len(cands) == 1
        assert cands[0]["name"] == "fetch"
        assert cands[0]["source"] == "Claude Desktop"

    def test_discovers_project_mcp_json_in_cwd(self, tmp_path, monkeypatch):
        from memtomem_stm.cli.proxy import _discover_candidates

        _, cwd, _ = self._setup_home(tmp_path, monkeypatch)
        (cwd / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"dev-tool": {"command": "node", "args": ["x.js"]}}}),
            encoding="utf-8",
        )

        cands = _discover_candidates(cwd)
        assert len(cands) == 1
        assert cands[0]["source"] == ".mcp.json (project)"

    def test_dedupes_by_name_priority(self, tmp_path, monkeypatch):
        """Same name in multiple sources → first-priority wins, others
        recorded in ``duplicate_in`` so the UI can show the overlap."""
        from memtomem_stm.cli.proxy import _discover_candidates

        home, cwd, desktop = self._setup_home(tmp_path, monkeypatch)
        (cwd / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"shared": {"command": "a"}}}),
            encoding="utf-8",
        )
        (home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"shared": {"command": "b"}}}),
            encoding="utf-8",
        )
        (desktop / "claude_desktop_config.json").write_text(
            json.dumps({"mcpServers": {"shared": {"command": "c"}}}),
            encoding="utf-8",
        )

        cands = _discover_candidates(cwd)
        assert len(cands) == 1
        assert cands[0]["entry"]["command"] == "a"  # .mcp.json wins
        assert cands[0]["duplicate_in"] == ["Claude Code (user)", "Claude Desktop"]

    def test_skips_self_reference(self, tmp_path, monkeypatch):
        """Imports of STM (``mms``) or LTM (``memtomem-server``, either as
        direct command or under ``uvx --from memtomem``) are filtered so
        users never accidentally configure STM to proxy itself or to
        double-register the LTM companion."""
        from memtomem_stm.cli.proxy import _discover_candidates

        home, cwd, _ = self._setup_home(tmp_path, monkeypatch)
        (home / ".claude.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "mms-self": {"command": "mms"},
                        "memtomem": {
                            "command": "uvx",
                            "args": ["--from", "memtomem", "memtomem-server"],
                        },
                        "real": {"command": "npx", "args": ["x"]},
                    }
                }
            ),
            encoding="utf-8",
        )

        cands = _discover_candidates(cwd)
        assert [c["name"] for c in cands] == ["real"]

    def test_missing_and_malformed_sources_are_silent(self, tmp_path, monkeypatch):
        """Discovery is best-effort: a malformed JSON file shouldn't crash
        ``mms init`` — it just yields nothing from that source."""
        from memtomem_stm.cli.proxy import _discover_candidates

        home, cwd, _ = self._setup_home(tmp_path, monkeypatch)
        (home / ".claude.json").write_text("{ not json", encoding="utf-8")

        assert _discover_candidates(cwd) == []


class TestInitImportFlow:
    """End-to-end ``mms init`` when discovery finds candidates. We stub
    ``_discover_candidates`` directly to focus on the select + prefix UI;
    source parsing is covered in ``TestInitDiscoverySources``.

    Same ``_run_mcp_integration`` stub as ``TestInit`` — the MCP-client
    registration step is tested independently in ``TestInitMcpRegistration``."""

    @pytest.fixture(autouse=True)
    def _stub_mcp_integration(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_run_mcp_integration", lambda *_a, **_kw: None)

    def _stub_candidates(self, monkeypatch, candidates):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: candidates)

    def test_import_all_imports_every_candidate(self, runner, config, monkeypatch):
        """Default answer 'all' + accept suggested prefixes → every
        discovered server is imported with transport/command preserved."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "Claude Code (user)",
                    "entry": {
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "@fs"],
                        "compression": "auto",
                        "max_result_chars": 8000,
                    },
                },
                {
                    "name": "docs",
                    "source": "Claude Desktop",
                    "entry": {
                        "transport": "streamable_http",
                        "url": "https://docs.example/mcp",
                        "compression": "auto",
                        "max_result_chars": 8000,
                    },
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="all\n\n\n",  # pick all, accept default prefix for each
        )
        assert result.exit_code == 0, result.output
        assert "Found 2 MCP server" in result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        servers = data["upstream_servers"]
        assert set(servers) == {"filesystem", "docs"}
        assert servers["filesystem"]["command"] == "npx"
        assert servers["filesystem"]["prefix"] == "filesystem"  # default suggestion
        assert servers["docs"]["url"] == "https://docs.example/mcp"

    def test_import_subset_by_number(self, runner, config, monkeypatch):
        """Numeric picks only import the chosen servers — not all of them."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "a",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "ca"},
                },
                {
                    "name": "b",
                    "source": "Y",
                    "entry": {"transport": "stdio", "command": "cb"},
                },
                {
                    "name": "c",
                    "source": "Z",
                    "entry": {"transport": "stdio", "command": "cc"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="1,3\n\n\n",  # pick a + c, default prefixes
        )
        assert result.exit_code == 0, result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert set(data["upstream_servers"]) == {"a", "c"}

    def test_empty_selection_exits_without_saving(self, runner, config, monkeypatch):
        """When candidates are present but the user picks none (``none`` in
        fallback / Cancel in TUI / Confirm-with-nothing-toggled), we exit
        cleanly without writing a config. The previous behavior of
        auto-dropping into a 'Adding a server manually:' prompt was
        confusing — users who didn't pick anything almost never intended to
        type a new server inline. They either wanted to re-think (rerun
        init) or run ``mms add`` explicitly."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "Claude Code",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="none\n",
        )
        assert result.exit_code == 0, result.output
        assert "No servers selected" in result.output
        # The guidance points users at the two legitimate next steps.
        assert "rerun `mms init`" in result.output
        assert "mms add" in result.output
        # Nothing saved — a subsequent `init` should not see a pre-existing config.
        assert not config.exists()

    def test_invalid_selection_reprompts(self, runner, config, monkeypatch):
        """Out-of-range indices don't save garbage; they reprompt."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "one",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "x"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="5\n1\n\n",  # bad → retry with "1" → accept default prefix
        )
        assert result.exit_code == 0, result.output
        assert "Invalid:" in result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert "one" in data["upstream_servers"]

    def test_tty_select_loop_toggles_and_confirms(self, runner, config, monkeypatch):
        """TUI path uses ``questionary.select`` in a loop: Enter on an
        item toggles it, Enter on the Confirm sentinel commits. CliRunner
        can't fake a TTY, so we force ``_should_use_tui`` True and stub
        ``questionary.select`` to script a fixed sequence of user actions."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)

        # Sequence the fake user will perform: toggle 0, toggle 2, Confirm.
        # Each questionary.select() call returns the next scripted action.
        actions = iter([0, 2, proxy_mod._TUI_CONFIRM])
        observed_choice_counts: list[int] = []

        class _Scripted:
            def __init__(self, result):
                self._result = result

            def ask(self):
                return self._result

        def fake_select(message, choices, **kwargs):
            # Accept arbitrary kwargs (style, default, use_arrow_keys,
            # use_jk_keys, use_emacs_keys) so this stub survives future
            # param additions without brittle signature drift.
            observed_choice_counts.append(len(choices))
            return _Scripted(next(actions))

        import questionary

        monkeypatch.setattr(questionary, "select", fake_select)

        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "a",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "ca"},
                },
                {
                    "name": "b",
                    "source": "Y",
                    "entry": {"transport": "stdio", "command": "cb"},
                },
                {
                    "name": "c",
                    "source": "Z",
                    "entry": {"transport": "stdio", "command": "cc"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="\n\n",  # default prefix for each of the 2 picked servers
        )
        assert result.exit_code == 0, result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert set(data["upstream_servers"]) == {"a", "c"}  # index 1 ("b") not toggled
        # 3 iterations of the select loop: 2 toggles + 1 Confirm.
        assert len(observed_choice_counts) == 3
        # Each render shows: 3 candidates + Separator + Confirm + Cancel = 6 items.
        assert all(n == 6 for n in observed_choice_counts)

    def test_tui_cancel_exits_without_saving(self, runner, config, monkeypatch):
        """Cancel sentinel (or Ctrl-C → None from questionary) exits the
        wizard cleanly without writing a config. Contrast with the older
        behavior that silently dropped into a manual name prompt — users
        found that confusing when they just wanted to bail out."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)

        class _Scripted:
            def ask(self):
                return proxy_mod._TUI_CANCEL

        import questionary

        monkeypatch.setattr(questionary, "select", lambda *a, **kw: _Scripted())

        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "x",
                    "source": "src",
                    "entry": {"transport": "stdio", "command": "xx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="",  # no prompts should fire after Cancel
        )
        assert result.exit_code == 0, result.output
        assert "No servers selected" in result.output
        assert not config.exists()

    def test_tui_confirm_with_nothing_toggled_exits_without_saving(
        self, runner, config, monkeypatch
    ):
        """Confirm with an empty selection is semantically 'I'm done and
        I picked nothing' — same outcome as Cancel, since there's nothing
        to save."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)

        class _Scripted:
            def ask(self):
                return proxy_mod._TUI_CONFIRM

        import questionary

        monkeypatch.setattr(questionary, "select", lambda *a, **kw: _Scripted())

        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "x",
                    "source": "src",
                    "entry": {"transport": "stdio", "command": "xx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
        )
        assert result.exit_code == 0, result.output
        assert "No servers selected" in result.output
        assert not config.exists()

    def test_non_default_config_surfaces_management_hint(self, runner, config, monkeypatch):
        """When `--config` deviates from ``~/.memtomem/stm_proxy.json``
        the tail of the output must print ``mms list --config <path>``
        / ``mms health --config <path>`` so the user doesn't get tripped
        up by ``mms list`` silently reading the empty default config.
        Caught during dogfooding with a throwaway ``/tmp/*.json`` test
        path — the gap was confusing."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "only",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "c"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output
        assert "Manage this config:" in result.output
        # Hints must name the actual resolved path so the user can copy
        # them verbatim — no ``{path}`` placeholder leakage.
        assert f"mms list --config {config.resolve()}" in result.output
        assert f"mms health --config {config.resolve()}" in result.output

    def test_default_config_does_not_show_management_hint(self, runner, tmp_path, monkeypatch):
        """Inverse of the above: when the user accepts the default
        config path, the hint is noise and should not print. We stub
        ``_DEFAULT_CONFIG`` to a tmp path so this test can match the
        "default" case without actually writing to the user's real
        ``~/.memtomem/stm_proxy.json``."""
        from memtomem_stm.cli import proxy as proxy_mod

        fake_default = tmp_path / "default_stm_proxy.json"
        monkeypatch.setattr(proxy_mod, "_DEFAULT_CONFIG", fake_default)

        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "only",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "c"},
                },
            ],
        )
        # Invoke without --config so the command picks up the (patched) default.
        result = runner.invoke(
            cli,
            ["init", "--no-validate", "--config", str(fake_default)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output
        assert "Manage this config:" not in result.output

    def test_import_prompts_prefix_per_pick(self, runner, config, monkeypatch):
        """User-entered prefix overrides the suggested default, and each
        selected server gets its own prompt."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
                {
                    "name": "github",
                    "source": "Y",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="all\nfs\ngh\n\n",  # custom prefixes for both
        )
        assert result.exit_code == 0, result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert data["upstream_servers"]["filesystem"]["prefix"] == "fs"
        assert data["upstream_servers"]["github"]["prefix"] == "gh"


# ── MCP client registration (3-way prompt) ──────────────────────────────


class _FakeClaudeResult:
    """Lightweight stand-in for ``subprocess.CompletedProcess[str]``. Using
    a plain object avoids pinning the test to a specific stdlib version's
    constructor signature."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


class TestInitMcpRegistration:
    """The new 3-way prompt at the end of ``mms init`` (and the re-entry
    point ``mms register``): Claude Code auto-register / ``.mcp.json``
    generation / skip.

    These tests exercise the real ``_run_mcp_integration`` flow (no autouse
    stub) and replace ``_run_claude_mcp`` with an in-process recorder so
    assertions can check exact argv."""

    @pytest.fixture
    def no_discovery(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: [])

    @pytest.fixture
    def fake_claude(self, monkeypatch):
        """Record every ``_run_claude_mcp`` call and return scripted results.

        The fixture returns a dict ``{"calls": [...], "script": [...]}``; tests
        append to ``script`` before the CLI runs and read ``calls`` after.
        Default scripted result is exit 0 (registration succeeds)."""
        from memtomem_stm.cli import proxy as proxy_mod

        state: dict = {"calls": [], "script": []}

        def fake_run(cmd, timeout=5):
            state["calls"].append(list(cmd))
            if state["script"]:
                nxt = state["script"].pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt
            return _FakeClaudeResult(returncode=0)

        monkeypatch.setattr(proxy_mod, "_run_claude_mcp", fake_run)
        return state

    def _init_input(self, mcp_choice: str) -> str:
        """Build input for the manual-flow init with an explicit MCP choice."""
        return f"filesystem\nfs\nstdio\nnpx\n-y @mcp/fs\n{mcp_choice}\n"

    def test_choice_1_calls_claude_mcp_add_with_expected_args(
        self, runner, config, no_discovery, fake_claude
    ):
        """Happy path: user picks 1, pre-check returns 'not registered',
        ``claude mcp add`` runs successfully."""
        fake_claude["script"] = [
            _FakeClaudeResult(returncode=1),  # `claude mcp get` → not registered
            _FakeClaudeResult(returncode=0),  # `claude mcp add` → success
        ]
        result = runner.invoke(
            cli, ["init", "--no-validate", *_cfg_args(config)], input=self._init_input("1")
        )
        assert result.exit_code == 0, result.output
        assert "Registered with Claude Code" in result.output
        assert len(fake_claude["calls"]) == 2
        # First call: pre-check
        assert fake_claude["calls"][0][:4] == ["claude", "mcp", "get", "memtomem-stm"]
        # Second call: actual add, with -s user and the server command
        add_cmd = fake_claude["calls"][1]
        assert add_cmd[:7] == ["claude", "mcp", "add", "memtomem-stm", "-s", "user", "--"]

    def test_choice_2_writes_mcp_json_without_shelling_out(
        self, runner, config, no_discovery, fake_claude, tmp_path, monkeypatch
    ):
        """Option 2 generates ``.mcp.json`` and never touches the ``claude`` CLI."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            cli, ["init", "--no-validate", *_cfg_args(config)], input=self._init_input("2")
        )
        assert result.exit_code == 0, result.output
        assert fake_claude["calls"] == []
        mcp_json = tmp_path / ".mcp.json"
        assert mcp_json.exists()
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert "memtomem-stm" in data["mcpServers"]

    def test_choice_3_skips_with_manual_hints(self, runner, config, no_discovery, fake_claude):
        """Option 3 prints the manual cheat sheet; no subprocess, no file write."""
        result = runner.invoke(
            cli, ["init", "--no-validate", *_cfg_args(config)], input=self._init_input("3")
        )
        assert result.exit_code == 0, result.output
        assert fake_claude["calls"] == []
        assert "claude mcp add memtomem-stm" in result.output
        assert "mcpServers" in result.output
        assert "mms register" in result.output

    def test_duplicate_pre_check_keeps_existing_when_user_picks_keep(
        self, runner, config, no_discovery, fake_claude
    ):
        """Pre-check finds an existing registration → user picks option 1
        ('keep') on the replace prompt → no ``remove`` or ``add`` call.

        The 'keep' option is intentionally the default for the prompt but
        ``click.CliRunner`` aborts on EOF rather than taking defaults, so
        the test feeds an explicit ``1`` for that prompt too."""
        fake_claude["script"] = [
            _FakeClaudeResult(returncode=0),  # `mcp get` → already exists
        ]
        # Extra "1\n" for the keep/replace prompt (pick keep).
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input=self._init_input("1").rstrip("\n") + "\n1\n",
        )
        assert result.exit_code == 0, result.output
        assert "already registered" in result.output
        assert "Kept existing registration" in result.output
        # Only the pre-check ran — no remove, no add.
        assert len(fake_claude["calls"]) == 1
        assert fake_claude["calls"][0][:3] == ["claude", "mcp", "get"]

    def test_duplicate_replace_removes_then_adds(self, runner, config, no_discovery, fake_claude):
        """Pre-check finds duplicate + user picks 'Replace' → remove, then
        add are called in that order."""
        fake_claude["script"] = [
            _FakeClaudeResult(returncode=0),  # mcp get → exists
            _FakeClaudeResult(returncode=0),  # mcp remove
            _FakeClaudeResult(returncode=0),  # mcp add
        ]
        # Extra "2\n" for the keep/replace prompt (pick replace)
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input=self._init_input("1").rstrip("\n") + "\n2\n",
        )
        assert result.exit_code == 0, result.output
        assert "Registered with Claude Code" in result.output
        assert len(fake_claude["calls"]) == 3
        assert fake_claude["calls"][0][:3] == ["claude", "mcp", "get"]
        assert fake_claude["calls"][1][:3] == ["claude", "mcp", "remove"]
        assert fake_claude["calls"][2][:3] == ["claude", "mcp", "add"]

    def test_fallback_on_file_not_found_writes_mcp_json(
        self, runner, config, no_discovery, fake_claude, tmp_path, monkeypatch
    ):
        """``claude`` CLI missing → ``FileNotFoundError`` on pre-check and
        on the add call → falls back to writing ``.mcp.json``."""
        monkeypatch.chdir(tmp_path)
        # Both `claude mcp get` (pre-check) and `claude mcp add` raise when the
        # binary is absent from PATH — the fake must model that, not just
        # script one exception.
        fake_claude["script"] = [
            FileNotFoundError("claude not on PATH"),  # pre-check
            FileNotFoundError("claude not on PATH"),  # actual add
        ]
        result = runner.invoke(
            cli, ["init", "--no-validate", *_cfg_args(config)], input=self._init_input("1")
        )
        assert result.exit_code == 0, result.output
        assert "claude CLI not found" in result.output
        assert "falling back to .mcp.json" in result.output
        assert (tmp_path / ".mcp.json").exists()

    def test_fallback_on_timeout_writes_mcp_json(
        self, runner, config, no_discovery, fake_claude, tmp_path, monkeypatch
    ):
        """``TimeoutExpired`` from the add call (duplicate check already
        passed) → fallback to ``.mcp.json``."""
        import subprocess

        monkeypatch.chdir(tmp_path)
        fake_claude["script"] = [
            _FakeClaudeResult(returncode=1),  # mcp get → not registered
            subprocess.TimeoutExpired(cmd="claude", timeout=5),  # mcp add → timeout
        ]
        result = runner.invoke(
            cli, ["init", "--no-validate", *_cfg_args(config)], input=self._init_input("1")
        )
        assert result.exit_code == 0, result.output
        assert "timed out" in result.output
        assert (tmp_path / ".mcp.json").exists()

    def test_fallback_on_nonzero_exit_writes_mcp_json(
        self, runner, config, no_discovery, fake_claude, tmp_path, monkeypatch
    ):
        """Add exits non-zero for a reason other than pre-check → generic
        'claude mcp add failed' fallback."""
        monkeypatch.chdir(tmp_path)
        fake_claude["script"] = [
            _FakeClaudeResult(returncode=1),  # mcp get → not registered
            _FakeClaudeResult(returncode=2, stderr="some unexpected error"),
        ]
        result = runner.invoke(
            cli, ["init", "--no-validate", *_cfg_args(config)], input=self._init_input("1")
        )
        assert result.exit_code == 0, result.output
        assert "claude mcp add failed" in result.output
        assert (tmp_path / ".mcp.json").exists()

    # ── --mcp flag (non-interactive path) ────────────────────────────

    def _init_input_without_mcp_choice(self) -> str:
        """Manual-flow input that STOPS before the 3-way prompt (the flag
        pre-answers it, so the prompt never fires)."""
        return "filesystem\nfs\nstdio\nnpx\n-y @mcp/fs\n"

    def test_mcp_flag_skip_bypasses_prompt(self, runner, config, no_discovery, fake_claude):
        """--mcp skip pre-answers option 3; no stdin consumed past the
        manual-flow prompts; no subprocess calls."""
        result = runner.invoke(
            cli,
            ["init", "--no-validate", "--mcp", "skip", *_cfg_args(config)],
            input=self._init_input_without_mcp_choice(),
        )
        assert result.exit_code == 0, result.output
        assert fake_claude["calls"] == []
        # The interactive prompt header MUST NOT appear.
        assert "Register memtomem-stm with an MCP client?" not in result.output
        # Manual hints still printed (option 3 behavior).
        assert "claude mcp add memtomem-stm" in result.output

    def test_mcp_flag_json_writes_mcp_json_without_shell_out(
        self, runner, config, no_discovery, fake_claude, tmp_path, monkeypatch
    ):
        """--mcp json pre-answers option 2; writes .mcp.json; no claude
        shell-out."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            cli,
            ["init", "--no-validate", "--mcp", "json", *_cfg_args(config)],
            input=self._init_input_without_mcp_choice(),
        )
        assert result.exit_code == 0, result.output
        assert fake_claude["calls"] == []
        assert "Register memtomem-stm with an MCP client?" not in result.output
        assert (tmp_path / ".mcp.json").exists()

    def test_mcp_flag_claude_registers_without_asking(
        self, runner, config, no_discovery, fake_claude
    ):
        """--mcp claude pre-answers option 1; pre-check + add run without
        the interactive prompt."""
        fake_claude["script"] = [
            _FakeClaudeResult(returncode=1),  # pre-check: not registered
            _FakeClaudeResult(returncode=0),  # add: success
        ]
        result = runner.invoke(
            cli,
            ["init", "--no-validate", "--mcp", "claude", *_cfg_args(config)],
            input=self._init_input_without_mcp_choice(),
        )
        assert result.exit_code == 0, result.output
        assert "Register memtomem-stm with an MCP client?" not in result.output
        assert "Registered with Claude Code" in result.output
        assert len(fake_claude["calls"]) == 2

    def test_mcp_flag_claude_keeps_existing_on_duplicate_noninteractive(
        self, runner, config, no_discovery, fake_claude
    ):
        """--mcp claude hits a duplicate → non-interactive default is
        'keep'. No keep/replace prompt fires, no ``claude mcp remove`` or
        ``add`` calls are made beyond the pre-check.

        This is the load-bearing contract for CI callers: scripted runs
        can re-assert 'register me' without clobbering whatever the user
        had configured previously."""
        fake_claude["script"] = [_FakeClaudeResult(returncode=0)]  # already registered
        result = runner.invoke(
            cli,
            ["init", "--no-validate", "--mcp", "claude", *_cfg_args(config)],
            input=self._init_input_without_mcp_choice(),
        )
        assert result.exit_code == 0, result.output
        assert "already registered" in result.output
        assert "Kept existing registration" in result.output
        # Prompts must NOT have fired.
        assert "Keep existing" not in result.output.split("already registered")[1]
        assert len(fake_claude["calls"]) == 1
        assert fake_claude["calls"][0][:3] == ["claude", "mcp", "get"]


class TestDetectInstallType:
    """``_detect_install_type`` returns ``(server_cmd, server_args)`` for the
    current install location. Covers the three relevant shapes: STM source
    checkout, user project that ``uv add``'d memtomem-stm, and global install."""

    def test_detects_stm_source_checkout(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "memtomem-stm"\nversion = "0.0.0"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "uv"
        assert args[:2] == ["run", "--directory"]
        assert args[-1] == "mms"

    def test_detects_user_project_with_stm_dep(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "my-app"\ndependencies = ["memtomem-stm>=0.1"]\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "uv"
        assert args[-1] == "mms"

    def test_defaults_to_bare_console_script_outside_project(self, tmp_path, monkeypatch):
        """No pyproject.toml reachable (up to 5 levels) → global install
        assumed → bare ``memtomem-stm`` command with no args."""
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "memtomem-stm"
        assert args == []

    def test_unrelated_pyproject_still_defaults(self, tmp_path, monkeypatch):
        """A pyproject.toml that doesn't mention memtomem-stm must NOT be
        treated as a project install — those users installed STM globally."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "unrelated"\ndependencies = ["requests"]\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "memtomem-stm"
        assert args == []

    def test_detects_optional_dependencies(self, tmp_path, monkeypatch):
        """A user project that has memtomem-stm in ``optional-dependencies``
        (e.g. `[project.optional-dependencies].stm = ["memtomem-stm"]`)
        should also resolve to ``uv run --directory ...``."""
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "my-app"\n'
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            'stm = ["memtomem-stm>=0.1"]\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "uv"
        assert args[-1] == "mms"

    def test_adjacent_package_name_does_not_false_positive(self, tmp_path, monkeypatch):
        """A project named `memtomem-stm-extension` (or similar dash-suffixed
        neighbor) must NOT be treated as STM's own source checkout.

        Pre-tightening heuristic matched the prefix string and would have
        flagged this as an STM dep. The PEP 508 name extraction pins the
        match to the exact package name."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "memtomem-stm-extension"\ndependencies = []\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "memtomem-stm"
        assert args == []

    def test_adjacent_dep_name_does_not_false_positive(self, tmp_path, monkeypatch):
        """Same invariant for the dependency list: a dep like
        ``memtomem-stm-adjacent>=0.1`` is a different package."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "my-app"\ndependencies = ["memtomem-stm-adjacent>=0.1"]\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "memtomem-stm"
        assert args == []

    def test_comment_mention_does_not_false_positive(self, tmp_path, monkeypatch):
        """A pyproject.toml with ``memtomem-stm`` only in a comment or
        free-form description string must not trigger uv-run mode.

        Old string-match heuristic would false-positive here; the TOML parse
        + exact-name match catches only real declarations."""
        (tmp_path / "pyproject.toml").write_text(
            "# Using memtomem-stm for memory proxying — see README.\n"
            "[project]\n"
            'name = "my-app"\n'
            'description = "Uses memtomem-stm under the hood"\n'
            'dependencies = ["requests"]\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "memtomem-stm"
        assert args == []

    def test_malformed_pyproject_falls_through(self, tmp_path, monkeypatch):
        """A pyproject.toml we can't parse (malformed TOML, I/O error) should
        break out of the walk and return the default — not crash."""
        (tmp_path / "pyproject.toml").write_text(
            "this is = [[not valid toml\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "memtomem-stm"
        assert args == []


class TestRegisterCommand:
    """``mms register`` is the post-init re-entry path for the MCP-client
    registration flow. It requires that ``mms init`` has been run, so
    ``mms register`` without a config should abort clearly."""

    @pytest.fixture
    def fake_claude(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        state: dict = {"calls": [], "script": []}

        def fake_run(cmd, timeout=5):
            state["calls"].append(list(cmd))
            if state["script"]:
                nxt = state["script"].pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt
            return _FakeClaudeResult(returncode=0)

        monkeypatch.setattr(proxy_mod, "_run_claude_mcp", fake_run)
        return state

    def test_register_aborts_without_config(self, runner, config):
        """Running ``mms register`` before ``mms init`` should fail with a
        clear 'run `mms init` first' message."""
        assert not config.exists()
        result = runner.invoke(cli, ["register", *_cfg_args(config)])
        assert result.exit_code == 1
        assert "config not found" in result.output
        assert "mms init" in result.output

    def test_register_runs_flow_when_config_exists(self, runner, config, fake_claude):
        """With a config present, ``mms register`` drops straight into the
        3-way prompt. Option 3 (skip) prints the manual hints."""
        config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["register", *_cfg_args(config)], input="3\n")
        assert result.exit_code == 0, result.output
        assert fake_claude["calls"] == []
        assert "claude mcp add memtomem-stm" in result.output
        assert "mms register" in result.output

    def test_register_is_idempotent_when_already_registered(self, runner, config, fake_claude):
        """Re-running register when STM is already in Claude Code and the
        user picks 'keep' → no destructive side effects.

        Pins the idempotency contract for the post-init re-entry path
        (plan Part 2): ``mms register`` can be safely rerun without
        clobbering an existing Claude Code entry."""
        config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}), encoding="utf-8")
        fake_claude["script"] = [_FakeClaudeResult(returncode=0)]  # already registered
        # "1\n" for the 3-way prompt (Claude Code) + "1\n" for keep/replace (keep).
        result = runner.invoke(cli, ["register", *_cfg_args(config)], input="1\n1\n")
        assert result.exit_code == 0, result.output
        assert "already registered" in result.output
        assert "Kept existing registration" in result.output
        # Only the pre-check ran.
        assert len(fake_claude["calls"]) == 1

    # ── --mcp flag ─────────────────────────────────────────────────────

    def test_register_mcp_flag_skip_bypasses_prompt(self, runner, config, fake_claude):
        """--mcp skip skips the interactive prompt entirely. No stdin input
        required — this is the shape CI callers use to confirm config is
        set up without asking a human."""
        config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["register", "--mcp", "skip", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        assert fake_claude["calls"] == []
        assert "Register memtomem-stm with an MCP client?" not in result.output

    def test_register_mcp_flag_claude_keeps_on_duplicate(self, runner, config, fake_claude):
        """Scripted re-assertion of 'register me' when already registered
        is a no-op — guarantees ``mms register --mcp claude`` is safe to
        rerun from CI."""
        config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}), encoding="utf-8")
        fake_claude["script"] = [_FakeClaudeResult(returncode=0)]  # already registered
        result = runner.invoke(cli, ["register", "--mcp", "claude", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        assert "Kept existing registration" in result.output
        assert len(fake_claude["calls"]) == 1


# ── add --from-clients (bulk import) ────────────────────────────────────


class TestAddFromClients:
    """`mms add --from-clients` (alias `--import`) reuses init's discovery +
    TUI to bulk-import additional servers. Difference from init: merges into
    an existing config and filters out servers already registered, so the
    user only sees *new* candidates."""

    def _stub_candidates(self, monkeypatch, candidates):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: candidates)

    def _seed_config(self, config: Path, servers: dict) -> None:
        config.write_text(
            json.dumps({"enabled": True, "upstream_servers": servers}, indent=2),
            encoding="utf-8",
        )

    def test_from_clients_happy_path_imports_new_server(self, runner, config, monkeypatch):
        """Fresh candidate + existing server in config → imports candidate,
        preserves existing entry, and shows the discovery header."""
        self._seed_config(
            config, {"existing": {"prefix": "ex", "command": "old", "transport": "stdio"}}
        )
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "Claude Code (user)",
                    "entry": {
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "@fs"],
                        "compression": "auto",
                        "max_result_chars": 8000,
                    },
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", *_cfg_args(config)],
            input="all\n\n",  # pick all + accept suggested prefix
        )
        assert result.exit_code == 0, result.output
        assert "Found 1 new MCP server" in result.output
        assert "Added 1 server" in result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        assert set(data["upstream_servers"]) == {"existing", "filesystem"}
        fs = data["upstream_servers"]["filesystem"]
        assert fs["command"] == "npx"
        assert fs["prefix"] == "filesystem"  # default suggestion

    def test_from_clients_import_alias(self, runner, config, monkeypatch):
        """`--import` is a flag synonym for `--from-clients`; must hit the
        same code path without any other difference."""
        self._seed_config(config, {})
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "npx", "args": []},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--import", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert "filesystem" in data["upstream_servers"]

    def test_filters_already_registered_by_name(self, runner, config, monkeypatch):
        """Discovered candidate whose name matches an existing server is
        dropped with a skip notice — the user isn't forced to re-review or
        re-pick a prefix for something they've already configured."""
        self._seed_config(
            config,
            {
                "filesystem": {
                    "prefix": "fs",
                    "transport": "stdio",
                    "command": "different",
                },
            },
        )
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
                {
                    "name": "github",
                    "source": "Y",
                    "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@gh"]},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output
        assert "Skipping: 'filesystem'" in result.output
        assert "already registered" in result.output
        assert "Found 1 new MCP server" in result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        # Existing filesystem entry preserved (different command proves it wasn't overwritten).
        assert data["upstream_servers"]["filesystem"]["command"] == "different"
        assert "github" in data["upstream_servers"]

    def test_filters_already_registered_by_signature(self, runner, config, monkeypatch):
        """Same (command, args) under a different name → still dedup'd so
        users don't end up with two registrations of the same underlying
        server (would double-surface tools)."""
        self._seed_config(
            config,
            {
                "my-fs": {
                    "prefix": "fs",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@fs"],
                },
            },
        )
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",  # different name...
                    "source": "X",
                    "entry": {
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "@fs"],  # ...same command+args
                    },
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", *_cfg_args(config)],
            input="",  # no prompts expected — exits early
        )
        assert result.exit_code == 0, result.output
        assert "Skipping: 'filesystem'" in result.output
        assert "matches an existing server" in result.output
        assert "All discovered servers are already registered." in result.output

        # Nothing was appended.
        data = json.loads(config.read_text(encoding="utf-8"))
        assert set(data["upstream_servers"]) == {"my-fs"}

    def test_all_registered_exits_cleanly(self, runner, config, monkeypatch):
        """When every discovered candidate is already registered, we exit
        with a no-op message instead of prompting for an empty selection."""
        self._seed_config(
            config,
            {"filesystem": {"prefix": "fs", "transport": "stdio", "command": "npx"}},
        )
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "x"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", *_cfg_args(config)],
        )
        assert result.exit_code == 0, result.output
        assert "All discovered servers are already registered." in result.output

    def test_no_candidates_discovered_exits_cleanly(self, runner, config, monkeypatch):
        """No MCP-client configs on the machine → no-op with a pointer to
        the manual `mms add NAME ...` path."""
        self._seed_config(config, {})
        self._stub_candidates(monkeypatch, [])

        result = runner.invoke(cli, ["add", "--from-clients", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        assert "No MCP servers found" in result.output
        assert "mms add <NAME>" in result.output

    def test_rejects_conflicting_name_and_prefix(self, runner, config, monkeypatch):
        """Passing NAME or --prefix alongside --from-clients is a usage
        error — silently ignoring them would be surprising."""
        self._seed_config(config, {})
        self._stub_candidates(monkeypatch, [])

        result = runner.invoke(
            cli,
            [
                "add",
                "mysrv",
                "--from-clients",
                "--prefix",
                "fs",
                "--command",
                "x",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code != 0
        assert "--from-clients cannot be combined with" in result.output
        # Lists the conflicting flags by name so the user knows what to drop.
        assert "NAME" in result.output
        assert "--prefix" in result.output
        assert "--command" in result.output

    def test_validate_flag_probes_only_picked_servers(self, runner, config, monkeypatch):
        """With --validate, the selected subset gets probed; unpicked
        candidates must not reach _probe_servers (would be surprising
        perf cost for servers the user declined)."""
        from memtomem_stm.cli import proxy as proxy_mod

        self._seed_config(config, {})
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "a",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "ca"},
                },
                {
                    "name": "b",
                    "source": "Y",
                    "entry": {"transport": "stdio", "command": "cb"},
                },
            ],
        )

        probe_calls: list[dict] = []

        async def fake_probe_servers(servers, timeout):
            probe_calls.append(dict(servers))
            return {n: {"connected": True, "tools": 3, "error": None} for n in servers}

        monkeypatch.setattr(proxy_mod, "_probe_servers", fake_probe_servers)

        result = runner.invoke(
            cli,
            ["add", "--from-clients", "--validate", *_cfg_args(config)],
            input="1\n\n",  # pick only index 1 (server "a")
        )
        assert result.exit_code == 0, result.output
        assert "Reachable:" in result.output
        assert "Validating 1 server" in result.output

        assert len(probe_calls) == 1
        assert set(probe_calls[0].keys()) == {"a"}  # "b" not probed

    def test_prints_source_removal_hint_per_client(self, runner, config, monkeypatch):
        """After a successful import, the user sees a per-source hint for
        removing the direct registration from the originating MCP client.
        Without it, tools are advertised on two paths (direct + STM) and the
        direct path bypasses compression/caching/LTM surfacing — see #202."""
        self._seed_config(config, {})
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "from-user",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@u"]},
                },
                {
                    "name": "from-local",
                    "source": "Claude Code (project)",
                    "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@l"]},
                },
                {
                    "name": "from-proj",
                    "source": ".mcp.json (project)",
                    "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@p"]},
                },
                {
                    "name": "from-desktop",
                    "source": "Claude Desktop",
                    "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@d"]},
                    "duplicate_in": ["Claude Code (user)"],
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", *_cfg_args(config)],
            input="all\n\n\n\n\n",  # pick all + accept suggested prefix for each
        )
        assert result.exit_code == 0, result.output

        # Per-source remediation, matching `claude mcp remove -s <scope>` flags.
        assert "claude mcp remove from-user -s user" in result.output
        assert "claude mcp remove from-local -s local" in result.output
        assert "claude mcp remove from-proj -s project" in result.output
        # Claude Desktop has no CLI analog — print a file-edit hint instead.
        assert "claude_desktop_config.json" in result.output
        assert "from-desktop" in result.output
        # `duplicate_in` yields a second hint line for the same server.
        assert "claude mcp remove from-desktop -s user" in result.output

    def test_no_removal_hint_when_nothing_imported(self, runner, config, monkeypatch):
        """Empty selection → no "Added" block, no removal hint either. The
        warning is scoped to actually-imported servers."""
        self._seed_config(config, {})
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "skipme",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", *_cfg_args(config)],
            input="\n",  # empty selection = decline
        )
        assert result.exit_code == 0, result.output
        assert "No servers selected" in result.output
        assert "claude mcp remove" not in result.output


# ── remove command ───────────────────────────────────────────────────────


class TestRemove:
    def test_remove_existing_server_with_yes(self, runner, config):
        runner.invoke(cli, ["add", "fs", "--prefix", "fs", "--command", "x", *_cfg_args(config)])
        result = runner.invoke(cli, ["remove", "fs", "--yes", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "Removed server 'fs'" in result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        assert "fs" not in data["upstream_servers"]

    def test_remove_nonexistent_server(self, runner, config):
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["remove", "ghost", "--yes", *_cfg_args(config)])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_remove_without_yes_requires_confirm(self, runner, config):
        runner.invoke(cli, ["add", "fs", "--prefix", "fs", "--command", "x", *_cfg_args(config)])
        # Simulate declining the confirm prompt.
        result = runner.invoke(cli, ["remove", "fs", *_cfg_args(config)], input="n\n")
        assert result.exit_code != 0
        data = json.loads(config.read_text(encoding="utf-8"))
        assert "fs" in data["upstream_servers"]


# ── End-to-end ───────────────────────────────────────────────────────────


class TestFullFlow:
    def test_add_list_remove_cycle(self, runner, config):
        add_result = runner.invoke(
            cli,
            ["add", "gh", "--prefix", "gh", "--command", "uvx", *_cfg_args(config)],
        )
        assert add_result.exit_code == 0

        list_result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert "gh" in list_result.output

        remove_result = runner.invoke(cli, ["remove", "gh", "--yes", *_cfg_args(config)])
        assert remove_result.exit_code == 0

        final_list = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert "No upstream servers configured" in final_list.output


# ── health command ──────────────────────────────────────────────────────


class TestHealth:
    def test_health_no_servers(self, runner, config):
        """Empty config → friendly message, no crash."""
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["health", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "No upstream servers configured" in result.output

    def test_health_json_no_servers(self, runner, config):
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["health", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"servers": {}}

    def test_health_unreachable_server(self, runner, config):
        """A server with a nonexistent command → DISCONNECTED."""
        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "bad": {
                            "prefix": "bad",
                            "transport": "stdio",
                            "command": "__nonexistent_cmd_12345__",
                            "args": [],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["health", "--timeout", "3", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "DISCONNECTED" in result.output

    def test_health_json_unreachable(self, runner, config):
        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "bad": {
                            "prefix": "bad",
                            "transport": "stdio",
                            "command": "__nonexistent_cmd_12345__",
                            "args": [],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["health", "--json", "--timeout", "3", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["servers"]["bad"]["connected"] is False
        assert data["servers"]["bad"]["error"]
