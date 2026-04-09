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

## Privacy

Sensitive content is auto-detected and never sent to external LLM compression:

| Pattern | Example |
|---------|---------|
| API keys / tokens | `api_key=...`, `sk-xxxx`, `ghp_xxxx`, `xoxb-...` |
| Passwords | `password=...`, `passwd: ...` |
| Email addresses | `user@example.com` |
| Private keys | `BEGIN RSA PRIVATE KEY` |

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
Total calls:     247       Current RPS: 2.5
Original chars:  1,234,567
Compressed:      345,678
Savings:         72.0%
Token savings:   68.3%
Cache hits:      89
Cache misses:    158
Errors:          3 (1.2%)
  transport: 2, timeout: 1

Latency (ms):
  clean:    p50=0.1  p95=0.5  p99=1.2
  compress: p50=0.3  p95=2.1  p99=8.5
  surface:  p50=15   p95=120  p99=450
  total:    p50=16   p95=125  p99=460

Upstream Health:
  filesystem: connected (12 tools)
  github:     connected (8 tools)

By server:
  filesystem: 142 calls, 800K → 200K chars (75.0% saved)
  github: 105 calls, 434K → 145K chars (66.6% saved)
```

- **Error classification** — errors are categorized as `transport`, `timeout`, `protocol`, `upstream_error`, or `programming`. Each failed call records the category and code for debugging.
- **Trace IDs** — every proxy call generates a unique `trace_id` (16-char hex) for correlating logs and metrics.

Metrics are persisted to SQLite (`~/.memtomem/proxy_metrics.db`, max 10K entries) with error category and trace_id columns.

### Langfuse Tracing (optional)

```bash
pip install "memtomem-stm[langfuse]"

export MEMTOMEM_STM_LANGFUSE__ENABLED=true
export MEMTOMEM_STM_LANGFUSE__PUBLIC_KEY=pk-...
export MEMTOMEM_STM_LANGFUSE__SECRET_KEY=sk-...
export MEMTOMEM_STM_LANGFUSE__HOST=https://cloud.langfuse.com
```

Traces proxy calls for latency analysis and debugging.

## Data Storage

| File | Purpose | Managed by |
|------|---------|------------|
| `~/.memtomem/stm_proxy.json` | Upstream server config (hot-reloaded) | `mms` CLI |
| `~/.memtomem/proxy_cache.db` | Response cache (SQLite, WAL mode) | ProxyCache |
| `~/.memtomem/proxy_metrics.db` | Compression metrics history | MetricsStore |
| `~/.memtomem/stm_feedback.db` | Surfacing events & feedback ratings | FeedbackStore |
| `~/.memtomem/pending_selections.db` | Shared pending TOC state (horizontal scaling) | SQLitePendingStore |
| `~/.memtomem/proxy_index/*.md` | Auto-indexed responses | auto-index pipeline |
