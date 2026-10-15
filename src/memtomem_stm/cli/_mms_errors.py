"""Clean exits for ``~/.mms`` state errors in the ``mms`` subcommands.

Every ``mms host``/``mms project``/``mms import`` command reads state files a
user can hand-edit, so any of them can meet a :class:`state.MmsConfigError`
(corrupt TOML, a schema mismatch, a lock timeout). One decorator per command
turns that into Click's ``Error: <reason>`` exit, or the single JSON failure
document in ``--json`` mode, instead of a traceback. The commands carry it as
a marker too, so a test can require it on every command of these groups and a
new subcommand cannot quietly skip it.
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

    setattr(wrapper, MARKER, True)
    return wrapper  # type: ignore[return-value]


def exit_on_state_error(
    exc: state.MmsConfigError, kwargs: dict[str, Any], *, fallback_action: str
) -> NoReturn:
    """The one rendering of a state error, for this decorator and for
    ``with_write_lock``, whose lock is taken before the callback runs and so
    must render its own timeout the same way."""
    if kwargs.get("json_output") or kwargs.get("as_json"):
        ctx = click.get_current_context(silent=True)
        action = ctx.command.name if ctx and ctx.command.name else fallback_action
        click.echo(f"Error: {_disp(str(exc))}", err=True)
        click.echo(
            _json_dumps(
                {
                    "action": action,
                    "ok": False,
                    "error": _error_code(exc),
                    "message": str(exc),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        sys.exit(1)
    raise click.ClickException(_disp(str(exc))) from exc
