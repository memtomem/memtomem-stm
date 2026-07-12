# Project-Scoped MCP Management

Project-scoped MCP state is separate from the STM proxy configuration:

- `~/.mms/registry.toml` stores imported host MCP definitions.
- `<project>/.mms/project.toml` selects definitions for one project.
- `~/.memtomem/stm_proxy.json` configures upstreams routed through STM.

## Import host definitions

```bash
mms import --from all          # preview
mms import --from codex --apply
```

Preview output redacts secrets. Apply writes the real values to the registry.
Project-local definitions are refused unless `--allow-project-configs` is
explicitly passed.

## Initialize and select a project

```bash
cd /path/to/project
mms project init
mms project enable filesystem github
mms project show
```

Commit the project marker when teammates should share the selection. The user
registry remains local and may contain credentials.

## Inspect and synchronize hosts

Use `mms host` to compare the registry/project selection with supported host
configuration. Mutating operations retain their preview/apply boundary; review
the plan before allowing a host-config write.

These commands do not implicitly add an entry to the STM proxy. Use `mms add`
or the guided import in `mms init` when the server should route through STM.

See the complete [project CLI reference](../reference/cli-projects.md).
