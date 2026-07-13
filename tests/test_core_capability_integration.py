"""Capability-gated memtomem core integration contracts."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.config import STMConfig
from memtomem_stm.proxy.config import ProxyConfig
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics import TokenTracker
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
)
from memtomem_stm.surfacing.observability import SurfacingObservability


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
        namespace=None,
        context_window=None,
        trace_id=None,
    )
    adapter.search.assert_not_awaited()


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
    )


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
        capabilities = LtmCapabilities(context_compose_schema=2)

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
