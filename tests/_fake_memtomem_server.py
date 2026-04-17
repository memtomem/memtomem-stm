"""Tiny stdio MCP server used by integration tests.

Stands in for `memtomem-server` so that STM's McpClientSearchAdapter can be
exercised end-to-end without depending on a real memtomem installation. It
exposes the two tools the adapter actually calls — ``mem_search`` and the
``mem_do`` meta-tool routing the ``scratch_get`` and ``increment_access``
actions — both returning canned text in **core's real compact format**
(``[rank] score | source > hierarchy``) so that integration tests validate
the same parsing path used in production.

**Default mode — content must vary per call.** STM's cross-session dedup
keys on ``sha256(content)[:16]`` (see
``src/memtomem_stm/surfacing/mcp_client.py:34``), so a fixture returning
identical content across calls gets silently suppressed after the first
run if the test exercises the ``FeedbackTracker`` path. The current
integration tests pass ``feedback_enabled=False`` and dodge this, but we
embed per-call UUIDs anyway so the fixture stays safe to drop into a
future test that *does* hit the dedup path. Assertions here are all
substring checks (``"JWT authentication"``, ``"current_task"``) so the
UUID suffixes are invisible to callers.

**bench_qa mode (``--seeds <path>``)** loads a JSON array of search
results and emits them from ``mem_search`` verbatim — *without* the
per-call UUID suffix — so ``sha256(content)`` is deterministic and tests
can assert against pre-computed chunk IDs. The dedup concern does not
apply here because bench_qa scenarios use an isolated ``tmp_path``
``stm_feedback.db`` and make a single ``call_tool`` per test.

Run with: ``python <path-to-this-file>`` (default canned hits) or
``python <path-to-this-file> --seeds <path>`` (fixture-driven).
"""

from __future__ import annotations

import argparse
import json
import uuid

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-memtomem")

# Populated once at startup when ``--seeds`` is passed. Reads like a
# constant after that — ``mem_search`` branches on ``is not None``.
_SEEDS: list[dict] | None = None


def _load_seeds(path: str) -> list[dict]:
    """Load bench_qa seed array from *path*.

    Each entry must have ``rank``, ``score``, ``source``, and ``content``
    keys; extras are ignored. Schema validation is owned by the harness
    that wrote the file, not by this helper.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"--seeds file must contain a JSON array, got {type(data).__name__}")
    return data


def _emit_seeds(seeds: list[dict]) -> str:
    """Format *seeds* as core's compact ``mem_search`` output.

    Matches the default-mode format (``[rank] score | source > Section``
    header, content on the next line) but omits the per-call UUID suffix
    — bench_qa callers require ``sha256(content)`` to be deterministic so
    pre-computed chunk IDs line up with ``surfacing_events.memory_ids``.
    """
    blocks = [f"Found {len(seeds)} results:", ""]
    for seed in seeds:
        blocks.append(f"[{seed['rank']}] {seed['score']} | {seed['source']} > Memory")
        blocks.append(str(seed["content"]))
        blocks.append("")
    return "\n".join(blocks).rstrip() + "\n"


@mcp.tool()
async def mem_search(
    query: str,
    top_k: int | None = None,
    namespace: str | list[str] | None = None,
    context_window: int = 0,
) -> str:
    """Return canned search hits, or fixture seeds if ``--seeds`` was given.

    Matches the output of ``memtomem.server.formatters._format_compact_result``
    so integration tests validate the real parsing path. In the default
    (no-seeds) mode each call embeds a fresh UUID in both the source path
    and the body text so ``sha256(content)`` dedup never collapses repeated
    calls. See the module docstring for the full rationale.
    """
    if _SEEDS is not None:
        return _emit_seeds(_SEEDS)

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
    if action == "version":
        return json.dumps(
            {
                "version": "0.3.0-fake",
                "capabilities": {
                    "search_formats": ["compact", "structured"],
                },
            }
        )
    return f"Error: unknown action '{action}'."


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fake memtomem MCP server.")
    parser.add_argument(
        "--seeds",
        metavar="PATH",
        default=None,
        help="Optional JSON array of mem_search seeds (bench_qa mode).",
    )
    args = parser.parse_args()
    if args.seeds is not None:
        _SEEDS = _load_seeds(args.seeds)
    mcp.run()
