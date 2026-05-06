"""Regression tests for ``tests.helpers`` — small but load-bearing.

The 10 monkeypatch sites that previously set only ``HOME`` were migrated to
``set_home`` for memtomem-stm#302 P1b. If ``set_home`` ever stops patching
``USERPROFILE`` the Windows leg of the CI matrix silently regresses, so
keep a direct contract test.
"""

from __future__ import annotations

import os
from pathlib import Path

from helpers import set_home


def test_set_home_patches_home_and_userprofile(tmp_path: Path, monkeypatch):
    set_home(monkeypatch, tmp_path)
    assert os.environ["HOME"] == str(tmp_path)
    assert os.environ["USERPROFILE"] == str(tmp_path)


def test_set_home_path_home_resolves_to_sandbox(tmp_path: Path, monkeypatch):
    """``Path.home()`` reads HOME on POSIX and USERPROFILE on Windows; setting
    both means the assertion holds on every platform without a ``sys.platform``
    branch in the test."""
    set_home(monkeypatch, tmp_path)
    assert Path.home() == tmp_path


def test_set_home_is_undone_after_test(tmp_path: Path, monkeypatch):
    """monkeypatch must restore both env vars when the test exits — otherwise
    the next test in the same session inherits the sandbox HOME.

    Note that ``os.environ.get("VAR") == None`` would silently pass even if
    monkeypatch had failed to *delete* a previously-absent key (since both
    cases ``get`` returns ``None``). When the original is absent, assert
    the key is not in ``os.environ`` so a regression that left a None-string
    behind would still trip — important for bare CI shells where ``HOME``
    or ``USERPROFILE`` may not be pre-populated.
    """
    snapshots = {
        var: ("HAS" if var in os.environ else "ABSENT", os.environ.get(var))
        for var in ("HOME", "USERPROFILE")
    }

    set_home(monkeypatch, tmp_path)
    monkeypatch.undo()

    for var, (state, original) in snapshots.items():
        if state == "HAS":
            assert os.environ.get(var) == original, f"{var} not restored"
        else:
            assert var not in os.environ, f"{var} key not deleted"
