"""0600 modes on STM-owned SQLite store files (utils/sqlite_private, #455).

``ProxyCache`` and ``MetricsStore`` historically chmod-ed their DB files
inline; the feedback/progressive/pending stores did not, so their DBs were
created at the umask default (typically 0644) when ``~/.memtomem`` already
existed. These tests pin the centralized policy: every store's
``initialize()`` leaves the DB file — and any sidecars left over from a
previous permissive run — at 0600.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memtomem_stm.proxy.cache import ProxyCache
from memtomem_stm.proxy.compression_feedback_store import CompressionFeedbackStore
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.proxy.pending_store import SQLitePendingStore
from memtomem_stm.proxy.progressive_reads_store import ProgressiveReadsStore
from memtomem_stm.surfacing.feedback_store import FeedbackStore
from memtomem_stm.utils.sqlite_private import ensure_private_db_files


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class TestEnsurePrivateDbFiles:
    def test_chmods_db_and_existing_sidecars(self, tmp_path: Path) -> None:
        db = tmp_path / "x.db"
        wal = tmp_path / "x.db-wal"
        shm = tmp_path / "x.db-shm"
        for p in (db, wal, shm):
            p.touch()
            p.chmod(0o644)

        ensure_private_db_files(db)

        assert _mode(db) == 0o600
        assert _mode(wal) == 0o600
        assert _mode(shm) == 0o600

    def test_missing_sidecars_are_ignored(self, tmp_path: Path) -> None:
        db = tmp_path / "x.db"
        db.touch()
        db.chmod(0o644)

        ensure_private_db_files(db)

        assert _mode(db) == 0o600

    def test_missing_db_does_not_raise(self, tmp_path: Path) -> None:
        ensure_private_db_files(tmp_path / "absent.db")

    def test_unrelated_files_untouched(self, tmp_path: Path) -> None:
        db = tmp_path / "x.db"
        db.touch()
        other = tmp_path / "x.db.bak"
        other.touch()
        other.chmod(0o644)

        ensure_private_db_files(db)

        assert _mode(other) == 0o644


STORE_FACTORIES = [
    pytest.param(ProxyCache, id="proxy_cache"),
    pytest.param(MetricsStore, id="metrics_store"),
    pytest.param(FeedbackStore, id="feedback_store"),
    pytest.param(CompressionFeedbackStore, id="compression_feedback_store"),
    pytest.param(ProgressiveReadsStore, id="progressive_reads_store"),
    pytest.param(SQLitePendingStore, id="pending_store"),
]


@pytest.mark.parametrize("store_cls", STORE_FACTORIES)
class TestStoreDbModes:
    def test_fresh_db_is_private(self, store_cls, tmp_path: Path) -> None:
        db_path = tmp_path / "store.db"
        store = store_cls(db_path)
        store.initialize()
        try:
            assert _mode(db_path) == 0o600
        finally:
            store.close()

    def test_permissive_db_and_sidecars_are_corrected(self, store_cls, tmp_path: Path) -> None:
        db_path = tmp_path / "store.db"
        # Simulate a DB created by an earlier release under a permissive
        # umask: valid (empty-schema) SQLite file plus leftover WAL
        # sidecars, all world-readable. SQLite copies the DB file's mode
        # when it creates sidecars, so a 0644 DB keeps producing 0644
        # sidecars until the DB itself is corrected.
        sqlite3.connect(str(db_path)).close()
        wal = tmp_path / "store.db-wal"
        shm = tmp_path / "store.db-shm"
        wal.touch()
        shm.touch()
        for p in (db_path, wal, shm):
            p.chmod(0o644)

        store = store_cls(db_path)
        store.initialize()
        try:
            assert _mode(db_path) == 0o600
            assert _mode(wal) == 0o600
            assert _mode(shm) == 0o600
        finally:
            store.close()
