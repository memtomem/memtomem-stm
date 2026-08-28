"""Tests for the shared SQLite PRAGMA tuning helper.

Asserts that ``tune_connection`` actually applies the documented
PRAGMAs (not just no-ops) so any future regression in the helper
is caught immediately.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from memtomem_stm.utils import sqlite_tuning
from memtomem_stm.utils.sqlite_tuning import (
    BUSY_TIMEOUT_MS,
    CACHE_SIZE_KIB,
    WAL_JOURNAL_SIZE_LIMIT,
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


def test_sets_journal_size_limit(conn):
    tune_connection(conn)
    assert _pragma(conn, "journal_size_limit") == WAL_JOURNAL_SIZE_LIMIT


def test_idempotent(conn):
    """Calling twice leaves PRAGMAs in the same state."""
    tune_connection(conn)
    tune_connection(conn)
    assert _pragma(conn, "busy_timeout") == BUSY_TIMEOUT_MS
    assert _pragma(conn, "synchronous") == 1
    assert _pragma(conn, "cache_size") == CACHE_SIZE_KIB
    assert _pragma(conn, "temp_store") == 2
    assert _pragma(conn, "journal_size_limit") == WAL_JOURNAL_SIZE_LIMIT


# ── the WAL retry (#901) ───────────────────────────────────────────────


def _busy_error() -> sqlite3.OperationalError:
    """A real SQLITE_BUSY, carrying the errorcode the retry reads."""
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "busy.db")
        holder = sqlite3.connect(path, timeout=0.001, isolation_level=None)
        other = sqlite3.connect(path, timeout=0.001, isolation_level=None)
        try:
            holder.execute("CREATE TABLE t (a)")
            holder.execute("BEGIN EXCLUSIVE")
            try:
                other.execute("INSERT INTO t VALUES (1)")
            except sqlite3.OperationalError as exc:
                assert exc.sqlite_errorcode == sqlite3.SQLITE_BUSY
                return exc
            raise AssertionError("could not provoke SQLITE_BUSY")
        finally:
            holder.close()
            other.close()


class _ScriptedConnection:
    """Fails ``journal_mode`` a set number of times, then succeeds."""

    def __init__(self, failures: int, error: sqlite3.Error) -> None:
        self._failures = failures
        self._error = error
        self.journal_attempts = 0
        self.statements: list[str] = []

    def execute(self, sql: str, *args: object) -> object:
        self.statements.append(sql)
        if sql.startswith("PRAGMA journal_mode"):
            self.journal_attempts += 1
            if self._failures > 0:
                self._failures -= 1
                raise self._error
        return None


def test_wal_retry_waits_out_a_busy_file():
    """A file busy for a moment must not fail an opener: SQLite does not run
    the busy handler for a journal_mode change, so the retry is the only thing
    covering it."""
    conn = _ScriptedConnection(failures=2, error=_busy_error())

    tune_connection(conn)  # type: ignore[arg-type]

    assert conn.journal_attempts == 3, "the retry gave up early"
    assert conn.statements[0].startswith("PRAGMA busy_timeout"), (
        "the busy timeout must be set before the statement it cannot cover"
    )


class _FakeClock:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.parametrize(("budget_ms", "expected_attempts"), [(50, 6), (200, 21), (55, 7)])
def test_wal_retry_spends_exactly_the_budget(monkeypatch, budget_ms, expected_attempts):
    """The wait must be bounded by the budget the CALLER passed.

    Asserted against a fake clock and an exact attempt count rather than
    wall-clock elapsed time: a "finished within N seconds" bound would pass
    just as well for a hard-coded retry window that ignores the argument
    entirely, which is the regression this exists to catch. Several budgets,
    so a fixed count cannot satisfy them all — including 55 ms, which is not a
    whole number of retry intervals and so catches a last nap that overshoots
    the budget instead of being clamped to what remains.
    """
    clock = _FakeClock()
    monkeypatch.setattr(sqlite_tuning.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(sqlite_tuning.time, "sleep", clock.sleep)
    conn = _ScriptedConnection(failures=10_000, error=_busy_error())

    with pytest.raises(sqlite3.OperationalError):
        tune_connection(conn, busy_timeout_ms=budget_ms)  # type: ignore[arg-type]

    assert conn.journal_attempts == expected_attempts
    assert clock.now == pytest.approx(budget_ms / 1000), (
        "the retry stopped somewhere other than the caller's budget"
    )


def test_wal_retry_reraises_a_non_busy_error_immediately():
    """A read-only file or an unusable mode is permanent. Spending the whole
    lock budget before reporting it would turn a clear startup failure into a
    stall, so only SQLITE_BUSY is retried."""
    permanent = sqlite3.OperationalError("attempt to write a readonly database")
    conn = _ScriptedConnection(failures=10_000, error=permanent)

    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        tune_connection(conn)  # type: ignore[arg-type]
    elapsed = time.monotonic() - started

    assert conn.journal_attempts == 1, "a permanent error was retried"
    assert elapsed < 1.0, f"a permanent error stalled for {elapsed:.2f}s"


def test_busy_timeout_honors_the_callers_budget(conn):
    """MetricsStore fast-fails on a locked file; passing the budget in is the
    only way it applies to the retry as well as to the pragma."""
    tune_connection(conn, busy_timeout_ms=250)
    assert _pragma(conn, "busy_timeout") == 250
