"""Multi-command user-journey tests through the top-level ``mms`` group.

Unit coverage for each command lives in ``test_proxy_cli.py`` /
``test_hook_cmd.py``; what was missing (found by an e2e QA campaign run as
real subprocesses) is the *sequence* a user actually types — where one
command's write is the next command's read. These journeys run unmarked in
the default CI filter on every platform, so they use only in-process
CliRunner invocations, the bundled demo server entry, and hermetic HOMEs:
no external binaries, no network, no TTY.

Kept deliberately journey-shaped: each test walks one documented flow
end-to-end and asserts the user-visible signal at every step, not the
internals (those pins live next to the per-command unit tests).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import cli
from helpers import set_home


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hermetic HOME + cwd, with no inherited STM env-var overrides.

    The cwd matters: ``doctor``/discovery also read ``<cwd>/.mcp.json``, so
    standing in the developer's repo would leak its project config into a
    "fresh machine" journey.
    """
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    for var in list(os.environ):
        if var.startswith(("MEMTOMEM_STM_HOOK__", "MEMTOMEM_STM_SURFACING__")):
            monkeypatch.delenv(var, raising=False)
    return home


def _config_path(home: Path) -> Path:
    return home / ".memtomem" / "stm_proxy.json"


class TestFirstRunDemoJourney:
    """docs/getting-started.md "shortest first success": init --demo, then
    the read commands a beginner is told to run, then clean removal."""

    def test_init_status_list_stats_surfacing_remove(self, runner, home):
        cfg = _config_path(home)

        res = runner.invoke(
            cli, ["init", "--demo", "--client", "skip", "--no-validate", "--lang", "en"]
        )
        assert res.exit_code == 0, res.output
        assert "Saved to:" in res.output
        assert cfg.exists()

        res = runner.invoke(cli, ["status"])
        assert res.exit_code == 0, res.output
        assert "Servers: 1" in res.output
        assert "Enabled: yes" in res.output

        res = runner.invoke(cli, ["list"])
        assert res.exit_code == 0, res.output
        assert "demo" in res.output
        assert "1 server(s) configured." in res.output

        res = runner.invoke(cli, ["stats"])
        assert res.exit_code == 0, res.output
        assert "No proxy metrics recorded yet" in res.output

        # The per-upstream surfacing toggle round-trips through the config
        # the earlier commands read.
        res = runner.invoke(cli, ["surfacing", "demo", "off"])
        assert res.exit_code == 0, res.output
        assert "off" in res.output
        assert json.loads(cfg.read_text())["upstream_servers"]["demo"]["surfacing_enabled"] is False
        res = runner.invoke(cli, ["surfacing", "demo", "on"])
        assert res.exit_code == 0, res.output

        res = runner.invoke(cli, ["remove", "demo", "--yes"])
        assert res.exit_code == 0, res.output
        res = runner.invoke(cli, ["list"])
        assert res.exit_code == 0, res.output
        assert "No upstream servers configured." in res.output

    def test_init_refuses_second_run_and_resume_hint_works(self, runner, home):
        """The bootstrap-only invariant as a journey: a second `init` aborts
        with the `mms add` pointer instead of clobbering the config."""
        first = runner.invoke(
            cli, ["init", "--demo", "--client", "skip", "--no-validate", "--lang", "en"]
        )
        assert first.exit_code == 0, first.output
        before = _config_path(home).read_text()

        second = runner.invoke(
            cli, ["init", "--demo", "--client", "skip", "--no-validate", "--lang", "en"]
        )
        assert second.exit_code == 1
        assert "already exists" in second.output
        assert "mms add" in second.output
        assert _config_path(home).read_text() == before, "second init must not rewrite"


class TestCorruptConfigRecoveryJourney:
    """docs/guides/operations.md "Recovering from a broken config file":
    the two failure shapes, diagnosed and repaired in the documented order."""

    def test_truncated_json_diagnose_and_restore(self, runner, home):
        res = runner.invoke(
            cli, ["init", "--demo", "--client", "skip", "--no-validate", "--lang", "en"]
        )
        assert res.exit_code == 0, res.output
        cfg = _config_path(home)
        good = cfg.read_text()

        cfg.write_text(good[:40])  # truncate mid-value → invalid JSON

        res = runner.invoke(cli, ["config", "validate"])
        assert res.exit_code == 1
        assert "invalid JSON" in res.output

        res = runner.invoke(cli, ["status"])
        assert res.exit_code == 1
        assert "Failed to parse" in res.output

        cfg.write_text(good)  # restore from backup (step 2 of the runbook)
        res = runner.invoke(cli, ["config", "validate"])
        assert res.exit_code == 0, res.output
        assert "OK" in res.output

    def test_schema_invalid_config_warns_but_stays_inspectable(self, runner, home):
        res = runner.invoke(
            cli, ["init", "--demo", "--client", "skip", "--no-validate", "--lang", "en"]
        )
        assert res.exit_code == 0, res.output
        cfg = _config_path(home)
        data = json.loads(cfg.read_text())
        data["enabled"] = "yes-please"  # valid JSON, invalid schema
        data["compresion"] = {}  # misspelled key — ignored at runtime
        cfg.write_text(json.dumps(data))

        # validate is the only command that reports the unknown key.
        res = runner.invoke(cli, ["config", "validate"])
        assert res.exit_code == 1
        assert "enabled" in res.output
        assert "compresion" in res.output

        # Inspection stays lenient: status exits 0 but names the fallback.
        res = runner.invoke(cli, ["status"])
        assert res.exit_code == 0, res.output
        assert "fails validation" in res.output


class TestHookRoundtripJourney:
    """docs/guides/native-hooks.md install→inspect→uninstall on a seeded
    Claude Code settings file: dry-run by default, backup on apply, exact
    restore on uninstall."""

    def test_install_apply_uninstall_restores_settings(self, runner, home):
        settings = home / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text("{}")

        dry = runner.invoke(cli, ["hook", "install", "--host", "claude"])
        assert dry.exit_code == 0, dry.output
        assert "Would add" in dry.output
        assert settings.read_text() == "{}", "dry-run must not write"

        applied = runner.invoke(cli, ["hook", "install", "--host", "claude", "--apply"])
        assert applied.exit_code == 0, applied.output
        installed = json.loads(settings.read_text())
        assert "PostToolUse" in installed.get("hooks", {})
        assert settings.with_suffix(".json.bak").exists()

        # Idempotence: a second apply must not stack a duplicate block.
        again = runner.invoke(cli, ["hook", "install", "--host", "claude", "--apply"])
        assert again.exit_code == 0, again.output
        hooks = json.loads(settings.read_text())["hooks"]["PostToolUse"]
        assert len(hooks) == 1

        removed = runner.invoke(cli, ["hook", "uninstall", "--host", "claude", "--apply"])
        assert removed.exit_code == 0, removed.output
        assert json.loads(settings.read_text()) == {}
