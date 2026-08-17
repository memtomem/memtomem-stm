"""Regression tests for ``tests.helpers`` — small but load-bearing.

The 10 monkeypatch sites that previously set only ``HOME`` were migrated to
``set_home`` for memtomem-stm#302 P1b. If ``set_home`` ever stops patching
``USERPROFILE`` the Windows leg of the CI matrix silently regresses, so
keep a direct contract test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
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


@pytest.mark.parametrize("original", ["/original/home", None], ids=["present", "absent"])
def test_set_home_is_undone_after_test(tmp_path: Path, original: str | None) -> None:
    """monkeypatch must restore both env vars when the test exits — otherwise
    the next test in the same session inherits the sandbox HOME.

    Both starting states are staged explicitly rather than read off the ambient
    environment: ``os.environ.get("VAR") == None`` would silently pass even if
    monkeypatch had failed to *delete* a previously-absent key, so the absent
    case asserts the KEY is gone — and that case is unreachable from the
    ambient environment now that ``conftest``'s ``isolate_home`` always
    populates both (bare CI shells used to be the only place it ran).

    The staging and the patch under test need separate ``MonkeyPatch``
    instances, since ``undo()`` reverts everything its own instance did.
    """
    staged = pytest.MonkeyPatch()
    try:
        for var in ("HOME", "USERPROFILE"):
            if original is None:
                staged.delenv(var, raising=False)
            else:
                staged.setenv(var, original)

        patched = pytest.MonkeyPatch()
        set_home(patched, tmp_path)
        assert os.environ["HOME"] == str(tmp_path)  # the patch took effect
        patched.undo()

        for var in ("HOME", "USERPROFILE"):
            if original is None:
                assert var not in os.environ, f"{var} key not deleted"
            else:
                assert os.environ.get(var) == original, f"{var} not restored"
    finally:
        staged.undo()
