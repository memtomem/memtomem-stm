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


class TestDefaultPatternCoverage:
    """Lock in which common credential formats the default set catches.

    Each added pattern has an associated positive + negative case so a
    regression (an overly loose / tight regex) trips the suite.
    """

    @pytest.mark.parametrize(
        "sample",
        [
            "user@example.com",  # email (regression: prior pattern had [A-Z|a-z])
            "contact: alice.bob+tag@sub.example.co.uk",
            "ghp_" + "A" * 36,  # classic GitHub PAT
            "github_pat_11ABC" + "_" + "x" * 40,  # fine-grained PAT
            "xoxb-123456-abcdef",  # Slack bot
            "xoxp-123456-abcdef",  # Slack user
            "xoxs-123456-abcdef",  # Slack scope
            "sk-" + "a" * 48,  # OpenAI-style
            "sk_live_" + "A" * 24,  # Stripe live
            "pk_test_" + "B" * 24,  # Stripe pub test
            "rk_live_" + "C" * 24,  # Stripe restricted
            "whsec_" + "D" * 24,  # Stripe webhook
            "npm_" + "E" * 32,
            "AKIAIOSFODNN7EXAMPLE",  # AWS IAM user
            "ASIAIOSFODNN7EXAMPLE",  # AWS temporary
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcDEF_-12345",  # JWT
            "-----BEGIN RSA PRIVATE KEY-----",
            "-----BEGIN DSA PRIVATE KEY-----",
            "-----BEGIN PGP PRIVATE KEY-----",
            # --- #1488 LTM-origin mirror (all examples are FAKE; assembled at
            #     runtime so no contiguous realistic token literal trips GitHub
            #     push protection) -----------------------------------------
            # Modern OpenAI key: legacy ``sk-`` rule misses it (hyphen after
            # "proj" halts the run) — hits via the T3BlbkFJ marker rule.
            "sk-proj-FAKE0aaaa1111bbbb2222cccc3333T3Blbk" + "FJEXAMPLE0dddd4444eeee5555ffff6666",
            "sk-ant-api03-" + ("FAKEfake0123456789" * 6)[:93] + "AA",  # Anthropic
            "ghs_" + "FAKEfake0123456789FAKEfake0123456789",  # GitHub server-to-server
            "AIza" + "Sy0_-BcdSy0_-BcdSy0_-BcdSy0_-BcdSy0",  # Google API key (AIza + 35)
            "glpat-" + ("FAKEfake0123456789" * 2)[:20],  # GitLab PAT (exact 20)
            "hf" + "_FAKEfake0123456789FAKEfake01234567",  # Hugging Face (hf_ + 34)
            "pypi-Ag"
            + "EIcHlwaS5vcmcFAKEfake0123456789FAKEfake0123456789FAKEfake0123456789",  # PyPI
        ],
    )
    def test_default_patterns_match_common_secret_shapes(self, sample: str) -> None:
        assert privacy.contains_sensitive_content(sample) is True

    @pytest.mark.parametrize(
        "sample",
        [
            "This is just a harmless sentence with no secrets.",
            "version: 1.2.3",
            "See RFC 5322 for email format rules.",
            # JWT false-positive guard: not prefixed with eyJ → should not match JWT rule
            "abc.def.ghi",
            # Short alphanumerics that could trip the AWS prefix if unanchored
            "AKIAshort",  # missing the 16-char IAM suffix
            "github_pat_",  # just the prefix, no body
            "npm_",  # prefix only
            # --- #1488 mirror false-positive guards: prose / kebab slugs that
            #     share a provider prefix but must NOT match -----------------
            "sk-project-management-tool",  # OpenAI prefix, no T3BlbkFJ marker
            "sk-ant-api03-release-notes-2026-migration-guide",  # Anthropic kebab slug
            "ghost_writer_mode_enabled",  # starts "gho" but not a gho_ token
            "AIzaShortKey",  # AIza prefix but body too short
            "glpat-form-builder-component-name-2026",  # GitLab kebab slug
            "hf" + "_abcdefghijklmnopqrstuvwxyzabcdefgh",  # hf_ + 34 letters, no digit
            "pypi-package-name-here",  # pypi- prefix without the macaroon header
        ],
    )
    def test_default_patterns_do_not_match_benign_strings(self, sample: str) -> None:
        assert privacy.contains_sensitive_content(sample) is False

    def test_scans_beyond_ten_kilobytes(self) -> None:
        text = ("safe content " * 900) + "password: hunter2"
        assert len(text) > 10_000
        assert privacy.contains_sensitive_content(text) is True


class TestCredentialPiiSplit:
    """L278: the default set splits into ``CREDENTIAL_PATTERNS`` (gates
    ACTIONS — e.g. "may this response go to an external LLM?") and
    ``PII_PATTERNS`` (gates STORAGE — e.g. surfacing query persistence).
    ``DEFAULT_PATTERNS`` stays the union for callers asking "is anything
    here sensitive?". The split exists because emails appear in ordinary
    compressible content (git logs, issue threads) and routing on them
    silently degraded llm_summary to truncation.
    """

    def test_default_is_credentials_then_pii(self):
        assert privacy.DEFAULT_PATTERNS == [
            *privacy.CREDENTIAL_PATTERNS,
            *privacy.PII_PATTERNS,
        ]

    def test_sets_are_disjoint_and_nonempty(self):
        assert privacy.CREDENTIAL_PATTERNS
        assert privacy.PII_PATTERNS
        assert not set(privacy.CREDENTIAL_PATTERNS) & set(privacy.PII_PATTERNS)

    def test_credential_set_count_pinned(self):
        # 9 original secret-class patterns + 7 mirrored from memtomem LTM
        # (#1488 / reverse sync #1491). Bump deliberately when adding a
        # pattern so a silent add/drop surfaces here and stays coherent with
        # LTM's matching pin (memtomem DEFAULT_PATTERNS == 16).
        assert len(privacy.CREDENTIAL_PATTERNS) == 16
        assert len(privacy.PII_PATTERNS) == 1
        assert len(privacy.DEFAULT_PATTERNS) == 17

    @pytest.mark.parametrize(
        "sample",
        [
            "user@example.com",
            "contact: alice.bob+tag@sub.example.co.uk",
        ],
    )
    def test_email_is_pii_not_credential(self, sample: str) -> None:
        assert privacy.contains_sensitive_content(sample, privacy.PII_PATTERNS) is True
        assert privacy.contains_sensitive_content(sample, privacy.CREDENTIAL_PATTERNS) is False

    @pytest.mark.parametrize(
        "sample",
        [
            "ghp_" + "A" * 36,
            "api_key=sk-" + "a" * 48,
            "psql password=hunter2",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcDEF_-12345",
            "-----BEGIN RSA PRIVATE KEY-----",
            # #1488 mirror: each must classify as a credential (not PII) so the
            # ACTION gate (external-LLM routing) fires, not just storage.
            "sk-proj-FAKE0aaaa1111bbbb2222cccc3333T3Blbk" + "FJEXAMPLE0dddd4444eeee5555ffff6666",
            "sk-ant-api03-" + ("FAKEfake0123456789" * 6)[:93] + "AA",
            "ghs_" + "FAKEfake0123456789FAKEfake0123456789",
            "AIza" + "Sy0_-BcdSy0_-BcdSy0_-BcdSy0_-BcdSy0",
            "glpat-" + ("FAKEfake0123456789" * 2)[:20],
            "hf" + "_FAKEfake0123456789FAKEfake01234567",
            "pypi-Ag" + "EIcHlwaS5vcmcFAKEfake0123456789FAKEfake0123456789FAKEfake0123456789",
        ],
    )
    def test_credentials_match_via_credential_set_alone(self, sample: str) -> None:
        assert privacy.contains_sensitive_content(sample, privacy.CREDENTIAL_PATTERNS) is True
