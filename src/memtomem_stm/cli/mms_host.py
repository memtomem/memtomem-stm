"""``mms host`` Click subgroup — W2 host-sync surfaces (RFC §7.3).

PR3 lands the read-only inspection command:

* ``mms host status`` — classify every registry entry into one of four
  drift buckets relative to the W2 sidecar baseline, and surface the
  result either as a human table or ``--json``.

The bucket vocabulary is the contract that PR4 (``removed_at_host``
main-table promotion) and W3+ (``--force`` re-stamp) build on. PR3
freezes the four state names; PR4 swaps a footer note for a main-table
row but does not rename anything.

The four buckets:

* ``unchanged`` — host scan finds the entry and its canonical hash
  matches the sidecar baseline.
* ``changed`` — host scan finds the entry but the canonical hash
  differs (host config was edited externally since last
  ``mms import --apply``).
* ``removed_at_host`` — registry has the entry, sidecar has the
  baseline, but no host scanner finds it. PR3 surfaces this in the
  footer as a count + neutral note; PR4 promotes it to the main table.
* ``no_baseline`` — sidecar lacks a row for this entry, OR the
  sidecar's ``drift_hash_version`` doesn't match the running mms's
  :data:`HASH_VERSION`. Either way the comparison can't be made and
  the user needs to re-stamp via ``mms import --apply``. This catches
  three real paths that PR2's backfill alone doesn't cover:

  1. Manual sidecar edits (RFC §3 says the user can hand-edit any of
     these files; the sidecar isn't exempt).
  2. Pre-PR2 installs that upgrade to the wired mms but call
     ``mms host status`` before any ``mms import --apply``.
  3. A future ``HASH_VERSION`` bump where v1 baselines coexist with a
     v2 binary — comparing them silently would always read as
     ``changed``, which is worse than asking for a re-stamp.

When candidates with the same name appear in multiple host configs,
the comparison candidate is chosen by ``baseline.source_label`` match
first, falling back to first-seen across :data:`ALL_HOSTS`. This keeps
``unchanged``/``changed`` stable when the user later mirrors an entry
into a second host — the import-time host stays the comparison axis.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import click

from memtomem_stm.mms import state
from memtomem_stm.mms.drift import HASH_VERSION, compute_drift_hash
from memtomem_stm.mms.import_hosts import ImportCandidate, discover

# ---------------------------------------------------------------------------
# Pinned UX strings — kept as module constants so tests assert against the
# *symbol* and any rewording is a single-place edit. Mirrors the convention
# in ``mms_project.py`` (``_REGISTRY_EMPTY_MSG``).
# ---------------------------------------------------------------------------

_NO_REGISTRY_MSG = "No registered MCP entries. Run `mms import --apply` to import host configs.\n"

_FOOTER_REMOVED_AT_HOST_TEMPLATE = "{n} entr{ies_or_y} in registry not present in any host scan"

_FOOTER_NO_BASELINE_TEMPLATE = (
    "{n} entr{ies_or_y} missing baseline hash — run `mms import --apply` to stamp"
)


def _ies_or_y(n: int) -> str:
    return "y" if n == 1 else "ies"


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


@click.group(name="host")
def host_group() -> None:
    """Host-config inspection and sync (RFC §7.3)."""


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _select_candidate_by_name(
    candidates: list[ImportCandidate],
    import_state: state.ImportState,
) -> dict[str, ImportCandidate]:
    """Return a name → candidate map with source_label-preferred matching.

    Pass 1: for every sidecar entry, look for a candidate with the same
    name *and* matching ``source_label``. This is the import-time host;
    keeping it as the comparison axis avoids drift flicker when the
    same entry name later appears in a second host.

    Pass 2: any name not yet bound falls back to first-seen across
    :data:`ALL_HOSTS` (the discover() iteration order). This covers
    entries the user moved to a different host, plus entries whose
    sidecar has no row yet (those will land in ``no_baseline`` anyway,
    but the candidate is still useful for the JSON ``current_hash``).
    """
    by_name: dict[str, ImportCandidate] = {}
    for name, baseline in import_state.entries.items():
        for cand in candidates:
            if cand.name == name and cand.source_label == baseline.source_label:
                by_name[name] = cand
                break
    for cand in candidates:
        by_name.setdefault(cand.name, cand)
    return by_name


def _classify(
    registry: state.RegistryConfig,
    import_state: state.ImportState,
    candidates: list[ImportCandidate],
) -> list[dict]:
    """Bucket every registry entry into one of four states.

    Returns a list of dicts (one per registry entry) with keys
    ``name``, ``state``, ``source_label``, ``baseline_hash``,
    ``current_hash``, ``last_imported``. Order matches
    ``registry.servers`` insertion order (registry.toml file order).
    """
    cand_by_name = _select_candidate_by_name(candidates, import_state)
    rows: list[dict] = []
    for name, server in registry.servers.items():
        baseline = import_state.entries.get(name)
        if baseline is None or baseline.drift_hash_version != HASH_VERSION:
            # ``no_baseline`` covers both the missing-row case and the
            # version-mismatch case — they both mean "the comparison
            # can't be trusted; re-stamp". Surfacing them as one bucket
            # keeps the user-facing fix the same single command.
            rows.append(
                {
                    "name": name,
                    "state": "no_baseline",
                    "source_label": baseline.source_label if baseline else None,
                    "baseline_hash": baseline.drift_hash if baseline else None,
                    "current_hash": compute_drift_hash(server),
                    "last_imported": baseline.last_imported if baseline else None,
                }
            )
            continue
        cand = cand_by_name.get(name)
        if cand is None:
            rows.append(
                {
                    "name": name,
                    "state": "removed_at_host",
                    "source_label": baseline.source_label,
                    "baseline_hash": baseline.drift_hash,
                    "current_hash": None,
                    "last_imported": baseline.last_imported,
                }
            )
            continue
        current_hash = compute_drift_hash(cand.server)
        rows.append(
            {
                "name": name,
                "state": "unchanged" if current_hash == baseline.drift_hash else "changed",
                "source_label": baseline.source_label,
                "baseline_hash": baseline.drift_hash,
                "current_hash": current_hash,
                "last_imported": baseline.last_imported,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_text(rows: list[dict]) -> None:
    """Default human-readable renderer.

    Main table shows ``unchanged`` + ``changed`` rows (the comparable
    states). ``removed_at_host`` and ``no_baseline`` are footer notes
    only — they don't have a meaningful current-hash to put in the
    SOURCE/STATE pair, so dumping them in the table would dilute the
    comparison signal. Each is conditionally shown when its count > 0.

    Column widths follow ``mms project list`` (mms_project.py:285):
    plain f-strings, no rich.
    """
    main_rows = [r for r in rows if r["state"] in ("unchanged", "changed")]
    n_unchanged = sum(1 for r in main_rows if r["state"] == "unchanged")
    n_changed = sum(1 for r in main_rows if r["state"] == "changed")
    n_removed = sum(1 for r in rows if r["state"] == "removed_at_host")
    n_no_baseline = sum(1 for r in rows if r["state"] == "no_baseline")

    if main_rows:
        click.echo(f" {'NAME':<20} {'STATE':<10} SOURCE")
        for row in main_rows:
            source = row["source_label"] or ""
            click.echo(f" {row['name']:<20} {row['state']:<10} {source}")
        click.echo("")
    click.echo(f" {n_unchanged} unchanged, {n_changed} changed at host")
    if n_removed:
        click.echo(
            " "
            + _FOOTER_REMOVED_AT_HOST_TEMPLATE.format(n=n_removed, ies_or_y=_ies_or_y(n_removed))
        )
    if n_no_baseline:
        click.echo(
            " "
            + _FOOTER_NO_BASELINE_TEMPLATE.format(
                n=n_no_baseline, ies_or_y=_ies_or_y(n_no_baseline)
            )
        )


def _render_json(rows: list[dict]) -> None:
    """JSON renderer. Includes every state; summary always carries 4 keys."""
    summary = {
        "unchanged": sum(1 for r in rows if r["state"] == "unchanged"),
        "changed": sum(1 for r in rows if r["state"] == "changed"),
        "removed_at_host": sum(1 for r in rows if r["state"] == "removed_at_host"),
        "no_baseline": sum(1 for r in rows if r["state"] == "no_baseline"),
    }
    payload = {"entries": rows, "summary": summary}
    click.echo(_json.dumps(payload, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@host_group.command("status")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output.")
def status_cmd(json_output: bool) -> None:
    """Show drift state of every registry entry vs sidecar baseline.

    Read-only: never writes to registry, sidecar, or anywhere else.
    Exit code is always 0 — drift is a normal observation, not a CI
    failure signal.
    """
    registry = state.load_registry()
    import_state = state.load_import_state()

    if not registry.servers:
        if json_output:
            click.echo(
                _json.dumps(
                    {
                        "entries": [],
                        "summary": {
                            "unchanged": 0,
                            "changed": 0,
                            "removed_at_host": 0,
                            "no_baseline": 0,
                        },
                    },
                    indent=2,
                )
            )
            return
        click.echo(_NO_REGISTRY_MSG, nl=False)
        return

    candidates = discover("all", Path.cwd().resolve())
    rows = _classify(registry, import_state, candidates)
    if json_output:
        _render_json(rows)
    else:
        _render_text(rows)
