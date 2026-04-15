"""Tests for automatic fact extraction."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memtomem_stm.proxy.config import (
    ExtractionConfig,
    ExtractionStrategy,
    LLMCompressorConfig,
    LLMProvider,
    _default_extraction_llm,
)
from memtomem_stm.proxy.extraction import (
    ExtractedFact,
    FactExtractor,
    _extract_heuristic,
    _parse_facts_json,
)


# ---------------------------------------------------------------------------
# _parse_facts_json
# ---------------------------------------------------------------------------


class TestParseFactsJson:
    def test_valid_json_array(self):
        raw = json.dumps(
            [
                {
                    "content": "Python 3.12 is required",
                    "category": "technical",
                    "confidence": 0.9,
                    "tags": ["python"],
                },
                {
                    "content": "Deploy on Friday",
                    "category": "decision",
                    "confidence": 0.7,
                    "tags": [],
                },
            ]
        )
        facts = _parse_facts_json(raw, max_facts=10)
        assert len(facts) == 2
        assert facts[0].content == "Python 3.12 is required"
        assert facts[0].category == "technical"
        assert facts[0].confidence == 0.9
        assert facts[0].tags == ["python"]

    def test_markdown_wrapped_json(self):
        raw = '```json\n[{"content": "fact one", "category": "technical", "confidence": 0.8}]\n```'
        facts = _parse_facts_json(raw, max_facts=10)
        assert len(facts) == 1
        assert facts[0].content == "fact one"

    def test_max_facts_limit(self):
        items = [
            {"content": f"fact {i}", "category": "technical", "confidence": 0.5} for i in range(20)
        ]
        raw = json.dumps(items)
        facts = _parse_facts_json(raw, max_facts=5)
        assert len(facts) == 5

    def test_missing_content_field_skipped(self):
        raw = json.dumps(
            [
                {"category": "technical", "confidence": 0.5},
                {"content": "valid", "category": "technical", "confidence": 0.8},
            ]
        )
        facts = _parse_facts_json(raw, max_facts=10)
        assert len(facts) == 1
        assert facts[0].content == "valid"

    def test_invalid_json_returns_empty(self):
        assert _parse_facts_json("not json at all", max_facts=10) == []

    def test_empty_array_returns_empty(self):
        assert _parse_facts_json("[]", max_facts=10) == []

    def test_defaults_for_missing_fields(self):
        raw = json.dumps([{"content": "just content"}])
        facts = _parse_facts_json(raw, max_facts=10)
        assert len(facts) == 1
        assert facts[0].category == "technical"
        assert facts[0].confidence == 0.5
        assert facts[0].tags == []


# ---------------------------------------------------------------------------
# _extract_heuristic
# ---------------------------------------------------------------------------


class TestExtractHeuristic:
    """Native regex-based heuristic extraction.

    Replaces the empty stub left after decoupling from
    memtomem.tools.entity_extraction. Self-contained — no core
    dependency, no external NLP. Recognizes URLs, ISO dates,
    decision/action-item lines, identifiers (snake/camel/pascal),
    and quoted concepts.
    """

    def test_empty_text_returns_empty(self):
        assert _extract_heuristic("", max_facts=10) == []

    def test_zero_max_facts_returns_empty(self):
        assert _extract_heuristic("Decision: ship it.", max_facts=0) == []

    def test_no_signal_text_returns_empty(self):
        # Plain prose with no URLs, identifiers, or marker phrases.
        assert _extract_heuristic("hello world this is fine", max_facts=10) == []

    def test_extracts_urls(self):
        text = "See https://example.com/docs and http://api.test.io for details."
        facts = _extract_heuristic(text, max_facts=10)
        urls = {f.content for f in facts if f.category == "url"}
        assert "https://example.com/docs" in urls
        assert "http://api.test.io" in urls

    def test_strips_trailing_punctuation_from_url(self):
        text = "Check out https://example.com."
        facts = _extract_heuristic(text, max_facts=10)
        urls = [f.content for f in facts if f.category == "url"]
        assert "https://example.com" in urls

    def test_url_high_confidence(self):
        facts = _extract_heuristic("https://example.com", max_facts=10)
        urls = [f for f in facts if f.category == "url"]
        assert urls and all(f.confidence >= 0.9 for f in urls)

    def test_extracts_iso_dates(self):
        text = "Released 2026-04-09. Next milestone is 2026-05-01."
        facts = _extract_heuristic(text, max_facts=10)
        dates = {f.content for f in facts if f.category == "date"}
        assert "2026-04-09" in dates
        assert "2026-05-01" in dates

    def test_extracts_decision_lines(self):
        text = "Decision: use SQLite for storage.\nResolved: ship Friday."
        facts = _extract_heuristic(text, max_facts=10)
        decisions = [f.content for f in facts if f.category == "decision"]
        assert any("SQLite" in d for d in decisions)
        assert any("Friday" in d for d in decisions)

    def test_extracts_we_will_decision(self):
        text = "We will migrate to Postgres next sprint."
        facts = _extract_heuristic(text, max_facts=10)
        decisions = [f.content for f in facts if f.category == "decision"]
        assert any("Postgres" in d for d in decisions)

    def test_extracts_todo_action_items(self):
        text = "TODO: write tests for the worker pool"
        facts = _extract_heuristic(text, max_facts=10)
        actions = [f.content for f in facts if f.category == "action_item"]
        assert any("write tests" in a for a in actions)

    def test_extracts_checkbox_action_items(self):
        text = "- [ ] update README\n- [ ] bump version"
        facts = _extract_heuristic(text, max_facts=10)
        actions = [f.content for f in facts if f.category == "action_item"]
        assert any("update README" in a for a in actions)
        assert any("bump version" in a for a in actions)

    def test_extracts_fixme_action_item(self):
        text = "FIXME: leak in the worker thread"
        facts = _extract_heuristic(text, max_facts=10)
        actions = [f.content for f in facts if f.category == "action_item"]
        assert any("leak" in a for a in actions)

    def test_extracts_snake_case_identifiers(self):
        text = "Call get_user_id(user_name) and check api_v2 response."
        facts = _extract_heuristic(text, max_facts=20)
        ids = {f.content for f in facts if f.category == "identifier"}
        assert "get_user_id" in ids
        assert "user_name" in ids
        assert "api_v2" in ids

    def test_extracts_camel_case_identifiers(self):
        text = "Use myHelper() then call getValue() on the response."
        facts = _extract_heuristic(text, max_facts=20)
        ids = {f.content for f in facts if f.category == "identifier"}
        assert "myHelper" in ids
        assert "getValue" in ids

    def test_extracts_pascal_case_identifiers(self):
        text = "The MyService class extends BaseHandler in production."
        facts = _extract_heuristic(text, max_facts=20)
        ids = {f.content for f in facts if f.category == "identifier"}
        assert "MyService" in ids
        assert "BaseHandler" in ids

    def test_pascal_case_skips_single_title_word(self):
        # Single-Pascal words like "Use" or "Redis" are too noisy to extract.
        text = "Use Redis for caching."
        facts = _extract_heuristic(text, max_facts=10)
        ids = {f.content for f in facts if f.category == "identifier"}
        assert "Use" not in ids
        assert "Redis" not in ids

    def test_extracts_quoted_concepts(self):
        text = 'The term "eventual consistency" comes up often.'
        facts = _extract_heuristic(text, max_facts=10)
        concepts = [f.content for f in facts if f.category == "concept"]
        assert "eventual consistency" in concepts

    def test_dedup_within_category(self):
        text = "https://example.com appears, then https://example.com again."
        facts = _extract_heuristic(text, max_facts=10)
        urls = [f for f in facts if f.category == "url"]
        assert len(urls) == 1

    def test_max_facts_cap(self):
        # Many distinct snake_case identifiers — cap should hold.
        text = " ".join(f"var_{i}_name" for i in range(50))
        facts = _extract_heuristic(text, max_facts=5)
        assert len(facts) == 5

    def test_returns_extracted_fact_with_tags(self):
        text = "TODO: implement caching layer"
        facts = _extract_heuristic(text, max_facts=10)
        assert facts
        assert all(isinstance(f, ExtractedFact) for f in facts)
        assert all(f.tags == [f.category] for f in facts)


# ---------------------------------------------------------------------------
# ExtractionConfig
# ---------------------------------------------------------------------------


class TestExtractionConfig:
    def test_defaults(self):
        cfg = ExtractionConfig()
        assert cfg.enabled is False
        assert cfg.strategy == ExtractionStrategy.LLM
        assert cfg.llm is None
        assert cfg.max_facts == 10
        assert cfg.background is True

    def test_effective_llm_default(self):
        cfg = ExtractionConfig()
        llm = cfg.effective_llm()
        assert llm.provider == LLMProvider.OLLAMA
        assert llm.model == "qwen3:4b"
        assert "/no_think" in llm.system_prompt

    def test_effective_llm_user_override(self):
        custom = LLMCompressorConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-4.1-nano",
            api_key="sk-test",
        )
        cfg = ExtractionConfig(llm=custom)
        llm = cfg.effective_llm()
        assert llm.provider == LLMProvider.OPENAI
        assert llm.model == "gpt-4.1-nano"

    def test_default_extraction_llm_function(self):
        llm = _default_extraction_llm()
        assert llm.provider == LLMProvider.OLLAMA
        assert llm.model == "qwen3:4b"
        assert llm.max_tokens == 1000


# ---------------------------------------------------------------------------
# FactExtractor
# ---------------------------------------------------------------------------


class TestFactExtractor:
    async def test_skip_short_text(self):
        cfg = ExtractionConfig(enabled=True, min_response_chars=100)
        extractor = FactExtractor(cfg)
        result = await extractor.extract("short", server="test", tool="read")
        assert result == []

    async def test_none_strategy_returns_empty(self):
        cfg = ExtractionConfig(enabled=True, strategy=ExtractionStrategy.NONE)
        extractor = FactExtractor(cfg)
        result = await extractor.extract("x" * 1000, server="test", tool="read")
        assert result == []

    async def test_heuristic_strategy(self):
        cfg = ExtractionConfig(
            enabled=True,
            strategy=ExtractionStrategy.HEURISTIC,
            min_response_chars=10,
        )
        extractor = FactExtractor(cfg)
        text = "Decision: Use SQLite for storage. Author: Jane Doe\n" * 10
        result = await extractor.extract(text, server="test", tool="read")
        assert isinstance(result, list)


class TestFactExtractorLLM:
    async def test_llm_success(self):
        cfg = ExtractionConfig(enabled=True, min_response_chars=10)
        extractor = FactExtractor(cfg)

        mock_response = json.dumps(
            [
                {"content": "fact 1", "category": "technical", "confidence": 0.9, "tags": ["test"]},
            ]
        )
        with patch.object(
            extractor, "_call_api", new_callable=AsyncMock, return_value=mock_response
        ):
            facts = await extractor.extract("x" * 100, server="s", tool="t")

        assert len(facts) == 1
        assert facts[0].content == "fact 1"

    async def test_llm_failure_falls_back_to_heuristic(self):
        cfg = ExtractionConfig(enabled=True, min_response_chars=10)
        extractor = FactExtractor(cfg)

        with patch.object(
            extractor, "_call_api", new_callable=AsyncMock, side_effect=RuntimeError("API down")
        ):
            text = "Decision: fallback works. " * 20
            facts = await extractor.extract(text, server="s", tool="t")

        # Should get heuristic results, not crash
        assert isinstance(facts, list)

    async def test_circuit_breaker_opens(self):
        cfg = ExtractionConfig(enabled=True, min_response_chars=10)
        extractor = FactExtractor(cfg)

        with patch.object(
            extractor, "_call_api", new_callable=AsyncMock, side_effect=RuntimeError("fail")
        ):
            for _ in range(4):
                await extractor.extract("x" * 100, server="s", tool="t")

        assert extractor._cb.state == "open"

    async def test_hybrid_merges_llm_and_heuristic(self):
        cfg = ExtractionConfig(
            enabled=True,
            strategy=ExtractionStrategy.HYBRID,
            min_response_chars=10,
        )
        extractor = FactExtractor(cfg)

        mock_response = json.dumps(
            [
                {"content": "LLM fact", "category": "technical", "confidence": 0.9},
            ]
        )
        with patch.object(
            extractor, "_call_api", new_callable=AsyncMock, return_value=mock_response
        ):
            text = "Decision: Use Redis. LLM fact is separate. " * 10
            facts = await extractor.extract(text, server="s", tool="t")

        contents = [f.content for f in facts]
        assert "LLM fact" in contents

    async def test_truncation_respects_max_input_chars(self):
        cfg = ExtractionConfig(enabled=True, min_response_chars=10, max_input_chars=100)
        extractor = FactExtractor(cfg)

        captured_text = None

        async def mock_api(text):
            nonlocal captured_text
            captured_text = text
            return "[]"

        with patch.object(extractor, "_call_api", side_effect=mock_api):
            await extractor.extract("x" * 5000, server="s", tool="t")

        assert captured_text is not None
        assert len(captured_text) <= 100


# ---------------------------------------------------------------------------
# Provider response defense (mirrors compression.py guards from #114/#119)
# ---------------------------------------------------------------------------


def _mock_http_response(payload: dict) -> MagicMock:
    """Build an httpx-like response whose ``.json()`` returns ``payload``."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _make_extractor(provider: LLMProvider) -> FactExtractor:
    """FactExtractor with the given provider. api_key satisfies PR #123 validator;
    system_prompt uses the ``{max_facts}`` placeholder that ``_call_api`` expects
    (the compressor-config default uses ``{max_chars}`` and would KeyError here)."""
    llm_cfg = LLMCompressorConfig(
        provider=provider,
        api_key="test-key",
        system_prompt="Extract up to {max_facts} facts as JSON.",
    )
    cfg = ExtractionConfig(
        enabled=True,
        strategy=ExtractionStrategy.LLM,
        llm=llm_cfg,
        min_response_chars=10,
    )
    return FactExtractor(cfg)


class TestOpenAIResponseDefense:
    """OpenAI content filter / quota returns ``{"choices": []}`` with 200 OK.
    Previously crashed with IndexError; now raises ValueError which the
    ``_extract_llm`` caller catches and falls back to heuristic extraction."""

    async def test_valid_response_returns_content(self):
        extractor = _make_extractor(LLMProvider.OPENAI)
        payload = {"choices": [{"message": {"content": '[{"content": "fact"}]'}}]}
        with patch.object(
            extractor._client, "post", AsyncMock(return_value=_mock_http_response(payload))
        ):
            result = await extractor._openai("text", "prompt")
        assert result == '[{"content": "fact"}]'

    async def test_empty_choices_raises_valueerror(self):
        extractor = _make_extractor(LLMProvider.OPENAI)
        with patch.object(
            extractor._client,
            "post",
            AsyncMock(return_value=_mock_http_response({"choices": []})),
        ):
            with pytest.raises(ValueError, match="empty 'choices'"):
                await extractor._openai("text", "prompt")

    async def test_missing_content_raises_valueerror(self):
        extractor = _make_extractor(LLMProvider.OPENAI)
        payload = {"choices": [{"message": {}}]}  # no "content" key
        with patch.object(
            extractor._client, "post", AsyncMock(return_value=_mock_http_response(payload))
        ):
            with pytest.raises(ValueError, match="choices\\[0\\].message.content"):
                await extractor._openai("text", "prompt")

    async def test_non_dict_root_raises_valueerror(self):
        """Some proxies return a bare error string or array on 200 OK."""
        extractor = _make_extractor(LLMProvider.OPENAI)
        with patch.object(
            extractor._client,
            "post",
            AsyncMock(return_value=_mock_http_response({"error": "rate limited"})),
        ):
            with pytest.raises(ValueError, match="empty 'choices'"):
                await extractor._openai("text", "prompt")


class TestAnthropicResponseDefense:
    """Anthropic safety filter returns ``{"content": []}`` with 200 OK."""

    async def test_valid_response_returns_text(self):
        extractor = _make_extractor(LLMProvider.ANTHROPIC)
        payload = {"content": [{"text": '[{"content": "fact"}]'}]}
        with patch.object(
            extractor._client, "post", AsyncMock(return_value=_mock_http_response(payload))
        ):
            result = await extractor._anthropic("text", "prompt")
        assert result == '[{"content": "fact"}]'

    async def test_empty_content_raises_valueerror(self):
        extractor = _make_extractor(LLMProvider.ANTHROPIC)
        with patch.object(
            extractor._client,
            "post",
            AsyncMock(return_value=_mock_http_response({"content": []})),
        ):
            with pytest.raises(ValueError, match="empty 'content'"):
                await extractor._anthropic("text", "prompt")

    async def test_missing_text_raises_valueerror(self):
        extractor = _make_extractor(LLMProvider.ANTHROPIC)
        payload = {"content": [{"type": "tool_use"}]}  # no "text" field
        with patch.object(
            extractor._client, "post", AsyncMock(return_value=_mock_http_response(payload))
        ):
            with pytest.raises(ValueError, match="content\\[0\\].text"):
                await extractor._anthropic("text", "prompt")


class TestOllamaResponseDefense:
    """Ollama returns malformed responses when the model errors mid-generation
    or when called with a wrong endpoint shape."""

    async def test_valid_response_returns_content(self):
        extractor = _make_extractor(LLMProvider.OLLAMA)
        payload = {"message": {"role": "assistant", "content": '[{"content": "fact"}]'}}
        with patch.object(
            extractor._client, "post", AsyncMock(return_value=_mock_http_response(payload))
        ):
            result = await extractor._ollama("text", "prompt")
        assert result == '[{"content": "fact"}]'

    async def test_missing_message_raises_valueerror(self):
        extractor = _make_extractor(LLMProvider.OLLAMA)
        with patch.object(
            extractor._client,
            "post",
            AsyncMock(return_value=_mock_http_response({"done": True})),
        ):
            with pytest.raises(ValueError, match="message.content"):
                await extractor._ollama("text", "prompt")

    async def test_non_string_content_raises_valueerror(self):
        """Ollama occasionally emits ``content: null`` for tool-only turns."""
        extractor = _make_extractor(LLMProvider.OLLAMA)
        payload = {"message": {"content": None}}
        with patch.object(
            extractor._client, "post", AsyncMock(return_value=_mock_http_response(payload))
        ):
            with pytest.raises(ValueError, match="message.content"):
                await extractor._ollama("text", "prompt")


class TestExtractLlmFallsBackOnValueError:
    """End-to-end: malformed provider response → ValueError → heuristic fallback.
    Ensures the caller (_extract_llm) treats the new ValueError the same as
    a transport error and doesn't regress into returning ``[]`` silently."""

    async def test_empty_choices_triggers_heuristic_fallback(self):
        extractor = _make_extractor(LLMProvider.OPENAI)
        with patch.object(
            extractor._client,
            "post",
            AsyncMock(return_value=_mock_http_response({"choices": []})),
        ):
            text = "Decision: Use Redis for caching. " * 20
            facts = await extractor.extract(text, server="s", tool="t")

        assert isinstance(facts, list)
        assert extractor._cb.failure_count == 1


# ---------------------------------------------------------------------------
# ExtractedFact dataclass
# ---------------------------------------------------------------------------


class TestExtractedFact:
    def test_frozen(self):
        fact = ExtractedFact(content="test", category="technical", confidence=0.5)
        with pytest.raises(AttributeError):
            fact.content = "changed"

    def test_default_tags(self):
        fact = ExtractedFact(content="test", category="technical", confidence=0.5)
        assert fact.tags == []


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


class TestExtractionStrategy:
    def test_all_values(self):
        assert set(ExtractionStrategy) == {"none", "llm", "heuristic", "hybrid"}
