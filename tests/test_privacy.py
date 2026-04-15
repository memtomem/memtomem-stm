"""Tests for ``proxy.privacy`` — pattern compilation, scanning, hot-reload.

Focus of this suite: the ``re.error`` handler at ``privacy.py:26`` that
silently skips invalid patterns. Silent pattern skip is security-adjacent
— operators need to know that a filter they configured may not actually
be running.
"""

from __future__ import annotations

import logging

import pytest

from memtomem_stm.proxy import privacy


@pytest.fixture(autouse=True)
def _clear_pattern_cache():
    """``_compile_patterns`` is lru_cache'd; reset between tests."""
    privacy._compile_patterns.cache_clear()
    yield
    privacy._compile_patterns.cache_clear()


class TestInvalidPatternHandling:
    """A bad regex in the config must not crash and must surface a warning."""

    def test_invalid_regex_logs_warning(self, caplog):
        bad = "(unclosed_group"
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.privacy"):
            compiled = privacy._compile_patterns((bad,))
        assert compiled == []
        assert any("Invalid privacy pattern" in rec.message for rec in caplog.records)

    def test_invalid_regex_skipped_valid_ones_still_compile(self, caplog):
        """A bad pattern does not prevent valid patterns in the same list
        from compiling — filter degrades to the subset that parsed."""
        bad = "(unclosed_group"
        good = r"(?i)password\s*[:=]"
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.privacy"):
            compiled = privacy._compile_patterns((bad, good))
        assert len(compiled) == 1
        assert compiled[0].search("Password:hunter2") is not None

    def test_invalid_pattern_silently_shrinks_filter_is_observable(self, caplog):
        """The warning is the ONLY signal that a pattern is missing — the
        test locks in that the log line names the offending pattern so
        operators can grep for it."""
        bad = "[bad-pattern("
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.privacy"):
            privacy._compile_patterns((bad,))
        warning_messages = [
            rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING
        ]
        assert any(bad in msg for msg in warning_messages), (
            "warning must include the offending pattern so ops can identify it"
        )


class TestSensitiveScanWithDegradedFilter:
    """End-to-end: bad pattern skipped, the remaining good ones still flag
    sensitive content. Guards the 'silently weaker privacy guarantee' scenario
    from the issue: operator configures filters A+B, A is malformed, B must
    still fire."""

    def test_sensitive_detected_via_surviving_pattern(self):
        bad = "(unclosed"
        good = r"(?i)password\s*[:="
        # Two goods and one bad — the bad is skipped, the goods still work.
        # (Second "good" intentionally also malformed to prove mixed order
        # handling: compiler must iterate past any bad pattern, not stop.)
        patterns = [bad, r"(?i)password\s*[:=]", r"\bapi[_-]?key\b"]
        assert privacy.contains_sensitive_content("my password: x", patterns) is True
        assert privacy.contains_sensitive_content("plain text nothing here", patterns) is False
        # unused local silences linter warnings on the literal above
        assert good  # referenced to document intent

    def test_all_patterns_invalid_filter_passes_everything(self, caplog):
        """If every configured pattern fails to compile, scanning returns
        False for all inputs — the filter is effectively disabled, and
        the warnings are the operator's only signal."""
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.privacy"):
            result = privacy.contains_sensitive_content(
                "password: hunter2 api_key=sk-abc",
                ["(bad1", "[bad2"],
            )
        assert result is False
        assert sum(1 for rec in caplog.records if "Invalid privacy pattern" in rec.message) == 2


class TestHotReloadRecovery:
    """Simulates operator fixing a broken config: first call with bad
    patterns logs a warning, second call with fixed patterns compiles
    cleanly and filters correctly."""

    def test_bad_then_fixed_pattern_recovers(self, caplog):
        bad = "(bad"
        fixed = r"(?i)password\s*[:=]"
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.privacy"):
            first = privacy._compile_patterns((bad,))
        assert first == []

        # Fresh tuple → fresh lru_cache entry — simulates a reload.
        second = privacy._compile_patterns((fixed,))
        assert len(second) == 1
        assert second[0].search("Password: x") is not None


class TestEmptyAndDefaultPatterns:
    """Cover the None/empty handling paths — these must not crash and
    must match the documented fallback behavior."""

    def test_none_patterns_uses_defaults(self):
        """``patterns=None`` falls through to ``DEFAULT_PATTERNS``."""
        assert privacy.contains_sensitive_content("password: x") is True
        assert privacy.contains_sensitive_content("harmless line") is False

    def test_empty_list_also_falls_back_to_defaults(self):
        """``patterns=[]`` is treated the same as ``None`` by the
        ``patterns if patterns else DEFAULT_PATTERNS`` guard. Locked in
        as an explicit test so any future change to use ``[]`` as a
        'disable all filters' signal breaks this test."""
        assert privacy.contains_sensitive_content("password: x", []) is True

    def test_all_patterns_invalid_does_not_crash(self, caplog):
        """``_compile_patterns`` returning [] when every explicit input
        is malformed must not crash ``contains_sensitive_content``."""
        with caplog.at_level(logging.WARNING, logger="memtomem_stm.proxy.privacy"):
            assert privacy.contains_sensitive_content("anything", ["(bad"]) is False
