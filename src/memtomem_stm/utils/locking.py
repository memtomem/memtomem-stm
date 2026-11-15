"""Small cross-platform advisory file-lock primitives.

Windows ``msvcrt.locking`` locks bytes from the current file position, so every
operation seeks back to offset zero and locks one byte.  The opener writes that
byte into an empty file when it can; a file can still be empty while another
handle holds byte 0 (locking past the end of a file is allowed), and the
opener then skips the write, which locking does not need.  Keeping that
detail here prevents the daemon and ``mms`` state writers from drifting into
subtly different implementations.
"""

from __future__ import annotations

import errno
import importlib
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def open_lock_fd(path: Path, *, create: bool = True) -> int:
    """Open ``path`` for locking and return an fd owned by the caller.

    With ``create=False`` nothing is made: no parent directory, no file, and
    no target for a link that does not resolve — a path with no file behind it
    raises ``FileNotFoundError``. For a caller that only asks whether a lock
    that already exists is held.
    """
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    else:
        fd = os.open(str(path), os.O_RDWR)
    if os.name == "nt":  # pragma: no cover - Windows CI
        try:
            if os.fstat(fd).st_size == 0:
                try:
                    os.write(fd, b"\0")
                except PermissionError:
                    # Another handle wrote and locked the byte after our size
                    # check (or locked it without writing). The file is in use
                    # as a lock; whether we get it is try_lock's question. An
                    # access denial would have failed the O_RDWR open instead.
                    pass
            os.lseek(fd, 0, os.SEEK_SET)
        except Exception:
            os.close(fd)
            raise
    return fd


# What a non-blocking lock attempt raises when someone else holds the lock:
# ``flock`` reports EWOULDBLOCK (EAGAIN on most systems) and ``msvcrt.locking``
# EACCES, which is also what POSIX allows a contended lock to report.
_CONTENDED = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES})


def try_lock_strict(fd: int) -> bool:
    """Attempt a non-blocking exclusive lock; say why when it is not taken.

    ``True`` when acquired and ``False`` when another handle holds it. Any
    other failure — a filesystem that does not support locking, an I/O error —
    raises ``OSError``: the lock was not found to be held, the question was
    not answered.
    """
    try:
        if os.name == "nt":  # pragma: no cover - Windows CI
            msvcrt: Any = importlib.import_module("msvcrt")

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl: Any = importlib.import_module("fcntl")

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in _CONTENDED:
            return False
        raise
    return True


def try_lock(fd: int) -> bool:
    """Attempt a non-blocking exclusive lock. Return whether it was acquired.

    ``False`` for every failure, not only for a lock someone else holds: the
    callers that take a lock to own it only need to know they did not get it.
    A caller that reports on a lock uses :func:`try_lock_strict`.
    """
    try:
        return try_lock_strict(fd)
    except OSError:
        return False


def unlock(fd: int) -> None:
    """Best-effort unlock of ``fd`` without closing it."""
    try:
        if os.name == "nt":  # pragma: no cover - Windows CI
            msvcrt: Any = importlib.import_module("msvcrt")

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl: Any = importlib.import_module("fcntl")

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        logger.debug("failed to release file lock", exc_info=True)


def release_lock(fd: int) -> None:
    """Best-effort unlock and close of ``fd``."""
    unlock(fd)
    try:
        os.close(fd)
    except OSError:
        logger.debug("failed to close lock fd", exc_info=True)
