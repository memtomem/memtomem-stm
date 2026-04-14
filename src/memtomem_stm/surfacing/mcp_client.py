"""MCP Client adapter for surfacing — connects to a remote memtomem server."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
            self.id = hashlib.sha256(content.encode()).hexdigest()[:16]

    def __init__(self, content: str, score: float, source: str = "", namespace: str = "default"):
        self.chunk = self._FakeChunk(content, source, namespace)
        self.score = score


class ResultParser:
    """Strategy interface for parsing mem_search text output."""

    def parse(self, text: str) -> list[RemoteSearchResult]:
        raise NotImplementedError


class CompactResultParser(ResultParser):
    """Parse core's compact format: ``[rank] score | source > hierarchy``."""

    def parse(self, text: str) -> list[RemoteSearchResult]:
        results: list[RemoteSearchResult] = []
        if not text or not text.strip():
            return results

        blocks = re.split(r"^(?=\[\d+\]\s+\d+\.?\d*\s*\|)", text, flags=re.MULTILINE)

        for block in blocks:
            block = block.strip()
            if not block:
                continue

            first_line, _, rest = block.partition("\n")

            header_match = re.match(r"\[(\d+)\]\s+(\d+\.?\d*)\s*\|(.+)", first_line)
            if not header_match:
                continue

            score = float(header_match.group(2))
            remainder = header_match.group(3).strip()

            ns_match = re.match(r"\[([^\]]+)\]\s*(.*)", remainder)
            if ns_match:
                namespace = ns_match.group(1)
                remainder = ns_match.group(2)
            else:
                namespace = "default"

            remainder = re.sub(r"\s*\[\d+/\d+\]\s*$", "", remainder)

            source_match = re.match(r"(\S+)", remainder)
            source = source_match.group(1) if source_match else "unknown"

            content = rest.strip() if rest else ""
            if content:
                if len(content) > 500:
                    logger.debug(
                        "Truncating search result content from %d to 500 chars (source=%s)",
                        len(content),
                        source,
                    )
                results.append(
                    RemoteSearchResult(
                        content=content[:500],
                        score=score,
                        source=source,
                        namespace=namespace,
                    )
                )

        return results


class StructuredResultParser(ResultParser):
    """Parse core's structured JSON format: ``{"results": [...]}``.

    Each element contains ``rank``, ``score``, ``source``, ``hierarchy``,
    ``namespace``, ``chunk_id``, and ``content`` fields.
    """

    def parse(self, text: str) -> list[RemoteSearchResult]:
        if not text or not text.strip():
            return []

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.warning("StructuredResultParser: invalid JSON, falling back to empty")
            return []

        raw_results = data.get("results", [])
        results: list[RemoteSearchResult] = []
        for item in raw_results:
            content = item.get("content", "")
            if not content:
                continue
            if len(content) > 500:
                logger.debug(
                    "Truncating search result content from %d to 500 chars (source=%s)",
                    len(content),
                    item.get("source", "unknown"),
                )
            result = RemoteSearchResult(
                content=content[:500],
                score=float(item.get("score", 0.0)),
                source=item.get("source", "unknown"),
                namespace=item.get("namespace", "default"),
            )
            # Preserve chunk_id from core instead of sha256(content)
            chunk_id = item.get("chunk_id")
            if chunk_id:
                result.chunk.id = chunk_id
            results.append(result)

        return results


def get_parser(fmt: str = "compact") -> ResultParser:
    """Return a ``ResultParser`` for the given format name."""
    if fmt == "structured":
        return StructuredResultParser()
    return CompactResultParser()


_compact_parser = CompactResultParser()


class McpClientSearchAdapter:
    """Connects to a memtomem MCP server via stdio and calls mem_search.

    Implements enough of the SearchPipeline interface for SurfacingEngine.
    """

    def __init__(self, config: SurfacingConfig) -> None:
        self._config = config
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._parser = get_parser(getattr(config, "result_format", "compact"))

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
        await self._negotiate_format()

    async def _negotiate_format(self) -> None:
        """Downgrade to compact if core doesn't advertise structured support.

        Called at the end of ``start()``. When ``result_format`` is
        ``"structured"``, asks the remote server for its capabilities via
        ``mem_do(action="version")``.  If the response doesn't list
        ``"structured"`` in ``capabilities.search_formats`` — or if the
        call fails (older core versions don't implement this action) —
        the parser is silently downgraded to ``CompactResultParser``.
        """
        if not isinstance(self._parser, StructuredResultParser) or self._session is None:
            return

        try:
            result = await self._session.call_tool("mem_do", {"action": "version"})
            text_parts = [c.text for c in result.content if c.type == "text"]
            if text_parts:
                data = json.loads(text_parts[0])
                formats = data.get("capabilities", {}).get("search_formats", [])
                if "structured" in formats:
                    logger.info("Core supports structured format — keeping StructuredResultParser")
                    return
        except Exception as exc:
            logger.debug("Version negotiation failed (older core?): %s", exc)

        logger.info("Core does not advertise structured format — falling back to compact")
        self._parser = CompactResultParser()

    async def stop(self) -> None:
        """Disconnect from the memtomem MCP server."""
        if self._stack:
            await self._stack.aclose()
            self._stack = None
            self._session = None

    _TRANSPORT_ERRORS = (OSError, ConnectionError, EOFError, BrokenPipeError, asyncio.TimeoutError)

    async def _reconnect(self) -> None:
        """Tear down and re-establish the MCP connection."""
        logger.info("Attempting MCP adapter reconnect to %s", self._config.ltm_mcp_command)
        try:
            await self.stop()
        except Exception:
            pass
        await self.start()
        logger.info("MCP adapter reconnected successfully")

    async def search(
        self,
        query: str,
        top_k: int | None = None,
        namespace: str | list[str] | None = None,
        context_window: int | None = None,
        *,
        trace_id: str | None = None,
        **kwargs: Any,
    ) -> tuple[list[RemoteSearchResult], object]:
        """Call mem_search on the remote server and parse results."""
        if self._session is None:
            return [], None

        args: dict[str, Any] = {"query": query}
        if top_k is not None:
            args["top_k"] = top_k
        if namespace is not None:
            # Core's mem_search accepts str|None; normalize lists to
            # comma-separated strings which NamespaceFilter.parse() handles.
            args["namespace"] = ",".join(namespace) if isinstance(namespace, list) else namespace
        if context_window is not None and context_window > 0:
            args["context_window"] = context_window
        if trace_id is not None:
            args["_trace_id"] = trace_id
        if isinstance(self._parser, StructuredResultParser):
            args["output_format"] = "structured"

        try:
            result = await self._session.call_tool("mem_search", args)
        except self._TRANSPORT_ERRORS as exc:
            logger.warning("MCP transport error, attempting reconnect: %s", exc)
            try:
                await self._reconnect()
                result = await self._session.call_tool("mem_search", args)  # type: ignore[union-attr]
            except Exception as retry_exc:
                logger.debug("MCP mem_search failed after reconnect: %s", retry_exc)
                return [], None
        except Exception as exc:
            logger.debug("MCP mem_search failed: %s", exc)
            return [], None

        # Parse text response into results
        text_parts = [c.text for c in result.content if c.type == "text"]
        if not text_parts:
            return [], None

        text = "\n".join(text_parts)
        return self._parser.parse(text), None

    async def increment_access(self, chunk_ids: list[str], *, trace_id: str | None = None) -> None:
        """Boost the access_count of the given chunks via mem_do(increment_access).

        Used by ``SurfacingEngine.handle_feedback`` when an agent rates a
        surfaced memory as ``helpful``. Failures are silent (debug log
        only) — feedback recording itself must never depend on the boost
        round trip succeeding.
        """
        if self._session is None or not chunk_ids:
            return

        call_args: dict[str, Any] = {
            "action": "increment_access",
            "params": {"chunk_ids": chunk_ids},
        }
        if trace_id is not None:
            call_args["_trace_id"] = trace_id

        try:
            await self._session.call_tool("mem_do", call_args)
        except self._TRANSPORT_ERRORS as exc:
            logger.warning("MCP transport error in increment_access, reconnecting: %s", exc)
            try:
                await self._reconnect()
                await self._session.call_tool("mem_do", call_args)  # type: ignore[union-attr]
            except Exception as retry_exc:
                logger.debug("MCP mem_do(increment_access) failed after reconnect: %s", retry_exc)
        except Exception as exc:
            logger.debug("MCP mem_do(increment_access) failed: %s", exc)

    async def scratch_list(self, *, trace_id: str | None = None) -> list[dict]:
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

        call_args: dict[str, Any] = {"action": "scratch_get", "params": {}}
        if trace_id is not None:
            call_args["_trace_id"] = trace_id

        try:
            result = await self._session.call_tool("mem_do", call_args)
        except self._TRANSPORT_ERRORS as exc:
            logger.warning("MCP transport error in scratch_list, reconnecting: %s", exc)
            try:
                await self._reconnect()
                result = await self._session.call_tool(  # type: ignore[union-attr]
                    "mem_do",
                    call_args,
                )
            except Exception:
                return []
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

        Keys may contain ``: `` (e.g., ``db: config``).  Core always
        appends ``...`` after the value preview, so we split from the
        right at the *last* ``: `` that precedes a value ending in
        ``...`` (or metadata).  If the text has no trailing ``...``,
        fall back to the first ``: `` split (best-effort).
        """
        if not text or "Working memory is empty" in text:
            return []

        entries: list[dict] = []
        for line in text.splitlines():
            if not line.startswith("  "):
                continue
            body = line[2:]

            # Best-effort key/value split.  Core always appends "..."
            # after the value preview, so look for the last ": " that
            # sits before trailing markers.  Fall back to first ": ".
            if "..." in body:
                # Find the ": " closest to the trailing "..." marker
                trail_pos = body.rfind("...")
                sep_pos = body.rfind(": ", 0, trail_pos)
                if sep_pos < 0:
                    sep_pos = body.find(": ")
            else:
                sep_pos = body.find(": ")

            if sep_pos < 0:
                continue
            key = body[:sep_pos]
            rest = body[sep_pos + 2 :]

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

        Delegates to :class:`CompactResultParser`. Kept as a static method
        for backward compatibility with existing tests and callers.
        """
        return _compact_parser.parse(text)
