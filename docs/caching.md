# Response Caching & Auto-Indexing

## Response Cache

Proxied tool responses are cached in SQLite to avoid repeated upstream calls:

```mermaid
sequenceDiagram
    autonumber
    actor Agent
    participant STM as memtomem-stm
    participant Cache as ProxyCache (SQLite)
    participant Up as upstream MCP
    participant LTM as memtomem LTM

    Agent->>STM: tool call (server, tool, args)
    STM->>Cache: lookup(SHA-256 of server:tool:args)
    alt cache miss
        Cache-->>STM: none
        STM->>Up: forward call
        Up-->>STM: raw response
        STM->>STM: CLEAN + COMPRESS
        STM->>Cache: store pre-surfacing payload (ttl)
    else cache hit
        Cache-->>STM: cached payload
    end
    STM->>LTM: surface (every call, even on hit)
    LTM-->>STM: relevant memories
    STM->>STM: inject memories
    STM-->>Agent: enriched response
```

The key insight: **the cache stores pre-surfacing content**. Surfacing runs on every cache hit so injected memories stay fresh even when the upstream payload was cached hours ago.

```json
{
  "cache": {
    "enabled": true,
    "db_path": "~/.memtomem/proxy_cache.db",
    "default_ttl_seconds": 3600,
    "max_entries": 10000
  }
}
```

Key details:

- Cache key = SHA-256 of `server:tool:args` (argument order independent)
- **Pre-surfacing content is cached** — surfacing is re-applied on cache hit, so memories stay fresh
- Expired entries are purged on startup; oldest entries evicted when `max_entries` is exceeded
- Clear cache via MCP tool: `stm_proxy_cache_clear(server="gh", tool="search_code")`
- TTL can be overridden per-tool via `tool_overrides`

## Auto-Indexing

When enabled, large tool responses are automatically saved to memtomem LTM for future retrieval:

```mermaid
flowchart LR
    Resp["compressed<br/>response"] --> Size{"original ≥<br/>min_chars?"}
    Size -->|no| Skip["skip"]
    Size -->|yes| FM["build markdown<br/>+ frontmatter"]
    FM --> Write["write to<br/>memory_dir/"]
    Write --> NS["namespace =<br/>'proxy-{server}'"]
    NS --> LTM[("memtomem LTM<br/>(MCP mem_add)")]
    LTM -.->|future calls| Search["surfacing search<br/>can find it"]
```


```json
{
  "auto_index": {
    "enabled": true,
    "min_chars": 2000,
    "memory_dir": "~/.memtomem/proxy_index",
    "namespace": "proxy-{server}"
  }
}
```

Each indexed response creates a markdown file with frontmatter:

```markdown
---
source: proxy/github/search_code
timestamp: 2026-04-05T12:00:00+00:00
compression: hybrid
original_chars: 50000
compressed_chars: 8000
---

# Proxy Response: github/search_code

- **Source**: `github/search_code(query="auth middleware")`
- **Original size**: 50000 chars

## Content

(compressed response content)
```

The namespace supports `{server}` and `{tool}` placeholders. Can be toggled per-server via `auto_index: true|false` in `UpstreamServerConfig`.
