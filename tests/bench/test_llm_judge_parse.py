"""Unit tests for the LLM-judge lenient JSON parser.

No API key required — these pin ``_loads_lenient`` against the response shapes
that a bare ``json.loads`` rejected, the trailing-prose case being the one that
errored 2 of 6 scenarios on real Anthropic Haiku runs (the Messages API has no
JSON-object response mode, so the model appends a sentence after the object).
Runs in the default ``test`` CI job (unmarked).
"""

from __future__ import annotations

import json

import pytest

from bench.llm_judge import _loads_lenient


def test_plain_object():
    assert _loads_lenient('{"overall": 8, "ok": true}') == {"overall": 8, "ok": True}


def test_strips_code_fence():
    assert _loads_lenient('```json\n{"overall": 7}\n```') == {"overall": 7}


def test_ignores_trailing_prose():
    # The Haiku failure mode: a valid object followed by a trailing sentence.
    raw = '{"overall": 6, "note": "x"}\n\nThis compression preserved the key facts.'
    assert _loads_lenient(raw) == {"overall": 6, "note": "x"}


def test_skips_leading_prose():
    assert _loads_lenient('Here is my assessment:\n{"overall": 9}') == {"overall": 9}


def test_fence_then_trailing_prose():
    # Close fence not stripped (text no longer ends with ```), but raw_decode
    # stops at the end of the object regardless.
    assert _loads_lenient('```\n{"overall": 5}\n```\nDone.') == {"overall": 5}


def test_nested_object_survives_trailing_prose():
    raw = '{"factual_completeness": {"score": 7}, "overall": 7} trailing'
    assert _loads_lenient(raw) == {"factual_completeness": {"score": 7}, "overall": 7}


def test_no_object_raises():
    with pytest.raises(json.JSONDecodeError):
        _loads_lenient("no json here at all")
