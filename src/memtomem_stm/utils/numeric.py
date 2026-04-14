"""Numeric parsing helpers for untrusted external input."""

from __future__ import annotations

import math


def safe_float(value: object, default: float = 0.0, *, reject_nonfinite: bool = True) -> float:
    """Coerce ``value`` to a finite float, falling back to ``default`` on failure.

    Defends against malformed LLM output and untrusted external JSON. Unlike
    raw ``float()``:

    - Catches ``TypeError``/``ValueError`` from non-convertible inputs.
    - Rejects ``nan``/``inf``/``-inf`` when ``reject_nonfinite=True`` (default),
      which plain ``float()`` accepts silently and propagates through
      comparison/sort logic as undefined behavior.
    """
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if reject_nonfinite and not math.isfinite(result):
        return default
    return result
