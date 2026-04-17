"""Generate the five tutorial notebooks as .ipynb files.

Run with: ``uv run python notebooks/_build_notebooks.py``

This script is in the repo so the notebooks can be regenerated from
source if anyone edits the cell content. The notebooks themselves are
checked in (so users don't have to run this), but edits should flow
through here to avoid JSON drift.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import nbformat as nbf

NOTEBOOKS_DIR = Path(__file__).parent.resolve()


# ---------------------------------------------------------------------------
# Shared snippets
# ---------------------------------------------------------------------------

_BOOTSTRAP = dedent(
    """\
    # Add notebooks/ to sys.path so `_helpers` is importable regardless of
    # where Jupyter was launched (from the repo root or from notebooks/).
    import sys
    from pathlib import Path

    _cwd = Path.cwd()
    if (_cwd / "_helpers.py").exists():
        sys.path.insert(0, str(_cwd))
    elif (_cwd / "notebooks" / "_helpers.py").exists():
        sys.path.insert(0, str(_cwd / "notebooks"))
    else:
        raise RuntimeError(
            "Cannot find notebooks/_helpers.py — run Jupyter from the repo "
            "root or from the notebooks/ directory."
        )
    """
)


def _md(text: str) -> nbf.NotebookNode:
    return nbf.v4.new_markdown_cell(dedent(text).strip())


def _code(text: str) -> nbf.NotebookNode:
    return nbf.v4.new_code_cell(dedent(text).strip())


def _save(nb: nbf.NotebookNode, filename: str) -> None:
    # Set a stable kernelspec so nbmake / jupyter lab both pick the
    # right Python kernel without prompting.
    nb.metadata["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }
    nb.metadata["language_info"] = {"name": "python", "version": "3.12"}
    path = NOTEBOOKS_DIR / filename
    nbf.write(nb, path)
    print(f"wrote {path.relative_to(NOTEBOOKS_DIR.parent)}")


# ---------------------------------------------------------------------------
# Notebook 00 — Hybrid CLI + MCP: how the pieces fit
# ---------------------------------------------------------------------------


def build_nb00() -> None:
    nb = nbf.v4.new_notebook()
    nb.cells = [
        _md(
            """
            # 00 — Hybrid CLI + MCP: how the pieces fit

            memtomem-stm has two faces. You talk to **one** at configuration
            time, and **another** at runtime — and the two are the same
            process wearing different hats.

            - **`mms` CLI** — the operator interface. You run it to
              register upstream MCP servers, list them, check status,
              remove them. It writes to a JSON config file. It's what
              you use *before* you hand STM over to an agent.
            - **`memtomem-stm` MCP server** — the agent interface.
              It reads that same JSON config, spawns the upstreams as
              subprocesses, and exposes them as proxied tools over the
              MCP stdio transport. It's what the agent talks to.

            This notebook is a prelude to the rest of the series: it
            walks both interfaces end-to-end against a trivial echo
            fixture so you can see the relationship explicitly. Every
            other notebook (01 through 05) assumes you already have this
            mental model.

            **You will learn:**

            - How `mms add` / `mms list` shape STM's config file
            - How the MCP server surfaces what the CLI registered
            - Where each side's responsibility ends

            **Prereqs:** `uv sync` (dev group, includes Jupyter). No
            external services. No API keys for the 3 core cells — an
            optional LangChain cell at the bottom gates itself on
            `_HAS_KEY`.
            """
        ),
        _md("## 1. Isolate state and import helpers"),
        _code(_BOOTSTRAP),
        _code(
            """
            import subprocess

            from _helpers import (
                isolate_stm_state,
                stm_session,
                extract_text,
                fixtures_dir,
            )

            config_path = isolate_stm_state(prefix="nb00_")
            print(f"STM config → {config_path}")
            print(f"Fixtures   → {fixtures_dir()}")
            """
        ),
        _md(
            """
            ## 2. CLI side — configuring STM with `mms`

            `mms` is the operator CLI. Its job is to write and inspect
            the proxy config file. It never runs as a long-lived server
            and never talks to agents directly — it's a one-shot tool
            for humans setting things up.

            ### 2a. Register the echo fixture as an upstream
            """
        ),
        _code(
            """
            echo_script = fixtures_dir() / "echo_mcp.py"
            result = subprocess.run(
                [
                    "uv", "run", "mms", "add", "echo",
                    "--config", str(config_path),
                    "--command", "uv",
                    "--args", f"run python {echo_script}",
                    "--prefix", "echo",
                ],
                capture_output=True, text=True, check=True,
            )
            print(result.stdout.strip())
            """
        ),
        _md(
            """
            ### 2b. Inspect what was registered

            `mms list` prints a compact table of every configured
            upstream. Try running this *after* a second `mms add` in
            your own experimenting — it's the fastest way to see at a
            glance what STM will load when it starts.
            """
        ),
        _code(
            """
            result = subprocess.run(
                ["uv", "run", "mms", "list", "--config", str(config_path)],
                capture_output=True, text=True, check=True,
            )
            print(result.stdout.strip())
            """
        ),
        _md(
            """
            ## 3. MCP side — STM as a live server

            Now switch perspectives. Every `memtomem-stm` subprocess
            you spawn reads the same config file you just wrote,
            launches the upstreams it finds there, and exposes their
            tools (plus its own `stm_proxy_*` / `stm_surfacing_*`
            control tools) over the MCP stdio transport. From here on
            the CLI is out of the picture — you're talking to the
            proxy directly as an MCP client, exactly like Claude Code
            or Cursor would.

            `stm_session()` (from `_helpers.py`) is a thin async
            context manager that spawns `memtomem-stm`, wraps stdio in
            an MCP `ClientSession`, and yields it initialized.
            """
        ),
        _code(
            """
            async with stm_session() as session:
                tools_response = await session.list_tools()
                tool_names = sorted(t.name for t in tools_response.tools)
                print(f"STM exposes {len(tool_names)} tools to MCP clients:")
                for name in tool_names:
                    print(f"  {name}")

                # Call the proxied tool we registered via the CLI above.
                echo_result = await session.call_tool(
                    "echo__say", {"text": "hello from an MCP client"}
                )
                print()
                print(f"echo__say → {extract_text(echo_result)}")

                # And read the stats that recorded it.
                stats = await session.call_tool("stm_proxy_stats", {})
                print()
                print("stm_proxy_stats output:")
                print(extract_text(stats))
            """
        ),
        _md(
            """
            Two families of tools appeared:

            - **`echo__say`** — the echo fixture's own tool,
              namespaced with the `--prefix echo` you passed to
              `mms add`. STM discovered this dynamically when it
              connected to the upstream.
            - **`stm_proxy_*` / `stm_surfacing_*`** — STM's own
              built-in tools (proxy stats, health, selective chunk
              retrieval, surfacing feedback, etc.) — present on every
              instance regardless of what's registered.

            The CLI wrote the upstream entry. The MCP server read it,
            spawned the subprocess, merged the upstream's tools into
            its own catalog, and served the whole catalog to you. That
            hand-off is the entire hybrid model.

            ## 4. Optional — same tools, a LangChain agent's perspective

            Everything above used the raw MCP client. In practice you
            put a real agent framework in front of STM so the tool
            calls happen as part of an autonomous loop. The cell below
            shows the minimal LangChain `create_agent` integration —
            notebook 04 has the full walkthrough including tool
            streaming and result inspection.

            This cell **does not run** unless you have an API key. It
            checks for `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` and
            short-circuits if neither is set, so the notebook stays
            runnable in CI.
            """
        ),
        _code(
            """
            import os

            _has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
            _has_openai = bool(os.environ.get("OPENAI_API_KEY"))
            _HAS_KEY = _has_anthropic or _has_openai

            if not _HAS_KEY:
                print(
                    "Note: neither ANTHROPIC_API_KEY nor OPENAI_API_KEY is set.\\n"
                    "      The next cell will render but skip the agent call.\\n"
                    "      Set a key and re-run to see a LangChain agent drive\\n"
                    "      the same echo__say tool. Notebook 04 has the full\\n"
                    "      LangChain walkthrough."
                )
            else:
                print(f"Anthropic key: {'yes' if _has_anthropic else 'no'}")
                print(f"OpenAI key:    {'yes' if _has_openai else 'no'}")
            """
        ),
        _code(
            """
            agent_answer = None

            if _HAS_KEY:
                from mcp import ClientSession
                from mcp.client.stdio import StdioServerParameters, stdio_client
                from langchain_mcp_adapters.tools import load_mcp_tools
                from langchain.agents import create_agent

                model_id = "anthropic:claude-sonnet-4-5" if _has_anthropic else "openai:gpt-4.1-mini"
                print(f"Using model: {model_id}")

                # stdio_client's default errlog is sys.stderr, which is a
                # Jupyter OutStream without a usable fileno() — open /dev/null
                # explicitly so Popen can attach it to the child.
                params = StdioServerParameters(
                    command="memtomem-stm", args=[], env=dict(os.environ),
                )
                with open(os.devnull, "w") as _devnull:
                    async with stdio_client(params, errlog=_devnull) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await load_mcp_tools(session)
                            agent = create_agent(model_id, tools)

                            result = await agent.ainvoke({
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": (
                                            "Call the echo__say tool with the text "
                                            "'hi from a langchain agent' and tell me "
                                            "what the tool returned, in one sentence."
                                        ),
                                    }
                                ]
                            })

                            last = result["messages"][-1]
                            content = getattr(last, "content", last)
                            if isinstance(content, list):
                                content = " ".join(
                                    part.get("text", str(part)) if isinstance(part, dict) else str(part)
                                    for part in content
                                )
                            agent_answer = str(content)

            if agent_answer is None:
                print("(skipped — no API key)")
            else:
                print(f"Agent said: {agent_answer}")
            """
        ),
        _md(
            """
            ## Recap

            You just watched the same tool flow through both interfaces:

            1. **CLI time** — `mms add echo` wrote an entry in an
               isolated `stm_proxy.json`. `mms list` read it back.
            2. **MCP time** — `stm_session()` spawned `memtomem-stm`,
               which loaded that same JSON, launched the echo fixture
               as a subprocess, and served `echo__say` to you as a
               proxied tool. `stm_proxy_stats` recorded the call.
            3. **Agent time (optional)** — a LangChain
               `create_agent` wrapped the same catalog and drove it
               autonomously.

            Three interfaces, one config file, one running STM process.
            Every notebook from here on skips straight to the MCP
            client view; refer back to this one whenever the CLI
            ↔ MCP hand-off feels fuzzy.

            ## Where to next

            - **Notebook 01** — deeper dive into the MCP client side
              with `stm_proxy_stats` interpretation.
            - **Notebook 02** — selective compression: turning an
              18 KB structured response into a 1.5 KB TOC.
            - **Notebook 03** — proactive memory surfacing with a
              fake LTM.
            - **Notebook 04** — full LangChain / LangGraph agent
              integration.
            """
        ),
    ]
    _save(nb, "00_cli_and_mcp_hybrid.ipynb")


# ---------------------------------------------------------------------------
# Notebook 01 — Quickstart: Proxy a tool through STM
# ---------------------------------------------------------------------------


def build_nb01() -> None:
    nb = nbf.v4.new_notebook()
    nb.cells = [
        _md(
            """
            # 01 — Quickstart: Proxy a tool through STM

            This notebook walks you through the minimum viable memtomem-stm
            setup: register one upstream MCP server, talk to STM as an MCP
            client, and read the proxy stats.

            **You will learn:**

            - How to register an upstream MCP server with the `mms` CLI
            - How STM exposes proxied tools (namespaced with a prefix)
            - How to call tools via the MCP Python client
            - How to read `stm_proxy_stats` to see what STM did

            **Prereqs:** `uv sync` (installs the dev group including Jupyter).
            No external services required — we use a trivial echo server
            shipped under `_fixtures/`.

            **No state is leaked.** Every notebook isolates its proxy config,
            cache, and metrics into a temp directory via environment
            variables. Your real `~/.memtomem/` is untouched.
            """
        ),
        _md("## 1. Isolate state and import helpers"),
        _code(_BOOTSTRAP),
        _code(
            """
            import subprocess

            from _helpers import (
                isolate_stm_state,
                stm_session,
                extract_text,
                fixtures_dir,
            )

            config_path = isolate_stm_state(prefix="nb01_")
            print(f"STM config → {config_path}")
            print(f"Fixtures   → {fixtures_dir()}")
            """
        ),
        _md(
            """
            ## 2. Register an upstream MCP server

            We'll point STM at a tiny echo server that ships with the
            repo. In production you would use `--command npx --args
            "-y @modelcontextprotocol/server-filesystem /your/path"` or
            any other MCP server — the mechanics are identical.
            """
        ),
        _code(
            """
            echo_script = fixtures_dir() / "echo_mcp.py"
            result = subprocess.run(
                [
                    "uv", "run", "mms", "add", "echo",
                    "--config", str(config_path),
                    "--command", "uv",
                    "--args", f"run python {echo_script}",
                    "--prefix", "echo",
                ],
                capture_output=True, text=True, check=True,
            )
            print(result.stdout.strip())
            """
        ),
        _code(
            """
            # Show what mms wrote to the config file
            print(config_path.read_text())
            """
        ),
        _md(
            """
            ## 3. Connect to STM as an MCP client

            `stm_session()` spawns `memtomem-stm` as a subprocess using
            the stdio transport, wraps it in an MCP `ClientSession`, and
            initializes the connection. This is exactly what Claude Code
            or Cursor do under the hood.

            > **Heads-up:** STM logs its lifecycle to stderr — those
            > `INFO` lines you see interleaved below are normal and show
            > the proxy manager connecting to the echo upstream.
            """
        ),
        _code(
            """
            async with stm_session() as session:
                tools_response = await session.list_tools()
                tool_names = sorted(t.name for t in tools_response.tools)
                print(f"STM exposes {len(tool_names)} tools:")
                for name in tool_names:
                    print(f"  {name}")
            """
        ),
        _md(
            """
            Two groups of tools appear above:

            - **`echo__*`** — the upstream echo server's tools, namespaced
              with the `--prefix echo` you passed to `mms add`. STM
              dynamically discovers these when it starts.
            - **`stm_proxy_*` / `stm_surfacing_*`** — STM's own built-in
              control tools (stats, health, selective chunk retrieval,
              progressive read, cache clear, surfacing feedback).

            ## 4. Call the proxied tool
            """
        ),
        _code(
            """
            async with stm_session() as session:
                result = await session.call_tool("echo__say", {"text": "hello from the notebook"})
                print(extract_text(result))
            """
        ),
        _md("## 5. Read the proxy stats"),
        _code(
            """
            async with stm_session() as session:
                # Trigger one real call so the stats show non-zero activity
                await session.call_tool("echo__say", {"text": "counting this one"})
                stats = await session.call_tool("stm_proxy_stats", {})
                print(extract_text(stats))
            """
        ),
        _md(
            """
            ## Recap

            You just routed a tool call through STM:

            1. `mms add` wrote an entry in an isolated `stm_proxy.json`
            2. STM loaded that config on startup and spawned `echo_mcp.py`
               as a subprocess
            3. Your `session.call_tool("echo__say", ...)` call flowed
               `notebook → STM → echo → STM → notebook`
            4. STM's `TokenTracker` recorded the call in its stats

            The echo server returns tiny responses, so no compression fires
            yet. **Notebook 02** swaps in a large structured document and
            shows STM's selective compression producing a table of contents.
            """
        ),
    ]
    _save(nb, "01_quickstart_proxy_setup.ipynb")


# ---------------------------------------------------------------------------
# Notebook 02 — Compression: Selective TOC and chunk selection
# ---------------------------------------------------------------------------


def build_nb02() -> None:
    nb = nbf.v4.new_notebook()
    nb.cells = [
        _md(
            """
            # 02 — Compression: Selective TOC and chunk selection

            In notebook 01 you saw STM proxy a tiny response. Real upstream
            MCP servers return much bigger payloads — full files, long API
            responses, large directory listings — and those burn through
            the agent's context window fast.

            STM has **10 compression strategies**. This notebook focuses
            on the most useful one for structured data: **selective
            compression**, which returns a *table of contents* and lets
            the agent request only the sections it needs via
            `stm_proxy_select_chunks`.

            **You will learn:**

            - How STM turns a ~18 KB structured response into a ~1.5 KB TOC
            - How to parse the TOC to discover section keys
            - How to retrieve specific sections on demand
            - How to measure the token savings

            **Prereqs:** Notebook 01 completed (or re-run its setup).
            """
        ),
        _md("## 1. Isolate state and register the `docfix` upstream"),
        _code(_BOOTSTRAP),
        _code(
            """
            import json
            import subprocess

            from _helpers import (
                isolate_stm_state,
                stm_session,
                extract_text,
                fixtures_dir,
                parse_toc_response,
                token_estimate,
            )

            config_path = isolate_stm_state(prefix="nb02_")
            doc_script = fixtures_dir() / "doc_mcp.py"

            # --compression selective forces STM to use the selective
            # compressor for this upstream regardless of content shape.
            # --max-chars sets a tight budget (1000 chars) so even a
            # medium-sized response triggers TOC generation.
            subprocess.run(
                [
                    "uv", "run", "mms", "add", "docfix",
                    "--config", str(config_path),
                    "--command", "uv",
                    "--args", f"run python {doc_script}",
                    "--prefix", "docfix",
                    "--compression", "selective",
                    "--max-chars", "1000",
                ],
                check=True, capture_output=True,
            )
            print("Registered docfix (selective, max 1000 chars)")
            """
        ),
        _md(
            """
            ## 2. Call the tool and capture the TOC

            The `docfix__get_document` tool returns a JSON object with 8
            labeled sections totaling ~18 KB. Without STM the agent would
            have to eat all of that. With STM in the middle, the selective
            compressor intercepts the response and returns a compact TOC.
            """
        ),
        _code(
            """
            async with stm_session() as session:
                result = await session.call_tool("docfix__get_document", {})
                raw_text = extract_text(result)

            print(f"Response length: {len(raw_text)} chars  (~{token_estimate(raw_text)} tokens)")
            print()
            print(raw_text[:500])
            print("...")
            """
        ),
        _code(
            """
            # Parse the TOC and show its structure
            toc = parse_toc_response(raw_text)
            if toc is None:
                raise RuntimeError("Expected a TOC response — check compression config")

            print(f"Type:          {toc['type']}")
            print(f"Selection key: {toc['selection_key']}")
            print(f"Total chars:   {toc.get('total_chars', 'n/a')}  (before compression)")
            print(f"Entries ({len(toc['entries'])}):")
            for entry in toc["entries"]:
                size = entry.get("size", 0)
                key = entry.get("key", "?")
                print(f"  - {key:<15} ({size} chars)")
            """
        ),
        _md(
            """
            ## 3. Retrieve selected sections

            Now the agent has enough information to pick what it actually
            needs. `stm_proxy_select_chunks` takes the `selection_key` from
            the TOC and a list of section keys and returns just those
            sections — zero information loss for the parts you pick,
            zero tokens spent on the rest.
            """
        ),
        _code(
            """
            picked_sections = ["Selective", "Progressive"]

            async with stm_session() as session:
                select_result = await session.call_tool(
                    "stm_proxy_select_chunks",
                    {"key": toc["selection_key"], "sections": picked_sections},
                )
                selected_text = extract_text(select_result)

            print(f"Selected {len(picked_sections)} sections "
                  f"({len(selected_text)} chars, ~{token_estimate(selected_text)} tokens)")
            print()
            print(selected_text[:800])
            print("..." if len(selected_text) > 800 else "")
            """
        ),
        _md(
            """
            ## 4. Compare: original vs TOC vs selected

            Here's what just happened in token terms:
            """
        ),
        _code(
            """
            original_chars = toc.get("total_chars", 0)
            toc_chars = len(raw_text)
            selected_chars = len(selected_text)

            print("| Stage                       |    Chars |   ~Tokens |   Ratio |")
            print("|-----------------------------|---------:|----------:|--------:|")
            print(f"| Original document           | {original_chars:>8} | {token_estimate(str(original_chars)*original_chars)[:1] if False else token_estimate('x' * original_chars):>9} | 100.00% |")
            print(f"| Compressed (TOC)            | {toc_chars:>8} | {token_estimate('x' * toc_chars):>9} | {toc_chars/original_chars*100:>5.2f}% |")
            print(f"| Selected 2/8 sections       | {selected_chars:>8} | {token_estimate('x' * selected_chars):>9} | {selected_chars/original_chars*100:>5.2f}% |")
            print()
            savings = 100 * (1 - toc_chars / original_chars)
            print(f"First-contact savings:     {savings:>5.1f}% — what the agent sees first")
            print(f"After targeted retrieval:  {100 * (1 - selected_chars/original_chars):>5.1f}% savings vs the full doc")
            print(f"                           (but 100% of the info it actually asked for)")
            """
        ),
        _md(
            """
            ## Where to next

            - **Progressive delivery** (`stm_proxy_read_more`) — for
              unstructured large responses that don't have natural
              sections, STM can chunk them and let the agent walk
              through with a cursor. See
              [`docs/compression.md`](../docs/compression.md).
            - **The other 9 strategies** — auto-selection, hybrid,
              truncate, extract_fields, schema_pruning, skeleton,
              llm_summary, none — all explained in the same doc.
            - **Notebook 03** — proactive memory surfacing: STM calling
              a long-term memory server and injecting relevant chunks
              into responses.
            """
        ),
    ]
    _save(nb, "02_compression_and_selective.ipynb")


# ---------------------------------------------------------------------------
# Notebook 03 — Memory surfacing with a fake LTM
# ---------------------------------------------------------------------------


def build_nb03() -> None:
    nb = nbf.v4.new_notebook()
    nb.cells = [
        _md(
            """
            # 03 — Memory surfacing with a fake LTM

            STM's headline feature is **proactive memory surfacing**:
            when an upstream tool returns, STM queries a long-term
            memory (LTM) server in the background and injects the most
            relevant chunks at the top of the response. The agent sees
            a single enriched response; no extra tool call needed.

            This notebook shows surfacing end-to-end **without requiring
            a real `memtomem-server`** — we point STM at
            `notebooks/_fixtures/fake_ltm.py`, a notebook-local stub
            that returns canned memories with per-call UUIDs so repeated
            runs stay deterministic and aren't silently suppressed by
            STM's cross-session dedup.

            **You will learn:**

            - How STM's surfacing engine talks to an LTM via MCP
            - What an injected `<surfaced-memories>` block looks like
            - How to send feedback back to STM with
              `stm_surfacing_feedback`
            - Where to see surfacing counters in the stats

            **Prereqs:** Notebook 01 completed.
            """
        ),
        _md("## 1. Isolate state and point surfacing at the fake LTM"),
        _code(_BOOTSTRAP),
        _code(
            """
            import re
            import subprocess

            from _helpers import (
                isolate_stm_state,
                configure_fake_ltm,
                stm_session,
                extract_text,
                fixtures_dir,
            )

            # enable_surfacing=True leaves the surfacing env knobs alone
            # so that configure_fake_ltm() can point them at the fake server.
            config_path = isolate_stm_state(prefix="nb03_", enable_surfacing=True)
            fake_ltm = configure_fake_ltm()
            print(f"STM config → {config_path}")
            print(f"Fake LTM   → {fake_ltm}")
            """
        ),
        _md(
            """
            > **Note:** In a real setup you would replace the fake LTM
            > with a running `memtomem-server` process pointed at your
            > actual memory store. The MCP protocol keeps the interface
            > identical, so switching between fake and real is a single
            > env var change.
            """
        ),
        _md("## 2. Register a small upstream and talk to it via STM"),
        _code(
            """
            doc_script = fixtures_dir() / "doc_mcp.py"
            subprocess.run(
                [
                    "uv", "run", "mms", "add", "docfix",
                    "--config", str(config_path),
                    "--command", "uv",
                    "--args", f"run python {doc_script}",
                    "--prefix", "docfix",
                    "--compression", "none",  # disable compression to highlight surfacing
                ],
                check=True, capture_output=True,
            )
            print("Registered docfix (compression disabled — we want raw + surfacing)")
            """
        ),
        _code(
            """
            async with stm_session() as session:
                result = await session.call_tool("docfix__describe", {})
                enriched = extract_text(result)

            print(enriched)
            """
        ),
        _md(
            """
            ## 3. What just happened

            The `docfix__describe` tool's own output is just:

            ```
            docfix fixture — 8 sections: Overview, Installation, ...
            ```

            But what came back above starts with a
            `<surfaced-memories>` block containing two chunks from the
            fake LTM — scored, labeled, and prepended to the real
            response. STM did this without the agent asking. Notice
            the **Surfacing ID** at the bottom of the block — that's
            how the agent rates the surfacing.
            """
        ),
        _md("## 4. Send feedback"),
        _code(
            """
            # Extract the surfacing ID from the response
            match = re.search(r"Surfacing ID: ([a-f0-9]+)", enriched)
            if not match:
                raise RuntimeError("Surfacing ID not found — surfacing did not fire")
            surfacing_id = match.group(1)
            print(f"Surfacing ID: {surfacing_id}")

            async with stm_session() as session:
                feedback_result = await session.call_tool(
                    "stm_surfacing_feedback",
                    {"surfacing_id": surfacing_id, "rating": "helpful"},
                )
                print(extract_text(feedback_result))
            """
        ),
        _md(
            """
            Over time, `"helpful"` feedback raises the confidence of the
            retrieved memory chunks via STM's auto-tuning loop, and
            `"not_relevant"` pulls them down. Agents that call the
            feedback tool get progressively more relevant surfacing.
            See [`docs/surfacing.md`](../docs/surfacing.md) for the
            scoring details.

            ## 5. Check the surfacing counters
            """
        ),
        _code(
            """
            async with stm_session() as session:
                stats_result = await session.call_tool("stm_surfacing_stats", {})
                print(extract_text(stats_result))
            """
        ),
        _md(
            """
            ## Where to next

            - **Horizontal scaling (scenario 5 from the docs)** — STM
              can share a SQLite-backed pending store across multiple
              instances so that an agent can start a selective
              compression on instance A and resolve it on instance B.
              Not demonstrable in a single-process notebook; see
              [`docs/operations.md#horizontal-scaling`](../docs/operations.md#horizontal-scaling).
            - **Notebook 04** — put a real LangChain / LangGraph agent
              in front of STM using `create_agent` and
              `langchain-mcp-adapters`. That notebook needs an Anthropic
              or OpenAI API key, so it's skipped in CI by default.
            """
        ),
    ]
    _save(nb, "03_memory_surfacing.ipynb")


# ---------------------------------------------------------------------------
# Notebook 04 — LangChain / LangGraph agent integration
# ---------------------------------------------------------------------------


def build_nb04() -> None:
    nb = nbf.v4.new_notebook()
    nb.cells = [
        _md(
            """
            # 04 — LangChain agent integration *(optional, needs API key)*

            Notebooks 01–03 talked to STM using the raw MCP client so
            you could see every wire-level call. That's great for
            understanding how STM behaves, but it's not how you would
            *use* STM day-to-day. In practice you put a real agent
            framework in front of it.

            This notebook shows the simplest real-world integration:
            **LangChain `create_agent` + `langchain-mcp-adapters`**.
            `create_agent` is LangChain v1's canonical agent factory
            and returns a LangGraph graph under the hood, so using it
            also exercises LangGraph.

            **You will learn:**

            - How `langchain-mcp-adapters.load_mcp_tools` converts
              STM's proxied tools into LangChain `BaseTool` instances
            - How to build a ReAct-style agent with `create_agent` and
              run it end-to-end
            - How to verify STM saw every tool call by reading its
              stats after the agent finishes

            **Prereqs:**

            - `uv sync --extra langchain` — installs
              `langchain`, `langchain-mcp-adapters`, and
              `langchain-anthropic`
            - `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` set in your
              environment (the notebook auto-detects and skips if
              neither is present)

            **CI behavior:** This notebook is **excluded from CI** — no
            API keys in CI. Run it manually when you want to see the
            full agent loop.
            """
        ),
        _md(
            """
            ## 1. Detect API key

            This notebook needs an LLM API key (Anthropic or OpenAI) to
            actually run the agent. The cell below sets a ``_HAS_KEY``
            flag; every downstream cell short-circuits if the key is
            missing so the notebook still renders cleanly without one.
            CI does not run this notebook at all.
            """
        ),
        _code(
            """
            import os

            _has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
            _has_openai = bool(os.environ.get("OPENAI_API_KEY"))
            _HAS_KEY = _has_anthropic or _has_openai

            if not _HAS_KEY:
                print(
                    "⚠️  Neither ANTHROPIC_API_KEY nor OPENAI_API_KEY is set.\\n"
                    "   This notebook will render its cells but skip the\\n"
                    "   actual agent invocation. Set a key and re-run to\\n"
                    "   execute the full flow:\\n"
                    "     export ANTHROPIC_API_KEY=sk-ant-...\\n"
                    "     export OPENAI_API_KEY=sk-..."
                )
            else:
                print(f"Anthropic key: {'yes' if _has_anthropic else 'no'}")
                print(f"OpenAI key:    {'yes' if _has_openai else 'no'}")
            """
        ),
        _md("## 2. Isolate state and bootstrap helpers"),
        _code(_BOOTSTRAP),
        _code(
            """
            import subprocess
            import tempfile
            from pathlib import Path

            from _helpers import (
                isolate_stm_state,
                stm_session,
                extract_text,
                fixtures_dir,
            )

            config_path = isolate_stm_state(prefix="nb04_")

            # Create a tiny sandbox directory with a couple of files
            # for the agent to list and read.
            sandbox = Path(tempfile.mkdtemp(prefix="nb04_sandbox_"))
            (sandbox / "notes.md").write_text(
                "# Notes\\n\\nmemtomem-stm compresses noisy MCP responses.\\n"
                "It also injects relevant long-term memories proactively.\\n"
            )
            (sandbox / "TODO.txt").write_text("- Write more notebooks\\n- Ship v0.2\\n")
            print(f"Sandbox: {sandbox}")
            print(f"Files:   {sorted(p.name for p in sandbox.iterdir())}")
            """
        ),
        _md(
            """
            ## 3. Register the filesystem MCP server as a docfix fallback

            For full reproducibility without external NPM packages, we
            reuse the `docfix` fixture from notebook 02 instead of the
            official `@modelcontextprotocol/server-filesystem`. The
            agent will ask about it using natural language and STM will
            compress the long response transparently.
            """
        ),
        _code(
            """
            doc_script = fixtures_dir() / "doc_mcp.py"
            subprocess.run(
                [
                    "uv", "run", "mms", "add", "docfix",
                    "--config", str(config_path),
                    "--command", "uv",
                    "--args", f"run python {doc_script}",
                    "--prefix", "docfix",
                    "--compression", "selective",
                    "--max-chars", "1500",
                ],
                check=True, capture_output=True,
            )
            print("Registered docfix (selective compression)")
            """
        ),
        _md(
            """
            ## 4. Load STM's tools into a LangChain agent

            The key bridge here is `load_mcp_tools(session)` from
            `langchain-mcp-adapters`. It introspects the MCP session,
            reads every tool's JSON schema, and wraps each one as a
            LangChain `BaseTool` that `create_agent` can consume.
            """
        ),
        _code(
            """
            result = None
            stats_text = "(skipped — no API key)"

            if _HAS_KEY:
                from mcp import ClientSession
                from mcp.client.stdio import StdioServerParameters, stdio_client
                from langchain_mcp_adapters.tools import load_mcp_tools
                from langchain.agents import create_agent

                model_id = "anthropic:claude-sonnet-4-5" if _has_anthropic else "openai:gpt-4.1-mini"
                print(f"Using model: {model_id}")

                # stdio_client / ClientSession must stay open for the lifetime
                # of agent.ainvoke — LangChain tool calls reach back into the
                # live MCP session. So we do everything inside one async block.
                params = StdioServerParameters(
                    command="memtomem-stm", args=[], env=dict(os.environ),
                )
                import os as _os
                with open(_os.devnull, "w") as _devnull:
                    async with stdio_client(params, errlog=_devnull) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await load_mcp_tools(session)
                            print(f"Loaded {len(tools)} tools from STM into LangChain")
                            for t in tools[:8]:
                                print(f"  - {t.name}")
                            if len(tools) > 8:
                                print(f"  ... and {len(tools) - 8} more")

                            agent = create_agent(model_id, tools)

                            result = await agent.ainvoke({
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": (
                                            "Use the docfix__get_document tool to list what "
                                            "sections are available, then use stm_proxy_select_chunks "
                                            "to retrieve just the 'Selective' and 'Surfacing' sections "
                                            "and summarize them in 2 sentences."
                                        ),
                                    }
                                ]
                            })

                            # Capture stats before the session closes
                            stats_raw = await session.call_tool("stm_proxy_stats", {})
                            stats_text = extract_text(stats_raw)
            else:
                print("(skipped — set ANTHROPIC_API_KEY or OPENAI_API_KEY to run)")
            """
        ),
        _md("## 5. What the agent did"),
        _code(
            """
            if result is None:
                print("(no result — notebook ran without an API key)")
            else:
                # result["messages"] is the full conversation including tool calls
                for msg in result["messages"]:
                    role = getattr(msg, "type", type(msg).__name__)
                    content = getattr(msg, "content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            part.get("text", str(part)) if isinstance(part, dict) else str(part)
                            for part in content
                        )
                    snippet = (str(content)[:240] + "...") if len(str(content)) > 240 else str(content)
                    print(f"[{role}] {snippet}")
                    print()
            """
        ),
        _md("## 6. And what STM saw"),
        _code(
            """
            print(stats_text)
            """
        ),
        _md(
            """
            ## Recap

            You just ran a LangChain ReAct agent whose entire tool
            surface came from STM. Every call the agent made — reading
            the document, selecting chunks, checking stats — flowed
            through STM's pipeline: cleaned, compressed, cached,
            counted. The agent code didn't know STM exists.

            **What to try next:**

            - Replace the `docfix` fixture with a real upstream like
              `@modelcontextprotocol/server-filesystem` or
              `@modelcontextprotocol/server-github`
            - Add a second `mms add` call for a second upstream and
              watch `load_mcp_tools` discover both
            - Swap `create_agent` for a custom `StateGraph` from
              LangGraph if you need more control over the loop
            - Enable surfacing (see notebook 03) and watch memories
              appear in the agent's observations automatically

            ## Where to next

            - [LangChain `create_agent` docs](https://docs.langchain.com/oss/python/langchain/agents)
            - [`langchain-mcp-adapters` on PyPI](https://pypi.org/project/langchain-mcp-adapters/)
            - [`docs/configuration.md`](../docs/configuration.md) — env
              vars and config file reference for tuning STM
            """
        ),
    ]
    _save(nb, "04_langchain_agent_integration.ipynb")


# ---------------------------------------------------------------------------
# Notebook 05 — Observability and Langfuse Tracing
# ---------------------------------------------------------------------------


def build_nb05() -> None:
    nb = nbf.v4.new_notebook()
    nb.cells = [
        _md(
            """
            # 05 — Observability and Langfuse Tracing

            memtomem-stm provides three layers of observability:

            1. **MCP tools** — `stm_proxy_stats`, `stm_proxy_health`,
               `stm_surfacing_stats` give you an agent-accessible summary
               of what the proxy is doing.
            2. **SQLite metrics** — `proxy_metrics.db` and
               `stm_feedback.db` persist per-call metrics for offline
               analysis.
            3. **Langfuse tracing** — optional nested spans that let you
               visualize the full proxy pipeline in a shared team UI.

            This notebook demonstrates all three layers. Core cells run
            without any API keys; the live Langfuse cells gate themselves
            on `_HAS_LANGFUSE_KEY`.

            **Prereqs:** `uv sync` (dev group). Optional:
            `pip install "memtomem-stm[langfuse]"` plus Langfuse
            credentials for live tracing.
            """
        ),
        _md("## 1. Isolate state and import helpers"),
        _code(_BOOTSTRAP),
        _code(
            """
            import os
            import subprocess

            from _helpers import (
                isolate_stm_state,
                stm_session,
                extract_text,
                fixtures_dir,
            )

            config_path = isolate_stm_state(prefix="nb05_")
            print(f"STM config → {config_path}")
            """
        ),
        _md(
            """
            ## 2. Register a fixture server and make a call

            We need proxy calls to generate stats. Let's add the echo
            fixture and make a quick call.
            """
        ),
        _code(
            """
            fixture_script = str(fixtures_dir() / "echo_mcp.py")
            subprocess.run(
                ["mms", "add", "echo", "--command", "uv", "--args",
                 f"run,python,{fixture_script}", "--prefix", "echo"],
                check=True,
                capture_output=True,
            )
            print("Registered echo fixture.")

            async with stm_session() as session:
                result = await session.call_tool("echo__say", {"text": "hello observability"})
                print(extract_text(result))
            """
        ),
        _md(
            """
            ## 3. Built-in observability: `stm_proxy_stats`

            The simplest way to check what STM is doing — call the
            `stm_proxy_stats` MCP tool from inside a session.
            """
        ),
        _code(
            """
            async with stm_session() as session:
                stats = await session.call_tool("stm_proxy_stats", {})
                print(extract_text(stats))
            """
        ),
        _md(
            """
            ## 4. Built-in observability: `stm_proxy_health`

            Check upstream connectivity and circuit-breaker state.
            """
        ),
        _code(
            """
            async with stm_session() as session:
                health = await session.call_tool("stm_proxy_health", {})
                print(extract_text(health))
            """
        ),
        _md(
            """
            ## 5. What Langfuse tracing adds

            The MCP tools give you a snapshot; SQLite gives you history.
            Langfuse adds **live, nested span visualization** across your
            entire proxy pipeline:

            | Span name | When | Metadata |
            |---|---|---|
            | `proxy_call` | Every proxy invocation | `server`, `tool`, `trace_id` |
            | `proxy_call_clean` | Content cleaning | `server`, `tool` |
            | `proxy_call_compress` | Compression | `server`, `tool`, `strategy` |
            | `proxy_call_surface` | Memory injection | `server`, `tool` |
            | `proxy_call_index` | Auto-indexing | `server`, `tool` |
            | `stm_surfacing_feedback` | Feedback tool | `surfacing_id`, `rating` |
            | `stm_surfacing_stats` | Stats query | `tool` |

            Every span is a no-op `nullcontext` when Langfuse is disabled
            — zero overhead.
            """
        ),
        _md(
            """
            ## 6. Enabling Langfuse

            Set these environment variables before starting the STM server:

            ```bash
            export MEMTOMEM_STM_LANGFUSE__ENABLED=true
            export MEMTOMEM_STM_LANGFUSE__PUBLIC_KEY=pk-lf-...
            export MEMTOMEM_STM_LANGFUSE__SECRET_KEY=sk-lf-...
            export MEMTOMEM_STM_LANGFUSE__HOST=https://cloud.langfuse.com
            ```

            For self-hosted Langfuse, point `HOST` at `http://localhost:3000`.
            """
        ),
        _code(
            """
            _HAS_LANGFUSE_KEY = bool(
                os.environ.get("LANGFUSE_PUBLIC_KEY")
                or os.environ.get("MEMTOMEM_STM_LANGFUSE__PUBLIC_KEY")
            )
            if _HAS_LANGFUSE_KEY:
                print("Langfuse credentials detected — live tracing cells will run.")
            else:
                print("No Langfuse credentials. Live tracing cells will be skipped.")
                print("Set MEMTOMEM_STM_LANGFUSE__PUBLIC_KEY to enable.")
            """
        ),
        _md(
            """
            ## 7. Sampling configuration

            For high-throughput deployments, trace only a fraction of calls:

            ```bash
            export MEMTOMEM_STM_LANGFUSE__SAMPLING_RATE=0.1  # trace 10%
            ```

            Default is `1.0` (trace all). SQLite metrics are always
            recorded regardless of sampling — they are never skipped.
            """
        ),
        _md(
            """
            ## 8. Trace context propagation

            When STM forwards calls to upstream servers, it includes a
            `_trace_id` field in the tool arguments. Upstream servers can
            extract this to correlate their own spans with the originating
            STM trace. The same mechanism works for LTM searches via the
            `McpClientSearchAdapter`.

            This makes end-to-end distributed tracing possible across the
            full `agent → STM proxy → upstream server` pipeline.
            """
        ),
        _md(
            """
            ## Recap

            | Layer | What you get | When to use |
            |---|---|---|
            | MCP tools | Live snapshot, agent-accessible | Quick checks |
            | SQLite | Full history, offline analysis | Debugging, tuning |
            | Langfuse | Nested spans, team-shared UI | Production monitoring |

            **Where to next:**

            - [`docs/operations.md`](../docs/operations.md) — full
              observability reference (span table, data storage, safety)
            - [`docs/configuration.md`](../docs/configuration.md) — env
              vars for Langfuse and all other settings
            - [Langfuse docs](https://langfuse.com/docs) — dashboard
              setup and SDK reference
            """
        ),
    ]
    _save(nb, "05_observability_and_langfuse.ipynb")


if __name__ == "__main__":
    build_nb00()
    build_nb01()
    build_nb02()
    build_nb03()
    build_nb04()
    build_nb05()
