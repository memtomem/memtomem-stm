# Selection Telemetry

Append-only JSONL log of tool selection and execution outcomes (#467). The
proxy sits in the call path, so it can record what an advisory analyzer never
sees: which tool the client model actually called, out of which advertised
candidate set, and how the call went. This log is the substrate for offline
replay/eval (#468) and later learning stages (#469/#470), and the landing
zone for hard-filter reject reasons once the STM-native selector ships
(#465).

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
by `tests/test_selection_log.py`) and `ranker_version` (`"v0-passthrough"`
today: no in-proxy ranking exists, the client model picks from the full
advertised set, so replay tooling can treat this version as the unranked
baseline).

### `selection` — one per proxied call

| field | v1 value |
|---|---|
| `selection_id` | joins the paired `execution` record |
| `trace_id` | joins `proxy_metrics.db` for per-stage diagnostics |
| `server`, `selected_tool` | prefixed name, same vocabulary as `candidate_tools` |
| `candidate_tools`, `candidate_count` | what the proxy last advertised (`get_proxy_tools()` snapshot) |
| `reject_reasons` | `{}` until the #465 hard filter populates it (tool → reason) |
| `candidate_features`, `graph_generation` | reserved `null` until toolgraph#13/#15 integration |
| `args_sha256`, `args_chars` | canonical-JSON hash + length of the call arguments |
| `ts` | unix seconds |

### `execution` — paired outcome

| field | v1 value |
|---|---|
| `selection_id`, `trace_id`, `server`, `selected_tool` | mirror the paired selection |
| `ok` | `true`/`false` |
| `latency_ms` | proxy-side wall time for the full pipeline (what the agent experienced) |
| `error_type` | exception class name only; the typed category stays in `proxy_metrics.db` |
| `retry_count`, `cost`, `cache_hit` | reserved `null` in v0 |

### `feedback` — schema pinned, no emitter yet

`selection_id`, optional `trace_id`, `user_corrected`, `operator_override`.
Nothing in the proxy produces this signal today; emitters arrive with their
signal sources (e.g. an operator-facing rating tool).

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
