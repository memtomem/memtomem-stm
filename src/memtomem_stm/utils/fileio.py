"""Filesystem helpers shared across the proxy.

The current owner is ``atomic_write_text`` — see PR #115 for the original
``_save`` it was extracted from. Centralising the temp + ``os.replace``
pattern keeps the third re-implementation of it from showing up.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
    ensure_parent: bool = True,
    parent_mode: int = 0o700,
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
        if mode is not None:
            try:
                tmp.chmod(mode)
            except OSError:
                pass
        os.replace(tmp, resolved)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
