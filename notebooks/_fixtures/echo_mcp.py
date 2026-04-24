"""Tiny FastMCP echo server used by notebook 01.

Exposes a single ``echo__say`` tool that returns a greeting. Lets the
notebook exercise the STM proxy end-to-end (notebook → STM → echo fixture
→ STM → notebook) without any external services.

Run with: ``python echo_mcp.py``
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
async def say(text: str) -> str:
    """Return the input text prefixed with a greeting."""
    return f"echo says: {text}"


if __name__ == "__main__":
    mcp.run()
