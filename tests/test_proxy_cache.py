"""Tests for ProxyCache — SQLite response cache."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from memtomem_stm.proxy import cache as cache_module
from memtomem_stm.proxy.cache import (
    ProxyCache,
    _make_key,
    _privacy_policy_fingerprint,
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


def _read_privacy_policy_stamp(db_path: Path) -> str | None:
    db = sqlite3.connect(str(db_path))
    try:
        row = db.execute(
            "SELECT value FROM proxy_cache_meta WHERE key = ?",
            (cache_module._PRIVACY_POLICY_FINGERPRINT_KEY,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return None
    finally:
        db.close()
    return str(row[0]) if row is not None else None


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

    @pytest.mark.parametrize(
        "bad_envelope",
        ["{not json", '{"schema_version": 99}', '{"schema_version": 1, "_meta": "not-a-dict"}'],
        ids=["unparseable", "unknown-schema", "non-dict-field"],
    )
    def test_malformed_envelope_row_is_evicted(self, proxy_cache: ProxyCache, bad_envelope):
        """An out-of-band envelope write must be evicted, not just missed.

        ``set()`` validates before writing, so a malformed ``envelope_json``
        can only come from an external SQL writer. Returning a plain miss
        would leave the row as dead weight — immortal for ``ttl_seconds
        NULL`` rows — still counting against ``max_entries``; mirror the
        sensitive-row read-side eviction instead.
        """
        proxy_cache.set("s", "t", {}, "body", ttl_seconds=60.0, structured_content={"a": 1})
        proxy_cache._db.execute("UPDATE proxy_cache SET envelope_json = ?", (bad_envelope,))
        proxy_cache._db.commit()

        assert proxy_cache.get("s", "t", {}) is None
        assert proxy_cache.stats()["total_entries"] == 0


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

    def test_initialize_stamps_current_privacy_policy(self, tmp_path):
        db_path = tmp_path / "privacy_stamp.db"
        cache = ProxyCache(db_path, max_entries=100)
        cache.initialize()
        cache.close()

        assert _read_privacy_policy_stamp(db_path) == _privacy_policy_fingerprint()

    def test_matching_stamp_skips_nonempty_body_query_and_scan(self, tmp_path, monkeypatch):
        """The #872 hot path must be real, not an empty-cache false pass."""
        db_path = tmp_path / "warm_privacy_stamp.db"
        seed = ProxyCache(db_path, max_entries=100)
        seed.initialize()
        try:
            seed.set("s", "clean", {}, "ordinary cached response", ttl_seconds=None)
            assert seed.stats()["total_entries"] == 1  # positive control
            plan = seed._db.execute(
                f"EXPLAIN QUERY PLAN {cache_module._SELECT_UNVERIFIED_PRIVACY_ROWS}"
            ).fetchall()
            assert not any("SCAN P" in str(row[3]).upper() for row in plan)
        finally:
            seed.close()

        statements: list[str] = []
        scan_calls: list[str] = []
        real_connect = sqlite3.connect

        def _traced_connect(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            db = real_connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db

        def _scan_spy(text: str) -> bool:
            scan_calls.append(text)
            return False

        monkeypatch.setattr(cache_module.sqlite3, "connect", _traced_connect)
        monkeypatch.setattr(cache_module.privacy, "contains_sensitive_content", _scan_spy)

        reopened = ProxyCache(db_path, max_entries=100)
        reopened.initialize()
        reopened.close()

        normalized = [" ".join(statement.split()) for statement in statements]
        assert not any(
            statement.startswith("SELECT cache_key, result, envelope_json FROM proxy_cache")
            for statement in normalized
        )
        assert any(
            "SELECT cache_key FROM proxy_cache_privacy_unverified" in statement
            for statement in normalized
        )
        assert scan_calls == []

    @pytest.mark.parametrize("stamp_state", ["missing", "stale"])
    def test_missing_or_stale_stamp_rescans_result_and_envelope(self, tmp_path, stamp_state):
        db_path = tmp_path / f"privacy_{stamp_state}.db"
        seed = ProxyCache(db_path, max_entries=100)
        seed.initialize()
        try:
            seed.set("s", "clean", {}, "ordinary cached response", ttl_seconds=None)
        finally:
            seed.close()

        raw = sqlite3.connect(str(db_path))
        try:
            raw.executemany(
                "INSERT INTO proxy_cache "
                "(cache_key, server, tool, result, created_at, ttl_seconds, "
                "envelope_json, envelope_safe) VALUES (?, 's', ?, ?, ?, NULL, ?, 1)",
                [
                    (
                        _make_key("s", "result-secret", {}),
                        "result-secret",
                        "login password=hunter2",
                        time.time(),
                        None,
                    ),
                    (
                        _make_key("s", "envelope-secret", {}),
                        "envelope-secret",
                        "ordinary body",
                        time.time(),
                        '{"schema_version":1,"_meta":{"password":"hunter2"}}',
                    ),
                ],
            )
            if stamp_state == "missing":
                raw.execute(
                    "DELETE FROM proxy_cache_meta WHERE key = ?",
                    (cache_module._PRIVACY_POLICY_FINGERPRINT_KEY,),
                )
            else:
                raw.execute(
                    "UPDATE proxy_cache_meta SET value = 'stale' WHERE key = ?",
                    (cache_module._PRIVACY_POLICY_FINGERPRINT_KEY,),
                )
            raw.commit()
        finally:
            raw.close()

        reopened = ProxyCache(db_path, max_entries=100)
        reopened.initialize()
        try:
            remaining = {
                row[0] for row in reopened._db.execute("SELECT tool FROM proxy_cache").fetchall()
            }
            assert remaining == {"clean"}
        finally:
            reopened.close()

        assert _read_privacy_policy_stamp(db_path) == _privacy_policy_fingerprint()

    def test_added_pattern_changes_fingerprint_and_forces_rescan(self, tmp_path, monkeypatch):
        db_path = tmp_path / "changed_privacy_policy.db"
        marker = "policy-only-marker-872"
        seed = ProxyCache(db_path, max_entries=100)
        seed.initialize()
        try:
            seed.set("s", "new-secret", {}, f"contains {marker}", ttl_seconds=None)
            assert seed.stats()["total_entries"] == 1
        finally:
            seed.close()

        old_fingerprint = _privacy_policy_fingerprint()
        monkeypatch.setattr(
            cache_module.privacy,
            "DEFAULT_PATTERNS",
            [*cache_module.privacy.DEFAULT_PATTERNS, re.escape(marker)],
        )
        assert _privacy_policy_fingerprint() != old_fingerprint

        reopened = ProxyCache(db_path, max_entries=100)
        reopened.initialize()
        try:
            assert reopened._db.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0] == 0
        finally:
            reopened.close()
        assert _read_privacy_policy_stamp(db_path) == _privacy_policy_fingerprint()

    def test_stamp_failure_rolls_back_deletions_and_retries(self, tmp_path, monkeypatch):
        db_path = tmp_path / "privacy_stamp_failure.db"
        seed = ProxyCache(db_path, max_entries=100)
        seed.initialize()
        seed.close()

        raw = sqlite3.connect(str(db_path))
        try:
            raw.execute(
                "INSERT INTO proxy_cache "
                "(cache_key, server, tool, result, created_at, ttl_seconds) "
                "VALUES (?, 's', 'secret', 'password=hunter2', ?, NULL)",
                (_make_key("s", "secret", {}), time.time()),
            )
            raw.execute(
                "UPDATE proxy_cache_meta SET value = 'stale' WHERE key = ?",
                (cache_module._PRIVACY_POLICY_FINGERPRINT_KEY,),
            )
            raw.commit()
        finally:
            raw.close()

        real_connect = sqlite3.connect

        class _StampFailsConnection:
            def __init__(self, real: sqlite3.Connection) -> None:
                self._real = real

            def execute(self, sql: str, *args):  # noqa: ANN002, ANN202
                if sql.lstrip().startswith("INSERT INTO proxy_cache_meta"):
                    raise sqlite3.OperationalError("disk I/O error")
                return self._real.execute(sql, *args)

            def __getattr__(self, name: str):  # noqa: ANN202
                return getattr(self._real, name)

        with monkeypatch.context() as patch:
            patch.setattr(
                cache_module.sqlite3,
                "connect",
                lambda *args, **kwargs: _StampFailsConnection(real_connect(*args, **kwargs)),
            )
            with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
                ProxyCache(db_path, max_entries=100).initialize()

        assert _read_privacy_policy_stamp(db_path) == "stale"
        check = sqlite3.connect(str(db_path))
        try:
            assert check.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0] == 1
        finally:
            check.close()

        recovered = ProxyCache(db_path, max_entries=100)
        recovered.initialize()
        try:
            assert recovered._db.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0] == 0
        finally:
            recovered.close()
        assert _read_privacy_policy_stamp(db_path) == _privacy_policy_fingerprint()

    def test_matching_stamp_purges_secret_inserted_by_legacy_writer(self, tmp_path):
        db_path = tmp_path / "legacy_secrets.db"
        bootstrap = sqlite3.connect(str(db_path))
        try:
            bootstrap.execute(cache_module._CREATE_TABLE)
            bootstrap.execute(f"PRAGMA user_version = {cache_module._KEY_SCHEMA_VERSION}")
            bootstrap.commit()
        finally:
            bootstrap.close()

        # Open and prime the legacy connection before the new initializer
        # installs its triggers, matching a still-running pre-gate process.
        legacy_writer = sqlite3.connect(str(db_path))
        try:
            assert legacy_writer.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0] == 0

            seed = ProxyCache(db_path, max_entries=100)
            seed.initialize()
            try:
                seed.set("s", "plain", {}, "ordinary cached response", ttl_seconds=None)
            finally:
                seed.close()

            # This writer knows neither the policy stamp nor the unverified-key
            # queue, but the database trigger still observes its insert.
            legacy_writer.execute(
                "INSERT INTO proxy_cache "
                "(cache_key, server, tool, result, created_at, ttl_seconds) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "legacy-opaque-key",
                    "s",
                    "sec",
                    "login password=hunter2",
                    time.time(),
                    None,
                ),
            )
            legacy_writer.commit()
        finally:
            legacy_writer.close()

        # Re-open the SAME db: the startup purge runs in initialize().
        reopened = ProxyCache(db_path, max_entries=100)
        reopened.initialize()
        try:
            remaining = {
                row[0] for row in reopened._db.execute("SELECT tool FROM proxy_cache").fetchall()
            }
            assert remaining == {"plain"}  # proved before ``get`` can evict
            assert reopened.get("s", "sec", {}) is None
            assert reopened.get("s", "plain", {}) == "ordinary cached response"
            assert (
                reopened._db.execute(
                    "SELECT COUNT(*) FROM proxy_cache_privacy_unverified"
                ).fetchone()[0]
                == 0
            )
        finally:
            reopened.close()

    def test_matching_stamp_purges_verified_row_overwritten_by_legacy_writer(self, tmp_path):
        db_path = tmp_path / "legacy_update.db"
        seed = ProxyCache(db_path, max_entries=100)
        seed.initialize()
        try:
            seed.set("s", "t", {}, "ordinary cached response", ttl_seconds=None)
        finally:
            seed.close()

        raw = sqlite3.connect(str(db_path))
        try:
            raw.execute("UPDATE proxy_cache SET result = 'login password=hunter2' WHERE tool = 't'")
            raw.commit()
        finally:
            raw.close()

        reopened = ProxyCache(db_path, max_entries=100)
        reopened.initialize()
        try:
            assert reopened._db.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0] == 0
        finally:
            reopened.close()

    def test_matching_stamp_checks_clean_legacy_row_only_once(self, tmp_path, monkeypatch):
        db_path = tmp_path / "clean_legacy_insert.db"
        seed = ProxyCache(db_path, max_entries=100)
        seed.initialize()
        seed.close()

        raw = sqlite3.connect(str(db_path))
        try:
            raw.execute(
                "INSERT INTO proxy_cache "
                "(cache_key, server, tool, result, created_at, ttl_seconds) "
                "VALUES ('legacy-key', 's', 't', 'ordinary legacy response', ?, NULL)",
                (time.time(),),
            )
            raw.commit()
        finally:
            raw.close()

        scan_calls: list[str] = []
        real_scan = cache_module.privacy.contains_sensitive_content

        def _scan_spy(text: str) -> bool:
            scan_calls.append(text)
            return real_scan(text)

        monkeypatch.setattr(cache_module.privacy, "contains_sensitive_content", _scan_spy)

        first = ProxyCache(db_path, max_entries=100)
        first.initialize()
        first.close()
        assert scan_calls == ["ordinary legacy response"]

        scan_calls.clear()
        second = ProxyCache(db_path, max_entries=100)
        second.initialize()
        second.close()
        assert scan_calls == []

    def test_current_writer_dequeues_its_verified_row(self, tmp_path):
        db_path = tmp_path / "current_writer.db"
        cache = ProxyCache(db_path, max_entries=100)
        cache.initialize()
        try:
            cache.set("s", "t", {}, "ordinary cached response", ttl_seconds=None)
            assert (
                cache._db.execute("SELECT COUNT(*) FROM proxy_cache_privacy_unverified").fetchone()[
                    0
                ]
                == 0
            )
        finally:
            cache.close()

    def test_older_writer_leaves_row_queued_under_newer_policy_stamp(self, tmp_path, monkeypatch):
        db_path = tmp_path / "rolling_policy_upgrade.db"
        future_policy_fingerprint = "future-policy-fingerprint"
        future_secret = "future-policy-sensitive-marker"
        older_writer = ProxyCache(db_path, max_entries=100)
        older_writer.initialize()
        try:
            older_writer._db.execute(
                "UPDATE proxy_cache_meta SET value = ? WHERE key = ?",
                (future_policy_fingerprint, cache_module._PRIVACY_POLICY_FINGERPRINT_KEY),
            )
            older_writer._db.commit()

            # The old policy accepts this body, but its trigger-created queue
            # entry must survive because a newer process owns the published
            # stamp and may classify the body differently.
            older_writer.set("s", "t", {}, future_secret, ttl_seconds=None)
            assert (
                older_writer._db.execute(
                    "SELECT COUNT(*) FROM proxy_cache_privacy_unverified"
                ).fetchone()[0]
                == 1
            )
        finally:
            older_writer.close()

        monkeypatch.setattr(
            cache_module, "_privacy_policy_fingerprint", lambda: future_policy_fingerprint
        )
        monkeypatch.setattr(
            cache_module.privacy,
            "contains_sensitive_content",
            lambda text: future_secret in text,
        )

        newer_process = ProxyCache(db_path, max_entries=100)
        newer_process.initialize()
        try:
            assert newer_process._db.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0] == 0
            assert (
                newer_process._db.execute(
                    "SELECT COUNT(*) FROM proxy_cache_privacy_unverified"
                ).fetchone()[0]
                == 0
            )
        finally:
            newer_process.close()

    def test_stale_policy_scan_reserves_writer_before_publishing_stamp(self, tmp_path, monkeypatch):
        db_path = tmp_path / "privacy_scan_writer_race.db"
        seed = ProxyCache(db_path, max_entries=100)
        seed.initialize()
        try:
            seed.set("s", "clean", {}, "ordinary cached response", ttl_seconds=None)
        finally:
            seed.close()

        raw = sqlite3.connect(str(db_path))
        try:
            raw.execute(
                "UPDATE proxy_cache_meta SET value = 'stale' WHERE key = ?",
                (cache_module._PRIVACY_POLICY_FINGERPRINT_KEY,),
            )
            raw.commit()
        finally:
            raw.close()

        scan_started = threading.Event()
        release_scan = threading.Event()
        writer_started = threading.Event()
        writer_committed = threading.Event()
        errors: list[BaseException] = []
        real_scan = cache_module.privacy.contains_sensitive_content

        def _blocking_scan(text: str) -> bool:
            if text == "ordinary cached response":
                scan_started.set()
                assert release_scan.wait(timeout=5.0)
            return real_scan(text)

        def _initialize() -> None:
            cache = ProxyCache(db_path, max_entries=100)
            try:
                cache.initialize()
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                cache.close()

        def _legacy_write() -> None:
            writer = sqlite3.connect(str(db_path), timeout=5.0)
            try:
                writer_started.set()
                writer.execute(
                    "INSERT INTO proxy_cache "
                    "(cache_key, server, tool, result, created_at, ttl_seconds) "
                    "VALUES ('racing-legacy-key', 's', 'secret', "
                    "'password=hunter2', ?, NULL)",
                    (time.time(),),
                )
                writer.commit()
                writer_committed.set()
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                writer.close()

        monkeypatch.setattr(cache_module.privacy, "contains_sensitive_content", _blocking_scan)
        initializer = threading.Thread(target=_initialize)
        initializer.start()
        assert scan_started.wait(timeout=5.0)

        legacy_writer = threading.Thread(target=_legacy_write)
        legacy_writer.start()
        assert writer_started.wait(timeout=5.0)
        assert not writer_committed.wait(timeout=0.1)

        release_scan.set()
        initializer.join(timeout=5.0)
        legacy_writer.join(timeout=5.0)
        assert not initializer.is_alive()
        assert not legacy_writer.is_alive()
        assert errors == []
        assert writer_committed.is_set()
        assert _read_privacy_policy_stamp(db_path) == _privacy_policy_fingerprint()

        check = sqlite3.connect(str(db_path))
        try:
            assert check.execute(
                "SELECT cache_key FROM proxy_cache_privacy_unverified"
            ).fetchall() == [("racing-legacy-key",)]
        finally:
            check.close()

        reopened = ProxyCache(db_path, max_entries=100)
        reopened.initialize()
        try:
            assert reopened._db.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()[0] == 1
            assert reopened._db.execute("SELECT tool FROM proxy_cache").fetchone() == ("clean",)
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

    def test_identically_serialized_args_share_a_row_on_purpose(self):
        """The key is injective over the SERIALIZED tuple, not Python identity.

        Two argument trees that ``json.dumps`` renders identically map to one
        key by design, because that same rendering is what goes out to the
        upstream tool: it cannot tell these pairs apart and owes them the same
        response. Splitting them would split rows that must not be split, so
        this is pinned as intended behavior rather than left to read as a gap
        in the framing above.
        """
        pairs = [
            ({"x": (1, 2)}, {"x": [1, 2]}),  # both serialize to [1, 2]
            ({"x": {1: "a"}}, {"x": {"1": "a"}}),  # int keys coerce to strings
        ]
        for left, right in pairs:
            assert json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
            assert _make_key("s", "t", left) == _make_key("s", "t", right)

    def test_component_boundaries_are_framed(self):
        # An unframed join lets a NUL inside one component shift the boundary:
        # ("a\0b", "c") and ("a", "b\0c") joined with "\0" are the same string,
        # so one call's cached row answered the other's (#784 case 1). Nothing
        # on the path rejects a NUL in an upstream server or tool name.
        assert _make_key("a\0b", "c", {}) != _make_key("a", "b\0c", {})

    def test_nul_cannot_shift_across_distant_boundaries(self):
        # The framing property must hold across EVERY boundary, not just the
        # adjacent server/tool pair. ``json.dumps`` never emits a raw NUL of
        # its own, so before framing, tool and config_fingerprint could trade
        # content straight across the args serialization sitting between them:
        # both of these joined to ``4\0s\0t\0{}\0{}\0f\0null``.
        k1 = _make_key("s", "t\0{}", {}, config_fingerprint="f")
        k2 = _make_key("s", "t", {}, config_fingerprint="{}\0f")
        assert k1 != k2

    def test_astral_scalar_does_not_alias_a_lone_surrogate_pair(self):
        # ``ensure_ascii=True`` renders U+10000 as the twelve-character
        # text ``\ud800\udc00`` — byte-identical to what it renders for
        # a caller string holding those two lone code units, so the astral
        # argument collided with the crafted pair (#784 case 2).
        # ``chr()`` because a JSON boundary merges the pair on decode; the two
        # code units have to be built in-process, which is also how they reach
        # this function in production.
        pair = chr(0xD800) + chr(0xDC00)
        scalar = chr(0x10000)
        assert pair != scalar
        assert _make_key("s", "t", {"x": pair}) != _make_key("s", "t", {"x": scalar})
        assert _make_key("s", "t", {}, context_query=pair) != _make_key(
            "s", "t", {}, context_query=scalar
        )


class TestKeyComponentsRoundtrip:
    def test_same_args_different_query_are_separate_entries(self, proxy_cache: ProxyCache):
        proxy_cache.set("s", "t", {"a": 1}, "for-q1", ttl_seconds=60.0, context_query="q1")
        proxy_cache.set("s", "t", {"a": 1}, "for-q2", ttl_seconds=60.0, context_query="q2")
        assert proxy_cache.get("s", "t", {"a": 1}, context_query="q1") == "for-q1"
        assert proxy_cache.get("s", "t", {"a": 1}, context_query="q2") == "for-q2"
        assert proxy_cache.get("s", "t", {"a": 1}) is None  # no-query key untouched

    def test_nul_shifted_names_are_separate_rows(self, proxy_cache: ProxyCache):
        # The live shape of the framing collision (#784): before the framed
        # derivation the second write landed on the first's row and the first
        # caller read back the second's body.
        proxy_cache.set("a\0b", "c", {}, "first", ttl_seconds=60.0)
        proxy_cache.set("a", "b\0c", {}, "second", ttl_seconds=60.0)
        assert proxy_cache.get("a\0b", "c", {}) == "first"
        assert proxy_cache.get("a", "b\0c", {}) == "second"

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
            assert version == 5
        finally:
            reopened.close()

    def test_v4_rows_are_purged_by_the_v5_bump(self, tmp_path):
        """The upgrade THIS release performs, pinned on its own.

        The other legacy fixtures seed ``user_version`` 0 and 2, so a
        regression that purged only those and left v4 rows in place would pass
        both — while the v4 rows it left behind are keyed under the pre-framing
        derivation and unreachable by any v5 lookup, which is exactly the dead
        weight the purge exists to drop (#784).
        """
        db_path = tmp_path / "c.db"
        cache = ProxyCache(db_path, max_entries=10)
        cache.initialize()
        cache.set("s", "t", {"a": 1}, "v4-row", ttl_seconds=None)
        # The v4 table shape is the current one — only the key derivation
        # changed — so seeding the version is enough to make this a v4 database.
        cache._db.execute("PRAGMA user_version = 4")
        cache._db.commit()
        cache.close()

        reopened = ProxyCache(db_path, max_entries=10)
        reopened.initialize()
        try:
            assert reopened.stats()["total_entries"] == 0
            (version,) = reopened._db.execute("PRAGMA user_version").fetchone()
            assert version == 5
        finally:
            reopened.close()

    def test_current_version_rows_survive_reopen(self, tmp_path):
        db_path = tmp_path / "c.db"
        cache = ProxyCache(db_path, max_entries=10)
        cache.initialize()  # stamps user_version = _KEY_SCHEMA_VERSION
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
            assert version == 5
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


# One from each end of the high and low surrogate blocks, matching the corpus
# in ``test_json_out.py`` so a range off by one at either boundary fails.
SURROGATES = ["\ud800", "\udbff", "\udc00", "\udfff"]


class TestSurrogateRoundTrip:
    """A stored lone surrogate must not come back as the code unit (#781).

    The writers escape, but a plain ``json.loads`` on the way out decodes the
    six characters ``\\ud800`` straight back into the unencodable code unit —
    which then raises at the next encode downstream rather than here. The
    pending store has scrubbed on read since #761; these are its siblings.
    """

    @pytest.mark.parametrize("surrogate", SURROGATES)
    def test_envelope_survives_the_round_trip(self, proxy_cache: ProxyCache, surrogate: str):
        proxy_cache.set(
            "s",
            "t",
            {"a": 1},
            "result",
            ttl_seconds=60.0,
            structured_content={"note": f"decision{surrogate}"},
            meta={"origin": f"trace{surrogate}"},
        )
        cached = proxy_cache.get("s", "t", {"a": 1})
        assert cached is not None and cached.structured_content is not None
        assert cached.meta is not None
        note = cached.structured_content["note"]
        origin = cached.meta["origin"]
        assert surrogate not in note and surrogate not in origin
        note.encode("utf-8")  # the call that used to raise
        origin.encode("utf-8")

    @pytest.mark.parametrize("surrogate", SURROGATES)
    def test_result_text_is_storable(self, proxy_cache: ProxyCache, surrogate: str):
        """``sqlite3`` encodes text parameters, so a raw one raised at execute.

        The caller catches ``Exception`` and leaves the response alone, so the
        cost was a silently uncached response and a warning per call — not a
        lost one. The body is content rather than an identity, so escaping it
        is right here where it would be wrong for the two names beside it.
        """
        proxy_cache.set("s", "t", {"a": 1}, f"answer{surrogate}", ttl_seconds=60.0)
        stored = proxy_cache.get("s", "t", {"a": 1})
        assert stored is not None
        assert surrogate not in stored
        stored.encode("utf-8")

    @pytest.mark.parametrize("surrogate", SURROGATES)
    def test_unencodable_identifier_is_not_cached(self, proxy_cache: ProxyCache, surrogate: str):
        """An identifier that cannot be a text parameter skips the store.

        Escaping it would be the wrong repair twice over: the row would no
        longer be reachable by its real name, and it would alias the distinct
        identifier spelled with those six literal characters. Not caching is
        the honest outcome, and the caller treats it as "response unaffected".
        """
        proxy_cache.set(f"srv{surrogate}", f"tool{surrogate}", {"a": 1}, "r", ttl_seconds=60.0)
        assert proxy_cache.get(f"srv{surrogate}", f"tool{surrogate}", {"a": 1}) is None
        assert proxy_cache.clear(server=f"srv{surrogate}") == 0

    @pytest.mark.parametrize("surrogate", SURROGATES)
    def test_key_does_not_alias_the_literal_escape(self, proxy_cache: ProxyCache, surrogate: str):
        """The digest must stay injective across the escaping helper's collision.

        ``escape_lone_surrogates`` maps the code unit and the six literal
        characters onto the same text by design, so deriving the key through
        it let one identifier's row answer for the other.
        """
        literal = f"srv\\u{ord(surrogate):04x}"
        assert _make_key(f"srv{surrogate}", "t", {}) != _make_key(literal, "t", {})

        proxy_cache.set(literal, "t", {}, "literal-body", ttl_seconds=60.0)
        assert proxy_cache.get(literal, "t", {}) == "literal-body"
        assert proxy_cache.get(f"srv{surrogate}", "t", {}) is None

    def test_key_derivation_matches_the_pinned_v5_shape(self):
        """The framed derivation, spelled out literally.

        Fails if the raw shape drifts without a ``_KEY_SCHEMA_VERSION`` bump —
        the way an existing entry becomes silently unreachable.
        """
        # The version is spelled out rather than read from the constant on
        # purpose: reading it would let a ``_KEY_SCHEMA_VERSION`` bump move
        # both sides together and pass, when a bump is exactly the event that
        # orphans every stored entry and must be acknowledged deliberately.
        # (v4 → v5 was such a bump: the framing fix #784 changed every key,
        # and the startup purge dropped the orphaned rows.)
        # Components chosen so BYTE length differs from character length in
        # three ways at once: a 3-byte CJK character, a 2-byte accented one,
        # and a lone surrogate that only ``surrogatepass`` can encode (3
        # bytes). A regression from ``len(data)`` to ``len(component)`` would
        # frame every one of them short and fail here — with ASCII-only
        # fixtures the two lengths coincide and the pin proves nothing.
        server, tool = "\uc11c\ubc84", "t\u00e9"
        args = {"k": "\u4e2d"}
        fingerprint = "fp\ud800"
        parts = [
            "5",
            server,
            tool,
            json.dumps(args, sort_keys=True, ensure_ascii=False),
            fingerprint,
            "null",
        ]
        raw = b"".join(
            f"{len(part.encode('utf-8', errors='surrogatepass'))}:".encode()
            + part.encode("utf-8", errors="surrogatepass")
            for part in parts
        )
        assert any(
            len(part.encode("utf-8", errors="surrogatepass")) != len(part) for part in parts
        ), "fixtures must make byte length differ from character length"
        assert (
            _make_key(server, tool, args, config_fingerprint=fingerprint)
            == hashlib.sha256(raw).hexdigest()
        )
