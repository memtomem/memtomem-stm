"""Numeric parsing helpers for untrusted external input."""

from __future__ import annotations

import math


def safe_float(value: object, default: float = 0.0, *, reject_nonfinite: bool = True) -> float:
    """Coerce ``value`` to a finite float, falling back to ``default`` on failure.

    Defends against malformed LLM output and untrusted external JSON. Unlike
    raw ``float()``:

    - Catches ``TypeError``/``ValueError`` from non-convertible inputs, and
      ``OverflowError`` from an integer too large for a float — JSON integer
      literals are unbounded, so an upstream can send one no ``float`` can hold.
    - Rejects ``nan``/``inf``/``-inf`` when ``reject_nonfinite=True`` (default),
      which plain ``float()`` accepts silently and propagates through
      comparison/sort logic as undefined behavior.

    Coerces permissively: a numeric string or a ``bool`` converts. Use
    :func:`finite_number` where the caller has already decided that only a real
    number is acceptable and needs to tell "no usable value" from a default.
    """
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    if reject_nonfinite and not math.isfinite(result):
        return default
    return result


def finite_number(value: object) -> float | None:
    """``value`` as a finite float, or ``None`` when it is not a usable number.

    Total by construction, and stricter than :func:`safe_float` in both
    directions. It refuses what a type check alone would admit — ``bool`` is an
    ``int`` subclass but never a measurement, an integer literal can be too
    large for ``float()`` to represent (``OverflowError``), and ``NaN``/
    ``Infinity`` survive a type check only to poison every aggregate they reach
    and to fail strict JSON serialization on the way out. It also refuses what
    ``float()`` would happily coerce: a numeric string is not a number that a
    producer wrote as one.

    Returning ``None`` rather than a default keeps "there is no usable value"
    distinguishable from "the value is zero", which matters wherever the
    absence is itself reported — a sample the caller drops instead of biasing.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None
