# Project and Host CLI Reference

## Registry import

`mms import --from <host|all>` previews definitions discovered in supported
host configs. `--apply` stores them in `~/.mms/registry.toml`; project-local
sources require `--allow-project-configs`.

Secrets are redacted in plans but retained in the applied local registry.
Conflicts require an explicit resolution and dangerous environment keys are
never adopted silently.

## Project selection

| Command | Purpose |
|---|---|
| `mms project init` | Create `.mms/project.toml` in the current project |
| `mms project show` | Show the resolved project and enabled entries |
| `mms project list` | List known project selections |
| `mms project enable NAMES...` | Enable registry entries for this project |
| `mms project disable NAMES...` | Disable entries for this project |

## Host synchronization

`mms host` inspects differences between registry/project state and supported
host configuration, then previews or applies the selected synchronization.
Adopting project-local commands requires explicit authorization; removal and
sidecar backfill do not adopt a new command.

This subsystem is independent of `~/.memtomem/stm_proxy.json`. Registry import
does not automatically proxy an MCP server through STM.

See [Project-Scoped MCP Management](../guides/project-scoped-mcps.md) for the
recommended workflow.
