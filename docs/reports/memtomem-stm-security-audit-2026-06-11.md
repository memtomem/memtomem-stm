# memtomem-stm Security Audit Report

Date: 2026-06-11
Branch: `fix/config-validation-diagnostics`
Scope: local source/config audit for `src/memtomem_stm/`, `SECURITY.md`, selected docs, security-adjacent tests, and local persisted-state file modes.

## Executive Summary

No critical remote-code-execution issue was found in the reviewed paths. The main risk is data confidentiality: the documented sensitive-content exclusion guarantee is not applied consistently to every persistence and external-LLM path.

The strongest existing controls are good: subprocess use is list-argument based, dangerous imported env keys are filtered, proxy config/cache/metrics writes are atomic/private, network LTM URL userinfo is redacted in logs, progressive/selective transient keys are excluded from the response cache, and AnyIO shutdown handling is guarded.

## Findings

### F1. Sensitive upstream responses can still be persisted in the response cache and LTM index

Severity: High
Status: Confirmed

`SECURITY.md:26` says responses that look like secrets are excluded from the response cache and LTM indexing. Current code only wires the privacy scan into the LLM compression route (`src/memtomem_stm/proxy/manager.py:731-740`). The cache write path checks for transient retrieval keys, not sensitive content (`src/memtomem_stm/proxy/manager.py:2046-2066`), and `ProxyCache.set()` stores the `result` directly (`src/memtomem_stm/proxy/cache.py:165-175`).

The auto-index path also writes the full response text into Markdown before indexing (`src/memtomem_stm/proxy/memory_ops.py:147-157`) without a `contains_sensitive_content()` gate. Auto-index is opt-in, but response cache is enabled by default.

Impact: an upstream tool response containing API keys, tokens, private keys, `.env` contents, or similar can be written to `~/.memtomem/proxy_cache.db`; when auto-index is enabled, it can also be written into the configured memory directory and indexed into LTM.

Recommendation: add a common pre-persistence guard for cache, auto-index, and extraction. On a privacy hit, skip persistence, record a metrics/observability reason, and return the tool response normally. Add regression tests for cache skip and auto-index skip on default privacy patterns.

### F2. LLM fact extraction sends raw response text to remote providers without the compression privacy guard

Severity: High when extraction uses OpenAI/Anthropic/custom remote base URL; otherwise Medium/Low with local Ollama
Status: Confirmed

`FactExtractor._extract_llm()` calls `_call_api(text)` directly (`src/memtomem_stm/proxy/extraction.py:223-224`). `_openai()` and `_anthropic()` then send the raw `text` as the user message (`src/memtomem_stm/proxy/extraction.py:272-281`, `src/memtomem_stm/proxy/extraction.py:302-310`). This path reuses `LLMCompressorConfig`, but does not honor `privacy_scan_enabled`; the equivalent compression path does honor it.

Impact: if `extraction.enabled=true` and the extraction LLM is configured for OpenAI, Anthropic, or a remote custom endpoint, secret-bearing upstream responses can leave the local machine. This is more severe than local cache persistence because it crosses a network/provider trust boundary.

Recommendation: before `_call_api()`, run the same privacy scan used by `LLMCompressor`. On a hit, skip the remote call and fall back to heuristic extraction or no extraction, with an observable fallback reason. Add tests proving `_call_api` is not invoked for secret-bearing text.

### F3. Shared feedback/progressive SQLite stores are not chmod-corrected to 0600

Severity: Medium
Status: Confirmed in code and on this machine

`ProxyCache` and `MetricsStore` explicitly chmod their DB files to `0600` after opening (`src/memtomem_stm/proxy/cache.py:93-99`, `src/memtomem_stm/proxy/metrics_store.py:169-176`). The shared feedback stores do not: `FeedbackStore`, `CompressionFeedbackStore`, `ProgressiveReadsStore`, and `SQLitePendingStore` create/open SQLite files after ensuring the parent path but never chmod the DB file (`src/memtomem_stm/surfacing/feedback_store.py:246-252`, `src/memtomem_stm/proxy/compression_feedback_store.py:92-99`, `src/memtomem_stm/proxy/progressive_reads_store.py:64-70`, `src/memtomem_stm/proxy/pending_store.py:98-103`).

Local check found:

```text
/Users/pdstudio/.memtomem          drwxr-xr-x
/Users/pdstudio/.memtomem/stm_feedback.db  -rw-r--r--
```

Impact: `stm_feedback.db` can contain surfacing queries, memory IDs, ratings, compression feedback text, and progressive read telemetry. Query hashing protects recognized secret patterns, but the file may still disclose sensitive paths, topics, and operational context to other local users/processes when the parent directory or DB mode is permissive.

Recommendation: centralize SQLite private-file initialization and apply `chmod(0o600)` in every local store, including existing files. Consider also warning or correcting a permissive `~/.memtomem` directory.

### F4. Auto-index file names trust raw server names

Severity: Low
Status: Confirmed hardening gap

`auto_index_response()` sanitizes `/` in the tool name but not in the `server` name before constructing the file name (`src/memtomem_stm/proxy/memory_ops.py:120-126`). Server names normally come from trusted config/import flows, and auto-index is off by default, so this is not an immediate exploit in the default setup.

Impact: a malicious or hand-edited config with path separators in a server key could make the auto-index write target less predictable if auto-index is enabled.

Recommendation: validate upstream server keys or slug both `server` and `tool` before file-path construction. Create auto-index/extraction directories with explicit private mode.

## Positive Controls Observed

- No real secrets were found by the high-signal secret-pattern scan outside test fixtures.
- `mms add --env` and host import strip dangerous subprocess-hijacking env keys such as `LD_PRELOAD`, `DYLD_INSERT_LIBRARIES`, `PYTHONPATH`, and `NODE_OPTIONS`.
- LLM compression blocks sensitive content before outbound provider calls by default.
- Surfacing query persistence hashes queries matching known secret patterns.
- Cache now skips progressive/selective transient retrieval keys.
- Network LTM reconnect handling includes `httpx.TransportError`, and log display redacts URL userinfo.
- Daemon handshake uses per-start random tokens and `0o600` atomic writes.

## Verification

Commands run:

```text
rg secret-pattern scan: only test fixtures matched
uv run pytest tests/test_privacy.py tests/test_proxy_cache.py tests/test_config_constraints.py tests/test_mcp_client_reconnect.py tests/daemon/test_server.py tests/test_proxy_manager_pipeline.py tests/test_memory_ops.py tests/mms/test_import_hosts.py -q
uv run ruff check src tests
find /Users/pdstudio/.memtomem -maxdepth 1 -type f -name '*.db' -ls
ls -ld /Users/pdstudio/.memtomem
```

Results:

```text
299 passed in 3.01s
ruff: All checks passed
pip-audit: not run; `uv run python -m pip_audit` failed because `pip_audit` is not installed in the venv
```

## Limitations

This audit did not query external CVE databases and did not run dynamic fuzzing. Dependency vulnerability status is therefore not asserted. The findings above are based on current local source, tests, docs, and local filesystem modes.
