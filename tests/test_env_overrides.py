"""Tests for env-var > file > defaults precedence in ProxyConfig loading.

Locks in the fix for #106: env-set ``MEMTOMEM_STM_PROXY__*`` fields must
win over file values both at startup and on hot-reload.
"""

from __future__ import annotations

import json
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
