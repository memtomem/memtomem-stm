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
compression, and caching remain available, so `doctor` treats this as WARN. It
does not change Claude Code or Codex client-managed memory.

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

## Recovering from a broken config file

`~/.memtomem/stm_proxy.json` is plain JSON, and hand edits can break it two
ways with different symptoms:

- **Invalid JSON** (truncated file, stray comma): `status`, `list`, and most
  commands fail with the parse position; `doctor` FAILs at the `config JSON`
  check. Nothing else is damaged — fix or restore the file.
- **Valid JSON, invalid schema** (wrong type, misspelled key): the running
  server silently falls back to env/defaults, and `status`/`list` print a
  `fails validation` warning. Misspelled keys are *ignored* at runtime; only
  `mms config validate` reports them, with the dotted path.

Recovery order:

1. `mms config validate` — lists *every* error, one line each (line/column for
   JSON errors, `key: message` — dotted for nested sections — for schema
   errors), plus every unknown key. This is the only command that reports
   them all; `status` and `list` name just the first.
2. Fix the named line, or restore a backup. Backups that may already exist:
   - `stm_proxy.json.bak-<UTC>` next to the config — written by every
     `mms tune --apply`.
   - `~/.memtomem/pruned_upstreams.json` — the prune backup log; `mms eject`
     can rebuild host entries from it.
3. Re-run `mms config validate` until it prints `OK`, then `mms doctor`.
4. If the file is beyond repair, delete it and re-run `mms init` (or
   `mms add --from-clients` to re-import from host configs).

## Uninstalling completely

There is no single wipe command; the pieces are independent and each step is
optional depending on how far you want to roll back.

```bash
mms hook uninstall --host claude --apply   # repeat per host you installed
mms list                                   # see what to eject or remove
mms daemon stop --all
```

1. **Hooks** — `mms hook uninstall --host <host> --apply` removes the
   PostToolUse hook after backing up the host settings file. Every install
   and uninstall leaves that backup **next to the host's own config**, not
   under STM's directories — `~/.claude/settings.json.bak`,
   `~/.codex/config.toml.bak`, and so on (`.bak.1`, `.bak.2`, … for later
   writes). They are verbatim copies of a host config, which can carry API
   keys, so delete them here rather than assuming step 5 catches them.
2. **Upstreams** — if you pruned direct registrations, `mms eject NAME` first
   restores each upstream to its original host client; plain `mms remove NAME`
   just drops it from STM.
3. **Client registration** — remove STM itself from the client, e.g.
   `claude mcp remove memtomem-stm` (the exact command is printed by `mms
   eject` when the last upstream leaves).
4. **Daemon** — `mms daemon stop --all` also reaps daemons left by older
   configs.
5. **State on disk** — remove the data directories: `rm -rf ~/.memtomem
   ~/.mms` (configs, metrics/feedback DBs, locks, logs, prune backups, and
   the `stm_proxy.json.bak-<UTC>` copies each `mms tune --apply` leaves next
   to the config). Host-settings backups are **not** here — see step 1.
6. **Package** — `uv tool uninstall memtomem-stm` or `pip uninstall
   memtomem-stm`.

## Safe changes

- `mms tune` previews recommendations; `--apply` writes after creating a
  timestamped configuration backup.
- `mms hook install`, `mms hook uninstall`, import, and host-sync operations
  default to previews and require `--apply` to write.
- Use `mms eject` to restore an imported upstream to its original host after
  pruning it from direct registration.

See the [gateway CLI reference](../reference/cli-gateway.md),
[surfacing guide](../surfacing.md), and [configuration hub](../configuration.md).
