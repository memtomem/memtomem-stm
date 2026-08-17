"""Tests for env-var > file > defaults precedence in ProxyConfig loading.

Locks in the fix for #106: env-set ``MEMTOMEM_STM_PROXY__*`` fields must
win over file values both at startup and on hot-reload.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from memtomem_stm.proxy.config import (
    ProxyConfig,
    ProxyConfigLoader,
    _deep_merge,
    _env_override_hint,
    collect_proxy_env_overrides,
)


class TestCollectProxyEnvOverrides:
    def test_top_level_field(self) -> None:
        env = {"MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS": "9999"}
        assert collect_proxy_env_overrides(env) == {"default_max_result_chars": "9999"}

    def test_nested_field_via_double_underscore(self) -> None:
        env = {"MEMTOMEM_STM_PROXY__CACHE__ENABLED": "true"}
        assert collect_proxy_env_overrides(env) == {"cache": {"enabled": "true"}}

    def test_deeply_nested_field(self) -> None:
        env = {
            "MEMTOMEM_STM_PROXY__RELEVANCE_SCORER__EMBEDDING_PROVIDER": "openai",
            "MEMTOMEM_STM_PROXY__RELEVANCE_SCORER__EMBEDDING_MODEL": "text-embedding-3-small",
        }
        assert collect_proxy_env_overrides(env) == {
            "relevance_scorer": {
                "embedding_provider": "openai",
                "embedding_model": "text-embedding-3-small",
            }
        }

    def test_toolgraph_nested_field(self) -> None:
        env = {"MEMTOMEM_STM_PROXY__TOOLGRAPH__ENABLED": "true"}
        assert collect_proxy_env_overrides(env) == {"toolgraph": {"enabled": "true"}}

    def test_unrelated_env_vars_ignored(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "MEMTOMEM_STM_SURFACING__ENABLED": "true",  # surfacing prefix, not proxy
            "MEMTOMEM_STM_PROXY__ENABLED": "true",
        }
        assert collect_proxy_env_overrides(env) == {"enabled": "true"}

    def test_empty_when_no_proxy_env(self) -> None:
        assert collect_proxy_env_overrides({"FOO": "bar"}) == {}


class TestComplexEnvValuesMatchSettings:
    """#834: pydantic-settings JSON-decodes *complex* fields (models and
    containers) and coerces scalars normally. The overlay has to answer the
    same question the same way, or a config the server runs is one this
    rebuild rejects — the whole reason the overlay exists is to reproduce
    what the server resolves.

    Every case below is pinned against ``STMConfig()`` itself in
    ``test_overlay_agrees_with_pydantic_settings`` rather than against my
    reading of the settings docs.
    """

    def test_aggregate_server_map_is_decoded(self) -> None:
        env = {
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": '{"gh": {"prefix": "gh", "command": "s"}}'
        }
        assert collect_proxy_env_overrides(env) == {
            "upstream_servers": {"gh": {"prefix": "gh", "command": "s"}}
        }

    def test_single_server_and_list_leaf_are_decoded(self) -> None:
        env = {
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH": '{"prefix": "gh", "command": "s"}',
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FX__ARGS": '["--one", "--two"]',
        }
        assert collect_proxy_env_overrides(env) == {
            "upstream_servers": {
                "gh": {"prefix": "gh", "command": "s"},
                "fx": {"args": ["--one", "--two"]},
            }
        }

    def test_scalar_leaves_stay_strings(self) -> None:
        """Decoding a scalar would change meaning: a command of "null" or a
        prefix of "1" must reach pydantic as the string it is."""
        env = {
            "MEMTOMEM_STM_PROXY__ENABLED": "true",
            "MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS": "9999",
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FX__COMMAND": "null",
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FX__PREFIX": "1",
        }
        assert collect_proxy_env_overrides(env) == {
            "enabled": "true",
            "default_max_result_chars": "9999",
            "upstream_servers": {"fx": {"command": "null", "prefix": "1"}},
        }

    def test_free_form_dict_leaf_is_not_decoded(self) -> None:
        """``env``/``headers`` are ``dict[str, str]``: the dict itself is
        complex, but a value *inside* it is a plain string."""
        env = {
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FX__ENV": '{"A": "1"}',
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FX__HEADERS__X": "[not json]",
        }
        assert collect_proxy_env_overrides(env) == {
            "upstream_servers": {
                "fx": {"env": {"A": "1"}, "headers": {"x": "[not json]"}},
            }
        }

    def test_field_canonicalization_stops_at_containers(self) -> None:
        """Settings rewrites field keys only while walking model fields and
        stops at a container, so `CACHE='{"ENABLED": …}'` configures the
        server while the same casing under `upstream_servers` does not (it is
        rejected). Rewriting past that boundary would make this rebuild
        accept an environment the server refuses — the exact failure the
        rebuild exists to avoid — so the payload is left verbatim there.

        Mapping keys are operator data either way: a server named `GH` is not
        the server named `gh`.
        """
        env = {
            "MEMTOMEM_STM_PROXY__CACHE": json.dumps({"ENABLED": False}),
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": json.dumps(
                {"GH": {"PREFIX": "gh", "Command": "s", "ENV": {"API_TOKEN": "v"}}}
            ),
        }
        assert collect_proxy_env_overrides(env) == {
            "cache": {"enabled": False},
            "upstream_servers": {
                "GH": {"PREFIX": "gh", "Command": "s", "ENV": {"API_TOKEN": "v"}}
            },
        }

    def test_malformed_json_for_a_complex_field_stays_raw(self) -> None:
        """Left for validation to name the field — substituting a default
        would be the silent degrade this module exists to prevent."""
        env = {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": "{not json"}
        assert collect_proxy_env_overrides(env) == {"upstream_servers": "{not json"}

    def test_unknown_key_stays_raw(self) -> None:
        env = {"MEMTOMEM_STM_PROXY__BOGUS_KEY": '{"a": 1}'}
        assert collect_proxy_env_overrides(env) == {"bogus_key": '{"a": 1}'}

    @pytest.mark.parametrize(
        "items",
        [
            [("MEMTOMEM_STM_PROXY__CACHE", '{"enabled": false}'), ("…__CACHE__ENABLED", "true")],
            [("…__CACHE__ENABLED", "true"), ("MEMTOMEM_STM_PROXY__CACHE", '{"enabled": false}')],
            [("MEMTOMEM_STM_PROXY__CACHE", "null"), ("…__CACHE__ENABLED", "true")],
            [("…__CACHE__ENABLED", "true"), ("MEMTOMEM_STM_PROXY__CACHE", "null")],
            [("memtomem_stm_proxy__cache__enabled", "false")],
            [("MEMTOMEM_STM_PROXY__CACHE____ENABLED", "false")],
            [
                ("MEMTOMEM_STM_PROXY__CACHE", '{"enabled": true}'),
                ("…__CACHE__ENABLED", "false"),
                ("memtomem_stm_proxy__cache", '{"enabled": true}'),
            ],
            [("MEMTOMEM_STM_PROXY__CACHE", '{"ENABLED": false}')],
            [("MEMTOMEM_STM_PROXY__CACHE", '{"Enabled": false}')],
            [("…__UPSTREAM_SERVERS", '{"GH": {"PREFIX": "gh", "COMMAND": "s"}}')],
            [("…__UPSTREAM_SERVERS__GH", '{"PREFIX": "gh", "COMMAND": "s"}')],
            [("…__UPSTREAM_SERVERS", '{"gh": {"prefix": "gh", "command": "s", "CACHE": true}}')],
            [("…__TOOLGRAPH", '{"ENABLED": true, "ARGS": ["x"]}')],
            [("…__UPSTREAM_SERVERS____PREFIX", "gh"), ("…__UPSTREAM_SERVERS____COMMAND", "s")],
            [
                ("…__UPSTREAM_SERVERS__FX__PREFIX", "fx"),
                ("…__UPSTREAM_SERVERS__FX__COMMAND", "s"),
                ("…__UPSTREAM_SERVERS__FX__ENV__", "v"),
            ],
            [("MEMTOMEM_ſTM_PROXY__ENABLED", "true")],
        ],
        ids=[
            "mapping-parent-first",
            "mapping-child-first",
            "scalar-parent-first",
            "scalar-child-first",
            "lowercase-name",
            "empty-delimiter-component",
            "case-equivalent-names-collapse",
            "uppercase-json-field-key",
            "mixed-case-json-field-key",
            "uppercase-fields-under-a-container",
            "uppercase-fields-in-a-container-value",
            "duplicate-cased-keys-under-a-container",
            "uppercase-fields-of-a-nested-model",
            "empty-component-names-a-server",
            "empty-component-inside-a-free-form-dict",
            "unicode-name-that-upper-cases-onto-the-prefix",
        ],
    )
    def test_parent_child_and_name_matching_follow_settings(self, monkeypatch, items) -> None:
        """Settings resolves a mapping parent against a deeper child by
        last-one-wins, keeps a *non-mapping* parent either way, matches names
        case-insensitively, and ignores a name with an empty component. Each
        is a way the rebuild could accept an environment the server rejects
        (or miss one it honors), so each is pinned against settings itself.

        Order is expressed through the environment because that is what
        settings reads it from — sorting the vars here changed the answer.
        """
        from memtomem_stm.config import STMConfig

        for name in [n for n in os.environ if n.upper().startswith("MEMTOMEM_STM_PROXY")]:
            monkeypatch.delenv(name, raising=False)
        for name, value in items:
            monkeypatch.setenv(name.replace("…", "MEMTOMEM_STM_PROXY"), value)

        def outcome(build):
            """Resolved config, or the rejection — same shape either way.

            Settings validates the whole ``STMConfig``, so its error paths
            carry a leading ``proxy``; strip it to compare like with like.
            """
            try:
                return build().model_dump()
            except ValidationError as exc:
                return sorted(
                    (tuple(e["loc"][1:] if e["loc"][:1] == ("proxy",) else e["loc"]), e["type"])
                    for e in exc.errors()
                )

        assert outcome(lambda: ProxyConfig.model_validate(collect_proxy_env_overrides())) == outcome(
            lambda: STMConfig().proxy
        )

    @pytest.mark.parametrize("reverse", [False, True], ids=["parent-first", "child-first"])
    def test_non_mapping_parent_is_not_resurrected_by_a_deeper_var(self, reverse) -> None:
        """`CACHE=null` is a supplied value, not an absence. Settings keeps it
        and lets validation reject the config; letting the deeper var rebuild
        a mapping would make this rebuild *accept* an environment the server
        refuses. Both orders, because process env order is arbitrary while
        settings' answer is not."""
        items = [
            ("MEMTOMEM_STM_PROXY__CACHE", "null"),
            ("MEMTOMEM_STM_PROXY__CACHE__ENABLED", "true"),
        ]
        env = dict(reversed(items) if reverse else items)
        assert collect_proxy_env_overrides(env) == {"cache": None}

    def test_non_mapping_parent_covers_every_json_scalar(self) -> None:
        for raw in ("null", "1", "true", '""', "[]"):
            env = {
                "MEMTOMEM_STM_PROXY__CACHE": raw,
                "MEMTOMEM_STM_PROXY__CACHE__ENABLED": "true",
            }
            assert collect_proxy_env_overrides(env)["cache"] == json.loads(raw), raw

    def test_decoded_payload_is_named_by_its_own_var_not_invented_leaves(self) -> None:
        """`_env_override_hint` names vars by walking overlay leaves, so a
        decoded payload must not contribute leaves of its own: those vars do
        not exist, and an `env` payload is keyed by operator-chosen names
        that would then be echoed into the warning."""
        env = {
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": json.dumps(
                {"gh": {"prefix": "", "command": "c", "env": {"API_TOKEN": "s3cret"}}}
            )
        }
        overrides = collect_proxy_env_overrides(env)
        with pytest.raises(ValidationError) as exc_info:
            ProxyConfig.model_validate(overrides)

        hint = _env_override_hint(exc_info.value, overrides)
        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS" in hint
        assert "GH" not in hint
        assert "API_TOKEN" not in hint
        assert "s3cret" not in hint

    def test_leaf_error_inside_a_payload_names_the_payload_var(self) -> None:
        """The location-resolution half: when the error lands on a scalar
        *inside* the payload, the walk must not synthesize a var name out of
        the path it consumed — that name does not exist, and the last segment
        can be an operator-chosen key like `API_TOKEN`."""
        env = {
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": json.dumps(
                {"gh": {"prefix": "gh", "command": "c", "env": {"API_TOKEN": ["not", "a", "str"]}}}
            )
        }
        overrides = collect_proxy_env_overrides(env)
        with pytest.raises(ValidationError) as exc_info:
            ProxyConfig.model_validate(overrides)
        assert any(
            "env" in [str(p) for p in e["loc"]] for e in exc_info.value.errors()
        ), exc_info.value.errors()

        hint = _env_override_hint(exc_info.value, overrides)
        assert hint == " (env override(s) implicated: MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS)"

    def test_deeper_var_inside_a_decoded_payload_is_still_named(self) -> None:
        """The merge half: a var that writes into the payload is a real var,
        so it stays nameable — otherwise fixing the provenance leak would
        blind the diagnostic to the leaf that actually broke."""
        env = {
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": json.dumps(
                {"gh": {"prefix": "gh", "command": "c"}}
            ),
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__PREFIX": "   ",
        }
        overrides = collect_proxy_env_overrides(env)
        with pytest.raises(ValidationError) as exc_info:
            ProxyConfig.model_validate(overrides)

        hint = _env_override_hint(exc_info.value, overrides)
        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__PREFIX" in hint

    @pytest.mark.parametrize(
        "env",
        [
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": '{"gh": {"prefix": "gh", "command": "s"}}'},
            {
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FX__PREFIX": "fx",
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FX__COMMAND": "s",
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FX__ARGS": '["--one"]',
            },
            {
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FX__PREFIX": "fx",
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FX__COMMAND": "s",
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FX__ENV": '{"A": "1"}',
            },
            {
                "MEMTOMEM_STM_PROXY__ENABLED": "true",
                "MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS": "9999",
                "MEMTOMEM_STM_PROXY__CONFIG_PATH": "/tmp/x.json",
                "MEMTOMEM_STM_PROXY__DEFAULT_COMPRESSION": "truncate",
            },
            # The other vars `docs/reference/environment-variables.md` types
            # as JSON — the same defect reached all of them, not just servers.
            {
                "MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS": '["serve", "--fast"]',
                "MEMTOMEM_STM_PROXY__TOOLGRAPH__SERVER_NAME_MAP": '{"a": "b"}',
                "MEMTOMEM_STM_PROXY__CACHE": '{"enabled": false}',
            },
        ],
        ids=["aggregate-map", "list-leaf", "dict-leaf", "scalars", "documented-json-vars"],
    )
    def test_overlay_agrees_with_pydantic_settings(self, monkeypatch, env) -> None:
        """The oracle: whatever ``STMConfig()`` builds from these vars, the
        overlay rebuild must build too.

        Now that the rebuild asks an ``EnvSettingsSource`` instead of
        reproducing one (#837), this no longer guards a reimplementation of
        settings' semantics — there is none left to drift. What it still
        guards is everything wrapped AROUND the source: the name filter, the
        mapping hook, the malformed-value path, and the source behaving the
        same way on whatever ``pydantic-settings`` a contributor resolves.
        """
        from memtomem_stm.config import STMConfig

        for name in [n for n in os.environ if n.upper().startswith("MEMTOMEM_STM_PROXY")]:
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)

        settings_proxy = STMConfig().proxy
        overlay_proxy = ProxyConfig.model_validate(collect_proxy_env_overrides())
        assert overlay_proxy.model_dump() == settings_proxy.model_dump()


class TestSettingsSourceCanaries:
    """Guards on the seam between this module and ``pydantic-settings``.

    The rebuild delegates every env-resolution rule to an
    ``EnvSettingsSource`` (#837), so the failure mode that remains is not a
    semantic divergence but the delegation itself breaking: a constructor that
    stops accepting what we pass, or the mapping hook no longer being
    consulted. The declared floor is ``>=2.7`` while behavior is measured on
    what the lockfile resolves, so these have to fail loudly rather than
    silently reading the wrong environment.
    """

    def test_source_constructs_with_only_settings_cls(self) -> None:
        """``settings_cls`` is the one constructor argument whose position and
        meaning are stable across the supported range; every other knob is read
        off ``model_config``. Passing more would pin us to one release."""
        from pydantic_settings import EnvSettingsSource

        from memtomem_stm.config import STMConfig

        source = EnvSettingsSource(STMConfig)

        assert source.env_prefix == "MEMTOMEM_STM_"
        assert source.env_nested_delimiter == "__"

    def test_mapping_hook_is_actually_consulted(self, monkeypatch) -> None:
        """If the hook stopped being called, the rebuild would silently resolve
        the PROCESS environment while claiming to answer about the mapping it
        was handed — a wrong answer, not an error. Contrast a variable set only
        in the process with one set only in the mapping.
        """
        for name in [n for n in os.environ if n.upper().startswith("MEMTOMEM_STM_PROXY")]:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS", "1111")

        result = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__MAX_UPSTREAM_CHARS": "2222"})

        assert result == {"max_upstream_chars": "2222"}

    def test_source_output_matches_a_hand_built_expectation(self, monkeypatch) -> None:
        """A written-down expectation of what the source resolves, so an
        upgrade that changes explosion, decoding or case folding trips here
        with a readable diff instead of surfacing as a config bug."""
        env = {
            "MEMTOMEM_STM_PROXY__ENABLED": "true",
            "MEMTOMEM_STM_PROXY__CACHE": '{"ENABLED": false}',
            "MEMTOMEM_STM_PROXY__CACHE__TTL_SECONDS": "5",
            "memtomem_stm_proxy__upstream_servers__gh__args": '["--one"]',
        }

        assert collect_proxy_env_overrides(env) == {
            "enabled": "true",  # scalars stay strings; pydantic coerces later
            "cache": {"enabled": False, "ttl_seconds": "5"},  # field key folded
            "upstream_servers": {"gh": {"args": ["--one"]}},  # complex decoded
        }


class TestMalformedValuesSurviveAsRawStrings:
    """The one place the source's behavior is deliberately NOT adopted.

    ``EnvSettingsSource`` raises on a complex value it cannot decode. The
    overlay instead keeps the raw string so it reaches ``model_validate``,
    which names the field — the diagnostic the load path's warning is built
    on. Dropping the variable, or substituting a default, would be the silent
    degrade this module exists to prevent.
    """

    def test_one_malformed_value_keeps_the_rest(self) -> None:
        env = {
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": "{not json",
            "MEMTOMEM_STM_PROXY__ENABLED": "true",
        }

        assert collect_proxy_env_overrides(env) == {
            "upstream_servers": "{not json",
            "enabled": "true",
        }

    def test_several_malformed_values_are_all_kept(self) -> None:
        """Attribution is by exclusion, so it has to find every culprit, not
        just the first one settings happened to raise on."""
        env = {
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": "{not json",
            "MEMTOMEM_STM_PROXY__CACHE": "[unclosed",
            "MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS": "not-a-list",
            "MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS": "9999",
        }

        assert collect_proxy_env_overrides(env) == {
            "upstream_servers": "{not json",
            "cache": "[unclosed",
            "toolgraph": {"args": "not-a-list"},
            "default_max_result_chars": "9999",
        }

    def test_a_malformed_value_reaches_validation_naming_its_field(self) -> None:
        """The point of keeping it raw: the error names the field the operator
        has to fix. A dropped variable would validate cleanly and run a config
        the operator never wrote."""
        overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__CACHE": "[unclosed"})

        with pytest.raises(ValidationError) as caught:
            ProxyConfig.model_validate(overrides)

        assert caught.value.errors()[0]["loc"] == ("cache",)


class TestDeepMerge:
    def test_overrides_replace_scalars(self) -> None:
        assert _deep_merge({"a": 1, "b": 2}, {"a": 9}) == {"a": 9, "b": 2}

    def test_nested_dicts_merge_recursively(self) -> None:
        base = {"cache": {"enabled": False, "ttl": 60}}
        env = {"cache": {"enabled": True}}
        assert _deep_merge(base, env) == {"cache": {"enabled": True, "ttl": 60}}

    def test_overrides_replace_dict_with_scalar(self) -> None:
        assert _deep_merge({"x": {"a": 1}}, {"x": "scalar"}) == {"x": "scalar"}


class TestLoadFromFileWithEnvOverrides:
    def test_env_wins_when_field_in_both(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 16000}))

        cfg = ProxyConfig.load_from_file(
            cfg_file, env_overrides={"default_max_result_chars": "9999"}
        )

        assert cfg is not None
        assert cfg.default_max_result_chars == 9999

    def test_file_value_kept_when_no_env_override(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 16000}))

        cfg = ProxyConfig.load_from_file(cfg_file, env_overrides={})

        assert cfg is not None
        assert cfg.default_max_result_chars == 16000

    def test_toolgraph_env_wins_over_file(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"toolgraph": {"on_unreachable": "open"}}))

        cfg = ProxyConfig.load_from_file(
            cfg_file, env_overrides={"toolgraph": {"on_unreachable": "closed"}}
        )

        assert cfg is not None
        assert cfg.toolgraph.on_unreachable == "closed"

    def test_env_overrides_when_file_missing(self, tmp_path: Path) -> None:
        cfg = ProxyConfig.load_from_file(
            tmp_path / "nonexistent.json",
            env_overrides={"default_max_result_chars": "7777"},
        )

        assert cfg is not None
        assert cfg.default_max_result_chars == 7777

    def test_missing_file_returns_none_when_missing_ok_false(self, tmp_path: Path) -> None:
        """``missing_ok=False`` lets a caller that already holds a better
        env-aware config (pydantic-settings parse) decline the swap in one
        atomic call — a missing file must NOT be rebuilt from the raw-string
        overlay, which can't represent JSON-encoded complex env values."""
        cfg = ProxyConfig.load_from_file(
            tmp_path / "nonexistent.json",
            env_overrides={"default_max_result_chars": "7777"},
            missing_ok=False,
        )

        assert cfg is None


class TestMalformedEnvOverrideDiagnostics:
    """A malformed MEMTOMEM_STM_PROXY__* value still collapses the load to
    defaults / None (the fallback semantics are deliberately unchanged), but
    the warning must name the env var — otherwise the operator debugs the
    healthy FILE while the env overlay is what broke the merged validation."""

    def test_malformed_env_value_named_in_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 9000}))
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS": "abc"}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None  # fallback unchanged: the whole load still degrades
        assert any(
            "MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS" in r.getMessage() for r in caplog.records
        )

    def test_nested_malformed_env_value_named_in_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"cache": {"enabled": True}}))
        overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES": "-5"})

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert any(
            "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES" in r.getMessage() for r in caplog.records
        )

    def test_file_caused_error_carries_no_env_hint(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A validation error caused by the FILE (no env override at that
        location) must not implicate env vars."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": "abc"}))
        overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__CACHE__ENABLED": "true"})

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert not any("implicated" in r.getMessage() for r in caplog.records)

    def test_model_validator_error_names_env_leaves_not_the_model_prefix(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A cross-field model validator reports its error at the MODEL's
        path (upstream_servers.gh.hybrid), not the field. The hint must name
        the env LEAF actually set under that subtree, never synthesize a
        non-leaf var (MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID) that
        was never in the environment."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(
            json.dumps(
                {"upstream_servers": {"gh": {"prefix": "gh", "hybrid": {"head_chars": 5000}}}}
            )
        )
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__MIN_HEAD_CHARS": "9000"}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        messages = [r.getMessage() for r in caplog.records]
        leaf = "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__MIN_HEAD_CHARS"
        assert any(leaf in m for m in messages)
        assert not any("HYBRID," in m or m.rstrip(")").endswith("HYBRID") for m in messages)

    def test_env_leaf_replacing_a_container_names_that_leaf(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An env string clobbering a whole sub-model (cache='oops') is named
        as the leaf the operator set, even though the error location may sit
        at or below the container."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"cache": {"enabled": True}}))
        overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__CACHE": "oops"})

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert any("MEMTOMEM_STM_PROXY__CACHE" in r.getMessage() for r in caplog.records)

    def test_env_untouched_error_path_names_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An error in an upstream entry the env never touched must not be
        attributed to env overrides set on a SIBLING entry."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "good": {"prefix": "good"},
                        "bad": {"prefix": "bad", "hybrid": {"head_chars": -1}},
                    }
                }
            )
        )
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GOOD__MAX_RESULT_CHARS": "5000"}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert not any("implicated" in r.getMessage() for r in caplog.records)

    def test_missing_field_in_env_created_entry_names_env_leaves(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An env var can CREATE an upstream entry that then fails on a
        missing required field (prefix). The error loc points at the absent
        field — outside the env subtree — but the collapse is env-caused:
        the entry exists only because the env built it, so the env leaves
        under it must be named."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 9000}))
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__MAX_RESULT_CHARS": "5000"}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert any(
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__MAX_RESULT_CHARS" in r.getMessage()
            for r in caplog.records
        )

    def test_missing_field_in_file_entry_carries_no_env_hint(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The provenance counterpart: when the FILE supplies the entry that
        is missing its required field, an env var that merely touched the
        same entry is innocent and must not be implicated."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"upstream_servers": {"gh": {"command": "gh-mcp"}}}))
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__MAX_RESULT_CHARS": "5000"}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert not any("implicated" in r.getMessage() for r in caplog.records)

    def test_file_caused_nested_validator_error_carries_no_env_hint(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When the FILE's own hybrid block breaks the ordering validator, an
        env var that merely set an innocent sibling field (tail_mode) under
        the same subtree must not be implicated — the file reproduces the
        identical (loc, type) error on its own."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "gh": {
                            "prefix": "gh",
                            "hybrid": {"head_chars": 5000, "min_head_chars": 9000},
                        }
                    }
                }
            )
        )
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__TAIL_MODE": "toc"}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert not any("implicated" in r.getMessage() for r in caplog.records)

    def test_env_rebreaking_a_file_broken_leaf_is_still_named(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Type-sensitive matching: the file breaks cache.max_entries one way
        (gt violation) and the env re-breaks the SAME location differently
        (unparseable int). The env value is what the merged config actually
        validated, so it must be named despite the shared location."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"cache": {"max_entries": -5}}))
        overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES": "abc"})

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert any(
            "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES" in r.getMessage() for r in caplog.records
        )

    def test_file_root_error_does_not_mask_a_different_env_root_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Model validators all share type="value_error", so the differential
        key includes the MESSAGE: the file's duplicate-prefix root error must
        not mask a separate env-caused empty-prefix root error."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(
            json.dumps({"upstream_servers": {"a": {"prefix": "x"}, "b": {"prefix": "x"}}})
        )
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__C__PREFIX": "   "}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert any(
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__C__PREFIX" in r.getMessage()
            for r in caplog.records
        )

    def test_env_caused_duplicate_prefix_names_env_leaves(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Top-level model validators (duplicate upstream prefixes) report at
        loc=() — no path to walk. When the file alone validates and only the
        merged config trips the root check, the env overlay flipped it and
        its leaves must be named."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"upstream_servers": {"a": {"prefix": "a"}}}))
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__B__PREFIX": "a"}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert any(
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__B__PREFIX" in r.getMessage()
            for r in caplog.records
        )

    def test_env_caused_whitespace_prefix_names_env_leaves(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 9000}))
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__B__PREFIX": "   "}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert any(
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__B__PREFIX" in r.getMessage()
            for r in caplog.records
        )

    def test_file_caused_root_validator_error_carries_no_env_hint(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When the FILE alone already fails the root validator (its own
        duplicate prefixes), an innocent env var must not be implicated."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(
            json.dumps({"upstream_servers": {"a": {"prefix": "x"}, "b": {"prefix": "x"}}})
        )
        overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__CACHE__ENABLED": "true"})

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert not any("implicated" in r.getMessage() for r in caplog.records)

    def test_non_dict_config_root_rejected_even_with_env_overrides(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A ``[]`` config root must fail the load even when env overrides
        are present: ``dict([])`` is ``{}``, so the deep merge used to turn
        the invalid file into a valid env-only config, silently dropping the
        fact that the file is broken."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text("[]")
        overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__ENABLED": "true"})

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert any("JSON object" in r.getMessage() for r in caplog.records)

    def test_env_only_path_names_the_env_var(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """File missing: the env-only rebuild degrades to defaults and the
        warning names the malformed var."""
        overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__LOCK_TIMEOUT_SECONDS": "x"})

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(tmp_path / "nonexistent.json", overrides)

        assert cfg is not None  # env-only path degrades to defaults, not None
        assert cfg.lock_timeout_seconds == 30.0
        assert any(
            "MEMTOMEM_STM_PROXY__LOCK_TIMEOUT_SECONDS" in r.getMessage() for r in caplog.records
        )

    def test_nested_env_override_merges_with_file(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"cache": {"enabled": True, "default_ttl_seconds": 3600.0}}))

        cfg = ProxyConfig.load_from_file(
            cfg_file, env_overrides={"cache": {"default_ttl_seconds": "60"}}
        )

        assert cfg is not None
        assert cfg.cache.enabled is True  # from file
        assert cfg.cache.default_ttl_seconds == 60.0  # env override


class TestProxyConfigLoaderRespectsEnvOnReload:
    def test_env_overrides_survive_hot_reload(self, tmp_path: Path) -> None:
        """The original bug: hot-reload of stm_proxy.json discards env overrides."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 16000}))

        loader = ProxyConfigLoader(cfg_file, env_overrides={"default_max_result_chars": "9999"})
        first = loader.get()
        assert first.default_max_result_chars == 9999

        # Edit file with a new (file-only) value; env override must still win.
        time.sleep(0.01)  # ensure mtime ticks
        cfg_file.write_text(json.dumps({"default_max_result_chars": 32000}))

        # Force mtime detection
        loader._mtime = -1.0  # noqa: SLF001 — direct test of reload behaviour
        second = loader.get()
        assert second.default_max_result_chars == 9999  # env override held

    def test_env_override_only_used_when_field_in_env(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 16000}))

        loader = ProxyConfigLoader(cfg_file, env_overrides={})
        cfg = loader.get()

        assert cfg.default_max_result_chars == 16000

    def test_loader_without_env_overrides_uses_pure_file(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 12345}))

        loader = ProxyConfigLoader(cfg_file)  # no env_overrides arg
        assert loader.get().default_max_result_chars == 12345


def test_collect_uses_real_environ_when_arg_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS", "4242")
    monkeypatch.delenv("MEMTOMEM_STM_PROXY__ENABLED", raising=False)
    out = collect_proxy_env_overrides()
    assert out.get("default_max_result_chars") == "4242"
