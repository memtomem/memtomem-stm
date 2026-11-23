"""Managed runtime policy serialized into MCP and native-hook registrations.

Host applications do not consistently inherit the shell environment in which
``mms register`` / ``mms hook install`` ran.  Keep the small set of settings
that define the shared-daemon contract in the registration itself so an MCP
server and a native hook discover the same warm daemon generation.

This module is intentionally internal to the CLI.  It does not add another
public configuration model: the existing ``STMConfig`` fields and environment
variables remain the source of truth at runtime.
"""

from __future__ import annotations

import math
import re
import shlex
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


# The lines whose ``shlex`` words are the words the shell passes on: plain
# characters and blanks, single-quoted runs (literal to both readers), and
# double-quoted runs without ``$``, backquote or ``\`` (nothing to expand or
# escape). On these, quote removal is the only step that changes words, and
# ``shlex`` performs it. It is the grammar ``shlex.join`` writes, so every
# command ``mms hook install`` writes matches. ``~/`` may open the line: the
# shell expands it to ``$HOME/``, which changes the executable's directory but
# not its basename, the part recognition reads (``~user/`` and ``~+/`` are
# refused, since they name another directory). Anything else (``#``,
# ``$``, a glob, an operator, a line break, ``\``, NUL, a ``~`` elsewhere) fails
# the match. ``\w`` is Unicode, so a non-ASCII path stays readable.
_SPLIT_GRAMMAR = re.compile(
    # Possessive (``++``, ``*+``): no backtracking positions are kept, so a
    # long line costs no memory beyond itself.
    r"(?:~(?=/))?(?:[\w@%+=:,./\- \t]++|'[^'\x00]*+'|\"[^\"$`\\\x00]*+\")*+"
)


def _shell_tokens(command: str) -> list[str] | None:
    """The words the shell passes on for ``command``, or ``None`` when the line
    holds shell syntax this split does not model (see ``_SPLIT_GRAMMAR``).

    ``None`` fails closed: ``mms hook install`` and ``uninstall`` do not treat
    the line as STM's hook, and no runtime setting is read from it. Operator
    characters inside quotes come out as words of their own, which the
    recognizer's operator guard still refuses."""
    if not _shell_tokens_accepts(command):
        return None
    return _lenient_shell_tokens(command)


def _shell_tokens_accepts(command: str) -> bool:
    """Whether :func:`_shell_tokens` reads ``command`` (a linear check that
    does not split it)."""
    return _SPLIT_GRAMMAR.fullmatch(command) is not None


def _lenient_shell_tokens(command: str) -> list[str]:
    """``shlex`` words of any line; on a parse error (an unbalanced quote, a
    trailing ``\\``), the words read before it, an empty word, then the
    ``str.split()`` words of the whole line, so neither reading is lost. An
    approximation of the shell, used only to decide whether a line that
    :func:`_shell_tokens` refuses still looks like STM's hook, never to act on
    it or read settings from it."""
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
        return [
            "--use-daemon" if normalized.use_daemon else "--no-daemon",
            "--surfacing-timeout-seconds",
            format_seconds(normalized.surfacing_timeout_seconds),
            "--daemon-timeout-seconds",
            format_seconds(normalized.daemon_timeout_seconds),
            (
                "--persist-query-text"
                if normalized.persist_query_text
                else "--no-persist-query-text"
            ),
        ]


def parse_managed_hook_runtime(command: str) -> PartialHostRuntimePolicy:
    """Recover managed flags or the pre-flag inline-``env`` representation.

    Invalid values are ignored.  The install command is operator-facing and
    will fall back to the current effective ``STMConfig`` for any missing
    field; the runtime bridge independently stays fail-open for hand edits.
    A command install does not recognize as STM's hook yields nothing.
    """
    # Lazy: ``hook_hosts`` imports this module.
    from memtomem_stm.cli.hook_hosts import _stm_hook_words
    from memtomem_stm.cli.hook_registration import hook_bridge_params

    # The words recognition reads (#1476): settings come from Click's own
    # parse of the words after ``hook``, so a repeated flag resolves as the
    # hook resolves it, and assignments only from a leading ``env`` launcher.
    words = _stm_hook_words(command)
    params = hook_bridge_params(list(words.args)) if words is not None else None
    if words is None or params is None:
        return PartialHostRuntimePolicy()
    # The hook applies its flags as an overlay on its environment
    # (``runtime_env_overrides``): an invalid flag value leaves the inline
    # setting in effect, and a surfacing deadline flag also sets the daemon
    # deadline (at least one second above it), over an inline one.
    env_values: dict[str, str] = {}
    for assignment in words.assignments:
        key, _sep, value = assignment.partition("=")
        env_values[key] = value

    def option(name: str) -> str | None:
        value = params.get(name)
        return value if isinstance(value, str) else None

    env_values.update(
        runtime_env_overrides(
            use_daemon=option("runtime_use_daemon"),
            surfacing_timeout_seconds=option("surfacing_timeout_seconds"),
            daemon_timeout_seconds=option("daemon_timeout_seconds"),
            persist_query_text=option("runtime_persist_query_text"),
        )
    )
    return PartialHostRuntimePolicy(
        # Only the variable the hook reads (``hook_cmd._daemon_enabled``):
        # ``MEMTOMEM_STM_SURFACING__USE_DAEMON`` sends the standalone proxy's
        # LTM calls to the daemon and says nothing about the hook's mode.
        use_daemon=_parse_bool(env_values.get(_USE_DAEMON_ENV)),
        surfacing_timeout_seconds=_parse_positive_float(env_values.get(_SURFACING_TIMEOUT_ENV)),
        daemon_timeout_seconds=_parse_positive_float(env_values.get(_DAEMON_TIMEOUT_ENV)),
        persist_query_text=_parse_bool(env_values.get(_PERSIST_QUERY_TEXT_ENV)),
    )


def resolve_host_runtime_policy(
    *,
    existing_command: str | None = None,
    use_daemon: bool | None = None,
    surfacing_timeout_seconds: float | None = None,
    config_path: str | Path | None = None,
) -> HostRuntimePolicy:
    """Resolve CLI overrides > existing registration > effective config.

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
    existing = (
        parse_managed_hook_runtime(existing_command)
        if existing_command is not None
        else PartialHostRuntimePolicy()
    )
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
    "HostRuntimePolicy",
    "PartialHostRuntimePolicy",
    "format_seconds",
    "parse_managed_hook_runtime",
    "resolve_host_runtime_policy",
    "runtime_env_overrides",
]
