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

    def test_env_enabled_bypasses_file_metrics_path(self, runner, tmp_path, monkeypatch):
        """When ``MEMTOMEM_STM_PROXY__ENABLED`` is set the server uses its
        env-only proxy config and ignores the JSON file (server.app_lifespan),
        so ``mms stats`` must too — otherwise it would probe a different
        ``proxy_metrics.db`` than the one the server writes to."""
        set_home(monkeypatch, tmp_path)
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__ENABLED", "1")

        # A config file points metrics at a SEEDED db that the env-enabled
        # server would ignore.
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
        assert data["config_status"] == "env"
        # The env path (default ~/.memtomem/proxy_metrics.db, unseeded under the
        # patched HOME) is used — NOT the file's seeded db — so its row must not
        # leak in.
        assert data["compression"]["total_calls"] == 0
