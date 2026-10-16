"""``~/.mms`` state errors exit cleanly from every ``mms`` subcommand.

A hand-edited ``registry.toml`` / ``projects.toml`` / ``import_state.toml``
used to escape as a traceback. ``surface_mms_state_errors`` turns it into
``Error: <reason>`` (or the ``--json`` failure document); the inventory test
keeps a new subcommand from skipping it.
"""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from memtomem_stm.cli._mms_errors import MARKER
from memtomem_stm.cli.mms_host import host_group
from memtomem_stm.cli.mms_import import import_command
from memtomem_stm.cli.mms_project import project_group
from memtomem_stm.mms import state
from helpers import set_home

SECRET = "sk-live-mms-canary"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return {"home": tmp_path, "cwd": proj}


def _corrupt_registry() -> None:
    """A registry whose ``env`` value is a list: valid TOML, invalid schema,
    and the value is the kind of thing pydantic would quote back."""
    path = state.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'schema_version = 1\n[servers.gh]\ncommand = "gh"\nprefix = "gh"\n'
        f'env = {{ TOKEN = ["{SECRET}"] }}\n',
        encoding="utf-8",
    )


def _corrupt_projects_index() -> None:
    path = state.projects_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is = not [valid toml\n", encoding="utf-8")


def _commands() -> list[tuple[str, click.Command]]:
    out: list[tuple[str, click.Command]] = [("import", import_command)]
    for group in (host_group, project_group):
        assert isinstance(group, click.Group)
        out += [(f"{group.name} {n}", c) for n, c in sorted(group.commands.items())]
    return out


@pytest.mark.parametrize(("name", "command"), _commands(), ids=lambda v: str(v))
def test_every_mms_command_surfaces_state_errors(name, command):
    assert getattr(command.callback, MARKER, False), (
        f"`mms {name}` reads ~/.mms state; decorate it with @surface_mms_state_errors"
    )


@pytest.mark.parametrize(
    "argv",
    [["status"], ["scan"], ["sync", "--plan"]],
    ids=["status", "scan", "sync"],
)
def test_host_command_reports_a_corrupt_registry_without_its_values(runner, sandbox, argv):
    _corrupt_registry()

    res = runner.invoke(host_group, argv)

    assert res.exit_code == 1
    assert not isinstance(res.exception, state.MmsConfigError)
    assert "Error:" in res.stderr
    assert "schema validation: servers.gh.env.TOKEN (string_type)" in res.stderr
    assert SECRET not in res.output


def test_host_status_json_emits_the_failure_document(runner, sandbox):
    _corrupt_registry()

    res = runner.invoke(host_group, ["status", "--json"])

    assert res.exit_code == 1
    doc = json.loads(res.stdout)
    assert doc["action"] == "status"
    assert doc["ok"] is False
    assert doc["error"] == "mms_state_invalid"
    assert doc["message"].endswith("schema validation: servers.gh.env.TOKEN (string_type)")
    assert SECRET not in res.output


def test_project_route_json_emits_the_failure_document(runner, sandbox):
    assert runner.invoke(project_group, ["init", "--name", "p"]).exit_code == 0
    _corrupt_registry()

    res = runner.invoke(project_group, ["route", "--json"])

    assert res.exit_code == 1
    doc = json.loads(res.stdout)
    assert (doc["action"], doc["ok"], doc["error"]) == ("route", False, "mms_state_invalid")


def test_project_list_json_reports_a_lock_timeout_as_a_document(runner, sandbox, monkeypatch):
    def _held(*args, **kwargs):
        raise state.WriteLockTimeout(state.mms_home() / ".lock", 5.0)

    monkeypatch.setattr(state, "write_lock", _held)

    res = runner.invoke(project_group, ["list", "--prune", "--json"])

    assert res.exit_code == 1
    assert json.loads(res.stdout)["error"] == "mms_lock_timeout"


def test_project_init_leaves_no_marker_when_the_index_is_unreadable(runner, sandbox):
    """The index is read before the marker is written, so a failed init can
    be rerun without ``--force`` once the index is fixed."""
    _corrupt_projects_index()

    res = runner.invoke(project_group, ["init", "--name", "p"])

    assert res.exit_code == 1
    assert "TOML parse error" in res.stderr
    assert not (sandbox["cwd"] / state.PROJECT_MARKER_RELPATH).exists()


@pytest.mark.parametrize("existing_marker", [False, True], ids=["new", "force"])
def test_project_init_directory_index_preserves_marker_and_recovers(
    runner, sandbox, existing_marker
):
    marker = sandbox["cwd"] / state.PROJECT_MARKER_RELPATH
    before = None
    if existing_marker:
        state.save_project_config(
            state.ProjectConfig(
                project=state.ProjectMeta(name="original"),
                mcp=state.ProjectMcp(enabled=["keep-me"]),
            ),
            marker,
        )
        before = marker.read_bytes()
    index = state.projects_index_path()
    index.mkdir(parents=True)
    sentinel = index / "keep.txt"
    sentinel.write_bytes(b"preserve index directory contents")
    argv = ["init", "--name", "replacement", *(["--force"] if existing_marker else [])]

    for _ in range(2):
        result = runner.invoke(project_group, argv)
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert result.exception.code == 1
        assert "Error:" in result.stderr and "regular file" in result.stderr
        assert "Traceback" not in result.output
        assert "Created " not in result.output
        if existing_marker:
            assert marker.read_bytes() == before
        else:
            assert not marker.exists()
        assert sentinel.read_bytes() == b"preserve index directory contents"

    sentinel.unlink()
    index.rmdir()
    result = runner.invoke(project_group, argv)
    assert result.exit_code == 0, result.output
    assert state.load_project_config(marker).project.name == "replacement"
    assert [entry.name for entry in state.load_projects_index().projects] == ["replacement"]


@pytest.mark.parametrize(
    "argv",
    [
        ["list"],
        ["list", "--prune"],
        ["show", "p"],
        ["route", "--project", "p"],
        ["route", "--project", "p", "--apply"],
    ],
)
@pytest.mark.parametrize("json_output", [False, True])
def test_project_index_directory_errors_preserve_files(runner, sandbox, argv, json_output):
    assert runner.invoke(project_group, ["init", "--name", "p"]).exit_code == 0
    marker = sandbox["cwd"] / state.PROJECT_MARKER_RELPATH
    before = marker.read_bytes()
    index = state.projects_index_path()
    index.unlink()
    index.mkdir()
    sentinel = index / "keep.txt"
    sentinel.write_bytes(b"keep")
    proxy = sandbox["home"] / "proxy.json"
    proxy.write_bytes(b'{"upstream_servers": {}}\n')
    args = [*argv]
    if argv[0] == "route":
        args.extend(["--config", str(proxy)])
    if json_output:
        args.append("--json")

    result = runner.invoke(project_group, args)

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.exception.code == 1
    assert "regular file" in result.stderr
    assert "Traceback" not in result.output
    assert marker.read_bytes() == before
    assert sentinel.read_bytes() == b"keep"
    assert list(index.iterdir()) == [sentinel]
    assert proxy.read_bytes() == b'{"upstream_servers": {}}\n'
    if json_output:
        payload = json.loads(result.stdout)
        assert (payload["action"], payload["ok"], payload["error"]) == (
            argv[0],
            False,
            "mms_state_invalid",
        )


def test_import_reports_a_corrupt_registry_cleanly(runner, sandbox):
    (sandbox["home"] / ".claude.json").write_text(
        json.dumps({"mcpServers": {"fs": {"command": "npx", "args": ["-y", "@fs"]}}}),
        encoding="utf-8",
    )
    _corrupt_registry()

    res = runner.invoke(import_command, ["--from", "claude-code"])

    assert res.exit_code == 1
    assert not isinstance(res.exception, state.MmsConfigError)
    assert SECRET not in res.output


def test_host_sync_json_reports_a_lock_timeout_as_a_document(runner, sandbox, monkeypatch):
    """``with_write_lock`` takes the lock before the callback runs, so its
    timeout never reaches the callback's decorator; it must render the same
    document itself."""

    def _held(*args, **kwargs):
        raise state.WriteLockTimeout(state.mms_home() / ".lock", 5.0)

    monkeypatch.setattr(state, "write_lock", _held)

    res = runner.invoke(host_group, ["sync", "--apply", "--yes", "--json"])

    assert res.exit_code == 1
    doc = json.loads(res.stdout)
    assert (doc["action"], doc["ok"], doc["error"]) == ("sync", False, "mms_lock_timeout")
    assert "timed out" in res.stderr
