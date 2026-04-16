# Custom Integration: FileIndexer

The default deployment runs the STM proxy **without** a file indexer.
Two optional pipeline stages — **INDEX** (Stage 4) and **EXTRACT**
(Stage 4b) — activate only when a `FileIndexer` is passed to
`ProxyManager`.  This guide covers the integration interface, wiring,
and known caveats.

## When you need this

Wire a `FileIndexer` when you want the proxy to:

- **Auto-index** compressed responses into a vector store (LTM) so
  they are searchable across sessions.
- **Extract facts** from tool responses and index them as individual
  memory entries for later retrieval.

If you only need compression, surfacing, and caching, skip this — the
default deployment covers those without a `FileIndexer`.

For auto-indexing configuration (thresholds, namespace templates,
file format), see [Caching & Auto-Indexing](caching.md#auto-indexing).

## Protocol

The proxy expects a structural match against `FileIndexer`
(`proxy/protocols.py`):

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass
class IndexResult:
    indexed_chunks: int = 0


class FileIndexer(Protocol):
    async def index_file(
        self,
        path: Path,
        *,
        force: bool = False,
        namespace: str | None = None,
    ) -> IndexResult: ...

    async def is_duplicate(
        self,
        text: str,
        *,
        namespace: str | None = None,
        threshold: float = 0.92,
    ) -> bool: ...
```

`memtomem`'s `IndexEngine` structurally satisfies this protocol — no
explicit import or registration needed.

### Methods

| Method | Required | Purpose |
|--------|----------|---------|
| `index_file` | Yes | Index a markdown file at `path` into `namespace`. |
| `is_duplicate` | No | Semantic dedup check before writing extracted facts. Returns `False` by default if unimplemented. |

## Wiring

Pass the indexer when constructing `ProxyManager`:

```python
from memtomem_stm.proxy.manager import ProxyManager

proxy = ProxyManager(
    config,
    tracker,
    index_engine=my_indexer,   # activates INDEX + EXTRACT stages
    surfacing_engine=engine,
)
```

The default `server.py` entry point does **not** pass `index_engine`
(line 164).  Custom deployments must instantiate `ProxyManager`
directly or extend the server to inject one.

## Configuration

Both stages are off by default even when `index_engine` is wired:

```jsonc
{
  "auto_index": {
    "enabled": true,          // activate Stage 4
    "min_chars": 2000,        // skip short responses
    "memory_dir": "~/.memtomem/proxy_index",
    "namespace": "proxy-{server}"
  },
  "extraction": {
    "enabled": true,          // activate Stage 4b
    "strategy": "llm",        // "llm", "heuristic", or "hybrid"
    "background": true,       // async (default) or blocking
    "max_facts": 10,
    "min_response_chars": 500,
    "dedup_threshold": 0.92,
    "memory_dir": "~/.memtomem/extracted_facts",
    "namespace": "facts-{server}"
  }
}
```

Namespace templates support `{server}` and `{tool}` placeholders.

Environment overrides follow the standard pattern:

```bash
MEMTOMEM_STM_PROXY__AUTO_INDEX__ENABLED=true
MEMTOMEM_STM_PROXY__EXTRACTION__ENABLED=true
```

For the complete env-var reference, see
[Configuration](configuration.md).  Both stages run as part of the
pipeline — see [Pipeline → Stage 4](pipeline.md#stage-4-index-optional)
for the runtime flow and failure guards.

## Known caveats

These are documented limitations of the current implementation.  All
are behind the `index_engine` guard — they do not affect the default
(indexer-less) deployment.

### 1. Auto-index failure is logged, not propagated

If `index_file()` raises, the proxy logs a WARNING with traceback and
returns the unindexed response.  The caller sees a successful result
but the response is not searchable in LTM.

- **Manager guard**: `manager.py:1327` catches and returns `surfaced`.
- **Inner handler**: `memory_ops.py:78` catches, logs, returns
  `chunks=0`.
- **Fixed in**: `977da4f` (outer guard), traceback logging confirmed.

### 2. No file-level dedup on auto-index retries

Auto-indexed files use timestamp-based filenames
(`{server}__{tool}__{ts}.md`).  If the same response is indexed twice
(e.g., retry after transient failure), two files accumulate with
identical content.  The vector store may deduplicate at embedding
level, but disk usage grows.

### 3. Fact file orphans on index failure

When `extract_and_store` writes a fact file to disk but
`index_file()` subsequently fails, the markdown file remains on disk
but is not indexed.  The proxy logs a WARNING but does not clean up
the orphan.  Orphans are harmless but accumulate over time.

### 4. No startup reconciliation

On restart, the proxy does not scan `memory_dir` for unindexed files
left by prior crashes.  Files written but not indexed before a crash
remain orphaned until manually re-indexed.

### 5. Extraction is fire-and-forget

When `extraction.background=true` (default), extraction runs as an
`asyncio.create_task`.  If the task fails internally, the exception is
caught and logged at WARNING level (`memory_ops.py:159`), but:

- The caller (agent/user) is not notified.
- No retry is attempted.
- No metric is recorded for the failure.

The extraction done-callback only removes the task from the tracking
set — it does not log exceptions (unlike the webhook pattern in
`surfacing/engine.py:514-521`).

## Operational recommendations

1. **Monitor WARNING logs** from `memtomem_stm.proxy.memory_ops` — all
   5 caveats surface as WARNING-level messages with tracebacks.
2. **Periodic cleanup**: scan `memory_dir` for files not present in the
   index and either re-index or delete them.
3. **Test `is_duplicate`** if your indexer implements it — extraction
   dedup silently skips facts that match above `dedup_threshold`.
4. **Consider `background=false`** for extraction if you need
   guaranteed delivery — it blocks the response but ensures extraction
   completes or raises visibly.
5. **Log level**: set `MEMTOMEM_STM_LOG_LEVEL=DEBUG` for full
   auto-index and extraction tracing.  See
   [Operations → Logging](operations.md#logging).
