# Operations and Troubleshooting

## Recommended diagnostic order

```bash
mms config validate
mms status
mms health
mms doctor
mms stats
```

- `config validate` catches parse errors and unknown keys without starting the
  proxy.
- `status` reports static state; it does not connect to upstreams.
- `health` performs staged upstream and LTM probes.
- `doctor` converts the same evidence into PASS/WARN/FAIL actions.
- `stats` reads durable compression and surfacing stores.

## Common states

### LTM warning on a fresh install

An unavailable LTM server disables proactive memory surfacing only. Proxying,
compression, and caching remain available, so `doctor` treats this as WARN.

### Proxied tools are missing

Run `mms health --names` to check discovery and composed-name overflow, then
confirm the client registered `mms` and restarted or refreshed its MCP list.

### Surfacing is quiet

Check the surfacing section of `mms stats`. Five consecutive non-empty searches
whose candidates remain below `min_score` produce a
`score_ceiling_below_min` diagnostic. This commonly indicates a BM25-only LTM
score scale or an intentionally high threshold. STM never lowers `min_score`
because of this diagnostic; pin a per-tool threshold only after inspecting the
score distribution.

When `MEMTOMEM_STM_SURFACING__USE_DAEMON=true`, also run `mms daemon status`.
`warming` means the shared child/model is still starting; `warm` is ready.
With warm-up disabled, the first eligible call starts the daemon and passes
through unchanged, and the immediately following call may still arrive before
the LTM is warm. Multiple children can be legitimate when configs or protocol
versions differ; `mms daemon stop --all` removes pinned stale-version daemons.

### Observability MCP tools are absent

The eight observability and admin tools are hidden by default to save tool-schema tokens. Set
`MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=true` before server start to expose
them. CLI diagnostics remain available when they are hidden.

### A native hook is quiet

Run `mms daemon status`, verify the installed host path, and check the host's
capability in the [native-hook guide](native-hooks.md). Cursor and Kimi hooks
are metrics-only by design.

## Safe changes

- `mms tune` previews recommendations; `--apply` writes after creating a
  timestamped configuration backup.
- `mms hook install`, `mms hook uninstall`, import, and host-sync operations
  default to previews and require `--apply` to write.
- Use `mms eject` to restore an imported upstream to its original host after
  pruning it from direct registration.

See the [gateway CLI reference](../reference/cli-gateway.md),
[surfacing guide](../surfacing.md), and [configuration hub](../configuration.md).
