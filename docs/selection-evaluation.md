# Offline Tool-Selection Evaluation

`mms selection replay` evaluates the tool-selection path without changing the
running proxy or its configuration. It combines two deliberately separate
evidence lanes:

- production selection JSONL provides observational schema, join, cohort,
  execution, latency, cache, and feedback diagnostics;
- the packaged, sanitized 30-case labelled corpus replays the current
  eligibility filter and BM25 ranker and evaluates candidate risk weights.

Production telemetry contains hashes instead of raw prompts and does not store
candidate descriptions or schemas. It therefore cannot reproduce lexical
scores or establish that one ranker caused a task outcome. In particular,
execution success is not task success, and ranker-version cohorts are not
randomized A/B groups.

## Run a replay

```bash
# Configured log plus numeric rotated backups (.N ... .1, then active)
mms selection replay

# Fixed corpus only; useful for deterministic CI
mms selection replay --no-telemetry --json

# Explicit inputs and durable private artifacts
mms selection replay \
  --log ./stm_selection_log.jsonl \
  --dataset ./selection_eval.json \
  --output-dir ./selection-report
```

`--active-only` excludes rotated backups. `--json` emits one stable report
document. `--output-dir` atomically writes `report.json` and `summary.md` with
mode `0600`. An explicitly supplied missing log, malformed corpus, unsupported
telemetry schema, conflicting duplicate, invariant failure, or score-parity
failure produces a non-zero result. A missing configured default log is not an
error: corpus evaluation still completes and records telemetry as unavailable.

The command is always read-only. Its `config_patch` is a preview; it never
updates `stm_proxy.json`.

## Corpus and tuning contract

The bundled `selection-eval-v1-synthetic-30` corpus contains 18 training, six
validation, and six held-out test cases. Cases cover name, description, input
schema, Korean and multi-intent matching, distractors, abstention, and the
strict/review/tool-graph safety paths. Group IDs may not cross splits, and the
loader rejects credential-shaped content.

Every replay executes the production `filter_tools` and
`ToolRelevanceRanker`. It tests the existing knobs only:

- `exposure.review_risk_penalty`: `0`, `0.25`, `0.5`, `0.75`, `1`
- `toolgraph.risk_penalty_scale`: `0`, `0.25`, `0.5`, `0.75`, `1`, `1.5`, `2`

The 35 variants report hit rate, task-success proxy, MRR, nDCG@5, unsafe
candidate rejection, unnecessary rejection, policy exposure, unsafe top-k,
and abstention false positives by split. Training first removes variants that
regress the safety and task@5 gates. Validation selects lexicographically with
safety before relevance. The held-out test is deliberately used only as a final
safety confirmation gate: it must show a strict improvement with no regression,
or the report keeps the configured baseline. The command remains preview-only,
so this conservative confirmation never rolls out a configuration automatically.

## CI golden

`tests/fixtures/selection_eval/golden.json` pins the SHA-256 of the complete
canonical JSON report, along with the corpus split, variant count, and baseline
recommendation. The corpus identity is computed from canonical parsed JSON, so
checkout line endings and formatting do not change the report across operating
systems. An intentional ranker, eligibility, metric, corpus, or report contract
change must be reviewed and then update that golden explicitly.

For the write-side schema and privacy guarantees, see
[Selection Telemetry](selection-telemetry.md).
