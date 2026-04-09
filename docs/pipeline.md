# Pipeline

Every proxied tool call goes through 4 stages (plus an optional 4b for fact extraction).

```
upstream response
       │
       ▼
┌──────────────┐
│ 1. CLEAN     │  HTML/script strip · paragraph dedup · link flood collapse
└──────┬───────┘
       ▼
┌──────────────┐
│ 2. COMPRESS  │  10 strategies · auto-selection · query-aware budget
└──────┬───────┘
       ▼
┌──────────────┐
│ 3. SURFACE   │  inject relevant LTM memories (gated, deduped, rate-limited)
└──────┬───────┘
       ▼
┌──────────────┐
│ 4. INDEX     │  optional auto-indexing of large responses → LTM
└──────┬───────┘
       ▼
   to agent
```

## Stage 1: CLEAN

Removes noise from the upstream response before compression. Each step can be toggled per server in `stm_proxy.json`:

- **`<script>` / `<style>` removal** — content and tags fully stripped before other processing
- **HTML stripping** — removes tags (preserves code fences and generic types like `List<String>`)
- **Paragraph deduplication** — removes identical paragraphs
- **Link flood collapse** — replaces paragraphs where 80%+ lines are links (10+ lines) with `[N links omitted]`
- **Whitespace normalization** — collapses triple+ newlines to double

```json
{
  "cleaning": {
    "strip_html": true,
    "deduplicate": true,
    "collapse_links": true
  }
}
```

## Stage 2: COMPRESS

Reduces response size to save tokens. See [Compression Strategies](compression.md) for the full reference of all 10 strategies.

## Stage 3: SURFACE

Proactively injects relevant memories from a memtomem LTM server. See [Surfacing](surfacing.md) for the gating, dedup, and feedback details.

Surfacing only activates when the compressed response is at least `min_response_chars` (default 5000). For small responses, surfacing is skipped to avoid negative token savings.

## Stage 4: INDEX (optional)

Automatically indexes large responses to memtomem LTM for future retrieval. See [Caching & Auto-Indexing](caching.md#auto-indexing) for the configuration reference.

## Stage 4b: Auto Fact Extraction (optional)

Automatically extracts discrete facts from tool responses using an LLM. Strategies: `llm` (default, Ollama qwen3:4b with no-think mode), `heuristic`, `hybrid`, `none`. Each extracted fact is stored as an individual `.md` file and indexed; deduplication via embedding similarity (threshold 0.92).

```json
{
  "extraction": {
    "enabled": true,
    "strategy": "llm",
    "llm": {
      "provider": "ollama",
      "model": "qwen3:4b"
    }
  }
}
```

Per-tool override: `"extraction": true|false` in `tool_overrides` or `UpstreamServerConfig`.
