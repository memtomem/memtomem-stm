"""Keep executable lookups from consulting the current directory.

STM runs other programs by bare name (``memtomem-server``, upstream servers,
``claude``, ``codex``, ``pgrep``) while its working directory is the user's
project. Every lookup involved, ``shutil.which``, ``subprocess`` /
``execvp`` on POSIX, ``CreateProcess`` and the MCP SDK's Windows resolver,
searches ``PATH``, and two inputs make that search depend on the current
directory:

* a ``PATH`` entry that is not fully qualified (``.``, an empty entry,
  ``bin``, and on Windows ``C:bin`` or ``\\bin``) is resolved against it;
* on Windows, ``CreateProcess`` and ``shutil.which`` also search the current
  directory itself, unless ``NoDefaultCurrentDirectoryInExePath`` exists in
  the environment (only its existence is checked, not its value).

So a project could supply its own executable under one of those names.
:func:`harden_exec_search` fixes the shared input once, at startup, instead of
re-implementing each lookup: entries that are not fully qualified are dropped,
and on Windows the variable is set. Each lookup keeps its own rules otherwise,
and children inherit the hardened ``PATH``.
"""

from __future__ import annotations

import ntpath
import os
import sys
from collections.abc import MutableMapping

_WINDOWS_CWD_SEARCH_OFF = "NoDefaultCurrentDirectoryInExePath"


def _qualified(entry: str, *, windows: bool) -> bool:
    if not windows:
        # No unquoting on POSIX: a literal quote is part of the directory name.
        return entry.startswith("/")
    # ``cmd.exe`` honours a quoted entry, so judge the path inside one pair of
    # quotes; the entry itself is kept as written.
    if len(entry) >= 2 and entry[0] == entry[-1] == '"':
        entry = entry[1:-1]
    drive, rest = ntpath.splitdrive(entry)
    if drive[:2] in ("\\\\", "//"):
        return True  # UNC: \\server\share[\...]
    # ``ntpath.isabs`` accepts ``\bin`` on Python 3.12, which depends on the
    # current drive, so require the drive and the root explicitly.
    return bool(drive) and rest[:1] in ("\\", "/")


def qualified_path(value: str, *, windows: bool) -> str:
    """``value`` with every entry that is not fully qualified removed.

    The separator follows ``windows`` (``;`` or ``:``), not the host, so both
    rules can be checked on any platform. Kept entries keep their order and
    spelling; nothing is normalized or deduplicated.
    """
    sep = ";" if windows else ":"
    return sep.join(e for e in value.split(sep) if e and _qualified(e, windows=windows))


def harden_exec_search(environ: MutableMapping[str, str] | None = None) -> None:
    """Make executable lookups in this process ignore the current directory.

    ``PATH`` keeps only its fully qualified entries. It is never left empty or
    absent: ``subprocess`` reads an empty ``PATH`` as the current directory, and
    an absent one falls back to ``os.defpath``, which is ``.;C:\\bin`` on
    Windows. Either case becomes the platform default without its relative
    entries. On Windows ``NoDefaultCurrentDirectoryInExePath`` is set unless it
    already exists.
    """
    env = os.environ if environ is None else environ
    windows = sys.platform == "win32"
    current = env.get("PATH")
    kept = qualified_path(os.defpath if current is None else current, windows=windows)
    if not kept:
        kept = qualified_path(os.defpath, windows=windows)
    if kept != current:
        env["PATH"] = kept
    if windows:
        env.setdefault(_WINDOWS_CWD_SEARCH_OFF, "1")
