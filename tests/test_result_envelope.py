"""MCP result-envelope preservation through the proxy call path.

Pins the envelope contract introduced with the ⑥ first-user fixes:

- results carrying ``structuredContent`` / result-level ``_meta`` return a
  full ``CallToolResult`` with those fields verbatim (text still compressed);
- the isError check runs BEFORE the no-text passthrough early-return, so a
  non-text-only error surfaces as an error instead of a passthrough success;
- content-block order is preserved: the processed text is reinserted at the
  upstream's first-text position;
- the cache never stores an envelope-bearing response (the ``result TEXT``
  schema cannot reproduce the fields), while envelope-free text responses
  keep storing and serving identically on hit and miss.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent

from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.config import (
    CacheConfig,
    CompressionStrategy,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import _NON_TEXT_ERROR_TEXT, ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore

# ── fixtures / helpers (style mirrors test_call_tool_inner_characterization) ─


def _text_content(text: str):
    return SimpleNamespace(type="text", text=text)


def _image_content() -> ImageContent:
    # A REAL content block: the envelope return path embeds non-text blocks in
    # a pydantic-validated CallToolResult, where a SimpleNamespace would fail.
    return ImageContent(type="image", data="aGVsbG8=", mimeType="image/png")


def _result(
    text: str | None = None,
    *,
    blocks: list | None = None,
    is_error: bool = False,
    structured: dict | None = None,
    meta: dict | None = None,
):
    """Mock upstream call_tool result with optional envelope fields."""
    if blocks is None:
        blocks = [_text_content(text)] if text is not None else []
    return SimpleNamespace(
        content=blocks, isError=is_error, structuredContent=structured, meta=meta
    )


def _build_mgr(
    tmp_path: Path,
    *,
    with_cache: bool = False,
) -> tuple[ProxyManager, MetricsStore, ProxyCache | None]:
    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=CompressionStrategy.TRUNCATE,
        max_result_chars=50000,
        max_retries=0,
        reconnect_delay_seconds=0.0,
    )
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": server_cfg},
        cache=CacheConfig(db_path=tmp_path / "cache.db"),
    )
    mgr = ProxyManager(proxy_cfg, TokenTracker(metrics_store=store))
    mgr._connections["srv"] = UpstreamConnection(
        name="srv", config=server_cfg, session=AsyncMock(), tools=[]
    )
    cache: ProxyCache | None = None
    if with_cache:
        cache = ProxyCache(tmp_path / "cache.db", max_entries=100)
        cache.initialize()
        mgr._cache = cache
    return mgr, store, cache


@pytest.fixture
def make_mgr(tmp_path):
    opened: list[tuple[MetricsStore, ProxyCache | None]] = []

    def _factory(**kwargs):
        mgr, store, cache = _build_mgr(tmp_path, **kwargs)
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


def _set_upstream(mgr: ProxyManager, result) -> AsyncMock:
    session = mgr._connections["srv"].session
    session.call_tool = AsyncMock(return_value=result)
    return session


def _latest_error(store: MetricsStore) -> dict | None:
    row = store._db.execute(
        "SELECT is_error, error_message FROM proxy_metrics ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {"is_error": row[0], "error_message": row[1]} if row else None


# ── envelope preservation on the call path ───────────────────────────────


class TestEnvelopePreservation:
    async def test_text_with_structured_and_meta_returns_envelope(self, make_mgr):
        mgr, _, _ = make_mgr()
        _set_upstream(mgr, _result("payload text", structured={"a": 1}, meta={"trace": "x"}))
        res = await mgr._call_tool_inner("srv", "tool", {})
        assert isinstance(res, CallToolResult)
        assert res.structuredContent == {"a": 1}
        assert res.meta == {"trace": "x"}
        assert res.isError is False
        assert len(res.content) == 1
        assert isinstance(res.content[0], TextContent)
        assert "payload text" in res.content[0].text

    async def test_meta_only_result_preserves_meta(self, make_mgr):
        mgr, _, _ = make_mgr()
        _set_upstream(mgr, _result("payload", meta={"m": 2}))
        res = await mgr._call_tool_inner("srv", "tool", {})
        assert isinstance(res, CallToolResult)
        assert res.meta == {"m": 2}
        assert res.structuredContent is None

    async def test_structured_only_empty_content_returns_envelope_not_sentinel(self, make_mgr):
        mgr, store, _ = make_mgr()
        _set_upstream(mgr, _result(blocks=[], structured={"only": "structured"}))
        res = await mgr._call_tool_inner("srv", "tool", {})
        assert isinstance(res, CallToolResult)
        assert res.structuredContent == {"only": "structured"}
        assert res.content == []  # no "[empty response]" fabricated into the envelope
        row = store._db.execute(
            "SELECT original_chars, compressed_chars FROM proxy_metrics ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert tuple(row) == (0, 0)  # passthrough 0/0 metric still recorded (#558)

    async def test_non_text_with_structured_preserves_block_order(self, make_mgr):
        mgr, _, _ = make_mgr()
        img = _image_content()
        _set_upstream(mgr, _result(blocks=[img, _text_content("txt")], structured={"a": 1}))
        res = await mgr._call_tool_inner("srv", "tool", {})
        assert isinstance(res, CallToolResult)
        assert res.structuredContent == {"a": 1}
        assert res.content[0] is img  # leading image stays leading
        assert isinstance(res.content[1], TextContent)
        assert "txt" in res.content[1].text

    async def test_text_only_no_envelope_stays_plain_str(self, make_mgr):
        mgr, _, _ = make_mgr()
        _set_upstream(mgr, _result("plain"))
        res = await mgr._call_tool_inner("srv", "tool", {})
        assert isinstance(res, str)
        assert "plain" in res

    async def test_mixed_no_envelope_preserves_order_as_list(self, make_mgr):
        mgr, _, _ = make_mgr()
        img = _image_content()
        _set_upstream(mgr, _result(blocks=[img, _text_content("txt")]))
        res = await mgr._call_tool_inner("srv", "tool", {})
        assert isinstance(res, list)
        assert res[0] is img
        assert isinstance(res[1], TextContent)

    async def test_mixed_text_first_keeps_text_first(self, make_mgr):
        mgr, _, _ = make_mgr()
        img = _image_content()
        _set_upstream(mgr, _result(blocks=[_text_content("txt"), img]))
        res = await mgr._call_tool_inner("srv", "tool", {})
        assert isinstance(res, list)
        assert isinstance(res[0], TextContent)
        assert res[1] is img


# ── isError ordering ─────────────────────────────────────────────────────


class TestIsErrorOrdering:
    async def test_non_text_only_error_raises_instead_of_leaking(self, make_mgr):
        """The leak this PR fixes: a non-text-only isError result previously
        took the passthrough early-return and came back as a success list."""
        mgr, store, _ = make_mgr()
        _set_upstream(mgr, _result(blocks=[_image_content()], is_error=True))
        with pytest.raises(ToolError, match="non-text error content"):
            await mgr._call_tool_inner("srv", "tool", {})
        err = _latest_error(store)
        assert err is not None
        assert err["is_error"] == 1
        assert err["error_message"] == _NON_TEXT_ERROR_TEXT

    async def test_empty_content_error_raises(self, make_mgr):
        mgr, _, _ = make_mgr()
        _set_upstream(mgr, _result(blocks=[], is_error=True))
        with pytest.raises(ToolError, match="non-text error content"):
            await mgr._call_tool_inner("srv", "tool", {})

    async def test_error_with_text_and_structured_raises_with_text(self, make_mgr):
        # Documented limitation: ToolError is text-only, so the errored
        # result's structuredContent/_meta are dropped.
        mgr, _, _ = make_mgr()
        _set_upstream(mgr, _result("boom", is_error=True, structured={"detail": 1}))
        with pytest.raises(ToolError, match="boom"):
            await mgr._call_tool_inner("srv", "tool", {})

    async def test_error_never_becomes_success_after_compression(self, make_mgr):
        mgr, _, _ = make_mgr()
        _set_upstream(mgr, _result("x" * 5000, is_error=True))
        with pytest.raises(ToolError):
            await mgr._call_tool_inner("srv", "tool", {})


# ── cache store bypass for envelope-bearing responses ────────────────────


class TestEnvelopeStoreBypass:
    @pytest.mark.parametrize(
        ("structured", "meta"),
        [({"a": 1}, None), (None, {"m": 1}), ({"a": 1}, {"m": 1})],
        ids=["structured-only", "meta-only", "both"],
    )
    async def test_envelope_response_is_never_stored(self, make_mgr, structured, meta):
        mgr, _, cache = make_mgr(with_cache=True)
        session = _set_upstream(mgr, _result("cacheable text", structured=structured, meta=meta))
        first = await mgr._call_tool_inner("srv", "tool", {"a": 1})
        second = await mgr._call_tool_inner("srv", "tool", {"a": 1})
        assert cache.stats()["total_entries"] == 0  # store bypassed
        assert session.call_tool.await_count == 2  # second call re-hit upstream
        assert isinstance(first, CallToolResult)
        assert isinstance(second, CallToolResult)

    async def test_envelope_response_invalidates_stale_row_when_cache_disabled(self, make_mgr):
        """Mirror of the #541 non-text invalidation for the envelope gate: an
        envelope response under a disabled cache (resolved ttl<=0) must drop a
        stale prior text row for the same key instead of leaving it live."""
        mgr, _, cache = make_mgr(with_cache=True)
        fp = mgr._cache_key_fingerprint("srv", "tool", cfg_snap=mgr._config)
        cache.set("srv", "tool", {"a": 1}, "stale text", ttl_seconds=60.0, config_fingerprint=fp)
        assert cache.stats()["total_entries"] == 1
        mgr._config.cache.default_ttl_seconds = 0
        _set_upstream(mgr, _result("fresh", structured={"a": 1}))
        res = await mgr._call_tool_inner("srv", "tool", {"a": 1})
        assert isinstance(res, CallToolResult)
        assert cache.stats()["total_entries"] == 0

    async def test_text_only_still_stores_and_hit_equals_miss(self, make_mgr):
        mgr, _, cache = make_mgr(with_cache=True)
        session = _set_upstream(mgr, _result("cacheable text"))
        miss = await mgr.call_tool("srv", "tool", {"a": 1})
        hit = await mgr.call_tool("srv", "tool", {"a": 1})
        assert session.call_tool.await_count == 1  # second call served from cache
        assert cache.stats()["total_entries"] == 1
        assert isinstance(miss, str)
        assert hit == miss  # hit == miss text contract
