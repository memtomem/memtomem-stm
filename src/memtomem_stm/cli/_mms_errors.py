"""Clean exits for ``~/.mms`` state errors in the ``mms`` subcommands.

Every ``mms host``/``mms project``/``mms import`` command reads state files a
user can hand-edit, so any of them can meet a :class:`state.MmsConfigError`
(corrupt TOML, a schema mismatch, a lock timeout). One decorator per command
turns that into Click's ``Error: <reason>`` exit, or the single JSON failure
document in ``--json`` mode, instead of a traceback. The commands carry it as
a marker too, so a test can require it on every command of these groups and a
new subcommand cannot quietly skip it.

The same decorator renders a :class:`CodedFailure` — a command's own
operational failure that names the ``error`` code its ``--json`` document
carries — so a ``--json`` run of ``mms project route`` keeps its one-document
contract on project-resolution failures too (#1138).
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from typing import Any, NoReturn, TypeVar

import click

from memtomem_stm.cli._display import _disp
from memtomem_stm.mms import state
from memtomem_stm.utils.json_out import dumps as _json_dumps

F = TypeVar("F", bound=Callable[..., Any])

MARKER = "_surfaces_mms_state_errors"


class CodedFailure(click.ClickException):
    """An operational failure with the stable ``error`` code scripts branch on.

    ``message`` is the raw sentence: it goes to the ``--json`` document as is
    and reaches the terminal display-escaped, so a value inside it (a project
    name, a path) must not be pre-escaped by the raiser — the same split the
    other ``--json`` failure documents keep (#1166). Without ``--json`` it is
    an ordinary :class:`click.ClickException`.

    ``json_message`` replaces ``message`` in the document when the terminal
    sentence carries detail the document must not: a validation ``msg`` can
    embed the rejected value, and stdout is what scripts and CI logs keep, so
    the document gets the location-and-type summary instead (#1051 precedent).
    """

    def __init__(
        self, code: str, message: str, *, json_message: str | None = None, **fields: Any
    ) -> None:
        super().__init__(_disp(message))
        self.code = code
        self.raw_message = message
        self.json_message = message if json_message is None else json_message
        self.fields = fields


def _error_code(exc: state.MmsConfigError) -> str:
    if isinstance(exc, state.WriteLockTimeout):
        return "mms_lock_timeout"
    if isinstance(exc, state.SchemaVersionMismatch):
        return "mms_schema_mismatch"
    return "mms_state_invalid"


def surface_mms_state_errors(f: F) -> F:
    """Render a :class:`state.MmsConfigError` from the command as a clean exit.

    The message is the exception's own text. ``CorruptedConfig`` carries the
    path plus either a validation summary (locations and error types, no
    values) or ``tomllib``'s parse error, which gives a position and at most
    one illegal character or a table name, not a value. With
    the command's ``--json`` flag set (``json_output`` or ``as_json``), stderr
    gets the human line and stdout the ``{"action", "ok": false, "error",
    "message"}`` document the other ``--json`` failures use, exit 1.
    """

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return f(*args, **kwargs)
        except state.MmsConfigError as exc:
            exit_on_state_error(exc, kwargs, fallback_action=f.__name__)
        except CodedFailure as exc:
            if not _json_mode(kwargs):
                raise
            _exit_with_failure_document(
                kwargs,
                exc.code,
                exc.json_message,
                fallback_action=f.__name__,
                shown=exc.raw_message,
                **exc.fields,
            )

    setattr(wrapper, MARKER, True)
    return wrapper  # type: ignore[return-value]


def exit_on_state_error(
    exc: state.MmsConfigError, kwargs: dict[str, Any], *, fallback_action: str
) -> NoReturn:
    """The one rendering of a state error, for this decorator and for
    ``with_write_lock``, whose lock is taken before the callback runs and so
    must render its own timeout the same way."""
    if _json_mode(kwargs):
        _exit_with_failure_document(
            kwargs, _error_code(exc), str(exc), fallback_action=fallback_action
        )
    raise click.ClickException(_disp(str(exc))) from exc


def _json_mode(kwargs: dict[str, Any]) -> bool:
    return bool(kwargs.get("json_output") or kwargs.get("as_json"))


def _exit_with_failure_document(
    kwargs: dict[str, Any],
    code: str,
    message: str,
    *,
    fallback_action: str,
    shown: str | None = None,
    **fields: Any,
) -> NoReturn:
    """stderr gets the human line (``shown``, default ``message``), stdout the
    one failure document, exit 1."""
    ctx = click.get_current_context(silent=True)
    action = ctx.command.name if ctx and ctx.command.name else fallback_action
    click.echo(f"Error: {_disp(message if shown is None else shown)}", err=True)
    click.echo(
        _json_dumps(
            {"action": action, "ok": False, "error": code, "message": message, **fields},
            indent=2,
            ensure_ascii=False,
        )
    )
    sys.exit(1)
