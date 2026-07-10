"""Shared prefix validation (``proxy/prefixes.py``).

The CLI used to warn-and-save on a duplicate prefix while the runtime
pydantic validator rejected the same config at load — separate code, so
``mms add`` could write a file the proxy then refused to start with.
These tests pin the shared helpers and, via source inspection, that both
sides actually call them.
"""

from __future__ import annotations

import inspect

import click
import pytest
from click.testing import CliRunner

from memtomem_stm.proxy import prefixes
from memtomem_stm.proxy.config import ProxyConfig


class TestPrefixFormatError:
    @pytest.mark.parametrize("prefix", ["fs", "gh2", "a", "snake_case", "CamelCase"])
    def test_valid_prefixes_pass(self, prefix: str) -> None:
        assert prefixes.prefix_format_error(prefix) is None

    @pytest.mark.parametrize("prefix", ["", " ", "9bad", "_lead", "has-dash", "a__b", "é"])
    def test_invalid_prefixes_name_the_value(self, prefix: str) -> None:
        error = prefixes.prefix_format_error(prefix)
        assert error is not None
        assert f"'{prefix}'" in error


class TestEmptyPrefixKeys:
    def test_empty_and_whitespace_flagged_sorted(self) -> None:
        assert prefixes.empty_prefix_keys({"b": " ", "a": "", "c": "ok"}) == ["a", "b"]

    def test_all_nonempty_yields_nothing(self) -> None:
        assert prefixes.empty_prefix_keys({"a": "x", "b": "y"}) == []


class TestPrefixCollisions:
    def test_no_collisions(self) -> None:
        assert prefixes.prefix_collisions({"a": "x", "b": "y"}) == {}

    def test_collision_groups_sorted_keys(self) -> None:
        got = prefixes.prefix_collisions({"b": "dup", "a": "dup", "c": "solo"})
        assert got == {"dup": ["a", "b"]}

    def test_three_way_collision(self) -> None:
        got = prefixes.prefix_collisions({"a": "dup", "b": "dup", "c": "dup"})
        assert got == {"dup": ["a", "b", "c"]}

    def test_format_collision_error_names_prefix_and_keys(self) -> None:
        msg = prefixes.format_collision_error({"dup": ["a", "b"]})
        assert "Duplicate upstream prefixes detected" in msg
        assert "'dup'" in msg and "a" in msg and "b" in msg


class TestSharedValidatorWiring:
    """CLI and runtime must go through the same shared functions — the whole
    point of ``proxy/prefixes.py`` is that the two sides can't diverge again.
    Source inspection, not behavior: behavior is covered by the runtime
    tests in ``test_config_constraints.py`` and the CLI tests in
    ``tests/cli/test_proxy_cli.py``; this pins the *mechanism*."""

    def test_runtime_uniqueness_validator_uses_shared_function(self) -> None:
        src = inspect.getsource(ProxyConfig._check_unique_upstream_prefixes)
        assert "prefixes.prefix_collisions(" in src
        assert "prefixes.format_collision_error(" in src

    def test_runtime_nonempty_validator_uses_shared_function(self) -> None:
        src = inspect.getsource(ProxyConfig._check_nonempty_upstream_prefixes)
        assert "prefixes.empty_prefix_keys(" in src

    def test_cli_add_uses_shared_functions(self) -> None:
        from memtomem_stm.cli import proxy as cli_proxy

        assert cli_proxy.add.callback is not None
        src = inspect.getsource(cli_proxy.add.callback)
        assert "prefixes.prefix_collisions(" in src
        assert "prefixes.prefix_format_error(" in src

    def test_cli_prompt_uses_shared_format_check(self) -> None:
        from memtomem_stm.cli import proxy as cli_proxy

        src = inspect.getsource(cli_proxy._prompt_prefix)
        assert "prefixes.prefix_format_error(" in src


class TestPromptPrefixTaken:
    """Interactive prefix prompt re-prompts on a collision — the suggestion
    default alone can't stop a user-typed duplicate (``add --from-clients``
    and ``mms init`` pass the run's ``used_prefixes`` as ``taken``)."""

    def _invoke(self, taken: set[str], input_text: str):
        from memtomem_stm.cli.proxy import _prompt_prefix

        @click.command()
        def _cmd() -> None:
            click.echo(f"got={_prompt_prefix(taken=taken)}")

        return CliRunner().invoke(_cmd, input=input_text)

    def test_taken_prefix_reprompts_until_unique(self) -> None:
        result = self._invoke({"fs"}, "fs\nfs2\n")
        assert result.exit_code == 0
        assert "already used" in result.output
        assert "got=fs2" in result.output

    def test_untaken_prefix_accepted_first_try(self) -> None:
        result = self._invoke({"fs"}, "gh\n")
        assert result.exit_code == 0
        assert "already used" not in result.output
        assert "got=gh" in result.output
