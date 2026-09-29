"""The CLI's own config-file readers fail cleanly and render from known values (#1095).

``_load`` is the first read of ``status``, ``list``, ``health`` and the
writers. It used to print ``str(exc)`` for a parse or decode error and let an
``OSError`` escape as a traceback. ``mms config validate`` let a
``UnicodeDecodeError`` escape. #1087 / #1089 covered the loader behind
``status`` / ``health`` / ``doctor``; these are the readers it did not reach.
"""

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
