"""STM (Short-Term Memory) root configuration."""

from __future__ import annotations

import ipaddress
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


class FormationConfig(BaseModel):
    """Review-first candidate submission to a capable memtomem core."""

    enabled: bool = False
    max_content_chars: int = Field(default=2000, ge=1, le=2000)


class HookCompressionConfig(BaseModel):
    """``mms hook`` built-in tool *output compression* settings (P1a — Bash).

    A gate **independent of surfacing**: compressing a built-in tool's output
    (via the PostToolUse ``updatedToolOutput`` field) replaces what the model
    reads, whereas surfacing only *appends* ``additionalContext``. Env keys are
    ``MEMTOMEM_STM_HOOK__COMPRESSION__<field>``.

    Scope is **Bash only** for now — compressing ``Read`` would break a later
    ``Edit`` whose ``old_string`` must match the file verbatim. The allowlist is
    intentionally hardcoded (not a Pydantic list field): pydantic-settings parses
    complex env values as JSON, so a comma-separated ``…__TOOLS=Bash,Grep`` would
    raise rather than split. The compression strategy is likewise fixed to
    ``TruncateCompressor`` (self-contained, no chunk-store callback)."""

    enabled: bool = False
    """Opt-in (default ``False``), mirroring ``proxy.enabled``. Because
    compression *replaces* model-visible output it ships dormant; enable with
    ``MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1`` after the empirical hook test
    confirms the host honors ``updatedToolOutput`` for Bash."""
    max_chars: int = Field(default=16000, gt=0)
    """Target character budget for the replacement ``stdout`` channel, and the
    threshold below which output is left untouched (only stdout longer than this
    is compressed). The sentinel prefix is reserved out of this budget; the
    compressor's own truncation suffix may still add a small, bounded overage, so
    treat it as a target rather than a hard ceiling. Matches the proxy's
    ``default_max_result_chars`` default."""
    min_retention: float = Field(default=0.65, ge=0.0, le=1.0)
    """Minimum fraction of the original stdout that a lossy native replacement
    may retain. Native PostToolUse hooks have no lossless continuation channel,
    so a result below this floor is passed through unchanged. Env:
    ``MEMTOMEM_STM_HOOK__COMPRESSION__MIN_RETENTION``."""


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
    daemon_timeout_seconds: float = Field(default=2.5, gt=0.0, allow_inf_nan=False)
    """Per-request wall-clock budget for the hook→daemon round trip, and the
    effective ceiling on the LTM attempt inside it. Small and independent of the
    cold-path ``_hook_budget_seconds()`` backstop. Must be finite: this budget
    becomes the client deadline (``now + budget``), and ``+inf`` is not a big
    budget but a deadline the daemon can never enforce — it rejects one as
    unusable (#722), which would silently disable surfacing.

    The daemon reserves a response margin out of this deadline and hands the
    rest to the surfacing engine as its absolute deadline (queue/lock wait
    and the engine's own pre-work debit it before the LTM attempt starts), so
    a value **below** ``surfacing.timeout_seconds`` silently shortens every LTM
    attempt rather than letting it run its configured course. That is the
    intended precedence — the host is waiting on this hook — but it means
    ``surfacing.timeout_seconds`` is only reachable when this budget exceeds it
    plus overhead. If searches abort here, the fix is a faster LTM (or a
    deliberately larger budget, paid in host latency), not a larger
    ``timeout_seconds``: use ``mms doctor`` telemetry and the ``error_timeout``
    fault counters rather than assuming a warm search is sub-second."""
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
    """Passed to the daemon's ``SurfacingEngine``. Gates the feedback loop,
    not event persistence: default ``False`` emits no ``stm_surfacing_feedback``
    rating prompt (the pure-hook path has no in-band channel for the model to
    return a rating) and skips the durable-demotion read; the daemon also
    forces auto-tune off in this mode. Surfacing telemetry is recorded either
    way whenever the dedup tracker exists: ``surfacing_events`` rows with
    ``server='builtin'`` and a ``sha256:`` digest query (the daemon forces
    ``persist_query_text=False``, so raw query text — e.g. a ``Bash`` command
    carrying secrets — never persists), so ``stm_surfacing_stats`` /
    ``mms stats`` / ``mms doctor`` reflect hook-path activity. See
    ``SurfacingEngine(record_feedback_events=...)``."""
    compression: HookCompressionConfig = Field(default_factory=HookCompressionConfig)
    """Built-in tool *output compression* (P1a — Bash). Independent of surfacing:
    see :class:`HookCompressionConfig`. Env: ``MEMTOMEM_STM_HOOK__COMPRESSION__*``."""
    metrics_enabled: bool = True
    """Record one per-call row (sizes + timings only, never tool input) into the
    shared ``proxy_metrics.db`` with ``source='hook'`` for every native built-in
    tool call the hook sees. Default ``True`` makes otherwise-invisible
    native-tool spend measurable (`mms stats --source hook`). Deliberately
    independent of ``proxy.metrics.enabled`` so a hook-only deployment (proxy
    disabled) still gets metrics; it reuses ``proxy.metrics.db_path`` /
    ``max_history`` for storage. Opt out with
    ``MEMTOMEM_STM_HOOK__METRICS_ENABLED=0``."""


def _is_loopback_host(host: str) -> bool:
    """True if ``host`` is a loopback bind target.

    Accepts the literal hostname ``localhost`` plus any loopback IP — the whole
    ``127.0.0.0/8`` range and IPv6 ``::1`` (incl. expanded / IPv4-mapped forms)
    — via :mod:`ipaddress`. Everything else (``0.0.0.0``, ``""`` which binds all
    interfaces, arbitrary hostnames) is non-loopback. Keyed on the address, not
    a fixed string allowlist, so legitimate loopback forms aren't false-rejected.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class DaemonConfig(BaseModel):
    """Local surfacing daemon (``mms daemon``) settings — Stage 2.

    Env keys are ``MEMTOMEM_STM_DAEMON__<field>``."""

    host: str = "127.0.0.1"
    """Loopback bind address. The daemon is local-only and authenticated by a
    per-start random token, not by network ACLs — never bind a non-loopback
    address. A non-loopback ``host`` (including ``0.0.0.0`` and ``""``, which
    bind every interface) is rejected unless ``allow_non_loopback`` is set."""
    allow_non_loopback: bool = False
    """Escape hatch for a deliberate non-loopback bind. Off by default so a
    stray ``MEMTOMEM_STM_DAEMON__HOST=0.0.0.0`` fails fast at config load
    instead of silently exposing the token-guarded daemon on a network
    interface. Set ``MEMTOMEM_STM_DAEMON__ALLOW_NON_LOOPBACK=true`` to override."""
    idle_timeout_seconds: float = Field(default=900.0, ge=0.0)
    """Shut the daemon (and its warm LTM child) down after this many seconds
    with no requests, so an abandoned coding session doesn't leak a
    multi-GB process forever. ``0`` disables idle shutdown (pin the process)."""
    max_pending_requests: int = Field(default=32, ge=1, le=1024)
    """Maximum number of hook or standalone surfacing requests admitted
    concurrently. Requests beyond this bound fail open immediately instead of
    building an unbounded queue behind the daemon's single LTM session."""

    @model_validator(mode="after")
    def _reject_non_loopback_host(self) -> "DaemonConfig":
        if not self.allow_non_loopback and not _is_loopback_host(self.host):
            raise ValueError(
                f"DaemonConfig.host={self.host!r} is not a loopback address. The "
                "surfacing daemon is local-only and authenticated by a per-start "
                "token, not network ACLs, so a non-loopback bind exposes it on "
                "that interface. Set MEMTOMEM_STM_DAEMON__ALLOW_NON_LOOPBACK=true "
                "to override intentionally."
            )
        return self


class STMConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMTOMEM_STM_",
        env_nested_delimiter="__",
    )

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "WARNING"
    log_file: Path | None = None
    """Opt-in rotating file log (#612), e.g. ``~/.memtomem/stm.log`` — set via
    ``MEMTOMEM_STM_LOG_FILE``. For an MCP-launched stdio server, stderr is
    captured (or dropped) by the client, so this is the diagnosable trail.
    Written in addition to stderr via a size-rotating handler (2 MiB × 3
    backups), file ``0o600`` / parent ``0o700`` per the data-at-rest
    convention. ``None`` (default) keeps stderr-only logging. Stored raw;
    ``expanduser()`` happens at the use site (like ``data_dir``)."""
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    surfacing: SurfacingConfig = Field(default_factory=SurfacingConfig)
    formation: FormationConfig = Field(default_factory=FormationConfig)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
    hook: HookConfig = Field(default_factory=HookConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    data_dir: Path = Path("~/.memtomem")

    advertise_observability_tools: bool = False
    """Whether STM's own observability/admin MCP tools (``stm_proxy_stats``,
    ``stm_proxy_health``, ``stm_proxy_cache_clear``, ``stm_surfacing_stats``,
    ``stm_selection_stats``, ``stm_compression_stats``,
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
