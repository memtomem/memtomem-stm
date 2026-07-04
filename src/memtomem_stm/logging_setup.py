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

import errno
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


def _unsafe_log_target_reason(target: Path) -> str | None:
    """Why *target* can't be a hardened log file, or ``None`` if it's fine.

    A single source of truth for the terminal-path rule, shared by the write
    probe (``log_file_writable``) and the real open (``_open``) so the two
    can't diverge: a 0o600 append log must resolve to a regular file, so a
    dangling symlink (``os.open(O_CREAT)`` would silently create the target,
    redirecting the log) and an existing non-regular target (directory /
    special file / symlink to one) are refused. A symlink to an existing
    regular file is allowed.
    """
    if target.is_symlink() and not target.exists():
        return f"log path is a dangling symlink: {target}"
    if target.exists() and not target.is_file():
        return f"log path is not a regular file: {target}"
    return None


def _reject_unsafe_log_target(target: Path) -> None:
    reason = _unsafe_log_target_reason(target)
    if reason is not None:
        raise OSError(errno.ELOOP, reason)


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
        _reject_unsafe_log_target(Path(self.baseFilename))
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        # Race-free tighten of a pre-existing permissive file. os.fchmod is
        # POSIX-only; Windows has no fchmod (and ignores POSIX modes anyway),
        # so guard it — the sibling stores' perm-assertion tests skip Windows.
        if hasattr(os, "fchmod"):
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


def log_file_writable(path: Path) -> bool:
    """Best-effort, non-mutating check that ``open_private_log_handler`` would
    succeed for *path*.

    ``mms health`` runs in a separate process from the server, so it cannot
    observe the live handler — it can only tell whether the configured path
    *would* open. This mirrors what the handler actually needs — ``mkdir(
    parents=True)`` on the parent then ``os.open(file, O_WRONLY|O_CREAT|
    O_APPEND)`` — without side effects, so it rejects the cases that pass a
    naive "exists and writable" test but still fail the real open:

    - an existing directory / symlink-to-directory / special file (can't be
      opened ``O_WRONLY``): the terminal path must resolve to a regular file;
    - a broken symlink (``O_CREAT`` would chase a missing target);
    - a non-directory ancestor (``mkdir(parents=True)`` fails on it).

    A symlink to a regular writable file is accepted. Point-in-time: like any
    probe it races the real open, but for a diagnostic that is acceptable.
    """
    resolved = path.expanduser()
    # Terminal-path rule, shared with the real open so they can't disagree:
    # dangling symlink or existing non-regular target → not usable.
    if _unsafe_log_target_reason(resolved) is not None:
        return False
    if resolved.exists():
        # A regular file (possibly via symlink) must be writable for append.
        return os.access(resolved, os.W_OK)
    # Missing file: mkdir(parents=True) needs the nearest existing ancestor to
    # be a writable, traversable *directory* (it fails on a non-directory).
    ancestor = resolved.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        # A broken symlink in the parent chain reads as "missing" via exists()
        # (which follows links), but mkdir(parents=True) raises FileExistsError
        # on it rather than creating through it — so it's not usable.
        if ancestor.is_symlink():
            return False
        ancestor = ancestor.parent
    return ancestor.is_dir() and os.access(ancestor, os.W_OK | os.X_OK)


def describe_log_destination(config: STMConfig) -> dict[str, Any]:
    """Shared payload for ``mms health`` text and ``--json`` output.

    ``writable`` reflects whether the configured ``log_file`` can be opened
    right now (``None`` when unset). It is deliberately *configured*, not
    *active*: a separate ``mms health`` process cannot see the running
    server's handler, so an unwritable path means the server would fall back
    to stderr — which the health text then says explicitly rather than
    pointing at a file that receives nothing.
    """
    log_file = str(config.log_file.expanduser()) if config.log_file is not None else None
    return {
        "log_level": config.log_level,
        "log_file": log_file,
        "destination": "stderr+file" if log_file else "stderr",
        "writable": log_file_writable(config.log_file) if config.log_file is not None else None,
    }
