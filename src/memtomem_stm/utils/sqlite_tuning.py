"""Shared PRAGMA tuning for long-lived SQLite stores.

Every STM-owned SQLite connection opens under WAL and picks up the same
baseline tuning: ``synchronous=NORMAL`` (saves one fsync per commit vs.
``FULL`` — WAL is safe under ``NORMAL``; the worst case is losing the
last committed transaction on OS-level power loss, acceptable for
cache/metrics/feedback stores), a 64 MB page cache (default ~2 MB
thrashes under moderate read load), and in-memory temp tables.

Centralized so all stores share the same durability tradeoff and any
future tuning lands in one place.
"""

from __future__ import annotations

import sqlite3
import time

BUSY_TIMEOUT_MS = 3000
# ``journal_mode`` is the one PRAGMA here that can fail outright while another
# connection holds a write lock: SQLite does not run the busy handler for it,
# so ``busy_timeout`` does not cover it and the call raises immediately. It is
# retried by hand instead, for whatever budget the caller asked for (#901).
_JOURNAL_MODE_RETRY_INTERVAL = 0.01
# Negative cache_size values are in KiB per SQLite docs; 64000 KiB = 64 MB.
CACHE_SIZE_KIB = -64000
# Cap WAL file growth so long-lived daemons don't accumulate unbounded
# .db-wal files. 64 MB matches the page cache budget above.
WAL_JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024  # 64 MB


def tune_connection(conn: sqlite3.Connection, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> None:
    """Apply the standard STM PRAGMA tuning to ``conn``.

    Idempotent — safe to call again on the same connection.

    ``busy_timeout_ms`` is the lock budget for this connection, and callers
    that want a different one must pass it HERE rather than overriding the
    pragma afterwards: the WAL retry below spends that budget during this call,
    so a later override would arrive too late to shorten it. ``MetricsStore``
    is the caller that cares — it deliberately fast-fails on a locked file so a
    best-effort write degrades instead of stalling a hook.
    """
    # Set the busy timeout FIRST so every statement after it is covered, then
    # take the one statement it cannot cover on its own terms.
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    _set_journal_mode_wal(conn, busy_timeout_ms / 1000)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA cache_size={CACHE_SIZE_KIB}")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute(f"PRAGMA journal_size_limit={WAL_JOURNAL_SIZE_LIMIT}")


def _set_journal_mode_wal(conn: sqlite3.Connection, budget_seconds: float) -> None:
    """Switch *conn* to WAL, waiting out a writer that holds the file.

    Two processes opening the same store at once is normal — a shared pending
    store exists for exactly that — and one of them may be mid-write while the
    other is still setting up. Every other statement here rides the busy
    timeout, but a ``journal_mode`` change does not: SQLite reports
    ``database is locked`` straight away rather than calling the busy handler,
    so an opener could fail on a file that was merely busy for a millisecond
    (#901). Retrying gives it the wait the other statements already get.

    Only ``SQLITE_BUSY`` is retried, read off ``sqlite_errorcode`` rather than
    matched on the message. Everything else a ``journal_mode`` change can raise
    — a read-only file, an unusable mode — is permanent, and spending the whole
    lock budget before reporting it would turn a clear startup failure into a
    stall.

    A file already in WAL — the overwhelmingly common case, since the mode is
    persistent — needs no lock at all and returns on the first attempt.
    """
    deadline = time.monotonic() + budget_seconds
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            # ``getattr``: only errors raised by the sqlite3 module carry the
            # code, and an error without one is not a busy error we can wait
            # out, so it re-raises with everything else that is not SQLITE_BUSY.
            code = getattr(exc, "sqlite_errorcode", None)
            now = time.monotonic()
            if code != sqlite3.SQLITE_BUSY or now >= deadline:
                raise
            # Clamped to what is left: a budget that is not a whole number of
            # intervals would otherwise overshoot on its last nap, spending
            # more than the caller allowed.
            time.sleep(min(_JOURNAL_MODE_RETRY_INTERVAL, deadline - now))
