"""Config-load failures render from known values on every reader (#1087, #1089).

The loader's warning lines and ``ConfigLoadResult`` (which reaches ``status``,
``list``, ``health``, ``doctor`` and ``stm_proxy_health``) share one renderer.
A rejected value, a path in an ``OSError``, the bytes in a
``UnicodeDecodeError`` and the character a JSON error quotes must not reach
either. ``mms config validate`` keeps the full detail on purpose.
"""

import json
import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import cli
from memtomem_stm.proxy.config import ProxyConfig, collect_proxy_env_overrides

CANARY = "cnryVal"


def _load(path: Path, overrides=None):
    return ProxyConfig.load_from_file_with_status(path, env_overrides=overrides)


def _messages(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def test_file_validation_error_logs_location_and_type_not_value(tmp_path, caplog):
    path = tmp_path / "proxy.json"
    path.write_text(json.dumps({"default_max_result_chars": CANARY}))

    with caplog.at_level(logging.WARNING):
        result = _load(path)

    logged = _messages(caplog)
    assert "Failed to parse proxy config" in logged
    assert "default_max_result_chars (int_parsing)" in logged
    assert CANARY not in logged
    assert "input_value" not in logged
    assert result.error == "1 validation error(s): default_max_result_chars (int_parsing)"


def test_env_only_validation_error_logs_location_and_type_not_value(tmp_path, caplog):
    overrides = collect_proxy_env_overrides(
        {"MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS": CANARY}
    )

    with caplog.at_level(logging.WARNING):
        result = _load(tmp_path / "absent.json", overrides)

    logged = _messages(caplog)
    assert "Env-only proxy config failed validation" in logged
    assert "default_max_result_chars (int_parsing)" in logged
    assert CANARY not in logged
    assert "input_value" not in logged
    assert result.env_error == "1 validation error(s): default_max_result_chars (int_parsing)"


def test_json_error_renders_position_only(tmp_path, caplog):
    path = tmp_path / "proxy.json"
    # An invalid escape: the pure-Python scanner quotes the character into
    # ``msg``, and ``str(exc)`` adds ``(char N)``.
    path.write_text('{"a": "\\q"}')

    with caplog.at_level(logging.WARNING):
        result = _load(path)

    assert result.error == "JSONDecodeError (line 1 column 8)"
    assert "JSONDecodeError (line 1 column 8)" in _messages(caplog)
    assert "escape" not in _messages(caplog)
    assert "char" not in _messages(caplog)


def test_non_object_root_keeps_its_stm_text(tmp_path, caplog):
    path = tmp_path / "proxy.json"
    path.write_text("[1]")

    with caplog.at_level(logging.WARNING):
        result = _load(path)

    assert result.error == "config root must be a JSON object, got list"
    assert "config root must be a JSON object, got list" in _messages(caplog)


def test_unreadable_file_renders_the_type_name(tmp_path, monkeypatch, caplog):
    path = tmp_path / "proxy.json"
    path.write_text("{}")
    real_read_text = Path.read_text

    def refuse(self, *args, **kwargs):
        if self == path.resolve():
            raise PermissionError(13, "Permission denied", "/cnry-dir/cnry-file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refuse)
    with caplog.at_level(logging.WARNING):
        result = _load(path)

    assert result.error == "PermissionError"
    assert "cnry" not in _messages(caplog)


def test_undecodable_file_renders_the_type_name(tmp_path, caplog):
    path = tmp_path / "proxy.json"
    path.write_bytes(b'{"a": "\xff' + CANARY.encode() + b'"}')

    with caplog.at_level(logging.WARNING):
        result = _load(path)

    assert result.error == "UnicodeDecodeError"
    assert "0xff" not in _messages(caplog)
    assert "position" not in _messages(caplog)


def _config_json_check(output: str) -> dict:
    checks = json.loads(output)["checks"]
    return next(check for check in checks if check["id"] == "config_json")


def test_doctor_json_error_renders_position_only(tmp_path):
    path = tmp_path / "proxy.json"
    path.write_text('{"a": "\\q"}')

    result = CliRunner().invoke(cli, ["doctor", "--config", str(path), "--json"])

    check = _config_json_check(result.output)
    assert check["status"] == "FAIL"
    assert check["detail"] == "invalid JSON at line 1 column 8"


def test_doctor_undecodable_file_fails_the_check_instead_of_crashing(tmp_path):
    path = tmp_path / "proxy.json"
    path.write_bytes(b'{"a": "\xff' + CANARY.encode() + b'"}')

    result = CliRunner().invoke(cli, ["doctor", "--config", str(path), "--json"])

    assert result.exception is None or isinstance(result.exception, SystemExit)
    check = _config_json_check(result.output)
    assert check["status"] == "FAIL"
    assert check["detail"] == "not valid UTF-8"


def test_doctor_unreadable_file_renders_the_type_name(tmp_path, monkeypatch):
    path = tmp_path / "proxy.json"
    path.write_text("{}")
    real_read_text = Path.read_text

    def refuse(self, *args, **kwargs):
        if self == path.resolve():
            raise PermissionError(13, "Permission denied", "/cnry-dir/cnry-file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", refuse)
    result = CliRunner().invoke(cli, ["doctor", "--config", str(path), "--json"])

    check = _config_json_check(result.output)
    assert check["status"] == "FAIL"
    assert check["detail"] == "cannot read file: PermissionError"
    assert "cnry" not in result.output


def test_config_validate_keeps_the_full_detail(tmp_path):
    """Compatibility control, green on main too: the local validator is where
    an operator reads what the other surfaces leave out."""
    path = tmp_path / "proxy.json"
    path.write_text('{"a": "\\q"}')

    result = CliRunner().invoke(cli, ["config", "validate", "--config", str(path)])

    assert "invalid JSON: Invalid \\escape" in result.output
    assert "char 7" in result.output
