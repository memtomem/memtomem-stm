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
_SHELL_SYNTAX = "not checked because of shell syntax (quotes, $, ~, # or operators): "
_OTHER = "not checked (an unknown launcher or an unsupported handler field): "
# What the row can miss, at the end of each detail that reports on commands (#1457).
_SCOPE = (
    "an STM hook is recognized by an STM name and a later hook in the command text, so one "
    "behind a wrapper script or with its name split by shell syntax can be missed; project, "
    "local and managed Claude Code settings are not read"
)
_NEXT_RUN = "fix the arguments of the commands that do not run the hook"
_NEXT_HOST = (
    "add --host claude where Claude Code routing is intended (or make the last "
    "--host claude), before any standalone --, keeping each command's other settings"
)
_NEXT_PLAIN = (
    "check the commands with shell syntax by hand, or spell them as plain words where that "
    "keeps their behavior (e.g. an absolute path instead of $HOME or ~)"
)
_NEXT_BY_HAND = "check the other not-checked commands by hand"


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
        # A bare ``--`` ends the options but runs the bridge; a --host appended
        # after it would be a leftover word, hence the advice's "before any --".
        ("mms hook --", "auto"),
    ],
)
def test_plain_command_reads_its_host(command: str, host: str) -> None:
    assert _read(command) == hr.HookRegistration(None, runs_bridge=True, host=host)


@pytest.mark.parametrize(
    ("command", "host"),
    [
        ("mms hook --host claude --help", "claude"),  # case 1: prints help, exits 0
        ("mms hook --host claude -h", "claude"),  # -h is a root help option
        # case 8: both facts on one location.
        ("env MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 mms hook --help", "auto"),
    ],
)
def test_help_does_not_run_but_keeps_the_host(command: str, host: str) -> None:
    assert _read(command) == hr.HookRegistration(None, runs_bridge=False, host=host)


@pytest.mark.parametrize(
    "command",
    [
        "mms hook --host claude install",  # a subcommand
        "mms hook --host claude uninstall",
        "mms hook --host claude extra",  # case 2: exits 2
        "mms hook extra",
        # The group stops reading options at the first leftover token, so a
        # later --host is never parsed: neither "lacks" nor "passes" holds.
        "mms hook extra --host claude",
        "mms hook install --host claude",
        "mms hook --host claude extra --host auto",
        "mms hook -- --host=claude",
        "mms hook --host claude --help extra --host auto",
    ],
)
def test_leftover_does_not_run_and_the_host_is_unknown(command: str) -> None:
    assert _read(command) == hr.HookRegistration(None, runs_bridge=False, host=None)


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
        # #1450: quote removal joins these into ``mms hook``, so they are STM's
        # hook (counted), but quoted words are still never read.
        'm"m"s hook --host claude',
        'mms ho"ok" --host claude',
        "'mms' 'hook' --host claude",
        # Quote-only, and the regex (not the word test) is what catches it.
        'sh -c "mms hook --host claude"',
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
        "python3 -P -m memtomem_stm hook --host claude",  # the module entry point
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
    # Whether ``shell`` applies next to ``args`` is undocumented.
    reading = _read("mms", args=["hook", "--host", "claude"], shell="powershell")
    assert reading.not_checked == hr.SHELL_FIELD
    assert _read("mms", args=["hook"], shell="bash").not_checked == hr.SHELL_FIELD


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
        # #1450: plain words are the argv, so ``hook`` must be a word of its own.
        {"type": "command", "command": "mms status --config /tmp/hook.json"},
        {"type": "command", "command": "echo /opt/bin/mms/hook.log"},
        # Shell syntax beyond quotes is never lexed, so none of these become
        # candidates by joining words: a comment, a second command, a
        # redirection target, and exec form (no shell, quotes are literal).
        {"type": "command", "command": 'true # m"m"s hook'},
        {"type": "command", "command": 'echo m"m"s\nhook'},
        {"type": "command", "command": 'true > /m"m"s hook'},
        {"type": "command", "command": "echo", "args": ['m"m"s', "hook"]},
        # Plain exec form gets the word rule too, not the raw search.
        {"type": "command", "command": "mms", "args": ["status", "--config", "/tmp/hook.json"]},
    ],
)
def test_non_candidates(handler: Any) -> None:
    assert not hr.is_candidate(handler)


@pytest.mark.parametrize(
    "command",
    [
        'm"m"s hook --host claude # comment',  # a comment keeps the line out of the quote tier
        "m\\\nms hook --host claude",  # so does a backslash line continuation
    ],
)
def test_known_misses_outside_the_quote_tier(command: str) -> None:
    """Known limit: the shell runs ``mms hook`` here, but only quote removal is
    modelled, so these are still missed (and can still let the row PASS next to
    a passing handler). A fix for them has to change this test on purpose."""
    assert not hr.is_candidate({"type": "command", "command": command})


_MORE_KNOWN_MISSES = [
    # Quote removal leaves the inner script as one word, which names nothing.
    {"type": "command", "command": "sh -c 'm\"m\"s hook --host claude'"},
    {"type": "command", "command": "sh", "args": ["-c", 'm"m"s hook --host claude']},
    # ``-m`` attached to the module name hides the name's left boundary.
    {"type": "command", "command": "python -mmemtomem_stm hook --host claude"},
    {"type": "command", "command": "python", "args": ["-mmemtomem_stm", "hook"]},
    # A wrapper script names nothing at all.
    {"type": "command", "command": "/usr/local/bin/stm-hook-wrapper.sh"},
]


@pytest.mark.parametrize("handler", _MORE_KNOWN_MISSES)
def test_known_misses_recognition_cannot_close(handler: dict[str, Any]) -> None:
    """#1457: each of these runs (or can run) the hook bridge and is not
    recognized. Recognition is by the command text, so the class is open-ended;
    the row states that limit instead of chasing each spelling."""
    assert not hr.is_candidate(handler)


@pytest.mark.parametrize(
    "command",
    [
        "mms version hook",  # the ``version`` subcommand, which rejects the extra word
        "mms status --config hook",  # the ``status`` subcommand
        "echo /opt/bin/mms/README hook",  # runs ``echo``
    ],
)
def test_known_over_matches_are_not_checked(command: str) -> None:
    """#1457: kept on purpose. Each exclusion tried for these could hide a real
    hook (``python …/memtomem_stm/__main__.py hook`` names STM only in a path
    component), and an over-match costs one "not checked" entry."""
    assert _read(command).not_checked == hr.LAUNCHER
    assert _read("python /venv/site-packages/memtomem_stm/__main__.py hook").not_checked == (
        hr.LAUNCHER
    )


def test_quote_tier_over_matches_a_printed_name() -> None:
    """Known, harmless over-match: ``echo`` only prints ``mms hook``, but the
    quote tier does not know what ``echo`` does. The handler is counted and
    reported as not checked, never given a verdict."""
    assert _read('echo m"m"s ho"ok"') == hr.HookRegistration(hr.NOT_PLAIN)


def test_quote_tier_is_skipped_above_its_length_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The quote tier's lexer is never reached for a line over the cap, which
    still gets the raw search; a line under it does reach the lexer."""
    calls: list[str] = []

    def spy(text: str) -> list[str] | None:
        calls.append(text)
        return None

    monkeypatch.setattr(hr, "_quoted_words", spy)
    # Fixed lengths, not the constant: a 4,096-character line is lexed, one
    # more character is not, and still gets the raw search.
    at_cap = 'm"m"s hook' + " " * (4096 - len('m"m"s hook'))
    over_cap = '"x" mms hook' + " " * (4097 - len('"x" mms hook'))
    assert (len(at_cap), len(over_cap)) == (4096, 4097)
    assert not hr.is_candidate({"type": "command", "command": at_cap})
    assert calls == [at_cap]
    assert hr.is_candidate({"type": "command", "command": over_cap})
    assert calls == [at_cap]


def test_candidate_scan_is_linear() -> None:
    """Many executable names without ``hook`` must not rescan the line per name."""
    import time

    # Measured: the single-pattern scan took 7.1 s here, two searches 0.6 ms.
    # The plain-word path, and (``#`` is shell syntax) the raw-text search.
    for command in ("mms " * 20_000, "mms " * 20_000 + "# note"):
        handler = {"type": "command", "command": command}
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
        "detail": (
            f"{_PREFIX}1 recognized, all passing --host claude to mms hook — in {path}; {_SCOPE}"
        ),
        "next_action": None,
    }


@pytest.mark.parametrize("missed", _MORE_KNOWN_MISSES)
def test_pass_says_it_covers_recognized_commands_only(
    tmp_path: Path, claude_dir: Path, missed: dict[str, Any]
) -> None:
    """#1457: a hook the row cannot recognize still leaves it at PASS, so the
    PASS has to say what it counted and what it cannot see."""
    path = _write(claude_dir, _cmd("mms hook --host claude"), missed)

    row = _row(tmp_path)

    assert row["status"] == "PASS"
    assert row["detail"] == (
        f"{_PREFIX}1 recognized, all passing --host claude to mms hook — in {path}; {_SCOPE}"
    )
    assert "can be missed" in row["detail"]


def test_text_output_states_the_recognition_limit(tmp_path: Path, claude_dir: Path) -> None:
    """The text renderer shows the whole detail that ``--json`` carries, once."""
    path = _write(claude_dir, _cmd("mms hook --host claude"))

    result = CliRunner().invoke(cli, ["doctor", "--config", str(tmp_path / "absent.json")])

    line = next(line for line in result.output.splitlines() if "hook registration" in line)
    detail = f"{_PREFIX}1 recognized, all passing --host claude to mms hook — in {path}; {_SCOPE}"
    assert line.endswith(detail)
    assert line.count("can be missed") == 1


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
        f"1 {_SHELL_SYNTAX}hooks.PostToolUse[1].hooks[0]; "
        f"2 {_OTHER}{loc(5)} (launcher), {loc(6)} (launcher)"
        f" — in {path}; {_SCOPE}"
    )
    assert row["next_action"] == (
        f"{_NEXT_RUN}; then {_NEXT_HOST}; then {_NEXT_PLAIN}; then {_NEXT_BY_HAND}"
    )
    # Cases 6/7: install rewrites every STM handler and drops inline settings,
    # and does not recognize a ``NAME=value mms hook`` line at all.
    assert "install" not in row["next_action"]


@pytest.mark.parametrize("quoted", ['m"m"s hook', 'sh -c "mms hook"'])
def test_quoted_hook_beside_a_passing_one_warns(
    tmp_path: Path, claude_dir: Path, quoted: str
) -> None:
    """#1450: a quoted STM hook next to a passing one must not let the file
    PASS. ``m"m"s hook`` was missed before; ``sh -c "mms hook"`` guards the
    regex fallback."""
    path = _write(claude_dir, _cmd("mms hook --host claude"), _cmd(quoted))

    row = _row(tmp_path)

    assert row["status"] == "WARN"
    assert row["detail"] == f"{_PREFIX}1 {_SHELL_SYNTAX}{_LOC1} — in {path}; {_SCOPE}"
    assert row["next_action"] == _NEXT_PLAIN


def test_this_macs_registration_shape_is_not_checked(tmp_path: Path, claude_dir: Path) -> None:
    path = _write(
        claude_dir,
        _cmd("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 $HOME/.local/bin/mms hook --host claude"),
    )

    row = _row(tmp_path)

    assert row["status"] == "WARN"
    assert row["detail"] == f"{_PREFIX}1 {_SHELL_SYNTAX}{_LOC0} — in {path}; {_SCOPE}"
    assert row["next_action"] == _NEXT_PLAIN


def test_windows_shell_form_warns_with_windows_advice(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shell form is never read on Windows, so even install's own output is
    unverified there: WARN, with advice that fits (a plain-word rewrite would
    not make it checkable). Exec form is still read and can pass."""
    monkeypatch.setattr(hr, "running_on_windows", lambda: True)
    path = _write(
        claude_dir,
        _cmd("mms hook --help"),
        {"type": "command", "command": "mms.exe", "args": ["hook", "--host", "claude"]},
    )

    row = _row(tmp_path)

    assert row["status"] == "WARN"
    assert row["detail"] == (
        f"{_PREFIX}1 in shell form, which is not checked on Windows (Claude Code may run it "
        f"under Git Bash or PowerShell): {_LOC0} — in {path}; {_SCOPE}"
    )
    assert row["next_action"] == (
        "check by hand that each Windows shell-form command runs mms hook with --host claude"
    )

    _write(
        claude_dir, {"type": "command", "command": "mms.exe", "args": ["hook", "--host", "claude"]}
    )
    assert _row(tmp_path)["status"] == "PASS"


def test_default_settings_path_without_claude_config_dir(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    (tmp_path / "home" / ".claude").mkdir(parents=True)
    path = _write(tmp_path / "home" / ".claude", _cmd("mms hook"))

    row = _row(tmp_path)

    assert row["detail"] == f"{_PREFIX}1 {_LACKING}{_LOC0} — in {path}; {_SCOPE}"


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
        "next_action": (
            "make the Claude Code settings file readable and valid JSON, then rerun mms doctor"
        ),
    }


@pytest.mark.parametrize(
    ("make", "detail", "action"),
    [
        (
            lambda d: (d / "settings.json").write_text("[]"),
            "the Claude Code user settings are valid JSON but not an object",
            "make the settings file's top level a JSON object, then rerun mms doctor",
        ),
        (
            lambda d: (d / "settings.json").mkdir(),
            "the Claude Code user settings path is not a regular file",
            "replace it with the settings JSON file, then rerun mms doctor",
        ),
    ],
)
def test_unusable_settings_get_specific_advice(
    tmp_path: Path, claude_dir: Path, make: Any, detail: str, action: str
) -> None:
    make(claude_dir)

    row = _row(tmp_path)

    assert row["detail"] == f"{detail}; STM hook registrations were not checked"
    assert row["next_action"] == action


@pytest.mark.parametrize("kind", ["relative", "a file"])
def test_unusable_claude_config_dir_warns(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    if kind == "relative":
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative/claude")
    else:
        # ``settings.json`` under a file raises NotADirectoryError, which is
        # the override's fault, not the settings file's.
        not_a_dir = tmp_path / "claude-config-file"
        not_a_dir.write_text("{}")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(not_a_dir))

    row = _row(tmp_path)

    assert row == {
        "id": "hook_registration",
        "label": "hook registration",
        "status": "WARN",
        "detail": "CLAUDE_CONFIG_DIR does not name a usable directory; "
        "STM hook registrations were not checked",
        "next_action": "set CLAUDE_CONFIG_DIR to an absolute directory, or unset it",
    }


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
        "the Claude Code user settings path is not a regular file; "
        "STM hook registrations were not checked"
    )
