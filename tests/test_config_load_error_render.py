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


# A config key carries whatever the author typed. In a log line, a newline
# forges a second line and U+202E reorders the rest of this one (#1147).
HOSTILE_KEY = "bad\nSuccess‮"
ESCAPED_KEY = "bad\\u000ASuccess\\u202E"


def test_file_validation_warning_escapes_the_key_but_the_result_keeps_it(tmp_path, caplog):
    path = tmp_path / "proxy.json"
    path.write_text(json.dumps({"upstream_servers": {HOSTILE_KEY: "not-an-object"}}))

    with caplog.at_level(logging.WARNING):
        result = _load(path)

    logged = _messages(caplog)
    assert f"upstream_servers.{ESCAPED_KEY} (model_type)" in logged
    assert "\nSuccess" not in logged and "‮" not in logged
    # The result is not terminal text: --json documents carry it raw.
    assert result.error == f"1 validation error(s): upstream_servers.{HOSTILE_KEY} (model_type)"


def test_env_only_validation_warning_escapes_the_key(tmp_path, caplog):
    overrides = collect_proxy_env_overrides(
        {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS": json.dumps({HOSTILE_KEY: "x"})}
    )

    with caplog.at_level(logging.WARNING):
        result = _load(tmp_path / "absent.json", overrides)

    logged = _messages(caplog)
    assert "Env-only proxy config failed validation" in logged
    assert f"upstream_servers.{ESCAPED_KEY} (model_type)" in logged
    assert "\nSuccess" not in logged and "‮" not in logged
    assert HOSTILE_KEY in result.env_error


def test_unknown_key_warning_escapes_the_key(tmp_path, caplog):
    path = tmp_path / "proxy.json"
    path.write_text(json.dumps({"upstream_servers": {}, HOSTILE_KEY: 1}))

    with caplog.at_level(logging.WARNING):
        result = _load(path)

    logged = _messages(caplog)
    assert f"unknown key(s) (ignored — possible typo): {ESCAPED_KEY}" in logged
    assert "\nSuccess" not in logged and "‮" not in logged
    assert result.unknown_keys == (HOSTILE_KEY,)


def test_implicated_env_variable_names_are_escaped(tmp_path, caplog):
    # The server-name segment of the variable name is the author's choice too.
    path = tmp_path / "proxy.json"
    path.write_text(json.dumps({"upstream_servers": {}}))
    overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__BAD‮X": "5"})

    with caplog.at_level(logging.WARNING):
        _load(path, overrides)

    logged = _messages(caplog)
    assert (
        "(env override(s) implicated: MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__BAD\\u202EX)" in logged
    )
    assert "‮" not in logged


# The env-var hint names variables, and an upstream-name segment of a variable
# name is the author's choice too.
HOSTILE_ENV = "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__BAD‮X"
ESCAPED_ENV = "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__BAD\\u202EX"


@pytest.mark.parametrize("argv", [["gateway", "status"], ["gateway", "explain", "x"]])
def test_gateway_invalid_config_escapes_the_key(tmp_path, argv):
    config = tmp_path / "proxy.json"
    config.write_text(json.dumps({"upstream_servers": {HOSTILE_KEY: "not-an-object"}}))

    result = CliRunner().invoke(cli, [*argv, "--config", str(config)])

    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1
    assert f"invalid proxy config: upstream_servers.{ESCAPED_KEY} (model_type)" in result.output
    assert "\nSuccess" not in result.output and "‮" not in result.output


def test_surfacing_bootstrap_text_escapes_the_error_but_the_status_keeps_it():
    from memtomem_stm.cli.proxy import _format_surfacing_bootstrap

    status = {"error": f"invalid configuration: proxy.upstream_servers.{HOSTILE_KEY} (model_type)"}

    rendered = "\n".join(_format_surfacing_bootstrap(status))

    assert f"proxy.upstream_servers.{ESCAPED_KEY} (model_type)" in rendered
    assert "\nSuccess" not in rendered and "‮" not in rendered
    assert HOSTILE_KEY in status["error"]  # the --json document renders this dict


def test_hook_install_policy_error_escapes_the_implicated_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv(HOSTILE_ENV, "5")

    result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude"])

    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1
    assert "prevented resolving the hook runtime policy" in result.output
    assert ESCAPED_ENV in result.output
    assert "‮" not in result.output


def test_registration_policy_error_escapes_the_implicated_variable(tmp_path, monkeypatch):
    import click

    from memtomem_stm.cli.proxy import _registration_command

    config = tmp_path / "proxy.json"
    config.write_text(json.dumps({"upstream_servers": {}}))
    monkeypatch.setenv(HOSTILE_ENV, "5")

    with pytest.raises(click.ClickException) as exc_info:
        _registration_command(config)

    message = exc_info.value.message
    assert "prevented resolving the registration's runtime policy" in message
    assert ESCAPED_ENV in message
    assert "‮" not in message


def test_surfacing_bootstrap_debug_log_escapes_but_the_status_keeps_raw(
    tmp_path, monkeypatch, caplog
):
    from memtomem_stm.cli import proxy as proxy_mod

    config = tmp_path / "proxy.json"
    config.write_text(json.dumps({"upstream_servers": {}}))
    monkeypatch.setenv(HOSTILE_ENV, "5")

    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.cli.proxy"):
        status = proxy_mod._surfacing_bootstrap_status(1.0, config_path=str(config))

    logged = _messages(caplog)
    assert "Surfacing bootstrap status inspection failed" in logged
    assert ESCAPED_ENV in logged
    assert "‮" not in logged
    assert HOSTILE_ENV in status["error"]  # the --json document renders this dict


def test_registration_json_message_mirrors_the_escaped_terminal_line(tmp_path, monkeypatch):
    """The setup commands' --json ``message`` is the last stderr line, so it
    carries the terminal escaping; the line's existing ``_disp(str(exc))``
    fallback already reached it the same way."""
    config = tmp_path / "proxy.json"
    config.write_text(json.dumps({"upstream_servers": {}}))
    monkeypatch.setenv(HOSTILE_ENV, "5")

    result = CliRunner().invoke(
        cli, ["register", "--config", str(config), "--mcp", "json", "--json"]
    )

    document = json.loads(result.stdout)
    assert document["error"] == "setup_failed"
    assert ESCAPED_ENV in document["message"]
    assert "‮" not in document["message"]


# #1166: every loader log line names the file, and a path can carry a newline or
# a bidi control. POSIX only: Windows rejects these characters in file names.
_HOSTILE_DIR = "bad\nSuccess‮"
_ESCAPED_DIR = "bad\\u000ASuccess\\u202E"

_posix_only = pytest.mark.skipif(
    __import__("sys").platform == "win32", reason="newline in a file name"
)


def _assert_path_escaped(caplog: pytest.LogCaptureFixture, expected_lines: int) -> None:
    messages = [r.getMessage() for r in caplog.records if "proxy config" in r.getMessage().lower()]
    assert len(messages) == expected_lines, messages
    for message in messages:
        assert _ESCAPED_DIR in message
        assert "\nSuccess" not in message and "‮" not in message


@_posix_only
def test_loader_warnings_escape_the_config_path(tmp_path, caplog):
    path = tmp_path / _HOSTILE_DIR / "proxy.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"bogus_key": 1, "upstream_servers": {"gh": {"command": "gh", "prefix": "gh"}}})
    )
    path.chmod(0o644)

    with caplog.at_level(logging.WARNING):
        result = _load(path)

    assert result.config is not None
    # permissive mode, unknown key, missing tool_annotation_policy, inert upstreams
    _assert_path_escaped(caplog, 4)


@_posix_only
def test_parse_failure_warning_escapes_the_config_path(tmp_path, caplog):
    path = tmp_path / _HOSTILE_DIR / "proxy.json"
    path.parent.mkdir()
    path.write_text("{not json")
    path.chmod(0o600)

    with caplog.at_level(logging.WARNING):
        result = _load(path)

    assert result.error is not None
    _assert_path_escaped(caplog, 1)


def test_missing_file_debug_line_escapes_the_config_path(tmp_path, caplog):
    # The path need not exist, so this one runs on Windows too.
    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.proxy.config"):
        ProxyConfig.load_from_file_with_status(
            tmp_path / _HOSTILE_DIR / "proxy.json", missing_ok=True
        )

    _assert_path_escaped(caplog, 1)
