"""An OS failure writing a ``~/.mms`` state file exits cleanly (#1181).

#1176 closed the proxy-config write path and the ``~/.mms`` lock; the state
savers themselves (``registry.toml``, ``projects.toml``, ``import_state.toml``
and a project's ``.mms/project.toml``) still let the ``OSError`` from the
atomic write escape. They now raise ``StateWriteFailed``, an ``MmsConfigError``
that ``surface_mms_state_errors`` (on every ``mms project`` / ``mms host`` /
``mms import`` command) renders. A command that writes two files says which one
already landed when the second fails.

The write is denied by patching ``state.atomic_write_text`` for one target file:
it stands in for a read-only directory or a full disk, and lets each test fail
one specific file of a two-file command.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from helpers import set_home
from memtomem_stm.cli.mms_host import host_group
from memtomem_stm.cli.mms_import import import_command
from memtomem_stm.cli.mms_project import project_group
from memtomem_stm.mms import state

TAIL = "(PermissionError); the file was not replaced by this write"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    return {"home": tmp_path, "cwd": proj}


def _deny(monkeypatch, *names: str) -> list[Path]:
    """Make the write of each named file fail; return the attempts."""
    real = state.atomic_write_text
    attempted: list[Path] = []

    def write(path, *args, **kwargs):
        if Path(path).name in names:
            attempted.append(Path(path))
            raise PermissionError(13, "Permission denied", "/cnry-dir/cnry-file")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(state, "atomic_write_text", write)
    return attempted


def _host_with_one_server(home: Path) -> None:
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"gh": {"command": "gh-mcp", "args": []}}}), encoding="utf-8"
    )


def _assert_clean_text_failure(result, *fragments: str) -> None:
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.stdout == "" or "Error:" not in result.stdout
    for fragment in fragments:
        assert fragment in result.stderr, result.stderr
    assert "cnry" not in result.output


# ---------------------------------------------------------------------------
# single-file writes
# ---------------------------------------------------------------------------


def test_project_init_reports_a_failed_marker_write(sandbox, monkeypatch):
    marker = (sandbox["cwd"] / state.PROJECT_MARKER_RELPATH).resolve()
    attempted = _deny(monkeypatch, "project.toml")

    result = CliRunner().invoke(project_group, ["init"])

    assert attempted
    _assert_clean_text_failure(result, f"Error: cannot write {marker} {TAIL}")
    assert "already written" not in result.stderr
    assert not state.projects_index_path().exists()


@pytest.mark.parametrize("json_output", [False, True], ids=["text", "json"])
def test_project_list_prune_reports_a_failed_index_write(sandbox, monkeypatch, json_output):
    runner = CliRunner()
    assert runner.invoke(project_group, ["init", "--name", "gone"]).exit_code == 0
    (sandbox["cwd"] / ".mms" / "project.toml").unlink()  # make the entry prunable
    index = state.projects_index_path()
    before = index.read_bytes()
    _deny(monkeypatch, "projects.toml")

    result = runner.invoke(project_group, ["list", "--prune", *(["--json"] if json_output else [])])

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    message = f"cannot write {index} {TAIL}"
    if json_output:
        document = json.loads(result.stdout)
        assert (document["ok"], document["error"]) == (False, "mms_state_write_failed")
        assert document["message"].endswith(message)
    else:
        assert f"Error: {message}" in result.stderr
    assert "cnry" not in result.output
    assert index.read_bytes() == before


@pytest.mark.parametrize("json_output", [False, True], ids=["text", "json"])
def test_host_sync_reports_a_failed_registry_write(sandbox, monkeypatch, json_output):
    _host_with_one_server(sandbox["home"])
    _deny(monkeypatch, "registry.toml")

    result = CliRunner().invoke(
        host_group, ["sync", "--apply", "--yes", *(["--json"] if json_output else [])]
    )

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    message = f"cannot write {state.registry_path()} {TAIL}"
    if json_output:
        document = json.loads(result.stdout)
        assert (document["action"], document["ok"], document["error"]) == (
            "sync",
            False,
            "mms_state_write_failed",
        )
        assert document["message"].endswith(message)
    else:
        assert f"Error: {message}" in result.stderr
    assert "already written" not in result.output
    assert not state.import_state_path().exists()


def test_import_reports_a_failed_registry_write(sandbox, monkeypatch):
    _host_with_one_server(sandbox["home"])
    _deny(monkeypatch, "registry.toml")

    result = CliRunner().invoke(import_command, ["--apply"])

    _assert_clean_text_failure(result, f"Error: cannot write {state.registry_path()} {TAIL}")
    assert not state.import_state_path().exists()


# ---------------------------------------------------------------------------
# two-file writes: the second fails after the first landed
# ---------------------------------------------------------------------------


def test_project_init_names_the_marker_when_the_index_write_fails(sandbox, monkeypatch):
    marker = (sandbox["cwd"] / state.PROJECT_MARKER_RELPATH).resolve()
    _deny(monkeypatch, "projects.toml")

    result = CliRunner().invoke(project_group, ["init"])

    _assert_clean_text_failure(
        result,
        f"Error: cannot write {state.projects_index_path()} {TAIL}; already written: {marker}",
    )
    assert marker.is_file()  # the claim is true: the marker did land


def test_import_names_the_registry_when_the_sidecar_write_fails(sandbox, monkeypatch):
    _host_with_one_server(sandbox["home"])
    _deny(monkeypatch, "import_state.toml")

    result = CliRunner().invoke(import_command, ["--apply"])

    _assert_clean_text_failure(
        result,
        f"Error: cannot write {state.import_state_path()} {TAIL}; "
        f"already written: {state.registry_path()}",
    )
    assert "gh" in state.load_registry().servers


@pytest.mark.parametrize("json_output", [False, True], ids=["text", "json"])
def test_host_sync_names_the_registry_when_the_sidecar_write_fails(
    sandbox, monkeypatch, json_output
):
    _host_with_one_server(sandbox["home"])
    _deny(monkeypatch, "import_state.toml")

    result = CliRunner().invoke(
        host_group, ["sync", "--apply", "--yes", *(["--json"] if json_output else [])]
    )

    assert result.exit_code == 1, result.output
    message = (
        f"cannot write {state.import_state_path()} {TAIL}; already written: {state.registry_path()}"
    )
    if json_output:
        assert json.loads(result.stdout)["message"].endswith(message)
    else:
        assert f"Error: {message}" in result.stderr
    assert "gh" in state.load_registry().servers


# ---------------------------------------------------------------------------
# #1161's own partial-completion messages keep their wording and error type
# ---------------------------------------------------------------------------


@pytest.fixture
def enabled_project(sandbox):
    runner = CliRunner()
    assert runner.invoke(project_group, ["init", "--name", "p"]).exit_code == 0
    state.save_registry(
        state.RegistryConfig(servers={"tool": state.RegistryServer(command="echo", prefix="tool")})
    )
    return runner


def test_project_enable_keeps_its_marker_failure_message(enabled_project, monkeypatch):
    _deny(monkeypatch, "project.toml")

    result = enabled_project.invoke(project_group, ["enable", "tool"])

    _assert_clean_text_failure(
        result, "Could not confirm saving project marker", ": PermissionError. The projects index"
    )
    assert "StateWriteFailed" not in result.output


def test_project_enable_keeps_its_index_failure_message(enabled_project, monkeypatch):
    _deny(monkeypatch, "projects.toml")

    result = enabled_project.invoke(project_group, ["enable", "tool"])

    _assert_clean_text_failure(
        result, "Project settings were saved to", "could not be confirmed: PermissionError."
    )
    assert "StateWriteFailed" not in result.output
