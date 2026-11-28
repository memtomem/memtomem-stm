# Toolgraph Policy Gateway

This guide connects Toolgraph's policy compiler to STM's runtime MCP gateway.
The two packages stay independent: Toolgraph writes one portable JSON artifact;
STM validates and enforces it without importing Toolgraph or opening its graph
database on the call path. This boundary is an instance of the integration
contract pattern recorded in
[ADR 0001](../adr/0001-ecosystem-integration-contracts.md).

Start in `review`. Rejected tools remain visible and callable while STM records
what strict mode would block. Move to `strict` only after `status` and `explain`
match the intended catalog.

## Prerequisites

- A working STM upstream configuration (`mms doctor` should have no FAILs).
- A Toolgraph graph that crawled those same upstream MCP servers.
- The same agent id and qualified `server::tool` names on both sides.

For a Docker-free producer example, complete Toolgraph's
[beginner guide](https://github.com/memtomem/toolgraph/blob/main/docs/beginner-guide.md).
It creates the `vibe-coder` agent, crawls the bundled `policy-gateway` server,
and authors one eligible and one rejected decision.

## 1. Keep server identities aligned

The simplest setup uses the same name in both configurations:

```bash
mms add policy-gateway \
  --command /absolute/path/to/toolgraph/.venv/bin/python \
  --args "/absolute/path/to/toolgraph/examples/policy_gateway_server.py" \
  --prefix demo \
  --compression none \
  --validate
```

Toolgraph must crawl that upstream as `policy-gateway`; its qualified key is
therefore `policy-gateway::read_note`. STM's public client-facing alias remains
`demo__read_note`. If an existing STM connection key differs from the crawled
name, set `toolgraph.server_name_map` explicitly before applying policy.

## 2. Publish a review bundle first

Run this from the Toolgraph checkout:

```bash
uv run toolgraph policy compile \
  --agent vibe-coder \
  --profile review \
  --output ~/.memtomem/toolgraph/policy-bundle.json
```

Then apply the matching STM profile:

```bash
mms gateway mode review \
  --bundle ~/.memtomem/toolgraph/policy-bundle.json \
  --apply
mms gateway status
mms gateway explain policy-gateway::read_note
mms gateway explain policy-gateway::publish_note
```

`status --json` reports `valid`, the exact bundle digest, graph identity and
generation, and eligible/rejected counts. Automation should inspect `valid`;
status commands remain informational and can return a JSON diagnostic for an
invalid artifact. `explain` reads the bundle only and never calls the upstream.

Restart the MCP client after applying the bundle. STM registers its advertised
tool list when the MCP server starts, so the client should reconnect before
you judge which tools are visible.

## 3. Verify review behavior

In the Toolgraph fixture, both `demo__read_note` and `demo__publish_note` remain
visible under review. Calls are allowed, while `stm_admin(action="proxy_health")`
reports the review would-block count when the MCP client registration sets
`MEMTOMEM_STM_ADVERTISE_OBSERVABILITY_TOOLS=true`. Restart the client after
adding that environment value, then call `stm_admin(action="proxy_health")` from the client.
The underlying gateway status field is `would_block_calls`.

The counter records policy decisions, not raw arguments or results.

## 4. Move to strict

Compile the strict artifact before changing STM's mode, so the bundle profile
and active gateway profile never disagree:

```bash
uv run toolgraph policy compile \
  --agent vibe-coder \
  --profile strict \
  --output ~/.memtomem/toolgraph/policy-bundle.json
mms gateway mode strict --apply
mms gateway status
```

Restart the MCP client again. Rejected, missing, unmapped, or contract-drifted
tools are no longer advertised, and direct calls through an already registered
handler are blocked before telemetry, response-cache lookup, or upstream
dispatch.

## Give each agent role its own tool set

An STM process enforces one Toolgraph agent, so roles such as a reviewer and
an implementer each get their own bundle, config file and MCP registration.
In Toolgraph, declare each role as an agent and grant it its tools.
`ingest-manifest` replaces the graph's authored governance, so edit the full
manifest rather than loading a fragment. With the quickstart fixture, grant
`read_note` to both roles and `publish_note` to the implementer only, and
remove the DENY binding on the drafts resource, which rejects `publish_note`
for every agent. Ingest the edited manifest, then compile one bundle per role
from the same directory, so Toolgraph reads the same `.toolgraph/config.json`
and graph (for the quickstart, `toolgraph-quickstart`); the reviewer's reports
1 eligible and 1 rejected tool, the implementer's 2 and 0:

```bash
toolgraph policy compile --agent reviewer --profile strict \
  --output ~/.memtomem/toolgraph/reviewer.json
toolgraph policy compile --agent implementer --profile strict \
  --output ~/.memtomem/toolgraph/implementer.json
```

Copy the working STM config once per role, for example to
`~/.memtomem/stm_proxy.reviewer.json`, and make later upstream edits in every
copy. In each copy set `toolgraph.agent_id` to the role and
`toolgraph.bundle_path` to its bundle, then run
`mms gateway mode strict --config <file> --apply`.
`mms gateway status --config <file> --json` should report `valid: true`, the
role as `agent` and `profile: "strict"`, and
`mms gateway explain policy-gateway::publish_note --config <file>` looks up
that role's decision for one tool. Keep one file per role rather than overriding
the agent or bundle with environment variables: `status` and `explain` read
the file as written.

Register STM once per role, in that role's own client configuration. For
example, `mms register --config <file> --client json` in a role's project
directory writes a `.mcp.json` entry that sets
`MEMTOMEM_STM_PROXY__CONFIG_PATH` to the file. It keeps an existing
`memtomem-stm` entry unless you pass `--replace-registration`, and replacing
an entry keeps environment values it does not manage. Check that each entry
points at its role's file, then restart each client and confirm in each role's
session that the proxied tools listed are only that role's (STM's own `stm_*`
tools appear alongside them). With the quickstart grants, the reviewer's call
to `demo__publish_note` is refused and the implementer's succeeds.
`mms gateway status` reads only the file, so it cannot show an
override that reaches STM from the client's environment, in any of its forms
(#1583). The session checks verify behavior, not which file was loaded: two
bundles with the same decisions pass them alike. To confirm the artifact,
compare the bundle digest `stm_admin(action="proxy_health")` reports in the
session (with the observability tools enabled, as in step 3) against the
digest `mms gateway status --config <file>` prints.

After the restart, a strict registration advertises only the proxied tools its
role's bundle makes eligible, a call to a name it does not advertise fails as
an unknown tool, and advertised tools still pass the policy check on every
call. Under `strict`, a bundle whose agent or profile does not match makes
startup fail; under `review` or `explore`, STM logs a warning, starts, and
lets the calls through.

STM does not know which agent is calling: the registration is the role. The
separation holds only when each role's session is given only its own
registration, and no direct registration of the same upstream; which session
sees which registration is the host's configuration. STM separates only the
calls that go through its registration. An agent that runs with the
operator's file or shell access can edit its own bundle and config, or start
the upstream itself, so where the split is a security boundary, the host must
also keep each role away from the policy files, the other registration and the
upstream. On POSIX systems STM warns when the owner or mode bits of a bundle,
a directory above it, or a symlink on its path suggest another user could
replace it, but still loads it in `strict` (#707). The check does not read
ACLs, so no warning is not an assurance.

The roles separate tool sets, not STM's state. Unless the files set their
own paths, both processes use the same response cache, metrics and feedback
stores. One role can be served a response the other role cached when both
files configure that upstream identically, and strict health filtering
counts both roles' upstream errors, so one role's failures can withhold a tool
the other role's bundle grants at its next startup.

## Updating policy safely

1. Crawl the live upstream again when its tool contract changes.
2. Update and ingest the governance manifest.
3. Compile a bundle whose profile matches STM's active profile.
4. Check `mms gateway status` and the relevant `explain` results.
5. Restart STM/the MCP client to refresh the advertised tool list.

An already running STM process checks the bundle before each proxied call, so
a new strict denial blocks immediately and cannot be bypassed by a warm cache.
The external MCP `tools/list` registry is rebuilt only when an upstream
replaces its own catalogue (#917) — a bundle change alone does not rebuild it; its
visibility change appears after restart. A malformed or missing strict bundle
causes startup to fail closed. Review keeps the last known good snapshot and
surfaces degraded status instead.

Under a **stdio** gateway (`toolgraph.source: "stdio"`) the consult runs once,
at startup, and the verdict map is held for the session. The snapshot also
records which candidates it covered, so a tool an upstream adds *after* that
consult was never put to the graph and is treated as unjudged rather than
approved: it takes the `toolgraph_unconsulted` reason, which rides the profile
ladder like any other — `strict` withholds it, `review` demotes it, `explore`
ignores it. Under `strict` this means a genuinely new upstream tool cannot be
advertised until STM restarts, **even when the graph would have granted it**.
Restart STM/the MCP client to pick it up. Bundle mode has no equivalent state:
its decisions are rebound against the live catalogue on every refresh, so a new
tool becomes `toolgraph_unmapped` or `toolgraph_drifted` instead.

## Trust and troubleshooting

- `bundle_digest` is the SHA-256 of the exact artifact bytes for identity and
  reporting; it is not a signature. Protect the bundle and config with local
  filesystem permissions.
- `toolgraph_drifted` usually means the live description, input schema, or MCP
  annotations changed after Toolgraph crawled the server.
- `toolgraph_unmapped` usually means STM's upstream connection key does not
  match the server name Toolgraph crawled.
- A bundle whose agent or profile does not match the config is rejected, never
  adopted. Under `strict` that fails closed; under `review` or `explore` STM
  warns, reports degraded status, and keeps its last known good snapshot (none
  at startup, so calls go through). After changing the config's agent or
  profile, restart STM so the bundle is checked against it.
- STM bundle mode never launches Toolgraph. Toolgraph and Ladybug may be fully
  stopped after the bundle is published.
