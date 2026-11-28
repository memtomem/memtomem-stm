# Gateway CLI Reference

All three installed scripts resolve to the same Click command group:
`mms`, `memtomem-stm`, and `memtomem-stm-proxy`.

```text
Usage: mms [OPTIONS] [COMMAND] [ARGS]...
```

A bare invocation on a TTY prints help. With piped stdin it starts the MCP
server, which lets any entrypoint be registered with an MCP client.

## Setup and registration

| Command | Purpose |
|---|---|
| `mms init` | Guided first-time setup, optional probe, and client registration |
| `mms register` | Register an existing STM configuration with a client |
| `mms add` | Add one upstream; supports stdio, SSE, and Streamable HTTP |
| `mms add --import` | Select existing host definitions through the guided importer |
| `mms list` | List configured upstreams without probing them |
| `mms remove NAME` | Remove an upstream from STM only |
| `mms prune` | Remove matching direct host registrations after backup |
| `mms eject` | Restore an imported host entry and remove it from STM |

Not every command accepts `--config`; use the command's own `--help` output as
the option source of truth. Where the flag exists but is not typed, the CLI
resolves `MEMTOMEM_STM_PROXY__CONFIG_PATH` before the default path, matching
the proxy server (explicit `--config` > env `CONFIG_PATH` > default).

`mms register` writes the proxy path plus managed shared-daemon, surfacing
timeout, and query-text privacy values into new client registrations. Existing
registrations are keep-by-default; use `--replace-registration` to refresh
those managed values explicitly.

## Diagnostics and control

| Command | Purpose |
|---|---|
| `mms config validate` | Report parse, schema, and unknown-key failures |
| `mms status` | Static path/enabled/server-count summary |
| `mms health` | Staged upstream and LTM connectivity probes |
| `mms doctor` | PASS/WARN/FAIL setup verdict with next actions |
| `mms surfacing NAME [on|off]` | Inspect or persist an upstream surfacing toggle |
| `mms stats` | Read durable compression/surfacing statistics |
| `mms tune` | Preview or apply per-tool compression recommendations |
| `mms selection replay` | Evaluate selection telemetry and the labelled corpus without applying config |
| `mms selection feedback` | Attach a user-correction or operator-override label to one recorded tool selection |
| `mms version` | Print the installed version |

## Toolgraph policy gateway

Use the portable bundle path when Toolgraph should remain a control plane and
STM should be the runtime enforcement gateway:

```bash
mms gateway mode review --apply
mms gateway status
mms gateway explain server::tool
```

`mode` is preview-only unless `--apply` is present. It aligns
`exposure.profile` and `toolgraph.query_profile`, enables `source: "bundle"`,
and leaves bundle publication to `toolgraph policy compile`. `status --json`
validates and identifies the exact artifact; `explain` shows one qualified
tool decision without calling the upstream. Applying `strict` warns if the
target bundle is missing because the next server start will fail closed.
Bundle changes gate proxied calls immediately; restart STM/the MCP client to
refresh which tools appear in the external MCP `tools/list`. See the
[Toolgraph Policy Gateway guide](../guides/toolgraph-policy-gateway.md) for the
review-first workflow.

`status` and `explain` read the configuration the way the server starts:
`MEMTOMEM_STM_*` values in the command's own environment apply over the file,
and `env_overrides` in `--json` (an `environment overrides:` line otherwise)
names the gateway fields they set, such as `toolgraph.agent_id` or
`exposure.profile`. A runtime configuration the server could not start from is
reported as `config_invalid`. So is a file that fails validation as merged: the
server then starts from the environment and defaults alone, and these commands
report the failure instead of describing that fallback. In
`status --json`, `enabled` is the Toolgraph switch (`toolgraph.enabled`) and
`proxy_enabled` the proxy switch (`enabled`); `explain --json` carries both
(`toolgraph_enabled`, `proxy_enabled`) with `profile`, and its human output says
when either switch is off. The environment is the command's, not an MCP client
registration's: to check what a registration's `env` block does, run the
command with those variables set. To check the file alone, run it without
`MEMTOMEM_STM_*` variables and confirm `env_overrides` is empty. Two more cases
differ from startup: a file whose `upstream_servers` is not an object fails
here even when the environment would replace it, and an empty
`MEMTOMEM_STM_PROXY__CONFIG_PATH` selects the default file here, while the
server tries the working directory, cannot read it as a file, and falls back to
the environment and defaults.

`mms --version` and `mms version` are equivalent. `mms stats --source mcp`
selects proxied calls; `--source hook` selects native-hook metrics.

## Output contracts

Commands offering `--json` emit one JSON document for well-formed invocations,
including operational errors. Click usage errors remain plain stderr with exit
2 and no JSON stdout. Consumers must parse by key because additive fields may
appear without a behavior-change release.

Mutating commands use a configuration write lock and preserve unknown config
keys. Preview/apply commands do not write until `--apply` is supplied.

`mms selection replay --json` is a read-only, stable report surface. Production
logs contribute observational joins and execution diagnostics; counterfactual
ranking uses the packaged sanitized corpus. See
[Offline Tool-Selection Evaluation](../selection-evaluation.md).

For workflows and troubleshooting, see [Getting Started](../getting-started.md)
and [Operations](../guides/operations.md). Run `mms <command> --help` for the
complete live flag list.
