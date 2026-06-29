# memtomem LTM Separation and Integration Review

Date: 2026-05-02
Scope: `memtomem-stm` separation from memtomem LTM, MCP-based retrieval/write integration, version compatibility, failure behavior, and operator-facing documentation.

## Summary

The STM/LTM separation is conceptually strong: `memtomem-stm` does not import memtomem core for surfacing, and the active retrieval path talks to LTM through MCP (`mem_search`, `mem_do`). This is the right architectural boundary for allowing LTM internals to evolve independently.

The main gap is asymmetry between read and write integration:

- LTM read/surfacing integration is implemented and covered by tests.
- LTM write/index integration is only represented by a structural `FileIndexer` protocol and proxy manager hooks, but no runtime adapter is wired into the server startup path.

That makes current behavior closer to "STM can retrieve memories from LTM" than "STM fully round-trips compressed responses into LTM."

## Verification

Selected LTM integration tests passed:

```text
uv run pytest tests/test_remote_ltm_integration.py tests/test_surfacing_engine.py::TestSurfacingTimeout -q
10 passed in 2.63s
```

## Separation Boundary

### Strength: surfacing is remote-only over MCP

Server startup explicitly constructs `McpClientSearchAdapter` for surfacing and passes it into `SurfacingEngine`.

- `src/memtomem_stm/server.py:126`
- `src/memtomem_stm/server.py:134`
- `src/memtomem_stm/server.py:158`

The adapter opens a stdio MCP client session to `ltm_mcp_command` and `ltm_mcp_args`.

- `src/memtomem_stm/surfacing/mcp_client.py:188`
- `src/memtomem_stm/surfacing/mcp_client.py:205`
- `src/memtomem_stm/surfacing/mcp_client.py:211`

This preserves package-level independence: LTM is an external MCP server, not an in-process Python dependency.

### Risk: docs overstate "same pipeline applies" to LTM

`docs/surfacing.md` says memtomem is "just another MCP upstream" and "the same compression / cache / surfacing pipeline applies."

- `docs/surfacing.md:191`

In code, LTM is not registered through the generic upstream proxy path. It is a special-purpose MCP client used by `SurfacingEngine`. Its `mem_search` responses are not passed through STM compression/cache/surfacing like normal upstream tool results.

Recommendation: adjust wording to "same MCP protocol boundary" rather than "same compression/cache/surfacing pipeline."

## Retrieval Integration

### Implemented calls

The adapter supports:

- `mem_search` for LTM retrieval.
- `mem_do(action="version")` for format negotiation.
- `mem_do(action="increment_access")` for helpful feedback boost.
- `mem_do(action="scratch_get")` for working-memory/session context.

Evidence:

- `src/memtomem_stm/surfacing/mcp_client.py:240`
- `src/memtomem_stm/surfacing/mcp_client.py:295`
- `src/memtomem_stm/surfacing/mcp_client.py:348`
- `src/memtomem_stm/surfacing/mcp_client.py:383`

`SurfacingEngine` calls the adapter with namespace, `top_k`, `context_window`, and trace correlation.

- `src/memtomem_stm/surfacing/engine.py:453`
- `src/memtomem_stm/surfacing/engine.py:458`

### Good compatibility behavior

Structured result parsing is capability-gated. If core does not advertise structured search support, STM downgrades to compact parsing.

- `src/memtomem_stm/surfacing/mcp_client.py:227`
- `src/memtomem_stm/surfacing/mcp_client.py:240`
- `src/memtomem_stm/surfacing/mcp_client.py:252`

This is a good loose-coupling mechanism between independently released STM and LTM versions.

### Risk: only stdio transport is supported for LTM surfacing

Generic upstreams support stdio, SSE, and streamable HTTP. LTM surfacing adapter only uses `stdio_client`.

- `src/memtomem_stm/surfacing/mcp_client.py:205`
- `src/memtomem_stm/surfacing/mcp_client.py:209`

This is acceptable for local single-user use, but limits production deployment where LTM should run as a long-lived network service.

Recommendation: add `ltm_mcp_transport`, `ltm_mcp_url`, and headers support to `SurfacingConfig`, mirroring upstream transport support.

## Write / Index Integration

### Gap: no runtime LTM write adapter is wired

STM defines a `FileIndexer` protocol intended to let memtomem's index engine satisfy the interface without a hard dependency.

- `src/memtomem_stm/proxy/protocols.py:17`

But server startup creates `ProxyManager` without `index_engine`.

- `src/memtomem_stm/server.py:187`

The write paths are gated on `self._index_engine is not None`.

- `src/memtomem_stm/proxy/manager.py:1651`
- `src/memtomem_stm/proxy/manager.py:1745`

So `auto_index` and `extraction` are not actually connected to LTM in normal server execution.

### Documentation mismatch

`docs/configuration.md` describes auto-indexing as "Stage 4 — save large responses to LTM."

- `docs/configuration.md:73`

Current runtime behavior does not satisfy that unless an embedding caller constructs `ProxyManager` with an external `index_engine`. The installed MCP server path does not.

Recommendation: either:

1. Implement a remote LTM write adapter that calls LTM MCP tools such as `mem_add` / `mem_index`, or
2. Document auto-index/extraction as library-only hooks until a server-wired adapter exists.

## Failure Behavior

### Good: LTM startup failure does not kill STM proxy

If the LTM adapter fails during app startup, surfacing is disabled and the proxy can continue.

- `src/memtomem_stm/server.py:130`
- `src/memtomem_stm/server.py:140`

This preserves the proxy's primary function even when memory is unavailable.

### Risk: empty result and LTM failure can look the same downstream

`McpClientSearchAdapter.search()` returns `([], [])` on no session, call failure, or failed reconnect.

- `src/memtomem_stm/surfacing/mcp_client.py:292`
- `src/memtomem_stm/surfacing/mcp_client.py:321`
- `src/memtomem_stm/surfacing/mcp_client.py:324`

`SurfacingEngine` then records `no_results_score` if no scored results remain.

- `src/memtomem_stm/surfacing/engine.py:493`
- `src/memtomem_stm/surfacing/engine.py:500`

Operationally, this conflates:

- LTM unavailable.
- LTM returned no results.
- Parser failed or returned empty output.

Recommendation: return a structured adapter result with status (`ok`, `transport_error`, `parse_error`, `empty`) or raise typed errors that `SurfacingEngine` records as distinct skip/outcome labels.

### Risk: surfacing timeout may cancel MCP calls without reconnect

`SurfacingEngine.surface()` wraps `_do_surface()` in an outer timeout. If it cancels an in-flight MCP `mem_search`, the adapter does not necessarily reconnect.

- `src/memtomem_stm/surfacing/engine.py:184`
- `src/memtomem_stm/surfacing/mcp_client.py:309`

The generic upstream proxy path has stronger timeout/reconnect handling. LTM adapter should match that standard.

Recommendation: put per-call timeout inside `McpClientSearchAdapter.search()` and reconnect on timeout/cancellation before returning.

## Data Boundary and Trust

### Retrieval data sent to LTM

For surfacing, STM sends:

- extracted query
- `top_k`
- namespace
- optional `context_window`
- optional `_trace_id`

This is a narrow and appropriate read-side boundary.

### Write-side data would be broad

If auto-index is wired, the current implementation writes full cleaned tool responses to Markdown files and indexes them.

- `src/memtomem_stm/proxy/manager.py:1674`
- `src/memtomem_stm/proxy/manager.py:1705`

That means the LTM write path would receive much more sensitive data than retrieval queries. This reinforces the need to fix the LLM/privacy issue from the implementation review and to add explicit redaction or operator confirmation semantics for auto-index.

## Tests

Current tests validate the most important read-side integration:

- remote stdio MCP server startup
- `mem_search` surfacing
- `scratch_get` session context
- `increment_access` feedback boost
- structured format negotiation

Evidence:

- `tests/test_remote_ltm_integration.py:1`
- `tests/test_remote_ltm_integration.py:56`
- `tests/test_remote_ltm_integration.py:73`
- `tests/test_remote_ltm_integration.py:93`
- `tests/test_remote_ltm_integration.py:128`

Missing coverage:

- LTM network transport because only stdio is supported.
- LTM call cancellation and reconnect after timeout.
- Parser failure distinct from empty search results.
- End-to-end write path to a remote LTM server.
- Compatibility tests for older LTM servers missing `mem_do(version)`, `scratch_get`, or `increment_access`.

## Recommended Priority

1. Decide the write integration contract: remote `mem_add` / `mem_index` adapter or document write hooks as library-only.
2. Add explicit LTM adapter health/status outcomes so "no memory found" and "LTM unavailable" are observable separately.
3. Add timeout/cancellation reconnect behavior to the LTM adapter.
4. Add non-stdio LTM transport support for service deployment.
5. Align docs with the actual boundary: LTM is MCP-coupled for retrieval, not a generic proxied upstream and not currently wired for runtime indexing.

## Bottom Line

The STM/LTM split is directionally correct and keeps the packages decoupled. The retrieval side is usable today. The write side and operator observability need tightening before claiming full STM-to-LTM memory round-trip behavior.
