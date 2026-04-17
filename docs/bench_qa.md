# bench_qa — Scenario Harness

`bench_qa` is an end-to-end pytest sub-suite that drives
`ProxyManager.call_tool()` through fixture scenarios and asserts two
properties together: (a) compression produced the expected strategy
and stayed above the retention floor, and (b) the compressed output
still answers each scenario's QA probes. Both gates must pass for the
run to be green.

It is **pytest-only** — there is no `mms bench` CLI. Fixtures live in
`tests/bench/fixtures/s{01..10}.json`; the public harness API is
re-exported from `tests/bench/bench_qa/__init__.py`.

## Markers

| Marker | Scope | When it runs |
|--------|-------|--------------|
| `bench_qa` | Scenario suite + determinism gate (S01–S10) | Always (CI default). Skipped only by `-m "not bench_qa"`. |
| `bench_qa_llm_judge` | Advisory LLM-as-judge scoring on 6 scenarios | **Opt-in.** `uv run pytest -m bench_qa_llm_judge`. Auto-skip when no API key is set. |
| `bench_qa_meta` | Self-tests of the bench harness itself | Always, but decoupled — if these fail the scenario gates are meaningless. |

Selecting `-m bench_qa` does **not** include `bench_qa_meta` or
`bench_qa_llm_judge`; run them with `-m "bench_qa or bench_qa_meta"` or
name the marker explicitly.

## Scenario matrix

| ID | Compressor | Tier | Content | What it guards |
|----|------------|------|---------|----------------|
| **s01** | `auto` | 2 (forced) | markdown | Long guide with many headings forced into `hybrid_fallback`; head + TOC must survive. |
| **s02** | `auto` | — | json | Paginated API response; `extract_fields` / `schema_pruning` must keep keys and roles. |
| **s03** | `auto` | — | json | Nested event stream; per-event identifiers must not be flattened away. |
| **s04** | `auto` | — | code | Python class + unified diff; signatures and hunk headers must not truncate mid-line. |
| **s05** | `skeleton` | — | markdown | 40-turn chat (one heading per turn); per-turn speaker heading + Turn 17's planted sentinel must survive SKELETON. |
| **s06** | `auto` | 1 (forced) | text | Mixed log dump forced into `progressive_fallback`; zero-loss round-trip byte-identity across `stm_proxy_read_more` chunks. |
| **s07** | `selective` | — | json | 50 ranked search results; top-ranked items must stay in the TOC and keep their IDs in the 80-char preview window. |
| **s08** | `auto` | 3 (forced) | text | Heading-light incident narrative forced into `truncate_fallback`; lossy but non-empty floor. |
| **s09** | `auto` | — | text | Short budget-fit payload — compressor picks `none`; also the **two-run determinism** scenario. |
| **s10** | `none` | — | text | Surfacing smoke: fake LTM returns declared seeds, top-2 memory IDs must match fixture ranks, recall@2 == 1.0. |

"Tier forced" means the fixture stubs higher tiers to raise so the
ratio-guard fallback ladder deterministically picks the target tier.
See `tests/bench/test_bench_qa_fallback.py` for the stubbing pattern.

### Marker coverage

- Normal path (`test_bench_qa_scenarios.py`): s02, s03, s04, s09 (parametrized), s05, s07, s10 (standalone).
- Fallback ladder (`test_bench_qa_fallback.py`): s01, s06, s08.
- LLM judge (`test_bench_qa_llm_judge.py`, `LLM_JUDGE_SCENARIOS`): s02, s03, s04, s05, s07, s09. S01/S06/S08/S10 are intentionally excluded — `force_tier` and surfacing seeds change the semantics the judge was calibrated on.
- Determinism (`test_bench_qa_determinism.py`): s09 only.
- Meta (`test_bench_qa_meta.py`): probes against s02 and s06 prove the harness detects regressions rather than silently passing.

## Fixture schema (v1)

`tests/bench/bench_qa/schema.py` pins `FIXTURE_SCHEMA_VERSION = 1`.
The loader rejects mismatched versions so a stale fixture fails fast.

Required fields:

| Field | Type | Purpose |
|-------|------|---------|
| `schema_version` | `1` | Must match `FIXTURE_SCHEMA_VERSION`. |
| `scenario_id` | `"s01"`…`"s10"` | Drives `trace_id` and fixture filename. |
| `payload` | `str` | Raw upstream response body the fake session returns. |
| `expected_compressor` | `str` | Name in `CompressionStrategy` (e.g. `"auto"`, `"skeleton"`, `"selective"`). |

Optional fields:

| Field | Default | Purpose |
|-------|---------|---------|
| `description` | `""` | Shown in the LLM judge prompt and report. |
| `content_type` | — | `"json" \| "markdown" \| "code" \| "text"`. |
| `max_result_chars` | `50_000` | Per-run compressor budget. |
| `force_tier` | `None` | 1/2/3 pins the fallback ladder for ratio-guard tests. |
| `payload_multiplier` | `1` | Repeat `payload` N times on load to avoid committing huge files. |
| `expected_keywords` | `[]` | Global keyword presence check on compressed output. |
| `qa_probes` | `[]` | Per-probe `{question, expected_keywords}` used by the keyword judge. Answerability is case-insensitive substring. |
| `qa_gate_min` | `0.75` (global target) | Per-scenario floor for `qa_answerable / total`; looser scenarios start below 0.75 and tighten as the suite stabilises. |
| `min_retention` | `0.65` | Per-scenario override of `min_result_retention`. S05 lowers this so SKELETON can be exercised without the ratio-guard ladder kicking in. |
| `surfacing_seeds` | — | S10 only: fake LTM memory rows. |
| `surfacing_eval` | — | S10 only: `{query, k, expected_ids}`. Recall formula: `|returned_top_k ∩ expected_ids| / min(k, len(expected_ids))`. |

Public harness API (`tests/bench/bench_qa/__init__.py`):

- `load_fixture(scenario_id)` → `BenchFixture`
- `make_proxy_manager(tmp_path, *, compression, max_result_chars, min_retention)` → `(mgr, store, session)`
- `deterministic_trace_id(scenario_id, run_seed=0)` → `"bench-<sha256[:16]>"`
- `latest_metrics_row(store)` — pulls the last `proxy_metrics` row
- `BenchReportCollector`, `canonicalize_report` — report builder + determinism-safe diff form
- `qa_answerable_ratio`, `surfacing_recall_at_k` — judges

## LLM-as-judge (advisory)

`bench_qa_llm_judge` is opt-in and **advisory only** — its scores are
recorded in the report but **never gate a run**. The rule-based
keyword judge remains the compression gate. Per-scenario `llm_gate_min`
is on the post-gate roadmap (item 5 of 5–9) and will ship separately.

### Providers and defaults

| Provider | Default model | API key env var |
|----------|---------------|------------------|
| `openai` (default) | `gpt-4.1-nano` | `OPENAI_API_KEY` |
| `anthropic` | `claude-haiku-4-5-20251001` | `ANTHROPIC_API_KEY` |

Override with:

- `BENCH_LLM_JUDGE_PROVIDER=anthropic`
- `BENCH_LLM_JUDGE_MODEL=<model-id>`

When no API key is set for the resolved provider, `score_scenario`
returns `None` and the test skips — the run stays green.

### Determinism strategy

- `temperature=0` for both providers.
- `seed=42` for OpenAI (no Anthropic equivalent today).
- On-disk cache at `tests/bench/.llm_judge_cache/<16hex>.json`
  (gitignored). Cache key is
  `sha256(provider:model:scenario_id:content_type:compressed)[:16]`,
  so switching model or provider invalidates naturally. Delete the
  directory to force a refresh:

  ```bash
  rm -rf tests/bench/.llm_judge_cache
  ```

- `canonicalize_report()` strips the entire `llm_judge` block before
  the two-run determinism diff, so provider-side model updates that
  shift scores cannot fail S09. The keyword judge's score stays in the
  canonical diff because it is pure-stdlib deterministic.

### Failure mode

`score_scenario` returns an `LLMJudgeResult` with `.error` populated on
transport or parse failure rather than raising. A single flaky scenario
does not fail the run; the error surfaces in the report for follow-up.

## Determinism gate (S09)

`tests/bench/test_bench_qa_determinism.py` runs S09 twice in fresh
`tmp_path` dirs and asserts the canonicalized reports are byte-equal.

`trace_id` for every bench scenario is derived from the scenario ID,
not a random UUID:

```python
digest = hashlib.sha256(f"{scenario_id}:{run_seed}".encode()).hexdigest()[:16]
return f"bench-{digest}"
```

Source: `tests/bench/bench_qa/runner.py:61-69`.

Two consequences:

1. `proxy_metrics.trace_id` for a bench scenario is stable across
   runs, so `SELECT … WHERE trace_id = ?` is a reliable lookup.
2. The `bench-` prefix lets operators filter bench rows out of
   production dashboards and Langfuse views
   (`WHERE trace_id LIKE 'bench-%'`). The prefix is code-only today —
   `docs/operations.md` does not yet cross-link it.

`canonicalize_report()` drops wall-clock timings
(`clean_ms` / `compress_ms` / `surface_ms`), metrics noise that is not
reproducible, and the `llm_judge` block. `trace_id` itself is kept in
the canonical form — a mismatch there means trace-ID injection broke,
which is a different bug than wall-clock noise.

## Running locally

```bash
# Full CI-equivalent bench gate
uv run pytest -m bench_qa

# Harness self-tests
uv run pytest -m bench_qa_meta

# Advisory LLM judge (requires API key)
OPENAI_API_KEY=sk-... uv run pytest -m bench_qa_llm_judge

# Anthropic with override
BENCH_LLM_JUDGE_PROVIDER=anthropic \
  ANTHROPIC_API_KEY=sk-... \
  uv run pytest -m bench_qa_llm_judge

# Single scenario
uv run pytest tests/bench/test_bench_qa_scenarios.py -k s02

# Refresh LLM judge cache
rm -rf tests/bench/.llm_judge_cache
```

API keys can also be loaded from a local `.env` file — `.env` is
gitignored in this repo and the usual test-driver `load_dotenv()`
pattern works.

## Operator trace filter

Bench `trace_id`s start with `bench-` (see above). In Langfuse or any
SQL query against `proxy_metrics.db`:

```sql
-- Exclude bench rows from production aggregates
WHERE trace_id NOT LIKE 'bench-%'

-- Or isolate a specific scenario's history
WHERE trace_id = 'bench-<digest>'
```

## Adding a new scenario

See `CONTRIBUTING.md` § *Adding a bench_qa scenario* for the full
checklist. The short version:

1. Drop `tests/bench/fixtures/s{NN}.json` following the schema above
   (at minimum: `schema_version`, `scenario_id`, `payload`,
   `expected_compressor`).
2. Add the scenario to `NORMAL_PATH_SCENARIOS` in
   `test_bench_qa_scenarios.py`, or to the fallback suite in
   `test_bench_qa_fallback.py` if it pins `force_tier`.
3. If the scenario is eligible for LLM judge review, append it to
   `LLM_JUDGE_SCENARIOS` in `test_bench_qa_llm_judge.py` —
   `force_tier` scenarios are currently excluded.
4. Update `test_bench_qa_meta.py` so the harness self-test still
   proves bench catches the regression you are trying to guard
   against.
5. Run `uv run pytest -m bench_qa -m bench_qa_meta` and confirm both
   markers stay green.
