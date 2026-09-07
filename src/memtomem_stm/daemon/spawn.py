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
import threading
from typing import TYPE_CHECKING, Any

from memtomem_stm.utils.child_reaper import release_claim, spawn_claimed

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
    # Detached, but still our direct child — nothing double-forks here. Claim it
    # under the sweep's lock, or a caller's teardown leaked-child sweep reads the
    # shared daemon as a leak and kills it (with the LTM it holds for everyone
    # else) on exit (#906).
    # Start the waiter first: thread exhaustion must not leave an unowned child.
    # Keep the Popen object alive rather than relying on subprocess._active,
    # which only reaps on a later Popen and can leave a long-lived host zombies.
    ready = threading.Event()
    child: subprocess.Popen[bytes] | None = None
    serial: int | None = None

    def reap() -> None:
        ready.wait()
        if child is not None and serial is not None:
            child.wait()
            # The claim outranks the child only while the child pins the pid.
            # Reaping frees that number for reuse, so keeping the claim would
            # spare whichever later child inherits it (#906). The serial is what
            # keeps this from retiring a *newer* spawn's claim on the same pid.
            release_claim(child.pid, serial)

    def launch() -> int:
        nonlocal child
        child = subprocess.Popen(cmd, **kwargs)
        return child.pid

    threading.Thread(target=reap, name="stm-daemon-reaper", daemon=True).start()
    try:
        serial = spawn_claimed(launch)
    finally:
        # Also release the waiter if Popen fails. Waiting never holds the claim
        # lock and never joins the shared daemon during host shutdown.
        ready.set()


def request_spawn(config: STMConfig, *, propagate_errors: bool = False) -> bool:
    """Fire-and-forget spawn a detached daemon iff none owns *this config's* lock.

    The lock is keyed by ``config``'s fingerprint, so a daemon running under a
    *different* config holds a different lock and never blocks this spawn — the
    new daemon coexists with it. Returns ``True`` if a child was launched.
    ``False`` covers three different things, and no caller can tell them apart:
    a same-config daemon already owns the lifetime lock (alive or mid-startup)
    so we deferred, the lock file couldn't be opened, or the spawn itself failed
    (no thread, no fork). Never blocks on readiness, and never raises — the hot
    path (``mms hook``, the daemon LTM adapter) only ever degrades to cold
    surfacing, so an exception there buys nothing.

    *propagate_errors* re-raises that third case for callers that do more than
    degrade: ``mms daemon start`` retries on a schedule and reports at the end,
    so it needs to charge a failed spawn against its retry budget and name the
    cause instead of blaming a daemon log that was never written.

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
    try:
        _spawn_detached()  # spawn OUTSIDE the lock (already released above)
    except (OSError, RuntimeError):
        if propagate_errors:
            raise
        logger.warning("Could not spawn surfacing daemon", exc_info=True)
        return False
    return True
