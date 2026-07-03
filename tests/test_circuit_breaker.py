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
            # Effectively half-open (probe eligible) — but reading it does NOT
            # commit the transition (#600): the public state reads half-open
            # while the raw internal state stays "open" until record_* runs.
            assert not cb.is_open
            assert cb.state == "half-open"
            assert cb._state == "open"

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

        # The probe fails WITHIN the elapsed window (as it does in production:
        # the gate reads is_open → admits → the call fails → record_failure, all
        # at roughly the same, post-timeout instant). Since reads no longer
        # commit the transition (#600), record_failure must see the elapsed
        # time itself to re-open — so it runs inside the patched clock.
        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = cb.opened_at + 11.0
            assert not cb.is_open  # effectively half-open
            cb.record_failure()
            assert cb._state == "open"
            assert cb.is_open  # re-opened with a fresh window

    def test_half_open_failure_resets_timeout(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()
        first_opened_at = cb.opened_at

        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = first_opened_at + 11.0
            assert not cb.is_open  # effectively half-open
            cb.record_failure()  # fails within the window → re-open
            # opened_at refreshed to the (mocked) failure time, strictly later.
            assert cb.opened_at == first_opened_at + 11.0
            assert cb.opened_at > first_opened_at

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

    def test_reads_are_pure_and_idempotent(self):
        """#600 — repeated is_open / state / time_until_reset reads in the
        elapsed (effectively half-open) window never mutate the breaker: the
        raw internal state stays "open" no matter how many times an observer
        reads it. This is what lets stm_proxy_health read the breaker without
        flipping it."""
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()

        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = cb.opened_at + 11.0
            for _ in range(5):
                assert not cb.is_open
                assert cb.state == "half-open"
                assert cb.time_until_reset is None
                # The raw state is never committed by a read.
                assert cb._state == "open"


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

    def test_time_until_reset_near_boundary(self):
        cb = CircuitBreaker(max_failures=1, reset_timeout=10.0)
        cb.record_failure()
        opened = cb.opened_at

        # Just BEFORE the window elapses: still open, a tiny positive remaining
        # (the float-residue tolerance — see the ``opened_at`` docstring).
        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = opened + 9.999
            result = cb.time_until_reset
            assert result is not None and 0.0 <= result < 0.01

        # Past the window: effectively half-open → None (no time until reset,
        # the reset has completed). Pure read — does not commit the transition.
        # (Use a value clearly past reset_timeout so the float residue at
        # ~1e5-1e9 magnitudes can't land it on the wrong side of the boundary.)
        with patch("memtomem_stm.utils.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = opened + 10.001
            assert cb.time_until_reset is None
            assert cb._state == "open"

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
