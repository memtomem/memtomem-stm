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

**It is deliberately biased toward sparing.** A leak it misses costs what the
code cost before the sweep existed; a process it kills wrongly is new damage
this module inflicted — the shared daemon's whole session, or a host's worker.
The two mistakes are not symmetric, so every ambiguity resolves toward leaving
the process alone: children that predate us, children claimed via
:func:`spawning_detached_child`, and pids that have already exited are all
skipped, and none of those exclusions is re-litigated later to try to recover a
leak. What it cannot do is attribute a child a *host* process spawned after we
started; a host embedding this lifespan and spawning its own subprocesses
should claim them the way ``daemon/spawn.py`` does.

Extracted from ``daemon/server.py``, which has carried this sweep since the
daemon's own leaked-LTM-child incident; the stdio MCP server needs the same
machinery and must not import the daemon server to get it.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Grace window between SIGTERM and SIGKILL for a leaked child — matches the
# escalation timeout mcp's own stdio shutdown sequence uses.
LEAK_KILL_ESCALATE_SECONDS = 2.0

_ESCALATION_POLL_SECONDS = 0.05

# Pids of children deliberately spawned to outlive us — see
# :func:`spawning_detached_child`. The lock spans the spawn itself, not just the
# bookkeeping, so a sweep can never observe a child that exists but is not yet
# claimed. Claims are never expired: a pid could be recycled onto a child we
# later leak, but chasing that would trade the cheap mistake (a missed leak) for
# the expensive one (killing the live daemon whose pid we misjudged).
_detached_pids: set[int] = set()
_detached_lock = threading.Lock()

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
    corpse. ``waitid`` answers correctly — and ``WNOWAIT`` peeks at the exit
    status without consuming it, so the probe leaves the child
    waitable, so detecting its exit never steals it from an owner still holding
    a ``Popen``. Falls back to the signal probe for a pid that is not ours to
    wait on.
    """
    if sys.platform == "win32":  # pragma: no cover — no waitid; sweep is a no-op
        return not is_pid_alive(pid)
    try:
        # The ignores are for typeshed gating waitid to Linux; every POSIX
        # target here has it (macOS included), and the win32 guard above is the
        # real platform bound.
        info = os.waitid(  # type: ignore[attr-defined]
            os.P_PID,  # type: ignore[attr-defined]
            pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,  # type: ignore[attr-defined]
        )
    except ChildProcessError:
        return not is_pid_alive(pid)  # not ours to wait on, or already reaped
    except OSError:  # pragma: no cover — EINVAL/EPERM are not "it exited"
        return False
    return info is not None


def terminate_leaked_children(
    pids: set[int], *, escalate_seconds: float = LEAK_KILL_ESCALATE_SECONDS
) -> None:
    """SIGTERM each leaked child's process group, then SIGKILL stragglers.

    Blocking, and deliberately not a coroutine. This is the last thing a
    teardown does, and that teardown may itself be unwinding under
    cancellation: an ``await`` between the SIGTERM and the SIGKILL is exactly
    where a ``CancelledError`` lands, which strands the SIGTERM-ignoring child
    the escalation exists for. Blocking costs nothing here — the process has
    nothing left to run, and the wait only happens when a child actually
    survived its ``stop()``.
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


@contextmanager
def spawning_detached_child() -> Iterator[Callable[[int], None]]:
    """Spawn a child meant to outlive us, without a sweep racing the spawn.

    The shared surfacing daemon is spawned detached but is still a direct child
    of whoever launched it (:mod:`memtomem_stm.daemon.spawn` does not
    double-fork), so a sweep that treats every surviving child as a leak takes
    down the daemon — and the LTM it holds for every other consumer — on each
    shutdown. Registering the pid after the fact is not enough: the spawn runs
    on a worker thread (``request_spawn`` under ``asyncio.to_thread``), so a
    teardown landing between ``Popen`` returning and the registration would see
    a pid nothing claims and kill it.

    Holding the lock across both means a concurrent sweep either runs entirely
    before the spawn — when the pid does not exist yet, so it cannot be swept —
    or entirely after it is claimed.
    """
    with _detached_lock:
        yield _detached_pids.add


def leaked_child_pids(baseline: set[int] | None = None) -> set[int]:
    """Direct children that are ours, alive, and were not meant to outlive us.

    *baseline* is the set of children that existed before we started: they
    predate us, so they are somebody else's (this lifespan can be hosted in a
    process with its own subprocesses).

    The probe runs *before* the claim set is read, and that order is the whole
    guarantee. :func:`spawning_detached_child` holds the same lock across its
    spawn, so a pid this probe saw was either claimed before the probe ran or
    belongs to a spawn that had not started — read the claims the other way
    round and a spawn completing between the two reads yields a live child that
    the stale claim snapshot does not cover.

    Pids that have already exited are dropped as well: a fire-and-forget child
    nobody reaped lingers as a zombie that ``pgrep`` still lists, and signalling
    it would mean group-signalling a session that is no longer ours.
    """
    seen = direct_child_pids() - (baseline or set())
    with _detached_lock:
        seen -= _detached_pids
    return {pid for pid in seen if not _has_exited(pid)}


def sweep_leaked_children(*, baseline: set[int] | None = None) -> None:
    """Terminate every direct child that is ours and outlived its owner.

    Logs under this module rather than the caller's, so one logger name covers
    every leak this process reports regardless of which teardown swept.

    Best-effort and never raises: it is the last step of a teardown, so a
    failure here must not replace one leak with a different one.
    """
    try:
        leaked = leaked_child_pids(baseline)
        if not leaked:
            return
        logger.warning("Terminating leaked child process(es): %s", sorted(leaked))
        terminate_leaked_children(leaked)
    except Exception:
        logger.warning("Leaked-child sweep failed", exc_info=True)
