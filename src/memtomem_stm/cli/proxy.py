"""CLI commands for managing the memtomem-stm proxy gateway."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import click

from memtomem_stm.utils.fileio import atomic_write_text

_PREFIX_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")

_DEFAULT_CONFIG = Path("~/.memtomem/stm_proxy.json")

# Environment variable names that could enable code injection via subprocess
_DANGEROUS_ENV_KEYS = frozenset(
    {
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "NODE_OPTIONS",
    }
)


# Style policy — color and bold carry *different* signals so visual weight
# stays meaningful.
#
# * red + bold → abort-worthy errors (``Error:``)
# * red       → bad-state keywords that shade a line but shouldn't shout
#   (e.g. ``DISCONNECTED`` in ``mms health``)
# * yellow    → non-abort warnings (``Warning:``, retry hints)
# * green     → successful-action verbs/labels (``Added``, ``Saved to:``)
# * bold      → section / table headers (no color)
#
# Click already strips ANSI when the output stream isn't a TTY (pipes, CI,
# ``CliRunner``), but Click 8.3 does *not* honor ``NO_COLOR`` on real TTYs.
# We enforce it here so the behavior matches https://no-color.org — presence
# of the var (even empty) disables color per the spec.
def _color_on() -> bool:
    return "NO_COLOR" not in os.environ


def _err(s: str) -> str:
    return click.style(s, fg="red", bold=True) if _color_on() else s


def _warn(s: str) -> str:
    return click.style(s, fg="yellow") if _color_on() else s


def _ok(s: str) -> str:
    return click.style(s, fg="green") if _color_on() else s


def _bad(s: str) -> str:
    return click.style(s, fg="red") if _color_on() else s


def _hdr(s: str) -> str:
    return click.style(s, bold=True) if _color_on() else s


def _load(config_path: Path) -> dict[str, Any]:
    resolved = config_path.expanduser().resolve()
    if not resolved.exists():
        return {"enabled": True, "upstream_servers": {}}
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        click.echo(f"{_err('Error:')} Failed to parse {resolved}: {exc}", err=True)
        raise SystemExit(1) from exc
    # Structural guard: the rest of the CLI assumes top-level dict with a dict
    # `upstream_servers`. Without this, a valid-but-wrong-shape JSON (e.g. a
    # list or a string, or `upstream_servers: "oops"`) crashes downstream with
    # an AttributeError traceback instead of a clean user-facing error.
    if not isinstance(data, dict):
        click.echo(
            f"{_err('Error:')} {resolved} top-level must be a JSON object, "
            f"got {type(data).__name__}.",
            err=True,
        )
        raise SystemExit(1)
    servers = data.get("upstream_servers")
    if servers is not None and not isinstance(servers, dict):
        click.echo(
            f"{_err('Error:')} {resolved} 'upstream_servers' must be an object, "
            f"got {type(servers).__name__}.",
            err=True,
        )
        raise SystemExit(1)
    return data


def _save(config_path: Path, data: dict[str, Any]) -> None:
    """Write the proxy config atomically.

    Delegates to :func:`atomic_write_text` so the temp + ``os.replace``
    pattern stays in one place (see PR #115 for the original failure
    mode: a running proxy's mtime-based hot-reload would otherwise read
    a half-written JSON file in the gap between truncate and write).
    ``mode=0o600`` keeps the rendered file out of a permissive listing
    even if the parent directory is shared.
    """
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(config_path, payload, mode=0o600)


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


@click.group(context_settings=CONTEXT_SETTINGS)
def cli() -> None:
    """memtomem-stm proxy gateway management.

    Output is colorized when writing to a terminal. Set NO_COLOR=1 to disable.
    """


@cli.command()
def version() -> None:
    """Show the installed memtomem-stm version."""
    from importlib.metadata import version as pkg_version

    click.echo(f"memtomem-stm {pkg_version('memtomem-stm')}")


@cli.command()
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON for scripting.")
def status(config_path: str, *, as_json: bool = False) -> None:
    """Show proxy gateway configuration and server list."""
    path = Path(config_path)
    resolved = path.expanduser().resolve()

    if not resolved.exists():
        if as_json:
            click.echo(json.dumps({"error": "config_not_found", "path": str(resolved)}))
        else:
            click.echo(f"Config not found: {resolved}")
            click.echo("Run `mms add` to create a configuration.")
        return

    data = _load(path)
    enabled = data.get("enabled", False)
    servers: dict[str, Any] = data.get("upstream_servers", {})

    if as_json:
        click.echo(
            json.dumps(
                {
                    "config_path": str(resolved),
                    "enabled": enabled,
                    "servers": servers,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    click.echo(f"Config : {resolved}")
    click.echo(f"Enabled: {'yes' if enabled else 'no'}")
    click.echo(f"Servers: {len(servers)}")

    if servers:
        click.echo("")
        for name, cfg in servers.items():
            transport = cfg.get("transport", "stdio")
            prefix = cfg.get("prefix", "")
            if transport == "stdio":
                cmd = cfg.get("command", "")
                args_str = " ".join(cfg.get("args", []))
                detail = f"{cmd} {args_str}".strip()
            else:
                detail = cfg.get("url", "")
            compression = cfg.get("compression", "auto")
            max_chars = cfg.get("max_result_chars", 8000)
            click.echo(f"  {name:<20} prefix={prefix}  [{transport}] {detail}")
            click.echo(f"  {'':<20} compression={compression}  max_chars={max_chars}")


@cli.command(name="list")
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
def list_servers(config_path: str) -> None:
    """List configured upstream servers."""
    data = _load(Path(config_path))
    servers: dict[str, Any] = data.get("upstream_servers", {})

    if not servers:
        click.echo("No upstream servers configured.")
        return

    click.echo(
        _hdr(f"{'NAME':<20} {'PREFIX':<10} {'TRANSPORT':<12} {'COMPRESSION':<12} COMMAND / URL")
    )
    click.echo("-" * 80)
    for name, cfg in servers.items():
        transport = cfg.get("transport", "stdio")
        prefix = cfg.get("prefix", "")
        compression = cfg.get("compression", "auto")
        if transport == "stdio":
            cmd = cfg.get("command", "")
            args_str = " ".join(cfg.get("args", []))
            detail = f"{cmd} {args_str}".strip()
        else:
            detail = cfg.get("url", "")
        click.echo(f"{name:<20} {prefix:<10} {transport:<12} {compression:<12} {detail}")
    click.echo(f"\n{len(servers)} server(s) configured.")


@cli.command()
@click.argument("name")
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option("--command", "command", default="", help="Executable command (stdio).")
@click.option("--args", "args_str", default="", help="Space-separated arguments.")
@click.option(
    "--prefix",
    required=True,
    help="Tool namespace (e.g. 'fs' -> tools appear as fs__read_file).",
)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse", "streamable_http"]),
    default="stdio",
    show_default=True,
    help="stdio for local processes, sse/streamable_http for remote.",
)
@click.option("--url", default="", help="Endpoint URL (SSE / HTTP).")
@click.option("--env", "env_pairs", multiple=True, metavar="KEY=VALUE")
@click.option(
    "--compression",
    type=click.Choice(["auto", "none", "truncate", "selective", "hybrid"]),
    default="auto",
    show_default=True,
    help="'auto' picks strategy per response by content type.",
)
@click.option(
    "--max-chars", "max_result_chars", type=click.IntRange(min=1), default=8000, show_default=True
)
@click.option(
    "--validate",
    is_flag=True,
    default=False,
    help="Probe the server (MCP initialize + list-tools) before saving; abort on failure.",
)
@click.option(
    "--timeout",
    "validate_timeout",
    type=click.IntRange(min=1),
    default=10,
    show_default=True,
    help="Connection timeout (seconds) when --validate is set.",
)
def add(
    name: str,
    config_path: str,
    command: str,
    args_str: str,
    prefix: str,
    transport: str,
    url: str,
    env_pairs: tuple[str, ...],
    compression: str,
    max_result_chars: int,
    validate: bool,
    validate_timeout: int,
) -> None:
    """Add an upstream MCP server to the proxy configuration."""
    path = Path(config_path)
    data = _load(path)
    servers: dict[str, Any] = data.setdefault("upstream_servers", {})

    if name in servers:
        click.echo(
            f"{_err('Error:')} server '{name}' already exists. Use `remove` first.",
            err=True,
        )
        sys.exit(1)

    # VAL-1: prefix format validation
    if not _PREFIX_RE.match(prefix) or "__" in prefix:
        click.echo(
            f"{_err('Error:')} invalid prefix '{prefix}'. "
            "Must start with a letter, contain only letters/digits/underscores, "
            "and must not contain '__'.",
            err=True,
        )
        sys.exit(1)

    # VAL-2: duplicate prefix warning
    for srv_name, srv_cfg in servers.items():
        if srv_cfg.get("prefix") == prefix:
            click.echo(
                f"{_warn('Warning:')} prefix '{prefix}' is already used by server "
                f"'{srv_name}'. Duplicate-named tools will shadow each "
                f"other at runtime. Proceeding anyway.",
                err=True,
            )
            break

    # VAL-3: stdio requires --command
    if transport == "stdio" and not command:
        click.echo(f"{_err('Error:')} --command is required for stdio transport.", err=True)
        sys.exit(1)

    # VAL-4: sse/streamable_http requires --url
    if transport != "stdio" and not url:
        click.echo(f"{_err('Error:')} --url is required for {transport} transport.", err=True)
        sys.exit(1)

    entry: dict[str, Any] = {
        "prefix": prefix,
        "transport": transport,
        "compression": compression,
        "max_result_chars": max_result_chars,
    }
    if transport == "stdio":
        entry["command"] = command
        if args_str:
            try:
                entry["args"] = shlex.split(args_str)
            except ValueError as exc:
                click.echo(f"{_err('Error:')} malformed --args: {exc}", err=True)
                sys.exit(1)
    else:
        entry["url"] = url

    if env_pairs:
        env_dict: dict[str, str] = {}
        for pair in env_pairs:
            if "=" not in pair:
                click.echo(f"{_err('Error:')} --env must be KEY=VALUE, got: {pair}", err=True)
                sys.exit(1)
            k, v = pair.split("=", 1)
            if not k:
                click.echo(
                    f"{_err('Error:')} --env key must be non-empty, got: {pair}",
                    err=True,
                )
                sys.exit(1)
            if k.upper() in _DANGEROUS_ENV_KEYS:
                click.echo(
                    f"{_err('Error:')} --env key '{k}' is blocked for security reasons "
                    "(could enable code injection in spawned processes).",
                    err=True,
                )
                sys.exit(1)
            env_dict[k] = v
        entry["env"] = env_dict

    if validate:
        click.echo(f"Validating '{name}' (timeout={validate_timeout}s)...")
        probe = asyncio.run(_probe_servers({name: entry}, validate_timeout))[name]
        if not probe["connected"]:
            click.echo(f"{_err('Error:')} validation failed — {probe['error']}", err=True)
            sys.exit(1)
        click.echo(f"{_ok('Validated:')} {probe['tools']} tool(s) reachable.")

    servers[name] = entry
    _save(path, data)
    click.echo(f"{_ok('Added')} server '{name}' (prefix={prefix})")


# ── init command ────────────────────────────────────────────────────────


def _prompt_prefix() -> str:
    """Prompt for a prefix until it passes the same rules as ``add --prefix``."""
    while True:
        value = click.prompt("Tool prefix (letters/digits/underscores, e.g. 'fs')", type=str)
        if _PREFIX_RE.match(value) and "__" not in value:
            return value
        click.echo(
            f"  {_warn('Invalid:')} must start with a letter, contain only letters/digits/"
            "underscores, and not contain '__'. Try again."
        )


@cli.command()
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option(
    "--no-validate",
    is_flag=True,
    default=False,
    help="Skip the connectivity probe entirely (default: prompt, probe on yes).",
)
def init(config_path: str, no_validate: bool) -> None:
    """Guided first-time setup for memtomem-stm.

    Prompts for a single upstream server and writes the config file. Aborts
    when the config already exists — use ``mms add`` to append more servers.
    """
    path = Path(config_path)
    resolved = path.expanduser().resolve()

    if resolved.exists():
        click.echo(f"{_err('Error:')} config already exists at {resolved}.", err=True)
        click.echo("  Use `mms add` to register another server.", err=True)
        click.echo("  Use `mms list` to see what's already configured.", err=True)
        sys.exit(1)

    click.echo(_hdr("Guided setup for memtomem-stm"))
    click.echo("=" * 30)
    click.echo(f"Config will be written to: {resolved}")
    click.echo("")

    name = click.prompt("Server name (e.g. 'filesystem', 'github')", type=str).strip()
    if not name:
        click.echo(f"{_err('Error:')} server name must be non-empty.", err=True)
        sys.exit(1)

    prefix = _prompt_prefix()

    transport = click.prompt(
        "Transport",
        type=click.Choice(["stdio", "sse", "streamable_http"]),
        default="stdio",
        show_default=True,
    )

    entry: dict[str, Any] = {
        "prefix": prefix,
        "transport": transport,
        "compression": "auto",
        "max_result_chars": 8000,
    }

    if transport == "stdio":
        command = click.prompt("Command (e.g. 'npx', 'uvx')", type=str).strip()
        if not command:
            click.echo(
                f"{_err('Error:')} command must be non-empty for stdio transport.",
                err=True,
            )
            sys.exit(1)
        entry["command"] = command

        args_str = click.prompt(
            "Arguments (space-separated, leave empty for none)",
            type=str,
            default="",
            show_default=False,
        )
        if args_str.strip():
            try:
                entry["args"] = shlex.split(args_str)
            except ValueError as exc:
                click.echo(f"{_err('Error:')} malformed arguments: {exc}", err=True)
                sys.exit(1)
    else:
        url = click.prompt(f"URL for {transport}", type=str).strip()
        if not url:
            click.echo(
                f"{_err('Error:')} URL must be non-empty for {transport} transport.",
                err=True,
            )
            sys.exit(1)
        entry["url"] = url

    if not no_validate and click.confirm("Validate connection now?", default=True):
        click.echo(f"Validating '{name}' (timeout=10s)...")
        probe = asyncio.run(_probe_servers({name: entry}, 10))[name]
        if probe["connected"]:
            click.echo(f"  {_ok('Reachable:')} {probe['tools']} tool(s) discovered.")
        else:
            click.echo(f"  {_warn('Warning:')} probe failed — {probe['error']}", err=True)
            click.echo("  Saving config anyway. Run `mms health` later to retry.", err=True)

    data = {"enabled": True, "upstream_servers": {name: entry}}
    _save(path, data)

    click.echo("")
    click.echo(f"{_ok('Saved to:')} {resolved}")
    click.echo("")
    click.echo(_hdr("Configured upstream servers:"))
    if transport == "stdio":
        detail = f"{entry['command']} {' '.join(entry.get('args', []))}".strip()
    else:
        detail = entry["url"]
    click.echo(f"  {name:<20} prefix={prefix}  [{transport}] {detail}")

    click.echo("")
    click.echo(f"{_ok('Next:')} connect your MCP client to memtomem-stm.")
    click.echo("")
    click.echo(f"  {_hdr('Claude Code (CLI):')}")
    click.echo("    claude mcp add memtomem-stm -s user -- memtomem-stm")
    click.echo("")
    click.echo(f"  {_hdr('Claude Desktop / JSON MCP config:')}")
    click.echo('    { "mcpServers": { "memtomem-stm": { "command": "memtomem-stm" } } }')


@cli.command()
@click.argument("name")
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
def remove(name: str, config_path: str, yes: bool) -> None:
    """Remove an upstream MCP server from the proxy configuration."""
    path = Path(config_path)
    data = _load(path)
    servers: dict[str, Any] = data.get("upstream_servers", {})

    if name not in servers:
        click.echo(f"{_err('Error:')} server '{name}' not found.", err=True)
        sys.exit(1)

    if not yes:
        click.confirm(f"Remove server '{name}'?", abort=True)

    del servers[name]
    _save(path, data)
    click.echo(f"{_ok('Removed')} server '{name}'.")


# ── health command ──────────────────────────────────────────────────────


async def _probe_one(cfg: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Probe a single upstream server: connect, initialize, list tools."""
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamablehttp_client

    transport = cfg.get("transport", "stdio")
    if transport == "stdio":
        ctx = stdio_client(
            StdioServerParameters(
                command=cfg.get("command", ""),
                args=cfg.get("args", []),
                env=cfg.get("env"),
            )
        )
    elif transport == "sse":
        ctx = sse_client(cfg.get("url", ""))
    else:
        ctx = streamablehttp_client(cfg.get("url", ""))

    async with ctx as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await asyncio.wait_for(session.initialize(), timeout=timeout)
            result = await session.list_tools()
            return {
                "connected": True,
                "tools": len(result.tools),
                "error": None,
            }


async def _probe_servers(servers: dict[str, Any], timeout: float) -> dict[str, dict[str, Any]]:
    """Probe all servers in parallel, returning per-server results."""

    async def _safe_probe(name: str, cfg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            result = await _probe_one(cfg, timeout)
        except asyncio.TimeoutError:
            result = {
                "connected": False,
                "tools": 0,
                "error": f"timeout ({timeout}s)",
            }
        except Exception as exc:
            err = str(exc) or type(exc).__name__
            result = {"connected": False, "tools": 0, "error": err}
        return name, result

    tasks = [_safe_probe(n, c) for n, c in servers.items()]
    pairs = await asyncio.gather(*tasks)
    return dict(pairs)


@cli.command()
@click.option(
    "--config",
    "config_path",
    default=str(_DEFAULT_CONFIG),
    show_default=True,
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Output as JSON for scripting.",
)
@click.option(
    "--timeout",
    default=10,
    show_default=True,
    type=click.IntRange(min=1),
    help="Per-server connection timeout in seconds.",
)
def health(config_path: str, *, as_json: bool = False, timeout: int = 10) -> None:
    """Check upstream server connectivity."""
    data = _load(Path(config_path))
    servers: dict[str, Any] = data.get("upstream_servers", {})

    if not servers:
        if as_json:
            click.echo(json.dumps({"servers": {}}))
        else:
            click.echo("No upstream servers configured.")
        return

    results = asyncio.run(_probe_servers(servers, timeout))

    if as_json:
        click.echo(json.dumps({"servers": results}))
        return

    click.echo(_hdr("Upstream Server Health"))
    click.echo("=" * 30)
    for name, info in results.items():
        if info["connected"]:
            click.echo(f"  {name}: {_ok('connected')} ({info['tools']} tools)")
        else:
            click.echo(f"  {name}: {_bad('DISCONNECTED')} — {info['error']}")
