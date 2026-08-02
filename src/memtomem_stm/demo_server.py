"""Bundled deterministic read-only MCP server for the first-success demo."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("memtomem-stm-demo")


@mcp.tool(
    annotations=ToolAnnotations(
        title="Demo project search",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
def demo_search(topic: str = "windows") -> str:
    """Return a deterministic sample result without network or file access."""
    rows = {
        "windows": "Windows 11: use PowerShell and an absolute Python registration.",
        "cache": "Balanced freshness reuses verified read-only results for one hour.",
        "privacy": "Mutating or unverified tools are excluded from the strict cache.",
    }
    key = topic.strip().lower()
    return rows.get(key, f"Demo result for '{topic}': the STM gateway call succeeded.")


if __name__ == "__main__":
    mcp.run(transport="stdio")
