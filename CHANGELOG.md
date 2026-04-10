# Changelog

All notable changes will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)

## [0.1.1] — 2026-04-10

### CLI
- `mms -h` short flag now works (previously only `--help`)
- `mms status` and `mms list` now show compression strategy and max_chars per server

### Fixes
- Resolve all mypy type errors across proxy and surfacing modules (assert guards for optional AsyncClient)

### Docs
- Add uv install options to README and Langfuse extra install sections
- Add CHANGELOG, CONTRIBUTING, and SECURITY files
- Sync LICENSE copyright and pyproject authors with parent memtomem repo

### Meta
- Correct pyproject Homepage/Repository URLs

## [0.1.0] — 2026-04-10

Initial open-source release.

### Proxy pipeline
- 4-stage pipeline: CLEAN → COMPRESS → SURFACE → INDEX
- MCP server entrypoint (`memtomem-stm`) and proxy CLI (`memtomem-stm-proxy` / `mms`)
- Transparent proxying for upstream MCP servers over stdio, SSE, and HTTP
- Per-upstream namespacing via `--prefix` (e.g. `fs__read_file`)

### Compression
- 10 strategies with auto-selection by content type
- Query-aware budget allocation (more tokens for query-relevant content)
- Zero-loss progressive delivery (full content on request via cache)
- Model-aware defaults

### Memory surfacing
- Proactive surfacing from a memtomem LTM server via MCP
- Relevance threshold gating (configurable)
- Rate limit + query cooldown
- Session and cross-session dedup
- Write-tool skip (no surfacing on mutations)
- Circuit breaker with retry + exponential backoff

### Caching
- Response cache with TTL and eviction
- Surfacing re-applied on cache hit (injected memories stay fresh)
- Auto-indexing of responses into LTM (when configured)

### Safety
- Sensitive content auto-detection (skip caching/indexing of responses with detected secrets)
- Circuit breaker per upstream
- Configurable write-tool skip list

### Observability
- Langfuse tracing (optional extra: `pip install "memtomem-stm[langfuse]"`)
- RPS, latency percentiles (p50/p95/p99), error classification, per-tool metrics
- `stm_proxy_stats` MCP tool for in-agent inspection

### Horizontal scaling
- `PendingStore` protocol with InMemory (default) and SQLite shared backends

### Testing
- 766 automated tests
- CI: GitHub Actions (lint, typecheck, test)

### Related projects
- [**memtomem**](https://github.com/memtomem/memtomem) — Long-term memory infrastructure. memtomem-stm surfaces memories from a running memtomem MCP server; the two communicate entirely through the MCP protocol with no shared Python dependency.
