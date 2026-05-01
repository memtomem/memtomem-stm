"""``mms host`` Click subgroup — W2 host-sync surfaces (RFC §7.3).

PR3 landed the read-only inspection command; PR4 promoted
``removed_at_host`` from a footer-only count into a main-table row:

* ``mms host status`` — classify every registry entry into one of four
  drift buckets relative to the W2 sidecar baseline, and surface the
  result either as a human table or ``--json``.

The bucket vocabulary is the contract that W3+ (``--force`` re-stamp,
``mms host scan``, ``mms host sync``) builds on. PR3 froze the four
state names; PR4 changed *where* ``removed_at_host`` renders without
renaming anything. JSON shape and footer text are backwards-compat
anchors and stayed frozen across PR4.

The four buckets:

* ``unchanged`` — host scan finds the entry and its canonical hash
  matches the sidecar baseline.
* ``changed`` — host scan finds the entry but the canonical hash
  differs (host config was edited externally since last
  ``mms import --apply``).
* ``removed_at_host`` — registry has the entry, sidecar has the
  baseline, but no host scanner finds it. PR3 surfaced this as a
  footer count + neutral note only; PR4 added a main-table row while
  keeping the footer count as a backwards-compat anchor for downstream
  tooling that grepped PR3's output.
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

_NO_SCAN_RESULTS_MSG = "No MCP entries discovered across host configs.\n"

# `mms host scan --from` choices. Subset of ALL_HOSTS plus the "all" sentinel
# that ``import_hosts.discover()`` recognizes natively.
_SCAN_HOST_CHOICES = click.Choice(["all", "claude-code", "cursor", "codex", "claude-desktop"])


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
    for name in registry.servers:
        baseline = import_state.entries.get(name)
        cand = cand_by_name.get(name)
        # ``current_hash`` is uniformly the host's view: the canonical
        # hash of the matched candidate, or ``None`` if no host scanner
        # finds the entry. This keeps the field's meaning the same
        # across every bucket — downstream tooling that recomputes
        # against the host config gets the same value back regardless
        # of whether the row is unchanged / changed / removed / has no
        # baseline.
        current_hash = compute_drift_hash(cand.server) if cand is not None else None

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
                    "current_hash": current_hash,
                    "last_imported": baseline.last_imported if baseline else None,
                }
            )
            continue
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

    Main table shows ``unchanged`` + ``changed`` + ``removed_at_host``
    rows — the three states that carry a baseline source attribution
    and benefit from name-level visibility. ``no_baseline`` stays
    footer-only: the only meaningful response is ``mms import --apply``
    to re-stamp, and the footer's hint conveys that more directly than
    a table row would. (The version-mismatch sub-case does carry a real
    ``source_label`` and ``baseline_hash``; the missing-sidecar
    sub-case doesn't — but the user-facing action is the same either
    way.)

    ``removed_at_host`` rows render with ``current_hash=None`` (no
    host candidate to canonicalize) but the table only shows NAME +
    STATE + SOURCE, all of which come from the preserved baseline. The
    footer count + neutral note still appear after the summary line as
    a backwards-compat anchor for downstream tooling that grepped
    PR3's output — duplication with the table row is intentional.

    Rows render in ``registry.servers`` insertion order (registry.toml
    file order). Stable order > clever sort; if a user reports they
    missed a single ``removed_at_host`` buried among many ``unchanged``
    rows, swap in a state-priority sort here.

    Column widths follow ``mms project list`` (mms_project.py:285):
    plain f-strings, no rich. Alignment is best-effort — names longer
    than the NAME column overflow rather than truncate. Not a contract;
    swap in textual/rich/whatever when MCP names regularly exceed 20
    chars in practice. STATE is widened to 16 chars so
    ``removed_at_host`` (15) fits with one char of padding past the
    longest state name.
    """
    main_rows = [r for r in rows if r["state"] in ("unchanged", "changed", "removed_at_host")]
    n_unchanged = sum(1 for r in main_rows if r["state"] == "unchanged")
    n_changed = sum(1 for r in main_rows if r["state"] == "changed")
    n_removed = sum(1 for r in rows if r["state"] == "removed_at_host")
    n_no_baseline = sum(1 for r in rows if r["state"] == "no_baseline")

    if main_rows:
        click.echo(f" {'NAME':<20} {'STATE':<16} SOURCE")
        for row in main_rows:
            source = row["source_label"] or ""
            click.echo(f" {row['name']:<20} {row['state']:<16} {source}")
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


# ---------------------------------------------------------------------------
# scan — host-side discovery surface (W2 PR5)
# ---------------------------------------------------------------------------


def _scan_rows(
    candidates: list[ImportCandidate],
    registry: state.RegistryConfig,
) -> list[dict]:
    """Project each ImportCandidate into a scan row.

    No deduplication: same name across hosts emits multiple rows — the
    full host-side inventory is the value scan provides. ``in_registry``
    is a name match only (``cand.name in registry.servers``); shape
    comparison is delegated to ``mms host status``.
    """
    return [
        {
            "name": c.name,
            "host": c.source_label,
            "in_registry": c.name in registry.servers,
        }
        for c in candidates
    ]


def _render_scan_text(rows: list[dict]) -> None:
    """Default human-readable scan renderer.

    Empty case: friendly one-liner. Non-empty: 3-column table (NAME /
    HOST / IN_REGISTRY) followed by a summary line. ``Yes``/``No`` for
    the boolean column — universal terminal encoding (no ✓/✗
    glyphs that break on Windows cmd or restrictive SSH clients).

    Same-name multi-host rows are emitted in full — intentional
    divergence from ``mms host status``'s first-match-wins (scan is
    *collection*, status is *comparison*; they answer different
    questions and shouldn't share dedup logic).
    """
    if not rows:
        click.echo(_NO_SCAN_RESULTS_MSG, nl=False)
        return
    click.echo(f" {'NAME':<20} {'HOST':<22} IN_REGISTRY")
    for row in rows:
        flag = "Yes" if row["in_registry"] else "No"
        click.echo(f" {row['name']:<20} {row['host']:<22} {flag}")
    click.echo("")
    n_total = len(rows)
    n_in_reg = sum(1 for r in rows if r["in_registry"])
    n_new = n_total - n_in_reg
    n_hosts = len({r["host"] for r in rows})
    click.echo(
        f" {n_total} entries across {n_hosts} host{'s' if n_hosts != 1 else ''} "
        f"({n_in_reg} in registry, {n_new} new at host)"
    )


def _render_scan_json(rows: list[dict]) -> None:
    """JSON scan renderer. Always emits 3-key summary (zero counts explicit)."""
    n_total = len(rows)
    n_in_reg = sum(1 for r in rows if r["in_registry"])
    payload = {
        "entries": rows,
        "summary": {
            "total": n_total,
            "in_registry": n_in_reg,
            # ``new_at_host`` is the complementary symmetry to
            # ``mms host status``'s ``removed_at_host`` (in registry,
            # not at host vs at host, not in registry).
            "new_at_host": n_total - n_in_reg,
        },
    }
    click.echo(_json.dumps(payload, indent=2, ensure_ascii=False))


@host_group.command("scan")
@click.option(
    "--from",
    "from_host",
    type=_SCAN_HOST_CHOICES,
    default="all",
    show_default=True,
    help="Limit scan to one host config.",
)
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output.")
def scan_cmd(from_host: str, json_output: bool) -> None:
    """List MCP entries discovered across host configs.

    Read-only host-side inventory. Complements ``mms host status``:
    scan is host-anchored (collection — every host occurrence shown),
    status is registry-anchored (comparison — one row per registry
    entry against sidecar baseline). When the same name appears in
    multiple host configs, scan emits every occurrence; status's
    first-match-wins is an axis difference, not an inconsistency.

    The ``IN_REGISTRY`` column is a name match only
    (``cand.name in registry.servers``). A registered entry whose host
    shape differs still shows ``Yes`` here — shape comparison is
    delegated to ``mms host status`` (it surfaces shape mismatches as
    ``changed``).

    Exit code is always 0; this is read-only inspection.
    """
    registry = state.load_registry()
    candidates = discover(from_host, Path.cwd().resolve())
    rows = _scan_rows(candidates, registry)
    if json_output:
        _render_scan_json(rows)
    else:
        _render_scan_text(rows)
