"""cwd → repository namespace include-list for hook-call surfacing."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from memtomem_stm.surfacing import project_scope
from memtomem_stm.surfacing.project_scope import (
    INACTIVE,
    ProjectScope,
    ProjectScopeCache,
    resolve_project_scope,
    slugify,
)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(*args: str, cwd: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env=env,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    return path


def _real(path: Path) -> str:
    """git reports worktree paths with symlinks resolved (macOS /var → /private/var)."""
    return str(path.resolve())


def _claude_home(tmp_path: Path, *project_paths: str) -> Path:
    home = tmp_path / "claude-home"
    for p in project_paths:
        (home / "projects" / slugify(p)).mkdir(parents=True)
    (home / "projects").mkdir(parents=True, exist_ok=True)
    return home


class TestSlugify:
    @pytest.mark.parametrize(
        ("path", "slug"),
        [
            (
                "/Users/pdstudio/Work/agent-harness/memtomem-stm",
                "-Users-pdstudio-Work-agent-harness-memtomem-stm",
            ),
            (
                "/Users/pdstudio/Work/agent-harness/memtomem"
                "/.claude/worktrees/hermes-readme-disclosures",
                "-Users-pdstudio-Work-agent-harness-memtomem"
                "--claude-worktrees-hermes-readme-disclosures",
            ),
            (
                "/private/tmp/memtomem-release-0.7.4",
                "-private-tmp-memtomem-release-0-7-4",
            ),
        ],
    )
    def test_matches_real_claude_code_project_names(self, path: str, slug: str) -> None:
        # Literals copied from directory names Claude Code created on macOS.
        assert slugify(path) == slug


class TestProjectScopeValue:
    def test_inactive_key_is_empty(self) -> None:
        assert INACTIVE.active is False
        assert INACTIVE.key == ""

    def test_namespaces_are_canonical(self) -> None:
        scope = ProjectScope(namespaces=("b", "a", "b"))
        assert scope.namespaces == ("a", "b")
        assert scope.key == "a\x1fb"
        assert scope == ProjectScope(namespaces=("a", "b"))
        assert scope.key == ProjectScope(namespaces=("a", "b")).key


@needs_git
class TestResolveWithGit:
    async def test_main_and_linked_worktree_share_one_scope(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        wt = repo / ".claude" / "worktrees" / "feature"
        _git("worktree", "add", "-q", "-b", "feature", str(wt), cwd=repo)
        home = _claude_home(tmp_path, _real(repo))

        from_main = await resolve_project_scope(str(repo.resolve()), claude_config_dir=home)
        from_wt = await resolve_project_scope(str(wt.resolve()), claude_config_dir=home)

        expected = {
            f"claude:{slugify(_real(repo))}",
            f"claude:{slugify(_real(wt))}",
            "shared:repo",
            "default",
        }
        assert set(from_main.namespaces) == expected
        assert set(from_wt.namespaces) == expected
        assert list(from_main.namespaces) == sorted(from_main.namespaces)

    async def test_active_when_only_the_worktree_has_a_project_dir(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        wt = tmp_path / "repo-wt"
        _git("worktree", "add", "-q", "-b", "feature", str(wt), cwd=repo)
        home = _claude_home(tmp_path, _real(wt))

        scope = await resolve_project_scope(str(wt.resolve()), claude_config_dir=home)

        assert f"claude:{slugify(_real(wt))}" in scope.namespaces
        assert f"claude:{slugify(_real(repo))}" in scope.namespaces

    async def test_prefix_sibling_repository_is_not_included(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "memtomem")
        sibling = _init_repo(tmp_path / "memtomem-stm")
        home = _claude_home(tmp_path, _real(repo), _real(sibling))

        scope = await resolve_project_scope(str(repo.resolve()), claude_config_dir=home)

        assert f"claude:{slugify(_real(repo))}" in scope.namespaces
        assert f"claude:{slugify(_real(sibling))}" not in scope.namespaces
        assert "shared:memtomem-stm" not in scope.namespaces

    async def test_subdirectory_resolves_to_the_repository(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        sub = repo / "src" / "pkg"
        sub.mkdir(parents=True)
        home = _claude_home(tmp_path, _real(repo))

        scope = await resolve_project_scope(str(sub.resolve()), claude_config_dir=home)

        assert f"claude:{slugify(_real(repo))}" in scope.namespaces
        assert "shared:repo" in scope.namespaces

    async def test_inactive_without_any_claude_project_dir(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        home = _claude_home(tmp_path)

        assert await resolve_project_scope(str(repo.resolve()), claude_config_dir=home) is INACTIVE

    async def test_git_env_pointing_elsewhere_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        other = _init_repo(tmp_path / "other")
        home = _claude_home(tmp_path, _real(repo))
        monkeypatch.setenv("GIT_DIR", str(other / ".git"))

        scope = await resolve_project_scope(str(repo.resolve()), claude_config_dir=home)

        assert f"claude:{slugify(_real(repo))}" in scope.namespaces
        assert f"claude:{slugify(_real(other))}" not in scope.namespaces


class TestUnrepresentableRepositoryName:
    @pytest.mark.parametrize(
        "name",
        [
            pytest.param(
                "repo*",
                marks=pytest.mark.skipif(
                    sys.platform == "win32", reason="'*' is not a valid Windows file name"
                ),
            ),
            "repo,other",
            " repo",
        ],
    )
    async def test_shared_name_is_omitted_when_core_cannot_take_it_exactly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        cwd = tmp_path / name
        cwd.mkdir()
        home = _claude_home(tmp_path, _real(cwd))
        monkeypatch.setattr(project_scope.shutil, "which", lambda n: None)

        scope = await resolve_project_scope(str(cwd.resolve()), claude_config_dir=home)

        assert scope.active
        assert not any(ns.startswith("shared:") for ns in scope.namespaces)
        assert all("," not in ns and "*" not in ns for ns in scope.namespaces)


def test_cancelled_resolution_does_not_hold_asyncio_run_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import time

    def slow(cwd: str, claude_config_dir: Path | None) -> ProjectScope:
        time.sleep(0.6)
        return INACTIVE

    monkeypatch.setattr(project_scope, "_resolve_sync", slow)

    async def main() -> None:
        task = asyncio.create_task(resolve_project_scope("/r"))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    started = time.monotonic()
    asyncio.run(main())
    # The cold hook process ends its loop with ``asyncio.run``; a resolution
    # on the default executor would hold it for the whole 0.6 s.
    assert time.monotonic() - started < 0.3


class TestResolveFallbacks:
    async def test_git_missing_falls_back_to_the_cwd_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = tmp_path / "plain"
        cwd.mkdir()
        home = _claude_home(tmp_path, _real(cwd))
        monkeypatch.setattr(project_scope.shutil, "which", lambda name: None)

        scope = await resolve_project_scope(str(cwd.resolve()), claude_config_dir=home)

        assert scope.namespaces == tuple(
            sorted({f"claude:{slugify(_real(cwd))}", "shared:plain", "default"})
        )

    async def test_git_timeout_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = tmp_path / "plain"
        cwd.mkdir()
        home = _claude_home(tmp_path, _real(cwd))

        def timeout(*args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="git", timeout=1.0)

        monkeypatch.setattr(project_scope.shutil, "which", lambda name: "/usr/bin/git")
        monkeypatch.setattr(project_scope.subprocess, "run", timeout)

        scope = await resolve_project_scope(str(cwd.resolve()), claude_config_dir=home)

        assert f"claude:{slugify(_real(cwd))}" in scope.namespaces

    async def test_non_repository_cwd_falls_back(self, tmp_path: Path) -> None:
        if shutil.which("git") is None:
            pytest.skip("git not installed")
        cwd = tmp_path / "plain"
        cwd.mkdir()
        home = _claude_home(tmp_path, _real(cwd))

        scope = await resolve_project_scope(str(cwd.resolve()), claude_config_dir=home)

        assert f"claude:{slugify(_real(cwd))}" in scope.namespaces

    async def test_unexpected_error_is_inactive_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*args: object) -> ProjectScope:
            raise RuntimeError("boom")

        monkeypatch.setattr(project_scope, "_resolve_sync", boom)

        assert await resolve_project_scope(str(tmp_path)) is INACTIVE

    async def test_relative_claude_config_dir_is_inactive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = tmp_path / "plain"
        cwd.mkdir()
        # The relative directory exists from the test's cwd, so only the
        # "relative is unknowable" rule keeps the scope inactive: Claude Code
        # resolves it against its own working directory, which STM never sees.
        (tmp_path / "relative" / "dir" / "projects" / slugify(_real(cwd))).mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(project_scope.shutil, "which", lambda name: None)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative/dir")

        assert await resolve_project_scope(str(cwd.resolve())) is INACTIVE

    async def test_claude_config_dir_env_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = tmp_path / "plain"
        cwd.mkdir()
        home = _claude_home(tmp_path, _real(cwd))
        monkeypatch.setattr(project_scope.shutil, "which", lambda name: None)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))

        scope = await resolve_project_scope(str(cwd.resolve()))

        assert scope.active


class TestProjectScopeCache:
    async def test_active_ttl_then_refresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = [1000.0]
        calls: list[str] = []
        results = [ProjectScope(namespaces=("a",)), ProjectScope(namespaces=("a", "b"))]

        async def fake(cwd: str, *, claude_config_dir: Path | None = None) -> ProjectScope:
            calls.append(cwd)
            return results[len(calls) - 1]

        monkeypatch.setattr(project_scope, "resolve_project_scope", fake)
        cache = ProjectScopeCache(clock=lambda: now[0])

        assert (await cache.get("/r")).namespaces == ("a",)
        now[0] += 59.0  # literal: the documented active TTL is 60 s
        assert (await cache.get("/r")).namespaces == ("a",)
        assert len(calls) == 1
        now[0] += 2.0
        assert (await cache.get("/r")).namespaces == ("a", "b")
        assert len(calls) == 2

    async def test_inactive_result_is_retried_sooner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = [0.0]
        calls: list[str] = []

        async def fake(cwd: str, *, claude_config_dir: Path | None = None) -> ProjectScope:
            calls.append(cwd)
            return INACTIVE

        monkeypatch.setattr(project_scope, "resolve_project_scope", fake)
        cache = ProjectScopeCache(clock=lambda: now[0])

        await cache.get("/r")
        now[0] += 4.0  # literal: the documented inactive TTL is 5 s
        await cache.get("/r")
        assert len(calls) == 1  # still cached before expiry
        now[0] += 1.5
        await cache.get("/r")
        assert len(calls) == 2

    async def test_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake(cwd: str, *, claude_config_dir: Path | None = None) -> ProjectScope:
            return ProjectScope(namespaces=(cwd,))

        monkeypatch.setattr(project_scope, "resolve_project_scope", fake)
        cache = ProjectScopeCache()
        for i in range(ProjectScopeCache.MAX_ENTRIES + 5):
            await cache.get(f"/r{i}")
        assert len(cache._entries) == ProjectScopeCache.MAX_ENTRIES
        assert "/r0" not in cache._entries

    @needs_git
    async def test_new_worktree_is_picked_up_after_ttl(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        home = _claude_home(tmp_path, _real(repo))
        now = [0.0]
        cache = ProjectScopeCache(clock=lambda: now[0], claude_config_dir=home)
        cwd = str(repo.resolve())

        before = await cache.get(cwd)
        wt = tmp_path / "late-wt"
        _git("worktree", "add", "-q", "-b", "late", str(wt), cwd=repo)
        assert f"claude:{slugify(_real(wt))}" not in (await cache.get(cwd)).namespaces
        now[0] += 61.0
        after = await cache.get(cwd)

        assert f"claude:{slugify(_real(wt))}" not in before.namespaces
        assert f"claude:{slugify(_real(wt))}" in after.namespaces
