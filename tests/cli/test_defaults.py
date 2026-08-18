"""Unit tests for ``resolve_cli_config_path`` (#848).

Every ``--config`` option declares ``default=None`` and resolves through this
helper, so the precedence pinned here — explicit flag > env
``MEMTOMEM_STM_PROXY__CONFIG_PATH`` > ``DEFAULT_PROXY_CONFIG`` — is the
precedence of every proxy-config command's file half.
"""

from __future__ import annotations

import pytest

from memtomem_stm.cli._defaults import (
    _CONFIG_PATH_ENV,
    DEFAULT_PROXY_CONFIG,
    resolve_cli_config_path,
)


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_CONFIG_PATH_ENV, raising=False)


class TestResolveCliConfigPath:
    def test_flag_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_CONFIG_PATH_ENV, "/tmp/env.json")
        resolved = resolve_cli_config_path("/tmp/flag.json")
        assert resolved.path == "/tmp/flag.json"
        assert resolved.source == "flag"

    def test_env_beats_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_CONFIG_PATH_ENV, "/tmp/env.json")
        resolved = resolve_cli_config_path(None)
        assert resolved.path == "/tmp/env.json"
        assert resolved.source == "env"

    def test_default_when_nothing_names_a_path(self) -> None:
        resolved = resolve_cli_config_path(None)
        assert resolved.path == str(DEFAULT_PROXY_CONFIG)
        assert resolved.source == "default"

    def test_present_but_empty_env_is_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Parity with _resolve_config_path_for_completion: an empty value
        # resolves relative to cwd at the use site; silently reading the
        # default file would target a file the running server never names.
        monkeypatch.setenv(_CONFIG_PATH_ENV, "")
        resolved = resolve_cli_config_path(None)
        assert resolved.path == ""
        assert resolved.source == "env"

    def test_path_stays_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No expanduser/resolve here — every use site expands, matching how
        # the env value arrives (stm_config_for_cli docstring).
        monkeypatch.setenv(_CONFIG_PATH_ENV, "~/elsewhere/stm.json")
        assert resolve_cli_config_path(None).path == "~/elsewhere/stm.json"
        assert resolve_cli_config_path("~/flag.json").path == "~/flag.json"
