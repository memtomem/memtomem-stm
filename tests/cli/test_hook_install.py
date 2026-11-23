"""Tests for ``mms hook install`` / ``mms hook uninstall`` — per-host hook-config
registration (Track B / B3).

Coverage:

- ``owned_hook_command`` / ``owned_handler`` — a handler is install's own only
  in the form install writes (text, fields and place); every line the writer
  produces is owned, and a line that differs in one respect is not.
- ``matcher_for`` — the registered tool matcher per host, derived from the
  adapter's ``native_tool_map`` (so it tracks what the live hook surfaces for).
- ``plan_install`` / ``plan_uninstall`` / ``apply_change`` — idempotent merge,
  symmetric uninstall, sibling-preserving, malformed-config refusal, backup.
- The CLI surface: dry-run default vs ``--apply``, ``--host`` validation, and
  that the group restructure left the bare ``mms hook`` runtime path intact.
"""

from __future__ import annotations

import json
import logging
import shlex
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import pytest
from click.testing import CliRunner
from helpers import set_home

from memtomem_stm.cli.hook_adapter import READLIKE_SURFACE_TOOLS, known_hosts
from memtomem_stm.cli.hook_cmd import _DEFAULT_SURFACE_TOOLS, _hook_install_command_argv
from memtomem_stm.cli.hook_hosts import (
    HOOK_HOSTS,
    HookInstallError,
    _config_path,
    apply_change,
    matcher_for,
    owned_handler,
    plan_install,
    plan_uninstall,
)
from memtomem_stm.cli.host_runtime import (
    HostRuntimePolicy,
    PartialHostRuntimePolicy,
    format_seconds,
    hook_launcher,
    owned_hook_command,
    runtime_env_overrides,
)
from memtomem_stm.cli.proxy import _launcher_for, _StmProject, cli

_CMD = "memtomem-stm hook --host {host}"
# An owned command from another launcher: what an earlier install from a source
# checkout left behind.
_OLD_UV = "uv run --directory /old/checkout memtomem-stm hook --host claude"


@pytest.fixture
def redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point a host's hook-config path at an isolated tmp file (per host, no
    cross-host filename collision) so install/uninstall never touch the real
    ``~/.claude`` / ``~/.codex`` / etc."""

    def _redirect(host_tag: str) -> Path:
        spec = HOOK_HOSTS[host_tag]
        path = tmp_path / host_tag / spec.config_path.name
        monkeypatch.setitem(HOOK_HOSTS, host_tag, replace(spec, config_path=path))
        return path

    return _redirect


@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The ``--apply`` span takes the hook-host write lock under
    ``~/.memtomem/`` (see ``hook_hosts_lock_path``); pin HOME so no test in
    this module touches the real one."""
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    for key in (
        "MEMTOMEM_STM_HOOK__USE_DAEMON",
        "MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS",
        "MEMTOMEM_STM_SURFACING__USE_DAEMON",
        "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS",
        "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT",
    ):
        monkeypatch.delenv(key, raising=False)
    return home


# ── ownership: the command text install writes ───────────────────────────────
#
# Install and uninstall manage a handler only when its command has the form
# ``mms hook install`` writes. The tests below pin that form from both sides:
# everything the writer produces is owned, and a line that differs from an
# owned one in a single respect is not.

_POLICY = (
    "--use-daemon --surfacing-timeout-seconds 3 --daemon-timeout-seconds 4 --no-persist-query-text"
)


def _owned(command: str, host: str = "claude") -> bool:
    return owned_hook_command(command, host) is not None


# Typed out, not generated: the lines released installers wrote, which must
# stay owned whatever the writer becomes.
_RELEASED_LINES = [
    # 0.1.29–0.1.38, and ``--inherit-runtime-env`` since.
    ("claude", "memtomem-stm hook --host claude"),
    ("codex", "uv run --directory /Users/me/src/memtomem-stm memtomem-stm hook --host codex"),
    # 0.1.40 onward: the six runtime-setting words.
    ("claude", f"memtomem-stm hook --host claude {_POLICY}"),
    (
        "kimi",
        "memtomem-stm hook --host kimi --no-daemon --surfacing-timeout-seconds 0.5 "
        "--daemon-timeout-seconds 6 --persist-query-text",
    ),
    (
        "cursor",
        f"uv run --directory 'C:\\Users\\me\\My Repo' memtomem-stm hook --host cursor {_POLICY}",
    ),
    # ``--surfacing-timeout inf`` / ``nan`` / a value that rounds past the
    # largest float were accepted and written as is (#1492).
    (
        "claude",
        "memtomem-stm hook --host claude --use-daemon --surfacing-timeout-seconds inf "
        "--daemon-timeout-seconds inf --no-persist-query-text",
    ),
    (
        "claude",
        "memtomem-stm hook --host claude --use-daemon --surfacing-timeout-seconds nan "
        "--daemon-timeout-seconds 3.5 --no-persist-query-text",
    ),
    (
        "claude",
        "memtomem-stm hook --host claude --use-daemon "
        "--surfacing-timeout-seconds 1.79769313486232e+308 "
        "--daemon-timeout-seconds 1.79769313486232e+308 --no-persist-query-text",
    ),
]


@pytest.mark.parametrize(("host", "command"), _RELEASED_LINES)
def test_a_line_a_released_install_wrote_is_owned(host: str, command: str) -> None:
    assert _owned(command, host) is True


# Pure paths: each root is spelled the same on every test platform. The native
# ``Path("/repo")`` is ``\\repo`` on Windows, which is drive-relative and so,
# rightly, not a directory install's own line can hold.
_ROOTS: list[PurePath | None] = [
    None,
    PurePosixPath("/repo"),
    PurePosixPath("/Users/x/My Repo"),
    PureWindowsPath("C:\\Users\\x\\repo"),
    PureWindowsPath("C:\\Users\\x\\it's $HOME %TEMP% (*) ~"),
    PureWindowsPath("\\\\server\\share\\repo"),
    # Every character a shell gives a meaning to, in install's own output
    # (``shlex.join`` single-quotes them).
    PurePosixPath("/Users/x/it's $HOME #1 (*) ~ {a} <b> `c` !"),
    PurePosixPath("/tmp/line\nbreak"),
    PurePosixPath("/Users/사용자/repo"),
    # The directory holds the words the reader searches for.
    PurePosixPath("/r/x memtomem-stm hook --host claude"),
    PurePosixPath("/r/''\"'\"'"),
]

_POLICIES = [
    None,
    HostRuntimePolicy(
        use_daemon=True,
        surfacing_timeout_seconds=3.0,
        daemon_timeout_seconds=5.0,
        persist_query_text=False,
    ),
    HostRuntimePolicy(
        use_daemon=False,
        surfacing_timeout_seconds=9.5,
        daemon_timeout_seconds=0.25,
        persist_query_text=True,
    ),
    HostRuntimePolicy(
        use_daemon=True,
        surfacing_timeout_seconds=1e-7,
        daemon_timeout_seconds=123456789.125,
    ),
]


def _written(host: str, root: PurePath | None, policy: HostRuntimePolicy | None) -> str:
    """Install's own output, built the way ``mms hook install`` builds it."""
    # ``_launcher_for`` only spells the root out, so a pure path stands in.
    project = None if root is None else _StmProject(root=root, source_checkout=True)
    return shlex.join(
        _hook_install_command_argv(host, policy=policy, launcher=_launcher_for(project))
    )


@pytest.mark.parametrize("host", sorted(HOOK_HOSTS))
@pytest.mark.parametrize("root", _ROOTS)
@pytest.mark.parametrize("policy", _POLICIES)
def test_install_owns_every_command_it_writes(
    host: str, root: PurePath | None, policy: HostRuntimePolicy | None
) -> None:
    """Writer ⊆ owned, and the settings read back are the ones written."""
    owned = owned_hook_command(_written(host, root, policy), host)

    assert owned is not None
    assert owned.launcher == tuple(hook_launcher(None if root is None else str(root)))
    if policy is None:
        assert owned.settings == PartialHostRuntimePolicy()
    else:
        written = policy.normalized()
        assert owned.settings == PartialHostRuntimePolicy(
            use_daemon=written.use_daemon,
            surfacing_timeout_seconds=written.surfacing_timeout_seconds,
            daemon_timeout_seconds=written.daemon_timeout_seconds,
            persist_query_text=written.persist_query_text,
        )


@pytest.mark.parametrize("surfacing", [float("inf"), float("nan"), 1.7976931348623157e308])
def test_install_owns_a_command_written_with_a_non_finite_deadline(surfacing: float) -> None:
    """The writer does not refuse these (#1492), so the reader must not
    either; the unusable deadline itself is not carried over."""
    policy = HostRuntimePolicy(
        use_daemon=True, surfacing_timeout_seconds=surfacing, daemon_timeout_seconds=3.5
    )

    owned = owned_hook_command(_written("claude", None, policy), "claude")

    assert owned is not None
    assert owned.settings.surfacing_timeout_seconds is None
    assert owned.settings.use_daemon is True


_UV = "uv run --directory /repo memtomem-stm hook --host claude"

# Each line differs from an owned one in the respect its comment names.
_NOT_OWNED = [
    # ── the entry-point word ──
    "mms hook --host claude",  # another entry-point name (the documented manual line)
    "memtomem-stm-proxy hook --host claude",
    "/usr/local/bin/memtomem-stm hook --host claude",  # a path to it
    '"memtomem-stm" hook --host claude',  # quotes ``shlex.join`` would not add (#1483)
    "'memtomem-stm' hook --host claude",
    "uv run --directory /repo mms hook --host claude",
    "uv run --directory /repo /repo/.venv/bin/memtomem-stm hook --host claude",
    # ── ``hook --host <this host>`` ──
    "memtomem-stm hook --host codex",  # another host's line
    "memtomem-stm hook",
    "memtomem-stm hook --host",
    "memtomem-stm hook --host=claude",
    "memtomem-stm status",
    "memtomem-stm",
    # ── spacing ──
    "memtomem-stm  hook --host claude",
    "memtomem-stm hook --host claude ",
    " memtomem-stm hook --host claude",
    "memtomem-stm hook --host claude\n",
    "memtomem-stm\thook --host claude",
    # ── the launcher ──
    "uv run memtomem-stm hook --host claude",
    "uv run --directory /repo --frozen memtomem-stm hook --host claude",
    "uv  run --directory /repo memtomem-stm hook --host claude",
    "uv run --help memtomem-stm hook --host claude",
    "env MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS=12 memtomem-stm hook --host claude",
    "/usr/bin/env MEMTOMEM_STM_HOOK__USE_DAEMON=true memtomem-stm hook --host claude",
    "MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 memtomem-stm hook --host claude",  # #1453
    "FOO=1 memtomem-stm hook --host claude",
    # ── the launcher's directory ──
    "uv run --directory project memtomem-stm hook --host claude",  # relative
    "uv run --directory @project memtomem-stm hook --host claude",  # PowerShell splats it
    "uv run --directory C:rel memtomem-stm hook --host claude",  # drive-relative
    "uv run --directory '' memtomem-stm hook --host claude",
    "uv run --directory -x memtomem-stm hook --host claude",  # an option, not a directory
    "uv run --directory ~ memtomem-stm hook --host claude",
    "uv run --directory ~/repo memtomem-stm hook --host claude",
    'uv run --directory "/a b" memtomem-stm hook --host claude',  # not ``shlex.join``'s quotes
    "uv run --directory /a b memtomem-stm hook --host claude",
    "uv run --directory '/repo' memtomem-stm hook --host claude",  # quotes it would not add
    "uv run --directory '/a\x00b' memtomem-stm hook --host claude",
    "uv run --directory memtomem-stm hook --host claude",  # the directory word is missing
    # ── the setting words ──
    "memtomem-stm hook --host claude --use-daemon",
    f"memtomem-stm hook --host claude {_POLICY} --verbose",
    "memtomem-stm hook --host claude --no-persist-query-text --use-daemon "
    "--surfacing-timeout-seconds 3 --daemon-timeout-seconds 4",  # another order
    "memtomem-stm hook --host claude --use-daemon --surfacing-timeout-seconds=3 "
    "--daemon-timeout-seconds=4 --no-persist-query-text",
    "memtomem-stm hook --host claude --use-daemon --surfacing-timeout-seconds 3 "
    "--no-persist-query-text",
    "memtomem-stm hook --host claude --use-daemon --surfacing-timeout-seconds -1 "
    "--daemon-timeout-seconds 4 --no-persist-query-text",
    "memtomem-stm hook --host claude --use-daemon --surfacing-timeout-seconds --e "
    "--daemon-timeout-seconds 4 --no-persist-query-text",
    "memtomem-stm hook --host claude --use-daemon --surfacing-timeout-seconds '3' "
    "--daemon-timeout-seconds 4 --no-persist-query-text",
    "memtomem-stm hook --host claude --use-daemon --surfacing-timeout-seconds 1_0 "
    "--daemon-timeout-seconds 4 --no-persist-query-text",
    *(
        f"memtomem-stm hook --host claude --use-daemon --surfacing-timeout-seconds {malformed} "
        "--daemon-timeout-seconds 4 --no-persist-query-text"
        # Not something ``format_seconds`` prints for any deadline.
        for malformed in ("9e", "1..2", "1.", ".5", "1e+", "e5", "1e5.5", "infinity", "0x10")
    ),
    "memtomem-stm hook --host claude --use-daemon --surfacing-timeout-seconds $T "
    "--daemon-timeout-seconds 4 --no-persist-query-text",
    # ── anything after the command ──
    "memtomem-stm hook --host claude # pinned",
    "memtomem-stm hook --host claude && echo done",
    "memtomem-stm hook --host claude | cat",
    "memtomem-stm hook --host claude; memtomem-stm hook --host claude",
    "memtomem-stm hook --host claude uninstall",
    "memtomem-stm hook --host claude --help",
    # ── not STM at all ──
    "echo hook",
    "ruff check hooks.py",
    "echo hook && memtomem-stm status",
    "",
]


@pytest.mark.parametrize("command", _NOT_OWNED)
def test_a_line_install_does_not_write_is_not_owned(command: str) -> None:
    assert _owned(command) is False


@pytest.mark.parametrize("command", ["memtomem-stm hook --host claude", _UV])
def test_the_lines_the_not_owned_corpus_varies_are_owned(command: str) -> None:
    """Positive control for the corpus above: the two lines it is built from."""
    assert _owned(command) is True
    assert _owned(f"{command} {_POLICY}") is True


@pytest.mark.parametrize(
    ("surfacing", "daemon", "recovered"),
    [
        ("3", "4", (3.0, 4.0)),
        # Not the writer's spelling of a number, but a number: the slot is
        # read as text, since releases wrote ``inf`` and ``nan`` there.
        ("9.0", "10", (9.0, 10.0)),
        ("1e-07", "1e+16", (1e-07, 1e16)),
        # The hook raises the daemon deadline above a usable surfacing one,
        # so a pair edited by hand is carried over as the hook runs it.
        ("9", "4", (9.0, 10.0)),
        ("9", "nan", (9.0, 10.0)),
        ("0", "4", (None, 4.0)),
        ("inf", "inf", (None, None)),
    ],
)
def test_recovered_deadlines_are_the_ones_the_hook_runs_with(
    surfacing: str, daemon: str, recovered: tuple[float | None, float | None]
) -> None:
    command = (
        f"memtomem-stm hook --host claude --no-daemon --surfacing-timeout-seconds {surfacing} "
        f"--daemon-timeout-seconds {daemon} --persist-query-text"
    )

    owned = owned_hook_command(command, "claude")

    assert owned is not None
    settings = owned.settings
    assert (settings.surfacing_timeout_seconds, settings.daemon_timeout_seconds) == recovered
    assert (settings.use_daemon, settings.persist_query_text) == (False, True)
    # The reference: the overlay ``mms hook`` applies to the same flags.
    overlay = runtime_env_overrides(
        use_daemon="false",
        surfacing_timeout_seconds=surfacing,
        daemon_timeout_seconds=daemon,
        persist_query_text="true",
    )
    assert overlay.get("MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS") == (
        None if recovered[0] is None else format_seconds(recovered[0])
    )
    assert overlay.get("MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS") == (
        None if recovered[1] is None else format_seconds(recovered[1])
    )


@pytest.mark.parametrize(
    "line",
    [
        "uv run --directory /" + "x" * 200_000 + " memtomem-stm hook --host claude",  # owned
        "uv run --directory '/" + "x" * 200_000 + " y' memtomem-stm hook --host claude",  # owned
        "echo " + "x" * 200_000,
        "memtomem-stm hook --host claude " + "x" * 200_000,
        " hook --host claude" * 10_000,
    ],
)
def test_ownership_never_splits_the_line(line: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """``shlex`` is quadratic in a word's length; the reader re-joins words it
    finds by position and never tokenizes."""
    from memtomem_stm.cli import host_runtime

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("ownership tokenized the line")

    monkeypatch.setattr(host_runtime.shlex, "shlex", refuse)
    monkeypatch.setattr(host_runtime.shlex, "split", refuse)

    assert _owned(line) is line.startswith("uv run")


# ── ownership: the handler's place and fields ────────────────────────────────

_CLAUDE = "memtomem-stm hook --host claude"


@pytest.mark.parametrize(
    ("host", "handler", "owned"),
    [
        ("claude", {"type": "command", "command": _CLAUDE}, True),
        ("codex", {"type": "command", "command": "memtomem-stm hook --host codex"}, True),
        # Fields a host supports on a handler do not change whose it is.
        (
            "claude",
            {"type": "command", "command": _CLAUDE, "timeout": 30, "statusMessage": "x"},
            True,
        ),
        ("claude", {"type": "command", "command": _CLAUDE, "shell": "bash"}, True),
        # Install writes ``type`` and never ``args``.
        ("claude", {"command": _CLAUDE}, False),
        ("claude", {"type": "prompt", "command": _CLAUDE}, False),
        ("claude", {"type": "command", "command": _CLAUDE, "args": []}, False),
        (
            "codex",
            {"type": "command", "command": "memtomem-stm hook --host codex", "args": []},
            False,
        ),
        # Cursor entries are a command and nothing else.
        ("cursor", {"command": "memtomem-stm hook --host cursor"}, True),
        ("cursor", {"command": "memtomem-stm hook --host cursor", "type": "x"}, True),
        # Kimi: the event install registers, and no other (#1461).
        ("kimi", {"event": "PostToolUse", "command": "memtomem-stm hook --host kimi"}, True),
        ("kimi", {"event": "PreToolUse", "command": "memtomem-stm hook --host kimi"}, False),
        (
            "kimi",
            {"event": "PostToolUseFailure", "command": "memtomem-stm hook --host kimi"},
            False,
        ),
        ("kimi", {"command": "memtomem-stm hook --host kimi"}, False),
        # Not a handler, or no command to read.
        ("claude", "memtomem-stm hook --host claude", False),
        ("claude", {"type": "command"}, False),
        ("claude", {"type": "command", "command": ["memtomem-stm", "hook"]}, False),
    ],
)
def test_owned_handler_reads_place_and_fields(host: str, handler: object, owned: bool) -> None:
    assert (owned_handler(host, handler) is not None) is owned


# ── matcher_for + cross-module consistency ───────────────────────────────────


def test_matcher_for_per_host() -> None:
    # Derived from each adapter's native_tool_map ∩ READLIKE_SURFACE_TOOLS,
    # formatted per host: alternation (Claude/Kimi), anchored regex (Codex),
    # none (Cursor's generic post-tool hook).
    assert matcher_for("claude") == "Read|Grep|Glob|Bash"
    assert matcher_for("kimi") == "Bash|Read|Grep|Glob|Shell|ReadFile"
    assert matcher_for("codex") == "^Bash$"
    assert matcher_for("cursor") is None


def test_install_matcher_source_is_the_surface_allowlist() -> None:
    # The install matcher is derived from the SAME set the live hook surfaces for,
    # so a registered matcher can never advertise a tool the hook ignores.
    assert _DEFAULT_SURFACE_TOOLS is READLIKE_SURFACE_TOOLS


def test_every_known_host_has_an_install_spec() -> None:
    # ``mms hook install --host`` offers exactly the adapter registry's hosts;
    # each must have a HookHostSpec or the choice would resolve to a KeyError.
    assert set(known_hosts()) == set(HOOK_HOSTS)


# ── plan_install: create / update / already (idempotent) ─────────────────────


@pytest.mark.parametrize("host", ["claude", "codex", "cursor", "kimi"])
def test_plan_install_create_then_idempotent(host: str, redirect) -> None:
    redirect(host)
    command = _CMD.format(host=host)

    created = plan_install(host, command)
    assert created.status == "create"
    assert created.changed is True
    assert created.current_text is None
    # The serialized result parses and carries our command.
    parsed = _parse(created.new_text, created.fmt)
    assert command in created.new_text
    assert _config_has_stm_command(parsed, host)
    if host != "cursor":
        assert (matcher_for(host) or "") in created.new_text

    # Write it, then re-plan: an identical block is recognized → no change.
    apply_change(created)
    again = plan_install(host, command)
    assert again.status == "already"
    assert again.changed is False


def test_plan_install_update_replaces_in_place(redirect) -> None:
    path = redirect("claude")
    # Seed an owned block from another launcher (an old source checkout).
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Read",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": _OLD_UV,
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    change = plan_install("claude", "memtomem-stm hook --host claude")
    assert change.status == "update"
    parsed = json.loads(change.new_text)
    entries = parsed["hooks"]["PostToolUse"]
    # Replaced in place — exactly one STM matcher-group, not appended/duplicated.
    assert len(entries) == 1
    assert entries[0]["matcher"] == "Read|Grep|Glob|Bash"


def test_plan_install_refresh_preserves_unknown_host_fields(redirect) -> None:
    path = redirect("claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Read",
                            "customGroupField": {"keep": True},
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": _OLD_UV,
                                    "timeout": 30,
                                    "statusMessage": "Searching memory",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    change = plan_install("claude", "memtomem-stm hook --host claude --use-daemon")
    entry = json.loads(change.new_text)["hooks"]["PostToolUse"][0]
    assert entry["customGroupField"] == {"keep": True}
    handler = entry["hooks"][0]
    assert handler["timeout"] == 30
    assert handler["statusMessage"] == "Searching memory"
    assert handler["command"].endswith("--use-daemon")


def test_plan_install_preserves_sibling_content(redirect) -> None:
    path = redirect("claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    change = plan_install("claude", "memtomem-stm hook --host claude")
    parsed = json.loads(change.new_text)
    assert parsed["model"] == "opus"  # unrelated top-level key untouched
    assert parsed["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "guard.sh"  # other event kept
    assert _config_has_stm_command(parsed, "claude")


def test_plan_install_preserves_cohandler_in_stm_matcher_group(redirect) -> None:
    # A user who manually co-located their own handler inside STM's matcher-group
    # entry must not lose it on re-install (#529). The STM handler is swapped in
    # place (command refreshed), siblings + the shared matcher are preserved —
    # symmetric with uninstall, which already filters rather than replaces.
    path = redirect("claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Read",  # user's own matcher, not STM's derived one
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": _OLD_UV,
                                },
                                {"type": "command", "command": "my-guard.sh"},
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    change = plan_install("claude", "memtomem-stm hook --host claude")
    entries = json.loads(change.new_text)["hooks"]["PostToolUse"]
    assert len(entries) == 1  # still a single matcher group — STM swapped in place
    entry = entries[0]
    assert entry["matcher"] == "Read"  # shared matcher kept (changing it would move guard.sh)
    commands = [h["command"] for h in entry["hooks"]]
    assert "my-guard.sh" in commands  # the co-located sibling survives
    assert sum(_owned(c) for c in commands) == 1  # exactly one STM handler
    assert "memtomem-stm hook --host claude" in commands  # refreshed to the new spelling


def test_plan_install_dedups_multiple_stm_groups(redirect) -> None:
    # A config that somehow holds *two* owned groups (a hand-edit, or an old
    # install) must converge to a single STM handler on re-install, honoring the
    # "never duplicated" contract — and never lose a co-located sibling (#529,
    # codex follow-up). Install scans all PostToolUse entries, like uninstall.
    path = redirect("claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Read",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": _OLD_UV,
                                },
                                {"type": "command", "command": "my-guard.sh"},
                            ],
                        },
                        {
                            "matcher": "Read|Grep|Glob|Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": _OLD_UV,
                                }
                            ],
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    apply_change(plan_install("claude", "memtomem-stm hook --host claude"))
    entries = json.loads(path.read_text())["hooks"]["PostToolUse"]
    commands = [h["command"] for e in entries for h in e["hooks"]]
    assert sum(_owned(c) for c in commands) == 1  # one STM handler total
    assert "my-guard.sh" in commands  # co-located sibling kept
    # Idempotent now — the lingering duplicate doesn't make re-install a no-op
    # while STM still fires twice.
    assert plan_install("claude", "memtomem-stm hook --host claude").status == "already"


# ── plan_uninstall: symmetric, sibling-preserving, cleans empties ────────────


def test_uninstall_removes_only_stm_and_prunes_empties(redirect) -> None:
    redirect("kimi")
    plan = plan_install("kimi", "memtomem-stm hook --host kimi")
    apply_change(plan)

    change = plan_uninstall("kimi")
    assert change.status == "remove"
    assert change.changed is True
    # Last hook removed → the empty ``[[hooks]]`` array is pruned entirely.
    assert tomllib.loads(change.new_text) == {}


def test_uninstall_keeps_foreign_hooks(redirect) -> None:
    path = redirect("kimi")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'model = "kimi-k2"\n\n'
        "[[hooks]]\n"
        'event = "PostToolUse"\n'
        'command = "memtomem-stm hook --host kimi"\n\n'
        "[[hooks]]\n"
        'event = "PostToolUse"\n'
        'command = "my-own-hook.sh"\n',
        encoding="utf-8",
    )
    change = plan_uninstall("kimi")
    parsed = tomllib.loads(change.new_text)
    assert parsed["model"] == "kimi-k2"  # sibling key kept
    commands = [h["command"] for h in parsed["hooks"]]
    assert commands == ["my-own-hook.sh"]  # only the foreign hook survives


def test_uninstall_not_installed_and_absent(redirect) -> None:
    path = redirect("codex")
    # Absent file → nothing to remove.
    assert plan_uninstall("codex").status == "absent"
    # Present file with no STM block → not_installed (no change).
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('model = "gpt-5"\n', encoding="utf-8")
    nochange = plan_uninstall("codex")
    assert nochange.status == "not_installed"
    assert nochange.changed is False


# ── malformed config is refused, never clobbered ─────────────────────────────


def test_plan_install_refuses_malformed_config(redirect) -> None:
    path = redirect("claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(HookInstallError, match="not valid JSON"):
        plan_install("claude", "memtomem-stm hook --host claude")


@pytest.mark.parametrize("host", ["claude", "codex"])
@pytest.mark.parametrize(
    "prefix", [b"", b"# \xea\xb0\x80 sk-live-hook-canary\n"], ids=["bare", "prefix"]
)
def test_plan_install_refuses_non_utf8_config_without_its_bytes(redirect, host, prefix) -> None:
    path = redirect(host)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(prefix + b"\xff\xfe")
    with pytest.raises(HookInstallError) as exc_info:
        plan_install(host, f"memtomem-stm hook --host {host}")
    message = str(exc_info.value)
    assert f"is not valid UTF-8 (byte offset {len(prefix)})" in message
    assert "canary" not in message and "0xff" not in message


def test_plan_install_refuses_non_object_toplevel(redirect) -> None:
    path = redirect("claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")  # valid JSON, wrong shape
    with pytest.raises(HookInstallError, match="not a top-level object"):
        plan_install("claude", "memtomem-stm hook --host claude")


@pytest.mark.parametrize(
    ("host", "env_name"),
    [
        ("claude", "CLAUDE_CONFIG_DIR"),
        ("codex", "CODEX_HOME"),
        ("kimi", "KIMI_CODE_HOME"),
    ],
)
def test_config_path_respects_host_home_override(
    host: str, env_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / host
    monkeypatch.setenv(env_name, str(home))
    assert _config_path(HOOK_HOSTS[host]).parent == home


def test_empty_codex_home_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Codex reads ~/.codex for an empty CODEX_HOME (measured on 0.167.2).
    monkeypatch.setenv("CODEX_HOME", "")
    assert _config_path(HOOK_HOSTS["codex"]) == HOOK_HOSTS["codex"].config_path.expanduser()


@pytest.mark.parametrize(
    ("host", "env_name", "value"),
    [
        ("claude", "CLAUDE_CONFIG_DIR", ""),
        ("claude", "CLAUDE_CONFIG_DIR", "rel"),
        ("codex", "CODEX_HOME", "rel"),
        ("kimi", "KIMI_CODE_HOME", ""),
        ("kimi", "KIMI_CODE_HOME", "rel"),
    ],
)
def test_config_path_refuses_a_home_it_cannot_place(
    host: str, env_name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A relative value names a file under the current directory, which a
    # repository checkout controls (#1345); STM refuses rather than guess.
    monkeypatch.setenv(env_name, value)
    with pytest.raises(HookInstallError, match=f"{env_name} is ") as excinfo:
        _config_path(HOOK_HOSTS[host])
    if value:
        assert f"'{value}'" not in str(excinfo.value)


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_cli_refuses_an_empty_kimi_code_home_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    # Kimi Code reads <its cwd>/config.toml when KIMI_CODE_HOME is empty, so
    # neither that file nor the default one is the right file to edit.
    home = Path.home()
    default = home / ".kimi-code" / "config.toml"
    default.parent.mkdir(parents=True)
    installed = plan_install("kimi", _CMD.format(host="kimi")).new_text
    cwd = tmp_path / "repo"
    cwd.mkdir()
    in_cwd = cwd / "config.toml"
    prior = installed if action == "uninstall" else 'model = "x"\n'
    default.write_text(prior, encoding="utf-8")
    in_cwd.write_text(prior, encoding="utf-8")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("KIMI_CODE_HOME", "")

    result = CliRunner().invoke(cli, ["hook", action, "--host", "kimi", "--apply"])

    assert result.exit_code == 1, result.output
    assert "KIMI_CODE_HOME is empty, relative" in result.output
    assert default.read_text(encoding="utf-8") == prior
    assert in_cwd.read_text(encoding="utf-8") == prior
    assert sorted(p.name for p in cwd.iterdir()) == ["config.toml"]
    assert sorted(p.name for p in default.parent.iterdir()) == ["config.toml"]


@pytest.mark.parametrize(
    "payload",
    [{"hooks": "not-an-object"}, {"hooks": {"PostToolUse": "not-a-list"}}],
)
def test_plan_install_refuses_wrong_typed_hook_containers(
    redirect, payload: dict[str, object]
) -> None:
    path = redirect("claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HookInstallError, match="expected"):
        plan_install("claude", "memtomem-stm hook --host claude")


# ── apply_change: write + backup + no-op ─────────────────────────────────────


def test_apply_change_backs_up_prior_file_verbatim(redirect) -> None:
    path = redirect("cursor")
    path.parent.mkdir(parents=True, exist_ok=True)
    prior = '{"version": 1, "hooks": {"postToolUse": [{"command": "other.sh"}]}}'
    path.write_text(prior, encoding="utf-8")

    change = plan_install("cursor", "memtomem-stm hook --host cursor")
    backup = apply_change(change)

    assert backup == path.parent / (path.name + ".bak")
    assert backup.read_text(encoding="utf-8") == prior  # byte-identical prior
    # New file carries both the foreign and the STM hook.
    parsed = json.loads(path.read_text(encoding="utf-8"))
    commands = [e["command"] for e in parsed["hooks"]["postToolUse"]]
    assert "other.sh" in commands
    assert any(_owned(c, "cursor") for c in commands)


def test_apply_change_second_apply_does_not_clobber_first_backup(redirect) -> None:
    # A second --apply (uninstall here) must not overwrite the backup the first
    # apply made (#529). For a TOML host that first .bak is the only
    # comment-preserving copy of the original — re-serialization drops comments —
    # so clobbering it would destroy exactly the safety net. The second backup
    # goes to .bak.1 instead; the original stays at .bak.
    path = redirect("codex")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = 'model = "gpt-5"  # keep this comment\n'
    path.write_text(original, encoding="utf-8")

    first = apply_change(plan_install("codex", "memtomem-stm hook --host codex"))
    assert first == path.parent / (path.name + ".bak")
    assert first.read_text(encoding="utf-8") == original  # verbatim original, comment intact

    second = apply_change(plan_uninstall("codex"))
    assert second == path.parent / (path.name + ".bak.1")  # numbered slot, not .bak
    # The original comment-preserving backup is untouched by the second apply.
    assert first.read_text(encoding="utf-8") == original
    assert "# keep this comment" not in second.read_text(encoding="utf-8")  # re-serialized copy


def test_write_backup_advances_past_existing_slots(tmp_path: Path) -> None:
    # _write_backup claims the first free slot with O_EXCL and never touches an
    # occupied one, so pre-existing backups are preserved and the new one lands
    # in the next slot (#529). This is the per-slot proof behind the two-apply
    # test above; it also exercises the collision-advance loop directly.
    from memtomem_stm.cli.hook_hosts import _write_backup

    cfg = tmp_path / "config.toml"
    cfg.write_text("current\n", encoding="utf-8")
    (tmp_path / "config.toml.bak").write_text("original\n", encoding="utf-8")
    (tmp_path / "config.toml.bak.1").write_text("second\n", encoding="utf-8")

    dest = _write_backup(cfg, "third\n", 0o600)

    assert dest == tmp_path / "config.toml.bak.2"
    assert dest.read_text(encoding="utf-8") == "third\n"
    # The two occupied slots are untouched.
    assert (tmp_path / "config.toml.bak").read_text(encoding="utf-8") == "original\n"
    assert (tmp_path / "config.toml.bak.1").read_text(encoding="utf-8") == "second\n"


def test_apply_change_no_backup_when_creating(redirect) -> None:
    path = redirect("kimi")
    change = plan_install("kimi", "memtomem-stm hook --host kimi")
    backup = apply_change(change)
    assert backup is None  # nothing existed to back up
    assert path.exists()


def test_apply_change_noop_when_unchanged(redirect) -> None:
    redirect("kimi")
    apply_change(plan_install("kimi", "memtomem-stm hook --host kimi"))
    again = plan_install("kimi", "memtomem-stm hook --host kimi")
    assert again.changed is False
    assert apply_change(again) is None  # idempotent re-apply writes nothing


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_apply_change_new_file_is_0600(redirect) -> None:
    path = redirect("codex")  # config.toml may carry API keys → conservative mode
    apply_change(plan_install("codex", "memtomem-stm hook --host codex"))
    assert (path.stat().st_mode & 0o777) == 0o600


# ── CLI surface ──────────────────────────────────────────────────────────────


def test_cli_install_dry_run_does_not_write(redirect) -> None:
    path = redirect("claude")
    result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude"])
    assert result.exit_code == 0
    assert "To apply" in result.output
    assert not path.exists()  # dry-run is the default — nothing written


def test_cli_install_apply_then_uninstall(redirect) -> None:
    path = redirect("claude")
    runner = CliRunner()

    res = runner.invoke(cli, ["hook", "install", "--host", "claude", "--apply"])
    assert res.exit_code == 0
    assert "Installed" in res.output
    assert _config_has_stm_command(json.loads(path.read_text()), "claude")

    # Idempotent.
    res2 = runner.invoke(cli, ["hook", "install", "--host", "claude", "--apply"])
    assert "already installed" in res2.output

    res3 = runner.invoke(cli, ["hook", "uninstall", "--host", "claude", "--apply"])
    assert res3.exit_code == 0
    assert "Removed" in res3.output
    assert json.loads(path.read_text()) == {}


def test_cli_install_serializes_managed_runtime_policy(redirect) -> None:
    path = redirect("claude")
    result = CliRunner().invoke(
        cli,
        [
            "hook",
            "install",
            "--host",
            "claude",
            "--surfacing-timeout",
            "12",
            "--apply",
        ],
    )
    assert result.exit_code == 0, result.output
    command = _claude_commands(path)[0]
    assert " --use-daemon " in command
    assert "--surfacing-timeout-seconds 12" in command
    assert "--daemon-timeout-seconds 13" in command
    assert command.endswith("--no-persist-query-text")
    assert not command.startswith("env ")
    assert path.exists()


def test_cli_install_leaves_an_env_prefixed_line_and_reads_no_setting_from_it(redirect) -> None:
    """No release of install wrote an ``env`` prefix, so such a line is its
    author's. #1484: install used to turn the daemon off for the replacement
    because of ``SURFACING__USE_DAEMON``, a variable the hook does not read."""
    path = redirect("claude")
    prefixed = (
        "env MEMTOMEM_STM_SURFACING__USE_DAEMON=false "
        "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS=12 memtomem-stm hook --host claude"
    )
    _seed(path, _claude_groups([_h(prefixed, timeout=30)]))

    result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude", "--apply"])

    assert result.exit_code == 0, result.output
    assert "(in the written file: hooks.PostToolUse[0].hooks[0])" in result.output
    commands = _claude_commands(path)
    assert commands[0] == prefixed and len(commands) == 2
    written = shlex.split(commands[1])
    assert "--use-daemon" in written and "--no-daemon" not in written
    assert written[written.index("--surfacing-timeout-seconds") + 1] != "12"
    kept = json.loads(path.read_text())["hooks"]["PostToolUse"][0]["hooks"][0]
    assert kept == _h(prefixed, timeout=30)


@pytest.mark.parametrize(
    "assignment",
    [
        # #1484: the proxy's variable, which the hook does not read.
        "MEMTOMEM_STM_SURFACING__USE_DAEMON=false",
        # The hook's own variable: it takes effect for the hand-written line,
        # which stays as it is, and is not copied into install's handler.
        "MEMTOMEM_STM_HOOK__USE_DAEMON=false",
    ],
)
def test_cli_install_reads_no_daemon_mode_from_an_env_prefix(assignment: str, redirect) -> None:
    path = redirect("claude")
    prefixed = f"env {assignment} memtomem-stm hook --host claude"
    _seed(path, _claude_groups([_h(prefixed)]))

    result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude", "--apply"])

    assert result.exit_code == 0, result.output
    kept, added = _claude_commands(path)
    assert kept == prefixed
    words = shlex.split(added)
    assert {"--use-daemon", "--no-daemon"} & set(words) == {"--use-daemon"}


def test_cli_install_leaves_a_commented_line_and_reads_no_setting_from_it(redirect) -> None:
    """A commented line is not taken over (#1460), so nothing written in its
    comment reaches the installed command's runtime settings."""
    path = redirect("claude")
    commented = "mms hook --host claude # --persist-query-text --surfacing-timeout-seconds 99"
    _seed(path, _claude_groups([_h(commented)]))

    result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude", "--apply"])

    assert result.exit_code == 0, result.output
    assert "(in the written file: hooks.PostToolUse[0].hooks[0])" in result.output
    commands = _claude_commands(path)
    assert commands[0] == commented and len(commands) == 2
    written = shlex.split(commands[1])
    assert "--persist-query-text" not in written
    assert written[written.index("--surfacing-timeout-seconds") + 1] != "99"


def test_cli_install_can_inherit_runtime_environment(redirect) -> None:
    path = redirect("cursor")
    result = CliRunner().invoke(
        cli,
        ["hook", "install", "--host", "cursor", "--inherit-runtime-env", "--apply"],
    )
    assert result.exit_code == 0, result.output
    command = json.loads(path.read_text())["hooks"]["postToolUse"][0]["command"]
    assert command.endswith("hook --host cursor")
    assert "timeout-seconds" not in command
    assert "persist-query-text" not in command


def test_cli_install_rejects_conflicting_runtime_options(redirect) -> None:
    redirect("claude")
    result = CliRunner().invoke(
        cli,
        [
            "hook",
            "install",
            "--host",
            "claude",
            "--inherit-runtime-env",
            "--no-daemon",
        ],
    )
    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_cli_install_toml_host_warns_about_rewrite(redirect) -> None:
    redirect("codex")
    result = CliRunner().invoke(cli, ["hook", "install", "--host", "codex"])
    assert result.exit_code == 0
    assert "comments/formatting are not preserved" in result.output
    assert "/hooks" in result.output  # Codex trust-step note surfaced


def test_cli_install_malformed_config_exits_nonzero(redirect) -> None:
    path = redirect("claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{bad", encoding="utf-8")
    result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude", "--apply"])
    assert result.exit_code != 0
    assert "not valid JSON" in result.output
    assert path.read_text(encoding="utf-8") == "{bad"  # untouched


def test_cli_install_non_utf8_config_exits_cleanly_and_untouched(redirect) -> None:
    path = redirect("codex")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe")
    result = CliRunner().invoke(cli, ["hook", "install", "--host", "codex", "--apply"])
    # CliRunner turns an uncaught exception into exit 1 too; only SystemExit
    # proves the error was rendered.
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1
    assert "is not valid UTF-8 (byte offset 0)" in result.output
    assert path.read_bytes() == b"\xff\xfe"


def test_cli_install_ignores_a_non_utf8_pyproject_in_cwd(redirect, tmp_path, monkeypatch) -> None:
    # Install-type detection walks up from cwd reading pyproject.toml; an
    # unreadable one means "not an STM project", like a malformed one.
    redirect("claude")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_bytes(b"\xff\xfe")
    monkeypatch.chdir(project)
    result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude"])
    assert result.exit_code == 0, result.exception


def test_cli_install_rejects_unknown_and_missing_host() -> None:
    runner = CliRunner()
    assert runner.invoke(cli, ["hook", "install", "--host", "bogus"]).exit_code == 2
    assert runner.invoke(cli, ["hook", "install"]).exit_code == 2  # --host is required


def test_cli_bare_hook_runtime_path_intact() -> None:
    # The group restructure must not disturb the bare ``mms hook`` payload path:
    # a malformed payload still passes through as ``{}`` at exit 0.
    result = CliRunner().invoke(cli, ["hook"], input="not json")
    assert result.exit_code == 0
    assert result.output.strip() == "{}"


# ── staleness guard + write lock (lost-update protection) ───────────────────


def test_apply_change_refuses_when_file_changed_after_plan(redirect) -> None:
    # The host application rewriting its own config between plan and apply is
    # the one writer mms' advisory lock cannot serialize; without the guard the
    # atomic replace writes the plan-time merge back wholesale and silently
    # reverts the app's edit (e.g. a permission Claude Code just persisted).
    path = redirect("claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"hooks": {}}\n', encoding="utf-8")

    change = plan_install("claude", _CMD.format(host="claude"))
    concurrent = '{"permissions": {"allow": ["Bash(ls:*)"]}}\n'
    path.write_text(concurrent, encoding="utf-8")  # app writes concurrently

    with pytest.raises(HookInstallError, match="changed after this change was planned"):
        apply_change(change)

    assert path.read_text(encoding="utf-8") == concurrent  # not clobbered
    # Aborted BEFORE the backup — a refused apply leaves no side effect.
    assert not (path.parent / (path.name + ".bak")).exists()


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_apply_change_refuses_when_file_turned_non_utf8_after_plan(redirect, action) -> None:
    path = redirect("claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"hooks": {}}\n', encoding="utf-8")
    install = plan_install("claude", _CMD.format(host="claude"))
    if action == "uninstall":
        apply_change(install)
    change = install if action == "install" else plan_uninstall("claude")
    assert change.changed
    backups_before = sorted(path.parent.iterdir())
    concurrent = b'{"x": 1}\n\xff\xfe'
    path.write_bytes(concurrent)

    with pytest.raises(HookInstallError) as exc_info:
        apply_change(change)

    message = str(exc_info.value)
    assert "is no longer valid UTF-8 (byte offset 9); nothing was written." in message
    assert "0xff" not in message
    assert path.read_bytes() == concurrent
    assert sorted(path.parent.iterdir()) == backups_before


def test_apply_change_refuses_when_file_created_after_plan(redirect) -> None:
    # Planned as "create" (file absent), but something wrote the file since —
    # blind-applying would overwrite content the plan never saw.
    path = redirect("claude")
    change = plan_install("claude", _CMD.format(host="claude"))
    assert change.status == "create"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(HookInstallError, match="changed after this change was planned"):
        apply_change(change)
    assert path.read_text(encoding="utf-8") == "{}\n"


def test_apply_change_refuses_when_file_deleted_after_plan(redirect) -> None:
    # Deleted since plan: applying would resurrect the (merged) old contents.
    path = redirect("claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"hooks": {}}\n', encoding="utf-8")
    change = plan_install("claude", _CMD.format(host="claude"))

    path.unlink()

    with pytest.raises(HookInstallError, match="changed after this change was planned"):
        apply_change(change)
    assert not path.exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock is POSIX-only; write_lock is documented as a no-op on Windows",
)
def test_cli_install_apply_under_held_lock_exits_cleanly(redirect, monkeypatch) -> None:
    # Two concurrent --apply runs must serialize on the hook-host write lock;
    # the loser times out with a clean, attributed error instead of planning
    # from a stale base and clobbering the winner's write (lost update).
    from memtomem_stm.cli._write_lock import hook_hosts_lock_path
    from memtomem_stm.mms import state

    path = redirect("claude")
    monkeypatch.setattr(state, "WRITE_LOCK_TIMEOUT_SECONDS", 0.2)

    with state.write_lock(lock_path=hook_hosts_lock_path()):
        result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude", "--apply"])

    assert result.exit_code == 1
    assert "another `mms hook install/uninstall" in result.output
    assert not path.exists()  # the loser wrote nothing


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock is POSIX-only; write_lock is documented as a no-op on Windows",
)
def test_cli_install_dry_run_ignores_held_lock(redirect, monkeypatch) -> None:
    # Dry-run never writes, so it must not queue behind (or fail on) the lock.
    # (Vacuous on Windows — the no-op lock can't block anything — so skipped
    # alongside the held-lock test rather than passing without meaning.)
    from memtomem_stm.cli._write_lock import hook_hosts_lock_path
    from memtomem_stm.mms import state

    redirect("claude")
    monkeypatch.setattr(state, "WRITE_LOCK_TIMEOUT_SECONDS", 0.2)

    with state.write_lock(lock_path=hook_hosts_lock_path()):
        result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude"])

    assert result.exit_code == 0
    assert "To apply" in result.output


# ── helpers ──────────────────────────────────────────────────────────────────


def _parse(text: str, fmt: str) -> dict:
    return json.loads(text) if fmt == "json" else tomllib.loads(text)


def _config_has_stm_command(parsed: dict, host: str) -> bool:
    """Whether a parsed host config carries a hook command install owns."""
    if host == "kimi":
        return any(_owned(h.get("command", ""), host) for h in parsed.get("hooks", []))
    if host == "cursor":
        entries = parsed.get("hooks", {}).get("postToolUse", [])
        return any(_owned(e.get("command", ""), host) for e in entries)
    # claude / codex — nested matcher groups
    for entry in parsed.get("hooks", {}).get("PostToolUse", []):
        for handler in entry.get("hooks", []):
            if _owned(handler.get("command", ""), host):
                return True
    return False


def test_hook_install_broken_proxy_env_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#847 observability: a proxy-subtree env break used to escape ``mms hook
    install`` as a raw ValidationError traceback. It must now exit non-zero
    with a clean message naming the implicated var — same failure, legible."""
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND", "x")

    with caplog.at_level(logging.WARNING):
        result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude"])

    assert result.exit_code == 1
    assert "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    # The command owns this failure's output (#847 review round 1): no
    # resolver-side log record prints the raw traceback alongside the clean
    # ClickException.
    assert not [r for r in caplog.records if r.exc_info]


def test_hook_install_malformed_bare_payload_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payload settings cannot even decode raises SettingsError, not
    ValidationError — it must get the same clean failure (#847 round 2)."""
    monkeypatch.setenv("MEMTOMEM_STM_PROXY", "{")

    result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude"])

    assert result.exit_code == 1
    assert "invalid MEMTOMEM_STM_* configuration" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


# ── deep or unwritable host configs fail as HookInstallError (#1285) ─────────
#
# ``copy.deepcopy`` recurses in Python, so a config nested 1,000 deep fails it
# on every supported interpreter while every decoder still parses it. The
# parser and serializer branches depend on the stack, so they are injected.

_DEEP = 1000
_CANARY = "private-canary"


def _deep_object(depth: int) -> dict:
    value: dict = {"leaf": 0}
    for _ in range(depth):
        value = {"a": value}
    return value


def _plan(action: str, host: str):
    if action == "install":
        return plan_install(host, f"memtomem-stm hook --host {host}")
    return plan_uninstall(host)


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_cli_refuses_a_config_too_deep_to_rewrite(redirect, action) -> None:
    path = redirect("claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    before = json.dumps({"deep": _deep_object(_DEEP)})
    path.write_text(before, encoding="utf-8")

    result = CliRunner().invoke(cli, ["hook", action, "--host", "claude", "--apply"])

    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1
    assert "is nested too deeply to rewrite (RecursionError); refusing to modify it" in (
        result.output
    )
    assert path.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


@pytest.mark.parametrize("action", ["install", "uninstall"])
@pytest.mark.parametrize("host", ["claude", "codex"])
def test_plan_refuses_a_config_the_parser_recurses_on(redirect, monkeypatch, action, host) -> None:
    from memtomem_stm.cli import hook_hosts

    path = redirect(host)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}" if host == "claude" else "x = 1\n", encoding="utf-8")

    def recurse(_text, *args, **kwargs):
        raise RecursionError(_CANARY)

    if host == "claude":
        monkeypatch.setattr(hook_hosts.json, "loads", recurse)
    else:
        monkeypatch.setattr(hook_hosts.tomllib, "loads", recurse)

    with pytest.raises(HookInstallError) as exc_info:
        _plan(action, host)

    message = str(exc_info.value)
    assert message == (
        f"{path} is nested too deeply to parse (RecursionError); refusing to "
        "modify it. Fix or remove the file, then re-run."
    )


@pytest.mark.parametrize("action", ["install", "uninstall"])
@pytest.mark.parametrize("stage", ["copy", "serialize"])
def test_plan_refuses_a_config_too_deep_to_copy_or_serialize(
    redirect, monkeypatch, action, stage
) -> None:
    from memtomem_stm.cli import hook_hosts

    path = redirect("codex")
    path.parent.mkdir(parents=True, exist_ok=True)
    # An STM block to remove, so uninstall reaches the serializer too.
    path.write_text(
        _plan("install", "codex").new_text if action == "uninstall" else "x = 1\n",
        encoding="utf-8",
    )
    fired: list[str] = []

    def recurse(*args, **kwargs):
        fired.append(stage)
        raise RecursionError(_CANARY)

    if stage == "copy":
        monkeypatch.setattr(hook_hosts.copy, "deepcopy", recurse)
    else:
        monkeypatch.setattr(hook_hosts, "_serialize", recurse)

    with pytest.raises(HookInstallError) as exc_info:
        _plan(action, "codex")

    assert fired
    assert str(exc_info.value) == (
        f"{path} is nested too deeply to rewrite (RecursionError); refusing to "
        "modify it. Fix or remove the file, then re-run."
    )


def test_plan_reports_an_unsearchable_config_directory(redirect, monkeypatch) -> None:
    """Python 3.12/3.13 ``Path.exists`` raises ``PermissionError`` for a file
    under a directory without search permission; 3.14 returns False."""
    path = redirect("claude")
    real_exists = Path.exists

    def exists(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "Permission denied", str(path))
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists)

    with pytest.raises(HookInstallError, match=r"^cannot read .*Permission denied"):
        plan_install("claude", "memtomem-stm hook --host claude")


def _fail_with_permission_error(*args, **kwargs):
    raise PermissionError(13, "Permission denied")


@pytest.mark.parametrize("failure", ["open", "partial-and-cleanup"])
def test_apply_change_reports_a_backup_it_cannot_write(redirect, monkeypatch, failure) -> None:
    """Through the real ``_write_backup``. When a partial backup cannot be
    removed either, the leftover file exists, so the message claims only that
    the config is unchanged (#1285 review)."""
    from memtomem_stm.cli import hook_hosts

    path = redirect("claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    backup = path.parent / (path.name + ".bak")
    change = plan_install("claude", "memtomem-stm hook --host claude")
    writes: list = []
    monkeypatch.setattr(hook_hosts, "atomic_write_text", lambda *a, **k: writes.append(a))
    if failure == "open":
        real_open = hook_hosts.os.open

        def os_open(target, *args, **kwargs):
            if str(target) == str(backup):
                raise PermissionError(13, "Permission denied")
            return real_open(target, *args, **kwargs)

        monkeypatch.setattr(hook_hosts.os, "open", os_open)
    else:
        real_unlink = Path.unlink

        def fdopen(fd, *args, **kwargs):
            hook_hosts.os.close(fd)
            raise OSError(28, "No space left on device")

        def unlink(self, *args, **kwargs):
            if self == backup:
                raise PermissionError(13, "Permission denied")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(hook_hosts.os, "fdopen", fdopen)
        monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(HookInstallError) as exc_info:
        apply_change(change)

    assert str(exc_info.value) == (
        f"cannot back up {path} before writing ([Errno 13] Permission denied); it was not changed."
    )
    assert writes == []
    assert path.read_text(encoding="utf-8") == "{}"
    assert backup.exists() is (failure == "partial-and-cleanup")


@pytest.mark.parametrize("prior", [True, False], ids=["update", "create"])
def test_apply_change_reports_a_config_it_cannot_write(redirect, monkeypatch, prior) -> None:
    from memtomem_stm.cli import hook_hosts

    path = redirect("claude")
    if prior:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    change = plan_install("claude", "memtomem-stm hook --host claude")
    monkeypatch.setattr(hook_hosts, "atomic_write_text", _fail_with_permission_error)

    with pytest.raises(HookInstallError) as exc_info:
        apply_change(change)

    backup = path.parent / (path.name + ".bak")
    if prior:
        assert str(exc_info.value) == (
            f"cannot write {path} ([Errno 13] Permission denied); it was not changed; "
            f"the backup at {backup} is a copy of it."
        )
        assert path.read_text(encoding="utf-8") == "{}"
        assert backup.read_text(encoding="utf-8") == "{}"
    else:
        assert str(exc_info.value) == (
            f"cannot write {path} ([Errno 13] Permission denied); it was not changed."
        )
        assert not path.exists()
        assert not backup.exists()


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_cli_apply_write_failure_exits_cleanly(redirect, monkeypatch, action) -> None:
    from memtomem_stm.cli import hook_hosts

    path = redirect("claude")
    path.parent.mkdir(parents=True, exist_ok=True)
    prior = _plan("install", "claude").new_text if action == "uninstall" else "{}"
    path.write_text(prior, encoding="utf-8")
    monkeypatch.setattr(hook_hosts, "atomic_write_text", _fail_with_permission_error)

    result = CliRunner().invoke(cli, ["hook", action, "--host", "claude", "--apply"])

    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1
    assert f"cannot write {path}" in result.output
    assert path.read_text(encoding="utf-8") == prior


# ── handlers that look like STM's hook but are not managed (#1453) ───────────
#
# Install and uninstall rewrite or remove only what ``owned_handler`` accepts.
# Anything else that looks like STM's hook stays in the config and is
# named by location, so a duplicate is never silent. ``_BARE`` is the shape
# #1453 reported: install appended a second handler beside it, said nothing,
# and uninstall printed "Removed" while it kept running.

_BARE = "MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 /home/u/.local/bin/mms hook"
_MANAGED = "memtomem-stm hook --host claude"
_MANAGED_UV = "uv run --directory /repo memtomem-stm hook --host claude"


def _seed(path: Path, data: dict, fmt: str = "json") -> None:
    import tomli_w

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data) if fmt == "json" else tomli_w.dumps(data)
    path.write_text(text, encoding="utf-8")


def _claude_groups(*groups: list[dict]) -> dict:
    return {
        "hooks": {"PostToolUse": [{"matcher": "Read|Grep|Glob|Bash", "hooks": g} for g in groups]}
    }


def _h(command: str, **fields: object) -> dict:
    return {"type": "command", "command": command, **fields}


@pytest.mark.parametrize(
    ("host", "data", "location"),
    [
        ("claude", _claude_groups([_h(_BARE)]), "hooks.PostToolUse[0].hooks[0]"),
        ("codex", _claude_groups([_h(_BARE)]), "hooks.PostToolUse[0].hooks[0]"),
        (
            "cursor",
            {"version": 1, "hooks": {"postToolUse": [{"command": _BARE}]}},
            "hooks.postToolUse[0]",
        ),
        (
            "kimi",
            {"hooks": [{"event": "PostToolUse", "matcher": "Bash", "command": _BARE}]},
            "hooks[0]",
        ),
    ],
)
def test_unmanaged_stm_like_handler_is_named_on_every_host(
    host: str, data: dict, location: str, redirect
) -> None:
    path = redirect(host)
    _seed(path, data, HOOK_HOSTS[host].fmt)

    installed = plan_install(host, _CMD.format(host=host))
    removed = plan_uninstall(host)

    # Kept as is: install appends its own handler, uninstall finds nothing of its own.
    assert _BARE in installed.new_text
    assert installed.unmanaged == (location,)
    assert installed.unmanaged_after == (location,)  # appended after it, so it stays put
    assert removed.status == "not_installed"
    assert removed.unmanaged == (location,)
    assert removed.unmanaged_after == (location,)


@pytest.mark.parametrize(
    ("host", "data", "before", "after"),
    [
        (
            "cursor",
            {
                "version": 1,
                "hooks": {
                    "postToolUse": [
                        {"command": "memtomem-stm hook --host cursor"},
                        {"command": _BARE},
                    ]
                },
            },
            "hooks.postToolUse[1]",
            "hooks.postToolUse[0]",
        ),
        (
            "kimi",
            {
                "hooks": [
                    {"event": "PostToolUse", "command": "memtomem-stm hook --host kimi"},
                    {"event": "PostToolUse", "command": _BARE},
                ]
            },
            "hooks[1]",
            "hooks[0]",
        ),
    ],
)
def test_flat_host_uninstall_shifts_the_look_alike(
    host: str, data: dict, before: str, after: str, redirect
) -> None:
    path = redirect(host)
    _seed(path, data, HOOK_HOSTS[host].fmt)

    change = plan_uninstall(host)

    assert change.status == "remove"
    assert (change.unmanaged, change.unmanaged_after) == ((before,), (after,))


@pytest.mark.parametrize(
    "handler",
    [
        _h(_MANAGED),  # owned: install manages it
        _h("echo hi"),
        _h("ruff check hooks.py"),
        # Unbalanced: ``shlex`` reads ``hook --label mms`` before the error and
        # ``str.split()`` starts with ``hook``; no pair forms across the two.
        _h('hook --label m"m"s #\''),
    ],
)
def test_managed_and_unrelated_handlers_are_not_named(handler: dict, redirect) -> None:
    path = redirect("claude")
    _seed(path, _claude_groups([handler]))

    assert plan_install("claude", _MANAGED).unmanaged == ()
    assert plan_uninstall("claude").unmanaged == ()


def test_exec_form_handler_is_named_from_its_args(redirect) -> None:
    """``command`` alone (``mms``) is not STM's hook; with ``args`` it is."""
    path = redirect("claude")
    _seed(path, _claude_groups([_h("mms", args=["hook", "--host", "claude"])]))

    assert plan_install("claude", _MANAGED).unmanaged == ("hooks.PostToolUse[0].hooks[0]",)


def test_install_names_positions_before_and_after_the_merge(redirect) -> None:
    """Two owned-only groups fold into one, so the unmanaged group moves up."""
    path = redirect("claude")
    _seed(path, _claude_groups([_h(_MANAGED)], [_h(_MANAGED_UV)], [_h(_BARE)]))

    change = plan_install("claude", _MANAGED)

    assert change.unmanaged == ("hooks.PostToolUse[2].hooks[0]",)
    assert change.unmanaged_after == ("hooks.PostToolUse[1].hooks[0]",)
    written = json.loads(change.new_text)["hooks"]["PostToolUse"]
    assert written[1]["hooks"][0]["command"] == _BARE


def test_uninstall_names_positions_before_and_after_the_removal(redirect) -> None:
    path = redirect("claude")
    _seed(path, _claude_groups([_h(_MANAGED)], [_h(_BARE)]))

    change = plan_uninstall("claude")

    assert change.status == "remove"
    assert change.unmanaged == ("hooks.PostToolUse[1].hooks[0]",)
    assert change.unmanaged_after == ("hooks.PostToolUse[0].hooks[0]",)
    written = json.loads(change.new_text)["hooks"]["PostToolUse"]
    assert written[0]["hooks"][0]["command"] == _BARE


def test_cli_names_the_unmanaged_handler_through_install_and_uninstall(redirect) -> None:
    """The #1453 reproduction, end to end: every step says the handler is there."""
    path = redirect("claude")
    _seed(path, _claude_groups([_h(_BARE)]))
    runner = CliRunner()
    current = "hooks.PostToolUse[0].hooks[0]"

    preview = runner.invoke(cli, ["hook", "install", "--host", "claude"])
    assert preview.exit_code == 0, preview.output
    assert f"in the current file: {current}" in preview.output
    assert "will stay beside memtomem-stm's hook in the config" in preview.output
    # Above the block it describes, not after it.
    assert preview.output.index("like memtomem-stm's hook") < preview.output.index("Would ")

    applied = runner.invoke(cli, ["hook", "install", "--host", "claude", "--apply"])
    assert applied.exit_code == 0, applied.output
    assert "Installed" in applied.output
    assert f"in the written file: {current}" in applied.output
    assert "stays beside memtomem-stm's hook in the config" in applied.output

    again = runner.invoke(cli, ["hook", "install", "--host", "claude", "--apply"])
    assert "already installed" in again.output
    assert f"in the current file: {current}" in again.output

    removed = runner.invoke(cli, ["hook", "uninstall", "--host", "claude", "--apply"])
    assert "Removed the memtomem-stm hook install manages" in removed.output
    assert "stays in the config" in removed.output
    assert "beside" not in removed.output
    assert f"in the written file: {current}" in removed.output

    nothing = runner.invoke(cli, ["hook", "uninstall", "--host", "claude", "--apply"])
    assert "no memtomem-stm hook that uninstall manages" in nothing.output
    assert f"in the current file: {current}" in nothing.output
    groups = json.loads(path.read_text())["hooks"]["PostToolUse"]
    assert [h["command"] for g in groups for h in g["hooks"]] == [_BARE]


def test_cli_without_unmanaged_handlers_prints_no_notice(redirect) -> None:
    redirect("claude")
    result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude"])
    assert "like memtomem-stm's hook" not in result.output


def test_cli_notice_never_shows_command_text(redirect) -> None:
    path = redirect("claude")
    # A fake value, assembled at run time so no key-shaped literal sits in
    # the source for secret scanners to flag.
    value = "-".join(["sk", "lf", "fake", "value"])
    _seed(path, _claude_groups([_h(f"MEMTOMEM_STM_LANGFUSE__SECRET_KEY={value} mms hook")]))

    result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude"])

    assert "hooks.PostToolUse[0].hooks[0]" in result.output
    assert value not in result.output
    assert "SECRET_KEY" not in result.output


def _notices(output: str) -> list[str]:
    return [line for line in output.splitlines() if "like memtomem-stm's hook" in line]


@pytest.mark.parametrize(
    ("action", "groups", "before", "after"),
    [
        # Two owned-only groups fold into one, so the look-alike moves up.
        (
            "install",
            ([_h(_MANAGED)], [_h(_MANAGED_UV)], [_h(_BARE)]),
            "hooks.PostToolUse[2].hooks[0]",
            "hooks.PostToolUse[1].hooks[0]",
        ),
        # The STM group goes, so the look-alike moves up.
        (
            "uninstall",
            ([_h(_MANAGED)], [_h(_BARE)]),
            "hooks.PostToolUse[1].hooks[0]",
            "hooks.PostToolUse[0].hooks[0]",
        ),
    ],
)
def test_cli_preview_names_the_current_file_and_apply_the_written_one(
    action: str, groups: tuple, before: str, after: str, redirect
) -> None:
    path = redirect("claude")
    _seed(path, _claude_groups(*groups))
    runner = CliRunner()

    preview = runner.invoke(cli, ["hook", action, "--host", "claude"])
    applied = runner.invoke(cli, ["hook", action, "--host", "claude", "--apply"])

    assert _notices(preview.output) == [_notices(preview.output)[0]]
    assert f"(in the current file: {before})" in preview.output
    assert _notices(applied.output) == [_notices(applied.output)[0]]
    assert f"(in the written file: {after})" in applied.output


def test_uninstall_of_only_empty_entries_does_not_claim_a_removal(redirect) -> None:
    """An empty matcher group is pruned, but no hook goes, so neither the
    preview nor the apply may say one was removed."""
    path = redirect("claude")
    _seed(path, _claude_groups([], [_h(_BARE)]))
    runner = CliRunner()

    assert plan_uninstall("claude").status == "prune"
    preview = runner.invoke(cli, ["hook", "uninstall", "--host", "claude"])
    applied = runner.invoke(cli, ["hook", "uninstall", "--host", "claude", "--apply"])

    only = "empty hook entries only (no memtomem-stm hook that uninstall manages)."
    assert f"Would remove {only}" in preview.output
    assert f"Removed {only}" in applied.output
    assert "hook install manages" not in preview.output + applied.output
    groups = json.loads(path.read_text())["hooks"]["PostToolUse"]
    assert [h["command"] for g in groups for h in g["hooks"]] == [_BARE]


def _claude_commands(path: Path) -> list[str]:
    groups = json.loads(path.read_text())["hooks"]["PostToolUse"]
    return [h["command"] for g in groups for h in g["hooks"]]


@pytest.mark.parametrize(
    "command", ["mms hook uninstall", "uv run --help mms hook", "OTHER=/mms hook"]
)
def test_cli_leaves_a_line_that_does_not_run_the_bridge(command: str, redirect) -> None:
    """#1460's end-to-end reproduction: install used to replace these lines
    and uninstall to delete them, both calling them STM's hook."""
    path = redirect("claude")
    _seed(path, _claude_groups([_h(command)]))
    runner = CliRunner()
    where = "hooks.PostToolUse[0].hooks[0]"

    assert plan_uninstall("claude").status == "not_installed"
    removed = runner.invoke(cli, ["hook", "uninstall", "--host", "claude", "--apply"])
    assert removed.exit_code == 0, removed.output
    assert "not installed (no memtomem-stm hook that uninstall manages)." in removed.output
    assert "Removed" not in removed.output
    assert f"(in the current file: {where})" in removed.output
    assert _claude_commands(path) == [command]

    installed = runner.invoke(cli, ["hook", "install", "--host", "claude", "--apply"])
    assert installed.exit_code == 0, installed.output
    assert "Installed memtomem-stm's PostToolUse hook." in installed.output
    assert f"(in the written file: {where})" in installed.output
    commands = _claude_commands(path)
    assert commands[0] == command
    assert len(commands) == 2 and _owned(commands[1])

    assert plan_uninstall("claude").status == "remove"
    again = runner.invoke(cli, ["hook", "uninstall", "--host", "claude", "--apply"])
    assert again.exit_code == 0, again.output
    assert "Removed the memtomem-stm hook install manages." in again.output
    assert f"(in the written file: {where})" in again.output
    assert _claude_commands(path) == [command]


@pytest.mark.parametrize(
    "command", ["mms hook uninstall", "uv run --help mms hook", "OTHER=/mms hook"]
)
def test_uninstall_beside_a_line_that_does_not_run_the_bridge_only_prunes(
    command: str, redirect
) -> None:
    path = redirect("claude")
    _seed(path, _claude_groups([], [_h(command)]))

    change = plan_uninstall("claude")

    assert change.status == "prune"
    assert json.loads(change.new_text) == _claude_groups([_h(command)])


def test_cursor_install_leaves_a_management_subcommand(redirect) -> None:
    """Only Claude was run end to end in #1460; Cursor matches on ``command`` too."""
    path = redirect("cursor")
    _seed(path, {"version": 1, "hooks": {"postToolUse": [{"command": "mms hook uninstall"}]}})

    installed = plan_install("cursor", _CMD.format(host="cursor"))

    entries = json.loads(installed.new_text)["hooks"]["postToolUse"]
    assert [e["command"] for e in entries] == ["mms hook uninstall", _CMD.format(host="cursor")]
    assert installed.unmanaged == ("hooks.postToolUse[0]",)
    assert plan_uninstall("cursor").status == "not_installed"


@pytest.mark.parametrize(
    ("host", "data", "location"),
    [
        (
            "cursor",
            {"version": 1, "hooks": {"postToolUse": [{"command": _BARE, "type": "x"}]}},
            "hooks.postToolUse[0]",
        ),
        ("kimi", {"hooks": [{"event": "PostToolUse", "command": _BARE, "type": "x"}]}, "hooks[0]"),
    ],
)
def test_flat_host_entry_with_its_own_type_is_still_named(
    host: str, data: dict, location: str, redirect
) -> None:
    """Cursor and Kimi run an entry's ``command`` whatever else it holds."""
    path = redirect(host)
    _seed(path, data, HOOK_HOSTS[host].fmt)

    assert plan_install(host, _CMD.format(host=host)).unmanaged == (location,)


def test_dry_run_no_change_paths_name_the_current_file(redirect) -> None:
    """``already`` and ``not_installed`` return early; a preview of either
    must still print the notice, once."""
    path = redirect("claude")
    _seed(path, _claude_groups([_h(_BARE)]))
    runner = CliRunner()
    current = "(in the current file: hooks.PostToolUse[0].hooks[0])"

    nothing = runner.invoke(cli, ["hook", "uninstall", "--host", "claude"])
    assert "not installed (no memtomem-stm hook that uninstall manages)." in nothing.output
    assert len(_notices(nothing.output)) == 1 and current in nothing.output

    runner.invoke(cli, ["hook", "install", "--host", "claude", "--apply"])
    already = runner.invoke(cli, ["hook", "install", "--host", "claude"])
    assert "already installed" in already.output
    assert len(_notices(already.output)) == 1 and current in already.output


def test_kimi_look_alike_on_another_event_is_named(redirect) -> None:
    """Install owns only ``PostToolUse`` entries, but the scan reads every
    entry whatever its ``event``."""
    path = redirect("kimi")
    _seed(
        path,
        {
            "hooks": [
                {"event": "PostToolUse", "matcher": "Bash", "command": "echo hi"},
                {"event": "PostToolUseFailure", "command": _BARE},
            ]
        },
        "toml",
    )

    assert plan_install("kimi", _CMD.format(host="kimi")).unmanaged == ("hooks[1]",)
    assert plan_uninstall("kimi").unmanaged == ("hooks[1]",)


def test_notice_agrees_with_the_number_of_handlers(redirect) -> None:
    path = redirect("claude")
    _seed(path, _claude_groups([_h(_BARE), _h("mms --verbose hook")]))

    result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude", "--apply"])

    assert (
        "2 other handlers look like memtomem-stm's hook but are not ones install manages, "
        "so they stay beside memtomem-stm's hook in the config (in the written file: "
        "hooks.PostToolUse[0].hooks[0], "
        "hooks.PostToolUse[0].hooks[1]). Check them by hand"
    ) in result.output


# ── a line with shell syntax is left alone and named (#1476) ─────────────────


@pytest.mark.parametrize(
    "command",
    [
        "mms hook --host # --persist-query-text",
        # Quote removal joins these into STM's executable or ``hook``, which
        # ``mms doctor``'s candidate test does not see in the raw text.
        'm"m"s hook --host # --persist-query-text',
        'mms h"o"ok --host "$H"',
        'm"m"s hook --host claude # pinned',
        # Unbalanced: the words read before the parse error still name it.
        'm"m"s hook --host \'claude',
    ],
)
def test_a_refused_hook_line_is_kept_and_named(command: str, redirect) -> None:
    path = redirect("claude")
    _seed(path, _claude_groups([_h(command)]))
    location = "hooks.PostToolUse[0].hooks[0]"

    installed = plan_install("claude", _MANAGED)
    removed = plan_uninstall("claude")

    groups = json.loads(installed.new_text)["hooks"]["PostToolUse"]
    assert [h["command"] for g in groups for h in g["hooks"]] == [command, _MANAGED]
    assert installed.unmanaged == (location,)
    assert removed.status == "not_installed"
    assert removed.unmanaged == (location,)


def test_a_refused_hook_line_without_a_type_is_named(redirect) -> None:
    """The word test reads ``command`` alone, so a handler the candidate test
    skips for its missing ``type`` is named too."""
    path = redirect("claude")
    _seed(path, _claude_groups([{"command": 'm"m"s hook --host # --persist-query-text'}]))

    assert plan_install("claude", _MANAGED).unmanaged == ("hooks.PostToolUse[0].hooks[0]",)


def test_cli_install_does_not_take_over_a_commented_line(redirect) -> None:
    """The issue's commented line, end to end: install used to rewrite it with
    ``--persist-query-text``, a flag the shell never passed to the hook."""
    commented = "mms hook --host # --persist-query-text"
    path = redirect("claude")
    _seed(path, _claude_groups([_h(commented)]))
    location = "hooks.PostToolUse[0].hooks[0]"
    runner = CliRunner()

    applied = runner.invoke(cli, ["hook", "install", "--host", "claude", "--apply"])
    assert applied.exit_code == 0, applied.output
    assert f"in the written file: {location}" in applied.output
    groups = json.loads(path.read_text())["hooks"]["PostToolUse"]
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert commands[0] == commented
    assert len(commands) == 2
    written = shlex.split(commands[1])
    assert "--no-persist-query-text" in written
    assert "--persist-query-text" not in written

    removed = runner.invoke(cli, ["hook", "uninstall", "--host", "claude", "--apply"])
    assert removed.exit_code == 0, removed.output
    assert f"in the written file: {location}" in removed.output
    groups = json.loads(path.read_text())["hooks"]["PostToolUse"]
    assert [h["command"] for g in groups for h in g["hooks"]] == [commented]


# ── ownership does not depend on the platform ────────────────────────────────
#
# The old recognizer had Windows rules of its own (Click expands ``~``,
# ``$VAR`` / ``%VAR%`` and globs in the hook's arguments there). The form
# install writes holds none of those after the executable, so one rule serves
# every platform.


@pytest.fixture
def on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    from memtomem_stm.cli import hook_registration

    monkeypatch.setattr(hook_registration, "running_on_windows", lambda: True)


_WINDOWS_LOOK_ALIKES = [
    # #1483: PowerShell reads a quoted executable as a string, not a command.
    '"mms" hook --host claude --persist-query-text',
    '"memtomem-stm" hook --host claude',
    "'mms' hook --host claude",
    'm"m"s hook --host claude',
    '""mms hook --host claude',
    '"uv" run --directory /repo memtomem-stm hook --host claude',
    "'env' MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS=12 mms hook --host claude",
    '~/bin/"mms" hook --host claude',
    '  "C:/Program Files/stm/mms" hook --host claude',
    "mms hook --host 'C*'",
    "mms hook --host %HOOK_HOST%",
    "mms hook --host '$HOOK_HOST'",
    "uv run --directory /repo memtomem-stm hook --host 'C*'",
]


@pytest.mark.parametrize("command", _WINDOWS_LOOK_ALIKES)
@pytest.mark.parametrize("windows", [True, False])
def test_a_look_alike_is_not_owned_on_either_platform(
    command: str, windows: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem_stm.cli import hook_registration

    monkeypatch.setattr(hook_registration, "running_on_windows", lambda: windows)
    assert _owned(command) is False


@pytest.mark.parametrize("host", sorted(HOOK_HOSTS))
@pytest.mark.parametrize(
    "root",
    [
        None,
        PureWindowsPath("C:\\Users\\x\\repo"),
        PureWindowsPath("C:\\Users\\x\\it's $HOME %TEMP% (*) ~"),
    ],
)
def test_windows_owns_every_command_install_writes(
    host: str, root: PurePath | None, on_windows
) -> None:
    assert _owned(_written(host, root, _POLICIES[2]), host) is True


def test_cli_leaves_a_quoted_executable_and_reads_no_setting_from_it(on_windows, redirect) -> None:
    """#1483, end to end: install used to import ``--persist-query-text`` from
    this line and uninstall to delete it."""
    quoted = '"mms" hook --host claude --persist-query-text'
    path = redirect("claude")
    _seed(path, _claude_groups([_h(quoted)]))
    location = "hooks.PostToolUse[0].hooks[0]"
    runner = CliRunner()

    applied = runner.invoke(cli, ["hook", "install", "--host", "claude", "--apply"])
    assert applied.exit_code == 0, applied.output
    assert f"in the written file: {location}" in applied.output
    commands = _claude_commands(path)
    assert commands[0] == quoted and len(commands) == 2
    assert commands[1].endswith(" --no-persist-query-text")

    removed = runner.invoke(cli, ["hook", "uninstall", "--host", "claude", "--apply"])
    assert removed.exit_code == 0, removed.output
    assert f"in the written file: {location}" in removed.output
    assert _claude_commands(path) == [quoted]


@pytest.mark.parametrize(
    "command",
    [
        "mms hook --host 'C*'",
        # Quote removal joins the executable name, which the raw search misses.
        "m\"m\"s hook --host 'C*'",
        "m\"m\"s hook --host '~x'",
    ],
)
def test_windows_names_a_line_it_does_not_own(command: str, on_windows, redirect) -> None:
    path = redirect("claude")
    _seed(path, _claude_groups([_h(command)]))
    location = "hooks.PostToolUse[0].hooks[0]"

    installed = plan_install("claude", _MANAGED)
    removed = plan_uninstall("claude")

    groups = json.loads(installed.new_text)["hooks"]["PostToolUse"]
    assert [h["command"] for g in groups for h in g["hooks"]] == [command, _MANAGED]
    assert installed.unmanaged == (location,)
    assert removed.status == "not_installed"
    assert removed.unmanaged == (location,)


# ── the notice's word test ───────────────────────────────────────────────────


def _count_splits(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every line handed to the notice's ``shlex`` split."""
    import shlex as shlex_module

    from memtomem_stm.cli import host_runtime

    calls: list[str] = []
    real = shlex_module.shlex

    def counting(command: str, *args: object, **kwargs: object) -> shlex_module.shlex:
        calls.append(command)
        return real(command, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(host_runtime.shlex, "shlex", counting)
    return calls


def _padded(text: str, tail: str) -> str:
    """``text`` behind enough filler that ``tail`` starts at character 4,096,
    the length past which ``mms doctor``'s candidate test stops splitting."""
    return "echo " + "x" * (4096 - len("echo ") - 1 - len(text)) + " " + text + tail


@pytest.mark.parametrize(
    ("command", "named"),
    [
        # Spelled so that ``is_candidate``'s raw search misses the name, which
        # leaves the decision to the word test; the ``#`` keeps that test's
        # own quoted split away. Length does not hide the hook's words.
        (f"m\"m\"s hook --host '{'x' * 5000}' # pinned", True),
        ('m"m"s hook --host claude' + " " * 5000 + "# pinned", True),
        (f"echo '{'x' * 5000}' # then\nm\"m\"s hook --host claude", True),
        # Lines a reading of only the first 4,096 characters would name: the
        # word is ``hookshot``, and in the second the words sit inside one
        # quoted argument.
        (_padded('m"m"s hook', "shot # note"), False),
        ('echo "' + _padded("mms hook", 'shot" # note')[len("echo ") + 1 :], False),
    ],
)
def test_the_notice_reads_the_words_of_the_whole_line(
    command: str, named: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem_stm.cli.hook_hosts import unmanaged_stm_like

    calls = _count_splits(monkeypatch)

    expected = ("hooks.PostToolUse[0].hooks[0]",) if named else ()
    assert unmanaged_stm_like("claude", _claude_groups([_h(command)])) == expected
    assert calls == [command]


def test_the_notice_scan_splits_a_line_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """One split per line per scan (``shlex`` is quadratic in a word's
    length), also past ``is_candidate``'s own limit of 4,096 characters."""
    from memtomem_stm.cli.hook_hosts import unmanaged_stm_like

    calls = _count_splits(monkeypatch)
    line = "echo '" + "x" * 5000 + "'"

    assert unmanaged_stm_like("claude", _claude_groups([_h(line)])) == ()
    assert calls == [line]


# ── a quoted first word on every host (#1483) ────────────────────────────────


@pytest.mark.parametrize(
    ("host", "data", "location"),
    [
        (
            "claude",
            _claude_groups([_h('"memtomem-stm" hook --host claude')]),
            "hooks.PostToolUse[0].hooks[0]",
        ),
        (
            "codex",
            _claude_groups([_h('"memtomem-stm" hook --host codex')]),
            "hooks.PostToolUse[0].hooks[0]",
        ),
        (
            "cursor",
            {
                "version": 1,
                "hooks": {"postToolUse": [{"command": '"memtomem-stm" hook --host cursor'}]},
            },
            "hooks.postToolUse[0]",
        ),
        (
            "kimi",
            {
                "hooks": [
                    {
                        "event": "PostToolUse",
                        "matcher": "Bash",
                        "command": '"memtomem-stm" hook --host kimi',
                    }
                ]
            },
            "hooks[0]",
        ),
    ],
)
@pytest.mark.parametrize("windows", [True, False])
def test_a_quoted_first_word_is_named_on_every_host(
    host: str,
    data: dict,
    location: str,
    windows: bool,
    redirect,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install's own line with quotes ``shlex.join`` would not add is left in
    place and named, in every host's container, on either platform."""
    from memtomem_stm.cli import hook_registration

    monkeypatch.setattr(hook_registration, "running_on_windows", lambda: windows)
    path = redirect(host)

    # Positive control: the same entry, unquoted, is install's own.
    unquoted = json.loads(json.dumps(data).replace('\\"memtomem-stm\\"', "memtomem-stm"))
    _seed(path, unquoted, HOOK_HOSTS[host].fmt)
    assert plan_install(host, _CMD.format(host=host)).unmanaged == ()
    assert plan_uninstall(host).status != "not_installed"

    _seed(path, data, HOOK_HOSTS[host].fmt)
    installed = plan_install(host, _CMD.format(host=host))
    removed = plan_uninstall(host)
    assert installed.unmanaged == (location,)
    assert installed.unmanaged_after == (location,)  # appended after it, so it stays put
    assert removed.status == "not_installed"
    assert removed.unmanaged == (location,)


def test_cli_windows_install_refreshes_the_owned_handler_after_a_quoted_one(
    on_windows, redirect
) -> None:
    """A quoted line ahead of an owned one: install keeps the first as
    written and carries the second's own settings over, not the quoted
    line's ``--persist-query-text``."""
    owned = _written(
        "claude",
        None,
        HostRuntimePolicy(
            use_daemon=False,
            surfacing_timeout_seconds=7.5,
            daemon_timeout_seconds=4.0,
            persist_query_text=False,
        ),
    )
    quoted = '"mms" hook --host claude --persist-query-text'
    path = redirect("claude")
    _seed(path, _claude_groups([_h(quoted)], [_h(owned)]))
    runner = CliRunner()

    applied = runner.invoke(cli, ["hook", "install", "--host", "claude", "--apply"])
    assert applied.exit_code == 0, applied.output
    assert "in the written file: hooks.PostToolUse[0].hooks[0]" in applied.output
    groups = json.loads(path.read_text())["hooks"]["PostToolUse"]
    assert len(groups) == 2
    assert [h["command"] for h in groups[0]["hooks"]] == [quoted]
    (refreshed,) = groups[1]["hooks"]
    written = shlex.split(refreshed["command"])
    assert "--no-daemon" in written
    assert written[written.index("--surfacing-timeout-seconds") + 1] == "7.5"
    assert "--no-persist-query-text" in written
    assert "--persist-query-text" not in written


def test_a_refused_line_the_candidate_test_names_is_not_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A line is split for the look-alike test only when ``mms doctor``'s
    candidate test has not already named it."""
    from memtomem_stm.cli.hook_hosts import unmanaged_stm_like

    calls = _count_splits(monkeypatch)
    # The raw search names it; the word test, which could split a line this
    # short, must not run after that.
    line = "mms hook --host claude # pinned"

    assert unmanaged_stm_like("claude", _claude_groups([_h(line)])) == (
        "hooks.PostToolUse[0].hooks[0]",
    )
    assert calls == []


def test_the_notice_scan_reads_a_long_unrelated_line_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For a line that is not STM's, the look-alike test makes one adjacency
    scan, which reads a word as a path only before a ``hook`` word (an
    ``echo`` with many arguments builds no path at all)."""
    from memtomem_stm.cli import hook_hosts

    scans: list[int] = []
    real_scan = hook_hosts._exe_before_hook

    def counting_scan(tokens: list[str]) -> Iterator[int]:
        scans.append(len(tokens))
        return real_scan(tokens)

    paths: list[str] = []
    real_path = hook_hosts.Path

    def counting_path(*args: str) -> Path:
        paths.extend(args)
        return real_path(*args)

    monkeypatch.setattr(hook_hosts, "_exe_before_hook", counting_scan)
    monkeypatch.setattr(hook_hosts, "Path", counting_path)
    line = "echo " + " ".join(["a"] * 1000)

    assert hook_hosts.unmanaged_stm_like("claude", _claude_groups([_h(line)])) == ()
    assert scans == [1001]
    assert paths == []


@pytest.mark.parametrize(
    ("tokens", "indexes"),
    [
        (["mms", "hook", "x", "memtomem-stm", "hook"], [0, 3]),  # two pairs, in order
        (["mms", "hook", "hook"], [0]),  # ``hook`` after ``hook`` is not a pair
        (["echo", "hook", "/bin/mms"], []),  # an executable last, with no ``hook``
        (["hook", "mms"], []),
        (["/usr/local/bin/memtomem-stm-proxy", "hook"], [0]),
    ],
)
def test_exe_before_hook_finds_each_pair(tokens: list[str], indexes: list[int]) -> None:
    from memtomem_stm.cli.hook_hosts import _exe_before_hook

    assert list(_exe_before_hook(tokens)) == indexes


@pytest.mark.parametrize(
    ("line", "most"),
    [
        # Quoted names, so ``is_candidate``'s raw search does not decide first;
        # the ``#`` keeps its quoted split away. The word test stops at the
        # first pair. Each line stays within the split's length bound.
        ('m"m"s hook # ' + 'm"m"s hook ' * 300, 1),
        # Named by the candidate test's own quoted split: no scan here.
        ('m"m"s hook ' * 300, 0),
        # Named by the raw search.
        ('mms hook --host "|" ' + 'm"m"s hook ' * 300, 0),
        ('m"m"s hook --host "|"', 1),
    ],
)
def test_the_notice_scan_stops_early(line: str, most: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from memtomem_stm.cli import hook_hosts

    yielded: list[int] = []
    real_scan = hook_hosts._exe_before_hook

    def counting_scan(tokens: list[str]) -> Iterator[int]:
        for index in real_scan(tokens):
            yielded.append(index)
            yield index

    monkeypatch.setattr(hook_hosts, "_exe_before_hook", counting_scan)

    assert hook_hosts.unmanaged_stm_like("claude", _claude_groups([_h(line)])) == (
        "hooks.PostToolUse[0].hooks[0]",
    )
    assert len(yielded) <= most


# ── #1461: Kimi entries on another event are not install's ───────────────────

_KIMI = "memtomem-stm hook --host kimi"


def _kimi_hooks(path: Path) -> list[dict]:
    return tomllib.loads(path.read_text(encoding="utf-8")).get("hooks", [])


def test_kimi_install_and_uninstall_leave_an_entry_on_another_event(redirect) -> None:
    """The issue's file: install moved the ``PreToolUse`` entry to
    ``PostToolUse`` and uninstall deleted it, both calling it STM's
    PostToolUse hook."""
    path = redirect("kimi")
    pre = {"event": "PreToolUse", "matcher": "Bash", "command": _KIMI}
    _seed(path, {"hooks": [pre]}, "toml")
    runner = CliRunner()

    removed = runner.invoke(cli, ["hook", "uninstall", "--host", "kimi", "--apply"])
    assert removed.exit_code == 0, removed.output
    assert "not installed (no memtomem-stm hook that uninstall manages)." in removed.output
    assert "(in the current file: hooks[0])" in removed.output
    assert _kimi_hooks(path) == [pre]

    installed = runner.invoke(
        cli, ["hook", "install", "--host", "kimi", "--inherit-runtime-env", "--apply"]
    )
    assert installed.exit_code == 0, installed.output
    assert "(in the written file: hooks[0])" in installed.output
    kept, added = _kimi_hooks(path)
    assert kept == pre
    assert (added["event"], added["matcher"]) == ("PostToolUse", matcher_for("kimi"))
    assert _owned(added["command"], "kimi")


def test_kimi_manages_only_the_post_tool_use_entry_of_two(redirect) -> None:
    path = redirect("kimi")
    pre = {"event": "PreToolUse", "matcher": "Bash", "command": _KIMI}
    post = {"event": "PostToolUse", "matcher": "Bash", "command": _KIMI, "timeout": 5}
    _seed(path, {"hooks": [pre, post]}, "toml")
    refreshed = f"{_KIMI} {_POLICY}"

    installed = plan_install("kimi", refreshed)
    apply_change(installed)

    assert _kimi_hooks(path) == [
        pre,
        {**post, "matcher": matcher_for("kimi"), "command": refreshed},
    ]
    assert installed.unmanaged == ("hooks[0]",)

    # Against the refreshed file: the entry install just wrote is the one,
    # and the only one, uninstall takes.
    removed = plan_uninstall("kimi")
    apply_change(removed)

    assert removed.status == "remove"
    assert _kimi_hooks(path) == [pre]


# ── a Claude Code handler on another event is not install's ─────────────────


def test_claude_install_and_uninstall_leave_an_owned_looking_handler_on_another_event(
    redirect,
) -> None:
    """Place is part of ownership on every host: install writes under
    ``PostToolUse`` and reads nothing else, so the same text under
    ``PreToolUse`` is neither replaced nor removed."""
    path = redirect("claude")
    pre = {"matcher": "Bash", "hooks": [_h(_MANAGED)]}
    _seed(path, {"hooks": {"PreToolUse": [pre]}})

    installed = plan_install("claude", _MANAGED_UV)
    apply_change(installed)
    removed = plan_uninstall("claude")

    written = json.loads(installed.new_text)["hooks"]
    assert written["PreToolUse"] == [pre]
    assert [h["command"] for g in written["PostToolUse"] for h in g["hooks"]] == [_MANAGED_UV]
    assert removed.status == "remove"
    assert json.loads(removed.new_text) == {"hooks": {"PreToolUse": [pre]}}


# ── a handler in exec form is not install's ──────────────────────────────────


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_a_handler_with_args_is_left_and_named(host: str, redirect) -> None:
    """Install never writes ``args``. In Claude Code it makes ``command`` one
    executable path, so this handler never ran STM whatever its text says."""
    path = redirect(host)
    exec_form = {"type": "command", "command": _CMD.format(host=host), "args": ["--verbose"]}
    _seed(path, _claude_groups([exec_form]), HOOK_HOSTS[host].fmt)
    location = "hooks.PostToolUse[0].hooks[0]"

    installed = plan_install(host, _CMD.format(host=host))
    removed = plan_uninstall(host)

    groups = _parse(installed.new_text, installed.fmt)["hooks"]["PostToolUse"]
    assert [h for g in groups for h in g["hooks"]] == [
        exec_form,
        {"type": "command", "command": _CMD.format(host=host)},
    ]
    assert installed.unmanaged == (location,)
    assert removed.status == "not_installed"
    assert removed.unmanaged == (location,)


# ── #1470: the reporter's three files ────────────────────────────────────────
#
# Their handlers are hand-written (``mms hook …``), so none is owned and none
# of them reaches the question of several owned handlers. Install used to fold
# them into one and keep the first one's settings only.

_PINNED = "mms hook --host claude --surfacing-timeout-seconds 9 --no-daemon"
_PLAIN = "mms hook --host claude"


@pytest.mark.parametrize("handlers", [[_PINNED], [_PINNED, _PLAIN], [_PLAIN, _PINNED]])
def test_install_leaves_hand_written_handlers_and_their_settings(
    handlers: list[str], redirect
) -> None:
    path = redirect("claude")
    _seed(path, _claude_groups([_h(command) for command in handlers]))

    result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude", "--apply"])

    assert result.exit_code == 0, result.output
    commands = _claude_commands(path)
    assert commands[: len(handlers)] == handlers  # byte for byte, in place
    assert len(commands) == len(handlers) + 1
    added = owned_hook_command(commands[-1], "claude")
    assert added is not None
    # Nothing is read from a handler install does not own.
    assert added.settings.use_daemon is True
    assert added.settings.surfacing_timeout_seconds == 3.0
    for index in range(len(handlers)):
        assert f"hooks.PostToolUse[0].hooks[{index}]" in result.output


# ── settings carry over from the owned handler, as the hook runs them ────────


def test_cli_refresh_carries_the_deadline_the_hook_runs_with(redirect) -> None:
    """A hand-edited pair (surfacing 9, daemon 4) runs as 9 and 10. A refresh
    that only changes the surfacing deadline keeps that 10, not the written 4."""
    path = redirect("claude")
    edited = (
        "memtomem-stm hook --host claude --no-daemon --surfacing-timeout-seconds 9 "
        "--daemon-timeout-seconds 4 --persist-query-text"
    )
    _seed(path, _claude_groups([_h(edited)]))

    result = CliRunner().invoke(
        cli, ["hook", "install", "--host", "claude", "--surfacing-timeout", "2", "--apply"]
    )

    assert result.exit_code == 0, result.output
    (command,) = _claude_commands(path)
    owned = owned_hook_command(command, "claude")
    assert owned is not None
    assert owned.settings == PartialHostRuntimePolicy(
        use_daemon=False,
        surfacing_timeout_seconds=2.0,
        daemon_timeout_seconds=10.0,
        persist_query_text=True,
    )


# ── one read of the host file per plan ───────────────────────────────────────


def test_cli_install_reads_the_host_file_once(redirect, monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings carried over and the handlers the merge replaces come from
    one read; two reads could straddle a write by the host app."""
    from memtomem_stm.cli import hook_hosts

    path = redirect("claude")
    _seed(path, _claude_groups([_h(f"{_MANAGED} {_POLICY}")]))
    reads: list[Path] = []
    real = hook_hosts._read_current

    def counting(read_path: Path, fmt: str):
        reads.append(read_path)
        return real(read_path, fmt)

    monkeypatch.setattr(hook_hosts, "_read_current", counting)

    result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude"])

    assert result.exit_code == 0, result.output
    assert reads == [path]


def test_a_file_rewritten_while_the_command_is_built_is_refused_before_any_backup(
    redirect,
) -> None:
    """The host app does not take the mms lock, so it can write between the
    plan's read and the apply; the apply must then refuse, not merge into a
    file it never read."""
    path = redirect("claude")
    _seed(path, _claude_groups([_h(_MANAGED)]))
    rewritten = json.dumps(_claude_groups([_h(_MANAGED)], [_h("other.sh")]))

    def build(owned: tuple) -> str:
        assert len(owned) == 1
        path.write_text(rewritten, encoding="utf-8")
        return f"{_MANAGED} {_POLICY}"

    change = plan_install("claude", build)

    with pytest.raises(HookInstallError):
        apply_change(change)
    assert path.read_text(encoding="utf-8") == rewritten
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_plan_install_hands_the_builder_the_owned_handlers_in_file_order(redirect) -> None:
    path = redirect("claude")
    pinned = (
        f"{_MANAGED_UV} --no-daemon --surfacing-timeout-seconds 7 "
        "--daemon-timeout-seconds 8 --persist-query-text"
    )
    _seed(path, _claude_groups([_h(_BARE), _h(pinned)], [_h(_MANAGED)]))
    seen: list[tuple] = []

    def build(owned: tuple) -> str:
        seen.append(owned)
        return _MANAGED

    plan_install("claude", build)

    ((first, second),) = seen
    assert first.launcher == tuple(hook_launcher("/repo"))
    assert first.settings.surfacing_timeout_seconds == 7.0
    assert second.launcher == ("memtomem-stm",)
    assert second.settings == PartialHostRuntimePolicy()
