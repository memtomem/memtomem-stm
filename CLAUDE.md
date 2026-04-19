# Claude Code notes — memtomem-stm

Short-term memory MCP proxy. For what it does see `README.md`; for setup and
project layout see `CONTRIBUTING.md`; for architecture see `docs/`. This file
only captures the few things Claude Code needs in context that aren't obvious
from those docs.

## Commands

Requires Python 3.12+ and `uv`.

```bash
uv sync                                                    # install deps
uv run pytest -m "not ollama"                              # tests (CI filter)
uv run ruff check src && uv run ruff format --check src    # lint (required)
uv run mypy src                                            # typecheck (advisory)
```

The `ollama` marker auto-skips when Ollama isn't running; CI always uses
`-m "not ollama"`. `ruff` and tests must pass to merge; `mypy` is advisory.

## Invariants when editing

- **No Python-level dependency on `memtomem` core.** STM talks to the LTM
  server only through the MCP protocol. Don't `import memtomem` from `src/`.
- **`mms` ≡ `memtomem-stm-proxy`.** Both entry points in `pyproject.toml`
  resolve to the same CLI — keep them in sync, don't diverge behavior.
- **Pipeline order is CLEAN → COMPRESS → SURFACE → INDEX** — comments in
  `src/memtomem_stm/proxy/` are the source of truth for the per-stage
  contracts; full architecture write-up lives in the private
  `memtomem-docs/memtomem-stm/guides-archived/pipeline.md`.
- **Line length 100**, target `py312` (`tool.ruff`, `tool.mypy`).
- `.claude/` and `scripts/` are gitignored — don't commit anything under them,
  and don't assume other contributors have the same contents there.

## PRs

Branch from `main`, one focused change per PR, add tests for new behavior, and
write commit messages that explain the "why". See `CONTRIBUTING.md` for the
full checklist.
