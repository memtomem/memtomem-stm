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
    import os

    for name in [n for n in os.environ if n.upper().startswith("MEMTOMEM_STM_PROXY")]:
        monkeypatch.delenv(name, raising=False)


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

    def test_present_but_empty_env_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pydantic coerces "" to Path("."), which the server's file load
        # degrades on — a directory is never a readable config. Resolving the
        # CLI to the working directory made every file-loading command crash
        # on a directory (and let register serialize one), so an empty value
        # keeps the pre-#848 target.
        monkeypatch.setenv(_CONFIG_PATH_ENV, "")
        resolved = resolve_cli_config_path(None)
        assert resolved.path == str(DEFAULT_PROXY_CONFIG)
        assert resolved.source == "default"

    def test_lowercase_env_spelling_is_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Settings resolves env names case-insensitively; the resolver must
        # match every spelling that steers the server (codex review of #849).
        monkeypatch.setenv(_CONFIG_PATH_ENV.lower(), "/tmp/lowercase.json")
        resolved = resolve_cli_config_path(None)
        assert resolved.path == "/tmp/lowercase.json"
        assert resolved.source == "env"

    def test_bare_proxy_payload_is_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The aggregate MEMTOMEM_STM_PROXY JSON payload steers the server's
        # config_path too (#840) — the CLI must follow it the same way.
        monkeypatch.setenv("MEMTOMEM_STM_PROXY", '{"config_path": "/tmp/bare-block.json"}')
        resolved = resolve_cli_config_path(None)
        assert resolved.path == "/tmp/bare-block.json"
        assert resolved.source == "env"

    def test_deeper_variable_beats_the_bare_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Same precedence the settings overlay gives the server.
        monkeypatch.setenv("MEMTOMEM_STM_PROXY", '{"config_path": "/tmp/bare-block.json"}')
        monkeypatch.setenv(_CONFIG_PATH_ENV, "/tmp/deeper.json")
        assert resolve_cli_config_path(None).path == "/tmp/deeper.json"

    def test_path_stays_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No expanduser/resolve here — every use site expands, matching how
        # the env value arrives (stm_config_for_cli docstring).
        monkeypatch.setenv(_CONFIG_PATH_ENV, "~/elsewhere/stm.json")
        assert resolve_cli_config_path(None).path == "~/elsewhere/stm.json"
        assert resolve_cli_config_path("~/flag.json").path == "~/flag.json"
