"""Tests for automatic fact extraction."""

from __future__ import annotations

import asyncio
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
from memtomem_stm.proxy.privacy import CREDENTIAL_PATTERNS, contains_sensitive_content


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

    @pytest.mark.parametrize("surrogate", ["\ud800", "\udbff", "\udc00", "\udfff"])
    def test_nested_json_content_is_scrubbed_at_ingest(self, surrogate):
        raw = json.dumps(
            [
                {
                    "content": f"fact{surrogate}",
                    "category": f"cat{surrogate}",
                    "tags": [f"tag{surrogate}"],
                }
            ]
        )
        fact = _parse_facts_json(raw, max_facts=1)[0]
        literal = f"\\u{ord(surrogate):04x}"
        assert fact.content == f"fact{literal}"
        assert fact.category == f"cat{literal}"
        assert fact.tags == [f"tag{literal}"]
        fact.content.encode("utf-8")


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

    async def test_privacy_scan_blocks_call_api_for_credentials(self):
        # #454: action gate — a credential-bearing response must never reach
        # the provider; heuristic extraction runs locally instead.
        cfg = ExtractionConfig(enabled=True, min_response_chars=10)
        extractor = FactExtractor(cfg)

        with patch.object(extractor, "_call_api", new_callable=AsyncMock) as call_api:
            text = "creds: password=hunter2\nDecision: rotate the key now. " + "x" * 50
            facts = await extractor.extract(text, server="s", tool="t")

        call_api.assert_not_awaited()
        # Heuristic fallback still produced local facts (the Decision line).
        assert any(f.category == "decision" for f in facts)

    async def test_privacy_scan_is_credentials_only(self):
        # Emails are PII, not credentials (#461): fine to SHOW the provider,
        # not fine to persist. The action gate must not fire on an email
        # alone — persistence-side blocking is memory_ops' job.
        cfg = ExtractionConfig(enabled=True, min_response_chars=10)
        extractor = FactExtractor(cfg)

        mock_response = json.dumps([{"content": "f", "category": "c", "confidence": 0.9}])
        with patch.object(
            extractor, "_call_api", new_callable=AsyncMock, return_value=mock_response
        ) as call_api:
            await extractor.extract("contact dev@example.com " * 10, server="s", tool="t")

        call_api.assert_awaited_once()

    async def test_privacy_scan_disabled_reaches_call_api(self):
        cfg = ExtractionConfig(
            enabled=True,
            min_response_chars=10,
            llm=LLMCompressorConfig(
                provider=LLMProvider.OLLAMA,
                model="qwen3:4b",
                privacy_scan_enabled=False,
            ),
        )
        extractor = FactExtractor(cfg)

        mock_response = json.dumps([{"content": "f", "category": "c", "confidence": 0.9}])
        with patch.object(
            extractor, "_call_api", new_callable=AsyncMock, return_value=mock_response
        ) as call_api:
            await extractor.extract("password=hunter2 " * 10, server="s", tool="t")

        call_api.assert_awaited_once()

    async def test_hybrid_privacy_scan_blocks_call_api(self):
        # HYBRID routes through _extract_llm too — the gate covers it.
        cfg = ExtractionConfig(
            enabled=True,
            strategy=ExtractionStrategy.HYBRID,
            min_response_chars=10,
        )
        extractor = FactExtractor(cfg)

        with patch.object(extractor, "_call_api", new_callable=AsyncMock) as call_api:
            facts = await extractor.extract("api_key: zzz-secret " * 10, server="s", tool="t")

        call_api.assert_not_awaited()
        assert isinstance(facts, list)

    async def test_privacy_scan_runs_before_truncation(self):
        # A credential split at the max_input_chars boundary must still fire
        # the gate: slicing leaves 15 of the AKIA key's 16 trailing chars, so
        # the truncated text matches no pattern while a near-complete secret
        # prefix would ship. The scan must see the pre-truncation text.
        cfg = ExtractionConfig(enabled=True, min_response_chars=10, max_input_chars=30)
        extractor = FactExtractor(cfg)
        text = "x" * 10 + " AKIA" + "A" * 16
        # Pin the repro shape itself — if either assert breaks, the test no
        # longer exercises the boundary split and must be reconstructed.
        assert not contains_sensitive_content(text[:30], CREDENTIAL_PATTERNS)
        assert contains_sensitive_content(text, CREDENTIAL_PATTERNS)

        with patch.object(extractor, "_call_api", new_callable=AsyncMock) as call_api:
            await extractor.extract(text, server="s", tool="t")

        call_api.assert_not_awaited()

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


# ---------------------------------------------------------------------------
# LLM timeout bound (mirrors LLMCompressor.compress)
# ---------------------------------------------------------------------------


def _tiny_timeout_config() -> ExtractionConfig:
    return ExtractionConfig(
        enabled=True,
        min_response_chars=10,
        llm=LLMCompressorConfig(
            provider=LLMProvider.OLLAMA,
            model="qwen3:4b",
            llm_timeout_seconds=0.05,
        ),
    )


class TestFactExtractorTimeout:
    async def test_hung_call_times_out_to_heuristic(self):
        """A provider that never responds must not hold the caller past
        llm_timeout_seconds: the client-level httpx timeout only covers
        socket phases, so the wait_for is the one wall-clock bound on the
        tool-response path (extraction.background=False awaits this inline)."""
        cfg = _tiny_timeout_config()
        extractor = FactExtractor(cfg)

        async def hang(text: str) -> str:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        with patch.object(extractor, "_call_api", side_effect=hang):
            text = "Decision: heuristic fallback reached. " * 5
            facts = await asyncio.wait_for(extractor.extract(text, server="s", tool="t"), timeout=5)

        # The heuristic path actually ran (the Decision line is its signal),
        # and the breaker recorded the failure.
        assert any(f.category == "decision" for f in facts)
        assert extractor._cb._failures == 1

    async def test_fast_call_unaffected_by_timeout_wrapper(self):
        # Positive control: with the same tiny budget, a prompt reply still
        # takes the LLM path — the wrapper only bounds, never rejects.
        cfg = _tiny_timeout_config()
        extractor = FactExtractor(cfg)

        mock_response = json.dumps([{"content": "f", "category": "c", "confidence": 0.9}])
        with patch.object(
            extractor, "_call_api", new_callable=AsyncMock, return_value=mock_response
        ):
            facts = await extractor.extract("x" * 100, server="s", tool="t")

        assert [f.content for f in facts] == ["f"]
        assert extractor._cb._failures == 0

    async def test_repeated_timeouts_open_breaker(self):
        cfg = _tiny_timeout_config()
        extractor = FactExtractor(cfg)

        async def hang(text: str) -> str:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        with patch.object(extractor, "_call_api", side_effect=hang):
            for _ in range(4):
                # Outer guard so a regression in the production timeout fails
                # the test instead of hanging the run (codex review round 2).
                await asyncio.wait_for(
                    extractor.extract("x" * 100, server="s", tool="t"), timeout=5
                )

        assert extractor._cb.state == "open"


class TestFactExtractorShutdown:
    """#867: close() must drain in-flight extract() calls, bounded.

    FactExtractor had no drain at all — ``aclose()`` could land under a live
    request, tearing the httpx client down mid-call. It now shares the
    ``InFlightGate`` LLMCompressor uses, with the same bounded wait so a stuck
    caller costs the drain ceiling rather than the process.
    """

    async def test_close_waits_for_in_flight_extract(self):
        import time

        cfg = _tiny_timeout_config()
        cfg.llm.llm_timeout_seconds = 30.0  # long enough that the gate, not the call, decides
        cfg.strategy = ExtractionStrategy.LLM
        extractor = FactExtractor(cfg)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_call_api(text: str) -> str:
            started.set()
            await release.wait()
            return '[{"content": "f", "type": "concept", "confidence": 0.9}]'

        with patch.object(extractor, "_call_api", new=slow_call_api):
            task = asyncio.create_task(extractor.extract("x" * 100, server="s", tool="t"))
            await started.wait()

            # Observe aclose() directly: ``_client`` is only nulled AFTER the
            # await returns, so "client is not None" alone would also hold for
            # an implementation already stalled inside aclose().
            aclose_calls = 0
            real_client = extractor._client

            async def recording_aclose() -> None:
                nonlocal aclose_calls
                aclose_calls += 1
                await real_client.aclose()

            extractor._client = MagicMock(aclose=recording_aclose)

            close_task = asyncio.create_task(extractor.close())
            for _ in range(10):
                await asyncio.sleep(0)

            assert not close_task.done(), (
                "close() returned while an extract was in flight — no drain"
            )
            assert aclose_calls == 0, (
                "close() called aclose() on the httpx client while extract was mid-call"
            )

            release.set()
            facts = await task
            # Releasing the registration — not a sleep inside close() — is
            # what lets close() finish, so it must complete promptly now.
            released_at = time.monotonic()
            await asyncio.wait_for(close_task, timeout=5.0)
            assert time.monotonic() - released_at < 1.0, (
                "close() did not resume promptly when the registration was released "
                "— it may be waiting on something other than the gate"
            )
            assert aclose_calls == 1

        assert [f.content for f in facts] == ["f"]
        assert extractor._client is None

    async def test_extract_after_close_refuses_even_with_a_live_client(self):
        # The gate must be the ``_closed`` flag, not "the client happens to be
        # None": a stale client reference (a config swap handing the extractor
        # back, a caller holding the instance) must still take the local
        # heuristic instead of crossing the provider boundary. Nulling the
        # client alone would make this pass without any gate at all.
        cfg = _tiny_timeout_config()
        cfg.strategy = ExtractionStrategy.LLM
        extractor = FactExtractor(cfg)
        await extractor.close()
        extractor._client = MagicMock()  # resurrect a client the gate must ignore

        called = False

        async def record_call(text: str) -> str:
            # A raise here would be swallowed by _extract_llm's broad except
            # and degrade to the heuristic anyway — the assertion would then
            # hold for the wrong reason. Record instead.
            nonlocal called
            called = True
            return "[]"

        with patch.object(extractor, "_call_api", new=record_call):
            facts = await extractor.extract("def some_function(): pass", server="s", tool="t")

        assert not called, "LLM route ran after close()"
        assert all(isinstance(f, ExtractedFact) for f in facts)

    async def test_close_drain_is_bounded_when_a_call_never_finishes(self):
        # Assert the ceiling handed to drain_or_warn, not elapsed wall clock:
        # a slow CI worker cannot then make an immediate return look like a
        # completed wait.
        from memtomem_stm.utils.anyio_shutdown import CLOSE_DRAIN_GRACE_SECONDS

        cfg = _tiny_timeout_config()  # llm_timeout_seconds=0.05
        extractor = FactExtractor(cfg)
        extractor._gate.enter(cfg.llm.llm_timeout_seconds)

        seen: dict[str, object] = {}

        async def recording_drain(idle, *, timeout, what):
            seen["timeout"] = timeout
            seen["what"] = what
            return False

        with patch("memtomem_stm.proxy.extraction.drain_or_warn", new=recording_drain):
            await asyncio.wait_for(extractor.close(), timeout=5.0)

        assert seen["what"] == "FactExtractor"
        assert (
            CLOSE_DRAIN_GRACE_SECONDS
            < seen["timeout"]
            <= cfg.llm.llm_timeout_seconds + CLOSE_DRAIN_GRACE_SECONDS
        ), seen
        assert extractor._client is None

    async def test_close_drain_actually_waits_and_gives_up(self):
        # The real drain_or_warn must return on its own with the gate stuck.
        import time

        cfg = _tiny_timeout_config()
        extractor = FactExtractor(cfg)
        extractor._gate.enter(cfg.llm.llm_timeout_seconds)

        started = time.monotonic()
        await asyncio.wait_for(extractor.close(), timeout=30.0)
        elapsed = time.monotonic() - started

        assert elapsed >= cfg.llm.llm_timeout_seconds
        assert extractor._client is None

    async def test_close_drain_ceiling_follows_the_captured_deadline(self):
        # The ceiling must come from what the live caller captured, not from
        # a re-read of the mutable config: another task lowering
        # llm_timeout_seconds mid-call must not shorten the drain under it.
        cfg = _tiny_timeout_config()
        extractor = FactExtractor(cfg)
        extractor._gate.enter(3.0)  # what the live caller captured
        cfg.llm.llm_timeout_seconds = 0.05  # config lowered afterwards

        seen: dict[str, object] = {}

        async def recording_drain(idle, *, timeout, what):
            seen["timeout"] = timeout
            return False

        with patch("memtomem_stm.proxy.extraction.drain_or_warn", new=recording_drain):
            await asyncio.wait_for(extractor.close(), timeout=5.0)

        assert seen["timeout"] > 2.9, (
            f"ceiling {seen['timeout']} re-read the lowered config instead of honoring "
            "the captured 3.0s deadline"
        )


class TestInFlightGate:
    """Unit-level arithmetic for the shared gate, on a pinned clock (#867)."""

    def _gate(self, now: list[float]):
        from memtomem_stm.utils.anyio_shutdown import InFlightGate

        return InFlightGate(clock=lambda: now[0])

    def test_ceiling_is_remaining_time_not_a_fresh_full_timeout(self):
        from memtomem_stm.utils.anyio_shutdown import CLOSE_DRAIN_GRACE_SECONDS

        now = [1000.0]
        gate = self._gate(now)
        gate.enter(60.0)  # deadline at 1060
        now[0] = 1059.0  # 59s already spent
        # The remaining second plus grace — NOT another full 60s.
        assert gate.drain_ceiling(60.0) == pytest.approx(1.0 + CLOSE_DRAIN_GRACE_SECONDS)

    def test_a_departed_long_caller_does_not_leave_its_ceiling_behind(self):
        from memtomem_stm.utils.anyio_shutdown import CLOSE_DRAIN_GRACE_SECONDS

        now = [0.0]
        gate = self._gate(now)
        long_token = gate.enter(300.0)
        gate.enter(1.0)
        gate.leave(long_token)  # the 300s caller finished; the 1s one is stuck
        assert gate.drain_ceiling(0.0) == pytest.approx(1.0 + CLOSE_DRAIN_GRACE_SECONDS)

    def test_config_is_only_the_no_registration_fallback(self):
        from memtomem_stm.utils.anyio_shutdown import CLOSE_DRAIN_GRACE_SECONDS

        now = [0.0]
        gate = self._gate(now)
        gate.enter(1.0)
        # Raising the config after capture must not inflate the drain.
        assert gate.drain_ceiling(900.0) == pytest.approx(1.0 + CLOSE_DRAIN_GRACE_SECONDS)
        gate.leave(1)
        # With nothing registered, the config is the fallback.
        assert gate.drain_ceiling(900.0) == pytest.approx(900.0 + CLOSE_DRAIN_GRACE_SECONDS)

    def test_overdue_caller_owes_only_the_grace(self):
        from memtomem_stm.utils.anyio_shutdown import CLOSE_DRAIN_GRACE_SECONDS

        now = [0.0]
        gate = self._gate(now)
        gate.enter(1.0)
        now[0] = 100.0  # long past the deadline
        assert gate.drain_ceiling(0.0) == pytest.approx(CLOSE_DRAIN_GRACE_SECONDS)

    def test_idle_tracks_the_last_departure(self):
        now = [0.0]
        gate = self._gate(now)
        assert gate.idle.is_set()
        a = gate.enter(1.0)
        b = gate.enter(1.0)
        assert not gate.idle.is_set()
        gate.leave(a)
        assert not gate.idle.is_set(), "idle set while a caller is still registered"
        gate.leave(b)
        assert gate.idle.is_set()
        assert gate.in_flight == 0

    def test_unmatched_leave_is_inert(self):
        now = [0.0]
        gate = self._gate(now)
        token = gate.enter(1.0)
        gate.leave(token)
        gate.leave(token)  # double release must not unbalance the gate
        assert gate.in_flight == 0
        assert gate.idle.is_set()
