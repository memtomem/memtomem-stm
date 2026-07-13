# Resume a project with reviewed memory

This scenario shows the boundary between operator-owned project context and
review-first user memory. It requires memtomem core 0.3.10 or later and
memtomem-stm 0.1.38 or later. Both packages remain separate processes and
negotiate the feature through `context_compose` schema 3.

## 1. Prepare a project-local memory tier

Run this from a Git project root. Project-local memory is gitignored and does
not leave the current checkout.

```bash
uv tool install 'memtomem[all]>=0.3.10,<0.4'
uv tool install 'memtomem-stm>=0.1.38,<0.2'

mm init --non-interactive --preset minimal --namespace resume-demo --mcp skip
mm mem init
mm pinned set resume-contract \
  --scope project_local \
  --content "Deploy with blue-green; roll back when the error rate exceeds 2%." \
  --description "Reviewed project resume contract" \
  --priority 10
mm pinned list --json
mm pinned compose "blue-green rollback checklist"
```

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
    sections.append(
        f"## Deployment phase {index}\n\n"
        + (f"Phase {index} uses blue-green verification and a 2% rollback threshold. " * 35)
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
export MEMTOMEM_STM_SURFACING__LTM_MCP_ARGS='["--from","memtomem>=0.3.10,<0.4","memtomem-server"]'
export MEMTOMEM_STM_SURFACING__DEFAULT_NAMESPACE=resume-demo
export MEMTOMEM_STM_SURFACING__CONTEXT_WINDOW_SIZE=1
export MEMTOMEM_STM_SURFACING__MIN_SCORE=0
export MEMTOMEM_STM_SURFACING__MIN_QUERY_TOKENS=1

mms add filesystem \
  --command npx \
  --args "-y @modelcontextprotocol/server-filesystem $PWD" \
  --prefix fs
mms daemon stop --all
```
Restart the AI client so it reloads STM and its environment. Ask it to call the
proxied `fs__read_file` tool for
`.memtomem/memories.local/resume-demo.md`. A client-native `Read` tool bypasses
STM and does not exercise this scenario.

The returned tool response should retain the original document and append a
`<surfaced-memories>` block containing:

- the **Pinned** resume contract;
- a matched `resume-demo.md` memory;
- the nearest before and after snippets around that match.

Schema 3 transports the full budgeted context arrays. STM 0.1.38 renders only
the nearest chunk on each side in its compact preview.

Check durable operational evidence after the call:

```bash
mms stats --json
```

The surfacing section should contain an event for the proxied read and should
not report an LTM dependency fault.

## 4. Optional: submit a review-first candidate

Formation is independently opt-in and must be enabled before STM starts.

```bash
export MEMTOMEM_STM_FORMATION__ENABLED=true
mms daemon stop --all
```

Restart the client, then call:

```text
stm_memory_propose(
  content="Decision: pause rollout when the error rate exceeds 2%.",
  source_ref="reviewed-memory-resume",
  idempotency_key="reviewed-memory-resume-v1"
)
```

The response remains `pending` until reviewed in core:

```bash
mm review list
mm review show CANDIDATE_ID
mm review approve CANDIDATE_ID --reviewer "$USER"
```

Proposal approval does not select a project destination. It normally writes a
user-scope memory according to core classification; it does not replace the
explicit project-local Pinned Context created in step 1.

## 5. Clean up demo overrides

```bash
unset MEMTOMEM_STM_SURFACING__LTM_MCP_COMMAND
unset MEMTOMEM_STM_SURFACING__LTM_MCP_ARGS
unset MEMTOMEM_STM_SURFACING__DEFAULT_NAMESPACE
unset MEMTOMEM_STM_SURFACING__CONTEXT_WINDOW_SIZE
unset MEMTOMEM_STM_SURFACING__MIN_SCORE
unset MEMTOMEM_STM_SURFACING__MIN_QUERY_TOKENS
unset MEMTOMEM_STM_FORMATION__ENABLED
mms daemon stop --all
```
