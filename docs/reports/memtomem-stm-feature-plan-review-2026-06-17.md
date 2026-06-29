# memtomem-stm Recent Feature Plan Review

Date: 2026-06-17
Scope: Review of the recent memtomem-stm hardening plan and the current
`fix/progressive-primary-store-guard` implementation, with follow-up feature
improvement and extension ideas.

## Summary

The current implementation direction is sound. The 2026-06-17 core logic review
identified two related near-term priorities:

1. Reduce risk in the central `ProxyManager._call_tool_inner()` orchestration
   path by moving toward explicit stage contracts.
2. Close the concrete progressive-delivery asymmetry where the primary
   `PROGRESSIVE` store path could throw and discard an otherwise successful
   upstream response.

The current branch addresses the second point narrowly and correctly. The
primary progressive store call now degrades to a zero-loss passthrough when
`_apply_progressive()` fails, records the degradation in metrics, and avoids
caching the degraded response so a later identical call can retry progressive
delivery after the backing store recovers.

Focused validation passed:

```bash
uv run pytest tests/test_compression_ratio_guard.py
```

Result: `23 passed`.

## Current Implementation Assessment

### What Looks Correct

- `src/memtomem_stm/proxy/manager.py` now wraps the primary
  `PROGRESSIVE` `_apply_progressive()` call and falls back to returning the full
  cleaned upstream content. This preserves correctness: the agent still receives
  the successful tool result even if the progressive pending store is temporarily
  unavailable.
- The metrics label `progressive->passthrough_on_error` makes the degradation
  visible without conflating it with normal progressive delivery.
- The cache-store gate skips this error-degraded passthrough. This is important:
  caching it would pin the non-progressive full response for the cache TTL and
  suppress progressive delivery even after store recovery.
- The tests distinguish the two important passthrough cases:
  - Error-degraded large progressive passthrough is not cached.
  - Normal single-chunk progressive passthrough remains cacheable.

### Remaining Design Risk

This is a good narrow fix, but it also confirms the structural issue described
in the core review: `ProxyManager._call_tool_inner()` still carries many
cross-stage invariants through local variables. The new
`progressive_passthrough_on_error` flag is correct, but it is another local
state bit that must stay aligned with metrics, surfacing, and cache behavior.

The next hardening step should avoid adding more one-off flags in this path.
Instead, stage outputs should carry explicit contracts such as cacheability,
metrics strategy, transient-key state, and fallback reason.

## Recommended Follow-Up Work

### 1. Promote Cacheability To A Stage Contract

Priority: High

Current behavior relies on a mix of:

- `progressive_passthrough_on_error`
- `response_carries_transient_key(compressed)`
- privacy checks inside `ProxyCache.set()`
- caller-side knowledge in `ProxyManager`

Recommended shape:

```python
@dataclass
class CompressionResult:
    text: str
    strategy_label: str
    compressed_chars_for_metrics: int
    cacheable: bool
    cache_skip_reason: str | None = None
    transient_key: bool = False
```

This would let the cache store gate read one explicit value instead of
reconstructing intent from response text and local flags.

Also consider moving the transient-key rejection into `ProxyCache.set()` itself.
Today the runtime transient-key guard is effective because there is one
`cache.set()` caller, but a future caller would not automatically inherit it.

### 2. Add Progressive Degradation Observability

Priority: Medium-High

The metrics strategy label is a good start, but operators should be able to
answer this directly:

> Is progressive delivery frequently degrading because the backing store is
> failing?

Useful additions:

- `stm_progressive_stats` count for `passthrough_on_error`
- recent server/tool pairs affected by progressive degradation
- last degradation error type
- optional fault bucket in `stm_proxy_stats`

This turns the current correctness fix into an operationally visible signal.

### 3. Consume Background Task Exceptions Explicitly

Priority: Medium

The current background index/extract paths use task done-callbacks that discard
finished tasks. A failure raised outside the coroutine's inner handling can
surface only as an un-retrieved task exception warning.

Recommended change:

- Add `_on_background_task_done(stage, trace_id, server, tool, task)`.
- Call `task.result()` inside the callback.
- Record exceptions through existing index/extraction observability.
- Optionally correlate background outcomes with the request `trace_id`.

This is a narrow reliability improvement and does not require the larger stage
refactor.

### 4. Add Selection Telemetry Replay

Priority: Medium

Recent unreleased work added `stm_selection_stats` and populated
`execution.cache_hit`. That makes live inspection stronger. The natural next
extension is offline replay:

```bash
mms selection replay --log ~/.memtomem/selection.jsonl
```

The command should validate historical JSONL rows against the current parser,
schema, ranker version, and eligibility logic without needing a live proxy.

Possible outputs:

- schema validation failures
- ranker-version distribution
- tools that would now be rejected by current hard filters
- drift in candidate/ranked tool sets
- cache-hit and execution-latency summaries

This closes the loop from "collect telemetry" to "evaluate selection quality".

### 5. Move Hot SQLite Persistence Behind Async Facades

Priority: Medium for current local usage; High before multi-client gateway use.

The cache, metrics store, and feedback hot reads still perform synchronous
SQLite work on the event loop. This is acceptable for low-volume local use, but
it becomes harder to reason about as memtomem-stm is used as a shared gateway.

Recommended phased approach:

1. Add `async_get` / `async_set` wrappers using `asyncio.to_thread` or a bounded
   executor.
2. Keep the existing sync API for CLI and tests.
3. Add a concurrency benchmark covering cache hit/miss plus metrics writes.
4. Move the proxy hot path to the async facade only after the benchmark exists.

### 6. Tighten Cold-Start Config Failure Behavior

Priority: Medium

Hot reload already keeps the last known-good config. The remaining risk is cold
start: a malformed non-critical override can cause broad fallback to defaults.

Recommended additions:

- Distinguish missing config, parse failure, and validation failure in startup
  diagnostics.
- Add a strict startup mode that exits on invalid config or env override.
- Document which contexts fail open and which fail closed.

This keeps local convenience while making production behavior explicit.

### 7. Add A Practical Doctor Surface

Priority: Medium-Low

Recent features now span proxy cache, pending store, LTM reachability,
selection telemetry, observability-tool exposure, and config validity. A
single operator-facing check would help.

Possible command:

```bash
mms doctor
```

Suggested checks:

- config load status and invalid env overrides
- proxy enabled/surfacing enabled consistency
- LTM transport reachability
- pending-store backend and file permissions
- cache database health and transient-key legacy rows
- selection telemetry file readability
- observability tools advertised or hidden

This should be read-only by default, with repair actions kept explicit.

## Suggested Execution Order

1. Finish the current progressive primary-store guard PR, including changelog.
2. Add cacheability as an explicit compression/progressive stage result.
3. Add progressive degradation counters to `stm_progressive_stats`.
4. Add the background-task done-callback exception consumer.
5. Build `mms selection replay` on top of existing selection telemetry.
6. Add async persistence facades and a concurrency benchmark.
7. Add strict cold-start config mode and `mms doctor`.

## Changelog Note For Current Branch

Suggested `CHANGELOG.md` entry:

```markdown
- **Primary progressive store failures degrade to uncached passthrough** —
  when the primary `PROGRESSIVE` path cannot build/store its first chunk, the
  proxy now returns the full cleaned upstream response instead of discarding a
  successful tool result as an internal error. The degraded response is not
  cached, so a later identical call can retry progressive delivery after the
  store recovers.
```
