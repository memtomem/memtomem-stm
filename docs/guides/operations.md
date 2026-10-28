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

### Ollama endpoint failure

Unlike optional LTM surfacing, an Ollama endpoint is checked only after the
effective configuration explicitly selects embedding relevance or
Ollama-backed LLM compression. A dead endpoint or missing configured model is
therefore a `doctor` FAIL. For the default local endpoint
(`http://localhost:11434`), start the service with `ollama serve`, pull the
model named by the `next:` line, and rerun `mms doctor`; a remote or
non-default endpoint's `next:` line instead asks you to verify that endpoint
and its models (plain `ollama` commands would act on the default instance,
not the configured one). For installation,
model selection, or a remote Ollama host, follow the
[local Ollama setup](../compression.md#local-ollama-setup).

### Proxied tools are missing

Run `mms health --names` to check discovery and composed-name overflow, then
confirm the client registered `mms` and restarted or refreshed its MCP list.

### Surfacing is quiet

Check the surfacing section of `mms stats`. Five consecutive non-empty searches
whose candidates remain below `min_score` produce a
`score_ceiling_below_min` diagnostic. On the baseline RRF scale the default
`min_score` admits anything both retrieval legs found, so possible causes for a
persistent ceiling below it include one leg not contributing (a BM25-only LTM,
for example), two legs returning disjoint candidates, Core's post-fusion score
modifiers or non-baseline fusion settings, and an intentionally high threshold. STM
never lowers `min_score` because of this diagnostic; pin a per-tool threshold
only after inspecting the score distribution.

When `MEMTOMEM_STM_SURFACING__USE_DAEMON=true`, also run `mms daemon status`.
`warming` means the shared child/model is still starting; `warm` is ready.
With warm-up disabled, the first eligible call starts the daemon and passes
through unchanged, and the immediately following call may still arrive before
the LTM is warm. Multiple children can be legitimate when configs or protocol
versions differ — including when the MCP server and the shell or hook get
different `MEMTOMEM_STM_*` environments. `status` marks which of the others exit
on their own once idle; `mms daemon stop --all` is for the ones that never do
and that no running STM process still uses.

### Observability MCP tools are absent

The eight observability and admin actions, served by the `stm_admin` tool, are hidden by default to save tool-schema tokens. Set
`MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=true` before server start to advertise
`stm_admin`. CLI diagnostics remain available when it is hidden.

### A native hook is quiet

Run `mms daemon status`, verify the installed host path, and check the host's
capability in the [native-hook guide](native-hooks.md). Cursor and Kimi hooks
are metrics-only by design.

## Recovering from a broken config file

`~/.memtomem/stm_proxy.json` is plain JSON, and hand edits break it in four
ways with sharply different symptoms — the fourth one is silent:

- **Invalid JSON** (truncated file, stray comma): `status`, `list`, and most
  commands fail with the parse position; `doctor` FAILs at the `config JSON`
  check. With `--json`, the commands that exit with the parse position also
  write a `config_invalid` failure document to stdout. Nothing else is
  damaged — fix or restore the file.
- **Unreadable or not UTF-8** (wrong permissions, a file saved in another
  encoding): `status` and `list` exit with the exception type only —
  `Failed to read <path>: PermissionError` or
  `Failed to parse <path>: UnicodeDecodeError`; `doctor` FAILs at `config JSON`
  with `cannot read file: PermissionError` or `not valid UTF-8`. Only
  `mms config validate` prints the full error text, including the byte offset
  of a bad encoding. With `--json`, `status`, `list` and the other commands
  that exit with the type name also write a failure document to stdout
  (`"error": "config_unreadable"` or `"config_invalid"`).
- **Valid JSON, invalid schema** (a wrong *type* — `"enabled": "yes"`): the
  running server silently falls back to env/defaults, and `status`, `list`,
  and `health` print a `fails validation` warning.
- **Valid JSON, unknown key** (a misspelling — `"compresion"`): a different
  and quieter failure. The schema is `extra="ignore"` for forward
  compatibility, so an unknown key is neither an error nor a fallback — the
  setting you meant to change simply never takes effect, and nothing warns.
  `mms config validate` is the only command that reports it.

Recovery order:

1. `mms config validate` — lists *every* error, one line each (line/column for
   JSON errors, `key: message` — dotted for nested sections — for schema
   errors), plus every unknown key. It is the only command that shows each
   schema error's message and reports unknown keys; `status` and `list` also
   count every schema error, but name each by location and error type only
   (`2 validation error(s): upstream_servers.a.prefix (string_type); …`).
2. Fix the named line, or restore a backup. Backups that may already exist:
   - `stm_proxy.json.bak-<UTC>` next to the config — written by every
     `mms tune --apply`. This is the one you can restore from directly
     (`cp` it back over the config; a running proxy hot-reloads it).
   - `~/.memtomem/pruned_upstreams.json` — an append-only record of host
     entries removed by `--prune`. Read it by hand to recover what an
     upstream looked like. It is not a restore path for *this* file, and
     `mms eject` only reads it to *suggest* a `--to` target — it never
     adopts a row automatically, and it cannot run at all while the config
     is unparseable.
3. Re-run `mms config validate` until it prints `OK`, then `mms doctor`.
4. If the file is beyond repair, delete it and re-run `mms init` (or
   `mms add --from-clients` to re-import from host configs).

## Upgrading

Read the target release's [Upgrade notes](../../CHANGELOG.md) first. Most
releases need only the package upgrade and a check:

```bash
uv tool upgrade memtomem-stm                 # uv tool installation
python -m pip install --upgrade memtomem-stm # virtualenv / pip installation
mms version
mms doctor
```

When a note says the daemon protocol changes, or that processes sharing the
pending store must be restarted together (0.7.0 says both), stop the old
processes **before** upgrading. A daemon is found by a fingerprint that
includes the protocol version, so after the upgrade the new build's
`mms daemon stop` and `mms daemon restart` no longer reach a daemon the old
build started.

1. Quit every MCP host that runs STM (Claude Code, Codex, …). A running proxy
   or hook starts a daemon again on its next call.
2. With the old build still installed, run `mms daemon stop`. `daemon stopped`
   means the daemon finished shutting down, and `no running daemon` that none
   was running for this config; in builds after 0.7.0 every other outcome
   that is not confirmed exits 1, so follow its message, and
   `no responsive daemon pid=N; cleaned stale handshake` means no daemon held
   this config's lock. 0.7.0 and earlier also print that message for a daemon
   already shutting down on its own (an idle timeout, say), without the pid,
   and remove the record it came from, so on those builds run
   `mms daemon status` first, note the pid, and check that it is gone (for
   example `ps -p N`). They also exit 0 in two cases
   that are not confirmed: `sent SIGTERM to daemon pid=N` means it only
   signalled the process, so check that the pid is gone before going on; and
   a warning that the endpoint is still listening (Windows) means nothing was
   stopped.
3. Run `mms daemon status`. A daemon a host started usually runs under a
   different config fingerprint than your shell, because `mms register` pins
   host-specific settings in the host's environment, and `status` lists it as
   running under a different config. With the hosts quit, nothing uses it: on
   macOS and Linux, `mms daemon stop --all` sends each listed daemon that
   still accepts connections SIGTERM. In builds after 0.7.0 it then waits for
   each to finish shutting down — and for one listed as not accepting, which
   it never signals, to release its lock — and exits 0 only if all of them
   did; 0.7.0 and earlier do not wait, so note their pids and check that those
   processes are gone. On Windows, `--all` does not signal them (and exits 1
   in builds after 0.7.0 when any listed daemon still accepts connections);
   stop the owning processes from Task Manager. In builds after 0.7.0 a daemon stays in the
   `status` listing until its teardown has removed its handshake, the last
   step of shutting down; in 0.7.0 and earlier it leaves the listing when it
   stops listening, before that, so there an empty listing is not that check.
4. Upgrade every installation whose processes share a pending store
   (`pending_store_path`, `~/.memtomem/pending_selections.db` by default)
   before any of them starts again, then run `mms version` and `mms doctor`.
5. Start the hosts again.

Each check covers the data directory of the config and `MEMTOMEM_STM_*`
environment it runs under; repeat it for any other you use. Do not count on
idle shutdown instead: a daemon exits after `daemon.idle_timeout_seconds`
(900 by default) without requests, and `0` disables it.

Client registration is kept across upgrades; use
`mms register --replace-registration` only when you intend to refresh it.

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
   restores each upstream to its original host client (an entry imported from
   Cursor needs an explicit `--to`, since `mms` never writes those configs);
   plain `mms remove NAME` just drops it from STM.
3. **Client registration** — remove STM itself from the client, e.g.
   `claude mcp remove memtomem-stm` (the exact command is printed by `mms
   eject` when the last upstream leaves).
4. **Daemon** — `mms daemon stop --all` also reaps daemons left by older
   configs.
5. **State on disk** — remove the home data directories: `rm -rf ~/.memtomem
   ~/.mms` (configs, metrics/feedback DBs, locks, logs, prune backups, and
   the `stm_proxy.json.bak-<UTC>` copies each `mms tune --apply` leaves next
   to the config). Two kinds of state live outside them: host-settings
   backups (step 1), and any per-project `<project>/.mms/project.toml`
   written by `mms project init` — that one is a committed file, so remove
   it in the project repo rather than with a blanket `rm`.
6. **Package** — `uv tool uninstall memtomem-stm` or `pip uninstall
   memtomem-stm`.

## Safe changes

- `mms tune` previews recommendations; `--apply` writes after creating a
  timestamped configuration backup.
- `mms hook install`, `mms hook uninstall`, import, and host-sync operations
  default to previews and require `--apply` to write.
- Use `mms eject` to restore an imported upstream to its original host after
  pruning it from direct registration — or, for a Cursor origin, to an
  explicit `--to` target.

See the [gateway CLI reference](../reference/cli-gateway.md),
[surfacing guide](../surfacing.md), and [configuration hub](../configuration.md).
