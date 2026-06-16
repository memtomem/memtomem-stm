"""Tiny stdio MCP server used by the ToolgraphConsultAdapter tests.

Stands in for the real tool-graph MCP server (``toolgraph serve``) so that
STM's :class:`~memtomem_stm.proxy.toolgraph_provider.ToolgraphConsultAdapter`
can be exercised end-to-end over a real stdio child without depending on the
external package or a running Neo4j. It exposes the one tool the adapter
calls — ``eligible_tools`` — returning the real structured shape
(``{agent, agent_found, profile, eligible, rejected, graph_generation}``).

Returning a bare ``-> dict`` means mcp serializes the verdict into a
``TextContent`` JSON payload (mcp 1.27.x does not emit ``structuredContent``
for an untyped dict return), which is exactly the production wire shape the
adapter parses.

Input-driven behaviors (so one fixture covers every adapter path):

* ``profile == "boom"`` raises (unknown-profile mirror) → ``isError`` →
  the adapter raises ``ToolgraphProtocolError``.
* ``profile == "sleep"`` blocks far longer than any test timeout → the
  adapter's per-consult ``asyncio.wait_for`` fires → ``ToolgraphUnreachableError``.
* ``agent == "ghost"`` returns ``agent_found=False`` as a *structured*
  result (NOT an error) — the abort signal the adapter must surface as data.
* any candidate ending in ``"::blocked"`` comes back as a ``NOT_GRANTED``
  rejected row; a candidate whose tool part starts with ``"missing"`` comes
  back as a ``TOOL_NOT_FOUND`` rejected row (the graph's blind spot); all
  others are eligible, in input order.

Run with: ``python <path-to-this-file>``.
"""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-toolgraph")

# Arbitrary fixed monotonic graph generation so tests can assert the field is
# threaded through verbatim.
_GRAPH_GENERATION = 11


@mcp.tool()
async def eligible_tools(agent: str, candidates: list[str], profile: str = "strict") -> dict:
    """Canned ``eligible_tools`` consult mirroring the real structured shape."""
    if profile == "boom":
        raise ValueError(f"unknown profile {profile!r}")
    if profile == "sleep":
        await asyncio.sleep(30)
    if agent == "ghost":
        return {
            "agent": agent,
            "agent_found": False,
            "profile": profile,
            "eligible": [],
            "rejected": [],
            "graph_generation": _GRAPH_GENERATION,
        }

    eligible: list[str] = []
    rejected: list[dict] = []
    for candidate in candidates:  # input order is part of the upstream contract
        tool_part = candidate.split("::", 1)[-1]
        if candidate.endswith("::blocked"):
            rejected.append(
                {"candidate": candidate, "tool_key": candidate, "reason": "NOT_GRANTED"}
            )
        elif tool_part.startswith("missing"):
            rejected.append({"candidate": candidate, "tool_key": None, "reason": "TOOL_NOT_FOUND"})
        else:
            eligible.append(candidate)

    return {
        "agent": agent,
        "agent_found": True,
        "profile": profile,
        "eligible": eligible,
        "rejected": rejected,
        "graph_generation": _GRAPH_GENERATION,
    }


if __name__ == "__main__":
    mcp.run()
