# memtomem-stm Implementation Review

Date: 2026-05-02
Scope: `src/memtomem_stm`, core proxy pipeline, surfacing, remote LTM adapter, config, cache, indexing/extraction paths, and selected regression tests.

## Summary

The current implementation is structurally solid. The main proxy path is clearly organized as:

1. CLEAN
2. COMPRESS or PROGRESSIVE
3. SURFACE
4. INDEX / EXTRACT
5. CACHE

Important production safeguards are already present: upstream timeout and reconnect handling, cache stampede guards, compression retention guard, progressive fallback, surfacing cache, session/cross-session deduplication, feedback invalidation, and metrics/tracing hooks.

Selected verification passed:

```text
uv run pytest tests/test_proxy_manager_pipeline.py tests/test_surfacing_engine.py tests/test_progressive.py tests/test_config_constraints.py -q
201 passed in 0.56s
```

## Findings

### 1. INDEX / EXTRACT is effectively inactive in normal server startup

`server.py` creates `ProxyManager` without an `index_engine`.

- `src/memtomem_stm/server.py:187`
- `src/memtomem_stm/proxy/manager.py:1651`
- `src/memtomem_stm/proxy/manager.py:1745`

Both `auto_index` and `extraction` require `self._index_engine is not None`, so enabling these config flags does not store anything in the normal MCP server path. The code emits a warning when `auto_index.enabled=true` but no engine exists, but the documented pipeline still suggests INDEX is part of runtime behavior.

Recommendation: implement a remote LTM `FileIndexer` adapter, or explicitly mark INDEX/EXTRACT as unavailable in the standalone server path.

### 2. LLM compression privacy scan is not wired into the main path

Privacy detection exists in `proxy/privacy.py`, and `LLMCompressor.compress()` can use `privacy_patterns`.

- `src/memtomem_stm/proxy/privacy.py:41`
- `src/memtomem_stm/proxy/compression.py:1166`

However, `ProxyManager._apply_compression()` calls LLM compression without passing privacy patterns. If `llm_summary` is enabled, sensitive content may be sent to the configured LLM provider.

Recommendation: add `privacy_scan_enabled` and `privacy_patterns` to `LLMCompressorConfig`, default to enabled with existing default patterns, and pass them from `ProxyManager`.

### 3. Surfacing MCP timeout/cancel does not reconnect the LTM session

The proxy upstream call path reconnects after timeout/cancel-like failures. Surfacing wraps `_do_surface()` in `asyncio.wait_for`, but `McpClientSearchAdapter.search()` only reconnects for caught transport errors.

- `src/memtomem_stm/surfacing/engine.py:184`
- `src/memtomem_stm/surfacing/mcp_client.py:309`
- `src/memtomem_stm/proxy/manager.py:1162`

If `mem_search` is cancelled by the outer surfacing timeout, the MCP session may be left in a bad state.

Recommendation: move timeout handling into `McpClientSearchAdapter.search()` or catch cancellation/timeout at the adapter boundary and reconnect before returning empty results.

### 4. Initial upstream connection path has weaker cleanup than reconnect path

Reconnect uses a local `AsyncExitStack` and closes it on failure. Initial `_connect_server()` enters contexts directly into the manager-wide stack.

- `src/memtomem_stm/proxy/manager.py:258`
- `src/memtomem_stm/proxy/manager.py:300`

If initialization or `list_tools()` fails after opening partial resources, cleanup is less precise until manager shutdown.

Recommendation: mirror `_reconnect_server()` by building a local stack during initial connect and transferring ownership only after successful tool discovery.

### 5. `ProxyConfig.default_compression` appears unused

`ProxyConfig.default_compression` is defined, but tool config resolution starts from `UpstreamServerConfig.compression`, whose own default is `AUTO`.

- `src/memtomem_stm/proxy/config.py:455`
- `src/memtomem_stm/proxy/manager.py:490`

Changing the global default may not affect upstreams as an operator expects.

Recommendation: either remove/deprecate the field or wire it as the fallback when an upstream does not explicitly set compression.

## Strengths

- Upstream calls have per-attempt timeout, total deadline, retry classification, and reconnect.
- Cache keys avoid trace-id pollution, preserving hit rate.
- Compression retention guard prevents unexpected severe loss and falls back to progressive/hybrid/truncate.
- Cache stores pre-surfacing content, so memories stay fresh on cache hits.
- Surfacing implements gating, rate limiting, deduplication, cache invalidation, feedback, and access boosting.
- Config hot reload keeps the last valid config on parse failure.
- SQLite stores use WAL-oriented tuning and reasonable file permissions.

## Recommended Priority

1. Decide and fix INDEX/EXTRACT runtime behavior.
2. Wire privacy scanning into LLM compression by default.
3. Add surfacing MCP timeout/cancel reconnect handling.
4. Harden initial upstream connection cleanup.
5. Clean up or activate unused global config fields.

## Notes

Untracked workspace files observed during review and left untouched:

- `node_modules/`
- `package-lock.json`
- `package.json`
