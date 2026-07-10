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


def _image_content():
    # MCP ImageContent stand-in: ``_shape_response`` treats any content whose
    # ``type`` is not "text" as non-text.
    return SimpleNamespace(type="image", data="aGk=", mimeType="image/png")


def _nontext_result():
    return SimpleNamespace(content=[_image_content()], isError=False)


def _mixed_result(text="payload"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text), _image_content()],
        isError=False,
    )


def _error_result(text="boom"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], isError=True)


def _mixed_error_result(text="boom"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text), _image_content()],
        isError=True,
    )


def _empty_result():
    return SimpleNamespace(content=[], isError=False)


def _fp(mgr, tool, server="srv"):
    """The fingerprint the manager keys rows under for ``(server, tool)``.

    Direct ``ProxyCache.get``/``set`` calls that interact with manager-stored
    rows must pass it — the manager never uses the default ``""`` for a
    registered server."""
    return mgr._cache_key_fingerprint(server, tool, cfg_snap=mgr._config)


def _get(mgr, cache, tool, args, server="srv"):
    """Fingerprint-aware ``cache.get`` for rows the manager stored (no
    ``_context_query`` in these tests, so only the fingerprint is needed)."""
    return cache.get(server, tool, args, config_fingerprint=_fp(mgr, tool, server))


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
        # Contradictory annotations → treat as a writer (safe default). This is
        # a DELIBERATE deviation from the spec-literal reading (destructiveHint
        # is only meaningful when readOnlyHint == false, under which the
        # read-only claim would win): see the conservative-branch NOTE in
        # ``_tool_cache_eligible``. Per-tool ``cache: true`` is the escape
        # hatch, pinned by test_tool_override_true_beats_contradictory_annotations.
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

    def test_tool_override_true_beats_contradictory_annotations(self, build):
        # Pins the escape hatch promised for the contradictory pair
        # (readOnlyHint=True + destructiveHint=True → writer; see the NOTE in
        # ``_tool_cache_eligible`` and docs/caching.md): a per-tool
        # ``cache: true`` must stay ahead of the annotation policy even for
        # this combination, or the documented opt-back-in silently breaks.
        mgr, _, _ = build(
            tools=[_tool("t", _ann(read_only=True, destructive=True))],
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
        assert _get(mgr, cache, "writer", {"x": 1}) is None  # not stored

    async def test_read_only_tool_is_served_from_cache(self, build):
        """A readOnlyHint=True tool hits the upstream once; the identical repeat is
        served from cache (regression guard: the gate must not over-block)."""
        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("payload")

        await mgr.call_tool("srv", "reader", {"q": "a"})
        await mgr.call_tool("srv", "reader", {"q": "a"})

        assert session.call_tool.await_count == 1  # second served from cache
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None

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
        assert _get(mgr, cache, "vol", {}) is None


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
class TestUnstorableAccounting:
    """#558: a recorded miss whose response the cache refuses to store is counted
    as ``cache_unstorable``, so a tool that can never register a hit (mixed /
    non-text / empty responses) is diagnosable instead of presenting as an
    ever-growing 0%-hit-rate miss pile. The counter mirrors the lookup gate:
    no recorded miss → no unstorable count."""

    async def test_mixed_response_counts_unstorable_every_call(self, build):
        mgr, _, cache = build(tools=[_tool("mixed", _ann(read_only=True))])
        mgr._connections["srv"].session.call_tool.return_value = _mixed_result()

        await mgr.call_tool("srv", "mixed", {"q": "a"})
        await mgr.call_tool("srv", "mixed", {"q": "a"})  # never stored → re-miss

        summary = mgr.tracker.get_summary()
        assert summary["cache_misses"] == 2
        assert summary["cache_unstorable"] == 2
        assert summary["cache_hits"] == 0
        assert _get(mgr, cache, "mixed", {"q": "a"}) is None

    async def test_nontext_only_response_counts_unstorable(self, build):
        mgr, _, _ = build(tools=[_tool("img", _ann(read_only=True))])
        mgr._connections["srv"].session.call_tool.return_value = _nontext_result()

        await mgr.call_tool("srv", "img", {})

        summary = mgr.tracker.get_summary()
        assert summary["cache_misses"] == 1
        assert summary["cache_unstorable"] == 1

    async def test_empty_response_counts_unstorable_and_reconciles(self, build):
        """An empty response records the 0/0 call metric too (#558 codex round
        1): without it the recorded miss/unstorable would have no matching call
        and ``total_invocations`` would understate actual invocations."""
        mgr, _, _ = build(tools=[_tool("empty", _ann(read_only=True))])
        mgr._connections["srv"].session.call_tool.return_value = _empty_result()

        await mgr.call_tool("srv", "empty", {})

        summary = mgr.tracker.get_summary()
        assert summary["cache_misses"] == 1
        assert summary["cache_unstorable"] == 1
        assert summary["total_calls"] == 1
        assert summary["total_invocations"] == 1

    async def test_ineligible_writer_mixed_response_not_counted(self, build):
        """No lookup was attempted (force-forwarded writer), so neither a miss
        nor an unstorable count may move — they must stay reconciled."""
        mgr, _, _ = build(tools=[_tool("writer", _ann(read_only=False))])
        mgr._connections["srv"].session.call_tool.return_value = _mixed_result()

        await mgr.call_tool("srv", "writer", {})

        summary = mgr.tracker.get_summary()
        assert summary["cache_misses"] == 0
        assert summary["cache_unstorable"] == 0

    async def test_ttl_zero_mixed_response_not_counted(self, build):
        """Caching disabled (resolved ttl<=0) bypasses the lookup entirely, so
        the store refusal must not be counted as an unstorable miss."""
        mgr, _, _ = build(
            tools=[_tool("mixed", _ann(read_only=True))],
            tool_overrides={"mixed": ToolOverrideConfig(cache_ttl_seconds=0.0)},
        )
        mgr._connections["srv"].session.call_tool.return_value = _mixed_result()

        await mgr.call_tool("srv", "mixed", {})

        summary = mgr.tracker.get_summary()
        assert summary["cache_misses"] == 0
        assert summary["cache_unstorable"] == 0

    async def test_storable_response_not_counted(self, build):
        mgr, _, _ = build(tools=[_tool("reader", _ann(read_only=True))])
        mgr._connections["srv"].session.call_tool.return_value = _text_result("payload")

        await mgr.call_tool("srv", "reader", {})

        summary = mgr.tracker.get_summary()
        assert summary["cache_misses"] == 1
        assert summary["cache_unstorable"] == 0

    async def test_hit_records_served_chars(self, build):
        """A cache hit records the size of the served (stored) text so the
        cache's benefit is visible next to the compression savings (#558)."""
        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        mgr._connections["srv"].session.call_tool.return_value = _text_result("payload")

        await mgr.call_tool("srv", "reader", {"q": "a"})  # miss → stored
        await mgr.call_tool("srv", "reader", {"q": "a"})  # hit

        stored = _get(mgr, cache, "reader", {"q": "a"})
        assert stored is not None
        summary = mgr.tracker.get_summary()
        assert summary["cache_hits"] == 1
        assert summary["cache_hit_chars"] == len(stored)
        assert summary["total_invocations"] == summary["total_calls"] + 1


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
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None

        # Operator lowers the cache TTL to 0 (hot-reload). The previously-cached
        # live row must no longer be served, and the next call must hit upstream.
        mgr._config.cache.default_ttl_seconds = 0

        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert session.call_tool.await_count == 2  # not served from the stale row
        assert _get(mgr, cache, "reader", {"q": "a"}) is None  # old row invalidated


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
        ttl<=0 is covered by ``TestTtlZeroNonTextInvalidation`` (#541)."""
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
        assert _get(mgr, cache, "vol", {}) is None

        # plain on the same server: default global TTL → second call served.
        await mgr.call_tool("srv", "plain", {"q": 1})
        assert session.call_tool.await_count == 3  # first plain hits upstream
        await mgr.call_tool("srv", "plain", {"q": 1})
        assert session.call_tool.await_count == 3  # second served from cache
        assert _get(mgr, cache, "plain", {"q": 1}) is not None


# ── Integration: ttl<=0 invalidation of non-text / mixed responses (#541) ─


@pytest.mark.asyncio
class TestTtlZeroNonTextInvalidation:
    """#541: a non-text / mixed response under a disabled (``ttl<=0``) cache must
    invalidate a row left behind by an EARLIER text response for the same key.

    The store-side ``ttl<=0`` self-heal in ``ProxyCache.set`` only runs on the
    TEXT store path (the store is gated text-only), so a tool whose response shape
    flips text→non-text across a TTL down-then-up window would otherwise resurface
    the stale text row once the TTL is raised back within its frozen window."""

    async def test_invalidate_helper_noop_under_positive_ttl(self, build):
        # Direct helper unit: a positive resolved TTL must leave the row intact
        # (invalidation is gated on ttl<=0, so no spurious deletes).
        mgr, _, cache = build(global_ttl=3600.0, tools=[_tool("reader", _ann(read_only=True))])
        cache.set(
            "srv",
            "reader",
            {"q": "a"},
            "payload",
            ttl_seconds=3600.0,
            config_fingerprint=_fp(mgr, "reader"),
        )
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None

        mgr._invalidate_disabled_cache("srv", "reader", {"q": "a"}, cfg_snap=mgr._config)
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None

    async def test_invalidate_helper_deletes_under_zero_ttl(self, build):
        mgr, _, cache = build(global_ttl=3600.0, tools=[_tool("reader", _ann(read_only=True))])
        cache.set(
            "srv",
            "reader",
            {"q": "a"},
            "payload",
            ttl_seconds=3600.0,
            config_fingerprint=_fp(mgr, "reader"),
        )
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None

        mgr._config.cache.default_ttl_seconds = 0
        mgr._invalidate_disabled_cache("srv", "reader", {"q": "a"}, cfg_snap=mgr._config)
        assert _get(mgr, cache, "reader", {"q": "a"}) is None

    async def test_nontext_only_response_invalidates_prior_text_row(self, build):
        # Site A: the non-text-ONLY early return in ``_call_tool_inner``.
        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session

        session.call_tool.return_value = _text_result("payload")
        await mgr.call_tool("srv", "reader", {"q": "a"})  # text cached under default TTL
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None

        # Caching disabled, and the SAME key now returns a non-text response.
        mgr._config.cache.default_ttl_seconds = 0
        session.call_tool.return_value = _nontext_result()
        await mgr.call_tool("srv", "reader", {"q": "a"})

        assert _get(mgr, cache, "reader", {"q": "a"}) is None  # stale text row gone

    async def test_mixed_response_invalidates_prior_text_row(self, build):
        # Site B: the mixed (text+non-text) branch in ``_store_cache``.
        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session

        session.call_tool.return_value = _text_result("payload")
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None

        mgr._config.cache.default_ttl_seconds = 0
        session.call_tool.return_value = _mixed_result("payload")
        result = await mgr.call_tool("srv", "reader", {"q": "a"})

        assert _get(mgr, cache, "reader", {"q": "a"}) is None  # stale text row gone
        # mixed response still returns text + the preserved non-text content.
        assert isinstance(result, list)

    async def test_per_tool_ttl_zero_nontext_invalidates(self, build):
        # The per-tool ``cache_ttl_seconds: 0`` path (#540) — the headline
        # disable-caching-for-a-volatile-tool use case — with a text→non-text flip.
        mgr, _, cache = build(tools=[_tool("vol", _ann(read_only=True))])
        session = mgr._connections["srv"].session

        # Cache a text row under the global positive TTL (no override yet).
        session.call_tool.return_value = _text_result("payload")
        await mgr.call_tool("srv", "vol", {})
        assert _get(mgr, cache, "vol", {}) is not None

        # Disable caching for THIS tool only, then flip to a non-text response.
        mgr._connections["srv"].config.tool_overrides["vol"] = ToolOverrideConfig(
            cache_ttl_seconds=0
        )
        session.call_tool.return_value = _nontext_result()
        await mgr.call_tool("srv", "vol", {})
        assert _get(mgr, cache, "vol", {}) is None

    async def test_lower_to_zero_then_raise_does_not_resurface_stale_row(self, build):
        # Full #541 sequence: cache under positive TTL → lower to 0 → a non-text
        # call invalidates → raise the TTL back WITHIN the original window. The
        # stale text row must not be served (await_count keeps climbing).
        mgr, _, cache = build(global_ttl=3600.0, tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session

        session.call_tool.return_value = _text_result("stale-text")
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert session.call_tool.await_count == 1
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None

        # Lower to 0; identical call now returns non-text → invalidates the row.
        mgr._config.cache.default_ttl_seconds = 0
        session.call_tool.return_value = _nontext_result()
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert session.call_tool.await_count == 2

        # Raise the TTL back within the original 3600s window; the tool returns
        # text again. Without the fix the stale row would serve (await_count
        # stays 2); with it the row is gone → upstream is hit and re-cached.
        mgr._config.cache.default_ttl_seconds = 3600.0
        session.call_tool.return_value = _text_result("fresh-text")
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert session.call_tool.await_count == 3  # not served from the stale row
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None  # fresh row cached


@pytest.mark.asyncio
class TestTtlZeroErrorAndEmptyInvalidation:
    """#541 follow-through (codex review of PR #548): the error-raise and the
    truly-empty early returns in ``_call_tool_inner`` also happen BEFORE the
    Stage-5 store, so a text-bearing error (text-only or mixed) or an empty
    response under a disabled (``ttl<=0``) cache must invalidate a prior cached
    text row too — otherwise it resurfaces once the TTL is raised back within the
    row's frozen window. (A non-text-ONLY error has no text and is handled by the
    passthrough branch, covered in ``TestTtlZeroNonTextInvalidation``.)"""

    async def test_mixed_error_invalidates_prior_text_row(self, build):
        from mcp.server.fastmcp.exceptions import ToolError

        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session

        session.call_tool.return_value = _text_result("payload")
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None

        mgr._config.cache.default_ttl_seconds = 0
        session.call_tool.return_value = _mixed_error_result("boom")
        with pytest.raises(ToolError):
            await mgr.call_tool("srv", "reader", {"q": "a"})

        assert _get(mgr, cache, "reader", {"q": "a"}) is None  # stale text row gone

    async def test_text_only_error_invalidates_prior_text_row(self, build):
        from mcp.server.fastmcp.exceptions import ToolError

        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session

        session.call_tool.return_value = _text_result("payload")
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None

        mgr._config.cache.default_ttl_seconds = 0
        session.call_tool.return_value = _error_result("boom")
        with pytest.raises(ToolError):
            await mgr.call_tool("srv", "reader", {"q": "a"})

        assert _get(mgr, cache, "reader", {"q": "a"}) is None

    async def test_empty_response_invalidates_prior_text_row(self, build):
        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session

        session.call_tool.return_value = _text_result("payload")
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None

        mgr._config.cache.default_ttl_seconds = 0
        session.call_tool.return_value = _empty_result()
        result = await mgr.call_tool("srv", "reader", {"q": "a"})

        assert result == "[empty response]"
        assert _get(mgr, cache, "reader", {"q": "a"}) is None


@pytest.mark.asyncio
class TestTtlZeroTextStoreSkipInvalidation:
    """#541 follow-through (codex review of PR #550): the text store-skip branches
    in ``_store_cache`` (cache-ineligible, progressive-passthrough-on-error,
    transient-key) bypass ``ProxyCache.set`` and so never reach its ``ttl<=0``
    self-heal. Under a disabled cache a TEXT response that takes one of those
    branches must still invalidate a stale prior row — guaranteed by hoisting the
    ``ttl<=0`` invalidation above the whole skip chain (it short-circuits before
    any skip reason is evaluated)."""

    async def test_ineligible_text_response_invalidates_prior_row_under_ttl_zero(self, build):
        # Cache a row while the tool is eligible and the TTL is positive.
        mgr, _, cache = build(tools=[_tool("t")])  # unannotated → eligible
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("payload")
        await mgr.call_tool("srv", "t", {"q": "a"})
        assert _get(mgr, cache, "t", {"q": "a"}) is not None

        # Disable caching globally AND make the tool cache-ineligible (cache=False).
        # Without the hoisted ttl<=0 invalidation, _store_cache would take the
        # "not cache-eligible" skip branch (before reaching set()) and leave the
        # stale row live, to resurface once the TTL is raised back.
        mgr._config.cache.default_ttl_seconds = 0
        mgr._connections["srv"].config.tool_overrides["t"] = ToolOverrideConfig(cache=False)
        session.call_tool.return_value = _text_result("new-payload")
        await mgr.call_tool("srv", "t", {"q": "a"})

        assert _get(mgr, cache, "t", {"q": "a"}) is None  # stale row invalidated


@pytest.mark.asyncio
class TestTtlZeroRaisedFailureInvalidation:
    """#541 follow-through (#548/#550 completed every *returned* response shape):
    an exception RAISED out of ``_call_tool_inner`` — an upstream fetch failure or
    a pipeline-stage error — also exits before the Stage-5 store, so under a
    disabled (``ttl<=0``) cache a prior text row frozen at a positive TTL would
    survive the failed call and resurface once the TTL is raised back within its
    window. The ``ttl<=0`` dispatch in ``_call_tool_guarded`` now backstops every
    raise with the same best-effort invalidation."""

    async def test_upstream_raise_invalidates_prior_text_row(self, build):
        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session

        session.call_tool.return_value = _text_result("payload")
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None

        mgr._config.cache.default_ttl_seconds = 0
        session.call_tool.side_effect = RuntimeError("upstream died")
        with pytest.raises(RuntimeError, match="upstream died"):
            await mgr.call_tool("srv", "reader", {"q": "a"})

        assert _get(mgr, cache, "reader", {"q": "a"}) is None  # stale text row gone

    async def test_upstream_raise_under_positive_ttl_leaves_row(self, build):
        # Under an ENABLED cache a failure must NOT delete the live row — the
        # TTL governs freshness there, and a transient upstream blip serving
        # the cached copy on the next call is the configured behavior.
        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session

        session.call_tool.return_value = _text_result("payload")
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None

        session.call_tool.side_effect = RuntimeError("blip")
        # The enabled-cache path serves the live row without hitting upstream,
        # so drive the raise through a DIFFERENT key (same tool) and then check
        # the original row survived.
        with pytest.raises(RuntimeError, match="blip"):
            await mgr.call_tool("srv", "reader", {"q": "other"})

        assert _get(mgr, cache, "reader", {"q": "a"}) is not None

    async def test_raise_window_does_not_resurface_stale_row(self, build):
        # Full #541 sequence with a raised failure in the disabled window:
        # cache under positive TTL → lower to 0 → the identical call RAISES
        # (invalidates) → raise the TTL back within the original window. The
        # stale row must not be served (await_count keeps climbing).
        mgr, _, cache = build(global_ttl=3600.0, tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session

        session.call_tool.return_value = _text_result("stale-text")
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert session.call_tool.await_count == 1

        mgr._config.cache.default_ttl_seconds = 0
        session.call_tool.side_effect = RuntimeError("net down")
        with pytest.raises(RuntimeError, match="net down"):
            await mgr.call_tool("srv", "reader", {"q": "a"})
        assert session.call_tool.await_count == 2

        mgr._config.cache.default_ttl_seconds = 3600.0
        session.call_tool.side_effect = None
        session.call_tool.return_value = _text_result("fresh-text")
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert session.call_tool.await_count == 3  # not served from the stale row
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None  # fresh row cached

    async def test_is_error_raise_invalidates_exactly_once(self, build):
        # The isError branch invalidates inside ``_call_tool_inner`` and marks
        # its ToolError (``_mark_cache_invalidated``); the raised-failure
        # backstop must not issue a second DELETE for the same call.
        from unittest.mock import patch as mock_patch

        from mcp.server.fastmcp.exceptions import ToolError

        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session

        session.call_tool.return_value = _text_result("payload")
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert _get(mgr, cache, "reader", {"q": "a"}) is not None

        mgr._config.cache.default_ttl_seconds = 0
        session.call_tool.return_value = _error_result("boom")
        with mock_patch.object(cache, "invalidate", wraps=cache.invalidate) as spy:
            with pytest.raises(ToolError):
                await mgr.call_tool("srv", "reader", {"q": "a"})

        assert spy.call_count == 1  # isError site only; backstop skipped via marker
        assert _get(mgr, cache, "reader", {"q": "a"}) is None

    async def test_unmarked_raise_invalidates_via_backstop_once(self, build):
        # Complement of the exactly-once test: a plain raise carries no marker,
        # so the backstop is the ONE site that runs — also exactly one DELETE.
        from unittest.mock import patch as mock_patch

        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session

        session.call_tool.return_value = _text_result("payload")
        await mgr.call_tool("srv", "reader", {"q": "a"})

        mgr._config.cache.default_ttl_seconds = 0
        session.call_tool.side_effect = RuntimeError("upstream died")
        with mock_patch.object(cache, "invalidate", wraps=cache.invalidate) as spy:
            with pytest.raises(RuntimeError):
                await mgr.call_tool("srv", "reader", {"q": "a"})

        assert spy.call_count == 1
        assert _get(mgr, cache, "reader", {"q": "a"}) is None


# ── Cache-key components: _context_query + compression fingerprint ───────


@pytest.mark.asyncio
class TestCacheKeyComponents:
    """The cache key includes ``_context_query`` and a fingerprint of the
    resolved compression settings: the stored body is the COMPRESSED response,
    which is query-aware (BM25 budgets) and config-dependent, so neither a
    different query context nor a config hot reload may serve another key's
    row."""

    async def test_different_context_query_is_a_separate_entry(self, build):
        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("payload")

        await mgr.call_tool("srv", "reader", {"q": "a", "_context_query": "alpha"})
        assert session.call_tool.call_count == 1
        # Same args, different query context → MISS, its own row.
        await mgr.call_tool("srv", "reader", {"q": "a", "_context_query": "beta"})
        assert session.call_tool.call_count == 2
        assert cache.stats()["total_entries"] == 2
        # Repeat of the first query context → HIT (no third upstream call).
        await mgr.call_tool("srv", "reader", {"q": "a", "_context_query": "alpha"})
        assert session.call_tool.call_count == 2

    async def test_no_query_and_query_do_not_share_a_row(self, build):
        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("payload")

        await mgr.call_tool("srv", "reader", {"q": "a"})
        await mgr.call_tool("srv", "reader", {"q": "a", "_context_query": "alpha"})
        assert session.call_tool.call_count == 2
        assert cache.stats()["total_entries"] == 2

    async def test_config_change_stops_serving_old_rows(self, build):
        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("payload")

        await mgr.call_tool("srv", "reader", {"q": "a"})
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert session.call_tool.call_count == 1  # steady-state hit

        # Hot-reload of a byte-affecting global → fingerprint rotates → the
        # pre-change row is never served again.
        mgr._config.min_result_retention = 0.11
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert session.call_tool.call_count == 2

    async def test_max_upstream_chars_change_stops_serving_old_rows(self, build):
        # ``max_upstream_chars`` truncates ``original_text`` at Stage-3 SHAPE,
        # before cleaning/compression, so an oversized response is cached under
        # that budget. Lowering it must rotate the fingerprint (else the next
        # identical call serves a body shaped under the old, larger limit).
        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("x" * 5000)

        await mgr.call_tool("srv", "reader", {"q": "a"})
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert session.call_tool.call_count == 1  # steady-state hit

        mgr._config.max_upstream_chars = 1000  # oversized response now truncates
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert session.call_tool.call_count == 2

    async def test_scorer_change_stops_serving_old_rows(self, build):
        # The query-aware compressors build budget from the relevance scorer, so
        # a bm25→embedding switch changes the cached bytes for a query-bearing
        # call. The fingerprint must rotate on the scorer config change.
        from memtomem_stm.proxy.config import RelevanceScorerConfig

        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("payload")

        await mgr.call_tool("srv", "reader", {"q": "a", "_context_query": "alpha"})
        await mgr.call_tool("srv", "reader", {"q": "a", "_context_query": "alpha"})
        assert session.call_tool.call_count == 1  # steady-state hit

        mgr._config.relevance_scorer = RelevanceScorerConfig(scorer="embedding")
        await mgr.call_tool("srv", "reader", {"q": "a", "_context_query": "alpha"})
        assert session.call_tool.call_count == 2

    async def test_store_passes_key_components(self, build):
        # The Stage-5 store must key on the SAME components the lookup used;
        # a store under different components is unreachable (hit rate 0%).
        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("payload")

        with patch.object(cache, "set", wraps=cache.set) as spy:
            await mgr.call_tool("srv", "reader", {"q": "a", "_context_query": "alpha"})
        kwargs = spy.call_args.kwargs
        assert kwargs["context_query"] == "alpha"
        assert kwargs["config_fingerprint"] == _fp(mgr, "reader")

    async def test_non_string_context_query_keys_like_absent(self, build):
        # Mirrors the pipeline's isinstance-str coercion: a malformed
        # ``_context_query`` behaves exactly like no query, including in the key.
        mgr, _, cache = build(tools=[_tool("reader", _ann(read_only=True))])
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _text_result("payload")

        await mgr.call_tool("srv", "reader", {"q": "a", "_context_query": 42})
        await mgr.call_tool("srv", "reader", {"q": "a"})
        assert session.call_tool.call_count == 1  # second call is a HIT
