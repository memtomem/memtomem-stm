"""CliRunner tests for ``mms project ...`` (RFC §7.1)."""

from __future__ import annotations

import errno
import json
import shutil
import shlex
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
from helpers import set_home, skip_if_inside_git


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
    skip_if_inside_git(sandbox["cwd"].resolve())
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
    skip_if_inside_git(sandbox["cwd"].resolve())
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


@pytest.mark.parametrize("json_output", [False, True])
def test_prune_missing_marker_preserves_files_and_is_idempotent(runner, sandbox, json_output):
    assert runner.invoke(project_group, ["init", "--name", "demo"]).exit_code == 0
    root = sandbox["cwd"]
    (root / state.PROJECT_MARKER_RELPATH).unlink()
    keep = root / "keep.txt"
    keep.write_text("user content")
    args = ["list", "--prune"] + (["--json"] if json_output else [])
    result = runner.invoke(project_group, args)
    assert result.exit_code == 0, result.output
    if json_output:
        assert json.loads(result.stdout)["pruned"] == [{"name": "demo", "path": str(root)}]
    else:
        assert "pruned: demo" in result.stdout
    assert state.load_projects_index().projects == []
    assert keep.read_text() == "user content"
    second = runner.invoke(project_group, args)
    assert second.exit_code == 0
    if json_output:
        assert json.loads(second.stdout)["pruned"] == []
    else:
        assert "pruned:" not in second.stdout
    missing = runner.invoke(project_group, ["show", "demo"])
    assert missing.exit_code == 1
    assert "not found" in missing.output and "--prune" not in missing.output


@pytest.mark.parametrize("command", ["show", "enable", "disable", "route"])
def test_missing_marker_hint_preserves_name(runner, sandbox, command):
    from memtomem_stm.cli.proxy import _shell_join

    root = sandbox["home"] / "project with spaces"
    root.mkdir()
    name = "demo ' $name"
    _seed_registry("fake")
    assert runner.invoke(project_group, ["init", str(root), "--name", name]).exit_code == 0
    (root / state.PROJECT_MARKER_RELPATH).unlink()
    args = (
        ["show", name]
        if command == "show"
        else [command, *(["fake"] if command in ("enable", "disable") else []), "--project", name]
    )
    result = runner.invoke(project_group, args)
    assert result.exit_code == 1, result.output
    repair_args = ["mms", "project", "init", str(root), "--name", name]
    rendered = _shell_join(repair_args)
    assert f"`{rendered}`" in result.output
    assert "enabled MCPs will be empty" in result.output
    assert "list --prune" in result.output
    # Native quoting already has Windows command-shell tests. Execute the
    # displayed POSIX argv here; keep the recovery journey portable on Windows.
    recovered_args = shlex.split(rendered)[2:] if sys.platform != "win32" else repair_args[2:]
    assert runner.invoke(project_group, recovered_args).exit_code == 0
    shown = runner.invoke(project_group, ["show", name, "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["name"] == name
    assert json.loads(shown.stdout)["enabled"] == []
    assert state.load_projects_index().projects[0].name == name


@pytest.mark.parametrize("json_output", [False, True])
def test_prune_non_directory_root_preserves_file_and_valid_project(runner, sandbox, json_output):
    assert runner.invoke(project_group, ["init"]).exit_code == 0
    root = sandbox["home"] / "stale"
    root.mkdir()
    assert runner.invoke(project_group, ["init", str(root), "--name", "stale"]).exit_code == 0
    marker = root / state.PROJECT_MARKER_RELPATH
    marker.unlink()
    marker.parent.rmdir()
    root.rmdir()
    root.write_text("user file")
    shown = runner.invoke(project_group, ["show", "stale"])
    assert shown.exit_code == 1
    assert "list --prune" in shown.output
    assert "mms project init" not in shown.output
    result = runner.invoke(project_group, ["list", "--prune"] + (["--json"] if json_output else []))
    assert result.exit_code == 0, result.output
    if json_output:
        assert json.loads(result.stdout)["pruned"] == [{"name": "stale", "path": str(root)}]
    else:
        assert "pruned: stale" in result.stdout
    assert root.read_text() == "user file"
    assert [entry.name for entry in state.load_projects_index().projects] == ["proj"]
    again = runner.invoke(project_group, ["list", "--prune", "--json"])
    assert again.exit_code == 0
    assert json.loads(again.stdout)["pruned"] == []


def test_enoent_with_file_parent_does_not_offer_init(runner, sandbox, monkeypatch):
    assert runner.invoke(project_group, ["init", "--name", "demo"]).exit_code == 0
    marker = sandbox["cwd"] / state.PROJECT_MARKER_RELPATH
    marker.unlink()
    marker.parent.rmdir()
    marker.parent.write_text("user file")
    original = Path.stat

    def missing(path, *args, **kwargs):
        if path == marker:
            raise FileNotFoundError(errno.ENOENT, "simulated missing-path report")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", missing)
    shown = runner.invoke(project_group, ["show", "demo"])
    assert shown.exit_code == 1
    assert "mms project init" not in shown.output
    assert "list --prune" in shown.output
    assert runner.invoke(project_group, ["list", "--prune"]).exit_code == 0
    assert marker.parent.read_text() == "user file"


def test_unrenderable_name_omits_recreate_command(runner, sandbox):
    name = "demo\x1b[31m"
    assert runner.invoke(project_group, ["init", "--name", name]).exit_code == 0
    (sandbox["cwd"] / state.PROJECT_MARKER_RELPATH).unlink()
    result = runner.invoke(project_group, ["show", name])
    assert result.exit_code == 1
    assert "Recreate the marker" not in result.output
    assert "\x1b" not in result.output
    assert "list --prune" in result.output


@pytest.mark.parametrize("damage", ["directory", "malformed", "invalid_utf8"])
def test_prune_and_named_lookup_keep_invalid_projects(runner, sandbox, damage):
    root = sandbox["home"] / "invalid"
    root.mkdir()
    assert runner.invoke(project_group, ["init", str(root), "--name", "demo"]).exit_code == 0
    marker = root / state.PROJECT_MARKER_RELPATH
    if damage == "directory":
        marker.unlink()
        marker.mkdir()
    elif damage == "malformed":
        marker.write_text("invalid = [")
    elif damage == "invalid_utf8":
        marker.write_bytes(b"\xff")
    # A stale entry before the invalid one must not be partially pruned.
    idx = state.load_projects_index()
    idx.projects.insert(
        0,
        state.ProjectIndexEntry(
            name="gone", path=str(root.parent / "gone"), last_seen="2026-10-16T00:00:00Z"
        ),
    )
    state.save_projects_index(idx)
    before = state.projects_index_path().read_bytes()
    for args in (
        ["list", "--prune", "--json"],
        ["show", "demo"],
        ["disable", "fake", "--project", "demo"],
    ):
        result = runner.invoke(project_group, args)
        assert result.exit_code == 1, result.output
        assert "list --prune" not in result.output and "mms project init" not in result.output
        assert state.projects_index_path().read_bytes() == before
        if "--json" in args:
            assert json.loads(result.stdout)["error"] == "mms_state_invalid"
            assert "Project 'demo': prune aborted; index unchanged" in result.stdout


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EPERM, errno.EIO])
@pytest.mark.parametrize("target", ["root", "marker", "read"])
def test_prune_and_lookup_do_not_confuse_io_errors_with_absence(
    runner, sandbox, monkeypatch, error_number, target
):
    assert runner.invoke(project_group, ["init", "--name", "demo"]).exit_code == 0
    root = sandbox["cwd"]
    marker = root / state.PROJECT_MARKER_RELPATH
    before = state.projects_index_path().read_bytes()
    method = "open" if target == "read" else "stat"
    original = getattr(Path, method)
    failing = root if target == "root" else marker

    def fail(path, *args, **kwargs):
        if path == failing:
            raise OSError(error_number, "sensitive-error-detail")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, fail)
    for args in (
        ["list", "--prune", "--json"],
        ["show", "demo"],
        ["disable", "fake", "--project", "demo"],
    ):
        result = runner.invoke(project_group, args)
        assert result.exit_code == 1, result.output
        assert "cannot" in result.output
        assert "sensitive-error-detail" not in result.output
        assert "list --prune" not in result.output and "mms project init" not in result.output
        assert state.projects_index_path().read_bytes() == before


@pytest.mark.parametrize("error_type", [FileNotFoundError, NotADirectoryError])
def test_prune_only_confirmed_missing_marker_errors(runner, sandbox, monkeypatch, error_type):
    assert runner.invoke(project_group, ["init", "--name", "demo"]).exit_code == 0
    marker = sandbox["cwd"] / state.PROJECT_MARKER_RELPATH
    original = Path.stat

    def missing(path, *args, **kwargs):
        if path == marker:
            raise error_type()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", missing)
    result = runner.invoke(project_group, ["list", "--prune", "--json"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)["pruned"]) == 1
    assert state.load_projects_index().projects == []


# ---------------------------------------------------------------------------
# route — project registry selection -> additive STM upstreams
# ---------------------------------------------------------------------------


def _seed_routable_project(runner: CliRunner) -> None:
    state.save_registry(
        state.RegistryConfig(
            servers={
                "filesystem": state.RegistryServer(
                    command="npx",
                    args=["-y", "@modelcontextprotocol/server-filesystem", "/work"],
                    env={"ROUTE_SECRET": "keep-private"},
                    prefix="fs",
                ),
                "github": state.RegistryServer(command="github-mcp", prefix="gh"),
            }
        )
    )
    runner.invoke(project_group, ["init"])
    enabled = runner.invoke(project_group, ["enable", "filesystem", "github"])
    assert enabled.exit_code == 0, enabled.output


def test_route_preview_is_read_only_and_redacted(runner, sandbox):
    _seed_routable_project(runner)
    config = sandbox["home"] / "proxy.json"

    res = runner.invoke(project_group, ["route", "--config", str(config), "--json"])

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["mode"] == "preview"
    assert payload["planned"] == ["filesystem", "github"]
    assert payload["applied"] == []
    assert "keep-private" not in res.output
    assert not config.exists()


def test_route_help_uses_shared_proxy_config_default(runner):
    from memtomem_stm.cli._defaults import DEFAULT_PROXY_CONFIG

    res = runner.invoke(project_group, ["route", "--help"])

    assert res.exit_code == 0, res.output
    assert str(DEFAULT_PROXY_CONFIG) in res.output


def test_route_no_flag_honors_env_config_path(runner, sandbox, monkeypatch):
    """#848: an untyped ``--config`` resolves ``MEMTOMEM_STM_PROXY__CONFIG_PATH``
    — route must update the file the server reads, not the literal default."""
    _seed_routable_project(runner)
    config = sandbox["home"] / "env_proxy.json"
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(config))

    res = runner.invoke(project_group, ["route", "--apply", "--json"])

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["applied"] == ["filesystem", "github"]
    assert config.exists()


def test_route_apply_guards_its_backup_read(runner, sandbox, monkeypatch):
    """The backup snapshot re-reads the file after ``_load``; a failure there
    exits cleanly instead of escaping as a traceback (#1095)."""
    _seed_routable_project(runner)
    config = sandbox["home"] / "proxy.json"
    config.write_text("{}", encoding="utf-8")
    real_read_text = Path.read_text
    reads = []

    def fail_second_read(self, *args, **kwargs):
        if self == config.resolve():
            reads.append(self)
            if len(reads) == 2:
                raise PermissionError(13, "Permission denied", "/cnry-dir/cnry-file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_second_read)
    res = runner.invoke(project_group, ["route", "--config", str(config), "--apply"])

    assert len(reads) == 2
    assert res.exit_code == 1, res.output
    assert isinstance(res.exception, SystemExit)
    assert "PermissionError" in res.output
    assert "cnry" not in res.output
    assert config.read_text(encoding="utf-8") == "{}"  # nothing written


def test_route_json_on_invalid_json_emits_the_failure_document(runner, sandbox):
    """A config ``_load`` cannot parse still yields the ``--json`` document (#1099)."""
    _seed_routable_project(runner)
    config = sandbox["home"] / "proxy.json"
    config.write_text("{", encoding="utf-8")

    res = runner.invoke(project_group, ["route", "--config", str(config), "--json"])

    assert res.exit_code == 1, res.output
    payload = json.loads(res.stdout)
    assert payload["action"] == "route"
    assert payload["ok"] is False
    assert payload["error"] == "config_invalid"
    assert payload["path"] == str(config.resolve())
    assert "JSONDecodeError (line 1 column 2)" in payload["message"]


def test_route_json_apply_guards_its_backup_read(runner, sandbox, monkeypatch):
    """The backup read fails after ``_load`` succeeded; the document still comes out."""
    _seed_routable_project(runner)
    config = sandbox["home"] / "proxy.json"
    config.write_text("{}", encoding="utf-8")
    real_read_text = Path.read_text
    reads = []

    def fail_second_read(self, *args, **kwargs):
        if self == config.resolve():
            reads.append(self)
            if len(reads) == 2:
                raise PermissionError(13, "Permission denied", "/cnry-dir/cnry-file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_second_read)
    res = runner.invoke(project_group, ["route", "--config", str(config), "--apply", "--json"])

    assert len(reads) == 2
    assert res.exit_code == 1, res.output
    payload = json.loads(res.stdout)
    assert payload["action"] == "route"
    assert payload["ok"] is False
    assert payload["error"] == "config_unreadable"
    assert payload["message"] == f"Failed to read {config.resolve()}: PermissionError"
    assert "cnry" not in res.stdout
    assert config.read_text(encoding="utf-8") == "{}"  # nothing written


def test_route_apply_writes_valid_additive_config_and_is_idempotent(runner, sandbox):
    from memtomem_stm.proxy.config import ProxyConfig

    _seed_routable_project(runner)
    config = sandbox["home"] / "proxy.json"

    res = runner.invoke(project_group, ["route", "--config", str(config), "--apply", "--json"])

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["applied"] == ["filesystem", "github"]
    raw = json.loads(config.read_text(encoding="utf-8"))
    typed = ProxyConfig.model_validate(raw)
    assert set(typed.upstream_servers) == {"filesystem", "github"}
    fs = raw["upstream_servers"]["filesystem"]
    assert fs["env"] == {"ROUTE_SECRET": "keep-private"}
    assert fs["origin"]["source"]["kind"] == "mms-registry"
    assert fs["origin"]["original"]["env"] == {"ROUTE_SECRET": "keep-private"}

    again = runner.invoke(project_group, ["route", "--config", str(config), "--apply", "--json"])
    assert again.exit_code == 0, again.output
    again_payload = json.loads(again.output)
    assert again_payload["planned"] == []
    assert again_payload["unchanged"] == ["filesystem", "github"]
    assert again_payload["applied"] == []


@pytest.mark.parametrize("invalid_upstreams", [None, [], "servers"])
def test_route_rejects_non_object_upstream_map_cleanly(runner, sandbox, invalid_upstreams):
    _seed_routable_project(runner)
    config = sandbox["home"] / "proxy.json"
    config.write_text(json.dumps({"upstream_servers": invalid_upstreams}), encoding="utf-8")

    res = runner.invoke(project_group, ["route", "--config", str(config)])

    assert res.exit_code == 1
    assert "upstream_servers" in res.output
    assert "object" in res.output
    assert "AttributeError" not in res.output


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock is POSIX-only; lock contention is covered on Windows by shared tests",
)
def test_route_lock_timeout_preserves_json_contract(runner, sandbox, monkeypatch):
    from memtomem_stm.cli._write_lock import stm_config_lock_path

    _seed_routable_project(runner)
    config = sandbox["home"] / "proxy.json"
    monkeypatch.setattr(state, "WRITE_LOCK_TIMEOUT_SECONDS", 0.2)

    with state.write_lock(lock_path=stm_config_lock_path()):
        res = runner.invoke(
            project_group,
            ["route", "--config", str(config), "--apply", "--json"],
        )

    assert res.exit_code == 1
    payload = json.loads(res.stdout)
    assert payload["action"] == "route"
    assert payload["ok"] is False
    assert payload["error"] == "config_lock_timeout"
    assert "timed out" in payload["message"]
    assert "timed out" in res.stderr


def test_route_skips_name_and_prefix_conflicts_without_clobbering(runner, sandbox):
    _seed_routable_project(runner)
    config = sandbox["home"] / "proxy.json"
    original = {
        "enabled": True,
        "cache": {"tool_annotation_policy": "strict"},
        "upstream_servers": {
            "filesystem": {"command": "different", "prefix": "other"},
            "existing": {"command": "echo", "prefix": "gh"},
        },
    }
    config.write_text(json.dumps(original), encoding="utf-8")

    res = runner.invoke(project_group, ["route", "--config", str(config), "--apply", "--json"])

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["applied"] == []
    assert payload["skipped"] == [
        {"name": "filesystem", "reason": "name_conflict"},
        {"name": "github", "reason": "prefix_conflict", "detail": "owned by existing"},
    ]
    assert json.loads(config.read_text(encoding="utf-8")) == original


def test_route_treats_empty_env_as_equivalent_to_null(runner, sandbox):
    """A hand-edited ``"env": {}`` on an otherwise identical entry is the
    same route — report it unchanged, not as a name conflict (the route
    writer itself normalizes an empty env to ``null``)."""
    _seed_routable_project(runner)
    config = sandbox["home"] / "proxy.json"
    config.write_text(
        json.dumps(
            {
                "upstream_servers": {
                    "github": {
                        "command": "github-mcp",
                        "args": [],
                        "env": {},
                        "prefix": "gh",
                        "transport": "stdio",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    res = runner.invoke(project_group, ["route", "--config", str(config), "--json"])

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["unchanged"] == ["github"]
    assert payload["planned"] == ["filesystem"]
    assert payload["skipped"] == []


# ---------------------------------------------------------------------------
# Project resolution edge case — --project name with stale index
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Wire-in smoke — `mms project ...` is reachable through the top-level cli
# ---------------------------------------------------------------------------


def test_project_group_wired_into_top_level_cli(runner):
    """`mms project --help` lists the project subcommands.

    Pins the wiring in proxy.py — if the import or `add_command` line is
    accidentally removed, this test catches it before the §12 e2e
    acceptance test does (faster signal).
    """
    from memtomem_stm.cli.proxy import cli

    res = runner.invoke(cli, ["project", "--help"])
    assert res.exit_code == 0, res.output
    for name in ["init", "show", "list", "enable", "disable", "route"]:
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


class TestDisplayEscaping:
    """Project names come from ``--name`` (argv, so a lone surrogate arrives via
    POSIX ``surrogateescape``), from the marker file, and from the project
    index; MCP names come from argv and the registry. All of them are printed
    into prose and into a tab-separated listing. First escaping coverage for
    this surface (#760)."""

    HOSTILE = "ev\x1b[31m\ril"

    @staticmethod
    def _assert_clean(output: str) -> None:
        assert "\x1b" not in output
        assert "\r" not in output

    def test_show_escapes_project_name(self, runner, sandbox):
        runner.invoke(project_group, ["init", "--name", self.HOSTILE])
        res = runner.invoke(project_group, ["show"])

        assert res.exit_code == 0, res.output
        assert "\\u001B" in res.output and "\\u000D" in res.output
        self._assert_clean(res.output)

    def test_show_json_leg_keeps_the_name_raw(self, runner, sandbox):
        runner.invoke(project_group, ["init", "--name", self.HOSTILE])
        res = runner.invoke(project_group, ["show", "--json"])

        assert res.exit_code == 0, res.output
        assert json.loads(res.output)["name"] == self.HOSTILE

    def test_list_escapes_indexed_name(self, runner, sandbox):
        runner.invoke(project_group, ["init", "--name", self.HOSTILE])
        res = runner.invoke(project_group, ["list"])

        assert res.exit_code == 0, res.output
        self._assert_clean(res.output)

    def test_enable_and_disable_escape_the_project_name(self, runner, sandbox):
        _seed_registry("gh")
        runner.invoke(project_group, ["init", "--name", self.HOSTILE])

        enabled = runner.invoke(project_group, ["enable", "gh"])
        assert enabled.exit_code == 0, enabled.output
        self._assert_clean(enabled.output)

        disabled = runner.invoke(project_group, ["disable", "gh"])
        assert disabled.exit_code == 0, disabled.output
        self._assert_clean(disabled.output)

    def test_unknown_mcp_error_escapes_the_name_from_argv(self, runner, sandbox):
        """``mms project enable`` echoes back names the user typed, so this one
        is reachable without any config on disk carrying a hostile value."""
        _seed_registry("gh")
        runner.invoke(project_group, ["init"])
        res = runner.invoke(project_group, ["enable", self.HOSTILE])

        assert res.exit_code != 0
        assert "Unknown MCP name(s)" in res.output
        self._assert_clean(res.output)

    def test_project_not_found_error_escapes_the_name_from_argv(self, runner, sandbox):
        res = runner.invoke(project_group, ["show", self.HOSTILE])

        assert res.exit_code != 0
        assert "not found in" in res.output, res.output
        self._assert_clean(res.output)


class TestRemainingRawRendersEscape:
    """The three surfaces the #785 sweep left raw in this module (#786).

    ``last_seen`` is typed as a bare ``str``, so ``projects.toml`` decides its
    contents; TOML basic strings admit ``\\r`` and ``\\u001B`` escapes, which is
    the reachability that gives this a severity (a lone surrogate is *not*
    reachable here — ``tomllib`` refuses a non-scalar escape). The two error
    renders carry a HOME-derived path, where an undecodable byte does become a
    lone surrogate via ``surrogateescape``.
    """

    HOSTILE = "20\x1b[31m\r26-01-01T00:00:00Z"

    def test_list_escapes_last_seen(self, runner, sandbox):
        runner.invoke(project_group, ["init", "--name", "clean"])
        idx = state.load_projects_index()
        idx.projects[0].last_seen = self.HOSTILE
        state.save_projects_index(idx)

        res = runner.invoke(project_group, ["list"])

        assert res.exit_code == 0, res.output
        assert "\\u001B" in res.output and "\\u000D" in res.output
        assert "\x1b" not in res.output and "\r" not in res.output

    def test_list_last_seen_survives_a_toml_round_trip(self, runner, sandbox):
        """The escape is at the render, so the stored value stays verbatim.

        Deliberately NOT a render test, and the only one here that isn't: it
        stays green when the render above is reverted to raw, because what it
        pins is the opposite direction — the constraint carried over from #785
        that escaping a value which is also *written* would rewrite
        ``projects.toml`` on the next save. It doubles as the reachability
        proof that TOML really does deliver these characters rather than
        rejecting them at load.
        """
        runner.invoke(project_group, ["init", "--name", "clean"])
        idx = state.load_projects_index()
        idx.projects[0].last_seen = self.HOSTILE
        state.save_projects_index(idx)

        assert state.load_projects_index().projects[0].last_seen == self.HOSTILE

    def test_list_prune_write_lock_timeout_escapes_the_lock_path(
        self, runner, sandbox, monkeypatch
    ):
        """``list --prune`` takes the lock inline, not via ``with_write_lock``.

        Its timeout reaches ``surface_mms_state_errors`` directly, so this
        targets that renderer: routing through a ``with_write_lock`` command
        would pass even with it left raw, since ``_write_lock`` escapes on
        its own.
        """
        hostile_lock = Path("/tmp") / "lo\x1b[31m\rck" / ".mms.lock"

        def _boom(*args, **kwargs):
            raise state.WriteLockTimeout(hostile_lock, 5.0)

        monkeypatch.setattr(state, "write_lock", _boom)
        res = runner.invoke(project_group, ["list", "--prune"])

        assert res.exit_code != 0
        assert "\\u001B" in res.output and "\\u000D" in res.output
        assert "\x1b" not in res.output and "\r" not in res.output

    def test_config_error_escapes_the_rendered_path(self, runner, sandbox, monkeypatch):
        hostile = "/tmp/re\x1b[31m\rgistry.toml"

        def _boom(*args, **kwargs):
            raise state.MmsConfigError(f"unreadable: {hostile}")

        runner.invoke(project_group, ["init", "--name", "clean"])
        monkeypatch.setattr(state, "load_registry", _boom)
        res = runner.invoke(project_group, ["route"])

        assert res.exit_code != 0
        assert "\\u001B" in res.output and "\\u000D" in res.output
        assert "\x1b" not in res.output and "\r" not in res.output


class TestShowFallbackBranchesEscapePaths:
    """The two no-marker branches rendered ``cwd`` and the git root raw (#780).

    #771 covered the marker-backed ``show`` output but not these. A path is as
    unvalidated as argv: on POSIX a byte that is not valid UTF-8 decodes with
    ``surrogateescape``, so a directory name alone puts a lone surrogate here.
    """

    HOSTILE = "pr\x1b[31m\roj"

    def test_no_marker_no_git_branch_escapes_cwd(self):
        from pathlib import Path

        from memtomem_stm.cli.mms_project import _show_no_marker_no_git_text

        out = _show_no_marker_no_git_text(Path("/tmp") / self.HOSTILE)

        assert "\x1b" not in out and "\r" not in out
        assert "\\u001B" in out and "\\u000D" in out

    def test_git_no_marker_branch_escapes_root(self):
        from pathlib import Path

        from memtomem_stm.cli.mms_project import _show_git_no_marker_text

        out = _show_git_no_marker_text(Path("/tmp") / self.HOSTILE)

        assert "\x1b" not in out and "\r" not in out
        assert "\\u001B" in out and "\\u000D" in out


# ---------------------------------------------------------------------------
# route --json: every operational failure emits the failure document (#1138)
# ---------------------------------------------------------------------------


def _route_failure(res, error: str) -> dict:
    assert res.exit_code == 1, res.output
    payload = json.loads(res.stdout)
    assert (payload["action"], payload["ok"], payload["error"]) == ("route", False, error)
    assert "Error:" in res.stderr
    return payload


def test_route_json_without_a_marker_emits_project_not_initialized(runner, sandbox):
    # A repository root of its own keeps the reported root at cwd even below a checkout.
    (sandbox["cwd"] / ".git").mkdir()
    config = sandbox["home"] / "proxy.json"

    res = runner.invoke(project_group, ["route", "--config", str(config), "--json"])

    payload = _route_failure(res, "project_not_initialized")
    assert str(sandbox["cwd"]) in payload["message"]
    assert "mms project init" in payload["message"]


def test_route_json_unknown_named_project_emits_project_not_found(runner, sandbox):
    config = sandbox["home"] / "proxy.json"

    res = runner.invoke(
        project_group, ["route", "--project", "nope", "--config", str(config), "--json"]
    )

    payload = _route_failure(res, "project_not_found")
    assert payload["message"].startswith("Project 'nope' not found in ")


def test_route_json_ambiguous_named_project_emits_project_ambiguous(runner, sandbox):
    _seed_routable_project(runner)
    index = state.load_projects_index()
    twin = state.ProjectIndexEntry(
        name=index.projects[0].name,
        path=str(sandbox["home"] / "twin"),
        last_seen="2026-01-01T00:00:00Z",
    )
    state.save_projects_index(state.ProjectsIndex(projects=[*index.projects, twin]))
    config = sandbox["home"] / "proxy.json"

    res = runner.invoke(
        project_group,
        ["route", "--project", index.projects[0].name, "--config", str(config), "--json"],
    )

    payload = _route_failure(res, "project_ambiguous")
    assert "(2 matches)" in payload["message"]


def test_route_json_indexed_project_without_a_marker_emits_project_missing(runner, sandbox):
    _seed_routable_project(runner)
    shutil.rmtree(sandbox["cwd"] / ".mms")
    config = sandbox["home"] / "proxy.json"

    res = runner.invoke(
        project_group, ["route", "--project", "proj", "--config", str(config), "--json"]
    )

    payload = _route_failure(res, "project_missing")
    assert "mms project list --prune" in payload["message"]


def test_route_json_apply_on_an_invalid_result_emits_result_invalid(runner, sandbox):
    """A pre-existing broken sibling entry fails the whole-file validation; nothing is written."""
    _seed_routable_project(runner)
    config = sandbox["home"] / "proxy.json"
    before = json.dumps({"upstream_servers": {"broken": {"transport": "stdio"}}})
    config.write_text(before, encoding="utf-8")

    res = runner.invoke(project_group, ["route", "--config", str(config), "--apply", "--json"])

    payload = _route_failure(res, "result_invalid")
    assert payload["path"] == str(config)
    assert payload["message"].startswith("routing would produce an invalid proxy config (")
    assert payload["message"].endswith("; nothing written")
    assert config.read_text(encoding="utf-8") == before
    assert not list(sandbox["home"].glob("proxy.json.bak-*"))


def test_route_json_result_invalid_keeps_the_rejected_value_out_of_the_document(runner, sandbox):
    """The duplicate-prefix validator names the prefix in its ``msg``; stdout gets the summary."""
    _seed_routable_project(runner)
    config = sandbox["home"] / "proxy.json"
    canary = "sk-live-canary"
    config.write_text(
        json.dumps(
            {
                "upstream_servers": {
                    "one": {"command": "x", "prefix": canary},
                    "two": {"command": "y", "prefix": canary},
                }
            }
        ),
        encoding="utf-8",
    )

    res = runner.invoke(project_group, ["route", "--config", str(config), "--apply", "--json"])

    payload = _route_failure(res, "result_invalid")
    assert canary not in res.stdout
    assert payload["message"].startswith("routing would produce an invalid proxy config (")
    # Positive control: the detailed terminal line still names it, as before.
    assert canary in res.stderr


def test_route_failure_message_is_raw_in_json_and_escaped_on_the_terminal(runner, sandbox):
    """The #1166 split: the document carries the value as is, stderr display-escapes it."""
    config = sandbox["home"] / "proxy.json"
    hostile = "evil\x1b[31mname"

    as_json = runner.invoke(
        project_group, ["route", "--project", hostile, "--config", str(config), "--json"]
    )
    as_text = runner.invoke(project_group, ["route", "--project", hostile, "--config", str(config)])

    payload = _route_failure(as_json, "project_not_found")
    assert hostile in payload["message"]
    assert "\x1b" not in as_json.stderr
    assert as_text.exit_code == 1, as_text.output
    assert as_text.stdout == ""
    assert "\x1b" not in as_text.stderr
    assert "evil" in as_text.stderr
