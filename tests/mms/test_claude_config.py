"""The ``~/.claude.json`` project key Claude Code uses for a directory (#1271).

The expected keys are the ones Claude Code 2.1.316 wrote (2026-10-31, macOS):
``claude mcp add -s local`` was run from each start directory of hand-built
layouts like these, with ``CLAUDE_CONFIG_DIR`` pointing at a scratch config,
and the ``projects`` key that received the entry was recorded. They are spelled
here as paths under ``tmp_path``, not derived from the resolver.
"""

from __future__ import annotations

import os
import sys
import unicodedata
from pathlib import Path

import pytest

from helpers import GIT_FILE_LAYOUTS, skip_if_inside_git
from memtomem_stm.mms.claude_config import claude_project_key

NFD_NAME = unicodedata.normalize("NFD", "한글repo")
NFC_NAME = unicodedata.normalize("NFC", "한글repo")


def _key(path: Path) -> str:
    """How Claude Code spells ``path`` as a key: NFC, and ``/`` on Windows."""
    text = unicodedata.normalize("NFC", str(path))
    return text.replace("\\", "/") if sys.platform == "win32" else text


@pytest.fixture
def base(tmp_path):
    root = tmp_path.resolve()
    skip_if_inside_git(root)
    return root


class TestMeasuredLayouts:
    def test_no_repository_keys_the_directory_itself(self, base):
        (base / "A" / "sub").mkdir(parents=True)

        assert claude_project_key(base / "A" / "sub") == _key(base / "A" / "sub")

    def test_subdirectory_keys_the_repository_root(self, base):
        (base / "B" / ".git").mkdir(parents=True)
        (base / "B" / "src").mkdir()

        assert claude_project_key(base / "B" / "src") == _key(base / "B")

    def test_nearest_git_directory_wins(self, base):
        (base / "outer" / ".git").mkdir(parents=True)
        (base / "outer" / "inner" / ".git").mkdir(parents=True)
        (base / "outer" / "inner" / "src").mkdir()

        assert claude_project_key(base / "outer" / "inner" / "src") == _key(
            base / "outer" / "inner"
        )

    def test_nfd_directory_is_keyed_in_nfc(self, base):
        repo = base / "G" / NFD_NAME
        (repo / ".git").mkdir(parents=True)
        (repo / "src").mkdir()
        listed = os.listdir(base / "G")
        if listed != [NFD_NAME]:
            pytest.skip(f"the filesystem does not keep NFD names ({listed!r})")

        key = claude_project_key(repo / "src")

        assert key == _key(base / "G" / NFC_NAME)
        assert key is not None and unicodedata.is_normalized("NFC", key)


class TestNoKey:
    @pytest.mark.parametrize("layout", sorted(GIT_FILE_LAYOUTS))
    def test_git_file_or_symlink_gives_no_key(self, base, layout):
        start, _main = GIT_FILE_LAYOUTS[layout](base)

        assert claude_project_key(start) is None

    def test_unresolvable_cwd_gives_no_key(self, base, monkeypatch):
        def fail(self, strict=False):
            raise OSError("boom")

        monkeypatch.setattr(Path, "resolve", fail)

        assert claude_project_key(base) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows key form")
def test_windows_key_uses_forward_slashes(base):
    (base / "B" / ".git").mkdir(parents=True)
    (base / "B" / "src").mkdir()

    key = claude_project_key(base / "B" / "src")

    assert key is not None and "\\" not in key
    assert key == str(base / "B").replace("\\", "/")
