"""Cache-hit reclaim of session-dedup IDs.

A surfacing cache hit re-injects the cached memories (the cache's purpose) but
used to leave the session-dedup set (``_surfaced_ids``) untouched, so a memory
re-shown from cache was not deduped against later misses. The fix *reclaims*
the injected IDs into ``_surfaced_ids`` (symmetric with the miss path) — it does
NOT filter the cached list, which would empty a normal repeated-query cache hit
and break the concurrent-identical-query contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine

pytestmark = pytest.mark.asyncio


@dataclass
class _Meta:
    source_file: Path = Path("/notes/test.md")
    namespace: str = "default"


@dataclass
class _Chunk:
    id: str
    content: str
    metadata: _Meta = field(default_factory=_Meta)


@dataclass
class _Result:
    chunk: _Chunk
    score: float
    rank: int = 1


def _config(**overrides) -> SurfacingConfig:
    base = {
        "enabled": True,
        "min_response_chars": 10,
        "min_score": 0.02,
        "max_results": 3,
        "cooldown_seconds": 0.0,  # no cooldown — let repeated queries reach the cache
        "max_surfacings_per_minute": 1000,
        "auto_tune_enabled": False,
        "include_session_context": False,
        "fire_webhook": False,
        "cache_ttl_seconds": 60.0,
        "query_retention_days": 0,
    }
    base.update(overrides)
    return SurfacingConfig(**base)


def _adapter(results):
    a = AsyncMock()
    a.search = AsyncMock(return_value=(results, [], "ok" if results else "empty_results"))
    return a


_LONG = "x" * 200
_Q1 = {"_context_query": "alpha query about authentication"}
_Q2 = {"_context_query": "beta query about billing systems"}


class TestClaimSurfacedIdsHelper:
    async def test_adds_ids(self) -> None:
        engine = SurfacingEngine(config=_config(), mcp_adapter=_adapter([]))
        engine._claim_surfaced_ids(["a", "b"])
        assert "a" in engine._surfaced_ids and "b" in engine._surfaced_ids

    async def test_prunes_to_cap(self) -> None:
        engine = SurfacingEngine(config=_config(), mcp_adapter=_adapter([]))
        engine._surfaced_ids_max = 10
        engine._claim_surfaced_ids([f"id-{i}" for i in range(20)])
        # Over the cap → evicted down to roughly half; oldest gone, newest kept.
        assert len(engine._surfaced_ids) <= 10
        assert "id-19" in engine._surfaced_ids
        assert "id-0" not in engine._surfaced_ids


class TestCacheHitReclaim:
    async def test_cache_hit_reclaims_evicted_id_and_dedups_later_miss(self) -> None:
        mem = _Result(chunk=_Chunk(id="mem-1", content="UNIQUE_ALPHA_MEMORY"), score=0.5)
        engine = SurfacingEngine(config=_config(), mcp_adapter=_adapter([mem]))

        # 1) Miss for Q1 surfaces the memory and claims its id.
        out1 = await engine.surface("s", "read_file", _Q1, _LONG)
        assert "UNIQUE_ALPHA_MEMORY" in out1
        assert "mem-1" in engine._surfaced_ids

        # 2) Simulate FIFO eviction of the id from the in-memory dedup set,
        #    while the cache entry for Q1 still holds the memory.
        del engine._surfaced_ids["mem-1"]

        # 3) Repeat Q1 → cache HIT. It must STILL inject (no filtering) AND
        #    reclaim the id back into the dedup set.
        out2 = await engine.surface("s", "read_file", _Q1, _LONG)
        assert "UNIQUE_ALPHA_MEMORY" in out2, "cache hit must still inject (no filtering)"
        assert "mem-1" in engine._surfaced_ids, "cache hit must reclaim the id"

        # 4) A different query Q2 whose miss returns the same memory is now
        #    deduped — without the reclaim in step 3 it would surface again.
        out3 = await engine.surface("s", "read_file", _Q2, _LONG)
        assert "UNIQUE_ALPHA_MEMORY" not in out3
        assert out3 == _LONG

    async def test_repeated_query_cache_hit_still_injects(self) -> None:
        # No-regression / contract: even with the id already in _surfaced_ids,
        # a same-query cache hit re-injects (the fix reclaims, never filters).
        mem = _Result(chunk=_Chunk(id="mem-9", content="UNIQUE_BETA_MEMORY"), score=0.5)
        engine = SurfacingEngine(config=_config(), mcp_adapter=_adapter([mem]))

        out1 = await engine.surface("s", "read_file", _Q1, _LONG)
        assert "UNIQUE_BETA_MEMORY" in out1
        out2 = await engine.surface("s", "read_file", _Q1, _LONG)
        assert "UNIQUE_BETA_MEMORY" in out2, (
            "repeated-query cache hit must not be filtered to empty"
        )
