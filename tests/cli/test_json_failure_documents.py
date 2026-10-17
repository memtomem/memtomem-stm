"""Every operational failure of ``tune --json`` and ``gateway status/explain --json``
emits the one failure document (#1138).

#1099 covered a config file the command cannot read or parse. These are the
branches after that read: a missing config, the typed validation ``tune`` runs
with the env overlay, and ``gateway explain``'s bundle checks. Each used to
print its human line to stderr and exit with an empty stdout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.proxy import cli

SCHEMA_INVALID = json.dumps({"default_max_result_chars": -1})
"""Parses and has the right shape, so ``_load`` accepts it; fails typed validation."""


def _failure_document(result, action: str, error: str) -> dict:
    assert result.exit_code == 1, result.output
    document = json.loads(result.stdout)
    assert document["action"] == action
    assert document["ok"] is False
    assert document["error"] == error
    assert "Error:" in result.stderr
    return document


def _bundle_config(tmp_path: Path, *, query_profile: str = "strict") -> Path:
    config = tmp_path / "proxy.json"
    config.write_text(
        json.dumps(
            {
                "enabled": True,
                "upstream_servers": {},
                "exposure": {"profile": "strict"},
                "toolgraph": {
                    "enabled": True,
                    "source": "bundle",
                    "bundle_path": str(tmp_path / "bundle.json"),
                    "query_profile": query_profile,
                    "agent_id": "stm-proxy",
                },
            }
        )
    )
    return config


def _write_bundle(tmp_path: Path) -> None:
    (tmp_path / "bundle.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "toolgraph.policy-bundle",
                "created_at": "2026-07-14T00:00:00+00:00",
                "graph_state": {"instance_id": "graph-1", "generation": 7},
                "agent": "stm-proxy",
                "agent_found": True,
                "profile": "strict",
                "governance_digest": "a" * 64,
                "catalog_digest": "b" * 64,
                "tools": [
                    {
                        "tool_key": "graph-srv::read",
                        "tool_contract_digest": "c" * 64,
                        "decision": "eligible",
                        "risk_score": 0.8,
                    }
                ],
            }
        )
    )


# ---------------------------------------------------------------------------
# tune
# ---------------------------------------------------------------------------


def test_tune_json_missing_config_emits_config_not_found(tmp_path):
    path = tmp_path / "missing.json"

    result = CliRunner().invoke(cli, ["tune", "--json", "--config", str(path)])

    document = _failure_document(result, "tune", "config_not_found")
    assert document["path"] == str(path.resolve())
    assert document["message"] == f"config not found at {path.resolve()}"
    assert "mms init" in result.stderr


def test_tune_json_typed_validation_failure_emits_config_invalid(tmp_path):
    path = tmp_path / "proxy.json"
    path.write_text(SCHEMA_INVALID)

    result = CliRunner().invoke(cli, ["tune", "--json", "--config", str(path)])

    document = _failure_document(result, "tune", "config_invalid")
    assert document["path"] == str(path.resolve())
    # The loader's value-free summary, so a script sees what failed.
    assert "default_max_result_chars (greater_than)" in document["message"]
    assert "mms config validate" in document["message"]


def test_tune_json_typed_read_failure_emits_config_unreadable(tmp_path, monkeypatch):
    """The typed load is ``tune``'s third read; an I/O failure there is not a schema failure."""
    path = tmp_path / "proxy.json"
    path.write_text("{}")
    real_read_text = Path.read_text
    reads: list[Path] = []

    def fail_third_read(self, *args, **kwargs):
        if self == path.resolve():
            reads.append(self)
            if len(reads) == 3:
                raise PermissionError(13, "Permission denied", "/cnry-dir/cnry-file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_third_read)
    result = CliRunner().invoke(cli, ["tune", "--json", "--config", str(path)])

    assert len(reads) == 3
    document = _failure_document(result, "tune", "config_unreadable")
    assert document["message"] == "config could not be read (PermissionError)"
    assert "cnry" not in result.stdout
    assert "cnry" not in result.stderr


def test_tune_json_config_vanishing_before_the_typed_read_is_not_found(tmp_path, monkeypatch):
    """A file deleted between the existence check and the typed load keeps its own code."""
    path = tmp_path / "proxy.json"
    path.write_text("{}")
    real_read_text = Path.read_text
    reads: list[Path] = []

    def unlink_after_second_read(self, *args, **kwargs):
        text = real_read_text(self, *args, **kwargs)
        if self == path.resolve():
            reads.append(self)
            if len(reads) == 2:
                self.unlink()
        return text

    monkeypatch.setattr(Path, "read_text", unlink_after_second_read)
    result = CliRunner().invoke(cli, ["tune", "--json", "--config", str(path)])

    assert len(reads) == 2
    document = _failure_document(result, "tune", "config_not_found")
    assert document["path"] == str(path.resolve())


def test_tune_json_without_metrics_emits_the_empty_preview(tmp_path):
    """Not a failure, but stdout must still be exactly one JSON document."""
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "upstream_servers": {},
                "metrics": {"db_path": str(tmp_path / "absent.db")},
            }
        )
    )

    result = CliRunner().invoke(cli, ["tune", "--json", "--config", str(path)])

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["config_path"] == str(path.resolve())
    assert document["changes"] == []
    assert document["skipped"] == []
    assert "No proxy metrics recorded yet" in result.stderr
    assert "No proxy metrics" not in result.stdout


def test_tune_text_run_without_metrics_keeps_its_stdout_line(tmp_path):
    """Preservation control for the text mode of the branch above."""
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps({"upstream_servers": {}, "metrics": {"db_path": str(tmp_path / "absent.db")}})
    )

    result = CliRunner().invoke(cli, ["tune", "--config", str(path)])

    assert result.exit_code == 0, result.output
    assert "No proxy metrics recorded yet" in result.stdout


# ---------------------------------------------------------------------------
# gateway status / explain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "argv"),
    [
        ("gateway-status", ["gateway", "status"]),
        ("gateway-explain", ["gateway", "explain", "graph-srv::read"]),
    ],
    ids=["status", "explain"],
)
def test_gateway_json_typed_validation_failure_emits_config_invalid(tmp_path, action, argv):
    path = tmp_path / "proxy.json"
    path.write_text(SCHEMA_INVALID)

    result = CliRunner().invoke(cli, [*argv, "--json", "--config", str(path)])

    document = _failure_document(result, action, "config_invalid")
    assert document["path"] == str(path.resolve())
    assert document["message"] == "invalid proxy config: default_max_result_chars (greater_than)"


def test_gateway_explain_json_source_not_bundle(tmp_path):
    path = tmp_path / "proxy.json"
    path.write_text(json.dumps({"upstream_servers": {}}))

    result = CliRunner().invoke(
        cli, ["gateway", "explain", "graph-srv::read", "--json", "--config", str(path)]
    )

    document = _failure_document(result, "gateway-explain", "source_not_bundle")
    assert document["message"] == "toolgraph.source must be 'bundle' for gateway explain"


def test_gateway_explain_json_profile_mismatch(tmp_path):
    path = _bundle_config(tmp_path, query_profile="review")

    result = CliRunner().invoke(
        cli, ["gateway", "explain", "graph-srv::read", "--json", "--config", str(path)]
    )

    _failure_document(result, "gateway-explain", "profile_mismatch")


def test_gateway_explain_json_missing_bundle_is_unreadable(tmp_path):
    path = _bundle_config(tmp_path)

    result = CliRunner().invoke(
        cli, ["gateway", "explain", "graph-srv::read", "--json", "--config", str(path)]
    )

    document = _failure_document(result, "gateway-explain", "bundle_unreadable")
    assert document["bundle_path"] == str(tmp_path / "bundle.json")
    assert document["message"].startswith("cannot read policy bundle ")


def test_gateway_explain_json_malformed_bundle_is_invalid(tmp_path):
    path = _bundle_config(tmp_path)
    (tmp_path / "bundle.json").write_text("{")

    result = CliRunner().invoke(
        cli, ["gateway", "explain", "graph-srv::read", "--json", "--config", str(path)]
    )

    document = _failure_document(result, "gateway-explain", "bundle_invalid")
    assert document["bundle_path"] == str(tmp_path / "bundle.json")


def test_gateway_explain_json_unknown_tool_key(tmp_path):
    path = _bundle_config(tmp_path)
    _write_bundle(tmp_path)

    result = CliRunner().invoke(
        cli, ["gateway", "explain", "graph-srv::other", "--json", "--config", str(path)]
    )

    document = _failure_document(result, "gateway-explain", "unknown_tool")
    assert document["tool_key"] == "graph-srv::other"
    # Positive control: the same bundle answers for the key it does hold.
    ok = CliRunner().invoke(
        cli, ["gateway", "explain", "graph-srv::read", "--json", "--config", str(path)]
    )
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.stdout)["decision"] == "eligible"


@pytest.mark.parametrize(
    "argv",
    [
        ["tune"],
        ["gateway", "status"],
        ["gateway", "explain", "graph-srv::read"],
    ],
    ids=["tune", "gateway-status", "gateway-explain"],
)
def test_text_runs_keep_their_plain_exit(tmp_path, argv):
    """Preservation control: without ``--json`` stdout stays empty and stderr has the line."""
    path = tmp_path / "proxy.json"
    path.write_text(SCHEMA_INVALID)

    result = CliRunner().invoke(cli, [*argv, "--config", str(path)])

    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "Error:" in result.stderr
    if argv == ["tune"]:
        # The sentence `tune` printed before #1138, unchanged; the summary is
        # on the loader's own log line, not in it.
        assert (
            "Error: config fails validation — run `mms config validate` and fix it "
            "before tuning." in result.stderr
        )
    else:
        assert "default_max_result_chars" in result.stderr


# ---------------------------------------------------------------------------
# #1176: malformed bundles and a corrupt store ended in a traceback
# ---------------------------------------------------------------------------


def _write_bundle_with_decision(tmp_path: Path, decision: object) -> None:
    _write_bundle(tmp_path)
    bundle = tmp_path / "bundle.json"
    doc = json.loads(bundle.read_text())
    doc["tools"][0]["decision"] = decision
    bundle.write_text(json.dumps(doc))


def _write_bundle_with_a_huge_integer(tmp_path: Path) -> None:
    _write_bundle(tmp_path)
    bundle = tmp_path / "bundle.json"
    text = bundle.read_text().replace('"generation": 7', '"generation": ' + "1" * 5000)
    assert "1" * 5000 in text
    bundle.write_text(text)


def test_gateway_status_json_reports_a_list_decision_as_invalid(tmp_path):
    path = _bundle_config(tmp_path)
    _write_bundle_with_decision(tmp_path, [])

    result = CliRunner().invoke(cli, ["gateway", "status", "--json", "--config", str(path)])

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["valid"] is False
    assert document["error"] == "tools[0].decision is invalid"


def test_gateway_status_text_reports_a_list_decision_as_invalid(tmp_path):
    path = _bundle_config(tmp_path)
    _write_bundle_with_decision(tmp_path, [])

    result = CliRunner().invoke(cli, ["gateway", "status", "--config", str(path)])

    assert result.exit_code == 0, result.output
    assert "INVALID" in result.stdout
    assert "tools[0].decision is invalid" in result.stdout


def test_gateway_explain_json_reports_a_list_decision_as_bundle_invalid(tmp_path):
    path = _bundle_config(tmp_path)
    _write_bundle_with_decision(tmp_path, [])

    result = CliRunner().invoke(
        cli, ["gateway", "explain", "graph-srv::read", "--json", "--config", str(path)]
    )

    document = _failure_document(result, "gateway-explain", "bundle_invalid")
    assert document["message"] == "tools[0].decision is invalid"


def test_gateway_explain_text_reports_a_list_decision_cleanly(tmp_path):
    path = _bundle_config(tmp_path)
    _write_bundle_with_decision(tmp_path, [])

    result = CliRunner().invoke(
        cli, ["gateway", "explain", "graph-srv::read", "--config", str(path)]
    )

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert result.stdout == ""
    assert "tools[0].decision is invalid" in result.stderr


def test_gateway_status_json_reports_a_huge_integer_as_invalid(tmp_path):
    """``json.loads`` raises a plain ``ValueError`` past the int digit limit."""
    path = _bundle_config(tmp_path)
    _write_bundle_with_a_huge_integer(tmp_path)

    result = CliRunner().invoke(cli, ["gateway", "status", "--json", "--config", str(path)])

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["valid"] is False
    assert document["error"].startswith("bundle is not valid UTF-8 JSON: ")


def test_gateway_explain_json_reports_a_huge_integer_as_bundle_invalid(tmp_path):
    path = _bundle_config(tmp_path)
    _write_bundle_with_a_huge_integer(tmp_path)

    result = CliRunner().invoke(
        cli, ["gateway", "explain", "graph-srv::read", "--json", "--config", str(path)]
    )

    document = _failure_document(result, "gateway-explain", "bundle_invalid")
    assert document["message"].startswith("bundle is not valid UTF-8 JSON: ")


def _tune_config_with_a_non_sqlite_metrics_file(tmp_path: Path) -> tuple[Path, Path]:
    metrics = tmp_path / "cnry-metrics.db"
    metrics.write_text("not sqlite")
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps({"enabled": True, "upstream_servers": {}, "metrics": {"db_path": str(metrics)}})
    )
    return path, metrics


def test_tune_json_reports_a_non_sqlite_metrics_store(tmp_path):
    path, metrics = _tune_config_with_a_non_sqlite_metrics_file(tmp_path)

    result = CliRunner().invoke(cli, ["tune", "--json", "--config", str(path)])

    document = _failure_document(result, "tune", "store_invalid")
    assert document["path"] == str(path.resolve())
    assert document["stores"] == [str(metrics)]
    assert document["message"] == (f"cannot open the metrics store ({metrics}): DatabaseError")
    # The sqlite message ("file is not a database") never reaches the document.
    assert "not a database" not in result.stdout


def test_tune_text_reports_a_non_sqlite_metrics_store(tmp_path):
    path, metrics = _tune_config_with_a_non_sqlite_metrics_file(tmp_path)

    result = CliRunner().invoke(cli, ["tune", "--config", str(path)])

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit)
    assert result.stdout == ""
    assert f"Error: cannot open the metrics store ({metrics}): DatabaseError" in result.stderr


def test_tune_reports_a_non_sqlite_feedback_store_by_name(tmp_path):
    """The step and the file named are the ones that actually failed."""
    metrics = tmp_path / "metrics.db"
    feedback = tmp_path / "feedback.db"
    feedback.write_text("not sqlite")
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "upstream_servers": {},
                "metrics": {"db_path": str(metrics)},
                "compression_feedback": {"db_path": str(feedback)},
            }
        )
    )
    from memtomem_stm.proxy.metrics_store import MetricsStore

    store = MetricsStore(metrics)
    store.initialize()
    store.close()

    result = CliRunner().invoke(cli, ["tune", "--json", "--config", str(path)])

    document = _failure_document(result, "tune", "store_invalid")
    assert document["stores"] == [str(feedback)]
    assert document["message"] == f"cannot open the feedback store ({feedback}): DatabaseError"


def test_tune_text_escapes_the_store_path_and_the_document_keeps_it_raw(tmp_path):
    hostile = tmp_path / "m\u202eetrics.db"
    hostile.write_text("not sqlite")
    path = tmp_path / "proxy.json"
    path.write_text(json.dumps({"upstream_servers": {}, "metrics": {"db_path": str(hostile)}}))

    text = CliRunner().invoke(cli, ["tune", "--config", str(path)])
    as_json = CliRunner().invoke(cli, ["tune", "--json", "--config", str(path)])

    assert text.exit_code == 1, text.output
    assert "\u202e" not in text.stderr
    assert "\\u202E" in text.stderr
    document = _failure_document(as_json, "tune", "store_invalid")
    assert document["stores"] == [str(hostile)]
    assert "\u202e" in document["message"]


@pytest.mark.parametrize(
    ("with_feedback", "step"),
    [(False, "analyze the metrics store"), (True, "analyze the stores")],
    ids=["metrics-only", "metrics-and-feedback"],
)
def test_tune_reports_an_analysis_failure_with_every_store_it_was_reading(
    tmp_path, monkeypatch, with_feedback, step
):
    """Past ``initialize``, a read failure cannot be pinned on one file, so the
    message names the step and lists each store the analysis touches."""
    import sqlite3

    from memtomem_stm.proxy.compression_feedback_store import CompressionFeedbackStore
    from memtomem_stm.proxy.metrics_store import MetricsStore
    from memtomem_stm.proxy.tuner import CompressionTuner

    metrics = tmp_path / "metrics.db"
    store = MetricsStore(metrics)
    store.initialize()
    store.close()
    config = {"enabled": True, "upstream_servers": {}, "metrics": {"db_path": str(metrics)}}
    expected_stores = [str(metrics)]
    if with_feedback:
        feedback = tmp_path / "feedback.db"
        fb = CompressionFeedbackStore(feedback, retention_days=0)
        fb.initialize()
        fb.close()
        config["compression_feedback"] = {"db_path": str(feedback)}
        expected_stores.append(str(feedback))
    path = tmp_path / "proxy.json"
    path.write_text(json.dumps(config))

    def broken_analyze(self, *args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(CompressionTuner, "analyze", broken_analyze)

    result = CliRunner().invoke(cli, ["tune", "--json", "--config", str(path)])

    document = _failure_document(result, "tune", "store_invalid")
    assert document["stores"] == expected_stores
    assert document["message"] == f"cannot {step} ({', '.join(expected_stores)}): DatabaseError"
    assert "malformed" not in result.stdout
