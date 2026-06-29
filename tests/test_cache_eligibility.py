"""Tests for the response-cache eligibility gate.

The proxy sits transparently in front of every upstream tool, and the response
cache is on by default. Without a gate, a mutating tool called twice with
identical args within the TTL is served the first call's cached success without
re-executing the side effect. ``ProxyManager._tool_cache_eligible`` gates both
the lookup (``_call_tool_guarded``) and the store (``_store_cache``) on the
tool's MCP annotations (``readOnlyHint`` / ``destructiveHint``) plus per-tool /
per-server ``cache`` overrides.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

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


def _text_result(text="ok"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], isError=False)


def _build(
    tmp_path: Path,
    *,
    policy: str = "conservative",
    server_cache: bool | None = None,
    server_cache_ttl: float | None = None,
    global_ttl: float | None = 3600.0,
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
        cache=server_cache,
        cache_ttl_seconds=server_cache_ttl,
        tool_overrides=tool_overrides or {},
    )
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": server_cfg},
        cache=CacheConfig(tool_annotation_policy=policy, default_ttl_seconds=global_ttl),
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


def _resolve_ttl(mgr, tool):
    return mgr._resolve_cache_ttl("srv", tool, cfg_snap=mgr._config)


# ── Unit: annotation policy ──────────────────────────────────────────────


class TestConservativePolicy:
    """Default policy: cache everything EXCEPT self-declared writers."""

    def test_unannotated_tool_is_eligible(self, build):
        mgr, _, _ = build(tools=[_tool("t")])
        assert _eligible(mgr, "t") is True

    def test_unknown_tool_is_eligible(self, build):
        # No matching tool record at all → no writer signal → cached as before.
        mgr, _, _ = build(tools=[])
        assert _eligible(mgr, "whatever") is True

    def test_explicit_read_only_is_eligible(self, build):
        mgr, _, _ = build(tools=[_tool("t", _ann(read_only=True))])
        assert _eligible(mgr, "t") is True

    def test_read_only_false_is_not_eligible(self, build):
        mgr, _, _ = build(tools=[_tool("t", _ann(read_only=False))])
        assert _eligible(mgr, "t") is False

    def test_destructive_is_not_eligible(self, build):
        mgr, _, _ = build(tools=[_tool("t", _ann(destructive=True))])
        assert _eligible(mgr, "t") is False

    def test_destructive_wins_over_read_only_claim(self, build):
        # Contradictory annotations → treat as a writer (safe default).
        mgr, _, _ = build(tools=[_tool("t", _ann(read_only=True, destructive=True))])
        assert _eligible(mgr, "t") is False


class TestStrictPolicy:
    """Cache ONLY explicit read-only tools; a missing hint defaults to may-mutate."""

    def test_read_only_true_is_eligible(self, build):
        mgr, _, _ = build(policy="strict", tools=[_tool("t", _ann(read_only=True))])
        assert _eligible(mgr, "t") is True

    def test_unannotated_is_not_eligible(self, build):
        mgr, _, _ = build(policy="strict", tools=[_tool("t")])
        assert _eligible(mgr, "t") is False

    def test_read_only_false_is_not_eligible(self, build):
        mgr, _, _ = build(policy="strict", tools=[_tool("t", _ann(read_only=False))])
        assert _eligible(mgr, "t") is False


class TestIgnorePolicy:
    """Pre-gate behavior: every tool eligible regardless of annotations."""

    def test_writer_is_eligible_under_ignore(self, build):
        mgr, _, _ = build(
            policy="ignore", tools=[_tool("t", _ann(read_only=False, destructive=True))]
        )
        assert _eligible(mgr, "t") is True


class TestOverridePrecedence:
    def test_tool_override_false_beats_read_only_annotation(self, build):
        mgr, _, _ = build(
            tools=[_tool("t", _ann(read_only=True))],
            tool_overrides={"t": ToolOverrideConfig(cache=False)},
        )
        assert _eligible(mgr, "t") is False

    def test_tool_override_true_beats_writer_annotation(self, build):
        mgr, _, _ = build(
            tools=[_tool("t", _ann(read_only=False))],
            tool_overrides={"t": ToolOverrideConfig(cache=True)},
        )
        assert _eligible(mgr, "t") is True

    def test_server_override_false_applies_when_no_tool_override(self, build):
        mgr, _, _ = build(server_cache=False, tools=[_tool("t", _ann(read_only=True))])
        assert _eligible(mgr, "t") is False

    def test_tool_override_wins_over_server_override(self, build):
        mgr, _, _ = build(
            server_cache=False,
            tools=[_tool("t")],
            tool_overrides={"t": ToolOverrideConfig(cache=True)},
        )
        assert _eligible(mgr, "t") is True


class TestUnknownServer:
    def test_unknown_server_is_eligible(self, build):
        # Direct dispatch / tests with no registered connection → preserve
        # pre-gate behavior rather than refusing the cache.
        mgr, _, _ = build(tools=[])
        assert mgr._tool_cache_eligible("nope", "t", cfg_snap=mgr._config) is True


# ── Integration: the side effect actually re-executes ────────────────────


@pytest.mark.asyncio
class TestCallToolHonoursEligibility:
    async def test_writer_is_force_forwarded_each_call(self, build):
        """A readOnlyHint=False tool called twice with identical args must hit the
        upstream BOTH times (side effect re-executes) and never be cached."""
        mgr, _, cache = build(tools=[_tool("writer", _ann(read_only=False))])
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("done")

        await mgr.call_tool("srv", "writer", {"x": 1})
        await mgr.call_tool("srv", "writer", {"x": 1})

        assert session.call_tool.await_count == 2  # not served from cache
        assert cache.get("srv", "writer", {"x": 1}) is None  # not stored

    async def test_read_only_tool_is_served_from_cache(self, build):
        """A readOnlyHint=True tool hits the upstream once; the identical repeat is
        served from cache (regression guard: the gate must not over-block)."""
        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("payload")

        await mgr.call_tool("srv", "reader", {"q": "a"})
        await mgr.call_tool("srv", "reader", {"q": "a"})

        assert session.call_tool.await_count == 1  # second served from cache
        assert cache.get("srv", "reader", {"q": "a"}) is not None

    async def test_unannotated_tool_still_cached_default(self, build):
        """Behavior preservation: the un-annotated majority is cached as before
        under the default conservative policy."""
        mgr, _, cache = build(tools=[_tool("plain")])
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("payload")

        await mgr.call_tool("srv", "plain", {})
        await mgr.call_tool("srv", "plain", {})

        assert session.call_tool.await_count == 1

    async def test_override_false_force_forwards_unannotated(self, build):
        mgr, _, cache = build(
            tools=[_tool("vol")],
            tool_overrides={"vol": ToolOverrideConfig(cache=False)},
        )
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("now")

        await mgr.call_tool("srv", "vol", {})
        await mgr.call_tool("srv", "vol", {})

        assert session.call_tool.await_count == 2
        assert cache.get("srv", "vol", {}) is None


@pytest.mark.asyncio
class TestMissAccountingHonoursEligibility:
    """A cache MISS is recorded only after an ELIGIBLE lookup actually misses.
    An ineligible (force-forwarded) tool attempts no lookup, so it must not be
    counted as a miss — otherwise it skews the hit-rate diagnostic."""

    async def test_ineligible_writer_records_no_miss(self, build):
        mgr, _, _ = build(tools=[_tool("writer", _ann(read_only=False))])
        mgr._connections["srv"].session.call_tool.return_value = _text_result("done")

        await mgr.call_tool("srv", "writer", {"x": 1})
        await mgr.call_tool("srv", "writer", {"x": 1})

        assert mgr.tracker.get_summary()["cache_misses"] == 0

    async def test_eligible_miss_records_one_miss_then_hit(self, build):
        mgr, _, _ = build(tools=[_tool("reader", _ann(read_only=True))])
        mgr._connections["srv"].session.call_tool.return_value = _text_result("payload")

        await mgr.call_tool("srv", "reader", {"q": "a"})  # eligible miss → 1
        await mgr.call_tool("srv", "reader", {"q": "a"})  # served from cache → no miss

        summary = mgr.tracker.get_summary()
        assert summary["cache_misses"] == 1
        assert summary["cache_hits"] == 1


@pytest.mark.asyncio
class TestTtlZeroDisablesServing:
    """Lowering cache.default_ttl_seconds to 0 must stop serving rows cached under
    a prior positive TTL — per-row TTL is frozen at write time, so the lookup path
    itself must bypass the cache when the configured TTL is non-positive."""

    async def test_ttl_lowered_to_zero_stops_serving_and_invalidates(self, build):
        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("payload")

        await mgr.call_tool("srv", "reader", {"q": "a"})  # cached under default TTL
        assert session.call_tool.await_count == 1
        assert cache.get("srv", "reader", {"q": "a"}) is not None

        # Operator lowers the cache TTL to 0 (hot-reload). The previously-cached
        # live row must no longer be served, and the next call must hit upstream.
        mgr._config.cache.default_ttl_seconds = 0

        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert session.call_tool.await_count == 2  # not served from the stale row
        assert cache.get("srv", "reader", {"q": "a"}) is None  # old row invalidated


# ── Unit: per-tool / per-server TTL override resolution ──────────────────


class TestTtlOverridePrecedence:
    """``_resolve_cache_ttl`` mirrors ``_tool_cache_eligible``'s precedence:
    per-tool > per-server > global. ``None`` at the tool/server level means
    *inherit the next level*, NOT *never expires* (only the global default's
    ``None`` means never-expires)."""

    def test_global_default_when_no_override(self, build):
        mgr, _, _ = build(global_ttl=1800.0, tools=[_tool("t")])
        assert _resolve_ttl(mgr, "t") == 1800.0

    def test_server_override_beats_global(self, build):
        mgr, _, _ = build(global_ttl=1800.0, server_cache_ttl=600.0, tools=[_tool("t")])
        assert _resolve_ttl(mgr, "t") == 600.0

    def test_tool_override_beats_server_and_global(self, build):
        mgr, _, _ = build(
            global_ttl=1800.0,
            server_cache_ttl=600.0,
            tools=[_tool("t")],
            tool_overrides={"t": ToolOverrideConfig(cache_ttl_seconds=42.0)},
        )
        assert _resolve_ttl(mgr, "t") == 42.0

    def test_none_at_tool_level_inherits_server(self, build):
        mgr, _, _ = build(
            global_ttl=1800.0,
            server_cache_ttl=600.0,
            tools=[_tool("t")],
            tool_overrides={"t": ToolOverrideConfig(cache_ttl_seconds=None)},
        )
        assert _resolve_ttl(mgr, "t") == 600.0  # None = inherit, not never-expires

    def test_none_at_both_levels_inherits_global(self, build):
        mgr, _, _ = build(global_ttl=1800.0, tools=[_tool("t")])
        assert _resolve_ttl(mgr, "t") == 1800.0

    def test_unknown_server_returns_global(self, build):
        mgr, _, _ = build(global_ttl=1800.0)
        assert mgr._resolve_cache_ttl("nope", "t", cfg_snap=mgr._config) == 1800.0

    def test_zero_override_is_a_real_value_not_inherit(self, build):
        # 0 (disable) must be distinct from None (inherit): a tool override of 0
        # must NOT fall through to the 1800s server/global value.
        mgr, _, _ = build(
            global_ttl=1800.0,
            server_cache_ttl=600.0,
            tools=[_tool("t")],
            tool_overrides={"t": ToolOverrideConfig(cache_ttl_seconds=0)},
        )
        assert _resolve_ttl(mgr, "t") == 0

    def test_global_none_never_expires_passes_through(self, build):
        # The global never-expires sentinel survives resolution when nothing
        # overrides it.
        mgr, _, _ = build(global_ttl=None, tools=[_tool("t")])
        assert _resolve_ttl(mgr, "t") is None


class TestCacheTtlConfigConstraint:
    def test_negative_tool_ttl_rejected(self):
        with pytest.raises(ValidationError):
            ToolOverrideConfig(cache_ttl_seconds=-1)

    def test_negative_server_ttl_rejected(self):
        # prefix is supplied so the ONLY validation error is the negative ttl.
        with pytest.raises(ValidationError):
            UpstreamServerConfig(prefix="t", cache_ttl_seconds=-1)

    def test_zero_and_none_allowed(self):
        assert ToolOverrideConfig(cache_ttl_seconds=0).cache_ttl_seconds == 0
        assert UpstreamServerConfig(prefix="t", cache_ttl_seconds=0).cache_ttl_seconds == 0
        assert ToolOverrideConfig().cache_ttl_seconds is None  # default = inherit
        assert UpstreamServerConfig(prefix="t").cache_ttl_seconds is None


# ── Integration: resolved TTL threads into store + gates serving ─────────


@pytest.mark.asyncio
class TestPerToolTtlOverrideBehavior:
    async def test_positive_per_tool_ttl_threaded_into_store(self, build):
        """A positive per-tool ``cache_ttl_seconds`` is the ttl the entry is stored
        with — proves ``_store_cache`` uses the resolved value, not the global."""
        mgr, _, cache = build(
            tools=[_tool("reader", _ann(read_only=True))],
            tool_overrides={"reader": ToolOverrideConfig(cache_ttl_seconds=120.0)},
        )
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("payload")

        with patch.object(cache, "set", wraps=cache.set) as set_spy:
            await mgr.call_tool("srv", "reader", {"q": "a"})

        set_spy.assert_called_once()
        assert set_spy.call_args.kwargs["ttl_seconds"] == 120.0

    async def test_per_tool_ttl_zero_disables_only_that_tool(self, build):
        """A per-tool ``cache_ttl_seconds`` of 0 bypasses the lookup and skips the
        store for THAT tool (both calls hit upstream, nothing cached), while a
        sibling tool on the SAME server still caches under the global TTL.

        Covers the text-response path, where the store-side ``set(ttl<=0)``
        invalidates any prior live row. The lookup bypass (never-served) holds for
        every response shape; the on-disk invalidation of a non-text response under
        ttl<=0 is a pre-existing #536 gap tracked as a separate follow-up."""
        mgr, _, cache = build(
            tools=[_tool("vol"), _tool("plain")],
            tool_overrides={"vol": ToolOverrideConfig(cache_ttl_seconds=0)},
        )
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("payload")

        # vol: ttl=0 → never served, never stored.
        await mgr.call_tool("srv", "vol", {})
        await mgr.call_tool("srv", "vol", {})
        assert session.call_tool.await_count == 2
        assert cache.get("srv", "vol", {}) is None

        # plain on the same server: default global TTL → second call served.
        await mgr.call_tool("srv", "plain", {"q": 1})
        assert session.call_tool.await_count == 3  # first plain hits upstream
        await mgr.call_tool("srv", "plain", {"q": 1})
        assert session.call_tool.await_count == 3  # second served from cache
        assert cache.get("srv", "plain", {"q": 1}) is not None
