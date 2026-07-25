"""CliRunner tests for ``mms import`` (RFC §7.2)."""

from __future__ import annotations

import json
import re
import stat
import sys
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from memtomem_stm.cli.mms_import import import_command
from memtomem_stm.mms import drift, state
from memtomem_stm.mms.drift import compute_drift_hash
from memtomem_stm.mms.secrets import REDACTED_DISPLAY
from helpers import set_home


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Pin HOME so ``~/.mms`` and ``~/.claude.json`` etc. land in tmp."""
    set_home(monkeypatch, tmp_path)
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return {"home": tmp_path, "cwd": cwd}


def _seed_claude_code(sandbox, mcps: dict) -> None:
    cfg = {"mcpServers": mcps}
    (sandbox["home"] / ".claude.json").write_text(json.dumps(cfg), encoding="utf-8")


def _seed_dot_mcp_json(sandbox, mcps: dict) -> None:
    """Write ``<cwd>/.mcp.json`` — a project-local (repo-shippable) source."""
    (sandbox["cwd"] / ".mcp.json").write_text(json.dumps({"mcpServers": mcps}), encoding="utf-8")


# ---------------------------------------------------------------------------
# --plan default
# ---------------------------------------------------------------------------


class TestPlan:
    def test_no_hosts_no_candidates(self, runner, sandbox):
        res = runner.invoke(import_command, ["--from", "claude-code"])
        assert res.exit_code == 0, res.output
        assert "No MCP definitions found" in res.output

    def test_plan_default_redacts_secrets(self, runner, sandbox):
        _seed_claude_code(
            sandbox,
            {
                "github": {
                    "command": "npx",
                    "args": ["-y", "@mcp/gh"],
                    "env": {"GITHUB_TOKEN": "ghp_realsecret"},
                },
            },
        )
        res = runner.invoke(import_command, ["--from", "claude-code"])
        assert res.exit_code == 0, res.output
        assert "ghp_realsecret" not in res.output  # ← critical: secret never printed
        assert REDACTED_DISPLAY in res.output
        assert "GITHUB_TOKEN" in res.output
        assert "matches *TOKEN*" in res.output  # classification reason surfaced
        # apply hint at the bottom
        assert "mms import --from claude-code --apply" in res.output

    def test_show_imported_reveals_secrets(self, runner, sandbox):
        _seed_claude_code(
            sandbox,
            {
                "github": {
                    "command": "npx",
                    "env": {"GITHUB_TOKEN": "ghp_realsecret"},
                },
            },
        )
        res = runner.invoke(import_command, ["--from", "claude-code", "--show-imported"])
        assert res.exit_code == 0, res.output
        assert "ghp_realsecret" in res.output
        assert "(secret)" in res.output  # tag still indicates classification

    def test_plan_does_not_write_registry(self, runner, sandbox):
        _seed_claude_code(sandbox, {"foo": {"command": "echo"}})
        runner.invoke(import_command, ["--from", "claude-code"])
        # No --apply → registry is still empty (no file)
        assert not state.registry_path().exists()

    def test_plan_does_not_write_import_state(self, runner, sandbox):
        """Mirror of ``test_plan_does_not_write_registry`` for the W2 sidecar.
        ``--plan`` returns at the early echo before the sidecar write block,
        so no ``import_state.toml`` should appear on disk.
        """
        _seed_claude_code(sandbox, {"foo": {"command": "echo"}})
        runner.invoke(import_command, ["--from", "claude-code"])
        assert not state.import_state_path().exists()

    def test_plan_summary_counts(self, runner, sandbox):
        _seed_claude_code(
            sandbox,
            {
                "foo": {"command": "echo"},
                "bar": {"command": "echo"},
            },
        )
        res = runner.invoke(import_command, ["--from", "claude-code"])
        assert "to add (new):    2" in res.output
        assert "unchanged:       0" in res.output
        assert "conflicts:       0" in res.output


# ---------------------------------------------------------------------------
# --apply
# ---------------------------------------------------------------------------


class TestApply:
    def test_apply_writes_registry_with_secrets_intact(self, runner, sandbox):
        _seed_claude_code(
            sandbox,
            {
                "filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]},
                "github": {
                    "command": "npx",
                    "env": {"GITHUB_TOKEN": "ghp_realsecret"},
                },
            },
        )
        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res.exit_code == 0, res.output
        assert "Wrote 2 new entries" in res.output

        cfg = state.load_registry()
        assert set(cfg.servers.keys()) == {"filesystem", "github"}
        # Secrets stored verbatim in the registry (registry is gitignored).
        assert cfg.servers["github"].env == {"GITHUB_TOKEN": "ghp_realsecret"}

    def test_apply_then_replay_idempotent(self, runner, sandbox):
        _seed_claude_code(sandbox, {"foo": {"command": "echo"}})
        runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        # Snapshot sidecar after the first apply — second apply must not change it.
        sidecar_after_first = state.load_import_state()

        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res.exit_code == 0, res.output
        assert "Already up to date" in res.output

        # Registry unchanged — same single entry.
        cfg = state.load_registry()
        assert list(cfg.servers.keys()) == ["foo"]

        # Sidecar idempotent too — first-import-wins semantics extended to the
        # sidecar means an unchanged-host re-apply is a true no-op including
        # ``last_imported``. PR3 drift detection assumes the sidecar carries
        # the timestamp of *first* import, not of last invocation.
        assert state.load_import_state() == sidecar_after_first

    def test_apply_first_import_wins_on_conflict(self, runner, sandbox):
        # Pre-seed registry with `foo` pointing at one command.
        first = state.RegistryConfig(
            servers={"foo": state.RegistryServer(command="first-cmd", prefix="foo")}
        )
        state.save_registry(first)

        # Host config now declares `foo` with a different command.
        _seed_claude_code(sandbox, {"foo": {"command": "second-cmd"}})
        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res.exit_code == 0, res.output
        assert "conflicts:       1" in res.output
        assert "differs from existing registry entry" in res.output

        # Registry should NOT have been overwritten.
        cfg = state.load_registry()
        assert cfg.servers["foo"].command == "first-cmd"

        # Sidecar must NOT contain the rejected candidate. Without this pin,
        # a future refactor that builds sidecar entries from `candidates`
        # instead of `new` would silently poison the sidecar with a hash of
        # a server the registry just refused — and PR3 drift detection
        # would then compare a host's "second-cmd" against its own hash and
        # see "no drift" forever.
        assert "foo" not in state.load_import_state().entries

    def test_apply_writes_new_alongside_existing(self, runner, sandbox):
        # Pre-seed registry with one entry.
        first = state.RegistryConfig(
            servers={"existing": state.RegistryServer(command="echo", prefix="ex")}
        )
        state.save_registry(first)

        _seed_claude_code(
            sandbox,
            {
                "existing": {
                    "command": "echo",
                    "prefix": "ex",
                },  # would be conflict — but identical
                "newcomer": {"command": "ls"},
            },
        )
        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res.exit_code == 0, res.output
        cfg = state.load_registry()
        assert set(cfg.servers.keys()) == {"existing", "newcomer"}

    def test_apply_writes_import_state_sidecar(self, runner, sandbox):
        """Wire-in counter test for ``compute_drift_hash`` — every accepted
        candidate must appear in the sidecar with a hash that matches the
        canonical implementation. Per ``feedback_wire_in_test_asserts_counters``,
        behavior-only assertions silently pass after a wire is removed; the
        oracle assertion (#4) is the kill-switch — a stub sidecar passes
        (1)-(3) but fails it.
        """
        _seed_claude_code(
            sandbox,
            {
                "filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]},
                "github": {
                    "command": "npx",
                    "env": {"GITHUB_TOKEN": "ghp_realsecret"},
                },
            },
        )
        before = datetime.now(timezone.utc)
        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res.exit_code == 0, res.output
        after = datetime.now(timezone.utc)

        # 1. file + permissions
        assert state.import_state_path().exists()
        if sys.platform != "win32":
            # NTFS doesn't expose POSIX mode bits; ACL is the right primitive.
            mode = stat.S_IMODE(state.import_state_path().stat().st_mode)
            assert mode == 0o600

        # 2. counter assertion: cardinality AND identity, both axes
        cfg = state.load_registry()
        loaded = state.load_import_state()
        assert len(loaded.entries) == len(cfg.servers)
        assert set(loaded.entries.keys()) == set(cfg.servers.keys())

        for name, entry in loaded.entries.items():
            # 3. format
            assert re.fullmatch(r"sha256:[0-9a-f]{16}", entry.drift_hash), entry.drift_hash
            # 4. oracle (kill-switch): hash matches canonical implementation
            assert entry.drift_hash == compute_drift_hash(cfg.servers[name])
            # 5. version pinned to module constant
            assert entry.drift_hash_version == drift.HASH_VERSION
            # 6. source_label exact match (claude-code at user scope)
            assert entry.source_label == "Claude Code (user)"
            # 7. timestamp within a CI-cold-start-tolerant window
            ts = datetime.fromisoformat(entry.last_imported.replace("Z", "+00:00"))
            assert before.replace(microsecond=0) <= ts
            assert (ts - after).total_seconds() <= 300

        # 8. single-timestamp invariant: every row in this --apply shares
        # one ``last_imported`` value. The production code captures ``now``
        # once at the action boundary; without this pin a future refactor
        # moving the capture inside the loop would still pass (1)-(7) since
        # all per-entry timestamps would still land in the 300s window.
        # PR3 reads "rows with the same timestamp were observed in one
        # apply" — break this invariant and that read is wrong.
        all_timestamps = {entry.last_imported for entry in loaded.entries.values()}
        assert len(all_timestamps) == 1, f"expected one shared timestamp, got {all_timestamps}"

    def test_apply_backfills_sidecar_when_registry_populated_but_sidecar_missing(
        self, runner, sandbox
    ):
        """Crash-recovery / pre-PR2 path: registry has entries but the
        sidecar lacks rows for them. The next ``--apply`` with unchanged
        host input must backfill the missing rows — this is what the
        module docstring promises as idempotent recovery. Without
        backfill, the sidecar stays empty forever (idempotent
        classification short-circuits the write block) and PR3 drift
        detection has no baseline to compare against.
        """
        _seed_claude_code(
            sandbox,
            {
                "filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]},
                "github": {"command": "npx"},
            },
        )
        # First apply: both registry and sidecar populated normally.
        res1 = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res1.exit_code == 0, res1.output
        assert set(state.load_import_state().entries.keys()) == {"filesystem", "github"}

        # Simulate: crash mid-write, manual sidecar delete, OR a registry
        # that was imported under a pre-PR2 mms (no sidecar wire existed).
        state.import_state_path().unlink()
        assert not state.import_state_path().exists()
        assert set(state.load_registry().servers.keys()) == {"filesystem", "github"}

        # Re-apply with unchanged host input — registry classifies both as
        # idempotent, but the backfill pass restores the sidecar.
        res2 = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res2.exit_code == 0, res2.output
        assert "Backfilled 2 sidecar rows" in res2.output
        # Registry message must NOT appear — registry is byte-unchanged.
        assert "Wrote" not in res2.output

        # Sidecar fully restored, hashes match the canonical implementation.
        cfg = state.load_registry()
        loaded = state.load_import_state()
        assert set(loaded.entries.keys()) == set(cfg.servers.keys())
        for name, entry in loaded.entries.items():
            assert entry.drift_hash == compute_drift_hash(cfg.servers[name])
            assert entry.drift_hash_version == drift.HASH_VERSION
            assert entry.source_label == "Claude Code (user)"
        # Backfill rows share the recovery-moment timestamp.
        assert len({e.last_imported for e in loaded.entries.values()}) == 1

        # Third apply with sidecar now populated — pure no-op.
        res3 = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res3.exit_code == 0, res3.output
        assert "Already up to date" in res3.output

    def test_apply_writes_new_and_backfills_in_one_invocation(self, runner, sandbox):
        """Mixed path: one ``--apply`` both writes a genuinely new entry
        AND backfills a sidecar row for an entry already in the registry
        but missing from the sidecar (pre-PR2 / crash-recovery shape).

        Pins two contracts the empirical smoke test exercised but no unit
        test had locked:
        * Output contract — both ``Wrote N new entry`` and
          ``Backfilled M sidecar row[s]`` lines are emitted in one run.
        * Single-timestamp invariant holds *across* the new and backfill
          paths (not just within either alone).

        Note: ``source_label`` provenance under cross-host recovery
        (original import from host A, recovery via host B) is its own
        edge case — pinned in
        ``test_backfill_source_label_uses_current_observing_host``.
        """
        # Step 1: first apply registers `existing` via the real scanner —
        # this is the only reliable way to get a candidate shape that the
        # subsequent apply will classify as idempotent. (Pre-seeding the
        # registry directly via ``save_registry`` doesn't work: the host
        # scanner derives ``prefix`` independently and the shapes diverge,
        # producing a conflict instead of an idempotent classification.)
        _seed_claude_code(sandbox, {"existing": {"command": "echo"}})
        res1 = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res1.exit_code == 0, res1.output
        assert "existing" in state.load_import_state().entries

        # Step 2: wipe sidecar — simulates crash mid-write or a pre-PR2
        # registry. Registry intact, sidecar gone.
        state.import_state_path().unlink()

        # Step 3: host config grows — ``existing`` still there (idempotent
        # against registry but missing from sidecar → backfill candidate)
        # AND ``newcomer`` (genuinely new).
        _seed_claude_code(
            sandbox,
            {
                "existing": {"command": "echo"},
                "newcomer": {"command": "ls"},
            },
        )
        res2 = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res2.exit_code == 0, res2.output

        # Output contract — both lines present in the same run. Reviewers
        # care that neither is silently swallowed; ordering is informational.
        assert "Wrote 1 new entry" in res2.output
        assert "Backfilled 1 sidecar row" in res2.output

        # Sidecar carries both entries with hashes matching the canonical
        # implementation.
        loaded = state.load_import_state()
        cfg = state.load_registry()
        assert set(loaded.entries.keys()) == {"existing", "newcomer"}
        assert set(cfg.servers.keys()) == {"existing", "newcomer"}
        for name, entry in loaded.entries.items():
            assert entry.drift_hash == compute_drift_hash(cfg.servers[name])
            assert entry.drift_hash_version == drift.HASH_VERSION
            assert entry.source_label == "Claude Code (user)"

        # Single-timestamp invariant — holds across both code paths in one
        # apply. Production captures ``now`` once at the action boundary;
        # without this pin a refactor that re-captured for backfill would
        # still pass the per-entry timestamp window check in #7.
        assert len({e.last_imported for e in loaded.entries.values()}) == 1

    def test_backfill_source_label_uses_current_observing_host(self, runner, sandbox):
        """Edge: registry has ``foo`` from an original claude-code import,
        sidecar is wiped, and by the time recovery runs the user has moved
        ``foo`` to cursor (claude-code config no longer mentions it). The
        backfill must stamp the row with **cursor**'s label, not the
        registry-resident original — the host scanner only sees what's
        there now, and "original import host" isn't recorded anywhere
        recoverable. Documented in module docstring; pinned here.
        """
        # Step 1: original import via claude-code populates registry+sidecar.
        _seed_claude_code(sandbox, {"foo": {"command": "echo"}})
        runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert state.load_import_state().entries["foo"].source_label == "Claude Code (user)"

        # Step 2: wipe sidecar AND remove ``foo`` from claude-code; user
        # has moved the same MCP definition to cursor.
        state.import_state_path().unlink()
        (sandbox["home"] / ".claude.json").write_text(
            json.dumps({"mcpServers": {}}), encoding="utf-8"
        )
        cursor_dir = sandbox["home"] / ".cursor"
        cursor_dir.mkdir()
        (cursor_dir / "mcp.json").write_text(
            json.dumps({"mcpServers": {"foo": {"command": "echo"}}}),
            encoding="utf-8",
        )

        # Step 3: recovery via cursor — registry shape matches → idempotent
        # → backfill. The candidate seen by ``--apply`` is from cursor.
        res = runner.invoke(import_command, ["--from", "cursor", "--apply"])
        assert res.exit_code == 0, res.output
        assert "Backfilled 1 sidecar row" in res.output

        # Source_label reflects the *current* observation, not the original.
        assert state.load_import_state().entries["foo"].source_label == "Cursor (user)"

    def test_apply_preserves_existing_sidecar_entries(self, runner, sandbox):
        """Adding new servers must not overwrite sidecar rows for servers
        that were imported in a previous apply (dict-merge invariant)."""
        # Seed sidecar with a pre-existing entry from some earlier import.
        prior = state.ImportState(
            entries={
                "old": state.ImportStateEntry(
                    drift_hash="sha256:dead0000beef0000",
                    drift_hash_version=1,
                    last_imported="2026-04-01T00:00:00Z",
                    source_label="Cursor (user)",
                )
            }
        )
        state.save_import_state(prior)

        _seed_claude_code(sandbox, {"newcomer": {"command": "echo"}})
        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res.exit_code == 0, res.output

        loaded = state.load_import_state()
        assert set(loaded.entries.keys()) == {"old", "newcomer"}
        # `old` row preserved byte-equal — including its older timestamp.
        assert loaded.entries["old"] == prior.entries["old"]


# ---------------------------------------------------------------------------
# Cross-host first-import-wins (same name in two hosts within one --from all)
# ---------------------------------------------------------------------------


class TestCrossHostConflict:
    def test_same_name_different_shape_across_hosts_is_conflict(self, runner, sandbox):
        # claude-code: foo with command "first"
        _seed_claude_code(sandbox, {"foo": {"command": "first"}})
        # cursor: foo with command "second"
        cursor_dir = sandbox["home"] / ".cursor"
        cursor_dir.mkdir()
        (cursor_dir / "mcp.json").write_text(
            json.dumps({"mcpServers": {"foo": {"command": "second"}}}), encoding="utf-8"
        )

        res = runner.invoke(import_command, ["--from", "all", "--apply"])
        assert res.exit_code == 0, res.output
        # First (claude-code) wins; second (cursor) reported as conflict.
        cfg = state.load_registry()
        assert cfg.servers["foo"].command == "first"
        assert "conflicts:       1" in res.output


# ---------------------------------------------------------------------------
# --allow-project-configs gate
# ---------------------------------------------------------------------------


class TestProjectConfigGate:
    """``--apply`` must not silently register MCP entries from project-local
    config (``<cwd>/.mcp.json`` / ``<cwd>/.cursor/mcp.json``) that an untrusted
    repo checkout can ship — they require explicit ``--allow-project-configs``."""

    def test_apply_aborts_on_project_local_without_flag(self, runner, sandbox):
        _seed_dot_mcp_json(sandbox, {"evil": {"command": "x"}})
        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res.exit_code == 2, res.output
        assert "--allow-project-configs" in res.output
        assert "evil" in res.output
        # Fail closed: nothing written.
        assert not state.registry_path().exists()

    def test_apply_succeeds_with_flag(self, runner, sandbox):
        _seed_dot_mcp_json(sandbox, {"local-tool": {"command": "echo"}})
        res = runner.invoke(
            import_command,
            ["--from", "claude-code", "--apply", "--allow-project-configs"],
        )
        assert res.exit_code == 0, res.output
        assert "local-tool" in state.load_registry().servers

    def test_apply_user_config_not_gated(self, runner, sandbox):
        # A user-scope (~/.claude.json) entry is trusted — applies without the flag.
        _seed_claude_code(sandbox, {"trusted": {"command": "echo"}})
        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res.exit_code == 0, res.output
        assert "trusted" in state.load_registry().servers

    def test_home_stored_project_scope_not_gated(self, runner, sandbox):
        # ``.projects.<cwd>`` lives in the user's home ~/.claude.json — a repo
        # checkout cannot ship it, so it is NOT gated.
        cwd = sandbox["cwd"].resolve()
        cfg = {"projects": {str(cwd): {"mcpServers": {"homeproj": {"command": "echo"}}}}}
        (sandbox["home"] / ".claude.json").write_text(json.dumps(cfg), encoding="utf-8")
        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        assert res.exit_code == 0, res.output
        assert "homeproj" in state.load_registry().servers

    def test_plan_notes_project_local_requirement(self, runner, sandbox):
        _seed_dot_mcp_json(sandbox, {"local-tool": {"command": "echo"}})
        res = runner.invoke(import_command, ["--from", "claude-code"])  # --plan default
        assert res.exit_code == 0, res.output
        assert "--allow-project-configs" in res.output
        # Plan never mutates.
        assert not state.registry_path().exists()


# ---------------------------------------------------------------------------
# Wire-in smoke
# ---------------------------------------------------------------------------


def test_import_wired_into_top_level_cli(runner):
    from memtomem_stm.cli.proxy import cli

    res = runner.invoke(cli, ["import", "--help"])
    assert res.exit_code == 0, res.output
    # Sanity: the help text mentions both modes
    assert "--plan" in res.output
    assert "--apply" in res.output
    assert "--show-imported" in res.output
    assert "--allow-project-configs" in res.output


class TestDisplayEscaping:
    """Host configs are plain ``json.loads`` with no character validation, so an
    imported or hand-edited one can carry anything in ``_disp_escapes``'s set
    into this listing — which is column-aligned, so a CR takes the surrounding
    rows with it. First escaping coverage for this surface (#760).
    """

    # ESC (opens an ANSI sequence), CR (rewrites the rendered line) and a lone
    # surrogate (unencodable, so it raises rather than rendering) — one per
    # subclass of the set, rather than one representative that could pass by
    # accident.
    HOSTILE = "ev\x1b[31m\ril"

    def _seed_hostile(self, sandbox):
        _seed_claude_code(
            sandbox,
            {
                "clean": {"command": "npx", "args": ["-y", "@mcp/ok"]},
                self.HOSTILE: {
                    "command": f"cmd{self.HOSTILE}",
                    "args": [],
                    "env": {"PLAIN_VALUE": f"v{self.HOSTILE}"},
                },
            },
        )

    def test_plan_listing_escapes_name_command_and_env(self, runner, sandbox):
        self._seed_hostile(sandbox)
        res = runner.invoke(import_command, ["--from", "claude-code", "--show-imported"])

        assert res.exit_code == 0, res.output
        assert "\\u001B" in res.output and "\\u000D" in res.output
        # The point: no member of the set survives raw anywhere in the render.
        assert "\x1b" not in res.output
        assert "\r" not in res.output
        # The clean sibling is untouched — escaping must not be a blanket mangle.
        assert "clean" in res.output

    def test_plan_listing_pads_by_the_displayed_width(self, runner, sandbox):
        """The name column is a literal ``:<22``. Padding the *stored* name pads
        the escaped one too narrow and raggeds every following column, which is
        the defect #755 recorded for the other aligned table."""
        self._seed_hostile(sandbox)
        res = runner.invoke(import_command, ["--from", "claude-code"])

        rows = [ln for ln in res.output.splitlines() if "command=" in ln]
        assert len(rows) == 2
        starts = {ln.index("command=") for ln in rows}
        assert len(starts) == 1, f"columns ragged: {rows}"

    def test_conflict_line_escapes_name_label_and_reason(self, runner, sandbox):
        """``reason`` is registry-derived and rendered on the same line as the
        name; the issue's site list names the name and label but not it."""
        self._seed_hostile(sandbox)
        runner.invoke(import_command, ["--from", "claude-code", "--apply"])
        # Re-import the same names from a second host with different bodies so
        # they land in the conflict branch rather than the idempotent one.
        _seed_dot_mcp_json(
            sandbox,
            {
                self.HOSTILE: {"command": "different", "args": ["x"]},
                "clean": {"command": "different", "args": ["x"]},
            },
        )
        res = runner.invoke(import_command, ["--from", "all"])

        assert res.exit_code == 0, res.output
        assert "Conflicts:" in res.output
        assert "\x1b" not in res.output
        assert "\r" not in res.output


class TestUnencodableEntryGate:
    """A host config can hold a lone surrogate — it is plain ``json.loads`` with
    no character validation, and ``"\\ud800"`` is a legal JSON escape.

    Such an entry is unusable end to end: ``compute_drift_hash`` encodes its
    canonical form, TOML cannot represent the character at all, and the name is
    a cache-key component. Before the gate, one of them aborted the whole
    ``--apply`` with an uncaught ``UnicodeEncodeError`` and **nothing** was
    imported, so a clean sibling was lost to an unrelated entry's defect (#761).
    """

    @staticmethod
    def _seed(sandbox, *, where: str):
        bad = {"command": "npx", "args": [], "env": {}}
        if where == "env":
            bad["env"] = {"TOK": "a\ud800b"}
        elif where == "command":
            bad["command"] = "np\ud800x"
        elif where == "args":
            bad["args"] = ["--flag=\ud800"]
        entry = {"clean": {"command": "npx"}, "bad": bad}
        if where == "name":
            entry = {"clean": {"command": "npx"}, "b\ud800d": {"command": "npx"}}
        _seed_claude_code(sandbox, entry)

    @pytest.mark.parametrize("where", ["env", "command", "args", "name"])
    def test_apply_skips_the_entry_and_still_imports_its_sibling(self, runner, sandbox, where):
        self._seed(sandbox, where=where)
        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])

        assert res.exit_code == 0, res.output
        assert res.exception is None
        registry = state.load_registry()
        assert "clean" in registry.servers
        assert len(registry.servers) == 1

    def test_the_skip_names_the_field_but_never_its_value(self, runner, sandbox):
        """Env values are routinely secrets, and this text reaches CI logs —
        the same rule ``mms add`` and the discovery scan follow."""
        self._seed(sandbox, where="env")
        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])

        assert "TOK" in res.output
        assert "not valid UTF-8" in res.output
        assert "\ud800" not in res.output

    def test_plan_reports_the_skip_before_anything_is_written(self, runner, sandbox):
        self._seed(sandbox, where="env")
        res = runner.invoke(import_command, ["--from", "claude-code"])

        assert res.exit_code == 0, res.output
        assert "not valid UTF-8" in res.output

    def test_a_clean_import_is_completely_unaffected(self, runner, sandbox):
        _seed_claude_code(sandbox, {"a": {"command": "npx"}, "b": {"command": "uvx"}})
        res = runner.invoke(import_command, ["--from", "claude-code", "--apply"])

        assert res.exit_code == 0, res.output
        assert "not valid UTF-8" not in res.output
        assert set(state.load_registry().servers) == {"a", "b"}
