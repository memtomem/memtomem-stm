"""Write-lock decorator shared by the mutating mms CLI commands.

Lives in its own module rather than joining the ``mms_import`` →
``mms_host`` cross-import: the promotion trigger documented at
``_classify_against_registry`` fires when the *next domain helper* joins
that pair, and this is CLI glue around
:func:`memtomem_stm.mms.state.write_lock`, not sync/import domain logic.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

import click

from memtomem_stm.mms import state

F = TypeVar("F", bound=Callable[..., Any])


def with_write_lock(f: F) -> F:
    """Run a Click command callback under the mms cross-process write lock.

    Applied between the ``@click.option`` stack and the callback so the lock
    spans the ENTIRE command — load, classification, the TTY confirmation
    prompt, and both saves (see :func:`memtomem_stm.mms.state.write_lock`
    for why the prompt must stay inside the span). ``--plan`` invocations
    (``is_plan=True``) skip the lock: they never write, and the mutating
    side's atomic per-file writes already keep readers consistent.

    A timed-out acquisition surfaces as :class:`click.ClickException` —
    "Error: <reason>", exit code 1 — instead of a traceback.

    ``functools.wraps`` keeps the callback's docstring visible to Click's
    help renderer.
    """

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        enabled = not kwargs.get("is_plan", False)
        try:
            with state.write_lock(enabled=enabled):
                return f(*args, **kwargs)
        except state.WriteLockTimeout as exc:
            raise click.ClickException(str(exc)) from exc

    return wrapper  # type: ignore[return-value]
