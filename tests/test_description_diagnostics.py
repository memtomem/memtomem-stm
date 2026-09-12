"""The description-budget advisory reports the cap the advertisement will use.

Read-only advice about a configured cap, so every case here builds a config and
a probe result directly: the check must never need a manager, a store, or a
live connection (#1015).
"""

from __future__ import annotations

import re

import pytest

from memtomem_stm.cli.description_diagnostics import (
    _SCOPE,
    description_budget_doctor_checks,
)
from memtomem_stm.proxy.config import (
    CompressionStrategy,
    HybridConfig,
    ProxyConfig,
    TailMode,
    ToolOverrideConfig,
    UpstreamServerConfig,
)
from memtomem_stm.proxy.staged_status import ProbeStage, StagedProbeResult

PATH_HINT = "/tmp/stm_proxy.json"
SELECTIVE_SUFFIX_CHARS = 44


def _config(*, global_cap: int | None = None, **server: object) -> ProxyConfig:
    default_compression = server.pop("default_compression", None)
    data: dict[str, object] = {
        "enabled": True,
        "upstream_servers": {
            "docs": UpstreamServerConfig(
                prefix="dc", transport="stdio", command="docs-server", **server
            )
        },
    }
    if global_cap is not None:
        data["max_description_chars"] = global_cap
    if default_compression is not None:
        data["default_compression"] = default_compression
    return ProxyConfig.model_validate(data)


def _probe(*rows: tuple[str, int], connected: bool = True) -> dict[str, StagedProbeResult]:
    if not connected:
        return {"docs": StagedProbeResult(stage=ProbeStage.CONFIGURED, error="offline")}
    return {
        "docs": StagedProbeResult(
            stage=ProbeStage.TOOLS_DISCOVERED, tools=len(rows), description_chars=rows
        )
    }


def _check(config: ProxyConfig, probes: dict[str, StagedProbeResult]):
    checks = description_budget_doctor_checks(config, probes, path_hint=PATH_HINT)
    assert len(checks) == 1
    return checks[0]


class TestStatusContract:
    def test_defaults_pass_with_no_next_action(self):
        check = _check(_config(), _probe(("search", 300), ("ls", 40)))
        assert check[0] == "description_budget:docs"
        assert check[1] == "description budget: docs"
        assert check[2] == "PASS"
        assert "all 2 discovered descriptions fit whole" in check[3]
        assert "longest 300 chars" in check[3]
        assert check[4] is None

    def test_never_fails(self):
        cases = [
            (_config(), _probe(("a", 10))),
            (_config(max_description_chars=100), _probe(("a", 5000))),
            (
                _config(max_description_chars=32, compression=CompressionStrategy.PROGRESSIVE),
                _probe(("a", 10)),
            ),
            (_config(), _probe(connected=False)),
            (_config(), {}),
        ]
        for config, probes in cases:
            assert _check(config, probes)[2] in {"PASS", "WARN"}

    def test_every_detail_carries_the_scope_and_the_host_cap_pointer(self):
        for config, probes in [
            (_config(), _probe(("a", 10))),
            (_config(max_description_chars=100), _probe(("a", 5000))),
            (_config(), _probe(connected=False)),
        ]:
            detail = _check(config, probes)[3]
            assert detail.endswith(_SCOPE)
            assert "#1014" in detail


class TestBindingLevel:
    def test_server_default_binds_when_only_the_global_was_raised(self):
        config = _config(global_cap=9000)
        detail = _check(config, _probe(("a", 5000)))[3]
        assert "cap 4000 = min(server 4000 (default), global 9000)" in detail
        assert "raising only the global is a no-op" in detail
        assert "upstream_servers.docs" in _check(config, _probe(("a", 5000)))[4]

    def test_global_binds_when_it_is_the_stricter(self):
        config = _config(global_cap=100, max_description_chars=4000)
        check = _check(config, _probe(("a", 500)))
        assert "the global value binds" in check[3]
        assert "the top level" in check[4]
        # Only the global is under the requirement, so only it is named.
        assert "upstream_servers.docs" not in check[4]

    def test_equal_levels_name_both(self):
        config = _config(global_cap=100, max_description_chars=100)
        check = _check(config, _probe(("a", 5000)))
        assert "both levels bind equally" in check[3]
        assert "upstream_servers.docs and the top level" in check[4]

    def test_every_level_below_the_requirement_is_named(self):
        """Raising the binding level alone stops at whichever level is next.

        server 100 / global 4000 needing 5010: an edit to the server alone
        leaves the global capping it at 4000, which is the very mistake this
        check exists to catch.
        """
        check = _check(_config(global_cap=4000, max_description_chars=100), _probe(("a", 5000)))
        assert "5010" in check[4]
        assert "upstream_servers.docs and the top level" in check[4]

    def test_binding_alone_is_not_a_finding(self):
        """A deliberately lower per-server cap that truncates nothing is fine."""
        check = _check(_config(global_cap=9000), _probe(("a", 10)))
        assert check[2] == "PASS"
        assert "raising only the global is a no-op" in check[3]


class TestTruncation:
    def test_counts_and_sizes_the_overflow(self):
        check = _check(_config(max_description_chars=100), _probe(("search", 1830), ("ls", 10)))
        assert check[2] == "WARN"
        assert "1 of 2 discovered descriptions exceed the text budget" in check[3]
        assert "largest by 1772 chars over it ('search', 1830 chars)" in check[3]
        assert "a cap of 1840" in check[3]
        assert "1840" in check[4]

    def test_the_overflow_is_named_as_a_budget_excess_not_the_cut(self):
        """The cut can remove more than the excess, so the line must not claim
        the excess IS the loss.

        A word-boundary retreat plus the ellipsis drops far more than the
        overflow: 101 chars into a 90-char budget shows 43, not 79.
        """
        detail = _check(_config(max_description_chars=100), _probe(("a", 101)))[3]
        assert "over it" in detail
        assert "retreats to a word or sentence boundary" in detail

    def test_a_fitting_suffix_shrinks_the_body(self):
        """The suffix takes its chars first, so it moves the truncation edge."""
        plain = _config(max_description_chars=100)
        suffixed = _config(
            max_description_chars=100, compression=CompressionStrategy.SELECTIVE
        )
        assert _check(plain, _probe(("a", 90)))[2] == "PASS"
        assert _check(suffixed, _probe(("a", 90)))[2] == "WARN"
        assert f"a cap of {90 + 10 + SELECTIVE_SUFFIX_CHARS}" in _check(
            suffixed, _probe(("a", 90))
        )[3]

    def test_unreachable_upstream_says_it_did_not_measure(self):
        check = _check(_config(max_description_chars=100), _probe(connected=False))
        assert "per-tool truncation was not assessed" in check[3]
        assert "tool discovery did not complete" in check[3]
        assert "exceed the text budget" not in check[3]

    def test_each_silence_names_its_own_reason(self):
        """A reachable upstream that advertises nothing is not an unreachable one."""
        config = _config(max_description_chars=100)
        assert "not probed in this run" in _check(config, {})[3]
        assert "tool discovery did not complete" in _check(config, _probe(connected=False))[3]
        assert "advertises no tools" in _check(config, _probe())[3]

        stale = {"docs": StagedProbeResult(stage=ProbeStage.TOOLS_DISCOVERED, tools=2)}
        assert "no per-tool description lengths" in _check(config, stale)[3]

    def test_a_failed_discovery_names_the_stage_it_reached(self):
        """Connect and initialize can succeed while ``tools/list`` fails; calling
        that "unreachable" sends the operator after the wrong thing."""
        probes = {
            "docs": StagedProbeResult(stage=ProbeStage.MCP_INITIALIZED, error="boom", tools=0)
        }
        detail = _check(_config(), probes)[3]
        assert "tool discovery did not complete" in detail
        assert "tools discovered" in detail

    def test_hidden_tools_are_not_counted(self):
        config = _config(
            max_description_chars=100,
            tool_overrides={"search": ToolOverrideConfig(hidden=True)},
        )
        check = _check(config, _probe(("search", 5000), ("ls", 10)))
        assert check[2] == "PASS"
        assert "all 1 discovered descriptions fit whole" in check[3]

    def test_the_scope_admits_what_it_did_not_apply(self):
        """Hidden is the only exposure rule applied, so the counts are about
        discovered tools, not advertised ones — and must say so."""
        detail = _check(_config(), _probe(("a", 10)))[3]
        assert "exposure filtering" in detail
        assert "can withhold more" in detail


class TestSuffixFit:
    def test_a_suffix_that_cannot_fit_is_reported(self):
        config = _config(max_description_chars=32, compression=CompressionStrategy.PROGRESSIVE)
        check = _check(config, _probe(("a", 5)))
        assert check[2] == "WARN"
        assert "convention suffix" in check[3]
        assert "(44 chars) is dropped" in check[3]
        assert "only 22 chars remain" in check[3]
        # 54 readmits the suffix but leaves zero body: the 5-char description
        # would be cut to nothing. Keeping both needs 5 + 10 + 44.
        assert "59" in check[4]
        assert "a cap of 59" in check[3]

    def test_a_recommendation_keeps_the_body_it_restores_the_suffix_beside(self):
        for chars, need in ((5, 59), (100, 154)):
            config = _config(
                max_description_chars=32, compression=CompressionStrategy.SELECTIVE
            )
            check = _check(config, _probe(("a", chars)))
            assert f"a cap of {need}" in check[3]

    def test_mixed_suffix_lengths_report_the_one_that_sets_the_requirement(self):
        """Two strategies drop two different suffixes; the line must not depend
        on which row happened to come last."""
        config = _config(
            max_description_chars=32,
            compression=CompressionStrategy.HYBRID,
            hybrid=HybridConfig(tail_mode=TailMode.TOC),
            tool_overrides={
                "long": ToolOverrideConfig(compression=CompressionStrategy.SELECTIVE)
            },
        )
        forward = _check(config, _probe(("long", 5), ("short", 5)))[3]
        reverse = _check(config, _probe(("short", 5), ("long", 5)))[3]
        assert "(44 chars) is dropped on 2 tool(s)" in forward
        assert forward == reverse

    def test_it_is_reported_without_a_reachable_upstream(self):
        """The fit is a config fact, so a dead upstream does not hide it."""
        config = _config(max_description_chars=32, compression=CompressionStrategy.SELECTIVE)
        check = _check(config, _probe(connected=False))
        assert check[2] == "WARN"
        assert "(44 chars) is dropped" in check[3]

    def test_the_boundary_matches_the_advertisement(self):
        for cap, dropped in ((53, True), (54, False)):
            config = _config(
                max_description_chars=cap, compression=CompressionStrategy.SELECTIVE
            )
            check = _check(config, _probe(("a", 5)))
            assert ("the convention suffix '" in check[3]) is dropped

    def test_a_hybrid_truncate_tail_needs_no_suffix(self):
        config = _config(
            max_description_chars=32,
            compression=CompressionStrategy.HYBRID,
            hybrid=HybridConfig(tail_mode=TailMode.TRUNCATE),
        )
        assert _check(config, _probe(("a", 5)))[2] == "PASS"


class TestStrategyPrecedence:
    def test_an_omitted_server_strategy_resolves_against_the_global_default(self):
        config = _config(
            max_description_chars=32, default_compression=CompressionStrategy.SELECTIVE
        )
        assert "is dropped" in _check(config, _probe(("a", 5)))[3]

    def test_a_tool_override_wins_over_the_server_strategy(self):
        config = _config(
            max_description_chars=100,
            compression=CompressionStrategy.SELECTIVE,
            tool_overrides={"plain": ToolOverrideConfig(compression=CompressionStrategy.NONE)},
        )
        detail = _check(config, _probe(("plain", 90), ("hinted", 90)))[3]
        # Only the tool that kept the suffix loses room to it.
        assert "1 of 2 discovered descriptions exceed the text budget" in detail
        assert "'hinted'" in detail


@pytest.mark.parametrize(
    "server_cap, global_cap, compression, rows",
    [
        (100, None, None, (("search", 1830), ("ls", 10))),
        (32, None, CompressionStrategy.SELECTIVE, (("a", 5),)),
        (32, None, CompressionStrategy.SELECTIVE, (("a", 500),)),
        (100, 4000, None, (("a", 5000),)),
        (4000, 100, None, (("a", 5000),)),
        (100, 100, CompressionStrategy.PROGRESSIVE, (("a", 900), ("b", 20))),
    ],
)
def test_the_recommended_cap_actually_clears_the_finding(server_cap, global_cap, compression, rows):
    """The advice is a claim about the state after the edit, so that is what
    has to be checked.

    Applying the recommended number to every level must silence the check, and
    one char less must not. A recommendation verified only against today's
    numbers is how a check ends up repeating, one step later, the very
    ``min(server, global)`` mistake it exists to catch.
    """
    server: dict[str, object] = {"max_description_chars": server_cap}
    if compression is not None:
        server["compression"] = compression

    warned = _check(_config(global_cap=global_cap, **server), _probe(*rows))
    assert warned[2] == "WARN"
    need = int(re.search(r"a cap of (\d+)", warned[3]).group(1))

    server["max_description_chars"] = need
    assert _check(_config(global_cap=need, **server), _probe(*rows))[2] == "PASS"

    server["max_description_chars"] = need - 1
    assert _check(_config(global_cap=need - 1, **server), _probe(*rows))[2] == "WARN"


def test_a_server_with_no_probe_entry_is_still_assessed():
    config = _config(max_description_chars=32, compression=CompressionStrategy.SELECTIVE)
    check = _check(config, {})
    assert check[2] == "WARN"


def test_every_configured_server_gets_one_check():
    config = ProxyConfig.model_validate(
        {
            "enabled": True,
            "upstream_servers": {
                "docs": {"prefix": "dc", "transport": "stdio", "command": "d"},
                "api": {"prefix": "ap", "transport": "stdio", "command": "a"},
            },
        }
    )
    checks = description_budget_doctor_checks(config, {}, path_hint=PATH_HINT)
    assert [c[0] for c in checks] == ["description_budget:docs", "description_budget:api"]


def test_no_upstreams_yields_no_checks():
    config = ProxyConfig.model_validate({"enabled": True})
    assert description_budget_doctor_checks(config, {}, path_hint=PATH_HINT) == []
