# Resume a project with reviewed memory

This scenario shows the boundary between operator-owned project context and
review-first user memory. It requires memtomem core 0.4.0 or later and
memtomem-stm 0.2.0 or later. Both packages remain separate processes and
negotiate the feature through `context_compose` schema 4. Runtime behavior
still follows the connected Core's advertised capability rather than its
package version.

This is an advanced scenario for an already initialized Core installation.
It intentionally changes project-local files, registers that project memory
tier in `~/.memtomem/config.json`, adds one STM upstream, and writes index and
telemetry rows. The cleanup section names each corresponding reversal. Run it
in a disposable Git project if you do not want these project-local artifacts
in an existing checkout.

## 1. Prepare a project-local memory tier

Run this from a Git project root. Project-local memory is gitignored and does
not leave the current checkout.

If memtomem-stm is already installed, `uv tool install` keeps a version the
range below admits and replaces one it does not, with the newest admitted
release. Before a replacement that crosses 0.7.0, whose daemon protocol
changed, quit your MCP hosts and stop the old daemon with the old build, as
the [Upgrading](operations.md#upgrading) runbook describes.

```bash
uv tool install 'memtomem[all]>=0.4,<0.7'
uv tool install 'memtomem-stm>=0.2,<0.8'

mm status
mm mem init
mm pinned set resume-contract \
  --scope project_local \
  --content "Deploy with blue-green; roll back when the error rate exceeds 2%." \
  --description "Reviewed project resume contract" \
  --priority 10
mm pinned list --json
mm pinned compose "blue-green rollback checklist"
```

Both pins sit on the `mcp` 2.x SDK: every core and memtomem-stm release the
ranges above admit declares `mcp[cli]>=2,<3` itself, so neither needs a
`--with` constraint the way the 0.3.x/0.1.x stack did. The ranges are what the
recipe admits, not a claim that every release in them was run: this walkthrough
was exercised with core 0.4.0 and 0.5.0 alongside memtomem-stm 0.2.x and
0.3.x, again with core 0.6.5 and memtomem-stm 0.6.1, and with core 0.6.12
(both the installed tool and the server STM launches) and memtomem-stm 0.7.1
on its default legacy delivery. The 0.7.1 run covered steps 1–3 and the
cleanup, with an MCP stdio client standing in for the AI client that made the
read 5 seconds after its session started; step 4 was not rerun. They are still
two separate `uv tool install` environments — the SDK major matters per
environment, not across the MCP connection between them. Upgrading an existing
tool from the older stack takes a reinstall:

```bash
uv tool install --reinstall 'memtomem[all]>=0.4,<0.7'
```

Do not run `mm init` as part of this scenario: it rewrites the user-level Core
configuration. If `mm status` reports that Core is not initialized, complete
Core's setup separately and then return here. `mm mem init` is narrower but is
still a persistent trust operation: it creates the local tier, updates the
project `.gitignore`, and registers the resolved tier path in
`indexing.project_memory_dirs`.

The compose command is a pinned-first baseline. Its CLI intentionally does not
accept namespace or context-window flags; STM supplies those controls over MCP.

## 2. Add a multi-chunk project note

Create a deterministic document long enough to pass STM's default 5,000-character
response gate and to produce adjacent chunks.

```bash
python - <<'PY'
from pathlib import Path

root = Path(".memtomem/memories.local")
root.mkdir(parents=True, exist_ok=True)
sections = []
for index in range(1, 9):
    marker = "resume-window-sentinel " if index == 4 else ""
    sections.append(
        f"## Deployment phase {index}\n\n"
        + (
            f"{marker}Phase {index} uses blue-green verification and a 2% rollback threshold. "
            * 10
        )
    )
(root / "resume-demo.md").write_text("\n\n".join(sections), encoding="utf-8")
PY

mm index .memtomem/memories.local/resume-demo.md --namespace resume-demo --force
```

## 3. Configure STM and call a proxied read tool

These are environment settings, not keys in `stm_proxy.json`. The zero score
floor and one-token query threshold are deterministic demo settings; restore
your normal thresholds afterwards. The larger preview cap is what makes the
adjacent context visible: the matched chunk is rendered first, and neighbouring
snippets only fill what is left of `preview_max_chars`. Each section of the demo
note is longer than the default cap of 300 characters, so with the default the
block shows the match alone.

```bash
export MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND=uvx
export MEMTOMEM_STM_SURFACING__LTM_MCP_ARGS='["--from","memtomem>=0.4,<0.7","memtomem-server"]'
export MEMTOMEM_STM_SURFACING__DEFAULT_NAMESPACE=resume-demo
export MEMTOMEM_STM_SURFACING__CONTEXT_WINDOW_SIZE=1
export MEMTOMEM_STM_SURFACING__MIN_SCORE=0
export MEMTOMEM_STM_SURFACING__MIN_QUERY_TOKENS=1
export MEMTOMEM_STM_SURFACING__PREVIEW_MAX_CHARS=1200

mms add resume_fs \
  --command npx \
  --args "-y @modelcontextprotocol/server-filesystem $PWD" \
  --prefix resume_fs
mms daemon stop
```
`uvx` resolves its own environment, so the version pin from step 1 has to be
repeated here — the tool you installed and the server STM launches are two
different environments.

`mms daemon stop` confirms with `daemon stopped` or `no running daemon`. Since
0.7.1 a stop it cannot confirm in time exits 1; run `mms daemon status` and wait
until it reports `stopped` before restarting the client. Step 2 of the
[Upgrading](operations.md#upgrading) runbook explains each outcome. The same
applies to the `mms daemon stop` calls in steps 4 and 5.

Restart the AI client so it reloads STM and its environment. Ask it to call the
proxied `resume_fs__read_file` tool for
`.memtomem/memories.local/resume-demo.md`. A client-native `Read` tool bypasses
STM and does not exercise this scenario.

Give STM a few seconds after the client starts before the read. Until STM has
finished connecting to Core it falls back to plain search, so a read within
about the first second (measured with a warm `uv` cache; a cold one takes
longer) shows the match without the Pinned contract or the adjacent snippets.
Surfacing remembers a match it delivered for `surfacing.dedup_ttl_seconds`
(7 days by default), so to repeat the read, export
`MEMTOMEM_STM_SURFACING__DEDUP_TTL_SECONDS=0` alongside the settings above,
restart the client, and again wait a few seconds before the read.

The returned tool response should retain the original document and append a
`<surfaced-memories>` block containing:

- the **Pinned** resume contract;
- a matched `resume-demo.md` memory;
- the nearest before and after snippets around that match.

Schema 4 retains schema 3's budgeted context arrays and adds score-scale
metadata. STM accepts at most ten chunks per direction and renders only the
nearest chunk on each side in its compact preview.

Check durable operational evidence by running this before and after the call:

```bash
mms stats --json
```

Its `surfacing` section reports totals, not individual events: `events_total`
should grow by the proxied read, and no count in `faults` should grow. `faults`
covers the last 7 days across all tools, so counts already present before the
call are not caused by this scenario.

## 4. Optional: submit a review-first candidate

Formation is independently opt-in and must be enabled before STM starts.

```bash
mms daemon stop
export MEMTOMEM_STM_FORMATION__ENABLED=true
```

Restart the client, then call:

```text
stm_memory_propose(
  content="Decision: pause rollout when the error rate exceeds 2%.",
  source_ref="reviewed-memory-resume",
  idempotency_key="reviewed-memory-resume-v1"
)
```

The response remains `pending` until reviewed in Core. For the copyable demo
path, inspect and reject it so no user-scope memory is created:

```bash
mm review list
mm review show CANDIDATE_ID
mm review reject CANDIDATE_ID \
  --reviewer demo-operator \
  --reason "reviewed-memory-resume demonstration"
```

Approval is a separate, persistent choice: it normally writes a user-scope
memory according to Core classification and does not replace the explicit
project-local Pinned Context from step 1. Follow Core's review guide if you
intend to approve and retain the candidate; this walkthrough does not approve
one implicitly.

## 5. Clean up demo state

If step 4 created a candidate and it is still pending, reject it before the
rest of the cleanup. Then remove the exact Pinned block, source document,
orphaned index rows, and uniquely named STM upstream:

```bash
mm review reject CANDIDATE_ID \
  --reviewer demo-operator \
  --reason "demo cleanup"
mm pinned delete resume-contract --scope project_local

python - <<'PY'
from pathlib import Path

Path(".memtomem/memories.local/resume-demo.md").unlink(missing_ok=True)
PY

mm gc orphan-sources
mm gc orphan-sources --apply
mms remove resume_fs --yes
mms daemon stop
```

The first GC command is a preview; inspect it before authorizing `--apply`.
`--apply` then asks for an interactive confirmation — add `--yes` only in a
non-interactive caller, where an unanswered prompt exits non-zero. A running
Core may also reap the deleted source through its own file watcher, so an
empty GC preview here is a success, not a missed cleanup. Do not use
`mms daemon stop --all` for routine cleanup because it also targets daemons
belonging to other configuration fingerprints.

Finally remove the process-local overrides:

```bash
unset MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND
unset MEMTOMEM_STM_SURFACING__LTM_MCP_ARGS
unset MEMTOMEM_STM_SURFACING__DEFAULT_NAMESPACE
unset MEMTOMEM_STM_SURFACING__CONTEXT_WINDOW_SIZE
unset MEMTOMEM_STM_SURFACING__MIN_SCORE
unset MEMTOMEM_STM_SURFACING__MIN_QUERY_TOKENS
unset MEMTOMEM_STM_SURFACING__PREVIEW_MAX_CHARS
unset MEMTOMEM_STM_SURFACING__DEDUP_TTL_SECONDS
unset MEMTOMEM_STM_FORMATION__ENABLED
```

The project memory tier registration is useful beyond this demo and is not
silently removed. For a full reversal, first back up
`~/.memtomem/config.json`, then remove only this project's resolved
`.memtomem/memories.local` entry from `indexing.project_memory_dirs` with an
editor. After verifying `mm status`, remove the now-empty local tier and only
the `.gitignore` guard lines that `mm mem init` added. Core deliberately has no
generic `mm config unset` path for this trust registration.
