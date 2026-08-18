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
        assert collect_proxy_env_overrides(env).fragment == {"default_max_result_chars": "9999"}

    def test_nested_field_via_double_underscore(self) -> None:
        env = {"MEMTOMEM_STM_PROXY__CACHE__ENABLED": "true"}
        assert collect_proxy_env_overrides(env).fragment == {"cache": {"enabled": "true"}}

    def test_deeply_nested_field(self) -> None:
        env = {
            "MEMTOMEM_STM_PROXY__RELEVANCE_SCORER__EMBEDDING_PROVIDER": "openai",
            "MEMTOMEM_STM_PROXY__RELEVANCE_SCORER__EMBEDDING_MODEL": "text-embedding-3-small",
        }
        assert collect_proxy_env_overrides(env).fragment == {
            "relevance_scorer": {
                "embedding_provider": "openai",
                "embedding_model": "text-embedding-3-small",
            }
        }

    def test_toolgraph_nested_field(self) -> None:
        env = {"MEMTOMEM_STM_PROXY__TOOLGRAPH__ENABLED": "true"}
        assert collect_proxy_env_overrides(env).fragment == {"toolgraph": {"enabled": "true"}}

    def test_unrelated_env_vars_ignored(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "MEMTOMEM_STM_SURFACING__ENABLED": "true",  # surfacing prefix, not proxy
            "MEMTOMEM_STM_PROXY__ENABLED": "true",
        }
        assert collect_proxy_env_overrides(env).fragment == {"enabled": "true"}

    def test_empty_when_no_proxy_env(self) -> None:
        assert collect_proxy_env_overrides({"FOO": "bar"}).fragment == {}


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
        env = {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": '{"gh": {"prefix": "gh", "command": "s"}}'}
        assert collect_proxy_env_overrides(env).fragment == {
            "upstream_servers": {"gh": {"prefix": "gh", "command": "s"}}
        }

    def test_single_server_and_list_leaf_are_decoded(self) -> None:
        env = {
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH": '{"prefix": "gh", "command": "s"}',
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FX__ARGS": '["--one", "--two"]',
        }
        assert collect_proxy_env_overrides(env).fragment == {
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
        assert collect_proxy_env_overrides(env).fragment == {
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
        assert collect_proxy_env_overrides(env).fragment == {
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
        assert collect_proxy_env_overrides(env).fragment == {
            "cache": {"enabled": False},
            "upstream_servers": {"GH": {"PREFIX": "gh", "Command": "s", "ENV": {"API_TOKEN": "v"}}},
        }

    def test_malformed_json_for_a_complex_field_stays_raw(self) -> None:
        """Left for validation to name the field — substituting a default
        would be the silent degrade this module exists to prevent."""
        env = {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": "{not json"}
        assert collect_proxy_env_overrides(env).fragment == {"upstream_servers": "{not json"}

    def test_unknown_key_stays_raw(self) -> None:
        env = {"MEMTOMEM_STM_PROXY__BOGUS_KEY": '{"a": 1}'}
        assert collect_proxy_env_overrides(env).fragment == {"bogus_key": '{"a": 1}'}

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
            [("MEMTOMEM_STM_PROXY", '{"default_max_result_chars": 4242}')],
            [
                ("MEMTOMEM_STM_PROXY", '{"default_max_result_chars": 4242}'),
                ("…__DEFAULT_MAX_RESULT_CHARS", "9999"),
            ],
            [
                ("…__DEFAULT_MAX_RESULT_CHARS", "9999"),
                ("MEMTOMEM_STM_PROXY", '{"default_max_result_chars": 4242}'),
            ],
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
            "bare-block-payload",
            "bare-payload-then-deeper",
            "deeper-then-bare-payload",
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

        assert outcome(
            lambda: ProxyConfig.model_validate(collect_proxy_env_overrides().fragment)
        ) == outcome(lambda: STMConfig().proxy)

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
        assert collect_proxy_env_overrides(env).fragment == {"cache": None}

    def test_non_mapping_parent_covers_every_json_scalar(self) -> None:
        for raw in ("null", "1", "true", '""', "[]"):
            env = {
                "MEMTOMEM_STM_PROXY__CACHE": raw,
                "MEMTOMEM_STM_PROXY__CACHE__ENABLED": "true",
            }
            assert collect_proxy_env_overrides(env).fragment["cache"] == json.loads(raw), raw

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
            ProxyConfig.model_validate(overrides.fragment)

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
            ProxyConfig.model_validate(overrides.fragment)
        assert any("env" in [str(p) for p in e["loc"]] for e in exc_info.value.errors()), (
            exc_info.value.errors()
        )

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
            ProxyConfig.model_validate(overrides.fragment)

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
        overlay_proxy = ProxyConfig.model_validate(collect_proxy_env_overrides().fragment)
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

        assert result.fragment == {"max_upstream_chars": "2222"}

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

        assert collect_proxy_env_overrides(env).fragment == {
            "enabled": "true",  # scalars stay strings; pydantic coerces later
            "cache": {"enabled": False, "ttl_seconds": "5"},  # field key folded
            "upstream_servers": {"gh": {"args": ["--one"]}},  # complex decoded
        }


class TestProvenanceSurvivesOverwritingVariables:
    """Which variable a warning names, when more than one wrote to a path.

    Settings resolves a mapping parent and a deeper child last-one-wins, so a
    variable a later payload covered contributed nothing — naming it points
    the operator at a value that is not in the config being complained about,
    and hides the one that is. Attribution answers this by MEASUREMENT
    (#843): a variable is named when removing it and re-resolving the whole
    remaining environment makes the error disappear, so the winner is
    whichever removal actually changes the outcome.

    The oracles cannot catch this: they compare validated model dumps, which
    discard variable identity entirely.
    """

    def _hint_for(self, env: dict[str, str]) -> str:
        overrides = collect_proxy_env_overrides(env)
        try:
            ProxyConfig.model_validate(overrides.fragment)
        except ValidationError as exc:
            return _env_override_hint(exc, overrides)
        raise AssertionError("expected the override to fail validation")

    def test_later_payload_wins_over_an_earlier_child(self) -> None:
        hint = self._hint_for(
            {
                "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES": "100",
                "MEMTOMEM_STM_PROXY__CACHE": '{"max_entries": 0}',
            }
        )

        assert "MEMTOMEM_STM_PROXY__CACHE)" in hint  # the payload that supplied 0
        assert "MAX_ENTRIES" not in hint  # its 100 never reached the config

    def test_later_aggregate_wins_over_an_earlier_payload(self) -> None:
        hint = self._hint_for(
            {
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH": '{"prefix": "gh", "command": "old"}',
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": (
                    '{"gh": {"prefix": "gh", "command": ["bad"]}}'
                ),
            }
        )

        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS)" in hint
        assert "__GH" not in hint

    def test_a_payload_does_not_own_an_entry_a_deeper_variable_created(self) -> None:
        """A payload owns what it declared, not what it was merged with.

        The aggregate here declares `other` and knows nothing about `gh`, which
        a deeper variable created; the resolved node holds both. Marking the
        whole node as the payload's would report its name for an entry it has
        nothing to do with. The assertion is exact — checking only that the
        child appears would pass with the parent spuriously named too.
        """
        env = {
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": '{"other": {"prefix": "other"}}',
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND": "gh-mcp",
        }

        assert self._hint_for(env) == (
            " (env override(s) implicated: MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND)"
        )

    def test_a_payload_does_own_the_entry_it_declared(self) -> None:
        """The control: when the payload DID declare the entry, it is named —
        so the test above cannot pass by the marking having stopped working."""
        env = {
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": '{"gh": {"command": "x"}}',
        }

        assert self._hint_for(env) == (
            " (env override(s) implicated: MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS)"
        )

    def test_a_payload_owns_a_field_it_spelled_in_another_case(self) -> None:
        """Settings canonicalizes a model field written in any case, so the
        payload's own branch does not come back under the key it wrote. Failing
        to recognize it made the hint SYNTHESIZE a variable name for a payload
        leaf — worse than naming too many, because that name does not exist."""
        env = {
            "MEMTOMEM_STM_PROXY__EXTRACTION": (
                '{"LLM": {"provider": "ollama", "llm_timeout_seconds": 0}}'
            )
        }

        assert self._hint_for(env) == (
            " (env override(s) implicated: MEMTOMEM_STM_PROXY__EXTRACTION)"
        )

    def test_a_mapping_key_in_another_case_is_a_different_entry(self) -> None:
        """The boundary of the rule above: settings folds field names but takes
        mapping keys verbatim, so a payload keyed `GH` and a deeper variable's
        `gh` are two servers. Folding here would hand the payload an entry that
        is not its own — the resolved node holding BOTH spellings is the tell."""
        env = {
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": '{"GH": {"prefix": "gh"}}',
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND": "x",
        }

        assert self._hint_for(env) == (
            " (env override(s) implicated: MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND)"
        )

    def test_a_deeper_variable_still_counts_when_nothing_covers_it(self) -> None:
        """The control: a later variable that goes DEEPER is merged on top, not
        replaced, so both are real and the deeper one stays nameable."""
        hint = self._hint_for(
            {
                "MEMTOMEM_STM_PROXY__CACHE": '{"enabled": false}',
                "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES": "-1",
            }
        )

        assert "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES" in hint

    def test_a_malformed_variable_inside_a_payload_is_named(self) -> None:
        """A malformed value is re-inserted raw INSIDE a decoded payload. It is
        still the variable the operator has to fix — and the ONLY one: the
        payload whose own args were valid must not be named alongside it (the
        exact assertion closed a hole the positive-only form left, #843)."""
        hint = self._hint_for(
            {
                "MEMTOMEM_STM_PROXY__TOOLGRAPH": '{"args": ["serve"]}',
                "MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS": "not-a-list",
            }
        )

        assert hint == (" (env override(s) implicated: MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS)")


class TestDivergenceEightIsClosedByDelegating:
    """A disagreement the hand-written rebuild had, found while reviewing its
    replacement — the ninth of the class #837 was filed about.

    A payload spelling a model field in another case does NOT merge with a
    deeper variable addressing the same field. Settings explodes the deeper
    variable into its own branch and canonicalizes afterwards, so the branches
    never meet and the later one replaces the earlier; the hand-written rebuild
    canonicalized first and merged them. It therefore reported a config the
    server does not run — the exact failure the overlay exists to prevent.
    """

    def test_a_cased_payload_field_does_not_merge_with_a_deeper_variable(self) -> None:
        env = {
            "MEMTOMEM_STM_PROXY__EXTRACTION": '{"LLM": {"provider": "ollama"}}',
            "MEMTOMEM_STM_PROXY__EXTRACTION__LLM__LLM_TIMEOUT_SECONDS": "30",
        }

        # `provider` is gone: the payload's whole `llm` branch was replaced.
        assert collect_proxy_env_overrides(env).fragment == {
            "extraction": {"llm": {"llm_timeout_seconds": "30"}}
        }

    def test_the_settings_parse_agrees(self, monkeypatch) -> None:
        """The oracle for the case above, stated separately so the expectation
        is not just this module's own reading: settings resolves the same
        thing, and rejects the config for the DEFAULT provider — which is only
        possible if the payload's `provider` never arrived.

        `OPENAI_API_KEY` is cleared because that default is what makes the
        rejection observable: with a key present the config validates and the
        test would pass or fail on an ambient variable it does not control.
        """
        from memtomem_stm.config import STMConfig

        for name in [n for n in os.environ if n.upper().startswith("MEMTOMEM_STM_PROXY")]:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__EXTRACTION", '{"LLM": {"provider": "ollama"}}')
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__EXTRACTION__LLM__LLM_TIMEOUT_SECONDS", "30")

        with pytest.raises(ValidationError) as caught:
            STMConfig()

        assert "openai" in str(caught.value)  # the default, not the payload's ollama


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

        assert collect_proxy_env_overrides(env).fragment == {
            "upstream_servers": "{not json",
            "enabled": "true",
        }

    def test_a_malformed_value_a_later_payload_replaced_is_not_restored(self) -> None:
        """Order decides whether a malformed value is still there to keep.

        Re-inserting one settings had already replaced would overwrite the
        payload that WON with the string that lost — turning an overlay
        settings accepts into one validation rejects, which is the opposite of
        this path's purpose. The reverse order is the control above.
        """
        env = {
            "MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS": "not-a-list",
            "MEMTOMEM_STM_PROXY__TOOLGRAPH": '{"args": ["serve"]}',
        }

        assert collect_proxy_env_overrides(env).fragment == {"toolgraph": {"args": ["serve"]}}

    def test_several_malformed_values_are_all_kept(self) -> None:
        """Attribution is by exclusion, so it has to find every culprit, not
        just the first one settings happened to raise on."""
        env = {
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": "{not json",
            "MEMTOMEM_STM_PROXY__CACHE": "[unclosed",
            "MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS": "not-a-list",
            "MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS": "9999",
        }

        assert collect_proxy_env_overrides(env).fragment == {
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
            ProxyConfig.model_validate(overrides.fragment)

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
    assert out.fragment.get("default_max_result_chars") == "4242"


class TestLeaveOneOutAttribution:
    """Regression pins for the #843 rewrite: attribution is leave-one-out
    measurement, and every counterexample the plan-review rounds produced
    against a weaker rule is pinned here. Each test names the review round
    that constructed it."""

    def _hint(self, env: dict[str, str], file_data: dict[str, object] | None = None) -> str:
        overrides = collect_proxy_env_overrides(env)
        data = (
            _deep_merge(dict(file_data), overrides.fragment)
            if file_data is not None
            else overrides.fragment
        )
        try:
            ProxyConfig.model_validate(data)
        except ValidationError as exc:
            return _env_override_hint(
                exc, overrides, dict(file_data) if file_data is not None else None
            )
        raise AssertionError("expected the merged config to fail validation")

    def test_dead_child_under_a_non_mapping_parent_is_not_named(self) -> None:
        """R1: settings lets ``CACHE=null`` block the deeper variable in either
        order, so the child contributed nothing — its removal changes nothing
        observable and it must not be named for the parent's error."""
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__CACHE": "null",
                "MEMTOMEM_STM_PROXY__CACHE__ENABLED": "true",
            }
        )

        assert hint == " (env override(s) implicated: MEMTOMEM_STM_PROXY__CACHE)"

    def test_mixed_subtree_root_error_spares_the_innocent_variable(self) -> None:
        """R1: a duplicate-prefix root error implicates the variables whose
        removal clears it, not every variable that happens to be set."""
        file_data = {
            "upstream_servers": {
                "a": {"prefix": "pa", "command": "a-mcp"},
                "b": {"prefix": "pb", "command": "b-mcp"},
            }
        }
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__A__PREFIX": "pb",
                "MEMTOMEM_STM_PROXY__CACHE__ENABLED": "true",
            },
            file_data,
        )

        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__A__PREFIX" in hint
        assert "CACHE__ENABLED" not in hint

    def test_hint_renders_the_original_spelling(self) -> None:
        """R1: settings accepts a lowercase-spelled variable, and the hint must
        name THAT spelling — an uppercased reconstruction does not exist on a
        case-sensitive system."""
        hint = self._hint({"memtomem_stm_proxy__cache__max_entries": "-5"})

        assert "memtomem_stm_proxy__cache__max_entries" in hint
        assert "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES" not in hint

    def test_overdetermined_error_falls_back_to_path_attribution(self) -> None:
        """A payload and a deeper variable supplying the SAME failing value:
        removing either leaves the error via the other, so no removal is
        cleanly implicating — the coarse fallback names both rather than
        going silent on a certainly-env-caused error."""
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__TOOLGRAPH": '{"args": "not-a-list"}',
                "MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS": '"not-a-list"',
            }
        )

        assert "MEMTOMEM_STM_PROXY__TOOLGRAPH," in hint or (
            "MEMTOMEM_STM_PROXY__TOOLGRAPH)" in hint
        )
        assert "MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS" in hint

    def test_aggregate_above_a_deeper_vars_error_is_not_named(self) -> None:
        """Non-missing variant of the payload-ownership pin: the aggregate
        declared only ``other``; the failing value under ``gh`` came from the
        deeper variables, so the aggregate's removal leaves the error and the
        aggregate stays unnamed."""
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": '{"other": {"prefix": "other"}}',
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__PREFIX": "gh",
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__HEAD_CHARS": "-1",
            }
        )

        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__HEAD_CHARS" in hint
        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS)" not in hint
        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS," not in hint

    def test_masked_innocent_entry_is_not_named_for_a_root_error(self) -> None:
        """R2: removing the innocent entry's prefix makes pydantic report a
        ``missing`` field error, which SUPPRESSES the root validators — the
        duplicate-prefix error 'disappears' without the innocent variable
        having caused it. The masking guard refuses that implication; the
        variables whose clean removal resolves the collision are named."""
        file_data = {
            "upstream_servers": {
                "a": {"prefix": "pa", "command": "a-mcp"},
                "b": {"prefix": "pb", "command": "b-mcp"},
            }
        }
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__A__PREFIX": "shared",
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__B__PREFIX": "shared",
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__INNOCENT__PREFIX": "inn",
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__INNOCENT__COMMAND": "inn-mcp",
            },
            file_data,
        )

        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__A__PREFIX" in hint
        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__B__PREFIX" in hint
        assert "INNOCENT" not in hint

    def test_env_var_rebreaking_the_same_error_key_is_still_named(self) -> None:
        """R2: file ``-5`` and env ``-6`` produce the IDENTICAL error key, so
        the naive file pre-filter would call it file-caused and hide the env
        variable that supplies the value the merged config actually holds."""
        hint = self._hint(
            {"MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES": "-6"},
            {"cache": {"max_entries": -5}},
        )

        assert "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES" in hint

    def test_repairing_variable_is_not_named_for_the_error_it_reveals(self) -> None:
        """R3: the env variable FIXES the file's empty prefix, and the fix
        un-masks the file's own duplicate-prefix error (model validators
        raise-and-stop). The repairer must not be named for the error its
        repair revealed."""
        file_data = {
            "upstream_servers": {
                "a": {"prefix": "x", "command": "a-mcp"},
                "b": {"prefix": "x", "command": "b-mcp"},
                "c": {"prefix": "   ", "command": "c-mcp"},
            }
        }
        hint = self._hint({"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__C__PREFIX": "c"}, file_data)

        assert hint == ""

    def test_repair_only_variable_stays_blocked_beside_an_env_echoed_error(self) -> None:
        """Diff review: the repairer-guard relaxation must be variable-
        specific. Here the overlay ECHOES the file's duplicate prefixes (so
        the overlay alone reproduces the error) while one variable only
        repairs the empty `c` prefix — removing that variable from the
        overlay alone does NOT clear the duplicate, so the guard holds and
        the repair-only variable is not named."""
        file_data = {
            "upstream_servers": {
                "a": {"prefix": "x", "command": "a-mcp"},
                "b": {"prefix": "x", "command": "b-mcp"},
                "c": {"prefix": "   ", "command": "c-mcp"},
            }
        }
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__A__PREFIX": "x",
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__B__PREFIX": "x",
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__C__PREFIX": "c",
            },
            file_data,
        )

        assert hint == ""  # exact: nothing legitimately env-caused survives

    def test_noop_ancestor_that_reproduces_nothing_alone_is_not_named(self) -> None:
        """Diff review: ancestry is not supply. The empty aggregate sits
        above the failing loc and every removal is a no-op (payload and
        deeper var supply the same value), but only the variables that ALONE
        reproduce the error are named."""
        file_data = {"upstream_servers": {"gh": {"prefix": "gh", "command": "gh-mcp"}}}
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": "{}",
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH": ('{"hybrid": {"head_chars": -1}}'),
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__HEAD_CHARS": "-1",
            },
            file_data,
        )

        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH" in hint
        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__HEAD_CHARS" in hint
        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS," not in hint
        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS)" not in hint

    def test_ancestor_payload_masking_a_model_validator_is_not_named(self) -> None:
        """Diff review R2: the aggregate supplied only the prefix; the deeper
        variable broke the reconnect ordering. Removing the aggregate makes
        the entry incomplete, which masks the model validator — ancestry plus
        overlay reach must not pass for causation."""
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": '{"gh": {"prefix": "gh"}}',
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__RECONNECT_DELAY_SECONDS": "40",
            }
        )

        assert hint == (
            " (env override(s) implicated: "
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__RECONNECT_DELAY_SECONDS)"
        )

    def test_repairer_of_its_own_aggregate_is_not_named(self) -> None:
        """Diff review R2: the aggregate carries both the duplicate and the
        empty prefix; the deeper variable only repairs the empty one, and its
        removal swaps the duplicate root error for the empty-prefix root
        error at the same loc. The swap is not causation — the aggregate,
        whose removal genuinely clears everything, is what gets named."""
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": (
                    '{"a": {"prefix": "x"}, "b": {"prefix": "x"}, "c": {"prefix": "   "}}'
                ),
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__C__PREFIX": "c",
            }
        )

        assert hint == " (env override(s) implicated: MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS)"

    def test_noop_ancestor_beside_a_file_identical_value_is_not_named(self) -> None:
        """Diff review R2: with the file holding the identical broken value,
        a file-merged sufficiency probe would credit ANY candidate. The
        empty aggregate's own fragment supplies nothing at the loc; only the
        variable actually carrying the value is named."""
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__CACHE": "{}",
                "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES": "-5",
            },
            {"cache": {"max_entries": -5}},
        )

        assert hint == (" (env override(s) implicated: MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES)")

    def test_contextual_pair_names_both_halves(self) -> None:
        """Diff review R3: validators embed the offending values in their
        message, so removing either half of an inconsistent pair re-fires
        the same validator with different numbers — that is a MUTATION of
        the error, not a clearing, and both halves are named (narrowing a
        cross-field violation further would mean guessing)."""
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__PREFIX": "gh",
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__HEAD_CHARS": "50",
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__MIN_HEAD_CHARS": "9000",
            }
        )

        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__HEAD_CHARS" in hint
        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__MIN_HEAD_CHARS" in hint
        assert "PREFIX" not in hint

    def test_env_rebreaking_a_file_broken_pair_is_still_named(self) -> None:
        """Diff review R3, the file-broken variant: the env override changes
        the numbers of an ordering violation the file already had — a
        different message, so the pre-filter lets it through, and the
        mutation rule keeps the override named instead of reading the
        trial's re-fired validator as a confound."""
        hint = self._hint(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__MIN_HEAD_CHARS": "8000"},
            {
                "upstream_servers": {
                    "gh": {
                        "prefix": "gh",
                        "command": "gh-mcp",
                        "hybrid": {"head_chars": 50, "min_head_chars": 9000},
                    }
                }
            },
        )

        assert hint == (
            " (env override(s) implicated: "
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__MIN_HEAD_CHARS)"
        )

    def test_shadowed_payload_value_is_not_named_for_the_survivors_error(self) -> None:
        """Diff review R3: the payload's -5 lost to the deeper -6, and the
        two produce the same generic error key. Only the variable whose own
        fragment holds the MERGED value is named; the shadowed one surfaces
        sequentially once the survivor is fixed."""
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__CACHE": '{"max_entries": -5}',
                "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES": "-6",
            }
        )

        assert hint == (" (env override(s) implicated: MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES)")

    def test_sibling_only_aggregate_is_excluded_from_coarse_attribution(self) -> None:
        """Diff review R3: the aggregate holds only the `other` entry — an
        ancestor of the overdetermined error under `gh`, but not a supplier
        of anything at its loc — and stays unnamed beside the two variables
        that do supply the failing value."""
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": '{"other": {"prefix": "other"}}',
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH": (
                    '{"prefix": "gh", "hybrid": {"head_chars": -1}}'
                ),
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__HEAD_CHARS": "-1",
            }
        )

        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH" in hint
        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__HEAD_CHARS" in hint
        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS," not in hint
        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS)" not in hint

    def test_bystander_below_a_model_error_is_not_named(self) -> None:
        """Diff review R4: the exact error survives the timeout variable's
        removal and its solo fragment reproduces nothing — sharing the
        failing entry does not make it a cause."""
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__RECONNECT_DELAY_SECONDS": "40",
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__CALL_TIMEOUT_SECONDS": "200",
            },
            {"upstream_servers": {"gh": {"prefix": "gh", "command": "gh-mcp"}}},
        )

        assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__RECONNECT_DELAY_SECONDS" in hint
        assert "CALL_TIMEOUT_SECONDS" not in hint

    def test_coerced_twin_value_is_a_documented_sequential_limit(self) -> None:
        """Diff review R4, adjudicated as a documented limit rather than
        machinery: the payload's numeric -5 and the deeper variable's string
        "-5" are the same value after pydantic coercion, but key-level
        measurement cannot see coercion without re-deriving the schema — the
        class #842 closed. The surviving variable is named; fixing it
        surfaces the payload with clean attribution on the next load."""
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__CACHE": '{"max_entries": -5}',
                "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES": "-5",
            }
        )

        assert hint == (" (env override(s) implicated: MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES)")

    def test_repairer_exposing_the_next_check_of_the_same_validator(self) -> None:
        """Diff review R5: one model validator holds several distinct checks
        that all raise ``value_error`` at the same loc. The env variable
        repairs the first check, exposing the second — a repair, not a
        cause, even though the revealed error shares (loc, type) with the
        file's. Check identity is the value-masked message template."""
        hint = self._hint(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__MAX_RECONNECT_DELAY_SECONDS": "50"},
            {
                "upstream_servers": {
                    "gh": {
                        "prefix": "gh",
                        "command": "gh-mcp",
                        "reconnect_delay_seconds": 40,
                        "call_timeout_seconds": 200,
                    }
                }
            },
        )

        assert hint == ""

    def test_aggregate_repairing_one_branch_and_breaking_another_is_named(self) -> None:
        """Diff review R6: the aggregate repairs `c`'s whitespace prefix AND
        supplies the reconnect value that breaks file-completed `gh`. Its
        removal reveals the file's own error (repair shape), and its
        file-free probe cannot run the failing check (`gh.prefix` lives in
        the file) — but it supplies the very field the error message names,
        so the file-completed probe answers and the aggregate is named."""
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": (
                    '{"gh": {"reconnect_delay_seconds": 40}, "c": {"prefix": "c"}}'
                )
            },
            {
                "upstream_servers": {
                    "gh": {"prefix": "gh", "command": "gh-mcp"},
                    "c": {"prefix": "   ", "command": "c-mcp"},
                }
            },
        )

        assert hint == " (env override(s) implicated: MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS)"

    def test_identical_root_error_in_file_and_env_stays_file_attributed(self) -> None:
        """R3, adjudicated: when the file alone reproduces the root error and
        no removal changes it (an env payload shadowing the file with the
        same broken map), the hint stays silent — at ``loc=()`` the shadower
        is indistinguishable from an innocent variable, and diagnosis
        converges sequentially through the file."""
        broken = {
            "upstream_servers": {
                "a": {"prefix": "x", "command": "a-mcp"},
                "b": {"prefix": "x", "command": "b-mcp"},
            }
        }
        hint = self._hint(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": json.dumps(broken["upstream_servers"])},
            broken,
        )

        assert hint == ""

    def test_dual_role_variable_is_named_for_the_error_it_supplies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R4: the payload both repairs the file (its ollama provider removes
        the api-key requirement) and supplies the failing timeout. Removal
        reveals the file's own api-key error, but a variable at-or-above the
        error's loc that the overlay reaches supplied the failing value —
        the supplier exception keeps it named."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        hint = self._hint(
            {
                "MEMTOMEM_STM_PROXY__EXTRACTION__LLM": (
                    '{"provider": "ollama", "llm_timeout_seconds": 0}'
                )
            },
            {"extraction": {"llm": {"provider": "openai"}}},
        )

        assert "MEMTOMEM_STM_PROXY__EXTRACTION__LLM" in hint

    def test_noop_aggregate_over_a_file_broken_block_is_not_named(self) -> None:
        """R4: an empty payload merged into the file's broken hybrid block
        changes nothing (the trial's merged data equals the original), so the
        error stays file-attributed with no hint — and a payload duplicating
        the file's own values is the same no-op."""
        file_data = {
            "upstream_servers": {
                "gh": {
                    "prefix": "gh",
                    "command": "gh-mcp",
                    "hybrid": {"head_chars": 50, "min_head_chars": 9000},
                }
            }
        }
        for payload in ("{}", '{"head_chars": 50}'):
            hint = self._hint(
                {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID": payload}, file_data
            )

            assert hint == "", payload

    def test_raw_dict_overrides_yield_no_hint_and_no_crash(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A hand-built plain dict keeps working at the load boundary: the
        fragment is honored, but with no raw variables to measure the hint
        stays silent (attribution requires ``collect_proxy_env_overrides``)."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"cache": {"enabled": True}}))

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(
                cfg_file, env_overrides={"cache": {"max_entries": "abc"}}
            )

        assert cfg is None
        assert any("Failed to parse proxy config" in r.getMessage() for r in caplog.records)
        assert not any("implicated" in r.getMessage() for r in caplog.records)

    def test_empty_environment_yields_a_falsy_overlay(self) -> None:
        overlay = collect_proxy_env_overrides({"UNRELATED": "x"})

        assert not overlay
        assert overlay.fragment == {}
        assert overlay.scoped == {}


class TestHintMemoizationAndCost:
    """The hint runs leave-one-out trials on the already-failing path, and a
    failed hot reload re-runs the warning every poll — so trials must be
    linear in the variable count and memoized on everything attribution
    reads (#843)."""

    @pytest.fixture(autouse=True)
    def _fresh_memo(self):
        from memtomem_stm.proxy import config as config_module

        config_module._hint_memo.clear()
        yield
        config_module._hint_memo.clear()

    def _failing(self, env: dict[str, str]):
        overrides = collect_proxy_env_overrides(env)
        try:
            ProxyConfig.model_validate(overrides.fragment)
        except ValidationError as exc:
            return overrides, exc
        raise AssertionError("expected the overlay to fail validation")

    def _counting_fragment_resolver(self, monkeypatch: pytest.MonkeyPatch):
        from memtomem_stm.proxy import config as config_module

        calls = {"n": 0}
        real = config_module._settings_proxy_fragment

        def counted(scoped):
            calls["n"] += 1
            return real(scoped)

        monkeypatch.setattr(config_module, "_settings_proxy_fragment", counted)
        return calls

    def test_identical_failed_loads_pay_the_trials_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        overrides, exc = self._failing(
            {
                "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES": "-1",
                "MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS": "9000",
            }
        )
        calls = self._counting_fragment_resolver(monkeypatch)

        first = _env_override_hint(exc, overrides)
        after_first = calls["n"]
        second = _env_override_hint(exc, overrides)

        assert first == second != ""
        assert after_first > 0
        assert calls["n"] == after_first  # memo hit: no new resolutions

    def test_toggling_an_ambient_provider_key_recomputes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Validation itself reads the provider keys, so a cached attribution
        must not outlive a key appearing or disappearing."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        overrides, exc = self._failing(
            {
                "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES": "-1",
                "MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS": "9000",
            }
        )
        calls = self._counting_fragment_resolver(monkeypatch)

        _env_override_hint(exc, overrides)
        after_first = calls["n"]
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        _env_override_hint(exc, overrides)

        assert calls["n"] > after_first  # ambient change: trials re-ran

    def test_ambient_key_toggles_the_memo_identity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Positive control for the digest's ambient tuple, at the unit
        level: with everything else fixed, toggling a provider key changes
        the memo key itself, so a cached attribution can never be served
        across the toggle. (The attribution text of a provider-sensitive
        scenario converges to the same correct answer in both ambient states
        under the final algorithm, so the pin lives on the digest.)"""
        from memtomem_stm.proxy.config import _hint_memo_key

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__EXTRACTION__LLM__PROVIDER": "openai"}
        )
        file_data = {"extraction": {"llm": {"provider": "anthropic"}}}
        try:
            ProxyConfig.model_validate(_deep_merge(file_data, overrides.fragment))
        except ValidationError as exc:
            captured = exc
        else:  # pragma: no cover - the construction must fail
            raise AssertionError("expected the merged config to fail validation")
        errors = captured.errors()

        without_key = _hint_memo_key(overrides, file_data, errors)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        with_key = _hint_memo_key(overrides, file_data, errors)
        monkeypatch.setenv("OPENAI_API_KEY", "   ")  # whitespace = absent
        stripped = _hint_memo_key(overrides, file_data, errors)

        assert without_key != with_key
        assert stripped == without_key  # presence is stripped, like validation

    def test_insertion_order_is_part_of_the_memo_identity(self) -> None:
        """Environment order decides parent-vs-child resolution, so the two
        orders are different environments with different hints — the second
        call must not be served the first call's cached text."""
        forward = {
            "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES": "-1",
            "MEMTOMEM_STM_PROXY__CACHE": '{"max_entries": 0}',
        }
        reversed_env = {
            "MEMTOMEM_STM_PROXY__CACHE": '{"max_entries": 0}',
            "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES": "-1",
        }
        overrides_f, exc_f = self._failing(forward)
        hint_f = _env_override_hint(exc_f, overrides_f)
        overrides_r, exc_r = self._failing(reversed_env)
        hint_r = _env_override_hint(exc_r, overrides_r)

        # Forward: the later payload covers the child (0 survives) — the
        # payload is named. Reversed: the later child merges on top (-1
        # survives) — the child is named.
        assert "MEMTOMEM_STM_PROXY__CACHE)" in hint_f
        assert "MAX_ENTRIES" not in hint_f
        assert "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES" in hint_r

    def test_a_respelled_variable_rerenders(self) -> None:
        """Same resolution, same errors — only the operator's spelling
        changed. The rendered name must follow it, not the cached text."""
        upper, exc_u = self._failing({"MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES": "-5"})
        hint_u = _env_override_hint(exc_u, upper)
        lower, exc_l = self._failing({"memtomem_stm_proxy__cache__max_entries": "-5"})
        hint_l = _env_override_hint(exc_l, lower)

        assert "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES" in hint_u
        assert "memtomem_stm_proxy__cache__max_entries" in hint_l

    def test_trial_count_stays_linear_with_a_malformed_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformedness is per-variable and precomputed at collect time; a
        trial must not re-probe every variable (that made the malformed path
        quadratic in settings resolutions)."""
        import time as time_module

        env = {"MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS": "{not json"}
        for i in range(9):
            env[f"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__T{i}__PREFIX"] = f"t{i}"
            env[f"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__T{i}__COMMAND"] = f"t{i}-mcp"
        env["MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES"] = "-1"
        assert len(env) == 20
        overrides, exc = self._failing(env)
        assert overrides.malformed  # the malformed branch is genuinely exercised
        calls = self._counting_fragment_resolver(monkeypatch)

        start = time_module.monotonic()
        hint = _env_override_hint(exc, overrides)
        elapsed = time_module.monotonic() - start

        assert "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES" in hint
        # One resolution per leave-one-out trial (20 live vars), not one per
        # (trial, variable) pair — a quadratic regression lands at 400+.
        assert 0 < calls["n"] <= 2 * len(env) + 5
        assert elapsed < 10  # generous absolute bound; the count is the pin

    def test_hundred_variable_smoke_stays_linear(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = {"MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS": "{not json"}
        for i in range(49):
            env[f"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__U{i}__PREFIX"] = f"u{i}"
            env[f"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__U{i}__COMMAND"] = f"u{i}-mcp"
        env["MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES"] = "-1"
        assert len(env) == 100
        overrides, exc = self._failing(env)
        assert overrides.malformed  # the malformed branch is genuinely exercised
        calls = self._counting_fragment_resolver(monkeypatch)

        hint = _env_override_hint(exc, overrides)

        assert "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES" in hint
        assert 0 < calls["n"] <= 2 * len(env) + 5

    def test_ambient_validation_vars_actually_affect_validation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Drift guard: every var listed in _AMBIENT_VALIDATION_VARS changes a
        validation outcome, so the memo digest's ambient tuple is neither
        stale nor decorative. If a validator stops (or starts) reading one,
        this test forces the constant to follow."""
        from memtomem_stm.proxy.config import _AMBIENT_VALIDATION_VARS

        provider_for = {"OPENAI_API_KEY": "openai", "ANTHROPIC_API_KEY": "anthropic"}
        assert set(_AMBIENT_VALIDATION_VARS) == set(provider_for)
        for var, provider in provider_for.items():
            for other in provider_for:
                monkeypatch.delenv(other, raising=False)
            data = {"extraction": {"llm": {"provider": provider}}}
            with pytest.raises(ValidationError):
                ProxyConfig.model_validate(data)
            monkeypatch.setenv(var, "sk-test")
            ProxyConfig.model_validate(data)  # the key alone flips the outcome


class TestBareProxyPayload:
    """#840: settings honors the block's own name, ``MEMTOMEM_STM_PROXY``, as
    one JSON payload for the whole proxy block; the overlay's name filter
    required the trailing delimiter and dropped it, so with a file present
    the file silently won over a variable the server honors."""

    def test_bare_payload_reaches_the_overlay(self) -> None:
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY": '{"default_max_result_chars": 4242}'}
        )

        assert overrides.fragment == {"default_max_result_chars": 4242}

    @pytest.mark.parametrize("reverse", [False, True], ids=["bare-first", "deeper-first"])
    def test_deeper_variable_wins_in_either_order(self, reverse: bool) -> None:
        """Settings reads the field's own env var as the BASE value and
        deep-updates exploded variables on top, so the deeper variable wins
        regardless of environment order — pinned against ``STMConfig`` by the
        oracle's ``deeper-then-bare-payload`` case."""
        items = [
            ("MEMTOMEM_STM_PROXY", '{"default_max_result_chars": 4242}'),
            ("MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS", "9999"),
        ]
        if reverse:
            items.reverse()

        assert collect_proxy_env_overrides(dict(items)).fragment == {
            "default_max_result_chars": "9999"
        }

    def test_env_wins_over_file_through_the_bare_payload(self, tmp_path: Path) -> None:
        """The consequence #834 had: without the overlay seeing the variable,
        the file beats a value the server honors."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 1111}))
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY": '{"default_max_result_chars": 4242}'}
        )

        cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is not None and cfg.default_max_result_chars == 4242

    def test_error_inside_the_bare_payload_names_it(self) -> None:
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY": '{"cache": {"max_entries": -1}}'}
        )
        try:
            ProxyConfig.model_validate(overrides.fragment)
        except ValidationError as exc:
            hint = _env_override_hint(exc, overrides)
        else:  # pragma: no cover - the construction must fail
            raise AssertionError("expected the overlay to fail validation")

        assert hint == " (env override(s) implicated: MEMTOMEM_STM_PROXY)"

    def test_broken_deeper_variable_is_named_not_the_later_bare_payload(self) -> None:
        """The covering rule must not let a later bare payload swallow the
        deeper variable that actually supplies the failing value (the bare
        payload never covers — it is the base, not the winner)."""
        overrides = collect_proxy_env_overrides(
            {
                "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES": "-1",
                "MEMTOMEM_STM_PROXY": '{"cache": {"max_entries": 5}}',
            }
        )
        try:
            ProxyConfig.model_validate(overrides.fragment)
        except ValidationError as exc:
            hint = _env_override_hint(exc, overrides)
        else:  # pragma: no cover - the construction must fail
            raise AssertionError("expected the overlay to fail validation")

        assert hint == (" (env override(s) implicated: MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES)")

    def test_malformed_bare_payload_is_dropped_with_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A raw string for the WHOLE block has no slot in a dict fragment,
        and the server refuses to start on it anyway (``STMConfig()``
        raises), so the overlay warns and keeps the decodable survivors."""
        with caplog.at_level(logging.WARNING):
            overrides = collect_proxy_env_overrides(
                {
                    "MEMTOMEM_STM_PROXY": "{not json",
                    "MEMTOMEM_STM_PROXY__ENABLED": "true",
                }
            )

        assert overrides.fragment == {"enabled": "true"}
        assert "memtomem_stm_proxy" in overrides.malformed
        assert any("MEMTOMEM_STM_PROXY" in r.getMessage() for r in caplog.records)

    def test_settings_rejects_the_malformed_bare_payload_too(self, monkeypatch) -> None:
        """The oracle for the drop above: the environment the overlay cannot
        represent is one the server never runs."""
        from pydantic_settings import SettingsError

        from memtomem_stm.config import STMConfig

        for name in [n for n in os.environ if n.upper().startswith("MEMTOMEM_STM_PROXY")]:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("MEMTOMEM_STM_PROXY", "{not json")

        with pytest.raises(SettingsError):
            STMConfig()

    def test_non_object_bare_payload_warns_and_resolves_to_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`[]` (or a string, or a number) decodes fine but the server
        rejects the config outright — the overlay cannot represent it, so it
        warns instead of letting diagnostics describe a config that cannot
        start."""
        with caplog.at_level(logging.WARNING):
            overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY": "[]"})

        assert overrides.fragment == {}
        assert any("non-object" in r.getMessage() for r in caplog.records)

    def test_null_bare_payload_is_consistent_silence(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`null` makes settings fall back to the field default — exactly
        what an empty overlay expresses — so no warning; pinned against
        ``STMConfig`` accepting the same environment."""
        from memtomem_stm.config import STMConfig

        with caplog.at_level(logging.WARNING):
            overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY": "null"})

        assert overrides.fragment == {}
        assert not caplog.records
        for name in [n for n in os.environ if n.upper().startswith("MEMTOMEM_STM_PROXY")]:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("MEMTOMEM_STM_PROXY", "null")
        assert STMConfig().proxy.default_max_result_chars == 16000
