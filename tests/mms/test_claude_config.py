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

from helpers import GIT_FILE_LAYOUTS, set_home, skip_if_inside_git
from memtomem_stm.mms.claude_config import (
    ClaudeConfigUnknown,
    claude_global_config,
    claude_project_key,
    read_claude_global_config,
)

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


# ---------------------------------------------------------------------------
# claude_global_config (#1271)
#
# Expected files are the ones Claude Code 2.1.316 wrote (2026-11-01, macOS):
# ``claude mcp add -s user`` with HOME and CLAUDE_CONFIG_DIR in a scratch dir,
# recording which file received the entry. They are spelled as literal paths
# under tmp_path, not derived from the resolver.
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    set_home(monkeypatch, home)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", raising=False)
    return home


def _unknown(reason: str) -> ClaudeConfigUnknown:
    return ClaudeConfigUnknown(reason)


class TestGlobalConfigMeasured:
    def test_plain_home(self, home):
        assert claude_global_config() == home / ".claude.json"

    def test_absolute_config_dir(self, home, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))

        assert claude_global_config() == tmp_path / "cfg" / ".claude.json"

    def test_config_dir_with_trailing_slash(self, home, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg") + os.sep)

        assert claude_global_config() == tmp_path / "cfg" / ".claude.json"

    def test_nonempty_legacy_file_is_unknown(self, home):
        # Measured: Claude Code wrote to ~/.claude/.config.json.
        (home / ".claude" / ".config.json").write_text("{}")

        assert claude_global_config() == _unknown("legacy_config")

    def test_empty_legacy_file_is_unknown_even_beside_the_main_file(self, home):
        # Measured: with ~/.claude.json present Claude Code used it, without it
        # the empty legacy file. STM does not replicate the rule.
        (home / ".claude" / ".config.json").write_text("")
        (home / ".claude.json").write_text("{}")

        assert claude_global_config() == _unknown("legacy_config")

    def test_legacy_file_in_the_config_dir_is_unknown(self, home, tmp_path, monkeypatch):
        # Measured: CLAUDE_CONFIG_DIR=D with D/.config.json -> D/.config.json.
        (tmp_path / "cfg").mkdir()
        (tmp_path / "cfg" / ".config.json").write_text("{}")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))

        assert claude_global_config() == _unknown("legacy_config")

    def test_home_legacy_file_does_not_count_under_a_config_dir(
        self, home, tmp_path, monkeypatch
    ):
        (home / ".claude" / ".config.json").write_text("{}")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))

        assert claude_global_config() == tmp_path / "cfg" / ".claude.json"

    def test_empty_config_dir_is_unknown(self, home, monkeypatch):
        # Measured: CLAUDE_CONFIG_DIR= made Claude Code use ./.config.json.
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")

        assert claude_global_config() == _unknown("config_dir")

    def test_custom_oauth_is_unknown(self, home, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", "https://example.invalid/oauth")

        assert claude_global_config() == _unknown("custom_oauth")


class TestGlobalConfigUnknown:
    @pytest.mark.parametrize("value", ["   ", "relative/cfg", "~/cfg", "./cfg"])
    def test_config_dir_that_is_not_absolute(self, home, monkeypatch, value):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)

        assert claude_global_config() == _unknown("config_dir")

    @pytest.mark.parametrize("tail", ["link/..", "a/./b", "a/../b"])
    def test_config_dir_with_dot_components(self, home, tmp_path, monkeypatch, tail):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path) + "/" + tail)

        assert claude_global_config() == _unknown("config_dir")

    def test_dot_dot_through_a_symlink_names_another_file(self, home, tmp_path, monkeypatch):
        """Why ``..`` is refused: Node joins ``/a/link/..`` to ``/a``, the OS
        resolves it through the link to ``/b``, and both files exist."""
        (tmp_path / "a").mkdir()
        (tmp_path / "b" / "child").mkdir(parents=True)
        (tmp_path / "a" / "link").symlink_to(tmp_path / "b" / "child")
        (tmp_path / "a" / ".claude.json").write_text("A")
        (tmp_path / "b" / ".claude.json").write_text("B")
        value = str(tmp_path / "a") + "/link/.."
        assert (Path(value) / ".claude.json").read_text() == "B"  # what Python reads
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)

        assert claude_global_config() == _unknown("config_dir")

    @pytest.mark.parametrize("value", [None, "", "relative/home"])
    def test_unusable_home(self, home, monkeypatch, value):
        var = "USERPROFILE" if sys.platform == "win32" else "HOME"
        if value is None:
            monkeypatch.delenv(var)
        else:
            monkeypatch.setenv(var, value)

        assert claude_global_config() == _unknown("home")

    def test_unusable_home_does_not_matter_under_a_config_dir(
        self, home, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", "relative/home")
        monkeypatch.setenv("USERPROFILE", "relative/home")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))

        assert claude_global_config() == tmp_path / "cfg" / ".claude.json"

    def test_legacy_directory_is_unknown(self, home):
        (home / ".claude" / ".config.json").mkdir()

        assert claude_global_config() == _unknown("legacy_config")

    def test_legacy_symlink_is_unknown_even_when_dangling(self, home, tmp_path):
        (home / ".claude" / ".config.json").symlink_to(tmp_path / "missing")

        assert claude_global_config() == _unknown("legacy_config")

    def test_legacy_probe_error_is_unknown(self, home, monkeypatch):
        real_lstat = Path.lstat

        def lstat(self):
            if self.name == ".config.json":
                raise PermissionError("denied")
            return real_lstat(self)

        monkeypatch.setattr(Path, "lstat", lstat)

        assert claude_global_config() == _unknown("legacy_config")

    def test_missing_config_home_is_absent_not_unknown(self, home, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nowhere"))

        assert claude_global_config() == tmp_path / "nowhere" / ".claude.json"

    def test_config_home_that_is_a_file_is_absent_not_unknown(
        self, home, tmp_path, monkeypatch
    ):
        (tmp_path / "file").write_text("x")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "file"))

        assert claude_global_config() == tmp_path / "file" / ".claude.json"

    def test_legacy_probe_uses_the_nfc_config_home(self, home, tmp_path, monkeypatch):
        """``Ne()`` NFC-normalizes the config home before joining
        ``.config.json``; the primary path keeps its own spelling."""
        nfc_dir = tmp_path / NFC_NAME
        nfc_dir.mkdir()
        (nfc_dir / ".config.json").write_text("{}")
        nfd_value = unicodedata.normalize("NFD", str(nfc_dir))
        if Path(nfd_value).exists():
            # APFS and NTFS-like lookups ignore normalization, so the NFD
            # spelling finds the NFC directory and this proves nothing there.
            pytest.skip("the filesystem matches names regardless of normalization")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", nfd_value)

        assert claude_global_config() == _unknown("legacy_config")


class TestReadGlobalConfig:
    def test_missing_file_is_inspected_and_empty(self, home):
        read = read_claude_global_config()

        assert (read.path, read.data, read.unread) == (home / ".claude.json", None, None)

    @pytest.mark.parametrize("raw", [b"{not json", b"[]", b'"x"', b'{"a": "\xff"}'])
    def test_unreadable_file_is_not_inspected(self, home, raw):
        (home / ".claude.json").write_bytes(raw)

        read = read_claude_global_config()

        assert (read.data, read.unread) == (None, "unreadable")

    def test_object_is_read(self, home):
        (home / ".claude.json").write_text('{"mcpServers": {}}')

        assert read_claude_global_config().data == {"mcpServers": {}}

    def test_unknown_config_reads_nothing(self, home, monkeypatch):
        (home / ".claude.json").write_text('{"mcpServers": {"x": {}}}')
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative")

        read = read_claude_global_config()

        assert (read.path, read.data, read.unread) == (None, None, "config_dir")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path forms")
class TestGlobalConfigWindows:
    @pytest.mark.parametrize("value", ["\\cfg", "/cfg", "C:cfg"])
    def test_drive_dependent_config_dir_is_unknown(self, home, monkeypatch, value):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)

        assert claude_global_config() == _unknown("config_dir")

    @pytest.mark.parametrize("value", ["\\Users\\me", "C:Users\\me"])
    def test_drive_dependent_profile_is_unknown(self, home, monkeypatch, value):
        monkeypatch.setenv("USERPROFILE", value)

        assert claude_global_config() == _unknown("home")

    def test_unc_config_dir_is_used(self, home, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "\\\\srv\\share\\cfg")

        assert claude_global_config() == Path("\\\\srv\\share\\cfg") / ".claude.json"
