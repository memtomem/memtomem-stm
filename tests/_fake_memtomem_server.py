"""Tiny stdio MCP server used by integration tests.

Stands in for `memtomem-server` so that STM's McpClientSearchAdapter can be
exercised end-to-end without depending on a real memtomem installation. It
exposes the two tools the adapter actually calls — ``mem_search`` and the
``mem_do`` meta-tool routing the ``scratch_get`` and ``increment_access``
actions — both returning canned text in **core's real compact format**
(``[rank] score | source > hierarchy``) so that integration tests validate
the same parsing path used in production.

**Content must vary per call.** STM's cross-session dedup keys on
``sha256(content)[:16]`` (see
``src/memtomem_stm/surfacing/mcp_client.py:34``), so a fixture returning
identical content across calls gets silently suppressed after the first
run if the test exercises the ``FeedbackTracker`` path. The current
integration tests pass ``feedback_enabled=False`` and dodge this, but we
embed per-call UUIDs anyway so the fixture stays safe to drop into a
future test that *does* hit the dedup path. Assertions here are all
substring checks (``"JWT authentication"``, ``"current_task"``) so the
UUID suffixes are invisible to callers.

Run with: `python <path-to-this-file>`
"""

from __future__ import annotations

import uuid

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-memtomem")


@mcp.tool()
async def mem_search(
    query: str,
    top_k: int | None = None,
    namespace: str | list[str] | None = None,
    context_window: int = 0,
) -> str:
    """Return canned search hits in core's compact format.

    Matches the output of ``memtomem.server.formatters._format_compact_result``
    so integration tests validate the real parsing path.  Each call embeds a
    fresh UUID in both the source path and the body text so
    ``sha256(content)`` dedup never collapses repeated calls.
    See the module docstring for the full rationale.
    """
    auth_tag = uuid.uuid4().hex[:8]
    api_tag = uuid.uuid4().hex[:8]
    return (
        "Found 2 results:\n\n"
        f"[1] 0.92 | auth-{auth_tag}.md > Authentication\n"
        f"JWT authentication uses HS256 with rotating secrets every 24 hours. [run={auth_tag}]\n\n"
        f"[2] 0.87 | api-{api_tag}.md > Rate Limiting\n"
        f"All API responses include rate limit headers (X-RateLimit-*). [run={api_tag}]"
    )


@mcp.tool()
async def mem_do(action: str, params: dict | None = None) -> str:
    """Stand-in for the core ``mem_do`` meta-tool.

    Only the actions STM actually calls are implemented; everything else
    returns an unknown-action error matching real core's response.
    """
    if action == "scratch_get":
        return (
            "Working memory: 2 entries\n"
            "\n"
            "  current_task: drafting follow-up 4 implementation plan...\n"
            "  recent_branch: feat/stm-session-context-restore..."
        )
    if action == "increment_access":
        chunk_ids = list((params or {}).get("chunk_ids") or [])
        return f"Incremented access_count for {len(chunk_ids)} chunk(s)."
    return f"Error: unknown action '{action}'."


if __name__ == "__main__":
    mcp.run()
