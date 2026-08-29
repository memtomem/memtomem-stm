"""Notice a client that went away without closing stdin (#914).

#906 left one branch open. The server's shutdown trigger is stdin EOF, which
arrives when the last write end of the pipe closes. A client that exits having
leaked that write end into a surviving descendant closes nothing: no EOF, no
signal, and the bounded teardown that #910-#913 hardened is never entered. The
process then holds its LTM child for as long as the descendant lives — days, in
the incident.

With no event to wait for, the only evidence left is the process tree: our
parent is the client, and when it dies we are reparented. So this watcher
records ``getppid()`` at startup and polls for a change.

That is an *inference*, not an event, and it can be wrong in a way the other
three backstops cannot. A launcher that ``exec``-replaces itself keeps its pid
and is safe; a short-lived wrapper shell that spawns us and exits reparents us
while the real client, holding the pipe, is very much alive. Shutting down
there costs a working session, which is worse than the leak this prevents — the
same asymmetry the sweep in #910 is built around. Hence:

* the comparison is against the *recorded* pid, never ``== 1``. A detached
  daemon is legitimately reparented to init at birth, and on macOS the new
  parent is launchd, pid 1, either way;
* a change must survive two consecutive polls, so a pid observed mid-reparent
  is not evidence;
* and the change alone is not enough. What separates the two shapes is not the
  pipe — in both of them somebody still holds the write end, so there is no
  hangup to detect — but the traffic: a live client behind a wrapper shell
  keeps speaking MCP, while a descendant that merely inherited a descriptor
  never does. So the shutdown also requires the connection to have been silent
  for a grace period, which callers stamp through :meth:`note_activity`.

The veto is a deferral, not a cancel: polling continues, and a parent that
really is gone shuts us down once the traffic stops. Detection latency of
minutes is free here, because the failure it prevents is measured in days.

Off unless configured, and POSIX-only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Polls a suspected change has to survive before it counts. Two is the smallest
# number that rejects a single bad sample, and the cost of the extra poll is one
# interval of latency against a failure mode measured in days — so this is not
# worth a knob.
_CONFIRM_POLLS = 2


class ParentLivenessWatcher:
    """Polls for the parent going away and asks for shutdown when it has.

    *on_parent_gone* is the shutdown to start — :meth:`ShutdownSignals.trigger`
    in the server — called at most once, with a reason for the log. *getppid*
    and *clock* are injection points for tests; nothing else should pass them.

    Construct this synchronously at the point the parent is still known to be
    the client: the baseline is taken in ``__init__``, not when the task first
    runs.
    """

    def __init__(
        self,
        *,
        poll_seconds: float,
        grace_seconds: float,
        on_parent_gone: Callable[[str], None],
        getppid: Callable[[], int] = os.getppid,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._poll_seconds = poll_seconds
        self._grace_seconds = grace_seconds
        self._on_parent_gone = on_parent_gone
        self._getppid = getppid
        self._clock = clock
        self._stop_event = asyncio.Event()
        self._last_activity = clock()
        # Sampled here, not in ``run()``. The task does not start until the
        # lifespan next awaits, and its startup awaits for as long as the
        # upstream connections take — a client that exits inside that window
        # would be recorded as the baseline, and every later poll would agree
        # with it. The watcher would then be permanently blind to precisely the
        # orphaning it exists to catch.
        self._recorded_ppid = getppid()
        self._in_flight = 0

    def note_activity(self) -> None:
        """Record that the client just spoke to us.

        Called from the event loop for every inbound frame, so it stays a single
        attribute write — the grace window is minutes wide and does not need
        better resolution than the poll interval.
        """
        self._last_activity = self._clock()

    @contextmanager
    def serving(self) -> Iterator[None]:
        """Hold the veto open for as long as a frame is being served.

        A stamp records that a frame *arrived*, which leaves the clock standing
        still through the handling of it: a single call that outlasts the grace
        would look like silence while the client sits there waiting for its
        answer. So a request in flight vetoes on its own — the strongest
        evidence of a live client there is — and the completion re-stamps, so
        the grace measures from when the client last heard from us.

        This is the daemon idle-watch's rule (``_active_requests == 0 and
        idle >= timeout``, ``daemon/server.py``) applied to the same question.
        It means a handler that never returns keeps the process alive even after
        the client is gone; that is the conservative direction, and #911's
        teardown watchdog still bounds the shutdown once one starts.
        """
        self._in_flight += 1
        self.note_activity()
        try:
            yield
        finally:
            self._in_flight -= 1
            self.note_activity()

    def stop(self) -> None:
        """End the loop at its next wake-up, for a teardown we did not start."""
        self._stop_event.set()

    async def run(self) -> None:
        if sys.platform == "win32":
            # Reparenting semantics differ and pids are reused; the inference
            # this rests on does not hold there.
            return
        if self._poll_seconds <= 0:
            return

        recorded = self._recorded_ppid
        logger.info(
            "Parent-liveness backstop watching ppid %d (poll %gs, grace %gs) — #914",
            recorded,
            self._poll_seconds,
            self._grace_seconds,
        )

        confirmations = 0
        vetoed = False
        while True:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_seconds)
            except asyncio.TimeoutError:
                pass
            # Deliberately no `except CancelledError` anywhere in this loop: the
            # shutdown this watcher asks for cancels the loop's tasks, this one
            # included, and swallowing that would leave the task alive through a
            # teardown it started itself.
            if self._stop_event.is_set():
                return

            current = self._getppid()
            if current == recorded:
                if confirmations:
                    logger.info(
                        "Parent pid is %d again — the earlier change was a bad sample.",
                        recorded,
                    )
                confirmations = 0
                vetoed = False
                continue

            confirmations += 1
            if confirmations < _CONFIRM_POLLS:
                logger.warning(
                    "Parent changed: recorded ppid %d, now %d — confirming at the next "
                    "poll before acting (#914).",
                    recorded,
                    current,
                )
                continue

            idle = self._clock() - self._last_activity
            serving = self._in_flight > 0
            if serving or (self._grace_seconds > 0 and idle < self._grace_seconds):
                if not vetoed:
                    logger.info(
                        "Parent changed (ppid %d -> %d) but the client is still here "
                        "(%s) — a live client behind a wrapper launcher looks exactly "
                        "like this, so not shutting down yet (#914).",
                        recorded,
                        current,
                        f"{self._in_flight} request(s) in flight"
                        if serving
                        else f"a frame {idle:.0f}s ago, inside the {self._grace_seconds:.0f}s grace",
                    )
                    vetoed = True
                continue

            logger.warning(
                "Parent gone: recorded ppid %d, now %d, no request for %.0fs "
                "(grace %.0fs) — shutting down (#914).",
                recorded,
                current,
                idle,
                self._grace_seconds,
            )
            # Nothing may be awaited after this: the shutdown cancels this task.
            self._on_parent_gone(f"Parent process gone (ppid {recorded} -> {current})")
            return
