"""Shutdown helpers: AnyIO cancel-scope detection, and bounded in-flight drain.

AnyIO cancel scopes are task-affine: exiting one from a different task than
entered it raises a ``RuntimeError`` whose message is the only stable way to
recognize it. STM-owned MCP client contexts now use dedicated lifecycle owner
tasks, so this is no longer an expected construction. The exact-match helper
remains a narrow shutdown barrier for an upstream SDK unwind or a legacy
injected adapter; mixed exception groups are never classified as clean.

:func:`drain_or_warn` is the unrelated half: the ceiling every ``close()``
puts on waiting for its in-flight callers, so one stuck request cannot
hang shutdown (#867).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

_CANCEL_SCOPE_SHUTDOWN_MESSAGES = (
    "Attempted to exit a cancel scope that isn't the current tasks's current cancel scope",
    "Attempted to exit cancel scope in a different task than it was entered in",
)


def is_anyio_cancel_scope_shutdown_error(exc: RuntimeError) -> bool:
    """Return true for the AnyIO shutdown cleanup error seen on stdio EOF."""

    message = str(exc)
    return any(marker in message for marker in _CANCEL_SCOPE_SHUTDOWN_MESSAGES)


def is_clean_cancel_scope_shutdown(exc: BaseException) -> bool:
    """True when *exc* consists only of AnyIO cancel-scope shutdown errors.

    ``mcp.run()`` executes the server inside ``anyio.create_task_group()``,
    and anyio >= 4 (strict task groups) wraps anything escaping a task group
    in an ``ExceptionGroup`` — on stdio EOF the cancel-scope RuntimeError
    reaches ``main()`` in that wrapped shape, not bare. Walk groups
    recursively and treat the tree as a clean shutdown only when EVERY leaf
    is the cancel-scope error; any other leaf is a real failure that must
    hit the exception barrier.
    """
    if isinstance(exc, BaseExceptionGroup):
        return all(is_clean_cancel_scope_shutdown(sub) for sub in exc.exceptions)
    return isinstance(exc, RuntimeError) and is_anyio_cancel_scope_shutdown_error(exc)


# Grace added to an in-flight call's own captured deadline to get the drain
# ceiling. The call is already bounded by the timeout it captured when it
# started; this only covers the hop from that deadline firing to the
# ``finally`` releasing the gate.
CLOSE_DRAIN_GRACE_SECONDS = 2.0

# Stand-in deadline for a timeout that is not one. Validation admits ``+inf``
# outright (``gt=0.0``; the same hole #722 closed for the hook deadline), and
# ``NaN`` — which that comparison does reject — still arrives by assignment,
# since these models are mutable and assignment is not re-validated. Either
# would restore exactly the unbounded shutdown this gate exists to prevent.
# A *finite* timeout, however long, is a bound the operator chose: the drain
# honors it rather than cutting a valid call short.
UNBOUNDED_TIMEOUT_FALLBACK_SECONDS = 60.0


def normalize_timeout(timeout: float) -> float:
    """Replace a timeout that is not a bound with one that is.

    ``llm_timeout_seconds`` is validated ``gt=0.0``, which admits ``+inf``
    (``NaN`` fails that comparison, but these models are mutable and
    assignment is not re-validated — the PR that added this gate mutates one
    in its own tests). Neither is a deadline: ``+inf`` makes
    ``asyncio.wait_for`` unbounded, and ``NaN`` collapses through comparisons.

    Callers normalize ONCE and use the result for both their own
    ``wait_for`` and their gate registration, so the deadline ``close()``
    drains against is the deadline the call actually runs under. The gate
    normalizes again defensively for any caller that forgets.
    """
    return timeout if math.isfinite(timeout) else UNBOUNDED_TIMEOUT_FALLBACK_SECONDS


class InFlightGate:
    """Registration gate shared by the LLM helpers' ``close()`` drains.

    Tracks the callers currently mid-request by the **absolute deadline** each
    one captured when it started, so ``close()`` waits out what is actually
    left of them:

    * A *relative* timeout would restart the clock — a 60s call already 59s in
      would hand shutdown another 60s instead of the remaining second.
    * The ceiling must come from the live registrations only. Re-reading the
      (mutable) config instead can undercut a live call when another task
      lowers ``llm_timeout_seconds``, and folding the config in alongside the
      registrations lets raising it inflate shutdown arbitrarily. The config
      is the fallback for "nothing in flight", nothing more.
    * Deadlines are per registration and dropped on ``leave``, so a long
      caller finishing does not leave its ceiling behind for a short one.
    * A non-finite capture is not a deadline at all; it is replaced at
      capture time (see :func:`normalize_timeout`) by
      :data:`UNBOUNDED_TIMEOUT_FALLBACK_SECONDS` so it cannot restore an
      unbounded shutdown — nor, as ``NaN``, silently collapse another live
      caller's ceiling to zero through the comparisons.

    ``clock`` is injectable so tests can pin the arithmetic without sleeping.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.closed: bool = False
        self.idle: asyncio.Event = asyncio.Event()
        self.idle.set()
        self._clock = clock
        self._deadlines: dict[int, float] = {}
        self._next_token: int = 0

    @property
    def in_flight(self) -> int:
        return len(self._deadlines)

    def try_enter(self, timeout: float) -> int | None:
        """Register a caller whose own bound expires *timeout* from now.

        Returns the token to hand back to :meth:`leave`, or ``None`` when the
        gate is already closed — the caller must then take its degraded path
        instead of touching the resource. Refusing here rather than leaving it
        to each caller keeps a late registration from clearing ``idle`` after
        ``close()`` computed its ceiling.

        The ``closed`` check and the insertion are one sync step on purpose:
        a caller that awaited in between could register into a gate ``close()``
        had already measured. Once registered, the token is what keeps the
        resource alive — holding it across awaits is the point.
        """
        if self.closed:
            return None
        # Defensive second pass: callers are expected to have normalized
        # already (so their own wait_for matches this deadline), but a stored
        # non-finite deadline would be worse than a mismatch — NaN survives
        # ``max(0.0, nan) -> 0.0`` and would hand a simultaneously live caller
        # nothing but the grace.
        timeout = normalize_timeout(timeout)
        self._next_token += 1
        token = self._next_token
        self._deadlines[token] = self._clock() + timeout
        self.idle.clear()
        return token

    def leave(self, token: int) -> None:
        """Drop a registration; idle again once the last one leaves."""
        self._deadlines.pop(token, None)
        if not self._deadlines:
            self.idle.set()

    def drain_ceiling(self, fallback_timeout: float) -> float:
        """Seconds ``close()`` should wait for the current registrations.

        The longest *remaining* time among live callers (never negative — a
        caller already past its deadline only owes the grace), or
        ``fallback_timeout`` when nothing is registered, plus the grace that
        covers the hop from a deadline firing to ``finally`` releasing.

        The result is finite but not capped: a long finite timeout is a bound
        the operator configured, and cutting the drain below it would close
        the transport inside a call that is still legitimately running — the
        very race this gate exists to prevent. Only a non-finite value is
        substituted (at capture for registrations, here for the fallback).
        """
        if self._deadlines:
            now = self._clock()
            remaining = max(0.0, max(self._deadlines.values()) - now)
        elif math.isfinite(fallback_timeout):
            remaining = fallback_timeout
        else:
            remaining = UNBOUNDED_TIMEOUT_FALLBACK_SECONDS
        return remaining + CLOSE_DRAIN_GRACE_SECONDS


async def drain_or_warn(idle: asyncio.Event, *, timeout: float, what: str) -> bool:
    """Wait for an in-flight gate to clear, bounded; log and proceed on timeout.

    ``close()`` on the LLM helpers waits for registered callers to finish
    before tearing their httpx client down. That wait must have a ceiling:
    each in-flight call carries its own ``wait_for``, so a drain that outlives
    it means a caller never released the gate — and an unbounded wait there
    turns one stuck request into a shutdown that never completes (#867).
    Proceeding is the lesser evil: the caller is already past its own
    deadline, and a hung shutdown blocks the whole process.

    Returns True when the gate cleared, False when the ceiling was hit.
    """
    try:
        await asyncio.wait_for(idle.wait(), timeout=timeout)
        return True
    except TimeoutError:
        logger.warning(
            "%s.close(): in-flight calls did not drain within %.1fs — "
            "closing the client anyway; an in-flight call may see a closed transport",
            what,
            timeout,
        )
        return False


async def await_or_warn(awaitable: Awaitable[Any], *, timeout: float, what: str) -> bool:
    """Await *awaitable* with a ceiling; log and proceed when it is hit.

    The teardown counterpart to :func:`drain_or_warn`, which waits on an event.
    A ``stop()`` that never returns is the difference between a process that
    exits and one that lives for days holding a child (#906), and every step of
    a teardown is on the critical path of process exit.

    Cancelling the wait does not guarantee the work stops — a ``stop()`` that
    awaits a shielded task keeps running — so this bounds *the teardown*, not
    the operation. What guarantees the exit is the watchdog; this makes the
    common wedge recoverable and leaves a named trail when it happens.

    Returns True when it completed, False when the ceiling was hit.
    """
    try:
        await asyncio.wait_for(awaitable, timeout=timeout)
        return True
    except TimeoutError:
        logger.warning(
            "%s did not finish within %.1fs — continuing shutdown without it; "
            "any child it owns is left to the leaked-child sweep",
            what,
            timeout,
        )
        return False
