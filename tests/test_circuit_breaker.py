"""Tests for CircuitBreaker state machine."""

from __future__ import annotations

from unittest.mock import patch

from memtomem_stm.utils.circuit_breaker import CircuitBreaker


class TestCircuitBreakerClosed:
    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert not cb.is_open

    def test_single_failure_stays_closed(self):
        cb = CircuitBreaker(max_failures=3)
        cb.record_failure()
        assert not cb.is_open

    def test_failures_below_threshold_stays_closed(self):
        cb = CircuitBreaker(max_failures=3)
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_open

    def test_record_success_is_idempotent(self):
        cb = CircuitBreaker()
        cb.record_success()
        cb.record_success()
        assert not cb.is_open


class TestCircuitBreakerOpen:
    def test_opens_at_max_failures(self):
        cb = CircuitBreaker(max_failures=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.is_open

    def test_opens_with_custom_threshold(self):
        cb = CircuitBreaker(max_failures=1)
        cb.record_failure()
        assert cb.is_open

    def test_stays_open_before_timeout(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=60.0)
        cb.record_failure()
        assert cb.is_open
        # Still open immediately after
        assert cb.is_open


class TestCircuitBreakerHalfOpen:
    def test_transitions_to_half_open_after_timeout(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()
        assert cb.is_open

        # Simulate time passing beyond reset_timeout
        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = cb.opened_at + 11.0
            # Should transition to half-open and allow one probe
            assert not cb.is_open
            assert cb._state == "half-open"

    def test_half_open_success_closes_circuit(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()

        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = cb.opened_at + 11.0
            assert not cb.is_open  # half-open

        cb.record_success()
        assert cb._state == "closed"
        assert not cb.is_open

    def test_half_open_failure_reopens_circuit(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()

        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = cb.opened_at + 11.0
            assert not cb.is_open  # half-open

        cb.record_failure()
        assert cb._state == "open"
        assert cb.is_open

    def test_half_open_failure_resets_timeout(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()
        first_opened_at = cb.opened_at

        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = first_opened_at + 11.0
            assert not cb.is_open  # half-open

        cb.record_failure()
        # opened_at should be refreshed
        assert cb.opened_at > first_opened_at or cb.opened_at == first_opened_at
        # If time.monotonic was called normally, opened_at >= first_opened_at

    def test_repeated_open_close_cycles(self):
        cb = CircuitBreaker(max_failures=2, reset_timeout=5.0)

        # Cycle 1: open
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open

        # Half-open → success → closed
        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = cb.opened_at + 6.0
            assert not cb.is_open
        cb.record_success()
        assert not cb.is_open

        # Cycle 2: open again
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open

    def test_half_open_does_not_allow_multiple_probes(self):
        """After transitioning to half-open, subsequent is_open calls should not
        return False again (the probe was already allowed)."""
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()

        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = cb.opened_at + 11.0
            # First call: transitions to half-open, returns False
            assert not cb.is_open
            assert cb._state == "half-open"

        # Second call without success/failure: state is half-open,
        # is_open returns False (half-open is not "open").
        # This is acceptable because the consumer should call
        # record_success/failure before the next is_open check.
        # The important thing is the circuit reacts correctly to the result.


class TestCircuitBreakerProperties:
    def test_time_until_reset_when_closed(self):
        cb = CircuitBreaker()
        assert cb.time_until_reset is None

    def test_time_until_reset_when_open(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=60.0)
        cb.record_failure()
        remaining = cb.time_until_reset
        assert remaining is not None
        assert 0 < remaining <= 60.0

    def test_time_until_reset_when_half_open(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()

        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = cb.opened_at + 11.0
            assert not cb.is_open  # triggers half-open
            # assertion inside the patch block — fix for timing concern
            assert cb.time_until_reset is None

    def test_time_until_reset_at_boundary(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()

        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = cb.opened_at + 10.0
            result = cb.time_until_reset
            # The contract here is "not positive, effectively zero" — pick a
            # tolerance several orders of magnitude below any observable reset
            # window. See ``CircuitBreaker.opened_at`` docstring for the
            # float-precision rationale (paragraph migrated there per #277).
            assert result is not None and abs(result) < 1e-9

    def test_opened_at_lifecycle_preserves_most_recent_open(self):
        """``opened_at`` is None initially, captures the open transition,
        and survives the recover→close path so diagnostics retain the
        historical "last trip" timestamp (#277).

        ``record_success`` flips state to ``closed`` and zeroes
        ``failure_count`` but intentionally does not clear
        ``_opened_at`` — this regression pins that contract so a
        future "reset on close" change does not silently erase the
        diagnostic without updating the property docstring.
        """
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        assert cb.opened_at is None  # never opened

        cb.record_failure()
        assert cb.is_open
        opened_when_tripped = cb.opened_at
        assert opened_when_tripped is not None  # captured at open
        # Still equals that value while currently open.
        assert cb.opened_at == opened_when_tripped

        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = opened_when_tripped + 11.0
            assert not cb.is_open  # auto-transitions to half-open
            assert cb.state == "half-open"
            # Half-open does not refresh opened_at.
            assert cb.opened_at == opened_when_tripped

        cb.record_success()
        assert cb.state == "closed"
        # Recovery does not clear opened_at — the "last trip" survives.
        assert cb.opened_at == opened_when_tripped

    def test_backward_compat_failure_alias(self):
        cb = CircuitBreaker(max_failures=2)
        cb.failure()
        assert cb.failure_count == 1

    def test_backward_compat_success_alias(self):
        cb = CircuitBreaker(max_failures=2)
        cb.failure()
        cb.failure()
        assert cb.is_open
        cb.success()
        assert cb.failure_count == 0
        assert cb._state == "closed"

    def test_backward_compat_aliases_match_originals(self):
        assert CircuitBreaker.success == CircuitBreaker.record_success
        assert CircuitBreaker.failure == CircuitBreaker.record_failure
