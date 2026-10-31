"""Claude Code plugin MCP servers reported as bypassing STM (#1108).

Report-only: these servers are never import candidates, and no field of a
server entry (command, args, env, headers, url) is printed anywhere. The probe
is the ``claude plugin list --json`` call behind the ``_run_claude_mcp`` seam;
every test scripts that seam, so none depends on the plugins of the machine
running it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from helpers import GIT_FILE_LAYOUTS, set_home, skip_if_inside_git
from memtomem_stm.cli import proxy as proxy_mod
from memtomem_stm.cli.proxy import cli

# Stands for the directory the probe runs in: the ``probe`` fixture replaces a
# ``projectPath`` equal to this with that directory, so a record carrying it is
# an install belonging to the project under test (#1270).
HERE = "<probe cwd>"

# The record `claude plugin list --json` returned for the Firebase plugin,
# enabled at local scope in a scratch directory (2026-10-29, Claude Code
# 2.1.312), with ``projectPath`` set to the directory under test; the recorded
# value named another directory. ``projectEnabled`` is false while ``enabled``
# is true (it tracks the project's shared settings only); installPath is
# shortened.
FIREBASE = {
    "id": "firebase@firebase",
    "version": "1.0.0",
    "scope": "local",
    "enabled": True,
    "installPath": "/home/u/.claude/plugins/cache/firebase/firebase/1.0.0",
    "projectPath": HERE,
    "projectEnabled": False,
    "mcpServers": {
        "firebase": {
            "command": "npx",
            "args": ["-y", "firebase-tools", "mcp", "--dir", "."],
            "env": {"IS_FIREBASE_MCP": "true"},
        }
    },
}

# Values that must never reach any output stream.
CANARIES = ("cnry-arg", "cnry-user", "cnry-pass", "cnry-query", "cnry-env", "cnry-header")


def _leaky_plugin() -> dict:
    return {
        "id": "leaky@market",
        "scope": "user",
        "enabled": True,
        "mcpServers": {
            "stdio-srv": {
                "command": "node",
                "args": ["server.js", "--token", "cnry-arg"],
                "env": {"API_KEY": "cnry-env"},
            },
            "http-srv": {
                "type": "http",
                "url": "https://cnry-user:cnry-pass@example.com/mcp?token=cnry-query",
                "headers": {"Authorization": "Bearer cnry-header"},
            },
        },
    }


def _result(stdout: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


@pytest.fixture
def probe(monkeypatch):
    """Script ``claude plugin list --json``: set ``state["out"]`` to the reply.

    ``out`` is a list of records (JSON-encoded), a raw string, a result object
    or an exception to raise. Calls are recorded with their ``cwd``.
    """
    state: dict = {"out": [], "calls": []}

    def fake_run(cmd, timeout=5, cwd=None):
        state["calls"].append({"cmd": list(cmd), "cwd": cwd})
        out = state["out"]
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, SimpleNamespace):
            return out
        if isinstance(out, str):
            return _result(out)
        records = [
            {**r, "projectPath": cwd} if isinstance(r, dict) and r.get("projectPath") == HERE else r
            for r in out
        ]
        return _result(json.dumps(records))

    # A git hook exports these, which turns the project match off (#1270).
    for name in proxy_mod._GIT_LOCATION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(proxy_mod.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(proxy_mod, "_run_claude_mcp", fake_run)
    return state


pytestmark = pytest.mark.real_plugin_discovery


class TestPluginBypassServers:
    def test_firebase_record_is_one_row(self, probe, tmp_path):
        probe["out"] = [FIREBASE]

        rows = proxy_mod._plugin_bypass_servers(tmp_path)

        assert [row.as_json() for row in rows] == [
            {
                "plugin_id": "firebase@firebase",
                "server": "firebase",
                "runtime_name": "plugin:firebase:firebase",
                "transport": "stdio",
                "scope": "local",
            }
        ]

    def test_probe_runs_in_the_resolved_cwd(self, probe, tmp_path):
        probe["out"] = []
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)

        proxy_mod._plugin_bypass_servers(link)

        assert probe["calls"] == [
            {"cmd": ["claude", "plugin", "list", "--json"], "cwd": str(tmp_path.resolve())}
        ]

    def test_disabled_record_is_dropped_whatever_its_project(self, probe, tmp_path):
        disabled = {**FIREBASE, "id": "off@m", "enabled": False}
        probe["out"] = [FIREBASE, disabled]

        rows = proxy_mod._plugin_bypass_servers(tmp_path)

        assert [row.plugin_id for row in rows] == ["firebase@firebase"]

    def test_repeated_record_is_one_row_and_shared_keys_are_two(self, probe, tmp_path):
        other = {**FIREBASE, "id": "other@m", "scope": "user"}
        probe["out"] = [FIREBASE, {**FIREBASE, "scope": "project"}, other]

        rows = proxy_mod._plugin_bypass_servers(tmp_path)

        assert [(row.runtime_name, row.scope) for row in rows] == [
            ("plugin:firebase:firebase", "local"),
            ("plugin:other:firebase", "user"),
        ]

    def test_http_and_sse_transports(self, probe, tmp_path):
        probe["out"] = [
            {
                "id": "web@m",
                "scope": "user",
                "enabled": True,
                "mcpServers": {
                    "h": {"type": "http", "url": "https://example.com/mcp"},
                    "s": {"type": "sse", "url": "https://example.com/sse"},
                },
            }
        ]

        rows = proxy_mod._plugin_bypass_servers(tmp_path)

        assert {row.server: row.transport for row in rows} == {
            "h": "streamable_http",
            "s": "sse",
        }

    def test_stms_own_ltm_server_is_not_reported(self, probe, tmp_path):
        probe["out"] = [
            {
                "id": "memtomem@memtomem",
                "scope": "user",
                "enabled": True,
                "mcpServers": {
                    "memtomem": {
                        "command": "uvx",
                        "args": ["--from", "memtomem[onnx]==0.6.1", "memtomem-server"],
                    }
                },
            }
        ]

        assert proxy_mod._plugin_bypass_servers(tmp_path) == []

    @pytest.mark.parametrize(
        "scope",
        [{"headers": {"Authorization": "Bearer cnry-scope"}}, 1, None, "global", "user\n"],
        ids=["dict", "int", "missing", "unsupported", "control-char"],
    )
    def test_unsupported_scope_reads_unknown(self, probe, tmp_path, scope, capsys):
        record = {**FIREBASE, "scope": scope}
        if scope is None:
            del record["scope"]
        probe["out"] = [record]

        rows = proxy_mod._plugin_bypass_servers(tmp_path)
        proxy_mod._echo_plugin_bypass(rows, err=False)

        assert [row.scope for row in rows] == ["unknown"]
        assert "cnry-scope" not in json.dumps([row.as_json() for row in rows])
        assert "cnry-scope" not in capsys.readouterr().out

    def test_malformed_entries_are_skipped_without_hiding_valid_ones(self, probe, tmp_path):
        probe["out"] = [
            "not a record",
            {"id": 7, "enabled": True, "mcpServers": {"x": {"command": "a"}}},
            {"id": "ctl\x1b@m", "enabled": True, "mcpServers": {"x": {"command": "a"}}},
            {"id": "list@m", "enabled": True, "mcpServers": ["x"]},
            {
                "id": "bad@m",
                "enabled": True,
                "mcpServers": {
                    "type-int": {"type": 1, "command": "node"},
                    "command-int": {"command": 1},
                    "args-int": {"command": "node", "args": [1, None]},
                    "env-list": {"command": "node", "env": ["A=b"]},
                    "not-dict": "node",
                    "no-endpoint": {"type": "http"},
                    "surrogate-\udcff": {"command": "node"},
                },
            },
            FIREBASE,
        ]

        rows = proxy_mod._plugin_bypass_servers(tmp_path)

        assert sorted(row.runtime_name for row in rows) == [
            "plugin:bad:args-int",
            "plugin:bad:env-list",
            "plugin:firebase:firebase",
        ]

    @pytest.mark.parametrize(
        ("out", "note"),
        [
            (_result(returncode=1), "exited 1"),
            (subprocess.TimeoutExpired(["claude"], 5), "TimeoutExpired"),
            (FileNotFoundError(2, "No such file"), "FileNotFoundError"),
            ("{not json", "did not return a JSON list"),
            (json.dumps({"id": "x"}), "did not return a JSON list"),
        ],
        ids=["rc1", "timeout", "oserror", "garbage", "not-a-list"],
    )
    def test_cli_failures_fail_open_with_a_note(self, probe, tmp_path, capsys, out, note):
        probe["out"] = out

        assert proxy_mod._plugin_bypass_servers(tmp_path) == []

        captured = capsys.readouterr()
        assert captured.out == ""
        assert note in captured.err
        assert "plugin MCP servers are not reported" in captured.err

    def test_decoder_recursion_fails_open_with_a_note(self, probe, tmp_path, capsys, monkeypatch):
        probe["out"] = "[]"

        def too_deep(*args, **kwargs):
            raise RecursionError("injected decoder recursion limit")

        monkeypatch.setattr(json, "loads", too_deep)
        assert proxy_mod._plugin_bypass_servers(tmp_path) == []
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "did not return a JSON list" in captured.err
        assert "plugin MCP servers are not reported" in captured.err

    def test_deep_list_has_no_plugin_records(self, probe, tmp_path, capsys):
        # Either the decoder rejects it or it parses a list without records.
        probe["out"] = "[" * 100_000 + "]" * 100_000
        assert proxy_mod._plugin_bypass_servers(tmp_path) == []

        captured = capsys.readouterr()
        assert captured.out == ""
        assert not captured.err or "did not return a JSON list" in captured.err

    def test_no_claude_on_path_is_silent(self, probe, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(proxy_mod.shutil, "which", lambda name: None)

        assert proxy_mod._plugin_bypass_servers(tmp_path) == []

        assert probe["calls"] == []
        assert capsys.readouterr() == ("", "")


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """Directories shaped like the ones #1270 was measured in (Claude Code 2.1.314).

    Only ``.git`` entries matter to the project match: a directory marks a
    repository root, a file marks a linked worktree. ``where(name)`` gives the
    absolute path of a layout entry.
    """
    if sys.platform == "win32":
        pytest.skip("the project match is off on Windows (test_windows_keeps_everything)")
    for name in proxy_mod._GIT_LOCATION_ENV:
        monkeypatch.delenv(name, raising=False)
    base = tmp_path.resolve()
    if any(os.path.lexists(p / ".git") for p in (base, *base.parents)):
        pytest.skip("the temporary directory sits inside a git repository")
    for repo in ("A", "D", "E", "F", "G", "H", "A/nested", "outer/N"):
        (tmp_path / repo / ".git").mkdir(parents=True)
    for sub in ("A/sub", "D/sub", "G/sub", "B/sub", "HW"):
        (tmp_path / sub).mkdir(parents=True)
    (tmp_path / "HW" / ".git").write_text(
        f"gitdir: {tmp_path / 'H' / '.git' / 'worktrees' / 'HW'}\n", encoding="utf-8"
    )
    (tmp_path / "L").symlink_to(tmp_path / "A", target_is_directory=True)
    return lambda name: tmp_path.resolve() / name


def _deny_lstat(monkeypatch, denied) -> None:
    """Make ``Path.lstat`` raise ``PermissionError`` for paths ``denied`` accepts."""
    real_lstat = Path.lstat

    def lstat(self):
        if denied(self):
            raise PermissionError(13, "Permission denied", str(self))
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", lstat)


def _record(project_path: object, scope: object = "local", plugin: str = "firebase") -> dict:
    return {**FIREBASE, "id": f"{plugin}@m", "scope": scope, "projectPath": project_path}


class TestProjectMatch:
    """An enabled install that belongs to another repository runs nothing here (#1270)."""

    def _kept(self, probe, cwd: Path, *records: dict) -> list[str]:
        probe["out"] = list(records)
        return [row.plugin_id for row in proxy_mod._plugin_bypass_servers(cwd)]

    @pytest.mark.parametrize(
        ("cwd", "installed", "scope", "runs"),
        [
            # Each row: where the probe ran, where the install was made from,
            # and whether `claude mcp list` there showed the server connected.
            ("A", "A", "local", True),
            ("A/sub", "A", "local", True),
            ("L", "A", "local", True),
            ("B", "B", "local", True),
            ("G", "G/sub", "local", True),
            ("G/sub", "G/sub", "local", True),
            ("H", "HW", "local", True),
            ("HW", "H", "local", True),
            ("E", "E", "project", True),
            ("D", "A", "local", False),
            ("D/sub", "B", "local", False),
            ("D", "G/sub", "local", False),
            ("F", "E", "project", False),
        ],
    )
    def test_measured_rows(self, probe, layout, cwd, installed, scope, runs):
        kept = self._kept(probe, layout(cwd), _record(str(layout(installed)), scope))

        assert kept == (["firebase@m"] if runs else [])

    def test_a_linked_worktree_install_is_kept_from_anywhere(self, probe, layout):
        # Not derivable without git, so it over-reports rather than guess.
        assert self._kept(probe, layout("D"), _record(str(layout("HW")))) == ["firebase@m"]

    def test_a_linked_worktree_cwd_keeps_an_unrelated_install(self, probe, layout):
        # HW belongs to H, not A: kept only because a worktree's root is unknown.
        assert self._kept(probe, layout("HW"), _record(str(layout("A")))) == ["firebase@m"]

    @pytest.mark.parametrize(
        ("cwd", "installed"),
        [("outer/N", "outer"), ("A", "A/nested"), ("A/nested", "A/sub")],
        ids=["install-around-the-repo", "nested-repo-inside", "outer-repo-of-a-nested-cwd"],
    )
    def test_nesting_with_unequal_roots_is_kept(self, probe, layout, cwd, installed):
        assert self._kept(probe, layout(cwd), _record(str(layout(installed)))) == ["firebase@m"]

    def test_install_spelled_through_a_symlink_is_kept(self, probe, layout):
        assert self._kept(probe, layout("A"), _record(str(layout("L")))) == ["firebase@m"]

    def test_install_spelled_in_another_case_is_kept(self, probe, layout):
        if not Path(str(layout("A")).lower()).exists():
            pytest.skip("case-sensitive filesystem")
        assert self._kept(probe, layout("A"), _record(str(layout("A")).lower())) == ["firebase@m"]

    @pytest.mark.parametrize(
        ("project_path", "scope"),
        [
            ("{D}", "user"),
            ("{D}", "global"),
            ("{D}", None),
            (7, "local"),
            ("", "local"),
            ("D", "local"),
            ("{missing}", "local"),
            ("{loop}", "local"),
            ("{D}\x00x", "local"),
        ],
        ids=[
            "user-scope",
            "unknown-scope",
            "no-scope",
            "not-a-string",
            "empty",
            "relative",
            "missing-dir",
            "symlink-loop",
            "nul",
        ],
    )
    def test_unsure_records_are_kept(self, probe, layout, tmp_path, project_path, scope):
        (tmp_path / "loop").symlink_to(tmp_path / "loop")
        if isinstance(project_path, str):
            project_path = project_path.format(
                D=layout("D"), missing=layout("gone"), loop=layout("loop")
            )

        assert self._kept(probe, layout("A"), _record(project_path, scope)) == ["firebase@m"]

    def test_git_location_in_the_environment_keeps_everything(self, probe, layout, monkeypatch):
        monkeypatch.setenv("GIT_DIR", str(layout("D") / ".git"))

        assert self._kept(probe, layout("A"), _record(str(layout("D")))) == ["firebase@m"]

    @pytest.mark.parametrize("side", ["cwd", "install"])
    def test_a_probe_error_on_either_side_keeps_the_record(self, probe, layout, monkeypatch, side):
        blocked = layout("A" if side == "cwd" else "D") / ".git"
        _deny_lstat(monkeypatch, lambda p: p == blocked)

        assert self._kept(probe, layout("A"), _record(str(layout("D")))) == ["firebase@m"]

    def test_windows_keeps_everything(self, probe, tmp_path, monkeypatch):
        # Plain directories, not ``layout``, so this also runs on Windows itself.
        for repo in ("A", "D"):
            (tmp_path / repo / ".git").mkdir(parents=True)
        monkeypatch.setattr(sys, "platform", "win32")

        kept = self._kept(probe, tmp_path / "A", _record(str((tmp_path / "D").resolve())))

        assert kept == ["firebase@m"]

    @pytest.mark.parametrize("foreign_first", [True, False], ids=["foreign-first", "local-first"])
    def test_records_are_filtered_before_they_are_merged(self, probe, layout, foreign_first):
        servers = {"shared": {"command": "npx"}}
        foreign = {
            **_record(str(layout("D")), "project"),
            "mcpServers": {**servers, "old-only": {"command": "npx"}},
        }
        local = {**_record(str(layout("A")), "local"), "mcpServers": servers}
        probe["out"] = [foreign, local] if foreign_first else [local, foreign]

        rows = proxy_mod._plugin_bypass_servers(layout("A"))

        assert [(row.server, row.scope) for row in rows] == [("shared", "local")]


class TestClaudeProjectRoot:
    @pytest.mark.parametrize(
        ("path", "root"),
        [("A", "A"), ("A/sub", "A"), ("L", "A"), ("B/sub", "B/sub"), ("A/nested", "A/nested")],
    )
    def test_root(self, layout, path, root):
        assert proxy_mod._claude_project_root(layout(path)) == layout(root)

    @pytest.mark.parametrize("path", ["HW", "gone"], ids=["linked-worktree", "missing"])
    def test_unsure_is_none(self, layout, path):
        assert proxy_mod._claude_project_root(layout(path)) is None

    @pytest.mark.parametrize("target", ["A/.git", "nowhere"], ids=["to-a-git-dir", "dangling"])
    def test_symlinked_git_marker_is_none(self, layout, target):
        (layout("B/sub") / ".git").symlink_to(layout(target))

        assert proxy_mod._claude_project_root(layout("B/sub")) is None

    @pytest.mark.parametrize("path", ["HW", "B/sub"], ids=["worktree", "non-git"])
    def test_an_unreadable_marker_is_none(self, layout, monkeypatch, path):
        # Python 3.14's Path.is_dir() / exists() read this as "no .git", which
        # would turn the worktree into a known non-git root.
        _deny_lstat(monkeypatch, lambda p: p.name == ".git")

        assert proxy_mod._claude_project_root(layout(path)) is None

    @pytest.mark.parametrize("name", proxy_mod._GIT_LOCATION_ENV)
    @pytest.mark.parametrize("value", ["", "x"], ids=["empty", "set"])
    def test_git_location_in_the_environment_is_none(self, layout, monkeypatch, name, value):
        monkeypatch.setenv(name, value)

        assert proxy_mod._claude_project_root(layout("A")) is None


class TestSameOrInside:
    def test_unreadable_base_counts_as_inside(self, tmp_path):
        assert proxy_mod._same_or_inside(tmp_path, tmp_path / "gone") is True

    def test_identity_not_spelling(self, tmp_path):
        (tmp_path / "real" / "sub").mkdir(parents=True)
        (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)

        assert proxy_mod._same_or_inside(tmp_path / "real" / "sub", tmp_path / "link") is True
        assert proxy_mod._same_or_inside(tmp_path / "real", tmp_path / "real" / "sub") is False


class TestEchoPluginBypass:
    def test_no_server_field_value_reaches_the_report(self, probe, tmp_path, capsys):
        probe["out"] = [_leaky_plugin(), FIREBASE]

        rows = proxy_mod._plugin_bypass_servers(tmp_path)
        proxy_mod._echo_plugin_bypass(rows, err=False)
        text = capsys.readouterr().out
        document = json.dumps([row.as_json() for row in rows])

        assert "plugin:leaky:http-srv (streamable_http, user)" in text
        assert "plugin:leaky:stdio-srv (stdio, user)" in text
        assert "plugin:firebase:firebase (stdio, local)" in text
        for canary in CANARIES:
            assert canary not in text
            assert canary not in document
        for field_value in ("server.js", "example.com", "API_KEY", "Authorization", "npx"):
            assert field_value not in text

    def test_nothing_printed_without_rows(self, capsys):
        proxy_mod._echo_plugin_bypass([], err=False)

        assert capsys.readouterr() == ("", "")

    @pytest.mark.parametrize("platform", ["linux", "win32"])
    def test_disable_command_is_shell_quoted(self, monkeypatch, capsys, platform):
        monkeypatch.setattr(sys, "platform", platform)
        row = proxy_mod._PluginBypass("evil;rm -rf ~&x@m", "s", "stdio", "user")

        proxy_mod._echo_plugin_bypass([row], err=False)

        expected = proxy_mod._shell_join(["claude", "plugin", "disable", "evil;rm -rf ~&x@m"])
        assert f"or turn the plugin off: {expected}" in capsys.readouterr().out
        assert expected != "claude plugin disable evil;rm -rf ~&x@m"


# Firebase with a second server, so a disable can be shown to drop exactly one.
FIREBASE_TWO = {
    **FIREBASE,
    "mcpServers": {**FIREBASE["mcpServers"], "emulator": {"command": "npx", "args": []}},
}


def _write_claude_json(home: Path, data: object) -> None:
    text = data if isinstance(data, str) else json.dumps(data)
    (home / ".claude.json").write_text(text, encoding="utf-8")


def _project_key(path: Path) -> str:
    """How ``/mcp`` spells a project directory as a key: NFC, with ``/``
    separators on Windows too (#1271). Built here, not by the resolver under
    test."""
    return unicodedata.normalize("NFC", path.resolve().as_posix())


def _disabled_for(cwd: Path, names: object) -> dict:
    return {"projects": {_project_key(cwd): {"disabledMcpServers": names}}}


def _disabled_under(paths: list[Path], names: list[str]) -> dict:
    return {"projects": {_project_key(p): {"disabledMcpServers": names} for p in paths}}


class TestMcpDisabledServers:
    """A server disabled in ``/mcp`` for this directory is not reported (#1268)."""

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        set_home(monkeypatch, home)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        return home

    def _names(self, cwd: Path) -> list[str]:
        return [row.runtime_name for row in proxy_mod._plugin_bypass_servers(cwd)]

    def test_disabled_server_is_dropped_and_its_sibling_kept(self, probe, home, tmp_path):
        # The string `/mcp` wrote for a disabled firebase server (2026-10-30,
        # Claude Code 2.1.314); `claude plugin list --json` was unchanged.
        probe["out"] = [FIREBASE_TWO]
        _write_claude_json(home, _disabled_for(tmp_path, ["plugin:firebase:firebase"]))

        assert self._names(tmp_path) == ["plugin:firebase:emulator"]

    def test_without_a_disable_both_are_reported(self, probe, home, tmp_path):
        probe["out"] = [FIREBASE_TWO]
        _write_claude_json(home, _disabled_for(tmp_path, []))

        assert self._names(tmp_path) == ["plugin:firebase:emulator", "plugin:firebase:firebase"]

    def test_another_projects_disable_does_not_apply(self, probe, home, tmp_path):
        probe["out"] = [FIREBASE_TWO]
        other = tmp_path / "other"
        other.mkdir()
        _write_claude_json(home, _disabled_for(other, ["plugin:firebase:firebase"]))

        assert self._names(tmp_path) == ["plugin:firebase:emulator", "plugin:firebase:firebase"]

    def test_symlinked_cwd_reads_the_resolved_key(self, probe, home, tmp_path):
        probe["out"] = [FIREBASE_TWO]
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        _write_claude_json(home, _disabled_for(real, ["plugin:firebase:firebase"]))

        assert self._names(link) == ["plugin:firebase:emulator"]

    def test_bare_server_key_does_not_disable(self, probe, home, tmp_path):
        probe["out"] = [FIREBASE_TWO]
        _write_claude_json(home, _disabled_for(tmp_path, ["firebase", "PLUGIN:FIREBASE:FIREBASE"]))

        assert self._names(tmp_path) == ["plugin:firebase:emulator", "plugin:firebase:firebase"]

    def test_claude_config_dir_reads_no_disables(self, probe, home, tmp_path, monkeypatch):
        # Claude Code reads its config from the override, so a disable left in
        # ~/.claude.json may be stale: it must not hide a running server.
        probe["out"] = [FIREBASE_TWO]
        _write_claude_json(home, _disabled_for(tmp_path, ["plugin:firebase:firebase"]))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))

        assert self._names(tmp_path) == ["plugin:firebase:emulator", "plugin:firebase:firebase"]

    def test_subdirectory_reads_the_repository_root_entry(self, probe, home, tmp_path):
        # Started in <repo>/src, /mcp filed the disable under <repo> (2026-10-31,
        # Claude Code 2.1.316).
        skip_if_inside_git(tmp_path.resolve())
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "src").mkdir()
        probe["out"] = [FIREBASE_TWO]
        _write_claude_json(home, _disabled_for(repo, ["plugin:firebase:firebase"]))

        assert self._names(repo / "src") == ["plugin:firebase:emulator"]

    def test_nfd_repository_reads_the_nfc_entry(self, probe, home, tmp_path):
        # /mcp files a directory stored in NFD under its NFC spelling
        # (2026-10-31, Claude Code 2.1.316).
        skip_if_inside_git(tmp_path.resolve())
        name = unicodedata.normalize("NFD", "한글repo")
        repo = tmp_path / "nfd" / name
        (repo / ".git").mkdir(parents=True)
        (repo / "src").mkdir()
        if os.listdir(tmp_path / "nfd") != [name]:
            pytest.skip("the filesystem does not keep NFD names")
        probe["out"] = [FIREBASE_TWO]
        _write_claude_json(home, _disabled_for(repo, ["plugin:firebase:firebase"]))

        assert self._names(repo / "src") == ["plugin:firebase:emulator"]

    def test_stale_subdirectory_entry_hides_nothing(self, probe, home, tmp_path):
        # Claude Code never reads an entry keyed by a subdirectory of a
        # repository, so a disable left there is stale.
        skip_if_inside_git(tmp_path.resolve())
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "src").mkdir()
        probe["out"] = [FIREBASE_TWO]
        _write_claude_json(home, _disabled_for(repo / "src", ["plugin:firebase:firebase"]))

        assert self._names(repo / "src") == ["plugin:firebase:emulator", "plugin:firebase:firebase"]

    @pytest.mark.parametrize("layout", sorted(GIT_FILE_LAYOUTS))
    def test_git_file_or_symlink_hides_nothing(self, probe, home, tmp_path, layout):
        # No key is given for these layouts, so a disable under the start
        # directory, its repository root or the main checkout hides nothing.
        skip_if_inside_git(tmp_path.resolve())
        start, main = GIT_FILE_LAYOUTS[layout](tmp_path)
        marked = next(p for p in (start, *start.parents) if os.path.lexists(p / ".git"))
        probe["out"] = [FIREBASE_TWO]
        _write_claude_json(
            home, _disabled_under([start, marked, main], ["plugin:firebase:firebase"])
        )

        assert self._names(start) == ["plugin:firebase:emulator", "plugin:firebase:firebase"]

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows key form")
    def test_backslash_key_hides_nothing_on_windows(self, probe, home, tmp_path):
        probe["out"] = [FIREBASE_TWO]
        key = str(tmp_path.resolve())
        _write_claude_json(
            home, {"projects": {key: {"disabledMcpServers": ["plugin:firebase:firebase"]}}}
        )

        assert self._names(tmp_path) == ["plugin:firebase:emulator", "plugin:firebase:firebase"]

    def test_non_string_members_are_ignored_not_fatal(self, probe, home, tmp_path):
        probe["out"] = [FIREBASE_TWO]
        _write_claude_json(home, _disabled_for(tmp_path, ["plugin:firebase:firebase", None, 3]))

        assert self._names(tmp_path) == ["plugin:firebase:emulator"]

    @pytest.mark.parametrize(
        "content",
        [
            None,
            "{not json",
            "[]",
            {"projects": []},
            {"projects": {"__cwd__": []}},
            {"projects": {"__cwd__": {"disabledMcpServers": "plugin:firebase:firebase"}}},
            # Whether decoding fails or yields a nested list, reporting must not abort.
            '{"projects": ' + "[" * 100_000 + "]" * 100_000 + "}",
        ],
        ids=["missing", "invalid", "root-list", "projects-list", "entry-list", "names-str", "deep"],
    )
    def test_unreadable_disable_data_hides_nothing(self, probe, home, tmp_path, content):
        probe["out"] = [FIREBASE_TWO]
        if content is not None:
            if isinstance(content, dict):
                content = json.dumps(content).replace(
                    '"__cwd__"', json.dumps(_project_key(tmp_path))
                )
            _write_claude_json(home, content)

        assert self._names(tmp_path) == ["plugin:firebase:emulator", "plugin:firebase:firebase"]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    set_home(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setenv("MMS_NO_TUI", "1")
    monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: [])
    return {"home": tmp_path, "cwd": proj}


def _config(home: Path, servers: dict | None = None) -> Path:
    path = home / "stm_proxy.json"
    path.write_text(json.dumps({"upstream_servers": servers or {}}), encoding="utf-8")
    return path


class TestCommandsReport:
    @pytest.mark.parametrize(
        "argv",
        [
            ["import", "--from", "claude-code", "--apply"],
            ["host", "status", "--json"],
            ["host", "sync", "--json"],
            ["host", "scan", "--from", "claude-code", "--json"],
        ],
    )
    def test_registry_commands_report_plugins_without_importing(self, sandbox, probe, argv):
        probe["out"] = [_leaky_plugin()]
        result = CliRunner().invoke(cli, argv)
        assert result.exit_code == 0, result.output
        if "--json" in argv:
            json.loads(result.stdout)
        assert "plugin:leaky:http-srv" in result.stderr
        assert "bypass STM" in result.stderr
        for canary in CANARIES:
            assert canary not in result.output
        assert not (sandbox["home"] / ".mms/registry.toml").exists()

    @pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
    def test_doctor_warns_without_leaking_or_writing(self, sandbox, probe, monkeypatch, as_json):
        probe["out"] = [_leaky_plugin()]
        monkeypatch.setenv("MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND", "__missing_ltm__")
        config = _config(sandbox["home"])
        before = config.read_bytes()

        result = CliRunner().invoke(
            cli, ["doctor", "--config", str(config), *(["--json"] if as_json else [])]
        )

        assert result.exit_code == 0, result.output
        assert config.read_bytes() == before
        plugin_calls = [
            c for c in probe["calls"] if c["cmd"] == ["claude", "plugin", "list", "--json"]
        ]
        assert len(plugin_calls) == 1
        assert plugin_calls[0]["cwd"] == str(sandbox["cwd"].resolve())
        if as_json:
            report = json.loads(result.stdout)
            row = next(c for c in report["checks"] if c["id"] == "plugin_bypass")
            assert row["status"] == "WARN"
            assert "2 Claude Code plugin MCP server(s) bypass STM" in row["detail"]
            assert "mms add ... --validate" in row["next_action"]
        else:
            assert "2 Claude Code plugin MCP server(s) bypass STM" in result.stdout
        assert "plugin:leaky:http-srv" in result.stdout
        for canary in CANARIES:
            assert canary not in result.output

    @pytest.mark.parametrize("out", [[], subprocess.TimeoutExpired(["claude"], 5)])
    def test_doctor_empty_or_failed_probe_never_claims_pass(self, sandbox, probe, monkeypatch, out):
        probe["out"] = out
        monkeypatch.setenv("MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND", "__missing_ltm__")
        config = _config(sandbox["home"])
        result = CliRunner().invoke(cli, ["doctor", "--config", str(config), "--json"])
        assert result.exit_code == 0, result.output
        assert all(c["id"] != "plugin_bypass" for c in json.loads(result.stdout)["checks"])
        if isinstance(out, BaseException):
            assert "plugin MCP servers are not reported" in result.stderr

    def test_doctor_invalid_config_skips_plugin_probe(self, sandbox, probe):
        config = _config(sandbox["home"])
        config.write_text("{", encoding="utf-8")
        result = CliRunner().invoke(cli, ["doctor", "--config", str(config), "--json"])
        assert result.exit_code == 1
        assert probe["calls"] == []
        assert all(c["id"] != "plugin_bypass" for c in json.loads(result.stdout)["checks"])

    def test_add_from_clients_with_only_plugin_servers(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = _config(sandbox["home"])

        result = CliRunner().invoke(cli, ["add", "--from-clients", "--config", str(config)])

        assert result.exit_code == 0, result.output
        out = result.stdout
        assert "1 MCP server(s) registered by Claude Code plugins bypass STM" in out
        assert "plugin:firebase:firebase (stdio, local)" in out
        assert "No importable MCP servers found" in out
        assert out.index("bypass STM") < out.index("No importable MCP servers found")

    def test_add_from_clients_without_plugin_servers_keeps_its_message(self, sandbox, probe):
        probe["out"] = []
        config = _config(sandbox["home"])

        result = CliRunner().invoke(cli, ["add", "--from-clients", "--config", str(config)])

        assert result.exit_code == 0, result.output
        assert "No MCP servers found in Claude Desktop" in result.stdout
        assert "bypass STM" not in result.output

    def test_add_from_clients_json_keeps_stdout_one_document(self, sandbox, probe):
        probe["out"] = [_leaky_plugin()]
        config = _config(sandbox["home"])

        result = CliRunner().invoke(
            cli, ["add", "--from-clients", "--all", "--json", "--config", str(config)]
        )

        assert result.exit_code == 0, result.output
        document = json.loads(result.stdout)
        assert [row["runtime_name"] for row in document["plugin_bypass"]] == [
            "plugin:leaky:http-srv",
            "plugin:leaky:stdio-srv",
        ]
        assert "bypass STM" in result.stderr
        for canary in CANARIES:
            assert canary not in result.output

    def test_add_from_clients_json_omits_a_server_disabled_in_mcp(self, sandbox, probe):
        probe["out"] = [FIREBASE_TWO]
        _write_claude_json(
            sandbox["home"], _disabled_for(sandbox["cwd"], ["plugin:firebase:firebase"])
        )
        config = _config(sandbox["home"])

        result = CliRunner().invoke(
            cli, ["add", "--from-clients", "--all", "--json", "--config", str(config)]
        )

        assert result.exit_code == 0, result.output
        document = json.loads(result.stdout)
        assert [row["runtime_name"] for row in document["plugin_bypass"]] == [
            "plugin:firebase:emulator"
        ]

    def test_prune_without_upstreams_survives_a_deeply_nested_claude_json(self, sandbox, probe):
        # prune returns before discovery when nothing is configured, so the
        # disable read is the only parse of ~/.claude.json on this path.
        probe["out"] = [FIREBASE]
        _write_claude_json(sandbox["home"], '{"projects": ' + "[" * 100_000 + "]" * 100_000 + "}")
        config = _config(sandbox["home"])

        result = CliRunner().invoke(cli, ["prune", "--all", "--json", "--config", str(config)])

        assert result.exit_code == 0, result.output
        document = json.loads(result.stdout)
        assert [row["runtime_name"] for row in document["plugin_bypass"]] == [
            "plugin:firebase:firebase"
        ]

    @pytest.mark.skipif(sys.platform == "win32", reason="the project match is off on Windows")
    def test_add_from_clients_json_omits_an_install_from_another_project(self, sandbox, probe):
        elsewhere = sandbox["home"] / "elsewhere"
        elsewhere.mkdir()
        probe["out"] = [{**FIREBASE, "projectPath": str(elsewhere)}]
        config = _config(sandbox["home"])

        result = CliRunner().invoke(
            cli, ["add", "--from-clients", "--all", "--json", "--config", str(config)]
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["plugin_bypass"] == []
        assert "bypass STM" not in result.stderr

    def test_select_naming_a_plugin_server_says_why_it_is_unknown(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = _config(sandbox["home"])

        result = CliRunner().invoke(
            cli,
            ["add", "--from-clients", "--select", "firebase", "--json", "--config", str(config)],
        )

        assert result.exit_code == 1
        document = json.loads(result.stdout)
        assert document["error"] == "unknown_server"
        assert document["unknown"] == ["firebase"]
        assert [row["runtime_name"] for row in document["plugin_bypass"]] == [
            "plugin:firebase:firebase"
        ]
        assert "firebase is registered by a Claude Code plugin" in result.stderr

    def test_prune_with_nothing_dual_registered_still_reports(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = _config(
            sandbox["home"], {"fs": {"prefix": "fs", "transport": "stdio", "command": "npx"}}
        )

        text = CliRunner().invoke(cli, ["prune", "--all", "--config", str(config)])
        as_json = CliRunner().invoke(cli, ["prune", "--all", "--json", "--config", str(config)])

        assert text.exit_code == 0, text.output
        assert "plugin:firebase:firebase (stdio, local)" in text.stdout
        assert "No dual-registered upstreams found." in text.stdout
        assert as_json.exit_code == 0, as_json.output
        document = json.loads(as_json.stdout)
        assert document["planned"] == []
        assert [row["runtime_name"] for row in document["plugin_bypass"]] == [
            "plugin:firebase:firebase"
        ]
        assert "bypass STM" in as_json.stderr

    def test_prune_not_dual_registered_carries_the_rows(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = _config(
            sandbox["home"], {"fs": {"prefix": "fs", "transport": "stdio", "command": "npx"}}
        )

        result = CliRunner().invoke(cli, ["prune", "firebase", "--json", "--config", str(config)])

        assert result.exit_code == 1
        document = json.loads(result.stdout)
        assert document["error"] == "not_dual_registered"
        assert document["plugin_bypass"][0]["runtime_name"] == "plugin:firebase:firebase"

    def test_init_text_reports_before_the_manual_prompt(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = sandbox["home"] / "new.json"

        result = CliRunner().invoke(
            cli,
            ["init", "--no-validate", "--client", "skip", "--config", str(config)],
            input="filesystem\nfs\nstdio\nnpx\n-y @modelcontextprotocol/server-fs\n",
        )

        assert result.exit_code == 0, result.output
        assert "plugin:firebase:firebase (stdio, local)" in result.stdout
        assert result.stdout.index("bypass STM") < result.stdout.index("Server name")

    def test_init_json_carries_the_rows_on_success(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = sandbox["home"] / "new.json"

        result = CliRunner().invoke(
            cli,
            [
                "init",
                "--no-validate",
                "--client",
                "skip",
                "--lang",
                "en",
                "--json",
                "--config",
                str(config),
            ],
            input="filesystem\nfs\nstdio\nnpx\n-y @modelcontextprotocol/server-fs\n",
        )

        assert result.exit_code == 0, result.output
        document = json.loads(result.stdout)
        assert document["ok"] is True
        assert "filesystem" in json.loads(config.read_text(encoding="utf-8"))["upstream_servers"]
        assert [row["runtime_name"] for row in document["plugin_bypass"]] == [
            "plugin:firebase:firebase"
        ]

    @pytest.mark.parametrize(
        "argv",
        [["--demo"], ["--resume"]],
        ids=["demo", "resume"],
    )
    def test_init_json_is_null_when_it_never_asked(self, sandbox, probe, argv):
        # Neither path runs discovery, so ``[]`` would claim no plugin server
        # bypasses STM when nothing was checked.
        probe["out"] = [FIREBASE]
        config = _config(sandbox["home"]) if argv == ["--resume"] else sandbox["home"] / "n.json"

        result = CliRunner().invoke(
            cli,
            ["init", *argv, "--client", "skip", "--no-validate", "--lang", "en", "--json"]
            + ["--config", str(config)],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["plugin_bypass"] is None
        assert probe["calls"] == []

    def test_init_json_is_null_when_it_fails_before_discovery(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = _config(sandbox["home"])  # exists, no --resume → refused

        result = CliRunner().invoke(cli, ["init", "--json", "--config", str(config)])

        assert result.exit_code == 1
        assert json.loads(result.stdout)["plugin_bypass"] is None

    def test_init_json_keeps_the_rows_on_prompt_eof(self, sandbox, probe):
        probe["out"] = [FIREBASE]
        config = sandbox["home"] / "new.json"

        result = CliRunner().invoke(
            cli, ["init", "--client", "skip", "--json", "--config", str(config)], input=""
        )

        assert result.exit_code != 0
        document = json.loads(result.stdout)
        assert document["error"] == "setup_failed"
        assert [row["runtime_name"] for row in document["plugin_bypass"]] == [
            "plugin:firebase:firebase"
        ]

    def test_init_json_keeps_the_rows_on_a_write_failure(self, sandbox, probe, monkeypatch):
        probe["out"] = [FIREBASE]
        config = sandbox["home"] / "new.json"

        def denied(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(proxy_mod, "atomic_write_text", denied)

        result = CliRunner().invoke(
            cli,
            [
                "init",
                "--no-validate",
                "--client",
                "skip",
                "--lang",
                "en",
                "--json",
                "--config",
                str(config),
            ],
            input="filesystem\nfs\nstdio\nnpx\n-y @modelcontextprotocol/server-fs\n",
        )

        assert result.exit_code != 0
        document = json.loads(result.stdout)
        assert document["error"] == "config_write_failed"
        assert [row["runtime_name"] for row in document["plugin_bypass"]] == [
            "plugin:firebase:firebase"
        ]

    def test_register_payload_does_not_gain_the_key(self, sandbox, probe):
        # Only ``init`` probes; the JSON decorator it shares with ``register``
        # must not add a field ``register`` never fills.
        probe["out"] = [FIREBASE]
        config = _config(sandbox["home"])

        result = CliRunner().invoke(
            cli, ["register", "--mcp", "json", "--json", "--config", str(config)]
        )

        document = json.loads(result.stdout)
        assert document["action"] == "register"
        assert "plugin_bypass" not in document
        assert probe["calls"] == []

    def test_init_lock_failure_is_the_shared_envelope_without_the_key(
        self, sandbox, probe, monkeypatch
    ):
        # The write lock wraps init from outside, so a lock failure is raised
        # before init runs and uses the envelope every mutator shares; docs say
        # it carries no ``plugin_bypass`` (rather than a null claiming a check).
        from memtomem_stm.mms import state

        probe["out"] = [FIREBASE]

        def denied(path):
            raise IsADirectoryError(21, "Is a directory", str(path))

        monkeypatch.setattr(state, "open_lock_fd", denied)
        config = sandbox["home"] / "new.json"

        result = CliRunner().invoke(cli, ["init", "--json", "--config", str(config)])

        assert result.exit_code == 1
        document = json.loads(result.stdout)
        assert document["error"] == "config_lock_unavailable"
        assert "plugin_bypass" not in document
        assert probe["calls"] == []
