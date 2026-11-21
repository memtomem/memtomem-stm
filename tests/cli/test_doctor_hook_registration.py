"""``mms doctor``'s ``hook_registration`` check (#1437).

Bash output compression replaces output only for a hook registered with
``--host claude``; the check reports Claude Code registrations that would
refuse it while compression is enabled.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import cli
from helpers import set_home


@pytest.fixture
def claude_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    set_home(monkeypatch, tmp_path / "home")
    config_dir = tmp_path / "claude"
    config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    for key in ("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED",):
        monkeypatch.delenv(key, raising=False)
    return config_dir


def _write_hooks(config_dir: Path, *commands: str) -> Path:
    path = config_dir / "settings.json"
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Read|Grep|Glob|Bash",
                            "hooks": [{"type": "command", "command": c} for c in commands],
                        }
                    ]
                }
            }
        )
    )
    return path


def _doctor_checks(tmp_path: Path) -> dict[str, dict]:
    # A missing proxy config short-circuits the probes; the env-path checks,
    # this one included, still run.
    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(tmp_path / "absent.json"), "--json"]
    )
    return {check["id"]: check for check in json.loads(result.stdout)["checks"]}


def test_bare_registration_warns_with_the_install_fix(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    path = _write_hooks(claude_dir, "mms hook", "mms hook --host claude")

    check = _doctor_checks(tmp_path)["hook_registration"]

    assert check["status"] == "WARN"
    assert check["detail"] == (
        f"1 of 2 STM hook command(s) in {path} lack --host claude, "
        "so hook compression never replaces their output"
    )
    assert check["next_action"] == "mms hook install --host claude --apply"


@pytest.mark.parametrize(
    "command",
    [
        "mms hook --host claude --help",
        "mms hook --host claude --typo",
        "mms hook --host claude install",
        "mms hook --host claude --surfacing-timeout-seconds",
        'mms hook --host claude --daemon-timeout-seconds "10',
    ],
)
def test_command_that_never_runs_the_hook_warns(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch, command: str
):
    """``--host claude`` alone is not a PASS: these print help, exit 2, or run
    a subcommand, so the host gets no hook output at all."""
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    path = _write_hooks(claude_dir, command)

    check = _doctor_checks(tmp_path)["hook_registration"]

    assert check["status"] == "WARN"
    assert check["detail"] == (
        f"1 of 1 STM hook command(s) in {path} do not run the hook "
        "(a usage error, --help, a subcommand, or an unmatched quote)"
    )
    assert check["next_action"] == "mms hook install --host claude --apply"


def test_both_problems_are_reported(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    path = _write_hooks(claude_dir, "mms hook", "mms hook --host claude --typo")

    check = _doctor_checks(tmp_path)["hook_registration"]

    assert check["detail"] == (
        f"1 of 2 STM hook command(s) in {path} lack --host claude, "
        "so hook compression never replaces their output; "
        f"1 of 2 STM hook command(s) in {path} do not run the hook "
        "(a usage error, --help, a subcommand, or an unmatched quote)"
    )


def test_inline_compression_env_gets_a_hand_edit_instead_of_install(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """Install would rewrite the command without the inline opt-in, leaving
    compression off; the advice must not send the operator there."""
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    _write_hooks(claude_dir, "env MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 mms hook")

    check = _doctor_checks(tmp_path)["hook_registration"]

    assert check["status"] == "WARN"
    assert check["next_action"] == (
        "add --host claude to the commands that lack it, by hand: "
        "`mms hook install --host claude --apply` would replace every STM hook command "
        "in that file and drop their inline MEMTOMEM_STM_HOOK__COMPRESSION__* settings"
    )


def test_inline_compression_on_an_unflagged_command_still_blocks_the_install_advice(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """Install consolidates every STM handler into one fresh command, so the
    `8000` override on the passing command would be lost too."""
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    _write_hooks(
        claude_dir,
        "mms hook",
        "env MEMTOMEM_STM_HOOK__COMPRESSION__MAX_CHARS=8000 mms hook --host claude",
    )

    check = _doctor_checks(tmp_path)["hook_registration"]

    assert check["detail"].startswith("1 of 2 STM hook command(s)")
    assert check["next_action"].startswith("add --host claude to the commands that lack it")
    assert "drop their inline MEMTOMEM_STM_HOOK__COMPRESSION__* settings" in check["next_action"]


def test_broken_inline_command_is_told_to_drop_the_bad_argument(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    """It already has --host claude; asking for it again would leave it
    printing help."""
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    _write_hooks(
        claude_dir, "env MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 mms hook --host claude --help"
    )

    check = _doctor_checks(tmp_path)["hook_registration"]

    assert check["next_action"].startswith(
        "remove the invalid argument, --help, subcommand or unmatched quote "
        "from the commands that do not run the hook, by hand:"
    )
    assert "add --host claude" not in check["next_action"]


def test_install_consolidates_every_stm_handler(claude_dir: Path):
    """The premise of the cross-command check above, against the real planner."""
    import json as _json
    import os

    from memtomem_stm.cli import hook_hosts

    assert Path(os.environ["CLAUDE_CONFIG_DIR"]) == claude_dir
    _write_hooks(
        claude_dir,
        "mms hook",
        "env MEMTOMEM_STM_HOOK__COMPRESSION__MAX_CHARS=8000 mms hook --host claude",
    )

    change = hook_hosts.plan_install("claude", "mms hook --host claude")

    handlers = _json.loads(change.new_text)["hooks"]["PostToolUse"][0]["hooks"]
    assert [h["command"] for h in handlers] == ["mms hook --host claude"]


def test_inline_env_without_compression_settings_keeps_the_install_advice(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    _write_hooks(claude_dir, "env MEMTOMEM_STM_HOOK__USE_DAEMON=0 mms hook")

    check = _doctor_checks(tmp_path)["hook_registration"]

    assert check["next_action"] == "mms hook install --host claude --apply"


@pytest.mark.parametrize(
    "command",
    [
        "mms hook --host claude --host auto",
        "mms hook --host codex",
        "mms hook --host",
    ],
)
def test_effective_host_other_than_claude_warns(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch, command: str
):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    _write_hooks(claude_dir, command)

    assert _doctor_checks(tmp_path)["hook_registration"]["status"] == "WARN"


@pytest.mark.parametrize(
    "command",
    [
        "mms hook --host claude",
        "mms hook --host=claude --no-daemon",
        "mms hook --host auto --host claude",
        "uv run --directory /src/memtomem-stm memtomem-stm hook --host claude",
    ],
)
def test_explicit_claude_registration_passes(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch, command: str
):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    _write_hooks(claude_dir, command)

    check = _doctor_checks(tmp_path)["hook_registration"]

    assert check["status"] == "PASS"
    assert "project and local Claude Code settings are not checked" in check["detail"]


def test_no_stm_hook_passes_and_names_what_was_read(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    path = _write_hooks(claude_dir, "echo hook")

    check = _doctor_checks(tmp_path)["hook_registration"]

    assert check["status"] == "PASS"
    assert f"no STM hook in {path}" in check["detail"]


def test_unreadable_settings_warn_without_quoting_the_file(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    (claude_dir / "settings.json").write_text('{"hooks": SECRET-CANARY')

    check = _doctor_checks(tmp_path)["hook_registration"]

    assert check["status"] == "WARN"
    assert check["detail"] == (
        "hook compression is enabled but the Claude Code hook config could not be "
        "read (HookInstallError); not checked"
    )
    assert check["next_action"] is None


def test_unresolvable_claude_home_warns(
    tmp_path: Path, claude_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative/dir")

    check = _doctor_checks(tmp_path)["hook_registration"]

    assert check["status"] == "WARN"
    assert "could not be read (HookInstallError)" in check["detail"]


def test_no_check_while_compression_is_off(tmp_path: Path, claude_dir: Path):
    _write_hooks(claude_dir, "mms hook")

    assert "hook_registration" not in _doctor_checks(tmp_path)


def test_install_still_drops_inline_compression_settings():
    """The premise of the hand-edit advice above. If install starts carrying
    these assignments over, this fails and the advice should go back to the
    install command."""
    import shlex

    from memtomem_stm.cli.hook_cmd import _hook_install_command_argv
    from memtomem_stm.cli.host_runtime import resolve_host_runtime_policy

    existing = "env MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 mms hook"
    policy = resolve_host_runtime_policy(
        existing_command=existing, use_daemon=None, surfacing_timeout_seconds=None
    )
    rewritten = shlex.join(_hook_install_command_argv("claude", policy=policy))

    assert "--host claude" in rewritten
    assert "MEMTOMEM_STM_HOOK__COMPRESSION__" not in rewritten
