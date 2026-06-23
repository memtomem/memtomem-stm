# Host hook contract fixtures (`tests/fixtures/hooks/`)

Golden inbound-payload + expected-output fixtures for the per-host hook adapters
(Track B, plan `mcp-serene-pinwheel.md`). Each `<host>/` directory captures, for one host:

- `inbound_<tool>_posttooluse.json` — a realistic **inbound** PostToolUse payload the host
  writes to the hook subprocess on stdin.
- `expected_surfacing_*.{json,txt}` — the **correct stdout** `mms hook` should emit to inject
  surfaced memories (the *surfacing* channel) for that host's contract.

These are the golden files for the B1 `HostHookAdapter` `parse()` / `render()` tests
(`inbound → CanonicalHookCall`, and a canonical surfaced block `→` host-shaped stdout).

## Provenance — B0 (verified 2026-06-23)

Field names, event names, output shapes, and config locations here are pinned to current
official docs and adversarially re-verified. Full report + citations + corrected capability
matrix: `docs/reports/b0-host-hook-contract-verification-2026-06-23.md`.

In each fixture, **field _names_ are doc-verified; field _values_ are illustrative** (test
data), except where a value is itself documented verbatim (e.g. Cursor's `tool_name: "Shell"`
example). Per-host caveats and the verified/unverified marks live in each `<host>/README.md`.

## Scope — what these fixtures do and do NOT cover

- **Surfacing only.** B0 verified that native-tool output **compression (replacement) ports
  to NO non-Claude host** (Cursor `updated_mcp_tool_output` = MCP-only; Kimi = no field;
  Codex `updatedMCPToolOutput` = "parsed but not supported yet" + MCP-only). So no host
  fixture emits an output-replace field — compression stays Claude-Code-only.
- **Canonical surfaced block (shared across hosts).** All three `expected_surfacing_*`
  fixtures carry the **same** surfaced-memories text, so a B1 render test can feed one
  canonical block and assert each host wraps it correctly. The canonical block is:

  ```
  Relevant memories (memtomem LTM):
  - Rotate the service token with `mms rotate`; do not edit config.toml by hand.
  - Point integration-test feedback_db_path at /tmp, never ~/.memtomem.
  ```

  (This mirrors the content `mms hook` puts in Claude Code's
  `hookSpecificOutput.additionalContext` — the `<surfaced-memories>` delimiters are sliced
  off by `SurfacingFormatter`, so the channel carries just the memories.)

## Hosts present / absent

| Host | Status | Surfacing channel |
|---|---|---|
| `cursor/` | ✅ fixtured | flat `additional_context` (string) — **runtime no-op today, staff-confirmed bug** |
| `kimi/` | ✅ fixtured | raw **stdout** text on exit 0 (not a JSON key) |
| `codex/` | ✅ fixtured | `hookSpecificOutput.additionalContext` (same shape as Claude) |
| **Claude Code** | not here | in-code baseline — covered by `tests/cli/test_hook_cmd.py` |
| **Antigravity** | ❌ **deliberately absent** | unfixturable: PostToolUse tool-output stdin field is undocumented and the official docs are an unreadable SPA. Writing a fixture now would encode guesses (the exact risk B0 exists to prevent). Documented gap — see the B0 report. |
