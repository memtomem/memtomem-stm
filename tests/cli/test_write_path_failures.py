"""An OS failure on the config write path exits cleanly (#1176, part 2).

Every ``stm_proxy.json`` mutator shares three steps that can raise an
``OSError`` after the command has decided to write: opening the write lock,
snapshotting the config to a backup, and the atomic replace itself. Each used
to escape as a traceback with an empty stdout; the decorator that already
renders the lock timeout now renders these too. The ``~/.mms`` domain has the
same two steps (its lock, its savers) behind ``surface_mms_state_errors``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from memtomem_stm.cli._write_lock import with_config_write_lock, with_write_lock
from memtomem_stm.cli.mms_host import host_group
from memtomem_stm.cli.mms_project import project_group
from memtomem_stm.cli.proxy import cli
from memtomem_stm.mms import state
from helpers import set_home

CONFIG = {"enabled": True, "upstream_servers": {"srv": {"command": "echo", "prefix": "sv"}}}
DENIED = PermissionError(13, "Permission denied", "/cnry-dir/cnry-file")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MMS_NO_TUI", "1")
    return {"home": tmp_path, "cwd": proj}


def _config(tmp_path: Path, name: str = "proxy.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    return path


def _fail_atomic_write(monkeypatch) -> None:
    import memtomem_stm.cli.proxy as proxy_mod

    def denied(*args, **kwargs):
        raise DENIED

    monkeypatch.setattr(proxy_mod, "atomic_write_text", denied)


def _fail_lock_open(monkeypatch) -> None:
    def denied(path):
        raise IsADirectoryError(21, "Is a directory", str(path))

    monkeypatch.setattr(state, "open_lock_fd", denied)


# ---------------------------------------------------------------------------
# config write (``_save``)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["add", "new", "--command", "echo", "--prefix", "nw"],
        ["remove", "srv", "--yes"],
        ["surfacing", "srv", "off"],
        ["gateway", "mode", "strict", "--apply"],
    ],
    ids=["add", "remove", "surfacing", "gateway-mode"],
)
def test_config_write_failure_exits_cleanly_in_text_mode(sandbox, monkeypatch, argv):
    config = _config(sandbox["home"])
    before = config.read_text(encoding="utf-8")
    _fail_atomic_write(monkeypatch)

    result = CliRunner().invoke(cli, [*argv, "--config", str(config)])

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert result.stdout == ""
    assert (
        f"Error: cannot write {config.resolve()} (PermissionError); the file was not replaced by this write"
        in result.stderr
    )
    assert "cnry" not in result.output
    assert config.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    ("argv", "action"),
    [
        (["add", "new", "--command", "echo", "--prefix", "nw", "--json"], "add"),
        (["remove", "srv", "--yes", "--json"], "remove"),
    ],
    ids=["add", "remove"],
)
def test_config_write_failure_emits_the_json_failure_document(sandbox, monkeypatch, argv, action):
    config = _config(sandbox["home"])
    _fail_atomic_write(monkeypatch)

    result = CliRunner().invoke(cli, [*argv, "--config", str(config)])

    assert result.exit_code == 1, result.output
    document = json.loads(result.stdout)
    assert document["action"] == action
    assert document["ok"] is False
    assert document["error"] == "config_write_failed"
    assert document["path"] == str(config.resolve())
    assert document["message"] == (
        f"cannot write {config.resolve()} (PermissionError); the file was not replaced by this write"
    )
    assert "cnry" not in result.output
    assert "Error:" in result.stderr


def test_config_write_failure_escapes_the_path_in_text_only(sandbox, monkeypatch):
    hostile = sandbox["home"] / "c‮fg"
    hostile.mkdir()
    config = _config(hostile)
    _fail_atomic_write(monkeypatch)

    text = CliRunner().invoke(cli, ["remove", "srv", "--yes", "--config", str(config)])
    as_json = CliRunner().invoke(cli, ["remove", "srv", "--yes", "--json", "--config", str(config)])

    assert text.exit_code == 1 and "‮" not in text.stderr and "\\u202E" in text.stderr
    document = json.loads(as_json.stdout)
    assert document["error"] == "config_write_failed"
    assert "‮" in document["message"] and "‮" in document["path"]


@pytest.mark.skipif(sys.platform == "win32", reason="directory modes are POSIX")
def test_a_read_only_config_directory_is_reported_end_to_end(sandbox):
    """Positive control for the monkeypatched tests: the real ``OSError`` from
    ``atomic_write_text`` takes the same path."""
    ro = sandbox["home"] / "ro"
    ro.mkdir()
    config = _config(ro)
    ro.chmod(0o500)
    try:
        result = CliRunner().invoke(
            cli, ["remove", "srv", "--yes", "--json", "--config", str(config)]
        )
    finally:
        ro.chmod(0o700)

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"] == "config_write_failed"
    assert json.loads(config.read_text(encoding="utf-8")) == CONFIG


# ---------------------------------------------------------------------------
# backup snapshot (``project route --apply``, ``tune --apply``)
# ---------------------------------------------------------------------------


def _seed_routable_project(runner: CliRunner) -> None:
    state.save_registry(
        state.RegistryConfig(servers={"github": state.RegistryServer(command="g", prefix="gh")})
    )
    assert runner.invoke(project_group, ["init"]).exit_code == 0
    assert runner.invoke(project_group, ["enable", "github"]).exit_code == 0


@pytest.mark.skipif(sys.platform == "win32", reason="directory modes are POSIX")
@pytest.mark.parametrize("json_output", [False, True], ids=["text", "json"])
def test_route_apply_reports_a_failed_backup_before_writing(sandbox, json_output):
    runner = CliRunner()
    _seed_routable_project(runner)
    ro = sandbox["home"] / "ro"
    ro.mkdir()
    config = ro / "proxy.json"
    config.write_text("{}", encoding="utf-8")
    ro.chmod(0o500)
    try:
        argv = ["route", "--config", str(config), "--apply", *(["--json"] if json_output else [])]
        result = runner.invoke(project_group, argv)
    finally:
        ro.chmod(0o700)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    message = (
        f"cannot create a backup of {config.resolve()} (PermissionError); "
        "the file was not replaced by this write"
    )
    if json_output:
        document = json.loads(result.stdout)
        assert (document["action"], document["ok"]) == ("route", False)
        assert document["error"] == "config_write_failed"
        assert document["message"] == message
        assert document["path"] == str(config.resolve())
    else:
        assert result.stdout == ""
        assert f"Error: {message}" in result.stderr
    assert config.read_text(encoding="utf-8") == "{}"


# ---------------------------------------------------------------------------
# opening the write lock (both domains)
# ---------------------------------------------------------------------------


def test_config_lock_open_failure_exits_cleanly(sandbox, monkeypatch):
    config = _config(sandbox["home"])
    _fail_lock_open(monkeypatch)
    lock = Path.home() / ".memtomem" / ".stm_proxy.lock"

    text = CliRunner().invoke(
        cli, ["add", "new", "--command", "echo", "--prefix", "nw", "--config", str(config)]
    )
    as_json = CliRunner().invoke(cli, ["remove", "srv", "--yes", "--json", "--config", str(config)])

    assert text.exit_code == 1 and isinstance(text.exception, SystemExit)
    assert f"Error: cannot open the write lock {lock} (IsADirectoryError)" in text.stderr
    document = json.loads(as_json.stdout)
    assert (document["action"], document["ok"], document["error"]) == (
        "remove",
        False,
        "config_lock_unavailable",
    )
    assert document["message"] == f"cannot open the write lock {lock} (IsADirectoryError)"
    assert json.loads(config.read_text(encoding="utf-8")) == CONFIG


def test_mms_lock_open_failure_exits_cleanly(sandbox, monkeypatch):
    runner = CliRunner()
    _seed_routable_project(runner)
    _fail_lock_open(monkeypatch)
    lock = state.write_lock_path()

    text = runner.invoke(project_group, ["disable", "github"])
    as_json = runner.invoke(host_group, ["sync", "--apply", "--yes", "--json"])

    assert text.exit_code == 1 and isinstance(text.exception, SystemExit)
    assert f"Error: cannot open the write lock {lock} (IsADirectoryError)" in text.stderr
    document = json.loads(as_json.stdout)
    assert (document["action"], document["ok"], document["error"]) == (
        "sync",
        False,
        "mms_lock_unavailable",
    )


# ---------------------------------------------------------------------------
# the decorators catch the lock's own failure, not the command's
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "decorator", [with_config_write_lock(), with_write_lock], ids=["config", "mms"]
)
def test_an_oserror_from_the_command_body_still_propagates(sandbox, decorator):
    """Negative control: only the lock-open failure is folded, so a command's own
    unrelated ``OSError`` keeps surfacing as before."""

    @click.command()
    @decorator
    def boom() -> None:
        raise FileNotFoundError(2, "No such file", "/elsewhere")

    result = CliRunner().invoke(boom, [])

    assert isinstance(result.exception, FileNotFoundError)


# ---------------------------------------------------------------------------
# the error handlers themselves must not fail (review r1)
# ---------------------------------------------------------------------------


def test_backup_cleanup_failure_does_not_mask_the_write_failure(sandbox, monkeypatch):
    """The slot is created, its write fails, and by then the directory no longer
    allows the unlink: the reported failure is still the write, not the cleanup."""
    from memtomem_stm.cli._write_lock import ConfigWriteError
    from memtomem_stm.cli.proxy import _backup_config_snapshot

    config = _config(sandbox["home"])
    real_fdopen = os.fdopen

    def failing_fdopen(fd, *args, **kwargs):
        os.close(fd)
        raise OSError(28, "No space left on device")

    def failing_unlink(self, missing_ok=False):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(os, "fdopen", failing_fdopen)
    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(ConfigWriteError) as info:
        _backup_config_snapshot(config.resolve(), "{}")

    assert isinstance(info.value.__cause__, OSError)
    assert info.value.__cause__.errno == 28
    assert "No space" not in str(info.value)
    monkeypatch.setattr(os, "fdopen", real_fdopen)


def test_save_names_the_path_without_touching_the_filesystem_in_the_handler(sandbox, monkeypatch):
    """``resolve()`` of a relative path reads the cwd; if that fails the error
    still comes out, naming the unresolved path."""
    from memtomem_stm.cli._write_lock import ConfigWriteError
    from memtomem_stm.cli.proxy import _save

    _fail_atomic_write(monkeypatch)
    real_resolve = Path.resolve

    def resolve_without_a_cwd(self, strict=False):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(Path, "resolve", resolve_without_a_cwd)
    try:
        with pytest.raises(ConfigWriteError) as info:
            _save(Path("relative.json"), {"enabled": True})
    finally:
        monkeypatch.setattr(Path, "resolve", real_resolve)

    assert info.value.path == Path("relative.json")
    assert "cannot write relative.json (PermissionError)" in info.value.raw_message


def test_init_json_reports_a_failed_config_write_in_its_own_document(sandbox, monkeypatch):
    """``init --json`` wraps the command in its own result document; a write
    failure keeps that schema but carries the shared code and the path."""
    _fail_atomic_write(monkeypatch)
    config = sandbox["home"] / "new.json"

    result = CliRunner().invoke(
        cli,
        ["init", "--demo", "--no-validate", "--client", "skip", "--json", "--config", str(config)],
    )

    assert result.exit_code == 1, result.output
    document = json.loads(result.stdout)
    assert document["action"] == "init"
    assert document["ok"] is False
    assert document["error"] == "config_write_failed"
    assert document["path"] == str(config.resolve())
    assert document["message"] == (
        f"cannot write {config.resolve()} (PermissionError); the file was not replaced by this write"
    )
    assert "cnry" not in result.output
    assert f"Error: cannot write {config.resolve()} (PermissionError)" in result.stderr
    assert not config.exists()


def test_init_json_survives_a_failing_post_failure_inspection(sandbox, monkeypatch):
    """After the write failed, the wrapper looks at the config to fill its
    document; a directory it can no longer traverse must not turn the clean
    failure back into a traceback (review r2)."""
    import memtomem_stm.cli.proxy as proxy_mod

    config = sandbox["home"] / "new.json"
    real_exists = Path.exists
    attempted: list[Path] = []

    def denied_write(path, *args, **kwargs):
        attempted.append(path)
        raise DENIED

    def denied_exists(self, *args, **kwargs):
        # The directory becomes untraversable at the write, not before: init
        # itself checks ``exists()`` on the way in, and that check is not the
        # subject here.
        if attempted and self == config.resolve():
            raise PermissionError(13, "Permission denied", str(self))
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(proxy_mod, "atomic_write_text", denied_write)
    monkeypatch.setattr(Path, "exists", denied_exists)

    result = CliRunner().invoke(
        cli,
        ["init", "--demo", "--no-validate", "--client", "skip", "--json", "--config", str(config)],
    )

    assert attempted, "the write was never attempted"
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    document = json.loads(result.stdout)
    assert document["error"] == "config_write_failed"
    assert document["servers"] == []
