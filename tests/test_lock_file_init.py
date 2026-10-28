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
from pathlib import Path

import pytest

from memtomem_stm.daemon.locking import LockProbe, probe_lifetime_lock
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


def test_open_succeeds_while_another_process_holds_the_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with held_empty_lock(path):
        fd = open_lock_fd(path)
        try:
            assert try_lock(fd) is False
            with probe_lifetime_lock(path) as state:
                assert state is LockProbe.HELD
        except BaseException:
            os.close(fd)
            raise
    try:
        assert try_lock(fd) is True
        unlock(fd)
        other = open_lock_fd(path)
        try:
            assert try_lock(other) is True
        finally:
            release_lock(other)
    finally:
        os.close(fd)
