"""Tests for SurfacingEngine — the core proactive memory surfacing orchestrator."""

from __future__ import annotations

import asyncio
import time
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
        output = await engine.surface(
            "fs", "write_file", {"path": "x", "_context_query": "test"}, LONG_RESPONSE
        )
        assert output == LONG_RESPONSE

    async def test_delete_tool_skipped(self):
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter([FakeSearchResult(FakeChunk(), 0.9)]),
        )
        output = await engine.surface(
            "fs", "delete_file", {"path": "x", "_context_query": "test"}, LONG_RESPONSE
        )
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
            "s",
            "read_file",
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


class TestSurfacingCacheStampede:
    """Two concurrent ``surface()`` calls for the same ``{server}/{tool}/{query}``
    cache key should trigger a single LTM search, not one per caller. The
    cache exists specifically to avoid redundant LTM searches; under the
    current check-then-await-then-set pattern (engine.py:209 get, L248 await
    search, L267 set), the ``await`` window lets both coroutines observe a
    miss before either writes back, so both hit LTM."""

    async def test_concurrent_identical_queries_share_single_search(self):
        """Three observable symptoms of the stampede, in order of severity:

        1. Duplicate LTM search (wasted upstream load).
        2. Second caller fails to receive the surfaced memory because the
           session dedup (``_surfaced_ids``) was claimed by the first caller
           between the two searches completing.
        3. Cache poisoning: the second caller's ``cache.set(key, [])``
           overwrites the first caller's ``cache.set(key, [memory])``, so
           every subsequent call for the same query inside the TTL window
           sees an empty-hit and skips surfacing entirely.

        Of these, (3) is the most impactful — a transient race permanently
        (for the TTL) suppresses surfacing for a query across future
        requests."""
        chunk = FakeChunk(id="mem-shared", content="shared result")
        results = [FakeSearchResult(chunk=chunk, score=0.5)]
        adapter = AsyncMock()

        async def slow_search(**_kwargs):
            await asyncio.sleep(0.01)
            return (results, {})

        adapter.search = AsyncMock(side_effect=slow_search)

        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
        )

        out_a, out_b = await asyncio.gather(
            engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE),
            engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE),
        )

        # Symptom 1: duplicate LTM search
        assert adapter.search.call_count == 1, (
            f"Stampede: {adapter.search.call_count} LTM searches for the "
            "same {server}/{tool}/{query} cache key (expected 1)"
        )

        # Symptom 2: both callers should see the memory.
        assert "shared result" in out_a
        assert "shared result" in out_b, (
            "Second concurrent caller did not receive the shared memory — "
            "the in-flight first caller claimed the _surfaced_ids slot "
            "before the second caller's filter ran"
        )

        # Symptom 3: cache entry reflects the populated result, not the
        # poisoned empty list. A subsequent call for the same query must
        # still hit the memory, not bypass surfacing on an empty-hit.
        cache_key = f"gh/read_file/{VALID_ARGS['_context_query']}"
        cached = engine._cache.get(cache_key)
        assert cached, (
            "Cache poisoned with empty list — stampede's losing writer "
            "overwrote the winning writer's populated cache entry"
        )
        assert any(r.chunk.id == "mem-shared" for r in cached), (
            "Cache entry exists but is missing the shared memory"
        )


class TestRelevanceGateConcurrency:
    """``RelevanceGate.should_surface`` is called at ``surface()`` entry and
    ``record_surfacing`` is called later inside ``_do_surface_miss`` (after
    the LTM search ``await``). Concurrent ``surface()`` calls can all pass
    ``should_surface`` (rate limit + cooldown check) before any of them
    reaches ``record_surfacing``, so the configured rate limit is bypassed
    by up to the concurrency level."""

    async def test_concurrent_surface_calls_bypass_rate_limit(self):
        # Rate-limit config = 1 surfacing per minute. Under the race,
        # N concurrent calls all observe an empty ``_surfacing_timestamps``
        # before any writes back, so all N pass the gate.
        adapter = AsyncMock()

        async def slow_search(**_kwargs):
            await asyncio.sleep(0.01)
            return ([FakeSearchResult(chunk=FakeChunk(content="hit"), score=0.5)], {})

        adapter.search = AsyncMock(side_effect=slow_search)

        engine = SurfacingEngine(
            config=_make_config(max_surfacings_per_minute=1),
            mcp_adapter=adapter,
        )

        # 5 distinct cache keys so the cache stampede fix doesn't mask the
        # race (each query has its own ``_do_surface`` miss path).
        await asyncio.gather(
            *(
                engine.surface(
                    "gh",
                    "read_file",
                    {"path": f"src/f{i}.py", "_context_query": f"query {i}"},
                    LONG_RESPONSE,
                )
                for i in range(5)
            )
        )

        assert adapter.search.call_count == 1, (
            "Rate limit bypassed under concurrency: "
            f"{adapter.search.call_count} LTM searches fired with "
            "max_surfacings_per_minute=1 — all should_surface checks "
            "passed before any record_surfacing wrote back"
        )


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


class TestCachedSurfacingFeedback:
    """Cached surfacing hits must record a surfacing event so that agent
    feedback submitted with the rendered surfacing_id can be resolved by
    the feedback store."""

    async def test_cache_hit_records_surfacing_event(self):
        """record_surfacing must be called for both the miss AND the cache hit."""
        results = [FakeSearchResult(chunk=FakeChunk(id="m1", content="mem"), score=0.7)]
        adapter = _make_mcp_adapter(results)
        tracker = MagicMock()
        tracker.record_surfacing = MagicMock()
        tracker.store = MagicMock()
        tracker.store.mark_surfaced = MagicMock()
        tracker.store.get_seen_ids = MagicMock(return_value=[])

        engine = SurfacingEngine(
            config=_make_config(cooldown_seconds=0),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        # First call — cache miss
        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert tracker.record_surfacing.call_count == 1

        # Second call — cache hit
        await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert tracker.record_surfacing.call_count == 2

        # Both calls should produce distinct surfacing_ids
        id1 = tracker.record_surfacing.call_args_list[0].kwargs["surfacing_id"]
        id2 = tracker.record_surfacing.call_args_list[1].kwargs["surfacing_id"]
        assert id1 != id2

    async def test_cache_hit_feedback_resolvable(self):
        """End-to-end: feedback on a cached surfacing_id must succeed."""
        from memtomem_stm.surfacing.feedback import FeedbackTracker

        results = [FakeSearchResult(chunk=FakeChunk(id="m2", content="cached mem"), score=0.6)]
        adapter = _make_mcp_adapter(results)

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fb.db"
            tracker = FeedbackTracker(config=_make_config(), db_path=db_path)

            engine = SurfacingEngine(
                config=_make_config(cooldown_seconds=0),
                mcp_adapter=adapter,
                feedback_tracker=tracker,
            )

            # Miss → populates cache
            out1 = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
            assert "Surfacing ID:" in out1

            # Cache hit → new surfacing_id recorded
            out2 = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
            assert "Surfacing ID:" in out2

            # Extract the surfacing_id from the second (cached) output
            import re

            match = re.search(r"Surfacing ID: (\w+)", out2)
            assert match, "surfacing_id not found in cached output"
            cached_sid = match.group(1)

            # Feedback for the cached surfacing_id must succeed
            result = await engine.handle_feedback(cached_sid, "helpful")
            assert "Error" not in result

            tracker.close()

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

    async def test_concurrent_helpful_for_same_surfacing_id_boosts_once(self):
        """Two concurrent ``handle_feedback`` calls for the same ``surfacing_id``
        must fire a single ``increment_access`` RPC — the class docstring and
        ``_boosted_event_ids`` guard promise "at most one per surfacing event"
        even under concurrency. Without claiming the guard before the await,
        both coroutines observe an empty guard, both await ``increment_access``,
        and the boost is double-counted in core."""
        adapter = _make_mcp_adapter([])

        async def slow_increment(_ids):
            await asyncio.sleep(0.01)

        adapter.increment_access = AsyncMock(side_effect=slow_increment)
        tracker = self._make_tracker(["mid-A"])
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        await asyncio.gather(
            engine.handle_feedback("sid-concurrent", "helpful", memory_id="mid-A"),
            engine.handle_feedback("sid-concurrent", "helpful", memory_id="mid-A"),
        )

        assert adapter.increment_access.await_count == 1, (
            "Dedup guard violated under concurrency: "
            f"increment_access awaited {adapter.increment_access.await_count} times"
        )

    async def test_boosted_event_ids_fifo_cap_evicts_oldest(self):
        """When ``_boosted_event_ids`` exceeds its cap, oldest entries evict first."""
        adapter = _make_mcp_adapter([])
        adapter.increment_access = AsyncMock()
        tracker = self._make_tracker(["mid-A"])
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )
        engine._boosted_event_ids_max = 10  # shrink for test speed

        for i in range(15):
            await engine.handle_feedback(f"sid-cap-{i}", "helpful", memory_id="mid-A")

        # Overflow triggers bulk prune to half the cap (~5 entries remain).
        assert len(engine._boosted_event_ids) <= 10
        # Oldest (first-inserted) entries should be gone; newest retained.
        assert "sid-cap-0" not in engine._boosted_event_ids
        assert "sid-cap-14" in engine._boosted_event_ids

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


class TestCacheInvalidationOnNegativeFeedback:
    """Negative feedback (``not_relevant`` / ``already_known``) must populate
    ``_invalidated_ids`` so subsequent cache hits for the same
    ``server/tool/query`` filter out the rejected memory. Without this,
    repeat queries inside the ``SurfacingCache`` TTL window keep resurfacing
    memories the agent just rejected (issue #146)."""

    def _make_tracker(
        self,
        server: str = "gh",
        tool: str = "read_file",
        memory_ids: list[str] | None = None,
        rating_response: str = "Feedback recorded: not_relevant",
    ):
        tracker = MagicMock()
        tracker.record_feedback = MagicMock(return_value=rating_response)
        tracker.store = MagicMock()
        tracker.store.get_seen_ids = MagicMock(return_value=set())
        tracker.store.get_surfacing_event = MagicMock(
            return_value={
                "server": server,
                "tool": tool,
                "memory_ids": list(memory_ids or []),
            }
        )
        tracker.store.mark_surfaced = MagicMock()
        return tracker

    async def test_not_relevant_adds_tuple_to_invalidation_set(self):
        adapter = _make_mcp_adapter([])
        tracker = self._make_tracker(memory_ids=["mid-A"])
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        await engine.handle_feedback("sid-1", "not_relevant", memory_id="mid-A")

        assert ("gh", "read_file", "mid-A") in engine._invalidated_ids

    async def test_already_known_adds_tuple_to_invalidation_set(self):
        adapter = _make_mcp_adapter([])
        tracker = self._make_tracker(
            memory_ids=["mid-A"],
            rating_response="Feedback recorded: already_known",
        )
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        await engine.handle_feedback("sid-1", "already_known", memory_id="mid-A")

        assert ("gh", "read_file", "mid-A") in engine._invalidated_ids

    async def test_helpful_does_not_add_to_invalidation_set(self):
        adapter = _make_mcp_adapter([])
        adapter.increment_access = AsyncMock()
        tracker = self._make_tracker(
            memory_ids=["mid-A"],
            rating_response="Feedback recorded: helpful",
        )
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        await engine.handle_feedback("sid-1", "helpful", memory_id="mid-A")

        assert engine._invalidated_ids == {}

    async def test_blanket_invalidation_adds_all_event_memory_ids(self):
        """handle_feedback with no memory_id invalidates every memory in the event."""
        adapter = _make_mcp_adapter([])
        tracker = self._make_tracker(memory_ids=["mid-A", "mid-B", "mid-C"])
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        await engine.handle_feedback("sid-1", "not_relevant")

        assert ("gh", "read_file", "mid-A") in engine._invalidated_ids
        assert ("gh", "read_file", "mid-B") in engine._invalidated_ids
        assert ("gh", "read_file", "mid-C") in engine._invalidated_ids

    async def test_cache_hit_filters_invalidated_memory_from_output(self):
        """After rating a memory not_relevant, the next cache hit for the same
        query returns the cached results minus that memory. Uses a real
        ``FeedbackTracker`` so the end-to-end event lookup runs."""
        from memtomem_stm.surfacing.feedback import FeedbackTracker

        chunk_a = FakeChunk(id="mid-A", content="apple memory")
        chunk_b = FakeChunk(id="mid-B", content="banana memory")
        results = [
            FakeSearchResult(chunk=chunk_a, score=0.5),
            FakeSearchResult(chunk=chunk_b, score=0.4),
        ]
        adapter = _make_mcp_adapter(results)

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fb.db"
            tracker = FeedbackTracker(config=_make_config(), db_path=db_path)
            engine = SurfacingEngine(
                config=_make_config(cooldown_seconds=0),
                mcp_adapter=adapter,
                feedback_tracker=tracker,
            )

            # First surface — populates cache with [mid-A, mid-B].
            out1 = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
            assert "apple memory" in out1
            assert "banana memory" in out1

            import re

            match = re.search(r"Surfacing ID: (\w+)", out1)
            assert match, "surfacing_id not found in first output"
            first_sid = match.group(1)

            # Rate mid-A as not_relevant.
            await engine.handle_feedback(first_sid, "not_relevant", memory_id="mid-A")

            # Second surface hits the cache; mid-A must be filtered out.
            out2 = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
            assert "apple memory" not in out2
            assert "banana memory" in out2

            tracker.close()

    async def test_cache_hit_filtered_to_empty_passes_response_through(self):
        """If every cached memory is invalidated, ``_render_cached`` returns
        the original response unchanged (no injection, no orphan event)."""
        from memtomem_stm.surfacing.feedback import FeedbackTracker

        chunk_a = FakeChunk(id="mid-A", content="apple memory")
        results = [FakeSearchResult(chunk=chunk_a, score=0.5)]
        adapter = _make_mcp_adapter(results)

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fb.db"
            tracker = FeedbackTracker(config=_make_config(), db_path=db_path)
            engine = SurfacingEngine(
                config=_make_config(cooldown_seconds=0),
                mcp_adapter=adapter,
                feedback_tracker=tracker,
            )

            out1 = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
            import re

            match = re.search(r"Surfacing ID: (\w+)", out1)
            assert match
            first_sid = match.group(1)

            await engine.handle_feedback(first_sid, "not_relevant", memory_id="mid-A")

            out2 = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
            # Fully filtered — response unchanged (no surfacing block injected).
            assert out2 == LONG_RESPONSE
            assert "apple memory" not in out2

            tracker.close()

    async def test_invalidation_keyed_by_server_tool_not_global(self):
        """Invalidating (srv-A, tool-X, mid) must not affect (srv-B, tool-X, mid)
        or (srv-A, tool-Y, mid) — the key is the full triple."""
        adapter = _make_mcp_adapter([])
        tracker = self._make_tracker(server="srv-A", tool="tool-X", memory_ids=["mid-1"])
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        await engine.handle_feedback("sid-1", "not_relevant", memory_id="mid-1")

        assert ("srv-A", "tool-X", "mid-1") in engine._invalidated_ids
        assert ("srv-B", "tool-X", "mid-1") not in engine._invalidated_ids
        assert ("srv-A", "tool-Y", "mid-1") not in engine._invalidated_ids

    async def test_invalidated_ids_fifo_cap_evicts_oldest(self):
        """When ``_invalidated_ids`` exceeds its cap, oldest entries evict first."""
        adapter = _make_mcp_adapter([])
        tracker = self._make_tracker(memory_ids=["mid-X"])
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )
        engine._invalidated_ids_max = 10  # shrink for test speed

        for i in range(15):
            tracker.store.get_surfacing_event = MagicMock(
                return_value={
                    "server": "gh",
                    "tool": "read_file",
                    "memory_ids": [f"mid-{i}"],
                }
            )
            await engine.handle_feedback(f"sid-{i}", "not_relevant", memory_id=f"mid-{i}")

        # Overflow triggers bulk prune to half the cap.
        assert len(engine._invalidated_ids) <= 10
        # Oldest entries evicted; newest retained.
        assert ("gh", "read_file", "mid-0") not in engine._invalidated_ids
        assert ("gh", "read_file", "mid-14") in engine._invalidated_ids

    async def test_missing_event_skips_invalidation(self):
        """If ``get_surfacing_event`` returns None, handle_feedback does not crash
        and does not pollute ``_invalidated_ids`` with a phantom tuple."""
        adapter = _make_mcp_adapter([])
        tracker = MagicMock()
        tracker.record_feedback = MagicMock(
            return_value="Error: surfacing event 'sid-ghost' not found"
        )
        tracker.store = MagicMock()
        tracker.store.get_seen_ids = MagicMock(return_value=set())
        tracker.store.get_surfacing_event = MagicMock(return_value=None)
        tracker.store.mark_surfaced = MagicMock()

        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        await engine.handle_feedback("sid-ghost", "not_relevant", memory_id="mid-A")

        assert engine._invalidated_ids == {}


class TestConcurrentSurfacedIdsDedup:
    """Dedup invariant: each memory surfaced at most once per session, even
    under concurrency (engine.py:62 / L256 / docstring).

    Before the fix, the in-memory write at ``_surfaced_ids`` happened AFTER
    the ``scratch_list`` await, opening an interleaving window where two
    concurrent ``_do_surface`` calls could both build ``relevant`` with the
    same memory and both return responses containing it."""

    def _make_tracker(self):
        tracker = MagicMock()
        tracker.record_feedback = MagicMock(return_value="ok")
        tracker.store = MagicMock()
        tracker.store.get_seen_ids = MagicMock(return_value=set())
        tracker.store.mark_surfaced = MagicMock()
        tracker.record_surfacing = MagicMock()
        return tracker

    async def test_concurrent_surface_same_memory_dedups(self):
        shared_chunk = FakeChunk(id="mem-shared", content="the shared memory content")
        results = [FakeSearchResult(chunk=shared_chunk, score=0.9)]
        adapter = _make_mcp_adapter(results)

        async def slow_scratch(**_kwargs):
            await asyncio.sleep(0.01)
            return []

        adapter.scratch_list = AsyncMock(side_effect=slow_scratch)
        adapter.increment_access = AsyncMock()

        tracker = self._make_tracker()
        engine = SurfacingEngine(
            config=_make_config(include_session_context=True),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        out_a, out_b = await asyncio.gather(
            engine.surface(
                "gh",
                "read_file",
                {"path": "src/a.py", "_context_query": "Flask architecture"},
                LONG_RESPONSE,
            ),
            engine.surface(
                "gh",
                "search",
                {"path": "src/b.py", "_context_query": "Django routes"},
                LONG_RESPONSE,
            ),
        )

        appears_in_a = "the shared memory content" in out_a
        appears_in_b = "the shared memory content" in out_b
        assert not (appears_in_a and appears_in_b), (
            "Session dedup violated under concurrency: shared memory surfaced "
            "in both concurrent responses"
        )


class TestMaybeCleanupExpired:
    """Integration: _maybe_cleanup_expired() scheduling from surface()."""

    def _make_tracker(self):
        tracker = MagicMock()
        tracker.store = MagicMock()
        tracker.store.get_seen_ids = MagicMock(return_value=set())
        tracker.store.mark_surfaced = MagicMock()
        tracker.store.cleanup_expired = MagicMock(return_value=0)
        tracker.store.record_surfacing_event = MagicMock()
        return tracker

    async def test_cleanup_called_once_per_interval(self):
        """Two surface() calls within the interval → cleanup runs only once."""
        tracker = self._make_tracker()
        results = [FakeSearchResult(chunk=FakeChunk(content="mem"), score=0.5)]
        engine = SurfacingEngine(
            config=_make_config(dedup_ttl_seconds=3600),
            mcp_adapter=_make_mcp_adapter(results),
            feedback_tracker=tracker,
        )
        # Set last_cleanup to the past so first call triggers cleanup
        engine._last_cleanup = time.monotonic() - 7200

        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert tracker.store.cleanup_expired.call_count == 1

        # Second call within interval — should NOT trigger cleanup again
        engine._cache.clear()
        await engine.surface(
            "s",
            "read_file",
            {"path": "/other", "_context_query": "different query for testing"},
            LONG_RESPONSE,
        )
        assert tracker.store.cleanup_expired.call_count == 1

    async def test_cleanup_fires_again_after_interval(self):
        """Advance the clock past the interval → cleanup runs again."""
        tracker = self._make_tracker()
        results = [FakeSearchResult(chunk=FakeChunk(content="mem"), score=0.5)]
        engine = SurfacingEngine(
            config=_make_config(dedup_ttl_seconds=3600),
            mcp_adapter=_make_mcp_adapter(results),
            feedback_tracker=tracker,
        )
        engine._last_cleanup = time.monotonic() - 7200

        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert tracker.store.cleanup_expired.call_count == 1

        # Simulate clock advancing past the 1-hour interval
        engine._last_cleanup = time.monotonic() - 7200
        engine._cache.clear()
        await engine.surface(
            "s",
            "read_file",
            {"path": "/z", "_context_query": "another query for clock test"},
            LONG_RESPONSE,
        )
        assert tracker.store.cleanup_expired.call_count == 2

    async def test_cleanup_skipped_when_ttl_zero(self):
        """dedup_ttl_seconds=0 disables cleanup entirely."""
        tracker = self._make_tracker()
        results = [FakeSearchResult(chunk=FakeChunk(content="mem"), score=0.5)]
        engine = SurfacingEngine(
            config=_make_config(dedup_ttl_seconds=0),
            mcp_adapter=_make_mcp_adapter(results),
            feedback_tracker=tracker,
        )
        engine._last_cleanup = time.monotonic() - 7200

        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        tracker.store.cleanup_expired.assert_not_called()

    async def test_cleanup_skipped_when_no_tracker(self):
        """No feedback_tracker → cleanup never fires."""
        results = [FakeSearchResult(chunk=FakeChunk(content="mem"), score=0.5)]
        engine = SurfacingEngine(
            config=_make_config(dedup_ttl_seconds=3600),
            mcp_adapter=_make_mcp_adapter(results),
            feedback_tracker=None,
        )
        engine._last_cleanup = time.monotonic() - 7200

        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        # No tracker → no cleanup call possible

    async def test_cleanup_exception_does_not_break_surface(self):
        """cleanup_expired() raising should be caught, surface() continues."""
        tracker = self._make_tracker()
        tracker.store.cleanup_expired = MagicMock(side_effect=RuntimeError("DB locked"))
        results = [FakeSearchResult(chunk=FakeChunk(content="mem"), score=0.5)]
        engine = SurfacingEngine(
            config=_make_config(dedup_ttl_seconds=3600),
            mcp_adapter=_make_mcp_adapter(results),
            feedback_tracker=tracker,
        )
        engine._last_cleanup = time.monotonic() - 7200

        output = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        # Should not crash — cleanup error is swallowed
        assert "mem" in output
        tracker.store.cleanup_expired.assert_called_once()


class TestSurfacingEngineStop:
    """Verify stop() drains background webhook tasks cleanly."""

    async def test_stop_cancels_pending_background_tasks(self):
        """Pending tasks are cancelled and drained."""
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter([]),
        )

        async def never_completes():
            await asyncio.sleep(100)

        t1 = asyncio.create_task(never_completes())
        t2 = asyncio.create_task(never_completes())
        engine._background_tasks.add(t1)
        engine._background_tasks.add(t2)

        await engine.stop()

        assert t1.cancelled()
        assert t2.cancelled()
        assert len(engine._background_tasks) == 0

    async def test_stop_is_idempotent_with_no_tasks(self):
        """stop() with no pending tasks is a no-op and does not raise."""
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter([]),
        )

        await engine.stop()
        await engine.stop()  # second call should also be safe


class TestWebhookExceptionPaths:
    """Verify `_on_webhook_done` handles failures without re-raising or leaking tasks.

    `_background_tasks` are fire-and-forget: a failing webhook must not (a) crash
    the caller of `surface()`, (b) leave a dangling task in the set, or (c)
    vanish silently — the warning log is the only operator signal.
    """

    async def _run_surface_with_webhook(self, fire_mock):
        results = [FakeSearchResult(chunk=FakeChunk(content="mem"), score=0.5)]
        webhook_manager = MagicMock()
        webhook_manager.fire = fire_mock
        engine = SurfacingEngine(
            config=_make_config(fire_webhook=True),
            mcp_adapter=_make_mcp_adapter(results),
            webhook_manager=webhook_manager,
        )
        output = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        # Drain the fire-and-forget task so `_on_webhook_done` gets a chance to run.
        pending = list(engine._background_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return engine, output

    async def test_webhook_http_error_is_logged_not_raised(self, caplog):
        """fire() raising (simulating HTTP 500) → warning logged, caller unaffected."""

        async def failing_fire(*args, **kwargs):
            raise RuntimeError("simulated 500 Internal Server Error")

        with caplog.at_level("WARNING", logger="memtomem_stm.surfacing.engine"):
            engine, output = await self._run_surface_with_webhook(failing_fire)

        assert "Relevant Memories" in output  # surface() returned normally
        assert len(engine._background_tasks) == 0  # task cleaned up
        assert any(
            "Webhook fire-and-forget task failed" in rec.message for rec in caplog.records
        ), "webhook failure must be logged as a warning"

    async def test_webhook_timeout_is_logged_not_raised(self, caplog):
        """fire() raising TimeoutError is treated the same as any other exception."""

        async def timeout_fire(*args, **kwargs):
            raise TimeoutError("webhook POST timed out")

        with caplog.at_level("WARNING", logger="memtomem_stm.surfacing.engine"):
            engine, _ = await self._run_surface_with_webhook(timeout_fire)

        assert len(engine._background_tasks) == 0
        assert any("Webhook fire-and-forget task failed" in rec.message for rec in caplog.records)

    async def test_webhook_success_no_warning_logged(self, caplog):
        """Happy path: fire() returns cleanly, no warning produced."""

        async def ok_fire(*args, **kwargs):
            return None

        with caplog.at_level("WARNING", logger="memtomem_stm.surfacing.engine"):
            engine, _ = await self._run_surface_with_webhook(ok_fire)

        assert len(engine._background_tasks) == 0
        assert not any(
            "Webhook fire-and-forget task failed" in rec.message for rec in caplog.records
        )

    async def test_webhook_cancelled_does_not_log_failure(self, caplog):
        """A cancelled task must NOT be logged as a failure — cancellation is
        an expected shutdown path, not an error."""
        webhook_manager = MagicMock()

        # fire() blocks forever so we can cancel it mid-flight.
        async def blocking_fire(*args, **kwargs):
            await asyncio.sleep(100)

        webhook_manager.fire = blocking_fire
        engine = SurfacingEngine(
            config=_make_config(fire_webhook=True),
            mcp_adapter=_make_mcp_adapter([FakeSearchResult(chunk=FakeChunk(), score=0.5)]),
            webhook_manager=webhook_manager,
        )

        with caplog.at_level("WARNING", logger="memtomem_stm.surfacing.engine"):
            await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
            await engine.stop()  # cancels and drains

        assert len(engine._background_tasks) == 0
        assert not any(
            "Webhook fire-and-forget task failed" in rec.message for rec in caplog.records
        ), "cancelled tasks must not be logged as failures"
        assert len(engine._background_tasks) == 0
