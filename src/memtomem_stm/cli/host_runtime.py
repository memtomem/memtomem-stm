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

import logging
import math
import shlex
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DAEMON_GRACE_SECONDS = 1.0

_USE_DAEMON_ENV = "MEMTOMEM_STM_HOOK__USE_DAEMON"
_SURFACING_USE_DAEMON_ENV = "MEMTOMEM_STM_SURFACING__USE_DAEMON"
_SURFACING_TIMEOUT_ENV = "MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS"
_DAEMON_TIMEOUT_ENV = "MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS"
_PERSIST_QUERY_TEXT_ENV = "MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT"

_TRUE = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE = frozenset({"0", "false", "f", "no", "n", "off"})


def _shell_tokens(command: str) -> list[str]:
    """Split a shell command while exposing unquoted compound operators."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return command.split()


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
    """
    tokens = _shell_tokens(command)
    env_values: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key.startswith("MEMTOMEM_STM_"):
            env_values[key] = value

    option_values: dict[str, str] = {}
    flags: set[str] = set()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {
            "--use-daemon",
            "--no-daemon",
            "--persist-query-text",
            "--no-persist-query-text",
        }:
            flags.add(token)
        elif token in {"--surfacing-timeout-seconds", "--daemon-timeout-seconds"}:
            if i + 1 < len(tokens):
                option_values[token] = tokens[i + 1]
                i += 1
        elif token.startswith("--surfacing-timeout-seconds="):
            option_values["--surfacing-timeout-seconds"] = token.split("=", 1)[1]
        elif token.startswith("--daemon-timeout-seconds="):
            option_values["--daemon-timeout-seconds"] = token.split("=", 1)[1]
        i += 1

    use_daemon: bool | None
    if "--use-daemon" in flags:
        use_daemon = True
    elif "--no-daemon" in flags:
        use_daemon = False
    else:
        use_daemon = _parse_bool(
            env_values.get(_USE_DAEMON_ENV) or env_values.get(_SURFACING_USE_DAEMON_ENV)
        )

    persist_query_text: bool | None
    if "--persist-query-text" in flags:
        persist_query_text = True
    elif "--no-persist-query-text" in flags:
        persist_query_text = False
    else:
        persist_query_text = _parse_bool(env_values.get(_PERSIST_QUERY_TEXT_ENV))

    return PartialHostRuntimePolicy(
        use_daemon=use_daemon,
        surfacing_timeout_seconds=_parse_positive_float(
            option_values.get("--surfacing-timeout-seconds")
            or env_values.get(_SURFACING_TIMEOUT_ENV)
        ),
        daemon_timeout_seconds=_parse_positive_float(
            option_values.get("--daemon-timeout-seconds") or env_values.get(_DAEMON_TIMEOUT_ENV)
        ),
        persist_query_text=persist_query_text,
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
    from memtomem_stm.config import log_stm_config_failure, stm_config_for_cli

    try:
        config = stm_config_for_cli(config_path)
    except Exception as exc:
        # Observability only (#847): callers keep their own failure handling
        # (hook install renders a ClickException; init/register still abort).
        log_stm_config_failure(exc, logger=logger, context="resolving hook runtime policy")
        raise
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
