"""CLI commands for managing the memtomem-stm proxy gateway."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import click

from memtomem_stm.cli.mms_host import host_group as _mms_host_group
from memtomem_stm.cli.mms_import import import_command as _mms_import_command
from memtomem_stm.cli.mms_project import project_group as _mms_project_group
from memtomem_stm.mms.import_hosts import (
    _DANGEROUS_ENV_KEYS,
    _desktop_config_path,
    _is_self_reference,
    _read_json_safely,
)
from memtomem_stm.proxy import tool_name_budget
from memtomem_stm.utils.fileio import atomic_write_text

_PREFIX_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")

_DEFAULT_CONFIG = Path("~/.memtomem/stm_proxy.json")
logger = logging.getLogger(__name__)

# `_DANGEROUS_ENV_KEYS`, `_BLOCKED_IMPORT_NAMES`, `_desktop_config_path`,
# `_read_json_safely`, `_is_self_reference` — re-imported from
# `mms.import_hosts` (single definition, shared with `mms import`).


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


def _split_args(args_str: str) -> list[str]:
    """Tokenize a stdio-server ``--args`` / prompt-args string.

    POSIX: plain ``shlex.split``. Windows: same POSIX-style quoting, but with
    backslash-escape disabled so ``D:\\a\\repo\\tests\\_x.py`` round-trips —
    default ``shlex.split`` would chew ``\\a``, ``\\t``, ``\\_`` as escapes
    and emit ``D:arepotests_x.py``, which would later be passed to the child
    interpreter as a (mangled, relative) path. Raises ``ValueError`` on
    unclosed quotes, matching ``shlex.split``'s contract.

    Trade-off: with ``escape=""`` an embedded quote-escape (``\\"`` inside a
    double-quoted token) is no longer recognized on Windows. Acceptable
    because Windows-native ``cmd.exe`` quoting uses ``^`` rather than ``\\``,
    and backslash-as-path-separator is the dominant case in this surface.
    """
    if sys.platform == "win32":
        lex = shlex.shlex(args_str, posix=True)
        lex.whitespace_split = True
        lex.commenters = ""
        lex.escape = ""
        return list(lex)
    return shlex.split(args_str)


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


# ── MCP client registration helpers ─────────────────────────────────────
#
# Mirror of the parent ``mm init`` pattern (see
# ``memtomem/packages/memtomem/src/memtomem/cli/init_cmd.py``
# lines 55–90, 526–534, 964–1001, 1051–1066). Kept at module top level so
# tests can ``monkeypatch.setattr`` individual helpers — particularly
# ``_run_claude_mcp``, which is the sole shell-out seam.
#
# Cross-repo note: the parent is the LTM server, STM is the proxy that
# chains upstream servers. Both register *themselves* (a single MCP
# server entry) with the downstream client — STM does **not** re-expose
# its configured upstreams to Claude Code; only the STM proxy itself is
# visible.


def _run_claude_mcp(cmd: list[str], timeout: int = 5) -> subprocess.CompletedProcess[str]:
    """Invoke the ``claude`` CLI — isolated so tests replace a single seam."""
    # ``encoding="utf-8"`` is explicit so non-ASCII bytes from the child don't
    # hit the platform default codec (cp1252/cp949 on Windows). See #302 P0.
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


# PEP 508 dependency specifier: the distribution name is the leading identifier
# before any extras (`[...]`), version spec (`==`, `>=`, ...), marker (`;`), or
# whitespace. Pinning the match to the start anchors the regex so `my-memtomem-stm`
# or `memtomem-stm-extension` can't hide a match inside the name.
_PEP508_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def _dep_name(spec: str) -> str | None:
    """Extract the package name from a PEP 508 dependency spec, or None.

    ``"memtomem-stm>=0.1"`` → ``"memtomem-stm"``
    ``"memtomem-stm[extra]==1.0"`` → ``"memtomem-stm"``
    ``"memtomem-stm-extension"`` → ``"memtomem-stm-extension"`` (different pkg)
    """
    if not isinstance(spec, str):
        return None
    match = _PEP508_NAME_RE.match(spec.strip())
    return match.group(1) if match else None


def _pyproject_references_stm(pyp: Path) -> bool:
    """True if ``pyp`` is STM's own pyproject.toml, or lists memtomem-stm as
    a dep. Parses the TOML via ``tomllib`` so comments, descriptions, and
    adjacent-name packages (``memtomem-stm-extension``) can't false-positive.
    """
    try:
        data = tomllib.loads(pyp.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    project = data.get("project")
    if not isinstance(project, dict):
        return False
    if project.get("name") == "memtomem-stm":
        return True
    dep_lists: list[list[Any]] = []
    deps = project.get("dependencies")
    if isinstance(deps, list):
        dep_lists.append(deps)
    optional = project.get("optional-dependencies")
    if isinstance(optional, dict):
        dep_lists.extend(v for v in optional.values() if isinstance(v, list))
    for dep_list in dep_lists:
        for spec in dep_list:
            if _dep_name(spec) == "memtomem-stm":
                return True
    return False


def _detect_install_type() -> tuple[str, list[str]]:
    """Return ``(server_cmd, server_args)`` to register with an MCP client.

    Branches:

    * Inside a ``pyproject.toml`` that is STM's own source checkout OR a
      user project that lists ``memtomem-stm`` in ``[project].dependencies``
      / ``[project].optional-dependencies`` → ``uv run --directory <root>
      memtomem-stm``.
    * Default (global install via ``uv tool install`` / ``pipx``, or a cwd
      with no relevant pyproject) → bare ``memtomem-stm`` console script.

    Both branches spawn the ``memtomem-stm`` console script, not ``mms``:
    ``mms`` is the click CLI group and exits 0 on no-subcommand, which closes
    the stdio pipe immediately and the MCP client reports "Failed to
    reconnect". ``memtomem-stm`` is the actual server entrypoint
    (``memtomem_stm.server:main``).

    Parent ``mm init`` keeps source/project as separate branches because
    the parent is a monorepo (``packages/`` subdir is the discriminator).
    STM has a flat layout so the two collapse into one.

    PEP 735 ``dependency-groups`` and tool-specific dev-dep tables (``[tool.uv
    .dev-dependencies]``, ``[tool.poetry.group.*.dependencies]``) are
    intentionally **not** matched — a dev-only dep usually means the user
    wants `uv run --group <name>` which the registration command can't pick.
    Bare ``memtomem-stm`` is the safer default; users can override manually.
    """
    check = Path.cwd()
    for _ in range(5):
        pyp = check / "pyproject.toml"
        if pyp.exists():
            if _pyproject_references_stm(pyp):
                return ("uv", ["run", "--directory", str(check), "memtomem-stm"])
            break
        check = check.parent
    return ("memtomem-stm", [])


def _check_already_registered(name: str = "memtomem-stm") -> bool:
    """Return True if Claude Code has *name* registered as an MCP server.

    Returns False on any subprocess error — callers should treat False as
    "safe to proceed with ``claude mcp add``". Pre-check exists because
    ``claude mcp add`` has no ``--force`` / ``--overwrite`` flag (verified
    against ``claude mcp add --help`` on 2026-04-21).
    """
    try:
        result = _run_claude_mcp(["claude", "mcp", "get", name])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _register_with_claude_code(
    server_cmd: str, server_args: list[str], *, name: str = "memtomem-stm"
) -> tuple[bool, str]:
    """Run ``claude mcp add``; return ``(success, failure_reason)``.

    ``failure_reason`` is one of ``"not_installed"``, ``"timeout"``,
    ``"failed"`` — callers map each to a user-facing message and always
    fall back to ``.mcp.json``.
    """
    cmd = ["claude", "mcp", "add", name, "-s", "user", "--", server_cmd, *server_args]
    try:
        result = _run_claude_mcp(cmd)
    except FileNotFoundError:
        return (False, "not_installed")
    except subprocess.TimeoutExpired:
        return (False, "timeout")
    if result.returncode == 0:
        return (True, "")
    return (False, "failed")


def _remove_from_claude_code(name: str = "memtomem-stm") -> None:
    """Best-effort ``claude mcp remove``; swallow errors (caller re-adds next)."""
    try:
        _run_claude_mcp(["claude", "mcp", "remove", name, "-s", "user"])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _write_mcp_json_for_stm(target_dir: Path, server_cmd: str, server_args: list[str]) -> Path:
    """Write or merge ``<target_dir>/.mcp.json`` with the memtomem-stm entry."""
    entry: dict[str, Any] = {"command": server_cmd}
    if server_args:
        entry["args"] = server_args
    mcp_path = target_dir / ".mcp.json"
    existing: dict[str, Any]
    if mcp_path.exists():
        try:
            loaded = json.loads(mcp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            loaded = None
        existing = loaded if isinstance(loaded, dict) else {}
    else:
        existing = {}
    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        existing["mcpServers"] = {"memtomem-stm": entry}
    else:
        servers["memtomem-stm"] = entry
    mcp_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return mcp_path


def _claude_desktop_config_hint() -> str:
    """Return the display path for Claude Desktop's config on the current OS.

    Claude Desktop is the only target in the paste-hint block whose config
    path is platform-specific — Cursor / Windsurf / Gemini all use the same
    dot-directory under ``$HOME`` across OSes.
    """
    if sys.platform == "darwin":
        return "~/Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform == "win32":
        return r"%APPDATA%\Claude\claude_desktop_config.json"
    # Linux and other POSIX: XDG default.
    return "~/.config/Claude/claude_desktop_config.json"


def _emit_mcp_paste_hints() -> None:
    """Per-editor paste targets for the generated ``.mcp.json``."""
    click.echo("    Cursor          → paste into ~/.cursor/mcp.json")
    click.echo("    Windsurf        → paste into ~/.codeium/windsurf/mcp_config.json")
    click.echo(f"    Claude Desktop  → paste into {_claude_desktop_config_hint()}")
    click.echo("    Gemini CLI      → paste into ~/.gemini/settings.json")
    click.echo("  (Claude Code picks up ./.mcp.json in this project automatically.)")


def _emit_skip_hints() -> None:
    """Manual-registration cheat sheet shown on option 3 (skip)."""
    server_cmd, server_args = _detect_install_type()
    cmd_str = " ".join([server_cmd, *server_args]) if server_args else server_cmd
    click.echo(f"{_ok('Next:')} connect your MCP client to memtomem-stm.")
    click.echo("")
    click.echo(f"  {_hdr('Claude Code (CLI):')}")
    click.echo(f"    claude mcp add memtomem-stm -s user -- {cmd_str}")
    click.echo("")
    click.echo(f"  {_hdr('Claude Desktop / JSON MCP config:')}")
    json_args = f', "args": {json.dumps(server_args)}' if server_args else ""
    click.echo(
        f'    {{ "mcpServers": {{ "memtomem-stm": {{ "command": "{server_cmd}"{json_args} }} }} }}'
    )
    click.echo("")
    click.echo(f"  Or run {_hdr('`mms register`')} later to re-run this wizard.")


def _prompt_mcp_choice() -> int:
    """Interactive 3-way prompt. Returns 1, 2, or 3."""
    click.echo(_hdr("Register memtomem-stm with an MCP client?"))
    click.echo("  [1] Add to Claude Code (run `claude mcp add` automatically)")
    click.echo("  [2] Generate .mcp.json in current directory")
    click.echo("      (for Cursor / Windsurf / Claude Desktop / Gemini)")
    click.echo("  [3] Skip — I'll configure it manually")
    return click.prompt("  Select", type=click.IntRange(1, 3), default=1, show_default=True)


_MCP_MODE_TO_CHOICE = {"claude": 1, "json": 2, "skip": 3}


def _run_mcp_integration(preselected: int | None = None) -> None:
    """Run the 3-way MCP registration flow.

    Called by both ``mms init`` (after config save) and ``mms register``
    (post-init re-entry path).

    ``preselected`` bypasses the interactive 3-way prompt when set (1, 2,
    or 3) — this is how ``--mcp`` / scripted callers skip stdin. When
    ``preselected`` is 1 and a duplicate is detected, we default to
    ``keep`` (non-destructive) rather than prompting, so CI / scripted
    callers don't hit ``click.Abort`` on EOF.
    """
    interactive = preselected is None
    choice = preselected if preselected is not None else _prompt_mcp_choice()
    click.echo("")

    if choice == 3:
        _emit_skip_hints()
        return

    server_cmd, server_args = _detect_install_type()

    if choice == 1:
        if _check_already_registered():
            click.echo(f"{_warn('Note:')} memtomem-stm is already registered with Claude Code.")
            if interactive:
                click.echo("    [1] Keep existing (recommended)")
                click.echo("    [2] Replace (remove + re-add)")
                action = click.prompt(
                    "  Select", type=click.IntRange(1, 2), default=1, show_default=True
                )
            else:
                # Non-interactive: always keep. Replacing requires an
                # explicit `claude mcp remove memtomem-stm` first.
                action = 1
            if action == 1:
                click.echo(f"  {_ok('Kept existing registration.')}")
                return
            _remove_from_claude_code()

        success, reason = _register_with_claude_code(server_cmd, server_args)
        if success:
            click.echo(f"  {_ok('Registered with Claude Code (user scope).')}")
            return

        reason_msg = {
            "not_installed": "claude CLI not found",
            "timeout": "claude mcp add timed out",
        }.get(reason, "claude mcp add failed")
        click.echo(f"  {_warn(reason_msg)} — falling back to .mcp.json.")

    # choice == 2, or choice == 1 fallback
    mcp_path = _write_mcp_json_for_stm(Path.cwd(), server_cmd, server_args)
    click.echo(f"  {_ok('Wrote')} {mcp_path}")
    _emit_mcp_paste_hints()


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def _stdin_is_tty() -> bool:
    """Indirection so tests can monkeypatch this seam.

    Same pattern as ``_should_use_tui`` (CHANGELOG #143) — Click's CliRunner
    replaces ``sys.stdin`` with a StringIO whose ``isatty()`` is False, so
    monkeypatching the function attribute directly doesn't reach the module's
    bound reference.
    """
    return bool(sys.stdin.isatty())


@click.group(context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
@click.version_option(
    package_name="memtomem-stm",
    prog_name="memtomem-stm",
    message="%(prog)s %(version)s",
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """memtomem-stm proxy gateway management.

    Output is colorized when writing to a terminal. Set NO_COLOR=1 to disable.

    Bare invocation (no subcommand) dispatches based on stdin: a TTY shows
    this help text, while a piped stdin (the MCP-client stdio case) boots
    the proxy as an MCP server. This lets any of the three entry points
    (``memtomem-stm``, ``memtomem-stm-proxy``, ``mms``) be registered as
    the MCP server command interchangeably (#260).
    """
    if ctx.invoked_subcommand is not None:
        return
    if _stdin_is_tty():
        click.echo(ctx.get_help())
        ctx.exit(0)
    # Non-TTY stdin → MCP client invocation. Lazy import keeps ``mms list`` /
    # ``mms add`` etc. from paying the server module's startup cost.
    from memtomem_stm.server import main as server_main

    server_main()


# ``version`` subcommand predates the ``--version`` flag (CHANGELOG #152) and
# is kept for backwards compatibility. Both paths must produce the exact same
# line so scripts that grep the version string don't care which entry point
# they use.
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
@click.option("--json", "as_json", is_flag=True, help="Output as JSON for scripting.")
def list_servers(config_path: str, *, as_json: bool = False) -> None:
    """List configured upstream servers."""
    path = Path(config_path)
    resolved = path.expanduser().resolve()

    # Missing-config handling matches ``status`` so a user troubleshooting
    # "why isn't my server listed?" can tell whether they're pointed at the
    # wrong path vs. a real-but-empty config (#221).
    if not resolved.exists():
        if as_json:
            click.echo(json.dumps({"error": "config_not_found", "path": str(resolved)}))
        else:
            click.echo(f"Config not found: {resolved}")
            click.echo("Run `mms add` (or `mms init`) to create a configuration.")
        return

    data = _load(path)
    servers: dict[str, Any] = data.get("upstream_servers", {})

    if as_json:
        click.echo(
            json.dumps(
                {"config_path": str(resolved), "servers": servers},
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    if not servers:
        click.echo("No upstream servers configured.")
        return

    # ``streamable_http`` is 15 chars — wider than the prior ``<12``
    # transport column, so rows for HTTP servers used to push the
    # COMPRESSION and COMMAND/URL columns out of alignment with the
    # header. ``<16`` fits every Choice value the ``--transport`` flag
    # accepts (``stdio`` / ``sse`` / ``streamable_http``) with at least
    # one space of padding.
    click.echo(
        _hdr(f"{'NAME':<20} {'PREFIX':<10} {'TRANSPORT':<16} {'COMPRESSION':<12} COMMAND / URL")
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
        click.echo(f"{name:<20} {prefix:<10} {transport:<16} {compression:<12} {detail}")
    click.echo(f"\n{len(servers)} server(s) configured.")


@cli.command()
@click.argument("name", required=False)
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option("--command", "command", default="", help="Executable command (stdio).")
@click.option("--args", "args_str", default="", help="Space-separated arguments.")
@click.option(
    "--prefix",
    default="",
    help="Tool namespace (e.g. 'fs' -> tools appear as fs__read_file). "
    "Required unless --from-clients is used.",
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
@click.option(
    "--from-clients",
    "--import",
    "from_clients",
    is_flag=True,
    default=False,
    help="Import additional servers interactively from existing MCP clients "
    "(Claude Desktop / Code, project .mcp.json). Reuses init's discovery + "
    "TUI flow. Skips candidates already registered in this config. "
    "Incompatible with NAME / --prefix / --command / --args / --url / --env.",
)
@click.option(
    "--prune",
    "prune",
    is_flag=True,
    default=False,
    help="After a successful --from-clients/--import, remove the direct "
    "registrations from each source MCP client so tools are only reachable "
    "via STM. Default: interactive prompt on TTY, skip on non-TTY. Only "
    "valid with --from-clients/--import.",
)
def add(
    name: str | None,
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
    from_clients: bool,
    prune: bool,
) -> None:
    """Add an upstream MCP server to the proxy configuration."""
    path = Path(config_path)

    if from_clients:
        # Guard against mixing interactive-import with single-server manual
        # flags — silently ignoring them would be surprising. `--validate` /
        # `--timeout` / `--config` are shared with both paths and don't
        # conflict.
        conflicts: list[str] = []
        if name:
            conflicts.append("NAME")
        if prefix:
            conflicts.append("--prefix")
        if command:
            conflicts.append("--command")
        if args_str:
            conflicts.append("--args")
        if url:
            conflicts.append("--url")
        if env_pairs:
            conflicts.append("--env")
        if conflicts:
            raise click.UsageError(
                f"--from-clients cannot be combined with: {', '.join(conflicts)}."
            )
        _add_from_clients(path, validate=validate, validate_timeout=validate_timeout, prune=prune)
        return

    if prune:
        # --prune only has semantics when we know which servers were just
        # imported from which sources. The manual path has neither, so the
        # flag would be a silent no-op — surface the mistake instead.
        raise click.UsageError("--prune requires --from-clients/--import.")

    # Manual single-server path — NAME and --prefix were previously click-
    # required. Relaxed to optional above so --from-clients can omit them;
    # re-enforce here so the manual path still has the same contract.
    if not name:
        raise click.UsageError("Missing argument 'NAME' (or pass --from-clients).")
    if not prefix:
        raise click.UsageError("Missing option '--prefix' (or pass --from-clients).")

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

    # VAL-1b: prefix length vs the 64-char MCP tool name limit (#261). We
    # don't have the upstream tool inventory at ``add`` time, so this is a
    # sanity check on the prefix alone. The boot-time check in
    # ``ProxyManager._connect_server`` does per-tool enforcement once
    # ``tools/list`` is available.
    hard_limit = tool_name_budget.prefix_hard_limit()
    warn_at = tool_name_budget.prefix_warn_threshold()
    assumed_server = tool_name_budget.client_server_name()
    if len(prefix) > hard_limit:
        click.echo(
            f"{_err('Error:')} prefix '{prefix}' is {len(prefix)} chars; the "
            f"max for client server name '{assumed_server}' is {hard_limit}. "
            f"Even a 1-char upstream tool name would overflow the "
            f"{tool_name_budget.TOOL_NAME_LIMIT}-char MCP limit.",
            err=True,
        )
        click.echo(
            "  Fix one of:\n"
            "    • Use a shorter --prefix.\n"
            "    • Register STM as 'mms' in your MCP client config (saves "
            "9 chars), and export MMS_CLIENT_SERVER_NAME=mms so this check\n"
            "      loosens to match.",
            err=True,
        )
        sys.exit(1)
    if len(prefix) > warn_at:
        max_tool = tool_name_budget.TOOL_NAME_LIMIT - tool_name_budget.overhead() - len(prefix)
        click.echo(
            f"{_warn('Warning:')} prefix '{prefix}' ({len(prefix)} chars) "
            f"leaves only {max_tool} chars for upstream tool names — longer "
            f"ones will be silently dropped by clients. Consider a shorter "
            f"--prefix, or register STM as 'mms' in your client config. "
            f"Proceeding.",
            err=True,
        )

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
                entry["args"] = _split_args(args_str)
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


def _prompt_prefix(default: str | None = None) -> str:
    """Prompt for a prefix until it passes the same rules as ``add --prefix``."""
    while True:
        value = click.prompt(
            "Tool prefix (letters/digits/underscores, e.g. 'fs')",
            type=str,
            default=default,
            show_default=default is not None,
        )
        if _PREFIX_RE.match(value) and "__" not in value:
            return value
        click.echo(
            f"  {_warn('Invalid:')} must start with a letter, contain only letters/digits/"
            "underscores, and not contain '__'. Try again."
        )


# ── discovery of already-registered MCP servers ─────────────────────────
#
# Reading external-client configs is best-effort: files may not exist, may be
# malformed, or may have shapes that don't translate (e.g. wrapper types we
# don't support). Discovery never raises — missing/bad sources just yield
# zero candidates and fall back to the manual prompt flow.


def _normalize_client_entry(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map a client-config MCP entry to an STM upstream-server entry.

    Returns ``None`` if the entry's shape isn't one we can import. STM-shape
    requires a concrete transport and either ``command`` (stdio) or ``url``
    (sse/streamable_http); ``prefix`` is intentionally left out so the caller
    can prompt for it.
    """
    # Client configs use `type: "http"|"sse"` (Claude Code) or omit it
    # entirely (stdio). Desktop config is stdio-only in practice.
    type_hint = (raw.get("type") or "").lower()
    command = raw.get("command")
    url = raw.get("url")

    if type_hint == "sse" and isinstance(url, str) and url:
        transport = "sse"
    elif type_hint in ("http", "streamable_http") and isinstance(url, str) and url:
        transport = "streamable_http"
    elif isinstance(command, str) and command:
        transport = "stdio"
    elif isinstance(url, str) and url:
        # url but no type → best guess; Claude Code sometimes omits `type`.
        transport = "streamable_http"
    else:
        return None

    entry: dict[str, Any] = {
        "transport": transport,
        "compression": "auto",
        "max_result_chars": 8000,
    }
    if transport == "stdio":
        entry["command"] = command
        args = raw.get("args")
        if isinstance(args, list) and all(isinstance(a, str) for a in args):
            entry["args"] = list(args)
        env = raw.get("env")
        if isinstance(env, dict):
            safe_env = {
                str(k): str(v)
                for k, v in env.items()
                if isinstance(k, str) and k.upper() not in _DANGEROUS_ENV_KEYS
            }
            if safe_env:
                entry["env"] = safe_env
    else:
        entry["url"] = url
    return entry


def _discover_candidates(cwd: Path) -> list[dict[str, Any]]:
    """Scan known MCP-client configs and return importable candidates.

    Each candidate dict is ``{name, source, entry}`` where ``entry`` is an
    already-normalized STM upstream-server entry (sans prefix). The first
    source to claim a name wins; later duplicates are dropped with a
    ``duplicate_in`` note so the UI can indicate the overlap.
    """
    sources: list[tuple[str, dict[str, Any]]] = []

    # 1. Project-scope ``.mcp.json`` in cwd (Claude Code project config).
    proj = _read_json_safely(cwd / ".mcp.json")
    if proj and isinstance(proj.get("mcpServers"), dict):
        sources.append((".mcp.json (project)", proj["mcpServers"]))

    # 2. Claude Code ~/.claude.json user-scope + per-project entries.
    cc = _read_json_safely(Path("~/.claude.json").expanduser())
    if cc:
        if isinstance(cc.get("mcpServers"), dict):
            sources.append(("Claude Code (user)", cc["mcpServers"]))
        projects = cc.get("projects")
        if isinstance(projects, dict):
            # Match on resolved cwd so we don't duplicate entries across sibling projects.
            resolved_cwd = str(cwd.resolve())
            proj_entry = projects.get(resolved_cwd)
            if isinstance(proj_entry, dict) and isinstance(proj_entry.get("mcpServers"), dict):
                sources.append(("Claude Code (project)", proj_entry["mcpServers"]))

    # 3. Claude Desktop (macOS path only; non-macOS → file absent → skipped).
    desktop = _read_json_safely(_desktop_config_path())
    if desktop and isinstance(desktop.get("mcpServers"), dict):
        sources.append(("Claude Desktop", desktop["mcpServers"]))

    seen: dict[str, dict[str, Any]] = {}
    for source_label, servers in sources:
        if not isinstance(servers, dict):
            continue
        for name, raw in servers.items():
            if not isinstance(name, str) or not isinstance(raw, dict):
                continue
            entry = _normalize_client_entry(raw)
            if entry is None or _is_self_reference(entry):
                continue
            if name in seen:
                seen[name].setdefault("duplicate_in", []).append(source_label)
                continue
            seen[name] = {"name": name, "source": source_label, "entry": entry}
    return list(seen.values())


def _format_candidate_detail(entry: dict[str, Any]) -> str:
    transport = entry.get("transport", "stdio")
    if transport == "stdio":
        parts = [entry.get("command", "")]
        parts.extend(entry.get("args", []))
        return f"[stdio] {' '.join(p for p in parts if p).strip()}"
    return f"[{transport}] {entry.get('url', '')}"


def _source_removal_hint(name: str, source: str) -> str:
    """One informational shell-line telling the user how to remove a server
    from the client that originated it.

    STM never edits source client configs itself — the hint is copy-paste
    only. Scope-label mapping matches the ``claude mcp remove -s`` flag:
    ``Claude Code (project)`` is the per-project entry in ``~/.claude.json``,
    which the Claude Code CLI calls ``local``.
    """
    if source == "Claude Code (user)":
        return f"claude mcp remove {name} -s user"
    if source == "Claude Code (project)":
        return f"claude mcp remove {name} -s local"
    if source == ".mcp.json (project)":
        return f"claude mcp remove {name} -s project"
    if source == "Claude Desktop":
        return f"# Edit {_desktop_config_path()} and remove '{name}' under mcpServers."
    return f"# Remove '{name}' from {source}."


def _print_source_removal_hints(imported_candidates: list[dict[str, Any]]) -> None:
    """Print the post-import dual-registration warning, deduped across sources.

    A single server can be discovered in more than one client (e.g. Claude
    Code and Claude Desktop both list ``playwright``), so each candidate's
    primary ``source`` plus any ``duplicate_in`` entries all get a line.
    """
    if not imported_candidates:
        return
    lines: list[str] = []
    seen: set[str] = set()
    for cand in imported_candidates:
        for src in [cand["source"], *(cand.get("duplicate_in") or [])]:
            line = _source_removal_hint(cand["name"], src)
            if line in seen:
                continue
            seen.add(line)
            lines.append(line)
    if not lines:
        return
    click.echo("")
    click.echo(
        f"{_warn('Note:')} the server(s) above are still registered in their source MCP client(s)."
    )
    click.echo("      Until removed there, tools are advertised on two paths (direct + via STM)")
    click.echo("      and direct calls bypass STM's compression, caching, and LTM surfacing.")
    click.echo("      To route through STM only, remove the direct registrations:")
    for line in lines:
        click.echo(f"        {line}")


# ── Source-client prune (#226) ─────────────────────────────────────────────
#
# Deliberate, opt-in exception to the otherwise read-only source-client
# contract (see ``_discover_candidates`` docstring and PR #203 for the hint-
# only predecessor). Writer surface is kept tight: a ``claude mcp remove``
# shell-out for Claude Code / ``.mcp.json`` scopes, and an atomic JSON
# rewrite for Claude Desktop. Any failure is non-fatal — the import stays
# put and the user is shown the manual-command fallback.


def _claude_mcp_remove(name: str, scope: str) -> tuple[bool, str | None]:
    """Shell out to ``claude mcp remove <name> -s <scope>``.

    Returns ``(ok, error_message_or_None)``. Treats any non-zero exit or
    subprocess error as a non-fatal failure so the caller can surface the
    manual command instead of aborting the import.
    """
    try:
        result = _run_claude_mcp(["claude", "mcp", "remove", name, "-s", scope], timeout=5)
    except FileNotFoundError:
        return (False, "`claude` CLI not on PATH")
    except subprocess.TimeoutExpired:
        return (False, "`claude mcp remove` timed out")
    except OSError as exc:
        return (False, f"OS error invoking `claude`: {exc}")
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        return (False, msg or f"`claude mcp remove` exited {result.returncode}")
    return (True, None)


def _desktop_json_remove_entry(name: str) -> tuple[bool, str | None]:
    """Delete ``mcpServers[name]`` from Claude Desktop's config, atomic write.

    Returns ``(ok, error_message_or_None)``. The atomic write uses
    :func:`atomic_write_text` for the same reason :func:`_save` does — a
    running Claude Desktop instance may re-read the file mid-rewrite, and
    ``os.replace`` is the only way to keep the update coherent.
    """
    path = _desktop_config_path()
    if not path.exists():
        return (False, f"{path} not found")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return (False, f"read error: {exc}")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return (False, f"parse error: {exc}")
    if not isinstance(data, dict):
        return (False, f"{path} top-level is not an object")
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return (False, f"'{name}' not registered in {path}")
    del servers[name]
    try:
        atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        return (False, f"write error: {exc}")
    return (True, None)


def _prune_from_source(name: str, source: str) -> tuple[bool, str | None]:
    """Dispatch the prune action per source label used by ``_discover_candidates``.

    Scope-label mapping matches :func:`_source_removal_hint` — the remediation
    hint and the prune writer agree on which ``claude mcp remove -s <scope>``
    flag maps to which label. Unknown sources are refused rather than guessed.
    """
    if source == "Claude Code (user)":
        return _claude_mcp_remove(name, "user")
    if source == "Claude Code (project)":
        return _claude_mcp_remove(name, "local")
    if source == ".mcp.json (project)":
        return _claude_mcp_remove(name, "project")
    if source == "Claude Desktop":
        return _desktop_json_remove_entry(name)
    return (False, f"unknown source label: {source}")


def _prune_imported_candidates(
    imported_candidates: list[dict[str, Any]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """Prune each imported candidate from every source that registered it.

    A single candidate can show up in multiple sources (primary ``source``
    plus ``duplicate_in``) — all are pruned so the dual-path collapses.
    Returns ``(pruned, failed)`` where ``pruned`` is ``[(name, source), ...]``
    and ``failed`` is ``[(name, source, error), ...]``.
    """
    pruned: list[tuple[str, str]] = []
    failed: list[tuple[str, str, str]] = []
    for cand in imported_candidates:
        for src in [cand["source"], *(cand.get("duplicate_in") or [])]:
            ok, err = _prune_from_source(cand["name"], src)
            if ok:
                pruned.append((cand["name"], src))
            else:
                failed.append((cand["name"], src, err or "unknown error"))
    return pruned, failed


def _confirm_prune_prompt(imported_candidates: list[dict[str, Any]]) -> bool:
    """Post-import prompt offering to prune direct registrations. Default No.

    Only called on TTY (callers gate via :func:`_should_use_tui`). Lists the
    exact (name, source) pairs so the user can see what would be written
    before consenting — the prompt is the point where the read-only invariant
    relaxes and visibility matters.
    """
    click.echo("")
    click.echo("The server(s) above are still registered directly in their source MCP client(s).")
    click.echo("Removing the direct registrations routes the tools through STM only")
    click.echo(
        "(adds compression / caching / LTM surfacing; avoids duplicate tool advertisements)."
    )
    click.echo("Affected entries:")
    seen: set[tuple[str, str]] = set()
    for cand in imported_candidates:
        for src in [cand["source"], *(cand.get("duplicate_in") or [])]:
            key = (cand["name"], src)
            if key in seen:
                continue
            seen.add(key)
            click.echo(f"    {cand['name']} — {src}")
    return click.confirm("Remove from source(s)?", default=False)


def _handle_source_prune(
    imported_candidates: list[dict[str, Any]],
    *,
    prune: bool,
) -> None:
    """Post-import prune + reporting.

    Three paths based on the caller's ``prune`` flag and the TTY gate:

    * ``prune=True`` → prune unconditionally.
    * ``prune=False`` + TTY → interactive prompt, default No.
    * ``prune=False`` + non-TTY → skip prune entirely, fall through to the
      #203 hint-only warning.

    Partial failures (e.g. one source prunes, another fails) surface a
    per-entry warning plus the manual-command fallback for the failed rows
    only, so the user always has a path forward even if the writer is
    degraded.
    """
    if not imported_candidates:
        return

    if prune:
        should_prune = True
    elif _should_use_tui():
        should_prune = _confirm_prune_prompt(imported_candidates)
    else:
        should_prune = False

    if not should_prune:
        _print_source_removal_hints(imported_candidates)
        return

    pruned, failed = _prune_imported_candidates(imported_candidates)

    if pruned:
        click.echo("")
        click.echo(f"{_ok('Removed from source client(s):')}")
        for name, src in pruned:
            click.echo(f"  {name} — {src}")

    if failed:
        click.echo("")
        click.echo(
            f"{_warn('Warning:')} could not remove {len(failed)} direct registration(s):",
            err=True,
        )
        for name, src, err in failed:
            click.echo(f"  {name} ({src}): {err}", err=True)
        click.echo("", err=True)
        click.echo("Run the following to remove them manually:", err=True)
        for name, src, _ in failed:
            click.echo(f"  {_source_removal_hint(name, src)}", err=True)


def _find_dual_registered(
    stm_upstreams: dict[str, dict[str, Any]], cwd: Path
) -> list[dict[str, Any]]:
    """Return source-client candidates whose name **and identity** match an STM upstream.

    Name alone isn't enough: a user can register an unrelated server under
    the same name in a source client (different command / URL / args) and
    would reasonably expect ``mms prune`` not to touch it. Mirrors the dedup
    logic :func:`_add_from_clients` uses in the other direction (name +
    :func:`_server_signature` match), so the two sides of the import↔prune
    round-trip agree on what "the same server" means.

    An entry with no extractable signature (missing command / URL) is a
    degraded match: we still include it on a name hit rather than silently
    dropping it, since refusing to prune would surprise users who onboarded
    via ``init`` and never edited the entry. The returned shape matches
    :func:`_discover_candidates` so callers can pass the result directly to
    :func:`_prune_imported_candidates` / the ``_handle_source_prune`` machinery.
    """
    all_candidates = _discover_candidates(cwd)
    dual: list[dict[str, Any]] = []
    for cand in all_candidates:
        name = cand["name"]
        if name not in stm_upstreams:
            continue
        stm_sig = _server_signature(stm_upstreams[name])
        src_sig = _server_signature(cand["entry"])
        # Both sides have a signature and they differ → intentionally distinct
        # servers sharing a name. Skip rather than prune the unrelated entry.
        if stm_sig is not None and src_sig is not None and stm_sig != src_sig:
            continue
        dual.append(cand)
    return dual


def _suggest_prefix(name: str, taken: set[str]) -> str:
    """Derive a valid-ish default prefix from a server name.

    Not authoritative — the user is re-prompted if invalid. Avoids clashing
    with prefixes already chosen in the current init run.
    """
    base = re.sub(r"[^a-zA-Z0-9_]", "_", name).strip("_") or "srv"
    if not base[0].isalpha():
        base = "s_" + base
    base = base.replace("__", "_")
    candidate = base
    n = 2
    while candidate in taken:
        candidate = f"{base}{n}"
        n += 1
    return candidate


def _parse_selection(raw: str, count: int) -> list[int] | None:
    """Parse ``"1,3,5"`` / ``"all"`` / ``""`` / ``"none"`` into 0-based indices.

    Returns ``None`` on parse error so the caller can reprompt. Empty input
    and ``"none"`` yield ``[]`` (explicit skip). Used as the non-TTY fallback
    for :func:`_pick_imports`; interactive TTY sessions use the checkbox UI.
    """
    text = raw.strip().lower()
    if text in ("", "none", "skip", "n"):
        return []
    if text in ("all", "a", "*"):
        return list(range(count))
    picks: list[int] = []
    for tok in text.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if not tok.isdigit():
            return None
        idx = int(tok) - 1
        if idx < 0 or idx >= count:
            return None
        if idx not in picks:
            picks.append(idx)
    return picks


def _should_use_tui() -> bool:
    """Gate for the interactive checkbox UI.

    Disabled when:
    * stdin isn't a TTY (CliRunner tests, shell pipes, CI) — there's no way
      to read arrow-key escape sequences, so fall back to comma-number input.
    * ``MMS_NO_TUI`` is set — explicit opt-out for users who pipe answers
      from scripts or hit terminal-compatibility issues.
    """
    if os.environ.get("MMS_NO_TUI"):
        return False
    return bool(sys.stdin.isatty())


# Sentinel values returned by the questionary.select loop in addition to
# integer indices. Distinct from any valid index so a dict-based toggle set
# can't collide.
_TUI_CONFIRM = "__confirm__"
_TUI_CANCEL = "__cancel__"


def _tui_style() -> Any:
    """High-contrast style for the import-selector prompt.

    questionary's built-in palette is conservative and can read as
    near-default in themed terminals (Ghostty, iTerm2 with custom color
    schemes, etc.), making the active-row "bar" indistinct. Using
    ``ansi*`` color names keeps us in the user's own terminal palette —
    so bright cyan actually IS that terminal's bright cyan, whatever the
    theme has redefined it to be.

    Deliberately *does not* style ``class:selected``. questionary's
    ``select()`` applies that class to the choice matching ``default=``
    (used here to preserve cursor position across re-renders), so
    colouring it ends up looking like a persistent highlight on the
    last-toggled row — the exact bug reported. Check-state is already
    conveyed by the ``[v]``/``[ ]`` marker in the title; we don't need
    a second visual channel for it.
    """
    from questionary import Style

    return Style(
        [
            ("qmark", "fg:ansibrightblue bold"),
            ("question", "bold"),
            ("pointer", "fg:ansibrightcyan bold"),
            ("highlighted", "fg:ansibrightcyan bold"),
            ("separator", "fg:ansibrightblack"),
            ("instruction", "fg:ansibrightblack"),
            ("answer", "fg:ansigreen bold"),
        ]
    )


def _pick_imports_tui(candidates: list[dict[str, Any]]) -> list[int]:
    """Enter-to-toggle select loop with explicit Confirm / Cancel.

    Chose this over ``questionary.checkbox`` after user feedback:
    checkbox's space-to-toggle + enter-to-confirm is technically standard
    but non-obvious on first exposure (and the default ``●``/``○`` glyphs
    are visually ambiguous). The select-loop instead matches the mental
    model "press enter on what I want, scroll down to Confirm when done,"
    with explicit ``[v]``/``[ ]`` markers in the choice titles.

    Returns sorted 0-based indices. ``[]`` on Cancel or empty Confirm —
    the caller treats either as "user declined to import anything,
    abort the wizard without saving."
    """
    import questionary

    picks: set[int] = set()
    cursor: object = 0

    while True:
        choices: list[Any] = []
        for i, c in enumerate(candidates):
            marker = "[v]" if i in picks else "[ ]"
            title = (
                f"{marker}  {c['name']:<18}  "
                f"{_format_candidate_detail(c['entry'])}  — from {c['source']}"
            )
            choices.append(questionary.Choice(title=title, value=i))
        choices.append(questionary.Separator())
        choices.append(
            questionary.Choice(
                title=f"Confirm — import {len(picks)} server(s)",
                value=_TUI_CONFIRM,
            )
        )
        choices.append(questionary.Choice(title="Cancel", value=_TUI_CANCEL))

        # questionary's stubs declare ``default: str | Choice | dict | None``
        # but the runtime matches on equality against Choice.value, so our
        # int-or-sentinel cursor works fine. Ignore the type mismatch rather
        # than muddy the cursor's declared type to suit the stub.
        #
        # ``use_jk_keys`` + ``use_emacs_keys`` are enabled as backup bindings
        # in case arrow-key events don't reach the TUI (Ghostty keybindings,
        # tmux prefix collisions, etc.) — j/k and Ctrl-N/Ctrl-P are the
        # conventional fallbacks and cost nothing to leave on.
        result = questionary.select(
            "Select servers to import (↑↓ or j/k to move, Enter toggles, scroll to Confirm):",
            choices=choices,
            default=cursor,  # type: ignore[arg-type]
            style=_tui_style(),
            use_arrow_keys=True,
            use_jk_keys=True,
            use_emacs_keys=True,
        ).ask()

        if result is None or result == _TUI_CANCEL:
            return []
        if result == _TUI_CONFIRM:
            return sorted(picks)
        # Integer index → toggle and keep the cursor in place so the next
        # render starts on the row the user just acted on (no disorienting
        # jump to the top).
        cursor = result
        if result in picks:
            picks.remove(result)
        else:
            picks.add(result)


def _pick_imports(candidates: list[dict[str, Any]]) -> list[int]:
    """Prompt the user to choose which discovered candidates to import.

    TTY → :func:`_pick_imports_tui` (enter-to-toggle loop). Non-TTY → the
    comma-number prompt below, with reprompt on parse error so a
    fat-finger entry doesn't silently abort the wizard.
    """
    if _should_use_tui():
        return _pick_imports_tui(candidates)

    while True:
        raw = click.prompt(
            "Select servers to import (e.g. '1,3', 'all', or empty to skip)",
            type=str,
            default="",
            show_default=False,
        )
        picks = _parse_selection(raw, len(candidates))
        if picks is not None:
            return picks
        click.echo(
            f"  {_warn('Invalid:')} use comma-separated numbers 1..{len(candidates)}, "
            "'all', or leave blank."
        )


def _server_signature(cfg: dict[str, Any]) -> tuple[str, ...] | None:
    """Best-effort content signature for dedup between discovered + registered.

    Same ``(transport, command, *args)`` or ``(transport, url)`` → assume
    the user already imported this server under a different name. Returns
    ``None`` when the entry is missing the identifying field (caller falls
    back to name-based match only).
    """
    transport = cfg.get("transport", "stdio")
    if transport == "stdio":
        cmd = cfg.get("command", "") or ""
        if not cmd:
            return None
        args = cfg.get("args") or []
        if not isinstance(args, list):
            args = []
        return ("stdio", cmd, *(str(a) for a in args))
    url = cfg.get("url", "") or ""
    if not url:
        return None
    return (transport, url)


def _add_from_clients(
    config_path: Path,
    *,
    validate: bool,
    validate_timeout: int,
    prune: bool = False,
) -> None:
    """Interactive bulk-import path for ``mms add --from-clients``.

    Reuses init's discovery + selection + prefix-prompt helpers. Difference
    from init: merges into an existing config (rather than creating one) and
    filters out candidates that match servers already registered, so the TUI
    only shows *new* options.
    """
    data = _load(config_path)
    servers: dict[str, Any] = data.setdefault("upstream_servers", {})

    all_candidates = _discover_candidates(Path.cwd())
    if not all_candidates:
        click.echo("No MCP servers found in Claude Desktop / Code / .mcp.json.")
        click.echo("Use `mms add <NAME> --prefix ... --command ...` to register one manually.")
        return

    existing_names = set(servers.keys())
    existing_signatures = {
        sig for srv_cfg in servers.values() if (sig := _server_signature(srv_cfg)) is not None
    }

    new_candidates: list[dict[str, Any]] = []
    for cand in all_candidates:
        cand_name = cand["name"]
        entry = cand["entry"]
        if cand_name in existing_names:
            click.echo(
                f"  {_warn('Skipping:')} '{cand_name}' — already registered.",
                err=True,
            )
            continue
        sig = _server_signature(entry)
        if sig is not None and sig in existing_signatures:
            click.echo(
                f"  {_warn('Skipping:')} '{cand_name}' — matches an existing server "
                "by command/url.",
                err=True,
            )
            continue
        new_candidates.append(cand)

    if not new_candidates:
        click.echo("All discovered servers are already registered.")
        return

    resolved = config_path.expanduser().resolve()
    click.echo(_hdr(f"Found {len(new_candidates)} new MCP server(s) to import:"))
    for i, cand in enumerate(new_candidates, 1):
        dup = cand.get("duplicate_in")
        dup_hint = f"  (also in: {', '.join(dup)})" if dup else ""
        click.echo(
            f"  {i:>2}. {cand['name']:<18} {_format_candidate_detail(cand['entry'])}"
            f"    — from {cand['source']}{dup_hint}"
        )
    click.echo("")
    picks = _pick_imports(new_candidates)

    if not picks:
        click.echo("")
        click.echo(f"{_ok('No servers selected.')} Config unchanged.")
        return

    # Seed the "taken prefixes" set with what's already in the config so
    # suggestions don't collide with prior registrations.
    used_prefixes: set[str] = {p for srv_cfg in servers.values() if (p := srv_cfg.get("prefix"))}
    imported: dict[str, dict[str, Any]] = {}
    click.echo("")
    for idx in picks:
        cand = new_candidates[idx]
        click.echo(_hdr(f"Configuring '{cand['name']}'"))
        suggested = _suggest_prefix(cand["name"], used_prefixes)
        prefix = _prompt_prefix(default=suggested)
        used_prefixes.add(prefix)
        entry = {"prefix": prefix, **cand["entry"]}
        imported[cand["name"]] = entry

    if validate:
        click.echo("")
        click.echo(f"Validating {len(imported)} server(s) (timeout={validate_timeout}s)...")
        probes = asyncio.run(_probe_servers(imported, validate_timeout))
        for n, probe in probes.items():
            if probe["connected"]:
                click.echo(f"  {_ok('Reachable:')} {n} — {probe['tools']} tool(s).")
            else:
                click.echo(f"  {_warn('Warning:')} {n} — probe failed: {probe['error']}", err=True)
                click.echo("  Saving anyway. Run `mms health` later to retry.", err=True)

    servers.update(imported)
    _save(config_path, data)

    click.echo("")
    click.echo(f"{_ok('Added')} {len(imported)} server(s) to {resolved}:")
    for n, e in imported.items():
        click.echo(f"  {n:<20} prefix={e['prefix']}  {_format_candidate_detail(e)}")

    # Source clients still hold the direct registrations. Without a prune,
    # tools surface on two paths (client → upstream and client → STM →
    # upstream) and the direct path bypasses compression, caching, and LTM
    # surfacing. ``_handle_source_prune`` picks between the --prune flag,
    # the interactive prompt (TTY), and the hint-only fallback (non-TTY /
    # user declined).
    _handle_source_prune([new_candidates[i] for i in picks], prune=prune)


# Token-aware budget presets keyed by primary content language. KO is the
# only language with empirical calibration shipped here (PR #274's 13-pair
# EN/KO doc corpus measured against ``cl100k_base``); EN is intentionally
# empty so the resulting config matches the existing code defaults exactly
# without per-key noise. ZH/JA placeholders are deliberately omitted until
# an analogous measurement lands — guessing by typology would defeat the
# point of the empirical-calibration framing in PR #274.
_LANG_PRESET_KO = "Korean (chars_per_token=1.85, max_result_tokens=2000 per server)"
_LANG_PRESET_EN = "English (no language-specific tuning — code defaults)"
_LANG_PRESETS: dict[str, dict[str, Any]] = {
    "en": {},
    "ko": {
        "chars_per_token": 1.85,
        "min_response_chars": 230,
        "default_max_result_chars": 8500,
        "_per_server": {
            "max_result_tokens": 2000,
            "chars_per_token": 1.85,
        },
    },
}


def _prompt_language() -> str:
    """Single-choice language preset prompt with TUI / click fallback.

    Returns ``"en"`` on non-TTY (CliRunner tests, pipes, CI) — the
    scriptable path is ``--lang``, not stdin emulation. TTY with TUI
    available → questionary select. TTY with ``MMS_NO_TUI=1`` (or
    questionary import failure) → ``click.prompt`` with a Choice.
    """
    if not sys.stdin.isatty():
        return "en"

    if _should_use_tui():
        try:
            import questionary

            choice = questionary.select(
                "Primary content language for token-aware budgets:",
                choices=[
                    questionary.Choice(title=_LANG_PRESET_EN, value="en"),
                    questionary.Choice(title=_LANG_PRESET_KO, value="ko"),
                ],
                default="en",
                style=_tui_style(),
                use_arrow_keys=True,
                use_jk_keys=True,
                use_emacs_keys=True,
            ).ask()
            return choice if choice in _LANG_PRESETS else "en"
        except Exception:
            # Fall through to click prompt if questionary blows up
            # (terminal incompatibility, etc.). Same defensive pattern as
            # ``_pick_imports`` already uses.
            pass

    return click.prompt(
        "Primary content language",
        type=click.Choice(list(_LANG_PRESETS)),
        default="en",
        show_default=True,
    )


@cli.command()
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option(
    "--no-validate",
    is_flag=True,
    default=False,
    help="Skip the connectivity probe entirely (default: prompt, probe on yes).",
)
@click.option(
    "--mcp",
    "mcp_mode",
    type=click.Choice(["claude", "json", "skip"]),
    default=None,
    help=(
        "Pre-answer the MCP-registration prompt for scripted runs: "
        "'claude' = `claude mcp add`, 'json' = write .mcp.json, 'skip' = "
        "no registration. Omit the flag for the interactive prompt."
    ),
)
@click.option(
    "--prune-originals",
    "prune_originals",
    is_flag=True,
    default=False,
    help=(
        "After a successful import + registration, remove the direct "
        "registrations from source MCP clients so tools route through STM "
        "only. TTY callers get a single y/N prompt (default No); non-TTY "
        "scripted callers must pass the flag explicitly."
    ),
)
@click.option(
    "--lang",
    type=click.Choice(list(_LANG_PRESETS)),
    default=None,
    help=(
        "Primary content language preset for token-aware budgets. "
        "'en' (default) writes no language-specific fields. 'ko' sets "
        "chars_per_token=1.85, default_max_result_chars=8500, "
        "min_response_chars=230, and adds max_result_tokens=2000 + "
        "chars_per_token=1.85 to each imported server. Omit for the "
        "interactive prompt; defaults to 'en' on non-TTY callers."
    ),
)
def init(
    config_path: str,
    no_validate: bool,
    mcp_mode: str | None,
    prune_originals: bool,
    lang: str | None,
) -> None:
    """Guided first-time setup for memtomem-stm.

    Prompts for a single upstream server and writes the config file. Aborts
    when the config already exists — use ``mms add`` to append more servers.
    """
    path = Path(config_path)
    resolved = path.expanduser().resolve()

    if resolved.exists():
        click.echo(f"{_err('Error:')} config already exists at {resolved}.", err=True)
        click.echo("  Use `mms add` to register another server.", err=True)
        click.echo(
            "  Use `mms add --import` to bulk-import more from MCP clients.",
            err=True,
        )
        click.echo("  Use `mms list` to see what's already configured.", err=True)
        sys.exit(1)

    click.echo(_hdr("Guided setup for memtomem-stm"))
    click.echo("=" * 30)
    click.echo(f"Config will be written to: {resolved}")
    click.echo("")

    candidates = _discover_candidates(Path.cwd())
    imported: dict[str, dict[str, Any]] = {}
    # Parallel list of the source-client candidate dicts for entries we
    # actually import. Needed so the end-of-flow prune step can address the
    # exact ``(name, source, duplicate_in)`` triples via ``_handle_source_prune``.
    imported_candidates: list[dict[str, Any]] = []

    if candidates:
        click.echo(_hdr(f"Found {len(candidates)} MCP server(s) in existing client configs:"))
        # TUI renders its own list; this preview exists so duplicate-source
        # notes ("also in: X") stay visible — those don't fit inside a
        # questionary Choice title and otherwise would be invisible.
        for i, cand in enumerate(candidates, 1):
            dup = cand.get("duplicate_in")
            dup_hint = f"  (also in: {', '.join(dup)})" if dup else ""
            click.echo(
                f"  {i:>2}. {cand['name']:<18} {_format_candidate_detail(cand['entry'])}"
                f"    — from {cand['source']}{dup_hint}"
            )
        click.echo("")
        picks = _pick_imports(candidates)

        # Empty selection when candidates exist = intentional "not this
        # time" (cancel, or Confirm with nothing toggled). We don't
        # silently drop into the manual-name prompt, which was confusing
        # when users just wanted to re-think: nothing is saved, the user
        # can rerun ``mms init`` fresh or use ``mms add`` explicitly.
        if not picks:
            click.echo("")
            click.echo(
                f"{_ok('No servers selected.')} Config not written — "
                "rerun `mms init` to re-select, or use `mms add` to configure one by name."
            )
            return

        click.echo("")
        used_prefixes: set[str] = set()
        for idx in picks:
            cand = candidates[idx]
            click.echo(_hdr(f"Configuring '{cand['name']}'"))
            suggested = _suggest_prefix(cand["name"], used_prefixes)
            prefix = _prompt_prefix(default=suggested)
            used_prefixes.add(prefix)
            entry = {"prefix": prefix, **cand["entry"]}
            imported[cand["name"]] = entry
            imported_candidates.append(cand)
    else:
        # Zero discovered candidates → true first-time user without any
        # existing MCP-client config. Full manual prompt flow.
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

        entry = {
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
                    entry["args"] = _split_args(args_str)
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

        imported[name] = entry

    # Resolve the language preset before validate so the prompt sequence
    # stays predictable for scripted callers ("which prompt fires next?").
    # Non-TTY callers without ``--lang`` get "en" silently — see
    # ``_prompt_language``.
    selected_lang = lang if lang is not None else _prompt_language()
    preset = _LANG_PRESETS[selected_lang]
    proxy_fields = {k: v for k, v in preset.items() if not k.startswith("_")}
    per_server_fields = preset.get("_per_server", {})
    if per_server_fields:
        for entry in imported.values():
            for key, value in per_server_fields.items():
                # ``setdefault`` preserves any operator-explicit value the
                # import flow already produced (an upstream MCP config could
                # in principle carry these keys). Manual flow's hardcoded
                # ``max_result_chars=8000`` is left in place; the token
                # budget wins via ``ProxyManager._resolve_tool_config``
                # precedence (PR #274), so the char value is dead code in
                # the resolved budget but kept visible in the saved config.
                entry.setdefault(key, value)

    if not no_validate and click.confirm("Validate connection(s) now?", default=True):
        probe_map = {n: e for n, e in imported.items()}
        click.echo(f"Validating {len(probe_map)} server(s) (timeout=10s)...")
        probes = asyncio.run(_probe_servers(probe_map, 10))
        for n, probe in probes.items():
            if probe["connected"]:
                click.echo(f"  {_ok('Reachable:')} {n} — {probe['tools']} tool(s).")
            else:
                click.echo(f"  {_warn('Warning:')} {n} — probe failed: {probe['error']}", err=True)
                click.echo("  Saving config anyway. Run `mms health` later to retry.", err=True)

    data: dict[str, Any] = {"enabled": True, **proxy_fields, "upstream_servers": imported}
    _save(path, data)

    click.echo("")
    click.echo(f"{_ok('Saved to:')} {resolved}")
    click.echo("")
    click.echo(_hdr("Configured upstream servers:"))
    for n, e in imported.items():
        click.echo(f"  {n:<20} prefix={e['prefix']}  {_format_candidate_detail(e)}")

    # Non-default ``--config`` paths: surface the flag in the management
    # hints so ``mms list`` / ``mms health`` don't silently read the empty
    # default config and confuse the user (reported after first dogfooding
    # with a throwaway ``/tmp/*.json`` test path).
    if resolved != _DEFAULT_CONFIG.expanduser().resolve():
        click.echo("")
        click.echo(f"  {_hdr('Manage this config:')}")
        click.echo(f"    mms list --config {resolved}")
        click.echo(f"    mms health --config {resolved}")
        click.echo(f"    mms add --import --config {resolved}")

    click.echo("")
    _run_mcp_integration(_MCP_MODE_TO_CHOICE.get(mcp_mode) if mcp_mode else None)

    # Post-registration prune of source-client originals, opt-in only.
    # * ``--prune-originals`` → unconditional prune (scripted path).
    # * TTY + no flag → single y/N confirm, default No.
    # * non-TTY + no flag → skip; fall through to the #203 hint-only warning.
    # Matches ``mms add --import --prune`` semantics via ``_handle_source_prune``.
    if imported_candidates:
        _handle_source_prune(imported_candidates, prune=prune_originals)


@cli.command()
@click.option(
    "--config",
    "config_path",
    default=str(_DEFAULT_CONFIG),
    show_default=True,
    help="Path to the proxy config (must already exist — run `mms init` first).",
)
@click.option(
    "--mcp",
    "mcp_mode",
    type=click.Choice(["claude", "json", "skip"]),
    default=None,
    help=(
        "Pre-answer the registration prompt for scripted runs: "
        "'claude' = `claude mcp add`, 'json' = write .mcp.json, 'skip' = "
        "print manual hints. Omit for the interactive prompt."
    ),
)
def register(config_path: str, mcp_mode: str | None) -> None:
    """Register memtomem-stm with an MCP client.

    Post-init re-entry path for the 3-way registration flow from ``mms init``:

    \b
    * Add to Claude Code (shell out to ``claude mcp add``)
    * Generate ``.mcp.json`` in the current directory
    * Skip and print manual instructions

    Requires that ``mms init`` has been run so ``~/.memtomem/stm_proxy.json``
    (or the ``--config`` path) exists. Safe to re-run; pre-checks existing
    Claude Code registration and defaults to 'keep' when already registered.
    """
    resolved = Path(config_path).expanduser().resolve()
    if not resolved.exists():
        click.echo(
            f"{_err('Error:')} config not found at {resolved}.",
            err=True,
        )
        click.echo("  Run `mms init` first.", err=True)
        sys.exit(1)
    _run_mcp_integration(_MCP_MODE_TO_CHOICE.get(mcp_mode) if mcp_mode else None)


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


# ── prune command ──────────────────────────────────────────────────────
#
# Post-hoc version of the prune step that ``mms add --import --prune`` does
# inline. Needed because ``mms init`` intentionally leaves source-client
# registrations untouched (bootstrap-only invariant + read-only discovery
# contract — see PR #200 and #203), which means anyone who onboarded via
# ``mms init`` ends up with dual-registered servers and no in-tree way to
# collapse the dual path without manually running ``claude mcp remove`` per
# entry. ``mms prune`` is the explicit opt-in command that closes that gap.


@cli.command()
@click.argument("names", nargs=-1)
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option(
    "--all",
    "all_servers",
    is_flag=True,
    help="Prune every dual-registered upstream. Required when no NAMES given.",
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Skip the confirm prompt (scripts / CI / non-TTY callers).",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    help="Print what would be pruned; no writes.",
)
def prune(
    names: tuple[str, ...],
    config_path: str,
    all_servers: bool,
    assume_yes: bool,
    dry_run: bool,
) -> None:
    """Remove direct registrations for STM upstreams that are dual-registered.

    Finds every STM upstream that's still directly registered in a source
    MCP client (``~/.claude.json``, project ``.mcp.json``, Claude Desktop)
    and removes the direct registration so tool calls route through STM —
    picking up compression, caching, and LTM surfacing, and collapsing the
    duplicate tool advertisement. STM's own config is not touched.
    """
    if names and all_servers:
        raise click.UsageError("--all cannot be combined with explicit NAMES.")
    if not names and not all_servers:
        raise click.UsageError(
            "Pass upstream NAMES or --all. Use `mms list` to see what's configured."
        )

    path = Path(config_path)
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        click.echo(f"{_err('Error:')} config not found at {resolved}.", err=True)
        click.echo("  Run `mms init` first.", err=True)
        sys.exit(1)

    data = _load(resolved)
    upstreams: dict[str, dict[str, Any]] = data.get("upstream_servers", {})
    if not upstreams:
        click.echo("No upstream servers configured.")
        return

    dual = _find_dual_registered(upstreams, Path.cwd())

    if names:
        # ``not dual-registered`` subsumes three distinct states the user
        # doesn't need to disambiguate up front: name absent from STM config,
        # name present in STM but not in any source client, or name in both
        # but with a divergent identity (``_find_dual_registered`` skipped it
        # precisely because the source-client entry looks like a different
        # server). The error surfaces all three branches in one message.
        dual_names = {c["name"] for c in dual}
        missing = set(names) - dual_names
        if missing:
            click.echo(
                f"{_err('Error:')} not dual-registered: {', '.join(sorted(missing))}.",
                err=True,
            )
            click.echo(
                "  These names are either not STM upstreams, not in any source "
                "client, or registered with a different command/URL in the source.",
                err=True,
            )
            sys.exit(1)
        requested = set(names)
        dual = [c for c in dual if c["name"] in requested]

    if not dual:
        click.echo("No dual-registered upstreams found.")
        return

    # Preview iteration must cover the same ``source + duplicate_in`` set
    # that ``_prune_imported_candidates`` acts on — otherwise a user could
    # approve fewer entries than get written. See
    # ``test_duplicate_in_sources_all_pruned`` for the contract pin.
    click.echo(_hdr(f"Dual-registered upstream(s): {len(dual)}"))
    name_width = max((len(c["name"]) for c in dual), default=0)
    seen: set[tuple[str, str]] = set()
    for cand in dual:
        detail = _format_candidate_detail(cand["entry"])
        for src in [cand["source"], *(cand.get("duplicate_in") or [])]:
            key = (cand["name"], src)
            if key in seen:
                continue
            seen.add(key)
            click.echo(f"  {cand['name']:<{name_width}}  {detail}  — {src}")

    if dry_run:
        click.echo("")
        click.echo(f"{_ok('Dry run:')} no writes performed.")
        return

    if assume_yes:
        proceed = True
    elif _should_use_tui():
        click.echo("")
        proceed = click.confirm("Remove from source(s)?", default=False)
    else:
        click.echo(
            f"{_err('Error:')} no TTY; pass --yes to confirm (or --dry-run to preview).",
            err=True,
        )
        sys.exit(1)

    if not proceed:
        click.echo("Aborted. No changes made.")
        return

    pruned, failed = _prune_imported_candidates(dual)

    if pruned:
        click.echo("")
        click.echo(f"{_ok('Removed from source client(s):')}")
        for name, src in pruned:
            click.echo(f"  {name} — {src}")

    if failed:
        click.echo("")
        click.echo(
            f"{_warn('Warning:')} could not remove {len(failed)} direct registration(s):",
            err=True,
        )
        for name, src, err in failed:
            click.echo(f"  {name} ({src}): {err}", err=True)
        click.echo("", err=True)
        click.echo("Run the following to remove them manually:", err=True)
        for name, src, _ in failed:
            click.echo(f"  {_source_removal_hint(name, src)}", err=True)
        sys.exit(1)


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
            prefix = cfg.get("prefix", "")
            overflowing = [
                t.name for t in result.tools if tool_name_budget.overflows(prefix, t.name)
            ]
            return {
                "connected": True,
                "tools": len(result.tools),
                "overflowing": overflowing,
                "error": None,
            }


def _root_cause_message(exc: BaseException) -> str:
    """Walk into ``BaseExceptionGroup`` (anyio TaskGroup wraps probe failures
    as ``unhandled errors in a TaskGroup (N sub-exception)``) to surface the
    first non-group leaf so users see the real cause instead of the wrapper.
    """
    seen: set[int] = set()
    cur: BaseException = exc
    while isinstance(cur, BaseExceptionGroup) and cur.exceptions and id(cur) not in seen:
        seen.add(id(cur))
        cur = cur.exceptions[0]
    return str(cur) or type(cur).__name__


def _surfacing_bootstrap_status() -> dict[str, Any]:
    """Return surfacing feedback DB readiness without starting the proxy."""
    try:
        from memtomem_stm.config import STMConfig
        from memtomem_stm.surfacing.feedback_store import inspect_feedback_db

        surfacing = STMConfig().surfacing
        db_status = inspect_feedback_db(surfacing.feedback_db_path)
        return {
            "enabled": surfacing.enabled,
            "feedback_enabled": surfacing.feedback_enabled,
            "feedback_db": db_status,
        }
    except Exception as exc:
        logger.debug("Surfacing bootstrap status inspection failed", exc_info=True)
        return {
            "enabled": None,
            "feedback_enabled": None,
            "feedback_db": None,
            "error": str(exc) or type(exc).__name__,
        }


def _format_surfacing_bootstrap(status: dict[str, Any]) -> list[str]:
    lines = [_hdr("Surfacing Bootstrap"), "=" * 30]
    if status.get("error"):
        lines.append(f"  {_bad('ERROR')} — {status['error']}")
        return lines

    lines.append(f"  config: {'enabled' if status['enabled'] else 'disabled'}")
    lines.append(f"  feedback tracking: {'enabled' if status['feedback_enabled'] else 'disabled'}")

    db = status.get("feedback_db")
    if not isinstance(db, dict):
        lines.append("  feedback db: unavailable")
        return lines

    lines.append(f"  feedback db: {db['path']}")
    if db.get("error"):
        lines.append(f"  feedback tables: {_bad('error')} — {db['error']}")
    elif not db.get("exists"):
        lines.append("  feedback tables: missing (DB has not been created)")
    elif db.get("initialized"):
        lines.append(f"  feedback tables: {_ok('ready')}")
    else:
        missing = ", ".join(str(t) for t in db.get("missing_tables", []))
        lines.append(
            f"  feedback tables: {_warn('missing')} ({missing}) — "
            "surfacing has not initialized this DB"
        )
    return lines


@contextmanager
def _silenced_mcp_sdk_logs() -> Iterator[None]:
    """Temporarily raise the MCP SDK loggers above ``ERROR`` so probe-time
    failures (which we already capture + report per-server) don't dump
    multi-line ``logger.exception`` tracebacks to stderr — those corrupt
    ``--json`` output for callers piping into ``jq`` / similar.
    """
    sdk_logger = logging.getLogger("mcp")
    prior = sdk_logger.level
    sdk_logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        sdk_logger.setLevel(prior)


async def _probe_servers(servers: dict[str, Any], timeout: float) -> dict[str, dict[str, Any]]:
    """Probe all servers in parallel, returning per-server results."""

    async def _safe_probe(name: str, cfg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            result = await _probe_one(cfg, timeout)
        except asyncio.TimeoutError:
            result = {
                "connected": False,
                "tools": 0,
                "overflowing": [],
                "error": f"timeout ({timeout}s)",
            }
        except Exception as exc:
            # anyio raises ``ExceptionGroup`` when probe failures bubble out
            # of the TaskGroup; that's already an ``Exception`` subclass,
            # so the existing catch surface is fine. ``_root_cause_message``
            # walks the wrapper to surface the real cause.
            err = _root_cause_message(exc)
            result = {"connected": False, "tools": 0, "overflowing": [], "error": err}
        return name, result

    with _silenced_mcp_sdk_logs():
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
@click.option(
    "--names",
    "show_names",
    is_flag=True,
    help=(
        "Also report any upstream tool whose composed proxied name "
        "(`mcp__<server>__<prefix>__<tool>`) would exceed the 64-char MCP "
        "limit. Useful when one tool from an upstream silently went missing "
        "after registration (#261)."
    ),
)
def health(
    config_path: str,
    *,
    as_json: bool = False,
    timeout: int = 10,
    show_names: bool = False,
) -> None:
    """Check upstream server connectivity."""
    path = Path(config_path)
    resolved = path.expanduser().resolve()

    # Missing-config handling matches ``status`` / ``list`` so a user
    # troubleshooting connectivity can tell wrong-path from empty-config
    # without re-running ``status`` (#221, extended to ``health``).
    if not resolved.exists():
        if as_json:
            click.echo(json.dumps({"error": "config_not_found", "path": str(resolved)}))
        else:
            click.echo(f"Config not found: {resolved}")
            click.echo("Run `mms add` (or `mms init`) to create a configuration.")
        return

    data = _load(path)
    servers: dict[str, Any] = data.get("upstream_servers", {})
    surfacing_status = _surfacing_bootstrap_status()

    # JSON output format matches ``status --json`` / ``list --json`` (indent=2,
    # ensure_ascii=False) so scripts piping the three commands through the
    # same formatter don't hit one compact-one-line outlier.
    if not servers:
        if as_json:
            click.echo(
                json.dumps(
                    {"servers": {}, "surfacing": surfacing_status},
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            click.echo("No upstream servers configured.")
            click.echo("")
            for line in _format_surfacing_bootstrap(surfacing_status):
                click.echo(line)
        return

    results = asyncio.run(_probe_servers(servers, timeout))

    if as_json:
        click.echo(
            json.dumps(
                {"servers": results, "surfacing": surfacing_status},
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    click.echo(_hdr("Upstream Server Health"))
    click.echo("=" * 30)
    for name, info in results.items():
        if info["connected"]:
            click.echo(f"  {name}: {_ok('connected')} ({info['tools']} tools)")
            if show_names:
                overflowing = info.get("overflowing", [])
                if overflowing:
                    click.echo(
                        f"    {_warn('overflow:')} {len(overflowing)} tool(s) "
                        f"would exceed the {tool_name_budget.TOOL_NAME_LIMIT}-char "
                        f"client limit and will be silently dropped:"
                    )
                    for t_name in overflowing:
                        click.echo(f"      - {t_name}")
                else:
                    click.echo("    all tool names fit")
        else:
            click.echo(f"  {name}: {_bad('DISCONNECTED')} — {info['error']}")
    click.echo("")
    for line in _format_surfacing_bootstrap(surfacing_status):
        click.echo(line)


# `mms project ...` — RFC §7.1, lives in src/memtomem_stm/cli/mms_project.py
# to keep this file from accreting another ~700 lines for the W1 surface.
cli.add_command(_mms_project_group)

# `mms import ...` — RFC §7.2, lives in src/memtomem_stm/cli/mms_import.py.
cli.add_command(_mms_import_command)

# `mms host ...` — RFC §7.3, lives in src/memtomem_stm/cli/mms_host.py.
cli.add_command(_mms_host_group)
