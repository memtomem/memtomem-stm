"""Tests for ProxyCache — SQLite response cache."""

from __future__ import annotations

import time

from memtomem_stm.proxy.cache import (
    ProxyCache,
    _make_key,
    response_carries_transient_key,
)
from memtomem_stm.proxy.compression import HybridCompressor, SelectiveCompressor
from memtomem_stm.proxy.progressive import PROGRESSIVE_FOOTER_TOKEN

# Markdown with enough sections that SELECTIVE/HYBRID emit a chunk TOC (which
# embeds a transient ``selection_key``) instead of falling back to truncation.
_TOC_TEXT = "# Doc\n\n" + "\n\n".join(
    f"## Section {i} heading text\n" + ("alpha beta gamma delta epsilon zeta " * 12)
    for i in range(12)
)


class TestProxyCacheBasic:
    def test_set_then_get(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "result", ttl_seconds=60.0)
        assert proxy_cache.get("s", "t", {"a": 1}) == "result"

    def test_cache_miss(self, proxy_cache: ProxyCache):
        assert proxy_cache.get("s", "t", {"a": 1}) is None

    def test_different_args_different_entries(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "r1", ttl_seconds=60.0)
        proxy_cache.set("s", "t", {"a": 2}, "r2", ttl_seconds=60.0)
        assert proxy_cache.get("s", "t", {"a": 1}) == "r1"
        assert proxy_cache.get("s", "t", {"a": 2}) == "r2"

    def test_update_existing_entry(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "old", ttl_seconds=60.0)
        proxy_cache.set("s", "t", {"a": 1}, "new", ttl_seconds=60.0)
        assert proxy_cache.get("s", "t", {"a": 1}) == "new"


class TestProxyCacheTTL:
    def test_expired_entry_returns_none(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "result", ttl_seconds=0.001)
        time.sleep(0.01)
        assert proxy_cache.get("s", "t", {"a": 1}) is None

    def test_no_ttl_never_expires(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "result", ttl_seconds=None)
        assert proxy_cache.get("s", "t", {"a": 1}) == "result"


class TestProxyCacheClear:
    def test_clear_all(self, proxy_cache: ProxyCache):
        proxy_cache.set("s1", "t1", {}, "r1", ttl_seconds=60.0)
        proxy_cache.set("s2", "t2", {}, "r2", ttl_seconds=60.0)
        removed = proxy_cache.clear()
        assert removed == 2
        assert proxy_cache.get("s1", "t1", {}) is None

    def test_clear_by_server(self, proxy_cache: ProxyCache):
        proxy_cache.set("s1", "t1", {}, "r1", ttl_seconds=60.0)
        proxy_cache.set("s2", "t2", {}, "r2", ttl_seconds=60.0)
        removed = proxy_cache.clear(server="s1")
        assert removed == 1
        assert proxy_cache.get("s2", "t2", {}) == "r2"

    def test_clear_by_server_and_tool(self, proxy_cache: ProxyCache):
        proxy_cache.set("s1", "t1", {}, "r1", ttl_seconds=60.0)
        proxy_cache.set("s1", "t2", {}, "r2", ttl_seconds=60.0)
        removed = proxy_cache.clear(server="s1", tool="t1")
        assert removed == 1
        assert proxy_cache.get("s1", "t2", {}) == "r2"

    def test_clear_by_tool_only(self, proxy_cache: ProxyCache):
        proxy_cache.set("s1", "t1", {}, "r1", ttl_seconds=60.0)
        proxy_cache.set("s2", "t1", {}, "r2", ttl_seconds=60.0)
        proxy_cache.set("s1", "t2", {}, "r3", ttl_seconds=60.0)
        removed = proxy_cache.clear(tool="t1")
        assert removed == 2
        assert proxy_cache.get("s1", "t1", {}) is None
        assert proxy_cache.get("s2", "t1", {}) is None
        assert proxy_cache.get("s1", "t2", {}) == "r3"


class TestProxyCacheEviction:
    def test_trim_evicts_oldest(self, tmp_path):
        cache = ProxyCache(tmp_path / "cache.db", max_entries=3)
        cache.initialize()
        try:
            for i in range(5):
                cache.set("s", "t", {"i": i}, f"r{i}", ttl_seconds=60.0)
            stats = cache.stats()
            assert stats["total_entries"] <= 3
        finally:
            cache.close()


class TestProxyCacheStats:
    def test_stats_counts(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "r", ttl_seconds=60.0)
        stats = proxy_cache.stats()
        assert stats["total_entries"] == 1
        assert stats["expired_entries"] == 0


class TestTransientKeyDetector:
    """``response_carries_transient_key`` must flag any response embedding a
    transient pending-store key (progressive first-chunk, SELECTIVE/HYBRID TOC)
    and leave key-free responses (plain text, truncate fallbacks) alone."""

    def test_plain_text_is_not_transient(self):
        assert not response_carries_transient_key("just a normal tool response")

    def test_progressive_footer_is_transient(self):
        text = f"chunk body{PROGRESSIVE_FOOTER_TOKEN}0-100/500] use stm_proxy_read_more"
        assert response_carries_transient_key(text)

    def test_selective_toc_is_transient(self):
        out = SelectiveCompressor().compress(_TOC_TEXT, max_chars=600)
        assert '"selection_key"' in out  # precondition: a key was minted
        assert response_carries_transient_key(out)

    def test_hybrid_fitted_toc_is_transient(self):
        # max_chars=550 drives HybridCompressor into _fit_toc_tail, which
        # abbreviates the call hint to ``select_chunks key=`` (dropping
        # ``stm_proxy_select_chunks(key=``) while keeping ``"selection_key"`` —
        # the regression that forced keying on the field, not the hint.
        out = HybridCompressor().compress(_TOC_TEXT, max_chars=550)
        assert '"selection_key"' in out
        assert "stm_proxy_select_chunks(key=" not in out
        assert response_carries_transient_key(out)

    def test_truncate_fallback_is_not_transient(self):
        # Tiny budget on unstructured text → plain truncation, no key minted.
        out = SelectiveCompressor().compress("x" * 5000, max_chars=200)
        assert '"selection_key"' not in out
        assert not response_carries_transient_key(out)

    def test_selection_key_field_without_toc_shape_is_not_transient(self):
        # A legit upstream JSON merely containing a ``selection_key`` field (but
        # not the SELECTIVE/HYBRID TOC shape) must NOT be misclassified — the
        # detector requires the ``selection_key`` + ``ttl_seconds_remaining`` pair.
        assert not response_carries_transient_key('{"selection_key": "abc", "data": 1}')

    def test_uppercase_markers_are_not_transient(self):
        # Detection is case-sensitive (matches the SQL ``instr`` purge, not LIKE).
        assert not response_carries_transient_key(
            '{"SELECTION_KEY": "x", "TTL_SECONDS_REMAINING": 1}'
        )


class TestLegacyTransientPurge:
    """``initialize`` purges pre-existing rows whose result embeds a transient
    key (they pre-date the store-side guard) while keeping normal rows."""

    def test_initialize_purges_legacy_transient_rows(self, tmp_path):
        db = tmp_path / "legacy.db"
        seed = ProxyCache(db, max_entries=100)
        seed.initialize()
        try:
            seed.set("s", "plain", {}, "ordinary cached response", ttl_seconds=None)
            seed.set(
                "s",
                "prog",
                {},
                f"chunk{PROGRESSIVE_FOOTER_TOKEN}0-9/99] more",
                ttl_seconds=None,
            )
            seed.set(
                "s",
                "sel",
                {},
                SelectiveCompressor().compress(_TOC_TEXT, max_chars=600),
                ttl_seconds=None,
            )
            seed.set(
                "s",
                "hyb",
                {},
                HybridCompressor().compress(_TOC_TEXT, max_chars=550),
                ttl_seconds=None,
            )
        finally:
            seed.close()

        # Re-open the SAME db: the startup purge runs in initialize().
        reopened = ProxyCache(db, max_entries=100)
        reopened.initialize()
        try:
            assert reopened.get("s", "plain", {}) == "ordinary cached response"
            assert reopened.get("s", "prog", {}) is None
            assert reopened.get("s", "sel", {}) is None
            assert reopened.get("s", "hyb", {}) is None
        finally:
            reopened.close()

    def test_initialize_keeps_non_toc_rows(self, tmp_path):
        # Precision + case-sensitivity guard: a legit response carrying a
        # ``selection_key`` field but NOT the TOC shape, and a case-variant, must
        # survive — the purge mirrors the case-sensitive, pair-requiring detector
        # (no over-broad LIKE / ASCII case-folding).
        db = tmp_path / "precision.db"
        seed = ProxyCache(db, max_entries=100)
        seed.initialize()
        field_row = '{"selection_key": "x", "data": 1}'
        upper_row = '{"SELECTION_KEY": "x", "TTL_SECONDS_REMAINING": 1}'
        try:
            seed.set("s", "field", {}, field_row, ttl_seconds=None)
            seed.set("s", "upper", {}, upper_row, ttl_seconds=None)
        finally:
            seed.close()
        reopened = ProxyCache(db, max_entries=100)
        reopened.initialize()
        try:
            assert reopened.get("s", "field", {}) == field_row
            assert reopened.get("s", "upper", {}) == upper_row
        finally:
            reopened.close()


class TestMakeKey:
    def test_deterministic(self):
        k1 = _make_key("s", "t", {"a": 1, "b": 2})
        k2 = _make_key("s", "t", {"a": 1, "b": 2})
        assert k1 == k2

    def test_arg_order_independent(self):
        k1 = _make_key("s", "t", {"a": 1, "b": 2})
        k2 = _make_key("s", "t", {"b": 2, "a": 1})
        assert k1 == k2

    def test_different_args_different_keys(self):
        k1 = _make_key("s", "t", {"a": 1})
        k2 = _make_key("s", "t", {"a": 2})
        assert k1 != k2
