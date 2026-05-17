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
        adapter = McpClientSearchAdapter(SurfacingConfig())
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
        adapter = McpClientSearchAdapter(SurfacingConfig())

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
