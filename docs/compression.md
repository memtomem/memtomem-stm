# Compression Strategies

memtomem-stm has 10 compression strategies. The CLI's `--compression` flag exposes 5 of them (`auto`, `none`, `truncate`, `selective`, `hybrid`); the remaining five are selected via the config file. The default is `auto`, which lets `auto_select_strategy()` pick per response.

```mermaid
flowchart TD
    R["upstream response<br/>(after CLEAN)"] --> A{"strategy ==<br/>'auto'?"}
    A -->|no| Fixed["use configured<br/>strategy directly"]
    A -->|yes| J{"valid JSON?"}
    J -->|"list ≥ 20 items"| SP["schema_pruning"]
    J -->|"dict w/ arrays ≥ 20"| SP
    J -->|"dict w/ ≥ 3<br/>nested dicts/lists"| EF["extract_fields"]
    J -->|"other JSON"| Tr["truncate"]
    J -->|no| MD{"≥ 4 headings?"}
    MD -->|"+ HTTP methods<br/>(GET/POST/…)"| Sk["skeleton"]
    MD -->|"≥ 5 headings<br/>+ ≥ 5K chars"| H["hybrid"]
    MD -->|no| Code{"≥ 6 code fences<br/>+ ≥ 5K chars?"}
    Code -->|yes| H
    Code -->|no| Tr
    Fixed --> Out["compressed<br/>response"]
    SP --> Out
    EF --> Out
    Sk --> Out
    H --> Out
    Tr --> Out
```

> **Note**: `progressive`, `llm_summary`, and `selective` are **never** chosen by `auto` — they're opt-in only because they change the agent interaction pattern (progressive needs `stm_proxy_read_more`; selective needs `stm_proxy_select_chunks`; `llm_summary` adds external API latency).

| Strategy | Best for | Description |
|----------|----------|-------------|
| **auto** (default) | All responses | Content-aware: picks the best strategy per response based on content type |
| **hybrid** | Large structured docs | Preserves first ~5K chars + TOC for the remainder |
| **selective** | Large structured data | 2-phase: returns TOC only, then retrieve selected sections on demand |
| **truncate** | Simple limiting | Section-aware for markdown (minimum representation for ALL sections, then enriches by relevance); query-aware budget allocation when `_context_query` is provided |
| **extract_fields** | JSON configs | Preserves all top-level keys with nested structure + first values |
| **schema_pruning** | Large JSON arrays | Recursive pruning: first 2 + last 1 items sampled per array |
| **skeleton** | API docs | All headings + first content line per section |
| **progressive** | Large any-type content | Zero information loss: stores full content, delivers in chunks on demand via `stm_proxy_read_more` |
| **llm_summary** | High-value content | Calls an external LLM (OpenAI / Anthropic / Ollama) to summarize |
| **none** | Passthrough | No compression (cache only) |

## Selective Compression (2-phase)

**Phase 1** — STM parses the response into sections and returns a compact TOC:

```json
{
  "type": "toc",
  "selection_key": "abc123def456",
  "format": "json",
  "total_chars": 50000,
  "ttl_seconds_remaining": 300,
  "entries": [
    {"key": "README", "type": "heading", "size": 200, "preview": "..."},
    {"key": "src/main.py", "type": "heading", "size": 5000, "preview": "..."}
  ],
  "hint": "Call stm_proxy_select_chunks(key='abc123def456', sections=[...]) to retrieve."
}
```

The `ttl_seconds_remaining` field tells the agent how many seconds it has to retrieve sections before the stored content expires. Each call to `stm_proxy_select_chunks` resets the TTL.

The proxied tool description automatically includes a convention suffix (`| TOC response: use stm_proxy_select_chunks`) so the agent knows to expect a TOC and which tool to call.

**Phase 2** — Agent calls `stm_proxy_select_chunks` to retrieve only the sections it needs.

```mermaid
sequenceDiagram
    autonumber
    actor Agent
    participant STM as SelectiveCompressor
    participant Store as PendingStore

    Agent->>STM: tool call (large response)
    STM->>STM: parse into sections
    STM->>Store: save full sections (key=abc123, ttl=300s)
    STM-->>Agent: TOC only<br/>{key, entries[], hint}
    Note over Agent: agent decides which sections matter
    Agent->>STM: stm_proxy_select_chunks(key=abc123, sections=["src/main.py","README"])
    STM->>Store: fetch by key
    Store-->>STM: full sections
    STM-->>Agent: only the requested sections
    Note over Store: TTL expires → eviction
```

Auto-detects format: JSON dicts (parsed by keys), JSON arrays (parsed by index), Markdown (parsed by headings), plain text (parsed by paragraphs).

Pending selections are stored for 5 minutes (max 100 concurrent), then auto-evicted. For multi-instance deployments, switch to SQLite-backed pending storage — see [Operations → Horizontal Scaling](operations.md#horizontal-scaling).

## Hybrid Compression

Combines immediate access with selective retrieval:

```mermaid
flowchart TB
    subgraph Resp["hybrid response"]
        Head["HEAD<br/>(first 5000 chars)<br/><br/><b>immediately available</b>"]
        TOC["TOC of remaining content<br/>• Section A — 2K chars<br/>• Section B — 8K chars<br/>• Section C — 35K chars<br/>…"]
        Head --- TOC
    end
    Resp -->|agent reads| Quick["quick scan"]
    Resp -.->|"stm_proxy_select_chunks(<br/>key, sections=['B'])"| Detail["full Section B"]
```

Configurable per server:

```json
{
  "hybrid": {
    "head_chars": 5000,
    "tail_mode": "toc",
    "head_ratio": 0.6,
    "min_toc_budget": 200,
    "min_head_chars": 100
  }
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `head_chars` | 5000 | Target characters for the preserved head section |
| `tail_mode` | `"toc"` | How to compress the tail (`"toc"` or `"truncate"`) |
| `head_ratio` | 0.6 | Fraction of budget allocated to head when total budget is tight |
| `min_toc_budget` | 200 | Minimum characters reserved for the tail TOC/truncation |
| `min_head_chars` | 100 | Absolute minimum head size — if the budget can't fit this, falls back to truncate |

## Progressive Delivery (cursor-based)

Inspired by how Claude Code reads files progressively (150 lines at a time), progressive delivery stores the full cleaned content and delivers it in chunks on demand — **zero information loss**.

```mermaid
sequenceDiagram
    participant Agent
    participant STM as memtomem-stm
    participant Store as PendingStore

    Agent->>STM: tool call (large response)
    STM->>Store: store full content (key=abc123)
    STM-->>Agent: chunk 1 (0-4000) + footer<br/>{key, has_more, hint}
    Agent->>STM: stm_proxy_read_more(key=abc123, offset=4000)
    STM->>Store: fetch content
    STM-->>Agent: chunk 2 (4000-8000)
    Agent->>STM: stm_proxy_read_more(key=abc123, offset=8000)
    STM->>Store: fetch content
    STM-->>Agent: final chunk (has_more=false)
```

The first chunk includes a metadata footer with remaining headings/structure hints and a `ttl` field so the agent can decide whether to continue reading and how urgently:

```
---
[progressive: chars=0-4000/50000 | remaining=46000 | has_more=True | ttl=1800s]
[Remaining: "Configuration", "API Reference", ...]
[-> stm_proxy_read_more(key="abc123", offset=4000)]
```

Each call to `stm_proxy_read_more` resets the TTL. The `ttl` field is omitted on the last chunk (`has_more=False`).

| Feature | Selective | Progressive |
|---------|-----------|-------------|
| Access pattern | By name (random) | By offset (sequential) |
| Requires structure | Yes (headings/JSON keys) | No (any content) |
| Information loss | None (section-level) | None (full content) |
| Use case | "Show me the Config section" | "Read through this file" |

```json
{
  "compression": "progressive",
  "progressive": {
    "chunk_size": 4000,
    "max_stored": 200,
    "ttl_seconds": 1800,
    "include_structure_hint": true
  }
}
```

Progressive is **opt-in only** — `auto` strategy never selects it because it changes the agent interaction pattern (requires calling `stm_proxy_read_more`). When configured, the proxied tool description includes a convention suffix (`| Chunked: use stm_proxy_read_more for more`) so the agent knows to expect chunked delivery.

> **Note**: Memory surfacing (Stage 3) is **skipped** for progressive delivery responses. Injecting memories into the first chunk would shift character offsets for subsequent `stm_proxy_read_more` calls.

## Progressive Fallback Ladder

When the compression ratio guard detects that a strategy cut below the dynamic retention floor (`min_result_retention`), it uses a three-tier fallback ladder:

```mermaid
flowchart TD
    V{"ratio < floor?"} -->|no| OK["keep compressed result"]
    V -->|yes| S{"strategy ==<br/>selective?"}
    S -->|yes| Skip["no fallback<br/>(TOC is intentionally compact)"]
    S -->|no| Size{"content ><br/>chunk_size?"}
    Size -->|yes| T1["Tier 1: progressive<br/>(zero-loss, best-effort)"]
    Size -->|no| H{"≥ 3 headings?"}
    T1 -->|success| Done["progressive_fallback<br/>+ skip surfacing"]
    T1 -->|failure| H
    H -->|yes| T2["Tier 2: hybrid<br/>(structure-preserving)"]
    H -->|no| T3["Tier 3: truncate<br/>(guaranteed floor)"]
    T2 -->|"ratio ≥ floor"| Done2["hybrid_fallback"]
    T2 -->|"still below"| T3
    T3 --> Done3["truncate_fallback"]
```

**Tier 1 — Progressive (zero-loss)**: Stores the full cleaned content and returns the first chunk with `stm_proxy_read_more` instructions and TTL. The agent can retrieve remaining content on demand. Only attempted when content exceeds `chunk_size` (default 4000 chars) — smaller content fits in one chunk and progressive adds no value.

**Tier 2 — Hybrid (structure-preserving)**: Applies `HybridCompressor` (head + TOC) at the effective budget. Fires when content has ≥ 3 markdown headings but is too small for progressive chunking. Preserves document structure (head section + table of contents) instead of a blunt truncation. If the hybrid output still falls below the retention floor, falls through to Tier 3.

**Tier 3 — Truncate (guaranteed floor)**: Falls back to boundary-aware `TruncateCompressor` at the effective budget. This is lossy but immediate, and always succeeds. Fires when progressive and hybrid aren't applicable or fail.

The metrics `compression_strategy` field records the full transition path (e.g. `"hybrid→progressive_fallback"`, `"truncate→hybrid_fallback"`, or `"skeleton→truncate_fallback"`) so the three tiers can be audited independently via SQL.

### Per-tool retention floor

By default, the retention floor scales dynamically with response size (< 1KB → 90%, < 3KB → 75%, < 10KB → 65%, else → `min_result_retention`). You can override this per server or per tool:

```json
{
  "upstream_servers": {
    "docs": {
      "retention_floor": 0.5,
      "tool_overrides": {
        "get_page": { "retention_floor": 0.4 }
      }
    }
  }
}
```

The auto-tuner (`stm_tuning_recommendations`) can recommend `retention_floor` adjustments based on observed violation patterns.

## LLM Compression

Routes through an external LLM for intelligent summarization:

```json
{
  "llm": {
    "provider": "openai",
    "model": "gpt-4.1-mini",
    "api_key": "sk-...",
    "max_tokens": 500,
    "system_prompt": "Summarize concisely, preserving key information. Under {max_chars} chars."
  }
}
```

Providers: `openai`, `anthropic`, `ollama`. Falls back to truncation on API failure (circuit breaker protection — see [Operations → Safety & Resilience](operations.md#safety--resilience)).

Sensitive content (API keys, passwords, PII) is auto-detected and **never** sent to external LLMs — falls back to local truncation. See [Operations → Privacy](operations.md#privacy) for the patterns.

`LLMCompressor` holds a single `httpx.AsyncClient` for the life of the instance. `ProxyManager` caches one compressor per active `llm` config and swaps it (awaiting `close()` on the old one) whenever the config changes at runtime, so integrators generally do not need to manage it directly. If you construct an `LLMCompressor` standalone, `await compressor.close()` before discarding it to release the client — see [Operations → Shutdown & Lifecycle](operations.md#shutdown--lifecycle).

## Query-Aware Compression

When an agent provides `_context_query` in tool arguments, compression allocates budget proportionally to section relevance instead of fixed top-down order. This preserves more information from query-relevant sections.

```json
{
  "relevance_scorer": {
    "scorer": "bm25",
    "embedding_provider": "ollama",
    "embedding_model": "nomic-embed-text",
    "embedding_base_url": "http://localhost:11434",
    "embedding_timeout": 10.0
  }
}
```

| Scorer | Latency | Cross-language | Dependencies |
|--------|---------|----------------|--------------|
| `bm25` (default) | <1ms | No | None |
| `embedding` | 5-50ms | Yes | Ollama / OpenAI |

`RelevanceScorer` protocol (`proxy/relevance.py`) enables custom scorer implementations. `EmbeddingScorer` uses sync httpx to call embedding APIs with automatic BM25 fallback on error.

## Per-Server and Per-Tool Overrides

```json
{
  "upstream_servers": {
    "github": {
      "prefix": "gh",
      "compression": "hybrid",
      "max_result_chars": 16000,
      "tool_overrides": {
        "search_code": {
          "compression": "selective",
          "max_result_chars": 8000
        },
        "get_file_contents": {
          "compression": "none"
        }
      }
    }
  }
}
```

## Model-Aware Defaults

When `consumer_model` is set, STM automatically scales settings for the consuming model's context window. Set it once and the compression budget, surfacing injection size, and result count all adjust.

```bash
export MEMTOMEM_STM_PROXY__CONSUMER_MODEL=claude-sonnet-4
```

| Setting | SLM (≤32K) | Medium (32K-200K) | LLM (>200K) |
|---------|------------|-------------------|--------------|
| `max_result_chars` | ~5,600 | ~16,000 | ~35,000 |
| `max_injection_chars` | 1,500 | 3,000 | 5,000 |
| `max_results` (surfacing) | 2 | 3 | 5 |
| `context_window` | 0-1 | 1-2 | 2-5 |
| Compression strategy | skeleton / truncate | auto (default) | auto / none |

### Model Examples

| Model | Context | Tier | Notes |
|-------|---------|------|-------|
| `claude-opus-4` | 200K | Medium | Default settings work well |
| `claude-sonnet-4` | 200K | Medium | Default settings work well |
| `claude-haiku-4` | 200K | Medium | Default settings work well |
| `gpt-4.1` | 1M | LLM | Generous budget, more surfacing |
| `gpt-4.1-mini` | 1M | LLM | **Default LLM compression model**; generous budget |
| `gpt-4.1-nano` | 1M | LLM | Generous budget, more surfacing |
| `gpt-4o` | 128K | Medium | Default settings work well |
| `gpt-4o-mini` | 128K | Medium | Default settings work well |
| `o4-mini` | 200K | Medium | Reasoning model, default settings |
| `o3` / `o3-pro` / `o3-mini` | 200K | Medium | Reasoning models, default settings |
| `o1` / `o1-pro` | 200K | Medium | Reasoning models, default settings |
| `o1-mini` | 128K | Medium | Default settings work well |
| `gemini-2.5-pro` | 1M | LLM | Generous budget, more surfacing |
| `gemini-2.5-flash` | 1M | LLM | Generous budget, more surfacing |
| `llama-4-maverick` | 1M | LLM | Open-weight, generous budget |
| `llama-4-scout` | 512K | LLM | Open-weight, generous budget |
| `deepseek-r1` / `deepseek-v3` | 131K | Medium | Default settings work well |
| `qwen-3` | 131K | Medium | Default settings work well |
| `mistral-large` | 131K | Medium | Default settings work well |
| `codestral` | 262K | Medium | Code-focused model |
| `command-a` | 262K | Medium | Cohere, default settings |

Prefix-matched: `claude-sonnet-4-20250514` matches `claude-sonnet-4`. All scaling is automatic when `consumer_model` is set. Override any value explicitly to disable auto-scaling for that setting.
