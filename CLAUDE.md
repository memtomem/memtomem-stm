# Claude Code notes — memtomem-stm

Short-term memory MCP proxy. What it does → `README.md`; setup and layout →
`CONTRIBUTING.md`; architecture → `docs/`. This file holds only what must be
known *before* reading those — everything else is a pointer, not a copy.

## Commands

Requires Python 3.12+ and `uv`.

```bash
uv sync                                                    # install deps
uv run pytest -m "not bench_qa_meta and not bench_qa_llm_judge and not bench_qa_sweep and not bench_qa_drift and not bench_qa_perf"  # tests (CI filter)
uv run ruff check src && uv run ruff format --check src    # lint (required)
uv run mypy src                                            # typecheck (required)
uv run pytest tests/test_docs_sync.py tests/test_docs_links.py  # docs contracts
uv run pytest --nbmake notebooks/01_quickstart_proxy_setup.ipynb --nbmake-timeout=180  # public tutorial
```

`ruff`, `mypy`, and tests gate merge — on **Windows too**, so POSIX-only stdlib
(`fcntl`, `os.killpg`, …) needs a `sys.platform` guard mypy can narrow.
Live-Ollama tests are out of scope: Ollama paths are tested with mocked
transport / dead ports only (#637).

## Invariants when editing

- **No Python-level dependency on `memtomem` core.** Don't `import memtomem`
  from `src/` — STM reaches LTM only over MCP.
- **`mms` ≡ `memtomem-stm-proxy` ≡ `memtomem-stm`.** Three entry points, one
  Click group; registering any of them as an MCP client's `command` must work
  identically. Dispatch rules: the `cli` group docstring in
  `src/memtomem_stm/cli/proxy.py` (#260).
- **Pipeline order is CLEAN → COMPRESS → SURFACE**, plus an optional INDEX
  stage for custom embedders. Per-stage contracts: comments in
  `src/memtomem_stm/proxy/` — authoritative, don't restate them here.
- **Line length 100**, target `py312`.
- **`.claude/` and `scripts/` are gitignored** except the tracked
  `scripts/audit-dependencies.sh` (release/CI gate) and
  `scripts/core_compat_smoke.py` (advisory compat check). Don't commit other
  files under them, and don't assume contributors share your local contents.
- **One focused change per PR**, branched from `main`, with tests for new
  behavior and a commit message explaining the "why". Full checklist:
  `CONTRIBUTING.md`.
