"""Tests for SurfacingEngine — the core proactive memory surfacing orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4


from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine


# ── Helpers ──────────────────────────────────────────────────────────────


@dataclass
class FakeChunkMeta:
    source_file: Path = Path("/notes/test.md")
    namespace: str = "default"


@dataclass
class FakeChunk:
    id: str = ""
    content: str = "some memory content"
    metadata: FakeChunkMeta | None = None

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid4())
        if self.metadata is None:
            self.metadata = FakeChunkMeta()


@dataclass
class FakeSearchResult:
    chunk: FakeChunk
    score: float
    rank: int = 1


def _make_config(**overrides) -> SurfacingConfig:
    defaults = {
        "enabled": True,
        "min_response_chars": 10,
        "timeout_seconds": 5.0,
        "min_score": 0.02,
        "max_results": 3,
        "cooldown_seconds": 0.0,
        "max_surfacings_per_minute": 1000,
        "auto_tune_enabled": False,
        "include_session_context": False,
        "fire_webhook": False,
        "cache_ttl_seconds": 60.0,
    }
    defaults.update(overrides)
    return SurfacingConfig(**defaults)


def _make_mcp_adapter(results: list[FakeSearchResult] | None = None):
    """Build a mock McpClientSearchAdapter that returns the given results."""
    adapter = AsyncMock()
    adapter.search = AsyncMock(return_value=(results or [], {}))
    return adapter


LONG_RESPONSE = "x" * 200  # above min_response_chars=10

# Arguments that produce a valid query for ContextExtractor
VALID_ARGS = {"path": "src/app.py", "_context_query": "Flask web framework architecture"}


# ── Tests ────────────────────────────────────────────────────────────────


class TestSurfacingBasic:
    async def test_normal_surfacing_injects_memories(self):
        results = [FakeSearchResult(chunk=FakeChunk(content="Flask chosen"), score=0.5)]
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter(results),
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "Relevant Memories" in output
        assert "Flask chosen" in output

    async def test_empty_results_returns_original(self):
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter([]),
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert output == LONG_RESPONSE

    async def test_disabled_returns_original(self):
        engine = SurfacingEngine(
            config=_make_config(enabled=False),
            mcp_adapter=_make_mcp_adapter([FakeSearchResult(FakeChunk(), 0.9)]),
        )
        output = await engine.surface("gh", "tool", {}, LONG_RESPONSE)
        assert output == LONG_RESPONSE

class TestSurfacingGating:
    async def test_short_response_skipped(self):
        engine = SurfacingEngine(
            config=_make_config(min_response_chars=1000),
            mcp_adapter=_make_mcp_adapter([FakeSearchResult(FakeChunk(), 0.9)]),
        )
        output = await engine.surface("gh", "tool", {}, "short")
        assert output == "short"

    async def test_write_tool_skipped(self):
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter([FakeSearchResult(FakeChunk(), 0.9)]),
        )
        output = await engine.surface("fs", "write_file", {"path": "x", "_context_query": "test"}, LONG_RESPONSE)
        assert output == LONG_RESPONSE

    async def test_delete_tool_skipped(self):
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter([FakeSearchResult(FakeChunk(), 0.9)]),
        )
        output = await engine.surface("fs", "delete_file", {"path": "x", "_context_query": "test"}, LONG_RESPONSE)
        assert output == LONG_RESPONSE


class TestSurfacingScoreFilter:
    async def test_below_min_score_filtered(self):
        results = [FakeSearchResult(chunk=FakeChunk(), score=0.01)]
        engine = SurfacingEngine(
            config=_make_config(min_score=0.02),
            mcp_adapter=_make_mcp_adapter(results),
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert output == LONG_RESPONSE  # filtered, no injection

    async def test_at_min_score_included(self):
        results = [FakeSearchResult(chunk=FakeChunk(content="exactly at threshold"), score=0.02)]
        engine = SurfacingEngine(
            config=_make_config(min_score=0.02),
            mcp_adapter=_make_mcp_adapter(results),
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "exactly at threshold" in output

    async def test_max_results_limit(self):
        results = [
            FakeSearchResult(chunk=FakeChunk(content=f"result-{i}"), score=0.5 - i * 0.01)
            for i in range(10)
        ]
        engine = SurfacingEngine(
            config=_make_config(max_results=2),
            mcp_adapter=_make_mcp_adapter(results),
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "result-0" in output
        assert "result-1" in output
        assert "result-5" not in output


class TestSurfacingCircuitBreaker:
    async def test_circuit_breaker_opens_after_failures(self):
        failing_adapter = AsyncMock()
        failing_adapter.search = AsyncMock(side_effect=RuntimeError("boom"))

        engine = SurfacingEngine(
            config=_make_config(circuit_max_failures=2, circuit_reset_seconds=60),
            mcp_adapter=failing_adapter,
        )

        # First 2 failures should still return original (caught by except)
        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        await engine.surface("gh", "read_file", {"path": "y"}, LONG_RESPONSE)

        # Circuit should now be open — adapter.search NOT called
        failing_adapter.search.reset_mock()
        output = await engine.surface("gh", "read_file", {"path": "z"}, LONG_RESPONSE)
        assert output == LONG_RESPONSE
        failing_adapter.search.assert_not_called()


class TestSurfacingTimeout:
    async def test_timeout_returns_original(self):
        async def slow_search(*args, **kwargs):
            await asyncio.sleep(10)
            return [], {}

        adapter = AsyncMock()
        adapter.search = slow_search

        engine = SurfacingEngine(
            config=_make_config(timeout_seconds=0.1),
            mcp_adapter=adapter,
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert output == LONG_RESPONSE


class TestSessionDedup:
    """Verify same memory isn't surfaced twice in one session."""

    async def test_same_memory_not_repeated(self):
        """Second surfacing call should skip already-seen memories."""
        chunk1 = FakeChunk(content="memory A")
        chunk2 = FakeChunk(content="memory B")
        results = [
            FakeSearchResult(chunk=chunk1, score=0.5),
            FakeSearchResult(chunk=chunk2, score=0.4),
        ]
        engine = SurfacingEngine(
            config=_make_config(cooldown_seconds=0),
            mcp_adapter=_make_mcp_adapter(results),
        )

        out1 = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "memory A" in out1
        assert "memory B" in out1

        # Clear cache to force re-search, but dedup should filter
        engine._cache.clear()
        out2 = await engine.surface(
            "s", "read_file",
            {"path": "/other", "_context_query": "different query for search"},
            LONG_RESPONSE,
        )
        # Both memories already surfaced → should not appear again
        assert "memory A" not in out2
        assert "memory B" not in out2


class TestSurfacingCache:
    async def test_cache_hit_skips_search(self):
        results = [FakeSearchResult(chunk=FakeChunk(content="cached memory"), score=0.5)]
        adapter = _make_mcp_adapter(results)

        engine = SurfacingEngine(
            config=_make_config(cooldown_seconds=0),
            mcp_adapter=adapter,
        )

        # First call — searches
        out1 = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "cached memory" in out1
        assert adapter.search.call_count == 1

        # Second call — cache hit, no search
        out2 = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "cached memory" in out2
        assert adapter.search.call_count == 1  # not called again


class TestSessionContextInjection:
    """Verify include_session_context wires the scratchpad through the MCP adapter."""

    async def test_scratch_items_injected_when_enabled(self):
        results = [FakeSearchResult(chunk=FakeChunk(content="LTM hit content"), score=0.5)]
        adapter = _make_mcp_adapter(results)
        adapter.scratch_list = AsyncMock(
            return_value=[{"key": "current_task", "value": "running follow-up 4"}]
        )
        engine = SurfacingEngine(
            config=_make_config(include_session_context=True),
            mcp_adapter=adapter,
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "LTM hit content" in output
        assert "Working Memory" in output
        assert "current_task" in output
        adapter.scratch_list.assert_awaited_once()

    async def test_scratch_not_fetched_when_disabled(self):
        results = [FakeSearchResult(chunk=FakeChunk(content="LTM hit content"), score=0.5)]
        adapter = _make_mcp_adapter(results)
        adapter.scratch_list = AsyncMock(return_value=[])
        engine = SurfacingEngine(
            config=_make_config(include_session_context=False),
            mcp_adapter=adapter,
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "LTM hit content" in output
        assert "Working Memory" not in output
        adapter.scratch_list.assert_not_called()

    async def test_scratch_failure_silent_fallback(self):
        """LTM injection still happens even if scratch_list raises."""
        results = [FakeSearchResult(chunk=FakeChunk(content="LTM hit content"), score=0.5)]
        adapter = _make_mcp_adapter(results)
        adapter.scratch_list = AsyncMock(side_effect=RuntimeError("scratch broke"))
        engine = SurfacingEngine(
            config=_make_config(include_session_context=True),
            mcp_adapter=adapter,
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "LTM hit content" in output
        assert "Working Memory" not in output
        adapter.scratch_list.assert_awaited_once()

    async def test_empty_scratch_list_omits_section(self):
        results = [FakeSearchResult(chunk=FakeChunk(content="LTM hit content"), score=0.5)]
        adapter = _make_mcp_adapter(results)
        adapter.scratch_list = AsyncMock(return_value=[])
        engine = SurfacingEngine(
            config=_make_config(include_session_context=True),
            mcp_adapter=adapter,
        )
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "LTM hit content" in output
        assert "Working Memory" not in output


class TestFeedbackBoost:
    """Verify handle_feedback boosts access_count via the MCP adapter on 'helpful'."""

    def _make_tracker(self, memory_ids: list[str]):
        """Build a fake FeedbackTracker the engine can call."""
        tracker = MagicMock()
        tracker.record_feedback = MagicMock(return_value="Feedback recorded: helpful")
        tracker.store = MagicMock()
        tracker.store.get_seen_ids = MagicMock(return_value=set())
        tracker.store.get_memory_ids_for_surfacing = MagicMock(return_value=list(memory_ids))
        return tracker

    async def test_helpful_with_explicit_memory_id_boosts_only_that_id(self):
        adapter = _make_mcp_adapter([])
        adapter.increment_access = AsyncMock()
        tracker = self._make_tracker(["mid-A", "mid-B"])
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        result = await engine.handle_feedback("sid-1", "helpful", memory_id="mid-X")

        assert "Feedback recorded" in result
        adapter.increment_access.assert_awaited_once_with(["mid-X"])
        tracker.store.get_memory_ids_for_surfacing.assert_not_called()
        assert "sid-1" in engine._boosted_event_ids

    async def test_helpful_without_memory_id_boosts_all_event_ids(self):
        adapter = _make_mcp_adapter([])
        adapter.increment_access = AsyncMock()
        tracker = self._make_tracker(["mid-A", "mid-B", "mid-C"])
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        await engine.handle_feedback("sid-2", "helpful")

        tracker.store.get_memory_ids_for_surfacing.assert_called_once_with("sid-2")
        adapter.increment_access.assert_awaited_once_with(["mid-A", "mid-B", "mid-C"])

    async def test_non_helpful_ratings_skip_boost(self):
        adapter = _make_mcp_adapter([])
        adapter.increment_access = AsyncMock()
        tracker = self._make_tracker(["mid-A"])
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        await engine.handle_feedback("sid-3", "not_relevant", memory_id="mid-A")
        await engine.handle_feedback("sid-3", "already_known", memory_id="mid-A")

        adapter.increment_access.assert_not_called()

    async def test_boost_guard_caps_per_event(self):
        """Repeat 'helpful' for the same surfacing_id only triggers one boost."""
        adapter = _make_mcp_adapter([])
        adapter.increment_access = AsyncMock()
        tracker = self._make_tracker(["mid-A"])
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        await engine.handle_feedback("sid-4", "helpful", memory_id="mid-A")
        await engine.handle_feedback("sid-4", "helpful", memory_id="mid-A")
        await engine.handle_feedback("sid-4", "helpful", memory_id="mid-A")

        assert adapter.increment_access.await_count == 1

    async def test_boost_failure_does_not_break_feedback(self):
        """If increment_access raises, record_feedback still returns success."""
        adapter = _make_mcp_adapter([])
        adapter.increment_access = AsyncMock(side_effect=RuntimeError("MCP gone"))
        tracker = self._make_tracker(["mid-A"])
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        result = await engine.handle_feedback("sid-5", "helpful", memory_id="mid-A")

        assert "Feedback recorded" in result
        adapter.increment_access.assert_awaited_once()
        # The boost failed mid-flight — guard set should NOT mark this event
        # so a future call can retry the boost.
        assert "sid-5" not in engine._boosted_event_ids

    async def test_no_boost_when_event_has_no_memories(self):
        """When the surfacing event has no memories, skip the call entirely."""
        adapter = _make_mcp_adapter([])
        adapter.increment_access = AsyncMock()
        tracker = self._make_tracker([])  # store returns []
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        await engine.handle_feedback("sid-6", "helpful")

        adapter.increment_access.assert_not_called()

    async def test_no_tracker_returns_disabled_message(self):
        adapter = _make_mcp_adapter([])
        adapter.increment_access = AsyncMock()
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=None,
        )

        result = await engine.handle_feedback("sid-7", "helpful")

        assert "not enabled" in result
        adapter.increment_access.assert_not_called()
