# STM MCP Tools

STM advertises four model-facing tools by default and hides eight operator tools
unless `MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=true` is set before startup.
All connected upstream tools are added as `{prefix}__{tool}`.

## Default model-facing tools

| Tool | Arguments | Purpose |
|---|---|---|
| `stm_proxy_select_chunks` | `key`, `sections[]` | Retrieve selected TOC sections |
| `stm_proxy_read_more` | `key`, `offset?=0`, `limit?` | Continue a progressive response |
| `stm_surfacing_feedback` | `surfacing_id`, rating fields | Rate surfaced memories |
| `stm_compression_feedback` | `server`, `tool`, `missing`, optional metadata | Report missing compressed information |

## Optional operator tools

| Tool | Key arguments | Purpose |
|---|---|---|
| `stm_proxy_stats` | — | Token, compression, and cache statistics |
| `stm_proxy_cache_clear` | `server?`, `tool?` | Clear response-cache scope |
| `stm_proxy_health` | — | Runtime upstream and breaker health |
| `stm_surfacing_stats` | `tool?`, `since?`, `limit=10` | Surfacing outcomes, faults, and feedback |
| `stm_selection_stats` | — | Selection telemetry and execution outcomes |
| `stm_compression_stats` | `tool?` | Compression feedback counts |
| `stm_progressive_stats` | `tool?` | Follow-up and coverage statistics |
| `stm_tuning_recommendations` | `since_hours?`, `tool?` | Per-tool tuning suggestions |

Operator tools are hidden only from MCP `tools/list`; the corresponding CLI
diagnostics remain available. Upstream tool titles are prefixed with the server
name when the upstream provides an MCP annotation title.

## Optional review-first tool

`stm_memory_propose` is advertised independently when
`MEMTOMEM_STM_FORMATION__ENABLED=true` is set before startup. It submits a
pending candidate to a compatible core and never performs a direct durable
write.
