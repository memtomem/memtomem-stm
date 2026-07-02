"""Tests for remaining STM modules: _fastmcp_compat, tracing, protocols, metrics, mcp_client."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memtomem_stm.proxy._fastmcp_compat import (
    _PASSTHROUGH_METADATA,
    _ProxyPassthroughArgs,
    register_proxy_tool,
)
from memtomem_stm.proxy.metrics import CallMetrics, TokenTracker
from memtomem_stm.proxy.protocols import FileIndexer, IndexResult
from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter, RemoteSearchResult


# ── _fastmcp_compat ────────────────────────────────────────────────────


class TestProxyPassthroughArgs:
    """Test the _ProxyPassthroughArgs pydantic model for extra-field forwarding."""

    def test_accepts_arbitrary_fields(self) -> None:
        args = _ProxyPassthroughArgs(foo="bar", count=42)
        assert args.__pydantic_extra__ == {"foo": "bar", "count": 42}

    def test_model_dump_one_level_includes_extras(self) -> None:
        args = _ProxyPassthroughArgs(query="test", top_k=5)
        dumped = args.model_dump_one_level()
        assert dumped["query"] == "test"
        assert dumped["top_k"] == 5

    def test_model_dump_one_level_empty(self) -> None:
        args = _ProxyPassthroughArgs()
        dumped = args.model_dump_one_level()
        assert isinstance(dumped, dict)


class TestPassthroughMetadata:
    """Test the singleton _PASSTHROUGH_METADATA FuncMetadata."""

    def test_arg_model_set(self) -> None:
        assert _PASSTHROUGH_METADATA.arg_model is _ProxyPassthroughArgs

    def test_output_schema_is_none(self) -> None:
        assert _PASSTHROUGH_METADATA.output_schema is None

    def test_wrap_output_false(self) -> None:
        assert _PASSTHROUGH_METADATA.wrap_output is False


class TestRegisterProxyTool:
    """Test register_proxy_tool patches the tool manager correctly."""

    def test_register_sets_parameters_and_metadata(self) -> None:
        mock_server = MagicMock()
        mock_tool = MagicMock()
        mock_server._tool_manager._tools.get.return_value = mock_tool

        @dataclass
        class FakeInfo:
            prefixed_name: str = "srv__my_tool"
            description: str = "does things"
            input_schema: dict = None
            annotations: Any = None
            server: str = "srv"

            def __post_init__(self):
                if self.input_schema is None:
                    self.input_schema = {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    }

        info = FakeInfo()
        handler = MagicMock()

        register_proxy_tool(mock_server, handler, info)

        mock_server.add_tool.assert_called_once_with(
            handler,
            name="srv__my_tool",
            description="[proxied] does things",
            annotations=None,
        )
        assert mock_tool.parameters == info.input_schema
        assert mock_tool.fn_metadata is _PASSTHROUGH_METADATA

    def test_register_skips_patch_when_tool_not_found(self) -> None:
        mock_server = MagicMock()
        mock_server._tool_manager._tools.get.return_value = None

        @dataclass
        class FakeInfo:
            prefixed_name: str = "missing__tool"
            description: str = "gone"
            input_schema: dict = None
            annotations: Any = None
            server: str = "missing"

            def __post_init__(self):
                if self.input_schema is None:
                    self.input_schema = {}

        register_proxy_tool(mock_server, MagicMock(), FakeInfo())
        # Should not raise


# ── tracing ─────────────────────────────────────────────────────────────


class TestTracing:
    """Test Langfuse tracing graceful fallbacks."""

    def test_init_disabled_config_returns_none(self) -> None:
        import memtomem_stm.observability.tracing as tracing_mod

        old = tracing_mod._langfuse_client
        try:
            config = MagicMock()
            config.enabled = False
            result = tracing_mod.init_langfuse(config)
            assert result is None
        finally:
            tracing_mod._langfuse_client = old

    def test_init_missing_langfuse_returns_none(self) -> None:
        import memtomem_stm.observability.tracing as tracing_mod

        old = tracing_mod._langfuse_client
        try:
            config = MagicMock()
            config.enabled = True
            # Temporarily make langfuse unimportable
            with patch.dict(sys.modules, {"langfuse": None}):
                result = tracing_mod.init_langfuse(config)
            assert result is None
        finally:
            tracing_mod._langfuse_client = old

    def test_init_with_langfuse_installed(self) -> None:
        import memtomem_stm.observability.tracing as tracing_mod

        old = tracing_mod._langfuse_client
        old_rate = tracing_mod._sampling_rate
        old_env = os.environ.get("OTEL_SERVICE_NAME")
        try:
            mock_langfuse_cls = MagicMock()
            mock_client = MagicMock()
            mock_langfuse_cls.return_value = mock_client
            mock_module = MagicMock()
            mock_module.Langfuse = mock_langfuse_cls

            config = MagicMock()
            config.enabled = True
            config.public_key = "pk-test"
            config.secret_key = "sk-test"
            config.host = "http://localhost:3000"
            # A real float: init_langfuse writes this into the module-global
            # _sampling_rate, and a leaked MagicMock breaks every later
            # traced() sampling comparison in the suite.
            config.sampling_rate = 1.0

            os.environ.pop("OTEL_SERVICE_NAME", None)
            with patch.dict(sys.modules, {"langfuse": mock_module}):
                result = tracing_mod.init_langfuse(config)

            assert result is mock_client
            mock_langfuse_cls.assert_called_once_with(
                public_key="pk-test",
                secret_key="sk-test",
                host="http://localhost:3000",
            )
            assert os.environ.get("OTEL_SERVICE_NAME") == "memtomem-stm"
        finally:
            tracing_mod._langfuse_client = old
            tracing_mod._sampling_rate = old_rate
            if old_env is not None:
                os.environ["OTEL_SERVICE_NAME"] = old_env
            else:
                os.environ.pop("OTEL_SERVICE_NAME", None)

    def test_traced_returns_nullcontext_when_no_client(self) -> None:
        import memtomem_stm.observability.tracing as tracing_mod

        old = tracing_mod._langfuse_client
        try:
            tracing_mod._langfuse_client = None
            ctx = tracing_mod.traced("test-span")
            # nullcontext is usable as a context manager
            with ctx:
                pass
        finally:
            tracing_mod._langfuse_client = old

    def test_traced_degrades_when_client_method_raises(self) -> None:
        # The proxy/surfacing hot paths wrap calls in `with traced(...)`; an
        # SDK that raises while constructing the observation must degrade to
        # untraced, never fail the proxied call.
        import memtomem_stm.observability.tracing as tracing_mod

        old = tracing_mod._langfuse_client
        old_rate = tracing_mod._sampling_rate
        old_warned = tracing_mod._warned_observation_failure
        try:
            tracing_mod._sampling_rate = 1.0
            tracing_mod._warned_observation_failure = False
            client = MagicMock()
            client.start_as_current_observation.side_effect = RuntimeError("exporter down")
            tracing_mod._langfuse_client = client
            ran = False
            with tracing_mod.traced("span"):
                ran = True
            assert ran
        finally:
            tracing_mod._langfuse_client = old
            tracing_mod._sampling_rate = old_rate
            tracing_mod._warned_observation_failure = old_warned

    def test_traced_degrades_when_observation_enter_raises(self) -> None:
        # Langfuse CMs do their real work in __enter__ (OTEL context attach,
        # exporter I/O) — a raising __enter__ must not break the traced body,
        # and the never-entered inner CM must not get an __exit__ call.
        import memtomem_stm.observability.tracing as tracing_mod

        old = tracing_mod._langfuse_client
        old_rate = tracing_mod._sampling_rate
        old_warned = tracing_mod._warned_observation_failure
        try:
            tracing_mod._sampling_rate = 1.0
            tracing_mod._warned_observation_failure = False
            broken_cm = MagicMock()
            broken_cm.__enter__ = MagicMock(side_effect=RuntimeError("otel context corrupt"))
            client = MagicMock()
            client.start_as_current_observation.return_value = broken_cm
            tracing_mod._langfuse_client = client
            ran = False
            with tracing_mod.traced("span"):
                ran = True
            assert ran
            broken_cm.__exit__.assert_not_called()
        finally:
            tracing_mod._langfuse_client = old
            tracing_mod._sampling_rate = old_rate
            tracing_mod._warned_observation_failure = old_warned

    def test_traced_body_exception_still_propagates(self) -> None:
        # Only SDK enter/exit failures are swallowed — an exception raised by
        # the traced body must propagate through the safe wrapper.
        import memtomem_stm.observability.tracing as tracing_mod

        old = tracing_mod._langfuse_client
        old_rate = tracing_mod._sampling_rate
        try:
            tracing_mod._sampling_rate = 1.0
            healthy_cm = MagicMock()
            healthy_cm.__exit__ = MagicMock(return_value=False)
            client = MagicMock()
            client.start_as_current_observation.return_value = healthy_cm
            tracing_mod._langfuse_client = client
            with pytest.raises(ValueError, match="body error"):
                with tracing_mod.traced("span"):
                    raise ValueError("body error")
            healthy_cm.__exit__.assert_called_once()
        finally:
            tracing_mod._langfuse_client = old
            tracing_mod._sampling_rate = old_rate

    def test_traced_sdk_failure_warns_once_then_debug(self, caplog) -> None:
        # traced() sits on the hot path: a persistently broken exporter must
        # warn exactly once, with repeats demoted to DEBUG.
        import logging

        import memtomem_stm.observability.tracing as tracing_mod

        old = tracing_mod._langfuse_client
        old_rate = tracing_mod._sampling_rate
        old_warned = tracing_mod._warned_observation_failure
        try:
            tracing_mod._sampling_rate = 1.0
            tracing_mod._warned_observation_failure = False
            client = MagicMock()
            client.start_as_current_observation.side_effect = RuntimeError("exporter down")
            tracing_mod._langfuse_client = client
            with caplog.at_level(logging.DEBUG, logger="memtomem_stm.observability.tracing"):
                with tracing_mod.traced("a"):
                    pass
                with tracing_mod.traced("b"):
                    pass
            records = [r for r in caplog.records if "Langfuse observation" in r.getMessage()]
            assert [r.levelname for r in records] == ["WARNING", "DEBUG"]
        finally:
            tracing_mod._langfuse_client = old
            tracing_mod._sampling_rate = old_rate
            tracing_mod._warned_observation_failure = old_warned

    def test_shutdown_langfuse_calls_shutdown(self) -> None:
        import memtomem_stm.observability.tracing as tracing_mod

        mock_client = MagicMock()
        tracing_mod.shutdown_langfuse(mock_client)
        mock_client.shutdown.assert_called_once()

    def test_shutdown_langfuse_none_safe(self) -> None:
        import memtomem_stm.observability.tracing as tracing_mod

        # Should not raise
        tracing_mod.shutdown_langfuse(None)

    def test_get_langfuse_returns_current(self) -> None:
        import memtomem_stm.observability.tracing as tracing_mod

        old = tracing_mod._langfuse_client
        try:
            sentinel = object()
            tracing_mod._langfuse_client = sentinel
            assert tracing_mod.get_langfuse() is sentinel
        finally:
            tracing_mod._langfuse_client = old


# ── protocols ───────────────────────────────────────────────────────────


class TestProtocols:
    """Test protocol/dataclass definitions."""

    def test_index_result_defaults(self) -> None:
        r = IndexResult()
        assert r.indexed_chunks == 0

    def test_index_result_custom(self) -> None:
        r = IndexResult(indexed_chunks=42)
        assert r.indexed_chunks == 42

    def test_file_indexer_protocol_structural(self) -> None:
        """A class with the right async method satisfies FileIndexer structurally."""

        class MyIndexer:
            async def index_file(
                self, path: Path, *, force: bool = False, namespace: str | None = None
            ) -> IndexResult:
                return IndexResult(indexed_chunks=1)

        # Runtime check: isinstance with Protocol requires runtime_checkable,
        # but structural typing means it should at least be assignable.
        indexer: FileIndexer = MyIndexer()
        assert hasattr(indexer, "index_file")


# ── metrics ─────────────────────────────────────────────────────────────


class TestCallMetrics:
    """Test CallMetrics dataclass."""

    def test_defaults(self) -> None:
        m = CallMetrics(server="srv", tool="t", original_chars=100, compressed_chars=80)
        assert m.cleaned_chars == 0
        assert m.original_tokens == 0
        assert m.trace_id is None


class TestTokenTracker:
    """Test TokenTracker aggregation logic."""

    def test_empty_summary(self, token_tracker: TokenTracker) -> None:
        s = token_tracker.get_summary()
        assert s["total_calls"] == 0
        assert s["total_savings_pct"] == 0.0
        assert s["cache_hits"] == 0
        assert s["reconnects"] == 0

    def test_record_updates_totals(self, token_tracker: TokenTracker) -> None:
        m = CallMetrics(server="s1", tool="tool_a", original_chars=1000, compressed_chars=600)
        token_tracker.record(m)
        s = token_tracker.get_summary()
        assert s["total_calls"] == 1
        assert s["total_original_chars"] == 1000
        assert s["total_compressed_chars"] == 600
        assert s["total_savings_pct"] == 40.0

    def test_record_by_server_breakdown(self, token_tracker: TokenTracker) -> None:
        token_tracker.record(
            CallMetrics(server="alpha", tool="t1", original_chars=500, compressed_chars=250)
        )
        token_tracker.record(
            CallMetrics(server="beta", tool="t2", original_chars=200, compressed_chars=200)
        )
        s = token_tracker.get_summary()
        assert "alpha" in s["by_server"]
        assert s["by_server"]["alpha"]["savings_pct"] == 50.0
        assert s["by_server"]["beta"]["savings_pct"] == 0.0

    def test_cache_hit_miss_counters(self, token_tracker: TokenTracker) -> None:
        token_tracker.record_cache_hit()
        token_tracker.record_cache_hit()
        token_tracker.record_cache_miss()
        s = token_tracker.get_summary()
        assert s["cache_hits"] == 2
        assert s["cache_misses"] == 1

    def test_total_invocations_reconciles_calls_and_hits(self, token_tracker: TokenTracker) -> None:
        """#558: hits never enter ``total_calls``; the summary exposes the
        reconciled ``total_invocations = total_calls + cache_hits +
        total_errors`` instead (this test is the no-error case; the failed
        component is pinned by test_total_invocations_includes_failed_calls)."""
        token_tracker.record(
            CallMetrics(server="s", tool="t", original_chars=100, compressed_chars=50)
        )
        token_tracker.record_cache_hit(chars=50)
        token_tracker.record_cache_hit(chars=30)
        s = token_tracker.get_summary()
        assert s["total_calls"] == 1
        assert s["cache_hits"] == 2
        assert s["total_invocations"] == 3
        assert s["cache_hit_chars"] == 80

    def test_total_invocations_includes_failed_calls(self, token_tracker: TokenTracker) -> None:
        """#558 codex round 2: a failed call only reaches ``record_error()``,
        never ``record()``, so the invocation total must include errors or a
        failing workload renders "0 live + 0 cache-served = 0 invocations"
        next to a non-zero error count."""
        token_tracker.record(
            CallMetrics(server="s", tool="t", original_chars=100, compressed_chars=50)
        )
        token_tracker.record_cache_hit(chars=50)
        token_tracker.record_error(
            CallMetrics(server="s", tool="t", original_chars=0, compressed_chars=0, is_error=True)
        )
        s = token_tracker.get_summary()
        assert s["total_calls"] == 1
        assert s["total_errors"] == 1
        assert s["total_invocations"] == 3

    def test_cache_hit_default_chars_zero(self, token_tracker: TokenTracker) -> None:
        token_tracker.record_cache_hit()
        s = token_tracker.get_summary()
        assert s["cache_hits"] == 1
        assert s["cache_hit_chars"] == 0

    def test_cache_unstorable_counter(self, token_tracker: TokenTracker) -> None:
        s = token_tracker.get_summary()
        assert s["cache_unstorable"] == 0
        token_tracker.record_cache_unstorable()
        assert token_tracker.get_summary()["cache_unstorable"] == 1

    def test_reconnect_counter(self, token_tracker: TokenTracker) -> None:
        token_tracker.record_reconnect()
        assert token_tracker.get_summary()["reconnects"] == 1

    def test_persist_failure_swallowed(self) -> None:
        mock_store = MagicMock()
        mock_store.record.side_effect = RuntimeError("db locked")
        tracker = TokenTracker(metrics_store=mock_store)
        # Should not raise
        tracker.record(CallMetrics(server="s", tool="t", original_chars=10, compressed_chars=5))
        assert tracker.get_summary()["total_calls"] == 1


# ── mcp_client ──────────────────────────────────────────────────────────


class TestRemoteSearchResult:
    """Test RemoteSearchResult and its fake inner classes."""

    def test_construction(self) -> None:
        r = RemoteSearchResult(content="hello world", score=0.85, source="notes.md")
        assert r.score == 0.85
        assert r.chunk.content == "hello world"
        assert r.chunk.metadata.source_file == Path("notes.md")
        assert r.chunk.metadata.namespace == "default"

    def test_default_namespace(self) -> None:
        r = RemoteSearchResult(content="x", score=0.5)
        assert r.chunk.metadata.namespace == "default"


class TestMcpClientParseResults:
    """Test _parse_results against core's compact format: [rank] score | source."""

    def test_empty_text(self) -> None:
        results = McpClientSearchAdapter._parse_results("")
        assert results == []

    def test_single_result(self) -> None:
        text = "Found 1 results:\n\n[1] 0.92 | notes.md\nSome memory content here"
        results = McpClientSearchAdapter._parse_results(text)
        assert len(results) == 1
        assert results[0].score == 0.92
        assert "Some memory content" in results[0].chunk.content

    def test_multiple_results(self) -> None:
        text = (
            "Found 3 results:\n\n"
            "[1] 0.9 | a.md\nFirst result\n\n"
            "[2] 0.7 | b.md\nSecond result\n\n"
            "[3] 0.5 | c.md\nThird result"
        )
        results = McpClientSearchAdapter._parse_results(text)
        assert len(results) == 3
        assert results[0].score == 0.9
        assert results[2].score == 0.5

    def test_source_extraction(self) -> None:
        text = "Found 1 results:\n\n[1] 0.8 | doc.md > Overview\nContent"
        results = McpClientSearchAdapter._parse_results(text)
        assert len(results) == 1
        assert "doc.md" in str(results[0].chunk.metadata.source_file)

    def test_content_truncated_to_500(self) -> None:
        long_content = "x" * 1000
        text = f"Found 1 results:\n\n[1] 0.5 | file.md\n{long_content}"
        results = McpClientSearchAdapter._parse_results(text)
        assert len(results) == 1
        assert len(results[0].chunk.content) <= 500


class TestMcpClientSearchAdapter:
    """Test McpClientSearchAdapter initialization and search with mock session."""

    def test_init_stores_config(self) -> None:
        from memtomem_stm.surfacing.config import SurfacingConfig

        cfg = SurfacingConfig(ltm_mcp_command="test-server")
        adapter = McpClientSearchAdapter(cfg)
        assert adapter._config is cfg
        assert adapter._session is None

    @pytest.mark.asyncio
    async def test_search_returns_empty_when_no_session(self) -> None:
        from memtomem_stm.surfacing.config import SurfacingConfig

        adapter = McpClientSearchAdapter(SurfacingConfig())
        # Mark the lazy-start as already attempted so _heal_if_needed
        # short-circuits to False without spawning a real memtomem-server
        # (which lazy-starts on this dev box and would yield 'ok'/'empty_results'
        # instead of the asserted 'no_session').
        adapter._start_attempted = True
        results, hints, outcome = await adapter.search("test query")
        assert results == []
        assert hints == []
        assert outcome == "no_session"

    @pytest.mark.asyncio
    async def test_search_calls_mem_search(self) -> None:
        """Default-config adapter requests the structured format (#560).

        The ``output_format`` arg in the asserted call is the wire-level
        pin for the structured default — compact's 2-decimal score
        rendering collapses the RRF score distribution to a single value
        above ``min_score``, so full-precision scores must be the default
        request.
        """
        from memtomem_stm.surfacing.config import SurfacingConfig

        adapter = McpClientSearchAdapter(SurfacingConfig())

        mock_content = MagicMock()
        mock_content.type = "text"
        mock_content.text = (
            '{"results": [{"rank": 1, "score": 0.0325, "source": "notes.md",'
            ' "hierarchy": "Notes", "namespace": "default",'
            ' "content": "Relevant memory"}]}'
        )

        mock_result = MagicMock()
        mock_result.content = [mock_content]

        mock_session = AsyncMock()
        mock_session.call_tool.return_value = mock_result
        adapter._session = mock_session

        results, _, outcome = await adapter.search("what is X", top_k=5)
        mock_session.call_tool.assert_awaited_once_with(
            "mem_search", {"query": "what is X", "top_k": 5, "output_format": "structured"}
        )
        assert len(results) == 1
        assert results[0].score == 0.0325
        assert outcome == "ok"

    @pytest.mark.asyncio
    async def test_search_handles_exception(self) -> None:
        from memtomem_stm.surfacing.config import SurfacingConfig

        adapter = McpClientSearchAdapter(SurfacingConfig())
        mock_session = AsyncMock()
        mock_session.call_tool.side_effect = ConnectionError("lost")
        adapter._session = mock_session
        # Prevent reconnect from hitting a real server
        adapter.start = AsyncMock(side_effect=ConnectionError("reconnect failed"))  # type: ignore[method-assign]

        results, hints, outcome = await adapter.search("query")
        assert results == []
        assert hints == []
        assert outcome == "transport_error"

    @pytest.mark.asyncio
    async def test_search_timeout_triggers_reconnect(self) -> None:
        """asyncio.TimeoutError is treated as a transport error, triggering reconnect."""
        from memtomem_stm.surfacing.config import SurfacingConfig

        adapter = McpClientSearchAdapter(SurfacingConfig())

        mock_session = AsyncMock()
        mock_session.call_tool.side_effect = asyncio.TimeoutError()
        adapter._session = mock_session

        adapter._reconnect = AsyncMock(side_effect=ConnectionError("reconnect failed"))  # type: ignore[method-assign]

        results, hints, outcome = await adapter.search("query")
        assert results == []
        assert hints == []
        assert outcome == "transport_error"
        # Reconnect was attempted (TimeoutError treated as transport error)
        adapter._reconnect.assert_awaited_once()
