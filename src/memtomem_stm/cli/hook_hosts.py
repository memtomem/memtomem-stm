"""Per-host hook-config registration for ``mms hook install`` (Track B / B3).

``mms hook`` bridges a host's built-in tool calls into STM's surfacing
(:mod:`memtomem_stm.cli.hook_cmd`); B4 made the live invocation host-explicit
(``mms hook --host <name>``). This module is the *registration UX* on top of
that: it read-modify-writes each host's hook-config file so the host actually
fires ``memtomem-stm hook --host <name>`` (behind ``uv run --directory <dir>``
in a project that uses STM) on PostToolUse.

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
kimi        ``~/.kimi-code/config.toml``     toml    top-level ``[[hooks]]`` array
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

* **Ownership, not recognition.** Install and uninstall manage the handlers
  install writes and no others. :func:`owned_handler` is the one test: the
  handler is in a container install writes to, has the fields install gives
  it, and its ``command`` is ``memtomem-stm hook --host <host>`` (bare or
  after ``uv run --directory <dir>``), alone or followed by the six
  runtime-setting words, spelled as ``shlex.join`` spells it. No shell is
  modelled: whether some other line would run STM's hook is not asked. A
  handler that only *looks* like STM's hook (``mms hook --host claude``,
  ``NAME=value mms hook``, an ``env`` wrapper, a quoted name) is left alone
  and named by location (:func:`unmanaged_stm_like`), never rewritten or
  deleted (#1453, #1460, #1483, #1484).
* **Idempotent.** Re-running :func:`plan_install` updates the owned handler
  in place rather than appending a duplicate, whether it was registered
  global (``memtomem-stm hook …``) or source (``uv run --directory …
  memtomem-stm hook …``).
* **Symmetric uninstall.** :func:`plan_uninstall` removes the owned handlers
  and names the look-alikes it leaves. Install is equally careful: a handler
  a user co-located in STM's matcher group is preserved across a re-install
  (see :func:`_nested_install`).
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
import stat
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from memtomem_stm.utils.json_out import dumps as _json_dumps
from memtomem_stm.cli.hook_adapter import READLIKE_SURFACE_TOOLS, get_adapter
from memtomem_stm.cli.host_runtime import (
    OwnedHookCommand,
    _lenient_shell_tokens,
    owned_hook_command,
)
from memtomem_stm.mms.host_homes import (
    HOST_HOME_UNKNOWN_REASONS,
    HostHomeUnknown,
    host_home_override,
)
from memtomem_stm.utils.fileio import atomic_write_text

# Executable basenames that mark a command as naming STM's hook, for the
# look-alike notice and ``mms doctor``: ``mms ≡ memtomem-stm ≡
# memtomem-stm-proxy`` (the three ``[project.scripts]`` entry points, #260).
# Ownership is narrower: install writes ``memtomem-stm`` only.
_STM_EXECUTABLES: frozenset[str] = frozenset({"mms", "memtomem-stm", "memtomem-stm-proxy"})

# Hosts whose config nests handlers in matcher groups under ``hooks.PostToolUse``.
_NESTED_HOSTS = frozenset({"claude", "codex"})
# The ``event`` of the Kimi entry install writes.
_KIMI_EVENT = "PostToolUse"


class HookInstallError(Exception):
    """A host config exists but cannot be safely modified (e.g. unparseable).

    Raised by :func:`plan_install` / :func:`plan_uninstall` so the CLI can report
    it and exit non-zero — distinct from the *runtime* ``mms hook`` path, which
    must always exit 0. Refusing to touch a config we cannot parse avoids
    clobbering valid-but-unusual contents (the ``.bak`` backup only helps if we
    got far enough to write one)."""


class HookConfigNotRegularFile(HookInstallError):
    """The host config path exists but is not a regular file (a directory, a
    FIFO, a device). Reading a FIFO would block until a writer appears."""


class HookConfigNotAnObject(HookInstallError):
    """The host config parses but its root is not an object/table."""


class HookConfigLocationUnknown(HookInstallError):
    """The host's home override (``CLAUDE_CONFIG_DIR``, …) does not name a
    usable directory, so no config file can be chosen."""


# ---------------------------------------------------------------------------
# Ownership: which handlers install and uninstall manage
# ---------------------------------------------------------------------------


def owned_handler(host_tag: str, handler: Any) -> OwnedHookCommand | None:
    """``handler`` read as one ``mms hook install --host <host_tag>`` writes,
    or ``None`` when it is not in that form.

    The one test of "is this handler install's own": settings are read from
    it, the merge replaces it, uninstall removes it, and the look-alike notice
    leaves it out. A handler is owned when it sits in a container install
    writes to (the caller passes only those), has the fields install gives it,
    and its ``command`` has the text install writes
    (:func:`~memtomem_stm.cli.host_runtime.owned_hook_command`):

    * Claude Code and Codex: ``type`` is ``"command"`` and there is no
      ``args`` key. Install never writes ``args``; in Claude Code it also
      turns ``command`` into a single executable path.
    * Kimi: ``event`` is ``"PostToolUse"``, the only event install registers.
    * Cursor: nothing beyond the text.

    Other fields do not count. A refresh keeps a timeout, a status message
    and any field it does not know, and sets the matcher to install's own
    (except in a matcher group shared with another handler). Whether the
    command would run STM is not asked: a hand-written registration is its
    author's, however close it comes."""
    if not isinstance(handler, dict):
        return None
    command = handler.get("command")
    if not isinstance(command, str):
        return None
    if host_tag in _NESTED_HOSTS:
        if handler.get("type") != "command" or "args" in handler:
            return None
    elif host_tag == "kimi" and handler.get("event") != _KIMI_EVENT:
        return None
    return owned_hook_command(command, host_tag)


def _group_has_owned_handler(host_tag: str, entry: Any) -> bool:
    """Whether a matcher-group entry (``{matcher, hooks:[…]}``) holds an owned handler."""
    return (
        isinstance(entry, dict)
        and isinstance(inner := entry.get("hooks"), list)
        and any(owned_handler(host_tag, h) is not None for h in inner)
    )


def _exe_before_hook(tokens: list[str]) -> Iterator[int]:
    """Indexes of the words that name an STM executable and are followed by a
    ``hook`` word, in order. Only a word before ``hook`` is read as a path,
    and a caller stops reading once it has what it needs."""
    for i, tok in enumerate(tokens):
        if tok == "hook" and i > 0 and Path(tokens[i - 1]).name in _STM_EXECUTABLES:
            yield i - 1


def _words_name_the_hook(command: str) -> bool:
    """Whether the words of ``command`` put an STM executable right before
    ``hook``. A fallback for the look-alike notice: it catches a line whose
    quoted executable name the raw search of ``mms doctor``'s candidate test
    does not see, such as ``m"m"s hook --host claude # pinned``, and a
    handler that test skips for its ``type``. The whole line is split, so a
    caller asks this only after that cheaper test has said no."""
    return next(_exe_before_hook(_lenient_shell_tokens(command)), None) is not None


# ---------------------------------------------------------------------------
# Per-host merge functions: (data, command, matcher) -> mutated data
# ---------------------------------------------------------------------------
#
# Each takes the host tag and the parsed config (a dict; ``{}`` when the file is
# absent) and
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


def _nested_install(
    host_tag: str, data: dict[str, Any], command: str, matcher: str | None
) -> dict[str, Any]:
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
        if not _group_has_owned_handler(host_tag, entry):
            out.append(entry)
            continue
        siblings = [h for h in entry["hooks"] if owned_handler(host_tag, h) is None]
        if not installed:
            installed = True
            old_handler = next(h for h in entry["hooks"] if owned_handler(host_tag, h) is not None)
            # A host may attach supported fields such as a timeout or status
            # message to the handler.  Refresh only STM-owned identity fields.
            refreshed_handler = {**old_handler, "type": "command", "command": command}
            if not siblings:
                # STM-only group: refresh matcher + command, but retain unknown
                # host fields on both the matcher group and handler.
                refreshed_entry = {**entry, "hooks": [refreshed_handler]}
                if matcher is not None:
                    refreshed_entry["matcher"] = matcher
                out.append(refreshed_entry)
            else:
                # Shared group: keep the user's matcher + siblings, swap STM's
                # handler in place (the first one, dropping any duplicates).
                new_inner: list[Any] = []
                replaced = False
                for h in entry["hooks"]:
                    if owned_handler(host_tag, h) is not None:
                        if not replaced:
                            new_inner.append(refreshed_handler)
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


def _nested_uninstall(host_tag: str, data: dict[str, Any]) -> dict[str, Any]:
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
            inner = [h for h in entry["hooks"] if owned_handler(host_tag, h) is None]
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


def _cursor_install(
    host_tag: str, data: dict[str, Any], command: str, matcher: str | None
) -> dict[str, Any]:
    """Merge Cursor's ``{version:1, hooks:{postToolUse:[{command}]}}`` (no matcher)."""
    if not isinstance(data.get("version"), int):
        data["version"] = 1
    entries = _ensure_list(_ensure_dict(data, "hooks"), "postToolUse")
    block = {"command": command}
    for i, entry in enumerate(entries):
        if owned_handler(host_tag, entry) is not None:
            entries[i] = {**entry, "command": command}
            return data
    entries.append(block)
    return data


def _cursor_uninstall(host_tag: str, data: dict[str, Any]) -> dict[str, Any]:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get("postToolUse"), list):
        return data
    kept = [e for e in hooks["postToolUse"] if owned_handler(host_tag, e) is None]
    if kept:
        hooks["postToolUse"] = kept
    else:
        hooks.pop("postToolUse", None)
    if not hooks:
        data.pop("hooks", None)
    return data


def _kimi_install(
    host_tag: str, data: dict[str, Any], command: str, matcher: str | None
) -> dict[str, Any]:
    """Merge Kimi's top-level ``[[hooks]]`` array of ``{event, matcher, command}``."""
    entries = _ensure_list(data, "hooks")
    block: dict[str, Any] = {"event": _KIMI_EVENT}
    if matcher is not None:
        block["matcher"] = matcher
    block["command"] = command
    for i, entry in enumerate(entries):
        if owned_handler(host_tag, entry) is not None:
            refreshed = {**entry, "command": command}
            if matcher is not None:
                refreshed["matcher"] = matcher
            entries[i] = refreshed
            return data
    entries.append(block)
    return data


def _kimi_uninstall(host_tag: str, data: dict[str, Any]) -> dict[str, Any]:
    entries = data.get("hooks")
    if not isinstance(entries, list):
        return data
    kept = [e for e in entries if owned_handler(host_tag, e) is None]
    if kept:
        data["hooks"] = kept
    else:
        data.pop("hooks", None)  # last hook removed → drop the empty array entirely
    return data


# ---------------------------------------------------------------------------
# Host registry
# ---------------------------------------------------------------------------

_InstallFn = Callable[[str, dict[str, Any], str, "str | None"], dict[str, Any]]
_UninstallFn = Callable[[str, dict[str, Any]], dict[str, Any]]


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


_HOME_FILES = {"claude": "settings.json", "codex": "config.toml", "kimi": "config.toml"}


def _config_path(spec: HookHostSpec) -> Path:
    """Resolve host homes without ignoring the host's supported home override.

    Raises :class:`HookInstallError` when STM cannot tell which directory the
    host's variable points at
    (:func:`~memtomem_stm.mms.host_homes.host_home_override`), rather than
    reading or writing a file relative to the current directory.
    """
    filename = _HOME_FILES.get(spec.host_tag)
    if filename is None:
        return spec.config_path.expanduser()
    home = host_home_override(spec.host_tag)
    if home is None:
        return spec.config_path.expanduser()
    if isinstance(home, HostHomeUnknown):
        raise HookInstallError(
            f"cannot tell which {spec.label} config to use: {HOST_HOME_UNKNOWN_REASONS[home.code]}"
        )
    return home / filename


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
    ``"remove"`` / ``"prune"`` (only emptied containers) / ``"absent"`` /
    ``"not_installed"`` for uninstall). ``unmanaged`` / ``unmanaged_after``
    locate the look-alike handlers left as they are, before and after.
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
    unmanaged: tuple[str, ...] = ()
    """Locations, in the current file, of handlers that look like STM's hook
    but that install/uninstall do not recognize, so they stay as they are."""
    unmanaged_after: tuple[str, ...] = ()
    """The same handlers' locations in ``new_text`` (a merge or removal can
    shift them)."""


def _read_current(path: Path, fmt: str) -> tuple[dict[str, Any], str] | None:
    """Parse an existing host config; ``None`` if absent.

    Raises :class:`HookInstallError` when the file exists but does not parse as
    ``fmt`` or is not a top-level table/object — we refuse to overwrite a config
    we cannot understand."""
    try:
        # Inside the ``try``: on Python 3.12/3.13 ``exists()`` raises
        # ``PermissionError`` for a file under an unsearchable directory, where
        # 3.14 returns False and the file reads as absent (#1285).
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        # The exception's own text quotes the offending byte; give the offset only.
        raise HookInstallError(
            f"{path} exists but is not valid UTF-8 (byte offset {exc.start}); refusing to "
            "modify it. Fix or remove the file, then re-run."
        ) from exc
    except OSError as exc:
        raise HookInstallError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw) if fmt == "json" else tomllib.loads(raw)
    except (json.JSONDecodeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise HookInstallError(
            f"{path} exists but is not valid {fmt.upper()} ({exc}); refusing to "
            "modify it. Fix or remove the file, then re-run."
        ) from exc
    except RecursionError as exc:
        raise HookInstallError(
            f"{path} is nested too deeply to parse (RecursionError); refusing to "
            "modify it. Fix or remove the file, then re-run."
        ) from exc
    if not isinstance(data, dict):
        raise HookConfigNotAnObject(
            f"{path} is valid {fmt.upper()} but not a top-level "
            f"{'object' if fmt == 'json' else 'table'}; refusing to modify it."
        )
    return data, raw


def _located_owned_hooks(
    host_tag: str, data: dict[str, Any]
) -> tuple[tuple[str, OwnedHookCommand], ...]:
    """The handlers :func:`owned_handler` accepts in parsed config ``data``,
    in file order, read from the containers install writes to, each with its
    location in the form :func:`unmanaged_stm_like` gives."""
    handlers: list[tuple[str, Any]] = []
    if host_tag in _NESTED_HOSTS:
        hooks = data.get("hooks")
        entries = hooks.get("PostToolUse", []) if isinstance(hooks, dict) else []
        if isinstance(entries, list):
            for i, entry in enumerate(entries):
                if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                    handlers.extend(
                        (f"hooks.PostToolUse[{i}].hooks[{j}]", handler)
                        for j, handler in enumerate(entry["hooks"])
                    )
    elif host_tag == "cursor":
        hooks = data.get("hooks")
        entries = hooks.get("postToolUse", []) if isinstance(hooks, dict) else []
        if isinstance(entries, list):
            handlers.extend((f"hooks.postToolUse[{i}]", e) for i, e in enumerate(entries))
    else:  # kimi
        entries = data.get("hooks", [])
        if isinstance(entries, list):
            handlers.extend((f"hooks[{i}]", e) for i, e in enumerate(entries))
    owned = ((location, owned_handler(host_tag, handler)) for location, handler in handlers)
    return tuple((location, hook) for location, hook in owned if hook is not None)


def _owned_hooks(host_tag: str, data: dict[str, Any]) -> tuple[OwnedHookCommand, ...]:
    """:func:`_located_owned_hooks` without the locations."""
    return tuple(hook for _, hook in _located_owned_hooks(host_tag, data))


# A ``PartialHostRuntimePolicy`` field, and the setting words of a hook
# command that set it, for naming a setting without its value.
CARRIED_SETTINGS: dict[str, str] = {
    "use_daemon": "--use-daemon/--no-daemon",
    "surfacing_timeout_seconds": "--surfacing-timeout-seconds",
    "daemon_timeout_seconds": "--daemon-timeout-seconds",
    "persist_query_text": "--persist-query-text/--no-persist-query-text",
}


class OwnedSettingsConflict(HookInstallError):
    """Install would fold owned handlers that differ in a setting it carries
    over from the first one, so the others' settings would be lost (#1470)."""


def _refuse_conflicting_owned(
    host_tag: str, path: Path, data: dict[str, Any], carried: frozenset[str]
) -> None:
    """Raise :class:`OwnedSettingsConflict` when ``host_tag`` folds owned
    handlers and the ones in ``data`` differ in a field of ``carried``.

    Only Claude Code and Codex fold: Cursor and Kimi refresh the first owned
    handler and leave the rest as they are. Settings are compared as parsed
    (:attr:`OwnedHookCommand.settings`), so a flagless handler (every field
    ``None``) differs from a pinned one. The message gives the locations and
    the settings' words, never a command or a value."""
    if host_tag not in _NESTED_HOSTS or not carried:
        return
    located = _located_owned_hooks(host_tag, data)
    if len(located) < 2:
        return
    differing = [
        field
        for field in CARRIED_SETTINGS
        if field in carried and len({getattr(hook.settings, field) for _, hook in located}) > 1
    ]
    if not differing:
        return
    # The install options that set a field; the other two have none.
    options = {
        "use_daemon": "--daemon or --no-daemon",
        "surfacing_timeout_seconds": "--surfacing-timeout",
    }
    chosen = [options[field] for field in differing if field in options]
    ways = f"pass {' and '.join(chosen)}, " if len(chosen) == len(differing) else ""
    raise OwnedSettingsConflict(
        f"{path} holds {len(located)} handlers that install manages "
        f"({', '.join(location for location, _ in located)}), and they differ in "
        f"{', '.join(CARRIED_SETTINGS[field] for field in differing)}. Install merges "
        "them into one and would keep only the first one's settings, so it left the "
        f"file unchanged and made no backup. To go on, {ways}pass --inherit-runtime-env "
        "(in place of --daemon, --no-daemon and --surfacing-timeout) to write the hook "
        "without runtime settings, or remove all but one of them by hand, then re-run."
    )


def unmanaged_stm_like(host_tag: str, data: dict[str, Any]) -> tuple[str, ...]:
    """Locations of handlers in ``data`` that look like STM's hook but that
    :func:`owned_handler` does not accept (#1453).

    Install and uninstall leave these as they are, so a hand-written
    ``mms hook --host claude`` would otherwise stay registered beside the
    installed hook with nothing said. "Look like" is ``mms doctor``'s
    candidate test, which errs toward matching, or an STM executable word
    right before ``hook`` (:func:`_words_name_the_hook`); only locations are
    returned, never command text,
    which can hold secret values. The containers are the ones install
    examines, and for Kimi every entry whatever its ``event``.
    """
    # Lazy: ``hook_registration`` imports ``_STM_EXECUTABLES`` from here.
    from memtomem_stm.cli.hook_registration import is_candidate

    found: list[str] = []

    def consider(location: str, handler: Any, *, typed: bool) -> None:
        if not isinstance(handler, dict):
            return
        command = handler.get("command")
        if not isinstance(command, str):
            return
        if owned_handler(host_tag, handler) is not None:
            return
        # Claude and Codex handlers name their ``type``, and only a ``command``
        # handler runs anything. Cursor and Kimi entries are commands whatever
        # else they hold, so the type is set last, over any ``type`` field of
        # their own.
        probe = handler if typed else {**handler, "type": "command"}
        # The word test reads ``command`` alone, so a handler that is not
        # owned for its ``type`` is still named.
        if is_candidate(probe) or _words_name_the_hook(command):
            found.append(location)

    if host_tag in _NESTED_HOSTS:
        hooks = data.get("hooks")
        entries = hooks.get("PostToolUse") if isinstance(hooks, dict) else None
        if isinstance(entries, list):
            for i, entry in enumerate(entries):
                inner = entry.get("hooks") if isinstance(entry, dict) else None
                if isinstance(inner, list):
                    for j, handler in enumerate(inner):
                        consider(f"hooks.PostToolUse[{i}].hooks[{j}]", handler, typed=True)
    elif host_tag == "cursor":
        hooks = data.get("hooks")
        entries = hooks.get("postToolUse") if isinstance(hooks, dict) else None
        if isinstance(entries, list):
            for i, entry in enumerate(entries):
                consider(f"hooks.postToolUse[{i}]", entry, typed=False)
    else:  # kimi: every entry, whatever its ``event``
        entries = data.get("hooks")
        if isinstance(entries, list):
            for i, entry in enumerate(entries):
                consider(f"hooks[{i}]", entry, typed=False)
    return tuple(found)


def _is_non_directory(path: Path) -> bool:
    """Whether ``path`` exists and is not a directory (``False`` when it cannot
    be inspected at all)."""
    try:
        return not stat.S_ISDIR(path.stat().st_mode)
    except OSError:
        return False


def claude_post_tool_handlers() -> tuple[Path, list[tuple[str, Any]] | None]:
    """Every ``hooks.PostToolUse[i].hooks[j]`` handler in Claude Code's user
    settings, keyed by that location, read from the file ``mms hook install
    --host claude`` writes. ``None`` in place of the list when the file is absent.

    Nothing is filtered: a reader that judges registrations (``mms doctor``,
    #1443) must also see the ones install does not own, such as
    ``NAME=value mms hook``.

    Raises :class:`HookConfigLocationUnknown` when ``CLAUDE_CONFIG_DIR`` names
    no usable directory, :class:`HookConfigNotRegularFile` for a path that is
    not a regular file (a FIFO would block the read), :class:`HookInstallError`
    (:class:`HookConfigNotAnObject` for a non-object root) for a file that
    cannot be read or parsed, and :class:`OSError` when it cannot be
    inspected. ``stat`` runs first because on Python 3.14 ``Path.exists``
    reports a file under an unsearchable directory as absent (#1285), which
    would hide that failure.
    """
    spec = HOOK_HOSTS["claude"]
    try:
        path = _config_path(spec)
    except HookInstallError as exc:
        raise HookConfigLocationUnknown(str(exc)) from None
    try:
        mode = path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError) as exc:
        # ``CLAUDE_CONFIG_DIR`` naming a file is the override's fault, not the
        # settings file's. Checked on the directory itself: below a file, POSIX
        # raises NotADirectoryError but Windows reports the path as not found.
        if host_home_override(spec.host_tag) is not None and _is_non_directory(path.parent):
            raise HookConfigLocationUnknown(
                f"{spec.label} config directory is not a directory"
            ) from None
        if isinstance(exc, FileNotFoundError):
            return path, None
        raise
    if not stat.S_ISREG(mode):
        raise HookConfigNotRegularFile(f"{path} is not a regular file")
    current = _read_current(path, spec.fmt)
    if current is None:
        return path, None
    hooks = current[0].get("hooks")
    entries = hooks.get("PostToolUse") if isinstance(hooks, dict) else None
    handlers: list[tuple[str, Any]] = []
    if isinstance(entries, list):
        for i, entry in enumerate(entries):
            inner = entry.get("hooks") if isinstance(entry, dict) else None
            if isinstance(inner, list):
                for j, handler in enumerate(inner):
                    handlers.append((f"hooks.PostToolUse[{i}].hooks[{j}]", handler))
    return path, handlers


def _serialize(fmt: str, data: dict[str, Any]) -> str:
    """Serialize merged config back to its on-disk format (trailing newline)."""
    if fmt == "json":
        return _json_dumps(data, indent=2, ensure_ascii=False) + "\n"
    text = tomli_w.dumps(data)
    return text if text.endswith("\n") else text + "\n"


def _render_block(host_tag: str, command: str) -> str:
    """A readable snippet of just STM's block, in the host's native format.

    Built by merging into an *empty* config so the preview shows exactly what
    ``install`` adds, not the whole rewritten file."""
    spec = HOOK_HOSTS[host_tag]
    only = spec.install_fn(host_tag, {}, command, matcher_for(host_tag))
    return _serialize(spec.fmt, only).rstrip("\n")


def _too_deep_to_rewrite(path: Path) -> HookInstallError:
    """The refusal for a config that parsed but is too deep to copy, merge or
    re-serialize: ``copy.deepcopy`` recurses in Python and fails first (#1285)."""
    return HookInstallError(
        f"{path} is nested too deeply to rewrite (RecursionError); refusing to "
        "modify it. Fix or remove the file, then re-run."
    )


CommandSource = str | Callable[[tuple[OwnedHookCommand, ...]], str]
"""The command :func:`plan_install` registers: the text itself, or a function
that builds it from the owned handlers already in the config (in file order),
for a caller that carries their settings over."""


def plan_install(
    host_tag: str, command: CommandSource, *, carried: frozenset[str] = frozenset()
) -> HookChange:
    """Plan registering ``command`` as ``host_tag``'s PostToolUse hook.

    Pure (no writes): parses the current config (if any), computes the merged
    result, and reports whether applying it would create / update / no-op. The
    merge is idempotent — an owned handler (:func:`owned_handler`) is replaced
    in place. Handlers that only look like STM's hook are left as they are and
    located in ``unmanaged`` (current file) and ``unmanaged_after``
    (``new_text``).

    The file is read once. A ``command`` function gets the owned handlers of
    that same read, so the settings it carries over and the handlers the merge
    replaces cannot come from two versions of the file; ``current_text`` is
    that read too, which :func:`apply_change` compares with the file before it
    writes.

    ``carried`` names the :data:`CARRIED_SETTINGS` fields the command takes
    from the first owned handler. Where the merge folds owned handlers into
    one (Claude Code, Codex) and they differ in one of those fields, the plan
    raises :class:`OwnedSettingsConflict` from the same read, before the
    command is built."""
    spec = HOOK_HOSTS[host_tag]
    path = _config_path(spec)
    current = _read_current(path, spec.fmt)
    base: dict[str, Any] = current[0] if current else {}
    current_text = current[1] if current else None
    _refuse_conflicting_owned(host_tag, path, base, carried)
    if not isinstance(command, str):
        command = command(_owned_hooks(host_tag, base))

    try:
        new_data = spec.install_fn(host_tag, copy.deepcopy(base), command, matcher_for(host_tag))
        new_text = _serialize(spec.fmt, new_data)
        unchanged = new_data == base
    except RecursionError as exc:
        raise _too_deep_to_rewrite(path) from exc

    if current is None:
        status, changed = "create", True
    elif unchanged:
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
        unmanaged=unmanaged_stm_like(host_tag, base),
        unmanaged_after=unmanaged_stm_like(host_tag, new_data),
    )


def plan_uninstall(host_tag: str) -> HookChange:
    """Plan removing STM's PostToolUse hook from ``host_tag``'s config.

    Removes the handlers :func:`owned_handler` accepts, the ones
    :func:`plan_install` writes or refreshes. A handler that only looks like
    STM's hook is left in place and reported in ``unmanaged``. A no-op when the
    file is absent or holds no owned handler, except that emptied
    containers (an empty matcher group, event list or ``hooks`` section) are
    still pruned (status ``prune``)."""
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
    try:
        new_data = spec.uninstall_fn(host_tag, copy.deepcopy(base))
        changed = new_data != base
        new_text = _serialize(spec.fmt, new_data)
    except RecursionError as exc:
        raise _too_deep_to_rewrite(path) from exc
    return HookChange(
        host_tag=host_tag,
        label=spec.label,
        path=path,
        fmt=spec.fmt,
        action="uninstall",
        # ``prune``: only empty containers go (a matcher group, event list or
        # ``hooks`` section with nothing in it), so
        # nothing the hook ran is removed and "Removed" would be false.
        status=(
            ("remove" if _owned_hooks(host_tag, base) else "prune") if changed else "not_installed"
        ),
        changed=changed,
        current_text=current_text,
        new_text=new_text,
        rendered_block="",
        notes=(),
        unmanaged=unmanaged_stm_like(host_tag, base),
        unmanaged_after=unmanaged_stm_like(host_tag, new_data),
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
    except UnicodeDecodeError as exc:
        raise HookInstallError(
            f"{path} changed after this change was planned and is no longer valid UTF-8 "
            f"(byte offset {exc.start}); nothing was written."
        ) from exc
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
        try:
            backup = _write_backup(path, change.current_text, mode)
        except OSError as exc:
            # Only the config is known untouched: ``_write_backup`` removes a
            # partial backup, but that removal can fail too (#1285 review).
            raise HookInstallError(
                f"cannot back up {path} before writing ({exc}); it was not changed."
            ) from exc
    try:
        atomic_write_text(path, change.new_text, mode=mode, durable=True)
    except OSError as exc:
        # ``atomic_write_text`` leaves the target untouched on failure; the
        # backup, if any, is a verbatim copy of it and stays (#1285).
        kept = f"; the backup at {backup} is a copy of it" if backup is not None else ""
        raise HookInstallError(f"cannot write {path} ({exc}); it was not changed{kept}.") from exc
    return backup
