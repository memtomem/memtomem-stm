"""Host restore must refuse unreadable JSON without changing the file."""

import json

import pytest
from click.testing import CliRunner

from helpers import set_home

from memtomem_stm.cli import proxy


@pytest.mark.parametrize("filename", [".mcp.json", "claude_desktop_config.json"])
def test_restore_refuses_decoder_recursion(tmp_path, monkeypatch, filename):
    path = tmp_path / filename
    before = b'{"mcpServers": {"existing": {"command": "keep"}}}'
    path.write_bytes(before)

    def fail_decode(_raw):
        raise RecursionError("decoder recursion limit")

    monkeypatch.setattr(proxy.json, "loads", fail_decode)
    ok, error = proxy._json_config_set_entry(
        path, "restored", {"command": "new"}, default_mode=0o600
    )

    assert not ok
    assert error is not None and "parse error:" in error
    assert "fix or move" in error
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("kind", ["claude-user", "claude-project", "mcp-json", "claude-desktop"])
@pytest.mark.parametrize("failure", ["recursion", "malformed", "unicode", "root", "servers"])
@pytest.mark.parametrize("force", [False, True])
def test_eject_refuses_unreadable_target_before_any_write(
    tmp_path, monkeypatch, kind, failure, force
):
    set_home(monkeypatch, tmp_path)
    target = tmp_path / (".mcp.json" if kind == "mcp-json" else ".claude.json")
    monkeypatch.setattr(proxy, "_desktop_config_path", lambda: target)
    raw = {
        "recursion": b'{"canary": "private"}',
        "malformed": b'{"private-canary":',
        "unicode": b"\xff",
        "root": b"[]",
        "servers": b'{"mcpServers": []}',
    }[failure]
    if kind == "claude-project" and failure == "servers":
        raw = json.dumps({"projects": {str(tmp_path.resolve()): {"mcpServers": []}}}).encode()
    target.write_bytes(raw)
    config = tmp_path / "stm.json"
    config.write_text(
        json.dumps({"upstream_servers": {"demo": {"command": "x", "prefix": "demo"}}})
    )
    before = config.read_bytes()
    real_loads = json.loads

    def loads(text, *args, **kwargs):
        if failure == "recursion" and text == raw.decode():
            raise RecursionError("private-canary")
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(proxy.json, "loads", loads)
    calls = []
    monkeypatch.setattr(proxy, "_run_claude_mcp", lambda *a, **k: calls.append((a, k)))
    dest = kind
    if kind == "claude-project":
        dest += ":" + str(tmp_path)
    elif kind == "mcp-json":
        dest += ":" + str(target)
    args = ["eject", "demo", "--to", dest, "--yes", "--json", "--config", str(config)]
    if force:
        args.append("--force")
    result = CliRunner().invoke(proxy.cli, args)
    assert result.exit_code == 1, result.output
    assert "fix it before ejecting" in result.output
    assert "private-canary" not in result.output
    assert calls == []
    assert target.read_bytes() == raw
    assert config.read_bytes() == before


@pytest.mark.parametrize("content", [None, "{}", '{"mcpServers": {}}'])
def test_strict_eject_read_allows_genuinely_absent_entry(tmp_path, content):
    target = tmp_path / ".mcp.json"
    if content is not None:
        target.write_text(content)
    assert proxy._host_existing_entry("mcp-json", str(target), "demo", strict=True) is None


def test_strict_eject_read_refuses_io_error(tmp_path, monkeypatch):
    target = tmp_path / ".mcp.json"

    def read(*args, **kwargs):
        raise PermissionError("private-canary")

    monkeypatch.setattr(type(target), "read_text", read)
    with pytest.raises(proxy._HostConfigReadError, match="PermissionError") as exc:
        proxy._host_existing_entry("mcp-json", str(target), "demo", strict=True)
    assert "private-canary" not in str(exc.value)


@pytest.mark.parametrize("servers", [None, [], "private-canary", 42])
def test_writer_refuses_invalid_servers_map_without_clobbering(tmp_path, servers):
    target = tmp_path / ".mcp.json"
    target.write_text(json.dumps({"mcpServers": servers, "other": {"keep": True}}))
    before = target.read_bytes()
    ok, error = proxy._json_config_set_entry(target, "demo", {"command": "x"}, default_mode=0o600)
    assert not ok
    assert "mcpServers is not an object" in error
    assert "private-canary" not in error
    assert target.read_bytes() == before


@pytest.mark.parametrize("kind", ["claude-user", "claude-project"])
@pytest.mark.parametrize("force", [False, True])
def test_eject_refuses_unsupported_claude_config_dir_before_host_access(
    tmp_path, monkeypatch, kind, force
):
    set_home(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "private-canary"))
    config = tmp_path / "stm.json"
    config.write_text(json.dumps({"upstream_servers": {"demo": {"command": "x"}}}))
    before = config.read_bytes()

    # Both the stale default config and the unknown active layout must stay untouched.
    def unexpected(*args, **kwargs):
        pytest.fail("custom Claude config must be rejected before host reads or writes")

    monkeypatch.setattr(proxy, "_read_eject_host_json", unexpected)
    monkeypatch.setattr(proxy, "_run_claude_mcp", unexpected)
    dest = kind + (":" + str(tmp_path) if kind == "claude-project" else "")
    args = ["eject", "demo", "--to", dest, "--yes", "--json", "--config", str(config)]
    if force:
        args.append("--force")
    result = CliRunner().invoke(proxy.cli, args)
    assert result.exit_code == 1, result.output
    assert "unsupported with CLAUDE_CONFIG_DIR set" in result.output
    assert "private-canary" not in result.output
    assert config.read_bytes() == before


def test_custom_claude_config_does_not_affect_explicit_json_target(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "custom"))
    target = tmp_path / ".mcp.json"
    target.write_text('{"mcpServers": {"demo": {"command": "x"}}}')
    assert proxy._host_existing_entry("mcp-json", str(target), "demo", strict=True) == {
        "command": "x"
    }
