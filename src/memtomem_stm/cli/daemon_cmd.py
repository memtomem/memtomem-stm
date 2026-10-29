"""``mms daemon`` — manage the shared local surfacing daemon (Stage 2).

The daemon keeps one warm LTM connection for ``mms hook`` and opt-in standalone
proxy surfacing, avoiding one private LTM child per client. Subcommands:

- ``run``     — the actual long-lived server loop (foreground, or the target of
  a detached spawn via ``--detached``).
- ``start``   — spawn the daemon detached if one isn't already running.
- ``stop``    — ask a running daemon to shut down (SIGTERM by pid as fallback)
  and wait for it to finish; ``--all`` also stops live daemons under any other
  config fingerprint. Exits nonzero when a stop it started is not confirmed.
- ``status``  — running/stale/starting/unknown/stopped, with pid/port/uptime/LTM
  warmth; ``stale`` and ``starting`` also say whether the lifetime lock is held; also
  lists live daemons under a different config fingerprint and whether each
  exits on its own once idle.
- ``restart`` — ``stop`` then ``start``.

Color helpers are defined locally (not imported from ``cli.proxy``) so this
module stays import-cheap and free of an import cycle with the group that
registers it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import time
from typing import TYPE_CHECKING, Any

import click

from memtomem_stm.daemon.locking import LockProbe
from memtomem_stm.logging_setup import (
    FILE_FORMAT,
    configure_stderr_logging,
    open_private_log_handler,
    stderr_handler,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from memtomem_stm.config import STMConfig


def _color_on() -> bool:
    return "NO_COLOR" not in os.environ


def _ok(s: str) -> str:
    return click.style(s, fg="green") if _color_on() else s


def _warn(s: str) -> str:
    return click.style(s, fg="yellow") if _color_on() else s


def _load_config() -> STMConfig:
    from memtomem_stm.config import STMConfig, log_stm_config_failure

    try:
        return STMConfig()
    except Exception as exc:
        # #847 observability: this runs BEFORE _configure_logging (the config
        # names the destination), so give the line a stderr handler the way
        # server.main() does, then exit 1 — every daemon subcommand still
        # exits non-zero exactly as before, now without the interpreter's
        # second, unescaped traceback (#1173; the logged one is escaped). A
        # detached run's failure stays file-less (data_dir is unknowable from
        # a config that will not build); `mms doctor`'s env_overrides check
        # covers that hole.
        configure_stderr_logging(level=logging.WARNING)
        log_stm_config_failure(
            exc, logger=logging.getLogger(__name__), context="loading the daemon configuration"
        )
        raise SystemExit(1) from exc


def _as_int(value: Any, default: int = -1) -> int:
    """Tolerantly coerce a handshake field to int.

    ``read_handshake`` validates only that the parsed JSON is a dict — its
    docstring calls hand-edited/corrupted files an anticipated input, so
    field types are NOT guaranteed. A bare ``int(raw.get("pid", -1))``
    turned a garbage ``pid`` into an uncaught ValueError/TypeError traceback
    in the very commands whose tail branches exist to clean up bad
    handshakes. Degrade to *default* instead.

    ``bool`` is rejected explicitly: JSON ``true`` would coerce to pid 1
    (launchd/init) and could steer the SIGTERM fallback at the wrong
    process. ``OverflowError`` covers JSON ``Infinity`` — Python's
    ``json.loads`` accepts it by default, and ``int(float("inf"))`` raises
    it rather than ValueError.
    """
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value: Any, default: float) -> float:
    """Tolerant float twin of :func:`_as_int` (e.g. ``created_at``)."""
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _idle_shutdown(raw: dict[str, Any]) -> tuple[float | None, bool | None]:
    """A foreign handshake's ``idle_shutdown_seconds`` as ``(seconds, exits_when_idle)``.

    ``null`` is a daemon that never shuts down on idle: ``(None, False)``. A
    missing field (a build that predates it) or a value no daemon writes — the
    daemon writes a positive finite number or ``null``, and handshakes may be
    hand-edited — is unknown: ``(None, None)``. ``bool`` is rejected for the
    reason :func:`_as_int` gives; the range check also rejects NaN.
    """
    if "idle_shutdown_seconds" not in raw:
        return None, None
    value = raw["idle_shutdown_seconds"]
    if value is None:
        return None, False
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, None
    try:
        # An integer too large for a float is still valid JSON.
        seconds = float(value)
    except OverflowError:
        return None, None
    if not 0 < seconds < math.inf:
        return None, None
    return seconds, True


class _ForeignListingError(Exception):
    """``data_dir`` could not be listed, so the foreign daemons are unknown.

    ``kind`` is the ``OSError`` subclass name — the only detail rendered; the
    path and ``strerror`` stay out of the output.
    """

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _foreign_unknown(kind: str) -> str:
    return f"could not list data_dir ({kind}) — daemons under a different config are unknown"


def _live_foreign_daemons(config: STMConfig) -> list[dict[str, Any]]:
    """Daemons running under a *different* config fingerprint than the current one.

    ``stop``/``status`` key to the *current* fingerprint, so these are invisible
    to them otherwise. A foreign daemon is not necessarily stale (#1195):
    processes on one machine may run with different ``MEMTOMEM_STM_*``
    environments on purpose, each with its own live daemon. The stale kind is
    left when a fingerprinted field — or ``PROTOCOL_VERSION``, which is folded
    into the fingerprint so *every* protocol bump counts — changes while a daemon
    is running. A daemon with an idle shutdown clears itself once nothing uses
    it; one without (``idle_timeout_seconds`` 0 or ``inf``) lingers. Each entry
    carries ``exits_when_idle`` — ``True``/``False``, or ``None`` when the
    handshake does not say (see :func:`_idle_shutdown`) — so callers can tell
    which kind they may be looking at.

    Liveness is **two-factor**: the recorded pid must exist
    (:func:`is_pid_alive`) *and* a bare TCP connect to its endpoint must succeed
    (:func:`~memtomem_stm.daemon.client.probe_listening`). A foreign daemon may
    speak an older protocol, so a token ``ping`` would fail the version gate —
    but it is still *accepting*, so a connect-probe reaches it without sending a
    frame. The pid alone is too weak: a daemon that crashed (SIGKILL/OOM, not a
    graceful teardown) leaves its handshake on disk, and the OS may recycle its
    pid to an unrelated process that would then look "alive". Requiring the
    endpoint to also accept rejects that recycled-pid case (its port is not
    bound) — a strictly stronger check than pid alone (the residual is a double
    coincidence: the pid recycled *and* its ephemeral port reassigned to another
    listener). Each entry carries only non-sensitive fields
    (``fingerprint``/``pid``/``host``/``port``/``idle_shutdown_seconds``/
    ``exits_when_idle``) — never the auth ``token`` — so callers can render them.

    Cross-platform: the connect-probe is what makes Windows viable too, where
    :func:`is_pid_alive` is uninformative (returns ``True`` for any positive pid,
    no signal-0) — there the connect carries the whole signal (#519).

    A record that fails the two-factor check is still listed when its
    fingerprint's lifetime lock is not free (#1245): a daemon shutting down has
    closed its listener before its teardown removes the handshake, and one
    starting holds the lock before it publishes over a predecessor's record —
    so this covers dead, alive and unreadable recorded pids alike. Those
    entries carry ``listening: False`` and ``lock`` (``"held"``/``"unknown"``);
    an unreadable record also has ``handshake_unreadable: True`` and ``None``
    for every field it would have supplied. The lock proves *some* process owns
    the fingerprint, not that the recorded pid does, so nothing signals them.
    Accepting entries carry ``listening: True`` and ``lock: None`` (not
    checked).

    Raises :class:`_ForeignListingError` when ``data_dir`` cannot be listed, so
    no caller reads "could not look" as "none" (#1249). Only the enumeration is
    translated; the per-entry probes report their own failures.
    """
    from memtomem_stm.daemon.client import probe_listening
    from memtomem_stm.daemon.discovery import (
        config_fingerprint,
        is_pid_alive,
        iter_foreign_handshakes,
    )
    from memtomem_stm.daemon.locking import lock_path

    current = config_fingerprint(config)
    try:
        handshakes = iter_foreign_handshakes(config.data_dir, current, include_unreadable=True)
    except OSError as exc:
        raise _ForeignListingError(type(exc).__name__) from exc
    live: list[dict[str, Any]] = []
    for fingerprint, hs in handshakes:
        if hs is None:
            state = _probe_lock(lock_path(config.data_dir, fingerprint))
            if state is not LockProbe.FREE:
                live.append(
                    {
                        "fingerprint": fingerprint,
                        "pid": None,
                        "host": None,
                        "port": None,
                        "idle_shutdown_seconds": None,
                        "exits_when_idle": None,
                        "listening": False,
                        "lock": state.value,
                        "handshake_unreadable": True,
                    }
                )
            continue
        pid = _as_int(hs.get("pid", -1))
        host = hs.get("host")
        port = hs.get("port")
        idle_seconds, exits_when_idle = _idle_shutdown(hs)
        entry: dict[str, Any] = {
            "fingerprint": fingerprint,
            "pid": pid,
            "host": host,
            "port": port,
            "idle_shutdown_seconds": idle_seconds,
            "exits_when_idle": exits_when_idle,
        }
        # Cheap pid filter first (drops dead/garbage pids and, on POSIX, the
        # bulk of stale handshakes without a connect); the probe then confirms
        # something still accepts on the recorded endpoint.
        if is_pid_alive(pid) and probe_listening(host, port):
            live.append({**entry, "listening": True, "lock": None})
            continue
        state = _probe_lock(lock_path(config.data_dir, fingerprint))
        if state is not LockProbe.FREE:
            live.append({**entry, "listening": False, "lock": state.value})
    return live


def _configure_logging(config: STMConfig, *, detached: bool) -> None:
    """Route daemon logs to a file under ``data_dir`` when detached (its stdio
    is ``DEVNULL``), otherwise to stderr for foreground debugging.

    The detached file goes through the shared #612 handler: 0o600 file /
    0o700 parent per the data-at-rest convention, and size rotation so the
    #581 crash-trace guarantee doesn't eventually drown in an unbounded log.
    A failure to open it propagates — with stdio devnulled, a daemon that
    cannot log its crashes must not run. (The server path degrades to
    stderr instead; see ``configure_server_logging``.)
    """
    level = getattr(logging, config.log_level, logging.WARNING)
    handler: logging.Handler
    dependency_records = config.log_dependencies
    exception_text = config.log_exception_text
    if detached:
        handler = open_private_log_handler(
            (config.data_dir / "stm-daemon.log").expanduser(),
            dependency_records=dependency_records,
            exception_text=exception_text,
        )
    else:
        handler = stderr_handler(
            FILE_FORMAT, dependency_records=dependency_records, exception_text=exception_text
        )
    logging.basicConfig(level=level, handlers=[handler], force=True)


@click.group(name="daemon")
def daemon_group() -> None:
    """Manage the shared local surfacing daemon."""


@daemon_group.command(name="run")
@click.option(
    "--detached",
    is_flag=True,
    help="Spawned-detached mode: log to a file under data_dir instead of stderr.",
)
@click.option(
    "--foreground",
    is_flag=True,
    help="Run in the foreground (default). Kept for symmetry/clarity.",
)
def run_cmd(detached: bool, foreground: bool) -> None:
    """Run the daemon server loop — this is the long-lived process."""
    from memtomem_stm.daemon.server import run as run_daemon

    config = _load_config()
    _configure_logging(config, detached=detached)
    raise SystemExit(run_daemon(config))


# Cap on detached children `mms daemon start` may launch in one invocation.
# A crashed child releases the lifetime lock, making "lock free" ambiguous
# (mid-shutdown handoff vs crash loop); 3 covers the handoff with margin while
# a persistently-failing config produces a handful of children, not dozens.
_START_MAX_SPAWNS = 3


def _can_force_terminate() -> bool:
    """Whether pid-based POSIX SIGTERM fallback is available."""
    return os.name != "nt"


@daemon_group.command(name="start")
def start_cmd() -> None:
    """Spawn the daemon detached if one isn't already running for this config.

    Reconciles toward a running daemon *for the current config*. The daemon
    self-guards via its (per-config) lifetime lock, so ``start`` never holds the
    lock. The lock may briefly be held by a *same-config* daemon that is
    *shutting down* (e.g. right after ``mms daemon stop``): it won't answer ping
    and will soon release the lock, so we keep retrying the spawn until it frees
    rather than polling ping once and giving up. A daemon started under a
    *different* config owns a different lock + handshake (config-drift
    coexistence), so it never blocks this spawn and is left untouched — there is
    no cross-config conflict to report.
    """
    from memtomem_stm.daemon import client
    from memtomem_stm.daemon.spawn import request_spawn

    config = _load_config()
    existing = asyncio.run(client.ping(config, timeout=2.0))
    if existing is not None:
        click.echo(
            _ok(f"daemon already running (pid={existing.get('pid')} port={existing.get('port')})")
        )
        return

    deadline = time.time() + 10.0
    spawns = 0
    spawn_error: Exception | None = None
    launched_any = False
    while time.time() < deadline:
        hs = asyncio.run(client.ping(config, timeout=1.0))
        if hs is not None:
            click.echo(_ok(f"daemon started (pid={hs.get('pid')} port={hs.get('port')})"))
            return
        # request_spawn launches a detached child iff our config's lock is free;
        # it declines when a same-config daemon already holds it (mid-startup →
        # ping succeeds soon, or mid-shutdown → the lock frees and the next
        # attempt wins). We never hold the lock, so the spawned child can take
        # ownership. BUT a free lock can also mean the child we just spawned
        # crashed on startup (bad config) — without a cap, a crash-looping
        # config would fire a detached child every 0.3s for the whole window
        # (~33 processes). Cap actual spawns and keep only ping-polling
        # afterwards; the mid-shutdown handoff needs at most one respawn after
        # the old daemon releases. A spawn that *raised* (no thread, no fork)
        # spends the same budget: a fork that cannot fork will not start
        # forking, and unbudgeted retries would bury the cause under a
        # ~33-deep stack of the same traceback.
        if spawns < _START_MAX_SPAWNS:
            try:
                launched = request_spawn(config, propagate_errors=True)
            except (OSError, RuntimeError) as exc:
                spawn_error = exc
                spawns += 1
            else:
                launched_any = launched_any or launched
                spawns += int(launched)
        time.sleep(0.3)
    # A child that got as far as exec is the one that can have logged, whichever
    # attempt it was, so report the fork failure only when *no* attempt ever
    # launched one. Reported the other way round, either mix sends the operator
    # somewhere the answer is not — to a log nothing could have written, or past
    # a child that ran. The log is where to *look*, not a promise: a child that
    # dies before ``run_cmd`` configures logging leaves nothing in it.
    detail = (
        f"could not spawn it: {spawn_error}"
        if spawn_error is not None and not launched_any
        else "check the daemon log under data_dir (stm-daemon.log)"
    )
    click.echo(_warn(f"daemon did not become ready in time — {detail}"), err=True)
    raise SystemExit(1)


@daemon_group.command(name="stop")
@click.option(
    "--all",
    "stop_all",
    is_flag=True,
    help="Also stop every daemon running under a different config fingerprint, "
    "e.g. one that never exits when idle, left after a config or PROTOCOL_VERSION "
    "change. Off by default: a live daemon under another config may belong to an STM "
    "process started with a different environment. Targets are gated by a "
    "connect-probe so a stale handshake naming a recycled pid is not signalled.",
)
def stop_cmd(stop_all: bool) -> None:
    """Ask a running daemon to shut down gracefully.

    By default acts only on the daemon for the *current* config. ``--all`` also
    SIGTERMs every live daemon under a different config fingerprint — e.g. a
    pinned ``idle_timeout_seconds=0`` daemon left by a config or
    ``PROTOCOL_VERSION`` change, which ``stop`` cannot otherwise reach. Not all
    of them are stale: one may serve an STM process with a different environment.

    Exits 1 when a stop it started is not confirmed (its handshake not
    removed) or could not be started, and when a daemon of this config holds
    its lifetime lock without accepting (starting, or shutting down past the
    wait). A record is cleaned up as stale only while that lock is free, so a
    daemon already shutting down on its own is waited for, not reported gone.
    """
    config = _load_config()
    current_stop_error: SystemExit | None = None
    try:
        _stop_current_config_daemon(config)
    except SystemExit as exc:
        current_stop_error = exc
    if stop_all:
        # Runs even after a failed current-config stop; either half failing
        # fails the command.
        _stop_foreign_daemons(config)
    if current_stop_error is not None:
        raise current_stop_error


def _handshake_removed(path: Path) -> bool:
    """``True`` only when the handshake file is really gone.

    Also asked of a lifetime lock file, before probing it would create one.

    ``read_handshake`` folds absent, unreadable and malformed into ``None``,
    which is what its readers want but the wrong question for a terminal
    condition: teardown removes the file last, so a handshake we merely cannot
    parse — a truncated write, a transient read error — must keep us waiting
    rather than announce a stop that may not have happened (#954).

    ``lstat`` rather than ``stat``: the question is whether the directory entry
    teardown removes is gone, and following a link would answer for its target
    instead — a dangling symlink left in place would read as removed.
    """
    try:
        path.lstat()
    except FileNotFoundError:
        # ``lstat`` does not follow the entry itself, but it still resolves
        # everything above it — a broken ancestor raises the same error while
        # saying nothing about whether teardown removed the handshake. Only an
        # ancestor that EXISTS proves the path was walkable and the entry
        # really is gone; anything else leaves the question unanswered, so the
        # wait stays open. ``is_junction`` covers the Windows spelling of a
        # link, which ``is_symlink`` reports as False.
        #
        # The walk carries its own guard: an error raised in here is raised
        # INSIDE this handler, where the ``except OSError`` below cannot see
        # it, and it would leave the command as a traceback rather than as the
        # conservative answer everything else in this function gives.
        try:
            for ancestor in path.parents:
                if ancestor.exists():
                    return True
                if ancestor.is_symlink() or ancestor.is_junction():
                    # An entry that is there but does not resolve. ``exists``
                    # has to be asked first: a WORKING link resolves, and
                    # treating it as unwalkable would hang the wait on every
                    # such layout.
                    return False
        except OSError:
            return False
        return False
    except OSError:
        return False
    return False


# How long ``stop`` waits for a daemon it asked or signalled to finish its
# teardown. One budget for every daemon in a call, not one each.
_STOP_WAIT_SECONDS = 5.0


def _wait_handshakes_removed(paths: list[Path]) -> list[Path]:
    """Poll until every handshake in *paths* is removed or the budget runs out.

    Returns the ones still present — empty means every daemon confirmed its
    stop. The handshake is the terminal condition for the SIGTERM paths too:
    the signal handler sets the same shutdown event the graceful op does, and
    teardown removes the handshake after the engine, stores and LTM child are
    down (every build since the daemon was introduced does this last).
    """
    pending = list(paths)
    deadline = time.time() + _STOP_WAIT_SECONDS
    while True:
        pending = [p for p in pending if not _handshake_removed(p)]
        if not pending or time.time() >= deadline:
            return pending
        time.sleep(0.05)


def _probe_lock(path: Path) -> LockProbe:
    """One instantaneous look at a lifetime lock, released before returning."""
    from memtomem_stm.daemon.locking import probe_lifetime_lock

    with probe_lifetime_lock(path) as state:
        return state


def _wait_lock_free(path: Path, on_free: Callable[[], None] | None = None) -> LockProbe:
    """Poll the lifetime lock at *path* until it is free or the stop budget runs out.

    Returns :attr:`LockProbe.FREE`, or the last non-free answer on timeout.
    ``on_free`` runs while the lock is held, so whatever it does to the
    handshake cannot race a successor: one starting meanwhile waits for the
    lock before it publishes. Never sleeps while holding it.

    The lock, not the handshake, is the terminal condition here: a daemon
    releases its lifetime lock only once ``serve`` has returned, after
    teardown, so a handshake already gone while the lock is held is a daemon
    still finishing — or one that has not published yet (#1245).
    """
    from memtomem_stm.daemon.locking import probe_lifetime_lock

    deadline = time.time() + _STOP_WAIT_SECONDS
    while True:
        with probe_lifetime_lock(path) as state:
            if state is LockProbe.FREE:
                if on_free is not None:
                    on_free()
                return state
        if time.time() >= deadline:
            return state
        time.sleep(0.05)


def _stop_current_config_daemon(config: STMConfig) -> None:
    """Stop the daemon for *this* config (graceful, then SIGTERM-by-pid fallback)."""
    from memtomem_stm.daemon import client
    from memtomem_stm.daemon.discovery import (
        config_fingerprint,
        handshake_path,
        is_pid_alive,
        read_handshake,
    )
    from memtomem_stm.daemon.locking import lock_path

    fp = config_fingerprint(config)
    hs_path = handshake_path(config.data_dir, fp)
    lock = lock_path(config.data_dir, fp)
    raw = read_handshake(hs_path)
    if asyncio.run(client.shutdown(config)):
        # The wire response means only "shutdown accepted". Teardown still has
        # to stop the engine/LTM child before it removes the handshake, so do
        # not tell scripts the daemon is stopped until that record is gone.
        #
        # The handshake alone is the terminal condition (#954). It is also the
        # strictest one: the daemon closes its listener when the serve block
        # exits, then tears down engine, tracker and LTM child, and removes the
        # handshake LAST — so an absent handshake already implies an absent
        # endpoint. Probing the recorded port on top of that asked about a
        # host/port that identified this daemon only at the moment we read it;
        # an ephemeral port the OS reassigned to an unrelated listener inside
        # this window kept the probe true forever and reported a clean stop as
        # a failure.
        if not _wait_handshakes_removed([hs_path]):
            click.echo(_ok("daemon stopped"))
            return
        click.echo(
            _warn(
                "daemon accepted shutdown but is still cleaning up — "
                "run `mms daemon status` before starting a replacement"
            ),
            err=True,
        )
        raise SystemExit(1)
    # Graceful path declined → no daemon for *this config*. Only ever act on our
    # own config's handshake; a different-config daemon owns a different file and
    # is handled by ``--all`` (``_stop_foreign_daemons``), not here.
    if raw is None:
        # ``raw`` is None for a handshake that is absent, unreadable OR
        # malformed, and only the first of those means no daemon. A record we
        # could not parse may belong to one that simply declined the graceful
        # shutdown, so claiming success here is what sends someone off to start
        # a replacement beside it (#954) — the same conflation the wait above
        # and ``status`` no longer make.
        if not _handshake_removed(hs_path):
            click.echo(
                _warn(
                    "a handshake is present but unreadable, so whether a daemon is "
                    "running cannot be determined — run `mms daemon status`"
                ),
                err=True,
            )
            raise SystemExit(1)
        # No handshake is not yet no daemon: one starting holds its lifetime
        # lock while it builds the engine, before it publishes, and one
        # finishing its teardown holds it until after the handshake is gone
        # (#1245). A lock file that does not exist has no holder; checking
        # first keeps a bare ``stop`` from creating the data dir. Asked with
        # ``_handshake_removed`` (an ``lstat`` of the entry) rather than
        # ``exists``, which reads a broken link as absent: that entry is there,
        # and the probe — not this check — must answer for it.
        if not _handshake_removed(lock):
            state = _wait_lock_free(lock)
            if state is LockProbe.HELD:
                click.echo(
                    _warn(
                        "no handshake, but this config's daemon lock is held — a daemon "
                        "is starting or shutting down; run `mms daemon status`"
                    ),
                    err=True,
                )
                raise SystemExit(1)
            if state is LockProbe.UNKNOWN:
                click.echo(
                    _warn(
                        "no handshake, and this config's daemon lock could not be "
                        "checked — run `mms daemon status`"
                    ),
                    err=True,
                )
                raise SystemExit(1)
        click.echo("no running daemon")
        return
    pid = _as_int(raw.get("pid", -1))
    # Graceful shutdown declined, but a handshake remains. Only SIGTERM when the
    # endpoint still accepts a connect — a bare probe (no token) tells "daemon
    # there but unresponsive to OP_SHUTDOWN" from "stale handshake naming a dead
    # or OS-recycled pid", so we don't signal an unrelated process. Replaces the
    # old POSIX-only ``is_pid_alive`` gate, which over-reported on Windows; the
    # probe makes the fallback safe cross-platform (#519).
    if is_pid_alive(pid) and client.probe_listening(raw.get("host"), raw.get("port")):
        if not _can_force_terminate():
            click.echo(
                _warn(
                    "daemon endpoint is still listening but rejected graceful shutdown; "
                    "Windows does not provide the POSIX SIGTERM fallback. Close the owning "
                    "terminal/process, then run `mms daemon status`."
                ),
                err=True,
            )
            raise SystemExit(1)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # exited between the probe and the signal: the stale case below
        except OSError as exc:
            # Alive (``is_pid_alive`` counts EPERM as alive) and listening, but
            # not ours to signal: the record is not stale, so keep it.
            click.echo(
                _warn(f"could not signal daemon pid={pid} ({type(exc).__name__})"),
                err=True,
            )
            raise SystemExit(1)
        else:
            # The signal only starts the same teardown the graceful path waits
            # for, so wait for the same terminal condition before saying so.
            click.echo(f"sent SIGTERM to daemon pid={pid}")
            if not _wait_handshakes_removed([hs_path]):
                click.echo(_ok("daemon stopped"))
                return
            click.echo(
                _warn(
                    f"daemon pid={pid} did not finish shutting down after SIGTERM — "
                    "run `mms daemon status` before starting a replacement"
                ),
                err=True,
            )
            raise SystemExit(1)
    # Not accepting is not stale: a daemon shutting down on its own has closed
    # its listener before its teardown removes the handshake, and one starting
    # holds the lock before it publishes over a predecessor's record. Only a
    # free lifetime lock says no daemon owns this config, and the record goes
    # while we hold it, so a successor cannot publish in between (#1245).
    outcome: list[str] = []

    def settle() -> None:
        if _handshake_removed(hs_path):
            outcome.append("stopped")
            return
        try:
            hs_path.unlink(missing_ok=True)
        except OSError:
            pass
        outcome.append("stale")

    state = _wait_lock_free(lock, settle)
    if state is LockProbe.HELD:
        click.echo(
            _warn(
                f"recorded daemon pid={pid} is not accepting, but this config's daemon lock "
                "is still held — a daemon is starting or shutting down; run "
                "`mms daemon status`"
            ),
            err=True,
        )
        raise SystemExit(1)
    if state is LockProbe.UNKNOWN:
        click.echo(
            _warn(
                f"recorded daemon pid={pid} is not accepting, and this config's daemon lock "
                "could not be checked — run `mms daemon status`"
            ),
            err=True,
        )
        raise SystemExit(1)
    if outcome == ["stopped"]:
        # It was finishing a teardown of its own; the daemon removed its
        # record and released the lock.
        click.echo(_ok("daemon stopped"))
        return
    click.echo(_warn(f"no responsive daemon pid={pid}; cleaned stale handshake"))


def _foreign_name(d: dict[str, Any]) -> str:
    """``pid=N (fp=…)`` for a foreign entry; ``pid=?`` when its record is unreadable."""
    pid = "?" if d["pid"] is None else d["pid"]
    return f"pid={pid} (fp={d['fingerprint']})"


def _foreign_stopped(d: dict[str, Any]) -> str:
    pid = "?" if d["pid"] is None else d["pid"]
    return f"daemon pid={pid} stopped (fp={d['fingerprint']})"


def _stop_foreign_daemons(config: STMConfig) -> None:
    """SIGTERM every live daemon under a *different* config fingerprint (``--all``).

    SIGTERM (not the socket shutdown op) because a foreign daemon may speak an
    older protocol the graceful path can't negotiate; the daemon's signal handler
    sets its shutdown event regardless of ``idle_timeout_seconds``, so even a
    pinned daemon exits. The daemon removes its own handshake on teardown, so we
    do not unlink it here (that could hide a survivor that ignored the signal) —
    its removal is what the wait after signalling counts as the stop. Exits 1
    when any target could not be signalled or did not finish within the budget,
    and on Windows for every target that would need a signal.

    Targets come from :func:`_live_foreign_daemons`, whose two-factor check
    (recorded pid alive *and* its endpoint accepting a connect) already rejects a
    crash-left handshake naming a since-recycled pid — so ``--all`` does not
    signal an unrelated process in the common case. The residual is a double
    coincidence (pid recycled *and* its ephemeral port reassigned to another
    listener); ``--all`` stays opt-in for that narrow window.

    A target that is not accepting but whose fingerprint's lifetime lock is
    held — listed that way, or found that way by the action-time re-probe — is
    never signalled: the lock does not say the recorded pid is its holder. It is
    waited on instead, and it counts as stopped once that lock is free (#1245).
    A daemon that was starting rather than stopping keeps the lock, so the wait
    times out and the command exits 1 rather than signal it.
    """
    from memtomem_stm.daemon import client
    from memtomem_stm.daemon.discovery import handshake_path, is_pid_alive
    from memtomem_stm.daemon.locking import lock_path

    try:
        foreign = _live_foreign_daemons(config)
    except _ForeignListingError as exc:
        click.echo(
            _warn(f"{_foreign_unknown(exc.kind)}; no foreign daemons were stopped"), err=True
        )
        raise SystemExit(1) from None
    if not foreign:
        click.echo("no daemons running under a different config")
        return
    can_signal = _can_force_terminate()
    # Signal every target first, then wait on all of them against one budget:
    # signalled ones until their handshake is removed, the others until their
    # lifetime lock is free.
    signalled: dict[Path, dict[str, Any]] = {}
    lock_waits: dict[Path, dict[str, Any]] = {}
    failed = False
    unsignallable = False
    for d in foreign:
        fingerprint = d["fingerprint"]
        hs_path = handshake_path(config.data_dir, fingerprint)
        lp = lock_path(config.data_dir, fingerprint)
        if d["listening"]:
            pid = d["pid"]
            # Re-confirm the *same* two-factor gate at action time, not just
            # `is_pid_alive`: the daemon may have exited since enumeration and the
            # OS recycled its pid onto an unrelated process. A pid-only recheck is
            # too weak for that race (and a no-op on Windows, where is_pid_alive
            # is always True), so re-run the connect-probe — a recycled pid's
            # endpoint is gone. Runs before the platform check, so a target that
            # no longer needs a signal is not reported as one Windows cannot send.
            if not (is_pid_alive(pid) and client.probe_listening(d["host"], d["port"])):
                state = _probe_lock(lp)
                if state is LockProbe.FREE:
                    if _handshake_removed(hs_path):
                        click.echo(_ok(_foreign_stopped(d)))
                    else:
                        click.echo(
                            f"daemon {_foreign_name(d)} is no longer accepting and its "
                            "lock is free; left its record as stale"
                        )
                    continue
                # Closed its listener since enumeration but still owns the
                # fingerprint: shutting down (or a successor starting).
                lock_waits[lp] = d
                continue
            if not can_signal:
                unsignallable = True
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                # Gone before the signal, but its handshake may still be there:
                # a daemon that crashed leaves it, so it joins the wait rather
                # than counting as stopped.
                signalled[hs_path] = d
            except OSError:
                click.echo(_warn(f"could not signal daemon {_foreign_name(d)}"), err=True)
                failed = True
            else:
                click.echo(f"sent SIGTERM to daemon {_foreign_name(d)}")
                signalled[hs_path] = d
        else:
            lock_waits[lp] = d
    for d in lock_waits.values():
        click.echo(
            f"daemon {_foreign_name(d)} is not accepting but its lock is not free — "
            "waiting for it without a signal"
        )
    if unsignallable:
        click.echo(
            _warn(
                "found daemon(s) under a different config, but Windows cannot safely "
                "SIGTERM them by pid. Stop the owning processes from Task Manager or "
                "restart Windows, then run `mms daemon status`."
            ),
            err=True,
        )
        failed = True
    remaining_hs, remaining_locks = _wait_foreign_stops(list(signalled), list(lock_waits))
    for path, d in signalled.items():
        if path in remaining_hs:
            click.echo(
                _warn(f"daemon {_foreign_name(d)} did not finish shutting down after SIGTERM"),
                err=True,
            )
        else:
            click.echo(_ok(_foreign_stopped(d)))
    for path, d in lock_waits.items():
        if remaining_locks.get(path) is LockProbe.UNKNOWN:
            click.echo(
                _warn(
                    f"daemon {_foreign_name(d)} is not accepting and its lock could not be "
                    "checked — run `mms daemon status`"
                ),
                err=True,
            )
        elif path in remaining_locks:
            click.echo(
                _warn(
                    f"daemon {_foreign_name(d)} still holds its lock — it is starting or "
                    "shutting down; run `mms daemon status`"
                ),
                err=True,
            )
        else:
            click.echo(_ok(_foreign_stopped(d)))
    if failed or remaining_hs or remaining_locks:
        raise SystemExit(1)


def _wait_foreign_stops(
    handshakes: list[Path], locks: list[Path]
) -> tuple[set[Path], dict[Path, LockProbe]]:
    """One shared budget: wait until every handshake is removed and every lock
    is free. Returns the handshakes still present and, for each lock still not
    free, its last answer (``HELD`` or ``UNKNOWN``) — both empty when every
    target confirmed."""
    pending_hs = list(handshakes)
    pending_locks = {p: LockProbe.HELD for p in locks}
    deadline = time.time() + _STOP_WAIT_SECONDS
    while True:
        pending_hs = [p for p in pending_hs if not _handshake_removed(p)]
        for p in list(pending_locks):
            state = _probe_lock(p)
            if state is LockProbe.FREE:
                del pending_locks[p]
            else:
                pending_locks[p] = state
        if (not pending_hs and not pending_locks) or time.time() >= deadline:
            return set(pending_hs), pending_locks
        time.sleep(0.05)


# (snapshot key, status-line label) for every surface-series counter. The
# status line switches on when any of these is nonzero and prints all of them:
# a line switched on by an observation it does not show reads as a positive
# report that nothing happened (#994).
_SURFACE_COUNTERS = (
    ("samples", "samples"),
    ("timeout_samples", "timeouts"),
    ("error_samples", "errors"),
    ("cold_samples", "cold"),
    ("pre_rpc_faults", "pre_rpc_faults"),
)


@daemon_group.command(name="status")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON for scripting.")
def status_cmd(as_json: bool) -> None:
    """Report whether a daemon is running, with pid/port/uptime/LTM warmth."""
    from memtomem_stm.daemon import client
    from memtomem_stm.daemon.discovery import (
        config_fingerprint,
        handshake_path,
        is_pid_alive,
        read_handshake,
    )
    from memtomem_stm.daemon.locking import lock_path

    config = _load_config()
    # The hook only reaches the daemon for eligible surfacing calls, so global
    # surfacing-off means it never will — regardless of the use_daemon knob.
    surfacing_on = config.surfacing.enabled
    use_daemon = config.hook.use_daemon and surfacing_on
    standalone_use_daemon = config.surfacing.use_daemon and surfacing_on
    hs = asyncio.run(client.ping(config, timeout=2.0))
    if hs is not None:
        uptime = max(0.0, time.time() - _as_float(hs.get("created_at"), time.time()))
        info: dict[str, Any] = {
            "state": "running",
            "pid": hs.get("pid"),
            "host": hs.get("host"),
            "port": hs.get("port"),
            "ltm": hs.get("ltm"),
            "queue": hs.get("queue"),
            # The percentiles and the derived hook-timeout recommendation. The
            # daemon has always reported them on the wire; dropping them here
            # left `--json` without the numbers the concurrency and timeout
            # guidance tells operators to tune from (#874).
            "latency": hs.get("latency"),
            "uptime_seconds": round(uptime, 1),
            "hook_will_use_daemon": use_daemon,
            "standalone_will_use_daemon": standalone_use_daemon,
        }
    else:
        fp = config_fingerprint(config)
        hs_path = handshake_path(config.data_dir, fp)
        lp = lock_path(config.data_dir, fp)
        raw = read_handshake(hs_path)
        if raw is not None:
            # Not answering a ping is not stale on its own: a daemon starting
            # or finishing its teardown holds its lifetime lock (#1245).
            info = {
                "state": "stale",
                "pid": raw.get("pid"),
                "pid_alive": is_pid_alive(_as_int(raw.get("pid", -1))),
                "lock": _probe_lock(lp).value,
                "hook_will_use_daemon": use_daemon,
                "standalone_will_use_daemon": standalone_use_daemon,
            }
        elif not _handshake_removed(hs_path):
            # A record we cannot parse is still a record: the daemon that owns
            # it may be mid-teardown or merely unresponsive to a ping, and
            # teardown removes this file last. Calling that "stopped" is what
            # sends someone off to start a replacement beside a live daemon —
            # and it is the answer `stop` now sends them here to get (#954).
            # No pid to report: reading it is the thing that failed.
            info = {
                "state": "stale",
                "handshake_unreadable": True,
                "lock": _probe_lock(lp).value,
                "hook_will_use_daemon": use_daemon,
                "standalone_will_use_daemon": standalone_use_daemon,
            }
        else:
            # No handshake: a daemon that has not published yet, or one past
            # removing it, still holds the lock. A lock file that does not
            # exist has no holder, and probing it would create it (see the
            # same check in ``_stop_current_config_daemon``).
            lock_state = LockProbe.FREE if _handshake_removed(lp) else _probe_lock(lp)
            if lock_state is LockProbe.FREE:
                info = {"state": "stopped"}
            elif lock_state is LockProbe.HELD:
                info = {"state": "starting", "lock": lock_state.value}
            else:
                info = {"state": "unknown", "lock": lock_state.value}
            info["hook_will_use_daemon"] = use_daemon
            info["standalone_will_use_daemon"] = standalone_use_daemon

    # Orthogonal to the current-config state: daemons under a different
    # fingerprint are invisible to the keyed paths above, so surface them here
    # regardless of running/stale/stopped. ``None`` when they could not be
    # listed — unknown, which an empty list would misreport as none (#1249).
    foreign: list[dict[str, Any]] | None
    foreign_error: str | None = None
    try:
        foreign = _live_foreign_daemons(config)
    except _ForeignListingError as exc:
        foreign, foreign_error = None, exc.kind

    if as_json:
        info["foreign"] = foreign
        if foreign_error is not None:
            info["foreign_error"] = foreign_error
        click.echo(json.dumps(info, indent=2))
        return

    state = info["state"]
    if state == "running":
        click.echo(
            _ok(
                f"running  pid={info['pid']} {info['host']}:{info['port']} "
                f"ltm={info['ltm']} uptime={info['uptime_seconds']}s"
            )
        )
        queue = info.get("queue")
        if isinstance(queue, dict):
            # Pre-auth evictions are dropped before admission, so the hook sees
            # an unreachable daemon rather than ``busy``; this line is the
            # operator's only plain-text sign of them.
            click.echo(
                "queue: "
                f"active={queue.get('active', 0)} "
                f"in_flight={queue.get('in_flight', 0)}/{queue.get('concurrency', 1)} "
                f"queued={queue.get('queued', 0)} "
                f"capacity={queue.get('capacity', 0)} "
                f"busy_rejections={queue.get('busy_rejections', 0)} "
                f"pre_auth_evictions={queue.get('pre_auth_evictions', 0)}"
            )
        surface = (info.get("latency") or {}).get("surface")
        if isinstance(surface, dict) and any(surface.get(key) for key, _ in _SURFACE_COUNTERS):
            recommendation = surface.get("recommendation") or {}
            # Rendered on any observation, not only on a successful one. A
            # round trip that failed mid-flight is filed as an error rather
            # than a success duration (#994), so gating the whole line on
            # ``samples`` hid the operator's only status-line view of a daemon
            # whose every search is failing. Every counter that can turn the
            # line on is also printed on it -- a line switched on by an
            # observation it does not show is worse than no line, since it
            # reads as a positive report that nothing happened.
            # A counter the snapshot does not carry is unmeasured, not zero:
            # a daemon built before #994 has no ``pre_rpc_faults`` at all, and
            # printing 0 there would report an absence as a clean bill of
            # health.
            summary = "surface latency: " + " ".join(
                f"{label}={surface[key]}" if key in surface else f"{label}=?"
                for key, label in _SURFACE_COUNTERS
            )
            if surface.get("samples"):
                summary += (
                    f" p50={_as_float(surface.get('p50_ms'), 0.0):.0f}ms"
                    f" p95={_as_float(surface.get('p95_ms'), 0.0):.0f}ms"
                    f" suggested_timeout={recommendation.get('seconds', '?')}s"
                )
            else:
                # Percentiles over an empty window render as 0ms, which reads
                # as "instant" -- the opposite of what this daemon is doing.
                # "warm" is load-bearing: a cold-only daemon may well have
                # completed searches, just none the percentiles accept.
                summary += " no successful warm searches yet"
            click.echo(summary)
    elif state == "stale" and info.get("handshake_unreadable"):
        # No pid line: reading the record is what failed.
        click.echo(_warn("handshake present but unreadable — daemon state unknown"))
    elif state == "stale" and info["lock"] == LockProbe.HELD.value:
        click.echo(
            _warn(
                f"daemon not answering (recorded pid={info['pid']}), lock held — "
                "starting or shutting down"
            )
        )
    elif state == "stale" and info["lock"] == LockProbe.UNKNOWN.value:
        click.echo(
            _warn(
                f"daemon not answering (recorded pid={info['pid']}), and its lock could "
                "not be checked — state unknown"
            )
        )
    elif state == "stale":
        click.echo(
            _warn(
                f"stale handshake (pid={info['pid']} alive={info['pid_alive']}) — "
                "daemon not answering"
            )
        )
    elif state == "starting":
        click.echo(_warn("no handshake yet, lock held — a daemon is starting or shutting down"))
    elif state == "unknown":
        click.echo(_warn("no handshake, and the daemon lock could not be checked — state unknown"))
    else:
        click.echo("stopped")
    if use_daemon:
        hint = "yes"
    elif not surfacing_on:
        hint = "no (surfacing disabled)"
    else:
        hint = "no (opted out via MEMTOMEM_STM_HOOK__USE_DAEMON=0)"
    click.echo(f"hook will use daemon: {hint}")
    if standalone_use_daemon:
        standalone_hint = "yes"
    elif not surfacing_on:
        standalone_hint = "no (surfacing disabled)"
    else:
        standalone_hint = "no (set MEMTOMEM_STM_SURFACING__USE_DAEMON=true to opt in)"
    click.echo(f"standalone surfacing will use daemon: {standalone_hint}")

    if foreign_error is not None:
        click.echo(_warn(_foreign_unknown(foreign_error)))
    elif foreign:
        _render_foreign(foreign)


def _foreign_idle_label(daemon: dict[str, Any]) -> str:
    if not daemon["listening"]:
        if daemon.get("handshake_unreadable"):
            prefix = "handshake unreadable"
        else:
            prefix = "not accepting"
        if daemon["lock"] == LockProbe.UNKNOWN.value:
            return f"{prefix}; lock could not be checked"
        return f"{prefix}; lock held — starting or shutting down"
    exits_when_idle = daemon["exits_when_idle"]
    if exits_when_idle is True:
        return f"exits after {daemon['idle_shutdown_seconds']:g}s idle"
    if exits_when_idle is False:
        return "never exits when idle"
    return "idle timeout unknown"


def _render_foreign(foreign: list[dict[str, Any]]) -> None:
    """Text lines for daemons under another config fingerprint (#1195).

    A different fingerprint is not evidence of staleness — an STM process with a
    different environment keys to its own live daemon — so only a daemon that
    may linger (never exits when idle, or does not say) earns a warning and the
    cleanup hint. One that exits on its own once idle needs no action from the
    operator: unused, it goes away by itself, and stopping one still in use may
    only make its user start another. Neither label says whether anything uses
    the daemon; they say whether it can outlive its users.
    """
    listing = ", ".join(
        f"pid={'?' if d['pid'] is None else d['pid']} fp={d['fingerprint']} "
        f"({_foreign_idle_label(d)})"
        for d in foreign
    )
    # One not accepting may be a daemon still starting, so the ones that are
    # neither listen nor promise to exit get neither "running" nor "nothing to
    # stop" (#1245).
    all_listening = all(d["listening"] for d in foreign)
    if all_listening:
        line = f"{len(foreign)} daemon(s) running under a different config: {listing}"
    else:
        line = f"{len(foreign)} daemon(s) under a different config: {listing}"
    if all_listening and all(d["exits_when_idle"] is True for d in foreign):
        click.echo(line)
        click.echo("each exits on its own once idle; nothing to stop")
        return
    click.echo(_warn(line))
    if _can_force_terminate() and all_listening:
        click.echo(
            "if no running STM process uses them, stop them with `mms daemon stop --all` "
            "(it stops every daemon listed above)"
        )
    elif _can_force_terminate():
        click.echo(
            "if no running STM process uses them, `mms daemon stop --all` signals the "
            "accepting ones and waits for the others to finish"
        )
    else:
        # ``stop --all`` refuses to signal by pid on Windows
        # (``_stop_foreign_daemons``), so pointing there would be a dead end.
        click.echo(
            "if no running STM process uses them, end those daemon processes from "
            "Task Manager (`mms daemon stop --all` cannot signal them on Windows)"
        )


@daemon_group.command(name="restart")
@click.pass_context
def restart_cmd(ctx: click.Context) -> None:
    """Stop this config's running daemon, then start a fresh one once its lock is
    free. Daemons under other configs are left running (config-drift coexistence).
    """
    from memtomem_stm.daemon.discovery import config_fingerprint
    from memtomem_stm.daemon.locking import lock_path

    config = _load_config()
    ctx.invoke(stop_cmd)
    # Wait for the old daemon to release its lifetime lock before starting. A
    # fixed sleep could race: _teardown() awaits the warm LTM child's stop and
    # has no hard timeout. Bounded so a wedged teardown reports clearly instead
    # of letting start_cmd produce a misleading readiness timeout.
    #
    # Only FREE starts. A lock file that cannot be opened is UNKNOWN, not free,
    # and it can reach this probe first: a successful ``stop`` need not have
    # opened it (#1247). Retried inside the same deadline, since the error may
    # be transient.
    lp = lock_path(config.data_dir, config_fingerprint(config))
    deadline = time.time() + 5.0
    state = LockProbe.HELD
    while time.time() < deadline:
        state = _probe_lock(lp)
        if state is LockProbe.FREE:
            ctx.invoke(start_cmd)
            return
        time.sleep(0.2)
    # Exit nonzero: the docs recommend `mms daemon restart` after an upgrade,
    # and a script (or a user skimming the output) must not read "stopped and
    # never came back" as success.
    if state is LockProbe.UNKNOWN:
        click.echo(
            _warn(
                "daemon NOT restarted: could not check the previous daemon's lock — "
                "run `mms daemon status`"
            )
        )
    else:
        click.echo(
            _warn(
                "daemon NOT restarted: previous daemon still shutting down — "
                "run `mms daemon start` again shortly"
            )
        )
    raise SystemExit(1)
