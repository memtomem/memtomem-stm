# Testing

```bash
# Run all tests
uv run pytest tests/ -v

# Run a specific test file
uv run pytest tests/test_compression.py -v

# Skip Ollama-dependent tests (CI uses this filter)
uv run pytest -m "not ollama"
```

**731 tests** organized by topic:

| Test file | Coverage |
|-----------|----------|
| `test_circuit_breaker.py` | State machine transitions (closed / open / half-open) |
| `test_compression.py` | All compression strategies + auto_select_strategy |
| `test_relevance_gate.py` | Exclusions, write-tool heuristic, rate limit, cooldown, Jaccard similarity |
| `test_context_extractor.py` | Query templates, heuristic extraction, path tokenization, identifier detection |
| `test_feedback.py` | FeedbackStore, FeedbackTracker, AutoTuner feedback loop, cold-start fallback |
| `test_proxy_cache.py` | TTL expiration, eviction, clear, key generation |
| `test_cleaning.py` | HTML stripping, script/style removal, deduplication, link flood collapse |
| `test_surfacing_cache.py` | In-memory TTL cache, eviction, empty list caching |
| `test_surfacing_engine.py` | Surfacing pipeline, session dedup, circuit breaker, cooldown, rate limit, feedback boost |
| `test_formatter.py` | Injection modes (prepend / append / section), size cap, source badges |
| `test_proxy_manager.py` | ToolConfig resolution, error handling, cleaning + compression pipeline |
| `test_config_persistence.py` | Hot reload, MetricsStore, FeedbackStore persistence, TokenTracker, privacy patterns |
| `test_stm_integration.py` | End-to-end pipeline (clean → compress → surface), selective 2-phase, auto-tuner loop |
| `test_effectiveness.py` | Decision quality, context extraction accuracy, compression ratio, feedback loop |
| `test_information_loss.py` | Content preservation across compression strategies, structural integrity |
| `test_proxy_error_paths.py` | Transport failure retry / reconnect, protocol error, exponential backoff, cache interaction |
| `test_latency_percentiles.py` | Percentile computation (p50 / p95 / p99), TokenTracker integration |
| `test_cross_session_dedup.py` | SQLite seen_memories persistence, TTL-based dedup, engine integration |
| `test_stress_concurrency.py` | 1-5 MB payloads, concurrent calls, SelectiveCompressor lock |
| `test_auto_compression.py` | AUTO strategy selection, passthrough, per-content-type routing |
| `test_error_metrics.py` | ErrorCategory enum, record_error, MetricsStore migration, CircuitBreaker properties |
| `test_tool_metadata.py` | Description truncation, schema distillation, hidden tools, token savings |
| `test_context_window.py` | Model context window lookup, effective_max_result_chars, prefix matching |
| `test_observability.py` | RPSTracker, trace_id propagation, MetricsStore trace_id, upstream health |
| `test_pending_store.py` | InMemory / SQLite PendingStore, persistence, concurrency, multi-instance sharing |
| `test_extraction.py` | Auto fact extraction: JSON parsing, heuristic / LLM strategies, circuit breaker |
| `test_progressive.py` | Progressive delivery: chunker boundaries, store adapter, content integrity |
| `test_bench_pipeline.py` | 181 benchmark tests: quality scoring, statistical analysis, dataset coverage |

CI runs these on every PR via `.github/workflows/ci.yml` (lint, typecheck, test). Contributions welcome.
