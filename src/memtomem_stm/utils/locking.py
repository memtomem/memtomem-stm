"""Small cross-platform advisory file-lock primitives.

Windows ``msvcrt.locking`` locks bytes from the current file position, so the
lock file must contain at least one byte and every operation must seek back to
offset zero.  Keeping that detail here prevents the daemon and ``mms`` state
writers from drifting into subtly different implementations.
"""

from __future__ import annotations

import logging
import importlib
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def open_lock_fd(path: Path) -> int:
    """Open ``path`` for locking and return an fd owned by the caller."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    if os.name == "nt":  # pragma: no cover - Windows CI
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
        except Exception:
            os.close(fd)
            raise
    return fd


def try_lock(fd: int) -> bool:
    """Attempt a non-blocking exclusive lock. Return whether it was acquired."""
    if os.name == "nt":  # pragma: no cover - Windows CI
        msvcrt: Any = importlib.import_module("msvcrt")

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError):
        return False


def unlock(fd: int) -> None:
    """Best-effort unlock of ``fd`` without closing it."""
    try:
        if os.name == "nt":  # pragma: no cover - Windows CI
            msvcrt: Any = importlib.import_module("msvcrt")

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

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
