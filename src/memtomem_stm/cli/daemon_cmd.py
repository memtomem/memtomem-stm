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
import subprocess
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


def _spawn_detached() -> None:
    """Launch ``mms daemon run --detached`` as a background process."""
    cmd = [sys.executable, "-m", "memtomem_stm", "daemon", "run", "--detached"]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI only
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def _wait_ready(config: STMConfig, *, timeout: float) -> dict[str, Any] | None:
    """Poll ``ping`` until the daemon answers or ``timeout`` elapses."""
    from memtomem_stm.daemon import client

    deadline = time.time() + timeout
    while time.time() < deadline:
        hs = asyncio.run(client.ping(config, timeout=1.0))
        if hs is not None:
            return hs
        time.sleep(0.2)
    return None


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
    """Spawn the daemon detached if it isn't already running."""
    from memtomem_stm.daemon import client
    from memtomem_stm.daemon.locking import lock_path, single_owner_lock

    config = _load_config()
    existing = asyncio.run(client.ping(config, timeout=2.0))
    if existing is not None:
        click.echo(
            _ok(f"daemon already running (pid={existing.get('pid')} port={existing.get('port')})")
        )
        return
    with single_owner_lock(lock_path(config.data_dir)) as acquired:
        if not acquired:
            click.echo(_warn("another `mms daemon start` is in progress"))
            return
        # Re-check under the lock to avoid a double-spawn race.
        if asyncio.run(client.ping(config, timeout=1.0)) is not None:
            click.echo(_ok("daemon already running"))
            return
        _spawn_detached()
        # Hold the lock through readiness. Releasing it here would let a
        # concurrent `start` acquire it, see ping return None (our child hasn't
        # published its handshake yet), and spawn a *second* daemon — which,
        # with ephemeral ports + last-writer-wins handshake, orphans an extra
        # warm LTM process.
        hs = _wait_ready(config, timeout=8.0)
    if hs is not None:
        click.echo(_ok(f"daemon started (pid={hs.get('pid')} port={hs.get('port')})"))
    else:
        click.echo(
            _warn(
                "daemon spawn requested but it did not become ready in time — "
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
    use_daemon = config.hook.use_daemon
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
    hint = "yes" if use_daemon else "no (set MEMTOMEM_STM_HOOK__USE_DAEMON=1)"
    click.echo(f"hook will use daemon: {hint}")


@daemon_group.command(name="restart")
@click.pass_context
def restart_cmd(ctx: click.Context) -> None:
    """Stop any running daemon, then start a fresh one."""
    ctx.invoke(stop_cmd)
    time.sleep(0.3)
    ctx.invoke(start_cmd)
