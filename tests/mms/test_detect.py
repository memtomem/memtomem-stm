"""Tests for ``mms.detect`` — RFC §6 algorithm + §6.1 edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem_stm.mms import state
from memtomem_stm.mms.detect import Project, Source, detect_project


def _write_marker(root: Path, name: str = "proj", enabled: list[str] | None = None) -> Path:
    marker = root / state.PROJECT_MARKER_RELPATH
    cfg = state.ProjectConfig(
        project=state.ProjectMeta(name=name),
        mcp=state.ProjectMcp(enabled=enabled or []),
    )
    state.save_project_config(cfg, marker)
    return marker


def _make_git(root: Path) -> None:
    (root / ".git").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Happy-path: each of the 3 source branches
# ---------------------------------------------------------------------------


def test_marker_at_cwd_wins(tmp_path):
    _write_marker(tmp_path, name="proj-a", enabled=["filesystem"])
    p = detect_project(tmp_path)
    assert p.source is Source.MARKER
    assert p.root == tmp_path.resolve()
    assert p.name == "proj-a"
    assert p.marker_path == (tmp_path / state.PROJECT_MARKER_RELPATH).resolve()
    assert p.config is not None
    assert p.config.mcp.enabled == ["filesystem"]


def test_marker_walk_up_finds_parent(tmp_path):
    _write_marker(tmp_path, name="parent-proj")
    nested = tmp_path / "sub" / "deep"
    nested.mkdir(parents=True)
    p = detect_project(nested)
    assert p.source is Source.MARKER
    assert p.root == tmp_path.resolve()
    assert p.name == "parent-proj"


def test_git_fallback_when_no_marker(tmp_path):
    _make_git(tmp_path)
    p = detect_project(tmp_path)
    assert p.source is Source.GIT
    assert p.root == tmp_path.resolve()
    assert p.name == tmp_path.name
    assert p.marker_path is None
    assert p.config is None


def test_cwd_fallback_when_no_marker_no_git(tmp_path):
    p = detect_project(tmp_path)
    assert p.source is Source.CWD
    assert p.root == tmp_path.resolve()
    assert p.name == tmp_path.name


# ---------------------------------------------------------------------------
# §6 ordering: marker beats git, even if git is closer
# ---------------------------------------------------------------------------


def test_marker_beats_closer_git(tmp_path):
    _write_marker(tmp_path, name="outer-marker")
    inner = tmp_path / "inner"
    inner.mkdir()
    _make_git(inner)  # .git is closer to cwd, but marker should still win
    p = detect_project(inner)
    assert p.source is Source.MARKER
    assert p.name == "outer-marker"
    assert p.root == tmp_path.resolve()


# ---------------------------------------------------------------------------
# §6.1 edge cases
# ---------------------------------------------------------------------------


def test_git_submodule_innermost_wins(tmp_path):
    """Two .git dirs in the walk; innermost (the submodule) wins."""
    _make_git(tmp_path)  # outer repo
    sub = tmp_path / "vendor" / "thing"
    sub.mkdir(parents=True)
    _make_git(sub)  # submodule
    p = detect_project(sub)
    assert p.source is Source.GIT
    assert p.root == sub.resolve()


def test_git_worktree_with_dot_git_as_file(tmp_path):
    """`.git` is a file in a worktree — still detected as a git repo."""
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    p = detect_project(tmp_path)
    assert p.source is Source.GIT
    assert p.root == tmp_path.resolve()


def test_monorepo_single_marker_at_root(tmp_path):
    """One marker at the monorepo root; sub-package without its own marker
    inherits the same project (RFC §6.1 "가장 가까운 marker가 진실")."""
    _write_marker(tmp_path, name="monorepo")
    pkg = tmp_path / "packages" / "pkg-a"
    pkg.mkdir(parents=True)
    p = detect_project(pkg)
    assert p.source is Source.MARKER
    assert p.name == "monorepo"
    assert p.root == tmp_path.resolve()


def test_malformed_marker_propagates_corruption(tmp_path):
    """RFC §6.1 — malformed marker is rejected, not silently bypassed."""
    marker = tmp_path / state.PROJECT_MARKER_RELPATH
    marker.parent.mkdir(parents=True)
    marker.write_text("this is = not [valid toml\n", encoding="utf-8")
    with pytest.raises(state.CorruptedConfig):
        detect_project(tmp_path)


def test_schema_mismatch_marker_propagates(tmp_path):
    """Detection must NOT silently fall through to git/cwd on schema mismatch."""
    marker = tmp_path / state.PROJECT_MARKER_RELPATH
    marker.parent.mkdir(parents=True)
    marker.write_text(
        'schema_version = 99\n[project]\nname = "x"\n[mcp]\nenabled = []\n',
        encoding="utf-8",
    )
    _make_git(tmp_path)  # git would otherwise match
    with pytest.raises(state.SchemaVersionMismatch):
        detect_project(tmp_path)


def test_cwd_is_root_filesystem_falls_back_to_cwd(monkeypatch, tmp_path):
    """Walk-up reaching `/` without a hit returns CWD source, not raise."""
    # Use tmp_path (no marker, no git) to simulate. We don't actually walk
    # to the real `/` — pathlib stops at root naturally.
    # The contract: even if walk reaches root, no exception.
    p = detect_project(tmp_path)
    assert p.source is Source.CWD


# ---------------------------------------------------------------------------
# Marker shape sanity — Project carries the parsed config
# ---------------------------------------------------------------------------


def test_marker_project_carries_parsed_config(tmp_path):
    _write_marker(tmp_path, name="p", enabled=["a", "b", "c"])
    p = detect_project(tmp_path)
    assert isinstance(p, Project)
    assert p.config is not None
    assert p.config.mcp.enabled == ["a", "b", "c"]
    assert p.config.project.name == "p"
