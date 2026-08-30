# Proxy Configuration Reference

The file at `~/.memtomem/stm_proxy.json` is a `ProxyConfig` document. It
contains proxy, upstream, compression, cache, metrics, exposure, telemetry, and
toolgraph settings. It does not contain root, surfacing, formation, hook,
daemon, Langfuse, or OTLP settings; those are covered by the
[environment-variable reference](environment-variables.md).

## Representative configuration

```json
{
  "enabled": true,
  "advertise_context_query": true,
  "token_estimation_mode": "unicode",
  "cache": {
    "tool_annotation_policy": "strict"
  },
  "upstream_servers": {
    "filesystem": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
      "prefix": "fs",
      "compression": "auto",
      "max_result_tokens": 2000,
      "selective": {
        "json_depth": 1,
        "min_section_chars": 50
      },
      "tool_overrides": {
        "read_file": {
          "description_override": "Read a project file through STM",
          "compression": "hybrid"
        }
      }
    }
  }
}
```

The example is intentionally representative, not exhaustive. Unknown keys are
preserved for forward compatibility but reported by `mms config validate`.

## Upstream servers

Each server selects `stdio`, `sse`, or `streamable_http`, a unique prefix, and
the matching command/URL fields. HTTP headers and stdio environment values may
contain secrets and must not be copied into issue reports. Connection fields
reconnect on hot reload; prefix and circuit-breaker construction settings
remain restart-bound.

Most per-server and per-tool values override global proxy settings. Two
description-shaping fields compose instead, so a per-server value cannot relax a
stricter global one: `max_description_chars` takes `min(server, global)` (see
[Advertised tool descriptions](#advertised-tool-descriptions)), and
`strip_schema_descriptions` takes `server or global`, so a global `true` cannot
be switched off for one upstream. A tool override can set
compression/cache budgets and `description_override`, which supplies the
description text without changing the callable name — it is budgeted like any
upstream text, not inserted verbatim.
`token_estimation_mode="unicode"` makes a configured token budget inspect the
actual response; the backwards-compatible default is `static`.
`advertise_context_query=true` is a proxy-level opt-in that adds the optional
proxy-only `_context_query` field to object input schemas without forwarding it
or mutating the upstream contract.

## Advertised tool descriptions

The description a client receives for a proxied tool is assembled, not copied.
Three parts compose it, in this order:

```
[proxied] <upstream text, truncated to fit><convention suffix, if any>
```

- **`[proxied] `** is prepended to every proxied tool. It is not configurable.
- **The source text** is the tool's own description, or
  `tool_overrides.<tool>.description_override` when set. An override replaces
  the *source* of the text, not the budgeting: it is truncated on the same path
  as upstream text. Truncation prefers a sentence boundary; failing that a word
  boundary, marked with an ellipsis; failing that a cut mid-word, also marked.
  The ellipsis is dropped when the remaining budget cannot hold both it and at
  least one character of text — under four characters, the text is cut raw. A
  boundary counts only if it falls in the last two thirds of the budget, so
  text whose only boundary comes early is cut rather than shortened drastically.
- **The convention suffix** is appended only by strategies that change how the
  agent must interact with the response — see
  [Compression Strategies](../compression.md). It follows the same compression
  the call path resolves: the per-tool override if set, otherwise the
  per-server `compression` field, otherwise the global `default_compression`.
  Omitting `compression` on a server hands the decision to that global value
  for advertisement and for calls alike; typing `compression: auto`
  explicitly is a choice, and keeps the global default out of both.
  One case still advertises no suffix even though the response may need one:
  `auto` picks a strategy per response, so no static description can be
  accurate for every call — it can select `hybrid`, whose default
  `tail_mode: "toc"` returns a TOC the agent has to retrieve from. Under
  `auto` the response envelope, not the description, carries that instruction.

`max_description_chars` is an exact cap on that whole assembled string, prefix
included. It is set globally and per server, and the effective budget is
`min(server, global)`: raising only the global value does not widen a stricter
per-server one. Both default to `200` and both require at least `32`, which
leaves room for the prefix and some surviving text.

When the budget is tight the convention suffix wins over upstream text, because
it names the follow-up tool the response requires. If even the suffix alone
cannot fit, it is dropped whole rather than cut short. When the selected source
text is empty — an upstream that supplies no description and no override to
stand in for it — nothing survives to be truncated, so what is advertised is the
prefix plus whatever suffix fits.

The cap is applied where a tool is advertised, so it describes what
registration produces rather than what the config file currently says. The
global value is read live but takes effect at the next registration — a restart
or an upstream catalogue change. The per-server value comes from the
connect-time snapshot, so it takes effect when that upstream next connects; a
`tools/list_changed` refresh replaces the catalogue but not the configuration
it is advertised under.

`stm_proxy_stats` and `stm_proxy_health` report counts, and the `mms` commands
report configuration and health — tool names among it, but never the advertised
description text. To see exactly what a client receives, list tools from the
client itself.

## Compression sections

- `cleaning` removes low-value response noise.
- `selective` builds retrievable TOCs. `json_depth` controls JSON flattening;
  `min_section_chars` inlines very short sections rather than advertising
  unhelpful retrieval entries.
- `hybrid` combines a retained head with selective retrieval.
- `progressive` stores lossless continuation chunks.
- `llm` controls optional external/local summarization.

See [Compression Strategies](../compression.md) for selection and fallback
semantics.

## Cache, telemetry, and exposure

Cache eligibility follows tool annotations plus explicit tool/server overrides.
Metrics, progressive-read state, selection telemetry, relevance/exposure, and
toolgraph blocks each have independent enable/retention settings. See
[Caching](../caching.md) and [Selection Telemetry](../selection-telemetry.md).

`toolgraph.source` selects either the backwards-compatible one-shot `stdio`
consult or the portable `bundle` enforcement path. Bundle mode reads
`toolgraph.bundle_path`, requires its agent and profile to match
`toolgraph.agent_id` and `exposure.profile`, and rechecks the artifact before
proxy filtering and calls. A new denial gates calls immediately; restart the
STM MCP session to rebuild the client-visible registered tool list. Use
`mms gateway status`, `explain`, and `mode` for the operator-facing workflow.

## Validation and hot reload

Run `mms config validate` before restarting or applying a generated change.
The running proxy hot-reloads safe call-time settings. Connection identity
changes reconnect on the next uncached call; restart-bound settings are called
out by validation and the operational guides.
