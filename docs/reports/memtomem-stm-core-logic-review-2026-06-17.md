# memtomem-stm Core Logic Review

Date: 2026-06-17
Revised: 2026-06-17 — corrections applied after an independent source-verification pass. Six improvement areas were re-checked against current code (Areas 3, 4, 5, and 6 over-stated work that is already shipped), one Strengths framing claim was tightened, two citation line numbers were corrected, and one missed robustness asymmetry was added as section 7.
Scope: Current checkout review of the core runtime paths: proxy call pipeline, compression/progressive delivery, surfacing/LTM integration, daemon hook path, persistence stores, configuration, exposure filtering, and observability. This review intentionally does not repeat the full 2026-06-10 source sweep; it re-checks the current implementation shape and summarizes improvement areas that matter most for the next hardening pass.

## Executive Summary

The current implementation is substantially stronger than the prior full sweep baseline. Several previously high-risk classes are now addressed in code: `_context_query` is normalized on the cache-miss path, surfacing cache hits re-apply durable feedback demotion, network LTM reconnects include `httpx.TransportError`, transient progressive/selective keys are blocked at the proxy cache store boundary, and AnyIO cancel-scope shutdown handling covers `ExceptionGroup`.

The main remaining risk is not a single obvious broken branch — though one concrete robustness asymmetry does exist in the progressive store path (see "7. Guard The Primary Progressive Store Call Like Its Fallback" below). The dominant theme is accumulated complexity in the central orchestration layer. `ProxyManager._call_tool_inner()` now owns upstream retry, content shaping, compression fallback, progressive delivery, surfacing, optional indexing/extraction, metrics, tracing, and cache interaction in one request path. The code has many local guards, but each new feature has to preserve several cross-stage invariants at once.

Recommended near-term focus:

1. Split the proxy call pipeline into explicit stage contracts.
2. Move persistence I/O behind async/off-loop facades before multi-client use grows.
3. Consume exceptions raised outside the background indexing/extraction coroutines, and add per-call `trace_id` correlation. (Aggregate outcomes are already first-class telemetry via `IndexObservability` / `stm_index_stats`; this is a narrower gap than the first draft implied.)
4. Add replay/golden tests for tool exposure and selection telemetry.
5. Tighten configuration failure behavior so one malformed override does not produce a broad runtime posture change.

## Current Strengths

### Stronger Stage Safety In The Proxy Path

`ProxyManager._call_tool_inner()` snapshots config once, strips `_context_query` before forwarding upstream, applies per-attempt and overall deadlines, caps oversized upstream text, preserves upstream `isError`, and records pipeline errors with trace IDs. The retry loop and post-failure reconnect behavior are now explicit enough for operators to separate timeout, transport, protocol, upstream, and proxy-internal failures.

Evidence:

- `src/memtomem_stm/proxy/manager.py:1940` starts the main call path and snapshots config.
- `src/memtomem_stm/proxy/manager.py:1960` normalizes `_context_query` and excludes it from upstream args.
- `src/memtomem_stm/proxy/manager.py:2003` runs the bounded retry loop.
- `src/memtomem_stm/proxy/manager.py:2144` applies the `max_upstream_chars` guard.

### Progressive And Cache Boundaries Are Better Defined

The proxy cache now rejects responses carrying transient pending-store keys, including progressive first chunks and selective/hybrid TOC selection keys. The detection predicate and the one-time legacy purge live in the cache module, so the rule is defined once and shared.

Caveat (corrected): the runtime store-time rejection is a caller-side guard in `ProxyManager` — a conditional at `manager.py:2723` that wraps `cache.set()` — not a check inside `ProxyCache.set()` itself. `ProxyCache.set()`'s only un-bypassable store chokepoint is the privacy / sensitive-content gate. There is exactly one `cache.set()` caller today, so the defense is effective in practice, but a future second caller would not inherit the transient-key guard automatically (it would inherit the privacy guard). Moving the transient-key check inside `ProxyCache.set()` would make the "callers cannot bypass it" property actually hold for this guard too.

Evidence:

- `src/memtomem_stm/proxy/cache.py:55` documents the transient-key cache invariant.
- `src/memtomem_stm/proxy/cache.py:76` implements the marker check (`response_carries_transient_key`).
- `src/memtomem_stm/proxy/cache.py:124` purges legacy rows carrying transient keys.
- `src/memtomem_stm/proxy/manager.py:2723` is the actual caller-side store gate (the only runtime call site of the marker check).

### Surfacing Has Better Failure Taxonomy

The LTM adapter returns distinct outcomes for no session, transport error, call error, empty content, and empty results. The engine records different skip labels, applies feedback demotion on miss and hit paths, and avoids feedback IDs when no durable event will be recorded.

Evidence:

- `src/memtomem_stm/surfacing/mcp_client.py:34` defines the search outcome vocabulary.
- `src/memtomem_stm/surfacing/mcp_client.py:372` includes `httpx.TransportError` in reconnect-worthy failures.
- `src/memtomem_stm/surfacing/engine.py:550` filters cached results through invalidation and durable demotion.
- `src/memtomem_stm/surfacing/engine.py:866` gates surfacing IDs on feedback event recording.

### Hook And Daemon Paths Are Operationally Conservative

The hook path now keeps compression and surfacing independent, bounds the daemon payload separately from compression, and offloads daemon auto-spawn to a worker thread. The daemon disables surfacing cache because a warm long-lived process needs session dedup to be authoritative.

Evidence:

- `src/memtomem_stm/cli/hook_cmd.py:505` offloads auto-spawn with `asyncio.to_thread`.
- `src/memtomem_stm/cli/hook_cmd.py:514` documents compression/surfacing separation.
- `src/memtomem_stm/daemon/server.py:247` configures daemon feedback/dedup behavior.
- `src/memtomem_stm/daemon/server.py:262` disables surfacing cache in daemon mode (`cache_ttl_seconds: 0.0`).

## Improvement Areas

### 1. Split `ProxyManager._call_tool_inner()` Into Explicit Stage Results

Priority: High

The main proxy call path is now too central. It carries too many stage-local invariants in local variables: `effective_compression`, `metrics_strategy`, `ratio_violation`, `progressive_fallback`, `surfacing_on_progressive_ok`, `index_ok`, `extract_ok`, `compressed_chars_for_metrics`, and more. This makes each small change risky because compression, surfacing, cache persistence, and metrics can drift from one another.

Recommended refactor:

- Introduce small stage result dataclasses, for example `UpstreamResult`, `CompressionResult`, `SurfacingResult`, `IndexResult`, and `PipelineMetrics`.
- Make transient response state explicit, e.g. `CompressionResult.cacheable: bool` instead of re-detecting transient markers later.
- Centralize final metrics assembly so stage implementations cannot accidentally disagree on char counts or strategy labels.
- Keep behavior identical first; do not combine this with feature work.

Primary code:

- `src/memtomem_stm/proxy/manager.py:1940`
- `src/memtomem_stm/proxy/manager.py:2245`
- `src/memtomem_stm/proxy/manager.py:2479`
- `src/memtomem_stm/proxy/manager.py:2522`

### 2. Move SQLite Stores Off The Event Loop Before Higher Concurrency

Priority: High for multi-client/server deployments; Medium for current local single-client use.

The cache and metrics stores use synchronous SQLite calls guarded by thread locks. The cache explicitly accepts this for low-volume local MCP usage, but the proxy is increasingly positioned as a gateway with multiple clients and background work. Under concurrent use, cache `get/set`, trim, metrics writes, feedback reads, and startup purges can add event-loop stalls that are hard to attribute from outside.

Recommended changes:

- Add async facades for `ProxyCache`, `MetricsStore`, and feedback-store hot reads using `asyncio.to_thread` or a bounded executor.
- Batch metrics writes where possible; avoid one commit per event on the hot path if traffic grows.
- Add a latency benchmark that simulates concurrent cache hit/miss plus metrics writes.
- Keep read-only CLI inspection paths read-only; the current `mode=ro` design is good.

Primary code:

- `src/memtomem_stm/proxy/cache.py:87`
- `src/memtomem_stm/proxy/cache.py:169`
- `src/memtomem_stm/proxy/cache.py:214`
- `src/memtomem_stm/proxy/metrics_store.py:154`
- `src/memtomem_stm/surfacing/feedback_store.py:1`

### 3. Make Background Indexing And Extraction Outcomes Observable

Priority: Medium (down from Medium-High after verification — the framing below was over-stated)

Background indexing/extraction protects request latency, which is correct. The first draft's premise that background outcomes are "mostly log-derived" and "weak for operations" is wrong: aggregate outcome telemetry already exists. `IndexObservability` (`src/memtomem_stm/proxy/index_observability.py`) is wired into both background coroutines (`manager.py:1404` for auto_index, `manager.py:1437` for extract) and records all outcome labels (success, error, privacy_skip, dedup, zero-fact) regardless of sync-vs-background, surfaced via the `stm_index_stats` control tool. So "are background writes failing in aggregate?" is already an operator-answerable question.

Two narrow gaps remain, and only the first is high-value:

1. Exceptions raised *outside* the coroutines' inner `try` — e.g. a `_get_extractor()` lock-timeout, `record_attempt`, the privacy pre-scan — are not caught by the in-coroutine handler. Both done-callbacks only do `set.discard` (`manager.py:2587` for index, `manager.py:2660` for extract); neither retrieves the task result, so such a failure surfaces only as a GC-time "task exception was never retrieved" warning. A shared `_on_background_task_done(stage, trace_id, server, tool)` callback that retrieves and records the exception closes this real swallow-gap.
2. The per-call `proxy_metrics.db` row leaves `index_ok` / `extract_ok` as `NULL` by design for background work (documented at `manager.py:2588-2590`), and there is no `trace_id` correlation between a request row and its eventual background outcome. Per-`trace_id` correlation and named `background_pending` / `background_failed` / `last_background_error` gauges are reasonable additive convenience, but lower value than the swallow-gap fix above — they should not be framed as closing an observability hole.

Recommended changes:

- Add the shared `_on_background_task_done(stage, trace_id, server, tool)` callback that retrieves and records task exceptions (primary win).
- Optionally add `trace_id` correlation and the `background_*` gauges as additive convenience, not as the core fix.
- Add a test for an exception raised *outside* the inner `auto_index_response()` / `extract_and_store()` handling (e.g. a lock-timeout in `_get_extractor()`), asserting the done-callback records it.

Primary code:

- `src/memtomem_stm/proxy/index_observability.py` (existing aggregate telemetry — already wired)
- `src/memtomem_stm/proxy/manager.py:1404` (auto_index observability wiring)
- `src/memtomem_stm/proxy/manager.py:1437` (extract observability wiring)
- `src/memtomem_stm/proxy/manager.py:2587` and `:2660` (index/extract done-callbacks — discard only, no exception retrieval)
- `src/memtomem_stm/proxy/manager.py:2588` (`index_ok` stays NULL on the background row by design)

### 4. Turn Tool Exposure And Selection Into Replayable Contracts

Priority: Medium

The exposure filter is thoughtfully designed: hard rejects cannot be resurrected by ranking, health is startup-stable, duplicate names are withheld as a group, and selection telemetry records the advertised candidate vocabulary. This is exactly the right direction, but the logic now depends on interactions among config profiles, metrics-derived health, external toolgraph verdicts, risk penalties, and ranking records.

Status (corrected): the golden wire-in tests and ranker-version pinning largely already exist. `tests/test_tool_eligibility.py:546` (`TestManagerWireIn`) and `tests/test_toolgraph_provider.py` already feed a fixed catalog / config / toolgraph verdict and assert the exact advertised set, `candidate_tools`, `reject_reasons`, `ranked_candidates`, and `ranker_version` (pinned to the `RANKER_VERSION_*` constants in `tool_relevance.py`). So sub-points 1 and 2 below are "consolidate + close one input gap", not "build from scratch."

Recommended changes:

- Consolidate the existing wire-in assertions into a single golden fixture, and close the one input not yet exercised end-to-end: metrics-history → health derivation via `compute_health_flags` (the e2e tests inject `unhealthy=frozenset(...)` directly; only a unit test covers the derivation).
- The genuinely missing piece: a standalone replay command that validates historical selection-log rows against the current parser/schema without requiring a live proxy. No such CLI exists today.

Primary code:

- `src/memtomem_stm/proxy/tool_eligibility.py:1`
- `src/memtomem_stm/proxy/tool_relevance.py:1` (the `RANKER_VERSION_*` constants)
- `src/memtomem_stm/proxy/toolgraph_provider.py:1`
- `src/memtomem_stm/proxy/manager.py:390`
- `tests/test_tool_eligibility.py:546` (existing `TestManagerWireIn` golden assertions)

### 5. Tighten Configuration Failure Semantics

Priority: Medium

The config loader now names implicated environment variables, which is a useful operator improvement. The broader behavior is still coarse: parse or validation failure returns `None` for file-backed loads, and missing env-only configs with malformed overrides degrade to defaults. That can be acceptable for fail-open local use, but it should be made explicit per context. A malformed override for a non-critical field should not accidentally put the proxy into a wider default posture.

Status (corrected): the hot-reload "keep last known-good" behavior is already implemented. `ProxyConfigLoader.get()` keeps `self._cached`, does not advance `_mtime`, and logs "Proxy config parse failed; keeping previous config" (`config.py:1085-1094`); a running proxy reading config via `_config_loader.get()` does not revert to defaults on a bad edit. The real exposure is therefore confined to the cold-start path, where `load_from_file` runs and one malformed field collapses the whole-config `model_validate` (`config.py:1042`), dropping all overrides — not the running-proxy hot-reload path.

Recommended changes:

- Scope the work to cold start: distinguish parse failure from validation failure in return types or diagnostics, so a single malformed non-critical field does not silently drop every override.
- For startup, consider a `strict_config` mode that exits on invalid file or env override instead of degrading to defaults.
- Add docs that distinguish "missing config", "invalid config", and "invalid env override".
- (Already shipped — no action needed: hot-reload last-known-good retention.)

Primary code:

- `src/memtomem_stm/proxy/config.py:64`
- `src/memtomem_stm/proxy/config.py:983` (`load_from_file`)
- `src/memtomem_stm/proxy/config.py:1010` (env-override application branch)
- `src/memtomem_stm/proxy/config.py:1042` (`model_validate` — whole-config validation)
- `src/memtomem_stm/proxy/config.py:1085` (hot-reload last-known-good retention — already shipped)

### 6. Keep AnyIO/MCP Lifecycle Handling In One Shared Pattern

Priority: Medium

The code now has a shared helper for clean AnyIO cancel-scope shutdown detection and the daemon has a child-process sweep when adapter stop fails. This is a good fix. The improvement is to prevent new lifecycle code from re-learning the same AnyIO task-affinity issue.

Status (corrected): the bare-and-nested `ExceptionGroup` injection tests already exist for both lifecycle entrypoints — `tests/test_server_tools.py:1822` / `:1834` cover the server `main()` path (bare, single-wrapped, and doubly-nested shapes) and `tests/daemon/test_server.py:508` covers daemon teardown. There are exactly two lifecycle entrypoints (`server.main`, daemon `_teardown`) and both are exercised. The real, unmet gap is that `anyio_shutdown.py` has no direct unit test of its own: the recursive `BaseExceptionGroup` walk in `is_clean_cancel_scope_shutdown` is only covered transitively.

Recommended changes:

- Add a focused unit test for `anyio_shutdown.is_clean_cancel_scope_shutdown` itself (bare RuntimeError, single-level group, nested group, and a group with one non-cancel leaf that must return False).
- Document a standard MCP-client lifecycle pattern: which task owns the `AsyncExitStack`, where stop must run, and how cancellation is classified.
- Prefer a dedicated long-lived adapter worker task for future MCP adapters that need lazy startup and cross-request reuse.

Primary code:

- `src/memtomem_stm/utils/anyio_shutdown.py:1` (no direct unit test today)
- `src/memtomem_stm/server.py:1668`
- `src/memtomem_stm/daemon/server.py:295`
- `src/memtomem_stm/daemon/server.py:314`
- `tests/test_server_tools.py:1822` / `tests/daemon/test_server.py:508` (existing `ExceptionGroup`-injection coverage)

### 7. Guard The Primary Progressive Store Call Like Its Fallback

Priority: Medium (correctness asymmetry; low frequency under the default backend)

The first draft missed a concrete branch-level asymmetry surfaced by a fresh-eyes pass. The primary PROGRESSIVE store call at `manager.py:2251` (`compressed = self._apply_progressive(...)`) is **not** wrapped in `try`/`except`, while the byte-identical ratio-guard fallback call at `manager.py:2367` **is** wrapped and degrades to hybrid/truncate on failure. Both ultimately reach `store.put()`.

Under the default `InMemoryPendingStore` (a pure dict insert) the primary call effectively cannot fail, so this is latent today. Under the SQLite-backed pending store (`pending_store.py`, real I/O), a transient error on the primary path escapes `_call_tool_inner`, is recorded as `INTERNAL_ERROR`, and discards an otherwise-successful upstream response — whereas the fallback path on the same failure would still return content. This is the one concrete code-level gap the first draft's "no single obvious broken branch" framing did not catch.

Recommended changes:

- Wrap the primary `_apply_progressive` call at `manager.py:2251` so a store-time failure degrades like its sibling at `manager.py:2367`; or
- Explicitly document that raising-and-discarding on a primary progressive store error is intended, and ensure the SQLite backend cannot raise on a normal put.
- The Area 1 stage-result refactor would surface this asymmetry naturally; pairing the two is reasonable.

Primary code:

- `src/memtomem_stm/proxy/manager.py:2251` (unguarded primary progressive store)
- `src/memtomem_stm/proxy/manager.py:2367` (guarded fallback progressive store)
- `src/memtomem_stm/proxy/pending_store.py` (SQLite backend that can perform real I/O)

## Suggested Execution Order

1. Pipeline stage-result refactor with behavior-preserving tests (Area 1) — and, while in that code, wrap the unguarded primary progressive store call (Area 7).
2. The background-task exception-consuming done-callback (Area 3) — narrow, and closes a real swallow-gap immediately.
3. Async persistence facade and concurrency benchmark (Area 2).
4. Selection-log replay command (Area 4) — the one genuinely missing exposure/selection contract; the golden wire-in tests already exist.
5. Cold-start config strict / parse-vs-validation semantics (Area 5) — hot-reload last-known-good is already shipped.
6. `anyio_shutdown` unit test plus the lifecycle pattern doc (Area 6) — the `ExceptionGroup`-injection coverage already exists.

## Verification Performed

This review was source-based. I inspected the current implementation and compared it against the older full review themes where relevant. I did not run the full test suite because no runtime code was changed.

A follow-up source-verification pass (2026-06-17) independently re-checked every cited `file:line` and every recommendation against the current checkout. All five Executive-Summary "now fixed" claims verified true. Corrections folded back into the sections above: Area 3 over-stated the background-observability gap (aggregate telemetry already exists via `IndexObservability` / `stm_index_stats`, wired at `manager.py:1404` / `:1437`); Areas 4 and 6 over-stated test gaps (golden wire-in, ranker-version pinning, and bare/nested `ExceptionGroup` injection already exist); Area 5's hot-reload last-known-good recommendation is already shipped (`config.py:1085-1094`); two citation line numbers were corrected (`hook_cmd.py` 493 → 505, `daemon/server.py` 253 → 262); the transient-key "cannot be bypassed" framing in the Strengths section was tightened (the guard is caller-side at `manager.py:2723`); and one missed branch-level asymmetry was added as section 7 (`manager.py:2251`).

