"""W1 acceptance test — RFC §12 (Path B + common path).

Path A (`mms add filesystem ... → registry`) is **out of W1 scope** per
the §12 banner added in `memtomem-docs#31`. The integration of
`mms add` and `mms import` is deferred to a W2+ trigger-driven RFC
follow-up (§14 W2-Q2). This test exercises only Path B + the common
path:

1. Seed a fake `~/.claude.json` with two MCP entries (one with secret env).
2. `mms import --from claude-code --plan` — verify secret REDACTED in stdout.
3. `mms import --from claude-code --apply` — verify both entries land in
   `~/.mms/registry.toml` with secret intact.
4. Two projects (`proj-a`, `proj-b`) — `init` each, different `enable` lists.
5. `mms project list --json` — both projects, current cwd marked.
6. `mms project show` from proj-a — enabled list correct, source = marker.
7. Re-run `mms import --apply` — idempotent, "Already up to date."

Sandboxed via `set_home(monkeypatch, tmp_path)` — patches HOME and
USERPROFILE so `Path.home()` is hermetic on every platform. The
registry, projects index, and per-project markers all land in tmp.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.mms_import import import_command
from memtomem_stm.cli.mms_project import project_group
from memtomem_stm.mms import state
from helpers import set_home


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_w1_acceptance_path_b_and_common_path(runner, tmp_path, monkeypatch):
    """Full §12 walk-through. Tagged via the test name; runs in CI default."""
    # ── Step 0: sandbox HOME, chdir into a clean dir, seed fake claude-code ────
    set_home(monkeypatch, tmp_path)
    # The import scanner's claude-code path also reads <cwd>/.mcp.json; make sure
    # we're not standing in the actual repo (which has no .mcp.json today, but
    # any future commit that adds one would silently inflate this test's count).
    sandbox_cwd = tmp_path / "_initial_cwd"
    sandbox_cwd.mkdir()
    monkeypatch.chdir(sandbox_cwd)

    seeded = {
        "mcpServers": {
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", str(tmp_path)],
            },
            "github": {
                "command": "npx",
                "args": ["-y", "@mcp/github"],
                "env": {"GITHUB_TOKEN": "ghp_realsecret_value"},
            },
        }
    }
    (tmp_path / ".claude.json").write_text(json.dumps(seeded), encoding="utf-8")

    # ── Step 1: --plan — secret REDACTED ──────────────────────────────
    res = runner.invoke(import_command, ["--from", "claude-code"])
    assert res.exit_code == 0, res.output
    assert "ghp_realsecret_value" not in res.output, "secret leaked in --plan"
    assert "<REDACTED>" in res.output
    assert "filesystem" in res.output
    assert "github" in res.output
    # Registry not yet written.
    assert not state.registry_path().exists()

    # ── Step 2: --apply — registry holds both, secret intact ──────────
    res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
    assert res.exit_code == 0, res.output
    assert "Wrote 2 new entries" in res.output

    registry = state.load_registry()
    assert set(registry.servers.keys()) == {"filesystem", "github"}
    assert registry.servers["github"].env == {"GITHUB_TOKEN": "ghp_realsecret_value"}

    # ── Step 3: two projects, init + enable ───────────────────────────
    proj_a = tmp_path / "proj-a"
    proj_a.mkdir()
    proj_b = tmp_path / "proj-b"
    proj_b.mkdir()

    monkeypatch.chdir(proj_a)
    res = runner.invoke(project_group, ["init"])
    assert res.exit_code == 0, res.output
    res = runner.invoke(project_group, ["enable", "filesystem", "github"])
    assert res.exit_code == 0, res.output

    monkeypatch.chdir(proj_b)
    res = runner.invoke(project_group, ["init"])
    assert res.exit_code == 0, res.output
    res = runner.invoke(project_group, ["enable", "filesystem"])
    assert res.exit_code == 0, res.output

    # ── Step 4: list --json → both projects, current marked ───────────
    monkeypatch.chdir(proj_a)
    res = runner.invoke(project_group, ["list", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert len(payload["projects"]) == 2
    by_name = {p["name"]: p for p in payload["projects"]}
    assert "proj-a" in by_name
    assert "proj-b" in by_name
    assert by_name["proj-a"]["current"] is True
    assert by_name["proj-b"]["current"] is False

    # ── Step 5: show from proj-a → marker source, both MCPs enabled ───
    res = runner.invoke(project_group, ["show", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["source"] == "marker"
    assert payload["name"] == "proj-a"
    assert payload["enabled"] == ["filesystem", "github"]

    # ── Step 6: show from proj-b → only filesystem ────────────────────
    monkeypatch.chdir(proj_b)
    res = runner.invoke(project_group, ["show", "--json"])
    payload = json.loads(res.output)
    assert payload["source"] == "marker"
    assert payload["enabled"] == ["filesystem"]

    # ── Step 7: re-run import --apply → idempotent ────────────────────
    res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
    assert res.exit_code == 0, res.output
    assert "Already up to date" in res.output

    # Registry unchanged — same two entries, same secret.
    registry_after = state.load_registry()
    assert registry_after == registry


def test_w1_acceptance_marker_walk_up_from_subdirectory(runner, tmp_path, monkeypatch):
    """RFC §6 — `mms project show` from a subdirectory finds the parent marker."""
    set_home(monkeypatch, tmp_path)

    project_root = tmp_path / "monorepo"
    project_root.mkdir()
    monkeypatch.chdir(project_root)
    runner.invoke(project_group, ["init", "--name", "monorepo"])

    sub = project_root / "packages" / "pkg-a"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)

    res = runner.invoke(project_group, ["show", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["source"] == "marker"
    assert payload["name"] == "monorepo"
    assert Path(payload["root"]) == project_root.resolve()


def test_w1_acceptance_empty_registry_friendly_error(runner, tmp_path, monkeypatch):
    """PR1's graceful contract surfaces in the W1 acceptance test —
    a user who runs enable before importing gets a friendly hint."""
    set_home(monkeypatch, tmp_path)

    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    runner.invoke(project_group, ["init"])

    res = runner.invoke(project_group, ["enable", "filesystem"])
    assert res.exit_code != 0
    assert "Registry is empty" in res.output
    assert "mms import --from" in res.output
