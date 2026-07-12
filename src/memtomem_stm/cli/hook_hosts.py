"""Per-host hook-config registration for ``mms hook install`` (Track B / B3).

``mms hook`` bridges a host's built-in tool calls into STM's surfacing
(:mod:`memtomem_stm.cli.hook_cmd`); B4 made the live invocation host-explicit
(``mms hook --host <name>``). This module is the *registration UX* on top of
that: it read-modify-writes each host's hook-config file so the host actually
fires ``mms hook --host <name>`` on PostToolUse.

It parallels :mod:`memtomem_stm.mms.import_hosts` (which knows each host's *MCP*
config locations) but for the *hook* configs, which live elsewhere and differ in
shape and format. Per-host facts (verified in the B0 contract report +
``tests/fixtures/hooks/``; Codex's registration TOML pinned to the official
``developers.openai.com/codex`` sample):

==========  ===============================  ======  ==========================
host        config file                      format  PostToolUse block shape
==========  ===============================  ======  ==========================
claude      ``~/.claude/settings.json``      json    ``hooks.PostToolUse`` = list
                                                      of ``{matcher, hooks:[{type,
                                                      command}]}``
cursor      ``~/.cursor/hooks.json``         json    ``{version:1, hooks:
                                                      {postToolUse:[{command}]}}``
                                                      — no matcher (generic hook)
kimi        ``~/.kimi/config.toml``          toml    top-level ``[[hooks]]`` array
                                                      of ``{event, matcher,
                                                      command}``
codex       ``~/.codex/config.toml``         toml    ``[[hooks.PostToolUse]]`` +
                                                      ``[[hooks.PostToolUse.hooks]]``
                                                      (``matcher`` regex; ``{type,
                                                      command}``) — same nesting as
                                                      Claude, in TOML
==========  ===============================  ======  ==========================

Why ``cli/`` (and not ``mms/`` as the plan sketched): this module imports
:mod:`hook_adapter` to derive each host's matcher from the *same*
``native_tool_map`` the live hook gates on, keeping the installed matcher from
drifting from what the hook surfaces for. ``mms/`` never imports ``cli/`` (the
import direction is ``cli → mms``), so a ``cli/`` home keeps that one-way and
co-locates "how to register host X" with "how to parse/render host X".

Design choices (all surfaced to the operator before any write):

* **Idempotent.** :func:`plan_install` recognizes STM's own block by command
  shape (:func:`_is_stm_hook_command`: an ``mms`` / ``memtomem-stm`` executable
  immediately followed by a ``hook`` token), so re-running updates that block in
  place rather than appending a duplicate, regardless of whether it was
  registered global (``memtomem-stm hook …``) or source
  (``uv run … memtomem-stm hook …``).
* **Symmetric uninstall.** :func:`plan_uninstall` removes exactly the blocks
  :func:`plan_install` would add — never a hand-written hook. Install is equally
  careful: a non-STM handler a user co-located in STM's matcher group is
  preserved across a re-install (see :func:`_nested_install`).
* **Dry-run default + backup.** The CLI previews by default; an ``--apply`` write
  copies the prior file to a non-clobbering backup first (``<path>.bak``, else
  ``.bak.1``, ``.bak.2``, …; :func:`_write_backup`, which claims each slot with
  ``O_EXCL`` so concurrent applies can't race) so a second apply never
  overwrites the original. A config that exists but does not parse is *refused*,
  never clobbered.
* **TOML rewrite caveat.** TOML hosts are parsed and re-serialized (``tomli_w``),
  so comments / key ordering in ``config.toml`` are **not preserved**. JSON has
  no comments, so JSON hosts are unaffected. The preview states this and the
  ``.bak`` backup is the safety net. (JSON / TOML re-serialization mirrors how
  STM already writes its own configs — see ``mms/state.py`` / ``utils/fileio``.)
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from memtomem_stm.cli.hook_adapter import READLIKE_SURFACE_TOOLS, get_adapter
from memtomem_stm.utils.fileio import atomic_write_text

# Executable basenames that mark a command as an STM hook invocation. Mirrors
# ``mms ≡ memtomem-stm ≡ memtomem-stm-proxy`` (the three ``[project.scripts]``
# entry points, #260) so a block registered under any of them is recognized.
_STM_EXECUTABLES: frozenset[str] = frozenset({"mms", "memtomem-stm", "memtomem-stm-proxy"})


class HookInstallError(Exception):
    """A host config exists but cannot be safely modified (e.g. unparseable).

    Raised by :func:`plan_install` / :func:`plan_uninstall` so the CLI can report
    it and exit non-zero — distinct from the *runtime* ``mms hook`` path, which
    must always exit 0. Refusing to touch a config we cannot parse avoids
    clobbering valid-but-unusual contents (the ``.bak`` backup only helps if we
    got far enough to write one)."""


# ---------------------------------------------------------------------------
# STM-hook command recognition (idempotency + symmetric uninstall)
# ---------------------------------------------------------------------------


def _is_stm_hook_command(command: str) -> bool:
    """Whether ``command`` is an STM ``mms hook`` invocation (any entry point).

    Token-based, not substring: a command is ours when an STM executable token is
    *immediately followed by* a ``hook`` token (the ``hook`` subcommand). Catches
    both the global (``memtomem-stm hook --host X``) and source
    (``uv run --directory … memtomem-stm hook --host X``) shapes, and any
    ``--host`` value, so install updates and uninstall removes regardless of how
    it was first registered.

    Adjacency (exe→``hook``) — not "a ``hook`` token anywhere *and* an STM exe
    token anywhere" — is what keeps a compound user command like
    ``echo hook && memtomem-stm status`` from matching: ``status`` is not ``hook``,
    so install/uninstall leave it alone."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if any(tok in {"|", "||", "&&", ";", "&", ">", ">>", "<"} for tok in tokens):
        return False
    # A source checkout directory can itself be named ``memtomem-stm``. Count
    # only executable-shaped tokens immediately followed by the subcommand, so
    # ``uv run --directory /repo/memtomem-stm memtomem-stm hook`` has one owner
    # candidate rather than mistaking the --directory value for an executable.
    starts = [
        i
        for i, tok in enumerate(tokens)
        if Path(tok).name in _STM_EXECUTABLES and tokens[i + 1 : i + 2] == ["hook"]
    ]
    if len(starts) != 1:
        return False
    i = starts[0]
    prefix = tokens[:i]
    if prefix and not (prefix[:2] == ["uv", "run"] and all(token != "--" for token in prefix)):
        return False
    return True


def _is_stm_command_handler(handler: Any) -> bool:
    """Whether a ``{type:"command", command:"…"}`` handler dict is STM's."""
    return (
        isinstance(handler, dict)
        and isinstance(cmd := handler.get("command"), str)
        and _is_stm_hook_command(cmd)
    )


def _entry_has_stm_handler(entry: Any) -> bool:
    """Whether a matcher-group entry (``{matcher, hooks:[…]}``) holds an STM handler."""
    return (
        isinstance(entry, dict)
        and isinstance(inner := entry.get("hooks"), list)
        and any(_is_stm_command_handler(h) for h in inner)
    )


# ---------------------------------------------------------------------------
# Per-host merge functions: (data, command, matcher) -> mutated data
# ---------------------------------------------------------------------------
#
# Each takes the parsed config (a dict; ``{}`` when the file is absent) and
# returns it with STM's PostToolUse block merged in idempotently. ``matcher`` is
# the host's tool-name filter (``None`` for hosts whose post-tool hook is generic,
# i.e. Cursor). They mutate-and-return; callers pass a deep copy when they need
# the pre-merge state for change detection.


def _ensure_dict(parent: dict[str, Any], key: str) -> dict[str, Any]:
    """Return ``parent[key]`` as a dict without clobbering malformed config."""
    val = parent.get(key)
    if val is None:
        val = {}
        parent[key] = val
    elif not isinstance(val, dict):
        raise HookInstallError(f"expected {key!r} to be an object")
    return val


def _ensure_list(parent: dict[str, Any], key: str) -> list[Any]:
    """Return ``parent[key]`` as a list without clobbering malformed config."""
    val = parent.get(key)
    if val is None:
        val = []
        parent[key] = val
    elif not isinstance(val, list):
        raise HookInstallError(f"expected {key!r} to be a list")
    return val


def _nested_install(data: dict[str, Any], command: str, matcher: str | None) -> dict[str, Any]:
    """Merge a matcher-group block (Claude / Codex shape).

    ``hooks.PostToolUse`` is a list of ``{matcher, hooks:[{type, command}]}``;
    Claude reads it from JSON and Codex from TOML, but the shape is identical."""
    entries = _ensure_list(_ensure_dict(data, "hooks"), "PostToolUse")
    block: dict[str, Any] = {}
    if matcher is not None:
        block["matcher"] = matcher
    handler = {"type": "command", "command": command}
    block["hooks"] = [handler]

    # Scan *all* PostToolUse entries (symmetric with ``_nested_uninstall``):
    # install/refresh exactly one STM handler at the first STM group and strip
    # STM from any later ones, so a config that somehow holds two STM groups
    # (a hand-edit, or an old install) converges to a single handler — honoring
    # the "never duplicated" contract — without ever losing a non-STM sibling a
    # user co-located in an STM group.
    out: list[Any] = []
    installed = False
    for entry in entries:
        if not _entry_has_stm_handler(entry):
            out.append(entry)
            continue
        siblings = [h for h in entry["hooks"] if not _is_stm_command_handler(h)]
        if not installed:
            installed = True
            if not siblings:
                out.append(block)  # STM-only group → refresh matcher + command
            else:
                # Shared group: keep the user's matcher + siblings, swap STM's
                # handler in place (the first one, dropping any duplicates).
                new_inner: list[Any] = []
                replaced = False
                for h in entry["hooks"]:
                    if _is_stm_command_handler(h):
                        if not replaced:
                            new_inner.append(handler)
                            replaced = True
                    else:
                        new_inner.append(h)
                out.append({**entry, "hooks": new_inner})
        elif siblings:
            out.append({**entry, "hooks": siblings})  # later shared group: drop STM, keep siblings
        # else: a later STM-only group is a duplicate → drop it entirely
    if not installed:
        out.append(block)
    entries[:] = out  # mutate in place so ``data["hooks"]["PostToolUse"]`` stays wired
    return data


def _nested_uninstall(data: dict[str, Any]) -> dict[str, Any]:
    """Drop STM handlers from a matcher-group block (Claude / Codex shape).

    Prunes containers that become empty (the now-empty ``PostToolUse`` list, then
    ``hooks`` if no other events remain) so uninstall leaves no STM residue — but
    never touches a sibling event (e.g. a hand-written ``PreToolUse``)."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return data
    entries = hooks.get("PostToolUse")
    if not isinstance(entries, list):
        return data
    kept_entries: list[Any] = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
            inner = [h for h in entry["hooks"] if not _is_stm_command_handler(h)]
            if not inner:
                continue  # entry held only STM handler(s) → drop the whole group
            entry = {**entry, "hooks": inner}
        kept_entries.append(entry)
    if kept_entries:
        hooks["PostToolUse"] = kept_entries
    else:
        hooks.pop("PostToolUse", None)
    if not hooks:
        data.pop("hooks", None)
    return data


def _cursor_install(data: dict[str, Any], command: str, matcher: str | None) -> dict[str, Any]:
    """Merge Cursor's ``{version:1, hooks:{postToolUse:[{command}]}}`` (no matcher)."""
    if not isinstance(data.get("version"), int):
        data["version"] = 1
    entries = _ensure_list(_ensure_dict(data, "hooks"), "postToolUse")
    block = {"command": command}
    for i, entry in enumerate(entries):
        if _is_stm_command_handler(entry):
            entries[i] = block
            return data
    entries.append(block)
    return data


def _cursor_uninstall(data: dict[str, Any]) -> dict[str, Any]:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get("postToolUse"), list):
        return data
    kept = [e for e in hooks["postToolUse"] if not _is_stm_command_handler(e)]
    if kept:
        hooks["postToolUse"] = kept
    else:
        hooks.pop("postToolUse", None)
    if not hooks:
        data.pop("hooks", None)
    return data


def _kimi_install(data: dict[str, Any], command: str, matcher: str | None) -> dict[str, Any]:
    """Merge Kimi's top-level ``[[hooks]]`` array of ``{event, matcher, command}``."""
    entries = _ensure_list(data, "hooks")
    block: dict[str, Any] = {"event": "PostToolUse"}
    if matcher is not None:
        block["matcher"] = matcher
    block["command"] = command
    for i, entry in enumerate(entries):
        if _is_stm_command_handler(entry):
            entries[i] = block
            return data
    entries.append(block)
    return data


def _kimi_uninstall(data: dict[str, Any]) -> dict[str, Any]:
    entries = data.get("hooks")
    if not isinstance(entries, list):
        return data
    kept = [e for e in entries if not _is_stm_command_handler(e)]
    if kept:
        data["hooks"] = kept
    else:
        data.pop("hooks", None)  # last hook removed → drop the empty array entirely
    return data


# ---------------------------------------------------------------------------
# Host registry
# ---------------------------------------------------------------------------

_InstallFn = Callable[[dict[str, Any], str, "str | None"], dict[str, Any]]
_UninstallFn = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class HookHostSpec:
    """How to register STM's PostToolUse hook in one host's config.

    ``matcher_style`` formats the host's tool-name filter from the read-like
    native names :func:`matcher_for` derives off the adapter: ``"alternation"``
    → ``Read|Grep|Glob|Bash`` (Claude/Kimi), ``"regex"`` → ``^Bash$`` /
    ``^(A|B)$`` (Codex), ``"none"`` → no matcher (Cursor's generic post-tool
    hook). ``notes`` are post-install caveats the CLI echoes (e.g. Codex's
    ``/hooks`` trust step, Cursor's runtime no-op)."""

    host_tag: str
    label: str
    config_path: Path
    fmt: str  # "json" | "toml"
    matcher_style: str  # "alternation" | "regex" | "none"
    install_fn: _InstallFn
    uninstall_fn: _UninstallFn
    notes: tuple[str, ...] = field(default_factory=tuple)


HOOK_HOSTS: dict[str, HookHostSpec] = {
    "claude": HookHostSpec(
        host_tag="claude",
        label="Claude Code",
        config_path=Path("~/.claude/settings.json"),
        fmt="json",
        matcher_style="alternation",
        install_fn=_nested_install,
        uninstall_fn=_nested_uninstall,
        notes=(
            "Surfacing runs through the warm `mms daemon` (auto-spawned on first "
            "use; `mms daemon start` to pre-warm). Restart Claude Code to load the hook.",
        ),
    ),
    "codex": HookHostSpec(
        host_tag="codex",
        label="Codex CLI",
        config_path=Path("~/.codex/config.toml"),
        fmt="toml",
        matcher_style="regex",
        install_fn=_nested_install,
        uninstall_fn=_nested_uninstall,
        notes=(
            "Codex requires explicit trust for non-managed hooks: run `/hooks` in "
            "Codex to approve it before it will execute.",
            "Surfacing-only (Codex does not replace native tool output). Codex's "
            "standalone `additionalContext` inject is undocumented — confirm memories "
            "appear before relying on it.",
        ),
    ),
    "cursor": HookHostSpec(
        host_tag="cursor",
        label="Cursor",
        config_path=Path("~/.cursor/hooks.json"),
        fmt="json",
        matcher_style="none",
        install_fn=_cursor_install,
        uninstall_fn=_cursor_uninstall,
        notes=(
            "Heads-up: Cursor's `additional_context` is a documented but "
            "runtime-no-op channel today (staff-confirmed bug) — surfaced memories "
            "may not appear until Cursor ships the fix.",
        ),
    ),
    "kimi": HookHostSpec(
        host_tag="kimi",
        label="Kimi Code",
        config_path=Path("~/.kimi-code/config.toml"),
        fmt="toml",
        matcher_style="alternation",
        install_fn=_kimi_install,
        uninstall_fn=_kimi_uninstall,
        notes=(
            "Metrics-only: Kimi PostToolUse is observational and cannot inject "
            "context or replace native tool output.",
        ),
    ),
}


def _config_path(spec: HookHostSpec) -> Path:
    """Resolve host homes without ignoring the host's supported home override."""
    env_and_name = {
        "claude": ("CLAUDE_CONFIG_DIR", "settings.json"),
        "codex": ("CODEX_HOME", "config.toml"),
        "kimi": ("KIMI_CODE_HOME", "config.toml"),
    }.get(spec.host_tag)
    if env_and_name and (home := os.environ.get(env_and_name[0])):
        return Path(home).expanduser() / env_and_name[1]
    return spec.config_path.expanduser()


def matcher_for(host_tag: str) -> str | None:
    """The PostToolUse tool-name matcher to register for ``host_tag``, or ``None``.

    Derived from the host adapter's ``native_tool_map`` filtered to
    :data:`READLIKE_SURFACE_TOOLS` (the exact native names whose canonical maps
    into the surface allowlist), so the installed matcher tracks what the live
    hook surfaces for. Formatted per :attr:`HookHostSpec.matcher_style`. ``None``
    when the host's post-tool hook is matcher-less (Cursor) or no native name
    maps into the allowlist."""
    spec = HOOK_HOSTS[host_tag]
    if spec.matcher_style == "none":
        return None
    adapter = get_adapter(host_tag)
    names = [
        native
        for native, canonical in adapter.native_tool_map.items()
        if canonical in READLIKE_SURFACE_TOOLS
    ]
    if not names:
        return None
    if spec.matcher_style == "regex":
        # Anchored regex on the tool name (Codex). Single name → ``^Bash$`` to
        # match the official sample; multiple → ``^(A|B)$``.
        return f"^{names[0]}$" if len(names) == 1 else "^(" + "|".join(names) + ")$"
    return "|".join(names)  # "alternation": Claude / Kimi


# ---------------------------------------------------------------------------
# Plan / apply
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookChange:
    """The result of planning an install/uninstall — preview now, apply later.

    ``changed`` drives whether :func:`apply_change` writes at all; ``status`` is
    the human verdict (``"create"`` / ``"update"`` / ``"already"`` for install;
    ``"remove"`` / ``"absent"`` / ``"not_installed"`` for uninstall).
    ``current_text`` is the verbatim prior file (``None`` when absent) — used both
    as the ``.bak`` payload and to show nothing will be lost. ``rendered_block`` is
    a readable snippet of just STM's block (the host's native format)."""

    host_tag: str
    label: str
    path: Path
    fmt: str
    action: str  # "install" | "uninstall"
    status: str
    changed: bool
    current_text: str | None
    new_text: str
    rendered_block: str
    notes: tuple[str, ...]


def _read_current(path: Path, fmt: str) -> tuple[dict[str, Any], str] | None:
    """Parse an existing host config; ``None`` if absent.

    Raises :class:`HookInstallError` when the file exists but does not parse as
    ``fmt`` or is not a top-level table/object — we refuse to overwrite a config
    we cannot understand."""
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HookInstallError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw) if fmt == "json" else tomllib.loads(raw)
    except (json.JSONDecodeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise HookInstallError(
            f"{path} exists but is not valid {fmt.upper()} ({exc}); refusing to "
            "modify it. Fix or remove the file, then re-run."
        ) from exc
    if not isinstance(data, dict):
        raise HookInstallError(
            f"{path} is valid {fmt.upper()} but not a top-level "
            f"{'object' if fmt == 'json' else 'table'}; refusing to modify it."
        )
    return data, raw


def _serialize(fmt: str, data: dict[str, Any]) -> str:
    """Serialize merged config back to its on-disk format (trailing newline)."""
    if fmt == "json":
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    text = tomli_w.dumps(data)
    return text if text.endswith("\n") else text + "\n"


def _render_block(host_tag: str, command: str) -> str:
    """A readable snippet of just STM's block, in the host's native format.

    Built by merging into an *empty* config so the preview shows exactly what
    ``install`` adds, not the whole rewritten file."""
    spec = HOOK_HOSTS[host_tag]
    only = spec.install_fn({}, command, matcher_for(host_tag))
    return _serialize(spec.fmt, only).rstrip("\n")


def plan_install(host_tag: str, command: str) -> HookChange:
    """Plan registering ``command`` as ``host_tag``'s PostToolUse hook.

    Pure (no writes): parses the current config (if any), computes the merged
    result, and reports whether applying it would create / update / no-op. The
    merge is idempotent — an existing STM block is replaced in place."""
    spec = HOOK_HOSTS[host_tag]
    path = _config_path(spec)
    current = _read_current(path, spec.fmt)
    base: dict[str, Any] = current[0] if current else {}
    current_text = current[1] if current else None

    new_data = spec.install_fn(copy.deepcopy(base), command, matcher_for(host_tag))
    new_text = _serialize(spec.fmt, new_data)

    if current is None:
        status, changed = "create", True
    elif new_data == base:
        status, changed = "already", False
    else:
        status, changed = "update", True

    return HookChange(
        host_tag=host_tag,
        label=spec.label,
        path=path,
        fmt=spec.fmt,
        action="install",
        status=status,
        changed=changed,
        current_text=current_text,
        new_text=new_text,
        rendered_block=_render_block(host_tag, command),
        notes=spec.notes,
    )


def plan_uninstall(host_tag: str) -> HookChange:
    """Plan removing STM's PostToolUse hook from ``host_tag``'s config.

    Removes exactly what :func:`plan_install` adds (recognized by command shape).
    A no-op when the file is absent or holds no STM block."""
    spec = HOOK_HOSTS[host_tag]
    path = _config_path(spec)
    current = _read_current(path, spec.fmt)
    if current is None:
        return HookChange(
            host_tag=host_tag,
            label=spec.label,
            path=path,
            fmt=spec.fmt,
            action="uninstall",
            status="absent",
            changed=False,
            current_text=None,
            new_text="",
            rendered_block="",
            notes=(),
        )
    base, current_text = current
    new_data = spec.uninstall_fn(copy.deepcopy(base))
    changed = new_data != base
    new_text = _serialize(spec.fmt, new_data)
    return HookChange(
        host_tag=host_tag,
        label=spec.label,
        path=path,
        fmt=spec.fmt,
        action="uninstall",
        status="remove" if changed else "not_installed",
        changed=changed,
        current_text=current_text,
        new_text=new_text,
        rendered_block="",
        notes=(),
    )


def _write_backup(path: Path, content: str, mode: int) -> Path:
    """Write ``content`` to the first free non-clobbering backup slot, atomically.

    Tries ``<name>.bak`` first (so ``settings.json`` → ``settings.json.bak``,
    keeping both suffixes), then ``<name>.bak.1``, ``<name>.bak.2``, …. A fixed
    single ``.bak`` would let a *second* ``--apply`` (an uninstall, or a
    re-install) overwrite the backup from the *first* apply; for a TOML host that
    first ``.bak`` is the only comment-preserving copy of the original
    (re-serialization drops comments — see the module docstring), so overwriting
    it destroys exactly the safety net. Numbered slots preserve every prior
    state, with the original always at ``.bak``.

    Each slot is claimed with ``O_CREAT | O_EXCL`` — slot selection *and* the
    write are one atomic step, so two concurrent ``--apply`` runs can't pick the
    same slot and clobber each other's backup (the bare ``exists()``-then-write
    that this replaces had that TOCTOU race). On a collision we advance to the
    next slot and retry. A backup is always a fresh file, never an overwrite, so
    no atomic-replace is needed; a partial write (mid-write crash) is unlinked
    and re-raised, leaving the next slot free."""
    i = 0
    while True:
        candidate = path.parent / (path.name + (".bak" if i == 0 else f".bak.{i}"))
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            i += 1
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            # O_EXCL honors umask, so the mode above may be narrowed; force it
            # (the prior file's bits, host configs can carry secrets).
            try:
                candidate.chmod(mode)
            except OSError:
                pass
        except Exception:
            candidate.unlink(missing_ok=True)
            raise
        return candidate


def apply_change(change: HookChange) -> Path | None:
    """Write ``change.new_text`` to disk, backing up any prior file first.

    No-op (returns ``None``) when ``change.changed`` is False. When a prior file
    exists, its verbatim contents are written to a non-clobbering backup slot
    (:func:`_write_backup`) *before* the new config is atomically written; returns
    the backup path (``None`` when there was nothing to back up). The new file
    inherits the prior file's permission bits, or ``0o600`` for a freshly-created
    one (host configs can carry secrets).

    Staleness guard: ``change.new_text`` was merged from the config as read at
    PLAN time, and the atomic replace writes that merge back wholesale — so if
    the file changed in between (the host application persisting its own
    settings; mms' advisory lock only serializes mms processes), applying would
    silently revert the other writer's edit. The guard re-reads the file and
    raises :class:`HookInstallError` on any divergence from what the plan saw
    (including created-since-planned and deleted-since-planned), BEFORE the
    backup, so an aborted apply has no side effect. Re-running re-plans from
    the fresh contents."""
    if not change.changed:
        return None
    path = change.path
    try:
        on_disk = path.read_text(encoding="utf-8") if path.exists() else None
    except OSError as exc:
        raise HookInstallError(
            f"cannot re-read {path} before writing ({exc}); nothing was written."
        ) from exc
    if on_disk != change.current_text:
        raise HookInstallError(
            f"{path} changed after this change was planned (a concurrent writer, "
            "possibly the host application itself); nothing was written. Re-run "
            "the command to re-plan against the current contents."
        )
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o600
    backup: Path | None = None
    if change.current_text is not None:
        backup = _write_backup(path, change.current_text, mode)
    atomic_write_text(path, change.new_text, mode=mode, durable=True)
    return backup
