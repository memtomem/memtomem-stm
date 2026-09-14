"""``stm_admin`` — one MCP tool dispatching the eight observability actions.

The dispatcher replaced eight individually advertised tools. What must not
change for a caller is how arguments are validated: the individual tools went
through the SDK's ``func_metadata`` argument model, so the dispatcher reuses
that model rather than re-validating per field. The parity class below runs
the same inputs through both and compares.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.func_metadata import func_metadata
from pydantic import ValidationError

from memtomem_stm.config import STMConfig
from memtomem_stm.proxy.config import ProxyConfig
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.server import (
    _OBSERVABILITY_ACTIONS,
    _OBSERVABILITY_TOOL_NAMES,
    STMContext,
    stm_admin,
)

_FLAG_ENV = "MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS"


def _make_ctx(
    *,
    proxy_manager: ProxyManager | None = None,
    surfacing_engine: object | None = None,
) -> SimpleNamespace:
    tracker = TokenTracker()
    if proxy_manager is None:
        proxy_manager = ProxyManager(
            ProxyConfig(config_path="/tmp/proxy.json", upstream_servers={}), tracker
        )
    app = STMContext(
        config=STMConfig(),
        proxy_manager=proxy_manager,
        tracker=tracker,
        surfacing_engine=surfacing_engine,
        feedback_tracker=None,
        compression_feedback_tracker=None,
        progressive_reads_tracker=None,
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


def _pm_with_cache(cleared: int = 5) -> tuple[ProxyManager, MagicMock]:
    pm = ProxyManager(ProxyConfig(config_path="/tmp/p.json", upstream_servers={}), TokenTracker())
    cache = MagicMock()
    cache.clear.return_value = cleared
    pm._cache = cache
    return pm, cache


# ── catalog ──────────────────────────────────────────────────────────────


class TestCatalog:
    def test_actions_are_the_eight_observability_functions(self):
        assert list(_OBSERVABILITY_ACTIONS) == [
            name.removeprefix("stm_") for name in _OBSERVABILITY_TOOL_NAMES
        ]
        for name, entry in _OBSERVABILITY_ACTIONS.items():
            assert entry.fn.__name__ == f"stm_{name}"

    async def test_help_lists_every_action_and_every_parameter(self):
        result = await stm_admin("help")
        for name, entry in _OBSERVABILITY_ACTIONS.items():
            assert f"- {name}: " in result
            for field in entry.meta.arg_model.model_fields:
                assert f"{field}: " in result, (name, field)
        assert "ctx" not in result

    async def test_help_for_one_action_returns_its_full_docstring(self):
        result = await stm_admin("help", {"action": "proxy_cache_clear"})
        assert result.startswith("stm_admin action 'proxy_cache_clear'")
        assert "server: str | None = None, tool: str | None = None" in result
        # The semantics that only the full docstring carries.
        assert "An unfiltered call flushes BOTH" in result
        assert "- surfacing_stats" not in result

    async def test_help_for_an_action_without_parameters_says_so(self):
        result = await stm_admin("help", {"action": "proxy_health"})
        assert "params: (none)" in result

    async def test_help_for_unknown_action_is_an_error(self):
        with pytest.raises(ToolError, match=r"^unknown action 'nope'"):
            await stm_admin("help", {"action": "nope"})

    async def test_help_refuses_an_unknown_parameter_instead_of_ignoring_it(self):
        with pytest.raises(ToolError) as info:
            await stm_admin("help", {"acton": "proxy_health"})
        assert str(info.value) == "unknown parameter 'acton' for action 'help'; accepted: action."

    async def test_help_refuses_a_non_string_action_filter(self):
        with pytest.raises(ToolError) as info:
            await stm_admin("help", {"action": []})
        assert str(info.value) == "invalid parameter for action 'help' — action (string_type)."

    async def test_help_refuses_params_that_are_not_an_object(self):
        with pytest.raises(ToolError) as info:
            await stm_admin("help", ["proxy_health"])  # type: ignore[arg-type]
        assert str(info.value) == "params for action 'help' must be an object."


# ── dispatch errors ──────────────────────────────────────────────────────


class TestDispatchErrors:
    async def test_unknown_action(self):
        with pytest.raises(ToolError) as info:
            await stm_admin("proxy_statz")
        message = str(info.value)
        assert message.startswith("unknown action 'proxy_statz'")
        assert "proxy_stats" in message and "help" in message

    async def test_unknown_parameter_is_rejected_before_the_action_runs(self):
        # The SDK drops keys it does not know. For cache clear that would turn a
        # misspelled filter into an unfiltered flush of every cache.
        pm, cache = _pm_with_cache()
        engine = MagicMock()
        ctx = _make_ctx(proxy_manager=pm, surfacing_engine=engine)

        with pytest.raises(ToolError) as info:
            await stm_admin("proxy_cache_clear", {"sever": "gh"}, ctx=ctx)

        assert str(info.value).startswith("unknown parameter 'sever'")
        assert "accepted: server, tool" in str(info.value)
        cache.clear.assert_not_called()
        engine.clear_cache.assert_not_called()

    async def test_caller_supplied_ctx_is_rejected(self):
        with pytest.raises(ToolError, match=r"^unknown parameter 'ctx'"):
            await stm_admin("proxy_stats", {"ctx": None}, ctx=_make_ctx())

    async def test_non_string_action_is_refused_as_a_tool_error(self):
        with pytest.raises(ToolError) as info:
            await stm_admin(["proxy_stats"], ctx=_make_ctx())  # type: ignore[arg-type]
        assert str(info.value) == "action must be a string."

    async def test_params_must_be_an_object(self):
        with pytest.raises(ToolError) as info:
            await stm_admin("proxy_stats", ["tool"], ctx=_make_ctx())  # type: ignore[arg-type]
        assert str(info.value) == "params for action 'proxy_stats' must be an object."

    async def test_invalid_value_reports_location_and_type_but_not_the_value(self):
        sentinel = "SENTINEL-not-an-int-7f3a"
        with pytest.raises(ToolError) as info:
            await stm_admin("surfacing_stats", {"limit": sentinel}, ctx=_make_ctx())
        message = str(info.value)
        assert message.startswith("invalid parameter for action 'surfacing_stats'")
        assert "limit (int_parsing)" in message
        assert sentinel not in message
        # Nor in the chained cause, which a traceback-rendering host would print.
        assert info.value.__cause__ is None


# ── no-argument calls ────────────────────────────────────────────────────


class TestNoArgumentCalls:
    """``params`` omitted, ``None`` and ``{}`` are the same call.

    The SDK's ``validate_arguments`` copies its input, so a ``None`` passed
    straight through would raise on the commonest call of all.
    """

    @pytest.mark.parametrize("params", [None, {}])
    async def test_proxy_stats_runs(self, params):
        result = await stm_admin("proxy_stats", params, ctx=_make_ctx())
        assert "STM Proxy Stats" in result

    async def test_omitted_params_runs(self):
        result = await stm_admin("proxy_stats", ctx=_make_ctx())
        assert "STM Proxy Stats" in result


# ── dispatch reaches the real function ───────────────────────────────────


_REAL_ACTION_FIRST_LINES = {
    "proxy_stats": "STM Proxy Stats",
    "proxy_cache_clear": "No caches enabled",
    "proxy_health": "No upstream servers configured.",
    "surfacing_stats": "Feedback tracking is not enabled.",
    "selection_stats": "Selection telemetry is disabled",
    "compression_stats": "Compression feedback tracking is not enabled.",
    "progressive_stats": "Progressive reads tracking is not enabled.",
    "tuning_recommendations": "Metrics store is not enabled",
}


class TestDispatchReachesTheAction:
    def test_real_action_table_covers_every_action(self):
        assert set(_REAL_ACTION_FIRST_LINES) == set(_OBSERVABILITY_ACTIONS)

    @pytest.mark.parametrize("name", sorted(_REAL_ACTION_FIRST_LINES))
    async def test_every_real_action_runs_through_the_dispatcher(self, name):
        """No stand-in: the registered function runs with the injected context.

        With nothing enabled each action answers with its own first line, so a
        dispatcher that reached the wrong function, or none, fails here.
        """
        result = await stm_admin(name, ctx=_make_ctx())
        assert result.startswith(_REAL_ACTION_FIRST_LINES[name]), result

    @pytest.mark.parametrize("name", list(_OBSERVABILITY_ACTIONS))
    async def test_every_action_is_called_with_validated_arguments_and_ctx(self, name, monkeypatch):
        from memtomem_stm import server

        entry = _OBSERVABILITY_ACTIONS[name]
        spy = AsyncMock(return_value=f"ran {name}")
        monkeypatch.setitem(
            server._OBSERVABILITY_ACTIONS, name, server._ObsAction(fn=spy, meta=entry.meta)
        )
        ctx = object()

        result = await stm_admin(name, ctx=ctx)  # type: ignore[arg-type]

        assert result == f"ran {name}"
        kwargs = spy.await_args.kwargs
        assert kwargs.pop("ctx") is ctx
        # Every declared parameter arrives, at its default.
        assert set(kwargs) == set(entry.meta.arg_model.model_fields)

    async def test_filtered_cache_clear(self):
        pm, cache = _pm_with_cache(cleared=5)
        engine = MagicMock()
        ctx = _make_ctx(proxy_manager=pm, surfacing_engine=engine)

        result = await stm_admin("proxy_cache_clear", {"server": "srv", "tool": "t"}, ctx=ctx)

        cache.clear.assert_called_once_with(server="srv", tool="t")
        engine.clear_cache.assert_not_called()
        assert "5" in result and "srv/t" in result

    async def test_unfiltered_cache_clear_flushes_both_caches(self):
        pm, cache = _pm_with_cache(cleared=7)
        engine = MagicMock()
        engine.clear_cache.return_value = 3
        ctx = _make_ctx(proxy_manager=pm, surfacing_engine=engine)

        result = await stm_admin("proxy_cache_clear", ctx=ctx)

        cache.clear.assert_called_once_with()
        engine.clear_cache.assert_called_once_with()
        assert "response-cache" in result and "surfacing-cache" in result

    @pytest.mark.parametrize(
        ("params", "label"),
        [
            ({"server": ""}, "server ''"),
            ({"tool": ""}, "tool ''"),
            ({"server": "", "tool": ""}, "/."),
        ],
    )
    async def test_empty_filter_is_a_filter_not_a_clear_all(self, params, label):
        """An empty string is a filter that matches nothing, not an absent filter.

        ``stm_admin`` is advertised by default, so a model can send
        ``{"server": ""}``. Treating that as unfiltered flushed every cache.
        """
        pm, cache = _pm_with_cache(cleared=0)
        engine = MagicMock()
        ctx = _make_ctx(proxy_manager=pm, surfacing_engine=engine)

        result = await stm_admin("proxy_cache_clear", params, ctx=ctx)

        cache.clear.assert_called_once_with(server=params.get("server"), tool=params.get("tool"))
        engine.clear_cache.assert_not_called()
        assert "Cleared all caches" not in result
        assert label in result


# ── validation parity with the SDK ───────────────────────────────────────


async def _parity_probe(
    tool: str | None = None,
    limit: int = 10,
    ctx: object = None,
) -> str:
    return json.dumps({"tool": tool, "limit": limit})


_PARITY_CASES = [
    pytest.param({"tool": "null"}, id="json-null-string-becomes-none"),
    pytest.param({"tool": "[]"}, id="json-array-string-is-rejected"),
    pytest.param({"tool": "mem_search"}, id="plain-string"),
    pytest.param({"limit": "10"}, id="numeric-string-coerces"),
    pytest.param({"limit": "ten"}, id="non-numeric-string-is-rejected"),
    pytest.param({}, id="omitted-uses-defaults"),
]


def _sdk_outcome(arguments: dict) -> tuple[str, object]:
    """What the SDK did for a directly registered tool with this signature."""
    meta = func_metadata(_parity_probe, skip_names=["ctx"])
    try:
        return "ok", json.loads(
            asyncio.run(meta.call_fn_with_arg_validation(_parity_probe, True, arguments, {}))
        )
    except ValidationError as exc:
        return "error", sorted((tuple(e["loc"]), e["type"]) for e in exc.errors())


class TestValidationParity:
    @pytest.fixture
    def probe_action(self, monkeypatch):
        from memtomem_stm import server

        monkeypatch.setitem(
            server._OBSERVABILITY_ACTIONS,
            "parity_probe",
            server._ObsAction(
                fn=_parity_probe, meta=func_metadata(_parity_probe, skip_names=["ctx"])
            ),
        )

    @pytest.mark.parametrize("arguments", _PARITY_CASES)
    async def test_dispatcher_matches_the_sdk(self, probe_action, arguments):
        expected_kind, expected = await asyncio.to_thread(_sdk_outcome, dict(arguments))

        if expected_kind == "ok":
            result = await stm_admin("parity_probe", dict(arguments))
            assert json.loads(result) == expected
        else:
            with pytest.raises(ToolError) as info:
                await stm_admin("parity_probe", dict(arguments))
            message = str(info.value)
            assert message.startswith("invalid parameter for action 'parity_probe'")
            for loc, err_type in expected:  # type: ignore[union-attr]
                assert f"{'.'.join(map(str, loc))} ({err_type})" in message

    def test_the_cases_distinguish_sdk_parsing_from_plain_field_validation(self):
        """Positive control: the parity cases are not all ones a naive validator
        would also get right. A per-field ``TypeAdapter`` keeps ``"null"`` as a
        string and accepts ``"[]"``; the SDK does neither."""
        from pydantic import TypeAdapter

        adapter = TypeAdapter(str | None)
        assert adapter.validate_python("null") == "null"
        assert adapter.validate_python("[]") == "[]"
        assert _sdk_outcome({"tool": "null"}) == ("ok", {"tool": None, "limit": 10})
        assert _sdk_outcome({"tool": "[]"})[0] == "error"


# ── advertised cost ──────────────────────────────────────────────────────


def _advertised(flag: str) -> list[dict]:
    env = {k: v for k, v in os.environ.items() if k != _FLAG_ENV}
    env[_FLAG_ENV] = flag
    script = (
        "import asyncio, json\n"
        "from memtomem_stm import server\n"
        "tools = asyncio.run(server.mcp.list_tools())\n"
        "print(json.dumps([{'name': t.name, 'description': t.description or '', "
        "'schema': t.input_schema} for t in tools]))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


class TestAdvertisedBudget:
    """The point of the dispatcher is what an eager-loading client pays per request."""

    ADMIN_BUDGET_CHARS = 800
    HELP_BUDGET_CHARS = 4000

    def test_stm_admin_fits_its_budget_and_undercuts_the_tools_it_replaced(self):
        tools = {t["name"]: t for t in _advertised("true")}
        admin = tools["stm_admin"]
        cost = len(admin["description"]) + len(json.dumps(admin["schema"]))
        assert cost <= self.ADMIN_BUDGET_CHARS, cost
        assert set(admin["schema"]["properties"]) == {"action", "params"}
        for name in _OBSERVABILITY_ACTIONS:
            assert name in admin["description"], name

    async def test_help_fits_its_budget(self):
        assert len(await stm_admin("help")) <= self.HELP_BUDGET_CHARS
        for name in _OBSERVABILITY_ACTIONS:
            assert len(await stm_admin("help", {"action": name})) <= self.HELP_BUDGET_CHARS


# ── through a real MCP client ────────────────────────────────────────────


async def _call_through_client(
    arguments: dict, *, forbid_unknown: bool = True
) -> tuple[object, AsyncMock]:
    """Register ``stm_admin`` on a fresh server and call it as a client would.

    ``proxy_cache_clear`` is replaced by a spy, so the test observes whether the
    action ran and with what, without needing a lifespan context.
    """
    from memtomem_stm import server as server_module

    spy = AsyncMock(return_value="cleared")
    entry = server_module._OBSERVABILITY_ACTIONS["proxy_cache_clear"]
    patched = dict(server_module._OBSERVABILITY_ACTIONS)
    patched["proxy_cache_clear"] = server_module._ObsAction(fn=spy, meta=entry.meta)

    server = MCPServer("stm-admin-test")
    server.tool()(stm_admin)
    if forbid_unknown:
        assert server_module._forbid_unknown_admin_arguments(server) is True

    from unittest.mock import patch

    with patch.dict(server_module._OBSERVABILITY_ACTIONS, patched, clear=True):
        async with InMemoryTransport(server) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                listed = await session.list_tools()
                result = await session.call_tool("stm_admin", arguments)
    schema = next(t for t in listed.tools if t.name == "stm_admin").input_schema
    return (result, schema), spy


class TestThroughTheClient:
    async def test_misspelled_envelope_key_is_refused_and_clears_nothing(self):
        (result, schema), spy = await _call_through_client(
            {"action": "proxy_cache_clear", "param": {"server": "gh"}}
        )
        assert result.is_error
        assert "extra_forbidden" in result.content[0].text
        spy.assert_not_called()
        assert schema.get("additionalProperties") is False

    async def test_without_the_guard_the_same_call_runs_unfiltered(self):
        """Positive control: the SDK alone drops the key and runs the action."""
        (result, _schema), spy = await _call_through_client(
            {"action": "proxy_cache_clear", "param": {"server": "gh"}}, forbid_unknown=False
        )
        assert not result.is_error
        spy.assert_awaited_once()
        assert spy.await_args.kwargs["server"] is None

    async def test_invalid_parameter_sets_the_error_flag(self):
        (result, _schema), spy = await _call_through_client(
            {"action": "proxy_cache_clear", "params": {"server": ["gh"]}}
        )
        assert result.is_error
        assert "invalid parameter for action 'proxy_cache_clear'" in result.content[0].text
        assert "['gh']" not in result.content[0].text
        spy.assert_not_called()

    async def test_unknown_action_sets_the_error_flag(self):
        (result, _schema), _spy = await _call_through_client({"action": "proxy_statz"})
        assert result.is_error
        assert "unknown action 'proxy_statz'" in result.content[0].text

    async def test_a_valid_call_still_succeeds(self):
        (result, _schema), spy = await _call_through_client(
            {"action": "proxy_cache_clear", "params": {"server": "gh"}}
        )
        assert not result.is_error
        assert spy.await_args.kwargs["server"] == "gh"
