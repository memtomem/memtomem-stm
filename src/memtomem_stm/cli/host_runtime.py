"""Managed runtime policy serialized into MCP and native-hook registrations.

Host applications do not consistently inherit the shell environment in which
``mms register`` / ``mms hook install`` ran.  Keep the small set of settings
that define the shared-daemon contract in the registration itself so an MCP
server and a native hook discover the same warm daemon generation.

The words of the hook command ``mms hook install`` writes are defined here
too (:func:`hook_command_words`), next to the reader that decides whether an
existing command has that form (:func:`owned_hook_command`).

This module is intentionally internal to the CLI.  It does not add another
public configuration model: the existing ``STMConfig`` fields and environment
variables remain the source of truth at runtime.
"""

from __future__ import annotations

import math
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_DAEMON_GRACE_SECONDS = 1.0

_USE_DAEMON_ENV = "MEMTOMEM_STM_HOOK__USE_DAEMON"
_SURFACING_USE_DAEMON_ENV = "MEMTOMEM_STM_SURFACING__USE_DAEMON"
_SURFACING_TIMEOUT_ENV = "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS"
_DAEMON_TIMEOUT_ENV = "MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS"
_PERSIST_QUERY_TEXT_ENV = "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT"

_TRUE = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE = frozenset({"0", "false", "f", "no", "n", "off"})

HOOK_ENTRY_POINT = "memtomem-stm"
"""The entry-point name ``mms hook install`` writes, bare or after ``uv run``."""

_UV_LAUNCHER_HEAD = ("uv", "run", "--directory")
_DAEMON_FLAGS = {True: "--use-daemon", False: "--no-daemon"}
_PERSIST_FLAGS = {True: "--persist-query-text", False: "--no-persist-query-text"}


def hook_launcher(directory: str | None = None) -> list[str]:
    """The launcher words of an installed hook command: the bare console
    script, or ``uv run --directory <directory>`` in front of it."""
    if directory is None:
        return [HOOK_ENTRY_POINT]
    return [*_UV_LAUNCHER_HEAD, directory, HOOK_ENTRY_POINT]


def hook_command_words(
    launcher: Sequence[str], host: str, policy_words: Sequence[str]
) -> list[str]:
    """The words of an installed hook command. ``mms hook install`` joins them
    with ``shlex.join``, and :func:`owned_hook_command` reads a command back
    through this same function, so the two cannot drift apart."""
    return [*launcher, "hook", "--host", host, *policy_words]


def _policy_words(daemon_flag: str, surfacing: str, daemon: str, persist_flag: str) -> list[str]:
    """The runtime-setting words of an installed hook command, in the order
    every release since 0.1.40 has written them. A new written form is added
    beside this one, never by editing it: commands written by older releases
    must stay recognizable."""
    return [
        daemon_flag,
        "--surfacing-timeout-seconds",
        surfacing,
        "--daemon-timeout-seconds",
        daemon,
        persist_flag,
    ]


def _lenient_shell_tokens(command: str) -> list[str]:
    """``shlex`` words of any line; on a parse error (an unbalanced quote, a
    trailing ``\\``), the words read before it, an empty word, then the
    ``str.split()`` words of the whole line, so neither reading is lost. An
    approximation of the shell, used only to decide whether a handler that
    ``mms hook install`` does not own still looks like STM's hook, never to
    act on it or read settings from it. ``shlex`` is quadratic in a word's
    length."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    words: list[str] = []
    try:
        for word in lexer:
            words.append(word)
    except ValueError:
        # The empty word keeps a pair from forming across the two readings:
        # it is neither an executable name nor ``hook``.
        return [*words, "", *command.split()]
    return words


def _parse_bool(raw: str | None) -> bool | None:
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


def _parse_positive_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0.0:
        return None
    return value


def format_seconds(value: float) -> str:
    """Stable, compact rendering for argv/environment serialization."""
    return format(value, ".15g")


@dataclass(frozen=True)
class PartialHostRuntimePolicy:
    """Settings recovered from an existing managed registration."""

    use_daemon: bool | None = None
    surfacing_timeout_seconds: float | None = None
    daemon_timeout_seconds: float | None = None
    persist_query_text: bool | None = None


@dataclass(frozen=True)
class HostRuntimePolicy:
    """Concrete policy written into a host registration."""

    use_daemon: bool
    surfacing_timeout_seconds: float
    daemon_timeout_seconds: float
    persist_query_text: bool = False

    def normalized(self) -> "HostRuntimePolicy":
        """Keep the hook round-trip outside the daemon's LTM deadline.

        The hook timeout covers connect, request framing, the daemon-side LTM
        search, and the reply.  It therefore needs a small margin above the LTM
        search deadline instead of expiring first and turning a valid late
        result into an apparent daemon failure.
        """
        daemon_timeout = max(
            self.daemon_timeout_seconds,
            self.surfacing_timeout_seconds + _DAEMON_GRACE_SECONDS,
        )
        return HostRuntimePolicy(
            use_daemon=self.use_daemon,
            surfacing_timeout_seconds=self.surfacing_timeout_seconds,
            daemon_timeout_seconds=daemon_timeout,
            persist_query_text=self.persist_query_text,
        )

    def mcp_env(self) -> dict[str, str]:
        """Environment values managed for a newly-created MCP registration."""
        return {
            _SURFACING_USE_DAEMON_ENV: "true" if self.use_daemon else "false",
            _SURFACING_TIMEOUT_ENV: format_seconds(self.surfacing_timeout_seconds),
            _PERSIST_QUERY_TEXT_ENV: "true" if self.persist_query_text else "false",
        }

    def hook_args(self) -> list[str]:
        """Cross-platform flags managed in a native hook command."""
        normalized = self.normalized()
        return _policy_words(
            _DAEMON_FLAGS[normalized.use_daemon],
            format_seconds(normalized.surfacing_timeout_seconds),
            format_seconds(normalized.daemon_timeout_seconds),
            _PERSIST_FLAGS[normalized.persist_query_text],
        )


@dataclass(frozen=True)
class OwnedHookCommand:
    """A hook command in the form ``mms hook install`` writes."""

    launcher: tuple[str, ...]
    """:func:`hook_launcher`'s words for the command."""
    settings: PartialHostRuntimePolicy
    """The command's flags parsed into a policy, which install carries over on
    refresh; every field ``None`` for a command without them. Not the hook's
    input: a deadline the hook keeps but its build rejects (one
    ``format_seconds`` writes as ``1.79769313486232e+308``) is dropped here.
    The hook's input from the flags is :meth:`env_overrides`."""
    policy_words: tuple[str, ...] = ()
    """The command's six setting words as written, or none."""

    def env_overrides(self) -> dict[str, str]:
        """The environment overlay ``mms hook`` applies from the command's
        flags, before its build validates them."""
        return _hook_overlay(self.policy_words)


# What ``format_seconds`` can write for a deadline, read as text: digits, an
# optional fraction and an optional exponent, or ``inf`` / ``nan``. Text and
# not a parsed number, because releases wrote ``inf``, ``nan`` and
# ``1.79769313486232e+308`` (which ``float`` reads as infinity) when given
# such a deadline, so the slot cannot require a finite value. ``9.0`` passes
# where the writer prints ``9``; ``9e`` and ``1..2`` are not numbers at all.
_NUMBER = r"(?:[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?|inf|nan)"
_POLICY_TAIL = re.compile(
    " "
    + " ".join(
        _policy_words(
            "(" + "|".join(_DAEMON_FLAGS.values()) + ")",
            f"({_NUMBER})",
            f"({_NUMBER})",
            "(" + "|".join(_PERSIST_FLAGS.values()) + ")",
        )
    )
)
# ``/…``, ``C:\…`` or ``C:/…``, ``\\server\…``. The writer takes the directory
# from ``Path.cwd()``, and a relative word can mean something else to a host
# shell: ``shlex.join`` leaves ``@project`` bare, which PowerShell splats.
_ABSOLUTE_DIRECTORY = re.compile(r"/|[A-Za-z]:[\\/]|\\\\")


def owned_hook_command(command: str, host: str) -> OwnedHookCommand | None:
    """``command`` read as a hook command ``mms hook install --host <host>``
    writes, or ``None`` when it does not have that form.

    The form is :func:`hook_command_words` joined by ``shlex.join``: a
    :func:`hook_launcher`, ``hook --host <host>``, then either nothing or the
    six setting words. The directory of a ``uv run`` launcher is any absolute
    path and each deadline is a number as ``format_seconds`` spells one, or
    ``inf`` / ``nan``; everything else is
    fixed, down to the spacing and the quoting ``shlex.join`` chooses.

    The last step decides: the command must equal the join of the words read
    from it. The steps before only propose those words, so a mistake in them
    can refuse a command but cannot accept one the writer's join would not
    produce. Nothing here models a shell, and the cost is linear in the
    command's length (no ``shlex.split``)."""
    marker = " " + " ".join(hook_command_words((), host, ()))
    at = command.rfind(marker)
    if at < 0:
        return None
    head, tail = command[:at], command[at + len(marker) :]
    policy_words: list[str] = []
    if tail:
        match = _POLICY_TAIL.fullmatch(tail)
        if match is None:
            return None
        policy_words = _policy_words(*match.groups())
    if head == HOOK_ENTRY_POINT:
        launcher = hook_launcher()
    else:
        before, after = " ".join(_UV_LAUNCHER_HEAD) + " ", " " + HOOK_ENTRY_POINT
        if not (head.startswith(before) and head.endswith(after)):
            return None
        directory = _unquoted(head[len(before) : len(head) - len(after)])
        if "\x00" in directory or not _ABSOLUTE_DIRECTORY.match(directory):
            return None
        launcher = hook_launcher(directory)
    if shlex.join(hook_command_words(launcher, host, policy_words)) != command:
        return None
    return OwnedHookCommand(tuple(launcher), _hook_settings(policy_words), tuple(policy_words))


def _unquoted(word: str) -> str:
    """The string ``shlex.quote`` wrote ``word`` for, when it is one of the
    two shapes ``shlex.quote`` writes; any other text comes back unchanged
    and fails the comparison in :func:`owned_hook_command`."""
    if len(word) >= 2 and word[0] == word[-1] == "'":
        return word[1:-1].replace("'\"'\"'", "'")
    return word


def _hook_overlay(policy_words: Sequence[str]) -> dict[str, str]:
    """The variables ``mms hook`` sets when given ``policy_words``: the strings
    its Click options hand :func:`runtime_env_overrides` (each daemon and
    persist flag's ``flag_value``, the deadline words as written)."""
    if not policy_words:
        return {}
    daemon_flag, _, surfacing, _, daemon, persist_flag = policy_words
    return runtime_env_overrides(
        use_daemon="true" if daemon_flag == _DAEMON_FLAGS[True] else "false",
        surfacing_timeout_seconds=surfacing,
        daemon_timeout_seconds=daemon,
        persist_query_text="true" if persist_flag == _PERSIST_FLAGS[True] else "false",
    )


def _hook_settings(policy_words: Sequence[str]) -> PartialHostRuntimePolicy:
    """``policy_words`` parsed into a policy.

    Read from :func:`_hook_overlay`, so a deadline that is not a positive
    finite number is dropped and the daemon deadline is at least a second
    above a usable surfacing one, as at run time. A value that the overlay
    writes but that does not read back as finite is dropped too."""
    overlay = _hook_overlay(policy_words)
    return PartialHostRuntimePolicy(
        use_daemon=_parse_bool(overlay.get(_USE_DAEMON_ENV)),
        surfacing_timeout_seconds=_parse_positive_float(overlay.get(_SURFACING_TIMEOUT_ENV)),
        daemon_timeout_seconds=_parse_positive_float(overlay.get(_DAEMON_TIMEOUT_ENV)),
        persist_query_text=_parse_bool(overlay.get(_PERSIST_QUERY_TEXT_ENV)),
    )


def resolve_host_runtime_policy(
    *,
    existing: PartialHostRuntimePolicy | None = None,
    use_daemon: bool | None = None,
    surfacing_timeout_seconds: float | None = None,
    config_path: str | Path | None = None,
) -> HostRuntimePolicy:
    """Resolve CLI overrides > existing registration > effective config.

    *existing* is the settings of the hook command being replaced
    (:attr:`OwnedHookCommand.settings`), ``None`` for a fresh registration.
    Query text remains non-persistent for a fresh managed registration.  An
    existing registration that explicitly opted in is retained on refresh;
    ``--inherit-runtime-env`` is the escape hatch for removing all serialized
    management from a hook command.

    *config_path* is the config file the caller is registering or diagnosing
    (#839): ``init``/``register`` serialize that exact path into the managed
    entry's environment, so the policy must resolve against it too — a bare
    construction reads the default/env path, which can name a different file.
    """
    from memtomem_stm.config import stm_config_for_cli

    # No log line here (#847 review round 1): the command that invoked the
    # resolution owns the failure's output — ``mms hook install`` renders a
    # ClickException with the env-var hint, and a resolver-side
    # ``exc_info`` log printed the raw traceback right before it.
    config = stm_config_for_cli(config_path)
    if existing is None:
        existing = PartialHostRuntimePolicy()
    policy = HostRuntimePolicy(
        use_daemon=(
            use_daemon
            if use_daemon is not None
            else existing.use_daemon
            if existing.use_daemon is not None
            else config.hook.use_daemon
        ),
        surfacing_timeout_seconds=(
            surfacing_timeout_seconds
            if surfacing_timeout_seconds is not None
            else existing.surfacing_timeout_seconds
            if existing.surfacing_timeout_seconds is not None
            else config.surfacing.timeout_seconds
        ),
        daemon_timeout_seconds=(
            existing.daemon_timeout_seconds
            if existing.daemon_timeout_seconds is not None
            else config.hook.daemon_timeout_seconds
        ),
        persist_query_text=(
            existing.persist_query_text if existing.persist_query_text is not None else False
        ),
    )
    return policy.normalized()


def runtime_env_overrides(
    *,
    use_daemon: str | None,
    surfacing_timeout_seconds: str | None,
    daemon_timeout_seconds: str | None,
    persist_query_text: str | None,
) -> dict[str, str]:
    """Validate runtime flag strings and return the env overlay to apply.

    Values deliberately arrive as strings so a hand-edited invalid number does
    not let Click exit 2 in a non-interactive host hook.  Invalid values are
    omitted and the ambient/default configuration wins.  When both deadlines
    are usable, the hook deadline is normalized above the surfacing deadline.
    """
    overrides: dict[str, str] = {}
    parsed_use_daemon = _parse_bool(use_daemon)
    if parsed_use_daemon is not None:
        overrides[_USE_DAEMON_ENV] = "true" if parsed_use_daemon else "false"

    surfacing = _parse_positive_float(surfacing_timeout_seconds)
    daemon = _parse_positive_float(daemon_timeout_seconds)
    if surfacing is not None:
        overrides[_SURFACING_TIMEOUT_ENV] = format_seconds(surfacing)
    if daemon is not None or surfacing is not None:
        effective_daemon = daemon or 0.0
        if surfacing is not None:
            effective_daemon = max(effective_daemon, surfacing + _DAEMON_GRACE_SECONDS)
        if effective_daemon > 0.0:
            overrides[_DAEMON_TIMEOUT_ENV] = format_seconds(effective_daemon)

    parsed_persist = _parse_bool(persist_query_text)
    if parsed_persist is not None:
        overrides[_PERSIST_QUERY_TEXT_ENV] = "true" if parsed_persist else "false"
    return overrides


__all__ = [
    "HOOK_ENTRY_POINT",
    "HostRuntimePolicy",
    "OwnedHookCommand",
    "PartialHostRuntimePolicy",
    "format_seconds",
    "hook_command_words",
    "hook_launcher",
    "owned_hook_command",
    "resolve_host_runtime_policy",
    "runtime_env_overrides",
]
