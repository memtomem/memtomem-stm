"""Unit tests for ``memtomem_stm.utils.numeric`` coercion helpers."""

from __future__ import annotations

import math

import pytest

from memtomem_stm.utils.numeric import finite_number, safe_float


class TestSafeFloatFinite:
    def test_passes_through_plain_float(self):
        assert safe_float(1.5) == 1.5

    def test_passes_through_int(self):
        assert safe_float(3) == 3.0

    def test_coerces_numeric_string(self):
        assert safe_float("2.5") == 2.5

    def test_coerces_scientific_notation(self):
        assert safe_float("1e-3") == 0.001

    def test_coerces_negative(self):
        assert safe_float("-0.25") == -0.25


class TestSafeFloatFallback:
    def test_malformed_string_returns_default(self):
        assert safe_float("not-a-number", 0.5) == 0.5

    def test_mixed_dots_returns_default(self):
        assert safe_float("1.2.3", 0.5) == 0.5

    def test_none_returns_default(self):
        assert safe_float(None, 0.0) == 0.0

    def test_list_returns_default(self):
        assert safe_float([1.0, 2.0], 0.1) == 0.1

    def test_default_default_is_zero(self):
        assert safe_float("nope") == 0.0


class TestSafeFloatNonFiniteRejection:
    def test_string_nan_rejected_by_default(self):
        assert safe_float("nan", 0.5) == 0.5

    def test_string_inf_rejected_by_default(self):
        assert safe_float("inf", 0.5) == 0.5

    def test_string_neg_inf_rejected_by_default(self):
        assert safe_float("-inf", 0.5) == 0.5

    def test_float_nan_rejected_by_default(self):
        assert safe_float(float("nan"), 0.5) == 0.5

    def test_float_inf_rejected_by_default(self):
        assert safe_float(float("inf"), 0.5) == 0.5

    def test_nonfinite_allowed_when_opted_in(self):
        result = safe_float("nan", 0.5, reject_nonfinite=False)
        assert math.isnan(result)

        result = safe_float("inf", 0.5, reject_nonfinite=False)
        assert math.isinf(result)


class TestSafeFloatOversizedInteger:
    """A JSON integer literal is unbounded; ``float()`` is not (#856)."""

    def test_oversized_int_returns_default(self):
        assert safe_float(10**400, 0.5) == 0.5

    def test_oversized_negative_int_returns_default(self):
        assert safe_float(-(10**400), 0.5) == 0.5

    def test_oversized_int_returns_default_when_nonfinite_allowed(self):
        # `reject_nonfinite=False` opts into inf, not into a crash.
        assert safe_float(10**400, 0.5, reject_nonfinite=False) == 0.5


class TestFiniteNumber:
    @pytest.mark.parametrize("value", [1.5, 3, 0, -0.25, 1e-3])
    def test_real_numbers_pass_through(self, value):
        assert finite_number(value) == float(value)

    @pytest.mark.parametrize(
        ("value", "why"),
        [
            (True, "bool is an int subclass but never a measurement"),
            (False, "bool is an int subclass but never a measurement"),
            ("2.5", "a numeric string is not a number a producer wrote"),
            (None, "absent"),
            ([1.0], "not a scalar"),
            (10**400, "too large for float()"),
            (-(10**400), "too large for float()"),
            (float("nan"), "poisons aggregates and fails strict JSON"),
            (float("inf"), "poisons aggregates and fails strict JSON"),
            (float("-inf"), "poisons aggregates and fails strict JSON"),
        ],
    )
    def test_unusable_values_are_none(self, value, why):
        assert finite_number(value) is None, why

    def test_zero_is_a_value_not_an_absence(self):
        # The whole point of returning None rather than a default.
        assert finite_number(0) == 0.0
        assert finite_number(0) is not None
