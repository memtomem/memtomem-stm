# Changelog

All notable changes will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)

Releases whose entries carry a `**Behavior change**:` marker open with an
**Upgrade notes** block summarizing them — read that block first when
upgrading. The convention starts after 0.1.31; older releases record behavior
changes inline only. See the deprecation policy in
[README](README.md#compatibility--deprecation-policy).

## [Unreleased]

### Upgrade notes

- **Behavior change**: STM can now export its own spans over OTLP/HTTP to an
  OpenTelemetry collector (`MEMTOMEM_STM_OTLP__ENABLED=1`). This is a new
  **outbound network path** and is off by default; enabling it requires the
  new `otlp` extra. Exported attributes are body-free by construction — no
  response content, no error messages, no tool arguments. See
  [OTLP Span Export](docs/otlp-export.md).
- **Behavior change**: root STM startup-configuration validation errors no
  longer render pydantic's `input_value`. A block that failed to coerce as a
  whole previously echoed it verbatim in the error the server logs and
  surfaces to MCP clients, which could include `otlp.headers` or
  `langfuse.secret_key`. Two limits worth knowing: it covers errors raised
  through `STMConfig` (validating a sub-model directly still renders
  `input_value`), and it covers that field only — a validator that
  deliberately interpolates a value into its own message (e.g. `daemon.host`)
  still shows it.

- **Behavior change**: Toolgraph identity-bearing request and verdict fields
  that contain a lone surrogate are now refused as protocol errors under
  `on_protocol_error`; they are never non-injectively escaped. That knob
  defaults to `fail_start`, so an `eligible_tools` payload carrying one
  refuses server startup rather than degrading — and the response check is
  deliberately whole-payload, covering identity fields STM does not itself
  read (`eligible`, `tool_key`). Set the knob to `open` or `closed` if you
  would rather degrade than block on a provider you do not control. Request
  validation runs before the consult cache branch, so cold and warm starts have
  the same enforcement posture; because a cached row stores only the verdict's
  raw facts, response-field identities cannot be revalidated from it, so rows
  written before this policy are dropped and one full consult re-runs after
  upgrade. Stored SQLite identifiers in compression feedback, metrics,
  progressive-read telemetry, and surfacing feedback are likewise refused
  rather than rewritten. (#788, fixes #783)
- The response cache is upgraded to schema v5 and performs a one-time cache
  reset on first run after upgrade: the key derivation is now framed, so every
  stored key changes and the pre-upgrade rows are unreachable. No
  configuration or API change; entries repopulate on use. (#795, fixes #784)
- **Behavior change**: `mms stats --tool` now refuses a filter that is not
  valid UTF-8 as a usage error (exit 2) instead of reporting an all-time zero
  (`--json`) or dying with `UnicodeEncodeError` on the filter echo (human
  form). (#788, fixes #783)

### Added

- docs: record the ecosystem integration decision as ADR 0001 — per-boundary
  contracts with named deferral gates (tracegraph, agent-guard, vigil,
  syncmill) instead of a generic adapter, plus a `docs/adr/` index and drift
  pins for the paths and claims the ADR makes. Stage ordering/status lives in
  the tracking issue (#789). (#790)
- observability: opt-in OTLP/HTTP span export, satisfying ADR 0001's
  `otlp-telemetry-export` gate. Spans carry real W3C trace/span ids and real
  in-process parentage — `proxy_call` with the pipeline stages and a new
  `upstream_rpc` span nested under it — rather than anything reconstructed
  from the selection log. Attributes are admitted per span and only when STM
  itself derived the value, so response content, error messages, tool
  arguments and configured header values never become telemetry. (Headers are
  still sent as HTTP headers on the export request — that is what
  authenticates STM to the collector.)
  Enable with the `otlp` extra and `MEMTOMEM_STM_OTLP__*`; counters surface
  in `stm_proxy_health`. (#789)

### Fixed

- proxy: derive the response-cache key from framed components so it is
  injective over the serialized
  `(server, tool, args, context_query, config_fingerprint)` — `args` by its
  JSON rendering, which is what the upstream tool receives, so trees that
  render identically keep sharing one row on purpose.
  The old derivation joined the components on a bare NUL and serialized two of
  them with `ensure_ascii=True`, so two distinct calls could hash to the same
  key and one call's cached body was served for the other: a NUL inside an
  upstream server or tool name shifted the component boundary (nothing on the
  path rejects one), and an astral scalar in the arguments rendered as the
  same escaped text as two lone surrogate code units. Each component is now
  length-prefixed and serialized with `ensure_ascii=False`, behind a
  `_KEY_SCHEMA_VERSION` bump to v5. (#795, fixes #784)

- observability: refuse an OTLP endpoint whose path trips the credential
  screen, and narrow the SDK log screen. The HTTP transport logs its request
  line on every *successful* export, so a token in the endpoint path was
  written out in full by a logger STM does not own — refusing the endpoint is
  the boundary STM controls, and `urllib3`/`requests` are deliberately left
  unfiltered since the whole process shares them. The screen on the two
  OpenTelemetry loggers now inspects every formatter-visible channel
  (`exc_info`, `stack_info`, `extra=`, not just the message), drops rather
  than rewrites a record that trips it, fails closed on a record it cannot
  render, and is detached at shutdown. (#789)

- Lone surrogates in SQLite-bound diagnostic/content fields and nested
  extraction JSON are escaped once at ingest, while legacy surfacing memory-ID
  JSON omits unencodable identifiers on display/stat reads without aliasing
  them to an existing literal `\udxxx` ID. Query-only digest inputs in
  surfacing cache keys, persisted query hashes, and tool-relevance telemetry
  now hash with `surrogatepass`, preserving clean digest values and keeping a
  raw surrogate distinct from its six-character literal twin. Observability
  stats filters and surfacing-feedback identifiers are rejected at their
  response boundary with sanitized, UTF-8-safe errors. (#788, fixes #783)
- Negative-feedback counts now drop only the unencodable memory IDs instead of
  the whole batch, so one bad ID no longer suppresses surfacing demotion for
  the valid IDs beside it. A refused stats filter also keeps reporting the
  schema-capability flags (`schema_outdated`,
  `diagnostics_recovery_supported`) accurately: they describe the file, not the
  filter. (#788, fixes #783)
- Core memory IDs are no longer aliased at ingest. `_core_json_loads` scrubbed
  the whole parsed payload, so a chunk ID core sent as a real lone surrogate
  and one sent as the six literal characters `\ud800` arrived as the same
  value — and the new identifier refusal, running after that rewrite, accepted
  it. Content is still escaped at ingest, but `id` / `chunk_id` / `block_id`
  now arrive unmodified and the search and context-compose parsers drop an item
  whose ID they cannot encode. (#788, fixes #783)

## [0.1.43] — 2026-07-26

### Upgrade notes

- The `# copy/paste hint unavailable: …` diagnostic now reads `a value contains
  characters that cannot be displayed safely` rather than `a value contains
  line-break or NUL characters`, and stands in for a command whose values carry
  any terminal-hostile character, not only CR/LF/NUL. (#756)
- The JSON snippet printed by `mms register --mcp skip` now renders a
  terminal-hostile character in the interpolated `command` escaped, so the
  snippet is copy-pasteable where it previously carried the raw value. The
  server key beside it is the constant `memtomem-stm` and never carried one.
  `command` now goes through `json.dumps`, which is `ensure_ascii=True`, so
  **every** non-ASCII character in it — CJK, emoji, accented paths — renders as
  a `\uXXXX` escape where 0.1.42 emitted it literally. The snippet still parses
  to the same string. (#759)
- A multi-line message from a host client's CLI now renders as one escaped line
  in `mms eject`'s and `mms prune`'s failure lists, where it previously wrapped
  across several. (#759)
- `mms add <name>` now exits 1 with an `invalid_name` error — under `--json`
  too — for a name that is not valid UTF-8, where 0.1.42 aborted with an
  `UnicodeEncodeError` traceback having written nothing. The refusal is about
  encodability, not display: it covers a lone surrogate, not every
  terminal-hostile character. The same refusal applies to the other commands
  that create an upstream server. (#758)
- Across `mms import`, `mms host`, `mms project`, `mms config validate` and
  `mms hook`, a displayed value carrying a terminal-hostile character now
  renders escaped rather than raw, and the aligned tables pad by the displayed
  width. Values with nothing to escape render byte-identically, except
  `mms project disable`'s no-op line, which now lists MCP names as
  `none of a, b` instead of the Python list repr `none of ['a', 'b']`.
  Coverage is the sites listed in the entry below, not these commands
  wholesale; the rest is closed further down this section by #785, which
  extends the same escaping to the re-stamp diff's `command` and env keys,
  `mms project show`'s no-marker branches, and `mms hook`'s preview and errors.
  (#769, #770, #771, #772, #785)
- Proxied content is now modified in one case: a lone surrogate in an upstream
  response is delivered as its escaped `\udxxx` literal rather than failing the
  response that carries it. Clean responses are byte-identical. (#773)
- `stm_memory_propose`'s `max_content_chars` and the 512/256 limits on
  `source_ref` and `idempotency_key` now measure the *escaped* form, so a value
  packed with lone surrogates can be refused where its raw length fit, under
  that field's own reason — `content_too_large`, `source_ref_too_large` or
  `idempotency_key_too_large`. Payloads with no surrogate are unaffected. (#778)

### Fixed

- Terminal-hostile characters in a config-derived server name, source label or
  path no longer reach the terminal verbatim in a hint. The set covers what
  cannot be rendered or can rewrite the rendered line — control characters
  such as ESC, the line separators, and the bidirectional controls, among
  others. Its single authoritative definition is the `_disp_escapes`
  predicate — moved to `cli/_display.py` by #768 later in this release, and
  re-exported from `cli/proxy.py` — whose docstring enumerates the members and
  the reason each is in; read that rather than this sentence for the exact
  membership. Preserved: everything else, so CJK, emoji, ZWJ sequences and
  the plain LRM/RLM marks render as before, and a value with nothing to
  escape renders byte-identically. Config is plain
  `json.loads` with no character validation on server names, so an imported or
  hand-edited config could carry any of these.
  Prose escapes such a character in place, as `\uXXXX`. A runnable command
  cannot — an escaped token would paste as a *different* server name — so it is
  refused wholesale instead, extending the existing CR/LF/NUL guard to the same
  class. Both halves of a hint therefore now agree about the same character.
  Previously both printed such a name raw — for anything outside CR/LF/NUL
  neither half guarded it — and escaping the prose alone would have replaced
  that with a worse state: an escaped name in the `Note:` sentence and the
  raw one, live ESC and all, in the `mms eject` command on the line below.
  The **prose sites** covered are: `mms remove`'s eject `Note:` sentence, in
  both the server name and the imported-from label (an unrecognized
  `origin.source.kind` is displayed as recorded); the
  `# Remove '<name>' from <source>.` line, in both values; the
  `# Edit … and remove '<name>' under mcpServers.` line; the
  `# Edit <path> … <name>: <payload>` restore line, in the path, the name and
  the payload JSON, since of the set's members `json.dumps(…,
  ensure_ascii=False)` escapes only the C0 ones and so left the rest raw
  inside string values; and the `mms remove` flow's own
  server-not-found error, confirmation prompt and success line, which print the
  same key in the same screenful. The prompt is the one site where this was
  more than cosmetic — a CR in the name overwrote the rendered `[y/N]` the user
  was answering.
  In that same `# Edit` line the server name is now rendered by `json.dumps`
  rather than wrapped in literal quotes, so the fragment parses back to the
  exact key. A name containing `"` used to produce invalid JSON, and one
  containing `\` used to break it either way: followed by a character JSON
  does not define as an escape (`back\slash`) the fragment was outright
  invalid, while followed by one it does define (`back\bslash`) it parsed
  silently into a *different* key — a real backspace.
  **Behavior change**: the `# copy/paste hint unavailable: …` diagnostic now
  reads `a value contains characters that cannot be displayed safely` rather
  than `a value contains line-break or NUL characters`, and stands in for a
  command whose values carry any character in the class above, not only
  CR/LF/NUL. The wording names no Unicode category on purpose: the class spans
  several, so `control characters` would be false for the separators and the
  lone surrogates.
  `mms remove --json` embeds the eject hint in its `warnings` array, so for a
  name carrying one of these characters that string now contains `\uXXXX`
  escapes or the diagnostic; the array's shape is unchanged, and a name with
  nothing to escape produces a byte-identical string. Machine-readable fields
  (the `name` key in the result and in `--json` failures) stay raw. Those are
  data, not display: of the set's members `json.dumps` escapes the C0 ones,
  and the rest of the
  set round-trips through a JSON parser unharmed even though it is emitted
  literally. The exception is a lone surrogate, which `json.dumps` also emits
  literally but which then cannot be encoded at all — see the residuals below.
  This closes the display wart the #752 entry below documents as out of scope
  for #751. Residuals: a backslash is never escaped by the display sanitizer,
  which keeps Windows paths readable and leaves any backslash escape
  `json.dumps` already produced intact, but makes the encoding non-injective
  (a name containing the literal text
  `\u001B` renders like one containing a real ESC). Within the `# Edit` line
  the escapes are mixed-case: a character `json.dumps` already escapes itself
  keeps its lowercase form, while one it leaves raw under `ensure_ascii=False`
  — every member of the set outside C0 — is then escaped uppercase by the
  sanitizer, as the path always is. A lone
  surrogate in a `--json` payload is a different matter: `json.dumps` returns
  it unescaped and writing that document to the terminal then raises, so the
  command emits no JSON at all. That predates this change and is unaffected by
  it; see #757. And the
  remaining prose sites the sweep found — the `mms list` and `mms prune`
  tables, the discovery/`mms add` flow, the eject summary and backup-log lines,
  and `mms health --names` / `mms doctor` — are deferred to #755.
  (#756, fixes #754)
- The same display escaping now covers the rest of the terminal output that
  carries a value nobody validated. Which values those are, by command:
  `mms list`'s table cells (name, prefix, transport, compression, origin,
  command/URL); `mms prune`'s and the import previews' candidate rows, whose
  command/URL cell comes from *another* client's config; the discovery flow's
  `Configuring '<name>'` header, probe results and import summary; `mms add`'s
  own name echoes and its `--validate` failure text; `mms eject`'s refusal
  reason, plan, warning, secret-gate prompt and failure lines; `mms health`'s
  server lines and, under `--names`, the tool names the upstream advertised
  over `tools/list`; `mms doctor`'s check labels and details; `mms stats`'s
  per-tool compression table and `mms tune`'s preview, selector and per-tool
  confirmation prompt, which key on tool names recorded from the upstream at
  call time; `mms surfacing`'s name echoes; `mms gateway status`/`explain`;
  the prefix-collision error; and the config-validation warning shared by
  `status`, `health` and `tune`, whose text names the offending config key.
  Unchanged: every `--json`
  payload and every `_json_fail` envelope keep raw values — those are decoded
  by a consumer, not read off a terminal — as do the copy/paste hints, which
  `_shell_join` already refuses wholesale for this character class. Also
  untouched, and not part of this change: values passed as argv by the user
  running the command, and the same class of site in `mms host`,
  `mms project`, `mms import`, `mms config validate` and `mms hook`, whose
  modules have no access to this helper.
  Three of these were more than cosmetic. `mms health --names` and the two
  metrics-derived tables are the first sites where the value is chosen by a
  *remote* party rather than by whoever wrote the config: a proxied server
  picks the tool names it advertises, so reaching this output needed no
  access to any file on the machine — and in `mms tune` such a name renders
  inside the prompt whose answer authorizes a config write. And in
  `mms list`, a name containing a lone surrogate — `"\ud800"` is a legal JSON
  escape, so such a config loads fine — made the command exit 1 with a bare
  `UnicodeEncodeError` and print no table at all; the row now renders with the
  character escaped. (The `--json` half of that failure is #757, separate.)
  Column widths are unchanged: an escaped value is longer than its raw form
  and so overflows its cell, exactly as an over-long ordinary name already
  did. `mms prune`'s preview, the one table whose width is computed rather
  than fixed, measures the escaped names so its second column stays aligned.
  Names are still padded by character count, so a CJK or emoji name misaligns
  the table by as much as it did before — that is unrelated and untouched.
  **Behavior change**: the JSON snippet printed by `mms register --mcp skip`
  and by `mms init`'s skip option now renders `command` through `json.dumps`
  like the neighbouring `args` and `env`, instead of wrapping the value in
  literal quotes. The snippet is meant to be pasted into a client's config
  file, and for any path containing `\` or `"` — on Windows, every path — it
  previously produced a document that would not parse back. A path needing no
  escaping renders byte-identically.
  **Behavior change**: a multi-line message from a host client's CLI now
  renders as one escaped line in `mms eject`'s and `mms prune`'s failure
  lists, where it previously wrapped across several.
  (#759, fixes #755)
- The same display escaping now reaches the CLI surfaces the earlier sweeps
  left, closing the sites #760 enumerates plus several they do not name. By
  command: `mms import`'s plan listing (candidate name, source label, command,
  the conflict line's `reason`, and the env summary — escaped inside
  `_format_env_summary`, so `mms host sync` is covered by the same change, and
  after redaction on the default path — `--show-imported` remains an explicit
  opt-in that displays real values, escaped but not redacted);
  `mms host`'s `status` and `scan` tables, every `sync --plan` bucket, the
  re-stamp diff's name and `Source:` lines, and the `--apply` confirmation
  prompt; `mms project`'s six echo sites, its marker-backed `show` output, its
  tab-separated `list`, and eleven error messages;
  and `mms config validate` and `mms hook`, covering each error, unknown key and
  warning, the hook change path and the backup path on both apply branches.
  Column-aligned surfaces pad by the *displayed* width, so an escaped value no
  longer offsets the columns after it.
  Two of these are more than cosmetic. The `mms host sync --apply` prompt
  authorizes removing registry entries, and a CR in a name overwrites the
  rendered `[y/N]` the user is answering. And `mms project enable` / `show NAME`
  echo a name straight from argv, where POSIX `surrogateescape` decoding
  produces a lone surrogate with nothing hostile on disk at all — so the *error*
  prose is the first place such a value renders.
  Values this package writes itself stay raw, as do the `--json` legs, which are
  pinned by tests.
  Not covered by this sweep, and closed further down this section: the
  re-stamp diff's `command` and env-key fields, the two `mms project show`
  fallback branches, and `mms hook`'s preview and error messages.
  **Behavior change**: on these surfaces a value carrying a terminal-hostile
  character now renders as its `\uXXXX` escape rather than raw, and the
  aligned tables pad by that displayed width. A value with nothing to escape
  renders byte-identically, with one exception: `mms project disable`'s no-op
  line now lists the MCP names as `none of a, b` rather than the Python list
  repr `none of ['a', 'b']`. (#769, #770, #771, #772, issue #760)
- A lone surrogate in a config no longer crashes the CLI's JSON output after
  the write it was reporting. `json.dumps(..., ensure_ascii=False)` — which
  keeps CJK and emoji readable — leaves such a code unit raw in the string it
  returns, so the `UnicodeEncodeError` landed at the `click.echo` or file write
  that encoded it. `mms remove <name> --yes --json` deleted the entry, saved
  the config, and *then* failed rendering the report: exit 1 and empty stdout
  for an operation that had succeeded. Every JSON document the CLI emits — the
  `--json` legs of `add`/`remove`/`prune`/`eject` including their error and
  `confirmation_required` envelopes, the read-only `status`/`list`/`stats`/
  `health`/`doctor`/`tune` payloads, `mms host`, `mms project route`,
  `mms config validate`, the lock-timeout envelope, and the PostToolUse hook's
  reply to Claude/Codex/Cursor — and every JSON config writer that passed
  `ensure_ascii=False` now goes through a serializer that re-escapes those code
  units as `\udxxx`. (The writers that omit it, and the TOML writers, were never
  exposed.) Two things
  could supply one: config is plain `json.loads` with no character validation
  and `"\ud800"` is a legal JSON escape, so an imported or hand-edited config
  could carry it as a server name; and on POSIX a command-line argument holding
  a byte that is not valid UTF-8 is decoded with `surrogateescape`, so
  `mms add $'s\xffv' ...` alone produced a name the CLI then could not write.
  Clean payloads render byte-identically, and a rewritten config decodes back to
  the identical name rather than losing the entry. `mms eject`'s manual
  `claude mcp add-json` hint is not a JSON document and needs none of this: its
  command form is refused wholesale by `_shell_join` and its `# Edit` form is
  display-escaped, both since #756. The argv `mms eject` actually spawns was
  the real remaining exposure there — see the entry below. (#758, fixes #757)
- The commands that *create* an upstream server now refuse a name that is not
  valid UTF-8, rather than storing one that fails later. Being writable is not
  the same as being usable: the server name is the first component of the
  response-cache key and part of the Toolgraph contract fingerprint, both of
  which hash encoded bytes and raise on such a character, and TOML cannot
  represent it at all, so `mms import`'s registry could never hold one either.
  **Behavior change**: `mms add <name>` now exits 1 with an `invalid_name`
  error — under `--json` too — where it previously aborted with an
  `UnicodeEncodeError` traceback having written nothing. `mms init`'s manual
  prompt refuses as well (its `--json` envelope reports the generic setup
  failure, not `invalid_name`), and the discovery scan behind
  `mms add --from-clients` and `mms init` skips such an entry, with a note on
  stderr, instead of importing it — so the other servers in that host config
  still import. `list`, `remove`, `surfacing` and the rest stay permissive by
  design, so a config that already holds such a name can be inspected and
  repaired: `mms remove` clears the entry and `mms surfacing <name> off`
  toggles it in either output mode, this change covering the `--json` reports
  and the display escape covering the printed lines. Making the config writable
  is what newly exposed that second half — before it, these commands failed
  inside the write, so nothing had changed yet; afterwards the write lands and
  only the report can still fail. Every command that writes the config was
  re-checked by running it against such a config and comparing the file before
  and after, and none now mutates and then raises. The read-only renderings of
  `mms list`, `mms doctor` and `mms health` print the name display-escaped
  rather than raising, as of #759 — the two escapes meet there, and a test
  pins all three text legs so a regression in either shows up as a crash.
  `mms eject` is checked the same way before it spawns either `claude mcp`
  verb: the name and payload go out as subprocess arguments, which encode to
  UTF-8, so the command now reports "server name or payload is not valid
  UTF-8" and leaves both sides intact where it previously raised — after the
  destructive pre-remove, on the `--force` path. (#758)
- The daemon starts, and every daemon-touching CLI command runs, when
  `MEMTOMEM_STM_HOOK_SURFACE_TOOLS` holds a byte that is not valid UTF-8. That
  variable is folded verbatim into the daemon's config fingerprint, which is
  hashed by encoding it — and on POSIX an environment variable carrying such a
  byte is decoded with `surrogateescape`, so a lone surrogate reached that
  encode with no config file involved. The fingerprint is frozen in
  `DaemonServer.__init__`, before `serve()` and outside any `try`, so the
  resulting `UnicodeEncodeError` meant the daemon process never started;
  `mms daemon start`, and `mms daemon status`/`stop` through the client's
  `_live_handshake_candidate`, exited on an uncaught traceback. The fingerprint
  now escapes lone surrogates before hashing, the same treatment `--json`
  payloads have had since #758. It stays a fingerprint: distinct env values
  still produce distinct digests, so two configs cannot collapse onto one
  handshake or lock file. A value with nothing to escape hashes byte-identically
  to before, so no existing daemon is orphaned by this. (#765, issue #761)
- A lone surrogate in a hook↔daemon frame no longer fails the connection.
  `encode_line` ends in an explicit `.encode("utf-8")`, so such a frame raised
  and the peer saw the socket close with no response — a hook waiting on a
  reply, not an error it could report. Both ends are now covered, not just the
  write end: the frame is serialized through the same surrogate-escaping writer
  the `--json` legs use, and `read_message` escapes what it decodes. The read
  half is load-bearing rather than belt-and-braces, because the write half's
  escape is the *JSON* one and `json.loads` decodes it faithfully back into the
  code unit — escaping only on the way out would have moved the failure into
  the receiving process, at whichever encode it reached next. It also covers a
  peer that never escaped it. A frame with nothing to escape is byte-identical
  on the wire and decodes to an equal object, so this is invisible to every
  existing exchange. (#766, issue #761)
- In Toolgraph bundle mode, an upstream tool whose `tools/list` metadata
  carries a lone surrogate is now rejected as drifted instead of taking the
  whole catalog down. The contract fingerprint encodes its canonical JSON, and
  it is computed inside the bind loop over every tool of every connection, so
  the resulting `UnicodeEncodeError` escaped both call sites — the reload site
  catches only `(OSError, PolicyBundleError)` and the startup apply is outside
  that `try` — and failed every `tools/list` and `tools/call`, not just the one
  tool's. The reachable shape is metadata *drift*, which is exactly what the
  digest exists to detect: a bundle can only ever be published for clean
  metadata, since Toolgraph's own encoder raises on a surrogate too, but it
  keys on the tool name, so a tool crawled clean and later serving a surrogate
  binds by name and reaches the digest. Its digest now cannot match one no
  producer could have published, which is the fail-closed rejection this path
  is meant to produce. Clean metadata hashes byte-identically to the producer,
  so every existing bundle keeps binding exactly as before — the cross-repo
  golden-fixture test pins that. (#767, issue #761)
- An upstream response carrying a lone surrogate is delivered with that
  character escaped instead of being discarded. The MCP SDK decodes a legal
  `"\ud800"` escape out of the upstream's wire JSON into a raw code unit, and
  `TextContent(...).model_dump_json()` then refuses to serialize it — so an
  otherwise-successful response was lost to a serialization error. Such a
  character is undeliverable by any route, so escaping it is strictly better
  than losing the response around it.
  **Behavior change**: proxied content is now modified in this one case. A lone
  surrogate is replaced by the six literal characters `\ud800` (its JSON escape
  spelling) in tool response text, in `structuredContent` and in `_meta`.
  Content with no lone surrogate — everything else, including all CJK, emoji
  and astral characters — is returned byte-identically, so this is invisible to
  every well-formed response.
  The escape happens once at ingest rather than at each serialization site,
  which is also what keeps the compression budgets honest: they measure the
  length of a re-serialized payload, so escaping later would have made them
  count a string six characters shorter per surrogate than the one actually
  delivered. (#773, issue #761)
- The same escaping now covers replies from the LTM core, so a surrogate in a
  surfaced memory or in a review candidate no longer fails the STM tool
  response carrying it. Two entry points, and each is needed: the SDK decodes a
  surrogate off Core's wire into the text we read, and Core also returns JSON
  *inside* that text, where the six characters `\ud800` are a legal escape
  that survives text-level escaping and then decodes into a fresh code unit
  when that nested document is parsed. Clean replies are unchanged. (#774, issue #761)
- `mms import` skips a host entry it cannot store instead of aborting the whole
  import. A host config is plain `json.loads` with no character validation, so
  a legal `"\ud800"` escape reaches the registry writer as a code unit that the
  drift hash cannot encode and TOML cannot represent. That raise was uncaught
  and left **nothing** imported, so one malformed entry cost every clean entry
  beside it. Such an entry is now reported and skipped per entry, naming the
  offending field, and the server name via `repr`, but never a command,
  argument or environment value (those are routinely secrets and this text
  reaches CI logs) — the same refusal `mms add` and the discovery
  scan have made since #757/#758, now applied to the third create path. (#775, issue #761)
- The SQLite pending store is hardened against a lone surrogate reaching it in
  both directions, and the response cache and progressive store on the write
  path. `sqlite3` encodes text parameters to UTF-8, so the failure landed at
  `execute` time, where the caller logs it and leaves the response alone — the
  cost was a silently uncached response and a warning per call. A `\ud800`
  escape sitting in a stored row also decodes straight back into the code unit
  through a plain parse, failing at the next encode downstream rather than at
  the read; the pending store scrubs on read for that reason. With the ingest
  escaping above in place nothing should reach these in normal operation; they
  are hardened because their failure mode was bad out of proportion to its
  cause. The cache and progressive readers are closed further down this
  section. (#776, issue #761)
- The response cache and progressive store are now surrogate-safe on read as
  well as write, closing part of the gap #761 left when it hardened only the
  pending store. Both escape on write and then parsed with a plain
  `json.loads`, which decodes the six characters `\ud800` straight back into
  the code unit, so the value came back unencodable and raised at the next
  encode downstream rather than at the read. The progressive store's `__meta__`
  is a JSON document nested *inside* a chunk, so the backing store's own scrub
  never reached it.
  `ProxyCache` also stored a response body without escaping it. `sqlite3`
  encodes text parameters to UTF-8, so that raised at `execute`; the caller
  catches it and leaves the response alone, so the cost was a silently uncached
  response and a warning on every call rather than a lost one.
  Its **identifiers** are handled differently from its content, on purpose. The
  cache key now hashes `server` and `tool` through `errors="surrogatepass"`
  rather than the escaping helper, which is documented as non-injective and
  would have let one identifier's row answer for the distinct identifier
  spelled with those six literal characters. That closes the aliasing this
  change would otherwise have introduced; it does not make the key injective
  in general, and two older collision classes in the same derivation are
  tracked in #784. A `server`/`tool` that cannot be a SQLite text parameter
  now skips the store rather than being escaped into one, because an escaped
  name is unmatchable by `clear()` and aliases that same distinct identifier;
  `clear()` returns 0 for such a filter, which is what it now always matches,
  and `stm_proxy_cache_clear` escapes the filter it echoes back so the reply
  itself stays serializable. Clean values hash, store, read and clear exactly
  as before. (#782, fixes #781)
- A lone surrogate in `stm_memory_propose`'s own arguments no longer escapes as
  a traceback. The tool derives its idempotency key by hashing the client's
  `content` and `source_ref`, and that `.encode()` sits *above* the tool's
  `try`, so an unencodable code unit in either raised out of the tool instead of
  returning one of its structured `{"ok": false, "reason": …}` replies. A
  client-supplied `idempotency_key` skipped the hash but failed later and worse:
  all three go verbatim into the outbound `mem_do` params, whose SDK
  serialization refuses a surrogate, so it degraded to `candidate_submit_failed`
  — a reason naming Core for a request that never left. All three are now
  escaped at entry, so an ordinary value carrying a surrogate is delivered
  rather than refused: the character is escapable, and the surrounding limits
  are about size.
  **Behavior change**: `max_content_chars` and the 512/256 limits on
  `source_ref` and `idempotency_key` now measure the *escaped* form. One code
  unit becomes six characters, so a value packed with surrogates can now be
  refused where the raw length fit — with that field's own reason,
  `content_too_large`, `source_ref_too_large` or
  `idempotency_key_too_large`. This is deliberate,
  and it is the only denomination that holds on both routes: with
  `surfacing.use_daemon` enabled the daemon re-applies these same three limits
  to what is actually sent, and its refusal arrives as an opaque
  `candidate_submit_failed`. Measuring the raw form would have made a value
  near the limit succeed or fail depending on the transport. Payloads with no
  surrogate are unaffected — the escaping helper returns them unchanged and
  every limit behaves exactly as before.
  This is the request half of the tool whose response half #761 closed; it was
  left out of that issue deliberately, because a tool argument is not one of the
  ingest points whose escaping made the rest of `server.py` safe.
  (#778, fixes #777)

- The display escaping now reaches the CLI surfaces the #760 sweep left. The
  `mms host` re-stamp diff escaped the server name and the `Source:` line but
  rendered `command` and the env keys raw in all three branches; those lines
  reach `sync --plan` *and* the `--apply` confirmation prompt that authorizes
  removing registry entries, so a CR there overwrites the `[y/N]` the user is
  answering. The env keys are escaped inside `_format_env_keys_redacted`, their
  only renderer, and still after redaction, which alone decides what is shown.
  `mms project show`'s two no-marker branches rendered `cwd`, `cwd.name` and the
  git root raw — a path needs no hostile config to carry a lone surrogate, since
  a POSIX byte that is not valid UTF-8 decodes with `surrogateescape`. And
  `mms hook`'s preview rendered `rendered_block` raw, which embeds the
  source-checkout command; it is escaped at the render rather than on the
  field, since `new_text` is what gets written. The block is split on its
  structural newline before escaping — escaping first would collapse a
  fifteen-line preview into one line, and splitting with `splitlines()` would
  consume a hostile CR as a line break instead of showing it. All seven
  `HookInstallError` raises are covered by escaping at the two
  `ClickException` sites instead, so messages added later are too.
  Not covered, and tracked in #786: several more terminal renders of
  unvalidated values in these same modules — `last_imported` and the
  post-apply paths in `mms host`, `last_seen` and two exception boundaries in
  `mms project`, and the shared `_write_lock` messages.
  Not changed: the `args` list in that same diff line. `list.__repr__` already
  escapes every character this class covers — verified over all 1,114,112 code
  points — so routing it through the escaper too would only double-escape.
  (#785, fixes #780)

## [0.1.42] — 2026-07-25

### Upgrade notes

- Copy/paste hints now quote an interpolated config path, server name, backup
  path, or upstream `command` that requires quoting, where those values were
  previously emitted bare; a value needing no quoting renders
  byte-identically. (#742, #744)
- `mms tune`'s restore line now prints `cp -- <backup> <config>` — a literal
  `--` end-of-options terminator that was not emitted before. (#742)
- `mms remove`'s eject hint now prints
  `mms eject --config <active-config> -- <name>` instead of `mms eject <name>`,
  which on paste silently targeted the default
  `~/.memtomem/stm_proxy.json`. (#748)
- On Windows, the three hints that previously rendered with POSIX
  `shlex` quoting now render in cmd.exe form. (#747)
- On Windows, a hint token containing a cmd.exe metacharacter
  (`& | < > ^ ( )`) or `%` is now double-quoted where it was previously emitted
  bare, and an embedded `"` renders as `""` instead of `\"`. Tokens whose only
  special content is a space or tab, and empty tokens, keep the exact
  `subprocess.list2cmdline` rendering they already had. (#750)
- A `_shell_join` call whose argv carries CR, LF, or NUL now returns only the
  `# copy/paste hint unavailable: …` text, in place of everything it would have
  rendered — not just the offending value. Hints come in three shapes and lose
  different amounts of the line. A whole-command join loses the command
  entirely. A `mms doctor` hint embeds a joined `--config <path>` fragment, so
  a tainted path leaves the surrounding `mms <subcommand> …` text followed by
  the diagnostic. The project-scoped `claude mcp add-json` eject fallback
  composes two whole-command joins around a literal `&&`, so a `cd <path>` leg
  whose own path is clean still prints and can run on paste. See the Fixed
  bullet for the full scope. (#752)

### Fixed

- Copy/paste command hints are now rendered shell-safely instead of by bare
  f-string interpolation. `mms init`'s `Next:`/`Manage this config:` lines,
  `mms remove`'s eject hint, and `mms doctor`'s `--config` argument plus its
  `where.exe <command>` / `command -v <command>` probe hints route their
  interpolated values (config paths, server names, upstream `command` values)
  through the `_shell_join` helper, so a renderable value containing a space or
  shell metacharacter pastes as one argument. `mms doctor` quotes only the path
  token so the templates' literal `<name>`/`<prefix>` metavariables stay
  readable. **Behavior change**: at these sites a value that requires quoting
  now renders quoted where it was previously interpolated bare; a value needing
  no quoting renders byte-identically, and a value carrying CR/LF/NUL instead
  collapses to the #752 diagnostic below. (#744, fixes #743)
- The three hints that already quoted with `shlex.quote` — `claude mcp remove`,
  `mms tune`'s `cp` restore line, and `mms eject`'s manual `claude mcp add-json`
  hint (including its `cd <path> &&` prefix) — now go through `_shell_join` too.
  `shlex.quote` is POSIX-only: it single-quotes a Windows path's backslashes
  into a token cmd.exe cannot read, so the Windows hints were unpasteable.
  **Behavior change**: the `cp` restore hint gains a literal `--`
  end-of-options terminator and the server name and both paths are quoted when
  they require it, where they were previously interpolated bare (#742); on
  Windows these three hints change from POSIX single-quoted rendering to
  cmd.exe rendering (#747). (#742, #747; fixes #741, closes #745)
- `mms remove`'s eject hint now names the active `--config` and guards the
  server name with a `--` terminator, so pasting it restores the entry to the
  host from the config it was removed from rather than from the default path,
  and a leading-dash server name pastes as a positional instead of an option.
  **Behavior change**: the hint text changes for every user, from
  `mms eject <name>` to `mms eject --config <active-config> -- <name>`.
  (#748, closes #746)
- The win32 leg of the hint joiner is now cmd.exe-shell-safe, not merely
  argv-safe. `subprocess.list2cmdline` implements MS C-runtime argv quoting
  only, leaving the cmd.exe metacharacters `& | < > ^ ( )` unprotected in
  tokens without whitespace, so a legal NTFS path like `C:\a&b\cfg.json` split
  or redirected the pasted command. Such tokens are now double-quoted (with
  embedded `"` escaped as `""` to preserve cmd's quote parity), and a token
  containing `%` is quoted so metacharacters in an expanded `%VAR%` value
  cannot split the command. Documented residuals: a defined `%VAR%` still
  expands, and `!VAR!` under `cmd /v:on` is not defeatable by quoting.
  **Behavior change**: on Windows, a token containing `& | < > ^ ( )` or `%` is
  now quoted where `list2cmdline` left it bare, and a token containing `"`
  renders as a quoted span with `""` instead of `list2cmdline`'s bare `\"`.
  Everything else keeps its previous rendering byte for byte: a token whose
  only special content is a space or tab (asserted against `list2cmdline`
  itself), an empty token, and a plain backslash path each keep their old
  output under a regression test. POSIX rendering is untouched.
  (#750, fixes #749)
- A `_shell_join` call is now refused outright when its argv carries CR, LF, or
  NUL: on every platform it returns a fixed `# copy/paste hint unavailable: …`
  string in place of its entire rendering, so the raw value never reaches the
  output. At an interactive cmd.exe prompt a pasted newline is consumed as
  Enter even inside an open quoted span, submitting the truncated prefix as its
  own command, so quoting cannot fix
  that class; on POSIX `shlex` contains the newline but only as a multi-line
  paste that breaks the one-line hint, and NUL is unrepresentable everywhere.
  Config is plain `json.loads` with no character validation on server names,
  `command`, or env values, so an imported or hand-edited config could carry
  these into a hint.
  **Behavior change**: the guard covers what `_shell_join` renders, not the
  prose around it, and it replaces that call's whole output rather than
  scrubbing the offending value out of it. What survives depends on the hint's
  shape, of which there are three. A **whole-command join** — the registration
  hints, `mms init`'s lines, `mms remove`'s eject restore hint, `mms tune`'s
  `cp`, `claude mcp remove`, the user-scoped `claude mcp add-json`, and
  `mms doctor`'s `where.exe` / `command -v` probes — loses the whole command
  along with the tainted value. The **doctor fragment** joins only the
  `--config` path (`--config {join}`) and has commands built around the result,
  so a tainted path leaves `mms <subcommand> … --config` followed by the
  diagnostic. The **two-leg project hint** (the project-scoped
  `claude mcp add-json` fallback, `{join} && {join}`) is composed from two
  whole-command joins, so a tainted leg collapses while the other still
  renders — a clean `cd <path>`, config-derived rather than app-owned, can
  still execute on paste. No injected content survives into a pasteable
  command. `mms remove`'s surrounding `Note:` sentence still prints
  the server name verbatim — a display wart rather than a paste-execution
  surface, and out of scope for #751. The `#` prefix is a comment on bash, fish
  and POSIX sh, and elsewhere (interactive zsh, cmd.exe) an ordinarily-failing
  command lookup, with the documented residual that `#` can still resolve via
  an alias, function, command hash, or a planted `#.{cmd,bat,exe}` — the same
  residual the pre-existing `# Edit …` /
  `# Remove …` hints already carry.
  (#752, fixes #751)

### Changed

- Public docs now distinguish Claude Code auto memory and Codex local memories
  from memtomem Core LTM and STM surfacing, align native-hook guidance with
  Claude Code 2.1.121+ and Codex's documented PostToolUse `additionalContext`,
  and identify Core 0.3.12 as the planned schema-4 compatibility baseline.
  (#740)

## [0.1.41] — 2026-07-22

### Upgrade notes

- The response cache is upgraded to schema v4 and performs a one-time cache
  reset on first run after upgrade; a cache-hit now retains JSON-safe
  `structuredContent` and result `_meta`. `mms daemon status` and the daemon
  ping gain queue-telemetry fields in both human and JSON output. Opt-in
  Unicode token estimation and `_context_query` schema advertisement are both
  off by default (character-budget behavior unchanged). (#731)
- On a core reporting a non-RRF `score_scale`, surfacing now injects up to
  `max_results` memories per call where it previously filtered most or all of
  them out against an incommensurable `min_score` threshold; dedup and feedback
  demotion remain the quality controls. Set
  `surfacing.scale_gated_min_score=false` to restore unconditional filtering.
  (#730)
- On a bypass-capable core with server-side rerank enabled, surfacing scores
  now return on the RRF scale (`(0, ~0.033]`) that `min_score` and the
  auto-tuner are calibrated against; set `surfacing.rerank=none` to restore
  server rerank policy. (#726)
- With a compatible Toolgraph and the default `on_unreachable: open`, a typed
  backend outage now starts loudly degraded under STM-native rules instead of
  failing startup as a protocol error. (#736)
- `hook.daemon_timeout_seconds=inf`/`nan` is now a config `ValidationError`
  instead of an accepted value. (#722)
- Default daemon installs now write `surfacing_events` telemetry rows on the
  hook path; growth is bounded by the existing `stats_retention_days` /
  `query_retention_days` cleanup. (#723)

### Added

- Developer-first project routing and runtime controls for the proxy. `mms
  project route` previews and applies a project-selected registry entry with
  config locking, validation, backups, provenance, conflict protection, and
  idempotency (previews by default; never overwrites a name/prefix conflict or
  prunes routes). Opt-in Unicode runtime token estimation
  (`token_estimation_mode`, default `static` — character-budget behavior
  unchanged) resolves with proxy/server/tool precedence. `mms status` and `mms
  doctor` surface compression-tuning readiness. `_context_query` schema
  advertisement is available opt-in (disabled by default) and never forwards
  the proxy-only argument upstream. **Behavior change**: the response cache is
  upgraded to schema v4 so a cache-hit now retains JSON-safe
  `structuredContent` and result `_meta` (errors and mixed/non-text responses
  remain uncached); the upgrade performs a one-time cache reset. `mms daemon
  status` and the daemon ping now expose bounded queue telemetry, adding fields
  to both the human and JSON output. (#731)

- Surfacing now reads the score scale a compose-capable core names on the
  composed bundle itself (`score_scale`/`reranker` on the `context_compose`
  envelope, `context_compose` schema 4, core #1796, first available in the
  core release after v0.3.11). This closes the compose blind spot the
  `mem_search` score-scale work (#727) left open: STM stamps the scale onto
  the compose retrieved results (never pinned blocks), so the scale gate
  (#730), the definitive `score_scale_mismatch` diagnostic (#727), the
  relevance-bucket suppression, `surfacing_events` persistence, and
  `stm_surfacing_stats` all now cover the composed retrieval path, not just
  the legacy `mem_search` fallback. The two keys cross the hook-daemon wire
  additively (no `PROTOCOL_VERSION` bump, emitted only at the negotiated
  schema 4) and survive the surfacing cache. Dormant against every released
  core (schema ≤ 3 never carries the keys → all-`None` → today's behavior);
  the live effect requires a core release carrying #1796, a reinstall, and a
  daemon restart. The process-level `stm_surfacing_stats` `Score scale:`
  line now also reflects compose batches. (#734)

- Surfacing now suspends the RRF-calibrated `min_score` filter when the core
  names a foreign score scale. `min_score` (default 0.03) and the auto-tuner
  are calibrated against RRF fusion scores (`(0, ~0.033]`); on a batch whose
  core-reported `score_scale` (#727) is a known non-RRF label
  (`bm25`/`dense`/`none`/`rerank` — e.g. raw cross-encoder logits with a
  negative median), no fixed constant is meaningful, so the global/auto-tuned
  floor is not applied (results stay bounded by `max_results`) and auto-tune
  pauses: `maybe_adjust` skips the batch and the tuner's rating ratios count
  only feedback earned on RRF-stamped or unstamped surfacings. Per-tool
  `context_tools.<name>.min_score` pins always keep the filter active; the
  relevance-bucket tags (`[weak]`/`[related]`/`[strong]`) are suppressed for
  results stamped with a non-RRF scale (the `[min_score, 1.0]` band math only
  holds on RRF); the first suspended batch marks a lingering
  `score_scale_mismatch`/`score_ceiling_below_min` episode recovered, so the
  `score_scale_mismatch` diagnostic and `mms doctor`'s `ltm_score_scale` FAIL
  now occur exactly when the filter actually applies (pin present, or gate
  disabled). New `surfacing.scale_gated_min_score` bool
  (`MEMTOMEM_STM_SURFACING__SCALE_GATED_MIN_SCORE`, default `true`); set
  `false` to restore unconditional filtering. Unstamped batches (`compact`
  format, pre-#1781 cores, and compose bundles on a core older than the
  schema-4 stamp) and unrecognized labels behave exactly as before; #734 later
  in this release extends the scale stamp to the composed retrieval path on a
  schema-4 core, so the gate is no longer limited to the legacy `mem_search`
  fallback.
  **Behavior change**: on a core reporting a non-RRF scale, surfacing now
  typically injects up to `max_results` memories per call where it previously
  filtered most or all of them out on an incommensurable threshold — dedup
  and feedback demotion remain the quality controls. (#730)

- Surfacing now reads the score scale the core names in structured
  `mem_search` output (`score_scale`: `rrf`/`bm25`/`dense`/`none`/`rerank`
  plus the active `reranker` model ID, core #1781, first available in the
  core release after v0.3.11) and threads it through observability:
  every parsed result carries the scale (including across the hook-daemon
  wire and the surfacing cache), each `surfacing_events` row records it
  (additive nullable column, migrated in place), `stm_surfacing_stats`
  renders a `Score scale:` line plus a per-scale event distribution, and
  the below-threshold tripwire gains a definitive tier — when the core
  names a non-RRF scale while the ceiling sits under the RRF-calibrated
  `min_score`, STM fires `score_scale_mismatch` on first observation
  (no five-call streak) and `mms doctor` names the mismatch and the fix.
  Observability only: filtering, thresholds, and unreported-scale paths
  (`compact` format, older cores, and — until #734 later in this release —
  compose) behave exactly as before. (#727)

- Surfacing retrievals now ask the core to skip its cross-encoder rerank
  stage per call (core #1766, first available in the core release after
  v0.3.11). On a rerank-enabled core that stage is ~99% of retrieval latency
  (compose p50 4.2s vs 42ms bypassed) and blew the surfacing budget on every
  builtin call, while survival past the default `min_score` is measured
  identical either way — the bypass trades ranking precision, not result
  existence. New `surfacing.rerank` tri-state (`MEMTOMEM_STM_SURFACING__RERANK`):
  `false` (default) bypasses, `true` forces the server-configured rerank,
  `none` omits the parameter so server config decides. The parameter is only
  sent when the connected core advertises it in its `mem_search` tool schema
  (negotiated once per session, like the `result_format` downgrade), so on
  older cores every value degrades to today's behavior instead of tripping
  the server's argument validation and charging the circuit breaker.
  **Behavior change**: on a bypass-capable core with rerank enabled
  server-side, surfacing scores return on the RRF scale (`(0, ~0.033]`) —
  the scale `min_score` and the auto-tuner were calibrated against; set
  `rerank: none` to restore server policy. (#726)

### Fixed

- Aligned proxy CLI defaults and clarified `mms eject` recovery. The default
  STM proxy config path is now centralized across CLI modules, and `mms project
  route`'s runtime resolution stays consistent with its help output. An
  origin-less `mms eject` failure now lists the valid `--to` targets, and
  prune-backup hints render as a concrete retry command. (#732)
- Toolgraph stdio consults now recognize the producer's typed
  `backend_unavailable` MCP envelope and route it through `on_unreachable`;
  legacy, unknown, malformed, and contract errors remain on
  `on_protocol_error`. **Behavior change**: with compatible Toolgraph and the
  default `on_unreachable: open`, a backend outage now starts loudly degraded
  under STM-native rules instead of failing startup as a protocol error. (#736)
- A transient DB failure while closing a `score_scale_mismatch` diagnostic no
  longer leaves the persisted episode open until the next full mismatch cycle.
  The warning latch still re-arms immediately on a healthy observation, while
  the recovery UPDATE now retries on later healthy observations or
  scale-gated suspended batches until it succeeds. (#733)
- The daemon now hands the surfacing engine the time left in the client's
  deadline instead of letting that deadline cancel the engine from outside.
  `hook.daemon_timeout_seconds` (2.5s) is smaller than
  `surfacing.timeout_seconds` (3.0s), so on the daemon path every slow LTM
  search was aborted by the transport before the engine's own timeout could
  fire. That abort raised `CancelledError`, which bypasses the engine's
  `asyncio.TimeoutError` handler — the one place that records the
  `error_timeout` fault, logs the warning, and counts the failure toward the
  circuit breaker (#579). The result was an invisible failure loop: no fault
  rows, no log line, and a breaker that could never open, so every eligible
  hook call re-paid the full timeout and respawned the LTM stdio child. A slow
  LTM now trips the breaker as designed and shows up in
  `mms stats` / `surfacing_faults`. When queue and lock wait have already eaten
  the deadline, the daemon skips the LTM round trip entirely rather than
  starting one it must cancel mid-RPC. A surfacing call that times out inside
  the engine is also recorded as a `timeout` latency sample rather than a
  `success` one roughly the length of the whole budget, keeping the censored
  duration out of the percentiles `mms daemon status`'s timeout recommendation
  is derived from. (#719)
- Closed the race #719 left between the surfacing engine's own timeout and the
  daemon's outer deadline backstop. The engine now receives the client's
  deadline as an absolute monotonic point rather than a relative budget, and
  derives its window right before the LTM attempt — so its pre-timeout work
  (gate, query extraction, privacy scan) debits that window instead of silently
  eating the fixed response margin. The remaining gap was that the engine only
  *raised* its timeout once the LTM adapter it had just cancelled finished
  unwinding — an unbounded wait, since a stdio child can be slow to give up —
  which is what let the caller's backstop cancel the engine from outside first
  and skip the `error_timeout` fault row, the warning log, and the
  circuit-breaker increment that only the engine's own timeout path records
  (#579). The LTM operation is now shielded, so that abort lands the moment the
  engine's timer fires and the abandoned unwind is left to finish on its own
  (the adapter already expects a caller to leave mid-RPC and marks the session
  for lazy reconnect; shutdown waits a bounded moment for that cleanup rather
  than cancelling it a second time, and declines new attempts once too many
  cancelled operations are still unwinding — warning once per draining episode
  and refunding the rate-limit slot it claimed, since a refusal starts no LTM
  work). The engine also
  books its timeout off *which timer fired* — a flag set inside the timer
  callback, which the loop runs in scheduled order ahead of any backstop
  scheduled later — rather than off elapsed time, the caller's deadline, or a
  timeout scope's own expiry, none of which can tell "my timer fired first"
  from "my timer also fired, later, while something else was cancelling me".
  A cancellation that is not this call's own timeout is left unbooked, so a
  shutdown or a client hanging up never charges a healthy LTM a breaker
  failure. A window fully consumed by pre-work is booked without starting an
  LTM round trip that would be cancelled mid-RPC, and gives its rate-limit
  slot back the same way a refusal does. (#721)
- The daemon's admission check now rejects a `deadline_monotonic` it cannot
  actually enforce, answering `expired` instead of admitting the request:
  `NaN` compared `False` against the expiry check and reached
  `asyncio.timeout_at(NaN)`, `+inf` admitted with a backstop that can never
  fire, and an int too large for a float made the dispatch raise instead of
  respond. #721 fixed the same family in `_surface_deadline`; both sites now
  share one validation helper. The one configuration that could legitimately
  produce an infinite deadline is closed at the source:
  `hook.daemon_timeout_seconds` must now be finite (it becomes the client
  deadline as `now + budget`, so `+inf` is not a big budget but a deadline
  the daemon can never enforce — and would have every request rejected).
  **Behavior change**: `daemon_timeout_seconds=inf`/`nan` is a config
  `ValidationError` instead of an accepted value. (#722)
- Successful surfacing on the hook/daemon path now records a `surfacing_events`
  telemetry row, so `stm_surfacing_stats` / `mms stats` / `mms doctor` no
  longer report 0 events for a working hook path ("0 events" was previously
  indistinguishable from "0 surfacing" — only `seen_memories` and
  `surfacing_faults` were written there). Hook-path rows carry
  `server='builtin'` and a `sha256:` digest in place of the query (the daemon
  forces `persist_query_text=false`), and the injected block still advertises
  no `stm_surfacing_feedback` rating prompt. `hook.record_feedback_events` now
  gates only the rating prompt and feedback loop, not event persistence.
  **Behavior change**: default daemon installs start writing these rows;
  growth is bounded by the existing `stats_retention_days` /
  `query_retention_days` cleanup. (#723)

### Security

- Raised the declared `mcp[cli]` floor to `>=1.28.1` so a fresh install cannot
  resolve a version affected by CVE-2026-52869 and CVE-2026-52870 (fixed in
  1.27.2) or CVE-2026-59950 (fixed in 1.28.1). #728 moved the lockfile to
  1.28.1 to clear the `pip-audit` CI gate, but the published wheel resolves
  against the declared floor, not `uv.lock` — the previous `>=1.26.0` still let
  a downstream install pull a vulnerable `mcp`. The floor now matches the
  audited lock. (#737)

## [0.1.40] — 2026-07-15

0.1.39 was never published — the version was bumped and a changelog section cut,
but no tag, GitHub release, or PyPI artifact ever carried it. This release
supersedes it and contains everything that section described. If you have a
`0.1.39` wheel from a local build, it predates this release and is not a
released version.

### Upgrade notes

- `mms doctor` now exits nonzero on a dense-to-BM25 LTM degradation that
  previously only warned. Automation that gates on `mms doctor`'s exit status
  will start failing against a core whose configured dense retrieval has
  degraded — the diagnosis is the point, but the exit status is new. An
  intentional BM25-only configuration still exits zero with a warning. (#709)
- A `context_compose` response missing its negotiated top-level keys is now a
  fault rather than an empty result. A core that conforms to the negotiated
  schema is unaffected; against one that renamed or retyped a key, surfacing now
  reports `ltm_call_failed` and warns, where it previously reported
  `empty_results` and stayed silent. The warning fires once per surfacing engine
  and does not re-arm, so a later independent compose regression is counted but
  not re-warned. (#710)

### Added

- Added portable Toolgraph policy-bundle enforcement for the STM MCP gateway,
  including strict/review/explore profiles, list/call gates, contract-drift
  validation, and `mms gateway status`, `explain`, and `mode` operator commands
  (#701). Added a review-first gateway guide and a producer-owned cross-repo
  smoke contract (#703).
- Added `mms selection replay` for issue #468 (#698): read-only schema/join/cohort
  diagnostics over selection JSONL plus deterministic evaluation of the current
  eligibility filter and BM25 ranker on a packaged, sanitized 30-case corpus.
  The report evaluates 35 existing risk-weight combinations with safety-first
  train/validation/test gates and emits a config preview without applying it.
- Added `mms doctor --measure-ltm` warm-daemon search sampling and runtime-profile
  checks for missing ONNX/Korean extras and configured-versus-effective retrieval
  mode, with measured timeout recommendations (#709).
- Added a native Windows first-success path: cross-process state locking,
  PowerShell diagnostics, safe daemon-stop behavior, platform-aware Claude
  Desktop discovery, and absolute-Python MCP registrations. (#700)
- Added `mms init --demo --client auto|claude|codex|json|skip`, `--resume`,
  cache freshness presets, Codex registration, `CODEX_HOME` discovery, trusted
  project Codex configs, and stdio upstream `cwd` support. (#700)
- A catalog-wide policy-bundle bind failure now warns once per episode, naming
  the likely cause — a `tool_contract_digest` algorithm or stale-catalog drift
  versus a `toolgraph.server_name_map` mismatch — instead of silently withholding
  every tool under `strict`. `stm_proxy_health` reports the same diagnosis, with
  the unmapped/drifted split when the cause is mixed. Only STM-computed
  rejections count toward the diagnosis: a `DRIFTED`/`UNMAPPED` reason the
  producer declared in the bundle maps to the same reject code, so counting
  final codes would false-alarm on a catalog of deliberate denials. (#706)
- The gateway now warns once at adoption when the policy bundle looks substitutable,
  naming what an unprotected bundle exposes. The bundle is the gateway's only
  enforcement authority and is unsigned, so anyone able to write it — or rename any
  directory above it — decides what the proxy exposes. Scope: POSIX owner and mode
  bits only. It is a no-op on Windows, and extended ACLs are not evaluated, so a
  `0644` file carrying `everyone allow write` reads as clean — **silence is not an
  assurance of protection**. Advisory only: findings are logged and the bundle is
  still adopted. (#708)
- Added a Korean vibe-coding quickstart guide. (#699)

### Changed

- **Behavior change**: `mms doctor` now exits nonzero when a connected LTM has
  degraded from configured dense retrieval to BM25-only, while an intentional
  BM25-only configuration remains a warning and older cores without a runtime
  profile remain warning-only (#709).
- The packaged `policy-bundle.schema.json` is re-vendored byte-identical to the
  producer's copy, which now carries the contract as normative prose: `tool_key`
  uniqueness across `tools` (with `uniqueItems: true` backstopping exact
  duplicates only), consumer MAY-ignore notes for `created_at`, `risk_score`,
  `reason` on an eligible row, and `paths`, `graph_state` documented
  closed-by-design, and the producer's int-literal obligation for `generation`.
  Enforcement is unchanged — the runtime parser never read the schema. (#712)

### Fixed

- **Behavior change**: a `context_compose` response whose negotiated top-level
  `pinned`/`retrieved` keys are missing or not arrays is now a fault naming the
  schema version, on both the direct and shared-daemon routes. Previously each
  route defaulted to an empty list, so a core that renamed a key silently
  produced an empty bundle that surfacing classified as `empty_results` — a
  degradation indistinguishable from an empty namespace, past every fault
  counter. It is now classified `ltm_call_failed` and warned once per surfacing
  engine (the latch is per-instance and does not re-arm after a healthy
  compose). (#710)
- `mms register --client codex --replace-registration` no longer removes the
  existing registration before knowing it can be rebuilt. It captures
  `codex mcp get --json` first and refuses to remove a registration
  `codex mcp add` cannot reproduce exactly; when the replacement add fails it
  attempts to restore the previous one and reports whether that restore
  succeeded. Codex has no `.mcp.json` fallback, so the previous remove-then-add
  could leave no registration at all. Rollback is an attempt, not a guarantee:
  a restore can itself fail, and the command says so rather than implying the
  old registration survived. (#705)
- New MCP and native-hook registrations now pin a shared-daemon runtime policy
  with ordered surfacing/hook deadlines instead of depending on host environment
  inheritance. Existing registrations remain keep-by-default; explicit refreshes
  preserve unrelated environment and host fields, and legacy inline-`env` hook
  commands migrate to portable runtime flags (#709).
- Score-scale recovery persistence now writes once per healthy transition instead
  of committing on every healthy search, while retaining restart recovery and
  re-arming after a below-threshold observation (#709).
- Short-lived MCP sessions now keep task groups owned by the serving task and
  close reconnect owners before shutdown, eliminating Python 3.13 AnyIO
  cancel-scope warnings without changing long-lived daemon behavior (#709).
- Failed Codex MCP registration now exits nonzero and reports `ok: false` in
  JSON setup output, preventing automation from treating an unusable setup as
  successful (#702).
- Corrected public configuration, surfacing, MCP-tool, and notebook guidance to
  match the v0.1.38 runtime contracts and keep copy/paste examples executable.
  (#697)

## [0.1.38] — 2026-07-14

### Upgrade notes

- The local daemon protocol is now v7. Upgrading may briefly leave one v6 and
  one v7 daemon, plus their warm LTM children, running for the same logical
  configuration. With the default 900-second idle timeout the v6 daemon exits
  naturally; pinned daemons (`idle_timeout_seconds=0`) remain until
  `mms daemon stop --all`. (#694)

### Added

- Context-compose schema 3 preserves budgeted adjacent chunks from compatible
  cores across direct and shared-daemon routes. Schema 2 remains supported for
  scoped Pinned Context without a visible context-window guarantee. (#694)
- Schema 3 window requests receive a larger wire-only retrieval budget while
  the configured model-injection limit remains unchanged. (#694)
- Added a copy/paste reviewed-project-resume guide covering project-local
  Pinned Context, automatic surfacing, nearest-neighbor previews, and the
  separate review-first proposal boundary. (#694)

### Changed

- Daemon protocol v7 negotiates a client maximum and selected compose schema
  per response. The protocol bump deliberately prevents a stale v6 daemon from
  silently suppressing schema 3 until restart. (#694)
- Schema 3 decoding now retains at most ten adjacent chunks per direction,
  preserving the nearest chunks and ignoring malformed overflow that STM will
  never render. (#695)
- Core compatibility advisory now pins 0.3.8 legacy, 0.3.9 schema 2, and 0.3.10
  schema 3 as separate scheduled cells. Its schema 3 cell also runs the
  reviewed-project-resume core CLI and candidate-review contract in a temporary
  Git project. (#695)

## [0.1.37] — 2026-07-13

### Upgrade notes

- The local daemon protocol is now v6. Finite-idle older daemons exit naturally;
  pinned stale daemons can be removed with `mms daemon stop --all`. A brief
  one-child-per-version overlap is possible during upgrade.
- The inactive bundled INDEX surface has been retired. `stm_index_stats` is no
  longer advertised and the standalone pipeline is CLEAN → COMPRESS → SURFACE;
  integrations must use an explicit, capability-negotiated LTM contract. (#690)

### Added

- Standalone `mms` surfacing can opt into the shared local daemon with
  `MEMTOMEM_STM_SURFACING__USE_DAEMON=true`. Search, scratch context,
  and helpful-feedback boosts share one daemon-owned LTM connection per
  matching config, while each proxy keeps its feedback/cache/tuning state.
  Missing or busy daemons fail open without spawning a private child.
  Daemon admission also converts an unexpected operation exception into a
  logged `unavailable` response; hook surfacing already catches its own
  exceptions and continues to fail open. (#689, issue #688)
- Capability-negotiated LTM integration now supports pinned-first context
  bundles and opt-in review-candidate submission. Compatible cores advertise
  `context_compose` / `candidate_propose` through `mem_do(action="version")`;
  legacy cores retain the existing structured/compact search path. Formation
  is disabled by default and never falls back to a direct durable write (#691).
- Daemon protocol v6 carries schema-bound context-compose scope and
  candidate-proposal operations without sharing mutable MCP session handles
  between proxy clients (#691).

### Fixed

- Context composition now requires `context_compose` schema 2 and preserves
  per-tool/default namespace plus context-window settings across direct and
  shared-daemon routes. **Behavior change**: schema 0/1 cores use legacy search;
  an advertised schema 2 failure remains visible as an LTM dependency fault
  instead of being hidden by a second search request (#692).

### Docs

- Reorganized the public documentation around a shorter first-success path,
  task-oriented guides, and split CLI/configuration references while keeping
  the existing documentation URLs compatible. Corrected native-hook host
  paths, CLI signatures, configuration-source boundaries, and previously
  undocumented public settings. (#687)
- Corrected the standalone pipeline after #690 retired the inactive bundled
  INDEX surface, qualified the token-reduction claim as workload-dependent,
  and added reproducible use-case boundaries for compression, caching, and
  surfacing (#691).

## [0.1.36] — 2026-07-12

### Upgrade notes

- Repeated healthy LTM searches whose candidates remain below `min_score` now
  emit and persist a score-scale diagnostic after five consecutive non-empty
  misses. STM does not lower `min_score` automatically; check whether the LTM
  is running without embedding extras or the configured threshold is
  intentionally high. (#684)

### Fixed

- Repeated healthy LTM searches whose candidates all score below the active
  `min_score` now produce a durable score-scale diagnostic after five
  consecutive non-empty misses. `mms stats` warns that the LTM may be running
  single-leg/BM25-only (or that the configured threshold may be intentionally
  high) without silently lowering the threshold. **Behavior change**: this
  previously silent `no_results_score` pattern now emits one warning per
  detected episode and appears in the on-disk stats summary. (#684, issue #672)

### Docs

- Expanded the optional toolgraph setup guide with a concrete
  `server_name_map`, an upstream-direct crawl recipe, and accurate
  `TOOL_NOT_FOUND` troubleshooting guidance. (#685, issue #652)

## [0.1.35] — 2026-07-12

### Upgrade notes

- Reinstall legacy Claude hooks so the generated command includes
  `--host claude`. Bare legacy `mms hook` registrations now fail safe by
  disabling native output replacement because auto-detection cannot reliably
  distinguish Claude from Codex. (#682)
- Large-context models no longer raise configured surfacing budgets to 5000
  characters / 5 results; `max_injection_chars` and `max_results` are now hard
  ceilings. Increase those settings explicitly to retain the larger budget.
  (#678)
- Upstream `isError` results now retain their complete MCP result envelope
  instead of becoming text-only `ToolError` responses. Consumers that assumed
  text-only errors should handle non-text content, `structuredContent`, and
  `_meta`. (#676)
- Explicit `_context_query` values and per-tool `query_template` output now
  obey `min_query_tokens`; raise the query length or lower that setting when a
  short non-sensitive query must still reach the LTM. (#676)

The surfacing/compression hardening below landed as tracking issue #676
(commits pushed directly to `main`) plus its follow-up PRs #677–#679.

### Added

- The surfacing size gate now measures the **cleaned, pre-compression**
  upstream response against `min_response_chars`, so compressing a large
  response before surfacing can no longer drop it under the gate. An
  agent-supplied `_context_query` is treated as an intentional retrieval and
  bypasses the size gate entirely; a per-tool `query_template` does not (the
  gate runs before query extraction). (#676, docs #679)

### Changed

- Hardened native-tool PostToolUse integration with explicit host capabilities
  (Claude surfacing plus safe opt-in Bash replacement, Codex surfacing-only,
  Cursor/Kimi metrics-only), a 65% minimum-retention compression guard, current
  Kimi paths/tool names, strict hook ownership recognition, and refusal to
  overwrite malformed host config structures. Daemon protocol v3 now carries
  an end-to-end monotonic deadline, bounds admitted requests, and does not idle
  down while a surface request is active. (#682)
- Model-context scaling of the surfacing injection/result budgets
  (`max_injection_chars`, `max_results`) is now a one-directional clamp: the
  configured value is a hard ceiling and only small-context consumer models
  (≤32K tokens) shrink it, to 1500 chars / 2 results. Mid- and large-context
  models receive the configured value unchanged, so budgets are now monotonic
  in context-window size. **Behavior change**: the previous large-context tier
  (>200K tokens) returned `max(configured, 5000 chars / 5 results)` — it could
  raise a small configured budget *up* to 5000/5. It now returns the configured
  value, so a >200K-token consumer model whose configured budget is at or below
  5000 chars / 5 results — **including the defaults (3000 chars / 3 results)** —
  now gets that configured value instead of being bumped to 5000/5. Budgets
  configured above 5000/5 were already returned unchanged; the ≤32K and
  32K–200K tiers are unchanged from the previous release. (#678)
- **Behavior change**: an upstream `isError` tool result is now proxied back as
  its full `CallToolResult` envelope — non-text content, `structuredContent`,
  and `_meta` preserved — instead of being collapsed into a text-only FastMCP
  `ToolError`. (#676)
- LTM dependency failures during surfacing (upstream `isError`, transport /
  call errors, and malformed or empty-content responses) now count toward the
  surfacing circuit breaker, so a persistently broken LTM opens the breaker
  instead of silently passing responses through untouched; a genuine empty
  result set is not counted as a failure. (#676)
- **Behavior change**: an explicit `_context_query` and a per-tool
  `query_template` are now subject to the `min_query_tokens` floor, the same as
  heuristic queries — previously they were used verbatim. A query below the
  floor is skipped (`no_query`) unless it contains sensitive content (which is
  hashed and still used). (#676)

### Fixed

- Only memories actually present in the injected block — after the id-display
  gate and injection-size truncation — are committed to feedback, session /
  cross-session dedup, and the surfacing webhook. A memory whose id cannot be
  rendered as a copyable token is now delivered as an id-less bullet and
  committed once, rather than dropping the whole block (all non-displayable) or
  re-surfacing on every subsequent call (mixed with displayable ids). (#676,
  #677)
- Selective compression keeps single-section documents losslessly retrievable
  through follow-up reads instead of plain-truncating them, and repeated
  Markdown headings / colliding dotted JSON paths no longer overwrite each
  other in the chunk map. (#676)
- Selective and progressive pending-selection eviction are scoped by format, so
  one strategy's TTL / size eviction no longer discards the other's entries
  from the shared store. (#676)
- A response whose background INDEX / EXTRACT work failed or is still
  unresolved is not written to the response cache, so a later cache hit (which
  bypasses those stages) cannot strand the ingestion work — it is retried on
  the next live call. (#676)
- Auto-tuning no longer re-applies the same `min_score` adjustment on repeated
  calls when no new feedback has arrived: within a process, a per-tool
  watermark over the durable feedback counts short-circuits the tuner, so a
  tool's threshold no longer drifts toward its bound just from being surfaced
  again. (The watermark is in-process, so the first tuner pass after a restart
  may still apply one adjustment.) (#676)
- Session-context (scratch) retrieval during surfacing is capped at 0.5s, so a
  stalled scratch dependency can no longer consume the whole surfacing timeout
  and suppress otherwise-valid LTM memories. (#676)

### Security

- Sensitive request context (credentials / PII detected in the extracted query)
  is replaced with a stable `sha256` digest before it is sent to the remote LTM
  as a search query, so raw secrets never leave the proxy while cache and
  cooldown behavior are preserved. (#676)
- Surfacing feedback now verifies the rated `memory_id` belongs to the cited
  surfacing event before recording it, rejecting mismatched pairs. (#676)

## [0.1.34] — 2026-07-12

### Upgrade notes

- **Background LTM warm-up is enabled by default** (#664, #671): server and
  daemon startup now launches a best-effort, host-owned LTM warm-up task so
  the usual ~9s child/model cold start is paid before the first surfacing
  call. It never blocks the proxy MCP initialize handshake, and lazy start
  remains the retry path after failure. Set
  `MEMTOMEM_STM_SURFACING__WARMUP_ENABLED=false` when spawning one LTM child
  per proxy process is undesirable, such as with many short-lived proxies.

### Added

- Background LTM warm-up at server/daemon startup, controlled by the new
  `surfacing.warmup_enabled` setting (default: `true`). (#664, #671)
- Durable surfacing fault counters (#666): timeout / breaker-open / degraded-LTM
  skips now persist day-aggregated to `surfacing_faults` in the feedback DB,
  and `mms stats` renders them with a warning line — previously these signals
  lived only in per-process memory (`stm_surfacing_stats`), so a surfacing
  pipeline dead on LTM timeouts for days looked merely quiet from the CLI.
  Rows share the `stats_retention_days` sweep.

### Security

- Surfaced memory fields are now serialized as inert, single-line Markdown
  data before injection. Control/bidi characters, HTML delimiters/entities,
  Markdown delimiters, and Unicode compatibility confusables can no longer
  create a second `<surfaced-memories>` boundary. Invalid memory ids omit only
  the copyable id token; feedback for the surfacing remains available.
  ([GHSA-43hx-xm7w-3mhj](https://github.com/memtomem/memtomem-stm/security/advisories/GHSA-43hx-xm7w-3mhj))
- LTM recovery is generation-aware and shares exactly one reconnect flight per
  dirty session. Caller cancellation no longer aborts a reconnect for other
  waiters, and cancellation during any post-reconnect retry dirties only that
  new session generation.
  ([GHSA-72jh-722p-rqr3](https://github.com/memtomem/memtomem-stm/security/advisories/GHSA-72jh-722p-rqr3))
- Raised runtime and optional-extra dependency floors to patched versions.
  Locked runtime and extras audits are now blocking CI and release gates;
  the full development closure remains advisory. (#673)

## [0.1.33] — 2026-07-11

### Upgrade notes

- **Per-server retry/timeout/cache knobs now hot-reload** (#660): `max_retries`,
  `call_timeout_seconds`, `overall_deadline_seconds`, reconnect delays,
  `cache`, and `cache_ttl_seconds` previously froze at connect time; they are
  now read per call from the hot-reloaded config. Connection-affecting edits
  (`transport`/`url`/`headers`/`command`/`args`/`env`) reconnect live on the
  next uncached call. `prefix` and `circuit_*` stay restart-only.
- **`connect_timeout_seconds` is now an end-to-end connect/discovery
  deadline** (#660): one shared budget over transport entry, MCP
  `initialize()`, and `tools/list` — previously only `initialize()` was
  bounded, so a hung connect or stalled discovery blocked forever.
- **One-time response-cache wipe on first start after upgrading** (#659,
  #655): the cache narrows to envelope-safe rows (successful, text-only, no
  `structuredContent`/`_meta`) and the key gains the call's `_context_query`
  plus a compression-config fingerprint; both changes bump the cache schema,
  purging existing rows once (entries repopulate on use).
- **Non-text upstream errors now surface as tool errors** (#659): previously
  they were returned as successful non-text responses.
- **New config files write `"tool_annotation_policy": "strict"`** (#658):
  fresh setups cache only tools declaring `readOnlyHint: true`. Existing
  configs keep the `conservative` default and are never retro-migrated;
  loading one logs a migration advisory.
- **`mms add` rejects a duplicate `--prefix`** (#654): exit 1 with the config
  file untouched, instead of warning and saving a config the runtime
  validator then refuses to load.
- **Registration aborts on a corrupt `.mcp.json`** (#653): `mms init` /
  `mms register` now fail with exit 1 and leave the file byte-identical,
  instead of "succeeding" by overwriting it with only the memtomem-stm entry.

### Added

- `mms doctor` — one read-only diagnostic pass over the whole setup:
  config file presence, JSON validity, schema validation, per-transport
  required fields, prefix conflicts (same shared validators the server's
  load path enforces), a staged connection probe per upstream, the
  cache-policy advisory, and the LTM probe. Each check prints
  `PASS`/`WARN`/`FAIL` with a runnable `next:` command; exit code is 1 on
  any FAIL and 0 on WARN-only, making doctor the scriptable success gate
  for a fresh install. The LTM check never FAILs — an unconfigured or
  unreachable LTM only disables memory surfacing, not the proxy core. (#661)
- Staged probe results in `mms health` / `mms add --validate`: a failing
  upstream now reports the last stage that completed (`configured →
  transport connected → MCP initialized → tools discovered`) instead of a
  bare boolean, so a dead binary, a broken MCP handshake, and a failing
  `tools/list` are distinguishable. `health --json` server entries gain
  additive `stage` / `failed_stage` / `transport` keys (existing
  `connected` / `tools` / `overflowing` / `error` fields unchanged). (#661)
- Live reconnect for connection-affecting per-server config edits.
  Hot-reloaded changes to `transport` / `url` / `headers` / `command` /
  `args` / `env` (and `connect_timeout_seconds` on network transports) are
  now applied on the next uncached call to that upstream: the replacement
  connection is fully established first, then swapped in and the old one
  closed. If the edited config can't connect, the old connection keeps
  serving and the failed edit is attempted once — not per call — until the
  config changes again. Reconnects (including failure-triggered ones) now
  read the current config snapshot instead of the connect-time one.
  **Behavior change**: per-server retry/timeout/cache knobs (`max_retries`,
  `call_timeout_seconds`, `overall_deadline_seconds`, reconnect delays,
  `cache`, `cache_ttl_seconds`) previously froze at connect time; they are
  now read per call from the hot-reloaded config, as the docs' hot-reload
  table already promised. A successful config-change reconnect also closes
  the upstream's circuit breaker, so a fixed `url` isn't fast-failed by the
  old config's failure streak. `prefix` and `circuit_*` stay restart-only.
  (#660)
- **Behavior change**: `connect_timeout_seconds` is now an end-to-end
  connect/discovery deadline — one shared budget over transport entry
  (process spawn / HTTP connect), MCP `initialize()`, and the `tools/list`
  discovery call, applied identically at first connect and every reconnect.
  Previously only `initialize()` was bounded, so a hung TCP connect or a
  stalled `tools/list` blocked forever; a slow phase also no longer grants
  later phases a fresh window. For `sse` / `streamable_http` the value is
  additionally passed to the SDK client factory as its HTTP connect
  `timeout=` (the stream read timeout stays at the SDK default). The
  per-upstream timeout contract (connect/discovery vs per-attempt
  `call_timeout_seconds` vs `overall_deadline_seconds`) is now documented
  in `docs/configuration.md`. (#660)
- MCP result-envelope preservation through the proxy. tools/list now
  advertises the upstream tool's `outputSchema` and `_meta`; call results
  carrying `structuredContent` or result-level `_meta` return them verbatim
  (text content is still compressed — clients consuming `structuredContent`
  get full fidelity), and content-block order is preserved (the processed
  text is reinserted at the upstream's first-text position instead of
  always leading). **Behavior change**: the response cache narrows to
  envelope-safe rows — only successful, text-only responses without
  `structuredContent`/`_meta` are stored (errors and non-text responses
  were already uncached) — and the cache schema bump wipes existing rows
  once on first start after upgrading (one-time cold start; entries
  repopulate on use). (#659)
- **Behavior change**: new config files are created with an explicit
  `"cache": {"tool_annotation_policy": "strict"}` — `mms init`, `mms add`
  against a missing config, and `mms add --from-clients` all write it, so
  fresh setups cache only tools that declare `readOnlyHint: true`. The
  schema default stays `conservative`: existing configs without the key
  keep their behavior and are never retro-migrated, but loading one now
  logs a one-line migration advisory (also shown by `mms config validate`;
  suppressed when the cache is disabled or an env override sets the
  policy). The per-tool / per-server `cache: true` override is the
  strict-mode allowlist for un-annotated read-only tools — no new setting.
  (#658)
- `mms add --header KEY=VALUE` (repeatable) registers HTTP headers for
  `sse` / `streamable_http` upstreams. Headers now also flow through
  `--from-clients` / `mms init` import discovery and the `mms add
  --validate` / `mms health` probes — previously the CLI dropped them
  everywhere even though the config schema and runtime transport already
  supported them, so header-authenticated servers failed import and probe.
  Header values are stored in plaintext in the config file (0600 perms);
  `--json` outputs mask them. `--header` with `--transport stdio` is
  rejected (`header_requires_http`). (#656)

### Fixed

- Probe failure messages no longer echo configured secrets. Upstream and
  LTM probe errors rendered by `mms health` / `mms doctor` /
  `mms add --validate` / import flows are sanitized against the server's
  own `env` and `headers` values and URL credentials (longest-first,
  empty-safe replacement) before display — previously an upstream
  exception that embedded an Authorization header (e.g. a 401 body)
  reached the terminal and `--json` output verbatim. (#661)
- **Behavior change**: a non-text-only (or empty-content) upstream error
  now surfaces as a tool error. Previously the no-text passthrough
  early-return ran before the `isError` check, so such an error was
  returned to the client as a successful non-text response; it now raises
  with a `[upstream error: non-text error content]` placeholder message
  and is recorded as an upstream error in the metrics. (#659)
- `mms add`'s `invalid_env` diagnostics no longer echo the raw `--env`
  argument: a malformed pair (`--env =tok`, or a bare token missing `KEY=`)
  is likely a stray credential, and both stderr and the `--json` error
  payload are routinely piped to CI logs. The messages now name the 1-based
  argument position instead. The dangerous-key diagnostic still names the
  offending KEY (keys are not secret-bearing). Mirrors the `--header`
  diagnostics in #656. (#657)
- **Behavior change**: the response-cache key now includes the call's
  `_context_query` and a fingerprint of the resolved compression settings.
  The cache stores the *compressed* body, and compression is query-aware
  (BM25 relevance budgets) and config-dependent — previously the same
  tool+args under a different query context, or after a compression-setting
  hot reload, was served a body compressed for another query / under old
  settings. Consequences: a one-time full cache purge on first start after
  upgrade (key schema v2, tracked via SQLite `user_version`); callers passing
  a per-turn-varying `_context_query` re-fetch instead of hitting the cache;
  compression-config changes immediately stop old rows from being served.
  (#655)
- **Behavior change**: `mms add` now rejects a duplicate `--prefix` (exit 1,
  `duplicate_prefix` in `--json` mode, config file untouched) instead of
  warning and saving a config the proxy's runtime validator then refuses to
  load. The interactive prefix prompts (`mms init`, `add --from-clients`)
  re-prompt on a colliding prefix. Prefix format/uniqueness rules now live in
  one shared module (`proxy/prefixes.py`) used by both the CLI pre-save
  checks and the runtime `ProxyConfig` validators, so the two sides cannot
  diverge again. (#654)
- **Behavior change**: client registration (`mms init` / `mms register`) now
  aborts when the target directory's `.mcp.json` cannot be parsed or merged,
  instead of overwriting it. The writer fell back to `{}` when the existing
  file was unreadable or invalid JSON — the subsequent write silently
  discarded every registration already in the file — and replaced a
  valid-but-wrong-shape top level or `mcpServers` wholesale. Registration now
  fails with exit 1 and leaves the file byte-identical, reporting the JSON
  parse position and a recovery hint; a missing file still creates a fresh
  one, and a valid file still merges, preserving sibling registrations and
  unknown top-level fields. (#653)

## [0.1.32] — 2026-07-05

### Upgrade notes

- **Daemon file logging is now hardened and rotating** (#626): the detached
  daemon's `stm-daemon.log` moves onto the shared private handler, gaining
  `0o600`/`0o700` permissions and 2 MiB × 3 rotation it lacked; a file-open
  failure there now propagates instead of degrading (stdio is `DEVNULL`, so
  the file is the only crash trace). A malformed `MEMTOMEM_STM_LOG_LEVEL` now
  raises loudly at server startup instead of silently falling back to
  `WARNING`.
- **Per-upstream circuit breaker is on by default** (#620): after 3
  consecutive failed calls an upstream's tools fail fast with `circuit_open`
  for up to 60s instead of each call burning the full retry/deadline budget.
  Set `circuit_max_failures: 0` on an upstream to restore the old
  always-retry behavior.
- **Embedding-scorer compression moved off the event loop** (#628): with an
  embedding relevance scorer configured, compression now runs on a worker
  thread so one slow endpoint no longer stalls concurrent calls. The default
  BM25 path is byte-identical; per-call semantics are unchanged.
- **`mms status` no longer prints per-server rows** (#645): status is now a
  config summary; per-server detail (including the surfacing toggle) lives in
  `mms list`, which gains a SURFACING column. Scripts scraping status's human
  `compression=` / `surfacing=` lines must switch to `mms list` or
  `mms status --json` — the JSON shape is unchanged apart from two additive
  keys (`server_count`, `pruned_count`).

### Security

- **CI supply-chain hardening** (#627, issue #609) — the release pipeline was already hardened (PyPI Trusted Publishers OIDC, least-privilege `id-token: write`), but CI itself had no supply-chain defenses. Every workflow `uses:` ref is now pinned to a full commit SHA with the version tag in a trailing comment (previously mutable tags — `@v4`, `@release/v1` — which a compromised upstream repo can move silently); `ci.yml` and `release.yml` gain a top-level `permissions: contents: read` baseline (jobs with elevated needs — `bench_qa_pr_comment`, `publish` — already carry job-level blocks, which fully replace the top level); a new `.github/dependabot.yml` covers the `github-actions` and `uv` ecosystems weekly with minor/patch updates grouped (Dependabot maintains SHA pins natively, and the existing `dependabot[bot]` CLA allowlist entry becomes functional); and a new advisory `audit` CI job runs `pip-audit` against the exported lockfile (`continue-on-error: true`, mirroring the mypy convention — the current baseline has known CVEs in transitive dev deps, so it starts advisory; promote to required once Dependabot cleans the baseline). CI/infra only — no runtime code touched.

### Added

- **`--json` result summaries for the mutating CLI commands** (#644, issue #614) — `mms add` / `remove` / `prune` / `eject` only spoke human text, so scripting them meant scraping colorized output; the read-only commands (`status`/`list`/`stats`/`health`/…) have taken `--json` for a long time. Each now emits a single JSON result document on stdout with a shared envelope: `action` + `ok` on every payload, `error` (stable snake_case code) + `message` on failures, all keys always present. Following the `mms host sync --json` precedent, `--json` is an output format, not consent: `remove`/`prune`/`eject` without `--yes` (and not `--dry-run`) refuse with exit 2 and a `confirmation_required` envelope instead of prompting — even on a TTY — and the eject secret gate fails the entry at plan time rather than confirming. stdout stays pure JSON (human stdout lines move into the payload or `warnings`; stderr diagnostics unchanged); operational failures keep exit 1. Secrets stay out: `add`'s `server` block is redacted like `list --json`, and eject never serializes the captured host original into `plan` rows (`failed[].hint` embeds the restore payload exactly like the existing stderr fallback hint — treat failed-eject `--json` output like the config file). `init` deliberately stays without `--json` — it would need a fully non-interactive wizard first. `add --json --from-clients` is a usage error (interactive selection flow).
- **`mms tune` — preview and apply per-tool tuning recommendations** (#642, issue #615) — `stm_tuning_recommendations` computes per-tool `max_result_chars` / `compression` suggestions but ended with "apply manually to `stm_proxy.json`", leaving the user to hand-edit the nested per-tool override block — exactly the error-prone step the recommendations were meant to remove. `mms tune` runs the same `CompressionTuner` analysis offline against the on-disk metrics/feedback stores (no running server needed; a preview never creates the DB files) and renders the `tool_overrides` diff it would write; `--apply` writes the accepted overrides through the raw-dict load/save path (unknown keys survive) under the config write lock, after snapshotting the config to a timestamped `stm_proxy.json.bak-<UTC>` slot (mode 0600, non-clobbering) — a running proxy hot-reloads the file, so the echoed one-command `cp` restore is the safety net. Selection is per `(server, tool)`: `--yes` applies all, a TTY gets the enter-to-toggle picker with everything pre-selected (`MMS_NO_TUI` degrades to per-tool confirms), and non-TTY without `--yes` errors loudly. Colliding tuner actions are merged before writing (`compression` last-wins — agent-reported feedback beats the latency pin; `max_result_chars` takes the numeric max), the mutated dict is schema-validated before anything is written (never produce a config a running server silently ignores, #611), and recommendations for upstreams present only in metrics (env-defined / renamed) are reported as skipped rather than written. `--since-hours` / `--tool` mirror the MCP tool's parameters; `--json` emits the preview for scripting.
- **Observability discoverability hints — data source, hidden tools, inert INDEX** (#641, issue #613) — three gaps in an otherwise strong observability surface let a user reach a wrong conclusion or miss tools entirely; all pure presentation, no counter semantics change. (1) `mms stats` reads only the on-disk stores while `stm_proxy_stats` reports live in-memory counters (cache hits/misses, latency percentiles, reconnects, RPS, progressive and hint events — never persisted), so comparing the two silently diverged with the caveat buried in the command docstring; the human output now carries a `Data   :` line and `--json` a `data_source` key naming the on-disk source and pointing at the live MCP tool. The divergence is structural (those counters are never written to disk), not a flush lag, so there is nothing to reconcile. (2) 9 of the 13 observability tools are hidden behind `advertise_observability_tools` (default off) with no runtime hint they exist; `mms health` now appends `"9 observability tools hidden; set MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=true to expose them"` (plus `obs_tools_hidden`/`obs_tools_hint` JSON keys), driven off the same `_should_advertise_obs_tools()` signal that gates registration and counting a new `_OBSERVABILITY_TOOL_NAMES` source-of-truth tuple so the number can't drift. The hint lives on `mms health` (a CLI command, always available) rather than `stm_proxy_health` because that MCP tool is itself gated — it is unreachable over MCP in the exact flag-off state where the hint would apply. (3) `stm_index_stats` returned the generic "No INDEX activity recorded" message even when the INDEX stage is structurally unwired (the bundled `mms` server passes no index engine, #288) — indistinguishable from wired-but-idle; a new public `ProxyManager.index_engine` accessor (parity with `selection_log`) lets the tool render a distinct `"INDEX stage inactive in this server (no index engine wired) — see #288."` instead. This only improves the message — it does not pre-empt the #288 retire-vs-wire decision.
- **Per-upstream circuit breaker on the proxied `call_tool` path** (#620, issue #608) — SECURITY.md has claimed "per-upstream circuit breaker isolates failures" since the resilience section was written, but the upstream fetch path had none: a persistently-failing upstream was retried on every call, each caller paying the full retry/deadline budget (default up to 180s) indefinitely, with no fast-fail and no isolation beyond the shared timeouts. Each upstream connection now carries its own `CircuitBreaker` (the same 3-state pure-read primitive the surfacing engine and LLM compressor/extractor already use, #600). Failure semantics: **one count per call, not per attempt** — only a call that exhausts its retry/deadline budget on a transport fault or timeout counts (the three terminal exits: retries-exhausted/replay-unsafe, overall-deadline exceeded, and a mid-loop reconnect failure); any completed round-trip records success — a tool `isError` result and a JSON-RPC protocol error (-326xx) both prove the upstream replied, so both reset the failure streak — while a programming/internal exception (no round-trip) does neither. After `circuit_max_failures` (default 3) consecutive failed calls the breaker opens and calls to that upstream fast-fail with a new `circuit_open` error category naming the upstream and the time until the next probe; after `circuit_reset_seconds` (default 60) the next call probes and its outcome commits the state. Cached responses keep serving while the breaker is open (the fast-fail sits after the cache fast-path), other upstreams are unaffected, and `stm_proxy_health` renders a per-upstream `circuit breaker:` line from the same pure-read state labels as the surfacing breaker line. `circuit_open` rows count as upstream-attributable in the tool-exposure health filter so fast-fails don't dilute per-tool error rates during an outage. New per-upstream config `circuit_max_failures` / `circuit_reset_seconds` (`0` disables; connect-time snapshot like `max_retries`). **Behavior change**: after 3 consecutive failed calls an upstream's tools fail fast with `circuit_open` for up to 60s instead of each call burning the full retry/deadline budget; set `circuit_max_failures: 0` on an upstream to restore the old always-retry behavior.
- **`mms config validate`, unknown-key warnings, and visible parse failures** (#625, issue #611) — the proxy config models keep pydantic's default `extra="ignore"` (so an older binary tolerates fields a newer CLI wrote), which means a typo'd key silently vanishes across the 100+-field surface, and `load_from_file` collapsed "file present but broken" into the same `None` as "file missing" behind only a stderr warning — invisible for an MCP-launched stdio server whose stderr the client captures or drops. Three additions close the gap: (1) an annotation-driven `find_unknown_keys` walker names every dropped key as a dotted path (descending into `dict[str, Model]` fields like `upstream_servers` / `tool_overrides` but never into free-form dict leaves — `env`, `headers`, `server_name_map`, `origin.original` — classified off `model_fields` annotations, not a name allowlist, so it tracks model evolution), and unknown keys found in the raw file dict (before the env merge, so env-injected keys are never misattributed) are logged as one aggregated warning per load; (2) `load_from_file_with_status` returns `ConfigLoadResult(config, error, unknown_keys)` with `error` set iff the file exists but failed to parse — `load_from_file` delegates with unchanged signature/behavior — and `stm_proxy_health` now surfaces a parse failure as its cause instead of only the downstream "No upstream servers configured." symptom; (3) a new `mms config validate` command reports both the parse status and the unknown-key list for a config without starting the server.
- **Opt-in rotating file log under `~/.memtomem/`** (#626, issue #612) — the MCP stdio server logged to stderr only, which the launching client captures or drops, so diagnosing "why did my proxy do nothing" (the dark-failure modes in #611 are only visible there) meant hunting per-client log locations. A new `logging_setup.py` adds `PrivateRotatingFileHandler` (`0o600` file via `os.open`+`fchmod`, parent `0o700`, 2 MiB × 3 backups) wired through `configure_server_logging` (stderr always on; opt-in file via `MEMTOMEM_STM_LOG_FILE`, degrading to stderr-only on `OSError`); `server.main()` now folds `log_level` + `log_file` into one `STMConfig()` env surface, so a bad `MEMTOMEM_STM_*` value fails loudly at startup with a logged `ValidationError` instead of the lifespan raising it later unlogged, and `mms health` prints the active log destination (text + `logging` JSON key). The daemon's own `stm-daemon.log` converges onto the same hardened handler (see Upgrade notes). Redaction is untouched — it happens at message-construction time (`manager.py:_redacted_error`), so the file handler inherits it for free.
- **Startup warning when an LLM path runs with the privacy scan disabled** (#640, issue #610) — with `privacy_scan_enabled=false`, `ProxyManager` passes `None` for `privacy_patterns`, so raw upstream responses reach the external LLM provider without the #289 credential redaction; the scan is default-on, but an operator who flipped it off got no signal anywhere that credentials now leave the machine unscanned. Mirroring the #288 "config enabled but inert" startup warnings, `ProxyManager.start()` now scans the resolved compression and extraction paths and logs a WARNING naming each enabled site and the destination provider/endpoint when the scan is off. Scoped to *external* destinations via a new `LLMCompressorConfig.is_external_destination()` — OpenAI/Anthropic always qualify, Ollama only on a non-loopback `base_url` — so the common local-Ollama scan-off setup (text never leaves the box) stays silent; extraction's warning is further gated on `index_engine is not None` (the bundled `mms` server wires none, #288), and only an explicit `compression: llm_summary` is flagged, not `auto` (which resolves at runtime and would produce static false positives). Part 1 of #610; the entropy heuristic (part 2) is out of scope.

### Changed

- **`mms status` is a config summary; per-server detail moved to `mms list`** (#645, issue #614) — the two commands printed near-identical output (both enumerated every server's prefix/transport/compression/command), so neither had a clear job. `status` now answers "is the proxy set up and pointed at the right config": path, enabled flag, schema-validation warning, and `Servers: N (P host-pruned)` — the pruned count reuses `_origin_fully_pruned`, the same predicate behind `list`'s `*` marker and `remove`'s orphaning hint, so the three surfaces cannot disagree — plus pointer lines at `mms list` / `mms health` (or an `mms add` hint when empty). `list` stays the per-server view and gains a SURFACING column (the visible home of the per-server `mms surfacing` toggle, since status no longer shows it); deliberately no MAX_CHARS column — the effective value is per-tool once `mms tune --apply` writes `tool_overrides`, so read it via `--json` or the config file. **Behavior change**: `mms status` human output no longer prints per-server rows — scripts scraping its `compression=` / `surfacing=` lines must switch to `mms list` or `status --json`; `status --json` itself is unchanged apart from two additive keys (`server_count`, `pruned_count`), keeping the full redacted `servers` map that scripted consumers and the #476/GHSA redaction pins depend on.
- **mypy is now a required CI gate on both platforms** (#638, issue #617) — typecheck had been advisory (`continue-on-error: true` on the mypy step) since the Windows matrix landed, which let real errors accumulate unseen. The #617 burn-down fixed all 9 baseline errors and re-enabled the globally-disabled `attr-defined` error code (#635, 19 more errors fixed — including typing the pending-store params with the existing `PendingStore` Protocol and a `FeedbackDbStatus` TypedDict for `inspect_feedback_db`); this change then fixes the 9 remaining **Windows-only** errors the advisory step had been masking (POSIX-only stdlib usage — `fcntl` in `mms/state.py`, `os.getpgid`/`os.killpg`/`signal.SIGKILL` in `daemon/server.py` — now behind `sys.platform` guards mypy can narrow; all three sites were already runtime-unreachable on Windows, so the guards are runtime-equivalent) and drops the `continue-on-error` flag, so `typecheck (ubuntu-latest)` / `typecheck (windows-latest)` fail red on any new mypy error. The two typecheck contexts are added to branch protection's required checks alongside lint/test. CLAUDE.md, README, and CONTRIBUTING updated to match (`mypy` moves from "advisory" to must-pass). The `audit` job's pip-audit step is now the only advisory step in CI, on the same promote-when-baseline-clean trajectory (#609).

### Fixed

- **Tuner feedback no longer pools across servers sharing a tool name** (#643) — `get_tool_feedback_summary` aggregated compression-feedback rows with `GROUP BY tool, kind`, dropping the `server` column that `record()` persists, and the tuner joined feedback onto profiles by raw tool name — so with two upstreams exposing the same tool name, one server's feedback reports surfaced on the other server's profile and the feedback heuristic recommended tuning the wrong server's `tool_overrides`. Display-only wrong attribution in `stm_tuning_recommendations` so far; caught by review of the `mms tune --apply` PR (#642), which would have turned it into an automatic wrong config write. The aggregate is now keyed by `(server, tool)` — the identity every other per-tool store uses — and the tuner joins on the pair; the summary's only consumer is the tuner, so the shape change has a single call site.
- **A slow embedding endpoint no longer stalls the whole proxy** (#628, issue #618) — `EmbeddingScorer` makes a synchronous httpx call inside `score_sections` while every consumer runs on the asyncio event loop, so with an embedding relevance scorer configured (opt-in; the default BM25 scorer is pure CPU) one slow or unresponsive Ollama/OpenAI endpoint froze every concurrent proxied call, surfacing pass, and health probe for up to `embedding_timeout` (default 10s) per compression — measured end-to-end, a concurrent 1-line tool call took ~3.5s behind a 4s embedding block. Scorers now declare `uses_blocking_io` (read via `getattr` with a `False` default, so custom scorers keep the inline path), and a new `ProxyManager._compress_maybe_offthread` helper routes all eight scorer-carrying sync `compress()` sites through `asyncio.to_thread` only when the flag is set; `fallback_count` increments under a lock so worker-thread failures can't lose a count against the loop-thread metrics read. **Behavior change**: with an embedding scorer configured, compression runs on a worker thread (the loop stays responsive; per-call wall time and the BM25-fallback-on-error semantics are unchanged); the default BM25 path takes no thread hop and is byte-identical. Follow-ups deliberately out of scope: embedding response cache, circuit breaker on the embedding path, full async conversion of `compress()`.
- **The mid-loop reconnect-failure log no longer leaks the credentialed upstream URL** (#623, issue #622) — the #605/#606 credential-redaction sweep scrubbed every reconnect/cleanup log site in `ProxyManager` except one: the mid-loop reconnect failure on `_fetch_upstream`'s retry-continue path logged the raw exception at `logger.error`. `_reconnect_server` re-raises the underlying connect error, and for an HTTP/SSE upstream that httpx error embeds `cfg.url` — which can carry userinfo credentials — so a reconnect failure during a retry storm wrote the token to the logs, at the default ERROR level (broader than the DEBUG siblings). The site now renders through the existing `_redacted_error` choke point (`redact_url_userinfo` + `MAX_ERROR_MESSAGE_CHARS` cap), matching the three sibling reconnect-failure sites in the same loop. Pure hardening — no behavior change beyond the log string; a per-site redaction regression test now covers the mid-loop path the original sweep missed.

## [0.1.31] — 2026-07-03

### Added

- **`stm_surfacing_stats` warns on a zero-variance score distribution** (#573, issue #560 step 3) — the flat-score state (every recorded surfacing score identical for weeks) was only discoverable by querying `stm_feedback.db` by hand; the stats tool reported the degenerate scores without flagging that they carry no ranking information, even though in that state the `min_score` filter is a step function at the single observed value and the auto-tuner walks a gradient that doesn't exist. `FeedbackStore.get_stats()` now returns a `score_distribution` aggregate (`count` / `min` / `max`), computed in the same pass — and against the same `tool=` / `since=` filter — as the per-tool breakdown, so the warning always describes exactly the window being rendered (`min == max` is the zero-variance predicate; booleans are excluded so a corrupt JSON `true` can't masquerade as score 1.0). The tool renders a `WARNING: zero score variance` line when 10+ recorded scores are all identical; below that, identical scores are expected small-sample noise. Zero-traffic output is byte-for-byte unchanged, and stats dicts without the new key degrade to silence. This is the regression tripwire for the #572 root-cause fix below: whatever the next cause of a degenerate score channel is, the stats output itself now says so.

### Fixed

- **Circuit-breaker `is_open` / `state` / `time_until_reset` are pure reads** (#602, issue #600) — the open→half-open transition was performed as a side effect of the property read, and `stm_proxy_health` renders the surfacing breaker by reading `is_open` — so once the breaker was open and `reset_timeout` had elapsed, *running a health check flipped the breaker to half-open and then reported `closed (healthy)`*: a read that both mutated and misreported the state it claimed to observe. A new `_effective_state()` helper computes the effective state (open past its reset window → half-open) without assigning `self._state`; state is now committed only by `record_success` / `record_failure`, which recompute the effective state so a probe failing within the elapsed window re-opens the breaker exactly as before — driven by the outcome instead of by a prior read. Single-probe half-open enforcement (never actually enforced) stays out of scope and the docstring is corrected to stop overclaiming it. **Behavior change**: none external — `is_open` returns the same values and the gate/probe lifecycle is preserved; reading `is_open` / `state` / `time_until_reset` no longer mutates the breaker, so `stm_proxy_health` reports the true state, and `time_until_reset` returns `None` at/after the reset window rather than `~0.0`.
- **`_reconnect_server` is serialized so a reconnect race can't orphan an `AsyncExitStack`** (#599, issue #586) — the reconnect path is called from four unserialized retry-loop sites; two concurrent calls for the same dead upstream could interleave across the transport-spawn / initialize / list_tools awaits, so the loser's freshly-built `AsyncExitStack` (wrapping a live stdio child + fds) was assigned and then overwritten by the winner and *never closed* — a leaked child process + fds per race, repeating under a retry storm against a flapping upstream. A per-server `asyncio.Lock` plus a reconnect generation counter (captured before acquiring the lock; bumped only on a *successful* reconnect) now makes a caller that finds the generation advanced return without reconnecting — removing the orphaned-stack leak and collapsing N concurrent attempts into a single transport spawn. **Behavior change**: none user-visible; concurrent reconnects for one server serialize and deduplicate instead of racing.
- **`atomic_write_text` gains opt-in `fsync` durability for config/state files** (#598, issue #585) — the temp-in-same-dir + `os.replace` funnel for every config/state write is crash-correct but had no `fsync` before the rename (nor a directory fsync after), so under power loss / kernel panic the rename could become durable ahead of the data blocks, leaving a zero-length or partial `stm_proxy.json` after reboot — and `mms init` refuses to recreate an existing config, so recovery is manual. A new `durable: bool = False` parameter flushes + `os.fsync`es the temp fd before the rename and best-effort fsyncs the parent directory after (POSIX; a dir-fsync failure never fails a write that already landed). Opted in for the single-source-of-truth low-frequency writers (all five `cli/proxy.py` writers, host-config apply, the four `mms/state.py` savers); the hot-path per-fact markdown writers and the ephemeral daemon handshake stay non-durable. **Behavior change**: config/state/registry writes now fsync before returning (one disk flush each); `durable` defaults off so hot-path writers are unaffected, and an unclean shutdown can no longer truncate a config file to zero bytes.
- **The four unbounded telemetry tables are now age-bounded** (#597, issue #584) — `surfacing_events`, `surfacing_feedback`, `progressive_reads`, and `compression_feedback` had no `DELETE FROM` anywhere and grew forever (`cleanup_expired_queries` only *nulls* the query column, keeping the row); compounding this, the default no-`since` `stm_surfacing_stats` call does an unbounded `fetchall()` + per-row `json.loads` synchronously on the event loop, so after a long deployment each stats call became a multi-second stall blocking every proxied call in flight. A config-gated age window now bounds all four: `SurfacingConfig.stats_retention_days` (default 90) drives a new `delete_events_older_than()` wired into the engine's opportunistic cleanup tick (running before the query-null pass), and `retention_days` (default 90) on `ProgressiveReadsConfig` / `CompressionFeedbackConfig` drives a once-per-process startup purge mirroring `ProxyCache`'s. Bounding the table is also what fixes the `get_stats` stall. Set the relevant knob to `0` to keep the prior keep-forever behavior. **Behavior change**: rows older than 90 days are deleted (surfacing rows on the cleanup tick, the two proxy tables on next start); no change to the data served to callers.
- **`stm_proxy_select_chunks` / `stm_proxy_read_more` reach the SQLite pending store after a restart** (#596, issue #583) — the opt-in `pending_store: "sqlite"` backend exists so pending TOC/progressive entries survive restarts, but both retrieval endpoints failed to reach it because the compressor and progressive store are built lazily inside a *compress* call: `select_chunks` early-returned "Selective compression not active" when `_selective_compressor` was still `None`, and `read_more` called `_get_progressive_store()` with no `sel_cfg`, constructing a fresh `InMemoryPendingStore` that reported "not found or expired" while the row sat live on disk. A new `_fallback_selective_cfg()` scans the configured servers (server-level then tool overrides, deterministically) for the first that opts into SQLite and builds the configured store when nothing has been built yet — for `read_more` only when `_progressive_store is None`, so a live in-memory store still holding keys isn't clobbered. Both degrade to their existing sentinel strings (with a WARNING) on a SQLite open failure and pin no bad state. **Behavior change**: under `pending_store: "sqlite"`, the two retrieval endpoints now recover keys persisted before a restart instead of reporting "not active"/"not found"; no change for the default `memory` backend.
- **The `mms project` mutators are write-locked against lost updates** (#595, #604, issue #582) — every registry-domain CLI mutator runs under the cross-process write lock *except* `mms project`, which did not import `_write_lock` at all: its `init` / `enable` / `disable` / `list --prune` each did an unlocked read-modify-write of the shared `~/.mms/projects.toml`, so two concurrent runs (plausible with parallel agent sessions) both load the same base index and the loser's save silently drops the winner's row — no corruption, but a dropped project stops resolving by name until re-registered. `init` / `enable` / `disable` get the `@with_write_lock` decorator; `list --prune` wraps its load→prune→save span in `with state.write_lock(enabled=prune)` so a plain read-only `list` stays unlocked. The `WriteLockTimeout` attribution hint was also broadened to name the `mms project` mutators alongside the registry/sidecar commands that share the lock (#604, codex review of #595). **Behavior change**: concurrent `mms project init/enable/disable/list --prune` now serialize on the registry lock; a run that can't acquire it within the timeout fails with a clean attributed `Error:` (exit 1) instead of racing. No change to single-process usage or read-only `show` / `list`.
- **A detached daemon startup crash now logs its traceback** (#594, issue #581) — the detached child runs with `stderr=DEVNULL`, and `serve()`'s `try/except` guarded only `open_lock_fd`, so an exception in `_build_engine()` or `asyncio.start_server` propagated uncaught through `asyncio.run` to an interpreter traceback on the devnulled stderr — never through a logger — leaving `stm-daemon.log` empty while `mms daemon start` told the user to "check the daemon log". `run()`'s `asyncio.run(...)` is now wrapped in a top-level exception barrier mirroring the MCP server's #209 barrier: a clean anyio cancel-scope teardown logs at WARNING and returns 0, any other exception is `logger.exception`-ed (landing in `stm-daemon.log`) and re-raised so the exit code still reflects the failure. Pure observability — no behavior change on a healthy start.
- **Startup-failed upstreams are surfaced in `stm_proxy_health`** (#593, issue #580) — startup degrade for a failing upstream was already correct (`_connect_server` failure is caught, other upstreams proceed), but the degradation was *invisible from inside the session*: `self._connections` entries are created only on a successful connect and `get_upstream_health` iterates them only, so a startup-failed server did not appear in `stm_proxy_health` at all — the one `logger.exception` at startup was the sole trace. A new `self._failed_servers` map records the connect-error summary in the startup `except`; `get_upstream_health` merges these in as `connected: False` / `tools: 0` with an `error` field (a live connection wins over a stale failed entry, and a successful reconnect pops the record), and `stm_proxy_health` renders the previously-dead `DISCONNECTED` branch plus a `startup connect failed: <error>` line. The retry/re-registration half of #580 is deliberately left open (there is no plumbing to re-register an upstream's tools with FastMCP after startup). **Behavior change**: `stm_proxy_health` now lists configured-but-unconnected upstreams as `DISCONNECTED` with their startup error, where previously they were absent entirely; no change for healthy servers.
- **Timeout-retry is gated on tool idempotency so a slow write isn't replayed** (#592, issue #578) — the upstream retry loop retries on `asyncio.TimeoutError` by reconnecting and *re-invoking the same tool* up to `max_retries` times, but a per-attempt timeout only cancels our `asyncio.wait_for` — the upstream request may already have committed its side effect — so for a non-idempotent tool the proxy manufactured duplicate writes the client never asked for (up to 4 applications with `max_retries=3`), a semantic change relative to the client calling the upstream directly. A new `_tool_idempotent_for_retry` gate now decides whether a *timed-out* call may be re-invoked: only `asyncio.TimeoutError` is gated (connection-level failures stay retryable for every tool, since they overwhelmingly mean the request never completed); idempotency is `readOnlyHint is True and destructiveHint is not True` under both `strict` and `conservative` policies (a conservative *retry* verdict would re-execute an unknown tool's side effect, so unknown means don't replay); an explicit per-tool/server `cache: true` override or `tool_annotation_policy: ignore` keeps the old replay-on-timeout behavior. When the gate says no, the loop records the TIMEOUT metric, refreshes the connection, and re-raises the original `TimeoutError` without re-invoking. **Behavior change**: under the default `conservative` policy, a tool without `readOnlyHint: true` no longer has its call *retried* on a per-attempt timeout — the connection is still refreshed and the timeout propagates; transport-error retries are unchanged.
- **Surfacing timeouts now count toward the circuit breaker** (#590, issue #579) — a *hung* (not erroring) LTM timed out on every surfacing call, but `SurfacingEngine.surface()`'s `TimeoutError` branch recorded only the `error_timeout` observability outcome and never called `record_failure()`, so the breaker never opened: every surfacing-eligible proxied call paid the full `timeout_seconds` (default 3.0s), and because the timeout cancels the adapter mid-RPC (setting `_needs_reconnect`), each call also tore down and respawned the LTM stdio child — while `stm_proxy_health` kept reporting the breaker `closed (healthy)`. The timeout branch now records a breaker failure too; the distinct `error_timeout` outcome and the `stm_surfacing_stats` split are unchanged. **Behavior change**: after `circuit_max_failures` (default 3) consecutive timeouts the breaker opens and surfacing is skipped for `circuit_reset_seconds` (default 60s) instead of taxing every call, and the `stm_proxy_health` breaker line flips to `open (failing)`.
- **A SELECTIVE/HYBRID pending-store write fault degrades to truncation instead of discarding the response** (#589, issue #577) — the PROGRESSIVE branch and the ratio-guard fallback tiers already guard their store-touching calls (the in-code comment names the stake: an escape "DISCARDS an otherwise-successful upstream response"), but the SELECTIVE/HYBRID primary `_apply_compression` call was bare and `SQLitePendingStore` handles no `sqlite3.Error` anywhere. Under the opt-in `pending_store: "sqlite"` backend, a write fault — a writer holding the lock past the 3s busy timeout, disk-full, a corrupt DB — raised out of the pipeline, was recorded as INTERNAL_ERROR, and threw away the already-fetched successful upstream response. The call is now wrapped in `sqlite3.Error` (scoped to the store fault; a compressor logic error still propagates) and degrades to a boundary-aware truncation at the retention budget — the ratio ladder's terminal tier — labeled `selective→truncate_on_store_error` in metrics and excluded from the response cache, so a transient store failure can't pin the lossy truncation for the cache TTL and suppress the chunk-TOC protocol after the store recovers (the #496 lesson). **Behavior change**: such a fault now returns a truncated response (one WARNING, not cached) instead of failing the tool call; no change on a healthy store or the default `pending_store: "memory"` backend.
- **`ProxyCache.get()` degrades to a MISS on a SQLite fault** (#588, issue #576) — the response cache was best-effort on the write side only (`_store_cache` wraps `set()`; degraded responses skip the cache per #496), but the lookup SELECT ran unguarded and both `ProxyManager` call sites are bare, so a runtime `sqlite3.Error` mid-session — disk I/O error, page-level corruption, an external process holding the file past the busy timeout — failed the proxied tool call with INTERNAL_ERROR even though the upstream was healthy and the cache is only an optimization; a persistently broken cache DB took down every cache-eligible call until restart. `get()` now logs a WARNING and returns a MISS so the call falls through to the upstream — the same degrade pattern its own privacy-eviction guard and `GraphConsultCache.get()` already use. **Behavior change**: a broken cache DB now costs cache hits, not tool calls; no change on a healthy DB.
- **Metrics/cache store init failure degrades to disabled instead of preventing startup** (#587, issue #575) — `metrics_store.initialize()` and `proxy_cache.initialize()` were the only two store inits in the lifespan not wrapped in the degrade-to-disabled pattern their siblings use, so a corrupt DB file (first PRAGMA raises `file is not a database`) or a concurrent-migration race (two `mms` sessions both reading the pre-migration schema after a column-adding upgrade; the loser's `ALTER TABLE` raises `duplicate column name`) crashed `app_lifespan` — **the MCP server never started and every proxied tool went down because an optional telemetry/cache DB was unhealthy.** Both inits now log a WARNING and fall back to `None` (`TokenTracker(metrics_store=None)` / `ProxyManager(cache=None)` already tolerate the degraded state), and the metrics migration tolerates the lost-race `duplicate column name` per-column while re-raising any other `OperationalError`. **Behavior change**: on a corrupt/locked metrics or cache DB, `mms` now starts with that feature disabled (one WARNING per feature) instead of failing to start; no change on a healthy DB.
- **Surfacing requests the structured `mem_search` format by default, restoring real relevance scores** (#572, issue #560) — every surfacing event since 2026-06-08 recorded scores of exactly `0.03` (zero variance across queries, tools, and weeks), leaving `min_score` and the auto-tuner with no signal to act on. The upstream LTM scores were fine; the flatness was a rendering artifact of the compact default: core's compact formatter rounds scores to two decimals, RRF fusion scores live in `(0, ~0.033]` (k=60 → max `2/61 ≈ 0.0328`), so the whole distribution collapsed to `{0.02, 0.03}` and the `min_score` filter (default `0.03`) passed only the `0.03` bucket. Notably the `0.03` default was calibrated in #329 against a raw-float score sweep, so under compact rounding the deployed filter never operated in the regime that calibration assumed — and rounding happened *before* the filter, so true scores down to `~0.025` passed a floor they were actually below. `result_format` now defaults to `"structured"` (four-decimal scores — ~330 distinct values over the RRF range instead of two — and real chunk ids end to end, so per-memory `helpful` boosts reach the underlying chunk instead of silently no-oping); the client auto-downgrades to compact when the core doesn't advertise `"structured"` in `capabilities.search_formats`, so older cores are unaffected. The fake test server now honors `output_format` like real core in both formats (it advertised structured while only emitting compact — latent while the default was compact), always emits `chunk_id` like core's structured formatter, and renders compact scores with core's two-decimal rounding so compact-path coverage reproduces the #560 failure mode instead of hiding it (codex review round 1). **Behavior change**: recorded surfacing scores become varied four-decimal values instead of a constant `0.03`; effective selectivity tightens slightly (true scores in `[~0.025, 0.03)` no longer round up past the `0.03` floor) — the regime the #329 calibration chose; memories surfaced under the default carry real LTM chunk ids.
- **Cache hits reconcile with every number rendered next to them in `stm_proxy_stats`** (#570, issue #558) — the #536 counters left three exclusions that made the stats output irreconcilable: a cache hit never entered `total_calls` (an operator reading "Total calls: 100 / Cache hits: 40" could not match the numbers to 140 actual invocations), the cache's benefit was structurally invisible in the compression-savings figure it sits next to (hits re-run no compression), and a tool whose responses the cache can never store — mixed or non-text-only content, empty responses, progressive-passthrough degradations, transient retrieval keys — re-missed forever, growing `cache_misses` unbounded with a permanent 0% hit-rate contribution that pointed diagnostics at a tool the cache cannot help. Rather than folding hits into `total_calls` (the denominator of the per-stage latency averages and the unit of the per-tool char counters — a hit runs none of that pipeline) the summary now exposes the reconciliation explicitly: `total_invocations = total_calls + cache_hits + total_errors` (the three counters are mutually exclusive per call — every pre-`record()` stage failure raises, and the hit path swallows surfacing errors), rendered as `Total calls: N live [+ K failed] + M cache-served = T invocations`; hits carry their served chars (`cache_hit_chars`, the honest figure available without a cache-schema migration) onto the hits line; and a new `cache_unstorable` counter counts each store refusal of a *counted* miss, gated on the exact lookup-path conditions so force-forwarded calls that recorded no miss count nothing — rendered (only when non-zero) as an `Unstorable` line plus an effective hit rate over storable lookups, so a never-cacheable-heavy workload no longer reads as a depressed hit rate. The `Savings` and `Errors` lines are labeled with their denominators (`compression of live calls` / `% of live attempts`). Converged through four codex review rounds (2→1→1 Major, then SHIP). **Behavior change**: an empty upstream response now records a 0/0 metrics row (in-memory and `proxy_metrics.db`), mirroring the non-text branch — previously it recorded nothing (the R8 characterization pin from the A1 refactor, amended deliberately); `get_summary()` gains `total_invocations` / `cache_hit_chars` / `cache_unstorable` (no existing key changes meaning) and the `stm_proxy_stats` lines change shape as described.
- **Upstream tool annotations are refreshed on `tools/list_changed`, and cache rows the change made unsafe are invalidated** (#568, issue #557) — the cache-eligibility gate (#535) reads `readOnlyHint` / `destructiveHint` from the per-connection tool snapshot, which was populated only at connect/reconnect; nothing in the proxy consumed `notifications/tools/list_changed`, so an upstream that re-declared a tool from read-only to may-mutate at runtime kept its connect-time snapshot indefinitely and a second identical call was served the pre-flip cached success **without re-executing the now-mutating side effect** — the exact replay #535 closed, reopened by staleness, self-healing only on an error-driven reconnect. Both upstream `ClientSession` constructions now wire a message handler that reacts to the notification by scheduling a coalesced background refresh (one in-flight `list_tools` per server; a notification landing mid-refresh triggers exactly one more pass; a refresh racing a reconnect is dropped by a session-identity guard): the tool snapshot is reassigned and cache rows are deleted for any tool whose eligibility verdict flipped eligible→ineligible under the current config, plus tools that disappeared from the advertised list (their rows could only mask the removal). Deletion on top of the gate is deliberate — a pre-flip row would otherwise outlive the flip and could serve again once the annotations or overrides move the verdict back. Comparing *verdicts* rather than raw annotations means an operator's explicit per-tool `cache: true` override keeps suppressing invalidation. The refresh bookkeeping is reset in `stop()` and the double-start guard (a drain task cancelled before its first step never runs its `finally`, which would otherwise silently drop every later notification for that server on manager reuse — caught by codex cross-review). **Behavior change**: the proxy issues an extra upstream `tools/list` request per (coalesced) `list_changed` notification, previously dropped by the SDK's default handler; cache rows for tools that flip toward may-mutate or vanish are deleted at refresh time instead of surviving until TTL expiry; and the per-connection tool snapshot (hence the advertised set on the next `tools/list`) now tracks runtime list changes instead of staying frozen until reconnect. Upstreams that change annotations *without* emitting `list_changed` still heal on the next reconnect, as before.
- **`ttl<=0` now invalidates a stale cached row on every response shape that bypasses the text store path** (#548, #550, issue #541) — the store-side `ttl<=0` self-heal in `ProxyCache.set` only ran on the TEXT store path, so a row left behind by an earlier text response for a `(server, tool, args)` key was never invalidated once caching was disabled and the same key returned a response that never reaches that store: a non-text-only response (early-returns before the Stage-5 store), a mixed text+non-text response (skipped by `_store_cache`'s text-only gate), a text-bearing error (raises before the store), an empty response, or a text response that skips the store for any other reason (cache-ineligible tool, progressive-passthrough degradation, transient retrieval key). While `ttl<=0` the lookup is bypassed so the stale row is never *served* during the disabled window, but raising the TTL back within the row's frozen window (per-row TTL is frozen at write time) made the now-eligible lookup serve the stale text — most likely on the `cache_ttl_seconds: 0` headline case (disabling caching for a volatile/binary tool whose response shape flips text→non-text). The delete-by-key is now factored into `ProxyCache.invalidate`; `_store_cache` resolves the TTL once and, when it is `<=0`, invalidates and returns above the whole store-skip chain, while the non-text / error / empty early-return paths invalidate inline — so every non-self-healing path drops a stale row when the resolved TTL is `<=0`. This preserves #536's zero-I/O posture for the steady-state text / no-row disabled-cache path; the only added I/O is one `DELETE` per such call under a disabled cache, bounded to calls that actually occur.

### Security

- **Credential-bearing connection-lifecycle cleanup logs are redacted on the reconnect / double-start / stop paths** (#606, issue #605) — the deferred sibling of #593. #593 closed a credential-leak class on the *startup* connect path (httpx transport exceptions embed the credentialed request URL, and `logger.debug(..., exc_info=True)` re-emits it in the traceback tail) via the single choke point `ProxyManager._redacted_error(exc, url)`. The same treatment now covers the sibling cleanup logs that close or reopen `AsyncExitStack`s wrapping transports opened with the same credentialed `cfg.url` — the `start()` double-start guard's per-connection stack close, `stop()`'s "Failed to close connection stack", both `_reconnect_server` cleanup logs, and the three `_fetch_upstream` post-deadline / post-error reconnect messages (which also gain the previously-absent server name). Each now renders the exception via `_redacted_error(exc, url)` (URL userinfo scrubbed, then capped) and drops `exc_info=True`. Sites with no credentialed URL in the exception path (tool-graph / store / telemetry closes, the empty `self._stack.aclose()`) deliberately keep `exc_info` for non-credential debugging. **Behavior change**: these DEBUG logs lose the `exc_info` traceback (the leak vector) in exchange for the redacted exception text; every credential-free `exc_info` debug path is untouched.
- **Quoted-JSON credential labels are caught beyond the AWS spellings** (#565, issue #562) — #553 added quoted-key detection for the two AWS secret-material labels only; the identical gap remained for the rest of the label vocabulary. The generic label rules end in `\s*[:=]`, and a quoted JSON key's closing quote sits between the label and the colon, so `"password": "hunter2"`, `"api_key": "sk-…"`, camelCase `"accessToken": "ya29.…"`, and dict-repr `{'password': 'hunter2'}` matched **zero** patterns — and JSON-serialized credentials are high-frequency tool output (`docker inspect`, `kubectl get secret -o json`, DB connection configs, OAuth token responses). One general quoted-label rule reuses #553's FP-guard shape — quote directly on both sides of the label, value must open as a string — so JSON-Schema object values (`"access_token": {"type"…` — login/OAuth tool schemas carry these constantly), embedded labels (`"my_api_key_name": …`), and prefixed keys (`"tools.api_key": …`) never fire; the optional `[_-]?` separator lets camelCase keys match under `(?i)`. `pwd` is deliberately excluded from the quoted vocabulary — shell/file tools legitimately return `"pwd": "/home/user"` working-directory fields. **Behavior change**: responses carrying quoted credential labels now route away from external-LLM compression strategies and are excluded from the response cache, and tool metadata carrying them is withheld from `tools/list` under the strict exposure profile — the same fail-safe treatment the unquoted spellings already get.
- **The `x-amz-security-token` wire label is caught (header + presigned-URL query param)** (#564, issue #561) — #553 catches AWS secret material by config/JSON label, but the label AWS actually puts on the **wire** for the same session-token material was covered by no pattern: the `x-amz-security-token` request header (emitted verbatim in botocore DEBUG logs and HTTP-header dumps) and the presigned-URL `X-Amz-Security-Token=…` query parameter (present in every presigned S3 URL generated with temporary credentials). `session[_-]?token` cannot cross the `security-token` spelling and the kebab shape has no `aws` separator before the label, so detection relied on an `ASIA…` key ID co-occurring in the same text. One new rule mirroring #553's two-alternative shape: a quoted form (serialized header dicts; string-opening value, so OpenAPI/JSON-Schema header *definitions* with object values never fire) and an unquoted form (raw header line, presigned-URL query param) whose left boundary rejects only a directly preceding **separator** (`(?<![_.\-])`) — kebab/dotted compounds that merely name the header (`forward-x-amz-security-token: true` config knobs, `proxy.headers.x-amz-security-token` telemetry heads) stay negative, while bytes-repr wire dumps, which render the newline before the header line as a literal `\r\n` and so put an alphanumeric directly before the label (`send: b'…\r\nx-amz-security-token: FwoG…'` — `http.client` debuglevel), stay positive. The boundary class was adjudicated against a codex cross-review finding (full details in the #564 review comment). **Behavior change**: responses carrying this label now route away from external-LLM compression strategies and are excluded from the response cache, and tool metadata carrying it is withheld from `tools/list` under the strict exposure profile. *(Sync note: #564 + #565 move `CREDENTIAL_PATTERNS` 17 → 19; the LTM forward-sync carrying both rules is memtomem#1541, restoring byte-identical same-order equality at 19 — after which the #559 content-hash pin can land.)*
- **AWS secret material is caught by label (`SECRET_ACCESS_KEY` / `SESSION_TOKEN`)** (#553) — the credential scan caught AWS key **IDs** (`AKIA`/`ASIA` prefixes) but not the secret **material** those IDs unlock: `secret[_-]?key` needs its two words adjacent (`secret_access_key` splits them) and `access[_-]?token` needs the literal `access` (`session_token` has neither), so a tool response echoing an `env | grep AWS` block or an STS AssumeRole JSON scanned clean and could be routed to an external-LLM compressor or stored in the response cache. One new `CREDENTIAL_PATTERNS` rule with two alternatives: a **quoted-key form** (`"SessionToken": "…"` — STS JSON, python-dict repr, kebab-case serialized headers; the quote must sit directly on both sides of the label and the value must open as a string, so JSON-Schema properties (`"session_token": {"type"…`) and tool-name-keyed telemetry dicts (`"aws.get_session_token": …`) never fire) and an **unquoted label form** (`AWS_SECRET_ACCESS_KEY=`, `aws_session_token = ` — env output, `~/.aws/credentials` INI, TOML dotted keys, namespaced `TF_VAR_aws_secret_access_key=`) carrying a left boundary (token start or a directly preceding `aws` separator) so identifiers that merely *embed* the label — `get_session_token: unhealthy`, `supports_session_token: true`, `rotateSecretAccessKey: done` — don't mask benign tools or degrade routing. STM-origin: `CREDENTIAL_PATTERNS` moves to 17 while LTM's mirrored set stays at 16 until the forward-sync into memtomem LTM's `privacy.py` lands (the inverse of the `#1488`→`#1491` reverse-sync direction; a content-hash pin over the shared subset is tracked in #559). **Behavior change**: responses carrying these labels now route away from external-LLM compression strategies and are excluded from the response cache — the same fail-safe treatment every other credential label already gets — and tool metadata carrying them is withheld from `tools/list` under the strict exposure profile.
- **Compression-routing credential scan mirrors LTM's seven provider-token patterns** (#549) — STM's `CREDENTIAL_PATTERNS` (the compression-ROUTING signal that steers a credential-bearing payload away from external-LLM strategies) was blind to seven secret formats that memtomem LTM blocks at its write-rejection boundary: modern OpenAI keys (`sk-proj`/`svcacct`/`admin`, `T3BlbkFJ`-anchored), Anthropic (`sk-ant-NN-…AA`), the `gh[ousr]_` GitHub token family, Google `AIza`, GitLab `glpat-`, Hugging Face `hf_`, and PyPI macaroons. A response carrying one of these could be routed to an external-LLM compressor here even though LTM refuses to store it. The seven are now mirrored back (reverse sync) so STM's `CREDENTIAL_PATTERNS` equals LTM's `DEFAULT_PATTERNS` exactly (16 secret-class patterns); the regex strings are byte-identical and case-sensitive on the fixed-case provider prefixes (`sk-`, `AIza`, `glpat-`, `hf_`, `gh*_`) to keep `re.search` linear-time and the false-positive profile tight. PII (email) is deliberately NOT crossed over — it stays in STM's storage-gating `PII_PATTERNS` only — per the asymmetric-sync rule now spelled out in the `privacy.py` module docstring. The source issues `#1488` (LTM-side origin) / `#1491` (reverse-sync tracking) refer to the memtomem LTM repo (`github.com/memtomem/memtomem`), not this one. No proxy runtime or config surface changes. *(Set-equality note: #553 above adds a 17th, STM-origin pattern, so the byte-identical-equality claim holds for the 16 mirrored patterns only until the LTM forward-sync restores full equality — integrity of the shared subset is tracked by the #559 content-hash pin.)*

## [0.1.30] — 2026-06-30

A security-hardening release. Three coordinated fixes close an ungated
project-local config adoption path during import / host-sync, `env` / `headers`
leakage in `--json` output, and a non-loopback bind of the surfacing daemon.
Each ships behind an explicit opt-in so an existing trusted workflow can
re-permit the prior behavior, and none changes the proxy runtime.

These fixes were developed and merged through GitHub's private security-fork
flow, so they carry no public PR number; citations below are the GitHub
Security Advisory IDs (`GHSA-…`).

### Security

- **Project-local MCP configs are gated before registry adoption during import / host-sync** (GHSA-9hjq-vxq2-36px) — `mms import --apply` and `mms host sync --apply` fed `discover("all", cwd)` into the global registry, which reads project-local config files under the current directory (`<cwd>/.mcp.json`, `<cwd>/.cursor/mcp.json`). An untrusted repository checkout can ship those files, so adopting them without acknowledgement let a checkout register a command that later runs with the user's privileges — and `mms host sync`'s confirmation gate covered only the REMOVE/RESTAMP buckets, leaving ADD entirely ungated. A new `is_repo_local` marker on `ImportCandidate` is set only at the two scanner sites that read a file under cwd; both registry-writing buckets now fail closed before any write when they adopt a repo-local candidate — ADD (`new`) in both commands, and (under `--force`) RESTAMP in `mms host sync` — unless the new `--allow-project-configs` flag is passed. The marker keys on the source *file location*, not the source label: `Claude Code (project)` entries live in the user's home `~/.claude.json` and a repo checkout cannot ship them, so they stay ungated; sidecar BACKFILL stays ungated (it records a drift hash, not a registry command); `mms init` / `mms add --from-clients` are unaffected (explicit selection flow). **Behavior change**: `mms import --apply` / `mms host sync --apply` now abort before writing when a candidate originates from a project-local file, unless `--allow-project-configs` is passed.
- **`mms status --json` / `mms list --json` mask `env` and `headers` values** (GHSA-fpm7-rf53-vjc9) — `_redacted_servers_json` stripped the imported `origin.original` block (#476) but passed each server entry's own active `env` and `headers` straight through to `--json`. That machine-readable output is routinely piped to scripts, CI logs, issue comments, and agent transcripts, so a token stored in `env` / `headers` could leak verbatim. Every `env` / `headers` value is now masked (keys preserved) so the JSON surface is log-safe; all values are redacted rather than only classifier-flagged ones, to catch `Cookie`-style headers and short/punctuated secrets a key/value heuristic would miss. Because `--json` reads the raw config (not a validated `UpstreamServerConfig`), a hand-edited / corrupted non-dict `env` / `headers` is replaced wholesale with the sentinel rather than emitted verbatim. Redaction is output-only — the function returns shallow copies and the on-disk config is unchanged. **Behavior change**: `--json` no longer emits `env` / `headers` values (the keys remain).
- **Surfacing daemon rejects a non-loopback `host` bind** (GHSA-wf6f-mgj9-wf8r) — `DaemonConfig.host` had no validation, so `MEMTOMEM_STM_DAEMON__HOST=0.0.0.0` (or an empty string, which `asyncio.start_server` treats as all-interfaces) would bind the surfacing daemon to a non-loopback interface with no pushback. The daemon is local-only and authenticated by a per-start random token, not network ACLs, so a non-loopback bind widens the attack surface from local processes to network-adjacent clients. A model validator now rejects a non-loopback `host` unless the new `daemon.allow_non_loopback=true` opt-in is set; loopback is detected via `ipaddress.ip_address(host).is_loopback` plus a literal accept for the hostname `localhost`, so the whole `127.0.0.0/8` range and IPv6 `::1` forms stay valid while `host=""` and `0.0.0.0` are treated as non-loopback. `SECURITY.md` is updated accordingly. **Behavior change**: a config setting `daemon.host` to a non-loopback address now fails at load unless `daemon.allow_non_loopback=true`.

## [0.1.29] — 2026-06-24

An import-reversibility and external tool-graph eligibility release. The `mms eject` round-trip makes STM's upstream import reversible: imported upstreams now capture their host-config provenance at import time (and redact it from `--json`), every prune is appended to an append-only backup log before the host original is deleted, all `stm_proxy.json` mutators serialize under a single cross-process write lock, and the new `mms eject` command restores an imported server's verbatim entry to its host MCP client config — verifying the restore before it removes the STM entry — with `mms list` / `mms remove` surfacing the new provenance. A second, opt-in feature wires an optional external tool-graph eligibility provider into the exposure path: cross-server authorization/data-flow facts can withhold tools at `tools/list` time and sink eligible-but-risky tools in the ranking telemetry, with the once-per-start consult disk-cached behind a live-`graph_generation` probe that never masks a degraded graph. Two observability additions round it out — `stm_selection_stats` surfaces the selection-telemetry log and `stm_progressive_stats` now reports primary-store degradation — alongside fixes that surface escaped background-task exceptions and degrade a failed primary progressive store to an uncached passthrough. This release also completes Track B's multi-host hook support: `mms hook install` / `uninstall` register STM's PostToolUse hook into each host's own config (Claude / Codex / Cursor / Kimi) and `mms hook --host` routes the runtime payload through the matching per-host adapter, so memory surfacing reaches Codex CLI, Cursor, and Kimi Code — surfacing-only, since native output replacement ports to no non-Claude host. Supporting changes move the hook↔daemon wire onto a canonical tool vocabulary at protocol v2 and add `mms daemon stop --all` to reap daemons orphaned under a stale fingerprint.

Citations are the merged PR numbers; an `issue #N` reference is added for source-issue traceability where one exists.

### Added

- **`mms hook install` / `uninstall` — per-host PostToolUse hook registration** (#525) — new `mms hook install --host <claude|codex|cursor|kimi>` writes STM's PostToolUse hook block into the host's own config so the host fires `mms hook --host <name>` after each built-in tool call: `~/.claude/settings.json` + `~/.cursor/hooks.json` (JSON), `~/.kimi/config.toml` + `~/.codex/config.toml` (TOML, via the existing `tomli-w` dep; Codex's nested `[[hooks.PostToolUse]]` schema pinned to the official `developers.openai.com/codex` sample). The tool matcher is derived from each adapter's `native_tool_map` ∩ the read-like surface allowlist (Claude `Read|Grep|Glob|Bash`, Kimi `Shell|ReadFile`, Codex `^Bash$`; Cursor's post-tool hook is matcher-less), so a registered matcher can never advertise a tool the live hook ignores. The merge is **idempotent** — an existing STM block, recognized by command shape (global `memtomem-stm hook …` or `uv run … memtomem-stm hook …`, any `--host`/entry point), is updated in place, never duplicated — and `uninstall` is **symmetric**: it removes exactly what install adds, prunes the emptied containers, and leaves hand-written hooks untouched. Both default to a **dry-run preview**; `--apply` writes after backing up any prior file to `<path>.bak`, and a config that exists but does not parse is refused (never clobbered). **TOML hosts are re-serialized**, so comments/formatting in `config.toml` are not preserved — the preview states this and the `.bak` is the safety net (JSON has no comments). The `mms hook` command becomes a group: bare `mms hook [--host …]` is unchanged (the runtime PostToolUse bridge — it still reads stdin and exits 0), with `install`/`uninstall` as subcommands. Surfacing-only on every host (B0: native output *replacement* ports to no non-Claude host); post-install notes flag each host's runtime caveat (Codex's `/hooks` trust step; Cursor's documented-but-runtime-no-op `additional_context`; Kimi's unverified exit-0 stdout inject).
- **`mms hook --host <name>` selects the per-host adapter; non-Claude surfacing goes live** (#524) — wires the dormant Codex/Cursor/Kimi adapters (#520/#522/#523) into the live `mms hook` path: `--host` (`claude`/`codex`/`cursor`/`kimi`) routes the PostToolUse payload through the matching adapter's parse → render → serialize chain, so STM's memory surfacing now reaches Codex CLI, Cursor, and Kimi Code — not only Claude Code. Surfacing-only on the non-Claude hosts (B0: native output *replacement*/compression ports to no other host); Kimi receives the surfaced block as raw stdout (no JSON envelope), Codex/Cursor as their respective JSON shapes. `--host` defaults to `auto`, which infers the host from the payload shape (`detect_host`) and falls back to Claude — so the original `mms hook` Claude registration keeps working unchanged. **Auto-detect cannot distinguish Codex from Claude** (their payloads are byte-identical) and cannot identify Kimi from a malformed payload, so per-host registration should pass an explicit `--host`. The hook still always exits 0 and passes the tool output through on any problem (an unrecognized runtime `--host` now falls back to auto-detect rather than erroring — see #526).
- **`mms daemon stop --all` + `status` orphan reporting** (#518, issue #517) — daemons left under a stale config fingerprint are now visible and reapable. A fingerprint changes on any fingerprinted config field or a `PROTOCOL_VERSION` bump (folded into the fingerprint since #516, so every upgrade across one counts); the old daemon then lives under the *old* fingerprint, where `mms daemon stop`/`status` (keyed to the *current* fingerprint) never look. `mms daemon status` now reports any live daemon under a different fingerprint (a text warning + a `foreign` array in `--json`, never the auth token); `mms daemon stop --all` SIGTERMs them. This matters most for a daemon pinned with `daemon.idle_timeout_seconds=0`, which never idle-shuts-down and previously had to be killed by hand. **Behavior change**: the default `mms daemon stop` scope is unchanged (current config only) and `restart` is unaffected; `--all` is opt-in because a live daemon under another config may be intentional. Foreign daemons are identified by recorded pid (a protocol-mismatched daemon can't be pinged), so a crash-orphaned handshake naming a recycled pid is a documented residual risk — the same one the pre-existing same-config SIGTERM fallback carries. POSIX-only: Windows has no reliable signal-0 liveness check, so foreign detection is disabled there (current-config behavior unchanged) until the cross-platform connect-probe in #519 lands.
- **Import provenance captured at import time** (#476, issue #475) — `mms init` and `mms add --import` now record an `origin` block on each imported `UpstreamServerConfig` (`schema_version`, per-source `kind`/`pruned`, `imported_at`, and the verbatim host entry under `original`). This is the data foundation for the new `mms eject` round-trip. **Behavior change**: `mms status --json` / `mms list --json` no longer dump the raw `original` host entry (which could leak `env`/`headers`); the serializer keeps the provenance summary and replaces `original` with a `has_origin` marker. The proxy runtime never reads the block; older binaries ignore it via pydantic `extra="ignore"`.
- **Backup-before-delete log and per-source pruned status for prune; cross-process config write lock** (#477, issue #475) — every prune path (`mms add --import --prune`, `mms init --prune-originals`, standalone `mms prune`) now appends the verbatim host entry to `~/.memtomem/pruned_upstreams.json` (`0600`, append-only) *before* deleting the host original; a failed append fails that source's prune and skips the delete. Successful prunes flip the matching `origin.source`/`origin.duplicates` row to `pruned: true` + `pruned_at`. **Behavior change**: all `stm_proxy.json` mutators (`add`, `init`, `remove`, `surfacing`, `prune`) now run under one `~/.memtomem/.stm_proxy.lock` cross-process write lock, so a concurrent mutator can no longer resurrect a removed entry by saving over a stale load.
- **`mms eject` — restore imported upstreams to their host config** (#478, issue #475) — new `mms eject NAME... [--to|--keep|--force|--allow-argv-secrets|--accept-schema-loss|--dry-run|--yes]` command restores an imported upstream's verbatim `origin.original` payload (captured in #476) back to its host MCP client config, verifies the restore by re-reading the backing host file, and only then removes the STM entry. This is the third opt-in writer to the otherwise read-only source-client contract. Writes via `claude mcp add-json` (Claude Code user/local) or atomic JSON edit (`.mcp.json` / Claude Desktop); a post-write verify mismatch keeps the STM entry by default (`--accept-schema-loss` opts out), secret-valued env/headers gate the argv shell-out (TTY confirm / `--allow-argv-secrets`, never bypassed by `--yes`), and any `add-json` failure is non-fatal per entry. STM entry removal runs under the #477 write lock; `--dry-run` skips it.
- **`mms remove` orphaning hint and `mms list` ORIGIN column** (#479, issue #475) — `mms remove` now prints an advisory note before its confirm prompt when the recorded provenance shows removal would leave the server registered nowhere (primary source pruned, no surviving un-pruned duplicate), pointing the operator at `mms eject NAME`; the removal is never blocked. `mms list` gains an `ORIGIN` column showing `origin.source.kind` with a `*` marking a pruned host original (legend rendered only when a starred row exists). `--json` output is unchanged. Reference docs for the whole import round-trip (`docs/cli.md` eject section, README reversibility note) land here.
- **Optional external tool-graph eligibility provider — config + adapter** (#491, issue #465) — new default-off `ToolgraphConfig` on `ProxyConfig.toolgraph` (stdio launch `command`/`args`/`env`, `agent_id`, `server_name_map`, `query_profile`, the four `on_*` failure knobs, `risk_penalty_scale`, `timeout_seconds`) plus a connectable `ToolgraphConsultAdapter` that consults an external authorization/data-flow graph over MCP for cross-server eligibility facts. The graph is consulted, never proxied, and STM holds no Python-level dependency on it. `on_unreachable`/`on_tool_not_found` default `open` (enhancement, not a hard dependency); `on_agent_not_found`/`on_protocol_error` default `fail_start`. Nothing is wired into the exposure filter in this PR; an enabled-but-inert block logs a startup WARNING.
- **Tool-graph eligibility provider wired into the exposure hard filter** (#492, issue #465) — when `toolgraph.enabled`, `ProxyManager` consults the provider once at startup and feeds its verdict into the `tools/list` exposure filter, replacing the inert warning from #491. Per-candidate rejects ride the profile-gated signal block under `toolgraph_*` reject codes (ranked above `unhealthy`, below `sensitive_metadata`; an unrecognized reason falls back to a fail-safe `toolgraph_rejected` — still withheld); a `closed` whole-call knob can withhold every tool. **Behavior change**: with the block enabled, tools the graph rejects are removed from the advertised set. A misconfigured provider resolving to `fail_start` raises out of `start()` before any tool is advertised; any failure resolving to `open` skips the external rules for the session and is surfaced loudly (startup WARNING + a `DEGRADED` line in `stm_proxy_health`). The consult populates the `graph_generation` field in the selection telemetry log.
- **Tool-graph `risk_score` consumed as relevance `risk_penalty`** (#495, issue #493) — the external graph's soft per-candidate `risk_score` (`[0,1]`, carried on eligible candidates via a second `rank_features` startup consult) is mapped onto the tool-relevance ranker's `risk_penalty` (`penalty = min(risk_score * risk_penalty_scale, 1.0)`), composed with the native review penalty via a complement-product, so eligible-but-risky tools sink in the ranking telemetry — never in exposure. A new `risk_penalty_source` (`none`/`review`/`graph`/`review+graph`) rides every ranked record, graph-penalized calls stamp the `v3-bm25-graph-risk-penalty` ranker cohort, and `stm_proxy_health` surfaces the per-session count. Replaces the inert `0.0` placeholder behind `toolgraph.risk_penalty_scale`.
- **Tool-graph startup consult is disk-cached** (#500, issue #494) — when `toolgraph.enabled`, the once-per-start eligibility consult is cached in a new SQLite store (`~/.memtomem/toolgraph_consult.db`, new `consult_cache_enabled` default `True` + `consult_cache_path` on `ToolgraphConfig`), so a restart skips the expensive `eligible_tools(refs)` + `rank_features(refs)` graph evaluation when the graph state is unchanged. Every start still runs a cheap `eligible_tools([])` probe to read the live `graph_generation`, so the cache can never mask a degraded/unreachable graph (the `on_*` knobs and `DEGRADED` health line stay loud) and only an agent-found success is written. Raw ref-level facts are cached and re-mapped downstream every start, so `on_tool_not_found` / `risk_penalty_scale` changes take effect on a hit; a ref-set or `server_name_map` change invalidates via the candidate hash.
- **`stm_selection_stats` observability tool — read-side surface for selection telemetry** (#484, issue #467) — surfaces the JSONL selection/execution log (added in #471) through a new opt-in observability MCP tool: live write-path counters plus a persisted aggregate (event counts, selections by ranker version, by server and tool, execution ok/error rate and latency p50/p95/p99, and the hard-filter reject-reason tally from #465). Makes accumulated telemetry inspectable ahead of offline replay/eval (#468). Advertised only when `MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=true`; reads the active log file (rotated backups are noted but not parsed).
- **`stm_progressive_stats` surfaces primary-store degradation** (#499) — adds a `Primary-store degradation (last 24h)` section reporting the count of `progressive→passthrough_on_error` events (the degradation introduced by #496) plus the top affected server/tool pairs, via a new `MetricsStore.get_progressive_degradations()`. Read from the metrics store independently of the reads tracker and shown even when reads tracking is disabled, so a failing backing store cannot go silent. Honors the existing `tool` filter.

### Changed

- **Runtime `mms hook --host <invalid>` now fails open instead of exiting 2** (#526, reversing #524) — the runtime PostToolUse bridge is fired *non-interactively by the host*, and every supported host treats a non-zero hook exit as a block/deny/correction (Claude exit 2 → stderr-to-model block; Cursor → permission deny; Kimi → stderr→LLM correction; Antigravity → deny). #524 made an unrecognized `--host` a `click.Choice` usage error (exit 2), which is correct at an operator's terminal but, on the runtime path, surfaces to the host as a blocked tool call — violating the always-exit-0 fail-open contract. The runtime `--host` is now a plain string with an **optional value** (`is_flag=False` + `flag_value="auto"`) validated in the command body: `auto` → `detect_host`, a known host → that adapter, an **unknown** value → log a warning and fall back to auto-detect (`detect_host`, which defaults to Claude). This closes *both* `--host` argv shapes that previously exited 2 before the body ran — an unrecognized value (`--host bogus`) and a bare `--host` with the value omitted — into the fail-open path, so no runtime `--host` ever yields a non-zero exit. The `install` / `uninstall` subcommands keep the strict `click.Choice(known_hosts())` and a required value — those are operator commands where an exit-2 usage error on a typo is the right feedback. Reachable only via a hand-edited host config or a future STM version renaming a host while an old registration still names it; both now degrade to a safe pass-through.
- **`mms hook` gates on a canonical tool vocabulary** (#515) — the built-in-tool surfacing/compression bridge no longer hard-codes Claude Code's PascalCase names. Each host adapter maps its native PostToolUse tool names into STM's canonical vocabulary `{read,grep,glob,shell,web_fetch,write,edit}`, and the surface allowlist (`read,grep,glob,shell`) + the shell-only compression gate key on the canonical name, so one set of gates works across hosts (groundwork for the Cursor/Kimi/Codex adapters). `mcp__`-prefixed tools are skipped (already proxied). Claude surfacing behavior is unchanged: the surfacing engine still sees the host-native tool name for query extraction. **Behavior change**: `MEMTOMEM_STM_HOOK_SURFACE_TOOLS` now documents *canonical* names (`read,grep,glob,shell`); it stays back-compatible — a legacy native value (`Read,Grep,Glob,Bash`) still resolves — and an unrecognized token is now logged and dropped instead of silently building an allowlist that matches nothing.
- **Hook↔daemon wire carries the canonical call; daemon protocol → v2** (#516) — the `mms hook` daemon link now transports a serialized `CanonicalHookCall` (`to_wire`/`from_wire`) instead of the host's raw PostToolUse payload, so the warm daemon needs no host knowledge (the per-host adapters plug in client-side). The `tool_response` object is no longer sent (compression runs in the hook process; the daemon reads only the flattened text). `PROTOCOL_VERSION` bumps 1 → 2 and is folded into the daemon's `config_fingerprint`, with an explicit per-frame version check on both ends. **Behavior change**: upgrading STM while a daemon is running no longer reuses the old daemon — it is wire-incompatible at a different fingerprint, so a fresh v2 daemon is auto-spawned on the next hook call (a brief transient double-daemon). Under the default idle timeout the old daemon shuts itself down shortly after; if you pinned it with `daemon.idle_timeout_seconds=0` it stays up and — being at the old fingerprint — is invisible to `mms daemon stop`/`status`; stop it with `mms daemon stop --all` (#518).
- **Selection-telemetry `execution.cache_hit` is now populated** (#485, issue #467) — the reserved field is set `true`/`false` per call (served from the proxy response cache vs a live upstream call; `null` when a raise escaped before it was attributable), and `stm_selection_stats` reports the cache hit/miss rate. **Behavior change**: when `selection_telemetry.enabled`, execution records now carry a real `cache_hit` value instead of `null` — additive, the schema key set is unchanged.

### Fixed

- **`mms hook install` / `uninstall` robustness — sibling clobber, command false-positive, single-slot `.bak`** (#530, issue #529, all in the unreleased #525 code) — three pre-existing gaps in the per-host hook-registration read-modify-write, surfaced by a `codex` pass on #528. (1) Re-installing no longer drops a non-STM handler a user co-located inside STM's matcher-group entry: install now scans all `PostToolUse` entries like `uninstall` — an STM-only group is refreshed wholesale (so a stale matcher updates), a shared group keeps the user's matcher + siblings and swaps only STM's handler in place, and a config that somehow holds two STM groups converges to a single handler (honoring "never duplicated" instead of leaving a duplicate that made a later re-install a misleading no-op). (2) STM's own hook is now recognized by an STM executable token *immediately followed by* a `hook` token (adjacency), not "a `hook` token anywhere **and** an STM executable anywhere", so a compound user command like `echo hook && memtomem-stm status` is no longer mistaken for STM's and overwritten/removed (both registered shapes — global `memtomem-stm hook …` and source `uv run … memtomem-stm hook …` — still match). (3) The `--apply` backup is now non-clobbering (`<path>.bak`, else `.bak.1`, `.bak.2`, …): a second apply (uninstall / re-install) previously overwrote the first backup, which for a TOML host is the only comment-preserving copy of the original (re-serialization drops comments) — destroying exactly the safety net. The original now always stays at `.bak`.
- **Escaped background auto-index/extraction exceptions are now surfaced** (#498) — background auto-index and extraction tasks were scheduled with a done-callback that only discarded the finished task, so an exception raised outside the coroutines' inner handlers (mkdir / atomic-write / genuinely unexpected errors) surfaced only as Python's non-deterministic `Task exception was never retrieved` warning at garbage-collection time. A new `_on_background_task_done` callback now retrieves such exceptions and logs a structured WARNING with traceback and `stage`/`server`/`tool` context (cancellation during `stop()` is treated as expected). Observability is logging-only by design.
- **Primary progressive store failures degrade to an uncached passthrough** (#496) — when the primary `PROGRESSIVE` path cannot build or store its first chunk, the proxy now returns the full cleaned upstream response instead of discarding an otherwise successful tool result as an internal error. The degraded response is recorded with a `progressive→passthrough_on_error` strategy label and is **not** cached, so a later identical call re-runs the pipeline and can retry progressive delivery once the pending store recovers. Transient progressive-store failures are uncommon, but when they occur the agent now keeps the full result instead of seeing an error.

## [0.1.28] — 2026-06-11

A security-hardening, correctness, and tool-selection release. A targeted audit across four issue classes — SQLite file permissions, sensitive-value caching, secret-content persistence, and extraction-call privacy — produced six security fixes. Thirteen correctness fixes address critical paths found in the implementation review (PROGRESSIVE metrics assignment, network LTM reconnects, shutdown ExceptionGroup handling) and the remaining medium and low backlog items across daemon, surfacing, compression, config, and CLI layers. Two new opt-in features instrument the tool-selection path (JSONL telemetry sink and BM25 relevance scoring), and a third hardens what the proxy advertises to MCP clients via an STM-native eligibility filter at exposure time.

Citations are the merged PR numbers; an `issue #N` reference is added for source-issue traceability where one exists.

### Added

- **Opt-in selection/execution telemetry JSONL sink** (#471, issue #467) — `SelectionTelemetry` writes per-call tool-selection events (eligible pool, scored ranking, selected set) to a configurable JSONL file when `selection_telemetry.enabled=true` (default `false`). **Behavior change**: when enabled, a JSONL file is written on every proxied tool call.
- **Deterministic BM25 tool-relevance ranking in selection telemetry** (#472, issue #466) — adds a BM25-based relevance score for each tool in the eligible pool to the telemetry record, enabling offline analysis of tool-selection quality. Inert when `selection_telemetry.enabled=false`.
- **STM-native tool-eligibility hard filter at exposure time** (#473, issue #465) — the proxy filters the advertised tool list at `tools/list` time using three eligibility axes: sustained connection failures, credential-bearing metadata, and duplicate-name conflicts within the same group. **Behavior change**: tools failing any eligibility axis are removed from the advertised set without per-call errors; `stm_proxy_health` now reports "N discovered, M advertised" to distinguish catalog size from what clients see; the connect-time skip is removed so `conn.tools` reflects the full upstream catalog.

### Fixed

- **PROGRESSIVE strategy branch no longer crashes every call** (#426) — `metrics_strategy` was unbound on the `PROGRESSIVE` branch, raising `UnboundLocalError` on every progressive tool call.
- **Network LTM transport reconnects after httpx errors** (#427) — httpx `HTTPStatusError` and `RequestError` were misclassified as internal errors; reclassifying them as transport errors lets the LTM reconnect logic kick in.
- **Shutdown no longer propagates ExceptionGroup-wrapped cancel-scope errors** (#428) — `ExceptionGroup`-wrapped AnyIO cancel-scope errors during server shutdown are now recognized and swallowed, matching the non-wrapped handling from #410.
- **`no_results_demoted` survives the surfacing stats healthy/fault split** (#429) — the skip reason was not assigned to either the healthy or fault bucket, causing it to vanish from `stm_surfacing_stats` after the #366 split.
- **Non-dict `mcpServers` in host configs no longer crashes `mms import`** (#430) — a host config file with `"mcpServers": null` or a non-dict value now yields zero candidates instead of a `TypeError`.
- **`mms health --timeout` is now honored end-to-end** (#431) — the upstream MCP probe ran without a deadline; it now shares the `--timeout` budget so `mms health` reliably exits within the requested window.
- **JSON config file loads when `MEMTOMEM_STM_PROXY__ENABLED` env var is set** (#432) — the env-var-only branch skipped `load_from_file`, so `consumer_model` and other file-only fields were silently missing; the file path is now always read first and env vars overlay it.
- **Durable feedback demotion re-applied on cache-hit path** (#433) — the in-memory demotion filter ran before the cache was consulted, so a cached response bypassed feedback demotion entirely; the filter now runs after cache retrieval.
- **LLM summary over-length responses capped to `max_chars`** (#434) — the LLM compression path returned summaries longer than `max_chars` when the model response exceeded the budget.
- **`stm_proxy_read_more` continues with the originating tool's `chunk_size` and hint preference** (#435) — `read_more` used the proxy-global defaults instead of those stored with the progressive key, causing inconsistent chunk sizing on multi-step reads.
- **Persisted AutoTuner adjustments clamped to the current band on load** (#436) — adjustments written under a previous band configuration could land outside the current band limits, breaking the per-tool adjustment contract.
- **`mms daemon stop/status` degrades on corrupted handshake fields** (#437) — malformed or missing fields in the daemon socket response raised an unhandled exception; the ops CLI now logs a warning and returns a degraded status.
- **Leaked LTM child process swept up when adapter teardown unwinds cross-task** (#438) — if the LTM child was spawned by one task and the cancel scope unwound in a sibling task, the child was never terminated; `_stop_adapter()` now guards against this.
- **Extractor nulled on stop; `_context_query` sanitized on cache miss** (#439) — the `ContextExtractor` was retained after `ProxyManager.stop()`, holding a live embedding session; `_context_query` could carry stale content into the next unrelated call on the cache-miss path.
- **`InMemoryPendingStore` eviction order kept in sync across `put`/`touch`/`delete`** (#440) — the LRU eviction heap could diverge from the store map after `touch` or `delete`, silently evicting the wrong entry.
- **URL userinfo redacted from LTM connection log lines** (#441) — the LTM MCP command or URL was logged verbatim; credentials embedded in a network transport URL were visible in INFO logs.
- **TOC budget forwarded, scorer length guarded, all-empty BM25 sections handled** (#442) — three compression edge cases: the SELECTIVE/Hybrid TOC budget was not propagated to the inner compressor; the BM25 scorer crashed on zero-length section text; an all-empty-body sections list produced a silent empty ranking.
- **`mms hook` skips compression below the sentinel budget and offloads spawn** (#443) — compression was attempted even when the result was at or below the no-op sentinel; daemon spawn was synchronous and could block the hook timeout path.
- **Daemon `start` respawns capped, response drain bounded, sub-second idle timeouts honored** (#444) — unbounded respawn loops on repeated start failures could spin indefinitely; the response drain had no timeout; `idle_timeout_seconds < 1` was silently rounded to zero (immediate exit).
- **`mms proxy` CLI config-file handling aligned with server startup; atomic `.mcp.json` writes preserve file mode** (#445) — `--config` was not applied before the config file was loaded in some subcommands. **Behavior change**: atomic `.mcp.json` writes now preserve the original file mode.
- **Unknown names in `mms project enable` rejected; ambiguous `project show` refused** (#446) — `project enable <name>` silently no-oped on an unregistered name; `project show` with a name matching multiple entries returned the first match. **Behavior change**: `mms project enable <unknown>` now exits non-zero.
- **Startup warns when surfacing is enabled but the proxy is disabled** (#447) — the combination was silent; the proxy now logs a WARNING so operators know surfacing events will never fire.
- **Proxy degrades on third-party API drift in tool registration and tracing** (#448) — unexpected `None` values from an upstream's `tools/list` or tracing calls now degrade gracefully instead of raising `AttributeError`.
- **`_take`'s unreachable container machinery removed; truncate fallback unified** (#449) — dead code paths in the compression `_take` helper and a duplicated truncate fallback were removed.
- **Freed budget refilled after `_json_key_truncate` drops keys** (#450) — keys dropped by the key-truncation pass freed space that was not credited back to the compressor budget, causing under-utilization on the subsequent fill pass.
- **Injection scan bounded honestly across full payload** (#451) — the comment claimed a "20 k char cap" but the scan ran on the full injection string; it now scans the first 20 k chars plus a head+tail window on longer inputs.
- **Hybrid ordering validated; zero model budgets rescued; env overrides named in load warnings** (#452) — `ProxyConfig` now rejects invalid hybrid ordering at load time; a zero `model_budget` is reset to the default instead of crashing; env-override load warnings name the environment variable. **Behavior change**: configs with invalid hybrid ordering are rejected at startup.
- **Concurrent `sync --apply` / `import --apply` serialized behind a cross-process write lock** (#457) — simultaneous invocations raced on the registry and sidecar; a 10-second lock acquisition timeout now serializes them, returning an error to the second caller if the lock is not acquired in time. **Behavior change**: a concurrent `--apply` fails with a lock-timeout error after 10 seconds instead of silently corrupting state.

### Security

- **SQLite store files set to mode `0600` at creation** (#458, issue #453) — the feedback, progressive, and pending SQLite files were created with the process umask (typically `0644`), making them world-readable. All three stores now `os.chmod` to `0o600` on first open.
- **Secret-looking responses never persisted to the response cache** (#460, issue #455) — the privacy scan ran on the raw upstream response but the cache stored the result regardless; a response flagged as credential-bearing now bypasses the cache write, and existing rows carrying the detection marker are purged once at `ProxyCache.initialize()`.
- **LLM routing gated on credentials only — emails are PII, not secrets** (#461, issue #456) — the privacy router sent any email-address-bearing response through the LLM path, which could persist a sanitized form; emails are now treated as PII (a logging concern) rather than secrets (a routing concern), so they no longer trigger the LLM fallback. **Behavior change**: email-only responses are no longer routed through the LLM compression path.
- **Auto-index and extraction never persist secret-looking content** (#462, issue #454) — the extraction LLM call was gated on privacy, but the raw candidate text was written to the pending store before the call; the store write is now skipped for credential-bearing candidates, and auto-index applies the same guard.
- **Credential scan runs before the extraction LLM call** (#463) — the privacy scan order was inverted: text was sent to the LLM and the scan ran on the result; the scan now runs first and short-circuits to skip the LLM on a hit.
- **Memory-ops filenames slugged; private directory and file modes enforced** (#464, issue #453) — persisted memory filenames now use a safe slug derived from server/tool names instead of raw strings, preventing path-traversal via crafted tool names; the memory-ops directory is created at `0o700` and files at `0o600`.

### Internal

- Decline policy for third-party automated promotional and badge PRs added to `CONTRIBUTING.md` (#424).
- Reference guides for `mms stats`, `mms status`, `mms remove`, and new config parameters updated and modernized (#425).
- The sync-SQLite-on-event-loop invariant documented inline at every store write path (#459).

## [0.1.27] — 2026-06-04

A small feature release: proactive surfacing can now be scoped per upstream.
`UpstreamServerConfig.surfacing_enabled` — toggled with `mms surfacing <server>
[on|off]` and shown in `mms status` — opts an upstream's tool responses in or out
of the SURFACE stage from the shared `stm_proxy.json`, so every MCP client
proxying through the same `mms` sees one consistent scope. Disabling skips
surfacing (and the LTM search) for that upstream before the engine runs.

### Added

- **Per-upstream surfacing toggle** — `UpstreamServerConfig` gains a `surfacing_enabled` flag (default `true`), and `mms surfacing <server> [on|off]` toggles it in `stm_proxy.json` (`mms status` renders it per server). Disabling an upstream short-circuits proactive surfacing for every tool on that server *before* the LTM search runs. It is enforced in `ProxyManager`, which reads the hot-reloaded proxy config — not the `SurfacingEngine` relevance gate, which is built once at startup from the top-level `SurfacingConfig` and never sees per-upstream config — and is counted as a new healthy `upstream_disabled` reason in `stm_surfacing_stats`. Because the flag lives in the shared proxy config rather than per-client `env`, every MCP client proxying through the same `mms` sees one consistent scope, unlike the existing `MEMTOMEM_STM_SURFACING__EXCLUDE_TOOLS` glob (which also matches `server__tool` but must be carried in each client's env). `docs/surfacing.md` now documents both the per-upstream toggle and the server-qualified `exclude_tools` glob, and corrects the circuit-breaker ordering in the surfacing sequence diagram.

## [0.1.26] — 2026-06-03

A correctness and surfacing-quality maintenance release completing the
proxy/surfacing review-fixes track (#412–#417): the proxy cache no longer
serves a response whose progressive/selective retrieval key has already
expired, surfaced memories expose a per-memory id so an agent can rate or
invalidate them individually, and short tool-call queries now surface instead
of silently dropping. Also drops a dead `min_response_chars` key from the
`mms init --lang ko` preset.

### Changed

- **Surfaced memories expose a per-memory `memory_id` for feedback** (EN-2/3) — each injected bullet now renders its `chunk.id` as a backticked token, and the feedback preamble adds a batched `stm_surfacing_feedback(ratings=[{"memory_id": ..., "rating": ...}])` example alongside the single-call one, so an agent can rate (and invalidate) individual memories rather than the whole surfacing event. **Behavior change**: the injected bullet format gains a `` `id` `` segment between the namespace badge and the `[bucket]` label; parsers keying on the exact `**source**{ns} [bucket]:` shape must account for it (the preview still follows `[bucket]: `). Injection-size truncation now pins the whole feedback preamble and drops body bullets on whole-line boundaries so a `memory_id` token is never severed mid-string. Under the default `result_format="compact"` the rendered id is a content-derived surrogate (`sha256(content)[:16]`), which drives STM-side invalidation but not the LTM `increment_access` boost; set `result_format="structured"` for the real `chunk_id` end to end.

### Fixed

- **Proxy cache no longer serves a response whose retrieval key has expired** (#412) — a progressive first-chunk and a SELECTIVE/HYBRID table-of-contents each mint a key into a process-local pending store, but the proxy cached that pre-surfacing `compressed` payload under the cache TTL (3600s) — longer than the pending store's (progressive 1800s, selective 300s) and unable to survive a restart. A later cache hit then returned a TOC / first-chunk whose `stm_proxy_read_more` / `stm_proxy_select_chunks` key was already dead, losing the response tail with no recovery. The cache now skips storing any compressed response carrying a transient-key marker (`PROGRESSIVE_FOOTER_TOKEN` or a `selection_key` JSON field) and purges such legacy rows once in `ProxyCache.initialize()`; the next identical call re-runs the pipeline and mints a fresh, live key. A false positive only costs one un-cached response (re-fetched on the next call), never correctness; progressive passthroughs and truncate fallbacks mint no key, carry no marker, and stay cacheable.
- **`mms init --lang ko` no longer writes a dead `min_response_chars` key** — the KO token-aware preset emitted a top-level `min_response_chars: 230`, but `ProxyConfig` has no such field (it lives only on the nested `ExtractionConfig`, default `500`), so pydantic's default `extra="ignore"` silently dropped it on load — an advertised budget that never applied. Removed it from the preset, the `--lang` help text, and `docs/configuration.md`; the other KO fields (`chars_per_token`, `default_max_result_chars`, per-server `max_result_tokens`) were and remain effective. The `--lang ko` CLI test now round-trips the written config through `ProxyConfig.load_from_file` and asserts the *effective* fields, so a future silently-dropped preset key fails loudly instead of passing a raw-JSON-only check.
- **Short tool-call queries surface instead of silently dropping** (KR-4.2) — `ContextExtractor` appended the tool name to clear `min_query_tokens` only when argument extraction yielded *nothing*. A short-but-non-empty extraction — e.g. `read_file {"path": "/etc/hosts"}` tokenizing to `"etc hosts"` (2 tokens, below the default floor of 3) — produced no query, so surfacing never fired for it. The fallback now also fires when the extracted tokens alone fall below `min_query_tokens`, appending the tool-name token(s) after the deterministic argument scan. Queries that already clear the floor are untouched, and a query still too short even with the tool name (e.g. a one-token tool) is still rejected. Trade-off: a marginal query now consumes a surfacing rate-limit / cooldown slot.

## [0.1.25] — 2026-06-02

A small maintenance release: the standalone `mms` MCP stdio server now exits
cleanly when the client closes the connection, instead of surfacing a benign
AnyIO teardown error as an unhandled exception.

### Fixed

- **`mms` server shuts down cleanly on stdio EOF** (#410) — when an MCP client (Claude Code, Codex, etc.) closes the stdio transport, the AnyIO task group occasionally raised `RuntimeError: Attempted to exit a cancel scope that isn't the current task's current cancel scope` (and the sibling "different task than it was entered in" variant) during teardown. `mcp.run()` is correct by then — the only work left is process exit — but the error propagated out of `server.main()` and was logged via `logger.exception(...)` as an unhandled crash, alarming operators tailing logs and producing a non-zero-looking shutdown. `main()` now recognizes the two known AnyIO cancel-scope-shutdown messages, logs them once at WARNING as ignored, and returns 0; any other `RuntimeError` keeps the original `logger.exception(...)` + re-raise path so genuine failures are not masked. Regression coverage in `tests/test_server_tools.py` pins both shutdown-message variants to the swallow path and an unrelated `RuntimeError` to the re-raise path.

## [0.1.24] — 2026-05-31

This release extends proactive memory surfacing to **Claude Code's built-in tools** through `mms hook` and a warm background daemon, adds a **network transport** for the LTM connection, makes response **compression query-aware and its JSON tiers safer** (the JSON-emitting tiers now return strictly-valid JSON and degrade monotonically, staying within budget except SELECTIVE's documented zero-preview-floor exception), and lands a broad surfacing-quality pass — relevance buckets, persisted auto-tuning, richer feedback ratings — alongside several query-privacy controls.

Citations are the merged PR numbers; an `issue #N` reference is added for source-issue traceability where one exists.

### Added

- **`mms hook` surfaces memories for built-in tools** (#378, #383) — injects relevant LTM memories for Claude Code's native tools (not only MCP-proxied servers) and can compress oversized Bash tool output inline via `updatedToolOutput`.
- **Warm surfacing daemon for `mms hook`** (#379, #380, #381, #382) — a lock-guarded background daemon keeps the embedding scorer and LTM session hot across hook calls, auto-spawned on first use and keyed by config fingerprint. **Behavior change**: daemon mode defaults on (`use_daemon=True`).
- **Network LTM MCP transport** (#398) — surfacing can reach the LTM server over a network transport, not just a local stdio subprocess.
- **Query-aware compression** (#386, #393, #394) — SELECTIVE / Hybrid / SCHEMA_PRUNING / SKELETON rank their table-of-contents by relevance to the active query, and the manager's relevance scorer is injected into the SelectiveCompressor.
- **Surfacing tunability + bootstrap readiness** (#324, #325, #326, #331, #332, #333, #336) — `_context_query` threading, `result_content_max_chars` / `preview_max_chars` knobs (applied to the assembled preview), per-tool feedback + auto-tune readiness in `stm_surfacing_stats`, AutoTuner `min_score` adjustments persisted across restarts, and readiness surfaced via `stm_proxy_health`.
- **Richer surfacing feedback** (#370, #373, issue #353) — `stm_surfacing_feedback` accepts batched per-memory `ratings`, and a new `partially_helpful` rating sits between `helpful` and the negatives with split helpful/negative AutoTuner bands.
- **`mms health` probes LTM reachability** (#356, #358, issue #349) — actively checks the configured LTM MCP command, and surfacing logs a one-shot WARNING when LTM is unreachable instead of silently disabling.

### Changed

- **Surfaced memories render relevance buckets, not raw scores** (#400, issue #360) — lines show `[weak]` / `[related]` / `[strong]`. **Behavior change**: read buckets, or `stm_surfacing_stats` for raw scores.
- **Surfaced sources show `parent/basename` and honor `default_namespace`** (#399, issue #359) — nested sources disambiguate by one parent directory. **Behavior change**: default-namespace results now carry a `[default]` badge.
- **`injection_mode` default `prepend` → `append`** (#355, issue #348) — the old default silently disabled surfacing on the progressive-delivery path. **Behavior change**: memories now append by default; explicit `prepend` is unaffected.
- **`min_score` default `0.02` → `0.03`** (#329, #330) — raises the surfacing floor per the F2 sweep; marginally fewer low-score memories surface.
- **AutoTuner bounds are configurable and validated** (#392) — the AutoTuner's `min_score` adjustment range is now operator-configurable and validated at load time, and pinned per-tool adjustments can be purged.
- **Observability/admin MCP tools are opt-in** (#343, issue #228) — `MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS` defaults to `false`; only the four model-facing STM tools are advertised unless re-enabled.
- **`stm_surfacing_stats` splits healthy vs fault skips and per-tool vs global readiness gaps** (#365, #366, issue #351). **Behavior change**: tooling scraping the old single skip-reason header must read the two new headers.
- **`ProxyConfig.default_compression` is now honored** (#300, issue #292) — previously declared but never read. **Behavior change**: configs that set it but omitted per-server `compression` now follow the global default.

### Fixed

- **Compression tiers degrade safely — valid output, monotonic, budget-aware** (#384, #390, #395, #396, #397, #401, #402, #403, #405, #406, #407) — held Truncate invariants (len≤max_chars, valid JSON, preserved content); routed multi-array dicts to SCHEMA_PRUNING by summed length; budget-filling, monotonic final tiers for SchemaPruning and FieldExtract (valid JSON) and Skeleton (valid, heading-preserving — not JSON); NaN/Infinity sanitized to `null` so the JSON tiers stay strictly parseable; Hybrid overshoot falls back structurally instead of raw-slicing JSON; standalone SELECTIVE shrinks previews rather than dropping entries (its two-phase TOC may still exceed budget at the zero-preview floor — the documented trade-off); and the FieldExtract final tier + `_compress_json` router degrade monotonically.
- **Compression retention ladder** (#385) — unified heading detection, re-checked the truncate-fallback ratio, and threaded `context_query` through the ladder.
- **Surfacing query extraction + dedup** (#387, #388, #389) — tokenized Grep/Glob patterns and stabilized query extraction, matched `write_tool_patterns` against the full `server__tool` name, and reclaimed cache-hit memory IDs into the session-dedup set.
- **Repeated negative feedback locally demotes memories** (#404) — memories with repeated `not_relevant` / `already_known` across distinct events are filtered before injection (STM-side only; configurable via `feedback_demotion_enabled` / threshold).
- **AutoTuner treats `already_known` as negative feedback** (#354, issue #347). **Behavior change**: per-tool adjustments learned via the old ratio re-converge over subsequent feedback.
- **Surfaced-memories block survives truncation and enumerates valid ratings** (#357, issue #350) — the surfacing ID and rating spec move above the bullet list so truncation can't drop them.
- **LTM connection robustness** (#294, #337, #338, #344, #377) — heal the MCP session after outer-timeout cancellation, type adapter outcomes for distinct skip labels, defer the LTM client start to the first RPC, fix initial upstream connection cleanup, and guard legacy feedback ratings before dispatch.

### Security

- **Query-text privacy controls** (#367, #368, #369, #391, #327; issue #352) — the INFO log no longer leaks the extracted query (preview moved to DEBUG); `surfacing_events.query` ages out after `query_retention_days` (default 30); opt-in `persist_query_text=False` stores a `sha256:` digest; secret-bearing queries are hashed on the proxy persist path; and the privacy scan now covers the full payload rather than the first 10K chars.
- **LLM compression scans for secrets first** (#293, issue #289) — `llm_summary` runs the privacy patterns before any outbound call and short-circuits to truncate on a hit (`privacy_scan_enabled`, default on).

### Internal

- `CircuitBreaker.opened_at` exposed as a public read-only property (#342, issue #277); proxy/manager progressive store widened to the `PendingStore` protocol with a non-None embedding-base-URL assertion (#374, #375, issue #164); mypy union-attr cleanup (#376); a `bench_qa` `s11` min_score sweep harness (#328); a guard rejecting module-level POSIX-only stdlib imports (#346, issue #302); compression docs for mode-aware surfacing on the progressive path (#323); a test pin for `auto_tune_adjustments` in bootstrap readiness (#335); and docs/test pins for the inert auto-index path, the cross-referenced stdio LTM transport site, the LTM-bypasses-pipeline note, and the Stage 4 inert claim (#339, #340, #341, #345).

## [0.1.23] — 2026-05-06

This release lights up **Windows as a gating CI platform**. Issue #302 ran a multi-week ladder (#303–#319, #321) shaking out Windows-specific bugs across the cli, the file-IO utilities, the surfacing feedback store, and ~10 test sites that assumed POSIX-shaped HOME / file-mode / monotonic-clock semantics. With every rung green, #320 PATCHed `main`'s required-status-checks to require `lint (windows-latest)` and `test (windows-latest)` — Windows is now a first-class supported platform, not an informational matrix axis. There are no new features; v0.1.23 is the first wheel published with Windows officially under CI gate.

### Fixed

- **`_run_claude_mcp` subprocess now pins UTF-8 on Windows** (#303) — `subprocess.run` inherits the system codepage on Windows (`cp949` on Korean locale, `cp1252` on Western European, etc.), so any MCP discovery output containing non-ASCII bytes (server names, descriptions, tool docstrings) decoded with `mojibake` and `mms add --from-clients` / `mms import` either raised `UnicodeDecodeError` or silently round-tripped garbled strings into the registry. `subprocess.run` calls in `cli.proxy._run_claude_mcp` now pin `encoding="utf-8"` + `errors="replace"` explicitly so the byte stream is interpreted the way the upstream MCP client emits it (UTF-8 across all four host scanners) regardless of the operator's system locale. `errors="replace"` (not `"strict"`) is deliberate: a corrupted byte from a misconfigured upstream should surface a `?` in the parsed output rather than crash a multi-host import that's otherwise succeeding. POSIX behavior is unchanged because POSIX defaults are already UTF-8.

- **`utils/fileio` retries `os.replace` on Windows transient `WinError 5`** (#302 P1d, #307) — `os.replace(tmp, dest)` is the atomic-write substrate under every TOML write in mms (`registry.toml`, `import_state.toml`, `stm_proxy.json`, the surfacing feedback store, the pending store) and the proxy metrics DB rotation. On Windows the call intermittently raises `PermissionError: [WinError 5] Access is denied` when an antivirus or indexer briefly holds the destination open — POSIX `rename(2)` would just succeed against an open destination, but Windows `MoveFileEx` doesn't. The replace path now retries up to 5 times with 10ms exponential backoff before re-raising; the retry budget is bounded so a *real* permission error (truly read-only file, ACL deny) still surfaces within ~310ms instead of looping. POSIX and macOS hit the success path on the first attempt with zero behavior change. Follow-up #318 widens the test reader to tolerate transient `PermissionError` from the same race in test fixtures, and #321 yields 1ms per reader iteration so the reader thread can't starve the writer's retry window when the writer is itself doing the retried `os.replace`.

- **`mms add --args` tokenizer is now backslash-safe on Windows** (#302 P1e, #314) — the `--args` flag accepts a single string and tokenizes it via `shlex.split` so users can paste a multi-arg command line. POSIX `shlex` treats `\` as an escape, which is the right default for `--args "/path/to/file with space"` on Linux/macOS, but on Windows it consumes path separators in `--args "C:\path\to\server.exe --port 1234"` and produces `C:pathtoserver.exe`. The cli now branches on `sys.platform == "win32"` and passes `posix=False` to `shlex.split`, which keeps `\` literal and treats `"..."` as the only quoting form — matching cmd.exe / PowerShell semantics. Cross-platform CI now exercises both paths so the POSIX path doesn't regress.

- **Surfacing feedback store recent ordering is now deterministic on tied `created_at`** (#302 P1e, #316) — `feedback_store.recent(limit=N)` previously returned rows in insertion order *until* two rows shared the same `created_at` Unix timestamp, at which point SQLite's `ORDER BY created_at DESC` ordering became implementation-defined. POSIX `time.time()` provides nanosecond resolution that practically never collides; Windows `time.time()` ticks at ~15.6ms (the system timer), so two feedback writes inside one operation routinely tied. The query now sorts by `(created_at DESC, rowid DESC)` so the secondary sort lands on insertion order regardless of clock resolution. Surfaced via the test suite on Windows but the fix is platform-agnostic correctness — POSIX users with batch writes inside a single timer tick benefit too.

### Changed

- **Windows is now a required CI status check on `main`** (#320) — `lint (windows-latest)` and `test (windows-latest)` were added as informational matrix axes in #304 and graduated to required-status-checks via the GitHub branch-protection API once #302 went green. Future PRs that break Windows fail the gate before merge instead of accumulating Windows-only debt against an advisory leg.

### Internal

- **`windows-latest` matrix axis added to lint + test workflows** (#304) — informational only at first; promoted to required by #320.
- **`set_home` test helper + 10-site HOME-patch sweep** (#305) — replaced direct `os.environ["HOME"] = ...` with a context-manager helper that round-trips the `USERPROFILE` env var Windows actually consults, so tests that exercise `~/.mms/` paths don't silently read the developer's real home dir on Windows. #310 tightened the helper's undo contract for the case where `HOME` was absent before the test set it.
- **NTFS mode-bit assertions skipif'd on Windows** (#306) — `os.stat(...).st_mode & 0o077` checks against `0o600` for sensitive sidecar files (`import_state.toml`, etc.) are POSIX-only — NTFS doesn't track POSIX mode bits and Python returns synthetic values. The assertions are now skipped on `sys.platform == "win32"` rather than re-implementing the equivalent ACL check (which would test the OS, not the writer).
- **Test platform-detection unified on `sys.platform == "win32"`** (#309, #312, #313) — replaced the mix of `os.name`, `platform.system()`, `sys.platform.startswith("win")`, and `is_windows()` helpers with one idiom across the test tree. #313 restored an `import os` line that the convention sweep over-removed.
- **`tests/test_qa_round3` reads docs with `encoding="utf-8"`** (#302 P2, #315) — `Path.read_text()` defaults to the system codepage on Windows, so QA round-3 fixtures containing Korean comments raised `UnicodeDecodeError` on `cp949`. Now pins UTF-8 at every test-side `read_text` call (production code already pinned encoding via #303 and prior).
- **Two timer-tick races fixed on Windows** (#317, #319) — `pending_store.touch` and the compression-key TTL test both compared `time.monotonic()` deltas against thresholds smaller than Windows' ~15.6ms timer tick and intermittently saw 0ms gaps. Both tests now sleep above the platform's tick floor before the assertion.
- **`utils/fileio` reader thread tolerates `PermissionError`** (#318, follow-up #321) — companion to #307's writer retry: when the writer is in its retry window, the reader could observe the destination in a transient access-denied state. The reader now treats `PermissionError` the same as "file not yet visible" and retries on its own polling loop. #321 added a 1ms `time.sleep(0.001)` yield per reader iteration so the reader doesn't tight-loop the writer's retry window into starvation under Windows' coarser scheduler.

## [0.1.22] — 2026-05-02

This release lands the full **mms host-config management surface** (RFC v0.3 Workstreams 1–3) — `mms project`, `mms import`, and the `mms host` subgroup (`status`/`scan`/`sync`). Together they form the read→reconcile→write-back loop for keeping host MCP configs in sync with the mms registry.

### Added

- **`mms project` subgroup — W1 of project-scoped MCP management RFC v0.3** (#278) — five Click subcommands (`init`, `show`, `list`, `enable`, `disable`) that manage *which MCP servers a given project sees*, separate from the STM proxy gateway config. State lives in a new dotdir, `~/.mms/`, with three TOML files: a global registry (`~/.mms/registry.toml`, secrets in env, gitignored), an auto-managed projects index (`~/.mms/projects.toml`, gitignored), and per-project enabled-MCP lists (`<project>/.mms/project.toml`, commit-recommended). All three start at `schema_version = 1`; `mms upgrade-config` (the migration entry point per RFC §16) is a W2+ separate code path. `~/.mms/` is intentionally separate from `~/.memtomem/` — STM proxy bootstrap (`stm_proxy.json`) and mms project state (`registry.toml`) are *fully disjoint mutation paths* in W1: `mms add` writes only to `stm_proxy.json`, and `mms import --apply` writes only to `registry.toml`. The registry stays empty until `mms import` populates it; `mms project enable X` against an empty registry surfaces a *friendly error* pointing at `mms import --from <host>` instead of crashing — the literal text is pinned by tests so any rewording is a single-place edit. Project detection (used by `show` / `enable` / `disable` without `--project`) walks parents in fixed order: `.mms/project.toml` marker → `.git` directory or worktree file → cwd fallback (anonymous). A marker beats git even if git is closer to cwd, since markers are explicit declarations and git roots are inference; malformed markers raise rather than silently falling through to git/cwd, per RFC §6.1. Pydantic models reject unknown TOML fields (`extra="forbid"`) so a typo in `[project]` or `[mcp]` fails loud at load time. Adds `tomli-w >= 1.0` to dependencies for writes (reads stay on stdlib `tomllib`); `tomlkit` would have brought a comment-preservation feature with no W1 use case.

- **`mms import` command — populate the registry from existing host configs** (#279) — RFC §7.2. Reads MCP definitions out of Claude Code (`~/.claude.json` user + per-project + `<cwd>/.mcp.json`), Cursor (`~/.cursor/mcp.json` user + project), Codex CLI (`~/.codex/config.toml` under `[mcp_servers.<name>]`), and Claude Desktop (macOS only) and writes them to `~/.mms/registry.toml`. `--from all` (default) iterates every host scanner; `--plan` (default mode) is a dry-run with secret env values redacted in stdout, `--apply` writes the registry, `--show-imported` reveals secret values in `--plan` output for a verify-before-apply flow. Secret classification is two-signal per RFC §7.2.1: key pattern (`*TOKEN*`, `*KEY*`, `*SECRET*`, `*PASSWORD*`, `*PASS*`, `*AUTH*`, `*CREDENTIAL*`, `*API_KEY*`, case-insensitive substring; hits even on short values like `API_KEY=test`) OR value heuristic (length ≥ 32 AND base64- or hex-charset; catches opaque tokens under unusual key names). When the same name appears twice (across hosts in one `--from all` run, or already in the registry), reconciliation is **first-import-wins** — identical re-imports are idempotent (no-op + `Already up to date.`), differing entries are reported as conflicts and skipped without overwriting. Dangerous env keys that could enable code injection through proxied subprocess (`LD_PRELOAD`, `DYLD_INSERT_LIBRARIES`, `PYTHONPATH`, `NODE_OPTIONS`, etc.) are filtered from every imported entry regardless of host. The five shape-agnostic helpers that previously lived privately in `cli/proxy.py` (`_DANGEROUS_ENV_KEYS`, `_BLOCKED_IMPORT_NAMES`, `_desktop_config_path`, `_read_json_safely`, `_is_self_reference`) now live in `mms/import_hosts.py` as the single source of truth and `cli/proxy.py` re-imports them — `mms add --from-clients` continues to work unchanged. Linux/Windows host paths for Claude Desktop are out of W1 scope; missing host configs are silently treated as "no candidates" so `--from all` always works as long as at least one host has something to import.

- **Drift-hash foundation: canonical form + sidecar baseline** (#280) — W2 PR1, RFC §5.2. Lays the substrate every later W2/W3 command builds on: a `compute_drift_hash(server)` function that hashes the *meaningful* shape of an `MCPServer` entry — `command + ordered args + key-sorted env`, *excluding* the `prefix` field — into a `sha256:<16hex>` string, plus a per-mms sidecar TOML at `~/.mms/import_state.toml` that records `(drift_hash, drift_hash_version, last_imported, source_label)` per registry entry. Cross-host equivalence is a load-bearing property: the same `github` MCP entry hashes byte-identical across the four host config formats (Claude Code JSON, Cursor JSON, Codex TOML, Claude Desktop JSON) — pinned by `test_canonical_form_byte_equal_across_hosts`. The sidecar's `IMPORT_STATE_SCHEMA_VERSION` is decoupled from the registry's `SCHEMA_VERSION` (verified by both-direction monkeypatch tests) so a future migration can bump one without touching the other. `drift_hash` and `drift_hash_version` are validated by `Field(pattern=r'^sha256:[0-9a-f]{16}$')` and `Field(ge=1)` so a corrupt sidecar fails fast at load time. Behavior change: none — this PR ships infrastructure only; no code path yet writes the sidecar, that lands in #281.

- **Wire drift-hash into `mms import --apply` + idempotent backfill** (#281) — W2 PR2. Stamps `~/.mms/import_state.toml` (mode `0o600`) on every accepted candidate during `mms import --apply` — closing the path from "import succeeds" to "next sync can detect drift". The `idempotent` candidate path now also backfills missing sidecar rows (rather than being a pure no-op): if a candidate's name is in the registry but absent from the sidecar — possible after a crash mid-write, a manual sidecar delete, or when upgrading from a pre-PR2 install — the row gets stamped with the same single `now` capture used by genuinely new candidates, so all rows in one apply share one `last_imported` timestamp (single-timestamp invariant). Backfill `source_label` reflects the *current observing host* rather than the original import host: the host scanner only sees what's present *now*, and "original import host" isn't recorded recoverably; a baseline imported via Claude Code that later only appears at Cursor will backfill with `source_label = "Cursor (user)"`. This is asymmetric to a fresh first-import (which uses the actual current scanner host) by design — pinned by `test_backfill_source_label_uses_current_observing_host`. Mixed new+backfill in one apply emits both `Wrote N new entr[y/ies]` and `Backfilled M sidecar row[s]`. Test setup gotcha: pre-seeding the registry directly via `save_registry` does NOT yield idempotent classification because the host scanner derives `prefix` independently — only path to a real idempotent candidate in tests is first-apply via the real scanner, then mutate.

- **`mms host status` — drift bucket inspection** (#282) — W2 PR3, RFC §7.3. New `host` Click subgroup whose first command, `status`, classifies every registry entry against the sidecar baseline into one of four states: `unchanged` (host scan finds it AND canonical hash matches the baseline), `changed` (host scan finds it but the hash differs — host config was edited externally), `removed_at_host` (registry has the entry, sidecar has the baseline, but no host scanner finds it), or `no_baseline` (sidecar lacks a row for this entry OR the sidecar's `drift_hash_version` doesn't match the running mms's `HASH_VERSION`). Read-only: no writes anywhere, exit code is always 0. Text output renders `unchanged` + `changed` rows as a 3-column table (NAME / STATE / SOURCE), with `removed_at_host` and `no_baseline` as footer counts in PR3 (PR4 promotes `removed_at_host` to the table). `--json` always emits a 4-key summary (one count per state) plus per-entry rows with name + state + `source_label` + `baseline_hash` + `current_hash` + `last_imported`; `current_hash` is uniformly the *host's* view across all four buckets (recomputed per call from the matched candidate, or `None` if no host has the entry) — pinned by `test_current_hash_is_host_view_across_buckets`, after a review caught an initial bucket-asymmetric implementation. The 4-state design (vs. 2 obvious states) is load-bearing: `import_state.entries[name]` can `KeyError` along three real paths PR2's backfill alone doesn't close — manual sidecar edits, atomic-write race windows, and pre-PR2 installs that upgrade and call `mms host status` before any subsequent `mms import --apply`. `no_baseline` is the explicit recovery bucket whose actionable hint is `mms import --apply` to stamp; same bucket gates future `HASH_VERSION` bumps so a v1 baseline against a v2 binary fails loud rather than silently miscomparing as "always changed". When the same name appears in multiple host configs, `_select_candidate_by_name` is two-pass: Pass 1 prefers the candidate whose `source_label` matches the sidecar's `baseline.source_label` (= the import-time host); Pass 2 falls back to first-seen across `ALL_HOSTS`. This pins the comparison axis to the import-time host so mirroring an entry into a second host doesn't flicker between `unchanged` and `changed` between runs.

- **`mms host status` table now includes `removed_at_host` rows** (#283) — W2 PR4. Promotes `removed_at_host` from a footer-only count into a main-table row alongside `unchanged` and `changed`. Single-function edit in `_render_text()`: filter widened to include `removed_at_host` (3 states now in `main_rows`) plus `STATE` column width `:<10` → `:<16` (longest state name = 15 chars + 1 pad). The footer count and `_FOOTER_REMOVED_AT_HOST_TEMPLATE` text are **frozen verbatim** as backwards-compat anchors: PR3 already used the footer count as a downstream signal, and removing it would silently break tooling that grepped PR3's output. Footer/table duplication is intentional. JSON shape (`_render_json` + `_classify` per-row dict) is untouched — JSON consumers always saw `removed_at_host` rows in the entries array; this PR is purely a text-mode UX change. New test `test_status_only_removed_at_host_renders_table` pins the new reachable code path where `unchanged + changed == 0` and `if main_rows:` opens for `removed_at_host`-only output. Row ordering still uses `registry.servers` insertion order (registry.toml file order) rather than state-priority sort — wait-for-signal: reopen if a user reports "missed a single `removed_at_host` buried among many `unchanged`".

- **`mms host scan` — host-side discovery surface** (#284) — W2 PR5, RFC §7.3 third command. Read-only inventory of every MCP entry visible to mms across the four host configs. Three columns: NAME / HOST / IN_REGISTRY. Complements `mms host status` along an orthogonal axis: scan is **host-anchored** (every host occurrence is a row), status is **registry-anchored** (every registry entry is a row). When the same name appears in multiple host configs, scan emits *every* occurrence (full inventory); status's first-match-wins is an axis difference, not an inconsistency. Four design choices each test-pinned: (1) same-name multi-host = full inventory, (2) `IN_REGISTRY` = name-match only (shape comparison delegated to status's `changed` bucket), (3) `Yes`/`No` rendering (universal terminal encoding — no Unicode glyphs that misrender on Windows / minimal terminals), (4) `--json` summary keys are `total` / `in_registry` / `new_at_host`, with `new_at_host` as **complementary symmetry** to status's `removed_at_host` (same boundary, opposite direction). `--from <host>` filter accepts the same case-insensitive choices as `mms import --from` (`claude-code` / `cursor` / `codex` / `claude-desktop` / `all`) so users get symmetric UX between the two commands; the choice list is derived from `ALL_HOSTS` rather than hard-coded so a future fifth host scanner is picked up automatically. Pluralization uses the existing `_ies_or_y` helper for `entr{y/ies}` plus a parallel `host`/`hosts` guard (the prior summary line "1 entries across 1 host" had a pluralization bug pinned by an existing test as a contract — fixed in the same PR). Empty-with-filter output reads `No MCP definitions found in <host> configs.` (scoped to the filter, not the misleading "across host configs" generic).

- **`mms host sync --plan/--apply` — write-back surface** (#285) — W2 PR6. Closes the host-side loop opened by `status` (#282) and `scan` (#284). Reconciles `~/.mms/registry.toml` + `~/.mms/import_state.toml` with the union of host scans via four mutating buckets and two surfacing-only buckets: **ADD** new host candidates → registry insert + sidecar stamp; **REMOVE** `removed_at_host` entries → registry delete + sidecar delete (first and only command in mms that *deletes* from the registry); **BACKFILL** `no_baseline` rows with a matched candidate → sidecar stamp; **CLEANUP** pre-existing orphan sidecar entries (sidecar has the row, registry doesn't) → sidecar filter; **SKIP `changed`** entries (footer + `summary.skipped_changed`, deferred to W3 `--force`); **SKIP `orphan_no_baseline`** (registry has the entry, sidecar has no baseline, no host scan finds it — no safe action; footer + `summary.orphan_no_baseline`). Atomicity: registry write first, sidecar write second; the sidecar is filtered to `set(import_state.entries) ⊆ set(registry.servers)` so a crash between writes self-heals on the next run (orphan baselines drop; missing baselines BACKFILL). TTY+`--yes` confirmation is a **new pattern in mms**: `--apply` with a non-empty REMOVE bucket prompts `click.confirm` on a TTY (lists every entry by name + `source_label` + `last_imported` plus a tail summary of non-destructive buckets), aborts on non-TTY without `--yes` (exit 2 + clear stderr message), and when combined with `--json` always requires `--yes` regardless of TTY (a confirmation prompt cannot be answered by a JSON consumer, and mixing prompt text with JSON output corrupts machine parsing). JSON shape is fully locked: top-level `mode` (plan/apply) + `aborted` (always-emit, false in normal flow, true on TTY decline / non-TTY abort / `--json`+REMOVE-without-yes) + `plan` (7 always-emit arrays: `add` / `remove` / `backfill` / `cleanup` / `skipped_changed` / `orphan_no_baseline` / `conflicts`) + `summary` (8 always-emit counts). `summary.unchanged` and `summary.conflicts` count *different units* — registry names vs. candidate occurrences — block-commented in the JSON emitter to prevent downstream sum-across bugs. Cross-host shape-relocation contract (load-bearing): when entry X is baselined to host A, A drops X, and host B picks up X with a different shape, sync surfaces X in BOTH `summary.skipped_changed` AND `summary.conflicts` and *does not* auto-replace the registry shape — pinned by `test_cross_host_shape_relocation_does_not_replace`. Mental-model docstring tripwires (`first-time` vs `ongoing-reconciliation` phrases pinned by `test_sync_docstring_mentions_first_time_vs_ongoing` + `test_import_docstring_mentions_sync_cross_link`) prevent silent drift between `mms import` (first-time entry point, additive only) and `mms host sync` (ongoing reconciliation; only command that deletes).

- **`mms host sync --force` — re-stamp `changed` entries (full reconciliation)** (#286) — W3. Closes the W2 carry-over by adding the only path that mutates the `changed` bucket. Without `--force`, `changed` stays non-mutating exactly as `mms host sync` ships in #285. With `--force`, eligible `changed` entries are **re-stamped as a full reconciliation**: the registry entry's `MCPServer` is replaced with the current host candidate's value AND the sidecar `drift_hash` + `last_imported` are refreshed against it, atomically under the same registry-first-sidecar-second invariant `mms import` and `mms host sync` already enforce. Sidecar-only re-stamp was rejected at design time: silently retaining a stale `MCPServer` while marking drift "acknowledged" would leave every downstream consumer (`mms import` re-runs, agent runtime, `mms host status`) with the *old* shape from the registry — permanent split state between registry / sidecar / host. The confirmation prompt shows an **old/new diff** per entry (`command` / `args` / env-key set, with env values redacted and env keys sorted for diff stability across host scanners) so users give informed consent for a destructive-but-visually-subtle change (the entry name doesn't disappear). When `command` + `args` + env keys all match but env values differ (e.g. token rotation), the diff renderer collapses to a single observable-shape line plus an explicit `(env values redacted; values changed)` note — user sees *why* re-stamp is firing without the values leaking. `--force` and `--yes` are orthogonal: `--force` activates the `changed` bucket as a mutator, `--yes` bypasses the interactive prompt; `--force` alone still prompts, `--force --json` without `--yes` aborts (exit 2, `aborted: true`) — same gate REMOVE uses. **Cross-host re-stamp is deliberately blocked** (Lock-down 6): when `_classify` Pass-2 fallback found the candidate at a *different* host than the baseline source, `--force` *skips* that entry rather than silently relocating its host-of-record — exactly the failure mode `mms host sync`'s "no silent reshape" contract was meant to prevent. Cross-host count surfaces in JSON `plan.skipped_changed[i].host_relocation: bool` and a text-mode footer pointing at `mms host status` for manual review; when *every* changed entry is cross-host, the standard "use --force to acknowledge" pointer is suppressed entirely (it would be misleading — `--force` won't help any of them) and only the manual-review footer fires. JSON shape additions are **additive at the entry-field level** (existing parsers reading `skipped_changed[i]["name"]` keep working): `summary.restamped: int` (always present, default 0) and `plan.skipped_changed[i]` gains five fields (`source_label` / `baseline_hash` / `current_hash` / `last_imported` / `host_relocation`).

## [0.1.21] — 2026-04-29

### Added

- **`stm_index_stats` MCP tool — INDEX pipeline observability** (#272) — `extract_and_store` (the auto-extraction path that writes facts to LTM) now carries a per-tool counter aggregate alongside `SurfacingObservability` (#256). Until this release, INDEX activity was visible only as per-call `ExtractOutcome.facts_stored` returns that the manager discarded — operators had no aggregate view to answer "is the auto-extraction pipeline actually producing LTM writes, or is it firing on tool calls that have nothing worth storing?". The new `IndexObservability` class (`src/memtomem_stm/observability/index_observability.py`) tracks per-tool `attempts` (incremented on every call) plus a 4-label `outcomes` dict (`stored` / `dedup_skip` / `extracted_zero_facts` / `error`). The split between `extracted_zero_facts` and `dedup_skip` is load-bearing: fusing them into `stored=0` would conflate "the extractor produced no facts" with "facts were produced but already in LTM", which are architecturally distinct signals. The new MCP tool `stm_index_stats` is gated by the existing `MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS` flag (default on), advertised right after `stm_surfacing_stats`; total advertised tool count goes from 11 to 12. State is in-memory only — counters reset on process restart, mirroring `SurfacingObservability`. No quality dimension is included by design: any quality signal for INDEX would have to choose between *positive-value* (does an extracted fact later surface on a related query?) and *harm-prevented* (what fraction of writes would have leaked sensitive content absent redaction?), which are non-equivalent observables; deferring lets a future quality-signal PR scope to one or both deliberately. Wire-in is via a kwarg-only `observability=` parameter on `extract_and_store` defaulting to a no-op singleton, so any direct caller works unchanged.

- **Token-equivalent budgets for CJK / non-Latin-script workloads** (#274) — every result-size budget in STM was previously expressed in characters, and the `effective_max_result_chars()` path used a hardcoded `3.5` chars-per-token multiplier that is English-biased. For Korean / Chinese / Japanese content this produced a meaningful budget gap: empirical measurement on a 13-pair EN/KO documentation corpus from `memtomem-com` against `tiktoken cl100k_base` showed KO at 1.85 chars/token vs EN at 4.03 (KO is 2.18× denser per char), and 10 of 11 KO docs that don't trigger compression at the char gate **would** trigger at the token-equivalent threshold — a 77% silent miss on translated content. Two opt-in fields make budgets token-aware without breaking existing char-based configs: (1) **`chars_per_token`** at `ProxyConfig` (default `3.5`, English-biased), `UpstreamServerConfig`, or `ToolOverrideConfig` — set to `~2.0` for Korean-dominant content, `~1.3` for Chinese-dominant; cascading resolution is tool override → server → proxy default. (2) **`max_result_tokens`** on `UpstreamServerConfig` and `ToolOverrideConfig` — when set, takes precedence over `max_result_chars` and is converted to a char budget at gate time via the resolved `chars_per_token`. Backward compat: all existing char-based fields and defaults are unchanged; a `ProxyConfig` with no new fields produces identical `max_chars` values as before. A new `proxy/token_estimate.py` module ships a Unicode-block-weighted codepoint approximation calibrated against `cl100k_base` (`approx_tokens` + `tokens_to_chars`) — currently only `tokens_to_chars` is used at gate time; `approx_tokens` is published for a follow-up that estimates real response token counts at runtime instead of relying on the operator's static ratio. **Note for operators**: setting the same `max_result_tokens` across EN and KO upstreams bounds context spend equally but does not preserve equal information content (KO encodes the same information in roughly 1.57× more tokens at `cl100k_base`); see `docs/configuration.md` for the spend-vs-information distinction.

- **`mms init --lang ko` writes a Korean-tuned token-aware budget preset** (#275) — closes the discoverability gap left open by PR #274. PR #274 shipped the mechanism (`max_result_tokens`, `chars_per_token` at proxy/server/tool levels) but required Korean operators to manually transcribe four numeric fields per server. The new `--lang` option (and matching interactive prompt on TTY) writes the calibrated KO preset directly: `chars_per_token=1.85`, `default_max_result_chars=8500`, `min_response_chars=230` at proxy level, and `max_result_tokens=2000` + `chars_per_token=1.85` on every imported upstream. Numbers come from PR #274's empirical 13-pair EN/KO doc-corpus calibration against `cl100k_base` (median 1.85 chars/tok for KO, vs 4.03 for EN). EN (default) is intentionally a no-op — config matches code defaults exactly without per-key noise. ZH/JA placeholders are deliberately omitted until analogous corpus measurements land — guessing by typology would defeat PR #274's empirical-calibration framing. Non-TTY callers without `--lang` get EN silently (load-bearing for backward compat with existing `mms init` tests); explicit `--lang` is the scriptable path. The interactive prompt uses `questionary.select` on a TTY and falls back to `click.prompt` with a `Choice` for `MMS_NO_TUI=1` users.

- **README coverage section: what STM proxies, and what it doesn't** (#273) — adds a top-level README section that names the MCP-proxy boundary explicitly: Claude Code built-in tools (`Read`, `Write`, `Edit`, `Bash`, `Grep`, `Glob`, `WebFetch`), Cursor / Windsurf / Claude Desktop built-ins, and built-in calls inside sub-agent (`Agent` / `Task`) invocations all bypass STM. Cross-references the filesystem MCP example in Quick Start as the standard workaround for bringing file/shell operations under STM. A reader on Claude Code could previously assume STM intercepts their `Read`/`Bash` traffic; in fact STM only sees what the client routes through MCP, so a meaningful share of that traffic stays invisible. The boundary affects expected ROI relative to the README's "20–80% token savings" headline, so the section sits between Quick Start and Tutorial Notebooks where the reader has just connected and is forming a mental model of "what's covered now". Pure documentation — no code or behavior change.

### Fixed

- **`__version__` drift in `src/memtomem_stm/__init__.py` resynced to current release** — `__version__` had drifted to `"0.1.18"` while `pyproject.toml` advanced through 0.1.19 and 0.1.20 without a parallel bump. The same drift class was previously surfaced and fixed in v0.1.12 (recorded in `project_stm_v016_milestone.md`). Both files now read `0.1.21`. The duplication itself is the root cause and a follow-up to consolidate to a single source-of-truth via `importlib.metadata` is tracked but out of scope for this release.

## [0.1.20] — 2026-04-27

### Changed

- **`ProxyConfig` now rejects upstreams with empty or whitespace-only `prefix`** (#266) — `UpstreamServerConfig.prefix: str` had no min-length, so `""` validated fine and produced composed tool names like `__list_items` that surfaced in `tools/list` as visually broken entries. The empty prefix also skewed `tool_name_budget.composed_length("", t.name)` (the prefix portion is zero), so the 64-char overflow guard added in #261 underestimated the real surface name a client sees, and a single empty prefix slipped past the duplicate-prefix validator added in #265 (which only fires on collisions). A new `@model_validator(mode="after")` on `ProxyConfig` raises `ValidationError` at config load with every offending upstream key listed together — `Empty upstream prefix in upstreams: ['blank', 'spaces']` — so the user fixes them all in a single round-trip without the uniqueness validator firing as second-iteration noise. Whitespace-only (`"   "`) is treated the same as empty since both are typo classes. **Behavior change:** configs that previously loaded with broken composed names now fail at startup; hot-reload (`ProxyConfigLoader.get`) retains the previous good config on validation failure, matching the behavior introduced for #265.
- **`ProxyConfig` now rejects two upstreams that share the same `prefix`** (#265) — composed names `<prefix>__<tool>` would collide, and `ProxyManager.start()` defensively dropped the second-loaded duplicate with only a `logger.warning`, leaving operators with mysterious missing tools and a buried log line as the only signal. A new `@model_validator(mode="after")` on `ProxyConfig` raises `ValidationError` at config load with a single message that names every collision group together — `Duplicate upstream prefixes detected: prefix 'foo' used by upstreams: ['serverA', 'serverB']` — so both the upstream dict keys (what the user edited) and the colliding prefix value (what collides) are visible without re-reading the file. **Behavior change:** configs that used to load with silent partial drops now fail at startup. Hot-reload (`ProxyConfigLoader.get`) keeps the previous good config on validation failure, so a running proxy stays serving its last known-good upstreams instead of going dark. The `seen_prefixed` dedup in `ProxyManager._connect_server` is retained as defense-in-depth (covers `model_construct()` and any future config source that bypasses validation). The empty-prefix case (single upstream with `prefix=""`) is tracked separately as #266.

### Added

- **Tool name overflow check against the 64-char MCP limit** (#261) — clients (Claude Code, Antigravity, the Anthropic SDK) compose a proxied tool name as `mcp__<client-server>__<prefix>__<tool>` and silently drop any tool whose composed name exceeds the spec's `^[a-zA-Z0-9_-]{1,64}$` regex. Previously a single overflowing tool from an otherwise-healthy upstream just disappeared from the client's catalogue with no signal — surfaced in production after registering LangChain's `query_docs_filesystem_docs_by_lang_chain` (40 chars) under a 14-char prefix and watching that one tool go missing. Now there are three layers of enforcement: (1) `mms add` rejects prefixes that guarantee overflow even with a 1-char tool name (>42 chars for the default 12-char client server name) and warns above the soft threshold (>21 chars) with both fix paths surfaced (shorten `--prefix`, or register STM as `mms` to gain 9 bytes of budget); (2) `ProxyManager._connect_server` now per-tool checks the composed length against the spec limit and skips offending tools at boot — better one missing tool than the whole upstream — emitting a `WARNING` log naming the tool, the composed length, and the suggested fix; (3) `mms health --names` re-runs the boot-time check on demand so a user diagnosing "one tool from server X is missing" can answer it without spinning STM up. The arithmetic lives in `memtomem_stm.proxy.tool_name_budget` and assumes Claude Code's stricter `mcp__…__…__…` template (3 × `__`) so any false-positives are conservative — clients using a leaner format would never reject a tool we cleared. The default client server name (`memtomem-stm`) can be overridden with `MMS_CLIENT_SERVER_NAME` for users who registered STM under a custom (typically shorter) name.
- **README registration example now uses `mms` instead of `memtomem-stm`** (#261, follow-up to #260) — `mms` (3 chars) saves 9 bytes of overhead vs `memtomem-stm` (12 chars) when the client composes proxied tool names, leaving room for upstreams with long tool names. Both names continue to work; the rationale is documented inline so users can keep their existing `memtomem-stm` registration if preferred.

### Fixed

- **`TestAutoIndexStartupWarning` no longer flakes under full-suite event-loop pressure** (#264) — `test_no_warning_when_compression_none` and its sibling `test_warns_compression_without_auto_index` wrap `await mgr.start()` in `try/except Exception` because the test only cares about the *warning* assertion that follows; the `echo` upstream is expected to fail the JSON-RPC handshake. But the MCP SDK's stdio reader can surface that handshake failure as `asyncio.CancelledError` (a `BaseException`, not an `Exception`), so the cancellation leaked past the catch and the test failed under enough total async test mass to push timing across a threshold (~50% locally on PR #263 which added 23 tests; not reproducible in isolation). Catch widened to `(Exception, asyncio.CancelledError)` with a comment naming the SDK cancellation path so a future reader doesn't narrow it back. No production code touched.

- **All three console scripts (`memtomem-stm`, `memtomem-stm-proxy`, `mms`) now boot the MCP stdio server when invoked bare with a piped stdin** (#260) — previously only `memtomem-stm` worked as an MCP-server registration target because it pointed straight at `server.main`, while `memtomem-stm-proxy` and `mms` resolved to the Click group whose bare invocation printed help and exited 2. An MCP client config like `{"command": "mms"}` therefore failed with `: calling "initialize": EOF` even though the package documentation calls these names interchangeable. The `cli` Click group now uses `invoke_without_command=True` and dispatches based on `sys.stdin.isatty()`: a TTY still prints the help text (now exit 0, matching Click's default `get_help` flow), and a non-TTY (the MCP-client stdio case, CliRunner, pipes) lazy-imports `memtomem_stm.server.main` and runs it. All three `[project.scripts]` entry points in `pyproject.toml` now resolve to `memtomem_stm.cli.proxy:cli`, so registering any of them in an MCP client's config works identically — closing the parity gap that surprised first-time Antigravity adopters.

## [0.1.19] — 2026-04-26

### Added

- **`stm_surfacing_stats` now reports per-tool skip reasons, outcomes, and cache hit ratio** (#256 — Phase 1 of the surfacing observability + UX RFC) — every reject path through the surfacing pipeline now increments an in-memory counter that an operator can read from `stm_surfacing_stats`. Previously every `RelevanceGate` reject (5 sub-reasons), engine-level skip (4: `disabled`, `response_too_short`, `circuit_open`, `no_query`), and post-search no-result path (4: `no_results_score`, `no_results_dedup`, `no_results_invalidated`, `no_results_empty_cache`) logged at `DEBUG` and disappeared — an operator who noticed "surfacing feels quiet" had no quantitative path from observation to root cause without grepping logs and reconstructing. Three new sections (`Skip reasons`, `Outcomes`, `Cache`) appear after the existing DB-backed stats output, aggregated per tool plus a `__total__` row, sorted descending by count so the dominant skip lands first. Counters live in a new `SurfacingObservability` class (`src/memtomem_stm/surfacing/observability.py`), wired into `SurfacingEngine` and `RelevanceGate` via a keyword-only `observability=` parameter that defaults to `None` for embedding callers and existing test fixtures. State is in-memory only — counters reset on process restart, mirroring the in-process aggregate shape of `stm_compression_stats` / `stm_progressive_stats`. The new sections are suppressed entirely when no surfacing call has been recorded, so the existing output stays byte-for-byte for zero-traffic deployments and scripted callers parsing the legacy fields keep working unchanged. Each `surface()` invocation records exactly one skip OR one outcome (no double-counting); cache hit/miss is incremented separately on every cache lookup independent of what the lookup ultimately renders. The full decision tree and the wait-for-signal Phases 2–5 (auto-tune saturation warning, top-1-preserving truncation, implicit feedback, operator CLI / hints forward / format A/B) are documented in a private design note.

### Internal

- **Phase 1 observability test coverage closed for three remaining reject paths** (#257) — `TestSurfacingEngineObservability` now pins `no_query` (`engine.py:170`), `no_results_dedup` (`engine.py:499`), and `no_results_invalidated` (`engine.py:350`) integration tests, so the 13 SkipReason + 4 Outcome + cache hit/miss matrix from the RFC §Resolution order is exhaustively covered across `test_surfacing_observability.py` (counter contract), `test_relevance_gate.py` (5 gate paths), and `test_surfacing_engine.py` (8 engine paths + 4 outcomes + cache). The `no_query` test overrides `min_query_tokens` explicitly rather than relying on the default value, so a future default change cannot silently push the test onto a different path.

## [0.1.18] — 2026-04-25

### Added

- **`error_message` column on `proxy_metrics.db`** (#254, closes #253) — every error row now persists the failing source's text alongside `error_category` / `error_code`, capped at 500 chars. Populated at all six exception-driven capture sites in `ProxyManager.call_tool` (PROGRAMMING / PROTOCOL / TRANSPORT / TIMEOUT / LOCK_TIMEOUT / INTERNAL_ERROR) plus the upstream `result.isError=True` site (UPSTREAM_ERROR uses the upstream's text payload directly, not a Python exception). Closes the asymmetry where `index_error` / `extract_error` / `surface_error` already stored TEXT but the upstream/protocol/transport/timeout categories did not — post-mortem inspection of the DB can now distinguish *why* a call failed (e.g. JSON-RPC `-32602` "Invalid params: page_path" vs. an upstream tool returning a slug-not-found message) without re-running it. Helper `format_error_message_from_exc(exc)` (in `proxy/metrics.py`) keeps the truncation pattern in one place; UPSTREAM_ERROR uses `original_text[:MAX_ERROR_MESSAGE_CHARS]` inline. Migration is idempotent — existing `~/.memtomem/proxy_metrics.db` files migrate transparently on next start, no operator action required. Operator-visible only; no surfacing change in MCP tool responses (the same text already reached the caller via `ToolError(original_text)` for UPSTREAM_ERROR or via re-raised exception for the others).

### Fixed

- **Per-tool `ToolSurfacingConfig.min_score` now takes precedence over the auto-tuner** (#247) — `surfacing.context_tools.<name>.min_score` was silently ignored whenever `auto_tune_enabled=true` (the default), because the engine consulted `AutoTuner.get_effective_min_score(tool)` first and that path only knew about the global `self._config.min_score` or a learned `_adjustments[tool]`, never the per-tool override. An operator who pinned `min_score=0.1` on a noisy tool saw the filter continue to fire at the 0.02 global default (or whatever the tuner had moved to inside `[0.005, 0.05]`). Precedence is now explicit — highest wins: (1) per-tool override, (2) auto-tuned value, (3) global default — and when a per-tool override is set the tuner's `maybe_adjust` is skipped for that tool so it does not learn a value that will never be applied. The same fix changes the truthy check (`tool_cfg.min_score`) to `is not None` on the auto-tune-disabled path, so an explicit `min_score=0.0` override is honored instead of falling through to the global default. New `TestPerToolMinScoreOverride` in `tests/test_surfacing_engine.py` pins all three precedence cases.
- **`mms list` / `mms health` distinguish missing config from empty config** (#250, closes #221) — both commands previously printed the same `"No upstream servers configured."` message whether the config file didn't exist (user pointing at a wrong `--config` path) or existed but had `upstream_servers: {}` — leaving troubleshooters with no signal which case they were in. They now mirror `mms status`'s text branch — `"Config not found: <resolved>"` plus a `mms add` (or `mms init`) hint — when the file is missing, and keep the existing empty-state message for present-but-empty configs. `mms health --json` likewise switches from the ambiguous `{"servers": {}}` shape to `{"error": "config_not_found", "path": "..."}` for missing-config, matching `status --json` / `list --json` (the latter was already correct since #220 — `health --json` had been overlooked). `status` was untouched — already handled this correctly and is the pattern this PR mirrors.
- **`mms health` no longer pollutes stderr with MCP SDK tracebacks; `error` field shows the real cause** (#251) — two compounding gaps in every probe path (`health`, `add --validate`, `init`'s import flow, `--from-clients`). The MCP stdio SDK calls `logger.exception("Failed to parse JSONRPC message from server")` on every non-JSON line an upstream emits, so an `echo`-typed entry (or any process that exits before speaking JSON-RPC) used to dump multi-line pydantic `ValidationError` tracebacks per line — corrupting downstream `jq` / `json.loads` consumers of `health --json`, and adding noise even with stderr separated. A `contextmanager` (`_silenced_mcp_sdk_logs`) scopes the silence to the probe and restores the prior log level on exit, so the runtime proxy's own diagnostic logging is untouched. Separately, probe failures bubble out of anyio's `TaskGroup` as an `ExceptionGroup`, so `str(exc)` returned the wrapper string `"unhandled errors in a TaskGroup (1 sub-exception)"` — the actual cause (`"Connection closed"`, `"Invalid JSON: …"`) sat at `exc.exceptions[0]`. New `_root_cause_message` walks `BaseExceptionGroup` recursively (with a cycle guard, falling back to the type name on empty `str()`) and surfaces the leaf in the `error` field. Catch surface stays at `except Exception` — `ExceptionGroup` is already an `Exception` subclass so the unwrap reaches it without expanding to `BaseException` (which would swallow `CancelledError` / `KeyboardInterrupt` from a probe).
- **`mms list` TRANSPORT column widened to fit `streamable_http`** (#252) — the column was sized at `<12` chars, but `streamable_http` is 15 chars, so HTTP-typed upstream rows pushed the COMPRESSION and COMMAND/URL columns three chars left of the header. `<16` fits every value the `--transport` Choice accepts (`stdio`, `sse`, `streamable_http`) with at least one space of padding. Regression test pins `header.index("COMPRESSION") == row.index("auto")` for a `streamable_http` row.

### Docs

- **`docs/configuration.md` surfaces `connect_timeout_seconds` and three v0.1.12 timeout fields** (#248, #249) — two consecutive doc-drift fixes against `docs/configuration.md` for fields that shipped without reference-doc entries: (1) `proxy.upstream_servers.<name>.connect_timeout_seconds` (the per-server upstream connect/handshake budget introduced alongside #206/#207/#210), and (2) the v0.1.12 timeout trio (`call_timeout_seconds`, `overall_deadline_seconds`, `compression.llm.llm_timeout_seconds`). Each entry pins the default, the unit, and the failure mode (`ErrorCategory.TIMEOUT` recorded in `proxy_metrics`). Both follow the audit pattern from #237 — CHANGELOG-field-grep then example-block verify — and add a `tests/test_docs_sync.py` regression test pinning the field name to its source-of-truth (config schema or CHANGELOG entry) so the same omission can't silently reappear.

## [0.1.17] — 2026-04-24

### Added

- **`mms prune` + `mms init --prune-originals` collapse dual-registration** (#241) — `mms init --mcp claude` (and `mms add --import` without `--prune`) intentionally leaves source-client registrations in place, so upstreams end up registered both directly in the source client and through STM. Tool calls bypass STM's compression, caching, and LTM surfacing whenever the direct path is taken. Two opt-in entry points now collapse the dual path, both built on the existing `_handle_source_prune` / `_prune_imported_candidates` pipeline from #226: (1) `mms prune [NAMES...] [--all] [--yes] [--dry-run]` is a standalone post-hoc pruner — refuses to run without explicit scope (`--all` or `NAMES`), aborts on unknown names before any writes, requires `--yes` on non-TTY, exits non-zero with a per-row manual-command fallback on partial failures; (2) `mms init --prune-originals` is the opt-in flag for scripted onboarding, with a single y/N prompt (default No) on TTY. Non-TTY without the flag preserves the #203 hint-only default (regression-guarded). `_find_dual_registered` matches on name **plus** `_server_signature` (mirroring `_add_from_clients` dedup in the opposite direction); a source-client entry sharing a name with an STM upstream but wired to a different command / URL is rejected with a "divergent identity" error rather than clobbered. The preview surfaces each source entry's command or URL via `_format_candidate_detail` so users can verify what will be removed before consenting. STM's own `stm_proxy.json` is never touched; only source-client files change. `mms init` remains bootstrap-only — the new flag adds a post-save step, not a re-entry into the wizard.

### Internal

- **`init_langfuse` drops three `type: ignore[union-attr]` via walrus** (#239) — `init_langfuse` takes `config: object` (duck-typed so any settings source can be passed), so mypy can't prove `config.public_key` / `.secret_key` / `.host` exist after a `getattr(config, name, "")` truthy check — the check narrows a local, not the attribute on `config`. Folded each guard-plus-usage pair into a single walrus assignment (`if public_key := getattr(config, "public_key", ""):`) so the checked value is also the one assigned, dropping all three `# type: ignore[union-attr]` suppressions. No runtime behavior change: each attribute is read exactly once, kwargs contains the same keys for the same inputs, and falsy-skip semantics ("" / None / missing) are preserved. Keeps the `config: object` duck-typing contract — no import of a concrete `LangfuseConfig` type, no `isinstance` narrowing branch.

### Docs

- **`docs/cli.md` catches up v0.1.9–v0.1.16 reference drift** (#237) — the v0.1.8 docs audit closed before v0.1.9 shipped; eight point releases later four user-facing changes were live in CHANGELOG but had never been mirrored into the per-feature reference docs. Synced: v0.1.12 #213 `stm_progressive_stats` brings the observability-tool count 6 → 7 (`docs/configuration.md` prose + `tests/test_server_tools.py` comment); v0.1.14 #227 `mms add --import --prune` flag documented (`docs/cli.md` options table + paragraph + example); v0.1.15 #231 proxied tool `annotations.title` `[server]` tagging explained (`docs/cli.md` §MCP Tools); v0.1.16 #234 `LangfuseConfig` fail-fast on missing `[langfuse]` extra surfaced (`docs/configuration.md` §Langfuse). No code changes — the sibling test-comment fix updates a stale literal next to the correct `_OBSERVABILITY_TOOLS` set.
- **`CONTRIBUTING.md` pytest command matches CI + `docs/cli.md` flags macOS-only Claude Desktop discovery** (#238) — two small doc drifts fixed together with regression tests pinned to the respective sources of truth, so they can't silently reappear. CONTRIBUTING previously quoted `pytest -m "not ollama"` while CI's `test` job runs `pytest -m "not ollama and not bench_qa_meta and not bench_qa_llm_judge"` — new contributors hit the `bench_qa_meta` self-tests (intentional failures per marker design) and `bench_qa_llm_judge` scenarios (require `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`), both looking like real regressions. `docs/cli.md` described the `mms add --import` Claude Desktop scan as "(OS-appropriate)" — but the discovery helper `_desktop_config_path()` is macOS-only (v0.1.13's #219 only fixed the paste hints, not discovery); Linux/Windows callers silently saw zero Desktop candidates. New `tests/test_docs_sync.py::test_contributing_pytest_command_matches_ci` extracts the CI filter with a regex and asserts CONTRIBUTING quotes it; `test_cli_docs_flag_desktop_discovery_is_macos_only` checks the helper remains macOS-only and if so requires `docs/cli.md` to keep a `macOS[- ]only` caveat.
- **`docs/cli.md` `list` subcommand section added to fix broken #list anchor** (#240) — L74 referenced `` [`list`](#list) `` from the `init` description but no matching `### list` heading existed, only a `mms list` example inside the shared Examples block. Added a `### list` section between `### add` and `### Examples`, matching the `register` / `health` pattern: Usage block plus a one-paragraph description covering read-only behavior, `--json` output shape, and the `config_not_found` JSON branch scripting callers can key off. Scope is limited to the one broken anchor surfaced by an internal link audit (12 tracked `.md` files scanned; this was the sole finding).

## [0.1.16] — 2026-04-22

### Fixed

- **`LangfuseConfig.enabled=true` now fails fast when the `langfuse` package is not installed** (#234, closes #233) — `LangfuseConfig` already rejects `enabled=true` without `public_key` / `secret_key` at config-load time via `_require_keys_when_enabled`. The parallel environment check was missing: a user who had all three fields set but didn't install the `[langfuse]` extra (e.g. after `uv tool install --reinstall memtomem-stm` dropped the extras) hit a silent `except ImportError: pass` branch in `server.py`, so the proxy started cleanly but produced no traces and surfaced no warning. A second `@model_validator(mode="after")` now probes `importlib.util.find_spec("langfuse")` whenever `enabled=true` and raises `ValueError` with an install hint (`uv tool install --reinstall 'memtomem-stm[langfuse]'` or `pip install 'memtomem-stm[langfuse]'`). Symmetric with the existing key-requirement validator — schema and environment are both checked at load time instead of one failing loud and the other failing silent. No behavior change for users with `enabled=false` (the new validator short-circuits before probing), or for users who already have the extra installed.

## [0.1.15] — 2026-04-22

### Changed

- **Proxied tool `annotations.title` is tagged with its source server** (#231) — MCP clients such as Claude Code's `/mcp` picker display `annotations.title` in place of the tool `name` when it is set. Upstream servers that populate `title` (e.g. Playwright's "Close browser") previously appeared unattributed in the picker, while servers that left it blank fell back to the prefixed `name` (e.g. `Context7__resolve-library-id`) — the same STM-hosted tool looked attributed or unattributed depending purely on whether the upstream author set `title`. The proxy now copy-on-writes `annotations.title` to `"[{server}] {original title}"` (e.g. `"[playwright] Close browser"`) so the source server is always visible in the picker. Invocation `name` (`playwright__browser_close`), input schema, description, and all other annotation hints (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) are unchanged — this is a display-layer change only, with no effect on how agents call the tool. When upstream `title` is absent/empty or annotations are `None`, the original value is returned unchanged (clients fall back to the already-prefixed `name`, preserving attribution).

## [0.1.14] — 2026-04-22

### Added

- **`mms add --import --prune` + interactive TTY confirm prompt** (#227, closes #226) — after a successful import, prune the direct registration from each source MCP client so tools are reachable via STM only. `--prune` runs unconditionally (scripted callers); without the flag, a TTY prompt (default **No**) lists the exact `(name, source)` pairs before writing. Non-TTY callers without `--prune` keep the existing #203 hint-only behavior — no silent auto-prune. Writer surface: `claude mcp remove <name> -s <scope>` for Claude Code user/local/project scopes, atomic JSON rewrite (`atomic_write_text`) for Claude Desktop. Prune failures are non-fatal: the import stays, a per-entry warning surfaces the error, and the exact manual command from `_source_removal_hint` is printed. `duplicate_in` sources (a candidate registered in more than one client) are pruned from every source, not just the primary. `--prune` without `--from-clients` is a `UsageError` rather than a silent no-op.

### Changed

- **Proxied tools now advertise before STM utility tools in `tools/list`** (#229, #228 phase 1) — STM utility tools register at module import (via `@mcp.tool()` / `@_obs_tool`); proxied tools register later, inside `app_lifespan`. FastMCP's insertion-ordered `_tool_manager._tools` previously yielded STM utility tools first, pushing the domain tools users actually reach for (`fs__…`, `gh__…`, `langchain__…`, etc.) to the bottom of pickers that preserve server order (Claude Code `/mcp`). After proxied registration, `_move_stm_tools_to_end` pops and reinserts each STM utility entry so proxied tools lead the advertise list. Missing entries (obs tools hidden by `MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=false`) are skipped safely. Tool set, names, schemas, annotations, and the flag semantics are unchanged; only the order flips. Phases 2 (flag default flip) and 3 (mem-do-style grouping) from #228 remain open.

### Fixed

- **`_detect_install_type` registers the `memtomem-stm` server entrypoint, not the `mms` click group** (#225) — the dev-checkout / user-project branch emitted `uv run --directory <root> mms`, but `mms` is the click CLI group (`memtomem_stm.cli.proxy:cli`): invoked with no subcommand it printed help and exited 0, closing the MCP stdio pipe before `initialize` and the client reported "Failed to reconnect to memtomem-stm". Both branches now land on the actual server entrypoint (`memtomem_stm.server:main`). `mms init` / `mms register` now produce a working config in source-checkout and `uv add memtomem-stm` project setups out of the box; prior broken configs need to be re-run through `mms register` (or hand-edited) to pick up the fix.

## [0.1.13] — 2026-04-21

### Added

- **`mms init` auto-registers with Claude Code + new `mms register` command** (#216) — `mms init` now ends with a 3-way MCP registration prompt (Claude Code auto-register / emit `.mcp.json` snippet / skip), mirroring parent `mm init`. The new `mms register` command re-runs the same prompt post-init without re-entering `init` (preserves the bootstrap-only invariant). Install-type detection picks between `uv run --directory <root> mms` (source checkout / `uv add memtomem-stm` project) and bare `memtomem-stm` (global install).
- **`--mcp claude|json|skip` flag on `mms init` / `mms register`** (#217) — pre-answers the 3-way registration prompt for scripted / CI callers, matching parent `mm init --mcp` shape. Fixes a regression from #216 where piped-stdin callers who didn't feed an extra line hit `click.Abort`. `--mcp claude` on an existing registration defaults to 'keep' (non-destructive, no prompt).
- **`mms --version` flag** (#220) — idiomatic Click entry point alongside the existing `mms version` subcommand (kept for backwards compatibility). Both paths emit the same `memtomem-stm X.Y.Z` line, so scripts that grep the version string don't care which they invoke.
- **`mms list --json`** (#220) — scriptable JSON output for the server list, mirroring the shape of `mms status --json` (`{config_path, servers}`; missing config returns `{error: "config_not_found", path}`). Closes the parity gap where `status` and `health` already supported `--json` but `list` required parsing the text table.

### Changed

- **`_detect_install_type` now does a `tomllib` parse + PEP 508 name extraction** (#218) — replaces the earlier `'"memtomem-stm' in content` prefix match, which would have false-positived on neighbor packages like `memtomem-stm-bundle` or on unrelated comments mentioning the name. Behavior unchanged for the three shipped happy paths; hardening for edge cases before the first incident.
- **`mms health --json` pretty-prints (indent=2, ensure_ascii=False)** (#222) — matches `mms status --json` and `mms list --json`. Previously emitted compact one-liners, leaving `health` as the odd one out when piping multiple `--json` commands through the same formatter. Shape unchanged; parsers that ignore whitespace are unaffected.

### Fixed

- **OS-appropriate Claude Desktop config path in `mms init` / `mms register` paste hints** (#219) — previously hardcoded the macOS path (`~/Library/Application Support/Claude/claude_desktop_config.json`) for all platforms. Now routes on `sys.platform`: Linux → `~/.config/Claude/claude_desktop_config.json`, Windows → `%APPDATA%\Claude\claude_desktop_config.json`. Cursor / Windsurf / Gemini targets are unchanged (cross-platform by design).

## [0.1.12] — 2026-04-20

### Added

- **`stm_progressive_stats` MCP tool + `progressive_reads` telemetry table** (#213, closes #204 PR #2) — every progressive initial chunk and every `stm_proxy_read_more` follow-up persists one row (`key, trace_id, server, tool, offset, chars, served_to, total_chars, created_at`) in `~/.memtomem/stm_feedback.db`. Aggregates surface as total reads, distinct responses, follow-up rate, avg chars served, avg total chars, avg coverage, and a per-tool breakdown — parity with `stm_compression_stats` / `stm_surfacing_stats`. Writes are fire-and-forget (tracker swallows exceptions) so telemetry outages cannot affect response delivery. Past-end `read_more` calls that return the `"(no more content)"` sentinel are deliberately skipped so they don't inflate follow-up rate. Opt out via `proxy.progressive_reads.enabled=false`. Unblocks stratified analysis of nudge strength vs. follow-up rate across compression strategies.
- **`trace_id` threaded through progressive delivery** (#205, closes #204 PR #1) — `_apply_progressive` / `ProgressiveResponse` / `stm_proxy_read_more` now carry the originating call's `trace_id`, so the follow-up span is filterable in Langfuse as a cohort with the initial `proxy_call` turn. The correlation is metadata-tag based (both spans carry the same `trace_id` attribute) rather than trace-tree merging — the two MCP turns run in separate OTel contexts, so `proxy_call_read_more` stays a root span. Closes the one call-pipeline path that Langfuse could not correlate to its originating call.
- **Upstream `call_timeout_seconds` + `overall_deadline_seconds` for proxy calls** (#206) — every upstream MCP tool invocation is now wrapped in a configurable per-call timeout, and the outer compression/surfacing pipeline honors a total-call deadline so a slow upstream cannot hang the agent past the budget. Timeouts are recorded as `ErrorCategory.TIMEOUT` in `proxy_metrics`. Defaults err on the lenient side so existing deployments don't regress; tighten per-upstream as you dial in.
- **Timeout-bound LLM compression stage** (#207, #210) — the `LLM_SUMMARY` compressor now respects `compression.llm.llm_timeout_seconds` (default 60s). A slow or hung LLM endpoint would previously freeze the pipeline *after* the upstream had already responded — outside the upstream `call_timeout_seconds` introduced in #206. On timeout the compressor falls back to `TruncateCompressor`, matching the existing LLM failure modes (privacy / circuit breaker / llm_error).
- **Exception barrier around `mcp.run()`** (#209 Part A, #212) — wraps the top-level FastMCP event loop so an unhandled exception in a tool handler or transport callback logs + exits cleanly instead of leaving the stdio subprocess in a half-dead state. The earlier failure mode (connection reset, broken pipe on shutdown) is now a loud terminal log with a traceback. Part B (periodic ping) remains wait-for-signal.
- **`MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=false` hides STM's observability MCP tools** (#201) — set the env var to drop the seven operator-facing tools (`stm_proxy_stats`, `stm_proxy_health`, `stm_proxy_cache_clear`, `stm_surfacing_stats`, `stm_compression_stats`, `stm_progressive_stats`, `stm_tuning_recommendations`) from the MCP `tools/list` surface, reducing upfront schema tokens on eager-loading MCP clients (e.g. OpenAI Codex CLI) that don't lazy-load tool schemas the way Claude Code does. The hidden tools remain importable as Python functions for tests and internal callers, but are not registered with the MCP server while hidden. The four model-facing tools (`stm_proxy_read_more`, `stm_proxy_select_chunks`, `stm_surfacing_feedback`, `stm_compression_feedback`) stay advertised regardless. Default `true` preserves existing behavior — opt in only if your client eager-loads. Env var only in this release; matching `STMConfig.advertise_observability_tools` field is present for type-checking but JSON-file configuration would require a registration refactor and is deferred until there's demand.
- **`mms add --from-clients` (alias `--import`) bulk-imports from MCP clients post-init** (#200) — reuses init's discovery + TUI flow so additional servers added to Claude Desktop / Code / project `.mcp.json` after initial setup can be pulled in interactively, without editing JSON by hand or calling `mms add` once per server. Filters candidates two ways before prompting: by name (skips `foo` if a server named `foo` already exists) and by `(transport, command, args)` / `(transport, url)` signature (skips duplicates registered under a different name). When all discovered servers are already registered, exits cleanly with a no-op message instead of an empty selection screen. `--prefix` is suggested from the upstream name and de-duped against prefixes already in the config. Incompatible with `NAME` / `--prefix` / `--command` / `--args` / `--url` / `--env` — those are for the single-server manual path; passing both raises a usage error rather than silently ignoring one. Works with `--validate` and `--timeout` to probe only the selected subset.

### Changed

- **Bounded lock acquisition helper** (#208, #211) — internal refactor of `ProxyManager`'s async locks (selective compressor, LLM compressor, extractor) behind a `bounded_lock()` helper with a configurable `lock_timeout_seconds` (default 30s). Timeout raises `LockTimeoutError` → recorded as `ErrorCategory.LOCK_TIMEOUT` in `proxy_metrics`, distinct from upstream `TIMEOUT` (a stuck lock indicates an internal bug, not a slow dependency). No external API change; deployments that never saw a lock-holding bug see no difference.

### Fixed

- **`mms add --import` dual-registration warning** (#202, #203) — when a candidate is already registered under a different name via the `(transport, command, args)` / `(transport, url)` signature check, the import now emits a clear WARNING naming both entries rather than silently skipping. Read-only w.r.t. source-client config (STM discovery never writes back); the warning is a hint for the operator to prune manually if desired.

## [0.1.11] — 2026-04-20

### Added

- **`stm_surfacing_stats` MCP tool enriched with parity to `stm_compression_stats`** (#198, closes #197) — output now includes `events_total`, `distinct_tools`, `date_range`, per-tool breakdown (events + average memory count, sorted descending), `rating_distribution`, `total_feedback`, helpfulness percentage, and a DESC-ordered `recent` tail with 80-character query previews. New optional inputs `since` (ISO-8601) and `limit` (default 10) restrict the window. Empty-DB / out-of-range filters return zeros with all collections present, so callers don't branch on shape. Malformed `since` is rejected with a clean error rather than raising. Closes the long-standing observability gap where surfacing analytics required raw SQL against `~/.memtomem/stm_feedback.db` while compression already had an aggregate tool.

## [0.1.10] — 2026-04-20

### Added

- **`mms init` imports MCP servers from existing clients** (#194) — scans `./.mcp.json`, `~/.claude.json` (user + per-project scope), and Claude Desktop's macOS config, then offers a TUI multi-select (Enter toggles, scroll to Confirm; ↑↓ / j/k / Ctrl+N/P all supported). For each pick the user only confirms a prefix — transport/command/args/url/env are imported as-is. Self-reference filter blocks `mms` / `memtomem-stm` / `memtomem` / `memtomem-server` entries (including `uvx --from memtomem …` shape) so users can't accidentally proxy STM through itself or double-register the LTM companion. Dangerous env keys (`LD_PRELOAD`, `NODE_OPTIONS`, etc.) are stripped during import, matching `mms add --env` policy. Non-TTY / `MMS_NO_TUI=1` / piped stdin fall back to a comma-number prompt so CI and scripted installs still work. Adds `questionary>=2.0` runtime dep.
- **`mms init` surfaces `--config` management hints on non-default paths** (#195) — after saving to a path other than `~/.memtomem/stm_proxy.json`, the output now prints `mms list --config <path>` / `mms health --config <path>` so subsequent management commands don't silently read the empty default config. Reported during dogfooding with throwaway `/tmp/*.json` test paths.

## [0.1.9] — 2026-04-19

### Added

- **Background auto-indexing** (F4) — `auto_index.background` (default `false`). When set `true`, Stage 4 INDEX runs via `asyncio.create_task` off the request path; the agent receives a `[Indexing…] · scheduled` placeholder footer immediately while indexing proceeds in the background. Trade-off: read-your-own-writes consistency is no longer guaranteed until the task completes — opt in only if agents tolerate the gap. Metrics row records `index_ok IS NULL` / `index_error IS NULL` / `chunks_indexed = 0` (tri-state matching background extraction); dashboards filter background rows with `WHERE index_ok IS NULL`. Default `false` preserves the synchronous contract for every existing deployment.
- **`PROGRESSIVE_FOOTER_TOKEN` — canonical split token for progressive chunks** (issue #160). Exported from `memtomem_stm.proxy.progressive` as the exact prefix (`"\n---\n[progressive: chars="`) that agents stitching sequential `stm_proxy_read_more` responses should split on, instead of the weaker `"\n---\n"`. The `[progressive: chars=` suffix is a sentinel that does not appear in natural prose; splitting on the three-char delimiter alone silently drops bytes when content contains markdown horizontal rules, YAML frontmatter fences, or other `---` sequences. Non-breaking: the footer wire format is unchanged. Regression tests cover markdown HR, YAML frontmatter, `\n---\n[` lookalike brackets, and content that ends in `\n---\n` immediately before the footer; an additional test pins the exact legacy failure mode so a future refactor cannot silently regress to `split("\n---\n")[0]`. Agent-side contract documented in `docs/pipeline.md` § Stage 3.
- **CLI: `mms version` + `mms status --json`** (#152) — dedicated version subcommand and scriptable JSON status output for tooling / CI.
- **CLI: `mms health`** (#155) — per-upstream MCP connectivity checks with actionable diagnostics.
- **CLI: `mms init` + `mms add --validate`** (#157) — first-time setup workflow (scaffold config, validate upstream on add).
- **CLI: colorized output** with `NO_COLOR` honored (#158).
- **INDEX / EXTRACT pipeline outcome metrics** (#159) — per-call success/failure surfaced alongside existing CLEAN/COMPRESS/SURFACE columns.
- **Optional deterministic `trace_id` on `call_tool`** (#173) — opt-in kwarg for reproducible traces (bench harnesses, golden tests).
- **Parent LTM hints forwarded to operator observability** (#191) — upstream hint payload exposed on surfacing spans for diagnostics (operator-only; downstream prepend text unchanged).

### Changed

- **Progressive delivery surfaces memories for users who opt in via `injection_mode`** (F6). The default `injection_mode` stays `prepend`, which **continues to bypass surfacing on progressive** (upgrading is a no-op for default deployments). Operators who set `injection_mode` to `append` or `section` now get Stage 3 (SURFACE) on progressive responses; `prepend` would shift `stm_proxy_read_more` offsets and stays skipped with a one-time WARNING. See `docs/pipeline.md` § Stage 3 and `tests/test_progressive.py::TestProgressiveContentIntegrity::test_concat_invariant_under_surfacing` for the empirical safety proof.
- `CallMetrics.surfacing_on_progressive_ok` / `surface_error` (schema-provisioned by v0.1.8) now populate on the progressive path: `True`/`False` when surfacing ran, `None` when skipped (non-progressive call, no engine, or `prepend` mode).

### Fixed

- **`metrics_store` read-path defensive lock** (#166) — three read methods wrap the write-path lock so a future move to `run_in_executor` cannot silently introduce torn reads.
- **CLI: reject non-dict JSON configs** with a clean error (#156); **duplicate prefix warning** now clearly states the operation proceeds (#154).
- **Notebook 05** — correct `mms add` invocation (#184), echo fixture refs, notebook 00 count (#181); **notebook builder reconciled** with post-commit direct edits from #150 (#182).
- **README CI pytest filter** aligned with workflow (#185); **pipeline / custom-integration** line references refreshed (#179); **bench trace prefix link** in `operations.md` (#183).

### Testing

- **1465 tests** (up from 1364).
- **`bench_qa` LLM-behavior harness** (#168-#178) — 10 scenarios (S1-S10) covering happy paths, fallback ladder (S1/S6/S8), progressive round-trip, selective TOC demotion (S7), surfacing recall@k smoke (S10), 40-turn chat skeleton (S5); deterministic `trace_id` two-run gate; LLM-as-judge advisory scoring (opt-in, `gpt-4.1-nano` default); self-test probes; CI advisory job with frozen JSON/Markdown reports. See `tests/bench/bench_qa/README.md` and `docs/bench_qa.md`.
- **Contract test for empty-structured JSON** from upstream (#190) — alpha-upstream loose pin; stable invariants asserted, new fields read via `data.get()`.

### Docs

- **README rewrite** — user-benefits framing + improved CLI help text (#153).
- **Alpha banner** above tagline (#188).
- **Docs restructure** — WIP/internal guides moved to private `memtomem-docs` repo to minimize beginner barrier (#186).
- **Notebooks slimmed to `01_quickstart` only** — 00 + 02-05 moved to private repo (#187).
- **`bench_qa` reference + scenario-adding guide** (#180).

### Internal

- **Remove unused `SelectiveCompressor` import** (#163).
- **Ignore local `.env` + `.mcp.json`** (#192).

## [0.1.8] — 2026-04-16

### Added

- **`MEMTOMEM_STM_LOG_LEVEL` env var** (#149) — proxy-wide log level control documented end-to-end.
- **`max_upstream_chars` OOM guard** (#118) — reject upstream text exceeding the configured cap before compression.
- **BM25 multilingual tokenizer** (#94) — Cyrillic, Arabic, Devanagari, Thai added alongside Latin/CJK.
- **Compression feedback lifecycle** — `stm_surfacing_feedback` invalidates cache on `not_relevant` / `already_known` (#148).

### Changed

- **Centralised SQLite PRAGMA tuning** (#96) across all long-lived stores (shared helper).
- **SurfacingCache → insertion-ordered FIFO eviction** (#95), matching `_boosted_event_ids` pattern.
- **Config precedence honoured end-to-end: env > file > defaults** (#106 / #116).
- **Constants refactored to module-level `_UPPER_SNAKE_CASE`** (#87); hot-path regex hoisted to module level (#112); `atomic_write_text` centralised (#121).

### Fixed

- **Concurrency / lifecycle audit** (15 PRs) — init-failure SQLite connection cleanup (#127, #141), config hot-reload mtime preservation on parse failure (#128), reconnect `conn_stack` unwind (#130), lifespan cleanup when init fails before yield (#131), `MCPClient.start()` context unwind (#129), `FeedbackTracker` degrade-not-crash on SQLite failure (#124), boost-guard race in `handle_feedback` (#133), `_surfaced_ids` dedup-claim race (#134), surfacing cache stampede (#137), proxy `call_tool` cache stampede (#139), `_background_tasks` drain loop (#135), `RelevanceGate` burst race (#138), `LLMCompressor.close()` drain of in-flight `compress()` (#140), `_trace_id` cache-key taint (#136), swallow `cache.set` failures to preserve response (#120), reconnect delay ordering validation (#132).
- **Upstream Phase 2 robustness** — tolerate spec-noncompliant `result.content=None` (#114) and `result.text=None` (#119); MCP adapter text-None guard + WAL journal growth cap (#145).
- **Pipeline failure guards (F1/S1)** — auto-index stage failures no longer kill the agent response; untracked `surfacing_id` no longer injected when `record_surfacing` fails; inner handlers now log with `exc_info=True` (#149 cross-link).
- **Config validation** — pydantic `Field` / `Literal` constraints prevent unsafe values (#109); reject empty `api_key` for openai/anthropic at load time (#123); atomic JSON write for `stm_proxy.json` via temp + `os.replace` (#115).
- **Privacy detection** — expanded default credential patterns + email TLD regex (#111).
- **Defensive parsing** — LLM provider response payloads (#126, #67/#80), embedding provider input-order preservation when `index` omitted (#68/#81), numeric parsing of untrusted external input (#66/#79), backward search room for small spans in `_find_boundary` (#69/#82).
- **Memory caps** — `TokenTracker` per-server/tool counters bounded (#70/#83); `_boosted_event_ids` FIFO eviction (#110/#113).
- **MCP client** — `asyncio.TimeoutError` added to transport errors (#52); configurable `session.initialize()` timeout (#53).
- **Metrics** — `INTERNAL_ERROR` row recorded when pipeline raises (#117); demoted expected-fallback warnings away from `exc_info` trace dumps (#102).
- **Surfacing / compressor lifecycle gaps** — consolidated fixes across store and compressor shutdown paths (#143).

### Testing

- **1364 tests** (up from 1033).
- New coverage — `CliRunner` for `cli/proxy.py` (#100), privacy invalid-regex / hot-reload / empty-patterns (#97), `SurfacingEngine` webhook exception / cancel (#98), `MCPClient` reconnect + version negotiation edges (#99), `auto_index` / `extract_and_store` / `format_fact_md` (#101), provider-aware `embedding_base_url` defaults (#54/#86), `RelevanceScorer` hot-reload (#62/#85), `LLMCompressor` singleton + close lifecycle (#61/#84), `CircuitBreaker` `time_until_reset` + backward-compat alias (#39), cleaning-stage injection detection.

### Docs

- **Full docs audit — 11 files + 2 notebooks** (#150) — line references, failure-guard cross-links, log-level documentation across operations / configuration / pipeline / surfacing.
- **Observability verification + custom integration guide** (#149).
- **Compression before/after examples** for strategy docstrings (#23/#125).
- **Drift catch-up** for PRs #54-#65 (#72/#105); auto-tune feedback direction clarified (#57/#104); `OPENAI_API_KEY` requirement documented for OpenAI embedding provider (#56/#103).

## [0.1.7] — 2026-04-13

### Added

- **Phase 2 `StructuredResultParser`** — structured JSON surfacing result format activated end-to-end (scaffolded in v0.1.6 behind `SurfacingConfig.result_format="structured"`).
- **Format negotiation via `mem_do(action="version")`** — surfacing engine negotiates compact vs. structured format with the upstream at start-up based on core server capability.

## [0.1.6] — 2026-04-13

### Observability

- **Langfuse spans for surfacing tools** — `stm_surfacing_feedback`, `stm_surfacing_stats`, and `surfacing_feedback_boost` (access count increment sub-span) are now wrapped in Langfuse observations
- **Upstream trace context propagation** — proxy forwards `_trace_id` as a reserved field in upstream MCP call arguments for end-to-end distributed tracing; `McpClientSearchAdapter` also accepts `trace_id` for LTM calls
- **End-to-end trace_id threading** — `SurfacingEngine.surface()` → `_do_surface()` → `mcp_adapter.search()`/`scratch_list()` now thread `trace_id` from the proxy call through the full surfacing pipeline

### Internal

- **Strategy-based parser for Phase 2** — `_parse_results` refactored into `CompactResultParser` / `StructuredResultParser` strategy classes with `get_parser()` factory; backward-compatible `_parse_results` static method delegates to compact parser. `SurfacingConfig.result_format` field added (`"compact"` default, `"structured"` reserved for Phase 2)
- **Extract `tool_metadata` and `memory_ops` from `manager.py`** — independent logic split into `proxy/tool_metadata.py` (62 LOC) and `proxy/memory_ops.py` (180 LOC); `manager.py` reduced from 1,333 → 1,179 LOC
- **Fix all ruff warnings** — 16 pre-existing lint issues in `tests/` cleaned up (F841, E741, F401)

### Testing

- 1033 automated tests (up from 975), +1 xfail for Phase 2 structured format
- New `test_proxy_manager_lifecycle.py` — 8 tests for start/stop/double-start guard
- New `test_proxy_manager_pipeline.py` — 15 tests for compression, surfacing, indexing, chunks, read_more
- New `test_server_tools.py` — 22 tests for all 10 MCP tool handlers + lifespan
- 7 observability tests for surfacing spans and trace propagation
- 7 parser strategy contract tests + 1 xfail Phase 2 snapshot

### Docs

- New notebook 05 — Observability and Langfuse Tracing (3 layers: MCP tools, SQLite, Langfuse)
- Updated operations.md span table with surfacing tool spans and trace propagation docs
- Updated notebook count in README and notebooks/README (4 → 6)

## [0.1.5] — 2026-04-12

### Critical

- **Fix `mem_search` response parser for real core format** — the `_parse_results()` regex expected a `--- [score] source ---` format that no real `memtomem-server` ever produced; core's actual compact output (`[rank] score | source > hierarchy`) was collapsed into a single garbage result with wrong score. Rewritten to match core's `_format_compact_result` output. The fake test server now emits the real format so integration tests validate the production parsing path.
- **Forward `context_window` to MCP call** — `context_window` configured in `SurfacingConfig` was silently swallowed by `**kwargs` and never sent to the core server. Now forwarded as an explicit parameter.

### Fixes

- Normalize `list[str]` namespace to comma-separated string before MCP call (core's `mem_search` accepts `str | None`; `NamespaceFilter.parse()` handles comma-separated values)
- Widen source-file regex from `.md`-only to any file extension
- Widen namespace badge regex to support hyphens, dots, and other non-word characters
- Fix mypy errors in `observability/tracing.py` — `Langfuse(**kwargs)` kwargs typed as `dict[str, Any]` instead of `dict[str, str]`

### Testing

- 975 automated tests (up from 963)
- New `test_core_format_contract.py` — 12 contract tests with snapshots of core's real formatter output (namespace badge, context window position, non-.md sources)
- Verified end-to-end against real `memtomem-server` v0.1.7

## [0.1.4] — 2026-04-12

### New Features

- **Compression ratio guard** (#20) — post-compression check detects when a strategy cuts below the dynamic retention floor; records `compression_strategy` and `ratio_violation` in `proxy_metrics.db`
- **Compression feedback tool** (#21) — `stm_compression_feedback` lets agents report information loss from compressed responses; `stm_compression_stats` shows aggregated feedback counts by kind and tool
- **Ratio guard fallback** (#22) — boundary-aware truncate fallback when compression overshoots the retention floor
- **3-tier fallback ladder** — progressive → hybrid → truncate; new hybrid tier preserves document structure (head + TOC) for content with ≥ 3 headings that is too small for progressive chunking
- **Per-tool `retention_floor`** — override the global dynamic retention scaling per server or per tool via `stm_proxy.json`
- **Compression auto-tuner** — `stm_tuning_recommendations` MCP tool analyses proxy metrics and produces per-tool recommendations (budget increase/decrease, strategy pinning, feedback-driven strategy changes)
- **Nested Langfuse sub-spans** — `proxy_call_clean`, `proxy_call_compress`, `proxy_call_surface`, `proxy_call_index`, `proxy_call_cache_hit` nested under the top-level `proxy_call` observation via OpenTelemetry context propagation
- **Langfuse sampling** — `MEMTOMEM_STM_LANGFUSE__SAMPLING_RATE` (0.0–1.0) to control tracing volume; metrics recording is never affected by sampling

### Improvements

- LLM fallback signal in `compression_strategy` metric (e.g. `llm_summary→privacy_fallback`)
- Embedding scorer fallback count exposed in `CallMetrics.scorer_fallback` + DB column
- SKELETON `body_trimmed_chars` metadata in compression footer
- Convention suffix in proxied tool descriptions for progressive/selective delivery
- Startup warning when `compression != none` but `auto_index` is disabled

### Docs

- 3-tier fallback ladder diagram in compression.md
- `retention_floor` config reference in configuration.md
- 7 → 10 MCP tools in cli.md (`stm_compression_feedback`, `stm_compression_stats`, `stm_tuning_recommendations`)
- Langfuse nested span table and sampling config in operations.md

### Testing

- 963 automated tests (up from 800)

## [0.1.3] — 2026-04-11

### Fixes

- **Drop phantom `memtomem` runtime dependency** — `pyproject.toml` declared `memtomem>=0.1,<0.2` as a runtime dep, but nothing in `src/` or `tests/` imports `memtomem`: the package talks to the LTM core exclusively through the MCP protocol, as documented in `README.md` and `CONTRIBUTING.md` and required by the invariant in `CLAUDE.md`. The stray entry silently pulled `memtomem` into every `pip install memtomem-stm`, putting the dependency graph at odds with all three documents. Runtime behavior is unchanged; only the dependency graph is cleaner now.

## [0.1.2] — 2026-04-10

### Critical

- **Fix server lifespan not connected** — `app_lifespan` was assigned to a non-existent `_lifespan_handler` attribute on FastMCP, making the entire server non-functional when run via CLI. Now passed via the `lifespan=` constructor kwarg.
- **Propagate upstream `isError` flag** — upstream tool errors were silently converted to success responses. Now raises `ToolError` so `isError=true` is preserved in the proxied response.

### Fixes

- Fix `_surfaced_ids` set pruning modifying a set during iteration (potential `RuntimeError` on CPython 3.12+); now uses snapshot via `itertools.islice`
- Fix `_parse_results` regex splitting on `---` inside content (YAML frontmatter, markdown horizontal rules); now requires score bracket `[N.N]` after separator
- Fix `_parse_scratch_list` truncating keys containing `: ` (e.g., `db: config`); now uses `rfind` heuristic anchored on trailing `...` marker
- Record metrics for non-text-only responses (images, embedded resources) instead of returning silently
- Pass original arguments (with `_context_query`) to surfacing on cache hit so the agent's query hint is preserved
- Snapshot config once per request in `_call_tool_inner` to prevent intra-request inconsistency from hot-reload
- Guard against double `start()` leaking connections by closing previous stack first
- Normalize `\r\n` and `\r` to `\n` in content cleaning before processing
- NFKC-normalize text before injection pattern matching to defeat Unicode confusable bypasses (Cyrillic, fullwidth)
- Add CJK sentence-end punctuation (`。！？`) to `_find_break` for better truncation points in East Asian text
- Widen UUID-based IDs from 12 hex (48 bits) to 16 hex (64 bits) to reduce collision probability
- Add `ProxyCache.stats()` thread-safety lock
- Wrap `_fastmcp_compat` private API access (`_tool_manager._tools`) in try/except for resilience against MCP SDK updates
- Move `feedback_tracker` creation inside `mcp_adapter` success guard so feedback endpoints are not activated when surfacing init fails

### Docs

- Add `stm_proxy_health` tool to cli.md tool table (was missing; tool count 6 → 7)
- Correct README tool count (6 → 7)
- Remove `selective` from auto-selection flowchart in compression.md (`auto_select_strategy` never returns SELECTIVE)
- Update Note to list `selective` alongside `progressive` and `llm_summary` as opt-in only

### Testing

- 800 automated tests (33 new in `test_qa_round3.py`)

## [0.1.1] — 2026-04-10

### CLI
- `mms -h` short flag now works (previously only `--help`)
- `mms status` and `mms list` now show compression strategy and max_chars per server
- Add `auto` to `--compression` choices and set it as the default (aligned with `ProxyConfig.default_compression`)
- Validate `--prefix` format (must start with a letter, no `__`) and warn on duplicate prefixes
- Require `--command` for stdio transport, `--url` for sse/streamable_http transport
- Support quoted paths with spaces in `--args` (via `shlex.split`)

### Fixes
- Resolve all mypy type errors across proxy and surfacing modules (assert guards for optional AsyncClient)
- Fix `cache.clear(tool=X)` without `server` silently wiping entire cache instead of filtering by tool
- Fix `proxy.enabled` field being ignored at runtime — server now skips upstream connections when disabled
- Fix non-deterministic ordering in feedback rating error message

### Docs
- Add uv install options to README and Langfuse extra install sections
- Add CHANGELOG, CONTRIBUTING, and SECURITY files
- Sync LICENSE copyright and pyproject authors with parent memtomem repo
- Align `stm_proxy_stats` example output in operations.md with actual code
- Document `[proxied]` tool description prefix and `{prefix}__{name}` naming convention
- Document that progressive delivery skips memory surfacing
- Clarify hot-reload scope (per-server settings only; adding/removing servers requires restart)
- Fix stale `min_result_retention` docstring (0.5 → 0.65)

### Meta
- Correct pyproject Homepage/Repository URLs

## [0.1.0] — 2026-04-10

Initial open-source release.

### Proxy pipeline
- 4-stage pipeline: CLEAN → COMPRESS → SURFACE → INDEX
- MCP server entrypoint (`memtomem-stm`) and proxy CLI (`memtomem-stm-proxy` / `mms`)
- Transparent proxying for upstream MCP servers over stdio, SSE, and HTTP
- Per-upstream namespacing via `--prefix` (e.g. `fs__read_file`)

### Compression
- 10 strategies with auto-selection by content type
- Query-aware budget allocation (more tokens for query-relevant content)
- Zero-loss progressive delivery (full content on request via cache)
- Model-aware defaults

### Memory surfacing
- Proactive surfacing from a memtomem LTM server via MCP
- Relevance threshold gating (configurable)
- Rate limit + query cooldown
- Session and cross-session dedup
- Write-tool skip (no surfacing on mutations)
- Circuit breaker with retry + exponential backoff

### Caching
- Response cache with TTL and eviction
- Surfacing re-applied on cache hit (injected memories stay fresh)
- Auto-indexing of responses into LTM (when configured)

### Safety
- Sensitive content auto-detection (skip caching/indexing of responses with detected secrets)
- Circuit breaker per upstream
- Configurable write-tool skip list

### Observability
- Langfuse tracing (optional extra: `pip install "memtomem-stm[langfuse]"`)
- RPS, latency percentiles (p50/p95/p99), error classification, per-tool metrics
- `stm_proxy_stats` MCP tool for in-agent inspection

### Horizontal scaling
- `PendingStore` protocol with InMemory (default) and SQLite shared backends

### Testing
- 767 automated tests
- CI: GitHub Actions (lint, typecheck, test)

### Related projects
- [**memtomem**](https://github.com/memtomem/memtomem) — Long-term memory infrastructure. memtomem-stm surfaces memories from a running memtomem MCP server; the two communicate entirely through the MCP protocol with no shared Python dependency.
