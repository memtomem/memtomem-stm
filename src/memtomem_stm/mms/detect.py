"""Project detection — RFC §6.

The algorithm walks parents in a fixed order and stops at the first
match:

1. ``.mms/project.toml`` marker (anywhere up the tree)
2. ``.git`` directory (innermost wins)
3. cwd fallback — anonymous project, ``mms project show`` will suggest
   running ``mms project init``

Edge cases per RFC §6.1:

* git submodule — innermost ``.git`` (the submodule's) wins
* git worktree — ``.git`` is a *file* pointing at the main repo's
  worktree dir, treated as a regular git repo
* monorepo with single marker — nearest marker wins; sub-projects must
  add their own marker to be distinct
* malformed marker — propagated as :class:`mms.state.CorruptedConfig`
  / :class:`mms.state.SchemaVersionMismatch`; detection does *not*
  silently fall through to the next strategy (RFC §6.1 "거부, 백업
  위치 안내, 자동 복구 X")

The Project returned is enough to (a) know which marker file (if any)
backs the detection so save operations land in the right place, and
(b) report the detection source to ``mms project show`` for the
init-hint UX.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from memtomem_stm.mms.state import (
    PROJECT_MARKER_RELPATH,
    ProjectConfig,
    load_project_config,
)


class Source(Enum):
    """Where the detection algorithm decided the project lives."""

    MARKER = "marker"  # .mms/project.toml found
    GIT = "git"  # .git found, no marker yet — init suggested
    CWD = "cwd"  # neither marker nor git — anonymous fallback


@dataclass(frozen=True)
class Project:
    """A detected project's location + identity.

    ``root`` is the directory containing the marker (MARKER source) or
    the git root (GIT source) or simply ``cwd`` (CWD source).

    ``marker_path`` is set only when ``source is Source.MARKER``;
    callers performing reads/writes should use this. For non-MARKER
    sources, the path where ``mms project init`` would create the
    marker is ``root / .mms / project.toml``.

    ``config`` is the parsed ``ProjectConfig`` for MARKER sources only.
    For GIT/CWD sources the project is "anonymous" and ``config`` is
    ``None`` — the CLI surfaces an init hint instead of an enabled list.

    ``name`` defaults to ``root.name`` for non-MARKER sources, or to
    the marker's recorded ``[project] name`` for MARKER sources.
    """

    root: Path
    source: Source
    name: str
    marker_path: Path | None = None
    config: ProjectConfig | None = None


def _is_git_dir(path: Path) -> bool:
    """``.git`` may be a directory (regular repo / submodule) or a file
    (git worktree pointing at the main repo's worktree dir). Both count.
    """
    g = path / ".git"
    return g.is_dir() or g.is_file()


def detect_project(cwd: Path) -> Project:
    """Run the §6 detection algorithm and return the result.

    Walks ``cwd`` and its parents in order; the *innermost* (closest to
    cwd) match wins. The marker walk and the git walk are independent
    — a marker anywhere up the tree beats a git root anywhere up the
    tree, even if the git root is closer.
    """
    resolved = cwd.expanduser().resolve()

    # 1. marker walk-up
    for p in [resolved, *resolved.parents]:
        marker = p / PROJECT_MARKER_RELPATH
        if marker.is_file():
            cfg = load_project_config(marker)  # may raise — propagate per §6.1
            return Project(
                root=p,
                source=Source.MARKER,
                name=cfg.project.name,
                marker_path=marker,
                config=cfg,
            )

    # 2. git root walk-up — innermost
    for p in [resolved, *resolved.parents]:
        if _is_git_dir(p):
            return Project(root=p, source=Source.GIT, name=p.name)

    # 3. cwd fallback
    return Project(root=resolved, source=Source.CWD, name=resolved.name)
