# Resume a project with reviewed memory

This scenario shows the boundary between operator-owned project context and
review-first user memory. It requires memtomem core 0.3.12 or later and
memtomem-stm 0.1.44 or later. Both packages remain separate processes and
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

```bash
uv tool install --with 'mcp<2' 'memtomem[all]>=0.3.12,<0.4'
uv tool install 'memtomem-stm>=0.1.44,<0.2'

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

Core 0.3.x imports the MCP 1.x `FastMCP` module, which the separately released
`mcp` 2.x package removed. The explicit `--with 'mcp<2'` keeps a fresh install
on Core's compatible MCP runtime until Core's package metadata carries that
upper bound. For an existing uv tool, reinstall with the same constraint before
continuing:

```bash
uv tool install --reinstall --with 'mcp<2' 'memtomem[all]>=0.3.12,<0.4'
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
your normal thresholds afterwards.

```bash
export MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND=uvx
export MEMTOMEM_STM_SURFACING__LTM_MCP_ARGS='["--from","memtomem>=0.3.12,<0.4","memtomem-server"]'
export MEMTOMEM_STM_SURFACING__DEFAULT_NAMESPACE=resume-demo
export MEMTOMEM_STM_SURFACING__CONTEXT_WINDOW_SIZE=1
export MEMTOMEM_STM_SURFACING__MIN_SCORE=0
export MEMTOMEM_STM_SURFACING__MIN_QUERY_TOKENS=1

mms add resume_fs \
  --command npx \
  --args "-y @modelcontextprotocol/server-filesystem $PWD" \
  --prefix resume_fs
mms daemon stop
```
Restart the AI client so it reloads STM and its environment. Ask it to call the
proxied `resume_fs__read_file` tool for
`.memtomem/memories.local/resume-demo.md`. A client-native `Read` tool bypasses
STM and does not exercise this scenario.

The returned tool response should retain the original document and append a
`<surfaced-memories>` block containing:

- the **Pinned** resume contract;
- a matched `resume-demo.md` memory;
- the nearest before and after snippets around that match.

Schema 4 retains schema 3's budgeted context arrays and adds score-scale
metadata. STM accepts at most ten chunks per direction and renders only the
nearest chunk on each side in its compact preview.

Check durable operational evidence after the call:

```bash
mms stats --json
```

The surfacing section should contain an event for the proxied read and should
not report an LTM dependency fault.

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
unset MEMTOMEM_STM_FORMATION__ENABLED
```

The project memory tier registration is useful beyond this demo and is not
silently removed. For a full reversal, first back up
`~/.memtomem/config.json`, then remove only this project's resolved
`.memtomem/memories.local` entry from `indexing.project_memory_dirs` with an
editor. After verifying `mm status`, remove the now-empty local tier and only
the `.gitignore` guard lines that `mm mem init` added. Core deliberately has no
generic `mm config unset` path for this trust registration.
