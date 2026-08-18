"""``mms daemon`` — manage the shared local surfacing daemon (Stage 2).

The daemon keeps one warm LTM connection for ``mms hook`` and opt-in standalone
proxy surfacing, avoiding one private LTM child per client. Subcommands:

- ``run``     — the actual long-lived server loop (foreground, or the target of
  a detached spawn via ``--detached``).
- ``start``   — spawn the daemon detached if one isn't already running.
- ``stop``    — ask a running daemon to shut down (SIGTERM by pid as fallback);
  ``--all`` also reaps daemons orphaned under a stale config fingerprint.
- ``status``  — running/stale/stopped, with pid/port/uptime/LTM warmth; also
  surfaces live daemons left under a different (stale) config fingerprint.
- ``restart`` — ``stop`` then ``start``.

Color helpers are defined locally (not imported from ``cli.proxy``) so this
module stays import-cheap and free of an import cycle with the group that
registers it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from typing import TYPE_CHECKING, Any

import click

from memtomem_stm.logging_setup import FILE_FORMAT, open_private_log_handler

if TYPE_CHECKING:
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
        # server.main() does, then re-raise — every daemon subcommand still
        # exits non-zero exactly as before. A detached run's failure stays
        # file-less (data_dir is unknowable from a config that will not
        # build); `mms doctor`'s env_overrides check covers that hole.
        from memtomem_stm.logging_setup import STDERR_FORMAT

        logging.basicConfig(level=logging.WARNING, format=STDERR_FORMAT)
        log_stm_config_failure(
            exc, logger=logging.getLogger(__name__), context="loading the daemon configuration"
        )
        raise


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


def _live_foreign_daemons(config: STMConfig) -> list[dict[str, Any]]:
    """Daemons running under a *different* config fingerprint than the current one.

    These are orphans left when a fingerprinted field — or ``PROTOCOL_VERSION``,
    which is folded into the fingerprint so *every* protocol bump counts — changes
    while a daemon is running: the old daemon keeps its handshake under the old
    fingerprint, where ``stop``/``status`` (keyed to the *current* fingerprint)
    never look. A daemon with a finite ``idle_timeout_seconds`` clears itself, but
    a pinned ``idle_timeout_seconds=0`` one lingers — this surfaces it.

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
    (``fingerprint``/``pid``/``host``/``port``) — never the auth ``token`` — so
    callers can render them.

    Cross-platform: the connect-probe is what makes Windows viable too, where
    :func:`is_pid_alive` is uninformative (returns ``True`` for any positive pid,
    no signal-0) — there the connect carries the whole signal (#519).
    """
    from memtomem_stm.daemon.client import probe_listening
    from memtomem_stm.daemon.discovery import (
        config_fingerprint,
        is_pid_alive,
        iter_foreign_handshakes,
    )

    current = config_fingerprint(config)
    live: list[dict[str, Any]] = []
    for fingerprint, hs in iter_foreign_handshakes(config.data_dir, current):
        pid = _as_int(hs.get("pid", -1))
        host = hs.get("host")
        port = hs.get("port")
        # Cheap pid filter first (drops dead/garbage pids and, on POSIX, the
        # bulk of stale handshakes without a connect); the probe then confirms
        # something still accepts on the recorded endpoint.
        if is_pid_alive(pid) and probe_listening(host, port):
            live.append(
                {
                    "fingerprint": fingerprint,
                    "pid": pid,
                    "host": host,
                    "port": port,
                }
            )
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
    if detached:
        handler = open_private_log_handler((config.data_dir / "stm-daemon.log").expanduser())
    else:
        handler = logging.StreamHandler(sys.stderr)
    logging.basicConfig(
        level=level,
        handlers=[handler],
        format=FILE_FORMAT,
        force=True,
    )


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
    while time.time() < deadline:
        hs = asyncio.run(client.ping(config, timeout=1.0))
        if hs is not None:
            click.echo(_ok(f"daemon started (pid={hs.get('pid')} port={hs.get('port')})"))
            return
        # request_spawn launches a detached child iff our config's lock is free;
        # it returns False only when a same-config daemon already holds it
        # (mid-startup → ping succeeds soon, or mid-shutdown → the lock frees and
        # the next attempt wins). We never hold the lock, so the spawned child
        # can take ownership. BUT a free lock can also mean the child we just
        # spawned crashed on startup (bad config) — without a cap, a
        # crash-looping config would fire a detached child every 0.3s for the
        # whole window (~33 processes). Cap actual spawns and keep only
        # ping-polling afterwards; the mid-shutdown handoff needs at most one
        # respawn after the old daemon releases.
        if spawns < _START_MAX_SPAWNS and request_spawn(config):
            spawns += 1
        time.sleep(0.3)
    click.echo(
        _warn(
            "daemon did not become ready in time — "
            "check the daemon log under data_dir (stm-daemon.log)"
        )
    )


@daemon_group.command(name="stop")
@click.option(
    "--all",
    "stop_all",
    is_flag=True,
    help="Also stop daemons running under a different config fingerprint "
    "(orphans left after a config or PROTOCOL_VERSION change). Off by default: a "
    "live daemon under another config may be intentional. Targets are gated by a "
    "connect-probe so a stale handshake naming a recycled pid is not signalled.",
)
def stop_cmd(stop_all: bool) -> None:
    """Ask a running daemon to shut down gracefully.

    By default acts only on the daemon for the *current* config. ``--all`` also
    SIGTERMs daemons left under a different (stale) config fingerprint — e.g. a
    pinned ``idle_timeout_seconds=0`` daemon orphaned by a config or
    ``PROTOCOL_VERSION`` change, which is otherwise invisible to ``stop``/``status``.
    """
    config = _load_config()
    _stop_current_config_daemon(config)
    if stop_all:
        _stop_foreign_daemons(config)


def _stop_current_config_daemon(config: STMConfig) -> None:
    """Stop the daemon for *this* config (graceful, then SIGTERM-by-pid fallback)."""
    from memtomem_stm.daemon import client
    from memtomem_stm.daemon.discovery import (
        config_fingerprint,
        handshake_path,
        is_pid_alive,
        read_handshake,
    )

    if asyncio.run(client.shutdown(config)):
        click.echo(_ok("daemon stopped"))
        return
    # Graceful path declined → no daemon for *this config*. Only ever act on our
    # own config's handshake; a different-config daemon owns a different file and
    # is handled by ``--all`` (``_stop_foreign_daemons``), not here.
    hs_path = handshake_path(config.data_dir, config_fingerprint(config))
    raw = read_handshake(hs_path)
    if raw is None:
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
                )
            )
            return
        try:
            os.kill(pid, signal.SIGTERM)
            click.echo(_ok(f"sent SIGTERM to daemon pid={pid}"))
            return
        except OSError:
            pass
    try:
        hs_path.unlink(missing_ok=True)
    except OSError:
        pass
    click.echo(_warn("no responsive daemon; cleaned stale handshake"))


def _stop_foreign_daemons(config: STMConfig) -> None:
    """SIGTERM every live daemon under a *different* config fingerprint (``--all``).

    SIGTERM (not the socket shutdown op) because a foreign daemon may speak an
    older protocol the graceful path can't negotiate; the daemon's signal handler
    sets its shutdown event regardless of ``idle_timeout_seconds``, so even a
    pinned daemon exits. The daemon removes its own handshake on teardown, so we
    do not unlink it here (that could hide a survivor that ignored the signal).

    Targets come from :func:`_live_foreign_daemons`, whose two-factor check
    (recorded pid alive *and* its endpoint accepting a connect) already rejects a
    crash-orphaned handshake naming a since-recycled pid — so ``--all`` does not
    signal an unrelated process in the common case. The residual is a double
    coincidence (pid recycled *and* its ephemeral port reassigned to another
    listener); ``--all`` stays opt-in for that narrow window.
    """
    from memtomem_stm.daemon import client
    from memtomem_stm.daemon.discovery import is_pid_alive

    foreign = _live_foreign_daemons(config)
    if not foreign:
        click.echo("no daemons running under a different config")
        return
    if not _can_force_terminate():
        click.echo(
            _warn(
                "found daemon(s) under a different config, but Windows cannot safely "
                "SIGTERM them by pid. Stop the owning processes from Task Manager or "
                "restart Windows, then run `mms daemon status`."
            )
        )
        return
    for d in foreign:
        pid = d["pid"]
        # Re-confirm the *same* two-factor gate at action time, not just
        # `is_pid_alive`: the daemon may have exited since enumeration and the OS
        # recycled its pid onto an unrelated process. A pid-only recheck is too
        # weak for that race (and a no-op on Windows, where is_pid_alive is always
        # True), so re-run the connect-probe — a recycled pid's endpoint is gone.
        if not (is_pid_alive(pid) and client.probe_listening(d["host"], d["port"])):
            continue  # raced to exit / pid recycled between enumeration and now
        try:
            os.kill(pid, signal.SIGTERM)
            click.echo(_ok(f"sent SIGTERM to daemon pid={pid} (fp={d['fingerprint']})"))
        except OSError:
            click.echo(_warn(f"could not signal daemon pid={pid} (fp={d['fingerprint']})"))


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
            "uptime_seconds": round(uptime, 1),
            "hook_will_use_daemon": use_daemon,
            "standalone_will_use_daemon": standalone_use_daemon,
        }
    else:
        raw = read_handshake(handshake_path(config.data_dir, config_fingerprint(config)))
        if raw is not None:
            info = {
                "state": "stale",
                "pid": raw.get("pid"),
                "pid_alive": is_pid_alive(_as_int(raw.get("pid", -1))),
                "hook_will_use_daemon": use_daemon,
                "standalone_will_use_daemon": standalone_use_daemon,
            }
        else:
            info = {
                "state": "stopped",
                "hook_will_use_daemon": use_daemon,
                "standalone_will_use_daemon": standalone_use_daemon,
            }

    # Orthogonal to the current-config state: daemons orphaned under a different
    # fingerprint (config/protocol drift) are invisible to the keyed paths above,
    # so surface them here regardless of running/stale/stopped.
    foreign = _live_foreign_daemons(config)

    if as_json:
        info["foreign"] = foreign
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
            click.echo(
                "queue: "
                f"active={queue.get('active', 0)} queued={queue.get('queued', 0)} "
                f"capacity={queue.get('capacity', 0)} "
                f"busy_rejections={queue.get('busy_rejections', 0)}"
            )
    elif state == "stale":
        click.echo(
            _warn(
                f"stale handshake (pid={info['pid']} alive={info['pid_alive']}) — "
                "daemon not answering"
            )
        )
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

    if foreign:
        listing = ", ".join(f"pid={d['pid']} fp={d['fingerprint']}" for d in foreign)
        click.echo(
            _warn(
                f"{len(foreign)} daemon(s) under a different config running "
                f"(orphaned by a config/protocol change): {listing}"
            )
        )
        click.echo("stop them with `mms daemon stop --all`")


@daemon_group.command(name="restart")
@click.pass_context
def restart_cmd(ctx: click.Context) -> None:
    """Stop this config's running daemon, then start a fresh one once its lock is
    free. Daemons under other configs are left running (config-drift coexistence).
    """
    from memtomem_stm.daemon.discovery import config_fingerprint
    from memtomem_stm.daemon.locking import lock_path, single_owner_lock

    config = _load_config()
    ctx.invoke(stop_cmd)
    # Wait for the old daemon to release its lifetime lock before starting. A
    # fixed sleep could race: _teardown() awaits the warm LTM child's stop and
    # has no hard timeout. Bounded so a wedged teardown reports clearly instead
    # of letting start_cmd produce a misleading readiness timeout.
    fp = config_fingerprint(config)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        with single_owner_lock(lock_path(config.data_dir, fp)) as acquired:
            free = acquired
        if free:
            ctx.invoke(start_cmd)
            return
        time.sleep(0.2)
    # Exit nonzero: the docs recommend `mms daemon restart` after an upgrade,
    # and a script (or a user skimming the output) must not read "stopped and
    # never came back" as success.
    click.echo(
        _warn(
            "daemon NOT restarted: previous daemon still shutting down — "
            "run `mms daemon start` again shortly"
        )
    )
    raise SystemExit(1)
