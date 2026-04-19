# Security Policy

## Reporting Vulnerabilities

Please report security issues via [GitHub private vulnerability advisory](https://github.com/memtomem/memtomem-stm/security/advisories/new). Do NOT open public issues for vulnerabilities.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |
| < 0.1.0 | No        |

## Threat Model

memtomem-stm is an MCP proxy gateway. Its threat surface differs from a server-facing application:

- **Transport**: Default communication is stdio with the AI client (Claude Code, Cursor, etc.). No network port is opened unless explicitly configured.
- **Trust boundary**: memtomem-stm trusts the AI client (local process) and the upstream MCP servers it is configured to proxy. Only configure upstream servers you trust.
- **Data at rest**: Response cache and `PendingStore` default to in-memory. The SQLite shared backend is local-only.

## Security Measures

### Content handling

- **Sensitive content auto-detection**: Responses containing patterns that look like secrets (API keys, tokens, private keys) are detected and excluded from the response cache and from being indexed into LTM.
- **Write-tool skip**: Memory surfacing is automatically disabled for upstream tools that mutate state, reducing the risk of injecting stale context into destructive operations.

### Resilience

- **Circuit breaker**: Per-upstream circuit breaker isolates failures; a misbehaving upstream cannot cascade into other proxied tools.
- **Retry with backoff**: Transient errors are retried with exponential backoff; persistent failures trip the breaker.
- **Rate limit + query cooldown**: Surfacing requests to the LTM server are rate-limited and cooled down per query to prevent recall loops.

### Data security

- **No unsafe deserialization**: No pickle, no unsafe YAML loading
- **No command injection**: No `subprocess` / `eval` / `exec` with user input
- **SQL injection**: All queries in the optional SQLite `PendingStore` use parameterized statements

## Best Practices

- Never commit API keys or credentials — use MCP client `env` blocks for configuration
- Keep `stm_proxy.json` out of version control if it contains sensitive upstream server paths
- If using the SQLite `PendingStore`, store the DB on local disk (not a shared network drive)
- Review the list of upstream MCP servers you proxy — memtomem-stm inherits the trust level of each upstream you configure
- Set conservative relevance thresholds for surfacing to avoid leaking LTM contents into unrelated contexts
- If using Langfuse tracing, review what data your traces capture and configure redaction accordingly

## Out of Scope

memtomem-stm does NOT include:
- Web UI (no XSS, CSP, or clickjacking concerns)
- URL fetching (no SSRF concerns)
- Inbound HTTP listener by default

If you run memtomem-stm behind an HTTP transport, standard HTTP hardening (TLS, auth, rate limiting at the edge) is your responsibility.
