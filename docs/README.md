# memtomem-stm documentation

Start with one verified proxied call, then add only the capabilities you need.
memtomem-stm remains useful as a compression and caching proxy when no
long-term-memory server is configured.

## First success

- [Getting started](getting-started.md) — install STM, configure the bundled
  demo or a real upstream, register a client, and verify one call.
- [한국어 바이브코딩 시작 가이드](guides/vibe-coding-getting-started-ko.md) —
  Claude Code와 Codex CLI에서 첫 MCP 프록시 호출을 확인합니다.
- [Runnable notebook](../notebooks/01_quickstart_proxy_setup.ipynb) — exercise
  proxying, selective compression, and stats without changing user state.

## Scenarios

- [Use cases](use-cases.md) — reproducible workloads and measurement limits.
- [Reviewed project resume](guides/reviewed-memory-resume.md) — combine
  project-local Pinned Context, adjacent retrieval context, and review-first
  proposals.
- [Project-scoped MCPs](guides/project-scoped-mcps.md) — route selected project
  MCP definitions through STM.
- [Toolgraph policy gateway](guides/toolgraph-policy-gateway.md) — enforce a
  reviewed tool-eligibility bundle.

## Configure and operate

- [Configuration map](configuration.md) — choose the authoritative reference
  for each configuration domain.
- [Environment variables](reference/environment-variables.md) — complete
  startup and runtime environment reference.
- [Proxy JSON](reference/proxy-config.md) — file-backed proxy configuration.
- [Operations and troubleshooting](guides/operations.md) — diagnose config,
  upstream, surfacing, daemon, and hook failures.
- [Native hooks](guides/native-hooks.md) — bridge supported PostToolUse events.
- [OTLP span export](otlp-export.md) — enable body-free OpenTelemetry spans.

## Behavior reference

- [Compression](compression.md) and [caching](caching.md)
- [Proactive memory surfacing](surfacing.md)
- [MCP tools](reference/mcp-tools.md) and [CLI](cli.md)
- [Selection telemetry](selection-telemetry.md) and
  [selection evaluation](selection-evaluation.md)

## Architecture and contribution

- [Architecture decisions](adr/README.md)
- [Contributing](../CONTRIBUTING.md), [security policy](../SECURITY.md), and
  [changelog](../CHANGELOG.md)

The root `README.md` is also rendered on PyPI, so it uses canonical GitHub
links. Pages under this directory use relative links so local clones and forks
remain navigable.
