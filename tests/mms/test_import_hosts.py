"""Tests for ``mms.import_hosts`` — host config scanners + extracted helpers."""

from __future__ import annotations

import json
import sys

import pytest

from memtomem_stm.mms import import_hosts as ih
from memtomem_stm.mms.secrets import Kind
from memtomem_stm.mms.state import RegistryServer
from helpers import set_home


# ---------------------------------------------------------------------------
# Shape-agnostic primitives — round-trip what proxy.py used to own
# ---------------------------------------------------------------------------


class TestHelperReexports:
    """The 5 helpers extracted from proxy.py — proxy.py re-imports these.

    These tests pin the shape so a future refactor that changes signatures
    without updating callers fails here, not at runtime in `mms add --from-clients`.
    """

    def test_dangerous_env_keys_includes_known_dangerous(self):
        for key in ["LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "NODE_OPTIONS"]:
            assert key in ih._DANGEROUS_ENV_KEYS

    def test_blocked_import_names_includes_self_and_ltm(self):
        for name in ["mms", "memtomem-stm", "memtomem-stm-proxy", "memtomem", "memtomem-server"]:
            assert name in ih._BLOCKED_IMPORT_NAMES

    def test_desktop_config_path_macos_shape(self):
        # We don't assert exact platform behavior — the path string shape is
        # what proxy.py and import_hosts both rely on.
        path = ih._desktop_config_path()
        assert "Claude" in str(path)
        assert path.name == "claude_desktop_config.json"

    def test_read_json_safely_returns_none_for_missing(self, tmp_path):
        assert ih._read_json_safely(tmp_path / "nope.json") is None

    def test_read_json_safely_returns_none_for_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        assert ih._read_json_safely(bad) is None

    def test_read_json_safely_returns_none_for_non_dict(self, tmp_path):
        listy = tmp_path / "list.json"
        listy.write_text("[1, 2, 3]", encoding="utf-8")
        assert ih._read_json_safely(listy) is None

    def test_read_json_safely_round_trip(self, tmp_path):
        good = tmp_path / "good.json"
        good.write_text('{"foo": "bar"}', encoding="utf-8")
        assert ih._read_json_safely(good) == {"foo": "bar"}

    def test_is_self_reference_blocks_command(self):
        assert ih._is_self_reference({"command": "mms"})
        assert ih._is_self_reference({"command": "/usr/local/bin/memtomem-stm"})

    def test_is_self_reference_blocks_in_args(self):
        # uvx wrapper pattern
        assert ih._is_self_reference(
            {"command": "uvx", "args": ["--from", "memtomem", "memtomem-server"]}
        )

    def test_is_self_reference_passes_normal_entries(self):
        assert not ih._is_self_reference({"command": "npx", "args": ["-y", "@mcp/fs"]})


# ---------------------------------------------------------------------------
# _to_registry_server — the host-shape → registry-shape mapper
# ---------------------------------------------------------------------------


class TestToRegistryServer:
    def test_basic_stdio_entry(self):
        srv = ih._to_registry_server(
            "filesystem",
            {"command": "npx", "args": ["-y", "@mcp/fs", "/home"]},
        )
        assert srv == RegistryServer(
            command="npx",
            args=["-y", "@mcp/fs", "/home"],
            env={},
            prefix="filesystem",
        )

    def test_strips_dangerous_env(self):
        srv = ih._to_registry_server(
            "x",
            {
                "command": "npx",
                "env": {
                    "LD_PRELOAD": "/evil.so",
                    "PYTHONPATH": "/evil",
                    "API_KEY": "ghp_secret",
                    "PORT": "8080",
                },
            },
        )
        assert srv is not None
        assert srv.env == {"API_KEY": "ghp_secret", "PORT": "8080"}
        assert "LD_PRELOAD" not in srv.env
        assert "PYTHONPATH" not in srv.env

    def test_skips_self_reference(self):
        assert ih._to_registry_server("mms", {"command": "mms"}) is None

    def test_skips_no_command(self):
        # SSE/HTTP entries (url-only) are out of W1 mms scope per RFC §5.3.
        assert ih._to_registry_server("x", {"url": "https://example/sse"}) is None
        assert ih._to_registry_server("x", {}) is None

    def test_handles_missing_args_and_env(self):
        srv = ih._to_registry_server("x", {"command": "echo"})
        assert srv == RegistryServer(command="echo", args=[], env={}, prefix="x")

    def test_args_filters_non_strings(self):
        srv = ih._to_registry_server("x", {"command": "echo", "args": ["a", 42, "b"]})
        assert srv is not None
        assert srv.args == ["a", "b"]


# ---------------------------------------------------------------------------
# _derive_prefix — RFC §5.3 schema + _PREFIX_RE constraint
# ---------------------------------------------------------------------------


class TestDerivePrefix:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("filesystem", "filesystem"),
            ("github", "github"),
            ("next-devtools", "next_devtools"),
            ("memtomem-com", "memtomem_com"),
            ("ALLCAPS", "allcaps"),
        ],
    )
    def test_typical_names(self, name, expected):
        assert ih._derive_prefix(name) == expected

    def test_starts_with_digit_strips_to_letter(self):
        # "9-foo" → "9_foo" → strip leading "9_" → "foo"
        assert ih._derive_prefix("9-foo") == "foo"

    def test_only_invalid_chars_falls_back_to_mcp(self):
        assert ih._derive_prefix("123") == "mcp"
        assert ih._derive_prefix("---") == "mcp"


# ---------------------------------------------------------------------------
# Per-host scanners
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path)
    return tmp_path


class TestScanClaudeCode:
    def test_user_scope(self, sandbox_home, tmp_path):
        cfg = {
            "mcpServers": {
                "filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]},
                "github": {
                    "command": "npx",
                    "args": ["-y", "@mcp/gh"],
                    "env": {"GITHUB_TOKEN": "ghp_x"},
                },
            }
        }
        (sandbox_home / ".claude.json").write_text(json.dumps(cfg), encoding="utf-8")

        candidates = ih.scan_claude_code(tmp_path)
        names = {c.name for c in candidates}
        assert names == {"filesystem", "github"}
        for c in candidates:
            assert c.source_label == "Claude Code (user)"
            assert c.is_repo_local is False

        gh = next(c for c in candidates if c.name == "github")
        assert gh.env_classification["GITHUB_TOKEN"].is_secret is True
        assert gh.env_classification["GITHUB_TOKEN"].kind is Kind.KEY_PATTERN

    def test_per_project_scope(self, sandbox_home, tmp_path):
        cwd = tmp_path / "proj"
        cwd.mkdir()
        cfg = {
            "projects": {
                str(cwd): {
                    "mcpServers": {
                        "playwright": {"command": "npx", "args": ["-y", "@mcp/pw"]},
                    }
                }
            }
        }
        (sandbox_home / ".claude.json").write_text(json.dumps(cfg), encoding="utf-8")
        candidates = ih.scan_claude_code(cwd)
        assert len(candidates) == 1
        assert candidates[0].source_label == "Claude Code (project)"
        # Home-stored (~/.claude.json .projects.<cwd>) — NOT repo-local, so a
        # repo checkout can't ship it; must not be gated.
        assert candidates[0].is_repo_local is False

    def test_dot_mcp_json(self, sandbox_home, tmp_path):
        cfg = {"mcpServers": {"local-tool": {"command": "echo"}}}
        (tmp_path / ".mcp.json").write_text(json.dumps(cfg), encoding="utf-8")
        candidates = ih.scan_claude_code(tmp_path)
        assert len(candidates) == 1
        assert candidates[0].source_label == ".mcp.json (project)"
        assert candidates[0].name == "local-tool"
        # <cwd>/.mcp.json — a repo checkout can ship this, so it is repo-local.
        assert candidates[0].is_repo_local is True

    def test_self_reference_filtered(self, sandbox_home, tmp_path):
        cfg = {
            "mcpServers": {
                "stm-self": {"command": "mms"},
                "fine": {"command": "echo"},
            }
        }
        (sandbox_home / ".claude.json").write_text(json.dumps(cfg), encoding="utf-8")
        names = {c.name for c in ih.scan_claude_code(tmp_path)}
        assert names == {"fine"}

    def test_dangerous_env_filtered(self, sandbox_home, tmp_path):
        cfg = {
            "mcpServers": {
                "evil": {
                    "command": "node",
                    "env": {"LD_PRELOAD": "/evil.so", "API_KEY": "ghp_x"},
                }
            }
        }
        (sandbox_home / ".claude.json").write_text(json.dumps(cfg), encoding="utf-8")
        candidates = ih.scan_claude_code(tmp_path)
        assert len(candidates) == 1
        assert "LD_PRELOAD" not in candidates[0].server.env
        assert "API_KEY" in candidates[0].server.env

    def test_missing_file_returns_empty(self, sandbox_home, tmp_path):
        assert ih.scan_claude_code(tmp_path) == []


class TestScanCursor:
    """The cwd must be different from HOME or `~/.cursor/mcp.json` and
    `<cwd>/.cursor/mcp.json` collapse to the same path."""

    def test_user_scope(self, sandbox_home, tmp_path):
        cwd = tmp_path / "proj"
        cwd.mkdir()
        cursor_dir = sandbox_home / ".cursor"
        cursor_dir.mkdir()
        cfg = {"mcpServers": {"foo": {"command": "echo"}}}
        (cursor_dir / "mcp.json").write_text(json.dumps(cfg), encoding="utf-8")
        candidates = ih.scan_cursor(cwd)
        assert len(candidates) == 1
        assert candidates[0].source_label == "Cursor (user)"
        assert candidates[0].is_repo_local is False

    def test_project_scope(self, sandbox_home, tmp_path):
        cwd = tmp_path / "proj"
        cwd.mkdir()
        proj_cursor = cwd / ".cursor"
        proj_cursor.mkdir()
        cfg = {"mcpServers": {"local": {"command": "echo"}}}
        (proj_cursor / "mcp.json").write_text(json.dumps(cfg), encoding="utf-8")
        candidates = ih.scan_cursor(cwd)
        assert len(candidates) == 1
        assert candidates[0].source_label == "Cursor (project)"
        # <cwd>/.cursor/mcp.json — repo-local, gated.
        assert candidates[0].is_repo_local is True

    def test_missing_returns_empty(self, sandbox_home, tmp_path):
        cwd = tmp_path / "proj"
        cwd.mkdir()
        assert ih.scan_cursor(cwd) == []


class TestProjectLocalGateMessage:
    def test_names_sorted_count_and_flag(self):
        msg = ih.project_local_gate_message(["beta", "alpha"])
        assert "2 MCP entries" in msg
        assert "alpha, beta" in msg  # sorted
        assert "--allow-project-configs" in msg
        assert ".mcp.json" in msg

    def test_singular(self):
        assert "1 MCP entry" in ih.project_local_gate_message(["solo"])


class TestScanCodex:
    def test_basic_toml_entries(self, sandbox_home, tmp_path):
        codex_dir = sandbox_home / ".codex"
        codex_dir.mkdir()
        toml_text = (
            "[mcp_servers.filesystem]\n"
            'command = "npx"\n'
            'args = ["-y", "@mcp/fs"]\n'
            "\n"
            "[mcp_servers.github]\n"
            'command = "npx"\n'
            'args = ["-y", "@mcp/gh"]\n'
            "[mcp_servers.github.env]\n"
            'GITHUB_TOKEN = "ghp_x"\n'
        )
        (codex_dir / "config.toml").write_text(toml_text, encoding="utf-8")

        candidates = ih.scan_codex(tmp_path)
        names = {c.name for c in candidates}
        assert names == {"filesystem", "github"}
        for c in candidates:
            assert c.source_label == "Codex CLI"

    def test_missing_returns_empty(self, sandbox_home, tmp_path):
        assert ih.scan_codex(tmp_path) == []

    def test_corrupted_toml_returns_empty(self, sandbox_home, tmp_path):
        codex_dir = sandbox_home / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("not [valid toml\n", encoding="utf-8")
        assert ih.scan_codex(tmp_path) == []

    def test_honors_codex_home(self, sandbox_home, tmp_path, monkeypatch):
        codex_home = tmp_path / "portable-codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            '[mcp_servers.portable]\ncommand = "echo"\n', encoding="utf-8"
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        assert [c.name for c in ih.scan_codex(tmp_path)] == ["portable"]

    def test_trusted_project_config_is_repo_local(self, sandbox_home, tmp_path):
        codex_dir = sandbox_home / ".codex"
        codex_dir.mkdir()
        cwd = tmp_path / "project"
        project_dir = cwd / ".codex"
        project_dir.mkdir(parents=True)
        escaped = str(cwd.resolve()).replace("\\", "\\\\").replace('"', '\\"')
        (codex_dir / "config.toml").write_text(
            f'[projects."{escaped}"]\ntrust_level = "trusted"\n', encoding="utf-8"
        )
        (project_dir / "config.toml").write_text(
            '[mcp_servers.local]\ncommand = "echo"\n', encoding="utf-8"
        )
        candidates = ih.scan_codex(cwd)
        assert len(candidates) == 1
        assert candidates[0].source_label == "Codex CLI (project)"
        assert candidates[0].is_repo_local is True


class TestScanClaudeDesktop:
    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only path in W1")
    def test_macos_scan(self, monkeypatch, tmp_path):
        # Pin _desktop_config_path() to a sandbox path.
        sandbox_path = tmp_path / "claude_desktop_config.json"
        cfg = {"mcpServers": {"foo": {"command": "echo"}}}
        sandbox_path.write_text(json.dumps(cfg), encoding="utf-8")
        monkeypatch.setattr(ih, "_desktop_config_path", lambda: sandbox_path)

        candidates = ih.scan_claude_desktop(tmp_path)
        assert len(candidates) == 1
        assert candidates[0].source_label == "Claude Desktop"

    def test_non_macos_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "linux")
        assert ih.scan_claude_desktop(tmp_path) == []

    def test_windows_appdata_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", str(tmp_path))
        config = tmp_path / "Claude" / "claude_desktop_config.json"
        config.parent.mkdir()
        config.write_text(
            json.dumps({"mcpServers": {"win": {"command": "cmd.exe"}}}),
            encoding="utf-8",
        )
        candidates = ih.scan_claude_desktop(tmp_path)
        assert [c.name for c in candidates] == ["win"]


class TestNonDictMcpServers:
    """Structurally-valid JSON whose ``mcpServers`` is a list/str/number is
    truthy, so an ``(config.get("mcpServers") or {})`` guard passes it
    straight to ``.items()`` — AttributeError. The module docstring contract
    treats unreadable configs as "no candidates (silent)"; a wrong-typed
    ``mcpServers`` (another tool's schema, or a hand-edit) must behave like
    missing, not crash ``mms import`` / ``mms host status`` / ``mms host
    scan`` / ``mms host sync``. ``scan_codex`` already guards with
    ``isinstance``; these pin the four JSON scanner sites to the same
    behavior.
    """

    BAD_VALUES = pytest.mark.parametrize(
        "bad", [["entry"], "string", 7], ids=["list", "str", "int"]
    )

    @BAD_VALUES
    def test_claude_code_user_scope_returns_empty(self, sandbox_home, tmp_path, bad):
        (sandbox_home / ".claude.json").write_text(
            json.dumps({"mcpServers": bad}), encoding="utf-8"
        )
        assert ih.scan_claude_code(tmp_path) == []

    @BAD_VALUES
    def test_claude_code_project_scope_returns_empty(self, sandbox_home, tmp_path, bad):
        cwd = tmp_path / "proj"
        cwd.mkdir()
        cfg = {"projects": {str(cwd): {"mcpServers": bad}}}
        (sandbox_home / ".claude.json").write_text(json.dumps(cfg), encoding="utf-8")
        assert ih.scan_claude_code(cwd) == []

    @BAD_VALUES
    def test_dot_mcp_json_returns_empty(self, sandbox_home, tmp_path, bad):
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": bad}), encoding="utf-8")
        assert ih.scan_claude_code(tmp_path) == []

    @BAD_VALUES
    def test_cursor_returns_empty(self, sandbox_home, tmp_path, bad):
        cwd = tmp_path / "proj"
        cwd.mkdir()
        cursor_dir = sandbox_home / ".cursor"
        cursor_dir.mkdir()
        (cursor_dir / "mcp.json").write_text(json.dumps({"mcpServers": bad}), encoding="utf-8")
        assert ih.scan_cursor(cwd) == []

    @BAD_VALUES
    def test_claude_desktop_returns_empty(self, monkeypatch, tmp_path, bad):
        monkeypatch.setattr(sys, "platform", "darwin")
        sandbox_path = tmp_path / "claude_desktop_config.json"
        sandbox_path.write_text(json.dumps({"mcpServers": bad}), encoding="utf-8")
        monkeypatch.setattr(ih, "_desktop_config_path", lambda: sandbox_path)
        assert ih.scan_claude_desktop(tmp_path) == []

    def test_bad_user_scope_does_not_block_other_scopes(self, sandbox_home, tmp_path):
        """A wrong-typed ``mcpServers`` skips only that scope — the scan
        continues to the next source (here ``.mcp.json``) instead of
        aborting the whole scanner."""
        (sandbox_home / ".claude.json").write_text(
            json.dumps({"mcpServers": ["bad"]}), encoding="utf-8"
        )
        (tmp_path / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"local-tool": {"command": "echo"}}}),
            encoding="utf-8",
        )
        candidates = ih.scan_claude_code(tmp_path)
        assert [c.name for c in candidates] == ["local-tool"]


# ---------------------------------------------------------------------------
# discover() façade
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_single_host(self, sandbox_home, tmp_path):
        (sandbox_home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"x": {"command": "echo"}}}), encoding="utf-8"
        )
        cands = ih.discover("claude-code", tmp_path)
        assert len(cands) == 1

    def test_unknown_host_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown host"):
            ih.discover("vim", tmp_path)

    def test_all_iterates_every_host(self, sandbox_home, tmp_path):
        # Seed each host with one distinct entry.
        (sandbox_home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"cc-one": {"command": "echo"}}}), encoding="utf-8"
        )
        cursor_dir = sandbox_home / ".cursor"
        cursor_dir.mkdir()
        (cursor_dir / "mcp.json").write_text(
            json.dumps({"mcpServers": {"cu-one": {"command": "echo"}}}), encoding="utf-8"
        )
        codex_dir = sandbox_home / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.cx-one]\ncommand = "echo"\n', encoding="utf-8"
        )

        cands = ih.discover("all", tmp_path)
        names = {c.name for c in cands}
        assert {"cc-one", "cu-one", "cx-one"}.issubset(names)
