# Codex CLI hook fixtures

Source: `developers.openai.com/codex/hooks` + `github.com/openai/codex/blob/main/docs/config.md`
(verified 2026-06-23). Confidence: high (research-needs-correction — two provisional cells
corrected; see B0 report).

## Contract (verified)

- **Event:** `PostToolUse` (distinct from the older `notify`, which fires only on
  `agent-turn-complete`). Runs after Bash, apply_patch, and MCP tool calls. ⚠️ Does **not**
  intercept WebSearch or other non-shell/non-MCP tools; only "simple" shell calls
  (apply_patch/MCP firing is community-reported as flaky). Enabled by default
  (`[features] hooks = false` to disable).
- **Inbound (stdin JSON, snake_case):** `tool_name` (`Bash` / `apply_patch` /
  `mcp__<server>__<tool>`), `tool_input`, `tool_response` (output field), `tool_use_id`,
  `turn_id` + common fields (`session_id`, `transcript_path`, `cwd`, `hook_event_name`,
  `model`, `permission_mode`). ✅ field list verified (no full verbatim sample in docs).
- **Surfacing channel — `hookSpecificOutput.additionalContext`** (same shape & field name as
  Claude Code). ✅ verified verbatim:
  `{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"…"}}` +
  "That additionalContext text is added as extra developer context."
- **Output replace — does NOT port.** `updatedMCPToolOutput` exists in the schema but is
  **"parsed but not supported yet"** AND MCP-only; native (Bash/apply_patch) has no rewrite
  field. The only working post-tool substitution is `decision:"block"+reason`
  (block-feedback text, not transparent rewrite). ❌ no output-replace fixture.
- **Exit:** `0` = continue · `2` = block (reason to stderr) · other = fail-open-ish.
- **Config:** `~/.codex/config.toml` `[hooks]` or `~/.codex/hooks.json` (+ project-level),
  TOML; managed governance via `requirements.toml allow_managed_hooks_only`.

## ⚠️ Caveats (load-bearing)

1. **Plain stdout is IGNORED for PostToolUse** (unlike Kimi). Verbatim: "Plain text on stdout
   is ignored." The stdout-as-context behavior applies only to SessionStart / UserPromptSubmit
   / SubagentStart. → the surfacing fixture MUST use the structured `additionalContext` field,
   never bare stdout.
2. **Standalone (non-block) `additionalContext` is undocumented.** Every official example
   nests `additionalContext` under `decision:"block"`. This fixture emits the **standalone
   exit-0** shape (the natural mms-hook surfacing emission); a B1 runtime check must confirm
   Codex honors `additionalContext` without `decision:"block"` before enabling by default.

## Files

- `inbound_bash_posttooluse.json` — PostToolUse payload for a Bash tool call (documented
  field names; illustrative values).
- `expected_surfacing_output.json` — correct surfacing emission:
  `{ hookSpecificOutput: { hookEventName: "PostToolUse", additionalContext } }`, carrying the
  canonical surfaced block.
