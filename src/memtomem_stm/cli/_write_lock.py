"""Write-lock decorator shared by the mutating mms CLI commands.

Lives in its own module rather than joining the ``mms_import`` →
``mms_host`` cross-import: the promotion trigger documented at
``_classify_against_registry`` fires when the *next domain helper* joins
that pair, and this is CLI glue around
:func:`memtomem_stm.mms.state.write_lock`, not sync/import domain logic.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn, TypeVar

import click

from memtomem_stm.cli._display import _disp

F = TypeVar("F", bound=Callable[..., Any])


class ConfigWriteError(click.ClickException):
    """An ``OSError`` from the proxy-config write path, after the command
    decided to write (#1176).

    Raised by ``_save`` (the atomic replace) and ``_backup_config_snapshot``
    (the pre-apply backup) in ``cli.proxy``; rendered by
    :func:`with_config_write_lock`, which every ``stm_proxy.json`` mutator
    already carries, so no caller needs its own ``except``. Both steps leave
    the config file as it was when they raise: the backup runs before the
    replace, and ``atomic_write_text`` writes a sibling temp file and only
    then renames it, so an ``OSError`` out of it means the rename did not
    happen. The message says so -- about THIS write. A command that saves
    twice (``add --from-clients --prune`` saves the import, then the prune
    marks) may already have landed its first save when the second fails.

    ``raw_message`` goes to the ``--json`` document as is; the terminal line
    carries the path display-escaped (it is operator-supplied; the sentence
    around it is ours, so only the value is escaped). Type only, no OS text:
    the OS message quotes the path (#1166 precedent).
    """

    code = "config_write_failed"

    def __init__(self, step: str, path: Path, exc: OSError) -> None:
        tail = f"({type(exc).__name__}); the file was not replaced by this write"
        self.raw_message = f"cannot {step} {path} {tail}"
        super().__init__(f"cannot {step} {_disp(str(path))} {tail}")
        self.path = path


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

    A timed-out acquisition surfaces as "Error: <reason>", exit code 1,
    instead of a traceback — or, when the command's ``--json`` flag is set,
    as the ``mms_lock_timeout`` failure document. The lock is taken before
    the callback runs, so ``surface_mms_state_errors`` on the callback never
    sees this timeout; both render through ``exit_on_state_error``.

    ``functools.wraps`` keeps the callback's docstring visible to Click's
    help renderer.
    """

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        from memtomem_stm.cli._mms_errors import exit_on_state_error
        from memtomem_stm.mms import state

        enabled = not kwargs.get("is_plan", False)
        try:
            with state.write_lock(enabled=enabled):
                return f(*args, **kwargs)
        except (state.WriteLockTimeout, state.WriteLockUnavailable) as exc:
            exit_on_state_error(exc, kwargs, fallback_action=f.__name__)

    return wrapper  # type: ignore[return-value]


def hook_hosts_lock_path() -> Path:
    """Cross-process write lock for the hook-host config domain.

    Guards the ``mms hook install``/``uninstall --apply`` read-modify-write of
    a HOST application's hook config (``~/.claude/settings.json``,
    ``~/.codex/config.toml``, …). One fixed path for the whole domain rather
    than a per-host sidecar: the span is milliseconds, contention is
    operator-initiated, and a sidecar next to the host file would litter
    foreign config directories with mms lock files. Resolved at call time so
    monkeypatched ``HOME`` works in tests (mirrors ``stm_config_lock_path``).
    """
    return Path.home() / ".memtomem" / ".hook_hosts.lock"


_HOOK_HOSTS_LOCK_HOLDER_HINT = (
    "another `mms hook install/uninstall --apply` appears to be rewriting a host hook config"
)


@contextmanager
def hook_hosts_write_lock(*, enabled: bool) -> Iterator[None]:
    """Hold the hook-host write lock for an install/uninstall plan+apply span.

    Same role as :func:`with_write_lock`, over :func:`hook_hosts_lock_path`:
    the locked span covers plan (the config read) AND apply (backup + atomic
    replace), so two concurrent ``--apply`` runs cannot both plan from the
    same base and have the loser's write clobber the winner's (lost update).
    Pass ``enabled=False`` for dry-run invocations — they never write.

    A context manager rather than a ``with_*`` decorator ON PURPOSE:
    ``hook_cmd`` is imported on the per-tool-call hook hot path, and a
    module-top decorator import would drag ``mms.state`` (pydantic) into
    every hook invocation (~+190ms measured); the command callbacks import
    this lazily instead.

    The lock only serializes *mms* processes. The host application itself
    rewrites its own config without taking it — that direction is handled by
    the staleness guard inside ``hook_hosts.apply_change``, which refuses to
    overwrite contents the plan never saw.
    """
    from memtomem_stm.mms import state

    try:
        with state.write_lock(
            enabled=enabled,
            lock_path=hook_hosts_lock_path(),
            holder_hint=_HOOK_HOSTS_LOCK_HOLDER_HINT,
        ):
            yield
    except (state.WriteLockTimeout, state.WriteLockUnavailable) as exc:
        raise click.ClickException(_disp(str(exc))) from exc


def with_config_write_lock(
    *,
    skip: Callable[[dict[str, Any]], bool] | None = None,
    json_envelope: bool = False,
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

    Timeout rendering honors the command's ``--json`` mode when the command
    opts in via ``json_envelope=True``: the lock is acquired *before* the
    callback runs, so the callback's own JSON error handling can never see
    a :class:`state.WriteLockTimeout`. For an opted-in command whose kwargs
    carry a truthy ``as_json``, the timeout is emitted as the same
    single-JSON-document envelope the commands produce (#614) instead of
    Click's plain-text error, keeping ``mms <cmd> --json | jq`` parseable
    on this failure too. Explicit opt-in rather than sniffing ``as_json``
    alone (codex #644 R2): ``mms tune`` also names its flag ``as_json`` but
    documents a *preview*-only ``--json`` and keeps text rendering. Error
    precedence under contention is unchanged from main for every mutator:
    the lock wraps the whole command, so a held lock surfaces the timeout
    before any callback-level usage validation runs — in both renderings.
    """

    def decorate(f: F) -> F:
        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            from memtomem_stm.mms import state

            enabled = skip is None or not skip(kwargs)
            try:
                with state.write_lock(
                    enabled=enabled,
                    lock_path=stm_config_lock_path(),
                    holder_hint=_CONFIG_LOCK_HOLDER_HINT,
                ):
                    return f(*args, **kwargs)
            except state.WriteLockTimeout as exc:
                _fail(f, kwargs, json_envelope, "config_lock_timeout", str(exc), cause=exc)
            except state.WriteLockUnavailable as exc:
                _fail(f, kwargs, json_envelope, "config_lock_unavailable", str(exc), cause=exc)
            except ConfigWriteError as exc:
                # Raised inside the span by ``_save`` / ``_backup_config_snapshot``
                # (#1176); same two renderings as the lock failures above.
                _fail(
                    f,
                    kwargs,
                    json_envelope,
                    exc.code,
                    exc.raw_message,
                    cause=exc,
                    shown=exc.message,
                    path=str(exc.path),
                )

        return wrapper  # type: ignore[return-value]

    return decorate


def _fail(
    f: Callable[..., Any],
    kwargs: dict[str, Any],
    json_envelope: bool,
    code: str,
    message: str,
    *,
    cause: BaseException,
    shown: str | None = None,
    **fields: Any,
) -> NoReturn:
    """Exit on a failure the config-lock span owns: the single-document
    ``--json`` envelope when the command opted in and asked for JSON, Click's
    ``Error: <message>`` otherwise. ``message`` is raw and goes to the
    document as is; the terminal gets ``shown`` when the raiser escaped the
    value itself, else ``message`` display-escaped whole (the lock errors are
    assembled in ``mms.state``, which does not import the display helper)."""
    if shown is None:
        shown = _disp(message)
    if json_envelope and kwargs.get("as_json"):
        import sys as _sys

        from memtomem_stm.utils.json_out import dumps as _json_dumps

        ctx = click.get_current_context(silent=True)
        action = ctx.command.name if ctx and ctx.command.name else f.__name__
        click.echo(f"Error: {shown}", err=True)
        click.echo(
            _json_dumps(
                {"action": action, "ok": False, "error": code, "message": message, **fields},
                indent=2,
                ensure_ascii=False,
            )
        )
        _sys.exit(1)
    raise click.ClickException(shown) from cause
