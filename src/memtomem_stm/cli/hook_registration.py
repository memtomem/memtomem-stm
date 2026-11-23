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
path does not grow. Its callers are ``mms doctor``, and ``mms hook install``
and ``mms hook uninstall``, which import lazily: :func:`runs_hook_bridge` to
decide which handlers are STM's own, and :func:`is_candidate` (through
``hook_hosts.unmanaged_stm_like``) to name look-alike handlers they leave alone.
"""

from __future__ import annotations

import contextlib
import copy
import io
import re
import shlex
import sys
import warnings
from dataclasses import dataclass
from typing import Any

import click

from memtomem_stm.cli.hook_hosts import _STM_EXECUTABLES

# The loose fallback of :func:`is_candidate` for lines with shell syntax: an STM
# executable name, then later a ``hook`` word, anywhere in the raw text.
# Over-matching is safe because a match that does not fit a checkable shape is
# reported as not checked, never given a verdict.
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
# The plain characters plus quotes and nothing else: no ``\``, ``$``, backtick,
# ``#``, line break, operator or redirection. In such a line the shell's only
# step that changes words is quote removal, which ``shlex`` performs, so its
# words are a fair (not exact: assignments and reserved words are not modelled)
# reading of what the shell joins. Used only to decide candidacy, never to read.
_QUOTED_LINE = re.compile(r"[A-Za-z0-9_./:=+\- \t'\"]*")
# ``shlex`` grows a token by repeated concatenation, which is quadratic in its
# length; past this a line gets only the raw search.
_QUOTED_MAX_CHARS = 4096
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
    """Whether ``handler`` looks like a command handler meant to run STM's hook.

    A heuristic for which handlers to report on, not a reading of them. Plain
    words are the arguments as passed, so they must hold a word naming an STM
    executable and a later word ``hook``. A shell line of at most
    ``_QUOTED_MAX_CHARS`` characters made only of plain words and quotes also
    counts when the words left by quote removal do (``m"m"s hook``), in
    addition to the loose raw-text search every other line gets, longer ones
    included. It over-matches some commands that only mention STM (``echo
    m"m"s ho"ok"``), and other shell syntax can still hide a hook.
    """
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    args = handler.get("args")
    if isinstance(args, list):
        # Exec form: no shell, so each element is one argument as written.
        words = [command, *(a for a in args if isinstance(a, str))]
        if all(_WORD.fullmatch(word) for word in words):
            return _names_the_hook(words)
        return _raw_names_the_hook(" ".join(words))
    text = command
    if isinstance(args, str):
        # Malformed (not a list), but still meant as STM's hook: report it.
        text += " " + args
    if _SHELL_LINE.fullmatch(text):
        return _names_the_hook(text.split())
    if len(text) <= _QUOTED_MAX_CHARS and _QUOTED_LINE.fullmatch(text):
        words_or_none = _quoted_words(text)
        if words_or_none is not None and _names_the_hook(words_or_none):
            return True
    return _raw_names_the_hook(text)


def _names_the_hook(words: list[str]) -> bool:
    """Whether a word names an STM executable and a later word is ``hook``.
    Linear: only the first such word is tried, since a ``hook`` after a later
    one is also after the first."""
    for i, word in enumerate(words):
        if _CANDIDATE_EXE.search(word):
            return any(later.casefold() == "hook" for later in words[i + 1 :])
    return False


def _raw_names_the_hook(text: str) -> bool:
    exe = _CANDIDATE_EXE.search(text)
    return exe is not None and _CANDIDATE_HOOK.search(text, exe.end()) is not None


def _quoted_words(text: str) -> list[str] | None:
    """The words of a quotes-only line after quote removal, or ``None`` when
    its quotes do not balance."""
    try:
        return shlex.split(text)
    except ValueError:
        return None


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


def runs_hook_bridge(args: list[str]) -> bool:
    """Whether ``mms hook`` given ``args`` (the words after ``hook``) would run
    the runtime bridge rather than help, a usage error or a subcommand such as
    ``install``. ``mms hook install`` and ``uninstall`` use it to decide which
    handlers are STM's own (#1460)."""
    return _parse_hook_args(args)[0].runs_bridge


def hook_bridge_params(args: list[str]) -> dict[str, Any] | None:
    """The option values ``mms hook`` would run the bridge with, given ``args``
    (the words after ``hook``), or ``None`` when it would not run the bridge
    (see :func:`runs_hook_bridge`). ``mms hook install`` reads an existing
    handler's runtime settings from these, so a repeated flag and an
    assignment-shaped ``--host`` value read as the hook reads them (#1476)."""
    return _parse_hook_args(args)[1]


def _read_hook_args(args: list[str]) -> HookRegistration:
    """Parse ``args`` with ``mms hook``'s own Click options, never running it."""
    return _parse_hook_args(args)[0]


def _parse_hook_args(args: list[str]) -> tuple[HookRegistration, dict[str, Any] | None]:
    """The reading of ``args`` and, for a bridge run, Click's parsed values."""
    from memtomem_stm.cli.hook_cmd import hook_command
    from memtomem_stm.cli.proxy import CONTEXT_SETTINGS

    help_names = CONTEXT_SETTINGS["help_option_names"]
    # Click (8.1.8+) builds a command's help option once and caches it on the
    # command, named by whichever context asked first. Parse on a copy with no
    # cache, so an earlier ``--help``-only context cannot turn ``-h`` into a
    # usage error here, and this reading does not hand ``-h`` to later callers
    # of ``hook_command``. Building the option appends to ``params``, so the
    # copy gets its own list.
    reader = copy.copy(hook_command)
    reader.params = list(hook_command.params)
    reader._help_option = None
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            strict = reader.make_context("hook", list(args), help_option_names=help_names)
    except click.exceptions.Exit:
        # ``--help``/``-h``: parsing finished before the help callback exited,
        # so a resilient parse of the same words reads the host.
        resilient = reader.make_context(
            "hook", list(args), resilient_parsing=True, help_option_names=help_names
        )
        return _registration(resilient, runs_bridge=False), None
    except Exception:
        # A usage error stops Click at the offending token, so options after
        # it were never read: the host is unknown (``--typo --host claude``).
        return HookRegistration(None, runs_bridge=False, host=None), None
    registration = _registration(strict, runs_bridge=True)
    return registration, dict(strict.params) if registration.runs_bridge else None


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
