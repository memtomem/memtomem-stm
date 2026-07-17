"""Read-only ``mms selection`` commands for offline evaluation (#468)."""

from __future__ import annotations

from pathlib import Path

import click

from memtomem_stm.cli._defaults import DEFAULT_PROXY_CONFIG
from memtomem_stm.proxy.config import ProxyConfig, collect_proxy_env_overrides
from memtomem_stm.proxy.selection_eval import (
    SelectionEvaluationError,
    evaluate_selection,
    format_selection_report,
)
from memtomem_stm.utils.fileio import atomic_write_text

_DEFAULT_CONFIG = DEFAULT_PROXY_CONFIG


@click.group(name="selection")
def selection_group() -> None:
    """Replay and evaluate tool-selection telemetry."""


@selection_group.command(name="replay")
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
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
    config_path: str,
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
