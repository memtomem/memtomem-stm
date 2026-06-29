# memtomem-stm Review — MCP proxy, native-tool proxy, compression & LTM surfacing

Date: 2026-05-30
Scope: MCP proxy pipeline (`proxy/manager.py`), native-tool hook proxy (`cli/hook_cmd.py` + `daemon/`), compression (`proxy/compression.py`, ratio vs information preservation), and LTM surfacing (`surfacing/`). Method: 10 parallel dimension reviewers → adversarial verification of every finding → synthesis, cross-checked against an empirical compression probe.

Result: 62 findings raised, 45 adversarially confirmed (is_real && not dropped).

## Overall assessment

memtomem-stm is mature with strong safety nets. The 3-tier retention fallback ladder, cache stampede guard, and trace_id propagation are correct and verified; several findings just confirm correct behavior. Net-new value lives in two areas. First, compression info-preservation: a cluster of hard-character-cut defects append a footer or tail or suffix after computing the break point, then slice to max_chars, destroying the high-value payload such as invalid JSON, lost tail anomalies, missing footers, and silent retention-floor breaches. Second, query-aware compression and surfacing quality: context_query is dropped at 5 of 8 strategies, surfacing has two query-extraction gaps, and the proxy path persists Bash secrets. Proxy completeness is solid; the only real gaps are observability. Most dedup and auto-tuner findings are correctly deferred as low-probability edge cases.

## Empirical compression probe (representative payloads)

Each payload run through `auto_select_strategy` + the selected sync compressor in isolation:

| Input | Strategy | Budget | Output | Ratio | Budget use |
|---|---|---|---|---|---|
| JSON array-of-objects (30, search-result shaped) | schema_pruning | 2000 | 626 | 0.063 | **31% used** |
| JSON config dict (8 nested) | extract_fields | 1500 | 1516 | 0.745 | **+16 over** |
| Markdown 12 sections + summary | truncate | 1500 | 1391 | 0.494 | 93% (head-bias) |
| Markdown + query='section 9 detail' | truncate | 1500 | 1048 | 0.372 | **70% (452 unused)** |
| Repetitive log + tail ERROR | truncate | 600 | 240 | 0.119 | **40% used** |
| NDJSON 40 lines | truncate | 800 | 836 | 0.377 | **+36 over, tail lost** |
| CSV 50 rows | truncate | 600 | 636 | 0.330 | **+36 over, tail lost** |

Note: the standalone compressor over-compresses array-of-objects (ratio 0.063), but in the live proxy the retention-floor guard (manager.py L1591-1710) catches `ratio < floor` and escalates progressive→hybrid→truncate, so the worst case is mitigated on the server path. Budget overshoot (+16/+36) and budget underuse (40-70%) are NOT caught by that guard.

## Findings by theme

### Hard-cut destroys preserved info

TruncateCompressor methods assemble head plus content plus footer or tail or suffix then slice to max_chars, landing on the high-value payload. Sibling methods pre-allocate; these skip it. Highest-leverage compression cluster.

- **`hard-cut-mid-json`** [high/high, implement_now] — Hard character cut in _json_key_truncate produces malformed JSON
  - evidence: src/memtomem_stm/proxy/compression.py:215-218
  - fix: Before hard-cutting, scan backward from max_chars to find a safe JSON boundary (top-level comma, closing brace for last key, or end of string value). If no safe boundary exists within a minimum window (e.g., 80 chars back), truncate gracefully by removing incomplete keys from parts before assembling, ensuring result is valid JSON. Alternatively, validate final result is valid JSON before returning; if not, recursively drop the last part and reassemble.
- **`tail-anomaly-hard-cut`** [high/high, implement_now] — Hard character cut in _tail_anomaly_truncate destroys the tail anomaly it was designed to preserve
  - evidence: src/memtomem_stm/proxy/compression.py:298-301
  - fix: Reserve enoughbudget for the tail_anomaly BEFORE truncating. Compute tail_len = len(tail_text), then allocate budget as: head_budget = max_chars - len(omitted_msg) - 20, truncate head_text to head_budget, then append tail. Or: detect when result would exceed max_chars and gracefully reduce sample_count (< 3 lines) or shorten head to preserve tail.
- **`section-aware-min-used-overflow`** [high/high, implement_now] — No validation that min_used + footer_reserve <= max_chars in _section_aware_truncate, risking negative enrich_budget and silent no-op enrichment
  - evidence: src/memtomem_stm/proxy/compression.py:338-361
  - fix: If min_used > (max_chars - footer_reserve), detect early and either (a) reduce sample_count from 3 to 1 or 2 to shrink minimums, (b) drop the least-relevant sections (by relevance score if available, or last sections) to fit, or (c) switch to a different strategy (e.g., SKELETON). Document the condition and ensure the budget is always sufficient for at least the first N sections (cap N or reduce per-section minimum).
- **`final-hard-cut-no-marker`** [medium/high, implement_now] — Markdown _section_aware_truncate hard-cut at line 434 lacks truncation sentinel when result exceeds budget
  - evidence: src/memtomem_stm/proxy/compression.py:429-435
  - fix: After the hard cut, verify the footer is complete. If the cut occurred mid-footer, regenerate a clean footer that fits: `result = result.rstrip()` to remove any partial footer, then conditionally append a compact footer if budget permits. Or: reserve footer space in Phase 2 enrich_budget to avoid over-running.
- **`code-aware-budget-reserve`** [medium/high, implement_now] — Code-aware truncate reserves only 60 chars for signature list, risking incomplete output
  - evidence: src/memtomem_stm/proxy/compression.py:554
  - fix: Make the reserve dynamic: `sig_budget = max(max_chars - used - len(remaining)*10, max_chars // 4)`. Or: before allocating space, estimate how many blocks can fit in sig_budget and trim remaining list accordingly. Document the per-block assumption in the code comment.
- **`truncate_exceeds_max_chars`** [medium/high, implement_now] — TruncateCompressor output can exceed max_chars due to suffix append
  - evidence: src/memtomem_stm/proxy/compression.py:155-157 — _find_break returns position <= max_chars, then L157 appends suffix
  - fix: Modify TruncateCompressor.compress and _section_aware_truncate to reserve space for the suffix within the max_chars budget. For the fallback case (L1697), add a post-truncate ratio check: after TruncateCompressor output, verify len(compressed)/cleaned_len >= dynamic. If truncate also violated floor, log WARNING and accept the violation (with ratio_violation=True) — this signals the operator that retention guardrails failed.
- **`hybrid_fallback_re_checks_ratio_truncate_does_not`** [high/high, implement_now] — Hybrid fallback re-checks ratio against floor, but truncate fallback does not
  - evidence: src/memtomem_stm/proxy/manager.py:1674 — hybrid fallback checks len(compressed)/cleaned_len >= dynamic
  - fix: After L1699 (truncate compressor output), add a post-truncate ratio check mirroring L1674. If len(compressed)/cleaned_len < dynamic, set a flag (e.g., post_truncate_still_violated=True) and log WARNING with both ratios. Accept the result but ensure ratio_violation=True in metrics.
- **`compression-suffix-unbound-overage`** [low/high, defer] — TruncateCompressor suffix can exceed budget by unbounded margin when footer metadata is large
  - evidence: src/memtomem_stm/proxy/compression.py:156-157 — footer is f"\n... (truncated, original: {len(text)} chars){summary}" with no size cap on summary
  - fix: In compression.py, cap the footer in TruncateCompressor.compress():
  - Compute footer_size = len(footer_template) + len(summary)
  - Reserve footer_size from max_chars before calling _find_break or internal truncate paths
  - Or: if result > max_chars after footer append, truncate body incrementally to fit, preserving footer integrity
  - For hook path (hook_cmd.py L234), explicitly reserve footer space: body_budget = max(1, cfg.max_chars - len(prefix) - 220) to account for footer + sentinel newline
- **`json-value-negative-budget`** [medium/medium, defer] — Negative budget passed to _truncate_json_value produces '...' -only output for keys
  - evidence: src/memtomem_stm/proxy/compression.py:210
  - fix: When computed budget < 20, either (a) skip truncation for that key entirely and take the full value, or (b) enforce a minimum useful budget of 30 chars before attempting recursive truncation, dropping lower-priority keys instead. Document the per-key minimum floor (currently implicit at 40 due to max(40,...)) in the docstring.

### Query-aware compression disabled

context_query is threaded into the pipeline but dropped before SELECTIVE, SCHEMA_PRUNING, EXTRACT_FIELDS, SKELETON, both truncate fallbacks, and the Hybrid TOC tail. Relevance-aware allocation is inert for 5 of 8 strategies.

- **`selective_no_query`** [high/high, implement_now] — SELECTIVE/TOC compression never receives context_query
  - evidence: manager.py:680-689 (SELECTIVE branch does not pass context_query to compress)
  - fix: Add context_query parameter to SelectiveCompressor.compress() signature and pass it through to downstream _store_and_build_toc(). Update get_compressor() return type to support scorers, or refactor get_compressor() to inject scorer instances into FieldExtractCompressor, SchemaPruningCompressor, SkeletonCompressor constructors (requires init signature changes). At minimum, pass context_query through fallback TruncateCompressor in SelectiveCompressor.compress when TOC fails.
- **`llm_fallback_no_query`** [medium/high, implement_now] — LLM_SUMMARY fallback to TruncateCompressor drops context_query
  - evidence: manager.py:724-726 (LLM fallback when no llm config, context_query not passed)
  - fix: Thread context_query through both fallback paths: (1) L725 add context_query=context_query, (2) L1698 add context_query=context_query. Both calls already have access to context_query variable in scope (L654, L1537).
- **`hybrid_selective_no_query`** [medium/high, implement_now] — HybridCompressor passes context_query to truncate fallback but NOT to SelectiveCompressor
  - evidence: compression.py:1423-1428 (HybridCompressor.compress, tail TOC path does not pass context_query to self._selective.compress)
  - fix: Add context_query parameter to SelectiveCompressor.compress() (see finding #selective_no_query), then pass it at L1424: self._selective.compress(tail_text, max_chars=toc_budget, context_query=context_query).
- **`embedding_section_truncation`** [medium/high, implement_now] — EmbeddingScorer truncates section body to 500 chars before embedding
  - evidence: relevance.py:162-172 (_score_via_embedding method, line 167 truncates body[:500])
  - fix: Make truncation configurable via EmbeddingScorer constructor (e.g., body_truncate_chars=500 parameter). Consider section-aware truncation: for markdown-heavy content, keep full section if <2000 chars; otherwise, keep head + tail (first 200 + last 300). Document the limitation and provide guidance on section size.

### Surfacing query-extraction gaps

pattern and glob args skip tokenization and fail the min-token gate so surfacing never fires; Bash command args truncate at literal period-space; and the proxy path persists Bash secrets to the surfacing query. Plus dict-order nondeterminism and semantic keys bypassing the identifier filter.

- **`pattern_glob_tokenization_gap`** [medium/high, implement_now] — Missing tokenization for 'pattern' and 'glob' keyword arguments
  - evidence: src/memtomem_stm/surfacing/context_extractor.py:14 (_PATH_KEYS definition)
  - fix: Expand _PATH_KEYS to include 'pattern' and 'glob' keys. These are semantically identical to file paths for query extraction (both are filesystem-keyed searches). Add them: _PATH_KEYS = {"path", "file", "filepath", "file_path", "filename", "pattern", "glob"}. Verify via test: Grep(pattern='src/*/test/conftest.py') or Glob(glob='**/*.py') should tokenize to meaningful terms.
- **`first_sentence_shell_command_misfire`** [medium/high, implement_now] — Bash command arguments can be broken by sentence delimiter matching
  - evidence: src/memtomem_stm/surfacing/context_extractor.py:107-114 (_first_sentence method)
  - fix: For Bash tool commands, apply a command-aware extraction instead of _first_sentence. Option 1 (lightweight): detect if the argument is likely a shell command (contains common shell metacharacters: pipes '|', redirects '>', ';', '$', '&&') and skip sentence-truncation entirely for those — fall through to full-text up to max_chars. Option 2 (conservative): only apply _first_sentence to string values that look like prose (whitespace-heavy, lacks shell metacharacters). This prevents Bash 'command' args from being mangled while preserving sentence-truncation benefits for prose arguments (descriptions, titles).
- **`bash_secret_leakage_risk_in_queries`** [high/high, implement_now] — Bash tool 'command' argument may persist secrets in surfacing_events.query
  - evidence: src/memtomem_stm/cli/hook_cmd.py:27-31 (docstring: '(secret-bearing Bash commands)')
  - fix: Add a privacy-aware extraction path for Bash tool commands. Before persisting a query, check if the tool is 'Bash' and if the query originated from a 'command' argument. If so, either (1) apply the hash substitution (persist_query_text=False logic, L168-169 in engine.py) unconditionally for that query, (2) explicitly redact common secret patterns from the command before extraction (scan for patterns like 'Authorization:', 'Bearer', '$', etc.), or (3) disable feedback persistence (record_feedback_events=False) when surfacing for Bash (symmetric to the hook cold path). Option 3 is most robust: Bash surfacing never persists query text or generates rating prompts.
- **`argument_ordering_determinism`** [medium/high, implement_now] — Query composition relies on Python dict iteration order (stable since 3.7)
  - evidence: src/memtomem_stm/surfacing/context_extractor.py:50 (for key, value in arguments.items())
  - fix: Sort argument keys before iteration (L50) to guarantee canonical query composition order. Change: `for key, value in sorted(arguments.items()):` instead of just `.items()`. This ensures that Read(path='x', title='y') and Read(title='y', path='x') produce identical queries. Add a note in comments explaining the determinism requirement for consistent surfacing/dedup behavior.
- **`semantic_keys_priority_inconsistency`** [medium/high, implement_now] — Semantic-key branch (L60-61) bypasses _is_identifier filter, allowing non-semantic strings
  - evidence: src/memtomem_stm/surfacing/context_extractor.py:50-61 (heuristic extraction logic)
  - fix: Apply _is_identifier check uniformly to semantic-key values too. Change L60-61 to: `elif key in _SEMANTIC_KEYS and not self._is_identifier(str(value)):`. This ensures that query='550e8400...' (a UUID) doesn't sneak in just because it came from a semantic key.

### Strategy-selection routing gaps

Multi-array dicts route to lossy TRUNCATE instead of SCHEMA_PRUNING; the hybrid fallback heading count misses docs starting with a heading, diverging from the correct regex used elsewhere.

- **`json-array-vs-dict-routing`** [medium/high, implement_now] — Pure JSON arrays [20+ items] route to SCHEMA_PRUNING; dict with array ignores secondary arrays
  - evidence: src/memtomem_stm/proxy/compression.py:1507-1512 — L1507: checks `isinstance(data, list) and len(data) >= 20` FIRST; L1510: checks arrays inside dict values but doesn't recurse for multiple arrays
  - fix: Sum array item counts across all dict values before deciding (e.g., if sum(len(v) for v in data.values() if isinstance(v, list)) >= 20, use SCHEMA_PRUNING). Alternatively, implement a TOTAL_ITEMS heuristic: if sum of all array lengths >30, prefer SCHEMA_PRUNING over TRUNCATE/EXTRACT_FIELDS for better schema preservation.
- **`heading_count_inconsistency`** [high/high, implement_now] — Heading detection inconsistency between fallback tier detection and AUTO resolution
  - evidence: src/memtomem_stm/proxy/manager.py:1664 — hybrid fallback uses cleaned.count('\n#')
  - fix: Unify heading detection across both code paths. Replace L1664's cleaned.count('\n#') with len(re.findall(r'(?:^|\n)#{1,6}\s', cleaned)). This ensures that (a) markdown documents starting with a heading are properly recognized for hybrid fallback eligibility, and (b) fallback tier selection matches the same logic used in auto_select_strategy.
- **`ndjson_jsonl_csv_unsupported`** [low/medium, defer] — No special handling for NDJSON, JSONL, CSV, TSV despite these being common tool outputs
  - evidence: src/memtomem_stm/proxy/compression.py:1503-1520 — JSON detection only tries json.loads(text); no check for 'newline-delimited' format
  - fix: Add format detection before JSON block (L1504): check for NDJSON via line-by-line json.loads() sampling, CSV via comma/tab-delimited heuristic. Route NDJSON to a dedicated compressor (sample first/last lines, preserve schema from first line). Route CSV to field-aware TRUNCATE (preserve header, show total row count, sample rows). These are 5-10% of tool calls likely but currently get worst-case TRUNCATE strategy.
- **`schema-pruning-string-loss`** [medium/high, defer] — SCHEMA_PRUNING destroys field values at 80-char boundary without lossless fallback
  - evidence: src/memtomem_stm/proxy/compression.py:1000-1002 — SchemaPruningCompressor(max_string=80, max_array_items=3)
  - fix: When iteratively reducing max_str fails (L1012-1024), before final truncation, try PROGRESSIVE (zero-loss, agent fetches via stm_proxy_read_more) or fall back to TRUNCATE instead of destructive string capping. Alternatively, increase SCHEMA_PRUNING's default max_string from 80 to 120-150 for search-result-like JSON and adjust iterative steps (80→100→60→40) to preserve more value before aggressive cuts.
- **`extract_fields_string_truncation_on_nested_dicts`** [medium/high, defer] — FieldExtractCompressor destroys nested dict values at 40 chars (vs SCHEMA_PRUNING at 80)
  - evidence: src/memtomem_stm/proxy/compression.py:933-948 — _preview_dict() with max_value_len=40 (nested dicts); L939-940 truncates to 40+ '...'
  - fix: Unify string truncation in FieldExtractCompressor: either use 80 for all depths OR make the threshold configurable (e.g., pass max_string to _preview_dict). Consider that nested-dict configs are often critical (db credentials, API keys in configs, nested auth) and warrant full value preservation.

### Gate consistency and auto-tuner config

write_tool_patterns matches only tool-name while exclude_tools matches both; auto-tuner floor and ceiling are unvalidated magic numbers not in config; increment can overshoot bounds; stale adjustments persist for pinned tools.

- **`write_tool_patterns_asymmetry`** [medium/high, implement_now] — write_tool_patterns checked against tool-only while exclude_tools checks both server__tool and tool
  - evidence: src/memtomem_stm/surfacing/relevance.py:65-66 (exclude_tools checks both fnmatch(full_name) and fnmatch(tool))
  - fix: For consistency, write_tool_patterns should also check fnmatch(full_name, pattern) OR fnmatch(tool, pattern) like exclude_tools does. Low priority since write_tool_patterns defaults sensibly and is rarely user-customized, but the asymmetry is a usability footgun.
- **`magichardcodedfloor_ceiling`** [medium/high, implement_now] — Hard-coded floor 0.005 and ceiling 0.05 are unvalidated magic numbers
  - evidence: feedback.py L152: ceiling enforced as min(current + increment, 0.05) with no comment
  - fix: Add config fields SurfacingConfig.auto_tune_score_ceiling (default 0.05, ge=config.min_score) and auto_tune_score_floor (default 0.005, le=config.min_score). Update feedback.py L152 and L165 to use these configurable bounds with explanatory log at initialization: 'AutoTune: bounds [floor, ceiling] = [{floor}, {ceiling}], base min_score = {config.min_score}'. Add docstring noting the relationship: ceiling should typically be 1-2x base min_score to allow suppression of noisy tools while remaining practical; floor should be low enough to surface quality memories even from quiet high-precision tools.
- **`no_validation_of_increment_vs_bounds`** [low/high, implement_now] — increment can be large enough to overshoot bounds in one step, leaving no opportunity for gradual correction
  - evidence: config.py L74: default auto_tune_score_increment=0.002
  - fix: Add a pydantic validator in SurfacingConfig that checks: auto_tune_score_increment * 5 < (ceiling - floor). If violated, raise ValidationError with message: 'auto_tune_score_increment={increment} is too large; at most ({ceiling - floor} / 5) = {(ceiling - floor) / 5} recommended for smooth convergence'. This ensures at least 5 adjustment steps are needed to cross the bounds, allowing feedback to stabilize before hitting hard limits.
- **`tool_cfg_pins_bypass_learning`** [low/high, implement_now] — When operator pins per-tool min_score, auto-tuner still learns adjustment that will never apply
  - evidence: engine.py L598-608: min_score precedence is tool_cfg.min_score > auto_tuner > default
  - fix: In AutoTuner.__init__ (L104-109), after loading persisted adjustments, filter them against the current config: remove any adjustment for tools whose context_tools entry has a pinned min_score. Log: 'AutoTune: purged N stale adjustment(s) for pinned tools on restart'. This keeps the tuner state hygenic and prevents get_min_score_snapshot from returning outdated values that the engine will never use.
- **`rate_limit_slot_consumption_policy`** [low/high, implement_now] — Rate-limit slot is eagerly consumed even for failed/empty surfacings; documented but worth audit
  - evidence: src/memtomem_stm/surfacing/relevance.py:101-108 (rate-limit slot consumed on should_surface=True return)
  - fix: No code change needed. The design is sound. Documentation is adequate (L102-107). Consider adding a comment in config.py clarifying the max_surfacings_per_minute semantics (counts all attempts, not just successes) in case users misconfigure.

### Cache-hit dedup and observability

Cache hits filter through invalidated ids but not surfaced ids, a session-dedup violation after FIFO pruning; cache hits record only a counter, no detailed metrics row.

- **`cache_hit_session_dedup_gap`** [high/high, implement_now] — Cache hits do not re-apply session dedup, enabling re-surfacing of stale memories
  - evidence: src/memtomem_stm/surfacing/engine.py:560-563 (cache hit calls _render_cached)
  - fix: In _render_cached (L469), after filtering via _invalidated_ids, also filter through _surfaced_ids: `cached = [r for r in cached if str(r.chunk.id) not in self._surfaced_ids]`. This ensures cached hits respect the same session dedup invariant as fresh searches.
- **`cache_hit_no_detailed_metrics`** [medium/high, implement_now] — Cache hits record only counter, no detailed CallMetrics row
  - evidence: src/memtomem_stm/proxy/manager.py:1078
  - fix: Record a lightweight CallMetrics row on cache hit with: server, tool, original_chars=cached_len, compressed_chars=cached_len, trace_id, surface_ms (from re-surfacing), cache_hit=True (new field). This enables dashboard queries like 'WHERE cache_hit=true' and correlates cache and non-cache paths.

### Already-correct or wait-for-signal

Confirmed correct mature code or low-probability edge cases on a system whose calibration is explicitly wait-for-signal. Deferring respects codebase discipline.

- **`trace_id_propagation_to_upstream_always`** [low/high, defer] — Trace_id correctly propagated to upstream in all paths
  - evidence: src/memtomem_stm/proxy/manager.py:1257-1258
  - fix: No change needed. This is well-handled.
- **`cache_stampede_guard_correct`** [high/high, implement_now] — Cache stampede guard (double-check lock) is correctly implemented
  - evidence: src/memtomem_stm/proxy/manager.py:1199-1210
  - fix: No change needed. The implementation is correct: cache.set() happens inside _call_tool_inner (awaited to completion) before the finally block's pop(), so the invariant 'second arrival's double-check will see the cached result' holds.
- **`progressive_branch_skips_ratio_check`** [low/high, defer] — Progressive delivery path skips ratio guard entirely (by design)
  - evidence: src/memtomem_stm/proxy/manager.py:1512-1539 — PROGRESSIVE branch does not enter ratio guard at L1603+
  - fix: Document this in a comment or docstring: 'The ratio guard does not apply to PROGRESSIVE delivery because the full content is stored and retrievable via read_more; the initial chunk is intentionally small.' Consider adding a metric flag (e.g., zero_loss=True) to distinguish progressive from lossy strategies in dashboards.
- **`pruning_order_FIFO_vs_LRU`** [low/high, defer] — FIFO pruning of _surfaced_ids on cap overflow can evict recent memories while keeping old ones
  - evidence: src/memtomem_stm/surfacing/engine.py:91-95 (_surfaced_ids initialized as insertion-ordered dict with 10k cap)
  - fix: Either: (1) Use an LRU strategy with access-time tracking; or (2) Increase cap to 50k or higher (current 10k is conservative); or (3) Use a time-based TTL per entry (e.g., evict entries >1 hour old regardless of insertion order). The current 50% truncation point on each cap overflow is also aggressive — reducing excess to 25% would provide smoother eviction.
- **`over_fetch_headroom_insufficient`** [low/medium, defer] — Over-fetch of top_k=max_results*2 may not provide sufficient headroom for dedup on long sessions
  - evidence: src/memtomem_stm/surfacing/engine.py:627 (top_k=max_results * 2)
  - fix: Either: (a) Request higher over-fetch multiplier when dedup_ttl_seconds is high (adaptive over-fetch); or (b) Track dedup hit ratio and warn when >50% of over-fetched results are filtered out (observability signal for operator to adjust dedup_ttl or max_results).
- **`score_filter_before_dedup_loses_signal`** [medium/high, defer] — Score filter applied before dedup can mask over-aggressive min_score tuning
  - evidence: src/memtomem_stm/surfacing/engine.py:676-683 (L676 filters by score; L678-683 applies dedup)
  - fix: In auto_tuner feedback ratio calculation (feedback.py L136-141), split ratios into two cohorts: (1) memories that passed score filter (scored) vs. (2) all results. This lets the tuner see 'negative feedback on in-threshold memories' (true signal) vs. 'negative feedback on deduplicated memories' (may indicate over-aggressive dedup, not bad scoring). Adjust tuning thresholds accordingly.
- **`no_stm_side_reranking_trusts_ltm_order`** [low/high, defer] — STM assumes LTM returns results sorted by score descending; if not, dedup via over-fetch fails silently
  - evidence: src/memtomem_stm/surfacing/engine.py:625-631 (requests top_k=max_results*2, no explicit sort)
  - fix: Add a defensive sort at the point results are received (L676): `scored = sorted((r for r in results if r.score >= min_score), key=lambda r: -r.score)` to ensure consistent dedup ordering regardless of upstream ordering guarantees. Comment explains the contract. Alternatively, add observability (histogram of score deltas between consecutive results) to detect if LTM ever violates the sort contract.
- **`invalidated_ids_pruning_same_gap`** [low/high, defer] — Invalidated IDs pruned via same FIFO strategy, allowing re-rejection of the same memory
  - evidence: src/memtomem_stm/surfacing/engine.py:114-122 (_invalidated_ids initialized with 10k cap)
  - fix: Consolidate pruning logic: create a shared _prune_fifo_dict(dict_, max_size, excess_fraction=0.5) utility that both _surfaced_ids and _invalidated_ids use. Document the desired semantics: whether the goal is FIFO, LRU, or age-based TTL. If the intent is 'forget old rejections after 1 hour', implement time-based eviction instead.
- **`jaccard_coarse_short_queries`** [low/high, defer] — Jaccard similarity threshold (>0.95) is too strict for short 2-3 token queries
  - evidence: src/memtomem_stm/surfacing/relevance.py:16 (_SIMILARITY_THRESHOLD = 0.95)
  - fix: For queries with <4 tokens, use a lower threshold (e.g. 0.7 or 0.75) or switch to a token-overlap heuristic (e.g., >= 2 common tokens for short queries). Alternatively, normalize tokens first (strip underscores before split) so 'read_file' becomes ['read', 'file'] to catch 'read' + 'file' overlap. Current threshold assumes queries have many tokens; it breaks down at min_query_tokens=3 (config default).
- **`no_oscillation_guard`** [low/medium, defer] — No protection against threshold oscillation when feedback ratios hover near band boundaries
  - evidence: feedback.py L151-163: raises when neg_ratio > 0.6, L164-176: lowers when helpful_ratio > 0.8
  - fix: Add hysteresis: track per-tool 'last_adjusted_at' timestamp in the tuner. In maybe_adjust(), skip adjustment if (time.monotonic() - last_adjusted_at) < hysteresis_seconds AND new_score would move in the opposite direction from the previous move (e.g., was raised, now lowering). Default hysteresis_seconds=300 (5min). Add log: 'AutoTune: {tool} skipped oscillation-guard hysteresis (last adjust {seconds_ago}s ago, would reverse direction)'.
- **`cold_start_cross_tool_bleed`** [medium/high, defer] — Cold-start fallback can blur per-tool signals with global noise
  - evidence: feedback.py L136-141: AutoTuner.maybe_adjust falls back to global ratio when per-tool sample count < auto_tune_min_samples
  - fix: Add a 'cross_tool_bleed_guard': skip the global fallback when the global sample count is <2*auto_tune_min_samples AND the global negative ratio is at an extreme (>0.65 or <0.15). This prevents a single noisy tool from poisoning cold-start tuning for unrelated tools. Alternatively, track per-tool 'observation_time' (time since first feedback) and only fall back to global if the tool has been active for >X seconds without hitting min_samples (indicating a truly quiet tool, not a brand-new one).
- **`cooldown_empty_retry_window`** [low/high, defer] — Empty-result queries can be repeatedly searched within cooldown window without cooldown penalty
  - evidence: src/memtomem_stm/surfacing/engine.py:249-255 (query extraction runs before gate)
  - fix: Move record_surfacing call or cooldown record to the gate level (inside should_surface after it returns True) OR call record_surfacing in _do_surface_miss BEFORE the early return paths at L700 (lines 688-700). The cooldown is documented as a 'skip if we already returned similar results' heuristic, but it only activates if the surfacing actually succeeded and found results. Recording the cooldown for empty searches would prevent legitimate retries on empty results (as acknowledged in L50-53), but the current code skips cooldown for failures entirely, creating the window. Consider: (a) add a separate 'recently_searched_empty' deque that blocks only identical empty queries, or (b) record cooldown in gate.should_surface after earning the True return (before await), since rate-limit slot is already consumed and empty cost is amortized.
- **`oversize_truncation_not_indexed`** [medium/high, defer] — Oversized responses truncated before indexing lose content without recovery
  - evidence: src/memtomem_stm/proxy/manager.py:1404-1447
  - fix: When max_upstream_chars truncation occurs: (1) Record a distinct error_category or flag in metrics to distinguish truncations from natural responses. (2) Consider raising an error or warning that short-circuits auto_index/extraction to avoid storing incomplete data. (3) Alternatively, if indexing is enabled, disable it for oversize responses and log a WAR log that the content is not recoverable.
- **`dynamic_floor_raises_beyond_operator_intent`** [medium/high, implement_now] — Dynamic floor raising effective_max_chars may defeat operator's small max_chars setting
  - evidence: src/memtomem_stm/proxy/manager.py:1560-1562 — min_budget calc and floor raise logic
  - fix: Document this behavior in ToolOverrideConfig and ProxyConfig docstrings, with an example: 'Setting max_result_chars below n*min_retention will be raised to preserve minimum retention; use retention_floor to override.' Add a trace-level log at L1562 when the floor raise occurs, showing: 'max_result_chars {original}→{effective} due to dynamic floor {dynamic:.2%} on {n} chars.'
- **`hook-budget-exceeded-leaves-hook-output-undefined`** [low/high, defer] — If _orchestrate exceeds _hook_budget_seconds, asyncio.wait_for raises but exit code is always 0 with empty output
  - evidence: src/memtomem_stm/cli/hook_cmd.py:117-131 — _hook_budget_seconds() returns max(configured + 8.0, 12.0), minimum 12 seconds
  - fix: In hook_command() L546-551, catch asyncio.TimeoutError separately and log at warning level: logger.warning("hook processing timed out (%.1f s) — passing tool output through", _hook_budget_seconds()). Keep the generic Exception handler for other failures.

## Recommended implementation order

1. 1. QW heading_count_inconsistency, highest value per effort, isolated
2. 2. QW tail-anomaly-hard-cut, reserve tail budget
3. 3. QW llm_fallback_no_query, one-arg query-aware fix
4. 4. QW pattern_glob_tokenization_gap, make Grep and Glob surfacing fire
5. 5. QW cache_hit_session_dedup_gap, filter cache hits through surfaced ids
6. 6. QW write_tool_patterns_asymmetry, match server tool
7. 7. QW semantic_keys_priority_inconsistency plus argument_ordering_determinism, one PR
8. 8. LARGER hard-cut-mid-json, valid JSON, do early, worst failure mode
9. 9. LARGER truncate_exceeds_max_chars plus hybrid_fallback_re_checks_ratio_truncate_does_not, coupled retention-ladder PR
10. 10. LARGER section-aware-min-used-overflow plus final-hard-cut-no-marker, one PR
11. 11. LARGER selective_no_query plus hybrid_selective_no_query, keystone query-aware PR, after step 3
12. 12. LARGER bash_secret_leakage_risk_in_queries plus first_sentence_shell_command_misfire, one Bash-extraction PR
13. 13. LARGER json-array-vs-dict-routing plus code-aware-budget-reserve
14. 14. LARGER magichardcodedfloor_ceiling plus no_validation_of_increment_vs_bounds plus tool_cfg_pins_bypass_learning, auto-tuner bundle
15. 15. LARGER cache_hit_no_detailed_metrics plus embedding_section_truncation, observability, ship last
16. DEFER wait-for-signal: dynamic_floor_raises_beyond_operator_intent, schema-pruning-string-loss, extract_fields_string_truncation_on_nested_dicts, ndjson_jsonl_csv_unsupported, json-value-negative-budget, compression-suffix-unbound-overage, pruning_order_FIFO_vs_LRU, over_fetch_headroom_insufficient, score_filter_before_dedup_loses_signal, no_stm_side_reranking_trusts_ltm_order, invalidated_ids_pruning_same_gap, jaccard_coarse_short_queries, no_oscillation_guard, cold_start_cross_tool_bleed, cooldown_empty_retry_window, oversize_truncation_not_indexed, hook-budget-exceeded-leaves-hook-output-undefined
17. NO ACTION correct: trace_id_propagation_to_upstream_always, cache_stampede_guard_correct, progressive_branch_skips_ratio_check, rate_limit_slot_consumption_policy
