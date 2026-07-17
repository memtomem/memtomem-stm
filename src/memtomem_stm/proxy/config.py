"""Proxy gateway configuration."""

from __future__ import annotations

import json
import logging
import os
import types
from collections.abc import Mapping
from urllib.parse import urlparse
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self, Union, get_args, get_origin

from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic_core import ErrorDetails

from memtomem_stm.proxy import prefixes

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


def _permissive_mode(resolved: Path) -> int | None:
    """Mode bits of *resolved* when it is group/world-accessible, else ``None``.

    Shared by the load path and ``mms config validate`` so the two warnings
    can't drift apart. ``None`` also covers a failed ``stat`` (best-effort).
    """
    try:
        mode = resolved.stat().st_mode & 0o777
    except OSError:
        return None
    return mode if mode & 0o077 else None


def _has_annotation_policy(data: dict[str, Any]) -> bool:
    """Whether a raw config dict explicitly sets ``cache.tool_annotation_policy``.

    Shared by the load path and ``mms config validate`` so the two
    missing-policy advisories can't drift apart. A non-dict ``cache`` value
    counts as unset — validation will reject it separately.
    """
    cache = data.get("cache")
    return isinstance(cache, dict) and "tool_annotation_policy" in cache


def _sanitized_load_error(exc: Exception) -> str:
    """Error summary safe to surface beyond the local process log.

    ``ConfigLoadResult.error`` flows to the MCP client via
    ``stm_proxy_health``, so it must not echo config *values*. Pydantic
    smuggles them in two ways: ``input_value=...`` (dropped via
    ``include_input=False``) and the rendered ``msg`` of a custom
    model-validator — e.g. the duplicate-prefix check embeds the prefix
    string, which is the secret itself if someone typos a token into a
    ``prefix`` field. So the summary uses ``loc`` + the machine-readable
    ``type`` code (``dict_type`` / ``value_error`` / ``missing`` …) only,
    never ``msg``. Full messages stay in the local stderr log and in
    ``mms config validate``, which reads the raw errors directly.

    Non-pydantic errors (``json.JSONDecodeError``, the non-object-root
    ``ValueError``) describe positions/types, not config values.
    """
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors(include_url=False, include_input=False):
            loc = ".".join(str(part) for part in err["loc"])
            parts.append(f"{loc} ({err['type']})" if loc else err["type"])
        summary = "; ".join(parts)
        return f"{exc.error_count()} validation error(s): {summary}"
    return str(exc)


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

    def _error_key(e: ErrorDetails) -> tuple[tuple[Any, ...], str, str]:
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


class TokenEstimationMode(StrEnum):
    """How token-equivalent response budgets are evaluated at gate time."""

    STATIC = "static"
    UNICODE = "unicode"


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


# Hosts whose traffic never leaves the machine — a scan-off LLM path pointed
# here does not cross a trust boundary, so the #610 startup warning stays
# silent for them.
_LOCAL_LLM_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


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

    def is_external_destination(self) -> bool:
        """True when this LLM path sends text off the machine.

        OpenAI / Anthropic are always external. Ollama is treated as local only
        when its ``base_url`` host is loopback (the common self-hosted case);
        an Ollama endpoint on a remote host still crosses the boundary. Used by
        the #610 startup warning to avoid false alarms on local Ollama.
        """
        if self.provider in (LLMProvider.OPENAI, LLMProvider.ANTHROPIC):
            return True
        host = urlparse(self.base_url).hostname or ""
        return host not in _LOCAL_LLM_HOSTS

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
    token_estimation_mode: TokenEstimationMode | None = None
    """Per-tool gate mode. ``unicode`` measures the actual response; ``None``
    inherits the upstream or proxy setting."""
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
    cache: bool | None = None
    """Per-tool response-cache opt-in/out. ``None`` (default) defers to the
    server-level ``cache``, then to the global
    ``CacheConfig.tool_annotation_policy``. ``True`` force-caches this tool
    (overriding the annotation policy — e.g. to re-enable caching for a tool an
    upstream mis-annotates as a writer); ``False`` never caches it (e.g. a
    volatile read tool, or a writer on an upstream that omits annotations).
    Under the ``strict`` policy — which new configs set explicitly — ``True``
    is the supported allowlist for a known-read-only tool whose upstream omits
    annotations. The privacy / transient-key store guards still apply when
    ``True``."""
    cache_ttl_seconds: float | None = Field(default=None, ge=0.0)
    """Per-tool override for the response-cache TTL (seconds). ``None`` (default)
    defers to the server-level ``cache_ttl_seconds``, then to the global
    ``CacheConfig.default_ttl_seconds``. A positive value caches this tool's
    responses for that many seconds; ``0`` disables caching for this tool: while
    the resolved TTL is ``<= 0`` the lookup is bypassed, so a stale row is never
    served (mirroring the global ``default_ttl_seconds <= 0`` behavior); any
    existing on-disk row is cleaned up opportunistically (invalidated when an
    identical call next stores a text response) and otherwise expires under its
    original frozen TTL. Independent of the ``cache`` on/off
    gate: ``cache: false`` always wins (never cached); ``cache: true`` with
    ``cache_ttl_seconds: 0`` is eligible but TTL-disabled, i.e. effectively off.
    Unlike the global field, ``None`` here means *inherit*, not *never expires*."""
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
    cwd: Path | None = None
    """Working directory for stdio servers. Useful for project-scoped MCP
    servers and avoids shell-specific ``cd`` wrappers on Windows."""
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
    token_estimation_mode: TokenEstimationMode | None = None
    """Per-server token gate mode. ``None`` inherits the proxy default."""
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
    cache: bool | None = None
    """Per-server response-cache opt-in/out (see ``ToolOverrideConfig.cache``).
    ``None`` (default) defers to the global ``CacheConfig.tool_annotation_policy``;
    ``True``/``False`` force every tool on this upstream in/out of the cache —
    ``True`` is the server-wide strict-mode allowlist for a trusted read-only
    upstream that omits annotations. A per-tool ``cache`` override wins over
    this."""
    cache_ttl_seconds: float | None = Field(default=None, ge=0.0)
    """Per-server response-cache TTL override (see
    ``ToolOverrideConfig.cache_ttl_seconds``). ``None`` (default) defers to the
    global ``CacheConfig.default_ttl_seconds``; a positive value sets the TTL for
    every tool on this upstream; ``0`` disables caching for the whole upstream. A
    per-tool ``cache_ttl_seconds`` wins over this."""
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
    """End-to-end budget for establishing a session with this upstream.

    One shared monotonic deadline covers transport entry (process spawn or
    HTTP/SSE connect), MCP ``initialize()``, and the ``tools/list`` discovery
    call — each phase gets whatever budget remains, so a slow phase cannot
    grant later phases a fresh window. Applied identically at first connect
    and at every reconnect.

    For network transports the same value is also passed as the SDK client
    factory's ``timeout=`` (the httpx connect budget); ``sse_read_timeout``
    stays at the SDK default so long-lived streams don't inherit the connect
    budget. Contrast with ``call_timeout_seconds`` (per tool-call attempt)
    and ``overall_deadline_seconds`` (per tool call across retries).
    """
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
    circuit_max_failures: int = Field(default=3, ge=0)
    """Consecutive failed calls before this upstream's circuit breaker opens.

    Counts one failure per *call* that exhausts its retry/deadline budget on a
    transport fault or timeout — not one per attempt, and not tool-level
    ``isError`` results (an erroring tool proves the upstream is alive). While
    open, calls fast-fail with a ``circuit_open`` error instead of paying the
    full retry/deadline cost; cached responses keep serving. ``0`` disables
    the breaker for this upstream. Connect-time snapshot like ``max_retries``:
    edits apply on the next restart, not via config hot-reload.
    """
    circuit_reset_seconds: float = Field(default=60.0, gt=0.0)
    """Seconds an open circuit breaker waits before allowing a probe call."""
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
    tool_annotation_policy: Literal["conservative", "strict", "ignore"] = "conservative"
    """Which proxied tool responses are eligible for the response cache, based on
    the upstream tool's MCP annotations (``readOnlyHint`` / ``destructiveHint``).

    The proxy sits transparently in front of every upstream tool, so without a
    gate a mutating tool (``create_*`` / ``send_*`` / ``write_*`` / ``delete_*``)
    called twice with identical args within the TTL is served the first call's
    cached success WITHOUT re-executing the side effect — the agent is told it
    mutated when it did not.

    - ``conservative`` (default): cache every tool EXCEPT those that explicitly
      self-declare as writers (``readOnlyHint is False`` or
      ``destructiveHint is True``). Keeps caching for the un-annotated majority
      and for declared read-only tools, while refusing to memoize a side effect
      the upstream itself flags as mutating. A tool declaring BOTH
      ``readOnlyHint=True`` and ``destructiveHint=True`` is contradictory and
      is deliberately treated as a writer (not the spec-literal reading, which
      scopes ``destructiveHint`` to ``readOnlyHint=false`` tools) — distrusting
      a self-contradiction costs one cache slot; trusting it could replay a
      side effect. Re-enable such a tool via a per-tool ``cache: true``.
    - ``strict``: cache ONLY tools that explicitly declare ``readOnlyHint=True``.
      Safest (the MCP spec treats a missing ``readOnlyHint`` as may-mutate), but
      drops caching for every upstream that omits annotations.
    - ``ignore``: pre-gate behavior — cache every tool regardless of annotations.

    The ``conservative`` default is a compatibility choice for files that
    predate the knob: NEW config files (``mms init`` / ``mms add`` /
    ``mms add --from-clients``) are written with an explicit ``"strict"``, and
    loading a file without the key logs a migration advisory.

    A per-tool / per-server ``cache`` override (``ToolOverrideConfig.cache`` /
    ``UpstreamServerConfig.cache``) takes precedence over this policy — under
    ``strict`` that override is the allowlist for un-annotated read-only tools.
    The privacy and transient-key store guards always apply on top, regardless
    of this knob."""


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
    retention_days: int = Field(default=90, ge=0)
    """#584 — days to keep ``compression_feedback`` rows before a startup
    purge deletes them. The table is otherwise append-only and unbounded.
    ``0`` disables the purge (rows kept indefinitely — the pre-#584
    behavior)."""


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
    retention_days: int = Field(default=90, ge=0)
    """#584 — days to keep ``progressive_reads`` rows before a startup purge
    deletes them. The table is append-only by design and otherwise
    unbounded. ``0`` disables the purge (rows kept indefinitely — the
    pre-#584 behavior)."""


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


class ToolgraphConfig(BaseModel):
    """Optional external tool-graph eligibility provider (#465).

    Consults a separate, non-proxied tool-graph MCP server for cross-server
    authorization / data-flow eligibility facts and feeds them into the
    STM-native exposure filter as an additional rule source (alongside the
    config / structural / native-signal rules already in
    ``tool_eligibility.filter_tools``). The graph is *consulted*, never
    proxied: the client never sees its tools, and STM holds **no
    Python-level dependency** on the external package — all traffic goes
    over the MCP protocol via :class:`~memtomem_stm.proxy.toolgraph_provider.ToolgraphConsultAdapter`
    (stdio transport), mirroring the surfacing LTM-consult pattern.

    Default-off: when ``enabled`` is ``True`` the consult runs once at
    proxy startup so the advertised tool set stays session-stable, exactly
    like the health-flag precompute. The verdict feeds
    ``tool_eligibility.filter_tools`` via per-candidate ``toolgraph_*`` reject
    codes (profile-gated, like the native signal rules) or a whole-call
    fail-closed withhold, and pins ``graph_generation`` into selection
    telemetry. Failures map onto the four ``on_*`` knobs below. Neo4j (behind
    the graph server) is an operational prerequisite of enabling this block:
    a backend outage while the graph *server* stays up surfaces as a
    server-side error and is classified as a contract failure
    (``on_protocol_error``).
    """

    enabled: bool = False
    source: Literal["stdio", "bundle"] = "stdio"
    """Policy source. ``stdio`` preserves the one-shot MCP consult; ``bundle``
    consumes a portable, atomically published Toolgraph policy artifact and
    never launches a Toolgraph subprocess."""
    bundle_path: Path = Path("~/.memtomem/toolgraph/policy-bundle.json")
    """Portable policy artifact used when ``source`` is ``bundle``."""
    command: str = "toolgraph"
    """Launch command for the stdio tool-graph MCP server. Defaults to the
    graph server's registered console script (mirroring the surfacing
    ``memtomem-server`` default); ``serve`` with no flag runs stdio
    (``serve --http`` is out of scope for v1 — stdio transport only)."""
    args: list[str] = ["serve"]
    env: dict[str, str] | None = None
    """Extra environment for the launched server (e.g. ``NEO4J_URI`` /
    ``NEO4J_USER`` / ``NEO4J_PASSWORD``). ``None`` inherits only mcp's safe
    default-environment allowlist (PATH / HOME / SHELL / TERM / USER /
    LOGNAME); set ``NEO4J_*`` etc. explicitly here — they are merged *over*
    that default and are not picked up from the proxy's own environment."""
    agent_id: str = "stm-proxy"
    """Identity the graph authorizes eligibility against; must be registered
    in the graph. A typo returns ``agent_found=false`` — see
    ``on_agent_not_found``."""
    server_name_map: dict[str, str] = {}
    """Maps an STM upstream connection key (the operator-chosen key in
    ``upstream_servers``) to the tool-graph server's *crawled* name. The two
    are independent strings, so they coincide only by luck; an empty map
    assumes identity and relies on a heuristic mismatch warning."""
    query_profile: str = "strict"
    """Profile passed to the upstream ``eligible_tools`` consult. Kept a free
    string (not coupled to STM's own ``ExposureProfile``) because the graph's
    profile ladder is the external package's concern; STM applies its own
    profile semantics on top."""
    on_unreachable: Literal["open", "closed"] = "open"
    """Transport down / timeout. ``open`` (default) skips the external rule
    family and advertises per STM-native rules (the graph is an enhancement,
    not a hard dependency); ``closed`` withholds every tool the graph did not
    bless (high-assurance)."""
    on_agent_not_found: Literal["fail_start", "open", "closed"] = "fail_start"
    """Graph reachable but ``agent_id`` unknown — almost always a config
    typo. ``fail_start`` (default) fails startup loudly so a typo cannot
    silently disable enforcement; ``open`` / ``closed`` are explicit opt-ins."""
    on_protocol_error: Literal["fail_start", "open", "closed"] = "fail_start"
    """Graph reachable but incompatible (missing ``eligible_tools``, malformed
    ``structuredContent``, non-int ``graph_generation``, unknown-profile
    error). ``fail_start`` (default) treats a contract break as a loud
    startup failure rather than a silent passthrough."""
    on_tool_not_found: Literal["open", "closed"] = "open"
    """A specific candidate was never crawled (the graph's blind spot).
    ``open`` (default) does not hide a working tool; ``closed`` rejects
    uncrawled candidates (high-assurance)."""
    risk_penalty_scale: float = Field(default=1.0, ge=0.0)
    """Multiplier mapping the graph's per-candidate ``risk_score`` (the
    rule-based data-flow/DENY risk, ``[0,1]``) to a relevance ``risk_penalty``
    for eligible-but-risky tools (#493): ``penalty = min(risk_score * scale,
    1.0)``, demoting them in tool-relevance ranking telemetry (#466) in EVERY
    profile — never exposure (ranking can neither resurrect nor hard-reject).
    When ``> 0`` the consult runs a second, best-effort ``rank_features`` batch
    query in the same startup session; a failure there degrades to no penalties
    (logged, never a startup gate). The penalty composes with the native
    ``review_risk_penalty`` via a complement-product when both apply. ``0``
    (or a disabled ``toolgraph`` block) skips the enrichment entirely."""
    timeout_seconds: float = Field(default=5.0, gt=0.0)
    """Per-consult timeout for the startup batch query."""
    consult_cache_enabled: bool = True
    """Disk-cache a successful consult's verdict keyed by ``graph_generation``
    (#494). On restart, a cheap generation probe is still made (so a degraded
    graph is never masked); only the expensive per-candidate ``eligible_tools`` /
    ``rank_features`` evaluation is skipped when the generation, candidate set,
    agent, profile, and backend all match a cached row."""
    consult_cache_path: Path = Path("~/.memtomem/toolgraph_consult.db")
    """SQLite path for the consult cache (#494). One DB serves all graph
    backends, disambiguated by a provider fingerprint over ``command`` / ``args``
    / env *keys*; point distinct backends sharing identical command/args/env-keys
    but different env *values* at distinct paths."""
    consult_cache_max_scopes: int = Field(default=64, gt=0)
    """Maximum number of cached consult rows kept in the #494 disk cache before
    the oldest (by ``created_at``) are trimmed on each write. Bounds growth of
    the user-wide ``toolgraph_consult.db``; one row per ``(provider, agent,
    profile, candidate-set, generation)`` scope, so this caps total rows across
    all scopes, not per scope. Must be ``>= 1`` (``0`` would trim every row on
    every write, defeating the cache)."""


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


def _model_arms(annotation: Any) -> list[type[BaseModel]] | None:
    """BaseModel arms of *annotation* after unwrapping ``Annotated``/unions.

    Returns ``None`` when descending would risk false positives: a non-model
    leaf, a mixed union (model | free-form), or anything else where key
    existence isn't defined by a model schema. The classification is read off
    the annotation itself — no name allowlist — so it stays correct as the
    config models evolve.
    """
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        arms: list[type[BaseModel]] = []
        for arm in get_args(annotation):
            if arm is type(None):
                continue
            sub = _model_arms(arm)
            if sub is None:  # mixed union — don't guess, don't descend
                return None
            arms.extend(sub)
        return arms or None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return None


def _container_value_arms(annotation: Any) -> tuple[str, list[type[BaseModel]]] | None:
    """Classify a container annotation whose *values* are models.

    Returns ``("dict", arms)`` for ``dict[str, Model]`` (user-defined keys —
    descend into values only) or ``("list", arms)`` for ``list[Model]``;
    ``None`` for free-form containers (``dict[str, str]``, ``dict[str, Any]``)
    and everything else.
    """
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    origin = get_origin(annotation)
    if origin is dict:
        args = get_args(annotation)
        if len(args) == 2 and (arms := _model_arms(args[1])) is not None:
            return ("dict", arms)
    elif origin in (list, tuple, set):
        args = get_args(annotation)
        if args and (arms := _model_arms(args[0])) is not None:
            return ("list", arms)
    return None


def _unknown_keys_via_arms(
    arms: list[type[BaseModel]], data: Mapping[str, Any], prefix: str
) -> list[str]:
    """Unknown keys under a (possibly multi-arm) model annotation.

    With several model arms (none in today's tree), only keys unknown to
    *every* arm are flagged — conservative, no false positives.
    """
    per_arm = [find_unknown_keys(arm, data, prefix) for arm in arms]
    common = set(per_arm[0])
    for other in per_arm[1:]:
        common &= set(other)
    return sorted(common)


def find_unknown_keys(
    model_cls: type[BaseModel], data: Mapping[str, Any], prefix: str = ""
) -> list[str]:
    """Dotted paths in *data* that no field of *model_cls* (recursively) accepts.

    The proxy config models deliberately keep pydantic's default
    ``extra="ignore"`` for forward compatibility (older binaries must ignore
    fields written by newer CLIs — see ``UpstreamServerConfig.origin``), which
    means a typo'd key is silently dropped at load time. This walker gives the
    load path and ``mms config validate`` a way to *name* those dropped keys
    without giving up the lenient validation.

    Key-existence only: a value of the wrong runtime type (a string where a
    model object belongs) is skipped silently — ``model_validate`` owns type
    errors. ``dict[str, Model]`` fields (``upstream_servers``,
    ``tool_overrides``) have user-defined keys, so only their values are
    descended; free-form leaves (``env``, ``headers``, ``origin.original``,
    ``server_name_map``) are never descended.
    """
    unknown: list[str] = []
    known: dict[str, Any] = {}
    for name, field in model_cls.model_fields.items():
        known[name] = field.annotation
        if field.alias:
            known[field.alias] = field.annotation
    for key, value in data.items():
        path = f"{prefix}{key}"
        if key not in known:
            unknown.append(path)
            continue
        annotation = known[key]
        if (arms := _model_arms(annotation)) is not None:
            if isinstance(value, Mapping):
                unknown.extend(_unknown_keys_via_arms(arms, value, f"{path}."))
        elif (container := _container_value_arms(annotation)) is not None:
            kind, arms = container
            if kind == "dict" and isinstance(value, Mapping):
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, Mapping):
                        unknown.extend(
                            _unknown_keys_via_arms(arms, sub_value, f"{path}.{sub_key}.")
                        )
            elif kind == "list" and isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, Mapping):
                        unknown.extend(_unknown_keys_via_arms(arms, item, f"{path}[{i}]."))
    return sorted(unknown)


@dataclass(frozen=True)
class ConfigLoadResult:
    """Outcome of ``ProxyConfig.load_from_file_with_status``.

    ``error`` is set iff the file exists but failed to parse or validate —
    the case a running server silently papers over by falling back to
    env/default config. A missing file is not an error (``config`` may still
    carry the env-only/defaults rebuild under ``missing_ok=True``).
    """

    config: ProxyConfig | None
    error: str | None
    unknown_keys: tuple[str, ...] = ()


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
    advertise_context_query: bool = False
    """Advertise the proxy-only ``_context_query`` string in every upstream
    tool schema. Opt-in preserves existing catalogs; the argument is stripped
    before forwarding and only guides query-aware compression and surfacing."""
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
    token_estimation_mode: TokenEstimationMode = TokenEstimationMode.STATIC
    """``static`` preserves chars-per-token conversion. ``unicode`` uses the
    runtime codepoint estimator and remains opt-in in 0.1.x."""
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
    toolgraph: ToolgraphConfig = Field(default_factory=ToolgraphConfig)

    @model_validator(mode="after")
    def _check_nonempty_upstream_prefixes(self) -> Self:
        # Empty / whitespace-only prefix produces composed names like
        # ``__list_items`` and skews ``tool_name_budget.composed_length``
        # (the prefix portion is zero), so the 64-char overflow guard
        # underestimates the real surface name a client sees. A single
        # empty prefix also slips past the uniqueness check below. Fail
        # at config load and name the upstream key so the user sees
        # which entry has the typo.
        empty = prefixes.empty_prefix_keys(
            {server_key: cfg.prefix for server_key, cfg in self.upstream_servers.items()}
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
        # The detection + wording live in ``proxy/prefixes.py``, shared with
        # the CLI's pre-save check so both sides can't diverge.
        collisions = prefixes.prefix_collisions(
            {server_key: cfg.prefix for server_key, cfg in self.upstream_servers.items()}
        )
        if collisions:
            raise ValueError(prefixes.format_collision_error(collisions))
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
        path: Path,
        env_overrides: dict[str, Any] | None = None,
        *,
        missing_ok: bool = True,
        log_warnings: bool = True,
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

        Callers that need to distinguish "missing" from "present but broken"
        use ``load_from_file_with_status`` instead.
        """
        return ProxyConfig.load_from_file_with_status(
            path, env_overrides, missing_ok=missing_ok, log_warnings=log_warnings
        ).config

    @staticmethod
    def load_from_file_with_status(
        path: Path,
        env_overrides: dict[str, Any] | None = None,
        *,
        missing_ok: bool = True,
        log_warnings: bool = True,
    ) -> ConfigLoadResult:
        """``load_from_file`` with the failure mode preserved in the result.

        Same loading semantics and logging; additionally reports (a) an
        ``error`` string when the file exists but fails to parse/validate —
        so the server can surface "running defaults because the file is
        broken" in health output instead of only a stderr line — and (b) the
        ``unknown_keys`` the lenient validation silently dropped, walked over
        the raw file dict *before* the env merge so an env-injected key can
        never be misattributed as a file typo.

        ``error`` is sanitized (location + message, never ``input_value``):
        it flows to the MCP client via ``stm_proxy_health``, and a mistyped
        secret-bearing field would otherwise embed the secret itself.

        ``log_warnings=False`` suppresses the advisory warnings (permissive
        mode, unknown keys, missing ``cache.tool_annotation_policy``) for
        re-loads of a file some earlier load already warned about — e.g. ``ProxyManager.start()``'s empty-upstreams
        fallback, which would otherwise duplicate them at startup. Parse
        *failures* are always logged: a silent ``None`` is the dark-failure
        mode this module exists to prevent.
        """
        resolved = path.expanduser().resolve()
        if not resolved.exists():
            logger.debug("Proxy config file not found: %s", resolved)
            if not missing_ok:
                return ConfigLoadResult(config=None, error=None)
            if env_overrides:
                try:
                    return ConfigLoadResult(
                        config=ProxyConfig.model_validate(env_overrides), error=None
                    )
                except Exception as exc:
                    logger.warning(
                        "Env-only proxy config failed validation: %s%s — using defaults",
                        exc,
                        _env_override_hint(exc, env_overrides),
                    )
            return ConfigLoadResult(config=ProxyConfig(), error=None)
        # Warn if config is group/world-readable (may contain API keys)
        mode = _permissive_mode(resolved)
        if mode is not None and log_warnings:
            logger.warning(
                "Proxy config %s has permissive mode %o — consider restricting to 0600",
                resolved,
                mode,
            )
        file_data: dict[str, Any] | None = None
        unknown_keys: tuple[str, ...] = ()
        try:
            loaded = json.loads(resolved.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                # Reject non-object roots BEFORE the env merge: ``[]`` would
                # otherwise slip through ``_deep_merge`` (``dict([])`` is
                # ``{}``) and an env override on top would validate cleanly,
                # silently accepting an invalid config file.
                raise ValueError(f"config root must be a JSON object, got {type(loaded).__name__}")
            file_data = loaded
            unknown_keys = tuple(find_unknown_keys(ProxyConfig, file_data))
            data = _deep_merge(file_data, env_overrides) if env_overrides else file_data
            config = ProxyConfig.model_validate(data)
            if unknown_keys and log_warnings:
                # One aggregated line, not one per key: the hot-reload loader
                # re-runs this on every mtime change.
                logger.warning(
                    "Proxy config %s has %d unknown key(s) (ignored — possible typo): %s",
                    resolved,
                    len(unknown_keys),
                    ", ".join(unknown_keys),
                )
            if log_warnings and config.cache.enabled and not _has_annotation_policy(data):
                # Migration advisory: new configs written by `mms init`/`mms add`
                # carry an explicit "strict", but a key-less legacy file keeps
                # the conservative Pydantic default. Checked against the MERGED
                # data (not the raw file) so an env-supplied policy — an
                # explicit operator choice — suppresses it. Skipped with the
                # cache disabled: the only other policy reader, the timeout-
                # retry gate, treats strict and conservative identically.
                logger.warning(
                    "Proxy config %s does not set cache.tool_annotation_policy — using the "
                    "'conservative' default. New configs are created with 'strict'; add "
                    '"cache": {"tool_annotation_policy": "strict"} (or "conservative" to '
                    "pin current behavior) to silence this.",
                    resolved,
                )
            return ConfigLoadResult(config=config, error=None, unknown_keys=unknown_keys)
        except (json.JSONDecodeError, Exception) as exc:
            # The parse-failure warning dominates; the unknown-keys warning is
            # suppressed here but the paths stay in the result for `mms
            # config validate` to report alongside the errors.
            logger.warning(
                "Failed to parse proxy config %s: %s%s",
                resolved,
                exc,
                _env_override_hint(exc, env_overrides, file_data),
            )
            return ConfigLoadResult(
                config=None, error=_sanitized_load_error(exc), unknown_keys=unknown_keys
            )


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
