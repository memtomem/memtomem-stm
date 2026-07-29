# Environment Variable Reference

Nested settings use Pydantic's double-underscore convention, for example
`MEMTOMEM_STM_SURFACING__MIN_SCORE=0.03`.

## Configuration-source boundary

`~/.memtomem/stm_proxy.json` loads `ProxyConfig` only. Root, surfacing,
formation, hook, daemon, Langfuse, and OTLP settings are environment/default-only; placing those
blocks in `stm_proxy.json` has no effect. The proxy's `consumer_model` is the
documented propagation exception used by surfacing model-budget resolution.

## Root and proxy process

| Variable | Default | Purpose |
|---|---|---|
| `MEMTOMEM_STM_DATA_DIR` | `~/.memtomem` | Directory for daemon handshakes, daemon ownership locks, and the detached daemon log |
| `MEMTOMEM_STM_PROXY__ENABLED` | `false` | Global proxy enable switch. `mms init` and `mms add` write `enabled: true` into the config they create. |
| `MEMTOMEM_STM_PROXY__CONFIG_PATH` | `~/.memtomem/stm_proxy.json` | Proxy JSON path |
| `MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS` | `false` | Expose eight operator MCP tools |

## Review-first formation

| Variable | Default | Purpose |
|---|---|---|
| `MEMTOMEM_STM_FORMATION__ENABLED` | `false` | Advertise `stm_memory_propose` and allow pending-candidate submission to a capable core |
| `MEMTOMEM_STM_FORMATION__MAX_CONTENT_CHARS` | `2000` | Maximum candidate content; values above 2000 are rejected |

## Surfacing

The complete behavior is described in [Proactive Memory Surfacing](../surfacing.md).
Important controls include:

| Setting suffix | Default | Purpose |
|---|---|---|
| `ENABLED` | `true` | Global surfacing switch |
| `USE_DAEMON` | `false` | Route standalone surfacing through the shared local daemon; no private fallback |
| `MIN_SCORE` | `0.03` | Result threshold |
| `AUTO_TUNE_SCORE_FLOOR` | `0.005` | Default lower tuning bound |
| `AUTO_TUNE_SCORE_CEILING` | `0.05` | Default upper tuning bound |
| `CONTEXT_TOOLS` | `{}` | Per-tool templates and fixed thresholds |
| `MIN_RESPONSE_CHARS` | `5000` | Cleaned pre-compression size gate |
| `WARMUP_ENABLED` | `true` | Background LTM warm-up |

Use the prefix `MEMTOMEM_STM_SURFACING__`. When an explicit `min_score` lies
outside the default tuning range, validation widens the effective range to
include that configured value rather than silently clamping it away.
For example, set `MEMTOMEM_STM_SURFACING__USE_DAEMON=true` to share the local
daemon across standalone proxy processes.

## Hook and daemon

| Variable | Default | Purpose |
|---|---|---|
| `MEMTOMEM_STM_HOOK__USE_DAEMON` | `true` | Use the warm daemon path |
| `MEMTOMEM_STM_HOOK__AUTO_SPAWN` | `true` | Start a missing daemon |
| `MEMTOMEM_STM_HOOK__FALLBACK` | `skip` | `skip` or legacy `cold` fallback |
| `MEMTOMEM_STM_HOOK__METRICS_ENABLED` | `true` | Record size/timing-only hook rows |
| `MEMTOMEM_STM_HOOK__RECORD_FEEDBACK_EVENTS` | `false` | Persist hook surfacing feedback events |
| `MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED` | `false` | Enable Claude Bash output replacement |
| `MEMTOMEM_STM_DAEMON__MAX_PENDING_REQUESTS` | `32` | Bound admitted hook and standalone surfacing requests |

## OTLP span export

Off by default and requires the `otlp` extra. Read once at startup. Full
attribute vocabulary and degradation policy: [OTLP Span Export](../otlp-export.md).

| Variable | Default | Purpose |
|---|---|---|
| `MEMTOMEM_STM_OTLP__ENABLED` | `false` | Export STM's spans over OTLP/HTTP |
| `MEMTOMEM_STM_OTLP__ENDPOINT` | — | Required when enabled; a bare base URL gets `/v1/traces` appended, any other path is used verbatim. Validated at startup |
| `MEMTOMEM_STM_OTLP__HEADERS` | `{}` | Extra HTTP headers (JSON object), e.g. an auth token. Sent as request headers to authenticate to the collector; syntax-checked at startup so a malformed value cannot reach a logger; never turned into span or resource attributes |
| `MEMTOMEM_STM_OTLP__TIMEOUT_SECONDS` | `10` | Per-export HTTP timeout |
| `MEMTOMEM_STM_OTLP__SAMPLING_RATE` | `1.0` | `ParentBased(TraceIdRatioBased)` head sampling |
| `MEMTOMEM_STM_OTLP__MAX_QUEUE_SIZE` | `2048` | Batch queue depth |
| `MEMTOMEM_STM_OTLP__MAX_EXPORT_BATCH_SIZE` | `512` | Spans per request; must not exceed the queue size |
| `MEMTOMEM_STM_OTLP__SCHEDULE_DELAY_MS` | `5000` | Batch flush interval |
| `MEMTOMEM_STM_OTLP__FLUSH_TIMEOUT_SECONDS` | `5` | Whole-shutdown budget (drain + final flush) |

## Langfuse

Langfuse tracing is optional and disabled until its nested enabled setting and
credentials are configured. Never commit tracing secrets to example files.

For the file-backed schema, see [Proxy Configuration Reference](proxy-config.md).
