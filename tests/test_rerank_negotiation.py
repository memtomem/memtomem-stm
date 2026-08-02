"""Per-call rerank bypass negotiation (core #1766).

The ``rerank`` argument only exists on cores newer than v0.3.11; an older
FastMCP server rejects the unknown key, which would surface as ``call_error``
and charge the circuit breaker. The adapter therefore probes the ``mem_search``
tool schema once per session and only sends the key when it is advertised —
these tests pin both halves: the probe and the conditional injection.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter


def _search_response() -> SimpleNamespace:
    return SimpleNamespace(
        is_error=False,
        content=[SimpleNamespace(type="text", text=json.dumps({"results": []}))],
    )


def _tool(name: str, properties: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(name=name, input_schema={"type": "object", "properties": properties})


def _session(*, rerank_capable: bool) -> AsyncMock:
    properties: dict[str, object] = {"query": {"type": "string"}}
    if rerank_capable:
        properties["rerank"] = {"anyOf": [{"type": "boolean"}, {"type": "null"}]}
    session = AsyncMock()
    session.list_tools.return_value = SimpleNamespace(
        tools=[_tool("mem_do", {}), _tool("mem_search", properties)]
    )
    session.call_tool.return_value = _search_response()
    return session


async def _searched_args(adapter: McpClientSearchAdapter, session: AsyncMock) -> dict[str, object]:
    adapter._session = session
    adapter._start_attempted = True
    await adapter.search("deployment", top_k=6)
    call = session.call_tool.await_args
    assert call.args[0] == "mem_search"
    return call.args[1]


@pytest.mark.asyncio
async def test_default_config_sends_rerank_false_to_capable_server() -> None:
    adapter = McpClientSearchAdapter(SurfacingConfig())
    session = _session(rerank_capable=True)
    await adapter._probe_rerank_support(session)
    args = await _searched_args(adapter, session)
    assert args["rerank"] is False


@pytest.mark.asyncio
async def test_rerank_true_is_forwarded_to_capable_server() -> None:
    adapter = McpClientSearchAdapter(SurfacingConfig(rerank=True))
    session = _session(rerank_capable=True)
    await adapter._probe_rerank_support(session)
    args = await _searched_args(adapter, session)
    assert args["rerank"] is True


@pytest.mark.asyncio
async def test_old_server_never_sees_the_rerank_key() -> None:
    adapter = McpClientSearchAdapter(SurfacingConfig())
    session = _session(rerank_capable=False)
    await adapter._probe_rerank_support(session)
    args = await _searched_args(adapter, session)
    assert "rerank" not in args


@pytest.mark.asyncio
async def test_rerank_none_skips_the_probe_and_sends_no_key() -> None:
    adapter = McpClientSearchAdapter(SurfacingConfig(rerank=None))
    session = _session(rerank_capable=True)
    await adapter._probe_rerank_support(session)
    session.list_tools.assert_not_awaited()
    args = await _searched_args(adapter, session)
    assert "rerank" not in args


@pytest.mark.asyncio
async def test_rerank_none_wins_even_if_the_latch_is_set() -> None:
    """The injection requires BOTH an explicit decision and a capable server."""
    adapter = McpClientSearchAdapter(SurfacingConfig(rerank=None))
    adapter._rerank_param_supported = True
    session = _session(rerank_capable=True)
    args = await _searched_args(adapter, session)
    assert "rerank" not in args


@pytest.mark.asyncio
async def test_probe_failure_degrades_to_unsupported_without_raising() -> None:
    adapter = McpClientSearchAdapter(SurfacingConfig())
    adapter._rerank_param_supported = True
    session = AsyncMock()
    session.list_tools.side_effect = RuntimeError("tools/list unsupported")
    await adapter._probe_rerank_support(session)
    assert adapter._rerank_param_supported is False


@pytest.mark.asyncio
async def test_reprobe_against_older_server_resets_the_latch() -> None:
    """Reconnects re-negotiate; a downgrade must not leak support forward."""
    adapter = McpClientSearchAdapter(SurfacingConfig())
    await adapter._probe_rerank_support(_session(rerank_capable=True))
    assert adapter._rerank_param_supported is True
    await adapter._probe_rerank_support(_session(rerank_capable=False))
    assert adapter._rerank_param_supported is False


@pytest.mark.asyncio
async def test_compact_format_still_gets_the_bypass() -> None:
    """The probe is orthogonal to result-format negotiation: compact users on
    a new core keep the latency win (pins the probe living OUTSIDE
    ``_negotiate_format``, which early-returns for compact parsers)."""
    adapter = McpClientSearchAdapter(SurfacingConfig(result_format="compact"))
    session = _session(rerank_capable=True)
    session.call_tool.return_value = SimpleNamespace(
        is_error=False, content=[SimpleNamespace(type="text", text="")]
    )
    await adapter._probe_rerank_support(session)
    args = await _searched_args(adapter, session)
    assert args["rerank"] is False
    assert "output_format" not in args


@pytest.mark.asyncio
async def test_compose_params_carry_rerank_only_when_supported() -> None:
    from memtomem_stm.surfacing.mcp_client import LtmCapabilities

    for supported, expected in ((True, {"rerank": False}), (False, {})):
        adapter = McpClientSearchAdapter(SurfacingConfig())
        adapter._capabilities = LtmCapabilities(context_compose_schema=2)
        adapter._rerank_param_supported = supported
        session = AsyncMock()
        session.call_tool.return_value = SimpleNamespace(
            is_error=False,
            content=[
                SimpleNamespace(type="text", text=json.dumps({"pinned": [], "retrieved": []}))
            ],
        )
        adapter._session = session
        adapter._start_attempted = True
        await adapter.context_compose("deployment", max_chars=2000, top_k=6)
        tool, args = session.call_tool.await_args.args
        assert tool == "mem_do"
        params = args["params"]
        assert {k: v for k, v in params.items() if k == "rerank"} == expected


@pytest.mark.asyncio
async def test_search_retry_after_reconnect_reevaluates_the_latch() -> None:
    """A transport-error retry reuses the args dict built for the
    pre-reconnect session; the reconnect re-probes the replacement core,
    which may be OLDER (rollback) — the retry must re-derive the key from
    the current latch, not replay the stale injection."""
    adapter = McpClientSearchAdapter(SurfacingConfig())
    adapter._rerank_param_supported = True
    # Snapshot the args at send time: the retry mutates the SAME dict the
    # mock would otherwise record by reference.
    sent: list[dict[str, object]] = []
    old_session = AsyncMock()

    async def fail_first(tool: str, args: dict[str, object]) -> SimpleNamespace:
        sent.append(dict(args))
        raise OSError("transport died")

    old_session.call_tool.side_effect = fail_first
    new_session = AsyncMock()

    async def succeed(tool: str, args: dict[str, object]) -> SimpleNamespace:
        sent.append(dict(args))
        return _search_response()

    new_session.call_tool.side_effect = succeed
    adapter._session = old_session
    adapter._start_attempted = True

    async def fake_reconnect(generation: int) -> None:
        adapter._session = new_session
        adapter._generation += 1
        adapter._rerank_param_supported = False  # replacement core predates #1766

    adapter._shared_reconnect = fake_reconnect  # type: ignore[method-assign]
    _, _, outcome = await adapter.search("deployment")

    assert outcome == "empty_results"
    first_args, retry_args = sent
    assert first_args["rerank"] is False  # positive control: capable session got the key
    assert "rerank" not in retry_args


@pytest.mark.asyncio
async def test_compose_retry_after_reconnect_reevaluates_the_latch() -> None:
    from memtomem_stm.surfacing.mcp_client import LtmCapabilities

    adapter = McpClientSearchAdapter(SurfacingConfig())
    adapter._capabilities = LtmCapabilities(context_compose_schema=2)
    adapter._rerank_param_supported = True
    sent: list[dict[str, object]] = []
    old_session = AsyncMock()

    async def fail_first(tool: str, args: dict[str, object]) -> SimpleNamespace:
        sent.append(json.loads(json.dumps(args)))
        raise OSError("transport died")

    old_session.call_tool.side_effect = fail_first
    new_session = AsyncMock()

    async def succeed(tool: str, args: dict[str, object]) -> SimpleNamespace:
        sent.append(json.loads(json.dumps(args)))
        return SimpleNamespace(
            is_error=False,
            content=[
                SimpleNamespace(type="text", text=json.dumps({"pinned": [], "retrieved": []}))
            ],
        )

    new_session.call_tool.side_effect = succeed
    adapter._session = old_session
    adapter._start_attempted = True

    async def fake_reconnect(generation: int) -> None:
        adapter._session = new_session
        adapter._generation += 1
        adapter._rerank_param_supported = False

    adapter._shared_reconnect = fake_reconnect  # type: ignore[method-assign]
    bundle = await adapter.context_compose("deployment")

    assert bundle is not None
    first_args, retry_args = sent
    assert first_args["params"]["rerank"] is False
    assert "rerank" not in retry_args["params"]


def test_env_style_strings_reach_the_tri_state() -> None:
    assert SurfacingConfig(rerank="none").rerank is None  # type: ignore[arg-type]
    assert SurfacingConfig(rerank="null").rerank is None  # type: ignore[arg-type]
    assert SurfacingConfig(rerank="").rerank is None  # type: ignore[arg-type]
    assert SurfacingConfig(rerank="false").rerank is False  # type: ignore[arg-type]
    assert SurfacingConfig(rerank="true").rerank is True  # type: ignore[arg-type]
    assert SurfacingConfig().rerank is False
