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
:func:`spawn_claimed` are skipped, an unanswerable probe sweeps
nothing, and none of those exclusions is re-litigated later to try to recover a
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
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Grace window between SIGTERM and SIGKILL for a leaked child — matches the
# escalation timeout mcp's own stdio shutdown sequence uses.
LEAK_KILL_ESCALATE_SECONDS = 2.0

_ESCALATION_POLL_SECONDS = 0.05

# Pids of children deliberately spawned to outlive us — see
# :func:`spawn_claimed`. The lock spans the spawn itself, not just the
# bookkeeping, so a sweep can never observe a child that exists but is not yet
# claimed. Claims are never expired: a pid could be recycled onto a child we
# later leak, but chasing that would trade the cheap mistake (a missed leak) for
# the expensive one (killing the live daemon whose pid we misjudged).
_detached_pids: set[int] = set()
_detached_lock = threading.Lock()

# Ceiling on waiting for a detached spawn to finish claiming. Long enough that a
# healthy Popen never trips it, short enough that a wedged one cannot park a
# shutdown — the sweep answers "unknown" and sweeps nothing instead.
_CLAIM_LOCK_WAIT_SECONDS = 5.0

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


def probe_child_pids() -> set[int] | None:
    """This process's direct child pids, or ``None`` when the probe failed.

    The distinction matters wherever the answer is used as a *spare* list: an
    empty set there means "nothing to protect" and arms the sweep against
    everything, so a transient ``pgrep`` failure would invert this module's
    bias. Callers that use the answer as a kill list want the empty set — see
    :func:`direct_child_pids`. ``pgrep`` never reports its own pid, so the probe
    doesn't observe itself.
    """
    if sys.platform == "win32":
        return None
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=_PGREP_TIMEOUT_SECONDS,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {int(tok) for tok in out.split() if tok.isdigit()}


def direct_child_pids() -> set[int]:
    """Best-effort set of this process's direct child pids (POSIX only).

    A failed or unavailable probe answers "no children", degrading the sweep to
    a no-op rather than blocking shutdown. Never use this where the answer
    spares processes — see :func:`probe_child_pids`.
    """
    return probe_child_pids() or set()


def signal_pid(pid: int, sig: int) -> None:
    """Best-effort signal to *pid*'s process group, or *pid* alone when it
    shares our own group (never signal our own group).

    A stdio child is spawned with ``start_new_session=True``, so it leads its
    own session and the group kill also reaches grandchildren — an abandoned
    ``uv run memtomem`` wrapper's live server is only reachable this way.

    That is also why a failed ``getpgid`` is not the end: it raises ESRCH for a
    zombie leader on some platforms, and a zombie leader is exactly the case
    where the group still holds the live grandchild we are trying to reach.
    ``start_new_session`` makes the leader's pid its pgid, so fall back to that.
    """
    if sys.platform == "win32":  # pragma: no cover — unreachable: no pgrep pids
        return
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        # A zombie leader answers ESRCH here on some platforms, and that is the
        # case where the group still holds the live grandchild. ``PermissionError``
        # is NOT folded in: it means the process exists but is not ours, so its
        # pid is no evidence about which group that number names.
        pgid = pid  # by construction a stdio child leads its own group
    except PermissionError:
        return
    try:
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


def _group_has_survivors(pid: int) -> bool:
    """True when *pid*'s process group still holds someone other than *pid*.

    A stdio child leads its own session, so its group is exactly the wrapper
    plus whatever it spawned. Asking this only makes sense once *pid* itself is
    gone, and it is a ``pgrep`` round trip, so callers must keep it out of any
    poll. Answers ``False`` when it cannot tell — the sweep has already sent
    SIGTERM, and escalating on a guess would aim SIGKILL at a group we could
    not confirm is ours.
    """
    if sys.platform == "win32":  # pragma: no cover — no process groups
        return False
    try:
        out = subprocess.run(
            ["pgrep", "-g", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PGREP_TIMEOUT_SECONDS,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(int(tok) != pid for tok in out.split() if tok.isdigit())


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
    # What we signal is the process *group*, so what the escalation has to ask
    # about is the group — not the pid we happen to name it by. An exited leader
    # leaves that loop immediately, which is right when its group went with it
    # and wrong when it did not: an abandoned wrapper's server keeps running in
    # the group of a leader that is now a zombie, and would never see the
    # SIGKILL. The re-check is a subprocess, so it runs once, at the end, only
    # for leaders that exited — never in the poll.
    remaining |= {pid for pid in pids - remaining if _group_has_survivors(pid)}
    for pid in remaining:
        signal_pid(pid, signal.SIGKILL)


def spawn_claimed(spawn: Callable[[], int]) -> None:
    """Run *spawn* and claim its pid as meant to outlive us, atomically.

    The shared surfacing daemon is spawned detached but is still a direct child
    of whoever launched it (:mod:`memtomem_stm.daemon.spawn` does not
    double-fork), so a sweep that treats every surviving child as a leak takes
    down the daemon — and the LTM it holds for every other consumer — on each
    shutdown. Claiming the pid afterwards is not enough: the spawn runs on a
    worker thread (``request_spawn`` under ``asyncio.to_thread``), so a teardown
    landing between the spawn returning and the claim would see a pid nothing
    claims and kill it.

    Taking *spawn* rather than yielding a claim callable is deliberate: a
    caller cannot forget to claim what it spawned, and the lock provably spans
    both. Holding the lock across a spawn is why the sweep's read of the claims
    is bounded — see :func:`leaked_child_pids`.
    """
    with _detached_lock:
        _detached_pids.add(spawn())


def leaked_child_pids(baseline: set[int]) -> set[int] | None:
    """Direct children that are ours and were not meant to outlive us.

    ``None`` means the question could not be answered — a failed probe, or a
    detached spawn holding the claim lock longer than we are willing to wait —
    and the caller must then sweep nothing.

    *baseline* is the set of children that existed before we started: they
    predate us, so they are somebody else's (this lifespan can be hosted in a
    process with its own subprocesses). :func:`sweep_leaked_children` is what
    turns "we never learned the baseline" into sweeping nothing.

    The probe runs *before* the claim set is read, and that order is the whole
    guarantee. :func:`spawn_claimed` holds the same lock across its spawn, so a
    pid this probe saw was either claimed before the probe ran or belongs to a
    spawn that had not started — read the claims the other way round and a spawn
    completing between the two reads yields a live child that the stale claim
    snapshot does not cover.

    A pid that has already exited stays in the answer. An unreaped zombie
    *leader* still pins its pid, so its pgid cannot have been recycled, and the
    live grandchild in that group is reachable only by signalling it. Skipping
    the corpse would skip the leak.
    """
    seen = probe_child_pids()
    if seen is None:
        logger.warning("Leaked-child sweep skipped: could not enumerate this process's children")
        return None
    seen -= baseline
    if not _detached_lock.acquire(timeout=_CLAIM_LOCK_WAIT_SECONDS):
        # A spawn is wedged mid-Popen. This is the one wait on the shutdown path
        # that has no other ceiling, and the sweep must never be the reason the
        # process fails to exit.
        logger.warning(
            "Leaked-child sweep skipped: a detached spawn held the claim lock for over %.1fs",
            _CLAIM_LOCK_WAIT_SECONDS,
        )
        return None
    try:
        return seen - _detached_pids
    finally:
        _detached_lock.release()


def sweep_leaked_children(*, baseline: set[int] | None) -> None:
    """Terminate every direct child that is ours and outlived its owner.

    *baseline* is the children that existed at startup, or ``None`` if that
    probe failed — and ``None`` sweeps nothing. An empty set would say "there
    was nothing to protect" and arm the sweep against every pre-existing
    process; a failed probe knows no such thing.

    Logs under this module rather than the caller's, so one logger name covers
    every leak this process reports regardless of which teardown swept.

    Best-effort and never raises: it is the last step of a teardown, so a
    failure here must not replace one leak with a different one.
    """
    if baseline is None:
        logger.warning("Leaked-child sweep skipped: no startup baseline to compare against")
        return
    try:
        leaked = leaked_child_pids(baseline)
        if not leaked:
            return
        logger.warning("Terminating leaked child process(es): %s", sorted(leaked))
        terminate_leaked_children(leaked)
    except Exception:
        logger.warning("Leaked-child sweep failed", exc_info=True)
