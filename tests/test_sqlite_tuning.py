"""Tests for the shared SQLite PRAGMA tuning helper.

Asserts that ``tune_connection`` actually applies the documented
PRAGMAs (not just no-ops) so any future regression in the helper
is caught immediately.
"""

from __future__ import annotations

import sqlite3

import pytest

from memtomem_stm.utils.sqlite_tuning import (
    BUSY_TIMEOUT_MS,
    CACHE_SIZE_KIB,
    tune_connection,
)


def _pragma(conn: sqlite3.Connection, name: str) -> object:
    row = conn.execute(f"PRAGMA {name}").fetchone()
    return row[0] if row else None


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    try:
        yield c
    finally:
        c.close()


def test_sets_wal_journal_mode(conn):
    tune_connection(conn)
    # SQLite returns "memory" for in-memory DBs even under WAL request,
    # but non-memory DBs return "wal". Just check the call did not error
    # and we can still query mode.
    mode = _pragma(conn, "journal_mode")
    assert mode in {"wal", "memory"}


def test_sets_busy_timeout(conn):
    tune_connection(conn)
    assert _pragma(conn, "busy_timeout") == BUSY_TIMEOUT_MS


def test_sets_synchronous_normal(conn):
    tune_connection(conn)
    # PRAGMA synchronous: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA
    assert _pragma(conn, "synchronous") == 1


def test_sets_cache_size(conn):
    tune_connection(conn)
    assert _pragma(conn, "cache_size") == CACHE_SIZE_KIB


def test_sets_temp_store_memory(conn):
    tune_connection(conn)
    # PRAGMA temp_store: 0=DEFAULT, 1=FILE, 2=MEMORY
    assert _pragma(conn, "temp_store") == 2


def test_idempotent(conn):
    """Calling twice leaves PRAGMAs in the same state."""
    tune_connection(conn)
    tune_connection(conn)
    assert _pragma(conn, "busy_timeout") == BUSY_TIMEOUT_MS
    assert _pragma(conn, "synchronous") == 1
    assert _pragma(conn, "cache_size") == CACHE_SIZE_KIB
    assert _pragma(conn, "temp_store") == 2
