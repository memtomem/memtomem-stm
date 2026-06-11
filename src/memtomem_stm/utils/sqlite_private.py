"""Private file modes for STM-owned SQLite stores.

Every STM SQLite store keeps user-scoped data (cached upstream responses,
surfacing queries and ratings, feedback text, read telemetry) under
``~/.memtomem``, so the DB files must not be readable by other local
users. ``mkdir(mode=0o700)`` on the parent only applies when the
directory is created — a pre-existing permissive ``~/.memtomem`` leaves
freshly created DB files at the umask default (typically ``0o644``).

SQLite creates ``-wal`` / ``-shm`` sidecars by copying the main DB
file's mode, so correcting the DB file at open time also governs
sidecars created later in the same or future runs — but sidecars left
behind by earlier runs under a permissive umask must be corrected
explicitly.

Centralized so every store applies the same policy (mirrors
``sqlite_tuning.tune_connection`` for PRAGMAs).
"""

from __future__ import annotations

from pathlib import Path

_SIDECAR_SUFFIXES = ("-wal", "-shm")


def ensure_private_db_files(db_path: Path) -> None:
    """``chmod 0o600`` the SQLite DB at ``db_path`` and existing sidecars.

    Call right after ``sqlite3.connect`` so the DB file exists.
    Best-effort: a missing sidecar or a filesystem without mode support
    must never fail store initialization (mirrors the previous inline
    chmod in ``ProxyCache`` / ``MetricsStore``).
    """
    sidecars = (db_path.with_name(db_path.name + s) for s in _SIDECAR_SUFFIXES)
    for path in (db_path, *sidecars):
        try:
            path.chmod(0o600)
        except OSError:
            pass
