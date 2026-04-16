"""CLI commands for managing the memtomem-stm proxy gateway."""

from __future__ import annotations

import json
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


def _load(config_path: Path) -> dict[str, Any]:
    resolved = config_path.expanduser().resolve()
    if not resolved.exists():
        return {"enabled": True, "upstream_servers": {}}
    try:
        return json.loads(resolved.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        click.echo(f"Error: Failed to parse {resolved}: {exc}", err=True)
        raise SystemExit(1) from exc


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
    """memtomem-stm proxy gateway management."""


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
            click.echo("Run `memtomem-stm-proxy add` to create a configuration.")
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

    click.echo(f"{'NAME':<20} {'PREFIX':<10} {'TRANSPORT':<12} {'COMPRESSION':<12} COMMAND / URL")
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


@cli.command()
@click.argument("name")
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option("--command", "command", default="", help="Executable command (stdio).")
@click.option("--args", "args_str", default="", help="Space-separated arguments.")
@click.option("--prefix", required=True, help="Tool name prefix (e.g. 'fs').")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse", "streamable_http"]),
    default="stdio",
    show_default=True,
)
@click.option("--url", default="", help="Endpoint URL (SSE / HTTP).")
@click.option("--env", "env_pairs", multiple=True, metavar="KEY=VALUE")
@click.option(
    "--compression",
    type=click.Choice(["auto", "none", "truncate", "selective", "hybrid"]),
    default="auto",
    show_default=True,
)
@click.option(
    "--max-chars", "max_result_chars", type=click.IntRange(min=1), default=8000, show_default=True
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
) -> None:
    """Add an upstream MCP server to the proxy configuration."""
    path = Path(config_path)
    data = _load(path)
    servers: dict[str, Any] = data.setdefault("upstream_servers", {})

    if name in servers:
        click.echo(f"Error: server '{name}' already exists. Use `remove` first.", err=True)
        sys.exit(1)

    # VAL-1: prefix format validation
    if not _PREFIX_RE.match(prefix) or "__" in prefix:
        click.echo(
            f"Error: invalid prefix '{prefix}'. "
            "Must start with a letter, contain only letters/digits/underscores, "
            "and must not contain '__'.",
            err=True,
        )
        sys.exit(1)

    # VAL-2: duplicate prefix warning
    for srv_name, srv_cfg in servers.items():
        if srv_cfg.get("prefix") == prefix:
            click.echo(
                f"Warning: prefix '{prefix}' is already used by server '{srv_name}'. "
                "Tools with the same prefixed name will be shadowed at runtime.",
                err=True,
            )
            break

    # VAL-3: stdio requires --command
    if transport == "stdio" and not command:
        click.echo("Error: --command is required for stdio transport.", err=True)
        sys.exit(1)

    # VAL-4: sse/streamable_http requires --url
    if transport != "stdio" and not url:
        click.echo(f"Error: --url is required for {transport} transport.", err=True)
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
                click.echo(f"Error: malformed --args: {exc}", err=True)
                sys.exit(1)
    else:
        entry["url"] = url

    if env_pairs:
        env_dict: dict[str, str] = {}
        for pair in env_pairs:
            if "=" not in pair:
                click.echo(f"Error: --env must be KEY=VALUE, got: {pair}", err=True)
                sys.exit(1)
            k, v = pair.split("=", 1)
            if not k:
                click.echo(f"Error: --env key must be non-empty, got: {pair}", err=True)
                sys.exit(1)
            if k.upper() in _DANGEROUS_ENV_KEYS:
                click.echo(
                    f"Error: --env key '{k}' is blocked for security reasons "
                    "(could enable code injection in spawned processes).",
                    err=True,
                )
                sys.exit(1)
            env_dict[k] = v
        entry["env"] = env_dict

    servers[name] = entry
    _save(path, data)
    click.echo(f"Added server '{name}' (prefix={prefix})")


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
        click.echo(f"Error: server '{name}' not found.", err=True)
        sys.exit(1)

    if not yes:
        click.confirm(f"Remove server '{name}'?", abort=True)

    del servers[name]
    _save(path, data)
    click.echo(f"Removed server '{name}'.")
