"""``mms host`` Click subgroup — W2 host-sync surfaces (RFC §7.3).

PR3 landed the read-only inspection command; PR4 promoted
``removed_at_host`` from a footer-only count into a main-table row:

* ``mms host status`` — classify every registry entry into one of four
  drift buckets relative to the W2 sidecar baseline, and surface the
  result either as a human table or ``--json``.

The bucket vocabulary is the contract that ``mms host scan``,
``mms host sync``, and ``mms host sync --force`` build on. PR3 froze the four
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

PR6 added ``mms host sync`` as the write-back counterpart to status's
read-only inspection. Sync's JSON shape (``mode``/``aborted``/``plan``
/``summary``) is its own contract independent of status's locked-in
4-key shape — bucket vocabulary is shared, but the surface that emits
it is not.
"""

from __future__ import annotations

import json as _json
import sys
from pathlib import Path

import click

from memtomem_stm.cli.mms_import import _classify_against_registry, _format_env_summary
from memtomem_stm.mms import state
from memtomem_stm.mms.drift import HASH_VERSION, compute_drift_hash
from memtomem_stm.mms.import_hosts import ALL_HOSTS, ImportCandidate, discover

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

# `mms host scan --from` choices. Derived from canonical ``ALL_HOSTS`` plus
# the "all" sentinel that ``import_hosts.discover()`` recognizes natively
# — when a 5th host is added to ``ALL_HOSTS``, scan picks it up automatically
# without a literal-list drift. ``case_sensitive=False`` matches ``mms
# import --from``'s contract so users get symmetric UX (``mms host scan
# --from CLAUDE-CODE`` works the same as ``mms import --from CLAUDE-CODE``).
_SCAN_HOST_CHOICES = click.Choice([*ALL_HOSTS, "all"], case_sensitive=False)

# ---------------------------------------------------------------------------
# sync UX strings — pinned templates so tests assert against the *symbol*.
# ---------------------------------------------------------------------------

_SYNC_NO_OP_MSG = "Already synchronized. No changes."

_SYNC_CHANGED_FOOTER_TEMPLATE = (
    "{n} entr{ies_or_y} differ in shape at host. Use `mms host sync --force` to acknowledge."
)

# Cross-host drift footer (Lock-down 6). Used in two contexts:
#   - Sub-line under ``_SYNC_CHANGED_FOOTER_TEMPLATE`` when ``--force``
#     is *not* set and some changed entries are cross-host (so users
#     know ``--force`` won't help these).
#   - Standalone footer when ``--force`` *is* set and only cross-host
#     entries remain in ``skipped_changed`` (the eligible subset got
#     re-stamped, so the original "use --force" footer would be wrong).
# ``--force`` deliberately skips cross-host entries because adopting a
# Pass-2 fallback candidate would silently relocate the entry's
# host-of-record — exactly the failure the W2 PR6 "no silent reshape"
# contract guards.
_SYNC_CROSS_HOST_FOOTER_TEMPLATE = (
    "{n} entr{ies_or_y} shape-relocated to a different host than baseline source — "
    "`--force` will not adopt these (manual review via `mms host status`)."
)

_SYNC_ORPHAN_NO_BASELINE_FOOTER_TEMPLATE = (
    "{n} entr{ies_or_y} in registry without baseline and not at any host scan."
)

_SYNC_REMOVE_PROMPT_HEADER_TEMPLATE = (
    "The following {n} entr{ies_or_y} will be removed from registry:"
)

_SYNC_RESTAMP_PROMPT_HEADER_TEMPLATE = (
    "The following {n} entr{ies_or_y} will be re-stamped (registry shape updated to current host):"
)

_SYNC_REMOVE_PROMPT_TAIL_TEMPLATE = (
    "Also: {added} added, {backfilled} backfilled, "
    "{cleanup} stale sidecar entr{cleanup_ies_or_y} cleaned."
)

_SYNC_NON_TTY_ABORT_TEMPLATE = (
    "Refusing to apply {n} registry mutation{s_suffix} non-interactively. Pass --yes to confirm."
)

_SYNC_DECLINE_MSG = "Aborted. No changes."

# Hint inserted under the ADD bucket (text mode) when any new candidate
# carries secret env. ``mms host sync`` deliberately does not offer
# ``--show-imported`` itself — escape hatch is ``mms import --plan
# --show-imported``. Surface it visibly so users don't grep the docs.
_SYNC_SHOW_IMPORTED_HINT = (
    "    (Use `mms import --plan --show-imported` to inspect ADD env values before --apply.)"
)


def _ies_or_y(n: int) -> str:
    return "y" if n == 1 else "ies"


def _s_suffix(n: int) -> str:
    """Plural helper for words like ``mutation`` (no -y/-ies vowel shift)."""
    return "" if n == 1 else "s"


def _is_interactive() -> bool:
    """TTY check for ``sync --apply`` confirmation gating.

    Wrapped as a module-level function so tests can flip the value
    cleanly via ``monkeypatch.setattr(mms_host, "_is_interactive",
    ...)``. ``CliRunner`` replaces ``sys.stdin`` with a non-TTY
    stream, so calling ``sys.stdin.isatty()`` directly inside tests
    would always read False — which would block the TTY-confirm test
    paths.
    """
    return sys.stdin.isatty()


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


def _render_scan_text(rows: list[dict], from_host: str) -> None:
    """Default human-readable scan renderer.

    Empty case: scoped one-liner — ``across host configs`` when no
    filter is in play, ``in <host>`` when ``--from <host>`` filters to
    one scanner. Non-empty: 3-column table (NAME / HOST / IN_REGISTRY)
    followed by a summary line. ``Yes``/``No`` for the boolean column —
    universal terminal encoding (no ✓/✗ glyphs that break on Windows
    cmd or restrictive SSH clients).

    Same-name multi-host rows are emitted in full — intentional
    divergence from ``mms host status``'s first-match-wins (scan is
    *collection*, status is *comparison*; they answer different
    questions and shouldn't share dedup logic).

    Rows render in ``ALL_HOSTS`` × per-scanner-discovery order. When
    the same name lives in two hosts that aren't iterated back-to-back,
    its rows may not be visually adjacent. JSON consumers grouping by
    name should sort their own pass. Reopen trigger for an
    alphabetical or state-priority sort here = user reports buried
    duplicates.
    """
    if not rows:
        scope = "across host configs" if from_host == "all" else f"in {from_host}"
        click.echo(f"No MCP entries discovered {scope}.\n", nl=False)
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
    entries_word = f"entr{_ies_or_y(n_total)}"
    hosts_word = "host" if n_hosts == 1 else "hosts"
    click.echo(
        f" {n_total} {entries_word} across {n_hosts} {hosts_word} "
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
        _render_scan_text(rows, from_host)


# ---------------------------------------------------------------------------
# sync — write-back surface (W2 PR6)
# ---------------------------------------------------------------------------


def _empty_sync_payload() -> dict:
    """Plan payload skeleton with all keys present + arrays empty.

    Used when an ``--apply`` aborts (TTY decline or non-TTY no-`--yes`)
    so the JSON shape stays uniform — downstream consumers don't have
    to special-case missing keys on abort.
    """
    return {
        "add": [],
        "remove": [],
        "backfill": [],
        "cleanup": [],
        "skipped_changed": [],
        "orphan_no_baseline": [],
        "conflicts": [],
    }


def _empty_sync_summary() -> dict:
    return {
        "added": 0,
        "removed": 0,
        "backfilled": 0,
        "cleanup": 0,
        "skipped_changed": 0,
        "orphan_no_baseline": 0,
        "restamped": 0,
        "unchanged": 0,
        "conflicts": 0,
    }


def _new_has_secret_env(new: list[ImportCandidate]) -> bool:
    """True when any ADD candidate has at least one secret-classified env key."""
    return any(cand.env_classification[k].is_secret for cand in new for k in cand.server.env)


def _format_env_keys_redacted(server: state.RegistryServer) -> str:
    """Render env as ``{key1, key2, ...}`` with keys sorted (diff-stable).

    Values redacted; ``mms host sync --force`` confirmation must show
    *what changed* without leaking secrets. Sorted ordering ensures
    two semantically-identical env dicts don't appear "different" just
    because their insertion order differs across host scanners.
    """
    if not server.env:
        return "{}"
    return "{" + ", ".join(sorted(server.env.keys())) + "}"


def _format_server_diff_lines(row: dict) -> list[str]:
    """Render Old/New shape for one re-stamp entry.

    Identical fields collapse to a single line so the prompt stays
    readable. Env values are redacted (key set only); ``command`` and
    ``args`` render verbatim. ``row`` carries the in-process keys
    ``_old_server`` and ``_new_server`` (excluded from the JSON
    payload — see ``sync_cmd``).
    """
    old: state.RegistryServer = row["_old_server"]
    new: state.RegistryServer = row["_new_server"]
    new_source = row["_new_source_label"]
    lines = [f"  - {row['name']}"]
    same_command = old.command == new.command
    same_args = list(old.args or []) == list(new.args or [])
    same_env = sorted(old.env.keys()) == sorted(new.env.keys())
    if same_command and same_args and same_env:
        # Edge case: hash differs but every observable surface matches.
        # Fall through to a single-line "unchanged surface" note —
        # users can still re-stamp (e.g. drift_hash_version bump).
        lines.append(
            f"    command={new.command}  args={list(new.args or [])}  "
            f"env={_format_env_keys_redacted(new)}"
        )
    else:
        lines.append(
            f"    Old: command={old.command}  args={list(old.args or [])}  "
            f"env={_format_env_keys_redacted(old)}"
        )
        lines.append(
            f"    New: command={new.command}  args={list(new.args or [])}  "
            f"env={_format_env_keys_redacted(new)}"
        )
    lines.append(f"    Source: {new_source}")
    return lines


def _build_apply_prompt(
    removed_rows: list[dict],
    restamp_rows: list[dict],
    summary: dict,
) -> str:
    """Compose the inline confirmation message for ``--apply`` mutations.

    Lists every entry by name + ``source_label`` + ``last_imported``
    so the user gives informed consent without a separate ``--plan``
    invocation. Tail summarizes the other (non-destructive) buckets so
    decline cost is visible (``Also: A added, B backfilled, ...``).

    Section order is intentional: REMOVE first (most destructive — entry
    disappears from registry), RESTAMP second (entry shape changes but
    name persists), summary tail last (additive buckets). Destructive-
    first lets users review the most reversibility-critical changes
    before fatigue. PRs that want to reorder must justify against this.

    ``restamp_rows`` is empty when ``--force`` is absent; the RE-STAMP
    section is skipped entirely. The W3 ``--force`` apply path fills it
    after the Lock-down 6 cross-host partition drops shape-relocated
    entries (re-stamping a candidate from a non-baseline host would
    silently relocate the entry's host-of-record).
    """
    lines: list[str] = []
    if removed_rows:
        n = len(removed_rows)
        lines.append(_SYNC_REMOVE_PROMPT_HEADER_TEMPLATE.format(n=n, ies_or_y=_ies_or_y(n)))
        for row in removed_rows:
            lines.append(
                f"  - {row['name']} (last seen: {row['source_label']}, {row['last_imported']})"
            )
        lines.append("")
    if restamp_rows:
        n = len(restamp_rows)
        lines.append(_SYNC_RESTAMP_PROMPT_HEADER_TEMPLATE.format(n=n, ies_or_y=_ies_or_y(n)))
        for row in restamp_rows:
            lines.extend(_format_server_diff_lines(row))
        lines.append("")
    lines.append(
        _SYNC_REMOVE_PROMPT_TAIL_TEMPLATE.format(
            added=summary["added"],
            backfilled=summary["backfilled"],
            cleanup=summary["cleanup"],
            cleanup_ies_or_y=_ies_or_y(summary["cleanup"]),
        )
    )
    lines.append("")
    lines.append("Proceed?")
    return "\n".join(lines)


def _render_sync_json(mode: str, plan_payload: dict, summary: dict, *, aborted: bool) -> None:
    """Emit the locked JSON shape — all keys always present.

    NOTE: ``summary.unchanged`` counts registry names (1 per registry
    name); ``summary.conflicts`` counts candidate occurrences (1 per
    candidate; multi-host name with shape divergence → multiple). The
    two report on different units; downstream tooling that sums them
    is buggy.
    """
    payload = {
        "mode": mode,
        "aborted": aborted,
        "plan": plan_payload,
        "summary": summary,
    }
    click.echo(_json.dumps(payload, indent=2, ensure_ascii=False))


def _render_sync_text(
    mode: str,
    plan_payload: dict,
    summary: dict,
    *,
    new: list[ImportCandidate],
    restamp_rows: list[dict] | None = None,
    cross_host_count: int = 0,
    force_active: bool = False,
    apply_outcome: str | None = None,
) -> None:
    """Default human-readable sync renderer.

    ``mode`` is "plan" or "apply"; ``apply_outcome`` is "no_op",
    "mutated", or None (None = plan mode). The same bucket sections
    render in every mode so users see consistent surface; only the
    final footer changes.

    ``restamp_rows`` (W3 ``--force``) renders as a RESTAMP section
    with an old/new diff per entry. Empty list (or default ``None``)
    skips the section. ``cross_host_count`` is the size of the
    Lock-down 6 cross-host partition; ``force_active`` flips the
    changed-bucket footer wording — without ``--force`` we point at
    ``--force`` as the remedy, with ``--force`` we point at manual
    review (``--force`` deliberately doesn't help cross-host).
    """
    add_rows = plan_payload["add"]
    remove_rows = plan_payload["remove"]
    backfill_rows = plan_payload["backfill"]
    cleanup_rows = plan_payload["cleanup"]
    conflict_rows = plan_payload["conflicts"]
    restamp_rows = restamp_rows or []

    def w(s: str) -> None:
        click.echo(s)

    w("Plan:" if mode == "plan" else "Apply:")
    if add_rows:
        n = len(add_rows)
        w(f"  ADD       {n} entr{_ies_or_y(n)} (new at host)")
        # Mirror import --plan's per-entry render shape, but always redact.
        cand_by_name = {c.name: c for c in new}
        for row in add_rows:
            cand = cand_by_name[row["name"]]
            env_summary = _format_env_summary(
                cand.server, cand.env_classification, show_imported=False
            )
            w(
                f"    - {cand.name} [{cand.source_label}]  "
                f"command={cand.server.command}  {env_summary}"
            )
        if _new_has_secret_env(new):
            w(_SYNC_SHOW_IMPORTED_HINT)
    if remove_rows:
        n = len(remove_rows)
        w(f"  REMOVE    {n} entr{_ies_or_y(n)} (no longer at any host)")
        for row in remove_rows:
            w(f"    - {row['name']} (last seen: {row['source_label']}, {row['last_imported']})")
    if backfill_rows:
        n = len(backfill_rows)
        w(f"  BACKFILL  {n} entr{_ies_or_y(n)} (re-stamp drift baseline)")
        for row in backfill_rows:
            w(f"    - {row['name']} [{row['host']}]")
    if cleanup_rows:
        n = len(cleanup_rows)
        w(f"  CLEANUP   {n} stale sidecar entr{_ies_or_y(n)} (no registry entry)")
        for row in cleanup_rows:
            w(f"    - {row['name']} (last seen: {row['source_label']}, {row['last_imported']})")
    if restamp_rows:
        n = len(restamp_rows)
        w(f"  RESTAMP   {n} entr{_ies_or_y(n)} (registry shape updated to current host)")
        for row in restamp_rows:
            for line in _format_server_diff_lines(row):
                w(line)

    w("")
    w(f"  {summary['unchanged']} unchanged")

    if conflict_rows:
        w("")
        n = len(conflict_rows)
        w(f"  Conflicts: {n}  (skipped — first-import-wins)")
        for row in conflict_rows:
            w(f"    - {row['name']} from {row['host']}: {row['reason']}")

    n_skipped = summary["skipped_changed"]
    if n_skipped:
        w("")
        if force_active:
            # ``--force`` was set; the only entries left in
            # skipped_changed are the cross-host slice (eligible got
            # re-stamped). Use the cross-host footer as the primary
            # message — the standard "use --force" pointer would be
            # wrong here since --force already declined to adopt these.
            w(
                "  "
                + _SYNC_CROSS_HOST_FOOTER_TEMPLATE.format(
                    n=n_skipped, ies_or_y=_ies_or_y(n_skipped)
                )
            )
        else:
            # No --force. Standard "use --force" pointer + optional
            # cross-host sub-line so users know --force won't help
            # those.
            w(
                "  "
                + _SYNC_CHANGED_FOOTER_TEMPLATE.format(n=n_skipped, ies_or_y=_ies_or_y(n_skipped))
            )
            if cross_host_count:
                w(
                    "  "
                    + _SYNC_CROSS_HOST_FOOTER_TEMPLATE.format(
                        n=cross_host_count, ies_or_y=_ies_or_y(cross_host_count)
                    )
                )

    if mode == "plan":
        w("")
        w("Run `mms host sync --apply` to execute.")
    elif apply_outcome == "no_op":
        w("")
        w(_SYNC_NO_OP_MSG)
    elif apply_outcome == "mutated":
        w("")
        if summary["added"]:
            w(
                f"Wrote {summary['added']} new entr{_ies_or_y(summary['added'])} "
                f"to {state.registry_path()}"
            )
        if summary["removed"]:
            w(
                f"Removed {summary['removed']} entr{_ies_or_y(summary['removed'])} "
                f"from {state.registry_path()}"
            )
        if summary["backfilled"]:
            w(
                f"Backfilled {summary['backfilled']} sidecar "
                f"row{'' if summary['backfilled'] == 1 else 's'} "
                f"in {state.import_state_path()}"
            )
        if summary["cleanup"]:
            w(
                f"Removed {summary['cleanup']} stale sidecar "
                f"entr{_ies_or_y(summary['cleanup'])} "
                f"from {state.import_state_path()}"
            )
        if summary["restamped"]:
            w(
                f"Re-stamped {summary['restamped']} entr{_ies_or_y(summary['restamped'])} "
                f"(registry + sidecar) — {state.registry_path()}, {state.import_state_path()}"
            )


def _render_orphan_no_baseline_footer(n: int) -> None:
    """Surface ``no_baseline`` rows that lack a matched candidate.

    These are non-mutating skips (registry entry exists but neither
    sidecar nor any host knows about it; sync can't safely act on
    them). Footer-only — they don't go in the plan payload arrays.
    """
    if n:
        click.echo(
            "  " + _SYNC_ORPHAN_NO_BASELINE_FOOTER_TEMPLATE.format(n=n, ies_or_y=_ies_or_y(n))
        )


@host_group.command("sync")
@click.option(
    "--plan/--apply",
    "is_plan",
    default=True,
    help="--plan (default) prints what would change; --apply writes registry + sidecar.",
)
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output.")
@click.option(
    "--yes",
    is_flag=True,
    help="Bypass the confirmation prompt before applying mutations.",
)
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Adopt host shape for entries flagged 'changed' (registry + sidecar). "
        "Skips entries that have shape-relocated to a different host than the "
        "baseline source. Orthogonal to --yes (which bypasses the confirmation "
        "prompt)."
    ),
)
def sync_cmd(is_plan: bool, json_output: bool, yes: bool, force: bool) -> None:
    """Reconcile registry + sidecar with the union of host scans.

    vs ``mms import``:
      ``mms import`` is the first-time entry point (host → empty/sparse
      registry; additive only). ``mms host sync`` is the ongoing-
      reconciliation entry point: it adds entries newly appearing at
      hosts, removes entries no longer at any host, and stamps
      baselines that PR2's import-time backfill couldn't cover. The
      two have overlapping mutations but distinct intents; collapsing
      them would mask the "first import" vs "drift reconciliation"
      decision boundary.

    Read-only buckets (see ``mms host status`` for definitions):
      - ``unchanged``: silent no-op (most common case).
      - ``changed``: non-mutating without ``--force``; user must
        acknowledge via ``--force`` to adopt the host shape. Surfaces
        as footer note + ``summary.skipped_changed``.

    Mutating buckets:
      - ``new`` (host candidate not in registry) → ADD.
      - ``removed_at_host`` (registry entry absent from every host
        scan) → REMOVE registry + sidecar entry.
      - ``no_baseline`` with matched host candidate → BACKFILL sidecar.
      - ``changed`` (with ``--force``) → RESTAMP registry + sidecar
        with current host shape. Lock-down 6: re-stamp candidate must
        come from the baseline source host. Cross-host (Pass-2)
        candidates are excluded — re-stamping from a non-baseline host
        would silently relocate the entry's host-of-record. Excluded
        entries surface in the extended CHANGED footer for manual
        review (``mms host status``).

    ``--force`` and ``--yes`` are orthogonal:
      - ``--force`` activates the ``changed`` bucket as a mutator
        (without it, ``changed`` stays non-mutating).
      - ``--yes`` bypasses the interactive confirmation prompt.
      ``--force`` alone still prompts; ``--force --yes`` is the
      non-interactive combination. ``--force --json`` without
      ``--yes`` aborts (exit 2, ``aborted: true``) — same gate REMOVE
      uses, since mixing prompt text with JSON corrupts parsing.

    Atomicity: registry write first, sidecar write second. The
    sidecar is filtered to ``set(import_state.entries) ⊆
    set(registry.servers)`` so a crash between writes self-heals on
    the next run (orphan baselines drop; missing baselines BACKFILL).

    With ``--json``, REMOVE and RESTAMP both require ``--yes`` — a TTY
    confirmation prompt cannot be answered programmatically by a JSON
    consumer, and mixing prompt text with JSON output corrupts machine
    parsing. Without ``--yes``, ``--json`` aborts (exit 2) with
    ``aborted: true`` in the payload.

    See ``mms_import.py`` for the matching docstring at
    ``import_command``.
    """
    cwd = Path.cwd().resolve()
    candidates = discover("all", cwd)
    registry = state.load_registry()
    import_state = state.load_import_state()

    # Two orthogonal classifiers (see Action-bucket matrix in the plan).
    new, conflicts, _idempotent = _classify_against_registry(candidates, registry)
    classified = _classify(registry, import_state, candidates)
    cand_by_name = _select_candidate_by_name(candidates, import_state)

    removed_at_host_rows = [r for r in classified if r["state"] == "removed_at_host"]
    changed_rows_classified = [r for r in classified if r["state"] == "changed"]
    no_baseline_rows = [r for r in classified if r["state"] == "no_baseline"]
    n_unchanged = sum(1 for r in classified if r["state"] == "unchanged")

    no_baseline_with_cand = [r for r in no_baseline_rows if cand_by_name.get(r["name"]) is not None]
    no_baseline_orphans = [r for r in no_baseline_rows if cand_by_name.get(r["name"]) is None]

    # Lock-down 6: partition ``changed`` by candidate host. Only entries
    # whose Pass-1 candidate matches the baseline source are valid
    # ``--force`` re-stamp targets; Pass-2 fallbacks to a different host
    # would silently relocate the entry's host-of-record (see
    # ``_select_candidate_by_name`` doc + W3 plan §Lock-down 6). The
    # cross-host slice surfaces as a sub-line in the CHANGED footer so
    # users can review via ``mms host status`` instead of having
    # ``--force`` adopt a non-baseline shape.
    restamp_eligible_rows = [
        r
        for r in changed_rows_classified
        if cand_by_name[r["name"]].source_label == r["source_label"]
    ]
    cross_host_changed_rows = [
        r
        for r in changed_rows_classified
        if cand_by_name[r["name"]].source_label != r["source_label"]
    ]

    # Pre-existing orphan sidecar entries: in sidecar but not in registry.
    # ``_classify`` is registry-anchored and never emits these — detect
    # separately.
    cleanup_names = sorted(set(import_state.entries.keys()) - set(registry.servers.keys()))

    plan_payload: dict = {
        "add": [{"name": c.name, "host": c.source_label} for c in new],
        "remove": [
            {
                "name": r["name"],
                "baseline_hash": r["baseline_hash"],
                "source_label": r["source_label"],
                "last_imported": r["last_imported"],
            }
            for r in removed_at_host_rows
        ],
        "backfill": [
            {"name": r["name"], "host": cand_by_name[r["name"]].source_label}
            for r in no_baseline_with_cand
        ],
        "cleanup": [
            {
                "name": name,
                "baseline_hash": import_state.entries[name].drift_hash,
                "source_label": import_state.entries[name].source_label,
                "last_imported": import_state.entries[name].last_imported,
            }
            for name in cleanup_names
        ],
        # ``skipped_changed`` lists changed entries that this run does
        # *not* re-stamp. Without ``--force``, that's every changed
        # entry (the bucket stays non-mutating per W2 PR6). With
        # ``--force``, only the cross-host slice remains skipped — the
        # eligible subset moves to RESTAMP. Counting both would
        # double-report and make the "use ``--force`` to acknowledge"
        # footer fire even after we just re-stamped them.
        #
        # Gains 5 fields in W3 (was minimal ``{"name": ...}`` in PR6).
        # Existing downstream parsers reading ``[i]["name"]`` keep
        # working — additive change at the entry-field level, not a
        # new top-level key. ``host_relocation`` flags the Lock-down 6
        # cross-host partition.
        "skipped_changed": [
            {
                "name": r["name"],
                "source_label": r["source_label"],
                "baseline_hash": r["baseline_hash"],
                "current_hash": r["current_hash"],
                "last_imported": r["last_imported"],
                "host_relocation": (cand_by_name[r["name"]].source_label != r["source_label"]),
            }
            for r in (cross_host_changed_rows if force else changed_rows_classified)
        ],
        # ``orphan_no_baseline``: registry has the entry, sidecar has no
        # baseline (or version mismatch), and no host scan finds it.
        # Sync can't safely act — JSON parity with the text footer.
        "orphan_no_baseline": [{"name": r["name"]} for r in no_baseline_orphans],
        "conflicts": [
            {"name": cand.name, "host": cand.source_label, "reason": reason}
            for cand, reason in conflicts
        ],
    }
    # ``restamped`` always present (default 0); incremented only on
    # ``--apply --force`` over baseline-source-matched entries.
    summary = {
        "added": len(plan_payload["add"]),
        "removed": len(plan_payload["remove"]),
        "backfilled": len(plan_payload["backfill"]),
        "cleanup": len(plan_payload["cleanup"]),
        "skipped_changed": len(plan_payload["skipped_changed"]),
        "orphan_no_baseline": len(plan_payload["orphan_no_baseline"]),
        "restamped": len(restamp_eligible_rows) if force else 0,
        "unchanged": n_unchanged,
        "conflicts": len(plan_payload["conflicts"]),
    }

    # In-process row enrichment for the diff renderer (Lock-down 2).
    # ``_old_server`` / ``_new_server`` / ``_new_source_label`` carry
    # the live ``RegistryServer`` references the prompt + plan-mode
    # RESTAMP section need — kept off the JSON payload (underscore-
    # prefixed keys are an in-process convention for "render-only").
    for row in restamp_eligible_rows:
        cand = cand_by_name[row["name"]]
        row["_old_server"] = registry.servers[row["name"]]
        row["_new_server"] = cand.server
        row["_new_source_label"] = cand.source_label

    if is_plan:
        if json_output:
            _render_sync_json("plan", plan_payload, summary, aborted=False)
        else:
            _render_sync_text(
                "plan",
                plan_payload,
                summary,
                new=new,
                restamp_rows=restamp_eligible_rows if force else [],
                cross_host_count=len(cross_host_changed_rows),
                force_active=force,
            )
            _render_orphan_no_baseline_footer(len(no_baseline_orphans))
        return

    # ----- --apply -----

    # ``--force`` activates the ``changed`` bucket as a mutator
    # (Lock-down 3). The cross-host slice (Lock-down 6) is excluded —
    # surfaced in the CHANGED footer for manual review only.
    restamp_rows = restamp_eligible_rows if force else []

    # Confirmation gate (REMOVE or RESTAMP non-empty).
    # ``--json`` + any destructive bucket forces ``--yes``: a TTY prompt
    # cannot be answered by a JSON consumer, and mixing prompt text with
    # JSON output would corrupt machine parsing. So we fall through to
    # the non-TTY abort path whenever ``--json`` is set, regardless of
    # the actual TTY status.
    needs_confirmation = bool(removed_at_host_rows) or bool(restamp_rows)
    if needs_confirmation and not yes:
        if not _is_interactive() or json_output:
            n_total = len(removed_at_host_rows) + len(restamp_rows)
            click.echo(
                _SYNC_NON_TTY_ABORT_TEMPLATE.format(n=n_total, s_suffix=_s_suffix(n_total)),
                err=True,
            )
            if json_output:
                _render_sync_json(
                    "apply", _empty_sync_payload(), _empty_sync_summary(), aborted=True
                )
            sys.exit(2)
        if not click.confirm(
            _build_apply_prompt(removed_at_host_rows, restamp_rows, summary),
            default=False,
        ):
            click.echo(_SYNC_DECLINE_MSG, err=True)
            sys.exit(2)

    # No-op gate. ``cleanup_names`` non-empty counts as a mutation
    # (orphan sidecar entries get filtered on the next save). Without
    # this, orphans would never be cleaned up by a "boring" sync and
    # the post-condition ``sidecar.keys() ⊆ registry.servers.keys()``
    # would not self-heal.
    no_mutations = not (new or removed_at_host_rows or no_baseline_with_cand or restamp_rows)
    no_orphans = not cleanup_names
    if no_mutations and no_orphans:
        if json_output:
            _render_sync_json("apply", plan_payload, summary, aborted=False)
        else:
            _render_sync_text(
                "apply",
                plan_payload,
                summary,
                new=new,
                restamp_rows=restamp_rows,
                cross_host_count=len(cross_host_changed_rows),
                force_active=force,
                apply_outcome="no_op",
            )
            _render_orphan_no_baseline_footer(len(no_baseline_orphans))
        return

    # Step 1: registry write (ADD inserts + REMOVE deletes + RESTAMP
    # replacements). Sequential passes, single ``save_registry`` —
    # cross-bucket name collision is impossible by construction (RESTAMP
    # rows require registry membership AND a host candidate; REMOVE
    # requires no candidate; ADD requires no registry membership).
    registry_changed = bool(new) or bool(removed_at_host_rows) or bool(restamp_rows)
    if registry_changed:
        new_servers = {**registry.servers}
        for cand in new:
            new_servers[cand.name] = cand.server
        for row in removed_at_host_rows:
            new_servers.pop(row["name"], None)
        # RESTAMP: replace the existing registry entry with the candidate's
        # server. Lock-down 1 (full reconciliation): registry shape adopts
        # the host's shape. Lock-down 6 has already filtered cross-host
        # candidates out of ``restamp_rows`` upstream.
        for row in restamp_rows:
            new_servers[row["name"]] = cand_by_name[row["name"]].server
        new_registry = state.RegistryConfig(
            schema_version=registry.schema_version, servers=new_servers
        )
        state.save_registry(new_registry)
    else:
        new_servers = dict(registry.servers)

    # Step 2: sidecar write. Filter to names surviving in new_servers
    # (orphan cleanup), then add ADD baselines + BACKFILL stamps + RESTAMP
    # refreshes. The filter enforces the post-condition atomically with
    # the rest of the write; PR2's "registry first, sidecar second"
    # extends here.
    now = state.utc_now_iso()
    new_state_entries: dict[str, state.ImportStateEntry] = {}
    for name, entry in import_state.entries.items():
        if name in new_servers:
            new_state_entries[name] = entry
    for cand in new:
        new_state_entries[cand.name] = state.ImportStateEntry(
            drift_hash=compute_drift_hash(cand.server),
            drift_hash_version=HASH_VERSION,
            last_imported=now,
            source_label=cand.source_label,
        )
    for row in no_baseline_with_cand:
        cand_for_row = cand_by_name[row["name"]]
        new_state_entries[row["name"]] = state.ImportStateEntry(
            drift_hash=compute_drift_hash(cand_for_row.server),
            drift_hash_version=HASH_VERSION,
            last_imported=now,
            source_label=cand_for_row.source_label,
        )
    for row in restamp_rows:
        cand_for_row = cand_by_name[row["name"]]
        new_state_entries[row["name"]] = state.ImportStateEntry(
            drift_hash=compute_drift_hash(cand_for_row.server),
            drift_hash_version=HASH_VERSION,
            last_imported=now,
            source_label=cand_for_row.source_label,
        )
    new_import_state = state.ImportState(
        schema_version=import_state.schema_version, entries=new_state_entries
    )
    state.save_import_state(new_import_state)

    if json_output:
        _render_sync_json("apply", plan_payload, summary, aborted=False)
    else:
        _render_sync_text(
            "apply",
            plan_payload,
            summary,
            new=new,
            restamp_rows=restamp_rows,
            cross_host_count=len(cross_host_changed_rows),
            force_active=force,
            apply_outcome="mutated",
        )
        _render_orphan_no_baseline_footer(len(no_baseline_orphans))
