"""Per-field env overrides of a file-declared upstream server (#835).

``MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__<NAME>__<FIELD>`` is the documented
``env > file > defaults`` shape, but pydantic-settings validates the fragment it
builds from the environment on its own, where a server carrying one field has no
``prefix`` — so overriding one field of a server the FILE declares made
``STMConfig()`` refuse to construct, at server startup and at every ``STMConfig()``
in the CLI. ``UpstreamServerCompletionSource`` supplies the file's entry for the
server names the environment mentions, below the env source.

The four shapes the fix has to keep working — file-only, env-only, file plus a
complete env server, file plus a partial override — are pinned as a matrix,
observed both at the settings parse and after the lifespan's file merge.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from memtomem_stm.config import STMConfig
from memtomem_stm.proxy.config import (
    ProxyConfig,
    collect_proxy_env_overrides,
    env_var_hint_for_validation_error,
)
from memtomem_stm.server import _apply_proxy_file_config

FILE_SERVER = {
    "prefix": "fk",
    "transport": "stdio",
    "command": "file-server",
    "args": ["--from-file"],
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Drop inherited ``MEMTOMEM_STM_PROXY*`` vars.

    ``STMConfig()`` reads the whole namespace, so one inherited upstream would
    change every asserted server set here.
    """
    for name in [n for n in os.environ if n.startswith("MEMTOMEM_STM_PROXY")]:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def write_config(tmp_path: Path, servers: dict[str, object]) -> Path:
    path = tmp_path / "stm_proxy.json"
    path.write_text(json.dumps({"enabled": True, "upstream_servers": servers}), encoding="utf-8")
    return path


class TestPartialOverrideOfFileDeclaredServer:
    def test_settings_parse_completes_the_server_from_the_file(self, tmp_path, clean_env):
        """The #835 reproduction: this raised ``prefix Field required``."""
        path = write_config(tmp_path, {"fake": FILE_SERVER})
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-server")

        server = STMConfig().proxy.upstream_servers["fake"]

        assert server.command == "env-server"  # env wins over the file's value
        assert server.prefix == "fk"  # the file supplies what the env omitted
        assert server.args == ["--from-file"]  # untouched file fields survive

    def test_aggregate_json_payload_is_completed_too(self, tmp_path, clean_env):
        """A whole-map payload is the other documented spelling of an override,
        and lands in the same fragment — so it is completed the same way."""
        path = write_config(tmp_path, {"fake": FILE_SERVER})
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv(
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS", json.dumps({"fake": {"command": "env-server"}})
        )

        server = STMConfig().proxy.upstream_servers["fake"]

        assert (server.prefix, server.command) == ("fk", "env-server")

    def test_lifespan_merge_agrees_with_the_settings_parse(self, tmp_path, clean_env):
        """Both halves resolve the same override, so the swap is a no-op on
        content — the completion did not invent a config the load path rejects."""
        path = write_config(tmp_path, {"fake": FILE_SERVER})
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-server")

        config = STMConfig()
        from_parse = config.proxy.upstream_servers["fake"].model_dump()
        assert _apply_proxy_file_config(config, collect_proxy_env_overrides()) is None

        assert config.proxy.upstream_servers["fake"].model_dump() == from_parse


class TestFourShapeMatrix:
    """Every file/env combination, at the settings parse AND after the merge.

    ``STMConfig()`` deliberately does NOT gain the file's own upstreams — the
    config-source boundary keeps those arriving through the load path — so the
    two observation points differ for the file-only shapes, and the expectation
    names each separately rather than folding them together.
    """

    @pytest.mark.parametrize(
        ("file_servers", "env", "expected_parse", "expected_merged"),
        [
            pytest.param(
                {"fake": FILE_SERVER},
                {},
                {},
                {"fake": ("fk", "file-server")},
                id="file-only",
            ),
            pytest.param(
                None,
                {"UPSTREAM_SERVERS__EV__PREFIX": "ev", "UPSTREAM_SERVERS__EV__COMMAND": "env-cmd"},
                {"ev": ("ev", "env-cmd")},
                {"ev": ("ev", "env-cmd")},
                id="env-only-complete",
            ),
            pytest.param(
                {"fake": FILE_SERVER},
                {"UPSTREAM_SERVERS__EV__PREFIX": "ev", "UPSTREAM_SERVERS__EV__COMMAND": "env-cmd"},
                {"ev": ("ev", "env-cmd")},
                {"fake": ("fk", "file-server"), "ev": ("ev", "env-cmd")},
                id="file-plus-complete-env-server",
            ),
            pytest.param(
                {"fake": FILE_SERVER},
                {"UPSTREAM_SERVERS__FAKE__COMMAND": "env-server"},
                {"fake": ("fk", "env-server")},
                {"fake": ("fk", "env-server")},
                id="file-plus-partial-override",
            ),
        ],
    )
    def test_shape(self, tmp_path, clean_env, file_servers, env, expected_parse, expected_merged):
        path = (
            write_config(tmp_path, file_servers)
            if file_servers is not None
            else tmp_path / "absent.json"
        )
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        for suffix, value in env.items():
            clean_env.setenv(f"MEMTOMEM_STM_PROXY__{suffix}", value)

        config = STMConfig()
        assert {
            name: (s.prefix, s.command) for name, s in config.proxy.upstream_servers.items()
        } == expected_parse

        _apply_proxy_file_config(config, collect_proxy_env_overrides())

        assert {
            name: (s.prefix, s.command) for name, s in config.proxy.upstream_servers.items()
        } == expected_merged


class TestIncompleteEnvServerStillFailsLoudly:
    """The completion must not turn a genuinely incomplete server into a
    silently-degraded one: with no file entry to complete it, ``STMConfig()``
    still refuses, and the hint names the variable the operator has to edit —
    which for a missing field is never the field the error reports."""

    def test_no_file_at_all(self, tmp_path, clean_env):
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(tmp_path / "absent.json"))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND", "x")

        with pytest.raises(ValidationError) as caught:
            STMConfig()

        assert caught.value.errors()[0]["loc"] == ("proxy", "upstream_servers", "gh", "prefix")
        assert env_var_hint_for_validation_error(caught.value) == (
            " (env var(s) implicated: MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND)"
        )

    def test_file_exists_without_that_server(self, tmp_path, clean_env):
        path = write_config(tmp_path, {"fake": FILE_SERVER})
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND", "x")

        with pytest.raises(ValidationError):
            STMConfig()

    def test_aggregate_payload_hint_names_the_payload_var(self, tmp_path, clean_env):
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(tmp_path / "absent.json"))
        clean_env.setenv(
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS", json.dumps({"gh": {"command": "x"}})
        )

        with pytest.raises(ValidationError) as caught:
            STMConfig()

        assert env_var_hint_for_validation_error(caught.value) == (
            " (env var(s) implicated: MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS)"
        )

    def test_hint_skips_a_variable_that_touched_another_subtree(self, tmp_path, clean_env):
        """An innocent var elsewhere in the namespace is not implicated — a
        false name costs the operator the debugging session the hint exists to
        save."""
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(tmp_path / "absent.json"))
        clean_env.setenv("MEMTOMEM_STM_PROXY__CACHE__ENABLED", "false")
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND", "x")

        with pytest.raises(ValidationError) as caught:
            STMConfig()

        hint = env_var_hint_for_validation_error(caught.value)
        assert "CACHE__ENABLED" not in hint
        assert "UPSTREAM_SERVERS__GH__COMMAND" in hint

    def test_hint_names_a_bad_scalar_at_its_own_location(self, clean_env):
        """The non-``missing`` shape resolves at the error location itself."""
        clean_env.setenv("MEMTOMEM_STM_PROXY__CACHE__ENABLED", "notabool")

        with pytest.raises(ValidationError) as caught:
            STMConfig()

        assert env_var_hint_for_validation_error(caught.value) == (
            " (env var(s) implicated: MEMTOMEM_STM_PROXY__CACHE__ENABLED)"
        )

    def test_hint_is_empty_for_a_non_validation_error(self) -> None:
        assert env_var_hint_for_validation_error(RuntimeError("boom")) == ""


class TestCompletionSourceIsInert:
    """Failure modes of the completion all resolve to "contribute nothing",
    leaving the pre-#835 behavior rather than a new way for a config to break."""

    def test_broken_file(self, tmp_path, clean_env):
        path = tmp_path / "stm_proxy.json"
        path.write_text("{not json", encoding="utf-8")
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-server")

        with pytest.raises(ValidationError):
            STMConfig()

    def test_non_object_root(self, tmp_path, clean_env):
        path = tmp_path / "stm_proxy.json"
        path.write_text("[]", encoding="utf-8")
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-server")

        with pytest.raises(ValidationError):
            STMConfig()

    def test_explicit_proxy_argument_wins_wholesale(self, tmp_path, clean_env):
        """An explicit ``proxy=`` object replaces the field, so there is no env
        fragment left to complete — and the file must not leak into it."""
        path = write_config(tmp_path, {"fake": FILE_SERVER})
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-server")

        config = STMConfig(proxy=ProxyConfig(enabled=True))

        assert config.proxy.upstream_servers == {}

    def test_server_name_is_matched_without_case_folding(self, tmp_path, clean_env):
        """Settings does not fold mapping keys, and the environment always
        yields a lower-cased name — so a file server spelled ``Fake`` is not
        completed by ``…__FAKE__…``, the same way the load path's deep merge
        would keep them as two distinct servers."""
        path = write_config(tmp_path, {"Fake": FILE_SERVER})
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-server")

        with pytest.raises(ValidationError) as caught:
            STMConfig()

        assert caught.value.errors()[0]["loc"] == ("proxy", "upstream_servers", "fake", "prefix")

    def test_file_upstreams_do_not_leak_when_env_touches_another_field(self, tmp_path, clean_env):
        """The boundary: only env-mentioned server NAMES are completed. A var
        elsewhere under ``proxy`` must not pull the file's upstreams into the
        settings parse, which would bypass the load path's advisories."""
        path = write_config(tmp_path, {"fake": FILE_SERVER})
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv("MEMTOMEM_STM_PROXY__CACHE__ENABLED", "false")

        assert STMConfig().proxy.upstream_servers == {}
