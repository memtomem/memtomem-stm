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
"""

from __future__ import annotations

from pathlib import Path

import click

from memtomem_stm.mms import state
from memtomem_stm.mms.import_hosts import (
    ALL_HOSTS,
    ImportCandidate,
    discover,
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
def import_command(from_host: str, is_plan: bool, show_imported: bool) -> None:
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
        click.echo(f"To apply: mms import --from {from_host} --apply")
        return

    # --apply
    if not new:
        click.echo("")
        click.echo("Already up to date. Registry unchanged.")
        return

    new_servers = {**registry.servers}
    for cand in new:
        new_servers[cand.name] = cand.server
    new_registry = state.RegistryConfig(schema_version=registry.schema_version, servers=new_servers)
    state.save_registry(new_registry)

    click.echo("")
    click.echo(
        f"Wrote {len(new)} new entr{'y' if len(new) == 1 else 'ies'} to {state.registry_path()}"
    )
