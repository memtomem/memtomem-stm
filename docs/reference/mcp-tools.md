# STM MCP Tools

STM advertises five model-facing tools by default and hides eight operator tools
unless `MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=true` is set before startup.
All connected upstream tools are added as `{prefix}__{tool}`.

## Default model-facing tools

| Tool | Arguments | Purpose |
|---|---|---|
| `stm_proxy_describe_tool` | `name`, `part?`, `offset?=0`, `limit?=4000`, `generation?` | Read bounded pages of instructions or input schema for an advertised `prefix__tool` |
| `stm_proxy_select_chunks` | `key`, `sections[]` | Retrieve selected TOC sections |
| `stm_proxy_read_more` | `key`, `offset?=0`, `limit?` | Continue a progressive response |
| `stm_surfacing_feedback` | `surfacing_id`, `rating?`, `memory_id?`, `ratings?` | Rate surfaced memories. Use either the legacy `rating`/`memory_id` shape or batched `ratings`, never both. |
| `stm_compression_feedback` | `server`, `tool`, `missing`, `kind="other"`, `trace_id?` | Report missing compressed information |

## Full tool metadata

`stm_proxy_describe_tool(name)` reads the effective instructions for the exact
advertised `prefix__tool` name, without a host-added `mcp__server__` prefix. It
reads registered metadata and never executes the upstream tool, compression,
or surfacing. The default call returns only the description; request the
schema separately so instruction recovery does not also spend schema tokens.

| Argument | Default | Meaning |
|---|---|---|
| `name` | required | Exact STM `prefix__tool` name |
| `part` | `"description"` | `description`, `input_schema`, or `upstream_description` |
| `offset` | `0` | Character offset into this part's returned source |
| `limit` | `4000` | Requested maximum characters, from 1 through 16,000 |
| `generation` | absent | Snapshot token; required when `offset > 0` |

`description` respects the operator's override and the ordinary empty-source
name fallback. `input_schema` restores descriptions and examples, including
`_context_query` when advertised by STM. `upstream_description` is available
only when `recover_upstream_description` is enabled **and** an override is
configured; requesting it otherwise returns an unavailable error. Selecting
another part never implicitly includes that replaced text.

Every page has these fields:

- `name`, `part`: the requested tool and selected source.
- `text`, `format`: page content and `"text"` or `"json"`. Schema pages contain
  serialized JSON text, not partial schema objects.
- `generation`: the registered metadata snapshot token.
- `offset`, `next_offset`, `total_chars`: character positions in this source.
  `next_offset: null` means complete; otherwise it is the next request's offset.
- `response_hint`: this advertisement's compression follow-up guidance,
  without the joining `|` separator.

For example, start schema recovery with:

```json
{"name": "cedar__search_docs", "part": "input_schema"}
```

When the response has a non-null `next_offset`, call the same tool with the
same `name`, `part`, and `generation`, and set `offset` to that `next_offset`.
Concatenate each page's `text` exactly, in order. For `input_schema`, parse the
combined text as JSON **after** the final page. Individual schema chunks need
not be independently parseable JSON schemas; every response envelope is valid
JSON. Ordinary Unicode is preserved; unencodable lone surrogates in prose use
the project's literal escape convention, while schema JSON preserves them as
JSON escapes. Offsets count characters in the returned, transport-safe source.

The complete MCP tool result is limited to **16,384 UTF-8 bytes**, including
both text and structured representations, escaping, and all page metadata.
The JSON-RPC transport envelope is excluded. `limit` can shorten a page but
cannot raise this ceiling. A page may therefore contain fewer characters than
requested, especially for non-ASCII or heavily escaped content. There is no
source-length cut and no `omitted_chars`: all remaining content can be read
from the same generation ([#1026][i1026], [#1027][i1027]). Host-specific response
limits still apply independently.

Negative offsets, offsets beyond the source, and limits outside 1..16000 are
errors. An offset exactly at the end returns an empty completed page. A result
whose metadata alone cannot fit returns a short error rather than truncating
identifiers or returning a page that cannot advance.

A successful metadata change invalidates the old token: discard accumulated
pages and restart at offset 0 without `generation`. Identical registrations
keep their token; a failed removal that leaves the old registration installed
also keeps it. Tokens are process-local and do not survive restart or removal
and re-registration. Previous generations are not retained.

Unknown, hidden, rejected and unregistered tools are unavailable on **every**
page, including continuations. The live Toolgraph call policy is checked each
time. Failed registration cannot expose new metadata, and shutdown invalidates
all snapshots. These pages use `stm_proxy_describe_tool` itself, not the
compression pipeline's `stm_proxy_read_more` store.

[i1026]: https://github.com/memtomem/memtomem-stm/issues/1026
[i1027]: https://github.com/memtomem/memtomem-stm/issues/1027

See [description budgets](proxy-config.md#advertised-tool-descriptions) for
`host_description_cap` and recovery hints.

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
