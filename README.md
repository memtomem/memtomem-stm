# memtomem-stm

**Official website & docs: [https://memtomem.com](https://memtomem.com)**

[![PyPI](https://img.shields.io/pypi/v/memtomem-stm)](https://pypi.org/project/memtomem-stm/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-green)](https://python.org)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
[![CLA](https://img.shields.io/badge/CLA-required-green)](CLA.md)

> 🚧 **Alpha** — APIs and defaults may change between 0.x minor releases. Feedback and issue reports are especially welcome: [Issues](https://github.com/memtomem/memtomem-stm/issues) · [Discussions](https://github.com/memtomem/memtomem-stm/discussions).

Spend fewer tokens. Remember more. Ship faster.

memtomem-stm is an MCP proxy that can substantially reduce the model-visible
size of large tool responses and surface **cross-session memory from a
configured memtomem LTM** — with no changes to your upstream MCP servers. The
actual reduction depends on response shape, compression strategy, and whether
the call travels through MCP; verify it against your workload with the
repository's deterministic `bench_qa` suite.

It sits between your AI agent and its upstream MCP servers, compressing tool responses, caching repeated calls, and automatically surfacing relevant context from prior sessions via a memtomem LTM server.

**What memtomem-stm does:**
- **Cuts token spend on repeated reads** — compresses and caches tool responses, so the agent doesn't re-pay for the same file or search result. Works with Claude Code, Cursor, Claude Desktop, or any MCP client.
- **Carries context across sessions** — surfaces prior decisions from memtomem LTM automatically, so the agent picks up where it left off rather than re-discovering what it already knew.
- **Drops in front of any MCP server** — adds compression, caching, and observability as a proxy layer, without changes to upstream code.

```mermaid
flowchart TB
    Agent["Agent<br/>(Claude Code, Cursor, …)"]
    subgraph STM["memtomem-stm (STM)"]
        Pipe["CLEAN → COMPRESS → SURFACE"]
    end
    LTM[("memtomem LTM<br/>(MCP server)")]
    FS["filesystem<br/>MCP server"]
    GH["github<br/>MCP server"]
    Other["…any MCP server"]

    Agent -->|MCP| STM
    STM <-->|MCP: stdio / SSE / HTTP| FS
    STM <-->|MCP| GH
    STM <-->|MCP| Other
    STM <-.->|surfacing<br/>via MCP| LTM
```

## Installation

```bash
pip install memtomem-stm
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install memtomem-stm     # install mms / memtomem-stm as global CLI tools
uvx memtomem-stm --help          # or run without installing
uv pip install memtomem-stm      # or install into the active environment
```

memtomem-stm is **independent**: it has no Python-level dependency on memtomem core. To enable proactive memory surfacing, point STM at a running memtomem MCP server (or any compatible MCP server) — communication happens entirely through the MCP protocol.

| Connected LTM capability | STM behavior |
| --- | --- |
| No core | Proxying, compression, and caching remain available; LTM surfacing is disabled |
| No `context_compose` advertisement, or schema 0/1 | Legacy `mem_search` surfacing |
| `context_compose` schema 2 | Optional scoped Pinned Context composition |
| `context_compose` schema 3 | Pinned composition plus adjacent context-window chunks |
| `context_compose` schema 4+ | Schema 3 behavior plus score-scale and reranker metadata |
| `candidate_propose` schema 1+ | Optional review-first proposal submission |

Core 0.3.8 is the tested legacy baseline, 0.3.9 first advertises schema 2,
Core 0.3.10 first advertises schema 3, and Core 0.3.12 first advertises schema
4. Runtime behavior always follows the connected server's advertised
capability, not its package version. The released-Core compatibility smoke also
covers the current Core 0.3.13 release.

## Upgrade

```bash
uv tool upgrade memtomem-stm                 # uv tool installation
python -m pip install --upgrade memtomem-stm # virtualenv / pip installation
mms version
mms doctor
```

Read the target release's [Upgrade notes](https://github.com/memtomem/memtomem-stm/blob/main/CHANGELOG.md)
before upgrading. Restart only the daemon for the current configuration with
`mms daemon restart` when an upgrade note requires it. Client registration is
kept unchanged by default; use `mms register --replace-registration` only when
you intentionally want to refresh it.

## Quick Start

All three installed commands—`mms`, `memtomem-stm`, and
`memtomem-stm-proxy`—resolve to the same CLI and MCP server entrypoint. The
shortest first-success path is:

```bash
mms init --demo --client auto  # no-network read-only first-success path
mms doctor    # exit 0 (WARNs allowed) means the proxy setup is usable
```

Restart the selected client and ask it to call `demo__demo_search`, for example
with `{"query": "cache"}`. A client may display the same MCP wire tool as
`mcp__memtomem-stm__demo__demo_search`; `demo__demo_search` is the alias STM
advertises, while the leading `mcp__<client-server>__` portion is added only by
some client UIs.

The filesystem path is separate from the bundled demo. Register it before
asking for `fs__read_file`:

```bash
mms add filesystem \
  --command npx \
  --args "-y @modelcontextprotocol/server-filesystem /home/user/projects" \
  --prefix fs
mms register
mms doctor
```

Your client should now list `fs__read_file` (possibly displayed with a client
prefix as `mcp__memtomem-stm__fs__read_file`). An LTM warning is expected when
no memtomem server is configured; it disables memory surfacing only, not
proxying, compression, caching, or Claude Code/Codex's own client-managed
memory.

Continue with the complete
[Getting Started guide](https://github.com/memtomem/memtomem-stm/blob/main/docs/getting-started.md),
including manual client registration, imports, verification, and next steps.
Korean-speaking Claude Code and Codex CLI users can follow the
[한국어 바이브코딩 시작 가이드](https://github.com/memtomem/memtomem-stm/blob/main/docs/guides/vibe-coding-getting-started-ko.md).

## What STM proxies — and what it doesn't

STM is an MCP proxy: it sees a tool call only if the client routes that call through the MCP protocol. Coverage depends on **how your client invokes the tool**, not on what the tool does.

**STM sees:** any MCP server you register with `mms add` — STM advertises each
tool on the MCP wire as `<prefix>__<tool>`, while some clients display it as
`mcp__<client-server>__<prefix>__<tool>` — plus LTM surfacing calls to a
configured memtomem server.

**STM does NOT see through the MCP proxy path:**
- **Claude Code's built-in tools** — `Read`, `Write`, `Edit`, `Bash`, `Grep`, `Glob`, `WebFetch`. They run inside the client and never reach an MCP server, so their token spend is invisible to STM and unaffected by compression or caching.
- **Cursor / Windsurf / Claude Desktop built-ins** — same principle: anything the client provides natively bypasses the MCP layer.
- **Sub-agent built-in calls** — the parent's MCP wiring is inherited, but built-in tool calls inside an `Agent` / `Task` invocation stay client-internal.

`mms hook` is an optional PostToolUse bridge for that client-internal path,
available for Claude Code, Codex CLI, Cursor, and Kimi Code. Claude and Codex
can receive surfaced LTM context; Cursor and Kimi are metrics-only. Claude can
also opt into guarded Bash compression. Native calls still do not gain proxy
response caching, upstream retries, progressive reads, or general MCP routing,
and the hook always fails open. See the
[native-hook guide](https://github.com/memtomem/memtomem-stm/blob/main/docs/guides/native-hooks.md)
for the capability matrix, installation paths, daemon behavior, and privacy.

For multi-agent fleets, standalone surfacing can opt into the same daemon with
`MEMTOMEM_STM_SURFACING__USE_DAEMON=true`, avoiding one private LTM child per
agent while keeping proxy feedback, caching, and tuning local.

**STM does not automatically write tool responses back to LTM.** Surfacing
reads from LTM via MCP. The opt-in review-first formation integration may
submit an explicit pending candidate to a capable core, but it cannot create
durable memory before core-side approval.

Claude Code auto memory and Codex local memories are separate, client-managed
stores. Registering STM as MCP, installing `mms hook`, or running `mms import
--from codex` does not automatically index or synchronize those stores. See
[Which memory layer does what?](https://github.com/memtomem/memtomem-stm/blob/main/docs/surfacing.md#which-memory-layer-does-what)
for the ownership boundary and explicit, read-only Core ingest options.

To bring file or shell operations under STM, register an MCP server that exposes
them and steer the agent toward the proxied alias instead of the built-in. This
is the same boundary every MCP proxy lives within—not one specific to STM.

## Project-scoped MCPs (`mms project` + `mms import`)

A separate registry lets a project select which MCP definitions it wants to
expose. `~/.mms/registry.toml` and project `.mms/project.toml` files remain
independent of STM's `~/.memtomem/stm_proxy.json`. Preview the additive route
plan with `mms project route`, then use `mms project route --apply` to copy the
selected stdio definitions into the proxy with provenance and conflict checks. See
[Project-Scoped MCP Management](https://github.com/memtomem/memtomem-stm/blob/main/docs/guides/project-scoped-mcps.md).

## Tutorial notebooks

> **Try it without wiring into your AI client first.** A [quickstart Jupyter notebook](https://github.com/memtomem/memtomem-stm/blob/main/notebooks/01_quickstart_proxy_setup.ipynb) registers upstream MCP servers, calls proxied tools, verifies selective compression, and reads `stm_proxy_stats` end-to-end. Clone the repo, `uv sync`, and `uv run jupyter lab notebooks/` — no external services needed.

## Documentation

| Guide | Topic |
|-------|-------|
| [Documentation hub](https://github.com/memtomem/memtomem-stm/blob/main/docs/README.md) | First success → scenarios → operations → reference → architecture |
| [Getting started](https://github.com/memtomem/memtomem-stm/blob/main/docs/getting-started.md) | Install → register → doctor → first proxied tool |
| [한국어 바이브코딩 시작 가이드](https://github.com/memtomem/memtomem-stm/blob/main/docs/guides/vibe-coding-getting-started-ko.md) | Claude Code·Codex CLI 설치 → 연결 → 첫 proxied tool |
| [Operations](https://github.com/memtomem/memtomem-stm/blob/main/docs/guides/operations.md) | Diagnose setup, upstreams, surfacing, and hooks |
| [Native hooks](https://github.com/memtomem/memtomem-stm/blob/main/docs/guides/native-hooks.md) | Host capabilities, installation, daemon, and metrics |
| [Surfacing](https://github.com/memtomem/memtomem-stm/blob/main/docs/surfacing.md) | How agents recall prior context automatically |
| [Compression](https://github.com/memtomem/memtomem-stm/blob/main/docs/compression.md) | All 10 strategies — pick the right one for your content |
| [Caching](https://github.com/memtomem/memtomem-stm/blob/main/docs/caching.md) | Skip repeated work with response caching |
| [Configuration](https://github.com/memtomem/memtomem-stm/blob/main/docs/configuration.md) | Configuration sources and reference map |
| [Selection telemetry](https://github.com/memtomem/memtomem-stm/blob/main/docs/selection-telemetry.md) | Opt-in JSONL log of tool selection + execution outcomes |
| [OTLP span export](https://github.com/memtomem/memtomem-stm/blob/main/docs/otlp-export.md) | Opt-in OpenTelemetry span export with body-free attributes |
| [Use cases](https://github.com/memtomem/memtomem-stm/blob/main/docs/use-cases.md) | Reproducible scenarios and honest measurement boundaries |
| [Reviewed project resume](https://github.com/memtomem/memtomem-stm/blob/main/docs/guides/reviewed-memory-resume.md) | Project-local Pinned Context, visible adjacent context, and optional review-first memory |
| [CLI](https://github.com/memtomem/memtomem-stm/blob/main/docs/cli.md) | Command-family and MCP-tool reference map |
| [Environment variables](https://github.com/memtomem/memtomem-stm/blob/main/docs/reference/environment-variables.md) | Complete startup/runtime variable inventory and precedence |
| [Architecture decisions](https://github.com/memtomem/memtomem-stm/blob/main/docs/adr/README.md) | Accepted contracts, status, and deferral gates |

STM advertises four model-facing MCP tools by default. Eight observability and
admin tools (`stm_proxy_stats`, `stm_surfacing_stats`, etc.)
are hidden unless `MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=true` is set,
which keeps eager-loading clients from paying schema tokens for rarely used
operator tools.

## Compatibility & deprecation policy

memtomem-stm is alpha (0.x): behavior and defaults may change between minor
releases while the design settles — defaults have flipped before (e.g.
`injection_mode` `prepend` → `append` in 0.1.24). Every such change is flagged
inline in [CHANGELOG.md](https://github.com/memtomem/memtomem-stm/blob/main/CHANGELOG.md)
with a `**Behavior change**:` marker and — for releases after 0.1.31 — is also
summarized up front in that release's **Upgrade notes** block; skim that block
before upgrading (older releases record behavior changes inline only).
Machine-readable surfaces (`--json` output shapes, exit codes) only change
with an Upgrade notes entry; additive keys may appear at any time, so parse by
key, not by shape equality. Deprecated CLI commands or flags keep working with
a warning for at least one minor release before removal.

## Development

```bash
uv sync                                                    # install dev deps
uv run pytest -m "not bench_qa_meta and not bench_qa_llm_judge and not bench_qa_sweep and not bench_qa_drift and not bench_qa_perf"   # tests (CI filter)
uv run ruff check src && uv run ruff format --check src    # lint (required)
uv run mypy src                                            # typecheck (required)
```

CI runs the same commands on every PR via `.github/workflows/ci.yml`. Lint (`ruff check` + `ruff format --check`), mypy, and tests must pass.

## License

[Apache License 2.0](LICENSE). Contributions are accepted under the terms of the [Contributor License Agreement](CLA.md).
