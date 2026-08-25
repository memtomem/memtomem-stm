"""Lazy subcommand loading for the ``cli`` group.

The hook hot path (``mms hook``, run by a host on every built-in tool call)
imports ``cli/proxy.py`` per invocation. The nested command families used to
be imported eagerly there, and ``selection_cmd`` alone pulled in
``proxy.manager`` and the whole MCP SDK (~230 ms of a ~340 ms import). These
tests pin both halves of the fix: importing the group stays light, and every
lazy entry still resolves and dispatches like an eagerly registered command.
"""

from __future__ import annotations

import json
import subprocess
import sys

import click
import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import _LAZY_SUBCOMMANDS, cli

# Modules that must NOT load when the CLI group is merely imported. The first
# two are the expensive transitive dependencies the lazy split exists for; the
# rest are the lazy sibling modules themselves.
_MUST_STAY_UNLOADED = [
    "mcp",
    "memtomem_stm.proxy.manager",
    *(module for module, _attr in _LAZY_SUBCOMMANDS.values()),
]


class TestImportStaysLight:
    def test_importing_cli_group_loads_no_lazy_modules(self):
        """Executed in a subprocess: this test process has already imported
        the CLI group (and other tests import sibling modules directly), so
        only a fresh interpreter can observe the import-time behavior."""
        code = (
            "import json, sys\n"
            "from memtomem_stm.cli.proxy import cli\n"
            "print(json.dumps([m for m in sys.argv[1:] if m in sys.modules]))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code, *_MUST_STAY_UNLOADED],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == []


class TestLazyEntriesStillDispatch:
    @pytest.fixture
    def runner(self):
        return CliRunner()

    def test_registry_resolves_to_matching_commands(self):
        group = cli
        assert isinstance(group, click.Group)
        ctx = click.Context(group)
        for name in _LAZY_SUBCOMMANDS:
            cmd = group.get_command(ctx, name)
            assert isinstance(cmd, click.Command), name
            assert cmd.name == name

    def test_root_help_lists_every_lazy_command(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        for name in _LAZY_SUBCOMMANDS:
            assert f"\n  {name} " in result.output

    @pytest.mark.parametrize("name", sorted(_LAZY_SUBCOMMANDS))
    def test_lazy_subcommand_help_dispatches(self, runner, name):
        result = runner.invoke(cli, [name, "--help"])
        assert result.exit_code == 0
        assert "Usage:" in result.output

    def test_unknown_command_still_rejected(self, runner):
        result = runner.invoke(cli, ["no-such-command"])
        assert result.exit_code != 0
        assert "No such command" in result.output

    def test_typo_suggestion_includes_lazy_commands(self):
        """Click builds "Did you mean" suggestions from ``self.commands``,
        not ``list_commands`` — resolve_command must materialize the lazy
        registry on an unknown name or the suggestions silently lose the
        seven lazy families. Fresh subprocess: in-process tests have already
        cached every lazy command onto the module-global group."""
        proc = subprocess.run(
            [sys.executable, "-m", "memtomem_stm", "selektion"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode != 0
        assert "Did you mean 'selection'" in proc.stderr

    def test_valid_lazy_dispatch_imports_only_its_own_module(self):
        """The unknown-name materialization must not fire for a VALID lazy
        invocation — the hook hot path stays on the one module it needs."""
        code = (
            "import sys\n"
            "from click.testing import CliRunner\n"
            "from memtomem_stm.cli.proxy import cli\n"
            "r = CliRunner().invoke(cli, ['hook', '--help'])\n"
            "assert r.exit_code == 0, r.output\n"
            "import json\n"
            "print(json.dumps([m for m in "
            "('mcp', 'memtomem_stm.cli.selection_cmd', 'memtomem_stm.cli.daemon_cmd') "
            "if m in sys.modules]))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == []
