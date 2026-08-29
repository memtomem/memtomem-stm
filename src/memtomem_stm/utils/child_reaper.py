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

logger = logging.getLogger(__name__)

# Grace window between SIGTERM and SIGKILL for a leaked child — matches the
# escalation timeout mcp's own stdio shutdown sequence uses.
LEAK_KILL_ESCALATE_SECONDS = 2.0

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


async def terminate_leaked_children(
    pids: set[int], *, escalate_seconds: float = LEAK_KILL_ESCALATE_SECONDS
) -> None:
    """SIGTERM each leaked child's process group, then SIGKILL stragglers."""
    if sys.platform == "win32":  # pragma: no cover — direct_child_pids is empty
        return
    for pid in pids:
        signal_pid(pid, signal.SIGTERM)
    deadline = asyncio.get_running_loop().time() + escalate_seconds
    remaining = {pid for pid in pids if is_pid_alive(pid)}
    while remaining and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        remaining = {pid for pid in remaining if is_pid_alive(pid)}
    for pid in remaining:
        signal_pid(pid, signal.SIGKILL)
