"""Which config file and project entry Claude Code uses (#1271).

Two coordinates, both measured on Claude Code 2.1.316 and kept apart from the
directory a server runs in (Claude Code spawns a local-scope server in the
session's start directory, whatever slot it came from).

**The config file** (:func:`claude_global_config`). Claude Code reads
``$CLAUDE_CONFIG_DIR/.claude.json`` when that variable is nonempty and
``~/.claude.json`` otherwise, unless a legacy ``.config.json`` in its config
home takes over, and a custom OAuth URL renames the file. STM answers only the
cases it reproduces exactly and calls the rest unknown, so a caller never reads
one file while ``claude mcp`` writes another.

**The project key** (:func:`claude_project_key`). Claude Code keeps
per-project state, ``/mcp`` disables and local-scope servers among it, under
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

import json
import os
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


@dataclass(frozen=True)
class ClaudeConfigUnknown:
    """STM cannot tell which file Claude Code reads its config from.

    ``reason`` is a stable code; :data:`CLAUDE_CONFIG_UNKNOWN_REASONS` words it.
    """

    reason: str


CLAUDE_CONFIG_UNKNOWN_REASONS: dict[str, str] = {
    "custom_oauth": "CLAUDE_CODE_CUSTOM_OAUTH_URL is set, which renames Claude Code's config file",
    "config_dir": "CLAUDE_CONFIG_DIR is empty, relative, or contains '.' or '..'",
    "home": "the home directory is unset, relative, or contains '.' or '..'",
    "legacy_config": "a legacy .config.json in Claude Code's config directory may be in use",
    "unreadable": "Claude Code's config file could not be read as a JSON object",
}

_SEPARATORS = re.compile(r"[\\/]" if sys.platform == "win32" else "/")


def _joinable_dir(value: str | None) -> bool:
    """Whether ``value`` joins to the same file in Node and in Python.

    Node's ``path.join`` collapses ``.`` and ``..`` lexically, while macOS and
    Linux resolve a ``..`` that Python leaves in place physically, through any
    symlink before it, so the two can name different files (Windows collapses
    it lexically as Node does; refusing it there too costs nothing). A relative value
    is resolved against Claude Code's own working directory, which STM does
    not know — and so, on Windows, is one rooted on the current drive
    (``\\cfg``) or relative to a drive's cwd (``C:cfg``): ``Path.is_absolute``
    requires a drive and a root there, where Python 3.12's ``os.path.isabs``
    accepts ``\\cfg``.
    """
    if not value or not Path(value).is_absolute():
        return False
    return not any(part in (".", "..") for part in _SEPARATORS.split(value))


def _home_dir() -> str | None:
    home = os.environ.get("USERPROFILE" if sys.platform == "win32" else "HOME")
    return home if _joinable_dir(home) else None


def claude_global_config() -> Path | ClaudeConfigUnknown:
    """The file Claude Code reads its global config from, or why STM cannot tell.

    Claude Code 2.1.316 picks it this way (from the executable, then measured
    with each case below in a scratch home)::

        home   = CLAUDE_CONFIG_DIR ?? join(homedir(), ".claude")   # NFC
        main   = join(CLAUDE_CONFIG_DIR || homedir(), ".claude" + suffix + ".json")
        legacy = join(home, ".config.json")
        legacy if it is a file and (nonempty, or writable and main is absent) else main

    ``suffix`` is ``"-custom-oauth"`` when ``CLAUDE_CODE_CUSTOM_OAUTH_URL`` is
    set. Only the plain case is answered: a custom OAuth URL, an empty or
    relative ``CLAUDE_CONFIG_DIR`` (empty makes Claude Code probe
    ``./.config.json`` in its working directory, which STM does not know), a
    ``.`` or ``..`` component,
    or a legacy entry of any kind — even an empty file, which is skipped only
    when ``main`` exists — gives :class:`ClaudeConfigUnknown`. So does a legacy
    probe that fails for any reason but absence: Claude Code would then read
    ``main``, but STM cannot show the legacy file is not the one in use.

    Not a trust boundary: the file is the user's own global config wherever
    ``CLAUDE_CONFIG_DIR`` points, since only the user's environment sets it.
    """
    if os.environ.get("CLAUDE_CODE_CUSTOM_OAUTH_URL"):
        return ClaudeConfigUnknown("custom_oauth")
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir is not None and not _joinable_dir(config_dir):
        return ClaudeConfigUnknown("config_dir")
    home = _home_dir()
    if config_dir is None and home is None:
        return ClaudeConfigUnknown("home")
    config_home = config_dir if config_dir is not None else os.path.join(str(home), ".claude")
    legacy = Path(unicodedata.normalize("NFC", config_home)) / ".config.json"
    try:
        legacy.lstat()
    except (FileNotFoundError, NotADirectoryError):
        pass
    except (OSError, ValueError):
        return ClaudeConfigUnknown("legacy_config")
    else:
        return ClaudeConfigUnknown("legacy_config")
    return Path(config_dir or str(home)) / ".claude.json"


@dataclass(frozen=True)
class ClaudeConfigRead:
    """One read of Claude Code's global config.

    ``unread`` is set (a key of :data:`CLAUDE_CONFIG_UNKNOWN_REASONS`) when the
    file was not inspected: STM cannot tell which file is in use, or the file
    exists and is not a readable JSON object. A missing file is inspected and
    empty — ``data`` and ``unread`` both ``None`` — so a caller that treats
    "not found" as "gone" may do so only when ``unread`` is ``None``.
    """

    path: Path | None
    data: dict[str, Any] | None
    unread: str | None


def read_claude_global_config() -> ClaudeConfigRead:
    """Resolve Claude Code's global config with :func:`claude_global_config` and read it."""
    resolved = claude_global_config()
    if isinstance(resolved, ClaudeConfigUnknown):
        return ClaudeConfigRead(None, None, resolved.reason)
    try:
        raw = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ClaudeConfigRead(resolved, None, None)
    except (OSError, ValueError):
        return ClaudeConfigRead(resolved, None, "unreadable")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return ClaudeConfigRead(resolved, None, "unreadable")
    if not isinstance(data, dict):
        return ClaudeConfigRead(resolved, None, "unreadable")
    return ClaudeConfigRead(resolved, data, None)
