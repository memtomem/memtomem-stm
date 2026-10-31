"""Which ``~/.claude.json`` project entry Claude Code uses for a directory (#1271).

Claude Code keeps per-project state, ``/mcp`` disables among it, under
``projects[<key>]``. The key is not the directory a session starts in. Measured
on Claude Code 2.1.316 (macOS), and matching the code in that executable:

* the nearest of the directory and its ancestors holding a ``.git`` directory
  is the repository, and its root is the key, so a session started in
  ``<repo>/src`` uses ``projects["<repo>"]``;
* outside any repository the directory itself is the key, not a parent;
* the key is NFC-normalized, so a directory stored in NFD is filed under NFC;
* on Windows the separators are ``/`` (from the executable, not measured).

A ``.git`` that is a file (a linked worktree, a submodule) or a symlink gives
no key. Claude Code follows a worktree's metadata to the main checkout, but
only after path checks STM does not reproduce, and a key that names the main
checkout where Claude Code fell back to the worktree would apply that
checkout's disables to the wrong session.

Only the filesystem is read: git never runs, since a repository's own git
config can run code.
"""

from __future__ import annotations

import stat
import sys
import unicodedata
from pathlib import Path


def claude_project_key(cwd: Path) -> str | None:
    """The ``projects`` key Claude Code uses for a session started in ``cwd``.

    ``None`` when STM cannot tell which entry Claude Code picks: ``cwd``
    cannot be resolved, or the nearest ``.git`` is a file or a symlink. A
    caller that would hide something based on the entry must treat ``None``
    as "no entry".
    """
    try:
        start = cwd.resolve()
        for candidate in (start, *start.parents):
            try:
                mode = (candidate / ".git").lstat().st_mode
            except OSError:
                # Claude Code reads any failure here as "no marker" and keeps walking.
                continue
            if stat.S_ISDIR(mode):
                key = str(candidate)
                break
            if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                return None
        else:
            key = str(start)
    except (OSError, RuntimeError, ValueError):
        return None
    key = unicodedata.normalize("NFC", key)
    if sys.platform == "win32":
        key = key.replace("\\", "/")
    return key
