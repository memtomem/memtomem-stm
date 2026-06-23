# Kimi Code hook fixtures

Source: `moonshotai.github.io/kimi-cli/en/customization/hooks` + `.../configuration/config-files`
+ repo `github.com/MoonshotAI/kimi-cli` (verified 2026-06-23). Confidence: high.

## Contract (verified)

- **Event:** `PostToolUse` (one of 13 lifecycle events; `PostToolUseFailure` is distinct).
  Genuine post-tool external-command hook.
- **Inbound (stdin JSON, snake_case):** `session_id`, `cwd`, `hook_event_name`, `tool_name`
  (values **PascalCase** — `Shell` / `WriteFile` / `StrReplaceFile` / `ReadFile`),
  `tool_input`, `tool_output`. ✅ field list verified (no full verbatim sample in docs).
- **Surfacing channel — raw stdout on exit 0.** Verbatim exit-code row:
  `| 0 | Allow | stdout content (if non-empty) is added to context |`. The hook prints the
  surfaced block to **stdout** and exits 0 — there is **no `additionalContext` JSON key**
  (it appears nowhere in Kimi docs). ✅ verified.
- **Output replace — NONE.** The only structured stdout JSON is
  `hookSpecificOutput { hookEventName, permissionDecision: "allow"|"deny",
  permissionDecisionReason }` — an allow/deny gate, not a rewrite. ❌ no compression channel
  (native or MCP) → no output-replace fixture.
- **Exit:** `0` = allow (stdout → context) · `2` = block (stderr → LLM as correction) ·
  other = allow (stderr logged only).
- **Config:** `~/.kimi/config.toml`, TOML `[[hooks]]` array (`event`/`matcher`/`command`/`timeout`).
  MCP at `~/.kimi/mcp.json`. ⚠️ base dir is **`~/.kimi/`**, NOT the plan's provisional
  `~/.kimi-code/`.

## Unverified

Whether `PostToolUse` fires for MCP tools vs native-only; whether the exit-0 stdout inject
must be pure JSON or arbitrary text injected verbatim (strictness unspecified — these fixtures
assume arbitrary text is injected verbatim).

## Files

- `inbound_shell_posttooluse.json` — PostToolUse payload (documented field names; `Shell`
  tool value; illustrative field values).
- `expected_surfacing_stdout.txt` — the canonical surfaced block as **literal stdout** (the
  hook prints it and exits 0; the channel is plain stdout, not a JSON key). The file stores the
  block bytes only — **byte-identical** to the Cursor/Codex `additional_context` values. A live
  hook additionally emits one trailing newline (stdout framing from `print`/`echo`); that
  newline is not part of the canonical block, so it is omitted here to keep the three fixtures
  byte-identical. The Kimi adapter's `render()` is responsible for that stdout framing.
