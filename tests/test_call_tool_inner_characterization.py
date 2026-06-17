"""Characterization net for the ``ProxyManager._call_tool_inner`` refactor (A1).

These tests pin the cross-stage invariants that the staged extract-method
refactor (PR1 → PR4b) must preserve byte-for-byte. They intentionally target the
ORCHESTRATION seams the refactor touches, which the per-stage unit tests do not
exercise end-to-end:

- **R1**  ``compressed_chars`` is ``len(compressed)`` on the compress branch but
  ``len(cleaned)`` on the progressive branch (the two branches disagree by design).
- **R3**  the cache stores the PRE-surfacing ``compressed``, never ``surfaced``.
- **R5**  the cache key uses the unmutated ``cache_args`` (no ``_trace_id``).
- **R6**  progressive paths surface via ``_apply_surfacing_on_progressive`` while
  non-progressive paths use ``_apply_surfacing`` — and the recorded
  ``surfacing_on_progressive_ok`` / ``surface_error`` match the engine outcome.
- **R8**  a non-text-only response records a ``0/0`` metric and returns the list;
  an empty response records NOTHING and returns the sentinel string.
- **R10** every downstream consumer receives the mutated ``upstream_args`` (with
  ``_trace_id``); only ``cache.set`` receives the unmutated ``cache_args``.
- ``scorer_fallback`` reflects a relevance-scorer fallback during compression.

This module must stay GREEN UNCHANGED through PR1-PR4b — that invariant is the
behavior-preservation proof of the refactor.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProgressiveConfig,
    ProxyConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore


# ── fixtures / helpers ───────────────────────────────────────────────────


def _text_content(text: str):
    return SimpleNamespace(type="text", text=text)


def _result(text: str, *, is_error: bool = False):
    return SimpleNamespace(content=[_text_content(text)], isError=is_error)


def _make_mgr(
    tmp_path: Path,
    *,
    compression: CompressionStrategy = CompressionStrategy.TRUNCATE,
    max_result_chars: int = 50000,
    min_retention: float = 0.65,
    progressive: ProgressiveConfig | None = None,
    with_cache: bool = False,
) -> tuple[ProxyManager, MetricsStore, ProxyCache | None]:
    """ProxyManager wired to a real MetricsStore (and optionally a real
    ProxyCache) with a mocked upstream session — the same seam the existing
    pipeline tests use."""
    store = MetricsStore(tmp_path / "metrics.db")
    store.initialize()
    server_cfg = UpstreamServerConfig(
        prefix="test",
        compression=compression,
        max_result_chars=max_result_chars,
        max_retries=0,
        reconnect_delay_seconds=0.0,
        progressive=progressive,
    )
    proxy_cfg = ProxyConfig(
        config_path=tmp_path / "proxy.json",
        upstream_servers={"srv": server_cfg},
        min_result_retention=min_retention,
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


def _latest(store: MetricsStore, *cols: str) -> dict | None:
    row = store._db.execute(
        f"SELECT {', '.join(cols)} FROM proxy_metrics ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(zip(cols, row)) if row is not None else None


def _row_count(store: MetricsStore) -> int:
    return store._db.execute("SELECT COUNT(*) FROM proxy_metrics").fetchone()[0]


class _FakeSurfacingEngine:
    """Minimal surfacing engine: the manager only reads ``injection_mode`` /
    ``observability`` and awaits ``surface(...)``."""

    def __init__(self, mode: str, *, surface_return: str = "surfaced", raises=None):
        self.injection_mode = mode
        self.observability = None
        self._surface_return = surface_return
        self._raises = raises

    async def surface(
        self,
        *,
        server: str,
        tool: str,
        arguments: dict,
        response_text: str,
        trace_id=None,
        context_query=None,
    ) -> str:
        if self._raises is not None:
            raise self._raises
        return self._surface_return


# ── R1: compressed_chars differs by branch ───────────────────────────────


@pytest.mark.asyncio
class TestCompressedCharsDualValue:
    async def test_compress_branch_records_compressed_length(self, tmp_path):
        mgr, store, _ = _make_mgr(
            tmp_path,
            compression=CompressionStrategy.TRUNCATE,
            max_result_chars=500,
            min_retention=0.0,  # disable the ratio-guard ladder
        )
        mgr._connections["srv"].session.call_tool.return_value = _result("word " * 1000)
        await mgr.call_tool("srv", "tool", {})
        row = _latest(store, "compressed_chars", "cleaned_chars", "compression_strategy")
        assert row["compression_strategy"] == "truncate"
        assert row["compressed_chars"] < row["cleaned_chars"]  # len(compressed)

    async def test_progressive_branch_records_cleaned_length(self, tmp_path):
        mgr, store, _ = _make_mgr(
            tmp_path,
            compression=CompressionStrategy.PROGRESSIVE,
            progressive=ProgressiveConfig(chunk_size=500),
        )
        mgr._connections["srv"].session.call_tool.return_value = _result(
            "content paragraph. " * 200
        )
        result = await mgr.call_tool("srv", "tool", {})
        assert "stm_proxy_read_more" in result
        row = _latest(store, "compressed_chars", "cleaned_chars", "compression_strategy")
        assert row["compression_strategy"] == "progressive"
        assert row["compressed_chars"] == row["cleaned_chars"]  # zero-loss → len(cleaned)


# ── R3 / R5: cache stores pre-surfacing compressed, keyed on cache_args ───


@pytest.mark.asyncio
class TestCacheStoresPreSurfacing:
    async def test_cache_value_excludes_surfaced_content(self, tmp_path):
        mgr, store, cache = _make_mgr(tmp_path, with_cache=True)
        mgr._connections["srv"].session.call_tool.return_value = _result("hello world")

        async def fake_surface(server, tool, arguments, text, *, trace_id=None, context_query=None):
            return text + "\n[[SURFACED]]"

        mgr._apply_surfacing = fake_surface

        result = await mgr.call_tool("srv", "tool", {})
        assert "[[SURFACED]]" in result  # the agent sees the surfaced response
        cached = cache.get("srv", "tool", {})
        assert cached == "hello world"  # cache holds the PRE-surfacing payload
        assert "[[SURFACED]]" not in cached

    async def test_cache_key_args_exclude_trace_id(self, tmp_path):
        mgr, store, cache = _make_mgr(tmp_path, with_cache=True)
        mgr._connections["srv"].session.call_tool.return_value = _result("payload")

        captured: dict = {}
        real_set = cache.set

        def spy_set(server, tool, args, value, **kwargs):
            captured["args"] = dict(args)
            captured["value"] = value
            return real_set(server, tool, args, value, **kwargs)

        cache.set = spy_set
        await mgr.call_tool("srv", "tool", {"q": "x"})
        assert captured["args"] == {"q": "x"}  # the unmutated snapshot
        assert "_trace_id" not in captured["args"]


# ── R10: which arg dict each consumer receives ───────────────────────────


@pytest.mark.asyncio
class TestArgsRouting:
    async def test_consumers_get_mutated_args_cache_gets_snapshot(self, tmp_path):
        mgr, store, cache = _make_mgr(tmp_path, with_cache=True)
        session = mgr._connections["srv"].session
        session.call_tool.return_value = _result("data")

        surf_spy = AsyncMock(wraps=mgr._apply_surfacing)
        mgr._apply_surfacing = surf_spy

        captured: dict = {}
        real_set = cache.set

        def spy_set(server, tool, args, value, **kwargs):
            captured["args"] = dict(args)
            return real_set(server, tool, args, value, **kwargs)

        cache.set = spy_set
        await mgr.call_tool("srv", "tool", {"q": "x"})

        # upstream call + surfacing receive the MUTATED upstream_args
        assert "_trace_id" in session.call_tool.await_args.args[1]
        assert "_trace_id" in surf_spy.await_args.args[2]  # arguments is positional #3
        # cache.set receives the UNMUTATED snapshot
        assert "_trace_id" not in captured["args"]
        assert captured["args"] == {"q": "x"}


# ── R6: surfacing-helper selection per path ──────────────────────────────


@pytest.mark.asyncio
class TestSurfacingHelperSelection:
    @staticmethod
    def _spy_both(mgr):
        plain = AsyncMock(wraps=mgr._apply_surfacing)
        prog = AsyncMock(wraps=mgr._apply_surfacing_on_progressive)
        mgr._apply_surfacing = plain
        mgr._apply_surfacing_on_progressive = prog
        return plain, prog

    async def test_non_progressive_uses_plain_surfacing(self, tmp_path):
        mgr, store, _ = _make_mgr(tmp_path, compression=CompressionStrategy.TRUNCATE)
        mgr._connections["srv"].session.call_tool.return_value = _result("small text")
        plain, prog = self._spy_both(mgr)
        await mgr.call_tool("srv", "tool", {})
        plain.assert_awaited_once()
        prog.assert_not_awaited()

    async def test_primary_progressive_uses_progressive_surfacing(self, tmp_path):
        mgr, store, _ = _make_mgr(
            tmp_path,
            compression=CompressionStrategy.PROGRESSIVE,
            progressive=ProgressiveConfig(chunk_size=500),
        )
        mgr._connections["srv"].session.call_tool.return_value = _result(
            "content paragraph. " * 200
        )
        plain, prog = self._spy_both(mgr)
        await mgr.call_tool("srv", "tool", {})
        prog.assert_awaited_once()
        plain.assert_not_awaited()

    async def test_progressive_fallback_uses_progressive_surfacing(self, tmp_path):
        mgr, store, _ = _make_mgr(
            tmp_path,
            compression=CompressionStrategy.TRUNCATE,
            min_retention=0.65,
            max_result_chars=500,
            progressive=ProgressiveConfig(chunk_size=500),
        )
        mgr._connections["srv"].session.call_tool.return_value = _result(
            "content paragraph. " * 800  # ~15k → truncate to 500 violates the floor
        )
        plain, prog = self._spy_both(mgr)
        await mgr.call_tool("srv", "tool", {})
        row = _latest(store, "compression_strategy")
        assert row["compression_strategy"] == "truncate→progressive_fallback"
        prog.assert_awaited_once()
        plain.assert_not_awaited()


# ── R6 (codex M3): surfacing_on_progressive_ok / surface_error metrics ────


@pytest.mark.asyncio
class TestSurfacingOnProgressiveMetric:
    @staticmethod
    def _progressive_mgr(tmp_path):
        return _make_mgr(
            tmp_path,
            compression=CompressionStrategy.PROGRESSIVE,
            progressive=ProgressiveConfig(chunk_size=500),
        )

    async def test_append_mode_records_ok_true(self, tmp_path):
        mgr, store, _ = self._progressive_mgr(tmp_path)
        mgr._surfacing_engine = _FakeSurfacingEngine("append", surface_return="chunk + memories")
        mgr._connections["srv"].session.call_tool.return_value = _result(
            "content paragraph. " * 200
        )
        await mgr.call_tool("srv", "tool", {})
        row = _latest(store, "surfacing_on_progressive_ok", "surface_error", "compression_strategy")
        assert row["compression_strategy"] == "progressive"
        assert row["surfacing_on_progressive_ok"] == 1
        assert row["surface_error"] is None

    async def test_prepend_mode_records_ok_none(self, tmp_path):
        mgr, store, _ = self._progressive_mgr(tmp_path)
        mgr._surfacing_engine = _FakeSurfacingEngine("prepend")
        mgr._connections["srv"].session.call_tool.return_value = _result(
            "content paragraph. " * 200
        )
        result = await mgr.call_tool("srv", "tool", {})
        assert "stm_proxy_read_more" in result  # prepend skips surfacing → footer intact
        row = _latest(store, "surfacing_on_progressive_ok", "surface_error")
        assert row["surfacing_on_progressive_ok"] is None
        assert row["surface_error"] is None

    async def test_surface_exception_records_ok_false_and_error(self, tmp_path):
        mgr, store, _ = self._progressive_mgr(tmp_path)
        mgr._surfacing_engine = _FakeSurfacingEngine("append", raises=RuntimeError("boom"))
        mgr._connections["srv"].session.call_tool.return_value = _result(
            "content paragraph. " * 200
        )
        await mgr.call_tool("srv", "tool", {})
        row = _latest(store, "surfacing_on_progressive_ok", "surface_error")
        assert row["surfacing_on_progressive_ok"] == 0
        assert row["surface_error"] == "RuntimeError"

    async def test_non_progressive_records_ok_none(self, tmp_path):
        mgr, store, _ = _make_mgr(tmp_path, compression=CompressionStrategy.TRUNCATE)
        mgr._connections["srv"].session.call_tool.return_value = _result("small text")
        await mgr.call_tool("srv", "tool", {})
        row = _latest(store, "surfacing_on_progressive_ok")
        assert row["surfacing_on_progressive_ok"] is None


# ── R8: early-return metric ownership ────────────────────────────────────


@pytest.mark.asyncio
class TestEarlyReturnMetrics:
    async def test_non_text_only_records_zero_metric(self, tmp_path):
        mgr, store, _ = _make_mgr(tmp_path)
        img = SimpleNamespace(type="image", data="x", mimeType="image/png")
        mgr._connections["srv"].session.call_tool.return_value = SimpleNamespace(
            content=[img], isError=False
        )
        result = await mgr.call_tool("srv", "tool", {})
        assert isinstance(result, list)
        assert len(result) == 1
        assert _row_count(store) == 1
        row = _latest(store, "original_chars", "compressed_chars")
        assert row["original_chars"] == 0
        assert row["compressed_chars"] == 0

    async def test_empty_response_records_nothing(self, tmp_path):
        mgr, store, _ = _make_mgr(tmp_path)
        mgr._connections["srv"].session.call_tool.return_value = SimpleNamespace(
            content=[], isError=False
        )
        result = await mgr.call_tool("srv", "tool", {})
        assert result == "[empty response]"
        assert _row_count(store) == 0


# ── scorer_fallback recorded around the compression call ─────────────────


@pytest.mark.asyncio
class TestScorerFallbackRecorded:
    async def test_true_when_scorer_falls_back_during_compression(self, tmp_path):
        mgr, store, _ = _make_mgr(
            tmp_path, compression=CompressionStrategy.TRUNCATE, min_retention=0.0
        )
        # ``_relevance_scorer`` is a hot-reload property over this backing field.
        mgr._relevance_scorer_instance = SimpleNamespace(fallback_count=0)
        mgr._connections["srv"].session.call_tool.return_value = _result("some text")

        async def bump(*args, **kwargs):
            mgr._relevance_scorer_instance.fallback_count += 1
            return "compressed-out", None

        mgr._apply_compression = bump
        await mgr.call_tool("srv", "tool", {})
        row = _latest(store, "scorer_fallback")
        assert row["scorer_fallback"] == 1

    async def test_false_when_scorer_count_stable(self, tmp_path):
        mgr, store, _ = _make_mgr(
            tmp_path, compression=CompressionStrategy.TRUNCATE, min_retention=0.0
        )
        mgr._relevance_scorer_instance = SimpleNamespace(fallback_count=0)
        mgr._connections["srv"].session.call_tool.return_value = _result("some text")

        async def no_bump(*args, **kwargs):
            return "compressed-out", None

        mgr._apply_compression = no_bump
        await mgr.call_tool("srv", "tool", {})
        row = _latest(store, "scorer_fallback")
        assert row["scorer_fallback"] == 0
