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
        assert "1 entry in registry not present in any host scan" in res.output
        # Main-table presence is asserted via the JSON shape below — the
        # text split was brittle to footer rewording (PR4 may evolve it).

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
