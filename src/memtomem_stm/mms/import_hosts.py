"""Host config scanners for ``mms import`` (RFC §7.2).

Reads MCP definitions out of four host config formats and produces
:class:`ImportCandidate` entries shaped for the new mms registry
(``~/.mms/registry.toml`` per RFC §5.3).

Hosts:

* ``claude-code`` — ``~/.claude.json`` (user + per-project under
  ``.projects.<cwd>.mcpServers``) + ``<cwd>/.mcp.json``
* ``cursor`` — ``~/.cursor/mcp.json`` + ``<cwd>/.cursor/mcp.json``
* ``codex`` — ``~/.codex/config.toml`` (``[mcp_servers.<name>]``)
* ``claude-desktop`` — macOS only,
  ``~/Library/Application Support/Claude/claude_desktop_config.json``

Linux/Windows host paths are out of W1 scope; missing files are
treated as "no candidates" (silent), and the ``mms import`` command
prints which hosts were scanned vs found.

This module also owns the small set of *shape-agnostic* helpers that
were previously private to ``cli/proxy.py``:
``_read_json_safely`` / ``_desktop_config_path`` /
``_BLOCKED_IMPORT_NAMES`` / ``_is_self_reference`` /
``_DANGEROUS_ENV_KEYS``. ``cli/proxy.py``'s ``mms add --from-clients``
flow re-imports them from here so there is one definition per primitive.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memtomem_stm.mms.secrets import Classification, classify_env
from memtomem_stm.mms.state import RegistryServer

# ---------------------------------------------------------------------------
# Shape-agnostic primitives (re-exported for cli/proxy.py)
# ---------------------------------------------------------------------------

# Environment variable names that could enable code injection via subprocess.
# Filtered out of every imported entry — never persisted to registry.toml or
# stm_proxy.json regardless of which host they came from.
_DANGEROUS_ENV_KEYS: frozenset[str] = frozenset(
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

# Names that should never appear as STM upstream servers or mms-managed MCPs:
# importing STM itself would proxy STM through STM (recursive), and the LTM
# companion is reached via a separate mechanism (CLAUDE.md "pipeline" rules).
_BLOCKED_IMPORT_NAMES: frozenset[str] = frozenset(
    {
        "mms",
        "memtomem-stm",
        "memtomem-stm-proxy",
        "memtomem",
        "memtomem-server",
    }
)


def _desktop_config_path() -> Path:
    """Claude Desktop's macOS config path. Linux/Windows variants out of W1."""
    return Path("~/Library/Application Support/Claude/claude_desktop_config.json").expanduser()


def _read_json_safely(path: Path) -> dict[str, Any] | None:
    """Read JSON, return None on any I/O or parse error (best-effort discovery)."""
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_toml_safely(path: Path) -> dict[str, Any] | None:
    """Read TOML, return None on any I/O or parse error (Codex config)."""
    try:
        if not path.exists():
            return None
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _is_self_reference(entry: dict[str, Any]) -> bool:
    """Skip entries that would make STM proxy itself or double-register LTM.

    Examines both the command basename and every argv token (exact
    match, not substring) because wrappers like ``uvx --from memtomem
    memtomem-server`` carry the blocked name only in ``args``.
    """
    cmd = (entry.get("command") or "").lower()
    if cmd and Path(cmd).name in _BLOCKED_IMPORT_NAMES:
        return True
    args = entry.get("args") or []
    if isinstance(args, list):
        for tok in args:
            if isinstance(tok, str) and tok.lower() in _BLOCKED_IMPORT_NAMES:
                return True
    return False


# ---------------------------------------------------------------------------
# Registry-shape import candidate
# ---------------------------------------------------------------------------


# RFC §5.3 prefix shape: starts with letter, then [a-zA-Z0-9_]*.
# Mirrors proxy/prefixes.py's PREFIX_RE — kept here so the mms registry
# package stays free of imports from the proxy package.
_PREFIX_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


@dataclass(frozen=True)
class ImportCandidate:
    """One MCP definition discovered in a host config.

    ``server`` is the registry-shape persistence object (will be
    written to ``registry.toml`` under ``[servers.<name>]``).
    ``env_classification`` is the per-key secret classification used by
    ``--plan`` to decide which env values to redact in output.
    ``source_label`` is what ``mms import --plan`` prints next to the
    name — e.g. ``"Claude Code (user)"``.

    ``is_repo_local`` marks candidates that came from a config file *under
    the current working directory* (``<cwd>/.mcp.json`` or
    ``<cwd>/.cursor/mcp.json``) — files an untrusted repository checkout can
    contain. It gates registration in ``mms import``/``mms host sync``
    (``--allow-project-configs``). It is keyed on the source *file location*,
    not the ``source_label``: ``"Claude Code (project)"`` candidates live in
    the user's home ``~/.claude.json`` (``.projects.<cwd>``) and are NOT
    repo-local.
    """

    name: str
    server: RegistryServer
    env_classification: dict[str, Classification]
    source_label: str
    is_repo_local: bool = False


# ---------------------------------------------------------------------------
# Per-host scanners
# ---------------------------------------------------------------------------


HostId = str
"""Canonical host identifier — one of: claude-code, cursor, codex, claude-desktop."""

ALL_HOSTS: tuple[HostId, ...] = ("claude-code", "cursor", "codex", "claude-desktop")


def _derive_prefix(name: str) -> str:
    """Default prefix for an imported MCP name.

    RFC §5.3 examples use a hand-picked short prefix (``filesystem`` →
    ``fs``). We can't read the user's mind, so we derive a deterministic
    default by lowercasing and replacing hyphens with underscores. The
    user can edit ``registry.toml`` post-import if they want a shorter
    one.
    """
    candidate = name.lower().replace("-", "_")
    if _PREFIX_RE.match(candidate):
        return candidate
    # Strip leading non-letters then re-check; if still bad, fall back to "mcp".
    stripped = re.sub(r"^[^a-zA-Z]+", "", candidate)
    if stripped and _PREFIX_RE.match(stripped):
        return stripped
    return "mcp"


def _to_registry_server(name: str, raw: dict[str, Any]) -> RegistryServer | None:
    """Map a host-config entry to a ``RegistryServer``.

    W1 only supports stdio entries (``command`` + optional ``args`` /
    ``env``); SSE/HTTP transports are skipped here — they live in the
    STM proxy config (``stm_proxy.json``) not the mms registry, since
    they have no per-project enable/disable use case yet (RFC §5.3
    schema mentions only stdio fields).

    Returns ``None`` for entries we can't import cleanly (no command,
    self-references, etc.).
    """
    command = raw.get("command")
    if not isinstance(command, str) or not command:
        return None
    if _is_self_reference(raw):
        return None

    args_raw = raw.get("args") or []
    args: list[str] = (
        [a for a in args_raw if isinstance(a, str)] if isinstance(args_raw, list) else []
    )

    env_raw = raw.get("env") or {}
    env: dict[str, str] = {}
    if isinstance(env_raw, dict):
        for k, v in env_raw.items():
            if not isinstance(k, str) or k.upper() in _DANGEROUS_ENV_KEYS:
                continue
            env[k] = str(v)

    return RegistryServer(command=command, args=args, env=env, prefix=_derive_prefix(name))


def _wrap(
    name: str, raw: dict[str, Any], source_label: str, *, is_repo_local: bool = False
) -> ImportCandidate | None:
    server = _to_registry_server(name, raw)
    if server is None:
        return None
    classification = classify_env(server.env)
    return ImportCandidate(
        name=name,
        server=server,
        env_classification=classification,
        source_label=source_label,
        is_repo_local=is_repo_local,
    )


def project_local_gate_message(names: list[str]) -> str:
    """Abort message when project-local candidates are registered without
    acknowledgement. Shared by ``mms import`` and ``mms host sync`` so both
    gates speak with one voice."""
    joined = ", ".join(sorted(names))
    n = len(names)
    plural = "y" if n == 1 else "ies"
    return (
        f"Refusing to register {n} MCP entr{plural} from project-local config "
        f"under the current directory: {joined}. These come from files a "
        "repository checkout can contain (.mcp.json / .cursor/mcp.json) and could "
        "register an untrusted command that later runs with your privileges. Pass "
        "--allow-project-configs to acknowledge and proceed."
    )


def _mcp_servers(config: dict[str, Any]) -> dict[str, Any]:
    """Return ``config["mcpServers"]`` if it is a dict, else ``{}``.

    A structurally-valid JSON whose ``mcpServers`` is a list/str/number is
    truthy, so an ``or {}`` guard would pass it straight to ``.items()`` and
    crash the scan with an AttributeError. Wrong-typed entries follow the
    module contract for unreadable configs — no candidates, silent — same as
    ``scan_codex``'s ``isinstance`` guard on ``mcp_servers``.
    """
    servers = config.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def scan_claude_code(cwd: Path) -> list[ImportCandidate]:
    """Scan ``~/.claude.json`` (user + per-project) plus ``<cwd>/.mcp.json``."""
    candidates: list[ImportCandidate] = []

    user_config = _read_json_safely(Path("~/.claude.json").expanduser())
    if user_config:
        for name, raw in _mcp_servers(user_config).items():
            if not isinstance(raw, dict):
                continue
            cand = _wrap(name, raw, "Claude Code (user)")
            if cand:
                candidates.append(cand)

        # Per-project entries live under .projects.<absolute-cwd>.mcpServers.
        projects = user_config.get("projects") or {}
        if isinstance(projects, dict):
            project_entry = projects.get(str(cwd)) or projects.get(str(cwd.resolve())) or {}
            if isinstance(project_entry, dict):
                for name, raw in _mcp_servers(project_entry).items():
                    if not isinstance(raw, dict):
                        continue
                    cand = _wrap(name, raw, "Claude Code (project)")
                    if cand:
                        candidates.append(cand)

    project_mcp = _read_json_safely(cwd / ".mcp.json")
    if project_mcp:
        for name, raw in _mcp_servers(project_mcp).items():
            if not isinstance(raw, dict):
                continue
            cand = _wrap(name, raw, ".mcp.json (project)", is_repo_local=True)
            if cand:
                candidates.append(cand)

    return candidates


def scan_cursor(cwd: Path) -> list[ImportCandidate]:
    """Scan ``~/.cursor/mcp.json`` (user) and ``<cwd>/.cursor/mcp.json`` (project)."""
    candidates: list[ImportCandidate] = []

    for path, label, repo_local in [
        (Path("~/.cursor/mcp.json").expanduser(), "Cursor (user)", False),
        (cwd / ".cursor" / "mcp.json", "Cursor (project)", True),
    ]:
        config = _read_json_safely(path)
        if not config:
            continue
        for name, raw in _mcp_servers(config).items():
            if not isinstance(raw, dict):
                continue
            cand = _wrap(name, raw, label, is_repo_local=repo_local)
            if cand:
                candidates.append(cand)

    return candidates


def scan_codex(cwd: Path) -> list[ImportCandidate]:
    """Scan ``~/.codex/config.toml`` for ``[mcp_servers.<name>]`` entries."""
    config = _read_toml_safely(Path("~/.codex/config.toml").expanduser())
    if not config:
        return []
    servers = config.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        return []

    out: list[ImportCandidate] = []
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            continue
        cand = _wrap(name, raw, "Codex CLI")
        if cand:
            out.append(cand)
    return out


def scan_claude_desktop(cwd: Path) -> list[ImportCandidate]:
    """Scan Claude Desktop's macOS config. Linux/Windows: returns []."""
    if sys.platform != "darwin":
        return []
    config = _read_json_safely(_desktop_config_path())
    if not config:
        return []
    out: list[ImportCandidate] = []
    for name, raw in _mcp_servers(config).items():
        if not isinstance(raw, dict):
            continue
        cand = _wrap(name, raw, "Claude Desktop")
        if cand:
            out.append(cand)
    return out


_HOST_SCANNERS: dict[HostId, "_HostScanner"] = {
    "claude-code": scan_claude_code,
    "cursor": scan_cursor,
    "codex": scan_codex,
    "claude-desktop": scan_claude_desktop,
}

# Type alias — declared after the dict so callers see the canonical shape.
from typing import Callable  # noqa: E402

_HostScanner = Callable[[Path], list[ImportCandidate]]


def discover(host: HostId | str, cwd: Path) -> list[ImportCandidate]:
    """Discover candidates from one host (or all when host == ``"all"``).

    ``"all"`` iterates every scanner in ``ALL_HOSTS`` order and
    concatenates results — same name appearing in two hosts produces
    two candidates; ``mms import`` applies first-import-wins reconciliation.
    """
    if host == "all":
        out: list[ImportCandidate] = []
        for h in ALL_HOSTS:
            out.extend(_HOST_SCANNERS[h](cwd))
        return out
    scanner = _HOST_SCANNERS.get(host)
    if scanner is None:
        raise ValueError(f"Unknown host '{host}'. Valid: {', '.join(ALL_HOSTS)}, all")
    return scanner(cwd)
