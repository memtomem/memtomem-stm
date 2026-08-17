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

    def test_disabled_proxy_with_upstreams_warns_but_exits_zero(self, runner, config):
        """#831: a config that lists upstreams the proxy will never advertise
        is still schema-valid, so this stays an advisory — but it must be
        said, and it must distinguish the unset default from an explicit
        control-only choice."""
        for extra, expected in (
            ({}, '"enabled" is unset and defaults to false'),
            ({"enabled": False}, "disabled explicitly"),
        ):
            config.write_text(
                json.dumps(
                    {**extra, "upstream_servers": {"gh": {"prefix": "gh", "command": "gh-server"}}}
                )
            )
            config.chmod(0o600)
            result = _validate(runner, config)
            assert result.exit_code == 0, result.output
            assert "1 upstream server(s) configured but the proxy is disabled" in result.output
            assert expected in result.output

    def test_disabled_proxy_advisory_suppressed(self, runner, config):
        """Silent when the proxy serves its upstreams, when there is nothing
        to serve, and when validation already failed (those errors dominate
        and `enabled` is unknown)."""
        servers = {"gh": {"prefix": "gh", "command": "gh-server"}}
        for data in (
            {"enabled": True, "upstream_servers": servers},
            {"upstream_servers": {}},
            {"upstream_servers": {"gh": {"prefix": 42, "command": "gh-server"}}},
        ):
            config.write_text(json.dumps(data))
            config.chmod(0o600)
            result = _validate(runner, config)
            assert "but the proxy is disabled" not in result.output, data

    def test_missing_cache_policy_warns_but_exits_zero(self, runner, config):
        """A key-less legacy config gets the same migration advisory as the
        runtime load path — as a warning only, so validate stays usable as a
        CI gate on configs that are merely un-migrated, not broken."""
        config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}))
        config.chmod(0o600)
        result = _validate(runner, config)
        assert result.exit_code == 0, result.output
        assert "tool_annotation_policy" in result.output
        assert '"cache": {"tool_annotation_policy": "strict"}' in result.output

    def test_cache_policy_present_no_advisory(self, runner, config):
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "cache": {"tool_annotation_policy": "conservative"},
                    "upstream_servers": {},
                }
            )
        )
        config.chmod(0o600)
        result = _validate(runner, config)
        assert result.exit_code == 0, result.output
        assert "tool_annotation_policy not set" not in result.output

    def test_cache_disabled_no_missing_policy_advisory(self, runner, config):
        config.write_text(
            json.dumps({"enabled": True, "cache": {"enabled": False}, "upstream_servers": {}})
        )
        config.chmod(0o600)
        result = _validate(runner, config)
        assert result.exit_code == 0, result.output
        assert "tool_annotation_policy" not in result.output

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


class TestDisplayEscaping:
    """`mms config validate` renders the config's own key names and the paths
    it was pointed at. Both are unvalidated: an unknown key is whatever the
    file happens to contain, and the path comes from ``--config`` (argv, so a
    lone surrogate arrives via POSIX ``surrogateescape``).

    This is the group's clearest inconsistency — the same `config_error` origin
    is escaped at proxy's three render sites as of #759 (#760).
    """

    HOSTILE = "ev\x1b[31m\ril"

    @staticmethod
    def _assert_clean(output: str) -> None:
        assert "\x1b" not in output
        assert "\r" not in output

    def test_unknown_key_name_is_escaped(self, runner, config):
        config.write_text(
            json.dumps({"upstream_servers": {}, self.HOSTILE: 1}, ensure_ascii=False),
            encoding="utf-8",
        )
        res = _validate(runner, config)

        assert "unknown key" in res.output
        assert "\\u001B" in res.output and "\\u000D" in res.output
        self._assert_clean(res.output)

    def test_validation_error_location_is_escaped(self, runner, config):
        """The leak here is the ``loc``, not the ``msg``. Pydantic's messages
        are already value-free by #759's design ("Input should be 'stdio', ..."
        names the allowed set, not what was given), but ``loc`` is built from
        the config's own keys — so a hostile *server name* renders inside
        ``upstream_servers.<name>.prefix``."""
        config.write_text(
            json.dumps(
                {"upstream_servers": {self.HOSTILE: {"command": "x"}}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        res = _validate(runner, config)

        assert res.exit_code != 0
        assert "Field required" in res.output
        self._assert_clean(res.output)

    def test_missing_file_error_escapes_the_path_from_argv(self, runner, tmp_path):
        missing = tmp_path / f"cfg{self.HOSTILE}.json"
        res = _validate(runner, missing)

        assert res.exit_code != 0
        assert "not found" in res.output
        self._assert_clean(res.output)

    def test_json_leg_keeps_the_key_raw(self, runner, config):
        config.write_text(
            json.dumps({"upstream_servers": {}, self.HOSTILE: 1}, ensure_ascii=False),
            encoding="utf-8",
        )
        res = _validate(runner, config, "--json")

        payload = json.loads(res.output)
        assert payload["unknown_keys"] == [self.HOSTILE]
