"""Unified circuit breaker for STM subsystems."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Three-state circuit breaker: closed → open → half-open.

    - **closed**: all calls pass through.
    - **open**: all calls blocked; transitions to half-open after ``reset_timeout``.
    - **half-open**: one probe call allowed; success closes, failure re-opens.
    """

    def __init__(
        self,
        max_failures: int = 3,
        reset_timeout: float = 60.0,
        name: str = "",
    ) -> None:
        self._max_failures = max_failures
        self._reset_timeout = reset_timeout
        self._name = name
        self._failures = 0
        self._state = "closed"
        self._opened_at = 0.0

    @property
    def is_open(self) -> bool:
        if self._state == "closed":
            return False
        if self._state == "open" and time.monotonic() - self._opened_at >= self._reset_timeout:
            self._state = "half-open"
            return False  # allow one probe
        return self._state == "open"

    @property
    def state(self) -> str:
        """Current state: 'closed', 'open', or 'half-open'."""
        # Trigger half-open transition if timeout elapsed
        if self._state == "open" and time.monotonic() - self._opened_at >= self._reset_timeout:
            self._state = "half-open"
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failures

    @property
    def opened_at(self) -> float | None:
        """Monotonic timestamp of the most recent open transition,
        or None if the breaker has never opened.

        After the breaker has opened at least once, this returns the
        timestamp of the *most recent* open transition — even if the
        breaker is currently closed. ``record_success`` flips state
        back to ``closed`` and zeroes ``failure_count`` but does not
        clear this timestamp, so the historical "last trip" is
        preserved for diagnostics. Use ``state`` to distinguish
        "currently open" from "previously opened, now recovered".

        The value is a real ``time.monotonic()`` reading — typically in
        the ~1e5 to 1e9 second range on long-running processes.
        ``time_until_reset`` computes ``reset_timeout - (now - opened_at)``
        from this; ``(opened_at + 10.0) - opened_at`` loses a few low
        bits at those magnitudes and the result is ``~1e-14`` rather
        than bit-exact ``0.0``. Callers subtracting two
        ``opened_at``-derived values must tolerate a few ULPs of float
        residue (the test at ``test_time_until_reset_at_boundary``
        pins an absolute-tolerance check).

        The backing field uses ``0.0`` as a "never opened" sentinel.
        ``time.monotonic()`` is implementation-defined and could in
        principle return ``0.0``, but does not in practice on CPython
        (Linux/macOS/Windows).
        """
        return None if self._opened_at == 0.0 else self._opened_at

    @property
    def time_until_reset(self) -> float | None:
        """Seconds until open breaker transitions to half-open. None if not open."""
        if self._state != "open":
            return None
        remaining = self._reset_timeout - (time.monotonic() - self._opened_at)
        return max(0.0, remaining)

    def record_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    def record_failure(self) -> None:
        self._failures += 1
        if self._state == "half-open" or (
            self._failures >= self._max_failures and self._state == "closed"
        ):
            self._state = "open"
            self._opened_at = time.monotonic()
            logger.warning(
                "CircuitBreaker[%s] opened after %d failures", self._name, self._failures
            )

    # Aliases for backward compatibility
    success = record_success
    failure = record_failure
