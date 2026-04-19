"""Contract tests: verify STM's parser handles core's real formatter output.

These tests use snapshot fixtures of ``memtomem.server.formatters._format_compact_result``
output so that STM CI detects format drift without depending on the core package at
runtime.  If core's formatter changes, update the fixtures here to match.

Fixture source: ``memtomem/packages/memtomem/src/memtomem/server/formatters.py``
(functions ``_format_results``, ``_format_compact_result``).
"""

from __future__ import annotations

import pytest

from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

# ── Core formatter output snapshots ─────────────────────────────────────
#
# These strings are produced by core's _format_results(results, verbose=False).
# Keep them in sync with the real formatter; see formatters.py:26-65.

COMPACT_TWO_RESULTS = (
    "Found 2 results:\n"
    "\n"
    "[1] 0.92 | auth.md > Authentication\n"
    "JWT authentication uses HS256 with rotating secrets every 24 hours.\n"
    "\n"
    "[2] 0.87 | api.md > Rate Limiting\n"
    "All API responses include rate limit headers (X-RateLimit-*)."
)

COMPACT_WITH_NAMESPACE = (
    "Found 1 results:\n"
    "\n"
    "[1] 0.85 | [project-x] design.md > Architecture\n"
    "The system uses event-driven architecture with CQRS."
)

COMPACT_WITH_CONTEXT_WINDOW = (
    "Found 1 results:\n"
    "\n"
    "[1] 0.91 | deploy.md > Rollback [2/5]\n"
    "...previous section about blue-green deployment\n"
    "To rollback, run `kubectl rollout undo deployment/api`.\n"
    "Next section covers canary releases..."
)

COMPACT_NON_MD_SOURCE = (
    "Found 1 results:\n"
    "\n"
    "[1] 0.78 | config.py\n"
    "DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///local.db')"
)

NO_RESULTS = "No results found."

ERROR_RESPONSE = "Error: query cannot be empty."


# ── Tests ───────────────────────────────────────────────────────────────


class TestCoreCompactFormat:
    """Verify _parse_results handles core's compact format snapshots."""

    def test_two_results(self):
        results = McpClientSearchAdapter._parse_results(COMPACT_TWO_RESULTS)
        assert len(results) == 2
        assert results[0].score == pytest.approx(0.92)
        assert results[1].score == pytest.approx(0.87)
        assert "auth.md" in str(results[0].chunk.metadata.source_file)
        assert "api.md" in str(results[1].chunk.metadata.source_file)
        assert "JWT authentication" in results[0].chunk.content
        assert "rate limit headers" in results[1].chunk.content

    def test_namespace_badge(self):
        results = McpClientSearchAdapter._parse_results(COMPACT_WITH_NAMESPACE)
        assert len(results) == 1
        assert results[0].score == pytest.approx(0.85)
        assert results[0].chunk.metadata.namespace == "project-x"
        assert "design.md" in str(results[0].chunk.metadata.source_file)
        assert "event-driven" in results[0].chunk.content

    def test_context_window_position(self):
        results = McpClientSearchAdapter._parse_results(COMPACT_WITH_CONTEXT_WINDOW)
        assert len(results) == 1
        assert results[0].score == pytest.approx(0.91)
        assert "deploy.md" in str(results[0].chunk.metadata.source_file)
        # Context window content is included as part of the content
        assert "rollback" in results[0].chunk.content.lower()

    def test_non_md_source(self):
        results = McpClientSearchAdapter._parse_results(COMPACT_NON_MD_SOURCE)
        assert len(results) == 1
        assert results[0].score == pytest.approx(0.78)
        assert "config.py" in str(results[0].chunk.metadata.source_file)
        assert "DATABASE_URL" in results[0].chunk.content

    def test_no_results(self):
        results = McpClientSearchAdapter._parse_results(NO_RESULTS)
        assert results == []

    def test_error_response(self):
        results = McpClientSearchAdapter._parse_results(ERROR_RESPONSE)
        assert results == []

    def test_default_namespace_when_no_badge(self):
        results = McpClientSearchAdapter._parse_results(COMPACT_TWO_RESULTS)
        assert results[0].chunk.metadata.namespace == "default"


class TestNamespaceNormalization:
    """Verify namespace list-to-string normalization in search()."""

    @pytest.mark.asyncio
    async def test_list_namespace_joined(self):
        """A list namespace should be comma-joined before MCP call."""
        from unittest.mock import AsyncMock, MagicMock

        from memtomem_stm.surfacing.config import SurfacingConfig
        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

        adapter = McpClientSearchAdapter(SurfacingConfig())
        # Inject a mock session
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = []
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        adapter._session = mock_session

        await adapter.search("test", namespace=["ns1", "ns2"])

        mock_session.call_tool.assert_called_once()
        call_args = mock_session.call_tool.call_args
        assert call_args[0][0] == "mem_search"
        assert call_args[0][1]["namespace"] == "ns1,ns2"

    @pytest.mark.asyncio
    async def test_string_namespace_passed_through(self):
        """A string namespace should be forwarded as-is."""
        from unittest.mock import AsyncMock, MagicMock

        from memtomem_stm.surfacing.config import SurfacingConfig
        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

        adapter = McpClientSearchAdapter(SurfacingConfig())
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = []
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        adapter._session = mock_session

        await adapter.search("test", namespace="myns")

        call_args = mock_session.call_tool.call_args
        assert call_args[0][1]["namespace"] == "myns"


class TestContextWindowForwarding:
    """Verify context_window is forwarded to MCP call."""

    @pytest.mark.asyncio
    async def test_context_window_forwarded(self):
        from unittest.mock import AsyncMock, MagicMock

        from memtomem_stm.surfacing.config import SurfacingConfig
        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

        adapter = McpClientSearchAdapter(SurfacingConfig())
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = []
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        adapter._session = mock_session

        await adapter.search("test", context_window=2)

        call_args = mock_session.call_tool.call_args
        assert call_args[0][1]["context_window"] == 2

    @pytest.mark.asyncio
    async def test_context_window_zero_not_sent(self):
        from unittest.mock import AsyncMock, MagicMock

        from memtomem_stm.surfacing.config import SurfacingConfig
        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

        adapter = McpClientSearchAdapter(SurfacingConfig())
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = []
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        adapter._session = mock_session

        await adapter.search("test", context_window=0)

        call_args = mock_session.call_tool.call_args
        assert "context_window" not in call_args[0][1]

    @pytest.mark.asyncio
    async def test_context_window_none_not_sent(self):
        from unittest.mock import AsyncMock, MagicMock

        from memtomem_stm.surfacing.config import SurfacingConfig
        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

        adapter = McpClientSearchAdapter(SurfacingConfig())
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = []
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        adapter._session = mock_session

        await adapter.search("test", context_window=None)

        call_args = mock_session.call_tool.call_args
        assert "context_window" not in call_args[0][1]


# ── Parser strategy tests ────────────────────────────────────────────────

from memtomem_stm.surfacing.config import SurfacingConfig  # noqa: E402


class TestParserStrategy:
    """Verify strategy-based parser dispatch and backward compatibility."""

    def test_get_parser_default_is_compact(self):
        from memtomem_stm.surfacing.mcp_client import CompactResultParser, get_parser

        parser = get_parser()
        assert isinstance(parser, CompactResultParser)

    def test_get_parser_explicit_compact(self):
        from memtomem_stm.surfacing.mcp_client import CompactResultParser, get_parser

        parser = get_parser("compact")
        assert isinstance(parser, CompactResultParser)

    def test_get_parser_structured(self):
        from memtomem_stm.surfacing.mcp_client import StructuredResultParser, get_parser

        parser = get_parser("structured")
        assert isinstance(parser, StructuredResultParser)

    def test_compact_parser_matches_static_method(self):
        """CompactResultParser.parse() produces identical results to _parse_results."""
        from memtomem_stm.surfacing.mcp_client import CompactResultParser

        parser = CompactResultParser()
        for text in [
            COMPACT_TWO_RESULTS,
            COMPACT_WITH_NAMESPACE,
            COMPACT_WITH_CONTEXT_WINDOW,
            COMPACT_NON_MD_SOURCE,
            NO_RESULTS,
            ERROR_RESPONSE,
        ]:
            strategy_results, strategy_hints = parser.parse(text)
            static_results = McpClientSearchAdapter._parse_results(text)
            assert len(strategy_results) == len(static_results)
            assert strategy_hints == []
            for s, st in zip(strategy_results, static_results):
                assert s.score == st.score
                assert s.chunk.content == st.chunk.content

    def test_structured_parser_returns_empty_for_invalid_json(self):
        """StructuredResultParser.parse() returns ([], []) for non-JSON input."""
        from memtomem_stm.surfacing.mcp_client import StructuredResultParser

        parser = StructuredResultParser()
        assert parser.parse("not json") == ([], [])
        assert parser.parse("") == ([], [])

    def test_adapter_uses_configured_parser(self):
        """McpClientSearchAdapter respects config.result_format."""
        from memtomem_stm.surfacing.mcp_client import CompactResultParser

        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="compact"))
        assert isinstance(adapter._parser, CompactResultParser)


# ── Phase 2 structured format snapshots ──────────────────────────────────

STRUCTURED_TWO_RESULTS = (
    '{"results": ['
    '  {"rank": 1, "score": 0.92, "source": "auth.md", "hierarchy": "Authentication",'
    '   "namespace": "default", "chunk_id": "abc123", "content": "JWT authentication..."},'
    '  {"rank": 2, "score": 0.87, "source": "api.md", "hierarchy": "Rate Limiting",'
    '   "namespace": "default", "chunk_id": "def456", "content": "Rate limit headers..."}'
    "]}"
)


class TestStructuredFormatSnapshots:
    """Verify StructuredResultParser handles core's structured JSON format."""

    def test_structured_two_results(self):
        from memtomem_stm.surfacing.mcp_client import StructuredResultParser

        parser = StructuredResultParser()
        results, hints = parser.parse(STRUCTURED_TWO_RESULTS)
        assert len(results) == 2
        assert hints == []
        assert results[0].score == pytest.approx(0.92)
        assert results[1].score == pytest.approx(0.87)
        assert "auth.md" in str(results[0].chunk.metadata.source_file)
        assert "api.md" in str(results[1].chunk.metadata.source_file)
        assert results[0].chunk.metadata.namespace == "default"
        assert results[0].chunk.id == "abc123"
        assert results[1].chunk.id == "def456"


# Parent PR #231 (7d184f1, 2026-04-18): mem_search(output_format="structured")
# now returns JSON on empty results instead of the plain "No results found."
# text. We pin only ``results == []`` — parent is alpha (Development Status
# :: 3 - Alpha) and the ``hints`` field is opportunistic, so a future rename
# or removal upstream must not block STM.

STRUCTURED_NO_RESULTS_PLAIN = '{"results": []}'

STRUCTURED_NO_RESULTS_WITH_HINTS = (
    '{"results": [], "hints": ['
    '"No results match your filters (3 results found before filtering). '
    'Try broader filters or remove source_filter/tag_filter."'
    "]}"
)


class TestStructuredEmptyResults:
    """StructuredResultParser tolerates parent PR #231's empty-result JSON
    in both the bare and hints-augmented shapes, and exposes hints to
    callers for operator-visible observability (B3)."""

    def test_structured_parser_returns_empty_without_hints(self):
        from memtomem_stm.surfacing.mcp_client import StructuredResultParser

        parser = StructuredResultParser()
        results, hints = parser.parse(STRUCTURED_NO_RESULTS_PLAIN)
        assert results == []
        assert hints == []

    def test_structured_parser_returns_empty_with_hints(self):
        from memtomem_stm.surfacing.mcp_client import StructuredResultParser

        parser = StructuredResultParser()
        results, hints = parser.parse(STRUCTURED_NO_RESULTS_WITH_HINTS)
        assert results == []
        assert len(hints) == 1
        assert "No results match your filters" in hints[0]

    def test_structured_parser_hints_non_list_is_empty(self):
        """Alpha tolerance: parent may future-rename ``hints``, emit a
        dict, or drop the field. Non-list values must degrade silently
        to an empty list."""
        from memtomem_stm.surfacing.mcp_client import StructuredResultParser

        parser = StructuredResultParser()
        for payload in (
            '{"results": [], "hints": "oops not a list"}',
            '{"results": [], "hints": {"msg": "still wrong"}}',
            '{"results": [], "hints": null}',
            '{"results": []}',
        ):
            results, hints = parser.parse(payload)
            assert results == []
            assert hints == []

    def test_structured_parser_hints_with_results(self):
        """Hints can accompany non-empty results too (e.g. "3 filtered")."""
        from memtomem_stm.surfacing.mcp_client import StructuredResultParser

        payload = (
            '{"results": ['
            '  {"rank": 1, "score": 0.5, "source": "a.md", "hierarchy": "X",'
            '   "namespace": "default", "chunk_id": "c1", "content": "hit"}'
            '], "hints": ["2 additional results hidden by namespace filter"]}'
        )
        parser = StructuredResultParser()
        results, hints = parser.parse(payload)
        assert len(results) == 1
        assert hints == ["2 additional results hidden by namespace filter"]

    def test_structured_parser_drops_non_string_hints(self):
        """Guard against mixed-type hint arrays (defensive, parent is alpha)."""
        from memtomem_stm.surfacing.mcp_client import StructuredResultParser

        payload = '{"results": [], "hints": ["ok", 42, null, {"x": 1}, "also ok"]}'
        parser = StructuredResultParser()
        results, hints = parser.parse(payload)
        assert results == []
        assert hints == ["ok", "also ok"]


class TestAdapterHintsFlow:
    """End-to-end through ``McpClientSearchAdapter.search`` — hints arrive
    at the caller exactly as the parent emits them (B3)."""

    @pytest.mark.asyncio
    async def test_search_returns_hints_from_structured_payload(self):
        from unittest.mock import AsyncMock, MagicMock

        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="structured"))
        mock_session = AsyncMock()
        mock_content = MagicMock()
        mock_content.type = "text"
        mock_content.text = STRUCTURED_NO_RESULTS_WITH_HINTS
        mock_result = MagicMock()
        mock_result.content = [mock_content]
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        adapter._session = mock_session

        results, hints = await adapter.search("q")
        assert results == []
        assert len(hints) == 1
        assert "No results match your filters" in hints[0]

    @pytest.mark.asyncio
    async def test_search_returns_empty_hints_for_compact_format(self):
        from unittest.mock import AsyncMock, MagicMock

        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="compact"))
        mock_session = AsyncMock()
        mock_content = MagicMock()
        mock_content.type = "text"
        mock_content.text = COMPACT_TWO_RESULTS
        mock_result = MagicMock()
        mock_result.content = [mock_content]
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        adapter._session = mock_session

        results, hints = await adapter.search("q")
        assert len(results) == 2
        assert hints == []

    @pytest.mark.asyncio
    async def test_search_returns_empty_hints_on_transport_error(self):
        import asyncio as _asyncio
        from unittest.mock import AsyncMock

        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="structured"))
        mock_session = AsyncMock()
        mock_session.call_tool.side_effect = ConnectionError("lost")
        adapter._session = mock_session
        adapter.start = AsyncMock(side_effect=_asyncio.TimeoutError())  # type: ignore[method-assign]

        results, hints = await adapter.search("q")
        assert results == []
        assert hints == []


class TestOutputFormatForwarding:
    """Verify output_format is sent to MCP when using structured parser."""

    @pytest.mark.asyncio
    async def test_structured_parser_sends_output_format(self):
        from unittest.mock import AsyncMock, MagicMock

        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="structured"))
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = []
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        adapter._session = mock_session

        await adapter.search("test query")

        call_args = mock_session.call_tool.call_args
        assert call_args[0][1]["output_format"] == "structured"

    @pytest.mark.asyncio
    async def test_compact_parser_does_not_send_output_format(self):
        from unittest.mock import AsyncMock, MagicMock

        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="compact"))
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.content = []
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        adapter._session = mock_session

        await adapter.search("test query")

        call_args = mock_session.call_tool.call_args
        assert "output_format" not in call_args[0][1]


class TestFormatNegotiation:
    """Verify _negotiate_format downgrades parser when core lacks structured support."""

    @pytest.mark.asyncio
    async def test_keeps_structured_when_core_supports_it(self):
        import json
        from unittest.mock import AsyncMock, MagicMock

        from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter, StructuredResultParser

        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="structured"))
        mock_session = AsyncMock()
        version_result = MagicMock()
        version_content = MagicMock()
        version_content.type = "text"
        version_content.text = json.dumps(
            {
                "version": "0.3.0",
                "capabilities": {"search_formats": ["compact", "structured"]},
            }
        )
        version_result.content = [version_content]
        mock_session.call_tool = AsyncMock(return_value=version_result)
        adapter._session = mock_session

        await adapter._negotiate_format()

        assert isinstance(adapter._parser, StructuredResultParser)

    @pytest.mark.asyncio
    async def test_downgrades_when_core_lacks_structured(self):
        import json
        from unittest.mock import AsyncMock, MagicMock

        from memtomem_stm.surfacing.mcp_client import CompactResultParser, McpClientSearchAdapter

        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="structured"))
        mock_session = AsyncMock()
        version_result = MagicMock()
        version_content = MagicMock()
        version_content.type = "text"
        version_content.text = json.dumps(
            {
                "version": "0.2.0",
                "capabilities": {"search_formats": ["compact"]},
            }
        )
        version_result.content = [version_content]
        mock_session.call_tool = AsyncMock(return_value=version_result)
        adapter._session = mock_session

        await adapter._negotiate_format()

        assert isinstance(adapter._parser, CompactResultParser)

    @pytest.mark.asyncio
    async def test_downgrades_when_version_call_fails(self):
        from unittest.mock import AsyncMock

        from memtomem_stm.surfacing.mcp_client import CompactResultParser, McpClientSearchAdapter

        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="structured"))
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(side_effect=Exception("unknown action"))
        adapter._session = mock_session

        await adapter._negotiate_format()

        assert isinstance(adapter._parser, CompactResultParser)

    @pytest.mark.asyncio
    async def test_skips_negotiation_for_compact(self):
        from unittest.mock import AsyncMock

        from memtomem_stm.surfacing.mcp_client import CompactResultParser, McpClientSearchAdapter

        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="compact"))
        mock_session = AsyncMock()
        adapter._session = mock_session

        await adapter._negotiate_format()

        assert isinstance(adapter._parser, CompactResultParser)
        mock_session.call_tool.assert_not_called()
