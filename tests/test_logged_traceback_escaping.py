"""An attached traceback cannot carry a display-hostile character to the log (#1173).

#1166 escaped the env-hint log lines; the traceback both sites attach still
rendered the failing validation location raw, and part of that location is the
operator's own variable name. Every handler STM installs now formats through
``DisplayEscapingFormatter``. These tests check the FORMATTED output, not
``record.getMessage()``: the traceback is not part of the message.

Since #1086 those handlers also withhold every exception message by default, so
the location never reaches them; the escaping is asserted on a formatter that
keeps the text (``withhold_exception_text=False``, what
``STMConfig.log_exception_text`` selects), and the default paths assert the
location is gone while the env hint line stays escaped.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from memtomem_stm.config import STMConfig, log_stm_config_failure
from memtomem_stm.logging_setup import (
    FILE_FORMAT,
    HOOK_STDERR_FORMAT,
    STDERR_FORMAT,
    DisplayEscapingFormatter,
    configure_hook_logging,
    configure_server_logging,
    configure_stderr_logging,
    open_private_log_handler,
)

# Upper case throughout: Windows upper-cases environment variable names. The
# name carries a newline and a right-to-left override; the traceback's
# location renders the key as ``upstream_servers.gh\nsuccess‮``.
HOSTILE_VAR = "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH\nSUCCESS‮__COMMAND"
RLO = "‮"
ESCAPED_RLO = "\\u202E"
ESCAPED_HINT = "GH\\u000ASUCCESS\\u202E__COMMAND"
LOCATION_TEXT = "success"


@pytest.fixture
def _restore_root_logging():
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    for h in root.handlers[:]:
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


@pytest.fixture
def hostile_env(tmp_path, monkeypatch) -> ValidationError:
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setenv(HOSTILE_VAR, "gh")
    with pytest.raises(ValidationError) as caught:
        STMConfig()
    return caught.value


def _capture(fmt: str, *, withhold: bool = True) -> tuple[logging.Logger, io.StringIO]:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(DisplayEscapingFormatter(fmt, withhold_exception_text=withhold))
    log = logging.getLogger(f"test.traceback_escaping.{id(buf)}")
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    return log, buf


# ---------------------------------------------------------------------------
# The formatter
# ---------------------------------------------------------------------------


def test_formatter_escapes_the_exception_text_line_by_line():
    log, buf = _capture("%(levelname)s %(message)s", withhold=False)
    try:
        raise ValueError(f"loc{RLO}ation\x1b[31m")
    except ValueError:
        log.warning("failed", exc_info=True)
    out = buf.getvalue()

    assert RLO not in out and "\x1b" not in out
    assert ESCAPED_RLO in out and "\\u001B" in out
    # Line structure is kept: the stack still reads as a stack.
    assert out.startswith("WARNING failed\nTraceback (most recent call last):\n")
    assert out.rstrip("\n").endswith(f"ValueError: loc{ESCAPED_RLO}ation\\u001B[31m")


def test_default_formatter_withholds_the_exception_text():
    log, buf = _capture("%(levelname)s %(message)s")
    try:
        raise ValueError(f"loc{RLO}ation\x1b[31m")
    except ValueError:
        log.warning("failed", exc_info=True)
    out = buf.getvalue()

    assert "ation" not in out.splitlines()[-1]
    assert out.startswith("WARNING failed\nTraceback (most recent call last):\n")
    assert out.rstrip("\n").endswith("ValueError: [message withheld]")
    # The frame's source line is code, and is still escaped.
    assert RLO not in out and "\x1b" not in out


def test_formatter_escapes_stack_info_too():
    """``stack_info=True`` renders through ``formatStack``; same rule."""
    formatter = DisplayEscapingFormatter("%(message)s")

    out = formatter.formatStack(f"Stack (most recent call last):\n  File {RLO}x.py")

    assert out == f"Stack (most recent call last):\n  File {ESCAPED_RLO}x.py"


def test_formatter_escapes_an_exception_text_another_formatter_cached():
    """The stdlib caches the rendered traceback on the record across handlers."""
    record = logging.LogRecord("t", logging.WARNING, __file__, 1, "msg", None, None)
    record.exc_text = f"Traceback\n  loc{RLO}ation\nValueError"
    record.exc_info = (ValueError, ValueError("x"), None)

    out = DisplayEscapingFormatter("%(message)s", withhold_exception_text=False).format(record)

    assert RLO not in out
    assert f"  loc{ESCAPED_RLO}ation" in out


def test_default_formatter_ignores_a_traceback_another_formatter_cached():
    """The cache may hold the full text; the withholding formatter re-renders
    from ``exc_info`` and leaves the cache as it found it."""
    record = logging.LogRecord("t", logging.WARNING, __file__, 1, "msg", None, None)
    cached = f"Traceback\n  loc{RLO}ation\nValueError: secret"
    record.exc_text = cached
    record.exc_info = (ValueError, ValueError("secret"), None)

    out = DisplayEscapingFormatter("%(message)s").format(record)

    assert "secret" not in out and "ation" not in out
    assert out == "msg\nValueError: [message withheld]"
    assert record.exc_text == cached


def test_formatter_is_idempotent_on_an_already_escaped_message():
    log, buf = _capture("%(message)s")
    log.warning("name %s", "gh\\u202E")  # what a call site's ``_disp`` produced
    assert buf.getvalue() == "name gh\\u202E\n"


# ---------------------------------------------------------------------------
# Every handler STM installs uses it
# ---------------------------------------------------------------------------


def test_file_handler_and_server_stderr_handler_use_the_escaping_formatter(
    tmp_path, _restore_root_logging
):
    file_handler = open_private_log_handler(tmp_path / "stm.log")
    try:
        assert isinstance(file_handler.formatter, DisplayEscapingFormatter)
        assert file_handler.formatter._fmt == FILE_FORMAT
    finally:
        file_handler.close()

    configure_server_logging(STMConfig(log_file=None))
    [handler] = logging.getLogger().handlers
    assert isinstance(handler.formatter, DisplayEscapingFormatter)
    assert handler.formatter._fmt == STDERR_FORMAT


def test_configure_stderr_logging_installs_the_escaping_formatter(_restore_root_logging):
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)

    configure_stderr_logging(level=logging.WARNING)

    [handler] = root.handlers
    assert isinstance(handler.formatter, DisplayEscapingFormatter)
    assert handler.formatter._fmt == STDERR_FORMAT
    assert root.level == logging.WARNING


class TestHookLoggingKeepsTheLastResortContract:
    """``mms hook`` relied on ``logging.lastResort``: WARNING and up, bare
    message, the current ``sys.stderr``. The replacement must match all three."""

    def test_handler_level_gates_like_last_resort(self, _restore_root_logging, capsys):
        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)
        configure_hook_logging()
        [handler] = root.handlers
        assert handler.level == logging.lastResort.level == logging.WARNING
        assert handler.formatter._fmt == HOOK_STDERR_FORMAT

        # A child logger an embedding process set to DEBUG: lastResort dropped
        # its DEBUG/INFO records, and so must this handler.
        chatty = logging.getLogger("test.hook.chatty")
        chatty.setLevel(logging.DEBUG)
        chatty.debug("debug line")
        chatty.info("info line")
        chatty.warning("warning line")

        assert capsys.readouterr().err == "warning line\n"

    def test_an_existing_root_level_is_kept(self, _restore_root_logging, capsys):
        """A process that set the root to ERROR before the hook keeps that gating."""
        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)
        root.setLevel(logging.ERROR)

        configure_hook_logging()
        inheriting = logging.getLogger("test.hook.inherits")
        inheriting.warning("dropped")
        inheriting.error("kept")

        assert root.level == logging.ERROR
        assert capsys.readouterr().err == "kept\n"

    def test_handler_follows_a_swapped_sys_stderr(self, _restore_root_logging, capsys):
        """Two hook invocations in one process each see their own stderr."""
        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)
        configure_hook_logging()
        first = io.StringIO()
        second = io.StringIO()
        import sys as _sys

        saved = _sys.stderr
        try:
            _sys.stderr = first
            logging.getLogger("test.hook.swap").warning("one")
            _sys.stderr = second
            logging.getLogger("test.hook.swap").warning("two")
        finally:
            _sys.stderr = saved

        assert (first.getvalue(), second.getvalue()) == ("one\n", "two\n")

    def test_two_cli_invocations_both_capture_the_barrier_warning(
        self, hostile_env, _restore_root_logging
    ):
        from memtomem_stm.cli.proxy import cli

        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)
        payload = json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "/src/x.py"},
                "tool_response": {"content": "x" * 2000},
            }
        )

        results = [CliRunner().invoke(cli, ["hook"], input=payload) for _ in range(2)]

        for result in results:
            assert result.exit_code == 0
            assert "passing tool output through" in result.stderr
            assert RLO not in result.stderr


def test_configure_stderr_logging_leaves_a_configured_root_alone(_restore_root_logging):
    """Same no-op rule as ``basicConfig`` without ``force``."""
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
    existing = logging.StreamHandler(io.StringIO())
    root.addHandler(existing)

    configure_stderr_logging(level=logging.DEBUG)

    assert root.handlers == [existing]


# ---------------------------------------------------------------------------
# The two sites from the issue, plus the hook barrier, end to end
# ---------------------------------------------------------------------------


def test_log_stm_config_failure_traceback_is_escaped(hostile_env):
    log, buf = _capture(STDERR_FORMAT, withhold=False)

    log_stm_config_failure(hostile_env, logger=log, context="probing")

    out = buf.getvalue()
    assert RLO not in out
    # The hint line (escaped by #1166) and the traceback location (escaped here).
    assert "GH\\u000ASUCCESS\\u202E__COMMAND" in out
    assert f"success{ESCAPED_RLO}.prefix" in out
    assert "Traceback (most recent call last)" in out


def test_log_stm_config_failure_traceback_withholds_the_location_by_default(hostile_env):
    log, buf = _capture(STDERR_FORMAT)

    log_stm_config_failure(hostile_env, logger=log, context="probing")

    out = buf.getvalue()
    assert RLO not in out
    # The hint still names the variable to edit; the location is withheld.
    assert ESCAPED_HINT in out
    assert ".prefix" not in out and LOCATION_TEXT not in out
    assert "ValidationError: [message withheld]" in out


def _hostile_child_env(tmp_path) -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.pop("MEMTOMEM_STM_LOG_FILE", None)
    env["MEMTOMEM_STM_PROXY__CONFIG_PATH"] = str(tmp_path / "absent.json")
    env[HOSTILE_VAR] = "gh"
    return env


def test_server_entry_point_prints_one_escaped_traceback_in_a_fresh_process(tmp_path):
    """Importing the server module installs the MCP SDK's Rich handler on the
    root logger, so the early-failure path must take the root over; an
    in-process test that clears the root handlers first would hide that. And
    the entry point must not re-raise: the interpreter would print the same
    traceback a second time, outside the formatter."""
    import subprocess
    import sys

    code = (
        "import logging\n"
        "from memtomem_stm import server\n"
        "print('before', [type(h).__name__ for h in logging.getLogger().handlers])\n"
        "server.main()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=_hostile_child_env(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    # The premise of the test: the SDK handler is there before ``main`` runs.
    assert "before ['RichHandler']" in proc.stdout, proc.stdout
    assert proc.returncode == 1
    assert "ERROR memtomem_stm.server: Invalid MEMTOMEM_STM_* environment configuration" in (
        proc.stderr
    )
    assert proc.stderr.count("Traceback (most recent call last)") == 1
    assert RLO not in proc.stderr
    assert ESCAPED_HINT in proc.stderr
    assert ".prefix" not in proc.stderr
    assert "ValidationError: [message withheld]" in proc.stderr


def test_daemon_subcommand_prints_one_escaped_traceback(hostile_env, _restore_root_logging):
    """``mms daemon <cmd>`` loads the config first; same boundary rule."""
    from memtomem_stm.cli.proxy import cli

    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)

    result = CliRunner().invoke(cli, ["daemon", "status"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "invalid MEMTOMEM_STM_* configuration while loading the daemon configuration" in (
        result.stderr
    )
    assert result.stderr.count("Traceback (most recent call last)") == 1
    assert RLO not in result.stderr
    assert ESCAPED_HINT in result.stderr
    assert ".prefix" not in result.stderr
    assert "ValidationError: [message withheld]" in result.stderr


def test_server_main_takes_over_a_root_the_sdk_already_configured(
    hostile_env, capsys, _restore_root_logging
):
    """In-process twin of the test above, with the SDK's state reproduced."""
    from rich.logging import RichHandler

    from memtomem_stm import server

    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.addHandler(RichHandler(rich_tracebacks=True))

    with pytest.raises(SystemExit) as exit_:
        server.main()

    assert exit_.value.code == 1
    [handler] = root.handlers
    assert isinstance(handler.formatter, DisplayEscapingFormatter)
    err = capsys.readouterr().err
    assert RLO not in err
    assert ESCAPED_HINT in err
    assert ".prefix" not in err


def test_hook_barrier_traceback_is_escaped(hostile_env, _restore_root_logging):
    """The hook's own site drops the traceback; the pass-through barrier logs it."""
    from memtomem_stm.cli.proxy import cli

    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/src/x.py"},
        "tool_response": {"content": "x" * 2000},
    }

    result = CliRunner().invoke(cli, ["hook"], input=json.dumps(payload))

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {}
    assert "passing tool output through" in result.stderr
    assert "Traceback (most recent call last)" in result.stderr
    assert RLO not in result.stderr
    assert ".prefix" not in result.stderr
    assert "ValidationError: [message withheld]" in result.stderr
