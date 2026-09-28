"""Runtime log message lines carry no URL query, argument or header echo (#1082 part 2).

The log-side twin of ``test_runtime_value_audit.py``. Every site below logs an
exception from an upstream, the LTM server, an LLM or embedding endpoint, a
webhook, or the tool-graph provider — text that can quote STM's request back —
or a configured URL. Each case drives the real code path with an exception
that echoes the canary URL (query and fragment included), an ``--opt=value``
argument and a header token, captures every record at DEBUG, and asserts:

* no canary in any record's rendered message (``getMessage()``), and
* the fixed vocabulary is there — the positive control that the site ran.

Categories, so a new site has a place to go:

* LTM client (``surfacing/mcp_client.py``): every ``_scrub_exc`` line and the
  two ``_target_display`` lines;
* surfacing engine: abandoned op, compose degraded, LTM unreachable, webhook;
* proxy manager: LLM destination warning, tool-graph risk enrichment and
  consult failure (including the ``fail_start`` exception), background task,
  extractor unavailable;
* LLM compression / extraction, auto-index / fact indexing, embedding scorer.

Tracebacks (``exc_info``) are out of scope here (#1086): only the message.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

_URL = "http://cnryUser:cnryPass@127.0.0.1:9/mcp?token=cnryQueryTok#cnryFrag"
_CANARIES = ("cnryUser", "cnryPass", "cnryQueryTok", "cnryFrag", "cnryArgVal", "cnryHdrVal")


def _http_error() -> httpx.HTTPStatusError:
    """An HTTP 401 whose message quotes the URL, an argument and a header token."""
    request = httpx.Request("POST", _URL)
    response = httpx.Response(401, request=request)
    return httpx.HTTPStatusError(
        f"Client error '401 Unauthorized' for url '{_URL}': "
        "rejected --api-key=cnryArgVal (token cnryHdrVal)",
        request=request,
        response=response,
    )


def _echo() -> RuntimeError:
    return RuntimeError(f"rejected --api-key=cnryArgVal (token cnryHdrVal) for {_URL}")


def _messages(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def _assert_clean(text: str, expected: str) -> None:
    leaked = [c for c in _CANARIES if c in text]
    assert leaked == [], f"canaries in log: {leaked}\n{text}"
    assert expected in text, text


# ── LTM client ────────────────────────────────────────────────────────────


def _ltm_config(**overrides: Any):
    from memtomem_stm.surfacing.config import SurfacingConfig

    return SurfacingConfig(**{"ltm_mcp_transport": "sse", "ltm_mcp_url": _URL, **overrides})


async def test_ltm_lazy_start_failure_line(caplog):
    from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

    adapter = McpClientSearchAdapter(_ltm_config())

    async def _boom() -> None:
        raise _http_error()

    adapter.start = _boom  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.surfacing.mcp_client"):
        assert await adapter._heal_if_needed() is False
    text = _messages(caplog)
    assert "surfacing disabled" in text
    _assert_clean(text, "HTTP 401 (HTTPStatusError)")


async def test_ltm_search_call_error_line(caplog):
    from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

    adapter = McpClientSearchAdapter(_ltm_config())
    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=_echo())
    adapter._session = session
    adapter._reconnect = AsyncMock()  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.surfacing.mcp_client"):
        _results, _hints, outcome = await adapter.search("q")
    assert outcome == "call_error"
    _assert_clean(_messages(caplog), "MCP mem_search failed: RuntimeError")


async def test_ltm_version_negotiation_failure_line(caplog):
    from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

    adapter = McpClientSearchAdapter(_ltm_config())
    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=_echo())
    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.surfacing.mcp_client"):
        await adapter._negotiate_format(session)
    _assert_clean(_messages(caplog), "Version negotiation failed (older core?): RuntimeError")


async def test_ltm_scratch_get_failure_line(caplog):
    from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

    adapter = McpClientSearchAdapter(_ltm_config())
    adapter._session = AsyncMock()
    adapter._heal_if_needed = AsyncMock(return_value=True)  # type: ignore[method-assign]
    adapter._rpc = AsyncMock(side_effect=ValueError(str(_echo())))  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.surfacing.mcp_client"):
        assert await adapter.scratch_list() == []
    _assert_clean(_messages(caplog), "MCP mem_do(scratch_get) failed: ValueError")


@pytest.mark.parametrize("transport", ["sse", "stdio"])
async def test_ltm_reconnect_target_line(caplog, transport):
    """The INFO line naming the reconnect target: ``diagnostic_url`` for a
    network LTM, the bare command for stdio — never its args."""
    from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

    adapter = McpClientSearchAdapter(
        _ltm_config(
            ltm_mcp_transport=transport,
            ltm_mcp_command="memtomem-server",
            ltm_mcp_args=["--api-key=cnryArgVal"],
        )
    )
    adapter._submit = AsyncMock()  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.surfacing.mcp_client"):
        await adapter._reconnect()
    expected = "http://***@127.0.0.1:9/mcp" if transport == "sse" else "to memtomem-server"
    _assert_clean(_messages(caplog), expected)


def test_ltm_target_display_network_drops_query():
    from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

    shown = McpClientSearchAdapter(_ltm_config())._target_display()
    assert shown == "http://***@127.0.0.1:9/mcp"


def test_ltm_target_display_stdio_shows_command_not_args():
    from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

    adapter = McpClientSearchAdapter(
        _ltm_config(
            ltm_mcp_transport="stdio",
            ltm_mcp_command="memtomem-server",
            ltm_mcp_args=["--api-key=cnryArgVal"],
        )
    )
    shown = adapter._target_display()
    assert shown == "memtomem-server"
    assert "cnryArgVal" not in shown


# ── surfacing engine ──────────────────────────────────────────────────────


def _engine(adapter: Any, **config: Any):
    from memtomem_stm.surfacing.config import SurfacingConfig
    from memtomem_stm.surfacing.engine import SurfacingEngine

    defaults: dict[str, Any] = {
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
        "query_retention_days": 0,
        "stats_retention_days": 0,
        "ltm_mcp_transport": "sse",
        "ltm_mcp_url": _URL,
    }
    defaults.update(config)
    return SurfacingEngine(config=SurfacingConfig(**defaults), mcp_adapter=adapter)


_ARGS = {"path": "src/app.py", "_context_query": "Flask web framework architecture"}
_RESPONSE = "x" * 200


async def test_engine_compose_degraded_line(caplog):
    from memtomem_stm.surfacing.mcp_client import LtmCapabilities

    adapter = AsyncMock()
    adapter.capabilities = LtmCapabilities(context_compose_schema=4)
    adapter.context_compose = AsyncMock(side_effect=_echo())
    engine = _engine(adapter)
    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.surfacing.engine"):
        await engine.surface("s", "read_file", _ARGS, _RESPONSE)
    text = _messages(caplog)
    assert "Surfacing degraded" in text
    _assert_clean(text, "call failed (RuntimeError)")


async def test_engine_ltm_unreachable_line(caplog):
    adapter = AsyncMock()
    adapter.search = AsyncMock(return_value=([], [], "no_session"))
    engine = _engine(adapter)
    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.surfacing.engine"):
        await engine.surface("s", "read_file", _ARGS, _RESPONSE)
    _assert_clean(_messages(caplog), "'http://***@127.0.0.1:9/mcp' is not reachable")


async def test_engine_webhook_failure_line(caplog):
    from memtomem_stm.surfacing.config import SurfacingConfig
    from memtomem_stm.surfacing.engine import SurfacingEngine

    class _Chunk:
        content = "mem"
        metadata = SimpleNamespace(source_file=Path("/m.md"), namespace="default", tags=())
        id = "c1"

    result = SimpleNamespace(chunk=_Chunk(), score=0.5, score_scale=None, reranker=None)
    adapter = AsyncMock()
    adapter.search = AsyncMock(return_value=([result], [], "ok"))
    webhooks = SimpleNamespace(fire=AsyncMock(side_effect=_http_error()))
    engine = SurfacingEngine(
        config=SurfacingConfig(
            enabled=True,
            min_response_chars=10,
            min_score=0.02,
            cooldown_seconds=0.0,
            max_surfacings_per_minute=1000,
            auto_tune_enabled=False,
            include_session_context=False,
            fire_webhook=True,
            query_retention_days=0,
            stats_retention_days=0,
        ),
        mcp_adapter=adapter,
        webhook_manager=webhooks,
    )
    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.surfacing.engine"):
        await engine.surface("s", "read_file", _ARGS, _RESPONSE)
        await asyncio.gather(*list(engine._background_tasks), return_exceptions=True)
    text = _messages(caplog)
    assert "Webhook fire-and-forget task failed" in text
    _assert_clean(text, "HTTP 401 (HTTPStatusError)")


async def test_engine_abandoned_op_line(caplog):
    engine = _engine(AsyncMock())

    async def _fail() -> None:
        raise _echo()

    task = asyncio.ensure_future(_fail())
    await asyncio.gather(task, return_exceptions=True)
    engine._abandoned_ops.add(task)
    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.surfacing.engine"):
        engine._on_abandoned_op_done(task)
    _assert_clean(_messages(caplog), "failed while unwinding: RuntimeError")


# ── proxy manager ─────────────────────────────────────────────────────────


def test_llm_destination_drops_query_and_userinfo():
    from memtomem_stm.proxy.config import LLMCompressorConfig, LLMProvider
    from memtomem_stm.proxy.manager import _describe_llm_destination

    shown = _describe_llm_destination(
        LLMCompressorConfig(provider=LLMProvider.OLLAMA, base_url=_URL)
    )
    _assert_clean(shown, "ollama (http://***@127.0.0.1:9/mcp)")


@pytest.mark.parametrize("stage", ["compression", "extraction"])
async def test_llm_destination_warning_line(tmp_path, caplog, stage):
    """The startup warning that raw responses go UNSCANNED to an LLM names the
    destination through ``diagnostic_url``."""
    from memtomem_stm.proxy.config import (
        CompressionStrategy,
        ExtractionConfig,
        ExtractionStrategy,
        LLMCompressorConfig,
        LLMProvider,
        ProxyConfig,
        UpstreamServerConfig,
    )
    from memtomem_stm.proxy.manager import ProxyManager
    from memtomem_stm.proxy.metrics import TokenTracker

    external = "https://cnryUser:cnryPass@llm.example/v1?token=cnryQueryTok#cnryFrag"
    llm = LLMCompressorConfig(
        provider=LLMProvider.ANTHROPIC,
        api_key="ant-test",
        base_url=external,
        privacy_scan_enabled=False,
    )
    if stage == "extraction":
        config = ProxyConfig(
            enabled=True,
            config_path=tmp_path / "missing-proxy.json",
            extraction=ExtractionConfig(enabled=True, strategy=ExtractionStrategy.LLM, llm=llm),
        )
    else:
        config = ProxyConfig(
            enabled=True,
            config_path=tmp_path / "missing-proxy.json",
            upstream_servers={
                "docs": UpstreamServerConfig(
                    prefix="docs", compression=CompressionStrategy.LLM_SUMMARY, llm=llm
                )
            },
        )
    mgr = ProxyManager(config, TokenTracker(), index_engine=object())
    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.proxy.manager"):
        await mgr.start()
    try:
        text = _messages(caplog)
        assert "UNSCANNED" in text
        _assert_clean(text, "anthropic (https://***@llm.example/v1)")
    finally:
        await mgr.stop()


def _manager(tmp_path: Path, **proxy: Any):
    from memtomem_stm.proxy.config import ProxyConfig
    from memtomem_stm.proxy.manager import ProxyManager
    from memtomem_stm.proxy.metrics import TokenTracker

    return ProxyManager(ProxyConfig(config_path=tmp_path / "proxy.json", **proxy), TokenTracker())


async def test_background_task_failure_line(tmp_path, caplog):
    mgr = _manager(tmp_path)

    async def _fail() -> None:
        raise _http_error()

    task = asyncio.ensure_future(_fail())
    await asyncio.gather(task, return_exceptions=True)
    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.proxy.manager"):
        mgr._on_background_task_done("auto_index", "srv", "tool", task)
    text = _messages(caplog)
    assert "Background auto_index task failed for srv/tool" in text
    _assert_clean(text, "HTTP 401 (HTTPStatusError)")


async def test_extractor_unavailable_line(tmp_path, caplog):
    mgr = _manager(tmp_path)
    with (
        patch.object(mgr, "_get_extractor", AsyncMock(side_effect=_echo())),
        caplog.at_level(logging.DEBUG, logger="memtomem_stm.proxy.manager"),
    ):
        await mgr._extract_and_store("srv", "tool", {}, "body", cfg_snap=mgr._config)
    _assert_clean(_messages(caplog), "Extraction unavailable for srv/tool: RuntimeError")


def _failing_adapter(kind: str) -> type:
    from memtomem_stm.proxy.toolgraph_provider import (
        ToolgraphProtocolError,
        ToolgraphUnreachableError,
    )

    error = ToolgraphUnreachableError if kind == "unreachable" else ToolgraphProtocolError

    class _Adapter:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def start(self) -> None:
            raise error(f"tool-graph at {_URL} said --api-key=cnryArgVal (token cnryHdrVal)")

        async def stop(self) -> None:
            pass

    return _Adapter


def _tg_manager(tmp_path: Path, **knobs: str):
    from memtomem_stm.proxy.config import ToolgraphConfig, UpstreamServerConfig
    from memtomem_stm.proxy.manager import UpstreamConnection

    mgr = _manager(
        tmp_path,
        upstream_servers={"srv": UpstreamServerConfig(prefix="srv")},
        toolgraph=ToolgraphConfig(
            enabled=True,
            command=sys.executable,
            **knobs,
            consult_cache_path=tmp_path / "tg.db",
        ),
    )
    tools = [SimpleNamespace(name="read_file", description="d", input_schema={"type": "object"})]
    mgr._connections["srv"] = UpstreamConnection(
        name="srv", config=UpstreamServerConfig(prefix="srv"), session=AsyncMock(), tools=tools
    )
    return mgr


async def test_toolgraph_risk_enrichment_failure_line(tmp_path, caplog):
    from memtomem_stm.proxy.toolgraph_provider import ToolgraphConsultError

    mgr = _manager(tmp_path)
    adapter = SimpleNamespace(
        rank_features=AsyncMock(side_effect=ToolgraphConsultError(str(_echo())))
    )
    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.proxy.manager"):
        facts, scores, ok = await mgr._fetch_graph_facts(adapter, ["srv::t"])  # type: ignore[arg-type]
    assert (facts, scores, ok) == ({}, {}, False)
    _assert_clean(_messages(caplog), "(rank_features) failed (ToolgraphConsultError)")


@pytest.mark.parametrize("knob", ["open", "closed"])
async def test_toolgraph_consult_failure_line(tmp_path, caplog, knob):
    mgr = _tg_manager(tmp_path, on_unreachable=knob)
    with (
        patch(
            "memtomem_stm.proxy.manager.ToolgraphConsultAdapter", _failing_adapter("unreachable")
        ),
        caplog.at_level(logging.DEBUG, logger="memtomem_stm.proxy.manager"),
    ):
        await mgr._consult_toolgraph()
    text = _messages(caplog)
    assert "Tool-graph consult failed" in text
    _assert_clean(text, "ToolgraphUnreachableError")


async def test_toolgraph_fail_start_exception_text(tmp_path):
    """``fail_start`` raises before any warning, so the exception is the surface."""
    from memtomem_stm.proxy.manager import ToolgraphStartupError

    mgr = _tg_manager(tmp_path, on_protocol_error="fail_start")
    with (
        patch("memtomem_stm.proxy.manager.ToolgraphConsultAdapter", _failing_adapter("protocol")),
        pytest.raises(ToolgraphStartupError) as raised,
    ):
        await mgr._consult_toolgraph()
    _assert_clean(str(raised.value), "ToolgraphProtocolError")


# ── LLM, indexing, embedding ──────────────────────────────────────────────


async def test_llm_compression_failure_line(caplog):
    from memtomem_stm.proxy.compression import LLMCompressor
    from memtomem_stm.proxy.config import LLMCompressorConfig, LLMProvider

    comp = LLMCompressor(LLMCompressorConfig(provider=LLMProvider.OLLAMA, base_url=_URL))
    with (
        patch.object(comp, "_call_api", AsyncMock(side_effect=_http_error())),
        caplog.at_level(logging.DEBUG, logger="memtomem_stm.proxy.compression"),
    ):
        result = await comp.compress("word " * 2000, max_chars=100)
    assert result.fallback_reason is not None
    text = _messages(caplog)
    assert "LLM compression failed" in text
    _assert_clean(text, "HTTP 401 (HTTPStatusError)")


async def test_llm_extraction_failure_line(caplog):
    from memtomem_stm.proxy.config import ExtractionConfig
    from memtomem_stm.proxy.extraction import FactExtractor

    extractor = FactExtractor(ExtractionConfig(enabled=True, min_response_chars=10))
    with (
        patch.object(extractor, "_call_api", AsyncMock(side_effect=_http_error())),
        caplog.at_level(logging.DEBUG, logger="memtomem_stm.proxy.extraction"),
    ):
        await extractor.extract("Decision: it works. " * 20, server="s", tool="t")
    text = _messages(caplog)
    assert "LLM extraction failed" in text
    _assert_clean(text, "HTTP 401 (HTTPStatusError)")


class _RaisingIndexer:
    async def index_file(self, path: Path, namespace: str | None = None) -> Any:
        raise _http_error()


async def test_auto_index_failure_line(tmp_path, caplog):
    from memtomem_stm.proxy.config import AutoIndexConfig
    from memtomem_stm.proxy.memory_ops import auto_index_response

    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.proxy.memory_ops"):
        await auto_index_response(
            _RaisingIndexer(),  # type: ignore[arg-type]
            AutoIndexConfig(enabled=True, memory_dir=tmp_path / "idx"),
            server="srv",
            tool="tool",
            arguments={},
            text="body",
            agent_summary="summary",
        )
    _assert_clean(_messages(caplog), "Auto-index failed for srv/tool: HTTP 401 (HTTPStatusError)")


async def test_fact_indexing_failure_line(tmp_path, caplog):
    from memtomem_stm.proxy.config import ExtractionConfig
    from memtomem_stm.proxy.extraction import ExtractedFact
    from memtomem_stm.proxy.memory_ops import extract_and_store

    extractor = SimpleNamespace(
        extract=AsyncMock(
            return_value=[ExtractedFact(content="a fact", category="decision", confidence=0.9)]
        )
    )
    with caplog.at_level(logging.DEBUG, logger="memtomem_stm.proxy.memory_ops"):
        await extract_and_store(
            _RaisingIndexer(),  # type: ignore[arg-type]
            extractor,  # type: ignore[arg-type]
            ExtractionConfig(enabled=True, memory_dir=tmp_path / "facts"),
            server="srv",
            tool="tool",
            arguments={},
            text="body",
        )
    _assert_clean(_messages(caplog), "Fact indexing failed: HTTP 401 (HTTPStatusError)")


def test_embedding_scorer_fallback_line(caplog):
    from memtomem_stm.proxy.relevance import EmbeddingScorer

    scorer = EmbeddingScorer(provider="ollama", base_url=_URL, timeout=0.5)
    with (
        patch.object(scorer, "_score_via_embedding", side_effect=_http_error()),
        caplog.at_level(logging.DEBUG, logger="memtomem_stm.proxy.relevance"),
    ):
        scorer.score_sections("query", [("## t", "body")])
    _assert_clean(
        _messages(caplog),
        "EmbeddingScorer failed, falling back to BM25: HTTP 401 (HTTPStatusError)",
    )


# ── what the vocabulary deliberately keeps ────────────────────────────────


def test_response_shape_errors_keep_their_stm_written_message():
    """STM's own shape checks on a core or provider reply say which side drifted;
    the message carries no request data, so it survives (#1082). Driven through
    the real checks, not a hand-built exception."""
    from memtomem_stm.proxy.relevance import _payload_list
    from memtomem_stm.surfacing.mcp_client import require_context_compose_lists
    from memtomem_stm.utils.redact import exception_summary

    with pytest.raises(ValueError) as compose:
        require_context_compose_lists({"retrieved": []}, origin="core context_compose", schema=3)
    assert exception_summary(compose.value) == (
        "ResponseShapeError: core context_compose (schema 3) is missing required key(s): pinned"
    )

    with pytest.raises(ValueError) as embedding:
        _payload_list({"error": "nope"}, key="embeddings", provider="ollama")
    assert exception_summary(embedding.value) == (
        "ResponseShapeError: ollama embedding response has no 'embeddings' field; it holds: error"
    )


def test_a_plain_value_error_is_still_reduced_to_its_type():
    from memtomem_stm.utils.redact import exception_summary

    assert exception_summary(ValueError(f"bad value in {_URL}")) == "ValueError"


def test_embedding_shape_error_counts_unknown_reply_keys():
    """A key STM does not know is the provider's text: counted, never shown."""
    from memtomem_stm.proxy.relevance import _payload_list

    with pytest.raises(ValueError) as raised:
        _payload_list({"error": "x", "cnryQueryTok": []}, key="embeddings", provider="ollama")
    assert str(raised.value) == (
        "ollama embedding response has no 'embeddings' field; it holds: error (+1 other)"
    )
