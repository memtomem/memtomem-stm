# Reproducible Use Cases

memtomem-stm is a local-first context-efficiency layer for MCP traffic. These
scenarios separate what compression, caching, and LTM surfacing each prove.
They do not assume that client-native tools pass through the MCP proxy.

## Read only the useful parts of a large response

Use a deterministic document MCP server and compare the same query under
`compression=none`, `selective`, and `progressive`.

Measure exact response characters, a named tokenizer's input tokens, whether
the question remains answerable, additional `stm_proxy_select_chunks` or
`stm_proxy_read_more` calls, and end-to-end p95 latency. Selective and
progressive retain a way to retrieve source content; they do not guarantee
that an agent will request every omitted section.

Place a named "needle" fact in a late section and assert both exact retention
and answerability. Run the same fixture without a query and with an advertised
`_context_query`; this distinguishes ordinary head bias from query-aware
quality. Treat a missing needle as a release-gate failure even if the aggregate
compression ratio improved.

## Resume a project with reviewed memory

With a core that advertises `context_compose` schema 2 or later, pre-load a
project decision, start a new agent session, and compare surfacing disabled
versus enabled. Schema 3 also makes the nearest adjacent context visible.
Measure the number of tool calls and time to the first correct answer, retrieval precision,
duplicate re-surfacing, and surfacing latency.

STM retrieves and delivers the memory. The core owns storage, privacy, scope,
review, and context composition. STM does not automatically turn arbitrary
tool output into durable memory.

Follow the copy/paste [reviewed project resume guide](guides/reviewed-memory-resume.md)
for the complete project-local Pinned Context and optional review-first flow.

## Avoid repeated upstream reads

Register a read-only MCP tool that records its invocation count and adds a
fixed delay. Run one cold call followed by 20 identical calls with caching off
and on. Measure upstream invocation count, cache hit rate, cold/warm latency,
and response equality.

Caching avoids upstream I/O and latency. Model-input reduction comes from the
cached compressed response, not from the cache hit itself. Write-like,
credential-bearing, mixed-content, error, and transient-key responses are not
assumed cacheable. Successful text responses with JSON-safe
`structuredContent` or result `_meta` should hit cache v4 and reproduce the
envelope exactly.

## Share one local LTM connection across agents

Start several STM clients with identical effective LTM configuration and
compare private surfacing children with
`MEMTOMEM_STM_SURFACING__USE_DAEMON=true`. Measure child count, total RSS,
warm-up time, pass-through during warm-up, and surfacing p95.

The daemon shares the LTM connection. Response caches, feedback, rate limits,
and tuning remain local to each proxy; this is not remote HA or multi-tenant
infrastructure.

Use `mms daemon status --json` to record `queue.active`, `queue.queued`, queue
capacity, and cumulative busy rejections beside latency percentiles. The
current daemon serializes access to one warm MCP session; queue telemetry is
the evidence needed before increasing concurrency or introducing a session
pool.

## Common evidence rules

- Compare direct upstream, STM passthrough, and STM feature-on separately.
- Record core and STM versions, fixture hashes, tokenizer identity, and config.
- Report answer quality and additional round trips beside any size reduction.
- Label examples as MCP-routed; built-in `Read`, `Bash`, or editor tools may
  bypass the proxy.
- Treat static and Unicode runtime token estimates as estimates, not provider
  billing counts.
