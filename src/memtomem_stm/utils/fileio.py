"""Filesystem helpers shared across the proxy.

The current owner is ``atomic_write_text`` — see PR #115 for the original
``_save`` it was extracted from. Centralising the temp + ``os.replace``
pattern keeps the third re-implementation of it from showing up.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

# Windows ``MoveFileEx`` retry budget. The wall-clock ceiling is approximate —
# Windows' default timer resolution is ~15.6 ms, so each ``time.sleep(0.005)``
# typically rounds up to one tick. Real worst case is closer to 150 ms than
# 50 ms; treat the budget as "low enough to not stall a hot-path write" rather
# than a hard SLA.
_WIN_REPLACE_ATTEMPTS = 10
_WIN_REPLACE_BACKOFF_S = 0.005


def atomic_write_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
    ensure_parent: bool = True,
    parent_mode: int = 0o700,
    durable: bool = False,
) -> None:
    """Atomically write ``content`` to ``path``.

    Writes to a sibling temp file in the same directory (so the rename is
    atomic on POSIX — same filesystem) and ``os.replace``\\ s onto the
    target. Concurrent readers either see the previous contents or the
    new ones, never a partially-written file. The proxy's hot-reload
    watcher (mtime-based) and the auto-indexer both rely on this; a
    half-written read produces a JSONDecodeError or a corrupt index entry
    that is hard to attribute back to the writer.

    Failure during the temp write removes the temp and re-raises, so the
    target is left untouched.

    :param path: Destination path. ``~`` is expanded and the path is
        resolved before the write so callers can pass user-relative paths.
    :param content: Text payload.
    :param encoding: Text encoding (default ``utf-8``).
    :param mode: If set, ``chmod`` is applied to the temp file *before*
        the rename so the final file is never observable at a permissive
        mode. Use ``0o600`` for sensitive configs.
    :param ensure_parent: When True (default), create missing parent
        directories with ``parent_mode``. Pass False when the caller has
        already prepared the parent and does not want its mode rewritten.
    :param parent_mode: Mode for created parent directories. Only used
        when ``ensure_parent`` is True.
    :param durable: When True, ``fsync`` the temp file before the rename
        and ``fsync`` the parent directory after it (POSIX), so the write
        survives power loss / kernel panic — not just a process crash. The
        default ``os.replace`` guards against process crash already; the
        rename can otherwise become durable before the data blocks, leaving
        a truncated file after an unclean shutdown. Costs a disk flush per
        write, so it is opt-in: pass ``durable=True`` from config/state
        writers (single sources of truth that ``mms init`` refuses to
        recreate), leave it off for high-frequency hot-path writers.
    """
    resolved = path.expanduser().resolve()
    if ensure_parent:
        resolved.parent.mkdir(parents=True, exist_ok=True, mode=parent_mode)
    fd, tmp_path = tempfile.mkstemp(
        prefix=resolved.name + ".",
        suffix=".tmp",
        dir=str(resolved.parent),
    )
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            if durable:
                # Flush Python + libc buffers, then force the data blocks to
                # disk BEFORE the rename, so os.replace can't make the new
                # name durable ahead of the content it points at.
                f.flush()
                os.fsync(f.fileno())
        if mode is not None:
            try:
                tmp.chmod(mode)
            except OSError:
                pass
        _replace_with_windows_retry(tmp, resolved)
        if durable:
            _fsync_dir(resolved.parent)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of ``directory`` so a rename into it is durable
    (POSIX). No-op on platforms without directory fds (Windows) or when the
    open/fsync fails — durability is a best-effort hardening, not a hard
    guarantee, and a failure here must not fail the write that already
    landed via ``os.replace``."""
    if sys.platform == "win32":
        return
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _replace_with_windows_retry(src: Path, dst: Path) -> None:
    """``os.replace`` with a brief retry loop on Windows.

    On Windows, ``MoveFileEx`` (the syscall behind ``os.replace``) raises
    ``PermissionError`` (``WinError 5``) when another process has ``dst``
    open without ``FILE_SHARE_DELETE`` — and Python's ``open()`` does not
    pass that flag. A concurrent reader (e.g. the auto-index watcher
    re-reading the same file ``atomic_write_text`` is replacing) holds a
    sub-millisecond handle that briefly blocks the rename. The rename
    itself stays NTFS-atomic; we just ride out the transient conflict.

    Scope intentionally narrow: only ``PermissionError`` (``WinError 5``)
    is retried. ``WinError 32`` (sharing violation under AV scans, etc.)
    has a similar transient pattern but isn't observed in CI today; widen
    the catch only when a concrete failure appears.
    """
    if sys.platform != "win32":
        os.replace(src, dst)
        return
    for attempt in range(_WIN_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _WIN_REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_WIN_REPLACE_BACKOFF_S)
