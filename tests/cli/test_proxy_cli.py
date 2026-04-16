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
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import cli


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
