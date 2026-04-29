"""Tests for token-aware budget configuration.

Covers ``proxy/token_estimate.py`` (codepoint approximation +
chars-per-token conversion) and the per-server / per-tool /
proxy-default ``chars_per_token`` and ``max_result_tokens`` resolution
paths in ``ProxyManager._resolve_tool_config``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    ProxyConfig,
    ToolOverrideConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.token_estimate import approx_tokens, tokens_to_chars


# ── token_estimate primitives ───────────────────────────────────────────


class TestApproxTokens:
    def test_empty_returns_zero(self):
        assert approx_tokens("") == 0

    def test_ascii_only(self):
        # 100 ASCII chars * 0.30 tok/char = 30 tokens
        assert approx_tokens("a" * 100) == 30

    def test_hangul_only(self):
        # 100 Hangul syllables * 1.25 tok/char = 125 tokens
        assert approx_tokens("가" * 100) == 125

    def test_cjk_ideograph_only(self):
        # 100 CJK ideographs * 1.40 tok/char = 140 tokens
        assert approx_tokens("漢" * 100) == 140

    def test_kana_only(self):
        # 100 Hiragana chars * 1.00 tok/char = 100 tokens
        assert approx_tokens("あ" * 100) == 100

    def test_mixed_korean_dominant(self):
        """Korean prose with English/punctuation produces token-heavy estimate."""
        text = "안녕하세요 STM proxy. " * 10  # ~200 chars, mix of Hangul + ASCII
        ascii_count = sum(1 for ch in text if ord(ch) < 0x80)
        hangul_count = sum(1 for ch in text if 0xAC00 <= ord(ch) <= 0xD7AF)
        expected = int(ascii_count * 0.30 + hangul_count * 1.25)
        assert approx_tokens(text) == expected

    def test_overestimate_safety_for_korean(self):
        """A short Korean response should already estimate >= cl100k_base reality.

        100 Hangul chars at cl100k_base typically tokenize to ~80-110 tokens.
        Our 1.25 coefficient over-estimates to 125 — the safe direction for
        gate decisions (compress slightly earlier rather than miss).
        """
        text = "한국어 텍스트는 영어보다 토큰을 더 많이 씁니다 " * 5
        # We don't import tiktoken in tests; just assert estimator runs and
        # the over-estimate property holds against a conservative lower bound.
        est = approx_tokens(text)
        assert est > len(text) * 0.5, "Korean estimate should be >0.5 tok/char"


class TestTokensToChars:
    def test_zero_or_negative_inputs(self):
        assert tokens_to_chars(0, 3.5) == 0
        assert tokens_to_chars(-100, 3.5) == 0
        assert tokens_to_chars(1000, 0.0) == 0
        assert tokens_to_chars(1000, -1.0) == 0

    def test_english_default_ratio(self):
        # 1500 tokens * 3.5 chars/tok = 5250 chars
        assert tokens_to_chars(1500, 3.5) == 5250

    def test_korean_calibrated_ratio(self):
        # 1500 tokens * 1.85 chars/tok = 2775 chars
        assert tokens_to_chars(1500, 1.85) == 2775


# ── ProxyConfig.chars_per_token + effective_max_result_chars ─────────────


class TestProxyConfigCharsPerToken:
    def test_default_is_3_5(self):
        cfg = ProxyConfig()
        assert cfg.chars_per_token == 3.5

    def test_validator_rejects_zero_or_negative(self):
        with pytest.raises(ValueError):
            ProxyConfig(chars_per_token=0)
        with pytest.raises(ValueError):
            ProxyConfig(chars_per_token=-1.0)

    def test_effective_max_result_chars_uses_configured_ratio(self):
        """Lowering ``chars_per_token`` shrinks the model-aware budget."""
        # Default English-biased: 200000 * 0.05 * 3.5 = 35000, capped at 16000.
        cfg_en = ProxyConfig(consumer_model="claude-sonnet-4", chars_per_token=3.5)
        assert cfg_en.effective_max_result_chars() == 16000  # cap dominates

        # Korean-tuned (lower ratio) shrinks pre-cap: 200000 * 0.05 * 1.85 = 18500,
        # capped at 16000. Still cap-dominated for Claude.
        cfg_ko = ProxyConfig(consumer_model="claude-sonnet-4", chars_per_token=1.85)
        assert cfg_ko.effective_max_result_chars() == 16000

        # Smaller model context exposes the ratio. Llama-4-scout has 512000 tokens.
        # context_budget_ratio default 0.05 → 25600 tokens budget.
        # At 3.5: 89600 chars, capped at default 16000.
        # At 1.85: 47360 chars, capped at default 16000.
        # Still cap-dominated. Drop default_max_result_chars to expose the ratio:
        cfg_small_en = ProxyConfig(
            consumer_model="claude-sonnet-4",
            chars_per_token=3.5,
            default_max_result_chars=50000,  # raise cap
        )
        cfg_small_ko = ProxyConfig(
            consumer_model="claude-sonnet-4",
            chars_per_token=1.85,
            default_max_result_chars=50000,
        )
        # Now ratio dominates: 200000 * 0.05 * 3.5 = 35000 vs 200000 * 0.05 * 1.85 = 18500.
        assert cfg_small_en.effective_max_result_chars() == 35000
        assert cfg_small_ko.effective_max_result_chars() == 18500

    def test_effective_max_result_chars_no_consumer_model(self):
        """Without ``consumer_model`` the chars_per_token field has no effect."""
        cfg = ProxyConfig(chars_per_token=1.0, default_max_result_chars=12345)
        assert cfg.effective_max_result_chars() == 12345


# ── _resolve_tool_config token-aware paths ───────────────────────────────


def _make_manager(proxy_cfg: ProxyConfig, server_cfg: UpstreamServerConfig) -> ProxyManager:
    mgr = ProxyManager(proxy_cfg, TokenTracker())
    mgr._connections["srv"] = UpstreamConnection(
        name="srv",
        config=server_cfg,
        session=AsyncMock(),
        tools=[],
    )
    return mgr


class TestServerLevelTokenBudget:
    def test_max_result_tokens_overrides_chars(self):
        """When server sets ``max_result_tokens``, it wins over ``max_result_chars``."""
        server_cfg = UpstreamServerConfig(
            prefix="kr",
            max_result_chars=8000,  # would normally drive max_chars
            max_result_tokens=1500,  # opt-in token budget
            chars_per_token=2.0,  # KO-tuned
        )
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg})
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc = mgr._resolve_tool_config("srv", "any_tool")
        # 1500 tokens * 2.0 chars/tok = 3000 chars
        assert tc.max_chars == 3000

    def test_chars_per_token_falls_back_to_proxy_default(self):
        """Server-level token budget without per-server ratio uses proxy default."""
        server_cfg = UpstreamServerConfig(
            prefix="x",
            max_result_tokens=1000,
            # chars_per_token NOT set on server
        )
        proxy_cfg = ProxyConfig(
            upstream_servers={"srv": server_cfg},
            chars_per_token=4.0,
        )
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc = mgr._resolve_tool_config("srv", "any_tool")
        # 1000 * 4.0 = 4000
        assert tc.max_chars == 4000

    def test_no_token_budget_keeps_existing_char_path(self):
        """Backward compat: without ``max_result_tokens``, behavior unchanged."""
        server_cfg = UpstreamServerConfig(prefix="x", max_result_chars=2500)
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg})
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc = mgr._resolve_tool_config("srv", "any_tool")
        assert tc.max_chars == 2500


class TestToolOverrideTokenBudget:
    def test_override_max_result_tokens_wins_over_server(self):
        server_cfg = UpstreamServerConfig(
            prefix="x",
            max_result_tokens=2000,  # server token budget
            chars_per_token=3.0,
            tool_overrides={
                "small_tool": ToolOverrideConfig(
                    max_result_tokens=500,  # tighter per-tool budget
                ),
            },
        )
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg})
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc_default = mgr._resolve_tool_config("srv", "other_tool")
        tc_small = mgr._resolve_tool_config("srv", "small_tool")
        # default tool: server 2000 * 3.0 = 6000
        assert tc_default.max_chars == 6000
        # overridden tool: 500 * 3.0 (server cpt inherited) = 1500
        assert tc_small.max_chars == 1500

    def test_override_chars_per_token_wins_for_token_conversion(self):
        """Tool-level ``chars_per_token`` overrides server's ratio for that tool only."""
        server_cfg = UpstreamServerConfig(
            prefix="x",
            chars_per_token=3.0,
            tool_overrides={
                "kr_tool": ToolOverrideConfig(
                    max_result_tokens=1000,
                    chars_per_token=1.85,  # KO-tuned for this tool
                ),
            },
        )
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg})
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc = mgr._resolve_tool_config("srv", "kr_tool")
        # tool override cpt 1.85 used (not server's 3.0)
        # 1000 * 1.85 = 1850
        assert tc.max_chars == 1850

    def test_override_token_takes_precedence_over_override_chars(self):
        """If both override fields are set, tokens wins (more specific intent)."""
        server_cfg = UpstreamServerConfig(
            prefix="x",
            tool_overrides={
                "tool": ToolOverrideConfig(
                    max_result_chars=99999,  # legacy
                    max_result_tokens=400,
                    chars_per_token=2.5,
                ),
            },
        )
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg})
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc = mgr._resolve_tool_config("srv", "tool")
        # token wins: 400 * 2.5 = 1000
        assert tc.max_chars == 1000

    def test_override_chars_alone_still_works(self):
        """Pure char override (existing pattern) is unchanged."""
        server_cfg = UpstreamServerConfig(
            prefix="x",
            tool_overrides={
                "tool": ToolOverrideConfig(max_result_chars=42),
            },
        )
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg})
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc = mgr._resolve_tool_config("srv", "tool")
        assert tc.max_chars == 42


class TestCharsPerTokenResolutionOrder:
    def test_full_cascade_tool_to_proxy(self):
        """tool.chars_per_token → server.chars_per_token → proxy.chars_per_token."""
        server_cfg = UpstreamServerConfig(
            prefix="x",
            max_result_tokens=1000,
            chars_per_token=2.0,  # server level
            tool_overrides={
                # this tool inherits server's 2.0
                "inherits": ToolOverrideConfig(max_result_tokens=1000),
                # this tool overrides to 1.5
                "overrides": ToolOverrideConfig(
                    max_result_tokens=1000,
                    chars_per_token=1.5,
                ),
            },
        )
        proxy_cfg = ProxyConfig(
            upstream_servers={"srv": server_cfg},
            chars_per_token=4.0,  # proxy level (unused if server set)
        )
        mgr = _make_manager(proxy_cfg, server_cfg)

        # default tool, server-level: 1000 * 2.0 = 2000
        assert mgr._resolve_tool_config("srv", "default").max_chars == 2000
        # tool inherits server cpt: 1000 * 2.0 = 2000
        assert mgr._resolve_tool_config("srv", "inherits").max_chars == 2000
        # tool overrides cpt: 1000 * 1.5 = 1500
        assert mgr._resolve_tool_config("srv", "overrides").max_chars == 1500

    def test_only_proxy_level_chars_per_token(self):
        """When neither server nor tool sets cpt, proxy default applies."""
        server_cfg = UpstreamServerConfig(
            prefix="x",
            max_result_tokens=2000,
            # no chars_per_token at server
        )
        proxy_cfg = ProxyConfig(
            upstream_servers={"srv": server_cfg},
            chars_per_token=2.5,
        )
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc = mgr._resolve_tool_config("srv", "any_tool")
        # 2000 * 2.5 = 5000
        assert tc.max_chars == 5000


class TestKoreanWorkloadEndToEnd:
    """Realistic Korean-workload configuration produces a tighter budget
    than the same operator would get on the default char path.

    This pins the user-visible behavior: a KO operator who switches from
    ``max_result_chars=8000`` to ``max_result_tokens=1500`` (with
    ``chars_per_token=1.85``) gets max_chars=2775 — roughly 35% of the
    char-path budget. This is the intent: KO content should compress
    earlier than EN content at the same token target.
    """

    def test_ko_operator_gets_tighter_budget(self):
        # Explicit non-default char budget so we hit the per-server char path
        # rather than ``effective_max_result_chars()``.
        en_explicit_cfg = UpstreamServerConfig(prefix="docs", max_result_chars=8500)
        ko_token_cfg = UpstreamServerConfig(
            prefix="docs",
            max_result_tokens=1500,
            chars_per_token=1.85,
            compression=CompressionStrategy.HYBRID,
        )
        en_proxy = ProxyConfig(upstream_servers={"srv": en_explicit_cfg})
        ko_proxy = ProxyConfig(upstream_servers={"srv": ko_token_cfg})

        en_mgr = _make_manager(en_proxy, en_explicit_cfg)
        ko_mgr = _make_manager(ko_proxy, ko_token_cfg)

        en_tc = en_mgr._resolve_tool_config("srv", "read_doc")
        ko_tc = ko_mgr._resolve_tool_config("srv", "read_doc")

        assert en_tc.max_chars == 8500
        # 1500 tokens * 1.85 chars/tok = 2775 chars (matches measurement-derived
        # KO calibration; ~33% of an EN char budget for the same token target).
        assert ko_tc.max_chars == 2775
        assert ko_tc.max_chars < en_tc.max_chars
