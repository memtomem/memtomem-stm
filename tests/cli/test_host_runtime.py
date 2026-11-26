"""Managed host-runtime policy: resolution, migration, and deadline ordering."""

from __future__ import annotations

import pytest

from memtomem_stm.cli.host_runtime import (
    HostRuntimePolicy,
    PartialHostRuntimePolicy,
    _parse_bool,
    _parse_positive_float,
    format_seconds,
    owned_hook_command,
    resolve_host_runtime_policy,
    runtime_env_overrides,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("garbage", None),
        (" TRUE ", True),
        ("On", True),
        ("Y", True),
        ("t", True),
        ("f", False),
        ("0", False),
        ("no", False),
    ],
)
def test_parse_bool_direct(raw: str | None, expected: bool | None) -> None:
    assert _parse_bool(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("0", None),
        ("-0", None),
        ("-1", None),
        ("nan", None),
        ("inf", None),
        ("1e400", None),
        ("12", 12.0),
        (" 3.5 ", 3.5),
    ],
)
def test_parse_positive_float_direct(raw: str | None, expected: float | None) -> None:
    assert _parse_positive_float(raw) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(12.0, "12"), (2.5, "2.5"), (0.1 + 0.2, "0.3"), (1e16, "1e+16")],
)
def test_format_seconds_direct(seconds: float, expected: str) -> None:
    assert format_seconds(seconds) == expected


@pytest.fixture(autouse=True)
def _isolated_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "MEMTOMEM_STM_HOOK__USE_DAEMON",
        "MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS",
        "MEMTOMEM_STM_SURFACING__USE_DAEMON",
        "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS",
        "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT",
    ):
        monkeypatch.delenv(key, raising=False)


def test_fresh_policy_uses_effective_timeout_and_orders_hook_deadline() -> None:
    policy = resolve_host_runtime_policy()
    assert policy == HostRuntimePolicy(
        use_daemon=True,
        surfacing_timeout_seconds=3.0,
        daemon_timeout_seconds=4.0,
        persist_query_text=False,
    )


def test_cli_timeout_wins_over_existing_and_existing_wins_over_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS", "5")
    existing = (
        "memtomem-stm hook --host claude --no-daemon "
        "--surfacing-timeout-seconds 12 --daemon-timeout-seconds 20 "
        "--persist-query-text"
    )
    owned = owned_hook_command(existing, "claude")
    assert owned is not None
    preserved = resolve_host_runtime_policy(existing=owned.settings)
    assert preserved == HostRuntimePolicy(False, 12.0, 20.0, True)

    explicit = resolve_host_runtime_policy(
        existing=owned.settings,
        use_daemon=True,
        surfacing_timeout_seconds=30.0,
    )
    assert explicit == HostRuntimePolicy(True, 30.0, 31.0, True)


def test_an_inline_env_prefix_is_not_a_registration_install_reads() -> None:
    """No release wrote this form, so nothing is recovered from it: the
    replacement gets the effective configuration, not these values."""
    assert (
        owned_hook_command(
            "env MEMTOMEM_STM_HOOK__USE_DAEMON=false "
            "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS=12 "
            "MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS=14 "
            "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT=false "
            "memtomem-stm hook --host claude",
            "claude",
        )
        is None
    )


@pytest.mark.parametrize(
    "assignments",
    [
        # The proxy's variable, which the hook does not read (#1484).
        {"MEMTOMEM_STM_SURFACING__USE_DAEMON": "false"},
        {"MEMTOMEM_STM_HOOK__USE_DAEMON": "", "MEMTOMEM_STM_SURFACING__USE_DAEMON": "false"},
        # The hook's own variable, which it does read.
        {"MEMTOMEM_STM_HOOK__USE_DAEMON": "false"},
        {"MEMTOMEM_STM_HOOK__USE_DAEMON": "true", "MEMTOMEM_STM_SURFACING__USE_DAEMON": "false"},
    ],
)
def test_no_daemon_mode_is_read_from_an_env_prefix(assignments: dict[str, str]) -> None:
    """#1484 asked that the proxy's variable not be read as the hook's mode.
    Install never wrote an ``env`` prefix, so now nothing in one is read,
    the hook's own variable included: the line is not install's."""
    prefix = " ".join(f"{key}={value}" for key, value in assignments.items())

    assert owned_hook_command(f"env {prefix} memtomem-stm hook --host claude", "claude") is None
    # Positive control: without the prefix the same line is install's own.
    assert owned_hook_command("memtomem-stm hook --host claude", "claude") is not None


def test_runtime_overlay_ignores_invalid_values_and_normalizes_deadline() -> None:
    overrides = runtime_env_overrides(
        use_daemon="true",
        surfacing_timeout_seconds="12",
        daemon_timeout_seconds="2",
        persist_query_text="false",
    )
    assert overrides == {
        "MEMTOMEM_STM_HOOK__USE_DAEMON": "true",
        "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS": "12",
        "MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS": "13",
        "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT": "false",
    }
    assert (
        runtime_env_overrides(
            use_daemon="garbage",
            surfacing_timeout_seconds="nan",
            daemon_timeout_seconds="-1",
            persist_query_text="garbage",
        )
        == {}
    )


def test_mcp_policy_serializes_shared_daemon_contract() -> None:
    policy = HostRuntimePolicy(True, 12.0, 13.0, False)
    flat = {
        "MEMTOMEM_STM_SURFACING__USE_DAEMON": "true",
        "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS": "12",
        "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT": "false",
    }
    # The same policy again in the channel the server applies as a settings
    # layer (#1544), keys sorted.
    assert policy.mcp_env() == {
        **flat,
        "MEMTOMEM_STM_SERVER_FLAGS": (
            '{"MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT": "false", '
            '"MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS": "12", '
            '"MEMTOMEM_STM_SURFACING__USE_DAEMON": "true"}'
        ),
    }


def test_explicit_config_path_reaches_the_policy_construction(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#839 (review round 1): ``init``/``register`` resolve the policy via
    ``_registration_command`` while holding the ``--config`` path, but the
    bare construction read the default file — so a per-field env override of
    a server only the flag's file declares refused to build and crashed the
    registration.

    The root conftest isolates HOME and ambient STM variables so a developer
    shell cannot make the positive control's expected failure disappear."""
    import json

    from pydantic import ValidationError

    config = tmp_path / "stm_proxy.json"
    config.write_text(
        json.dumps(
            {
                "enabled": True,
                "upstream_servers": {"fake": {"prefix": "fk", "command": "file-server"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-server")

    # Positive control: the override really is live and really does break the
    # bare construction (the default path declares no such server).
    with pytest.raises(ValidationError):
        resolve_host_runtime_policy()

    policy = resolve_host_runtime_policy(config_path=config)
    assert policy.use_daemon is True


# ── recovery reads what the hook reads (#1476) ───────────────────────────────


_SETTINGS = (
    "--no-daemon --surfacing-timeout-seconds 7 --daemon-timeout-seconds 9 --persist-query-text"
)


@pytest.mark.parametrize(
    "command",
    [
        # The shell drops a comment, so the hook never receives these flags.
        f"memtomem-stm hook --host # {_SETTINGS}",
        f"memtomem-stm hook --host claude #c {_SETTINGS}",
        # A here-string: the shell sends the flags to stdin, not to the hook.
        f"memtomem-stm hook --host <<< {_SETTINGS}",
        # Flags the hook would read, but not in the form install writes.
        "memtomem-stm hook --persist-query-text --no-persist-query-text --host claude",
        "memtomem-stm hook --host claude --surfacing-timeout-seconds=7",
        # #1484: a variable the hook does not read, in a prefix install never wrote.
        "env MEMTOMEM_STM_SURFACING__USE_DAEMON=false memtomem-stm hook --host claude",
    ],
)
def test_no_setting_is_read_from_a_line_install_did_not_write(command: str) -> None:
    assert owned_hook_command(command, "claude") is None


def test_settings_are_read_from_the_form_install_writes() -> None:
    """Positive control for the lines above: the same flags, in place."""
    owned = owned_hook_command(f"memtomem-stm hook --host claude {_SETTINGS}", "claude")

    assert owned is not None
    assert owned.settings == PartialHostRuntimePolicy(
        use_daemon=False,
        surfacing_timeout_seconds=7.0,
        daemon_timeout_seconds=9.0,
        persist_query_text=True,
    )


def test_a_long_number_slot_is_refused_in_linear_time() -> None:
    """The slot pattern backtracks one step per digit and fails each at once;
    a nested quantifier here would make a long handler line stall install."""
    import time

    digits = "9" * 2_000_000
    line = (
        f"memtomem-stm hook --host claude --use-daemon --surfacing-timeout-seconds {digits}"
        " --daemon-timeout-seconds 4 --no-persist-query-text x"
    )

    started = time.perf_counter()
    assert owned_hook_command(line, "claude") is None
    assert time.perf_counter() - started < 5.0


# --- the variables the hook sets from an owned command's flags (#1508) -------

_EXTREME = "1.7976931348623157e+308"


def _policy(daemon_flag: str, surfacing: str, daemon: str, persist_flag: str) -> list[str]:
    """Setting words as literal strings, in install's order: never normalized or
    reformatted, so a word ``format_seconds`` would rewrite reaches the hook as is."""
    return [
        daemon_flag,
        "--surfacing-timeout-seconds",
        surfacing,
        "--daemon-timeout-seconds",
        daemon,
        persist_flag,
    ]


def _hook_overlay(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> dict[str, str]:
    """The overlay ``mms hook <args>`` applies, read from a real invocation."""
    import contextlib

    from click.testing import CliRunner

    from memtomem_stm.cli import hook_cmd
    from memtomem_stm.cli.proxy import cli

    seen: list[dict[str, str]] = []

    @contextlib.contextmanager
    def record(overrides: dict[str, str]):
        seen.append(dict(overrides))
        yield

    monkeypatch.setattr(hook_cmd, "_runtime_registration_env", record)
    result = CliRunner().invoke(cli, ["hook", *args], input="")
    assert result.exit_code == 0, result.output
    assert len(seen) == 1, seen
    return seen[0]


@pytest.mark.parametrize(
    "policy",
    [
        [],
        _policy("--use-daemon", "3", "4", "--no-persist-query-text"),
        _policy("--no-daemon", "3", "4", "--persist-query-text"),
        _policy("--use-daemon", "3", _EXTREME, "--no-persist-query-text"),
        _policy("--use-daemon", _EXTREME, "4", "--no-persist-query-text"),
        _policy("--use-daemon", "3", "inf", "--no-persist-query-text"),
        _policy("--use-daemon", "3", "nan", "--no-persist-query-text"),
        _policy("--no-daemon", "inf", "4", "--persist-query-text"),
        _policy("--use-daemon", "9.0", "4", "--no-persist-query-text"),
        _policy("--use-daemon", "3.004857197904855", "4", "--no-persist-query-text"),
    ],
    ids=[
        "no-settings",
        "ordinary",
        "no-daemon-persist",
        "extreme-daemon",
        "extreme-surfacing",
        "inf-daemon",
        "nan-daemon",
        "inf-surfacing",
        "trailing-zero",
        "rounding-surfacing",
    ],
)
def test_env_overrides_are_the_ones_the_hook_applies(
    monkeypatch: pytest.MonkeyPatch, policy: list[str]
) -> None:
    import shlex

    from memtomem_stm.cli.host_runtime import hook_command_words, hook_launcher

    command = shlex.join(hook_command_words(hook_launcher(), "claude", policy))
    owned = owned_hook_command(command, "claude")
    assert owned is not None, command

    assert owned.env_overrides() == _hook_overlay(monkeypatch, ["--host", "claude", *policy])


# ── what the hook command can carry (#1492) ──────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (3.0, True),
        (1e-7, True),
        (1e308, True),
        (5e-324, True),
        (float("inf"), False),
        (float("nan"), False),
        (1.7976931348623157e308, False),  # ``format_seconds`` rounds it to infinity
        (0.0, False),
        (-1.0, False),
    ],
)
def test_writable_seconds(value: float, expected: bool) -> None:
    from memtomem_stm.cli.host_runtime import writable_seconds

    assert writable_seconds(value) is expected
    # Same answer as the hook's reader on the written text.
    assert (_parse_positive_float(format_seconds(value)) is not None) is expected


@pytest.mark.parametrize(
    ("surfacing", "daemon", "field"),
    [
        (float("inf"), 3.5, "surfacing.timeout_seconds"),
        (float("nan"), 3.5, "surfacing.timeout_seconds"),
        (1.7976931348623157e308, 3.5, "surfacing.timeout_seconds"),
        (3.0, 1.7976931348623157e308, "hook.daemon_timeout_seconds"),
        (3.0, float("inf"), "hook.daemon_timeout_seconds"),
    ],
)
def test_hook_args_refuses_a_deadline_the_hook_would_drop(
    surfacing: float, daemon: float, field: str
) -> None:
    from memtomem_stm.cli.host_runtime import UnwritableDeadlineError

    policy = HostRuntimePolicy(
        use_daemon=True, surfacing_timeout_seconds=surfacing, daemon_timeout_seconds=daemon
    )

    with pytest.raises(UnwritableDeadlineError) as excinfo:
        policy.hook_args()

    assert excinfo.value.field == field


# --- the look-alike notice's word reader (#1498) -----------------------------


def _shlex_words(command: str) -> list[str]:
    """The reading the notice used before #1498, kept as the oracle: ``shlex``
    words, and on a parse error the words read before it, an empty word, then
    the ``str.split()`` words of the whole line."""
    import shlex

    lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    words: list[str] = []
    try:
        for word in lexer:
            words.append(word)
    except ValueError:
        return [*words, "", *command.split()]
    return words


_WORD_CORPUS = [
    "",
    " ",
    " \t\r\n ",
    "mms hook --host claude",
    'm"m"s hook --host claude # pinned',
    "m'm's hook",
    "m\\ms hook",
    "mms\\ hook",
    "a|b&&c;d<e>f",
    "a||b",
    "| & ; < >",
    "|'x'",
    "|\\x",
    "'single \\ literal'",
    '"double \\" quote"',
    '"double \\\\ escape"',
    '"double \\a keeps"',
    '"double \\$ keeps"',
    "'' ''",
    "''",
    '""',
    "a''b",
    "unbalanced 'single",
    'unbalanced "double',
    'unbalanced "double \\',
    "trailing \\",
    'hook m"m"s \'',
    'm"m"s\rho"ok"',
    'm"m"s\u00a0ho"ok"',
    'm"m"s \'\' ho"ok" # note',
    'echo done\nm"m"s hook',
    "café mms hook ünïcode",
    "\x0bmms\x0chook",
    "x" * 300 + ' "y z" ' + "|" * 7,
]


@pytest.mark.parametrize("command", _WORD_CORPUS)
def test_the_word_reader_reads_as_shlex_reads(command: str) -> None:
    from memtomem_stm.cli.host_runtime import _lenient_shell_tokens

    assert _lenient_shell_tokens(command) == _shlex_words(command)


def test_the_word_reader_reads_random_lines_as_shlex_reads() -> None:
    import random

    from memtomem_stm.cli.host_runtime import _lenient_shell_tokens

    rng = random.Random(1498)
    alphabet = [*"ab '\"\\|&;<>#\t\n\r", "mms", "hook", " ", "\u00a0"]
    for _ in range(5000):
        command = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 24)))
        assert _lenient_shell_tokens(command) == _shlex_words(command), repr(command)


@pytest.mark.parametrize(
    "command",
    [
        "echo " + "x" * 1_000_000,
        "echo " + "|" * 1_000_000,
        'echo "' + " " * 1_000_000 + '"',
        "echo " + "\\x" * 1_000_000,
        "echo " + "'a'" * 1_000_000,
        'echo "' + '\\"' * 1_000_000 + '"',
    ],
    ids=["plain", "punctuation", "quoted-space", "escapes", "quote-pairs", "quoted-escapes"],
)
def test_the_notice_reads_a_long_word_in_linear_time(command: str) -> None:
    """``shlex`` builds a word one character at a time by concatenation, so
    a long word in any handler made every install and uninstall plan slow."""
    import time

    from memtomem_stm.cli.hook_hosts import unmanaged_stm_like

    data = {"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": command}]}]}}

    started = time.perf_counter()
    assert unmanaged_stm_like("claude", data) == ()
    assert time.perf_counter() - started < 3.0
