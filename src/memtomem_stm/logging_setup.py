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

What the handlers do render themselves is the interpolation of a record's
arguments and an attached traceback, and either can carry a character the
terminal must not get as itself. An upstream server key is an argument of most
proxy log lines and config does not restrict its characters (#1425); a pydantic
``ValidationError`` location carries the same key from a
``MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__<KEY>__…`` variable name, which no call
site ever saw (#1173). So every handler STM installs formats through
:class:`DisplayEscapingFormatter`, which escapes each argument as it is
interpolated and the exception and stack text line by line.

Below ``WARNING``, the same handlers also refuse every record that does not
come from STM's own ``memtomem_stm`` logger tree (:class:`DependencyRecordFilter`,
#1086). The MCP SDK and the HTTP clients under it log at INFO and DEBUG what no
STM call site rendered: the upstream URL with its query string on every
request, the MCP session id, response headers and whole MCP messages. Those
lines come back only when ``STMConfig.log_dependencies`` is set.

At every level, a traceback those handlers render keeps its frames but not its
messages (:func:`render_withheld_traceback`, #1086): the last line of each
exception would repeat the exception's text, which an upstream, the LTM server
or an LLM provider wrote and can fill with a URL query, a tool argument or part
of a header. ``STMConfig.log_exception_text`` restores the stdlib rendering.
"""

from __future__ import annotations

import errno
import logging
import operator
import os
import sys
import traceback
import types
from collections.abc import Mapping
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


WITHHELD_RENDER_FAILED = "Traceback withheld: it could not be rendered without its messages."
"""The whole rendering when building the withheld form raised: a constant, so
nothing about the exception that broke it can reach the line."""

WITHHELD_CACHED_TRACEBACK = "[traceback withheld]"
"""Stands in for a traceback another handler rendered and cached on the record
(``exc_text``) when the exception itself is no longer attached."""

_MAX_CHAIN = 32
_MAX_GROUP_WIDTH = 15
_MAX_GROUP_DEPTH = 10
_MAX_EXCEPTIONS = 256
_MAX_LINES = 2000
_MAX_FRAMES = 1000
_RESERVED_LINES = 3
"""Kept free after an exception's frames: the omitted-frames marker, the
exception line and the notes line, so the raise site's class always prints."""
_OWN_TRACEBACK: Any = object()
_CAUSE_SEPARATOR = "The above exception was the direct cause of the following exception:"
_CONTEXT_SEPARATOR = "During handling of the above exception, another exception occurred:"


class _BudgetSpent(Exception):
    """Unwinds the render once the line or exception budget is used up."""


class _WithheldOutput:
    """Lines written so far, with the budgets every branch draws from."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.exceptions = 0

    def add(self, line: str, margin: str) -> None:
        if len(self.lines) >= _MAX_LINES:
            raise _BudgetSpent
        self.lines.append(f"{margin}{line}" if line or not margin else margin.rstrip())

    def count_exception(self) -> None:
        if self.exceptions >= _MAX_EXCEPTIONS:
            raise _BudgetSpent
        self.exceptions += 1


def render_withheld_traceback(exc: BaseException, tb: Any = _OWN_TRACEBACK) -> str:
    """*exc*'s traceback with every message left out (#1086).

    Built only from what the code defines, never from what an exception was
    given: each exception's module-qualified class name, a code from
    ``exception_code_label`` (an HTTP status, a reserved JSON-RPC code, a SQLite
    result-code name), its frames — file names, line numbers, function names
    and source lines, without locals or the column carets — and the chain and
    group structure the stdlib prints. Notes are counted, not shown. Nothing
    reads ``str(exc)``, ``args``, a ``SyntaxError``'s text or a note, so a field
    the stdlib adds later cannot carry a message here either. Counts and codes
    come from built-in types and fixed ranges, not from a subclass's override.
    *tb*, when given,
    stands in for *exc*'s own ``__traceback__``, as the third item of an
    ``exc_info`` tuple does for the stdlib — ``None`` included, which renders
    no frames for *exc*.

    Trusted, and so still shown: class, module and function names and the
    source of the frames — they come from code, not from the data that failed.
    Bounded while it renders, not after: the innermost frames of each
    traceback, at most 1000 and as many as the line budget still holds with
    room for the exception's own line (the raise site stays), 32 chained exceptions per chain, 15
    members and 10 levels of exception groups, 256 exceptions and 2000 lines in
    all, each cut marked (the ``[traceback truncated]`` marker is one line
    past the 2000). If any step raises, the whole result is
    :data:`WITHHELD_RENDER_FAILED`; only ``KeyboardInterrupt`` and
    ``SystemExit`` pass through.
    """
    out = _WithheldOutput()
    try:
        _render_chain(exc, out, "", 0, set(), tb)
    except _BudgetSpent:
        out.lines.append("[traceback truncated]")
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        return WITHHELD_RENDER_FAILED
    return "\n".join(out.lines)


def _render_chain(
    exc: BaseException,
    out: _WithheldOutput,
    margin: str,
    depth: int,
    seen: set[int],
    tb: Any = _OWN_TRACEBACK,
) -> None:
    # The chain, newest first, each entry with the sentence that links it to
    # the entry before it; the stdlib's cause/context precedence.
    chain: list[tuple[BaseException, str | None]] = []
    current: BaseException | None = exc
    link: str | None = None
    omitted = False
    while current is not None and id(current) not in seen:
        if len(chain) == _MAX_CHAIN:
            omitted = True
            break
        seen.add(id(current))
        chain.append((current, link))
        if current.__cause__ is not None:
            current, link = current.__cause__, _CAUSE_SEPARATOR
        elif current.__context__ is not None and not current.__suppress_context__:
            current, link = current.__context__, _CONTEXT_SEPARATOR
        else:
            current = None
    if omitted:
        out.add("[earlier exceptions in the chain omitted]", margin)
    ordered = list(reversed(chain))
    for index, (item, _) in enumerate(ordered):
        if index:
            # The sentence that links the previous (older) item to this one.
            out.add("", margin)
            out.add(ordered[index - 1][1] or "", margin)
            out.add("", margin)
        own_tb = tb if item is exc and tb is not _OWN_TRACEBACK else item.__traceback__
        _render_exception(item, own_tb, out, margin, depth, seen)


def _render_exception(
    exc: BaseException,
    tb: types.TracebackType | None,
    out: _WithheldOutput,
    margin: str,
    depth: int,
    seen: set[int],
) -> None:
    from memtomem_stm.utils.redact import exception_code_label

    out.count_exception()
    if isinstance(exc, BaseExceptionGroup) and depth >= _MAX_GROUP_DEPTH:
        out.add("[nested exception groups omitted]", margin)
        return
    if tb is not None:
        if not isinstance(tb, types.TracebackType):
            raise TypeError("not a traceback")
        out.add("Traceback (most recent call last):", margin)
        # The innermost frames, capped before extraction: the line budget
        # alone would still extract and format every frame first, and a
        # positive limit would keep the outermost ones and drop the raise
        # site. Walking tb_next is bounded: the interpreter refuses a cycle.
        walked = 0

        def counted(frames_in: Any) -> Any:
            nonlocal walked
            for item in frames_in:
                walked += 1
                yield item

        # Each frame renders as at most two lines (no column positions are
        # extracted, so no multi-line statements or carets).
        room = (_MAX_LINES - len(out.lines) - _RESERVED_LINES) // 2
        if room < 1:
            raise _BudgetSpent
        frames = traceback.StackSummary.extract(
            counted(traceback.walk_tb(tb)), limit=-min(_MAX_FRAMES, room), capture_locals=False
        )
        if walked > len(frames):
            out.add(f"[{walked - len(frames)} outer frames omitted]", margin)
        for chunk in frames.format():
            for line in chunk.rstrip("\n").split("\n"):
                out.add(line, margin)
    code = exception_code_label(exc)
    detail = f"[message withheld; {code}]" if code else "[message withheld]"
    out.add(f"{_exception_type_label(type(exc))}: {detail}", margin)
    notes = exc.__dict__.get("__notes__")
    if notes is not None:
        # Counted only for the built-in types, whose len() no subclass overrides.
        count = len(notes) if type(notes) in (list, tuple) else None
        out.add(f"[{count} note(s) withheld]" if count else "[notes withheld]", margin)
    if isinstance(exc, BaseExceptionGroup):
        # The members the group was built with, through the base descriptor: a
        # subclass's ``exceptions`` property could report any count.
        members = BaseExceptionGroup.exceptions.__get__(exc)
        for number, member in enumerate(members[:_MAX_GROUP_WIDTH], start=1):
            out.add(f"+-- sub-exception {number} of {len(members)} --", margin)
            if id(member) in seen:
                out.add(f"{_exception_type_label(type(member))}: [shown above]", margin + "| ")
            else:
                _render_chain(member, out, margin + "| ", depth + 1, seen)
        if len(members) > _MAX_GROUP_WIDTH:
            out.add(f"+-- {len(members) - _MAX_GROUP_WIDTH} more sub-exceptions omitted --", margin)


def _exception_type_label(cls: type[BaseException]) -> str:
    """The stdlib's spelling: module-qualified unless builtin or ``__main__``."""
    module, qualname = cls.__module__, cls.__qualname__
    if not isinstance(module, str) or not isinstance(qualname, str):
        raise TypeError("exception type without a string name")
    return qualname if module in ("builtins", "__main__") else f"{module}.{qualname}"


class DisplayEscapingFormatter(logging.Formatter):
    """A ``Formatter`` whose interpolated arguments, exception text and stack
    text cannot carry a display-hostile character to the terminal or the log
    file (#1173, #1425).

    **Arguments.** A call site passes its values as logging arguments, and each
    one is escaped as ``%`` interpolates it, the way ``utils/display._disp``
    escapes a value for the CLI: a member of the set ``_disp_escapes`` defines
    becomes its ``\\uXXXX`` form, under ``%s`` and ``%r`` alike. So a server key
    with a newline in it stays on the line that names it. The literal text of
    the format string is not touched — a line break a call site wrote there is
    its own — and neither is a value formatted into the message before the
    call: an f-string or a pre-built message never reaches ``record.args``.
    ``tests/test_log_call_sites.py`` holds STM's own call sites to a literal
    message for that reason. ``record.args`` is swapped for the duration of one
    ``format`` call and put back, so another handler sees the record as logged.

    An argument whose rendering cannot contain a member of the set is passed as
    itself (:func:`_display_arg`), which keeps ``%d``, ``%f`` and a ``*`` width
    working on plain numbers. Every other argument is interpolated through a
    stand-in, with one visible difference from the stdlib: ``%c`` refuses the
    stand-in for a string, so a record that gives ``%c`` a one-character string
    that needed escaping, or a one-character ``str`` subclass, is reported
    through ``logging``'s error handler instead of written. ``%c`` of an ``int``
    writes that code point raw, which is the other reason the same test keeps
    ``%c`` out of STM's format strings.

    **Tracebacks.** The traceback is rendered here too, and a
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

    By default it also withholds exception messages
    (:func:`render_withheld_traceback`, #1086); *withhold_exception_text*
    ``False`` keeps the stdlib rendering. A withholding formatter ignores a
    cached ``exc_text`` — another handler may have rendered it in full — and
    restores the record's cache after formatting, so a handler that runs
    later sees the record as it would have without this one.
    """

    def __init__(
        self, fmt: str | None = None, *, withhold_exception_text: bool = True, **kwargs: Any
    ) -> None:
        super().__init__(fmt, **kwargs)
        self._withhold_exception_text = withhold_exception_text

    def formatException(self, ei) -> str:  # type: ignore[no-untyped-def, override]
        if self._withhold_exception_text:
            exc = ei[1]
            if isinstance(exc, BaseException):
                return _escape_lines(render_withheld_traceback(exc, ei[2]))
            if exc is not None:
                # Not an exception: the stdlib would print whatever str() gives.
                return WITHHELD_RENDER_FAILED
        return _escape_lines(super().formatException(ei))

    def formatStack(self, stack_info: str) -> str:
        return _escape_lines(super().formatStack(stack_info))

    def format(self, record: logging.LogRecord) -> str:
        args = record.args
        record.args = _display_args(args)
        try:
            return self._format_escaped_traceback(record)
        finally:
            record.args = args

    def _format_escaped_traceback(self, record: logging.LogRecord) -> str:
        if not self._withhold_exception_text:
            if record.exc_text:
                record.exc_text = _escape_lines(record.exc_text)
            return super().format(record)
        cached = record.exc_text
        if record.exc_info:
            record.exc_text = None
        elif cached:
            record.exc_text = WITHHELD_CACHED_TRACEBACK
        try:
            return super().format(record)
        finally:
            record.exc_text = cached


def _escape_lines(text: str) -> str:
    return "\n".join(_escape(line) for line in text.split("\n"))


def _escape(text: str) -> str:
    from memtomem_stm.utils.display import _disp

    return _disp(text)


class _DisplayArg:
    """Stand-in for one log argument: ``%s`` and ``%r`` of it are escaped.

    The numeric conversions go to the value itself, so ``%d`` of a ``Decimal``
    or of any object with ``__int__`` renders as the stdlib renders it.
    """

    __slots__ = ("_value",)

    def __init__(self, value: object) -> None:
        self._value = value

    def __str__(self) -> str:
        return _escape(str(self._value))

    def __repr__(self) -> str:
        return _escape(repr(self._value))

    def __int__(self) -> int:
        return int(self._value)  # type: ignore[call-overload, no-any-return]

    def __index__(self) -> int:
        return operator.index(self._value)  # type: ignore[arg-type]

    def __float__(self) -> float:
        return float(self._value)  # type: ignore[arg-type]


class _DisplayMapping(_DisplayArg):
    """Stand-in for a mapping argument (``"%(key)s" % mapping``).

    Lookups go to the original mapping, so a ``defaultdict`` still supplies its
    default, and what a lookup returns is escaped like any other argument.
    ``%s`` of the whole mapping is the inherited escaped ``str()``.
    """

    __slots__ = ()

    def __getitem__(self, key: object) -> object:
        return _display_arg(self._value[key])  # type: ignore[index]


def _display_arg(value: object) -> object:
    """*value* as itself when no rendering of it can need escaping, else a stand-in.

    As itself: ``None`` and an exact ``bool``, ``int`` or ``float`` — digits,
    signs and a few fixed words — and an exact ``str`` that ``_disp`` would not
    change. Exact types only: a subclass can define a ``__str__`` or
    ``__repr__`` that returns anything. The exact ``int`` is more than a
    shortcut, since ``%*d`` accepts nothing else as its width.
    """
    if value is None or type(value) in (bool, int, float):
        return value
    if type(value) is str and _escape(value) == value:
        return value
    return _DisplayArg(value)


def _display_args(args: Any) -> Any:
    """``record.args`` with every argument passed through :func:`_display_arg`.

    The three shapes ``LogRecord.getMessage`` hands to ``%``: a tuple, a
    mapping (the stdlib unwraps a single mapping argument), and — from a
    record built by hand — one bare object.
    """
    if not args:
        return args
    if isinstance(args, Mapping):
        return _DisplayMapping(args)
    if isinstance(args, tuple):
        return tuple(_display_arg(value) for value in args)
    return _display_arg(args)


def stderr_handler(
    fmt: str = STDERR_FORMAT, *, dependency_records: bool = False, exception_text: bool = False
) -> logging.Handler:
    """A ``sys.stderr`` handler with the escaping formatter and, unless
    *dependency_records*, the :class:`DependencyRecordFilter`. Tracebacks are
    withheld unless *exception_text*.

    Binds the stream at creation, as ``logging.StreamHandler()`` and the
    ``basicConfig(format=...)`` calls it replaces always did.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(DisplayEscapingFormatter(fmt, withhold_exception_text=not exception_text))
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
    exception_text: bool = False,
) -> logging.Handler:
    """Rotating 0o600 file handler at *path*, parent created 0o700, with the
    :class:`DependencyRecordFilter` unless *dependency_records*, and tracebacks
    withheld unless *exception_text*.

    Raises ``OSError`` when the file or directory cannot be created — the
    caller decides whether that is fatal (daemon detached mode: yes, the file
    is the only output) or degradable (server: stderr still works).
    """
    resolved = path.expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = PrivateRotatingFileHandler(
        resolved, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(
        DisplayEscapingFormatter(FILE_FORMAT, withhold_exception_text=not exception_text)
    )
    return _screen_dependencies(handler, dependency_records)


def configure_server_logging(config: STMConfig) -> Path | None:
    """Root logging for the MCP server: stderr always, plus the opt-in file.

    Returns the active log-file path, or ``None`` when running stderr-only
    (unset ``log_file`` or a file that could not be opened — an opt-in
    diagnostic aid must not take the server down with it; the failure is
    logged to stderr instead).
    """
    dependency_records = config.log_dependencies
    exception_text = config.log_exception_text
    handlers: list[logging.Handler] = [
        stderr_handler(
            STDERR_FORMAT, dependency_records=dependency_records, exception_text=exception_text
        )
    ]
    active_path: Path | None = None
    file_error: str | None = None
    if config.log_file is not None:
        try:
            handlers.append(
                open_private_log_handler(
                    config.log_file,
                    dependency_records=dependency_records,
                    exception_text=exception_text,
                )
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
