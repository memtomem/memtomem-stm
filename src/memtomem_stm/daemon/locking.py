"""Cross-platform single-owner advisory lock — the daemon's lifetime ownership.

**This lock is the ownership primitive** — the port bind is *not*: the daemon
binds an OS-assigned ephemeral port (``port 0``), so two daemons would simply
get two different ports and both succeed, and the handshake file is
last-writer-wins (the second overwrites the first, orphaning it). Instead, the
running daemon **holds this lock for its entire lifetime** (acquired before it
warms its engine), so "lock held" is the authoritative "a daemon is alive"
signal. A would-be spawner (``mms hook`` auto-spawn, ``mms daemon start``) only
*probes* the lock: if it's held, a daemon already owns it, so don't launch a
duplicate; if it's free, spawn — and the spawned child re-acquires the lock as
the single owner, so a rare concurrent double-spawn just has the loser exit
before warming an LTM.

The lock file is **keyed by the config fingerprint** (``stm-daemon-<fp>.lock``),
mirroring the handshake file, so a daemon under one config holds a *different*
lock than a daemon under another: they coexist, and a config-A daemon never
blocks a config-B hook from spawning the daemon it actually needs (config-drift
coexistence). "Single owner" is therefore per-config — exactly one daemon *per
distinct config*.

Two access shapes:
- :func:`single_owner_lock` — a context manager for the *probe* (acquire,
  yield, release). Used by ``request_spawn`` and ``mms daemon start``.
- :func:`open_lock_fd` / :func:`try_lock` / :func:`release_lock` — the daemon
  holds an fd across its whole asyncio run, retrying ``try_lock`` with
  ``await asyncio.sleep`` and releasing on teardown.

POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking``. Both are advisory
and auto-released if the holding process dies, so a crashed daemon never wedges
the lock (the next probe finds it free and self-heals).
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

# Shares the handshake's ``stm-daemon`` prefix; differs only by extension. The
# fingerprint is a 16-hex digest, always a filesystem-safe filename component.
LOCK_PREFIX = "stm-daemon"


def lock_path(data_dir: Path, fingerprint: str) -> Path:
    """Per-config lifetime ownership lock ``stm-daemon-<fingerprint>.lock``.

    Keyed by the config fingerprint so daemons under different configs hold
    different locks and coexist (see the module docstring). Callers pass
    :func:`~memtomem_stm.daemon.discovery.config_fingerprint` of their own
    effective config — the same value used to derive the handshake path.
    """
    return (data_dir / f"{LOCK_PREFIX}-{fingerprint}.lock").expanduser()


def open_lock_fd(path: Path) -> int:
    """Open (creating) the lock file and return its fd. Caller owns the fd and
    must :func:`release_lock` it. Raises ``OSError`` only if the file can't be
    opened at all (which a caller may treat as "don't run / don't spawn")."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)


def try_lock(fd: int) -> bool:
    """Non-blocking exclusive lock attempt on ``fd``. ``True`` iff acquired."""
    return _try_lock(fd)


def release_lock(fd: int) -> None:
    """Best-effort unlock + close of a lock fd. Safe to call even if ``fd`` was
    never locked (a never-acquired retry that timed out) — the unlock is
    swallowed and the fd is always closed."""
    _unlock(fd)
    try:
        os.close(fd)
    except OSError:
        logger.debug("failed to close lock fd", exc_info=True)


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
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI only
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
        if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI only
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        logger.debug("failed to release spawn lock", exc_info=True)
