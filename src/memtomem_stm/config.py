"""STM (Short-Term Memory) root configuration."""

from __future__ import annotations

from pathlib import Path

from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from memtomem_stm.proxy.config import ProxyConfig
from memtomem_stm.surfacing.config import SurfacingConfig


class LangfuseConfig(BaseModel):
    """Langfuse tracing configuration."""

    enabled: bool = False
    public_key: str = ""
    secret_key: str = ""
    host: str = ""
    sampling_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    """Fraction of proxy calls to trace (0.0–1.0).  Default 1.0 = all."""

    @model_validator(mode="after")
    def _require_keys_when_enabled(self) -> "LangfuseConfig":
        if self.enabled and not (self.public_key and self.secret_key):
            raise ValueError(
                "LangfuseConfig.enabled=true requires both public_key and secret_key "
                "to be set (non-empty)."
            )
        return self

    @model_validator(mode="after")
    def _require_langfuse_package_when_enabled(self) -> "LangfuseConfig":
        if self.enabled:
            from importlib.util import find_spec

            if find_spec("langfuse") is None:
                raise ValueError(
                    "LangfuseConfig.enabled=true but the 'langfuse' package is not "
                    "installed. Install the langfuse extra "
                    "(e.g. `uv tool install --reinstall 'memtomem-stm[langfuse]'` "
                    "or `pip install 'memtomem-stm[langfuse]'`)."
                )
        return self


class HookConfig(BaseModel):
    """Built-in tool hook (``mms hook``) settings — Stage 2 daemon integration.

    Env keys are ``MEMTOMEM_STM_HOOK__<field>`` (note the double underscore for
    nesting). The pre-existing ``MEMTOMEM_STM_HOOK_SURFACE_TOOLS`` (single
    underscore) is a separate direct-read knob in ``cli/hook_cmd.py`` and is
    unaffected by this model."""

    use_daemon: bool = True
    """When ``True`` (the default), ``mms hook`` routes surfacing through the
    local daemon (warm LTM connection) instead of spawning a cold LTM subprocess
    per call. The daemon is auto-spawned on first use (see ``auto_spawn``), so no
    manual ``mms daemon start`` is needed. Set
    ``MEMTOMEM_STM_HOOK__USE_DAEMON=0`` to opt out to the legacy cold in-process
    path."""
    daemon_timeout_seconds: float = Field(default=2.5, gt=0.0)
    """Per-request wall-clock budget for the hook→daemon round trip. Small and
    independent of the cold-path ``_hook_budget_seconds()`` backstop — a warm
    LTM search is sub-second, so this only needs to cover connect + RTT."""
    fallback: Literal["skip", "cold"] = "skip"
    """What ``mms hook`` does when the daemon is unreachable. ``skip`` (default)
    degrades to ``{}`` immediately — the daemon exists precisely to avoid the
    ~6s cold start, so the default never pays it. ``cold`` falls back to the
    in-process surfacing path (still bounded by ``_hook_budget_seconds()``),
    preserving pre-daemon behavior for callers who prefer it."""
    auto_spawn: bool = True
    """When ``True`` (and ``use_daemon``), ``mms hook`` fire-and-forget spawns
    the daemon if none is running, so the *next* call is warm — no manual
    ``mms daemon start`` needed. Lock-guarded against duplicate warm daemons.
    Disable with ``MEMTOMEM_STM_HOOK__AUTO_SPAWN=0``. Client-only knob (like
    ``use_daemon``/``fallback``) — excluded from the daemon config fingerprint,
    so a daemon started with it off still matches an auto-spawning hook."""
    record_feedback_events: bool = False
    """Passed to the daemon's ``SurfacingEngine``. Default ``False`` keeps
    cross-session dedup (``seen_memories``, memory IDs only) while persisting
    *no* query text and emitting no ``stm_surfacing_feedback`` rating prompt:
    the pure-hook path has no in-band channel for the model to return a rating,
    and a ``Bash`` query may carry secrets. See
    ``SurfacingEngine(record_feedback_events=...)``."""


class DaemonConfig(BaseModel):
    """Local surfacing daemon (``mms daemon``) settings — Stage 2.

    Env keys are ``MEMTOMEM_STM_DAEMON__<field>``."""

    host: str = "127.0.0.1"
    """Loopback bind address. The daemon is local-only and authenticated by a
    per-start random token, not by network ACLs — never bind a non-loopback
    address."""
    idle_timeout_seconds: float = Field(default=900.0, ge=0.0)
    """Shut the daemon (and its warm LTM child) down after this many seconds
    with no requests, so an abandoned coding session doesn't leak a
    multi-GB process forever. ``0`` disables idle shutdown (pin the process)."""


class STMConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMTOMEM_STM_",
        env_nested_delimiter="__",
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "WARNING"
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    surfacing: SurfacingConfig = Field(default_factory=SurfacingConfig)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
    hook: HookConfig = Field(default_factory=HookConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    data_dir: Path = Path("~/.memtomem")

    advertise_observability_tools: bool = False
    """Whether STM's own observability/admin MCP tools (``stm_proxy_stats``,
    ``stm_proxy_health``, ``stm_proxy_cache_clear``, ``stm_surfacing_stats``,
    ``stm_index_stats``, ``stm_compression_stats``,
    ``stm_progressive_stats``, ``stm_tuning_recommendations``) are
    advertised to MCP clients. When ``False``, these are not registered
    with the MCP server — useful for eager-loading clients (e.g. OpenAI
    Codex CLI) where every advertised tool pays schema tokens upfront. Set
    ``MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=true`` to opt these admin
    tools back into MCP advertisement. Claude Code defers tool schemas via
    its own mechanism so this flag has no effect there. Read via env var
    ``MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS`` at server import time."""

    def model_post_init(self, __context: object) -> None:
        # Propagate consumer_model from proxy to surfacing for model-aware defaults
        if self.proxy.consumer_model and not self.surfacing.consumer_model:
            self.surfacing.consumer_model = self.proxy.consumer_model
