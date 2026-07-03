"""CliRunner tests for ``mms project ...`` (RFC §7.1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.mms_project import (
    _REGISTRY_EMPTY_MSG,
    _show_git_no_marker_text,
    _show_no_marker_no_git_text,
    project_group,
)
from memtomem_stm.mms import state
from helpers import set_home


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Pin HOME so ``~/.mms`` is sandboxed; chdir to a fresh project dir.

    Returns a dict with the sandbox HOME and the cwd directory so tests
    can do path assertions.
    """
    set_home(monkeypatch, tmp_path)
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    monkeypatch.chdir(proj_dir)
    return {"home": tmp_path, "cwd": proj_dir}


def _seed_registry(*names: str) -> None:
    cfg = state.RegistryConfig(
        servers={n: state.RegistryServer(command="echo", prefix=n[:2]) for n in names}
    )
    state.save_registry(cfg)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_creates_marker_and_indexes(runner, sandbox):
    res = runner.invoke(project_group, ["init"])
    assert res.exit_code == 0, res.output

    marker = sandbox["cwd"] / state.PROJECT_MARKER_RELPATH
    assert marker.is_file()

    cfg = state.load_project_config(marker)
    assert cfg.project.name == "proj"
    assert cfg.mcp.enabled == []

    idx = state.load_projects_index()
    assert len(idx.projects) == 1
    assert idx.projects[0].name == "proj"
    assert Path(idx.projects[0].path) == sandbox["cwd"]


def test_init_with_explicit_path_and_name(runner, sandbox):
    other = sandbox["home"] / "other-proj"
    other.mkdir()
    res = runner.invoke(project_group, ["init", str(other), "--name", "custom"])
    assert res.exit_code == 0, res.output

    marker = other / state.PROJECT_MARKER_RELPATH
    assert marker.is_file()
    assert state.load_project_config(marker).project.name == "custom"


def test_init_aborts_when_marker_exists(runner, sandbox):
    runner.invoke(project_group, ["init"])
    res = runner.invoke(project_group, ["init"])
    assert res.exit_code != 0
    assert "already exists" in res.output
    assert "--force" in res.output


def test_init_force_overwrites(runner, sandbox):
    runner.invoke(project_group, ["init", "--name", "first"])
    res = runner.invoke(project_group, ["init", "--name", "second", "--force"])
    assert res.exit_code == 0, res.output
    marker = sandbox["cwd"] / state.PROJECT_MARKER_RELPATH
    assert state.load_project_config(marker).project.name == "second"


def test_init_rejects_nonexistent_path(runner, sandbox):
    nope = sandbox["home"] / "does-not-exist"
    res = runner.invoke(project_group, ["init", str(nope)])
    assert res.exit_code != 0
    assert "Not a directory" in res.output or "does not exist" in res.output


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_no_marker_no_git_friendly_text(runner, sandbox):
    """RFC §6.1 — pinned literal text used by `mms project show` UX."""
    res = runner.invoke(project_group, ["show"])
    assert res.exit_code == 0, res.output
    expected = _show_no_marker_no_git_text(sandbox["cwd"].resolve())
    assert res.output == expected


def test_show_git_no_marker_friendly_text(runner, sandbox):
    (sandbox["cwd"] / ".git").mkdir()
    res = runner.invoke(project_group, ["show"])
    assert res.exit_code == 0, res.output
    expected = _show_git_no_marker_text(sandbox["cwd"].resolve())
    assert res.output == expected


def test_show_marker_renders_enabled_list(runner, sandbox):
    runner.invoke(project_group, ["init"])
    _seed_registry("filesystem", "github")
    runner.invoke(project_group, ["enable", "filesystem", "github"])
    res = runner.invoke(project_group, ["show"])
    assert res.exit_code == 0, res.output
    assert "Project: proj" in res.output
    assert "Detected via: marker" in res.output
    assert "Enabled MCPs: filesystem, github" in res.output


def test_show_marker_with_no_enabled(runner, sandbox):
    runner.invoke(project_group, ["init"])
    res = runner.invoke(project_group, ["show"])
    assert "Enabled MCPs: (none)" in res.output


def test_show_json_marker(runner, sandbox):
    runner.invoke(project_group, ["init"])
    _seed_registry("filesystem")
    runner.invoke(project_group, ["enable", "filesystem"])
    res = runner.invoke(project_group, ["show", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["name"] == "proj"
    assert payload["source"] == "marker"
    assert payload["enabled"] == ["filesystem"]


def test_show_json_cwd_fallback(runner, sandbox):
    res = runner.invoke(project_group, ["show", "--json"])
    payload = json.loads(res.output)
    assert payload["source"] == "cwd"
    assert payload["enabled"] == []


def test_show_marker_walk_up(runner, sandbox, monkeypatch):
    runner.invoke(project_group, ["init"])
    sub = sandbox["cwd"] / "sub" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    res = runner.invoke(project_group, ["show"])
    assert "Detected via: marker" in res.output
    assert "Project: proj" in res.output


def test_show_by_name(runner, sandbox):
    other = sandbox["home"] / "other-proj"
    other.mkdir()
    runner.invoke(project_group, ["init", str(other), "--name", "elsewhere"])
    res = runner.invoke(project_group, ["show", "elsewhere"])
    assert res.exit_code == 0
    assert "Project: elsewhere" in res.output


def test_show_by_name_not_found(runner, sandbox):
    res = runner.invoke(project_group, ["show", "missing"])
    assert res.exit_code != 0
    assert "not found" in res.output


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_empty(runner, sandbox):
    res = runner.invoke(project_group, ["list"])
    assert res.exit_code == 0
    assert "No projects indexed" in res.output


def test_list_marks_current(runner, sandbox):
    runner.invoke(project_group, ["init"])
    other = sandbox["home"] / "other-proj"
    other.mkdir()
    runner.invoke(project_group, ["init", str(other), "--name", "other"])

    res = runner.invoke(project_group, ["list"])
    assert res.exit_code == 0, res.output
    lines = res.output.strip().splitlines()
    # current cwd is /tmp/.../proj — should be marked with *
    cwd_line = next(line for line in lines if "proj\t" in line and "other-proj" not in line)
    assert cwd_line.startswith("*")
    other_line = next(line for line in lines if "other-proj" in line)
    assert other_line.startswith(" ")


def test_list_json(runner, sandbox):
    runner.invoke(project_group, ["init"])
    res = runner.invoke(project_group, ["list", "--json"])
    payload = json.loads(res.output)
    assert len(payload["projects"]) == 1
    assert payload["projects"][0]["name"] == "proj"
    assert payload["projects"][0]["current"] is True
    assert payload["pruned"] == []


def test_list_prune_removes_stale(runner, sandbox):
    runner.invoke(project_group, ["init"])

    stale_dir = sandbox["home"] / "stale"
    stale_dir.mkdir()
    runner.invoke(project_group, ["init", str(stale_dir), "--name", "stale"])

    # Now delete the stale dir on disk; index still has it.
    import shutil

    shutil.rmtree(stale_dir)

    res = runner.invoke(project_group, ["list", "--prune"])
    assert res.exit_code == 0, res.output
    assert "pruned: stale" in res.output
    assert "Pruned 1" in res.output

    # Index should now have only the live project.
    idx = state.load_projects_index()
    names = {e.name for e in idx.projects}
    assert names == {"proj"}


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------


def test_enable_against_empty_registry_friendly_error(runner, sandbox):
    """Pinned graceful-error contract from W1 PR1 plan."""
    runner.invoke(project_group, ["init"])
    res = runner.invoke(project_group, ["enable", "filesystem"])
    assert res.exit_code != 0
    # stderr in CliRunner with mix_stderr=True (default) lands in res.output
    assert _REGISTRY_EMPTY_MSG.strip() in res.output


def test_enable_requires_marker(runner, sandbox):
    _seed_registry("filesystem")  # registry non-empty so the check passes
    res = runner.invoke(project_group, ["enable", "filesystem"])
    assert res.exit_code != 0
    assert "No project marker" in res.output
    assert "mms project init" in res.output


def test_enable_appends_unique(runner, sandbox):
    runner.invoke(project_group, ["init"])
    _seed_registry("a", "b", "c")
    runner.invoke(project_group, ["enable", "a", "b"])
    runner.invoke(project_group, ["enable", "b", "c"])  # b is dup

    cfg = state.load_project_config(sandbox["cwd"] / state.PROJECT_MARKER_RELPATH)
    assert cfg.mcp.enabled == ["a", "b", "c"]


def test_show_duplicate_name_is_ambiguous(runner, sandbox):
    # The index is keyed by canonical path, so two projects can share a name.
    # `show NAME` must refuse like enable/disable do, not silently pick one.
    a = sandbox["home"] / "proj-a"
    b = sandbox["home"] / "proj-b"
    a.mkdir()
    b.mkdir()
    runner.invoke(project_group, ["init", str(a), "--name", "same"])
    runner.invoke(project_group, ["init", str(b), "--name", "same"])

    res = runner.invoke(project_group, ["show", "same"])
    assert res.exit_code != 0
    assert "ambiguous (2 matches)" in res.output


def test_enable_unknown_name_rejected_with_hint(runner, sandbox):
    # A typo must not be silently persisted to the enabled list — it would
    # resolve to nothing at proxy time with no signal to the user.
    runner.invoke(project_group, ["init"])
    _seed_registry("filesystem")

    res = runner.invoke(project_group, ["enable", "filesytem"])  # typo
    assert res.exit_code != 0
    assert "Unknown MCP name(s): filesytem" in res.output
    assert "filesystem" in res.output  # registered set shown as the hint

    cfg = state.load_project_config(sandbox["cwd"] / state.PROJECT_MARKER_RELPATH)
    assert cfg.mcp.enabled == []  # nothing persisted


def test_enable_with_project_flag(runner, sandbox):
    other = sandbox["home"] / "other-proj"
    other.mkdir()
    runner.invoke(project_group, ["init", str(other), "--name", "elsewhere"])
    _seed_registry("filesystem")
    res = runner.invoke(project_group, ["enable", "filesystem", "--project", "elsewhere"])
    assert res.exit_code == 0, res.output

    other_marker = other / state.PROJECT_MARKER_RELPATH
    assert state.load_project_config(other_marker).mcp.enabled == ["filesystem"]


def test_disable_removes_listed(runner, sandbox):
    runner.invoke(project_group, ["init"])
    _seed_registry("a", "b")
    runner.invoke(project_group, ["enable", "a", "b"])
    runner.invoke(project_group, ["disable", "a"])

    cfg = state.load_project_config(sandbox["cwd"] / state.PROJECT_MARKER_RELPATH)
    assert cfg.mcp.enabled == ["b"]


def test_disable_works_with_empty_registry(runner, sandbox):
    """Disable doesn't require a populated registry — it only mutates project state."""
    runner.invoke(project_group, ["init"])
    _seed_registry("a")
    runner.invoke(project_group, ["enable", "a"])
    # Now wipe the registry.
    state.save_registry(state.RegistryConfig())

    res = runner.invoke(project_group, ["disable", "a"])
    assert res.exit_code == 0, res.output
    cfg = state.load_project_config(sandbox["cwd"] / state.PROJECT_MARKER_RELPATH)
    assert cfg.mcp.enabled == []


def test_disable_noop_when_not_enabled(runner, sandbox):
    runner.invoke(project_group, ["init"])
    res = runner.invoke(project_group, ["disable", "never-enabled"])
    assert res.exit_code == 0
    assert "No changes" in res.output


# ---------------------------------------------------------------------------
# Project resolution edge case — --project name with stale index
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Wire-in smoke — `mms project ...` is reachable through the top-level cli
# ---------------------------------------------------------------------------


def test_project_group_wired_into_top_level_cli(runner):
    """`mms project --help` lists all five W1 subcommands.

    Pins the wiring in proxy.py — if the import or `add_command` line is
    accidentally removed, this test catches it before the §12 e2e
    acceptance test does (faster signal).
    """
    from memtomem_stm.cli.proxy import cli

    res = runner.invoke(cli, ["project", "--help"])
    assert res.exit_code == 0, res.output
    for name in ["init", "show", "list", "enable", "disable"]:
        assert name in res.output, f"subcommand '{name}' missing from `mms project --help`"


def test_enable_project_flag_missing_marker_complains(runner, sandbox):
    other = sandbox["home"] / "other-proj"
    other.mkdir()
    runner.invoke(project_group, ["init", str(other), "--name", "elsewhere"])
    # Simulate user deleting the marker directly without pruning the index.
    (other / state.PROJECT_MARKER_RELPATH).unlink()
    _seed_registry("filesystem")
    res = runner.invoke(project_group, ["enable", "filesystem", "--project", "elsewhere"])
    assert res.exit_code != 0
    assert "marker file is missing" in res.output
    assert "list --prune" in res.output


# ---------------------------------------------------------------------------
# write lock (#582) — the shared ~/.mms registry lock serializes the mutators
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock is POSIX-only; write_lock is documented as a no-op on Windows",
)
@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["init"], id="init"),
        pytest.param(["enable", "filesystem"], id="enable"),
        pytest.param(["disable", "filesystem"], id="disable"),
        pytest.param(["list", "--prune"], id="list-prune"),
    ],
)
def test_project_mutator_under_held_lock_exits_cleanly(runner, sandbox, monkeypatch, argv):
    # Every project mutator must serialize on the shared ~/.mms registry write
    # lock; a concurrent run that can't acquire it times out with a clean,
    # attributed error instead of doing an unlocked read-modify-write of the
    # shared projects.toml and dropping the other run's rows (lost update).
    if argv[0] in {"enable", "disable"}:
        runner.invoke(project_group, ["init"])
        _seed_registry("filesystem")
    monkeypatch.setattr(state, "WRITE_LOCK_TIMEOUT_SECONDS", 0.2)

    with state.write_lock():  # hold the default registry lock like a sibling run
        res = runner.invoke(project_group, argv)

    assert res.exit_code == 1
    # WriteLockTimeout is surfaced as a Click error, not a traceback.
    assert "Error" in res.output
    # #582 follow-up: the timeout attribution must name the `mms project`
    # mutators too, not just host sync / import — they share this lock, so an
    # operator whose project command timed out needs to see it in the hint.
    # Pin the specific subcommand tokens so the assertion fails if the hint
    # stops enumerating them (not just the "mms project" prefix).
    assert "mms project" in res.output
    for token in ("init", "enable", "disable", "list --prune"):
        assert token in res.output, f"holder hint should name `{token}`: {res.output!r}"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock is POSIX-only; write_lock is documented as a no-op on Windows",
)
def test_list_without_prune_ignores_held_lock(runner, sandbox, monkeypatch):
    # A plain list is read-only, so it must not queue behind (or fail on) the
    # write lock — mirroring the --plan skip convention.
    runner.invoke(project_group, ["init"])
    monkeypatch.setattr(state, "WRITE_LOCK_TIMEOUT_SECONDS", 0.2)

    with state.write_lock():
        res = runner.invoke(project_group, ["list"])

    assert res.exit_code == 0, res.output
    assert "proj" in res.output
