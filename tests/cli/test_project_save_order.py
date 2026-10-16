"""Project toggles preflight the index and report partial saves (#1145)."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from helpers import set_home
from memtomem_stm.cli.mms_project import project_group
from memtomem_stm.mms import state


@pytest.fixture
def project(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    runner = CliRunner()
    assert runner.invoke(project_group, ["init", "--name", "p"]).exit_code == 0
    state.save_registry(
        state.RegistryConfig(
            servers={
                "tool": state.RegistryServer(command="echo", prefix="tool"),
            }
        )
    )
    return runner, root / state.PROJECT_MARKER_RELPATH


def prepare(project, command, named):
    runner, marker = project
    if command == "disable":
        assert runner.invoke(project_group, ["enable", "tool"]).exit_code == 0
    return [command, "tool", *(["--project", "p"] if named else [])]


@pytest.mark.parametrize("command", ["enable", "disable"])
@pytest.mark.parametrize("named", [False, True], ids=["cwd", "named"])
@pytest.mark.parametrize("failure", ["toml", "schema", "read", "directory", "utf8"])
def test_index_preflight_preserves_marker(project, monkeypatch, command, named, failure):
    runner, marker = project
    argv = prepare(project, command, named)
    before = marker.read_bytes()
    index = state.projects_index_path()
    if failure == "toml":
        index.write_text("broken = [", encoding="utf-8")
    elif failure == "utf8":
        index.write_bytes(b"\xff\xfe")
    elif failure == "schema":
        index.write_text("schema_version = 999\n", encoding="utf-8")
    elif failure == "directory":
        index.unlink()
        index.mkdir()
    else:
        original = Path.open

        def unreadable(path, *args, **kwargs):
            if path == index:
                raise PermissionError("secret-error-value\x1b[31m")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", unreadable)
    res = runner.invoke(project_group, argv)
    assert res.exit_code == 1
    assert "Error:" in res.stderr
    assert marker.read_bytes() == before
    assert "No project settings were written." in res.stderr
    assert "secret-error-value" not in res.output
    assert "\x1b" not in res.output
    assert "Enabled in" not in res.output and "Disabled in" not in res.output


@pytest.mark.parametrize("command", ["enable", "disable"])
@pytest.mark.parametrize("named", [False, True], ids=["cwd", "named"])
@pytest.mark.parametrize(
    ("after_replace", "encoding_error"),
    [(False, False), (True, False), (False, True)],
    ids=["before-write", "after-write", "encoding-error"],
)
def test_partial_index_save_and_idempotent_recovery(
    project, monkeypatch, command, named, after_replace, encoding_error
):
    runner, marker = project
    argv = prepare(project, command, named)
    original = state.save_projects_index
    before = state.projects_index_path().read_bytes()

    def failed(index, path=None):
        if encoding_error:
            # Produce a real encoder failure portably, without a non-UTF-8 filesystem.
            "secret-error-value\x1b[31m\udcff".encode("utf-8")
        if after_replace:
            original(index, path)
        raise OSError("secret-error-value\x1b[31m")

    with monkeypatch.context() as patch:
        patch.setattr(state, "save_projects_index", failed)
        res = runner.invoke(project_group, argv)
    assert res.exit_code == 1
    expected = ["tool"] if command == "enable" else []
    assert state.load_project_config(marker).mcp.enabled == expected
    assert "Project settings were saved" in res.stderr
    error_type = "UnicodeEncodeError" if encoding_error else "OSError"
    assert f"could not be confirmed: {error_type}" in res.stderr
    assert "rerun the same command" in res.stderr
    assert "not been rolled back" in res.stderr
    assert "secret-error-value" not in res.output and "\x1b" not in res.output
    assert "Enabled in" not in res.output and "Disabled in" not in res.output
    if not after_replace:
        assert state.projects_index_path().read_bytes() == before
    # The no-op path must refresh the index too, rather than returning early.
    saves = []

    def recording(index, path=None):
        saves.append(index)
        original(index, path)

    monkeypatch.setattr(state, "save_projects_index", recording)
    retry = runner.invoke(project_group, argv)
    assert retry.exit_code == 0, retry.output
    assert len(saves) == 1
    assert state.load_project_config(marker).mcp.enabled == expected
    entries = state.load_projects_index().projects
    assert len(entries) == 1 and entries[0].name == "p"
    assert entries[0].path == str(marker.parent.parent.resolve())


@pytest.mark.parametrize("command", ["enable", "disable"])
@pytest.mark.parametrize("after_replace", [False, True], ids=["before-write", "after-write"])
def test_marker_save_failure_does_not_write_index(project, monkeypatch, command, after_replace):
    runner, marker = project
    argv = prepare(project, command, False)
    before_marker = marker.read_bytes()
    before_index = state.projects_index_path().read_bytes()
    original = state.save_project_config

    def failed(config, path):
        if after_replace:
            original(config, path)
        raise PermissionError("secret-error-value")

    def forbidden(*args, **kwargs):
        pytest.fail("index written after marker save failure")

    monkeypatch.setattr(state, "save_project_config", failed)
    monkeypatch.setattr(state, "save_projects_index", forbidden)
    res = runner.invoke(project_group, argv)
    assert res.exit_code == 1
    assert "Could not confirm saving project marker" in res.stderr
    assert "projects index was not updated" in res.stderr
    assert "secret-error-value" not in res.output
    assert state.projects_index_path().read_bytes() == before_index
    if not after_replace:
        assert marker.read_bytes() == before_marker


@pytest.mark.parametrize("command", ["enable", "disable"])
def test_missing_index_is_created_and_disable_needs_no_registry(project, command):
    runner, marker = project
    argv = prepare(project, command, False)
    state.projects_index_path().unlink()
    if command == "disable":
        state.registry_path().unlink()
    res = runner.invoke(project_group, argv)
    assert res.exit_code == 0, res.output
    assert state.load_project_config(marker).mcp.enabled == (
        ["tool"] if command == "enable" else []
    )
    entries = state.load_projects_index().projects
    assert len(entries) == 1 and entries[0].name == "p"
