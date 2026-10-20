"""Shared logging configuration for the server and daemon entry points (#612).

The MCP stdio server logs to stderr, which the launching client captures (or
drops) — diagnosing "why did my proxy do nothing" means hunting per-client
log locations. ``MEMTOMEM_STM_LOG_FILE`` / ``STMConfig.log_file`` opt into a
rotating file log *in addition to* stderr. The daemon's detached mode routes
its fixed ``stm-daemon.log`` through the same handler (its stdio is DEVNULL —
the file is the only crash trace, #581).

Hardening matches the data-at-rest convention (``utils/fileio.py``,
``proxy/selection_log.py``): file ``0o600``, parent directory created
``0o700`` (a pre-existing directory keeps its mode). There is no
handler-level redaction hook: call sites render error text before logging it,
e.g. ``proxy/manager.py:_safe_error_text`` for upstream failures (#1082).

The one thing the handlers do render themselves is an attached traceback, and
that text can quote operator input a call site never saw — a pydantic
``ValidationError`` location carries the upstream key from a
``MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__<KEY>__…`` variable name. So every
handler STM installs formats through :class:`DisplayEscapingFormatter`, which
escapes the exception and stack text line by line the way call sites escape
values (#1173).

Below ``WARNING``, the same handlers also refuse every record that does not
come from STM's own ``memtomem_stm`` logger tree (:class:`DependencyRecordFilter`,
#1086). The MCP SDK and the HTTP clients under it log at INFO and DEBUG what no
STM call site rendered: the upstream URL with its query string on every
request, the MCP session id, response headers and whole MCP messages. Those
lines come back only when ``STMConfig.log_dependencies`` is set.
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

HOOK_STDERR_FORMAT = "%(message)s"
"""``mms hook`` stderr format: the bare message, exactly what the stdlib's
``logging.lastResort`` handler printed before the hook installed a handler of
its own (#1173). The hook's stderr reaches the host, so the shape stays."""

FILE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
"""File format carries timestamps: a persistent log spans restarts."""

LOG_FILE_MAX_BYTES = 2 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 3
"""2 MiB × (1 current + 3 backups) caps the worst-case footprint at 8 MiB
while keeping enough history to cover a crash discovered a few days late."""


STM_LOGGER_NAME = "memtomem_stm"
"""Root of the logger tree whose records pass STM's handlers at every level."""


class DependencyRecordFilter(logging.Filter):
    """Pass a record below ``WARNING`` only if STM's own code logged it (#1086).

    Third-party loggers — the MCP SDK (``mcp.*``, plus the top-level ``client``
    logger its session module uses), ``httpx``/``httpx2``, ``httpcore*``,
    ``urllib3`` under the OTLP exporter, and whatever else a dependency names —
    render their own text at INFO and DEBUG: an upstream URL with its query
    string, the MCP session id, response headers and reason phrases, exception
    text, and complete MCP messages in both directions. An allowlist rather
    than a list of those loggers, because a list is a coverage claim the next
    dependency falsifies. ``WARNING`` and above pass from every logger.

    This is a handler filter, not a logger level: dependency loggers keep
    their levels, and a handler someone else installs (an embedding host,
    pytest's capture) still receives the records. It protects only the
    handlers STM installs, and only records that propagate to them.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        name = record.name
        return name == STM_LOGGER_NAME or name.startswith(STM_LOGGER_NAME + ".")


def _screen_dependencies(handler: logging.Handler, dependency_records: bool) -> logging.Handler:
    if not dependency_records:
        handler.addFilter(DependencyRecordFilter())
    return handler


class DisplayEscapingFormatter(logging.Formatter):
    """A ``Formatter`` whose exception and stack text cannot carry a
    display-hostile character to the terminal or the log file (#1173).

    The message itself is the call site's job (each renders its values through
    ``utils/display._disp``), but the traceback is rendered here, and a
    ``ValidationError`` location quotes the operator's own environment: the
    upstream key of ``MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__<KEY>__…`` reaches
    the ``loc`` as ``upstream_servers.<key>``. A newline or a bidi control in
    that name would otherwise forge or reorder the lines below the escaped hint.

    Escaping is per line of the exception text, so the stack keeps its line
    structure. That leaves one thing open by design: a newline the operator
    injected still splits a location across two lines, and the second line is
    an unindented line inside the traceback block — under the bare-message
    hook format nothing distinguishes it from a message of its own, and under
    the prefixed formats exception text that starts with ``WARNING name:``
    reads as a line of its own too. What the formatter closes is reordering
    and terminal control: every bidi control, ESC and other C0/C1 character
    becomes its ``\\uXXXX`` form. ``_disp`` is idempotent on its own
    output, so a message a call site already escaped is unchanged. The stdlib
    caches the rendered exception on the record (``exc_text``) across handlers;
    ``format`` escapes that cache too, so the order handlers run in does not
    decide what the file gets.
    """

    def formatException(self, ei) -> str:  # type: ignore[no-untyped-def, override]
        return _escape_lines(super().formatException(ei))

    def formatStack(self, stack_info: str) -> str:
        return _escape_lines(super().formatStack(stack_info))

    def format(self, record: logging.LogRecord) -> str:
        if record.exc_text:
            record.exc_text = _escape_lines(record.exc_text)
        return super().format(record)


def _escape_lines(text: str) -> str:
    from memtomem_stm.utils.display import _disp

    return "\n".join(_disp(line) for line in text.split("\n"))


def stderr_handler(
    fmt: str = STDERR_FORMAT, *, dependency_records: bool = False
) -> logging.Handler:
    """A ``sys.stderr`` handler with the escaping formatter and, unless
    *dependency_records*, the :class:`DependencyRecordFilter`.

    Binds the stream at creation, as ``logging.StreamHandler()`` and the
    ``basicConfig(format=...)`` calls it replaces always did.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(DisplayEscapingFormatter(fmt))
    return _screen_dependencies(handler, dependency_records)


class _CurrentStderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """``StreamHandler`` that writes to whatever ``sys.stderr`` is at emit time.

    The shape of the stdlib's ``logging.lastResort`` (``_StderrHandler``):
    the stream is a property, so a process that swaps ``sys.stderr`` after the
    handler exists — Click's test runner per invocation, a host that redirects
    late — still gets the lines. ``mms hook`` relied on ``lastResort`` before
    #1173 and keeps that behavior through this class.
    """

    def __init__(self, level: int = logging.NOTSET) -> None:
        logging.Handler.__init__(self, level)

    @property
    def stream(self):  # type: ignore[no-untyped-def, override]
        return sys.stderr

    @stream.setter
    def stream(self, value: object) -> None:  # the base class assigns it; ignored
        pass


def configure_hook_logging() -> None:
    """Root logging for ``mms hook``: the ``logging.lastResort`` contract with
    the escaping formatter (#1173).

    Before this the hook installed no handler and the stdlib's last resort
    printed WARNING-and-up records as the bare message to the current
    ``sys.stderr``. This keeps all three — the handler's own level is WARNING,
    so a child logger an embedding process set to DEBUG still emits nothing
    below WARNING here; the format is ``%(message)s``; the stream is resolved
    per record — and only adds the formatter. The root logger's level is left
    as it is (no ``level=``): a process that set it to ERROR before running
    the hook keeps that gating, as it did under the last resort. Same no-op
    rule as ``basicConfig`` without ``force``. The dependency filter is
    attached too, for uniformity; at this handler's level it never decides.
    """
    handler = _CurrentStderrHandler(level=logging.WARNING)
    handler.setFormatter(DisplayEscapingFormatter(HOOK_STDERR_FORMAT))
    logging.basicConfig(handlers=[_screen_dependencies(handler, False)])


def configure_stderr_logging(
    *, level: int = logging.WARNING, fmt: str = STDERR_FORMAT, force: bool = False
) -> None:
    """``logging.basicConfig`` with one stderr handler and the escaping formatter.

    Same no-op rule as ``basicConfig`` without ``force``: a root logger that
    already has handlers (an embedding process, pytest's capture) is left
    alone, so the early-failure sites that call this keep their pre-#1173
    behavior of never replacing a host's logging. The handler's own level is
    ``NOTSET``, so the dependency filter is what keeps a dependency logger set
    to DEBUG off it; there is no config yet to opt out with.
    """
    logging.basicConfig(level=level, handlers=[stderr_handler(fmt)], force=force)


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
    dependency_records: bool = False,
) -> logging.Handler:
    """Rotating 0o600 file handler at *path*, parent created 0o700, with the
    :class:`DependencyRecordFilter` unless *dependency_records*.

    Raises ``OSError`` when the file or directory cannot be created — the
    caller decides whether that is fatal (daemon detached mode: yes, the file
    is the only output) or degradable (server: stderr still works).
    """
    resolved = path.expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = PrivateRotatingFileHandler(
        resolved, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(DisplayEscapingFormatter(FILE_FORMAT))
    return _screen_dependencies(handler, dependency_records)


def configure_server_logging(config: STMConfig) -> Path | None:
    """Root logging for the MCP server: stderr always, plus the opt-in file.

    Returns the active log-file path, or ``None`` when running stderr-only
    (unset ``log_file`` or a file that could not be opened — an opt-in
    diagnostic aid must not take the server down with it; the failure is
    logged to stderr instead).
    """
    dependency_records = config.log_dependencies
    handlers: list[logging.Handler] = [
        stderr_handler(STDERR_FORMAT, dependency_records=dependency_records)
    ]
    active_path: Path | None = None
    file_error: str | None = None
    if config.log_file is not None:
        try:
            handlers.append(
                open_private_log_handler(config.log_file, dependency_records=dependency_records)
            )
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
