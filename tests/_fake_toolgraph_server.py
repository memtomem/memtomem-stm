"""Tiny stdio MCP server used by the ToolgraphConsultAdapter tests.

Stands in for the real tool-graph MCP server (``toolgraph serve``) so that
STM's :class:`~memtomem_stm.proxy.toolgraph_provider.ToolgraphConsultAdapter`
can be exercised end-to-end over a real stdio child without depending on the
external package or a running Neo4j. It exposes the two tools the adapter
calls — ``eligible_tools`` (the hard-filter verdict) and ``rank_features`` (the
per-candidate ``risk_score`` enrichment, #493) — returning the real structured
shapes (``{agent, agent_found, profile, eligible, rejected, graph_generation}``
and ``{agent, agent_found, features, graph_generation}``).

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
* any candidate ending in ``"::blocked"`` (or whose tool part starts with
  ``"riskyblocked"``) comes back as a ``NOT_GRANTED`` rejected row; a candidate
  whose tool part starts with ``"missing"`` comes back as a ``TOOL_NOT_FOUND``
  rejected row (the graph's blind spot); all others are eligible, in input
  order.
* ``rank_features`` mirrors that resolution and returns the full real row —
  resolution, grant, verdict, classification, drift/mapping/evidence facts, the
  four annotation hints — with a ``risk_score`` per the real fixed table
  (``selector._risk_score``): a tool part starting with ``"risky"`` carries an
  unbacked load-bearing edge and so scores ``0.4`` (eligible-but-risky — the
  case PR #493 demotes), ``"missing*"`` scores ``None`` (unresolved, every other
  fact unknowable), and everything else — including ``"::blocked"``
  (NOT_GRANTED is grant-only, data-flow clean) — scores ``0.0``.
  ``"riskyblocked*"`` therefore lands in BOTH (rejected by ``eligible_tools``
  AND scored ``0.4``): under ``review`` it earns a native demote stacked with
  the graph penalty — the ``review+graph`` provenance.

Run with: ``python <path-to-this-file>``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

mcp = MCPServer("fake-toolgraph")

_BACKEND_UNAVAILABLE_MESSAGE = "Toolgraph backend is temporarily unavailable; retry later."


def _backend_unavailable() -> CallToolResult:
    payload = {
        "error_kind": "backend_unavailable",
        "retryable": True,
        "message": _BACKEND_UNAVAILABLE_MESSAGE,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=_BACKEND_UNAVAILABLE_MESSAGE)],
        structured_content=payload,
        is_error=True,
    )


# Arbitrary fixed monotonic graph generation so tests can assert the field is
# threaded through verbatim. The #494 consult-cache tests need to vary the
# generation *between* consults of one running server, so it is read from a file
# (path in ``FAKE_TG_GENERATION_FILE``) when set — mutating the file content
# bumps the generation without changing the env *keys* (so the provider
# fingerprint stays stable). Unset → the fixed default below.
_GRAPH_GENERATION = 11


def _generation() -> int:
    path = os.environ.get("FAKE_TG_GENERATION_FILE")
    if path:
        try:
            return int(Path(path).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pass
    return _GRAPH_GENERATION


def _record_call(tool: str, n_candidates: int) -> None:
    """Append ``<tool>:<n_candidates>`` to ``FAKE_TG_CALL_LOG`` when set.

    Lets the #494 tests assert that a cache hit issued only the cheap
    ``eligible_tools([])`` probe (``eligible_tools:0``) and skipped the
    full-candidate ``eligible_tools`` / ``rank_features`` evaluation.
    """
    path = os.environ.get("FAKE_TG_CALL_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{tool}:{n_candidates}\n")


@mcp.tool()
async def eligible_tools(agent: str, candidates: list[str], profile: str = "strict") -> Any:
    """Canned ``eligible_tools`` consult mirroring the real structured shape."""
    _record_call("eligible_tools", len(candidates))
    if profile == "boom":
        raise ValueError(f"unknown profile {profile!r}")
    if profile == "backend_unavailable":
        return _backend_unavailable()
    if profile == "sleep":
        await asyncio.sleep(30)
    if agent == "ghost":
        return {
            "agent": agent,
            "agent_found": False,
            "profile": profile,
            "eligible": [],
            "rejected": [],
            "graph_generation": _generation(),
        }

    eligible: list[str] = []
    rejected: list[dict] = []
    for candidate in candidates:  # input order is part of the upstream contract
        tool_part = candidate.split("::", 1)[-1]
        # "riskyblocked*" is BOTH rejected here AND scored >0 by rank_features —
        # the overlap case (under review: native demote + graph penalty = BOTH).
        if candidate.endswith("::blocked") or tool_part.startswith("riskyblocked"):
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
        "graph_generation": _generation(),
    }


def _unresolved_feature(candidate: str) -> dict:
    """A row for a ref the graph could not resolve to exactly one tool."""
    return {
        "candidate": candidate,
        "tool_key": None,
        "found": False,
        "ambiguous": False,
        "permitted": None,
        "verdict": "TOOL_NOT_FOUND",
        "classification": None,
        "deny_paths": [],
        "is_drifted": None,
        "is_unmapped": None,
        "has_unbacked_edges": None,
        "read_only_hint": None,
        "destructive_hint": None,
        "idempotent_hint": None,
        "open_world_hint": None,
        "risk_score": None,
    }


@mcp.tool()
async def rank_features(agent: str, candidates: list[str]) -> Any:
    """Canned ``rank_features`` consult mirroring the real per-candidate shape.

    Every field of the real row is populated (``selector.rank_features``), not
    just ``risk_score``: since #469 STM logs the whole row per eligible
    candidate, so a fixture that summarized the rest would let a fact reach
    production telemetry having never been exercised here. The values follow
    the same fixed table the real ``_risk_score`` implements — ``risky*``
    scores 0.4 *because* it carries an unbacked load-bearing edge, rather than
    carrying a score with no fact behind it.

    ``agent == "rankboom"`` raises (``isError``) so the best-effort enrichment
    degrade path is exercisable while ``eligible_tools`` still succeeds for the
    same agent.
    """
    _record_call("rank_features", len(candidates))
    if agent == "rankboom":
        raise ValueError("rank_features boom")
    if agent == "rankunavailable":
        return _backend_unavailable()
    if agent == "rankmalformed":
        # A non-error response MISSING the 'features' list — the malformed-but-
        # successful enrichment shape. parse_graph_features leniently yields no
        # penalties and no facts, but #494 must not cache this as a successful
        # capture.
        return {"agent": agent, "agent_found": True, "graph_generation": _generation()}
    if agent == "ghost":
        return {
            "agent": agent,
            "agent_found": False,
            "features": [],
            "graph_generation": _generation(),
        }

    features: list[dict] = []
    for candidate in candidates:  # input order is part of the upstream contract
        tool_part = candidate.split("::", 1)[-1]
        if tool_part.startswith("missing"):
            # Unresolved: nothing but the resolution facts is knowable, and the
            # score is None rather than 0.0 — the distinction #469 exists for.
            features.append(_unresolved_feature(candidate))
            continue
        # "riskyblocked*" is BOTH: not granted (so eligible_tools rejects it)
        # AND carrying an unbacked edge (so it still scores 0.4).
        granted = not (candidate.endswith("::blocked") or tool_part.startswith("riskyblocked"))
        unbacked = tool_part.startswith("risky")
        features.append(
            {
                "candidate": candidate,
                "tool_key": candidate,
                "found": True,
                "ambiguous": False,
                "permitted": granted,
                "verdict": "ALLOW" if granted else "NOT_GRANTED",
                "classification": None,  # no DENY path in this fixture
                "deny_paths": [],
                "is_drifted": False,
                "is_unmapped": False,
                "has_unbacked_edges": unbacked,
                "read_only_hint": tool_part.startswith("read"),
                "destructive_hint": False,
                "idempotent_hint": True,
                "open_world_hint": False,
                # The fixed table, top-down: unbacked edge → 0.4, otherwise
                # clean → 0.0. A grant-only reject is data-flow clean.
                "risk_score": 0.4 if unbacked else 0.0,
            }
        )

    return {
        "agent": agent,
        "agent_found": True,
        "features": features,
        "graph_generation": _generation(),
    }


if __name__ == "__main__":
    mcp.run()
