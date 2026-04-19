# Tutorial notebooks

> **한국어 사용자 분들께**: 노트북은 유지보수 편의와 GitHub 인덱싱을 위해 영어로 작성되어 있지만, 코드 셀은 그대로 실행하시면 됩니다.

One quick-start notebook is kept here as a runnable demo of memtomem-stm:

| # | Notebook | Scenario | External deps |
|---|----------|----------|---------------|
| 01 | [`01_quickstart_proxy_setup.ipynb`](01_quickstart_proxy_setup.ipynb) | Register an upstream MCP server, call a proxied tool, read `stm_proxy_stats` | None |

The other five scenario notebooks (CLI/MCP hybrid, selective compression,
memory surfacing, LangChain integration, observability + Langfuse) live in
the private `memtomem/memtomem-docs` repo at
`memtomem-stm/examples/notebooks/` along with the `_build_notebooks.py`
generator. They were moved out of the public repo to keep the beginner
examples surface small while still preserving them as internal reference.

## How to run

### In Jupyter Lab

```bash
uv sync                            # installs dev deps (jupyter, ipykernel, nbmake)
uv run jupyter lab notebooks/      # open and run interactively
```

### Headless via nbmake (what CI does)

```bash
uv run pytest --nbmake \
    notebooks/01_quickstart_proxy_setup.ipynb \
    --nbmake-timeout=180
```

## State isolation

The notebook's first code cell calls `isolate_stm_state()` from
`_helpers.py`, which points STM's proxy config, cache, metrics, and
surfacing feedback databases at a fresh temp directory via environment
variables:

- `MEMTOMEM_STM_PROXY__CONFIG_PATH`
- `MEMTOMEM_STM_PROXY__CACHE__DB_PATH`
- `MEMTOMEM_STM_PROXY__METRICS__DB_PATH`
- `MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH`

The notebook is hermetic — your real `~/.memtomem/` is untouched.

## Files

```
notebooks/
├── README.md                                   # this file
├── _helpers.py                                 # shared isolation + MCP session utilities
├── _fixtures/
│   ├── echo_mcp.py                             # trivial echo MCP server (used by 01)
│   ├── doc_mcp.py                              # structured doc MCP server (used by archived notebooks)
│   └── fake_ltm.py                             # fake memtomem LTM (used by archived notebooks)
└── 01_quickstart_proxy_setup.ipynb
```

`_fixtures/doc_mcp.py` and `_fixtures/fake_ltm.py` are kept for
completeness even though `01` only uses `echo_mcp.py` — they're tiny and
the archived notebooks (and any future re-promotion) depend on them.
