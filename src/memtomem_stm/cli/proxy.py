"""CLI commands for managing the memtomem-stm proxy gateway."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from contextlib import AsyncExitStack, contextmanager, redirect_stderr, redirect_stdout
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from functools import wraps
from typing import TYPE_CHECKING, Any, NoReturn, TextIO
from urllib.parse import unquote, urlsplit

import click

if TYPE_CHECKING:
    from memtomem_stm.proxy.tuner import TuningRecommendation

from memtomem_stm.cli._defaults import DEFAULT_PROXY_CONFIG

# Re-exported deliberately: ``_disp`` and ``_disp_escapes`` were defined here
# until #760 moved them to a leaf every CLI module can import. Importing them
# under their own names keeps this module's ~100 call sites, ``_shell_join``,
# and every existing ``from memtomem_stm.cli.proxy import _disp`` working
# unchanged — which is what makes that move a pure refactor.
from memtomem_stm.cli._display import _disp, _disp_escapes
from memtomem_stm.utils.json_out import dumps as _json_dumps
from memtomem_stm.utils.json_out import has_lone_surrogate, unencodable_field
from memtomem_stm.cli._write_lock import with_config_write_lock
from memtomem_stm.cli.config_cmd import config_group as _config_group
from memtomem_stm.cli.daemon_cmd import daemon_group as _daemon_group
from memtomem_stm.cli.hook_cmd import hook_command as _hook_command
from memtomem_stm.cli.host_runtime import resolve_host_runtime_policy
from memtomem_stm.cli.mms_host import host_group as _mms_host_group
from memtomem_stm.cli.mms_import import import_command as _mms_import_command
from memtomem_stm.cli.mms_project import project_group as _mms_project_group
from memtomem_stm.cli.selection_cmd import selection_group as _selection_group
from memtomem_stm.mms.import_hosts import (
    _DANGEROUS_ENV_KEYS,
    _desktop_config_path,
    _is_self_reference,
    _read_json_safely,
)
from memtomem_stm.mms.secrets import REDACTED_DISPLAY
from memtomem_stm.mms.state import utc_now_iso
from memtomem_stm.proxy import prefixes, tool_name_budget
from memtomem_stm.proxy.staged_status import ProbeStage, StagedProbeResult
from memtomem_stm.utils.fileio import atomic_write_text
from memtomem_stm.utils.redact import (
    redact_exception_text,
    redact_url_userinfo,
    sanitize_secrets,
)

# Backward-compatible private alias for this module's decorators. The
# canonical value lives in ``cli._defaults`` so sibling command modules do
# not need to import this large, mutually dependent module.
_DEFAULT_CONFIG = DEFAULT_PROXY_CONFIG
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


# cmd.exe metacharacters that a double-quoted span renders literal. ``%`` is
# included not to stop ``%VAR%`` expansion (quoting can't — it runs first) but
# because a quoted span keeps any metacharacter *introduced by the expanded
# value* (e.g. a var whose value contains ``&``) from splitting the command.
# ``!`` is excluded: it is inert unless the user opted into ``cmd /v:on``, and
# there ``!VAR!`` expands even inside double quotes, so quoting cannot help it.
# See ``_cmd_quote``'s docstring for the residuals.
_CMD_NEEDS_QUOTE = set(' \t"&|<>^()%')


def _cmd_quote(token: str) -> str:
    """Quote one token for paste at an interactive ``cmd.exe`` prompt / ``cmd /c``.

    ``subprocess.list2cmdline`` implements MS C-runtime *argv* quoting only —
    it wraps tokens containing whitespace/quotes/empty but leaves cmd.exe
    *shell* metacharacters (``& | < > ^ ( )``) unprotected in tokens without
    whitespace, so ``C:\\a&b\\cfg.json`` pasted into cmd.exe splits the command
    at ``&`` (#749). This wraps any token carrying a metacharacter in double
    quotes — inside a quoted span cmd treats those chars as literal — while
    keeping metachar-free tokens (incl. plain backslash paths) untouched so the
    hint templates' ``<name>`` metavars stay readable.

    Embedded ``"`` is escaped as ``""`` rather than ``list2cmdline``'s ``\\"``:
    cmd ignores the backslash, so ``\\"`` toggles cmd's quote state and exposes
    the following metacharacters; ``""`` keeps quote parity while the modern
    ucrt argv parser reads the pair as one literal quote. Backslash runs are
    doubled before an emitted quote per the MS argv rules.

    A token containing ``%`` is quoted too: quoting cannot stop a *defined*
    ``%VAR%`` from expanding (that runs before quote processing), but it keeps
    metacharacters in the *expanded value* from splitting the command — an
    unquoted ``%VAR%`` whose value is ``a&whoami`` would otherwise run
    ``whoami`` as a second command.

    Residual limitations (batch-file paste is out of scope): a *defined*
    ``%VAR%`` still expands (only its value's metacharacters are neutralized),
    and ``!VAR!`` expands under ``cmd /v:on`` delayed expansion even inside
    double quotes — that one is not defeatable by quoting.
    """
    if token and not (set(token) & _CMD_NEEDS_QUOTE):
        return token
    out = ['"']
    backslashes = 0
    for ch in token:
        if ch == "\\":
            backslashes += 1
        elif ch == '"':
            out.append("\\" * (backslashes * 2))
            backslashes = 0
            out.append('""')
        else:
            out.append("\\" * backslashes)
            backslashes = 0
            out.append(ch)
    out.append("\\" * (backslashes * 2))
    out.append('"')
    return "".join(out)


# Two overlapping reasons a value cannot be rendered into a paste hint, both
# resolved by refusing the whole command rather than quoting or escaping it.
#
# CR/LF/NUL break the *paste*, and the failure mode differs by shell — which is
# why this is a different class from the ``_cmd_quote`` metacharacters (#749):
#   - Windows cmd.exe / the interactive console consume a pasted newline as
#     Enter *even inside an open quoted span*, submitting the truncated prefix
#     as its own command — quoting genuinely cannot defeat this (#751).
#   - POSIX shells DO contain a newline inside ``shlex``'s single quotes (the
#     paste is a valid multi-line command, not an early submit), but that
#     shatters the one-line ``next:``/``run:`` rendering; NUL is unrepresentable
#     everywhere.
# Rejecting uniformly (rather than only on win32) keeps the guard testable on
# both CI legs. A JSON config (plain ``json.loads``, no character validation on
# server names/``command``/env values) can carry any of these into a token.
# No longer tested directly — the wider display class below subsumes it — but
# kept because it records *which* characters would still have to be refused if
# the display reason ever went away, and a test pins the subset relation so it
# cannot drift into decoration.
_HINT_UNSAFE_CHARS = frozenset("\r\n\x00")

# The second reason is display, and it covers a strictly wider set: a rendered
# command is terminal output before it is anything else, so an ESC, a C1 byte,
# a line separator or a BiDi override in a token repaints or reorders the very
# line the user is being asked to read — the #754 hazard, arriving by the other
# half of the same hint. Prose escapes those in place; a command cannot, since
# an escaped token would paste as a *different* server name, so the whole
# command is refused instead. ``_disp_escapes`` is the single definition of the
# class, and it subsumes ``_HINT_UNSAFE_CHARS`` (CR/LF/NUL are all C0); the two
# are kept separate because they justify the refusal for different reasons and
# only the paste-execution one is platform-specific.

# Non-executable fallback (never embeds the raw value). Pasted, the ``#`` line
# is inert on every target shell but by two routes: bash/fish/POSIX sh treat
# ``#`` as a comment; interactive zsh (``interactive_comments`` off by default)
# and cmd.exe instead look ``#`` up as a command, which *normally* fails —
# ``command not found``/unknown command — and short-circuits any ``&&`` tail.
# Residual (documented, not fixed): ``#`` can still resolve via a shell
# alias/function/command hash, or a planted ``#.{cmd,bat,exe}`` on PATH/cwd —
# the same residual the pre-existing ``# Edit ...`` / ``# Remove ...`` fallback
# hints already carry, and below #751's own low severity (the user pastes their
# own hint). No single prefix is a comment on both shell families.
#
# At the few call sites that embed the result as a *fragment* (``--config
# {join}``; ``cd X && {join}``) the tainted value is still fully suppressed — the
# injected newline-tail never survives — but the surrounding app-owned prefix
# (an errored ``--config``, a benign ``cd``) remains. That prefix is never
# attacker-controlled, so no injected command runs (see the composition test).
#
# The wording covers both reasons and is deliberately broader than #751's
# original ``line-break or NUL``, since the same diagnostic now stands in for an
# ESC-, separator- or BiDi-bearing token too (#754). It avoids naming a Unicode
# category: the class spans several (Cc, Cs, Zl, Zp, Cf), so ``control
# characters`` would be false for the separators and the lone surrogates.
_HINT_UNRENDERABLE = (
    "# copy/paste hint unavailable: a value contains characters that cannot be displayed safely"
)


def _shell_join(args: list[str]) -> str:
    """Render a copy/paste command for the current native shell family.

    The win32 leg is cmd.exe-metacharacter-safe (via ``_cmd_quote``), not just
    argv-safe; the POSIX leg (``shlex.join``) already quotes metacharacters.

    An argv carrying a character :func:`_disp_escapes` rejects renders as the
    non-executable ``_HINT_UNRENDERABLE`` diagnostic on *both* legs, for either
    of two reasons. CR/LF/NUL break the paste itself (#751): on Windows a
    pasted newline submits the truncated prefix even inside quotes, on POSIX
    ``shlex`` would contain it but only as a multi-line paste that breaks the
    one-line hint, and NUL is unrepresentable everywhere. Every other member of
    the set — ESC and the rest, whatever :func:`_disp_escapes` currently
    defines — breaks the *display* of the line the user is asked to read
    (#754); quoting preserves such a character faithfully, which is exactly the
    problem. See ``_HINT_UNSAFE_CHARS`` for the per-shell paste rationale and
    :func:`_disp` for why prose escapes where this refuses.
    """
    if any(any(_disp_escapes(ch) for ch in token) for token in args):
        return _HINT_UNRENDERABLE
    if sys.platform == "win32":
        return " ".join(_cmd_quote(a) for a in args)
    return shlex.join(args)


_SETUP_RESOLVED_CLIENT: ContextVar[str | None] = ContextVar("setup_resolved_client", default=None)

# Whether the current setup command runs in ``--json`` mode. The
# ``_setup_json_result`` decorator pops ``as_json`` before calling the command
# body, so the body reads this instead of a parameter. JSON mode must never
# block on stdin: with stdout redirected into the capture buffer a prompt is
# invisible and hangs the caller (or dies as a bare "Aborted!" on EOF).
_SETUP_JSON_MODE: ContextVar[bool] = ContextVar("setup_json_mode", default=False)


def _setup_json_result(action: str):  # noqa: ANN201
    """Capture a setup command and emit one secret-safe JSON result document."""

    def decorate(fn):  # noqa: ANN001, ANN202
        @wraps(fn)
        def wrapped(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            as_json = bool(kwargs.pop("as_json", False))
            _SETUP_JSON_MODE.set(as_json)
            if not as_json:
                return fn(*args, **kwargs)

            _SETUP_RESOLVED_CLIENT.set(None)
            out = io.StringIO()
            err = io.StringIO()
            exit_code = 0
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    fn(*args, **kwargs)
                except SystemExit as exc:
                    exit_code = int(exc.code) if isinstance(exc.code, int) else 1
                except click.exceptions.Abort:
                    # EOF at an interactive prompt. Without this the Abort
                    # escapes to Click's top level, which prints a bare
                    # "Aborted!" and exits 1 with NO JSON document — exactly
                    # the output mode automation parses.
                    exit_code = 1
                    click.echo(
                        "stdin ended before an interactive prompt was answered; "
                        "pipe answers or pre-answer with flags "
                        "(--demo/--client/--no-validate)",
                        err=True,
                    )
                except click.ClickException as exc:
                    exit_code = exc.exit_code
                    click.echo(exc.format_message(), err=True)

            config_path = (
                Path(kwargs.get("config_path", str(_DEFAULT_CONFIG))).expanduser().resolve()
            )
            server_names: list[str] = []
            config_data: dict[str, Any] | None = None
            if config_path.exists():
                try:
                    data = json.loads(config_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        config_data = data
                    servers = data.get("upstream_servers", {}) if isinstance(data, dict) else {}
                    if isinstance(servers, dict):
                        server_names = sorted(str(name) for name in servers)
                except (OSError, json.JSONDecodeError, ValueError):
                    pass
            payload = {
                "action": action,
                "ok": exit_code == 0,
                "config_path": str(config_path),
                "servers": server_names,
                "client": _SETUP_RESOLVED_CLIENT.get()
                or kwargs.get("client_mode")
                or kwargs.get("mcp_mode"),
            }
            if exit_code:
                payload["error"] = "setup_failed"
                lines = [line.strip() for line in err.getvalue().splitlines() if line.strip()]
                message = lines[-1] if lines else "setup command failed"
                if config_data is not None:
                    message = sanitize_secrets(message, _all_config_secret_values(config_data))
                payload["message"] = message[:1000]
            elif captured_err := err.getvalue():
                # A *successful* run used to drop its diagnostics entirely —
                # only the failure branch read the capture, and then only its
                # last line. That is how discovery's "skipping <name>" advisory
                # went missing under ``--json`` (#758), on exactly the output
                # mode automation reads. Replay it on the real stderr,
                # sanitized like ``message`` since the capture holds whatever
                # the command wrote; stdout stays one pure JSON document.
                if config_data is not None:
                    captured_err = sanitize_secrets(
                        captured_err, _all_config_secret_values(config_data)
                    )
                click.echo(captured_err, err=True, nl=False)
            click.echo(_json_dumps(payload, indent=2, ensure_ascii=False))
            if exit_code:
                raise SystemExit(exit_code)

        return wrapped

    return decorate


def _new_config_cache_block() -> dict[str, Any]:
    """Cache block written into every NEW config file (init/add/import).

    New configs opt into the "strict" annotation policy — cache only tools
    that explicitly declare ``readOnlyHint=True`` — while the schema default
    stays "conservative" so existing key-less files keep their behavior
    (they get a migration advisory at load time instead). A fresh dict per
    call: ``add`` mutates ``_load``'s returned dict before saving, so a
    shared module-level constant could alias across loads.
    """
    return {"tool_annotation_policy": "strict"}


def _load(config_path: Path) -> dict[str, Any]:
    resolved = config_path.expanduser().resolve()
    if not resolved.exists():
        return {"enabled": True, "cache": _new_config_cache_block(), "upstream_servers": {}}
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


def _schema_validation_error(data: dict[str, Any]) -> str | None:
    """First schema-validation error for a raw config dict, or ``None``.

    ``_load`` only guards JSON syntax and coarse shape; valid-JSON-but-
    invalid-schema is exactly the case a running server silently degrades to
    env/defaults on (#611). ``status`` / ``health`` warn on it — exit code
    unchanged, since inspection commands stay lenient and strictness lives in
    ``mms config validate``.
    """
    from pydantic import ValidationError

    from memtomem_stm.proxy.config import ProxyConfig

    try:
        ProxyConfig.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first["loc"])
        return f"{loc}: {first['msg']}" if loc else first["msg"]
    return None


_CONFIG_INVALID_WARNING = (
    "config file present but fails validation — a running server falls back to env/defaults"
)


def _transport_field_error(transport: str, command: str, url: str) -> str | None:
    """Missing required connection field for *transport*, or ``None``.

    Single source of truth for which field each transport needs — shared by
    ``add`` (VAL-3/VAL-4, rendered with a ``--`` option flavor) and ``mms
    doctor`` (raw config dicts), so the two can't drift.
    """
    if transport == "stdio" and not command:
        return "command is required for stdio transport"
    if transport != "stdio" and not url:
        return f"url is required for {transport} transport"
    return None


def _logging_destination_status() -> dict[str, Any]:
    """Active log destination for ``mms health`` (#612) — the point of the
    opt-in file log is diagnosability, so health says where to look.
    ValidationError-guarded: a bad ``MEMTOMEM_STM_*`` env value must not
    break a diagnostics command."""
    from pydantic import ValidationError

    from memtomem_stm.config import STMConfig
    from memtomem_stm.logging_setup import describe_log_destination

    try:
        return describe_log_destination(STMConfig())
    except ValidationError as exc:
        return {"error": f"invalid MEMTOMEM_STM_* environment ({exc.error_count()} error(s))"}


def _format_logging_destination(status: dict[str, Any]) -> str:
    """One ``Logging:`` line — always printed, so users learn the file-log
    option exists even while running stderr-only. When a configured log file
    isn't writable, name stderr as the real destination (the server degrades
    to it) instead of pointing at a file that receives nothing."""
    if "error" in status:
        return f"{_warn('Logging:')} {status['error']}"
    if status["log_file"]:
        if status.get("writable"):
            return (
                f"Logging: stderr + file {status['log_file']} "
                f"(level {status['log_level']}, rotating)"
            )
        return (
            f"{_warn('Logging:')} stderr only — configured log file {status['log_file']} "
            "is not writable; the server falls back to stderr"
        )
    return "Logging: stderr only (set MEMTOMEM_STM_LOG_FILE for a persistent log)"


def _save(config_path: Path, data: dict[str, Any]) -> None:
    """Write the proxy config atomically.

    Delegates to :func:`atomic_write_text` so the temp + ``os.replace``
    pattern stays in one place (see PR #115 for the original failure
    mode: a running proxy's mtime-based hot-reload would otherwise read
    a half-written JSON file in the gap between truncate and write).
    ``mode=0o600`` keeps the rendered file out of a permissive listing
    even if the parent directory is shared.
    """
    payload = _json_dumps(data, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(config_path, payload, mode=0o600, durable=True)


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


def _run_claude_mcp(
    cmd: list[str], timeout: int = 5, cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Invoke the ``claude`` CLI — isolated so tests replace a single seam.

    ``cwd`` exists for ``claude mcp add-json -s local`` (eject, #475 PR3):
    the local scope resolves its ``~/.claude.json`` ``projects.<path>`` slot
    from the process cwd, so the restore must run from the recorded origin
    path or it lands in the wrong project.
    """
    # ``encoding="utf-8"`` is explicit so non-ASCII bytes from the child don't
    # hit the platform default codec (cp1252/cp949 on Windows). See #302 P0.
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=cwd,
    )


def _run_codex_mcp(cmd: list[str], timeout: int = 5) -> subprocess.CompletedProcess[str]:
    """Invoke the official ``codex mcp`` CLI with Windows-safe decoding."""
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


def _registration_command(config_path: Path) -> tuple[str, list[str], dict[str, str]]:
    """Stable cross-platform command and environment for new registrations."""
    # Keep a virtualenv's python symlink intact. ``Path.resolve()`` follows it
    # to the base interpreter, which can no longer import the installed STM.
    command = os.path.abspath(sys.executable)
    env = {"MEMTOMEM_STM_PROXY__CONFIG_PATH": str(config_path.expanduser().resolve())}
    # A newly managed MCP host uses the shared warm daemon.  Serialize the
    # deadline and privacy policy as well: GUI-launched clients often do not
    # inherit the shell environment in which `mms register` ran.
    policy = resolve_host_runtime_policy(use_daemon=True)
    env.update(policy.mcp_env())
    return command, ["-m", "memtomem_stm"], env


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
    server_cmd: str,
    server_args: list[str],
    *,
    env: dict[str, str] | None = None,
    name: str = "memtomem-stm",
) -> tuple[bool, str]:
    """Run ``claude mcp add``; return ``(success, failure_reason)``.

    ``failure_reason`` is one of ``"not_installed"``, ``"timeout"``,
    ``"failed"`` — callers map each to a user-facing message and always
    fall back to ``.mcp.json``.
    """
    cmd = ["claude", "mcp", "add", name, "-s", "user"]
    for key, value in (env or {}).items():
        cmd.extend(["-e", f"{key}={value}"])
    cmd.extend(["--", server_cmd, *server_args])
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


def _codex_registered(name: str = "memtomem-stm") -> bool:
    try:
        return _run_codex_mcp(["codex", "mcp", "get", name]).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _register_with_codex(
    server_cmd: str,
    server_args: list[str],
    *,
    env: dict[str, str] | None = None,
    name: str = "memtomem-stm",
) -> tuple[bool, str]:
    cmd = ["codex", "mcp", "add", name]
    for key, value in (env or {}).items():
        cmd.extend(["--env", f"{key}={value}"])
    cmd.extend(["--", server_cmd, *server_args])
    try:
        result = _run_codex_mcp(cmd)
    except FileNotFoundError:
        return False, "codex CLI not found"
    except subprocess.TimeoutExpired:
        return False, "codex mcp add timed out"
    except OSError as exc:
        return False, f"could not run codex: {exc}"
    if result.returncode == 0:
        return True, ""
    return False, (result.stderr or result.stdout or "codex mcp add failed").strip()


def _remove_from_codex(name: str = "memtomem-stm") -> bool:
    try:
        return _run_codex_mcp(["codex", "mcp", "remove", name]).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


# Fields ``codex mcp get --json`` reports that ``codex mcp add`` has no flag
# for, mapped to the user-facing name we print when refusing to replace. A
# registration carrying any of these cannot be rebuilt from the captured
# snapshot, so replacing it is a one-way destruction (verified against
# ``codex mcp add --help`` / ``codex mcp get --json``, codex-cli 0.144.3).
# ``add`` covers only: stdio ``-- command args`` + ``--env KEY=VALUE``, or
# ``--url`` + ``--bearer-token-env-var``.
_CODEX_UNREPRODUCIBLE_TOP: dict[str, str] = {
    "enabled_tools": "enabled_tools",
    "disabled_tools": "disabled_tools",
    "startup_timeout_sec": "startup_timeout_sec",
    "tool_timeout_sec": "tool_timeout_sec",
}
_CODEX_UNREPRODUCIBLE_TRANSPORT: dict[str, str] = {
    "cwd": "transport.cwd",
    "env_vars": "transport.env_vars",
}
# Every key of the 0.144.3 schema we have actually reasoned about. A field
# outside these sets is one a NEWER codex added: we cannot know whether it
# carries restorable state, and ignoring it is exactly the lossy-restore
# failure this preflight exists to prevent. So an unrecognised field blocks the
# replacement REGARDLESS of its value — an explicitly empty value need not mean
# the same thing as an omitted one (``[]`` = "deny everything" vs. absent =
# "inherit the default" is an ordinary way for a config schema to grow), and the
# rebuilt command omits the key either way. The cost is that a codex release
# adding any field aborts ``--replace-registration`` until this allowlist is
# updated; that abort is loud, leaves the registration untouched, and tells the
# user how to proceed — which is the trade this whole preflight exists to make.
# ``auth_status`` is reported by ``codex mcp list --json`` but not ``get``; it is
# derived state, not configuration.
_CODEX_KNOWN_TOP = frozenset(
    {
        "name",
        "enabled",
        "disabled_reason",
        "transport",
        "auth_status",
        *_CODEX_UNREPRODUCIBLE_TOP,
    }
)
_CODEX_KNOWN_STDIO_TRANSPORT = frozenset({"type", "command", "args", "env", "env_vars", "cwd"})


@dataclass(frozen=True)
class _CodexRestorePlan:
    """How (or whether) a captured Codex registration can be rebuilt.

    ``command`` is ``None`` when the snapshot carries settings ``codex mcp
    add`` cannot express — ``blockers`` then names those fields. The command
    embeds ``--env KEY=VALUE`` pairs, so it is **never** echoed or logged:
    only :attr:`blockers` (field names, no values) is user-facing.
    """

    command: list[str] | None
    blockers: tuple[str, ...]


def _capture_codex_registration(name: str = "memtomem-stm") -> dict[str, Any] | None:
    """Return the parsed ``codex mcp get --json`` snapshot, or None.

    None means the snapshot is unusable for rollback — the CLI is missing,
    errored, timed out, or emitted something other than a JSON object. It
    deliberately does **not** distinguish "not registered", because every
    such case has the same consequence for the caller: there is nothing it
    can promise to restore.
    """
    try:
        result = _run_codex_mcp(["codex", "mcp", "get", name, "--json"])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _unknown_fields(obj: dict[str, Any], known: frozenset[str], prefix: str) -> list[str]:
    """Name every key of *obj* we have not reasoned about.

    A key outside the allowlist may be configuration a newer codex persists, and
    the rebuilt ``codex mcp add`` would drop it. The value cannot settle that —
    an explicitly empty one may still differ from an omitted one — so every
    unknown key blocks, whatever it holds.
    """
    return [f"{prefix}{key} (unrecognised)" for key in obj if key not in known]


def _codex_restore_plan(payload: dict[str, Any], name: str = "memtomem-stm") -> _CodexRestorePlan:
    """Plan an exact ``codex mcp add`` rebuild of a captured registration.

    Conservative by construction: anything not positively recognised as
    reproducible becomes a blocker. A lossy "restore" would silently hand the
    user back a *different* registration than the one we removed, which is
    worse than refusing to touch it.
    """
    blockers: list[str] = []
    for key, label in _CODEX_UNREPRODUCIBLE_TOP.items():
        if payload.get(key) not in (None, [], {}):
            blockers.append(label)
    blockers.extend(_unknown_fields(payload, _CODEX_KNOWN_TOP, ""))
    # ``add`` always writes an enabled entry; a disabled one cannot come back.
    if payload.get("enabled") is not True:
        blockers.append("enabled=false")

    transport = payload.get("transport")
    if not isinstance(transport, dict):
        blockers.append("transport")
        return _CodexRestorePlan(None, tuple(sorted(set(blockers))))
    for key, label in _CODEX_UNREPRODUCIBLE_TRANSPORT.items():
        if transport.get(key) not in (None, [], {}):
            blockers.append(label)

    command: list[str] | None = None
    kind = transport.get("type")
    if kind == "stdio":
        blockers.extend(_unknown_fields(transport, _CODEX_KNOWN_STDIO_TRANSPORT, "transport."))
        cmd = transport.get("command")
        # Default ONLY a literal null. `or []` would swallow a wrong-typed
        # falsey value (``"args": 0``) into a valid-looking empty list and
        # restore a registration that never existed.
        args = [] if transport.get("args") is None else transport.get("args")
        env = {} if transport.get("env") is None else transport.get("env")
        if not isinstance(cmd, str) or not cmd:
            blockers.append("transport.command")
        elif not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            blockers.append("transport.args")
        elif not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            blockers.append("transport.env")
        else:
            command = ["codex", "mcp", "add", name]
            for key, value in env.items():
                command.extend(["--env", f"{key}={value}"])
            command.extend(["--", cmd, *args])
    elif kind == "streamable_http":
        # `codex mcp add` accepts --oauth-client-id / --oauth-resource and
        # persists them to config.toml, but `codex mcp get --json` reports
        # NEITHER (verified on codex-cli 0.144.3: the fields land under
        # `[mcp_servers.<n>.oauth]` yet the JSON transport carries only url /
        # bearer_token_env_var / http_headers / env_http_headers). So the
        # snapshot cannot tell an OAuth registration from a plain one, and a
        # "successful" restore would silently drop the OAuth config. Absence
        # of evidence is not evidence of absence — refuse the whole transport
        # rather than replace what we cannot prove we can rebuild.
        blockers.append("transport.type='streamable_http' (OAuth state is not readable)")
    else:
        blockers.append(f"transport.type={kind!r}")

    if blockers:
        return _CodexRestorePlan(None, tuple(sorted(set(blockers))))
    return _CodexRestorePlan(command, ())


def _restore_codex_registration(plan: _CodexRestorePlan) -> bool:
    """Best-effort rollback of a removed registration; True when restored."""
    if plan.command is None:
        return False
    try:
        return _run_codex_mcp(plan.command).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _write_mcp_json_for_stm(
    target_dir: Path,
    server_cmd: str,
    server_args: list[str],
    env: dict[str, str] | None = None,
    *,
    replace_existing: bool = False,
) -> tuple[Path, bool]:
    """Write or merge ``<target_dir>/.mcp.json`` with the memtomem-stm entry.

    Aborts (``SystemExit(1)``, same convention as ``_load``) when an existing
    file cannot be read or parsed as a JSON object: merging is impossible, and
    the previous behavior — falling back to ``{}`` — silently discarded every
    registration already in the file. On abort the file is left untouched.
    """
    mcp_path = target_dir / ".mcp.json"
    existing: dict[str, Any]
    if mcp_path.exists():
        try:
            raw = mcp_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            # Not an OSError — read_text raises it from the decode step.
            click.echo(f"{_err('Error:')} {mcp_path} is not valid UTF-8: {exc}", err=True)
            click.echo("    Registration aborted; the file was not modified.", err=True)
            raise SystemExit(1) from exc
        except OSError as exc:
            click.echo(f"{_err('Error:')} Could not read {mcp_path}: {exc}", err=True)
            click.echo("    Registration aborted; the file was not modified.", err=True)
            raise SystemExit(1) from exc
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError as exc:
            click.echo(
                f"{_err('Error:')} Failed to parse {mcp_path}: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno}).",
                err=True,
            )
            click.echo(
                "    Registration aborted; the file was not modified. Fix the JSON "
                f"(python3 -m json.tool {shlex.quote(str(mcp_path))}) or rename the "
                "file aside (e.g. .mcp.json.bak) and re-run.",
                err=True,
            )
            raise SystemExit(1) from exc
        if not isinstance(loaded, dict):
            click.echo(
                f"{_err('Error:')} {mcp_path} top-level must be a JSON object, "
                f"got {type(loaded).__name__}. Registration aborted; the file was "
                "not modified.",
                err=True,
            )
            raise SystemExit(1)
        existing = loaded
    else:
        existing = {}
    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        click.echo(
            f"{_err('Error:')} {mcp_path} 'mcpServers' must be an object, got "
            f"{type(servers).__name__}. Registration aborted; the file was not modified.",
            err=True,
        )
        raise SystemExit(1)
    current_entry = servers.get("memtomem-stm")
    if current_entry is not None and not isinstance(current_entry, dict):
        click.echo(
            f"{_err('Error:')} {mcp_path} 'memtomem-stm' entry must be an object, got "
            f"{type(current_entry).__name__}. Registration aborted; the file was not modified.",
            err=True,
        )
        click.echo(
            "    Remove the 'memtomem-stm' entry manually, then re-run registration.",
            err=True,
        )
        raise SystemExit(1)
    if current_entry is not None and not replace_existing:
        # Existing host registrations are keep-by-default.  Managed daemon /
        # timeout values are applied only to a new entry or an explicit
        # `--replace-registration` refresh.
        return mcp_path, False

    # On explicit refresh, retain fields STM does not own and merge environment
    # values key-by-key.  This preserves host-specific metadata and operator
    # variables while updating only the command/args and managed STM keys.
    entry: dict[str, Any] = dict(current_entry or {})
    entry["command"] = server_cmd
    if server_args:
        entry["args"] = server_args
    else:
        entry.pop("args", None)
    if env:
        current_env = entry.get("env")
        if current_env is not None and not isinstance(current_env, dict):
            click.echo(
                f"{_err('Error:')} {mcp_path} 'memtomem-stm.env' must be an object, got "
                f"{type(current_env).__name__}. Registration aborted; the file was not modified.",
                err=True,
            )
            raise SystemExit(1)
        entry["env"] = {**(current_env or {}), **env}
    servers["memtomem-stm"] = entry
    # Atomic like every sibling config writer (_save, _desktop_json_remove_entry):
    # Claude Code re-reads .mcp.json at project load, and a crash/disk-full
    # mid-write must leave the prior contents, not truncated JSON. Unlike
    # those secret-bearing configs, .mcp.json is a shared project file: keep
    # an existing file's mode, default new ones to 0o644 — without an
    # explicit mode the helper's mkstemp temp (0600) survives the rename and
    # silently makes the file unreadable to group/other.
    try:
        file_mode = mcp_path.stat().st_mode & 0o777
    except OSError:
        file_mode = 0o644
    atomic_write_text(mcp_path, json.dumps(existing, indent=2) + "\n", mode=file_mode, durable=True)
    return mcp_path, True


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


def _emit_skip_hints(config_path: Path = _DEFAULT_CONFIG) -> None:
    """Manual-registration cheat sheet shown on option 3 (skip)."""
    server_cmd, server_args, server_env = _registration_command(config_path)
    click.echo(f"{_ok('Next:')} connect your MCP client to memtomem-stm.")
    click.echo("")
    click.echo(f"  {_hdr('Claude Code (CLI):')}")
    claude_cmd = ["claude", "mcp", "add", "memtomem-stm", "-s", "user"]
    for key, value in server_env.items():
        claude_cmd.extend(["-e", f"{key}={value}"])
    claude_cmd.extend(["--", server_cmd, *server_args])
    click.echo(f"    {_shell_join(claude_cmd)}")
    click.echo("")
    click.echo(f"  {_hdr('Codex (CLI):')}")
    codex_cmd = ["codex", "mcp", "add", "memtomem-stm"]
    for key, value in server_env.items():
        codex_cmd.extend(["--env", f"{key}={value}"])
    codex_cmd.extend(["--", server_cmd, *server_args])
    click.echo(f"    {_shell_join(codex_cmd)}")
    click.echo("")
    click.echo(f"  {_hdr('Claude Desktop / JSON MCP config:')}")
    # Every value goes through ``json.dumps``, which supplies its own
    # quotes — hand-writing them around ``server_cmd`` emitted a document
    # the user could not paste back whenever the interpreter path held a
    # ``"`` or a ``\`` (a Windows path always does). ``ensure_ascii`` stays
    # at its default here, unlike the ``--json`` legs: it escapes the
    # control characters and BiDi overrides ``_disp`` guards elsewhere, so
    # this snippet needs no separate display pass (#755).
    json_args = f', "args": {json.dumps(server_args)}' if server_args else ""
    json_env = f', "env": {json.dumps(server_env)}'
    click.echo(
        f'    {{ "mcpServers": {{ "memtomem-stm": {{ "command": {json.dumps(server_cmd)}'
        f"{json_args}{json_env} }} }} }}"
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


def _run_mcp_integration(
    preselected: int | None = None,
    *,
    client_mode: str | None = None,
    config_path: Path = _DEFAULT_CONFIG,
    replace_registration: bool = False,
) -> None:
    """Run the 3-way MCP registration flow.

    Called by both ``mms init`` (after config save) and ``mms register``
    (post-init re-entry path).

    ``preselected`` bypasses the interactive 3-way prompt when set (1, 2,
    or 3) — this is how ``--mcp`` / scripted callers skip stdin. When
    ``preselected`` is 1 and a duplicate is detected, we default to
    ``keep`` (non-destructive) rather than prompting, so CI / scripted
    callers don't hit ``click.Abort`` on EOF.
    """
    if preselected is None and client_mode is None and _SETUP_JSON_MODE.get():
        # JSON setup output captures stdout, making the interactive choice
        # invisible. With no explicit client, choose the non-destructive
        # fallback and leave manual registration hints available for later.
        preselected = 3

    if client_mode == "auto":
        if shutil.which("codex"):
            client_mode = "codex"
        elif shutil.which("claude"):
            client_mode = "claude"
        else:
            client_mode = "json"
    if client_mode == "skip":
        _SETUP_RESOLVED_CLIENT.set("skip")
        _emit_skip_hints(config_path)
        return

    server_cmd, server_args, server_env = _registration_command(config_path)
    if client_mode == "codex":
        _SETUP_RESOLVED_CLIENT.set("codex")
        restore: _CodexRestorePlan | None = None
        if _codex_registered():
            if not replace_registration:
                click.echo(f"  {_ok('Kept existing Codex registration.')}")
                return
            # Replacing is remove-then-add, and ``add`` can fail. Capture a
            # rollback FIRST and refuse to remove anything we could not put
            # back exactly — a failed add would otherwise leave the user with
            # no registration at all.
            snapshot = _capture_codex_registration()
            if snapshot is None:
                raise click.ClickException(
                    "could not read the existing Codex registration, so it cannot be "
                    "restored if the replacement fails; the previous registration was "
                    "left unchanged. Remove it with `codex mcp remove memtomem-stm` "
                    "and re-run to replace it anyway"
                )
            restore = _codex_restore_plan(snapshot)
            if restore.command is None:
                raise click.ClickException(
                    "the existing Codex registration uses settings `codex mcp add` "
                    f"cannot reproduce ({', '.join(restore.blockers)}), so it cannot be "
                    "restored if the replacement fails; the previous registration was "
                    "left unchanged. Remove it with `codex mcp remove memtomem-stm` "
                    "and re-run to replace it anyway"
                )
            # The replacement owns STM's managed keys, not unrelated operator
            # variables.  `_codex_restore_plan` has already proved this is a
            # stdio snapshot with a string-to-string env mapping, so merge it
            # before remove/add and let the managed values win on collisions.
            transport = snapshot.get("transport")
            raw_previous_env = transport.get("env") if isinstance(transport, dict) else None
            previous_env = raw_previous_env if isinstance(raw_previous_env, dict) else {}
            server_env = {**previous_env, **server_env}
            if not _remove_from_codex():
                raise click.ClickException(
                    "could not remove the existing Codex registration; "
                    "the previous registration was left unchanged"
                )
        success, reason = _register_with_codex(server_cmd, server_args, env=server_env)
        if success:
            click.echo(f"  {_ok('Registered with Codex.')}")
        else:
            # Unlike Claude Code, Codex has no local-file registration fallback.
            message = reason or "codex mcp add failed"
            if restore is not None:
                if _restore_codex_registration(restore):
                    message += "; restored the previous Codex registration"
                else:
                    # The captured command carries --env values, so it is never
                    # printed — the user re-runs `mms register` instead.
                    message += (
                        "; the previous Codex registration was removed and could not "
                        "be restored — re-run `mms register --client codex` once the "
                        "failure above is resolved"
                    )
            raise click.ClickException(message)
        return

    if client_mode == "json":
        _SETUP_RESOLVED_CLIENT.set("json")
        mcp_path, written = _write_mcp_json_for_stm(
            Path.cwd(),
            server_cmd,
            server_args,
            server_env,
            replace_existing=replace_registration,
        )
        if written:
            click.echo(f"  {_ok('Wrote')} {mcp_path}")
        else:
            click.echo(
                f"  {_ok('Kept existing registration')} (use --replace-registration to refresh)"
            )
        _emit_mcp_paste_hints()
        return

    if client_mode == "claude":
        preselected = 1

    interactive = preselected is None and client_mode is None
    choice = preselected if preselected is not None else _prompt_mcp_choice()
    click.echo("")

    if choice == 3:
        _SETUP_RESOLVED_CLIENT.set("skip")
        _emit_skip_hints(config_path)
        return

    if choice == 1:
        _SETUP_RESOLVED_CLIENT.set("claude")
        if _check_already_registered():
            click.echo(f"{_warn('Note:')} memtomem-stm is already registered with Claude Code.")
            if replace_registration:
                action = 2
            elif interactive:
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

        success, reason = _register_with_claude_code(server_cmd, server_args, env=server_env)
        if success:
            click.echo(f"  {_ok('Registered with Claude Code (user scope).')}")
            return

        reason_msg = {
            "not_installed": "claude CLI not found",
            "timeout": "claude mcp add timed out",
        }.get(reason, "claude mcp add failed")
        click.echo(f"  {_warn(reason_msg)} — falling back to .mcp.json.")

    # choice == 2, or choice == 1 fallback
    _SETUP_RESOLVED_CLIENT.set("json")
    mcp_path, written = _write_mcp_json_for_stm(
        Path.cwd(),
        server_cmd,
        server_args,
        server_env,
        replace_existing=replace_registration,
    )
    if written:
        click.echo(f"  {_ok('Wrote')} {mcp_path}")
    else:
        click.echo(f"  {_ok('Kept existing registration')} (use --replace-registration to refresh)")
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


@click.group(name="gateway")
def gateway_group() -> None:
    """Inspect and configure Toolgraph-backed gateway policy."""


@gateway_group.command(name="status")
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON for scripting.")
def gateway_status(config_path: str, *, as_json: bool = False) -> None:
    """Show the configured policy source and validate the active bundle."""
    from pydantic import ValidationError

    from memtomem_stm.proxy.config import ProxyConfig
    from memtomem_stm.proxy.toolgraph_bundle import PolicyBundleError, load_policy_bundle

    path = Path(config_path)
    try:
        config = ProxyConfig.model_validate(_load(path))
    except ValidationError as exc:
        raise click.ClickException(f"invalid proxy config: {exc.errors()[0]['msg']}") from exc
    tg = config.toolgraph
    result: dict[str, Any] = {
        "enabled": tg.enabled,
        "source": tg.source,
        "agent": tg.agent_id,
        "profile": config.exposure.profile.value,
    }
    if tg.enabled and tg.source == "bundle":
        result["bundle_path"] = str(tg.bundle_path.expanduser())
        if tg.query_profile != config.exposure.profile.value:
            result.update(
                {
                    "valid": False,
                    "error": "toolgraph.query_profile must match exposure.profile in bundle mode",
                }
            )
        else:
            try:
                snapshot = load_policy_bundle(
                    tg.bundle_path,
                    expected_agent=tg.agent_id,
                    expected_profile=config.exposure.profile.value,
                )
            except PolicyBundleError as exc:
                result.update({"valid": False, "error": str(exc)})
            else:
                decisions = list(snapshot.decisions.values())
                result.update(
                    {
                        "valid": True,
                        "bundle_digest": snapshot.bundle_digest,
                        "graph_instance_id": snapshot.instance_id,
                        "graph_generation": snapshot.generation,
                        "eligible": sum(d.decision == "eligible" for d in decisions),
                        "rejected": sum(d.decision == "rejected" for d in decisions),
                    }
                )
    if as_json:
        click.echo(_json_dumps(result, ensure_ascii=False, sort_keys=True))
        return
    click.echo(f"Gateway policy: {'enabled' if tg.enabled else 'disabled'} ({_disp(tg.source)})")
    click.echo(f"  agent/profile: {_disp(tg.agent_id)} / {config.exposure.profile.value}")
    if tg.source == "bundle" and tg.enabled:
        click.echo(f"  bundle: {_disp(str(result['bundle_path']))}")
        if result.get("valid"):
            click.echo(
                f"  active: graph generation {result['graph_generation']}, "
                f"{result['eligible']} eligible / {result['rejected']} rejected"
            )
            click.echo(f"  digest: {_disp(str(result['bundle_digest']))}")
        else:
            # The loader's message quotes the bundle file it just rejected.
            click.echo(f"  {_bad('INVALID')}: {_disp(str(result.get('error') or ''))}")


@gateway_group.command(name="explain")
@click.argument("tool_key")
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON for scripting.")
def gateway_explain(tool_key: str, config_path: str, *, as_json: bool = False) -> None:
    """Explain one qualified ``server::tool`` decision from the bundle."""
    from pydantic import ValidationError

    from memtomem_stm.proxy.config import ProxyConfig
    from memtomem_stm.proxy.toolgraph_bundle import PolicyBundleError, load_policy_bundle

    try:
        config = ProxyConfig.model_validate(_load(Path(config_path)))
        if config.toolgraph.source != "bundle":
            raise PolicyBundleError("toolgraph.source must be 'bundle' for gateway explain")
        if config.toolgraph.query_profile != config.exposure.profile.value:
            raise PolicyBundleError(
                "toolgraph.query_profile must match exposure.profile in bundle mode"
            )
        snapshot = load_policy_bundle(
            config.toolgraph.bundle_path,
            expected_agent=config.toolgraph.agent_id,
            expected_profile=config.exposure.profile.value,
        )
    except (ValidationError, PolicyBundleError) as exc:
        raise click.ClickException(str(exc)) from exc
    decision = snapshot.decisions.get(tool_key)
    if decision is None:
        raise click.ClickException(
            f"{tool_key!r} is not in the active bundle; strict mode treats it as unmapped"
        )
    result = {
        "tool_key": tool_key,
        "decision": decision.decision,
        "reason": decision.reason,
        "risk_score": decision.risk_score,
        "tool_contract_digest": decision.contract_digest,
        "bundle_digest": snapshot.bundle_digest,
        "graph_generation": snapshot.generation,
    }
    if as_json:
        click.echo(_json_dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        click.echo(f"{_disp(tool_key)}: {_disp(decision.decision)}")
        if decision.reason:
            click.echo(f"  reason: {_disp(decision.reason)}")
        risk_score = decision.risk_score if decision.risk_score is not None else "n/a"
        click.echo(f"  risk score: {risk_score}")
        click.echo(f"  graph generation: {snapshot.generation}")


@gateway_group.command(name="mode")
@click.argument("profile", type=click.Choice(["strict", "review", "explore"]))
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option("--bundle", "bundle_path", type=click.Path(path_type=Path), default=None)
@click.option("--apply", "do_apply", is_flag=True, help="Write the configuration.")
@click.option("--dry-run", is_flag=True, help="Explicitly preview without writing.")
@with_config_write_lock(skip=lambda kwargs: not kwargs.get("do_apply"))
def gateway_mode(
    profile: str,
    config_path: str,
    bundle_path: Path | None,
    do_apply: bool,
    dry_run: bool,
) -> None:
    """Preview or apply the bundle enforcement profile.

    The default is a safe preview. Use ``--apply`` to enable the bundle source
    and atomically align both Toolgraph query and STM exposure profiles.
    """
    if do_apply and dry_run:
        raise click.UsageError("--apply and --dry-run are mutually exclusive")
    path = Path(config_path)
    data = _load(path)
    toolgraph = data.get("toolgraph") or {}
    target_bundle = bundle_path or Path(
        toolgraph.get("bundle_path", "~/.memtomem/toolgraph/policy-bundle.json")
    )
    preview = {
        "enabled": True,
        "source": "bundle",
        "profile": profile,
        "agent": toolgraph.get("agent_id", "stm-proxy"),
        "bundle_path": str(target_bundle),
    }
    if not do_apply:
        click.echo(_json_dumps(preview, indent=2, ensure_ascii=False))
        click.echo("Preview only. Re-run with --apply to write the config.")
        return
    exposure = data.setdefault("exposure", {})
    writable_toolgraph = data.setdefault("toolgraph", {})
    exposure["profile"] = profile
    writable_toolgraph.update(
        {
            "enabled": True,
            "source": "bundle",
            "bundle_path": str(target_bundle),
            "query_profile": profile,
        }
    )
    _save(path, data)
    # Post-write prose again: ``--bundle`` is argv, so on POSIX it can carry a
    # surrogateescaped byte, and the config write above has already landed.
    click.echo(
        f"{_ok('Applied')} gateway mode {profile!r}; publish a matching bundle to "
        f"{_disp(str(target_bundle.expanduser()))}."
    )
    expanded_bundle = target_bundle.expanduser()
    if profile == "strict" and not expanded_bundle.is_file():
        click.echo(
            f"{_warn('Warning:')} no policy bundle exists at {_disp(str(expanded_bundle))}; "
            "strict mode will refuse to start until one is published.",
            err=True,
        )


cli.add_command(gateway_group)


def _mask_mapping_values(mapping: dict[str, Any]) -> dict[str, Any]:
    """Mask every value in an ``env`` / ``headers`` mapping (keys preserved).

    Machine-readable ``--json`` output is routinely piped to scripts, CI logs,
    issue comments, and agent transcripts, so it must not carry secret-bearing
    ``env`` / ``headers`` values verbatim. We redact *all* values rather than
    only classifier-flagged ones: a key/value classifier misses ``Cookie``-style
    headers and short or punctuated secrets, and machine output has no reliable
    way to tell a secret value from a benign one. Keys stay visible so scripts
    can still see *which* variables / headers a server defines.
    """
    return {key: REDACTED_DISPLAY for key in mapping}


def _redacted_servers_json(servers: dict[str, Any]) -> dict[str, Any]:
    """Server map for ``--json`` output, scrubbed of secret-bearing values.

    Two passes:

    * ``origin.original`` — the verbatim host entry an import captured — is
      dropped (#475). The summary keys (``source`` kinds, ``pruned`` flags,
      ``imported_at``) stay, plus ``has_original`` so scripts can tell a
      redacted block from one that never captured an original.
    * An entry's own active ``env`` and ``headers`` values are masked, since
      they reach ``--json`` verbatim and are routinely piped to logs. Keys are
      kept; only values become ``<REDACTED>``. Use the human-readable output
      (which never prints ``env`` / ``headers``) or read the on-disk config
      directly when a value is genuinely needed.

    Redaction is output-only: this returns shallow copies and never mutates the
    loaded config.
    """
    redacted: dict[str, Any] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            redacted[name] = cfg
            continue
        entry = dict(cfg)
        if isinstance(entry.get("origin"), dict):
            origin = dict(entry["origin"])
            origin["has_original"] = isinstance(origin.pop("original", None), dict)
            entry["origin"] = origin
        for field in ("env", "headers"):
            value = entry.get(field)
            if isinstance(value, dict):
                entry[field] = _mask_mapping_values(value)
            elif value is not None:
                # ``--json`` reads the raw config, not a validated
                # ``UpstreamServerConfig``, so a hand-edited / corrupted entry
                # can carry a non-dict ``env`` / ``headers`` (e.g. the string
                # ``"TOKEN=abc"``). Anything present-but-not-None here is still
                # potentially secret-bearing — never emit it verbatim.
                entry[field] = REDACTED_DISPLAY
        redacted[name] = entry
    return redacted


def _echo_json(payload: dict[str, Any]) -> None:
    """Emit the single JSON document of a mutating command's ``--json`` run.

    In ``--json`` mode stdout carries exactly one JSON object — human
    diagnostics stay on stderr — so ``mms <cmd> --json | jq`` parses on
    success and failure alike. Same formatting as the read-only commands.
    """
    click.echo(_json_dumps(payload, indent=2, ensure_ascii=False))


def _json_fail(
    action: str, code: str, message: str, *, exit_code: int = 1, **fields: Any
) -> NoReturn:
    """Failure envelope for a mutating command's ``--json`` mode.

    Callers print their human stderr line first (wording shared with the
    non-JSON path), then delegate the stdout document + exit here. ``code``
    is the stable snake_case identifier scripts branch on; ``message`` is
    the human sentence. Exit 1 = operational failure; exit 2 = consent
    missing (`host sync --json` precedent — a formatting flag must not
    authorize a destructive write, so ``--json`` without ``--yes`` refuses
    rather than prompts).
    """
    _echo_json({"action": action, "ok": False, "error": code, "message": message, **fields})
    sys.exit(exit_code)


def _json_requires_yes(action: str) -> NoReturn:
    """Refuse a prompting action in ``--json`` mode without ``--yes`` (exit 2)."""
    click.echo(
        f"{_err('Error:')} --json runs non-interactively; pass --yes to confirm.",
        err=True,
    )
    _json_fail(
        action,
        "confirmation_required",
        "--json runs non-interactively; pass --yes to confirm",
        exit_code=2,
    )


def _origin_fully_pruned(origin: Any) -> bool:
    """True when provenance records *every* host source as pruned (#475 PR4).

    The primary ``origin.source`` plus all recorded ``duplicates`` — the
    condition under which the entry exists only behind STM, so removing it
    from STM would leave the server registered nowhere. One predicate
    shared by the ``mms list`` pruned marker and the ``mms remove`` eject
    hint: the two surfaces must not contradict each other on what "pruned"
    means (a primary-only check would star an entry that an un-pruned
    duplicate still registers). Advisory by construction — flags are as
    recorded at prune time; ``mms eject``'s live no-clobber check stays the
    authoritative guard.

    Strict types, not truthiness: the block is pydantic-built by our own
    writers, so anything else is a hand-edited or corrupt config — e.g.
    ``"pruned": "false"`` (truthy string) or a non-list ``duplicates`` —
    and a claim this drives ("registered nowhere") must not rest on it.
    Malformed provenance answers False (codex R2).
    """
    if not isinstance(origin, dict):
        return False
    source = origin.get("source")
    if not isinstance(source, dict) or source.get("pruned") is not True:
        return False
    duplicates = origin.get("duplicates") or []
    if not isinstance(duplicates, list):
        return False
    return all(isinstance(d, dict) and d.get("pruned") is True for d in duplicates)


def _origin_cell(cfg: Any) -> str:
    """ORIGIN table cell for one ``mms list`` row (#475 PR4).

    ``-`` for entries without provenance (manual ``mms add``, pre-#475
    imports); otherwise the recorded ``origin.source.kind`` — the
    machine-readable identifier, not the human label, so the cell stays
    within one column and matches what ``--json`` exposes. A trailing ``*``
    marks an entry whose every recorded host source was pruned (the entry
    now exists only behind STM — see :func:`_origin_fully_pruned`); the
    legend under the table points at ``mms eject``. Unknown kinds print as
    recorded — the cell is a display of stored provenance, not a
    validation of it.
    """
    origin = cfg.get("origin") if isinstance(cfg, dict) else None
    source = origin.get("source") if isinstance(origin, dict) else None
    if not isinstance(source, dict) or not isinstance(source.get("kind"), str):
        return "-"
    return source["kind"] + ("*" if _origin_fully_pruned(origin) else "")


def _tuning_readiness(data: dict[str, Any]) -> dict[str, Any]:
    """Read-only signal that enough per-tool samples exist for ``mms tune``.

    This intentionally does not run the tuner or migrate/open SQLite
    read-write: ``status`` and default ``doctor`` are inspection commands.
    ``mms tune`` remains the authoritative recommendation preview and apply
    surface once a tool reaches the tuner's five-call sample floor.
    """
    from memtomem_stm.proxy.config import ProxyConfig
    from memtomem_stm.proxy.metrics_store import read_compression_summary
    from memtomem_stm.proxy.tuner import MIN_CALLS

    try:
        config = ProxyConfig.model_validate(data)
    except Exception:
        return {
            "available": False,
            "ready": False,
            "sample_threshold": MIN_CALLS,
            "tools": [],
        }
    summary = read_compression_summary(config.metrics.db_path, source="mcp")
    rows = summary.get("by_tool")
    safe_rows = rows if isinstance(rows, list) else []
    tools = [
        {"server": row["server"], "tool": row["tool"], "calls": row["calls"]}
        for row in safe_rows
        if isinstance(row, dict)
        and isinstance(row.get("server"), str)
        and isinstance(row.get("tool"), str)
        and isinstance(row.get("calls"), int)
        and row["calls"] >= MIN_CALLS
    ]
    return {
        "available": bool(summary.get("available")),
        "ready": bool(tools),
        "sample_threshold": MIN_CALLS,
        "tools": tools,
    }


@cli.command()
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON for scripting.")
def status(config_path: str, *, as_json: bool = False) -> None:
    """Show proxy gateway config summary (path, enabled flag, server count)."""
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
    config_error = _schema_validation_error(data)
    tuning = _tuning_readiness(data)
    # Same predicate as the `mms list` pruned marker (via _origin_cell), so
    # this count and list's `*` rows can never disagree on what "pruned" means.
    pruned_count = sum(
        1
        for cfg in servers.values()
        if isinstance(cfg, dict) and _origin_fully_pruned(cfg.get("origin"))
    )

    if as_json:
        # The full (redacted) `servers` map stays in --json even though the
        # human output below no longer prints per-server rows (#614) —
        # scripts consume this shape (see the redaction tests / #476), and
        # dropping it would break them for zero information gain. The two
        # count keys are additive so callers can match the human summary
        # without re-deriving the pruned predicate.
        click.echo(
            _json_dumps(
                {
                    "config_path": str(resolved),
                    "enabled": enabled,
                    "config_valid": config_error is None,
                    "config_error": config_error,
                    "server_count": len(servers),
                    "pruned_count": pruned_count,
                    "tuning": tuning,
                    "servers": _redacted_servers_json(servers),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    # Human output is a config summary — per-server detail lives in
    # `mms list` (#614: the two commands used to print near-identical
    # output; status now answers "is the proxy set up and pointed at the
    # right config", list answers "what servers are behind it").
    if config_error:
        click.echo(f"{_warn('Warning:')} {_CONFIG_INVALID_WARNING}: {_disp(config_error)}")
    click.echo(f"Config : {resolved}")
    click.echo(f"Enabled: {'yes' if enabled else 'no'}")
    pruned_suffix = f" ({pruned_count} host-pruned)" if pruned_count else ""
    click.echo(f"Servers: {len(servers)}{pruned_suffix}")
    if tuning["ready"]:
        click.echo(
            f"Tuning : ready for {len(tuning['tools'])} tool(s); "
            "run `mms tune` to preview recommendations"
        )
    click.echo("")
    if servers:
        click.echo("Run `mms list` for per-server detail; `mms health` to probe connectivity.")
    else:
        click.echo("Run `mms add` (or `mms init`) to register an upstream.")


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
            _json_dumps(
                {"config_path": str(resolved), "servers": _redacted_servers_json(servers)},
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
    # one space of padding. ORIGIN ``<16`` likewise fits the widest
    # source kind plus the pruned marker (``claude-desktop*``, 15).
    # SURFACING ``<10`` fits its 9-char header; the per-server surfacing
    # toggle's visible home is this table (#614 — `mms status` no longer
    # prints per-server rows). ``max_result_chars`` deliberately has no
    # column: the effective value is per-tool once `mms tune --apply`
    # writes ``tool_overrides``, so a per-server number would mislead —
    # read it via ``--json`` or the config file.
    header = (
        f"{'NAME':<20} {'PREFIX':<10} {'TRANSPORT':<16} {'COMPRESSION':<12} "
        f"{'SURFACING':<10} {'ORIGIN':<16} COMMAND / URL"
    )
    click.echo(_hdr(header))
    click.echo("-" * len(header))
    any_pruned = False
    for name, cfg in servers.items():
        transport = cfg.get("transport", "stdio")
        prefix = cfg.get("prefix", "")
        compression = cfg.get("compression", "auto")
        surfacing = "on" if cfg.get("surfacing_enabled", True) else "off"
        origin_cell = _origin_cell(cfg)
        any_pruned = any_pruned or origin_cell.endswith("*")
        if transport == "stdio":
            cmd = cfg.get("command", "")
            args_str = " ".join(cfg.get("args", []))
            detail = f"{cmd} {args_str}".strip()
        else:
            detail = cfg.get("url", "")
        # Escaped per cell, inside the pad: a CR in any of them would
        # otherwise redraw this row over the previous one and take the
        # whole table's alignment with it (#755). ``surfacing`` is computed
        # here, so only the config-derived cells need it.
        click.echo(
            f"{_disp(name):<20} {_disp(prefix):<10} {_disp(transport):<16} "
            f"{_disp(compression):<12} {surfacing:<10} {_disp(origin_cell):<16} {_disp(detail)}"
        )
    click.echo(f"\n{len(servers)} server(s) configured.")
    if any_pruned:
        click.echo("* host original pruned — `mms eject NAME` restores it (see `mms eject -h`).")


def _render_compression_block(summary: dict[str, Any]) -> None:
    click.echo(_hdr("Proxy / Compression"))
    click.echo("=" * 30)
    if not summary.get("available") or not summary.get("total_calls"):
        click.echo("  No proxy metrics recorded yet.")
        return
    if summary.get("schema_outdated"):
        click.echo(f"  {_warn('schema outdated')} — error counts unavailable (pre-migration DB).")
    orig = summary["total_original_chars"]
    comp = summary["total_compressed_chars"]
    ratio = float(summary["saved_ratio"]) * 100
    click.echo(f"  calls: {summary['total_calls']}  (errors: {summary['error_count']})")
    click.echo(f"  chars: {orig:,} -> {comp:,}  (saved {summary['saved_chars']:,}, {ratio:.1f}%)")
    by_tool = summary.get("by_tool") or []
    if by_tool:
        click.echo("")
        header = (
            f"  {'SERVER':<12} {'TOOL':<28} {'CALLS':>6} {'ORIG':>10} {'COMP':>10} {'SAVED%':>7}"
        )
        click.echo(_hdr(header))
        for row in by_tool:
            saved_pct = float(row["saved_ratio"]) * 100
            # Truncate first, then escape: the slice keeps its existing
            # meaning (28 characters of the recorded name) and an escaped
            # cell overflows like an over-long one, rather than being cut
            # mid-escape into an ambiguous ``\u00`` (#755).
            click.echo(
                f"  {_disp(str(row['server'])[:12]):<12} {_disp(str(row['tool'])[:28]):<28} "
                f"{row['calls']:>6} {row['original_chars']:>10,} "
                f"{row['compressed_chars']:>10,} {saved_pct:>6.1f}%"
            )


def _render_surfacing_block(summary: dict[str, Any]) -> None:
    click.echo(_hdr("Surfacing"))
    click.echo("=" * 30)
    if not summary.get("available"):
        click.echo("  No surfacing events recorded yet.")
        return
    click.echo(
        f"  surfaced events: {summary['events_total']}  "
        f"(distinct tools: {summary['distinct_tools']})"
    )
    click.echo(f"  feedback ratings: {summary['total_feedback']}")
    for rating, count in sorted((summary.get("rating_distribution") or {}).items()):
        click.echo(f"    {rating:<20} {count}")
    faults = summary.get("faults") or {}
    if faults:
        window_days = summary.get("faults_window_days")
        click.echo(f"  pipeline faults (last {window_days} UTC days):")
        for kind, count in sorted(faults.items()):
            click.echo(f"    {kind:<20} {count}")
        last_at = summary.get("faults_last_at")
        if isinstance(last_at, (int, float)):
            last_str = datetime.fromtimestamp(last_at).strftime("%Y-%m-%d %H:%M")
            click.echo(f"    last fault: {last_str}")
        click.echo(
            "  "
            + _warn(
                "surfacing has been skipping on degraded-LTM faults — "
                "see stm-daemon.log / server stderr; timeouts usually mean the "
                "LTM answers slower than surfacing.timeout_seconds"
            )
        )
    diagnostics = summary.get("diagnostics") or {}
    if diagnostics:
        window_days = summary.get("diagnostics_window_days")
        click.echo(f"  score-scale diagnostics (last {window_days} UTC days):")
        for kind, count in sorted(diagnostics.items()):
            click.echo(f"    {kind:<28} {count}")
        last_at = summary.get("diagnostics_last_at")
        if isinstance(last_at, (int, float)):
            last_str = datetime.fromtimestamp(last_at).strftime("%Y-%m-%d %H:%M")
            click.echo(f"    last diagnostic: {last_str}")
        click.echo(
            "  "
            + _warn(
                "LTM candidates repeatedly stayed below active min_score — "
                "the LTM may be single-leg/BM25-only or min_score may be "
                "intentionally high; check embedding extras and LTM logs. "
                "STM did not lower the threshold"
            )
        )


@cli.command()
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option("--tool", "tool_filter", default=None, help="Filter to one upstream tool name.")
@click.option(
    "--source",
    "source_filter",
    type=click.Choice(["mcp", "hook"]),
    default=None,
    help="Filter compression rows by provenance: 'mcp' (proxied upstream tools) "
    "or 'hook' (native built-in tools recorded by 'mms hook').",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON for scripting.")
def stats(
    config_path: str,
    *,
    tool_filter: str | None = None,
    source_filter: str | None = None,
    as_json: bool = False,
) -> None:
    """Show proxy compression and surfacing stats from the persistent stores.

    Reads ``proxy_metrics.db`` and ``stm_feedback.db`` read-only (it never
    creates or migrates them) and reports all-time totals. The live MCP server
    keeps additional in-memory counters that a separate CLI process cannot see,
    so the numbers here reflect only what has been written to disk.
    """
    from memtomem_stm.config import STMConfig
    from memtomem_stm.proxy.config import ProxyConfig, collect_proxy_env_overrides
    from memtomem_stm.proxy.metrics_store import read_compression_summary
    from memtomem_stm.surfacing.feedback_store import read_surfacing_summary

    # Refuse before the stores are touched or anything is printed. The summary
    # readers treat an unencodable filter as "matched nothing but the DB is
    # fine", which reads as a real all-time zero here rather than as a rejected
    # input; the human form then crashed encoding the filter echo below, so the
    # two output modes disagreed about the same argv. ``--source`` needs no such
    # guard: it is a ``click.Choice``. ``!r`` renders the code unit as ASCII
    # ``\udcff`` — echoing it raw is the crash this is refusing (mirrors the
    # ``add`` name guard). Reachable from plain argv: on POSIX a byte that is
    # not valid UTF-8 decodes with ``surrogateescape``.
    if tool_filter is not None and has_lone_surrogate(tool_filter):
        raise click.UsageError(f"--tool {tool_filter!r} is not valid UTF-8.")

    path = Path(config_path)
    resolved = path.expanduser().resolve()

    # Resolve config + DB paths exactly as the server does (server.app_lifespan):
    # JSON file loaded unconditionally with env vars deep-merged on top, so a
    # file-level ``metrics.db_path`` points ``mms stats`` at the same
    # ``proxy_metrics.db`` the server writes to even when the proxy is enabled
    # via ``MEMTOMEM_STM_PROXY__ENABLED``. (The server used to skip the file
    # in env-enabled mode and this command mirrored the bypass; both honor the
    # file now.) ``STMConfig()`` still supplies the surfacing feedback path.
    try:
        stm_config: STMConfig | None = STMConfig()
    except Exception:
        stm_config = None
    feedback_path = (
        stm_config.surfacing.feedback_db_path
        if stm_config is not None
        else Path("~/.memtomem/stm_feedback.db")
    )

    loaded = ProxyConfig.load_from_file(
        path, env_overrides=collect_proxy_env_overrides(), missing_ok=False
    )
    if loaded is not None:
        config_status = "ok"
        proxy_cfg = loaded
    elif not resolved.exists():
        # Missing file: mirror the server, which keeps STMConfig()'s
        # pydantic-settings parse — it handles JSON-encoded complex env
        # values (e.g. UPSTREAM_SERVERS) that the raw-string overlay can't.
        # (``missing_ok=False`` already folded "missing" into ``None``; the
        # exists() here only picks the status label and fallback source.)
        config_status = "missing"
        proxy_cfg = stm_config.proxy if stm_config is not None else ProxyConfig()
    else:
        # Parse/validation error — fall back to defaults purely to locate the
        # DB paths to probe, but flag the config as invalid.
        config_status = "invalid"
        proxy_cfg = ProxyConfig()

    compression = read_compression_summary(
        proxy_cfg.metrics.db_path, tool=tool_filter, source=source_filter
    )
    surfacing = read_surfacing_summary(feedback_path, tool=tool_filter)

    # These numbers come only from the on-disk stores; the live MCP server
    # keeps additional in-memory-only counters (cache hits, latency, reconnects,
    # …) that a separate CLI process cannot see. Name the source and point at
    # the live tool so a user comparing the two doesn't read the gap as a bug.
    # Wording is generic (not the default filenames) because metrics.db_path and
    # surfacing.feedback_db_path are both configurable.
    data_source = (
        "on-disk stores only (metrics DB + surfacing-feedback DB); "
        "live in-memory counters via the stm_proxy_stats MCP tool"
    )

    if as_json:
        click.echo(
            _json_dumps(
                {
                    "config_path": str(resolved),
                    "config_status": config_status,
                    "enabled": proxy_cfg.enabled,
                    "servers": len(proxy_cfg.upstream_servers),
                    "tool_filter": tool_filter,
                    "source_filter": source_filter,
                    "data_source": data_source,
                    "compression": compression,
                    "surfacing": surfacing,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    click.echo(f"Config : {resolved} ({config_status})")
    click.echo(f"Enabled: {'yes' if proxy_cfg.enabled else 'no'}")
    click.echo(f"Servers: {len(proxy_cfg.upstream_servers)}")
    if tool_filter:
        click.echo(f"Filter : tool={tool_filter}")
    if source_filter:
        click.echo(f"Source : {source_filter}")
    click.echo(f"Data   : {data_source}")
    click.echo("")
    _render_compression_block(compression)
    click.echo("")
    _render_surfacing_block(surfacing)


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
    "--header",
    "header_pairs",
    multiple=True,
    metavar="KEY=VALUE",
    help="HTTP header for sse/streamable_http transports (repeatable). "
    "Values are stored in plaintext in the config file (0600 perms).",
)
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
    "Incompatible with NAME / --prefix / --command / --args / --url / --env "
    "/ --header.",
)
@click.option(
    "--all",
    "select_all",
    is_flag=True,
    default=False,
    help="With --from-clients/--import: import every newly discovered server "
    "without prompting, assigning each a suggested prefix. Non-interactive "
    "even on a TTY. Mutually exclusive with --select.",
)
@click.option(
    "--select",
    "select_names",
    multiple=True,
    metavar="NAME[,NAME...]",
    help="With --from-clients/--import: import only the named discovered "
    "servers, without prompting. Repeatable and comma-separated. A name no "
    "client advertises is an error; one already registered here is skipped.",
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
@click.option("--json", "as_json", is_flag=True, help="Output as JSON for scripting.")
@with_config_write_lock(json_envelope=True)
def add(
    name: str | None,
    config_path: str,
    command: str,
    args_str: str,
    prefix: str,
    transport: str,
    url: str,
    env_pairs: tuple[str, ...],
    header_pairs: tuple[str, ...],
    compression: str,
    max_result_chars: int,
    validate: bool,
    validate_timeout: int,
    from_clients: bool,
    select_all: bool,
    select_names: tuple[str, ...],
    prune: bool,
    as_json: bool = False,
) -> None:
    """Add an upstream MCP server to the proxy configuration."""
    path = Path(config_path)

    # ``--select a,b --select c`` and ``--select a --select b`` are the same
    # request: flatten both spellings into one ordered tuple.
    selected = tuple(n.strip() for value in select_names for n in value.split(",") if n.strip())
    if select_all and selected:
        raise click.UsageError("--all cannot be combined with --select.")
    if (select_all or selected) and not from_clients:
        raise click.UsageError("--all / --select require --from-clients/--import.")

    if from_clients:
        noninteractive = select_all or bool(selected)
        if as_json and not noninteractive:
            # Without a selection flag the import path is an interactive TUI
            # flow, and a formatting flag must not turn prompts into
            # guesses — there is no document to serialize until the caller
            # says *what* to import.
            raise click.UsageError(
                "--json is not supported with --from-clients/--import without a "
                "non-interactive selection; pass --all or --select NAME[,NAME...]."
            )
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
        if header_pairs:
            conflicts.append("--header")
        if conflicts:
            raise click.UsageError(
                f"--from-clients cannot be combined with: {', '.join(conflicts)}."
            )
        _add_from_clients(
            path,
            validate=validate,
            validate_timeout=validate_timeout,
            prune=prune,
            select_all=select_all,
            select_names=selected,
            as_json=as_json,
        )
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
    if has_lone_surrogate(name):
        # Reachable without a hand-edited config: on POSIX an argument holding
        # a byte that is not valid UTF-8 is decoded with ``surrogateescape``.
        # Writing it is safe now (#757) but the entry would be unusable — see
        # ``has_lone_surrogate`` — so refuse rather than persist a name that
        # only fails later, at the first proxied call.
        # ``!r`` renders the offending code unit as ASCII ``\udcff``; echoing
        # the name raw is the very crash this command is refusing to set up.
        click.echo(f"{_err('Error:')} server name {name!r} is not valid UTF-8.", err=True)
        if as_json:
            _json_fail("add", "invalid_name", f"server name {name!r} is not valid UTF-8", name=name)
        sys.exit(1)

    data = _load(path)
    servers: dict[str, Any] = data.setdefault("upstream_servers", {})

    if name in servers:
        click.echo(
            f"{_err('Error:')} server '{_disp(name)}' already exists. Use `remove` first.",
            err=True,
        )
        if as_json:
            _json_fail("add", "already_exists", f"server '{name}' already exists", name=name)
        sys.exit(1)

    # VAL-1: prefix format validation
    format_error = prefixes.prefix_format_error(prefix)
    if format_error:
        click.echo(f"{_err('Error:')} {format_error}", err=True)
        if as_json:
            _json_fail("add", "invalid_prefix", f"invalid prefix '{prefix}'", name=name)
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
        if as_json:
            _json_fail(
                "add",
                "prefix_too_long",
                f"prefix '{prefix}' is {len(prefix)} chars; max is {hard_limit}",
                name=name,
            )
        sys.exit(1)
    # Non-abort warnings print to stderr in both modes AND land in the
    # --json ``warnings`` array — one message string so wording can't fork.
    warnings_json: list[str] = []
    if len(prefix) > warn_at:
        max_tool = tool_name_budget.TOOL_NAME_LIMIT - tool_name_budget.overhead() - len(prefix)
        warn_msg = (
            f"prefix '{prefix}' ({len(prefix)} chars) "
            f"leaves only {max_tool} chars for upstream tool names — longer "
            f"ones will be silently dropped by clients. Consider a shorter "
            f"--prefix, or register STM as 'mms' in your client config. "
            f"Proceeding."
        )
        click.echo(f"{_warn('Warning:')} {warn_msg}", err=True)
        warnings_json.append(warn_msg)

    # VAL-2: duplicate prefix — hard reject. The runtime pydantic validator
    # (``ProxyConfig._check_unique_upstream_prefixes``) refuses to load a
    # config with a shared prefix, so saving one here would strand the user
    # with a server that silently fails to start. Same shared detection as
    # the runtime (``proxy/prefixes.py``) so the two can't diverge.
    collisions = prefixes.prefix_collisions(
        {
            **{s: str(c.get("prefix", "")) for s, c in servers.items()},
            name: prefix,
        }
    )
    if collisions:
        click.echo(
            f"{_err('Error:')} {_disp(prefixes.format_collision_error(collisions))}. "
            "The proxy refuses to load configs with duplicate prefixes — "
            "pick a different --prefix.",
            err=True,
        )
        if as_json:
            _json_fail(
                "add",
                "duplicate_prefix",
                prefixes.format_collision_error(collisions),
                name=name,
            )
        sys.exit(1)

    # VAL-3/VAL-4: per-transport required field (shared with `mms doctor`);
    # the `--` prefix turns the config-flavored message into the option name.
    field_error = _transport_field_error(transport, command, url)
    if field_error:
        click.echo(f"{_err('Error:')} --{field_error}.", err=True)
        if as_json:
            code = "stdio_requires_command" if transport == "stdio" else "url_required"
            _json_fail("add", code, f"--{field_error}", name=name)
        sys.exit(1)

    # VAL-5: --header only applies to HTTP transports; a header on a stdio
    # entry would be silently ignored by the runtime.
    if transport == "stdio" and header_pairs:
        click.echo(
            f"{_err('Error:')} --header requires an HTTP transport (sse/streamable_http).",
            err=True,
        )
        if as_json:
            _json_fail(
                "add",
                "header_requires_http",
                "--header requires an HTTP transport (sse/streamable_http)",
                name=name,
            )
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
                if as_json:
                    _json_fail("add", "malformed_args", f"malformed --args: {exc}", name=name)
                sys.exit(1)
    else:
        entry["url"] = url

    if env_pairs:
        env_dict: dict[str, str] = {}
        for idx, pair in enumerate(env_pairs, start=1):
            # Never echo the raw argument in these diagnostics: a malformed
            # --env is likely a stray credential (`--env =tok`, or a bare
            # token missing KEY=), and both stderr and the --json error
            # payload are routinely piped to CI logs and transcripts. The
            # dangerous-key diagnostic below still names the KEY — keys are
            # not secret-bearing.
            if "=" not in pair:
                msg = (
                    f"--env #{idx} must be KEY=VALUE "
                    "(raw argument withheld: env values may be secrets)"
                )
                click.echo(f"{_err('Error:')} {msg}", err=True)
                if as_json:
                    _json_fail("add", "invalid_env", msg, name=name)
                sys.exit(1)
            k, v = pair.split("=", 1)
            if not k:
                msg = (
                    f"--env #{idx} key must be non-empty "
                    "(raw argument withheld: env values may be secrets)"
                )
                click.echo(f"{_err('Error:')} {msg}", err=True)
                if as_json:
                    _json_fail("add", "invalid_env", msg, name=name)
                sys.exit(1)
            if k.upper() in _DANGEROUS_ENV_KEYS:
                click.echo(
                    f"{_err('Error:')} --env key '{k}' is blocked for security reasons "
                    "(could enable code injection in spawned processes).",
                    err=True,
                )
                if as_json:
                    _json_fail(
                        "add",
                        "invalid_env",
                        f"--env key '{k}' is blocked for security reasons",
                        name=name,
                    )
                sys.exit(1)
            env_dict[k] = v
        entry["env"] = env_dict

    if header_pairs:
        headers_dict: dict[str, str] = {}
        for idx, pair in enumerate(header_pairs, start=1):
            # Unlike the --env diagnostics above, these NEVER echo the raw
            # argument: a malformed --header is likely a stray credential
            # (e.g. `--header =Bearer_tok`), and both stderr and the --json
            # error payload are routinely piped to CI logs and transcripts.
            if "=" not in pair:
                msg = (
                    f"--header #{idx} must be KEY=VALUE "
                    "(raw argument withheld: header values may be secrets)"
                )
                click.echo(f"{_err('Error:')} {msg}", err=True)
                if as_json:
                    _json_fail("add", "invalid_header", msg, name=name)
                sys.exit(1)
            k, v = pair.split("=", 1)
            if not k:
                msg = (
                    f"--header #{idx} key must be non-empty "
                    "(raw argument withheld: header values may be secrets)"
                )
                click.echo(f"{_err('Error:')} {msg}", err=True)
                if as_json:
                    _json_fail("add", "invalid_header", msg, name=name)
                sys.exit(1)
            # Deliberately NO _DANGEROUS_ENV_KEYS check here: that list guards
            # env injection into spawned processes; headers are sent over HTTP
            # and never touch a subprocess environment. A header literally
            # named "PATH" is legal.
            headers_dict[k] = v
        entry["headers"] = headers_dict

    # The name gate above is not enough: a `command` gets spawned, a `url`
    # dialled, `env` values handed to a child process, and each of those
    # encodes. Before #757 the write itself refused such an entry, so making
    # `_save` succeed is what let one reach disk — an entry that saves but
    # can never run. Gate the assembled entry at the one point that writes
    # it. The diagnostic names the field and never its value: env and header
    # values are routinely secrets and this text reaches CI logs.
    bad_field = unencodable_field(entry)
    if bad_field is not None:
        msg = f"{bad_field} is not valid UTF-8 (value withheld)"
        click.echo(f"{_err('Error:')} {msg}", err=True)
        if as_json:
            _json_fail("add", "invalid_entry", msg, name=name)
        sys.exit(1)

    tools_reachable: int | None = None
    if validate:
        if not as_json:
            click.echo(f"Validating '{_disp(name)}' (timeout={validate_timeout}s)...")
        probe = asyncio.run(_probe_servers({name: entry}, validate_timeout))[name]
        if not probe.connected:
            msg = f"validation failed — {probe.error} (stage reached: {probe.stage.display()})"
            # ``msg`` is also the ``--json`` error text below, which a
            # consumer parses rather than displays — escape the printed
            # copy only (#755).
            click.echo(f"{_err('Error:')} {_disp(msg)}", err=True)
            if as_json:
                _json_fail("add", "validation_failed", msg, name=name)
            sys.exit(1)
        tools_reachable = probe.tools
        if not as_json:
            click.echo(f"{_ok('Validated:')} {probe.tools} tool(s) reachable.")

    servers[name] = entry
    _save(path, data)
    if as_json:
        _echo_json(
            {
                "action": "add",
                "ok": True,
                "config_path": str(path.expanduser().resolve()),
                "name": name,
                "prefix": prefix,
                # Redacted like `status`/`list` --json: env/header values are
                # secret-bearing and --json output is routinely piped to logs.
                "server": _redacted_servers_json({name: entry})[name],
                "validated": validate,
                "tools_reachable": tools_reachable,
                "warnings": warnings_json,
            }
        )
        return
    click.echo(f"{_ok('Added')} server '{_disp(name)}' (prefix={prefix})")


# ── init command ────────────────────────────────────────────────────────


def _prompt_prefix(default: str | None = None, taken: set[str] | None = None) -> str:
    """Prompt for a prefix until it passes the same rules as ``add --prefix``.

    ``taken`` holds prefixes already claimed (existing config entries plus
    earlier picks in the same run) — a colliding value re-prompts, because
    the runtime refuses to load configs with duplicate prefixes and the
    suggestion default alone can't stop a user-typed collision.
    """
    while True:
        value = click.prompt(
            "Tool prefix (letters/digits/underscores, e.g. 'fs')",
            type=str,
            default=default,
            show_default=default is not None,
        )
        if prefixes.prefix_format_error(value):
            click.echo(
                f"  {_warn('Invalid:')} must start with a letter, contain only letters/digits/"
                "underscores, and not contain '__'. Try again."
            )
            continue
        if taken and value in taken:
            click.echo(
                f"  {_warn('Invalid:')} prefix '{value}' is already used by another "
                "server. Pick a unique prefix."
            )
            continue
        return value


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
        headers = raw.get("headers")
        if isinstance(headers, dict):
            # Unlike env, no dangerous-key filter: headers go on HTTP
            # requests, not into a spawned process's environment.
            safe_headers = {str(k): str(v) for k, v in headers.items() if isinstance(k, str)}
            if safe_headers:
                entry["headers"] = safe_headers
    return entry


# One definition of the source-client locations the import flow can read,
# keeping three consumers in lockstep (#475): the discovery labels below,
# the removal hint (`_source_removal_hint`), and the prune writer
# (`_prune_from_source`). ``kind`` is the machine-readable identifier
# persisted in ``origin.source.kind`` (see ``UpstreamOrigin`` in
# ``proxy/config.py``); ``claude_scope`` is the ``claude mcp remove -s``
# scope for shell-out sources, ``None`` marking the one source (Claude
# Desktop) handled by direct JSON edit instead.
@dataclass(frozen=True)
class _SourceSpec:
    label: str
    kind: str
    claude_scope: str | None


_SOURCE_SPECS: tuple[_SourceSpec, ...] = (
    _SourceSpec(".mcp.json (project)", "mcp-json", "project"),
    _SourceSpec("Claude Code (user)", "claude-user", "user"),
    _SourceSpec("Claude Code (project)", "claude-project", "local"),
    _SourceSpec("Claude Desktop", "claude-desktop", None),
)
_SOURCE_BY_LABEL: dict[str, _SourceSpec] = {spec.label: spec for spec in _SOURCE_SPECS}
_SOURCE_BY_KIND: dict[str, _SourceSpec] = {spec.kind: spec for spec in _SOURCE_SPECS}
_EJECT_TARGETS_HELP = "claude-user | claude-project[:PATH] | mcp-json[:PATH] | claude-desktop"


def _discover_candidates(cwd: Path) -> list[dict[str, Any]]:
    """Scan known MCP-client configs and return importable candidates.

    Each candidate dict is ``{name, source, entry, raw, source_ref}`` where
    ``entry`` is an already-normalized STM upstream-server entry (sans
    prefix), ``raw`` is the **verbatim** host entry (normalization is lossy —
    the original is what ``mms eject`` restores, #475), and ``source_ref``
    is the structured ``{kind, path?}`` record persisted as
    ``origin.source``. The first source to claim a name wins; later
    duplicates are dropped with a ``duplicate_in`` label note for the UI
    plus a structured ``duplicates`` record (``{label, source_ref, raw}``)
    so provenance and the prune backup log can address every source.
    """
    sources: list[tuple[_SourceSpec, str | None, dict[str, Any]]] = []

    # 1. Project-scope ``.mcp.json`` in cwd (Claude Code project config).
    mcp_json_path = cwd / ".mcp.json"
    proj = _read_json_safely(mcp_json_path)
    if proj and isinstance(proj.get("mcpServers"), dict):
        sources.append(
            (
                _SOURCE_BY_LABEL[".mcp.json (project)"],
                str(mcp_json_path.resolve()),
                proj["mcpServers"],
            )
        )

    # 2. Claude Code ~/.claude.json user-scope + per-project entries.
    cc = _read_json_safely(Path("~/.claude.json").expanduser())
    if cc:
        if isinstance(cc.get("mcpServers"), dict):
            sources.append((_SOURCE_BY_LABEL["Claude Code (user)"], None, cc["mcpServers"]))
        projects = cc.get("projects")
        if isinstance(projects, dict):
            # Match on resolved cwd so we don't duplicate entries across sibling projects.
            resolved_cwd = str(cwd.resolve())
            proj_entry = projects.get(resolved_cwd)
            if isinstance(proj_entry, dict) and isinstance(proj_entry.get("mcpServers"), dict):
                sources.append(
                    (
                        _SOURCE_BY_LABEL["Claude Code (project)"],
                        resolved_cwd,
                        proj_entry["mcpServers"],
                    )
                )

    # 3. Claude Desktop (platform-specific path from `_desktop_config_path`;
    #    absent file → skipped).
    desktop = _read_json_safely(_desktop_config_path())
    if desktop and isinstance(desktop.get("mcpServers"), dict):
        sources.append((_SOURCE_BY_LABEL["Claude Desktop"], None, desktop["mcpServers"]))

    seen: dict[str, dict[str, Any]] = {}
    for spec, src_path, servers in sources:
        if not isinstance(servers, dict):
            continue
        for name, raw in servers.items():
            if not isinstance(name, str) or not isinstance(raw, dict):
                continue
            if has_lone_surrogate(name):
                # Skip rather than abort the scan: one unusable name in a host
                # config must not block importing the servers beside it.
                # `"\udcff"` is a legal JSON escape, so a host file can carry
                # one (#757). Say so, though — dropping in silence would leave
                # "No MCP servers found" as the only clue when it is the sole
                # entry. ``!r`` renders the code unit as ASCII; echoing the name
                # raw is the crash being avoided.
                click.echo(
                    f"{_warn('Note:')} skipping {name!r} from {spec.label} — "
                    "the name is not valid UTF-8.",
                    err=True,
                )
                continue
            entry = _normalize_client_entry(raw)
            if entry is None or _is_self_reference(entry):
                continue
            bad_field = unencodable_field(entry)
            if bad_field is not None:
                # Same reasoning as the name above, for the fields that get
                # spawned or dialled rather than keyed on. Named, never
                # echoed: a host entry's env values are its secrets.
                click.echo(
                    f"{_warn('Note:')} skipping {name!r} from {spec.label} — "
                    f"{bad_field} is not valid UTF-8 (value withheld).",
                    err=True,
                )
                continue
            source_ref: dict[str, Any] = {"kind": spec.kind}
            if src_path is not None:
                source_ref["path"] = src_path
            if name in seen:
                seen[name].setdefault("duplicate_in", []).append(spec.label)
                seen[name].setdefault("duplicates", []).append(
                    {"label": spec.label, "source_ref": source_ref, "raw": copy.deepcopy(raw)}
                )
                continue
            seen[name] = {
                "name": name,
                "source": spec.label,
                "entry": entry,
                "raw": copy.deepcopy(raw),
                "source_ref": source_ref,
            }
    return list(seen.values())


def _build_origin(cand: dict[str, Any], imported_at: str) -> dict[str, Any] | None:
    """Build the per-entry ``origin`` provenance block for an import (#475).

    Returns ``None`` when the candidate carries no verbatim ``raw`` /
    ``source_ref`` (hand-constructed candidates) — a partial block could not
    drive a faithful restore, so none is written.

    Construction goes through the ``UpstreamOrigin`` pydantic model so the
    shape the CLI writes cannot drift from the schema the server documents
    (``proxy/config.py``). ``pruned`` / ``pruned_at`` stay at their defaults
    here; the prune writers update them per source (PR2 of #475).
    """
    raw = cand.get("raw")
    source_ref = cand.get("source_ref")
    if not isinstance(raw, dict) or not isinstance(source_ref, dict):
        return None

    # Deferred import: ``proxy.config`` materializes the full pydantic model
    # tree, which only the two import commands need — keep it off the CLI's
    # cold-start path.
    from memtomem_stm.proxy.config import OriginSource, UpstreamOrigin

    origin = UpstreamOrigin(
        source=OriginSource(**source_ref),
        duplicates=[
            OriginSource(**dup["source_ref"])
            for dup in cand.get("duplicates") or []
            if isinstance(dup.get("source_ref"), dict)
        ],
        imported_at=imported_at,
        original=raw,
    )
    return origin.model_dump(mode="json", exclude_none=True)


def _format_candidate_detail(entry: dict[str, Any]) -> str:
    """One-line ``[transport] command/url`` cell for a discovered candidate.

    Display-escaped as a whole (#755): every caller prints the result into
    a terminal — a preview list, a picker title, a prune row — and none
    puts it in a ``--json`` payload or styles it, so there is no escape
    sequence of ours inside the string to protect. The values are another
    client's config, not STM's, so they never passed ``add``'s validation.
    """
    transport = entry.get("transport", "stdio")
    if transport == "stdio":
        parts = [entry.get("command", "")]
        parts.extend(entry.get("args", []))
        return _disp(f"[stdio] {' '.join(p for p in parts if p).strip()}")
    return _disp(f"[{transport}] {entry.get('url', '')}")


def _source_removal_hint(name: str, source: str) -> str:
    """One informational shell-line telling the user how to remove a server
    from the client that originated it.

    STM never edits source client configs itself — the hint is copy-paste
    only. Scope-label mapping comes from ``_SOURCE_SPECS`` and matches the
    ``claude mcp remove -s`` flag: ``Claude Code (project)`` is the
    per-project entry in ``~/.claude.json``, which the Claude Code CLI calls
    ``local``.

    The two ``#`` branches are prose, not commands, so ``_shell_join`` never
    sees them — their config-derived values are display-escaped by ``_disp``
    instead (#754). ``_desktop_config_path()`` is platform state, not config,
    and stays as-is.
    """
    spec = _SOURCE_BY_LABEL.get(source)
    if spec is None:
        # Both values are untrusted here: reaching this branch means ``source``
        # was not one of the known labels either.
        return f"# Remove '{_disp(name)}' from {_disp(source)}."
    if spec.claude_scope is not None:
        return _shell_join(["claude", "mcp", "remove", name, "-s", spec.claude_scope])
    return f"# Edit {_desktop_config_path()} and remove '{_disp(name)}' under mcpServers."


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


def _claude_mcp_remove(name: str, scope: str, cwd: str | None = None) -> tuple[bool, str | None]:
    """Shell out to ``claude mcp remove <name> -s <scope>``.

    Returns ``(ok, error_message_or_None)``. Treats any non-zero exit or
    subprocess error as a non-fatal failure so the caller can surface the
    manual command instead of aborting the import.

    ``cwd`` matters for ``-s local``: the claude CLI picks the
    ``~/.claude.json`` project slot from the process cwd, so a remove run
    from the wrong directory deletes a same-named entry from the wrong
    project (or nothing). Prune leaves it unset because its discovery
    matched the current cwd by construction; eject passes the recorded
    origin path (#475 PR3).
    """
    try:
        result = _run_claude_mcp(["claude", "mcp", "remove", name, "-s", scope], timeout=5, cwd=cwd)
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
        atomic_write_text(
            path, _json_dumps(data, indent=2, ensure_ascii=False) + "\n", durable=True
        )
    except OSError as exc:
        return (False, f"write error: {exc}")
    return (True, None)


def _prune_from_source(name: str, source: str) -> tuple[bool, str | None]:
    """Dispatch the prune action per source label used by ``_discover_candidates``.

    Scope-label mapping comes from the same ``_SOURCE_SPECS`` table as
    :func:`_source_removal_hint` — the remediation hint and the prune writer
    cannot disagree on which ``claude mcp remove -s <scope>`` flag maps to
    which label. Unknown sources are refused rather than guessed.
    """
    spec = _SOURCE_BY_LABEL.get(source)
    if spec is None:
        return (False, f"unknown source label: {source}")
    if spec.claude_scope is not None:
        return _claude_mcp_remove(name, spec.claude_scope)
    return _desktop_json_remove_entry(name)


_PRUNED_BACKUP_SCHEMA_VERSION = 1


def _pruned_backup_path() -> Path:
    """Append-only backup log of pruned host originals (#475 PR2).

    Deliberately a fixed user-wide path rather than ``--config``-relative:
    the log records host-side deletions, which exist independently of
    whichever proxy config drove them. Resolved at call time so
    monkeypatched ``HOME`` works in tests (mirrors ``state.mms_home``).
    """
    return Path.home() / ".memtomem" / "pruned_upstreams.json"


def _append_pruned_backup(
    name: str, source_ref: dict[str, Any], original: dict[str, Any], pruned_at: str
) -> tuple[bool, str | None]:
    """Append one verbatim host entry to the prune backup log.

    Runs BEFORE the host delete (backup-before-delete): a crash between the
    two leaves a stale row — harmless, the log is advisory — while the
    reverse order would destroy the only verbatim copy of an entry that has
    no ``origin`` block (pre-#475 imports, manual adds). Returns
    ``(ok, error_message_or_None)``; on failure the caller must SKIP the
    delete for the same reason.

    A corrupt or wrong-shape existing log refuses the append rather than
    clobbering it — the log is a last-resort recovery source, so destroying
    prior rows to record a new one would invert its purpose. Rows are never
    deduped or rewritten: it is a chronological event record.
    """
    path = _pruned_backup_path()
    data: dict[str, Any] = {"schema_version": _PRUNED_BACKUP_SCHEMA_VERSION, "entries": []}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return (False, f"backup log {path} is unreadable ({exc}) — move it aside and retry")
        if not isinstance(loaded, dict) or not isinstance(loaded.get("entries"), list):
            return (
                False,
                f"backup log {path} has an unexpected shape — move it aside and retry",
            )
        data = loaded
    data["entries"].append(
        {
            "name": name,
            "source": source_ref,
            "original": original,
            "pruned_at": pruned_at,
        }
    )
    # 0600 like ``stm_proxy.json`` (``_save``): ``original`` carries verbatim
    # host env/headers, secrets included.
    try:
        atomic_write_text(
            path, _json_dumps(data, indent=2, ensure_ascii=False) + "\n", mode=0o600, durable=True
        )
    except OSError as exc:
        return (False, f"backup log write failed: {exc}")
    return (True, None)


def _candidate_source_records(
    cand: dict[str, Any],
) -> list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]]:
    """``(label, source_ref, raw)`` for every source registering this candidate.

    Primary source first, then ``duplicate_in`` order — the exact label
    sequence the prune loop has always acted on (pinned by
    ``test_duplicate_in_sources_all_pruned``). ``source_ref`` / ``raw``
    come from the structured ``duplicates`` records discovery captures
    (#475 PR1); both are ``None`` for old-shape candidates, where there is
    nothing verbatim to back up or match against an origin row.
    """
    records: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]] = [
        (cand["source"], cand.get("source_ref"), cand.get("raw"))
    ]
    by_label = {
        dup.get("label"): dup for dup in cand.get("duplicates") or [] if isinstance(dup, dict)
    }
    for label in cand.get("duplicate_in") or []:
        dup = by_label.get(label) or {}
        records.append((label, dup.get("source_ref"), dup.get("raw")))
    return records


def _prune_imported_candidates(
    imported_candidates: list[dict[str, Any]],
    *,
    pruned_at: str,
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """Prune each imported candidate from every source that registered it.

    A single candidate can show up in multiple sources (primary ``source``
    plus ``duplicate_in``) — all are pruned so the dual-path collapses.
    Per source, the verbatim host entry is appended to the backup log
    *before* the delete runs (see :func:`_append_pruned_backup` for the
    ordering rationale); an append failure fails that source's prune
    outright and the delete is skipped.

    Returns ``(pruned, failed)`` where ``pruned`` is ``[(name, source), ...]``
    and ``failed`` is ``[(name, source, error), ...]``.
    """
    pruned: list[tuple[str, str]] = []
    failed: list[tuple[str, str, str]] = []
    for cand in imported_candidates:
        for src, source_ref, raw in _candidate_source_records(cand):
            if isinstance(raw, dict) and isinstance(source_ref, dict):
                ok, err = _append_pruned_backup(cand["name"], source_ref, raw, pruned_at)
                if not ok:
                    failed.append((cand["name"], src, err or "backup log append failed"))
                    continue
            ok, err = _prune_from_source(cand["name"], src)
            if ok:
                pruned.append((cand["name"], src))
            else:
                failed.append((cand["name"], src, err or "unknown error"))
    return pruned, failed


def _mark_pruned_sources(
    servers: dict[str, Any],
    candidates: list[dict[str, Any]],
    pruned: list[tuple[str, str]],
    pruned_at: str,
) -> bool:
    """Flip per-source ``origin`` ``pruned``/``pruned_at`` for successful prunes.

    Matches each pruned ``(name, source-label)`` against the entry's
    ``origin.source`` / ``origin.duplicates`` rows by structured
    ``(kind, path)`` — not by label, which is a human string the origin
    block deliberately doesn't store. Prune permits partial failure across
    sources, so only rows whose writer succeeded flip; a failed duplicate
    keeps ``pruned: false`` and stays visible to ``mms eject`` (PR3).
    Returns ``True`` when anything changed so the caller knows to save.
    """
    refs: dict[tuple[str, str], dict[str, Any]] = {}
    for cand in candidates:
        for label, source_ref, _raw in _candidate_source_records(cand):
            if isinstance(source_ref, dict):
                refs[(cand["name"], label)] = source_ref
    changed = False
    for name, label in pruned:
        ref = refs.get((name, label))
        if ref is None:
            continue
        entry = servers.get(name)
        origin = entry.get("origin") if isinstance(entry, dict) else None
        if not isinstance(origin, dict):
            continue
        for row in (origin.get("source"), *(origin.get("duplicates") or [])):
            if not isinstance(row, dict):
                continue
            if row.get("kind") != ref.get("kind") or row.get("path") != ref.get("path"):
                continue
            if not row.get("pruned"):
                row["pruned"] = True
                row["pruned_at"] = pruned_at
                changed = True
    return changed


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
            click.echo(f"    {_disp(cand['name'])} — {src}")
    return click.confirm("Remove from source(s)?", default=False)


def _handle_source_prune(
    imported_candidates: list[dict[str, Any]],
    *,
    prune: bool,
    config_path: Path,
    data: dict[str, Any],
    interactive: bool = True,
    quiet: bool = False,
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """Post-import prune + reporting. Returns ``(pruned, failed)``.

    Three paths based on the caller's ``prune`` flag and the TTY gate:

    * ``prune=True`` → prune unconditionally.
    * ``prune=False`` + TTY → interactive prompt, default No.
    * ``prune=False`` + non-TTY → skip prune entirely, fall through to the
      #203 hint-only warning.

    ``interactive=False`` (``add --from-clients --all/--select``, #817)
    suppresses the prompt even on a TTY, so a scripted import never blocks:
    there, ``--prune`` is the only way to consent. ``quiet`` is the
    ``--json`` mode — it drops the stdout hint block and success report,
    which the returned pairs carry into the payload instead; stderr failure
    diagnostics still print.

    Partial failures (e.g. one source prunes, another fails) surface a
    per-entry warning plus the manual-command fallback for the failed rows
    only, so the user always has a path forward even if the writer is
    degraded.

    ``config_path`` / ``data`` are the already-saved import — callers MUST
    have saved the origin-bearing entries before invoking this
    (save-import-first, #475): ① import saved with ``pruned: false`` →
    ② backup append + host delete per source → ③ the per-source ``pruned``
    metadata save below. Any crash point leaves the entry restorable; even
    a failed ③ only loses the metadata flip, never the origin or backup.
    """
    if not imported_candidates:
        return [], []

    if prune:
        should_prune = True
    elif interactive and _should_use_tui():
        should_prune = _confirm_prune_prompt(imported_candidates)
    else:
        should_prune = False

    if not should_prune:
        if not quiet:
            _print_source_removal_hints(imported_candidates)
        return [], []

    pruned_at = utc_now_iso()
    pruned, failed = _prune_imported_candidates(imported_candidates, pruned_at=pruned_at)
    servers = data.get("upstream_servers")
    if isinstance(servers, dict) and _mark_pruned_sources(
        servers, imported_candidates, pruned, pruned_at
    ):
        _save(config_path, data)
    _report_prune_results(pruned, failed, quiet_success=quiet)
    return pruned, failed


def _report_prune_results(
    pruned: list[tuple[str, str]],
    failed: list[tuple[str, str, str]],
    *,
    quiet_success: bool = False,
) -> bool:
    """Print the prune outcome — shared by ``_handle_source_prune`` and the
    ``prune`` command so the operator-facing wording cannot drift between
    them. Returns ``True`` when any removal failed (the command path exits
    non-zero on that). ``quiet_success`` (the ``--json`` path) skips the
    stdout success block — the JSON payload carries it — while the stderr
    failure diagnostics still print."""
    if pruned and not quiet_success:
        click.echo("")
        click.echo(f"{_ok('Removed from source client(s):')}")
        for name, src in pruned:
            click.echo(f"  {_disp(name)} — {src}")

    if failed:
        # Visual separator: stdout in human mode, but it must not precede
        # the JSON document in --json mode — stdout carries exactly one
        # JSON object there (json.loads tolerates leading whitespace, so a
        # naive parse test would not catch this).
        click.echo("", err=quiet_success)
        click.echo(
            f"{_warn('Warning:')} could not remove {len(failed)} direct registration(s):",
            err=True,
        )
        for name, src, err in failed:
            # ``err`` is whatever the host client's CLI wrote to stderr —
            # arbitrary text from a subprocess, not a value from our config.
            click.echo(f"  {_disp(name)} ({src}): {_disp(err)}", err=True)
        click.echo("", err=True)
        click.echo("Run the following to remove them manually:", err=True)
        for name, src, _ in failed:
            click.echo(f"  {_source_removal_hint(name, src)}", err=True)
    return bool(failed)


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
    """Derive a default prefix from a server name.

    Always satisfies :func:`prefixes.prefix_format_error` — the
    non-interactive import path (``add --from-clients --all/--select``)
    saves the suggestion with no operator to re-prompt, so an invalid one
    would strand the user with a config the runtime refuses to load.
    Underscore runs collapse with ``_{2,}`` rather than a single
    ``replace("__", "_")`` pass, which leaves ``__`` behind on odd-length
    runs (``a___b`` → ``a__b``). Avoids clashing with prefixes already
    chosen in the current run.
    """
    base = re.sub(r"[^a-zA-Z0-9_]", "_", name).strip("_") or "srv"
    if not base[0].isalpha():
        base = "s_" + base
    base = re.sub(r"_{2,}", "_", base)
    candidate = base
    n = 2
    while candidate in taken:
        candidate = f"{base}{n}"
        n += 1
    return candidate


def _auto_prefix(name: str, taken: set[str], warnings: list[str]) -> str:
    """Prefix for the non-interactive import path — no prompt, no re-prompt.

    The interactive flow leans on :func:`_prompt_prefix`'s loop to catch a
    suggestion that is too long for the ``<client>__<prefix>__<tool>`` tool
    name budget (#261); with nobody at the keyboard the same rule has to
    hold up front, so an over-budget suggestion is truncated rather than
    saved or refused. Truncation happens on the *base*, before the
    collision suffix is re-applied, so the shortened value stays unique.
    """
    hard_limit = tool_name_budget.prefix_hard_limit()
    prefix = _suggest_prefix(name, taken)
    if len(prefix) > hard_limit:
        base = _suggest_prefix(name, set())[: max(hard_limit - 2, 1)].rstrip("_") or "srv"
        prefix = _suggest_prefix(base, taken)
    warn_at = tool_name_budget.prefix_warn_threshold()
    if len(prefix) > warn_at:
        max_tool = tool_name_budget.TOOL_NAME_LIMIT - tool_name_budget.overhead() - len(prefix)
        warn_msg = (
            f"prefix '{prefix}' ({len(prefix)} chars) leaves only {max_tool} chars for "
            "upstream tool names — longer ones will be silently dropped by clients. "
            "Re-register the server with a shorter --prefix, or register STM as 'mms' "
            "in your client config. Proceeding."
        )
        click.echo(f"{_warn('Warning:')} {warn_msg}", err=True)
        warnings.append(warn_msg)
    return prefix


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
                f"{marker}  {_disp(c['name']):<18}  "
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
    select_all: bool = False,
    select_names: tuple[str, ...] = (),
    as_json: bool = False,
) -> None:
    """Bulk-import path for ``mms add --from-clients``.

    Reuses init's discovery + selection + prefix-prompt helpers. Difference
    from init: merges into an existing config (rather than creating one) and
    filters out candidates that match servers already registered, so the
    selection only shows *new* options.

    ``select_all`` / ``select_names`` (#817) replace both prompts with a
    scripted contract: the selection comes from the flag, each prefix from
    :func:`_auto_prefix`. That mode never prompts — not for the selection,
    not for a prefix, not for the post-import prune — even on a TTY, because
    a caller who named the servers on the command line has nobody waiting to
    answer. ``as_json`` then serializes the outcome; there every human line
    goes to stderr so stdout carries exactly one document.
    """
    noninteractive = select_all or bool(select_names)
    resolved = config_path.expanduser().resolve()

    def info(message: str = "") -> None:
        """Human-facing line: stdout normally, stderr under ``--json``."""
        click.echo(message, err=as_json)

    def emit(
        imported_rows: list[dict[str, Any]],
        skipped: list[dict[str, str]],
        warnings: list[str],
        prune_result: dict[str, Any] | None = None,
    ) -> None:
        if not as_json:
            return
        _echo_json(
            {
                "action": "add",
                "ok": True,
                "mode": "from_clients",
                "config_path": str(resolved),
                "imported": imported_rows,
                "skipped": skipped,
                "validated": validate,
                "warnings": warnings,
                "prune": prune_result,
            }
        )

    data = _load(config_path)
    servers: dict[str, Any] = data.setdefault("upstream_servers", {})
    skipped: list[dict[str, str]] = []
    warnings_json: list[str] = []

    all_candidates = _discover_candidates(Path.cwd())

    if select_names:
        # Resolve the requested names against *everything* discovered, not
        # just the importable remainder: a name no client advertises is a
        # typo, and a typo must not half-succeed by importing the names that
        # did resolve. Checked before any write.
        discovered = {cand["name"] for cand in all_candidates}
        unknown = [n for n in dict.fromkeys(select_names) if n not in discovered]
        if unknown:
            available = sorted(discovered)
            msg = (
                f"--select named server(s) no MCP client advertises: {', '.join(unknown)}. "
                f"Discovered: {', '.join(available) if available else '(none)'}"
            )
            click.echo(f"{_err('Error:')} {_disp(msg)}", err=True)
            if as_json:
                _json_fail("add", "unknown_server", msg, unknown=unknown, available=available)
            sys.exit(1)
        requested = set(select_names)
        all_candidates = [cand for cand in all_candidates if cand["name"] in requested]

    if not all_candidates:
        info("No MCP servers found in Claude Desktop / Code / .mcp.json.")
        info("Use `mms add <NAME> --prefix ... --command ...` to register one manually.")
        emit([], skipped, warnings_json)
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
                f"  {_warn('Skipping:')} '{_disp(cand_name)}' — already registered.",
                err=True,
            )
            skipped.append({"name": cand_name, "reason": "already_registered"})
            continue
        sig = _server_signature(entry)
        if sig is not None and sig in existing_signatures:
            click.echo(
                f"  {_warn('Skipping:')} '{_disp(cand_name)}' — matches an existing server "
                "by command/url.",
                err=True,
            )
            skipped.append({"name": cand_name, "reason": "duplicate_signature"})
            continue
        new_candidates.append(cand)

    if not new_candidates:
        # Everything requested is already registered. Not an error: re-running
        # the same scripted import has to stay idempotent.
        info("All discovered servers are already registered.")
        emit([], skipped, warnings_json)
        return

    if noninteractive:
        picks = list(range(len(new_candidates)))
    else:
        click.echo(_hdr(f"Found {len(new_candidates)} new MCP server(s) to import:"))
        for i, cand in enumerate(new_candidates, 1):
            dup = cand.get("duplicate_in")
            dup_hint = f"  (also in: {', '.join(dup)})" if dup else ""
            click.echo(
                f"  {i:>2}. {_disp(cand['name']):<18} {_format_candidate_detail(cand['entry'])}"
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
    imported_rows: list[dict[str, Any]] = []
    imported_at = utc_now_iso()
    info("")
    for idx in picks:
        cand = new_candidates[idx]
        if noninteractive:
            prefix = _auto_prefix(cand["name"], used_prefixes, warnings_json)
        else:
            # Escape the value, never the styled result: ``_hdr`` wraps this
            # line in a bold SGR span whose own escapes must stay real (#755).
            click.echo(_hdr(f"Configuring '{_disp(cand['name'])}'"))
            suggested = _suggest_prefix(cand["name"], used_prefixes)
            prefix = _prompt_prefix(default=suggested, taken=used_prefixes)
        used_prefixes.add(prefix)
        entry = {"prefix": prefix, **cand["entry"]}
        origin = _build_origin(cand, imported_at)
        if origin is not None:
            entry["origin"] = origin
        imported[cand["name"]] = entry
        imported_rows.append(
            {"name": cand["name"], "prefix": prefix, "source": cand["source"], "server": None}
        )

    if validate:
        info("")
        info(f"Validating {len(imported)} server(s) (timeout={validate_timeout}s)...")
        probes = asyncio.run(_probe_servers(imported, validate_timeout))
        rows_by_name = {row["name"]: row for row in imported_rows}
        for n, probe in probes.items():
            rows_by_name[n]["reachable"] = probe.connected
            rows_by_name[n]["tools_reachable"] = probe.tools if probe.connected else None
            if probe.connected:
                info(f"  {_ok('Reachable:')} {_disp(n)} — {probe.tools} tool(s).")
            else:
                # One message string for both surfaces so the stderr wording
                # and the --json ``warnings`` entry cannot fork.
                warn_msg = f"{n} — probe failed: {probe.error or ''}"
                warnings_json.append(warn_msg)
                click.echo(f"  {_warn('Warning:')} {_disp(warn_msg)}", err=True)
                click.echo("  Saving anyway. Run `mms health` later to retry.", err=True)

    servers.update(imported)
    _save(config_path, data)

    # Redacted like the manual `add --json` path: env/header values are
    # secret-bearing and --json output is routinely piped to logs.
    redacted = _redacted_servers_json(imported)
    for row in imported_rows:
        row["server"] = redacted[row["name"]]

    info("")
    info(f"{_ok('Added')} {len(imported)} server(s) to {resolved}:")
    for n, e in imported.items():
        info(f"  {_disp(n):<20} prefix={_disp(e['prefix'])}  {_format_candidate_detail(e)}")

    # Source clients still hold the direct registrations. Without a prune,
    # tools surface on two paths (client → upstream and client → STM →
    # upstream) and the direct path bypasses compression, caching, and LTM
    # surfacing. ``_handle_source_prune`` picks between the --prune flag,
    # the interactive prompt (TTY), and the hint-only fallback (non-TTY /
    # non-interactive selection / user declined).
    pruned, failed = _handle_source_prune(
        [new_candidates[i] for i in picks],
        prune=prune,
        config_path=config_path,
        data=data,
        interactive=not noninteractive,
        quiet=as_json,
    )
    emit(
        imported_rows,
        skipped,
        warnings_json,
        {
            "pruned": [{"name": n, "source": src} for n, src in pruned],
            "failed": [{"name": n, "source": src, "error": err} for n, src, err in failed],
        }
        if prune
        else None,
    )


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


def _confirm_validation() -> bool:
    """Confirm validation, taking the default only on piped stdin EOF.

    Click deliberately converts both ``EOFError`` and ``KeyboardInterrupt``
    into ``click.Abort``, so catching that exception cannot tell a closed pipe
    from Ctrl-C. Read non-TTY stdin directly to preserve interrupts while
    retaining the confirm's default-yes behavior at EOF.
    """
    if sys.stdin.isatty():
        return click.confirm("Validate connection(s) now?", default=True)

    while True:
        click.echo("Validate connection(s) now? [Y/n]: ", nl=False)
        answer = sys.stdin.readline()
        click.echo("")
        if answer == "":
            click.echo("No answer on stdin — validating by default (--no-validate to skip).")
            return True

        value = answer.strip().lower()
        if value in ("", "y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        click.echo("Error: invalid input")


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
    "--client",
    "client_mode",
    type=click.Choice(["auto", "claude", "codex", "json", "skip"]),
    default=None,
    help="Register with a detected client, Claude Code, Codex, JSON config, or skip.",
)
@click.option(
    "--resume",
    is_flag=True,
    help="Continue registration when the proxy config already exists.",
)
@click.option(
    "--demo",
    is_flag=True,
    help="Configure the bundled deterministic read-only demo server.",
)
@click.option(
    "--freshness",
    type=click.Choice(["live", "balanced", "reuse"]),
    default="balanced",
    show_default=True,
    help="Response-cache freshness preset.",
)
@click.option(
    "--allow-project-configs",
    is_flag=True,
    help="Acknowledge discovery of project-local MCP configurations.",
)
@click.option(
    "--replace-registration",
    is_flag=True,
    help="Replace an existing selected-client registration.",
)
@click.option(
    "--save-unverified",
    is_flag=True,
    help="Save even if an optional connection probe fails (current compatibility behavior).",
)
@click.option("--json", "as_json", is_flag=True, help="Output one JSON result document.")
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
        "chars_per_token=1.85 and default_max_result_chars=8500, and "
        "adds max_result_tokens=2000 + chars_per_token=1.85 to each "
        "imported server. Omit for the interactive prompt; defaults to "
        "'en' on non-TTY callers."
    ),
)
@with_config_write_lock(json_envelope=True)
@_setup_json_result("init")
def init(
    config_path: str,
    no_validate: bool,
    mcp_mode: str | None,
    client_mode: str | None,
    resume: bool,
    demo: bool,
    freshness: str,
    allow_project_configs: bool,
    replace_registration: bool,
    save_unverified: bool,
    prune_originals: bool,
    lang: str | None,
) -> None:
    """Guided first-time setup for memtomem-stm.

    Prompts for a single upstream server and writes the config file. Without
    ``--resume``, aborts when the config already exists; ``--resume`` preserves
    that config and continues client registration. Use ``mms add`` to append
    more servers.
    """
    path = Path(config_path)
    resolved = path.expanduser().resolve()

    if client_mode is not None and mcp_mode is not None:
        raise click.UsageError("use either --client or the legacy --mcp flag, not both")
    selected_client = client_mode or mcp_mode

    # ``--json`` still allows stdin-scripted prompts (#758 pins the discovery
    # selection flow), but prompts with a safe default must not read stdin:
    # with stdout captured the prompt text is invisible, so an unanswered
    # confirm hangs the caller. See the validate confirm and language preset
    # below, plus the decorator's ``click.Abort`` envelope for EOF.
    json_mode = _SETUP_JSON_MODE.get()

    if resolved.exists():
        if resume:
            click.echo(f"{_ok('Resuming setup with:')} {resolved}")
            _run_mcp_integration(
                client_mode=selected_client,
                config_path=resolved,
                replace_registration=replace_registration,
            )
            click.echo(f"Next: {_shell_join(['mms', 'doctor', '--config', str(resolved)])}")
            return
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

    candidates = [] if demo else _discover_candidates(Path.cwd())
    if not allow_project_configs:
        candidates = [
            cand for cand in candidates if str(cand.get("source", "")) != ".mcp.json (project)"
        ]
    imported: dict[str, dict[str, Any]] = {}
    # Parallel list of the source-client candidate dicts for entries we
    # actually import. Needed so the end-of-flow prune step can address the
    # exact ``(name, source, duplicate_in)`` triples via ``_handle_source_prune``.
    imported_candidates: list[dict[str, Any]] = []

    if demo:
        imported["demo"] = {
            "prefix": "demo",
            "transport": "stdio",
            "command": os.path.abspath(sys.executable),
            "args": ["-m", "memtomem_stm.demo_server"],
            "compression": "auto",
            "max_result_chars": 8000,
            "cache": True,
        }
        click.echo(f"{_ok('Using bundled read-only demo server.')} No network access required.")
    elif candidates:
        click.echo(_hdr(f"Found {len(candidates)} MCP server(s) in existing client configs:"))
        # TUI renders its own list; this preview exists so duplicate-source
        # notes ("also in: X") stay visible — those don't fit inside a
        # questionary Choice title and otherwise would be invisible.
        for i, cand in enumerate(candidates, 1):
            dup = cand.get("duplicate_in")
            dup_hint = f"  (also in: {', '.join(dup)})" if dup else ""
            click.echo(
                f"  {i:>2}. {_disp(cand['name']):<18} {_format_candidate_detail(cand['entry'])}"
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
        imported_at = utc_now_iso()
        for idx in picks:
            cand = candidates[idx]
            # Escape the value, never the styled result: ``_hdr`` wraps this
            # line in a bold SGR span whose own escapes must stay real (#755).
            click.echo(_hdr(f"Configuring '{_disp(cand['name'])}'"))
            suggested = _suggest_prefix(cand["name"], used_prefixes)
            prefix = _prompt_prefix(default=suggested, taken=used_prefixes)
            used_prefixes.add(prefix)
            entry = {"prefix": prefix, **cand["entry"]}
            origin = _build_origin(cand, imported_at)
            if origin is not None:
                entry["origin"] = origin
            imported[cand["name"]] = entry
            imported_candidates.append(cand)
    else:
        # Zero discovered candidates → true first-time user without any
        # existing MCP-client config. Full manual prompt flow.
        name = click.prompt("Server name (e.g. 'filesystem', 'github')", type=str).strip()
        if not name:
            click.echo(f"{_err('Error:')} server name must be non-empty.", err=True)
            sys.exit(1)
        if has_lone_surrogate(name):
            click.echo(f"{_err('Error:')} server name {name!r} is not valid UTF-8.", err=True)
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

        # Third create path, same gate as `add` and the discovery scan: the
        # name check above does not cover the command or URL typed after it,
        # and this entry goes straight into the config that gets written.
        bad_field = unencodable_field(entry)
        if bad_field is not None:
            click.echo(
                f"{_err('Error:')} {bad_field} is not valid UTF-8 (value withheld).", err=True
            )
            sys.exit(1)

        imported[name] = entry

    # Resolve the language preset before validate so the prompt sequence
    # stays predictable for scripted callers ("which prompt fires next?").
    # Non-TTY callers without ``--lang`` get "en" silently — see
    # ``_prompt_language``.
    selected_lang = lang if lang is not None else ("en" if json_mode else _prompt_language())
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

    do_validate = not no_validate and (json_mode or _confirm_validation())
    if do_validate:
        probe_map = {n: e for n, e in imported.items()}
        click.echo(f"Validating {len(probe_map)} server(s) (timeout=10s)...")
        probes = asyncio.run(_probe_servers(probe_map, 10))
        for n, probe in probes.items():
            if probe.connected:
                click.echo(f"  {_ok('Reachable:')} {_disp(n)} — {probe.tools} tool(s).")
            else:
                click.echo(
                    f"  {_warn('Warning:')} {_disp(n)} — probe failed: {_disp(probe.error or '')}",
                    err=True,
                )
                suffix = " (--save-unverified acknowledged)" if save_unverified else ""
                click.echo(
                    f"  Saving config anyway{suffix}. Run `mms health` later to retry.",
                    err=True,
                )

    cache_block = _new_config_cache_block()
    if freshness != "balanced":
        cache_block["default_ttl_seconds"] = {"live": 0, "reuse": 86400}[freshness]

    data: dict[str, Any] = {
        "enabled": True,
        "cache": cache_block,
        **proxy_fields,
        "upstream_servers": imported,
    }
    _save(path, data)

    click.echo("")
    click.echo(f"{_ok('Saved to:')} {resolved}")
    click.echo("")
    click.echo(_hdr("Configured upstream servers:"))
    for n, e in imported.items():
        click.echo(f"  {_disp(n):<20} prefix={_disp(e['prefix'])}  {_format_candidate_detail(e)}")

    # Non-default ``--config`` paths: surface the flag in the management
    # hints so ``mms list`` / ``mms health`` don't silently read the empty
    # default config and confuse the user (reported after first dogfooding
    # with a throwaway ``/tmp/*.json`` test path).
    if resolved != _DEFAULT_CONFIG.expanduser().resolve():
        click.echo("")
        click.echo(f"  {_hdr('Manage this config:')}")
        click.echo(f"    {_shell_join(['mms', 'list', '--config', str(resolved)])}")
        click.echo(f"    {_shell_join(['mms', 'health', '--config', str(resolved)])}")
        click.echo(f"    {_shell_join(['mms', 'add', '--import', '--config', str(resolved)])}")
        click.echo(
            "    New client registrations carry this config path via "
            "MEMTOMEM_STM_PROXY__CONFIG_PATH."
        )

    click.echo("")
    _run_mcp_integration(
        _MCP_MODE_TO_CHOICE.get(mcp_mode) if mcp_mode else None,
        client_mode=client_mode,
        config_path=resolved,
        replace_registration=replace_registration,
    )

    # Post-registration prune of source-client originals, opt-in only.
    # * ``--prune-originals`` → unconditional prune (scripted path).
    # * TTY + no flag → single y/N confirm, default No.
    # * non-TTY + no flag → skip; fall through to the #203 hint-only warning.
    # Matches ``mms add --import --prune`` semantics via ``_handle_source_prune``.
    if imported_candidates:
        _handle_source_prune(
            imported_candidates, prune=prune_originals, config_path=path, data=data
        )


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
@click.option(
    "--client",
    "client_mode",
    type=click.Choice(["auto", "claude", "codex", "json", "skip"]),
    default=None,
    help="Register with a detected client, Claude Code, Codex, JSON config, or skip.",
)
@click.option(
    "--replace-registration",
    is_flag=True,
    help="Replace an existing selected-client registration.",
)
@click.option("--json", "as_json", is_flag=True, help="Output one JSON result document.")
@_setup_json_result("register")
def register(
    config_path: str,
    mcp_mode: str | None,
    client_mode: str | None,
    replace_registration: bool,
) -> None:
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
    if client_mode is not None and mcp_mode is not None:
        raise click.UsageError("use either --client or the legacy --mcp flag, not both")
    if not resolved.exists():
        click.echo(
            f"{_err('Error:')} config not found at {resolved}.",
            err=True,
        )
        click.echo("  Run `mms init` first.", err=True)
        sys.exit(1)
    _run_mcp_integration(
        _MCP_MODE_TO_CHOICE.get(mcp_mode) if mcp_mode else None,
        client_mode=client_mode,
        config_path=resolved,
        replace_registration=replace_registration,
    )


def _remove_eject_hint(name: str, entry: Any, resolved: Path) -> str | None:
    """Eject hint printed before removing an imported entry (#475 PR4).

    Fires only when the recorded provenance says removal would leave the
    server registered **nowhere** — :func:`_origin_fully_pruned`, the same
    predicate behind the ``mms list`` pruned marker. Entries without an
    ``origin`` block (manual ``mms add``, pre-#475 imports) and entries
    some host still registers get no hint — removing those just stops
    proxying.

    The rendered ``mms eject`` command names the *active* ``--config``
    (``resolved``, the config being removed from) so pasting it doesn't
    silently target the default ``~/.memtomem/stm_proxy.json`` (#746), and
    guards ``name`` with a ``--`` end-of-options terminator so a
    leading-dash server name pastes as a positional rather than an option.

    Advisory only: the flags are as recorded at prune time (a manually
    re-added host entry isn't detected), and ``mms eject``'s live
    no-clobber check stays the authoritative guard. The removal itself is
    never blocked — the hint precedes the confirm prompt so a TTY user can
    still abort.

    The two halves of the hint act on one character class — the one
    ``_disp_escapes`` defines — but differently: ``_shell_join`` refuses the
    whole runnable ``mms eject`` command (#751/#752, widened by #754), while
    the surrounding prose keeps rendering with its config-derived values
    escaped in place by ``_disp``.
    """
    if not isinstance(entry, dict):
        return None
    origin = entry.get("origin")
    if not _origin_fully_pruned(origin):
        return None
    if not isinstance(origin, dict):  # unreachable — narrows for mypy
        return None
    source = origin.get("source")
    if not isinstance(source, dict):  # unreachable — narrows for mypy
        return None
    kind = source.get("kind")
    spec = _SOURCE_BY_KIND.get(kind) if isinstance(kind, str) else None
    # ``label`` is tainted too when the kind is unrecognized: it then falls
    # through to the recorded string verbatim, the same display-what-was-stored
    # policy ``_origin_cell`` documents. ``_disp`` is identity on the app-owned
    # labels, so it can wrap both branches unconditionally.
    label = spec.label if spec else (kind if isinstance(kind, str) else "its host client")
    return (
        f"{_warn('Note:')} '{_disp(name)}' was imported from {_disp(label)} and the host original "
        "was pruned —\n"
        "  removing it from STM leaves it registered nowhere.\n"
        "  To restore it to the host instead, run: "
        f"{_shell_join(['mms', 'eject', '--config', str(resolved), '--', name])}"
    )


@cli.command()
@click.argument("name")
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Output as JSON for scripting (requires --yes).",
)
@with_config_write_lock(json_envelope=True)
def remove(name: str, config_path: str, yes: bool, as_json: bool = False) -> None:
    """Remove an upstream MCP server from the proxy configuration."""
    path = Path(config_path)
    # Missing-config guard matching prune/register: _load returns the default
    # empty config for a missing file, which would misreport a wrong --config
    # path as "server not found" instead of "config not found".
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        click.echo(f"{_err('Error:')} config not found at {resolved}.", err=True)
        click.echo("  Run `mms init` first.", err=True)
        if as_json:
            _json_fail(
                "remove",
                "config_not_found",
                f"config not found at {resolved}",
                path=str(resolved),
            )
        sys.exit(1)
    data = _load(path)
    servers: dict[str, Any] = data.get("upstream_servers", {})

    if name not in servers:
        # Terminal echo only: the JSON leg's values stay raw — ``json.dumps``
        # escapes control characters itself, and the ``name`` field is
        # machine-readable, not display (#754).
        click.echo(f"{_err('Error:')} server '{_disp(name)}' not found.", err=True)
        if as_json:
            _json_fail("remove", "server_not_found", f"server '{name}' not found", name=name)
        sys.exit(1)

    warnings: list[str] = []
    hint = _remove_eject_hint(name, servers[name], resolved)
    if hint:
        if as_json:
            # Same wording as the terminal hint; unstyle so the payload never
            # carries ANSI codes (click only strips them on echo, not dumps).
            warnings.append(click.unstyle(hint))
        else:
            click.echo(hint)

    if not yes:
        if as_json:
            _json_requires_yes("remove")
        # A CR here would overwrite the rendered ``[y/N]`` the user is answering.
        click.confirm(f"Remove server '{_disp(name)}'?", abort=True)

    del servers[name]
    _save(path, data)
    if as_json:
        _echo_json(
            {
                "action": "remove",
                "ok": True,
                "config_path": str(resolved),
                "name": name,
                "removed": True,
                "warnings": warnings,
            }
        )
        return
    click.echo(f"{_ok('Removed')} server '{_disp(name)}'.")


@cli.command()
@click.argument("name")
@click.argument("state", type=click.Choice(["on", "off"]), required=False)
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@with_config_write_lock(skip=lambda kwargs: kwargs.get("state") is None)
def surfacing(name: str, state: str | None, config_path: str) -> None:
    """Toggle proactive memory surfacing for an upstream server.

    \b
    mms surfacing <server>          # show current state
    mms surfacing <server> off      # disable surfacing for this upstream
    mms surfacing <server> on       # re-enable

    Writes ``surfacing_enabled`` into the upstream's entry in the proxy config
    (``stm_proxy.json``); a running proxy hot-reloads it without a restart, and
    `mms list` shows the effective state (SURFACING column). Because the flag
    lives in the shared
    config file rather than per-client env, every MCP client that proxies
    through this `mms` sees the same scope.

    For tool-grained or cross-server glob scope instead, set
    ``MEMTOMEM_STM_SURFACING__EXCLUDE_TOOLS`` (matches ``server__tool``).
    """
    path = Path(config_path)
    data = _load(path)
    servers: dict[str, Any] = data.get("upstream_servers", {})

    if name not in servers:
        click.echo(f"{_err('Error:')} server '{_disp(name)}' not found.", err=True)
        sys.exit(1)

    current = bool(servers[name].get("surfacing_enabled", True))
    if state is None:
        click.echo(f"surfacing for '{_disp(name)}': {'on' if current else 'off'}")
        return

    desired = state == "on"
    servers[name]["surfacing_enabled"] = desired
    _save(path, data)
    # After the write, so an unrenderable name must not raise here — that is
    # the mutate-then-crash shape #757 is about, and making `_save` succeed on
    # such a config is what newly exposed it (#758).
    click.echo(f"{_ok('Surfacing ' + state)} for '{_disp(name)}'.")


# ── tune command (#615) ────────────────────────────────────────────────
#
# CLI front-end for the CompressionTuner analysis behind the
# `stm_tuning_recommendations` MCP tool. The MCP tool renders a report that
# ends with "apply manually to stm_proxy.json"; this command closes that
# loop: preview the per-tool overrides the tuner suggests, then `--apply`
# writes the accepted ones under the config write lock, after a timestamped
# backup — a running proxy hot-reloads the file, so a bad apply is live
# immediately and one-command restore matters.


@dataclass(frozen=True)
class _TuneChange:
    """One ``(server, tool, field)`` override the tuner recommends writing."""

    server: str
    tool: str
    field: str  # "max_result_chars" | "compression" | "retention_floor"
    current: str | None  # effective current value, for display
    recommended: str  # recommended value, for display
    value: int | str | float  # typed value written into the config
    reason: str
    confidence: str


def _tune_typed_value(field: str, recommended: str) -> int | str | float:
    """Convert a ``TuningAction.recommended`` display string to the config type.

    The tuner emits strings uniformly (its output was designed for a rendered
    report); ``ToolOverrideConfig`` wants ``int`` for ``max_result_chars`` and
    ``float`` for ``retention_floor``. Raises ``ValueError`` on a
    non-numeric string so the caller can skip the action instead of writing
    a value the server would reject.
    """
    if field == "max_result_chars":
        return int(recommended)
    if field == "retention_floor":
        return float(recommended)
    return recommended


def _merge_tune_changes(
    recs: list[TuningRecommendation], raw_servers: dict[str, Any]
) -> tuple[list[_TuneChange], list[str]]:
    """Collapse duplicate ``(server, tool, field)`` actions into one change each.

    A single recommendation can carry colliding actions (see
    ``CompressionTuner._analyze_profile``): the strategy-pin heuristic and the
    feedback heuristic both emit ``compression``; a budget heuristic and the
    feedback heuristic both emit ``max_result_chars``. Policy per field:

    * ``compression`` — last-wins. The feedback-driven action is appended
      after the latency pin, and an explicit agent-reported deficiency should
      beat an optimization.
    * ``max_result_chars`` — numeric max. "Budget too small" signals combine
      as max; when the budget-savings heuristic collides with feedback asking
      for more room, feedback wins, which max also yields.
    * ``retention_floor`` — defensive last-wins (no current heuristic emits it).

    Recommendations for servers absent from the raw config file are split off
    as human-readable skip warnings: the metrics DB can carry rows for an
    env-only or since-renamed upstream that has no dict entry to write into.
    The same guard covers a raw entry that isn't writable — a non-dict server
    value, a non-dict ``tool_overrides``, or a non-dict per-tool override —
    because the *typed* validation the command runs sees the env-overlaid
    composite, and an env override for ``upstream_servers`` can mask a
    malformed file entry that ``_apply_tune_changes`` would then crash on.
    """
    merged: dict[tuple[str, str, str], _TuneChange] = {}
    merge_counts: dict[tuple[str, str, str], int] = {}
    skipped: list[str] = []
    skipped_servers: set[str] = set()

    for rec in recs:
        entry = raw_servers.get(rec.server)
        if not isinstance(entry, dict) or not isinstance(entry.get("tool_overrides", {}), dict):
            if rec.server not in skipped_servers:
                skipped_servers.add(rec.server)
                if entry is None:
                    detail = "is not in the config file (env-defined or renamed upstream)"
                else:
                    detail = "has a malformed entry in the config file"
                skipped.append(
                    f"{rec.server}/{rec.tool}: server '{rec.server}' {detail} — nothing to write"
                )
            continue
        existing_override = entry.get("tool_overrides", {}).get(rec.tool)
        if existing_override is not None and not isinstance(existing_override, dict):
            skipped.append(
                f"{rec.server}/{rec.tool}: existing tool_overrides entry is malformed "
                "in the config file — nothing to write"
            )
            continue
        for action in rec.actions:
            try:
                value = _tune_typed_value(action.field, action.recommended)
            except ValueError:
                skipped.append(
                    f"{rec.server}/{rec.tool}: unusable {action.field} recommendation "
                    f"{action.recommended!r} — skipped"
                )
                continue
            key = (rec.server, rec.tool, action.field)
            prior = merged.get(key)
            if prior is not None:
                merge_counts[key] += 1
                if (
                    action.field == "max_result_chars"
                    and isinstance(value, int)
                    and isinstance(prior.value, int)
                    and value <= prior.value
                ):
                    continue
            else:
                merge_counts[key] = 1
            merged[key] = _TuneChange(
                server=rec.server,
                tool=rec.tool,
                field=action.field,
                # The feedback heuristic emits ``current=None``; keep the first
                # action's resolved current so the diff still shows what the
                # override replaces.
                current=action.current if prior is None else (prior.current or action.current),
                recommended=action.recommended,
                value=value,
                reason=action.reason,
                confidence=rec.confidence,
            )

    changes: list[_TuneChange] = []
    for key, change in merged.items():
        if merge_counts[key] > 1:
            reason = f"{change.reason} (merged {merge_counts[key]} recommendations)"
            change = _TuneChange(
                server=change.server,
                tool=change.tool,
                field=change.field,
                current=change.current,
                recommended=change.recommended,
                value=change.value,
                reason=reason,
                confidence=change.confidence,
            )
        changes.append(change)
    return changes, skipped


def _tune_groups(
    changes: list[_TuneChange],
) -> list[tuple[tuple[str, str], list[_TuneChange]]]:
    """Group changes per ``(server, tool)`` — the selection granularity."""
    groups: dict[tuple[str, str], list[_TuneChange]] = {}
    for change in changes:
        groups.setdefault((change.server, change.tool), []).append(change)
    return list(groups.items())


def _pick_tune_tui(
    groups: list[tuple[tuple[str, str], list[_TuneChange]]],
) -> list[int] | None:
    """Enter-to-toggle select loop over per-tool recommendations.

    Same interaction model as :func:`_pick_imports_tui` (chosen over
    ``questionary.checkbox`` — see that docstring), but starts with **all**
    rows selected: the expected flow is "apply what the tuner found, minus
    exceptions". Returns sorted indices into ``groups``; ``None`` on Cancel
    (distinct from an empty Confirm, though callers treat both as abort).
    """
    import questionary

    picks: set[int] = set(range(len(groups)))
    cursor: object = 0

    while True:
        choices: list[Any] = []
        for i, ((server, tool), items) in enumerate(groups):
            marker = "[v]" if i in picks else "[ ]"
            fields = ", ".join(f"{c.field} -> {c.recommended}" for c in items)
            choices.append(
                questionary.Choice(
                    title=f"{marker}  {_disp(server)}/{_disp(tool)}  {_disp(fields)}", value=i
                )
            )
        choices.append(questionary.Separator())
        choices.append(
            questionary.Choice(
                title=f"Confirm — apply to {len(picks)} tool(s)",
                value=_TUI_CONFIRM,
            )
        )
        choices.append(questionary.Choice(title="Cancel", value=_TUI_CANCEL))

        result = questionary.select(
            "Select tools to tune (↑↓ or j/k to move, Enter toggles, scroll to Confirm):",
            choices=choices,
            default=cursor,  # type: ignore[arg-type]
            style=_tui_style(),
            use_arrow_keys=True,
            use_jk_keys=True,
            use_emacs_keys=True,
        ).ask()

        if result is None or result == _TUI_CANCEL:
            return None
        if result == _TUI_CONFIRM:
            return sorted(picks)
        cursor = result
        if result in picks:
            picks.remove(result)
        else:
            picks.add(result)


def _pick_tune_changes(changes: list[_TuneChange]) -> list[_TuneChange] | None:
    """Interactive per-tool selection. ``None`` means the user cancelled.

    TUI when available; otherwise (``MMS_NO_TUI``, or questionary failing on
    an incompatible terminal — same recovery as the init-wizard prompts) a
    sequential ``click.confirm`` per tool, default yes. Callers guarantee a
    TTY — the non-TTY case errors out before selection.
    """
    groups = _tune_groups(changes)
    if _should_use_tui():
        try:
            picks = _pick_tune_tui(groups)
        except Exception:
            pass  # degrade to plain confirms below
        else:
            if picks is None:
                return None
            return [c for i in picks for c in groups[i][1]]

    selected: list[_TuneChange] = []
    for (server, tool), items in groups:
        fields = ", ".join(c.field for c in items)
        # The prompt the user answers to authorize a config write: a CR in
        # the recorded tool name would overwrite the rendered ``[Y/n]``.
        if click.confirm(
            f"Apply to {_disp(server)}/{_disp(tool)} ({_disp(fields)})?", default=True
        ):
            selected.extend(items)
    return selected


def _render_tune_preview(
    changes: list[_TuneChange],
    skipped: list[str],
    *,
    since_hours: float,
    apply_hint: bool,
) -> None:
    groups = _tune_groups(changes)
    click.echo(_hdr(f"Tuning recommendations (last {since_hours:g}h): {len(groups)} tool(s)"))
    for (server, tool), items in groups:
        click.echo(f"  {_disp(server)}/{_disp(tool)}  [{items[0].confidence} confidence]")
        for change in items:
            current = change.current if change.current is not None else "(default)"
            click.echo(f"    {_disp(change.field)}: {current} -> {change.recommended}")
            click.echo(f"        {_disp(change.reason)}")
    for entry in skipped:
        click.echo(f"  {_warn('Skipped:')} {_disp(entry)}")
    if apply_hint:
        click.echo("")
        click.echo(
            f"{len(changes)} change(s) across {len(groups)} tool(s). "
            "Run with --apply to write them."
        )


def _tune_json_payload(
    changes: list[_TuneChange],
    skipped: list[str],
    *,
    resolved: Path,
    since_hours: float,
    tool_filter: str | None,
) -> dict[str, Any]:
    return {
        "config_path": str(resolved),
        "since_hours": since_hours,
        "tool_filter": tool_filter,
        "changes": [
            {
                "server": c.server,
                "tool": c.tool,
                "field": c.field,
                "current": c.current,
                "recommended": c.recommended,
                "reason": c.reason,
                "confidence": c.confidence,
            }
            for c in changes
        ],
        "skipped": skipped,
    }


def _backup_config_snapshot(resolved: Path, original_text: str) -> Path:
    """Snapshot the pre-apply config to a timestamped, non-clobbering slot.

    ``stm_proxy.json.bak-20260704T101530Z`` (``.1``, ``.2``, … on same-second
    collision), each slot claimed with ``O_CREAT | O_EXCL`` like
    ``hook_hosts._write_backup`` so two concurrent applies can't share one.
    Mode 0o600 — the config can carry upstream env secrets, same rationale
    as ``_save``. Restore is a plain ``cp`` back over the config; a running
    proxy hot-reloads it.
    """
    stamp = utc_now_iso().replace("-", "").replace(":", "")
    base = resolved.parent / f"{resolved.name}.bak-{stamp}"
    i = 0
    while True:
        candidate = base if i == 0 else base.with_name(f"{base.name}.{i}")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            i += 1
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(original_text)
            # O_EXCL honors umask, so the mode above may be narrowed; force it.
            try:
                candidate.chmod(0o600)
            except OSError:
                pass
        except Exception:
            candidate.unlink(missing_ok=True)
            raise
        return candidate


def _apply_tune_changes(data: dict[str, Any], selected: list[_TuneChange]) -> None:
    """Write selected overrides into the raw config dict (unknown keys untouched)."""
    servers = data.setdefault("upstream_servers", {})
    for change in selected:
        overrides = servers[change.server].setdefault("tool_overrides", {})
        overrides.setdefault(change.tool, {})[change.field] = change.value


@cli.command()
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option(
    "--apply",
    "do_apply",
    is_flag=True,
    help="Write the accepted overrides into the config (default: preview only).",
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Apply all recommendations without prompting (scripts / CI / non-TTY).",
)
@click.option(
    "--since-hours",
    "since_hours",
    type=float,
    default=24.0,
    show_default=True,
    help="Analysis window over the metrics/feedback stores.",
)
@click.option("--tool", "tool_filter", default=None, help="Filter to one upstream tool name.")
@click.option("--json", "as_json", is_flag=True, help="Preview as JSON for scripting.")
@with_config_write_lock(skip=lambda kwargs: not kwargs.get("do_apply"))
def tune(
    config_path: str,
    do_apply: bool,
    assume_yes: bool,
    since_hours: float,
    tool_filter: str | None,
    as_json: bool = False,
) -> None:
    """Preview and apply per-tool compression tuning recommendations.

    \b
    mms tune                 # preview what --apply would write
    mms tune --apply         # pick recommendations, write tool_overrides
    mms tune --apply --yes   # apply all, no prompts

    Runs the same analysis as the ``stm_tuning_recommendations`` MCP tool
    against the on-disk metrics/feedback stores (no running server needed)
    and shows the per-tool ``tool_overrides`` diff it suggests. ``--apply``
    writes the accepted overrides under the config write lock, after
    snapshotting the config to a timestamped ``.bak-<UTC>`` file next to it;
    restore is ``cp <backup> <config>``. A running proxy hot-reloads the
    result without a restart.

    Unlike ``mms stats`` (pure read-only summaries), this opens the stores
    read-write to run their idempotent schema migrations — the same step the
    server performs at startup — but only when the DB files already exist;
    a preview never creates anything.
    """
    from memtomem_stm.proxy.compression_feedback_store import CompressionFeedbackStore
    from memtomem_stm.proxy.config import ProxyConfig, collect_proxy_env_overrides
    from memtomem_stm.proxy.metrics_store import MetricsStore
    from memtomem_stm.proxy.tuner import CompressionTuner

    if as_json and do_apply:
        raise click.UsageError("--json is a preview format; it cannot be combined with --apply.")

    path = Path(config_path)
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        click.echo(f"{_err('Error:')} config not found at {resolved}.", err=True)
        click.echo("  Run `mms init` first.", err=True)
        sys.exit(1)

    original_text = resolved.read_text(encoding="utf-8")
    data = _load(resolved)

    # Typed load with the env overlay: the tuner resolves *effective* current
    # values (and the DB paths) the way the server does. A file that fails the
    # typed load is one a running server silently ignores (#611) — refuse to
    # write overrides into it.
    typed_cfg = ProxyConfig.load_from_file(
        path, env_overrides=collect_proxy_env_overrides(), missing_ok=False
    )
    if typed_cfg is None:
        click.echo(
            f"{_err('Error:')} config fails validation — run `mms config validate` "
            "and fix it before tuning.",
            err=True,
        )
        sys.exit(1)

    # Reaching here means the env-overlaid composite validated; the file AS
    # WRITTEN can still be invalid (an env var can replace a malformed
    # subtree). --apply edits the FILE, and its post-mutation validation
    # covers the whole raw dict — so a masked pre-existing defect would
    # abort the apply after all the analysis/selection work, or strand
    # healthy sibling changes behind it. Refuse (or warn, for a preview)
    # up front instead, pointing at the fix-it workflow.
    raw_error = _schema_validation_error(data)
    if raw_error is not None:
        if do_apply:
            click.echo(
                f"{_err('Error:')} the config file as written fails validation "
                f"({_disp(raw_error)}) — fix it (see `mms config validate`) before applying "
                "tuning overrides.",
                err=True,
            )
            sys.exit(1)
        click.echo(
            f"{_warn('Warning:')} the config file as written fails validation "
            f"({_disp(raw_error)}) — --apply will refuse until it is fixed "
            "(see `mms config validate`).",
            err=True,
        )

    metrics_path = typed_cfg.metrics.db_path.expanduser()
    if not metrics_path.exists():
        click.echo(f"No proxy metrics recorded yet at {metrics_path} — nothing to tune.")
        return
    feedback_path = typed_cfg.compression_feedback.db_path.expanduser()

    metrics_store = MetricsStore(metrics_path)
    feedback_store = CompressionFeedbackStore(feedback_path) if feedback_path.exists() else None
    try:
        metrics_store.initialize()
        if feedback_store is not None:
            feedback_store.initialize()
        tuner = CompressionTuner(metrics_store, feedback_store, config=typed_cfg)
        recs = tuner.analyze(since_seconds=since_hours * 3600.0, tool_filter=tool_filter)
    finally:
        metrics_store.close()
        if feedback_store is not None:
            feedback_store.close()

    raw_servers: dict[str, Any] = data.get("upstream_servers", {})
    changes, skipped = _merge_tune_changes(recs, raw_servers)

    if as_json:
        payload = _tune_json_payload(
            changes, skipped, resolved=resolved, since_hours=since_hours, tool_filter=tool_filter
        )
        click.echo(_json_dumps(payload, indent=2, ensure_ascii=False))
        return

    if not changes:
        for entry in skipped:
            click.echo(f"{_warn('Skipped:')} {_disp(entry)}")
        click.echo("No recommendations — all observed tools are within healthy parameters.")
        return

    _render_tune_preview(changes, skipped, since_hours=since_hours, apply_hint=not do_apply)
    if not do_apply:
        return

    if assume_yes:
        selected: list[_TuneChange] | None = list(changes)
    elif _stdin_is_tty():
        click.echo("")
        selected = _pick_tune_changes(changes)
    else:
        click.echo(
            f"{_err('Error:')} no TTY; pass --yes to apply all "
            "(or run without --apply to preview).",
            err=True,
        )
        sys.exit(1)

    if not selected:
        click.echo("Aborted. No changes made.")
        return

    _apply_tune_changes(data, selected)
    config_error = _schema_validation_error(data)
    if config_error:
        click.echo(
            f"{_err('Error:')} applying would produce an invalid config "
            f"({_disp(config_error)}); nothing written.",
            err=True,
        )
        sys.exit(1)

    backup = _backup_config_snapshot(resolved, original_text)
    click.echo(f"Backup: {backup}")
    _save(resolved, data)
    tool_count = len({(c.server, c.tool) for c in selected})
    click.echo(f"{_ok('Applied')} {len(selected)} override(s) to {tool_count} tool(s).")
    click.echo(
        "  A running proxy hot-reloads the config; restore with: "
        f"{_shell_join(['cp', '--', str(backup), str(resolved)])}"
    )


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
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Output as JSON for scripting (requires --yes, or --dry-run).",
)
@with_config_write_lock(skip=lambda kwargs: bool(kwargs.get("dry_run")), json_envelope=True)
def prune(
    names: tuple[str, ...],
    config_path: str,
    all_servers: bool,
    assume_yes: bool,
    dry_run: bool,
    as_json: bool = False,
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
        if as_json:
            _json_fail(
                "prune",
                "config_not_found",
                f"config not found at {resolved}",
                path=str(resolved),
            )
        sys.exit(1)

    def _prune_json_payload(
        planned: list[dict[str, str]],
        pruned: list[tuple[str, str]],
        failed: list[tuple[str, str, str]],
    ) -> dict[str, Any]:
        # All keys always present (locked shape — `host sync --json` doctrine)
        # so scripts can branch without existence checks.
        return {
            "action": "prune",
            "ok": not failed,
            "dry_run": dry_run,
            "config_path": str(resolved),
            "planned": planned,
            "pruned": [{"name": n, "source": s} for n, s in pruned],
            "failed": [
                {"name": n, "source": s, "error": e, "hint": _source_removal_hint(n, s)}
                for n, s, e in failed
            ],
        }

    data = _load(resolved)
    upstreams: dict[str, dict[str, Any]] = data.get("upstream_servers", {})
    if not upstreams:
        if as_json:
            _echo_json(_prune_json_payload([], [], []))
            return
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
            if as_json:
                _json_fail(
                    "prune",
                    "not_dual_registered",
                    f"not dual-registered: {', '.join(sorted(missing))}",
                    names=sorted(missing),
                )
            sys.exit(1)
        requested = set(names)
        dual = [c for c in dual if c["name"] in requested]

    if not dual:
        if as_json:
            _echo_json(_prune_json_payload([], [], []))
            return
        click.echo("No dual-registered upstreams found.")
        return

    # Preview iteration must cover the same ``source + duplicate_in`` set
    # that ``_prune_imported_candidates`` acts on — otherwise a user could
    # approve fewer entries than get written. See
    # ``test_duplicate_in_sources_all_pruned`` for the contract pin. The
    # ``--json`` ``planned`` rows come from this same loop so the machine
    # plan and the human preview cannot drift.
    if not as_json:
        click.echo(_hdr(f"Dual-registered upstream(s): {len(dual)}"))
    # Width over the *displayed* names, not the stored ones: an escaped
    # name is longer than its raw form, so measuring the raw one would
    # leave the detail column ragged for exactly the rows the escaping is
    # for (#755). ``planned`` keeps the stored name — it is the ``--json``
    # plan, and its consumer matches it against the config.
    disp_names = {c["name"]: _disp(c["name"]) for c in dual}
    name_width = max((len(n) for n in disp_names.values()), default=0)
    planned: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for cand in dual:
        detail = _format_candidate_detail(cand["entry"])
        for src in [cand["source"], *(cand.get("duplicate_in") or [])]:
            key = (cand["name"], src)
            if key in seen:
                continue
            seen.add(key)
            planned.append({"name": cand["name"], "source": src})
            if not as_json:
                click.echo(f"  {disp_names[cand['name']]:<{name_width}}  {detail}  — {src}")

    if dry_run:
        if as_json:
            _echo_json(_prune_json_payload(planned, [], []))
            return
        click.echo("")
        click.echo(f"{_ok('Dry run:')} no writes performed.")
        return

    if assume_yes:
        proceed = True
    elif as_json:
        # Never prompt in --json mode, even on a TTY — a formatting flag
        # must not authorize destructive writes (host sync precedent).
        _json_requires_yes("prune")
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

    pruned_at = utc_now_iso()
    pruned, failed = _prune_imported_candidates(dual, pruned_at=pruned_at)
    # Same step-③ metadata save as the inline import path: flip the matching
    # per-source ``origin`` rows for entries that have one. The save only
    # happens when a row actually flipped — entries without origin leave the
    # config untouched (the backup rows above are their only record).
    if _mark_pruned_sources(upstreams, dual, pruned, pruned_at):
        _save(resolved, data)
    # In --json mode the stdout success block is replaced by the payload;
    # the stderr failure diagnostics keep printing (stdout stays pure JSON).
    had_failures = _report_prune_results(pruned, failed, quiet_success=as_json)
    if as_json:
        _echo_json(_prune_json_payload(planned, pruned, failed))
    if had_failures:
        sys.exit(1)


# ── eject command (#475 PR3) ───────────────────────────────────────────────
#
# Reverse of the import(+prune) forward path: write the entry back to its
# host config (``origin.original`` verbatim when captured), then remove it
# from STM. Order invariant: host write FIRST, STM removal SECOND — every
# failure mode degrades to dual registration (visible, recoverable), never
# to a server registered nowhere. Like prune, this is an explicit opt-in
# writer; discovery itself stays read-only (#202/#226 lineage).


def _denormalize_client_entry(entry: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Reconstruct a host-config entry from an STM upstream entry.

    Degraded inverse of :func:`_normalize_client_entry` for entries without
    an ``origin.original`` (manual ``mms add``, pre-#475 imports). Forward
    normalization is lossy — ``_DANGEROUS_ENV_KEYS`` are filtered, and
    imports made before HTTP ``headers`` were captured carry none — so the
    reconstruction cannot recover those; the returned warnings name what may
    be missing. STM-only
    fields (prefix, compression, budgets, ...) are dropped by construction:
    the host entry is built from the transport-identifying fields alone.
    """
    transport = entry.get("transport", "stdio")
    warnings: list[str] = []
    if transport == "stdio":
        payload: dict[str, Any] = {"type": "stdio", "command": entry.get("command", "")}
        args = entry.get("args")
        if isinstance(args, list) and args:
            payload["args"] = [str(a) for a in args]
        env = entry.get("env")
        if isinstance(env, dict) and env:
            payload["env"] = {str(k): str(v) for k, v in env.items()}
        warnings.append("env keys filtered at import time (LD_PRELOAD etc.) are not recoverable")
    else:
        # STM calls streamable HTTP ``streamable_http``; host configs and the
        # claude CLI call it ``http`` (``_normalize_client_entry`` accepts both).
        payload = {
            "type": "sse" if transport == "sse" else "http",
            "url": entry.get("url", ""),
        }
        headers = entry.get("headers")
        if isinstance(headers, dict) and headers:
            payload["headers"] = {str(k): str(v) for k, v in headers.items()}
        else:
            warnings.append(
                "no HTTP headers recorded on this entry — imports made before "
                "headers were captured restore with none"
            )
    return payload, warnings


def _parse_eject_to(value: str) -> tuple[_SourceSpec, str | None]:
    """Parse ``--to kind[:PATH]`` into a source spec + resolved path.

    User-supplied paths are resolved here (relative paths, ``~``) — unlike
    ``origin.source.path``, which is used exactly as recorded because the
    claude CLI keyed it (already realpath'd; re-deriving could relocate the
    restore). Raises :class:`click.UsageError` on an unknown kind or a path
    on a kind that doesn't take one.
    """
    kind, _, path_part = value.partition(":")
    spec = _SOURCE_BY_KIND.get(kind)
    if spec is None:
        kinds = " | ".join(s.kind for s in _SOURCE_SPECS)
        raise click.UsageError(f"--to must be one of: {kinds} (got {value!r})")
    if kind == "claude-project":
        return spec, str(Path(path_part or ".").expanduser().resolve())
    if kind == "mcp-json":
        return spec, str(Path(path_part or ".mcp.json").expanduser().resolve())
    if path_part:
        raise click.UsageError(f"--to {kind} does not take a :PATH suffix")
    return spec, None


def _read_host_servers(kind: str, path: str | None) -> dict[str, Any] | None:
    """Read-only ``mcpServers`` map for one host target; ``None`` when absent.

    Same backing files as :func:`_discover_candidates`. Eject's no-clobber
    pre-check and post-write verify both read the host config directly
    rather than parsing ``claude mcp get`` output — the latter has no
    machine-readable contract, and a parse drift could false-pass the
    verify or leak secret values through diagnostics.
    """
    if kind == "mcp-json":
        data = _read_json_safely(Path(path)) if path else None
    elif kind == "claude-desktop":
        data = _read_json_safely(_desktop_config_path())
    elif kind == "claude-user":
        data = _read_json_safely(Path("~/.claude.json").expanduser())
    elif kind == "claude-project":
        cc = _read_json_safely(Path("~/.claude.json").expanduser())
        projects = cc.get("projects") if cc else None
        candidate = projects.get(path) if isinstance(projects, dict) else None
        data = candidate if isinstance(candidate, dict) else None
    else:
        return None
    if not isinstance(data, dict):
        return None
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else None


def _host_existing_entry(kind: str, path: str | None, name: str) -> dict[str, Any] | None:
    servers = _read_host_servers(kind, path)
    entry = servers.get(name) if servers else None
    return entry if isinstance(entry, dict) else None


def _payload_secret_keys(payload: dict[str, Any]) -> list[str]:
    """``env`` / ``headers`` keys whose values classify as secrets.

    Reuses the ``mms import`` classifier so the two flows agree on what
    counts as a credential. Decides whether the ``claude mcp add-json``
    shell-out would put a secret on argv (visible in the process list) —
    the §7 gate that ``--yes`` must never bypass.
    """
    from memtomem_stm.mms.secrets import classify_env

    found: list[str] = []
    for section in ("env", "headers"):
        block = payload.get(section)
        if not isinstance(block, dict):
            continue
        text = {str(k): str(v) for k, v in block.items()}
        for key, cls in classify_env(text).items():
            if cls.is_secret:
                found.append(f"{section}.{key}")
    return found


def _dict_diff_paths(
    expected: dict[str, Any], actual: dict[str, Any], prefix: str = ""
) -> list[str]:
    """Dotted paths where ``actual`` deviates from ``expected`` (post-write verify)."""
    paths: list[str] = []
    for key in expected.keys() | actual.keys():
        dotted = f"{prefix}{key}"
        if key not in actual:
            paths.append(f"{dotted} (dropped)")
        elif key not in expected:
            paths.append(f"{dotted} (added)")
        elif isinstance(expected[key], dict) and isinstance(actual[key], dict):
            paths.extend(_dict_diff_paths(expected[key], actual[key], prefix=f"{dotted}."))
        elif expected[key] != actual[key]:
            paths.append(f"{dotted} (changed)")
    return sorted(paths)


def _json_config_set_entry(
    path: Path, name: str, entry: dict[str, Any], *, default_mode: int
) -> tuple[bool, str | None]:
    """Set ``mcpServers[name]`` in a plain JSON host config, atomic write.

    Restore counterpart of :func:`_desktop_json_remove_entry`; also the
    ``.mcp.json`` writer (direct edit by design — argv non-exposure plus
    writing exactly the recorded path, where ``claude mcp add-json -s
    project`` would resolve by cwd). A corrupt existing file refuses the
    write rather than clobbering — restoring one entry must not destroy a
    config the user can still fix by hand. An existing file keeps its mode
    (a restore shouldn't change permissions); ``default_mode`` only applies
    to newly created files.
    """
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return (False, f"read error: {exc}")
        try:
            loaded = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            return (False, f"parse error: {exc} — fix or move {path} aside and retry")
        if not isinstance(loaded, dict):
            return (False, f"{path} top-level is not an object")
        existing = loaded
        try:
            default_mode = path.stat().st_mode & 0o777
        except OSError:
            pass
    servers = existing.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        existing["mcpServers"] = servers
    servers[name] = entry
    try:
        atomic_write_text(
            path,
            _json_dumps(existing, indent=2, ensure_ascii=False) + "\n",
            mode=default_mode,
            durable=True,
        )
    except OSError as exc:
        return (False, f"write error: {exc}")
    return (True, None)


def _claude_mcp_add_json(
    name: str, payload: dict[str, Any], scope: str, cwd: str | None = None
) -> tuple[bool, str | None]:
    """Shell out to ``claude mcp add-json <name> '<json>' -s <scope>``.

    Returns ``(ok, error_message_or_None)`` — non-fatal failure contract
    matching :func:`_claude_mcp_remove`, so eject leaves the STM entry put
    and prints the manual command instead of aborting the run. ``cwd`` is
    set for the ``local`` scope only (see :func:`_run_claude_mcp`).
    """
    cmd = ["claude", "mcp", "add-json", name, json.dumps(payload, ensure_ascii=False)]
    cmd += ["-s", scope]
    try:
        result = _run_claude_mcp(cmd, cwd=cwd)
    except FileNotFoundError:
        return (False, "`claude` CLI not on PATH")
    except subprocess.TimeoutExpired:
        return (False, "`claude mcp add-json` timed out")
    except OSError as exc:
        return (False, f"OS error invoking `claude`: {exc}")
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        return (False, msg or f"`claude mcp add-json` exited {result.returncode}")
    return (True, None)


def _eject_manual_hint(name: str, kind: str, path: str | None, payload: dict[str, Any]) -> str:
    """Copy-paste restore fallback when a host writer fails or is declined.

    Prints the full restore JSON (secrets included) — terminal output for
    the operator's own paste-back, per the RFC fallback UX; this is not the
    argv-exposure surface the secret gate covers.

    The ``# Edit`` branch is prose rather than a command, so ``_shell_join``
    never guards it; its config-derived interpolations are display-escaped by
    ``_disp`` instead (#754). The name is rendered by ``json.dumps`` — quotes
    included — rather than wrapped in literal quotes, so a name containing
    ``"`` or ``\\`` produces a fragment that still parses back to that exact
    key. Both used to break it, but a backslash broke it two different ways
    depending on what followed: an undefined JSON escape (``\\s``) made the
    fragment invalid, while a defined one (``\\b``) parsed silently into a
    *different* key.
    """
    payload_json = json.dumps(payload, ensure_ascii=False)
    if kind == "claude-user":
        return _shell_join(["claude", "mcp", "add-json", name, payload_json, "-s", "user"])
    if kind == "claude-project":
        # `&&` is a shell chain operator valid in both cmd.exe and POSIX sh, so it
        # stays a literal between two independently-joined commands — never an argv token.
        return (
            f"{_shell_join(['cd', path or '.'])} && "
            f"{_shell_join(['claude', 'mcp', 'add-json', name, payload_json, '-s', 'local'])}"
        )
    target = path if kind == "mcp-json" else str(_desktop_config_path())
    # Both JSON halves need ``_disp`` on top of ``json.dumps``:
    # ``ensure_ascii=False`` escapes only what JSON requires — the C0 subset —
    # so every other member of ``_disp_escapes``'s set survives raw inside
    # string values. Applying ``_disp`` after encoding is safe and
    # order-dependent —
    # ``\uXXXX`` is itself a JSON string escape and ``_disp`` never touches
    # backslashes, so ``json.dumps``'s own escapes pass through unchanged and
    # the fragment still parses back to the identical name and payload.
    name_json = json.dumps(name, ensure_ascii=False)
    return (
        f"# Edit {_disp(str(target))} and add under mcpServers: "
        f"{_disp(name_json)}: {_disp(payload_json)}"
    )


@dataclass
class _EjectPlan:
    """Resolved restore action for one entry (or the reason it can't run)."""

    name: str
    kind: str = ""
    path: str | None = None
    payload: dict[str, Any] | None = None
    verbatim: bool = False
    skip_write: bool = False
    overwrite: bool = False
    needs_secret_confirm: bool = False
    warnings: list[str] | None = None
    pruned_duplicates: list[str] | None = None
    error: str | None = None


def _latest_backup_row(name: str) -> dict[str, Any] | None:
    """Most recent prune-backup row for ``name``, or None.

    Suggestion source only (RFC §4.2): eject never auto-adopts a backup row
    — it may be stale relative to what the host had last — so the caller
    prints it and asks the user to re-run with ``--to`` after checking.
    """
    path = _pruned_backup_path()
    data = _read_json_safely(path)
    entries = data.get("entries") if data else None
    if not isinstance(entries, list):
        return None
    for row in reversed(entries):
        if isinstance(row, dict) and row.get("name") == name:
            return row
    return None


def _resolve_eject_plan(
    name: str,
    entry: Any,
    to_spec: tuple[_SourceSpec, str | None] | None,
    *,
    force: bool,
    allow_argv_secrets: bool,
    interactive: bool = True,
) -> _EjectPlan:
    """Decide target, payload, and guards for one entry — no writes.

    Implements RFC #475 §4.3 steps 1–3 plus the secret gate's non-TTY
    branch. TTY secret confirmation is deferred to execution time so the
    prompt sits next to the write it authorizes. ``interactive=False``
    (the ``--json`` path, which never prompts) forces the secret gate down
    the non-TTY branch even on a real TTY, so the entry fails with the
    same wording in both modes.
    """
    if not isinstance(entry, dict):
        return _EjectPlan(name=name, error="entry is not an object — fix the config by hand")
    if _is_self_reference(entry):
        return _EjectPlan(name=name, error="refusing to eject STM's own registration")

    origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else None
    source = origin.get("source") if origin and isinstance(origin.get("source"), dict) else None
    warnings: list[str] = []

    # Step 1 — target: origin.source wins; --to is the escape hatch for
    # origin-less entries or a vanished origin path.
    kind = path = None
    no_origin = "no usable origin recorded"
    if source and source.get("kind") in _SOURCE_BY_KIND:
        kind = source["kind"]
        path = source.get("path") if isinstance(source.get("path"), str) else None
    if kind == "claude-project" and (path is None or not Path(path).is_dir()):
        no_origin = f"origin project directory no longer exists: {path}"
        kind = path = None
    if kind == "mcp-json" and (path is None or not Path(path).parent.is_dir()):
        no_origin = f"origin .mcp.json directory no longer exists: {path}"
        kind = path = None
    if kind is None:
        if to_spec is None:
            row = _latest_backup_row(name)
            if row is not None:
                src = row.get("source") or {}
                hint = src.get("kind", "?") if isinstance(src, dict) else "?"
                recorded_path = src.get("path") if isinstance(src, dict) else None
                if (
                    hint in {"claude-project", "mcp-json"}
                    and isinstance(recorded_path, str)
                    and recorded_path
                ):
                    target_value = f"{hint}:{recorded_path}"
                    # Render a copy-paste-safe shell argument. This is POSIX/
                    # PowerShell-oriented quoting, not cmd.exe syntax.
                    retry_target = f"`--to {shlex.quote(target_value)}`"
                elif hint in _SOURCE_BY_KIND:
                    retry_target = f"`--to {hint}`"
                else:
                    retry_target = f"`--to TARGET` ({_EJECT_TARGETS_HELP})"
                return _EjectPlan(
                    name=name,
                    error=(
                        f"{no_origin} — the prune backup log has a row "
                        f"for '{name}' (kind={hint}, pruned_at={row.get('pruned_at')}); "
                        f"verify it is current, then re-run with {retry_target}"
                    ),
                )
            return _EjectPlan(
                name=name,
                error=(
                    f"{no_origin}; STM cannot infer the original host — re-run with "
                    f"`--to TARGET` where TARGET is one of: {_EJECT_TARGETS_HELP}. "
                    "This STM entry was not changed"
                ),
            )
        spec, to_path = to_spec
        kind, path = spec.kind, to_path
        if kind == "claude-project" and (path is None or not Path(path).is_dir()):
            return _EjectPlan(name=name, error=f"--to project directory does not exist: {path}")

    # Step 2 — payload: verbatim original first; reconstructed otherwise.
    original = origin.get("original") if origin else None
    if isinstance(original, dict):
        payload: dict[str, Any] = copy.deepcopy(original)
        verbatim = True
        normalized = _normalize_client_entry(original)
        if normalized is not None:
            stm_sig = _server_signature(entry)
            orig_sig = _server_signature(normalized)
            if stm_sig is not None and orig_sig is not None and stm_sig != orig_sig:
                warnings.append(
                    "STM entry was modified after import — restoring the original "
                    "host entry as imported (STM-side changes are not carried over)"
                )
    else:
        payload, loss = _denormalize_client_entry(entry)
        verbatim = False
        warnings.append("no verbatim original captured — restoring a reconstructed entry")
        warnings.extend(loss)

    # Step 3 — no-clobber guard against the live host config.
    skip_write = overwrite = False
    existing = _host_existing_entry(kind, path, name)
    if existing is not None:
        existing_norm = _normalize_client_entry(existing)
        payload_norm = _normalize_client_entry(payload)
        existing_sig = _server_signature(existing_norm) if existing_norm else None
        payload_sig = _server_signature(payload_norm) if payload_norm else None
        if existing_sig is not None and payload_sig is not None:
            if existing_sig == payload_sig:
                skip_write = True
            elif force:
                overwrite = True
            else:
                return _EjectPlan(
                    name=name,
                    error=(
                        f"a different '{name}' already exists in the target "
                        "(command/url mismatch) — pass --force to overwrite it"
                    ),
                )
        # A missing signature on either side is NEVER an idempotent match:
        # full structural equality is the only skip condition (#475 R1 M1).
        elif existing == payload:
            skip_write = True
        elif force:
            overwrite = True
        else:
            return _EjectPlan(
                name=name,
                error=(
                    f"the target already has '{name}' and its identity can't be "
                    "compared (no command/url signature) — pass --force to overwrite"
                ),
            )

    # Secret gate (§7) — shell-out scopes only, and only when a write will
    # actually run. --yes never bypasses this; --allow-argv-secrets is the
    # single override for both TTY and non-TTY.
    needs_secret_confirm = False
    spec = _SOURCE_BY_KIND[kind]
    shell_out = spec.kind in ("claude-user", "claude-project")
    if shell_out and not skip_write and not allow_argv_secrets:
        secret_keys = _payload_secret_keys(payload)
        if secret_keys:
            if not interactive or not _should_use_tui():
                return _EjectPlan(
                    name=name,
                    error=(
                        f"payload carries secret-classified values ({', '.join(secret_keys)}) "
                        "that `claude mcp add-json` would expose on argv — pass "
                        "--allow-argv-secrets to proceed (--yes does not cover this)"
                    ),
                )
            needs_secret_confirm = True
            warnings.append(
                f"secret-classified values on argv if confirmed: {', '.join(secret_keys)}"
            )

    pruned_dups: list[str] = []
    for dup in (origin.get("duplicates") or []) if origin else []:
        if isinstance(dup, dict) and dup.get("pruned"):
            label_spec = _SOURCE_BY_KIND.get(dup.get("kind", ""))
            pruned_dups.append(label_spec.label if label_spec else str(dup.get("kind")))

    return _EjectPlan(
        name=name,
        kind=kind,
        path=path,
        payload=payload,
        verbatim=verbatim,
        skip_write=skip_write,
        overwrite=overwrite,
        needs_secret_confirm=needs_secret_confirm,
        warnings=warnings,
        pruned_duplicates=pruned_dups,
    )


def _argv_is_encodable(name: str, payload: dict[str, Any]) -> bool:
    """True when both halves of the ``claude mcp add-json`` argv can be spawned.

    ``subprocess`` encodes arguments to UTF-8 and raises ``UnicodeEncodeError``
    on a lone surrogate — a raise the callers' ``FileNotFoundError`` /
    ``OSError`` handlers do not cover. Asking before a destructive step turns
    that into the ordinary non-fatal failure those callers already report.
    """
    try:
        name.encode("utf-8")
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _eject_host_write(plan: _EjectPlan) -> tuple[bool, str | None]:
    """Dispatch the host write for one resolved plan (RFC §4.1 writer table)."""
    assert plan.payload is not None
    spec = _SOURCE_BY_KIND[plan.kind]
    if plan.kind in ("claude-user", "claude-project"):
        scope = spec.claude_scope or "user"
        cwd = plan.path if plan.kind == "claude-project" else None
        # Both paths below spawn `claude mcp …` with the name and payload as
        # arguments, which `subprocess` encodes to UTF-8 — a raise neither
        # `_claude_mcp_add_json` nor `_claude_mcp_remove` catches, since both
        # guard `FileNotFoundError`/`OSError` (#757). Check once, before
        # either: on the `--force` path the pre-remove is destructive and
        # would otherwise leave the host with neither the old registration
        # nor the new one, and on the plain path an uncaught traceback
        # replaces the "cannot eject" diagnostic this function exists to
        # return.
        if not _argv_is_encodable(plan.name, plan.payload):
            return (False, "server name or payload is not valid UTF-8")
        if plan.overwrite:
            # `claude mcp add-json` has no overwrite flag (same probe lineage
            # as `claude mcp add`, cli/proxy.py:274) — remove-then-add. The
            # pre-remove needs the same cwd as the add: `-s local` resolves
            # its project slot from the process cwd on both verbs.
            ok, err = _claude_mcp_remove(plan.name, scope, cwd=cwd)
            if not ok:
                return (False, f"--force pre-remove failed: {err}")
        return _claude_mcp_add_json(plan.name, plan.payload, scope, cwd=cwd)
    # ``plan.path`` is always set for mcp-json by plan resolution (the
    # preflight rejected pathless targets); mypy can't see that invariant.
    target = Path(plan.path or "") if plan.kind == "mcp-json" else _desktop_config_path()
    # .mcp.json is a shared project file (0644 precedent in
    # _write_mcp_json_for_stm); the Desktop config carries secrets (0600).
    default_mode = 0o644 if plan.kind == "mcp-json" else 0o600
    return _json_config_set_entry(target, plan.name, plan.payload, default_mode=default_mode)


def _eject_verify(plan: _EjectPlan) -> list[str]:
    """Pre-removal verify: re-read the host config, deep-compare to the payload.

    Returns the list of deviating paths (empty = verbatim restore). Runs
    after a write — the claude CLI re-serializes through its own schema and
    silently drops unknown fields (probed on 2.1.173), so a clean
    `add-json` exit does not prove the verbatim-restore contract held —
    AND on the skipped-write path, where the no-clobber signature match
    ignores env/headers/unknown fields. Only a host entry deep-equal to
    the payload may release the STM entry. A `None` read (entry not where
    expected) reports as a full mismatch rather than crashing.
    """
    assert plan.payload is not None
    actual = _host_existing_entry(plan.kind, plan.path, plan.name)
    if actual is None:
        return ["<entry not found at the expected host location after write>"]
    return _dict_diff_paths(plan.payload, actual)


@cli.command()
@click.argument("names", nargs=-1, required=True)
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option(
    "--to",
    "to_target",
    default=None,
    metavar="TARGET",
    help=(
        f"Restore target for entries without a usable origin: {_EJECT_TARGETS_HELP}. Entries "
        "with a recorded origin ignore this."
    ),
)
@click.option(
    "--keep",
    is_flag=True,
    help="Restore to the host but keep the STM entry (dual registration).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite a same-name host entry whose identity differs.",
)
@click.option(
    "--allow-argv-secrets",
    "allow_argv_secrets",
    is_flag=True,
    help=(
        "Permit `claude mcp add-json` shell-outs whose payload carries "
        "secret-classified values (argv is visible in the process list). "
        "The only override for the secret gate — --yes does not bypass it."
    ),
)
@click.option(
    "--accept-schema-loss",
    "accept_schema_loss",
    is_flag=True,
    help=(
        "Proceed with STM removal even when the restored host entry does not "
        "structurally match the original (the claude CLI strips fields it "
        "does not know). Default is to keep the STM entry and fail."
    ),
)
@click.option("--dry-run", "dry_run", is_flag=True, help="Print the plan; no writes.")
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Skip the confirm prompt (scripts / CI / non-TTY callers).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Output as JSON for scripting (requires --yes, or --dry-run).",
)
@with_config_write_lock(skip=lambda kwargs: bool(kwargs.get("dry_run")), json_envelope=True)
def eject(
    names: tuple[str, ...],
    config_path: str,
    to_target: str | None,
    keep: bool,
    force: bool,
    allow_argv_secrets: bool,
    accept_schema_loss: bool,
    dry_run: bool,
    assume_yes: bool,
    as_json: bool = False,
) -> None:
    """Restore imported upstream(s) to their host MCP client, then remove from STM.

    Reverse of ``mms add --import --prune``: writes the verbatim host entry
    captured at import time (``origin.original``) back to where it came
    from, verifies the restore against the host config, and only then
    removes the STM entry. Any failure leaves the server registered in at
    least one place — worst case is dual registration, never disappearance.
    """
    path = Path(config_path)
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        click.echo(f"{_err('Error:')} config not found at {resolved}.", err=True)
        click.echo("  Run `mms init` first.", err=True)
        if as_json:
            _json_fail(
                "eject",
                "config_not_found",
                f"config not found at {resolved}",
                path=str(resolved),
            )
        sys.exit(1)

    data = _load(resolved)
    servers: dict[str, Any] = data.get("upstream_servers", {})
    missing = [n for n in names if n not in servers]
    if missing:
        click.echo(f"{_err('Error:')} not configured: {', '.join(missing)}.", err=True)
        if as_json:
            _json_fail(
                "eject",
                "not_configured",
                f"not configured: {', '.join(missing)}",
                names=missing,
            )
        sys.exit(1)

    to_spec = _parse_eject_to(to_target) if to_target else None
    plans = [
        _resolve_eject_plan(
            n,
            servers[n],
            to_spec,
            force=force,
            allow_argv_secrets=allow_argv_secrets,
            # --json never prompts; route the secret gate to its non-TTY
            # branch so the entry fails with the same wording either way.
            interactive=not as_json,
        )
        for n in names
    ]

    def _eject_json_payload(
        restored_names: list[str],
        failed_rows: list[tuple[_EjectPlan, str]],
    ) -> dict[str, Any]:
        # All keys always present (locked shape). ``plan`` mirrors the human
        # plan display; ``_EjectPlan.payload`` is never serialized — it is
        # the verbatim host entry and may carry secrets. ``failed[].hint``
        # embeds the payload the same way the stderr hint already does.
        return {
            "action": "eject",
            "ok": not failed_rows and not errors,
            "dry_run": dry_run,
            "keep": keep,
            "config_path": str(resolved),
            "plan": [
                {
                    "name": p.name,
                    "target": None if p.error else {"kind": p.kind, "path": p.path},
                    "write": None
                    if p.error
                    else (
                        "skip_write"
                        if p.skip_write
                        else ("overwrite" if p.overwrite else "restore")
                    ),
                    "verbatim": p.verbatim,
                    "warnings": list(p.warnings or []),
                    "pruned_duplicates": list(p.pruned_duplicates or []),
                    "error": p.error,
                }
                for p in plans
            ],
            "restored": restored_names,
            "removed_from_stm": [] if keep else restored_names,
            "failed": [
                {
                    "name": p.name,
                    "error": err,
                    "hint": _eject_manual_hint(p.name, p.kind, p.path, p.payload)
                    if p.payload is not None
                    else None,
                }
                for p, err in failed_rows
            ],
        }

    # Plan display — every entry, actionable or not, before any consent.
    # Suppressed in --json mode: the payload's ``plan`` rows carry it.
    if not as_json:
        click.echo(_hdr(f"Eject plan ({len(plans)} entr{'y' if len(plans) == 1 else 'ies'}):"))
        for plan in plans:
            if plan.error:
                # Escaped here rather than where the message is built: every
                # branch that sets ``error`` folds in a config value — a
                # name, an origin path, a ``kind`` and ``pruned_at`` read
                # back from the prune backup log (#755).
                click.echo(f"  {_disp(plan.name)}: {_bad('cannot eject')} — {_disp(plan.error)}")
                continue
            spec = _SOURCE_BY_KIND[plan.kind]
            where = f"{spec.label}" + (f" [{_disp(plan.path)}]" if plan.path else "")
            action = (
                "already present — skip write"
                if plan.skip_write
                else ("overwrite" if plan.overwrite else "restore")
            )
            body = "verbatim original" if plan.verbatim else "reconstructed entry"
            click.echo(f"  {_disp(plan.name)}: {action} → {where}  ({body})")
            for w in plan.warnings or []:
                click.echo(f"    {_warn('warning:')} {_disp(w)}")
            if plan.pruned_duplicates:
                # Source labels are constants, except that a row recorded
                # under an unrecognized kind falls back to printing that
                # kind — a config value — verbatim.
                click.echo(
                    f"    {_warn('note:')} also pruned from "
                    f"{_disp(', '.join(plan.pruned_duplicates))} — "
                    "not restored here; originals remain in "
                    f"{_pruned_backup_path()} (restore manually if needed)"
                )

    errors = [p for p in plans if p.error]
    actionable = [p for p in plans if not p.error]

    if dry_run:
        if as_json:
            _echo_json(_eject_json_payload([], []))
            if errors:
                sys.exit(1)
            return
        click.echo("")
        click.echo(f"{_ok('Dry run:')} no writes performed.")
        if errors:
            sys.exit(1)
        return

    if not actionable:
        if as_json:
            _echo_json(_eject_json_payload([], []))
        sys.exit(1)

    if assume_yes:
        proceed = True
    elif as_json:
        # Never prompt in --json mode, even on a TTY — a formatting flag
        # must not authorize destructive writes (host sync precedent).
        _json_requires_yes("eject")
    elif _should_use_tui():
        click.echo("")
        verb = (
            "Restore to host(s) and keep in STM?"
            if keep
            else "Restore to host(s) and remove from STM?"
        )
        proceed = click.confirm(verb, default=False)
    else:
        click.echo(
            f"{_err('Error:')} no TTY; pass --yes to confirm (or --dry-run to preview).",
            err=True,
        )
        sys.exit(1)
    if not proceed:
        click.echo("Aborted. No changes made.")
        return

    restored: list[_EjectPlan] = []
    failed: list[tuple[_EjectPlan, str]] = []
    config_changed = False
    for plan in actionable:
        assert plan.payload is not None
        if plan.needs_secret_confirm:
            ok = click.confirm(
                f"  '{_disp(plan.name)}': pass the secret value(s) above on `claude` argv "
                "(visible in the process list while it runs)?",
                default=False,
            )
            if not ok:
                failed.append((plan, "secret gate declined"))
                continue

        wrote = False
        if not plan.skip_write:
            ok, err = _eject_host_write(plan)
            if not ok:
                failed.append((plan, err or "host write failed"))
                continue
            wrote = True

        # Verify on BOTH paths — written and skipped. A same-signature host
        # entry is not necessarily structurally equal to the original
        # (signatures ignore env/headers/unknown fields), so removing the
        # STM entry on signature alone could destroy the only complete copy.
        # The invariant: STM removal requires a host entry deep-equal to the
        # payload, or the operator's explicit --accept-schema-loss.
        mismatches = _eject_verify(plan)
        if mismatches:
            detail = ", ".join(mismatches)
            state = "restored host entry" if wrote else "existing host entry"
            if not accept_schema_loss:
                failed.append(
                    (
                        plan,
                        f"{state} does not match the captured original "
                        f"({detail}) — the STM entry is kept (dual registration); "
                        "re-run with --accept-schema-loss to remove from STM anyway",
                    )
                )
                continue
            click.echo(
                f"  {_warn('Warning:')} '{_disp(plan.name)}' {state} deviates from the "
                f"original (schema loss accepted): {_disp(detail)}",
                err=True,
            )

        if not keep:
            del servers[plan.name]
            config_changed = True
        restored.append(plan)

    if config_changed:
        _save(resolved, data)

    if restored and not as_json:
        click.echo("")
        if keep:
            click.echo(f"{_ok('Restored to host (kept in STM):')}")
        else:
            click.echo(f"{_ok('Restored to host and removed from STM:')}")
        for plan in restored:
            spec = _SOURCE_BY_KIND[plan.kind]
            click.echo(f"  {_disp(plan.name)} — {spec.label}")
        if keep:
            click.echo(
                f"{_warn('Note:')} entries are now dual-registered (direct + via STM). "
                "Run `mms prune NAME` to collapse back to STM-only routing."
            )

    if failed:
        if not as_json:
            click.echo("")
        click.echo(f"{_warn('Warning:')} could not eject {len(failed)} entr(ies):", err=True)
        for plan, err in failed:
            # Distinct from the ``plan.error`` line above: these come from
            # the host write — the claude CLI's stderr, or an OS error
            # naming the target file.
            click.echo(f"  {_disp(plan.name)}: {_disp(err)}", err=True)
        click.echo("", err=True)
        click.echo("Restore manually (entry remains in STM):", err=True)
        click.echo(
            "  (a failing `claude mcp add-json` may also mean the installed claude "
            "CLI predates json/http support — probed working on 2.1.173)",
            err=True,
        )
        for plan, _err_msg in failed:
            if plan.payload is not None:
                click.echo(
                    f"  {_eject_manual_hint(plan.name, plan.kind, plan.path, plan.payload)}",
                    err=True,
                )

    if config_changed and not servers:
        # In --json mode the note goes to stderr so stdout stays pure JSON.
        click.echo("", err=as_json)
        click.echo(
            f"{_warn('Note:')} no upstream servers remain. To also remove STM's own "
            "client registration, run: claude mcp remove memtomem-stm",
            err=as_json,
        )

    if as_json:
        _echo_json(_eject_json_payload([p.name for p in restored], failed))

    if failed or errors:
        sys.exit(1)


# ── health command ──────────────────────────────────────────────────────


async def _probe_one(cfg: dict[str, Any], timeout: float) -> StagedProbeResult:
    """Probe a single upstream server: connect, initialize, list tools.

    *timeout* is an **end-to-end** budget shared across transport connect +
    ``initialize()`` + ``list_tools()`` — each phase gets ``deadline - now``
    so a stall in any phase can't push the probe past the
    ``mms health --timeout`` / ``mms add --validate --timeout`` ceiling.
    Mirrors ``_probe_ltm_mcp_server``'s deadline pattern; previously only
    ``initialize()`` was bounded, so a network upstream hanging on TCP
    connect (or any upstream stalling on ``tools/list``) blocked the probe
    indefinitely and the timeout classification never fired.

    Returns a ``StagedProbeResult`` instead of raising: the exception has
    already unwound the stage ladder by the time a caller could catch it,
    so classifying here is the only way to preserve *which* phase failed.
    Failure causes are sanitized against the server's configured
    ``env``/``headers`` values (and URL credentials) before being stored —
    ``health``/``doctor`` render ``error`` verbatim.
    """
    import httpx2

    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from memtomem_stm.utils.mcp_transport import streamable_http_transport

    deadline = asyncio.get_running_loop().time() + timeout

    def remaining() -> float:
        # The operation-phase ``wait_for`` calls treat <=0 as "fire
        # immediately"; clamp to a tiny positive so we always raise
        # ``TimeoutError`` rather than returning a bogus result on an
        # exhausted budget.
        return max(1e-3, deadline - asyncio.get_running_loop().time())

    transport = str(cfg.get("transport", "stdio"))
    stage = ProbeStage.CONFIGURED
    tools = 0
    overflowing: tuple[str, ...] = ()
    try:
        if transport == "stdio":
            ctx = stdio_client(
                StdioServerParameters(
                    command=cfg.get("command", ""),
                    args=cfg.get("args", []),
                    env=cfg.get("env"),
                    cwd=cfg.get("cwd"),
                )
            )
        elif transport == "sse":
            sdk_timeout = remaining()
            ctx = sse_client(
                cfg.get("url", ""),
                headers=cfg.get("headers"),
                timeout=sdk_timeout,
                sse_read_timeout=sdk_timeout,
            )
        else:
            sdk_timeout = remaining()
            # ``Timeout(sdk_timeout)`` sets connect/write/pool, and the
            # explicit ``read=`` keeps the probe's single deadline on every
            # leg — the 1.x call passed ``timeout`` and ``sse_read_timeout``
            # the same way.
            ctx = streamable_http_transport(
                cfg.get("url", ""),
                headers=cfg.get("headers"),
                timeout=httpx2.Timeout(sdk_timeout, read=sdk_timeout),
            )

        async with AsyncExitStack() as stack:
            # ``wait_for(context.__aenter__())`` runs the enter in a child task
            # on Python 3.12, then this task exits it — invalid for AnyIO's
            # task-affine cancel scopes. ``timeout_at`` keeps both lifecycle
            # operations in this probe task while preserving the deadline.
            async with asyncio.timeout_at(deadline):
                streams = await stack.enter_async_context(ctx)
            stage = ProbeStage.TRANSPORT_CONNECTED
            async with ClientSession(streams[0], streams[1]) as session:
                await asyncio.wait_for(session.initialize(), timeout=remaining())
                stage = ProbeStage.MCP_INITIALIZED
                result = await asyncio.wait_for(session.list_tools(), timeout=remaining())
                prefix = cfg.get("prefix", "")
                tools = len(result.tools)
                overflowing = tuple(
                    t.name for t in result.tools if tool_name_budget.overflows(prefix, t.name)
                )
                # Only now is discovery genuinely complete. Setting the stage
                # after processing the result (not right after list_tools)
                # keeps a malformed-result failure classified as an
                # MCP_INITIALIZED failure instead of being swallowed by the
                # post-discovery teardown-noise path below.
                stage = ProbeStage.TOOLS_DISCOVERED
    except Exception as exc:
        # A failure raised *after* discovery completed is teardown noise —
        # anyio transports commonly re-raise a background-task error from the
        # context ``__aexit__``. The probe already got the tool list, so it
        # succeeded; reporting DISCONNECTED "failed after tools discovered"
        # would be wrong. Return the success result and drop the cleanup
        # error (it's not actionable for a connectivity probe).
        if stage is ProbeStage.TOOLS_DISCOVERED:
            return StagedProbeResult(
                stage=stage, transport=transport, tools=tools, overflowing=overflowing
            )
        # ``asyncio.wait_for`` raises ``TimeoutError`` directly, but anyio's
        # TaskGroup (wrapped by the SDK transports) re-raises failures as
        # ``ExceptionGroup`` leaves — dispatch on the root cause so
        # ``--timeout N`` always renders as ``timeout (Ns)`` rather than a
        # wrapper type name.
        root = _root_cause_exc(exc)
        if isinstance(root, TimeoutError):
            error = f"timeout ({timeout}s)"
        else:
            error = _sanitize_probe_error(_root_cause_message(exc), cfg)
        return StagedProbeResult(stage=stage, transport=transport, error=error)
    return StagedProbeResult(stage=stage, transport=transport, tools=tools, overflowing=overflowing)


def _format_command_for_display(command: str, args: list[str]) -> str:
    return shlex.join([command, *args]) if args else command


def _text_parts_from_tool_result(result: Any) -> list[str]:
    return [
        str(getattr(content, "text", "") or "")
        for content in getattr(result, "content", [])
        if getattr(content, "type", None) == "text"
    ]


def _ltm_metadata_from_tool_result(result: Any) -> dict[str, Any]:
    text_parts = _text_parts_from_tool_result(result)
    if not text_parts:
        return {"version": None, "runtime_profile": None}
    try:
        data = json.loads(text_parts[0])
    except json.JSONDecodeError:
        return {"version": None, "runtime_profile": None}
    if not isinstance(data, dict):
        return {"version": None, "runtime_profile": None}
    version = data.get("version")
    profile = data.get("runtime_profile")
    if not isinstance(profile, dict) or profile.get("schema_version") != 1:
        profile = None
    return {"version": str(version) if version else None, "runtime_profile": profile}


def _version_from_tool_result(result: Any) -> str | None:
    return _ltm_metadata_from_tool_result(result)["version"]


async def _probe_ltm_mcp_server(
    transport: str,
    command: str,
    args: list[str],
    url: str,
    headers: dict[str, str] | None,
    timeout: float,
    errlog: TextIO,
) -> dict[str, Any]:
    """Probe the configured LTM MCP server without starting the proxy.

    *timeout* is an **end-to-end** budget shared across initialize +
    list_tools + the optional ``mem_do(action="version")`` probe — each
    ``wait_for`` gets ``deadline - now`` so a stall in any phase can't push
    the probe past the ``mms health --timeout`` ceiling. If the budget is
    already exhausted when the version probe would run, it's skipped (the
    version string is best-effort UX, not a correctness signal).

    Verifies that ``mem_search`` (the only tool the surfacing adapter strictly
    requires — see ``surfacing/mcp_client.py:423``) is advertised by the
    server before declaring ``connected``. A process that handshakes as MCP
    but doesn't expose ``mem_search`` would later silently fail every
    surfacing call; flagging it here is the diagnostic operators want from
    ``mms health``. The ``mem_do(action="version")`` probe stays best-effort
    and only runs when ``mem_do`` is advertised — older servers that don't
    recognize ``action=version`` should not poison the result.

    Child stderr is routed to *errlog* so server banner / log lines don't
    bleed into the ``mms health`` text/JSON output (caller passes an
    fd-backed sink — ``stdio_client`` forwards this to
    ``create_subprocess_exec(stderr=...)``, which requires ``fileno``).
    """
    import httpx2

    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from memtomem_stm.utils.mcp_transport import streamable_http_transport

    deadline = asyncio.get_running_loop().time() + timeout

    def remaining() -> float:
        # ``asyncio.wait_for`` treats <=0 as "fire immediately"; clamp to a
        # tiny positive so we always raise ``TimeoutError`` rather than
        # returning a bogus result on a zero-budget call.
        return max(1e-3, deadline - asyncio.get_running_loop().time())

    sdk_timeout = remaining()
    if transport == "sse":
        ctx = sse_client(
            url,
            headers=headers,
            timeout=sdk_timeout,
            sse_read_timeout=sdk_timeout,
        )
    elif transport == "streamable_http":
        # Same mapping as ``_probe_one``: one probe deadline on every leg.
        ctx = streamable_http_transport(
            url,
            headers=headers,
            timeout=httpx2.Timeout(sdk_timeout, read=sdk_timeout),
        )
    else:
        params = StdioServerParameters(command=command, args=args)
        ctx = stdio_client(params, errlog=errlog)

    async with AsyncExitStack() as stack:
        # Keep transport enter/exit in this task.  On Python 3.12,
        # ``wait_for(context.__aenter__())`` would enter in a child task and
        # make this stack's later exit violate AnyIO cancel-scope affinity.
        async with asyncio.timeout_at(deadline):
            streams = await stack.enter_async_context(ctx)
        async with ClientSession(streams[0], streams[1]) as session:
            await asyncio.wait_for(session.initialize(), timeout=remaining())
            tools_result = await asyncio.wait_for(session.list_tools(), timeout=remaining())
            tool_names = {t.name for t in tools_result.tools}
            if "mem_search" not in tool_names:
                return {
                    "connected": False,
                    "version": None,
                    "error": (
                        "server initialized but does not expose 'mem_search' "
                        "(surfacing adapter calls this tool — config likely "
                        "points at a non-memtomem MCP server)"
                    ),
                }
            version: str | None = None
            runtime_profile: dict[str, Any] | None = None
            # ``mem_search`` connectivity is already proven — don't burn what
            # remains of the budget on the optional version probe if it'd
            # push us past the deadline. The threshold is small enough to
            # let a typical sub-second ``mem_do`` round-trip through but
            # large enough to avoid spawning a ``wait_for`` we know will
            # immediately raise.
            budget_left = deadline - asyncio.get_running_loop().time()
            if "mem_do" in tool_names and budget_left > 0.05:
                try:
                    result = await asyncio.wait_for(
                        session.call_tool("mem_do", {"action": "version"}),
                        timeout=remaining(),
                    )
                    metadata = _ltm_metadata_from_tool_result(result)
                    version = metadata["version"]
                    runtime_profile = metadata["runtime_profile"]
                except Exception:
                    logger.debug(
                        "LTM mem_do(version) probe failed or timed out within "
                        "the shared budget; treating as unsupported.",
                        exc_info=True,
                    )
            return {
                "connected": True,
                "version": version,
                "runtime_profile": runtime_profile,
                "error": None,
            }


def _ltm_mcp_status(surfacing: Any, timeout: float) -> dict[str, Any]:
    """Probe the configured LTM MCP server for ``mms health``.

    *timeout* is the per-server bound from ``mms health --timeout``; the LTM
    probe must honor it so a stalled LTM command can't push the health
    diagnostic past the user-requested ceiling. ``surfacing.timeout_seconds``
    (the runtime-call timeout) is not used here — it's a different SLA.
    """
    transport = str(getattr(surfacing, "ltm_mcp_transport", "stdio") or "stdio")
    command = str(getattr(surfacing, "ltm_mcp_command", "") or "")
    args = [str(arg) for arg in (getattr(surfacing, "ltm_mcp_args", []) or [])]
    url = str(getattr(surfacing, "ltm_mcp_url", "") or "")
    headers = getattr(surfacing, "ltm_mcp_headers", None)
    if not isinstance(headers, dict):
        headers = None
    # ``status`` is the operator-facing payload (text lines AND ``--json``
    # dump), so the URL fields are userinfo-redacted; only the local ``url``
    # stays raw for the probe's actual connection below.
    display = (
        _format_command_for_display(command, args)
        if transport == "stdio" and command
        else redact_url_userinfo(url) or "(empty url)"
    )
    status: dict[str, Any] = {
        "route": "direct",
        "transport": transport,
        "command": command,
        "args": args,
        "url": redact_url_userinfo(url),
        "display": display,
        "connected": None,
        "version": None,
        "runtime_profile": None,
        "error": None,
    }

    if not getattr(surfacing, "enabled", False):
        status["skipped"] = "surfacing_disabled"
        return status
    if transport != "stdio" and not url:
        status["connected"] = False
        status["error"] = "ltm_mcp_url is required for network LTM transport"
        return status
    if transport == "stdio" and not command:
        status["connected"] = False
        status["error"] = "ltm_mcp_command is empty"
        return status
    if transport == "stdio" and shutil.which(command) is None:
        status["connected"] = False
        status["error"] = f"{command} not on PATH"
        return status

    probe_timeout = max(0.1, float(timeout))
    try:
        # ``stdio_client`` forwards *errlog* directly to
        # ``asyncio.create_subprocess_exec(stderr=...)``, which requires a real
        # file descriptor — ``io.StringIO`` lacks ``fileno`` and would crash
        # here. ``os.devnull`` gives us a fd-backed sink so server banners
        # and MCP-SDK stderr can't bleed into ``mms health`` output.
        with open(os.devnull, "w") as errnull, _silenced_mcp_sdk_logs():
            probe = asyncio.run(
                _probe_ltm_mcp_server(
                    transport,
                    command,
                    args,
                    url,
                    headers,
                    probe_timeout,
                    errnull,
                )
            )
    except Exception as exc:
        # ``asyncio.wait_for`` inside the probe raises ``TimeoutError``, but
        # anyio's ``TaskGroup`` (wrapped by ``stdio_client``) re-raises it
        # as an ``ExceptionGroup`` leaf — a bare ``except TimeoutError``
        # would miss the wrapped case. Walk to the root cause and dispatch
        # on the leaf type instead so ``--timeout N`` always renders as
        # ``timeout (Ns)`` rather than a ``TimeoutError`` type name.
        root = _root_cause_exc(exc)
        status["connected"] = False
        if isinstance(root, TimeoutError):
            status["error"] = f"{display}: timeout ({probe_timeout:g}s)"
        else:
            # httpx exceptions embed the full request URL — userinfo included
            # — so the rendered message is scrubbed against the raw url, then
            # against the configured header values (a 401 body can echo the
            # Authorization header back into the exception text).
            message = redact_exception_text(str(root), url) or type(root).__name__
            message = sanitize_secrets(message, list((headers or {}).values()))
            status["error"] = f"{display}: {message}"
    else:
        status.update(probe)
    return status


_LTM_MEASURE_SAMPLES = 5
_LTM_MEASURE_MAX_TIMEOUT_SECONDS = 30.0


async def _measure_warm_daemon_ltm(
    config: Any, *, initial_state: str, timeout: float
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run an explicitly requested, content-discarding warm-LTM measurement.

    A cold daemon gets one uncounted priming search.  Five unique synthetic
    searches then exercise the real daemon admission + LTM path.  The server
    owns the numeric rolling telemetry; this helper returns only run counters
    and a refreshed ping snapshot.  It never starts a missing daemon and never
    writes configuration or LTM content.
    """
    from memtomem_stm.daemon import client as daemon_client
    from memtomem_stm.daemon.protocol import OP_LTM_SEARCH

    surfacing = config.surfacing
    sample_timeout = min(
        _LTM_MEASURE_MAX_TIMEOUT_SECONDS,
        max(10.0, float(timeout), 2.0 * float(surfacing.timeout_seconds)),
    )
    payload_base: dict[str, Any] = {
        "top_k": int(surfacing.max_results),
        "namespace": surfacing.default_namespace,
        "context_window": int(surfacing.context_window_size),
    }

    async def one(label: str) -> tuple[str, dict[str, Any] | None]:
        payload = dict(payload_base)
        # Unique, synthetic text avoids a result-cache hit without exposing any
        # operator data.  Search is read-only and returned content is discarded.
        payload["query"] = f"memtomem stm latency probe {label} {asyncio.get_running_loop().time()}"
        return await daemon_client.ltm_request(
            config, OP_LTM_SEARCH, payload, timeout=sample_timeout
        )

    primed = initial_state != "warm"
    if primed:
        await one("prime")

    completed = 0
    timed_out = 0
    errors = 0
    attempted = 0
    for index in range(_LTM_MEASURE_SAMPLES):
        attempted += 1
        state, response = await one(f"sample-{index}")
        if state == "ok" and isinstance(response, dict):
            if response.get("status") == "expired":
                timed_out += 1
            elif response.get("ok") is True and response.get("outcome") in (
                "ok",
                "empty_results",
            ):
                completed += 1
            else:
                errors += 1
        elif state == "unavailable":
            timed_out += 1
        else:
            errors += 1
        if timed_out >= 2:
            break

    refreshed = await daemon_client.ping(config, timeout=max(2.0, min(sample_timeout, 5.0)))
    return (
        {
            "requested_samples": _LTM_MEASURE_SAMPLES,
            "attempted_samples": attempted,
            "completed_samples": completed,
            "timeout_samples": timed_out,
            "error_samples": errors,
            "primed": primed,
            "sample_timeout_seconds": sample_timeout,
        },
        refreshed,
    )


def _ltm_daemon_status(config: Any, timeout: float, *, measure_ltm: bool = False) -> dict[str, Any]:
    """Shared-daemon readiness and telemetry; active only with ``measure_ltm``."""
    from memtomem_stm.daemon import client as daemon_client

    surfacing = config.surfacing
    status: dict[str, Any] = {
        "route": "daemon",
        "transport": surfacing.ltm_mcp_transport,
        "command": surfacing.ltm_mcp_command,
        "args": list(surfacing.ltm_mcp_args),
        "url": redact_url_userinfo(surfacing.ltm_mcp_url),
        "display": "mms daemon",
        "connected": False,
        "version": None,
        "runtime_profile": None,
        "error": None,
        "daemon_reachable": False,
        "ltm_state": None,
        "latency": None,
        "measurement": None,
    }
    if not surfacing.enabled:
        status["skipped"] = "surfacing_disabled"
        return status
    hs = asyncio.run(daemon_client.ping(config, timeout=max(0.1, float(timeout))))
    if hs is None:
        status["error"] = "shared daemon is not reachable; run `mms daemon status`"
        return status
    state = str(hs.get("ltm") or "cold")
    if isinstance(hs.get("latency"), dict):
        status["latency"] = hs["latency"]
    core = hs.get("core")
    if isinstance(core, dict):
        profile = core.get("runtime_profile")
        if isinstance(profile, dict) and profile.get("schema_version") == 1:
            status["runtime_profile"] = profile
    if measure_ltm:
        measurement, refreshed = asyncio.run(
            _measure_warm_daemon_ltm(config, initial_state=state, timeout=timeout)
        )
        status["measurement"] = measurement
        if isinstance(refreshed, dict):
            state = str(refreshed.get("ltm") or state)
            if isinstance(refreshed.get("latency"), dict):
                status["latency"] = refreshed["latency"]
    status["daemon_reachable"] = True
    status["ltm_state"] = state
    if state == "warm":
        status["connected"] = True
    else:
        status["error"] = f"shared daemon is reachable but LTM is {state}"
    return status


def _ltm_status(
    config: Any,
    timeout: float,
    *,
    measure_ltm: bool = False,
    prefer_hook_daemon: bool = False,
) -> dict[str, Any]:
    # Standalone proxy surfacing and native hooks have independent routing
    # knobs. Health keeps describing the standalone route; doctor opts into
    # the hook daemon because its timeout advice governs hook→daemon traffic.
    if config.surfacing.use_daemon or (prefer_hook_daemon and config.hook.use_daemon):
        return _ltm_daemon_status(config, timeout, measure_ltm=measure_ltm)
    return _ltm_mcp_status(config.surfacing, timeout)


def _root_cause_exc(exc: BaseException) -> BaseException:
    """Walk into ``BaseExceptionGroup`` (anyio TaskGroup wraps probe failures
    as ``unhandled errors in a TaskGroup (N sub-exception)``) to surface the
    first non-group leaf so callers can dispatch on the real cause's type
    or message instead of the wrapper.
    """
    seen: set[int] = set()
    cur: BaseException = exc
    while isinstance(cur, BaseExceptionGroup) and cur.exceptions and id(cur) not in seen:
        seen.add(id(cur))
        cur = cur.exceptions[0]
    return cur


def _root_cause_message(exc: BaseException) -> str:
    cur = _root_cause_exc(exc)
    return str(cur) or type(cur).__name__


# Values shorter than this are not treated as redactable secrets: a 1-3 char
# token like "k", "1", or "on" occurs incidentally all over ordinary
# diagnostic text, so redacting it globally corrupts the message far more than
# it protects — and a secret that short isn't meaningfully protectable anyway.
_MIN_REDACTABLE_SECRET_LEN = 4


def _probe_secret_values(cfg: dict[str, Any]) -> list[str]:
    """Secret-bearing values from a raw server config dict.

    Everything ``sanitize_secrets`` must scrub out of a probe failure
    message: all ``env`` values (stdio child environment), all ``headers``
    values (HTTP auth), and the URL userinfo. SDK/validation exceptions
    interpolate these verbatim (a 401 body can echo the Authorization
    header back), and the mapping redactors only cover structured output.

    Trivially short values are dropped (see ``_MIN_REDACTABLE_SECRET_LEN``)
    so redaction can't mangle ordinary diagnostic text.
    """
    values: list[str] = []
    for key in ("env", "headers"):
        mapping = cfg.get(key)
        if isinstance(mapping, dict):
            values.extend(str(v) for v in mapping.values())
    url = cfg.get("url", "")
    if isinstance(url, str) and url:
        try:
            userinfo = urlsplit(url).netloc.rpartition("@")[0]
        except ValueError:
            userinfo = ""
        if userinfo:
            # Add the combined ``user:pass`` plus the individual username and
            # password (and their percent-decoded forms): an exception may
            # report only one component, or a form the SDK URL-decoded, so
            # the combined string alone wouldn't match.
            for part in (userinfo, *userinfo.split(":", 1)):
                if part:
                    values.append(part)
                    decoded = unquote(part)
                    if decoded != part:
                        values.append(decoded)
    return [v for v in values if len(v) >= _MIN_REDACTABLE_SECRET_LEN]


def _sanitize_probe_error(text: str, cfg: dict[str, Any]) -> str:
    """Sanitize a probe failure message against *cfg*'s secret values.

    ``redact_exception_text`` first (it rewrites full-URL forms httpx embeds,
    keeping the host readable), then ``sanitize_secrets`` for the raw
    env/header values themselves.
    """
    url = cfg.get("url", "")
    if isinstance(url, str) and url:
        text = redact_exception_text(text, url)
    return sanitize_secrets(text, _probe_secret_values(cfg))


def _all_config_secret_values(data: dict[str, Any]) -> list[str]:
    """Every secret value across all configured upstream servers.

    Used to scrub free-form diagnostics that aren't bound to one server —
    a pydantic schema-validation error can embed the rejected ``input_value``
    (or a validator message quoting it), so a malformed ``headers``/``env``
    value could otherwise reach ``mms doctor`` output.
    """
    servers = data.get("upstream_servers", {})
    if not isinstance(servers, dict):
        return []
    values: list[str] = []
    for cfg in servers.values():
        if isinstance(cfg, dict):
            values.extend(_probe_secret_values(cfg))
    return values


def _surfacing_bootstrap_status(
    timeout: float,
    *,
    measure_ltm: bool = False,
    prefer_hook_daemon: bool = False,
) -> dict[str, Any]:
    """Return surfacing bootstrap readiness without starting the proxy.

    *timeout* is the ``mms health --timeout`` value, forwarded into the LTM
    probe so the documented per-server bound covers it too.
    """
    try:
        from memtomem_stm.config import STMConfig
        from memtomem_stm.surfacing.feedback_store import (
            inspect_feedback_db,
            read_surfacing_summary,
        )

        config = STMConfig()
        surfacing = config.surfacing
        db_status = inspect_feedback_db(surfacing.feedback_db_path)
        return {
            "enabled": surfacing.enabled,
            "feedback_enabled": surfacing.feedback_enabled,
            "feedback_db": db_status,
            "feedback_summary": read_surfacing_summary(surfacing.feedback_db_path),
            "ltm_server": _ltm_status(
                config,
                timeout,
                measure_ltm=measure_ltm,
                prefer_hook_daemon=prefer_hook_daemon,
            ),
            "timeouts": {
                "surfacing_seconds": float(surfacing.timeout_seconds),
                "hook_daemon_seconds": float(config.hook.daemon_timeout_seconds),
            },
        }
    except Exception as exc:
        logger.debug("Surfacing bootstrap status inspection failed", exc_info=True)
        return {
            "enabled": None,
            "feedback_enabled": None,
            "feedback_db": None,
            "ltm_server": None,
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
    ltm_server = status.get("ltm_server")
    if isinstance(ltm_server, dict):
        if ltm_server.get("skipped") == "surfacing_disabled":
            lines.append("  ltm server: skipped (surfacing disabled)")
        elif ltm_server.get("connected"):
            detail = str(ltm_server.get("display") or ltm_server.get("command") or "")
            if ltm_server.get("version"):
                detail = f"{detail}, version {ltm_server['version']}"
            lines.append(f"  ltm server: {_ok('connectable')} ({detail})")
        else:
            err = ltm_server.get("error") or "unknown error"
            label = "NOT READY" if ltm_server.get("daemon_reachable") else "UNREACHABLE"
            lines.append(f"  ltm server: {_bad(label)} — {err}")
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


async def _probe_servers(servers: dict[str, Any], timeout: float) -> dict[str, StagedProbeResult]:
    """Probe all servers in parallel, returning per-server staged results."""

    async def _safe_probe(name: str, cfg: dict[str, Any]) -> tuple[str, StagedProbeResult]:
        try:
            result = await _probe_one(cfg, timeout)
        except Exception as exc:
            # ``_probe_one`` classifies its own failures; this is a
            # last-resort guard so one server's unexpected bug can't kill
            # the whole ``gather``.
            result = StagedProbeResult(
                stage=ProbeStage.CONFIGURED,
                transport=str(cfg.get("transport", "stdio")),
                error=_sanitize_probe_error(_root_cause_message(exc), cfg),
            )
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

    from memtomem_stm.server import _hidden_obs_tools_hint

    data = _load(path)
    servers: dict[str, Any] = data.get("upstream_servers", {})
    surfacing_status = _surfacing_bootstrap_status(float(timeout))
    config_error = _schema_validation_error(data)
    if config_error:
        # A schema error can echo the rejected input_value (or a validator
        # message quoting it), so scrub it against configured server secrets
        # before it lands in text/--json output.
        config_error = sanitize_secrets(config_error, _all_config_secret_values(data))
    logging_status = _logging_destination_status()
    obs_tools_hint = _hidden_obs_tools_hint()

    # JSON output format matches ``status --json`` / ``list --json`` (indent=2,
    # ensure_ascii=False) so scripts piping the three commands through the
    # same formatter don't hit one compact-one-line outlier.
    if not servers:
        if as_json:
            click.echo(
                _json_dumps(
                    {
                        "servers": {},
                        "config_valid": config_error is None,
                        "config_error": config_error,
                        "surfacing": surfacing_status,
                        "logging": logging_status,
                        "obs_tools_hidden": obs_tools_hint is not None,
                        "obs_tools_hint": obs_tools_hint,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            if config_error:
                click.echo(f"{_warn('Warning:')} {_CONFIG_INVALID_WARNING}: {_disp(config_error)}")
            click.echo("No upstream servers configured.")
            click.echo("")
            for line in _format_surfacing_bootstrap(surfacing_status):
                click.echo(line)
            click.echo(_format_logging_destination(logging_status))
            if obs_tools_hint:
                click.echo(obs_tools_hint)
        return

    results = asyncio.run(_probe_servers(servers, timeout))

    if as_json:
        click.echo(
            _json_dumps(
                {
                    # Legacy probe keys plus additive ``stage`` /
                    # ``failed_stage`` / ``transport`` — scripts written
                    # against the pre-staged shape keep working.
                    "servers": {n: r.as_dict() for n, r in results.items()},
                    "config_valid": config_error is None,
                    "config_error": config_error,
                    "surfacing": surfacing_status,
                    "logging": logging_status,
                    "obs_tools_hidden": obs_tools_hint is not None,
                    "obs_tools_hint": obs_tools_hint,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    if config_error:
        click.echo(f"{_warn('Warning:')} {_CONFIG_INVALID_WARNING}: {_disp(config_error)}")
    click.echo(_hdr("Upstream Server Health"))
    click.echo("=" * 30)
    for name, info in results.items():
        if info.connected:
            click.echo(f"  {_disp(name)}: {_ok('connected')} ({info.tools} tools)")
            if show_names:
                if info.overflowing:
                    click.echo(
                        f"    {_warn('overflow:')} {len(info.overflowing)} tool(s) "
                        f"would exceed the {tool_name_budget.TOOL_NAME_LIMIT}-char "
                        f"client limit and will be silently dropped:"
                    )
                    for t_name in info.overflowing:
                        # Advertised by the upstream over ``tools/list``, so
                        # unlike the config-derived names elsewhere in this
                        # block nobody on this machine ever typed or reviewed
                        # them (#755).
                        click.echo(f"      - {_disp(t_name)}")
                else:
                    click.echo("    all tool names fit")
        else:
            click.echo(
                f"  {_disp(name)}: {_bad('DISCONNECTED')} — {_disp(info.error or '')} "
                f"(last successful stage: {info.stage.display()})"
            )
    click.echo("")
    for line in _format_surfacing_bootstrap(surfacing_status):
        click.echo(line)
    click.echo(_format_logging_destination(logging_status))
    if obs_tools_hint:
        click.echo(obs_tools_hint)


# ── doctor command ──────────────────────────────────────────────────────


_DOCTOR_STYLES = {"PASS": _ok, "WARN": _warn, "FAIL": _bad}


def _runtime_profile_doctor_checks(profile: Any) -> list[tuple[str, str, str, str, str | None]]:
    """Translate the additive core runtime profile into stable doctor checks."""
    if not isinstance(profile, dict):
        return [
            (
                "ltm_runtime_profile",
                "ltm runtime profile",
                "WARN",
                "connected core does not expose runtime_profile schema 1",
                "upgrade memtomem core, then restart the LTM/daemon",
            )
        ]
    if profile.get("config_state") != "ok":
        return [
            (
                "ltm_runtime_profile",
                "ltm runtime profile",
                "FAIL",
                "core could not read its effective configuration",
                "run `mm status` in the LTM environment and repair its config",
            )
        ]

    checks: list[tuple[str, str, str, str, str | None]] = []
    dependencies = profile.get("dependencies")
    dependencies = dependencies if isinstance(dependencies, dict) else {}
    fastembed = dependencies.get("fastembed")
    kiwi = dependencies.get("kiwipiepy")
    missing = profile.get("missing_extras")
    missing = missing if isinstance(missing, list) else []
    if "onnx" in missing or (
        isinstance(fastembed, dict)
        and fastembed.get("required_for")
        and not fastembed.get("available")
    ):
        checks.append(
            (
                "ltm_dependencies",
                "ltm dependencies",
                "FAIL",
                "fastembed is required by the active embedding/rerank config but is not installed",
                "install `memtomem[onnx]` in the LTM server environment and restart it",
            )
        )
    elif "korean" in missing or (
        isinstance(kiwi, dict) and kiwi.get("required_for") and not kiwi.get("available")
    ):
        checks.append(
            (
                "ltm_dependencies",
                "ltm dependencies",
                "WARN",
                "kiwipiepy tokenizer is configured but its extra is not installed",
                "install `memtomem[korean]` in the LTM server environment",
            )
        )
    else:
        checks.append(
            ("ltm_dependencies", "ltm dependencies", "PASS", "required extras available", None)
        )

    search = profile.get("search")
    configured_mode = search.get("configured_mode") if isinstance(search, dict) else None
    effective_mode = (
        search.get("effective_mode", configured_mode) if isinstance(search, dict) else None
    )
    if effective_mode == "disabled":
        checks.append(
            (
                "ltm_retrieval_mode",
                "ltm retrieval mode",
                "FAIL",
                "effective retrieval mode is disabled",
                "enable a working dense embedding provider, then re-index vectors",
            )
        )
    elif effective_mode == "bm25_only" and configured_mode != "bm25_only":
        checks.append(
            (
                "ltm_retrieval_mode",
                "ltm retrieval mode",
                "FAIL",
                f"configured mode {configured_mode} degraded to effective mode bm25_only",
                "restore the configured dense embedding provider, then re-index vectors",
            )
        )
    elif effective_mode == "bm25_only":
        checks.append(
            (
                "ltm_retrieval_mode",
                "ltm retrieval mode",
                "WARN",
                "intentional BM25-only configuration; semantic surfacing is unavailable",
                "enable a dense embedding provider and re-index vectors for semantic retrieval",
            )
        )
    elif effective_mode in {"hybrid", "dense_only"}:
        checks.append(
            ("ltm_retrieval_mode", "ltm retrieval mode", "PASS", str(effective_mode), None)
        )
    else:
        checks.append(
            (
                "ltm_retrieval_mode",
                "ltm retrieval mode",
                "WARN",
                "runtime profile did not report a recognized retrieval mode",
                None,
            )
        )
    return checks


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
    "--measure-ltm",
    is_flag=True,
    help=(
        "Actively run five synthetic searches against an existing shared daemon "
        "to refresh warm-LTM latency advice. Never starts a missing daemon."
    ),
)
def doctor(
    config_path: str,
    *,
    as_json: bool = False,
    timeout: int = 10,
    measure_ltm: bool = False,
) -> None:
    """Diagnose the proxy setup end-to-end (passive unless measuring LTM).

    Runs the existing status/health/config checks as one PASS/WARN/FAIL
    report with a copy-paste next action per failing check. Exit code is 1
    when any check FAILs; WARN-only runs exit 0 — `mms doctor` passing is
    the quickstart success gate, so it must be scriptable. `health` stays
    the always-exit-0 inspection command; strict config linting stays in
    `mms config validate`. The default never modifies state or runs a search.
    ``--measure-ltm`` explicitly authorizes read-only synthetic searches against
    an already running daemon; it still never edits configuration or LTM data.
    """
    path = Path(config_path)
    resolved = path.expanduser().resolve()
    # Only the path token needs shell-safe rendering; the surrounding hints are
    # templates with literal ``<name>``/``<prefix>`` metavariables that must not
    # be quoted, so quote just the path (platform-aware) rather than the whole
    # command. ``_shell_join`` on a one-item list yields the correctly-quoted
    # token for the native shell (cmd.exe vs POSIX).
    cfg_arg = f"--config {_shell_join([str(resolved)])}"

    checks: list[dict[str, Any]] = []

    def check(
        check_id: str, label: str, status: str, detail: str, next_action: str | None = None
    ) -> None:
        checks.append(
            {
                "id": check_id,
                "label": label,
                "status": status,
                "detail": detail,
                "next_action": next_action,
            }
        )

    servers_payload: dict[str, Any] | None = None
    surfacing_status: dict[str, Any] | None = None

    # 1. config file exists — every later check reads it, so FAIL
    # short-circuits the report instead of cascading noise.
    if not resolved.exists():
        check("config_file", "config file", "FAIL", f"not found: {resolved}", "mms init")
    else:
        check("config_file", "config file", "PASS", str(resolved))

        # 2. JSON validity — mirrors `mms config validate`'s parse guard
        # (NOT `_load`, which SystemExits with its own styled message).
        data: dict[str, Any] | None = None
        try:
            loaded = json.loads(resolved.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
                check("config_json", "config JSON", "PASS", "parses as an object")
            else:
                check(
                    "config_json",
                    "config JSON",
                    "FAIL",
                    f"root must be a JSON object, got {type(loaded).__name__}",
                    f"mms config validate {cfg_arg}",
                )
        except json.JSONDecodeError as exc:
            check(
                "config_json",
                "config JSON",
                "FAIL",
                f"invalid JSON: {exc}",
                f"mms config validate {cfg_arg}",
            )
        except OSError as exc:
            check(
                "config_json",
                "config JSON",
                "FAIL",
                f"cannot read file: {exc}",
                f"mms config validate {cfg_arg}",
            )

        if data is not None:
            # 3. schema validation — same lazy-pydantic path `status`/`health`
            # warn through; here it FAILs (a running server would silently
            # fall back to env/defaults) but does NOT short-circuit: the
            # transport/prefix/probe checks operate on the raw dicts and
            # stay meaningful.
            schema_error = _schema_validation_error(data)
            if schema_error:
                # A pydantic error can echo the rejected input_value (or a
                # validator message quoting it), so scrub it against every
                # configured server's secrets before it reaches the report.
                schema_error = sanitize_secrets(schema_error, _all_config_secret_values(data))
                check(
                    "config_schema",
                    "config schema",
                    "FAIL",
                    f"{_CONFIG_INVALID_WARNING}: {schema_error}",
                    f"mms config validate {cfg_arg}",
                )
            else:
                check("config_schema", "config schema", "PASS", "valid")

            raw_servers = data.get("upstream_servers", {})
            servers = (
                {n: c for n, c in raw_servers.items() if isinstance(c, dict)}
                if isinstance(raw_servers, dict)
                else {}
            )

            if servers:
                # 4. per-transport required fields — shared with `add`
                # VAL-3/VAL-4 via `_transport_field_error`.
                transport_problems = [
                    f"{n}: {terr}"
                    for n, cfg in servers.items()
                    if (
                        terr := _transport_field_error(
                            str(cfg.get("transport", "stdio") or "stdio"),
                            str(cfg.get("command", "") or ""),
                            str(cfg.get("url", "") or ""),
                        )
                    )
                ]
                if transport_problems:
                    check(
                        "server_transports",
                        "server transports",
                        "FAIL",
                        "; ".join(transport_problems),
                        f"mms remove <name> {cfg_arg}  "
                        "# then re-add with --command (stdio) or --url (sse/http)",
                    )
                else:
                    check(
                        "server_transports",
                        "server transports",
                        "PASS",
                        f"{len(servers)} server(s) have their required connection fields",
                    )

                # 5. prefixes — same shared validators the runtime load path
                # uses (`proxy/prefixes.py`), so doctor can't disagree with
                # what the server would refuse.
                prefix_map = {n: str(cfg.get("prefix", "") or "") for n, cfg in servers.items()}
                prefix_problems = []
                empty = prefixes.empty_prefix_keys(prefix_map)
                if empty:
                    prefix_problems.append(f"empty prefix: {', '.join(empty)}")
                collisions = prefixes.prefix_collisions(prefix_map)
                if collisions:
                    prefix_problems.append(prefixes.format_collision_error(collisions))
                if prefix_problems:
                    check(
                        "prefixes",
                        "prefixes",
                        "FAIL",
                        "; ".join(prefix_problems),
                        f"mms remove <name> {cfg_arg}  # then re-add with a unique --prefix",
                    )
                else:
                    check("prefixes", "prefixes", "PASS", "unique and non-empty")

                # 6. staged probe per server — the shared StagedProbeResult
                # `health` renders; doctor adds the per-stage next action.
                results = asyncio.run(_probe_servers(servers, timeout))
                servers_payload = {n: r.as_dict() for n, r in results.items()}
                for n, r in results.items():
                    if r.connected:
                        check(f"upstream:{n}", f"upstream: {n}", "PASS", f"{r.tools} tool(s)")
                        continue
                    detail = f"failed after '{r.stage.display()}' — {r.error}"
                    timed_out = (r.error or "").startswith("timeout")
                    if (
                        r.transport == "stdio"
                        and r.stage is ProbeStage.CONFIGURED
                        and not timed_out
                    ):
                        # Transport-neutral stage names; the child-process
                        # detail is stdio-only rendering, not a stage.
                        detail += " (stdio child process did not start)"
                        command = str(servers[n].get("command", ""))
                        next_cmd = (
                            _shell_join(["where.exe", command])
                            if os.name == "nt"
                            else _shell_join(["command", "-v", command])
                        )
                    elif timed_out:
                        next_cmd = f"mms health --timeout {max(30, timeout)} {cfg_arg}"
                    else:
                        next_cmd = f"mms health {cfg_arg}"
                    check(f"upstream:{n}", f"upstream: {n}", "FAIL", detail, next_cmd)
            else:
                check(
                    "upstreams",
                    "upstreams",
                    "WARN",
                    "no upstream servers configured",
                    f"mms add <name> --prefix <prefix> --command <command> {cfg_arg}",
                )

            # Downstream host registration: config + healthy upstreams are
            # insufficient if no MCP client launches this gateway.
            registered_with: list[str] = []
            if shutil.which("codex") and _codex_registered():
                registered_with.append("Codex")
            if shutil.which("claude") and _check_already_registered():
                registered_with.append("Claude Code")
            project_mcp = _read_json_safely(Path.cwd() / ".mcp.json") or {}
            project_servers = project_mcp.get("mcpServers") or {}
            if isinstance(project_servers, dict) and "memtomem-stm" in project_servers:
                registered_with.append("project .mcp.json")
            if registered_with:
                check(
                    "host_registration",
                    "host registration",
                    "PASS",
                    ", ".join(registered_with),
                )
            else:
                check(
                    "host_registration",
                    "host registration",
                    "WARN",
                    "not detected in Codex, Claude Code, or .mcp.json",
                    f"mms register --client auto {cfg_arg}",
                )

            # 7. cache policy — same condition + shared predicate as
            # `mms config validate` and the runtime load advisory (#658).
            from memtomem_stm.proxy.config import _has_annotation_policy

            cache = data.get("cache")
            cache_enabled = cache.get("enabled", True) if isinstance(cache, dict) else True
            if not cache_enabled:
                check("cache_policy", "cache policy", "PASS", "cache disabled")
            elif _has_annotation_policy(data):
                policy = cache["tool_annotation_policy"] if isinstance(cache, dict) else None
                check("cache_policy", "cache policy", "PASS", f"tool_annotation_policy={policy}")
            else:
                check(
                    "cache_policy",
                    "cache policy",
                    "WARN",
                    "cache.tool_annotation_policy not set — using the 'conservative' "
                    "default (unclassified tools are cached); new configs are created "
                    "with 'strict'",
                    'add "cache": {"tool_annotation_policy": "strict"} to '
                    f'{resolved}  # or "conservative" to pin current behavior',
                )

            # 8. Tuning readiness — read-only.  This is a discoverability
            # hint, not an automatic config mutation; the existing
            # preview/apply boundary remains authoritative in ``mms tune``.
            tuning = _tuning_readiness(data)
            ready_tools = tuning["tools"]
            if tuning["ready"]:
                check(
                    "tuning",
                    "compression tuning",
                    "PASS",
                    f"{len(ready_tools)} tool(s) have at least "
                    f"{tuning['sample_threshold']} samples",
                    f"mms tune {cfg_arg}",
                )
            elif tuning["available"]:
                check(
                    "tuning",
                    "compression tuning",
                    "PASS",
                    f"collecting per-tool samples (need {tuning['sample_threshold']})",
                )
            else:
                check(
                    "tuning",
                    "compression tuning",
                    "PASS",
                    "no proxy metrics recorded yet",
                )

            # 9. LTM server — never FAIL: LTM is optional, and an unreachable
            # or unconfigured LTM only disables surfacing, not the proxy
            # core. A FAIL here would break the exit-code gate on every
            # fresh install without a memtomem server.
            surfacing_status = _surfacing_bootstrap_status(
                float(timeout),
                measure_ltm=measure_ltm,
                prefer_hook_daemon=True,
            )
            ltm = surfacing_status.get("ltm_server")
            if isinstance(ltm, dict) and ltm.get("connected"):
                detail = str(ltm.get("display") or ltm.get("command") or "")
                if ltm.get("version"):
                    detail = f"{detail}, version {ltm['version']}"
                check("ltm", "ltm server", "PASS", f"connectable ({detail})")
                for runtime_check in _runtime_profile_doctor_checks(ltm.get("runtime_profile")):
                    check(*runtime_check)

                feedback_summary = surfacing_status.get("feedback_summary")
                if isinstance(feedback_summary, dict):
                    active = feedback_summary.get("active_diagnostics")
                    supported = feedback_summary.get("diagnostics_recovery_supported", True)
                    if isinstance(active, dict) and active.get("score_scale_mismatch"):
                        check(
                            "ltm_score_scale",
                            "ltm score scale",
                            "FAIL",
                            "core reports a non-RRF score scale while min_score assumes RRF "
                            "(unrecovered score_scale_mismatch episode in the last 7 UTC days)",
                            "set surfacing.scale_gated_min_score=true (default) to suspend the "
                            "RRF-calibrated filter on core-named non-RRF scales, or adjust/"
                            "remove the context_tools.<tool>.min_score pin keeping it active; "
                            "if the scale is 'rerank', also check surfacing.rerank (default "
                            "false returns RRF scores)",
                        )
                    elif isinstance(active, dict) and active.get("score_ceiling_below_min"):
                        check(
                            "ltm_score_scale",
                            "ltm score scale",
                            "FAIL",
                            "unrecovered score_ceiling_below_min episode in the last 7 UTC days",
                            "verify dense embeddings and min_score, then run a successful warm search",
                        )
                    elif not supported:
                        check(
                            "ltm_score_scale",
                            "ltm score scale",
                            "WARN",
                            "feedback DB predates recovery tracking; daemon initialization will migrate it",
                        )
                    else:
                        check(
                            "ltm_score_scale",
                            "ltm score scale",
                            "PASS",
                            "no active mismatch episode",
                        )
            else:
                if isinstance(ltm, dict) and ltm.get("skipped") == "surfacing_disabled":
                    cause = "surfacing disabled"
                elif isinstance(ltm, dict) and ltm.get("error"):
                    cause = str(ltm["error"])
                else:
                    cause = str(surfacing_status.get("error") or "status unavailable")
                check(
                    "ltm",
                    "ltm server",
                    "WARN",
                    f"{cause} — only LTM-dependent features (memory surfacing) are "
                    "disabled; the proxy core is unaffected",
                    (
                        "mms daemon status"
                        if isinstance(ltm, dict) and ltm.get("route") == "daemon"
                        else "export MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND=<memtomem-server>"
                        "  # see docs/surfacing.md"
                    ),
                )

            # 9. Warm-daemon timeout advice.  Ping telemetry is passive; the
            # optional measurement above is the only path that executes a
            # synthetic search.  These are WARN-only operational checks: an
            # undersized budget degrades surfacing but never breaks proxying.
            if measure_ltm and (
                not isinstance(ltm, dict)
                or ltm.get("route") != "daemon"
                or ltm.get("daemon_reachable") is False
            ):
                check(
                    "ltm_measurement",
                    "ltm measurement",
                    "WARN",
                    "--measure-ltm requires surfacing.use_daemon=true and a running daemon",
                    "export MEMTOMEM_STM_SURFACING__USE_DAEMON=true; mms daemon start",
                )
            elif isinstance(ltm, dict) and ltm.get("route") == "daemon":
                measurement = ltm.get("measurement")
                if isinstance(measurement, dict):
                    completed = int(measurement.get("completed_samples", 0) or 0)
                    attempted = int(measurement.get("attempted_samples", 0) or 0)
                    measurement_status = "PASS" if completed else "WARN"
                    check(
                        "ltm_measurement",
                        "ltm measurement",
                        measurement_status,
                        f"{completed}/{attempted} warm sample(s) completed",
                        None if completed else "mms doctor --measure-ltm --timeout 30",
                    )

                latency = ltm.get("latency")
                timeout_cfg = surfacing_status.get("timeouts")
                if isinstance(latency, dict) and isinstance(timeout_cfg, dict):
                    surface_summary = latency.get("surface")
                    retrieval_summary = latency.get("retrieval")

                    def recommendation(summary: Any) -> dict[str, Any] | None:
                        if not isinstance(summary, dict):
                            return None
                        value = summary.get("recommendation")
                        return value if isinstance(value, dict) else None

                    surface_rec = recommendation(surface_summary)
                    retrieval_rec = recommendation(retrieval_summary)
                    # Hook traffic populates ``surface``; explicit low-level
                    # measurement populates ``retrieval``. Prefer the former
                    # when ready, otherwise use the fresh retrieval estimate.
                    chosen_rec = (
                        surface_rec
                        if isinstance(surface_rec, dict)
                        and isinstance(surface_rec.get("seconds"), (int, float))
                        else retrieval_rec
                    )
                    surfacing_current = float(timeout_cfg.get("surfacing_seconds", 0.0))
                    recommended = (
                        float(chosen_rec["seconds"])
                        if isinstance(chosen_rec, dict)
                        and isinstance(chosen_rec.get("seconds"), (int, float))
                        else None
                    )
                    recommendation_status = (
                        str(chosen_rec.get("status", "unknown"))
                        if isinstance(chosen_rec, dict)
                        else "unknown"
                    )
                    if isinstance(chosen_rec, dict) and chosen_rec.get("status") == "too_slow":
                        check(
                            "surfacing_timeout",
                            "surfacing timeout",
                            "WARN",
                            "warm LTM requires more than the 30s operational ceiling; "
                            "fix LTM performance before increasing timeout",
                            "mms doctor --measure-ltm --timeout 30",
                        )
                    elif recommended is None:
                        samples = max(
                            int((surface_summary or {}).get("samples", 0) or 0)
                            if isinstance(surface_summary, dict)
                            else 0,
                            int((retrieval_summary or {}).get("samples", 0) or 0)
                            if isinstance(retrieval_summary, dict)
                            else 0,
                        )
                        check(
                            "surfacing_timeout",
                            "surfacing timeout",
                            "PASS",
                            f"collecting telemetry ({samples}/5 successful warm samples)",
                            "mms doctor --measure-ltm" if samples < 5 else None,
                        )
                    elif surfacing_current < recommended:
                        check(
                            "surfacing_timeout",
                            "surfacing timeout",
                            "WARN",
                            f"configured {surfacing_current:g}s; warm-daemon telemetry "
                            f"recommends {recommended:g}s ({recommendation_status})",
                            f"export MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS={recommended:g}",
                        )
                    else:
                        check(
                            "surfacing_timeout",
                            "surfacing timeout",
                            "PASS",
                            f"configured {surfacing_current:g}s >= observed recommendation "
                            f"{recommended:g}s",
                        )

                    from memtomem_stm.daemon.latency import hook_timeout_recommendation

                    effective_surfacing = max(surfacing_current, recommended or 0.0)
                    hook_recommended = hook_timeout_recommendation(
                        surfacing_timeout_seconds=effective_surfacing,
                        surface_summary=(
                            surface_summary if isinstance(surface_summary, dict) else None
                        ),
                    )
                    hook_current = float(timeout_cfg.get("hook_daemon_seconds", 0.0))
                    if hook_current < hook_recommended:
                        check(
                            "hook_daemon_timeout",
                            "hook daemon timeout",
                            "WARN",
                            f"configured {hook_current:g}s; outer hook deadline truncates the "
                            f"inner surfacing budget (need >= {hook_recommended:g}s)",
                            "export MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS="
                            f"{hook_recommended:g}",
                        )
                    else:
                        check(
                            "hook_daemon_timeout",
                            "hook daemon timeout",
                            "PASS",
                            f"configured {hook_current:g}s >= required {hook_recommended:g}s",
                        )

    counts = {status: sum(1 for c in checks if c["status"] == status) for status in _DOCTOR_STYLES}
    overall = "fail" if counts["FAIL"] else ("warn" if counts["WARN"] else "pass")

    if as_json:
        payload: dict[str, Any] = {
            "config_path": str(resolved),
            "status": overall,
            "checks": checks,
        }
        if servers_payload is not None:
            payload["servers"] = servers_payload
        if surfacing_status is not None:
            payload["surfacing"] = surfacing_status
        click.echo(_json_dumps(payload, indent=2, ensure_ascii=False))
    else:
        click.echo(_hdr(f"Doctor: {resolved}"))
        click.echo("=" * 30)
        for c in checks:
            styled = _DOCTOR_STYLES[c["status"]](c["status"])
            # Escape at render, not in ``check()``: the same dicts are the
            # ``--json`` payload above, where a consumer decodes the value
            # rather than reading it off a terminal (#755). ``next_action``
            # is exempt — every one is a literal template or goes through
            # ``_shell_join``, which refuses these characters outright.
            click.echo(f"  {styled}  {_disp(c['label']):<18} {_disp(c['detail'])}")
            if c["next_action"]:
                click.echo(f"        next: {c['next_action']}")
        click.echo(f"Summary: {counts['FAIL']} FAIL, {counts['WARN']} WARN, {counts['PASS']} PASS")

    if counts["FAIL"]:
        sys.exit(1)


# `mms project ...` — RFC §7.1, lives in src/memtomem_stm/cli/mms_project.py
# to keep this file from accreting another ~700 lines for the W1 surface.
cli.add_command(_mms_project_group)

# `mms import ...` — RFC §7.2, lives in src/memtomem_stm/cli/mms_import.py.
cli.add_command(_mms_import_command)

# `mms host ...` — RFC §7.3, lives in src/memtomem_stm/cli/mms_host.py.
cli.add_command(_mms_host_group)

# `mms hook` — bridge a host's built-in tool calls (Claude Code PostToolUse)
# into STM surfacing; lives in src/memtomem_stm/cli/hook_cmd.py.
cli.add_command(_hook_command)

# `mms daemon ...` — manage the local surfacing daemon (Stage 2 warm LTM
# connection for `mms hook`); lives in src/memtomem_stm/cli/daemon_cmd.py.
cli.add_command(_daemon_group)

# `mms config ...` — strict config-file linting (#611); lives in
# src/memtomem_stm/cli/config_cmd.py.
cli.add_command(_config_group)

# `mms selection replay` — offline selection-log validation + labelled
# relevance/safety evaluation (#468), deliberately CLI-only (no MCP file-read
# surface). Lives in its own module like the other nested command families.
cli.add_command(_selection_group)
