"""Tests for ``mms hook install`` / ``mms hook uninstall`` — per-host hook-config
registration (Track B / B3).

Coverage:

- ``_is_stm_hook_command`` — recognizes STM's hook block by command shape
  (global / uv-run / any entry point / any ``--host``) without false-positiving
  on a command that merely mentions "hook".
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
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner
from helpers import set_home

from memtomem_stm.cli.hook_adapter import READLIKE_SURFACE_TOOLS, known_hosts
from memtomem_stm.cli.hook_cmd import _DEFAULT_SURFACE_TOOLS, _hook_install_command_argv
from memtomem_stm.cli.hook_hosts import (
    HOOK_HOSTS,
    HookInstallError,
    _config_path,
    _is_stm_hook_command,
    apply_change,
    installed_stm_hook_commands,
    matcher_for,
    plan_install,
    plan_uninstall,
)
from memtomem_stm.cli.host_runtime import HostRuntimePolicy
from memtomem_stm.cli.proxy import _launcher_for, _StmProject, cli

_CMD = "memtomem-stm hook --host {host}"


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


# ── _is_stm_hook_command ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "memtomem-stm hook --host claude",
        "mms hook --host codex",
        "memtomem-stm-proxy hook",
        "uv run --directory /repo memtomem-stm hook --host kimi",
        "uv run --directory /repo/memtomem-stm memtomem-stm hook --host claude",
        "/usr/local/bin/memtomem-stm hook --host cursor",  # absolute path basename
        ("env MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS=12 memtomem-stm hook --host claude"),
        (
            "/usr/bin/env MEMTOMEM_STM_HOOK__USE_DAEMON=true "
            "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS=12 uv run --directory /repo "
            "memtomem-stm hook --host claude"
        ),
    ],
)
def test_is_stm_hook_command_recognizes_our_invocations(command: str) -> None:
    assert _is_stm_hook_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "memtomem-stm status",  # STM exe, but not the hook subcommand
        "echo hook",  # 'hook' token but no STM executable
        "ruff check hooks.py",  # 'hooks.py' is not the 'hook' token (substring guard)
        "my-linter --run hook-stage",  # neither an STM exe nor a bare 'hook' token
        # Compound command: a bare 'hook' token AND an STM exe, but the exe's
        # subcommand is 'status', not 'hook' — adjacency (exe→hook) is required,
        # so install/uninstall must leave this user command alone (#529).
        "echo hook && memtomem-stm status",
        "memtomem-stm",  # bare exe, no trailing subcommand at all (no IndexError)
        "env FOO=bar memtomem-stm hook --host claude",  # arbitrary wrapper is not ours
        "env -i MEMTOMEM_STM_X=1 memtomem-stm hook --host claude",  # env flags refused
        "env MEMTOMEM_STM_X=1; memtomem-stm hook --host claude",  # compound shell
        "",
    ],
)
def test_is_stm_hook_command_ignores_foreign_commands(command: str) -> None:
    assert _is_stm_hook_command(command) is False


# Lines that name STM's hook but would not run its bridge (#1460): a management
# subcommand, help, a usage error, an assignment or a directory where the
# executable goes, and ``uv`` printing its own help or failing on an empty
# directory. Each was rewritten by install and deleted by uninstall.
_NOT_THE_BRIDGE = [
    "mms hook uninstall",
    "mms hook install --host claude --apply",
    "mms hook --help",
    "mms hook -h",
    "mms hook --typo",
    "uv run --help mms hook",
    "uv run --directory '' memtomem-stm hook --host claude",
    "OTHER=/mms hook",
    "mms/ hook",
    # The same word check on the ``env`` launcher: an assignment (which makes
    # the line a bare assignment prefix) and a directory.
    "OTHER=/env MEMTOMEM_STM_X=1 mms hook",
    "/tmp/env/ MEMTOMEM_STM_X=1 mms hook",
]


@pytest.mark.parametrize("command", _NOT_THE_BRIDGE)
def test_is_stm_hook_command_requires_a_line_that_runs_the_bridge(command: str) -> None:
    assert _is_stm_hook_command(command) is False


@pytest.mark.parametrize(
    "command",
    [
        # These may well run the hook, but not through the one ``uv`` launcher
        # install writes (``uv run --directory <dir> <entry point>``), so they
        # are left alone rather than rewritten.
        "uv run memtomem-stm hook --host claude",
        "uv run --directory /repo --frozen memtomem-stm hook --host claude",
        "uv run --directory /repo /repo/.venv/bin/memtomem-stm hook --host claude",
        "uv run --directory -x memtomem-stm hook --host claude",  # an option, not a directory
    ],
)
def test_is_stm_hook_command_requires_the_uv_launcher_install_writes(command: str) -> None:
    assert _is_stm_hook_command(command) is False


@pytest.mark.parametrize(
    "command",
    [
        # #1453 is not taken over here: a bare assignment prefix stays unrecognized.
        "MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 mms hook --host claude",
        "FOO=1 mms hook",
    ],
)
def test_is_stm_hook_command_still_ignores_a_bare_assignment_prefix(command: str) -> None:
    assert _is_stm_hook_command(command) is False


def test_is_stm_hook_command_keeps_a_quoted_legacy_env_value() -> None:
    """The legacy ``env`` form keeps its recognition whatever the value: the
    runtime-policy reader recovers ``12`` from it on install."""
    assert _is_stm_hook_command("env MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS=' 12 ' mms hook")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path rules")
def test_is_stm_hook_command_keeps_a_drive_relative_executable() -> None:
    """``Path("C:mms").name`` is ``mms`` on Windows; the executable-word check
    must not take the drive prefix for a directory."""
    assert _is_stm_hook_command("C:mms hook --host claude") is True


@pytest.mark.parametrize(
    "command",
    [
        # The split refuses a line with ``#`` outside quotes, so these working
        # lines are left alone (and named) rather than read without their
        # comment (#1476).
        "mms hook --host claude # managed",
        "env MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS=12 mms hook --host claude #pinned",
    ],
)
def test_is_stm_hook_command_leaves_a_line_with_a_trailing_comment(command: str) -> None:
    assert _is_stm_hook_command(command) is False


@pytest.mark.parametrize(
    "command",
    [
        "mms hook --host '#x' uninstall",  # a quoted ``#`` is a word; ``uninstall`` is left over
        "mms hook \r#x | cat",  # the shell keeps ``\r#x`` as a word and runs a pipeline
        "mms/. hook",  # a directory, as written
    ],
)
def test_is_stm_hook_command_leaves_a_pipeline_a_leftover_word_and_a_directory(
    command: str,
) -> None:
    assert _is_stm_hook_command(command) is False


# ── shell syntax the command split does not model (#1476) ────────────────────
#
# Each line below reads as a bridge run in ``shlex`` words, while the shell
# reads it differently (a second command, a comment, an expansion, a syntax
# error, a redirection). Recognizing it would let install rewrite and
# uninstall delete a handler on a reading the shell does not share.


@pytest.mark.parametrize(
    "command",
    [
        "mms\nhook --host claude",  # two commands: ``mms``, then ``hook …``
        "mms hook --host # --persist-query-text",  # a comment: runs ``mms hook --host``
        "mms hook --host 'claude",  # unbalanced quote: a syntax error, nothing runs
        "mms hook --host $HOOK_HOST",  # expanded and word-split first
        'mms hook --host "$H"',  # still expanded inside double quotes
        'mms hook --host "\\$H"',  # the shell passes ``$H``, ``shlex`` ``\\$H``
        "mms hook --host cl*",  # a glob: may become several words
        "mms hook --host [c]laude",
        "mms hook --host <<< --persist-query-text",  # here-string: the flag goes to stdin
        "mms hook --host clau\x00de",  # NUL cannot reach the shell intact
        "mms hook --host claude\r",  # ``\r`` is part of the word to the shell
        "env MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS=~ mms hook --host claude",
        "uv run --directory ~ memtomem-stm hook --host claude",
        "mms hook --host {claude}",
        # ``~user/`` and ``~+/`` name another directory than ``$HOME``.
        "~root/mms hook --host claude",
        "~+/mms hook --host claude",
    ],
)
def test_is_stm_hook_command_refuses_shell_syntax_the_split_does_not_model(
    command: str,
) -> None:
    assert _is_stm_hook_command(command) is False


@pytest.mark.parametrize(
    "command",
    [
        # Tilde expansion keeps the executable's basename.
        "~/.local/bin/mms hook --host claude",
        'mms hook --host "claude"',
        # Single quotes keep every character literal, to the shell and to shlex.
        "mms hook --host 'cl$a#u*de'",
        "uv run --directory '/tmp/line\nbreak' memtomem-stm hook --host claude",
        "/home/사용자/.local/bin/mms hook --host claude",
    ],
)
def test_is_stm_hook_command_keeps_quoted_and_plain_lines(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shell's reading; on Windows Click expands ``$`` and ``*`` itself.
    from memtomem_stm.cli import hook_registration

    monkeypatch.setattr(hook_registration, "running_on_windows", lambda: False)
    assert _is_stm_hook_command(command) is True


def test_is_stm_hook_command_still_refuses_a_quoted_operator_word() -> None:
    """The word-level operator guard predates the split's whitelist and still
    refuses a quoted operator, though the shell passes it as a plain word."""
    assert _is_stm_hook_command("mms hook --host '|'") is False


@pytest.mark.parametrize("host", sorted(HOOK_HOSTS))
@pytest.mark.parametrize(
    "root",
    [
        None,
        Path("/repo"),
        Path("/Users/x/My Repo"),
        Path("C:\\Users\\x\\repo"),
        # Every character the split refuses outside quotes, in install's own
        # output (``shlex.join`` single-quotes them).
        Path("/Users/x/it's $HOME #1 (*) ~ {a} <b> `c` !"),
        Path("/tmp/line\nbreak"),
        Path("/Users/사용자/repo"),
    ],
)
@pytest.mark.parametrize(
    "policy",
    [
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
    ],
)
def test_install_recognizes_every_command_it_writes(
    host: str, root: Path | None, policy: HostRuntimePolicy | None
) -> None:
    """Install's own output, built the way ``mms hook install`` builds it."""
    project = None if root is None else _StmProject(root=root, source_checkout=True)
    argv = _hook_install_command_argv(host, policy=policy, launcher=_launcher_for(project))
    assert _is_stm_hook_command(shlex.join(argv)) is True


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
    # Seed an OLD STM block (a different --host value / entry-point spelling).
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Read",
                            "hooks": [{"type": "command", "command": "mms hook --host claude"}],
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
                                    "command": "mms hook --host claude",
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
                                {"type": "command", "command": "mms hook --host claude"},
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
    assert sum(_is_stm_hook_command(c) for c in commands) == 1  # exactly one STM handler
    assert "memtomem-stm hook --host claude" in commands  # refreshed to the new spelling


def test_plan_install_dedups_multiple_stm_groups(redirect) -> None:
    # A config that somehow holds *two* STM groups (a hand-edit, or an old
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
                                {"type": "command", "command": "mms hook --host claude"},
                                {"type": "command", "command": "my-guard.sh"},
                            ],
                        },
                        {
                            "matcher": "Read|Grep|Glob|Bash",
                            "hooks": [{"type": "command", "command": "mms hook --host claude"}],
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
    assert sum(_is_stm_hook_command(c) for c in commands) == 1  # one STM handler total
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
    assert any(_is_stm_hook_command(c) for c in commands)


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
    command = installed_stm_hook_commands("claude")[0]
    assert " --use-daemon " in command
    assert "--surfacing-timeout-seconds 12" in command
    assert "--daemon-timeout-seconds 13" in command
    assert command.endswith("--no-persist-query-text")
    assert not command.startswith("env ")
    assert path.exists()


def test_cli_install_migrates_legacy_inline_env_and_preserves_timeout(redirect) -> None:
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
                                    "command": (
                                        "env MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS=12 "
                                        "memtomem-stm hook --host claude"
                                    ),
                                    "timeout": 30,
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["hook", "install", "--host", "claude", "--apply"])
    assert result.exit_code == 0, result.output
    command = installed_stm_hook_commands("claude")[0]
    assert not command.startswith("env ")
    assert "--surfacing-timeout-seconds 12" in command
    assert "--daemon-timeout-seconds 13" in command
    handler = json.loads(path.read_text())["hooks"]["PostToolUse"][0]["hooks"][0]
    assert handler["timeout"] == 30


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
    redirect("cursor")
    result = CliRunner().invoke(
        cli,
        ["hook", "install", "--host", "cursor", "--inherit-runtime-env", "--apply"],
    )
    assert result.exit_code == 0, result.output
    command = installed_stm_hook_commands("cursor")[0]
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
    """Whether a parsed host config carries an STM hook command (any shape)."""
    if host == "kimi":
        return any(_is_stm_hook_command(h.get("command", "")) for h in parsed.get("hooks", []))
    if host == "cursor":
        entries = parsed.get("hooks", {}).get("postToolUse", [])
        return any(_is_stm_hook_command(e.get("command", "")) for e in entries)
    # claude / codex — nested matcher groups
    for entry in parsed.get("hooks", {}).get("PostToolUse", []):
        for handler in entry.get("hooks", []):
            if _is_stm_hook_command(handler.get("command", "")):
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
# Install and uninstall still rewrite or remove only what ``_is_stm_hook_command``
# recognizes. Anything else that looks like STM's hook stays in the config and is
# named by location, so a duplicate is never silent. ``_BARE`` is the shape
# #1453 reported: install appended a second handler beside it, said nothing,
# and uninstall printed "Removed" while it kept running.

_BARE = "MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 /home/u/.local/bin/mms hook"
_MANAGED = "mms hook --host claude"


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
            {"version": 1, "hooks": {"postToolUse": [{"command": _MANAGED}, {"command": _BARE}]}},
            "hooks.postToolUse[1]",
            "hooks.postToolUse[0]",
        ),
        (
            "kimi",
            {
                "hooks": [
                    {"event": "PostToolUse", "command": _MANAGED},
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
        _h(_MANAGED),  # recognized: install manages it
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
    """Two STM-only groups fold into one, so the unmanaged group moves up."""
    path = redirect("claude")
    _seed(path, _claude_groups([_h(_MANAGED)], [_h("memtomem-stm hook")], [_h(_BARE)]))

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
    assert "will stay in the config" in preview.output

    applied = runner.invoke(cli, ["hook", "install", "--host", "claude", "--apply"])
    assert applied.exit_code == 0, applied.output
    assert "Installed" in applied.output
    assert f"in the written file: {current}" in applied.output
    assert "stays in the config" in applied.output

    again = runner.invoke(cli, ["hook", "install", "--host", "claude", "--apply"])
    assert "already installed" in again.output
    assert f"in the current file: {current}" in again.output

    removed = runner.invoke(cli, ["hook", "uninstall", "--host", "claude", "--apply"])
    assert "Removed the memtomem-stm hook install manages" in removed.output
    assert "stays in the config" in removed.output
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
        # Two STM-only groups fold into one, so the look-alike moves up.
        (
            "install",
            ([_h(_MANAGED)], [_h("memtomem-stm hook")], [_h(_BARE)]),
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
    assert len(commands) == 2 and _is_stm_hook_command(commands[1])

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
    """Kimi's install loop reads every entry whatever its ``event``, so the
    scan does too."""
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
        "so they stay in the config (in the written file: hooks.PostToolUse[0].hooks[0], "
        "hooks.PostToolUse[0].hooks[1]). Check them by hand"
    ) in result.output


# ── a line the split refuses is left alone and named (#1476) ─────────────────


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
    """Install's own match reads ``command`` alone, so a handler with no
    ``type`` that loses recognition is named too."""
    path = redirect("claude")
    _seed(path, _claude_groups([{"command": 'm"m"s hook --host # --persist-query-text'}]))

    assert plan_install("claude", _MANAGED).unmanaged == ("hooks.PostToolUse[0].hooks[0]",)


def test_the_notice_scan_splits_a_line_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recognition and the look-alike test share one split of a line
    (``shlex`` is quadratic in a word's length)."""
    import shlex as shlex_module

    from memtomem_stm.cli import host_runtime
    from memtomem_stm.cli.hook_hosts import unmanaged_stm_like

    calls: list[str] = []
    real = shlex_module.shlex

    def counting(command: str, *args: object, **kwargs: object) -> shlex_module.shlex:
        calls.append(command)
        return real(command, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(host_runtime.shlex, "shlex", counting)
    # Past ``is_candidate``'s own ``shlex`` limit (4,096 characters), as in
    # the 320 KB line the review measured.
    line = "echo '" + "x" * 5000 + "'"

    assert unmanaged_stm_like("claude", _claude_groups([_h(line)])) == ()
    assert calls == [line]


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


# ── Windows: Click expands the hook's own arguments (#1476) ──────────────────
#
# On Windows the ``mms`` entry point runs Click's ``_expand_args`` on every
# argument before parsing: ``~`` at the start, ``$VAR`` / ``%VAR%`` and globs.
# A word the shell passes as written can still reach ``mms hook`` as other
# words, or as several, depending on the hook's environment and directory.


@pytest.fixture
def on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    from memtomem_stm.cli import hook_registration

    monkeypatch.setattr(hook_registration, "running_on_windows", lambda: True)


_WINDOWS_EXPANDED = [
    "mms hook --host 'C*'",
    "mms hook --host cl'?'ude",
    "mms hook --host '[c]laude'",
    "mms hook --host %HOOK_HOST%",
    "mms hook --host '$HOOK_HOST'",
    "mms hook --host '~x'",
    "uv run --directory /repo memtomem-stm hook --host 'C*'",
]


@pytest.mark.parametrize("command", _WINDOWS_EXPANDED)
def test_windows_refuses_arguments_click_would_expand(command: str, on_windows) -> None:
    assert _is_stm_hook_command(command) is False


@pytest.mark.parametrize("command", _WINDOWS_EXPANDED[:2] + _WINDOWS_EXPANDED[4:6])
def test_the_same_quoted_words_are_recognized_elsewhere(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: without Click's Windows expansion these quoted words
    reach the hook as written."""
    from memtomem_stm.cli import hook_registration

    monkeypatch.setattr(hook_registration, "running_on_windows", lambda: False)
    assert _is_stm_hook_command(command) is True


@pytest.mark.parametrize("host", sorted(HOOK_HOSTS))
@pytest.mark.parametrize(
    "root", [None, Path("C:\\Users\\x\\repo"), Path("C:\\Users\\x\\it's $HOME %TEMP% (*) ~")]
)
def test_windows_recognizes_every_command_install_writes(
    host: str, root: Path | None, on_windows
) -> None:
    """The launcher's directory comes before the executable, so Click never
    sees it; the words after the executable are install's fixed flags."""
    project = None if root is None else _StmProject(root=root, source_checkout=True)
    policy = HostRuntimePolicy(
        use_daemon=False,
        surfacing_timeout_seconds=9.5,
        daemon_timeout_seconds=0.25,
        persist_query_text=True,
    )
    argv = _hook_install_command_argv(host, policy=policy, launcher=_launcher_for(project))
    assert _is_stm_hook_command(shlex.join(argv)) is True


def test_windows_reads_no_setting_from_an_expanded_line(on_windows) -> None:
    from memtomem_stm.cli.host_runtime import (
        PartialHostRuntimePolicy,
        parse_managed_hook_runtime,
    )

    assert (
        parse_managed_hook_runtime("mms hook --host 'C*' --persist-query-text")
        == PartialHostRuntimePolicy()
    )


@pytest.mark.parametrize(
    "command",
    [
        "mms hook --host 'C*'",
        # Quote removal joins the executable name, which the raw search misses.
        "m\"m\"s hook --host 'C*'",
        # ``~`` counts at the start of a word, not of the line.
        "m\"m\"s hook --host '~x'",
    ],
)
def test_windows_names_a_line_it_refuses(command: str, on_windows, redirect) -> None:
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


@pytest.mark.parametrize(
    ("line", "named"),
    [
        ("'" + "x" * 5000 + "'", False),  # recognized: install manages it
        ("'" + "x" * 4999 + "*'", True),  # refused by the Windows rule
        ("'" + "x" * 4999 + "%'", True),
        ("'" + "x" * 5000 + "' # refused by the split", True),
    ],
)
def test_windows_notice_scan_splits_a_line_once(
    line: str, named: bool, on_windows, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recognition and the look-alike test share one split of each line, also
    where the Windows rule refuses it."""
    import shlex as shlex_module

    from memtomem_stm.cli import host_runtime
    from memtomem_stm.cli.hook_hosts import unmanaged_stm_like

    calls: list[str] = []
    real = shlex_module.shlex

    def counting(command: str, *args: object, **kwargs: object) -> shlex_module.shlex:
        calls.append(command)
        return real(command, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(host_runtime.shlex, "shlex", counting)
    # Spelled so that ``is_candidate``'s raw search misses it, which leaves the
    # decision to the word test.
    command = f'm"m"s hook --host {line}'

    expected = ("hooks.PostToolUse[0].hooks[0]",) if named else ()
    assert unmanaged_stm_like("claude", _claude_groups([_h(command)])) == expected
    assert calls.count(command) == 1


def test_a_refused_line_the_candidate_test_names_is_not_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A line the split refuses is split for the look-alike test only when
    ``mms doctor``'s candidate test has not already named it."""
    import shlex as shlex_module

    from memtomem_stm.cli import host_runtime
    from memtomem_stm.cli.hook_hosts import unmanaged_stm_like

    calls: list[str] = []
    real = shlex_module.shlex

    def counting(command: str, *args: object, **kwargs: object) -> shlex_module.shlex:
        calls.append(command)
        return real(command, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(host_runtime.shlex, "shlex", counting)
    # Past ``is_candidate``'s ``shlex`` limit, so its raw search names it.
    line = "mms hook --host '" + "x" * 5000 + "' # pinned"

    assert unmanaged_stm_like("claude", _claude_groups([_h(line)])) == (
        "hooks.PostToolUse[0].hooks[0]",
    )
    assert calls == []
