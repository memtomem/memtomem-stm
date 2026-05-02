"""CliRunner tests for ``mms host status`` (RFC §7.3, W2 PR3).

The four-state classifier (``unchanged`` / ``changed`` / ``removed_at_host``
/ ``no_baseline``) is the contract that PR4 builds on, so every state
gets explicit coverage here. ``no_baseline`` and the
``drift_hash_version`` guard are reachable via real edit paths
(manual sidecar edit, pre-PR2 install) — not just defensive code, so
each gets a dedicated test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.mms_host import host_group
from memtomem_stm.cli.mms_import import import_command
from memtomem_stm.mms import state
from memtomem_stm.mms.drift import HASH_VERSION, compute_drift_hash


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Pin HOME so ``~/.mms`` and ``~/.claude.json`` etc. land in tmp."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return {"home": tmp_path, "cwd": cwd}


def _seed_claude_code(sandbox, mcps: dict) -> None:
    cfg = {"mcpServers": mcps}
    (sandbox["home"] / ".claude.json").write_text(json.dumps(cfg), encoding="utf-8")


def _seed_cursor_user(sandbox, mcps: dict) -> None:
    cursor_dir = sandbox["home"] / ".cursor"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    (cursor_dir / "mcp.json").write_text(json.dumps({"mcpServers": mcps}), encoding="utf-8")


def _apply_claude_code(runner) -> None:
    res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
    assert res.exit_code == 0, res.output


def _status(runner, *args: str):
    return runner.invoke(host_group, ["status", *args])


def _scan(runner, *args: str):
    return runner.invoke(host_group, ["scan", *args])


# ---------------------------------------------------------------------------
# Core states
# ---------------------------------------------------------------------------


class TestStates:
    def test_status_after_apply_all_unchanged(self, runner, sandbox):
        _seed_claude_code(
            sandbox,
            {
                "filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]},
                "github": {"command": "npx"},
            },
        )
        _apply_claude_code(runner)

        res = _status(runner)
        assert res.exit_code == 0, res.output
        assert "filesystem" in res.output
        assert "github" in res.output
        # Footer count fragments — substring assertions, joining
        # punctuation/order intentionally not pinned (PR4 may evolve
        # the line shape).
        assert "2 unchanged" in res.output
        assert "0 changed at host" in res.output
        # Nothing outside the comparable bucket → no extra footer notes.
        assert "not present" not in res.output
        assert "missing baseline" not in res.output

    def test_status_changed_after_host_edit(self, runner, sandbox):
        _seed_claude_code(
            sandbox,
            {
                "filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]},
                "github": {"command": "npx"},
            },
        )
        _apply_claude_code(runner)

        # Mutate one entry's args at the host config layer.
        _seed_claude_code(
            sandbox,
            {
                "filesystem": {"command": "npx", "args": ["-y", "@mcp/fs2"]},
                "github": {"command": "npx"},
            },
        )

        res = _status(runner)
        assert res.exit_code == 0, res.output
        # filesystem is changed; github stays unchanged.
        # Use the JSON shape for unambiguous state assertions.
        json_res = _status(runner, "--json")
        assert json_res.exit_code == 0, json_res.output
        payload = json.loads(json_res.output)
        states = {row["name"]: row["state"] for row in payload["entries"]}
        assert states == {"filesystem": "changed", "github": "unchanged"}
        assert "1 unchanged" in res.output
        assert "1 changed at host" in res.output

    def test_status_removed_at_host(self, runner, sandbox):
        _seed_claude_code(
            sandbox,
            {
                "filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]},
                "github": {"command": "npx"},
            },
        )
        _apply_claude_code(runner)

        # Drop one entry at the host config — registry/sidecar still
        # have it.
        _seed_claude_code(sandbox, {"github": {"command": "npx"}})

        res = _status(runner)
        assert res.exit_code == 0, res.output
        # Footer count + neutral phrasing (no PR-planning vocab leak).
        # Backwards-compat anchor — pinned across PR4's main-table promotion.
        assert "1 entry in registry not present in any host scan" in res.output
        # PR4: removed_at_host now renders as a main-table row, not only a
        # footer count. Pin the row at line level so a future text-format
        # refactor that splits NAME and STATE across lines (or drops SOURCE)
        # fails this test loudly.
        lines = res.output.splitlines()
        removed_row = next((line for line in lines if "removed_at_host" in line), None)
        assert removed_row is not None, res.output
        assert "filesystem" in removed_row
        assert "Claude Code (user)" in removed_row

        json_res = _status(runner, "--json")
        payload = json.loads(json_res.output)
        states = {row["name"]: row["state"] for row in payload["entries"]}
        assert states == {"filesystem": "removed_at_host", "github": "unchanged"}
        # current_hash is null for removed entries.
        for row in payload["entries"]:
            if row["name"] == "filesystem":
                assert row["current_hash"] is None
                assert row["baseline_hash"] is not None  # baseline preserved
                assert row["source_label"] == "Claude Code (user)"

    def test_status_only_removed_at_host_renders_table(self, runner, sandbox):
        """PR4 reachable path: only ``removed_at_host`` rows, zero
        ``unchanged`` / ``changed``. The table must still render
        (header + each removed row); footer summary + removed-at-host
        count still appear. Guards against a future refactor that
        re-narrows the ``if main_rows:`` guard to comparable states
        only — that would break PR4's promotion silently.
        """
        _seed_claude_code(
            sandbox,
            {"a": {"command": "npx"}, "b": {"command": "npx"}},
        )
        _apply_claude_code(runner)
        # Drop both entries from the host config — registry/sidecar still have them.
        _seed_claude_code(sandbox, {})

        res = _status(runner)
        assert res.exit_code == 0, res.output
        # Header rendered (table not suppressed by the if-guard).
        assert "NAME" in res.output
        assert "STATE" in res.output
        assert "SOURCE" in res.output
        # Both rows present in the body, each on its own line.
        # 2 table rows; the footer ("...not present in any host scan")
        # doesn't contain "removed_at_host".
        lines = res.output.splitlines()
        removed_lines = [line for line in lines if "removed_at_host" in line]
        assert len(removed_lines) == 2, res.output
        assert any(" a " in line for line in removed_lines)
        assert any(" b " in line for line in removed_lines)
        # Summary + footer still appear.
        assert "0 unchanged, 0 changed at host" in res.output
        assert "2 entries in registry not present in any host scan" in res.output

    def test_status_no_baseline_missing_sidecar_entry(self, runner, sandbox):
        """Manual sidecar deletion / pre-PR2 install / atomic-write race —
        any path that leaves a registry entry without a sidecar row.
        Read-only inspection must not crash; it must surface the row in
        a recoverable bucket with an actionable hint.
        """
        _seed_claude_code(
            sandbox,
            {
                "filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]},
                "github": {"command": "npx"},
            },
        )
        _apply_claude_code(runner)

        # Manually delete one sidecar entry.
        s = state.load_import_state()
        new_entries = {k: v for k, v in s.entries.items() if k != "github"}
        state.save_import_state(
            state.ImportState(schema_version=s.schema_version, entries=new_entries)
        )

        res = _status(runner)
        assert res.exit_code == 0, res.output
        # filesystem is still unchanged; github falls into no_baseline.
        assert "1 unchanged" in res.output
        assert "0 changed at host" in res.output
        assert "1 entry missing baseline hash" in res.output
        assert "run `mms import --apply` to stamp" in res.output

        json_res = _status(runner, "--json")
        payload = json.loads(json_res.output)
        states = {row["name"]: row["state"] for row in payload["entries"]}
        assert states == {"filesystem": "unchanged", "github": "no_baseline"}
        for row in payload["entries"]:
            if row["name"] == "github":
                assert row["baseline_hash"] is None
                assert row["source_label"] is None
                assert row["last_imported"] is None
                # current_hash reflects the host's view — github is
                # still in the host config, so the host candidate's
                # canonical hash is computable.
                assert row["current_hash"] is not None

    def test_status_no_baseline_drift_hash_version_mismatch(self, runner, sandbox):
        """Future HASH_VERSION bump — v1 baselines vs v2 binary should
        not silently miscompare as ``changed``. PR3 is the first
        consumer; if it doesn't gate on version, no later consumer will.
        """
        _seed_claude_code(sandbox, {"filesystem": {"command": "npx"}})
        _apply_claude_code(runner)

        # Stamp the sidecar with a wildly different version — hash
        # itself is real (we don't break the regex) but the version
        # disagrees with the running mms's HASH_VERSION.
        s = state.load_import_state()
        entry = s.entries["filesystem"]
        s.entries["filesystem"] = state.ImportStateEntry(
            drift_hash=entry.drift_hash,
            drift_hash_version=HASH_VERSION + 998,
            last_imported=entry.last_imported,
            source_label=entry.source_label,
        )
        state.save_import_state(s)

        json_res = _status(runner, "--json")
        assert json_res.exit_code == 0, json_res.output
        payload = json.loads(json_res.output)
        assert payload["entries"][0]["state"] == "no_baseline"


# ---------------------------------------------------------------------------
# source_label-preferred candidate matching
# ---------------------------------------------------------------------------


class TestSourceLabelMatching:
    def test_status_source_label_match_preferred(self, runner, sandbox):
        """Same-name entry exists in two hosts with different shapes;
        comparison candidate must be the one whose source_label matches
        the sidecar baseline (= the import-time host).
        """
        # Seed both claude-code and cursor with the same name, different args.
        _seed_claude_code(
            sandbox,
            {"shared": {"command": "npx", "args": ["claude-args"]}},
        )
        _seed_cursor_user(
            sandbox,
            {"shared": {"command": "npx", "args": ["cursor-args"]}},
        )

        # Import only from cursor → registry + sidecar baseline carry
        # source_label="Cursor (user)" and the cursor-args canonical hash.
        res_apply = runner.invoke(import_command, ["--from", "cursor", "--apply"])
        assert res_apply.exit_code == 0, res_apply.output
        sidecar = state.load_import_state()
        assert sidecar.entries["shared"].source_label == "Cursor (user)"

        # Now mms host status discovers ALL hosts. ALL_HOSTS iterates
        # claude-code first (different shape). Without source_label
        # preference, the comparison would pick the claude-code
        # candidate and report changed (false positive).
        json_res = _status(runner, "--json")
        payload = json.loads(json_res.output)
        states = {row["name"]: row["state"] for row in payload["entries"]}
        assert states == {"shared": "unchanged"}, payload

    def test_status_source_label_fallback_to_first_match(self, runner, sandbox):
        """Sidecar source_label points at a host that no longer has the
        entry; fallback to first-seen ALL_HOSTS match. Behavior pin —
        not necessarily ideal long-term but stable today.
        """
        _seed_cursor_user(sandbox, {"shared": {"command": "npx", "args": ["cursor-args"]}})
        res_apply = runner.invoke(import_command, ["--from", "cursor", "--apply"])
        assert res_apply.exit_code == 0, res_apply.output
        # source_label baseline = "Cursor (user)"
        assert state.load_import_state().entries["shared"].source_label == "Cursor (user)"

        # Now move the entry to claude-code only (cursor config gone).
        (sandbox["home"] / ".cursor" / "mcp.json").unlink()
        _seed_claude_code(sandbox, {"shared": {"command": "npx", "args": ["cursor-args"]}})

        # Same canonical args → fallback candidate (Claude Code (user))
        # has matching hash, so state is unchanged.
        json_res = _status(runner, "--json")
        payload = json.loads(json_res.output)
        states = {row["name"]: row["state"] for row in payload["entries"]}
        assert states == {"shared": "unchanged"}

    def test_status_source_column_shows_baseline_attribution(self, runner, sandbox):
        """SOURCE column = baseline.source_label (import-time), NOT
        current host. User imports from claude-code, then later mirrors
        the same entry into cursor — SOURCE stays "Claude Code (user)".
        """
        _seed_claude_code(sandbox, {"shared": {"command": "npx"}})
        _apply_claude_code(runner)
        # Mirror to cursor with same shape (would be unchanged via fallback).
        _seed_cursor_user(sandbox, {"shared": {"command": "npx"}})

        json_res = _status(runner, "--json")
        payload = json.loads(json_res.output)
        assert len(payload["entries"]) == 1
        assert payload["entries"][0]["source_label"] == "Claude Code (user)"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdges:
    def test_status_malformed_host_config_exit_0(self, runner, sandbox):
        """Scanners catch parse errors at the import_hosts level
        (import_hosts.py:_read_json_safely treats unreadable as "no
        candidates"). PR3 must inherit that behavior — a malformed
        host config is the same as missing host config, not a crash.
        Pins the exit-0 contract against a future scanner refactor
        that drops the catch.
        """
        _seed_claude_code(sandbox, {"x": {"command": "npx"}})
        _apply_claude_code(runner)
        # Now corrupt the host config — invalid JSON.
        (sandbox["home"] / ".claude.json").write_text("{ this is not valid json", encoding="utf-8")

        for args in ([], ["--json"]):
            res = _status(runner, *args)
            assert res.exit_code == 0, (args, res.output)

    def test_status_empty_registry(self, runner, sandbox):
        res = _status(runner)
        assert res.exit_code == 0, res.output
        assert (
            "No registered MCP entries. Run `mms import --apply` to import host configs."
            in res.output
        )

    def test_status_empty_registry_json(self, runner, sandbox):
        res = _status(runner, "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload == {
            "entries": [],
            "summary": {
                "unchanged": 0,
                "changed": 0,
                "removed_at_host": 0,
                "no_baseline": 0,
            },
        }


# ---------------------------------------------------------------------------
# JSON shape & invariants
# ---------------------------------------------------------------------------


class TestJsonShape:
    def test_status_json_shape_includes_all_states(self, runner, sandbox):
        """Seed one row per state; --json entries must include all four,
        summary must always carry the four keys."""
        # Two claude-code entries + one to be hand-edited into mismatch.
        _seed_claude_code(
            sandbox,
            {
                "fs_unchanged": {"command": "npx", "args": ["-y", "@mcp/fs"]},
                "gh_changed": {"command": "npx", "args": ["original"]},
                "to_remove": {"command": "npx"},
                "no_base_entry": {"command": "npx"},
            },
        )
        _apply_claude_code(runner)

        # Construct each state:
        # - fs_unchanged: leave alone → unchanged
        # - gh_changed: edit host args → changed
        # - to_remove: drop from host config → removed_at_host
        # - no_base_entry: delete sidecar row → no_baseline
        _seed_claude_code(
            sandbox,
            {
                "fs_unchanged": {"command": "npx", "args": ["-y", "@mcp/fs"]},
                "gh_changed": {"command": "npx", "args": ["mutated"]},
                "no_base_entry": {"command": "npx"},
            },
        )
        s = state.load_import_state()
        new_entries = {k: v for k, v in s.entries.items() if k != "no_base_entry"}
        state.save_import_state(
            state.ImportState(schema_version=s.schema_version, entries=new_entries)
        )

        res = _status(runner, "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)

        # Summary always carries the four keys, all integers.
        assert set(payload["summary"].keys()) == {
            "unchanged",
            "changed",
            "removed_at_host",
            "no_baseline",
        }
        assert payload["summary"] == {
            "unchanged": 1,
            "changed": 1,
            "removed_at_host": 1,
            "no_baseline": 1,
        }

        # entries[] includes all four states.
        states_seen = {row["name"]: row["state"] for row in payload["entries"]}
        assert states_seen == {
            "fs_unchanged": "unchanged",
            "gh_changed": "changed",
            "to_remove": "removed_at_host",
            "no_base_entry": "no_baseline",
        }

        # Required keys per row.
        required = {
            "name",
            "state",
            "source_label",
            "baseline_hash",
            "current_hash",
            "last_imported",
        }
        for row in payload["entries"]:
            assert set(row.keys()) == required, row

    def test_status_exit_code_always_zero(self, runner, sandbox):
        """Every state combination — drift / removed / no_baseline —
        must exit 0. Read-only observation never signals failure.
        """
        _seed_claude_code(
            sandbox,
            {"a": {"command": "npx", "args": ["x"]}, "b": {"command": "npx"}},
        )
        _apply_claude_code(runner)
        # Edit host (changed) + remove one (removed_at_host) + drop sidecar
        # row for the remaining (no_baseline).
        _seed_claude_code(sandbox, {"a": {"command": "npx", "args": ["edited"]}})
        s = state.load_import_state()
        s.entries.pop("a")  # baseline gone for the only host-present entry → no_baseline
        state.save_import_state(s)

        for args in ([], ["--json"]):
            res = _status(runner, *args)
            assert res.exit_code == 0, (args, res.output)


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


class TestPlumbing:
    def test_baseline_hash_matches_canonical(self, runner, sandbox):
        """The JSON ``baseline_hash`` is exactly what the canonical
        compute_drift_hash() returns for the registry entry — so
        downstream tooling can recompute and match without surprises.
        """
        _seed_claude_code(sandbox, {"x": {"command": "npx", "args": ["a", "b"]}})
        _apply_claude_code(runner)

        json_res = _status(runner, "--json")
        payload = json.loads(json_res.output)
        registry = state.load_registry()
        assert payload["entries"][0]["baseline_hash"] == compute_drift_hash(registry.servers["x"])
        assert payload["entries"][0]["current_hash"] == compute_drift_hash(registry.servers["x"])

    def test_current_hash_is_host_view_across_buckets(self, runner, sandbox):
        """``current_hash`` means "the host's canonical hash, or null
        if no host has the entry" in every bucket — including
        ``no_baseline``. Without this, a script that recomputes
        compute_drift_hash() against the live host config would match
        for unchanged/changed but diverge for no_baseline whenever
        registry and host differ (which is the most likely state when
        the sidecar is stale).
        """
        # Apply with the canonical shape, then mutate the host to a
        # different shape *and* drop the sidecar baseline. The registry
        # still holds the original shape; the host now holds the new.
        _seed_claude_code(sandbox, {"x": {"command": "npx", "args": ["original"]}})
        _apply_claude_code(runner)
        _seed_claude_code(sandbox, {"x": {"command": "npx", "args": ["edited"]}})
        s = state.load_import_state()
        s.entries.pop("x")
        state.save_import_state(s)

        json_res = _status(runner, "--json")
        payload = json.loads(json_res.output)
        row = payload["entries"][0]
        assert row["state"] == "no_baseline"

        # current_hash must reflect the *host's* current canonical form
        # (the edited shape), not the registry entry. Recomputing
        # against the host wire gives the same value back.
        host_view = compute_drift_hash(
            state.RegistryServer(
                command="npx",
                args=["edited"],
                env={},
                prefix="x",
            )
        )
        registry_view = compute_drift_hash(state.load_registry().servers["x"])
        assert row["current_hash"] == host_view
        assert row["current_hash"] != registry_view  # the asymmetry the reviewer flagged

    def test_current_hash_null_when_host_absent(self, runner, sandbox):
        """When no host scanner finds the entry (``removed_at_host`` or
        no_baseline-with-no-host), ``current_hash`` is null — the host
        view is the empty set.
        """
        _seed_claude_code(sandbox, {"x": {"command": "npx"}})
        _apply_claude_code(runner)
        # Drop the entry from the host AND drop the sidecar row, so the
        # row would be no_baseline (sidecar gate fires before host gate)
        # but with no host candidate to compute current_hash against.
        _seed_claude_code(sandbox, {})
        s = state.load_import_state()
        s.entries.pop("x")
        state.save_import_state(s)

        json_res = _status(runner, "--json")
        payload = json.loads(json_res.output)
        row = payload["entries"][0]
        assert row["state"] == "no_baseline"
        assert row["current_hash"] is None

    def test_status_does_not_write_anything(self, runner, sandbox):
        """Read-only invariant — running status N times must not
        mutate registry or sidecar (mtimes / contents stable).
        """
        _seed_claude_code(sandbox, {"x": {"command": "npx"}})
        _apply_claude_code(runner)

        registry_before = Path(state.registry_path()).read_bytes()
        sidecar_before = Path(state.import_state_path()).read_bytes()
        for _ in range(3):
            res = _status(runner)
            assert res.exit_code == 0
            res_json = _status(runner, "--json")
            assert res_json.exit_code == 0
        assert Path(state.registry_path()).read_bytes() == registry_before
        assert Path(state.import_state_path()).read_bytes() == sidecar_before


# ---------------------------------------------------------------------------
# scan — host-side discovery surface (W2 PR5)
# ---------------------------------------------------------------------------


class TestScan:
    def test_scan_default_shows_all_hosts(self, runner, sandbox):
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _seed_cursor_user(sandbox, {"b": {"command": "npx"}})

        res = _scan(runner)
        assert res.exit_code == 0, res.output
        assert " a " in res.output
        assert " b " in res.output
        assert "Claude Code (user)" in res.output
        assert "Cursor (user)" in res.output
        assert "2 entries across 2 hosts" in res.output

    def test_scan_in_registry_yes_after_apply(self, runner, sandbox):
        _seed_claude_code(sandbox, {"filesystem": {"command": "npx"}})
        _apply_claude_code(runner)

        res = _scan(runner)
        assert res.exit_code == 0, res.output
        # Find the row line; assert it ends with Yes (IN_REGISTRY column).
        lines = res.output.splitlines()
        row = next((line for line in lines if "filesystem" in line and "Claude Code" in line), None)
        assert row is not None, res.output
        assert row.rstrip().endswith("Yes"), row
        assert "1 in registry, 0 new at host" in res.output

        json_res = _scan(runner, "--json")
        payload = json.loads(json_res.output)
        assert payload["entries"][0]["in_registry"] is True
        assert payload["summary"] == {"total": 1, "in_registry": 1, "new_at_host": 0}

    def test_scan_in_registry_no_for_orphan(self, runner, sandbox):
        # Cursor entry, never imported.
        _seed_cursor_user(sandbox, {"shadcn": {"command": "npx"}})

        res = _scan(runner)
        assert res.exit_code == 0, res.output
        lines = res.output.splitlines()
        row = next((line for line in lines if "shadcn" in line), None)
        assert row is not None, res.output
        assert row.rstrip().endswith("No"), row
        assert "0 in registry, 1 new at host" in res.output

    def test_scan_same_name_multi_host_shows_both(self, runner, sandbox):
        """Locked-in #1: full inventory. ``mms host status`` does
        first-match-wins for its registry-comparison axis; ``scan`` has
        no such axis and shows every host occurrence — intentional
        divergence so users see real-world same-name mirroring.
        """
        _seed_claude_code(sandbox, {"filesystem": {"command": "npx", "args": ["-y", "@a/fs"]}})
        _seed_cursor_user(sandbox, {"filesystem": {"command": "npx", "args": ["-y", "@b/fs"]}})

        res = _scan(runner)
        assert res.exit_code == 0, res.output
        # Two text rows for the same name (one per host).
        rows = [line for line in res.output.splitlines() if " filesystem " in line]
        assert len(rows) == 2, res.output
        assert any("Claude Code (user)" in line for line in rows)
        assert any("Cursor (user)" in line for line in rows)

        json_res = _scan(runner, "--json")
        payload = json.loads(json_res.output)
        fs_entries = [e for e in payload["entries"] if e["name"] == "filesystem"]
        assert len(fs_entries) == 2, payload
        assert payload["summary"]["total"] == 2

    def test_scan_from_filter_single_host(self, runner, sandbox):
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _seed_cursor_user(sandbox, {"b": {"command": "npx"}})

        res = _scan(runner, "--from", "cursor")
        assert res.exit_code == 0, res.output
        assert " b " in res.output
        assert " a " not in res.output  # claude-code excluded
        assert "Claude Code (user)" not in res.output
        # Pin both singular forms — `entry`/`host` (not `entries`/`hosts`).
        assert "1 entry across 1 host" in res.output

    def test_scan_from_invalid_host_errors(self, runner, sandbox):
        res = _scan(runner, "--from", "foo")
        # click.Choice rejects with UsageError → exit code 2.
        assert res.exit_code == 2, res.output
        assert "foo" in res.output
        assert "claude-code" in res.output  # choices listed in error

    def test_scan_empty_no_host_configs(self, runner, sandbox):
        res = _scan(runner)
        assert res.exit_code == 0, res.output
        assert "No MCP entries discovered across host configs." in res.output

    def test_scan_empty_no_host_configs_json(self, runner, sandbox):
        res = _scan(runner, "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        # Locked-in #4: 3-key always-emit summary, zero counts explicit.
        assert payload == {
            "entries": [],
            "summary": {"total": 0, "in_registry": 0, "new_at_host": 0},
        }

    def test_scan_empty_filtered_host_message_scoped(self, runner, sandbox):
        """``--from cursor`` with cursor empty (other hosts populated)
        must say ``in cursor``, not the misleading ``across host
        configs``. JSON shape unaffected (always 3-key summary).
        """
        _seed_claude_code(sandbox, {"only-claude-code-entry": {"command": "npx"}})

        res = _scan(runner, "--from", "cursor")
        assert res.exit_code == 0, res.output
        assert "No MCP entries discovered in cursor." in res.output
        assert "across host configs" not in res.output

        json_res = _scan(runner, "--from", "cursor", "--json")
        assert json_res.exit_code == 0, json_res.output
        assert json.loads(json_res.output) == {
            "entries": [],
            "summary": {"total": 0, "in_registry": 0, "new_at_host": 0},
        }

    def test_scan_from_filter_case_insensitive(self, runner, sandbox):
        """``--from CLAUDE-CODE`` works the same as ``--from
        claude-code`` — symmetric with ``mms import --from`` UX. Pins
        the ``case_sensitive=False`` derivation from ``ALL_HOSTS``.
        """
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})

        res = _scan(runner, "--from", "CLAUDE-CODE")
        assert res.exit_code == 0, res.output
        assert " a " in res.output
        assert "Claude Code (user)" in res.output

    def test_scan_json_shape(self, runner, sandbox):
        _seed_claude_code(sandbox, {"registered": {"command": "npx"}})
        _apply_claude_code(runner)
        # ``mms import --apply`` is read-only on host configs (project
        # invariant) — the claude-code seed survives the cursor seed and
        # the apply, so no re-seed needed.
        _seed_cursor_user(sandbox, {"orphan": {"command": "npx"}})

        res = _scan(runner, "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)

        # Per-entry required keys (locked-in shape).
        required = {"name", "host", "in_registry"}
        for entry in payload["entries"]:
            assert set(entry.keys()) == required, entry

        # Summary keys (locked-in #4).
        assert set(payload["summary"].keys()) == {"total", "in_registry", "new_at_host"}

        states = {e["name"]: e["in_registry"] for e in payload["entries"]}
        assert states == {"registered": True, "orphan": False}
        assert payload["summary"] == {"total": 2, "in_registry": 1, "new_at_host": 1}

    def test_scan_in_registry_name_match_only_not_shape(self, runner, sandbox):
        """Locked-in #2: ``IN_REGISTRY`` is a name match only. Shape
        comparison is delegated to ``mms host status`` — registered
        entry whose host shape differs still shows ``Yes`` here.
        """
        _seed_claude_code(sandbox, {"filesystem": {"command": "npx", "args": ["-y", "@a/fs"]}})
        _apply_claude_code(runner)

        # Mutate the host config — same name, different args → "changed"
        # in status's vocab. scan should still report Yes.
        _seed_claude_code(sandbox, {"filesystem": {"command": "npx", "args": ["-y", "@b/fs"]}})

        res = _scan(runner, "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["entries"][0]["in_registry"] is True

        # Cross-check: status would call this `changed`, demonstrating the
        # contrast. scan's `IN_REGISTRY=Yes` and status's `changed` are
        # both true at once — they're orthogonal axes, not a contradiction.
        status_res = _status(runner, "--json")
        status_payload = json.loads(status_res.output)
        assert status_payload["entries"][0]["state"] == "changed"

    def test_scan_does_not_write_anything(self, runner, sandbox):
        """Read-only invariant. Mirrors ``test_status_does_not_write_anything``."""
        _seed_claude_code(sandbox, {"a": {"command": "npx"}, "b": {"command": "npx"}})
        _apply_claude_code(runner)
        _seed_cursor_user(sandbox, {"c": {"command": "npx"}})

        registry_before = Path(state.registry_path()).read_bytes()
        sidecar_before = Path(state.import_state_path()).read_bytes()

        for args in ([], ["--json"], ["--from", "cursor"], ["--from", "claude-code", "--json"]):
            res = _scan(runner, *args)
            assert res.exit_code == 0, (args, res.output)

        assert Path(state.registry_path()).read_bytes() == registry_before
        assert Path(state.import_state_path()).read_bytes() == sidecar_before


# ---------------------------------------------------------------------------
# sync — write-back surface (W2 PR6)
# ---------------------------------------------------------------------------


def _sync(runner, *args: str, **kwargs):
    return runner.invoke(host_group, ["sync", *args], **kwargs)


def _force_tty(monkeypatch) -> None:
    """Make ``_is_interactive()`` return True under CliRunner.

    CliRunner replaces ``sys.stdin`` with a non-TTY stream, so without
    this monkeypatch the TTY-confirm code path is unreachable from
    tests.
    """
    from memtomem_stm.cli import mms_host

    monkeypatch.setattr(mms_host, "_is_interactive", lambda: True)


class TestSyncPlan:
    def test_plan_no_op_when_in_sync(self, runner, sandbox):
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)

        res = _sync(runner, "--plan", "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["mode"] == "plan"
        assert payload["aborted"] is False
        assert all(payload["plan"][k] == [] for k in payload["plan"])
        assert payload["summary"] == {
            "added": 0,
            "removed": 0,
            "backfilled": 0,
            "cleanup": 0,
            "skipped_changed": 0,
            "orphan_no_baseline": 0,
            "restamped": 0,
            "unchanged": 1,
            "conflicts": 0,
        }

    def test_plan_add_redacts_secrets_by_default(self, runner, sandbox):
        # Seed an entry with a secret-classified env key (key contains "TOKEN").
        _seed_claude_code(
            sandbox,
            {"svc": {"command": "npx", "env": {"GITHUB_TOKEN": "ghp_supersecret"}}},
        )

        res = _sync(runner, "--plan")
        assert res.exit_code == 0, res.output
        # The secret value should not appear verbatim.
        assert "ghp_supersecret" not in res.output
        # The hint pointing at `mms import --plan --show-imported` must
        # be visible — sync deliberately omits its own --show-imported.
        assert "mms import --plan --show-imported" in res.output

    def test_plan_does_not_write(self, runner, sandbox):
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)
        _seed_cursor_user(sandbox, {"b": {"command": "npx"}})

        registry_before = Path(state.registry_path()).read_bytes()
        sidecar_before = Path(state.import_state_path()).read_bytes()

        for args in (["--plan"], ["--plan", "--json"]):
            res = _sync(runner, *args)
            assert res.exit_code == 0, (args, res.output)

        assert Path(state.registry_path()).read_bytes() == registry_before
        assert Path(state.import_state_path()).read_bytes() == sidecar_before

    def test_plan_changed_footer_text(self, runner, sandbox):
        _seed_claude_code(sandbox, {"a": {"command": "npx", "args": ["v1"]}})
        _apply_claude_code(runner)
        # Mutate host shape — entry surfaces as ``changed``.
        _seed_claude_code(sandbox, {"a": {"command": "npx", "args": ["v2"]}})

        res = _sync(runner, "--plan")
        assert res.exit_code == 0, res.output
        # The pointer footer fires when ``changed`` count > 0 and now
        # references a real flag (W3 shipped ``--force``).
        assert "differ in shape at host" in res.output
        assert "mms host sync --force" in res.output
        # The ``(W3+)`` qualifier is gone — `--force` is now a real
        # working flag, not a future promise.
        assert "(W3+)" not in res.output

    def test_plan_orphan_no_baseline_footer(self, runner, sandbox):
        """no_baseline + no matched candidate → footer surfaces it.

        Reachable path: host config drops the entry AND sidecar lacks
        a baseline (e.g. import_state.toml hand-edited). Sync can't
        safely act, but silence is the failure mode (Lock-down 3
        siblings) — surface as a footer note.
        """
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)
        # Delete the sidecar baseline AND drop the host entry.
        s = state.load_import_state()
        s.entries.pop("a")
        state.save_import_state(s)
        _seed_claude_code(sandbox, {})

        res = _sync(runner, "--plan")
        assert res.exit_code == 0, res.output
        assert "in registry without baseline and not at any host scan" in res.output

    def test_plan_orphan_no_baseline_in_json_summary(self, runner, sandbox):
        """JSON parity with the text footer: ``orphan_no_baseline``
        surfaces in both ``summary`` (count) and ``plan`` (per-entry
        list). Without this, JSON consumers miss what text users see.
        """
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)
        s = state.load_import_state()
        s.entries.pop("a")
        state.save_import_state(s)
        _seed_claude_code(sandbox, {})

        res = _sync(runner, "--plan", "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["summary"]["orphan_no_baseline"] == 1
        assert payload["plan"]["orphan_no_baseline"] == [{"name": "a"}]

    def test_plan_aborted_field_always_false_in_plan_mode(self, runner, sandbox):
        # Seed REMOVE-triggering state so the bucket would be visible
        # in apply mode; --plan must still emit aborted=False.
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)
        _seed_claude_code(sandbox, {})

        res = _sync(runner, "--plan", "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["aborted"] is False
        assert payload["mode"] == "plan"


class TestSyncApply:
    def test_apply_add_writes_registry_and_sidecar(self, runner, sandbox):
        _seed_claude_code(sandbox, {"alpha": {"command": "npx"}})

        res = _sync(runner, "--apply", "--yes")
        assert res.exit_code == 0, res.output

        registry = state.load_registry()
        assert "alpha" in registry.servers

        sidecar = state.load_import_state()
        assert "alpha" in sidecar.entries
        assert sidecar.entries["alpha"].drift_hash == compute_drift_hash(registry.servers["alpha"])

    def test_apply_remove_drops_registry_and_sidecar(self, runner, sandbox):
        """removed_at_host → REMOVE registry + sidecar entry (orphan cleanup
        atomic with the registry write)."""
        _seed_claude_code(sandbox, {"to_remove": {"command": "npx"}})
        _apply_claude_code(runner)
        # Drop entry from host.
        _seed_claude_code(sandbox, {})

        res = _sync(runner, "--apply", "--yes")
        assert res.exit_code == 0, res.output

        registry = state.load_registry()
        assert "to_remove" not in registry.servers
        sidecar = state.load_import_state()
        assert "to_remove" not in sidecar.entries

    def test_apply_backfill_stamps_no_baseline_with_candidate(self, runner, sandbox):
        """no_baseline + matched candidate → BACKFILL only the sidecar.
        Registry stays as it was (no add, no remove)."""
        _seed_claude_code(sandbox, {"x": {"command": "npx"}})
        _apply_claude_code(runner)
        # Manually delete the sidecar row but leave host + registry intact.
        s = state.load_import_state()
        s.entries.pop("x")
        state.save_import_state(s)

        registry_before = Path(state.registry_path()).read_bytes()

        res = _sync(runner, "--apply", "--yes")
        assert res.exit_code == 0, res.output

        # Registry bytes unchanged (BACKFILL never re-writes registry
        # when there are no add/remove ops).
        assert Path(state.registry_path()).read_bytes() == registry_before
        sidecar = state.load_import_state()
        assert "x" in sidecar.entries
        assert sidecar.entries["x"].drift_hash_version == HASH_VERSION

    def test_apply_changed_not_re_stamped(self, runner, sandbox):
        """Lock-down 3: without ``--force``, ``changed`` is NOT mutated
        by sync. The sidecar baseline_hash stays at the original value;
        ``--force`` is the only writer (pinned by
        ``TestSyncForceApply.test_apply_force_yes_re_stamps_registry_and_sidecar``).
        """
        _seed_claude_code(sandbox, {"a": {"command": "npx", "args": ["v1"]}})
        _apply_claude_code(runner)
        original = state.load_import_state().entries["a"].drift_hash
        _seed_claude_code(sandbox, {"a": {"command": "npx", "args": ["v2"]}})

        res = _sync(runner, "--apply", "--yes")
        assert res.exit_code == 0, res.output

        # baseline_hash unchanged after --apply.
        assert state.load_import_state().entries["a"].drift_hash == original

    def test_apply_orphan_no_baseline_left_alone(self, runner, sandbox):
        """no_baseline with no matched candidate → SKIP. Sidecar +
        registry both unchanged; sync can't safely fabricate a baseline.
        """
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)
        # Drop sidecar baseline AND host entry — registry retains entry.
        s = state.load_import_state()
        s.entries.pop("a")
        state.save_import_state(s)
        _seed_claude_code(sandbox, {})

        registry_before = Path(state.registry_path()).read_bytes()
        sidecar_before = Path(state.import_state_path()).read_bytes()

        res = _sync(runner, "--apply", "--yes")
        assert res.exit_code == 0, res.output

        # Both unchanged: orphan no_baseline is non-mutating.
        assert Path(state.registry_path()).read_bytes() == registry_before
        assert Path(state.import_state_path()).read_bytes() == sidecar_before

    def test_apply_atomicity_registry_first(self, runner, sandbox, monkeypatch):
        """Crash between registry write and sidecar write → second sync
        recovers via BACKFILL (no_baseline + matched candidate)."""
        _seed_claude_code(sandbox, {"alpha": {"command": "npx"}})

        # Patch save_import_state to raise, only for the first call.
        from memtomem_stm.mms import state as state_mod

        original_save = state_mod.save_import_state
        calls = {"n": 0}

        def flaky_save(s):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("simulated sidecar write failure")
            return original_save(s)

        monkeypatch.setattr(state_mod, "save_import_state", flaky_save)

        res = _sync(runner, "--apply", "--yes")
        # Registry was written; sidecar raised → exit non-zero.
        assert res.exit_code != 0, res.output
        registry = state.load_registry()
        assert "alpha" in registry.servers
        # Sidecar still empty (write failed before any state was saved).
        assert "alpha" not in state.load_import_state().entries

        # Second sync recovers: alpha now appears as no_baseline +
        # matched candidate → BACKFILL.
        res2 = _sync(runner, "--apply", "--yes")
        assert res2.exit_code == 0, res2.output
        assert "alpha" in state.load_import_state().entries

    def test_apply_no_sidecar_write_when_registry_fails(self, runner, sandbox, monkeypatch):
        """Atomicity invariant reverse: if save_registry raises, the
        sidecar must be untouched (registry-first means sidecar never
        gets a chance to write)."""
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)
        _seed_claude_code(sandbox, {"a": {"command": "npx"}, "b": {"command": "npx"}})

        sidecar_before = Path(state.import_state_path()).read_bytes()

        from memtomem_stm.mms import state as state_mod

        def boom(_):
            raise OSError("simulated registry write failure")

        monkeypatch.setattr(state_mod, "save_registry", boom)

        res = _sync(runner, "--apply", "--yes")
        assert res.exit_code != 0, res.output
        assert Path(state.import_state_path()).read_bytes() == sidecar_before

    def test_apply_orphan_cleanup_executes_when_only_orphans_present(self, runner, sandbox):
        """Orphan-only state (sidecar has row for name not in registry,
        nothing else pending) must trigger a sidecar write — orphan
        cleanup is a mutation, not a no-op.
        """
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)
        # Inject an orphan: sidecar has a row for "ghost" with no
        # corresponding registry entry.
        s = state.load_import_state()
        s.entries["ghost"] = state.ImportStateEntry(
            drift_hash="sha256:" + "f" * 16,
            drift_hash_version=HASH_VERSION,
            last_imported="2026-01-01T00:00:00Z",
            source_label="Claude Code (user)",
        )
        state.save_import_state(s)

        res = _sync(runner, "--apply", "--yes")
        assert res.exit_code == 0, res.output
        # Orphan got filtered out.
        assert "ghost" not in state.load_import_state().entries
        # NOT the no-op message — orphan cleanup counts as work done.
        assert "Already synchronized" not in res.output
        assert "stale sidecar" in res.output

    def test_apply_orphan_cleanup_alongside_other_mutations(self, runner, sandbox):
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)
        # Add orphan + add a new host entry (ADD bucket).
        s = state.load_import_state()
        s.entries["ghost"] = state.ImportStateEntry(
            drift_hash="sha256:" + "f" * 16,
            drift_hash_version=HASH_VERSION,
            last_imported="2026-01-01T00:00:00Z",
            source_label="Claude Code (user)",
        )
        state.save_import_state(s)
        _seed_cursor_user(sandbox, {"b": {"command": "npx"}})

        res = _sync(runner, "--apply", "--yes")
        assert res.exit_code == 0, res.output
        assert "b" in state.load_registry().servers
        assert "ghost" not in state.load_import_state().entries

    def test_apply_already_synchronized_message(self, runner, sandbox):
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)

        registry_before = Path(state.registry_path()).read_bytes()
        sidecar_before = Path(state.import_state_path()).read_bytes()

        res = _sync(runner, "--apply", "--yes")
        assert res.exit_code == 0, res.output
        assert "Already synchronized. No changes." in res.output

        # No writes (mtime / contents stable).
        assert Path(state.registry_path()).read_bytes() == registry_before
        assert Path(state.import_state_path()).read_bytes() == sidecar_before

    def test_apply_first_import_wins_for_new_in_multiple_hosts(self, runner, sandbox):
        """Same `new` name in two hosts with different shapes:
        first-import-wins on the ADD path → only one written; the
        second is a conflict.
        """
        _seed_claude_code(sandbox, {"shared": {"command": "claude-cmd"}})
        _seed_cursor_user(sandbox, {"shared": {"command": "cursor-cmd"}})

        res = _sync(runner, "--apply", "--yes")
        assert res.exit_code == 0, res.output

        # Claude Code is iterated first in ALL_HOSTS, so it wins.
        registry = state.load_registry()
        assert registry.servers["shared"].command == "claude-cmd"

        # Conflict surfaced in plan output.
        assert "Conflicts" in res.output

    def test_cross_host_shape_relocation_does_not_replace(self, runner, sandbox):
        """Load-bearing contract (BLOCKER fix): X baselined to host A,
        then A drops X and a different host B picks up X with a new
        shape. ``_classify`` Pass 2 fallback finds B's X → state =
        ``changed`` (NOT ``removed_at_host``). Sync MUST NOT replace
        the registry shape; user can acknowledge via ``--force`` only
        when the candidate matches the baseline source — see W3
        Lock-down 6 + ``TestSyncForceCrossHostLockdown6`` for the
        ``--force`` behavior on cross-host drift.
        """
        # Step 1: import shared from claude-code (becomes baseline).
        _seed_claude_code(sandbox, {"shared": {"command": "npx", "args": ["claude-args"]}})
        _apply_claude_code(runner)
        original_shape = state.load_registry().servers["shared"]
        original_hash = state.load_import_state().entries["shared"].drift_hash

        # Step 2: drop from claude-code, add to cursor with a DIFFERENT shape.
        _seed_claude_code(sandbox, {})
        _seed_cursor_user(sandbox, {"shared": {"command": "npx", "args": ["cursor-args"]}})

        # Plan: assert X surfaces as `changed` (skipped) — not REMOVE,
        # not ADD.
        plan_res = _sync(runner, "--plan", "--json")
        assert plan_res.exit_code == 0, plan_res.output
        plan_payload = json.loads(plan_res.output)
        assert plan_payload["summary"]["skipped_changed"] == 1
        assert plan_payload["summary"]["removed"] == 0
        assert plan_payload["summary"]["added"] == 0

        # Apply: registry shape MUST be unchanged.
        apply_res = _sync(runner, "--apply", "--yes")
        assert apply_res.exit_code == 0, apply_res.output
        assert state.load_registry().servers["shared"] == original_shape
        assert state.load_import_state().entries["shared"].drift_hash == original_hash


class TestSyncConfirmation:
    def test_apply_remove_prompts_in_tty_declines_no_writes(self, runner, sandbox, monkeypatch):
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)
        _seed_claude_code(sandbox, {})  # triggers REMOVE

        _force_tty(monkeypatch)
        registry_before = Path(state.registry_path()).read_bytes()
        sidecar_before = Path(state.import_state_path()).read_bytes()

        res = _sync(runner, "--apply", input="n\n")
        assert res.exit_code == 2, res.output
        assert "Aborted. No changes." in res.output
        # No writes.
        assert Path(state.registry_path()).read_bytes() == registry_before
        assert Path(state.import_state_path()).read_bytes() == sidecar_before

    def test_apply_remove_prompts_in_tty_accepts_executes(self, runner, sandbox, monkeypatch):
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)
        _seed_claude_code(sandbox, {})

        _force_tty(monkeypatch)

        res = _sync(runner, "--apply", input="y\n")
        assert res.exit_code == 0, res.output
        assert "a" not in state.load_registry().servers

    def test_apply_remove_aborts_in_non_tty_without_yes(self, runner, sandbox):
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)
        _seed_claude_code(sandbox, {})

        registry_before = Path(state.registry_path()).read_bytes()

        res = _sync(runner, "--apply")
        # Non-TTY without --yes → exit 2 + clear error.
        assert res.exit_code == 2, res.output
        # CliRunner mixes stdout+stderr; the abort message lands in
        # res.output regardless.
        assert "--yes" in res.output
        # No writes.
        assert Path(state.registry_path()).read_bytes() == registry_before

    def test_apply_yes_bypasses_prompt_in_non_tty(self, runner, sandbox):
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)
        _seed_claude_code(sandbox, {})

        res = _sync(runner, "--apply", "--yes")
        assert res.exit_code == 0, res.output
        assert "a" not in state.load_registry().servers

    def test_apply_no_remove_no_prompt(self, runner, sandbox):
        """Only ADD bucket — no REMOVE, no prompt regardless of TTY/--yes."""
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        # No --yes; should still succeed because no removal needs confirmation.
        res = _sync(runner, "--apply")
        assert res.exit_code == 0, res.output
        assert "a" in state.load_registry().servers
        assert "Aborted" not in res.output
        assert "--yes" not in res.output

    def test_apply_remove_prompt_lists_each_entry(self, runner, sandbox, monkeypatch):
        """Confirmation message must list every removed entry by name +
        source_label + last_imported, plus the tail with other-bucket
        counts. Pins the inline-disambiguation contract.
        """
        _seed_claude_code(sandbox, {"first": {"command": "npx"}, "second": {"command": "npx"}})
        _apply_claude_code(runner)
        _seed_claude_code(sandbox, {})  # both → REMOVE

        captured = {"prompt": ""}
        # Intercept click.confirm to capture the prompt text. The
        # spy declines so no writes happen; the test asserts on the
        # captured text rather than dispatching to the real confirm.
        import click as _click_mod

        def spy_confirm(text, *args, **kwargs):
            captured["prompt"] = text
            return False

        monkeypatch.setattr(_click_mod, "confirm", spy_confirm)
        _force_tty(monkeypatch)

        res = _sync(runner, "--apply")
        assert res.exit_code == 2, res.output
        prompt = captured["prompt"]
        assert "first" in prompt
        assert "second" in prompt
        assert "Claude Code (user)" in prompt
        # Tail summary present.
        assert "Also:" in prompt


class TestSyncJsonShape:
    _PLAN_PAYLOAD_KEYS = {
        "add",
        "remove",
        "backfill",
        "cleanup",
        "skipped_changed",
        "orphan_no_baseline",
        "conflicts",
    }
    _SUMMARY_KEYS = {
        "added",
        "removed",
        "backfilled",
        "cleanup",
        "skipped_changed",
        "orphan_no_baseline",
        "restamped",
        "unchanged",
        "conflicts",
    }

    def test_json_shape_plan_mode_all_keys_present(self, runner, sandbox):
        # Empty registry → still emits full shape with zeros.
        res = _sync(runner, "--plan", "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert set(payload.keys()) == {"mode", "aborted", "plan", "summary"}
        assert payload["mode"] == "plan"
        assert payload["aborted"] is False
        assert set(payload["plan"].keys()) == self._PLAN_PAYLOAD_KEYS
        assert set(payload["summary"].keys()) == self._SUMMARY_KEYS

    def test_json_shape_apply_mode_same_shape(self, runner, sandbox):
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        res = _sync(runner, "--apply", "--yes", "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["mode"] == "apply"
        assert payload["aborted"] is False
        assert set(payload["plan"].keys()) == self._PLAN_PAYLOAD_KEYS
        assert set(payload["summary"].keys()) == self._SUMMARY_KEYS

    def test_json_apply_with_remove_requires_yes_even_in_tty(self, runner, sandbox, monkeypatch):
        """``--json`` + REMOVE forces ``--yes`` regardless of TTY.

        A TTY confirmation prompt cannot be answered programmatically
        by a JSON consumer, and mixing prompt text with JSON output
        would corrupt machine parsing — so even with a real TTY we
        bypass the prompt path and abort like the non-TTY case.
        """
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)
        _seed_claude_code(sandbox, {})

        _force_tty(monkeypatch)

        res = _sync(runner, "--apply", "--json")
        assert res.exit_code == 2, res.output
        # No prompt was shown — output is just the stderr abort
        # message followed by JSON. Pin both halves: prompt absent +
        # JSON parses cleanly when stripped of the stderr message.
        assert "Proceed?" not in res.output
        # Locate the JSON payload (after the stderr abort message).
        text = res.output
        start = text.find("{")
        end = text.rfind("}")
        payload = json.loads(text[start : end + 1])
        assert payload["aborted"] is True
        assert payload["mode"] == "apply"
        assert all(payload["plan"][k] == [] for k in payload["plan"])
        assert all(payload["summary"][k] == 0 for k in payload["summary"])

    def test_json_aborted_true_on_non_tty_abort(self, runner, sandbox):
        """Non-TTY + REMOVE + no ``--yes`` + ``--json`` → exit 2 with
        ``aborted: true`` JSON. Symmetric path with the TTY+--json
        case (see test above)."""
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)
        _seed_claude_code(sandbox, {})

        res = _sync(runner, "--apply", "--json")
        assert res.exit_code == 2, res.output
        text = res.output
        start = text.find("{")
        end = text.rfind("}")
        payload = json.loads(text[start : end + 1])
        assert payload["aborted"] is True
        assert payload["mode"] == "apply"


class TestSyncContractPin:
    def test_classify_against_registry_signature_and_return_shape_pinned(self):
        """Cross-CLI import is brittle by convention; signature OR
        return-shape change in mms_import.py would silently break
        sync_cmd. Pin both — the param-name list AND the runtime
        contract (3-tuple of lists).
        """
        from inspect import signature

        from memtomem_stm.cli.mms_import import _classify_against_registry

        sig = signature(_classify_against_registry)
        assert list(sig.parameters) == ["candidates", "registry"]

        # Smoke: empty inputs → 3-tuple of lists. sync_cmd unpacks
        # ``new, conflicts, _idempotent = _classify_against_registry(...)``;
        # if the return type ever changes to a dict / 2-tuple / NamedTuple,
        # this test fires before the unpack does at runtime.
        result = _classify_against_registry([], state.RegistryConfig())
        assert isinstance(result, tuple) and len(result) == 3
        assert all(isinstance(x, list) for x in result)

    def test_format_env_summary_signature_and_return_shape_pinned(self):
        """Second cross-imported helper. ``_render_sync_text`` calls
        ``_format_env_summary(cand.server, cand.env_classification,
        show_imported=False)``; a kwarg rename or type change would
        silently break sync's ADD bucket text rendering.
        """
        from inspect import signature

        from memtomem_stm.cli.mms_import import _format_env_summary
        from memtomem_stm.mms.secrets import classify_env

        sig = signature(_format_env_summary)
        assert list(sig.parameters) == ["server", "env_classification", "show_imported"]

        # Smoke: empty env → returns a string ("no env" path).
        server = state.RegistryServer(command="npx", args=[], env={}, prefix="x")
        result = _format_env_summary(server, classify_env({}), show_imported=False)
        assert isinstance(result, str)

    def test_sync_docstring_mentions_first_time_vs_ongoing(self):
        """Lock-down 1 anchor: sync's docstring carries the
        intent-distinction phrase. Future docstring rewrites must
        preserve it or this test fires."""
        from memtomem_stm.cli.mms_host import sync_cmd

        # ``sync_cmd.__doc__`` returns Click's command docstring.
        doc = sync_cmd.__doc__ or ""
        assert "ongoing-" in doc and "reconciliation" in doc, doc

    def test_import_docstring_mentions_sync_cross_link(self):
        """Lock-down 1 mirror anchor: mms_import.py module docstring
        cross-links sync."""
        from memtomem_stm.cli import mms_import

        assert "mms host sync" in (mms_import.__doc__ or "")


# ---------------------------------------------------------------------------
# sync --force — re-stamp surface (W3)
# ---------------------------------------------------------------------------


def _drift_changed(sandbox, runner, *, original: dict, drifted: dict, name: str = "x") -> None:
    """Helper: import ``original`` from claude-code, then mutate host
    config to ``drifted`` so the entry surfaces as ``changed`` on the
    next sync.
    """
    _seed_claude_code(sandbox, {name: original})
    _apply_claude_code(runner)
    _seed_claude_code(sandbox, {name: drifted})


class TestSyncForcePlan:
    def test_plan_force_renders_restamp_section_with_diff(self, runner, sandbox):
        _drift_changed(
            sandbox,
            runner,
            original={"command": "npx", "args": ["v1"]},
            drifted={"command": "npx", "args": ["v2"]},
        )
        res = _sync(runner, "--plan", "--force")
        assert res.exit_code == 0, res.output
        # RESTAMP section header.
        assert "RESTAMP" in res.output
        # Diff renderer shows old/new args.
        assert "v1" in res.output
        assert "v2" in res.output
        assert "Old:" in res.output
        assert "New:" in res.output

    def test_plan_force_no_changed_renders_no_restamp_section(self, runner, sandbox):
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)
        # No drift — `changed` bucket empty, RESTAMP must NOT render
        # even with --force.
        res = _sync(runner, "--plan", "--force")
        assert res.exit_code == 0, res.output
        assert "RESTAMP" not in res.output

    def test_plan_force_diff_redacts_secret_env_values(self, runner, sandbox):
        _drift_changed(
            sandbox,
            runner,
            original={"command": "npx", "env": {"GITHUB_TOKEN": "ghp_old"}},
            drifted={"command": "npx", "env": {"GITHUB_TOKEN": "ghp_new"}},
        )
        res = _sync(runner, "--plan", "--force")
        assert res.exit_code == 0, res.output
        # Secret values must NOT leak in either Old or New.
        assert "ghp_old" not in res.output
        assert "ghp_new" not in res.output
        # The key should still appear.
        assert "GITHUB_TOKEN" in res.output

    def test_plan_force_env_value_only_drift_renders_changed_note(self, runner, sandbox):
        """Edge case: command + args + env keys all match; only env
        values changed (e.g. token rotation). The diff renderer
        collapses to a single observable-shape line + an explicit
        "(env values redacted; values changed)" note so the user knows
        *something* changed without the values leaking.
        """
        _drift_changed(
            sandbox,
            runner,
            original={"command": "npx", "env": {"API_KEY": "k1"}},
            drifted={"command": "npx", "env": {"API_KEY": "k2"}},
        )
        res = _sync(runner, "--plan", "--force")
        assert res.exit_code == 0, res.output
        # Both values redacted, key visible.
        assert "k1" not in res.output
        assert "k2" not in res.output
        assert "API_KEY" in res.output
        # The clarifying note fires — user sees *why* re-stamp will run
        # even though Old/New rows would have looked identical.
        assert "(env values redacted; values changed)" in res.output


class TestSyncForceApply:
    def test_apply_force_yes_re_stamps_registry_and_sidecar(self, runner, sandbox):
        _drift_changed(
            sandbox,
            runner,
            original={"command": "npx", "args": ["v1"]},
            drifted={"command": "npx", "args": ["v2"]},
        )
        original_baseline = state.load_import_state().entries["x"].drift_hash

        res = _sync(runner, "--apply", "--force", "--yes")
        assert res.exit_code == 0, res.output

        # Registry adopted host shape (Lock-down 1).
        assert list(state.load_registry().servers["x"].args) == ["v2"]
        # Sidecar baseline refreshed.
        new_baseline = state.load_import_state().entries["x"].drift_hash
        assert new_baseline != original_baseline
        # And matches current canonical hash.
        from memtomem_stm.mms.drift import compute_drift_hash

        assert new_baseline == compute_drift_hash(state.load_registry().servers["x"])

    def test_apply_no_force_does_not_mutate_changed(self, runner, sandbox):
        """Lock-down 3 sibling: without ``--force``, ``changed`` stays
        non-mutating even on ``--apply``."""
        _drift_changed(
            sandbox,
            runner,
            original={"command": "npx", "args": ["v1"]},
            drifted={"command": "npx", "args": ["v2"]},
        )
        original_baseline = state.load_import_state().entries["x"].drift_hash
        original_shape = state.load_registry().servers["x"]

        res = _sync(runner, "--apply", "--yes")
        assert res.exit_code == 0, res.output
        # Both registry and sidecar untouched.
        assert state.load_registry().servers["x"] == original_shape
        assert state.load_import_state().entries["x"].drift_hash == original_baseline

    def test_apply_force_no_changed_is_no_op(self, runner, sandbox):
        """``--apply --force`` with empty ``changed`` and no other
        mutations → no-op message, exit 0."""
        _seed_claude_code(sandbox, {"a": {"command": "npx"}})
        _apply_claude_code(runner)

        res = _sync(runner, "--apply", "--force", "--yes")
        assert res.exit_code == 0, res.output
        assert "Already synchronized" in res.output

    def test_apply_force_summary_emits_restamped_count(self, runner, sandbox):
        _drift_changed(
            sandbox,
            runner,
            original={"command": "npx", "args": ["v1"]},
            drifted={"command": "npx", "args": ["v2"]},
        )
        res = _sync(runner, "--apply", "--force", "--yes", "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["summary"]["restamped"] == 1
        # Default-0 invariant for non-force runs is pinned by
        # ``TestSyncJsonShape._SUMMARY_KEYS`` + the no-op test in
        # TestSyncPlan.

    def test_apply_force_no_force_keeps_restamped_zero(self, runner, sandbox):
        _drift_changed(
            sandbox,
            runner,
            original={"command": "npx", "args": ["v1"]},
            drifted={"command": "npx", "args": ["v2"]},
        )
        # Without --force, restamped stays 0 even when changed > 0.
        res = _sync(runner, "--plan", "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        assert payload["summary"]["restamped"] == 0
        assert payload["summary"]["skipped_changed"] == 1


class TestSyncForceConfirmation:
    def test_apply_force_prompts_in_tty_and_declines(self, runner, sandbox, monkeypatch):
        _drift_changed(
            sandbox,
            runner,
            original={"command": "npx", "args": ["v1"]},
            drifted={"command": "npx", "args": ["v2"]},
        )
        original_shape = state.load_registry().servers["x"]
        _force_tty(monkeypatch)

        res = _sync(runner, "--apply", "--force", input="n\n")
        assert res.exit_code == 2, res.output
        assert "Aborted" in res.output
        # No mutation on decline.
        assert state.load_registry().servers["x"] == original_shape

    def test_apply_force_non_tty_aborts_without_yes(self, runner, sandbox):
        _drift_changed(
            sandbox,
            runner,
            original={"command": "npx", "args": ["v1"]},
            drifted={"command": "npx", "args": ["v2"]},
        )
        original_shape = state.load_registry().servers["x"]

        res = _sync(runner, "--apply", "--force")
        assert res.exit_code == 2, res.output
        assert "--yes" in res.output
        assert state.load_registry().servers["x"] == original_shape

    def test_apply_force_json_without_yes_aborts_with_payload(self, runner, sandbox):
        _drift_changed(
            sandbox,
            runner,
            original={"command": "npx", "args": ["v1"]},
            drifted={"command": "npx", "args": ["v2"]},
        )
        res = _sync(runner, "--apply", "--force", "--json")
        assert res.exit_code == 2, res.output
        # Output is stderr abort message + JSON payload (CliRunner
        # mixes streams). Locate the JSON payload bounds — same
        # pattern as the REMOVE-only abort tests.
        text = res.output
        start = text.find("{")
        end = text.rfind("}")
        payload = json.loads(text[start : end + 1])
        assert payload["aborted"] is True
        assert payload["mode"] == "apply"

    def test_apply_force_yes_bypasses_prompt(self, runner, sandbox):
        _drift_changed(
            sandbox,
            runner,
            original={"command": "npx", "args": ["v1"]},
            drifted={"command": "npx", "args": ["v2"]},
        )
        # --yes alongside --force skips confirmation entirely.
        res = _sync(runner, "--apply", "--force", "--yes")
        assert res.exit_code == 0, res.output
        assert list(state.load_registry().servers["x"].args) == ["v2"]

    def test_apply_force_combined_remove_and_restamp_single_prompt(
        self, runner, sandbox, monkeypatch
    ):
        """REMOVE + RESTAMP both pending → one combined prompt with
        REMOVE section before RESTAMP section (Lock-down 2 / MEDIUM 3
        section order)."""
        _seed_claude_code(
            sandbox,
            {
                "to_remove": {"command": "npx"},
                "to_restamp": {"command": "npx", "args": ["v1"]},
            },
        )
        _apply_claude_code(runner)
        # Drop ``to_remove`` (REMOVE) and mutate ``to_restamp`` (RESTAMP).
        _seed_claude_code(sandbox, {"to_restamp": {"command": "npx", "args": ["v2"]}})

        captured = {"prompt": ""}
        import click as _click_mod

        def spy_confirm(text, *args, **kwargs):
            captured["prompt"] = text
            return False

        monkeypatch.setattr(_click_mod, "confirm", spy_confirm)
        _force_tty(monkeypatch)

        res = _sync(runner, "--apply", "--force")
        assert res.exit_code == 2, res.output
        prompt = captured["prompt"]
        # Both sections present.
        assert "removed from registry" in prompt
        assert "re-stamped" in prompt
        # Section order: REMOVE first (most destructive), RESTAMP second.
        # Pin the relative order so future PRs that flip them must
        # justify against the Lock-down 2 anchor.
        assert prompt.index("removed from registry") < prompt.index("re-stamped"), prompt


class TestSyncForceCrossHostLockdown6:
    def test_apply_force_does_not_re_stamp_cross_host_drift(
        self, runner, sandbox
    ):
        """Lock-down 6 BLOCKER: baseline source = claude-code; only
        cursor has the entry now; ``--force`` MUST NOT adopt cursor's
        shape. The entry stays in the changed bucket; registry +
        sidecar unchanged.
        """
        # Step 1: import shared from claude-code (baseline).
        _seed_claude_code(sandbox, {"shared": {"command": "npx", "args": ["claude-args"]}})
        _apply_claude_code(runner)
        original_shape = state.load_registry().servers["shared"]
        original_hash = state.load_import_state().entries["shared"].drift_hash

        # Step 2: drop from claude-code, add to cursor with a different shape.
        _seed_claude_code(sandbox, {})
        _seed_cursor_user(sandbox, {"shared": {"command": "npx", "args": ["cursor-args"]}})

        # --apply --force --yes: still must NOT replace registry shape.
        res = _sync(runner, "--apply", "--force", "--yes")
        assert res.exit_code == 0, res.output
        assert state.load_registry().servers["shared"] == original_shape
        assert state.load_import_state().entries["shared"].drift_hash == original_hash

    def test_skipped_changed_payload_marks_host_relocation(self, runner, sandbox):
        """Lock-down 6 JSON shape: cross-host drift → ``host_relocation:
        true``; in-place drift → ``host_relocation: false``."""
        # Cross-host drift case.
        _seed_claude_code(sandbox, {"x": {"command": "npx", "args": ["v1"]}})
        _apply_claude_code(runner)
        _seed_claude_code(sandbox, {})
        _seed_cursor_user(sandbox, {"x": {"command": "npx", "args": ["v2"]}})

        res = _sync(runner, "--plan", "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        skipped = payload["plan"]["skipped_changed"]
        assert len(skipped) == 1
        assert skipped[0]["name"] == "x"
        assert skipped[0]["host_relocation"] is True

    def test_skipped_changed_in_place_drift_host_relocation_false(self, runner, sandbox):
        _drift_changed(
            sandbox,
            runner,
            original={"command": "npx", "args": ["v1"]},
            drifted={"command": "npx", "args": ["v2"]},
        )
        res = _sync(runner, "--plan", "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        skipped = payload["plan"]["skipped_changed"]
        assert len(skipped) == 1
        assert skipped[0]["host_relocation"] is False

    def test_json_skipped_changed_does_not_leak_render_only_keys(self, runner, sandbox):
        """The diff renderer enriches ``changed_rows_classified`` rows
        in-place with underscore-prefixed keys (``_old_server``,
        ``_new_server``, ``_new_source_label``). These are
        render-only — a future refactor that accidentally builds
        ``plan_payload["skipped_changed"]`` from the enriched rows
        would silently leak ``RegistryServer`` objects into JSON
        (TypeError at serialization, or worse, a custom serializer
        that succeeds and exposes secret env values).

        Pin the invariant explicitly: no key in the JSON
        ``skipped_changed`` entries starts with ``_``.
        """
        # --plan --force triggers the enrichment loop; --json reads
        # the JSON payload after enrichment ran.
        _drift_changed(
            sandbox,
            runner,
            original={"command": "npx", "args": ["v1"], "env": {"API_KEY": "secret"}},
            drifted={"command": "npx", "args": ["v2"], "env": {"API_KEY": "secret"}},
        )
        res = _sync(runner, "--plan", "--force", "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)
        # Force surfaced the entry into RESTAMP, so skipped_changed is
        # empty in --force mode. Make the leak check robust by also
        # exercising the no-force path where skipped_changed has rows.
        for row in payload["plan"]["skipped_changed"]:
            for key in row:
                assert not key.startswith("_"), (
                    f"render-only key {key!r} leaked into JSON: {row}"
                )

        res2 = _sync(runner, "--plan", "--json")
        payload2 = json.loads(res2.output)
        assert len(payload2["plan"]["skipped_changed"]) == 1
        for row in payload2["plan"]["skipped_changed"]:
            for key in row:
                assert not key.startswith("_"), (
                    f"render-only key {key!r} leaked into JSON: {row}"
                )
        # Also assert the secret value didn't leak via any route.
        assert "secret" not in res.output
        assert "secret" not in res2.output

    def test_apply_force_only_eligible_when_mixed(self, runner, sandbox):
        """Mixed bucket: one in-place drift (eligible) + one cross-host
        drift (ineligible). ``--force`` re-stamps only the eligible one;
        cross-host stays in skipped_changed; in-place is removed from
        skipped_changed (it was just re-stamped, no longer 'skipped').
        """
        _seed_claude_code(
            sandbox,
            {
                "in_place": {"command": "npx", "args": ["v1"]},
                "moves": {"command": "npx", "args": ["origA"]},
            },
        )
        _apply_claude_code(runner)

        # in_place drifts at claude-code; moves disappears from claude-
        # code and re-appears at cursor with a different shape.
        _seed_claude_code(sandbox, {"in_place": {"command": "npx", "args": ["v2"]}})
        _seed_cursor_user(sandbox, {"moves": {"command": "npx", "args": ["cursorB"]}})

        original_moves_shape = state.load_registry().servers["moves"]

        res = _sync(runner, "--apply", "--force", "--yes", "--json")
        assert res.exit_code == 0, res.output
        payload = json.loads(res.output)

        # Eligible re-stamped.
        assert payload["summary"]["restamped"] == 1
        assert list(state.load_registry().servers["in_place"].args) == ["v2"]

        # Cross-host untouched, still in skipped_changed.
        assert state.load_registry().servers["moves"] == original_moves_shape
        skipped_names = {row["name"] for row in payload["plan"]["skipped_changed"]}
        assert skipped_names == {"moves"}, skipped_names
        # Smoke-discovered fix: in-place must NOT be reported as
        # 'skipped' once --force has re-stamped it. Counting both in
        # skipped_changed would make the "use --force to acknowledge"
        # footer fire after we just re-stamped them.
        assert payload["summary"]["skipped_changed"] == 1

    def test_all_cross_host_suppresses_standard_pointer(self, runner, sandbox):
        """When *every* changed entry is cross-host, the standard
        "use --force to acknowledge" pointer is wrong — --force won't
        adopt any of them. Suppress it; render only the cross-host
        manual-review footer as the primary message. A user reading
        only the first footer line should walk away with the right
        action.
        """
        _seed_claude_code(sandbox, {"x": {"command": "npx", "args": ["v1"]}})
        _apply_claude_code(runner)
        _seed_claude_code(sandbox, {})
        _seed_cursor_user(sandbox, {"x": {"command": "npx", "args": ["v2"]}})

        res = _sync(runner, "--plan")
        assert res.exit_code == 0, res.output
        # Standard pointer suppressed.
        assert "differ in shape at host" not in res.output
        # Cross-host manual-review footer fires as primary.
        assert "shape-relocated to a different host than baseline source" in res.output
        assert "manual review" in res.output

    def test_mixed_cross_host_renders_both_pointer_and_subline(self, runner, sandbox):
        """When changed entries are a *mix* of in-place + cross-host,
        the standard pointer fires (it helps the in-place subset) and
        the cross-host sub-line clarifies that --force will skip the
        relocated subset.
        """
        _seed_claude_code(
            sandbox,
            {
                "in_place": {"command": "npx", "args": ["v1"]},
                "moves": {"command": "npx", "args": ["origA"]},
            },
        )
        _apply_claude_code(runner)
        # in_place drifts at claude-code; moves shape-relocates to cursor.
        _seed_claude_code(sandbox, {"in_place": {"command": "npx", "args": ["v2"]}})
        _seed_cursor_user(sandbox, {"moves": {"command": "npx", "args": ["cursorB"]}})

        res = _sync(runner, "--plan")
        assert res.exit_code == 0, res.output
        # Both lines fire.
        assert "differ in shape at host" in res.output
        assert "shape-relocated to a different host than baseline source" in res.output
        # Pointer first (helps the in_place subset), sub-line after.
        assert res.output.index("differ in shape") < res.output.index("shape-relocated"), res.output

    def test_apply_force_does_not_print_changed_footer_for_restamped(
        self, runner, sandbox
    ):
        """Smoke-discovered fix: after ``--force --apply`` re-stamps an
        entry, the "differ in shape at host. Use --force to acknowledge."
        footer must NOT fire for it. The footer's ``--force`` pointer
        would be misleading — the entry is already re-stamped.
        """
        _drift_changed(
            sandbox,
            runner,
            original={"command": "npx", "args": ["v1"]},
            drifted={"command": "npx", "args": ["v2"]},
        )
        res = _sync(runner, "--apply", "--force", "--yes")
        assert res.exit_code == 0, res.output
        # The standard "differ" pointer footer must NOT fire — the
        # only entry in changed bucket got re-stamped.
        assert "differ in shape at host" not in res.output
        assert "use `mms host sync --force` to acknowledge" not in res.output.lower()

    def test_apply_force_prints_cross_host_only_footer_when_only_cross_host_left(
        self, runner, sandbox
    ):
        """When ``--force --apply`` only had cross-host entries to deal
        with (no eligible re-stamps), the changed-bucket footer renders
        as the cross-host manual-review message — not the standard
        ``--force`` pointer (which is wrong for cross-host)."""
        _seed_claude_code(sandbox, {"x": {"command": "npx", "args": ["v1"]}})
        _apply_claude_code(runner)
        _seed_claude_code(sandbox, {})
        _seed_cursor_user(sandbox, {"x": {"command": "npx", "args": ["v2"]}})

        res = _sync(runner, "--apply", "--force", "--yes")
        assert res.exit_code == 0, res.output
        # Standard pointer suppressed — --force already declined to
        # adopt cross-host candidates.
        assert "differ in shape at host" not in res.output
        # Cross-host manual-review footer fires instead.
        assert "shape-relocated to a different host than baseline source" in res.output
        assert "manual review" in res.output
