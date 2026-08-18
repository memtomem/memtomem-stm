"""Shared defaults for memtomem CLI commands.

Keep path defaults outside the command modules so help text and runtime
resolution cannot drift when commands live in modules that import each other.
``ProxyConfig.config_path`` keeps an intentionally independent runtime-model
default in ``proxy/config.py`` because the proxy layer must not import CLI code.
"""

import os
from pathlib import Path
from typing import Literal, NamedTuple


DEFAULT_PROXY_CONFIG = Path("~/.memtomem/stm_proxy.json")

_CONFIG_PATH_ENV = "MEMTOMEM_STM_PROXY__CONFIG_PATH"


class ResolvedConfigPath(NamedTuple):
    """A CLI command's proxy-config path plus the source that named it."""

    path: str
    source: Literal["flag", "env", "default"]


def resolve_cli_config_path(config_path: str | None) -> ResolvedConfigPath:
    """Resolve a command's ``--config`` value: explicit flag > env > default.

    ``None`` means the operator did not type the flag (every ``--config``
    option declares ``default=None``), so ``MEMTOMEM_STM_PROXY__CONFIG_PATH``
    governs — the same variable the server reads into
    ``ProxyConfig.config_path`` — and only then the shared default. Without
    this, a no-flag run split across two files: the command's file loads used
    the Click default while its ``STMConfig``-backed checks honored the env
    var (#848).

    The returned path is the raw string: every use site already expands with
    ``Path(...).expanduser().resolve()``, matching how the env value arrives
    (see ``stm_config_for_cli``). A PRESENT but empty env value is honored,
    not skipped — parity with ``_resolve_config_path_for_completion``, which
    pins why: reading the default file instead would target a file the
    running server never names. The env lookup is exact-uppercase only (the
    documented spelling); pydantic-settings is case-insensitive, and a
    casing that only it accepts keeps governing the ``STMConfig`` half.
    """
    if config_path is not None:
        return ResolvedConfigPath(config_path, "flag")
    env = os.environ.get(_CONFIG_PATH_ENV)
    if env is not None:
        return ResolvedConfigPath(env, "env")
    return ResolvedConfigPath(str(DEFAULT_PROXY_CONFIG), "default")
