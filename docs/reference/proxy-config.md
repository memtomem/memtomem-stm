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
  "max_upstream_bytes": 41943040,
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

`max_upstream_bytes` caps the complete inbound MCP result envelope at 40 MiB
by default. Text, images, `structuredContent`, result `_meta`, and error
payloads all count. An oversized result is rejected before caching,
compression, indexing, or surfacing. The cap is measured on the decoded
message's compact JSON size, not on the result re-measured after parsing —
validation fills in defaults the wire may omit, so the two differ. This is
separate from `max_upstream_chars`, which is a text-only shaping guard.

## Upstream servers

Each server selects `stdio`, `sse`, or `streamable_http`, a unique prefix, and
the matching command/URL fields. HTTP headers and stdio environment values may
contain secrets and must not be copied into issue reports. Connection fields
reconnect on hot reload; prefix and circuit-breaker construction settings
remain restart-bound.

Most per-server and per-tool values override global proxy settings. Two
description-shaping fields compose instead, so a per-server value cannot relax a
stricter global one: description limits take
`min(server, global, host_description_cap)`, omitting the host term when unset
(see [Advertised tool descriptions](#advertised-tool-descriptions)), and
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
The parts appear in this order:

```
[proxied] <source text, truncated to fit><compression hint, if any><recovery hint, if needed>
```

- **`[proxied] `** is prepended to every proxied tool. It is not configurable.
- **The source text** is the tool's own description, or
  `tool_overrides.<tool>.description_override` when set. An override replaces
  the *source* of the text, not the budgeting: it is truncated on the same path
  as upstream text. Truncation prefers a sentence boundary; failing that a word
  boundary, marked with an ellipsis; failing that a cut mid-word, also marked.
  The ellipsis is dropped when the remaining budget cannot hold both it and at
  least one character of text — under four characters, the text is cut raw. The
  sentence boundary used is the last one inside the budget, and it is used only
  when it keeps at least 90% of that budget; text whose tail carries no sentence
  separator, such as a bullet list or a code block, therefore gets the marked
  word-boundary cut instead of retreating past the whole block. So text that was
  shortened without an ellipsis is always within 10% of the cap; a cut that gives
  up more than that always carries the ellipsis that says so. What no cut can
  express is how much text lies beyond the cap — a sentence cut inside that 10%
  reads as a finished description.
- **The convention suffix** is appended only by strategies that change how the
  agent must interact with the response — see
  [Compression Strategies](../compression.md). It follows the compression
  resolved by the same rule calls use: the per-tool override if set, otherwise
  the per-server `compression` field, otherwise the global
  `default_compression`. Omitting `compression` on a server hands the decision
  to that global value for advertisement and for calls alike; typing
  `compression: auto` explicitly is a choice, and keeps the global default out
  of both. (`hybrid` carries a suffix only with its default
  `tail_mode: "toc"`; a `truncate` tail is self-contained and needs none.)
  One case still advertises no suffix even though the response may need one:
  `auto` picks a strategy per response, so no static description can be
  accurate for every call — it can select `hybrid`, whose default
  `tail_mode: "toc"` returns a TOC the agent has to retrieve from. Under `auto`
  that TOC names `stm_proxy_select_chunks` in the response text itself, which is
  what carries the instruction there. A response that needs retrieval generally
  carries such an inline hint — though a TOC fitted to a tight `max_chars`
  abbreviates it to `select_chunks key=…`, dropping the registered tool name.
  The suffix is what says so *before* the call, where it can inform the
  decision to make one at all.

  Unlike the fields above, the suffix resolves off the **live** config at each
  rebuild rather than the connect-time snapshot, since it predicts what a call
  will do and calls read those values live.

`max_description_chars` is an exact cap on that whole assembled string, prefix
included. It is set globally and per server, and the effective budget is
`min(server, global, host_description_cap)`, omitting the host term when unset.
Raising only the global value does not widen a stricter per-server one. Server
and global limits default to `4000` and require at least `32`, which leaves room
for the prefix and some surviving text.

The default is a sanity bound against a pathological upstream, not a token
budget — a policy choice with headroom over the descriptions sampled in #1015,
not a derived threshold. Lower it when the client pays for every advertised
character on every request, and note that the convention suffix rides at the end
of the advertisement: a cap large enough that the client truncates first leaves
that client naming no follow-up tool, just as a cap too small to fit the suffix
does.

The optional **global** `host_description_cap` declares the client's independent
per-tool description limit. It defaults to `null` (unknown) and accepts integers
of at least 32. For a host measured to cap descriptions at 2048 characters:

```json
{
  "max_description_chars": 4000,
  "host_description_cap": 2048
}
```

Merge these fields into the existing proxy config and restart the proxy. STM
does not detect the limit or assume every host uses 2048. When unset, STM-only
cuts receive recovery hints, but a subsequent host-only cut may lose the hint.
Host limits expressed in bytes or tokens need a conservatively chosen character
budget; this setting uses the same character count as `max_description_chars`.

If source text is cut, or schema descriptions/examples are removed, STM appends
` | full: stm_proxy_describe_tool` when it fits. The model can call that tool
with the exact advertised `prefix__tool` name to recover the instructions this
cap discarded. The default call returns a description page; choose
`part="input_schema"` to recover the schema separately. Recovery pages share a
16,384-byte whole-MCP-result budget and can be continued using `next_offset`
and `generation`, so there is no source-length cut. See
[full tool metadata](mcp-tools.md#full-tool-metadata) for arguments and schema
reassembly. The tool is always advertised, including when observability tools
are hidden. An uncut description with an unchanged schema needs no hint.

The compression hint is reserved first, the recovery hint second, then the
remaining budget goes to source text. Neither hint is shortened. At tiny budgets
one or both hints may be absent; `mms doctor` warns about the lost hints.

A configured host limit is not automatically the binding one. Doctor recommends
full-metadata retrieval *instead of* a budget edit only when the host limit sits
at or under both STM levels, where no edit could recover a character. When an STM
level is the lower one, it still recommends raising that level — to whichever is
smaller, the cap a lossless advertisement needs or the host limit, never past
the host limit. When the host limit is the smaller of those two, it also reports
the cap a lossless advertisement would have needed, so the shortfall is visible
as the gap between two caps rather than as a count of surviving text. Its advice
reflects declared limits and discovered lengths, not measured host behavior or
model adoption.

When the budget is tight the convention suffix wins over upstream text, because
it names the follow-up tool the response requires. If even the suffix alone
cannot fit, it is dropped whole rather than cut short. If no body survives
beside a suffix, the space the suffix opens with is dropped: the prefix already
ends in one.

Source text is stripped before it is budgeted, so whitespace-only text counts as
none. When the selected source text is empty — an upstream that supplies no
description, and no override to stand in for it — the prefixed tool name is
advertised in its place (`[proxied] fs__read_file`). It is the only text the
proxy can supply without inventing a claim about what the tool does, and it is
budgeted like any other source text: truncated to what the cap leaves after the
prefix and the suffix, never exempt from the cap.

The cap is applied where a tool is advertised, so it describes what
registration produces rather than what the config file currently says. The
global value is read live but takes effect at the next registration — a restart
or an upstream catalogue change. The per-server value comes from the
connect-time snapshot, so it takes effect when that upstream next connects; a
`tools/list_changed` refresh replaces the catalogue but not the configuration
it is advertised under.

`mms doctor` reports, per upstream, how many of the discovered descriptions
exceed this cap and by how much (the cut itself can remove more, since it
retreats to a boundary), the smallest cap that would carry every description and
its required hints whole, whether each hint fits today, and which configured
limit binds. Raising one STM budget cannot widen a lower server, global or host
limit. It reads the config file, so it describes what the next start would
advertise rather than what a running proxy holds; it does not measure any cap
the client's host applies afterwards.

`stm_proxy_stats` and `stm_proxy_health` report counts, and the `mms` commands
report configuration and health — tool names among it, but never the advertised
description text (`mms doctor` reports lengths and counts only). To see exactly what a client receives, list tools from the
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
