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


def _seed_hook_rows(db: Path, rows: list[tuple[str, str, int, int]], **store_kwargs) -> None:
    """``(server, tool, original_chars, compressed_chars)`` rows with ``source='hook'``."""
    db.parent.mkdir(parents=True, exist_ok=True)
    store = MetricsStore(db, **store_kwargs)
    store.initialize()
    try:
        for server, tool, orig, comp in rows:
            store.record(
                CallMetrics(
                    server=server,
                    tool=tool,
                    original_chars=orig,
                    compressed_chars=comp,
                    source="hook",
                )
            )
    finally:
        store.close()


_CLAUDE_AND_CODEX = [
    ("builtin:claude", "Bash", 20_000, 16_000),
    ("builtin:claude", "Read", 5_000, 5_000),
    ("builtin:codex", "Bash", 30_000, 30_000),
    ("builtin:codex", "Bash", 30_000, 30_000),
]
_CLAUDE_ENTRY = {
    "host": "claude",
    "calls": 2,
    "original_chars": 25_000,
    "compressed_chars": 21_000,
    "saved_ratio": 0.16,
    "can_replace_output": True,
}
_CODEX_ENTRY = {
    "host": "codex",
    "calls": 2,
    "original_chars": 60_000,
    "compressed_chars": 60_000,
    "saved_ratio": 0.0,
    "can_replace_output": False,
}


class TestStatsHookByHost:
    """The hook ratio is split by host (#1438).

    A host that cannot replace tool output only ever adds to the denominator,
    so one blended ``hook`` ratio says nothing about the host that can.
    """

    def test_each_host_gets_its_own_line_under_the_hook_source(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = _seed_metrics(tmp_path)
        _seed_hook_rows(db, _CLAUDE_AND_CODEX)

        result = runner.invoke(cli, ["stats"])

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        hook = lines.index("    hook  calls: 4  chars: 85,000 -> 81,000  (saved 4.7%)")
        # Largest host first; the one that cannot replace output has no ratio.
        assert lines[hook + 1 : hook + 3] == [
            "      codex  calls: 2  chars: 60,000  (measured only: host cannot replace output)",
            "      claude calls: 2  chars: 25,000 -> 21,000  (saved 16.0%)",
        ]
        # The longest label fits the per-tool table's server cell whole.
        assert any(line.startswith("  builtin:claude Bash ") for line in lines)

    def test_json_reports_the_hosts(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = _seed_metrics(tmp_path)
        _seed_hook_rows(db, _CLAUDE_AND_CODEX)

        data = json.loads(runner.invoke(cli, ["stats", "--json"]).output)

        assert data["hook_by_host"] == [_CODEX_ENTRY, _CLAUDE_ENTRY]

    def test_a_lone_host_that_cannot_replace_output_is_still_explained(
        self, runner, tmp_path, monkeypatch
    ):
        # Hook-only DB with one host: no per-source lines to hang the host
        # under, and a bare "saved 0.0%" would read as compression not working.
        set_home(monkeypatch, tmp_path)
        _seed_hook_rows(
            tmp_path / ".memtomem" / "proxy_metrics.db", [("builtin:codex", "Bash", 30_000, 30_000)]
        )

        result = runner.invoke(cli, ["stats"])

        assert result.exit_code == 0, result.output
        assert (
            "    codex  calls: 1  chars: 30,000  (measured only: host cannot replace output)"
            in result.output.splitlines()
        )

    def test_a_lone_host_that_can_replace_output_adds_no_line(self, runner, tmp_path, monkeypatch):
        # Its ratio is the hook ratio already.
        set_home(monkeypatch, tmp_path)
        _seed_hook_rows(
            tmp_path / ".memtomem" / "proxy_metrics.db",
            [("builtin:claude", "Bash", 20_000, 16_000)],
        )

        result = runner.invoke(cli, ["stats"])

        assert result.exit_code == 0, result.output
        assert "chars: 20,000 -> 16,000  (saved 4,000, 20.0%)" in result.output
        assert "claude calls:" not in result.output
        data = json.loads(runner.invoke(cli, ["stats", "--json"]).output)
        assert [entry["host"] for entry in data["hook_by_host"]] == ["claude"]

    def test_rows_without_a_recorded_host_are_reported_apart(self, runner, tmp_path, monkeypatch):
        # Rows written before the host was recorded, or by an older hook that
        # shares the DB, keep the bare ``builtin`` and cannot be attributed.
        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        _seed_hook_rows(
            db, [("builtin", "Bash", 90_000, 90_000), ("builtin:claude", "Bash", 20_000, 16_000)]
        )

        result = runner.invoke(cli, ["stats"])

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        # Last, whatever its volume.
        first = lines.index("    claude calls: 1  chars: 20,000 -> 16,000  (saved 20.0%)")
        assert lines[first + 1 : first + 3] == [
            "      status: not recorded 1",
            "    (host not recorded) calls: 1  chars: 90,000 -> 90,000  (saved 0.0%)",
        ]
        data = json.loads(runner.invoke(cli, ["stats", "--json"]).output)
        assert data["hook_by_host"][1] == {
            "host": None,
            "calls": 1,
            "original_chars": 90_000,
            "compressed_chars": 90_000,
            "saved_ratio": 0.0,
            "can_replace_output": None,
        }

    def test_a_host_this_version_does_not_know_gets_no_capability(
        self, runner, tmp_path, monkeypatch
    ):
        # ``get_adapter`` falls back to Claude for an unknown tag; the reader
        # must not, or a newer hook's rows would be labelled as replaceable.
        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        _seed_hook_rows(
            db, [("builtin:future", "Bash", 10_000, 10_000), ("builtin:codex", "Bash", 1, 1)]
        )

        data = json.loads(runner.invoke(cli, ["stats", "--json"]).output)

        assert [(e["host"], e["can_replace_output"]) for e in data["hook_by_host"]] == [
            ("future", None),
            ("codex", False),
        ]
        human = runner.invoke(cli, ["stats"]).output.splitlines()
        assert "    future calls: 1  chars: 10,000 -> 10,000  (saved 0.0%)" in human

    @pytest.mark.parametrize(
        ("source", "expected"),
        [("hook", [_CODEX_ENTRY, _CLAUDE_ENTRY]), ("mcp", None)],
    )
    def test_source_filter(self, runner, tmp_path, monkeypatch, source, expected):
        # ``--source hook`` drops the per-source breakdown the hosts hang under
        # by default; the split has to follow the hook summary itself.
        set_home(monkeypatch, tmp_path)
        db = _seed_metrics(tmp_path)
        _seed_hook_rows(db, _CLAUDE_AND_CODEX)

        data = json.loads(runner.invoke(cli, ["stats", "--source", source, "--json"]).output)
        human = runner.invoke(cli, ["stats", "--source", source]).output.splitlines()

        assert data["compression_by_source"] is None
        assert data["hook_by_host"] == expected
        shown = "    claude calls: 2  chars: 25,000 -> 21,000  (saved 16.0%)" in human
        assert shown is (expected is not None)

    def test_tool_filter_reaches_the_hosts(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = _seed_metrics(tmp_path)
        _seed_hook_rows(db, _CLAUDE_AND_CODEX)

        data = json.loads(runner.invoke(cli, ["stats", "--tool", "Read", "--json"]).output)

        assert data["hook_by_host"] == [
            {
                "host": "claude",
                "calls": 1,
                "original_chars": 5_000,
                "compressed_chars": 5_000,
                "saved_ratio": 0.0,
                "can_replace_output": True,
            }
        ]

    def test_hosts_survive_trimming_into_the_rollup(self, runner, tmp_path, monkeypatch):
        # The daily rollup is keyed by server, which is why the host rides there.
        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        _seed_hook_rows(db, _CLAUDE_AND_CODEX, max_history=1)

        raw = sqlite3.connect(str(db))
        try:
            retained = raw.execute("SELECT server, tool FROM proxy_metrics").fetchall()
            rolled = dict(
                raw.execute(
                    "SELECT server, SUM(calls) FROM proxy_metrics_daily GROUP BY server"
                ).fetchall()
            )
        finally:
            raw.close()
        # Positive control: rows of BOTH hosts left the raw table.
        assert retained == [("builtin:codex", "Bash")]
        assert rolled == {"builtin:claude": 2, "builtin:codex": 1}

        windowed = json.loads(runner.invoke(cli, ["stats", "--json"]).output)
        assert [entry["host"] for entry in windowed["hook_by_host"]] == ["codex"]
        data = json.loads(runner.invoke(cli, ["stats", "--json", "--all-time"]).output)
        assert data["hook_by_host"] == [_CODEX_ENTRY, _CLAUDE_ENTRY]
        human = runner.invoke(cli, ["stats", "--all-time"]).output.splitlines()
        assert "    claude calls: 2  chars: 25,000 -> 21,000  (saved 16.0%)" in human

    def test_an_upstream_server_with_a_hook_label_is_not_a_host(
        self, runner, tmp_path, monkeypatch
    ):
        # ``server`` is operator data for MCP rows: nothing stops an upstream
        # from being named like a hook label. Only ``source='hook'`` rows count.
        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        _seed_hook_rows(db, [("builtin:codex", "Bash", 30_000, 30_000)])
        store = MetricsStore(db)
        store.initialize()
        try:
            for server in ("builtin:claude", "builtin"):
                store.record(
                    CallMetrics(
                        server=server, tool="Bash", original_chars=8_000, compressed_chars=2_000
                    )
                )
        finally:
            store.close()

        data = json.loads(runner.invoke(cli, ["stats", "--json"]).output)

        assert data["compression_by_source"]["mcp"]["total_calls"] == 2
        assert data["hook_by_host"] == [
            {
                "host": "codex",
                "calls": 1,
                "original_chars": 30_000,
                "compressed_chars": 30_000,
                "saved_ratio": 0.0,
                "can_replace_output": False,
            }
        ]

    def test_host_tag_is_escaped_for_display(self, runner, tmp_path, monkeypatch):
        # The tag is read back from the DB file, like every other recorded name.
        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        _seed_hook_rows(
            db, [("builtin:\x1b[31mx", "Bash", 10, 10), ("builtin:codex", "Bash", 1, 1)]
        )

        result = runner.invoke(cli, ["stats"])

        assert result.exit_code == 0, result.output
        assert "\x1b" not in result.output
        assert (
            "    \\u001B[31mx calls: 1  chars: 10 -> 10  (saved 0.0%)" in result.output.splitlines()
        )


def _seed_hook_statuses(
    db: Path, rows: list[tuple[str, str, int, str | None]], **store_kwargs
) -> None:
    """``(server, tool, original_chars, status)`` hook rows; compressed only when
    the status says so, as the hook writes them."""
    db.parent.mkdir(parents=True, exist_ok=True)
    store = MetricsStore(db, **store_kwargs)
    store.initialize()
    try:
        for server, tool, orig, status in rows:
            store.record(
                CallMetrics(
                    server=server,
                    tool=tool,
                    original_chars=orig,
                    compressed_chars=orig // 2 if status == "compressed" else orig,
                    compression_strategy="truncate" if status == "compressed" else None,
                    source="hook",
                    hook_compression_status=status,
                )
            )
    finally:
        store.close()


_MIXED_STATUSES = [
    ("builtin:claude", "Bash", 20_000, "compressed"),
    ("builtin:claude", "Bash", 600, "below_threshold"),
    ("builtin:claude", "Bash", 900, "below_threshold"),
    ("builtin:claude", "Read", 4_000, "ineligible_tool"),
    ("builtin:claude", "Bash", 300, None),
    ("builtin:codex", "Bash", 30_000, "unsupported_host"),
    ("builtin:codex", "Bash", 30_000, "unsupported_host"),
]


class TestStatsHookStatus:
    """Why each retained hook call was or was not compressed (#1438)."""

    def test_capable_hosts_get_a_status_line(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = _seed_metrics(tmp_path)
        _seed_hook_statuses(db, _MIXED_STATUSES)

        result = runner.invoke(cli, ["stats"])

        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        claude = lines.index("      claude calls: 5  chars: 25,800 -> 15,800  (saved 38.8%)")
        assert lines[claude + 1] == (
            "        status: below_threshold 2, compressed 1, ineligible_tool 1, not recorded 1"
        )
        # The "measured only" note already explains a host that cannot replace.
        codex = lines.index(
            "      codex  calls: 2  chars: 60,000  (measured only: host cannot replace output)"
        )
        assert not lines[codex + 1].lstrip().startswith("status:")
        assert "unsupported_host" not in result.output

    def test_json_reports_every_host(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = _seed_metrics(tmp_path)
        _seed_hook_statuses(db, _MIXED_STATUSES)

        data = json.loads(runner.invoke(cli, ["stats", "--json"]).output)

        assert data["hook_status"] == {
            "scope": "retained",
            "by_host": [
                {
                    "host": "claude",
                    "statuses": [
                        {"status": "below_threshold", "calls": 2, "original_chars": 1_500},
                        {"status": "compressed", "calls": 1, "original_chars": 20_000},
                        {"status": "ineligible_tool", "calls": 1, "original_chars": 4_000},
                        {"status": None, "calls": 1, "original_chars": 300},
                    ],
                    "status_not_kept_calls": None,
                },
                {
                    "host": "codex",
                    "statuses": [
                        {"status": "unsupported_host", "calls": 2, "original_chars": 60_000}
                    ],
                    "status_not_kept_calls": None,
                },
            ],
        }
        # Reported once, at the top level, not again inside the hook summary.
        assert "hook_status" not in data["compression_by_source"]["hook"]

    @pytest.mark.parametrize(
        ("with_mcp", "indent"),
        [(True, "      "), (False, "    ")],
        ids=["under-the-hook-source", "under-the-total"],
    )
    def test_a_lone_capable_host_still_gets_its_status(
        self, runner, tmp_path, monkeypatch, with_mcp, indent
    ):
        set_home(monkeypatch, tmp_path)
        db = _seed_metrics(tmp_path) if with_mcp else tmp_path / ".memtomem" / "proxy_metrics.db"
        _seed_hook_statuses(
            db,
            [
                ("builtin:claude", "Bash", 20_000, "compressed"),
                ("builtin:claude", "Bash", 30_000, "retention_guard"),
                ("builtin:claude", "Bash", 30_000, "retention_guard"),
            ],
        )

        lines = runner.invoke(cli, ["stats"]).output.splitlines()

        assert f"{indent}status: retention_guard 2, compressed 1" in lines
        assert not any("claude calls:" in line for line in lines)

    def test_a_known_host_with_no_recorded_status_says_so(self, runner, tmp_path, monkeypatch):
        # Rows from part 1 of #1438: the host was recorded, the status not yet.
        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        _seed_hook_statuses(db, [("builtin:claude", "Bash", 300, None)])

        result = runner.invoke(cli, ["stats"])

        assert result.exit_code == 0, result.output
        assert "    status: not recorded 1" in result.output.splitlines()
        data = json.loads(runner.invoke(cli, ["stats", "--json"]).output)
        assert data["hook_status"]["by_host"] == [
            {
                "host": "claude",
                "statuses": [{"status": None, "calls": 1, "original_chars": 300}],
                "status_not_kept_calls": None,
            }
        ]

    def test_rows_with_neither_host_nor_status_add_no_line(self, runner, tmp_path, monkeypatch):
        # Older than both: ``(host not recorded)`` already says all there is.
        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        _seed_hook_statuses(
            db,
            [
                ("builtin", "Bash", 90_000, None),
                ("builtin:claude", "Bash", 20_000, "compressed"),
            ],
        )

        lines = runner.invoke(cli, ["stats"]).output.splitlines()

        unrecorded = next(i for i, line in enumerate(lines) if "(host not recorded)" in line)
        assert not lines[unrecorded + 1].lstrip().startswith("status:")
        assert "      status: compressed 1" in lines

    def test_order_is_by_calls_with_not_recorded_last_and_ties_by_name(
        self, runner, tmp_path, monkeypatch
    ):
        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        _seed_hook_statuses(
            db,
            [
                *[("builtin:claude", "Bash", 10, None)] * 3,
                ("builtin:claude", "Bash", 20_000, "compressed"),
                ("builtin:kimi", "Bash", 10, "unsupported_host"),
                ("builtin:codex", "Bash", 10, "unsupported_host"),
            ],
        )

        data = json.loads(runner.invoke(cli, ["stats", "--json"]).output)

        by_host = data["hook_status"]["by_host"]
        assert [entry["host"] for entry in by_host] == ["claude", "codex", "kimi"]
        assert [item["status"] for item in by_host[0]["statuses"]] == ["compressed", None]

    def test_a_lone_host_status_shares_the_snapshot_of_the_total_above_it(
        self, runner, tmp_path, monkeypatch
    ):
        """A row committed after the total was read must not appear in the
        status line printed under that total (hook-only DB, one host)."""
        from memtomem_stm.proxy import metrics_store

        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        _seed_hook_statuses(db, [("builtin:claude", "Bash", 20_000, "compressed")])
        real = metrics_store.read_compression_summary
        reads = []

        def _write_after_first_read(*args, **kwargs):
            result = real(*args, **kwargs)
            if not reads:
                _seed_hook_statuses(db, [("builtin:claude", "Bash", 20_000, "compressed")])
            reads.append(kwargs.get("source"))
            return result

        monkeypatch.setattr(metrics_store, "read_compression_summary", _write_after_first_read)

        lines = runner.invoke(cli, ["stats"]).output.splitlines()

        assert reads[0] is None  # the aggregate was read first, then the row landed
        assert "  calls: 1  (errors: 0)" in lines
        assert "    status: compressed 1" in lines

    def test_host_ties_do_not_depend_on_the_row_order(self):
        # SQLite happens to return groups sorted; the order must not rely on it.
        from memtomem_stm.cli.proxy import _hook_status

        rows = [
            {
                "server": f"builtin:{host}",
                "status": "unsupported_host",
                "not_kept": False,
                "calls": 1,
                "original_chars": 1,
            }
            for host in ("kimi", "codex", "cursor")
        ]
        hosts = [
            [e["host"] for e in _hook_status({"scope": "retained", "rows": order})["by_host"]]
            for order in (rows, rows[::-1])
        ]
        assert hosts == [["codex", "cursor", "kimi"]] * 2

    def test_status_not_kept_comes_last_whatever_its_count(self):
        from memtomem_stm.cli.proxy import _hook_status, _hook_status_line

        def row(status, calls, not_kept=False):
            return {
                "server": "builtin:claude",
                "status": status,
                "not_kept": not_kept,
                "calls": calls,
                "original_chars": calls,
            }

        value = {
            "scope": "all_time",
            "rows": [row(None, 9, not_kept=True), row(None, 2), row("compressed", 1)],
        }
        lines = _hook_status_line(_hook_status(value), "claude", "  ", True)
        assert lines == ["  status: compressed 1, not recorded 2, status not kept 9"]

    def test_json_status_for_unreadable_and_pre_source_stores(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"not a sqlite database" * 100)
        corrupt = json.loads(runner.invoke(cli, ["stats", "--json"]).output)
        assert corrupt["hook_status"] is None

        db.unlink()
        raw = sqlite3.connect(str(db))
        try:
            raw.execute(
                "CREATE TABLE proxy_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "server TEXT, tool TEXT, original_chars INTEGER, compressed_chars INTEGER, "
                "cleaned_chars INTEGER DEFAULT 0, created_at REAL)"
            )
            raw.execute(
                "INSERT INTO proxy_metrics (server, tool, original_chars, compressed_chars, "
                "created_at) VALUES ('builtin:claude', 'Bash', 100, 40, 0)"
            )
            raw.commit()
        finally:
            raw.close()
        legacy = json.loads(runner.invoke(cli, ["stats", "--json"]).output)
        assert legacy["hook_status"] == {"scope": "retained", "by_host": []}

    def test_a_stored_value_outside_the_set_prints_as_other(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        _seed_hook_statuses(db, [("builtin:claude", "Bash", 20_000, "compressed")])
        raw = sqlite3.connect(str(db))
        try:
            raw.execute(
                "INSERT INTO proxy_metrics (server, tool, original_chars, compressed_chars, "
                "source, created_at, hook_compression_status) "
                "VALUES ('builtin:claude', 'Bash', 10, 10, 'hook', 0, '\x1b[31mred')"
            )
            raw.commit()
        finally:
            raw.close()

        result = runner.invoke(cli, ["stats"])

        assert result.exit_code == 0, result.output
        assert "\x1b" not in result.output and "31mred" not in result.output
        assert "    status: compressed 1, other 1" in result.output.splitlines()

    def test_all_time_keeps_the_status_of_trimmed_rows(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        _seed_hook_statuses(
            db,
            [
                ("builtin:claude", "Bash", 20_000, "compressed"),
                ("builtin:claude", "Bash", 900, "below_threshold"),
                ("builtin:codex", "Bash", 30_000, "unsupported_host"),
                ("builtin:kimi", "Bash", 7_000, "unsupported_host"),
            ],
            max_history=1,
        )
        raw = sqlite3.connect(str(db))
        try:
            retained = raw.execute("SELECT server FROM proxy_metrics").fetchall()
        finally:
            raw.close()
        assert retained == [("builtin:kimi",)]  # the others live only in the rollups

        data = json.loads(runner.invoke(cli, ["stats", "--json", "--all-time"]).output)

        assert [entry["host"] for entry in data["hook_by_host"]] == ["codex", "claude", "kimi"]
        assert data["hook_status"] == {
            "scope": "all_time",
            "by_host": [
                {
                    "host": "claude",
                    "statuses": [
                        {"status": "below_threshold", "calls": 1, "original_chars": 900},
                        {"status": "compressed", "calls": 1, "original_chars": 20_000},
                    ],
                    "status_not_kept_calls": 0,
                },
                {
                    "host": "codex",
                    "statuses": [
                        {"status": "unsupported_host", "calls": 1, "original_chars": 30_000}
                    ],
                    "status_not_kept_calls": 0,
                },
                {
                    "host": "kimi",
                    "statuses": [
                        {"status": "unsupported_host", "calls": 1, "original_chars": 7_000}
                    ],
                    "status_not_kept_calls": 0,
                },
            ],
        }
        lines = runner.invoke(cli, ["stats", "--all-time"]).output.splitlines()
        assert "      status: below_threshold 1, compressed 1" in lines

    def test_all_time_labels_the_status_line_only_without_the_status_rollup(
        self, runner, tmp_path, monkeypatch
    ):
        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        _seed_hook_statuses(db, [("builtin:claude", "Bash", 20_000, "compressed")])

        lines = runner.invoke(cli, ["stats", "--all-time"]).output.splitlines()
        assert "    status: compressed 1" in lines

        # A DB an earlier build last stamped has the daily rollup only.
        raw = sqlite3.connect(str(db))
        try:
            raw.execute("DROP TRIGGER proxy_metrics_hook_status_follow_daily_delete_v1")
            raw.execute("DROP TRIGGER proxy_metrics_hook_status_rollup_on_delete_v1")
            raw.execute("DROP TABLE proxy_metrics_hook_status_daily")
            raw.commit()
        finally:
            raw.close()
        lines = runner.invoke(cli, ["stats", "--all-time"]).output.splitlines()
        assert "    status: compressed 1  (retained rows)" in lines
        data = json.loads(runner.invoke(cli, ["stats", "--json", "--all-time"]).output)
        assert data["hook_status"]["scope"] == "retained"
        assert data["hook_status"]["by_host"][0]["status_not_kept_calls"] is None

    def test_all_time_counts_calls_trimmed_without_their_status(
        self, runner, tmp_path, monkeypatch
    ):
        """Calls trimmed while only the daily rollup existed keep their place in
        the totals but not their status; the line says how many."""
        set_home(monkeypatch, tmp_path)
        db = tmp_path / ".memtomem" / "proxy_metrics.db"
        _seed_hook_statuses(
            db,
            [
                ("builtin", "Bash", 500, None),
                ("builtin:claude", "Bash", 20_000, "compressed"),
                ("builtin:claude", "Bash", 900, "below_threshold"),
                ("builtin:claude", "Bash", 800, "below_threshold"),
            ],
        )
        raw = sqlite3.connect(str(db))
        try:
            raw.execute("DROP TRIGGER proxy_metrics_hook_status_follow_daily_delete_v1")
            raw.execute("DROP TRIGGER proxy_metrics_hook_status_rollup_on_delete_v1")
            raw.execute("DROP TABLE proxy_metrics_hook_status_daily")
            raw.execute("DELETE FROM proxy_metrics WHERE id <= 2")
            raw.execute("UPDATE metrics_meta SET value = 'older' WHERE key = 'schema_fingerprint'")
            raw.commit()
        finally:
            raw.close()
        MetricsStore(db).initialize()  # this version opens it: installs and seeds

        lines = runner.invoke(cli, ["stats", "--all-time"]).output.splitlines()

        assert "      status: below_threshold 2, status not kept 1" in lines
        # The unrecorded host has nothing but a trimmed call without a status:
        # that is still worth a line, since it explains part of the total.
        unrecorded = next(i for i, line in enumerate(lines) if "(host not recorded)" in line)
        assert lines[unrecorded + 1] == "      status: status not kept 1"
        data = json.loads(runner.invoke(cli, ["stats", "--json", "--all-time"]).output)
        not_kept = {e["host"]: e["status_not_kept_calls"] for e in data["hook_status"]["by_host"]}
        assert not_kept == {"claude": 1, None: 1}

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["--source", "mcp"], None),
            (
                ["--source", "hook", "--tool", "Read"],
                [
                    {
                        "host": "claude",
                        "statuses": [
                            {"status": "ineligible_tool", "calls": 1, "original_chars": 4_000}
                        ],
                        "status_not_kept_calls": None,
                    }
                ],
            ),
        ],
        ids=["mcp", "hook-and-tool"],
    )
    def test_filters(self, runner, tmp_path, monkeypatch, argv, expected):
        set_home(monkeypatch, tmp_path)
        db = _seed_metrics(tmp_path)
        _seed_hook_statuses(db, _MIXED_STATUSES)

        data = json.loads(runner.invoke(cli, ["stats", "--json", *argv]).output)

        if expected is None:
            assert data["hook_status"] is None
        else:
            assert data["hook_status"] == {"scope": "retained", "by_host": expected}

    def test_empty_and_missing_stores(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        missing = json.loads(runner.invoke(cli, ["stats", "--json"]).output)
        assert missing["hook_status"] is None

        db = _seed_metrics(tmp_path)  # MCP rows only, one named like a hook label
        store = MetricsStore(db)
        store.initialize()
        try:
            store.record(
                CallMetrics(
                    server="builtin:claude", tool="Bash", original_chars=10, compressed_chars=10
                )
            )
        finally:
            store.close()
        empty = json.loads(runner.invoke(cli, ["stats", "--json"]).output)
        assert empty["hook_status"] == {"scope": "retained", "by_host": []}


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


class TestStatsProgressiveAllTime:
    """``--all-time`` covers progressive delivery through its rollup (#1328)."""

    def _db(self, tmp_path: Path) -> Path:
        return tmp_path / ".memtomem" / "stm_feedback.db"

    def test_purged_reads_still_count(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = self._db(tmp_path)
        _seed_progressive(db)
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM progressive_reads WHERE key = 'a'")
        conn.commit()
        conn.close()

        retained = runner.invoke(cli, ["stats"]).output
        result = runner.invoke(cli, ["stats", "--all-time"])

        assert result.exit_code == 0, result.output
        assert "  responses: 1  (with follow-up reads: 0; reads recorded: 1)" in retained
        assert "Progressive delivery (all time)" in result.output
        assert "  responses: 2  (with follow-up reads: 1; reads recorded: 3)" in result.output
        assert "  chars: 20,000 produced -> 5,000 delivered (25.0%)" in result.output
        assert "  Window: all time — retained rows plus the rollup, since " in result.output
        assert "Incomplete" not in result.output
        assert "rows older than" not in result.output

    def test_json_names_the_scope_and_the_coverage(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        _seed_progressive(self._db(tmp_path))

        data = json.loads(runner.invoke(cli, ["stats", "--all-time", "--json"]).output)
        default = json.loads(runner.invoke(cli, ["stats", "--json"]).output)

        assert data["progressive_scope"] == "all_time"
        rollup = data["progressive"]["rollup"]
        assert rollup["installed"] is True
        assert rollup["lost_before_install"] == 0
        assert rollup["incomplete_before"] is None
        assert rollup["earliest_created_at"] == data["progressive"]["oldest_at"]
        assert default["progressive_scope"] == "retained"
        assert "rollup" not in default["progressive"]

    def test_a_db_without_the_rollup_is_unavailable(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = self._db(tmp_path)
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE progressive_reads (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, "
            "trace_id TEXT, server TEXT, tool TEXT, offset INTEGER, chars INTEGER, "
            "served_to INTEGER, total_chars INTEGER, created_at REAL, is_initial INTEGER);"
            "INSERT INTO progressive_reads (key, server, tool, offset, chars, served_to, "
            "total_chars, created_at, is_initial) VALUES ('k', 's', 't', 0, 1, 1, 1, 1.0, 1);"
        )
        conn.commit()
        conn.close()

        result = runner.invoke(cli, ["stats", "--all-time"])
        data = json.loads(runner.invoke(cli, ["stats", "--all-time", "--json"]).output)

        assert "All-time totals unavailable: this feedback DB has no usable progressive" in (
            result.output
        )
        assert "No progressive responses recorded yet." not in result.output
        assert data["progressive"]["available"] is False
        assert data["progressive"]["rollup"]["installed"] is False

    def test_a_malformed_marker_is_unavailable_in_text_and_json(
        self, runner, tmp_path, monkeypatch
    ):
        set_home(monkeypatch, tmp_path)
        db = self._db(tmp_path)
        _seed_progressive(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE progressive_reads_meta SET value = 'nan' WHERE key = 'rollup_complete_from'"
        )
        conn.commit()
        conn.close()

        output = runner.invoke(cli, ["stats", "--all-time"]).output
        data = json.loads(runner.invoke(cli, ["stats", "--all-time", "--json"]).output)

        assert "All-time totals unavailable" in output
        assert data["progressive"]["available"] is False
        assert data["progressive"]["rollup"]["installed"] is False

    def test_an_empty_installed_rollup_still_shows_coverage(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        store = ProgressiveReadsStore(self._db(tmp_path), retention_days=0)
        store.initialize()
        store.close()

        output = runner.invoke(cli, ["stats", "--all-time"]).output

        assert "No progressive responses recorded yet." in output
        assert "  Window: all time — retained rows plus the rollup." in output

    def test_a_repaired_rollup_says_the_loss_is_unknown(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = self._db(tmp_path)
        _seed_progressive(db)
        conn = sqlite3.connect(db)
        conn.execute("DROP TRIGGER progressive_reads_rollup_on_delete_v1")
        conn.commit()
        conn.close()
        store = ProgressiveReadsStore(db, retention_days=0)
        store.initialize()
        store.close()

        output = runner.invoke(cli, ["stats", "--all-time"]).output

        assert "  Possibly incomplete before " in output
        assert "an unknown number of reads\n    recorded before then may be missing." in output
        assert "before the\n    rollup was installed" not in output


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
        # The hook split follows the summary it belongs to: unavailable too,
        # though the retained rows are readable (the rollup arrives on the
        # next writing open).
        data = json.loads(runner.invoke(cli, ["stats", "--all-time", "--json"]).output)
        assert (data["hook_by_host"], data["hook_status"]) == (None, None)
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


class TestStatsCache:
    """#1329: cache counts persisted in ``proxy_cache_daily`` are reported."""

    @staticmethod
    def _seed(home: Path, counts: dict[tuple[str, str, str], list[int]]) -> Path:
        db = _seed_metrics(home)
        store = MetricsStore(db)
        store.initialize()
        try:
            store.record_cache_counts(counts)
        finally:
            store.close()
        return db

    def test_text_and_json_report_the_persisted_counts(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        self._seed(
            tmp_path,
            {
                ("2026-11-20", "c7", "query-docs"): [3, 900, 2, 1],
                ("2026-11-21", "c7", "resolve"): [1, 100, 0, 0],
            },
        )

        text = runner.invoke(cli, ["stats"])
        assert text.exit_code == 0, text.output
        block = text.output.split("Proxy / Cache", 1)[1].split("Progressive delivery", 1)[0]
        assert "lookups: 4 hits, 2 misses (1 unstorable)" in block
        assert "hit rate: 80.0% of storable lookups" in block
        assert "served from cache: 1,000 chars" in block
        assert "Coverage: since " in block

        data = json.loads(runner.invoke(cli, ["stats", "--json"]).output)
        cache = data["cache"]
        assert cache["available"] is True
        assert (cache["hits"], cache["hit_chars"], cache["misses"], cache["unstorable"]) == (
            4,
            1000,
            2,
            1,
        )
        assert cache["storable_hit_rate"] == 0.8
        assert isinstance(cache["installed_at"], float)

        filtered = json.loads(runner.invoke(cli, ["stats", "--json", "--tool", "resolve"]).output)
        assert (filtered["cache"]["hits"], filtered["cache"]["storable_hit_rate"]) == (1, 1.0)

    def test_huge_buckets_report_an_exact_count_in_strict_json(self, runner, tmp_path, monkeypatch):
        """Two outside-written ``1e308`` buckets: a float total would be inf.
        The reader adds whole numbers as ints, so the block renders the exact
        count and --json stays strict JSON with a finite rate."""
        set_home(monkeypatch, tmp_path)
        db = self._seed(tmp_path, {("2026-11-21", "s", "t"): [1, 1, 0, 0]})
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO proxy_cache_daily VALUES ('2026-11-21', 's', 'a', 1e308, 0, 0, 0), "
            "('2026-11-21', 's', 'b', 1e308, 0, 0, 0)"
        )
        conn.commit()
        conn.close()

        text = runner.invoke(cli, ["stats"])
        assert text.exit_code == 0, text.output
        block = text.output.split("Proxy / Cache", 1)[1].split("Progressive delivery", 1)[0]
        hits = 2 * int(1e308) + 1
        assert "unreadable" not in block
        assert f"lookups: {hits:,} hits, 0 misses (0 unstorable)" in block
        assert "hit rate: 100.0% of storable lookups" in block

        def _reject(token: str) -> None:
            raise AssertionError(f"non-JSON constant {token}")

        data = json.loads(runner.invoke(cli, ["stats", "--json"]).output, parse_constant=_reject)
        assert data["cache"]["available"] is True
        assert data["cache"]["error"] is None
        assert data["cache"]["hits"] == hits
        assert data["cache"]["storable_hit_rate"] == 1.0

    def test_zero_counts_still_show_the_coverage_start(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        self._seed(tmp_path, {("2026-11-21", "s", "t"): [1, 1, 0, 0]})
        result = runner.invoke(cli, ["stats", "--tool", "no-such-tool"])
        assert result.exit_code == 0, result.output
        block = result.output.split("Proxy / Cache", 1)[1].split("Progressive delivery", 1)[0]
        assert "No cache lookups recorded yet." in block
        assert "Coverage: since " in block
        assert "an unknown start" not in block

    def test_unstorable_only_tool_is_shown(self, runner, tmp_path, monkeypatch):
        """An unstorable count with no miss in the same bucket is a supported
        state (eligibility can change mid-call); it must not read as empty."""
        set_home(monkeypatch, tmp_path)
        self._seed(
            tmp_path,
            {
                ("2026-11-21", "s", "flip"): [0, 0, 0, 1],
                ("2026-11-21", "s", "other"): [2, 2, 1, 0],
            },
        )
        result = runner.invoke(cli, ["stats", "--tool", "flip"])
        assert result.exit_code == 0, result.output
        block = result.output.split("Proxy / Cache", 1)[1].split("Progressive delivery", 1)[0]
        assert "No cache lookups recorded yet." not in block
        assert "lookups: 0 hits, 0 misses (1 unstorable)" in block
        assert "Coverage: since " in block

    def test_no_storable_lookups_renders_na(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        self._seed(tmp_path, {("2026-11-21", "s", "t"): [0, 0, 2, 3]})
        result = runner.invoke(cli, ["stats"])
        assert result.exit_code == 0, result.output
        assert "hit rate: n/a (no storable lookups) of storable lookups" in result.output
        data = json.loads(runner.invoke(cli, ["stats", "--json"]).output)
        assert data["cache"]["storable_hit_rate"] is None

    def test_source_hook_omits_the_cache_block(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        self._seed(tmp_path, {("2026-11-21", "s", "t"): [1, 1, 0, 0]})
        result = runner.invoke(cli, ["stats", "--source", "hook"])
        assert result.exit_code == 0, result.output
        assert "Proxy / Cache" not in result.output
        data = json.loads(runner.invoke(cli, ["stats", "--json", "--source", "hook"]).output)
        assert data["cache"] is None

    def test_pre_cache_db_reports_nothing_recorded_and_stays_untouched(
        self, runner, tmp_path, monkeypatch
    ):
        set_home(monkeypatch, tmp_path)
        db = _seed_metrics(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE proxy_cache_daily")
        conn.commit()
        conn.close()
        result = runner.invoke(cli, ["stats"])
        assert result.exit_code == 0, result.output
        assert "No cache lookups recorded yet." in result.output
        conn = sqlite3.connect(db)
        try:
            assert (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'proxy_cache_daily'"
                ).fetchone()
                is None
            )
        finally:
            conn.close()

    def test_coverage_start_outside_pythons_date_range_renders(self, runner, tmp_path, monkeypatch):
        set_home(monkeypatch, tmp_path)
        db = self._seed(tmp_path, {("2026-11-21", "s", "t"): [1, 1, 0, 0]})
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE metrics_meta SET value = '1e300' WHERE key = 'cache_rollup_installed_at'"
        )
        conn.commit()
        conn.close()
        result = runner.invoke(cli, ["stats"])
        assert result.exit_code == 0, result.output
        assert "Coverage: since an unknown start" in result.output
