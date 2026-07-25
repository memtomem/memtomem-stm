"""Capability-gated memtomem core integration contracts."""

from __future__ import annotations

import json
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.config import STMConfig
from memtomem_stm.proxy.config import ProxyConfig
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm import server as server_module
from memtomem_stm.server import STMContext, stm_memory_propose
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.mcp_client import (
    CompactResultParser,
    ContextComposeResult,
    LtmCapabilities,
    LtmTransportError,
    McpClientSearchAdapter,
    RemoteSearchResult,
    decode_context_compose_context,
)
from memtomem_stm.surfacing.observability import SurfacingObservability

# One from each end of the high and low surrogate blocks, so a range that is
# off by one at either boundary fails. Kept in step with the corpus of the
# same name in ``test_json_out.py``; duplicated rather than imported because
# nothing else in this suite imports across test modules.
SURROGATES = ["\ud800", "\udbff", "\udc00", "\udfff"]


def _ctx(config: STMConfig, engine: object | None):
    tracker = TokenTracker()
    app = STMContext(
        config=config,
        proxy_manager=ProxyManager(ProxyConfig(upstream_servers={}), tracker),
        tracker=tracker,
        surfacing_engine=engine,  # type: ignore[arg-type]
        feedback_tracker=None,
        compression_feedback_tracker=None,
        progressive_reads_tracker=None,
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


@pytest.mark.asyncio
async def test_capability_negotiation_is_additive() -> None:
    adapter = McpClientSearchAdapter(SurfacingConfig(result_format="structured"))
    session = AsyncMock()
    session.call_tool.return_value = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="text",
                text=json.dumps(
                    {
                        "capabilities": {
                            "search_formats": ["compact", "structured"],
                            "context_compose": {"schema_version": 2},
                            "candidate_propose": 1,
                            "scratch_formats": ["structured"],
                            "increment_access": True,
                        }
                    }
                ),
            )
        ]
    )
    await adapter._negotiate_format(session)
    assert adapter.capabilities == LtmCapabilities(2, 1, True, True)

    # A reconnect/downgrade must not retain the previous session's features.
    adapter._parser = CompactResultParser()
    await adapter._negotiate_format(session)
    assert adapter.capabilities == LtmCapabilities()


@pytest.mark.asyncio
async def test_engine_prefers_compose_and_pinned_bypasses_feedback_shape() -> None:
    pinned = RemoteSearchResult("always follow review policy", 1.0, "policy", pinned=True)
    pinned.chunk.id = "policy"
    retrieved = RemoteSearchResult("blue-green deployment", 0.8, "decision.md")
    retrieved.chunk.id = "memory-1"

    class Adapter:
        capabilities = LtmCapabilities(context_compose_schema=2)

        def __init__(self) -> None:
            self.search = AsyncMock(side_effect=AssertionError("legacy search must not run"))
            self.context_compose = AsyncMock(
                return_value=ContextComposeResult((pinned,), (retrieved,))
            )

        async def scratch_list(self, **kwargs):
            return []

    adapter = Adapter()
    config = SurfacingConfig(
        min_response_chars=0,
        min_query_tokens=1,
        cooldown_seconds=0,
        fire_webhook=False,
        default_namespace="work",
        context_window_size=2,
    )
    engine = SurfacingEngine(config, mcp_adapter=adapter)
    output = await engine.surface(
        "docs", "read_file", {}, "response", context_query="deployment policy"
    )
    assert "Pinned" in output
    assert "always follow review policy" in output
    assert "blue-green deployment" in output
    assert "`policy`" not in output
    adapter.context_compose.assert_awaited_once_with(
        "deployment policy",
        max_chars=3000,
        top_k=6,
        namespace="work",
        context_window=2,
        trace_id=None,
    )
    adapter.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_engine_expands_only_schema_three_wire_budget_for_context() -> None:
    class Adapter:
        capabilities = LtmCapabilities(context_compose_schema=3)

        def __init__(self) -> None:
            self.search = AsyncMock(side_effect=AssertionError("legacy search must not run"))
            self.context_compose = AsyncMock(return_value=ContextComposeResult((), ()))

        async def scratch_list(self, **kwargs):
            return []

    adapter = Adapter()
    engine = SurfacingEngine(
        SurfacingConfig(
            min_response_chars=0,
            min_query_tokens=1,
            cooldown_seconds=0,
            fire_webhook=False,
            max_injection_chars=3000,
            context_window_size=2,
        ),
        mcp_adapter=adapter,
    )

    await engine.surface("docs", "read_file", {}, "response", context_query="deployment")

    adapter.context_compose.assert_awaited_once_with(
        "deployment",
        max_chars=15_000,
        top_k=6,
        namespace=None,
        context_window=2,
        trace_id=None,
    )


@pytest.mark.asyncio
async def test_direct_schema_two_compose_forwards_scope_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = McpClientSearchAdapter(SurfacingConfig(result_format="structured"))
    adapter._capabilities = LtmCapabilities(context_compose_schema=2)
    monkeypatch.setattr(adapter, "_heal_if_needed", AsyncMock(return_value=True))
    call = AsyncMock(
        return_value=SimpleNamespace(
            isError=False,
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        {
                            "pinned": [],
                            "retrieved": [
                                {
                                    "id": "memory-1",
                                    "content": "decision",
                                    "source": "decision.md",
                                    "namespace": "work",
                                    "score": 0.8,
                                }
                            ],
                        }
                    ),
                )
            ],
        )
    )
    monkeypatch.setattr(adapter, "_call_mem_do", call)

    bundle = await adapter.context_compose(
        "deployment",
        max_chars=2000,
        top_k=6,
        namespace=["work"],
        context_window=2,
        trace_id="trace-1",
    )

    assert bundle is not None and bundle.retrieved[0].chunk.metadata.namespace == "work"
    call.assert_awaited_once_with(
        "context_compose",
        {
            "query": "deployment",
            "max_chars": 2000,
            "top_k": 6,
            "namespace": ["work"],
            "context_window": 2,
        },
        trace_id="trace-1",
        refresh_params=adapter._refresh_rerank_arg,
    )


def _compose_adapter(monkeypatch: pytest.MonkeyPatch, payload: object, *, schema: int = 2):
    adapter = McpClientSearchAdapter(SurfacingConfig(result_format="structured"))
    adapter._capabilities = LtmCapabilities(context_compose_schema=schema)
    monkeypatch.setattr(adapter, "_heal_if_needed", AsyncMock(return_value=True))
    monkeypatch.setattr(
        adapter,
        "_call_mem_do",
        AsyncMock(
            return_value=SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            )
        ),
    )
    return adapter


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # A renamed top-level key used to read back as ``[]`` and reach the
        # engine as a healthy empty bundle instead of a diagnosable fault.
        ({"blocks": [], "retrieved": []}, "missing required key\\(s\\): pinned"),
        ({"pinned": []}, "missing required key\\(s\\): retrieved"),
        ({"hits": []}, "missing required key\\(s\\): pinned, retrieved"),
        ({"pinned": {}, "retrieved": []}, "key 'pinned' is not an array"),
        ({"pinned": [], "retrieved": "none"}, "key 'retrieved' is not an array"),
    ],
)
@pytest.mark.asyncio
async def test_direct_compose_rejects_missing_or_malformed_top_level_keys(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    expected: str,
) -> None:
    adapter = _compose_adapter(monkeypatch, payload, schema=3)

    with pytest.raises(ValueError, match=expected):
        await adapter.context_compose("deployment")


@pytest.mark.asyncio
async def test_direct_compose_error_names_the_negotiated_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _compose_adapter(monkeypatch, {"retrieved": []}, schema=2)

    with pytest.raises(ValueError, match=r"core context_compose \(schema 2\)"):
        await adapter.context_compose("deployment")


@pytest.mark.asyncio
async def test_direct_compose_accepts_present_but_empty_required_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: a genuinely empty bundle stays a bundle, not an error."""
    adapter = _compose_adapter(monkeypatch, {"pinned": [], "retrieved": []})

    bundle = await adapter.context_compose("deployment")

    assert bundle is not None
    assert bundle.pinned == () and bundle.retrieved == ()


@pytest.mark.asyncio
async def test_direct_compose_stamps_score_scale_and_reranker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compose schema-4 core (#1796) names the scale on the envelope; STM
    carries it on the bundle and stamps every retrieved result, never pinned."""
    payload = {
        "pinned": [{"content": "pinned block", "block_id": "pin-1"}],
        "retrieved": [
            {"id": "hit-1", "content": "matched one", "source": "a.md", "score": 0.42},
            {"id": "hit-2", "content": "matched two", "source": "b.md", "score": -0.17},
        ],
        "score_scale": "rerank",
        "reranker": "jina-reranker-v2",
    }
    adapter = _compose_adapter(monkeypatch, payload, schema=4)

    bundle = await adapter.context_compose("deployment")

    assert bundle is not None
    assert bundle.score_scale == "rerank"
    assert bundle.reranker == "jina-reranker-v2"
    assert [r.score_scale for r in bundle.retrieved] == ["rerank", "rerank"]
    assert [r.reranker for r in bundle.retrieved] == ["jina-reranker-v2", "jina-reranker-v2"]
    # Pinned blocks never carry a scale.
    assert all(p.score_scale is None and p.reranker is None for p in bundle.pinned)


@pytest.mark.asyncio
async def test_direct_compose_scale_absent_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-#1796 core (or empty retrieved) omits both keys → all-None."""
    payload = {
        "pinned": [],
        "retrieved": [{"id": "hit", "content": "matched", "source": "a.md", "score": 0.8}],
    }
    adapter = _compose_adapter(monkeypatch, payload, schema=3)

    bundle = await adapter.context_compose("deployment")

    assert bundle is not None
    assert bundle.score_scale is None and bundle.reranker is None
    assert bundle.retrieved[0].score_scale is None
    assert bundle.retrieved[0].reranker is None


@pytest.mark.asyncio
async def test_direct_compose_ignores_non_string_scale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-string / empty-string envelope values degrade to None."""
    payload = {
        "pinned": [],
        "retrieved": [{"id": "hit", "content": "matched", "source": "a.md", "score": 0.8}],
        "score_scale": 123,
        "reranker": "",
    }
    adapter = _compose_adapter(monkeypatch, payload, schema=4)

    bundle = await adapter.context_compose("deployment")

    assert bundle is not None
    assert bundle.score_scale is None and bundle.reranker is None
    assert bundle.retrieved[0].score_scale is None


@pytest.mark.asyncio
async def test_direct_compose_reads_scale_opportunistically_below_schema_four(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decode is presence-based, not schema-gated: if the keys are on the
    payload STM stamps them even when capabilities lag at schema 2 (the same
    opportunistic contract the mem_search parser uses)."""
    payload = {
        "pinned": [],
        "retrieved": [{"id": "hit", "content": "matched", "source": "a.md", "score": 0.5}],
        "score_scale": "bm25",
    }
    adapter = _compose_adapter(monkeypatch, payload, schema=2)

    bundle = await adapter.context_compose("deployment")

    assert bundle is not None
    assert bundle.score_scale == "bm25"
    assert bundle.retrieved[0].score_scale == "bm25"


@pytest.mark.asyncio
async def test_direct_schema_three_compose_parses_adjacent_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = McpClientSearchAdapter(
        SurfacingConfig(result_format="structured", result_content_max_chars=8)
    )
    adapter._capabilities = LtmCapabilities(context_compose_schema=3)
    monkeypatch.setattr(adapter, "_heal_if_needed", AsyncMock(return_value=True))
    monkeypatch.setattr(
        adapter,
        "_call_mem_do",
        AsyncMock(
            return_value=SimpleNamespace(
                isError=False,
                content=[
                    SimpleNamespace(
                        type="text",
                        text=json.dumps(
                            {
                                "pinned": [],
                                "retrieved": [
                                    {
                                        "id": "hit",
                                        "content": "matched",
                                        "source": "memory.md",
                                        "namespace": "work",
                                        "score": 0.8,
                                        "context": {
                                            "before": [
                                                {
                                                    "id": "before-0",
                                                    "content": "older-context",
                                                    "source": "memory.md",
                                                    "namespace": "work",
                                                },
                                                {
                                                    "id": "before-1",
                                                    "content": "before-context",
                                                    "source": "memory.md",
                                                    "namespace": "work",
                                                },
                                            ],
                                            "after": [
                                                {
                                                    "id": "after-1",
                                                    "content": "after-context",
                                                    "source": "memory.md",
                                                    "namespace": "work",
                                                },
                                                {
                                                    "id": "after-2",
                                                    "content": "farther-context",
                                                    "source": "memory.md",
                                                    "namespace": "work",
                                                },
                                            ],
                                            "chunk_position": 2,
                                            "total_chunks_in_file": 3,
                                        },
                                    }
                                ],
                            }
                        ),
                    )
                ],
            )
        ),
    )

    bundle = await adapter.context_compose("deployment", context_window=1)

    assert bundle is not None
    context = bundle.retrieved[0].context
    assert context is not None
    assert context.window_before[0].id == "before-1"
    assert context.window_before[0].content == "before-c"
    assert context.window_after[0].id == "after-1"
    assert context.window_after[0].content == "after-co"
    assert context.chunk_position == 2
    assert context.total_chunks_in_file == 3


def _adjacent_chunk(index: int, *, prefix: str = "chunk") -> dict[str, str]:
    return {
        "id": f"{prefix}-{index}",
        "content": f"content-{index}",
        "source": "memory.md",
        "namespace": "work",
    }


@pytest.mark.parametrize(
    ("context_window", "expected_before", "expected_after"),
    [
        (None, list(range(2, 12)), list(range(10))),
        (2, [10, 11], [0, 1]),
        (20, list(range(2, 12)), list(range(10))),
        (0, [], []),
    ],
)
def test_schema_three_context_bounds_count_and_preserves_nearest_order(
    context_window: int | None,
    expected_before: list[int],
    expected_after: list[int],
) -> None:
    context = decode_context_compose_context(
        {
            "before": [_adjacent_chunk(index, prefix="before") for index in range(12)],
            "after": [_adjacent_chunk(index, prefix="after") for index in range(12)],
        },
        max_content_chars=100,
        context_window=context_window,
    )

    assert [chunk.id for chunk in context.window_before] == [
        f"before-{index}" for index in expected_before
    ]
    assert [chunk.id for chunk in context.window_after] == [
        f"after-{index}" for index in expected_after
    ]


def test_schema_three_context_ignores_malformed_overflow_but_rejects_retained() -> None:
    valid = [_adjacent_chunk(index) for index in range(10)]
    malformed = {"id": "bad", "content": 1, "source": "memory.md"}

    context = decode_context_compose_context(
        {"before": [malformed, *valid], "after": []},
        max_content_chars=100,
        context_window=None,
    )
    assert len(context.window_before) == 10

    with pytest.raises(ValueError, match="adjacent chunk"):
        decode_context_compose_context(
            {"before": [*valid[1:], malformed], "after": []},
            max_content_chars=100,
            context_window=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [
        "not-an-object",
        {"before": {}, "after": []},
        {"before": [{"id": "b", "content": 1, "source": "x"}], "after": []},
        {"before": [], "after": [], "chunk_position": True},
    ],
)
async def test_direct_schema_three_rejects_malformed_context(
    monkeypatch: pytest.MonkeyPatch, context: object
) -> None:
    adapter = McpClientSearchAdapter(SurfacingConfig(result_format="structured"))
    adapter._capabilities = LtmCapabilities(context_compose_schema=3)
    monkeypatch.setattr(adapter, "_heal_if_needed", AsyncMock(return_value=True))
    monkeypatch.setattr(
        adapter,
        "_call_mem_do",
        AsyncMock(
            return_value=SimpleNamespace(
                isError=False,
                content=[
                    SimpleNamespace(
                        type="text",
                        text=json.dumps(
                            {
                                "pinned": [],
                                "retrieved": [
                                    {
                                        "id": "hit",
                                        "content": "matched",
                                        "source": "memory.md",
                                        "namespace": "work",
                                        "score": 0.8,
                                        "context": context,
                                    }
                                ],
                            }
                        ),
                    )
                ],
            )
        ),
    )

    with pytest.raises(ValueError, match="context compose"):
        await adapter.context_compose("deployment", context_window=1)


@pytest.mark.asyncio
async def test_schema_one_falls_back_to_legacy_with_scope_fields() -> None:
    class Adapter:
        capabilities = LtmCapabilities(context_compose_schema=1)
        context_compose = AsyncMock(side_effect=AssertionError("schema 1 compose must not run"))
        search = AsyncMock(return_value=([], [], "empty_results"))

        async def scratch_list(self, **kwargs):
            return []

    adapter = Adapter()
    engine = SurfacingEngine(
        SurfacingConfig(
            min_response_chars=0,
            min_query_tokens=1,
            cooldown_seconds=0,
            fire_webhook=False,
            default_namespace="work",
            context_window_size=2,
        ),
        mcp_adapter=adapter,
    )

    assert await engine.surface("docs", "read_file", {}, "response", context_query="q") == (
        "response"
    )
    adapter.context_compose.assert_not_awaited()
    adapter.search.assert_awaited_once_with(
        query="q", top_k=6, namespace="work", context_window=2, trace_id=None
    )


@pytest.mark.asyncio
async def test_compose_failure_is_classified_without_legacy_retry() -> None:
    class Adapter:
        capabilities = LtmCapabilities(context_compose_schema=3)

        def __init__(self) -> None:
            self.search = AsyncMock(side_effect=AssertionError("must not retry legacy search"))

        async def context_compose(self, *args, **kwargs):
            raise RuntimeError("core failed")

        async def scratch_list(self, **kwargs):
            return []

    adapter = Adapter()
    observability = SurfacingObservability()
    engine = SurfacingEngine(
        SurfacingConfig(
            min_response_chars=0,
            min_query_tokens=1,
            cooldown_seconds=0,
            fire_webhook=False,
        ),
        mcp_adapter=adapter,
        observability=observability,
    )
    assert await engine.surface("docs", "read_file", {}, "response", context_query="q") == (
        "response"
    )
    assert observability.snapshot()["skip_reasons"]["read_file"] == {"ltm_call_failed": 1}
    adapter.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_compose_contract_break_warns_once_per_engine(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Adapter:
        capabilities = LtmCapabilities(context_compose_schema=3)

        def __init__(self) -> None:
            self.search = AsyncMock(side_effect=AssertionError("must not retry legacy search"))

        async def context_compose(self, *args, **kwargs):
            raise ValueError("core context_compose (schema 3) is missing required key(s): pinned")

        async def scratch_list(self, **kwargs):
            return []

    observability = SurfacingObservability()
    engine = SurfacingEngine(
        SurfacingConfig(
            min_response_chars=0,
            min_query_tokens=1,
            cooldown_seconds=0,
            fire_webhook=False,
        ),
        mcp_adapter=Adapter(),
        observability=observability,
    )

    with caplog.at_level(logging.WARNING, logger="memtomem_stm.surfacing.engine"):
        for _ in range(2):
            await engine.surface("docs", "read_file", {}, "response", context_query="q")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    # The operator needs the schema and the offending key, not just a counter.
    assert "missing required key(s): pinned" in warnings[0].getMessage()
    assert "schema 3" in warnings[0].getMessage()
    assert observability.snapshot()["skip_reasons"]["read_file"] == {"ltm_call_failed": 2}


@pytest.mark.asyncio
async def test_healthy_compose_logs_no_degraded_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Positive control for the warn-once latch: no fault, no WARNING."""

    class Adapter:
        capabilities = LtmCapabilities(context_compose_schema=2)

        async def context_compose(self, *args, **kwargs):
            return ContextComposeResult((), (RemoteSearchResult("hit", 0.9, "memory.md"),))

        async def scratch_list(self, **kwargs):
            return []

    engine = SurfacingEngine(
        SurfacingConfig(
            min_response_chars=0,
            min_query_tokens=1,
            cooldown_seconds=0,
            fire_webhook=False,
        ),
        mcp_adapter=Adapter(),
    )

    with caplog.at_level(logging.WARNING, logger="memtomem_stm.surfacing.engine"):
        await engine.surface("docs", "read_file", {}, "response", context_query="q")

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


@pytest.mark.asyncio
async def test_compose_transport_failure_is_ltm_unavailable_without_legacy_retry() -> None:
    class Adapter:
        capabilities = LtmCapabilities(context_compose_schema=2)

        def __init__(self) -> None:
            self.search = AsyncMock(side_effect=AssertionError("must not retry legacy search"))

        async def context_compose(self, *args, **kwargs):
            raise LtmTransportError("transport failed")

        async def scratch_list(self, **kwargs):
            return []

    adapter = Adapter()
    observability = SurfacingObservability()
    engine = SurfacingEngine(
        SurfacingConfig(
            min_response_chars=0,
            min_query_tokens=1,
            cooldown_seconds=0,
            fire_webhook=False,
        ),
        mcp_adapter=adapter,
        observability=observability,
    )
    assert await engine.surface("docs", "read_file", {}, "response", context_query="q") == (
        "response"
    )
    assert observability.snapshot()["skip_reasons"]["read_file"] == {"ltm_unavailable": 1}
    adapter.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_pinned_id_dedups_retrieved_and_survives_cached_invalidation() -> None:
    pinned = RemoteSearchResult("policy", 1.0, "policy", pinned=True)
    pinned.chunk.id = "shared-id"
    duplicate = RemoteSearchResult("duplicate policy", 0.9, "memory.md")
    duplicate.chunk.id = "shared-id"

    class Adapter:
        capabilities = LtmCapabilities(context_compose_schema=2)

        async def context_compose(self, *args, **kwargs):
            return ContextComposeResult((pinned,), (duplicate,))

        async def search(self, *args, **kwargs):
            raise AssertionError("legacy search must not run")

        async def scratch_list(self, **kwargs):
            return []

    engine = SurfacingEngine(
        SurfacingConfig(
            min_response_chars=0,
            min_query_tokens=1,
            cooldown_seconds=0,
            fire_webhook=False,
        ),
        mcp_adapter=Adapter(),
    )
    first = await engine.surface("docs", "read_file", {}, "response", context_query="q")
    assert "policy" in first
    assert "duplicate policy" not in first

    engine._invalidated_ids[("docs", "read_file", "shared-id")] = None
    cached = await engine.surface("docs", "read_file", {}, "response", context_query="q")
    assert "policy" in cached


@pytest.mark.asyncio
async def test_formation_is_opt_in_and_never_direct_writes() -> None:
    config = STMConfig()
    disabled = json.loads(
        await stm_memory_propose("Decision: use blue-green", ctx=_ctx(config, None))
    )
    assert disabled == {"ok": False, "reason": "formation_disabled"}

    config.formation.enabled = True
    engine = AsyncMock()
    engine.propose_candidate.return_value = {"candidate_id": "candidate-1"}
    result = json.loads(
        await stm_memory_propose(
            "Decision: use blue-green",
            source_ref="docs/read_file/trace-1",
            ctx=_ctx(config, engine),
        )
    )
    assert result["candidate_id"] == "candidate-1"
    assert result["status"] == "pending"
    engine.propose_candidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_formation_unsupported_has_no_mem_add_fallback() -> None:
    config = STMConfig()
    config.formation.enabled = True
    engine = AsyncMock()
    engine.propose_candidate.return_value = None
    result = json.loads(await stm_memory_propose("Preference: concise", ctx=_ctx(config, engine)))
    assert result == {"ok": False, "reason": "formation_unsupported"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "source_ref", "reason"),
    [
        ("   ", "", "content_empty"),
        ("x" * 2_001, "", "content_too_large"),
        ("valid", "r" * 513, "source_ref_too_large"),
    ],
)
async def test_formation_validation_and_response_whitelist(
    content: str, source_ref: str, reason: str
) -> None:
    config = STMConfig()
    config.formation.enabled = True
    engine = AsyncMock()
    result = json.loads(
        await stm_memory_propose(content, source_ref=source_ref, ctx=_ctx(config, engine))
    )
    assert result["reason"] == reason
    engine.propose_candidate.assert_not_awaited()


async def _propose(config: STMConfig, engine: AsyncMock, **kwargs: str) -> dict:
    """Run the tool and return its parsed reply."""
    return json.loads(await stm_memory_propose(**kwargs, ctx=_ctx(config, engine)))


def _delivered(engine: AsyncMock) -> dict[str, str]:
    """The three strings the tool handed to ``propose_candidate``."""
    args, awaited = engine.propose_candidate.await_args
    return {
        "content": args[0],
        "source_ref": awaited["source_ref"],
        "idempotency_key": awaited["idempotency_key"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["content", "source_ref", "idempotency_key"])
@pytest.mark.parametrize("surrogate", SURROGATES)
async def test_formation_escapes_lone_surrogates_in_client_arguments(
    field: str, surrogate: str
) -> None:
    """A surrogate in any of the three client strings yields a candidate (#777).

    ``content`` and ``source_ref`` used to raise ``UnicodeEncodeError`` out of
    the tool: the fallback idempotency hash ``.encode()``\\ s them above the
    ``try``, so the client saw a traceback rather than a structured reply.
    ``idempotency_key`` skipped that hash but still reached the SDK's
    serialization of the outbound ``mem_do`` params. Escaping all three at
    entry makes the whole outbound path total, so the value is delivered
    rather than rejected.
    """
    config = STMConfig()
    config.formation.enabled = True
    engine = AsyncMock()
    engine.propose_candidate.return_value = {"candidate_id": "candidate-1"}
    kwargs = {
        "content": "Decision: use blue-green",
        "source_ref": "docs/read_file/trace-1",
        # Empty unless it is the field under test: a client-supplied key skips
        # the fallback hash, which is the expression that actually raised for
        # the other two. Passing one here would test neither path.
        "idempotency_key": "client-key-1" if field == "idempotency_key" else "",
    }
    kwargs[field] += surrogate

    result = await _propose(config, engine, **kwargs)

    assert result["candidate_id"] == "candidate-1"
    engine.propose_candidate.assert_awaited_once()
    delivered = _delivered(engine)
    assert f"\\u{ord(surrogate):04x}" in delivered[field]
    for value in delivered.values():
        assert surrogate not in value
        value.encode("utf-8")  # the call that used to raise
    if field != "idempotency_key":
        # Reached the fallback at all, and the digest ran over escaped input.
        assert re.fullmatch(r"[0-9a-f]{64}", delivered["idempotency_key"])


@pytest.mark.asyncio
async def test_formation_refuses_oversize_without_escaping_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The limits bound the WORK, not only the outcome (#777).

    Escaping scans and can expand its input sixfold, so it must not run on a
    value already known to be too large. Only the raw pre-check enforces that:
    dropping it leaves every functional assertion in this file passing while
    restoring the unbounded scan, so this pins the call itself.
    """
    seen: list[int] = []
    real = server_module.escape_lone_surrogates

    def spy(text: str) -> str:
        seen.append(len(text))
        return real(text)

    monkeypatch.setattr(server_module, "escape_lone_surrogates", spy)
    config = STMConfig()
    config.formation.enabled = True
    engine = AsyncMock()

    oversize = await _propose(
        config, engine, content="\ud800" * (config.formation.max_content_chars + 1)
    )

    assert oversize["reason"] == "content_too_large"
    assert seen == []
    engine.propose_candidate.assert_not_awaited()

    # The in-limit path still escapes, so the empty list above is the
    # pre-check refusing and not the spy failing to be installed.
    engine.propose_candidate.return_value = {"candidate_id": "candidate-1"}
    await _propose(config, engine, content="Decision: use blue-green")
    assert seen


@pytest.mark.asyncio
@pytest.mark.parametrize("surrogate", SURROGATES)
async def test_formation_derived_key_is_deterministic_across_surrogates(surrogate: str) -> None:
    """Escaping keeps the fallback key idempotent (#777).

    The key exists to deduplicate retries, so the same client input must derive
    the same digest — and a different one must not collide onto it.
    """
    config = STMConfig()
    config.formation.enabled = True
    keys = []
    for content in (f"Decision{surrogate} A", f"Decision{surrogate} A", f"Decision{surrogate} B"):
        engine = AsyncMock()
        engine.propose_candidate.return_value = {"candidate_id": "candidate-1"}
        await _propose(config, engine, content=content)
        keys.append(_delivered(engine)["idempotency_key"])

    assert keys[0] == keys[1]
    assert keys[0] != keys[2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "limit", "reason"),
    [
        ("content", 2_000, "content_too_large"),
        ("source_ref", 512, "source_ref_too_large"),
        ("idempotency_key", 256, "idempotency_key_too_large"),
    ],
)
@pytest.mark.parametrize("surrogate", SURROGATES)
async def test_formation_limits_measure_the_escaped_form(
    field: str, limit: int, reason: str, surrogate: str
) -> None:
    """Each limit is denominated in what is sent, not in what arrived (#777).

    One code unit escapes to six characters, so the two denominations differ
    for a surrogate-bearing value. The escaped one is the load-bearing choice:
    in daemon mode these same three limits are re-applied to the escaped
    strings by ``daemon/server.py``, and measuring the raw form here would
    accept a value the daemon then refuses as an opaque ``status="invalid"``.
    The cross-boundary half of this is pinned in
    ``tests/daemon/test_shared_ltm.py``.
    """
    config = STMConfig()
    config.formation.enabled = True
    # Escapes to exactly ``limit`` characters: six per surrogate, plus filler.
    at_limit = surrogate * (limit // 6) + "a" * (limit % 6)
    other = {"content": "Decision: use blue-green"} if field != "content" else {}

    engine = AsyncMock()
    engine.propose_candidate.return_value = {"candidate_id": "candidate-1"}
    accepted = await _propose(config, engine, **{field: at_limit}, **other)
    assert accepted["candidate_id"] == "candidate-1"
    assert len(_delivered(engine)[field]) == limit

    over = AsyncMock()
    rejected = await _propose(config, over, **{field: at_limit + surrogate}, **other)
    assert rejected["reason"] == reason
    over.propose_candidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_formation_response_drops_untrusted_keys() -> None:
    config = STMConfig()
    config.formation.enabled = True
    engine = AsyncMock()
    engine.propose_candidate.return_value = {
        "candidate_id": "candidate-1",
        "status": "pending",
        "internal_secret": "must-not-leak",
    }
    result = json.loads(await stm_memory_propose("valid", ctx=_ctx(config, engine)))
    assert result["candidate_id"] == "candidate-1"
    assert "internal_secret" not in result
