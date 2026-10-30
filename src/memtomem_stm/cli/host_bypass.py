"""Report host registrations omitted by file-based discovery (#1269).

These rows never become import, prune, or registry reconciliation candidates.
Codex's effective list has no plugin provenance, so label its extra entries as
additional registrations rather than asserting that every one is a plugin.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import click

from memtomem_stm.cli._display import _disp
from memtomem_stm.mms.import_hosts import (
    _BLOCKED_IMPORT_NAMES,
    _is_self_reference,
    codex_declared_names,
)


def codex_extra_servers(cwd: Path) -> list[str]:
    # Late import avoids a module cycle with proxy's command registration.
    from memtomem_stm.cli.proxy import _plain_identifier, _run_codex_mcp

    executable = shutil.which("codex")
    if executable is None:
        return []
    try:
        result = _run_codex_mcp(
            [executable, "mcp", "list", "--json"],
            cwd=str(cwd.resolve()),
            stdin=subprocess.DEVNULL,
            timeout=5,
        )
        records = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, RecursionError):
        records = None
    if not isinstance(records, list):
        click.echo(
            "Note: could not list Codex MCP servers; additional registrations are not reported.",
            err=True,
        )
        return []

    known = codex_declared_names(cwd)
    extra: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or record.get("enabled") is not True:
            continue
        name, transport = record.get("name"), record.get("transport")
        if not _plain_identifier(name) or not isinstance(transport, dict):
            continue
        if name in known or name in _BLOCKED_IMPORT_NAMES:
            continue
        if transport.get("type") not in ("stdio", "streamable_http", "sse"):
            continue
        command, args = transport.get("command"), transport.get("args")
        if command is not None and not isinstance(command, str):
            continue
        if _is_self_reference({"command": command, "args": args if isinstance(args, list) else []}):
            continue
        extra.add(name)
    return sorted(extra)


def echo_codex_extras(cwd: Path, *, names: list[str] | None = None, err: bool = True) -> list[str]:
    if names is None:
        names = codex_extra_servers(cwd)
    if names:
        click.echo(
            f"Note: {len(names)} additional Codex MCP registration(s) are absent from STM's "
            "config-file scan (possibly plugin-provided or from another Codex config layer; "
            "not imported): " + ", ".join(_disp(name) for name in names),
            err=err,
        )
        click.echo(
            "  Inspect them in Codex; register with `mms add ... --validate` if needed, "
            "then disable the direct copy in its host.",
            err=err,
        )
    return names


def report_host_bypass(cwd: Path, host: str = "all") -> None:
    if host in {"all", "claude-code"}:
        from memtomem_stm.cli.proxy import _echo_plugin_bypass, _plugin_bypass_servers

        _echo_plugin_bypass(_plugin_bypass_servers(cwd), err=True)
    if host in {"all", "codex"}:
        echo_codex_extras(cwd)
