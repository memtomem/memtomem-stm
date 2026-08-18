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
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner
from helpers import set_home

from memtomem_stm.cli.hook_adapter import READLIKE_SURFACE_TOOLS, known_hosts
from memtomem_stm.cli.hook_cmd import _DEFAULT_SURFACE_TOOLS
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
from memtomem_stm.cli.proxy import cli

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
