# Operations

Reference for running memtomem-stm in production: safety, privacy, scaling, observability, and on-disk state.

## Safety & Resilience

### Circuit Breaker

A unified 3-state circuit breaker protects against cascading failures:

```mermaid
stateDiagram-v2
    [*] --> Closed
    Closed --> Open: 3 consecutive failures
    Open --> HalfOpen: 60s reset timeout
    HalfOpen --> Closed: probe call succeeds
    HalfOpen --> Open: probe call fails

    note right of Closed
        all calls pass through
    end note
    note right of Open
        calls blocked,
        fallback returned
    end note
    note right of HalfOpen
        exactly one probe
        call allowed
    end note
```

- **Closed**: all calls pass through normally
- **Open**: all surfacing/LLM calls blocked (falls back to original response or truncation)
- **Half-open**: allows exactly one probe call after timeout; success closes, failure re-opens

Applied to both surfacing (LTM search) and LLM compression (external API calls).

### Connection Recovery

```mermaid
flowchart TD
    Call["upstream call"] --> Try{"call succeeds?"}
    Try -->|yes| Done["return result"]
    Try -->|no| Class{"classify error"}
    Class -->|"transport<br/>(OSError, Timeout, …)"| Retry{"retries<br/>remaining?"}
    Class -->|"protocol<br/>(JSON-RPC -32600..-32603)"| Reset["reset connection<br/>(no retry)"]
    Class -->|"programming<br/>(TypeError, …)"| Raise["propagate immediately"]
    Retry -->|yes| Backoff["wait (1s → 2s → 4s,<br/>capped at 30s)"]
    Backoff --> Call
    Retry -->|no| Fail["fail with<br/>fallback / error"]
    Reset --> Fail
```

- **Retry with backoff** — transport errors retried up to `max_retries` (default 3) with exponential backoff (1s → 2s → 4s → max 30s)
- **Protocol error isolation** — JSON-RPC errors (-32600 to -32603) are not retried; the connection is reset for the next call
- **Error type filtering** — only transport errors (`OSError`, `ConnectionError`, `TimeoutError`, `EOFError`) and MCP errors trigger retry. Programming errors (`TypeError`, `AttributeError`) propagate immediately.

### Other Protections

- **Timeout** — 3s surfacing timeout, falls back to original compressed response
- **Rate limiting** — max 15 surfacings per minute (sliding window)
- **Write-tool skip** — never surfaces for `*write*`, `*create*`, `*delete*`, `*push*`, `*send*`, `*remove*` tools
- **Query cooldown** — deduplicates similar queries (Jaccard similarity > 0.95) within a 5s window
- **Response size gate** — skips surfacing for responses under `min_response_chars` (default 5000)
- **Session dedup** — same memory ID not shown twice in one session
- **Cross-session dedup** — recently surfaced memory IDs persisted to SQLite; not re-surfaced within `dedup_ttl_seconds` (default 7 days). Set to `0` to disable.
- **Injection size cap** — memory block truncated if total exceeds `max_injection_chars` (default 3000)
- **Boost guard** — each surfacing event can only boost `access_count` once (duplicate feedback ignored)
- **Fresh cache** — proxy cache stores pre-surfacing content; surfacing is re-applied on cache hit so memories stay current
- **Upstream size cap** — `max_upstream_chars` (default **10 M chars**) hard-caps the response loaded into memory before compression. Oversized payloads are truncated with a notice so a runaway upstream cannot OOM the proxy.

## Shutdown & Lifecycle

When STM is run as the packaged CLI (`mms serve` / `memtomem-stm-proxy`), the shutdown sequence is handled for you: signals cascade into `ProxyManager.stop()` and `SurfacingEngine.stop()`, which drain background tasks and close any held HTTP clients. Integrators embedding STM inside a long-lived application need to call these explicitly.

```python
from memtomem_stm.proxy.manager import ProxyManager
from memtomem_stm.surfacing.engine import SurfacingEngine

# ... run STM inside your app ...

await surfacing_engine.stop()   # cancels & drains webhook tasks
await proxy_manager.stop()      # drains background extraction,
                                # closes LLMCompressor / extractor httpx clients,
                                # closes per-upstream MCP connection stacks
```

| Shutdown step | What it cleans up |
|---------------|-------------------|
| `SurfacingEngine.stop()` | Cancels and awaits outstanding webhook tasks (fired via `asyncio.create_task`). Safe to call multiple times. |
| `ProxyManager.stop()` | Cancels background extraction tasks, calls `LLMCompressor.close()` and `Extractor.close()` to release each `httpx.AsyncClient`, and `aclose()`s every `AsyncExitStack` that owns an upstream MCP connection. |
| `LLMCompressor.close()` | Closes the held `httpx.AsyncClient` and sets it to `None` so the instance is safe to discard. Normally called via `ProxyManager.stop()`; call it directly only if you constructed `LLMCompressor` standalone (e.g. for tests or hot-reload replacement). |

**Ordering.** Stop surfacing first so webhook tasks do not race against a half-closed proxy. Then stop the proxy manager, which is responsible for closing compressor/extractor HTTP clients and upstream connections. Inverting the order leaks an `httpx.AsyncClient` if a webhook task is mid-flight.

**Hot-reload / replacement.** When swapping `LLMCompressor` or the relevance scorer at runtime (e.g. because config changed), `await old.close()` before discarding the reference so the old `httpx.AsyncClient` is drained. `ProxyManager` already does this for `LLMCompressor` via its config-reload path; the relevance scorer holds no `httpx.AsyncClient` (sync `httpx` per call) and is simply replaced in place.

## Privacy

Sensitive content is auto-detected and never sent to external LLM compression:

| Pattern | Example |
|---------|---------|
| API keys / tokens | `api_key=...`, `sk-xxxx`, `ghp_xxxx`, `xoxb-...` |
| Passwords | `password=...`, `passwd: ...`, `pwd=...` |
| Email addresses | `user@example.com` |
| Private keys | `BEGIN RSA PRIVATE KEY`, `BEGIN EC PRIVATE KEY`, `BEGIN OPENSSH PRIVATE KEY` |

Detection scans the first 10K characters. When sensitive content is found, LLM compression falls back to local truncation.

## Horizontal Scaling

By default, `SelectiveCompressor` stores pending TOC selections in memory. For multi-instance deployments, switch to SQLite-backed storage so instances share state:

```mermaid
flowchart TB
    AgentA["Agent A"] --> InstA["STM instance A"]
    AgentB["Agent B"] --> InstB["STM instance B"]
    AgentC["Agent C"] --> InstC["STM instance C"]
    InstA <--> Store
    InstB <--> Store
    InstC <--> Store
    Store[("SQLitePendingStore<br/>~/.memtomem/pending_selections.db<br/>(WAL mode)")]
    Store -.->|"selective TOC<br/>shared across instances"| All["agent A creates TOC,<br/>agent B can resolve it"]
```


```json
{
  "upstream_servers": {
    "filesystem": {
      "selective": {
        "pending_store": "sqlite",
        "pending_store_path": "~/.memtomem/pending_selections.db"
      }
    }
  }
}
```

| Backend | Config value | Use case |
|---------|-------------|----------|
| `memory` (default) | In-process dict + deque | Single instance, zero overhead |
| `sqlite` | SQLite with WAL mode | Multiple instances sharing TOC state |

With `sqlite`, instance A can create a TOC and instance B can `stm_proxy_select_chunks` to retrieve sections from that TOC.

## Observability

### Metrics

Token savings, error rates, and latency tracked per server and tool. Example output of `stm_proxy_stats`:

```
STM Proxy Stats
===============
Total calls:     247
Original chars:  1234567
Compressed:      345678
Savings:         72.0%
Cache hits:      89
Cache misses:    158

By server:
  filesystem: 142 calls, 800000 → 200000 chars (75.0% saved)
  github: 105 calls, 434567 → 145678 chars (66.6% saved)

Surfacing: enabled (min_score=0.02)
```

- **Error classification** — errors are categorized as `transport`, `timeout`, `protocol`, `upstream_error`, `programming`, or `internal_error`. The `internal_error` category covers exceptions raised inside the CLEAN/COMPRESS/SURFACE/INDEX pipeline after the upstream call already returned (rare in practice — most strategies fall back internally — but recording these guarantees no failed call is invisible). Each failed call records the category and code for debugging.
- **Trace IDs** — every proxy call generates a unique `trace_id` (16-char hex) for correlating logs and metrics. Bench fixtures instead emit a deterministic `bench-<sha256[:16]>` prefix so operators can exclude them from production aggregates (`WHERE trace_id NOT LIKE 'bench-%'`); see [bench_qa.md → Operator trace filter](bench_qa.md#operator-trace-filter).

Metrics are persisted to SQLite (`~/.memtomem/proxy_metrics.db`, max 10K entries) with error category and trace_id columns.

### Logging

STM uses Python's `logging` module.  Format:

```
LEVEL module_name: message
```

Set the level via environment variable:

```bash
export MEMTOMEM_STM_LOG_LEVEL=DEBUG
```

Default is `WARNING`.  Read once at startup by `server.py:main()`.

| Level | What it shows |
|-------|---------------|
| `DEBUG` | Pipeline tracing, strategy selection, cache decisions |
| `INFO` | Surfacing injections, config reloads, extraction completions |
| `WARNING` | Failure guards (F1/S1), fallback activations, skips |
| `ERROR` | Unrecoverable failures (rare — most errors fall back) |

See [Configuration → General](configuration.md#general) for the env
var reference.

### Langfuse Tracing (optional)

```bash
pip install "memtomem-stm[langfuse]"
# or with uv:
uv pip install "memtomem-stm[langfuse]"

export MEMTOMEM_STM_LANGFUSE__ENABLED=true
export MEMTOMEM_STM_LANGFUSE__PUBLIC_KEY=pk-lf-...
export MEMTOMEM_STM_LANGFUSE__SECRET_KEY=sk-lf-...
export MEMTOMEM_STM_LANGFUSE__HOST=https://cloud.langfuse.com   # or http://localhost:3000 for self-hosted
```

**What gets traced.** Every proxy tool invocation is wrapped in a top-level Langfuse observation called **`proxy_call`** with nested sub-spans for each pipeline stage:

| Span name | When | Metadata |
|---|---|---|
| `proxy_call` | Every proxy invocation | `server`, `tool`, `trace_id` |
| `proxy_call_clean` | Stage 1 (content cleaning) | `server`, `tool` |
| `proxy_call_compress` | Stage 2 (compression) | `server`, `tool`, `strategy`, `max_chars` |
| `proxy_call_surface` | Stage 3 (memory injection) | `server`, `tool` |
| `proxy_call_index` | Stage 4 (auto-indexing) | `server`, `tool` |
| `proxy_call_cache_hit` | Cache hit (replaces stages 1-2) | `server`, `tool` |
| `stm_surfacing_feedback` | Surfacing feedback tool call | `surfacing_id`, `rating`, `memory_id` |
| `stm_surfacing_stats` | Surfacing stats query | `tool` |
| `surfacing_feedback_boost` | Access count boost (sub-span of feedback) | `surfacing_id`, `chunk_count` |

The `trace_id` (16-char hex) matches `proxy_metrics.db.trace_id` — join on this to correlate a Langfuse span with its SQLite metrics row. Span durations are auto-recorded from the `with` block. Errors propagate through spans — a failed upstream call shows up with an exception attached.

**Trace context propagation.** When forwarding calls to upstream MCP servers, STM includes a `_trace_id` reserved field in the tool arguments. Upstream servers can extract this to correlate their own spans with the originating STM trace. The same `_trace_id` field is available for `McpClientSearchAdapter.search()`, `increment_access()`, and `scratch_list()` calls to the LTM server.

**Sampling.** For high-throughput deployments, set a sampling rate to reduce Langfuse ingest volume:

```bash
export MEMTOMEM_STM_LANGFUSE__SAMPLING_RATE=0.1  # trace 10% of calls
```

Default is `1.0` (trace all). When a call is sampled out, all sub-spans are skipped. Metrics recording (`proxy_metrics.db`) is never affected by sampling — it always runs.

**Why this is the recommended observability UI.** memtomem-stm intentionally does not ship an in-repo web dashboard — the MCP tools (`stm_proxy_stats`, `stm_surfacing_stats`, `stm_proxy_health`), SQLite metrics (`proxy_metrics.db`, `stm_feedback.db`), and Langfuse together cover the observability surface without duplication. For team deployments that want a shared UI, point every instance at the same Langfuse project. When reporting issues, include the `trace_id` from `stm_proxy_stats` output so it can be located in Langfuse immediately.

**Graceful degradation.** If the `langfuse` optional extra is not installed, or if `MEMTOMEM_STM_LANGFUSE__ENABLED=false` (the default), the trace wrapper collapses to a `nullcontext` — zero overhead, no log spam, no behavior change.

## Data Storage

| File | Purpose | Managed by |
|------|---------|------------|
| `~/.memtomem/stm_proxy.json` | Upstream server config (hot-reloaded) | `mms` CLI |
| `~/.memtomem/proxy_cache.db` | Response cache (SQLite, WAL mode) | ProxyCache |
| `~/.memtomem/proxy_metrics.db` | Compression metrics history | MetricsStore |
| `~/.memtomem/stm_feedback.db` | Surfacing events, feedback ratings & compression feedback | FeedbackStore, CompressionFeedbackStore |
| `~/.memtomem/pending_selections.db` | Shared pending TOC state (horizontal scaling) | SQLitePendingStore |
| `~/.memtomem/proxy_index/*.md` | Auto-indexed responses | auto-index pipeline |

```mermaid
erDiagram
    surfacing_events ||--o{ surfacing_feedback : "0..n ratings"

    surfacing_events {
        string id PK "surfacing_id"
        string server
        string tool
        string query
        string memory_ids "JSON array of memory IDs"
        string scores "JSON array of floats"
        real created_at
    }
    surfacing_feedback {
        int id PK "AUTOINCREMENT"
        string surfacing_id FK
        string memory_id "nullable"
        string rating "helpful / not_relevant / already_known"
        real created_at
    }
    seen_memories {
        string memory_id PK
        real first_seen_at
        real last_seen_at
        int seen_count "DEFAULT 1"
    }
    compression_feedback {
        int id PK "AUTOINCREMENT"
        string server
        string tool
        string trace_id "nullable"
        string kind "truncated / missing_example / missing_metadata / wrong_topic / other"
        string missing
        real created_at
    }

    proxy_metrics {
        int id PK "AUTOINCREMENT"
        string server
        string tool
        int original_chars
        int compressed_chars
        int cleaned_chars "DEFAULT 0"
        int is_error "DEFAULT 0"
        string error_category "nullable"
        int error_code "nullable"
        string trace_id "nullable"
        string compression_strategy "nullable"
        int ratio_violation "DEFAULT 0"
        int scorer_fallback "DEFAULT 0"
        real created_at
    }
    pending_selections {
        string key PK
        string chunks_json
        string format
        real created_at
        int total_chars
    }
```

The `surfacing_events`, `surfacing_feedback`, `seen_memories`, and `compression_feedback` tables live in `stm_feedback.db`. `proxy_metrics` lives in `proxy_metrics.db`. `pending_selections` lives in `pending_selections.db` only when the SQLite-backed `PendingStore` is enabled.
