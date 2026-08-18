"""Shared defaults for memtomem CLI commands.

Keep path defaults outside the command modules so help text and runtime
resolution cannot drift when commands live in modules that import each other.
``ProxyConfig.config_path`` keeps an intentionally independent runtime-model
default in ``proxy/config.py`` because the proxy layer must not import CLI code.
"""

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
    option declares ``default=None``), so the environment governs — resolved
    through ``collect_proxy_env_overrides``, i.e. pydantic-settings' own
    resolution (case folding, the bare ``MEMTOMEM_STM_PROXY`` JSON payload,
    ``__`` explosion), so every spelling that steers the server's
    ``ProxyConfig.config_path`` steers the CLI the same way (#837's lesson:
    never reimplement the settings resolution). Only then the shared default.
    Without this, a no-flag run split across two files: the command's file
    loads used the Click default while its ``STMConfig``-backed checks
    honored the env var (#848).

    The returned path is the raw string: every use site already expands with
    ``Path(...).expanduser().resolve()``, matching how the env value arrives
    (see ``stm_config_for_cli``). A present-but-EMPTY env value falls back to
    the default: pydantic coerces ``""`` to ``Path(".")``, which the server's
    file load degrades on (a directory is never a readable config), while a
    CLI command needs a concrete file target — resolving it to the working
    directory made every file-loading command crash on a directory and let
    ``register`` serialize one. A non-string value (a JSON payload's number,
    say) also falls through: the server rejects that environment at startup,
    and the CLI keeps its pre-#848 target rather than inventing one.
    """
    if config_path is not None:
        return ResolvedConfigPath(config_path, "flag")
    from memtomem_stm.proxy.config import collect_proxy_env_overrides

    fragment = collect_proxy_env_overrides().fragment
    env_value = fragment.get("config_path")
    if isinstance(env_value, str) and env_value:
        return ResolvedConfigPath(env_value, "env")
    return ResolvedConfigPath(str(DEFAULT_PROXY_CONFIG), "default")
