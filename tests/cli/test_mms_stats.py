"""End-to-end tests for the ``mms stats`` CLI command.

``mms stats`` aggregates the persistent stores (``proxy_metrics.db`` and
``stm_feedback.db``) read-only. The load-bearing invariants:

- it reads from ``~/.memtomem`` resolved under the (patched) HOME,
- it NEVER creates or migrates a DB (read path), and
- empty / missing state renders cleanly instead of crashing.

Uses ``CliRunner`` + ``set_home`` so no real home directory is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import cli
from memtomem_stm.proxy.metrics import CallMetrics
from memtomem_stm.proxy.metrics_store import MetricsStore
from helpers import set_home


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed_metrics(home: Path) -> Path:
    db = home / ".memtomem" / "proxy_metrics.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = MetricsStore(db)
    store.initialize()
    try:
        store.record(
            CallMetrics(server="c7", tool="query-docs", original_chars=1000, compressed_chars=400)
        )
        store.record(
            CallMetrics(server="c7", tool="query-docs", original_chars=1000, compressed_chars=600)
        )
    finally:
        store.close()
    return db


class TestMmsStats:
    def test_human_output(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        _seed_metrics(tmp_path)

        result = runner.invoke(cli, ["stats"])

        assert result.exit_code == 0, result.output
        assert "Proxy / Compression" in result.output
        assert "query-docs" in result.output
        assert "calls: 2" in result.output
        assert "Surfacing" in result.output

    def test_json_output(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        _seed_metrics(tmp_path)

        result = runner.invoke(cli, ["stats", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # No config file was written under the patched HOME.
        assert data["config_status"] == "missing"
        assert data["compression"]["available"] is True
        assert data["compression"]["total_calls"] == 2
        assert data["compression"]["saved_ratio"] == 0.5
        # No feedback DB seeded → surfacing block degrades, not errors.
        assert data["surfacing"]["available"] is False

    def test_empty_state_renders_cleanly(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)

        result = runner.invoke(cli, ["stats"])

        assert result.exit_code == 0, result.output
        assert "No proxy metrics recorded yet." in result.output
        assert "No surfacing events recorded yet." in result.output

    def test_does_not_create_dbs(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)

        runner.invoke(cli, ["stats"])

        assert not (tmp_path / ".memtomem" / "proxy_metrics.db").exists()
        assert not (tmp_path / ".memtomem" / "stm_feedback.db").exists()

    def test_tool_filter(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        _seed_metrics(tmp_path)

        result = runner.invoke(cli, ["stats", "--tool", "query-docs", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["tool_filter"] == "query-docs"
        assert data["compression"]["total_calls"] == 2

    def test_env_enabled_still_honors_file_metrics_path(self, runner, tmp_path, monkeypatch):
        """When ``MEMTOMEM_STM_PROXY__ENABLED`` is set the server now loads
        the JSON file and overlays env on top
        (``server._apply_proxy_file_config``), so ``mms stats`` must honor a
        file-level ``metrics.db_path`` too — probing the same
        ``proxy_metrics.db`` the server writes to. Previously both sides
        skipped the file wholesale (``config_status: "env"``), which left
        the proxy enabled with zero upstreams and pointed stats at the
        wrong db."""
        set_home(monkeypatch, tmp_path)
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__ENABLED", "1")

        # A config file points metrics at a SEEDED db; env-enabled mode must
        # still pick it up.
        file_db = tmp_path / "file_metrics.db"
        store = MetricsStore(file_db)
        store.initialize()
        try:
            store.record(
                CallMetrics(server="c7", tool="query-docs", original_chars=100, compressed_chars=40)
            )
        finally:
            store.close()
        config = tmp_path / "stm_proxy.json"
        config.write_text(json.dumps({"enabled": True, "metrics": {"db_path": str(file_db)}}))

        result = runner.invoke(cli, ["stats", "--config", str(config), "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["config_status"] == "ok"
        assert data["enabled"] is True
        # The file's seeded db is probed — its row shows up.
        assert data["compression"]["total_calls"] == 1

    def test_missing_file_env_only_uses_pydantic_settings_parse(
        self, runner, tmp_path, monkeypatch
    ):
        """Missing config file + env-only setup: the server keeps
        ``STMConfig()``'s pydantic-settings parse (which decodes
        JSON-encoded complex env values like ``UPSTREAM_SERVERS``), so
        ``mms stats`` must mirror that instead of rebuilding from the
        raw-string env overlay — the rebuild would fail validation and
        report defaults (disabled, zero servers) for a working setup."""
        set_home(monkeypatch, tmp_path)
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__ENABLED", "1")
        monkeypatch.setenv(
            "MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS",
            '{"gh": {"prefix": "gh", "command": "echo"}}',
        )

        result = runner.invoke(
            cli, ["stats", "--config", str(tmp_path / "nonexistent.json"), "--json"]
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["config_status"] == "missing"
        assert data["enabled"] is True
        assert data["servers"] == 1
