"""FastMCP server returning a large structured document.

Used by notebook 02 to demonstrate STM's selective compression: the doc is
big enough to exceed the default ``max_result_chars`` budget, and it is
structured (markdown sections) so the selective compressor produces a TOC
response that the notebook can follow up on via ``stm_proxy_select_chunks``.

The content is deterministic — no randomness — so notebook cell outputs
are reproducible across runs.

Run with: ``python doc_mcp.py``
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("docfix")


# 8 labeled sections, each ~1.2KB → total ~10KB (well above the 8000-char
# default budget, so selective compression fires and produces a TOC).
_SECTIONS: dict[str, str] = {
    "Overview": (
        "memtomem-stm is a proxy gateway that sits between AI agents and "
        "upstream MCP servers. It compresses noisy responses, caches "
        "results, and surfaces relevant long-term memories — all without "
        "the agent knowing it's there.\n\n"
    )
    * 10,
    "Installation": (
        "Install with uv or pip. memtomem-stm has no Python-level "
        "dependency on memtomem core: LTM access happens over the MCP "
        "protocol, so you can point it at any compatible memory server.\n\n"
    )
    * 10,
    "Pipeline": (
        "Each upstream tool call flows through a four-stage pipeline: "
        "CLEAN strips noise, COMPRESS applies one of ten strategies, "
        "SURFACE injects relevant memories, and INDEX stores the result "
        "in the response cache for future hits.\n\n"
    )
    * 10,
    "Compression": (
        "Ten strategies are available: auto, truncate, extract_fields, "
        "schema_pruning, skeleton, llm_summary, selective, hybrid, "
        "progressive, and none. The auto strategy picks one based on "
        "content shape and the consumer model's context window.\n\n"
    )
    * 10,
    "Selective": (
        "Selective compression returns a TOC (table of contents) when "
        "the response is a structured document. The agent reads the TOC, "
        "then calls stm_proxy_select_chunks(key, sections) to retrieve "
        "only the sections it actually needs. Zero information is lost.\n\n"
    )
    * 10,
    "Progressive": (
        "Progressive delivery chunks very large responses and lets the "
        "agent walk through them with stm_proxy_read_more(key, offset). "
        "Unlike truncation, every byte remains accessible — the agent "
        "just sees a cursor-style interface.\n\n"
    )
    * 10,
    "Surfacing": (
        "When an upstream tool returns, STM optionally queries a "
        "long-term memory server for chunks relevant to the call and "
        "injects them at the top of the response. The feedback tool "
        "lets the agent rate helpfulness and auto-tune future scoring.\n\n"
    )
    * 10,
    "Operations": (
        "Circuit breakers isolate flaky upstreams, a SQLite-shared "
        "pending store supports horizontal scaling, and Langfuse tracing "
        "captures the full request path. None of this is visible to the "
        "agent — it only sees faster, cleaner, memory-aware responses.\n\n"
    )
    * 10,
}


@mcp.tool()
async def get_document() -> str:
    """Return the full structured document as a JSON object.

    Returning JSON (not markdown) guarantees that STM's selective
    compressor recognizes the structure and produces a TOC — markdown
    heading detection is best-effort, but a dict is unambiguous.
    """
    return json.dumps(_SECTIONS, indent=2)


@mcp.tool()
async def describe() -> str:
    """Return a short description of what this fixture exposes."""
    keys = ", ".join(_SECTIONS.keys())
    return f"docfix fixture — 8 sections: {keys}"


if __name__ == "__main__":
    mcp.run()
