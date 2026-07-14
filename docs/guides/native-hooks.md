# Native PostToolUse Hooks

`mms hook` bridges supported host-native PostToolUse events into STM. It is an
observer/postprocessor, not an MCP proxy: native calls do not gain response
caching, upstream retries, progressive reads, or general MCP routing.

## Host capabilities

| Host | Surfaced context | Output replacement | Metrics |
|---|---|---|---|
| Claude Code | Yes | Opt-in Bash compression | Yes |
| Codex CLI | Bash only; verify after hook approval | No | Yes |
| Cursor | No reliable model-visible channel | No | Yes |
| Kimi Code | No reliable model-visible channel | No | Yes |

The hook always fails open. Malformed payloads, timeouts, disabled features,
and internal errors leave the original tool output unchanged.

## Install or remove a hook

Preview is the default; `--apply` performs the write and creates a backup.

```bash
# Preview, then apply
mms hook install --host claude
mms hook install --host claude --apply
mms hook install --host codex
mms hook install --host codex --apply
mms hook uninstall --host codex --apply
```

| Host | Config file |
|---|---|
| Claude Code | `~/.claude/settings.json` |
| Codex CLI | `~/.codex/config.toml` |
| Cursor | `~/.cursor/hooks.json` |
| Kimi Code | `$KIMI_CODE_HOME/config.toml`, otherwise `~/.kimi-code/config.toml` |

Codex requires approving the installed hook through `/hooks`. Its adapter
cannot replace output, and only `Bash` currently reaches the read-like
surfacing allowlist (`apply_patch` can still be observed for metrics). Codex's
standalone `additionalContext` injection is not explicitly guaranteed by its
public hook documentation, so confirm that a memory appears before relying on
it. TOML host files are re-serialized; use the generated `.bak` file if
comments or formatting must be restored.

## Runtime host selection

The installed command includes `mms hook --host <name>`. On the runtime bridge,
`--host` accepts text rather than a strict Choice because a non-zero usage exit
could block a host action. An unrecognized or bare value warns and falls back
to auto-detection. `install` and `uninstall` remain strict operator commands.

Auto-detection cannot distinguish Claude from Codex payloads, so Codex
registrations must keep the explicit `--host codex` argument.

## Daemon and fallback

The default hook path uses the local `mms daemon`, auto-spawning it on first
use. Configure the behavior with:

```bash
export MEMTOMEM_STM_HOOK__USE_DAEMON=true
export MEMTOMEM_STM_HOOK__AUTO_SPAWN=true
export MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS=2.5
export MEMTOMEM_STM_HOOK__FALLBACK=skip  # skip | cold
```

`skip` returns immediately when the daemon is unavailable. `cold` runs the
legacy in-process path and may pay the LTM startup cost inside the hook call.

## Metrics and privacy

Hook metrics are enabled independently of proxy metrics:

```bash
export MEMTOMEM_STM_HOOK__METRICS_ENABLED=true
mms stats --source hook
```

Rows contain source/tool identity, sizes, timing, and compression outcome—not
the native tool's content. Set the variable to `false` to disable recording.
Feedback-event recording, which can retain query metadata, remains separately
opt-in through `MEMTOMEM_STM_HOOK__RECORD_FEEDBACK_EVENTS`.

Claude-only Bash output replacement is also opt-in:

```bash
export MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=true
export MEMTOMEM_STM_HOOK__COMPRESSION__MIN_RETENTION=0.65
```

See the [hook CLI reference](../reference/cli-hooks.md) and the complete
[environment-variable reference](../reference/environment-variables.md).
