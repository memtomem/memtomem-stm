"""OTLP span export — STM's outbound telemetry boundary (#789 stage 2).

This is the ``Outbound telemetry`` profile of ADR 0001: STM is the *producer*,
the contract is the external standard (OTLP), and two properties are
load-bearing.

**Real lineage.** Spans carry genuine W3C trace/span ids and genuine
parentage: a child span's parent is the span its code actually ran inside.
Nothing is reconstructed from adjacent log records. The application-level
``trace_id`` STM already threads through the proxy (a 16-hex correlation id,
not a W3C trace id) rides along as the ``stm.trace_id`` *attribute* and is
never promoted to a trace identity.

**Body-free attributes.** The admission rule is *provenance*: a value is
exportable only if STM derived it from its own configuration, routing or
pipeline. That is why :data:`_SPAN_ATTRIBUTES` is keyed by span name — the
same metadata key can be STM-derived at one span and caller-typed at another —
and why spans whose metadata comes from MCP arguments export no attributes at
all. On top of that, a key outside the span's map is dropped whatever its
type, a wrong-typed value is dropped, and every surviving string goes through
the same privacy screen the selection log uses. Error information appears only
as an exception class name. There is no path by which a response body, a tool
argument, an error message, or a header value becomes a span attribute.

Why a private ``TracerProvider`` that is never installed globally: Langfuse
(the other consumer of ``traced()``) registers its own global provider, and
the OTel *context* is process-global even when providers are not. Starting a
span the ordinary way would silently adopt an active Langfuse span as parent —
a parent that lives in a different backend, which is exactly the synthesized
lineage the ADR forbids. So this module keeps its own parent stack in a
``ContextVar`` and always passes an explicit context, rooted at an empty one.

Failure policy: invalid configuration fails startup (in
:class:`~memtomem_stm.config.OtlpExportConfig`); everything at runtime
degrades open once the exporter exists — construction failures propagate, so
enabled telemetry is never silently inert. Export failures are counted, and
STM's own warning about them is emitted once (the SDK keeps logging each
failed batch; see ``_log_export_failure``). A span that cannot be started is
not a span — never an error.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Iterator, cast

from memtomem_stm.proxy.privacy import contains_sensitive_content

if TYPE_CHECKING:  # pragma: no cover - typing only
    from memtomem_stm.config import OtlpExportConfig

logger = logging.getLogger(__name__)

_SERVICE_NAME = "memtomem-stm"

_emitter: OtlpEmitter | None = None

# The exported attribute vocabulary, keyed by span name and then by metadata
# key. Two levels, not one, because **the same metadata key can have different
# provenance at different spans**: ``tool`` on ``proxy_call`` is the upstream
# tool STM routed to (STM-derived, from its own discovered tool list), while
# ``tool`` on ``stm_surfacing_stats`` is a filter string the caller typed. A
# flat key map cannot tell those apart, and would export the second one.
#
# The admission rule is therefore provenance, not shape: a value is exportable
# only if STM derived it from its own configuration, routing or pipeline. Every
# span absent from this map — the STM control tools and the surfacing engine's
# own spans, whose metadata comes from MCP arguments — exports **no**
# attributes at all. It still carries timing and lineage, which is what a
# call-graph consumer wants from it.
#
# Also absent by policy: the progressive-read handle (``key``), an opaque
# retrieval token; and every ``*_error`` field, since
# ``CallMetrics.error_message`` and its siblings are documented as unsanitized.
_SERVER_TOOL: dict[str, tuple[str, type | tuple[type, ...]]] = {
    "server": ("stm.server", str),
    "tool": ("stm.tool", str),
}
_SPAN_ATTRIBUTES: dict[str, dict[str, tuple[str, type | tuple[type, ...]]]] = {
    "proxy_call": {**_SERVER_TOOL, "trace_id": ("stm.trace_id", str)},
    "proxy_call_read_more": {**_SERVER_TOOL, "trace_id": ("stm.trace_id", str)},
    "proxy_call_cache_hit": _SERVER_TOOL,
    "proxy_call_clean": _SERVER_TOOL,
    "proxy_call_compress": {
        **_SERVER_TOOL,
        "strategy": ("stm.compression.strategy", str),
        "max_chars": ("stm.compression.max_chars", int),
    },
    "proxy_call_surface": {**_SERVER_TOOL, "path": ("stm.surfacing.path", str)},
    "proxy_call_index": _SERVER_TOOL,
    "upstream_rpc": _SERVER_TOOL,
}

# Stand-in when an exception class name trips the privacy screen. A stable
# placeholder keeps the attribute present (so "this span failed" is still
# visible) without exporting the name.
_REDACTED_ERROR_TYPE = "redacted"

_warned_export_failure = False
_warned_export_failure_lock = threading.Lock()


def _log_export_failure(detail: str) -> None:
    """Warn once per process about export failures, then drop to DEBUG.

    Own latch, not the Langfuse one in ``tracing.py``: a broken OTLP endpoint
    must still produce its own first WARNING even if Langfuse already warned.

    The latch covers *this* message only. The OTLP exporter logs its own ERROR
    per failed batch; those are deliberately left alone, since they carry the
    retry and status detail an operator needs. Operators who want silence raise
    the level of the ``opentelemetry.exporter.otlp`` logger.
    """
    global _warned_export_failure
    with _warned_export_failure_lock:
        first = not _warned_export_failure
        _warned_export_failure = True
    if not first:
        logger.debug("OTLP span export failed (%s) (repeat, ignored)", detail)
        return
    logger.warning(
        "OTLP span export failed (%s) — spans dropped (repeats logged at DEBUG)",
        detail,
    )


def _screen_attributes(
    span_name: str, metadata: dict[str, Any] | None
) -> tuple[dict[str, Any], int]:
    """Map ``traced()`` metadata to span attributes; return them and the drop count.

    Drops everything for a span with no entry in :data:`_SPAN_ATTRIBUTES`, then
    anything whose key is not in that span's map, anything whose value has the
    wrong type, and any string value the privacy screen matches. Only the last
    of those is counted as a redaction — an unmapped key is not a redaction, it
    is simply not part of this span's vocabulary.
    """
    allowed = _SPAN_ATTRIBUTES.get(span_name)
    if not allowed:
        return {}, 0
    attributes: dict[str, Any] = {}
    redacted = 0
    for key, value in (metadata or {}).items():
        mapped = allowed.get(key)
        if mapped is None:
            continue
        name, expected = mapped
        # bool is a subclass of int; an int-typed field must not accept True.
        if expected is int and isinstance(value, bool):
            continue
        if not isinstance(value, expected):
            continue
        if isinstance(value, str) and contains_sensitive_content(value):
            redacted += 1
            continue
        attributes[name] = value
    return attributes, redacted


class _LogScreen(logging.Filter):
    """Redact SDK log records whose text trips the privacy screen.

    The exporter logs before returning, so :class:`_CountingExporter` cannot
    sanitize what it says — and what it says is not always ours. A collector
    that reflects the configured token into its HTTP reason phrase gets it
    logged verbatim (reproduced against the locked SDK), and any future SDK
    message could carry an endpoint path or header the same way.

    Screening the records themselves closes the channel rather than the one
    instance: a clean message is left completely alone, so the retry and
    status detail an operator needs survives, while a message carrying
    secret-shaped text is replaced.
    """

    def __init__(self, counters: _Counters) -> None:
        super().__init__()
        self._counters = counters

    def retarget(self, counters: _Counters) -> None:
        """Point an already-installed screen at the current emitter's counters."""
        self._counters = counters

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record
            return True
        if not contains_sensitive_content(message):
            return True
        record.msg = (
            "OTLP exporter reported a failure whose detail matched the privacy "
            "screen and was withheld (level %s)" % record.levelname
        )
        record.args = ()
        self._counters.bump("logs_redacted")
        return True


# The SDK loggers that can render exporter-supplied text.
_SDK_LOGGERS = (
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "opentelemetry.exporter.otlp.proto.http",
    "opentelemetry.util.re",
)


def _install_log_screen(counters: _Counters) -> None:
    """Attach the screen once per logger, rebinding it to the live counters.

    Idempotent across re-inits: adding a second filter would double-count, but
    leaving the first one bound to a dead emitter's counters would report
    redactions nobody can read. So an existing screen is retargeted instead.
    """
    for name in _SDK_LOGGERS:
        logger_obj = logging.getLogger(name)
        existing = next((f for f in logger_obj.filters if isinstance(f, _LogScreen)), None)
        if existing is not None:
            existing.retarget(counters)
            continue
        logger_obj.addFilter(_LogScreen(counters))


class _CountingExporter:
    """Wrap a ``SpanExporter`` so export failures are observable.

    ``BatchSpanProcessor`` runs the exporter on its own worker thread and
    swallows both a ``FAILURE`` result and any exception the exporter raises,
    so a counter incremented around ``traced()`` would never see an export
    problem. This sits at the boundary where failures actually happen.

    Queue-overflow drops are *not* counted here — the SDK discards those
    before the exporter is reached and logs them itself.
    """

    def __init__(self, delegate: Any, counters: _Counters) -> None:
        self._delegate = delegate
        self._counters = counters

    def export(self, spans: Any) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        try:
            result = self._delegate.export(spans)
        except Exception as exc:
            self._counters.bump("export_failures")
            _log_export_failure(type(exc).__name__)
            return SpanExportResult.FAILURE
        if result is not SpanExportResult.SUCCESS:
            self._counters.bump("export_failures")
            _log_export_failure("exporter reported failure")
        return result

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return bool(self._delegate.force_flush(timeout_millis))

    def shutdown(self) -> None:
        self._delegate.shutdown()


class _Counters:
    """The emitter's observable state, exposed through ``stm_proxy_health``."""

    _FIELDS = (
        "spans_started",
        "spans_ended",
        "export_failures",
        "attributes_redacted",
        "logs_redacted",
        "shutdown_flush_timeout",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values = dict.fromkeys(self._FIELDS, 0)

    def bump(self, field: str, amount: int = 1) -> None:
        if amount == 0:
            return
        with self._lock:
            self._values[field] += amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)


class OtlpEmitter:
    """Starts and ends OTLP spans with real, self-contained lineage.

    Parentage comes from ``_parent`` — this emitter's own ``ContextVar`` stack
    — never from the ambient OTel context, so a Langfuse span active in the
    same task can never become the parent of a span exported to a different
    backend. A span with no emitter parent is a root, started from an
    explicitly empty ``Context``.

    Shutdown is a two-phase, single-deadline sequence; see :meth:`shutdown`.
    """

    def __init__(self, provider: Any, counters: _Counters) -> None:
        self._provider = provider
        self._tracer = provider.get_tracer(_SERVICE_NAME)
        self._counters = counters
        self._parent: ContextVar[Any] = ContextVar(f"otlp_parent_{id(self)}", default=None)
        # One lock guards the closing flag *and* the active-span count, so a
        # span cannot check "open", pause, and register itself after shutdown
        # has already observed a count of zero.
        self._state = threading.Condition(threading.Lock())
        self._closing = False
        self._active = 0

    @property
    def counters(self) -> _Counters:
        return self._counters

    def snapshot(self) -> dict[str, int]:
        return self._counters.snapshot()

    def _admit(self) -> bool:
        """Reserve a slot for one span, or refuse because shutdown started.

        The closing check and the increment are one critical section. Split
        them and a span could observe "open", stall, and register itself after
        shutdown already saw a count of zero and started flushing.
        """
        with self._state:
            if self._closing:
                return False
            self._active += 1
            return True

    def _release(self) -> None:
        with self._state:
            self._active -= 1
            if self._active <= 0:
                self._state.notify_all()

    @contextmanager
    def span(self, name: str, metadata: dict[str, Any] | None = None) -> Iterator[Any]:
        """Enter one OTLP span.

        Self-safe, as ``_FanOut`` requires: every failure mode between here
        and the ``yield`` degrades to yielding ``None``, so a broken exporter
        cannot fail the traced call. Only the body's own exception escapes.
        """
        if not self._admit():
            yield None
            return
        try:
            span, token = self._start(name, metadata)
        except Exception as exc:
            _log_export_failure(f"span start: {type(exc).__name__}")
            span, token = None, None
        if span is None or token is None:
            self._release()
            yield None
            return
        try:
            yield span
        except BaseException as exc:
            self._set_error(span, exc)
            raise
        finally:
            self._parent.reset(token)
            try:
                span.end()
            except Exception as exc:  # pragma: no cover - SDK-internal failure
                _log_export_failure(f"span end: {type(exc).__name__}")
            self._counters.bump("spans_ended")
            self._release()

    def _start(self, name: str, metadata: dict[str, Any] | None) -> tuple[Any, Any]:
        """Screen the metadata, start the span under our own parent, push it."""
        from opentelemetry import trace as trace_api

        attributes, redacted = _screen_attributes(name, metadata)
        self._counters.bump("attributes_redacted", redacted)
        parent = self._parent.get()
        # An empty Context is the whole point: the ambient one may hold a
        # Langfuse span, and adopting it would parent this span into another
        # backend's trace.
        context = trace_api.Context()
        if parent is not None:
            context = trace_api.set_span_in_context(parent, context)
        span = self._tracer.start_span(name, context=context, attributes=attributes)
        self._counters.bump("spans_started")
        return span, self._parent.set(span)

    def _set_error(self, span: Any, exc: BaseException) -> None:
        """Mark the span failed by exception *class* only.

        Never ``record_exception()`` and never a status description: both
        would carry the exception message, and STM's error messages are
        documented as unsanitized (``proxy/metrics.py``).

        The class name is screened like any other exported string. A class
        name is usually a literal in source, but it need not be — a
        dynamically constructed exception type carries whatever the code that
        built it put there, so exempting it would be the one unscreened string
        on the whole boundary.
        """
        from opentelemetry.trace import Status, StatusCode

        # ``__name__``, not ``__qualname__``: matches the selection log's
        # ``error_type`` convention, and a qualname would carry enclosing
        # scope names.
        error_type = type(exc).__name__
        if contains_sensitive_content(error_type):
            error_type = _REDACTED_ERROR_TYPE
            self._counters.bump("attributes_redacted")
        try:
            span.set_attribute("error.type", error_type)
            span.set_status(Status(StatusCode.ERROR))
        except Exception:  # pragma: no cover - SDK-internal failure
            _log_export_failure("set error status")

    def shutdown(self, timeout_seconds: float) -> None:
        """Stop admitting spans, drain the open ones, flush — within one deadline.

        A single monotonic deadline covers both waits, so a drain that nearly
        exhausts the budget cannot be followed by a full-length flush. The
        flush runs on a *daemon* thread that is joined with the remaining
        budget: unlike the default executor's threads, an abandoned daemon
        thread cannot keep the interpreter alive, so a hung collector delays
        shutdown by at most the deadline.

        Exceeding the deadline is explicit span loss, counted as
        ``shutdown_flush_timeout``. Idempotent.
        """
        import time

        deadline = time.monotonic() + timeout_seconds

        with self._state:
            if self._closing:
                return
            self._closing = True
            while self._active > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._state.wait(timeout=remaining):
                    break
            drained = self._active == 0

        done = threading.Event()
        flushed = threading.Event()

        def _flush_and_shutdown() -> None:
            try:
                # A False return means the processor hit its own timeout with
                # spans still queued — the same explicit loss as missing our
                # deadline, and invisible unless we read the result.
                if self._provider.force_flush(int(max(deadline - time.monotonic(), 0) * 1000)):
                    flushed.set()
            except Exception as exc:
                _log_export_failure(f"flush: {type(exc).__name__}")
            try:
                self._provider.shutdown()
            except Exception as exc:
                _log_export_failure(f"shutdown: {type(exc).__name__}")
            done.set()

        worker = threading.Thread(target=_flush_and_shutdown, name="otlp-shutdown", daemon=True)
        worker.start()
        worker.join(timeout=max(deadline - time.monotonic(), 0))

        if not drained or not done.is_set() or not flushed.is_set():
            self._counters.bump("shutdown_flush_timeout")
            logger.warning(
                "OTLP shutdown exceeded its %.1fs budget — remaining spans abandoned",
                timeout_seconds,
            )


def init_otlp(config: OtlpExportConfig, *, span_processor: Any = None) -> OtlpEmitter | None:
    """Build the emitter, or return None when export is disabled.

    Only *disabled* yields ``None``. When export is enabled, an import or
    construction failure propagates rather than degrading — see the
    fail-fast boundary in the module docstring.

    *span_processor* replaces the production
    ``BatchSpanProcessor(_CountingExporter(OTLPSpanExporter(...)))`` — the seam
    tests use to attach an in-memory exporter, which is why it is a
    *processor* and not an exporter: a deterministic test needs
    ``SimpleSpanProcessor``, not batching.
    """
    global _emitter

    if not config.enabled:
        return None

    # No ImportError guard: config validation already refuses to enable export
    # without the packages, so a failure here means a broken or partial
    # install. Degrading would leave explicitly-enabled telemetry inert, which
    # is the failure this boundary's fail-fast contract exists to prevent.
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    counters = _Counters()
    _install_log_screen(counters)
    provider = TracerProvider(
        # Resource(...) directly, never Resource.create(): the latter runs the
        # environment detector, and OTEL_RESOURCE_ATTRIBUTES would then reach
        # the wire without passing the attribute map or the privacy screen.
        resource=Resource(
            attributes={"service.name": _SERVICE_NAME, "service.version": _stm_version()}
        ),
        sampler=ParentBased(TraceIdRatioBased(config.sampling_rate)),
        # We flush explicitly in shutdown(); the SDK's atexit hook would
        # otherwise re-join a hung flush after our deadline already expired.
        shutdown_on_exit=False,
    )

    if span_processor is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from memtomem_stm.config import normalize_otlp_endpoint

        exporter = OTLPSpanExporter(
            endpoint=normalize_otlp_endpoint(config.endpoint),
            headers=dict(config.headers) or None,
            timeout=config.timeout_seconds,
        )
        span_processor = BatchSpanProcessor(
            # Duck-typed rather than subclassed: the SDK is an optional extra,
            # so `SpanExporter` cannot be a module-level base class here.
            cast("Any", _CountingExporter(exporter, counters)),
            max_queue_size=config.max_queue_size,
            max_export_batch_size=config.max_export_batch_size,
            schedule_delay_millis=config.schedule_delay_ms,
        )
    provider.add_span_processor(span_processor)

    _emitter = OtlpEmitter(provider, counters)
    return _emitter


def _stm_version() -> str:
    from memtomem_stm import __version__

    return __version__


def get_otlp() -> OtlpEmitter | None:
    """Return the process emitter, or None when OTLP export is not running."""
    return _emitter


def shutdown_otlp(emitter: OtlpEmitter | None = None, *, timeout_seconds: float = 5.0) -> None:
    """Detach the emitter, then drain and flush it within *timeout_seconds*.

    Detaching first is what makes the drain finite: ``traced()`` reads the
    module singleton, so once it is cleared no *new* span can join the set
    being drained.
    """
    global _emitter

    target = emitter or _emitter
    _emitter = None
    if target is None:
        return
    try:
        target.shutdown(timeout_seconds)
    except Exception:
        logger.warning("OTLP shutdown failed", exc_info=True)
