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
    assert check["detail"].startswith(f"1 of 2 STM hook command(s) in {path} lack --host claude")
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
