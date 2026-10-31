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

from helpers import CLAUDE_UNKNOWN_CASES, claude_unknown_worktree, git_worktree, skip_if_inside_git
from memtomem_stm.mms import claude_config
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

    def test_linked_worktree_keys_the_main_checkout(self, base):
        (base / "C" / "main" / ".git").mkdir(parents=True)
        git_worktree(base / "C" / "main" / ".git", base / "C" / "wt", "wt")
        (base / "C" / "wt" / "src").mkdir()

        assert claude_project_key(base / "C" / "wt" / "src") == _key(base / "C" / "main")

    def test_backpointer_naming_another_path_keys_thegit_worktree(self, base):
        (base / "D" / "main" / ".git").mkdir(parents=True)
        elsewhere = base / "D" / "elsewhere" / ".git"
        git_worktree(
            base / "D" / "main" / ".git", base / "D" / "wt", "wt", backpointer=str(elsewhere)
        )
        (base / "D" / "wt" / "src").mkdir()

        assert claude_project_key(base / "D" / "wt" / "src") == _key(base / "D" / "wt")

    def test_submodule_style_git_file_keys_its_own_root(self, base):
        modules = base / "E" / "super" / ".git" / "modules" / "m"
        modules.mkdir(parents=True)
        (base / "E" / "m").mkdir()
        (base / "E" / "m" / ".git").write_text(f"gitdir: {modules}\n", encoding="utf-8")

        assert claude_project_key(base / "E" / "m") == _key(base / "E" / "m")

    def test_worktree_of_a_bare_repository_keys_the_repository_directory(self, base):
        bare = base / "F" / "bare.git"
        bare.mkdir(parents=True)
        git_worktree(bare, base / "F" / "wt", "wt")

        assert claude_project_key(base / "F" / "wt") == _key(bare)

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

    def test_relative_gitdir_keys_the_main_checkout(self, base):
        (base / "H" / "main" / ".git").mkdir(parents=True)
        admin = git_worktree(base / "H" / "main" / ".git", base / "H" / "wt", "wt")
        (base / "H" / "wt" / ".git").write_text(
            f"gitdir: {os.path.relpath(admin, base / 'H' / 'wt')}\n", encoding="utf-8"
        )
        (base / "H" / "wt" / "src").mkdir()

        assert claude_project_key(base / "H" / "wt" / "src") == _key(base / "H" / "main")

    def test_symlinked_git_marker_is_unknown(self, base):
        # Claude Code keyed this layout by ``I/repo``, after vetting the link
        # target with checks STM does not reproduce, so STM does not guess.
        (base / "I" / "real.git").mkdir(parents=True)
        (base / "I" / "repo" / "src").mkdir(parents=True)
        try:
            (base / "I" / "repo" / ".git").symlink_to(base / "I" / "real.git")
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")

        assert claude_project_key(base / "I" / "repo" / "src") is None


class TestFallbacks:
    def test_git_file_without_gitdir_line_keys_its_root(self, base):
        (base / "R").mkdir()
        (base / "R" / ".git").write_text("not a pointer\n", encoding="utf-8")

        assert claude_project_key(base / "R") == _key(base / "R")

    def test_missing_admin_directory_keys_thegit_worktree(self, base):
        (base / "W").mkdir()
        (base / "W" / ".git").write_text(f"gitdir: {base / 'gone'}\n", encoding="utf-8")

        assert claude_project_key(base / "W") == _key(base / "W")


class TestUnknown:
    @pytest.mark.parametrize("case", CLAUDE_UNKNOWN_CASES)
    def test_metadata_stm_does_not_follow_is_unknown(self, base, case):
        wt, _main = claude_unknown_worktree(base, case, claude_config._METADATA_LIMIT)

        assert claude_project_key(wt) is None

    def test_unresolvable_cwd_is_unknown(self, base, monkeypatch):
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
