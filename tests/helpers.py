"""Shared cross-platform test helpers.

Kept deliberately small — fixtures live in ``conftest.py``; this module
holds plain functions that are easier to grep for and call inline.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

if TYPE_CHECKING:
    from memtomem_stm.proxy.cache import ProxyCache
    from memtomem_stm.proxy.config import ProxyConfig, UpstreamServerConfig
    from memtomem_stm.proxy.manager import ProxyManager
    from memtomem_stm.proxy.metrics_store import MetricsStore


def set_home(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Pin the user's home directory to ``path`` on every platform.

    ``Path.home()`` and ``os.path.expanduser("~")`` consult ``USERPROFILE``
    before ``HOME`` on Windows. Tests that monkeypatch only ``HOME`` therefore
    leak the developer's real profile into temp-dir fixtures on Windows
    runners. Patching both keeps the sandbox hermetic.
    """
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))


def claude_key(path: Path) -> str:
    """How Claude Code spells ``path`` as a ``projects`` key: resolved, NFC,
    ``/``-separated on Windows too (#1271).

    Built here for fixtures and expectations, not by the resolver under test.
    A subprocess cwd or a recorded execution dir stays native; only the key
    takes this form.
    """
    import unicodedata

    return unicodedata.normalize("NFC", path.resolve().as_posix())


def fake_tool_result(text: str, *, is_error: bool = False) -> SimpleNamespace:
    """The upstream ``CallToolResult`` shape the manager reads.

    One definition on purpose: the pipeline reads ``content`` / ``is_error`` /
    ``structured_content`` / ``meta``, and a per-file copy of this shape drifts
    silently — the copy that is missing a field keeps passing against whatever
    the manager stopped reading.
    """
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        is_error=is_error,
        structured_content=None,
        meta=None,
    )


class FakeSurfacingEngine:
    """Minimal surfacing engine: the manager only reads ``injection_mode`` /
    ``observability`` and awaits ``surface(...)``.

    ``surface`` APPENDS its suffix so a progressive first chunk's read-more
    footer survives — a fake that replaced the text could report success while
    silently breaking the progressive concat invariant.
    """

    def __init__(
        self,
        mode: str = "append",
        *,
        suffix: str = " [mem]",
        raises: Exception | None = None,
    ) -> None:
        self.injection_mode = mode
        self.observability = None
        self._suffix = suffix
        self._raises = raises

    async def surface(
        self,
        *,
        server: str = "",
        tool: str = "",
        arguments: dict | None = None,
        response_text: str,
        trace_id: Any = None,
        context_query: Any = None,
    ) -> str:
        if self._raises is not None:
            raise self._raises
        return response_text + self._suffix


def count_loader_reads(manager: ProxyManager) -> list[int]:
    """Wrap the manager's config loader so every ``get()`` bumps a counter.

    Returns the one-element list holding the count, so a caller can reset it
    between phases. Each ``get()`` is a ``Path.stat()`` — the syscall #871 is
    about — so this is the measurement those read-count ceilings are pinned on.
    """
    calls = [0]
    real_get = manager._config_loader.get

    def counting_get():
        calls[0] += 1
        return real_get()

    manager._config_loader.get = counting_get  # type: ignore[method-assign]
    return calls


def wire_proxy_manager(
    proxy_cfg: ProxyConfig,
    server_cfg: UpstreamServerConfig,
    tmp_path: Path,
    *,
    server: str = "srv",
    index_engine: object | None = None,
    with_cache: bool = False,
    upstream_text: str | None = None,
) -> tuple[ProxyManager, MetricsStore, ProxyCache | None]:
    """Build a ``ProxyManager`` over a real MetricsStore and a mocked upstream.

    The manager/connection/cache wiring only — the CONFIG is the caller's, so
    each suite keeps its own intent (file-backed vs in-memory, which stages are
    on) without also re-deriving this seam. A second copy of it drifts the
    moment the constructor or ``UpstreamConnection`` shape changes: one file
    gets fixed, the other keeps passing against a stale contract.
    """
    from memtomem_stm.proxy.cache import ProxyCache
    from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
    from memtomem_stm.proxy.metrics import TokenTracker
    from memtomem_stm.proxy.metrics_store import MetricsStore

    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    mgr = ProxyManager(proxy_cfg, TokenTracker(metrics_store=store), index_engine=index_engine)
    session = AsyncMock()
    if upstream_text is not None:
        session.call_tool.return_value = fake_tool_result(upstream_text)
    mgr._connections[server] = UpstreamConnection(
        name=server, config=server_cfg, session=session, tools=[]
    )
    cache: ProxyCache | None = None
    if with_cache:
        cache = ProxyCache(tmp_path / "cache.db", max_entries=100)
        cache.initialize()
        mgr._cache = cache
    return mgr, store, cache


FOREIGN_FEEDBACK_CANARIES = ("cnryDecision", "cnryRating", "cnryErrorTail", "\x1b")


def seed_foreign_feedback_vocabulary(db_path: Path) -> None:
    """A feedback DB whose ``gate_decision`` / ``rating`` columns hold, next to
    STM's own values, values STM never writes (a foreign or corrupted file):
    4 folded decisions (one a non-ASCII exception name) and 2 foreign ratings
    (#1091)."""
    import sqlite3

    from memtomem_stm.surfacing.feedback_store import FeedbackStore

    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = FeedbackStore(db_path)
    store.initialize()
    try:
        store.record_surfacing("s1", "c7", "query-docs", "q", ["m1"], [0.9])
        store.record_feedback("s1", "helpful", "m1")
    finally:
        store.close()
    db = sqlite3.connect(db_path)
    try:
        columns = [row[1] for row in db.execute("PRAGMA table_info('surfacing_opportunities')")]
        decisions = [
            "surfaced",
            "skip:gate_cooldown",
            "error:timeout",
            "error:TimeoutError",
            "cnryDecision\x1b[31m",
            "error:cnry cnryErrorTail",
            "error:\u00c9chec",
            "skip:cnryDecision",
        ]
        for i, decision in enumerate(decisions):
            row = dict.fromkeys(columns)
            row.update(
                id=f"o{i}",
                server="c7",
                tool="query-docs",
                arg_shape_json="{}",
                response_len=1,
                gate_decision=decision,
                created_at=1.0,
            )
            db.execute(
                f"INSERT INTO surfacing_opportunities ({', '.join(row)}) "
                f"VALUES ({', '.join('?' * len(row))})",
                list(row.values()),
            )
        for rating in ("not_relevant", "cnryRating\x1b[2J", "HELPFUL"):
            db.execute(
                "INSERT INTO surfacing_feedback (surfacing_id, memory_id, rating, created_at) "
                "VALUES ('s1', 'm1', ?, 1.0)",
                (rating,),
            )
        db.commit()
    finally:
        db.close()


_EMPTY_LOCK_HOLDER = """
import os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
if os.name == "nt":
    import msvcrt
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
sys.stdout.write("ready\\n")
sys.stdout.flush()
sys.stdin.read()
"""


@contextmanager
def held_empty_lock(path: Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Hold ``path``'s lock from another process without writing to the file.

    On Windows the child locks byte 0 of a zero-length file, which is what an
    opener sees when it loses the race to initialise a fresh lock file (and
    what a pre-byte build left behind). Elsewhere it takes a plain ``flock``.
    The lock is released when the block exits; the child is always reaped,
    since the autouse child-sweep guard in ``conftest.py`` cleans up nothing.
    """
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", _EMPTY_LOCK_HOLDER, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: lines.put(proc.stdout.readline()), daemon=True).start()
        try:
            line = lines.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("lock holder did not become ready") from None
        assert line == "ready\n", f"lock holder failed to start (rc={proc.poll()})"
        yield
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()


def skip_if_inside_git(root: Path) -> None:
    """Skip when ``root`` sits in a git checkout: its ``.git`` would change every key."""
    import os

    if any(os.path.lexists(p / ".git") for p in (root, *root.parents)):
        pytest.skip("the temporary directory sits inside a git repository")


def git_worktree(main_common: Path, wt: Path, name: str, *, backpointer: str | None = None) -> Path:
    """Lay out a linked worktree the way git does, without running git.

    Writes the worktree's ``.git`` file, and ``commondir`` plus the back-pointer
    ``gitdir`` in its admin directory under ``main_common/worktrees/<name>``,
    which it returns.
    """
    admin = main_common / "worktrees" / name
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{backpointer or wt / '.git'}\n", encoding="utf-8")
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    return admin


def _symlink(link: Path, target: str | Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


def _plain_worktree(base: Path) -> tuple[Path, Path]:
    (base / "main" / ".git").mkdir(parents=True)
    git_worktree(base / "main" / ".git", base / "wt", "wt")
    (base / "wt" / "src").mkdir()
    return base / "wt" / "src", base / "main"


def _relative_gitdir(base: Path) -> tuple[Path, Path]:
    import os

    start, main = _plain_worktree(base)
    admin = main / ".git" / "worktrees" / "wt"
    (base / "wt" / ".git").write_text(
        f"gitdir: {os.path.relpath(admin, base / 'wt')}\n", encoding="utf-8"
    )
    return start, main


def _foreign_backpointer(base: Path) -> tuple[Path, Path]:
    (base / "main" / ".git").mkdir(parents=True)
    git_worktree(base / "main" / ".git", base / "wt", "wt", backpointer=str(base / "x" / ".git"))
    return base / "wt", base / "main"


def _bare_worktree(base: Path) -> tuple[Path, Path]:
    (base / "bare.git").mkdir()
    git_worktree(base / "bare.git", base / "wt", "wt")
    return base / "wt", base / "bare.git"


def _submodule(base: Path) -> tuple[Path, Path]:
    modules = base / "super" / ".git" / "modules" / "m"
    modules.mkdir(parents=True)
    (base / "super" / "m").mkdir()
    (base / "super" / "m" / ".git").write_text(f"gitdir: {modules}\n", encoding="utf-8")
    return base / "super" / "m", base / "super"


def _backpointer_through_symlink(target: str):
    def build(base: Path) -> tuple[Path, Path]:
        (base / "main" / ".git").mkdir(parents=True)
        (base / "other" / "child").mkdir(parents=True)
        wt = base / "wt"
        git_worktree(base / "main" / ".git", wt, "wt", backpointer=str(wt / target))
        if target.startswith("jump"):
            _symlink(wt / "jump", base / "other" / "child", directory=True)
        else:
            _symlink(wt / "link", "/" + str(wt / ".git"))
        return wt, base / "main"

    return build


def _prefixed_dotgit(prefix: str):
    def build(base: Path) -> tuple[Path, Path]:
        start, main = _plain_worktree(base)
        dotgit = base / "wt" / ".git"
        dotgit.write_text(prefix + dotgit.read_text(encoding="utf-8"), encoding="utf-8")
        return start, main

    return build


def _symlinked_dotgit(base: Path) -> tuple[Path, Path]:
    (base / "real.git").mkdir()
    (base / "repo" / "src").mkdir(parents=True)
    _symlink(base / "repo" / ".git", base / "real.git", directory=True)
    return base / "repo" / "src", base / "repo"


# Layouts whose ``.git`` is a file or a symlink, each with the start directory
# and the entry a wrong mapping would read (#1271). Claude Code keys most of
# them by the main checkout after checks STM does not reproduce, and the review
# rounds of #1288 found three where a port of that mapping picked differently
# (``backpointer-symlink-*``, ``bom``), so STM gives no key for any of them.
GIT_FILE_LAYOUTS = {
    "linked-worktree": _plain_worktree,
    "relative-gitdir": _relative_gitdir,
    "foreign-backpointer": _foreign_backpointer,
    "bare-repo-worktree": _bare_worktree,
    "submodule": _submodule,
    "backpointer-symlink-dotdot": _backpointer_through_symlink("jump/../.git"),
    "backpointer-symlink-unc-target": _backpointer_through_symlink("link"),
    "bom": _prefixed_dotgit("\ufeff"),
    "not-a-pointer": _prefixed_dotgit("garbage "),
    "symlinked-dotgit": _symlinked_dotgit,
}
