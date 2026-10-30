"""Host restore must refuse unreadable JSON without changing the file."""

import pytest

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
