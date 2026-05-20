"""Tests for SurfacingEngine — the core proactive memory surfacing orchestrator."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

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
        # #352 part 2: default off in tests so the cleanup loop's new
        # retention branch only fires when a test opts in. Production
        # default is 30 days (set in SurfacingConfig).
        "query_retention_days": 0,
    }
    defaults.update(overrides)
    return SurfacingConfig(**defaults)


def _make_mcp_adapter(
    results: list[FakeSearchResult] | None = None,
    *,
    hints: list[str] | None = None,
    outcome: str | None = None,
):
    """Build a mock McpClientSearchAdapter that returns the given results.

    ``outcome`` defaults to ``"ok"`` when results are non-empty and
    ``"empty_results"`` when they are — the two fall-through paths that
    keep the existing engine flow intact (#295). Pass it explicitly to
    drive the new failure-outcome dispatch (``no_session``,
    ``transport_error``, ``call_error``, ``empty_content``).
    """
    res_list = results or []
    if outcome is None:
        outcome = "ok" if res_list else "empty_results"
    adapter = AsyncMock()
    adapter.search = AsyncMock(return_value=(res_list, hints or [], outcome))
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


class TestExplicitContextQueryKwarg:
    """``surface(context_query=...)`` threads through to the LTM query.

    Behavioral pin — the proxy forwards the agent-provided ``_context_query``
    via this kwarg instead of leaving it in ``arguments``. The query reaching
    ``adapter.search`` must be the explicit value, not the heuristic.
    """

    async def test_explicit_kwarg_drives_adapter_search_query(self):
        adapter = _make_mcp_adapter([FakeSearchResult(FakeChunk(content="hit"), 0.5)])
        engine = SurfacingEngine(config=_make_config(), mcp_adapter=adapter)
        await engine.surface(
            "gh",
            "read_file",
            {"path": "/unrelated/path.py"},
            LONG_RESPONSE,
            context_query="find authentication code",
        )
        assert adapter.search.await_args is not None
        kwargs = adapter.search.await_args.kwargs
        assert kwargs["query"] == "find authentication code"

    async def test_explicit_kwarg_wins_over_legacy_in_arguments(self):
        adapter = _make_mcp_adapter([FakeSearchResult(FakeChunk(content="hit"), 0.5)])
        engine = SurfacingEngine(config=_make_config(), mcp_adapter=adapter)
        await engine.surface(
            "gh",
            "read_file",
            {"path": "/x.py", "_context_query": "loser"},
            LONG_RESPONSE,
            context_query="winner",
        )
        assert adapter.search.await_args.kwargs["query"] == "winner"

    async def test_per_tool_template_still_wins_over_kwarg(self):
        from memtomem_stm.surfacing.config import ToolSurfacingConfig

        adapter = _make_mcp_adapter([FakeSearchResult(FakeChunk(content="hit"), 0.5)])
        engine = SurfacingEngine(
            config=_make_config(
                context_tools={"read_file": ToolSurfacingConfig(query_template="file {arg.path}")}
            ),
            mcp_adapter=adapter,
        )
        await engine.surface(
            "gh",
            "read_file",
            {"path": "/src/main.py"},
            LONG_RESPONSE,
            context_query="ignored because template wins",
        )
        assert adapter.search.await_args.kwargs["query"] == "file /src/main.py"


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
            return [], [], "empty_results"

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
            return (results, [], "ok")

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
            return ([FakeSearchResult(chunk=FakeChunk(content="hit"), score=0.5)], [], "ok")

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
            assert "surfacing_id" in out1

            # Cache hit → new surfacing_id recorded
            out2 = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
            assert "surfacing_id" in out2

            # Extract the surfacing_id from the second (cached) output
            import re

            match = re.search(r"surfacing_id[:=]\s*\"?([a-f0-9]{16})", out2)
            assert match, "surfacing_id not found in cached output"
            cached_sid = match.group(1)

            # Feedback for the cached surfacing_id must succeed
            result = await engine.handle_feedback(cached_sid, "helpful")
            assert "Error" not in result

            tracker.close()

    async def test_record_failure_omits_surfacing_id(self, caplog):
        """When record_surfacing() raises, memories are still injected but
        the surfacing_id feedback prompt is omitted — untracked IDs must
        never be shown to the agent."""
        results = [FakeSearchResult(chunk=FakeChunk(id="m3", content="mem content"), score=0.7)]
        adapter = _make_mcp_adapter(results)
        tracker = MagicMock()
        tracker.record_surfacing = MagicMock(side_effect=RuntimeError("DB locked"))
        tracker.store = MagicMock()
        tracker.store.mark_surfaced = MagicMock()
        tracker.store.get_seen_ids = MagicMock(return_value=[])

        engine = SurfacingEngine(
            config=_make_config(cooldown_seconds=0),
            mcp_adapter=adapter,
            feedback_tracker=tracker,
        )

        # Cache miss path — record_surfacing raises
        out = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)

        assert "mem content" in out
        assert "surfacing_id" not in out
        assert "Failed to record surfacing event" in caplog.text

        # Cache hit path — same behavior
        caplog.clear()
        out2 = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)

        assert "mem content" in out2
        assert "surfacing_id" not in out2
        assert "Failed to record cached surfacing event" in caplog.text

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

            match = re.search(r"surfacing_id[:=]\s*\"?([a-f0-9]{16})", out1)
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

            match = re.search(r"surfacing_id[:=]\s*\"?([a-f0-9]{16})", out1)
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
        tracker.store.cleanup_expired_queries = MagicMock(return_value=0)
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

    # ── #352 part 2: query retention branch ────────────────────────────

    async def test_query_retention_runs_when_enabled(self):
        """``query_retention_days > 0`` triggers ``cleanup_expired_queries``
        with the configured window in seconds."""
        tracker = self._make_tracker()
        results = [FakeSearchResult(chunk=FakeChunk(content="mem"), score=0.5)]
        engine = SurfacingEngine(
            config=_make_config(dedup_ttl_seconds=3600, query_retention_days=30),
            mcp_adapter=_make_mcp_adapter(results),
            feedback_tracker=tracker,
        )
        engine._last_cleanup = time.monotonic() - 7200

        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        tracker.store.cleanup_expired_queries.assert_called_once_with(30 * 86400.0)

    async def test_query_retention_skipped_when_zero(self):
        """``query_retention_days=0`` disables the retention branch even
        when dedup cleanup is on — operators who want indefinite query
        retention keep the column populated."""
        tracker = self._make_tracker()
        results = [FakeSearchResult(chunk=FakeChunk(content="mem"), score=0.5)]
        engine = SurfacingEngine(
            config=_make_config(dedup_ttl_seconds=3600, query_retention_days=0),
            mcp_adapter=_make_mcp_adapter(results),
            feedback_tracker=tracker,
        )
        engine._last_cleanup = time.monotonic() - 7200

        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        tracker.store.cleanup_expired_queries.assert_not_called()

    async def test_retention_runs_even_when_dedup_disabled(self):
        """Retention is independent of dedup — ``dedup_ttl_seconds=0`` must
        not suppress query-column nulling. The old guard short-circuited
        on ``dedup_ttl_seconds <= 0`` and would have skipped retention
        entirely; this test pins the independent-sub-tasks contract."""
        tracker = self._make_tracker()
        results = [FakeSearchResult(chunk=FakeChunk(content="mem"), score=0.5)]
        engine = SurfacingEngine(
            config=_make_config(dedup_ttl_seconds=0, query_retention_days=7),
            mcp_adapter=_make_mcp_adapter(results),
            feedback_tracker=tracker,
        )
        engine._last_cleanup = time.monotonic() - 7200

        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        tracker.store.cleanup_expired.assert_not_called()
        tracker.store.cleanup_expired_queries.assert_called_once_with(7 * 86400.0)

    async def test_retention_exception_is_swallowed(self):
        """A misbehaving ``cleanup_expired_queries`` must not break
        surface() — symmetric to the dedup-cleanup exception path."""
        tracker = self._make_tracker()
        tracker.store.cleanup_expired_queries = MagicMock(side_effect=RuntimeError("locked"))
        results = [FakeSearchResult(chunk=FakeChunk(content="mem"), score=0.5)]
        engine = SurfacingEngine(
            config=_make_config(dedup_ttl_seconds=0, query_retention_days=7),
            mcp_adapter=_make_mcp_adapter(results),
            feedback_tracker=tracker,
        )
        engine._last_cleanup = time.monotonic() - 7200

        out = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "mem" in out
        tracker.store.cleanup_expired_queries.assert_called_once()


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


class TestLtmHintsObservability:
    """B3 — parent trust-UX hints are surfaced to operators via INFO log and
    optional TokenTracker snapshot; they are NOT forwarded to the downstream
    agent (prepend body is unchanged). See B3 plan § 'forward hints'."""

    async def test_hints_logged_at_info_when_non_empty(self, caplog):
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter(
                [FakeSearchResult(chunk=FakeChunk(), score=0.5)],
                hints=["2 results filtered by namespace"],
            ),
        )
        with caplog.at_level("INFO", logger="memtomem_stm.surfacing.engine"):
            await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        matching = [r for r in caplog.records if "LTM hints for" in r.message]
        assert len(matching) == 1
        assert matching[0].levelname == "INFO"
        assert "2 results filtered" in matching[0].message

    async def test_no_log_when_hints_empty(self, caplog):
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter(
                [FakeSearchResult(chunk=FakeChunk(), score=0.5)],
                hints=[],
            ),
        )
        with caplog.at_level("INFO", logger="memtomem_stm.surfacing.engine"):
            await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert not any("LTM hints for" in r.message for r in caplog.records)

    async def test_hints_logged_even_when_results_empty(self, caplog):
        """Operator-observability fires independently of result filtering —
        a "3 filtered before you got here" hint matters even when SURFACE
        ultimately injects nothing."""
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter(
                results=[],
                hints=["All 5 candidates below min_score"],
            ),
        )
        with caplog.at_level("INFO", logger="memtomem_stm.surfacing.engine"):
            out = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        # Prepend body unchanged — hints are not forwarded to downstream.
        assert out == LONG_RESPONSE
        assert any(
            "LTM hints for" in r.message and "below min_score" in r.message for r in caplog.records
        )

    async def test_hints_forwarded_to_token_tracker_when_provided(self):
        tracker = MagicMock()
        tracker.record_hints = MagicMock()
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter(
                [FakeSearchResult(chunk=FakeChunk(), score=0.5)],
                hints=["notice A", "notice B"],
            ),
            token_tracker=tracker,
        )
        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        tracker.record_hints.assert_called_once_with(["notice A", "notice B"])

    async def test_token_tracker_absent_is_not_fatal(self, caplog):
        """Engine must log INFO even when no tracker is wired — observability
        path degrades open, never crashes a proxy response."""
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter(
                [FakeSearchResult(chunk=FakeChunk(), score=0.5)],
                hints=["standalone notice"],
            ),
            token_tracker=None,
        )
        with caplog.at_level("INFO", logger="memtomem_stm.surfacing.engine"):
            out = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        # Response still normal, log still fired.
        assert "Relevant Memories" in out or out == LONG_RESPONSE
        assert any("LTM hints for" in r.message for r in caplog.records)

    async def test_token_tracker_failure_is_swallowed(self, caplog):
        """A misbehaving tracker must not break the proxy response path."""
        tracker = MagicMock()
        tracker.record_hints = MagicMock(side_effect=RuntimeError("tracker boom"))
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter(
                [FakeSearchResult(chunk=FakeChunk(), score=0.5)],
                hints=["notice"],
            ),
            token_tracker=tracker,
        )
        # Must not raise
        out = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert out is not None


class TestPerToolMinScoreOverride:
    """Per-tool `ToolSurfacingConfig.min_score` must win over the auto-tuner
    and over the falsy-check trap on 0.0. Precedence: tool_cfg.min_score >
    auto-tuned value > global default."""

    def _make_tracker(self, tmp_path: Path, config: SurfacingConfig):
        from memtomem_stm.surfacing.feedback import FeedbackTracker

        return FeedbackTracker(config=config, db_path=tmp_path / "fb.db")

    async def test_per_tool_override_wins_when_auto_tune_enabled(self, tmp_path: Path):
        """tool_cfg.min_score=0.1 must filter results with score=0.05 even
        though global default is 0.02 and auto-tune default min would accept."""
        from memtomem_stm.surfacing.config import ToolSurfacingConfig

        config = _make_config(
            auto_tune_enabled=True,
            min_score=0.02,
            context_tools={"read_file": ToolSurfacingConfig(min_score=0.1)},
        )
        tracker = self._make_tracker(tmp_path, config)
        try:
            engine = SurfacingEngine(
                config=config,
                mcp_adapter=_make_mcp_adapter(
                    [FakeSearchResult(chunk=FakeChunk(content="below override"), score=0.05)]
                ),
                feedback_tracker=tracker,
            )
            out = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
            assert out == LONG_RESPONSE, (
                "per-tool min_score=0.1 must filter score=0.05; got injection"
            )
        finally:
            tracker.close()

    async def test_per_tool_override_skips_auto_tune_learning(self, tmp_path: Path):
        """When a per-tool override is set, the auto-tuner must not learn
        (write to _adjustments) for that tool, even if feedback would
        otherwise trigger an adjustment. A control tool without override
        still gets adjusted."""
        from memtomem_stm.surfacing.config import ToolSurfacingConfig

        config = _make_config(
            auto_tune_enabled=True,
            auto_tune_min_samples=3,
            min_score=0.02,
            context_tools={"read_file": ToolSurfacingConfig(min_score=0.1)},
        )
        tracker = self._make_tracker(tmp_path, config)
        try:
            # Seed feedback that would trigger an auto-tune raise (>60%
            # not_relevant) for BOTH the overridden tool and a control tool.
            for i, tool_name in enumerate(["read_file", "list_dir"]):
                sid = f"seed-{i}"
                tracker.store.record_surfacing(sid, "gh", tool_name, "q", ["m1"], [0.5])
                for _ in range(5):
                    tracker.store.record_feedback(sid, "not_relevant")

            engine = SurfacingEngine(
                config=config,
                mcp_adapter=_make_mcp_adapter([FakeSearchResult(chunk=FakeChunk(), score=0.5)]),
                feedback_tracker=tracker,
            )
            # Exercise the overridden tool — should NOT invoke maybe_adjust.
            await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
            assert "read_file" not in engine._auto_tuner._adjustments, (
                "auto-tuner must not learn for a tool with a per-tool override"
            )

            # Control: a tool WITHOUT override still gets adjusted.
            await engine.surface(
                "gh",
                "list_dir",
                {"path": "src/", "_context_query": "Flask architecture"},
                LONG_RESPONSE,
            )
            assert engine._auto_tuner._adjustments.get("list_dir", 0) > 0.02, (
                "control tool should have been raised above global default"
            )
        finally:
            tracker.close()

    async def test_per_tool_override_zero_respected(self, tmp_path: Path):
        """Regression for the falsy-check trap: tool_cfg.min_score=0.0 is a
        valid explicit override (Field ge=0.0). Must NOT fall through to
        global default."""
        from memtomem_stm.surfacing.config import ToolSurfacingConfig

        config = _make_config(
            auto_tune_enabled=False,
            min_score=0.5,  # high global; tool_cfg should lower it to 0.0
            context_tools={"read_file": ToolSurfacingConfig(min_score=0.0)},
        )
        engine = SurfacingEngine(
            config=config,
            mcp_adapter=_make_mcp_adapter(
                [FakeSearchResult(chunk=FakeChunk(content="barely any score"), score=0.01)]
            ),
        )
        out = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "barely any score" in out, (
            "per-tool min_score=0.0 must be respected (is-not-None, not truthy)"
        )


class TestSurfacingEngineObservability:
    """Engine-level skip/outcome counter wiring.

    Gate-level skip reasons (5 of 9) are covered in
    ``test_relevance_gate.py::TestRelevanceGateObservability``. This class
    covers the engine's 4 skip reasons + 4 outcomes + cache hit/miss
    counters, plus the rule that exactly one skip OR one outcome is
    recorded per ``surface()`` call (no double-counting).
    """

    def _engine_with_obs(self, **cfg_overrides):
        from memtomem_stm.surfacing.observability import SurfacingObservability

        results = cfg_overrides.pop("_results", None)
        if results is None:
            results = [FakeSearchResult(chunk=FakeChunk(content="m"), score=0.5)]
        adapter = _make_mcp_adapter(results)
        obs = SurfacingObservability()
        engine = SurfacingEngine(
            config=_make_config(**cfg_overrides),
            mcp_adapter=adapter,
            observability=obs,
        )
        return engine, obs, adapter

    async def test_disabled_records_skip(self):
        engine, obs, _ = self._engine_with_obs(enabled=False)
        await engine.surface("s", "tool", VALID_ARGS, LONG_RESPONSE)
        assert obs.snapshot()["skip_reasons"]["tool"] == {"disabled": 1}

    async def test_response_too_short_records_skip(self):
        engine, obs, _ = self._engine_with_obs(min_response_chars=10000)
        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert obs.snapshot()["skip_reasons"]["read_file"] == {"response_too_short": 1}

    async def test_circuit_open_records_skip(self):
        engine, obs, _ = self._engine_with_obs()
        # Force the breaker open by directly calling its failure recorder.
        for _ in range(engine._circuit_breaker._max_failures):
            engine._circuit_breaker.record_failure()
        assert engine._circuit_breaker.is_open
        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert obs.snapshot()["skip_reasons"]["read_file"] == {"circuit_open": 1}

    async def test_no_results_score_records_skip_and_cache_miss(self):
        results = [FakeSearchResult(chunk=FakeChunk(), score=0.001)]
        engine, obs, _ = self._engine_with_obs(min_score=0.5, _results=results)
        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        snap = obs.snapshot()
        assert snap["skip_reasons"]["read_file"] == {"no_results_score": 1}
        # First call goes through the miss path, so cache.miss == 1
        assert snap["cache"] == {"miss": 1}

    async def test_surfaced_cache_miss_then_hit(self):
        results = [FakeSearchResult(chunk=FakeChunk(content="hit me"), score=0.5)]
        engine, obs, _ = self._engine_with_obs(_results=results)
        # Use a query stable across calls so the cache key matches; the
        # in-memory ``_surfaced_ids`` would normally dedup the second call,
        # but the cache short-circuits before dedup runs.
        args = {"_context_query": "stable query for cache hit test"}
        await engine.surface("s", "read_file", args, LONG_RESPONSE)
        await engine.surface("s", "read_file", args, LONG_RESPONSE)
        snap = obs.snapshot()
        assert snap["outcomes"]["read_file"] == {
            "surfaced_cache_miss": 1,
            "surfaced_cache_hit": 1,
        }
        assert snap["cache"] == {"miss": 1, "hit": 1}

    async def test_no_results_empty_cache_skip_on_repeat(self):
        """An empty result populates the cache with []; the second identical
        query is a cache hit but renders nothing — that's the
        ``no_results_empty_cache`` bucket."""
        engine, obs, _ = self._engine_with_obs(_results=[])
        args = {"_context_query": "query that returns nothing"}
        await engine.surface("s", "read_file", args, LONG_RESPONSE)
        await engine.surface("s", "read_file", args, LONG_RESPONSE)
        snap = obs.snapshot()
        # First call: miss path → no LTM results → no_results_score
        # Second call: cache hit (empty) → no_results_empty_cache
        assert snap["skip_reasons"]["read_file"]["no_results_score"] == 1
        assert snap["skip_reasons"]["read_file"]["no_results_empty_cache"] == 1
        assert snap["cache"] == {"miss": 1, "hit": 1}

    async def test_error_other_records_outcome(self):
        from memtomem_stm.surfacing.observability import SurfacingObservability

        adapter = _make_mcp_adapter()
        adapter.search = AsyncMock(side_effect=RuntimeError("boom"))
        obs = SurfacingObservability()
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            observability=obs,
        )
        out = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert out == LONG_RESPONSE
        assert obs.snapshot()["outcomes"]["read_file"] == {"error_other": 1}

    async def test_error_timeout_records_outcome(self):
        from memtomem_stm.surfacing.observability import SurfacingObservability

        async def slow_search(*a, **kw):
            await asyncio.sleep(1.0)
            return ([], [], "empty_results")

        adapter = _make_mcp_adapter()
        adapter.search = AsyncMock(side_effect=slow_search)
        obs = SurfacingObservability()
        engine = SurfacingEngine(
            config=_make_config(timeout_seconds=0.05),
            mcp_adapter=adapter,
            observability=obs,
        )
        out = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert out == LONG_RESPONSE
        assert obs.snapshot()["outcomes"]["read_file"] == {"error_timeout": 1}

    async def test_no_double_counting_per_call(self):
        """Each surface() invocation records exactly one skip OR one outcome."""
        engine, obs, _ = self._engine_with_obs()
        for _ in range(5):
            await engine.surface(
                "s",
                "read_file",
                {"_context_query": f"q {time.time()}"},  # unique to bypass cooldown+cache
                LONG_RESPONSE,
            )
        snap = obs.snapshot()
        total_skips = sum(snap["skip_reasons"].get("read_file", {}).values())
        total_outcomes = sum(snap["outcomes"].get("read_file", {}).values())
        # 5 unique queries, score=0.5 default, default min_score=0.02 → all
        # surface successfully; the gate's eager rate-limit claim still
        # counts in the engine's outcome (surfaced_cache_miss).
        assert total_skips + total_outcomes == 5

    async def test_observability_omitted_keeps_engine_working(self):
        """Default ``observability=None`` (existing callers) must not break
        anything — the engine just doesn't record."""
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter(
                [FakeSearchResult(chunk=FakeChunk(content="ok"), score=0.5)]
            ),
        )
        out = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert "ok" in out
        assert engine.observability is None

    async def test_no_query_records_skip(self):
        """When ``ContextExtractor.extract_query`` returns None — the
        fallback tool-name token count below ``min_query_tokens`` — the
        engine records ``no_query``. Force the None return by raising
        ``min_query_tokens`` above any fallback's token count rather than
        relying on the default value, so a future default change cannot
        silently push the test onto a different path."""
        engine, obs, _ = self._engine_with_obs(min_query_tokens=999)
        await engine.surface("s", "read_file", {}, LONG_RESPONSE)
        assert obs.snapshot()["skip_reasons"]["read_file"] == {"no_query": 1}

    async def test_no_results_dedup_records_skip(self):
        """Results pass the score filter but every memory id is already in
        ``_surfaced_ids`` from an earlier surface — distinct from
        ``no_results_score`` so an operator can tell whether to lower
        ``min_score`` (former) or whether session-dedup is over-aggressive
        on long sessions (latter)."""
        chunk = FakeChunk(id="dup-id", content="m")
        results = [FakeSearchResult(chunk=chunk, score=0.5)]
        engine, obs, _ = self._engine_with_obs(_results=results)
        engine._surfaced_ids["dup-id"] = None
        await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        snap = obs.snapshot()
        assert snap["skip_reasons"]["read_file"] == {"no_results_dedup": 1}
        assert snap["cache"] == {"miss": 1}

    async def test_no_results_invalidated_records_skip(self):
        """Cache hit returns results but all are in ``_invalidated_ids``
        (rated ``not_relevant`` / ``already_known`` within the cache TTL) —
        distinct from ``no_results_empty_cache`` so an operator can tell
        whether the cache was deliberately seeded empty (LTM had nothing)
        or whether feedback invalidated everything since."""
        chunk = FakeChunk(id="inv-id", content="m")
        results = [FakeSearchResult(chunk=chunk, score=0.5)]
        engine, obs, _ = self._engine_with_obs(_results=results)
        args = {"_context_query": "stable cache key for invalidation test"}
        # First call: miss path populates cache with [chunk inv-id].
        await engine.surface("s", "read_file", args, LONG_RESPONSE)
        # Mark the surfaced memory invalidated for this server/tool. The
        # second call hits the cache, runs the invalidation filter, and
        # falls through to the "had results, all rejected" branch.
        engine._invalidated_ids[("s", "read_file", "inv-id")] = None
        await engine.surface("s", "read_file", args, LONG_RESPONSE)
        snap = obs.snapshot()
        assert snap["outcomes"]["read_file"] == {"surfaced_cache_miss": 1}
        assert snap["skip_reasons"]["read_file"] == {"no_results_invalidated": 1}
        assert snap["cache"] == {"miss": 1, "hit": 1}


class TestSurfacingLtmOutcomeDispatch:
    """#295: adapter ``SearchOutcome`` → distinct engine skip label.

    Five different failure modes used to collapse to ``([], [])`` and look
    identical to a healthy empty namespace. The engine now reads the
    adapter's outcome and records ``ltm_unavailable`` / ``ltm_call_failed``
    / ``ltm_parse_empty`` so an operator looking at the surfacing stats
    table can tell which of "session never opened", "core raised
    mid-call", "core returned no text content", and "core returned no
    rows" is happening. ``ok`` / ``empty_results`` still fall through to
    the existing min_score / dedup path so the no_results_score signal an
    operator uses to tune min_score is preserved.
    """

    def _engine(self, *, outcome: str, results: list | None = None):
        from memtomem_stm.surfacing.observability import SurfacingObservability

        adapter = _make_mcp_adapter(results, outcome=outcome)
        obs = SurfacingObservability()
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=adapter,
            observability=obs,
        )
        return engine, obs

    async def test_no_session_outcome_records_ltm_unavailable(self):
        engine, obs = self._engine(outcome="no_session")
        out = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert out == LONG_RESPONSE
        assert obs.snapshot()["skip_reasons"]["read_file"] == {"ltm_unavailable": 1}

    async def test_transport_error_outcome_records_ltm_unavailable(self):
        engine, obs = self._engine(outcome="transport_error")
        out = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert out == LONG_RESPONSE
        # Same bucket as ``no_session`` — both indicate "LTM not currently
        # answering"; an operator just needs to know to look at LTM, not
        # which of the two specific causes it was.
        assert obs.snapshot()["skip_reasons"]["read_file"] == {"ltm_unavailable": 1}

    async def test_call_error_outcome_records_ltm_call_failed(self):
        engine, obs = self._engine(outcome="call_error")
        out = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert out == LONG_RESPONSE
        assert obs.snapshot()["skip_reasons"]["read_file"] == {"ltm_call_failed": 1}

    async def test_empty_content_outcome_records_ltm_parse_empty(self):
        engine, obs = self._engine(outcome="empty_content")
        out = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert out == LONG_RESPONSE
        assert obs.snapshot()["skip_reasons"]["read_file"] == {"ltm_parse_empty": 1}

    async def test_empty_results_outcome_falls_through_to_no_results_score(self):
        """A genuine empty-namespace ``mem_search`` still records
        ``no_results_score`` so operators tuning min_score see the same
        signal they did before the outcome refactor."""
        engine, obs = self._engine(outcome="empty_results", results=[])
        out = await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        assert out == LONG_RESPONSE
        skips = obs.snapshot()["skip_reasons"]["read_file"]
        assert "ltm_unavailable" not in skips
        assert "ltm_call_failed" not in skips
        assert "ltm_parse_empty" not in skips
        assert skips == {"no_results_score": 1}

    async def test_failure_outcomes_do_not_populate_cache(self):
        """An ``ltm_unavailable`` early return must NOT poison the cache
        with an empty entry — the next call should retry LTM rather than
        silently serving the empty cached result."""
        engine, obs = self._engine(outcome="transport_error")
        args = {"_context_query": "transport-error retry probe"}
        await engine.surface("s", "read_file", args, LONG_RESPONSE)
        await engine.surface("s", "read_file", args, LONG_RESPONSE)
        snap = obs.snapshot()
        # Two LTM attempts, two ``ltm_unavailable`` skips, no cache hits.
        assert snap["skip_reasons"]["read_file"] == {"ltm_unavailable": 2}
        assert snap["cache"].get("hit", 0) == 0

    @pytest.mark.parametrize("outcome", ["no_session", "transport_error"])
    async def test_first_ltm_unavailable_logs_warning_once(self, outcome, caplog):
        """#349: the operator-visible signal for "LTM unreachable" was
        previously only a counter in ``stm_surfacing_stats`` that operators
        had to know to read. The first ``no_session`` / ``transport_error``
        outcome now logs a single WARNING naming the configured
        ``ltm_mcp_command`` so the operator can grep their logs and so
        ``mms health`` becomes a discoverable next step. Subsequent skips
        increment the counter only — the WARNING must not repeat per call,
        matching the prepend-on-progressive WARNING-once pattern (#348).

        Parametrized across both outcomes that map to ``ltm_unavailable``:
        a single-outcome test would silently pass if the engine condition
        were narrowed to ``if outcome == "no_session"`` only — the
        ``transport_error`` branch would lose its warning with no
        regression signal."""
        engine, obs = self._engine(outcome=outcome)
        args = {"_context_query": "ltm unreachable warning probe"}

        with caplog.at_level("WARNING", logger="memtomem_stm.surfacing.engine"):
            await engine.surface("s", "read_file", args, LONG_RESPONSE)
            await engine.surface("s", "read_file", args, LONG_RESPONSE)
            await engine.surface("s", "another_tool", args, LONG_RESPONSE)

        warnings = [
            r
            for r in caplog.records
            if "is not reachable" in r.message and r.levelname == "WARNING"
        ]
        assert len(warnings) == 1, (
            f"ltm-unavailable WARNING must fire exactly once across N skips, got {len(warnings)}"
        )
        # The WARNING must name the configured command so operators can map
        # the log line back to the misconfigured path.
        assert "memtomem-server" in warnings[0].message
        # All three calls still record the skip — the WARNING is in addition
        # to, not in place of, the counter signal.
        snap = obs.snapshot()
        assert snap["skip_reasons"]["read_file"]["ltm_unavailable"] == 2
        assert snap["skip_reasons"]["another_tool"]["ltm_unavailable"] == 1

    async def test_call_error_outcome_does_not_log_unavailable_warning(self, caplog):
        """The WARNING is scoped to the no-session / transport-error bucket
        (LTM not reachable). A mid-call ``call_error`` means the session
        opened fine and the operator's diagnostic path is different — that
        skip stays counter-only."""
        engine, _ = self._engine(outcome="call_error")
        with caplog.at_level("WARNING", logger="memtomem_stm.surfacing.engine"):
            await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        warnings = [r for r in caplog.records if "is not reachable" in r.message]
        assert warnings == []


class TestSurfacingQueryPrivacyAtInfo:
    """Issue #352 part 1 — the surfacing hot path must not emit user-derived
    query text at INFO. The extracted query routinely contains internal file
    paths, partial commit messages, or ticket first-sentences; operators who
    need it for tracing can flip the engine logger to DEBUG."""

    async def test_happy_path_info_log_omits_query_preview(self, caplog):
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter([FakeSearchResult(FakeChunk(), 0.9)]),
        )
        with caplog.at_level("INFO", logger="memtomem_stm.surfacing.engine"):
            await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        # `VALID_ARGS["_context_query"]` is 'Flask web framework architecture',
        # well under 50 chars so it would fully fit in any historical
        # `query[:50]` slice. Assert no INFO record leaks it.
        query = VALID_ARGS["_context_query"]
        info_records = [r for r in caplog.records if r.levelname == "INFO"]
        assert info_records, "expected at least one INFO record for the surfacing path"
        assert not any(query in r.getMessage() for r in info_records)

    async def test_query_preview_still_available_at_debug(self, caplog):
        engine = SurfacingEngine(
            config=_make_config(),
            mcp_adapter=_make_mcp_adapter([FakeSearchResult(FakeChunk(), 0.9)]),
        )
        with caplog.at_level("DEBUG", logger="memtomem_stm.surfacing.engine"):
            await engine.surface("s", "read_file", VALID_ARGS, LONG_RESPONSE)
        query = VALID_ARGS["_context_query"]
        debug_with_query = [
            r
            for r in caplog.records
            if r.levelname == "DEBUG" and query in r.getMessage() and "Surfacing" in r.getMessage()
        ]
        assert debug_with_query, (
            "operators flipping the surfacing logger to DEBUG must still see the "
            "query preview for tracing — only the default INFO level is sanitized"
        )
