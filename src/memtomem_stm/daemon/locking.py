"""Cross-platform single-owner advisory lock.

Used to keep two concurrent ``mms hook`` auto-spawns (or two ``mms daemon
start`` invocations) from racing to launch a second daemon. **This lock is the
ownership primitive** — the port bind is *not*: the daemon binds an
OS-assigned ephemeral port (``port 0``), so two daemons would simply get two
different ports and both succeed, and the handshake file is last-writer-wins
(the second overwrites the first, orphaning it). The guard against a second
daemon is therefore: hold this lock across the start decision and re-check a
``ping`` under it before spawning. A caller that fails to acquire assumes
another process is mid-spawn and just polls the handshake file.

POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking``. Both are advisory
and auto-released if the holding process dies, so a crashed spawner never
wedges the lock.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_FILENAME = "stm-daemon.lock"


def lock_path(data_dir: Path) -> Path:
    """Absolute path to the spawn lock file under ``data_dir``."""
    return (data_dir / LOCK_FILENAME).expanduser()


@contextmanager
def single_owner_lock(path: Path) -> Iterator[bool]:
    """Try (non-blocking) to take an exclusive lock on ``path``.

    Yields ``True`` if acquired (and releases on exit), ``False`` if another
    process holds it. Never raises on contention — only on an inability to open
    the lock file at all, which the caller may also treat as "don't spawn".
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        acquired = _try_lock(fd)
        yield acquired
    finally:
        if acquired:
            _unlock(fd)
        os.close(fd)


def _try_lock(fd: int) -> bool:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI only
        import msvcrt

        try:
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


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI only
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        logger.debug("failed to release spawn lock", exc_info=True)
