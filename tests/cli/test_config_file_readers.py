"""The CLI's own config-file readers fail cleanly and render from known values (#1095).

``_load`` is the first read of ``status``, ``list``, ``health`` and the
writers. It used to print ``str(exc)`` for a parse or decode error and let an
``OSError`` escape as a traceback. ``mms config validate`` let a
``UnicodeDecodeError`` escape. #1087 / #1089 covered the loader behind
``status`` / ``health`` / ``doctor``; these are the readers it did not reach.
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli import proxy as proxy_mod
from memtomem_stm.cli.proxy import cli

CANARY = "cnryVal"
UNDECODABLE = b'{"a": "\xff' + CANARY.encode() + b'"}'


def _refuse_reads_of(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    real_read_text = Path.read_text

    def refuse(self, *args, **kwargs):
        if self == path.resolve():
            raise PermissionError(13, "Permission denied", "/cnry-dir/cnry-file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refuse)


def _assert_clean_exit(result) -> None:
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("command", ["status", "list"])
def test_invalid_json_renders_position_only(tmp_path, command):
    path = tmp_path / "proxy.json"
    path.write_text('{"a": "\\q"}')

    result = CliRunner().invoke(cli, [command, "--config", str(path)])

    _assert_clean_exit(result)
    assert f"Failed to parse {path.resolve()}: JSONDecodeError (line 1 column 8)" in result.output
    assert "escape" not in result.output
    assert "char" not in result.output
    assert "mms config validate" in result.output


@pytest.mark.parametrize("command", ["status", "list"])
def test_undecodable_file_renders_the_type_name(tmp_path, command):
    path = tmp_path / "proxy.json"
    path.write_bytes(UNDECODABLE)

    result = CliRunner().invoke(cli, [command, "--config", str(path)])

    _assert_clean_exit(result)
    assert f"Failed to parse {path.resolve()}: UnicodeDecodeError" in result.output
    assert "0xff" not in result.output
    assert "position" not in result.output


@pytest.mark.parametrize("command", ["status", "list"])
def test_unreadable_file_exits_cleanly_with_the_type_name(tmp_path, monkeypatch, command):
    path = tmp_path / "proxy.json"
    path.write_text("{}")
    _refuse_reads_of(monkeypatch, path)

    result = CliRunner().invoke(cli, [command, "--config", str(path)])

    _assert_clean_exit(result)
    assert f"Failed to read {path.resolve()}: PermissionError" in result.output
    assert "cnry" not in result.output


def test_tune_reads_through_the_guarded_loader(tmp_path, monkeypatch):
    """``tune`` used to read the file itself before ``_load`` and crash."""
    undecodable = tmp_path / "undecodable.json"
    undecodable.write_bytes(UNDECODABLE)
    result = CliRunner().invoke(cli, ["tune", "--config", str(undecodable)])
    _assert_clean_exit(result)
    assert "UnicodeDecodeError" in result.output
    assert "0xff" not in result.output

    unreadable = tmp_path / "unreadable.json"
    unreadable.write_text("{}")
    _refuse_reads_of(monkeypatch, unreadable)
    result = CliRunner().invoke(cli, ["tune", "--config", str(unreadable)])
    _assert_clean_exit(result)
    assert "Failed to read" in result.output
    assert "PermissionError" in result.output
    assert "cnry" not in result.output


def test_tune_guards_its_second_read(tmp_path, monkeypatch):
    """``tune`` reads the file twice; a failure on the later read exits cleanly too."""
    path = tmp_path / "proxy.json"
    path.write_text("{}")
    real_read_text = Path.read_text
    reads = []

    def fail_second_read(self, *args, **kwargs):
        if self == path.resolve():
            reads.append(self)
            if len(reads) == 2:
                raise PermissionError(13, "Permission denied", "/cnry-dir/cnry-file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_second_read)
    result = CliRunner().invoke(cli, ["tune", "--config", str(path)])

    assert len(reads) == 2
    _assert_clean_exit(result)
    assert "PermissionError" in result.output
    assert "cnry" not in result.output


def test_validate_hint_is_a_shell_safe_command(tmp_path):
    path = tmp_path / "dir with space" / "proxy.json"
    path.parent.mkdir()
    path.write_text("{")

    result = CliRunner().invoke(cli, ["status", "--config", str(path)])

    _assert_clean_exit(result)
    expected = proxy_mod._shell_join(["mms", "config", "validate", "--config", str(path.resolve())])
    assert f"Run `{expected}` for details." in result.output
    # The quoted form differs from the raw path: a bare interpolation would split it.
    assert expected != f"mms config validate --config {path.resolve()}"


def test_non_object_root_keeps_its_message(tmp_path):
    """Preservation control, green on main too: STM's own structural text."""
    path = tmp_path / "proxy.json"
    path.write_text("[1]")

    result = CliRunner().invoke(cli, ["status", "--config", str(path)])

    _assert_clean_exit(result)
    assert "top-level must be a JSON object, got list" in result.output


def test_config_validate_reports_an_undecodable_file(tmp_path):
    path = tmp_path / "proxy.json"
    path.write_bytes(UNDECODABLE)

    result = CliRunner().invoke(cli, ["config", "validate", "--config", str(path)])

    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    assert result.exit_code != 0
    # The local validator keeps full detail: the byte and its position.
    assert "not valid UTF-8" in result.output
    assert "0xff" in result.output


# ── --json mode: the failure document still reaches stdout (#1099) ────────

# Every ``--json`` command whose config read goes through ``_load`` /
# ``_read_config_text``, with the ``action`` its document carries. The
# arguments get each one past its own usage checks to the config read.
JSON_COMMANDS = [
    ("add", ["add", "fs", "--prefix", "fs", "--command", "x", "--json"]),
    ("add", ["add", "--from-clients", "--all", "--json"]),
    ("remove", ["remove", "fs", "--yes", "--json"]),
    ("prune", ["prune", "fs", "--yes", "--json"]),
    ("eject", ["eject", "fs", "--yes", "--json"]),
    ("status", ["status", "--json"]),
    ("list", ["list", "--json"]),
    ("health", ["health", "--json"]),
    ("gateway-status", ["gateway", "status", "--json"]),
    ("gateway-explain", ["gateway", "explain", "fs__read", "--json"]),
    ("tune", ["tune", "--json"]),
]
JSON_COMMAND_IDS = [
    "add",
    "add-from-clients",
    "remove",
    "prune",
    "eject",
    "status",
    "list",
    "health",
    "gateway-status",
    "gateway-explain",
    "tune",
]


def _failure_document(result, action: str, error: str, path: Path) -> dict:
    """The one stdout document of a failed ``--json`` run, checked field by field."""
    assert result.exit_code == 1, result.output
    document = json.loads(result.stdout)
    assert document["action"] == action
    assert document["ok"] is False
    assert document["error"] == error
    assert document["path"] == str(path.resolve())
    return document


@pytest.mark.parametrize(("action", "argv"), JSON_COMMANDS, ids=JSON_COMMAND_IDS)
def test_json_run_on_invalid_json_emits_the_failure_document(tmp_path, action, argv):
    path = tmp_path / "proxy.json"
    path.write_text('{"a": "\\q"}')

    result = CliRunner().invoke(cli, [*argv, "--config", str(path)])

    document = _failure_document(result, action, "config_invalid", path)
    assert document["message"] == (
        f"Failed to parse {path.resolve()}: JSONDecodeError (line 1 column 8)"
    )
    # The human lines stay on stderr, unchanged.
    assert "Failed to parse" in result.stderr
    assert "mms config validate" in result.stderr


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("[1]", "top-level must be a JSON object, got list"),
        ('{"upstream_servers": []}', "'upstream_servers' must be an object, got list"),
    ],
    ids=["non-object-root", "non-object-upstream-servers"],
)
@pytest.mark.parametrize("command", ["status", "remove"])
def test_json_run_on_a_wrong_shape_emits_config_invalid(tmp_path, content, expected, command):
    path = tmp_path / "proxy.json"
    path.write_text(content)
    argv = ["status", "--json"] if command == "status" else ["remove", "fs", "--yes", "--json"]

    result = CliRunner().invoke(cli, [*argv, "--config", str(path)])

    document = _failure_document(result, command, "config_invalid", path)
    assert document["message"] == f"{path.resolve()} {expected}"
    assert expected in result.stderr


@pytest.mark.parametrize("command", ["status", "remove"])
def test_json_run_on_an_undecodable_file_emits_config_invalid(tmp_path, command):
    path = tmp_path / "proxy.json"
    path.write_bytes(UNDECODABLE)
    argv = ["status", "--json"] if command == "status" else ["remove", "fs", "--yes", "--json"]

    result = CliRunner().invoke(cli, [*argv, "--config", str(path)])

    document = _failure_document(result, command, "config_invalid", path)
    assert document["message"] == f"Failed to parse {path.resolve()}: UnicodeDecodeError"
    assert "0xff" not in result.stdout
    assert CANARY not in result.stdout


@pytest.mark.parametrize("command", ["status", "remove"])
def test_json_run_on_an_unreadable_file_emits_config_unreadable(tmp_path, command):
    # A directory at the config path is unreadable on every platform; the
    # exception type differs (IsADirectoryError / PermissionError), the code
    # does not.
    path = tmp_path / "proxy.json"
    path.mkdir()
    argv = ["status", "--json"] if command == "status" else ["remove", "fs", "--yes", "--json"]

    result = CliRunner().invoke(cli, [*argv, "--config", str(path)])

    document = _failure_document(result, command, "config_unreadable", path)
    assert document["message"].startswith(f"Failed to read {path.resolve()}: ")


def test_json_tune_guards_its_second_read(tmp_path, monkeypatch):
    """``tune`` reads the file twice; a failure on the later read keeps the document."""
    path = tmp_path / "proxy.json"
    path.write_text("{}")
    real_read_text = Path.read_text
    reads = []

    def fail_second_read(self, *args, **kwargs):
        if self == path.resolve():
            reads.append(self)
            if len(reads) == 2:
                raise PermissionError(13, "Permission denied", "/cnry-dir/cnry-file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_second_read)
    result = CliRunner().invoke(cli, ["tune", "--json", "--config", str(path)])

    assert len(reads) == 2
    document = _failure_document(result, "tune", "config_unreadable", path)
    assert document["message"] == f"Failed to read {path.resolve()}: PermissionError"
    assert "cnry" not in result.stdout


def test_non_json_runs_keep_their_plain_exit(tmp_path):
    """Preservation control: without ``--json`` stdout stays empty, as before."""
    path = tmp_path / "proxy.json"
    path.write_text("{")

    result = CliRunner().invoke(cli, ["remove", "fs", "--yes", "--config", str(path)])

    _assert_clean_exit(result)
    assert result.stdout == ""
    assert "Failed to parse" in result.stderr
