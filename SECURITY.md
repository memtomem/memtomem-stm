# Security Policy

## Reporting Vulnerabilities

Please report security issues via [GitHub private vulnerability advisory](https://github.com/memtomem/memtomem-stm/security/advisories/new). Do NOT open public issues for vulnerabilities.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.3.x   | Yes       |
| < 0.3.0 | No        |

## Threat Model

memtomem-stm is an MCP proxy gateway. Its threat surface differs from a server-facing application:

- **Transport**: Default communication is stdio with the AI client (Claude Code, Cursor, etc.); the MCP proxy server itself opens no network port. When the built-in `mms hook` surfacing path is used, an eligible hook call may auto-spawn a local surfacing daemon (`hook.use_daemon`/`hook.auto_spawn` default on) that binds an ephemeral port on `daemon.host` — **`127.0.0.1` (loopback) by default** — and authenticates every connection with a per-start random token rather than network ACLs. Keep `daemon.host` on loopback: a non-loopback `daemon.host` (e.g. `0.0.0.0`, or the empty string — both bind every interface) is rejected at config load unless you explicitly opt in with `MEMTOMEM_STM_DAEMON__ALLOW_NON_LOOPBACK=true`, which deliberately exposes the token-guarded daemon on that interface.
- **Trust boundary**: memtomem-stm trusts the AI client (local process) and the upstream MCP servers and telemetry endpoints it is configured to contact. Only configure endpoints you trust. An operator-configured SSE/streamable-HTTP upstream, network LTM, hosted compression or relevance provider, Langfuse host, or OTLP collector is an outbound path and may be able to reach internal-network services. `mms import --apply` and `mms host sync --apply` additionally gate on the *source* of a candidate: entries discovered in project-local config files (`.mcp.json`, `.cursor/mcp.json`) under the current directory — files an untrusted repository checkout can ship — are refused unless `--allow-project-configs` is passed, so a checkout cannot silently register a command that later runs with your privileges.
- **Data at rest**: The optional selective-compression `PendingStore` defaults to in-memory (`pending_store="memory"`). Other persisted stores — the response cache (`proxy_cache.db`, enabled by default), metrics (`proxy_metrics.db`), and feedback — are local-only SQLite files under `~/.memtomem/`. No store is remote or shared across hosts.

## Security Measures

### Content handling

- **Untrusted surfaced memories**: Every untrusted field rendered inside the
  Markdown-compatible `<surfaced-memories>` envelope is flattened to one line
  and escaped immediately before rendering. Controls (including bidi), HTML
  delimiters/entities, Markdown delimiters, and compatibility confusables are
  inert; memory ids are rendered as copyable tokens only when they match the
  documented conservative id grammar. The pinned preamble tells consumers to
  treat retrieved memories as data, not instructions. This structural boundary
  does not eliminate the residual risk that ordinary natural-language text may
  persuade a model; keep write-tool surfacing disabled and review retrieved
  context before consequential actions.
- **Sensitive content auto-detection**: Responses containing patterns that look like secrets (API keys, tokens, private keys) are detected and excluded from the response cache. Index exclusion applies only when an embedder is explicitly supplied through the library-mode `ProxyManager(index_engine=...)` extension; the bundled `mms` server does not automatically index tool responses into LTM.
- **Write-tool skip**: Memory surfacing is automatically disabled for upstream tools that mutate state, reducing the risk of injecting stale context into destructive operations.
- **CLI output redaction**: `mms status --json` and `mms list --json` mask every `env` and `headers` value (`<REDACTED>`, keys preserved) since that machine-readable output is routinely piped to scripts, CI logs, or issue comments. The human-readable `status`/`list` tables never print `env`/`headers` at all; read the on-disk config directly when a value is genuinely needed.

### Resilience

- **Circuit breaker**: Per-upstream circuit breaker isolates failures; a misbehaving upstream cannot cascade into other proxied tools.
- **Retry with backoff**: Transient errors are retried with exponential backoff; persistent failures trip the breaker.
- **Rate limit + query cooldown**: Surfacing requests to the LTM server are rate-limited and cooled down per query to prevent recall loops.

### Data security

- **No unsafe deserialization**: No pickle, no unsafe YAML loading
- **No command injection**: Subprocess spawns (the surfacing daemon, and stdio MCP child servers such as the LTM-consult client and the optional tool-graph eligibility provider) use static or config-derived argv lists — `shell=True` is never set, so no untrusted input is interpolated into a shell. No `eval` / `exec` on input.
- **SQL injection**: All queries in the optional SQLite `PendingStore` use parameterized statements

## Best Practices

- Never commit API keys or credentials — use MCP client `env` blocks for configuration
- Keep `stm_proxy.json` out of version control if it contains sensitive upstream server paths
- If using the SQLite `PendingStore`, store the DB on local disk (not a shared network drive)
- Review the list of upstream MCP servers you proxy — memtomem-stm inherits the trust level of each upstream you configure
- Treat every configured HTTP/SSE/LTM/Langfuse/OTLP URL as trusted operator configuration. Apply egress controls when STM must not reach internal services, and review redirect and credential-forwarding behavior of the selected client SDK/provider.
- Set conservative relevance thresholds for surfacing to avoid leaking LTM contents into unrelated contexts
- If using Langfuse tracing, review what data your traces capture and configure redaction accordingly

## Out of Scope

memtomem-stm does NOT include:
- Web UI (no XSS, CSP, or clickjacking concerns)
- An arbitrary end-user URL-fetch tool. Operator-configured outbound endpoints remain in scope for egress and SSRF-style network-boundary review.
- Inbound HTTP listener by default

If you run memtomem-stm behind an HTTP transport, standard HTTP hardening (TLS, auth, rate limiting at the edge) is your responsibility.
