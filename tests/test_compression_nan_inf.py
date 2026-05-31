"""Regression net: compression tiers must never emit the JSON-invalid bareword
tokens ``NaN`` / ``Infinity`` / ``-Infinity``.

Python's ``json.loads`` accepts those extension tokens (turning them into
``float('nan')`` / ``float('inf')``) and ``json.dumps`` re-emits them, so any
tier that parsed upstream text carrying them and re-serialized produced output
that RFC 8259 parsers reject. ``_mm_json_loads`` sanitizes non-finite floats to
``null`` at parse time, the single ingest point every JSON tier flows through.

Scope: this guards the COMPRESSION path (parse + re-serialize). Payloads here are
deliberately larger than the budget so compression actually runs — when upstream
content already fits the budget STM passes it through verbatim and does not parse
it (STM is a compression proxy, not a JSON-repair proxy). Tier coverage matches
each tier's JSON contract: FieldExtract / SchemaPruning / Selective always parse
and re-emit JSON; TruncateCompressor only JSON-processes config-like dicts (its
``_json_key_truncate`` path), handling arrays / prose as plain text.

The assertions use ``json.loads(..., parse_constant=...)`` because PLAIN
``json.loads`` silently accepts ``NaN``/``Infinity`` and would let the bug pass.
"""

from __future__ import annotations

import json

import pytest

from memtomem_stm.proxy.compression import (
    FieldExtractCompressor,
    SchemaPruningCompressor,
    SelectiveCompressor,
    TruncateCompressor,
    _sanitize_nonfinite,
)


def _strict_loads(s: str) -> object:
    """``json.loads`` that REJECTS NaN/Infinity tokens (plain loads accepts them)."""

    def _reject(token: str) -> object:
        raise ValueError(f"non-finite JSON token: {token}")

    return json.loads(s, parse_constant=_reject)


def test_strict_loads_helper_rejects_naninf() -> None:
    """The helper itself must reject the tokens, else every test below is a no-op."""
    with pytest.raises(ValueError):
        _strict_loads('{"x": NaN}')
    with pytest.raises(ValueError):
        _strict_loads('{"x": Infinity}')
    assert _strict_loads('{"x": 1}') == {"x": 1}


# Budget-exceeding upstream payloads (so compression runs, not passthrough) whose
# values carry NaN / Infinity / -Infinity at varying nesting depths.
def _naninf_list() -> str:
    return json.dumps(
        [
            {"loss": float("nan"), "score": float("inf"), "id": i, "note": "x" * 40}
            for i in range(20)
        ]
    )


def _naninf_config_dict() -> str:
    """Config-like dict (every top-level value is a dict) — the shape every tier,
    including TruncateCompressor, routes through its JSON path."""
    return json.dumps(
        {
            f"svc{i}": {"loss": float("nan"), "score": float("inf"), "id": i, "note": "x" * 40}
            for i in range(20)
        }
    )


_JSON_TIERS = {
    "field_extract": FieldExtractCompressor,
    "schema_pruning": SchemaPruningCompressor,
}


@pytest.mark.parametrize("tier_name", sorted(_JSON_TIERS))
@pytest.mark.parametrize("payload_name", ["list", "config_dict"])
@pytest.mark.parametrize("budget", [400, 200, 80, 30])
def test_json_tier_output_is_strict_valid_json(
    tier_name: str, payload_name: str, budget: int
) -> None:
    """FieldExtract / SchemaPruning parse any JSON and re-emit JSON, so their
    compressed output must be strict-valid with no surviving NaN/Inf token."""
    payload = _naninf_list() if payload_name == "list" else _naninf_config_dict()
    assert len(payload) > budget  # ensure compression runs, not passthrough
    out = _JSON_TIERS[tier_name]().compress(payload, max_chars=budget)
    _strict_loads(out)  # raises if a NaN/Infinity token survived
    assert "NaN" not in out and "Infinity" not in out
    assert len(out) <= max(2, budget)


@pytest.mark.parametrize("budget", [400, 200, 80, 30])
def test_truncate_config_dict_is_strict_valid_json(budget: int) -> None:
    """TruncateCompressor JSON-processes config-like dicts via _json_key_truncate;
    that path must emit strict-valid JSON free of NaN/Inf tokens."""
    payload = _naninf_config_dict()
    assert len(payload) > budget
    out = TruncateCompressor().compress(payload, max_chars=budget)
    _strict_loads(out)
    assert "NaN" not in out and "Infinity" not in out
    assert len(out) <= max(2, budget)


@pytest.mark.parametrize("budget", [200, 120, 60])
def test_selective_toc_is_strict_valid_json(budget: int) -> None:
    """SelectiveCompressor's TOC re-dumps parsed values as entry previews, so a
    non-finite value must not leak into the (always-JSON) TOC envelope."""
    payload = _naninf_config_dict()
    assert len(payload) > budget
    out = SelectiveCompressor().compress(payload, max_chars=budget)
    _strict_loads(out)
    assert "NaN" not in out and "Infinity" not in out


# ── Direct unit coverage of the sanitizer ────────────────────────────────────


def test_sanitize_maps_nonfinite_to_none() -> None:
    assert _sanitize_nonfinite(float("nan")) is None
    assert _sanitize_nonfinite(float("inf")) is None
    assert _sanitize_nonfinite(float("-inf")) is None


def test_sanitize_leaves_finite_values_untouched() -> None:
    assert _sanitize_nonfinite(3.14) == 3.14
    assert _sanitize_nonfinite(0) == 0
    assert _sanitize_nonfinite(True) is True  # bool is not float
    assert _sanitize_nonfinite(False) is False
    assert _sanitize_nonfinite("NaN") == "NaN"  # a string, not a float
    assert _sanitize_nonfinite(None) is None


def test_sanitize_recurses_through_containers() -> None:
    src = {"a": [1, float("inf"), {"b": float("nan")}], "c": (float("-inf"), 2)}
    out = _sanitize_nonfinite(src)
    assert out == {"a": [1, None, {"b": None}], "c": [None, 2]}
    _strict_loads(json.dumps(out))  # the sanitized form is strict-valid JSON


def test_sanitize_clean_input_returns_same_identity() -> None:
    """No non-finite float -> the original object is returned (no copy), so the
    common case adds no allocation on the hot serialization path."""
    clean = {"a": [1, 2, {"b": "x"}], "c": 3.5}
    assert _sanitize_nonfinite(clean) is clean
    inner = [1, 2, 3]
    assert _sanitize_nonfinite(inner) is inner


def test_mm_json_loads_sanitizes_at_parse() -> None:
    """The parse-time wrapper turns the extension tokens into null on the way in,
    so every tier reading through it sees finite-or-null values only."""
    from memtomem_stm.proxy.compression import _mm_json_loads

    parsed = _mm_json_loads('{"a": NaN, "b": [Infinity, -Infinity, 1]}')
    assert parsed == {"a": None, "b": [None, None, 1]}
