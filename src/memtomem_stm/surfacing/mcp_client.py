"""MCP Client adapter for surfacing — connects to a remote memtomem server."""

from __future__ import annotations

import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from memtomem_stm.surfacing.config import SurfacingConfig

logger = logging.getLogger(__name__)


@dataclass
class RemoteSearchResult:
    """Lightweight search result parsed from mem_search text output."""

    class _FakeMeta:
        def __init__(self, source: str, namespace: str):
            self.source_file = Path(source)
            self.namespace = namespace

    class _FakeChunk:
        def __init__(self, content: str, source: str, namespace: str):
            self.content = content
            self.metadata = RemoteSearchResult._FakeMeta(source, namespace)
            self.id = ""

    def __init__(self, content: str, score: float, source: str = "", namespace: str = "default"):
        self.chunk = self._FakeChunk(content, source, namespace)
        self.score = score


class McpClientSearchAdapter:
    """Connects to a memtomem MCP server via stdio and calls mem_search.

    Implements enough of the SearchPipeline interface for SurfacingEngine.
    """

    def __init__(self, config: SurfacingConfig) -> None:
        self._config = config
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def start(self) -> None:
        """Connect to the memtomem MCP server."""
        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command=self._config.ltm_mcp_command,
            args=self._config.ltm_mcp_args,
        )
        transport = stdio_client(params)
        streams = await self._stack.enter_async_context(transport)
        self._session = await self._stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await self._session.initialize()
        logger.info("MCP client connected to memtomem server: %s", self._config.ltm_mcp_command)

    async def stop(self) -> None:
        """Disconnect from the memtomem MCP server."""
        if self._stack:
            await self._stack.aclose()
            self._stack = None
            self._session = None

    async def search(
        self,
        query: str,
        top_k: int | None = None,
        namespace: str | list[str] | None = None,
        **kwargs: Any,
    ) -> tuple[list[RemoteSearchResult], object]:
        """Call mem_search on the remote server and parse results."""
        if self._session is None:
            return [], None

        args: dict[str, Any] = {"query": query}
        if top_k is not None:
            args["top_k"] = top_k
        if namespace is not None:
            args["namespace"] = namespace

        try:
            result = await self._session.call_tool("mem_search", args)
        except Exception as exc:
            logger.debug("MCP mem_search failed: %s", exc)
            return [], None

        # Parse text response into results
        text_parts = [c.text for c in result.content if c.type == "text"]
        if not text_parts:
            return [], None

        text = "\n".join(text_parts)
        return self._parse_results(text), None

    async def increment_access(self, chunk_ids: list[str]) -> None:
        """Boost the access_count of the given chunks via mem_do(increment_access).

        Used by ``SurfacingEngine.handle_feedback`` when an agent rates a
        surfaced memory as ``helpful``. Failures are silent (debug log
        only) — feedback recording itself must never depend on the boost
        round trip succeeding.
        """
        if self._session is None or not chunk_ids:
            return

        try:
            await self._session.call_tool(
                "mem_do",
                {"action": "increment_access", "params": {"chunk_ids": chunk_ids}},
            )
        except Exception as exc:
            logger.debug("MCP mem_do(increment_access) failed: %s", exc)

    async def scratch_list(self) -> list[dict]:
        """Fetch working memory entries via mem_do(action="scratch_get").

        The remote core's ``mem_scratch_get`` returns a human-readable
        listing when called with no key. We parse it back into the
        ``[{"key": ..., "value": ...}, ...]`` shape that
        :class:`SurfacingFormatter` expects.

        Returns an empty list if the session is not started, the call
        fails, or working memory is empty — surfacing must always be
        able to silently skip session-context injection without losing
        the LTM hits.
        """
        if self._session is None:
            return []

        try:
            result = await self._session.call_tool(
                "mem_do",
                {"action": "scratch_get", "params": {}},
            )
        except Exception as exc:
            logger.debug("MCP mem_do(scratch_get) failed: %s", exc)
            return []

        text_parts = [c.text for c in result.content if c.type == "text"]
        if not text_parts:
            return []

        return self._parse_scratch_list("\n".join(text_parts))

    @staticmethod
    def _parse_scratch_list(text: str) -> list[dict]:
        """Parse ``mem_scratch_get`` listing output into entry dicts.

        Expected format from core (mem_scratch_get with key=None)::

            Working memory: 2 entries

              key1: value preview... (expires: 2026-04-09T12:00:00) [promoted]
              key2: another value...

        Each entry line starts with two leading spaces. The trailing
        ``...`` marker is stripped (core always appends it after the
        truncated preview); ``(expires: ...)`` and ``[promoted]`` are
        captured into optional fields.
        """
        if not text or "Working memory is empty" in text:
            return []

        entries: list[dict] = []
        for line in text.splitlines():
            if not line.startswith("  "):
                continue
            body = line[2:]
            key, sep, rest = body.partition(": ")
            if not sep:
                continue

            value_part = rest
            promoted = False
            if value_part.endswith(" [promoted]"):
                value_part = value_part[: -len(" [promoted]")]
                promoted = True

            expires_at: str | None = None
            expires_match = re.search(r"\s*\(expires:\s*([^)]+)\)\s*$", value_part)
            if expires_match:
                expires_at = expires_match.group(1)
                value_part = value_part[: expires_match.start()]

            if value_part.endswith("..."):
                value_part = value_part[:-3]

            entry: dict = {"key": key, "value": value_part}
            if expires_at is not None:
                entry["expires_at"] = expires_at
            if promoted:
                entry["promoted"] = True
            entries.append(entry)

        return entries

    @staticmethod
    def _parse_results(text: str) -> list[RemoteSearchResult]:
        """Parse mem_search formatted output into RemoteSearchResult objects.

        Expected format per result:
        --- [score] source_file ---
        content...
        """
        results: list[RemoteSearchResult] = []
        # Split by result separators
        blocks = re.split(r"^---\s*", text, flags=re.MULTILINE)

        for block in blocks:
            block = block.strip()
            if not block:
                continue

            # Try to extract score from first line
            first_line, _, rest = block.partition("\n")
            score_match = re.search(r"\[(\d+\.?\d*)\]", first_line)
            score = float(score_match.group(1)) if score_match else 0.5

            # Extract source file
            source_match = re.search(r"(\S+\.md)", first_line)
            source = source_match.group(1) if source_match else "unknown"

            content = rest.strip() if rest else first_line
            if content:
                results.append(
                    RemoteSearchResult(
                        content=content[:500],
                        score=score,
                        source=source,
                    )
                )

        return results
