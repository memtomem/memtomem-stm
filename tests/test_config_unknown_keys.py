"""Tests for the unknown-key walker over the proxy config models (#611).

The models keep pydantic's default ``extra="ignore"`` on purpose (forward
compat — older binaries must ignore fields written by newer CLIs), so a
typo'd key vanishes silently at load time. ``find_unknown_keys`` names those
dropped keys; these tests pin the walker's classification of every container
shape in the tree, plus a full-clean-config canary that fails loudly if a
future pydantic version changes the ``model_fields`` annotation surface the
walker reads.
"""

from __future__ import annotations

from memtomem_stm.proxy.config import ProxyConfig, find_unknown_keys


def test_top_level_unknown_key_flagged():
    assert find_unknown_keys(ProxyConfig, {"enabld": True}) == ["enabld"]


def test_nested_block_unknown_key_flagged():
    data = {"cache": {"enabled": True, "ttl_secondz": 60}}
    assert find_unknown_keys(ProxyConfig, data) == ["cache.ttl_secondz"]


def test_upstream_server_typo_flagged_under_user_key():
    data = {
        "upstream_servers": {
            "gh": {"prefix": "gh", "command": "gh-server", "max_result_char": 4000}
        }
    }
    assert find_unknown_keys(ProxyConfig, data) == ["upstream_servers.gh.max_result_char"]


def test_two_dict_hops_via_tool_overrides():
    data = {
        "upstream_servers": {
            "gh": {
                "prefix": "gh",
                "tool_overrides": {"list_issues": {"max_result_charz": 1}},
            }
        }
    }
    assert find_unknown_keys(ProxyConfig, data) == [
        "upstream_servers.gh.tool_overrides.list_issues.max_result_charz"
    ]


def test_free_form_dict_leaves_never_descended():
    data = {
        "upstream_servers": {
            "gh": {
                "prefix": "gh",
                "env": {"ANYTHING_GOES": "1", "another_key": "x"},
                "headers": {"X-Custom-Header": "y"},
                "origin": {
                    "source": {"kind": "mcp-json"},
                    "original": {"verbatim": {"host": "entry"}, "weird-key": 1},
                },
            }
        },
        "toolgraph": {"server_name_map": {"gh": "github-crawled"}},
    }
    assert find_unknown_keys(ProxyConfig, data) == []


def test_optional_model_field_descended():
    data = {
        "upstream_servers": {"gh": {"prefix": "gh", "llm": {"modell": "x"}}},
    }
    assert find_unknown_keys(ProxyConfig, data) == ["upstream_servers.gh.llm.modell"]


def test_list_of_models_descended_with_index():
    data = {
        "upstream_servers": {
            "gh": {
                "prefix": "gh",
                "origin": {
                    "source": {"kind": "mcp-json"},
                    "duplicates": [{"kind": "claude-user"}, {"kind": "mcp-json", "badkey": 1}],
                },
            }
        }
    }
    assert find_unknown_keys(ProxyConfig, data) == [
        "upstream_servers.gh.origin.duplicates[1].badkey"
    ]


def test_wrong_type_value_skipped_not_crashed():
    # Key existence is the walker's only concern; model_validate owns type
    # errors. A scalar where a model/dict belongs must neither crash nor flag.
    data = {
        "cache": "not-a-dict",
        "upstream_servers": "also-not-a-dict",
        "toolgraph": {"env": "not-a-dict-either"},
    }
    assert find_unknown_keys(ProxyConfig, data) == []


def test_multiple_unknown_keys_sorted():
    data = {"zz_bogus": 1, "aa_bogus": 2, "cache": {"bogus": 3}}
    assert find_unknown_keys(ProxyConfig, data) == ["aa_bogus", "cache.bogus", "zz_bogus"]


def test_full_clean_config_yields_nothing():
    # Canary: exercises every nested-container classification against a
    # realistic config. If a pydantic upgrade changes the annotation surface
    # (model_fields / FieldInfo.annotation), this fails loudly.
    data = {
        "enabled": True,
        "default_max_result_chars": 16000,
        "consumer_model": "claude-sonnet-4",
        "relevance_scorer": {"scorer": "bm25"},
        "cache": {"enabled": True, "default_ttl_seconds": 3600.0},
        "auto_index": {},
        "metrics": {},
        "toolgraph": {"enabled": False, "server_name_map": {"gh": "github"}},
        "upstream_servers": {
            "gh": {
                "prefix": "gh",
                "command": "gh-server",
                "env": {"TOKEN": "x"},
                "headers": {"X-H": "y"},
                "llm": None,
                "tool_overrides": {"list_issues": {"max_result_chars": 4000}},
                "origin": {
                    "source": {"kind": "mcp-json", "path": "/p/.mcp.json"},
                    "duplicates": [{"kind": "claude-user"}],
                    "original": {"command": "gh-server", "anything": [1, 2]},
                },
            }
        },
    }
    assert find_unknown_keys(ProxyConfig, data) == []
    # And the same payload actually validates — the walker and the validator
    # must agree on what a clean config is.
    assert ProxyConfig.model_validate(data).enabled is True
