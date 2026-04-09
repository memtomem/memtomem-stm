# Pipeline

Every proxied tool call goes through 4 stages (plus an optional 4b for fact extraction).

```mermaid
flowchart TD
    Up["upstream response"] --> Clean
    subgraph Clean["1. CLEAN"]
        C1["HTML / script strip"]
        C2["paragraph dedup"]
        C3["link flood collapse"]
    end
    Clean --> Compress
    subgraph Compress["2. COMPRESS"]
        C4["10 strategies"]
        C5["auto-selection"]
        C6["query-aware budget"]
    end
    Compress --> Surface
    subgraph Surface["3. SURFACE"]
        S1["gated · deduped<br/>rate-limited"]
        S2["inject LTM memories"]
    end
    Surface --> Index
    subgraph Index["4. INDEX (optional)"]
        I1["auto-index large<br/>responses → LTM"]
    end
    Index --> Agent["to agent"]

    Surface -.->|optional| Extract
    Extract["4b. EXTRACT<br/>(fact extraction)"] -.-> LTM[("memtomem LTM")]
    Index -.->|optional| LTM
```

The CLEAN → COMPRESS → SURFACE → INDEX path is synchronous. The optional **4b EXTRACT** stage runs in parallel via a background extractor that does not block the agent response.

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
