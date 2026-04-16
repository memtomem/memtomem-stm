"""Unit tests for `McpClientSearchAdapter` reconnect and version-negotiation paths.

Complements the coarse integration tests in `test_stm_remaining.py` and the
happy-path/obvious-failure coverage in `test_core_format_contract.py`. The
goal here is to lock in the **less obvious** behaviors that would silently
degrade surfacing quality or leave the client in an inconsistent state
(issue #74):

- Reconnect that succeeds on retry must return actual results, not `[]`.
- Reconnect that itself fails must not leak the original transport error.
- Version negotiation must downgrade (not crash) when the response is
  malformed JSON, missing the capabilities key, or reports an unknown
  format name.
- The downgraded parser must actually parse the compact format downstream.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.mcp_client import (
    CompactResultParser,
    McpClientSearchAdapter,
    StructuredResultParser,
)


def _text_content(text: str):
    c = MagicMock()
    c.type = "text"
    c.text = text
    return c


def _result_with_text(text: str):
    r = MagicMock()
    r.content = [_text_content(text)]
    return r


# ── Reconnect retry paths ────────────────────────────────────────────────


class TestReconnectRetrySuccess:
    """A transient transport failure followed by a successful reconnect must
    deliver the retry's actual results to the caller, not silently drop them."""

    @pytest.mark.asyncio
    async def test_transient_failure_then_retry_returns_results(self):
        adapter = McpClientSearchAdapter(SurfacingConfig())

        compact_output = "[1] 0.95 | [default] src/app.py\nThe retry worked.\n"
        good_result = _result_with_text(compact_output)

        mock_session = AsyncMock()
        # First call raises a transport error; second call (post-reconnect) succeeds.
        mock_session.call_tool = AsyncMock(side_effect=[ConnectionError("transient"), good_result])
        adapter._session = mock_session

        # _reconnect is mocked so we don't actually restart anything — but
        # we verify it was called exactly once, and crucially that
        # `adapter._session` is unchanged afterwards so the second call_tool
        # hits the same mock.
        adapter._reconnect = AsyncMock()  # type: ignore[method-assign]

        results, stats = await adapter.search("anything")

        adapter._reconnect.assert_awaited_once()
        assert mock_session.call_tool.await_count == 2
        assert len(results) == 1
        assert "retry worked" in results[0].chunk.content.lower()
        assert results[0].score == 0.95
        assert stats is None


class TestReconnectRetryFailure:
    """If `_reconnect` itself raises, `search()` swallows it and returns
    an empty list — the adapter must never propagate the original transport
    error up into SurfacingEngine."""

    @pytest.mark.asyncio
    async def test_reconnect_raises_search_returns_empty(self):
        adapter = McpClientSearchAdapter(SurfacingConfig())

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(side_effect=OSError("broken pipe"))
        adapter._session = mock_session

        adapter._reconnect = AsyncMock(side_effect=ConnectionError("reconnect failed"))  # type: ignore[method-assign]

        results, stats = await adapter.search("q")

        assert results == []
        assert stats is None
        adapter._reconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reconnect_succeeds_but_retry_call_also_fails(self):
        """Reconnect works but the retry's call_tool still fails — we must
        still return [] instead of raising into the caller."""
        adapter = McpClientSearchAdapter(SurfacingConfig())

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(
            side_effect=[ConnectionError("first"), RuntimeError("second")]
        )
        adapter._session = mock_session
        adapter._reconnect = AsyncMock()  # type: ignore[method-assign]

        results, stats = await adapter.search("q")

        assert results == []
        assert stats is None
        assert mock_session.call_tool.await_count == 2


class TestNonTransportErrorsDoNotReconnect:
    """Errors outside `_TRANSPORT_ERRORS` must NOT trigger a reconnect —
    reconnecting on an application-level error would mask real bugs and
    amplify tail latency for nothing."""

    @pytest.mark.asyncio
    async def test_generic_exception_returns_empty_without_reconnect(self):
        adapter = McpClientSearchAdapter(SurfacingConfig())

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(side_effect=ValueError("bad args"))
        adapter._session = mock_session
        adapter._reconnect = AsyncMock()  # type: ignore[method-assign]

        results, stats = await adapter.search("q")

        assert results == []
        assert stats is None
        adapter._reconnect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_timeout_error_is_transport_and_triggers_reconnect(self):
        """`asyncio.TimeoutError` is in `_TRANSPORT_ERRORS` — double-check
        the tuple membership by behavior, so reordering the tuple in future
        doesn't silently change reconnect semantics."""
        adapter = McpClientSearchAdapter(SurfacingConfig())

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(side_effect=asyncio.TimeoutError())
        adapter._session = mock_session
        adapter._reconnect = AsyncMock(side_effect=ConnectionError("fail"))  # type: ignore[method-assign]

        await adapter.search("q")

        adapter._reconnect.assert_awaited_once()


# ── Version negotiation fallback paths ───────────────────────────────────


class TestNegotiationMalformedResponse:
    """`_negotiate_format` must downgrade (not crash) when the response is
    broken. Downgrade is also logged, but the important contract is that
    surfacing never ends up holding a `StructuredResultParser` pointed at
    a server that can't emit structured output — that would produce zero
    results for every query."""

    @pytest.mark.asyncio
    async def test_downgrades_on_malformed_json(self):
        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="structured"))
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=_result_with_text("not-json{{"))
        adapter._session = mock_session

        await adapter._negotiate_format()

        assert isinstance(adapter._parser, CompactResultParser)

    @pytest.mark.asyncio
    async def test_downgrades_on_missing_capabilities_key(self):
        """Older core versions may return only `{"version": "..."}` with no
        capabilities — we must treat that as 'structured not supported'
        rather than assuming it."""
        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="structured"))
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(
            return_value=_result_with_text(json.dumps({"version": "0.1.0"}))
        )
        adapter._session = mock_session

        await adapter._negotiate_format()

        assert isinstance(adapter._parser, CompactResultParser)

    @pytest.mark.asyncio
    async def test_downgrades_on_unknown_format_name(self):
        """Server returns a capability list that doesn't include `structured`
        — downgrade to compact so the remainder of the session still works."""
        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="structured"))
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(
            return_value=_result_with_text(
                json.dumps({"capabilities": {"search_formats": ["experimental-v2"]}})
            )
        )
        adapter._session = mock_session

        await adapter._negotiate_format()

        assert isinstance(adapter._parser, CompactResultParser)

    @pytest.mark.asyncio
    async def test_downgrades_on_empty_text_parts(self):
        """Server returns a successful tool call but with no text content.
        Current behavior: skip the 'supports structured' early return and
        fall through to the downgrade path."""
        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="structured"))
        mock_session = AsyncMock()
        empty_result = MagicMock()
        empty_result.content = []
        mock_session.call_tool = AsyncMock(return_value=empty_result)
        adapter._session = mock_session

        await adapter._negotiate_format()

        assert isinstance(adapter._parser, CompactResultParser)


class TestNegotiationDowngradeAffectsParsing:
    """After downgrade, subsequent parser calls must actually return compact
    results — proves the downgrade is wired through end-to-end and not just
    a cosmetic instance swap."""

    @pytest.mark.asyncio
    async def test_post_downgrade_parser_parses_compact_output(self):
        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="structured"))
        # Pre-condition: starts as structured.
        assert isinstance(adapter._parser, StructuredResultParser)

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=_result_with_text("garbage"))
        adapter._session = mock_session

        await adapter._negotiate_format()

        # Post: downgraded, and the downgraded parser handles compact text.
        compact = "[1] 0.42 | [default] a/b.md\nHello from compact.\n"
        results = adapter._parser.parse(compact)
        assert len(results) == 1
        assert results[0].score == 0.42
        assert "Hello from compact" in results[0].chunk.content


# ── Spec-noncompliant ``result.content=None`` from upstream ──────────────


class TestNoneContentDefense:
    """PR #114 fixed ``result.content=None`` in ``proxy/manager.py``; the
    surfacing client kept the same unguarded iteration in ``search`` and
    ``scratch_list`` and would crash with ``TypeError`` instead of returning
    an empty result. Both paths must degrade silently — surfacing is always
    allowed to skip on missing data."""

    @pytest.mark.asyncio
    async def test_search_returns_empty_when_content_is_none(self):
        adapter = McpClientSearchAdapter(SurfacingConfig())
        bad = MagicMock()
        bad.content = None
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=bad)
        adapter._session = mock_session

        results, stats = await adapter.search("anything")
        assert results == []
        assert stats is None

    @pytest.mark.asyncio
    async def test_scratch_list_returns_empty_when_content_is_none(self):
        adapter = McpClientSearchAdapter(SurfacingConfig())
        bad = MagicMock()
        bad.content = None
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=bad)
        adapter._session = mock_session

        entries = await adapter.scratch_list()
        assert entries == []


# ── start() cleanup on failure ───────────────────────────────────────────


class TestStartCleansUpOnFailure:
    """If `start()` fails after entering the transport+session contexts (e.g.
    `session.initialize()` raises against an unreachable server), the
    AsyncExitStack must be aclosed so the spawned subprocess and stdio streams
    aren't leaked across reconnect retries — otherwise repeated transient
    failures pile up file descriptors and zombie processes.
    """

    @pytest.mark.asyncio
    async def test_initialize_failure_unwinds_stack_and_clears_state(self, monkeypatch):
        from memtomem_stm.surfacing import mcp_client as mod

        transport_exited = asyncio.Event()
        session_exited = asyncio.Event()

        class FakeTransport:
            async def __aenter__(self):
                return (MagicMock(), MagicMock())

            async def __aexit__(self, *args):
                transport_exited.set()
                return None

        class FakeSession:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                session_exited.set()
                return None

            async def initialize(self):
                raise ConnectionError("simulated init failure")

        monkeypatch.setattr(mod, "stdio_client", lambda _params: FakeTransport())
        monkeypatch.setattr(mod, "ClientSession", FakeSession)

        adapter = McpClientSearchAdapter(SurfacingConfig())

        with pytest.raises(ConnectionError, match="simulated init failure"):
            await adapter.start()

        assert transport_exited.is_set(), "transport context must be aclosed on init failure"
        assert session_exited.is_set(), "session context must be aclosed on init failure"
        assert adapter._stack is None
        assert adapter._session is None


# ── c.text=None tolerance (PR #114 parity) ──────────────────────────────


class TestTextNoneTolerance:
    """MCP spec requires TextContent.text to be str, but a spec-noncompliant
    server may return None. manager.py:1042 guards with ``c.text or ""``;
    the surfacing adapter must do the same."""

    @pytest.mark.asyncio
    async def test_search_tolerates_none_text(self):
        adapter = McpClientSearchAdapter(SurfacingConfig())
        mock_session = AsyncMock()

        none_content = MagicMock()
        none_content.type = "text"
        none_content.text = None

        good_content = _text_content("[1] 0.90 | [default] note.md\nreal content")

        result_obj = MagicMock()
        result_obj.content = [none_content, good_content]
        mock_session.call_tool = AsyncMock(return_value=result_obj)
        adapter._session = mock_session

        results, _ = await adapter.search("test query")
        assert len(results) == 1
        assert "real content" in results[0].chunk.content

    @pytest.mark.asyncio
    async def test_search_all_none_text_returns_empty(self):
        adapter = McpClientSearchAdapter(SurfacingConfig())
        mock_session = AsyncMock()

        none_content = MagicMock()
        none_content.type = "text"
        none_content.text = None

        result_obj = MagicMock()
        result_obj.content = [none_content]
        mock_session.call_tool = AsyncMock(return_value=result_obj)
        adapter._session = mock_session

        results, _ = await adapter.search("test query")
        assert results == []
