"""``mms selection`` commands: offline evaluation (#468) and labelling (#469).

``replay`` is read-only. ``feedback`` is the group's one writer, and it writes
only to the selection telemetry log — appending a ``feedback`` record that
joins an existing ``selection`` by id. It never touches the proxy config, the
metrics stores, or any source MCP-client config.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from collections.abc import Iterator

import click

from memtomem_stm.cli._defaults import DEFAULT_PROXY_CONFIG, resolve_cli_config_path
from memtomem_stm.proxy.config import ProxyConfig, collect_proxy_env_overrides
from memtomem_stm.proxy.selection_eval import (
    SelectionEvaluationError,
    evaluate_selection,
    format_selection_report,
)
from memtomem_stm.cli._display import _disp
from memtomem_stm.proxy.selection_log import (
    APPEND_UNCONFIRMED,
    APPEND_WRITTEN,
    SelectionTelemetryLog,
    discover_log_files,
    find_selection,
    rotation_lock,
    selection_defect,
)
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

# The writer's hold on the rotation lock is a handful of renames, so a brief
# retry outlasts it; failing after that reports a busy log rather than blocking
# an operator behind a stuck holder.
_LOCK_ATTEMPTS = 20


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
# Opposing labels are separate flags, not a Click ``--x/--no-x`` pair: a pair
# silently lets the LAST of two contradictory flags win, which inverts a
# training label instead of refusing an incoherent command.
@click.option("--user-corrected", is_flag=True, help="The user corrected this selection.")
@click.option("--no-user-corrected", is_flag=True, help="The user did NOT correct this selection.")
@click.option("--operator-override", is_flag=True, help="An operator overrode this selection.")
@click.option(
    "--no-operator-override", is_flag=True, help="An operator did NOT override this selection."
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Confirm the --last target without prompting (required off a TTY).",
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
    user_corrected: bool,
    no_user_corrected: bool,
    operator_override: bool,
    no_operator_override: bool,
    assume_yes: bool,
    active_only: bool,
    as_json: bool,
) -> None:
    """Attach a label to one recorded tool selection.

    Appends a feedback record that joins an existing selection by id. Existing
    records are never edited, and this command never rotates the log.

    Pass exactly one selector. --selection-id names the row and never prompts.
    --last resolves the most recent selection in append order, narrowed by
    --server / --tool, prints which selection it resolved to, and asks for
    confirmation before writing. That echo is part of the human surface: under
    --json the target is reported only in the result document, after the
    write, so a scripted --last must pass --yes and check what came back. Because that check is all that stands behind
    an inferred target, --last off a terminal (a pipe, CI, or --json) is
    refused without --yes rather than prompting where nobody can answer.

    Both labels are three-valued: --no-user-corrected records that the
    selection was RIGHT, which offline evaluation needs as much as the
    negative case, while omitting both forms records nothing for that field.
    At least one label is required, and opposing forms of the same label are
    a usage error rather than last-flag-wins.
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
    corrected = _tri_state(user_corrected, no_user_corrected, "user-corrected")
    overridden = _tri_state(operator_override, no_operator_override, "operator-override")
    if corrected is None and overridden is None:
        raise click.UsageError(
            "nothing to record: pass --user-corrected/--no-user-corrected "
            "and/or --operator-override/--no-operator-override"
        )

    if log_path is not None:
        # An explicit path decides the target outright, so the config is not
        # consulted — and cannot fail the command for a file it would not have
        # read anything from.
        telemetry_path = log_path.expanduser()
    else:
        cfg_path = Path(resolve_cli_config_path(config_path).path)
        loaded = ProxyConfig.load_from_file_with_status(
            cfg_path,
            env_overrides=collect_proxy_env_overrides(),
            missing_ok=True,
        )
        if loaded.error is not None:
            # Falling back to defaults here would silently label a DIFFERENT
            # log than the operator configured — a writing command must not
            # guess which file it is annotating.
            _feedback_failure(
                as_json,
                "config_invalid",
                f"cannot read {cfg_path}: {loaded.error}",
            )
        if loaded.env_error is not None:
            # Same hazard by the other route: no file, and the environment the
            # operator DID set failed to validate, so the defaults rebuild
            # names a log nobody chose.
            _feedback_failure(
                as_json,
                "config_invalid",
                f"the MEMTOMEM_STM_PROXY environment is invalid: {loaded.env_error}",
            )
        cfg = loaded.config or ProxyConfig(config_path=cfg_path)
        telemetry_path = cfg.selection_telemetry.path.expanduser()
    # Presence is decided by the whole log, not the active file alone: a crash
    # between ``active → .1`` and the next append leaves the history entirely in
    # backups, and refusing to label it would contradict the default search.
    include_rotated = not active_only
    try:
        segments = discover_log_files(telemetry_path, include_rotated=include_rotated)
    except OSError as exc:
        # Listing the directory can fail on its own; "not found" would be the
        # wrong diagnosis, and a traceback is not something a scripted caller
        # can branch on.
        _feedback_failure(
            as_json, "log_unreadable", f"cannot list the log directory for {telemetry_path}: {exc}"
        )
    if not segments:
        _feedback_failure(as_json, "no_log", f"selection log not found: {telemetry_path}")
    # A segment this process cannot read would be skipped by the scan, turning
    # "I could not look there" into "no such selection" — a wrong answer that
    # reads like a right one.
    for segment in segments:
        try:
            with segment.open("rb"):
                pass
        except OSError as exc:
            _feedback_failure(
                as_json, "log_unreadable", f"cannot read log segment {segment.name}: {exc}"
            )

    # Resolve under the rotation lock. Rotation renames every segment at once,
    # so an unguarded scan can miss the newest selections — they move into a
    # file it already passed — or resolve one the same rotation evicts. The
    # writer takes this lock only when it has already decided to rotate and
    # defers instead of waiting, so holding it here cannot stall a proxied call.
    with _rotation_guard(telemetry_path, as_json) as acquired:
        if not acquired:
            _feedback_failure(
                as_json,
                "log_busy",
                "the selection log's rotation lock is held; nothing was written — re-run",
            )
        record = find_selection(
            telemetry_path,
            selection_id=selection_id,
            server=server,
            tool=tool,
            include_rotated=include_rotated,
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
    # A row can be present and still not be labellable: an unsupported schema
    # version, which offline replay drops outright, or no cohort stamp for the
    # label to inherit, which would file the operator's judgement under a
    # ranker this process guessed at. A label on either is the dead weight a
    # mistyped id would have produced. Reported by name rather than as "not
    # found", because the record IS there and an operator can go look at it.
    #
    # A missing ``selection_id`` is one of these defects rather than a code of
    # its own: an exact-id lookup can only return the row whose id equals the
    # (non-empty) argument, and ``--last`` skips defective rows, so a separate
    # "matched record has no id" branch had no way to fire.
    resolved_id = record.get("selection_id")
    defect = selection_defect(record)
    if defect is not None or not isinstance(resolved_id, str):
        _feedback_failure(
            as_json,
            "unusable_record",
            f"the matched selection cannot be labelled ({defect or 'no selection_id'}); "
            "label it by id against a record offline replay can load",
        )

    # Identify the row BEFORE writing, and — when a human is at the terminal —
    # let them stop it. ``--last`` is an inference; the operator is the check on
    # it, which they cannot be if the label is already on disk. Values are
    # escaped for display: ``selected_tool`` is upstream-controlled, and an ANSI
    # or bidi sequence in it could forge the very target being confirmed.
    if last and not assume_yes and (as_json or not _human_at_the_terminal()):
        # A formatting flag must not authorize a write, and a script cannot
        # answer a prompt — the repo-wide rule for a prompting action.
        _feedback_failure(
            as_json,
            "confirmation_required",
            "--last infers its target; pass --yes to confirm non-interactively",
            exit_code=2,
        )
    if not as_json:
        click.echo(
            f"Selection {_disp(resolved_id)} "
            f"({_disp(str(record.get('server')))} / {_disp(str(record.get('selected_tool')))})"
        )
    if last and not assume_yes:
        click.confirm("Label this selection?", default=False, abort=True)

    # Carry the selection's own trace_id onto the label so the feedback record
    # joins the metrics store the same way its selection does.
    trace_id = record.get("trace_id")
    # The label inherits the labelled selection's cohort stamp — see
    # ``log_feedback``. A stamp this process invented would be a claim about a
    # ranker that never ran for this call.
    ranker_version = record.get("ranker_version")
    # Second lock hold, around verify+append. The confirmation above is human
    # time, and a rotation landing in it can evict the very selection just
    # agreed to — so the target is re-checked while rotation is excluded,
    # rather than trusting a resolve that is now arbitrarily old.
    log = SelectionTelemetryLog(telemetry_path, max_bytes=_NEVER_ROTATE)
    with _rotation_guard(telemetry_path, as_json) as acquired:
        if not acquired:
            _feedback_failure(
                as_json,
                "log_busy",
                "the selection log's rotation lock is held; nothing was written — re-run",
            )
        if (
            find_selection(
                telemetry_path, selection_id=resolved_id, include_rotated=include_rotated
            )
            is None
        ):
            _feedback_failure(
                as_json,
                "log_rotated",
                f"selection {resolved_id} is no longer in the log (rotated out between "
                "resolution and append); nothing was written",
            )
        status = _write_label(
            log,
            selection_id=resolved_id,
            trace_id=trace_id if isinstance(trace_id, str) else None,
            user_corrected=corrected,
            operator_override=overridden,
            ranker_version=ranker_version if isinstance(ranker_version, str) else None,
        )
    # The sink swallows write failures by design — a telemetry fault must never
    # break a proxied call — but this caller is a person waiting to hear that
    # their label exists. Reporting success for a record that never reached
    # disk is worse than the failure itself.
    if status == APPEND_UNCONFIRMED:
        # The bytes are complete in the file but the flush proving they survive
        # a crash did not complete, so neither "written" nor "not written" is a
        # statement this process can make. Say which it is, and say that a
        # re-run is safe: repeating the same label for the same selection is
        # the accumulate-and-supersede case the schema already defines, not a
        # second, conflicting judgement.
        _feedback_failure(
            as_json,
            f"write_{status}",
            f"the label reached {telemetry_path} but could not be confirmed durable; "
            "check the log, and re-running with the same flags is safe",
        )
    if status != APPEND_WRITTEN:
        _feedback_failure(
            as_json,
            f"write_{status}",
            f"no label was written to {telemetry_path} (append status: {status})",
        )
    result = {
        "action": "selection-feedback",
        "ok": True,
        "selection_id": resolved_id,
        "trace_id": trace_id if isinstance(trace_id, str) else None,
        "server": record.get("server"),
        "selected_tool": record.get("selected_tool"),
        "user_corrected": corrected,
        "operator_override": overridden,
        "log": str(telemetry_path),
    }
    if as_json:
        click.echo(json_out.dumps(result, sort_keys=True, ensure_ascii=False))
        return
    click.echo(f"Labelled selection {_disp(resolved_id)}")
    for field in ("user_corrected", "operator_override"):
        if result[field] is not None:
            click.echo(f"  {field}: {str(result[field]).lower()}")


@contextmanager
def _rotation_guard(path: Path, as_json: bool) -> Iterator[bool]:
    """``rotation_lock`` whose *setup* failure is a reported error.

    The lock lives in a sidecar file, so taking it can fail for reasons that
    have nothing to do with contention — a writable log inside a directory this
    user cannot create files in, most plainly. Letting that ``OSError`` escape
    replaces the command's stable error document with a traceback and an empty
    stdout, which is exactly what a scripted caller cannot handle.
    """
    try:
        manager = rotation_lock(path, attempts=_LOCK_ATTEMPTS)
        entered = manager.__enter__()
    except OSError as exc:
        _feedback_failure(
            as_json,
            "lock_failed",
            f"cannot create the rotation lock beside {path}: {exc}",
        )
    try:
        yield entered
    finally:
        manager.__exit__(None, None, None)


def _write_label(log: SelectionTelemetryLog, **fields: Any) -> str:
    """Append the label. Split out so a test can observe when the write runs
    relative to the confirmation output, which output ordering alone cannot
    prove."""
    return log.log_feedback(**fields)


def _human_at_the_terminal() -> bool:
    """Whether anyone can answer a prompt.

    Named rather than inlined because it decides whether the confirmation
    exists at all: in a pipe or CI there is nobody to ask, and prompting would
    hang the caller instead of protecting it.
    """
    return sys.stdin.isatty()


def _tri_state(positive: bool, negative: bool, name: str) -> bool | None:
    """Fold two opposing flags into one three-valued label.

    Both set is a usage error, not a precedence question: the two flags assert
    contradictory facts about the same selection, and picking one would record
    a label the operator did not mean.
    """
    if positive and negative:
        raise click.UsageError(f"--{name} and --no-{name} contradict each other")
    if positive:
        return True
    if negative:
        return False
    return None


def _feedback_failure(as_json: bool, code: str, message: str, exit_code: int = 1) -> NoReturn:
    """Exit with a stable error code, in the caller's chosen format.

    Exit 1 is an operational failure; exit 2 is missing consent, matching the
    ``--json``-without-``--yes`` precedent elsewhere in the CLI.
    """
    if as_json:
        click.echo(
            json_out.dumps(
                {"action": "selection-feedback", "ok": False, "error": code, "message": message},
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        raise click.exceptions.Exit(exit_code)
    if exit_code == 2:
        raise click.UsageError(message)
    raise click.ClickException(message)
