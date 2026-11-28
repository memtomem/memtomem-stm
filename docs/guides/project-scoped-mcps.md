# Project-Scoped MCP Management

Project-scoped MCP state is separate from the STM proxy configuration:

- `~/.mms/registry.toml` stores imported host MCP definitions.
- `<project>/.mms/project.toml` selects definitions for one project.
- `~/.memtomem/stm_proxy.json` configures upstreams routed through STM.

## Import host definitions

```bash
cd /path/to/project
mms import --from codex          # preview
```

Project-local files are looked up in the current directory, so run every step
there. The preview is a summary and redacts secrets. A repository checkout can
ship project-local files, so before applying, open the file itself
(`.codex/config.toml` for Codex) and check each server's command, arguments and
environment. Apply reads the files again and writes the real values to the
registry:

```bash
mms import --from codex --apply
```

Apply with the same `--from` value you previewed (`all` covers every host). If
the preview reports new entries from project-local files, `--apply` refuses
unless you also pass `--allow-project-configs`. Add the flag only after you have
checked those files:

```bash
mms import --from codex --apply --allow-project-configs
```

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

## Route the project through STM

Routing remains explicit and previews by default:

```bash
mms project route
mms project route --apply
mms doctor
```

Only enabled stdio definitions are eligible. Apply is additive: it validates
the complete proxy config, creates a backup when the config already exists,
and skips name/prefix conflicts instead of replacing another route. Re-running
the command is idempotent.

See the complete [project CLI reference](../reference/cli-projects.md).
