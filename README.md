# memtomem-stm

Short-term memory proxy gateway with **proactive memory surfacing** for AI agents.

Sits between your AI agent and upstream MCP servers. Compresses responses to save tokens, caches results, and automatically surfaces relevant memories from a memtomem LTM server.

**Built for:**
- Agents (Claude Code, Cursor, Claude Desktop, etc.) running multiple MCP servers and burning tokens on noisy upstream responses
- Long-running coding sessions where the agent should *recall* prior decisions instead of re-searching
- Teams running custom MCP servers that need a proxy layer for compression, caching, and observability — no upstream code changes required

```
Agent (Claude Code, Cursor, etc.)
    │
    ▼
┌──────────────────────────────────────┐
│        memtomem-stm (STM)            │
│  CLEAN → COMPRESS → SURFACE → INDEX  │
└──────────┬───────────────────────────┘
           │ stdio / SSE / HTTP
     ┌─────┴──────┐
     ▼            ▼
 [filesystem]  [github]
  MCP server    MCP server
```

## Installation

```bash
pip install memtomem-stm
```

memtomem-stm is **independent**: it has no Python-level dependency on memtomem core. To enable proactive memory surfacing, point STM at a running memtomem MCP server (or any compatible MCP server) — communication happens entirely through the MCP protocol.

## Quick Start

### 1. Add an upstream MCP server

```bash
memtomem-stm-proxy add filesystem \
  --command npx \
  --args "-y @modelcontextprotocol/server-filesystem /home/user/projects" \
  --prefix fs
```

`--prefix` is required: it's the namespace under which the upstream server's tools will appear (e.g. `fs__read_file`). Repeat for each MCP server you want to proxy.

```bash
memtomem-stm-proxy list      # show what you've added
memtomem-stm-proxy status    # show full config + connectivity
```

### 2. Connect your AI client to STM

Point your MCP client at the `memtomem-stm` server instead of the upstream servers directly. For Claude Code:

```bash
claude mcp add memtomem-stm -s user -- memtomem-stm
```

Or add it to a JSON MCP config:

```json
{
  "mcpServers": {
    "memtomem-stm": {
      "command": "memtomem-stm"
    }
  }
}
```

### 3. Use the proxied tools

Your agent now sees proxied tools (`fs__read_file`, `gh__search_repositories`, etc.). Every call goes through the 4-stage pipeline automatically — responses are cleaned, compressed, cached, and (when an LTM server is configured) enriched with relevant memories.

To check what's happening, ask the agent to call `stm_proxy_stats`.

## Key Features

- 🗜️ **10 compression strategies** with auto-selection by content type, query-aware budget allocation, and zero-loss progressive delivery → [docs/compression.md](docs/compression.md)
- 🧠 **Proactive memory surfacing** from a memtomem LTM server, gated by relevance threshold, rate limit, dedup, and circuit breaker → [docs/surfacing.md](docs/surfacing.md)
- 💾 **Response caching** with TTL and eviction; surfacing re-applied on cache hit so injected memories stay fresh → [docs/caching.md](docs/caching.md)
- 🔍 **Observability** — Langfuse tracing, RPS, latency percentiles (p50/p95/p99), error classification, per-tool metrics → [docs/operations.md#observability](docs/operations.md#observability)
- 📈 **Horizontal scaling** — `PendingStore` protocol with InMemory (default) or SQLite-shared backend for multi-instance deployments → [docs/operations.md#horizontal-scaling](docs/operations.md#horizontal-scaling)
- 🛡️ **Safety** — circuit breaker, retry with backoff, write-tool skip, query cooldown, session/cross-session dedup, sensitive content auto-detection → [docs/operations.md#safety--resilience](docs/operations.md#safety--resilience)

## Documentation

| Guide | Topic |
|-------|-------|
| [Pipeline](docs/pipeline.md) | The 4-stage CLEAN → COMPRESS → SURFACE → INDEX flow |
| [Compression](docs/compression.md) | All 10 strategies, query-aware compression, progressive delivery, model-aware defaults |
| [Surfacing](docs/surfacing.md) | Memory surfacing engine, relevance gating, feedback loop, auto-tuning |
| [Caching](docs/caching.md) | Response cache and auto-indexing |
| [Configuration](docs/configuration.md) | Environment variables and `stm_proxy.json` reference |
| [CLI](docs/cli.md) | `memtomem-stm-proxy` commands and the 6 MCP tools |
| [Operations](docs/operations.md) | Safety, privacy, horizontal scaling, observability, on-disk state |
| [Testing](docs/testing.md) | Test layout and how to run them |

## License

[Apache License 2.0](LICENSE)
