"""Tests that unsafe config values are rejected by pydantic validators."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memtomem_stm.config import LangfuseConfig, STMConfig
from memtomem_stm.proxy.config import (
    AutoIndexConfig,
    ExtractionConfig,
    LLMCompressorConfig,
    LLMProvider,
    RelevanceScorerConfig,
    SelectiveConfig,
    UpstreamServerConfig,
)
from memtomem_stm.surfacing.config import SurfacingConfig


class TestProxyNumericConstraints:
    def test_llm_compressor_rejects_nonpositive_max_tokens(self) -> None:
        with pytest.raises(ValidationError):
            LLMCompressorConfig(max_tokens=0)
        with pytest.raises(ValidationError):
            LLMCompressorConfig(max_tokens=-10)

    def test_selective_json_depth_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            SelectiveConfig(json_depth=0)
        with pytest.raises(ValidationError):
            SelectiveConfig(json_depth=-1)
        SelectiveConfig(json_depth=1)  # minimum valid

    def test_selective_min_section_chars_nonnegative(self) -> None:
        with pytest.raises(ValidationError):
            SelectiveConfig(min_section_chars=-1)
        SelectiveConfig(min_section_chars=0)  # zero allowed (passthrough)

    def test_selective_pending_store_literal_rejects_typo(self) -> None:
        with pytest.raises(ValidationError):
            SelectiveConfig(pending_store="memry")  # type: ignore[arg-type]
        SelectiveConfig(pending_store="memory")
        SelectiveConfig(pending_store="sqlite")

    def test_extraction_rejects_invalid_ranges(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionConfig(max_facts=0)
        with pytest.raises(ValidationError):
            ExtractionConfig(min_response_chars=-1)
        with pytest.raises(ValidationError):
            ExtractionConfig(dedup_threshold=1.5)
        with pytest.raises(ValidationError):
            ExtractionConfig(dedup_threshold=-0.1)
        with pytest.raises(ValidationError):
            ExtractionConfig(max_input_chars=0)

    def test_auto_index_min_chars_nonnegative(self) -> None:
        with pytest.raises(ValidationError):
            AutoIndexConfig(min_chars=-100)
        AutoIndexConfig(min_chars=0)  # zero = index everything

    def test_relevance_scorer_embedding_timeout_positive(self) -> None:
        with pytest.raises(ValidationError):
            RelevanceScorerConfig(embedding_timeout=0.0)
        with pytest.raises(ValidationError):
            RelevanceScorerConfig(embedding_timeout=-1.0)

    def test_reconnect_delay_must_not_exceed_max(self) -> None:
        with pytest.raises(ValidationError):
             UpstreamServerConfig(prefix="x", reconnect_delay_seconds=10, max_reconnect_delay_seconds=5)

    def test_reconnect_delay_equal_to_max_is_valid(self) -> None:
        cfg = UpstreamServerConfig(prefix="x", reconnect_delay_seconds=5, max_reconnect_delay_seconds=5)
        assert cfg.reconnect_delay_seconds == 5
    

class TestLLMCompressorApiKey:
    """``provider=openai|anthropic`` with empty ``api_key`` used to send a
    malformed ``Bearer `` header and silently fall back to truncate; validate
    at config-load time instead so misconfiguration is loud."""

    def test_openai_empty_api_key_rejected_when_env_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
            LLMCompressorConfig(provider=LLMProvider.OPENAI)

    def test_anthropic_empty_api_key_rejected_when_env_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
            LLMCompressorConfig(provider=LLMProvider.ANTHROPIC)

    def test_openai_env_fallback_populates_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        cfg = LLMCompressorConfig(provider=LLMProvider.OPENAI)
        assert cfg.api_key == "sk-from-env"

    def test_anthropic_env_fallback_populates_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-from-env")
        cfg = LLMCompressorConfig(provider=LLMProvider.ANTHROPIC)
        assert cfg.api_key == "ant-from-env"

    def test_explicit_api_key_bypasses_env_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = LLMCompressorConfig(provider=LLMProvider.OPENAI, api_key="sk-explicit")
        assert cfg.api_key == "sk-explicit"

    def test_ollama_does_not_require_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg = LLMCompressorConfig(provider=LLMProvider.OLLAMA)
        assert cfg.api_key == ""

    def test_whitespace_only_env_treated_as_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "   ")
        with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
            LLMCompressorConfig(provider=LLMProvider.OPENAI)


class TestSurfacingNumericConstraints:
    def test_surfacing_min_score_range(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(min_score=-0.01)
        with pytest.raises(ValidationError):
            SurfacingConfig(min_score=1.5)

    def test_surfacing_timeouts_positive(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(timeout_seconds=0.0)
        with pytest.raises(ValidationError):
            SurfacingConfig(timeout_seconds=-1.0)
        with pytest.raises(ValidationError):
            SurfacingConfig(circuit_reset_seconds=0.0)

    def test_surfacing_cooldown_nonnegative(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(cooldown_seconds=-1.0)
        SurfacingConfig(cooldown_seconds=0.0)  # 0 disables cooldown

    def test_surfacing_auto_tune_increment_positive(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(auto_tune_score_increment=0.0)
        with pytest.raises(ValidationError):
            SurfacingConfig(auto_tune_score_increment=-0.01)

    def test_surfacing_counts_positive(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(max_results=0)
        with pytest.raises(ValidationError):
            SurfacingConfig(max_surfacings_per_minute=0)
        with pytest.raises(ValidationError):
            SurfacingConfig(max_injection_chars=0)
        with pytest.raises(ValidationError):
            SurfacingConfig(min_query_tokens=0)

    def test_surfacing_context_window_nonnegative(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(context_window_size=-1)
        SurfacingConfig(context_window_size=0)  # 0 disables

    def test_surfacing_injection_mode_literal(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(injection_mode="postpend")  # type: ignore[arg-type]
        for mode in ("prepend", "append", "section"):
            SurfacingConfig(injection_mode=mode)  # type: ignore[arg-type]

    def test_surfacing_result_format_literal(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(result_format="json")  # type: ignore[arg-type]
        SurfacingConfig(result_format="compact")
        SurfacingConfig(result_format="structured")


class TestLangfuseInterdepValidator:
    def test_enabled_requires_both_keys(self) -> None:
        with pytest.raises(ValidationError, match="public_key and secret_key"):
            LangfuseConfig(enabled=True)
        with pytest.raises(ValidationError, match="public_key and secret_key"):
            LangfuseConfig(enabled=True, public_key="pk-lf-x")
        with pytest.raises(ValidationError, match="public_key and secret_key"):
            LangfuseConfig(enabled=True, secret_key="sk-lf-x")

    def test_enabled_with_both_keys_ok(self) -> None:
        cfg = LangfuseConfig(enabled=True, public_key="pk-lf-x", secret_key="sk-lf-x")
        assert cfg.enabled is True

    def test_disabled_allows_empty_keys(self) -> None:
        cfg = LangfuseConfig(enabled=False)
        assert cfg.public_key == ""


class TestLogLevel:
    def test_default_is_warning(self) -> None:
        cfg = STMConfig()
        assert cfg.log_level == "WARNING"

    def test_valid_levels_accepted(self) -> None:
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            cfg = STMConfig(log_level=level)
            assert cfg.log_level == level

    def test_invalid_level_rejected(self) -> None:
        with pytest.raises(ValidationError):
            STMConfig(log_level="TRACE")  # type: ignore[arg-type]

    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMTOMEM_STM_LOG_LEVEL", "DEBUG")
        cfg = STMConfig()
        assert cfg.log_level == "DEBUG"
