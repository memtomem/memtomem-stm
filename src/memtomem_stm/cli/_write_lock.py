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
from pathlib import Path
from typing import Any, TypeVar

import click

from memtomem_stm.mms import state

F = TypeVar("F", bound=Callable[..., Any])


def stm_config_lock_path() -> Path:
    """Cross-process write lock for the proxy-config domain (#475 PR2).

    Guards every ``stm_proxy.json`` mutator plus the prune backup log
    (``pruned_upstreams.json``) — both live under ``~/.memtomem``, and the
    backup-log append is a read-modify-write that shares its span with
    config mutations, so one lock covers the domain. Deliberately a single
    fixed path rather than a per-``--config`` sidecar: two processes
    pruning different configs still append to the one shared backup log.
    Resolved at call time so monkeypatched ``HOME`` works in tests
    (mirrors ``state.mms_home``).
    """
    return Path.home() / ".memtomem" / ".stm_proxy.lock"


_CONFIG_LOCK_HOLDER_HINT = (
    "another `mms` command appears to be mutating the proxy config "
    "(possibly waiting at its confirmation prompt)"
)


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


def with_config_write_lock(
    *, skip: Callable[[dict[str, Any]], bool] | None = None
) -> Callable[[F], F]:
    """Run a Click command callback under the proxy-config write lock (#475 PR2).

    Same shape as :func:`with_write_lock` but over
    :func:`stm_config_lock_path`, serializing every ``stm_proxy.json``
    mutator (``add``/``init``/``remove``/``surfacing``/``prune``) so an
    unlocked command holding a stale load can't save over a locked one's
    result — e.g. resurrect an entry a concurrent ``mms eject`` just
    removed. Like the mms lock, the span covers the ENTIRE command
    including TTY prompts; releasing around a prompt would re-open the
    stale-load window.

    ``skip`` receives the callback kwargs and returns ``True`` for
    invocations that never write (``prune --dry-run``, ``surfacing`` with
    no state argument) — those run unlocked, mirroring the mms ``--plan``
    skip. A factory rather than a bare decorator because the skip
    predicate differs per command.
    """

    def decorate(f: F) -> F:
        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            enabled = skip is None or not skip(kwargs)
            try:
                with state.write_lock(
                    enabled=enabled,
                    lock_path=stm_config_lock_path(),
                    holder_hint=_CONFIG_LOCK_HOLDER_HINT,
                ):
                    return f(*args, **kwargs)
            except state.WriteLockTimeout as exc:
                raise click.ClickException(str(exc)) from exc

        return wrapper  # type: ignore[return-value]

    return decorate
