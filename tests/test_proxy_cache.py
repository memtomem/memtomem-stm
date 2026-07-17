"""Tests for ProxyCache — SQLite response cache."""

from __future__ import annotations

import sqlite3
import time

import pytest

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

    def test_envelope_round_trip(self, proxy_cache: ProxyCache):
        proxy_cache.set(
            "s",
            "t",
            {},
            "body",
            ttl_seconds=60.0,
            structured_content={"answer": 42},
            meta={"trace": "safe"},
        )
        cached = proxy_cache.get("s", "t", {})
        assert cached == "body"
        assert cached.structured_content == {"answer": 42}
        assert cached.meta == {"trace": "safe"}

    def test_non_json_envelope_is_not_stored(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {}, "body", ttl_seconds=60.0, meta={"bad": object()})
        assert proxy_cache.get("s", "t", {}) is None

    def test_sensitive_envelope_is_not_stored(self, proxy_cache: ProxyCache):
        proxy_cache.set(
            "s",
            "t",
            {},
            "body",
            ttl_seconds=60.0,
            meta={"api_key": "abc123-def"},
        )
        assert proxy_cache.get("s", "t", {}) is None


class TestProxyCacheDegradation:
    def test_get_degrades_to_miss_on_sqlite_error(self, tmp_path, caplog):
        """A lookup fault (disk I/O error, page corruption mid-session, a
        writer holding the file past the busy timeout) must degrade to a
        plain MISS instead of raising out of the request path — the cache
        is optional and must never fail an otherwise-healthy proxied call."""
        cache = ProxyCache(tmp_path / "c.db", max_entries=10)
        cache.initialize()
        cache.set("s", "t", {"a": 1}, "result", ttl_seconds=60.0)
        assert cache.get("s", "t", {"a": 1}) == "result"  # sanity

        real_db = cache._db

        class _BoomDB:
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("disk I/O error")

        cache._db = _BoomDB()
        try:
            with caplog.at_level("WARNING", logger="memtomem_stm.proxy.cache"):
                assert cache.get("s", "t", {"a": 1}) is None
            assert any("Cache lookup failed" in r.getMessage() for r in caplog.records)
        finally:
            cache._db = real_db
            cache.close()


class TestProxyCacheTTL:
    def test_expired_entry_returns_none(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "result", ttl_seconds=0.001)
        time.sleep(0.01)
        assert proxy_cache.get("s", "t", {"a": 1}) is None

    def test_no_ttl_never_expires(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "result", ttl_seconds=None)
        assert proxy_cache.get("s", "t", {"a": 1}) == "result"

    def test_zero_ttl_does_not_store(self, proxy_cache: ProxyCache):
        # A ttl of 0 would make every row born-expired; the store short-circuits
        # so it does not burn write+trim I/O for a guaranteed miss.
        proxy_cache.set("s", "t", {"a": 1}, "result", ttl_seconds=0)
        assert proxy_cache.get("s", "t", {"a": 1}) is None
        assert proxy_cache.stats()["total_entries"] == 0

    def test_negative_ttl_does_not_store(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "result", ttl_seconds=-5.0)
        assert proxy_cache.get("s", "t", {"a": 1}) is None
        assert proxy_cache.stats()["total_entries"] == 0

    def test_zero_ttl_invalidates_existing_live_row(self, proxy_cache: ProxyCache):
        # A later ttl<=0 store must not leave a previously-cached LIVE row serving
        # stale content — it invalidates the key, matching the old behavior of
        # overwriting it with a born-expired row.
        proxy_cache.set("s", "t", {"a": 1}, "old", ttl_seconds=60.0)
        assert proxy_cache.get("s", "t", {"a": 1}) == "old"
        proxy_cache.set("s", "t", {"a": 1}, "new", ttl_seconds=0)
        assert proxy_cache.get("s", "t", {"a": 1}) is None
        assert proxy_cache.stats()["total_entries"] == 0


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

    def test_eviction_counter_tracks_trim(self, tmp_path):
        cache = ProxyCache(tmp_path / "cache.db", max_entries=3)
        cache.initialize()
        try:
            for i in range(5):
                cache.set("s", "t", {"i": i}, f"r{i}", ttl_seconds=60.0)
            # 5 inserts past a cap of 3 → 2 rows evicted, surfaced in stats.
            assert cache.stats()["evictions"] == 2
        finally:
            cache.close()


class TestProxyCacheStats:
    def test_stats_counts(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "r", ttl_seconds=60.0)
        stats = proxy_cache.stats()
        assert stats["total_entries"] == 1
        assert stats["expired_entries"] == 0
        assert stats["evictions"] == 0


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

    def test_single_chunk_selective_is_transient_and_retrievable(self):
        # SELECTIVE remains lossless even for one unstructured chunk.
        out = SelectiveCompressor().compress("x" * 5000, max_chars=200)
        assert '"selection_key"' in out
        assert response_carries_transient_key(out)

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


class TestPrivacyGate:
    """``set()`` refuses secret-looking results (SECURITY.md exclusion, #453)
    and ``initialize`` purges matching rows that pre-date the gate."""

    @pytest.mark.parametrize(
        "secret_text",
        [
            "api_key: abc123-def",
            "password=hunter2",
            "token sk-" + "a" * 24,
            "creds AKIA" + "B" * 16 + " end",
            "-----BEGIN RSA PRIVATE KEY-----",
        ],
    )
    def test_set_skips_secret_bearing_result(self, proxy_cache: ProxyCache, secret_text: str):
        proxy_cache.set("s", "t", {}, secret_text, ttl_seconds=60.0)
        assert proxy_cache.get("s", "t", {}) is None

    def test_set_stores_clean_result(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {}, "ordinary tool response", ttl_seconds=60.0)
        assert proxy_cache.get("s", "t", {}) == "ordinary tool response"

    def test_email_bearing_result_is_not_cached(self, proxy_cache: ProxyCache):
        # The cache is a STORAGE consumer, so it scans the full
        # DEFAULT_PATTERNS (credentials + PII) per the #461 split: an email
        # is fine to show an external summarizer but not fine to persist.
        # The cost is one pipeline re-run per repeat call, never correctness.
        proxy_cache.set("s", "t", {}, "contact: dev@example.com", ttl_seconds=60.0)
        assert proxy_cache.get("s", "t", {}) is None

    def test_initialize_purges_legacy_secret_rows(self, tmp_path):
        db_path = tmp_path / "legacy_secrets.db"
        seed = ProxyCache(db_path, max_entries=100)
        seed.initialize()
        try:
            seed.set("s", "plain", {}, "ordinary cached response", ttl_seconds=None)
        finally:
            seed.close()
        # Seed the secret row via raw SQL: it models a row written by a
        # pre-gate release — ``set()`` itself now refuses such content.
        raw = sqlite3.connect(str(db_path))
        try:
            raw.execute(
                "INSERT INTO proxy_cache "
                "(cache_key, server, tool, result, created_at, ttl_seconds) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _make_key("s", "sec", {}),
                    "s",
                    "sec",
                    "login password=hunter2",
                    time.time(),
                    None,
                ),
            )
            raw.commit()
        finally:
            raw.close()

        # Re-open the SAME db: the startup purge runs in initialize().
        reopened = ProxyCache(db_path, max_entries=100)
        reopened.initialize()
        try:
            assert reopened.get("s", "sec", {}) is None
            assert reopened.get("s", "plain", {}) == "ordinary cached response"
        finally:
            reopened.close()

    def test_get_refuses_and_evicts_sensitive_row_written_after_startup(self, tmp_path):
        # An older still-running pre-gate process (or an external SQL writer)
        # can insert AFTER this process's startup purge ran — the read-side
        # guard must refuse to serve the row and evict it.
        db_path = tmp_path / "live_writer.db"
        cache = ProxyCache(db_path, max_entries=100)
        cache.initialize()
        try:
            raw = sqlite3.connect(str(db_path))
            try:
                raw.execute(
                    "INSERT INTO proxy_cache "
                    "(cache_key, server, tool, result, created_at, ttl_seconds) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _make_key("s", "sec", {}),
                        "s",
                        "sec",
                        "login password=hunter2",
                        time.time(),
                        None,
                    ),
                )
                raw.commit()
            finally:
                raw.close()

            assert cache.get("s", "sec", {}) is None

            # Evicted, not just hidden: the row is gone from the table.
            check = sqlite3.connect(str(db_path))
            try:
                count = check.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0]
            finally:
                check.close()
            assert count == 0
        finally:
            cache.close()

    def test_get_evicts_expired_sensitive_row(self, tmp_path):
        # The sensitivity check must run BEFORE the expiry check: an
        # already-expired sensitive row would otherwise return a miss while
        # the secret keeps resting on disk until the next startup purge.
        db_path = tmp_path / "expired_secret.db"
        cache = ProxyCache(db_path, max_entries=100)
        cache.initialize()
        try:
            raw = sqlite3.connect(str(db_path))
            try:
                raw.execute(
                    "INSERT INTO proxy_cache "
                    "(cache_key, server, tool, result, created_at, ttl_seconds) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _make_key("s", "sec", {}),
                        "s",
                        "sec",
                        "login password=hunter2",
                        time.time() - 100.0,
                        1.0,
                    ),
                )
                raw.commit()
            finally:
                raw.close()

            assert cache.get("s", "sec", {}) is None

            check = sqlite3.connect(str(db_path))
            try:
                count = check.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0]
            finally:
                check.close()
            assert count == 0
        finally:
            cache.close()

    def test_get_serves_miss_when_eviction_is_blocked(self, tmp_path):
        # A concurrent writer holding the SQLite write lock makes the
        # privacy-eviction DELETE raise OperationalError; get() must degrade
        # to a plain miss (never propagate), keep the row for a later sweep,
        # and evict it once the writer releases the lock.
        db_path = tmp_path / "blocked_eviction.db"
        cache = ProxyCache(db_path, max_entries=100)
        cache.initialize()
        try:
            assert cache._db is not None
            # Shrink the busy timeout so the blocked DELETE fails fast
            # instead of stalling the test for the 3 s default.
            cache._db.execute("PRAGMA busy_timeout=50")

            raw = sqlite3.connect(str(db_path))
            try:
                raw.execute(
                    "INSERT INTO proxy_cache "
                    "(cache_key, server, tool, result, created_at, ttl_seconds) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _make_key("s", "sec", {}),
                        "s",
                        "sec",
                        "login password=hunter2",
                        time.time(),
                        None,
                    ),
                )
                raw.commit()

                raw.execute("BEGIN IMMEDIATE")  # hold the write lock
                assert cache.get("s", "sec", {}) is None  # miss, no raise
                count = raw.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0]
                assert count == 1  # eviction failed, row still present
                raw.rollback()  # release the lock
            finally:
                raw.close()

            assert cache.get("s", "sec", {}) is None  # retried eviction
            check = sqlite3.connect(str(db_path))
            try:
                count = check.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0]
            finally:
                check.close()
            assert count == 0
        finally:
            cache.close()


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

    def test_context_query_changes_key(self):
        # The stored body is the COMPRESSED response and compression is
        # query-aware (BM25 budgets), so two calls that differ only in query
        # context must never share a row.
        base = _make_key("s", "t", {"a": 1})
        q1 = _make_key("s", "t", {"a": 1}, context_query="find auth code")
        q2 = _make_key("s", "t", {"a": 1}, context_query="billing systems")
        assert len({base, q1, q2}) == 3

    def test_context_query_none_empty_and_null_string_distinct(self):
        # JSON-encoding the query keeps the "no query" sentinel distinct from
        # both the empty string and the literal string "null".
        absent = _make_key("s", "t", {})
        empty = _make_key("s", "t", {}, context_query="")
        null_str = _make_key("s", "t", {}, context_query="null")
        assert len({absent, empty, null_str}) == 3

    def test_config_fingerprint_changes_key(self):
        fp_a = _make_key("s", "t", {"a": 1}, config_fingerprint="aaa")
        fp_b = _make_key("s", "t", {"a": 1}, config_fingerprint="bbb")
        default = _make_key("s", "t", {"a": 1})
        assert len({fp_a, fp_b, default}) == 3

    def test_same_components_same_key(self):
        k1 = _make_key("s", "t", {"a": 1}, context_query="q", config_fingerprint="fp")
        k2 = _make_key("s", "t", {"a": 1}, context_query="q", config_fingerprint="fp")
        assert k1 == k2


class TestKeyComponentsRoundtrip:
    def test_same_args_different_query_are_separate_entries(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "for-q1", ttl_seconds=60.0, context_query="q1")
        proxy_cache.set("s", "t", {"a": 1}, "for-q2", ttl_seconds=60.0, context_query="q2")
        assert proxy_cache.get("s", "t", {"a": 1}, context_query="q1") == "for-q1"
        assert proxy_cache.get("s", "t", {"a": 1}, context_query="q2") == "for-q2"
        assert proxy_cache.get("s", "t", {"a": 1}) is None  # no-query key untouched

    def test_fingerprint_mismatch_is_a_miss(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "old-config", ttl_seconds=60.0, config_fingerprint="v1")
        assert proxy_cache.get("s", "t", {"a": 1}, config_fingerprint="v2") is None
        assert proxy_cache.get("s", "t", {"a": 1}, config_fingerprint="v1") == "old-config"

    def test_invalidate_targets_matching_components_only(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "for-q1", ttl_seconds=60.0, context_query="q1")
        proxy_cache.set("s", "t", {"a": 1}, "for-q2", ttl_seconds=60.0, context_query="q2")
        proxy_cache.invalidate("s", "t", {"a": 1}, context_query="q1")
        assert proxy_cache.get("s", "t", {"a": 1}, context_query="q1") is None
        assert proxy_cache.get("s", "t", {"a": 1}, context_query="q2") == "for-q2"

    def test_ttl_zero_self_heal_deletes_matching_component_row(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "live", ttl_seconds=60.0, context_query="q1")
        # ttl<=0 is do-not-store but must delete the existing row for the SAME
        # key components.
        proxy_cache.set("s", "t", {"a": 1}, "ignored", ttl_seconds=0.0, context_query="q1")
        assert proxy_cache.get("s", "t", {"a": 1}, context_query="q1") is None


class TestKeySchemaVersionPurge:
    def test_pre_versioning_rows_are_purged_once(self, tmp_path):
        """Rows written before the key-schema bump are opaque hashes no current
        lookup can produce — dead weight that never expires for ``ttl NULL``
        rows. ``initialize()`` purges them exactly once via ``user_version``."""
        db_path = tmp_path / "c.db"
        cache = ProxyCache(db_path, max_entries=10)
        cache.initialize()
        # Simulate a legacy database: rows present but user_version still 0.
        cache.set("s", "t", {"a": 1}, "v1-row", ttl_seconds=None)
        cache._db.execute("PRAGMA user_version = 0")
        cache._db.commit()
        cache.close()

        reopened = ProxyCache(db_path, max_entries=10)
        reopened.initialize()
        try:
            assert reopened.stats()["total_entries"] == 0  # legacy rows wiped
            (version,) = reopened._db.execute("PRAGMA user_version").fetchone()
            assert version == 4
        finally:
            reopened.close()

    def test_current_version_rows_survive_reopen(self, tmp_path):
        db_path = tmp_path / "c.db"
        cache = ProxyCache(db_path, max_entries=10)
        cache.initialize()  # stamps user_version = 3
        cache.set("s", "t", {"a": 1}, "row", ttl_seconds=None)
        cache.close()

        reopened = ProxyCache(db_path, max_entries=10)
        reopened.initialize()
        try:
            assert reopened.get("s", "t", {"a": 1}) == "row"
        finally:
            reopened.close()

    def test_v2_table_reopen_wipes_and_adds_envelope_columns(self, tmp_path):
        """A v2-era database (old six-column table, user_version=2) is dropped
        and recreated on open: rows are gone and the recreated table carries
        the ``envelope_safe`` column."""
        db_path = tmp_path / "c.db"
        db = sqlite3.connect(str(db_path))
        db.execute(
            """
            CREATE TABLE proxy_cache (
                cache_key   TEXT    PRIMARY KEY,
                server      TEXT    NOT NULL,
                tool        TEXT    NOT NULL,
                result      TEXT    NOT NULL,
                created_at  REAL    NOT NULL,
                ttl_seconds REAL
            )
            """
        )
        db.execute(
            "INSERT INTO proxy_cache VALUES ('k', 's', 't', 'v2-row', ?, NULL)",
            (time.time(),),
        )
        db.execute("PRAGMA user_version = 2")
        db.commit()
        db.close()

        cache = ProxyCache(db_path, max_entries=10)
        cache.initialize()
        try:
            assert cache.stats()["total_entries"] == 0
            columns = {
                row[1] for row in cache._db.execute("PRAGMA table_info(proxy_cache)").fetchall()
            }
            assert "envelope_safe" in columns
            assert "envelope_json" in columns
            (version,) = cache._db.execute("PRAGMA user_version").fetchone()
            assert version == 4
        finally:
            cache.close()


class TestEnvelopeSafeMarker:
    """v3 rows carry ``envelope_safe=1``; unmarked rows are never served."""

    def _insert_unmarked_row(self, cache: ProxyCache, key: str, result: str) -> None:
        # Simulate an out-of-band writer (older binary / external SQL) that
        # names the pre-v3 columns only, leaving envelope_safe at DEFAULT 0.
        cache._db.execute(
            "INSERT INTO proxy_cache (cache_key, server, tool, result, created_at, ttl_seconds) "
            "VALUES (?, 's', 't', ?, ?, NULL)",
            (key, result, time.time()),
        )
        cache._db.commit()

    def test_set_get_round_trip_serves_marked_rows(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "marked", ttl_seconds=60.0)
        assert proxy_cache.get("s", "t", {"a": 1}) == "marked"
        (flag,) = proxy_cache._db.execute("SELECT envelope_safe FROM proxy_cache").fetchone()
        assert flag == 1

    def test_unmarked_row_is_a_miss(self, proxy_cache: ProxyCache):
        key = _make_key("s", "t", {"a": 1})
        self._insert_unmarked_row(proxy_cache, key, "unmarked")
        assert proxy_cache.get("s", "t", {"a": 1}) is None

    def test_set_over_unmarked_row_re_marks_it(self, proxy_cache: ProxyCache):
        """The UPSERT's conflict branch must re-mark the row: without
        ``envelope_safe = excluded.envelope_safe`` an unmarked row upserted by
        ``set()`` would keep the 0 marker and miss forever."""
        key = _make_key("s", "t", {"a": 1})
        self._insert_unmarked_row(proxy_cache, key, "unmarked")
        proxy_cache.set("s", "t", {"a": 1}, "re-marked", ttl_seconds=60.0)
        assert proxy_cache.get("s", "t", {"a": 1}) == "re-marked"
