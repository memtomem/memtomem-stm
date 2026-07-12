# Hook and Daemon CLI Reference

## `mms hook`

```text
Usage: mms hook [OPTIONS] [COMMAND] [ARGS]...
```

Bare `mms hook` is the runtime PostToolUse bridge. `--host TEXT` defaults to
`auto`; unknown or value-less input warns and falls back to auto-detection so a
host action is never blocked by a usage error.

```text
mms hook install --host [claude|codex|cursor|kimi] [--apply]
mms hook uninstall --host [claude|codex|cursor|kimi] [--apply]
```

Install and uninstall require a strict host choice. They preview by default,
write only with `--apply`, create a non-clobbering backup, and refuse malformed
host configuration.

Current host paths are:

- Claude: `~/.claude/settings.json`
- Codex: `~/.codex/config.toml`
- Cursor: `~/.cursor/hooks.json`
- Kimi: `$KIMI_CODE_HOME/config.toml` or `~/.kimi-code/config.toml`

## `mms daemon`

| Command | Purpose |
|---|---|
| `start` | Spawn the daemon if this config has none |
| `status` | Report pid, port, uptime, and LTM warmth |
| `restart` | Stop and start this config's daemon |
| `stop` | Gracefully stop; `--all` also handles stale fingerprints |
| `run` | Run the server loop; supports `--foreground` and `--detached` |

Daemons are keyed by effective configuration and protocol version. Runtime
files live under `MEMTOMEM_STM_DATA_DIR` (default `~/.memtomem`).

See [Native PostToolUse Hooks](../guides/native-hooks.md) for capabilities,
metrics, privacy, and troubleshooting.
