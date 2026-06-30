"""``mms import`` Click command — RFC §7.2.

Reads MCP definitions out of host configs (claude-code / cursor /
codex / claude-desktop) and writes them to ``~/.mms/registry.toml``.

Defaults:

* ``--from all`` — iterate every host scanner.
* ``--plan`` — dry-run; secrets in env are redacted in output unless
  ``--show-imported`` is passed. (``--plan`` is the default mode.)
* ``--apply`` — write to registry; secrets stored verbatim.

Reconciliation when the same name exists across multiple hosts (or
already in the registry): **first-import-wins**. Identical re-imports
are idempotent (no-op + "already up to date" message); a same-name
entry with a *different* `command`/`args`/`env` is reported as a
conflict and skipped.

Sidecar population & recovery: every registry entry observed by an
``--apply`` run that is missing from ``~/.mms/import_state.toml``
gets stamped during that run. New entries (registry-additive) are
stamped from their candidate; idempotent entries (registry already
matches host) are *backfilled* from the same canonical hash. This
makes sidecar population idempotent-recoverable across:

* a crash between the registry and sidecar writes within one apply,
* a sidecar deleted out-of-band,
* a registry imported under a pre-PR2 mms (no wire existed yet).

Backfill timestamps reflect the recovery moment, not the original
import — the sidecar's ``last_imported`` is "first observation by a
wired apply", which is the only moment the hash can be authoritatively
computed. Backfill row hashes are byte-equal to what a fresh
``--apply`` would compute against the same canonical_form.

Backfill row attribution (``source_label``) reflects the apply that
observed the entry, not the original import — the host scanner only
sees what's there now, and "original import host" isn't recorded
anywhere we can recover from.

Registry write still precedes sidecar write within a single apply;
no transaction. To re-stamp existing sidecar rows (after a host edit
you've already acknowledged), use ``mms host sync --force`` — the
ongoing-reconciliation entry point, not ``mms import``.

vs ``mms host sync``:
  ``mms import`` is the first-time entry point (host → empty
  registry; additive only). For ongoing reconciliation — adding
  entries newly appearing at hosts, removing entries no longer at
  any host, and stamping baselines — see ``mms host sync``. The
  two have overlapping mutations but distinct intents; collapsing
  them would mask the "first import" vs "drift reconciliation"
  decision boundary.
"""

from __future__ import annotations

from pathlib import Path

import click

from memtomem_stm.cli._write_lock import with_write_lock
from memtomem_stm.mms import state
from memtomem_stm.mms.drift import HASH_VERSION, compute_drift_hash
from memtomem_stm.mms.import_hosts import (
    ALL_HOSTS,
    ImportCandidate,
    discover,
    project_local_gate_message,
)
from memtomem_stm.mms.secrets import redact_for_plan
from memtomem_stm.mms.state import RegistryServer

_VALID_FROM_VALUES = (*ALL_HOSTS, "all")


def _format_env_summary(
    server: RegistryServer, env_classification: dict, *, show_imported: bool
) -> str:
    """Render the env block for `--plan` output, redacting secrets unless --show-imported."""
    if not server.env:
        return "no env"
    if show_imported:
        # Show real values, but mark secrets so the user knows what's classified.
        parts = []
        for key, value in server.env.items():
            tag = " (secret)" if env_classification[key].is_secret else ""
            parts.append(f"{key}={value}{tag}")
        return "env: " + ", ".join(parts)

    redacted = redact_for_plan(server.env, env_classification)
    parts = []
    for key, value in redacted.items():
        cls = env_classification[key]
        tag = f" ← secret ({cls.reason})" if cls.is_secret else " (non-secret)"
        parts.append(f"{key}={value}{tag}")
    return "env: " + ", ".join(parts)


# Cross-imported by mms_host.sync_cmd (along with ``_format_env_summary``
# above). Two helpers already share the cross-import; the promotion
# trigger is now "next cross-imported helper joins" rather than "3rd
# caller of this function" — at that point both should land in
# ``mms/sync.py``. Signature drift is pinned by
# ``test_classify_against_registry_signature_pinned`` /
# ``test_format_env_summary_signature_pinned`` in tests/cli/test_mms_host.py.
def _classify_against_registry(
    candidates: list[ImportCandidate], registry: state.RegistryConfig
) -> tuple[
    list[ImportCandidate],  # new — safe to add
    list[tuple[ImportCandidate, str]],  # conflict — same name, different shape (skip + reason)
    list[ImportCandidate],  # idempotent — same name, same shape
]:
    """Bucket candidates by their relationship to the existing registry.

    First-import-wins: when the same name already lives in registry with
    a *different* shape, the candidate is reported as a conflict and
    skipped (the existing registry entry stays untouched). Identical
    shape → idempotent no-op.
    """
    # Per-import dedup: if the same name appears twice in `candidates`
    # (e.g. claude-code and cursor both define `filesystem`), the first
    # occurrence wins.
    seen_in_batch: dict[str, ImportCandidate] = {}
    new: list[ImportCandidate] = []
    conflicts: list[tuple[ImportCandidate, str]] = []
    idempotent: list[ImportCandidate] = []

    for cand in candidates:
        if cand.name in seen_in_batch:
            existing = seen_in_batch[cand.name]
            if existing.server == cand.server:
                idempotent.append(cand)
            else:
                conflicts.append((cand, f"already imported from {existing.source_label} this run"))
            continue
        seen_in_batch[cand.name] = cand

        existing_in_registry = registry.servers.get(cand.name)
        if existing_in_registry is None:
            new.append(cand)
        elif existing_in_registry == cand.server:
            idempotent.append(cand)
        else:
            conflicts.append((cand, "differs from existing registry entry"))

    return new, conflicts, idempotent


@click.command("import")
@click.option(
    "--from",
    "from_host",
    type=click.Choice(_VALID_FROM_VALUES, case_sensitive=False),
    default="all",
    show_default=True,
    help="Host config to scan (default: all).",
)
@click.option(
    "--plan/--apply",
    "is_plan",
    default=True,
    help="--plan (default) prints what would be imported with secrets REDACTED. "
    "--apply writes ~/.mms/registry.toml.",
)
@click.option(
    "--show-imported",
    is_flag=True,
    help="In --plan mode, reveal secret values instead of redacting.",
)
@click.option(
    "--allow-project-configs",
    is_flag=True,
    help="Acknowledge and register MCP entries discovered in project-local "
    "config files (.mcp.json / .cursor/mcp.json) under the current directory. "
    "Without this flag, --apply refuses to register them.",
)
@with_write_lock
def import_command(
    from_host: str, is_plan: bool, show_imported: bool, allow_project_configs: bool
) -> None:
    """Import MCP definitions from host configs into the mms registry."""
    cwd = Path.cwd().resolve()
    candidates = discover(from_host, cwd)

    if not candidates:
        click.echo(
            f"No MCP definitions found from --from={from_host}. Is the host config in place?"
        )
        return

    registry = state.load_registry()
    new, conflicts, idempotent = _classify_against_registry(candidates, registry)

    # ADD candidates that came from project-local files under cwd — an
    # untrusted repo checkout can ship these, so registering them is gated
    # behind --allow-project-configs (see import_hosts.ImportCandidate).
    repo_local_new = [cand for cand in new if cand.is_repo_local]

    # Render the plan. The same render runs in both --plan and --apply
    # modes — --apply just additionally writes the registry afterward.
    click.echo(f"Found {len(candidates)} MCP definition(s) from --from={from_host}:\n")

    for cand in candidates:
        env_summary = _format_env_summary(
            cand.server, cand.env_classification, show_imported=show_imported
        )
        click.echo(
            f"  {cand.name:<22} [{cand.source_label}]  command={cand.server.command}  {env_summary}"
        )

    click.echo("")
    click.echo(f"  to add (new):    {len(new)}")
    click.echo(f"  unchanged:       {len(idempotent)}")
    click.echo(f"  conflicts:       {len(conflicts)}  (skipped — first-import-wins)")

    if conflicts:
        click.echo("")
        click.echo("Conflicts:")
        for cand, reason in conflicts:
            click.echo(f"  - {cand.name} from {cand.source_label}: {reason}")

    if is_plan:
        click.echo("")
        if any(cand.env_classification[k].is_secret for cand in new for k in cand.server.env):
            if not show_imported:
                click.echo(
                    "Secrets above were REDACTED. To see actual values that would be imported:"
                )
                click.echo(f"  mms import --from {from_host} --plan --show-imported")
                click.echo("")
        if repo_local_new and not allow_project_configs:
            n = len(repo_local_new)
            click.echo(
                f"  {n} new entr{'y' if n == 1 else 'ies'} from project-local config under "
                "cwd; --apply will require --allow-project-configs."
            )
            click.echo("")
        click.echo(f"To apply: mms import --from {from_host} --apply")
        return

    # --apply
    # Fail closed before any write when the ADD set includes project-local
    # candidates (an untrusted repo checkout can ship .mcp.json /
    # .cursor/mcp.json) and the user hasn't acknowledged them.
    if repo_local_new and not allow_project_configs:
        click.echo(project_local_gate_message([cand.name for cand in repo_local_new]), err=True)
        raise SystemExit(2)

    # Load existing sidecar up front — needed to decide whether any
    # idempotent entries require backfill (sidecar row missing for an
    # entry that is already in the registry).
    existing_state = state.load_import_state()

    # Single timestamp captured at the action boundary — shared by every
    # row written this run (new + backfill) so PR3 can read "all rows
    # with timestamp T were observed in the same --apply".
    now = state.utc_now_iso()

    backfill_entries: dict[str, state.ImportStateEntry] = {}
    for cand in idempotent:
        if cand.name not in existing_state.entries:
            backfill_entries[cand.name] = state.ImportStateEntry(
                drift_hash=compute_drift_hash(cand.server),
                drift_hash_version=HASH_VERSION,
                last_imported=now,
                source_label=cand.source_label,
            )

    if not new and not backfill_entries:
        click.echo("")
        click.echo("Already up to date. Registry unchanged.")
        return

    new_entries: dict[str, state.ImportStateEntry] = {}
    if new:
        new_servers = {**registry.servers}
        for cand in new:
            new_servers[cand.name] = cand.server
            new_entries[cand.name] = state.ImportStateEntry(
                drift_hash=compute_drift_hash(cand.server),
                drift_hash_version=HASH_VERSION,
                last_imported=now,
                source_label=cand.source_label,
            )
        new_registry = state.RegistryConfig(
            schema_version=registry.schema_version, servers=new_servers
        )
        state.save_registry(new_registry)

    merged_state = state.ImportState(
        schema_version=existing_state.schema_version,
        entries={**existing_state.entries, **new_entries, **backfill_entries},
    )
    state.save_import_state(merged_state)

    click.echo("")
    if new:
        click.echo(
            f"Wrote {len(new)} new entr{'y' if len(new) == 1 else 'ies'} to {state.registry_path()}"
        )
    if backfill_entries:
        click.echo(
            f"Backfilled {len(backfill_entries)} sidecar "
            f"row{'' if len(backfill_entries) == 1 else 's'} to {state.import_state_path()}"
        )
