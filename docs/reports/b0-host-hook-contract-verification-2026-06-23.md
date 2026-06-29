# B0 — Non-Claude host hook-contract verification (2026-06-23)

**Gate:** Phase 2 / Track B hard gate from `~/.claude/plans/mcp-serene-pinwheel.md`.
Pin each non-Claude MCP host's tool-call **hook** contract to current official docs,
confirm or correct the plan's provisional matrix, and decide what of the `mms hook`
pattern (PostToolUse → compression via output-replace + surfacing via context-inject,
fail-open) actually ports per host.

**Method:** per host, one research agent located the current official docs and filled a
7-dimension contract with `{claim, url, verbatim quote}` citations; an independent
adversarial verifier re-fetched the official sources and tried to refute the two
load-bearing fields (output-replace, context-inject). Hosts: Cursor, Kimi Code,
Antigravity, Codex CLI. Claude Code is the code-verified baseline (not re-run).

**Verification provenance:** all primary claims rest on official sources — vendor docs
sites (`cursor.com/docs/hooks`, `moonshotai.github.io/kimi-cli`, `developers.openai.com/codex/hooks`)
and official GitHub orgs (`MoonshotAI/kimi-cli`, `google-antigravity/*`, `openai/codex`).
Cursor's runtime caveat is from the official Cursor forum (staff reply). Antigravity's
authoritative docs site (`antigravity.google/docs/*`) is a JS-rendered SPA unreadable to
fetchers, so its evidence leans on the official GitHub org (SDK README + CLI CHANGELOG)
plus weak community corroboration — hence its lower confidence.

---

## Headline: the corrected capability matrix

Legend: ✅ verified-yes · ❌ verified-no · ⚠️ corrected/caveated · ❓ unverifiable from official docs

| Host | Post-tool ext-cmd hook | **Compression** (replace *native* output) | **Surfacing** (inject context) | Fail-open on hook crash? | Config | Confidence |
|---|---|---|---|---|---|---|
| **Claude Code** (baseline) | ✅ PostToolUse | ✅ `hookSpecificOutput.updatedToolOutput` (built-ins incl. Bash) | ✅ `hookSpecificOutput.additionalContext` | ✅ yes (exit 0) | `~/.claude/settings.json` JSON | ✅ code-verified |
| **Cursor** | ✅ `postToolUse` (generic, all tools) | ❌ `updated_mcp_tool_output` is **MCP-only** — no native rewrite field | ⚠️ `additional_context` documented **but runtime no-op** (staff-confirmed bug) | ✅ yes (non-0/non-2 = pass-through) | `~/.cursor/hooks.json` JSON `{version:1}` | high |
| **Kimi Code** | ✅ `PostToolUse` | ❌ **no output-replace field at all** (only allow/deny gate) | ✅ raw **stdout-on-exit-0** ("added to context") — *not* an `additionalContext` key | ✅ yes (non-0/non-2 = allow+log) | `~/.kimi/config.toml` TOML `[[hooks]]` | high |
| **Antigravity** | ✅ `PostToolUse` (+ in-proc Python SDK) | ❌ **no mechanism** — post-tool hook is Inspect-only / decision-only | ❓ no field found; docs SPA unreadable (leaning no) | ❌ **INVERTED: non-zero exit = DENY** (fail-closed on PreToolUse) | `~/.gemini/config/hooks.json` JSON | medium |
| **Codex CLI** | ✅ `PostToolUse` (≠ old `notify`) | ⚠️ `updatedMCPToolOutput` exists but **"parsed but not supported yet"** + MCP-only; native = no field | ⚠️ `hookSpecificOutput.additionalContext` ✅ but plain-stdout **ignored**; standalone exit-0 inject undocumented | ✅ yes (exit 2 = block, else continue) | `~/.codex/config.toml` `[hooks]` / `hooks.json` TOML | high |

### Two findings that change the Track-B plan

1. **Compression does NOT port to native tools on any of the four hosts.** Every host
   either scopes output-replacement to MCP tools (Cursor, Codex), provides no replace
   field (Kimi), or has only an Inspect/decision post-tool hook (Antigravity). On current
   official docs, **native-tool output-replacement is a Claude-Code-exclusive capability.**
   → Track B is a **surfacing-only** story across hosts; the B2 line "compression only
   enables where output replacement is verified" resolves to *nowhere but Claude Code*.

2. **"Surfacing degrades to any host that can inject context" is only partly true.**
   - **Kimi** — clean (stdout-on-exit-0).
   - **Codex** — works via `additionalContext`, with caveats (plain stdout ignored;
     standalone non-block inject undocumented — every official example nests it under
     `decision:"block"`).
   - **Cursor** — documented field is **a runtime no-op today** (staff-confirmed bug,
     v2.6.20→still broken v3.7.36). Surfacing cannot be demonstrated end-to-end on Cursor
     right now.
   - **Antigravity** — unverifiable (no field found; docs unreadable).

   → Per-host surfacing capability must be **runtime-verified**, not docs-derived. Only
   Kimi is unconditionally green today.

---

## Per-host verified contracts

### Cursor — `cursor.com/docs/hooks` (high; research-trustworthy)

- **Hook:** `postToolUse` is a real command-based external hook, generic across ALL tools
  (Shell/Read/Write/MCP), distinct from MCP-specific `beforeMCPExecution`/`afterMCPExecution`.
  Communicates "over stdio using JSON in both directions."
- **Output replace — MCP-ONLY (load-bearing).** Field `updated_mcp_tool_output`, an
  **object** (e.g. `{ "modified": "output" }`), emitted from `postToolUse` stdout.
  Verbatim: *"For MCP tools only: replaces the tool output seen by the model."* No field
  exists to rewrite native/built-in (Shell/Read/Write/file-op) output. → **Compression
  does not port for native tools.**
- **Context inject — documented, runtime-broken (load-bearing).** Field `additional_context`,
  a **string**; verbatim *"Extra context injected into the conversation after the tool
  result"*; not scoped to MCP. **BUT** the official Cursor forum has a staff reply (Mohit
  Jain, 2026-03-24) confirming `additional_context` is *"accepted and validated by the hook
  runner... but the value is never actually injected into the model's conversation context."*
  Reported v2.6.20 (Mar 2026), still broken at v3.7.36 (Jun 2026), no fix shipped as of
  verification. → Surfacing is **a no-op in practice** today.
- **Payload (json-stdin):** `tool_name` (value capitalized: `"Shell"`/`"Read"`/`"Write"`),
  `tool_input` (object), `tool_output` (**JSON-stringified string**), `tool_use_id`, `cwd`,
  `duration` (ms) + base fields (`conversation_id`, `model`, `hook_event_name`, …).
- **Output keys are FLAT top-level** (not nested under `hookSpecificOutput`), snake_case.
- **Exit:** 0 = use JSON output · 2 = block (== permission deny) · other = fail-open pass-through.
- **Config:** `<proj>/.cursor/hooks.json` or `~/.cursor/hooks.json` (+ enterprise paths),
  schema `{version:1, hooks:{<event>:[{command}]}}`.
- **Unresolved:** stdout strictness for interleaved text; `additional_context` on
  `postToolUseFailure`; merge-vs-replace semantics of `updated_mcp_tool_output`; full native
  tool-name enumeration; the `additional_context` bug-fix status.

### Kimi Code — `moonshotai.github.io/kimi-cli` (high; research-trustworthy)

- **Hook:** genuine post-tool external-command hook; 13 lifecycle events
  (`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, … `Notification`).
- **Output replace — NONE (load-bearing).** The only structured stdout JSON is
  `hookSpecificOutput { hookEventName, permissionDecision: "allow"|"deny",
  permissionDecisionReason }` — an allow/deny gate, **not** a rewrite. GitHub-raw source
  search: `additionalContext`/`updatedToolOutput`/`replace` all **not found**. → **No
  compression channel at all** (not native, not MCP).
- **Context inject — via raw stdout (load-bearing).** Verbatim exit-code row:
  *"| 0 | Allow | stdout content (if non-empty) is added to context |"*. Mechanism is
  **plain stdout on exit 0**, NOT a named `additionalContext` key. → Surfacing ports, but
  you print text rather than emit a JSON field.
- **Payload (json-stdin, snake_case):** `session_id`, `cwd`, `hook_event_name`, `tool_name`
  (values **PascalCase**: `Shell`/`WriteFile`/`StrReplaceFile`/`ReadFile`), `tool_input`,
  `tool_output`.
- **Exit:** 0 = allow (stdout→context) · 2 = block (stderr→LLM correction) · other = allow
  (stderr logged only).
- **Config:** `~/.kimi/config.toml`, TOML `[[hooks]]` array (`event`/`matcher`/`command`/`timeout`);
  MCP at `~/.kimi/mcp.json`. ⚠️ **Provisional matrix's `~/.kimi-code/*` is WRONG — base dir
  is `~/.kimi/`.**
- **Unresolved:** whether `PostToolUse` fires for MCP vs native-only; whether exit-0 stdout
  must be pure JSON or arbitrary text injected verbatim.

### Antigravity — Google (medium; research-trustworthy within the SPA-docs limit)

- **Hook:** confirmed real post-tool external-command hook via official CLI **CHANGELOG
  v1.0.8** (the `/hooks` command writes `~/.gemini/config/hooks.json`). There are **two
  systems**: (a) external-command JSON hooks (the `mms hook` analog) and (b) an in-process
  **Python SDK** (decorators; Inspect/Decide/Transform categories) — a different
  integration model, not a subprocess.
- **Output replace — NO mechanism (load-bearing).** SDK README (read via both rendered +
  raw GitHub): `PostToolCallHook` is an **Inspect** hook — *"They receive data but cannot
  modify it."* Only **Transform** hooks modify, and the only post-tool Transform is
  `OnToolErrorHook` (error path, not normal output). External-command path documents only
  `{decision: allow|deny|ask, reason}`. No `updatedToolOutput` analog anywhere. → **No
  compression** (native or MCP).
- **Context inject — UNVERIFIABLE (load-bearing).** No Antigravity `additionalContext` /
  context-injection field found in any source; the post-tool SDK hook is read-only;
  external-command stdout is decision/reason only. (Gemini CLI *does* document
  `hookSpecificOutput.additionalContext`, but on a differently-named/shaped system —
  `BeforeTool`/`AfterTool`, `tool_name`/`tool_response` — so it can't be assumed to carry
  over.) The authoritative `antigravity.google/docs/hooks` page is a JS-rendered SPA
  returning empty body to fetchers → genuinely unverifiable, not a definitive "no".
- **⚠️ Fail-mode is INVERTED (critical).** Per cmux issue #4768: a non-zero hook exit on
  `PreToolUse` is interpreted as a **DENY (fail-closed) — blocking every tool call**.
  Passthrough requires emitting `{}` and exit 0. This is the **opposite** of `mms hook`'s
  fail-open assumption; a crashing/unavailable hook here could block all tool use.
- **Payload (json-stdin):** nested **`toolCall.name` / `toolCall.args`** (camelCase) —
  differs from every other host's flat `tool_name`. The field carrying the tool
  **result/output** for the external-command PostToolUse hook is **unknown** (undocumented
  in any readable source). Built-in tool names are snake_case (`run_command`, `view_file`,
  `edit_file`, `read_file`).
- **Config:** `~/.gemini/config/hooks.json` (global, official) + `.agents/hooks.json`
  (workspace, community), JSON.
- **Unresolved (many):** any output-replace/inject field; the tool-output stdin field;
  whether `AfterTool`→`PostToolUse` (only `BeforeTool`→`PreToolUse` is explicitly stated);
  MCP config path; PostToolUse non-zero exit semantics; all verbatim strictness/enum
  statements (blocked by SPA docs).

### Codex CLI — `developers.openai.com/codex/hooks` (high; **research-needs-correction**)

- **Hook:** real `PostToolUse` external-command hook, distinct from the older `notify`
  (which fires only on `agent-turn-complete`). Events: `SessionStart`, `SubagentStart`,
  `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`,
  `UserPromptSubmit`, `SubagentStop`, `Stop`. Enabled by default
  (`[features] hooks = false` to disable).
- **Output replace — field exists but inert + MCP-only (load-bearing; CORRECTED).** Verbatim:
  *"updatedMCPToolOutput and suppressOutput are parsed but not supported yet."* So a
  rewrite field exists in the schema, but (a) it is **non-functional today**, and (b) it is
  **MCP-only** by name; native `Bash`/`apply_patch` have no rewrite field at all. The only
  working post-tool substitution is `decision:"block"+reason` — *"Codex records the
  feedback, replaces the tool result with that feedback, and continues the model from the
  hook-provided message"* — i.e. block-feedback text, **not** transparent output rewrite.
  `PreToolUse` has `updatedInput` (rewrites tool **input**, not output). → **Compression
  does not port.**
- **Context inject — `additionalContext` confirmed, with caveats (load-bearing; CORRECTED).**
  Verbatim: `{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"…"}}`
  + *"That additionalContext text is added as extra developer context."* **Same field name
  as Claude Code.** Caveats: (a) for PostToolUse, *"Plain text on stdout is ignored"* — the
  stdout-as-context behavior applies only to `SessionStart`/`UserPromptSubmit`/`SubagentStart`,
  so the research agent's "plain stdout → context" sub-claim was **refuted**; (b) every
  official `additionalContext` example is nested under `decision:"block"` — standalone exit-0
  injection is undocumented (verify at runtime).
- **Coverage gaps (verbatim):** *"this doesn't intercept WebSearch or other non-shell,
  non-MCP tool calls"*; only "simple" shell calls are intercepted; community reports
  (weak) say apply_patch/MCP firing is flaky in practice.
- **Payload (json-stdin, snake_case):** `tool_name` (`Bash`/`apply_patch`/`mcp__<server>__<tool>`),
  `tool_input`, `tool_response` (output field), `tool_use_id`, `turn_id` + common fields.
- **Exit:** 0 = continue · 2 = block (reason to stderr) · other = fail-open-ish. → fail-open
  maps cleanly.
- **Config:** `~/.codex/config.toml` `[hooks]` / `~/.codex/hooks.json` (+ project-level),
  TOML; managed governance via `requirements.toml allow_managed_hooks_only`.
- **Unresolved:** whether the `decision:block` feedback path can carry arbitrary compressed
  content vs being model-labeled as a correction; real-world apply_patch/MCP reliability.

---

## Corrections to the plan's provisional matrix

| Cell | Provisional (plan) | Verified | Change |
|---|---|---|---|
| Cursor output-replace | `updated_mcp_tool_output` "may be MCP-only; verify" | **MCP-only, confirmed**; no native field | confirmed + tightened |
| Cursor surfacing | "likely via post-hook context" | `additional_context` documented **but runtime no-op (staff-confirmed bug)** | corrected (works on paper, broken in practice) |
| Kimi config path | `~/.kimi-code/config.toml`, MCP `~/.kimi-code/mcp.json` | **`~/.kimi/config.toml`, `~/.kimi/mcp.json`** | **corrected (wrong dir)** |
| Kimi output-replace | `hookSpecificOutput` "replace capability unconfirmed" | **no replace field at all** (allow/deny gate only) | corrected (none) |
| Kimi surfacing | "unconfirmed (`additionalContext`-equiv?)" | **raw stdout-on-exit-0**, not an `additionalContext` key | resolved (different mechanism) |
| Antigravity output-replace | "only Transform hooks modify" | confirmed — but the **post-tool slot is Inspect-only**, so **no output replace** on the success path | corrected (Transform exists, but not post-tool) |
| Antigravity surfacing | "unconfirmed" | **unverifiable** (no field; SPA docs) | unresolved (leaning no) |
| Antigravity fail-mode | (not noted) | **fail-CLOSED: non-zero exit = deny** | **new critical finding** |
| Antigravity config | `~/.gemini/config/hooks.json` (provisional) | **confirmed** via official CLI CHANGELOG v1.0.8 | confirmed |
| Codex output-replace | "partial / TBD" | `updatedMCPToolOutput` exists but **"parsed but not supported yet" + MCP-only**; native = none | corrected (precise) |
| Codex surfacing | "partial / TBD" | `hookSpecificOutput.additionalContext` ✅ (same as Claude); plain stdout **ignored** | corrected (precise) |

---

## Recommended Track-B re-prioritization

The plan's host ordering was **Cursor first** ("closest contract"). The verified data
suggests revising:

- **Cursor's surfacing is runtime-broken** (the one capability that ports is a no-op today)
  → it cannot demonstrate end-to-end value until Cursor ships the `additional_context` fix.
  Demote from "first" until the bug is resolved (track the forum thread).
- **Kimi Code** has clean, working surfacing (stdout-on-exit-0) and complete official docs
  → **strongest first generalization target** for the surfacing-only bridge.
- **Codex CLI** has the closest *working* analog to Claude's field name
  (`hookSpecificOutput.additionalContext`) and thorough docs → strong second, with the
  standalone-inject + coverage-gap caveats documented.
- **Antigravity last** — least verifiable (SPA docs), no confirmed inject field, and the
  fail-closed inversion makes a naive port dangerous. Treat as research/observability-only
  until the docs become machine-readable or the SDK path is adopted.

Concrete implications for the B1–B4 abstraction:
- **`HostCapabilities` must split `can_inject_context` into documented vs runtime-verified**
  — Cursor is the counterexample where docs say yes but runtime says no.
- **`safe_emit` / failure-open hardening (B4) must encode per-host exit-code semantics** —
  Antigravity's non-zero = deny means a crashing hook there is *not* safe; the adapter must
  guarantee `{}` + exit 0 on every error path, never a non-zero exit.
- **Compression is out of scope for the cross-host adapter** — keep it Claude-Code-only;
  the canonical `render()` should omit any output-replace field for non-Claude hosts (or,
  for Cursor/Codex, only ever target MCP tools, which already go through the proxy — i.e.
  redundant). This simplifies B2's capability gating to essentially "surfacing where
  runtime-verified."

---

## Fixture readiness (`tests/fixtures/hooks/<host>/`)

The plan's other B0 deliverable is golden inbound-payload + expected-output fixtures.
Readiness varies — **not all hosts can be faithfully fixtured yet**:

- **Cursor** — ✅ inbound documented verbatim; expected output = flat `{additional_context}`
  (surfacing). Note in the fixture that runtime injection is currently a no-op.
- **Kimi Code** — ✅ payload fields documented (no full verbatim sample); expected output =
  plain stdout text + exit 0. Fixture should assert the stdout channel, not a JSON key.
- **Codex CLI** — ✅ inbound field list + expected output `{hookSpecificOutput:{hookEventName,
  additionalContext}}` documented verbatim.
- **Antigravity** — ❌ **cannot be faithfully fixtured.** The PostToolUse external-command
  **tool-output stdin field is unknown**, no verbatim inbound sample exists, and the inject
  field is unverifiable. Writing a fixture now would encode guesses — exactly the risk B0
  exists to prevent. Defer until docs are readable or captured from a live install.

Recommendation: scaffold fixtures for **Cursor / Kimi / Codex** only; leave Antigravity as
a documented gap. (Not written in this pass — pending go-ahead, since `tests/fixtures/` is
committed and the host set to pursue is a decision for the user.)

---

## Bottom line

B0 gate **resolved**. The contracts are pinned to current official docs and adversarially
verified. The provisional matrix had two material errors (Kimi config dir; Antigravity
"Transform can modify" applied to the wrong slot) and several "TBD/may-be" cells now
confirmed. The strategic verdict for Track B: **it is a surfacing-only effort** —
native-tool compression is Claude-Code-exclusive across all five hosts — and **surfacing
itself only works cleanly on Kimi today** (Codex with caveats; Cursor runtime-broken;
Antigravity unverifiable). If Track B proceeds, target **Kimi → Codex** first, treat
compression as out-of-scope for non-Claude hosts, and make failure-open per-host-aware
(Antigravity's fail-closed inversion is a correctness hazard).
