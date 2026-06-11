"""``mms daemon`` — manage the local surfacing daemon (Stage 2).

The daemon keeps one warm LTM connection + ``SurfacingEngine`` so ``mms hook``
avoids the ~6s cold start on every built-in tool call. Subcommands:

- ``run``     — the actual long-lived server loop (foreground, or the target of
  a detached spawn via ``--detached``).
- ``start``   — spawn the daemon detached if one isn't already running.
- ``stop``    — ask a running daemon to shut down (SIGTERM by pid as fallback).
- ``status``  — running/stale/stopped, with pid/port/uptime/LTM warmth.
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

if TYPE_CHECKING:
    from memtomem_stm.config import STMConfig


def _color_on() -> bool:
    return "NO_COLOR" not in os.environ


def _ok(s: str) -> str:
    return click.style(s, fg="green") if _color_on() else s


def _warn(s: str) -> str:
    return click.style(s, fg="yellow") if _color_on() else s


def _load_config() -> STMConfig:
    from memtomem_stm.config import STMConfig

    return STMConfig()


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


def _configure_logging(config: STMConfig, *, detached: bool) -> None:
    """Route daemon logs to a file under ``data_dir`` when detached (its stdio
    is ``DEVNULL``), otherwise to stderr for foreground debugging."""
    level = getattr(logging, config.log_level, logging.WARNING)
    handler: logging.Handler
    if detached:
        logpath = (config.data_dir / "stm-daemon.log").expanduser()
        logpath.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(logpath, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)
    logging.basicConfig(
        level=level,
        handlers=[handler],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


@click.group(name="daemon")
def daemon_group() -> None:
    """Manage the local surfacing daemon (warm LTM connection for ``mms hook``)."""


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
def stop_cmd() -> None:
    """Ask a running daemon to shut down gracefully."""
    from memtomem_stm.daemon import client
    from memtomem_stm.daemon.discovery import (
        config_fingerprint,
        handshake_path,
        is_pid_alive,
        read_handshake,
    )

    config = _load_config()
    if asyncio.run(client.shutdown(config)):
        click.echo(_ok("daemon stopped"))
        return
    # Graceful path declined → no daemon for *this config*. Only ever act on our
    # own config's handshake; a different-config daemon owns a different file and
    # is none of our business here.
    hs_path = handshake_path(config.data_dir, config_fingerprint(config))
    raw = read_handshake(hs_path)
    if raw is None:
        click.echo("no running daemon")
        return
    pid = _as_int(raw.get("pid", -1))
    if os.name != "nt" and is_pid_alive(pid):
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
    hs = asyncio.run(client.ping(config, timeout=2.0))
    if hs is not None:
        uptime = max(0.0, time.time() - _as_float(hs.get("created_at"), time.time()))
        info: dict[str, Any] = {
            "state": "running",
            "pid": hs.get("pid"),
            "host": hs.get("host"),
            "port": hs.get("port"),
            "ltm": hs.get("ltm"),
            "uptime_seconds": round(uptime, 1),
            "hook_will_use_daemon": use_daemon,
        }
    else:
        raw = read_handshake(handshake_path(config.data_dir, config_fingerprint(config)))
        if raw is not None:
            info = {
                "state": "stale",
                "pid": raw.get("pid"),
                "pid_alive": is_pid_alive(_as_int(raw.get("pid", -1))),
                "hook_will_use_daemon": use_daemon,
            }
        else:
            info = {"state": "stopped", "hook_will_use_daemon": use_daemon}

    if as_json:
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
    click.echo(_warn("previous daemon still shutting down — try `mms daemon start` again shortly"))
