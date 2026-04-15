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

BUSY_TIMEOUT_MS = 3000
# Negative cache_size values are in KiB per SQLite docs; 64000 KiB = 64 MB.
CACHE_SIZE_KIB = -64000


def tune_connection(conn: sqlite3.Connection) -> None:
    """Apply the standard STM PRAGMA tuning to ``conn``.

    Idempotent — safe to call again on the same connection.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA cache_size={CACHE_SIZE_KIB}")
    conn.execute("PRAGMA temp_store=MEMORY")
