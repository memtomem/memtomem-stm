"""Tests for ``mms config validate`` (#611).

Strict linting of the config file as written: parse errors, schema
validation errors, unknown keys (silently ignored at runtime), and a
missing file all exit non-zero; the permissive-file-mode case is a
warning only. Uses ``CliRunner`` + a tmp config path — no real home
directory is touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import cli
from helpers import set_home


@pytest.fixture(autouse=True)
def _hermetic_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "hermetic-home"
    home.mkdir()
    set_home(monkeypatch, home)
    return home


@pytest.fixture
def config(tmp_path: Path) -> Path:
    return tmp_path / "stm_proxy.json"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _validate(runner: CliRunner, config: Path, *extra: str):
    return runner.invoke(cli, ["config", "validate", "--config", str(config), *extra])


class TestConfigValidate:
    def test_clean_config_exits_zero(self, runner, config):
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {"gh": {"prefix": "gh", "command": "gh-server"}},
                }
            )
        )
        config.chmod(0o600)
        result = _validate(runner, config)
        assert result.exit_code == 0, result.output
        assert "OK" in result.output

    def test_unknown_keys_exit_one_with_dotted_paths(self, runner, config):
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "max_result_char": 4000,
                    "upstream_servers": {"gh": {"prefix": "gh", "tool_overides": {}}},
                }
            )
        )
        config.chmod(0o600)
        result = _validate(runner, config)
        assert result.exit_code == 1
        assert "max_result_char" in result.output
        assert "upstream_servers.gh.tool_overides" in result.output
        assert "unknown key" in result.output

    def test_invalid_json_exits_one(self, runner, config):
        config.write_text("{ not valid json")
        result = _validate(runner, config)
        assert result.exit_code == 1
        assert "invalid JSON" in result.output

    def test_non_object_root_exits_one(self, runner, config):
        config.write_text("[]")
        result = _validate(runner, config)
        assert result.exit_code == 1
        assert "JSON object" in result.output

    def test_validation_error_exits_one_with_location(self, runner, config):
        # Duplicate prefixes trip the ProxyConfig model validator.
        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "a": {"prefix": "dup", "command": "a"},
                        "b": {"prefix": "dup", "command": "b"},
                    }
                }
            )
        )
        config.chmod(0o600)
        result = _validate(runner, config)
        assert result.exit_code == 1
        assert "Duplicate upstream prefixes" in result.output

    def test_missing_file_exits_one(self, runner, config):
        result = _validate(runner, config)
        assert result.exit_code == 1
        assert "not found" in result.output

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_permissive_mode_warns_but_exits_zero(self, runner, config):
        config.write_text(json.dumps({"enabled": True}))
        config.chmod(0o644)
        result = _validate(runner, config)
        assert result.exit_code == 0, result.output
        assert "permissive mode 644" in result.output
        assert "OK" in result.output

    def test_json_output_shape(self, runner, config):
        config.write_text(json.dumps({"enabled": True, "cache": {"ttl_secondz": 60}}))
        config.chmod(0o600)
        result = _validate(runner, config, "--json")
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["status"] == "invalid"
        assert payload["unknown_keys"] == ["cache.ttl_secondz"]
        assert payload["errors"] == []
        assert payload["config_path"].endswith("stm_proxy.json")

    def test_json_output_missing(self, runner, config):
        result = _validate(runner, config, "--json")
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["status"] == "missing"
        assert payload["errors"]

    def test_env_vars_deliberately_ignored(self, runner, config, monkeypatch):
        # The command lints the file as written; a bogus env var must not
        # taint the verdict, and an env-only "fix" must not mask a file typo.
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__BOGUS_ENV_KEY", "1")
        config.write_text(json.dumps({"enabled": True}))
        config.chmod(0o600)
        result = _validate(runner, config)
        assert result.exit_code == 0, result.output
