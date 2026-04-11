# Tutorial notebooks

> **한국어 사용자 분들께**: 노트북은 유지보수 편의와 GitHub 인덱싱을 위해 영어로 작성되어 있지만, 코드 셀은 그대로 실행하시면 됩니다. 셀 사이의 설명도 직관적인 영어로 유지했으니 부담 없이 따라와 주세요.

Four scenario-based Jupyter notebooks that let you see memtomem-stm behavior end-to-end without setting up Claude Code or Cursor first. Each notebook spawns STM as a subprocess, talks to it via the MCP Python client, and isolates its state into a temp directory so your real `~/.memtomem/` is untouched.

## Notebooks

Start with **notebook 00** for the mental model — it walks both interfaces end-to-end so you can see the CLI ↔ MCP hand-off explicitly. Notebooks 01–04 then dive deeper into individual features.

| # | Notebook | Scenario | External deps |
|---|----------|----------|---------------|
| 00 | [`00_cli_and_mcp_hybrid.ipynb`](00_cli_and_mcp_hybrid.ipynb) | Hybrid prelude: `mms` CLI registers + inspects an upstream, then the MCP client sees the same tool and the optional LangChain cell drives it | None (3 core cells); `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` for the optional LangChain cell |
| 01 | [`01_quickstart_proxy_setup.ipynb`](01_quickstart_proxy_setup.ipynb) | Register an upstream, call a proxied tool, read `stm_proxy_stats` | None |
| 02 | [`02_compression_and_selective.ipynb`](02_compression_and_selective.ipynb) | Selective compression turns an 18 KB doc into a 1.5 KB TOC, then retrieve specific sections via `stm_proxy_select_chunks` | None |
| 03 | [`03_memory_surfacing.ipynb`](03_memory_surfacing.ipynb) | Proactive memory surfacing using an in-repo fake LTM server, with feedback via `stm_surfacing_feedback` | None (uses `_fixtures/fake_ltm.py`) |
| 04 | [`04_langchain_agent_integration.ipynb`](04_langchain_agent_integration.ipynb) | LangChain `create_agent` + `langchain-mcp-adapters`: a real LangGraph agent using STM's proxied tools | `uv sync --extra langchain` and `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` |

Notebooks 00–03 are fully reproducible, hit no external services, and run in CI on every PR. Notebook 00's optional LangChain cell short-circuits when no key is set so CI still passes. Notebook 04 requires an LLM API key end-to-end and is excluded from CI — run it manually when you want to see the full agent loop.

## How to run

### In Jupyter Lab

```bash
uv sync                            # installs dev deps (jupyter, ipykernel, nbmake)
uv run jupyter lab notebooks/      # open and run interactively
```

For notebook 04:

```bash
uv sync --extra langchain          # adds langchain, langchain-mcp-adapters, langchain-anthropic
export ANTHROPIC_API_KEY=sk-ant-...  # or OPENAI_API_KEY=sk-...
uv run jupyter lab notebooks/
```

### Headless via nbmake (what CI does)

```bash
uv run pytest --nbmake \
    notebooks/00_cli_and_mcp_hybrid.ipynb \
    notebooks/01_quickstart_proxy_setup.ipynb \
    notebooks/02_compression_and_selective.ipynb \
    notebooks/03_memory_surfacing.ipynb \
    --nbmake-timeout=180
```

Notebook 04 auto-skips subsequent cells when no API key is set — it's safe to include it in a broader nbmake run, but you won't see the agent execute unless a key is present.

## State isolation

Every notebook's first code cell calls `isolate_stm_state()` from `_helpers.py`, which points STM's proxy config, cache, metrics, and surfacing feedback databases at a fresh temp directory via environment variables:

- `MEMTOMEM_STM_PROXY__CONFIG_PATH`
- `MEMTOMEM_STM_PROXY__CACHE__DB_PATH`
- `MEMTOMEM_STM_PROXY__METRICS__DB_PATH`
- `MEMTOMEM_STM_SURFACING__FEEDBACK_DB_PATH`

All four notebooks — including notebook 03's surfacing demo — are fully hermetic. Your real `~/.memtomem/` is untouched.

## Files

```
notebooks/
├── README.md                                   # this file
├── _build_notebooks.py                         # generates the .ipynb files from source
├── _helpers.py                                 # shared isolation + MCP session utilities
├── _fixtures/
│   ├── echo_mcp.py                             # trivial echo MCP server (nb 00, 01)
│   ├── doc_mcp.py                              # structured doc MCP server (nb 02, 04)
│   └── fake_ltm.py                             # fake memtomem LTM with per-call unique IDs (nb 03)
├── 00_cli_and_mcp_hybrid.ipynb
├── 01_quickstart_proxy_setup.ipynb
├── 02_compression_and_selective.ipynb
├── 03_memory_surfacing.ipynb
└── 04_langchain_agent_integration.ipynb
```

The `.ipynb` files are generated from `_build_notebooks.py`. To edit, modify the builder script and re-run `uv run python notebooks/_build_notebooks.py`.
