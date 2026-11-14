"""Tests for mms/host_homes.py — where a host's home variable points (#1345)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from memtomem_stm.mms.host_homes import (
    HOST_HOME_UNKNOWN_REASONS,
    HostHomeUnknown,
    host_home_override,
)

_VARIABLES = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "kimi": "KIMI_CODE_HOME"}
_CODES = {"claude": "config_dir", "codex": "codex_home", "kimi": "kimi_home"}


@pytest.mark.parametrize("host", sorted(_VARIABLES))
def test_unset_variable_leaves_the_default_to_the_caller(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_VARIABLES[host], raising=False)
    assert host_home_override(host) is None


@pytest.mark.parametrize("host", sorted(_VARIABLES))
def test_absolute_value_is_the_home(
    host: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_VARIABLES[host], str(tmp_path / "cfg"))
    assert host_home_override(host) == tmp_path / "cfg"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        # Codex treats an empty CODEX_HOME as unset; Kimi Code and Claude Code
        # resolve it against their own working directory.
        ("codex", None),
        ("kimi", HostHomeUnknown("kimi_home")),
        ("claude", HostHomeUnknown("config_dir")),
    ],
)
def test_empty_value(
    host: str, expected: HostHomeUnknown | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_VARIABLES[host], "")
    assert host_home_override(host) == expected


@pytest.mark.parametrize("host", sorted(_VARIABLES))
@pytest.mark.parametrize("form", ["relative", "tilde", "dotdot", "dot"])
def test_a_value_stm_cannot_place_is_unknown(
    host: str, form: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = {
        "relative": "cfg",
        # Neither the hosts nor this module expand a literal tilde.
        "tilde": "~/cfg",
        "dotdot": str(tmp_path / "a" / ".." / "cfg"),
        # Path() drops a "." component, so spell it out.
        "dot": f"{tmp_path}{os.sep}.{os.sep}cfg",
    }[form]
    monkeypatch.setenv(_VARIABLES[host], value)
    assert host_home_override(host) == HostHomeUnknown(_CODES[host])


@pytest.mark.parametrize("host", sorted(_VARIABLES))
def test_reason_names_the_variable(host: str) -> None:
    assert HOST_HOME_UNKNOWN_REASONS[_CODES[host]].startswith(_VARIABLES[host] + " is ")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path forms")
@pytest.mark.parametrize("host", sorted(_VARIABLES))
@pytest.mark.parametrize("value", ["C:cfg", "\\cfg", "/cfg"])
def test_windows_drive_relative_and_rooted_values_are_unknown(
    host: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_VARIABLES[host], value)
    assert host_home_override(host) == HostHomeUnknown(_CODES[host])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path forms")
@pytest.mark.parametrize("host", sorted(_VARIABLES))
@pytest.mark.parametrize("value", ["C:\\cfg", "\\\\server\\share\\cfg"])
def test_windows_absolute_values_are_the_home(
    host: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_VARIABLES[host], value)
    assert host_home_override(host) == Path(value)
