# Selection Telemetry

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
and `"v2-bm25-risk-penalty"` when at least one nonzero #465 risk penalty
shaped the scores (an all-zero penalty map is v1 math and keeps the v1
stamp).

### `selection` — one per proxied call

| field | v1 value |
|---|---|
| `selection_id` | joins the paired `execution` record |
| `trace_id` | joins `proxy_metrics.db` for per-stage diagnostics |
| `server`, `selected_tool` | prefixed name, same vocabulary as `candidate_tools` |
| `candidate_tools`, `candidate_count` | what the proxy last advertised (`get_proxy_tools()` snapshot) |
| `reject_reasons` | prefixed tool → reason code for every tool the #465 filter withheld from that advertisement (see [Hard-filter reject reasons](#hard-filter-reject-reasons-465)); `{}` when nothing was rejected |
| `candidate_features` | ranking output object when #466 ranking ran (shape below); `null` otherwise |
| `graph_generation` | reserved `null` until toolgraph#13 integration |
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
     "risk_penalty": 0.0, "final_score": 2.41}
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
- `risk_penalty` is the #465 hard filter's demotion input: under the
  `review` exposure profile a signal-flagged tool stays advertised but
  carries the configured penalty, and `final_score = relevance_score *
  (1 - risk_penalty)` (ordering follows `final_score`). Multiplicative
  because BM25 scores are unbounded. Penalties are session-stable (health
  flags are computed once at startup), so records stay deterministic
  within a session and self-describing across sessions. When any nonzero
  penalty applied, both pair halves stamp `"v2-bm25-risk-penalty"`.
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
