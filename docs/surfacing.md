# Proactive Memory Surfacing

When your agent calls a proxied tool, STM automatically:

1. **Extracts context** from the tool name and arguments
2. **Checks relevance** (rate limit, cooldown, write-tool filter)
3. **Searches LTM** (memtomem) for related memories
4. **Injects relevant memories** at the top of the response

```mermaid
flowchart LR
    Tool["proxied tool call"] --> Extract["1. extract context<br/>(query)"]
    Extract --> Gate{"2. relevance gate"}
    Gate -->|skip| Pass["return original<br/>response"]
    Gate -->|pass| Search["3. search LTM<br/>(MCP mem_search)"]
    Search --> Filter{"score ≥ min_score?<br/>not already shown?"}
    Filter -->|no| Pass
    Filter -->|yes| Inject["4. inject memories<br/>+ working memory"]
    Inject --> Out["enriched response"]
```

## How Context Extraction Works

STM extracts a search query in priority order:

```mermaid
flowchart TD
    Start(["tool call"]) --> P1{"per-tool<br/>query_template?"}
    P1 -->|yes| Use1["render template<br/>e.g. 'file {arg.path}'"]
    P1 -->|no| P2{"_context_query<br/>arg present?"}
    P2 -->|yes| Use2["use agent-provided<br/>query"]
    P2 -->|no| P3["scan all args"]
    P3 --> Collect["collect path-like tokens<br/>+ semantic string values<br/>(skip UUID / hex / bool)"]
    Collect --> Has{"any tokens<br/>collected?"}
    Has -->|yes| Use3["merged query<br/>e.g. 'src auth handler my-project'"]
    Has -->|no| Use5["fallback: tool name<br/>'search_repos' → 'search repos'"]

    Use1 --> Check{"≥ min_query_tokens<br/>(default 3)?"}
    Use2 --> Check
    Use3 --> Check
    Use5 --> Check
    Check -->|yes| Q["query"]
    Check -->|no| Skip["skip surfacing"]
```

> **Note**: Steps 1 (template) and 2 (`_context_query`) are "first match wins". The heuristic fallback (step 3) iterates over **all** tool arguments and collects path-like tokenizations and semantic string values together into a merged query, rather than stopping at the first match.

## What the Agent Sees

When memories are found, they're wrapped in `<surfaced-memories>` XML tags and injected before the response:

```
<surfaced-memories>
## Relevant Memories

- **auth_notes.md** [code-notes] (score=0.85): OAuth2 implementation uses PKCE flow...
- **api_design.md** (score=0.72): Rate limiting is handled by middleware in...

_Surfacing ID: abc123def456 — call `stm_surfacing_feedback` to rate_
</surfaced-memories>

(original tool response here)
```

The injection mode is configurable: `prepend` (default), `append`, or `section`.

## Surfacing Controls

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `true` | Global on/off switch |
| `min_score` | `0.02` | Minimum search score to include a result |
| `max_results` | `3` | Maximum memories surfaced per tool call (model-scaled) |
| `max_injection_chars` | `3000` | Maximum total chars injected, truncated if exceeded (model-scaled) |
| `min_response_chars` | `5000` | Skip surfacing for small responses |
| `min_query_tokens` | `3` | Skip if extracted query has fewer tokens |
| `timeout_seconds` | `3.0` | Surfacing timeout (falls back to original response) |
| `cooldown_seconds` | `5.0` | Skip duplicate queries (Jaccard > 0.95) within this window |
| `max_surfacings_per_minute` | `15` | Global rate limit |
| `injection_mode` | `prepend` | Where to inject: `prepend`, `append`, `section` |
| `section_header` | `## Relevant Memories` | Header text for injected section |
| `default_namespace` | `null` | Restrict search to a specific namespace |
| `exclude_tools` | `[]` | fnmatch patterns to never surface (e.g. `["*debug*"]`) |
| `write_tool_patterns` | `*write*`, `*create*`, `*delete*`, `*push*`, `*send*`, `*remove*` | Auto-skip write/mutation operations |
| `include_session_context` | `true` | Include working memory (scratch) items |
| `dedup_ttl_seconds` | `604800` (7d) | Cross-session dedup window; `0` to disable |
| `context_window_size` | `0` | Expand ±N adjacent chunks around search hits; `0` to disable |
| `consumer_model` | `""` | Model name for auto-scaling `max_results` and `max_injection_chars` |
| `feedback_db_path` | `~/.memtomem/stm_feedback.db` | SQLite store for events, feedback, and cross-session dedup |
| `cache_ttl_seconds` | `60.0` | Internal surfacing result cache TTL |
| `circuit_max_failures` | `3` | Consecutive failures before circuit breaker opens |
| `circuit_reset_seconds` | `60.0` | Seconds before half-open probe after circuit opens |
| `auto_tune_enabled` | `true` | Auto-adjust `min_score` from feedback: >60% `not_relevant` raises it (stricter), <20% lowers it (more inclusive) |
| `auto_tune_min_samples` | `20` | Minimum feedback entries before adjusting per-tool score |
| `auto_tune_score_increment` | `0.002` | Step size for `min_score` adjustments |
| `feedback_enabled` | `true` | Enable the feedback recording and `stm_surfacing_feedback` tool |
| `fire_webhook` | `true` | Fire surfacing event webhooks |

## Per-tool Templates

Fine-tune surfacing behavior per tool:

```json
{
  "surfacing": {
    "context_tools": {
      "read_file": {
        "enabled": true,
        "query_template": "file {arg.path}",
        "namespace": "code-notes",
        "min_score": 0.1,
        "max_results": 5
      },
      "search_issues": {
        "min_score": 0.5,
        "max_results": 2
      },
      "get_diff": {
        "enabled": false
      }
    }
  }
}
```

Template variables: `{tool_name}`, `{server}`, `{arg.ARGUMENT_NAME}`

## End-to-end Surface Call

```mermaid
sequenceDiagram
    autonumber
    actor Agent
    participant STM as SurfacingEngine
    participant Ext as ContextExtractor
    participant Gate as RelevanceGate
    participant CB as CircuitBreaker
    participant MCP as McpClientSearchAdapter
    participant Core as memtomem core

    Agent->>STM: tool response (≥ min_response_chars)
    STM->>Ext: extract_query(server, tool, args)
    Ext-->>STM: query
    STM->>Gate: should_surface(server, tool, query)
    Gate-->>STM: pass / skip
    alt skip (write tool / cooldown / rate limit)
        STM-->>Agent: original response
    else pass
        STM->>CB: check
        alt circuit open
            STM-->>Agent: original response
        else closed / half-open
            STM->>MCP: search(query, top_k, namespace)
            MCP->>Core: mem_search (via stdio MCP)
            Core-->>MCP: results
            MCP-->>STM: parsed results
            opt include_session_context
                STM->>MCP: scratch_list()
                MCP->>Core: mem_do(action="scratch_get")
                Core-->>MCP: working memory text
                MCP-->>STM: parsed entries
            end
            STM->>STM: filter min_score · dedup
            STM->>STM: format + inject
            STM-->>Agent: enriched response (+ surfacing_id)
        end
    end
```

## LTM Connection

STM connects to the LTM exclusively over the MCP protocol. The surfacing engine spawns (or attaches to) a memtomem MCP server using these settings:

```bash
# Default — spawns `memtomem-server` as a child process
export MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND=memtomem-server

# Pass extra arguments if needed (e.g. point at a custom config)
export MEMTOMEM_STM_SURFACING__LTM_MCP_ARGS='["--config","/etc/memtomem.json"]'
```

This makes memtomem just another MCP upstream as far as STM is concerned — the same compression / cache / surfacing pipeline applies, and a memtomem crash never takes down STM's other upstream connections.

> **Note**: prior versions supported an in-process mode that imported memtomem directly. That path was removed so STM has a single LTM retrieval path and so core internals can evolve without breaking STM.

## Session & Cross-Session Dedup

Surfacing tracks which memory IDs have already been shown so the same content does not surface twice. Two layers work together:

| Layer | Storage | Purpose | Eviction |
|-------|---------|---------|----------|
| In-memory `_surfaced_ids` | Insertion-ordered `dict` on the `SurfacingEngine` | Skip IDs already surfaced in this process | Bulk prune to ~5,000 entries when size exceeds **10,000** (FIFO — oldest insertions go first) |
| Persistent `seen_memories` | SQLite row in `stm_feedback.db` | Skip IDs surfaced in a prior session within `dedup_ttl_seconds` | TTL-based (default 7 days; `0` disables) |

The in-memory set is **seeded from `seen_memories`** on startup so dedup survives restarts within the TTL, and every new surfacing writes to both layers via `FeedbackStore.mark_surfaced(ids)`.

> **Why did an old memory re-surface?** Two common causes: (1) the 10k FIFO cap evicted the ID during a long session, so the in-memory layer no longer remembers it; (2) `dedup_ttl_seconds` elapsed, so the persistent row was ignored. Lower the TTL or raise it as needed — setting `dedup_ttl_seconds=0` disables cross-session dedup entirely.

## Feedback & Auto-Tuning

```mermaid
sequenceDiagram
    autonumber
    actor Agent
    participant STM as SurfacingEngine
    participant FB as FeedbackTracker
    participant AT as AutoTuner
    participant Core as memtomem core

    Note over Agent,STM: surfacing_id was returned with the memories
    Agent->>STM: stm_surfacing_feedback(id, rating)
    STM->>FB: record_feedback(id, rating)
    alt rating == "helpful"
        FB->>Core: mem_do(action="increment_access", chunk_ids)
        Core-->>FB: ok (capped at max_boost=1.5)
    end
    FB->>AT: maybe_adjust(tool)
    AT->>AT: compute not_relevant ratio
    alt ≥ 20 samples for tool
        AT->>AT: per-tool ratio
    else cold start
        AT->>AT: global ratio fallback
    end
    AT-->>STM: new min_score (clamped)
    Note over STM: next call uses tuned threshold
```

Rate surfaced memories to improve future relevance:

```
stm_surfacing_feedback(surfacing_id="abc123", rating="helpful")
stm_surfacing_feedback(surfacing_id="def456", rating="not_relevant")
stm_surfacing_feedback(surfacing_id="ghi789", rating="already_known")
```

Valid ratings: `helpful`, `not_relevant`, `already_known`.

When auto-tuning is enabled (default), STM adjusts `min_score` per tool based on feedback. In plain terms: **`not_relevant` ratings push `min_score` up** (surfacing becomes more selective), while a run of **`helpful`/`already_known` ratings pushes it down** (surfacing becomes more inclusive) because they keep the `not_relevant` ratio low. Concretely:

| Feedback ratio | Action |
|----------------|--------|
| > 60% `not_relevant` | Raise `min_score` by +0.002 (surface fewer, more relevant) |
| < 20% `not_relevant` | Lower `min_score` by -0.002 (surface more) |
| 20–60% `not_relevant` | Hold current `min_score` |

Adjustment step is `auto_tune_score_increment` (default `0.002`) and the tuned score is clamped to `[0.005, 0.05]`.

```mermaid
flowchart LR
    Sample["new feedback"] --> N{"≥ 20 samples<br/>for this tool?"}
    N -->|no| Cold["use global ratio<br/>(cold-start fallback)"]
    N -->|yes| Local["use per-tool ratio"]
    Cold --> R{"not_relevant<br/>ratio?"}
    Local --> R
    R -->|"> 60%"| Up["min_score += 0.002<br/>(surface less)"]
    R -->|"< 20%"| Down["min_score -= 0.002<br/>(surface more)"]
    R -->|"20-60%"| Hold["no change"]
    Up --> Cap["clamp to<br/>[0.005, 0.05]"]
    Down --> Cap
    Cap --> Tool[("per-tool<br/>min_score")]
    Tool -.->|next call| Sample
```

Requires `auto_tune_min_samples` (default 20) feedback entries before adjusting. Score is capped between 0.005 and 0.05. **Cold-start fallback**: new tools with insufficient samples use the global ratio across all tools instead of waiting for 20 per-tool samples.

**Search boost from feedback**: when you rate memories as "helpful", their `access_count` is incremented in the core search index (once per surfacing event, capped at `max_boost=1.5`). This creates a positive feedback loop where useful memories rank higher in future searches.

Check effectiveness with `stm_surfacing_stats`:

```
Surfacing Stats
===============
Total surfacings: 142
Total feedback:   38

By rating:
  helpful: 28
  not_relevant: 7
  already_known: 3

Helpfulness: 73.7%
```
