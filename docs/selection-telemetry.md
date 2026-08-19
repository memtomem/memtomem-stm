# Selection Telemetry

For the surrounding proxy schema and configuration-source boundary, see the
[Proxy Configuration Reference](reference/proxy-config.md). For how this log
relates to other cross-tool exchange boundaries (and why it is not an
observability trace export), see
[ADR 0001](adr/0001-ecosystem-integration-contracts.md).

Append-only JSONL log of tool selection and execution outcomes (#467). The
proxy sits in the call path, so it can record what an advisory analyzer never
sees: which tool the client model actually called, out of which advertised
candidate set, and how the call went. This log is the substrate for offline
replay/eval (#468) and later learning stages (#469/#470), and the landing
zone for the STM-native hard filter's reject reasons (#465) — replay sees
the tools that were withheld from the advertisement, not just the ones in
it.

Off by default — it is a new disk write path, so the operator opts in:

```json
{
  "selection_telemetry": {
    "enabled": true,
    "path": "~/.memtomem/stm_selection_log.jsonl",
    "sample_rate": 1.0,
    "max_bytes": 50000000,
    "max_backups": 3
  }
}
```

The flag is read at startup (like `metrics.enabled`); toggling it requires a
restart. Files are created `0600`; the parent directory is created `0700`
(a pre-existing parent keeps its mode, matching the other STM stores — the
file mode alone guards the content). When the log reaches `max_bytes` it
rotates (`log → log.1 → … → log.N`, oldest dropped; `max_backups: 0`
truncates instead). `sample_rate` keeps the given fraction of calls and
applies to the selection+execution pair atomically — and a selection whose
write failed also skips its execution event — so neither sampling nor write
failures produce orphan halves.

## Schema v1

One JSON object per line, keys sorted, every record self-describing via
`schema_version` (bumped on any shape change — the exact key sets are pinned
by `tests/test_selection_log.py`) and `ranker_version`, a per-call cohort
marker stamped on both halves of a pair: `"v0-passthrough"` when no ranking
informed the call (the client model picked from the full advertised set
unaided — the unranked baseline), `"v1-bm25-tool-relevance"` when the #466
ranking ran (see [Tool-relevance ranking](#tool-relevance-ranking-466-v0)),
`"v2-bm25-risk-penalty"` when at least one nonzero penalty came *only* from
the #465 review-profile demotion, and `"v3-bm25-graph-risk-penalty"` when at
least one penalty included a component derived from the external tool-graph's
per-candidate `risk_score` (#493). The scoring math is identical across v1–v3;
the cohorts split on penalty *provenance* (and reach — a graph risk penalty
applies in every profile, the native review penalty only under `review`), so
replay must not pool them. An all-zero penalty map is v1 math and keeps the v1
stamp.

### `selection` — one per proxied call

| field | v1 value |
|---|---|
| `selection_id` | joins the paired `execution` record |
| `trace_id` | joins `proxy_metrics.db` for per-stage diagnostics |
| `server`, `selected_tool` | prefixed name, same vocabulary as `candidate_tools` |
| `candidate_tools`, `candidate_count` | what the proxy last advertised (`get_proxy_tools()` snapshot) |
| `reject_reasons` | prefixed tool → reason code for every tool the #465 filter withheld from that advertisement (see [Hard-filter reject reasons](#hard-filter-reject-reasons-465)); `{}` when nothing was rejected |
| `candidate_features` | ranking output object when #466 ranking ran (shape below); `null` otherwise |
| `graph_generation` | the external tool-graph generation the advertisement was filtered under (#465), pinning replay to a graph state; `null` when the `toolgraph` provider is disabled, unconsulted, or the consult failed without a usable verdict (unreachable / protocol error). On an agent-not-found degrade the graph still responded, so the generation is recorded |
| `args_sha256`, `args_chars` | canonical-JSON hash + length of the call arguments |
| `ts` | unix seconds |

### `execution` — paired outcome

| field | v1 value |
|---|---|
| `selection_id`, `trace_id`, `server`, `selected_tool` | mirror the paired selection |
| `ok` | `true`/`false` |
| `latency_ms` | proxy-side wall time for the full pipeline (what the agent experienced) |
| `error_type` | exception class name only; the typed category stays in `proxy_metrics.db` |
| `cache_hit` | `true` when the result was served from the proxy response cache, `false` on a live upstream call, `null` when a raise escaped before the hit/miss was attributable |
| `retry_count`, `cost` | reserved `null` |

### `feedback` — schema pinned, no emitter yet

`selection_id`, optional `trace_id`, `user_corrected`, `operator_override`.
Nothing in the proxy produces this signal today; emitters arrive with their
signal sources (e.g. an operator-facing rating tool).

## Tool-relevance ranking (#466 v0)

When `tool_relevance.enabled` (default `true`, inert without
`selection_telemetry.enabled` — there is nowhere else for the output to go
in v0), each call's advertised candidate set is ranked against the call's
query signal and recorded in `candidate_features`:

```json
{
  "query_source": "context_query | args",
  "query_sha256": "…",
  "query_chars": 42,
  "ranked_candidates": [
    {"tool": "gh__create_issue", "rank": 1, "relevance_score": 2.41,
     "risk_penalty": 0.0, "risk_penalty_source": "none", "final_score": 2.41,
     "graph_facts": null}
  ]
}
```

- **Telemetry input only — exposure never changes.** No `tools/list`
  reorder, no `list_changed`, no meta-tool. Whether the ranking is worth
  acting on is what offline replay (#468) decides from these records;
  dynamic exposure is deferred until that evidence exists.
- **Deterministic by construction**: BM25 only (the embedding scorer is
  deliberately excluded — its scores drift across providers/model versions,
  which would poison replay comparisons), candidates are the *advertised*
  artifacts (post-truncation description, post-distill schema — what the
  client actually saw), ties break on the prefixed name, never discovery
  order. Same inputs → byte-identical output, pinned by
  `tests/test_tool_relevance.py`.
- **Query signal**: `_context_query` when the caller attaches one, else the
  call's top-level string argument values (sorted by key, capped); no
  signal → no ranking, and the pair keeps the `v0-passthrough` baseline.
  The raw query never enters the log — `query_source`/sha256/length only.
- `risk_penalty` demotes a tool without removing it: `final_score =
  relevance_score * (1 - risk_penalty)` (ordering follows `final_score`),
  multiplicative because BM25 scores are unbounded. Two sources feed it,
  composed via a complement-product (`1 - (1 - native)(1 - graph)`): the #465
  `review`-profile demotion (a signal-flagged-but-advertised tool), and the
  #493 external tool-graph `risk_score` scaled by `risk_penalty_scale` (an
  eligible-but-risky tool, demoted in *every* profile). `risk_penalty_source`
  records which contributed — `none` / `review` / `graph` / `review+graph` —
  so replay can attribute the demotion (the combined value alone can't). The
  penalties are session-stable (health flags + the graph consult both run once
  at startup), so records stay deterministic within a session and
  self-describing across sessions. A graph-derived penalty stamps the pair
  `"v3-bm25-graph-risk-penalty"`; a native-review-only penalty stamps
  `"v2-bm25-risk-penalty"`.
- `graph_facts` carries the external tool-graph's per-candidate
  `rank_features` row for that tool (#469) — the facts the `risk_score` was
  derived from, as ranker *input features* for the learning stage. `null`
  means the graph said nothing about this tool: the provider is off, the
  consult degraded, the enrichment failed, or the ref was not in the batch.
  When present the object always carries the full key set, so an unknown fact
  is an explicit `null` and never a missing key:

  | key | meaning |
  | --- | --- |
  | `found` / `ambiguous` | did the ref resolve to exactly one tool |
  | `permitted` | the agent holds a grant for it |
  | `verdict` | `ALLOW` / `DENY` / `NOT_GRANTED` / `TOOL_NOT_FOUND` / `AMBIGUOUS_TOOL`, or `other` for a value this STM version does not know |
  | `classification` | worst-case DENY-path class: `violation` / `authorized_but_governed` / `other` |
  | `deny_path_count` | how many DENY evidence paths the graph reported (`0` = reported none, `null` = reported no list at all) |
  | `is_drifted` / `is_unmapped` / `has_unbacked_edges` | the drift, mapping and evidence-coverage facts behind the score |
  | `read_only_hint` / `destructive_hint` / `idempotent_hint` / `open_world_hint` | the tool's four annotation self-claims |
  | `risk_score` | the graph's rule-based risk in `[0,1]`, or `null` when the candidate did not resolve, or when the reported value was not a finite number (`NaN`/`Infinity` are not JSON, and an oversized integer has no float) |

  **These are the graph's facts, not STM's judgement** — `risk_score` `0.0`
  says the graph looked and found nothing wrong, which is a different record
  from `null`. Only the facts above are recorded: the graph's `tool_key` and
  its DENY evidence `deny_paths` are graph-authored text and never enter the
  log, which is why the paths appear only as a count. A **portable policy
  bundle** carries the compiled decision, not the row behind it, so in bundle
  mode only `risk_score` is populated and every other fact is `null`.
- `top_n` (default 20) bounds `ranked_candidates`; the full advertised set
  is already in `candidate_tools`.

## Hard-filter reject reasons (#465)

The exposure filter (`proxy/tool_eligibility.py`, configured by the
`exposure` block — see [configuration.md](configuration.md)) decides at
advertisement time which discovered tools the client model gets to see.
Every withheld tool appears in `reject_reasons` as `prefixed_name →
reason code`:

| code | meaning | profiles |
|---|---|---|
| `duplicate_name` | composed name carried by more than one discovered tool — the entire ambiguous group is withheld | all |
| `config_hidden` | per-tool `hidden: true` override | all |
| `profile_excluded` | `expose_in_profiles` does not include the active profile | all |
| `name_overflow` | composed client-side name exceeds the 64-char MCP limit | all |
| `sensitive_metadata` | credential-pattern match in the tool's description/schema | rejects under `strict`; demotes under `review` |
| `unhealthy` | upstream-attributable error rate over the recent metrics window crossed the threshold | rejects under `strict`; demotes under `review` |
| `toolgraph_not_granted`, `toolgraph_deny_violation`, `toolgraph_deny_governed`, `toolgraph_drifted`, `toolgraph_ambiguous`, `toolgraph_unmapped`, `toolgraph_tool_not_found`, `toolgraph_rejected` | per-candidate verdict from the optional external tool-graph provider (the `toolgraph` block — one code per upstream reason, `toolgraph_rejected` is the forward-compatible fallback for an unrecognized reason) | rejects under `strict`; demotes under `review` |
| `toolgraph_unreachable`, `toolgraph_agent_not_found`, `toolgraph_protocol_error` | whole-call fail-closed: the consult failed under a `closed` knob, so **every** tool is withheld under one code | all (profile-independent) |

Reason codes are the only #465 payload in the log — no tool metadata, no
error text — so the redaction policy below is untouched. `reject_reasons`
keys are **disjoint from `candidate_tools` by construction**: an entry
means that composed name was withheld from the client entirely. Ambiguous
names are never auto-exposed in any profile: upstream calls route by raw
tool name, so same-named occurrences (a pathological state — config
validation rejects duplicate upstream prefixes, so this needs a
misbehaving upstream or a bypassed config) are one callable entity wearing
several metadata claims, and advertising a "clean" copy would attach
metadata that does not bind to what executes. A rejected tool never
appears in `candidate_tools` or `ranked_candidates`: ranking runs over the
filter's eligible output and can never resurrect a reject (both pinned by
`tests/test_tool_eligibility.py`). The codes are additive vocabulary:
replay tooling should treat unknown codes as opaque.

## Redaction policy

Redaction is **structural**, not filter-based: no field ever carries raw
arguments, results, prompts, resource URIs, or error message strings.
Argument payloads appear only as a sha256 over their canonical JSON plus a
char count; error detail beyond the exception class name lives in
`proxy_metrics.db` and is reachable through the `trace_id` join instead of
being duplicated into this log.

As a backstop (the storage-gating rule from `proxy/privacy.py`, mirroring
the never-persist-secrets behavior of #460/#462), every serialized line is
screened with `contains_sensitive_content` before persisting and dropped —
and counted — on a match. A false positive costs one telemetry record,
never data. Backstop drops are per-record, so an `execution` whose paired
`selection` was dropped can appear alone: replay tooling must treat
`selection_id` joins as left-outer.

## Replay joins

- `selection.selection_id = execution.selection_id` — outcome per selection
  (left-outer; see above).
- `*.trace_id = proxy_metrics.trace_id` — full per-stage diagnostics
  (compression strategy, typed error category, stage timings) without
  duplicating them into the telemetry log.

Telemetry failures never propagate to proxied calls: write errors are
counted (`write_errors`) and logged, and the call proceeds untouched.

## Reading the telemetry

The `stm_selection_stats` MCP tool reads the active log back into a quick
operator summary, so a health check needs no hand-parsing of JSONL. It is one
of the opt-in observability tools — advertise it with
`MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=true` (see [cli.md](cli.md)) — and
it reports two views:

- **Live counters** — this process's write-path counters (`events_written` /
  `events_sampled_out` / `redaction_drops` / `write_errors`), reset at
  restart. These come straight off the running `SelectionTelemetryLog`, so
  they reflect activity even when sampling or the redaction backstop kept
  records off disk.
- **Persisted aggregate** — read off the active log file: event counts,
  selections **by ranker version** (the #468 cohort split — confirm each
  cohort has enough samples before replay), by server and tool, execution
  ok/error rate, latency p50/p95/p99, cache hit/miss rate, and the #465
  reject-reason tally.

Rotated backups (`log.1` …) are noted but not parsed — the summary covers the
active log only. For the full history, or any join against `proxy_metrics.db`,
stream the JSONL directly (see [Replay joins](#replay-joins)).

For deterministic corpus replay, rotated-log joins, data-quality checks, and
risk-weight evaluation, use [`mms selection replay`](selection-evaluation.md).
That evaluator keeps production telemetry observational and uses a separate
sanitized labelled corpus for counterfactual ranking.
