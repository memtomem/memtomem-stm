# Toolgraph Policy Gateway

This guide connects Toolgraph's policy compiler to STM's runtime MCP gateway.
The two packages stay independent: Toolgraph writes one portable JSON artifact;
STM validates and enforces it without importing Toolgraph or opening its graph
database on the call path.

Start in `review`. Rejected tools remain visible and callable while STM records
what strict mode would block. Move to `strict` only after `status` and `explain`
match the intended catalog.

## Prerequisites

- A working STM upstream configuration (`mms doctor` should have no FAILs).
- A Toolgraph graph that crawled those same upstream MCP servers.
- The same agent id and qualified `server::tool` names on both sides.

For a Docker-free producer example, complete Toolgraph's
[beginner guide](https://github.com/memtomem/toolgraph/blob/main/docs/beginner-guide.md).
It creates the `vibe-coder` agent, crawls the bundled `policy-gateway` server,
and authors one eligible and one rejected decision.

## 1. Keep server identities aligned

The simplest setup uses the same name in both configurations:

```bash
mms add policy-gateway \
  --command /absolute/path/to/toolgraph/.venv/bin/python \
  --args "/absolute/path/to/toolgraph/examples/policy_gateway_server.py" \
  --prefix demo \
  --compression none \
  --validate
```

Toolgraph must crawl that upstream as `policy-gateway`; its qualified key is
therefore `policy-gateway::read_note`. STM's public client-facing alias remains
`demo__read_note`. If an existing STM connection key differs from the crawled
name, set `toolgraph.server_name_map` explicitly before applying policy.

## 2. Publish a review bundle first

Run this from the Toolgraph checkout:

```bash
uv run toolgraph policy compile \
  --agent vibe-coder \
  --profile review \
  --output ~/.memtomem/toolgraph/policy-bundle.json
```

Then apply the matching STM profile:

```bash
mms gateway mode review \
  --bundle ~/.memtomem/toolgraph/policy-bundle.json \
  --apply
mms gateway status
mms gateway explain policy-gateway::read_note
mms gateway explain policy-gateway::publish_note
```

`status --json` reports `valid`, the exact bundle digest, graph identity and
generation, and eligible/rejected counts. Automation should inspect `valid`;
status commands remain informational and can return a JSON diagnostic for an
invalid artifact. `explain` reads the bundle only and never calls the upstream.

Restart the MCP client after applying the bundle. STM registers its advertised
tool list when the MCP server starts, so the client should reconnect before
you judge which tools are visible.

## 3. Verify review behavior

In the Toolgraph fixture, both `demo__read_note` and `demo__publish_note` remain
visible under review. Calls are allowed, while `stm_proxy_health` reports the
review would-block count when the MCP client registration sets
`MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=true`. Restart the client after
adding that environment value, then call `stm_proxy_health` from the client.
The underlying gateway status field is `would_block_calls`.

The counter records policy decisions, not raw arguments or results.

## 4. Move to strict

Compile the strict artifact before changing STM's mode, so the bundle profile
and active gateway profile never disagree:

```bash
uv run toolgraph policy compile \
  --agent vibe-coder \
  --profile strict \
  --output ~/.memtomem/toolgraph/policy-bundle.json
mms gateway mode strict --apply
mms gateway status
```

Restart the MCP client again. Rejected, missing, unmapped, or contract-drifted
tools are no longer advertised, and direct calls through an already registered
handler are blocked before telemetry, response-cache lookup, or upstream
dispatch.

## Updating policy safely

1. Crawl the live upstream again when its tool contract changes.
2. Update and ingest the governance manifest.
3. Compile a bundle whose profile matches STM's active profile.
4. Check `mms gateway status` and the relevant `explain` results.
5. Restart STM/the MCP client to refresh the advertised tool list.

An already running STM process checks the bundle before each proxied call, so
a new strict denial blocks immediately and cannot be bypassed by a warm cache.
The external MCP `tools/list` registry is session-stable in this release; its
visibility change appears after restart. A malformed or missing strict bundle
causes startup to fail closed. Review keeps the last known good snapshot and
surfaces degraded status instead.

## Trust and troubleshooting

- `bundle_digest` is the SHA-256 of the exact artifact bytes for identity and
  reporting; it is not a signature. Protect the bundle and config with local
  filesystem permissions.
- `toolgraph_drifted` usually means the live description, input schema, or MCP
  annotations changed after Toolgraph crawled the server.
- `toolgraph_unmapped` usually means STM's upstream connection key does not
  match the server name Toolgraph crawled.
- Agent/profile mismatch is rejected rather than silently degraded.
- STM bundle mode never launches Toolgraph. Toolgraph and Ladybug may be fully
  stopped after the bundle is published.
