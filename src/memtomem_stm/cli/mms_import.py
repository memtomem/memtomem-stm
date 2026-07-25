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

from memtomem_stm.cli._display import _disp
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
from memtomem_stm.utils.json_out import unencodable_field

_VALID_FROM_VALUES = (*ALL_HOSTS, "all")


def _partition_encodable(
    candidates: list[ImportCandidate],
) -> tuple[list[ImportCandidate], list[tuple[ImportCandidate, str]]]:
    """Split candidates into storable ones and (candidate, offending field).

    Gates exactly what gets stored and hashed: the name (a registry key, a
    proxy cache-key component) and the three fields ``drift.canonical_form``
    puts into the digest. ``unencodable_field`` returns a *path* such as
    ``env['TOK']`` and never the value (#758).
    """
    ok: list[ImportCandidate] = []
    refused: list[tuple[ImportCandidate, str]] = []
    for cand in candidates:
        bad = unencodable_field(
            {
                "name": cand.name,
                "command": cand.server.command,
                "args": list(cand.server.args or []),
                "env": dict(cand.server.env or {}),
            }
        )
        if bad is None:
            ok.append(cand)
        else:
            refused.append((cand, bad))
    return ok, refused


def _format_env_summary(
    server: RegistryServer, env_classification: dict, *, show_imported: bool
) -> str:
    """Render the env block for `--plan` output, redacting secrets unless --show-imported.

    Keys and values come straight out of a host's MCP config, so both are
    display-escaped here rather than at the two call sites (#760) — this is the
    only renderer of them, and ``mms host sync`` prints the same string.
    Redaction still happens first: escaping a value must never be what decides
    whether a secret is shown.
    """
    if not server.env:
        return "no env"
    if show_imported:
        # Show real values, but mark secrets so the user knows what's classified.
        parts = []
        for key, value in server.env.items():
            tag = " (secret)" if env_classification[key].is_secret else ""
            parts.append(f"{_disp(key)}={_disp(value)}{tag}")
        return "env: " + ", ".join(parts)

    redacted = redact_for_plan(server.env, env_classification)
    parts = []
    for key, value in redacted.items():
        cls = env_classification[key]
        tag = f" ← secret ({_disp(cls.reason)})" if cls.is_secret else " (non-secret)"
        parts.append(f"{_disp(key)}={_disp(value)}{tag}")
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

    # Drop entries that cannot survive being stored, BEFORE anything hashes or
    # renders them. A host config is plain `json.loads` with no character
    # validation, so `"\ud800"` — a legal JSON escape — reaches us as a code
    # unit that `compute_drift_hash` cannot encode, TOML cannot represent, and
    # the proxy cache cannot use as a key component. Such an entry is unusable
    # end to end, which is why `mms add` and the discovery scan already refuse
    # to create one (#757/#758); this is the third create path.
    #
    # Per entry, not per run: before this, the drift hash raised and aborted the
    # whole `--apply` with nothing imported, so one host's malformed entry cost
    # the user every clean entry beside it (#761).
    candidates, refused = _partition_encodable(candidates)
    for cand, bad_field in refused:
        # Name the field, never the value: env values are routinely secrets and
        # this text reaches CI logs — the rule `mms add` and the scan follow.
        #
        # `!r` rather than `_disp`: that helper lives in the #760 stack, and
        # `repr` already escapes the whole class here — a surrogate renders as
        # `\ud800` and a control character as its escape — which is also why
        # `unencodable_field` builds its own path with `!r`. `source_label` is
        # a host-spec literal this package writes.
        click.echo(
            f"Note: skipping {cand.name!r} from {cand.source_label} — "
            f"{bad_field} is not valid UTF-8 (value withheld).",
            err=True,
        )

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
        # Pad the *displayed* name, not the stored one: escaping lengthens it,
        # so measuring the raw value pads this column too narrow and raggeds
        # every column after it — the defect #755 recorded for `mms remove`'s
        # preview. `env_summary` is escaped by its own renderer (#760).
        click.echo(
            f"  {_disp(cand.name):<22} [{_disp(cand.source_label)}]  "
            f"command={_disp(cand.server.command)}  {env_summary}"
        )

    click.echo("")
    click.echo(f"  to add (new):    {len(new)}")
    click.echo(f"  unchanged:       {len(idempotent)}")
    click.echo(f"  conflicts:       {len(conflicts)}  (skipped — first-import-wins)")

    if conflicts:
        click.echo("")
        click.echo("Conflicts:")
        for cand, reason in conflicts:
            click.echo(f"  - {_disp(cand.name)} from {_disp(cand.source_label)}: {_disp(reason)}")

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
        click.echo(
            project_local_gate_message([_disp(cand.name) for cand in repo_local_new]), err=True
        )
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
