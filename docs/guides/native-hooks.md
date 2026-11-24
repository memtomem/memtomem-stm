# Native PostToolUse Hooks

`mms hook` bridges supported host-native PostToolUse events into STM. It is an
observer/postprocessor, not an MCP proxy: native calls do not gain response
caching, upstream retries, progressive reads, or general MCP routing.

## Host capabilities

| Host | Surfaced context | Output replacement | Metrics |
|---|---|---|---|
| Claude Code | Yes | Opt-in Bash compression; requires Claude Code 2.1.121+ | Yes |
| Codex CLI | Bash only after `/hooks` approval | No | Yes |
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

New installs serialize the shared-daemon route, the effective surfacing
deadline, a hook-to-daemon deadline at least one second larger, and
non-persistent query text directly in the hook command. This avoids relying on
environment inheritance from a terminal into a GUI host. Pin a larger search
budget, opt out of the daemon, or retain the legacy ambient-environment model
with:

```bash
mms hook install --host claude --surfacing-timeout 12 --apply
mms hook install --host claude --no-daemon --apply
mms hook install --host claude --inherit-runtime-env --apply
```

Every host config above is user-scope, so the command install writes runs for
every project. Its launcher depends on the nearest `pyproject.toml` in the
directory install runs from or one of its four parents (the search stops at the
first one found). When that file is STM's own (`name = "memtomem-stm"`) or
lists `memtomem-stm` as a dependency or optional dependency, the launcher is
`uv run --directory <that directory> memtomem-stm`; otherwise it is the bare
`memtomem-stm` console script. Unless that directory is STM's source checkout
(a `memtomem-stm` project with `src/memtomem_stm/__init__.py`), every hook call
then runs from that project's environment, and install says so in its output.
To get the bare launcher (for example with `uv tool install memtomem-stm`),
re-run install from a directory where no such `pyproject.toml` is found.

Reinstalling updates the command install wrote earlier in place and keeps its
daemon, timeout and query-text settings. A command install did not write,
such as one that names `mms` or begins with `env MEMTOMEM_STM_...`, is left
as it is, and install adds its own beside it; see
[`mms hook install`](../cli.md).
The runtime flags are listed in the
[hook CLI reference](../reference/cli-hooks.md#mms-hook). Unknown host-specific fields on the hook
entry are preserved.

| Host | Config file |
|---|---|
| Claude Code | `~/.claude/settings.json` |
| Codex CLI | `~/.codex/config.toml` |
| Cursor | `~/.cursor/hooks.json` |
| Kimi Code | `$KIMI_CODE_HOME/config.toml`, otherwise `~/.kimi-code/config.toml` |

Codex requires approving the installed hook through `/hooks`. Its adapter
cannot replace output, and only `Bash` currently reaches STM's read-like
surfacing allowlist (`apply_patch` can still be observed for metrics). Codex's
official PostToolUse contract adds `additionalContext` as extra developer
context; `updatedMCPToolOutput` remains unsupported. See the official
[Codex hook documentation](https://learn.chatgpt.com/docs/hooks#posttooluse).
TOML host files are re-serialized; use the generated `.bak` file if comments or
formatting must be restored.

Claude Bash output replacement uses `PostToolUse.updatedToolOutput` for native
tools and therefore requires Claude Code 2.1.121 or later. Older clients can
still keep the fail-open hook path, but do not rely on model-visible output
replacement. See Claude Code's
[PostToolUse decision control](https://code.claude.com/docs/en/hooks#posttooluse-decision-control).

Claude Code startup modes can intentionally bypass the integration:
[`--bare`](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode)
does not load hooks, MCP servers, or auto memory, while `--safe-mode` disables
hooks and MCP servers along with other project customization. Exit those modes
before diagnosing an apparently missing STM registration.

## Runtime host selection

The installed command includes `memtomem-stm hook --host <name>`. On the runtime
bridge, `--host` accepts text rather than a strict Choice because a non-zero
usage exit could block a host action. An unrecognized or bare value warns and falls back
to auto-detection. `install` and `uninstall` remain strict operator commands.

Auto-detection recognizes Codex when the payload carries a non-empty `turn_id`.
An otherwise Claude/Codex-ambiguous payload falls back to Claude, so installed
Codex registrations still keep the explicit `--host codex` argument.

## Daemon and fallback

The default hook path uses the local `mms daemon`, auto-spawning it on first
use. Configure the behavior with:

```bash
export MEMTOMEM_STM_HOOK__USE_DAEMON=true
export MEMTOMEM_STM_HOOK__AUTO_SPAWN=true
export MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS=3
export MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS=4
export MEMTOMEM_STM_HOOK__FALLBACK=skip  # skip | cold
```

`skip` returns immediately when the daemon is unavailable. `cold` runs the
legacy in-process path and may pay the LTM startup cost inside the hook call.
Neither applies when the daemon answers that it is busy or that the request
ran out of time: that call surfaces nothing, with no fallback and no spawn
request. A burst of more than 64 simultaneous connections is different: the
daemon closes the oldest ones whose first frame it has not yet read, without
answering, so those calls take the unreachable path above.
`daemon.max_pending_requests` and that fixed 64-connection budget are described
under [Surfacing Daemon](../configuration.md#surfacing-daemon).
Keep the hook-to-daemon deadline above the surfacing deadline; managed installs
enforce a one-second margin automatically.

## Metrics and privacy

Hook metrics are enabled independently of proxy metrics:

```bash
export MEMTOMEM_STM_HOOK__METRICS_ENABLED=true
mms stats --source hook
```

Rows contain source/tool identity, sizes, timing, and compression outcome—not
the native tool's content. Set the variable to `false` to disable hook metrics.
`MEMTOMEM_STM_HOOK__RECORD_FEEDBACK_EVENTS` is a separate feedback-loop gate:
when false it disables the rating prompt, durable-demotion reads, and auto-tune
feedback processing. Surfacing telemetry events are still written with the
configured query persistence policy.

Claude's `updatedToolOutput` changes what the model sees; it does not guarantee
that the original output was absent from Claude Code telemetry recorded before
the hook ran. Do not treat output replacement as a telemetry-redaction
boundary.

Claude-only Bash output replacement is also opt-in:

```bash
export MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=true
export MEMTOMEM_STM_HOOK__COMPRESSION__MIN_RETENTION=0.65
```

Replacement also requires the hook command to pass `--host claude`, which
`mms hook install --host claude --apply` writes. A bare `mms hook`
auto-detects the host, and auto-detection cannot tell Claude from Codex, so it
never replaces output. On a call that auto-detection routes to the Claude
adapter (its fallback; a payload with a non-empty Codex `turn_id` goes to the
Codex adapter and gets no warning) and that compression would have attempted (a
Bash result that passes every check made before the compressor runs: safe,
structured, over `max_chars`, not already compressed, and a budget larger than
the sentinel and its newline), the hook still passes the output through,
prints a warning to stderr, and returns it as `systemMessage`, which Claude Code
and Codex document as a warning shown to the user. Claude Code does not show the stderr of a hook
that exits 0 outside its debug log.

To fix it, add `--host claude` to the hook command where it is registered; for
a command you registered by hand, that is a hand edit.
`mms hook install --host claude --apply` writes a correct registration into
the user settings file (`$CLAUDE_CONFIG_DIR/settings.json`, otherwise
`~/.claude/settings.json`), but not into Claude Code's project or local
settings. It replaces only a command install wrote earlier
(`memtomem-stm hook --host claude`, bare or after `uv run --directory <dir>`,
with install's own setting flags or none; see
[`mms hook install`](../cli.md)),
and carries that command's daemon, timeout and query-text settings over. Any
other STM hook command, such as `mms hook`,
`MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED=1 mms hook` or one behind `env`,
stays in place next to the new one; install and uninstall name its location
(`hooks.PostToolUse[i].hooks[j]`) when it looks like STM's hook, and you edit
or remove such a registration by hand.

See the [hook CLI reference](../reference/cli-hooks.md) and the complete
[environment-variable reference](../reference/environment-variables.md).
