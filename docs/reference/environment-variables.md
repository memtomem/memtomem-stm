# Environment Variable Reference

This is the authoritative inventory of memtomem-stm environment settings.
Root settings use `MEMTOMEM_STM_<FIELD>`; nested settings use Pydantic's
double-underscore convention, for example
`MEMTOMEM_STM_SURFACING__MIN_SCORE=0.03`. Complex lists and mappings are JSON.
The table below lists one row per leaf setting. A whole nested block can also be
supplied as one JSON object at its parent name — `MEMTOMEM_STM_PROXY__CACHE` for
every `MEMTOMEM_STM_PROXY__CACHE__*` leaf — including optional blocks such as
`MEMTOMEM_STM_PROXY__EXTRACTION__LLM`, and the top-level block itself
(`MEMTOMEM_STM_PROXY` as one JSON object for the whole proxy config). For a
top-level field payload like `MEMTOMEM_STM_PROXY`, a deeper variable wins in
either order; nested block payloads (`MEMTOMEM_STM_PROXY__CACHE`) resolve
against deeper variables last-one-wins by environment order.

Environment values have the highest precedence. `~/.memtomem/stm_proxy.json`
loads `ProxyConfig` only; root, surfacing, formation, hook, daemon, Langfuse,
and OTLP settings are environment/default-only. Proxy environment values are
layered over the file. The proxy's `consumer_model` propagation into surfacing
is the documented exception across those domains.

Layering reaches inside an upstream server: a single field of a server the file
declares can be overridden at
`MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__<NAME>__<FIELD>` (for example
`MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__COMMAND`), and the file supplies the
fields the variable omits. `<NAME>` arrives lower-cased, so this form addresses
a server the file keys in lower case; to override one field of a server keyed
with capitals, spell it in the aggregate JSON form, whose keys are taken
verbatim — `MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS='{"GH": {"command": "…"}}'`.
A name that matches no server in the file declares a new one, which must then
carry every required field itself, `PREFIX` included.

Defaults below are model defaults before config-file or environment overlays.
Paths containing `~` are expanded at their use sites. Never paste configured
secret values into issue reports.

Status meanings:

- **Bundled** — used by the installed `mms`/MCP server.
- **Startup** — bundled, but read only at process startup or module import.
- **Library** — configuration for a custom `ProxyManager` extension; the
  bundled `mms` server does not wire the feature.

## Complete STMConfig inventory

<!-- stmconfig-env:start -->
| Variable | Type / accepted shape | Default | Purpose | Status |
|---|---|---|---|---|
| `MEMTOMEM_STM_LOG_LEVEL` | `DEBUG / INFO / WARNING / ERROR / CRITICAL` | `WARNING` | Process log level. | Startup |
| `MEMTOMEM_STM_LOG_FILE` | path or `null` | — | Optional rotating log file in addition to stderr. | Startup |
| `MEMTOMEM_STM_PROXY__ENABLED` | boolean | `false` | Enable proxy serving; new CLI configs normally persist this in JSON. | Bundled |
| `MEMTOMEM_STM_PROXY__CONFIG_PATH` | path | `~/.memtomem/stm_proxy.json` | Proxy JSON location. | Bundled |
| `MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS` | JSON object | `{}` | Complete map of upstream MCP definitions. | Bundled |
| `MEMTOMEM_STM_PROXY__DEFAULT_COMPRESSION` | compression strategy | `auto` | Default strategy when an upstream does not override it. | Bundled |
| `MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS` | positive integer | `16000` | Configured response-size ceiling. | Bundled |
| `MEMTOMEM_STM_PROXY__MAX_UPSTREAM_CHARS` | positive integer | `10000000` | Pre-compression OOM guard for upstream content. | Bundled |
| `MEMTOMEM_STM_PROXY__MIN_RESULT_RETENTION` | float `0..1` | `0.65` | Minimum retained fraction before a fixed budget is allowed. | Bundled |
| `MEMTOMEM_STM_PROXY__RELEVANCE_SCORER__SCORER` | `bm25 / embedding` | `bm25` | Query-aware compression scorer. | Bundled |
| `MEMTOMEM_STM_PROXY__RELEVANCE_SCORER__EMBEDDING_PROVIDER` | provider name | `ollama` | Embedding backend when the scorer is `embedding`. | Bundled |
| `MEMTOMEM_STM_PROXY__RELEVANCE_SCORER__EMBEDDING_MODEL` | string | `nomic-embed-text` | Embedding model identifier. | Bundled |
| `MEMTOMEM_STM_PROXY__RELEVANCE_SCORER__EMBEDDING_BASE_URL` | URL or `null` | — | Optional embedding-provider base URL. | Bundled |
| `MEMTOMEM_STM_PROXY__RELEVANCE_SCORER__EMBEDDING_TIMEOUT` | positive float | `10.0` | Embedding request timeout in seconds. | Bundled |
| `MEMTOMEM_STM_PROXY__MAX_DESCRIPTION_CHARS` | positive integer | `200` | Maximum advertised tool-description length. | Bundled |
| `MEMTOMEM_STM_PROXY__STRIP_SCHEMA_DESCRIPTIONS` | boolean | `false` | Remove nested schema descriptions from proxied tools. | Bundled |
| `MEMTOMEM_STM_PROXY__ADVERTISE_CONTEXT_QUERY` | boolean | `false` | Add the proxy-only `_context_query` argument. | Bundled |
| `MEMTOMEM_STM_PROXY__LOCK_TIMEOUT_SECONDS` | positive float | `30.0` | Internal proxy state-lock deadline. | Bundled |
| `MEMTOMEM_STM_PROXY__CONSUMER_MODEL` | string | empty | Consumer model used for conservative budget calculation. | Bundled |
| `MEMTOMEM_STM_PROXY__CONTEXT_BUDGET_RATIO` | float `0..1` | `0.05` | Fraction of a known model context allotted to one response. | Bundled |
| `MEMTOMEM_STM_PROXY__CHARS_PER_TOKEN` | positive float | `3.5` | Token-to-character conversion ratio. | Bundled |
| `MEMTOMEM_STM_PROXY__TOKEN_ESTIMATION_MODE` | `static / unicode` | `static` | Response token-estimation mode. | Bundled |
| `MEMTOMEM_STM_PROXY__CACHE__ENABLED` | boolean | `true` | Enable response caching. | Bundled |
| `MEMTOMEM_STM_PROXY__CACHE__DB_PATH` | path | `~/.memtomem/proxy_cache.db` | Response-cache SQLite file. | Bundled |
| `MEMTOMEM_STM_PROXY__CACHE__DEFAULT_TTL_SECONDS` | non-negative float or `null` | `3600.0` | Default cache TTL. | Bundled |
| `MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES` | positive integer | `10000` | Maximum cache rows. | Bundled |
| `MEMTOMEM_STM_PROXY__CACHE__TOOL_ANNOTATION_POLICY` | `conservative / strict / ignore` | `conservative` | Cache behavior for missing/read-only annotations. | Bundled |
| `MEMTOMEM_STM_PROXY__AUTO_INDEX__ENABLED` | boolean | `false` | Enable library-mode response indexing. | Library |
| `MEMTOMEM_STM_PROXY__AUTO_INDEX__BACKGROUND` | boolean | `false` | Run library-mode indexing in the background. | Library |
| `MEMTOMEM_STM_PROXY__AUTO_INDEX__MIN_CHARS` | non-negative integer | `2000` | Minimum content size for library-mode indexing. | Library |
| `MEMTOMEM_STM_PROXY__AUTO_INDEX__MEMORY_DIR` | path | `~/.memtomem/proxy_index` | Library-mode index output directory. | Library |
| `MEMTOMEM_STM_PROXY__AUTO_INDEX__NAMESPACE` | template string | `proxy-{server}` | Library-mode namespace template. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__ENABLED` | boolean | `false` | Enable library-mode fact extraction. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__STRATEGY` | extraction strategy | `llm` | Fact-extraction strategy. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__LLM__PROVIDER` | `openai / anthropic / ollama` | `openai` | Provider **once the block exists** — not the extractor's absent-block default. See the caveat below. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__LLM__MODEL` | string | `gpt-4.1-mini` | Model once the block exists; the absent-block extractor uses `qwen3:4b`. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__LLM__API_KEY` | secret string | empty | Extractor credential; falls back to the provider's own key variable. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__LLM__BASE_URL` | URL | empty | Optional extractor endpoint override. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__LLM__SYSTEM_PROMPT` | string | `Summarize the following content concisely, preserving all key information. Keep the summary under {max_chars} characters.` | Shared field default written for the *compression* path. Extraction substitutes `{max_facts}` only, so this default raises on the extraction path — set an extraction prompt whenever you materialize this block. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__LLM__MAX_TOKENS` | positive integer | `500` | Completion budget once the block exists; the absent-block extractor uses `1000`. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__LLM__LLM_TIMEOUT_SECONDS` | positive float | `60.0` | Extractor request timeout. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__LLM__PRIVACY_SCAN_ENABLED` | boolean | `true` | Scan extractor input for credentials before sending. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__MAX_FACTS` | positive integer | `10` | Maximum facts extracted per response. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__MIN_RESPONSE_CHARS` | non-negative integer | `500` | Minimum response size eligible for extraction. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__DEDUP_THRESHOLD` | float `0..1` | `0.92` | Extracted-fact similarity threshold. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__MEMORY_DIR` | path | `~/.memtomem/extracted_facts` | Extracted-fact output directory. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__NAMESPACE` | template string | `facts-{server}` | Extracted-fact namespace template. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__BACKGROUND` | boolean | `true` | Run extraction in the background. | Library |
| `MEMTOMEM_STM_PROXY__EXTRACTION__MAX_INPUT_CHARS` | positive integer | `20000` | Maximum extractor input size. | Library |
| `MEMTOMEM_STM_PROXY__METRICS__ENABLED` | boolean | `true` | Record proxy metrics. | Bundled |
| `MEMTOMEM_STM_PROXY__METRICS__DB_PATH` | path | `~/.memtomem/proxy_metrics.db` | Proxy metrics SQLite file. | Bundled |
| `MEMTOMEM_STM_PROXY__METRICS__MAX_HISTORY` | positive integer | `10000` | Maximum retained metric rows. | Bundled |
| `MEMTOMEM_STM_PROXY__COMPRESSION_FEEDBACK__ENABLED` | boolean | `true` | Record compression feedback. | Bundled |
| `MEMTOMEM_STM_PROXY__COMPRESSION_FEEDBACK__DB_PATH` | path | `~/.memtomem/stm_feedback.db` | Compression-feedback SQLite file. | Bundled |
| `MEMTOMEM_STM_PROXY__COMPRESSION_FEEDBACK__RETENTION_DAYS` | non-negative integer | `90` | Compression-feedback retention period. | Bundled |
| `MEMTOMEM_STM_PROXY__PROGRESSIVE_READS__ENABLED` | boolean | `true` | Record progressive-read events. | Bundled |
| `MEMTOMEM_STM_PROXY__PROGRESSIVE_READS__DB_PATH` | path | `~/.memtomem/stm_feedback.db` | Progressive-read SQLite file. | Bundled |
| `MEMTOMEM_STM_PROXY__PROGRESSIVE_READS__RETENTION_DAYS` | non-negative integer | `90` | Progressive-read retention period. | Bundled |
| `MEMTOMEM_STM_PROXY__SELECTION_TELEMETRY__ENABLED` | boolean | `false` | Enable tool-selection JSONL telemetry. | Bundled |
| `MEMTOMEM_STM_PROXY__SELECTION_TELEMETRY__PATH` | path | `~/.memtomem/stm_selection_log.jsonl` | Selection log path. | Bundled |
| `MEMTOMEM_STM_PROXY__SELECTION_TELEMETRY__SAMPLE_RATE` | float `0..1` | `1.0` | Selection-event sampling rate. | Bundled |
| `MEMTOMEM_STM_PROXY__SELECTION_TELEMETRY__MAX_BYTES` | positive integer | `50000000` | Selection-log rotation size. | Bundled |
| `MEMTOMEM_STM_PROXY__SELECTION_TELEMETRY__MAX_BACKUPS` | non-negative integer | `3` | Selection-log backup count. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOL_RELEVANCE__ENABLED` | boolean | `true` | Enable task-aware tool-candidate ranking. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOL_RELEVANCE__TOP_N` | positive integer | `20` | Candidate count retained by relevance ranking. | Bundled |
| `MEMTOMEM_STM_PROXY__EXPOSURE__PROFILE` | `strict / review / full` | `strict` | Tool-exposure profile. | Bundled |
| `MEMTOMEM_STM_PROXY__EXPOSURE__HEALTH_WINDOW_HOURS` | positive float | `24.0` | Health-history lookback. | Bundled |
| `MEMTOMEM_STM_PROXY__EXPOSURE__HEALTH_MIN_CALLS` | non-negative integer | `5` | Calls required for health-based exposure evidence. | Bundled |
| `MEMTOMEM_STM_PROXY__EXPOSURE__HEALTH_ERROR_RATE_THRESHOLD` | float `0..1` | `0.95` | Error-rate threshold for unhealthy exposure. | Bundled |
| `MEMTOMEM_STM_PROXY__EXPOSURE__REVIEW_RISK_PENALTY` | non-negative float | `0.5` | Ranking penalty for review-risk tools. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__ENABLED` | boolean | `false` | Enable Toolgraph gateway policy. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__SOURCE` | `stdio / bundle` | `stdio` | Toolgraph verdict source. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__BUNDLE_PATH` | path | `~/.memtomem/toolgraph/policy-bundle.json` | Policy-bundle path. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__COMMAND` | string | `toolgraph` | Toolgraph stdio executable. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__ARGS` | JSON string array | `["serve"]` | Toolgraph stdio arguments. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__ENV` | JSON object or `null` | — | Optional Toolgraph child environment. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__AGENT_ID` | string | `stm-proxy` | Toolgraph agent identity. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__SERVER_NAME_MAP` | JSON object | `{}` | STM-to-Toolgraph upstream-name mapping. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__QUERY_PROFILE` | string | `strict` | Toolgraph query profile. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__ON_UNREACHABLE` | `open / closed` | `open` | Policy when Toolgraph is unreachable. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__ON_AGENT_NOT_FOUND` | `fail_start / open / closed` | `fail_start` | Policy for an unknown agent. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__ON_PROTOCOL_ERROR` | `fail_start / open / closed` | `fail_start` | Policy for malformed provider data. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__ON_TOOL_NOT_FOUND` | `open / closed` | `open` | Policy for an unknown tool. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__RISK_PENALTY_SCALE` | non-negative float | `1.0` | Toolgraph risk-score penalty multiplier. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__TIMEOUT_SECONDS` | positive float | `5.0` | Toolgraph consult timeout. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__CONSULT_CACHE_ENABLED` | boolean | `true` | Enable consult-result caching. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__CONSULT_CACHE_PATH` | path | `~/.memtomem/toolgraph_consult.db` | Consult-cache SQLite file. | Bundled |
| `MEMTOMEM_STM_PROXY__TOOLGRAPH__CONSULT_CACHE_MAX_SCOPES` | positive integer | `64` | Maximum cached consult scopes. | Bundled |
| `MEMTOMEM_STM_SURFACING__ENABLED` | boolean | `true` | Enable proactive memory surfacing. | Bundled |
| `MEMTOMEM_STM_SURFACING__USE_DAEMON` | boolean | `false` | Route standalone surfacing through the shared daemon. | Startup |
| `MEMTOMEM_STM_SURFACING__WARMUP_ENABLED` | boolean | `true` | Warm the LTM connection after startup. | Startup |
| `MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH` | path | `~/.memtomem/stm_feedback.db` | Surfacing events, feedback, faults, and dedup SQLite file. | Bundled |
| `MEMTOMEM_STM_SURFACING__LTM_MCP_TRANSPORT` | `stdio / sse / streamable_http` | `stdio` | LTM MCP transport. | Startup |
| `MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND` | string | `memtomem-server` | LTM stdio executable. | Startup |
| `MEMTOMEM_STM_SURFACING__LTM_MCP_ARGS` | JSON string array | `[]` | LTM stdio arguments. | Startup |
| `MEMTOMEM_STM_SURFACING__LTM_MCP_URL` | URL | empty | LTM SSE/HTTP endpoint. | Startup |
| `MEMTOMEM_STM_SURFACING__LTM_MCP_HEADERS` | JSON object or `null` | — | LTM network headers; values may be secret. | Startup |
| `MEMTOMEM_STM_SURFACING__MIN_SCORE` | float `0..1` | `0.03` | Global result-score floor when scale gating permits it. | Bundled |
| `MEMTOMEM_STM_SURFACING__MAX_RESULTS` | positive integer | `3` | Maximum injected retrieval results. | Bundled |
| `MEMTOMEM_STM_SURFACING__MIN_QUERY_TOKENS` | positive integer | `3` | Minimum extracted query size. | Bundled |
| `MEMTOMEM_STM_SURFACING__COOLDOWN_SECONDS` | non-negative float | `5.0` | Per-query cooldown. | Bundled |
| `MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS` | positive float | `3.0` | LTM search deadline. | Bundled |
| `MEMTOMEM_STM_SURFACING__INJECTION_MODE` | `prepend / append / section` | `append` | Memory-block placement. | Bundled |
| `MEMTOMEM_STM_SURFACING__SECTION_HEADER` | string | `## Relevant Memories` | Header for section-mode injection. | Bundled |
| `MEMTOMEM_STM_SURFACING__DEFAULT_NAMESPACE` | string or `null` | — | Default Core namespace. | Bundled |
| `MEMTOMEM_STM_SURFACING__EXCLUDE_TOOLS` | JSON string array | `[]` | Tool patterns excluded from surfacing. | Bundled |
| `MEMTOMEM_STM_SURFACING__WRITE_TOOL_PATTERNS` | JSON string array | `["*write*","*create*","*delete*","*push*","*send*","*remove*"]` | Mutation-like tool patterns skipped by default. | Bundled |
| `MEMTOMEM_STM_SURFACING__CONTEXT_TOOLS` | JSON object | `{}` | Per-tool query, namespace, threshold, and count overrides. | Bundled |
| `MEMTOMEM_STM_SURFACING__FEEDBACK_ENABLED` | boolean | `true` | Accept surfacing ratings. | Bundled |
| `MEMTOMEM_STM_SURFACING__FEEDBACK_DEMOTION_ENABLED` | boolean | `true` | Enable local negative-feedback demotion. | Bundled |
| `MEMTOMEM_STM_SURFACING__FEEDBACK_DEMOTION_NEGATIVE_THRESHOLD` | positive integer | `3` | Distinct negative events required for demotion. | Bundled |
| `MEMTOMEM_STM_SURFACING__MAX_SURFACINGS_PER_MINUTE` | positive integer | `15` | Surfacing rate limit. | Bundled |
| `MEMTOMEM_STM_SURFACING__CACHE_TTL_SECONDS` | non-negative float | `60.0` | In-process retrieval-cache TTL. | Bundled |
| `MEMTOMEM_STM_SURFACING__CIRCUIT_MAX_FAILURES` | non-negative integer | `3` | Failures before opening the LTM breaker. | Bundled |
| `MEMTOMEM_STM_SURFACING__CIRCUIT_RESET_SECONDS` | positive float | `60.0` | LTM breaker reset interval. | Bundled |
| `MEMTOMEM_STM_SURFACING__AUTO_TUNE_ENABLED` | boolean | `true` | Tune the global score floor from eligible feedback. | Bundled |
| `MEMTOMEM_STM_SURFACING__AUTO_TUNE_MIN_SAMPLES` | positive integer | `20` | Feedback samples required before tuning. | Bundled |
| `MEMTOMEM_STM_SURFACING__AUTO_TUNE_SCORE_INCREMENT` | positive float | `0.002` | Score-floor adjustment step. | Bundled |
| `MEMTOMEM_STM_SURFACING__AUTO_TUNE_SCORE_CEILING` | float `0..1` | `0.05` | Default upper tuning bound. | Bundled |
| `MEMTOMEM_STM_SURFACING__AUTO_TUNE_SCORE_FLOOR` | float `0..1` | `0.005` | Default lower tuning bound. | Bundled |
| `MEMTOMEM_STM_SURFACING__MIN_RESPONSE_CHARS` | non-negative integer | `5000` | Cleaned response-size eligibility gate. | Bundled |
| `MEMTOMEM_STM_SURFACING__INCLUDE_SESSION_CONTEXT` | boolean | `true` | Include Core working-memory context when available. | Bundled |
| `MEMTOMEM_STM_SURFACING__FIRE_WEBHOOK` | boolean | `true` | Request Core-side search webhook behavior. | Bundled |
| `MEMTOMEM_STM_SURFACING__MAX_INJECTION_CHARS` | positive integer | `3000` | Hard ceiling for rendered memory injection. | Bundled |
| `MEMTOMEM_STM_SURFACING__CONTEXT_WINDOW_SIZE` | non-negative integer | `0` | Adjacent chunks requested on each side. | Bundled |
| `MEMTOMEM_STM_SURFACING__RESULT_CONTENT_MAX_CHARS` | positive integer | `500` | Content retained per parsed LTM result. | Bundled |
| `MEMTOMEM_STM_SURFACING__PREVIEW_MAX_CHARS` | positive integer | `300` | Preview retained per rendered memory. | Bundled |
| `MEMTOMEM_STM_SURFACING__DEDUP_TTL_SECONDS` | non-negative float | `604800.0` | Cross-session seen-memory window. | Bundled |
| `MEMTOMEM_STM_SURFACING__QUERY_RETENTION_DAYS` | non-negative integer | `30` | Raw persisted query retention. | Bundled |
| `MEMTOMEM_STM_SURFACING__STATS_RETENTION_DAYS` | non-negative integer | `90` | Surfacing event/feedback row retention. | Bundled |
| `MEMTOMEM_STM_SURFACING__PERSIST_QUERY_TEXT` | boolean | `true` | Persist raw query text instead of a short digest. | Bundled |
| `MEMTOMEM_STM_SURFACING__CONSUMER_MODEL` | string | empty | Explicit surfacing consumer model. | Bundled |
| `MEMTOMEM_STM_SURFACING__RESULT_FORMAT` | `compact / structured` | `structured` | Legacy `mem_search` response format. | Bundled |
| `MEMTOMEM_STM_SURFACING__RERANK` | boolean or `none` | `false` | Force, bypass, or defer Core reranking when supported. | Bundled |
| `MEMTOMEM_STM_SURFACING__SCALE_GATED_MIN_SCORE` | boolean | `true` | Suspend the RRF floor on Core-named foreign score scales. | Bundled |
| `MEMTOMEM_STM_FORMATION__ENABLED` | boolean | `false` | Advertise and enable review-first candidate proposals. | Startup |
| `MEMTOMEM_STM_FORMATION__MAX_CONTENT_CHARS` | integer `1..2000` | `2000` | Maximum proposed candidate content. | Bundled |
| `MEMTOMEM_STM_LANGFUSE__ENABLED` | boolean | `false` | Enable Langfuse tracing. | Startup |
| `MEMTOMEM_STM_LANGFUSE__PUBLIC_KEY` | string | empty | Langfuse public key. | Startup |
| `MEMTOMEM_STM_LANGFUSE__SECRET_KEY` | secret string | empty | Langfuse secret key. | Startup |
| `MEMTOMEM_STM_LANGFUSE__HOST` | URL | empty | Langfuse host. | Startup |
| `MEMTOMEM_STM_LANGFUSE__SAMPLING_RATE` | float `0..1` | `1.0` | Proxy-call trace sampling rate. | Startup |
| `MEMTOMEM_STM_OTLP__ENABLED` | boolean | `false` | Export STM spans over OTLP/HTTP. | Startup |
| `MEMTOMEM_STM_OTLP__ENDPOINT` | URL | empty | OTLP base or traces endpoint; required when enabled. | Startup |
| `MEMTOMEM_STM_OTLP__HEADERS` | JSON object | `{}` | Syntax-checked OTLP HTTP headers; values may be secret. | Startup |
| `MEMTOMEM_STM_OTLP__TIMEOUT_SECONDS` | positive float | `10.0` | Per-export HTTP timeout. | Startup |
| `MEMTOMEM_STM_OTLP__SAMPLING_RATE` | float `0..1` | `1.0` | Parent-based head sampling ratio. | Startup |
| `MEMTOMEM_STM_OTLP__MAX_QUEUE_SIZE` | positive integer | `2048` | Batch processor queue depth. | Startup |
| `MEMTOMEM_STM_OTLP__MAX_EXPORT_BATCH_SIZE` | positive integer | `512` | Spans per request; no greater than queue size. | Startup |
| `MEMTOMEM_STM_OTLP__SCHEDULE_DELAY_MS` | positive integer | `5000` | Batch flush interval. | Startup |
| `MEMTOMEM_STM_OTLP__FLUSH_TIMEOUT_SECONDS` | positive float | `5.0` | Whole OTLP shutdown budget. | Startup |
| `MEMTOMEM_STM_HOOK__USE_DAEMON` | boolean | `true` | Use the shared warm surfacing daemon. | Bundled |
| `MEMTOMEM_STM_HOOK__DAEMON_TIMEOUT_SECONDS` | positive float | `2.5` | Outer hook-to-daemon deadline. | Bundled |
| `MEMTOMEM_STM_HOOK__FALLBACK` | `skip / cold` | `skip` | Behavior when the daemon is unavailable. | Bundled |
| `MEMTOMEM_STM_HOOK__AUTO_SPAWN` | boolean | `true` | Start a missing daemon. | Bundled |
| `MEMTOMEM_STM_HOOK__RECORD_FEEDBACK_EVENTS` | boolean | `false` | Enable rating prompts, durable-demotion reads, and auto-tune feedback processing. Surfacing telemetry rows still persist when false. | Bundled |
| `MEMTOMEM_STM_HOOK__COMPRESSION__ENABLED` | boolean | `false` | Enable guarded Claude Bash output replacement. | Bundled |
| `MEMTOMEM_STM_HOOK__COMPRESSION__MAX_CHARS` | positive integer | `16000` | Hook compression target size. | Bundled |
| `MEMTOMEM_STM_HOOK__COMPRESSION__MIN_RETENTION` | float `0..1` | `0.65` | Minimum allowed native-output retained fraction. | Bundled |
| `MEMTOMEM_STM_HOOK__METRICS_ENABLED` | boolean | `true` | Record size/timing-only native-hook metrics. | Bundled |
| `MEMTOMEM_STM_DAEMON__HOST` | host string | `127.0.0.1` | Daemon bind host. | Startup |
| `MEMTOMEM_STM_DAEMON__ALLOW_NON_LOOPBACK` | boolean | `false` | Explicitly permit a non-loopback daemon bind. | Startup |
| `MEMTOMEM_STM_DAEMON__IDLE_TIMEOUT_SECONDS` | non-negative float | `900.0` | Idle shutdown delay; zero pins the daemon. | Startup |
| `MEMTOMEM_STM_DAEMON__MAX_PENDING_REQUESTS` | integer `1..1024` | `32` | Bound concurrently admitted surfacing requests. | Startup |
| `MEMTOMEM_STM_DATA_DIR` | path | `~/.memtomem` | Daemon handshakes, locks, and detached-log directory. | Startup |
| `MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS` | boolean | `false` | Advertise eight operator/admin MCP tools at import time. | Startup |
<!-- stmconfig-env:end -->

### Caveat: `EXTRACTION__LLM__*` defaults are not the extractor's defaults

The `llm` block is **absent** by default, and while it is absent the extractor
does not use the field defaults tabulated above. It falls back to a separate
extraction profile: provider `ollama`, model `qwen3:4b`, base URL
`http://localhost:11434`, `max_tokens` 1000, and an extraction-specific prompt.

Setting any single `MEMTOMEM_STM_PROXY__EXTRACTION__LLM__*` variable
materializes the whole block, and **every sibling you did not set then takes the
field default from the table above** — not the extraction profile. Which
consequences you get therefore depends on which siblings you leave unset:

| Sibling left unset | Value it takes | Consequence |
|---|---|---|
| `__PROVIDER` | `openai` | Startup raises a validation error unless `__API_KEY` or `OPENAI_API_KEY` is set. Setting `__PROVIDER=ollama` avoids both. |
| `__SYSTEM_PROMPT` | the compression prompt, which contains `{max_chars}` | The extraction path substitutes `{max_facts}` only, so formatting raises and extraction falls back to heuristic extraction. Setting an extraction prompt avoids this. |
| `__MODEL`, `__MAX_TOKENS` | `gpt-4.1-mini`, `500` | Silent change from the extraction profile's `qwen3:4b` / `1000`. |

So no single variable is safe to set alone unless you also set the siblings it
implicates. Configure the block wholesale — as a JSON object at
`MEMTOMEM_STM_PROXY__EXTRACTION__LLM`, or with every field set explicitly —
rather than by overriding one leaf.

Unknown proxy keys are ignored by the permissive file loader but reported by
`mms config validate`; unknown environment suffixes are not supported. See
[Proxy configuration](proxy-config.md), [surfacing](../surfacing.md),
[compression](../compression.md), and [OTLP export](../otlp-export.md) for
behavior and failure policy.

## Direct runtime and integration variables

These variables are read outside the nested settings inventory or belong to a
provider, host, or standard SDK. They do not change `STMConfig` completeness.

| Variable | Purpose |
|---|---|
| `MEMTOMEM_STM_HOOK_SURFACE_TOOLS` | Legacy comma-separated canonical native-tool allowlist (`read,grep,glob,shell,web_fetch`); mutating `write`/`edit` tokens are refused. |
| `MMS_CLIENT_SERVER_NAME` | Client-visible STM registration name used only for composed tool-name budget checks. |
| `MMS_NO_TUI` | Disable the interactive server-discovery TUI. |
| `NO_COLOR` | Disable ANSI color in CLI output. |
| `OPENAI_API_KEY` | Fallback credential for OpenAI compression or embedding relevance scoring. |
| `ANTHROPIC_API_KEY` | Fallback credential for Anthropic compression. |
| `CODEX_HOME` | Codex user config discovery root; defaults to `~/.codex`. |
| `CLAUDE_CONFIG_DIR` | Claude hook config discovery root. |
| `KIMI_CODE_HOME` | Kimi Code hook config discovery root. |
| `APPDATA` | Native Windows Claude Desktop config discovery root. |
| `OTEL_SERVICE_NAME` | Standard OpenTelemetry service name; STM supplies its configured/default name only when this is absent. |
| `OTEL_RESOURCE_ATTRIBUTES` and standard `OTEL_EXPORTER_OTLP_*` controls | Read by the OpenTelemetry SDK for resource/TLS/compression settings not owned by STM. `OTEL_EXPORTER_OTLP_HEADERS` and `OTEL_EXPORTER_OTLP_TRACES_HEADERS` are rejected while STM OTLP export is enabled; put credentials in `MEMTOMEM_STM_OTLP__HEADERS`. |

Host discovery variables select existing user locations; they are not copied
into subprocess environments as credentials. Provider keys are read only when
the corresponding hosted provider is selected.
