# Changelog

All notable changes will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)

## [Unreleased]

### Changed

- **Progressive delivery now surfaces memories when `injection_mode` is `append` or `section`** (F6). Previously surfacing was skipped unconditionally on the progressive path to preserve the `stm_proxy_read_more` offset invariant. `prepend` mode still skips (with a one-time WARNING) because it would shift offsets. Default `injection_mode` is `prepend` so existing deployments see no behavior change until they opt in by switching to `append` or `section`. See `docs/pipeline.md` § Stage 3 and `tests/test_progressive.py::TestProgressiveContentIntegrity::test_concat_invariant_under_surfacing` for the empirical safety proof.
- `CallMetrics.surfacing_on_progressive_ok` / `surface_error` (schema-provisioned in 0.1.x) now populate on the progressive path: `True`/`False` when surfacing ran, `None` when skipped (non-progressive call, no engine, or `prepend` mode).

## [0.1.6] — 2026-04-13

### Observability

- **Langfuse spans for surfacing tools** — `stm_surfacing_feedback`, `stm_surfacing_stats`, and `surfacing_feedback_boost` (access count increment sub-span) are now wrapped in Langfuse observations
- **Upstream trace context propagation** — proxy forwards `_trace_id` as a reserved field in upstream MCP call arguments for end-to-end distributed tracing; `McpClientSearchAdapter` also accepts `trace_id` for LTM calls
- **End-to-end trace_id threading** — `SurfacingEngine.surface()` → `_do_surface()` → `mcp_adapter.search()`/`scratch_list()` now thread `trace_id` from the proxy call through the full surfacing pipeline

### Internal

- **Strategy-based parser for Phase 2** — `_parse_results` refactored into `CompactResultParser` / `StructuredResultParser` strategy classes with `get_parser()` factory; backward-compatible `_parse_results` static method delegates to compact parser. `SurfacingConfig.result_format` field added (`"compact"` default, `"structured"` reserved for Phase 2)
- **Extract `tool_metadata` and `memory_ops` from `manager.py`** — independent logic split into `proxy/tool_metadata.py` (62 LOC) and `proxy/memory_ops.py` (180 LOC); `manager.py` reduced from 1,333 → 1,179 LOC
- **Fix all ruff warnings** — 16 pre-existing lint issues in `tests/` cleaned up (F841, E741, F401)

### Testing

- 1033 automated tests (up from 975), +1 xfail for Phase 2 structured format
- New `test_proxy_manager_lifecycle.py` — 8 tests for start/stop/double-start guard
- New `test_proxy_manager_pipeline.py` — 15 tests for compression, surfacing, indexing, chunks, read_more
- New `test_server_tools.py` — 22 tests for all 10 MCP tool handlers + lifespan
- 7 observability tests for surfacing spans and trace propagation
- 7 parser strategy contract tests + 1 xfail Phase 2 snapshot

### Docs

- New notebook 05 — Observability and Langfuse Tracing (3 layers: MCP tools, SQLite, Langfuse)
- Updated operations.md span table with surfacing tool spans and trace propagation docs
- Updated notebook count in README and notebooks/README (4 → 6)

## [0.1.5] — 2026-04-12

### Critical

- **Fix `mem_search` response parser for real core format** — the `_parse_results()` regex expected a `--- [score] source ---` format that no real `memtomem-server` ever produced; core's actual compact output (`[rank] score | source > hierarchy`) was collapsed into a single garbage result with wrong score. Rewritten to match core's `_format_compact_result` output. The fake test server now emits the real format so integration tests validate the production parsing path.
- **Forward `context_window` to MCP call** — `context_window` configured in `SurfacingConfig` was silently swallowed by `**kwargs` and never sent to the core server. Now forwarded as an explicit parameter.

### Fixes

- Normalize `list[str]` namespace to comma-separated string before MCP call (core's `mem_search` accepts `str | None`; `NamespaceFilter.parse()` handles comma-separated values)
- Widen source-file regex from `.md`-only to any file extension
- Widen namespace badge regex to support hyphens, dots, and other non-word characters
- Fix mypy errors in `observability/tracing.py` — `Langfuse(**kwargs)` kwargs typed as `dict[str, Any]` instead of `dict[str, str]`

### Testing

- 975 automated tests (up from 963)
- New `test_core_format_contract.py` — 12 contract tests with snapshots of core's real formatter output (namespace badge, context window position, non-.md sources)
- Verified end-to-end against real `memtomem-server` v0.1.7

## [0.1.4] — 2026-04-12

### New Features

- **Compression ratio guard** (#20) — post-compression check detects when a strategy cuts below the dynamic retention floor; records `compression_strategy` and `ratio_violation` in `proxy_metrics.db`
- **Compression feedback tool** (#21) — `stm_compression_feedback` lets agents report information loss from compressed responses; `stm_compression_stats` shows aggregated feedback counts by kind and tool
- **Ratio guard fallback** (#22) — boundary-aware truncate fallback when compression overshoots the retention floor
- **3-tier fallback ladder** — progressive → hybrid → truncate; new hybrid tier preserves document structure (head + TOC) for content with ≥ 3 headings that is too small for progressive chunking
- **Per-tool `retention_floor`** — override the global dynamic retention scaling per server or per tool via `stm_proxy.json`
- **Compression auto-tuner** — `stm_tuning_recommendations` MCP tool analyses proxy metrics and produces per-tool recommendations (budget increase/decrease, strategy pinning, feedback-driven strategy changes)
- **Nested Langfuse sub-spans** — `proxy_call_clean`, `proxy_call_compress`, `proxy_call_surface`, `proxy_call_index`, `proxy_call_cache_hit` nested under the top-level `proxy_call` observation via OpenTelemetry context propagation
- **Langfuse sampling** — `MEMTOMEM_STM_LANGFUSE__SAMPLING_RATE` (0.0–1.0) to control tracing volume; metrics recording is never affected by sampling

### Improvements

- LLM fallback signal in `compression_strategy` metric (e.g. `llm_summary→privacy_fallback`)
- Embedding scorer fallback count exposed in `CallMetrics.scorer_fallback` + DB column
- SKELETON `body_trimmed_chars` metadata in compression footer
- Convention suffix in proxied tool descriptions for progressive/selective delivery
- Startup warning when `compression != none` but `auto_index` is disabled

### Docs

- 3-tier fallback ladder diagram in compression.md
- `retention_floor` config reference in configuration.md
- 7 → 10 MCP tools in cli.md (`stm_compression_feedback`, `stm_compression_stats`, `stm_tuning_recommendations`)
- Langfuse nested span table and sampling config in operations.md

### Testing

- 963 automated tests (up from 800)

## [0.1.3] — 2026-04-11

### Fixes

- **Drop phantom `memtomem` runtime dependency** — `pyproject.toml` declared `memtomem>=0.1,<0.2` as a runtime dep, but nothing in `src/` or `tests/` imports `memtomem`: the package talks to the LTM core exclusively through the MCP protocol, as documented in `README.md` and `CONTRIBUTING.md` and required by the invariant in `CLAUDE.md`. The stray entry silently pulled `memtomem` into every `pip install memtomem-stm`, putting the dependency graph at odds with all three documents. Runtime behavior is unchanged; only the dependency graph is cleaner now.

## [0.1.2] — 2026-04-10

### Critical

- **Fix server lifespan not connected** — `app_lifespan` was assigned to a non-existent `_lifespan_handler` attribute on FastMCP, making the entire server non-functional when run via CLI. Now passed via the `lifespan=` constructor kwarg.
- **Propagate upstream `isError` flag** — upstream tool errors were silently converted to success responses. Now raises `ToolError` so `isError=true` is preserved in the proxied response.

### Fixes

- Fix `_surfaced_ids` set pruning modifying a set during iteration (potential `RuntimeError` on CPython 3.12+); now uses snapshot via `itertools.islice`
- Fix `_parse_results` regex splitting on `---` inside content (YAML frontmatter, markdown horizontal rules); now requires score bracket `[N.N]` after separator
- Fix `_parse_scratch_list` truncating keys containing `: ` (e.g., `db: config`); now uses `rfind` heuristic anchored on trailing `...` marker
- Record metrics for non-text-only responses (images, embedded resources) instead of returning silently
- Pass original arguments (with `_context_query`) to surfacing on cache hit so the agent's query hint is preserved
- Snapshot config once per request in `_call_tool_inner` to prevent intra-request inconsistency from hot-reload
- Guard against double `start()` leaking connections by closing previous stack first
- Normalize `\r\n` and `\r` to `\n` in content cleaning before processing
- NFKC-normalize text before injection pattern matching to defeat Unicode confusable bypasses (Cyrillic, fullwidth)
- Add CJK sentence-end punctuation (`。！？`) to `_find_break` for better truncation points in East Asian text
- Widen UUID-based IDs from 12 hex (48 bits) to 16 hex (64 bits) to reduce collision probability
- Add `ProxyCache.stats()` thread-safety lock
- Wrap `_fastmcp_compat` private API access (`_tool_manager._tools`) in try/except for resilience against MCP SDK updates
- Move `feedback_tracker` creation inside `mcp_adapter` success guard so feedback endpoints are not activated when surfacing init fails

### Docs

- Add `stm_proxy_health` tool to cli.md tool table (was missing; tool count 6 → 7)
- Correct README tool count (6 → 7)
- Remove `selective` from auto-selection flowchart in compression.md (`auto_select_strategy` never returns SELECTIVE)
- Update Note to list `selective` alongside `progressive` and `llm_summary` as opt-in only

### Testing

- 800 automated tests (33 new in `test_qa_round3.py`)

## [0.1.1] — 2026-04-10

### CLI
- `mms -h` short flag now works (previously only `--help`)
- `mms status` and `mms list` now show compression strategy and max_chars per server
- Add `auto` to `--compression` choices and set it as the default (aligned with `ProxyConfig.default_compression`)
- Validate `--prefix` format (must start with a letter, no `__`) and warn on duplicate prefixes
- Require `--command` for stdio transport, `--url` for sse/streamable_http transport
- Support quoted paths with spaces in `--args` (via `shlex.split`)

### Fixes
- Resolve all mypy type errors across proxy and surfacing modules (assert guards for optional AsyncClient)
- Fix `cache.clear(tool=X)` without `server` silently wiping entire cache instead of filtering by tool
- Fix `proxy.enabled` field being ignored at runtime — server now skips upstream connections when disabled
- Fix non-deterministic ordering in feedback rating error message

### Docs
- Add uv install options to README and Langfuse extra install sections
- Add CHANGELOG, CONTRIBUTING, and SECURITY files
- Sync LICENSE copyright and pyproject authors with parent memtomem repo
- Align `stm_proxy_stats` example output in operations.md with actual code
- Document `[proxied]` tool description prefix and `{prefix}__{name}` naming convention
- Document that progressive delivery skips memory surfacing
- Clarify hot-reload scope (per-server settings only; adding/removing servers requires restart)
- Fix stale `min_result_retention` docstring (0.5 → 0.65)

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
- 767 automated tests
- CI: GitHub Actions (lint, typecheck, test)

### Related projects
- [**memtomem**](https://github.com/memtomem/memtomem) — Long-term memory infrastructure. memtomem-stm surfaces memories from a running memtomem MCP server; the two communicate entirely through the MCP protocol with no shared Python dependency.
