"""``mms selection`` commands: offline evaluation (#468) and labelling (#469).

``replay`` is read-only. ``feedback`` is the group's one writer, and it writes
only to the selection telemetry log — appending a ``feedback`` record that
joins an existing ``selection`` by id. It never touches the proxy config, the
metrics stores, or any source MCP-client config.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NoReturn

import click

from memtomem_stm.cli._defaults import DEFAULT_PROXY_CONFIG, resolve_cli_config_path
from memtomem_stm.proxy.config import ProxyConfig, collect_proxy_env_overrides
from memtomem_stm.proxy.selection_eval import (
    SelectionEvaluationError,
    evaluate_selection,
    format_selection_report,
)
from memtomem_stm.proxy.selection_log import SelectionTelemetryLog, find_selection
from memtomem_stm.utils import json_out
from memtomem_stm.utils.fileio import atomic_write_text

_DEFAULT_CONFIG = DEFAULT_PROXY_CONFIG


@click.group(name="selection")
def selection_group() -> None:
    """Replay, evaluate, and label tool-selection telemetry."""


@selection_group.command(name="replay")
@click.option("--config", "config_path", default=None, show_default=str(_DEFAULT_CONFIG))
@click.option(
    "--log",
    "log_path",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Selection JSONL path; overrides selection_telemetry.path.",
)
@click.option(
    "--dataset",
    "dataset_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    help="Labelled evaluation corpus; defaults to the packaged v1 corpus.",
)
@click.option(
    "--active-only",
    is_flag=True,
    help="Read only the active log, excluding numeric rotated backups.",
)
@click.option(
    "--no-telemetry",
    is_flag=True,
    help="Evaluate the fixed corpus only; do not read production telemetry.",
)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    help="Atomically write report.json and summary.md to this directory.",
)
@click.option("--json", "as_json", is_flag=True, help="Output stable JSON for scripting.")
def replay_command(
    config_path: str | None,
    log_path: Path | None,
    dataset_path: Path | None,
    active_only: bool,
    no_telemetry: bool,
    output_dir: Path | None,
    as_json: bool,
) -> None:
    """Replay selection logs and tune deterministic risk-penalty weights.

    Production logs supply observational quality metrics only. Counterfactual
    ranking runs against the sanitized labelled corpus, never raw prompts.
    Recommendations are previews and never modify the proxy config.
    """
    config_path = resolve_cli_config_path(config_path).path
    if no_telemetry and log_path is not None:
        raise click.UsageError("--no-telemetry cannot be combined with --log")

    cfg_path = Path(config_path)
    cfg = ProxyConfig.load_from_file(
        cfg_path,
        env_overrides=collect_proxy_env_overrides(),
        missing_ok=True,
    ) or ProxyConfig(config_path=cfg_path)
    telemetry_path: Path | None
    if no_telemetry:
        telemetry_path = None
    elif log_path is not None:
        telemetry_path = log_path.expanduser()
        if not telemetry_path.exists():
            raise click.ClickException(f"telemetry log not found: {telemetry_path}")
    else:
        telemetry_path = cfg.selection_telemetry.path.expanduser()

    try:
        report = evaluate_selection(
            telemetry_path=telemetry_path,
            dataset_path=dataset_path,
            include_rotated=not active_only,
            baseline_review_penalty=cfg.exposure.review_risk_penalty,
            baseline_graph_scale=cfg.toolgraph.risk_penalty_scale,
        )
    except SelectionEvaluationError as exc:
        raise click.ClickException(str(exc)) from exc

    summary = format_selection_report(report)
    if output_dir is not None:
        target = output_dir.expanduser().resolve()
        atomic_write_text(target / "report.json", report.to_json(), mode=0o600)
        atomic_write_text(target / "summary.md", summary, mode=0o600)

    if as_json:
        click.echo(report.to_json(), nl=False)
    else:
        click.echo(summary, nl=False)
    if report.data["status"] == "invalid":
        raise click.exceptions.Exit(1)


# ``feedback`` never rotates. Rotation renames files, and this command runs in a
# process that does not own the log — a concurrent rename from the proxy and
# from here can interleave into a lost segment. A ceiling no log can reach makes
# ``_rotate_if_needed_locked`` a no-op here; the proxy still rotates on its own
# next write, and one appended label cannot meaningfully grow the file anyway.
_NEVER_ROTATE = sys.maxsize


@selection_group.command(name="feedback")
@click.option("--config", "config_path", default=None, show_default=str(_DEFAULT_CONFIG))
@click.option(
    "--log",
    "log_path",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Selection JSONL path; overrides selection_telemetry.path.",
)
@click.option("--selection-id", "selection_id", help="Label this exact selection.")
@click.option(
    "--last",
    is_flag=True,
    help="Label the most recent selection (optionally filtered by --server/--tool).",
)
@click.option("--server", help="With --last: only consider selections from this upstream.")
@click.option("--tool", help="With --last: only consider this prefixed tool name.")
@click.option(
    "--user-corrected/--no-user-corrected",
    "user_corrected",
    default=None,
    help="Record that the selection was (or was not) corrected by the user.",
)
@click.option(
    "--operator-override/--no-operator-override",
    "operator_override",
    default=None,
    help="Record that an operator overrode (or accepted) the selection.",
)
@click.option(
    "--active-only",
    is_flag=True,
    help="Resolve against the active log only, excluding numeric rotated backups.",
)
@click.option("--json", "as_json", is_flag=True, help="Output stable JSON for scripting.")
def feedback_command(
    config_path: str | None,
    log_path: Path | None,
    selection_id: str | None,
    last: bool,
    server: str | None,
    tool: str | None,
    user_corrected: bool | None,
    operator_override: bool | None,
    active_only: bool,
    as_json: bool,
) -> None:
    """Attach a label to one recorded tool selection.

    Appends a feedback record that joins an existing selection by id. Existing
    records are never edited, and this command never rotates the log.

    Pass exactly one selector. --selection-id names the row. --last resolves
    the most recent selection in append order, narrowed by --server / --tool,
    and prints which selection it resolved to before writing.

    Both labels are three-valued: --no-user-corrected records that the
    selection was RIGHT, which offline evaluation needs as much as the
    negative case, while omitting the flag records nothing for that field. At
    least one label is required.
    """
    # Docstring note (not for --help): this is the selection log's first and
    # only producer of the ``feedback`` event, schema-pinned since #467 with
    # nothing to write it. Because it is operator-driven, the stream carries
    # judgement at operator volume — the client model never sees a
    # ``selection_id``, so nothing on the request path can emit one. ADR 0001
    # states that, and ``tests/test_docs_sync.py`` pins the emitter set.
    if bool(selection_id) == last:
        raise click.UsageError("pass exactly one of --selection-id or --last")
    if (server or tool) and not last:
        raise click.UsageError("--server/--tool only apply to --last")
    if user_corrected is None and operator_override is None:
        raise click.UsageError(
            "nothing to record: pass --user-corrected/--no-user-corrected "
            "and/or --operator-override/--no-operator-override"
        )

    config_path = resolve_cli_config_path(config_path).path
    cfg_path = Path(config_path)
    cfg = ProxyConfig.load_from_file(
        cfg_path,
        env_overrides=collect_proxy_env_overrides(),
        missing_ok=True,
    ) or ProxyConfig(config_path=cfg_path)
    telemetry_path = (
        log_path.expanduser() if log_path is not None else cfg.selection_telemetry.path.expanduser()
    )
    if not telemetry_path.exists():
        _feedback_failure(as_json, "no_log", f"selection log not found: {telemetry_path}")

    record = find_selection(
        telemetry_path,
        selection_id=selection_id,
        server=server,
        tool=tool,
        include_rotated=not active_only,
    )
    # Resolve before writing: an id that matches nothing would append a label
    # that joins to no selection — silently useless to every reader, and
    # indistinguishable from a selection whose own record was dropped by the
    # redaction screen.
    if record is None:
        if selection_id is not None:
            _feedback_failure(as_json, "not_found", f"no selection record with id {selection_id}")
        scope = ", ".join(
            part
            for part in (f"server={server}" if server else "", f"tool={tool}" if tool else "")
            if part
        )
        _feedback_failure(
            as_json,
            "no_match",
            f"no selection record matches{' ' + scope if scope else ''}",
        )
    resolved_id = record.get("selection_id")
    if not isinstance(resolved_id, str) or not resolved_id:
        # Only reachable on a hand-edited log; its own code so an operator is
        # not told "not found" about a record that was in fact found.
        _feedback_failure(
            as_json, "malformed_record", "matched selection record carries no selection_id"
        )

    # Carry the selection's own trace_id onto the label so the feedback record
    # joins the metrics store the same way its selection does.
    trace_id = record.get("trace_id")
    # The label inherits the labelled selection's cohort stamp — see
    # ``log_feedback``. A stamp this process invented would be a claim about a
    # ranker that never ran for this call.
    ranker_version = record.get("ranker_version")
    log = SelectionTelemetryLog(telemetry_path, max_bytes=_NEVER_ROTATE)
    log.log_feedback(
        selection_id=resolved_id,
        trace_id=trace_id if isinstance(trace_id, str) else None,
        user_corrected=user_corrected,
        operator_override=operator_override,
        ranker_version=ranker_version if isinstance(ranker_version, str) else None,
    )
    result = {
        "action": "selection-feedback",
        "ok": True,
        "selection_id": resolved_id,
        "trace_id": trace_id if isinstance(trace_id, str) else None,
        "server": record.get("server"),
        "selected_tool": record.get("selected_tool"),
        "user_corrected": user_corrected,
        "operator_override": operator_override,
        "log": str(telemetry_path),
    }
    if as_json:
        click.echo(json_out.dumps(result, sort_keys=True, ensure_ascii=False))
        return
    click.echo(f"Labelled selection {resolved_id} ({result['server']} / {result['selected_tool']})")
    for field in ("user_corrected", "operator_override"):
        if result[field] is not None:
            click.echo(f"  {field}: {str(result[field]).lower()}")


def _feedback_failure(as_json: bool, code: str, message: str) -> NoReturn:
    """Exit 1 with a stable error code, in the caller's chosen format."""
    if as_json:
        click.echo(
            json_out.dumps(
                {"action": "selection-feedback", "ok": False, "error": code, "message": message},
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        raise click.exceptions.Exit(1)
    raise click.ClickException(message)
