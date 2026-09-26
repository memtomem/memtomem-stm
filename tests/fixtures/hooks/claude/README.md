# Claude Code hook fixtures

Source: `code.claude.com/docs/en/hooks` — "Common input fields" and `#posttooluse`
(verified 2026-09-26). Confidence: high for field names.

Claude Code is still the in-code baseline for parse/render (`tests/cli/test_hook_cmd.py`);
these two inbound payloads exist to pin the host **call identifiers** the adapter reads.

## Contract (verified)

- **Common fields:** `session_id` ("Current session identifier"), `transcript_path`,
  `cwd` ("Current working directory when the hook is invoked"), `hook_event_name`.
  `permission_mode` is documented as "Not all events receive this field"; the PostToolUse
  example includes it. Newer versions add `prompt_id` / `scratchpad_dir` / `effort`.
- **Subagent-only fields.** Verbatim: "When running with `--agent` or inside a subagent, two
  additional fields are included" — `agent_id`: "Present only when the hook fires inside a
  subagent call. Use this to distinguish subagent hook calls from main-thread calls." and
  `agent_type` (also present under `--agent` on the main thread).
- **PostToolUse-specific:** `tool_name`, `tool_input`, `tool_use_id`, `tool_response`
  ("The exact schema for both depends on the tool"), and an optional `duration_ms`.

## Files

- `inbound_read_posttooluse.json` — main-thread `Read`; no `agent_id`.
- `inbound_read_posttooluse_subagent.json` — the same call inside an `Explore` subagent;
  carries `agent_id` / `agent_type` and its own `tool_use_id`.

Field **names** are doc-verified; field **values** are illustrative. The docs' PostToolUse
example is a `Write` call; the `Read` `tool_response` shape here is not documented and is
illustrative only (the adapter flattens any shape through `_tool_response_to_text`).
