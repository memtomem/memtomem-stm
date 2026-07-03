"""Unified circuit breaker for STM subsystems."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Three-state circuit breaker: closed → open → half-open.

    - **closed**: all calls pass through.
    - **open**: all calls blocked; becomes eligible to probe (half-open) after
      ``reset_timeout``.
    - **half-open**: the reset window has elapsed; the next call is allowed
      through as a probe — success closes, failure re-opens.

    The half-open transition is **computed, not committed on read** (#600):
    ``is_open`` / ``state`` / ``time_until_reset`` are pure — reading them
    never mutates the breaker, so an observer (e.g. ``stm_proxy_health``)
    cannot perturb or misreport its state. The commit happens on the probe's
    outcome via ``record_success`` / ``record_failure``. (Half-open is not
    single-probe-gated: a failing dependency behind a serialized single MCP
    client is probed once per elapsed window in practice; concurrent
    single-probe enforcement is out of scope — see #600.)
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

    def _effective_state(self) -> str:
        """Effective state without mutating (#600): an open breaker whose
        ``reset_timeout`` has elapsed reads as ``half-open`` (probe eligible)
        but ``self._state`` stays ``open`` until ``record_*`` commits the
        outcome. Only ``record_success`` / ``record_failure`` write state."""
        if self._state == "open" and time.monotonic() - self._opened_at >= self._reset_timeout:
            return "half-open"
        return self._state

    @property
    def is_open(self) -> bool:
        """Whether calls should currently be blocked. PURE — reading this never
        mutates the breaker (#600), so an observer like ``stm_proxy_health`` can
        read it without perturbing state. Returns ``False`` once the reset
        window has elapsed (effectively half-open — the next call probes)."""
        return self._effective_state() == "open"

    @property
    def state(self) -> str:
        """Current effective state: 'closed', 'open', or 'half-open'. PURE —
        computes the open→half-open transition from elapsed time without
        committing it (#600)."""
        return self._effective_state()

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
        from this; at those magnitudes ``now - opened_at`` loses a few low
        bits, so a value just under the reset window can return a tiny
        positive residue rather than a bit-exact figure. Callers must
        tolerate a few ULPs of float residue (the test at
        ``test_time_until_reset_near_boundary`` pins an absolute-tolerance
        check). At or past the window the breaker reads effectively
        half-open and ``time_until_reset`` is ``None``.

        The backing field uses ``0.0`` as a "never opened" sentinel.
        ``time.monotonic()`` is implementation-defined and could in
        principle return ``0.0``, but does not in practice on CPython
        (Linux/macOS/Windows).
        """
        return None if self._opened_at == 0.0 else self._opened_at

    @property
    def time_until_reset(self) -> float | None:
        """Seconds until an open breaker becomes probe-eligible (half-open).
        ``None`` once the window has elapsed (effectively half-open) or closed.
        PURE — uses the effective state, so a read never commits the transition
        (#600)."""
        if self._effective_state() != "open":
            return None
        remaining = self._reset_timeout - (time.monotonic() - self._opened_at)
        return max(0.0, remaining)

    def record_success(self) -> None:
        self._failures = 0
        self._state = "closed"

    def record_failure(self) -> None:
        self._failures += 1
        # Re-open on a failed probe (effectively half-open once the reset window
        # elapsed) or when a closed breaker crosses the failure threshold. Since
        # reads no longer commit the half-open transition (#600), the effective
        # state is recomputed here; a probe that fails in the elapsed window
        # restarts the window with a fresh ``_opened_at``.
        eff = self._effective_state()
        if eff == "half-open" or (self._failures >= self._max_failures and eff == "closed"):
            self._state = "open"
            self._opened_at = time.monotonic()
            logger.warning(
                "CircuitBreaker[%s] opened after %d failures", self._name, self._failures
            )

    # Aliases for backward compatibility
    success = record_success
    failure = record_failure
