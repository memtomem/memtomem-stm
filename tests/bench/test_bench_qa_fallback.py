"""bench_qa — fallback-ladder gates (Tier-1 progressive, Tier-2 hybrid, Tier-3 truncate).

Each scenario drives ``ProxyManager.call_tool()`` through a forced ratio
violation so the Tier-N fallback path activates. Assertions build on
``tests/test_compression_ratio_guard.py`` — **do not** duplicate those
unit checks; bench_qa adds the scenario-level story (realistic payload,
QA probes, round-trip byte-identity on Tier-1).

S6 → Tier-1 progressive_fallback: stub ``_apply_compression`` to overshoot
so the ratio guard takes over; verify that reading every ``stm_proxy_read_more``
chunk reproduces the cleaned payload exactly (PR #160/#165 invariant).

S1 → Tier-2 hybrid_fallback: also stub ``_apply_progressive`` to raise, so
the ladder falls through to hybrid; verify the effective strategy and the
retention floor.

S8 → Tier-3 truncate_fallback: stub progressive and hybrid so the guaranteed
floor runs; verify the strategy marker and a non-empty, non-violating
compressed_chars lower bound.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.config import CleaningConfig

from bench.bench_qa import latest_metrics_row, load_fixture, make_proxy_manager
from bench.bench_qa.progressive import reassemble
from bench.bench_qa.runner import make_tool_result


def _stub_raise(*_args, **_kwargs):
    raise RuntimeError("forced fallback-ladder failure (bench_qa)")


@pytest.mark.bench_qa
@pytest.mark.asyncio
async def test_s06_tier1_progressive_round_trip(tmp_path):
    fixture = load_fixture("s06")
    assert fixture["force_tier"] == 1
    payload = fixture["payload"]
    assert len(payload) >= 10_000, (
        f"s06 payload is {len(payload)} chars; progressive fallback needs >=10KB "
        "to exceed the dynamic retention floor"
    )

    mgr, store, session = make_proxy_manager(
        tmp_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
    )
    session.call_tool.return_value = make_tool_result(payload)
    # Force compressor to overshoot the retention floor so the ratio guard
    # promotes the call to progressive delivery.
    mgr._apply_compression = AsyncMock(return_value=("x" * 50, None))

    first_chunk = await mgr.call_tool("fake", "tool_s06", {})
    row = latest_metrics_row(store)
    try:
        assert row["ratio_violation"] == 1, "Tier-1 should record a violation"
        assert "→progressive_fallback" in (row["compression_strategy"] or ""), (
            f"unexpected strategy: {row['compression_strategy']!r}"
        )
        assert "stm_proxy_read_more" in first_chunk
        assert "has_more=True" in first_chunk
        assert "ttl=" in first_chunk, "progressive footer should expose TTL"

        # Round-trip: follow read_more until has_more=False and compare bytes.
        # The invariant is against the *cleaned* payload — the pipeline stores
        # the cleaner's output, not the raw upstream response.
        reassembled = reassemble(mgr, first_chunk)
        assert reassembled.has_more_final is False
        assert reassembled.chunks >= 2, (
            f"expected multiple chunks for {len(payload)}-char payload, got {reassembled.chunks}"
        )
        cleaned = DefaultContentCleaner(CleaningConfig()).clean(payload)
        assert reassembled.content == cleaned, (
            "reassembled content differs from cleaned payload — "
            f"lens {len(reassembled.content)} vs {len(cleaned)}; "
            "violates the PR #160/#165 byte-identity invariant"
        )
    finally:
        store.close()


@pytest.mark.bench_qa
@pytest.mark.asyncio
async def test_s01_tier2_hybrid_fallback(tmp_path):
    fixture = load_fixture("s01")
    assert fixture["force_tier"] == 2
    payload = fixture["payload"]
    assert payload.count("##") >= 3, (
        "s01 hybrid_fallback needs at least 3 headings so hybrid has structure to preserve"
    )

    mgr, store, session = make_proxy_manager(
        tmp_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
    )
    session.call_tool.return_value = make_tool_result(payload)
    mgr._apply_compression = AsyncMock(return_value=("x" * 50, None))
    mgr._apply_progressive = _stub_raise  # type: ignore[method-assign]

    result = await mgr.call_tool("fake", "tool_s01", {})
    row = latest_metrics_row(store)
    try:
        assert row["ratio_violation"] == 1
        strategy = row["compression_strategy"] or ""
        assert "→hybrid_fallback" in strategy, f"unexpected strategy: {strategy!r}"
        # Hybrid_fallback honors the retention floor but only after head +
        # TOC selection, so the exact floor depends on heading density. The
        # initial observed value on this fixture is ~850 chars — lock in the
        # sanity lower bound and ratchet up as the suite stabilises.
        assert row["compressed_chars"] > 500, (
            f"hybrid_fallback compressed_chars={row['compressed_chars']} is suspiciously small"
        )
        # Realistic payload preserves at least one of the expected anchors.
        lowered = result.lower()
        assert any(kw.lower() in lowered for kw in fixture["expected_keywords"]), (
            f"hybrid_fallback lost every expected_keyword; result[:200]={result[:200]!r}"
        )
    finally:
        store.close()


@pytest.mark.bench_qa
@pytest.mark.asyncio
async def test_s08_tier3_truncate_fallback(tmp_path):
    fixture = load_fixture("s08")
    assert fixture["force_tier"] == 3

    mgr, store, session = make_proxy_manager(
        tmp_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
    )
    session.call_tool.return_value = make_tool_result(fixture["payload"])
    mgr._apply_compression = AsyncMock(return_value=("x" * 50, None))
    mgr._apply_progressive = _stub_raise  # type: ignore[method-assign]
    mgr._apply_hybrid = _stub_raise  # type: ignore[method-assign]

    result = await mgr.call_tool("fake", "tool_s08", {})
    row = latest_metrics_row(store)
    try:
        assert row["ratio_violation"] == 1
        strategy = row["compression_strategy"] or ""
        assert "→truncate_fallback" in strategy, f"unexpected strategy: {strategy!r}"
        # Tier-3 guarantees a floor — result must be non-empty.
        assert len(result) > 0, "Tier-3 truncate_fallback returned empty result"
        # Observed compressed_chars on this fixture is ~1_000; lock a
        # conservative lower bound that still catches empty-truncate bugs.
        assert row["compressed_chars"] > 500, (
            f"truncate_fallback compressed_chars={row['compressed_chars']} below expected floor"
        )
    finally:
        store.close()
