"""Tests for STM config loading, metrics persistence, token tracking, and privacy detection."""

from __future__ import annotations

import json
import os
import time


from memtomem_stm.proxy.config import (
    ProxyConfig,
    ProxyConfigLoader,
    RelevanceScorerConfig,
)
from memtomem_stm.proxy.metrics import CallMetrics, TokenTracker
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.proxy.privacy import contains_sensitive_content


# ── ProxyConfigLoader hot-reload ─────────────────────────────────────────


class TestProxyConfigLoader:
    def test_load_from_file(self, tmp_path):
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({
            "enabled": True,
            "upstream_servers": {
                "gh": {"prefix": "gh", "command": "gh-server"}
            },
        }))
        loader = ProxyConfigLoader(cfg_file)
        config = loader.get()
        assert config.enabled is True
        assert "gh" in config.upstream_servers

    def test_hot_reload_on_change(self, tmp_path):
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"enabled": False}))
        loader = ProxyConfigLoader(cfg_file)
        loader.seed(ProxyConfig.load_from_file(cfg_file))

        c1 = loader.get()
        assert c1.enabled is False

        # Modify file (force mtime change)
        time.sleep(0.05)
        cfg_file.write_text(json.dumps({"enabled": True}))

        c2 = loader.get()
        assert c2.enabled is True

    def test_missing_file_returns_default(self, tmp_path):
        loader = ProxyConfigLoader(tmp_path / "nonexistent.json")
        config = loader.get()
        assert config.enabled is False  # default
        assert config.upstream_servers == {}

    def test_seed_uses_cached(self, tmp_path):
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"enabled": True}))
        loader = ProxyConfigLoader(cfg_file)
        seeded = ProxyConfig(enabled=True)
        loader.seed(seeded)
        assert loader.get() is seeded  # same object, not re-parsed

    def test_parse_failure_does_not_block_subsequent_reload(self, tmp_path):
        """If a fix lands within filesystem mtime granularity after a parse
        failure, the next get() must still pick it up — the loader must not
        treat the broken file's mtime as "already seen".
        """
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"enabled": False}))
        loader = ProxyConfigLoader(cfg_file)

        assert loader.get().enabled is False

        # Write broken JSON (its mtime differs so the loader notices).
        time.sleep(0.05)
        cfg_file.write_text("{ not valid json")
        broken_mtime = cfg_file.stat().st_mtime

        # Parse fails — the loader must keep the previously cached good config.
        assert loader.get().enabled is False

        # Simulate a fix that lands at the same mtime as the broken write
        # (e.g., a rapid in-place edit on a coarse-grained filesystem).
        cfg_file.write_text(json.dumps({"enabled": True}))
        os.utime(cfg_file, (broken_mtime, broken_mtime))

        # Without the fix, the loader stored broken_mtime when parsing failed
        # and now sees the same mtime → skips reload → returns stale config.
        assert loader.get().enabled is True

    def test_unseeded_loader_with_broken_file_returns_defaults(self, tmp_path):
        """An unseeded loader whose first load fails has no cache to fall back
        on, and used to return None with the type error suppressed.

        ``ProxyManager`` seeds its loader in ``__init__``, so this is NOT
        reachable from the production request path; it is the contract for
        anyone constructing a ``ProxyConfigLoader`` directly (tooling and
        tests do). Every caller does attribute access on the result, so None
        is never a usable answer — return defaults instead.
        """
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text("{ not valid json")
        loader = ProxyConfigLoader(cfg_file)

        config = loader.get()
        assert isinstance(config, ProxyConfig)
        assert config.enabled is False  # defaults

        # The parse-failure retry contract still holds: a later fix is picked
        # up even though the failed load returned defaults.
        time.sleep(0.05)
        cfg_file.write_text(json.dumps({"enabled": True}))
        assert loader.get().enabled is True

    def test_current_reports_only_seeded_or_loaded_generations(self, tmp_path):
        """``current`` is an identity check, so its None must mean "cannot compare".

        The unseeded fallbacks build a config per call without recording it —
        recording one would advance state the parse-failure retry depends on.
        So a loader can hand out a config and still answer None here; that is
        the safe answer for a staleness check, and the docstring says so.
        """
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text("{ not valid json")
        broken = ProxyConfigLoader(cfg_file)
        assert broken.current is None
        broken.get()
        assert broken.current is None, "a per-call fallback must not pose as a generation"

        time.sleep(0.05)
        cfg_file.write_text(json.dumps({"enabled": True}))
        loaded = broken.get()
        assert broken.current is loaded, "a real load must be comparable by identity"

    def test_duplicate_prefix_reload_keeps_previous_good_config(self, tmp_path):
        """If a hot-reloaded config violates the duplicate-prefix validator,
        the loader must keep the previously cached good config rather than
        reverting to defaults — a running proxy stays serving its last
        known-good upstreams instead of going dark."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({
            "enabled": True,
            "upstream_servers": {"gh": {"prefix": "gh", "command": "gh-server"}},
        }))
        loader = ProxyConfigLoader(cfg_file)
        good = loader.get()
        assert good.enabled is True
        assert "gh" in good.upstream_servers

        # User edits the file, accidentally giving two upstreams the same prefix.
        time.sleep(0.05)
        cfg_file.write_text(json.dumps({
            "enabled": True,
            "upstream_servers": {
                "gh": {"prefix": "dup", "command": "gh-server"},
                "gh2": {"prefix": "dup", "command": "gh-server-2"},
            },
        }))

        # Loader must keep the previous good config, not fall back to defaults.
        reloaded = loader.get()
        assert reloaded.enabled is True
        assert reloaded.upstream_servers["gh"].prefix == "gh"
        assert "gh2" not in reloaded.upstream_servers


# ── load_from_file_with_status / unknown-key warning (#611) ──────────────


class TestLoadFromFileWithStatus:
    def test_valid_file_no_error(self, tmp_path):
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"enabled": True}))
        result = ProxyConfig.load_from_file_with_status(cfg_file)
        assert result.config is not None and result.config.enabled is True
        assert result.error is None
        assert result.unknown_keys == ()

    def test_parse_failure_sets_error(self, tmp_path):
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text("{ not valid json")
        result = ProxyConfig.load_from_file_with_status(cfg_file)
        assert result.config is None
        assert result.error is not None

    def test_validation_failure_sets_error_and_keeps_unknown_keys(self, tmp_path):
        # Duplicate prefixes fail the model validator; the typo'd key found
        # before validation must survive into the result for `mms config
        # validate` to report both.
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({
            "bogus_key": 1,
            "upstream_servers": {
                "a": {"prefix": "dup", "command": "a"},
                "b": {"prefix": "dup", "command": "b"},
            },
        }))
        result = ProxyConfig.load_from_file_with_status(cfg_file)
        assert result.config is None
        # loc + type, not the raw validator message (which embeds prefixes).
        assert result.error is not None and "value_error" in result.error
        assert result.unknown_keys == ("bogus_key",)

    def test_missing_file_is_not_an_error(self, tmp_path):
        missing = tmp_path / "nonexistent.json"
        strict = ProxyConfig.load_from_file_with_status(missing, missing_ok=False)
        assert strict.config is None and strict.error is None
        lenient = ProxyConfig.load_from_file_with_status(missing)
        assert lenient.config is not None and lenient.error is None

    def test_unknown_keys_warned_once_aggregated(self, tmp_path, caplog):
        import logging

        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({
            "enabled": True,
            "max_result_char": 4000,
            "cache": {"ttl_secondz": 60},
        }))
        with caplog.at_level(logging.WARNING):
            result = ProxyConfig.load_from_file_with_status(cfg_file)
        assert result.config is not None
        assert result.unknown_keys == ("cache.ttl_secondz", "max_result_char")
        warnings = [r for r in caplog.records if "unknown key" in r.getMessage()]
        assert len(warnings) == 1
        assert "cache.ttl_secondz" in warnings[0].getMessage()
        assert "max_result_char" in warnings[0].getMessage()

    def test_env_injected_keys_not_flagged_as_file_typos(self, tmp_path, caplog):
        import logging

        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"enabled": True}))
        # An env overlay carrying a key the file doesn't have (even a bogus
        # one — extra="ignore" accepts it) must not surface as a file typo:
        # the walk runs on the raw file dict before the merge.
        overrides = {"default_max_result_chars": "9000", "bogus_env_key": "1"}
        with caplog.at_level(logging.WARNING):
            result = ProxyConfig.load_from_file_with_status(cfg_file, overrides)
        assert result.config is not None
        assert result.config.default_max_result_chars == 9000
        assert result.unknown_keys == ()
        assert not [r for r in caplog.records if "unknown key" in r.getMessage()]

    def test_error_never_carries_input_values(self, tmp_path):
        """codex review of #611: str(ValidationError) embeds input_value=...,
        and a mistyped secret-bearing field (headers/env) would flow to the
        MCP client via stm_proxy_health. The error must carry location +
        message only."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({
            "upstream_servers": {
                "gh": {"prefix": "gh", "headers": "Bearer SECRET_TOKEN_ABC"}
            }
        }))
        result = ProxyConfig.load_from_file_with_status(cfg_file)
        assert result.config is None
        assert result.error is not None
        assert "SECRET_TOKEN_ABC" not in result.error
        assert "upstream_servers.gh.headers" in result.error

    def test_error_never_carries_custom_validator_values(self, tmp_path):
        """Round-2 codex: pydantic input_value was only one leak channel —
        a custom model-validator renders raw values into its message too
        (the duplicate-prefix check embeds the prefix string). A secret
        typo'd into a `prefix` field must not reach ConfigLoadResult.error,
        which flows to the MCP client via stm_proxy_health."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({
            "upstream_servers": {
                "a": {"prefix": "SECRET_TOKEN_ABC", "command": "a"},
                "b": {"prefix": "SECRET_TOKEN_ABC", "command": "b"},
            }
        }))
        result = ProxyConfig.load_from_file_with_status(cfg_file)
        assert result.config is None
        assert result.error is not None
        assert "SECRET_TOKEN_ABC" not in result.error
        assert "value_error" in result.error

    def test_log_warnings_false_suppresses_advisory_warnings(self, tmp_path, caplog):
        """codex review of #611: ProxyManager.start()'s empty-upstreams
        fallback re-loads a file the server startup already loaded — advisory
        warnings (permissive mode, unknown keys) must not fire twice, while
        the unknown_keys stay in the result and parse failures still log."""
        import logging

        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"enabled": True, "max_result_char": 1}))
        cfg_file.chmod(0o644)
        with caplog.at_level(logging.WARNING):
            result = ProxyConfig.load_from_file_with_status(cfg_file, log_warnings=False)
        assert result.config is not None
        assert result.unknown_keys == ("max_result_char",)
        assert not [
            r
            for r in caplog.records
            if "unknown key" in r.getMessage() or "permissive mode" in r.getMessage()
        ]

        caplog.clear()
        cfg_file.write_text("{ broken")
        with caplog.at_level(logging.WARNING):
            result = ProxyConfig.load_from_file_with_status(cfg_file, log_warnings=False)
        assert result.config is None
        assert [r for r in caplog.records if "Failed to parse" in r.getMessage()]

    def test_upstreams_with_disabled_proxy_warn_inert(self, tmp_path, caplog):
        """#831: upstream tools are only registered behind the enabled gate,
        so a disabled proxy with configured servers advertises nothing while
        every direct probe of those servers still succeeds. The advisory
        names which of the two ways the proxy ended up disabled."""
        import logging

        cfg_file = tmp_path / "stm_proxy.json"
        servers = {"upstream_servers": {"fx": {"prefix": "fx", "command": "fx-server"}}}
        for extra, expected in (
            ({}, '"enabled" is unset and defaults to false'),
            ({"enabled": False}, "disabled explicitly"),
        ):
            caplog.clear()
            cfg_file.write_text(json.dumps({**servers, **extra}))
            with caplog.at_level(logging.WARNING):
                result = ProxyConfig.load_from_file_with_status(cfg_file)
            assert result.config is not None
            advisories = [r for r in caplog.records if "present but inert" in r.getMessage()]
            assert len(advisories) == 1, extra
            assert expected in advisories[0].getMessage()

    def test_env_only_config_without_file_warns_inert(self, tmp_path, caplog):
        """An env-only startup has no file to read, so it takes the early
        missing-file return — the one shape with nothing to inspect must not
        also be the one shape that warns about nothing."""
        import logging

        missing = tmp_path / "stm_proxy.json"
        overrides = {"upstream_servers": {"fx": {"prefix": "fx", "command": "fx-server"}}}
        with caplog.at_level(logging.WARNING):
            result = ProxyConfig.load_from_file_with_status(missing, overrides)
        assert result.config is not None and not result.config.enabled
        advisories = [r for r in caplog.records if "present but inert" in r.getMessage()]
        assert len(advisories) == 1
        assert '"enabled" is unset' in advisories[0].getMessage()

        caplog.clear()
        with caplog.at_level(logging.WARNING):
            ProxyConfig.load_from_file_with_status(missing, {**overrides, "enabled": True})
        assert not [r for r in caplog.records if "present but inert" in r.getMessage()]

    def test_inert_upstream_warning_suppressed_when_serving(self, tmp_path, caplog):
        """Silent in both non-inert shapes — enabled (env-enabled included,
        since the advisory reads the merged data) and no upstreams at all."""
        import logging

        cfg_file = tmp_path / "stm_proxy.json"
        servers = {"upstream_servers": {"fx": {"prefix": "fx", "command": "fx-server"}}}
        cases: list[tuple[dict, dict | None]] = [
            ({"enabled": True, **servers}, None),
            (servers, {"enabled": True}),
            ({"enabled": False}, None),
        ]
        for data, overrides in cases:
            caplog.clear()
            cfg_file.write_text(json.dumps(data))
            with caplog.at_level(logging.WARNING):
                result = ProxyConfig.load_from_file_with_status(cfg_file, overrides)
            assert result.config is not None
            assert not [
                r for r in caplog.records if "present but inert" in r.getMessage()
            ], (data, overrides)

    def test_missing_cache_policy_warns_with_migration_hint(self, tmp_path, caplog):
        """A legacy file that never sets cache.tool_annotation_policy keeps the
        conservative default but gets a one-line migration advisory (new
        configs are written with an explicit "strict")."""
        import logging

        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"enabled": True}))
        with caplog.at_level(logging.WARNING):
            result = ProxyConfig.load_from_file_with_status(cfg_file)
        assert result.config is not None
        assert result.config.cache.tool_annotation_policy == "conservative"
        advisories = [
            r for r in caplog.records if "tool_annotation_policy" in r.getMessage()
        ]
        assert len(advisories) == 1
        msg = advisories[0].getMessage()
        assert '"cache": {"tool_annotation_policy": "strict"}' in msg

    def test_cache_block_without_policy_still_warns(self, tmp_path, caplog):
        import logging

        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"enabled": True, "cache": {"max_entries": 5}}))
        with caplog.at_level(logging.WARNING):
            ProxyConfig.load_from_file_with_status(cfg_file)
        assert [r for r in caplog.records if "tool_annotation_policy" in r.getMessage()]

    def test_cache_policy_present_suppresses_warning(self, tmp_path, caplog):
        import logging

        cfg_file = tmp_path / "stm_proxy.json"
        for policy in ("conservative", "strict", "ignore"):
            caplog.clear()
            cfg_file.write_text(
                json.dumps({"enabled": True, "cache": {"tool_annotation_policy": policy}})
            )
            with caplog.at_level(logging.WARNING):
                result = ProxyConfig.load_from_file_with_status(cfg_file)
            assert result.config is not None
            assert not [
                r for r in caplog.records if "tool_annotation_policy" in r.getMessage()
            ], policy

    def test_env_override_policy_suppresses_missing_policy_warning(self, tmp_path, caplog):
        """The advisory checks the MERGED data: a policy supplied via
        MEMTOMEM_STM_PROXY__* env override is an explicit operator choice,
        not an accidental reliance on the default."""
        import logging

        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"enabled": True}))
        overrides = {"cache": {"tool_annotation_policy": "strict"}}
        with caplog.at_level(logging.WARNING):
            result = ProxyConfig.load_from_file_with_status(cfg_file, overrides)
        assert result.config is not None
        assert result.config.cache.tool_annotation_policy == "strict"
        assert not [r for r in caplog.records if "tool_annotation_policy" in r.getMessage()]

    def test_cache_disabled_suppresses_missing_policy_warning(self, tmp_path, caplog):
        """With the cache off, conservative vs strict changes nothing (the
        timeout-retry gate treats them identically) — no advisory noise."""
        import logging

        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"enabled": True, "cache": {"enabled": False}}))
        with caplog.at_level(logging.WARNING):
            result = ProxyConfig.load_from_file_with_status(cfg_file)
        assert result.config is not None
        assert not [r for r in caplog.records if "tool_annotation_policy" in r.getMessage()]

    def test_log_warnings_false_suppresses_cache_policy_warning(self, tmp_path, caplog):
        import logging

        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"enabled": True}))
        with caplog.at_level(logging.WARNING):
            result = ProxyConfig.load_from_file_with_status(cfg_file, log_warnings=False)
        assert result.config is not None
        assert not [r for r in caplog.records if "tool_annotation_policy" in r.getMessage()]

    def test_missing_file_no_cache_policy_warning(self, tmp_path, caplog):
        """Nothing to migrate: the defaults path and the env-only path never
        fire the advisory (branch placement, pinned here against refactors)."""
        import logging

        missing = tmp_path / "nonexistent.json"
        with caplog.at_level(logging.WARNING):
            ProxyConfig.load_from_file_with_status(missing)
            ProxyConfig.load_from_file_with_status(missing, {"enabled": "true"})
        assert not [r for r in caplog.records if "tool_annotation_policy" in r.getMessage()]

    def test_load_from_file_delegate_contract_unchanged(self, tmp_path):
        good = tmp_path / "good.json"
        good.write_text(json.dumps({"enabled": True}))
        broken = tmp_path / "broken.json"
        broken.write_text("{ nope")
        missing = tmp_path / "missing.json"

        assert ProxyConfig.load_from_file(good).enabled is True
        assert ProxyConfig.load_from_file(broken) is None
        assert ProxyConfig.load_from_file(missing).enabled is False  # defaults
        assert ProxyConfig.load_from_file(missing, missing_ok=False) is None


# ── MetricsStore persistence ─────────────────────────────────────────────


class TestMetricsStore:
    def test_record_and_query(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        store.record(CallMetrics(server="gh", tool="list", original_chars=1000, compressed_chars=500))
        store.record(CallMetrics(server="gh", tool="search", original_chars=2000, compressed_chars=800))

        history = store.get_history(limit=10)
        assert len(history) == 2
        assert history[0]["tool"] == "search"  # newest first
        store.close()

    def test_trim_excess(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db", max_history=5)
        store.initialize()
        for i in range(10):
            store.record(CallMetrics(server="s", tool=f"t{i}", original_chars=100, compressed_chars=50))

        history = store.get_history(limit=100)
        assert len(history) == 5  # trimmed to max_history
        store.close()

    def test_empty_history(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        assert store.get_history() == []
        store.close()


# ── TokenTracker aggregation ─────────────────────────────────────────────


class TestTokenTracker:
    def test_basic_recording(self):
        tracker = TokenTracker()
        tracker.record(CallMetrics(server="gh", tool="list", original_chars=1000, compressed_chars=500))
        tracker.record(CallMetrics(server="gh", tool="search", original_chars=2000, compressed_chars=800))

        summary = tracker.get_summary()
        assert summary["total_calls"] == 2
        assert summary["total_original_chars"] == 3000
        assert summary["total_compressed_chars"] == 1300
        assert summary["total_savings_pct"] > 50

    def test_by_server_aggregation(self):
        tracker = TokenTracker()
        tracker.record(CallMetrics(server="gh", tool="t1", original_chars=100, compressed_chars=50))
        tracker.record(CallMetrics(server="fs", tool="t2", original_chars=200, compressed_chars=100))

        summary = tracker.get_summary()
        assert "gh" in summary["by_server"]
        assert "fs" in summary["by_server"]
        assert summary["by_server"]["gh"]["calls"] == 1

    def test_cache_counters(self):
        tracker = TokenTracker()
        tracker.record_cache_hit()
        tracker.record_cache_hit()
        tracker.record_cache_miss()

        summary = tracker.get_summary()
        assert summary["cache_hits"] == 2
        assert summary["cache_misses"] == 1

    def test_empty_tracker(self):
        tracker = TokenTracker()
        summary = tracker.get_summary()
        assert summary["total_calls"] == 0
        assert summary["total_savings_pct"] == 0.0

    def test_with_persistent_store(self, tmp_path):
        store = MetricsStore(tmp_path / "metrics.db")
        store.initialize()
        tracker = TokenTracker(metrics_store=store)
        tracker.record(CallMetrics(server="gh", tool="list", original_chars=100, compressed_chars=50))

        # Check both in-memory and persistent
        assert tracker.get_summary()["total_calls"] == 1
        assert len(store.get_history()) == 1
        store.close()


# ── Privacy detection ────────────────────────────────────────────────────


class TestPrivacyDetection:
    def test_api_key_detected(self):
        assert contains_sensitive_content("api_key=sk-1234567890abcdefghijklmn")

    def test_password_detected(self):
        assert contains_sensitive_content("password: hunter2")

    def test_email_detected(self):
        assert contains_sensitive_content("Contact: user@example.com for info")

    def test_github_token_detected(self):
        assert contains_sensitive_content("token: ghp_" + "a" * 36)

    def test_private_key_detected(self):
        assert contains_sensitive_content("-----BEGIN RSA PRIVATE KEY-----")

    def test_clean_content_not_flagged(self):
        assert not contains_sensitive_content("This is a normal markdown document about APIs.")


# ── Provider-aware embedding_base_url default (regression for #54) ──────


class TestEmbeddingBaseUrlDefault:
    def test_ollama_default_url(self):
        """Unset embedding_base_url with ollama provider resolves to Ollama's endpoint."""
        cfg = RelevanceScorerConfig(scorer="embedding", embedding_provider="ollama")
        assert cfg.embedding_base_url == "http://localhost:11434"

    def test_openai_default_url(self):
        """Unset embedding_base_url with openai provider resolves to OpenAI's endpoint,
        not Ollama's localhost:11434 (the original bug)."""
        cfg = RelevanceScorerConfig(scorer="embedding", embedding_provider="openai")
        assert cfg.embedding_base_url == "https://api.openai.com"

    def test_explicit_url_preserved_for_openai(self):
        """Explicit base_url must override the provider default."""
        cfg = RelevanceScorerConfig(
            scorer="embedding",
            embedding_provider="openai",
            embedding_base_url="http://custom-proxy:8080",
        )
        assert cfg.embedding_base_url == "http://custom-proxy:8080"

    def test_explicit_url_preserved_for_ollama(self):
        cfg = RelevanceScorerConfig(
            scorer="embedding",
            embedding_provider="ollama",
            embedding_base_url="http://remote-ollama:11434",
        )
        assert cfg.embedding_base_url == "http://remote-ollama:11434"

    def test_unknown_provider_falls_back_to_ollama_default(self):
        """Unknown provider keeps the historical fallback, not None."""
        cfg = RelevanceScorerConfig(scorer="embedding", embedding_provider="custom-xyz")
        assert cfg.embedding_base_url == "http://localhost:11434"

    def test_defaults_preserved_on_bm25_scorer(self):
        """Provider default still applies even when scorer=bm25 (embedding fields unused)."""
        cfg = RelevanceScorerConfig(scorer="bm25", embedding_provider="openai")
        assert cfg.embedding_base_url == "https://api.openai.com"

    def test_empty_string_not_flagged(self):
        assert not contains_sensitive_content("")
