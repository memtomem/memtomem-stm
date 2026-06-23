# Cursor hook fixtures

Source: `cursor.com/docs/hooks` (verified 2026-06-23, schema `version: 1`). Confidence: high.

## Contract (verified)

- **Event:** `postToolUse` — generic command-based hook, fires for ALL tools
  (Shell/Read/Write/MCP). Communicates "over stdio using JSON in both directions".
- **Inbound (stdin JSON):** `tool_name` (snake_case key; value capitalized — `"Shell"`,
  `"Read"`, `"Write"`), `tool_input` (object), `tool_output` (**JSON-stringified string**),
  `tool_use_id`, `cwd`, `duration` (ms) + base fields. ✅ verified verbatim.
- **Output (stdout JSON) — FLAT top-level keys, snake_case** (NOT nested under
  `hookSpecificOutput`):
  - `additional_context` (string) — surfacing channel. ✅ field/type/scope verified verbatim
    ("Extra context injected into the conversation after the tool result"; not MCP-scoped).
  - `updated_mcp_tool_output` (object) — output replace, **MCP-tools ONLY** ("For MCP tools
    only: replaces the tool output seen by the model"). ❌ no native-tool equivalent →
    **omitted from these fixtures** (compression does not port).
- **Exit:** `0` = use JSON output · `2` = block (== permission deny) · other = fail-open
  pass-through.
- **Config:** `~/.cursor/hooks.json` or `<proj>/.cursor/hooks.json`, JSON
  `{version:1, hooks:{<event>:[{command}]}}`.

## ⚠️ Runtime caveat (load-bearing)

`additional_context` is **documented but currently a runtime no-op** — staff-confirmed bug
(Cursor forum, Mohit Jain 2026-03-24): the value is "accepted and validated by the hook
runner... but never actually injected into the model's conversation context." Reported
v2.6.20 → still broken v3.7.36 (Jun 2026). The fixture encodes the **documented-correct
contract**; a B1 runtime check must confirm injection actually works before enabling Cursor
surfacing by default. Track: forum thread `t/155689`.

## Unverified

stdout strictness for interleaved non-JSON text; `additional_context` on `postToolUseFailure`;
merge-vs-replace semantics of `updated_mcp_tool_output`; full native tool-name enumeration.

## Files

- `inbound_shell_posttooluse.json` — verbatim-shaped Shell PostToolUse payload (the docs'
  own example, plus base fields).
- `expected_surfacing_output.json` — correct surfacing emission: `{ additional_context }`,
  flat top-level, carrying the canonical surfaced block.
