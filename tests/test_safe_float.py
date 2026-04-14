"""Unit tests for ``memtomem_stm.utils.numeric.safe_float``."""

from __future__ import annotations

import math

from memtomem_stm.utils.numeric import safe_float


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
