"""Tests for ``memtomem_stm.cli.proxy`` — the user-facing proxy management CLI.

The CLI has no dedicated tests (issue #73). Logic worth locking in:

- ``_load`` config-corruption handling (silently broken CLI would otherwise
  give no error signal).
- ``add``'s security-relevant validations — dangerous env keys, duplicate
  prefixes, prefix format rules.
- Golden-path end-to-end flow: ``add`` → ``list``/``status`` → ``remove``.

Uses ``click.testing.CliRunner`` and a tmp path for the config — no real
home directory is touched.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import cli
from memtomem_stm.mms.secrets import REDACTED_DISPLAY
from helpers import set_home

_FAKE_SERVER = Path(__file__).resolve().parents[1] / "_fake_memtomem_server.py"


@pytest.fixture(autouse=True)
def _hermetic_home(monkeypatch, tmp_path: Path) -> Path:
    """Repoint ``$HOME`` at a sandbox for every test in this module.

    The mutating commands acquire the cross-process write lock at
    ``~/.memtomem/.stm_proxy.lock`` and prune appends to
    ``~/.memtomem/pruned_upstreams.json`` (#475 PR2) — both resolved via
    ``Path.home()`` at call time, so without this the suite would write
    into the developer's real home (violating the module contract above).
    Tests that build their own sandbox home still win: class/function
    fixtures run after module-level autouse ones. Named ``hermetic-home``
    so tests that ``mkdir`` their own ``tmp_path / "home"`` don't collide.
    """
    home = tmp_path / "hermetic-home"
    home.mkdir()
    set_home(monkeypatch, home)
    return home


@pytest.fixture
def config(tmp_path: Path) -> Path:
    """Fresh config path inside a tmp dir — never collides with $HOME."""
    return tmp_path / "stm_proxy.json"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _cfg_args(config: Path) -> list[str]:
    """Shared ``--config`` flag for sub-tests."""
    return ["--config", str(config)]


def _config_with_origin() -> dict:
    """Config payload with one origin-bearing entry (secret in
    ``origin.original.env``) and one plain entry — shared by the
    ``status --json`` / ``list --json`` redaction tests (#475)."""
    return {
        "enabled": True,
        "upstream_servers": {
            "gh": {
                "prefix": "gh",
                "transport": "stdio",
                "command": "npx",
                "origin": {
                    "schema_version": 1,
                    "source": {"kind": "claude-user", "pruned": False},
                    "duplicates": [{"kind": "claude-desktop", "pruned": False}],
                    "imported_at": "2026-06-11T05:00:00Z",
                    "original": {
                        "command": "npx",
                        "env": {"GITHUB_TOKEN": "ghp_supersecret"},
                    },
                },
            },
            "plain": {"prefix": "pl", "command": "uvx"},
        },
    }


def _config_with_active_secrets() -> dict:
    """Config whose *active* (non-origin) ``env`` / ``headers`` carry secrets —
    shared by the ``status``/``list --json`` value-redaction tests. Includes a
    benign value (``NODE_ENV`` / ``X-Trace``) to pin that *all* values are
    masked, and a ``Cookie`` header a key/value secret classifier would miss."""
    return {
        "enabled": True,
        "upstream_servers": {
            "gh": {
                "prefix": "gh",
                "transport": "stdio",
                "command": "npx",
                "env": {"GITHUB_TOKEN": "ghp_active_secret", "NODE_ENV": "production"},
            },
            "remote": {
                "prefix": "rm",
                "transport": "sse",
                "url": "https://example.test/mcp",
                "headers": {
                    "Authorization": "Bearer aaa.bbb.ccc",
                    "Cookie": "sessionid=xyz; csrftoken=qq",
                    "X-Trace": "ok",
                },
            },
        },
    }


# ── version command ─────────────────────────────────────────────────────


class TestVersion:
    def test_prints_package_version(self, runner):
        result = runner.invoke(cli, ["version"])
        assert result.exit_code == 0
        assert "memtomem-stm" in result.output
        assert result.output.strip().startswith("memtomem-stm ")

    def test_version_flag_matches_subcommand(self, runner):
        """``--version`` is the idiomatic Click entry point; the subcommand
        predates it. Both must emit the identical line so scripts that grep
        the version don't care which they invoke (CHANGELOG keeps the
        subcommand for compatibility)."""
        flag = runner.invoke(cli, ["--version"])
        sub = runner.invoke(cli, ["version"])
        assert flag.exit_code == 0
        assert sub.exit_code == 0
        assert flag.output.strip() == sub.output.strip()


# ── bare invocation dispatch (#260) ─────────────────────────────────────


class TestBareInvocationDispatch:
    """Bare ``mms`` / ``memtomem-stm-proxy`` / ``memtomem-stm`` (no subcommand)
    must dispatch on stdin: a TTY prints help so an interactive user discovers
    the CLI, but a piped stdin (the MCP-client stdio case) boots the proxy
    MCP server. This lets any of the three entry points be registered
    interchangeably in an MCP client config — closing the surprise from #260
    where only ``memtomem-stm`` worked as a registration target."""

    def test_bare_with_tty_prints_help(self, runner, monkeypatch):
        monkeypatch.setattr("memtomem_stm.cli.proxy._stdin_is_tty", lambda: True)
        result = runner.invoke(cli, [])
        assert result.exit_code == 0
        assert "proxy gateway management" in result.output
        # Help body should list the subcommands so users can discover them.
        assert "Commands:" in result.output

    def test_bare_with_pipe_dispatches_to_mcp_server(self, runner, monkeypatch):
        called: list[bool] = []

        def fake_server_main() -> None:
            called.append(True)

        monkeypatch.setattr("memtomem_stm.cli.proxy._stdin_is_tty", lambda: False)
        monkeypatch.setattr("memtomem_stm.server.main", fake_server_main)
        result = runner.invoke(cli, [])
        assert result.exit_code == 0
        assert called == [True], "non-TTY bare invocation must boot server.main"

    def test_subcommand_skips_dispatch(self, runner, monkeypatch):
        """``mms version`` (or any subcommand) must NOT trip the MCP-server
        dispatch even if stdin happens to be non-TTY (CI, pipes). The
        ``invoked_subcommand`` guard in the group callback enforces this."""

        def boom() -> None:
            raise AssertionError("server.main should not be called when a subcommand runs")

        monkeypatch.setattr("memtomem_stm.cli.proxy._stdin_is_tty", lambda: False)
        monkeypatch.setattr("memtomem_stm.server.main", boom)
        result = runner.invoke(cli, ["version"])
        assert result.exit_code == 0
        assert "memtomem-stm" in result.output

    def test_unknown_subcommand_does_not_dispatch_server(self, runner, monkeypatch):
        """An unknown positional (``mms doesnotexist``) under non-TTY stdin
        must NOT fall through to ``server.main``. Click raises ``UsageError``
        before the group callback runs, so the dispatch is skipped — but
        ``invoked_subcommand`` semantics are easy to misremember (set only
        for *known* subcommands? for any positional?), so pin the invariant
        here. Regression cost: a stray non-TTY user typo silently boots an
        MCP server instead of erroring."""

        def boom() -> None:
            raise AssertionError("server.main reached via an unknown-subcommand path")

        monkeypatch.setattr("memtomem_stm.cli.proxy._stdin_is_tty", lambda: False)
        monkeypatch.setattr("memtomem_stm.server.main", boom)
        result = runner.invoke(cli, ["doesnotexist"])
        assert result.exit_code != 0

    def test_version_flag_with_pipe_does_not_dispatch_server(self, runner, monkeypatch):
        """``mms --version`` under a piped stdin must short-circuit on the
        eager ``--version`` option, not boot the MCP server. Click's
        ``version_option`` is eager and exits before the group callback,
        so the dispatch never runs — but adding ``invoke_without_command=True``
        is exactly the kind of change that could shift evaluation order, so
        pin the contract. Regression cost: anyone scripting ``mms --version``
        for a quick install check would suddenly hang waiting for stdio
        JSON-RPC."""

        def boom() -> None:
            raise AssertionError("--version must short-circuit before dispatch")

        monkeypatch.setattr("memtomem_stm.cli.proxy._stdin_is_tty", lambda: False)
        monkeypatch.setattr("memtomem_stm.server.main", boom)
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "memtomem-stm" in result.output


# ── style helpers / NO_COLOR contract ───────────────────────────────────


class TestStyleHelpers:
    """The style helpers are tiny but they enforce two contracts that CLI
    users rely on: (1) each signal uses a distinct SGR combo so a screen-
    reader / grep user can tell errors from warnings; (2) NO_COLOR disables
    everything per the https://no-color.org spec, including empty-string.

    Click 8.3 doesn't honor NO_COLOR on real TTYs, so we enforce it in the
    helpers. A regression here would silently re-break that promise."""

    def test_color_emits_expected_sgr(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        from memtomem_stm.cli.proxy import _bad, _err, _hdr, _ok, _warn

        assert _err("X") == "\x1b[31m\x1b[1mX\x1b[0m"  # red + bold
        assert _bad("X") == "\x1b[31mX\x1b[0m"  # red only
        assert _warn("X") == "\x1b[33mX\x1b[0m"  # yellow
        assert _ok("X") == "\x1b[32mX\x1b[0m"  # green
        assert _hdr("X") == "\x1b[1mX\x1b[0m"  # bold only

    @pytest.mark.parametrize("value", ["1", "", "anything"])
    def test_no_color_any_value_disables(self, monkeypatch, value):
        """Per no-color.org: presence of NO_COLOR (any value, incl. empty)
        disables color."""
        monkeypatch.setenv("NO_COLOR", value)
        from memtomem_stm.cli.proxy import _bad, _err, _hdr, _ok, _warn

        for fn in (_err, _warn, _ok, _bad, _hdr):
            assert fn("X") == "X"


# ── _split_args tokenization (Windows backslash-safe) ───────────────────


class TestSplitArgs:
    """``_split_args`` must round-trip Windows paths supplied via ``--args``
    and the interactive prompt. POSIX-mode ``shlex.split`` (the previous
    implementation) consumed ``\\a``, ``\\t``, ``\\_`` etc. as escape
    sequences and emitted ``D:arepotests_x.py`` for
    ``D:\\a\\repo\\tests\\_x.py`` — the mangled string was then handed to
    ``asyncio.create_subprocess_exec`` as a (relative) script path,
    surfacing as ``[Errno 2] No such file`` on the windows-latest CI
    matrix added in #304.

    Tested against monkeypatched ``sys.platform`` so the Windows branch
    runs on POSIX runners too; the live Windows leg covers the real
    interpreter on top.
    """

    def test_posix_uses_shlex_split(self, monkeypatch):
        from memtomem_stm.cli.proxy import _split_args

        monkeypatch.setattr(sys, "platform", "linux")
        assert _split_args("/usr/bin/python script.py --flag x") == [
            "/usr/bin/python",
            "script.py",
            "--flag",
            "x",
        ]

    def test_windows_preserves_backslash_paths(self, monkeypatch):
        """The exact CI repro: a GitHub-Actions Windows-runner path under
        ``D:\\a\\<repo>\\<repo>\\tests\\_fake_memtomem_server.py`` must
        survive tokenization unmodified."""
        from memtomem_stm.cli.proxy import _split_args

        monkeypatch.setattr(sys, "platform", "win32")
        path = r"D:\a\memtomem-stm\memtomem-stm\tests\_fake_memtomem_server.py"
        assert _split_args(path) == [path]

    def test_windows_still_honors_quoted_whitespace(self, monkeypatch):
        from memtomem_stm.cli.proxy import _split_args

        monkeypatch.setattr(sys, "platform", "win32")
        assert _split_args(r'C:\bin\x.py --msg "hello world"') == [
            r"C:\bin\x.py",
            "--msg",
            "hello world",
        ]

    def test_windows_honors_single_quoted_whitespace(self, monkeypatch):
        """POSIX-style single quoting is preserved on Windows too — only
        the backslash-escape (``\\X``) is suppressed by ``escape=""``."""
        from memtomem_stm.cli.proxy import _split_args

        monkeypatch.setattr(sys, "platform", "win32")
        assert _split_args(r"C:\bin\x.py --msg 'hello world'") == [
            r"C:\bin\x.py",
            "--msg",
            "hello world",
        ]

    def test_windows_unclosed_quote_raises_valueerror(self, monkeypatch):
        """Match ``shlex.split``'s contract so the existing
        ``except ValueError`` blocks in ``add`` / ``init`` keep working."""
        from memtomem_stm.cli.proxy import _split_args

        monkeypatch.setattr(sys, "platform", "win32")
        with pytest.raises(ValueError):
            _split_args(r'C:\bin\x.py "open ended')

    def test_add_persists_unmangled_args_on_windows(self, runner, config, monkeypatch):
        """End-to-end: ``mms add --args <win path>`` must round-trip into
        the saved JSON config without backslash-escape damage."""
        monkeypatch.setattr(sys, "platform", "win32")
        win_path = r"D:\a\memtomem-stm\memtomem-stm\tests\_fake_memtomem_server.py"
        result = runner.invoke(
            cli,
            [
                "add",
                "fake",
                "--prefix",
                "fk",
                "--command",
                sys.executable,
                "--args",
                win_path,
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert data["upstream_servers"]["fake"]["args"] == [win_path]


# ── _load / config-corruption paths ──────────────────────────────────────


class TestConfigLoad:
    def test_status_handles_missing_config_gracefully(self, runner, config):
        """No config file → friendly hint, no crash."""
        result = runner.invoke(cli, ["status", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "Config not found" in result.output
        assert "mms add" in result.output

    def test_corrupt_json_surfaces_error_not_silent(self, runner, config):
        """Malformed JSON must fail with a message, not return a default dict."""
        config.write_text("{this is not: json", encoding="utf-8")
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 1
        assert "Failed to parse" in result.output

    @pytest.mark.parametrize(
        "payload,expected_type",
        [("[]", "list"), ("null", "NoneType"), ('"oops"', "str"), ("42", "int")],
    )
    def test_rejects_non_dict_top_level(self, runner, config, payload, expected_type):
        """Valid JSON that isn't an object (list/null/string/int) used to crash the
        CLI with an AttributeError traceback. Guard surfaces a clean error."""
        config.write_text(payload, encoding="utf-8")
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 1
        assert "top-level must be a JSON object" in result.output
        assert expected_type in result.output

    def test_rejects_non_dict_upstream_servers(self, runner, config):
        """`upstream_servers` must be a dict — a list/string here would crash
        downstream iteration with an AttributeError traceback."""
        config.write_text(json.dumps({"upstream_servers": "oops"}), encoding="utf-8")
        result = runner.invoke(cli, ["status", *_cfg_args(config)])
        assert result.exit_code == 1
        assert "'upstream_servers' must be an object" in result.output

    def test_list_missing_config(self, runner, config):
        """Missing file → ``list`` distinguishes from empty-config (#221)
        so users troubleshooting wrong --config paths get a clear hint."""
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "Config not found" in result.output
        assert "mms add" in result.output
        assert "No upstream servers configured" not in result.output

    def test_list_empty_config(self, runner, config):
        """Empty (but present) config → ``list`` prints the empty-state
        message — distinct from the missing-config branch above."""
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "No upstream servers configured" in result.output
        assert "Config not found" not in result.output

    def test_list_json_missing_config(self, runner, config):
        """``list --json`` already returned ``config_not_found`` since #220;
        pin it here next to the new text-path test for symmetry."""
        result = runner.invoke(cli, ["list", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["error"] == "config_not_found"
        assert str(config) in data["path"]


# ── status command ───────────────────────────────────────────────────────


class TestStatus:
    def test_shows_config_path_and_enabled_state(self, runner, config):
        config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["status", *_cfg_args(config)])
        assert result.exit_code == 0
        assert str(config) in result.output
        assert "Enabled: yes" in result.output
        assert "Servers: 0" in result.output

    def test_json_output(self, runner, config):
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {
                        "fs": {"prefix": "fs", "command": "uvx", "args": ["mcp-server-fs"]},
                    },
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["status", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["enabled"] is True
        assert "fs" in data["servers"]
        assert str(config) in data["config_path"]
        # #614 additive keys — the full `servers` map above stays (scripted
        # consumers + the redaction pins depend on it); the counts let
        # callers match the human summary without re-deriving the pruned
        # predicate.
        assert data["server_count"] == 1
        assert data["pruned_count"] == 0

    def test_json_missing_config(self, runner, config):
        result = runner.invoke(cli, ["status", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["error"] == "config_not_found"

    def test_valid_json_invalid_schema_warns_exit_zero(self, runner, config):
        """#611: valid JSON that fails model validation is exactly what a
        running server silently degrades on — ``status`` must warn (but stay
        exit 0; strictness lives in ``mms config validate``)."""
        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "a": {"prefix": "dup", "command": "a"},
                        "b": {"prefix": "dup", "command": "b"},
                    }
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["status", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "fails validation" in result.output
        assert "falls back to env/defaults" in result.output

    def test_valid_json_invalid_schema_flagged_in_json(self, runner, config):
        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "a": {"prefix": "dup", "command": "a"},
                        "b": {"prefix": "dup", "command": "b"},
                    }
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["status", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["config_valid"] is False
        assert "Duplicate upstream prefixes" in data["config_error"]

    def test_valid_config_marked_valid_in_json(self, runner, config):
        config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["status", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["config_valid"] is True
        assert data["config_error"] is None

    def test_json_redacts_origin_original(self, runner, config):
        """``origin.original`` is the verbatim host entry and may carry
        secrets — ``status --json`` must emit only the provenance summary
        plus ``has_original`` (#475)."""
        config.write_text(json.dumps(_config_with_origin()), encoding="utf-8")
        result = runner.invoke(cli, ["status", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        origin = json.loads(result.output)["servers"]["gh"]["origin"]
        assert "original" not in origin
        assert origin["has_original"] is True
        assert "ghp_supersecret" not in result.output
        # Summary keys survive — scripts can still read provenance.
        assert origin["source"] == {"kind": "claude-user", "pruned": False}
        assert origin["duplicates"] == [{"kind": "claude-desktop", "pruned": False}]
        assert origin["imported_at"] == "2026-06-11T05:00:00Z"
        # Redaction is output-only: the config file keeps the original.
        on_disk = json.loads(config.read_text(encoding="utf-8"))
        assert on_disk["upstream_servers"]["gh"]["origin"]["original"]["env"] == {
            "GITHUB_TOKEN": "ghp_supersecret"
        }

    def test_json_redacts_active_env_and_headers(self, runner, config):
        """Active ``env`` / ``headers`` reach ``--json`` verbatim and get piped
        to logs, so their values must be masked (keys preserved). Redaction
        covers *all* values, including ones a secret classifier would miss
        (``Cookie``)."""
        config.write_text(json.dumps(_config_with_active_secrets()), encoding="utf-8")
        result = runner.invoke(cli, ["status", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        servers = json.loads(result.output)["servers"]
        # env: keys kept, every value masked (secret and benign alike).
        assert servers["gh"]["env"] == {
            "GITHUB_TOKEN": REDACTED_DISPLAY,
            "NODE_ENV": REDACTED_DISPLAY,
        }
        # headers: keys kept, every value masked (incl. the Cookie a classifier misses).
        assert servers["remote"]["headers"] == {
            "Authorization": REDACTED_DISPLAY,
            "Cookie": REDACTED_DISPLAY,
            "X-Trace": REDACTED_DISPLAY,
        }
        # No secret material survives anywhere in the output stream.
        for secret in ("ghp_active_secret", "aaa.bbb.ccc", "sessionid=xyz"):
            assert secret not in result.output
        # Output-only: the config file keeps the real values.
        on_disk = json.loads(config.read_text(encoding="utf-8"))
        assert on_disk["upstream_servers"]["gh"]["env"]["GITHUB_TOKEN"] == "ghp_active_secret"
        assert (
            on_disk["upstream_servers"]["remote"]["headers"]["Authorization"]
            == "Bearer aaa.bbb.ccc"
        )

    def test_json_redacts_malformed_non_dict_env_headers(self, runner, config):
        """``--json`` reads raw config, not a validated model, so a hand-edited
        / corrupted entry can carry a non-dict ``env`` / ``headers`` (string,
        list, …). Those are still potentially secret-bearing and must not be
        emitted verbatim — they are replaced wholesale with the sentinel."""
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {
                        "bad": {
                            "prefix": "bd",
                            "command": "x",
                            "env": "GITHUB_TOKEN=ghp_strsecret",
                            "headers": ["Authorization: Bearer leaked"],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["status", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        servers = json.loads(result.output)["servers"]
        assert servers["bad"]["env"] == REDACTED_DISPLAY
        assert servers["bad"]["headers"] == REDACTED_DISPLAY
        for secret in ("ghp_strsecret", "Bearer leaked"):
            assert secret not in result.output

    def test_human_output_is_summary_without_server_rows(self, runner, config):
        """#614: status is the config summary; per-server rows moved to
        ``mms list``. Pins the removal — none of the old per-server
        fragments may reappear — and the pointer at the new home."""
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {
                        "fs": {
                            "prefix": "fs",
                            "transport": "stdio",
                            "command": "uvx",
                            "args": ["mcp-server-fs"],
                            "compression": "auto",
                            "max_result_chars": 8000,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["status", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "Servers: 1" in result.output
        assert "mms list" in result.output
        assert "prefix=" not in result.output
        assert "compression=" not in result.output
        assert "surfacing=" not in result.output
        assert "uvx" not in result.output

    def test_servers_line_counts_host_pruned(self, runner, config):
        """The host-pruned suffix uses the same every-source predicate as
        the ``mms list`` ``*`` marker: fully-pruned entries count, an
        entry with an un-pruned duplicate does not."""

        def _entry(*, source_pruned: bool, dup_pruned: bool | None = None) -> dict:
            duplicates = (
                [] if dup_pruned is None else [{"kind": "claude-desktop", "pruned": dup_pruned}]
            )
            return {
                "prefix": "xx",
                "transport": "stdio",
                "command": "npx",
                "origin": {
                    "schema_version": 1,
                    "source": {"kind": "claude-user", "pruned": source_pruned},
                    "duplicates": duplicates,
                    "imported_at": "2026-06-11T00:00:00Z",
                    "original": {"command": "npx"},
                },
            }

        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {
                        "gone": _entry(source_pruned=True),
                        "partial": _entry(source_pruned=True, dup_pruned=False),
                        "manual": {"prefix": "mn", "command": "npx"},
                    },
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["status", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "Servers: 3 (1 host-pruned)" in result.output
        data = json.loads(runner.invoke(cli, ["status", "--json", *_cfg_args(config)]).output)
        assert data["server_count"] == 3
        assert data["pruned_count"] == 1

    def test_no_pruned_suffix_when_nothing_pruned(self, runner, config):
        config.write_text(
            json.dumps(
                {"enabled": True, "upstream_servers": {"fs": {"prefix": "fs", "command": "x"}}}
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["status", *_cfg_args(config)])
        assert "Servers: 1\n" in result.output

    def test_empty_config_points_at_add(self, runner, config):
        config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["status", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "mms add" in result.output
        assert "mms list" not in result.output


# ── list command ─────────────────────────────────────────────────────────


class TestListServers:
    def test_list_one_stdio_server(self, runner, config):
        runner.invoke(
            cli,
            ["add", "fs", "--prefix", "fs", "--command", "uvx", *_cfg_args(config)],
        )
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "NAME" in result.output
        assert "fs" in result.output
        assert "stdio" in result.output
        assert "1 server(s) configured" in result.output

    def test_list_http_server_shows_url(self, runner, config):
        runner.invoke(
            cli,
            [
                "add",
                "remote",
                "--prefix",
                "rt",
                "--transport",
                "sse",
                "--url",
                "https://example.com/mcp",
                *_cfg_args(config),
            ],
        )
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert "example.com/mcp" in result.output

    def test_list_streamable_http_row_aligns_with_header(self, runner, config):
        """``streamable_http`` (15 chars) used to overflow the TRANSPORT
        column and push COMPRESSION + COMMAND/URL out of alignment with
        the header. Pin the header/row column boundaries so the regression
        is caught — not just whether the URL appears.
        """
        runner.invoke(
            cli,
            [
                "add",
                "wide",
                "--prefix",
                "wd",
                "--transport",
                "streamable_http",
                "--url",
                "https://example.com/mcp",
                *_cfg_args(config),
            ],
        )
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 0
        lines = result.output.splitlines()
        # Header is the first non-empty styled line; the row immediately
        # follows the dashed separator.
        header = next(line for line in lines if "NAME" in line and "TRANSPORT" in line)
        row = next(line for line in lines if line.startswith("wide"))
        # The COMPRESSION column starts at the same offset on header
        # and row — drift used to put them several chars apart.
        assert header.index("COMPRESSION") == row.index("auto")
        # Same pin for the SURFACING column added in #614 ("on" is the
        # default — the flag is absent on this entry).
        assert row[header.index("SURFACING") :].startswith("on")
        # Same pin for the ORIGIN column added in #475 PR4 ("-" is the
        # no-provenance cell; this entry was added manually).
        assert row[header.index("ORIGIN")] == "-"

    def test_list_surfacing_column_shows_toggle(self, runner, config):
        """The SURFACING column is the per-server toggle's visible home
        (#614 — ``mms status`` no longer prints per-server rows): ``off``
        when ``surfacing_enabled`` is false, ``on`` when absent (default)."""
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {
                        "quiet": {"prefix": "q", "command": "npx", "surfacing_enabled": False},
                        "loud": {"prefix": "l", "command": "npx"},
                    },
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 0
        lines = result.output.splitlines()
        header = next(line for line in lines if "SURFACING" in line)
        offset = header.index("SURFACING")
        quiet_row = next(line for line in lines if line.startswith("quiet"))
        loud_row = next(line for line in lines if line.startswith("loud"))
        assert quiet_row[offset:].startswith("off")
        assert loud_row[offset:].startswith("on")

    def test_list_origin_column_summarizes_provenance(self, runner, config):
        """The ORIGIN column shows the recorded source kind for imported
        entries, ``*`` when the host original was pruned, and ``-`` for
        entries without provenance (#475 PR4). The pruned legend points
        at ``mms eject``."""

        def _entry(kind: str, pruned: bool) -> dict:
            return {
                "prefix": "xx",
                "transport": "stdio",
                "command": "npx",
                "origin": {
                    "schema_version": 1,
                    "source": {"kind": kind, "pruned": pruned},
                    "duplicates": [],
                    "imported_at": "2026-06-11T00:00:00Z",
                    "original": {"command": "npx"},
                },
            }

        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {
                        "manual": {"prefix": "mn", "command": "npx"},
                        "stm-only": _entry("claude-user", pruned=True),
                        "dual": _entry("claude-desktop", pruned=False),
                    },
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 0
        lines = result.output.splitlines()
        header = next(line for line in lines if "NAME" in line and "ORIGIN" in line)
        names = ("manual", "stm-only", "dual")
        rows = {n: next(line for line in lines if line.startswith(n)) for n in names}
        assert "claude-user*" in rows["stm-only"]
        assert "claude-desktop" in rows["dual"]
        assert "claude-desktop*" not in rows["dual"]
        assert rows["manual"][header.index("ORIGIN")] == "-"
        assert "mms eject" in result.output

    def test_list_no_pruned_marker_when_unpruned_duplicate_remains(self, runner, config):
        """Partial prune (primary source pruned, duplicate source not) must
        NOT star the row: an un-pruned duplicate still registers the server,
        so 'exists only behind STM' would be false — and the marker would
        contradict ``mms remove``'s orphaning hint, which shares the same
        every-source predicate (codex R1)."""
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {
                        "partial": {
                            "prefix": "pa",
                            "command": "npx",
                            "origin": {
                                "schema_version": 1,
                                "source": {"kind": "claude-user", "pruned": True},
                                "duplicates": [{"kind": "claude-desktop", "pruned": False}],
                                "imported_at": "2026-06-11T00:00:00Z",
                                "original": {"command": "npx"},
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 0
        row = next(line for line in result.output.splitlines() if line.startswith("partial"))
        assert "claude-user" in row
        assert "claude-user*" not in row
        assert "mms eject" not in result.output

    def test_list_origin_legend_absent_without_pruned_entries(self, runner, config):
        """The ``mms eject`` legend only appears when some row actually
        carries the pruned marker — an all-dual / all-manual table stays
        free of restore advice that doesn't apply."""
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {
                        "manual": {"prefix": "mn", "command": "npx"},
                        "dual": {
                            "prefix": "du",
                            "command": "npx",
                            "origin": {
                                "schema_version": 1,
                                "source": {"kind": "claude-user", "pruned": False},
                                "duplicates": [],
                                "imported_at": "2026-06-11T00:00:00Z",
                                "original": {"command": "npx"},
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "mms eject" not in result.output

    def test_json_output(self, runner, config):
        """``list --json`` mirrors ``status --json`` shape for scripting."""
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {
                        "fs": {"prefix": "fs", "command": "uvx", "args": ["mcp-server-fs"]},
                    },
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["list", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "fs" in data["servers"]
        assert str(config) in data["config_path"]
        # ``enabled`` is proxy-wide state owned by ``status`` — its absence
        # here is intentional. Pinning it prevents a future copy-paste from
        # ``status --json`` silently widening the contract.
        assert "enabled" not in data

    def test_json_empty_config(self, runner, config):
        """Empty config → empty ``servers`` object, not text fallback."""
        config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["list", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["servers"] == {}

    def test_json_missing_config(self, runner, config):
        """Missing config → same ``{error, path}`` shape as ``status --json``."""
        result = runner.invoke(cli, ["list", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["error"] == "config_not_found"
        assert str(config) in data["path"]

    def test_json_redacts_origin_original(self, runner, config):
        """Same redaction contract as ``status --json`` (#475) — entries
        without an origin pass through byte-identical."""
        config.write_text(json.dumps(_config_with_origin()), encoding="utf-8")
        result = runner.invoke(cli, ["list", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        servers = json.loads(result.output)["servers"]
        assert "original" not in servers["gh"]["origin"]
        assert servers["gh"]["origin"]["has_original"] is True
        assert "ghp_supersecret" not in result.output
        assert servers["plain"] == {"prefix": "pl", "command": "uvx"}
        assert "origin" not in servers["plain"]

    def test_json_redacts_active_env_and_headers(self, runner, config):
        """Same active-``env``/``headers`` value masking as ``status --json``."""
        config.write_text(json.dumps(_config_with_active_secrets()), encoding="utf-8")
        result = runner.invoke(cli, ["list", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        servers = json.loads(result.output)["servers"]
        assert servers["gh"]["env"] == {
            "GITHUB_TOKEN": REDACTED_DISPLAY,
            "NODE_ENV": REDACTED_DISPLAY,
        }
        assert servers["remote"]["headers"] == {
            "Authorization": REDACTED_DISPLAY,
            "Cookie": REDACTED_DISPLAY,
            "X-Trace": REDACTED_DISPLAY,
        }
        for secret in ("ghp_active_secret", "aaa.bbb.ccc", "sessionid=xyz"):
            assert secret not in result.output


# ── add command — validation paths ───────────────────────────────────────


class TestAddValidation:
    def test_rejects_invalid_prefix_format(self, runner, config):
        result = runner.invoke(
            cli,
            ["add", "s", "--prefix", "1bad", "--command", "x", *_cfg_args(config)],
        )
        assert result.exit_code == 1
        assert "invalid prefix" in result.output

    def test_rejects_double_underscore_in_prefix(self, runner, config):
        """``__`` is the tool-namespace separator; user prefixes must not use it."""
        result = runner.invoke(
            cli,
            ["add", "s", "--prefix", "my__pref", "--command", "x", *_cfg_args(config)],
        )
        assert result.exit_code == 1
        assert "invalid prefix" in result.output

    def test_rejects_prefix_over_hard_budget(self, runner, config, monkeypatch):
        """#261: prefix length above ``prefix_hard_limit()`` (42 for the
        default 12-char client server name) guarantees overflow even for a
        1-char upstream tool name. Reject at ``add`` time so the user
        doesn't discover the silent drop only after registering the
        upstream and watching tools go missing."""
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)
        long_prefix = "a" * 43  # 1 over hard limit
        result = runner.invoke(
            cli,
            ["add", "s", "--prefix", long_prefix, "--command", "x", *_cfg_args(config)],
        )
        assert result.exit_code == 1
        assert "Error:" in result.output
        # Error names the offending length and the relevant max — both
        # are pulled from the helper, so this catches drift if either
        # the message template or the budget arithmetic changes.
        assert "is 43 chars" in result.output
        assert "max for client server name 'memtomem-stm' is 42" in result.output
        # Suggestions surface both fix paths the user can take.
        assert "Use a shorter --prefix" in result.output
        assert "Register STM as 'mms'" in result.output

    def test_warns_on_prefix_above_soft_threshold(self, runner, config, monkeypatch):
        """Above 21 chars but within hard budget: warn and proceed (it might
        be fine if all upstream tool names are short, but flag the risk)."""
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)
        warn_prefix = "a" * 22  # 1 over warn threshold, well under hard limit
        result = runner.invoke(
            cli,
            ["add", "s", "--prefix", warn_prefix, "--command", "x", *_cfg_args(config)],
        )
        assert result.exit_code == 0
        assert "Warning" in result.output
        assert "silently dropped" in result.output

    def test_silent_at_or_below_warn_threshold(self, runner, config, monkeypatch):
        """At 21 chars and below: the silent path. No warning noise on
        every routine ``add`` for typical short prefixes."""
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)
        ok_prefix = "a" * 21
        result = runner.invoke(
            cli,
            ["add", "s", "--prefix", ok_prefix, "--command", "x", *_cfg_args(config)],
        )
        assert result.exit_code == 0
        assert "silently dropped" not in result.output

    def test_hard_budget_loosens_when_client_server_name_short(self, runner, config, monkeypatch):
        """Setting ``MMS_CLIENT_SERVER_NAME=mms`` mirrors the recommended
        short-server-name registration in the client config — prefixes that
        would be rejected for the default 12-char server should now
        proceed (warn or pass)."""
        monkeypatch.setenv("MMS_CLIENT_SERVER_NAME", "mms")
        # Same 43-char prefix that gets hard-rejected with default server:
        prefix = "a" * 43  # hard limit becomes 51, so 43 is now under
        result = runner.invoke(
            cli,
            ["add", "s", "--prefix", prefix, "--command", "x", *_cfg_args(config)],
        )
        assert result.exit_code == 0  # passes — no longer a hard reject
        # Hard-reject message is gone (the user-visible signal of a real
        # negative — checking for the actual error template fragment, not
        # an arbitrary string that happens to be absent everywhere).
        assert "Error:" not in result.output
        assert "max for client server name" not in result.output
        # Should still warn since 43 > the (relaxed) warn threshold of 30.
        assert "Warning" in result.output

    def test_rejects_duplicate_server_name(self, runner, config):
        runner.invoke(
            cli,
            ["add", "fs", "--prefix", "fs", "--command", "x", *_cfg_args(config)],
        )
        result = runner.invoke(
            cli,
            ["add", "fs", "--prefix", "fs2", "--command", "y", *_cfg_args(config)],
        )
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_rejects_duplicate_prefix_without_writing(self, runner, config):
        """Duplicate prefix is a hard reject — the runtime validator refuses
        to load such a config, so `add` must not save one. The config file
        stays byte-identical."""
        runner.invoke(
            cli,
            ["add", "fs1", "--prefix", "fs", "--command", "x", *_cfg_args(config)],
        )
        before = config.read_bytes()
        result = runner.invoke(
            cli,
            ["add", "fs2", "--prefix", "fs", "--command", "y", *_cfg_args(config)],
        )
        assert result.exit_code == 1
        assert "Error:" in result.output
        assert "Duplicate upstream prefixes detected" in result.output
        # Both the colliding prefix and the existing server key are named.
        assert "'fs'" in result.output
        assert "fs1" in result.output
        assert config.read_bytes() == before

    def test_stdio_requires_command(self, runner, config):
        result = runner.invoke(
            cli,
            ["add", "s", "--prefix", "s", *_cfg_args(config)],
        )
        assert result.exit_code == 1
        assert "--command is required" in result.output

    def test_sse_requires_url(self, runner, config):
        result = runner.invoke(
            cli,
            ["add", "s", "--prefix", "s", "--transport", "sse", *_cfg_args(config)],
        )
        assert result.exit_code == 1
        assert "--url is required" in result.output

    def test_env_requires_kv_format(self, runner, config):
        result = runner.invoke(
            cli,
            [
                "add",
                "s",
                "--prefix",
                "s",
                "--command",
                "x",
                "--env",
                "MALFORMED",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert "--env must be KEY=VALUE" in result.output

    def test_env_rejects_empty_key(self, runner, config):
        result = runner.invoke(
            cli,
            [
                "add",
                "s",
                "--prefix",
                "s",
                "--command",
                "x",
                "--env",
                "=value",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert "--env key must be non-empty" in result.output

    @pytest.mark.parametrize(
        "key",
        ["LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "NODE_OPTIONS"],
    )
    def test_env_blocks_dangerous_injection_keys(self, runner, config, key):
        """Security: these env vars can hijack spawned subprocesses.
        The block is the whole reason --env parsing exists as logic instead
        of a plain dict copy — a regression here would reopen an RCE vector."""
        result = runner.invoke(
            cli,
            [
                "add",
                "s",
                "--prefix",
                "s",
                "--command",
                "x",
                "--env",
                f"{key}=anything",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert "blocked for security reasons" in result.output
        assert key in result.output

    def test_env_dangerous_key_check_is_case_insensitive(self, runner, config):
        """The check upper-cases the key — a lowercase variant must also block."""
        result = runner.invoke(
            cli,
            [
                "add",
                "s",
                "--prefix",
                "s",
                "--command",
                "x",
                "--env",
                "ld_preload=bad.so",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert "blocked for security reasons" in result.output

    def test_header_requires_kv_format(self, runner, config):
        result = runner.invoke(
            cli,
            [
                "add",
                "s",
                "--prefix",
                "s",
                "--transport",
                "sse",
                "--url",
                "https://example.com/sse",
                "--header",
                "MALFORMED",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert "--header must be KEY=VALUE" in result.output

    def test_header_rejects_empty_key(self, runner, config):
        result = runner.invoke(
            cli,
            [
                "add",
                "s",
                "--prefix",
                "s",
                "--transport",
                "sse",
                "--url",
                "https://example.com/sse",
                "--header",
                "=value",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert "--header key must be non-empty" in result.output

    def test_header_allows_dangerous_env_names(self, runner, config):
        """Intentional asymmetry with ``--env``: ``_DANGEROUS_ENV_KEYS``
        guards env injection into spawned processes, while headers are HTTP
        request metadata that never touch a subprocess environment — a header
        literally named PATH or LD_PRELOAD is legal (contrast with
        ``test_env_blocks_dangerous_injection_keys``)."""
        result = runner.invoke(
            cli,
            [
                "add",
                "s",
                "--prefix",
                "s",
                "--transport",
                "sse",
                "--url",
                "https://example.com/sse",
                "--header",
                "PATH=/x",
                "--header",
                "LD_PRELOAD=v",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert data["upstream_servers"]["s"]["headers"] == {"PATH": "/x", "LD_PRELOAD": "v"}

    def test_header_rejected_for_stdio_transport(self, runner, config):
        """A header on a stdio entry would be silently ignored by the
        runtime — reject at registration time instead."""
        result = runner.invoke(
            cli,
            [
                "add",
                "s",
                "--prefix",
                "s",
                "--command",
                "x",
                "--header",
                "Authorization=Bearer t",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert "--header requires an HTTP transport" in result.output
        # No partial entry written.
        if config.exists():
            data = json.loads(config.read_text(encoding="utf-8"))
            assert "s" not in data.get("upstream_servers", {})


class TestAtomicSave:
    """Direct exercises of ``_save``'s atomic-rename behaviour.

    A torn write would otherwise let the proxy's hot-reload watcher read
    a half-written JSON file (the previous ``Path.write_text`` path was
    truncate-then-write).
    """

    def test_write_mcp_json_failure_leaves_prior_contents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # .mcp.json is re-read by Claude Code at project load; a failed write
        # must leave the previous contents intact, not truncated JSON (the
        # previous Path.write_text path was truncate-then-write).
        from memtomem_stm.cli.proxy import _write_mcp_json_for_stm

        mcp_path = tmp_path / ".mcp.json"
        prior = '{"mcpServers": {"existing": {"command": "keep-me"}}}\n'
        mcp_path.write_text(prior, encoding="utf-8")

        def boom(*_a, **_kw):
            raise OSError("simulated rename failure")

        monkeypatch.setattr("memtomem_stm.utils.fileio.os.replace", boom)
        with pytest.raises(OSError, match="simulated rename"):
            _write_mcp_json_for_stm(tmp_path, "memtomem-stm", [])

        assert mcp_path.read_text(encoding="utf-8") == prior  # untouched
        assert list(tmp_path.glob(".mcp.json.*.tmp")) == []  # no temp litter

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_write_mcp_json_preserves_existing_mode(self, tmp_path: Path) -> None:
        # .mcp.json is a shared (non-secret) project file. The atomic helper's
        # mkstemp temp is 0600; without an explicit mode that would survive
        # the rename and silently drop group/other read access (codex catch).
        from memtomem_stm.cli.proxy import _write_mcp_json_for_stm

        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text("{}", encoding="utf-8")
        mcp_path.chmod(0o644)
        _write_mcp_json_for_stm(tmp_path, "memtomem-stm", [])
        assert (mcp_path.stat().st_mode & 0o777) == 0o644

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_write_mcp_json_new_file_gets_project_mode(self, tmp_path: Path) -> None:
        from memtomem_stm.cli.proxy import _write_mcp_json_for_stm

        _write_mcp_json_for_stm(tmp_path, "memtomem-stm", [])
        assert ((tmp_path / ".mcp.json").stat().st_mode & 0o777) == 0o644

    def test_save_writes_payload_and_leaves_no_temp_file(self, tmp_path: Path) -> None:
        from memtomem_stm.cli.proxy import _save

        target = tmp_path / "stm_proxy.json"
        _save(target, {"enabled": True, "upstream_servers": {}})

        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8")) == {
            "enabled": True,
            "upstream_servers": {},
        }
        # No leftover sibling temp files (mkstemp prefix matches target name)
        leftover = list(tmp_path.glob("stm_proxy.json.*.tmp"))
        assert leftover == []

    def test_save_overwrites_existing_atomically(self, tmp_path: Path) -> None:
        from memtomem_stm.cli.proxy import _save

        target = tmp_path / "stm_proxy.json"
        target.write_text('{"enabled": false, "upstream_servers": {}}\n', encoding="utf-8")
        old_inode = target.stat().st_ino

        _save(target, {"enabled": True, "upstream_servers": {}})

        # Content updated
        assert json.loads(target.read_text(encoding="utf-8"))["enabled"] is True
        # On POSIX, atomic rename replaces the file (new inode); the old
        # inode is unlinked. Skip strict inode check on platforms where
        # this is not guaranteed (Windows), but assert content correctness
        # everywhere.
        if hasattr(os, "fsync"):  # POSIX
            new_inode = target.stat().st_ino
            assert new_inode != old_inode

    def test_save_cleans_up_temp_on_failure(self, tmp_path: Path, monkeypatch) -> None:
        """If ``os.replace`` fails, the sibling temp file must be removed."""
        from memtomem_stm.cli.proxy import _save

        target = tmp_path / "stm_proxy.json"

        def boom(*_a, **_kw):  # pragma: no cover - patched per test
            raise OSError("simulated rename failure")

        # ``_save`` was migrated to delegate to ``utils.fileio.atomic_write_text``;
        # the rename now happens there, so patch the helper's ``os.replace``.
        monkeypatch.setattr("memtomem_stm.utils.fileio.os.replace", boom)

        with pytest.raises(OSError, match="simulated rename"):
            _save(target, {"enabled": True})

        leftover = list(tmp_path.glob("stm_proxy.json.*.tmp"))
        assert leftover == []
        assert not target.exists()  # original was never written


class TestWriteMcpJsonParseSafety:
    """Parse/shape failures must abort registration, never rewrite the file.

    The previous behavior swallowed ``JSONDecodeError``/``OSError``, fell back
    to ``{}``, and the subsequent (atomic) write then discarded every
    registration already present in ``.mcp.json``. Byte-identical comparison
    via ``read_bytes`` on purpose — text-level reads can hide e.g.
    trailing-newline rewrites.
    """

    @staticmethod
    def _write(target_dir: Path) -> Path:
        from memtomem_stm.cli.proxy import _write_mcp_json_for_stm

        return _write_mcp_json_for_stm(target_dir, "memtomem-stm", [])

    def test_corrupt_json_aborts_and_leaves_bytes_untouched(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mcp_path = tmp_path / ".mcp.json"
        corrupt = b'{"mcpServers": {"existing": {"command": "keep-me"}}'  # missing brace
        mcp_path.write_bytes(corrupt)

        with pytest.raises(SystemExit) as excinfo:
            self._write(tmp_path)

        assert excinfo.value.code == 1
        assert mcp_path.read_bytes() == corrupt
        err = capsys.readouterr().err
        assert "Failed to parse" in err
        assert "line 1" in err  # JSONDecodeError position surfaced
        assert "not modified" in err

    def test_invalid_utf8_aborts_and_leaves_bytes_untouched(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # UnicodeDecodeError is a ValueError, not an OSError — it must get the
        # same styled abort, not an unhandled traceback (codex catch, PR #653).
        mcp_path = tmp_path / ".mcp.json"
        prior = b'\xff\xfe{"mcpServers": {}}'
        mcp_path.write_bytes(prior)

        with pytest.raises(SystemExit) as excinfo:
            self._write(tmp_path)

        assert excinfo.value.code == 1
        assert mcp_path.read_bytes() == prior
        assert "not valid UTF-8" in capsys.readouterr().err

    def test_top_level_non_dict_aborts_and_leaves_bytes_untouched(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mcp_path = tmp_path / ".mcp.json"
        prior = b'["valid json", "wrong shape"]'
        mcp_path.write_bytes(prior)

        with pytest.raises(SystemExit) as excinfo:
            self._write(tmp_path)

        assert excinfo.value.code == 1
        assert mcp_path.read_bytes() == prior
        assert "top-level must be a JSON object" in capsys.readouterr().err

    def test_mcp_servers_non_dict_aborts_and_leaves_bytes_untouched(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mcp_path = tmp_path / ".mcp.json"
        prior = b'{"mcpServers": ["not", "a", "mapping"]}'
        mcp_path.write_bytes(prior)

        with pytest.raises(SystemExit) as excinfo:
            self._write(tmp_path)

        assert excinfo.value.code == 1
        assert mcp_path.read_bytes() == prior
        assert "'mcpServers' must be an object" in capsys.readouterr().err

    def test_unreadable_file_aborts_without_write(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Previously an OSError on read also fell back to {} and overwrote.
        mcp_path = tmp_path / ".mcp.json"
        prior = b'{"mcpServers": {"existing": {"command": "keep-me"}}}'
        mcp_path.write_bytes(prior)

        def boom(self: Path, *args: object, **kwargs: object) -> str:
            raise OSError("simulated read failure")

        monkeypatch.setattr(Path, "read_text", boom)

        with pytest.raises(SystemExit) as excinfo:
            self._write(tmp_path)

        assert excinfo.value.code == 1
        monkeypatch.undo()
        assert mcp_path.read_bytes() == prior
        assert "Could not read" in capsys.readouterr().err

    def test_valid_file_merge_preserves_siblings_and_unknown_fields(self, tmp_path: Path) -> None:
        mcp_path = tmp_path / ".mcp.json"
        mcp_path.write_text(
            json.dumps(
                {
                    "mcpServers": {"existing": {"command": "keep-me"}},
                    "unknownTopLevel": {"custom": True},
                }
            ),
            encoding="utf-8",
        )

        self._write(tmp_path)

        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        assert data["mcpServers"]["existing"] == {"command": "keep-me"}
        assert data["unknownTopLevel"] == {"custom": True}
        assert data["mcpServers"]["memtomem-stm"] == {"command": "memtomem-stm"}


class TestAddPersistence:
    def test_add_persists_full_entry(self, runner, config):
        result = runner.invoke(
            cli,
            [
                "add",
                "fs",
                "--prefix",
                "fs",
                "--command",
                "uvx",
                "--args",
                "mcp-server-fs --root /tmp",
                "--compression",
                "selective",
                "--max-chars",
                "4000",
                "--env",
                "FOO=bar",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 0

        data = json.loads(config.read_text(encoding="utf-8"))
        srv = data["upstream_servers"]["fs"]
        assert srv["prefix"] == "fs"
        assert srv["command"] == "uvx"
        assert srv["args"] == ["mcp-server-fs", "--root", "/tmp"]
        assert srv["compression"] == "selective"
        assert srv["max_result_chars"] == 4000
        assert srv["env"] == {"FOO": "bar"}
        # Config file is chmod 0o600 (best-effort — skip on platforms that
        # don't support it; the CLI silently ignores the OSError).
        assert config.exists()

    @pytest.mark.parametrize("transport", ["sse", "streamable_http"])
    def test_add_persists_headers_for_http_transports(self, runner, config, transport):
        result = runner.invoke(
            cli,
            [
                "add",
                "api",
                "--prefix",
                "api",
                "--transport",
                transport,
                "--url",
                "https://example.com/mcp",
                "--header",
                "Authorization=Bearer abc",
                "--header",
                "X-Two=2=2",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 0, result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        srv = data["upstream_servers"]["api"]
        assert srv["transport"] == transport
        # "2=2" pins split("=", 1): only the first "=" separates key/value.
        assert srv["headers"] == {"Authorization": "Bearer abc", "X-Two": "2=2"}

    def test_add_malformed_args_fails_cleanly(self, runner, config):
        """shlex can't parse unterminated quotes — must error, not crash."""
        result = runner.invoke(
            cli,
            [
                "add",
                "fs",
                "--prefix",
                "fs",
                "--command",
                "uvx",
                "--args",
                "--unterminated 'quote",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert "malformed --args" in result.output


# ── add --validate (probe before save) ──────────────────────────────────


class TestAddValidate:
    """`add --validate` reuses the health probe to reject unreachable servers
    at config-write time. The contract is: probe fails → exit 1 and *no*
    entry written; probe succeeds → entry written and tool count reported."""

    def test_validate_unreachable_aborts_and_skips_write(self, runner, config):
        result = runner.invoke(
            cli,
            [
                "add",
                "bad",
                "--prefix",
                "bad",
                "--command",
                "__nonexistent_cmd_12345__",
                "--validate",
                "--timeout",
                "3",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert "validation failed" in result.output

        # Config file must not carry a partial entry.
        if config.exists():
            data = json.loads(config.read_text(encoding="utf-8"))
            assert "bad" not in data.get("upstream_servers", {})

    def test_validate_success_writes_entry_with_tool_count(self, config):
        """Probe against the repo's fake MCP server — should report tool count.

        Runs via real subprocess rather than ``CliRunner`` because Click's
        runner replaces ``sys.stderr`` with a buffer that has no ``fileno()``,
        and the underlying MCP stdio client passes that stderr to a child
        ``asyncio.create_subprocess_exec`` call — which requires a real
        file descriptor. The live ``mms`` binary doesn't hit this."""
        import subprocess

        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "from memtomem_stm.cli.proxy import cli; cli()",
                "add",
                "fake",
                "--prefix",
                "fk",
                "--command",
                sys.executable,
                "--args",
                str(_FAKE_SERVER),
                "--validate",
                "--timeout",
                "15",
                "--config",
                str(config),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        assert "Validated:" in proc.stdout
        assert "tool(s) reachable" in proc.stdout
        assert "Added server 'fake'" in proc.stdout

        data = json.loads(config.read_text(encoding="utf-8"))
        assert data["upstream_servers"]["fake"]["command"] == sys.executable

    def test_validate_flag_absent_skips_probe(self, runner, config):
        """Without --validate, a nonexistent command is accepted (probe opt-in)."""
        result = runner.invoke(
            cli,
            [
                "add",
                "lazy",
                "--prefix",
                "lz",
                "--command",
                "__nonexistent_cmd_12345__",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 0
        assert "Validated" not in result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert "lazy" in data["upstream_servers"]

    def test_validate_probe_receives_headers(self, runner, config, monkeypatch):
        """`add --header ... --validate` must probe with the same headers it
        is about to persist — otherwise a header-authenticated server fails
        validation for an entry that would work at runtime."""
        from memtomem_stm.cli import proxy as proxy_mod

        probe_calls: list[dict] = []

        async def fake_probe_servers(servers, timeout):
            probe_calls.append(dict(servers))
            return {
                n: {"connected": True, "tools": 1, "overflowing": [], "error": None}
                for n in servers
            }

        monkeypatch.setattr(proxy_mod, "_probe_servers", fake_probe_servers)

        result = runner.invoke(
            cli,
            [
                "add",
                "api",
                "--prefix",
                "api",
                "--transport",
                "sse",
                "--url",
                "https://example.com/sse",
                "--header",
                "A=b",
                "--validate",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 0, result.output
        assert len(probe_calls) == 1
        assert probe_calls[0]["api"]["headers"] == {"A": "b"}


class TestAddJson:
    """``mms add --json`` result summary (#614). Contract shared by all
    mutating ``--json`` modes: stdout carries exactly one JSON document
    (human diagnostics stay on stderr — asserted via ``result.stdout``,
    which Click ≥8.2 exposes separately from the teed ``output``), every
    payload has ``action``/``ok``, failures add ``error``/``message`` and
    keep the non-JSON path's exit code."""

    def test_success_shape_redacts_env_values(self, runner, config):
        result = runner.invoke(
            cli,
            [
                "add", "gh", "--prefix", "gh", "--command", "npx",
                "--env", "GITHUB_TOKEN=ghp_supersecret", "--json", *_cfg_args(config),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["action"] == "add" and data["ok"] is True
        assert data["name"] == "gh" and data["prefix"] == "gh"
        assert data["server"]["env"]["GITHUB_TOKEN"] == REDACTED_DISPLAY
        assert data["validated"] is False and data["tools_reachable"] is None
        assert data["warnings"] == []
        # The secret must not appear anywhere in the JSON document, while
        # the on-disk config still carries it verbatim.
        assert "ghp_supersecret" not in result.stdout
        saved = json.loads(config.read_text(encoding="utf-8"))
        assert saved["upstream_servers"]["gh"]["env"]["GITHUB_TOKEN"] == "ghp_supersecret"

    def test_duplicate_name_error_shape(self, runner, config):
        runner.invoke(cli, ["add", "gh", "--prefix", "gh", "--command", "x", *_cfg_args(config)])
        result = runner.invoke(
            cli, ["add", "gh", "--prefix", "gh", "--command", "x", "--json", *_cfg_args(config)]
        )
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["ok"] is False and data["error"] == "already_exists"
        # The human stderr line still prints (wording shared across modes).
        assert "already exists" in result.stderr

    def test_invalid_prefix_error_shape(self, runner, config):
        result = runner.invoke(
            cli, ["add", "s", "--prefix", "9bad", "--command", "x", "--json", *_cfg_args(config)]
        )
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"] == "invalid_prefix"

    def test_missing_command_error_shape(self, runner, config):
        result = runner.invoke(cli, ["add", "s", "--prefix", "s", "--json", *_cfg_args(config)])
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"] == "stdio_requires_command"

    def test_success_shape_redacts_header_values(self, runner, config):
        result = runner.invoke(
            cli,
            [
                "add", "api", "--prefix", "api", "--transport", "sse",
                "--url", "https://example.com/sse",
                "--header", "Authorization=Bearer_ghp_supersecret", "--json",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert data["server"]["headers"]["Authorization"] == REDACTED_DISPLAY
        # The secret must not appear anywhere in the JSON document, while
        # the on-disk config still carries it verbatim.
        assert "ghp_supersecret" not in result.stdout
        saved = json.loads(config.read_text(encoding="utf-8"))
        headers = saved["upstream_servers"]["api"]["headers"]
        assert headers["Authorization"] == "Bearer_ghp_supersecret"

    def test_invalid_header_error_shape(self, runner, config):
        result = runner.invoke(
            cli,
            [
                "add", "s", "--prefix", "s", "--transport", "sse",
                "--url", "https://x", "--header", "MALFORMED", "--json",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"] == "invalid_header"

    def test_header_on_stdio_error_shape(self, runner, config):
        result = runner.invoke(
            cli,
            [
                "add", "s", "--prefix", "s", "--command", "x",
                "--header", "A=b", "--json", *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"] == "header_requires_http"

    def test_duplicate_prefix_error_shape_and_no_write(self, runner, config):
        runner.invoke(cli, ["add", "one", "--prefix", "fs", "--command", "x", *_cfg_args(config)])
        before = config.read_bytes()
        result = runner.invoke(
            cli, ["add", "two", "--prefix", "fs", "--command", "x", "--json", *_cfg_args(config)]
        )
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["ok"] is False and data["error"] == "duplicate_prefix"
        # The human stderr line still prints (wording shared across modes).
        assert "Duplicate upstream prefixes detected" in result.stderr
        assert config.read_bytes() == before

    def test_warnings_land_in_payload_and_stderr(self, runner, config, monkeypatch):
        """Non-abort warnings (here: the long-prefix soft warning) print to
        stderr AND land in the ``warnings`` array."""
        monkeypatch.delenv("MMS_CLIENT_SERVER_NAME", raising=False)
        warn_prefix = "a" * 22  # 1 over warn threshold, well under hard limit
        result = runner.invoke(
            cli,
            ["add", "s", "--prefix", warn_prefix, "--command", "x", "--json", *_cfg_args(config)],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert any("silently dropped" in w for w in data["warnings"])
        assert "silently dropped" in result.stderr

    def test_from_clients_is_usage_error(self, runner, config):
        result = runner.invoke(cli, ["add", "--from-clients", "--json", *_cfg_args(config)])
        assert result.exit_code == 2
        assert "--json is not supported with --from-clients" in result.output

    def test_validate_failure_error_shape_and_no_write(self, runner, config):
        result = runner.invoke(
            cli,
            [
                "add", "bad", "--prefix", "bad", "--command", "__nonexistent_cmd_12345__",
                "--validate", "--timeout", "3", "--json", *_cfg_args(config),
            ],
        )
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["error"] == "validation_failed"
        if config.exists():
            saved = json.loads(config.read_text(encoding="utf-8"))
            assert "bad" not in saved.get("upstream_servers", {})

    def test_validate_success_stdout_is_pure_json(self, config):
        """Progress lines (``Validating...`` / ``Validated:``) are suppressed
        in --json mode; ``tools_reachable`` carries the count instead. Real
        subprocess for the same fileno() reason as ``TestAddValidate``."""
        import subprocess

        proc = subprocess.run(
            [
                sys.executable, "-c",
                "from memtomem_stm.cli.proxy import cli; cli()",
                "add", "fake", "--prefix", "fk",
                "--command", sys.executable, "--args", str(_FAKE_SERVER),
                "--validate", "--timeout", "15", "--json", "--config", str(config),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        data = json.loads(proc.stdout)  # any progress line would break the parse
        assert data["ok"] is True and data["validated"] is True
        assert isinstance(data["tools_reachable"], int) and data["tools_reachable"] >= 1
        assert "Validating" not in proc.stdout


# ── init command (guided setup) ─────────────────────────────────────────


@pytest.fixture
def no_discovery(monkeypatch):
    """Stub discovery to an empty list so manual-flow tests aren't
    contaminated by whatever MCP configs happen to exist on the host
    running the tests (dev machine, CI, etc.)."""
    from memtomem_stm.cli import proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: [])


class TestInit:
    """`mms init` is an interactive wizard. Two entry points:

    * **Import flow:** if discovery finds servers registered in other MCP
      clients (Claude Code, Claude Desktop, ``.mcp.json``), the user picks
      from a numbered list and is only prompted for a prefix per pick.
    * **Manual flow:** discovery empty (or user picks ``none``) → falls
      back to free-form prompts (name → prefix → transport → command/url).

    Tests that only exercise the manual flow use the ``no_discovery``
    fixture to force an empty candidate list.

    The MCP-client registration step (``_run_mcp_integration``) is stubbed
    out here so existing tests don't shell out to ``claude`` on the test
    machine. Dedicated coverage for that flow lives in
    ``TestInitMcpRegistration`` / ``TestRegisterCommand``."""

    @pytest.fixture(autouse=True)
    def _stub_mcp_integration(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_run_mcp_integration", lambda *_a, **_kw: None)

    def test_init_happy_path_stdio_no_validate(self, runner, config, no_discovery):
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="filesystem\nfs\nstdio\nnpx\n-y @modelcontextprotocol/server-fs\n",
        )
        assert result.exit_code == 0, result.output
        assert "Saved to:" in result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        srv = data["upstream_servers"]["filesystem"]
        assert srv["prefix"] == "fs"
        assert srv["transport"] == "stdio"
        assert srv["command"] == "npx"
        assert srv["args"] == ["-y", "@modelcontextprotocol/server-fs"]

    def test_init_custom_config_warns_registered_entry_reads_default(
        self, runner, config, no_discovery
    ):
        # A non-default --config gets the management hints PLUS a note that
        # any registered MCP client entry boots reading the DEFAULT config —
        # otherwise the divergence is invisible until tools don't appear.
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="filesystem\nfs\nstdio\nnpx\n-y @modelcontextprotocol/server-fs\n",
        )
        assert result.exit_code == 0, result.output
        assert "Manage this config:" in result.output
        assert "MEMTOMEM_STM_PROXY__CONFIG_PATH" in result.output
        assert "DEFAULT config path" in result.output

    def test_init_aborts_if_config_exists(self, runner, config):
        """Init is for first-time setup only; `mms add` handles append."""
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["init", *_cfg_args(config)])
        assert result.exit_code == 1
        assert "config already exists" in result.output
        assert "mms add" in result.output
        assert "mms list" in result.output

    def test_init_invalid_prefix_reprompts(self, runner, config, no_discovery):
        """Bad prefix → re-ask instead of aborting; saves on the retry value."""
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="srv\n1bad\nfs\nstdio\ncmd\n\n",
        )
        assert result.exit_code == 0, result.output
        assert "Invalid: must start with a letter" in result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        assert data["upstream_servers"]["srv"]["prefix"] == "fs"

    def test_init_sse_transport_prompts_for_url(self, runner, config, no_discovery):
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="docs\ndocs\nsse\nhttps://docs.example.com/mcp\n",
        )
        assert result.exit_code == 0, result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        srv = data["upstream_servers"]["docs"]
        assert srv["transport"] == "sse"
        assert srv["url"] == "https://docs.example.com/mcp"
        assert "command" not in srv

    def test_init_validate_failure_still_saves_with_warning(self, config, tmp_path):
        """Validation is advisory: probe failure warns and continues (a flaky
        network shouldn't block setup). Uses a real subprocess to dodge
        CliRunner's stderr-fileno interaction (see TestAddValidate).

        ``HOME``/``cwd`` are redirected to an empty tmp dir so discovery
        finds no candidates and drops into the manual flow — otherwise the
        host's real ``~/.claude.json`` would leak in."""
        import subprocess

        isolated_home = tmp_path / "home"
        isolated_home.mkdir()
        isolated_cwd = tmp_path / "cwd"
        isolated_cwd.mkdir()

        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "from memtomem_stm.cli.proxy import cli; cli()",
                "init",
                "--config",
                str(config),
            ],
            # server name, prefix, transport, command, args, validate=y,
            # mcp-register choice = 3 (skip, prevents shelling out to the
            # host's real `claude` CLI / polluting cwd with .mcp.json)
            input="bad\nbad\nstdio\n__nonexistent_cmd_12345__\n\ny\n3\n",
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(isolated_cwd),
            env={**os.environ, "HOME": str(isolated_home)},
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        combined = proc.stdout + proc.stderr
        assert "probe failed" in combined
        assert "Saving config anyway" in combined
        assert "Saved to:" in proc.stdout

        data = json.loads(config.read_text(encoding="utf-8"))
        assert "bad" in data["upstream_servers"]


# ── init: language preset (--lang) ──────────────────────────────────────


class TestInitLangPreset:
    """``mms init --lang`` selects a token-aware budget preset.

    PR #274 ships KO calibration; EN is the empty-default preset. The
    flag is the scriptable path — non-TTY callers without ``--lang`` get
    "en" silently, which keeps the entire ``TestInit`` suite (and any
    third-party scripted caller) from needing to feed an extra prompt
    line. Existing tests in ``TestInit`` already pin that contract by
    not supplying any lang input and still passing.
    """

    @pytest.fixture(autouse=True)
    def _stub_mcp_integration(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_run_mcp_integration", lambda *_a, **_kw: None)

    def test_lang_ko_writes_proxy_and_per_server_fields(self, runner, config, no_discovery):
        """KO preset writes proxy-level chars_per_token /
        default_max_result_chars AND per-server max_result_tokens +
        chars_per_token. The hardcoded manual-flow ``max_result_chars=8000``
        stays in place — token budget wins via PR #274 precedence.

        The written config is also round-tripped through
        ``ProxyConfig.load_from_file`` so that a preset key the schema does
        not actually accept (a silently-dropped top-level field, e.g. the old
        top-level ``min_response_chars`` that only exists on the nested
        ``ExtractionConfig``) fails here instead of looking applied in the
        raw JSON."""
        result = runner.invoke(
            cli,
            ["init", "--no-validate", "--lang", "ko", *_cfg_args(config)],
            input="filesystem\nfs\nstdio\nnpx\n-y @modelcontextprotocol/server-fs\n",
        )
        assert result.exit_code == 0, result.output

        data = json.loads(config.read_text(encoding="utf-8"))

        # Proxy-level fields
        assert data["chars_per_token"] == 1.85
        assert data["default_max_result_chars"] == 8500
        # The preset must NOT emit a top-level ``min_response_chars``: it is
        # not a ``ProxyConfig`` field (only ``ExtractionConfig`` has one), so
        # writing it top-level is silently dropped on load — a no-op that
        # advertised a budget it never applied.
        assert "min_response_chars" not in data

        # Per-server fields
        srv = data["upstream_servers"]["filesystem"]
        assert srv["max_result_tokens"] == 2000
        assert srv["chars_per_token"] == 1.85
        # Manual-flow hardcode preserved (token wins at resolution time)
        assert srv["max_result_chars"] == 8000

        # Round-trip every written proxy-level key through schema validation
        # and assert the *effective* values — this is what catches a
        # silently-dropped (dead) preset key, which a raw-JSON assertion
        # cannot.
        from memtomem_stm.proxy.config import ProxyConfig

        loaded = ProxyConfig.load_from_file(config)
        assert loaded is not None
        assert loaded.chars_per_token == 1.85
        assert loaded.default_max_result_chars == 8500
        # The extraction gate keeps its default — the KO preset never touched
        # it (the removed top-level key would not have reached it anyway).
        assert loaded.extraction.min_response_chars == 500
        loaded_srv = loaded.upstream_servers["filesystem"]
        assert loaded_srv.max_result_tokens == 2000
        assert loaded_srv.chars_per_token == 1.85

    def test_lang_en_writes_no_language_specific_fields(self, runner, config, no_discovery):
        """EN preset is a no-op on the data dict — config matches pre-PR
        behavior exactly. This is the contract that lets existing TestInit
        tests pass without supplying a lang prompt."""
        result = runner.invoke(
            cli,
            ["init", "--no-validate", "--lang", "en", *_cfg_args(config)],
            input="filesystem\nfs\nstdio\nnpx\n-y @modelcontextprotocol/server-fs\n",
        )
        assert result.exit_code == 0, result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        assert "chars_per_token" not in data
        assert "min_response_chars" not in data
        assert "default_max_result_chars" not in data

        srv = data["upstream_servers"]["filesystem"]
        assert "max_result_tokens" not in srv
        assert "chars_per_token" not in srv

    def test_no_lang_flag_non_tty_defaults_to_en(self, runner, config, no_discovery):
        """Non-TTY callers (CliRunner is non-TTY) skip the prompt and get EN.

        This pins the contract that the lang prompt does not consume a
        stdin line in scripted contexts — load-bearing for backward
        compat with TestInit."""
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="filesystem\nfs\nstdio\nnpx\n-y @modelcontextprotocol/server-fs\n",
        )
        assert result.exit_code == 0, result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        assert "chars_per_token" not in data
        srv = data["upstream_servers"]["filesystem"]
        assert "max_result_tokens" not in srv

    def test_lang_invalid_choice_rejected(self, runner, config, no_discovery):
        """Click validates --lang against the preset list; bad value aborts
        before any prompt fires (no config side-effect)."""
        result = runner.invoke(
            cli,
            ["init", "--no-validate", "--lang", "xx", *_cfg_args(config)],
            input="filesystem\nfs\nstdio\nnpx\n\n",
        )
        assert result.exit_code != 0
        assert not config.exists()

    def test_lang_ko_with_no_servers_imported_writes_nothing(self, runner, config, monkeypatch):
        """If discovery yields candidates and the user picks none, the wizard
        returns early before save — no config is written even with --lang.
        Pins that --lang doesn't sneak past the empty-pick guard."""
        from memtomem_stm.cli import proxy as proxy_mod

        # Force discovery to surface a candidate (so the import flow runs)
        # but stub _pick_imports to return [] (user toggled nothing).
        monkeypatch.setattr(
            proxy_mod,
            "_discover_candidates",
            lambda _cwd: [
                {
                    "name": "fs",
                    "entry": {"command": "npx", "args": ["-y", "fs"]},
                    "source": "test",
                }
            ],
        )
        monkeypatch.setattr(proxy_mod, "_pick_imports", lambda _c: [])

        result = runner.invoke(
            cli,
            ["init", "--no-validate", "--lang", "ko", *_cfg_args(config)],
        )
        assert result.exit_code == 0
        assert "No servers selected" in result.output
        assert not config.exists()


# ── init: discovery + import helpers ────────────────────────────────────


class TestInitDiscoveryHelpers:
    """Pure-function tests for discovery building blocks. Keep these fast
    and source-free (no file I/O) so the import flow can be reasoned about
    piece by piece."""

    def test_normalize_stdio_entry(self):
        from memtomem_stm.cli.proxy import _normalize_client_entry

        entry = _normalize_client_entry(
            {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]}
        )
        assert entry is not None
        assert entry["transport"] == "stdio"
        assert entry["command"] == "npx"
        assert entry["args"] == ["-y", "@modelcontextprotocol/server-filesystem"]
        # Imported entries get our default compression/max_chars policy.
        assert entry["compression"] == "auto"
        assert entry["max_result_chars"] == 8000
        # Prefix is intentionally absent — the caller prompts per-server.
        assert "prefix" not in entry

    def test_normalize_http_entry(self):
        from memtomem_stm.cli.proxy import _normalize_client_entry

        entry = _normalize_client_entry({"type": "http", "url": "https://example.com/mcp"})
        assert entry is not None
        assert entry["transport"] == "streamable_http"
        assert entry["url"] == "https://example.com/mcp"

    def test_normalize_sse_entry(self):
        from memtomem_stm.cli.proxy import _normalize_client_entry

        entry = _normalize_client_entry({"type": "sse", "url": "https://example.com/sse"})
        assert entry is not None
        assert entry["transport"] == "sse"

    def test_normalize_strips_dangerous_env(self):
        """Same policy as `mms add --env`: never import env keys that could
        hijack a spawned process."""
        from memtomem_stm.cli.proxy import _normalize_client_entry

        entry = _normalize_client_entry(
            {
                "command": "node",
                "args": ["srv.js"],
                "env": {"API_KEY": "ok", "LD_PRELOAD": "/evil.so"},
            }
        )
        assert entry is not None
        assert entry["env"] == {"API_KEY": "ok"}

    @pytest.mark.parametrize("type_hint", ["http", "sse"])
    def test_normalize_copies_headers_for_http_and_sse(self, type_hint):
        """HTTP headers carry auth for remote servers — dropping them at
        import time made header-authenticated servers fail behind STM while
        the direct registration kept working. Unlike env, no dangerous-key
        filter applies (headers never touch a subprocess environment).
        Non-str values are coerced like env values."""
        from memtomem_stm.cli.proxy import _normalize_client_entry

        entry = _normalize_client_entry(
            {
                "type": type_hint,
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer t", "X-Int": 1},
            }
        )
        assert entry is not None
        assert entry["headers"] == {"Authorization": "Bearer t", "X-Int": "1"}

    @pytest.mark.parametrize("headers", [["Authorization: x"], {}, "Authorization=x", None])
    def test_normalize_ignores_non_dict_or_empty_headers(self, headers):
        from memtomem_stm.cli.proxy import _normalize_client_entry

        raw: dict = {"type": "http", "url": "https://example.com/mcp"}
        if headers is not None:
            raw["headers"] = headers
        entry = _normalize_client_entry(raw)
        assert entry is not None
        assert "headers" not in entry

    def test_normalize_rejects_unsupported_shape(self):
        """No command + no url + no recognized type → can't import."""
        from memtomem_stm.cli.proxy import _normalize_client_entry

        assert _normalize_client_entry({}) is None
        assert _normalize_client_entry({"type": "http"}) is None  # url missing
        assert _normalize_client_entry({"command": ""}) is None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", []),
            ("none", []),
            ("all", [0, 1, 2]),
            ("ALL", [0, 1, 2]),
            ("1", [0]),
            ("1,3", [0, 2]),
            (" 2 , 1 ", [1, 0]),  # preserves user's ordering
            ("1,1,2", [0, 1]),  # dedupes
        ],
    )
    def test_parse_selection_valid(self, raw, expected):
        from memtomem_stm.cli.proxy import _parse_selection

        assert _parse_selection(raw, 3) == expected

    @pytest.mark.parametrize("raw", ["0", "4", "1,4", "abc", "1-3", "1.5"])
    def test_parse_selection_invalid(self, raw):
        """Out-of-range, ranges, and non-digit tokens reprompt (None)."""
        from memtomem_stm.cli.proxy import _parse_selection

        assert _parse_selection(raw, 3) is None

    def test_suggest_prefix_sanitizes_and_dedupes(self):
        from memtomem_stm.cli.proxy import _suggest_prefix

        assert _suggest_prefix("filesystem", set()) == "filesystem"
        # dashes / dots → underscores
        assert _suggest_prefix("my-server.name", set()) == "my_server_name"
        # leading digit → prefixed so it matches _PREFIX_RE
        assert _suggest_prefix("123", set()) == "s_123"
        # collision → numbered suffix
        assert _suggest_prefix("fs", {"fs"}) == "fs2"
        assert _suggest_prefix("fs", {"fs", "fs2"}) == "fs3"

    def test_is_self_reference_blocks_recursion(self):
        """STM (mms/memtomem-stm) and the LTM companion (memtomem/
        memtomem-server) must never be imported as upstream servers.

        STM self-proxy → infinite loop on tool calls.
        LTM as upstream → double-registered, since STM already reaches LTM
        via a separate mechanism (CLAUDE.md pipeline invariants)."""
        from memtomem_stm.cli.proxy import _is_self_reference

        # STM itself (command basename).
        assert _is_self_reference({"command": "mms"})
        assert _is_self_reference({"command": "/usr/local/bin/memtomem-stm"})
        assert _is_self_reference({"command": "memtomem-stm-proxy"})
        # LTM companion (command basename).
        assert _is_self_reference({"command": "memtomem-server"})
        assert _is_self_reference({"command": "memtomem"})
        # LTM via uvx wrapper — the blocked name hides in args, not command.
        # This is the shape Claude Code writes: `uvx --from memtomem memtomem-server`.
        assert _is_self_reference(
            {"command": "uvx", "args": ["--from", "memtomem", "memtomem-server"]}
        )
        # Legitimate neighbor names must not be over-matched (substring
        # collisions would flag user-written `memtomem-foo-bar`).
        assert not _is_self_reference({"command": "npx"})
        assert not _is_self_reference({"url": "http://x"})
        assert not _is_self_reference({"command": "uvx", "args": ["--from", "memtomem-notes"]})


class TestInitDiscoverySources:
    """`_discover_candidates` reads three source files (project `.mcp.json`,
    `~/.claude.json`, Claude Desktop config) and dedupes by name in that
    priority order. Each test sets up a sandbox HOME + cwd to simulate a
    user with specific configs already present."""

    def _setup_home(self, tmp_path: Path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        set_home(monkeypatch, home)
        # Redirect the macOS-specific Desktop path into our sandbox too.
        desktop = home / "Library/Application Support/Claude"
        desktop.mkdir(parents=True)
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(
            proxy_mod,
            "_desktop_config_path",
            lambda: desktop / "claude_desktop_config.json",
        )
        return home, cwd, desktop

    def test_discovers_claude_code_user_scope(self, tmp_path, monkeypatch):
        from memtomem_stm.cli.proxy import _discover_candidates

        home, cwd, _ = self._setup_home(tmp_path, monkeypatch)
        (home / ".claude.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "filesystem": {"command": "npx", "args": ["-y", "@fs"]},
                        "docs": {"type": "http", "url": "https://docs.example/mcp"},
                    }
                }
            ),
            encoding="utf-8",
        )

        cands = _discover_candidates(cwd)
        names = {c["name"] for c in cands}
        assert names == {"filesystem", "docs"}
        fs = next(c for c in cands if c["name"] == "filesystem")
        assert fs["source"] == "Claude Code (user)"
        assert fs["entry"]["transport"] == "stdio"
        # Origin capture (#475): verbatim raw + structured source ref.
        assert fs["raw"] == {"command": "npx", "args": ["-y", "@fs"]}
        assert fs["source_ref"] == {"kind": "claude-user"}

    def test_discovers_claude_code_project_scope_with_path(self, tmp_path, monkeypatch):
        """Per-project entries in ``~/.claude.json`` carry the resolved cwd in
        ``source_ref.path`` — ``mms eject`` restores via ``claude mcp add-json
        -s local``, which resolves the project by cwd (#475)."""
        from memtomem_stm.cli.proxy import _discover_candidates

        home, cwd, _ = self._setup_home(tmp_path, monkeypatch)
        (home / ".claude.json").write_text(
            json.dumps(
                {
                    "projects": {
                        str(cwd.resolve()): {
                            "mcpServers": {"proj-tool": {"command": "node", "args": ["a.js"]}}
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        cands = _discover_candidates(cwd)
        assert len(cands) == 1
        assert cands[0]["source"] == "Claude Code (project)"
        assert cands[0]["source_ref"] == {
            "kind": "claude-project",
            "path": str(cwd.resolve()),
        }
        assert cands[0]["raw"] == {"command": "node", "args": ["a.js"]}

    def test_raw_is_verbatim_where_normalization_is_lossy(self, tmp_path, monkeypatch):
        """``_normalize_client_entry`` drops dangerous env keys and unknown
        fields (HTTP ``headers`` are copied since the headers-plumbing
        change). ``raw`` must keep everything verbatim — it is the restore
        payload for ``mms eject`` (#475)."""
        from memtomem_stm.cli.proxy import _discover_candidates

        home, cwd, _ = self._setup_home(tmp_path, monkeypatch)
        raw_entry = {
            "type": "http",
            "url": "https://api.example/mcp",
            "headers": {"Authorization": "Bearer tok"},
            "unknown_host_field": True,
        }
        (home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"api": raw_entry}}),
            encoding="utf-8",
        )

        cands = _discover_candidates(cwd)
        assert len(cands) == 1
        assert cands[0]["raw"] == raw_entry
        assert cands[0]["entry"]["headers"] == {"Authorization": "Bearer tok"}
        assert "unknown_host_field" not in cands[0]["entry"]

    def test_discovers_desktop_config(self, tmp_path, monkeypatch):
        from memtomem_stm.cli.proxy import _discover_candidates

        _, cwd, desktop = self._setup_home(tmp_path, monkeypatch)
        (desktop / "claude_desktop_config.json").write_text(
            json.dumps({"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}),
            encoding="utf-8",
        )

        cands = _discover_candidates(cwd)
        assert len(cands) == 1
        assert cands[0]["name"] == "fetch"
        assert cands[0]["source"] == "Claude Desktop"

    def test_discovers_project_mcp_json_in_cwd(self, tmp_path, monkeypatch):
        from memtomem_stm.cli.proxy import _discover_candidates

        _, cwd, _ = self._setup_home(tmp_path, monkeypatch)
        (cwd / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"dev-tool": {"command": "node", "args": ["x.js"]}}}),
            encoding="utf-8",
        )

        cands = _discover_candidates(cwd)
        assert len(cands) == 1
        assert cands[0]["source"] == ".mcp.json (project)"
        assert cands[0]["source_ref"] == {
            "kind": "mcp-json",
            "path": str((cwd / ".mcp.json").resolve()),
        }

    def test_dedupes_by_name_priority(self, tmp_path, monkeypatch):
        """Same name in multiple sources → first-priority wins, others
        recorded in ``duplicate_in`` so the UI can show the overlap."""
        from memtomem_stm.cli.proxy import _discover_candidates

        home, cwd, desktop = self._setup_home(tmp_path, monkeypatch)
        (cwd / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"shared": {"command": "a"}}}),
            encoding="utf-8",
        )
        (home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"shared": {"command": "b"}}}),
            encoding="utf-8",
        )
        (desktop / "claude_desktop_config.json").write_text(
            json.dumps({"mcpServers": {"shared": {"command": "c"}}}),
            encoding="utf-8",
        )

        cands = _discover_candidates(cwd)
        assert len(cands) == 1
        assert cands[0]["entry"]["command"] == "a"  # .mcp.json wins
        assert cands[0]["duplicate_in"] == ["Claude Code (user)", "Claude Desktop"]
        # Structured duplicate records (#475): every losing source keeps its
        # kind + verbatim raw so origin.duplicates and the prune backup log
        # (PR2) can address each source individually.
        assert cands[0]["duplicates"] == [
            {
                "label": "Claude Code (user)",
                "source_ref": {"kind": "claude-user"},
                "raw": {"command": "b"},
            },
            {
                "label": "Claude Desktop",
                "source_ref": {"kind": "claude-desktop"},
                "raw": {"command": "c"},
            },
        ]

    def test_skips_self_reference(self, tmp_path, monkeypatch):
        """Imports of STM (``mms``) or LTM (``memtomem-server``, either as
        direct command or under ``uvx --from memtomem``) are filtered so
        users never accidentally configure STM to proxy itself or to
        double-register the LTM companion."""
        from memtomem_stm.cli.proxy import _discover_candidates

        home, cwd, _ = self._setup_home(tmp_path, monkeypatch)
        (home / ".claude.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "mms-self": {"command": "mms"},
                        "memtomem": {
                            "command": "uvx",
                            "args": ["--from", "memtomem", "memtomem-server"],
                        },
                        "real": {"command": "npx", "args": ["x"]},
                    }
                }
            ),
            encoding="utf-8",
        )

        cands = _discover_candidates(cwd)
        assert [c["name"] for c in cands] == ["real"]

    def test_missing_and_malformed_sources_are_silent(self, tmp_path, monkeypatch):
        """Discovery is best-effort: a malformed JSON file shouldn't crash
        ``mms init`` — it just yields nothing from that source."""
        from memtomem_stm.cli.proxy import _discover_candidates

        home, cwd, _ = self._setup_home(tmp_path, monkeypatch)
        (home / ".claude.json").write_text("{ not json", encoding="utf-8")

        assert _discover_candidates(cwd) == []


class TestInitImportFlow:
    """End-to-end ``mms init`` when discovery finds candidates. We stub
    ``_discover_candidates`` directly to focus on the select + prefix UI;
    source parsing is covered in ``TestInitDiscoverySources``.

    Same ``_run_mcp_integration`` stub as ``TestInit`` — the MCP-client
    registration step is tested independently in ``TestInitMcpRegistration``.
    ``_handle_source_prune`` is stubbed for the same reason: this class is
    about the select + prefix UI, and prune coverage lives in
    ``TestInitPruneOriginals`` / ``TestPruneCommand``."""

    @pytest.fixture(autouse=True)
    def _stub_post_save(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_run_mcp_integration", lambda *_a, **_kw: None)
        monkeypatch.setattr(proxy_mod, "_handle_source_prune", lambda *_a, **_kw: None)

    def _stub_candidates(self, monkeypatch, candidates):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: candidates)

    def test_import_all_imports_every_candidate(self, runner, config, monkeypatch):
        """Default answer 'all' + accept suggested prefixes → every
        discovered server is imported with transport/command preserved."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "Claude Code (user)",
                    "entry": {
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "@fs"],
                        "compression": "auto",
                        "max_result_chars": 8000,
                    },
                },
                {
                    "name": "docs",
                    "source": "Claude Desktop",
                    "entry": {
                        "transport": "streamable_http",
                        "url": "https://docs.example/mcp",
                        "compression": "auto",
                        "max_result_chars": 8000,
                    },
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="all\n\n\n",  # pick all, accept default prefix for each
        )
        assert result.exit_code == 0, result.output
        assert "Found 2 MCP server" in result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        servers = data["upstream_servers"]
        assert set(servers) == {"filesystem", "docs"}
        assert servers["filesystem"]["command"] == "npx"
        assert servers["filesystem"]["prefix"] == "filesystem"  # default suggestion
        assert servers["docs"]["url"] == "https://docs.example/mcp"

    def test_user_typed_duplicate_prefix_reprompts(self, runner, config, monkeypatch):
        """Overriding the suggested prefix with one already claimed in the
        same run re-prompts instead of saving a config the runtime pydantic
        validator would refuse to load."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "a",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "ca"},
                },
                {
                    "name": "b",
                    "source": "Y",
                    "entry": {"transport": "stdio", "command": "cb"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            # pick all; accept default 'a'; type colliding 'a' for the
            # second server (re-prompt), then a unique 'b2'.
            input="all\n\na\nb2\n",
        )
        assert result.exit_code == 0, result.output
        assert "already used" in result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert data["upstream_servers"]["a"]["prefix"] == "a"
        assert data["upstream_servers"]["b"]["prefix"] == "b2"

    def test_import_subset_by_number(self, runner, config, monkeypatch):
        """Numeric picks only import the chosen servers — not all of them."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "a",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "ca"},
                },
                {
                    "name": "b",
                    "source": "Y",
                    "entry": {"transport": "stdio", "command": "cb"},
                },
                {
                    "name": "c",
                    "source": "Z",
                    "entry": {"transport": "stdio", "command": "cc"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="1,3\n\n\n",  # pick a + c, default prefixes
        )
        assert result.exit_code == 0, result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert set(data["upstream_servers"]) == {"a", "c"}

    def test_empty_selection_exits_without_saving(self, runner, config, monkeypatch):
        """When candidates are present but the user picks none (``none`` in
        fallback / Cancel in TUI / Confirm-with-nothing-toggled), we exit
        cleanly without writing a config. The previous behavior of
        auto-dropping into a 'Adding a server manually:' prompt was
        confusing — users who didn't pick anything almost never intended to
        type a new server inline. They either wanted to re-think (rerun
        init) or run ``mms add`` explicitly."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "Claude Code",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="none\n",
        )
        assert result.exit_code == 0, result.output
        assert "No servers selected" in result.output
        # The guidance points users at the two legitimate next steps.
        assert "rerun `mms init`" in result.output
        assert "mms add" in result.output
        # Nothing saved — a subsequent `init` should not see a pre-existing config.
        assert not config.exists()

    def test_invalid_selection_reprompts(self, runner, config, monkeypatch):
        """Out-of-range indices don't save garbage; they reprompt."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "one",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "x"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="5\n1\n\n",  # bad → retry with "1" → accept default prefix
        )
        assert result.exit_code == 0, result.output
        assert "Invalid:" in result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert "one" in data["upstream_servers"]

    def test_tty_select_loop_toggles_and_confirms(self, runner, config, monkeypatch):
        """TUI path uses ``questionary.select`` in a loop: Enter on an
        item toggles it, Enter on the Confirm sentinel commits. CliRunner
        can't fake a TTY, so we force ``_should_use_tui`` True and stub
        ``questionary.select`` to script a fixed sequence of user actions."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)

        # Sequence the fake user will perform: toggle 0, toggle 2, Confirm.
        # Each questionary.select() call returns the next scripted action.
        actions = iter([0, 2, proxy_mod._TUI_CONFIRM])
        observed_choice_counts: list[int] = []

        class _Scripted:
            def __init__(self, result):
                self._result = result

            def ask(self):
                return self._result

        def fake_select(message, choices, **kwargs):
            # Accept arbitrary kwargs (style, default, use_arrow_keys,
            # use_jk_keys, use_emacs_keys) so this stub survives future
            # param additions without brittle signature drift.
            observed_choice_counts.append(len(choices))
            return _Scripted(next(actions))

        import questionary

        monkeypatch.setattr(questionary, "select", fake_select)

        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "a",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "ca"},
                },
                {
                    "name": "b",
                    "source": "Y",
                    "entry": {"transport": "stdio", "command": "cb"},
                },
                {
                    "name": "c",
                    "source": "Z",
                    "entry": {"transport": "stdio", "command": "cc"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="\n\n",  # default prefix for each of the 2 picked servers
        )
        assert result.exit_code == 0, result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert set(data["upstream_servers"]) == {"a", "c"}  # index 1 ("b") not toggled
        # 3 iterations of the select loop: 2 toggles + 1 Confirm.
        assert len(observed_choice_counts) == 3
        # Each render shows: 3 candidates + Separator + Confirm + Cancel = 6 items.
        assert all(n == 6 for n in observed_choice_counts)

    def test_tui_cancel_exits_without_saving(self, runner, config, monkeypatch):
        """Cancel sentinel (or Ctrl-C → None from questionary) exits the
        wizard cleanly without writing a config. Contrast with the older
        behavior that silently dropped into a manual name prompt — users
        found that confusing when they just wanted to bail out."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)

        class _Scripted:
            def ask(self):
                return proxy_mod._TUI_CANCEL

        import questionary

        monkeypatch.setattr(questionary, "select", lambda *a, **kw: _Scripted())

        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "x",
                    "source": "src",
                    "entry": {"transport": "stdio", "command": "xx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="",  # no prompts should fire after Cancel
        )
        assert result.exit_code == 0, result.output
        assert "No servers selected" in result.output
        assert not config.exists()

    def test_tui_confirm_with_nothing_toggled_exits_without_saving(
        self, runner, config, monkeypatch
    ):
        """Confirm with an empty selection is semantically 'I'm done and
        I picked nothing' — same outcome as Cancel, since there's nothing
        to save."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)

        class _Scripted:
            def ask(self):
                return proxy_mod._TUI_CONFIRM

        import questionary

        monkeypatch.setattr(questionary, "select", lambda *a, **kw: _Scripted())

        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "x",
                    "source": "src",
                    "entry": {"transport": "stdio", "command": "xx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
        )
        assert result.exit_code == 0, result.output
        assert "No servers selected" in result.output
        assert not config.exists()

    def test_non_default_config_surfaces_management_hint(self, runner, config, monkeypatch):
        """When `--config` deviates from ``~/.memtomem/stm_proxy.json``
        the tail of the output must print ``mms list --config <path>``
        / ``mms health --config <path>`` so the user doesn't get tripped
        up by ``mms list`` silently reading the empty default config.
        Caught during dogfooding with a throwaway ``/tmp/*.json`` test
        path — the gap was confusing."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "only",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "c"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output
        assert "Manage this config:" in result.output
        # Hints must name the actual resolved path so the user can copy
        # them verbatim — no ``{path}`` placeholder leakage.
        assert f"mms list --config {config.resolve()}" in result.output
        assert f"mms health --config {config.resolve()}" in result.output

    def test_default_config_does_not_show_management_hint(self, runner, tmp_path, monkeypatch):
        """Inverse of the above: when the user accepts the default
        config path, the hint is noise and should not print. We stub
        ``_DEFAULT_CONFIG`` to a tmp path so this test can match the
        "default" case without actually writing to the user's real
        ``~/.memtomem/stm_proxy.json``."""
        from memtomem_stm.cli import proxy as proxy_mod

        fake_default = tmp_path / "default_stm_proxy.json"
        monkeypatch.setattr(proxy_mod, "_DEFAULT_CONFIG", fake_default)

        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "only",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "c"},
                },
            ],
        )
        # Invoke without --config so the command picks up the (patched) default.
        result = runner.invoke(
            cli,
            ["init", "--no-validate", "--config", str(fake_default)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output
        assert "Manage this config:" not in result.output

    def test_import_prompts_prefix_per_pick(self, runner, config, monkeypatch):
        """User-entered prefix overrides the suggested default, and each
        selected server gets its own prompt."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
                {
                    "name": "github",
                    "source": "Y",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="all\nfs\ngh\n\n",  # custom prefixes for both
        )
        assert result.exit_code == 0, result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert data["upstream_servers"]["filesystem"]["prefix"] == "fs"
        assert data["upstream_servers"]["github"]["prefix"] == "gh"


# ── MCP client registration (3-way prompt) ──────────────────────────────


class TestRunClaudeMcp:
    """The single shell-out seam ``_run_claude_mcp`` must pin
    ``encoding="utf-8"`` so non-ASCII output from the ``claude`` CLI
    (em-dash, localized error strings, box drawing) doesn't crash on
    Windows consoles whose default codec is cp1252/cp949. Regression
    for memtomem-stm#302 P0."""

    def test_passes_explicit_utf8_encoding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        from memtomem_stm.cli import proxy as proxy_mod

        captured: dict[str, object] = {}

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(
                args=list(args[0]) if args else [], returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(proxy_mod.subprocess, "run", fake_run)
        proxy_mod._run_claude_mcp(["claude", "mcp", "list"])

        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs.get("encoding") == "utf-8"
        assert kwargs.get("errors") == "replace"
        assert kwargs.get("text") is True
        assert kwargs.get("capture_output") is True


class _FakeClaudeResult:
    """Lightweight stand-in for ``subprocess.CompletedProcess[str]``. Using
    a plain object avoids pinning the test to a specific stdlib version's
    constructor signature."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = stderr


class TestImportOriginCapture:
    """Both import paths (``mms init`` + ``mms add --import``) persist an
    ``origin`` provenance block per imported entry (#475 PR1): structured
    source kind(s), import timestamp, and the **verbatim** host entry — the
    restore payload for ``mms eject``. Manual entries get none.

    ``_run_mcp_integration`` / ``_handle_source_prune`` are stubbed as in
    ``TestInitImportFlow`` — this class is about what gets persisted, not the
    post-save steps."""

    @pytest.fixture(autouse=True)
    def _stub_post_save(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_run_mcp_integration", lambda *_a, **_kw: None)
        monkeypatch.setattr(proxy_mod, "_handle_source_prune", lambda *_a, **_kw: None)

    def _stub_candidates(self, monkeypatch, candidates):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: candidates)

    @staticmethod
    def _github_candidate() -> dict:
        """Full discovery-shaped candidate: lossy-normalized entry + verbatim
        raw (keeps the env secret + a field normalization drops) + one
        duplicate source."""
        return {
            "name": "github",
            "source": "Claude Code (user)",
            "entry": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@gh"],
                "compression": "auto",
                "max_result_chars": 8000,
            },
            "raw": {
                "command": "npx",
                "args": ["-y", "@gh"],
                "env": {"GITHUB_TOKEN": "ghp_secret"},
                "host_only_field": True,
            },
            "source_ref": {"kind": "claude-user"},
            "duplicate_in": ["Claude Desktop"],
            "duplicates": [
                {
                    "label": "Claude Desktop",
                    "source_ref": {"kind": "claude-desktop"},
                    "raw": {"command": "npx", "args": ["-y", "@gh"]},
                }
            ],
        }

    def _assert_origin_block(self, entry: dict, cand: dict) -> None:
        origin = entry["origin"]
        assert origin["schema_version"] == 1
        assert origin["source"] == {"kind": "claude-user", "pruned": False}
        assert origin["duplicates"] == [{"kind": "claude-desktop", "pruned": False}]
        # Verbatim deep-equality — normalization loss must not leak into the
        # provenance copy.
        assert origin["original"] == cand["raw"]
        assert origin["imported_at"].endswith("Z")
        # Lockstep pin: what the CLI wrote validates against the schema the
        # server documents.
        from memtomem_stm.proxy.config import UpstreamOrigin

        assert UpstreamOrigin.model_validate(origin).source.kind == "claude-user"

    def test_init_import_writes_origin_block(self, runner, config, monkeypatch):
        cand = self._github_candidate()
        self._stub_candidates(monkeypatch, [cand])
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        self._assert_origin_block(data["upstream_servers"]["github"], cand)

    def test_add_import_writes_origin_block(self, runner, config, monkeypatch):
        config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}), encoding="utf-8")
        cand = self._github_candidate()
        self._stub_candidates(monkeypatch, [cand])
        result = runner.invoke(
            cli,
            ["add", "--import", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        self._assert_origin_block(data["upstream_servers"]["github"], cand)

    def test_candidate_without_raw_gets_no_origin(self, runner, config, monkeypatch):
        """A candidate that never captured a verbatim raw (hand-constructed)
        produces no origin block at all — a partial block could not drive a
        faithful restore."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "legacy",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                }
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        assert "origin" not in data["upstream_servers"]["legacy"]

    def test_manual_init_flow_writes_no_origin(self, runner, config, no_discovery):
        """Origin is import-only provenance — the manual prompt flow has no
        host entry to record."""
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="filesystem\nfs\nstdio\nnpx\n-y @fs\n",
        )
        assert result.exit_code == 0, result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        assert "origin" not in data["upstream_servers"]["filesystem"]

    def test_init_roundtrip_preserves_verbatim_original_from_real_files(
        self, tmp_path, monkeypatch, runner
    ):
        """End-to-end through real discovery (no stubs): host file → init →
        config. ``origin.original`` deep-equals the host entry including
        fields the STM entry drops."""
        home = tmp_path / "home"
        home.mkdir()
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        set_home(monkeypatch, home)
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_desktop_config_path", lambda: home / "no_desktop.json")
        host_entry = {
            "command": "npx",
            "args": ["-y", "@gh"],
            "env": {"GITHUB_TOKEN": "ghp_secret", "LD_PRELOAD": "/evil.so"},
        }
        (home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"github": host_entry}}), encoding="utf-8"
        )
        monkeypatch.chdir(cwd)

        config = tmp_path / "stm_proxy.json"
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        entry = data["upstream_servers"]["github"]
        assert entry["origin"]["original"] == host_entry
        assert entry["origin"]["source"] == {"kind": "claude-user", "pruned": False}
        # The dangerous env key is filtered from the *active* entry (forward
        # import) but kept verbatim in the provenance copy — eject restores
        # what the host originally had (#475 §7).
        assert "LD_PRELOAD" not in (entry.get("env") or {})

    def test_other_cli_commands_preserve_origin(self, runner, config, monkeypatch):
        """The CLI's raw-dict load/save passthrough keeps origin intact across
        unrelated mutations (``mms surfacing``, ``mms remove`` of a sibling)."""
        cand = self._github_candidate()
        self._stub_candidates(monkeypatch, [cand])
        runner.invoke(cli, ["init", "--no-validate", *_cfg_args(config)], input="all\n\n")
        runner.invoke(
            cli,
            ["add", "other", "--prefix", "ot", "--command", "uvx", *_cfg_args(config)],
        )
        before = json.loads(config.read_text(encoding="utf-8"))
        origin_before = before["upstream_servers"]["github"]["origin"]

        result = runner.invoke(cli, ["surfacing", "github", "off", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        result = runner.invoke(cli, ["remove", "other", "--yes", *_cfg_args(config)])
        assert result.exit_code == 0, result.output

        after = json.loads(config.read_text(encoding="utf-8"))
        assert after["upstream_servers"]["github"]["origin"] == origin_before
        assert after["upstream_servers"]["github"]["surfacing_enabled"] is False


class TestInitMcpRegistration:
    """The new 3-way prompt at the end of ``mms init`` (and the re-entry
    point ``mms register``): Claude Code auto-register / ``.mcp.json``
    generation / skip.

    These tests exercise the real ``_run_mcp_integration`` flow (no autouse
    stub) and replace ``_run_claude_mcp`` with an in-process recorder so
    assertions can check exact argv."""

    @pytest.fixture
    def no_discovery(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: [])

    @pytest.fixture
    def fake_claude(self, monkeypatch):
        """Record every ``_run_claude_mcp`` call and return scripted results.

        The fixture returns a dict ``{"calls": [...], "script": [...]}``; tests
        append to ``script`` before the CLI runs and read ``calls`` after.
        Default scripted result is exit 0 (registration succeeds)."""
        from memtomem_stm.cli import proxy as proxy_mod

        state: dict = {"calls": [], "script": []}

        def fake_run(cmd, timeout=5, cwd=None):
            state["calls"].append(list(cmd))
            if state["script"]:
                nxt = state["script"].pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt
            return _FakeClaudeResult(returncode=0)

        monkeypatch.setattr(proxy_mod, "_run_claude_mcp", fake_run)
        return state

    def _init_input(self, mcp_choice: str) -> str:
        """Build input for the manual-flow init with an explicit MCP choice."""
        return f"filesystem\nfs\nstdio\nnpx\n-y @mcp/fs\n{mcp_choice}\n"

    def test_choice_1_calls_claude_mcp_add_with_expected_args(
        self, runner, config, no_discovery, fake_claude
    ):
        """Happy path: user picks 1, pre-check returns 'not registered',
        ``claude mcp add`` runs successfully."""
        fake_claude["script"] = [
            _FakeClaudeResult(returncode=1),  # `claude mcp get` → not registered
            _FakeClaudeResult(returncode=0),  # `claude mcp add` → success
        ]
        result = runner.invoke(
            cli, ["init", "--no-validate", *_cfg_args(config)], input=self._init_input("1")
        )
        assert result.exit_code == 0, result.output
        assert "Registered with Claude Code" in result.output
        assert len(fake_claude["calls"]) == 2
        # First call: pre-check
        assert fake_claude["calls"][0][:4] == ["claude", "mcp", "get", "memtomem-stm"]
        # Second call: actual add, with -s user and the server command
        add_cmd = fake_claude["calls"][1]
        assert add_cmd[:7] == ["claude", "mcp", "add", "memtomem-stm", "-s", "user", "--"]

    def test_choice_2_writes_mcp_json_without_shelling_out(
        self, runner, config, no_discovery, fake_claude, tmp_path, monkeypatch
    ):
        """Option 2 generates ``.mcp.json`` and never touches the ``claude`` CLI."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            cli, ["init", "--no-validate", *_cfg_args(config)], input=self._init_input("2")
        )
        assert result.exit_code == 0, result.output
        assert fake_claude["calls"] == []
        mcp_json = tmp_path / ".mcp.json"
        assert mcp_json.exists()
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
        assert "memtomem-stm" in data["mcpServers"]

    def test_choice_3_skips_with_manual_hints(self, runner, config, no_discovery, fake_claude):
        """Option 3 prints the manual cheat sheet; no subprocess, no file write."""
        result = runner.invoke(
            cli, ["init", "--no-validate", *_cfg_args(config)], input=self._init_input("3")
        )
        assert result.exit_code == 0, result.output
        assert fake_claude["calls"] == []
        assert "claude mcp add memtomem-stm" in result.output
        assert "mcpServers" in result.output
        assert "mms register" in result.output

    def test_duplicate_pre_check_keeps_existing_when_user_picks_keep(
        self, runner, config, no_discovery, fake_claude
    ):
        """Pre-check finds an existing registration → user picks option 1
        ('keep') on the replace prompt → no ``remove`` or ``add`` call.

        The 'keep' option is intentionally the default for the prompt but
        ``click.CliRunner`` aborts on EOF rather than taking defaults, so
        the test feeds an explicit ``1`` for that prompt too."""
        fake_claude["script"] = [
            _FakeClaudeResult(returncode=0),  # `mcp get` → already exists
        ]
        # Extra "1\n" for the keep/replace prompt (pick keep).
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input=self._init_input("1").rstrip("\n") + "\n1\n",
        )
        assert result.exit_code == 0, result.output
        assert "already registered" in result.output
        assert "Kept existing registration" in result.output
        # Only the pre-check ran — no remove, no add.
        assert len(fake_claude["calls"]) == 1
        assert fake_claude["calls"][0][:3] == ["claude", "mcp", "get"]

    def test_duplicate_replace_removes_then_adds(self, runner, config, no_discovery, fake_claude):
        """Pre-check finds duplicate + user picks 'Replace' → remove, then
        add are called in that order."""
        fake_claude["script"] = [
            _FakeClaudeResult(returncode=0),  # mcp get → exists
            _FakeClaudeResult(returncode=0),  # mcp remove
            _FakeClaudeResult(returncode=0),  # mcp add
        ]
        # Extra "2\n" for the keep/replace prompt (pick replace)
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input=self._init_input("1").rstrip("\n") + "\n2\n",
        )
        assert result.exit_code == 0, result.output
        assert "Registered with Claude Code" in result.output
        assert len(fake_claude["calls"]) == 3
        assert fake_claude["calls"][0][:3] == ["claude", "mcp", "get"]
        assert fake_claude["calls"][1][:3] == ["claude", "mcp", "remove"]
        assert fake_claude["calls"][2][:3] == ["claude", "mcp", "add"]

    def test_fallback_on_file_not_found_writes_mcp_json(
        self, runner, config, no_discovery, fake_claude, tmp_path, monkeypatch
    ):
        """``claude`` CLI missing → ``FileNotFoundError`` on pre-check and
        on the add call → falls back to writing ``.mcp.json``."""
        monkeypatch.chdir(tmp_path)
        # Both `claude mcp get` (pre-check) and `claude mcp add` raise when the
        # binary is absent from PATH — the fake must model that, not just
        # script one exception.
        fake_claude["script"] = [
            FileNotFoundError("claude not on PATH"),  # pre-check
            FileNotFoundError("claude not on PATH"),  # actual add
        ]
        result = runner.invoke(
            cli, ["init", "--no-validate", *_cfg_args(config)], input=self._init_input("1")
        )
        assert result.exit_code == 0, result.output
        assert "claude CLI not found" in result.output
        assert "falling back to .mcp.json" in result.output
        assert (tmp_path / ".mcp.json").exists()

    def test_fallback_on_timeout_writes_mcp_json(
        self, runner, config, no_discovery, fake_claude, tmp_path, monkeypatch
    ):
        """``TimeoutExpired`` from the add call (duplicate check already
        passed) → fallback to ``.mcp.json``."""
        import subprocess

        monkeypatch.chdir(tmp_path)
        fake_claude["script"] = [
            _FakeClaudeResult(returncode=1),  # mcp get → not registered
            subprocess.TimeoutExpired(cmd="claude", timeout=5),  # mcp add → timeout
        ]
        result = runner.invoke(
            cli, ["init", "--no-validate", *_cfg_args(config)], input=self._init_input("1")
        )
        assert result.exit_code == 0, result.output
        assert "timed out" in result.output
        assert (tmp_path / ".mcp.json").exists()

    def test_fallback_on_nonzero_exit_writes_mcp_json(
        self, runner, config, no_discovery, fake_claude, tmp_path, monkeypatch
    ):
        """Add exits non-zero for a reason other than pre-check → generic
        'claude mcp add failed' fallback."""
        monkeypatch.chdir(tmp_path)
        fake_claude["script"] = [
            _FakeClaudeResult(returncode=1),  # mcp get → not registered
            _FakeClaudeResult(returncode=2, stderr="some unexpected error"),
        ]
        result = runner.invoke(
            cli, ["init", "--no-validate", *_cfg_args(config)], input=self._init_input("1")
        )
        assert result.exit_code == 0, result.output
        assert "claude mcp add failed" in result.output
        assert (tmp_path / ".mcp.json").exists()

    # ── --mcp flag (non-interactive path) ────────────────────────────

    def _init_input_without_mcp_choice(self) -> str:
        """Manual-flow input that STOPS before the 3-way prompt (the flag
        pre-answers it, so the prompt never fires)."""
        return "filesystem\nfs\nstdio\nnpx\n-y @mcp/fs\n"

    def test_mcp_flag_skip_bypasses_prompt(self, runner, config, no_discovery, fake_claude):
        """--mcp skip pre-answers option 3; no stdin consumed past the
        manual-flow prompts; no subprocess calls."""
        result = runner.invoke(
            cli,
            ["init", "--no-validate", "--mcp", "skip", *_cfg_args(config)],
            input=self._init_input_without_mcp_choice(),
        )
        assert result.exit_code == 0, result.output
        assert fake_claude["calls"] == []
        # The interactive prompt header MUST NOT appear.
        assert "Register memtomem-stm with an MCP client?" not in result.output
        # Manual hints still printed (option 3 behavior).
        assert "claude mcp add memtomem-stm" in result.output

    def test_mcp_flag_json_writes_mcp_json_without_shell_out(
        self, runner, config, no_discovery, fake_claude, tmp_path, monkeypatch
    ):
        """--mcp json pre-answers option 2; writes .mcp.json; no claude
        shell-out."""
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            cli,
            ["init", "--no-validate", "--mcp", "json", *_cfg_args(config)],
            input=self._init_input_without_mcp_choice(),
        )
        assert result.exit_code == 0, result.output
        assert fake_claude["calls"] == []
        assert "Register memtomem-stm with an MCP client?" not in result.output
        assert (tmp_path / ".mcp.json").exists()

    def test_mcp_flag_claude_registers_without_asking(
        self, runner, config, no_discovery, fake_claude
    ):
        """--mcp claude pre-answers option 1; pre-check + add run without
        the interactive prompt."""
        fake_claude["script"] = [
            _FakeClaudeResult(returncode=1),  # pre-check: not registered
            _FakeClaudeResult(returncode=0),  # add: success
        ]
        result = runner.invoke(
            cli,
            ["init", "--no-validate", "--mcp", "claude", *_cfg_args(config)],
            input=self._init_input_without_mcp_choice(),
        )
        assert result.exit_code == 0, result.output
        assert "Register memtomem-stm with an MCP client?" not in result.output
        assert "Registered with Claude Code" in result.output
        assert len(fake_claude["calls"]) == 2

    def test_mcp_flag_claude_keeps_existing_on_duplicate_noninteractive(
        self, runner, config, no_discovery, fake_claude
    ):
        """--mcp claude hits a duplicate → non-interactive default is
        'keep'. No keep/replace prompt fires, no ``claude mcp remove`` or
        ``add`` calls are made beyond the pre-check.

        This is the load-bearing contract for CI callers: scripted runs
        can re-assert 'register me' without clobbering whatever the user
        had configured previously."""
        fake_claude["script"] = [_FakeClaudeResult(returncode=0)]  # already registered
        result = runner.invoke(
            cli,
            ["init", "--no-validate", "--mcp", "claude", *_cfg_args(config)],
            input=self._init_input_without_mcp_choice(),
        )
        assert result.exit_code == 0, result.output
        assert "already registered" in result.output
        assert "Kept existing registration" in result.output
        # Prompts must NOT have fired.
        assert "Keep existing" not in result.output.split("already registered")[1]
        assert len(fake_claude["calls"]) == 1
        assert fake_claude["calls"][0][:3] == ["claude", "mcp", "get"]


class TestDetectInstallType:
    """``_detect_install_type`` returns ``(server_cmd, server_args)`` for the
    current install location. Covers the three relevant shapes: STM source
    checkout, user project that ``uv add``'d memtomem-stm, and global install."""

    def test_detects_stm_source_checkout(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "memtomem-stm"\nversion = "0.0.0"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "uv"
        assert args[:2] == ["run", "--directory"]
        # The entrypoint must be the ``memtomem-stm`` console script, not the
        # ``mms`` click group — ``mms`` with no subcommand prints help and
        # exits 0, which closes the MCP stdio pipe and the client reports
        # "Failed to reconnect".
        assert args[-1] == "memtomem-stm"

    def test_detects_user_project_with_stm_dep(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "my-app"\ndependencies = ["memtomem-stm>=0.1"]\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "uv"
        assert args[-1] == "memtomem-stm"

    def test_defaults_to_bare_console_script_outside_project(self, tmp_path, monkeypatch):
        """No pyproject.toml reachable (up to 5 levels) → global install
        assumed → bare ``memtomem-stm`` command with no args."""
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "memtomem-stm"
        assert args == []

    def test_unrelated_pyproject_still_defaults(self, tmp_path, monkeypatch):
        """A pyproject.toml that doesn't mention memtomem-stm must NOT be
        treated as a project install — those users installed STM globally."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "unrelated"\ndependencies = ["requests"]\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "memtomem-stm"
        assert args == []

    def test_detects_optional_dependencies(self, tmp_path, monkeypatch):
        """A user project that has memtomem-stm in ``optional-dependencies``
        (e.g. `[project.optional-dependencies].stm = ["memtomem-stm"]`)
        should also resolve to ``uv run --directory ...``."""
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "my-app"\n'
            "dependencies = []\n"
            "[project.optional-dependencies]\n"
            'stm = ["memtomem-stm>=0.1"]\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "uv"
        assert args[-1] == "memtomem-stm"

    def test_adjacent_package_name_does_not_false_positive(self, tmp_path, monkeypatch):
        """A project named `memtomem-stm-extension` (or similar dash-suffixed
        neighbor) must NOT be treated as STM's own source checkout.

        Pre-tightening heuristic matched the prefix string and would have
        flagged this as an STM dep. The PEP 508 name extraction pins the
        match to the exact package name."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "memtomem-stm-extension"\ndependencies = []\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "memtomem-stm"
        assert args == []

    def test_adjacent_dep_name_does_not_false_positive(self, tmp_path, monkeypatch):
        """Same invariant for the dependency list: a dep like
        ``memtomem-stm-adjacent>=0.1`` is a different package."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "my-app"\ndependencies = ["memtomem-stm-adjacent>=0.1"]\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "memtomem-stm"
        assert args == []

    def test_comment_mention_does_not_false_positive(self, tmp_path, monkeypatch):
        """A pyproject.toml with ``memtomem-stm`` only in a comment or
        free-form description string must not trigger uv-run mode.

        Old string-match heuristic would false-positive here; the TOML parse
        + exact-name match catches only real declarations."""
        (tmp_path / "pyproject.toml").write_text(
            "# Using memtomem-stm for memory proxying — see README.\n"
            "[project]\n"
            'name = "my-app"\n'
            'description = "Uses memtomem-stm under the hood"\n'
            'dependencies = ["requests"]\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "memtomem-stm"
        assert args == []

    def test_malformed_pyproject_falls_through(self, tmp_path, monkeypatch):
        """A pyproject.toml we can't parse (malformed TOML, I/O error) should
        break out of the walk and return the default — not crash."""
        (tmp_path / "pyproject.toml").write_text(
            "this is = [[not valid toml\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        from memtomem_stm.cli.proxy import _detect_install_type

        cmd, args = _detect_install_type()
        assert cmd == "memtomem-stm"
        assert args == []

    def test_never_emits_click_group_as_mcp_entrypoint(self, tmp_path, monkeypatch):
        """Regression guard: neither branch may return ``mms`` as the command
        the MCP client will spawn.

        ``mms`` is the click CLI group (entrypoint
        ``memtomem_stm.cli.proxy:cli``) — calling it with no subcommand
        prints help and exits 0, the stdio pipe closes before ``initialize``,
        and the client reports "Failed to reconnect". The actual server is
        ``memtomem-stm`` (entrypoint ``memtomem_stm.server:main``). Pinning
        the non-inclusion here because the three branch-specific tests above
        could all flip together in a refactor and silently reintroduce the
        regression."""
        from memtomem_stm.cli.proxy import _detect_install_type

        # Dev-checkout branch.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "memtomem-stm"\nversion = "0.0.0"\n',
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        cmd, args = _detect_install_type()
        assert "mms" not in [cmd, *args], (
            "regression: _detect_install_type emits the click group — "
            "MCP stdio will close on initialize"
        )


class TestClaudeDesktopConfigHint:
    """``_claude_desktop_config_hint`` picks the right Claude Desktop config
    path per OS. Displayed in the ``mms init`` / ``mms register`` paste-hint
    block after option 2 (generate .mcp.json)."""

    @pytest.mark.parametrize(
        "platform,expected_fragment",
        [
            ("darwin", "Library/Application Support/Claude"),
            ("linux", ".config/Claude"),
            ("linux2", ".config/Claude"),  # older sys.platform label
            ("freebsd14", ".config/Claude"),  # any non-darwin/non-win32 POSIX
        ],
    )
    def test_posix_variants(self, monkeypatch, platform, expected_fragment):
        monkeypatch.setattr("sys.platform", platform)
        from memtomem_stm.cli.proxy import _claude_desktop_config_hint

        assert expected_fragment in _claude_desktop_config_hint()

    def test_windows(self, monkeypatch):
        """Windows uses ``%APPDATA%`` + backslashes — displayed literally so
        Windows users can paste the string into PowerShell / Explorer."""
        monkeypatch.setattr("sys.platform", "win32")
        from memtomem_stm.cli.proxy import _claude_desktop_config_hint

        hint = _claude_desktop_config_hint()
        assert "%APPDATA%" in hint
        assert "\\Claude\\" in hint

    def test_emitted_in_paste_hints(self, monkeypatch, capsys):
        """Integration-level: ``_emit_mcp_paste_hints`` uses the per-OS path."""
        monkeypatch.setattr("sys.platform", "linux")
        from memtomem_stm.cli.proxy import _emit_mcp_paste_hints

        _emit_mcp_paste_hints()
        out = capsys.readouterr().out
        assert ".config/Claude/claude_desktop_config.json" in out
        # macOS-specific path must NOT leak when running on Linux.
        assert "Library/Application Support" not in out


class TestRegisterCommand:
    """``mms register`` is the post-init re-entry path for the MCP-client
    registration flow. It requires that ``mms init`` has been run, so
    ``mms register`` without a config should abort clearly."""

    @pytest.fixture
    def fake_claude(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        state: dict = {"calls": [], "script": []}

        def fake_run(cmd, timeout=5, cwd=None):
            state["calls"].append(list(cmd))
            if state["script"]:
                nxt = state["script"].pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt
            return _FakeClaudeResult(returncode=0)

        monkeypatch.setattr(proxy_mod, "_run_claude_mcp", fake_run)
        return state

    def test_register_aborts_without_config(self, runner, config):
        """Running ``mms register`` before ``mms init`` should fail with a
        clear 'run `mms init` first' message."""
        assert not config.exists()
        result = runner.invoke(cli, ["register", *_cfg_args(config)])
        assert result.exit_code == 1
        assert "config not found" in result.output
        assert "mms init" in result.output

    def test_register_runs_flow_when_config_exists(self, runner, config, fake_claude):
        """With a config present, ``mms register`` drops straight into the
        3-way prompt. Option 3 (skip) prints the manual hints."""
        config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["register", *_cfg_args(config)], input="3\n")
        assert result.exit_code == 0, result.output
        assert fake_claude["calls"] == []
        assert "claude mcp add memtomem-stm" in result.output
        assert "mms register" in result.output

    def test_register_is_idempotent_when_already_registered(self, runner, config, fake_claude):
        """Re-running register when STM is already in Claude Code and the
        user picks 'keep' → no destructive side effects.

        Pins the idempotency contract for the post-init re-entry path
        (plan Part 2): ``mms register`` can be safely rerun without
        clobbering an existing Claude Code entry."""
        config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}), encoding="utf-8")
        fake_claude["script"] = [_FakeClaudeResult(returncode=0)]  # already registered
        # "1\n" for the 3-way prompt (Claude Code) + "1\n" for keep/replace (keep).
        result = runner.invoke(cli, ["register", *_cfg_args(config)], input="1\n1\n")
        assert result.exit_code == 0, result.output
        assert "already registered" in result.output
        assert "Kept existing registration" in result.output
        # Only the pre-check ran.
        assert len(fake_claude["calls"]) == 1

    # ── --mcp flag ─────────────────────────────────────────────────────

    def test_register_mcp_flag_skip_bypasses_prompt(self, runner, config, fake_claude):
        """--mcp skip skips the interactive prompt entirely. No stdin input
        required — this is the shape CI callers use to confirm config is
        set up without asking a human."""
        config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["register", "--mcp", "skip", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        assert fake_claude["calls"] == []
        assert "Register memtomem-stm with an MCP client?" not in result.output

    def test_register_mcp_flag_claude_keeps_on_duplicate(self, runner, config, fake_claude):
        """Scripted re-assertion of 'register me' when already registered
        is a no-op — guarantees ``mms register --mcp claude`` is safe to
        rerun from CI."""
        config.write_text(json.dumps({"enabled": True, "upstream_servers": {}}), encoding="utf-8")
        fake_claude["script"] = [_FakeClaudeResult(returncode=0)]  # already registered
        result = runner.invoke(cli, ["register", "--mcp", "claude", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        assert "Kept existing registration" in result.output
        assert len(fake_claude["calls"]) == 1


# ── add --from-clients (bulk import) ────────────────────────────────────


class TestAddFromClients:
    """`mms add --from-clients` (alias `--import`) reuses init's discovery +
    TUI to bulk-import additional servers. Difference from init: merges into
    an existing config and filters out servers already registered, so the
    user only sees *new* candidates."""

    def _stub_candidates(self, monkeypatch, candidates):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: candidates)

    def _seed_config(self, config: Path, servers: dict) -> None:
        config.write_text(
            json.dumps({"enabled": True, "upstream_servers": servers}, indent=2),
            encoding="utf-8",
        )

    def test_from_clients_happy_path_imports_new_server(self, runner, config, monkeypatch):
        """Fresh candidate + existing server in config → imports candidate,
        preserves existing entry, and shows the discovery header."""
        self._seed_config(
            config, {"existing": {"prefix": "ex", "command": "old", "transport": "stdio"}}
        )
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "Claude Code (user)",
                    "entry": {
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "@fs"],
                        "compression": "auto",
                        "max_result_chars": 8000,
                    },
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", *_cfg_args(config)],
            input="all\n\n",  # pick all + accept suggested prefix
        )
        assert result.exit_code == 0, result.output
        assert "Found 1 new MCP server" in result.output
        assert "Added 1 server" in result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        assert set(data["upstream_servers"]) == {"existing", "filesystem"}
        fs = data["upstream_servers"]["filesystem"]
        assert fs["command"] == "npx"
        assert fs["prefix"] == "filesystem"  # default suggestion

    def test_from_clients_import_alias(self, runner, config, monkeypatch):
        """`--import` is a flag synonym for `--from-clients`; must hit the
        same code path without any other difference."""
        self._seed_config(config, {})
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "npx", "args": []},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--import", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert "filesystem" in data["upstream_servers"]

    def test_filters_already_registered_by_name(self, runner, config, monkeypatch):
        """Discovered candidate whose name matches an existing server is
        dropped with a skip notice — the user isn't forced to re-review or
        re-pick a prefix for something they've already configured."""
        self._seed_config(
            config,
            {
                "filesystem": {
                    "prefix": "fs",
                    "transport": "stdio",
                    "command": "different",
                },
            },
        )
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
                {
                    "name": "github",
                    "source": "Y",
                    "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@gh"]},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output
        assert "Skipping: 'filesystem'" in result.output
        assert "already registered" in result.output
        assert "Found 1 new MCP server" in result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        # Existing filesystem entry preserved (different command proves it wasn't overwritten).
        assert data["upstream_servers"]["filesystem"]["command"] == "different"
        assert "github" in data["upstream_servers"]

    def test_filters_already_registered_by_signature(self, runner, config, monkeypatch):
        """Same (command, args) under a different name → still dedup'd so
        users don't end up with two registrations of the same underlying
        server (would double-surface tools)."""
        self._seed_config(
            config,
            {
                "my-fs": {
                    "prefix": "fs",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@fs"],
                },
            },
        )
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",  # different name...
                    "source": "X",
                    "entry": {
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "@fs"],  # ...same command+args
                    },
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", *_cfg_args(config)],
            input="",  # no prompts expected — exits early
        )
        assert result.exit_code == 0, result.output
        assert "Skipping: 'filesystem'" in result.output
        assert "matches an existing server" in result.output
        assert "All discovered servers are already registered." in result.output

        # Nothing was appended.
        data = json.loads(config.read_text(encoding="utf-8"))
        assert set(data["upstream_servers"]) == {"my-fs"}

    def test_all_registered_exits_cleanly(self, runner, config, monkeypatch):
        """When every discovered candidate is already registered, we exit
        with a no-op message instead of prompting for an empty selection."""
        self._seed_config(
            config,
            {"filesystem": {"prefix": "fs", "transport": "stdio", "command": "npx"}},
        )
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "x"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", *_cfg_args(config)],
        )
        assert result.exit_code == 0, result.output
        assert "All discovered servers are already registered." in result.output

    def test_no_candidates_discovered_exits_cleanly(self, runner, config, monkeypatch):
        """No MCP-client configs on the machine → no-op with a pointer to
        the manual `mms add NAME ...` path."""
        self._seed_config(config, {})
        self._stub_candidates(monkeypatch, [])

        result = runner.invoke(cli, ["add", "--from-clients", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        assert "No MCP servers found" in result.output
        assert "mms add <NAME>" in result.output

    def test_rejects_conflicting_name_and_prefix(self, runner, config, monkeypatch):
        """Passing NAME or --prefix alongside --from-clients is a usage
        error — silently ignoring them would be surprising."""
        self._seed_config(config, {})
        self._stub_candidates(monkeypatch, [])

        result = runner.invoke(
            cli,
            [
                "add",
                "mysrv",
                "--from-clients",
                "--prefix",
                "fs",
                "--command",
                "x",
                "--header",
                "A=b",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code != 0
        assert "--from-clients cannot be combined with" in result.output
        # Lists the conflicting flags by name so the user knows what to drop.
        assert "NAME" in result.output
        assert "--prefix" in result.output
        assert "--command" in result.output
        assert "--header" in result.output

    def test_validate_flag_probes_only_picked_servers(self, runner, config, monkeypatch):
        """With --validate, the selected subset gets probed; unpicked
        candidates must not reach _probe_servers (would be surprising
        perf cost for servers the user declined)."""
        from memtomem_stm.cli import proxy as proxy_mod

        self._seed_config(config, {})
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "a",
                    "source": "X",
                    "entry": {"transport": "stdio", "command": "ca"},
                },
                {
                    "name": "b",
                    "source": "Y",
                    "entry": {"transport": "stdio", "command": "cb"},
                },
            ],
        )

        probe_calls: list[dict] = []

        async def fake_probe_servers(servers, timeout):
            probe_calls.append(dict(servers))
            return {n: {"connected": True, "tools": 3, "error": None} for n in servers}

        monkeypatch.setattr(proxy_mod, "_probe_servers", fake_probe_servers)

        result = runner.invoke(
            cli,
            ["add", "--from-clients", "--validate", *_cfg_args(config)],
            input="1\n\n",  # pick only index 1 (server "a")
        )
        assert result.exit_code == 0, result.output
        assert "Reachable:" in result.output
        assert "Validating 1 server" in result.output

        assert len(probe_calls) == 1
        assert set(probe_calls[0].keys()) == {"a"}  # "b" not probed

    def test_prints_source_removal_hint_per_client(self, runner, config, monkeypatch):
        """After a successful import, the user sees a per-source hint for
        removing the direct registration from the originating MCP client.
        Without it, tools are advertised on two paths (direct + STM) and the
        direct path bypasses compression/caching/LTM surfacing — see #202."""
        self._seed_config(config, {})
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "from-user",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@u"]},
                },
                {
                    "name": "from-local",
                    "source": "Claude Code (project)",
                    "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@l"]},
                },
                {
                    "name": "from-proj",
                    "source": ".mcp.json (project)",
                    "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@p"]},
                },
                {
                    "name": "from-desktop",
                    "source": "Claude Desktop",
                    "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@d"]},
                    "duplicate_in": ["Claude Code (user)"],
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", *_cfg_args(config)],
            input="all\n\n\n\n\n",  # pick all + accept suggested prefix for each
        )
        assert result.exit_code == 0, result.output

        # Per-source remediation, matching `claude mcp remove -s <scope>` flags.
        assert "claude mcp remove from-user -s user" in result.output
        assert "claude mcp remove from-local -s local" in result.output
        assert "claude mcp remove from-proj -s project" in result.output
        # Claude Desktop has no CLI analog — print a file-edit hint instead.
        assert "claude_desktop_config.json" in result.output
        assert "from-desktop" in result.output
        # `duplicate_in` yields a second hint line for the same server.
        assert "claude mcp remove from-desktop -s user" in result.output

    def test_no_removal_hint_when_nothing_imported(self, runner, config, monkeypatch):
        """Empty selection → no "Added" block, no removal hint either. The
        warning is scoped to actually-imported servers."""
        self._seed_config(config, {})
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "skipme",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", *_cfg_args(config)],
            input="\n",  # empty selection = decline
        )
        assert result.exit_code == 0, result.output
        assert "No servers selected" in result.output
        assert "claude mcp remove" not in result.output


class TestAddFromClientsPrune:
    """`mms add --from-clients --prune` (and the TTY prompt variant) removes
    the direct registration from each source MCP client after a successful
    import, closing the dual-path gap #203 left warning-only. See #226."""

    def _stub_candidates(self, monkeypatch, candidates):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: candidates)

    def _seed_config(self, config: Path, servers: dict) -> None:
        config.write_text(
            json.dumps({"enabled": True, "upstream_servers": servers}, indent=2),
            encoding="utf-8",
        )

    @pytest.fixture
    def fake_claude(self, monkeypatch):
        """Record `claude mcp remove` invocations; default to exit 0."""
        from memtomem_stm.cli import proxy as proxy_mod

        state: dict = {"calls": [], "script": []}

        def fake_run(cmd, timeout=5, cwd=None):
            state["calls"].append(list(cmd))
            if state["script"]:
                nxt = state["script"].pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt
            return _FakeClaudeResult(returncode=0)

        monkeypatch.setattr(proxy_mod, "_run_claude_mcp", fake_run)
        return state

    def _all_cc_candidates(self):
        """Candidate set covering every Claude Code scope the writer handles."""
        return [
            {
                "name": "from-user",
                "source": "Claude Code (user)",
                "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@u"]},
            },
            {
                "name": "from-local",
                "source": "Claude Code (project)",
                "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@l"]},
            },
            {
                "name": "from-proj",
                "source": ".mcp.json (project)",
                "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@p"]},
            },
        ]

    def test_prune_flag_shells_out_with_correct_scope_per_source(
        self, runner, config, monkeypatch, fake_claude
    ):
        """`--prune` invokes ``claude mcp remove <name> -s <scope>`` with the
        same scope-label mapping `_source_removal_hint` uses for the manual
        hint: user → user, project → local, .mcp.json → project."""
        self._seed_config(config, {})
        self._stub_candidates(monkeypatch, self._all_cc_candidates())

        result = runner.invoke(
            cli,
            ["add", "--from-clients", "--prune", *_cfg_args(config)],
            input="all\n\n\n\n",  # pick all + accept each default prefix
        )
        assert result.exit_code == 0, result.output

        argvs = [c for c in fake_claude["calls"] if c[:3] == ["claude", "mcp", "remove"]]
        # One remove call per source. Argv = [claude, mcp, remove, <name>, -s, <scope>].
        pairs = {(c[3], c[5]) for c in argvs}
        assert pairs == {
            ("from-user", "user"),
            ("from-local", "local"),
            ("from-proj", "project"),
        }
        assert "Removed from source client(s)" in result.output

    def test_prune_flag_writes_desktop_json_atomically(
        self, runner, config, tmp_path, monkeypatch, fake_claude
    ):
        """Claude Desktop source → atomic JSON rewrite (no `claude` CLI call).
        Verifies the entry is deleted, the file stays valid JSON, and
        unrelated entries are preserved."""
        from memtomem_stm.cli import proxy as proxy_mod

        desktop_path = tmp_path / "claude_desktop_config.json"
        desktop_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "from-desktop": {"command": "npx", "args": ["-y", "@d"]},
                        "keep-me": {"command": "other"},
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(proxy_mod, "_desktop_config_path", lambda: desktop_path)

        self._seed_config(config, {})
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "from-desktop",
                    "source": "Claude Desktop",
                    "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@d"]},
                },
            ],
        )

        result = runner.invoke(
            cli,
            ["add", "--from-clients", "--prune", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        # Claude Desktop must not go through `claude mcp remove`.
        remove_calls = [c for c in fake_claude["calls"] if c[:3] == ["claude", "mcp", "remove"]]
        assert remove_calls == []

        updated = json.loads(desktop_path.read_text(encoding="utf-8"))
        assert "from-desktop" not in updated["mcpServers"]
        assert "keep-me" in updated["mcpServers"]

    def test_prune_flag_nonfatal_on_failure_falls_back_to_manual_hint(
        self, runner, config, monkeypatch, fake_claude
    ):
        """A failing `claude mcp remove` must not roll back the import — the
        proxy config stays updated — and the user sees a warning plus the
        exact manual command to retry."""
        self._seed_config(config, {})
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "from-user",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@u"]},
                },
            ],
        )
        fake_claude["script"] = [_FakeClaudeResult(returncode=1, stderr="boom")]

        result = runner.invoke(
            cli,
            ["add", "--from-clients", "--prune", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        # Import was not rolled back.
        data = json.loads(config.read_text(encoding="utf-8"))
        assert "from-user" in data["upstream_servers"]

        # Warning surfaces the failing entry and the manual command.
        assert "could not remove 1 direct registration" in result.output
        assert "boom" in result.output
        assert "claude mcp remove from-user -s user" in result.output

    def test_prune_flag_without_from_clients_is_usage_error(
        self, runner, config, monkeypatch
    ):
        """`--prune` has no imported-candidate set to act on without
        ``--from-clients``; silently ignoring it would be surprising."""
        self._seed_config(config, {})
        result = runner.invoke(
            cli,
            [
                "add",
                "mysrv",
                "--prefix",
                "fs",
                "--command",
                "x",
                "--prune",
                *_cfg_args(config),
            ],
        )
        assert result.exit_code != 0
        assert "--prune requires --from-clients" in result.output

    def test_tty_prompt_default_no_falls_back_to_hint(
        self, runner, config, monkeypatch, fake_claude
    ):
        """On TTY without `--prune`, user gets a confirm prompt defaulting to
        No. Declining must skip the prune entirely and fall through to the
        #203 hint so the user still has a manual path forward.

        The pick step's questionary UI doesn't work under CliRunner, so we
        stub ``_pick_imports`` directly and force the TUI gate True — what
        we actually want to exercise is the prune prompt branch downstream
        of pick, not pick itself."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)
        monkeypatch.setattr(proxy_mod, "_pick_imports", lambda cands: list(range(len(cands))))
        self._seed_config(config, {})
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "from-user",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", *_cfg_args(config)],
            # Inputs: prefix accept (\n), then decline prune (n\n).
            input="\nn\n",
        )
        assert result.exit_code == 0, result.output

        # No `claude mcp remove` calls — user declined.
        assert not any(c[:3] == ["claude", "mcp", "remove"] for c in fake_claude["calls"])
        # Hint still shown.
        assert "claude mcp remove from-user -s user" in result.output

    def test_tty_prompt_accept_prunes(
        self, runner, config, monkeypatch, fake_claude
    ):
        """On TTY, accepting the prompt prunes exactly like `--prune`. Stubs
        ``_pick_imports`` for the same CliRunner-vs-questionary reason as
        the default-No test above."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)
        monkeypatch.setattr(proxy_mod, "_pick_imports", lambda cands: list(range(len(cands))))
        self._seed_config(config, {})
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "from-user",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", *_cfg_args(config)],
            input="\ny\n",
        )
        assert result.exit_code == 0, result.output

        argvs = [c for c in fake_claude["calls"] if c[:3] == ["claude", "mcp", "remove"]]
        assert argvs == [["claude", "mcp", "remove", "from-user", "-s", "user"]]
        assert "Removed from source client(s)" in result.output

    def test_non_tty_without_flag_keeps_existing_hint_only_behavior(
        self, runner, config, monkeypatch, fake_claude
    ):
        """CliRunner stdin is non-TTY by default. Without `--prune` the prompt
        must not fire and the #203 warning-only behavior must be preserved —
        regression guard against accidentally flipping the default."""
        self._seed_config(config, {})
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "from-user",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        assert not any(c[:3] == ["claude", "mcp", "remove"] for c in fake_claude["calls"])
        assert "claude mcp remove from-user -s user" in result.output
        # Confirm prompt text must NOT appear — no silent auto-prune.
        assert "Remove from source(s)?" not in result.output

    def test_prune_acts_on_duplicate_in_sources_too(
        self, runner, config, monkeypatch, fake_claude
    ):
        """A candidate registered in more than one source is listed with
        `duplicate_in`; `--prune` must remove it from every source, not just
        the primary one, otherwise the dual-path survives."""
        self._seed_config(config, {})
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                    "duplicate_in": ["Claude Code (project)"],
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", "--prune", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        argvs = [c for c in fake_claude["calls"] if c[:3] == ["claude", "mcp", "remove"]]
        pairs = {(c[3], c[5]) for c in argvs}
        assert pairs == {("filesystem", "user"), ("filesystem", "local")}


# ── prune command ────────────────────────────────────────────────────────


class TestPruneCommand:
    """``mms prune`` — standalone post-hoc pruner for the ``init``-only dual-reg
    gap. Covers the same writer surface as ``mms add --import --prune`` (see
    ``TestAddFromClientsPrune``) plus its own argument/TTY contract."""

    def _stub_candidates(self, monkeypatch, candidates):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: candidates)

    def _seed_config(self, config: Path, server_names: list[str]) -> None:
        servers = {
            n: {"prefix": n, "transport": "stdio", "command": "npx"} for n in server_names
        }
        config.write_text(
            json.dumps({"enabled": True, "upstream_servers": servers}, indent=2),
            encoding="utf-8",
        )

    @pytest.fixture
    def fake_claude(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        state: dict = {"calls": [], "script": []}

        def fake_run(cmd, timeout=5, cwd=None):
            state["calls"].append(list(cmd))
            if state["script"]:
                nxt = state["script"].pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt
            return _FakeClaudeResult(returncode=0)

        monkeypatch.setattr(proxy_mod, "_run_claude_mcp", fake_run)
        return state

    def test_no_args_is_usage_error(self, runner, config):
        """Destructive command with no scope: refuse instead of "everything"."""
        self._seed_config(config, [])
        result = runner.invoke(cli, ["prune", *_cfg_args(config)])
        assert result.exit_code != 0
        assert "Pass upstream NAMES or --all" in result.output

    def test_all_with_names_is_usage_error(self, runner, config):
        """``--all`` is the explicit "everything" flag; combining with NAMES
        is an ambiguous request rather than a silent override."""
        self._seed_config(config, [])
        result = runner.invoke(cli, ["prune", "--all", "foo", *_cfg_args(config)])
        assert result.exit_code != 0
        assert "--all cannot be combined with explicit NAMES" in result.output

    def test_missing_config_errors_with_init_hint(self, runner, tmp_path):
        """Pointing at a non-existent config is a cold-start signal — surface
        the init hint rather than failing on _load's generic empty-dict path."""
        result = runner.invoke(
            cli, ["prune", "--all", "--config", str(tmp_path / "nope.json")]
        )
        assert result.exit_code != 0
        assert "config not found" in result.output
        assert "mms init" in result.output

    def test_no_upstream_servers_early_returns(self, runner, config):
        """Empty STM config: no-op exit 0, not an error."""
        self._seed_config(config, [])
        result = runner.invoke(cli, ["prune", "--all", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "No upstream servers configured" in result.output

    def test_no_dual_registered_early_returns(self, runner, config, monkeypatch):
        """STM has upstreams but none are in source clients → clean no-op."""
        self._seed_config(config, ["docs-langchain"])
        self._stub_candidates(monkeypatch, [])  # source clients discovered: none
        result = runner.invoke(cli, ["prune", "--all", "--yes", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "No dual-registered upstreams found" in result.output

    def test_names_filter_prunes_only_requested(
        self, runner, config, monkeypatch, fake_claude
    ):
        """Explicit NAMES must act only on the named subset — unrelated
        dual-reg servers stay put."""
        self._seed_config(config, ["docs-langchain", "langfuse-docs", "other"])
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "docs-langchain",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
                {
                    "name": "langfuse-docs",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli, ["prune", "docs-langchain", "--yes", *_cfg_args(config)]
        )
        assert result.exit_code == 0, result.output

        argvs = [c for c in fake_claude["calls"] if c[:3] == ["claude", "mcp", "remove"]]
        names = [c[3] for c in argvs]
        assert names == ["docs-langchain"]

    def test_unknown_name_errors_before_any_write(
        self, runner, config, monkeypatch, fake_claude
    ):
        """Any NAMES entry that isn't dual-registered fails the whole command
        up front — half-applied writes are worse than a rejection."""
        self._seed_config(config, ["docs-langchain"])
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "docs-langchain",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli, ["prune", "docs-langchain", "not-a-thing", "--yes", *_cfg_args(config)]
        )
        assert result.exit_code != 0
        assert "not dual-registered" in result.output
        assert "not-a-thing" in result.output
        # Safety: docs-langchain was valid, but because one name was invalid
        # we abort before any prune writes fire.
        assert not any(c[:3] == ["claude", "mcp", "remove"] for c in fake_claude["calls"])

    def test_all_prunes_every_dual_registered(
        self, runner, config, monkeypatch, fake_claude
    ):
        """``--all`` = every (STM upstream ∩ source client) pair pruned from
        the source that advertises it."""
        self._seed_config(config, ["a", "b"])
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "a",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
                {
                    "name": "b",
                    "source": ".mcp.json (project)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli, ["prune", "--all", "--yes", *_cfg_args(config)]
        )
        assert result.exit_code == 0, result.output

        argvs = [c for c in fake_claude["calls"] if c[:3] == ["claude", "mcp", "remove"]]
        pairs = {(c[3], c[5]) for c in argvs}
        assert pairs == {("a", "user"), ("b", "project")}

    def test_dry_run_shows_preview_without_writes_or_prompt(
        self, runner, config, monkeypatch, fake_claude
    ):
        """``--dry-run`` must not shell out to ``claude`` or prompt — the
        preview is for scripts that want to surface what *would* happen."""
        self._seed_config(config, ["docs-langchain"])
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "docs-langchain",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli, ["prune", "--all", "--dry-run", *_cfg_args(config)]
        )
        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        assert "docs-langchain" in result.output
        assert "Claude Code (user)" in result.output
        assert not any(c[:3] == ["claude", "mcp", "remove"] for c in fake_claude["calls"])

    def test_non_tty_without_yes_errors(self, runner, config, monkeypatch, fake_claude):
        """CliRunner stdin is non-TTY; without ``--yes`` we must error out
        rather than silently accept a destructive default or hang on a
        prompt that has no way to be answered."""
        self._seed_config(config, ["docs-langchain"])
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "docs-langchain",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(cli, ["prune", "--all", *_cfg_args(config)])
        assert result.exit_code != 0
        assert "no TTY" in result.output
        assert "--yes" in result.output
        assert not any(c[:3] == ["claude", "mcp", "remove"] for c in fake_claude["calls"])

    def test_tty_prompt_accept_prunes(self, runner, config, monkeypatch, fake_claude):
        """TTY path: prompt fires, default No but explicit ``y`` accepts."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)
        self._seed_config(config, ["docs-langchain"])
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "docs-langchain",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli, ["prune", "--all", *_cfg_args(config)], input="y\n"
        )
        assert result.exit_code == 0, result.output

        argvs = [c for c in fake_claude["calls"] if c[:3] == ["claude", "mcp", "remove"]]
        assert argvs == [["claude", "mcp", "remove", "docs-langchain", "-s", "user"]]

    def test_tty_prompt_decline_no_writes(
        self, runner, config, monkeypatch, fake_claude
    ):
        """TTY path + No → clean exit, no writes. Default No is load-bearing
        for destructive opt-in semantics."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)
        self._seed_config(config, ["docs-langchain"])
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "docs-langchain",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli, ["prune", "--all", *_cfg_args(config)], input="n\n"
        )
        assert result.exit_code == 0, result.output
        assert "Aborted" in result.output
        assert not any(c[:3] == ["claude", "mcp", "remove"] for c in fake_claude["calls"])

    def test_duplicate_in_sources_all_pruned(
        self, runner, config, monkeypatch, fake_claude
    ):
        """A name registered in multiple sources (``source`` + ``duplicate_in``)
        must be removed from every source or the dual-path survives."""
        self._seed_config(config, ["filesystem"])
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "filesystem",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                    "duplicate_in": ["Claude Code (project)"],
                },
            ],
        )
        result = runner.invoke(
            cli, ["prune", "--all", "--yes", *_cfg_args(config)]
        )
        assert result.exit_code == 0, result.output

        argvs = [c for c in fake_claude["calls"] if c[:3] == ["claude", "mcp", "remove"]]
        pairs = {(c[3], c[5]) for c in argvs}
        assert pairs == {("filesystem", "user"), ("filesystem", "local")}

    def test_divergent_identity_not_dual_registered(
        self, runner, config, monkeypatch, fake_claude
    ):
        """Same name in STM and source client but different command → the
        user has two intentionally distinct servers sharing a name, not a
        dual-reg. Pruning the source entry would clobber unrelated config;
        ``_find_dual_registered`` must skip it (mirrors the name+signature
        dedup ``mms add --import`` uses in the other direction)."""
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {
                        "docs": {
                            "prefix": "docs",
                            "transport": "stdio",
                            "command": "npx",
                            "args": ["-y", "@me/stm-imported"],
                        }
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        # Source client has a server also called ``docs`` but wired to a
        # completely different command — treat as a separate registration.
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "docs",
                    "source": "Claude Code (user)",
                    "entry": {
                        "transport": "stdio",
                        "command": "python",
                        "args": ["-m", "unrelated_server"],
                    },
                },
            ],
        )
        result = runner.invoke(
            cli, ["prune", "--all", "--yes", *_cfg_args(config)]
        )
        assert result.exit_code == 0, result.output
        assert "No dual-registered upstreams found" in result.output
        assert not any(c[:3] == ["claude", "mcp", "remove"] for c in fake_claude["calls"])

    def test_dry_run_with_names_filters_before_preview(
        self, runner, config, monkeypatch, fake_claude
    ):
        """Scope selection (NAMES) and preview-only (--dry-run) compose:
        the preview must show only the named subset and still make no
        writes. Lock in the contract so future refactors can't accidentally
        preview the full set when the user asked for one."""
        self._seed_config(config, ["a", "b"])
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "a",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
                {
                    "name": "b",
                    "source": ".mcp.json (project)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli, ["prune", "a", "--dry-run", *_cfg_args(config)]
        )
        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        # Header should reflect the filtered count, not the full 2.
        assert "Dual-registered upstream(s): 1" in result.output
        assert "Claude Code (user)" in result.output
        assert ".mcp.json (project)" not in result.output
        assert not any(c[:3] == ["claude", "mcp", "remove"] for c in fake_claude["calls"])

    def test_partial_failure_exits_nonzero_with_manual_hint(
        self, runner, config, monkeypatch, fake_claude
    ):
        """Unlike ``mms add --import --prune`` (import stays if prune fails,
        exit 0), ``mms prune`` is *only* pruning — a failure is the whole
        job failing and must surface via a non-zero exit for scripting.
        The manual-command hint is still printed so the user can recover."""
        self._seed_config(config, ["docs-langchain"])
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "docs-langchain",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        fake_claude["script"] = [_FakeClaudeResult(returncode=1, stderr="boom")]

        result = runner.invoke(
            cli, ["prune", "--all", "--yes", *_cfg_args(config)]
        )
        assert result.exit_code != 0
        assert "could not remove 1 direct registration" in result.output
        assert "boom" in result.output
        assert "claude mcp remove docs-langchain -s user" in result.output

    # ── --json result summary (#614) ──

    _DUAL_CANDIDATE = {
        "name": "docs-langchain",
        "source": "Claude Code (user)",
        "entry": {"transport": "stdio", "command": "npx"},
    }

    def test_json_dry_run_shape_and_no_writes(self, runner, config, monkeypatch, fake_claude):
        self._seed_config(config, ["docs-langchain"])
        self._stub_candidates(monkeypatch, [dict(self._DUAL_CANDIDATE)])
        result = runner.invoke(cli, ["prune", "--all", "--dry-run", "--json", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["action"] == "prune" and data["ok"] is True and data["dry_run"] is True
        assert data["planned"] == [{"name": "docs-langchain", "source": "Claude Code (user)"}]
        assert data["pruned"] == [] and data["failed"] == []
        assert fake_claude["calls"] == []

    def test_json_without_yes_refuses_with_exit_2(self, runner, config, monkeypatch, fake_claude):
        """--json never prompts — even where the TTY path would confirm —
        because a formatting flag must not authorize destructive writes."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)
        self._seed_config(config, ["docs-langchain"])
        self._stub_candidates(monkeypatch, [dict(self._DUAL_CANDIDATE)])
        result = runner.invoke(cli, ["prune", "--all", "--json", *_cfg_args(config)])
        assert result.exit_code == 2
        data = json.loads(result.stdout)
        assert data["ok"] is False and data["error"] == "confirmation_required"
        assert fake_claude["calls"] == []

    def test_json_success_shape(self, runner, config, monkeypatch, fake_claude):
        self._seed_config(config, ["docs-langchain"])
        self._stub_candidates(monkeypatch, [dict(self._DUAL_CANDIDATE)])
        result = runner.invoke(cli, ["prune", "--all", "--yes", "--json", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["ok"] is True and data["dry_run"] is False
        assert data["pruned"] == [{"name": "docs-langchain", "source": "Claude Code (user)"}]
        assert data["failed"] == []

    def test_json_partial_failure_carries_hint_and_exits_1(
        self, runner, config, monkeypatch, fake_claude
    ):
        self._seed_config(config, ["docs-langchain"])
        self._stub_candidates(monkeypatch, [dict(self._DUAL_CANDIDATE)])
        fake_claude["script"] = [_FakeClaudeResult(returncode=1, stderr="boom")]
        result = runner.invoke(cli, ["prune", "--all", "--yes", "--json", *_cfg_args(config)])
        assert result.exit_code == 1
        # json.loads tolerates leading whitespace, so pin byte-level purity
        # too: the failure-block separator line must not reach stdout
        # (codex #644 R1 caught exactly that leak).
        assert result.stdout.startswith("{")
        data = json.loads(result.stdout)  # stderr diagnostics must not corrupt stdout
        assert data["ok"] is False
        assert data["failed"][0]["name"] == "docs-langchain"
        assert data["failed"][0]["hint"] == "claude mcp remove docs-langchain -s user"
        # Human failure diagnostics still print to stderr in --json mode.
        assert "could not remove" in result.stderr

    def test_json_not_dual_registered_error_shape(self, runner, config, monkeypatch):
        self._seed_config(config, ["docs-langchain"])
        self._stub_candidates(monkeypatch, [])
        result = runner.invoke(cli, ["prune", "ghost", "--yes", "--json", *_cfg_args(config)])
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["error"] == "not_dual_registered" and data["names"] == ["ghost"]

    def test_json_no_dual_registered_is_ok_with_empty_arrays(self, runner, config, monkeypatch):
        self._seed_config(config, ["docs-langchain"])
        self._stub_candidates(monkeypatch, [])
        result = runner.invoke(cli, ["prune", "--all", "--yes", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert data["planned"] == [] and data["pruned"] == [] and data["failed"] == []


# ── prune backup log + per-source pruned metadata (#475 PR2) ─────────────


def _backup_log_path(home: Path) -> Path:
    return home / ".memtomem" / "pruned_upstreams.json"


def _read_backup_log(home: Path) -> dict:
    return json.loads(_backup_log_path(home).read_text(encoding="utf-8"))


def _user_candidate(name: str = "alpha", *, secret: str = "tok_secret") -> dict:
    """Discovery-shaped candidate from Claude Code (user) with verbatim raw.

    ``raw`` deliberately differs from ``entry`` (env secret + a field
    normalization drops) so verbatim-copy assertions can't pass by accident.
    """
    return {
        "name": name,
        "source": "Claude Code (user)",
        "entry": {"transport": "stdio", "command": "npx", "args": ["-y", f"@{name}"]},
        "raw": {
            "command": "npx",
            "args": ["-y", f"@{name}"],
            "env": {"TOKEN": secret},
            "host_only_field": True,
        },
        "source_ref": {"kind": "claude-user"},
    }


class TestPruneBackupLog:
    """Every prune path appends the verbatim host entry to
    ``~/.memtomem/pruned_upstreams.json`` BEFORE the host delete runs
    (#475 PR2). The log is the only recovery source for entries without an
    ``origin`` block, so backup-before-delete is the load-bearing order and
    a failed append must skip the delete."""

    def _stub_candidates(self, monkeypatch, candidates):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: candidates)

    def _seed_config(self, config: Path, servers: dict) -> None:
        config.write_text(
            json.dumps({"enabled": True, "upstream_servers": servers}, indent=2),
            encoding="utf-8",
        )

    @pytest.fixture
    def fake_claude(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        state: dict = {"calls": [], "script": []}

        def fake_run(cmd, timeout=5, cwd=None):
            state["calls"].append(list(cmd))
            if state["script"]:
                nxt = state["script"].pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt
            return _FakeClaudeResult(returncode=0)

        monkeypatch.setattr(proxy_mod, "_run_claude_mcp", fake_run)
        return state

    def test_import_prune_appends_verbatim_row_per_source(
        self, runner, config, monkeypatch, fake_claude, _hermetic_home
    ):
        """`add --import --prune` writes one row per pruned source — primary
        and duplicate each with their OWN verbatim raw — at 0600."""
        import stat as stat_mod

        cand = _user_candidate("github")
        cand["duplicate_in"] = ["Claude Code (project)"]
        cand["duplicates"] = [
            {
                "label": "Claude Code (project)",
                "source_ref": {"kind": "claude-project", "path": "/proj"},
                "raw": {"command": "npx", "args": ["-y", "@github"], "type": "stdio"},
            }
        ]
        self._seed_config(config, {})
        self._stub_candidates(monkeypatch, [cand])

        result = runner.invoke(
            cli,
            ["add", "--from-clients", "--prune", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        log_path = _backup_log_path(_hermetic_home)
        if sys.platform != "win32":  # chmod is a near-no-op on Windows
            assert stat_mod.S_IMODE(log_path.stat().st_mode) == 0o600
        log = _read_backup_log(_hermetic_home)
        assert log["schema_version"] == 1
        assert [(r["name"], r["source"], r["original"]) for r in log["entries"]] == [
            ("github", {"kind": "claude-user"}, cand["raw"]),
            (
                "github",
                {"kind": "claude-project", "path": "/proj"},
                cand["duplicates"][0]["raw"],
            ),
        ]
        assert all(r["pruned_at"] for r in log["entries"])

    def test_backup_log_accumulates_across_runs(
        self, runner, config, monkeypatch, fake_claude, _hermetic_home
    ):
        """Append-only: a second prune run adds rows, never rewrites."""
        self._seed_config(config, {})
        self._stub_candidates(monkeypatch, [_user_candidate("one")])
        result = runner.invoke(
            cli,
            ["add", "--from-clients", "--prune", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        self._stub_candidates(monkeypatch, [_user_candidate("two")])
        result = runner.invoke(
            cli,
            ["add", "--from-clients", "--prune", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        log = _read_backup_log(_hermetic_home)
        assert [r["name"] for r in log["entries"]] == ["one", "two"]

    def test_crash_between_append_and_delete_keeps_row_and_host_entry(
        self, runner, config, tmp_path, monkeypatch, _hermetic_home
    ):
        """Backup-before-delete pin (RFC acceptance): crash AFTER the append
        but BEFORE the host delete → the backup row exists and the host file
        still holds the entry. The reverse order would leave neither."""
        from memtomem_stm.cli import proxy as proxy_mod

        desktop_path = tmp_path / "claude_desktop_config.json"
        desktop_entry = {"command": "npx", "args": ["-y", "@d"], "env": {"K": "v"}}
        desktop_path.write_text(
            json.dumps({"mcpServers": {"crashy": desktop_entry}}, indent=2),
            encoding="utf-8",
        )
        monkeypatch.setattr(proxy_mod, "_desktop_config_path", lambda: desktop_path)

        cand = {
            "name": "crashy",
            "source": "Claude Desktop",
            "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@d"]},
            "raw": dict(desktop_entry),
            "source_ref": {"kind": "claude-desktop"},
        }
        self._seed_config(config, {"crashy": {"prefix": "cr", **cand["entry"]}})
        self._stub_candidates(monkeypatch, [cand])

        def boom(name, source):
            raise RuntimeError("simulated crash between backup append and host delete")

        monkeypatch.setattr(proxy_mod, "_prune_from_source", boom)

        result = runner.invoke(cli, ["prune", "--all", "--yes", *_cfg_args(config)])
        assert isinstance(result.exception, RuntimeError)

        # Row landed before the crash...
        log = _read_backup_log(_hermetic_home)
        assert [(r["name"], r["original"]) for r in log["entries"]] == [
            ("crashy", desktop_entry)
        ]
        # ...and the host entry survived untouched.
        host = json.loads(desktop_path.read_text(encoding="utf-8"))
        assert host["mcpServers"]["crashy"] == desktop_entry

    def test_failed_delete_leaves_stale_row_and_pruned_false(
        self, runner, config, monkeypatch, fake_claude, _hermetic_home
    ):
        """A failed host delete leaves the already-appended row in place
        (stale, harmless — the log is advisory) and must NOT flip the
        origin row's ``pruned`` flag."""
        cand = _user_candidate("flaky")
        self._seed_config(config, {})
        self._stub_candidates(monkeypatch, [cand])
        fake_claude["script"] = [_FakeClaudeResult(returncode=1, stderr="nope")]

        result = runner.invoke(
            cli,
            ["add", "--from-clients", "--prune", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        log = _read_backup_log(_hermetic_home)
        assert [r["name"] for r in log["entries"]] == ["flaky"]

        saved = json.loads(config.read_text(encoding="utf-8"))
        origin = saved["upstream_servers"]["flaky"]["origin"]
        assert origin["source"]["pruned"] is False
        assert "pruned_at" not in origin["source"]

    def test_corrupt_backup_log_refuses_append_and_skips_delete(
        self, runner, config, tmp_path, monkeypatch, _hermetic_home
    ):
        """A corrupt log is never clobbered (it's the last-resort recovery
        source) — the append fails, and the host delete is SKIPPED so the
        original keeps existing somewhere."""
        from memtomem_stm.cli import proxy as proxy_mod

        log_path = _backup_log_path(_hermetic_home)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("{not json", encoding="utf-8")

        desktop_path = tmp_path / "claude_desktop_config.json"
        desktop_entry = {"command": "npx", "args": ["-y", "@d"]}
        desktop_path.write_text(
            json.dumps({"mcpServers": {"keep": desktop_entry}}), encoding="utf-8"
        )
        monkeypatch.setattr(proxy_mod, "_desktop_config_path", lambda: desktop_path)

        cand = {
            "name": "keep",
            "source": "Claude Desktop",
            "entry": {"transport": "stdio", "command": "npx", "args": ["-y", "@d"]},
            "raw": dict(desktop_entry),
            "source_ref": {"kind": "claude-desktop"},
        }
        self._seed_config(config, {"keep": {"prefix": "k", **cand["entry"]}})
        self._stub_candidates(monkeypatch, [cand])

        result = runner.invoke(cli, ["prune", "--all", "--yes", *_cfg_args(config)])
        assert result.exit_code == 1
        assert "move it aside" in result.output

        # No clobber, no delete.
        assert log_path.read_text(encoding="utf-8") == "{not json"
        host = json.loads(desktop_path.read_text(encoding="utf-8"))
        assert "keep" in host["mcpServers"]

    def test_old_shape_candidate_without_raw_prunes_without_backup(
        self, runner, config, monkeypatch, fake_claude, _hermetic_home
    ):
        """Candidates lacking verbatim ``raw`` (pre-#475 shape) still prune —
        there is just nothing to back up. Pins backward compatibility for
        the hand-constructed-candidate paths."""
        self._seed_config(config, {})
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "legacy",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                }
            ],
        )
        result = runner.invoke(
            cli,
            ["add", "--from-clients", "--prune", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output
        assert "Removed from source client(s)" in result.output
        assert not _backup_log_path(_hermetic_home).exists()


class TestPerSourcePrunedMetadata:
    """Successful prunes flip the matching ``origin`` row's per-source
    ``pruned``/``pruned_at`` (#475 PR2) — a single ``pruned`` boolean would
    misreport partial failure across primary + duplicate sources."""

    def _stub_candidates(self, monkeypatch, candidates):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: candidates)

    def _seed_config(self, config: Path, servers: dict) -> None:
        config.write_text(
            json.dumps({"enabled": True, "upstream_servers": servers}, indent=2),
            encoding="utf-8",
        )

    @pytest.fixture
    def fake_claude(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        state: dict = {"calls": [], "script": []}

        def fake_run(cmd, timeout=5, cwd=None):
            state["calls"].append(list(cmd))
            if state["script"]:
                nxt = state["script"].pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt
            return _FakeClaudeResult(returncode=0)

        monkeypatch.setattr(proxy_mod, "_run_claude_mcp", fake_run)
        return state

    def test_inline_import_prune_marks_origin_source(
        self, runner, config, monkeypatch, fake_claude
    ):
        cand = _user_candidate("github")
        self._seed_config(config, {})
        self._stub_candidates(monkeypatch, [cand])

        result = runner.invoke(
            cli,
            ["add", "--from-clients", "--prune", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        saved = json.loads(config.read_text(encoding="utf-8"))
        origin = saved["upstream_servers"]["github"]["origin"]
        assert origin["source"]["pruned"] is True
        assert origin["source"]["pruned_at"]
        # The verbatim restore payload must survive the metadata save.
        assert origin["original"] == cand["raw"]

    def test_partial_failure_marks_only_successful_source(
        self, runner, config, monkeypatch, fake_claude
    ):
        """Primary (claude-user) succeeds, duplicate (claude-desktop, file
        missing) fails → only the primary row flips."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(
            proxy_mod, "_desktop_config_path", lambda: Path("/nonexistent/desktop.json")
        )
        cand = _user_candidate("github")
        cand["duplicate_in"] = ["Claude Desktop"]
        cand["duplicates"] = [
            {
                "label": "Claude Desktop",
                "source_ref": {"kind": "claude-desktop"},
                "raw": {"command": "npx", "args": ["-y", "@github"]},
            }
        ]
        self._seed_config(config, {})
        self._stub_candidates(monkeypatch, [cand])

        result = runner.invoke(
            cli,
            ["add", "--from-clients", "--prune", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        saved = json.loads(config.read_text(encoding="utf-8"))
        origin = saved["upstream_servers"]["github"]["origin"]
        assert origin["source"]["pruned"] is True
        assert origin["duplicates"] == [{"kind": "claude-desktop", "pruned": False}]

    def test_standalone_prune_updates_origin_and_saves(
        self, runner, config, monkeypatch, fake_claude
    ):
        """``mms prune`` (post-hoc) flips the origin row of an entry imported
        earlier, matching by structured ``(kind, path)``."""
        cand = _user_candidate("github")
        entry = {
            "prefix": "gh",
            **cand["entry"],
            "origin": {
                "schema_version": 1,
                "source": {"kind": "claude-user", "pruned": False},
                "duplicates": [],
                "imported_at": "2026-06-11T00:00:00+00:00",
                "original": cand["raw"],
            },
        }
        self._seed_config(config, {"github": entry})
        self._stub_candidates(monkeypatch, [cand])

        result = runner.invoke(cli, ["prune", "github", "--yes", *_cfg_args(config)])
        assert result.exit_code == 0, result.output

        saved = json.loads(config.read_text(encoding="utf-8"))
        origin = saved["upstream_servers"]["github"]["origin"]
        assert origin["source"]["pruned"] is True
        assert origin["source"]["pruned_at"]
        assert origin["original"] == cand["raw"]

    def test_standalone_prune_without_origin_leaves_config_untouched(
        self, runner, config, monkeypatch, fake_claude
    ):
        """No origin row to flip → no config rewrite (byte-identical)."""
        cand = _user_candidate("github")
        self._seed_config(config, {"github": {"prefix": "gh", **cand["entry"]}})
        before = config.read_text(encoding="utf-8")
        self._stub_candidates(monkeypatch, [cand])

        result = runner.invoke(cli, ["prune", "github", "--yes", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        assert config.read_text(encoding="utf-8") == before

    def test_metadata_save_failure_leaves_origin_and_backup_restorable(
        self, runner, config, monkeypatch, fake_claude, _hermetic_home
    ):
        """Save-import-first order pin (RFC acceptance): the import (with
        origin, ``pruned: false``) is saved BEFORE any host delete, so a
        step-③ metadata-save failure still leaves the entry restorable —
        origin on disk + backup row appended + host delete done."""
        from memtomem_stm.cli import proxy as proxy_mod

        cand = _user_candidate("github")
        self._seed_config(config, {})
        self._stub_candidates(monkeypatch, [cand])

        real_save = proxy_mod._save
        calls = {"n": 0}

        def flaky_save(path, data):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("disk full at metadata save")
            real_save(path, data)

        monkeypatch.setattr(proxy_mod, "_save", flaky_save)

        result = runner.invoke(
            cli,
            ["add", "--from-clients", "--prune", *_cfg_args(config)],
            input="all\n\n",
        )
        assert isinstance(result.exception, OSError)
        assert calls["n"] == 2  # import save succeeded, metadata save crashed

        # Step-① save survived: origin (with verbatim original) is on disk.
        saved = json.loads(config.read_text(encoding="utf-8"))
        origin = saved["upstream_servers"]["github"]["origin"]
        assert origin["original"] == cand["raw"]
        assert origin["source"]["pruned"] is False
        # Step ② ran: backup row appended and host delete executed.
        log = _read_backup_log(_hermetic_home)
        assert [r["name"] for r in log["entries"]] == ["github"]
        assert ["claude", "mcp", "remove", "github", "-s", "user"] in fake_claude["calls"]


# ── mms eject (#475 PR3) ────────────────────────────────────────────────


def _eject_entry(
    *,
    kind: str = "claude-user",
    path: str | None = None,
    original: dict | None = None,
    duplicates: list[dict] | None = None,
    command: str = "npx",
    args: list[str] | None = None,
) -> dict:
    """STM config entry with an origin block, shaped like a real import."""
    if original is None:
        original = {"command": command, "args": args or ["-y", "@demo"]}
    source: dict = {"kind": kind, "pruned": True, "pruned_at": "2026-06-11T00:00:00Z"}
    if path is not None:
        source["path"] = path
    return {
        "prefix": "dm",
        "transport": "stdio",
        "command": command,
        "args": args or ["-y", "@demo"],
        "origin": {
            "schema_version": 1,
            "source": source,
            "duplicates": duplicates or [],
            "imported_at": "2026-06-11T00:00:00Z",
            "original": original,
        },
    }


class TestEjectCommand:
    """``mms eject`` restores the verbatim host entry, verifies the restore
    against the backing host config, and only then removes the STM entry
    (#475 PR3). Order invariant: host write first, STM removal second —
    every failure mode is dual registration, never disappearance."""

    def _seed_config(self, config: Path, servers: dict) -> None:
        config.write_text(
            json.dumps({"enabled": True, "upstream_servers": servers}, indent=2),
            encoding="utf-8",
        )

    def _stm_servers(self, config: Path) -> dict:
        return json.loads(config.read_text(encoding="utf-8"))["upstream_servers"]

    @pytest.fixture
    def fake_claude_host(self, monkeypatch, _hermetic_home):
        """``_run_claude_mcp`` fake that emulates the claude CLI's writes.

        ``add-json`` applies the payload to the hermetic ``~/.claude.json``
        (user scope to top-level ``mcpServers``, local scope under
        ``projects[<cwd>]``) so the post-write verify exercises a real
        re-read. ``strip_keys`` simulates the CLI's schema-strip of unknown
        fields (probed on 2.1.173); ``write=False`` simulates a clean exit
        that wrote nowhere we can see (wrong project slot).
        """
        from memtomem_stm.cli import proxy as proxy_mod

        state: dict = {"calls": [], "rc": 0, "stderr": "", "strip_keys": [], "write": True}

        def fake_run(cmd, timeout=5, cwd=None):
            state["calls"].append({"cmd": list(cmd), "cwd": cwd})
            if state["rc"] != 0:
                return _FakeClaudeResult(returncode=state["rc"], stderr=state["stderr"])
            if cmd[:3] == ["claude", "mcp", "add-json"] and state["write"]:
                name, payload = cmd[3], json.loads(cmd[4])
                scope = cmd[cmd.index("-s") + 1]
                for key in state["strip_keys"]:
                    payload.pop(key, None)
                cc_path = _hermetic_home / ".claude.json"
                cc = json.loads(cc_path.read_text(encoding="utf-8")) if cc_path.exists() else {}
                if scope == "user":
                    cc.setdefault("mcpServers", {})[name] = payload
                else:
                    proj = cc.setdefault("projects", {}).setdefault(cwd, {})
                    proj.setdefault("mcpServers", {})[name] = payload
                cc_path.write_text(json.dumps(cc), encoding="utf-8")
            return _FakeClaudeResult(returncode=0)

        monkeypatch.setattr(proxy_mod, "_run_claude_mcp", fake_run)
        return state

    def _claude_user_servers(self, home: Path) -> dict:
        cc = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        return cc.get("mcpServers", {})

    # ── round trip + verify ──

    def test_mcp_json_round_trip_deep_equal(self, runner, config, tmp_path):
        """Direct-edit restore is byte-for-byte the verbatim original —
        including fields STM's normalization drops (the round-trip
        invariant the origin block exists to guarantee)."""
        target = tmp_path / "proj" / ".mcp.json"
        target.parent.mkdir()
        original = {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@demo"],
            "env": {"PLAIN": "1"},
            "hostOnly": True,
        }
        self._seed_config(
            config,
            {"demo": _eject_entry(kind="mcp-json", path=str(target), original=original)},
        )

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 0, result.output
        written = json.loads(target.read_text(encoding="utf-8"))
        assert written["mcpServers"]["demo"] == original
        assert "demo" not in self._stm_servers(config)
        # Last upstream gone → STM self-deregistration hint.
        assert "claude mcp remove memtomem-stm" in result.output

    def test_claude_user_shell_out_writes_and_removes(
        self, runner, config, fake_claude_host, _hermetic_home
    ):
        original = {"type": "stdio", "command": "npx", "args": ["-y", "@demo"]}
        self._seed_config(config, {"demo": _eject_entry(original=original)})

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 0, result.output
        add_calls = [c for c in fake_claude_host["calls"] if c["cmd"][:3][2:] == ["add-json"]]
        assert len(add_calls) == 1
        cmd = add_calls[0]["cmd"]
        assert cmd[3] == "demo"
        assert json.loads(cmd[4]) == original
        assert cmd[-2:] == ["-s", "user"]
        assert self._claude_user_servers(_hermetic_home)["demo"] == original
        assert "demo" not in self._stm_servers(config)

    def test_desktop_direct_edit_round_trip(self, runner, config, _hermetic_home):
        from memtomem_stm.mms.import_hosts import _desktop_config_path

        desktop = _desktop_config_path()
        original = {"command": "npx", "args": ["-y", "@demo"], "env": {"X": "1"}}
        self._seed_config(config, {"demo": _eject_entry(kind="claude-desktop", original=original)})

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 0, result.output
        written = json.loads(desktop.read_text(encoding="utf-8"))
        assert written["mcpServers"]["demo"] == original

    # ── schema-loss hard gate ──

    def test_schema_strip_fails_entry_and_keeps_stm(
        self, runner, config, fake_claude_host
    ):
        """The claude CLI silently drops unknown fields — a clean add-json
        exit is NOT proof the verbatim contract held. Default: the entry
        stays in STM (dual registration) and eject fails loudly."""
        original = {"type": "stdio", "command": "npx", "hostOnly": True}
        self._seed_config(config, {"demo": _eject_entry(original=original)})
        fake_claude_host["strip_keys"] = ["hostOnly"]

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 1
        assert "demo" in self._stm_servers(config)
        assert "hostOnly (dropped)" in result.output
        assert "--accept-schema-loss" in result.output
        assert "dual registration" in result.output

    def test_accept_schema_loss_removes_with_warning(
        self, runner, config, fake_claude_host
    ):
        original = {"type": "stdio", "command": "npx", "hostOnly": True}
        self._seed_config(config, {"demo": _eject_entry(original=original)})
        fake_claude_host["strip_keys"] = ["hostOnly"]

        result = runner.invoke(
            cli, ["eject", "demo", "--yes", "--accept-schema-loss", *_cfg_args(config)]
        )

        assert result.exit_code == 0, result.output
        assert "demo" not in self._stm_servers(config)
        assert "schema loss" in result.output

    def test_clean_exit_but_no_visible_write_fails(self, runner, config, fake_claude_host):
        """rc=0 with the entry absent from the expected host slot is a full
        mismatch — the safety net for a CLI writing somewhere unexpected."""
        self._seed_config(config, {"demo": _eject_entry()})
        fake_claude_host["write"] = False

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 1
        assert "demo" in self._stm_servers(config)
        assert "not found at the expected host location" in result.output

    # ── host-write failure / capability message ──

    def test_host_write_failure_keeps_stm_and_prints_fallback(
        self, runner, config, fake_claude_host
    ):
        self._seed_config(config, {"demo": _eject_entry()})
        fake_claude_host["rc"] = 1
        fake_claude_host["stderr"] = "unknown command add-json"

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 1
        assert "demo" in self._stm_servers(config)
        assert "unknown command add-json" in result.output
        assert "claude mcp add-json demo" in result.output  # manual fallback
        assert "2.1.173" in result.output  # capability-oriented hint

    # ── no-clobber guard ──

    def test_idempotent_skip_when_host_identical(
        self, runner, config, fake_claude_host, _hermetic_home
    ):
        """Same signature already in the target → skip the write, proceed
        with STM removal (idempotent re-run after a crashed first eject)."""
        original = {"type": "stdio", "command": "npx", "args": ["-y", "@demo"]}
        (_hermetic_home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"demo": dict(original)}}), encoding="utf-8"
        )
        self._seed_config(config, {"demo": _eject_entry(original=original)})

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 0, result.output
        assert fake_claude_host["calls"] == []  # no shell-out at all
        assert "demo" not in self._stm_servers(config)
        assert "skip write" in result.output

    def test_no_clobber_different_entry_requires_force(
        self, runner, config, fake_claude_host, _hermetic_home
    ):
        (_hermetic_home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"demo": {"command": "other-server"}}}),
            encoding="utf-8",
        )
        self._seed_config(config, {"demo": _eject_entry()})

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 1
        assert "--force" in result.output
        assert fake_claude_host["calls"] == []
        assert "demo" in self._stm_servers(config)

    def test_force_overwrites_via_remove_then_add(
        self, runner, config, fake_claude_host, _hermetic_home
    ):
        (_hermetic_home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"demo": {"command": "other-server"}}}),
            encoding="utf-8",
        )
        self._seed_config(config, {"demo": _eject_entry()})

        result = runner.invoke(cli, ["eject", "demo", "--yes", "--force", *_cfg_args(config)])

        assert result.exit_code == 0, result.output
        verbs = [c["cmd"][2] for c in fake_claude_host["calls"]]
        assert verbs == ["remove", "add-json"]
        assert "demo" not in self._stm_servers(config)

    def test_signature_none_is_never_an_idempotent_match(
        self, runner, config, fake_claude_host, _hermetic_home
    ):
        """No command/url on either side → only full structural equality may
        skip; anything else aborts (R1 M1 — ``None == None`` must not pass)."""
        (_hermetic_home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"demo": {"broken": "entry"}}}), encoding="utf-8"
        )
        self._seed_config(
            config, {"demo": _eject_entry(original={"type": "stdio", "command": "npx"})}
        )

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 1
        assert "--force" in result.output
        assert "demo" in self._stm_servers(config)

    def test_same_signature_different_content_never_releases_stm(
        self, runner, config, fake_claude_host, _hermetic_home
    ):
        """A host entry matching by signature (command/args) but NOT
        structurally (missing env / host-only fields) must not satisfy the
        verbatim-restore invariant: the write is skipped, but the pre-removal
        verify keeps the STM entry — signature alone ignores exactly the
        fields the original exists to preserve (codex R1 Blocker)."""
        original = {
            "command": "npx",
            "args": ["-y", "@demo"],
            "env": {"PLAIN": "1"},
            "hostOnly": True,
        }
        (_hermetic_home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"demo": {"command": "npx", "args": ["-y", "@demo"]}}}),
            encoding="utf-8",
        )
        self._seed_config(config, {"demo": _eject_entry(original=original)})

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 1
        assert fake_claude_host["calls"] == []  # write was skipped...
        assert "demo" in self._stm_servers(config)  # ...and removal blocked
        assert "existing host entry does not match" in result.output
        assert "--accept-schema-loss" in result.output

        accepted = runner.invoke(
            cli, ["eject", "demo", "--yes", "--accept-schema-loss", *_cfg_args(config)]
        )
        assert accepted.exit_code == 0, accepted.output
        assert "demo" not in self._stm_servers(config)

    def test_signature_none_deep_equal_skips(
        self, runner, config, fake_claude_host, _hermetic_home
    ):
        weird = {"weird": "shape"}
        (_hermetic_home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"demo": dict(weird)}}), encoding="utf-8"
        )
        self._seed_config(config, {"demo": _eject_entry(original=weird)})

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 0, result.output
        assert fake_claude_host["calls"] == []
        assert "demo" not in self._stm_servers(config)

    # ── secret gate (§7) ──

    def test_secret_gate_non_tty_yes_does_not_bypass(
        self, runner, config, fake_claude_host
    ):
        original = {"command": "npx", "env": {"GITHUB_TOKEN": "ghp_secret"}}
        self._seed_config(config, {"demo": _eject_entry(original=original)})

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 1
        assert "--allow-argv-secrets" in result.output
        assert fake_claude_host["calls"] == []
        assert "demo" in self._stm_servers(config)

    def test_secret_gate_allow_flag_proceeds(self, runner, config, fake_claude_host):
        original = {"command": "npx", "env": {"GITHUB_TOKEN": "ghp_secret"}}
        self._seed_config(config, {"demo": _eject_entry(original=original)})

        result = runner.invoke(
            cli, ["eject", "demo", "--yes", "--allow-argv-secrets", *_cfg_args(config)]
        )

        assert result.exit_code == 0, result.output
        assert "demo" not in self._stm_servers(config)

    def test_secret_gate_tty_confirm_default_no(
        self, runner, config, monkeypatch, fake_claude_host
    ):
        """TTY path: declining the dedicated secret confirm fails the entry
        even after the main eject confirm was accepted."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)
        original = {"command": "npx", "env": {"GITHUB_TOKEN": "ghp_secret"}}
        self._seed_config(config, {"demo": _eject_entry(original=original)})

        result = runner.invoke(cli, ["eject", "demo", *_cfg_args(config)], input="y\nn\n")

        assert result.exit_code == 1
        assert "secret gate declined" in result.output
        assert fake_claude_host["calls"] == []
        assert "demo" in self._stm_servers(config)

    def test_secret_gate_direct_edit_scope_unaffected(self, runner, config, tmp_path):
        """mcp-json/desktop restores never shell out — no argv exposure, no gate."""
        target = tmp_path / ".mcp.json"
        original = {"command": "npx", "env": {"GITHUB_TOKEN": "ghp_secret"}}
        self._seed_config(
            config, {"demo": _eject_entry(kind="mcp-json", path=str(target), original=original)}
        )

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 0, result.output
        assert json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["demo"] == original

    # ── claude-project cwd rule ──

    def test_claude_project_shell_out_runs_at_recorded_path(
        self, runner, config, tmp_path, fake_claude_host
    ):
        """`-s local` resolves its project slot from the process cwd, and the
        recorded path is used EXACTLY as written — a symlinked alias must not
        be resolved away (the claude CLI keyed the slot, not us)."""
        real = tmp_path / "real-proj"
        real.mkdir()
        alias = tmp_path / "alias-proj"
        alias.symlink_to(real)
        recorded = str(alias)
        self._seed_config(
            config, {"demo": _eject_entry(kind="claude-project", path=recorded)}
        )

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 0, result.output
        add = [c for c in fake_claude_host["calls"] if c["cmd"][2] == "add-json"][0]
        assert add["cwd"] == recorded  # verbatim, not str(real)
        assert add["cmd"][-2:] == ["-s", "local"]
        assert "demo" not in self._stm_servers(config)

    def test_force_pre_remove_runs_at_recorded_path_too(
        self, runner, config, tmp_path, fake_claude_host, _hermetic_home
    ):
        """`--force` remove-then-add: BOTH verbs need the recorded cwd —
        `claude mcp remove -s local` run from elsewhere would delete a
        same-named entry from the wrong project (codex R1 Blocker). The
        test process cwd is unrelated to the recorded path by construction."""
        proj = tmp_path / "proj"
        proj.mkdir()
        recorded = str(proj)
        cc = {
            "projects": {
                recorded: {"mcpServers": {"demo": {"command": "other-server"}}},
            }
        }
        (_hermetic_home / ".claude.json").write_text(json.dumps(cc), encoding="utf-8")
        self._seed_config(config, {"demo": _eject_entry(kind="claude-project", path=recorded)})

        result = runner.invoke(cli, ["eject", "demo", "--yes", "--force", *_cfg_args(config)])

        assert result.exit_code == 0, result.output
        by_verb = {c["cmd"][2]: c for c in fake_claude_host["calls"]}
        assert set(by_verb) == {"remove", "add-json"}
        assert by_verb["remove"]["cwd"] == recorded
        assert by_verb["add-json"]["cwd"] == recorded

    def test_claude_project_vanished_path_aborts_entry(self, runner, config, tmp_path):
        gone = str(tmp_path / "deleted-proj")
        self._seed_config(config, {"demo": _eject_entry(kind="claude-project", path=gone)})

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 1
        assert "no longer exists" in result.output
        assert "--to" in result.output
        assert "demo" in self._stm_servers(config)

    # ── --to / origin-less entries ──

    def test_origin_less_requires_to(self, runner, config):
        self._seed_config(config, {"plain": {"prefix": "p", "command": "npx"}})

        result = runner.invoke(cli, ["eject", "plain", "--yes", *_cfg_args(config)])

        assert result.exit_code == 1
        assert "--to" in result.output

    def test_origin_less_suggests_backup_log_row(
        self, runner, config, _hermetic_home
    ):
        log = _hermetic_home / ".memtomem" / "pruned_upstreams.json"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "entries": [
                        {
                            "name": "plain",
                            "source": {"kind": "claude-user"},
                            "original": {"command": "npx"},
                            "pruned_at": "2026-06-10T00:00:00Z",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self._seed_config(config, {"plain": {"prefix": "p", "command": "npx"}})

        result = runner.invoke(cli, ["eject", "plain", "--yes", *_cfg_args(config)])

        assert result.exit_code == 1
        assert "backup log has a row" in result.output
        assert "kind=claude-user" in result.output

    def test_to_denormalizes_and_strips_stm_fields(self, runner, config, tmp_path):
        target = tmp_path / ".mcp.json"
        self._seed_config(
            config,
            {
                "plain": {
                    "prefix": "p",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@p"],
                    "env": {"A": "1"},
                    "compression": "auto",
                    "max_result_chars": 8000,
                }
            },
        )

        result = runner.invoke(
            cli, ["eject", "plain", "--yes", "--to", f"mcp-json:{target}", *_cfg_args(config)]
        )

        assert result.exit_code == 0, result.output
        written = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["plain"]
        assert written == {"type": "stdio", "command": "npx", "args": ["-y", "@p"], "env": {"A": "1"}}
        assert "reconstructed" in result.output

    def test_to_usage_errors(self, runner, config):
        self._seed_config(config, {"plain": {"prefix": "p", "command": "npx"}})
        bad_kind = runner.invoke(
            cli, ["eject", "plain", "--yes", "--to", "cursor", *_cfg_args(config)]
        )
        assert bad_kind.exit_code == 2
        assert "--to must be one of" in bad_kind.output
        bad_path = runner.invoke(
            cli, ["eject", "plain", "--yes", "--to", "claude-user:/x", *_cfg_args(config)]
        )
        assert bad_path.exit_code == 2
        assert "does not take a :PATH" in bad_path.output

    # ── drift guard / duplicates / --keep ──

    def test_drift_guard_warns_and_restores_original(
        self, runner, config, fake_claude_host, _hermetic_home
    ):
        original = {"type": "stdio", "command": "npx", "args": ["-y", "@demo"]}
        entry = _eject_entry(original=original)
        entry["args"] = ["-y", "@demo", "--edited-after-import"]
        self._seed_config(config, {"demo": entry})

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 0, result.output
        assert "modified after import" in result.output
        assert self._claude_user_servers(_hermetic_home)["demo"] == original

    def test_pruned_duplicates_reported_not_restored(
        self, runner, config, fake_claude_host
    ):
        self._seed_config(
            config,
            {
                "demo": _eject_entry(
                    duplicates=[
                        {"kind": "claude-desktop", "pruned": True, "pruned_at": "x"},
                        {"kind": "mcp-json", "path": "/p/.mcp.json", "pruned": False},
                    ]
                )
            },
        )

        result = runner.invoke(cli, ["eject", "demo", "--yes", *_cfg_args(config)])

        assert result.exit_code == 0, result.output
        assert "also pruned from Claude Desktop" in result.output
        assert "pruned_upstreams.json" in result.output
        # Only the primary restore shell-out — duplicates are not written.
        assert len([c for c in fake_claude_host["calls"] if c["cmd"][2] == "add-json"]) == 1

    def test_keep_restores_but_retains_stm_entry(
        self, runner, config, fake_claude_host, _hermetic_home
    ):
        self._seed_config(config, {"demo": _eject_entry()})

        result = runner.invoke(cli, ["eject", "demo", "--yes", "--keep", *_cfg_args(config)])

        assert result.exit_code == 0, result.output
        assert "demo" in self._claude_user_servers(_hermetic_home)
        assert "demo" in self._stm_servers(config)
        assert "dual-registered" in result.output
        assert "mms prune" in result.output

    # ── command plumbing ──

    def test_dry_run_writes_nothing(self, runner, config, fake_claude_host):
        self._seed_config(config, {"demo": _eject_entry()})

        result = runner.invoke(cli, ["eject", "demo", "--dry-run", *_cfg_args(config)])

        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        assert fake_claude_host["calls"] == []
        assert "demo" in self._stm_servers(config)

    def test_non_tty_without_yes_exits_1(self, runner, config, fake_claude_host):
        self._seed_config(config, {"demo": _eject_entry()})

        result = runner.invoke(cli, ["eject", "demo", *_cfg_args(config)])

        assert result.exit_code == 1
        assert "--yes" in result.output
        assert fake_claude_host["calls"] == []

    def test_self_reference_refused(self, runner, config):
        entry = _eject_entry(command="memtomem-stm", args=[])
        entry["origin"]["original"] = {"command": "memtomem-stm"}
        self._seed_config(config, {"self": entry})

        result = runner.invoke(cli, ["eject", "self", "--yes", *_cfg_args(config)])

        assert result.exit_code == 1
        assert "STM's own registration" in result.output

    def test_unknown_name_errors(self, runner, config):
        self._seed_config(config, {"demo": _eject_entry()})

        result = runner.invoke(cli, ["eject", "ghost", "--yes", *_cfg_args(config)])

        assert result.exit_code == 1
        assert "not configured: ghost" in result.output

    def test_partial_failure_exits_1_but_completes_others(
        self, runner, config, tmp_path, fake_claude_host
    ):
        """One entry failing must not stop the others — per-entry isolation
        with a non-zero exit, matching the prune reporting convention."""
        target = tmp_path / ".mcp.json"
        ok_original = {"command": "npx", "args": ["-y", "@ok"]}
        self._seed_config(
            config,
            {
                "ok": _eject_entry(kind="mcp-json", path=str(target), original=ok_original),
                "bad": _eject_entry(),
            },
        )
        fake_claude_host["rc"] = 1
        fake_claude_host["stderr"] = "boom"

        result = runner.invoke(cli, ["eject", "ok", "bad", "--yes", *_cfg_args(config)])

        assert result.exit_code == 1
        servers = self._stm_servers(config)
        assert "ok" not in servers  # succeeded and was removed
        assert "bad" in servers  # failed and was kept
        assert json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["ok"] == ok_original

    # ── --json result summary (#614) ──

    def test_json_dry_run_plan_rows_without_payload(self, runner, config, tmp_path):
        """Plan rows mirror the human display; ``_EjectPlan.payload`` (the
        verbatim host entry, secrets included) is never serialized."""
        target = tmp_path / ".mcp.json"
        original = {"command": "npx", "env": {"TOKEN": "sekret_val"}}
        self._seed_config(
            config, {"demo": _eject_entry(kind="mcp-json", path=str(target), original=original)}
        )
        result = runner.invoke(cli, ["eject", "demo", "--dry-run", "--json", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["action"] == "eject" and data["ok"] is True and data["dry_run"] is True
        row = data["plan"][0]
        assert row["name"] == "demo" and row["write"] == "restore" and row["verbatim"] is True
        assert row["target"] == {"kind": "mcp-json", "path": str(target)}
        assert data["restored"] == [] and data["failed"] == []
        assert "sekret_val" not in result.stdout

    def test_json_without_yes_refuses_with_exit_2(self, runner, config, monkeypatch, tmp_path):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)
        target = tmp_path / ".mcp.json"
        self._seed_config(config, {"demo": _eject_entry(kind="mcp-json", path=str(target))})
        result = runner.invoke(cli, ["eject", "demo", "--json", *_cfg_args(config)])
        assert result.exit_code == 2
        data = json.loads(result.stdout)
        assert data["error"] == "confirmation_required"
        assert not target.exists()  # no writes before the refusal

    def test_json_success_shape_and_stderr_note(self, runner, config, tmp_path):
        target = tmp_path / ".mcp.json"
        original = {"command": "npx", "env": {"TOKEN": "sekret_val"}}
        self._seed_config(
            config, {"demo": _eject_entry(kind="mcp-json", path=str(target), original=original)}
        )
        result = runner.invoke(cli, ["eject", "demo", "--yes", "--json", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert data["restored"] == ["demo"] and data["removed_from_stm"] == ["demo"]
        assert "demo" not in self._stm_servers(config)
        # Success path never serializes the payload — secret stays out of stdout.
        assert "sekret_val" not in result.stdout
        # Last upstream gone → self-deregistration note moves to stderr.
        assert "claude mcp remove memtomem-stm" in result.stderr

    def test_json_keep_leaves_removed_from_stm_empty(self, runner, config, tmp_path):
        target = tmp_path / ".mcp.json"
        self._seed_config(config, {"demo": _eject_entry(kind="mcp-json", path=str(target))})
        result = runner.invoke(
            cli, ["eject", "demo", "--keep", "--yes", "--json", *_cfg_args(config)]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["keep"] is True
        assert data["restored"] == ["demo"] and data["removed_from_stm"] == []
        assert "demo" in self._stm_servers(config)

    def test_json_host_write_failure_row_carries_manual_hint(
        self, runner, config, fake_claude_host
    ):
        self._seed_config(config, {"demo": _eject_entry()})
        fake_claude_host["rc"] = 1
        fake_claude_host["stderr"] = "unknown command add-json"
        result = runner.invoke(cli, ["eject", "demo", "--yes", "--json", *_cfg_args(config)])
        assert result.exit_code == 1
        data = json.loads(result.stdout)  # stderr diagnostics must not corrupt stdout
        assert data["ok"] is False
        assert data["failed"][0]["name"] == "demo"
        assert data["failed"][0]["hint"].startswith("claude mcp add-json demo")
        assert "demo" in self._stm_servers(config)

    def test_json_secret_gate_fails_plan_without_prompt(self, runner, config, monkeypatch):
        """In --json mode the secret gate never prompts, even on a TTY —
        the entry fails at plan time with the non-TTY wording."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)
        original = {"command": "npx", "env": {"GITHUB_TOKEN": "ghp_secret"}}
        self._seed_config(config, {"demo": _eject_entry(original=original)})
        result = runner.invoke(cli, ["eject", "demo", "--yes", "--json", *_cfg_args(config)])
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["ok"] is False
        assert "--allow-argv-secrets" in data["plan"][0]["error"]
        assert "demo" in self._stm_servers(config)


class TestDenormalizeClientEntry:
    """Unit pins for the degraded inverse of ``_normalize_client_entry``."""

    def test_stdio_strips_stm_fields_and_warns_on_env_filter(self):
        from memtomem_stm.cli.proxy import _denormalize_client_entry

        payload, warnings = _denormalize_client_entry(
            {
                "prefix": "p",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@p"],
                "env": {"A": "1"},
                "compression": "auto",
                "max_result_chars": 8000,
                "surfacing_enabled": False,
            }
        )
        assert payload == {"type": "stdio", "command": "npx", "args": ["-y", "@p"], "env": {"A": "1"}}
        assert any("filtered at import time" in w for w in warnings)

    def test_streamable_http_maps_to_host_http_type(self):
        from memtomem_stm.cli.proxy import _denormalize_client_entry

        payload, warnings = _denormalize_client_entry(
            {"prefix": "p", "transport": "streamable_http", "url": "https://x/mcp"}
        )
        assert payload == {"type": "http", "url": "https://x/mcp"}
        assert any("headers" in w for w in warnings)

    def test_headers_carried_when_present(self):
        from memtomem_stm.cli.proxy import _denormalize_client_entry

        payload, warnings = _denormalize_client_entry(
            {
                "prefix": "p",
                "transport": "sse",
                "url": "https://x/sse",
                "headers": {"Authorization": "Bearer t"},
            }
        )
        assert payload == {
            "type": "sse",
            "url": "https://x/sse",
            "headers": {"Authorization": "Bearer t"},
        }
        assert not any("headers" in w for w in warnings)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="flock is POSIX-only; write_lock is documented as a no-op on Windows",
)
class TestConfigWriteLock:
    """Every ``stm_proxy.json`` mutator runs under the cross-process
    ``~/.memtomem/.stm_proxy.lock`` (#475 PR2). Locking only prune/eject
    would leave a hole: an unlocked ``add`` holding a stale load could save
    over a locked command's result. Read paths and ``--dry-run`` skip it."""

    @staticmethod
    def _hold_lock(home: Path):
        """Hold the config lock through a raw fd, as a foreign process would."""
        import fcntl
        from contextlib import contextmanager

        @contextmanager
        def held():
            lock = home / ".memtomem" / ".stm_proxy.lock"
            lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield
            finally:
                os.close(fd)

        return held()

    @pytest.fixture(autouse=True)
    def _fast_timeout(self, monkeypatch):
        from memtomem_stm.mms import state as mms_state

        monkeypatch.setattr(mms_state, "WRITE_LOCK_TIMEOUT_SECONDS", 0.2)

    def _seed(self, config: Path) -> None:
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {"srv": {"prefix": "s", "command": "npx"}},
                }
            ),
            encoding="utf-8",
        )

    @pytest.mark.parametrize(
        "argv",
        [
            ["add", "other", "--prefix", "o", "--command", "npx"],
            ["remove", "srv", "--yes"],
            ["surfacing", "srv", "off"],
            ["prune", "--all", "--yes"],
            ["init"],
            ["eject", "srv", "--yes"],
            ["tune", "--apply", "--yes"],
        ],
        ids=["add", "remove", "surfacing-write", "prune", "init", "eject", "tune-apply"],
    )
    def test_mutators_fail_cleanly_when_lock_held(
        self, runner, config, argv, _hermetic_home
    ):
        self._seed(config)
        if argv == ["init"]:
            # init aborts on an existing config before doing anything; give
            # it a fresh path so it reaches the lock acquisition.
            cfg_args = ["--config", str(config.parent / "fresh.json")]
        else:
            cfg_args = _cfg_args(config)
        with self._hold_lock(_hermetic_home):
            result = runner.invoke(cli, [*argv, *cfg_args])
        assert result.exit_code == 1
        assert "timed out" in result.output
        assert "mutating the proxy config" in result.output

    def test_lock_timeout_renders_json_envelope_in_json_mode(
        self, runner, config, _hermetic_home
    ):
        """The lock is acquired before the callback runs, so the commands'
        own --json error handling can never see a timeout — the decorator
        must render the envelope itself (#614, codex #644 R1). Exit code
        stays 1; stdout is exactly one JSON document."""
        self._seed(config)
        with self._hold_lock(_hermetic_home):
            result = runner.invoke(
                cli, ["remove", "srv", "--yes", "--json", *_cfg_args(config)]
            )
        assert result.exit_code == 1
        assert result.stdout.lstrip().startswith("{")
        data = json.loads(result.stdout)
        assert data["action"] == "remove" and data["ok"] is False
        assert data["error"] == "config_lock_timeout"
        assert "timed out" in data["message"]
        # Human diagnostics still land on stderr.
        assert "timed out" in result.stderr

    def test_lock_timeout_json_envelope_is_opt_in(self, runner, config, _hermetic_home):
        """`mms tune` also names its flag ``as_json`` but is NOT opted in
        to the JSON timeout envelope (its --json is a preview-only mode) —
        pins that the decorator keys on json_envelope, not on flag-name
        sniffing (codex #644 R2)."""
        self._seed(config)
        with self._hold_lock(_hermetic_home):
            # --json + --apply is a usage error inside the callback, but the
            # held lock times out first (same precedence as main for every
            # mutator); a non-opted-in command must keep text rendering.
            result = runner.invoke(
                cli, ["tune", "--apply", "--yes", "--json", *_cfg_args(config)]
            )
        assert result.exit_code == 1
        assert result.stdout.strip() == ""
        assert "timed out" in result.output

    def test_read_and_dry_run_paths_skip_lock(
        self, runner, config, monkeypatch, _hermetic_home
    ):
        """`surfacing NAME` (read), `prune --dry-run`, and `tune` without
        --apply never write, so a held lock must not block them — mirrors
        the mms ``--plan`` skip."""
        from memtomem_stm.cli import proxy as proxy_mod

        self._seed(config)
        monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: [])
        with self._hold_lock(_hermetic_home):
            read = runner.invoke(cli, ["surfacing", "srv", *_cfg_args(config)])
            dry = runner.invoke(cli, ["prune", "--all", "--dry-run", *_cfg_args(config)])
            eject_dry = runner.invoke(
                cli,
                ["eject", "srv", "--dry-run", "--to", "claude-user", *_cfg_args(config)],
            )
            tune_preview = runner.invoke(cli, ["tune", *_cfg_args(config)])
        assert read.exit_code == 0, read.output
        assert "surfacing for 'srv': on" in read.output
        assert dry.exit_code == 0, dry.output
        assert eject_dry.exit_code == 0, eject_dry.output
        assert tune_preview.exit_code == 0, tune_preview.output

    def test_concurrent_prunes_serialize_and_lose_no_backup_rows(
        self, config, monkeypatch, _hermetic_home
    ):
        """Two concurrent prune spans serialize on the lock: the second's
        backup append happens strictly after the first's host delete
        finishes, so the read-modify-write log append can't lose rows."""
        import threading

        from memtomem_stm.cli import proxy as proxy_mod
        from memtomem_stm.mms import state as mms_state

        # Override this class's 0.2s `_fast_timeout`: t2 must keep polling
        # while t1 deliberately parks inside its span.
        monkeypatch.setattr(mms_state, "WRITE_LOCK_TIMEOUT_SECONDS", 10.0)

        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {
                        "alpha": {"prefix": "a", "command": "npx", "args": ["-y", "@alpha"]},
                        "beta": {"prefix": "b", "command": "npx", "args": ["-y", "@beta"]},
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            proxy_mod,
            "_discover_candidates",
            lambda _cwd: [_user_candidate("alpha"), _user_candidate("beta")],
        )

        t1_in_span = threading.Event()
        release_t1 = threading.Event()
        events: list[str] = []
        events_lock = threading.Lock()

        def fake_remove(name, scope):
            with events_lock:
                events.append(f"remove:{name}")
            if name == "alpha":
                t1_in_span.set()
                assert release_t1.wait(timeout=10)
            return (True, None)

        monkeypatch.setattr(proxy_mod, "_claude_mcp_remove", fake_remove)

        prune_cb = cli.commands["prune"].callback
        errors: list[BaseException] = []

        def run(name: str) -> None:
            try:
                prune_cb(
                    names=(name,),
                    config_path=str(config),
                    all_servers=False,
                    assume_yes=True,
                    dry_run=False,
                )
            except BaseException as exc:  # noqa: BLE001 — surfaced via `errors`
                errors.append(exc)

        t1 = threading.Thread(target=run, args=("alpha",))
        t2 = threading.Thread(target=run, args=("beta",))
        t1.start()
        assert t1_in_span.wait(timeout=10)
        t2.start()
        # t1 is parked inside its host delete, still holding the lock; give
        # t2 time to reach acquisition. It must not have appended: its whole
        # span waits on the lock.
        t2.join(timeout=0.5)
        assert t2.is_alive(), "t2 must block on the lock while t1 holds it"
        log = _read_backup_log(_hermetic_home)
        assert [r["name"] for r in log["entries"]] == ["alpha"]

        release_t1.set()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert not t1.is_alive() and not t2.is_alive()
        assert errors == []
        assert events == ["remove:alpha", "remove:beta"]

        log = _read_backup_log(_hermetic_home)
        assert [r["name"] for r in log["entries"]] == ["alpha", "beta"]


# ── init --prune-originals integration ───────────────────────────────────


class TestInitPruneOriginals:
    """``mms init --prune-originals`` (and the TTY prompt variant) — the
    same ``_handle_source_prune`` machinery invoked inside the init flow so
    onboarding users don't need a separate ``mms prune`` round-trip. Keeps
    the bootstrap-only invariant: init still writes the STM config once,
    but now additionally offers to collapse the dual-path it just created."""

    def _stub_candidates(self, monkeypatch, candidates):
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_discover_candidates", lambda _cwd: candidates)

    @pytest.fixture
    def fake_claude(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        state: dict = {"calls": [], "script": []}

        def fake_run(cmd, timeout=5, cwd=None):
            state["calls"].append(list(cmd))
            if state["script"]:
                nxt = state["script"].pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt
            return _FakeClaudeResult(returncode=0)

        monkeypatch.setattr(proxy_mod, "_run_claude_mcp", fake_run)
        return state

    @pytest.fixture(autouse=True)
    def _stub_mcp_integration(self, monkeypatch):
        """Disable the 3-way client-registration prompt — these tests are
        only exercising the downstream prune step. ``TestInitMcpRegistration``
        covers the registration half."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_run_mcp_integration", lambda *_a, **_kw: None)

    def test_flag_prunes_scripted_non_tty(
        self, runner, config, monkeypatch, fake_claude
    ):
        """``--prune-originals`` in a non-TTY scripted run must fire the prune
        unconditionally — this is the exact `mms init --mcp claude
        --prune-originals` path that the bug report called out."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "docs-langchain",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", "--prune-originals", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        argvs = [c for c in fake_claude["calls"] if c[:3] == ["claude", "mcp", "remove"]]
        assert argvs == [["claude", "mcp", "remove", "docs-langchain", "-s", "user"]]
        assert "Removed from source client(s)" in result.output

    def test_no_flag_non_tty_preserves_hint_only(
        self, runner, config, monkeypatch, fake_claude
    ):
        """Regression guard: the existing ``mms init`` scripted flow (no flag,
        non-TTY) must not suddenly start pruning. The #203 read-only
        invariant holds by default; only opt-in flips it."""
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "docs-langchain",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="all\n\n",
        )
        assert result.exit_code == 0, result.output

        assert not any(c[:3] == ["claude", "mcp", "remove"] for c in fake_claude["calls"])
        # Hint remains so the user has a manual path.
        assert "claude mcp remove docs-langchain -s user" in result.output
        # And no auto-prompt fired.
        assert "Remove from source(s)?" not in result.output

    def test_tty_prompt_accept_prunes(
        self, runner, config, monkeypatch, fake_claude
    ):
        """TTY without flag → prompt fires, ``y`` accepts → prune."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)
        # _pick_imports uses questionary on TTY; stub it the same way the
        # add-prune tests do — we're exercising the post-pick prune branch,
        # not the picker itself.
        monkeypatch.setattr(proxy_mod, "_pick_imports", lambda cands: list(range(len(cands))))
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "docs-langchain",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            # Inputs: accept default prefix (\n), then accept prune prompt (y\n).
            input="\ny\n",
        )
        assert result.exit_code == 0, result.output

        argvs = [c for c in fake_claude["calls"] if c[:3] == ["claude", "mcp", "remove"]]
        assert argvs == [["claude", "mcp", "remove", "docs-langchain", "-s", "user"]]

    def test_tty_prompt_decline_no_prune(
        self, runner, config, monkeypatch, fake_claude
    ):
        """TTY + decline → no writes, manual hint still shown (same as the
        non-TTY default path so user behavior is consistent across paths)."""
        from memtomem_stm.cli import proxy as proxy_mod

        monkeypatch.setattr(proxy_mod, "_should_use_tui", lambda: True)
        monkeypatch.setattr(proxy_mod, "_pick_imports", lambda cands: list(range(len(cands))))
        self._stub_candidates(
            monkeypatch,
            [
                {
                    "name": "docs-langchain",
                    "source": "Claude Code (user)",
                    "entry": {"transport": "stdio", "command": "npx"},
                },
            ],
        )
        result = runner.invoke(
            cli,
            ["init", "--no-validate", *_cfg_args(config)],
            input="\nn\n",
        )
        assert result.exit_code == 0, result.output

        assert not any(c[:3] == ["claude", "mcp", "remove"] for c in fake_claude["calls"])
        assert "claude mcp remove docs-langchain -s user" in result.output


# ── remove command ───────────────────────────────────────────────────────


class TestRemove:
    def test_remove_existing_server_with_yes(self, runner, config):
        runner.invoke(cli, ["add", "fs", "--prefix", "fs", "--command", "x", *_cfg_args(config)])
        result = runner.invoke(cli, ["remove", "fs", "--yes", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "Removed server 'fs'" in result.output

        data = json.loads(config.read_text(encoding="utf-8"))
        assert "fs" not in data["upstream_servers"]

    def test_remove_nonexistent_server(self, runner, config):
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["remove", "ghost", "--yes", *_cfg_args(config)])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_remove_missing_config_reports_config_not_found(self, runner, tmp_path):
        # A wrong --config path must be diagnosed as a missing CONFIG (like
        # prune/register), not as a missing server — _load's default empty
        # config used to make this report "server 'ghost' not found".
        missing = tmp_path / "nope.json"
        result = runner.invoke(cli, ["remove", "ghost", "--yes", "--config", str(missing)])
        assert result.exit_code == 1
        assert "config not found" in result.output
        assert "mms init" in result.output
        assert "server 'ghost'" not in result.output

    def test_remove_without_yes_requires_confirm(self, runner, config):
        runner.invoke(cli, ["add", "fs", "--prefix", "fs", "--command", "x", *_cfg_args(config)])
        # Simulate declining the confirm prompt.
        result = runner.invoke(cli, ["remove", "fs", *_cfg_args(config)], input="n\n")
        assert result.exit_code != 0
        data = json.loads(config.read_text(encoding="utf-8"))
        assert "fs" in data["upstream_servers"]

    # ── --json result summary (#614) ──

    def test_json_success_shape(self, runner, config):
        runner.invoke(cli, ["add", "fs", "--prefix", "fs", "--command", "x", *_cfg_args(config)])
        result = runner.invoke(cli, ["remove", "fs", "--yes", "--json", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["action"] == "remove" and data["ok"] is True
        assert data["name"] == "fs" and data["removed"] is True and data["warnings"] == []
        saved = json.loads(config.read_text(encoding="utf-8"))
        assert "fs" not in saved["upstream_servers"]

    def test_json_without_yes_refuses_with_exit_2(self, runner, config):
        """--json never reaches the confirm prompt: no input is supplied, so
        a prompt would abort on EOF (exit 1) — the exit-2 envelope pins the
        refuse-don't-prompt contract."""
        runner.invoke(cli, ["add", "fs", "--prefix", "fs", "--command", "x", *_cfg_args(config)])
        result = runner.invoke(cli, ["remove", "fs", "--json", *_cfg_args(config)])
        assert result.exit_code == 2
        data = json.loads(result.stdout)
        assert data["ok"] is False and data["error"] == "confirmation_required"
        saved = json.loads(config.read_text(encoding="utf-8"))
        assert "fs" in saved["upstream_servers"]

    def test_json_server_not_found_error_shape(self, runner, config):
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["remove", "ghost", "--yes", "--json", *_cfg_args(config)])
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"] == "server_not_found"

    def test_json_config_not_found_error_shape(self, runner, tmp_path):
        missing = tmp_path / "nope.json"
        result = runner.invoke(cli, ["remove", "g", "--yes", "--json", "--config", str(missing)])
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["error"] == "config_not_found" and data["path"] == str(missing.resolve())


class TestOriginFullyPruned:
    """The shared every-source predicate behind the ``mms list`` pruned
    marker and the ``mms remove`` orphaning hint. Strict types by design:
    provenance is written by our own pydantic-backed writers, so malformed
    shapes mean a hand-edited config — the predicate answers False rather
    than letting truthiness back an unproven "registered nowhere" claim
    (codex R2)."""

    def _origin(self, *, pruned=True, duplicates=None) -> dict:
        return {
            "schema_version": 1,
            "source": {"kind": "claude-user", "pruned": pruned},
            "duplicates": [] if duplicates is None else duplicates,
            "imported_at": "2026-06-11T00:00:00Z",
            "original": {"command": "npx"},
        }

    def test_fully_pruned_with_and_without_duplicates(self):
        from memtomem_stm.cli.proxy import _origin_fully_pruned

        assert _origin_fully_pruned(self._origin()) is True
        assert (
            _origin_fully_pruned(
                self._origin(duplicates=[{"kind": "claude-desktop", "pruned": True}])
            )
            is True
        )

    def test_unpruned_source_or_duplicate(self):
        from memtomem_stm.cli.proxy import _origin_fully_pruned

        assert _origin_fully_pruned(self._origin(pruned=False)) is False
        assert (
            _origin_fully_pruned(
                self._origin(duplicates=[{"kind": "claude-desktop", "pruned": False}])
            )
            is False
        )

    def test_malformed_provenance_is_never_fully_pruned(self):
        """Truthy-but-not-True values and wrong container shapes must not
        classify as pruned: `"false"` is a truthy string, a dict
        ``duplicates`` would iterate its keys, and a non-dict duplicate
        row can't prove anything about its source."""
        from memtomem_stm.cli.proxy import _origin_fully_pruned

        assert _origin_fully_pruned(None) is False
        assert _origin_fully_pruned("pruned") is False
        assert _origin_fully_pruned({"source": "claude-user"}) is False
        assert _origin_fully_pruned(self._origin(pruned="false")) is False
        assert _origin_fully_pruned(self._origin(pruned=1)) is False
        assert (
            _origin_fully_pruned(self._origin(duplicates={"kind": "x", "pruned": False})) is False
        )
        assert _origin_fully_pruned(self._origin(duplicates=["claude-desktop"])) is False
        assert (
            _origin_fully_pruned(self._origin(duplicates=[{"kind": "x", "pruned": "false"}]))
            is False
        )


class TestRemoveEjectHint:
    """``mms remove`` warns before deleting the only registration of an
    imported entry whose host original was pruned — the exact scenario
    #475 exists for ("registered nowhere"). The hint is advisory: it
    precedes the confirm prompt and never blocks the removal."""

    def _imported_entry(self, *, pruned: bool = True, duplicates: list | None = None) -> dict:
        return {
            "prefix": "gh",
            "transport": "stdio",
            "command": "npx",
            "origin": {
                "schema_version": 1,
                "source": {"kind": "claude-user", "pruned": pruned},
                "duplicates": duplicates or [],
                "imported_at": "2026-06-11T00:00:00Z",
                "original": {"command": "npx"},
            },
        }

    def _seed(self, config, entry: dict) -> None:
        config.write_text(
            json.dumps({"enabled": True, "upstream_servers": {"gh": entry}}),
            encoding="utf-8",
        )

    def test_hint_shown_and_removal_proceeds_under_yes(self, runner, config):
        self._seed(config, self._imported_entry())
        result = runner.invoke(cli, ["remove", "gh", "--yes", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "registered nowhere" in result.output
        assert "mms eject gh" in result.output
        # The recorded kind resolves to its human label, not the raw kind.
        assert "Claude Code (user)" in result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert "gh" not in data["upstream_servers"]

    def test_hint_precedes_confirm_so_decline_keeps_entry(self, runner, config):
        """The point of hinting before the prompt: a TTY user who meant
        'stop proxying, keep the server' can abort and run eject instead."""
        self._seed(config, self._imported_entry())
        result = runner.invoke(cli, ["remove", "gh", *_cfg_args(config)], input="n\n")
        assert result.exit_code != 0
        assert "mms eject gh" in result.output
        data = json.loads(config.read_text(encoding="utf-8"))
        assert "gh" in data["upstream_servers"]

    def test_no_hint_without_origin(self, runner, config):
        self._seed(config, {"prefix": "gh", "transport": "stdio", "command": "npx"})
        result = runner.invoke(cli, ["remove", "gh", "--yes", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "mms eject" not in result.output

    def test_no_hint_when_host_original_remains(self, runner, config):
        """Un-pruned origin → the host still has the direct registration;
        removing the STM entry just stops proxying."""
        self._seed(config, self._imported_entry(pruned=False))
        result = runner.invoke(cli, ["remove", "gh", "--yes", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "mms eject" not in result.output

    def test_no_hint_when_unpruned_duplicate_remains(self, runner, config):
        """A duplicate source that was never pruned still registers the
        server somewhere — removal does not orphan it."""
        self._seed(
            config,
            self._imported_entry(duplicates=[{"kind": "claude-desktop", "pruned": False}]),
        )
        result = runner.invoke(cli, ["remove", "gh", "--yes", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "mms eject" not in result.output

    def test_hint_when_every_source_was_pruned(self, runner, config):
        self._seed(
            config,
            self._imported_entry(duplicates=[{"kind": "claude-desktop", "pruned": True}]),
        )
        result = runner.invoke(cli, ["remove", "gh", "--yes", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "mms eject gh" in result.output

    def test_json_mode_moves_hint_into_warnings(self, runner, config):
        """In --json mode the orphaning hint moves off stdout into the
        payload's ``warnings`` — same wording, no ANSI codes."""
        self._seed(config, self._imported_entry())
        result = runner.invoke(cli, ["remove", "gh", "--yes", "--json", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)  # hint on stdout would corrupt the parse
        assert data["removed"] is True
        assert len(data["warnings"]) == 1
        assert "registered nowhere" in data["warnings"][0]
        assert "mms eject gh" in data["warnings"][0]
        assert "\x1b[" not in data["warnings"][0]


# ── End-to-end ───────────────────────────────────────────────────────────


class TestFullFlow:
    def test_add_list_remove_cycle(self, runner, config):
        add_result = runner.invoke(
            cli,
            ["add", "gh", "--prefix", "gh", "--command", "uvx", *_cfg_args(config)],
        )
        assert add_result.exit_code == 0

        list_result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert "gh" in list_result.output

        remove_result = runner.invoke(cli, ["remove", "gh", "--yes", *_cfg_args(config)])
        assert remove_result.exit_code == 0

        final_list = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert "No upstream servers configured" in final_list.output


# ── health command ──────────────────────────────────────────────────────


class TestHealth:
    @pytest.fixture(autouse=True)
    def _isolated_home(self, monkeypatch, tmp_path):
        set_home(monkeypatch, tmp_path / "home")
        monkeypatch.setenv("MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND", "__missing_ltm__")

    def test_health_no_servers(self, runner, config):
        """Empty config → friendly message, no crash."""
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["health", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "No upstream servers configured" in result.output
        assert "Surfacing Bootstrap" in result.output
        assert "feedback tables: missing" in result.output

    def test_health_no_servers_from_invalid_schema_names_cause(self, runner, config):
        """#611: a schema-invalid config is the classic cause of the
        "No upstream servers" symptom on a running server — ``health`` must
        lead with the cause (exit code unchanged)."""
        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "a": {"prefix": "dup", "command": "a"},
                        "b": {"prefix": "dup", "command": "b"},
                    }
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["health", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["config_valid"] is False
        assert "Duplicate upstream prefixes" in data["config_error"]

    def test_health_warns_on_invalid_schema_text(self, runner, config):
        config.write_text(
            json.dumps({"upstream_servers": {}, "default_max_result_chars": -1}),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["health", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "fails validation" in result.output
        assert "No upstream servers configured" in result.output

    def test_health_obs_tools_hint_when_hidden(self, runner, config, monkeypatch):
        """#613: flag off → ``mms health`` surfaces the hint so a user learns
        the env flag that exposes the hidden observability tools."""
        monkeypatch.delenv("MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS", raising=False)
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["health", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "observability tools hidden" in result.output
        assert "MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=true" in result.output

    def test_health_obs_tools_hint_absent_when_advertised(self, runner, config, monkeypatch):
        monkeypatch.setenv("MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS", "true")
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["health", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "observability tools hidden" not in result.output

    def test_health_json_obs_tools_hidden_key(self, runner, config, monkeypatch):
        monkeypatch.delenv("MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS", raising=False)
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["health", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["obs_tools_hidden"] is True
        assert "observability tools hidden" in data["obs_tools_hint"]

    def test_health_logging_line_stderr_only(self, runner, config, monkeypatch):
        """#612: health always prints where logs go, so users learn the
        file-log option exists even while running stderr-only."""
        monkeypatch.delenv("MEMTOMEM_STM_LOG_FILE", raising=False)
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["health", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "Logging: stderr only" in result.output
        assert "MEMTOMEM_STM_LOG_FILE" in result.output

    def test_health_logging_line_with_file(self, runner, config, tmp_path, monkeypatch):
        logf = tmp_path / "stm.log"
        monkeypatch.setenv("MEMTOMEM_STM_LOG_FILE", str(logf))
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["health", *_cfg_args(config)])
        assert result.exit_code == 0
        assert f"stderr + file {logf}" in result.output
        assert "rotating" in result.output

    def test_health_json_logging_key(self, runner, config, tmp_path, monkeypatch):
        logf = tmp_path / "stm.log"
        monkeypatch.setenv("MEMTOMEM_STM_LOG_FILE", str(logf))
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["health", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        logging_status = json.loads(result.output)["logging"]
        assert logging_status["log_file"] == str(logf)
        assert logging_status["destination"] == "stderr+file"
        assert logging_status["writable"] is True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_health_logging_line_unwritable_file_names_stderr(
        self, runner, config, tmp_path, monkeypatch
    ):
        """codex review of #612: mms health runs in a separate process and
        cannot see the server's live handler. When the configured log file
        can't be opened, the server degrades to stderr — health must say so,
        not point at a file that receives nothing."""
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o500)
        try:
            monkeypatch.setenv("MEMTOMEM_STM_LOG_FILE", str(locked / "sub" / "stm.log"))
            config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
            result = runner.invoke(cli, ["health", *_cfg_args(config)])
            assert result.exit_code == 0
            assert "stderr only — configured log file" in result.output
            assert "not writable" in result.output
            json_result = runner.invoke(cli, ["health", "--json", *_cfg_args(config)])
            assert json.loads(json_result.output)["logging"]["writable"] is False
        finally:
            locked.chmod(0o700)

    def test_health_json_no_servers(self, runner, config, monkeypatch):
        monkeypatch.delenv("MEMTOMEM_STM_LOG_FILE", raising=False)
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        result = runner.invoke(cli, ["health", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["servers"] == {}
        assert data["config_valid"] is True
        assert data["config_error"] is None
        assert data["logging"]["destination"] == "stderr"
        assert data["surfacing"]["enabled"] is True
        assert data["surfacing"]["feedback_enabled"] is True
        assert data["surfacing"]["feedback_db"]["exists"] is False
        assert data["surfacing"]["feedback_db"]["initialized"] is False
        assert data["surfacing"]["ltm_server"]["connected"] is False

    def test_health_flags_existing_uninitialized_surfacing_db(
        self, runner, config, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "stm_feedback.db"
        db_path.touch()
        monkeypatch.setenv("MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH", str(db_path))
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")

        result = runner.invoke(cli, ["health", *_cfg_args(config)])

        assert result.exit_code == 0
        assert f"feedback db: {db_path.resolve()}" in result.output
        assert "surfacing has not initialized this DB" in result.output

    def test_health_json_flags_existing_uninitialized_surfacing_db(
        self, runner, config, tmp_path, monkeypatch
    ):
        db_path = tmp_path / "stm_feedback.db"
        db_path.touch()
        monkeypatch.setenv("MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH", str(db_path))
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")

        result = runner.invoke(cli, ["health", "--json", *_cfg_args(config)])

        assert result.exit_code == 0
        data = json.loads(result.output)
        db = data["surfacing"]["feedback_db"]
        assert db["path"] == str(db_path.resolve())
        assert db["exists"] is True
        assert db["initialized"] is False
        assert "surfacing_events" in db["missing_tables"]

    def test_health_surfaces_surfacing_bootstrap_errors(self, runner, config, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")
        monkeypatch.setattr(
            proxy_mod,
            "_surfacing_bootstrap_status",
            lambda _timeout: {
                "enabled": None,
                "feedback_enabled": None,
                "feedback_db": None,
                "error": "bad config",
            },
        )

        text = runner.invoke(cli, ["health", *_cfg_args(config)])
        assert text.exit_code == 0
        assert "ERROR" in text.output
        assert "bad config" in text.output

        as_json = runner.invoke(cli, ["health", "--json", *_cfg_args(config)])
        assert as_json.exit_code == 0
        assert json.loads(as_json.output)["surfacing"]["error"] == "bad config"

    def test_health_reports_unreachable_ltm_server(self, runner, config):
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")

        result = runner.invoke(cli, ["health", *_cfg_args(config)])

        assert result.exit_code == 0
        assert "ltm server: UNREACHABLE" in result.output
        assert "__missing_ltm__ not on PATH" in result.output

    def test_ltm_network_health_uses_url_not_stdio_command(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        captured = {}

        async def fake_probe(transport, command, args, url, headers, timeout, errlog):
            captured.update(
                {
                    "transport": transport,
                    "command": command,
                    "args": args,
                    "url": url,
                    "headers": headers,
                    "timeout": timeout,
                    "errlog": errlog,
                }
            )
            return {"connected": True, "version": "0.3.0-net", "error": None}

        monkeypatch.setattr(proxy_mod, "_probe_ltm_mcp_server", fake_probe)

        status = proxy_mod._ltm_mcp_status(
            SimpleNamespace(
                enabled=True,
                ltm_mcp_transport="sse",
                ltm_mcp_command="__missing_ltm__",
                ltm_mcp_args=[],
                ltm_mcp_url="https://ltm.example/sse",
                ltm_mcp_headers={"Authorization": "Bearer token"},
            ),
            timeout=2,
        )

        assert status["connected"] is True
        assert status["transport"] == "sse"
        assert status["display"] == "https://ltm.example/sse"
        assert captured["transport"] == "sse"
        assert captured["command"] == "__missing_ltm__"
        assert captured["url"] == "https://ltm.example/sse"
        assert captured["headers"] == {"Authorization": "Bearer token"}

    def test_ltm_network_health_redacts_url_credentials(self, monkeypatch):
        # ``status`` feeds both the text lines and the ``--json`` dump, so a
        # basic-auth URL must be redacted in url/display/error — while the
        # probe itself still receives the raw configured URL.
        from memtomem_stm.cli import proxy as proxy_mod

        captured = {}

        async def fake_probe(transport, command, args, url, headers, timeout, errlog):
            captured["url"] = url
            # httpx-style message embedding the full credentialed request URL
            raise ConnectionError(f"Client error '401 Unauthorized' for url '{url}'")

        monkeypatch.setattr(proxy_mod, "_probe_ltm_mcp_server", fake_probe)

        status = proxy_mod._ltm_mcp_status(
            SimpleNamespace(
                enabled=True,
                ltm_mcp_transport="sse",
                ltm_mcp_command="",
                ltm_mcp_args=[],
                ltm_mcp_url="https://alice:s3cret@ltm.example/sse",
                ltm_mcp_headers=None,
            ),
            timeout=2,
        )

        assert captured["url"] == "https://alice:s3cret@ltm.example/sse"  # probe stays raw
        assert status["connected"] is False
        for field in ("url", "display", "error"):
            assert "s3cret" not in str(status[field]), field
        assert status["display"] == "https://***@ltm.example/sse"
        assert "***@ltm.example" in status["error"]

    def test_sse_ltm_probe_timeout_bounds_transport_enter(self, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        captured = {}

        class HangingTransport:
            async def __aenter__(self):
                await asyncio.sleep(60)

            async def __aexit__(self, *_args):
                return None

        def fake_sse_client(url, *, headers=None, timeout=None, sse_read_timeout=None):
            captured.update(
                {
                    "url": url,
                    "headers": headers,
                    "timeout": timeout,
                    "sse_read_timeout": sse_read_timeout,
                }
            )
            return HangingTransport()

        monkeypatch.setattr("mcp.client.sse.sse_client", fake_sse_client)

        with pytest.raises(TimeoutError):
            asyncio.run(
                proxy_mod._probe_ltm_mcp_server(
                    "sse",
                    "",
                    [],
                    "https://ltm.example/sse",
                    {"Authorization": "Bearer token"},
                    0.1,
                    sys.stderr,
                )
            )

        assert captured["url"] == "https://ltm.example/sse"
        assert captured["headers"] == {"Authorization": "Bearer token"}
        assert captured["timeout"] == pytest.approx(0.1, rel=0.1)
        assert captured["sse_read_timeout"] == pytest.approx(0.1, rel=0.1)

    def test_health_reports_connectable_ltm_server(self, config, monkeypatch):
        """Probe a live MCP child via real subprocess.

        Cannot use ``CliRunner`` here: Click's runner replaces ``sys.stderr``
        with a buffer that has no ``fileno()``, and the MCP stdio client
        forwards that stderr to ``asyncio.create_subprocess_exec`` which needs
        a real file descriptor. Mirrors ``TestAddValidate``'s success-path
        test, which hit the same constraint."""
        import subprocess

        monkeypatch.setenv("MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND", sys.executable)
        monkeypatch.setenv(
            "MEMTOMEM_STM_SURFACING__LTM_MCP_ARGS",
            json.dumps([str(_FAKE_SERVER)]),
        )
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")

        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "from memtomem_stm.cli.proxy import cli; cli()",
                "health",
                "--config",
                str(config),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        assert "ltm server: connectable" in proc.stdout
        assert "version 0.3.0-fake" in proc.stdout

    def test_health_flags_ltm_server_missing_mem_search(self, config, monkeypatch, tmp_path):
        """An MCP server that initializes but doesn't expose ``mem_search``
        cannot serve any surfacing call — flag it as UNREACHABLE instead of
        falsely advertising ``connectable``.

        Uses real subprocess for the same stderr-fileno reason as the
        connectable test."""
        import subprocess
        import textwrap

        bare_server = tmp_path / "_bare_mcp_server.py"
        bare_server.write_text(
            textwrap.dedent(
                """\
                from mcp.server.fastmcp import FastMCP

                mcp = FastMCP("bare-no-mem-search")

                @mcp.tool()
                async def unrelated_tool() -> str:
                    return "ok"

                if __name__ == "__main__":
                    mcp.run()
                """
            ),
            encoding="utf-8",
        )

        monkeypatch.setenv("MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND", sys.executable)
        monkeypatch.setenv(
            "MEMTOMEM_STM_SURFACING__LTM_MCP_ARGS",
            json.dumps([str(bare_server)]),
        )
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")

        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "from memtomem_stm.cli.proxy import cli; cli()",
                "health",
                "--config",
                str(config),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        assert "ltm server: UNREACHABLE" in proc.stdout
        assert "mem_search" in proc.stdout
        assert "ltm server: connectable" not in proc.stdout

    def test_health_ltm_probe_honors_cli_timeout(self, config, monkeypatch, tmp_path):
        """``mms health --timeout N`` must bound the LTM probe at N seconds,
        not at ``surfacing.timeout_seconds``. A stalled LTM command with a
        large surfacing timeout would otherwise pin ``mms health`` past the
        documented per-server bound."""
        import subprocess

        stall_server = tmp_path / "_stall_server.py"
        stall_server.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")

        monkeypatch.setenv("MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND", sys.executable)
        monkeypatch.setenv(
            "MEMTOMEM_STM_SURFACING__LTM_MCP_ARGS",
            json.dumps([str(stall_server)]),
        )
        # Surfacing's runtime timeout is set high so the CLI flag is the only
        # thing that can bound the probe — pins the regression.
        monkeypatch.setenv("MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS", "30")
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")

        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "from memtomem_stm.cli.proxy import cli; cli()",
                "health",
                "--timeout",
                "1",
                "--config",
                str(config),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        assert "ltm server: UNREACHABLE" in proc.stdout
        assert "timeout (1s)" in proc.stdout

    def test_health_ltm_probe_shares_timeout_across_phases(
        self, config, monkeypatch, tmp_path
    ):
        """A server that handshakes + lists tools fast but stalls on
        ``mem_do(action="version")`` must not extend total probe time past
        ``--timeout``. The probe shares one end-to-end budget across
        initialize + list_tools + the optional version call; the version
        call's stall is absorbed and ``connected`` stays true because
        ``mem_search`` connectivity was already proven."""
        import subprocess
        import textwrap
        import time

        stall_version_server = tmp_path / "_stall_version_server.py"
        stall_version_server.write_text(
            textwrap.dedent(
                """\
                import asyncio
                from mcp.server.fastmcp import FastMCP

                mcp = FastMCP("stall-version")

                @mcp.tool()
                async def mem_search(query: str) -> str:
                    return ""

                @mcp.tool()
                async def mem_do(action: str, params: dict | None = None) -> str:
                    if action == "version":
                        await asyncio.sleep(60)
                    return "{}"

                if __name__ == "__main__":
                    mcp.run()
                """
            ),
            encoding="utf-8",
        )

        monkeypatch.setenv("MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND", sys.executable)
        monkeypatch.setenv(
            "MEMTOMEM_STM_SURFACING__LTM_MCP_ARGS",
            json.dumps([str(stall_version_server)]),
        )
        monkeypatch.setenv("MEMTOMEM_STM_SURFACING__TIMEOUT_SECONDS", "30")
        config.write_text(json.dumps({"upstream_servers": {}}), encoding="utf-8")

        # ``--timeout 3`` (not 1) gives initialize + list_tools headroom on
        # slow CI runners where Python child startup + FastMCP boot can eat
        # several hundred ms; we still want the version stall to be bounded
        # at ~3s, not 30s.
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from memtomem_stm.cli.proxy import cli; cli()",
                    "health",
                    "--json",
                    "--timeout",
                    "3",
                    "--config",
                    str(config),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired as exc:
            # Pre-fix this is the canonical regression mode: the version
            # probe gets the full ``TIMEOUT_SECONDS=30`` budget on top of
            # initialize/list_tools, so the outer 20s watchdog trips before
            # ``mms health`` returns.
            raise AssertionError(
                f"LTM probe didn't honor shared budget — outer subprocess "
                f"timed out at {exc.timeout}s (expected ~3s wall time)"
            ) from exc
        elapsed = time.perf_counter() - start

        assert proc.returncode == 0, f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        data = json.loads(proc.stdout)
        ltm = data["surfacing"]["ltm_server"]
        # ``mem_search`` connectivity is proven before the version probe
        # runs, so the version stall must not flip the verdict.
        assert ltm["connected"] is True, ltm
        # Version probe was budget-starved → silently dropped, not reported.
        assert ltm["version"] is None, ltm
        # Pre-fix the version probe alone would burn ~30s; post-fix the
        # shared budget caps it near the 3s ``--timeout``. 10s upper bound
        # gives plenty of slack for slow CI without being a no-op check —
        # pre-fix elapsed would actually trip the outer 20s
        # ``subprocess.run`` first (caught above).
        assert elapsed < 10.0, f"shared-budget probe took {elapsed:.2f}s"

    def test_health_missing_config(self, runner, config):
        """Missing file → distinguish from empty-config so a user pointing
        at the wrong path gets a clear hint instead of a silent no-op
        (compounding gap from #221, extended to ``health``)."""
        result = runner.invoke(cli, ["health", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "Config not found" in result.output
        assert "mms add" in result.output
        assert "No upstream servers configured" not in result.output

    def test_health_json_missing_config(self, runner, config):
        """``health --json`` mirrors ``status --json`` / ``list --json`` for
        missing-config so scripts piping any of the three through the same
        formatter don't have to branch."""
        result = runner.invoke(cli, ["health", "--json", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["error"] == "config_not_found"
        assert str(config) in data["path"]

    def test_health_unreachable_server(self, runner, config):
        """A server with a nonexistent command → DISCONNECTED."""
        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "bad": {
                            "prefix": "bad",
                            "transport": "stdio",
                            "command": "__nonexistent_cmd_12345__",
                            "args": [],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["health", "--timeout", "3", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "DISCONNECTED" in result.output

    def test_health_json_unreachable(self, runner, config):
        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "bad": {
                            "prefix": "bad",
                            "transport": "stdio",
                            "command": "__nonexistent_cmd_12345__",
                            "args": [],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["health", "--json", "--timeout", "3", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["servers"]["bad"]["connected"] is False
        assert data["servers"]["bad"]["error"]
        assert "surfacing" in data

    @pytest.mark.parametrize(
        ("transport", "client_path"),
        [
            ("sse", "mcp.client.sse.sse_client"),
            ("streamable_http", "mcp.client.streamable_http.streamablehttp_client"),
        ],
    )
    def test_upstream_probe_timeout_bounds_transport_enter(
        self, monkeypatch, transport, client_path
    ):
        """#398 gave ``_probe_ltm_mcp_server`` an end-to-end deadline, but
        the older upstream probe ``_probe_one`` only bounded
        ``initialize()`` — a network upstream hanging on TCP connect
        blocked ``mms health --timeout N`` / ``mms add --validate``
        indefinitely, and no ``timeout=``/``sse_read_timeout=`` reached
        the SDK client. Mirrors
        ``test_sse_ltm_probe_timeout_bounds_transport_enter``."""
        import time

        from memtomem_stm.cli import proxy as proxy_mod

        captured = {}

        class HangingTransport:
            async def __aenter__(self):
                await asyncio.sleep(8)

            async def __aexit__(self, *_args):
                return None

        def fake_client(url, *, headers=None, timeout=None, sse_read_timeout=None):
            captured.update(
                {
                    "url": url,
                    "headers": headers,
                    "timeout": timeout,
                    "sse_read_timeout": sse_read_timeout,
                }
            )
            return HangingTransport()

        monkeypatch.setattr(client_path, fake_client)

        cfg = {
            "transport": transport,
            "url": "https://up.example/mcp",
            "prefix": "up",
            "headers": {"X-Api-Key": "k"},
        }
        start = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(proxy_mod._probe_one(cfg, 0.1))
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"transport-enter stall ran {elapsed:.2f}s past the 0.1s budget"
        assert captured["url"] == "https://up.example/mcp"
        assert captured["headers"] == {"X-Api-Key": "k"}
        assert captured["timeout"] == pytest.approx(0.1, rel=0.1)
        assert captured["sse_read_timeout"] == pytest.approx(0.1, rel=0.1)

    def test_upstream_probe_timeout_bounds_list_tools(self, monkeypatch):
        """An upstream that connects and initializes fine but stalls on
        ``tools/list`` must also resolve within the probe budget — this
        phase was previously unbounded so ``_safe_probe``'s
        ``asyncio.TimeoutError`` classification could never fire for it."""
        import time

        from memtomem_stm.cli import proxy as proxy_mod

        class FakeTransport:
            async def __aenter__(self):
                return (object(), object())

            async def __aexit__(self, *_args):
                return None

        class FakeSession:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def initialize(self):
                return None

            async def list_tools(self):
                await asyncio.sleep(8)

        monkeypatch.setattr("mcp.client.sse.sse_client", lambda url, **_kw: FakeTransport())
        monkeypatch.setattr("mcp.ClientSession", FakeSession)

        cfg = {"transport": "sse", "url": "https://up.example/sse", "prefix": "up"}
        start = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(proxy_mod._probe_one(cfg, 0.1))
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"list_tools stall ran {elapsed:.2f}s past the 0.1s budget"

    def test_safe_probe_classifies_deadline_timeout(self, monkeypatch):
        """The end-to-end deadline surfaces through ``_probe_servers`` as
        the same ``timeout (Ns)`` classification the initialize-phase
        timeout already gets — DISCONNECTED with a clear cause, not a
        hang or an unwrapped traceback."""
        from memtomem_stm.cli import proxy as proxy_mod

        class HangingTransport:
            async def __aenter__(self):
                await asyncio.sleep(8)

            async def __aexit__(self, *_args):
                return None

        monkeypatch.setattr("mcp.client.sse.sse_client", lambda url, **_kw: HangingTransport())

        cfg = {"transport": "sse", "url": "https://up.example/sse", "prefix": "up"}
        results = asyncio.run(proxy_mod._probe_servers({"up": cfg}, 0.1))
        assert results["up"]["connected"] is False
        assert results["up"]["error"] == "timeout (0.1s)"

    def test_health_error_unwraps_taskgroup_wrapper(self, runner, config):
        """Probe failures inside an anyio TaskGroup are wrapped as
        ``BaseExceptionGroup`` and stringify as ``unhandled errors in a
        TaskGroup (N sub-exception)`` — useless to a user. The CLI must
        unwrap to the leaf cause.

        Repro path: an upstream that starts cleanly (so ``stdio_client``
        opens its TaskGroup) but doesn't speak JSON-RPC. ``echo`` exits
        immediately, so the SDK detects ``Connection closed`` inside the
        group. Bare ``__nonexistent_cmd__`` fails earlier (``OSError``
        before the group opens) and so doesn't exercise this path.
        """
        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "echo": {
                            "prefix": "echo",
                            "transport": "stdio",
                            "command": "echo",
                            "args": ["hello"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(cli, ["health", "--json", "--timeout", "3", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        err = data["servers"]["echo"]["error"]
        # The wrapper string was the symptom — explicitly assert it doesn't
        # leak through. Any non-trivial leaf message is fine.
        assert "TaskGroup" not in err
        assert "sub-exception" not in err
        assert err  # must be non-empty

    def test_health_json_stderr_not_polluted_by_sdk_logs(self, runner, config):
        """The MCP SDK calls ``logger.exception(...)`` on parse failures,
        which dumps multi-line tracebacks to stderr. For ``health --json``
        callers piping into ``jq`` / similar, that noise is just garbage
        sharing the pipe — silence it for the duration of the probe.
        """
        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "echo": {
                            "prefix": "echo",
                            "transport": "stdio",
                            "command": "echo",
                            "args": ["hello"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        # ``mix_stderr=False`` was removed in click 8.2 — instead, capture
        # stderr explicitly via ``CliRunner(catch_exceptions=False)`` plus
        # ``invoke``'s default behavior of teeing stderr into ``output``.
        # We assert on the JSON body parse-cleanly: any stderr noise that
        # was joined into stdout would break ``json.loads``.
        result = runner.invoke(cli, ["health", "--json", "--timeout", "3", *_cfg_args(config)])
        assert result.exit_code == 0
        # If logger.exception had fired into the same stream, this would
        # raise ``json.JSONDecodeError`` because the traceback prefix
        # corrupts the JSON document.
        data = json.loads(result.output)
        assert data["servers"]["echo"]["connected"] is False

    def test_names_flag_lists_overflowing_tools(self, runner, config, monkeypatch):
        """``mms health --names`` reports any upstream tool that would be
        silently dropped by clients due to the 64-char overflow (#261).
        Without ``--names`` the report stays compact."""
        from memtomem_stm.cli import proxy as proxy_mod

        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "docs": {
                            "prefix": "docs_langchain",
                            "transport": "streamable_http",
                            "url": "https://example/mcp",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        async def fake_probe_servers(servers, timeout):
            return {
                "docs": {
                    "connected": True,
                    "tools": 2,
                    "overflowing": ["query_docs_filesystem_docs_by_lang_chain"],
                    "error": None,
                }
            }

        monkeypatch.setattr(proxy_mod, "_probe_servers", fake_probe_servers)

        # Without --names: silent on overflow.
        result = runner.invoke(cli, ["health", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "connected" in result.output
        assert "overflow:" not in result.output.lower()

        # With --names: the offending tool is named.
        result = runner.invoke(cli, ["health", "--names", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "overflow" in result.output.lower()
        assert "query_docs_filesystem_docs_by_lang_chain" in result.output

    def test_names_flag_says_all_fit_when_clean(self, runner, config, monkeypatch):
        """When no tool overflows, ``--names`` confirms positively (lets
        the user distinguish 'clean check ran' from 'flag was a no-op')."""
        from memtomem_stm.cli import proxy as proxy_mod

        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "ok": {
                            "prefix": "ok",
                            "transport": "stdio",
                            "command": "x",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        async def fake_probe_servers(servers, timeout):
            return {
                "ok": {
                    "connected": True,
                    "tools": 3,
                    "overflowing": [],
                    "error": None,
                }
            }

        monkeypatch.setattr(proxy_mod, "_probe_servers", fake_probe_servers)

        result = runner.invoke(cli, ["health", "--names", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "all tool names fit" in result.output

    def test_names_flag_json_includes_overflowing_array(self, runner, config, monkeypatch):
        """``--json`` already exposes the per-server probe shape; the
        new ``overflowing`` field has to round-trip cleanly so callers
        scripting ``mms health --json --names`` can grep without parsing
        prose."""
        from memtomem_stm.cli import proxy as proxy_mod

        config.write_text(
            json.dumps(
                {
                    "upstream_servers": {
                        "docs": {
                            "prefix": "docs_langchain",
                            "transport": "streamable_http",
                            "url": "https://example/mcp",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        async def fake_probe_servers(servers, timeout):
            return {
                "docs": {
                    "connected": True,
                    "tools": 2,
                    "overflowing": ["query_docs_filesystem_docs_by_lang_chain"],
                    "error": None,
                }
            }

        monkeypatch.setattr(proxy_mod, "_probe_servers", fake_probe_servers)

        result = runner.invoke(cli, ["health", "--json", "--names", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["servers"]["docs"]["overflowing"] == [
            "query_docs_filesystem_docs_by_lang_chain"
        ]
        # ``--json`` must stay schema-stable — these fields are documented as
        # part of the health contract.
        assert data["servers"]["docs"]["connected"] is True
        assert data["servers"]["docs"]["tools"] == 2


# ── probe error helpers ──────────────────────────────────────────────────


class TestRootCauseMessage:
    """Unit-test ``_root_cause_message`` directly. Cheaper than driving the
    whole CLI to exercise edge cases like deeply nested groups or
    self-referential cycles."""

    def test_returns_str_for_plain_exception(self):
        from memtomem_stm.cli.proxy import _root_cause_message

        assert _root_cause_message(ValueError("boom")) == "boom"

    def test_falls_back_to_type_name_for_empty_str(self):
        from memtomem_stm.cli.proxy import _root_cause_message

        # ``str(asyncio.CancelledError())`` is empty; the helper should
        # surface the type name rather than a useless empty string.
        import asyncio

        assert _root_cause_message(asyncio.CancelledError()) == "CancelledError"

    def test_unwraps_baseexceptiongroup_to_first_leaf(self):
        from memtomem_stm.cli.proxy import _root_cause_message

        leaf = RuntimeError("real cause")
        group = BaseExceptionGroup("unhandled errors in a TaskGroup", [leaf])
        assert _root_cause_message(group) == "real cause"

    def test_unwraps_nested_groups(self):
        from memtomem_stm.cli.proxy import _root_cause_message

        leaf = ConnectionError("network down")
        inner = BaseExceptionGroup("inner", [leaf])
        outer = BaseExceptionGroup("outer", [inner])
        assert _root_cause_message(outer) == "network down"


class TestSurfacingCommand:
    """`mms surfacing <server> [on|off]` toggles per-upstream surfacing in
    stm_proxy.json; `mms list` renders the effective state (SURFACING
    column — #614 moved the per-server view off `mms status`)."""

    @staticmethod
    def _seed(config: Path, *, surfacing_enabled: bool | None = None) -> None:
        entry: dict = {"prefix": "c7", "command": "echo", "args": []}
        if surfacing_enabled is not None:
            entry["surfacing_enabled"] = surfacing_enabled
        config.write_text(
            json.dumps({"enabled": True, "upstream_servers": {"context7": entry}}),
            encoding="utf-8",
        )

    def test_show_defaults_to_on(self, runner, config):
        self._seed(config)
        result = runner.invoke(cli, ["surfacing", "context7", *_cfg_args(config)])
        assert result.exit_code == 0
        assert "surfacing for 'context7': on" in result.output

    def test_off_persists_flag(self, runner, config):
        self._seed(config)
        result = runner.invoke(cli, ["surfacing", "context7", "off", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(config.read_text())
        assert data["upstream_servers"]["context7"]["surfacing_enabled"] is False

    def test_on_re_enables(self, runner, config):
        self._seed(config, surfacing_enabled=False)
        result = runner.invoke(cli, ["surfacing", "context7", "on", *_cfg_args(config)])
        assert result.exit_code == 0
        data = json.loads(config.read_text())
        assert data["upstream_servers"]["context7"]["surfacing_enabled"] is True

    def test_unknown_server_errors(self, runner, config):
        self._seed(config)
        result = runner.invoke(cli, ["surfacing", "ghost", "off", *_cfg_args(config)])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_invalid_state_rejected(self, runner, config):
        self._seed(config)
        result = runner.invoke(cli, ["surfacing", "context7", "maybe", *_cfg_args(config)])
        assert result.exit_code != 0

    def test_list_renders_surfacing_off(self, runner, config):
        """#614: the per-server surfacing state's visible home is the
        ``mms list`` SURFACING column (status is a config summary now)."""
        self._seed(config, surfacing_enabled=False)
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 0
        header = next(line for line in result.output.splitlines() if "SURFACING" in line)
        row = next(line for line in result.output.splitlines() if line.startswith("context7"))
        assert row[header.index("SURFACING") :].startswith("off")

    def test_list_renders_surfacing_on_by_default(self, runner, config):
        self._seed(config)
        result = runner.invoke(cli, ["list", *_cfg_args(config)])
        assert result.exit_code == 0
        header = next(line for line in result.output.splitlines() if "SURFACING" in line)
        row = next(line for line in result.output.splitlines() if line.startswith("context7"))
        assert row[header.index("SURFACING") :].startswith("on")


class TestTune:
    """`mms tune [--apply]` — preview and apply CompressionTuner overrides (#615).

    Seeds a real metrics DB so the CLI exercises the same offline
    MetricsStore → CompressionTuner path the command wires up; recommendation
    heuristics themselves are covered in tests/test_tuner.py.
    """

    @staticmethod
    def _seed_config(config: Path, metrics_db: Path, **extra) -> None:
        payload = {
            "enabled": True,
            "upstream_servers": {"srv": {"prefix": "s", "command": "npx"}},
            "metrics": {"db_path": str(metrics_db)},
            **extra,
        }
        config.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _seed_metrics(metrics_db: Path, *, server: str = "srv", tool: str = "big_tool") -> None:
        """Six calls, four ratio violations at 30000 chars → deterministic H1:
        current server-level budget 8000 → recommended max(24000, 10000) = 24000.
        Call count 6 stays below MEDIUM_CONFIDENCE_CALLS so H3 cannot fire."""
        from memtomem_stm.proxy.metrics import CallMetrics
        from memtomem_stm.proxy.metrics_store import MetricsStore

        store = MetricsStore(metrics_db)
        store.initialize()
        try:
            for i in range(6):
                store.record(
                    CallMetrics(
                        server=server,
                        tool=tool,
                        original_chars=30000,
                        compressed_chars=8000,
                        cleaned_chars=30000,
                        compression_strategy="truncate",
                        ratio_violation=i < 4,
                    )
                )
        finally:
            store.close()

    def test_preview_no_metrics_db_is_noop_and_creates_nothing(
        self, runner, config, tmp_path
    ):
        metrics_db = tmp_path / "metrics.db"
        self._seed_config(config, metrics_db)
        result = runner.invoke(cli, ["tune", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        assert "nothing to tune" in result.output
        assert not metrics_db.exists()

    def test_preview_renders_diff_and_does_not_write(self, runner, config, tmp_path):
        metrics_db = tmp_path / "metrics.db"
        self._seed_config(config, metrics_db)
        self._seed_metrics(metrics_db)
        before = config.read_bytes()
        result = runner.invoke(cli, ["tune", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        assert "srv/big_tool" in result.output
        assert "max_result_chars: 8000 -> 24000" in result.output
        assert "violation rate" in result.output
        assert "confidence" in result.output
        assert "--apply" in result.output
        assert config.read_bytes() == before

    def test_preview_json_shape(self, runner, config, tmp_path):
        metrics_db = tmp_path / "metrics.db"
        self._seed_config(config, metrics_db)
        self._seed_metrics(metrics_db)
        result = runner.invoke(cli, ["tune", "--json", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["since_hours"] == 24.0
        assert payload["tool_filter"] is None
        assert payload["skipped"] == []
        (change,) = payload["changes"]
        assert change["server"] == "srv"
        assert change["tool"] == "big_tool"
        assert change["field"] == "max_result_chars"
        assert change["current"] == "8000"
        assert change["recommended"] == "24000"
        assert change["confidence"] == "low"

    def test_json_with_apply_is_a_usage_error(self, runner, config, tmp_path):
        self._seed_config(config, tmp_path / "metrics.db")
        result = runner.invoke(cli, ["tune", "--json", "--apply", "--yes", *_cfg_args(config)])
        assert result.exit_code == 2
        assert "preview format" in result.output

    def test_apply_yes_writes_typed_overrides_and_preserves_unknown_keys(
        self, runner, config, tmp_path
    ):
        from memtomem_stm.proxy.config import ProxyConfig

        metrics_db = tmp_path / "metrics.db"
        self._seed_config(config, metrics_db, future_top_level={"keep": 1})
        data = json.loads(config.read_text())
        data["upstream_servers"]["srv"]["future_server_key"] = "keep me"
        config.write_text(json.dumps(data), encoding="utf-8")
        self._seed_metrics(metrics_db)

        result = runner.invoke(cli, ["tune", "--apply", "--yes", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        assert "Applied 1 override(s) to 1 tool(s)." in result.output

        saved = json.loads(config.read_text())
        override = saved["upstream_servers"]["srv"]["tool_overrides"]["big_tool"]
        assert override["max_result_chars"] == 24000
        assert isinstance(override["max_result_chars"], int)
        assert saved["future_top_level"] == {"keep": 1}
        assert saved["upstream_servers"]["srv"]["future_server_key"] == "keep me"
        assert ProxyConfig.model_validate(saved).upstream_servers["srv"] is not None

    def test_apply_creates_timestamped_backup_with_original_bytes(
        self, runner, config, tmp_path
    ):
        metrics_db = tmp_path / "metrics.db"
        self._seed_config(config, metrics_db)
        self._seed_metrics(metrics_db)
        before = config.read_bytes()

        result = runner.invoke(cli, ["tune", "--apply", "--yes", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        backups = sorted(config.parent.glob("stm_proxy.json.bak-*"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == before
        if sys.platform != "win32":  # chmod is a near-no-op on Windows
            assert (backups[0].stat().st_mode & 0o777) == 0o600
        assert str(backups[0]) in result.output  # restore hint names the backup

    def test_backup_same_second_collision_uses_numbered_slot(self, tmp_path, monkeypatch):
        from memtomem_stm.cli import proxy as proxy_mod

        target = tmp_path / "stm_proxy.json"
        target.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(proxy_mod, "utc_now_iso", lambda: "2026-07-04T10:15:30Z")
        first = proxy_mod._backup_config_snapshot(target, "one")
        second = proxy_mod._backup_config_snapshot(target, "two")
        assert first.name == "stm_proxy.json.bak-20260704T101530Z"
        assert second.name == "stm_proxy.json.bak-20260704T101530Z.1"
        assert first.read_text() == "one"
        assert second.read_text() == "two"

    def test_apply_non_tty_without_yes_errors(self, runner, config, tmp_path):
        metrics_db = tmp_path / "metrics.db"
        self._seed_config(config, metrics_db)
        self._seed_metrics(metrics_db)
        before = config.read_bytes()
        result = runner.invoke(cli, ["tune", "--apply", *_cfg_args(config)])
        assert result.exit_code == 1
        assert "--yes" in result.output
        assert config.read_bytes() == before

    def test_apply_with_no_recommendations_writes_nothing(self, runner, config, tmp_path):
        from memtomem_stm.proxy.metrics_store import MetricsStore

        metrics_db = tmp_path / "metrics.db"
        self._seed_config(config, metrics_db)
        store = MetricsStore(metrics_db)
        store.initialize()
        store.close()
        before = config.read_bytes()
        result = runner.invoke(cli, ["tune", "--apply", "--yes", *_cfg_args(config)])
        assert result.exit_code == 0, result.output
        assert "No recommendations" in result.output
        assert config.read_bytes() == before
        assert list(config.parent.glob("stm_proxy.json.bak-*")) == []

    def test_apply_refuses_env_masked_invalid_raw_file(
        self, runner, config, tmp_path, monkeypatch
    ):
        """A malformed per-tool entry in the FILE can be masked from the typed
        load by an env override (`_deep_merge` replaces a non-dict file leaf
        with an env-built dict). ``--apply`` edits the file, so it must refuse
        up front — otherwise a healthy sibling change strands behind the
        post-mutation validation abort. Preview still runs, with a warning."""
        metrics_db = tmp_path / "metrics.db"
        payload = {
            "enabled": True,
            "upstream_servers": {
                "srv": {
                    "prefix": "s",
                    "command": "npx",
                    "tool_overrides": {"bad": "not-a-dict"},
                }
            },
            "metrics": {"db_path": str(metrics_db)},
        }
        config.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setenv(
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__SRV__TOOL_OVERRIDES__BAD__MAX_RESULT_CHARS",
            "4000",
        )
        self._seed_metrics(metrics_db)  # healthy recommendation on srv/big_tool
        before = config.read_bytes()

        applied = runner.invoke(cli, ["tune", "--apply", "--yes", *_cfg_args(config)])
        assert applied.exit_code == 1
        assert "as written" in applied.output
        assert config.read_bytes() == before
        assert list(config.parent.glob("stm_proxy.json.bak-*")) == []

        preview = runner.invoke(cli, ["tune", *_cfg_args(config)])
        assert preview.exit_code == 0, preview.output
        assert "as written" in preview.output  # warning still surfaces
        assert "srv/big_tool" in preview.output  # analysis itself still renders

    def test_apply_refuses_schema_invalid_config(self, runner, config, tmp_path):
        metrics_db = tmp_path / "metrics.db"
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {"srv": {"command": "npx", "max_result_chars": -5}},
                    "metrics": {"db_path": str(metrics_db)},
                }
            ),
            encoding="utf-8",
        )
        before = config.read_bytes()
        result = runner.invoke(cli, ["tune", "--apply", "--yes", *_cfg_args(config)])
        assert result.exit_code == 1
        assert "config validate" in result.output
        assert config.read_bytes() == before


class TestMergeTuneChanges:
    """Merge policy for colliding TuningActions — unit-level, hand-built recs."""

    @staticmethod
    def _rec(server: str, tool: str, actions, confidence: str = "medium"):
        from memtomem_stm.proxy.tuner import TuningRecommendation

        return TuningRecommendation(
            server=server, tool=tool, confidence=confidence, actions=list(actions)
        )

    @staticmethod
    def _action(field: str, current: str | None, recommended: str, reason: str = "r"):
        from memtomem_stm.proxy.tuner import TuningAction

        return TuningAction(field=field, current=current, recommended=recommended, reason=reason)

    _SERVERS = {"srv": {"command": "npx"}}

    def test_compression_collision_last_wins_keeps_first_current(self):
        from memtomem_stm.cli.proxy import _merge_tune_changes

        rec = self._rec(
            "srv",
            "t",
            [
                self._action("compression", "auto", "skeleton", "pin dominant"),
                self._action("compression", None, "hybrid", "feedback: truncated"),
            ],
        )
        changes, skipped = _merge_tune_changes([rec], self._SERVERS)
        assert skipped == []
        (change,) = changes
        assert change.value == "hybrid"  # H4 (feedback) beats H3 (latency pin)
        assert change.current == "auto"  # resolved current survives the None-current winner
        assert "feedback: truncated" in change.reason
        assert "(merged 2 recommendations)" in change.reason

    @pytest.mark.parametrize("order", ["asc", "desc"])
    def test_max_result_chars_collision_takes_numeric_max(self, order):
        from memtomem_stm.cli.proxy import _merge_tune_changes

        values = ["16000", "24000"] if order == "asc" else ["24000", "16000"]
        rec = self._rec(
            "srv",
            "t",
            [self._action("max_result_chars", "8000", v) for v in values],
        )
        changes, _ = _merge_tune_changes([rec], self._SERVERS)
        (change,) = changes
        assert change.value == 24000
        assert isinstance(change.value, int)

    def test_unknown_server_is_skipped_with_warning(self):
        from memtomem_stm.cli.proxy import _merge_tune_changes

        rec = self._rec("ghost", "t", [self._action("max_result_chars", None, "16000")])
        changes, skipped = _merge_tune_changes([rec], self._SERVERS)
        assert changes == []
        assert len(skipped) == 1
        assert "ghost" in skipped[0]

    @pytest.mark.parametrize(
        "raw_entry",
        [
            "not-a-dict",
            {"command": "npx", "tool_overrides": "not-a-dict"},
            {"command": "npx", "tool_overrides": {"t": "not-a-dict"}},
        ],
        ids=["server-entry-not-dict", "tool-overrides-not-dict", "per-tool-override-not-dict"],
    )
    def test_unwritable_raw_entry_is_skipped_not_crashed(self, raw_entry):
        """An env override for ``upstream_servers`` can mask a malformed file
        entry from the typed validation the command runs, so the raw-dict
        writability guard is the only thing between the tuner and an
        AttributeError inside ``_apply_tune_changes``."""
        from memtomem_stm.cli.proxy import _merge_tune_changes

        rec = self._rec("srv", "t", [self._action("max_result_chars", None, "16000")])
        changes, skipped = _merge_tune_changes([rec], {"srv": raw_entry})
        assert changes == []
        assert len(skipped) == 1
        assert "malformed" in skipped[0]

    def test_non_numeric_budget_recommendation_is_skipped_not_fatal(self):
        from memtomem_stm.cli.proxy import _merge_tune_changes

        rec = self._rec(
            "srv",
            "t",
            [
                self._action("max_result_chars", None, "lots"),
                self._action("compression", "auto", "hybrid"),
            ],
        )
        changes, skipped = _merge_tune_changes([rec], self._SERVERS)
        (change,) = changes
        assert change.field == "compression"
        assert any("unusable" in s for s in skipped)

    def test_retention_floor_converted_to_float(self):
        from memtomem_stm.cli.proxy import _merge_tune_changes

        rec = self._rec("srv", "t", [self._action("retention_floor", None, "0.4")])
        changes, _ = _merge_tune_changes([rec], self._SERVERS)
        assert changes[0].value == 0.4
        assert isinstance(changes[0].value, float)
