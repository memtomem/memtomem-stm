# Hook and Daemon CLI Reference

## `mms hook`

```text
Usage: mms hook [OPTIONS] [COMMAND] [ARGS]...
```

Bare `mms hook` is the runtime PostToolUse bridge. `--host TEXT` defaults to
`auto`; unknown or value-less input warns and falls back to auto-detection so a
host action is never blocked by a usage error.

Bare `mms hook` also takes the runtime flags that `mms hook install` writes
into the host command. Each one overrides the environment setting in its row
for that invocation only, and `--surfacing-timeout-seconds` also sets the
daemon deadline:

| Flag | Overrides | Meaning |
|---|---|---|
| `--use-daemon` / `--no-daemon` | `MEMTOMEM_STM_HOOK__USE_DAEMON` | Surface through the shared warm daemon, or run in-process |
| `--surfacing-timeout-seconds SECONDS` | `MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS` | LTM search deadline |
| `--daemon-timeout-seconds SECONDS` | `MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS` | Hook-to-daemon deadline; raised to at least one second above `--surfacing-timeout-seconds` whenever that flag is given |
| `--persist-query-text` / `--no-persist-query-text` | `MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT` | Store surfacing query text, or only its hash |

The values arrive as plain strings, so a hand-edited invalid value (a
non-number or a non-positive deadline) is dropped instead of Click exiting 2
inside a host hook. The dropped setting then keeps its ambient or default
value, with one exception: a valid `--surfacing-timeout-seconds` still sets the
daemon deadline to one second above it when `--daemon-timeout-seconds` is
invalid or missing.

```text
mms hook install --host [claude|codex|cursor|kimi]
  [--surfacing-timeout SECONDS] [--daemon|--no-daemon]
  [--inherit-runtime-env] [--apply]
mms hook uninstall --host [claude|codex|cursor|kimi] [--apply]
```

Install and uninstall require a strict host choice. They preview by default,
write only with `--apply`, create a non-clobbering backup, and refuse malformed
host configuration. Install serializes shared-daemon, deadline, and query-text
privacy settings into portable runtime flags. `--inherit-runtime-env` omits
those flags and cannot be combined with the explicit daemon/deadline options.

Current host paths are:

- Claude: `~/.claude/settings.json`
- Codex: `~/.codex/config.toml`
- Cursor: `~/.cursor/hooks.json`
- Kimi: `$KIMI_CODE_HOME/config.toml` or `~/.kimi-code/config.toml`

Claude Bash output replacement requires Claude Code 2.1.121+ and remains
opt-in. Codex officially accepts PostToolUse `additionalContext`, but STM only
surfaces read-like `Bash` calls after `/hooks` approval; `apply_patch` is
metrics-only and Codex output replacement is unsupported. Claude `--bare` and
`--safe-mode` can bypass installed hooks/MCP. See
[Native PostToolUse Hooks](../guides/native-hooks.md) for the full host and
privacy contract.

## `mms daemon`

| Command | Purpose |
|---|---|
| `start` | Spawn the daemon if this config has none |
| `status` | Report pid, port, uptime, and LTM warmth |
| `restart` | Stop and start this config's daemon |
| `stop` | Gracefully stop; `--all` also handles stale fingerprints |
| `run` | Run the server loop; supports `--foreground` and `--detached` |

Daemons are keyed by effective configuration and protocol version. They serve
native hooks plus standalone surfacing when
`MEMTOMEM_STM_SURFACING__USE_DAEMON=true`. Runtime files live under
`MEMTOMEM_STM_DATA_DIR` (default `~/.memtomem`).

See [Native PostToolUse Hooks](../guides/native-hooks.md) for daemon behavior
and troubleshooting.
