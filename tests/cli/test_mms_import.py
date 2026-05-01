"""CliRunner tests for ``mms import`` (RFC §7.2)."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.mms_import import import_command
from memtomem_stm.mms import state
from memtomem_stm.mms.secrets import REDACTED_DISPLAY


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Pin HOME so ``~/.mms`` and ``~/.claude.json`` etc. land in tmp."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return {"home": tmp_path, "cwd": cwd}


def _seed_claude_code(sandbox, mcps: dict) -> None:
    cfg = {"mcpServers": mcps}
    (sandbox["home"] / ".claude.json").write_text(json.dumps(cfg), encoding="utf-8")


# ---------------------------------------------------------------------------
# --plan default
# ---------------------------------------------------------------------------


class TestPlan:
    def test_no_hosts_no_candidates(self, runner, sandbox):
        res = runner.invoke(import_command, ["--from", "claude-code"])
        assert res.exit_code == 0, res.output
        assert "No MCP definitions found" in res.output

    def test_plan_default_redacts_secrets(self, runner, sandbox):
        _seed_claude_code(
            sandbox,
            {
                "github": {
                    "command": "npx",
                    "args": ["-y", "@mcp/gh"],
                    "env": {"GITHUB_TOKEN": "ghp_realsecret"},
                },
            },
        )
        res = runner.invoke(import_command, ["--from", "claude-code"])
        assert res.exit_code == 0, res.output
        assert "ghp_realsecret" not in res.output  # ← critical: secret never printed
        assert REDACTED_DISPLAY in res.output
        assert "GITHUB_TOKEN" in res.output
        assert "matches *TOKEN*" in res.output  # classification reason surfaced
        # apply hint at the bottom
        assert "mms import --from claude-code --apply" in res.output

    def test_show_imported_reveals_secrets(self, runner, sandbox):
        _seed_claude_code(
            sandbox,
            {
                "github": {
                    "command": "npx",
                    "env": {"GITHUB_TOKEN": "ghp_realsecret"},
                },
            },
        )
        res = runner.invoke(import_command, ["--from", "claude-code", "--show-imported"])
        assert res.exit_code == 0, res.output
        assert "ghp_realsecret" in res.output
        assert "(secret)" in res.output  # tag still indicates classification

    def test_plan_does_not_write_registry(self, runner, sandbox):
        _seed_claude_code(sandbox, {"foo": {"command": "echo"}})
        runner.invoke(import_command, ["--from", "claude-code"])
        # No --apply → registry is still empty (no file)
        assert not state.registry_path().exists()

    def test_plan_summary_counts(self, runner, sandbox):
        _seed_claude_code(
            sandbox,
            {
                "foo": {"command": "echo"},
                "bar": {"command": "echo"},
            },
        )
        res = runner.invoke(import_command, ["--from", "claude-code"])
        assert "to add (new):    2" in res.output
        assert "unchanged:       0" in res.output
        assert "conflicts:       0" in res.output


# ---------------------------------------------------------------------------
# --apply
# ---------------------------------------------------------------------------


class TestApply:
    def test_apply_writes_registry_with_secrets_intact(self, runner, sandbox):
        _seed_claude_code(
            sandbox,
            {
                "filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]},
                "github": {
                    "command": "npx",
                    "env": {"GITHUB_TOKEN": "ghp_realsecret"},
                },
            },
        )
        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res.exit_code == 0, res.output
        assert "Wrote 2 new entries" in res.output

        cfg = state.load_registry()
        assert set(cfg.servers.keys()) == {"filesystem", "github"}
        # Secrets stored verbatim in the registry (registry is gitignored).
        assert cfg.servers["github"].env == {"GITHUB_TOKEN": "ghp_realsecret"}

    def test_apply_then_replay_idempotent(self, runner, sandbox):
        _seed_claude_code(sandbox, {"foo": {"command": "echo"}})
        runner.invoke(import_command, ["--from", "claude-code", "--apply"])

        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res.exit_code == 0, res.output
        assert "Already up to date" in res.output

        # Registry unchanged — same single entry.
        cfg = state.load_registry()
        assert list(cfg.servers.keys()) == ["foo"]

    def test_apply_first_import_wins_on_conflict(self, runner, sandbox):
        # Pre-seed registry with `foo` pointing at one command.
        first = state.RegistryConfig(
            servers={"foo": state.RegistryServer(command="first-cmd", prefix="foo")}
        )
        state.save_registry(first)

        # Host config now declares `foo` with a different command.
        _seed_claude_code(sandbox, {"foo": {"command": "second-cmd"}})
        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res.exit_code == 0, res.output
        assert "conflicts:       1" in res.output
        assert "differs from existing registry entry" in res.output

        # Registry should NOT have been overwritten.
        cfg = state.load_registry()
        assert cfg.servers["foo"].command == "first-cmd"

    def test_apply_writes_new_alongside_existing(self, runner, sandbox):
        # Pre-seed registry with one entry.
        first = state.RegistryConfig(
            servers={"existing": state.RegistryServer(command="echo", prefix="ex")}
        )
        state.save_registry(first)

        _seed_claude_code(
            sandbox,
            {
                "existing": {
                    "command": "echo",
                    "prefix": "ex",
                },  # would be conflict — but identical
                "newcomer": {"command": "ls"},
            },
        )
        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res.exit_code == 0, res.output
        cfg = state.load_registry()
        assert set(cfg.servers.keys()) == {"existing", "newcomer"}


# ---------------------------------------------------------------------------
# Cross-host first-import-wins (same name in two hosts within one --from all)
# ---------------------------------------------------------------------------


class TestCrossHostConflict:
    def test_same_name_different_shape_across_hosts_is_conflict(self, runner, sandbox):
        # claude-code: foo with command "first"
        _seed_claude_code(sandbox, {"foo": {"command": "first"}})
        # cursor: foo with command "second"
        cursor_dir = sandbox["home"] / ".cursor"
        cursor_dir.mkdir()
        (cursor_dir / "mcp.json").write_text(
            json.dumps({"mcpServers": {"foo": {"command": "second"}}}), encoding="utf-8"
        )

        res = runner.invoke(import_command, ["--from", "all", "--apply"])
        assert res.exit_code == 0, res.output
        # First (claude-code) wins; second (cursor) reported as conflict.
        cfg = state.load_registry()
        assert cfg.servers["foo"].command == "first"
        assert "conflicts:       1" in res.output


# ---------------------------------------------------------------------------
# Wire-in smoke
# ---------------------------------------------------------------------------


def test_import_wired_into_top_level_cli(runner):
    from memtomem_stm.cli.proxy import cli

    res = runner.invoke(cli, ["import", "--help"])
    assert res.exit_code == 0, res.output
    # Sanity: the help text mentions both modes
    assert "--plan" in res.output
    assert "--apply" in res.output
    assert "--show-imported" in res.output
