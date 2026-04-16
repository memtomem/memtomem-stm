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

    def model_post_init(self, __context: object) -> None:
        # Propagate consumer_model from proxy to surfacing for model-aware defaults
        if self.proxy.consumer_model and not self.surfacing.consumer_model:
            self.surfacing.consumer_model = self.proxy.consumer_model
