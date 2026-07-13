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

## Resume a project with reviewed memory

With a compatible memtomem core, pre-load or approve a project decision, start
a new agent session, and compare surfacing disabled versus enabled. Measure the
number of tool calls and time to the first correct answer, retrieval precision,
duplicate re-surfacing, and surfacing latency.

STM retrieves and delivers the memory. The core owns storage, privacy, scope,
review, and context composition. STM does not automatically turn arbitrary
tool output into durable memory.

## Avoid repeated upstream reads

Register a read-only MCP tool that records its invocation count and adds a
fixed delay. Run one cold call followed by 20 identical calls with caching off
and on. Measure upstream invocation count, cache hit rate, cold/warm latency,
and response equality.

Caching avoids upstream I/O and latency. Model-input reduction comes from the
cached compressed response, not from the cache hit itself. Write-like,
credential-bearing, mixed-content, error, and transient-key responses are not
assumed cacheable.

## Share one local LTM connection across agents

Start several STM clients with identical effective LTM configuration and
compare private surfacing children with
`MEMTOMEM_STM_SURFACING__USE_DAEMON=true`. Measure child count, total RSS,
warm-up time, pass-through during warm-up, and surfacing p95.

The daemon shares the LTM connection. Response caches, feedback, rate limits,
and tuning remain local to each proxy; this is not remote HA or multi-tenant
infrastructure.

## Common evidence rules

- Compare direct upstream, STM passthrough, and STM feature-on separately.
- Record core and STM versions, fixture hashes, tokenizer identity, and config.
- Report answer quality and additional round trips beside any size reduction.
- Label examples as MCP-routed; built-in `Read`, `Bash`, or editor tools may
  bypass the proxy.
- Treat STM's character-derived token estimate as an estimate, not a provider
  billing count.
