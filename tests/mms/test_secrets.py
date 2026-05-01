"""Tests for ``mms.secrets`` — RFC §7.2.1 classification matrix."""

from __future__ import annotations

import pytest

from memtomem_stm.mms.secrets import (
    REDACTED_DISPLAY,
    Classification,
    Kind,
    classify_env,
    redact_for_plan,
)


# ---------------------------------------------------------------------------
# Key pattern matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected_pattern"),
    [
        # Exact uppercase matches
        ("API_KEY", "API_KEY"),
        ("TOKEN", "TOKEN"),
        ("SECRET", "SECRET"),
        ("PASSWORD", "PASSWORD"),
        ("PASS", "PASS"),
        ("AUTH", "AUTH"),
        ("CREDENTIAL", "CREDENTIAL"),
        ("KEY", "KEY"),
        # Case-insensitive
        ("api_key", "API_KEY"),
        ("Token", "TOKEN"),
        # Substring matches
        ("GITHUB_TOKEN", "TOKEN"),
        ("OPENAI_API_KEY", "API_KEY"),  # API_KEY is more specific than KEY
        ("USER_PASSWORD", "PASSWORD"),
        ("ANTHROPIC_AUTH_HEADER", "AUTH"),
        ("MY_CREDENTIAL_FILE", "CREDENTIAL"),
        # KEY substring (least specific) — only matches when nothing more specific does
        ("AWS_SIGNING_KEY", "KEY"),
    ],
)
def test_key_pattern_classified_secret(key, expected_pattern):
    result = classify_env({key: "x"})
    classification = result[key]
    assert classification.is_secret is True
    assert classification.kind is Kind.KEY_PATTERN
    assert f"*{expected_pattern}*" in classification.reason


def test_key_pattern_short_value_still_secret():
    """RFC explicit edge: API_KEY=test is still secret-classified."""
    result = classify_env({"API_KEY": "test"})
    assert result["API_KEY"].is_secret is True
    assert result["API_KEY"].kind is Kind.KEY_PATTERN


def test_key_pattern_takes_precedence_over_heuristic():
    """When both signals match, kind is KEY_PATTERN (more specific)."""
    long_hex_value = "deadbeef" * 8  # 64 chars hex
    result = classify_env({"API_KEY": long_hex_value})
    assert result["API_KEY"].kind is Kind.KEY_PATTERN


# ---------------------------------------------------------------------------
# Value heuristic matrix
# ---------------------------------------------------------------------------


def test_long_hex_value_no_pattern_classified_secret():
    """Pattern-free key + 32+ char hex → heuristic hit."""
    value = "0123456789abcdef" * 2  # 32 chars
    result = classify_env({"OPAQUE": value})
    assert result["OPAQUE"].is_secret is True
    assert result["OPAQUE"].kind is Kind.VALUE_HEURISTIC
    assert "32+ chars hex" in result["OPAQUE"].reason


def test_long_base64_value_no_pattern_classified_secret():
    value = "aGVsbG8gd29ybGQgdGhpcyBpcyA0OCBjaGFycw=="  # base64-ish, 40 chars
    result = classify_env({"OPAQUE": value})
    assert result["OPAQUE"].is_secret is True
    assert result["OPAQUE"].kind is Kind.VALUE_HEURISTIC
    assert "base64" in result["OPAQUE"].reason


def test_short_opaque_value_not_secret():
    """Below the 32-char floor — no false positive."""
    result = classify_env({"OPAQUE": "deadbeef"})
    assert result["OPAQUE"].is_secret is False
    assert result["OPAQUE"].kind is Kind.NON_SECRET


def test_long_value_with_whitespace_not_heuristic_match():
    """Real opaque tokens never contain spaces."""
    result = classify_env({"NOTE": "this is a long human-readable note over 32 chars"})
    assert result["NOTE"].is_secret is False


def test_long_plain_text_not_heuristic_match():
    """Long English text isn't hex or base64-charset."""
    result = classify_env({"NOTE": "the quick brown fox jumps over the lazy dog!"})
    assert result["NOTE"].is_secret is False


def test_exactly_at_min_length_classified():
    """Boundary: exactly 32 chars hex → secret."""
    value = "0" * 32
    result = classify_env({"OPAQUE": value})
    assert result["OPAQUE"].is_secret is True


def test_below_min_length_not_classified():
    value = "0" * 31
    result = classify_env({"OPAQUE": value})
    assert result["OPAQUE"].is_secret is False


# ---------------------------------------------------------------------------
# Non-secret cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("BROWSER", "chromium"),
        ("PORT", "8080"),
        ("PYTHONPATH", "/usr/lib/python3.12"),
        ("LOG_LEVEL", "info"),
        ("HOSTNAME", "example.com"),
        ("NODE_ENV", "production"),
    ],
)
def test_non_secret_examples(key, value):
    result = classify_env({key: value})
    assert result[key].is_secret is False
    assert result[key].kind is Kind.NON_SECRET
    assert result[key].reason == ""


# ---------------------------------------------------------------------------
# classify_env preserves order, handles empty input
# ---------------------------------------------------------------------------


def test_classify_empty_env():
    assert classify_env({}) == {}


def test_classify_preserves_insertion_order():
    env = {"BROWSER": "chromium", "API_KEY": "x", "PORT": "8080"}
    result = classify_env(env)
    assert list(result.keys()) == ["BROWSER", "API_KEY", "PORT"]


# ---------------------------------------------------------------------------
# redact_for_plan
# ---------------------------------------------------------------------------


def test_redact_replaces_secrets_keeps_non_secrets():
    env = {"BROWSER": "chromium", "API_KEY": "ghp_abcdef", "PORT": "8080"}
    classification = classify_env(env)
    redacted = redact_for_plan(env, classification)
    assert redacted["BROWSER"] == "chromium"
    assert redacted["API_KEY"] == REDACTED_DISPLAY
    assert redacted["PORT"] == "8080"


def test_redact_handles_heuristic_secret():
    env = {"OPAQUE": "0" * 40}
    classification = classify_env(env)
    redacted = redact_for_plan(env, classification)
    assert redacted["OPAQUE"] == REDACTED_DISPLAY


def test_redact_passthrough_when_classification_overridden():
    """Caller can pass a classification dict that re-marks everything as
    non-secret to implement ``--show-imported``."""
    env = {"API_KEY": "ghp_x"}
    classification = {"API_KEY": Classification(is_secret=False, kind=Kind.NON_SECRET, reason="")}
    redacted = redact_for_plan(env, classification)
    assert redacted["API_KEY"] == "ghp_x"
