"""Fake memtomem LTM server for notebook 03.

Stands in for a real ``memtomem-server`` so notebook 03 can demonstrate
proactive surfacing without any external dependencies. Exposes the two
tools STM's ``McpClientSearchAdapter`` actually calls — ``mem_search``
and ``mem_do`` — both returning canned responses in **core's real
compact format** (``[rank] score | source > hierarchy``) so the notebook
exercises the same parsing path used in production.

**Why not reuse tests/_fake_memtomem_server.py?** That fixture returns
fixed memory IDs (``/notes/auth.md``, ``/notes/api.md``). STM's
cross-session dedup tracks previously-seen memory IDs in
``~/.memtomem/stm_feedback.db`` for 7 days, so after the first run the
fixed IDs get suppressed on every subsequent run — the notebook would
appear broken. This fixture generates fresh UUIDs per call to dodge the
dedup cache, keeping the notebook reproducible across repeated runs.

Run with: ``python fake_ltm.py``
"""

from __future__ import annotations

import uuid

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-ltm-notebook")


@mcp.tool()
async def mem_search(
    query: str,
    top_k: int | None = None,
    namespace: str | list[str] | None = None,
    context_window: int = 0,
) -> str:
    """Return two canned search hits in core's compact format.

    Matches the output of ``memtomem.server.formatters._format_compact_result``
    so the notebook exercises the same parsing path used in production.

    Critically, STM derives the *chunk ID* used for cross-session dedup
    from ``sha256(content)`` (not from the filename), so the **content
    itself** must change each call. We embed a UUID in each block so
    repeated notebook runs always produce fresh memory chunks and the
    surfacing fires reliably.
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
    """Stand-in for the ``mem_do`` meta-tool.

    Only implements the two actions STM's adapter actually calls:
    ``scratch_get`` (returns a fake working-memory snapshot) and
    ``increment_access`` (returns a synthetic OK response).
    """
    if action == "scratch_get":
        return (
            "Working memory: 2 entries\n"
            "\n"
            "  current_task: reading the memtomem-stm tutorial notebooks...\n"
            "  recent_branch: main..."
        )
    if action == "increment_access":
        chunk_ids = list((params or {}).get("chunk_ids") or [])
        return f"Incremented access_count for {len(chunk_ids)} chunk(s)."
    return f"Error: unknown action '{action}'."


if __name__ == "__main__":
    mcp.run()
