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


@daemon_group.command(name="start")
def start_cmd() -> None:
    """Spawn the daemon detached if it isn't already running.

    The daemon self-guards via its lifetime lock, so ``start`` never holds the
    lock — it reconciles toward a running daemon. The lock may currently be held
    by a daemon that is *shutting down* (e.g. right after ``mms daemon stop``):
    it won't answer ping and will soon release the lock, so we keep retrying the
    spawn until the lock frees rather than polling ping once and giving up. A
    *different-config* daemon stays alive and won't release — that we report.
    """
    from memtomem_stm.daemon import client
    from memtomem_stm.daemon.discovery import (
        config_fingerprint,
        handshake_path,
        is_pid_alive,
        read_handshake,
    )
    from memtomem_stm.daemon.spawn import request_spawn

    config = _load_config()
    existing = asyncio.run(client.ping(config, timeout=2.0))
    if existing is not None:
        click.echo(
            _ok(f"daemon already running (pid={existing.get('pid')} port={existing.get('port')})")
        )
        return

    deadline = time.time() + 10.0
    spawned = False
    while time.time() < deadline:
        hs = asyncio.run(client.ping(config, timeout=1.0))
        if hs is not None:
            click.echo(_ok(f"daemon started (pid={hs.get('pid')} port={hs.get('port')})"))
            return
        if request_spawn(config):
            # We launched a child (lock was free). Give it a moment to acquire
            # the lock + publish, then loop back to ping it. We don't hold the
            # lock, so the child can take ownership.
            spawned = True
            time.sleep(0.3)
            continue
        # Lock held by another daemon. Only a *live* different-config daemon that
        # we did not just spawn is a real conflict worth reporting: a stale
        # handshake (dead pid) left by a crash, or the matching child we just
        # spawned that hasn't published its handshake yet, must NOT be
        # misreported. Everything else falls through to wait — the next
        # iteration re-attempts the spawn once the lock frees.
        if not spawned:
            raw = read_handshake(handshake_path(config.data_dir))
            if (
                raw is not None
                and raw.get("config_fingerprint") != config_fingerprint(config)
                and is_pid_alive(int(raw.get("pid", -1)))
            ):
                click.echo(
                    _warn(
                        f"a daemon with a different config is running (pid={raw.get('pid')}) — "
                        "run `mms daemon restart` to replace it"
                    )
                )
                return
        time.sleep(0.2)
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
    from memtomem_stm.daemon.discovery import handshake_path, is_pid_alive, read_handshake

    config = _load_config()
    if asyncio.run(client.shutdown(config)):
        click.echo(_ok("daemon stopped"))
        return
    # Graceful path declined (no daemon, or its config fingerprint drifted).
    raw = read_handshake(handshake_path(config.data_dir))
    if raw is None:
        click.echo("no running daemon")
        return
    pid = int(raw.get("pid", -1))
    if os.name != "nt" and is_pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            click.echo(_ok(f"sent SIGTERM to daemon pid={pid}"))
            return
        except OSError:
            pass
    try:
        handshake_path(config.data_dir).unlink(missing_ok=True)
    except OSError:
        pass
    click.echo(_warn("no responsive daemon; cleaned stale handshake"))


@daemon_group.command(name="status")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON for scripting.")
def status_cmd(as_json: bool) -> None:
    """Report whether a daemon is running, with pid/port/uptime/LTM warmth."""
    from memtomem_stm.daemon import client
    from memtomem_stm.daemon.discovery import handshake_path, is_pid_alive, read_handshake

    config = _load_config()
    # The hook only reaches the daemon for eligible surfacing calls, so global
    # surfacing-off means it never will — regardless of the use_daemon knob.
    surfacing_on = config.surfacing.enabled
    use_daemon = config.hook.use_daemon and surfacing_on
    hs = asyncio.run(client.ping(config, timeout=2.0))
    if hs is not None:
        uptime = max(0.0, time.time() - float(hs.get("created_at", time.time())))
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
        raw = read_handshake(handshake_path(config.data_dir))
        if raw is not None:
            info = {
                "state": "stale",
                "pid": raw.get("pid"),
                "pid_alive": is_pid_alive(int(raw.get("pid", -1))),
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
    """Stop any running daemon, then start a fresh one once the lock is free."""
    from memtomem_stm.daemon.locking import lock_path, single_owner_lock

    config = _load_config()
    ctx.invoke(stop_cmd)
    # Wait for the old daemon to release its lifetime lock before starting. A
    # fixed sleep could race: _teardown() awaits the warm LTM child's stop and
    # has no hard timeout. Bounded so a wedged teardown reports clearly instead
    # of letting start_cmd produce a misleading readiness timeout.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        with single_owner_lock(lock_path(config.data_dir)) as acquired:
            free = acquired
        if free:
            ctx.invoke(start_cmd)
            return
        time.sleep(0.2)
    click.echo(_warn("previous daemon still shutting down — try `mms daemon start` again shortly"))
