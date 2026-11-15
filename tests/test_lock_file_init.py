"""Opening a lock file whose first byte another process already holds.

On Windows ``open_lock_fd`` writes one byte into a zero-length lock file,
because ``msvcrt.locking`` locks bytes from the current position. Two openers
of a fresh file can both see it empty; once the first has written and locked
the byte, the second's write lands in a locked range and fails with
``PermissionError``. That opener must still get an fd, so the lock attempt —
not the open — decides who owns the lock (#1248).
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

from memtomem_stm.daemon import locking as daemon_locking
from memtomem_stm.daemon.locking import (
    LockProbe,
    probe_existing_lifetime_lock,
    probe_lifetime_lock,
)
from memtomem_stm.utils import locking
from memtomem_stm.utils.locking import open_lock_fd, release_lock, try_lock, unlock
from helpers import held_empty_lock


def _failing_write(exc: OSError, seen: list[int]):
    def write(fd: int, data: bytes) -> int:
        seen.append(fd)
        raise exc

    return write


def test_locked_initialising_write_still_returns_the_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "x.lock"
    path.touch()
    seen: list[int] = []
    with monkeypatch.context() as m:
        m.setattr(locking.os, "name", "nt")
        m.setattr(
            locking.os, "write", _failing_write(PermissionError(errno.EACCES, "locked"), seen)
        )
        fd = open_lock_fd(path)
    try:
        assert seen == [fd]
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
    finally:
        os.close(fd)


def test_other_initialising_write_failure_closes_the_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "x.lock"
    path.touch()
    seen: list[int] = []
    with monkeypatch.context() as m:
        m.setattr(locking.os, "name", "nt")
        m.setattr(locking.os, "write", _failing_write(OSError(errno.EIO, "io"), seen))
        with pytest.raises(OSError) as raised:
            open_lock_fd(path)
    assert raised.value.errno == errno.EIO
    assert len(seen) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(seen[0])
    assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize("create", [True, False], ids=["creating", "existing-only"])
def test_open_succeeds_while_another_process_holds_the_empty_file(
    tmp_path: Path, create: bool
) -> None:
    """Both ways of opening: the one that may create the file, and the one
    that only opens a file already there (the holder made it)."""
    path = tmp_path / "x.lock"
    with held_empty_lock(path):
        fd = open_lock_fd(path, create=create)
        try:
            assert try_lock(fd) is False
            with probe_lifetime_lock(path) as state:
                assert state is LockProbe.HELD
            assert probe_existing_lifetime_lock(path) is LockProbe.HELD
        except BaseException:
            os.close(fd)
            raise
    try:
        assert try_lock(fd) is True
        unlock(fd)
        other = open_lock_fd(path, create=create)
        try:
            assert try_lock(other) is True
        finally:
            release_lock(other)
    finally:
        os.close(fd)


# ── opening only what is already there (#1421) ───────────────────────────────


def test_existing_only_open_creates_neither_the_file_nor_its_directory(tmp_path: Path) -> None:
    path = tmp_path / "not-there" / "x.lock"
    with pytest.raises(FileNotFoundError):
        open_lock_fd(path, create=False)
    assert probe_existing_lifetime_lock(path) is None
    assert list(tmp_path.iterdir()) == []

    # The control: the default still makes both.
    release_lock(open_lock_fd(path))
    assert path.is_file()


def test_existing_lock_probe_reports_free_and_holds_nothing_afterwards(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    path.touch()
    before = path.stat()

    assert probe_existing_lifetime_lock(path) is LockProbe.FREE

    # Released on return: the next taker gets it.
    fd = open_lock_fd(path)
    try:
        assert try_lock(fd) is True
    finally:
        release_lock(fd)
    if os.name != "nt":
        # Nothing was written either (Windows prepares byte 0 of an empty file).
        assert path.stat().st_size == before.st_size == 0


@pytest.mark.skipif(sys.platform == "win32", reason="creating a symlink needs a privilege")
def test_existing_lock_probe_does_not_create_the_target_of_a_dangling_link(
    tmp_path: Path,
) -> None:
    """``O_CREAT`` follows a link and creates what it points at; then the
    probe that asked "is this held" answers "free" about a file it just made."""
    target = tmp_path / "target"
    link = tmp_path / "x.lock"
    link.symlink_to(target)

    assert probe_existing_lifetime_lock(link) is None
    assert not target.exists()

    # The control: this is what the creating probe does with the same link.
    with probe_lifetime_lock(link) as state:
        assert state is LockProbe.FREE
    assert target.exists()


def test_existing_lock_probe_reports_a_file_it_cannot_open_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "x.lock"
    path.touch()

    def refuse(path: Path, **kwargs: bool) -> int:
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(daemon_locking, "_open_lock_fd", refuse)

    assert probe_existing_lifetime_lock(path) is LockProbe.UNKNOWN
