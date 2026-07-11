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

import httpx
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


# ── LTM transport selection (#297) ───────────────────────────────────────


class TestLtmTransportSelection:
    def test_stdio_transport_uses_command_and_args(self, monkeypatch):
        from memtomem_stm.surfacing import mcp_client as mod

        captured = {}
        sentinel = object()

        def fake_stdio_client(params):
            captured["command"] = params.command
            captured["args"] = params.args
            return sentinel

        monkeypatch.setattr(mod, "stdio_client", fake_stdio_client)

        adapter = McpClientSearchAdapter(
            SurfacingConfig(ltm_mcp_command="memtomem-dev", ltm_mcp_args=["--debug"])
        )

        assert adapter._open_transport() is sentinel
        assert captured == {"command": "memtomem-dev", "args": ["--debug"]}

    def test_sse_transport_uses_url_and_headers(self, monkeypatch):
        from memtomem_stm.surfacing import mcp_client as mod

        captured = {}
        sentinel = object()

        def fake_sse_client(url, *, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return sentinel

        monkeypatch.setattr(mod, "sse_client", fake_sse_client)

        adapter = McpClientSearchAdapter(
            SurfacingConfig(
                ltm_mcp_transport="sse",
                ltm_mcp_url="https://ltm.example/sse",
                ltm_mcp_headers={"Authorization": "Bearer token"},
            )
        )

        assert adapter._open_transport() is sentinel
        assert captured == {
            "url": "https://ltm.example/sse",
            "headers": {"Authorization": "Bearer token"},
        }

    def test_streamable_http_transport_uses_url_and_headers(self, monkeypatch):
        from memtomem_stm.surfacing import mcp_client as mod

        captured = {}
        sentinel = object()

        def fake_streamablehttp_client(url, *, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return sentinel

        monkeypatch.setattr(mod, "streamablehttp_client", fake_streamablehttp_client)

        adapter = McpClientSearchAdapter(
            SurfacingConfig(
                ltm_mcp_transport="streamable_http",
                ltm_mcp_url="https://ltm.example/mcp",
                ltm_mcp_headers={"X-Project": "stm"},
            )
        )

        assert adapter._open_transport() is sentinel
        assert captured == {
            "url": "https://ltm.example/mcp",
            "headers": {"X-Project": "stm"},
        }


# ── Reconnect retry paths ────────────────────────────────────────────────


class TestTargetDisplayRedaction:
    """_target_display feeds INFO logs in start()/_reconnect() — URL userinfo
    (basic-auth credentials in front of a network LTM, #398) must not leak."""

    def test_url_userinfo_is_redacted(self):
        adapter = McpClientSearchAdapter(
            SurfacingConfig(
                ltm_mcp_transport="sse",
                ltm_mcp_url="https://alice:s3cret@ltm.example/sse",
            )
        )
        display = adapter._target_display()
        assert "s3cret" not in display
        assert "alice" not in display
        assert display == "https://***@ltm.example/sse"

    def test_url_without_userinfo_unchanged(self):
        adapter = McpClientSearchAdapter(
            SurfacingConfig(
                ltm_mcp_transport="streamable_http",
                ltm_mcp_url="https://ltm.example/mcp",
            )
        )
        assert adapter._target_display() == "https://ltm.example/mcp"

    def test_stdio_returns_command(self):
        adapter = McpClientSearchAdapter(SurfacingConfig(ltm_mcp_command="memtomem-dev"))
        assert adapter._target_display() == "memtomem-dev"

    def test_transport_still_receives_raw_url(self, monkeypatch):
        # Redaction is display-only — the connection must use the verbatim
        # configured URL, credentials included.
        from memtomem_stm.surfacing import mcp_client as mod

        captured = {}
        monkeypatch.setattr(
            mod,
            "sse_client",
            lambda url, *, headers=None: captured.update(url=url) or object(),
        )
        adapter = McpClientSearchAdapter(
            SurfacingConfig(
                ltm_mcp_transport="sse",
                ltm_mcp_url="https://alice:s3cret@ltm.example/sse",
            )
        )
        adapter._open_transport()
        assert captured["url"] == "https://alice:s3cret@ltm.example/sse"

    def test_unparseable_url_not_echoed(self):
        from memtomem_stm.utils.redact import redact_url_userinfo

        # Malformed IPv6 brackets make urlsplit raise ValueError; the raw
        # string could still embed credentials, so it is replaced wholesale.
        assert redact_url_userinfo("https://user:pw@[::1/mcp") == "<unparseable url>"

    def test_schemeless_credential_value_not_echoed(self):
        from memtomem_stm.utils.redact import redact_url_userinfo

        # urlsplit parses a scheme-less value as a bare path (empty netloc,
        # no exception) — it must still not be echoed verbatim.
        assert redact_url_userinfo("alice:s3cret@ltm.example/sse") == "<unparseable url>"

    def test_exception_text_scrubs_userinfo_variants(self):
        from memtomem_stm.utils.redact import redact_exception_text

        url = "https://alice:s3cret@ltm.example/sse"
        text = (
            f"Client error '401 Unauthorized' for url '{url}'; "
            "retried 'https://alice:s3cret@ltm.example/other'"
        )
        out = redact_exception_text(text, url)
        assert "s3cret" not in out
        assert out.count("***@ltm.example") == 2  # exact URL and derived variant

    async def test_start_failure_log_scrubs_url_credentials(self, caplog):
        # httpx exceptions embed the full request URL; the lazy-start failure
        # WARNING must scrub it rather than logging the exception raw.
        adapter = McpClientSearchAdapter(
            SurfacingConfig(
                ltm_mcp_transport="sse",
                ltm_mcp_url="https://alice:s3cret@ltm.example/sse",
            )
        )

        async def _boom():
            raise RuntimeError(
                "Server error '502 Bad Gateway' for url 'https://alice:s3cret@ltm.example/sse'"
            )

        adapter.start = _boom  # type: ignore[method-assign]
        with caplog.at_level("WARNING", logger="memtomem_stm.surfacing.mcp_client"):
            ok = await adapter._heal_if_needed()
        assert ok is False
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "surfacing disabled" in joined
        assert "s3cret" not in joined
        assert "***@ltm.example" in joined


class TestReconnectRetrySuccess:
    """A transient transport failure followed by a successful reconnect must
    deliver the retry's actual results to the caller, not silently drop them."""

    @pytest.mark.asyncio
    async def test_transient_failure_then_retry_returns_results(self):
        # The session is injected directly (no start() → no format
        # negotiation), so pin the legacy compact format to match the
        # compact fixture text below. Same pattern in the other
        # injected-session tests in this module that parse results.
        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="compact"))

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

        results, hints, outcome = await adapter.search("anything")

        adapter._reconnect.assert_awaited_once()
        assert mock_session.call_tool.await_count == 2
        assert len(results) == 1
        assert "retry worked" in results[0].chunk.content.lower()
        assert results[0].score == 0.95
        assert hints == []
        assert outcome == "ok"


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

        results, hints, outcome = await adapter.search("q")

        assert results == []
        assert hints == []
        assert outcome == "transport_error"
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

        results, hints, outcome = await adapter.search("q")

        assert results == []
        assert hints == []
        assert outcome == "transport_error"
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

        results, hints, outcome = await adapter.search("q")

        assert results == []
        assert hints == []
        assert outcome == "call_error"
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


# ── Network transports (#398): httpx errors are transport errors ─────────


class TestNetworkTransportErrorsReconnect:
    """The sse / streamable_http clients raise httpx errors whose MRO has no
    OSError ancestor. They must be classified as transport errors (reconnect +
    retry, outcome ``transport_error``) — pre-fix they fell into the generic
    handler (outcome ``call_error``), the adapter never healed, and surfacing
    stayed dead over the network path after the first blip."""

    @staticmethod
    def _network_adapter(transport: str = "sse") -> McpClientSearchAdapter:
        # result_format pinned to match the compact fixture text (injected
        # session, no negotiation).
        return McpClientSearchAdapter(
            SurfacingConfig(
                ltm_mcp_transport=transport,
                ltm_mcp_url="http://127.0.0.1:9/mcp",
                result_format="compact",
            )
        )

    @pytest.mark.parametrize("transport", ["sse", "streamable_http"])
    @pytest.mark.parametrize(
        "exc_type",
        [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError],
        ids=["ConnectError", "ReadTimeout", "RemoteProtocolError"],
    )
    @pytest.mark.asyncio
    async def test_httpx_error_triggers_reconnect_and_retry(self, exc_type, transport):
        adapter = self._network_adapter(transport)

        good_result = _result_with_text("[1] 0.95 | [default] src/app.py\nRecovered fine.\n")
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(side_effect=[exc_type("network blip"), good_result])
        adapter._session = mock_session
        adapter._reconnect = AsyncMock()  # type: ignore[method-assign]

        results, hints, outcome = await adapter.search("q")

        adapter._reconnect.assert_awaited_once()
        assert mock_session.call_tool.await_count == 2
        assert outcome == "ok"
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_httpx_error_with_failed_reconnect_is_transport_error(self):
        """Outcome must be ``transport_error`` (reconnect path), not
        ``call_error`` (generic handler — the pre-fix misclassification)."""
        adapter = self._network_adapter("streamable_http")

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(side_effect=httpx.ConnectError("refused"))
        adapter._session = mock_session
        adapter._reconnect = AsyncMock(  # type: ignore[method-assign]
            side_effect=httpx.ConnectError("still refused")
        )

        results, hints, outcome = await adapter.search("q")

        assert results == []
        assert hints == []
        assert outcome == "transport_error"
        adapter._reconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_httpx_status_error_does_not_reconnect(self):
        """An HTTP status error means the server answered — application-level,
        not a transport failure; reconnecting would mask real errors."""
        adapter = self._network_adapter()

        request = httpx.Request("POST", "http://127.0.0.1:9/mcp")
        response = httpx.Response(500, request=request)
        exc = httpx.HTTPStatusError("server error", request=request, response=response)
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(side_effect=exc)
        adapter._session = mock_session
        adapter._reconnect = AsyncMock()  # type: ignore[method-assign]

        results, hints, outcome = await adapter.search("q")

        assert results == []
        assert outcome == "call_error"
        adapter._reconnect.assert_not_awaited()


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
        results, hints = adapter._parser.parse(compact)
        assert len(results) == 1
        assert hints == []
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

        results, hints, outcome = await adapter.search("anything")
        assert results == []
        assert hints == []
        # ``result.content is None`` is treated as a missing-text response,
        # not a transport error — outcome is ``empty_content`` (#295).
        assert outcome == "empty_content"

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

        # start() now marshals to the adapter's owner task (#663) — join it
        # so no pending task leaks into event-loop teardown.
        await adapter.stop()


# ── c.text=None tolerance (PR #114 parity) ──────────────────────────────


class TestTextNoneTolerance:
    """MCP spec requires TextContent.text to be str, but a spec-noncompliant
    server may return None. manager.py:1042 guards with ``c.text or ""``;
    the surfacing adapter must do the same."""

    @pytest.mark.asyncio
    async def test_search_tolerates_none_text(self):
        # result_format pinned to match the compact fixture text (injected
        # session, no negotiation).
        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="compact"))
        mock_session = AsyncMock()

        none_content = MagicMock()
        none_content.type = "text"
        none_content.text = None

        good_content = _text_content("[1] 0.90 | [default] note.md\nreal content")

        result_obj = MagicMock()
        result_obj.content = [none_content, good_content]
        mock_session.call_tool = AsyncMock(return_value=result_obj)
        adapter._session = mock_session

        results, _, outcome = await adapter.search("test query")
        assert len(results) == 1
        assert "real content" in results[0].chunk.content
        assert outcome == "ok"

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

        results, _, outcome = await adapter.search("test query")
        assert results == []
        # All-None text → ``text_parts`` was non-empty but stringified to
        # empty content; the parser still ran and returned ``[]`` →
        # ``empty_results`` (#295). The all-None-string case is parser-empty,
        # not adapter-empty.
        assert outcome == "empty_results"


# ── Outer wait_for cancellation (#290) ──────────────────────────────────


class TestOuterCancellationLazyReconnect:
    """``SurfacingEngine`` wraps adapter calls in ``asyncio.wait_for``. When the
    outer timeout fires, the inner ``call_tool`` is cancelled mid-RPC and the
    MCP session is left in a half-read state. ``_TRANSPORT_ERRORS`` must NOT
    catch ``CancelledError`` (cooperative cancellation must propagate), but
    the next adapter call must heal the connection lazily before issuing a
    fresh RPC — otherwise the next surfacing cycle hangs or sees out-of-order
    responses on the same stream."""

    @pytest.mark.asyncio
    async def test_mid_rpc_cancellation_marks_for_reconnect(self):
        adapter = McpClientSearchAdapter(SurfacingConfig())

        async def hang(*_args, **_kwargs):
            # Never returns on its own — outer wait_for must cancel us.
            await asyncio.sleep(10)

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(side_effect=hang)
        adapter._session = mock_session
        adapter._reconnect = AsyncMock()  # type: ignore[method-assign]

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.search("q"), timeout=0.05)

        # Flag set so the next caller heals the session before issuing an RPC.
        assert adapter._needs_reconnect is True
        # The cancellation alone does not synchronously trigger reconnect —
        # heal is lazy on the next call.
        adapter._reconnect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_next_call_lazy_reconnects_after_cancellation(self):
        # result_format pinned to match the compact fixture text (injected
        # session, no negotiation).
        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="compact"))
        adapter._needs_reconnect = True  # simulate prior cancellation

        compact_output = "[1] 0.90 | [default] note.md\nhealed result\n"
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=_result_with_text(compact_output))
        adapter._session = mock_session
        adapter._reconnect = AsyncMock()  # type: ignore[method-assign]

        results, _, outcome = await adapter.search("q")

        adapter._reconnect.assert_awaited_once()
        assert adapter._needs_reconnect is False
        assert len(results) == 1
        assert "healed result" in results[0].chunk.content
        assert outcome == "ok"

    @pytest.mark.asyncio
    async def test_failed_lazy_reconnect_returns_empty_and_keeps_flag(self):
        """If the heal itself fails, the call returns empty (matching the
        ``_session is None`` path) and the flag stays so a future call can
        try again — the adapter must not get stuck pretending it's healthy."""
        adapter = McpClientSearchAdapter(SurfacingConfig())
        adapter._needs_reconnect = True

        mock_session = AsyncMock()
        adapter._session = mock_session
        adapter._reconnect = AsyncMock(side_effect=ConnectionError("still down"))  # type: ignore[method-assign]

        results, hints, outcome = await adapter.search("q")

        assert results == []
        assert hints == []
        # Failed lazy heal leaves the adapter in the same surface state as
        # ``_session is None`` — both report ``no_session`` so the engine
        # records ``ltm_unavailable`` instead of a no-results bucket (#295).
        assert outcome == "no_session"
        # Flag preserved so a later call retries the heal rather than
        # silently accepting the broken state.
        assert adapter._needs_reconnect is True
        # call_tool was never reached because heal failed.
        mock_session.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_increment_access_cancellation_marks_for_reconnect(self):
        adapter = McpClientSearchAdapter(SurfacingConfig())

        async def hang(*_args, **_kwargs):
            await asyncio.sleep(10)

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(side_effect=hang)
        adapter._session = mock_session

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                adapter.increment_access(["chunk-1"]),
                timeout=0.05,
            )

        assert adapter._needs_reconnect is True

    @pytest.mark.asyncio
    async def test_scratch_list_cancellation_marks_for_reconnect(self):
        adapter = McpClientSearchAdapter(SurfacingConfig())

        async def hang(*_args, **_kwargs):
            await asyncio.sleep(10)

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(side_effect=hang)
        adapter._session = mock_session

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.scratch_list(), timeout=0.05)

        assert adapter._needs_reconnect is True


# ── Lazy start paths ─────────────────────────────────────────────────────


class TestLazyStart:
    """Adapter.start() is deferred from ``app_lifespan`` to the first RPC.

    The host's proxy startup used to hang on ``mcp_adapter.start()`` waiting
    for the LTM subprocess's MCP handshake, which exceeded codex's 60s
    startup_timeout and caused codex to respawn the proxy — creating two
    parallel LTM children. The fix moves start() into ``_heal_if_needed``
    so the proxy's own initialize() returns immediately. These tests pin
    the new contract: lazy bootstrap, sticky failure, lock-serialized
    concurrent first-callers.
    """

    @pytest.mark.asyncio
    async def test_first_search_triggers_start_and_returns_results(self):
        """No prior ``start()`` — search() must bootstrap the session itself
        and deliver real results, not the silent ``no_session`` of the old
        eager-start contract."""
        # result_format pinned to match the compact fixture text (start()
        # is mocked, so no negotiation runs).
        adapter = McpClientSearchAdapter(SurfacingConfig(result_format="compact"))

        compact_output = "[1] 0.9 | [default] src/a.py\nfrom lazy start.\n"
        good_result = _result_with_text(compact_output)
        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=good_result)

        async def fake_start():
            adapter._session = mock_session

        adapter.start = AsyncMock(side_effect=fake_start)  # type: ignore[method-assign]

        results, hints, outcome = await adapter.search("q")

        adapter.start.assert_awaited_once()
        assert outcome == "ok"
        assert len(results) == 1
        assert "lazy start" in results[0].chunk.content.lower()
        assert adapter._start_attempted is True

    @pytest.mark.asyncio
    async def test_failed_start_is_sticky_within_lifecycle(self):
        """A start() that raises must flip ``_start_attempted`` and stop
        retrying. Otherwise every surfacing cycle would respawn the LTM
        subprocess — the exact thundering-herd the lifespan-shot avoided."""
        adapter = McpClientSearchAdapter(SurfacingConfig())
        adapter.start = AsyncMock(  # type: ignore[method-assign]
            side_effect=ConnectionError("LTM unreachable"),
        )

        first = await adapter.search("q1")
        second = await adapter.search("q2")

        assert first == ([], [], "no_session")
        assert second == ([], [], "no_session")
        # Only the first call attempted start; the second short-circuits
        # on the sticky flag instead of spawning a fresh LTM subprocess.
        adapter.start.assert_awaited_once()
        assert adapter._start_attempted is True
        assert adapter._session is None

    @pytest.mark.asyncio
    async def test_concurrent_first_callers_serialize_start(self):
        """Surfacing fires from multiple coroutines per request. Without the
        lock, two coroutines hitting ``_session is None`` simultaneously
        would each spawn a separate LTM subprocess — the same dual-process
        regression we just fixed at the lifespan level."""
        adapter = McpClientSearchAdapter(SurfacingConfig())

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=_result_with_text(""))

        start_call_count = 0

        async def fake_start():
            nonlocal start_call_count
            start_call_count += 1
            # Yield once so a concurrent caller has a real chance to race
            # the assignment — without the lock this is where the bug would
            # show up as start_call_count == 2.
            await asyncio.sleep(0)
            adapter._session = mock_session

        adapter.start = AsyncMock(side_effect=fake_start)  # type: ignore[method-assign]

        results = await asyncio.gather(
            adapter.search("q1"),
            adapter.search("q2"),
            adapter.search("q3"),
        )

        assert start_call_count == 1
        adapter.start.assert_awaited_once()
        # All three callers see the same healthy session.
        for r in results:
            assert r[2] in {"ok", "empty_content", "empty_results"}

    @pytest.mark.asyncio
    async def test_cancelled_start_resets_flag_and_propagates(self):
        """If ``start()`` is cancelled mid-init (outer wait_for timeout),
        the sticky flag must reset so a later cycle can retry — otherwise
        a single timeout permanently disables surfacing in exactly the
        slow-startup environments lazy-start was meant to help.
        Cancellation must propagate (cooperative cancellation, #290)."""
        adapter = McpClientSearchAdapter(SurfacingConfig())

        async def slow_start():
            # Simulate a long-running LTM handshake that an outer
            # wait_for will cancel before it completes.
            await asyncio.sleep(10)

        adapter.start = AsyncMock(side_effect=slow_start)  # type: ignore[method-assign]

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.search("q"), timeout=0.05)

        # Flag reset → next caller can retry.
        assert adapter._start_attempted is False
        # No session ever materialized.
        assert adapter._session is None

    @pytest.mark.asyncio
    async def test_cancelled_start_allows_retry_on_next_call(self):
        """End-to-end: cancellation on attempt 1 must not block attempt 2.
        Pairs with the unit test above — proves the reset actually
        un-sticks the path rather than just clearing a flag in isolation."""
        adapter = McpClientSearchAdapter(SurfacingConfig())

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=_result_with_text(""))

        attempt = 0

        async def start_impl():
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                # First attempt: hang until outer wait_for cancels us.
                await asyncio.sleep(10)
            # Second attempt: succeed instantly.
            adapter._session = mock_session

        adapter.start = AsyncMock(side_effect=start_impl)  # type: ignore[method-assign]

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.search("q1"), timeout=0.05)

        # Second call must reach start() again (not short-circuit on the
        # sticky flag) and succeed.
        results, _, outcome = await adapter.search("q2")
        assert attempt == 2
        assert outcome in {"ok", "empty_content", "empty_results"}
        assert adapter._session is mock_session


# ── Owner-task lifecycle (#663) ──────────────────────────────────────────


_COMPACT_HIT = "[1] 0.90 | [default] note.md\nowner task test content"


class _TrackingTransport:
    """Fake stdio transport recording which task enters/exits its context.

    Each instance gets a monotonic ``id`` so a test can assert *which*
    transport was exited — an abandoned (never-aclosed) stack leaves its
    id out of ``record["exited_ids"]`` entirely.
    """

    def __init__(self, record: dict, gate: asyncio.Event | None = None):
        self._record = record
        self._gate = gate
        self._id = record["next_id"] = record.get("next_id", 0) + 1

    async def __aenter__(self):
        self._record["enter_task"] = asyncio.current_task()
        self._record["enters"] = self._record.get("enters", 0) + 1
        self._record["last_entered_id"] = self._id
        if self._gate is not None:
            await self._gate.wait()
        return (MagicMock(), MagicMock())

    async def __aexit__(self, *args):
        self._record["exit_task"] = asyncio.current_task()
        self._record.setdefault("exited_ids", []).append(self._id)
        return None


class _OkSession:
    """Fake ClientSession serving a canned compact mem_search hit."""

    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def initialize(self):
        pass

    async def call_tool(self, _name, _args):
        return _result_with_text(_COMPACT_HIT)


class TestOwnerTaskLifecycle:
    """#663: lifecycle ops must enter AND exit the transport/session contexts
    in one dedicated owner task. Under lazy start the first RPC arrives in a
    short-lived request-handler task; entering anyio cancel scopes there
    corrupted that task's scope stack on exit (killing the STM server right
    after the first successful surfacing) and made ``stop()`` from the
    lifespan task raise the cross-task cancel-scope RuntimeError."""

    def _adapter(self, monkeypatch, record: dict, gate: asyncio.Event | None = None):
        from memtomem_stm.surfacing import mcp_client as mod

        monkeypatch.setattr(mod, "stdio_client", lambda _params: _TrackingTransport(record, gate))
        monkeypatch.setattr(mod, "ClientSession", _OkSession)
        # result_format pinned to compact so no version negotiation runs.
        return McpClientSearchAdapter(SurfacingConfig(result_format="compact"))

    @pytest.mark.asyncio
    async def test_contexts_enter_and_exit_in_owner_task(self, monkeypatch):
        record: dict = {}
        adapter = self._adapter(monkeypatch, record)

        async def caller():
            _, _, outcome = await adapter.search("q")
            assert outcome == "ok"
            return asyncio.current_task()

        caller_task = asyncio.create_task(caller())
        await caller_task
        assert record["enter_task"] is not caller_task, (
            "contexts must not be entered in the request task"
        )

        # Stop from yet another task: exit must still happen in the task
        # that entered (the owner), and nothing may raise.
        await asyncio.create_task(adapter.stop())
        assert record["exit_task"] is record["enter_task"]

    @pytest.mark.asyncio
    async def test_lazy_start_survives_request_task_exit(self, monkeypatch):
        """The #663 shape: the request task that triggered the lazy start
        exits, a later request from a different task still works, and a
        final stop from the main task is clean."""
        record: dict = {}
        adapter = self._adapter(monkeypatch, record)

        await asyncio.create_task(adapter.search("first"))  # task exits after this
        results, _, outcome = await asyncio.create_task(adapter.search("second"))
        assert outcome == "ok"
        assert len(results) == 1
        assert record["enters"] == 1  # one session, reused across tasks

        await adapter.stop()
        assert adapter._session is None
        assert adapter._owner_task is not None and adapter._owner_task.done()

    @pytest.mark.asyncio
    async def test_stop_before_start_and_double_stop_and_rpc_after_stop(self):
        adapter = McpClientSearchAdapter(SurfacingConfig())

        await adapter.stop()  # stop before any start: no-op, no owner spawned
        assert adapter._owner_task is None
        await adapter.stop()  # idempotent

        # RPC after stop must degrade fast, not hang or spawn anything.
        results, hints, outcome = await asyncio.wait_for(adapter.search("q"), timeout=1.0)
        assert (results, hints, outcome) == ([], [], "no_session")
        assert adapter._owner_task is None

    @pytest.mark.asyncio
    async def test_caller_cancelled_mid_start_owner_survives(self, monkeypatch):
        """Outer wait_for cancels the caller while the start op is in flight
        in the owner: rollback runs in the owner task, the sticky flag
        resets, and the owner keeps serving the next attempt."""
        record: dict = {}
        gate = asyncio.Event()
        adapter = self._adapter(monkeypatch, record, gate)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.search("q"), timeout=0.05)

        assert adapter._start_attempted is False
        assert adapter._session is None
        owner = adapter._owner_task
        assert owner is not None and not owner.done(), "op cancel must not kill the owner"

        gate.set()
        _, _, outcome = await asyncio.wait_for(adapter.search("q"), timeout=1.0)
        assert outcome == "ok"
        assert record["enters"] == 2  # first attempt rolled back, second succeeded
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_caller_cancelled_while_queued_op_is_skipped(self, monkeypatch):
        record: dict = {}
        gate = asyncio.Event()
        adapter = self._adapter(monkeypatch, record, gate)

        t1 = asyncio.create_task(adapter.start())
        await asyncio.sleep(0.01)  # owner picked up op1, blocked on the gate
        t2 = asyncio.create_task(adapter.start())
        await asyncio.sleep(0.01)  # op2 queued behind op1
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t2

        gate.set()
        await t1  # op1 unaffected by op2's abandonment
        assert adapter._session is not None
        assert record["enters"] == 1, "abandoned queued op must not run"
        owner = adapter._owner_task
        assert owner is not None and not owner.done()
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_owner_crash_recovers_without_hanging(self, monkeypatch):
        """If the owner task is lost to an external cancellation, in-flight
        RPCs on the existing session keep working and the next lifecycle op
        recreates the owner instead of hanging on a dead queue.

        Regression for the crash-recovery half of #663: the replacement
        owner must NOT aclose the contexts entered by the dead owner (their
        anyio cancel scopes are affine to that gone task — acloseing them
        cross-task re-raises the very RuntimeError being fixed). It abandons
        the stale stack unclosed instead.
        """
        record: dict = {}
        adapter = self._adapter(monkeypatch, record)

        await adapter.start()
        owner = adapter._owner_task
        assert owner is not None
        stale_id = record["last_entered_id"]  # transport entered by the dead owner
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner

        # call_tool is task-agnostic — the surviving session still serves.
        _, _, outcome = await asyncio.wait_for(adapter.search("q"), timeout=1.0)
        assert outcome == "ok"

        # A lifecycle op recreates the owner rather than hanging. The stale
        # contexts are abandoned (not aclosed cross-task), so a fresh
        # transport is entered.
        await asyncio.wait_for(adapter._reconnect(), timeout=1.0)
        assert adapter._owner_task is not owner
        assert adapter._session is not None
        assert record["last_entered_id"] != stale_id, "reconnect must enter a fresh transport"
        assert stale_id not in record.get("exited_ids", []), (
            "the dead owner's contexts must be abandoned, not aclosed cross-task"
        )

        await adapter.stop()
        # Final stop closes only the live (post-reconnect) transport; the
        # stale one stays leaked for the process/daemon sweep to reap.
        assert stale_id not in record.get("exited_ids", [])

    @pytest.mark.asyncio
    async def test_stop_unblocks_stuck_start_caller(self, monkeypatch):
        """stop() against an owner stuck mid-start must cancel the op
        in-task, resolve the stuck caller (→ no_session), and return in
        bounded time."""
        record: dict = {}
        gate = asyncio.Event()  # never set — start blocks forever
        adapter = self._adapter(monkeypatch, record, gate)
        adapter._STOP_TIMEOUT_SECONDS = 0.2  # type: ignore[misc]

        t1 = asyncio.create_task(adapter.search("q"))
        await asyncio.sleep(0.05)  # owner blocked inside the transport enter

        await asyncio.wait_for(adapter.stop(), timeout=2.0)

        results, hints, outcome = await asyncio.wait_for(t1, timeout=1.0)
        assert (results, hints, outcome) == ([], [], "no_session")
        assert adapter._owner_task is not None and adapter._owner_task.done()
