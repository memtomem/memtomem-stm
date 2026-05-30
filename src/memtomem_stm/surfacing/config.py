"""Surfacing configuration models."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from memtomem_stm.proxy.config import MODEL_CONTEXT_WINDOWS


class ToolSurfacingConfig(BaseModel):
    """Per-tool override for surfacing behavior."""

    enabled: bool = True
    query_template: str = ""
    namespace: str | None = None
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    """Per-tool override. Takes precedence over the auto-tuner when set,
    even if ``auto_tune_enabled=True``."""
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
    min_score: float = Field(default=0.03, ge=0.0, le=1.0)
    max_results: int = Field(default=3, gt=0)
    min_query_tokens: int = Field(default=3, gt=0)
    cooldown_seconds: float = Field(default=5.0, ge=0.0)
    timeout_seconds: float = Field(default=3.0, gt=0.0)
    # #348: default flipped from ``"prepend"`` to ``"append"`` so the
    # progressive-delivery path actually surfaces memories. ``"prepend"``
    # would shift character offsets and break the
    # ``PROGRESSIVE_FOOTER_TOKEN`` concat invariant ``stm_proxy_read_more``
    # depends on, so the progressive path short-circuits in that mode
    # (see ``ProxyManager._apply_surfacing_on_progressive``). ``"prepend"``
    # remains valid for non-progressive responses where the agent should
    # see memories first.
    injection_mode: Literal["prepend", "append", "section"] = "append"
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
    auto_tune_score_ceiling: float = Field(default=0.05, gt=0.0, le=1.0)
    """Upper bound auto-tuning may raise a tool's min_score to. Left at its
    default it widens to ``max(0.05, min_score)`` so a stricter min_score
    never breaks construction; set it explicitly to cap below the default."""
    auto_tune_score_floor: float = Field(default=0.005, ge=0.0, le=1.0)
    """Lower bound auto-tuning may lower a tool's min_score to. Left at its
    default it widens to ``min(0.005, min_score)``."""
    min_response_chars: int = Field(default=5000, ge=0)
    include_session_context: bool = True
    fire_webhook: bool = True
    max_injection_chars: int = Field(default=3000, gt=0)
    context_window_size: int = Field(default=0, ge=0)
    """0=disabled; >0 expands ±N adjacent chunks."""
    result_content_max_chars: int = Field(default=500, gt=0)
    """Max chars retained per LTM result by the parser. Trims long content
    before it reaches the formatter."""
    preview_max_chars: int = Field(default=300, gt=0)
    """Max chars rendered per result preview in the injected memory block."""
    dedup_ttl_seconds: float = Field(default=604800.0, ge=0.0)
    """7 days default; 0 disables cross-session dedup."""
    query_retention_days: int = Field(default=30, ge=0)
    """#352 part 2 — days to keep the raw extracted query string in
    ``surfacing_events.query`` before the opportunistic cleanup nulls it.
    ``0`` disables retention-driven null-out (the column keeps whatever
    ``record_surfacing`` wrote, indefinitely). The event row itself is
    never deleted by this knob — only the ``query`` column is cleared,
    so aggregate counts in ``stm_surfacing_stats`` are unaffected. Part 3
    of #352 (``persist_query_text=False`` opt-in hashing) is the
    write-side counterpart to this read-side retention sweep."""
    persist_query_text: bool = True
    """#352 part 3 — when ``True`` (default, backward-compatible),
    ``FeedbackStore`` stores the verbatim extracted query in
    ``surfacing_events.query``. When ``False``, the engine substitutes
    ``"sha256:" + sha256(query)[:16]`` before handing the value to the
    store, so the persisted column never contains user-derived text.
    The hashed value still survives ``stm_surfacing_stats`` rendering
    (``query_preview`` returns it verbatim) and the server formatter
    emits a one-line legend explaining the substitution. Part 2's
    ``query_retention_days`` is the read-side counterpart that ages
    out raw text on a TTL for operators who keep the default and want
    bounded retention rather than full opt-out."""
    consumer_model: str = ""
    result_format: Literal["compact", "structured"] = "compact"
    """Parser format for mem_search output. ``compact`` is the legacy
    core format (``[rank] score | source``). ``structured`` selects the
    machine-parseable JSON format (``{"results": [...]}``) with automatic
    version negotiation — falls back to compact if core is too old."""

    @model_validator(mode="after")
    def _validate_auto_tune_bounds(self) -> SurfacingConfig:
        """Keep floor <= min_score <= ceiling and the increment no larger
        than the band it moves within.

        The default ceiling/floor (0.05 / 0.005) bracket the default
        min_score (0.03). When an operator raises or lowers min_score
        without touching the bounds, the unset bound widens to include it so
        a previously-valid config never fails. Explicitly setting a bound on
        the wrong side of min_score is a real misconfiguration and is
        rejected.

        The increment guard rejects only a step bigger than the whole
        [floor, ceiling] band — such a step would jump from one clamp to the
        other in a single adjustment. Sub-band increments (the 0.002 default
        crosses the band in ~22 steps) are accepted as-is; calibrating the
        step further is a wait-for-signal concern, not a constructor
        constraint.
        """
        set_fields = self.model_fields_set
        if "auto_tune_score_ceiling" not in set_fields:
            self.auto_tune_score_ceiling = max(self.auto_tune_score_ceiling, self.min_score)
        if "auto_tune_score_floor" not in set_fields:
            self.auto_tune_score_floor = min(self.auto_tune_score_floor, self.min_score)

        if not (self.auto_tune_score_floor <= self.min_score <= self.auto_tune_score_ceiling):
            raise ValueError(
                "auto-tune bounds must satisfy floor <= min_score <= ceiling; got "
                f"floor={self.auto_tune_score_floor}, min_score={self.min_score}, "
                f"ceiling={self.auto_tune_score_ceiling}"
            )
        band = self.auto_tune_score_ceiling - self.auto_tune_score_floor
        if band > 0 and self.auto_tune_score_increment > band:
            raise ValueError(
                f"auto_tune_score_increment={self.auto_tune_score_increment} exceeds the "
                f"auto-tune band [{self.auto_tune_score_floor}, "
                f"{self.auto_tune_score_ceiling}] (width {band}); a single step would "
                "jump the entire range. Reduce the increment or widen the band."
            )
        return self

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
