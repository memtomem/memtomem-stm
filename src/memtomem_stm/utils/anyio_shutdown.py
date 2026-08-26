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


class InFlightGate:
    """Registration gate shared by the LLM helpers' ``close()`` drains.

    Tracks how many callers are mid-request and, crucially, the largest
    timeout any of them **captured when it started** — not whatever the
    (mutable) config says later. ``close()`` deriving its ceiling from a
    re-read of the config can undercut a live call: another task lowering
    ``llm_timeout_seconds`` between the call's start and the close would
    close the transport while that call is still inside its own deadline.
    """

    def __init__(self) -> None:
        self.in_flight: int = 0
        self.idle: asyncio.Event = asyncio.Event()
        self.idle.set()
        self.closed: bool = False
        # Max timeout captured by the callers currently registered; 0.0 when
        # idle, so a close() with nothing in flight falls back to the config.
        self._captured_ceiling: float = 0.0

    def enter(self, timeout: float) -> None:
        """Register an in-flight caller that captured *timeout*.

        Sync on purpose: callers must not ``await`` between their ``closed``
        check and this claim, or ``close()`` can tear the client down in
        between.
        """
        self.in_flight += 1
        self._captured_ceiling = max(self._captured_ceiling, timeout)
        self.idle.clear()

    def leave(self) -> None:
        self.in_flight -= 1
        if self.in_flight <= 0:
            self.in_flight = 0
            self._captured_ceiling = 0.0
            self.idle.set()

    def drain_ceiling(self, fallback_timeout: float) -> float:
        """Seconds ``close()`` should wait, from the captured deadlines."""
        return max(self._captured_ceiling, fallback_timeout) + CLOSE_DRAIN_GRACE_SECONDS


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
