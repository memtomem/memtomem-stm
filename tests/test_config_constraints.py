"""Tests that unsafe config values are rejected by pydantic validators."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memtomem_stm.config import LangfuseConfig
from memtomem_stm.proxy.config import (
    AutoIndexConfig,
    ExtractionConfig,
    LLMCompressorConfig,
    RelevanceScorerConfig,
    SelectiveConfig,
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
