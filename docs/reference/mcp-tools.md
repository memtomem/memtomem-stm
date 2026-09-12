# STM MCP Tools

STM advertises five model-facing tools by default and hides eight operator tools
unless `MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=true` is set before startup.
All connected upstream tools are added as `{prefix}__{tool}`.

## Default model-facing tools

| Tool | Arguments | Purpose |
|---|---|---|
| `stm_proxy_describe_tool` | `name` | Retrieve full instructions and input schema for an advertised `prefix__tool` |
| `stm_proxy_select_chunks` | `key`, `sections[]` | Retrieve selected TOC sections |
| `stm_proxy_read_more` | `key`, `offset?=0`, `limit?` | Continue a progressive response |
| `stm_surfacing_feedback` | `surfacing_id`, `rating?`, `memory_id?`, `ratings?` | Rate surfaced memories. Use either the legacy `rating`/`memory_id` shape or batched `ratings`, never both. |
| `stm_compression_feedback` | `server`, `tool`, `missing`, `kind="other"`, `trace_id?` | Report missing compressed information |

## Full tool metadata

`stm_proxy_describe_tool(name)` reads cached metadata for the exact advertised
`prefix__tool` name, without a host-added `mcp__server__` prefix; it never
executes the upstream tool. It returns:

- `name`: the advertised tool name.
- `description`: full effective instructions, respecting the operator's override
  and the ordinary empty-description name fallback.
- `input_schema`: the input schema with descriptions and examples restored,
  including `_context_query` when advertised by STM.
- `response_hint`: the compression follow-up hint from that advertisement.
- `upstream_description`: the original upstream description, included only when
  an operator override is configured, separately from the effective instructions.

Unknown, hidden, rejected and unregistered tools are unavailable through this
endpoint. Catalogue changes update recovery metadata when registration succeeds;
failed registration cannot expose a newly discovered tool. The existing live
Toolgraph call policy also applies. Reading metadata does not run response
compression or surfacing.

See [description budgets](proxy-config.md#advertised-tool-descriptions) for
`host_description_cap` and recovery hints. Host response-size limits still apply
to this tool's result; this API does not add pagination or bypass them.

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

| Tool | Arguments | Purpose |
|---|---|---|
| `stm_memory_propose` | `content`, `source_ref=""`, `idempotency_key=""` | Submit a pending candidate to a compatible core; never perform a direct durable write |

`stm_memory_propose` is advertised independently when
`MEMTOMEM_STM_FORMATION__ENABLED=true` is set before startup.
