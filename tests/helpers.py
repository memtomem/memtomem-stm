"""Shared cross-platform test helpers.

Kept deliberately small — fixtures live in ``conftest.py``; this module
holds plain functions that are easier to grep for and call inline.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def set_home(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Pin the user's home directory to ``path`` on every platform.

    ``Path.home()`` and ``os.path.expanduser("~")`` consult ``USERPROFILE``
    before ``HOME`` on Windows. Tests that monkeypatch only ``HOME`` therefore
    leak the developer's real profile into temp-dir fixtures on Windows
    runners. Patching both keeps the sandbox hermetic.
    """
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
