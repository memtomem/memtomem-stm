"""Proxy gateway configuration."""

from __future__ import annotations

import json
import logging
import os
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)


_PROXY_ENV_PREFIX = "MEMTOMEM_STM_PROXY__"


def collect_proxy_env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a nested dict from ``MEMTOMEM_STM_PROXY__*`` env vars.

    Used to layer env-set proxy fields on top of the JSON config file so the
    documented precedence (env > file > defaults) holds end-to-end. Without
    this, the file-load path in ``server.py`` would clobber every env-set
    field (``MEMTOMEM_STM_PROXY__ENABLED`` included — the file load is
    unconditional; env wins purely through this overlay).

    The returned dict mirrors the JSON config shape — nested by ``__``
    delimiters, lower-cased — and pydantic's coercion handles type
    conversion at validation time.
    """
    env = environ if environ is not None else dict(os.environ)
    overrides: dict[str, Any] = {}
    for key, val in env.items():
        if not key.startswith(_PROXY_ENV_PREFIX):
            continue
        path = [p.lower() for p in key[len(_PROXY_ENV_PREFIX) :].split("__") if p]
        if not path:
            continue
        cursor = overrides
        for part in path[:-1]:
            existing = cursor.get(part)
            if not isinstance(existing, dict):
                existing = {}
                cursor[part] = existing
            cursor = existing
        cursor[path[-1]] = val
    return overrides


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *overrides* on top of *base*; returns a new dict."""
    out = dict(base)
    for k, v in overrides.items():
        existing = out.get(k)
        if isinstance(v, dict) and isinstance(existing, dict):
            out[k] = _deep_merge(existing, v)
        else:
            out[k] = v
    return out


def _env_override_hint(
    exc: Exception,
    env_overrides: dict[str, Any] | None,
    file_data: dict[str, Any] | None = None,
) -> str:
    """Name the env var(s) implicated in a config ValidationError.

    A malformed ``MEMTOMEM_STM_PROXY__*`` value fails validation of the
    MERGED config, and the load falls back to defaults with a single warning
    — without this hint the operator sees "Failed to parse proxy config
    <file>" and debugs the FILE while the file is fine. (Fallback semantics
    are unchanged: this only improves the warning.)

    Attribution is two-staged:

    1. DIFFERENTIAL pre-filter — every error of the merged config whose
       ``(loc, type, msg)`` the file reproduces ON ITS OWN is file-caused
       and skipped: an env var that merely touched the same subtree (an
       innocent ``tail_mode`` under a hybrid block whose ordering the file
       itself breaks) must not be implicated. The file-alone validation runs
       lazily, once, only on this already-failing path. The error TYPE keeps
       an env value that re-breaks a file-broken location DIFFERENTLY
       (gt-violation -> int-parsing) attributed; the MESSAGE disambiguates
       model validators, which all share ``type="value_error"`` — a
       file-caused duplicate-prefix error must not mask a separate
       env-caused empty-prefix error at the same root location (and an env
       var that changes an aggregated root message, e.g. adds a collision,
       is attributed too).

    2. NAMING — hints are derived from the env overlay's LEAVES, the var
       names the operator actually set, never synthesized from the error
       location alone (a model-level validator reports at the MODEL's path,
       e.g. ``upstream_servers.gh.hybrid``, and naming that prefix would
       point at a var that was never set):

       - a location resolving to an env leaf names that leaf (also when the
         location runs PAST it — the env string replaced a container);
       - a location resolving to an env subtree — a cross-field validator
         there, or a missing required field of an env-created entry — names
         every env leaf under it;
       - a ROOT-level error (``loc=()``, duplicate/empty upstream prefixes)
         has no path at all and names every env leaf;
       - a location the env never touched names nothing.
    """
    if not env_overrides or not isinstance(exc, ValidationError):
        return ""
    implicated: set[str] = set()

    def _add_leaves(path: list[str], subtree: dict[str, Any]) -> None:
        stack: list[tuple[list[str], dict[str, Any]]] = [(path, subtree)]
        while stack:
            prefix, node = stack.pop()
            for key, value in node.items():
                if isinstance(value, dict):
                    stack.append(([*prefix, str(key)], value))
                else:
                    implicated.add(
                        _PROXY_ENV_PREFIX + "__".join(p.upper() for p in [*prefix, str(key)])
                    )

    def _error_key(e: dict[str, Any]) -> tuple[tuple[Any, ...], str, str]:
        return (tuple(e.get("loc", ())), str(e.get("type", "")), str(e.get("msg", "")))

    file_error_keys: frozenset[tuple[tuple[Any, ...], str, str]] | None = None

    def _file_alone_error_keys() -> frozenset[tuple[tuple[Any, ...], str, str]]:
        if file_data is None:
            return frozenset()  # no file at all: every failure is env-caused
        try:
            ProxyConfig.model_validate(file_data)
        except ValidationError as file_exc:
            return frozenset(_error_key(e) for e in file_exc.errors())
        except Exception:  # non-pydantic failure: attribute nothing to the file
            return frozenset()
        return frozenset()

    for err in exc.errors():
        loc = err.get("loc", ())
        if file_error_keys is None:
            file_error_keys = _file_alone_error_keys()
        if _error_key(err) in file_error_keys:
            continue  # the file fails this same check on its own — file-caused
        if not loc:
            _add_leaves([], env_overrides)
            continue
        node: Any = env_overrides
        consumed: list[str] = []
        for part in loc:
            if isinstance(node, dict) and part in node:
                node = node[part]
                consumed.append(str(part))
            else:
                break
        if not consumed:
            continue  # the env overlay never touched this error's path
        if not isinstance(node, dict):
            # Landed on an env leaf — exact when consumed == loc; when the
            # error location goes deeper, this leaf REPLACED a container the
            # model expected, so it is still the var to fix.
            implicated.add(_PROXY_ENV_PREFIX + "__".join(p.upper() for p in consumed))
        else:
            # A subtree the env touched: a cross-field validator error there
            # (consumed == loc) or a walk that broke inside it (e.g. the
            # missing required field of an env-created entry — the file-
            # caused variants were already filtered above). Name the env
            # leaves under it.
            _add_leaves(consumed, node)
    if not implicated:
        return ""
    return " (env override(s) implicated: " + ", ".join(sorted(implicated)) + ")"


class CompressionStrategy(StrEnum):
    NONE = "none"
    AUTO = "auto"
    TRUNCATE = "truncate"
    EXTRACT_FIELDS = "extract_fields"
    SCHEMA_PRUNING = "schema_pruning"
    SKELETON = "skeleton"
    LLM_SUMMARY = "llm_summary"
    SELECTIVE = "selective"
    HYBRID = "hybrid"
    PROGRESSIVE = "progressive"


class TailMode(StrEnum):
    TOC = "toc"
    TRUNCATE = "truncate"


class TransportType(StrEnum):
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable_http"


class ExposureProfile(StrEnum):
    """Operating mode for the tool-exposure hard filter (#465).

    Controls how *signal-based* eligibility rules (runtime health,
    sensitive-metadata scan) are enforced at tool-advertisement time.
    Structural rules (composed-name overflow, duplicate names) and explicit
    config rules (``hidden``, ``expose_in_profiles``) apply in every profile
    — a profile never overrides what the operator wrote or what would break
    the client.

    - ``strict`` (default): signal rules hard-reject — flagged tools are not
      advertised, with the reason recorded in selection telemetry.
    - ``review``: signal rules demote instead of reject — flagged tools stay
      advertised but carry a ``risk_penalty`` in tool-relevance telemetry
      (#466), so an operator can observe what *would* be hidden.
    - ``explore``: signal rules are off.
    """

    STRICT = "strict"
    REVIEW = "review"
    EXPLORE = "explore"


class LLMProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"


class LLMCompressorConfig(BaseModel):
    provider: LLMProvider = LLMProvider.OPENAI
    model: str = "gpt-4.1-mini"
    api_key: str = ""
    base_url: str = ""
    system_prompt: str = (
        "Summarize the following content concisely, preserving all key information. "
        "Keep the summary under {max_chars} characters."
    )
    max_tokens: int = Field(default=500, gt=0)
    # Timeout for a single LLM compression call. A slow or hung LLM endpoint
    # would otherwise freeze the pipeline AFTER the upstream has already
    # responded — outside the upstream ``call_timeout_seconds`` (#206).
    # On timeout the compressor falls back to TruncateCompressor (matching
    # other LLM failure modes: privacy, circuit_breaker, llm_error).
    llm_timeout_seconds: float = Field(default=60.0, gt=0.0)
    # When true, scan the upstream response for API keys / passwords / JWT /
    # private keys before sending it to the LLM provider; on a hit, skip the
    # outbound call and fall back to TruncateCompressor (last_fallback="privacy").
    # Default-on: an operator who flips ``compression: llm_summary`` should not
    # have to remember a second knob to avoid leaking credentials to OpenAI /
    # Anthropic / a custom ``base_url``. Set to false only when the response
    # body is known to be sensitive-free or you are using a self-hosted
    # provider you trust (e.g. local Ollama). See #289.
    privacy_scan_enabled: bool = True

    @model_validator(mode="after")
    def _require_api_key_for_hosted_providers(self) -> LLMCompressorConfig:
        # Deliberately EAGER: every llm block in the config is validated at
        # load, even when the strategy that would use it is not selected
        # (compression: truncate with an attached llm block, or
        # extraction.enabled: false). The trade was reviewed 2026-06-11 and
        # kept: deferring the key check to first use would also defer the
        # failure of a genuinely-enabled compressor from startup to the first
        # tool call. Operators pasting an example llm block they don't use
        # yet should remove it (or use provider: ollama) — documented in
        # docs/compression.md.
        if self.provider not in (LLMProvider.OPENAI, LLMProvider.ANTHROPIC):
            return self
        if self.api_key:
            return self
        env_var = "OPENAI_API_KEY" if self.provider == LLMProvider.OPENAI else "ANTHROPIC_API_KEY"
        env_val = os.environ.get(env_var, "").strip()
        if env_val:
            self.api_key = env_val
            return self
        raise ValueError(
            f"api_key is required for provider='{self.provider.value}' "
            f"(set api_key in config or the {env_var} environment variable)"
        )


class CleaningConfig(BaseModel):
    enabled: bool = True
    strip_html: bool = True
    deduplicate: bool = True
    collapse_links: bool = True


class HybridConfig(BaseModel):
    head_chars: int = Field(default=5000, gt=0)
    tail_mode: TailMode = TailMode.TOC
    min_toc_budget: int = Field(default=200, gt=0)
    min_head_chars: int = Field(default=100, gt=0)
    head_ratio: float = Field(default=0.6, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_ordering(self) -> Self:
        # min_head_chars > head_chars makes HybridCompressor's head-budget
        # guard fire on every call, silently degrading the operator's chosen
        # hybrid strategy to plain truncation. Reject the combination at load
        # like UpstreamServerConfig does for its dependent numeric fields,
        # instead of accepting a config that is structurally a no-op.
        if self.min_head_chars > self.head_chars:
            raise ValueError(
                f"min_head_chars ({self.min_head_chars}) must be <= head_chars ({self.head_chars})"
            )
        return self


class SelectiveConfig(BaseModel):
    max_pending: int = Field(default=100, gt=0)
    pending_ttl_seconds: float = Field(default=300.0, ge=0.0)
    json_depth: int = Field(default=1, gt=0)
    min_section_chars: int = Field(default=50, ge=0)
    pending_store: Literal["memory", "sqlite"] = "memory"
    pending_store_path: Path = Path("~/.memtomem/pending_selections.db")


class AutoIndexConfig(BaseModel):
    enabled: bool = False
    background: bool = False
    min_chars: int = Field(default=2000, ge=0)
    memory_dir: Path = Path("~/.memtomem/proxy_index")
    namespace: str = "proxy-{server}"


class ExtractionStrategy(StrEnum):
    """Strategy for automatic fact extraction from tool responses."""

    NONE = "none"
    LLM = "llm"
    HEURISTIC = "heuristic"
    HYBRID = "hybrid"


def _default_extraction_llm() -> LLMCompressorConfig:
    """Default LLM config for fact extraction: Ollama qwen3:4b (no-think mode)."""
    return LLMCompressorConfig(
        provider=LLMProvider.OLLAMA,
        model="qwen3:4b",
        base_url="http://localhost:11434",
        system_prompt=(
            "/no_think\n"
            "You are a knowledge extraction system. Extract discrete, atomic facts "
            "from the following tool response.\n\n"
            "Rules:\n"
            "- Each fact must be a single, self-contained statement\n"
            "- Categorize: decision, preference, technical, process, relationship, "
            "definition, reference\n"
            "- Rate confidence 0.0-1.0\n"
            "- Extract up to {max_facts} most important facts\n"
            "- Skip boilerplate, navigation, and UI text\n"
            "- Include relevant tags\n\n"
            "Respond ONLY with a JSON array:\n"
            '[{{"content": "...", "category": "...", "confidence": 0.8, '
            '"tags": ["tag1"]}}]'
        ),
        max_tokens=1000,
    )


class ExtractionConfig(BaseModel):
    """Configuration for automatic fact extraction from tool responses."""

    enabled: bool = False
    strategy: ExtractionStrategy = ExtractionStrategy.LLM
    llm: LLMCompressorConfig | None = None
    max_facts: int = Field(default=10, gt=0)
    min_response_chars: int = Field(default=500, ge=0)
    dedup_threshold: float = Field(default=0.92, ge=0.0, le=1.0)
    memory_dir: Path = Path("~/.memtomem/extracted_facts")
    namespace: str = "facts-{server}"
    background: bool = True
    max_input_chars: int = Field(default=20000, gt=0)

    def effective_llm(self) -> LLMCompressorConfig:
        """Return user-provided LLM config or the default (Ollama qwen3:4b)."""
        return self.llm or _default_extraction_llm()


class ProgressiveConfig(BaseModel):
    """Configuration for progressive (cursor-based) delivery."""

    chunk_size: int = Field(default=4000, gt=0)
    """Characters per chunk delivered to the agent."""
    max_stored: int = Field(default=200, gt=0)
    """Maximum concurrent stored progressive responses."""
    ttl_seconds: float = Field(default=1800.0, ge=0.0)
    """Time-to-live for stored responses (seconds)."""
    include_structure_hint: bool = True
    """Include remaining headings/structure hint in first chunk footer."""


class ToolOverrideConfig(BaseModel):
    compression: CompressionStrategy | None = None
    max_result_chars: int | None = Field(default=None, gt=0)
    max_result_tokens: int | None = Field(default=None, gt=0)
    """Token-equivalent budget for this tool. When set, takes precedence over
    ``max_result_chars`` and is converted to a char budget via the resolved
    ``chars_per_token`` ratio. Useful for non-Latin-script content where a
    fixed char budget under-triggers compression. See ``token_estimate.py``."""
    chars_per_token: float | None = Field(default=None, gt=0.0)
    """Per-tool override for the chars-per-token ratio used to convert
    ``max_result_tokens`` to a char budget. Falls back to the upstream
    server's ratio, then ``ProxyConfig.chars_per_token``."""
    retention_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    """Override the dynamic retention floor for this tool.

    When set, the ratio guard uses this value instead of the global
    size-based scaling (<1KB→0.9, <3KB→0.75, etc.).  Useful for tools
    whose responses tolerate more aggressive compression or, conversely,
    for tools where even small losses are costly.
    """
    llm: LLMCompressorConfig | None = None
    selective: SelectiveConfig | None = None
    hybrid: HybridConfig | None = None
    progressive: ProgressiveConfig | None = None
    cleaning: CleaningConfig | None = None
    auto_index: bool | None = None
    extraction: bool | None = None
    hidden: bool = False
    description_override: str | None = None
    expose_in_profiles: list[ExposureProfile] | None = None
    """Exposure profiles in which this tool is advertised (#465).

    ``None`` (default) means every profile. A set list restricts the tool to
    those profiles — e.g. ``["explore"]`` keeps a destructive admin tool out
    of production exposure. Overrides the upstream-level
    ``expose_in_profiles`` when both are set. An empty list is equivalent to
    ``hidden: true``. This is a visibility constraint only; it does not
    exempt the tool from signal-based rules in the profiles where it is
    visible."""


class OriginSource(BaseModel):
    """One host-config location an imported upstream entry came from (#475)."""

    kind: str
    """Machine-readable source kind: ``claude-user`` / ``claude-project`` /
    ``mcp-json`` / ``claude-desktop``. Kept in lockstep with the CLI's shared
    source table (``cli/proxy.py``); a plain ``str`` rather than an enum so a
    config written by a newer CLI with a new kind still validates here."""
    path: str | None = None
    """Filesystem anchor for path-scoped kinds: the resolved project dir for
    ``claude-project``, the ``.mcp.json`` path for ``mcp-json``."""
    pruned: bool = False
    """``True`` once this source's host entry was removed by a prune writer.
    Per-source rather than per-entry because prune permits partial failure
    across the primary source and its duplicates."""
    pruned_at: str | None = None


class UpstreamOrigin(BaseModel):
    """Import provenance for an upstream entry (#475).

    Written by the CLI import paths (``mms init`` / ``mms add --import``) so
    the entry can later be restored to its host config verbatim (``mms
    eject``). The proxy runtime never reads it — the field exists here to
    document the schema and give the CLI a validated constructor.

    ``original`` is the verbatim host entry and may contain secrets
    (``env`` / ``headers``); CLI ``--json`` outputs must strip it (see the
    redacted serializer in ``cli/proxy.py``) rather than dumping it.
    """

    schema_version: int = 1
    source: OriginSource
    duplicates: list[OriginSource] = []
    imported_at: str | None = None
    original: dict[str, Any] | None = None


class UpstreamServerConfig(BaseModel):
    command: str = ""
    args: list[str] = []
    env: dict[str, str] | None = None
    prefix: str
    transport: TransportType = TransportType.STDIO
    url: str = ""
    headers: dict[str, str] | None = None
    compression: CompressionStrategy = CompressionStrategy.AUTO
    max_result_chars: int = Field(default=8000, gt=0)
    max_result_tokens: int | None = Field(default=None, gt=0)
    """Token-equivalent budget for this upstream. When set, takes precedence
    over ``max_result_chars`` and is converted to a char budget via the
    resolved ``chars_per_token`` ratio. See ``token_estimate.py`` for the
    estimator used at gate time."""
    chars_per_token: float | None = Field(default=None, gt=0.0)
    """Per-server override for the chars-per-token ratio. Falls back to
    ``ProxyConfig.chars_per_token`` (default 3.5, English-biased). Set to
    ~2.0 for Korean-dominant content, ~1.3 for Chinese-dominant."""
    retention_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    """Per-server retention floor override (see ToolOverrideConfig)."""
    llm: LLMCompressorConfig | None = None
    selective: SelectiveConfig | None = None
    hybrid: HybridConfig | None = None
    progressive: ProgressiveConfig | None = None
    cleaning: CleaningConfig | None = None
    tool_overrides: dict[str, ToolOverrideConfig] = {}
    auto_index: bool | None = None
    extraction: bool | None = None
    expose_in_profiles: list[ExposureProfile] | None = None
    """Exposure profiles in which this upstream's tools are advertised
    (#465). ``None`` (default) means every profile. Per-tool
    ``expose_in_profiles`` overrides this when set. See
    ``ToolOverrideConfig.expose_in_profiles``."""
    surfacing_enabled: bool = True
    """Opt this upstream's proxied tool responses in/out of the SURFACE stage
    (proactive memory surfacing). Default ``True`` preserves existing behavior;
    ``False`` suppresses surfacing for every tool on this server.

    Useful for third-party upstreams whose calls never match the user's LTM
    (so the per-call LTM search is pure wasted latency), or to keep a sensitive
    upstream's request context out of LTM queries entirely.

    Enforced in ``ProxyManager``, which reads this from ``stm_proxy.json`` via
    the hot-reloaded config — *not* in the ``SurfacingEngine`` relevance gate,
    which is built once at startup from the top-level ``SurfacingConfig`` and
    never sees per-upstream config. For tool-grained or glob scope instead, see
    ``SurfacingConfig.exclude_tools`` (matches ``server__tool``)."""
    max_retries: int = Field(default=3, ge=0)
    reconnect_delay_seconds: float = Field(default=1.0, ge=0.0)
    max_reconnect_delay_seconds: float = Field(default=30.0, ge=0.0)
    connect_timeout_seconds: float = Field(default=30.0, gt=0.0)
    call_timeout_seconds: float = Field(default=90.0, gt=0.0)
    """Per-attempt timeout for ``session.call_tool()`` against this upstream.

    Without this bound, a silently-hung upstream blocks the proxy indefinitely
    and every downstream client blocks on the proxy. On ``TimeoutError`` the
    session is force-reset (so the orphaned ``request_id`` cannot pollute a
    future call) and the retry loop proceeds to the next attempt, capped by
    ``max_retries`` and ``overall_deadline_seconds``.

    Default 90s: most tool calls complete in <30s, LLM-backed tools can take
    30-60s, 90s leaves headroom without permitting an infinite hang. Lower for
    known-fast upstreams; raise for upstreams that invoke long-running LLMs.
    """
    overall_deadline_seconds: float = Field(default=180.0, gt=0.0)
    """Total wall-clock budget for a single tool call across all retry attempts.

    Each attempt's effective timeout is ``min(call_timeout_seconds,
    remaining_deadline)``. When the deadline is exhausted the retry loop aborts
    and ``TimeoutError`` propagates. This prevents ``call_timeout_seconds ×
    (max_retries+1)`` worst-case blowout while still allowing multiple attempts
    within a bounded window. Default 180s = 2× ``call_timeout_seconds``.
    """
    max_description_chars: int = Field(default=200, gt=0)
    strip_schema_descriptions: bool = False
    origin: UpstreamOrigin | None = None
    """Import provenance (#475) — see :class:`UpstreamOrigin`. CLI-owned
    metadata: the server validates the shape but never reads it at runtime.
    Older binaries that predate the field ignore it via pydantic's default
    ``extra="ignore"``; the CLI's raw-dict load/save preserves it through
    every config mutation."""

    @model_validator(mode="after")
    def _check_ordering(self) -> Self:
        if self.reconnect_delay_seconds > self.max_reconnect_delay_seconds:
            raise ValueError(
                f"reconnect_delay_seconds ({self.reconnect_delay_seconds}) "
                f"must be <= max_reconnect_delay_seconds ({self.max_reconnect_delay_seconds})"
            )
        if self.call_timeout_seconds > self.overall_deadline_seconds:
            raise ValueError(
                f"call_timeout_seconds ({self.call_timeout_seconds}) "
                f"must be <= overall_deadline_seconds ({self.overall_deadline_seconds})"
            )
        return self


class CacheConfig(BaseModel):
    enabled: bool = True
    db_path: Path = Path("~/.memtomem/proxy_cache.db")
    default_ttl_seconds: float | None = Field(default=3600.0, ge=0.0)
    max_entries: int = Field(default=10000, gt=0)


class MetricsConfig(BaseModel):
    enabled: bool = True
    db_path: Path = Path("~/.memtomem/proxy_metrics.db")
    max_history: int = Field(default=10000, gt=0)


class CompressionFeedbackConfig(BaseModel):
    """Configuration for the stm_compression_feedback learning loop.

    Collection-only in this release: reports are persisted for later
    inspection via ``stm_compression_stats`` and for future auto-tuning.
    Shares the user-wide ``~/.memtomem/stm_feedback.db`` file with
    surfacing feedback (different tables; WAL mode makes concurrent
    access safe).
    """

    enabled: bool = True
    db_path: Path = Path("~/.memtomem/stm_feedback.db")


class ProgressiveReadsConfig(BaseModel):
    """Configuration for progressive-delivery read telemetry.

    Records one row per initial progressive response plus one row per
    ``stm_proxy_read_more`` follow-up into ``progressive_reads``.
    Aggregates surface via ``stm_progressive_stats`` and enable
    stratified analysis of follow-up rate by tool / compression
    strategy / response size. Shares the user-wide
    ``~/.memtomem/stm_feedback.db`` file with surfacing and
    compression feedback (disjoint tables; WAL mode makes concurrent
    access safe).
    """

    enabled: bool = True
    db_path: Path = Path("~/.memtomem/stm_feedback.db")


class SelectionTelemetryConfig(BaseModel):
    """Configuration for tool-selection telemetry (#467).

    When enabled, the proxy appends one ``selection`` + one ``execution``
    JSONL record per proxied call to ``path`` (schema and redaction policy
    in ``proxy/selection_log.py``). Off by default: it is a new disk write
    path, so the operator opts in explicitly. The flag is read at startup
    (lifespan wiring, like ``metrics.enabled``) — toggling it requires a
    restart, not a hot-reload.
    """

    enabled: bool = False
    path: Path = Path("~/.memtomem/stm_selection_log.jsonl")
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    """Fraction of calls recorded; applies to the selection+execution pair
    atomically so the log never contains orphan halves."""
    max_bytes: int = Field(default=50_000_000, gt=0)
    """Rotate the log when it reaches this size."""
    max_backups: int = Field(default=3, ge=0)
    """Rotated files kept (``.1`` … ``.N``); ``0`` truncates instead."""


class ToolRelevanceConfig(BaseModel):
    """Configuration for per-call tool-relevance ranking (#466 v0).

    Deterministic BM25 ranking of the advertised tool set against the
    call's query signal, recorded ONLY into selection telemetry
    (``candidate_features``) — exposure never changes. Inert unless
    ``selection_telemetry.enabled`` is also on (there is nowhere else for
    the ranking to go in v0), so the default-on here adds no write path
    by itself. Read per call via the hot-reloaded proxy config.
    """

    enabled: bool = True
    top_n: int = Field(default=20, gt=0)
    """Ranked candidates recorded per selection event (full advertised
    set is already in ``candidate_tools``; this bounds the scored list)."""


class ExposureConfig(BaseModel):
    """Configuration for the STM-native tool-exposure hard filter (#465).

    The filter runs at advertisement time (``ProxyManager.get_proxy_tools``)
    — the proxy's tool-exposure choke point — and decides which upstream
    tools the client model gets to see. Rejected tools are not registered;
    their reject reasons land in selection telemetry (#467,
    ``reject_reasons``) when it is enabled. Relevance ranking (#466) runs
    over the filter's *output*, so a hard-rejected tool can never be
    resurrected by ranking.

    Health signals are evaluated once at proxy startup from the persisted
    metrics store (``proxy_metrics.db``), so the advertised set is stable
    for the lifetime of the session — MCP clients are not guaranteed to
    re-list tools, and a mid-session change would make telemetry lie about
    what the client saw. A tool hidden for health is re-evaluated at the
    next startup: once its failures age out of ``health_window_hours`` it
    is advertised again (startup-grained half-open probing).
    """

    profile: ExposureProfile = ExposureProfile.STRICT
    health_window_hours: float = Field(default=24.0, gt=0.0)
    """Look-back window over ``proxy_metrics.db`` for per-tool health."""
    health_min_calls: int = Field(default=5, gt=0)
    """Minimum calls inside the window before health is judged at all —
    below this the tool is presumed healthy (insufficient evidence)."""
    health_error_rate_threshold: float = Field(default=0.95, gt=0.0, le=1.0)
    """Upstream-attributable error rate (transport / timeout / protocol /
    upstream_error — proxy-internal pipeline errors do not count against
    the tool) at or above which a tool is flagged unhealthy. The default
    is deliberately conservative: only consistently failing tools
    (≥95% of recent calls) are flagged."""
    review_risk_penalty: float = Field(default=0.5, ge=0.0, le=1.0)
    """Multiplicative demotion recorded for signal-flagged tools under the
    ``review`` profile: ``final_score = relevance_score * (1 - penalty)``
    in tool-relevance telemetry (#466). Exposure itself never changes in
    ``review``."""


# Static context window sizes (tokens) for known model families.
# Used by ProxyConfig.effective_max_result_chars() to scale compression budget.
# Prefix-matched: "claude-sonnet-4-20250514" matches "claude-sonnet-4".
# Ordered longest-prefix-first where ambiguity exists.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic — Claude 4.x / 4.5 / 4.6
    "claude-opus-4": 200000,
    "claude-sonnet-4": 200000,
    "claude-haiku-4": 200000,
    # OpenAI — GPT-4.1 / o-series / GPT-4o
    "gpt-4.1-mini": 1048576,
    "gpt-4.1-nano": 1048576,
    "gpt-4.1": 1048576,
    "gpt-4o-mini": 128000,
    "gpt-4o": 128000,
    "o4-mini": 200000,
    "o3-pro": 200000,
    "o3-mini": 200000,
    "o3": 200000,
    "o1-pro": 200000,
    "o1-mini": 128000,
    "o1": 200000,
    # Google — Gemini 2.x
    "gemini-2.5-pro": 1048576,
    "gemini-2.5-flash": 1048576,
    "gemini-2.0-flash": 1048576,
    "gemini-2": 1048576,
    # Meta — Llama 4
    "llama-4-maverick": 1048576,
    "llama-4-scout": 512000,
    "llama-4": 512000,
    # Open-weight
    "qwen-3": 131072,
    "qwen3": 131072,
    "deepseek-r1": 131072,
    "deepseek-v3": 131072,
    "mistral-large": 131072,
    "codestral": 262144,
    "command-a": 262144,
}


_EMBEDDING_PROVIDER_DEFAULTS: dict[str, str] = {
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com",
}


class RelevanceScorerConfig(BaseModel):
    """Configuration for query-aware relevance scoring.

    When ``embedding_provider`` is ``"openai"``, the ``OPENAI_API_KEY``
    environment variable must be set for authentication.
    """

    scorer: str = "bm25"
    """Scorer type: "bm25" (default, zero-latency) or "embedding" (semantic)."""
    embedding_provider: str = "ollama"
    """Embedding provider: "ollama" or "openai". Only used when scorer="embedding"."""
    embedding_model: str = "nomic-embed-text"
    """Embedding model name. Only used when scorer="embedding"."""
    embedding_base_url: str | None = None
    """Embedding API base URL. Defaults to the provider's standard endpoint
    (Ollama → http://localhost:11434, OpenAI → https://api.openai.com).
    Only used when scorer="embedding"."""
    embedding_timeout: float = Field(default=10.0, gt=0.0)
    """Embedding API timeout in seconds."""

    @model_validator(mode="after")
    def _apply_provider_default_url(self) -> "RelevanceScorerConfig":
        if self.embedding_base_url is None:
            self.embedding_base_url = _EMBEDDING_PROVIDER_DEFAULTS.get(
                self.embedding_provider, "http://localhost:11434"
            )
        return self


class ProxyConfig(BaseModel):
    enabled: bool = False
    config_path: Path = Path("~/.memtomem/stm_proxy.json")
    upstream_servers: dict[str, UpstreamServerConfig] = {}
    default_compression: CompressionStrategy = CompressionStrategy.AUTO
    default_max_result_chars: int = Field(default=16000, gt=0)
    max_upstream_chars: int = Field(default=10_000_000, gt=0)
    """Hard cap on the size of the upstream response loaded into memory before
    compression. A misbehaving (or malicious) upstream returning a 100 MB
    payload would otherwise OOM the proxy. When the cap is exceeded the
    response is truncated with a notice and the call is recorded as
    ``upstream_error`` / ``oversize`` in ``proxy_metrics.db``.

    Default 10 M chars (~10 MB UTF-8). Per-server / per-tool overrides are a
    follow-up if needed.
    """
    min_result_retention: float = Field(default=0.65, ge=0.0, le=1.0)
    relevance_scorer: RelevanceScorerConfig = Field(default_factory=RelevanceScorerConfig)
    """Minimum fraction of response to preserve after compression (0-1).

    If ``default_max_result_chars`` or per-tool ``max_result_chars`` would
    retain less than this fraction of the cleaned response, the effective
    budget is raised to ``len(response) * min_result_retention``.

    Default 0.65 ensures at least 65% of every response survives compression.
    Set to 0 to disable and use fixed budgets only.
    """
    max_description_chars: int = Field(default=200, gt=0)
    strip_schema_descriptions: bool = False
    # Bounded lock acquisition timeout (#208). Applies to internal state
    # locks in ``ProxyManager`` (selective compressor, LLM compressor,
    # extractor). A timeout here raises ``LockTimeoutError`` → recorded as
    # ``ErrorCategory.LOCK_TIMEOUT`` — distinct from upstream TIMEOUT (#206)
    # since lock hangs indicate an internal bug (deadlock / stuck holder),
    # not a slow dependency. Default 30s: anything longer is almost
    # certainly a bug in the lock-holding code.
    lock_timeout_seconds: float = Field(default=30.0, gt=0.0)
    consumer_model: str = ""
    context_budget_ratio: float = Field(default=0.05, ge=0.0, le=1.0)
    chars_per_token: float = Field(default=3.5, gt=0.0)
    """Default chars-per-token ratio used to convert token budgets into char
    budgets. The default ``3.5`` is English-biased (ASCII text averages
    ~4.0 chars/token for cl100k_base). Set to ~2.0 for Korean-dominant
    workloads, ~1.3 for Chinese-dominant. Per-server / per-tool overrides
    are available on ``UpstreamServerConfig`` and ``ToolOverrideConfig``.
    Also used inside ``effective_max_result_chars()`` to convert the
    consumer model's context window from tokens to chars."""
    cache: CacheConfig = Field(default_factory=CacheConfig)
    auto_index: AutoIndexConfig = Field(default_factory=AutoIndexConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    compression_feedback: CompressionFeedbackConfig = Field(
        default_factory=CompressionFeedbackConfig
    )
    progressive_reads: ProgressiveReadsConfig = Field(default_factory=ProgressiveReadsConfig)
    selection_telemetry: SelectionTelemetryConfig = Field(default_factory=SelectionTelemetryConfig)
    tool_relevance: ToolRelevanceConfig = Field(default_factory=ToolRelevanceConfig)
    exposure: ExposureConfig = Field(default_factory=ExposureConfig)

    @model_validator(mode="after")
    def _check_nonempty_upstream_prefixes(self) -> Self:
        # Empty / whitespace-only prefix produces composed names like
        # ``__list_items`` and skews ``tool_name_budget.composed_length``
        # (the prefix portion is zero), so the 64-char overflow guard
        # underestimates the real surface name a client sees. A single
        # empty prefix also slips past the uniqueness check below. Fail
        # at config load and name the upstream key so the user sees
        # which entry has the typo.
        empty = sorted(
            server_key
            for server_key, cfg in self.upstream_servers.items()
            if not cfg.prefix.strip()
        )
        if empty:
            raise ValueError(f"Empty upstream prefix in upstreams: {empty}")
        return self

    @model_validator(mode="after")
    def _check_unique_upstream_prefixes(self) -> Self:
        # Two upstreams sharing a prefix make composed names <prefix>__<tool>
        # collide. ProxyManager keeps a shared `seen_prefixed` set as
        # defense-in-depth and silently drops the second-loaded duplicate
        # with a logger.warning, so the user sees mysterious missing tools
        # instead of a config error. Surface the collision at load time.
        by_prefix: dict[str, list[str]] = {}
        for server_key, cfg in self.upstream_servers.items():
            by_prefix.setdefault(cfg.prefix, []).append(server_key)
        collisions = {prefix: sorted(keys) for prefix, keys in by_prefix.items() if len(keys) > 1}
        if collisions:
            details = "; ".join(
                f"prefix '{prefix}' used by upstreams: {keys}"
                for prefix, keys in sorted(collisions.items())
            )
            raise ValueError(f"Duplicate upstream prefixes detected: {details}")
        return self

    def effective_max_result_chars(self) -> int:
        """Compute max_result_chars scaled by consumer model's context window.

        If ``consumer_model`` is set and matches a known model prefix,
        the budget is ``context_window * context_budget_ratio * chars_per_token``
        (tokens → chars), capped at ``default_max_result_chars``. The
        ``chars_per_token`` field defaults to ``3.5`` (English-biased) and
        is configurable for non-Latin-script workloads.
        """
        if not self.consumer_model:
            return self.default_max_result_chars
        # Prefix match: "claude-sonnet-4-20250514" matches "claude-sonnet-4"
        ctx_tokens = None
        for prefix, tokens in MODEL_CONTEXT_WINDOWS.items():
            if self.consumer_model.startswith(prefix):
                ctx_tokens = tokens
                break
        if ctx_tokens is None:
            return self.default_max_result_chars
        model_budget = int(ctx_tokens * self.context_budget_ratio * self.chars_per_token)
        if model_budget <= 0:
            # context_budget_ratio is validated ge=0.0, so 0 is a legal value —
            # but a 0-char budget would flow into every per-server max_chars
            # and compress responses to nothing whenever min_result_retention
            # (itself disable-able with 0) doesn't rescue it. A degenerate
            # model budget means "model scaling effectively off", not "no
            # output": fall back to the static default, which is gt=0.
            return self.default_max_result_chars
        return min(model_budget, self.default_max_result_chars)

    @staticmethod
    def load_from_file(
        path: Path, env_overrides: dict[str, Any] | None = None, *, missing_ok: bool = True
    ) -> ProxyConfig | None:
        """Load config from *path*. Returns ``None`` on parse/validation error
        (with ``missing_ok=True``, distinct from file-not-found which returns
        a default ``ProxyConfig``).

        When *env_overrides* is supplied it is deep-merged on top of the file
        contents so env-set fields win over file-set fields, matching the
        ``env > file > defaults`` precedence documented in
        ``docs/configuration.md``.

        With ``missing_ok=False`` a missing file returns ``None`` instead of
        the env-only/defaults rebuild. Callers that already hold a better
        env-aware config than the raw-string overlay can produce — e.g.
        ``STMConfig()``'s pydantic-settings parse, which decodes JSON-encoded
        complex env values the overlay cannot — use this to decline the swap
        in a single atomic call, rather than a separate ``exists()``
        pre-check that races with file deletion. A file deleted between the
        existence check and the read also lands on ``None`` here, so every
        disappearance mode converges on "do not swap".
        """
        resolved = path.expanduser().resolve()
        if not resolved.exists():
            logger.debug("Proxy config file not found: %s", resolved)
            if not missing_ok:
                return None
            if env_overrides:
                try:
                    return ProxyConfig.model_validate(env_overrides)
                except Exception as exc:
                    logger.warning(
                        "Env-only proxy config failed validation: %s%s — using defaults",
                        exc,
                        _env_override_hint(exc, env_overrides),
                    )
            return ProxyConfig()
        # Warn if config is group/world-readable (may contain API keys)
        try:
            mode = resolved.stat().st_mode & 0o777
            if mode & 0o077:
                logger.warning(
                    "Proxy config %s has permissive mode %o — consider restricting to 0600",
                    resolved,
                    mode,
                )
        except OSError:
            pass
        file_data: dict[str, Any] | None = None
        try:
            loaded = json.loads(resolved.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                # Reject non-object roots BEFORE the env merge: ``[]`` would
                # otherwise slip through ``_deep_merge`` (``dict([])`` is
                # ``{}``) and an env override on top would validate cleanly,
                # silently accepting an invalid config file.
                raise ValueError(f"config root must be a JSON object, got {type(loaded).__name__}")
            file_data = loaded
            data = _deep_merge(file_data, env_overrides) if env_overrides else file_data
            return ProxyConfig.model_validate(data)
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning(
                "Failed to parse proxy config %s: %s%s",
                resolved,
                exc,
                _env_override_hint(exc, env_overrides, file_data),
            )
            return None


class ProxyConfigLoader:
    """mtime-based hot-reload for proxy config file.

    Env overrides captured at construction time are re-applied on every
    reload so ``MEMTOMEM_STM_PROXY__*`` settings continue to win over file
    contents after the agent edits ``stm_proxy.json`` at runtime.
    """

    def __init__(self, path: Path, env_overrides: dict[str, Any] | None = None) -> None:
        self._path = path.expanduser().resolve()
        self._cached: ProxyConfig | None = None
        self._mtime: float = 0.0
        self._env_overrides = env_overrides or {}

    def seed(self, config: ProxyConfig) -> None:
        self._cached = config
        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:
            self._mtime = -1.0

    def get(self) -> ProxyConfig:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            if self._cached is not None:
                return self._cached
            return (
                ProxyConfig.load_from_file(self._path, env_overrides=self._env_overrides)
                or ProxyConfig()
            )
        if mtime != self._mtime or self._cached is None:
            loaded = ProxyConfig.load_from_file(self._path, env_overrides=self._env_overrides)
            if loaded is not None:
                self._cached = loaded
                self._mtime = mtime
            else:
                # Don't advance _mtime on parse failure: the next get() must
                # retry instead of treating the broken file as up-to-date,
                # otherwise a fix that lands within filesystem mtime
                # granularity (or before any other write) would be ignored.
                logger.warning("Proxy config parse failed; keeping previous config")
        return self._cached  # type: ignore[return-value]
