"""Spawn a detached surfacing daemon — shared by ``mms daemon start`` (ops) and
``mms hook`` auto-spawn (hot path).

The lock is used here only as a **liveness probe** (acquire + immediately
release): if a daemon already owns the lifetime lock we don't launch a
duplicate; if it's free we spawn a detached child *outside* the lock. The child
re-acquires the lifetime lock as the authoritative single owner (see
:mod:`~memtomem_stm.daemon.server`), so a rare concurrent double-spawn just has
one child exit before warming an LTM — no orphaned warm process. Spawning is
fire-and-forget: it never blocks on readiness, so the warm daemon serves the
*next* call, not this one.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any

from memtomem_stm.utils.child_reaper import note_detached_child

if TYPE_CHECKING:
    from memtomem_stm.config import STMConfig

logger = logging.getLogger(__name__)


def _spawn_detached() -> None:
    """Launch ``mms daemon run --detached`` as a background process."""
    cmd = [sys.executable, "-m", "memtomem_stm", "daemon", "run", "--detached"]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI only
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    child = subprocess.Popen(cmd, **kwargs)
    # Detached, but still our direct child — nothing double-forks here. Say so,
    # or a caller's teardown leaked-child sweep reads the shared daemon as a
    # leak and kills it (and the LTM it holds for everyone else) on exit (#906).
    note_detached_child(child.pid)


def request_spawn(config: STMConfig) -> bool:
    """Fire-and-forget spawn a detached daemon iff none owns *this config's* lock.

    The lock is keyed by ``config``'s fingerprint, so a daemon running under a
    *different* config holds a different lock and never blocks this spawn — the
    new daemon coexists with it. Returns ``True`` if a child was launched,
    ``False`` if a same-config daemon already owns the lifetime lock (alive or
    mid-startup) so we deferred, or if the lock file couldn't be opened. Never
    blocks on readiness and never raises.

    The lock is a probe only (acquire + release); the spawned child re-acquires
    it for its lifetime as the single owner.
    """
    from memtomem_stm.daemon.discovery import config_fingerprint
    from memtomem_stm.daemon.locking import lock_path, single_owner_lock

    try:
        with single_owner_lock(lock_path(config.data_dir, config_fingerprint(config))) as acquired:
            alive = not acquired  # held by a live/starting daemon → don't pile on
    except OSError:
        logger.debug("request_spawn: could not open lock file", exc_info=True)
        return False
    if alive:
        return False
    _spawn_detached()  # spawn OUTSIDE the lock (already released above)
    return True
