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
            strategy_results = parser.parse(text)
            static_results = McpClientSearchAdapter._parse_results(text)
            assert len(strategy_results) == len(static_results)
            for s, st in zip(strategy_results, static_results):
                assert s.score == st.score
                assert s.chunk.content == st.chunk.content

    def test_structured_parser_returns_empty_for_invalid_json(self):
        """StructuredResultParser.parse() returns [] for non-JSON input."""
        from memtomem_stm.surfacing.mcp_client import StructuredResultParser

        parser = StructuredResultParser()
        assert parser.parse("not json") == []
        assert parser.parse("") == []

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
        results = parser.parse(STRUCTURED_TWO_RESULTS)
        assert len(results) == 2
        assert results[0].score == pytest.approx(0.92)
        assert results[1].score == pytest.approx(0.87)
        assert "auth.md" in str(results[0].chunk.metadata.source_file)
        assert "api.md" in str(results[1].chunk.metadata.source_file)
        assert results[0].chunk.metadata.namespace == "default"
        assert results[0].chunk.id == "abc123"
        assert results[1].chunk.id == "def456"


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
        version_content.text = json.dumps({
            "version": "0.3.0",
            "capabilities": {"search_formats": ["compact", "structured"]},
        })
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
        version_content.text = json.dumps({
            "version": "0.2.0",
            "capabilities": {"search_formats": ["compact"]},
        })
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
