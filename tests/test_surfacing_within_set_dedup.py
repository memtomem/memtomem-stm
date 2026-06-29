"""Within-result-set dedup on the surfacing miss path.

The miss-path relevance loop excludes IDs demoted by feedback and IDs claimed by
*prior* surfacings (``_surfaced_ids``), but ``_surfaced_ids`` is only populated
*after* the loop runs — so two results carrying the same id in a SINGLE search
response were not deduped against each other and both rendered as bullets. Under
``result_format='compact'`` a chunk's id is ``sha256(content)[:16]``, so any two
results with byte-identical content collide on one surrogate id and produced a
verbatim-duplicated memory line in the wire payload. The fix tracks an in-loop
``seen`` set so each id surfaces at most once per response.
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
        "cooldown_seconds": 0.0,
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
_Q = {"_context_query": "alpha query about authentication"}


class TestWithinResultSetDedup:
    async def test_identical_id_results_render_one_bullet(self) -> None:
        # Two results share one id (the compact surrogate-id collision). Pre-fix
        # both passed the loop's _surfaced_ids check and rendered twice.
        dup = "DUPLICATE_MEMORY_CONTENT_ABC"
        r1 = _Result(chunk=_Chunk(id="same-id", content=dup), score=0.6)
        r2 = _Result(chunk=_Chunk(id="same-id", content=dup), score=0.5)
        engine = SurfacingEngine(config=_config(), mcp_adapter=_adapter([r1, r2]))

        out = await engine.surface("s", "read_file", _Q, _LONG)
        assert dup in out, "the memory must still surface once"
        assert out.count(dup) == 1, "a duplicate-id memory must not render twice"
        # The id is claimed exactly once into the session-dedup set.
        assert "same-id" in engine._surfaced_ids

    async def test_distinct_ids_are_all_kept(self) -> None:
        # No-regression: distinct ids (the normal structured path) all surface.
        a = _Result(chunk=_Chunk(id="id-a", content="MEMORY_AAA"), score=0.6)
        b = _Result(chunk=_Chunk(id="id-b", content="MEMORY_BBB"), score=0.5)
        engine = SurfacingEngine(config=_config(), mcp_adapter=_adapter([a, b]))

        out = await engine.surface("s", "read_file", _Q, _LONG)
        assert "MEMORY_AAA" in out and "MEMORY_BBB" in out

    async def test_dedup_does_not_starve_max_results(self) -> None:
        # max_results counts UNIQUE memories: a leading duplicate must not consume
        # a slot that a later distinct memory should fill.
        dup = "DUP_FIRST"
        results = [
            _Result(chunk=_Chunk(id="dup", content=dup), score=0.9),
            _Result(chunk=_Chunk(id="dup", content=dup), score=0.8),
            _Result(chunk=_Chunk(id="u1", content="UNIQUE_ONE"), score=0.7),
            _Result(chunk=_Chunk(id="u2", content="UNIQUE_TWO"), score=0.6),
        ]
        engine = SurfacingEngine(config=_config(max_results=3), mcp_adapter=_adapter(results))

        out = await engine.surface("s", "read_file", _Q, _LONG)
        assert out.count(dup) == 1
        assert "UNIQUE_ONE" in out and "UNIQUE_TWO" in out
