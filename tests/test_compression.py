"""Tests for compression strategies."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memtomem_stm.proxy.compression import (
    FieldExtractCompressor,
    HybridCompressor,
    LLMCompressor,
    NoopCompressor,
    SelectiveCompressor,
    TruncateCompressor,
)
from memtomem_stm.proxy.config import LLMCompressorConfig, LLMProvider


# ---------------------------------------------------------------------------
# NoopCompressor
# ---------------------------------------------------------------------------


class TestNoopCompressor:
    def test_passthrough(self):
        c = NoopCompressor()
        text = "hello world" * 100
        assert c.compress(text, max_chars=10) == text

    def test_empty(self):
        assert NoopCompressor().compress("", max_chars=100) == ""


# ---------------------------------------------------------------------------
# TruncateCompressor
# ---------------------------------------------------------------------------


class TestTruncateCompressor:
    def test_short_text_passthrough(self):
        c = TruncateCompressor()
        assert c.compress("short", max_chars=100) == "short"

    def test_truncates_at_sentence_boundary(self):
        # Budget comfortably holds the first sentence plus the truncation note.
        # (Pre-fix, max_chars=20 only "passed" because compress() overshot to
        # ~52 chars — the very overshoot the output-length invariant now forbids;
        # see test_truncate_output_invariants.py. A 48-char source has no budget
        # that fits sentence + full note under that invariant, so use a longer
        # source where the sentence-boundary contract is actually expressible.)
        text = "First sentence. " + "Filler sentence here. " * 20
        result = TruncateCompressor().compress(text, max_chars=80)
        assert "First sentence." in result
        assert "truncated" in result
        assert len(result) <= 80

    def test_truncation_metadata_includes_summary(self):
        text = "# Heading\n\nSome text.\n\n```code```\n\n- item1\n- item2"
        text = text * 20  # Make it long
        result = TruncateCompressor().compress(text, max_chars=100)
        assert "truncated" in result
        assert "original:" in result

    def test_empty_text(self):
        assert TruncateCompressor().compress("", max_chars=10) == ""

    def test_find_break_prefers_sentence_end(self):
        idx = TruncateCompressor._find_break("Hello world. More text here.", 15)
        assert idx == 12  # After "Hello world."


# ---------------------------------------------------------------------------
# SelectiveCompressor
# ---------------------------------------------------------------------------


class TestSelectiveCompressor:
    def test_short_text_passthrough(self):
        c = SelectiveCompressor()
        assert c.compress("short", max_chars=100) == "short"

    def test_json_dict_produces_toc(self):
        data = {"key1": "x" * 200, "key2": "y" * 200, "key3": "z" * 200}
        text = json.dumps(data)
        c = SelectiveCompressor()
        result = c.compress(text, max_chars=100)
        toc = json.loads(result)
        assert toc["type"] == "toc"
        assert "selection_key" in toc
        assert len(toc["entries"]) == 3
        assert toc["entries"][0]["key"] == "key1"

    def test_toc_includes_ttl_seconds_remaining(self):
        c = SelectiveCompressor(pending_ttl_seconds=120.0)
        data = {"key1": "x" * 200, "key2": "y" * 200}
        text = json.dumps(data)
        result = c.compress(text, max_chars=50)
        toc = json.loads(result)
        assert toc["ttl_seconds_remaining"] == 120

    def test_toc_ttl_default(self):
        c = SelectiveCompressor()
        data = {"key1": "x" * 200, "key2": "y" * 200}
        text = json.dumps(data)
        result = c.compress(text, max_chars=50)
        toc = json.loads(result)
        assert toc["ttl_seconds_remaining"] == 300  # default pending_ttl_seconds

    def test_json_array_produces_toc(self):
        data = [{"item": i, "content": "x" * 100} for i in range(5)]
        text = json.dumps(data)
        c = SelectiveCompressor()
        result = c.compress(text, max_chars=100)
        toc = json.loads(result)
        assert toc["type"] == "toc"
        assert len(toc["entries"]) == 5

    def test_markdown_parsed_by_headings(self):
        text = "# Section A\n\nContent A here.\n\n# Section B\n\nContent B here."
        text = text * 10
        c = SelectiveCompressor()
        result = c.compress(text, max_chars=50)
        toc = json.loads(result)
        assert toc["type"] == "toc"
        assert any(e["key"] == "Section A" for e in toc["entries"])

    def test_compress_full_toc_honors_max_chars(self):
        # compress_full_toc accepted max_chars but never forwarded it to
        # _store_and_build_toc, so the TOC was built with full-size previews
        # regardless of budget.
        data = {f"key{i}": f"word{i} " * 60 for i in range(10)}
        text = json.dumps(data)
        c = SelectiveCompressor()
        unbounded = c.compress_full_toc(text, max_chars=10**9)
        assert unbounded is not None
        budget = len(unbounded) - 200  # force the preview-shrink path
        result = c.compress_full_toc(text, max_chars=budget)
        assert result is not None
        assert len(result) <= budget
        toc = json.loads(result)
        assert len(toc["entries"]) == 10  # every section stays addressable

    def test_select_returns_requested_sections(self):
        data = {"alpha": "AAA" * 100, "beta": "BBB" * 100, "gamma": "CCC" * 100}
        text = json.dumps(data)
        c = SelectiveCompressor()
        result = c.compress(text, max_chars=100)
        toc = json.loads(result)
        key = toc["selection_key"]

        selected = c.select(key, ["alpha", "gamma"])
        assert "AAA" in selected
        assert "CCC" in selected
        assert "BBB" not in selected

    def test_select_expired_key(self):
        c = SelectiveCompressor(pending_ttl_seconds=0.0)
        data = {"a": "x" * 200, "b": "y" * 200}
        text = json.dumps(data)
        result = c.compress(text, max_chars=50)
        toc = json.loads(result)
        key = toc["selection_key"]

        import time

        # `evict_expired` uses ``(now - created_at) > ttl`` with ttl=0.0, so
        # any non-zero monotonic delta marks the entry expired. Windows
        # ``time.monotonic()`` falls back to ``GetTickCount64`` with ~15.6 ms
        # resolution; sleeping 10 ms could land inside a single tick, leave
        # ``now == created_at``, and skip eviction. Sleep ≥ 1 tick + headroom
        # so the delta is guaranteed positive.
        time.sleep(0.05)
        result = c.select(key, ["a"])
        assert "not found or expired" in result

    def test_select_unknown_sections(self):
        data = {"a": "x" * 200, "b": "y" * 200}
        text = json.dumps(data)
        c = SelectiveCompressor()
        result = c.compress(text, max_chars=50)
        toc = json.loads(result)
        key = toc["selection_key"]

        result = c.select(key, ["nonexistent"])
        assert "No matching sections" in result

    def test_eviction_removes_oldest(self):
        c = SelectiveCompressor(max_pending=2)
        for i in range(3):
            data = {f"k{i}": "x" * 200}
            c.compress(json.dumps(data), max_chars=50)
        assert len(c._store) <= 2

    def test_plain_text_parsed_by_paragraphs(self):
        text = "\n\n".join([f"Paragraph {i} content here." * 10 for i in range(5)])
        c = SelectiveCompressor()
        result = c.compress(text, max_chars=50)
        toc = json.loads(result)
        assert toc["type"] == "toc"


# ---------------------------------------------------------------------------
# FieldExtractCompressor
# ---------------------------------------------------------------------------


class TestFieldExtractCompressor:
    def test_json_dict_truncates_long_strings(self):
        data = {"short": "hi", "long": "word " * 100}
        text = json.dumps(data)
        c = FieldExtractCompressor()
        result = c.compress(text, max_chars=100)
        # Result is JSON with truncated long values
        assert "hi" in result
        assert "..." in result

    def test_json_array_shows_first_items(self):
        data = [{"name": f"item{i}", "value": "x" * 50} for i in range(10)]
        text = json.dumps(data)
        c = FieldExtractCompressor()
        # Budget large enough to keep the first preview readable as VALID JSON
        # (a sub-100 budget can only fit the array by shrinking item0's values
        # to "...", so the output is valid JSON but item0 is no longer literal).
        result = c.compress(text, max_chars=400)
        # Shows preview of first items, and the whole output parses as JSON.
        assert "item0" in result
        json.loads(result)

    def test_plain_text_head_tail(self):
        lines = [f"Line {i}" for i in range(100)]
        text = "\n".join(lines)
        c = FieldExtractCompressor()
        result = c.compress(text, max_chars=500)
        assert "lines omitted" in result

    def test_short_text_passthrough(self):
        c = FieldExtractCompressor()
        assert c.compress("short", max_chars=100) == "short"


# ---------------------------------------------------------------------------
# HybridCompressor
# ---------------------------------------------------------------------------


class TestHybridCompressor:
    def test_short_text_passthrough(self):
        c = HybridCompressor(head_chars=100)
        assert c.compress("short", max_chars=500) == "short"

    def test_preserves_head_content(self):
        head = "HEAD CONTENT. " * 50
        tail = "# Section\n\nTail content. " * 50
        text = head + tail
        c = HybridCompressor(head_chars=200)
        result = c.compress(text, max_chars=500)
        assert result.startswith("HEAD CONTENT")

    @staticmethod
    def _toc_tail(result: str) -> str | None:
        """Return the JSON TOC tail of a Hybrid result, or None.

        In TOC mode the separator always carries the ``Table of Contents:``
        label, but the tail itself is a JSON TOC envelope only when the
        SelectiveCompressor produced one; for a low-chunk tail it falls back to
        plain truncated text. A JSON envelope is the only tail that starts with
        ``{`` after the separator, so that is the reliable discriminator."""
        if "Table of Contents:" not in result:
            return None
        tail = result.split("Table of Contents:", 1)[1].strip()
        return tail if tail.startswith("{") else None

    def test_tail_contains_toc_or_truncation(self):
        text = "# Intro\n\nFirst part.\n\n" + "# Detail\n\nDetail content. " * 100
        c = HybridCompressor(head_chars=100)
        result = c.compress(text, max_chars=300)
        assert len(result) <= 300
        # The output is either a head + TOC envelope (when it fits) or, on
        # overshoot, a whole-text truncation. Each carries a recognizable
        # marker: the TOC separator ("Remaining content"), the position-based
        # truncation note ("truncated"), or the section-aware marker
        # ("(original: N chars)").
        assert "Remaining content" in result or "truncated" in result or "original:" in result
        # A JSON TOC tail, if present, must never be severed mid-object.
        tail = self._toc_tail(result)
        if tail is not None:
            json.loads(tail)

    def test_falls_back_to_truncate_when_budget_tight(self):
        # head_chars > text length triggers the `len(text) <= self._head_chars` passthrough
        # So use text longer than head_chars but with budget too tight for head+tail
        text = "A long sentence here. " * 500  # ~11000 chars
        c = HybridCompressor(head_chars=5000, min_head_chars=5000)
        result = c.compress(text, max_chars=100)
        assert "truncated" in result

    def test_toc_overshoot_stays_valid_json_within_budget(self):
        """A many-section document makes the budget-blind SelectiveCompressor TOC
        envelope far exceed its tail budget, so the assembled head + separator +
        TOC overshoots ``max_chars``. The old ``result[:max_chars]`` slice then
        severed the ``json.dumps``'d TOC mid-object, emitting INVALID JSON. The
        fix falls back to a whole-text truncation, so the output stays within
        budget and never leaves a half-cut JSON envelope."""
        c = HybridCompressor(head_chars=200)
        text = "\n\n".join(
            f"## Section number {i} heading\n\n" + ("lorem ipsum dolor sit amet " * 4)
            for i in range(40)
        )
        for budget in (600, 800, 1000, 1500):
            result = c.compress(text, max_chars=budget)
            assert len(result) <= budget, f"@{budget}: +{len(result) - budget}"
            # A surviving JSON TOC tail must be parseable, never a slice cut.
            tail = self._toc_tail(result)
            if tail is not None:
                json.loads(tail)


# ---------------------------------------------------------------------------
# LLMCompressor — last_fallback attribute
# ---------------------------------------------------------------------------


def _make_llm_compressor() -> LLMCompressor:
    cfg = LLMCompressorConfig(provider=LLMProvider.OLLAMA, base_url="http://localhost:11434")
    return LLMCompressor(cfg)


class TestLLMCompressorFallback:
    @pytest.mark.asyncio
    async def test_no_fallback_when_text_fits(self):
        comp = _make_llm_compressor()
        result = await comp.compress("short", max_chars=1000)
        assert result == "short"
        assert comp.last_fallback is None

    @pytest.mark.asyncio
    async def test_privacy_fallback(self):
        comp = _make_llm_compressor()
        text = "API_KEY=sk-secret-1234567890 " * 50
        with patch("memtomem_stm.proxy.privacy.contains_sensitive_content", return_value=True):
            result = await comp.compress(text, max_chars=100, privacy_patterns=["API_KEY"])
        assert comp.last_fallback == "privacy"
        assert len(result) < len(text)

    @pytest.mark.asyncio
    async def test_privacy_fallback_scans_full_payload_before_provider_call(self):
        comp = _make_llm_compressor()
        text = ("safe content " * 900) + "password: hunter2"
        assert len(text) > 10_000

        with patch.object(comp, "_call_api", new_callable=AsyncMock) as call_api:
            result = await comp.compress(text, max_chars=100, privacy_patterns=[r"password\s*:"])

        call_api.assert_not_called()
        assert comp.last_fallback == "privacy"
        assert len(result) < len(text)

    @pytest.mark.asyncio
    async def test_circuit_breaker_fallback(self):
        comp = _make_llm_compressor()
        # Trip the circuit breaker
        for _ in range(3):
            comp._cb.failure()
        assert comp._cb.is_open

        text = "Long document content. " * 50
        result = await comp.compress(text, max_chars=200)
        assert comp.last_fallback == "circuit_breaker"
        assert len(result) < len(text)

    @pytest.mark.asyncio
    async def test_llm_error_fallback(self):
        comp = _make_llm_compressor()
        text = "Long document content. " * 50
        with patch.object(
            comp, "_call_api", new_callable=AsyncMock, side_effect=RuntimeError("API down")
        ):
            result = await comp.compress(text, max_chars=200)
        assert comp.last_fallback == "llm_error"
        assert len(result) < len(text)

    @pytest.mark.asyncio
    async def test_fallback_resets_on_each_call(self):
        comp = _make_llm_compressor()
        # First call: trip circuit breaker
        for _ in range(3):
            comp._cb.failure()
        text = "Long document content. " * 50
        await comp.compress(text, max_chars=200)
        assert comp.last_fallback == "circuit_breaker"

        # Second call: text fits budget — no compression needed
        await comp.compress("short", max_chars=1000)
        assert comp.last_fallback is None

    @pytest.mark.asyncio
    async def test_overlength_llm_output_is_bounded(self):
        """The system prompt only ASKS the model to honor max_chars — a
        model that overshoots must not leak an over-budget payload through
        the success path. Every sync tier provably bounds its output to
        max_chars; the LLM success path was the sole exception (it returned
        the model string verbatim, and the manager's ratio guard checks
        only UNDER-retention, never the upper bound)."""
        comp = _make_llm_compressor()
        text = "Long document content. " * 50
        with patch.object(comp, "_call_api", new=AsyncMock(return_value="X" * 1000)):
            result = await comp.compress(text, max_chars=200)
        assert len(result) <= 200
        assert comp.last_fallback == "llm_overlength"

    @pytest.mark.asyncio
    async def test_overlength_does_not_count_as_breaker_failure(self):
        """Overshoot is a model-quality event, not an endpoint failure —
        the API responded fine, so it must not accumulate toward opening
        the circuit breaker the way timeouts and errors do."""
        comp = _make_llm_compressor()
        text = "Long document content. " * 50
        with patch.object(comp, "_call_api", new=AsyncMock(return_value="X" * 1000)):
            for _ in range(4):
                await comp.compress(text, max_chars=200)
        assert not comp._cb.is_open

    @pytest.mark.asyncio
    async def test_within_budget_llm_output_returned_verbatim(self):
        """The clamp must not touch a compliant summary — within-budget
        output is returned exactly as the model produced it."""
        comp = _make_llm_compressor()
        text = "Long document content. " * 50
        with patch.object(comp, "_call_api", new=AsyncMock(return_value="ok summary")):
            result = await comp.compress(text, max_chars=200)
        assert result == "ok summary"
        assert comp.last_fallback is None


# ---------------------------------------------------------------------------
# LLMCompressor — llm_timeout_seconds guards a slow/hung LLM endpoint (#207)
# ---------------------------------------------------------------------------


def _make_llm_compressor_with_timeout(timeout: float) -> LLMCompressor:
    cfg = LLMCompressorConfig(
        provider=LLMProvider.OLLAMA,
        base_url="http://localhost:11434",
        llm_timeout_seconds=timeout,
    )
    return LLMCompressor(cfg)


class TestLLMCompressorTimeout:
    """`llm_timeout_seconds` must fire when the LLM hangs past the budget,
    sending compression through the truncate fallback with
    ``last_fallback == "timeout"`` and a circuit-breaker failure recorded
    (so a persistently slow endpoint eventually trips CB)."""

    @pytest.mark.asyncio
    async def test_timeout_fires_and_falls_back_to_truncate(self):
        comp = _make_llm_compressor_with_timeout(0.05)
        text = "Long document content. " * 50

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(5.0)
            return "should never return"

        with patch.object(comp, "_call_api", new=AsyncMock(side_effect=_hang)):
            result = await comp.compress(text, max_chars=200)
        assert comp.last_fallback == "timeout"
        assert len(result) < len(text)
        # Truncate marker
        assert "truncated" in result or "original:" in result

    @pytest.mark.asyncio
    async def test_timeout_counts_as_circuit_breaker_failure(self):
        """Repeated timeouts must eventually open the breaker so subsequent
        calls short-circuit to the ``circuit_breaker`` fallback rather than
        continuing to pay the timeout latency on every call."""
        comp = _make_llm_compressor_with_timeout(0.02)
        text = "Long document content. " * 50

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(5.0)
            return "should never return"

        with patch.object(comp, "_call_api", new=AsyncMock(side_effect=_hang)):
            for _ in range(3):
                await comp.compress(text, max_chars=200)
        assert comp._cb.is_open

    @pytest.mark.asyncio
    async def test_fast_call_does_not_trigger_timeout(self):
        comp = _make_llm_compressor_with_timeout(10.0)
        text = "Long document content. " * 50

        async def _fast(*_args, **_kwargs):
            return "compressed summary"

        with patch.object(comp, "_call_api", new=AsyncMock(side_effect=_fast)):
            result = await comp.compress(text, max_chars=200)
        assert result == "compressed summary"
        assert comp.last_fallback is None

    def test_config_rejects_non_positive_timeout(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            LLMCompressorConfig(
                provider=LLMProvider.OLLAMA,
                base_url="http://localhost:11434",
                llm_timeout_seconds=0.0,
            )
        with pytest.raises(ValidationError):
            LLMCompressorConfig(
                provider=LLMProvider.OLLAMA,
                base_url="http://localhost:11434",
                llm_timeout_seconds=-1.0,
            )


# ---------------------------------------------------------------------------
# LLMCompressor — empty / malformed provider response guards (#67)
# ---------------------------------------------------------------------------


def _make_mock_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _patch_post(comp: LLMCompressor, payload: dict) -> AsyncMock:
    mock_post = AsyncMock(return_value=_make_mock_response(payload))
    comp._client.post = mock_post  # type: ignore[union-attr]
    return mock_post


class TestLLMCompressorEmptyResponseGuard:
    """Provider responses with empty / malformed payloads must raise a
    descriptive ``ValueError`` rather than ``IndexError``/``KeyError``,
    so the existing ``compress()`` fallback handler logs a useful message."""

    def _openai_comp(self) -> LLMCompressor:
        cfg = LLMCompressorConfig(provider=LLMProvider.OPENAI, api_key="test")
        return LLMCompressor(cfg)

    def _anthropic_comp(self) -> LLMCompressor:
        cfg = LLMCompressorConfig(provider=LLMProvider.ANTHROPIC, api_key="test")
        return LLMCompressor(cfg)

    def _ollama_comp(self) -> LLMCompressor:
        cfg = LLMCompressorConfig(provider=LLMProvider.OLLAMA, base_url="http://localhost:11434")
        return LLMCompressor(cfg)

    @pytest.mark.asyncio
    async def test_openai_empty_choices_raises(self):
        comp = self._openai_comp()
        _patch_post(comp, {"choices": []})
        with pytest.raises(ValueError, match="empty 'choices'"):
            await comp._openai("text", "system")

    @pytest.mark.asyncio
    async def test_openai_missing_message_content_raises(self):
        comp = self._openai_comp()
        _patch_post(comp, {"choices": [{"message": {}}]})
        with pytest.raises(ValueError, match="missing 'choices\\[0\\].message.content'"):
            await comp._openai("text", "system")

    @pytest.mark.asyncio
    async def test_openai_valid_response_succeeds(self):
        comp = self._openai_comp()
        _patch_post(comp, {"choices": [{"message": {"content": "hello"}}]})
        result = await comp._openai("text", "system")
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_anthropic_empty_content_raises(self):
        comp = self._anthropic_comp()
        _patch_post(comp, {"content": []})
        with pytest.raises(ValueError, match="empty 'content'"):
            await comp._anthropic("text", "system")

    @pytest.mark.asyncio
    async def test_anthropic_missing_text_raises(self):
        comp = self._anthropic_comp()
        _patch_post(comp, {"content": [{"type": "text"}]})
        with pytest.raises(ValueError, match="missing 'content\\[0\\].text'"):
            await comp._anthropic("text", "system")

    @pytest.mark.asyncio
    async def test_anthropic_valid_response_succeeds(self):
        comp = self._anthropic_comp()
        _patch_post(comp, {"content": [{"text": "summary"}]})
        result = await comp._anthropic("text", "system")
        assert result == "summary"

    @pytest.mark.asyncio
    async def test_ollama_missing_message_raises(self):
        comp = self._ollama_comp()
        _patch_post(comp, {})
        with pytest.raises(ValueError, match="missing 'message.content'"):
            await comp._ollama("text", "system")

    @pytest.mark.asyncio
    async def test_ollama_missing_content_raises(self):
        comp = self._ollama_comp()
        _patch_post(comp, {"message": {"role": "assistant"}})
        with pytest.raises(ValueError, match="missing 'message.content'"):
            await comp._ollama("text", "system")

    @pytest.mark.asyncio
    async def test_ollama_valid_response_succeeds(self):
        comp = self._ollama_comp()
        _patch_post(comp, {"message": {"content": "summary"}})
        result = await comp._ollama("text", "system")
        assert result == "summary"

    @pytest.mark.asyncio
    async def test_compress_falls_back_to_truncate_on_empty_response(self):
        """End-to-end: an empty provider response should trigger the
        existing ``llm_error`` fallback rather than crash with IndexError."""
        comp = self._openai_comp()
        _patch_post(comp, {"choices": []})
        text = "Long document content. " * 50
        result = await comp.compress(text, max_chars=200)
        assert comp.last_fallback == "llm_error"
        assert len(result) < len(text)


# ---------------------------------------------------------------------------
# LLMCompressor — close() vs in-flight compress() race
# ---------------------------------------------------------------------------


class TestLLMCompressorShutdown:
    """``close()`` must wait for any in-flight ``compress()`` before closing
    the httpx client. Otherwise the config-swap path in ProxyManager (which
    calls ``close()`` on the old instance) and the ``stop()`` teardown path
    can tear down the client while a concurrent caller is still awaiting
    ``self._client.post(...)``, yielding ``httpx.ClosedError`` or
    ``RuntimeError("stream has been closed")``.

    Sister pattern to #125 (extraction), #129 (mcp_client), #130 (proxy
    conn_stack) — the fix here uses the shared ``InFlightGate`` (per-caller
    registrations + an ``asyncio.Event``) so ``close()`` drains in-flight
    callers before ``_client.aclose()``.
    """

    import asyncio

    @pytest.mark.asyncio
    async def test_close_waits_for_in_flight_compress(self):
        """close() must block on in-flight compress() until it completes."""
        import asyncio

        cfg = LLMCompressorConfig(provider=LLMProvider.OPENAI, api_key="test")
        comp = LLMCompressor(cfg)
        compress_started = asyncio.Event()
        release_compress = asyncio.Event()

        async def slow_call_api(text: str, *, max_chars: int) -> str:
            compress_started.set()
            await release_compress.wait()
            return "summary"

        with patch.object(comp, "_call_api", new=slow_call_api):
            text = "x" * 500
            compress_task = asyncio.create_task(comp.compress(text, max_chars=100))
            await compress_started.wait()

            close_task = asyncio.create_task(comp.close())
            # Yield several times so close_task has a chance to observe that
            # a compress is in flight and park on the idle gate.
            for _ in range(10):
                await asyncio.sleep(0)

            assert not close_task.done(), (
                "close() returned before in-flight compress finished — race not fixed"
            )
            assert comp._client is not None, (
                "close() aclose'd the httpx client while compress was mid-call"
            )

            release_compress.set()
            result = await compress_task
            await close_task

            assert result == "summary"
            assert comp._client is None
            assert comp.last_fallback is None  # success path

    @pytest.mark.asyncio
    async def test_compress_after_close_falls_back_to_truncate(self):
        """Once close() has completed, subsequent compress() calls must
        degrade to truncate rather than use the aclose'd client."""
        cfg = LLMCompressorConfig(provider=LLMProvider.OPENAI, api_key="test")
        comp = LLMCompressor(cfg)
        await comp.close()

        text = "x" * 500
        result = await comp.compress(text, max_chars=100)
        assert len(result) <= len(text)
        assert comp.last_fallback == "closed"

    @pytest.mark.asyncio
    async def test_concurrent_compresses_drain_before_close(self):
        """Multiple concurrent in-flight compresses must all drain before
        close() proceeds — in-flight counter must track correctly."""
        import asyncio

        cfg = LLMCompressorConfig(provider=LLMProvider.OPENAI, api_key="test")
        comp = LLMCompressor(cfg)
        started = asyncio.Event()
        release = asyncio.Event()
        started_count = 0

        async def slow_call_api(text: str, *, max_chars: int) -> str:
            nonlocal started_count
            started_count += 1
            if started_count >= 3:
                started.set()
            await release.wait()
            return "summary"

        with patch.object(comp, "_call_api", new=slow_call_api):
            tasks = [asyncio.create_task(comp.compress("x" * 500, max_chars=100)) for _ in range(3)]
            await started.wait()

            close_task = asyncio.create_task(comp.close())
            for _ in range(10):
                await asyncio.sleep(0)
            assert not close_task.done()

            release.set()
            results = await asyncio.gather(*tasks)
            await close_task

            assert all(r == "summary" for r in results)
            assert comp._client is None

    @pytest.mark.asyncio
    async def test_close_drain_is_bounded_when_a_call_never_finishes(self):
        """#867: the drain wait itself must have a ceiling.

        In practice compress() is bounded by its own wait_for, but close()
        awaiting the gate with no timeout means any future unbounded path
        hangs shutdown forever. A stuck in-flight caller must cost the drain
        ceiling, not the process. The ceiling is asserted directly — the
        value handed to drain_or_warn — rather than inferred from elapsed
        wall clock, which cannot separate "waited the ceiling" from "a slow
        CI worker scheduled us late".
        """
        import asyncio

        from memtomem_stm.utils.anyio_shutdown import CLOSE_DRAIN_GRACE_SECONDS

        cfg = LLMCompressorConfig(
            provider=LLMProvider.OPENAI, api_key="test", llm_timeout_seconds=0.1
        )
        comp = LLMCompressor(cfg)
        # Register directly: going through compress() would rely on its own
        # wait_for, which is exactly the bound this test must not depend on.
        comp._gate.try_enter(cfg.llm_timeout_seconds)

        seen: dict[str, float] = {}

        async def recording_drain(idle, *, timeout, what):
            seen["timeout"] = timeout
            seen["what"] = what
            return False  # the ceiling fired

        with patch("memtomem_stm.proxy.compression.drain_or_warn", new=recording_drain):
            await asyncio.wait_for(comp.close(), timeout=5.0)

        assert seen["what"] == "LLMCompressor"
        # Remaining time on the captured deadline (~0.1s, minus the sliver
        # spent registering) plus the grace — not a re-read of the config and
        # not an unbounded wait.
        assert (
            CLOSE_DRAIN_GRACE_SECONDS
            < seen["timeout"]
            <= cfg.llm_timeout_seconds + CLOSE_DRAIN_GRACE_SECONDS
        ), seen
        assert comp._client is None, (
            "close() must still tear the client down after a timed-out drain"
        )

    @pytest.mark.asyncio
    async def test_close_drain_actually_waits_and_gives_up(self, caplog):
        """The real drain_or_warn must reach its ceiling and return on its own.

        The give-up warning is the observable that separates "waited, then
        proceeded" from "never waited" — elapsed wall clock cannot, since a
        delayed scheduler can hand an immediate return any duration.
        """
        import asyncio

        cfg = LLMCompressorConfig(
            provider=LLMProvider.OPENAI, api_key="test", llm_timeout_seconds=0.05
        )
        comp = LLMCompressor(cfg)
        comp._gate.try_enter(cfg.llm_timeout_seconds)

        with caplog.at_level("WARNING", logger="memtomem_stm.utils.anyio_shutdown"):
            await asyncio.wait_for(comp.close(), timeout=30.0)

        assert any("did not drain" in r.getMessage() for r in caplog.records), caplog.text
        assert comp._client is None
