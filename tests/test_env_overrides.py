"""Tests for env-var > file > defaults precedence in ProxyConfig loading.

Locks in the fix for #106: env-set ``MEMTOMEM_STM_PROXY__*`` fields must
win over file values both at startup and on hot-reload.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from memtomem_stm.proxy.config import (
    ProxyConfig,
    ProxyConfigLoader,
    _deep_merge,
    collect_proxy_env_overrides,
)


class TestCollectProxyEnvOverrides:
    def test_top_level_field(self) -> None:
        env = {"MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS": "9999"}
        assert collect_proxy_env_overrides(env) == {"default_max_result_chars": "9999"}

    def test_nested_field_via_double_underscore(self) -> None:
        env = {"MEMTOMEM_STM_PROXY__CACHE__ENABLED": "true"}
        assert collect_proxy_env_overrides(env) == {"cache": {"enabled": "true"}}

    def test_deeply_nested_field(self) -> None:
        env = {
            "MEMTOMEM_STM_PROXY__RELEVANCE_SCORER__EMBEDDING_PROVIDER": "openai",
            "MEMTOMEM_STM_PROXY__RELEVANCE_SCORER__EMBEDDING_MODEL": "text-embedding-3-small",
        }
        assert collect_proxy_env_overrides(env) == {
            "relevance_scorer": {
                "embedding_provider": "openai",
                "embedding_model": "text-embedding-3-small",
            }
        }

    def test_unrelated_env_vars_ignored(self) -> None:
        env = {
            "PATH": "/usr/bin",
            "MEMTOMEM_STM_SURFACING__ENABLED": "true",  # surfacing prefix, not proxy
            "MEMTOMEM_STM_PROXY__ENABLED": "true",
        }
        assert collect_proxy_env_overrides(env) == {"enabled": "true"}

    def test_empty_when_no_proxy_env(self) -> None:
        assert collect_proxy_env_overrides({"FOO": "bar"}) == {}


class TestDeepMerge:
    def test_overrides_replace_scalars(self) -> None:
        assert _deep_merge({"a": 1, "b": 2}, {"a": 9}) == {"a": 9, "b": 2}

    def test_nested_dicts_merge_recursively(self) -> None:
        base = {"cache": {"enabled": False, "ttl": 60}}
        env = {"cache": {"enabled": True}}
        assert _deep_merge(base, env) == {"cache": {"enabled": True, "ttl": 60}}

    def test_overrides_replace_dict_with_scalar(self) -> None:
        assert _deep_merge({"x": {"a": 1}}, {"x": "scalar"}) == {"x": "scalar"}


class TestLoadFromFileWithEnvOverrides:
    def test_env_wins_when_field_in_both(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 16000}))

        cfg = ProxyConfig.load_from_file(
            cfg_file, env_overrides={"default_max_result_chars": "9999"}
        )

        assert cfg is not None
        assert cfg.default_max_result_chars == 9999

    def test_file_value_kept_when_no_env_override(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 16000}))

        cfg = ProxyConfig.load_from_file(cfg_file, env_overrides={})

        assert cfg is not None
        assert cfg.default_max_result_chars == 16000

    def test_env_overrides_when_file_missing(self, tmp_path: Path) -> None:
        cfg = ProxyConfig.load_from_file(
            tmp_path / "nonexistent.json",
            env_overrides={"default_max_result_chars": "7777"},
        )

        assert cfg is not None
        assert cfg.default_max_result_chars == 7777

    def test_missing_file_returns_none_when_missing_ok_false(self, tmp_path: Path) -> None:
        """``missing_ok=False`` lets a caller that already holds a better
        env-aware config (pydantic-settings parse) decline the swap in one
        atomic call — a missing file must NOT be rebuilt from the raw-string
        overlay, which can't represent JSON-encoded complex env values."""
        cfg = ProxyConfig.load_from_file(
            tmp_path / "nonexistent.json",
            env_overrides={"default_max_result_chars": "7777"},
            missing_ok=False,
        )

        assert cfg is None


class TestMalformedEnvOverrideDiagnostics:
    """A malformed MEMTOMEM_STM_PROXY__* value still collapses the load to
    defaults / None (the fallback semantics are deliberately unchanged), but
    the warning must name the env var — otherwise the operator debugs the
    healthy FILE while the env overlay is what broke the merged validation."""

    def test_malformed_env_value_named_in_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 9000}))
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS": "abc"}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None  # fallback unchanged: the whole load still degrades
        assert any(
            "MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS" in r.getMessage() for r in caplog.records
        )

    def test_nested_malformed_env_value_named_in_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"cache": {"enabled": True}}))
        overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES": "-5"})

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert any(
            "MEMTOMEM_STM_PROXY__CACHE__MAX_ENTRIES" in r.getMessage() for r in caplog.records
        )

    def test_file_caused_error_carries_no_env_hint(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A validation error caused by the FILE (no env override at that
        location) must not implicate env vars."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": "abc"}))
        overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__CACHE__ENABLED": "true"})

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert not any("implicated" in r.getMessage() for r in caplog.records)

    def test_model_validator_error_names_env_leaves_not_the_model_prefix(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A cross-field model validator reports its error at the MODEL's
        path (upstream_servers.gh.hybrid), not the field. The hint must name
        the env LEAF actually set under that subtree, never synthesize a
        non-leaf var (MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID) that
        was never in the environment."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(
            json.dumps(
                {"upstream_servers": {"gh": {"prefix": "gh", "hybrid": {"head_chars": 5000}}}}
            )
        )
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__MIN_HEAD_CHARS": "9000"}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        messages = [r.getMessage() for r in caplog.records]
        leaf = "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__HYBRID__MIN_HEAD_CHARS"
        assert any(leaf in m for m in messages)
        assert not any("HYBRID," in m or m.rstrip(")").endswith("HYBRID") for m in messages)

    def test_env_leaf_replacing_a_container_names_that_leaf(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An env string clobbering a whole sub-model (cache='oops') is named
        as the leaf the operator set, even though the error location may sit
        at or below the container."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"cache": {"enabled": True}}))
        overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__CACHE": "oops"})

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert any("MEMTOMEM_STM_PROXY__CACHE" in r.getMessage() for r in caplog.records)

    def test_env_untouched_error_path_names_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An error in an upstream entry the env never touched must not be
        attributed to env overrides set on a SIBLING entry."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "good": {"prefix": "good"},
                        "bad": {"prefix": "bad", "hybrid": {"head_chars": -1}},
                    }
                }
            )
        )
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GOOD__MAX_RESULT_CHARS": "5000"}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert not any("implicated" in r.getMessage() for r in caplog.records)

    def test_missing_field_in_env_created_entry_names_env_leaves(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An env var can CREATE an upstream entry that then fails on a
        missing required field (prefix). The error loc points at the absent
        field — outside the env subtree — but the collapse is env-caused:
        the entry exists only because the env built it, so the env leaves
        under it must be named."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 9000}))
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__MAX_RESULT_CHARS": "5000"}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert any(
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__MAX_RESULT_CHARS" in r.getMessage()
            for r in caplog.records
        )

    def test_missing_field_in_file_entry_carries_no_env_hint(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The provenance counterpart: when the FILE supplies the entry that
        is missing its required field, an env var that merely touched the
        same entry is innocent and must not be implicated."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"upstream_servers": {"gh": {"command": "gh-mcp"}}}))
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__GH__MAX_RESULT_CHARS": "5000"}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert not any("implicated" in r.getMessage() for r in caplog.records)

    def test_env_caused_duplicate_prefix_names_env_leaves(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Top-level model validators (duplicate upstream prefixes) report at
        loc=() — no path to walk. When the file alone validates and only the
        merged config trips the root check, the env overlay flipped it and
        its leaves must be named."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"upstream_servers": {"a": {"prefix": "a"}}}))
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__B__PREFIX": "a"}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert any(
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__B__PREFIX" in r.getMessage()
            for r in caplog.records
        )

    def test_env_caused_whitespace_prefix_names_env_leaves(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 9000}))
        overrides = collect_proxy_env_overrides(
            {"MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__B__PREFIX": "   "}
        )

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert any(
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__B__PREFIX" in r.getMessage()
            for r in caplog.records
        )

    def test_file_caused_root_validator_error_carries_no_env_hint(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When the FILE alone already fails the root validator (its own
        duplicate prefixes), an innocent env var must not be implicated."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(
            json.dumps({"upstream_servers": {"a": {"prefix": "x"}, "b": {"prefix": "x"}}})
        )
        overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__CACHE__ENABLED": "true"})

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert not any("implicated" in r.getMessage() for r in caplog.records)

    def test_non_dict_config_root_rejected_even_with_env_overrides(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A ``[]`` config root must fail the load even when env overrides
        are present: ``dict([])`` is ``{}``, so the deep merge used to turn
        the invalid file into a valid env-only config, silently dropping the
        fact that the file is broken."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text("[]")
        overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__ENABLED": "true"})

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(cfg_file, env_overrides=overrides)

        assert cfg is None
        assert any("JSON object" in r.getMessage() for r in caplog.records)

    def test_env_only_path_names_the_env_var(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """File missing: the env-only rebuild degrades to defaults and the
        warning names the malformed var."""
        overrides = collect_proxy_env_overrides({"MEMTOMEM_STM_PROXY__LOCK_TIMEOUT_SECONDS": "x"})

        with caplog.at_level(logging.WARNING):
            cfg = ProxyConfig.load_from_file(tmp_path / "nonexistent.json", overrides)

        assert cfg is not None  # env-only path degrades to defaults, not None
        assert cfg.lock_timeout_seconds == 30.0
        assert any(
            "MEMTOMEM_STM_PROXY__LOCK_TIMEOUT_SECONDS" in r.getMessage() for r in caplog.records
        )

    def test_nested_env_override_merges_with_file(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"cache": {"enabled": True, "default_ttl_seconds": 3600.0}}))

        cfg = ProxyConfig.load_from_file(
            cfg_file, env_overrides={"cache": {"default_ttl_seconds": "60"}}
        )

        assert cfg is not None
        assert cfg.cache.enabled is True  # from file
        assert cfg.cache.default_ttl_seconds == 60.0  # env override


class TestProxyConfigLoaderRespectsEnvOnReload:
    def test_env_overrides_survive_hot_reload(self, tmp_path: Path) -> None:
        """The original bug: hot-reload of stm_proxy.json discards env overrides."""
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 16000}))

        loader = ProxyConfigLoader(cfg_file, env_overrides={"default_max_result_chars": "9999"})
        first = loader.get()
        assert first.default_max_result_chars == 9999

        # Edit file with a new (file-only) value; env override must still win.
        time.sleep(0.01)  # ensure mtime ticks
        cfg_file.write_text(json.dumps({"default_max_result_chars": 32000}))

        # Force mtime detection
        loader._mtime = -1.0  # noqa: SLF001 — direct test of reload behaviour
        second = loader.get()
        assert second.default_max_result_chars == 9999  # env override held

    def test_env_override_only_used_when_field_in_env(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 16000}))

        loader = ProxyConfigLoader(cfg_file, env_overrides={})
        cfg = loader.get()

        assert cfg.default_max_result_chars == 16000

    def test_loader_without_env_overrides_uses_pure_file(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "stm_proxy.json"
        cfg_file.write_text(json.dumps({"default_max_result_chars": 12345}))

        loader = ProxyConfigLoader(cfg_file)  # no env_overrides arg
        assert loader.get().default_max_result_chars == 12345


def test_collect_uses_real_environ_when_arg_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__DEFAULT_MAX_RESULT_CHARS", "4242")
    monkeypatch.delenv("MEMTOMEM_STM_PROXY__ENABLED", raising=False)
    out = collect_proxy_env_overrides()
    assert out.get("default_max_result_chars") == "4242"
