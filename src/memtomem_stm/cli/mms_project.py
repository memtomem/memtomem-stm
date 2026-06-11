"""``mms project`` Click subgroup — W1 project state CRUD (RFC §7.1).

Five subcommands: ``init / show / list / enable / disable``. All
reads/writes go through :mod:`memtomem_stm.mms.state`; project
detection through :mod:`memtomem_stm.mms.detect`.

The W1 contract for ``enable`` against an empty registry is a
*graceful friendly error* (not a crash) — the registry is filled by
``mms import --apply`` (PR2). The literal message is pinned by tests.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import click

from memtomem_stm.mms import state
from memtomem_stm.mms.detect import Project, Source, detect_project

# ---------------------------------------------------------------------------
# Pinned UX strings — kept here as constants so tests assert against the
# *symbol* and any rewording is a single-place edit.
# ---------------------------------------------------------------------------

_REGISTRY_EMPTY_MSG = (
    "Registry is empty.\n"
    "  Run `mms import --from <host>` (W1 PR2) to import existing MCP definitions,\n"
    "  or wait for the integrated `mms add` path (planned post-W1).\n"
)


def _show_no_marker_no_git_text(cwd: Path) -> str:
    return (
        f"No project marker (.mms/project.toml) or git repo found at {cwd}.\n"
        f"Cwd-fallback project: '{cwd.name}' (anonymous, not registered).\n"
        f"  Run `mms project init` here to create .mms/project.toml.\n"
    )


def _show_git_no_marker_text(root: Path) -> str:
    return (
        f"Detected via git: {root}\n"
        f"No project marker (.mms/project.toml) yet.\n"
        f"  Run `mms project init` here to create one.\n"
    )


# ---------------------------------------------------------------------------
# Group + helpers
# ---------------------------------------------------------------------------


@click.group(name="project")
def project_group() -> None:
    """Project-scoped MCP management (RFC §7.1).

    Commands operate on the project containing the current working
    directory unless ``--project NAME`` is passed (where supported).
    """


def _project_payload(p: Project) -> dict:
    """Render a :class:`Project` as the JSON shape used by ``--json`` output."""
    payload = {
        "name": p.name,
        "root": str(p.root),
        "source": p.source.value,
        "marker": str(p.marker_path) if p.marker_path else None,
        "enabled": list(p.config.mcp.enabled) if p.config else [],
    }
    return payload


def _resolve_project_for_mutation(project_name: str | None) -> Project:
    """Resolve which project a mutation (enable/disable) targets.

    Without ``--project NAME``: detect from cwd; require a marker
    (init suggested otherwise). With ``--project NAME``: look up in
    the projects index and load that project's marker.
    """
    if project_name is None:
        p = detect_project(Path.cwd())
        if p.source is not Source.MARKER:
            raise click.ClickException(
                f"No project marker at {p.root} (detected via {p.source.value}). "
                "Run `mms project init` here first."
            )
        return p

    # --project NAME path
    idx = state.load_projects_index()
    matches = [entry for entry in idx.projects if entry.name == project_name]
    if not matches:
        raise click.ClickException(
            f"Project '{project_name}' not found in {state.projects_index_path()}. "
            "Run `mms project list` to see known projects."
        )
    if len(matches) > 1:
        raise click.ClickException(
            f"Project name '{project_name}' is ambiguous ({len(matches)} matches). "
            "Use `mms project show NAME` from inside the target project root instead."
        )
    entry = matches[0]
    marker = Path(entry.path) / state.PROJECT_MARKER_RELPATH
    if not marker.is_file():
        raise click.ClickException(
            f"Project '{project_name}' indexed at {entry.path} but marker file is missing. "
            "The directory may have been moved or the marker deleted; "
            "run `mms project list --prune` to clean up."
        )
    cfg = state.load_project_config(marker)
    return Project(
        root=Path(entry.path),
        source=Source.MARKER,
        name=cfg.project.name,
        marker_path=marker,
        config=cfg,
    )


def _refresh_index(name: str, root: Path) -> None:
    """Upsert ``(name, root)`` into the projects index."""
    idx = state.load_projects_index()
    idx = state.upsert_project_in_index(idx, name=name, path=str(root))
    state.save_projects_index(idx)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@project_group.command("init")
@click.argument(
    "path",
    required=False,
    type=click.Path(file_okay=False, path_type=Path),
)
@click.option(
    "--name", "name_opt", default=None, help="Override project name (default: dir basename)."
)
@click.option("--force", is_flag=True, help="Overwrite an existing .mms/project.toml.")
def init_cmd(path: Path | None, name_opt: str | None, force: bool) -> None:
    """Create ``<path>/.mms/project.toml`` (default path = cwd) and add to index."""
    target_root = (path or Path.cwd()).expanduser().resolve()
    if not target_root.is_dir():
        raise click.ClickException(f"Not a directory: {target_root}")

    marker = target_root / state.PROJECT_MARKER_RELPATH
    if marker.is_file() and not force:
        raise click.ClickException(
            f"{marker} already exists. Use --force to overwrite, or edit it directly."
        )

    project_name = name_opt or target_root.name
    cfg = state.ProjectConfig(
        project=state.ProjectMeta(name=project_name),
        mcp=state.ProjectMcp(enabled=[]),
    )
    state.save_project_config(cfg, marker)
    _refresh_index(project_name, target_root)
    click.echo(f"Created {marker}")
    click.echo(f"Indexed in {state.projects_index_path()}")


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@project_group.command("show")
@click.argument("name", required=False)
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output.")
def show_cmd(name: str | None, json_output: bool) -> None:
    """Show the detected (or named) project, with init hints when no marker."""
    if name is not None:
        idx = state.load_projects_index()
        matches = [e for e in idx.projects if e.name == name]
        if not matches:
            raise click.ClickException(
                f"Project '{name}' not found in {state.projects_index_path()}."
            )
        # The index is keyed by canonical path, so two projects can share a
        # name — mirror _resolve_project_for_mutation's ambiguity error
        # instead of silently showing an arbitrary one of them.
        if len(matches) > 1:
            raise click.ClickException(
                f"Project name '{name}' is ambiguous ({len(matches)} matches). "
                "Run `mms project show` without NAME from inside the "
                "target project root instead."
            )
        entry = matches[0]
        marker = Path(entry.path) / state.PROJECT_MARKER_RELPATH
        if not marker.is_file():
            raise click.ClickException(
                f"Project '{name}' indexed at {entry.path} but marker is missing."
            )
        cfg = state.load_project_config(marker)
        proj = Project(
            root=Path(entry.path),
            source=Source.MARKER,
            name=cfg.project.name,
            marker_path=marker,
            config=cfg,
        )
        _emit_show(proj, json_output, cwd=Path(entry.path))
        return

    cwd = Path.cwd().resolve()
    proj = detect_project(cwd)
    _emit_show(proj, json_output, cwd=cwd)


def _emit_show(proj: Project, json_output: bool, *, cwd: Path) -> None:
    if json_output:
        click.echo(_json.dumps(_project_payload(proj), indent=2))
        return

    if proj.source is Source.MARKER:
        click.echo(f"Project: {proj.name}")
        click.echo(f"Root: {proj.root}")
        click.echo("Detected via: marker")
        enabled = proj.config.mcp.enabled if proj.config else []
        if enabled:
            click.echo(f"Enabled MCPs: {', '.join(enabled)}")
        else:
            click.echo("Enabled MCPs: (none)")
        return

    if proj.source is Source.GIT:
        click.echo(_show_git_no_marker_text(proj.root), nl=False)
        return

    # Source.CWD
    click.echo(_show_no_marker_no_git_text(cwd), nl=False)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@project_group.command("list")
@click.option("--prune", is_flag=True, help="Remove entries whose path no longer exists.")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output.")
def list_cmd(prune: bool, json_output: bool) -> None:
    """List known projects from the index. Mark current cwd's project with ``*``."""
    idx = state.load_projects_index()
    cwd = Path.cwd().resolve()

    pruned: list[state.ProjectIndexEntry] = []
    if prune:
        kept: list[state.ProjectIndexEntry] = []
        for entry in idx.projects:
            if Path(entry.path).is_dir():
                kept.append(entry)
            else:
                pruned.append(entry)
        if pruned:
            new_idx = state.ProjectsIndex(schema_version=idx.schema_version, projects=kept)
            state.save_projects_index(new_idx)
            idx = new_idx

    def _is_current(entry: state.ProjectIndexEntry) -> bool:
        return Path(entry.path).resolve() == cwd

    if json_output:
        payload = {
            "projects": [
                {
                    "name": e.name,
                    "path": e.path,
                    "last_seen": e.last_seen,
                    "current": _is_current(e),
                }
                for e in idx.projects
            ],
            "pruned": [{"name": e.name, "path": e.path} for e in pruned],
        }
        click.echo(_json.dumps(payload, indent=2))
        return

    for entry in pruned:
        click.echo(f"pruned: {entry.name} ({entry.path})")
    if pruned:
        click.echo(f"Pruned {len(pruned)} stale entr{'y' if len(pruned) == 1 else 'ies'}.")
    if not idx.projects:
        click.echo("No projects indexed yet. Run `mms project init` in a project directory.")
        return
    for entry in idx.projects:
        marker = "*" if _is_current(entry) else " "
        click.echo(f"{marker} {entry.name}\t{entry.path}\t(last seen {entry.last_seen})")


# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------


@project_group.command("enable")
@click.argument("mcps", nargs=-1, required=True)
@click.option(
    "--project", "project_name", default=None, help="Target project (default: detect from cwd)."
)
def enable_cmd(mcps: tuple[str, ...], project_name: str | None) -> None:
    """Add MCP names to the project's enabled list (RFC §7.1)."""
    proj = _resolve_project_for_mutation(project_name)

    registry = state.load_registry()
    if not registry.servers:
        # PR1 graceful contract — empty registry is the dominant W1 failure
        # mode (PR2 has not landed yet). Pinned literal text per plan.
        click.echo(_REGISTRY_EMPTY_MSG, nl=False, err=True)
        raise click.exceptions.Exit(1)

    # Reject names absent from the registry: a typo would otherwise be
    # silently persisted to the enabled list and resolve to nothing at proxy
    # time, with no signal to the user. `disable` stays registry-agnostic by
    # design (its docstring: must work against an empty registry).
    unknown = [m for m in mcps if m not in registry.servers]
    if unknown:
        known = ", ".join(sorted(registry.servers))
        raise click.ClickException(
            f"Unknown MCP name(s): {', '.join(unknown)}. "
            f"Registered: {known}. Run `mms list` to inspect the registry."
        )

    assert proj.config is not None  # _resolve_project_for_mutation guarantees MARKER
    current = list(proj.config.mcp.enabled)
    added: list[str] = []
    for mcp in mcps:
        if mcp not in current:
            current.append(mcp)
            added.append(mcp)

    new_cfg = state.ProjectConfig(
        schema_version=proj.config.schema_version,
        project=proj.config.project,
        mcp=state.ProjectMcp(enabled=current),
    )
    assert proj.marker_path is not None
    state.save_project_config(new_cfg, proj.marker_path)
    _refresh_index(proj.name, proj.root)

    if added:
        click.echo(f"Enabled in '{proj.name}': {', '.join(added)}")
    else:
        click.echo(f"No changes to '{proj.name}' (all already enabled).")


@project_group.command("disable")
@click.argument("mcps", nargs=-1, required=True)
@click.option(
    "--project", "project_name", default=None, help="Target project (default: detect from cwd)."
)
def disable_cmd(mcps: tuple[str, ...], project_name: str | None) -> None:
    """Remove MCP names from the project's enabled list.

    Runs against the project state only — does not consult the registry,
    so disable works even when the registry is empty.
    """
    proj = _resolve_project_for_mutation(project_name)

    assert proj.config is not None
    current = list(proj.config.mcp.enabled)
    removed: list[str] = []
    for mcp in mcps:
        if mcp in current:
            current.remove(mcp)
            removed.append(mcp)

    new_cfg = state.ProjectConfig(
        schema_version=proj.config.schema_version,
        project=proj.config.project,
        mcp=state.ProjectMcp(enabled=current),
    )
    assert proj.marker_path is not None
    state.save_project_config(new_cfg, proj.marker_path)
    _refresh_index(proj.name, proj.root)

    if removed:
        click.echo(f"Disabled in '{proj.name}': {', '.join(removed)}")
    else:
        click.echo(f"No changes to '{proj.name}' (none of {list(mcps)} were enabled).")
