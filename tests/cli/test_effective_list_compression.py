"""List the effective compression default without rewriting raw config JSON (#1043)."""

import json

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import cli


@pytest.mark.parametrize(
    "global_strategy,server_strategy,expected,source",
    [
        ("none", None, "none", "global"),
        ("hybrid", None, "hybrid", "global"),
        ("none", "auto", "auto", "server"),
        ("auto", "none", "none", "server"),
    ],
)
def test_list_uses_runtime_compression_precedence(
    tmp_path, global_strategy, server_strategy, expected, source
):
    entry = {"prefix": "s", "command": "echo"}
    if server_strategy is not None:
        entry["compression"] = server_strategy
    raw = {"default_compression": global_strategy, "upstream_servers": {"s": entry}}
    path = tmp_path / "proxy.json"
    path.write_text(json.dumps(raw))
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--config", str(path)])
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if line.startswith("s "))
    assert row.split()[3] == expected
    assert "resolved server default" in result.output
    result = runner.invoke(cli, ["list", "--config", str(path), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["servers"]["s"] == entry
    assert data["effective_compression"]["s"] == {
        "strategy": expected,
        "source": source,
        "tool_overrides": {},
    }
    assert json.loads(path.read_text()) == raw


def test_list_distinguishes_tool_override_and_applies_environment(tmp_path, monkeypatch):
    path = tmp_path / "proxy.json"
    raw = {
        "default_compression": "auto",
        "upstream_servers": {
            "s": {
                "prefix": "s",
                "command": "echo",
                "tool_overrides": {
                    "t": {"compression": "none"},
                    "other": {"max_result_chars": 5000},
                },
            }
        },
    }
    path.write_text(json.dumps(raw))
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__DEFAULT_COMPRESSION", "hybrid")
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--config", str(path)])
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if line.startswith("s "))
    assert row.split()[3] == "hybrid"
    assert '"s"/"t": none (tool override)' in result.output
    result = runner.invoke(cli, ["list", "--config", str(path), "--json"])
    data = json.loads(result.output)
    # The whole entry: a JSON leg that kept the file's `auto` would still carry
    # the right override.
    assert data["effective_compression"]["s"] == {
        "strategy": "hybrid",
        "source": "global",
        "tool_overrides": {"t": "none"},
    }
    assert data["servers"]["s"] == raw["upstream_servers"]["s"]


def test_invalid_config_does_not_claim_a_runtime_strategy(tmp_path):
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            {
                "default_compression": "invalid",
                "upstream_servers": {
                    "s": {"prefix": "s", "command": "echo"},
                },
            }
        )
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert "fails validation" in result.output
    row = next(line for line in result.output.splitlines() if line.startswith("s "))
    assert row.split()[3] == "unknown"
    result = runner.invoke(cli, ["list", "--config", str(path), "--json"])
    assert json.loads(result.output)["effective_compression"] == {}


def test_server_level_environment_override_is_a_server_source(tmp_path, monkeypatch):
    path = tmp_path / "proxy.json"
    raw = {
        "default_compression": "auto",
        "upstream_servers": {
            "s": {
                "prefix": "s",
                "command": "echo",
                "tool_overrides": {"same": {"compression": "none"}},
            }
        },
    }
    path.write_text(json.dumps(raw))
    monkeypatch.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__S__COMPRESSION", "none")
    runner = CliRunner()
    result = runner.invoke(cli, ["list", "--config", str(path), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    # The raw map stays the file as written; the effective view carries the env.
    assert data["servers"]["s"] == raw["upstream_servers"]["s"]
    # A tool override equal to the server default is still an explicit override.
    assert data["effective_compression"]["s"] == {
        "strategy": "none",
        "source": "server",
        "tool_overrides": {"same": "none"},
    }


def test_tool_override_lines_cannot_collide_on_a_slash(tmp_path):
    path = tmp_path / "proxy.json"
    raw = {
        "upstream_servers": {
            "a/b": {
                "prefix": "ab",
                "command": "echo",
                "tool_overrides": {"c": {"compression": "none"}},
            },
            "a": {
                "prefix": "a",
                "command": "echo",
                "tool_overrides": {"b/c": {"compression": "hybrid"}},
            },
        }
    }
    path.write_text(json.dumps(raw))
    result = CliRunner().invoke(cli, ["list", "--config", str(path)])
    assert result.exit_code == 0, result.output
    lines = [line.strip() for line in result.output.splitlines() if "(tool override)" in line]
    assert sorted(lines) == [
        '"a"/"b/c": hybrid (tool override)',
        '"a/b"/"c": none (tool override)',
    ]


def test_tool_override_names_are_escaped_not_just_quoted(tmp_path):
    """Quoting alone would let `"` or `\\` forge a boundary. Lone surrogates
    take the JSON escape (lowercase, from the surrogate-safe writer); `_disp`
    covers what JSON leaves literal (bidi controls, uppercase)."""
    names = ['q"x', "b\\x", "\u4e2d\u6587", "r\u202ex", "s\ud800"]
    raw = {
        "upstream_servers": {
            name: {
                "prefix": f"p{i}",
                "command": "echo",
                "tool_overrides": {"t": {"compression": "none"}},
            }
            for i, name in enumerate(names)
        }
    }
    path = tmp_path / "proxy.json"
    path.write_text(json.dumps(raw))
    result = CliRunner().invoke(cli, ["list", "--config", str(path)])
    assert result.exit_code == 0, result.output
    lines = {line.strip() for line in result.output.splitlines() if "(tool override)" in line}
    assert lines == {
        '"q\\"x"/"t": none (tool override)',
        '"b\\\\x"/"t": none (tool override)',
        '"\u4e2d\u6587"/"t": none (tool override)',
        '"r\\u202Ex"/"t": none (tool override)',
        '"s\\ud800"/"t": none (tool override)',
    }
