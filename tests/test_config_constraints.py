"""Tests that unsafe config values are rejected by pydantic validators."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from memtomem_stm.config import LangfuseConfig, STMConfig
from memtomem_stm.proxy.config import (
    AutoIndexConfig,
    ExtractionConfig,
    HybridConfig,
    LLMCompressorConfig,
    LLMProvider,
    OriginSource,
    ProxyConfig,
    RelevanceScorerConfig,
    SelectiveConfig,
    UpstreamOrigin,
    UpstreamServerConfig,
)
from memtomem_stm.surfacing.config import SurfacingConfig


class TestProxyNumericConstraints:
    def test_llm_compressor_rejects_nonpositive_max_tokens(self) -> None:
        with pytest.raises(ValidationError):
            LLMCompressorConfig(max_tokens=0)
        with pytest.raises(ValidationError):
            LLMCompressorConfig(max_tokens=-10)

    def test_selective_json_depth_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            SelectiveConfig(json_depth=0)
        with pytest.raises(ValidationError):
            SelectiveConfig(json_depth=-1)
        SelectiveConfig(json_depth=1)  # minimum valid

    def test_selective_min_section_chars_nonnegative(self) -> None:
        with pytest.raises(ValidationError):
            SelectiveConfig(min_section_chars=-1)
        SelectiveConfig(min_section_chars=0)  # zero allowed (passthrough)

    def test_selective_pending_store_literal_rejects_typo(self) -> None:
        with pytest.raises(ValidationError):
            SelectiveConfig(pending_store="memry")  # type: ignore[arg-type]
        SelectiveConfig(pending_store="memory")
        SelectiveConfig(pending_store="sqlite")

    def test_extraction_rejects_invalid_ranges(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionConfig(max_facts=0)
        with pytest.raises(ValidationError):
            ExtractionConfig(min_response_chars=-1)
        with pytest.raises(ValidationError):
            ExtractionConfig(dedup_threshold=1.5)
        with pytest.raises(ValidationError):
            ExtractionConfig(dedup_threshold=-0.1)
        with pytest.raises(ValidationError):
            ExtractionConfig(max_input_chars=0)

    def test_auto_index_min_chars_nonnegative(self) -> None:
        with pytest.raises(ValidationError):
            AutoIndexConfig(min_chars=-100)
        AutoIndexConfig(min_chars=0)  # zero = index everything

    def test_relevance_scorer_embedding_timeout_positive(self) -> None:
        with pytest.raises(ValidationError):
            RelevanceScorerConfig(embedding_timeout=0.0)
        with pytest.raises(ValidationError):
            RelevanceScorerConfig(embedding_timeout=-1.0)

    def test_reconnect_delay_must_not_exceed_max(self) -> None:
        with pytest.raises(ValidationError):
            UpstreamServerConfig(
                prefix="x", reconnect_delay_seconds=10, max_reconnect_delay_seconds=5
            )

    def test_reconnect_delay_equal_to_max_is_valid(self) -> None:
        cfg = UpstreamServerConfig(
            prefix="x", reconnect_delay_seconds=5, max_reconnect_delay_seconds=5
        )
        assert cfg.reconnect_delay_seconds == 5

    def test_hybrid_min_head_must_not_exceed_head(self) -> None:
        """min_head_chars > head_chars makes HybridCompressor's head-budget
        guard fall back to truncation on EVERY call — the operator's chosen
        hybrid strategy silently never runs. Rejected at load instead."""
        with pytest.raises(ValidationError, match="min_head_chars"):
            HybridConfig(head_chars=5000, min_head_chars=9000)

    def test_hybrid_min_head_equal_to_head_is_valid(self) -> None:
        cfg = HybridConfig(head_chars=500, min_head_chars=500)
        assert cfg.min_head_chars == cfg.head_chars

    def test_hybrid_defaults_are_valid(self) -> None:
        cfg = HybridConfig()
        assert cfg.min_head_chars <= cfg.head_chars


class TestUpstreamOrigin:
    """`origin` import-provenance block (#475) — schema documented server-side.

    The proxy runtime never reads `origin`; these pins exist so the shape the
    CLI import paths write keeps validating here (the CLI constructs it via
    this model) and so compat with configs written by newer/older versions
    cannot silently regress.
    """

    _FULL_ORIGIN = {
        "schema_version": 1,
        "source": {"kind": "claude-user", "pruned": False},
        "duplicates": [
            {"kind": "claude-desktop", "pruned": False},
            {"kind": "mcp-json", "path": "/proj/.mcp.json", "pruned": False},
        ],
        "imported_at": "2026-06-11T05:00:00Z",
        "original": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_TOKEN": "ghp_x"},
        },
    }

    def test_full_origin_block_validates_on_upstream_entry(self) -> None:
        cfg = UpstreamServerConfig(prefix="gh", command="npx", origin=self._FULL_ORIGIN)
        assert cfg.origin is not None
        assert cfg.origin.source.kind == "claude-user"
        assert cfg.origin.source.pruned is False
        assert [d.kind for d in cfg.origin.duplicates] == ["claude-desktop", "mcp-json"]
        assert cfg.origin.duplicates[1].path == "/proj/.mcp.json"
        # Verbatim original survives validation untouched — it is what
        # `mms eject` restores.
        assert cfg.origin.original == self._FULL_ORIGIN["original"]

    def test_origin_defaults(self) -> None:
        origin = UpstreamOrigin(source=OriginSource(kind="claude-user"))
        assert origin.schema_version == 1
        assert origin.duplicates == []
        assert origin.source.pruned is False
        assert origin.source.pruned_at is None
        assert origin.original is None

    def test_unknown_origin_keys_ignored_for_forward_compat(self) -> None:
        """A config written by a newer CLI (schema_version bump adding keys,
        or a new source kind) must still load in this server — pydantic's
        default ``extra="ignore"`` is the compat mechanism (#475)."""
        block = {
            "schema_version": 2,
            "source": {"kind": "some-future-host", "pruned": False, "new_field": 1},
            "future_top_level": {"x": 1},
        }
        cfg = UpstreamServerConfig(prefix="gh", command="npx", origin=block)
        assert cfg.origin is not None
        assert cfg.origin.schema_version == 2
        assert cfg.origin.source.kind == "some-future-host"

    def test_entry_without_origin_still_valid(self) -> None:
        cfg = UpstreamServerConfig(prefix="gh", command="npx")
        assert cfg.origin is None


class TestSurfacingLtmTransportConfig:
    def test_stdio_transport_keeps_existing_defaults(self) -> None:
        cfg = SurfacingConfig()

        assert cfg.ltm_mcp_transport == "stdio"
        assert cfg.ltm_mcp_command == "memtomem-server"
        assert cfg.ltm_mcp_url == ""
        assert cfg.ltm_mcp_headers is None

    def test_network_transport_requires_url(self) -> None:
        with pytest.raises(ValidationError, match="ltm_mcp_url is required"):
            SurfacingConfig(ltm_mcp_transport="sse")

        with pytest.raises(ValidationError, match="ltm_mcp_url is required"):
            SurfacingConfig(ltm_mcp_transport="streamable_http")

    def test_network_transport_accepts_url_and_headers(self) -> None:
        cfg = SurfacingConfig(
            ltm_mcp_transport="streamable_http",
            ltm_mcp_url="https://ltm.example/mcp",
            ltm_mcp_headers={"Authorization": "Bearer token"},
        )

        assert cfg.ltm_mcp_url == "https://ltm.example/mcp"
        assert cfg.ltm_mcp_headers == {"Authorization": "Bearer token"}


class TestLLMCompressorApiKey:
    """``provider=openai|anthropic`` with empty ``api_key`` used to send a
    malformed ``Bearer `` header and silently fall back to truncate; validate
    at config-load time instead so misconfiguration is loud."""

    def test_openai_empty_api_key_rejected_when_env_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
            LLMCompressorConfig(provider=LLMProvider.OPENAI)

    def test_anthropic_empty_api_key_rejected_when_env_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
            LLMCompressorConfig(provider=LLMProvider.ANTHROPIC)

    def test_openai_env_fallback_populates_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        cfg = LLMCompressorConfig(provider=LLMProvider.OPENAI)
        assert cfg.api_key == "sk-from-env"

    def test_anthropic_env_fallback_populates_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-from-env")
        cfg = LLMCompressorConfig(provider=LLMProvider.ANTHROPIC)
        assert cfg.api_key == "ant-from-env"

    def test_explicit_api_key_bypasses_env_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = LLMCompressorConfig(provider=LLMProvider.OPENAI, api_key="sk-explicit")
        assert cfg.api_key == "sk-explicit"

    def test_ollama_does_not_require_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg = LLMCompressorConfig(provider=LLMProvider.OLLAMA)
        assert cfg.api_key == ""

    def test_whitespace_only_env_treated_as_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "   ")
        with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
            LLMCompressorConfig(provider=LLMProvider.OPENAI)


class TestSurfacingNumericConstraints:
    def test_surfacing_min_score_range(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(min_score=-0.01)
        with pytest.raises(ValidationError):
            SurfacingConfig(min_score=1.5)

    def test_surfacing_timeouts_positive(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(timeout_seconds=0.0)
        with pytest.raises(ValidationError):
            SurfacingConfig(timeout_seconds=-1.0)
        with pytest.raises(ValidationError):
            SurfacingConfig(circuit_reset_seconds=0.0)

    def test_surfacing_cooldown_nonnegative(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(cooldown_seconds=-1.0)
        SurfacingConfig(cooldown_seconds=0.0)  # 0 disables cooldown

    def test_surfacing_auto_tune_increment_positive(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(auto_tune_score_increment=0.0)
        with pytest.raises(ValidationError):
            SurfacingConfig(auto_tune_score_increment=-0.01)

    def test_surfacing_counts_positive(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(max_results=0)
        with pytest.raises(ValidationError):
            SurfacingConfig(max_surfacings_per_minute=0)
        with pytest.raises(ValidationError):
            SurfacingConfig(max_injection_chars=0)
        with pytest.raises(ValidationError):
            SurfacingConfig(min_query_tokens=0)
        with pytest.raises(ValidationError):
            SurfacingConfig(feedback_demotion_negative_threshold=0)

    def test_surfacing_context_window_nonnegative(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(context_window_size=-1)
        SurfacingConfig(context_window_size=0)  # 0 disables

    def test_surfacing_injection_mode_literal(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(injection_mode="postpend")  # type: ignore[arg-type]
        for mode in ("prepend", "append", "section"):
            SurfacingConfig(injection_mode=mode)  # type: ignore[arg-type]

    def test_surfacing_injection_mode_default_is_append(self) -> None:
        """#348: default is ``append`` so the progressive-delivery path
        surfaces by default. ``prepend`` short-circuits in progressive mode
        because it would shift ``stm_proxy_read_more`` character offsets."""
        assert SurfacingConfig().injection_mode == "append"

    def test_surfacing_result_format_literal(self) -> None:
        with pytest.raises(ValidationError):
            SurfacingConfig(result_format="json")  # type: ignore[arg-type]
        SurfacingConfig(result_format="compact")
        SurfacingConfig(result_format="structured")


class TestLangfuseInterdepValidator:
    def test_enabled_requires_both_keys(self) -> None:
        with pytest.raises(ValidationError, match="public_key and secret_key"):
            LangfuseConfig(enabled=True)
        with pytest.raises(ValidationError, match="public_key and secret_key"):
            LangfuseConfig(enabled=True, public_key="pk-lf-x")
        with pytest.raises(ValidationError, match="public_key and secret_key"):
            LangfuseConfig(enabled=True, secret_key="sk-lf-x")

    def test_enabled_with_both_keys_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Stub out the package-availability validator — that axis is covered
        # separately by ``TestLangfusePackageValidator``.
        import importlib.util as importlib_util

        real_find_spec = importlib_util.find_spec

        def _fake_find_spec(name: str, package: str | None = None) -> object | None:
            return object() if name == "langfuse" else real_find_spec(name, package)

        monkeypatch.setattr("importlib.util.find_spec", _fake_find_spec)
        cfg = LangfuseConfig(enabled=True, public_key="pk-lf-x", secret_key="sk-lf-x")
        assert cfg.enabled is True

    def test_disabled_allows_empty_keys(self) -> None:
        cfg = LangfuseConfig(enabled=False)
        assert cfg.public_key == ""


class TestLangfusePackageValidator:
    """``LangfuseConfig.enabled=true`` must fail-fast when the langfuse extra is absent."""

    def test_enabled_without_langfuse_package_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.util as importlib_util

        def _fake_find_spec(name: str, package: str | None = None) -> object | None:
            return None if name == "langfuse" else importlib_util.find_spec(name, package)

        monkeypatch.setattr("importlib.util.find_spec", _fake_find_spec)
        with pytest.raises(ValidationError, match="'langfuse' package is not installed"):
            LangfuseConfig(enabled=True, public_key="pk-lf-x", secret_key="sk-lf-x")

    def test_enabled_with_langfuse_package_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The validator only probes ``find_spec``; any non-None stand-in is
        # sufficient. We patch rather than relying on the dev env because
        # the langfuse extra is not included in ``uv sync`` by default.
        import importlib.util as importlib_util

        real_find_spec = importlib_util.find_spec

        def _fake_find_spec(name: str, package: str | None = None) -> object | None:
            return object() if name == "langfuse" else real_find_spec(name, package)

        monkeypatch.setattr("importlib.util.find_spec", _fake_find_spec)
        cfg = LangfuseConfig(enabled=True, public_key="pk-lf-x", secret_key="sk-lf-x")
        assert cfg.enabled is True

    def test_disabled_skips_package_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def _tracking_find_spec(name: str, package: str | None = None) -> object | None:
            calls.append(name)
            return None

        monkeypatch.setattr("importlib.util.find_spec", _tracking_find_spec)
        cfg = LangfuseConfig(enabled=False)
        assert cfg.enabled is False
        assert "langfuse" not in calls


class TestProxyConfigUniquePrefixes:
    """Two upstreams sharing a prefix used to load fine and silently drop the
    second upstream's colliding tools at ``ProxyManager.start()``. Reject at
    config-load time so the typo surfaces immediately."""

    def test_duplicate_prefix_two_upstreams_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # LLMCompressorConfig has its own env-driven validator; isolate.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValidationError) as exc_info:
            ProxyConfig(
                upstream_servers={
                    "serverA": UpstreamServerConfig(prefix="mcp1", command="a"),
                    "serverB": UpstreamServerConfig(prefix="mcp1", command="b"),
                }
            )
        msg = str(exc_info.value)
        # Both upstream keys AND the colliding prefix value must appear so the
        # user can locate the typo without re-reading the config.
        assert "mcp1" in msg
        assert "serverA" in msg
        assert "serverB" in msg

    def test_three_way_collision_lists_all_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValidationError) as exc_info:
            ProxyConfig(
                upstream_servers={
                    "a": UpstreamServerConfig(prefix="dup", command="x"),
                    "b": UpstreamServerConfig(prefix="dup", command="y"),
                    "c": UpstreamServerConfig(prefix="dup", command="z"),
                }
            )
        msg = str(exc_info.value)
        # All three upstream keys reported in one error so the user fixes
        # them in a single round-trip rather than chasing them one at a time.
        for key in ("a", "b", "c"):
            assert f"'{key}'" in msg

    def test_unique_prefixes_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg = ProxyConfig(
            upstream_servers={
                "gh": UpstreamServerConfig(prefix="gh", command="gh-server"),
                "fs": UpstreamServerConfig(prefix="fs", command="fs-server"),
                "slack": UpstreamServerConfig(prefix="slack", command="slack-server"),
            }
        )
        assert set(cfg.upstream_servers) == {"gh", "fs", "slack"}

    def test_single_upstream_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg = ProxyConfig(
            upstream_servers={"only": UpstreamServerConfig(prefix="only", command="x")}
        )
        assert cfg.upstream_servers["only"].prefix == "only"

    def test_two_collision_groups_reported_together(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValidationError) as exc_info:
            ProxyConfig(
                upstream_servers={
                    "a1": UpstreamServerConfig(prefix="alpha", command="x"),
                    "a2": UpstreamServerConfig(prefix="alpha", command="y"),
                    "b1": UpstreamServerConfig(prefix="beta", command="x"),
                    "b2": UpstreamServerConfig(prefix="beta", command="y"),
                }
            )
        msg = str(exc_info.value)
        # Independent collision groups must surface together so the user
        # doesn't need a second load to find the second typo.
        assert "alpha" in msg
        assert "beta" in msg


class TestProxyConfigNonemptyPrefix:
    """``UpstreamServerConfig.prefix`` is required but had no min-length, so
    an empty string used to validate fine and produce composed names like
    ``__list_items``. A single empty prefix also slipped past the
    uniqueness check (#265). Reject at config-load with the upstream key
    named so the user can locate the typo."""

    def test_empty_prefix_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValidationError) as exc_info:
            ProxyConfig(
                upstream_servers={
                    "blank": UpstreamServerConfig(prefix="", command="x"),
                }
            )
        msg = str(exc_info.value)
        assert "blank" in msg
        assert "Empty upstream prefix" in msg

    def test_whitespace_only_prefix_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Whitespace-only is the same typo class as empty — fail it the same way.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValidationError) as exc_info:
            ProxyConfig(
                upstream_servers={
                    "spaces": UpstreamServerConfig(prefix="   ", command="x"),
                }
            )
        assert "spaces" in str(exc_info.value)

    def test_multiple_empty_prefixes_listed_together(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValidationError) as exc_info:
            ProxyConfig(
                upstream_servers={
                    "a": UpstreamServerConfig(prefix="", command="x"),
                    "b": UpstreamServerConfig(prefix="", command="y"),
                    "ok": UpstreamServerConfig(prefix="real", command="z"),
                }
            )
        msg = str(exc_info.value)
        # Both offenders surface in one error so the user fixes them both
        # before the uniqueness validator (which would otherwise also fire
        # on the empty/empty pair) becomes the second-iteration noise.
        assert "'a'" in msg
        assert "'b'" in msg
        # The legitimate upstream is not falsely flagged.
        assert "'ok'" not in msg

    def test_two_empty_prefixes_report_empty_not_duplicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pins validator declaration order: _check_nonempty_upstream_prefixes
        # must run before _check_unique_upstream_prefixes. If the order
        # flipped (e.g. an alphabetical reorder, or a refactor that merges
        # the two), the user would see "Duplicate upstream prefixes
        # detected: prefix '' used by …" — technically true but the empty
        # error is the more actionable root cause.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ValidationError) as exc_info:
            ProxyConfig(
                upstream_servers={
                    "a": UpstreamServerConfig(prefix="", command="x"),
                    "b": UpstreamServerConfig(prefix="", command="y"),
                }
            )
        msg = str(exc_info.value)
        assert "Empty upstream prefix" in msg
        assert "Duplicate" not in msg


class TestLogLevel:
    def test_default_is_warning(self) -> None:
        cfg = STMConfig()
        assert cfg.log_level == "WARNING"

    def test_valid_levels_accepted(self) -> None:
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            cfg = STMConfig(log_level=level)
            assert cfg.log_level == level

    def test_invalid_level_rejected(self) -> None:
        with pytest.raises(ValidationError):
            STMConfig(log_level="TRACE")  # type: ignore[arg-type]

    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEMTOMEM_STM_LOG_LEVEL", "DEBUG")
        cfg = STMConfig()
        assert cfg.log_level == "DEBUG"
