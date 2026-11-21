"""``mms doctor``'s ``hook_registration`` check (#1443).

Bash output compression replaces output only for a hook registered with
``--host claude`` (#1437). The check reads Claude Code's user settings and
reports, by location, the STM hook commands that lack it, that do not run the
hook, or that cannot be read without modelling the shell. The cases from #1443
are the regression list; each names the case it covers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from helpers import set_home
from memtomem_stm.cli import hook_registration as hr
from memtomem_stm.cli.proxy import cli

_WINDOWS = sys.platform == "win32"
_LOC0 = "hooks.PostToolUse[0].hooks[0]"
_LOC1 = "hooks.PostToolUse[0].hooks[1]"
_NOT_RUNNING = "not running the hook (--help, a usage error or a subcommand): "
_PREFIX = "STM hook commands: "
_LACKING = "without --host claude, so hook compression, if enabled, never replaces their output: "
_NOT_CHECKED = (
    "not checked (only plain words without quotes or expansions, after a known launcher, "
    "are read): "
)
_SCOPE = "project, local and managed Claude Code settings are not read"
_NEXT_RUN = "fix the arguments of the commands that do not run the hook"
_NEXT_HOST = (
    "add --host claude where Claude Code routing is intended (or make the last "
    "--host claude), keeping each command's other settings"
)
_NEXT_BY_HAND = (
    "check the not-checked commands by hand, or spell them as plain words "
    "(an absolute path instead of $HOME or ~)"
)


def _read(command: str, *, windows: bool = False, **fields: Any) -> hr.HookRegistration:
    handler = {"type": "command", "command": command, **fields}
    assert hr.is_candidate(handler), command
    return hr.inspect_hook_registration(handler, windows=windows)


# ---------------------------------------------------------------------------
# Reading one handler
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "host"),
    [
        ("mms hook --host claude", "claude"),
        ("mms hook", "auto"),
        ("mms hook --host=claude", "claude"),
        ("mms hook --host claude --host auto", "auto"),  # the last one wins
        ("mms hook --host", "auto"),  # a bare --host is auto
        ("mms hook --host codex", "codex"),
        # An option value that looks like --help is consumed as the value.
        ("mms hook --host claude --surfacing-timeout-seconds --help", "claude"),
        ("/opt/bin/memtomem-stm hook --host claude", "claude"),
        ("memtomem-stm-proxy hook --host claude", "claude"),
        ("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 mms hook", "auto"),
        ("MEMTOMEM_STM_A=1 MEMTOMEM_STM_B=x mms hook --host claude", "claude"),
        ("env MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 mms hook --host claude", "claude"),
        # STM's own source-install shape.
        ("uv run --directory /repo/memtomem-stm memtomem-stm hook --host claude", "claude"),
        # Install's own global output.
        (
            "memtomem-stm hook --host claude --use-daemon --surfacing-timeout-seconds 2 "
            "--daemon-timeout-seconds 3 --no-persist-query-text",
            "claude",
        ),
        ("mms\thook  --host claude ", "claude"),
    ],
)
def test_plain_command_reads_its_host(command: str, host: str) -> None:
    assert _read(command) == hr.HookRegistration(None, runs_bridge=True, host=host)


@pytest.mark.parametrize(
    ("command", "host"),
    [
        ("mms hook --host claude --help", "claude"),  # case 1: prints help, exits 0
        ("mms hook --host claude -h", "claude"),  # -h is a root help option
        ("mms hook --host claude install", "claude"),  # a subcommand
        ("mms hook --host claude uninstall", "claude"),
        ("mms hook --host claude extra", "claude"),  # case 2: exits 2
        ("mms hook extra", "auto"),
        # case 8: both facts on one location.
        ("env MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 mms hook --help", "auto"),
    ],
)
def test_help_or_leftover_does_not_run_but_keeps_the_host(command: str, host: str) -> None:
    assert _read(command) == hr.HookRegistration(None, runs_bridge=False, host=host)


@pytest.mark.parametrize(
    "command",
    [
        "mms hook --host claude --typo",  # case 2
        "mms hook --host claude --surfacing-timeout-seconds",  # case 2: missing value
        # Click stops at the unknown option, so the later --host is never read.
        "mms hook --typo --host claude",
        # Click reads every option before the eager --help callback runs, so a
        # usage error anywhere wins over help.
        "mms hook --typo --help",
        "mms hook --help --typo",
    ],
)
def test_usage_error_does_not_run_and_the_host_is_unknown(command: str) -> None:
    assert _read(command) == hr.HookRegistration(None, runs_bridge=False, host=None)


def test_measured_exit_codes_match_the_reading() -> None:
    """The cases above are what ``mms hook`` actually does, not an assumption."""

    def exit_code(*args: str) -> int:
        return subprocess.run(
            # None of these reaches the bridge, so nothing is read or written.
            [sys.executable, "-P", "-m", "memtomem_stm", "hook", *args],
            input="{}",
            capture_output=True,
            text=True,
            timeout=60,
        ).returncode

    assert exit_code("--host", "claude", "--help") == 0
    assert exit_code("--host", "claude", "--typo") == 2
    assert exit_code("--host", "claude", "extra") == 2
    assert exit_code("--host", "claude", "--surfacing-timeout-seconds") == 2


@pytest.mark.parametrize(
    "command",
    [
        'mms hook --host claude --daemon-timeout-seconds "10',  # case 3: unmatched quote
        "mms hook --host auto\n--host claude",  # case 4: the shell runs the first line
        "mms hook --host claude --daemon-timeout-seconds #",  # case 5: a comment
        '"MEMTOMEM_STM_X=1" mms hook --host claude',  # a quoted word is a command name
        'MEMTOMEM_STM_X"="1 mms hook --host claude',
        "$HOOK_DIR/bin/mms hook --host claude",  # an unquoted expansion word-splits
        "MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 $HOME/.local/bin/mms hook --host claude",
        "~/.local/bin/mms hook --host claude",
        "mms ${HOOK_ACTION:-hook} --host claude",  # sh expands it to ``hook``
        "mms hook --host claude\x00 --typo",
        "mms hook --host claude; echo done",
        "mms hook --host claude | tee /tmp/x",
        "mms hook --host 'claude'",
    ],
)
def test_shell_syntax_is_not_checked(command: str) -> None:
    assert _read(command) == hr.HookRegistration(hr.NOT_PLAIN)


@pytest.mark.parametrize(
    "command",
    [
        "uv run echo mms hook --host claude",  # runs echo
        "uv run --help mms hook --host claude",  # prints uv's help
        "uv run --directory --help mms hook --host claude",
        "uv run mms hook --host claude",  # not STM's install shape
        "PATH=/no-such-dir mms hook --host claude",  # changes what runs
        "_MMS_COMPLETE=bash_source mms hook --host claude",  # Click's completion path
        "MEMTOMEM_STM_X-Y=1 mms hook --host claude",  # not a shell assignment
        "env -i mms hook --host claude",
        "env mms hook --host claude",
        "env PATH=/x mms hook --host claude",
        "nice mms hook --host claude",
        "echo mms hook --host claude",
        "mms --verbose hook --host claude",
        "mms hooks --host claude --x hook",
        # A word with ``=`` after a name is an assignment, so ``hook`` would run.
        "OTHER=/mms hook --host claude",
        "env MEMTOMEM_STM_X=1 OTHER=/mms hook --host claude",
        "PATH=/usr/env MEMTOMEM_STM_X=1 mms hook --host claude",
        "env MY_CUSTOM_VAR=1 mms hook --host claude",  # issue case 7b
        # These name directories; pathlib would shorten them to ``mms``.
        "mms/ hook --host claude",
        "/opt/bin/mms/. hook --host claude",
        "/opt/bin/mms/.. hook --host claude",
        # ``uv run`` takes a bare entry-point name; anything else is not STM's.
        "uv run --directory /repo --not-a-uv-option/mms hook --host claude",
        "uv run --directory /repo https://example.com/mms hook --host claude",
        "uv run --directory /repo /repo/.venv/bin/memtomem-stm hook --host claude",
    ],
)
def test_unknown_launcher_is_not_checked(command: str) -> None:
    assert _read(command) == hr.HookRegistration(hr.LAUNCHER)


def test_exec_form_is_read_from_args() -> None:
    assert _read("mms", args=["hook", "--host", "claude"]) == hr.HookRegistration(
        None, runs_bridge=True, host="claude"
    )
    assert _read("/opt/bin/mms", args=["hook"]) == hr.HookRegistration(
        None, runs_bridge=True, host="auto"
    )
    assert _read("env", args=["MEMTOMEM_STM_X=1", "mms", "hook"]).host == "auto"


def test_exec_form_word_rule_and_shape() -> None:
    # A space-padded value is accepted at runtime, but not a plain word here.
    assert _read("mms", args=["hook", "--host", " claude "]).not_checked == hr.NOT_PLAIN
    # Click expands these on Windows before parsing.
    assert _read("mms", args=["hook", "--host", "%HOST%"]).not_checked == hr.NOT_PLAIN
    assert _read("mms", args=["hook", "--host", "$HOST"]).not_checked == hr.NOT_PLAIN
    # No shell runs an exec-form handler, so a bare assignment is the executable.
    assert _read("MEMTOMEM_STM_X=1", args=["mms", "hook"]).not_checked == hr.LAUNCHER
    assert _read("mms", args="hook --host claude").not_checked == hr.ARGS_FIELD
    assert _read("mms", args=["hook", 1]).not_checked == hr.ARGS_FIELD
    # No executable path holds a NUL or an ``=``-shaped assignment.
    assert _read("\x00/mms", args=["hook", "--host", "claude"]).not_checked == hr.LAUNCHER
    assert _read("X=/mms", args=["hook", "--host", "claude"]).not_checked == hr.LAUNCHER


def test_shell_field_other_than_bash_is_not_checked() -> None:
    assert _read("mms hook --host claude", shell="powershell").not_checked == hr.SHELL_FIELD
    # A present field must have its documented type; null is not "absent".
    assert _read("mms hook --host claude", shell=None).not_checked == hr.SHELL_FIELD
    assert _read("mms hook --host claude", args=None).not_checked == hr.ARGS_FIELD
    assert _read("mms hook --host claude", shell="bash").host == "claude"


def test_windows_rules() -> None:
    # Shell form runs under Git Bash (which may convert path-like arguments) or
    # PowerShell, so it is not read even with ``shell: "bash"``.
    assert _read("mms hook --host claude", windows=True).not_checked == hr.WINDOWS_SHELL
    reading = _read("mms hook --host claude", windows=True, shell="bash")
    assert reading.not_checked == hr.WINDOWS_SHELL
    # Git's ``env.exe`` is an MSYS program and may convert path-like arguments.
    reading = _read("env", windows=True, args=["MEMTOMEM_STM_X=1", "mms", "hook"])
    assert reading.not_checked == hr.LAUNCHER
    assert _read("C:\\Tools\\mms\\", windows=True, args=["hook"]).not_checked == hr.LAUNCHER
    reading = _read(r"C:\Tools\MMS.EXE", windows=True, args=["hook", "--host", "claude"])
    assert reading == hr.HookRegistration(None, runs_bridge=True, host="claude")


@pytest.mark.parametrize(
    "handler",
    [
        {"type": "command", "command": "echo hi"},
        {"type": "command", "command": "mms-notify hookup"},
        {"type": "command", "command": "mms status"},
        {"type": "http", "command": "mms hook --host claude"},  # not run as a command
        {"command": "mms hook --host claude"},
        {"type": "command", "command": ["mms", "hook"]},
        "mms hook",
    ],
)
def test_non_candidates(handler: Any) -> None:
    assert not hr.is_candidate(handler)


def test_candidate_scan_is_linear() -> None:
    """Many executable names without ``hook`` must not rescan the line per name."""
    import time

    # Measured: the single-pattern scan took 7.1 s here, two searches 0.6 ms.
    handler = {"type": "command", "command": "mms " * 20_000}
    start = time.perf_counter()
    assert not hr.is_candidate(handler)
    assert time.perf_counter() - start < 2.0


# ---------------------------------------------------------------------------
# The doctor row
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    set_home(monkeypatch, tmp_path / "home")
    config_dir = tmp_path / "claude"
    config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", raising=False)
    # The reader's own seam, not ``sys.platform``: faking that would also make
    # ``cli.main()`` rewrite a Windows runner's real PATH with POSIX rules.
    monkeypatch.setattr(hr, "running_on_windows", lambda: False)
    return config_dir


def _write(config_dir: Path, *handlers: Any, extra_group: Any = None) -> Path:
    groups: list[Any] = [{"matcher": "Read|Grep|Glob|Bash", "hooks": list(handlers)}]
    if extra_group is not None:
        groups.append(extra_group)
    path = config_dir / "settings.json"
    path.write_text(json.dumps({"hooks": {"PostToolUse": groups}}), encoding="utf-8")
    return path


def _cmd(command: str) -> dict[str, str]:
    return {"type": "command", "command": command}


def _doctor(tmp_path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    # A missing proxy config short-circuits the probes; the env-path checks,
    # this one included, still run.
    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(tmp_path / "absent.json"), "--json"]
    )
    checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
    return checks, result.stdout


def _row(tmp_path: Path) -> dict[str, Any]:
    return _doctor(tmp_path)[0]["hook_registration"]


def test_registration_with_host_claude_passes(tmp_path: Path, claude_dir: Path) -> None:
    path = _write(claude_dir, _cmd("mms hook --host claude"), _cmd("echo unrelated"))

    assert _row(tmp_path) == {
        "id": "hook_registration",
        "label": "hook registration",
        "status": "PASS",
        "detail": f"1 STM hook command(s) pass --host claude to mms hook in {path}; {_SCOPE}",
        "next_action": None,
    }


@pytest.mark.parametrize("env", [None, "1", "0"])
def test_bare_registration_warns_whatever_compression_says(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch, env: str | None
) -> None:
    """Case 9: no compression gate, so neither doctor's own environment nor an
    inline setting can hide the registration."""
    if env is not None:
        monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", env)
    path = _write(
        claude_dir,
        _cmd("mms hook"),
        _cmd("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=0 mms hook"),
    )

    row = _row(tmp_path)

    assert row["status"] == "WARN"
    assert row["detail"] == f"{_PREFIX}2 {_LACKING}{_LOC0}, {_LOC1} — in {path}; {_SCOPE}"
    assert row["next_action"] == _NEXT_HOST


def test_mixed_handlers_report_each_category_by_location(tmp_path: Path, claude_dir: Path) -> None:
    """Cases 6–8: the mixed file install would merge into one handler, an inline
    opt-in, a custom env wrapper, and a broken command missing the flag."""
    path = _write(
        claude_dir,
        _cmd("mms hook"),
        _cmd("env MEMTOMEM_STM_HOOK__COMPRESSION__MAX_CHARS=8000 mms hook --host claude"),
        _cmd("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 mms hook"),
        _cmd("env MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 mms hook --help"),
        _cmd("env MEMTOMEM_STM_HOOK__METRICS_ENABLED=0 mms hook --host claude --typo"),
        _cmd("env -i mms hook --host claude"),
        _cmd("env MY_CUSTOM_VAR=1 mms hook"),
        extra_group={"matcher": "Bash", "hooks": [_cmd("mms hook --host claude # note")]},
    )

    row = _row(tmp_path)

    loc = "hooks.PostToolUse[0].hooks[{}]".format
    assert row["status"] == "WARN"
    assert row["detail"] == (
        f"{_PREFIX}2 {_NOT_RUNNING}{loc(3)}, {loc(4)}; "
        f"3 {_LACKING}{loc(0)}, {loc(2)}, {loc(3)}; "
        f"3 {_NOT_CHECKED}{loc(5)} (launcher), {loc(6)} (launcher), "
        "hooks.PostToolUse[1].hooks[0] (shell syntax)"
        f" — in {path}; {_SCOPE}"
    )
    assert row["next_action"] == f"{_NEXT_RUN}; then {_NEXT_HOST}; then {_NEXT_BY_HAND}"
    # Cases 6/7: install rewrites every STM handler and drops inline settings,
    # and does not recognize a ``NAME=value mms hook`` line at all.
    assert "install" not in row["next_action"]


def test_this_macs_registration_shape_is_not_checked(tmp_path: Path, claude_dir: Path) -> None:
    path = _write(
        claude_dir,
        _cmd("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 $HOME/.local/bin/mms hook --host claude"),
    )

    row = _row(tmp_path)

    assert row["status"] == "WARN"
    assert row["detail"] == f"{_PREFIX}1 {_NOT_CHECKED}{_LOC0} (shell syntax) — in {path}; {_SCOPE}"
    assert row["next_action"] == _NEXT_BY_HAND


def test_command_text_is_never_shown(tmp_path: Path, claude_dir: Path) -> None:
    secret = "cnrySecretValue"
    _write(
        claude_dir,
        _cmd(f"MEMTOMEM_STM_LANGFUSE__SECRET_KEY={secret} mms hook"),
        _cmd(f"MEMTOMEM_STM_LANGFUSE__SECRET_KEY={secret} mms hook --host 'x'"),
    )

    checks, stdout = _doctor(tmp_path)

    # Positive control: both handlers were reported.
    assert _LOC0 in checks["hook_registration"]["detail"]
    assert _LOC1 in checks["hook_registration"]["detail"]
    assert secret not in stdout
    text = CliRunner().invoke(cli, ["doctor", "--config", str(tmp_path / "absent.json")])
    assert secret not in text.output


def test_project_settings_are_not_read(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 10: only the user settings install writes are read."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"PostToolUse": [{"hooks": [_cmd("mms hook")]}]}})
    )
    monkeypatch.chdir(project)

    assert "hook_registration" not in _doctor(tmp_path)[0]


def test_no_row_without_an_stm_hook(tmp_path: Path, claude_dir: Path) -> None:
    assert "hook_registration" not in _doctor(tmp_path)[0]  # no file
    _write(claude_dir, _cmd("echo hi"), {"type": "http", "url": "http://127.0.0.1:9/"})
    assert "hook_registration" not in _doctor(tmp_path)[0]
    (claude_dir / "settings.json").write_text(json.dumps({"hooks": {"PostToolUse": "x"}}))
    assert "hook_registration" not in _doctor(tmp_path)[0]


def test_invalid_json_warns_with_the_type_only(tmp_path: Path, claude_dir: Path) -> None:
    (claude_dir / "settings.json").write_text('{"hooks": "cnryBroken', encoding="utf-8")

    row = _row(tmp_path)

    assert row == {
        "id": "hook_registration",
        "label": "hook registration",
        "status": "WARN",
        "detail": "the Claude Code user settings could not be read (JSONDecodeError); "
        "STM hook registrations were not checked",
        "next_action": "make the Claude Code settings file readable, then rerun mms doctor",
    }


def test_relative_claude_config_dir_warns(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative/claude")

    row = _row(tmp_path)

    assert row["status"] == "WARN"
    assert row["detail"].startswith("the Claude Code user settings could not be read (")


@pytest.mark.skipif(
    _WINDOWS or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX permission bits; root reads everything",
)
@pytest.mark.parametrize("blocked", ["file", "parent"])
def test_unreadable_settings_warn_instead_of_reading_as_absent(
    tmp_path: Path, claude_dir: Path, blocked: str
) -> None:
    """``Path.exists`` reads a file under an unsearchable directory as absent on
    Python 3.14 (#1285); the check must still report it."""
    _write(claude_dir, _cmd("mms hook"))
    target = claude_dir / "settings.json" if blocked == "file" else claude_dir
    target.chmod(0)
    try:
        row = _row(tmp_path)
    finally:
        target.chmod(0o700)

    assert row["status"] == "WARN"
    assert row["detail"] == (
        "the Claude Code user settings could not be read (PermissionError); "
        "STM hook registrations were not checked"
    )


@pytest.mark.skipif(_WINDOWS, reason="a newline is not allowed in a Windows directory name")
def test_text_output_escapes_the_path(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forged = tmp_path / "evil\n  PASS  forged"
    forged.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(forged))
    _write(forged, _cmd("mms hook"))

    result = CliRunner().invoke(cli, ["doctor", "--config", str(tmp_path / "absent.json")])

    lines = result.output.splitlines()
    assert any("hook registration" in line and "evil\\u000A" in line for line in lines)
    assert not any(line.startswith("  PASS  forged") for line in lines)
    next_line = next(line for line in lines if line.startswith("        next: add --host"))
    assert "evil" not in next_line


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a POSIX FIFO")
def test_fifo_settings_warn_instead_of_blocking(tmp_path: Path, claude_dir: Path) -> None:
    """Opening a FIFO blocks until a writer appears; the check must not open it.
    Run in a subprocess with a timeout so a regression fails instead of hanging."""
    os.mkfifo(claude_dir / "settings.json")
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(claude_dir), "HOME": str(tmp_path / "home")}

    result = subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "memtomem_stm",
            "doctor",
            "--config",
            str(tmp_path / "absent.json"),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    checks = {c["id"]: c for c in json.loads(result.stdout)["checks"]}
    assert checks["hook_registration"]["detail"] == (
        "the Claude Code user settings could not be read (HookConfigNotRegularFile); "
        "STM hook registrations were not checked"
    )
