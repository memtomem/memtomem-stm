# OTLP Span Export

STM can export its own spans over OTLP/HTTP to any OpenTelemetry collector
(#789). For how this boundary relates to STM's other cross-tool exchanges —
and why it is a *different thing* from the selection log — see
[ADR 0001](adr/0001-ecosystem-integration-contracts.md); for the append-only
selection/execution record, see [Selection Telemetry](selection-telemetry.md).

The two documents describe different signals and neither is derived from the
other. The selection log answers "which tool was chosen, out of what
candidate set, and how did the call go", keyed by an application-level
`trace_id`. This export answers "what happened inside a call, and in what
order", with genuine W3C trace and span ids and genuine parentage. Spans are
emitted by the code as it runs; nothing here is reconstructed from log
records.

Off by default — it is a new outbound network path, so the operator opts in,
and the OpenTelemetry SDK is an optional extra:

```bash
uv tool install --reinstall 'memtomem-stm[otlp]'   # or: pip install 'memtomem-stm[otlp]'
```

```bash
export MEMTOMEM_STM_OTLP__ENABLED=1
export MEMTOMEM_STM_OTLP__ENDPOINT=http://localhost:4318
```

Configuration lives on the STM root config (env only), not in
`stm_proxy.json` — like `langfuse`, and for the same reason: spans cross the
proxy, the surfacing engine and the STM control tools, so it is not part of
the proxy's hot-reload domain. Every key is read once at startup.

| Key | Default | Meaning |
|---|---|---|
| `MEMTOMEM_STM_OTLP__ENABLED` | `false` | Opt in. |
| `MEMTOMEM_STM_OTLP__ENDPOINT` | — | Required when enabled. See *Endpoint* below. |
| `MEMTOMEM_STM_OTLP__HEADERS` | `{}` | Extra HTTP headers (JSON object), e.g. an auth token. Syntax-checked at startup. |
| `MEMTOMEM_STM_OTLP__TIMEOUT_SECONDS` | `10` | Per-export HTTP timeout. |
| `MEMTOMEM_STM_OTLP__SAMPLING_RATE` | `1.0` | `ParentBased(TraceIdRatioBased)`. `0.0` samples nothing. |
| `MEMTOMEM_STM_OTLP__MAX_QUEUE_SIZE` | `2048` | Batch queue depth; spans beyond it are dropped by the SDK. |
| `MEMTOMEM_STM_OTLP__MAX_EXPORT_BATCH_SIZE` | `512` | Spans per request. Must not exceed the queue size. |
| `MEMTOMEM_STM_OTLP__SCHEDULE_DELAY_MS` | `5000` | Batch flush interval. |
| `MEMTOMEM_STM_OTLP__FLUSH_TIMEOUT_SECONDS` | `5` | Whole-shutdown budget. See *Shutdown*. |

## Endpoint

Give either a base URL or a full signal URL:

- `http://localhost:4318` → posts to `http://localhost:4318/v1/traces`
- `https://collector.example/custom/path` → used verbatim

The reason both work: OpenTelemetry appends `/v1/traces` to
`OTEL_EXPORTER_OTLP_ENDPOINT` but uses `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` —
and an endpoint passed programmatically — verbatim. STM applies that suffix
itself when the path is empty or `/`.

The endpoint is validated at startup, not at first export: it must be
`http`/`https`, must carry a host, must not embed credentials (put those in
`headers`), and must not carry a query or fragment. A malformed endpoint
fails startup rather than becoming a permanent, quiet export failure.

## What is exported

Spans mirror STM's existing tracing boundaries. `proxy_call` is the root of a
proxied tool call; the pipeline stages nest under it, and `upstream_rpc`
covers the upstream MCP call including its retries — the split between
upstream time and STM pipeline time.

```
proxy_call
├── upstream_rpc
├── proxy_call_clean
├── proxy_call_compress
└── proxy_call_surface
```

`proxy_call_read_more` is deliberately a **root**, not a child of the call
that produced the handle. The two are separate MCP turns, so there is no live
span to parent to; correlate through the `stm.trace_id` attribute. Promoting
that application id to a trace identity would be exactly the synthesized
lineage ADR 0001 forbids. (A real OTel *link* would need the originating
span's `SpanContext` persisted with the progressive-read record — a schema
change, deferred.)

### Attribute vocabulary

The vocabulary is defined **per span**, not per attribute name, because the
same name can have different provenance in different places: `tool` on
`proxy_call` is the upstream tool STM routed to, while `tool` on
`stm_surfacing_stats` would be a filter string the caller typed. Only the
first is exportable.

| Span | Attributes |
|---|---|
| `proxy_call` | `stm.server`, `stm.tool`, `stm.trace_id` |
| `proxy_call_read_more` | `stm.server`, `stm.tool`, `stm.trace_id` |
| `proxy_call_cache_hit` | `stm.server`, `stm.tool` |
| `proxy_call_clean` | `stm.server`, `stm.tool` |
| `proxy_call_compress` | `stm.server`, `stm.tool`, `stm.compression.strategy`, `stm.compression.max_chars` |
| `proxy_call_surface` | `stm.server`, `stm.tool`, `stm.surfacing.path` |
| `proxy_call_index` | `stm.server`, `stm.tool` |
| `upstream_rpc` | `stm.server`, `stm.tool` |

`error.type` — the exception class name — is added to any span whose body
raised. `stm.trace_id` is STM's application correlation id, **not** a W3C
trace id.

Every other span STM emits — the STM control tools (`stm_surfacing_feedback`,
`stm_surfacing_stats`, `stm_selection_stats`) and the surfacing engine's own
spans — carries **no attributes at all**, because its metadata comes from MCP
call arguments. Those spans still contribute timing and lineage.

Span timing is the duration signal; per-stage millisecond counters are not
duplicated as attributes.

## Privacy classification

Body-free by construction, not by convention. The admission rule is
**provenance**: a value is exportable only if STM derived it from its own
configuration, routing or pipeline. Values that originate in an MCP call
argument are not exportable, which is why the vocabulary is per-span and why
spans whose metadata comes from arguments export nothing.

On top of that rule: a key not in the span's map is dropped whatever its
value, a value of the wrong type is dropped, and every string that survives is
run through the same credential/PII screen the selection log uses before
appending. A screened-out value is dropped and counted as
`attributes_redacted`. The screen is a backstop, not the boundary — it
recognizes known secret and PII shapes, so it is not relied on to sanitize
free-form caller input.

Never exported, by policy:

- **Response content**, in any form.
- **Error messages.** A failed span's only error detail is `error.type` —
  the exception class name, itself privacy-screened and replaced with
  `redacted` if it trips. `record_exception()` is never called and the span
  status carries no description, because both would embed the message, and
  STM's `error_message` / `index_error` / `extract_error` / `surface_error`
  fields are documented as unsanitized.
- **Tool arguments**, in any form.
- **OTLP header values, the endpoint, and any other credential-bearing
  exporter configuration** — never as a span attribute, a resource attribute,
  or anything in the exported payload. Configured headers are of course sent
  *as HTTP headers* on the export request itself; that is how they
  authenticate STM to your collector. What they never do is become telemetry.
  (Non-credential pipeline settings such as the compression strategy in use
  *are* exported as attributes — see the vocabulary above.) Endpoint
  validation errors withhold the endpoint too, since a rejected URL is the one
  most likely to carry the credential that got it rejected.
- **`OTEL_RESOURCE_ATTRIBUTES`.** The resource is built explicitly
  (`service.name`, `service.version`) rather than through the environment
  detector, because resource attributes are exported alongside spans and
  would otherwise bypass the map and the screen entirely.
- The **progressive-read handle**, an opaque retrieval token with no consumer
  value here.

STM config wins wherever it is set; the standard `OTEL_EXPORTER_OTLP_*`
environment variables still supply what it does not (TLS material,
compression). That is the "adopt the external standard" contract of ADR
0001 — with the resource exception above.

## Degradation

Invalid configuration fails startup. Everything at runtime degrades open: a
telemetry consumer is never a dependency of the calls it accounts for.

- Header syntax is checked at startup. A value carrying a control character
  — a newline in an `authorization` token, say — would otherwise be rejected
  deep in the export path by a layer that logs the rejection *with the value*.
  Startup is the last point at which that failure can be reported without
  printing the credential, so it is rejected there, generically.
- Initialization failures are not degraded away. If export is enabled and the
  exporter cannot be built — a malformed endpoint, a missing package, an
  invalid standard `OTEL_EXPORTER_OTLP_*` value — the server fails to start
  rather than running with telemetry silently inert.
- A failed export is counted (`export_failures`). STM's own warning about it
  is emitted **once** per process and then drops to DEBUG. That rate limit
  covers STM's message only — the OpenTelemetry exporter logs its own ERROR
  for every failed batch, and those are left alone deliberately: they carry
  the retry and status detail an operator needs to diagnose a collector
  outage. To quieten them, raise the level of the
  `opentelemetry.exporter.otlp` logger. Proxied calls are unaffected either
  way.
- Queue-overflow drops are logged by the SDK and are deliberately *not*
  folded into `export_failures` — that counter means "an export attempt
  failed", not "a span was lost".
- A collector that is down at startup is not an error. There is no
  preflight; OTLP/HTTP is per-batch.

Counters appear in `stm_proxy_health`, including when no upstream servers are
configured — export is independent of the proxy block, so it stays visible in
control-only mode:

```
OTLP Span Export
================
  spans: 128 started, 128 ended
  export failures: 0
  attributes redacted: 0
  shutdown flush timeouts: 0
```

## Shutdown

On shutdown STM stops admitting new spans, waits for the spans already open
to finish, then flushes — all under a **single** `flush_timeout_seconds`
deadline, so a slow drain cannot be followed by a full-length flush.

The flush runs on a daemon thread. If the deadline expires, the remaining
spans are abandoned by design and `shutdown_flush_timeout` is incremented:
a hung collector delays process exit by at most that budget rather than
holding the interpreter open.

## Running alongside Langfuse

Both can be enabled at once. They are independent: STM's OTLP exporter owns a
private `TracerProvider` that is never installed as the global one, and it
never reads the ambient OpenTelemetry context.

That independence is deliberate rather than incidental. Langfuse ≥ 4 installs
its own global provider, and the OTel *context* is process-global even when
providers are not — so starting a span the ordinary way would silently adopt
an active Langfuse span as parent, producing a span whose parent lives in
another backend. Each consumer therefore builds its own tree from its own
decisions, and neither can corrupt the other's parentage.

One asymmetry is worth knowing: this exporter samples with
`ParentBased(TraceIdRatioBased)`, which is trace-scoped — a trace is kept or
dropped whole. Langfuse's `sampling_rate` is evaluated per boundary, so below
`1.0` it can keep a child while dropping its parent, leaving partial trees on
that side. The OTLP side is unaffected.

`MEMTOMEM_STM_LANGFUSE__*` configures Langfuse; `MEMTOMEM_STM_OTLP__*`
configures this exporter. They do not share knobs.

## Not yet covered

Stated plainly so the export is not read as more complete than it is:

- The LTM MCP subcalls made during surfacing have no spans of their own yet.
- Background auto-index and extraction run without spans, so they do not
  appear under `proxy_call`.
- `proxy_call_read_more` correlates by attribute, not by an OTel link.
- No OpenInference semantic-convention attributes are emitted. Its core
  conventions (`input.value`, `output.value`) are body-bearing, so adopting
  them would contradict the section above; OTLP is the transport contract
  here, and the span vocabulary is STM's own `stm.*` namespace.
- `stm.selection_id` is not emitted: the selection log's join key is not in
  scope at the span boundary. Correlate through `stm.trace_id`, which both
  surfaces carry.
