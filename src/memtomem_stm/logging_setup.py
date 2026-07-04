"""Shared logging configuration for the server and daemon entry points (#612).

The MCP stdio server logs to stderr, which the launching client captures (or
drops) — diagnosing "why did my proxy do nothing" means hunting per-client
log locations. ``MEMTOMEM_STM_LOG_FILE`` / ``STMConfig.log_file`` opt into a
rotating file log *in addition to* stderr. The daemon's detached mode routes
its fixed ``stm-daemon.log`` through the same handler (its stdio is DEVNULL —
the file is the only crash trace, #581).

Hardening matches the data-at-rest convention (``utils/fileio.py``,
``proxy/selection_log.py``): file ``0o600``, parent directory created
``0o700`` (a pre-existing directory keeps its mode). Credential redaction
needs no handler-level hook — it happens at message-construction time
(``proxy/manager.py:_redacted_error``), so every handler receives already-
redacted records.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from memtomem_stm.config import STMConfig

STDERR_FORMAT = "%(levelname)s %(name)s: %(message)s"
"""Server stderr format — unchanged from the pre-#612 ``basicConfig``."""

FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
"""File format carries timestamps: a persistent log spans restarts."""

LOG_FILE_MAX_BYTES = 2 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 3
"""2 MiB × (1 current + 3 backups) caps the worst-case footprint at 8 MiB
while keeping enough history to cover a crash discovered a few days late."""


class PrivateRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler whose files are created ``0o600``.

    ``FileHandler`` has no ``opener=`` hook, so ``_open`` is overridden with
    the create-private-then-tighten idiom from ``proxy/selection_log.py``:
    ``os.open(..., 0o600)`` covers creation (umask-permitting) and the
    ``fchmod`` tightens a pre-existing permissive file. ``doRollover``
    re-invokes ``_open`` for the recreated base file, so post-rotation files
    get the same mode; rotated backups are ``os.rename``d (mode travels with
    the inode) and need nothing extra.
    """

    def _open(self):  # type: ignore[override]
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        return os.fdopen(fd, "a", encoding="utf-8")


def open_private_log_handler(
    path: Path,
    *,
    max_bytes: int = LOG_FILE_MAX_BYTES,
    backup_count: int = LOG_FILE_BACKUP_COUNT,
) -> logging.Handler:
    """Rotating 0o600 file handler at *path*, parent created 0o700.

    Raises ``OSError`` when the file or directory cannot be created — the
    caller decides whether that is fatal (daemon detached mode: yes, the file
    is the only output) or degradable (server: stderr still works).
    """
    resolved = path.expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = PrivateRotatingFileHandler(
        resolved, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(FILE_FORMAT))
    return handler


def configure_server_logging(config: STMConfig) -> Path | None:
    """Root logging for the MCP server: stderr always, plus the opt-in file.

    Returns the active log-file path, or ``None`` when running stderr-only
    (unset ``log_file`` or a file that could not be opened — an opt-in
    diagnostic aid must not take the server down with it; the failure is
    logged to stderr instead).
    """
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter(STDERR_FORMAT))
    handlers: list[logging.Handler] = [stderr_handler]
    active_path: Path | None = None
    file_error: str | None = None
    if config.log_file is not None:
        try:
            handlers.append(open_private_log_handler(config.log_file))
            active_path = config.log_file.expanduser()
        except OSError as exc:
            file_error = str(exc)  # logged once basicConfig installs stderr
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.WARNING),
        handlers=handlers,
        force=True,
    )
    if file_error is not None:
        logging.getLogger(__name__).warning(
            "log_file %s could not be opened (%s); continuing with stderr only",
            config.log_file,
            file_error,
        )
    return active_path


def describe_log_destination(config: STMConfig) -> dict[str, Any]:
    """Shared payload for ``mms health`` text and ``--json`` output."""
    log_file = str(config.log_file.expanduser()) if config.log_file is not None else None
    return {
        "log_level": config.log_level,
        "log_file": log_file,
        "destination": "stderr+file" if log_file else "stderr",
    }
