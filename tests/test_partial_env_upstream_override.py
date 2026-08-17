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
from helpers import set_home
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

    def test_hint_skips_an_aggregate_payload_that_declares_no_upstreams(self, tmp_path, clean_env):
        """A payload var sitting ABOVE the incomplete entry is only implicated
        when it actually reaches it. A `MEMTOMEM_STM_PROXY` block carrying only
        cache settings did not declare the server and must not be blamed for
        it."""
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(tmp_path / "absent.json"))
        clean_env.setenv("MEMTOMEM_STM_PROXY", json.dumps({"cache": {"enabled": False}}))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND", "x")

        with pytest.raises(ValidationError) as caught:
            STMConfig()

        assert env_var_hint_for_validation_error(caught.value) == (
            " (env var(s) implicated: MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND)"
        )

    def test_hint_skips_a_variable_a_later_payload_overwrote(self, tmp_path, clean_env):
        """Settings resolves a mapping parent and a deeper child last-one-wins,
        so the aggregate here replaced the per-field variable's whole subtree.
        Naming that variable sends the operator to edit a value that is not in
        the config at all."""
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(tmp_path / "absent.json"))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND", "old")
        clean_env.setenv(
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS", json.dumps({"gh": {"command": "new"}})
        )

        with pytest.raises(ValidationError) as caught:
            STMConfig()

        assert env_var_hint_for_validation_error(caught.value) == (
            " (env var(s) implicated: MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS)"
        )

    def test_hint_matches_a_payload_field_key_the_way_settings_does(self, tmp_path, clean_env):
        """An error location reports a model field by its DECLARED name, while
        the payload may spell it any way settings accepts. Comparing raw keys
        left this hint empty for a variable that plainly declared the server."""
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(tmp_path / "absent.json"))
        clean_env.setenv(
            "MEMTOMEM_STM_PROXY", json.dumps({"UPSTREAM_SERVERS": {"gh": {"command": "x"}}})
        )

        with pytest.raises(ValidationError) as caught:
            STMConfig()

        assert env_var_hint_for_validation_error(caught.value) == (
            " (env var(s) implicated: MEMTOMEM_STM_PROXY)"
        )

    def test_hint_names_an_aggregate_payload_that_does_declare_the_server(
        self, tmp_path, clean_env
    ):
        """The positive control: the same shape, with a payload that does reach
        the entry, must still be named — otherwise the test above would pass
        against a helper that had simply stopped naming payload vars."""
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(tmp_path / "absent.json"))
        clean_env.setenv(
            "MEMTOMEM_STM_PROXY", json.dumps({"upstream_servers": {"gh": {"command": "x"}}})
        )

        with pytest.raises(ValidationError) as caught:
            STMConfig()

        assert env_var_hint_for_validation_error(caught.value) == (
            " (env var(s) implicated: MEMTOMEM_STM_PROXY)"
        )


class TestCompletionSourceIsInert:
    """Failure modes of the completion all resolve to "contribute nothing",
    leaving the pre-#835 behavior rather than a new way for a config to break."""

    @pytest.mark.parametrize(
        ("content", "shape"),
        [("{not json", "malformed-json"), ("[]", "non-object-root"), ("{}", "no-upstreams-key")],
    )
    def test_unusable_file_contributes_nothing(self, tmp_path, clean_env, content, shape):
        """The error must be the untouched env fragment's own — a different one
        would mean the completion contributed something out of a file it should
        not have been able to read."""
        path = tmp_path / "stm_proxy.json"
        path.write_text(content, encoding="utf-8")
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-server")

        with pytest.raises(ValidationError) as caught:
            STMConfig()

        assert [(e["loc"], e["type"]) for e in caught.value.errors()] == [
            (("proxy", "upstream_servers", "fake", "prefix"), "missing")
        ], shape

    def test_a_directory_in_place_of_the_file_contributes_nothing(self, tmp_path, clean_env):
        """``read_text`` on a directory raises ``IsADirectoryError`` on POSIX and
        ``PermissionError`` on Windows — both are ``OSError``, which is why the
        guard catches the base class."""
        path = tmp_path / "stm_proxy.json"
        path.mkdir()
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-server")

        with pytest.raises(ValidationError) as caught:
            STMConfig()

        assert caught.value.errors()[0]["type"] == "missing"

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

    def test_an_invalid_file_entry_is_not_merged_in(self, tmp_path, clean_env):
        """The gate that keeps the completion from breaking a working config:
        the file's entry is emitted only when the COMPLETED server validates,
        so a file field invalid by itself cannot fail a construction that
        succeeded before. The file's `args` here is not a list."""
        path = write_config(tmp_path, {"fake": {"prefix": "fk", "args": 7}})
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__PREFIX", "ev")
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-cmd")

        server = STMConfig().proxy.upstream_servers["fake"]

        assert (server.prefix, server.command) == ("ev", "env-cmd")
        assert server.args == []  # the file's broken value was never consulted

    def test_an_invalid_file_entry_leaves_the_original_error_intact(self, tmp_path, clean_env):
        """The converse, so the gate is not mistaken for "never fail": this
        entry needs the file and the file's entry is invalid, so the completion
        contributes nothing and the operator keeps the error they already had —
        the missing `prefix`, NOT a new one about the file's `args`. Pinning the
        error's identity is the point; asserting only that something raised
        would pass while the completion swapped one failure for another."""
        path = write_config(tmp_path, {"fake": {"prefix": "fk", "args": 7}})
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-cmd")

        with pytest.raises(ValidationError) as caught:
            STMConfig()

        assert [(e["loc"], e["type"]) for e in caught.value.errors()] == [
            (("proxy", "upstream_servers", "fake", "prefix"), "missing")
        ]

    def test_a_valid_fragment_still_gets_the_file_fields_it_omitted(self, tmp_path, clean_env):
        """Per-field layering does not begin at `prefix`. An override of an
        OPTIONAL field leaves a fragment that validates on its own, and the file
        still has the rest of the server to give — asking whether the env
        fragment alone validates would drop it."""
        path = write_config(tmp_path, {"fake": FILE_SERVER})
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__PREFIX", "ev")

        server = STMConfig().proxy.upstream_servers["fake"]

        assert server.prefix == "ev"  # env wins
        assert (server.command, server.args) == ("file-server", ["--from-file"])

    def test_a_higher_precedence_source_completing_the_server_is_not_overridden(
        self, tmp_path, clean_env
    ):
        """Init kwargs outrank both the environment and this source. When they
        already complete the server, an invalid file entry must not be merged
        underneath and fail a construction that worked."""
        path = write_config(tmp_path, {"fake": {"prefix": "file", "args": 7}})
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "x")

        config = STMConfig(proxy={"upstream_servers": {"fake": {"prefix": "init"}}})

        assert config.proxy.upstream_servers["fake"].prefix == "init"

    def test_an_empty_config_path_is_honored_not_replaced_by_the_default(
        self, tmp_path, clean_env, monkeypatch
    ):
        """``config_path=""`` resolves to ``Path(".")`` at runtime. Treating it
        as absent would complete a server out of the default file, which the
        running config does not name."""
        default_home = tmp_path / "home"
        (default_home / ".memtomem").mkdir(parents=True)
        (default_home / ".memtomem" / "stm_proxy.json").write_text(
            json.dumps({"upstream_servers": {"fake": FILE_SERVER}}), encoding="utf-8"
        )
        set_home(monkeypatch, default_home)
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", "")
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-server")

        with pytest.raises(ValidationError):
            STMConfig()

    def test_the_default_path_is_used_when_no_config_path_is_set(
        self, tmp_path, clean_env, monkeypatch
    ):
        """The positive control for the case above — without it, that test
        would pass just as well if the default were never consulted at all."""
        default_home = tmp_path / "home"
        (default_home / ".memtomem").mkdir(parents=True)
        (default_home / ".memtomem" / "stm_proxy.json").write_text(
            json.dumps({"upstream_servers": {"fake": FILE_SERVER}}), encoding="utf-8"
        )
        set_home(monkeypatch, default_home)
        clean_env.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-server")

        server = STMConfig().proxy.upstream_servers["fake"]

        assert (server.prefix, server.command) == ("fk", "env-server")

    def test_file_upstreams_do_not_leak_when_env_touches_another_field(self, tmp_path, clean_env):
        """The boundary: only env-mentioned server NAMES are completed. A var
        elsewhere under ``proxy`` must not pull the file's upstreams into the
        settings parse, which would bypass the load path's advisories."""
        path = write_config(tmp_path, {"fake": FILE_SERVER})
        clean_env.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(path))
        clean_env.setenv("MEMTOMEM_STM_PROXY__CACHE__ENABLED", "false")

        assert STMConfig().proxy.upstream_servers == {}
