"""STM (Short-Term Memory) root configuration."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
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


class OtlpExportConfig(BaseModel):
    """Outbound OTLP span export (#789 stage 2, ADR 0001 gate ``otlp-telemetry-export``).

    Named for the standard, not for any one consumer: STM exports OTLP spans
    and any OTLP receiver can consume them (tracegraph is the motivating one).
    Startup-only, like :class:`LangfuseConfig` — the exporter owns a
    background worker and a shutdown flush, so it is not part of the proxy's
    hot-reload domain. Env keys are ``MEMTOMEM_STM_OTLP__<field>``.

    Invalid *configuration* fails startup; every *runtime* export failure
    degrades open (dropped and counted, never raised) — a telemetry consumer
    is not a dependency of the calls it accounts for. The exported attribute
    vocabulary is body-free by construction; see ``docs/otlp-export.md``.
    """

    model_config = ConfigDict(
        # A ValidationError renders `input_value`, and server.py logs config
        # validation exceptions — `headers` can carry an authorization token.
        hide_input_in_errors=True,
        # pydantic's `gt=0` admits `+inf` (#722): a non-finite timeout would
        # be legal here and then hang a shutdown deadline.
        allow_inf_nan=False,
    )

    enabled: bool = False
    """Opt-in (default ``False``). Read once at startup, not hot-reloaded."""
    endpoint: str = ""
    """OTLP/HTTP traces endpoint, e.g. ``http://localhost:4318`` (a bare base
    URL gets ``/v1/traces`` appended) or ``http://collector/custom/path``
    (used verbatim). Required when ``enabled``; validated structurally so a
    malformed URL fails startup instead of becoming a permanent export
    error."""
    headers: dict[str, str] = Field(default_factory=dict)
    """Extra OTLP/HTTP headers, e.g. an authorization token. Values are never
    logged and never exported as span attributes."""
    timeout_seconds: float = Field(default=10.0, gt=0.0)
    """Per-export HTTP timeout."""
    sampling_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    """Head sampling ratio, ``ParentBased(TraceIdRatioBased)``. ``0.0`` is
    legal and means "sample nothing" — use ``enabled=false`` to also skip
    building the exporter."""
    max_queue_size: int = Field(default=2048, gt=0)
    """Batch processor queue depth. Spans beyond it are dropped by the SDK."""
    max_export_batch_size: int = Field(default=512, gt=0)
    """Spans per export request. Must not exceed ``max_queue_size``."""
    schedule_delay_ms: int = Field(default=5000, gt=0)
    """Batch processor flush interval."""
    flush_timeout_seconds: float = Field(default=5.0, gt=0.0)
    """Wall-clock ceiling for the whole shutdown sequence (drain of open
    spans + final flush). Exceeding it abandons the remaining spans by
    design; see ``shutdown_otlp``."""

    @model_validator(mode="after")
    def _require_endpoint_when_enabled(self) -> "OtlpExportConfig":
        if self.enabled and not self.endpoint:
            raise ValueError(
                "OtlpExportConfig.enabled=true requires endpoint to be set "
                "(e.g. http://localhost:4318)."
            )
        return self

    @model_validator(mode="after")
    def _validate_endpoint_shape(self) -> "OtlpExportConfig":
        if self.endpoint:
            normalize_otlp_endpoint(self.endpoint)
        return self

    @model_validator(mode="after")
    def _reject_standard_header_env(self) -> "OtlpExportConfig":
        """Own the header channel outright when export is enabled.

        The exporter resolves headers as ``headers or parse_env_headers(...)``,
        so an empty STM mapping hands the channel to
        ``OTEL_EXPORTER_OTLP_HEADERS`` / ``..._TRACES_HEADERS`` — bypassing
        :meth:`_validate_header_syntax`, and letting the SDK's parser log a
        malformed value verbatim while it constructs the exporter. Since that
        value is the one carrying the credential, STM refuses the variables
        rather than validating around them.

        This is the one place STM narrows the "standard OTel env fills what
        config does not" contract; TLS material, compression and the rest are
        untouched.
        """
        if not self.enabled:
            return self
        import os

        for name in ("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_HEADERS"):
            if os.environ.get(name):
                raise ValueError(
                    f"{name} is set, but STM owns the OTLP header channel when "
                    "otlp.enabled=true — the SDK would consume it without the "
                    "syntax check that keeps a malformed credential out of the "
                    "logs. Move it to MEMTOMEM_STM_OTLP__HEADERS and unset "
                    f"{name} (value withheld)."
                )
        return self

    @model_validator(mode="after")
    def _validate_header_syntax(self) -> "OtlpExportConfig":
        """Reject headers the HTTP layer would refuse — before it sees them.

        An `authorization` value carrying a newline passes every other check
        here, and is then rejected deep in the export path by a layer that
        quotes the offending value into an ERROR log. A credential logged in
        full is exactly what the rest of this block exists to prevent, so the
        syntax check happens at startup, where the failure can be reported
        without echoing anything.
        """
        for name, value in self.headers.items():
            if not name or not _HEADER_NAME_RE.fullmatch(name):
                raise ValueError(
                    "OtlpExportConfig.headers contains an invalid header name "
                    "(RFC 7230 token characters only; name and value withheld)."
                )
            if not _HEADER_VALUE_RE.fullmatch(value):
                # The name is withheld too: a credential is itself a valid
                # RFC 7230 token, so `headers={"<token>": "\n"}` would print
                # the secret while reporting that its value is malformed.
                raise ValueError(
                    "OtlpExportConfig.headers contains an invalid value — no "
                    "control characters, and no leading or trailing whitespace "
                    "(name and value withheld)."
                )
        return self

    @model_validator(mode="after")
    def _batch_fits_queue(self) -> "OtlpExportConfig":
        if self.max_export_batch_size > self.max_queue_size:
            raise ValueError(
                "OtlpExportConfig.max_export_batch_size "
                f"({self.max_export_batch_size}) must not exceed max_queue_size "
                f"({self.max_queue_size})."
            )
        return self

    @model_validator(mode="after")
    def _require_otlp_packages_when_enabled(self) -> "OtlpExportConfig":
        if self.enabled:
            from importlib.util import find_spec

            # find_spec raises ModuleNotFoundError when a *parent* package is
            # absent — which is the ordinary "extra not installed" case, so
            # treating it as anything but "missing" would replace this
            # actionable error with a traceback.
            def _absent(name: str) -> bool:
                try:
                    return find_spec(name) is None
                except (ModuleNotFoundError, ImportError, ValueError):
                    return True

            missing = [
                name
                for name in ("opentelemetry.sdk", "opentelemetry.exporter.otlp.proto.http")
                if _absent(name)
            ]
            if missing:
                raise ValueError(
                    "OtlpExportConfig.enabled=true but the OpenTelemetry packages are "
                    f"not installed (missing: {', '.join(missing)}). Install the otlp "
                    "extra (e.g. `uv tool install --reinstall 'memtomem-stm[otlp]'` "
                    "or `pip install 'memtomem-stm[otlp]'`)."
                )
        return self


# A header name must be an RFC 7230 token. For values STM is deliberately
# *stricter* than the HTTP stack: visible ASCII plus space and horizontal tab,
# no leading or trailing whitespace. RFC 7230 also permits obs-text
# (\x80-\xff) and the requests stack accepts it, but a Latin-1 header value has
# no use here and narrowing the grammar keeps this check easy to reason about.
# What matters is the exclusion of CR and LF — the case that would otherwise
# reach a logger.
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_HEADER_VALUE_RE = re.compile(r"[\x21-\x7e]([\x20\x09\x21-\x7e]*[\x21-\x7e])?|")


def normalize_otlp_endpoint(endpoint: str) -> str:
    """Validate an OTLP/HTTP endpoint and return the traces URL to POST to.

    OpenTelemetry appends ``/v1/traces`` to ``OTEL_EXPORTER_OTLP_ENDPOINT``
    but uses ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` — and an endpoint passed
    to the exporter constructor — verbatim. STM takes a base URL *or* a full
    signal URL, so it applies that suffix itself: a path of ``""`` or ``/``
    becomes ``/v1/traces``, any other path is kept as given.

    Raises ``ValueError`` (surfacing as a startup config error) rather than
    letting a malformed URL become a permanent runtime export failure. The
    messages never echo the endpoint: a rejected URL is exactly the one most
    likely to carry the credential or token that got it rejected, and these
    errors are logged.
    """
    from urllib.parse import urlsplit, urlunsplit

    # Nothing derived from the endpoint is interpolated into any message
    # below — not the scheme, not the port, not a parser exception. urlsplit
    # embeds the netloc in its own errors (invalid IPv6, NFKC normalization),
    # and the port parser quotes the offending text, so both are caught and
    # re-raised generically rather than allowed to surface.
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        raise ValueError("OTLP endpoint is not a parseable URL (value withheld).") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError("OTLP endpoint must use http or https (value withheld).")
    # ``is not None``, not truthiness: ``http://@host`` parses to an *empty*
    # username, which is still userinfo syntax and still not a valid endpoint.
    if parts.username is not None or parts.password is not None:
        raise ValueError(
            "OTLP endpoint must not embed credentials — put them in "
            "otlp.headers instead (value withheld)."
        )
    if parts.query or parts.fragment:
        raise ValueError("OTLP endpoint must not carry a query or fragment (value withheld).")
    # ``.port`` only validates when read; an out-of-range or non-numeric port
    # otherwise sails through to become a permanent connection failure.
    try:
        parts.port
    except ValueError:
        raise ValueError(
            "OTLP endpoint has an invalid port — must be an integer in 0-65535 (value withheld)."
        ) from None
    try:
        host = parts.hostname
    except ValueError:
        raise ValueError("OTLP endpoint has an invalid host (value withheld).") from None
    if not host:
        raise ValueError("OTLP endpoint must include a host (value withheld).")
    if any(char.isspace() for char in host):
        raise ValueError("OTLP endpoint host must not contain whitespace (value withheld).")
    path = parts.path if parts.path not in ("", "/") else "/v1/traces"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


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
        # A child model's `hide_input_in_errors` does not govern an error
        # raised on the *parent* field, so a whole block that fails to coerce
        # renders here verbatim — including `otlp.headers` and
        # `langfuse.secret_key`. These errors are logged (server.py) and
        # surfaced to MCP clients, so the setting belongs at the root.
        hide_input_in_errors=True,
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
    otlp: OtlpExportConfig = Field(default_factory=OtlpExportConfig)
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
