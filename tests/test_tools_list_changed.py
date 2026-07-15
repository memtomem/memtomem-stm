"""Tests for the ``tools/list_changed`` refresh path (#557).

The cache-eligibility gate reads tool annotations from the ``conn.tools``
snapshot, which used to be populated only at connect/reconnect. An upstream
that re-declares a tool from read-only to may-mutate at runtime (and emits
``notifications/tools/list_changed``) would keep replaying the pre-flip cached
response until the next error-driven reconnect. The proxy now subscribes to
the notification on every upstream session, re-lists the tools, and
invalidates the cache rows the change made unsafe.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import mcp.types as mcp_types
import pytest

from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.config import (
    CacheConfig,
    ProxyConfig,
    ToolOverrideConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore


def _ann(*, read_only=None, destructive=None):
    """A stand-in for MCP ``ToolAnnotations`` carrying only the two hints the gate
    reads (``getattr``-based, so a SimpleNamespace matches the real model)."""
    return SimpleNamespace(readOnlyHint=read_only, destructiveHint=destructive)


def _tool(name, ann=None):
    return SimpleNamespace(name=name, annotations=ann)


def _list_result(*tools):
    return SimpleNamespace(tools=list(tools))


def _build(
    tmp_path: Path,
    *,
    policy: str = "conservative",
    tool_overrides: dict | None = None,
    tools=(),
    with_cache: bool = True,
):
    store = MetricsStore(tmp_path / "m.db")
    store.initialize()
    server_cfg = UpstreamServerConfig(
        prefix="t",
        max_retries=0,
        reconnect_delay_seconds=0.0,
        tool_overrides=tool_overrides or {},
    )
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": server_cfg},
        cache=CacheConfig(tool_annotation_policy=policy, default_ttl_seconds=3600.0),
    )
    mgr = ProxyManager(proxy_cfg, TokenTracker(metrics_store=store))
    mgr._connections["srv"] = UpstreamConnection(
        name="srv", config=server_cfg, session=AsyncMock(), tools=list(tools)
    )
    cache: ProxyCache | None = None
    if with_cache:
        cache = ProxyCache(tmp_path / "c.db", max_entries=100)
        cache.initialize()
        mgr._cache = cache
    return mgr, store, cache


@pytest.fixture
def build(tmp_path):
    opened: list[tuple[MetricsStore, ProxyCache | None]] = []

    def _factory(**kwargs):
        mgr, store, cache = _build(tmp_path, **kwargs)
        opened.append((store, cache))
        return mgr, store, cache

    yield _factory
    for store, cache in opened:
        try:
            store.close()
        except Exception:
            pass
        if cache is not None:
            try:
                cache.close()
            except Exception:
                pass


def _eligible(mgr, tool):
    return mgr._tool_cache_eligible("srv", tool, cfg_snap=mgr._config)


# ── Refresh: conn.tools reassignment + cache invalidation ────────────────


@pytest.mark.asyncio
class TestRefreshServerTools:
    async def test_refresh_reassigns_conn_tools(self, build):
        mgr, _, _ = build(tools=[_tool("old")])
        conn = mgr._connections["srv"]
        conn.session.list_tools.return_value = _list_result(_tool("new"))

        await mgr._refresh_server_tools("srv")

        assert [t.name for t in conn.tools] == ["new"]

    async def test_flip_to_writer_invalidates_only_that_tool(self, build):
        """The exact #557 scenario: a tool advertised read-only at connect flips
        to may-mutate at runtime. Its cached row must be deleted (not merely
        gated), while an unaffected sibling tool's row survives."""
        mgr, _, cache = build(
            tools=[_tool("search", _ann(read_only=True)), _tool("other", _ann(read_only=True))]
        )
        cache.set("srv", "search", {"q": 1}, "pre-flip", 3600.0)
        cache.set("srv", "other", {"q": 1}, "untouched", 3600.0)
        conn = mgr._connections["srv"]
        conn.session.list_tools.return_value = _list_result(
            _tool("search", _ann(read_only=False)), _tool("other", _ann(read_only=True))
        )

        await mgr._refresh_server_tools("srv")

        assert cache.get("srv", "search", {"q": 1}) is None
        assert cache.get("srv", "other", {"q": 1}) == "untouched"
        assert not _eligible(mgr, "search")  # gate refuses new lookups too
        assert _eligible(mgr, "other")

    async def test_destructive_flip_invalidates(self, build):
        mgr, _, cache = build(tools=[_tool("apply", _ann(read_only=True))])
        cache.set("srv", "apply", {}, "pre-flip", 3600.0)
        conn = mgr._connections["srv"]
        conn.session.list_tools.return_value = _list_result(
            _tool("apply", _ann(read_only=True, destructive=True))
        )

        await mgr._refresh_server_tools("srv")

        assert cache.get("srv", "apply", {}) is None

    async def test_override_true_suppresses_invalidation(self, build):
        """A per-tool ``cache: true`` override is the operator's explicit
        escape hatch; the verdict never flips, so the row must survive."""
        mgr, _, cache = build(
            tools=[_tool("search", _ann(read_only=True))],
            tool_overrides={"search": ToolOverrideConfig(cache=True)},
        )
        cache.set("srv", "search", {"q": 1}, "kept", 3600.0)
        conn = mgr._connections["srv"]
        conn.session.list_tools.return_value = _list_result(_tool("search", _ann(read_only=False)))

        await mgr._refresh_server_tools("srv")

        assert cache.get("srv", "search", {"q": 1}) == "kept"
        assert _eligible(mgr, "search")

    async def test_removed_tool_rows_cleared(self, build):
        """A tool dropped from the advertised list can only ever serve stale
        content (the upstream no longer answers it), so its rows go too."""
        mgr, _, cache = build(tools=[_tool("gone", _ann(read_only=True)), _tool("kept")])
        cache.set("srv", "gone", {}, "dead", 3600.0)
        cache.set("srv", "kept", {}, "alive", 3600.0)
        conn = mgr._connections["srv"]
        conn.session.list_tools.return_value = _list_result(_tool("kept"))

        await mgr._refresh_server_tools("srv")

        assert cache.get("srv", "gone", {}) is None
        assert cache.get("srv", "kept", {}) == "alive"

    async def test_flip_toward_read_only_keeps_rows(self, build):
        """Eligibility moving ineligible→eligible needs no invalidation."""
        mgr, _, cache = build(tools=[_tool("t", _ann(read_only=False)), _tool("r")])
        cache.set("srv", "r", {}, "alive", 3600.0)
        conn = mgr._connections["srv"]
        conn.session.list_tools.return_value = _list_result(
            _tool("t", _ann(read_only=True)), _tool("r")
        )

        await mgr._refresh_server_tools("srv")

        assert cache.get("srv", "r", {}) == "alive"
        assert _eligible(mgr, "t")

    async def test_refresh_without_cache_is_safe(self, build):
        mgr, _, _ = build(tools=[_tool("t", _ann(read_only=True))], with_cache=False)
        conn = mgr._connections["srv"]
        conn.session.list_tools.return_value = _list_result(_tool("t", _ann(read_only=False)))

        await mgr._refresh_server_tools("srv")

        assert not _eligible(mgr, "t")

    async def test_unknown_server_is_noop(self, build):
        mgr, _, _ = build()
        await mgr._refresh_server_tools("ghost")  # must not raise

    async def test_stale_session_snapshot_not_applied(self, build):
        """If a reconnect swaps ``conn.session`` while the refresh's
        ``list_tools`` is in flight, the (possibly older) snapshot must not
        clobber the state the reconnect just re-listed."""
        mgr, _, _ = build(tools=[_tool("fresh-from-reconnect")])
        conn = mgr._connections["srv"]

        async def _list_tools_then_reconnect():
            conn.session = AsyncMock()  # reconnect replaced the session
            return _list_result(_tool("stale"))

        conn.session.list_tools.side_effect = _list_tools_then_reconnect

        await mgr._refresh_server_tools("srv")

        assert [t.name for t in conn.tools] == ["fresh-from-reconnect"]


# ── Notification handler + scheduling ────────────────────────────────────


@pytest.mark.asyncio
class TestHandlerAndScheduling:
    async def test_handler_schedules_refresh_on_tool_list_changed(self, build):
        mgr, _, _ = build(tools=[_tool("old")])
        conn = mgr._connections["srv"]
        conn.session.list_tools.return_value = _list_result(_tool("new"))
        handler = mgr._make_message_handler("srv")

        await handler(mcp_types.ServerNotification(mcp_types.ToolListChangedNotification()))
        await asyncio.gather(*mgr._background_tasks)

        assert [t.name for t in conn.tools] == ["new"]
        assert "srv" not in mgr._tools_refresh_running

    async def test_handler_ignores_other_messages(self, build):
        mgr, _, _ = build(tools=[_tool("old")])
        conn = mgr._connections["srv"]
        handler = mgr._make_message_handler("srv")

        await handler(mcp_types.ServerNotification(mcp_types.PromptListChangedNotification()))
        await handler(RuntimeError("stream error"))

        assert not mgr._background_tasks
        conn.session.list_tools.assert_not_awaited()

    async def test_notification_burst_coalesces_to_one_refresh(self, build):
        mgr, _, _ = build(tools=[_tool("old")])
        conn = mgr._connections["srv"]
        conn.session.list_tools.return_value = _list_result(_tool("new"))

        mgr._schedule_tools_refresh("srv")
        mgr._schedule_tools_refresh("srv")
        mgr._schedule_tools_refresh("srv")

        assert len(mgr._background_tasks) == 1
        await asyncio.gather(*mgr._background_tasks)
        assert conn.session.list_tools.await_count == 1

    async def test_notification_during_refresh_triggers_second_pass(self, build):
        """A change arriving while ``list_tools`` is already in flight could be
        missed by that in-flight snapshot — the drain loop must re-list."""
        mgr, _, _ = build(tools=[_tool("old")])
        conn = mgr._connections["srv"]
        release = asyncio.Event()
        started = asyncio.Event()

        async def _blocking_list_tools():
            started.set()
            await release.wait()
            return _list_result(_tool("new"))

        conn.session.list_tools.side_effect = _blocking_list_tools

        mgr._schedule_tools_refresh("srv")
        await started.wait()  # first refresh is mid-list_tools
        mgr._schedule_tools_refresh("srv")
        assert len(mgr._background_tasks) == 1  # no second task, just dirty
        release.set()
        await asyncio.gather(*mgr._background_tasks)

        assert conn.session.list_tools.await_count == 2

    async def test_failed_refresh_clears_running_state(self, build):
        mgr, _, _ = build(tools=[_tool("old")])
        conn = mgr._connections["srv"]
        conn.session.list_tools.side_effect = RuntimeError("torn down")

        mgr._schedule_tools_refresh("srv")
        await asyncio.gather(*mgr._background_tasks, return_exceptions=True)

        assert "srv" not in mgr._tools_refresh_running
        # a later notification can schedule a fresh attempt
        conn.session.list_tools.side_effect = None
        conn.session.list_tools.return_value = _list_result(_tool("new"))
        mgr._schedule_tools_refresh("srv")
        await asyncio.gather(*mgr._background_tasks)
        assert [t.name for t in conn.tools] == ["new"]

    async def test_stop_resets_refresh_bookkeeping(self, build):
        """A drain task cancelled before its first step never enters its
        ``finally`` (the coroutine body never runs), so ``running`` would keep
        the server name — and a stop→start reuse of the manager would then
        silently drop every later ``list_changed`` notification for that
        server. ``stop()`` must reset the bookkeeping."""
        mgr, _, _ = build(tools=[_tool("old")])
        conn = mgr._connections["srv"]
        conn.session.list_tools.return_value = _list_result(_tool("new"))

        mgr._schedule_tools_refresh("srv")  # task created, never given a tick
        await mgr.stop()

        conn.session.list_tools.assert_not_awaited()  # premise: body never ran
        assert not mgr._tools_refresh_running
        assert not mgr._tools_refresh_dirty

        # restart-style reuse: a fresh notification must schedule again
        mgr._connections["srv"] = conn
        mgr._schedule_tools_refresh("srv")
        assert len(mgr._background_tasks) == 1
        await asyncio.gather(*mgr._background_tasks)
        assert [t.name for t in conn.tools] == ["new"]


# ── Wiring pin ────────────────────────────────────────────────────────────


class TestSessionWiring:
    def test_both_connect_paths_pass_message_handler(self):
        """``_connect_server`` and ``_reconnect_server`` must both build their
        ``ClientSession`` with the message handler; a unit spy can't tell the
        two call sites apart, so pin the wiring at the source level (the
        reconnect path silently dropping the handler would disable refresh
        exactly for long-lived flaky servers — the ones that need it most).
        Both paths now delegate session construction to the shared
        ``_establish_connection`` helper, so pin (a) that each still routes
        through the helper and (b) that the helper wires the handler."""
        for method in (ProxyManager._connect_server, ProxyManager._reconnect_server):
            src = inspect.getsource(method)
            assert "self._establish_connection(name, cfg)" in src, method.__name__
        owner_src = inspect.getsource(ProxyManager._run_connection_owner)
        assert "message_handler=self._make_message_handler(name)" in owner_src
