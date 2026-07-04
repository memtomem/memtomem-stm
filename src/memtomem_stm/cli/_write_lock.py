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
    try:
        with state.write_lock(
            enabled=enabled,
            lock_path=hook_hosts_lock_path(),
            holder_hint=_HOOK_HOSTS_LOCK_HOLDER_HINT,
        ):
            yield
    except state.WriteLockTimeout as exc:
        raise click.ClickException(str(exc)) from exc


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
            enabled = skip is None or not skip(kwargs)
            try:
                with state.write_lock(
                    enabled=enabled,
                    lock_path=stm_config_lock_path(),
                    holder_hint=_CONFIG_LOCK_HOLDER_HINT,
                ):
                    return f(*args, **kwargs)
            except state.WriteLockTimeout as exc:
                if json_envelope and kwargs.get("as_json"):
                    import json as _json
                    import sys as _sys

                    ctx = click.get_current_context(silent=True)
                    action = ctx.command.name if ctx and ctx.command.name else f.__name__
                    click.echo(f"Error: {exc}", err=True)
                    click.echo(
                        _json.dumps(
                            {
                                "action": action,
                                "ok": False,
                                "error": "config_lock_timeout",
                                "message": str(exc),
                            },
                            indent=2,
                            ensure_ascii=False,
                        )
                    )
                    _sys.exit(1)
                raise click.ClickException(str(exc)) from exc

        return wrapper  # type: ignore[return-value]

    return decorate
