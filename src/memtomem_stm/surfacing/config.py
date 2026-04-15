"""Surfacing configuration models."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from memtomem_stm.proxy.config import MODEL_CONTEXT_WINDOWS


class ToolSurfacingConfig(BaseModel):
    """Per-tool override for surfacing behavior."""

    enabled: bool = True
    query_template: str = ""
    namespace: str | None = None
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    max_results: int | None = Field(default=None, gt=0)


class SurfacingConfig(BaseModel):
    """Proactive memory surfacing configuration.

    LTM access is always remote-only via the MCP protocol. The surfacing
    engine spawns (or connects to) a memtomem MCP server using the
    ltm_mcp_command / ltm_mcp_args settings below.
    """

    enabled: bool = True
    feedback_db_path: Path = Path("~/.memtomem/stm_feedback.db")
    """Path to the SQLite store for surfacing events, feedback, and
    ``seen_memories`` cross-session dedup. Configurable so tests and
    notebooks can isolate state into a tempdir via
    ``MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH``."""
    ltm_mcp_command: str = "memtomem-server"
    ltm_mcp_args: list[str] = []
    min_score: float = Field(default=0.02, ge=0.0, le=1.0)
    max_results: int = Field(default=3, gt=0)
    min_query_tokens: int = Field(default=3, gt=0)
    cooldown_seconds: float = Field(default=5.0, ge=0.0)
    timeout_seconds: float = Field(default=3.0, gt=0.0)
    injection_mode: Literal["prepend", "append", "section"] = "prepend"
    section_header: str = "## Relevant Memories"
    default_namespace: str | None = None
    exclude_tools: list[str] = []
    write_tool_patterns: list[str] = [
        "*write*",
        "*create*",
        "*delete*",
        "*push*",
        "*send*",
        "*remove*",
    ]
    context_tools: dict[str, ToolSurfacingConfig] = {}
    feedback_enabled: bool = True
    max_surfacings_per_minute: int = Field(default=15, gt=0)
    cache_ttl_seconds: float = Field(default=60.0, ge=0.0)
    circuit_max_failures: int = Field(default=3, ge=0)
    circuit_reset_seconds: float = Field(default=60.0, gt=0.0)
    auto_tune_enabled: bool = True
    auto_tune_min_samples: int = Field(default=20, gt=0)
    auto_tune_score_increment: float = Field(default=0.002, gt=0.0)
    min_response_chars: int = Field(default=5000, ge=0)
    include_session_context: bool = True
    fire_webhook: bool = True
    max_injection_chars: int = Field(default=3000, gt=0)
    context_window_size: int = Field(default=0, ge=0)
    """0=disabled; >0 expands ±N adjacent chunks."""
    dedup_ttl_seconds: float = Field(default=604800.0, ge=0.0)
    """7 days default; 0 disables cross-session dedup."""
    consumer_model: str = ""
    result_format: Literal["compact", "structured"] = "compact"
    """Parser format for mem_search output. ``compact`` is the legacy
    core format (``[rank] score | source``). ``structured`` selects the
    machine-parseable JSON format (``{"results": [...]}``) with automatic
    version negotiation — falls back to compact if core is too old."""

    def _context_tokens(self) -> int | None:
        if not self.consumer_model:
            return None
        for prefix, tokens in MODEL_CONTEXT_WINDOWS.items():
            if self.consumer_model.startswith(prefix):
                return tokens
        return None

    def effective_max_injection_chars(self) -> int:
        """Scale max_injection_chars by model context window.

        SLM (≤32K): 1500 chars — minimal, high-density injection
        Medium (32K-200K): 3000 chars — default
        LLM (>200K): 5000 chars — richer context from memories
        """
        ctx = self._context_tokens()
        if ctx is None:
            return self.max_injection_chars
        if ctx <= 32000:
            return min(self.max_injection_chars, 1500)
        if ctx > 200000:
            return max(self.max_injection_chars, 5000)
        return self.max_injection_chars

    def effective_max_results(self) -> int:
        """Scale max_results by model context window.

        SLM (≤32K): 2 results — fit in tight context
        Medium (32K-200K): 3 results — default
        LLM (>200K): 5 results — can process more memories
        """
        ctx = self._context_tokens()
        if ctx is None:
            return self.max_results
        if ctx <= 32000:
            return min(self.max_results, 2)
        if ctx > 200000:
            return max(self.max_results, 5)
        return self.max_results
