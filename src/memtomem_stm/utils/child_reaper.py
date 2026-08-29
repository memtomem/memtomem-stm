"""Terminate child processes this process spawned but failed to reap.

Every STM entry point that talks to an upstream MCP server spawns it as a stdio
child, and every one of those children is owned by a context manager that kills
it *if the context is actually exited*. Teardown paths that return without
exiting one — an owner task lost mid-lifetime, a bounded join that gave up on a
stuck ``aclose`` — leave a live child whose parent is about to exit, and the
child then survives as an orphan holding its own resources (#906).

The sweep is the belt-and-braces backstop for that class: enumerate our direct
children, signal their process groups, escalate to ``SIGKILL``. It degrades to a
no-op on Windows and wherever ``pgrep`` is unavailable rather than blocking
shutdown, so it can never be the reason a process fails to exit.

Extracted from ``daemon/server.py``, which has carried this sweep since the
daemon's own leaked-LTM-child incident; the stdio MCP server needs the same
machinery and must not import the daemon server to get it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

# Grace window between SIGTERM and SIGKILL for a leaked child — matches the
# escalation timeout mcp's own stdio shutdown sequence uses.
LEAK_KILL_ESCALATE_SECONDS = 2.0

_ESCALATION_POLL_SECONDS = 0.05

# Pids of children deliberately spawned to outlive us — see
# :func:`note_detached_child`. Written from a worker thread (``request_spawn``
# runs under ``asyncio.to_thread``) and read on the shutdown path; a bare set
# is enough, since ``add`` is atomic and a missed entry is impossible (the
# spawn returns before its pid can be swept).
_detached_pids: set[int] = set()

# Looked up rather than called directly: typeshed gates ``os.waitid`` to Linux,
# but every POSIX target here provides it (macOS included), and it is absent
# only on Windows — where the sweep is already a no-op. ``WNOWAIT`` peeks at the
# exit status without consuming it, so probing never steals a child from an
# owner still holding a ``Popen``.
_waitid = getattr(os, "waitid", None)
_P_PID = getattr(os, "P_PID", 0)
_WAITID_PEEK = getattr(os, "WEXITED", 0) | getattr(os, "WNOHANG", 0) | getattr(os, "WNOWAIT", 0)

# Ceiling on the `pgrep` probe. It is on the shutdown path, so a wedged process
# table must not become the new reason we never exit.
_PGREP_TIMEOUT_SECONDS = 5.0


def is_pid_alive(pid: int) -> bool:
    """Best-effort liveness probe for a PID. Liveness for *daemon* decisions
    should prefer a socket ``ping``; this is a secondary signal for status/stop.
    """
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI only
        # No portable signal-0 on Windows; assume alive and let the ping decide.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    except OverflowError:
        # A pid beyond the platform's pid_t range (e.g. a huge integer from
        # a hand-edited handshake) raises OverflowError before ESRCH — it
        # cannot name a live process. Not an OSError subclass, so it needs
        # its own arm.
        return False
    return True


def direct_child_pids() -> set[int]:
    """Best-effort set of this process's direct child pids (POSIX only).

    Used by the teardown leak sweep. Returns an empty set on Windows or when
    ``pgrep`` is unavailable/fails, degrading the sweep to a no-op rather
    than blocking shutdown. ``pgrep`` never reports its own pid, so the
    probe doesn't observe itself.
    """
    if sys.platform == "win32":
        return set()
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=_PGREP_TIMEOUT_SECONDS,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {int(tok) for tok in out.split() if tok.isdigit()}


def signal_pid(pid: int, sig: int) -> None:
    """Best-effort signal to *pid*'s process group, or *pid* alone when it
    shares our own group (never signal our own group). A stdio child is
    spawned with ``start_new_session=True``, so the group kill also reaches
    grandchildren (e.g. ``uv run memtomem`` wrappers)."""
    if sys.platform == "win32":  # pragma: no cover — unreachable: no pgrep pids
        return
    try:
        pgid = os.getpgid(pid)
        if pgid != os.getpgid(0):
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _has_exited(pid: int) -> bool:
    """True once *pid* is gone, counting a zombie as gone.

    A signal-0 probe cannot see the difference: a child we signalled but that
    nobody reaped stays visible as a zombie, so polling with
    :func:`is_pid_alive` alone reports every terminated fire-and-forget child as
    still running, burns the whole escalation window, and then SIGKILLs a
    corpse. ``waitid`` answers correctly — and ``WNOWAIT`` leaves the child
    waitable, so detecting its exit never steals it from an owner still holding
    a ``Popen``. Falls back to the signal probe for a pid that is not ours to
    wait on.
    """
    if _waitid is None:  # pragma: no cover — Windows; the sweep is a no-op there
        return not is_pid_alive(pid)
    try:
        info = _waitid(_P_PID, pid, _WAITID_PEEK)
    except ChildProcessError:
        return not is_pid_alive(pid)  # not ours to wait on, or already reaped
    except OSError:  # pragma: no cover — EINVAL/EPERM are not "it exited"
        return False
    return info is not None


async def terminate_leaked_children(
    pids: set[int], *, escalate_seconds: float = LEAK_KILL_ESCALATE_SECONDS
) -> None:
    """SIGTERM each leaked child's process group, then SIGKILL stragglers."""
    if sys.platform == "win32":  # pragma: no cover — direct_child_pids is empty
        return
    for pid in pids:
        signal_pid(pid, signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + escalate_seconds
    remaining = {pid for pid in pids if not _has_exited(pid)}
    while remaining and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(_ESCALATION_POLL_SECONDS)
        remaining = {pid for pid in remaining if not _has_exited(pid)}
    for pid in remaining:
        signal_pid(pid, signal.SIGKILL)


def terminate_leaked_children_sync(
    pids: set[int], *, escalate_seconds: float = LEAK_KILL_ESCALATE_SECONDS
) -> None:
    """Blocking twin of :func:`terminate_leaked_children`.

    The escalation must not be cancellable. This runs at the very end of a
    teardown that may itself be unwinding under cancellation, where an
    ``await`` between the SIGTERM and the SIGKILL is exactly where a
    ``CancelledError`` lands — leaving a SIGTERM-ignoring child alive, which is
    the case the escalation exists for. Blocking the loop is free here: the
    process has nothing left to run, and the wait only happens when a child
    actually survived its stop().
    """
    if sys.platform == "win32":  # pragma: no cover — direct_child_pids is empty
        return
    for pid in pids:
        signal_pid(pid, signal.SIGTERM)
    deadline = time.monotonic() + escalate_seconds
    remaining = {pid for pid in pids if not _has_exited(pid)}
    while remaining and time.monotonic() < deadline:
        time.sleep(_ESCALATION_POLL_SECONDS)
        remaining = {pid for pid in remaining if not _has_exited(pid)}
    for pid in remaining:
        signal_pid(pid, signal.SIGKILL)


def note_detached_child(pid: int) -> None:
    """Record a child deliberately spawned to outlive this process.

    The shared surfacing daemon is spawned detached but is still a direct child
    of whoever launched it (:mod:`memtomem_stm.daemon.spawn` does not
    double-fork), so a sweep that treats every surviving child as a leak would
    take down the daemon — and the LTM it holds for every other consumer — on
    each shutdown. Registered pids are excluded from :func:`leaked_child_pids`.
    """
    _detached_pids.add(pid)


def leaked_child_pids() -> set[int]:
    """Direct children that were *not* meant to outlive us."""
    return direct_child_pids() - _detached_pids


def sweep_leaked_children(log: logging.Logger | None = None) -> None:
    """Terminate every direct child that was not meant to outlive us.

    Best-effort and never raises: it is the last step of a teardown, so a
    failure here must not replace one leak with a different one.
    """
    at = log or logger
    try:
        leaked = leaked_child_pids()
        if not leaked:
            return
        at.warning("Terminating leaked child process(es): %s", sorted(leaked))
        terminate_leaked_children_sync(leaked)
    except Exception:
        at.warning("Leaked-child sweep failed", exc_info=True)
