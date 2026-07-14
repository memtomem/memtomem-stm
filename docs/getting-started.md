# Getting Started

This guide takes a new installation from zero to one verified proxied tool.
Long-term-memory (LTM) surfacing is optional: the proxy still compresses and
caches upstream MCP responses when no LTM server is configured.

## 1. Install

```bash
pip install memtomem-stm
```

With `uv`, install the global CLI with `uv tool install memtomem-stm` or run it
without installing with `uvx memtomem-stm --help`. The three console scripts
`mms`, `memtomem-stm`, and `memtomem-stm-proxy` are interchangeable. This
guide uses `mms` for operator commands and the explicit `memtomem-stm` server
command in MCP client registrations.

## 2. Add an upstream

The guided path prompts for an upstream and can register STM with your client:

```bash
mms init
```

For a non-interactive filesystem example:

```bash
mms add filesystem \
  --command npx \
  --args "-y @modelcontextprotocol/server-filesystem /home/user/projects" \
  --prefix fs
```

The prefix becomes the public tool namespace, such as `fs__read_file`. It must
be unique and short enough to fit the MCP tool-name limit after the client adds
its own server prefix.

## 3. Register STM with a client

`mms register` can register Claude Code automatically or generate a generic
JSON configuration. Claude Code:

```bash
mms register --mcp claude
# Equivalent manual registration:
claude mcp add memtomem-stm -s user -- memtomem-stm
```

Codex CLI is not an automatic `mms register` target; register it with Codex's
MCP command:

```bash
codex mcp add memtomem-stm -- memtomem-stm
codex mcp list
```

For other JSON-based clients:

```json
{
  "mcpServers": {
    "memtomem-stm": {"command": "memtomem-stm"}
  }
}
```

`mms init` does not discover existing Codex MCP definitions. Also,
`mms import --from codex` populates the separate project registry at
`~/.mms/registry.toml`; it does not add an upstream to
`~/.memtomem/stm_proxy.json`.

## 4. Verify the setup

```bash
mms doctor
```

Setup is complete when `mms doctor` exits 0 and the client lists at least one
proxied tool. WARNs are allowed. An `ltm server` warning only means proactive
memory surfacing is unavailable; it does not disable proxying, compression, or
caching.

Use these commands when more detail is needed:

```bash
mms status   # static config summary
mms list     # configured upstreams
mms health   # live upstream and surfacing probes
```

## 5. Call a proxied tool

Ask the client to use a proxied alias such as `fs__read_file`. Calls made
through a client's built-in `Read`, `Bash`, or similar tools do not cross the
MCP proxy boundary. The optional [`mms hook`](guides/native-hooks.md) observes
some native PostToolUse events, but it is not a replacement for the proxy.

## Next steps

- [한국어: Claude Code와 Codex CLI를 위한 시작 가이드](guides/vibe-coding-getting-started-ko.md)
- [Operate and troubleshoot STM](guides/operations.md)
- [Bridge supported native-tool events](guides/native-hooks.md)
- [Manage project-scoped MCP definitions](guides/project-scoped-mcps.md)
- [Choose compression behavior](compression.md)
- [Configure proactive memory surfacing](surfacing.md)
- [Resume a project with reviewed memory](guides/reviewed-memory-resume.md)
