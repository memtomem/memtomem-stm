"""Read a Claude Code hook handler the way ``mms hook`` would receive it (#1443).

``mms doctor`` uses this to report hook registrations whose ``--host`` keeps
hook compression from ever replacing output (#1437). The reading is
deliberately fact-only: it never models the host's shell. A handler is read
only when every word it would pass is a plain word, from a short whitelist of
characters that ``sh``, Bash and Click's Windows argument expansion all leave
alone. Anything else (quotes, ``$``, ``~``, ``#``, operators, line breaks, an
unknown launcher) is reported as not checked instead of guessed at.

The claim is limited to the arguments: *if* the line reaches ``mms hook``, this
is what Click makes of them. Whether the executable resolves, and whether
compression is enabled for the hook process, are not claimed.

Kept out of :mod:`memtomem_stm.cli.hook_cmd` so the runtime bridge's import
path does not grow; only doctor imports this module.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
import warnings
from dataclasses import dataclass
from typing import Any

import click

from memtomem_stm.cli.hook_hosts import _STM_EXECUTABLES

# A loose test for "this handler means to run STM's hook", independent of
# whether the line can be read: an STM executable name, then later a ``hook``
# word. Over-matching is safe because a match that does not fit a checkable
# shape is reported as not checked, never given a verdict.
# Two searches, not one ``exe.*?hook`` pattern, so a long line of repeated
# executable names without ``hook`` costs one scan rather than one per name: if
# no ``hook`` follows the first name, none follows a later one either.
_CANDIDATE_EXE = re.compile(
    # ``memtomem_stm`` is the module name (``python -m memtomem_stm hook``).
    r"(?<![A-Za-z0-9_-])(?:memtomem-stm-proxy|memtomem[-_]stm|mms)(?:\.exe)?(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
# No left boundary on ``hook``: ``mms ${ACTION:-hook}`` still means STM's hook.
_CANDIDATE_HOOK = re.compile(r"hook(?![A-Za-z0-9_-])", re.IGNORECASE)
# An executable word with ``=`` is a shell assignment when its prefix is a name
# (``OTHER=/mms hook`` runs ``hook``), and no path can hold a control character.
_NOT_EXECUTABLE = re.compile(r"[=\x00-\x1f\x7f]")
# Characters with no meaning to ``sh``/Bash and none to Click's Windows
# expansion (env vars, ``~``, globs). Quotes are excluded on purpose: a quoted
# word can look like an assignment and run as a command name.
_WORD = re.compile(r"[A-Za-z0-9_./:=+-]+")
_SHELL_LINE = re.compile(r"[A-Za-z0-9_./:=+\- \t]*")
# Only STM's own settings may prefix the command; any other name (``PATH``,
# Click's ``_MMS_COMPLETE``) can change what runs or how it dispatches.
_STM_ASSIGNMENT = re.compile(r"MEMTOMEM_STM_[A-Za-z0-9_]*=[A-Za-z0-9_./:=+-]*")

NOT_PLAIN = "shell syntax"
"""The line has characters outside the plain-word whitelist."""
LAUNCHER = "launcher"
"""The words do not have a shape this reader knows."""
SHELL_FIELD = "shell field"
"""The handler names a shell other than Bash."""
WINDOWS_SHELL = "windows shell form"
"""Shell form on Windows: Claude Code runs it under Git Bash, which may convert
path-like arguments, or under PowerShell when Git Bash is missing."""
ARGS_FIELD = "args field"
"""The handler's ``args`` is not a list of strings."""


@dataclass(frozen=True, slots=True)
class HookRegistration:
    """What ``mms hook`` would receive from one registered handler."""

    not_checked: str | None
    """Why the handler was not read (one of the module's reason constants), or
    ``None`` when it was."""
    runs_bridge: bool = False
    """Whether the arguments reach the runtime bridge: ``False`` for
    ``--help``/``-h``, a usage error, or a leftover token (a subcommand such as
    ``install``, or an unknown one)."""
    host: str | None = None
    """The effective ``--host`` value, or ``None`` when it cannot be told: the
    handler was not read, or a usage error stopped parsing early."""


def running_on_windows() -> bool:
    """Which platform rules :func:`inspect_hook_registration` should apply here.
    A seam so tests can pick the rules without faking ``sys.platform``."""
    return sys.platform == "win32"


def is_candidate(handler: Any) -> bool:
    """Whether ``handler`` is a command handler that means to run STM's hook."""
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    args = handler.get("args")
    text = command
    if isinstance(args, list):
        text += " " + " ".join(a for a in args if isinstance(a, str))
    elif isinstance(args, str):
        # Malformed (not a list), but still meant as STM's hook: report it.
        text += " " + args
    exe = _CANDIDATE_EXE.search(text)
    return exe is not None and _CANDIDATE_HOOK.search(text, exe.end()) is not None


def _executable_name(word: str, *, windows: bool) -> str:
    """The basename a launcher or executable word names, or ``""`` for a word
    that cannot be an executable at all."""
    if _NOT_EXECUTABLE.search(word):
        return ""
    # The last component as written, not a normalized path: ``mms/`` and
    # ``mms/.`` name directories, which pathlib would shorten to ``mms``.
    name = re.split(r"[\\/]" if windows else "/", word)[-1]
    if windows:
        name = name.lower()
        return name[: -len(".exe")] if name.endswith(".exe") else name
    return name


def _hook_args(words: list[str], *, exec_form: bool, windows: bool) -> list[str] | None:
    """The words after ``hook`` when ``words`` has a checkable shape, else ``None``."""
    i = 0
    if not exec_form:
        while i < len(words) and _STM_ASSIGNMENT.fullmatch(words[i]):
            i += 1
    # Not on Windows: there ``env`` is Git's MSYS ``env.exe``, which may convert
    # path-like arguments before starting a native executable.
    if not windows and i < len(words) and _executable_name(words[i], windows=False) == "env":
        start = i + 1
        i = start
        while i < len(words) and _STM_ASSIGNMENT.fullmatch(words[i]):
            i += 1
        if i == start:
            return None
    if (
        words[i : i + 3] == ["uv", "run", "--directory"]
        and i + 3 < len(words)
        and not words[i + 3].startswith("-")
    ):
        i += 4
        # ``uv run`` resolves a bare entry-point name; any other target (a path,
        # a URL, an option) is a script or an error, not STM's console script.
        if i < len(words) and words[i] not in _STM_EXECUTABLES:
            return None
    if i + 1 >= len(words) or words[i + 1] != "hook":
        return None
    if _executable_name(words[i], windows=windows) not in _STM_EXECUTABLES:
        return None
    return words[i + 2 :]


def _read_hook_args(args: list[str]) -> HookRegistration:
    """Parse ``args`` with ``mms hook``'s own Click options, never running it."""
    from memtomem_stm.cli.hook_cmd import hook_command
    from memtomem_stm.cli.proxy import CONTEXT_SETTINGS

    help_names = CONTEXT_SETTINGS["help_option_names"]
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            strict = hook_command.make_context("hook", list(args), help_option_names=help_names)
    except click.exceptions.Exit:
        # ``--help``/``-h``: parsing finished before the help callback exited,
        # so a resilient parse of the same words reads the host.
        resilient = hook_command.make_context(
            "hook", list(args), resilient_parsing=True, help_option_names=help_names
        )
        return _registration(resilient, runs_bridge=False)
    except Exception:
        # A usage error stops Click at the offending token, so options after
        # it were never read: the host is unknown (``--typo --host claude``).
        return HookRegistration(None, runs_bridge=False, host=None)
    return _registration(strict, runs_bridge=True)


def _registration(ctx: click.Context, *, runs_bridge: bool) -> HookRegistration:
    """The reading of a parsed context. A group stops reading options at its
    first leftover token (a subcommand name, an unknown word, or ``--``), so
    any later ``--host`` was never parsed and the host is unknown."""
    # Click 8.x keeps the first leftover token in ``protected_args``
    # (deprecated; folded into ``args`` in 9.0).
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        leftover = [*getattr(ctx, "protected_args", ()), *ctx.args]
    value = ctx.params.get("host")
    if leftover or not isinstance(value, str):
        return HookRegistration(None, runs_bridge=False, host=None)
    return HookRegistration(None, runs_bridge=runs_bridge, host=value)


def inspect_hook_registration(handler: dict[str, Any], *, windows: bool) -> HookRegistration:
    """Read a candidate handler (see :func:`is_candidate`).

    ``windows`` selects the platform rules: executable names lose ``.exe``,
    and neither a shell-form command nor an ``env`` launcher is read, because
    neither shell Claude Code may use there (Git Bash, PowerShell), nor Git's
    MSYS ``env.exe``, is guaranteed to pass the words through as written.
    A field that is present must have its documented type: ``"args": null``
    and ``"shell": null`` are not read as absent, and ``shell`` next to
    ``args`` is not read at all.
    """
    command: str = handler["command"]
    if "args" in handler:
        args = handler["args"]
        # Exec form: no shell; each element is one argument as written.
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            return HookRegistration(ARGS_FIELD)
        if "shell" in handler:
            # Whether Claude Code honors ``shell`` next to ``args`` is not
            # documented, so neither reading can be relied on.
            return HookRegistration(SHELL_FIELD)
        if not all(_WORD.fullmatch(a) for a in args):
            return HookRegistration(NOT_PLAIN)
        words = [command, *args]
        hook_args = _hook_args(words, exec_form=True, windows=windows)
    else:
        if "shell" in handler and handler["shell"] != "bash":
            return HookRegistration(SHELL_FIELD)
        if windows:
            return HookRegistration(WINDOWS_SHELL)
        if not _SHELL_LINE.fullmatch(command):
            return HookRegistration(NOT_PLAIN)
        hook_args = _hook_args(command.split(), exec_form=False, windows=windows)
    if hook_args is None:
        return HookRegistration(LAUNCHER)
    return _read_hook_args(hook_args)
