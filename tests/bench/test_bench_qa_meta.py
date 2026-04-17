"""bench_qa_meta — self-test probes that guard bench_qa's detection power.

Each probe injects a specific failure mode (neutralised compressor, corrupted
progressive store, disabled retention guard) and asserts the bench signal
that the normal suite relies on. A probe failing here means a bench_qa gate
has silently stopped detecting the failure class it was designed to catch —
these tests act as load-bearing smoke for the harness itself.

**Marker hygiene**: every test here carries ``@pytest.mark.bench_qa_meta``
and **only** that marker. CI runs ``-m bench_qa``, which excludes this
module by design (see ``/Users/pdstudio/.claude/plans/mcp-snug-river.md``
§ Marker separation). Run locally via::

    uv run pytest -m bench_qa_meta

Probes intentionally reach for private ``ProxyManager`` attributes
(``_apply_compression``) and patch class-level methods on
``ProgressiveStoreAdapter``; that coupling is the point — the probes are
checking the same seams the production code stands on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.config import CleaningConfig
from memtomem_stm.proxy.progressive import ProgressiveResponse, ProgressiveStoreAdapter

from bench.bench_qa import latest_metrics_row, load_fixture, make_proxy_manager
from bench.bench_qa.progressive import reassemble
from bench.bench_qa.runner import make_tool_result


@pytest.mark.bench_qa_meta
@pytest.mark.asyncio
async def test_meta_compression_neutralize_trips_ratio_guard(tmp_path):
    """Neutralised compressor must trip the ``ratio_violation`` guard.

    Inverts ``test_bench_qa_normal_path``: that gate asserts
    ``ratio_violation == 0`` on happy-path fixtures. Here we force
    ``_apply_compression`` to return an empty string, and the same proxy
    row should now report ``ratio_violation == 1``. If this probe ever
    passes with ``ratio_violation == 0``, the dynamic-retention-floor guard
    has a hole — a compressor that drops every byte would slip past the
    bench suite.
    """
    fixture = load_fixture("s02")
    assert fixture.get("force_tier") is None, "s02 must stay on the happy-path track"

    mgr, store, session = make_proxy_manager(
        tmp_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
    )
    session.call_tool.return_value = make_tool_result(fixture["payload"])
    mgr._apply_compression = AsyncMock(return_value=("", None))

    await mgr.call_tool("fake", "tool_s02_meta", {})
    row = latest_metrics_row(store)
    try:
        assert row, "meta probe: proxy_metrics row was not written"
        assert row["ratio_violation"] == 1, (
            f"meta probe: empty-output compressor slipped past the "
            f"ratio_violation guard (row={row}); the bench_qa normal-path "
            "gate is no longer load-bearing."
        )
    finally:
        store.close()


@pytest.mark.bench_qa_meta
@pytest.mark.asyncio
async def test_meta_progressive_round_trip_detects_corruption(tmp_path, monkeypatch):
    """Corrupted progressive store must break the Tier-1 byte-identity gate.

    S06 exercises the Tier-1 progressive fallback and asserts
    ``reassembled.content == cleaned`` (PR #160/#165 invariant). This probe
    patches ``ProgressiveStoreAdapter.put`` to drop the last 10 chars of the
    stored content; the reassembled payload must diverge from the cleaned
    baseline. If this probe passes with equal content, the round-trip gate
    in ``test_s06_tier1_progressive_round_trip`` is cosmetic.
    """
    fixture = load_fixture("s06")
    assert fixture["force_tier"] == 1, "s06 must stay on the Tier-1 track"
    payload = fixture["payload"]

    original_put = ProgressiveStoreAdapter.put

    def lossy_put(self, key: str, resp: ProgressiveResponse) -> None:
        new_content = resp.content[:-10] if len(resp.content) >= 10 else ""
        corrupted = ProgressiveResponse(
            content=new_content,
            total_chars=len(new_content),
            total_lines=resp.total_lines,
            content_type=resp.content_type,
            structure_hint=resp.structure_hint,
            created_at=resp.created_at,
            ttl_seconds=resp.ttl_seconds,
            access_count=resp.access_count,
        )
        original_put(self, key, corrupted)

    monkeypatch.setattr(ProgressiveStoreAdapter, "put", lossy_put)

    mgr, store, session = make_proxy_manager(
        tmp_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
    )
    session.call_tool.return_value = make_tool_result(payload)
    # Force the ratio guard to escalate into Tier-1 progressive delivery so
    # the corrupted put() actually runs.
    mgr._apply_compression = AsyncMock(return_value=("x" * 50, None))

    first_chunk = await mgr.call_tool("fake", "tool_s06_meta", {})
    try:
        reassembled = reassemble(mgr, first_chunk)
        cleaned = DefaultContentCleaner(CleaningConfig()).clean(payload)
        assert reassembled.content != cleaned, (
            "meta probe: progressive store dropped 10 chars but reassembly "
            "still matched the cleaned payload — the round-trip byte-identity "
            "gate is broken."
        )
        assert len(cleaned) - len(reassembled.content) == 10, (
            f"meta probe: expected exactly 10 missing chars, got "
            f"{len(cleaned) - len(reassembled.content)} "
            f"(cleaned={len(cleaned)}, reassembled={len(reassembled.content)})"
        )
    finally:
        store.close()


@pytest.mark.bench_qa_meta
@pytest.mark.asyncio
async def test_meta_min_retention_zero_disables_ratio_guard(tmp_path):
    """``min_result_retention=0.0`` must leave the ratio guard silent.

    The happy-path gate relies on the guard firing whenever the compressor
    undershoots the dynamic retention floor. This probe sets retention to 0
    and monkeypatches the compressor to emit ~1 % of the cleaned payload;
    the proxy row must record ``ratio_violation == 0`` anyway, and
    ``compressed_chars`` must equal the 1 % stub — proving the patch ran and
    the guard deliberately abstained. A false-positive here would obscure
    real regressions on operators who intentionally disabled the guard.
    """
    fixture = load_fixture("s02")

    mgr, store, session = make_proxy_manager(
        tmp_path,
        compression=fixture["expected_compressor"],
        max_result_chars=fixture["max_result_chars"],
        min_retention=0.0,
    )
    session.call_tool.return_value = make_tool_result(fixture["payload"])
    tiny = "x" * max(1, len(fixture["payload"]) // 100)
    mgr._apply_compression = AsyncMock(return_value=(tiny, None))

    await mgr.call_tool("fake", "tool_s02_meta_guard_off", {})
    row = latest_metrics_row(store)
    try:
        assert row, "meta probe: proxy_metrics row was not written"
        assert row["ratio_violation"] == 0, (
            f"meta probe: guard fired with min_result_retention=0.0 "
            f"(row={row}); guard should be disabled in this configuration."
        )
        assert row["compressed_chars"] == len(tiny), (
            f"meta probe: compressed_chars={row['compressed_chars']} "
            f"expected={len(tiny)} — compressor patch did not take effect, "
            "so the probe did not actually test the guard-off path."
        )
    finally:
        store.close()
