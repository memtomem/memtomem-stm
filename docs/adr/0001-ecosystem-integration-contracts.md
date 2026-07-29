# 0001 — Ecosystem integrations are per-boundary contracts, not a generic adapter

Status: Accepted · Date: 2026-07-26

## Context

STM already exchanges data with one sibling ecosystem tool in production:
Toolgraph, via two distinct surfaces — a peer-produced portable policy bundle
(consumed by `src/memtomem_stm/proxy/toolgraph_bundle.py`) and a live stdio
MCP consult (`src/memtomem_stm/proxy/toolgraph_provider.py`). Integrations
with further sibling tools are planned: tracegraph (consumes telemetry STM
would produce), agent-guard (a request-time content-bearing consult), vigil,
and syncmill.

The question this ADR answers: should STM introduce a standard adapter — a
generic provider protocol, registry, or unified export format — for these
exchanges?

The planned exchanges are heterogeneous in direction, transport, lifecycle,
and contract ownership. Even the shipped Toolgraph integration is not one
pattern: the bundle is a versioned durable artifact STM only consumes, while
the stdio consult is a peer-owned, currently unversioned live response shape.
A single abstraction spanning these would conflate incompatible directions
and lifecycles, and would be designed against consumers whose requirements
exist only in README form.

## Decision

Each ecosystem integration gets a **per-boundary contract** at its natural
domain boundary, classified under one of the exchange profiles below. There
is no generic provider registry, no shared adapter base class, and no unified
telemetry export **until the corresponding deferral gate is satisfied** — the
gates authorize exactly those architectures later; the decision is "not yet",
not "never".

An **ordinary proxied MCP upstream needs no ecosystem adapter at all**: STM's
existing upstream pipeline (`UpstreamServerConfig`) is the integration.
syncmill's read-only `serve` gateway is this case.

**Versioning follows the contract owner's declared convention.** Integer
`schema_version` is required for STM-owned contracts; peers that declare one
(the policy bundle) are validated against it. A peer-owned surface with no
declared version (the Toolgraph stdio consult today) is consumed as-is and
flagged as unversioned in the matrix. Four selection modes exist; choose per
boundary, do not unify:

1. **Owner-declared version** — durable JSON contracts (policy bundle,
   selection log records: integer `schema_version`).
2. **Exact protocol match** — closed, coupled peers where discovery and
   framing version together (the daemon wire protocol).
3. **Negotiated capability schemas** — independently deployed live MCP peers
   (the LTM core handshake in `src/memtomem_stm/surfacing/mcp_client.py`).
4. **Adopt the external standard** — externally standardized formats
   (OTLP/OpenInference for outbound telemetry).

This ADR is the single authority for the deferral-gate *criteria*. Stage
ordering and status are roadmap state and live in the tracking issue (#789),
which links back to the gate names here.

## Common invariants

Only these apply to every exchange:

- **No Python-level dependency on the peer.** The artifact or wire format is
  the compatibility boundary (same rule as the `memtomem` core invariant in
  `CLAUDE.md`).
- **Explicit degradation policy** — what happens when the peer is absent,
  slow, or malformed is decided and documented per boundary, never implicit.
- **Explicit privacy classification** — what content may cross the boundary,
  and which screening applies before it does.

Failure reporting is **tiered**, not uniform:

- *Propagating adapter boundaries* (a failure the caller must act on) use
  typed exception classes — e.g. `ToolgraphUnreachableError` /
  `ToolgraphProtocolError` in `src/memtomem_stm/proxy/toolgraph_provider.py`.
- *Exchange boundaries* surface machine-readable outcomes — e.g. the
  selection log's suppressed-write counters.
- *Internal performance-only caches* may degrade fault-as-miss with operator
  logging, provided the behavior is documented — the Toolgraph consult cache
  (`src/memtomem_stm/proxy/toolgraph_cache.py`), which also deliberately
  skips privacy scanning for its non-content payloads, conforms to this tier
  only.

## Exchange profiles

Profiles are scoped by **contract ownership**; obligations attach to the role
STM actually plays.

| Profile | Reference | STM role | Contract rules |
|---|---|---|---|
| Immutable whole-file artifact | Toolgraph policy bundle | Consumer | Owner-declared version; hand-written v1 contract/version/scope/digest validation at runtime, with the vendored JSON Schema (`src/memtomem_stm/data/policy-bundle.schema.json`) serving as the producer-side and cross-repository test contract; golden fixtures + digests (`src/memtomem_stm/data/toolgraph-contract-v1/`). Provenance checking is **advisory only** — a POSIX permission walk that warns; the bundle is unsigned and an unsafe path does not fail closed. Writer rules (atomic private write) bind the producer, not STM. |
| Append-only event stream | Selection log (`stm_selection_log.jsonl`) | Producer/owner | `O_APPEND` + rotation (not whole-file atomic replace); per-record integer `schema_version` and an `event` discriminator; structural redaction + privacy screen before append. |
| Live MCP consult | Toolgraph stdio consult; agent-guard (future) | Consumer | Typed availability errors; cache-invalidation identity (e.g. `graph_generation`); timeout and explicit fail-open/closed semantics; contract may be capability-negotiated or, if peer-owned and unversioned, flagged as such. |
| Outbound telemetry | tracegraph (future) | Producer | Externally standardized format (OTLP/OpenInference); real span lineage (span/parent/links — never synthesized from adjacent records); body-free attributes; structural redaction preserved. |
| Ordinary proxied upstream | syncmill `serve` | Proxy | No ecosystem-specific contract; the standard upstream pipeline applies unchanged; peer-owned result text is passed through the normal compression/cache path. |
| Local persistence | SQLite caches/stores | Owner | `src/memtomem_stm/utils/sqlite_private.py` 0600 files; any deliberate privacy-scan skip is documented at the store. |

## Exchange-boundary matrix

One row per contract (not per tool). "Gate" names the deferral gate that must
be satisfied before the planned work starts.

| Boundary | Direction | Contract owner | Transport | Profile | Versioning | Privacy classification | Correlation identifiers | Gate |
|---|---|---|---|---|---|---|---|---|
| Toolgraph policy bundle | in | Toolgraph | JSON file | Immutable whole-file artifact | `schema_version` (int, owner-declared) | decision metadata only — no tool-call content | `graph_state` (`instance_id`, `generation`), catalog/governance digests, `bundle_digest` (exact-byte integrity identifier, not a signature) | shipped |
| Toolgraph stdio consult | in | Toolgraph | stdio MCP | Live MCP consult | **unversioned** (consumed as-is) | tool names/metadata only — no tool-call content | `graph_generation` | shipped |
| Selection log | out | STM | JSONL file | Append-only event stream | `schema_version` (int) | structural redaction (args as digest + length) + privacy screen | `selection_id` (selection/execution join key); `trace_id` (application correlation id, propagated across MCP boundaries — not an OpenTelemetry trace/span identity) | shipped (no external consumer yet) |
| OTLP span export (tracegraph) | out | external standard | OTLP/HTTP | Outbound telemetry | adopt-external-standard | body-free by construction — per-span attribute map admitting only STM-derived values (spans whose metadata comes from MCP arguments export none), surviving strings privacy-screened, no tool arguments in any form, error class names only, explicit resource (no `OTEL_RESOURCE_ATTRIBUTES`) | real W3C trace/span ids with in-process parentage (`src/memtomem_stm/observability/otlp.py`); the app-level `trace_id` rides as an attribute and is never promoted to trace identity; `read_more` is a root correlated by attribute, link deferred | shipped |
| agent-guard consult | in | agent-guard | MCP | Live MCP consult | unversioned today — **inventoried, not integrated** | **content-bearing** (request-time tool I/O leaves the proxy) — TBD, gate-controlled | TBD | `content-egress-consult` |
| vigil | — | — | — | no contract defined yet | — | — | — | `vigil-trigger` |
| syncmill `serve` | in | syncmill | MCP | Ordinary proxied upstream | peer-owned; no ecosystem contract applies | content-bearing, no general ingress redaction — credential scanning gates the enabled persistence/egress paths (external-LLM compression, response-cache writes, auto-index/extraction), not passthrough | generic `_trace_id` on live calls; no syncmill-specific identifier | none needed |

Two statements the matrix makes explicit:

- **The selection log is not tracegraph telemetry.** It records
  `selection_id` (joining selection and execution records) and an
  application-level `trace_id` that is propagated across MCP boundaries but
  is not an OpenTelemetry trace or span identity — there is no span
  parentage or linkage, and tracegraph rejects synthesized causality. An
  outbound-telemetry integration emits real spans; it does not repackage
  this log.
- The selection log's `feedback` event is schema-pinned but has **no
  production call site** (`SelectionTelemetryLog.log_feedback` exists and is
  exercised by tests only). Any export built on this stream must state that
  limitation rather than implying a complete learning signal.

## Deferral gates

- **`otlp-telemetry-export`** — criteria: none; no additional external
  evidence was required before designing the exporter. **Satisfied**: shipped
  as `src/memtomem_stm/observability/otlp.py`, operator documentation in
  [OTLP Span Export](../otlp-export.md).
- **`content-egress-consult`** — agent-guard publishes a versioned
  result/error contract, **and** the operator approves the new
  content-egress boundary (request-time tool I/O leaving the proxy: opt-in
  configuration, timeout, fail-open/closed semantics).
- **`shared-consult-lifecycle`** — two *implemented* adapters repeat the
  same MCP session lifecycle, capability negotiation, and failure taxonomy.
  Extraction covers that lifecycle only; domain verdict semantics stay
  per-tool.
- **`unified-telemetry-export`** — two consumers require the same event
  family, proven by identical golden fixtures.
- **`vigil-trigger`** — an STM-side trigger event is actually defined.

## Consequences

- The next integration starts by picking a profile and adding its matrix row
  in the same PR — not by designing new machinery.
- Premature-abstraction risk is bounded by the gates: shared code appears
  only after two concrete implementations demonstrate the repeated seam.
- Peer-owned unversioned surfaces are visible as such in the matrix instead
  of being silently tolerated; versioning pressure on a peer (agent-guard)
  is an explicit gate criterion rather than an STM-side workaround.
- The source paths this ADR names, and its "no production call site" claim
  about `log_feedback`, are pinned by `tests/test_docs_sync.py`, so the
  cheapest kinds of drift fail CI rather than aging in prose.

## References

- Operator guide for the shipped Toolgraph gateway:
  [Toolgraph Policy Gateway](../guides/toolgraph-policy-gateway.md)
- Selection log schema and privacy policy:
  [Selection Telemetry](../selection-telemetry.md)
- Toolgraph's side of the bundle boundary: ADR 0010 in the Toolgraph
  repository ("Portable policy bundle separates control plane from gateway
  data plane").
