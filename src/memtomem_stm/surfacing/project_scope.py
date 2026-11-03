"""Which LTM namespaces belong to the repository a hook call ran in.

Native-tool hook calls (``mms hook``) carry the host's ``cwd``. Without it the
engine searched every namespace on the machine, and most of what it injected
came from other projects' auto-memory. This module turns a ``cwd`` into the
include-list the engine sends as ``namespace``: ``claude:<slug>`` for each git
worktree of the repository (Claude Code names its per-project directories, and
therefore the ``claude:`` namespaces built from them, after the worktree path),
``shared:<repo>``, and ``default``.

Identity comes from ``git worktree list``, not from slug prefixes: a hyphen in a
directory name is indistinguishable from a path separator once slugified, so
``…-memtomem`` is a prefix of ``…-memtomem-stm`` although the two are separate
repositories.

Everything that touches the filesystem or spawns ``git`` runs in one function
on the default executor, never on the event loop. Any failure yields
:data:`INACTIVE`, which the engine treats as today's unscoped search: a project
STM cannot identify widens retrieval rather than narrowing it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from pathlib import Path, PurePath

from memtomem_stm.mms.claude_config import _home_dir, _joinable_dir, claude_project_key

logger = logging.getLogger(__name__)

__all__ = [
    "INACTIVE",
    "ProjectScope",
    "ProjectScopeCache",
    "resolve_project_scope",
    "slugify",
]

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")
_GIT_TIMEOUT_SECONDS = 1.0
# Variables that would point ``git -C <cwd>`` at some other repository — set,
# for instance, when the hook fires inside a git hook of the host's own.
_GIT_ENV_OVERRIDES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")


def slugify(path: str) -> str:
    """Claude Code's project-directory name for ``path``.

    Every character outside ``[A-Za-z0-9]`` becomes ``-``, so
    ``/x/.claude/worktrees/y`` is ``-x--claude-worktrees-y``. Measured against
    the names Claude Code created on macOS; Unicode and Windows paths are not
    verified, which is why :func:`resolve_project_scope` only activates a scope
    whose directory actually exists.
    """
    return _NON_ALNUM.sub("-", path)


@dataclass(frozen=True)
class ProjectScope:
    """The namespaces one hook call may search; empty means unscoped."""

    namespaces: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Canonical form: equal sets are equal scopes and share a cache key.
        object.__setattr__(self, "namespaces", tuple(sorted(set(self.namespaces))))

    @property
    def active(self) -> bool:
        return bool(self.namespaces)

    @property
    def key(self) -> str:
        """Deterministic cache-key component; ``""`` when unscoped."""
        return "\x1f".join(self.namespaces)


INACTIVE = ProjectScope()


def _normalize(path: str) -> str:
    path = unicodedata.normalize("NFC", path)
    if sys.platform == "win32":
        path = path.replace("\\", "/")
    return path


def _claude_home(explicit: Path | None) -> Path | None:
    """Claude Code's config home, or ``None`` when STM cannot tell."""
    if explicit is not None:
        return explicit
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir is not None:
        return Path(config_dir) if _joinable_dir(config_dir) else None
    home = _home_dir()
    return Path(home) / ".claude" if home is not None else None


def _git_worktrees(cwd: str) -> list[str] | None:
    """Worktree paths of the repository holding ``cwd``, main first; ``None`` on failure."""
    git = shutil.which("git")
    if git is None:
        return None
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_OVERRIDES}
    try:
        proc = subprocess.run(
            [git, "-C", cwd, "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    paths = [
        line[len("worktree ") :]
        for line in proc.stdout.splitlines()
        if line.startswith("worktree ")
    ]
    return paths or None


def _resolve_sync(cwd: str, claude_config_dir: Path | None) -> ProjectScope:
    worktrees = _git_worktrees(cwd)
    if worktrees is None:
        worktrees = [claude_project_key(Path(cwd)) or cwd]
    paths = [_normalize(p) for p in worktrees]
    home = _claude_home(claude_config_dir)
    if home is None:
        logger.debug("Project scope inactive: Claude Code config directory is unknown")
        return INACTIVE
    projects = home / "projects"
    if not any((projects / slugify(p)).is_dir() for p in paths):
        logger.debug("Project scope inactive: no Claude Code project directory for this repository")
        return INACTIVE
    names = {f"claude:{slugify(p)}" for p in [*paths, _normalize(cwd)]}
    main_name = PurePath(paths[0]).name
    if _usable_exact_name(main_name):
        names.add(f"shared:{main_name}")
    elif main_name:
        logger.debug("Project scope: repository name is not an exact namespace; shared: omitted")
    names.add("default")
    return ProjectScope(namespaces=tuple(sorted(names)))


def _usable_exact_name(name: str) -> bool:
    """Whether ``name`` survives Core's comma-joined exact-name union intact.

    ``NamespaceFilter.parse`` splits on ``,``, strips each item, and rejects a
    value mixing ``*`` with ``,``. ``claude:`` slugs are always safe (slugify
    leaves only ``[A-Za-z0-9-]``); a directory name may not be.
    """
    return bool(name) and name == name.strip() and "," not in name and "*" not in name


_T = TypeVar("_T")


def _run_detached(fn: Callable[..., _T], *args: object) -> asyncio.Future[_T]:
    """Run ``fn`` on a daemon thread and return a future for its result.

    Not the loop's default executor: ``asyncio.run`` waits for that executor's
    threads at shutdown, so a cancelled resolution would still hold the cold
    hook process for as long as ``git`` takes. A daemon thread is abandoned
    instead; it ends on its own within the ``git`` timeout.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[_T] = loop.create_future()

    def set_result(result: _T) -> None:
        if not future.done():
            future.set_result(result)

    def set_exception(exc: BaseException) -> None:
        if not future.done():
            future.set_exception(exc)

    def deliver(callback: Callable[[Any], None], value: object) -> None:
        try:
            loop.call_soon_threadsafe(callback, value)
        except RuntimeError:
            pass  # the loop closed while we worked; nobody is waiting

    def work() -> None:
        try:
            result = fn(*args)
        except BaseException as exc:  # delivered to the awaiting caller
            deliver(set_exception, exc)
        else:
            deliver(set_result, result)

    threading.Thread(target=work, name="mms-project-scope", daemon=True).start()
    return future


async def resolve_project_scope(cwd: str, *, claude_config_dir: Path | None = None) -> ProjectScope:
    """Resolve ``cwd`` to its repository's namespaces; never raises except on cancel."""
    try:
        return await _run_detached(_resolve_sync, cwd, claude_config_dir)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Project scope resolution failed", exc_info=True)
        return INACTIVE


class ProjectScopeCache:
    """Per-``cwd`` cache of :func:`resolve_project_scope` results.

    An active scope is kept for 60 s so a worktree added during a long daemon
    run is picked up; an inactive one (including a transient ``git`` failure)
    only for 5 s. Concurrent cold calls for one ``cwd`` may each resolve: there
    is no shared in-flight future, so one caller's cancellation cannot reach
    another.
    """

    ACTIVE_TTL_SECONDS = 60.0
    INACTIVE_TTL_SECONDS = 5.0
    MAX_ENTRIES = 64

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        claude_config_dir: Path | None = None,
    ) -> None:
        self._clock = clock
        self._claude_config_dir = claude_config_dir
        self._entries: OrderedDict[str, tuple[float, ProjectScope]] = OrderedDict()

    async def get(self, cwd: str) -> ProjectScope:
        now = self._clock()
        entry = self._entries.get(cwd)
        if entry is not None and entry[0] > now:
            return entry[1]
        scope = await resolve_project_scope(cwd, claude_config_dir=self._claude_config_dir)
        ttl = self.ACTIVE_TTL_SECONDS if scope.active else self.INACTIVE_TTL_SECONDS
        self._entries[cwd] = (self._clock() + ttl, scope)
        self._entries.move_to_end(cwd)
        while len(self._entries) > self.MAX_ENTRIES:
            self._entries.popitem(last=False)
        return scope
