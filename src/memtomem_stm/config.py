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


class STMConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMTOMEM_STM_",
        env_nested_delimiter="__",
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "WARNING"
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    surfacing: SurfacingConfig = Field(default_factory=SurfacingConfig)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
    data_dir: Path = Path("~/.memtomem")

    advertise_observability_tools: bool = True
    """Whether STM's own observability/admin MCP tools (``stm_proxy_stats``,
    ``stm_proxy_health``, ``stm_proxy_cache_clear``, ``stm_surfacing_stats``,
    ``stm_compression_stats``, ``stm_tuning_recommendations``) are advertised
    to MCP clients. When ``False``, these are hidden from ``tools/list`` but
    remain callable via the ``mms`` CLI — useful for eager-loading clients
    (e.g. OpenAI Codex CLI) where every advertised tool pays schema tokens
    upfront. Claude Code defers tool schemas via its own mechanism so this
    flag has no effect there. Read via env var
    ``MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS`` at server import time."""

    def model_post_init(self, __context: object) -> None:
        # Propagate consumer_model from proxy to surfacing for model-aware defaults
        if self.proxy.consumer_model and not self.surfacing.consumer_model:
            self.surfacing.consumer_model = self.proxy.consumer_model
