"""Which ``~/.claude.json`` project entry Claude Code uses for a directory (#1271).

Claude Code keeps per-project state, ``/mcp`` disables among it, under
``projects[<key>]``. The key is not the directory a session starts in. Measured
on Claude Code 2.1.316 (macOS), and matching the code in that executable:

* the nearest of the directory and its ancestors holding a ``.git`` file or
  directory is the repository, and its root is the key, so a session started
  in ``<repo>/src`` uses ``projects["<repo>"]``;
* a linked worktree whose git metadata checks out uses the main checkout's
  entry (the directory holding the common ``.git``), or the repository
  directory itself for a worktree of a bare repository;
* outside any repository the directory itself is the key, not a parent;
* the key is NFC-normalized, so a directory stored in NFD is filed under NFC;
* on Windows the separators are ``/`` (from the executable, not measured).

Only the filesystem is read: git never runs, since a repository's own git
config can run code.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import unicodedata
from pathlib import Path

# Git writes these files with one short path each. Claude Code reads them whole,
# STM reads at most this much, so a larger file is a layout STM does not follow.
_METADATA_LIMIT = 64 * 1024

# Paths Claude Code vets with checks STM does not reproduce: UNC and device
# paths, and a Windows drive letter not followed by a separator.
_UNSUPPORTED_PATH = re.compile(r"^(?:[\\/]{2}|[A-Za-z]:(?![\\/]))")


class _Unknown(Exception):
    """The layout takes a branch STM cannot reproduce, so the key is unknown."""


def claude_project_key(cwd: Path) -> str | None:
    """The ``projects`` key Claude Code uses for a session started in ``cwd``.

    ``None`` when STM cannot tell which entry Claude Code picks: ``cwd`` cannot
    be resolved, the ``.git`` marker is a symlink (Claude Code vets its target
    with checks not reproduced here), or worktree metadata is oversized, not
    UTF-8, or names a UNC, device or drive-relative path. A caller that would
    hide something based on the entry must treat ``None`` as "no entry".
    """
    try:
        start = cwd.resolve()
        root = _git_root(start)
        key = str(start) if root is None else _canonical_root(str(root))
    except (_Unknown, OSError, RuntimeError, ValueError):
        return None
    key = unicodedata.normalize("NFC", key)
    if sys.platform == "win32":
        key = key.replace("\\", "/")
    return key


def _git_root(start: Path) -> Path | None:
    """The nearest of ``start`` and its ancestors whose ``.git`` is a file or directory."""
    for candidate in (start, *start.parents):
        try:
            mode = (candidate / ".git").lstat().st_mode
        except OSError:
            # Claude Code reads any failure here as "no marker" and keeps walking.
            continue
        if stat.S_ISLNK(mode):
            raise _Unknown
        if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
            return candidate
    return None


def _canonical_root(root: str) -> str:
    """Map a linked worktree's root to the entry Claude Code files it under.

    Every check Claude Code makes before following the worktree's metadata is
    made here in the same order, and a failed one falls back to ``root``, as
    there.
    """
    git = os.path.join(root, ".git")
    try:
        if stat.S_ISDIR(os.lstat(git).st_mode):
            return root
        content = _read_metadata(git).strip()
        if not content.startswith("gitdir:"):
            return root
        gitdir = _checked_path(content[len("gitdir:") :].strip())
        admin = os.path.normpath(os.path.join(root, gitdir))
        commondir_file = os.path.join(admin, "commondir")
        if not _is_regular_file(commondir_file):
            return root
        common = os.path.normpath(
            os.path.join(admin, _checked_path(_read_metadata(commondir_file).strip()))
        )
        if os.path.normpath(os.path.dirname(admin)) != os.path.join(common, "worktrees"):
            return root
        backpointer_file = os.path.join(admin, "gitdir")
        if not _is_regular_file(backpointer_file):
            return root
        backpointer = _checked_path(_read_metadata(backpointer_file).strip())
        if os.sep == "/" and any("\\" in p for p in (root, admin, backpointer, common)):
            return root
        if os.path.realpath(os.path.join(admin, backpointer)) != os.path.join(
            os.path.realpath(root), ".git"
        ):
            return root
        if os.path.basename(common) != ".git":
            # A bare repository's common directory, unless it is itself inside
            # a checkout.
            return root if _has_git_marker(common) else common
        return os.path.dirname(common)
    except OSError:
        return root


def _read_metadata(path: str) -> str:
    with open(path, "rb") as fh:
        raw = fh.read(_METADATA_LIMIT + 1)
    if len(raw) > _METADATA_LIMIT:
        raise _Unknown
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Unknown from None


def _checked_path(value: str) -> str:
    if _UNSUPPORTED_PATH.match(value):
        raise _Unknown
    return value


def _is_regular_file(path: str) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _has_git_marker(directory: str) -> bool:
    try:
        mode = os.lstat(os.path.join(directory, ".git")).st_mode
    except OSError:
        return False
    if stat.S_ISLNK(mode):
        raise _Unknown
    return stat.S_ISDIR(mode) or stat.S_ISREG(mode)
