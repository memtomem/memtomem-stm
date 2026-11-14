"""Where Claude Code, Codex and Kimi Code keep their config home (#1345).

Each host lets an environment variable move its home, and each reads an empty
or relative value differently. Measured on macOS with a scratch ``HOME``, a
distinct entry in every candidate file, and the host run from another
directory:

* Codex CLI 0.167.2, ``CODEX_HOME``: unset or ``""`` is ``~/.codex``; a
  relative value is taken from Codex's own working directory; a ``..`` after a
  symlink is resolved physically.
* Kimi Code 0.39.0, ``KIMI_CODE_HOME``: unset is ``~/.kimi-code``; ``""`` and a
  relative value are taken from Kimi's working directory; a ``..`` after a
  symlink is joined lexically.
* Claude Code, ``CLAUDE_CONFIG_DIR``: unset is ``~/.claude``; empty, relative
  and ``..`` values are covered in :mod:`~memtomem_stm.mms.claude_config`.

STM does not know the directory a host runs in, and opening a path joins a
``..`` physically where a Node host joins it lexically, so a value is used only
when it is absolute with no ``.`` or ``..`` component
(:func:`~memtomem_stm.mms.claude_config._joinable_dir`). Codex resolves ``..``
physically, as Python does, but is held to the same rule: a nonexistent
component or a Windows junction before the ``..`` was not measured. Anything
else is :class:`HostHomeUnknown` — never the current directory, which is where
a repository checkout can put a file of its own.

Only the variable is judged here. When it is not in effect, each caller keeps
its own default under ``~``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from memtomem_stm.mms.claude_config import _joinable_dir


@dataclass(frozen=True)
class HostHomeUnknown:
    """STM cannot tell which directory the host reads its config from.

    ``code`` is a key of :data:`HOST_HOME_UNKNOWN_REASONS`. Neither carries the
    variable's value.
    """

    code: str


HOST_HOME_UNKNOWN_REASONS: dict[str, str] = {
    "config_dir": "CLAUDE_CONFIG_DIR is empty, relative, or contains '.' or '..'",
    "codex_home": "CODEX_HOME is relative or contains '.' or '..'",
    "kimi_home": "KIMI_CODE_HOME is empty, relative, or contains '.' or '..'",
}

# host -> (variable, whether an empty value means unset, unknown code)
_HOSTS: dict[str, tuple[str, bool, str]] = {
    "claude": ("CLAUDE_CONFIG_DIR", False, "config_dir"),
    "codex": ("CODEX_HOME", True, "codex_home"),
    "kimi": ("KIMI_CODE_HOME", False, "kimi_home"),
}


def host_home_override(host: str) -> Path | HostHomeUnknown | None:
    """The config home ``host``'s variable points at, or ``None`` when it is not in effect.

    ``host`` is ``"claude"``, ``"codex"`` or ``"kimi"``. ``None`` means the
    host uses its default home, which the caller supplies.
    """
    variable, empty_is_unset, code = _HOSTS[host]
    value = os.environ.get(variable)
    if value is None or (empty_is_unset and value == ""):
        return None
    return Path(value) if _joinable_dir(value) else HostHomeUnknown(code)
