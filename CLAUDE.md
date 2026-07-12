# Claude Code notes — memtomem-stm

Short-term memory MCP proxy. For what it does see `README.md`; for setup and
project layout see `CONTRIBUTING.md`; for architecture see `docs/`. This file
only captures the few things Claude Code needs in context that aren't obvious
from those docs.

## Commands

Requires Python 3.12+ and `uv`.

```bash
uv sync                                                    # install deps
uv run pytest -m "not bench_qa_meta and not bench_qa_llm_judge and not bench_qa_sweep and not bench_qa_drift and not bench_qa_perf"  # tests (CI filter)
uv run ruff check src && uv run ruff format --check src    # lint (required)
uv run mypy src                                            # typecheck (required)
```

The filter excludes only the `bench_qa_*` markers CI runs in separate jobs;
live-Ollama tests are out of scope — Ollama code paths are tested with mocked
transport / dead ports only (#637). `ruff`, `mypy`, and tests must pass to
merge. mypy must be clean on Windows too — CI typechecks both platforms, so
POSIX-only stdlib usage (`fcntl`, `os.killpg`, …) needs a `sys.platform` guard
mypy can narrow.

## Invariants when editing

- **No Python-level dependency on `memtomem` core.** STM talks to the LTM
  server only through the MCP protocol. Don't `import memtomem` from `src/`.
- **`mms` ≡ `memtomem-stm-proxy` ≡ `memtomem-stm`.** All three
  `[project.scripts]` entry points in `pyproject.toml` resolve to
  `memtomem_stm.cli.proxy:cli` (#260). The Click group's
  `invoke_without_command=True` callback dispatches bare invocations on
  `sys.stdin.isatty()`: TTY → help, non-TTY → `server.main` (the MCP stdio
  server). Don't diverge behavior between the three names — registering
  any of them as an MCP client's `command` must work identically.
- **Pipeline order is CLEAN → COMPRESS → SURFACE, with optional library INDEX.**
  `ProxyManager` runs INDEX only when a custom embedder supplies an
  `index_engine`; the bundled `mms` server pipeline ends after SURFACE.
  Comments in `src/memtomem_stm/proxy/` remain the source of truth for the
  per-stage contracts; full architecture write-up lives in the private
  `memtomem-docs/memtomem-stm/guides-archived/pipeline.md`.
- **Line length 100**, target `py312` (`tool.ruff`, `tool.mypy`).
- `.claude/` and `scripts/` are gitignored except for the tracked
  `scripts/audit-dependencies.sh` release/CI gate — don't commit other files
  under them, and don't assume contributors have the same local contents.

## PRs

Branch from `main`, one focused change per PR, add tests for new behavior, and
write commit messages that explain the "why". See `CONTRIBUTING.md` for the
full checklist.
