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
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import cli
from memtomem_stm.proxy.metrics import CallMetrics
from memtomem_stm.proxy.metrics_store import MetricsStore
from memtomem_stm.proxy.progressive_reads_store import (
    ProgressiveReadsStore,
    read_progressive_summary,
)
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

    def test_per_tool_row_escapes_the_recorded_tool_name(self, runner, tmp_path, monkeypatch):
        """The per-tool table keys on names the *upstream* advertised, which
        the proxy recorded verbatim at call time — nobody here typed or
        reviewed them (#755). It is column-aligned, so a CR in one redraws
        the row above it; ``--json`` keeps the recorded name raw."""
        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        store = MetricsStore(db)
        store.initialize()
        try:
            store.record(
                CallMetrics(server="c7", tool="ev\ril", original_chars=1000, compressed_chars=400)
            )
        finally:
            store.close()

        result = runner.invoke(cli, ["stats"])
        assert result.exit_code == 0, result.output
        assert "ev\\u000Dil" in result.output
        assert "\r" not in result.output

        result = runner.invoke(cli, ["stats", "--json"])
        data = json.loads(result.output)
        assert data["compression"]["by_tool"][0]["tool"] == "ev\ril"

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

    def test_human_output_names_data_source(self, runner, tmp_path, monkeypatch):
        # #613: the on-disk vs live divergence used to live only in the
        # docstring — surface it in stdout with a pointer to the live tool.
        set_home(monkeypatch, tmp_path)
        _seed_metrics(tmp_path)

        result = runner.invoke(cli, ["stats"])

        assert result.exit_code == 0, result.output
        assert "Data   :" in result.output
        assert "on-disk stores only" in result.output
        assert "stm_admin MCP tool (action proxy_stats)" in result.output

    def test_json_output_includes_data_source(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        _seed_metrics(tmp_path)

        result = runner.invoke(cli, ["stats", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data_source"]
        assert "stm_admin MCP tool (action proxy_stats)" in data["data_source"]

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

    @pytest.mark.parametrize("surrogate", ["\ud800", "\udbff", "\udc00", "\udfff"])
    @pytest.mark.parametrize("as_json", [False, True])
    def test_unencodable_tool_filter_is_refused_in_both_output_modes(
        self, runner, tmp_path, monkeypatch, surrogate, as_json
    ):
        """An unencodable ``--tool`` is a rejected input, not an all-time zero.

        Both modes must say the same thing about the same argv. Before the
        boundary guard the human form died encoding its ``Filter :`` echo
        (exit 1, ``UnicodeEncodeError``) while ``--json`` exited 0 with
        ``compression.available = true`` and ``total_calls = 0`` — the seeded
        rows below are what makes that zero a lie rather than an empty store.
        """
        set_home(monkeypatch, tmp_path)
        _seed_metrics(tmp_path)
        argv = ["stats", "--tool", f"query-docs{surrogate}"]

        result = runner.invoke(cli, [*argv, "--json"] if as_json else argv)

        assert result.exit_code == 2, result.output
        assert isinstance(result.exception, SystemExit)
        assert "is not valid UTF-8" in result.output
        # Sanitized: the offending code unit is echoed only as its ASCII
        # ``\udxxx`` repr, so the refusal itself can always be printed.
        assert surrogate not in result.output
        assert f"\\u{ord(surrogate):04x}" in result.output
        result.output.encode("utf-8")
        # Refused before any store read, so no summary reached stdout.
        assert "Proxy / Compression" not in result.output
        assert "total_calls" not in result.output

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

    def test_config_flag_reaches_the_stmconfig_feedback_path(self, runner, tmp_path, monkeypatch):
        """#839: a per-field env override of a server the ``--config`` file
        declares used to break the bare ``STMConfig()`` behind the feedback
        path — silently falling back to the default location and ignoring
        ``MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH``."""
        set_home(monkeypatch, tmp_path)
        config = tmp_path / "stm_proxy.json"
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {"fake": {"prefix": "fk", "command": "file-server"}},
                }
            )
        )
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-server")
        custom = tmp_path / "custom_feedback.db"
        monkeypatch.setenv("MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH", str(custom))

        result = runner.invoke(cli, ["stats", "--config", str(config), "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["surfacing"]["path"] == str(custom.resolve())

    def test_no_flag_keeps_env_config_path_governing(self, runner, tmp_path, monkeypatch):
        """Without an explicit ``--config``, ``MEMTOMEM_STM_PROXY__CONFIG_PATH``
        names the file BOTH halves read. The STMConfig half stays a bare
        construction (an untyped flag resolves to the env path, but
        ``_explicit_config_path`` still reports it non-explicit, keeping the
        env-parse semantics of ``stm_config_for_cli(None)``), and since #848
        the file-loading half resolves the same env path instead of Click's
        literal default — the config_status/servers assertions below pin that
        second half."""
        set_home(monkeypatch, tmp_path)
        env_config = tmp_path / "env_named.json"
        env_config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "upstream_servers": {"fake": {"prefix": "fk", "command": "file-server"}},
                }
            )
        )
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__CONFIG_PATH", str(env_config))
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__UPSTREAM_SERVERS__FAKE__COMMAND", "env-server")
        custom = tmp_path / "custom_feedback.db"
        monkeypatch.setenv("MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH", str(custom))

        result = runner.invoke(cli, ["stats", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["surfacing"]["path"] == str(custom.resolve())
        # File half (#848): the env-named file was loaded — not the absent
        # default path, which would report config_status "missing" and 0
        # servers.
        assert data["config_status"] == "ok"
        assert data["servers"] == 1

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


def test_stats_explains_initial_delivery_and_unknown_legacy_accounting(
    runner, tmp_path, monkeypatch
):
    set_home(monkeypatch, tmp_path)
    _seed_metrics(tmp_path)
    result = runner.invoke(cli, ["stats", "--source", "mcp"])
    assert result.exit_code == 0, result.output
    assert "follow-up reads are excluded" in result.output
    assert "unclassified/legacy accounting" in result.output
    data = json.loads(runner.invoke(cli, ["stats", "--source", "mcp", "--json"]).output)
    assert data["compression"]["unclassified_mcp_calls"] == 2


def _seed_hook_row(db: Path) -> None:
    store = MetricsStore(db)
    store.initialize()
    try:
        store.record(
            CallMetrics(
                server="builtin",
                tool="Read",
                original_chars=100_000,
                compressed_chars=100_000,
                source="hook",
            )
        )
    finally:
        store.close()


def _seed_progressive(db: Path) -> None:
    store = ProgressiveReadsStore(db, retention_days=0)
    store.initialize()
    try:
        # Response A: 10,000 chars produced, first page of 2,000 plus one
        # follow-up of 2,000 → 4,000 delivered.
        store.record("a", None, "docs", "search", 0, 2000, 2000, 10_000, is_initial=True)
        store.record("a", None, "docs", "search", 2000, 2000, 4000, 10_000, is_initial=False)
        # Response B: 10,000 produced, only the first 1,000 delivered.
        store.record("b", None, "docs", "fetch", 0, 1000, 1000, 10_000, is_initial=True)
    finally:
        store.close()


class TestStatsBySource:
    def test_each_source_gets_its_own_ratio_when_both_are_present(
        self, runner, tmp_path, monkeypatch
    ):
        # The blended ratio is dominated by the high-volume source: here 2 MCP
        # calls saved 50% but one 100k-char hook row drags the aggregate to ~1%.
        set_home(monkeypatch, tmp_path)
        db = _seed_metrics(tmp_path)
        _seed_hook_row(db)

        result = runner.invoke(cli, ["stats"])

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert "    mcp   calls: 2  chars: 2,000 -> 1,000  (saved 50.0%)" in lines
        assert "    hook  calls: 1  chars: 100,000 -> 100,000  (saved 0.0%)" in lines
        # The aggregate keeps its shape and meaning.
        assert "  calls: 3  (errors: 0)" in lines

        data = json.loads(runner.invoke(cli, ["stats", "--json"]).output)
        assert data["compression"]["total_calls"] == 3
        by_source = data["compression_by_source"]
        assert by_source["mcp"]["total_calls"] == 2
        assert by_source["mcp"]["saved_ratio"] == 0.5
        assert by_source["hook"]["total_calls"] == 1
        assert by_source["hook"]["saved_ratio"] == 0.0

    def test_single_source_prints_no_split(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        _seed_metrics(tmp_path)

        result = runner.invoke(cli, ["stats"])

        assert result.exit_code == 0, result.output
        assert "    mcp   calls:" not in result.output
        assert "    hook  calls:" not in result.output

    def test_source_filter_omits_the_split(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = _seed_metrics(tmp_path)
        _seed_hook_row(db)

        data = json.loads(runner.invoke(cli, ["stats", "--source", "mcp", "--json"]).output)

        assert data["compression_by_source"] is None
        assert data["compression"]["total_calls"] == 2

    def test_tool_filter_reaches_each_source(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = _seed_metrics(tmp_path)
        _seed_hook_row(db)

        data = json.loads(runner.invoke(cli, ["stats", "--tool", "Read", "--json"]).output)

        assert data["compression_by_source"]["mcp"]["total_calls"] == 0
        assert data["compression_by_source"]["hook"]["total_calls"] == 1

    def test_structured_only_source_shows_no_text_ratio(self, runner, tmp_path, monkeypatch):
        # Structured deliveries are sized separately, so an MCP source made only
        # of them has no text ratio — printing "saved 0.0%" would report an
        # unmeasured ratio as a measured zero.
        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        store = MetricsStore(db)
        store.initialize()
        try:
            store.record(
                CallMetrics(
                    server="srv",
                    tool="read",
                    original_chars=2000,
                    compressed_chars=1500,
                    compression_accounting="initial_structured_v1",
                    delivery_format="structured",
                    wire_bytes=1700,
                )
            )
        finally:
            store.close()
        _seed_hook_row(db)

        lines = runner.invoke(cli, ["stats"]).output.splitlines()

        assert (
            "    mcp   calls: 1  chars: 0 -> 0  (no text ratio)  "
            "[1 structured, separate accounting]"
        ) in lines
        assert "    hook  calls: 1  chars: 100,000 -> 100,000  (saved 0.0%)" in lines

    def test_window_line_names_the_row_cap(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        _seed_metrics(tmp_path)

        result = runner.invoke(cli, ["stats"])

        assert "this config caps each source at 10,000 rows" in result.output
        assert "    (proxy.metrics.max_history). The oldest are trimmed in batches" in result.output
        assert "MEMTOMEM_STM_PROXY__METRICS__MAX_HISTORY" in result.output
        assert "all-time" not in result.output


class TestStatsProgressive:
    def test_delivered_vs_produced(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        _seed_progressive(tmp_path / ".memtomem" / "stm_feedback.db")

        result = runner.invoke(cli, ["stats"])

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert "  responses: 2  (with follow-up reads: 1; reads recorded: 3)" in lines
        assert "  chars: 20,000 produced -> 5,000 delivered (25.0%)" in lines
        assert "Read volume:" not in result.output

        prog = json.loads(runner.invoke(cli, ["stats", "--json"]).output)["progressive"]
        assert prog["produced_chars"] == 20_000
        assert prog["delivered_chars"] == 5000
        assert prog["delivered_ratio"] == 0.25
        assert prog["unclassified_reads"] == 0

    def test_tool_filter_applies(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        _seed_progressive(tmp_path / ".memtomem" / "stm_feedback.db")

        prog = json.loads(runner.invoke(cli, ["stats", "--tool", "fetch", "--json"]).output)[
            "progressive"
        ]

        assert prog["total_responses"] == 1
        assert prog["produced_chars"] == 10_000
        assert prog["delivered_chars"] == 1000

    def test_empty_state(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)

        result = runner.invoke(cli, ["stats"])

        assert result.exit_code == 0, result.output
        assert "No progressive responses recorded yet." in result.output

    def test_collection_disabled_still_reads_retained_rows(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        _seed_progressive(tmp_path / ".memtomem" / "stm_feedback.db")
        config = tmp_path / ".memtomem" / "stm_proxy.json"
        config.write_text(json.dumps({"progressive_reads": {"enabled": False}}))

        result = runner.invoke(cli, ["stats", "--config", str(config)])

        assert result.exit_code == 0, result.output
        assert "Collection disabled (proxy.progressive_reads.enabled = false)." in result.output
        assert "  responses: 2  (with follow-up reads: 1; reads recorded: 3)" in result.output

    def test_reads_the_progressive_db_path_not_the_feedback_db(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        custom = tmp_path / "reads.db"
        _seed_progressive(custom)
        config = tmp_path / ".memtomem" / "stm_proxy.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"progressive_reads": {"db_path": str(custom)}}))

        prog = json.loads(runner.invoke(cli, ["stats", "--config", str(config), "--json"]).output)[
            "progressive"
        ]

        assert prog["path"] == str(custom.resolve())
        assert prog["total_responses"] == 2
        # The default feedback DB was never created by the read.
        assert not (tmp_path / ".memtomem" / "stm_feedback.db").exists()

    def test_invalid_config_locates_dbs_from_the_environment(self, runner, tmp_path, monkeypatch):
        # The server keeps its env-built config when the file fails to load, so
        # stats must probe the env-moved DBs rather than the defaults.
        set_home(monkeypatch, tmp_path)
        reads = tmp_path / "env_reads.db"
        _seed_progressive(reads)
        metrics = tmp_path / "env_metrics.db"
        _seed_hook_row(metrics)
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__PROGRESSIVE_READS__DB_PATH", str(reads))
        monkeypatch.setenv("MEMTOMEM_STM_PROXY__METRICS__DB_PATH", str(metrics))
        config = tmp_path / ".memtomem" / "stm_proxy.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("{not json")

        data = json.loads(runner.invoke(cli, ["stats", "--json"]).output)

        assert data["config_status"] == "invalid"
        assert data["progressive"]["total_responses"] == 2
        assert data["compression"]["total_calls"] == 1


class TestReadProgressiveSummary:
    def test_missing_file_is_unavailable_and_not_created(self, tmp_path):
        db = tmp_path / "nope" / "reads.db"

        summary = read_progressive_summary(db)

        assert summary["available"] is False
        assert summary["error"] is None
        assert not db.exists()
        assert not db.parent.exists()

    def test_corrupt_file_reports_an_error(self, tmp_path):
        db = tmp_path / "reads.db"
        db.write_bytes(b"not a sqlite database at all" * 100)

        summary = read_progressive_summary(db)

        assert summary["available"] is False
        assert summary["error"]

    def test_db_without_the_table_is_unavailable(self, tmp_path):
        db = tmp_path / "reads.db"
        sqlite3.connect(db).close()

        summary = read_progressive_summary(db)

        assert summary["available"] is False
        assert summary["error"] is None

    def test_legacy_schema_and_aged_rows_are_left_untouched(self, tmp_path):
        # A pre-``is_initial`` table holding a row far past the 90-day window:
        # ``initialize()`` would migrate the table and purge the row; the reader
        # must do neither, and must count the legacy rows as unclassified.
        db = tmp_path / "reads.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE progressive_reads (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "key TEXT NOT NULL, trace_id TEXT, server TEXT NOT NULL, tool TEXT NOT NULL, "
            "offset INTEGER NOT NULL, chars INTEGER NOT NULL, served_to INTEGER NOT NULL, "
            "total_chars INTEGER NOT NULL, created_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO progressive_reads (key, server, tool, offset, chars, served_to, "
            "total_chars, created_at) VALUES ('k', 's', 't', 0, 500, 500, 4000, 1.0)"
        )
        conn.commit()
        conn.close()
        before = db.read_bytes()

        summary = read_progressive_summary(db)

        assert summary["available"] is True
        assert summary["classified_supported"] is False
        assert summary["unclassified_reads"] == 1
        assert summary["produced_chars"] == 4000
        assert summary["delivered_chars"] == 500
        assert summary["oldest_at"] == 1.0
        assert db.read_bytes() == before

    def test_unencodable_tool_filter_is_empty_but_available(self, tmp_path):
        db = tmp_path / "reads.db"
        _seed_progressive(db)

        summary = read_progressive_summary(db, tool="bad\udcff")

        assert summary["available"] is True
        assert summary["classified_supported"] is True
        assert summary["total_responses"] == 0

    def test_rereads_can_push_the_ratio_above_one(self, tmp_path):
        db = tmp_path / "reads.db"
        store = ProgressiveReadsStore(db, retention_days=0)
        store.initialize()
        try:
            store.record("k", None, "s", "t", 0, 1000, 1000, 1000, is_initial=True)
            store.record("k", None, "s", "t", 0, 1000, 1000, 1000, is_initial=False)
        finally:
            store.close()

        summary = read_progressive_summary(db)

        assert summary["produced_chars"] == 1000
        assert summary["delivered_chars"] == 2000
        assert summary["delivered_ratio"] == 2.0

    def test_lone_follow_up_row_counts_as_followed_up(self, tmp_path):
        # The initial row of a response can be missing (a failed write, or
        # purged while a later read survived); the follow-up marker still says
        # the agent read further.
        db = tmp_path / "reads.db"
        store = ProgressiveReadsStore(db, retention_days=0)
        store.initialize()
        try:
            store.record("k", None, "s", "t", 2000, 1000, 3000, 9000, is_initial=False)
        finally:
            store.close()

        summary = read_progressive_summary(db)

        assert summary["total_responses"] == 1
        assert summary["follow_up_responses"] == 1

    def test_zero_produced_does_not_divide_by_zero(self, tmp_path):
        db = tmp_path / "reads.db"
        store = ProgressiveReadsStore(db, retention_days=0)
        store.initialize()
        try:
            store.record("k", None, "s", "t", 0, 0, 0, 0, is_initial=True)
        finally:
            store.close()

        summary = read_progressive_summary(db)

        assert summary["available"] is True
        assert summary["delivered_ratio"] == 0.0


class TestMmsStatsAllTime:
    """``--all-time`` swaps the compression section to the daily rollup (#1316)."""

    @staticmethod
    def _seed_trimmed(home: Path) -> Path:
        db = home / ".memtomem" / "proxy_metrics.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        store = MetricsStore(db, max_history=1)
        store.initialize()
        try:
            for comp in (400, 600, 500):
                store.record(
                    CallMetrics(
                        server="c7", tool="query-docs", original_chars=1000, compressed_chars=comp
                    )
                )
        finally:
            store.close()
        return db

    def test_human_output_counts_trimmed_rows(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        self._seed_trimmed(tmp_path)

        retained = runner.invoke(cli, ["stats"])
        assert retained.exit_code == 0, retained.output
        assert "calls: 1 " in retained.output, "positive control: the cap trimmed"

        result = runner.invoke(cli, ["stats", "--all-time"])
        assert result.exit_code == 0, result.output
        assert "Proxy / Compression (all time)" in result.output
        assert "calls: 3 " in result.output
        assert "chars: 3,000 -> 1,500" in result.output
        assert "Window: all time" in result.output
        assert "retained rows only" not in result.output
        # Fresh DB: nothing was trimmed before the rollup existed.
        assert "Incomplete before" not in result.output

    def test_json_names_the_scope_and_coverage(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        self._seed_trimmed(tmp_path)

        data = json.loads(runner.invoke(cli, ["stats", "--json", "--all-time"]).output)
        assert data["compression_scope"] == "all_time"
        assert data["compression"]["total_calls"] == 3
        rollup = data["compression"]["rollup"]
        assert rollup["installed"] is True
        assert rollup["lost_before_install"] == 0
        assert rollup["incomplete_before"] is None
        assert data["compression_by_source"]["mcp"]["total_calls"] == 3

        plain = json.loads(runner.invoke(cli, ["stats", "--json"]).output)
        assert plain["compression_scope"] == "retained"
        assert "rollup" not in plain["compression"]

    def test_pre_rollup_db_says_unavailable_and_stays_untouched(
        self, runner, tmp_path, monkeypatch
    ):
        set_home(monkeypatch, tmp_path)
        db = _seed_metrics(tmp_path)
        raw = sqlite3.connect(str(db))
        raw.execute("DROP TRIGGER proxy_metrics_rollup_on_delete_v1")
        raw.execute("DROP TABLE proxy_metrics_daily")
        raw.commit()
        raw.close()

        result = runner.invoke(cli, ["stats", "--all-time"])

        assert result.exit_code == 0, result.output
        assert "All-time totals unavailable" in result.output
        assert "calls:" not in result.output.split("Progressive delivery")[0]
        raw = sqlite3.connect(str(db))
        tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master")}
        raw.close()
        assert "proxy_metrics_daily" not in tables

    def test_incomplete_coverage_line(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = self._seed_trimmed(tmp_path)
        raw = sqlite3.connect(str(db))
        raw.execute(
            "UPDATE metrics_meta SET value = '7' WHERE key = 'rollup_rows_lost_before_install'"
        )
        raw.execute(
            "UPDATE metrics_meta SET value = '4102444800.0' WHERE key = 'rollup_installed_at'"
        )
        raw.commit()
        raw.close()

        result = runner.invoke(cli, ["stats", "--all-time"])

        assert result.exit_code == 0, result.output
        assert "Incomplete before 2100-01-01 00:00 UTC: 7 rows were trimmed" in result.output

    def test_unknown_loss_is_not_reported_as_a_loss(self, runner, tmp_path, monkeypatch):
        """Markers erased from a running rollup and re-created (#1348): whether any
        row predates the trigger is unknown, so the line must not assert a trim."""
        set_home(monkeypatch, tmp_path)
        db = self._seed_trimmed(tmp_path)
        raw = sqlite3.connect(str(db))
        raw.execute("DELETE FROM metrics_meta")
        raw.commit()
        raw.close()
        store = MetricsStore(db)
        store.initialize()
        store.close()
        raw = sqlite3.connect(str(db))
        assert raw.execute(
            "SELECT value FROM metrics_meta WHERE key = 'rollup_rows_lost_before_install'"
        ).fetchone() == ("unknown",), "positive control: the re-created count is unknown"
        raw.execute(
            "UPDATE metrics_meta SET value = '4102444800.0' WHERE key = 'rollup_installed_at'"
        )
        raw.commit()
        raw.close()

        result = runner.invoke(cli, ["stats", "--all-time"])

        assert result.exit_code == 0, result.output
        assert "Possibly incomplete before 2100-01-01 00:00 UTC" in result.output
        assert "is unknown" in result.output
        assert "rows were trimmed" not in result.output
        assert "Incomplete before" not in result.output

    def test_coverage_outside_pythons_date_range_renders(self, runner, tmp_path, monkeypatch):
        """SQLite dates year 0, which ``datetime`` refuses; the report must not
        crash, whether the row is retained or already rolled up."""
        set_home(monkeypatch, tmp_path)
        db = self._seed_trimmed(tmp_path)
        raw = sqlite3.connect(str(db))
        raw.execute("UPDATE proxy_metrics SET created_at = -62167219200")
        raw.commit()
        raw.close()
        retained = runner.invoke(cli, ["stats", "--all-time"])
        assert retained.exit_code == 0, retained.output
        assert "since epoch -62167219200.0 UTC" in retained.output

        raw = sqlite3.connect(str(db))
        raw.execute("DELETE FROM proxy_metrics")
        raw.commit()
        raw.close()
        rolled = runner.invoke(cli, ["stats", "--all-time"])
        assert rolled.exit_code == 0, rolled.output
        assert "since epoch -62167219200.0 UTC" in rolled.output

    def test_non_finite_sizes_render_in_both_output_modes(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = self._seed_trimmed(tmp_path)
        store = MetricsStore(db, max_history=1)
        store.initialize()
        try:
            store.record(
                CallMetrics(
                    server="c7",
                    tool="query-docs",
                    original_chars=float("inf"),
                    compressed_chars=float("-inf"),
                )
            )
            store.record(
                CallMetrics(
                    server="builtin",
                    tool="Bash",
                    original_chars=10,
                    compressed_chars=10,
                    source="hook",
                )
            )
        finally:
            store.close()

        human = runner.invoke(cli, ["stats", "--all-time"])
        assert human.exit_code == 0, human.output
        assert "mcp   calls: 4  chars: 3,000 -> 1,500" in human.output

        def _reject(token: str) -> None:
            raise AssertionError(f"non-standard JSON number {token}")

        data = json.loads(
            runner.invoke(cli, ["stats", "--json", "--all-time"]).output, parse_constant=_reject
        )
        assert data["compression"]["total_original_chars"] == 3010
