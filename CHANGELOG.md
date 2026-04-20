# Changelog

All notable changes will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)

## [Unreleased]

## [0.1.12] — 2026-04-20

### Added

- **`stm_progressive_stats` MCP tool + `progressive_reads` telemetry table** (#213, closes #204 PR #2) — every progressive initial chunk and every `stm_proxy_read_more` follow-up persists one row (`key, trace_id, server, tool, offset, chars, served_to, total_chars, created_at`) in `~/.memtomem/stm_feedback.db`. Aggregates surface as total reads, distinct responses, follow-up rate, avg chars served, avg total chars, avg coverage, and a per-tool breakdown — parity with `stm_compression_stats` / `stm_surfacing_stats`. Writes are fire-and-forget (tracker swallows exceptions) so telemetry outages cannot affect response delivery. Past-end `read_more` calls that return the `"(no more content)"` sentinel are deliberately skipped so they don't inflate follow-up rate. Opt out via `proxy.progressive_reads.enabled=false`. Unblocks stratified analysis of nudge strength vs. follow-up rate across compression strategies.
- **`trace_id` threaded through progressive delivery** (#205, closes #204 PR #1) — `_apply_progressive` / `ProgressiveResponse` / `stm_proxy_read_more` now carry the originating call's `trace_id`, so the follow-up span is filterable in Langfuse as a cohort with the initial `proxy_call` turn. The correlation is metadata-tag based (both spans carry the same `trace_id` attribute) rather than trace-tree merging — the two MCP turns run in separate OTel contexts, so `proxy_call_read_more` stays a root span. Closes the one call-pipeline path that Langfuse could not correlate to its originating call.
- **Upstream `call_timeout_seconds` + `overall_deadline_seconds` for proxy calls** (#206) — every upstream MCP tool invocation is now wrapped in a configurable per-call timeout, and the outer compression/surfacing pipeline honors a total-call deadline so a slow upstream cannot hang the agent past the budget. Timeouts are recorded as `ErrorCategory.TIMEOUT` in `proxy_metrics`. Defaults err on the lenient side so existing deployments don't regress; tighten per-upstream as you dial in.
- **Timeout-bound LLM compression stage** (#207, #210) — the `LLM_SUMMARY` compressor now respects `compression.llm.llm_timeout_seconds` (default 60s). A slow or hung LLM endpoint would previously freeze the pipeline *after* the upstream had already responded — outside the upstream `call_timeout_seconds` introduced in #206. On timeout the compressor falls back to `TruncateCompressor`, matching the existing LLM failure modes (privacy / circuit breaker / llm_error).
- **Exception barrier around `mcp.run()`** (#209 Part A, #212) — wraps the top-level FastMCP event loop so an unhandled exception in a tool handler or transport callback logs + exits cleanly instead of leaving the stdio subprocess in a half-dead state. The earlier failure mode (connection reset, broken pipe on shutdown) is now a loud terminal log with a traceback. Part B (periodic ping) remains wait-for-signal.
- **`MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=false` hides STM's observability MCP tools** (#201) — set the env var to drop the seven operator-facing tools (`stm_proxy_stats`, `stm_proxy_health`, `stm_proxy_cache_clear`, `stm_surfacing_stats`, `stm_compression_stats`, `stm_progressive_stats`, `stm_tuning_recommendations`) from the MCP `tools/list` surface, reducing upfront schema tokens on eager-loading MCP clients (e.g. OpenAI Codex CLI) that don't lazy-load tool schemas the way Claude Code does. The hidden tools remain fully callable via the `mms` CLI; only the MCP advertisement is suppressed. The four model-facing tools (`stm_proxy_read_more`, `stm_proxy_select_chunks`, `stm_surfacing_feedback`, `stm_compression_feedback`) stay advertised regardless. Default `true` preserves existing behavior — opt in only if your client eager-loads. Env var only in this release; matching `STMConfig.advertise_observability_tools` field is present for type-checking but JSON-file configuration would require a registration refactor and is deferred until there's demand.
- **`mms add --from-clients` (alias `--import`) bulk-imports from MCP clients post-init** (#200) — reuses init's discovery + TUI flow so additional servers added to Claude Desktop / Code / project `.mcp.json` after initial setup can be pulled in interactively, without editing JSON by hand or calling `mms add` once per server. Filters candidates two ways before prompting: by name (skips `foo` if a server named `foo` already exists) and by `(transport, command, args)` / `(transport, url)` signature (skips duplicates registered under a different name). When all discovered servers are already registered, exits cleanly with a no-op message instead of an empty selection screen. `--prefix` is suggested from the upstream name and de-duped against prefixes already in the config. Incompatible with `NAME` / `--prefix` / `--command` / `--args` / `--url` / `--env` — those are for the single-server manual path; passing both raises a usage error rather than silently ignoring one. Works with `--validate` and `--timeout` to probe only the selected subset.

### Changed

- **Bounded lock acquisition helper** (#208, #211) — internal refactor of `ProxyManager`'s async locks (selective compressor, LLM compressor, extractor) behind a `bounded_lock()` helper with a configurable `lock_timeout_seconds` (default 30s). Timeout raises `LockTimeoutError` → recorded as `ErrorCategory.LOCK_TIMEOUT` in `proxy_metrics`, distinct from upstream `TIMEOUT` (a stuck lock indicates an internal bug, not a slow dependency). No external API change; deployments that never saw a lock-holding bug see no difference.

### Fixed

- **`mms add --import` dual-registration warning** (#202, #203) — when a candidate is already registered under a different name via the `(transport, command, args)` / `(transport, url)` signature check, the import now emits a clear WARNING naming both entries rather than silently skipping. Read-only w.r.t. source-client config (STM discovery never writes back); the warning is a hint for the operator to prune manually if desired.

## [0.1.11] — 2026-04-20

### Added

- **`stm_surfacing_stats` MCP tool enriched with parity to `stm_compression_stats`** (#198, closes #197) — output now includes `events_total`, `distinct_tools`, `date_range`, per-tool breakdown (events + average memory count, sorted descending), `rating_distribution`, `total_feedback`, helpfulness percentage, and a DESC-ordered `recent` tail with 80-character query previews. New optional inputs `since` (ISO-8601) and `limit` (default 10) restrict the window. Empty-DB / out-of-range filters return zeros with all collections present, so callers don't branch on shape. Malformed `since` is rejected with a clean error rather than raising. Closes the long-standing observability gap where surfacing analytics required raw SQL against `~/.memtomem/stm_feedback.db` while compression already had an aggregate tool.

## [0.1.10] — 2026-04-20

### Added

- **`mms init` imports MCP servers from existing clients** (#194) — scans `./.mcp.json`, `~/.claude.json` (user + per-project scope), and Claude Desktop's macOS config, then offers a TUI multi-select (Enter toggles, scroll to Confirm; ↑↓ / j/k / Ctrl+N/P all supported). For each pick the user only confirms a prefix — transport/command/args/url/env are imported as-is. Self-reference filter blocks `mms` / `memtomem-stm` / `memtomem` / `memtomem-server` entries (including `uvx --from memtomem …` shape) so users can't accidentally proxy STM through itself or double-register the LTM companion. Dangerous env keys (`LD_PRELOAD`, `NODE_OPTIONS`, etc.) are stripped during import, matching `mms add --env` policy. Non-TTY / `MMS_NO_TUI=1` / piped stdin fall back to a comma-number prompt so CI and scripted installs still work. Adds `questionary>=2.0` runtime dep.
- **`mms init` surfaces `--config` management hints on non-default paths** (#195) — after saving to a path other than `~/.memtomem/stm_proxy.json`, the output now prints `mms list --config <path>` / `mms health --config <path>` so subsequent management commands don't silently read the empty default config. Reported during dogfooding with throwaway `/tmp/*.json` test paths.

## [0.1.9] — 2026-04-19

### Added

- **Background auto-indexing** (F4) — `auto_index.background` (default `false`). When set `true`, Stage 4 INDEX runs via `asyncio.create_task` off the request path; the agent receives a `[Indexing…] · scheduled` placeholder footer immediately while indexing proceeds in the background. Trade-off: read-your-own-writes consistency is no longer guaranteed until the task completes — opt in only if agents tolerate the gap. Metrics row records `index_ok IS NULL` / `index_error IS NULL` / `chunks_indexed = 0` (tri-state matching background extraction); dashboards filter background rows with `WHERE index_ok IS NULL`. Default `false` preserves the synchronous contract for every existing deployment.
- **`PROGRESSIVE_FOOTER_TOKEN` — canonical split token for progressive chunks** (issue #160). Exported from `memtomem_stm.proxy.progressive` as the exact prefix (`"\n---\n[progressive: chars="`) that agents stitching sequential `stm_proxy_read_more` responses should split on, instead of the weaker `"\n---\n"`. The `[progressive: chars=` suffix is a sentinel that does not appear in natural prose; splitting on the three-char delimiter alone silently drops bytes when content contains markdown horizontal rules, YAML frontmatter fences, or other `---` sequences. Non-breaking: the footer wire format is unchanged. Regression tests cover markdown HR, YAML frontmatter, `\n---\n[` lookalike brackets, and content that ends in `\n---\n` immediately before the footer; an additional test pins the exact legacy failure mode so a future refactor cannot silently regress to `split("\n---\n")[0]`. Agent-side contract documented in `docs/pipeline.md` § Stage 3.
- **CLI: `mms version` + `mms status --json`** (#152) — dedicated version subcommand and scriptable JSON status output for tooling / CI.
- **CLI: `mms health`** (#155) — per-upstream MCP connectivity checks with actionable diagnostics.
- **CLI: `mms init` + `mms add --validate`** (#157) — first-time setup workflow (scaffold config, validate upstream on add).
- **CLI: colorized output** with `NO_COLOR` honored (#158).
- **INDEX / EXTRACT pipeline outcome metrics** (#159) — per-call success/failure surfaced alongside existing CLEAN/COMPRESS/SURFACE columns.
- **Optional deterministic `trace_id` on `call_tool`** (#173) — opt-in kwarg for reproducible traces (bench harnesses, golden tests).
- **Parent LTM hints forwarded to operator observability** (#191) — upstream hint payload exposed on surfacing spans for diagnostics (operator-only; downstream prepend text unchanged).

### Changed

- **Progressive delivery surfaces memories for users who opt in via `injection_mode`** (F6). The default `injection_mode` stays `prepend`, which **continues to bypass surfacing on progressive** (upgrading is a no-op for default deployments). Operators who set `injection_mode` to `append` or `section` now get Stage 3 (SURFACE) on progressive responses; `prepend` would shift `stm_proxy_read_more` offsets and stays skipped with a one-time WARNING. See `docs/pipeline.md` § Stage 3 and `tests/test_progressive.py::TestProgressiveContentIntegrity::test_concat_invariant_under_surfacing` for the empirical safety proof.
- `CallMetrics.surfacing_on_progressive_ok` / `surface_error` (schema-provisioned by v0.1.8) now populate on the progressive path: `True`/`False` when surfacing ran, `None` when skipped (non-progressive call, no engine, or `prepend` mode).

### Fixed

- **`metrics_store` read-path defensive lock** (#166) — three read methods wrap the write-path lock so a future move to `run_in_executor` cannot silently introduce torn reads.
- **CLI: reject non-dict JSON configs** with a clean error (#156); **duplicate prefix warning** now clearly states the operation proceeds (#154).
- **Notebook 05** — correct `mms add` invocation (#184), echo fixture refs, notebook 00 count (#181); **notebook builder reconciled** with post-commit direct edits from #150 (#182).
- **README CI pytest filter** aligned with workflow (#185); **pipeline / custom-integration** line references refreshed (#179); **bench trace prefix link** in `operations.md` (#183).

### Testing

- **1465 tests** (up from 1364).
- **`bench_qa` LLM-behavior harness** (#168-#178) — 10 scenarios (S1-S10) covering happy paths, fallback ladder (S1/S6/S8), progressive round-trip, selective TOC demotion (S7), surfacing recall@k smoke (S10), 40-turn chat skeleton (S5); deterministic `trace_id` two-run gate; LLM-as-judge advisory scoring (opt-in, `gpt-4.1-nano` default); self-test probes; CI advisory job with frozen JSON/Markdown reports. See `tests/bench/bench_qa/README.md` and `docs/bench_qa.md`.
- **Contract test for empty-structured JSON** from upstream (#190) — alpha-upstream loose pin; stable invariants asserted, new fields read via `data.get()`.

### Docs

- **README rewrite** — user-benefits framing + improved CLI help text (#153).
- **Alpha banner** above tagline (#188).
- **Docs restructure** — WIP/internal guides moved to private `memtomem-docs` repo to minimize beginner barrier (#186).
- **Notebooks slimmed to `01_quickstart` only** — 00 + 02-05 moved to private repo (#187).
- **`bench_qa` reference + scenario-adding guide** (#180).

### Internal

- **Remove unused `SelectiveCompressor` import** (#163).
- **Ignore local `.env` + `.mcp.json`** (#192).

## [0.1.8] — 2026-04-16

### Added

- **`MEMTOMEM_STM_LOG_LEVEL` env var** (#149) — proxy-wide log level control documented end-to-end.
- **`max_upstream_chars` OOM guard** (#118) — reject upstream text exceeding the configured cap before compression.
- **BM25 multilingual tokenizer** (#94) — Cyrillic, Arabic, Devanagari, Thai added alongside Latin/CJK.
- **Compression feedback lifecycle** — `stm_surfacing_feedback` invalidates cache on `not_relevant` / `already_known` (#148).

### Changed

- **Centralised SQLite PRAGMA tuning** (#96) across all long-lived stores (shared helper).
- **SurfacingCache → insertion-ordered FIFO eviction** (#95), matching `_boosted_event_ids` pattern.
- **Config precedence honoured end-to-end: env > file > defaults** (#106 / #116).
- **Constants refactored to module-level `_UPPER_SNAKE_CASE`** (#87); hot-path regex hoisted to module level (#112); `atomic_write_text` centralised (#121).

### Fixed

- **Concurrency / lifecycle audit** (15 PRs) — init-failure SQLite connection cleanup (#127, #141), config hot-reload mtime preservation on parse failure (#128), reconnect `conn_stack` unwind (#130), lifespan cleanup when init fails before yield (#131), `MCPClient.start()` context unwind (#129), `FeedbackTracker` degrade-not-crash on SQLite failure (#124), boost-guard race in `handle_feedback` (#133), `_surfaced_ids` dedup-claim race (#134), surfacing cache stampede (#137), proxy `call_tool` cache stampede (#139), `_background_tasks` drain loop (#135), `RelevanceGate` burst race (#138), `LLMCompressor.close()` drain of in-flight `compress()` (#140), `_trace_id` cache-key taint (#136), swallow `cache.set` failures to preserve response (#120), reconnect delay ordering validation (#132).
- **Upstream Phase 2 robustness** — tolerate spec-noncompliant `result.content=None` (#114) and `result.text=None` (#119); MCP adapter text-None guard + WAL journal growth cap (#145).
- **Pipeline failure guards (F1/S1)** — auto-index stage failures no longer kill the agent response; untracked `surfacing_id` no longer injected when `record_surfacing` fails; inner handlers now log with `exc_info=True` (#149 cross-link).
- **Config validation** — pydantic `Field` / `Literal` constraints prevent unsafe values (#109); reject empty `api_key` for openai/anthropic at load time (#123); atomic JSON write for `stm_proxy.json` via temp + `os.replace` (#115).
- **Privacy detection** — expanded default credential patterns + email TLD regex (#111).
- **Defensive parsing** — LLM provider response payloads (#126, #67/#80), embedding provider input-order preservation when `index` omitted (#68/#81), numeric parsing of untrusted external input (#66/#79), backward search room for small spans in `_find_boundary` (#69/#82).
- **Memory caps** — `TokenTracker` per-server/tool counters bounded (#70/#83); `_boosted_event_ids` FIFO eviction (#110/#113).
- **MCP client** — `asyncio.TimeoutError` added to transport errors (#52); configurable `session.initialize()` timeout (#53).
- **Metrics** — `INTERNAL_ERROR` row recorded when pipeline raises (#117); demoted expected-fallback warnings away from `exc_info` trace dumps (#102).
- **Surfacing / compressor lifecycle gaps** — consolidated fixes across store and compressor shutdown paths (#143).

### Testing

- **1364 tests** (up from 1033).
- New coverage — `CliRunner` for `cli/proxy.py` (#100), privacy invalid-regex / hot-reload / empty-patterns (#97), `SurfacingEngine` webhook exception / cancel (#98), `MCPClient` reconnect + version negotiation edges (#99), `auto_index` / `extract_and_store` / `format_fact_md` (#101), provider-aware `embedding_base_url` defaults (#54/#86), `RelevanceScorer` hot-reload (#62/#85), `LLMCompressor` singleton + close lifecycle (#61/#84), `CircuitBreaker` `time_until_reset` + backward-compat alias (#39), cleaning-stage injection detection.

### Docs

- **Full docs audit — 11 files + 2 notebooks** (#150) — line references, failure-guard cross-links, log-level documentation across operations / configuration / pipeline / surfacing.
- **Observability verification + custom integration guide** (#149).
- **Compression before/after examples** for strategy docstrings (#23/#125).
- **Drift catch-up** for PRs #54-#65 (#72/#105); auto-tune feedback direction clarified (#57/#104); `OPENAI_API_KEY` requirement documented for OpenAI embedding provider (#56/#103).

## [0.1.7] — 2026-04-13

### Added

- **Phase 2 `StructuredResultParser`** — structured JSON surfacing result format activated end-to-end (scaffolded in v0.1.6 behind `SurfacingConfig.result_format="structured"`).
- **Format negotiation via `mem_do(action="version")`** — surfacing engine negotiates compact vs. structured format with the upstream at start-up based on core server capability.

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
