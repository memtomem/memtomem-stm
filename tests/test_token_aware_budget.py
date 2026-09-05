"""Tests for token-aware budget configuration.

Covers ``proxy/token_estimate.py`` (codepoint approximation +
chars-per-token conversion) and the per-server / per-tool /
proxy-default ``chars_per_token`` and ``max_result_tokens`` resolution
paths in ``ProxyManager._resolve_tool_config``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from memtomem_stm.proxy.config import (
    MODEL_CONTEXT_WINDOWS,
    CompressionStrategy,
    ProgressiveConfig,
    ProxyConfig,
    TokenEstimationMode,
    ToolOverrideConfig,
    UpstreamServerConfig,
    effective_max_result_chars,
)
from memtomem_stm.proxy.manager import ProxyManager, UpstreamConnection, compression_fingerprint
from memtomem_stm.proxy.metrics import TokenTracker
from memtomem_stm.proxy.token_estimate import MAX_CHAR_BUDGET, approx_tokens, tokens_to_chars


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


class TestTokensToCharsSaturation:
    """The conversion is total: no input reaches the caller as an exception (#977).

    The old behaviour differed per route, so each case below says which it
    was. Three raised ``OverflowError``, and those are the ones a config could
    reach: they failed the proxied tool call while naming neither the field
    nor the level it was written at. ``nan`` raised ``ValueError``, but only
    through a direct call like this one -- ``gt=0`` kept it out of every
    validated config. The remaining two returned a value that was merely
    unhelpful.
    """

    def test_non_finite_ratio_saturates(self):
        # Was ``OverflowError``. ``gt=0`` admits ``+inf`` at the field level (#722), and a config
        # file can carry one: ``json.dumps`` writes a bare ``Infinity`` and
        # ``json.loads`` reads it back. The field now refuses it, but the
        # helper is public and stays total on its own.
        assert tokens_to_chars(400, float("inf")) == MAX_CHAR_BUDGET

    def test_finite_operands_whose_product_overflows_saturate(self):
        # Was ``OverflowError``. Rejecting non-finite input does not cover
        # this: both operands are finite and individually legal.
        assert tokens_to_chars(2, 1e308) == MAX_CHAR_BUDGET

    def test_token_budget_beyond_float_range_saturates(self):
        # Was ``OverflowError``: an int past the float range raises in the
        # multiplication itself rather than producing an inf to test for.
        # ``max_result_tokens`` is now bounded, so no validated config reaches
        # this; the helper is public, so its contract is pinned anyway.
        assert tokens_to_chars(10**400, 3.5) == MAX_CHAR_BUDGET

    def test_finite_product_beyond_int64_saturates(self):
        # Did NOT raise before — it returned the full-width integer. No
        # overflow and no inf, just a value wider than the budget's domain.
        assert tokens_to_chars(10**30, 3.5) == MAX_CHAR_BUDGET

    def test_nan_ratio_yields_zero(self):
        # Was ``ValueError`` (not OverflowError): the old guard was
        # ``chars_per_token <= 0``, which ``nan`` passes, so it reached
        # ``int(nan)``. ``nan`` compares false against 0 either way, so the
        # positive-form guard takes the non-positive branch instead.
        assert tokens_to_chars(1000, float("nan")) == 0

    def test_an_integer_ratio_does_not_break_totality(self):
        # Two ints make the product an int, which no float test can inspect:
        # ``math.isinf`` raises on it. Reachable only through the public
        # helper, since ``max_result_tokens`` is bounded well below this.
        assert tokens_to_chars(10**400, 2) == MAX_CHAR_BUDGET

    def test_sub_one_product_truncates_to_zero(self):
        # Unchanged behaviour, pinned because it is now load-bearing.
        # The helper truncates and does NOT floor: the two callers want
        # different things from a degenerate product, so the floor is a
        # per-site policy. ``ProxyConfig.effective_max_result_chars`` reads
        # this ``0`` as "model scaling off" and falls back to its static
        # default; the token path floors at 1 instead.
        assert tokens_to_chars(1, 0.5) == 0


# ── ProxyConfig.chars_per_token + effective_max_result_chars ─────────────


class TestProxyConfigCharsPerToken:
    def test_default_is_3_5(self):
        cfg = ProxyConfig()
        assert cfg.chars_per_token == 3.5
        assert cfg.token_estimation_mode == TokenEstimationMode.STATIC

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


class TestCharsPerTokenRejectsNonFinite:
    """A non-finite ratio is refused at load, at all three levels (#977).

    ``gt=0`` alone admits ``+inf`` — the same gap #722 documented for the
    shutdown timeouts — and a config file can legitimately carry one, since
    ``json.loads`` reads back the bare ``Infinity`` that ``json.dumps``
    writes. Loading it used to succeed and then raise ``OverflowError`` in
    budget resolution -- on every call whose budget this ratio converts,
    which is not every call: a ratio no conversion selects is inert, and one
    written at a level a nearer ratio shadows never applies.
    """

    @pytest.mark.parametrize(
        "build",
        [
            pytest.param(lambda v: ProxyConfig(chars_per_token=v), id="proxy"),
            pytest.param(
                lambda v: UpstreamServerConfig(prefix="p", command="c", chars_per_token=v),
                id="server",
            ),
            pytest.param(lambda v: ToolOverrideConfig(chars_per_token=v), id="tool"),
        ],
    )
    def test_positive_infinity_is_refused(self, build):
        with pytest.raises(ValidationError):
            build(float("inf"))

    @pytest.mark.parametrize(
        "build",
        [
            pytest.param(lambda v: ProxyConfig(chars_per_token=v), id="proxy"),
            pytest.param(
                lambda v: UpstreamServerConfig(prefix="p", command="c", chars_per_token=v),
                id="server",
            ),
            pytest.param(lambda v: ToolOverrideConfig(chars_per_token=v), id="tool"),
        ],
    )
    def test_a_finite_ratio_still_loads(self, build):
        """Positive control: the new constraint refuses only the non-finite value."""
        assert build(1.85).chars_per_token == 1.85

    @pytest.mark.parametrize(
        "build",
        [
            pytest.param(lambda v: ProxyConfig(chars_per_token=v), id="proxy"),
            pytest.param(
                lambda v: UpstreamServerConfig(prefix="p", command="c", chars_per_token=v),
                id="server",
            ),
            pytest.param(lambda v: ToolOverrideConfig(chars_per_token=v), id="tool"),
        ],
    )
    @pytest.mark.parametrize("value", [float("nan"), float("-inf")])
    def test_nan_and_negative_infinity_were_already_refused(self, build, value):
        """These were already refused before this change, and still are.

        Both compare false against 0, so ``gt=0`` covered them on its own and
        they never reached the conversion; ``allow_inf_nan=False`` now refuses
        them too. The two constraints overlap here, so this pins the outcome
        and not which one is carrying it -- the point is that widening the
        guard did not quietly let either value through.
        """
        with pytest.raises(ValidationError):
            build(value)


class TestModelBudgetOverflow:
    """``ProxyConfig.effective_max_result_chars`` used to multiply on its own.

    A guard placed only in ``tokens_to_chars`` would have left that route
    open, and this is the budget every default-budget call runs under (#977).
    It now goes through the helper, so these pin the outcome rather than the
    arrangement.
    """

    def test_huge_ratio_saturates_instead_of_raising(self):
        # (200000 * 0.05) * 1e308 overflows to inf and the int conversion
        # raised. Both operands are finite, so field validation cannot cover
        # it. The budget is capped at ``default_max_result_chars`` anyway, so
        # saturating lands on the number the operator configured.
        cfg = ProxyConfig(
            consumer_model="claude-sonnet-4",
            chars_per_token=1e308,
            default_max_result_chars=12345,
        )
        assert cfg.effective_max_result_chars() == 12345

    def test_degenerate_product_still_falls_back_rather_than_flooring(self):
        # A tiny ratio makes the product < 1. The existing branch reads that
        # as "model scaling effectively off" and returns the static default;
        # the token path's floor of 1 must not leak in here.
        cfg = ProxyConfig(
            consumer_model="claude-sonnet-4",
            context_budget_ratio=1e-12,
            default_max_result_chars=12345,
        )
        assert cfg.effective_max_result_chars() == 12345


class TestResolvedBudgetSaturationAndFloor:
    """The shared resolver saturates and floors, so no call fails or starves."""

    def test_server_ratio_overflow_saturates(self):
        srv = UpstreamServerConfig(
            prefix="p", command="c", max_result_tokens=2, chars_per_token=1e308
        )
        assert effective_max_result_chars(srv, None, ProxyConfig()) == (MAX_CHAR_BUDGET, 2)

    def test_tool_ratio_overflow_saturates(self):
        """The #929 route: a tool ratio applied to an inherited token budget."""
        srv = UpstreamServerConfig(prefix="p", command="c", max_result_tokens=2)
        override = ToolOverrideConfig(chars_per_token=1e308)
        assert effective_max_result_chars(srv, override, ProxyConfig()) == (MAX_CHAR_BUDGET, 2)

    def test_sub_one_product_floors_at_one_char(self):
        """Two legal positive values must not resolve to a zero-char budget.

        With ``min_result_retention`` disabled a 0 would compress the
        response to nothing — "no output" from input that only said "small".
        """
        srv = UpstreamServerConfig(
            prefix="p", command="c", max_result_tokens=1, chars_per_token=0.5
        )
        assert effective_max_result_chars(srv, None, ProxyConfig()) == (1, 1)

    def test_an_ordinary_budget_is_untouched(self):
        """Positive control: the floor and the cap are both inert in normal range."""
        srv = UpstreamServerConfig(
            prefix="p", command="c", max_result_tokens=1500, chars_per_token=1.85
        )
        assert effective_max_result_chars(srv, None, ProxyConfig()) == (2775, 1500)


class TestMaxResultTokensUpperBound:
    """A token budget is bounded, so it cannot break the cache fingerprint (#977).

    The resolved budget is serialized into the response-cache fingerprint on
    every call, and ``json.dumps`` refuses an integer past the interpreter's
    4300-digit conversion limit — which would fail the call before the
    upstream is dispatched, with the conversion itself never implicated.
    """

    @pytest.mark.parametrize(
        "build",
        [
            pytest.param(
                lambda v: UpstreamServerConfig(prefix="p", command="c", max_result_tokens=v),
                id="server",
            ),
            pytest.param(lambda v: ToolOverrideConfig(max_result_tokens=v), id="tool"),
        ],
    )
    def test_a_budget_past_the_ceiling_is_refused(self, build):
        with pytest.raises(ValidationError):
            build(MAX_CHAR_BUDGET + 1)

    @pytest.mark.parametrize(
        "build",
        [
            pytest.param(
                lambda v: UpstreamServerConfig(prefix="p", command="c", max_result_tokens=v),
                id="server",
            ),
            pytest.param(lambda v: ToolOverrideConfig(max_result_tokens=v), id="tool"),
        ],
    )
    def test_the_ceiling_itself_is_accepted(self, build):
        """Positive control: the new bound refuses only what is above it.

        The lower end is unchanged -- ``gt=0`` still excludes zero and
        negatives -- so this pins that the ceiling is inclusive.
        """
        assert build(MAX_CHAR_BUDGET).max_result_tokens == MAX_CHAR_BUDGET

    def test_the_largest_legal_budget_still_fingerprints(self):
        """The bound is chosen so the fingerprint never sees an unserializable int."""
        srv = UpstreamServerConfig(prefix="p", command="c", max_result_tokens=MAX_CHAR_BUDGET)
        cfg = ProxyConfig(upstream_servers={"srv": srv})
        mgr = _make_manager(cfg, srv)
        tc = mgr._resolve_tool_config("srv", "any_tool")
        assert tc.token_budget == MAX_CHAR_BUDGET
        assert compression_fingerprint(
            tc,
            cfg.min_result_retention,
            cfg.max_upstream_chars,
            cfg.max_upstream_bytes,
            cfg.relevance_scorer,
        )


class TestModelBudgetKeepsItsArithmetic:
    """Routing through the helper must not re-associate the multiplication.

    ``(ctx_tokens * ratio) * chars_per_token`` and
    ``ctx_tokens * (ratio * chars_per_token)`` are not the same float, and the
    difference survives the truncation to an int for some in-range operands.
    Values below are ones where the two groupings actually differ.
    """

    # Operands whose product lands on an integer boundary, where the two
    # groupings straddle it. Random sampling does not find these; each was
    # constructed by choosing the target integer and solving for the ratio.
    # The last case shifts the other way, so the regrouping is not a
    # consistent round-down that could be waved through.
    @pytest.mark.parametrize(
        ("ratio", "cpt", "expected"),
        [
            (0.6436126644587643, 2.461309553832581, 316826),  # regrouped: 316825
            (0.7384910302977905, 4.4640149504194495, 659327),  # regrouped: 659326
            (0.2774381482202568, 4.6249768037714745, 256628),  # regrouped: 256629
        ],
    )
    def test_budget_matches_the_original_grouping(self, ratio, cpt, expected):
        ctx_tokens = MODEL_CONTEXT_WINDOWS["claude-sonnet-4"]
        # What the pre-#977 expression computed, spelled out.
        assert int(ctx_tokens * ratio * cpt) == expected
        cfg = ProxyConfig(
            consumer_model="claude-sonnet-4",
            context_budget_ratio=ratio,
            chars_per_token=cpt,
            default_max_result_chars=10_000_000,  # keep the cap out of the way
        )
        assert cfg.effective_max_result_chars() == expected


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

    def test_token_budget_bypasses_model_aware_path(self):
        """Token budget short-circuits ``effective_max_result_chars()``.

        When a server sets ``max_result_tokens``, the resolution must NOT
        fall through to the model-aware char budget — even if the proxy
        has ``consumer_model`` set and the server keeps the default
        ``max_result_chars``. The token budget is the more specific intent.
        """
        server_cfg = UpstreamServerConfig(
            prefix="x",
            # default max_result_chars=8000 → would normally trigger
            # effective_max_result_chars() in the char path
            max_result_tokens=1000,
            chars_per_token=2.0,
        )
        proxy_cfg = ProxyConfig(
            upstream_servers={"srv": server_cfg},
            consumer_model="claude-sonnet-4",  # would yield model-aware budget
        )
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc = mgr._resolve_tool_config("srv", "any_tool")
        # Token path wins: 1000 * 2.0 = 2000 (NOT effective_max_result_chars()).
        assert tc.max_chars == 2000


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

    def test_tool_ratio_applies_to_an_inherited_token_budget(self):
        """A tool ratio counts even when the token budget comes from the server (#929).

        The ratio describes the content this tool returns, not the level its
        budget happens to be written at. Reading it only beside a per-tool
        ``max_result_tokens`` dropped it here and ran the call at the server
        ratio, against what the field docstring and the configuration guide
        both state.
        """
        server_cfg = UpstreamServerConfig(
            prefix="x",
            max_result_tokens=400,
            chars_per_token=2.5,
            tool_overrides={
                # No budget of its own — only the ratio for its own content.
                "kr_tool": ToolOverrideConfig(chars_per_token=4.0),
            },
        )
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg}, chars_per_token=1.0)
        mgr = _make_manager(proxy_cfg, server_cfg)

        kr = mgr._resolve_tool_config("srv", "kr_tool")
        # 400 * 4.0 (tool ratio) = 1600, not 400 * 2.5 = 1000.
        assert kr.max_chars == 1600
        # The budget is still the server's, so the caller can still tell
        # "1600 chars because 400 tokens" from a flat char budget.
        assert kr.token_budget == 400
        # The sibling tool is untouched: 400 * 2.5 = 1000.
        assert mgr._resolve_tool_config("srv", "other").max_chars == 1000

    def test_tool_ratio_alone_does_not_create_a_token_budget(self):
        """Negative control for the rule above.

        Without a token budget at any level there is nothing to convert, so a
        tool ratio must leave the char budget exactly where it was. A version
        that reached for ``proxy_cfg.chars_per_token`` as a budget would pass
        the test above and fail here.
        """
        server_cfg = UpstreamServerConfig(
            prefix="x",
            max_result_chars=4321,
            tool_overrides={"kr_tool": ToolOverrideConfig(chars_per_token=4.0)},
        )
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg})
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc = mgr._resolve_tool_config("srv", "kr_tool")
        assert tc.max_chars == 4321
        assert tc.token_budget is None

    def test_tool_char_budget_outranks_a_tool_ratio(self):
        """A per-tool char budget still ends the question (#929 boundary).

        Both fields on the same tool under an inherited token budget: the char
        budget is absolute and nothing converts into it, so the ratio is inert
        rather than re-scaling an inherited budget behind it.
        """
        server_cfg = UpstreamServerConfig(
            prefix="x",
            max_result_tokens=400,
            chars_per_token=2.5,
            tool_overrides={
                "both": ToolOverrideConfig(max_result_chars=321, chars_per_token=4.0),
            },
        )
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg})
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc = mgr._resolve_tool_config("srv", "both")
        assert tc.max_chars == 321
        assert tc.token_budget is None


class TestTokenEstimationMode:
    def test_server_and_tool_mode_precedence(self):
        server_cfg = UpstreamServerConfig(
            prefix="x",
            max_result_tokens=1000,
            token_estimation_mode="unicode",
            tool_overrides={
                "static_tool": ToolOverrideConfig(token_estimation_mode="static"),
            },
        )
        proxy_cfg = ProxyConfig(
            upstream_servers={"srv": server_cfg}, token_estimation_mode="static"
        )
        mgr = _make_manager(proxy_cfg, server_cfg)

        dynamic = mgr._resolve_tool_config("srv", "dynamic_tool")
        static = mgr._resolve_tool_config("srv", "static_tool")
        assert dynamic.token_budget == 1000
        assert dynamic.token_estimation_mode == TokenEstimationMode.UNICODE
        assert static.token_estimation_mode == TokenEstimationMode.STATIC

    def test_char_override_clears_inherited_token_budget(self):
        server_cfg = UpstreamServerConfig(
            prefix="x",
            max_result_tokens=1000,
            token_estimation_mode="unicode",
            tool_overrides={"chars": ToolOverrideConfig(max_result_chars=321)},
        )
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg})
        mgr = _make_manager(proxy_cfg, server_cfg)

        tc = mgr._resolve_tool_config("srv", "chars")
        assert tc.max_chars == 321
        assert tc.token_budget is None

    @pytest.mark.asyncio
    async def test_unicode_gate_uses_actual_hangul_density(self):
        server_cfg = UpstreamServerConfig(
            prefix="x",
            compression="truncate",
            max_result_tokens=100,
            chars_per_token=3.5,
            token_estimation_mode="unicode",
        )
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg}, min_result_retention=0)
        mgr = _make_manager(proxy_cfg, server_cfg)
        tc = mgr._resolve_tool_config("srv", "tool")
        mgr._apply_compression = AsyncMock(return_value=("압" * 80, None))  # type: ignore[method-assign]

        await mgr._compress_and_surface(
            server="srv",
            tool="tool",
            upstream_args={},
            cleaned="가" * 200,
            tc=tc,
            cfg_snap=proxy_cfg,
            context_query=None,
            trace_id=None,
        )

        call = mgr._apply_compression.await_args  # type: ignore[union-attr]
        assert call.args[1] == CompressionStrategy.TRUNCATE
        assert call.args[2] == 80  # 200 chars * 100 budget / 250 estimated tokens

    @pytest.mark.asyncio
    async def test_unicode_gate_preserves_ascii_response_that_fits(self):
        server_cfg = UpstreamServerConfig(
            prefix="x",
            compression="truncate",
            max_result_tokens=100,
            chars_per_token=0.5,
            token_estimation_mode="unicode",
        )
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg}, min_result_retention=0)
        mgr = _make_manager(proxy_cfg, server_cfg)
        tc = mgr._resolve_tool_config("srv", "tool")
        mgr._apply_compression = AsyncMock(return_value=("a" * 200, None))  # type: ignore[method-assign]

        await mgr._compress_and_surface(
            server="srv",
            tool="tool",
            upstream_args={},
            cleaned="a" * 200,  # approx 60 tokens: below the 100-token gate
            tc=tc,
            cfg_snap=proxy_cfg,
            context_query=None,
            trace_id=None,
        )

        call = mgr._apply_compression.await_args  # type: ignore[union-attr]
        assert call.args[1] == CompressionStrategy.NONE

    @pytest.mark.asyncio
    async def test_unicode_gate_flag_set_when_the_gate_ran(self):
        """``unicode_token_gate`` keys the metrics estimator switch, so it must
        be True exactly when the gate evaluated this response."""
        server_cfg = UpstreamServerConfig(
            prefix="x",
            compression="truncate",
            max_result_tokens=100,
            token_estimation_mode="unicode",
        )
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg}, min_result_retention=0)
        mgr = _make_manager(proxy_cfg, server_cfg)
        tc = mgr._resolve_tool_config("srv", "tool")
        mgr._apply_compression = AsyncMock(return_value=("압", None))  # type: ignore[method-assign]

        comp = await mgr._compress_and_surface(
            server="srv",
            tool="tool",
            upstream_args={},
            cleaned="가" * 200,
            tc=tc,
            cfg_snap=proxy_cfg,
            context_query=None,
            trace_id=None,
        )

        assert comp.unicode_token_gate is True

    @pytest.mark.asyncio
    async def test_unicode_mode_without_budget_never_sets_the_flag(self):
        """Mode alone must not flip accounting: without a token budget the
        gate cannot run, so metrics keep the static chars/3.5 basis."""
        server_cfg = UpstreamServerConfig(
            prefix="x",
            compression="truncate",
            token_estimation_mode="unicode",
        )
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg}, min_result_retention=0)
        mgr = _make_manager(proxy_cfg, server_cfg)
        tc = mgr._resolve_tool_config("srv", "tool")
        mgr._apply_compression = AsyncMock(return_value=("압", None))  # type: ignore[method-assign]

        comp = await mgr._compress_and_surface(
            server="srv",
            tool="tool",
            upstream_args={},
            cleaned="가" * 200,
            tc=tc,
            cfg_snap=proxy_cfg,
            context_query=None,
            trace_id=None,
        )

        assert comp.unicode_token_gate is False

    @pytest.mark.asyncio
    async def test_progressive_branch_never_sets_the_unicode_flag(self):
        """PROGRESSIVE is zero-loss and skips the gate; its deliberate
        ``len(cleaned)`` metrics basis must not flip to the envelope size."""
        server_cfg = UpstreamServerConfig(
            prefix="x",
            compression="progressive",
            progressive=ProgressiveConfig(chunk_size=10_000),
            max_result_tokens=100,
            token_estimation_mode="unicode",
        )
        proxy_cfg = ProxyConfig(upstream_servers={"srv": server_cfg}, min_result_retention=0)
        mgr = _make_manager(proxy_cfg, server_cfg)
        tc = mgr._resolve_tool_config("srv", "tool")

        comp = await mgr._compress_and_surface(
            server="srv",
            tool="tool",
            upstream_args={},
            cleaned="가" * 200,  # fits one chunk: zero-loss passthrough
            tc=tc,
            cfg_snap=proxy_cfg,
            context_query=None,
            trace_id=None,
        )

        assert comp.unicode_token_gate is False
        assert comp.compressed_chars_for_metrics == 200


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
