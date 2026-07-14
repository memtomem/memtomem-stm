# Proxy Configuration Reference

The file at `~/.memtomem/stm_proxy.json` is a `ProxyConfig` document. It
contains proxy, upstream, compression, cache, metrics, exposure, telemetry, and
toolgraph settings. It does not contain root, surfacing, hook, daemon, or
Langfuse settings; those are covered by the
[environment-variable reference](environment-variables.md).

## Representative configuration

```json
{
  "enabled": true,
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

Per-server and per-tool values override global proxy settings. A tool override
can set compression/cache budgets and `description_override`, which replaces
the advertised proxied-tool description without changing the callable name.

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
tool listing and calls. Use `mms gateway status`, `explain`, and `mode` for the
operator-facing workflow.

## Validation and hot reload

Run `mms config validate` before restarting or applying a generated change.
The running proxy hot-reloads safe call-time settings. Connection identity
changes reconnect on the next uncached call; restart-bound settings are called
out by validation and the operational guides.
